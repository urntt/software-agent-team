"""Tests for adaptive Planning dialogue, validation, approval, and evidence."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

import software_agent_team.planning as planning
from software_agent_team.artifacts import (
    AcceptanceCriterion,
    ArtifactKind,
    DeliveryMaturity,
    ProductDefinition,
    ProductDefinitionDimension,
    ProductDefinitionDisposition,
    ProductDefinitionImpact,
    ProductDefinitionStatement,
    ProductMaturityDefinition,
    ProviderLivenessEvidence,
    ReviewBoundaryKind,
)
from software_agent_team.budgets import (
    AgentBudget,
    AgentBudgetLedger,
    AgentBudgetUsage,
    BudgetAuthority,
    ModelPricing,
)
from software_agent_team.execution import (
    AgentExecutionActivity,
    AgentExecutionActivityKind,
    AgentExecutionTelemetry,
    AgentTokenUsage,
    AgentToolActionClass,
    AgentToolTargetClass,
    ScriptedAgentExecutor,
    ScriptedAgentResponse,
)
from software_agent_team.integrity import canonical_model_sha256
from software_agent_team.invocation_lifecycle import InvocationPhase
from software_agent_team.model_costs import CachePricing
from software_agent_team.model_metadata import ModelMetadataSource
from software_agent_team.model_routing import ModelProfile, ModelRoutingPolicy
from software_agent_team.planning import (
    AdaptivePlanningCoordinator,
    AgentWorkload,
    ApprovedPlanningResult,
    CapabilityTimeoutPolicy,
    PlanningActivity,
    PlanningActivityKind,
    PlanningDecisionAuthority,
    PlanningDecisionCategory,
    PlanningDecisionProvenance,
    PlanningDecisionProvenanceKind,
    PlanningDecisionRecord,
    PlanningError,
    PlanningIntegrityError,
    PlanningModelResponse,
    PlanningOption,
    PlanningOptionValue,
    PlanningPolicy,
    PlanningPreview,
    PlanningProposal,
    PlanningProposalBody,
    PlanningProposalSource,
    PlanningQuestion,
    PlanningQuestionAnswer,
    PlanningQuestionOrigin,
    PlanningRequest,
    PlanningResponseKind,
    PlanningSessionStatus,
    PlanningStore,
    PlanningTurn,
    PresentedPlanningQuestion,
    ProposedAgent,
    ProposedCriterion,
    ProposedTask,
    StructuredEditKind,
    StructuredPlanEdit,
    TerminalPlanningProgress,
    apply_structured_edit,
    compile_approved_review_scopes,
    preview_adaptive_proposal,
    render_planning_overview,
    run_interactive_planning,
)
from software_agent_team.response_corrections import (
    ResponseFailureClass,
    ResponseIssueAuthority,
    SemanticCorrectionOutcome,
    semantic_correction_slot_handle,
)
from software_agent_team.submissions import AgentSubmissionPurpose
from software_agent_team.teams import (
    AgentCapability,
    AgentSpecialization,
    ModelRouteSelectionSource,
    ModelRoutingMode,
    ModelSwitchCondition,
    PermissionProfile,
    TeamPlanOrigin,
)

FIXED_TIME = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
AMBIGUOUS_LINK_REQUEST = (
    "For developers who will use it repeatedly, build a usable local product that "
    "checks Markdown links."
)


class AdvancingClock:
    """Return deterministic increasing timestamps for persisted evidence."""

    def __init__(self) -> None:
        self.current = FIXED_TIME

    def __call__(self) -> datetime:
        value = self.current
        self.current += timedelta(seconds=1)
        return value


def request(
    *,
    source_request: str = (
        "For developers who will use it repeatedly, build a usable local product "
        "that checks Markdown links in files and fragments without fetching "
        "remote URLs."
    ),
) -> PlanningRequest:
    """Return direct input with explicit pre-model authorization."""

    return PlanningRequest(
        run_id="sat-adaptive-001",
        project_name="link-checker",
        source_request=source_request,
        destination="/tmp/link-checker",
        execution_profile=(
            "Small greenfield Python 3.12 project",
            "No network access during deterministic verification",
        ),
        base_constraints=("Use the versioned uv environment",),
        model="provider/model",
        authorization="user_confirmed",
        authorized_at=FIXED_TIME,
    )


def policy(**updates: object) -> PlanningPolicy:
    """Return the controller authority used by adaptive plan tests."""

    values: dict[str, object] = {
        "budget": AgentBudget(
            max_calls=14,
            max_input_tokens=1_000_000,
            max_output_tokens=200_000,
            max_agent_duration_seconds=7_200,
            max_estimated_cost_usd="25",
        ),
        "capability_timeouts": {
            AgentCapability.IMPLEMENTATION: CapabilityTimeoutPolicy(
                default_seconds=600,
                ceiling_seconds=900,
            ),
            AgentCapability.INTEGRATION: CapabilityTimeoutPolicy(
                default_seconds=600,
                ceiling_seconds=900,
            ),
            AgentCapability.TESTING: CapabilityTimeoutPolicy(
                default_seconds=240,
                ceiling_seconds=300,
            ),
            AgentCapability.REVIEW: CapabilityTimeoutPolicy(
                default_seconds=240,
                ceiling_seconds=300,
            ),
        },
    }
    values.update(updates)
    return PlanningPolicy.model_validate(values)


def proposal_body(
    *,
    title: str = "Markdown Link Checker",
    question_id: str | None = None,
) -> PlanningProposalBody:
    """Return one complete task-defined team proposal."""

    decisions = (
        PlanningDecisionRecord(
            id="DECISION_ACCEPTANCE",
            category=PlanningDecisionCategory.ACCEPTANCE_SCOPE,
            authority=PlanningDecisionAuthority.PLANNER_PROPOSAL,
            provenance=PlanningDecisionProvenance(
                kind=PlanningDecisionProvenanceKind.PLANNER_RECOMMENDATION,
                source="planner",
            ),
            summary="Verify observable scan failures and diagnostic locations.",
            rationale="These behaviors make the requested CLI testable.",
        ),
        PlanningDecisionRecord(
            id="DECISION_DELIVERY",
            category=PlanningDecisionCategory.DELIVERY,
            authority=PlanningDecisionAuthority.PLANNER_PROPOSAL,
            provenance=PlanningDecisionProvenance(
                kind=PlanningDecisionProvenanceKind.PLANNER_RECOMMENDATION,
                source="planner",
            ),
            summary="Deliver one runnable local CLI project.",
            rationale="The request is for a local command-line tool.",
        ),
        PlanningDecisionRecord(
            id="DECISION_TEAM",
            category=PlanningDecisionCategory.TEAM,
            authority=PlanningDecisionAuthority.PLANNER_PROPOSAL,
            provenance=PlanningDecisionProvenance(
                kind=PlanningDecisionProvenanceKind.PLANNER_RECOMMENDATION,
                source="planner",
            ),
            summary="Use one writer and two downstream quality Agents.",
            rationale="The cohesive implementation still needs independent checks.",
        ),
        PlanningDecisionRecord(
            id="DECISION_MODEL_ROUTE",
            category=PlanningDecisionCategory.MODEL_ROUTE,
            authority=PlanningDecisionAuthority.PLANNER_PROPOSAL,
            provenance=PlanningDecisionProvenance(
                kind=PlanningDecisionProvenanceKind.PLANNER_RECOMMENDATION,
                source="planner",
            ),
            summary="Use capability-compatible configured model routes.",
            rationale="No task evidence justifies a route override.",
        ),
        PlanningDecisionRecord(
            id="DECISION_SCAN_STRUCTURE",
            category=PlanningDecisionCategory.LOCAL_IMPLEMENTATION,
            authority=PlanningDecisionAuthority.AGENT_AUTONOMY,
            provenance=PlanningDecisionProvenance(
                kind=PlanningDecisionProvenanceKind.AGENT_AUTONOMY,
                source="agent",
            ),
            summary="Use one single-process scan before considering parallelism.",
            rationale="The small initial workload does not justify added coordination.",
        ),
    )
    if question_id is not None:
        decisions = (
            *decisions,
            PlanningDecisionRecord(
                id="DECISION_LINK_SCOPE_ANSWER",
                category=PlanningDecisionCategory.PRODUCT_REQUIREMENT,
                authority=PlanningDecisionAuthority.USER,
                provenance=PlanningDecisionProvenance(
                    kind=PlanningDecisionProvenanceKind.RESOLVED_QUESTION,
                    source=question_id,
                ),
                summary="Keep the first release limited to local links.",
                rationale="The user selected the deterministic local-only option.",
            ),
        )
    return PlanningProposalBody(
        title=title,
        product_definition=ProductDefinition(
            target_users=ProductDefinitionStatement(
                statement="developers",
                disposition=ProductDefinitionDisposition.EXPLICIT_INPUT,
                source="developers",
                rationale="The request names developers as the recurring users.",
                requirement_ids=("REQ_SCAN",),
            ),
            primary_workflow=ProductDefinitionStatement(
                statement="checks Markdown links",
                disposition=ProductDefinitionDisposition.EXPLICIT_INPUT,
                source="checks Markdown links",
                rationale="The requested scan is the primary repeated workflow.",
                requirement_ids=("REQ_SCAN", "REQ_REPORT"),
            ),
            delivery_maturity=ProductMaturityDefinition(
                level=DeliveryMaturity.USABLE_LOCAL_PRODUCT,
                disposition=ProductDefinitionDisposition.EXPLICIT_INPUT,
                source="usable local product",
                rationale="The request explicitly asks for a reusable local tool.",
                requirement_ids=("REQ_SCAN",),
            ),
            usability_expectations=ProductDefinitionStatement(
                statement="Failures are actionable from ordinary terminal output.",
                disposition=ProductDefinitionDisposition.PLANNER_RECOMMENDATION,
                source="planner",
                rationale="A repeatedly used CLI needs understandable diagnostics.",
                criterion_ids=("AC_REPORT",),
                decision_ids=("DECISION_ACCEPTANCE",),
            ),
            operational_expectations=ProductDefinitionStatement(
                statement="Local scans are deterministic and avoid network access.",
                disposition=ProductDefinitionDisposition.PLANNER_RECOMMENDATION,
                source="planner",
                rationale="Deterministic local operation matches the approved scope.",
                criterion_ids=("AC_SCAN",),
                decision_ids=("DECISION_ACCEPTANCE",),
            ),
            delivery_expectations=ProductDefinitionStatement(
                statement="Deliver a documented, runnable local CLI project.",
                disposition=ProductDefinitionDisposition.PLANNER_RECOMMENDATION,
                source="planner",
                rationale="The user needs a repeatable project rather than a snippet.",
                requirement_ids=("REQ_SCAN",),
                decision_ids=("DECISION_DELIVERY",),
            ),
            impact=ProductDefinitionImpact(
                architecture="Separate scanning from terminal presentation.",
                team="Use one cohesive writer with downstream quality authority.",
                cost="Keep the small cohesive implementation within the task budget.",
                delivery="Include runnable commands, tests, and user documentation.",
            ),
        ),
        requirements=(
            "Scan Markdown files below a selected path.",
            "Report broken local links with source locations.",
        ),
        requirement_ids=("REQ_SCAN", "REQ_REPORT"),
        non_goals=("Fetching or validating remote web links is not included.",),
        acceptance_criteria=(
            ProposedCriterion(
                id="AC_SCAN",
                description="Broken local links produce a non-zero exit status.",
                verification="Run the CLI against valid and broken fixtures.",
                requirement_ids=("REQ_SCAN",),
                verification_agent_ids=("acceptance_tester",),
            ),
            ProposedCriterion(
                id="AC_REPORT",
                description="Every failure includes the file and line number.",
                verification="Assert structured output for a broken fixture.",
                requirement_ids=("REQ_REPORT",),
                verification_agent_ids=("acceptance_tester", "quality_reviewer"),
            ),
        ),
        constraints=("Use only the standard library at runtime.",),
        assumptions=("The initial implementation uses a single-process scan.",),
        assumption_decision_ids=("DECISION_SCAN_STRUCTURE",),
        decisions=decisions,
        objective="Deliver a documented, tested Markdown link-checking CLI.",
        approach=(
            "Parse Markdown link targets without fetching network resources.",
            "Separate scanning, resolution, and CLI presentation.",
        ),
        tasks=(
            ProposedTask(
                id="TASK_IMPLEMENT",
                owner_agent_id="cli_developer",
                description="Implement scanning, resolution, and CLI output.",
                acceptance_criteria=("AC_SCAN", "AC_REPORT"),
                expected_paths=("src", "tests"),
            ),
        ),
        risks=("Markdown edge cases may require explicit documented limits.",),
        agents=(
            ProposedAgent(
                id="cli_developer",
                label="CLI Developer",
                responsibility="Implement the complete CLI and its focused tests.",
                rationale="The small cohesive codebase does not need split writers.",
                capability=AgentCapability.IMPLEMENTATION,
                stage_id="implement",
                workspace_scope="repository",
                workload=AgentWorkload.ROUTINE,
            ),
            ProposedAgent(
                id="acceptance_tester",
                label="Acceptance Tester",
                responsibility="Verify deterministic behavior against every criterion.",
                rationale="Testing remains independent from implementation.",
                capability=AgentCapability.TESTING,
                stage_id="verify",
                dependencies=("cli_developer",),
                workspace_scope="repository",
                workload=AgentWorkload.ROUTINE,
            ),
            ProposedAgent(
                id="quality_reviewer",
                label="Quality Reviewer",
                responsibility="Review correctness, maintainability, and evidence.",
                rationale="A separate reviewer prevents self-approval.",
                capability=AgentCapability.REVIEW,
                stage_id="verify",
                dependencies=("cli_developer",),
                workspace_scope="repository",
                workload=AgentWorkload.ROUTINE,
            ),
        ),
        iteration_limit=2,
        max_concurrency=2,
        revision_enabled=True,
    )


def proposal(
    *,
    revision: int = 1,
    body: PlanningProposalBody | None = None,
) -> PlanningProposal:
    return PlanningProposal(
        run_id=request().run_id,
        revision=revision,
        created_at=FIXED_TIME,
        source=PlanningProposalSource.MODEL,
        source_turn_sequence=revision,
        body=body or proposal_body(),
    )


def strip_v8_decision_fields(body_payload: dict[str, object]) -> None:
    """Turn one current proposal-body payload into its schema-v7 decision shape."""

    decisions = body_payload["decisions"]
    assert isinstance(decisions, list)
    for decision in decisions:
        assert isinstance(decision, dict)
        provenance = decision.pop("provenance", None)
        if (
            isinstance(provenance, dict)
            and provenance.get("kind") == "resolved_question"
        ):
            decision["question_id"] = provenance["source"]


def test_planning_overview_separates_constraint_authority_without_losing_data() -> None:
    body = proposal_body().model_copy(
        update={
            "constraints": (
                "Use the versioned uv environment",
                *proposal_body().constraints,
            )
        }
    )
    preview = preview_adaptive_proposal(
        request(),
        proposal(body=body),
        policy(),
        created_at=FIXED_TIME,
    )

    assert preview.task_brief.constraints == [
        "Use the versioned uv environment",
        "Use only the standard library at runtime.",
    ]
    overview = render_planning_overview(preview)
    assert "Fixed policy details: hidden" in overview
    assert "Use the versioned uv environment" not in overview
    assert "Additional task constraints proposed by Planning:" in overview
    assert preview.planner_constraints == ("Use only the standard library at runtime.",)
    assert overview.count("Use only the standard library at runtime.") == 1
    expanded = render_planning_overview(preview, include_fixed_policy=True)
    assert "Execution-profile constraints (controller-owned):" in expanded
    assert expanded.count("Use the versioned uv environment") == 1
    assert expanded.count("Use only the standard library at runtime.") == 1
    assert "Product definition and scope:" in overview
    assert "target users [explicit_input]: developers" in overview
    assert "primary workflow [explicit_input]: checks Markdown links" in overview
    assert "delivery maturity: usable_local_product [explicit_input]" in overview
    assert "Product-definition effects:" in overview
    assert "architecture: Separate scanning from terminal presentation." in overview
    assert "REQ_SCAN: Scan Markdown files" in overview
    assert "Non-goals:" in overview
    assert "Decisions and assumptions:" in overview
    assert "Planning recommendations requiring approval:" in overview
    assert "none beyond the user-owned source request shown above" in overview
    assert "Non-negotiable Controller policy:" not in overview
    assert "Non-negotiable Controller policy:" in expanded
    assert "Requirement-to-evidence traceability:" in overview
    assert "AC_SCAN: writers=TASK_IMPLEMENT->cli_developer" in overview
    assert "independent verification=acceptance_tester" in overview
    assert "inputs: approved TaskBrief and implementation plan" in overview
    assert "output: work_result" in overview
    assert (
        "handoff: durable artifact to acceptance_tester, quality_reviewer" in overview
    )
    assert "Failure and delivery boundary:" in overview


def test_planning_overview_contains_multiline_text_inside_its_own_entry() -> None:
    body = proposal_body()
    definition = body.product_definition
    assert definition is not None
    body = body.model_copy(
        update={
            "product_definition": definition.model_copy(
                update={
                    "impact": definition.impact.model_copy(
                        update={
                            "architecture": (
                                "Separate scanning from presentation.\nTasks:"
                            )
                        }
                    )
                }
            ),
            "requirements": (
                "REQ_SCAN: Scan Markdown files.\nRuntime Agents:",
                body.requirements[1],
            ),
            "non_goals": (
                "No remote fetching.\n  Requirements:\x1b[2J\u2028Runtime Agents:",
            ),
        }
    )
    preview = preview_adaptive_proposal(
        request(),
        proposal(body=body),
        policy(),
        created_at=FIXED_TIME,
    )

    overview = render_planning_overview(preview)
    lines = overview.splitlines()

    impact_index = lines.index(
        "    - architecture: Separate scanning from presentation."
    )
    assert lines[impact_index + 1] == "                    Tasks:"
    non_goal_index = lines.index("    - No remote fetching.")
    assert lines[non_goal_index + 1] == (
        "        Requirements:\\x1b[2J\\u2028Runtime Agents:"
    )
    requirement_index = lines.index("    - REQ_SCAN: Scan Markdown files.")
    assert lines[requirement_index + 1] == "                Runtime Agents:"
    assert "\x1b" not in overview
    assert "\u2028" not in overview
    assert "REQ_SCAN: REQ_SCAN:" not in overview


def response(value: PlanningModelResponse) -> str:
    payload = value.model_dump(mode="json")
    if value.kind is PlanningResponseKind.QUESTION:
        assert value.question is not None
        question = payload["question"]
        assert isinstance(question, dict)
        question.setdefault(
            "product_definition_dimensions",
            [item.value for item in value.question.product_definition_dimensions],
        )
    else:
        assert value.proposal is not None
        payload["proposal"] = planning._planning_proposal_body_for_model(value.proposal)
    return json.dumps(payload)


def test_planner_contract_does_not_treat_provenance_as_semantic_relevance() -> None:
    contract = planning.PLANNING_TEMPLATE.read_text(encoding="utf-8")

    assert "An exact substring is necessary provenance, not proof" in contract
    assert "a build instruction, product type" in contract
    assert "typed `agents` graph is the only team-topology owner" in contract
    assert "Every Review-owned task may reference only criteria" in contract
    assert "cannot be used as target users" in contract
    assert (
        "must leave `requirement_ids`, `criterion_ids`, and `decision_ids` empty"
        in (contract)
    )


def correction_response(
    base_payload: dict[str, object],
    replacements: dict[str, object],
    *,
    target_paths: tuple[str, ...] | None = None,
) -> str:
    target_paths = tuple(sorted(replacements)) if target_paths is None else target_paths
    return json.dumps(
        {
            "replacements": [
                {
                    "slot_handle": semantic_correction_slot_handle(
                        target_paths,
                        path,
                    ),
                    "replacement_value": replacements[path],
                }
                for path in replacements
            ],
        }
    )


def question_response() -> PlanningModelResponse:
    return PlanningModelResponse(
        kind=PlanningResponseKind.QUESTION,
        question=PlanningQuestion(
            id="link_scope",
            text="Should the first version check web links too?",
            why="Network access changes reliability and test architecture.",
            decision_category=PlanningDecisionCategory.PRODUCT_REQUIREMENT,
            decision_owner=PlanningDecisionAuthority.USER,
            missing_evidence=(
                "The request does not state whether remote URLs are in scope.",
            ),
            material_consequences=(
                "The answer changes acceptance, network access, and retry behavior.",
            ),
            options=(
                PlanningOption(
                    id="local_only",
                    label="Local only",
                    description="Check files and fragments deterministically.",
                ),
                PlanningOption(
                    id="include_web",
                    label="Include web",
                    description="Add network requests and retry behavior.",
                ),
            ),
        ),
    )


def product_intent_question_response() -> PlanningModelResponse:
    """Ask one user-owned question that resolves only the audience dimension."""

    return PlanningModelResponse(
        kind=PlanningResponseKind.QUESTION,
        question=PlanningQuestion(
            id="product_intent",
            text="Who will repeatedly use the delivered local tool?",
            why="The audience changes usability, distribution, and documentation.",
            decision_category=PlanningDecisionCategory.PRODUCT_REQUIREMENT,
            decision_owner=PlanningDecisionAuthority.USER,
            missing_evidence=("The short request does not identify an audience.",),
            material_consequences=(
                "Different audiences need different usability and delivery choices.",
            ),
            product_definition_dimensions=(ProductDefinitionDimension.TARGET_USERS,),
            options=(
                PlanningOption(
                    id="individual_developer",
                    label="Individual developer",
                    description="One developer repeatedly scans personal notes.",
                    product_definition_values=(
                        PlanningOptionValue(
                            dimension=ProductDefinitionDimension.TARGET_USERS,
                            value="Individual developer",
                        ),
                    ),
                ),
                PlanningOption(
                    id="researchers",
                    label="Developers and researchers",
                    description=(
                        "Developers and researchers use the reports for local "
                        "Markdown collections."
                    ),
                    product_definition_values=(
                        PlanningOptionValue(
                            dimension=ProductDefinitionDimension.TARGET_USERS,
                            value="Developers and researchers",
                        ),
                    ),
                ),
            ),
        ),
    )


def resolved_product_body() -> PlanningProposalBody:
    """Return a proposal whose audience came from product_intent."""

    body = proposal_body(question_id="product_intent")
    definition = body.product_definition
    assert definition is not None
    decision_id = "DECISION_LINK_SCOPE_ANSWER"
    return body.model_copy(
        update={
            "product_definition": definition.model_copy(
                update={
                    "target_users": definition.target_users.model_copy(
                        update={
                            "statement": "Developers and researchers",
                            "disposition": (
                                ProductDefinitionDisposition.RESOLVED_QUESTION
                            ),
                            "source": "product_intent",
                            "decision_ids": (decision_id,),
                        }
                    ),
                }
            )
        }
    )


def proposal_response(
    body: PlanningProposalBody | None = None,
) -> PlanningModelResponse:
    return PlanningModelResponse(
        kind=PlanningResponseKind.PROPOSAL,
        proposal=body or proposal_body(),
    )


def test_planning_request_requires_explicit_pre_model_authorization() -> None:
    payload = request().model_dump(mode="json")
    payload["authorization"] = "assumed"

    with pytest.raises(ValidationError, match="user_confirmed"):
        PlanningRequest.model_validate(payload)


def test_planning_uses_typed_submission_instead_of_assistant_text(
    tmp_path: Path,
) -> None:
    payload = proposal_response().model_dump(mode="json")
    executor = ScriptedAgentExecutor(
        [
            ScriptedAgentResponse(
                text="This presentation is deliberately not JSON.",
                submission_payload=payload,
            )
        ]
    )
    store = PlanningStore(tmp_path / "planning")
    coordinator = AdaptivePlanningCoordinator(
        executor=executor,
        store=store,
        policy=policy(),
        clock=AdvancingClock(),
    )

    created = coordinator.start(
        request(),
        answer_question=lambda _question: pytest.fail("unexpected question"),
    )

    assert created is not None
    contract = executor.requests[0].submission_contract
    assert contract is not None
    assert contract.purpose is AgentSubmissionPurpose.PLANNING_RESPONSE
    assert contract.transport_payload_schema() == {
        "type": "object",
        "additionalProperties": True,
    }
    assert contract.transport_schema() == {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "artifact": {
                "type": "object",
                "additionalProperties": True,
            }
        },
        "required": ["artifact"],
    }
    turn = store.load_turn(request().run_id, 1)
    assert turn.response_text == "This presentation is deliberately not JSON."
    assert turn.submission_payload == payload
    assert turn.submission_evidence is not None
    assert turn.parsed_response == proposal_response()

    without_presentation = turn.model_dump(mode="json")
    without_presentation["response_text"] = None
    without_presentation["response_sha256"] = None
    loaded = PlanningTurn.model_validate(without_presentation)
    assert loaded.response_text is None
    assert loaded.parsed_response == proposal_response()


def test_planning_captures_extra_fields_before_deterministic_normalization(
    tmp_path: Path,
) -> None:
    payload = proposal_response().model_dump(mode="json")
    payload["proposal"]["non_goals_note"] = "legacy extra field"
    executor = ScriptedAgentExecutor(
        [ScriptedAgentResponse(text="ignored", submission_payload=payload)]
    )
    store = PlanningStore(tmp_path / "planning")
    coordinator = AdaptivePlanningCoordinator(
        executor=executor,
        store=store,
        policy=policy(),
        clock=AdvancingClock(),
    )

    created = coordinator.start(
        request(),
        answer_question=lambda _question: pytest.fail("unexpected question"),
    )

    assert created is not None
    assert len(executor.requests) == 1
    turn = store.load_turn(request().run_id, 1)
    assert turn.submission_payload == payload
    assert turn.response_normalizations == (
        "removed schema-forbidden field /proposal/non_goals_note",
    )


def test_planning_repairs_schema_then_all_invalid_product_dimensions_together(
    tmp_path: Path,
) -> None:
    valid_body = proposal_body()
    valid_definition = valid_body.product_definition
    assert valid_definition is not None
    invalid_definition = valid_definition.model_copy(
        update={
            "target_users": valid_definition.target_users.model_copy(
                update={
                    "source": 'exact quote: "developers"',
                    "statement": "developers",
                }
            ),
            "primary_workflow": valid_definition.primary_workflow.model_copy(
                update={
                    "source": 'exact quote: "checks Markdown links"',
                    "statement": "checks Markdown links",
                }
            ),
            "delivery_maturity": valid_definition.delivery_maturity.model_copy(
                update={"source": 'exact quote: "usable local product"'}
            ),
            "usability_expectations": (
                valid_definition.usability_expectations.model_copy(
                    update={"decision_ids": ("DECISION_MODEL_ROUTE",)}
                )
            ),
            "operational_expectations": (
                valid_definition.operational_expectations.model_copy(
                    update={"decision_ids": ("DECISION_SCAN_STRUCTURE",)}
                )
            ),
        }
    )
    initial = proposal_response(
        valid_body.model_copy(update={"product_definition": invalid_definition})
    ).model_dump(mode="json")
    maturity = initial["proposal"]["product_definition"]["delivery_maturity"]
    maturity.pop("level")
    maturity["statement"] = "schema-forbidden presentation"
    first_correction_base = json.loads(json.dumps(initial))
    del first_correction_base["proposal"]["product_definition"]["delivery_maturity"][
        "statement"
    ]
    after_maturity = json.loads(json.dumps(first_correction_base))
    after_maturity["proposal"]["product_definition"]["delivery_maturity"]["level"] = (
        "usable_local_product"
    )
    executor = ScriptedAgentExecutor(
        [
            ScriptedAgentResponse(text="ignored", submission_payload=initial),
            ScriptedAgentResponse(
                text="ignored",
                submission_payload=json.loads(
                    correction_response(
                        first_correction_base,
                        {
                            "/proposal/product_definition/delivery_maturity/level": (
                                "usable_local_product"
                            )
                        },
                    )
                ),
            ),
            ScriptedAgentResponse(
                text="ignored",
                submission_payload=json.loads(
                    correction_response(
                        after_maturity,
                        {
                            "/proposal/product_definition/target_users": (
                                valid_definition.target_users.model_dump(mode="json")
                            ),
                            "/proposal/product_definition/primary_workflow": (
                                valid_definition.primary_workflow.model_dump(
                                    mode="json"
                                )
                            ),
                            "/proposal/product_definition/usability_expectations": (
                                valid_definition.usability_expectations.model_dump(
                                    mode="json"
                                )
                            ),
                            "/proposal/product_definition/operational_expectations": (
                                valid_definition.operational_expectations.model_dump(
                                    mode="json"
                                )
                            ),
                            "/proposal/product_definition/delivery_maturity": (
                                valid_definition.delivery_maturity.model_dump(
                                    mode="json"
                                )
                            ),
                        },
                    )
                ),
            ),
        ]
    )
    store = PlanningStore(tmp_path / "planning")
    coordinator = AdaptivePlanningCoordinator(
        executor=executor,
        store=store,
        policy=policy(response_repair_limit=2),
        clock=AdvancingClock(),
    )

    created = coordinator.start(
        request(),
        answer_question=lambda _question: pytest.fail("unexpected question"),
    )

    assert created is not None
    assert len(executor.requests) == 3
    first = store.load_turn(request().run_id, 1)
    second = store.load_turn(request().run_id, 2)
    third = store.load_turn(request().run_id, 3)
    assert first.response_validation is not None
    assert first.response_validation.correction_paths == (
        "/proposal/product_definition/delivery_maturity/level",
    )
    assert second.response_validation is not None
    assert second.response_validation.correction_paths == (
        "/proposal/product_definition/delivery_maturity",
        "/proposal/product_definition/operational_expectations",
        "/proposal/product_definition/primary_workflow",
        "/proposal/product_definition/target_users",
        "/proposal/product_definition/usability_expectations",
    )
    assert third.semantic_correction_outcome is not None
    assert third.parsed_response == proposal_response()
    assert '"value_schema"' in executor.requests[1].prompt
    assert "one contiguous verbatim substring" in executor.requests[2].prompt


def test_planning_replays_legacy_direct_decisions_through_reachable_slots(
    tmp_path: Path,
) -> None:
    valid_body = proposal_body()
    valid_definition = valid_body.product_definition
    assert valid_definition is not None
    initial = proposal_response().model_dump(mode="json")
    proposal_payload = initial["proposal"]
    for decision in proposal_payload["decisions"]:
        decision.pop("provenance")
    proposal_payload["decisions"][1]["authority"] = "user"
    proposal_payload["decisions"][:0] = [
        {
            "id": "DECISION_TARGET_USERS",
            "category": "product_requirement",
            "authority": "user",
            "summary": "Developers are the intended users.",
            "rationale": "The request mentions developers.",
        },
        {
            "id": "DECISION_WORKFLOW",
            "category": "product_requirement",
            "authority": "user",
            "summary": "The primary workflow checks Markdown links.",
            "rationale": "The request names that workflow.",
        },
    ]
    definition = proposal_payload["product_definition"]
    definition["target_users"].update(
        {
            "statement": "Developers who repeatedly run the tool.",
            "decision_ids": ["DECISION_TARGET_USERS"],
        }
    )
    definition["primary_workflow"].update(
        {
            "statement": "Run a complete Markdown link-checking workflow.",
            "decision_ids": ["DECISION_WORKFLOW"],
        }
    )
    definition["delivery_maturity"]["decision_ids"] = ["DECISION_DELIVERY"]
    correction_base, _ = planning._normalize_planning_response_payload(
        initial,
        profile_criterion_ids=(
            criterion.id for criterion in policy().profile_acceptance_criteria
        ),
        user_inputs=(request().source_request,),
    )
    executor = ScriptedAgentExecutor(
        [
            ScriptedAgentResponse(text="ignored", submission_payload=initial),
            ScriptedAgentResponse(
                text="ignored",
                submission_payload=json.loads(
                    correction_response(
                        correction_base,
                        {
                            "/proposal/product_definition/target_users": (
                                valid_definition.target_users.model_dump(mode="json")
                            ),
                            "/proposal/product_definition/primary_workflow": (
                                valid_definition.primary_workflow.model_dump(
                                    mode="json"
                                )
                            ),
                        },
                    )
                ),
            ),
        ]
    )
    store = PlanningStore(tmp_path / "planning")
    coordinator = AdaptivePlanningCoordinator(
        executor=executor,
        store=store,
        policy=policy(response_repair_limit=1),
        clock=AdvancingClock(),
    )

    created = coordinator.start(
        request(),
        answer_question=lambda _question: pytest.fail("unexpected question"),
    )

    assert created is not None
    assert len(executor.requests) == 2
    first = store.load_turn(request().run_id, 1)
    second = store.load_turn(request().run_id, 2)
    assert first.response_validation is not None
    assert first.response_validation.correction_paths == (
        "/proposal/product_definition/primary_workflow",
        "/proposal/product_definition/target_users",
    )
    assert all(
        "/proposal/decisions/" not in path
        for path in first.response_validation.correction_paths
    )
    assert any(
        "removed redundant direct-input decision DECISION_TARGET_USERS" in item
        for item in first.response_normalizations
    )
    assert second.parsed_response is not None
    assert second.semantic_correction_outcome is not None
    assert second.parsed_response.proposal is not None
    assert {item.id for item in second.parsed_response.proposal.decisions}.isdisjoint(
        {"DECISION_TARGET_USERS", "DECISION_WORKFLOW"}
    )


def test_question_requires_suggestions_and_preserves_custom_answers() -> None:
    question = question_response().question

    assert question is not None
    assert len(question.options) == 2
    assert question.allow_custom


def test_product_question_declares_one_atomic_dimension() -> None:
    question = product_intent_question_response().question

    assert question is not None
    assert question.product_definition_dimensions == (
        ProductDefinitionDimension.TARGET_USERS,
    )
    output: list[str] = []
    answer = planning._interactive_question_answerer(
        read=lambda _prompt: "2",
        write=output.append,
    )(question)
    assert answer is not None
    assert answer.selected_option_id == "researchers"
    assert answer.product_definition_values == (
        PlanningOptionValue(
            dimension=ProductDefinitionDimension.TARGET_USERS,
            value="Developers and researchers",
        ),
    )
    assert "Decision scope: target_users" in output
    assert any("not a universal SAT prerequisite" in line for line in output)
    assert not any(question.text in line for line in output)


def test_product_question_option_values_must_match_exact_dimension_scope() -> None:
    question = product_intent_question_response().question
    assert question is not None
    payload = question.model_dump(mode="json")
    for option in payload["options"]:
        option["product_definition_values"] = [
            {"dimension": "delivery_maturity", "value": "usable local product"}
        ]
    parsed = PlanningQuestion.model_validate(payload)

    with pytest.raises(PlanningError, match="option values must exactly match"):
        planning.validate_question_admission(parsed)


def test_product_question_shows_planner_prose_only_as_advisory_details() -> None:
    question = product_intent_question_response().question
    assert question is not None
    answers = iter(("d", "2"))
    output: list[str] = []

    answer = planning._interactive_question_answerer(
        read=lambda _prompt: next(answers),
        write=output.append,
    )(question)

    assert answer is not None
    details_index = output.index(
        "Planner-authored advisory details (cannot expand scope):"
    )
    assert not any(question.text in line for line in output[:details_index])
    assert any(question.text in line for line in output[details_index:])
    assert answer.selected_option_id == "researchers"


def test_product_question_renderer_cannot_widen_its_typed_scope() -> None:
    question = product_intent_question_response().question
    assert question is not None
    widened = question.model_copy(
        update={
            "text": (
                "Who will use this tool, and at what maturity should it be delivered?"
            ),
            "why": (
                "A target user is a prerequisite for every SAT product-depth contract."
            ),
            "options": tuple(
                option.model_copy(
                    update={
                        "label": option.label + " with releasable maturity",
                        "description": (
                            option.description
                            + " Deliver it as a releasable small product."
                        ),
                    }
                )
                for option in question.options
            ),
        }
    )
    output: list[str] = []

    answer = planning._interactive_question_answerer(
        read=lambda _prompt: "1",
        write=output.append,
    )(widened)

    assert answer is not None
    rendered = "\n".join(output)
    assert "Decision scope: target_users" in rendered
    assert "not a universal SAT prerequisite" in rendered
    assert "maturity" not in rendered.casefold()
    assert "prerequisite for every SAT" not in rendered
    assert answer.product_definition_values == (
        PlanningOptionValue(
            dimension=ProductDefinitionDimension.TARGET_USERS,
            value="Individual developer",
        ),
    )


def test_one_question_can_authorize_an_explicit_product_dimension_bundle(
    tmp_path: Path,
) -> None:
    question = product_intent_question_response().question
    assert question is not None
    bundled = question.model_copy(
        update={
            "product_definition_dimensions": (
                ProductDefinitionDimension.TARGET_USERS,
                ProductDefinitionDimension.PRIMARY_WORKFLOW,
            ),
            "options": tuple(
                option.model_copy(
                    update={
                        "product_definition_values": (
                            *option.product_definition_values,
                            PlanningOptionValue(
                                dimension=(ProductDefinitionDimension.PRIMARY_WORKFLOW),
                                value="Repeatedly scan local Markdown collections",
                            ),
                        )
                    }
                )
                for option in question.options
            ),
        }
    )
    coordinator = AdaptivePlanningCoordinator(
        executor=ScriptedAgentExecutor(
            [
                response(
                    PlanningModelResponse(
                        kind=PlanningResponseKind.QUESTION,
                        question=bundled,
                    )
                )
            ]
        ),
        store=PlanningStore(tmp_path / "planning"),
        policy=policy(response_repair_limit=0),
        clock=AdvancingClock(),
    )

    shown: list[PresentedPlanningQuestion] = []

    created = coordinator.start(
        request(),
        answer_question=lambda shown_question: shown.append(shown_question) or None,
    )

    assert created is None
    assert len(shown) == 1
    assert shown[0].model_dump(exclude={"admission"}) == bundled.model_dump()
    assert shown[0].admission.origin is PlanningQuestionOrigin.PLANNER_SUGGESTION
    assert shown[0].admission.controller_invariant_ids == ()
    current_turn = coordinator.store.load_turn(request().run_id, 1)
    current_payload = current_turn.model_dump(mode="json")
    assert current_payload["schema_version"] == planning.PLANNING_SCHEMA_VERSION
    assert current_payload["question_admission"]["origin"] == "planner_suggestion"
    schema_seventeen = deepcopy(current_payload)
    schema_seventeen["schema_version"] = 17
    assert (
        PlanningTurn.model_validate(schema_seventeen).model_dump(mode="json")
        == schema_seventeen
    )
    without_admission = dict(current_payload)
    without_admission.pop("question_admission")
    with pytest.raises(ValidationError, match="requires Controller admission"):
        PlanningTurn.model_validate(without_admission)
    legacy_payload = dict(without_admission)
    legacy_payload["schema_version"] = 16
    assert (
        PlanningTurn.model_validate(legacy_payload).model_dump(mode="json")
        == legacy_payload
    )


def test_bundled_product_answer_is_projected_without_model_paraphrase(
    tmp_path: Path,
) -> None:
    question = PlanningQuestion(
        id="product_intent",
        text="Who will use the tool, for what workflow, and at what maturity?",
        why="These coupled choices change product scope and delivery depth.",
        decision_category=PlanningDecisionCategory.PRODUCT_REQUIREMENT,
        decision_owner=PlanningDecisionAuthority.USER,
        missing_evidence=(
            "The request does not identify the audience, workflow, or maturity.",
        ),
        material_consequences=(
            "The answer changes requirements, acceptance, and delivery.",
        ),
        product_definition_dimensions=(
            ProductDefinitionDimension.TARGET_USERS,
            ProductDefinitionDimension.PRIMARY_WORKFLOW,
            ProductDefinitionDimension.DELIVERY_MATURITY,
        ),
        options=(
            PlanningOption(
                id="one_time",
                label="Personal local, one-time helper",
                description="A throwaway_prototype for one local scan.",
                product_definition_values=(
                    PlanningOptionValue(
                        dimension=ProductDefinitionDimension.TARGET_USERS,
                        value="Personal user",
                    ),
                    PlanningOptionValue(
                        dimension=ProductDefinitionDimension.PRIMARY_WORKFLOW,
                        value="Run one local Markdown link scan",
                    ),
                    PlanningOptionValue(
                        dimension=ProductDefinitionDimension.DELIVERY_MATURITY,
                        value=DeliveryMaturity.THROWAWAY_PROTOTYPE.value,
                    ),
                ),
            ),
            PlanningOption(
                id="reusable",
                label="Personal reusable local tool",
                description="A usable_local_product for repeated local scans.",
                product_definition_values=(
                    PlanningOptionValue(
                        dimension=ProductDefinitionDimension.TARGET_USERS,
                        value="Personal user",
                    ),
                    PlanningOptionValue(
                        dimension=ProductDefinitionDimension.PRIMARY_WORKFLOW,
                        value="Repeatedly check Markdown links in local directories",
                    ),
                    PlanningOptionValue(
                        dimension=ProductDefinitionDimension.DELIVERY_MATURITY,
                        value=DeliveryMaturity.USABLE_LOCAL_PRODUCT.value,
                    ),
                ),
            ),
        ),
    )
    body = proposal_body(question_id=question.id)
    definition = body.product_definition
    assert definition is not None
    decision_id = "DECISION_LINK_SCOPE_ANSWER"
    body = body.model_copy(
        update={
            "product_definition": definition.model_copy(
                update={
                    "target_users": definition.target_users.model_copy(
                        update={
                            "statement": "The requester",
                            "disposition": (
                                ProductDefinitionDisposition.RESOLVED_QUESTION
                            ),
                            "source": question.id,
                            "decision_ids": (decision_id,),
                        }
                    ),
                    "primary_workflow": definition.primary_workflow.model_copy(
                        update={
                            "statement": "Run repeated local link scans",
                            "disposition": (
                                ProductDefinitionDisposition.RESOLVED_QUESTION
                            ),
                            "source": question.id,
                            "decision_ids": (decision_id,),
                        }
                    ),
                    "delivery_maturity": definition.delivery_maturity.model_copy(
                        update={
                            "level": DeliveryMaturity.USABLE_LOCAL_PRODUCT,
                            "disposition": (
                                ProductDefinitionDisposition.RESOLVED_QUESTION
                            ),
                            "source": question.id,
                            "decision_ids": (decision_id,),
                        }
                    ),
                }
            )
        }
    )
    executor = ScriptedAgentExecutor(
        [
            response(
                PlanningModelResponse(
                    kind=PlanningResponseKind.QUESTION,
                    question=question,
                )
            ),
            response(
                PlanningModelResponse(
                    kind=PlanningResponseKind.PROPOSAL,
                    proposal=body,
                )
            ),
        ]
    )
    store = PlanningStore(tmp_path / "planning")
    coordinator = AdaptivePlanningCoordinator(
        executor=executor,
        store=store,
        policy=policy(response_repair_limit=0),
        clock=AdvancingClock(),
    )

    created = coordinator.start(
        request(),
        answer_question=planning._interactive_question_answerer(
            read=lambda _prompt: "2",
            write=lambda _line: None,
        ),
    )

    assert created is not None
    created_definition = created.body.product_definition
    assert created_definition is not None
    assert created_definition.target_users.statement == "Personal user"
    assert created_definition.primary_workflow.statement == (
        "Repeatedly check Markdown links in local directories"
    )
    assert created_definition.delivery_maturity.level is (
        DeliveryMaturity.USABLE_LOCAL_PRODUCT
    )
    turn = store.load_turn(request().run_id, 2)
    assert {
        item
        for item in turn.response_normalizations
        if "statement from exact question answer" in item
    } == {
        "compiled proposal.product_definition.target_users.statement from exact "
        "question answer",
        "compiled proposal.product_definition.primary_workflow.statement from exact "
        "question answer",
    }


def test_bundled_answer_with_ambiguous_maturity_returns_to_focused_dialogue(
    tmp_path: Path,
) -> None:
    question = PlanningQuestion(
        id="product_intent",
        text="Who will use the tool, for what workflow, and at what maturity?",
        why="These coupled choices change product scope and delivery depth.",
        decision_category=PlanningDecisionCategory.PRODUCT_REQUIREMENT,
        decision_owner=PlanningDecisionAuthority.USER,
        missing_evidence=(
            "The request does not identify the audience, workflow, or maturity.",
        ),
        material_consequences=(
            "The answer changes requirements, acceptance, and delivery.",
        ),
        product_definition_dimensions=(
            ProductDefinitionDimension.TARGET_USERS,
            ProductDefinitionDimension.PRIMARY_WORKFLOW,
            ProductDefinitionDimension.DELIVERY_MATURITY,
        ),
        options=(
            PlanningOption(
                id="one_time",
                label="Personal local, one-time helper",
                description="A throwaway_prototype for one local scan.",
                product_definition_values=(
                    PlanningOptionValue(
                        dimension=ProductDefinitionDimension.TARGET_USERS,
                        value="Personal user",
                    ),
                    PlanningOptionValue(
                        dimension=ProductDefinitionDimension.PRIMARY_WORKFLOW,
                        value="Run one local Markdown link scan",
                    ),
                    PlanningOptionValue(
                        dimension=ProductDefinitionDimension.DELIVERY_MATURITY,
                        value=DeliveryMaturity.THROWAWAY_PROTOTYPE.value,
                    ),
                ),
            ),
            PlanningOption(
                id="reusable",
                label="Personal reusable local tool",
                description="A usable_local_product for repeated local scans.",
                product_definition_values=(
                    PlanningOptionValue(
                        dimension=ProductDefinitionDimension.TARGET_USERS,
                        value="Personal user",
                    ),
                    PlanningOptionValue(
                        dimension=ProductDefinitionDimension.PRIMARY_WORKFLOW,
                        value="Repeatedly check Markdown links in local directories",
                    ),
                    PlanningOptionValue(
                        dimension=ProductDefinitionDimension.DELIVERY_MATURITY,
                        value=DeliveryMaturity.USABLE_LOCAL_PRODUCT.value,
                    ),
                ),
            ),
        ),
    )
    answer = "A developer will use it repeatedly to scan local directories."
    body = proposal_body(question_id=question.id)
    definition = body.product_definition
    assert definition is not None
    decision_id = "DECISION_LINK_SCOPE_ANSWER"
    body = body.model_copy(
        update={
            "product_definition": definition.model_copy(
                update={
                    "target_users": definition.target_users.model_copy(
                        update={
                            "statement": "A developer",
                            "disposition": (
                                ProductDefinitionDisposition.RESOLVED_QUESTION
                            ),
                            "source": question.id,
                            "decision_ids": (decision_id,),
                        }
                    ),
                    "primary_workflow": definition.primary_workflow.model_copy(
                        update={
                            "statement": "Scan local directories repeatedly",
                            "disposition": (
                                ProductDefinitionDisposition.RESOLVED_QUESTION
                            ),
                            "source": question.id,
                            "decision_ids": (decision_id,),
                        }
                    ),
                    "delivery_maturity": definition.delivery_maturity.model_copy(
                        update={
                            "level": DeliveryMaturity.USABLE_LOCAL_PRODUCT,
                            "disposition": (
                                ProductDefinitionDisposition.RESOLVED_QUESTION
                            ),
                            "source": question.id,
                            "decision_ids": (decision_id,),
                        }
                    ),
                }
            )
        }
    )
    followup = PlanningQuestion(
        id="delivery_maturity_followup",
        text="How mature should the delivered tool be?",
        why="The previous answer did not state a delivery maturity.",
        decision_category=PlanningDecisionCategory.PRODUCT_REQUIREMENT,
        decision_owner=PlanningDecisionAuthority.USER,
        missing_evidence=("The delivery maturity remains ambiguous.",),
        material_consequences=("Maturity changes packaging and verification.",),
        product_definition_dimensions=(ProductDefinitionDimension.DELIVERY_MATURITY,),
        options=(
            PlanningOption(
                id="throwaway",
                label="Throwaway prototype",
                description="Prove the workflow once with minimal polish.",
                product_definition_values=(
                    PlanningOptionValue(
                        dimension=ProductDefinitionDimension.DELIVERY_MATURITY,
                        value=DeliveryMaturity.THROWAWAY_PROTOTYPE.value,
                    ),
                ),
            ),
            PlanningOption(
                id="usable",
                label="Usable local product",
                description="Support repeated use with tests and docs.",
                product_definition_values=(
                    PlanningOptionValue(
                        dimension=ProductDefinitionDimension.DELIVERY_MATURITY,
                        value=DeliveryMaturity.USABLE_LOCAL_PRODUCT.value,
                    ),
                ),
            ),
        ),
    )
    executor = ScriptedAgentExecutor(
        [
            response(
                PlanningModelResponse(
                    kind=PlanningResponseKind.QUESTION,
                    question=question,
                )
            ),
            response(
                PlanningModelResponse(
                    kind=PlanningResponseKind.PROPOSAL,
                    proposal=body,
                )
            ),
            response(
                PlanningModelResponse(
                    kind=PlanningResponseKind.QUESTION,
                    question=followup,
                )
            ),
        ]
    )
    store = PlanningStore(tmp_path / "planning")
    coordinator = AdaptivePlanningCoordinator(
        executor=executor,
        store=store,
        policy=policy(response_repair_limit=0),
        clock=AdvancingClock(),
    )
    shown: list[str] = []

    def answer_question(shown_question: PlanningQuestion) -> str | None:
        shown.append(shown_question.id)
        return answer if shown_question.id == question.id else None

    created = coordinator.start(request(), answer_question=answer_question)

    assert created is None
    assert shown == [question.id, followup.id]
    invalid = store.load_turn(request().run_id, 2)
    assert invalid.response_validation is not None
    assert invalid.response_validation.failure_class is (
        ResponseFailureClass.MISSING_USER_DECISION
    )
    assert {issue.path for issue in invalid.response_validation.issues} == {
        "/proposal/product_definition/delivery_maturity"
    }
    assert invalid.response_validation.correction_paths == ()


def test_invalid_product_question_is_replaced_as_one_authority_unit(
    tmp_path: Path,
) -> None:
    initial = product_intent_question_response().model_dump(mode="json")
    initial_question = initial["question"]
    initial_question["text"] = (
        "Who will use the tool, and what repeated workflow should it support?"
    )
    initial_question["why"] = (
        "The audience and workflow both change the product and its interface."
    )
    initial_question["missing_evidence"] = [
        "The request identifies neither the audience nor the repeated workflow."
    ]
    initial_question["material_consequences"] = [
        "The answer changes audience, workflow, interface, and acceptance behavior."
    ]
    initial_question["product_definition_dimensions"] = [
        "target_users",
        "target_users",
    ]
    initial_question["options"] = [
        {
            "id": "developers_scan",
            "label": "Developers scanning notes",
            "description": "Developers repeatedly scan a notes directory.",
        },
        {
            "id": "researchers_export",
            "label": "Researchers exporting summaries",
            "description": "Researchers repeatedly export deterministic summaries.",
        },
    ]
    initial_question.pop("decision_owner")
    correction_base, _ = planning._normalize_planning_response_payload(initial)

    corrected = product_intent_question_response().question
    assert corrected is not None
    corrected_payload = corrected.model_dump(mode="json")
    corrected_payload.pop("decision_owner")
    executor = ScriptedAgentExecutor(
        [
            ScriptedAgentResponse(text="ignored", submission_payload=initial),
            ScriptedAgentResponse(
                text="ignored",
                submission_payload=json.loads(
                    correction_response(
                        correction_base,
                        {"/question": corrected_payload},
                    )
                ),
            ),
        ]
    )
    store = PlanningStore(tmp_path / "planning")
    coordinator = AdaptivePlanningCoordinator(
        executor=executor,
        store=store,
        policy=policy(response_repair_limit=1),
        clock=AdvancingClock(),
    )
    shown: list[PresentedPlanningQuestion] = []

    created = coordinator.start(
        request(),
        answer_question=lambda question: shown.append(question) or None,
    )

    assert created is None
    assert len(shown) == 1
    assert shown[0].model_dump(exclude={"admission"}) == corrected.model_dump()
    assert shown[0].admission.origin is PlanningQuestionOrigin.PLANNER_SUGGESTION
    first = store.load_turn(request().run_id, 1)
    assert first.response_validation is not None
    assert first.response_validation.correction_paths == ("/question",)
    correction_schema = executor.requests[1].submission_contract
    assert correction_schema is not None
    replacement = correction_schema.parameters_schema()["properties"]["replacements"]
    value_schema = replacement["items"]["oneOf"][0]["properties"]["replacement_value"]
    assert value_schema["type"] == "object"
    assert "text" in value_schema["required"]
    assert "product_definition_dimensions" in value_schema["required"]


def test_question_authority_field_typo_requires_whole_question_replacement(
    tmp_path: Path,
) -> None:
    initial = product_intent_question_response().model_dump(mode="json")
    initial_question = initial["question"]
    initial_question.pop("decision_owner")
    dimensions = initial_question.pop("product_definition_dimensions")
    initial_question["products_definition_dimensions"] = dimensions
    correction_base, _ = planning._normalize_planning_response_payload(initial)

    corrected = product_intent_question_response().question
    assert corrected is not None
    corrected_payload = corrected.model_dump(mode="json")
    corrected_payload.pop("decision_owner")
    executor = ScriptedAgentExecutor(
        [
            ScriptedAgentResponse(text="ignored", submission_payload=initial),
            ScriptedAgentResponse(
                text="ignored",
                submission_payload=json.loads(
                    correction_response(
                        correction_base,
                        {"/question": corrected_payload},
                    )
                ),
            ),
        ]
    )
    store = PlanningStore(tmp_path / "planning")
    coordinator = AdaptivePlanningCoordinator(
        executor=executor,
        store=store,
        policy=policy(response_repair_limit=1),
        clock=AdvancingClock(),
    )
    shown: list[PresentedPlanningQuestion] = []

    created = coordinator.start(
        request(),
        answer_question=lambda question: shown.append(question) or None,
    )

    assert created is None
    assert len(shown) == 1
    assert shown[0].model_dump(exclude={"admission"}) == corrected.model_dump()
    assert shown[0].admission.origin is PlanningQuestionOrigin.PLANNER_SUGGESTION
    first = store.load_turn(request().run_id, 1)
    assert first.parsed_response is None
    assert first.response_normalizations == (
        "compiled question.decision_owner from category product_requirement",
    )
    assert first.response_validation is not None
    assert first.response_validation.correction_paths == ("/question",)
    assert {issue.invariant_id for issue in first.response_validation.issues} == {
        "planning_current_question_wire_contract"
    }


def test_current_question_requires_explicit_option_values_on_wire() -> None:
    payload = json.loads(response(question_response()))
    question = payload["question"]
    assert isinstance(question, dict)
    question.pop("decision_owner")
    options = question["options"]
    assert isinstance(options, list)
    first_option = options[0]
    assert isinstance(first_option, dict)
    first_option.pop("product_definition_values")
    normalized, _ = planning._normalize_planning_response_payload(payload)

    with pytest.raises(
        planning._PlanningModelInvariantError,
        match=r"options\[0\]\.product_definition_values",
    ):
        planning._validate_current_planning_response_wire(
            normalized,
            response_schema=planning._planning_response_schema(),
        )


def test_legacy_question_without_current_dimension_field_remains_readable() -> None:
    payload = product_intent_question_response().question
    assert payload is not None
    legacy = payload.model_dump(mode="json")
    legacy.pop("product_definition_dimensions")

    parsed = PlanningQuestion.model_validate(legacy)

    assert parsed.product_definition_dimensions == ()


def test_product_dimensions_cannot_be_attached_to_non_product_questions(
    tmp_path: Path,
) -> None:
    question = product_intent_question_response().question
    assert question is not None
    invalid = question.model_copy(
        update={
            "decision_category": PlanningDecisionCategory.ACCEPTANCE_SCOPE,
            "decision_owner": PlanningDecisionAuthority.PLANNER_PROPOSAL,
        }
    )
    coordinator = AdaptivePlanningCoordinator(
        executor=ScriptedAgentExecutor(
            [
                response(
                    PlanningModelResponse(
                        kind=PlanningResponseKind.QUESTION,
                        question=invalid,
                    )
                )
            ]
        ),
        store=PlanningStore(tmp_path / "planning"),
        policy=policy(response_repair_limit=0),
        clock=AdvancingClock(),
    )

    with pytest.raises(PlanningError, match="product-definition clarification"):
        coordinator.start(
            request(),
            answer_question=lambda _question: pytest.fail(
                "misowned product question reached the user"
            ),
        )


def test_under_specified_request_requires_question_backed_core_dimensions(
    tmp_path: Path,
) -> None:
    source = "Build a CLI that summarizes a Markdown notes directory."
    definition = proposal_body().product_definition
    assert definition is not None
    invented = definition.model_copy(
        update={
            "target_users": definition.target_users.model_copy(
                update={
                    "statement": "Developers",
                    "disposition": ProductDefinitionDisposition.PLANNER_RECOMMENDATION,
                    "source": "planner",
                }
            ),
            "primary_workflow": definition.primary_workflow.model_copy(
                update={
                    "statement": "Repeatedly summarize notes",
                    "disposition": ProductDefinitionDisposition.PLANNER_RECOMMENDATION,
                    "source": "planner",
                }
            ),
            "delivery_maturity": definition.delivery_maturity.model_copy(
                update={
                    "disposition": ProductDefinitionDisposition.PLANNER_RECOMMENDATION,
                    "source": "planner",
                }
            ),
        }
    )
    executor = ScriptedAgentExecutor(
        [
            response(
                proposal_response(
                    proposal_body().model_copy(update={"product_definition": invented})
                )
            ),
            response(product_intent_question_response()),
        ]
    )
    store = PlanningStore(tmp_path / "planning")
    coordinator = AdaptivePlanningCoordinator(
        executor=executor,
        store=store,
        policy=policy(response_repair_limit=0),
        clock=AdvancingClock(),
    )

    seen_questions: list[PlanningQuestion] = []

    def cancel_after_question(question: PlanningQuestion) -> None:
        seen_questions.append(question)
        return None

    created = coordinator.start(
        request(source_request=source),
        answer_question=cancel_after_question,
    )

    assert created is None
    assert [item.product_definition_dimensions for item in seen_questions] == [
        (ProductDefinitionDimension.TARGET_USERS,)
    ]
    first = store.load_turn(request().run_id, 1)
    assert first.response_validation is not None
    assert first.response_validation.failure_class is (
        ResponseFailureClass.MISSING_USER_DECISION
    )
    assert first.response_validation.correction_paths == ()
    assert store.load_session(request().run_id).status is (
        PlanningSessionStatus.CANCELLED
    )


def test_question_backed_product_definition_reaches_confirmed_task_brief(
    tmp_path: Path,
) -> None:
    planning_request = request(
        source_request=("Build a usable local product that checks Markdown links.")
    )
    coordinator = AdaptivePlanningCoordinator(
        executor=ScriptedAgentExecutor(
            [
                response(product_intent_question_response()),
                response(proposal_response(resolved_product_body())),
            ]
        ),
        store=PlanningStore(tmp_path / "planning"),
        policy=policy(response_repair_limit=0),
        clock=AdvancingClock(),
    )

    created = coordinator.start(
        planning_request,
        answer_question=lambda _question: "Developers and researchers",
    )

    assert created is not None
    preview = preview_adaptive_proposal(
        planning_request,
        created,
        policy(),
        created_at=FIXED_TIME,
    )
    definition = preview.task_brief.product_definition
    assert definition is not None
    assert definition.delivery_maturity.level is DeliveryMaturity.USABLE_LOCAL_PRODUCT
    assert (
        definition.target_users.disposition
        is ProductDefinitionDisposition.RESOLVED_QUESTION
    )
    assert preview.implementation_plan.product_definition == definition


def test_question_backed_product_depth_discards_model_paraphrase(
    tmp_path: Path,
) -> None:
    store = PlanningStore(tmp_path / "planning")
    coordinator = AdaptivePlanningCoordinator(
        executor=ScriptedAgentExecutor(
            [
                response(product_intent_question_response()),
                response(proposal_response(resolved_product_body())),
            ]
        ),
        store=store,
        policy=policy(response_repair_limit=0),
        clock=AdvancingClock(),
    )

    created = coordinator.start(
        request(
            source_request=("Build a usable local product that checks Markdown links.")
        ),
        answer_question=lambda _question: "Researchers",
    )

    assert created is not None
    definition = created.body.product_definition
    assert definition is not None
    assert definition.target_users.statement == "Researchers"
    turn = store.load_turn(request().run_id, 2)
    assert (
        "compiled proposal.product_definition.target_users.statement from exact "
        "question answer"
    ) in turn.response_normalizations


def test_resolved_dimension_must_be_declared_by_its_question(tmp_path: Path) -> None:
    question = product_intent_question_response().question
    assert question is not None
    narrowed = question.model_copy(
        update={
            "product_definition_dimensions": (
                ProductDefinitionDimension.PRIMARY_WORKFLOW,
            ),
            "options": tuple(
                option.model_copy(
                    update={
                        "product_definition_values": (
                            PlanningOptionValue(
                                dimension=(ProductDefinitionDimension.PRIMARY_WORKFLOW),
                                value="Repeatedly summarize a Markdown directory",
                            ),
                        )
                    }
                )
                for option in question.options
            ),
        }
    )
    followup = question.model_copy(update={"id": "target_users_followup"})
    store = PlanningStore(tmp_path / "planning")
    coordinator = AdaptivePlanningCoordinator(
        executor=ScriptedAgentExecutor(
            [
                response(
                    PlanningModelResponse(
                        kind=PlanningResponseKind.QUESTION,
                        question=narrowed,
                    )
                ),
                response(proposal_response(resolved_product_body())),
                response(
                    PlanningModelResponse(
                        kind=PlanningResponseKind.QUESTION,
                        question=followup,
                    )
                ),
            ]
        ),
        store=store,
        policy=policy(response_repair_limit=0),
        clock=AdvancingClock(),
    )

    shown: list[str] = []

    def answer(shown_question: PlanningQuestion) -> str | None:
        shown.append(shown_question.id)
        return "Developers and researchers" if len(shown) == 1 else None

    created = coordinator.start(
        request(source_request="Build a CLI that summarizes a Markdown directory."),
        answer_question=answer,
    )

    assert created is None
    assert shown == [narrowed.id, followup.id]
    invalid = store.load_turn(request().run_id, 2)
    assert invalid.response_validation is not None
    assert invalid.response_validation.failure_class is (
        ResponseFailureClass.MISSING_USER_DECISION
    )
    assert invalid.response_validation.correction_paths == ()


def test_controller_rejects_questions_for_autonomous_decisions(
    tmp_path: Path,
) -> None:
    question = question_response().question
    assert question is not None
    invalid = question.model_copy(
        update={
            "decision_category": PlanningDecisionCategory.LOCAL_IMPLEMENTATION,
            "decision_owner": PlanningDecisionAuthority.USER,
        }
    )
    coordinator = AdaptivePlanningCoordinator(
        executor=ScriptedAgentExecutor(
            [
                response(
                    PlanningModelResponse(
                        kind=PlanningResponseKind.QUESTION,
                        question=invalid,
                    )
                )
            ]
        ),
        store=PlanningStore(tmp_path / "planning"),
        policy=policy(response_repair_limit=0),
        clock=AdvancingClock(),
    )

    with pytest.raises(PlanningError, match="cannot ask the user to decide"):
        coordinator.start(
            request(source_request=AMBIGUOUS_LINK_REQUEST),
            answer_question=lambda _question: pytest.fail(
                "inadmissible question reached the user"
            ),
        )


def test_answered_question_must_have_exact_decision_provenance(tmp_path: Path) -> None:
    coordinator = AdaptivePlanningCoordinator(
        executor=ScriptedAgentExecutor(
            [
                response(question_response()),
                response(proposal_response()),
            ]
        ),
        store=PlanningStore(tmp_path / "planning"),
        policy=policy(response_repair_limit=0),
        clock=AdvancingClock(),
    )

    with pytest.raises(PlanningError, match="question-decision provenance"):
        coordinator.start(
            request(source_request=AMBIGUOUS_LINK_REQUEST),
            answer_question=lambda _question: "Only local links.",
        )


def test_unknown_user_question_provenance_requests_bound_clarification(
    tmp_path: Path,
) -> None:
    """Replay the ca1ee02 existing-report authority failure through production APIs."""

    question_id = "existing_report_handling"
    decision = PlanningDecisionRecord(
        id="DECISION_EXISTING_REPORT_HANDLING",
        category=PlanningDecisionCategory.PRODUCT_REQUIREMENT,
        authority=PlanningDecisionAuthority.USER,
        provenance=PlanningDecisionProvenance(
            kind=PlanningDecisionProvenanceKind.RESOLVED_QUESTION,
            source=question_id,
        ),
        summary="Refuse to replace an existing report unless --force is supplied.",
        rationale="Overwriting an existing output is a material user-data choice.",
    )
    body = proposal_body().model_copy(
        update={"decisions": (*proposal_body().decisions, decision)}
    )
    recovery_question = PlanningQuestion(
        id=question_id,
        text="What should happen when the report path already exists?",
        why="The answer changes whether a normal rerun can replace prior output.",
        decision_category=PlanningDecisionCategory.PRODUCT_REQUIREMENT,
        decision_owner=PlanningDecisionAuthority.USER,
        missing_evidence=(
            "The request does not authorize replacing an existing report.",
        ),
        material_consequences=(
            "The choice changes default CLI behavior and failure-path tests.",
        ),
        options=(
            PlanningOption(
                id="overwrite",
                label="Overwrite",
                description="Replace the existing report by default.",
            ),
            PlanningOption(
                id="require_force",
                label="Require --force",
                description="Refuse replacement unless --force is supplied.",
            ),
        ),
    )
    executor = ScriptedAgentExecutor(
        [
            response(proposal_response(body)),
            response(
                PlanningModelResponse(
                    kind=PlanningResponseKind.QUESTION,
                    question=recovery_question,
                )
            ),
            response(proposal_response(body)),
        ]
    )
    store = PlanningStore(tmp_path / "planning")
    coordinator = AdaptivePlanningCoordinator(
        executor=executor,
        store=store,
        policy=policy(response_repair_limit=0),
        clock=AdvancingClock(),
    )
    shown: list[PresentedPlanningQuestion] = []
    clarification_output: list[str] = []
    interactive_answer = planning._interactive_question_answerer(
        read=lambda _prompt: "2",
        write=clarification_output.append,
    )

    created = coordinator.start(
        request(),
        answer_question=lambda question: (
            shown.append(question) or interactive_answer(question)
        ),
    )

    assert created is not None
    assert created.body == body
    assert len(shown) == 1
    assert shown[0].id == question_id
    assert shown[0].product_definition_dimensions == ()
    assert shown[0].admission.origin is PlanningQuestionOrigin.CONTROLLER_REQUIREMENT
    assert shown[0].admission.controller_decision_id == decision.id
    assert shown[0].admission.controller_invariant_ids == (
        "planning_question_decision_completeness",
    )
    assert (
        "Decision scope: product_requirement / DECISION_EXISTING_REPORT_HANDLING"
        in clarification_output
    )
    assert any(
        "Controller validation requires this user-owned decision" in line
        for line in clarification_output
    )
    assert not any("d. Show Planner wording" in line for line in clarification_output)

    invalid = store.load_turn(request().run_id, 1)
    assert invalid.response_validation is not None
    assert invalid.response_validation.failure_class is (
        ResponseFailureClass.MISSING_USER_DECISION
    )
    assert invalid.response_validation.correction_paths == ()
    assert {
        (subject.kind.value, subject.identifier)
        for subject in invalid.response_validation.issues[0].subjects
    } == {
        ("decision", decision.id),
        ("question", question_id),
    }

    recovery_contract = executor.requests[1].submission_contract
    assert recovery_contract is not None
    recovery_schema = recovery_contract.parameters_schema()
    question_definition = recovery_schema["$defs"]["PlanningQuestion"]
    assert question_definition["properties"]["id"] == {
        "const": question_id,
        "type": "string",
    }
    assert question_definition["properties"]["decision_category"] == {
        "const": "product_requirement",
        "type": "string",
    }
    product_dimensions = question_definition["properties"][
        "product_definition_dimensions"
    ]
    assert product_dimensions["maxItems"] == 1
    assert product_dimensions["uniqueItems"] is True
    assert product_dimensions["items"]["enum"] == [
        item.value for item in ProductDefinitionDimension
    ]
    option_definition = recovery_schema["$defs"]["PlanningOption"]
    option_values = option_definition["properties"]["product_definition_values"]
    assert option_values["items"] == {"$ref": "#/$defs/PlanningOptionValue"}
    assert option_values["maxItems"] == 1
    assert "minItems" not in option_values
    assert '"question_id": "existing_report_handling"' in executor.requests[1].prompt
    assert '"decision_id": "DECISION_EXISTING_REPORT_HANDLING"' in (
        executor.requests[1].prompt
    )
    recovery_turn = store.load_turn(request().run_id, 2)
    recovery_payload = recovery_turn.model_dump(mode="json")
    assert recovery_payload["schema_version"] == planning.PLANNING_SCHEMA_VERSION
    assert recovery_payload["question_admission"]["controller_decision_id"] == (
        decision.id
    )
    schema_seventeen = deepcopy(recovery_payload)
    schema_seventeen["schema_version"] = 17
    with pytest.raises(
        ValidationError,
        match="legacy Planning turns cannot contain Controller decision binding",
    ):
        PlanningTurn.model_validate(schema_seventeen)


@pytest.mark.parametrize(
    ("answer_inputs", "resolved_value"),
    (
        (
            ("1",),
            "Default to the current directory, group human-readable results, "
            "and offer machine-readable output with documented exit codes.",
        ),
        (
            (
                "c",
                "Require an explicit directory, emit grouped text by default, "
                "and provide an optional JSON report with documented exit codes.",
            ),
            "Require an explicit directory, emit grouped text by default, and "
            "provide an optional JSON report with documented exit codes.",
        ),
    ),
)
def test_controller_bound_product_decision_can_share_one_declared_dimension(
    tmp_path: Path,
    answer_inputs: tuple[str, ...],
    resolved_value: str,
) -> None:
    """Replay Journey 52 through the production question authority chain."""

    question_id = "q_usage_contract"
    decision_id = "DECISION_USABILITY"
    selected_value = (
        "Default to the current directory, group human-readable results, and offer "
        "machine-readable output with documented exit codes."
    )
    user_decision = PlanningDecisionRecord(
        id=decision_id,
        category=PlanningDecisionCategory.PRODUCT_REQUIREMENT,
        authority=PlanningDecisionAuthority.USER,
        provenance=PlanningDecisionProvenance(
            kind=PlanningDecisionProvenanceKind.RESOLVED_QUESTION,
            source=question_id,
        ),
        summary=resolved_value,
        rationale="The selected option defines the developer-facing CLI contract.",
    )
    base = proposal_body()
    invalid_body = base.model_copy(
        update={"decisions": (*base.decisions, user_decision)}
    )
    recovery_question = PlanningQuestion(
        id=question_id,
        text="How should the no-argument CLI and script output behave?",
        why="The answer changes the CLI, report, documentation, and tests.",
        decision_category=PlanningDecisionCategory.PRODUCT_REQUIREMENT,
        decision_owner=PlanningDecisionAuthority.USER,
        missing_evidence=("The usage contract has not been selected.",),
        material_consequences=(
            "The choice changes defaults, output modes, and exit-code tests.",
        ),
        product_definition_dimensions=(
            ProductDefinitionDimension.USABILITY_EXPECTATIONS,
        ),
        options=(
            PlanningOption(
                id="default_and_machine_readable",
                label="Safe default plus machine-readable option",
                description=selected_value,
                product_definition_values=(
                    PlanningOptionValue(
                        dimension=(ProductDefinitionDimension.USABILITY_EXPECTATIONS),
                        value=selected_value,
                    ),
                ),
            ),
            PlanningOption(
                id="explicit_root",
                label="Require an explicit directory",
                description="Require a path and print only grouped text.",
                product_definition_values=(
                    PlanningOptionValue(
                        dimension=(ProductDefinitionDimension.USABILITY_EXPECTATIONS),
                        value="Require a directory and print grouped text.",
                    ),
                ),
            ),
        ),
    )
    definition = base.product_definition
    assert definition is not None
    resolved_body = invalid_body.model_copy(
        update={
            "product_definition": definition.model_copy(
                update={
                    "usability_expectations": (
                        definition.usability_expectations.model_copy(
                            update={
                                "statement": resolved_value,
                                "disposition": (
                                    ProductDefinitionDisposition.RESOLVED_QUESTION
                                ),
                                "source": question_id,
                                "decision_ids": (decision_id,),
                            }
                        )
                    )
                }
            )
        }
    )
    executor = ScriptedAgentExecutor(
        [
            response(proposal_response(invalid_body)),
            response(
                PlanningModelResponse(
                    kind=PlanningResponseKind.QUESTION,
                    question=recovery_question,
                )
            ),
            response(proposal_response(resolved_body)),
        ]
    )
    store = PlanningStore(tmp_path / "planning")
    coordinator = AdaptivePlanningCoordinator(
        executor=executor,
        store=store,
        policy=policy(response_repair_limit=0),
        clock=AdvancingClock(),
    )
    shown: list[PresentedPlanningQuestion] = []
    output: list[str] = []
    answers = iter(answer_inputs)
    interactive_answer = planning._interactive_question_answerer(
        read=lambda _prompt: next(answers), write=output.append
    )

    created = coordinator.start(
        request(),
        answer_question=lambda question: (
            shown.append(question) or interactive_answer(question)
        ),
    )

    assert created is not None
    assert created.body == resolved_body
    assert len(shown) == 1
    admission = shown[0].admission
    assert admission.controller_decision_id == decision_id
    assert admission.product_definition_dimensions == (
        ProductDefinitionDimension.USABILITY_EXPECTATIONS,
    )
    assert (
        "Decision scope: product_requirement / DECISION_USABILITY; product "
        "definition: usability_expectations"
    ) in output
    recovery_turn = store.load_turn(request().run_id, 2)
    assert recovery_turn.schema_version == planning.PLANNING_SCHEMA_VERSION
    legacy_payload = recovery_turn.model_dump(mode="json")
    legacy_payload["schema_version"] = 19
    with pytest.raises(
        ValidationError,
        match="legacy Planning turns cannot combine Controller decision",
    ):
        PlanningTurn.model_validate(legacy_payload)


def test_controller_bound_non_product_decision_schema_denies_dimensions() -> None:
    schema = planning._planning_question_response_schema(
        planning._PlanningClarificationRecovery(
            decision_category=PlanningDecisionCategory.EXTERNAL_ACTION,
            invariant_ids=("planning_question_decision_completeness",),
            messages=("Publishing authority has not been supplied.",),
            question_id="q_publish",
            decision_id="DECISION_PUBLISH",
        )
    )

    question = schema["$defs"]["PlanningQuestion"]
    assert question["properties"]["product_definition_dimensions"] == {
        "maxItems": 0,
        "type": "array",
    }
    option = schema["$defs"]["PlanningOption"]
    assert option["properties"]["product_definition_values"]["maxItems"] == 0


def test_controller_bound_question_rejects_a_different_decision_identity() -> None:
    question_id = "existing_report_handling"
    original_id = "DECISION_EXISTING_REPORT_HANDLING"
    changed = PlanningDecisionRecord(
        id="DECISION_DIFFERENT_OUTPUT_POLICY",
        category=PlanningDecisionCategory.PRODUCT_REQUIREMENT,
        authority=PlanningDecisionAuthority.USER,
        provenance=PlanningDecisionProvenance(
            kind=PlanningDecisionProvenanceKind.RESOLVED_QUESTION,
            source=question_id,
        ),
        summary="Replace an existing report.",
        rationale="The question answer selected this output behavior.",
    )
    body = proposal_body().model_copy(
        update={"decisions": (*proposal_body().decisions, changed)}
    )

    with pytest.raises(
        PlanningError,
        match=f"must preserve Controller-bound ID {original_id}",
    ):
        planning.validate_planning_clarity(
            body,
            source_request=request().source_request,
            question_contracts={
                question_id: planning._PlanningQuestionContract(
                    category=PlanningDecisionCategory.PRODUCT_REQUIREMENT,
                    owner=PlanningDecisionAuthority.USER,
                    answer="Overwrite the report.",
                    required_decision_id=original_id,
                )
            },
        )


def test_user_question_recovery_interrupts_model_owned_correction(
    tmp_path: Path,
) -> None:
    question_id = "existing_report_handling"
    decision = PlanningDecisionRecord(
        id="DECISION_EXISTING_REPORT_HANDLING",
        category=PlanningDecisionCategory.PRODUCT_REQUIREMENT,
        authority=PlanningDecisionAuthority.USER,
        provenance=PlanningDecisionProvenance(
            kind=PlanningDecisionProvenanceKind.RESOLVED_QUESTION,
            source=question_id,
        ),
        summary="Require --force before replacing an existing report.",
        rationale="Existing-output behavior needs user authority.",
    )
    final_body = proposal_body().model_copy(
        update={"decisions": (*proposal_body().decisions, decision)}
    )
    invalid_body = final_body.model_copy(update={"non_goals": ()})
    initial_payload = json.loads(response(proposal_response(invalid_body)))
    correction_base, _ = planning._normalize_planning_response_payload(initial_payload)
    recovery_question = PlanningQuestion(
        id=question_id,
        text="What should happen when the output already exists?",
        why="The answer determines whether replacement needs explicit consent.",
        decision_category=PlanningDecisionCategory.PRODUCT_REQUIREMENT,
        decision_owner=PlanningDecisionAuthority.USER,
        missing_evidence=("No existing-output behavior was approved.",),
        material_consequences=("The choice changes destructive CLI behavior.",),
        options=(
            PlanningOption(
                id="overwrite",
                label="Overwrite",
                description="Replace it by default.",
            ),
            PlanningOption(
                id="require_force",
                label="Require --force",
                description="Refuse unless the user opts in.",
            ),
        ),
    )
    executor = ScriptedAgentExecutor(
        [
            ScriptedAgentResponse(text="ignored", submission_payload=initial_payload),
            ScriptedAgentResponse(
                text="ignored",
                submission_payload=json.loads(
                    correction_response(
                        correction_base,
                        {"/proposal/non_goals": list(final_body.non_goals)},
                    )
                ),
            ),
            response(
                PlanningModelResponse(
                    kind=PlanningResponseKind.QUESTION,
                    question=recovery_question,
                )
            ),
            response(proposal_response(final_body)),
        ]
    )
    store = PlanningStore(tmp_path / "planning")
    coordinator = AdaptivePlanningCoordinator(
        executor=executor,
        store=store,
        policy=policy(response_repair_limit=1),
        clock=AdvancingClock(),
    )

    created = coordinator.start(
        request(),
        answer_question=lambda _question: "Require --force",
    )

    assert created is not None
    assert len(executor.requests) == 4
    first = store.load_turn(request().run_id, 1)
    assert first.response_validation is not None
    assert first.response_validation.correction_paths == ("/proposal/non_goals",)
    second = store.load_turn(request().run_id, 2)
    assert second.semantic_correction_outcome is SemanticCorrectionOutcome.IMPROVED
    assert second.response_validation is not None
    assert second.response_validation.failure_class is (
        ResponseFailureClass.MISSING_USER_DECISION
    )
    assert second.response_validation.correction_paths == ()
    assert store.load_turn(request().run_id, 3).question_admission is not None


@pytest.mark.parametrize(
    ("invalid_body", "message"),
    (
        (
            proposal_body().model_copy(update={"non_goals": ()}),
            "must state at least one non-goal",
        ),
        (
            proposal_body().model_copy(update={"requirement_ids": ("REQ_SCAN",)}),
            "one stable ID for every description",
        ),
        (
            proposal_body().model_copy(
                update={
                    "acceptance_criteria": (
                        proposal_body()
                        .acceptance_criteria[0]
                        .model_copy(update={"verification_agent_ids": ()}),
                        proposal_body().acceptance_criteria[1],
                    )
                }
            ),
            "must name an independent verifier",
        ),
        (
            proposal_body().model_copy(
                update={
                    "decisions": (
                        *proposal_body().decisions,
                        PlanningDecisionRecord(
                            id="DECISION_SAFETY_POLICY",
                            category=PlanningDecisionCategory.SAFETY_INVARIANT,
                            authority=PlanningDecisionAuthority.CONTROLLER_POLICY,
                            summary="Let the Planner redefine the safety boundary.",
                            rationale="This is intentionally invalid.",
                        ),
                    )
                }
            ),
            "cannot claim controller-policy decision authority",
        ),
    ),
)
def test_clarity_gate_rejects_incomplete_or_misowned_proposals(
    invalid_body: PlanningProposalBody,
    message: str,
) -> None:
    with pytest.raises(PlanningError, match=message):
        preview_adaptive_proposal(
            request(),
            proposal(body=invalid_body),
            policy(),
            created_at=FIXED_TIME,
        )


def test_explicit_product_statement_cannot_expand_beyond_user_wording() -> None:
    body = proposal_body()
    definition = body.product_definition
    assert definition is not None
    expanded = definition.model_copy(
        update={
            "target_users": definition.target_users.model_copy(
                update={"statement": "Developers and enterprise operators"}
            )
        }
    )

    with pytest.raises(
        PlanningError, match="statement must preserve that exact wording"
    ):
        preview_adaptive_proposal(
            request(),
            proposal(body=body.model_copy(update={"product_definition": expanded})),
            policy(),
            created_at=FIXED_TIME,
        )


def test_explicit_maturity_accepts_an_unambiguous_natural_language_source() -> None:
    body = proposal_body()
    definition = body.product_definition
    assert definition is not None
    inferred_maturity = definition.model_copy(
        update={
            "delivery_maturity": definition.delivery_maturity.model_copy(
                update={"source": "reusable local tool"}
            )
        }
    )

    preview = preview_adaptive_proposal(
        request(
            source_request=(
                "For developers who will use it repeatedly, build a reusable "
                "local tool that checks Markdown links in files and fragments "
                "without fetching remote URLs."
            )
        ),
        proposal(
            body=body.model_copy(update={"product_definition": inferred_maturity})
        ),
        policy(),
        created_at=FIXED_TIME,
    )

    assert (
        preview.task_brief.product_definition.delivery_maturity.level
        is DeliveryMaturity.USABLE_LOCAL_PRODUCT
    )


def test_product_definition_may_reference_controller_profile_criteria() -> None:
    body = proposal_body()
    definition = body.product_definition
    assert definition is not None
    profile_criterion = AcceptanceCriterion(
        id="AC_PROFILE",
        description="The project satisfies the fixed execution contract.",
        verification="Run the profile gate.",
    )
    traced = definition.model_copy(
        update={
            "delivery_expectations": definition.delivery_expectations.model_copy(
                update={"criterion_ids": ("AC_PROFILE",)}
            )
        }
    )

    preview = preview_adaptive_proposal(
        request(),
        proposal(body=body.model_copy(update={"product_definition": traced})),
        policy(profile_acceptance_criteria=(profile_criterion,)),
        created_at=FIXED_TIME,
    )

    assert "AC_PROFILE" in {
        criterion.id for criterion in preview.task_brief.acceptance_criteria
    }


def test_product_definition_references_must_resolve_to_the_proposal() -> None:
    body = proposal_body()
    definition = body.product_definition
    assert definition is not None
    invalid = definition.model_copy(
        update={
            "usability_expectations": definition.usability_expectations.model_copy(
                update={"criterion_ids": ("AC_INVENTED",)}
            )
        }
    )

    with pytest.raises(PlanningError, match="unknown downstream criteria"):
        preview_adaptive_proposal(
            request(),
            proposal(body=body.model_copy(update={"product_definition": invalid})),
            policy(),
            created_at=FIXED_TIME,
        )


def test_product_definition_correction_binds_immutable_reference_vocabularies(
    tmp_path: Path,
) -> None:
    initial_payload = json.loads(response(proposal_response()))
    for dimension in ("target_users", "primary_workflow"):
        initial_payload["proposal"]["product_definition"][dimension][
            "requirement_ids"
        ].append("REQ_GHOST")
    correction_base, _ = planning._normalize_planning_response_payload(
        initial_payload,
        user_inputs=(request().source_request,),
    )
    corrected_dimensions = {}
    for dimension in ("target_users", "primary_workflow"):
        corrected = deepcopy(
            correction_base["proposal"]["product_definition"][dimension]
        )
        corrected["requirement_ids"].remove("REQ_GHOST")
        corrected_dimensions[f"/proposal/product_definition/{dimension}"] = corrected
    profile_criterion = AcceptanceCriterion(
        id="AC_PROFILE",
        description="The project satisfies the fixed execution contract.",
        verification="Run the profile gate.",
    )
    executor = ScriptedAgentExecutor(
        [
            json.dumps(initial_payload),
            correction_response(correction_base, corrected_dimensions),
        ]
    )
    store = PlanningStore(tmp_path / "planning")
    configured = policy(
        response_repair_limit=1,
        profile_acceptance_criteria=(profile_criterion,),
    )
    coordinator = AdaptivePlanningCoordinator(
        executor=executor,
        store=store,
        policy=configured,
        clock=AdvancingClock(),
    )

    created = coordinator.start(
        request(),
        answer_question=lambda _question: pytest.fail("unexpected question"),
    )

    assert created is not None
    rejected = store.load_turn(request().run_id, 1)
    assert rejected.response_validation is not None
    assert rejected.response_validation.correction_paths == (
        "/proposal/product_definition/primary_workflow",
        "/proposal/product_definition/target_users",
    )
    assert {issue.invariant_id for issue in rejected.response_validation.issues} == {
        "planning_product_definition_reference"
    }
    contract = executor.requests[1].submission_contract
    assert contract is not None
    variants = contract.parameters_schema()["properties"]["replacements"]["items"][
        "oneOf"
    ]
    expected_requirements = correction_base["proposal"]["requirement_ids"]
    expected_criteria = [
        *(
            criterion["id"]
            for criterion in correction_base["proposal"]["acceptance_criteria"]
        ),
        profile_criterion.id,
    ]
    expected_decisions = [
        decision["id"] for decision in correction_base["proposal"]["decisions"]
    ]
    assert len(variants) == 2
    for variant in variants:
        properties = variant["properties"]["replacement_value"]["properties"]
        assert properties["requirement_ids"]["items"]["enum"] == (expected_requirements)
        assert "REQ_GHOST" not in properties["requirement_ids"]["items"]["enum"]
        assert properties["criterion_ids"]["items"]["enum"] == expected_criteria
        assert properties["decision_ids"]["items"]["enum"] == expected_decisions
    assert store.load_turn(request().run_id, 2).semantic_correction_outcome == (
        "accepted"
    )


def test_direct_user_decision_rejects_unattributable_input_with_zero_repair_budget(
    tmp_path: Path,
) -> None:
    body = proposal_body()
    direct = PlanningDecisionRecord(
        id="DECISION_NO_NETWORK",
        category=PlanningDecisionCategory.PRIVACY_OR_DATA,
        authority=PlanningDecisionAuthority.USER,
        provenance=PlanningDecisionProvenance(
            kind=PlanningDecisionProvenanceKind.EXPLICIT_INPUT,
            source="without fetching remote URLs",
        ),
        summary="without fetching remote URLs",
        rationale="The request explicitly excludes remote fetches.",
    )
    accepted = body.model_copy(update={"decisions": (*body.decisions, direct)})

    preview_adaptive_proposal(
        request(),
        proposal(body=accepted),
        policy(),
        created_at=FIXED_TIME,
    )

    invented = direct.model_copy(
        update={
            "provenance": direct.provenance.model_copy(
                update={"source": "the user approved uploading every file"}
            )
        }
    )
    invalid = proposal_response(
        body.model_copy(update={"decisions": (*body.decisions, invented)})
    ).model_dump(mode="json")
    executor = ScriptedAgentExecutor(
        [ScriptedAgentResponse(text="ignored", submission_payload=invalid)]
    )
    store = PlanningStore(tmp_path / "planning")
    coordinator = AdaptivePlanningCoordinator(
        executor=executor,
        store=store,
        policy=policy(response_repair_limit=0),
        clock=AdvancingClock(),
    )

    with pytest.raises(PlanningError, match="not present in the Planning request"):
        coordinator.start(
            request(),
            answer_question=lambda _question: pytest.fail("unexpected question"),
        )

    assert len(executor.requests) == 1
    turn = store.load_turn(request().run_id, 1)
    assert turn.response_validation is not None
    assert turn.response_validation.failure_class is (
        ResponseFailureClass.SEMANTIC_CONTEXT
    )
    assert turn.response_validation.correction_paths == (
        f"/proposal/decisions/{len(body.decisions)}/provenance/source",
    )
    assert {item.authority for item in turn.response_validation.issues} == {
        ResponseIssueAuthority.MODEL
    }


@pytest.mark.parametrize("correct_quote", [True, False])
def test_direct_decision_quote_correction_preserves_user_authority(
    tmp_path: Path, correct_quote: bool
) -> None:
    from test_submission_bridge import capture_controller_correction

    captures = []
    captured_submissions = []

    class CapturedExecutor(ScriptedAgentExecutor):
        def execute(self, execution_request, *, activity_handler=None):
            result = super().execute(
                execution_request, activity_handler=activity_handler
            )
            assert result.semantic_submission is not None
            captured, status, evidence = capture_controller_correction(
                tmp_path / f"planning-capture-{len(captures)}",
                execution_request,
                result.semantic_submission.payload,
            )
            captures.append(evidence)
            captured_submissions.append(status)
            return result.model_copy(
                update={
                    "semantic_submission": captured,
                    "submission_evidence": status,
                    "telemetry": result.telemetry.model_copy(
                        update={
                            "tool_calls": evidence.tool_calls,
                            "session_transcript_sha256": evidence.transcript_sha256,
                            "session_record_count": evidence.record_count,
                            "session_id": "controller-bridge",
                        }
                    ),
                }
            )

    payload = proposal_response().model_dump(mode="json")
    index = len(payload["proposal"]["decisions"])
    payload["proposal"]["decisions"].append(
        {
            "id": "DECISION_NO_NETWORK",
            "category": "privacy_or_data",
            "authority": "user",
            "provenance": {"kind": "explicit_input", "source": "scope"},
            "summary": "scope",
            "rationale": "Preserve the requested no-fetch boundary.",
        }
    )
    path = f"/proposal/decisions/{index}/provenance/source"
    replacement = "without fetching remote URLs" if correct_quote else "scope"
    executor = CapturedExecutor(
        [
            ScriptedAgentResponse(text="ignored", submission_payload=payload),
            ScriptedAgentResponse(
                text="ignored",
                submission_payload=json.loads(
                    correction_response(payload, {path: replacement})
                ),
            ),
        ]
    )
    store = PlanningStore(tmp_path / "planning")
    coordinator = AdaptivePlanningCoordinator(
        executor=executor, store=store, policy=policy(), clock=AdvancingClock()
    )
    if correct_quote:
        created = coordinator.start(
            request(), answer_question=lambda _: pytest.fail("unexpected question")
        )
        assert created is not None
    else:
        with pytest.raises(PlanningError, match="not present in the Planning request"):
            coordinator.start(
                request(), answer_question=lambda _: pytest.fail("unexpected question")
            )
    assert len(executor.requests) == 2
    first, second = (store.load_turn(request().run_id, n) for n in (1, 2))
    assert len(captures) == 2
    for turn, capture, submission in zip(
        (first, second), captures, captured_submissions, strict=True
    ):
        assert turn.submission_evidence == submission
        assert len(capture.tool_calls) == 1
    assert first.submission_payload == payload
    assert first.response_validation.correction_paths == (path,)
    assert second.semantic_correction_request.target_paths == (path,)
    if correct_quote:
        repaired = second.parsed_response.proposal.decisions[-1]
        assert repaired.category is PlanningDecisionCategory.PRIVACY_OR_DATA
        assert repaired.authority is PlanningDecisionAuthority.USER
        assert repaired.provenance.source == replacement
        assert repaired.summary == replacement
    else:
        assert second.parsed_response is None


def test_quote_diagnostics_collect_independent_sources_without_widening_slots() -> None:
    body = proposal_body()
    extra = tuple(
        PlanningDecisionRecord(
            id=f"DECISION_QUOTE_{index}",
            category=PlanningDecisionCategory.PRIVACY_OR_DATA,
            authority=PlanningDecisionAuthority.USER,
            provenance=PlanningDecisionProvenance(
                kind=PlanningDecisionProvenanceKind.EXPLICIT_INPUT, source="scope"
            ),
            summary="scope",
            rationale="Preserve the real user restriction.",
        )
        for index in range(2)
    )
    with pytest.raises(planning._PlanningContextInvariantsError) as caught:
        planning._validate_decision_provenance(
            body.model_copy(update={"decisions": (*body.decisions, *extra)}),
            normalized_inputs=(request().source_request.casefold(),),
            require_current_provenance=True,
        )
    assert tuple(item.paths for item in caught.value.invariants) == tuple(
        (f"/proposal/decisions/{len(body.decisions) + index}/provenance/source",)
        for index in range(2)
    )


@pytest.mark.parametrize("missing_kind", ["input", "provenance"])
def test_genuinely_missing_user_source_remains_user_owned(missing_kind: str) -> None:
    decision = PlanningDecisionRecord(
        id="DECISION_PERMISSION",
        category=PlanningDecisionCategory.EXTERNAL_ACTION,
        authority=PlanningDecisionAuthority.USER,
        provenance=(
            None
            if missing_kind == "provenance"
            else PlanningDecisionProvenance(
                kind=PlanningDecisionProvenanceKind.EXPLICIT_INPUT,
                source="Publish the project",
            )
        ),
        summary="Publish the project",
        rationale="This needs actual user authorization.",
    )
    with pytest.raises(planning._PlanningContextInvariantError) as caught:
        planning._validate_decision_provenance(
            proposal_body().model_copy(update={"decisions": (decision,)}),
            normalized_inputs=(),
            require_current_provenance=True,
        )
    assert caught.value.authority is ResponseIssueAuthority.USER
    assert caught.value.failure_class is ResponseFailureClass.MISSING_USER_DECISION


def test_product_definition_decision_links_are_disposition_specific() -> None:
    body = proposal_body()
    definition = body.product_definition
    assert definition is not None

    unrelated_explicit = definition.model_copy(
        update={
            "target_users": definition.target_users.model_copy(
                update={"decision_ids": ("DECISION_MODEL_ROUTE",)}
            )
        }
    )
    with pytest.raises(PlanningError, match="cannot cite a separate decision"):
        preview_adaptive_proposal(
            request(),
            proposal(
                body=body.model_copy(update={"product_definition": unrelated_explicit})
            ),
            policy(),
            created_at=FIXED_TIME,
        )

    mixed_recommendation = definition.model_copy(
        update={
            "usability_expectations": definition.usability_expectations.model_copy(
                update={
                    "decision_ids": (
                        "DECISION_ACCEPTANCE",
                        "DECISION_MODEL_ROUTE",
                    )
                }
            )
        }
    )
    with pytest.raises(PlanningError, match="only its corresponding Planner"):
        preview_adaptive_proposal(
            request(),
            proposal(
                body=body.model_copy(
                    update={"product_definition": mixed_recommendation}
                )
            ),
            policy(),
            created_at=FIXED_TIME,
        )

    resolved = resolved_product_body()
    resolved_definition = resolved.product_definition
    assert resolved_definition is not None
    extra_question_link = resolved_definition.model_copy(
        update={
            "target_users": resolved_definition.target_users.model_copy(
                update={
                    "decision_ids": (
                        "DECISION_LINK_SCOPE_ANSWER",
                        "DECISION_MODEL_ROUTE",
                    )
                }
            )
        }
    )
    with pytest.raises(PlanningError, match="only its resolved question decision"):
        preview_adaptive_proposal(
            request(source_request=AMBIGUOUS_LINK_REQUEST),
            proposal(
                body=resolved.model_copy(
                    update={"product_definition": extra_question_link}
                )
            ),
            policy(),
            created_at=FIXED_TIME,
        )


def test_schema_v7_decisions_remain_readable_without_canonical_rewrite() -> None:
    payload = proposal().model_dump(mode="json")
    payload["schema_version"] = 7
    body = payload["body"]
    assert isinstance(body, dict)
    strip_v8_decision_fields(body)
    body["product_definition"]["delivery_maturity"]["decision_ids"] = [
        "DECISION_DELIVERY"
    ]
    legacy = PlanningProposal.model_validate(payload)

    preview = preview_adaptive_proposal(
        request(),
        legacy,
        policy(),
        created_at=FIXED_TIME,
    )

    assert preview.task_brief.product_definition is not None
    serialized = legacy.model_dump(mode="json")
    assert serialized["schema_version"] == 7
    assert all(
        "provenance" not in decision for decision in serialized["body"]["decisions"]
    )
    assert serialized["body"]["product_definition"]["delivery_maturity"][
        "decision_ids"
    ] == ["DECISION_DELIVERY"]


def test_schema_v7_not_material_trace_remains_canonically_readable() -> None:
    payload = proposal().model_dump(mode="json")
    payload["schema_version"] = 7
    body = payload["body"]
    assert isinstance(body, dict)
    strip_v8_decision_fields(body)
    target_users = body["product_definition"]["target_users"]
    target_users.update(
        {
            "statement": "No persistent target user is material.",
            "disposition": "not_material",
            "source": "planner",
            "rationale": "This legacy prototype treated audience as immaterial.",
        }
    )
    expected = json.loads(json.dumps(payload))

    loaded = PlanningProposal.model_validate(payload)

    assert loaded.schema_version == 7
    assert loaded.model_dump(mode="json") == expected
    assert loaded.body.product_definition is not None
    assert loaded.body.product_definition.target_users.requirement_ids == ("REQ_SCAN",)


def test_explicit_throwaway_prototype_can_form_a_lean_plan_without_questions(
    tmp_path: Path,
) -> None:
    source = (
        "Build a throwaway prototype that counts Markdown links for one experiment."
    )
    body = proposal_body()
    definition = body.product_definition
    assert definition is not None
    prototype = definition.model_copy(
        update={
            "target_users": definition.target_users.model_copy(
                update={
                    "statement": "No persistent target user is material.",
                    "disposition": ProductDefinitionDisposition.NOT_MATERIAL,
                    "source": "planner",
                    "requirement_ids": (),
                    "criterion_ids": (),
                    "decision_ids": (),
                }
            ),
            "primary_workflow": definition.primary_workflow.model_copy(
                update={
                    "statement": "counts Markdown links for one experiment",
                    "disposition": ProductDefinitionDisposition.EXPLICIT_INPUT,
                    "source": "counts Markdown links for one experiment",
                    "decision_ids": (),
                }
            ),
            "delivery_maturity": definition.delivery_maturity.model_copy(
                update={
                    "level": DeliveryMaturity.THROWAWAY_PROTOTYPE,
                    "disposition": ProductDefinitionDisposition.EXPLICIT_INPUT,
                    "source": "throwaway prototype",
                }
            ),
        }
    )
    prototype_body = body.model_copy(update={"product_definition": prototype})
    coordinator = AdaptivePlanningCoordinator(
        executor=ScriptedAgentExecutor([response(proposal_response(prototype_body))]),
        store=PlanningStore(tmp_path / "planning"),
        policy=policy(response_repair_limit=0),
        clock=AdvancingClock(),
    )

    created = coordinator.start(
        request(source_request=source),
        answer_question=lambda _question: pytest.fail(
            "an explicit prototype should not trigger a fixed questionnaire"
        ),
    )

    assert created is not None
    assert created.body.product_definition == prototype
    assert not created.body.product_definition.target_users.requirement_ids
    assert created.body.product_definition.primary_workflow.requirement_ids


def test_throwaway_workflow_is_material_in_current_but_readable_in_v8() -> None:
    source = "For developers, build a throwaway prototype that checks Markdown links."
    payload = proposal().model_dump(mode="json")
    definition = payload["body"]["product_definition"]
    definition["delivery_maturity"].update(
        level="throwaway_prototype", source="throwaway prototype"
    )
    definition["primary_workflow"].update(
        statement="Check Markdown links once.",
        disposition="not_material",
        source="planner",
        requirement_ids=[],
        criterion_ids=[],
        decision_ids=[],
    )
    current = PlanningProposal.model_validate(payload)
    with pytest.raises(PlanningError, match="primary workflow is always material"):
        preview_adaptive_proposal(
            request(source_request=source), current, policy(), created_at=FIXED_TIME
        )

    payload["schema_version"] = 8
    legacy = PlanningProposal.model_validate(payload)
    assert legacy.model_dump(mode="json") == payload
    preview_adaptive_proposal(
        request(source_request=source), legacy, policy(), created_at=FIXED_TIME
    )


def test_current_workflow_schema_excludes_immaterial_or_planner_owned_choices() -> None:
    schema = planning._planning_response_schema()
    definitions = schema["$defs"]
    reference = definitions["ProductDefinition"]["properties"]["primary_workflow"][
        "$ref"
    ]
    workflow = definitions[reference.rsplit("/", 1)[1]]
    assert workflow["properties"]["disposition"]["enum"] == [
        "explicit_input",
        "resolved_question",
    ]
    assert workflow["properties"]["requirement_ids"]["minItems"] == 1
    assert "requirement_ids" in workflow["required"]


def test_workflow_materiality_repairs_one_atomic_slot_without_erasing_trace(
    tmp_path: Path,
) -> None:
    valid = proposal_response().model_dump(mode="json")
    invalid = json.loads(json.dumps(valid))
    workflow = invalid["proposal"]["product_definition"]["primary_workflow"]
    workflow.update(disposition="not_material", source="planner")
    normalized, changes = planning._normalize_planning_response_payload(invalid)
    assert normalized["proposal"]["product_definition"]["primary_workflow"] == workflow
    assert not any("primary_workflow" in change for change in changes)
    path = "/proposal/product_definition/primary_workflow"
    executor = ScriptedAgentExecutor(
        [
            json.dumps(invalid),
            correction_response(
                invalid,
                {path: valid["proposal"]["product_definition"]["primary_workflow"]},
            ),
        ]
    )
    store = PlanningStore(tmp_path / "planning")
    coordinator = AdaptivePlanningCoordinator(
        executor=executor,
        store=store,
        policy=policy(response_repair_limit=None),
        clock=AdvancingClock(),
    )
    created = coordinator.start(
        request(),
        answer_question=lambda _: pytest.fail("explicit workflow needs no question"),
    )
    assert created is not None
    assert created.body == proposal_body()
    first = store.load_turn(request().run_id, 1)
    assert first.response_validation is not None
    assert first.response_validation.correction_paths == (path,)
    assert (
        first.response_validation.issues[0].invariant_id
        == "planning_product_workflow_required"
    )
    assert (
        store.load_turn(request().run_id, 2).semantic_correction_outcome == "accepted"
    )
    correction = executor.requests[1].prompt.rsplit(
        "TARGETED_SEMANTIC_CORRECTION_SLOTS_V3", 1
    )[1]
    schema_text = correction.split("CORRECTION_SCHEMA_JSON\n", 1)[1].split(
        "\nCall `sat_submit_artifact`", 1
    )[0]
    replacement = json.loads(schema_text)["properties"]["replacements"]["items"][
        "oneOf"
    ][0]["properties"]["replacement_value"]
    assert replacement["properties"]["disposition"]["enum"] == [
        "explicit_input",
        "resolved_question",
    ]
    assert replacement["properties"]["requirement_ids"]["minItems"] == 1


def test_material_product_dimension_requires_a_downstream_effect() -> None:
    with pytest.raises(
        ValidationError,
        match="each material product-definition dimension requires",
    ):
        ProductDefinitionStatement(
            statement="developers",
            disposition=ProductDefinitionDisposition.EXPLICIT_INPUT,
            source="developers",
            rationale="The request explicitly names the intended users.",
        )

    not_material = ProductDefinitionStatement(
        statement="No persistent target user is material.",
        disposition=ProductDefinitionDisposition.NOT_MATERIAL,
        source="planner",
        rationale="The approved throwaway result is not shaped by an audience.",
    )

    assert not not_material.requirement_ids
    assert not not_material.criterion_ids
    assert not not_material.decision_ids


def test_current_not_material_dimension_cannot_claim_downstream_effects() -> None:
    source = "Build a throwaway prototype that counts Markdown links."
    body = proposal_body()
    definition = body.product_definition
    assert definition is not None
    invalid = definition.model_copy(
        update={
            "target_users": definition.target_users.model_copy(
                update={
                    "statement": "No persistent target user is material.",
                    "disposition": ProductDefinitionDisposition.NOT_MATERIAL,
                    "source": "planner",
                }
            ),
            "delivery_maturity": definition.delivery_maturity.model_copy(
                update={
                    "level": DeliveryMaturity.THROWAWAY_PROTOTYPE,
                    "source": "throwaway prototype",
                }
            ),
        }
    )

    with pytest.raises(PlanningError, match="cannot claim a downstream"):
        preview_adaptive_proposal(
            request(source_request=source),
            proposal(body=body.model_copy(update={"product_definition": invalid})),
            policy(),
            created_at=FIXED_TIME,
        )


def test_natural_language_revision_can_make_throwaway_audience_not_material(
    tmp_path: Path,
) -> None:
    source = (
        "Build a one-time throwaway Python command-line tool that scans a "
        "directory of Markdown notes."
    )
    body = proposal_body()
    definition = body.product_definition
    assert definition is not None
    prototype = definition.model_copy(
        update={
            "target_users": definition.target_users.model_copy(
                update={
                    "statement": "Build a one-time throwaway Python command-line tool",
                    "source": "Build a one-time throwaway Python command-line tool",
                }
            ),
            "primary_workflow": definition.primary_workflow.model_copy(
                update={
                    "statement": "scans a directory of Markdown notes",
                    "source": "scans a directory of Markdown notes",
                }
            ),
            "delivery_maturity": definition.delivery_maturity.model_copy(
                update={
                    "level": DeliveryMaturity.THROWAWAY_PROTOTYPE,
                    "source": "one-time throwaway",
                }
            ),
        }
    )
    initial_body = body.model_copy(update={"product_definition": prototype})
    revised_target = prototype.target_users.model_copy(
        update={
            "statement": "Target users do not shape this throwaway prototype.",
            "disposition": ProductDefinitionDisposition.NOT_MATERIAL,
            "source": "planner",
            "requirement_ids": (),
            "criterion_ids": (),
            "decision_ids": (),
        }
    )
    revised_body = initial_body.model_copy(
        update={
            "product_definition": prototype.model_copy(
                update={"target_users": revised_target}
            )
        }
    )
    executor = ScriptedAgentExecutor(
        [
            response(proposal_response(initial_body)),
            response(proposal_response(revised_body)),
        ]
    )
    coordinator = AdaptivePlanningCoordinator(
        executor=executor,
        store=PlanningStore(tmp_path / "planning"),
        policy=policy(response_repair_limit=1),
        clock=AdvancingClock(),
    )
    planning_request = request(source_request=source)
    first = coordinator.start(
        planning_request,
        answer_question=lambda _question: pytest.fail("unexpected question"),
    )
    assert first is not None

    revised = coordinator.revise(
        planning_request,
        first,
        (
            "Target users are not material for this explicitly approved one-time "
            "throwaway prototype."
        ),
        answer_question=lambda _question: pytest.fail("unexpected question"),
    )

    assert revised is not None
    assert len(executor.requests) == 2
    assert revised.body.product_definition is not None
    assert (
        revised.body.product_definition.target_users.disposition
        is ProductDefinitionDisposition.NOT_MATERIAL
    )
    assert not revised.body.product_definition.target_users.requirement_ids


def test_natural_language_revision_revalidates_the_complete_product_definition(
    tmp_path: Path,
) -> None:
    invalid_revision = proposal_body(title="Revised Link Checker").model_copy(
        update={"product_definition": None}
    )
    coordinator = AdaptivePlanningCoordinator(
        executor=ScriptedAgentExecutor(
            [
                response(proposal_response()),
                response(proposal_response(invalid_revision)),
            ]
        ),
        store=PlanningStore(tmp_path / "planning"),
        policy=policy(response_repair_limit=0),
        clock=AdvancingClock(),
    )
    planning_request = request()
    first = coordinator.start(
        planning_request,
        answer_question=lambda _question: pytest.fail("unexpected question"),
    )
    assert first is not None

    with pytest.raises(PlanningError, match="approved product definition"):
        coordinator.revise(
            planning_request,
            first,
            "Make the delivery a releasable small product.",
            answer_question=lambda _question: pytest.fail("unexpected question"),
        )

    session = coordinator.store.load_session(planning_request.run_id)
    assert session.status is PlanningSessionStatus.PROPOSED
    assert session.latest_proposal_revision == first.revision
    approved = coordinator.approve(planning_request, first)
    assert approved.approval.revision == first.revision


def test_natural_language_revision_can_supply_new_explicit_product_depth(
    tmp_path: Path,
) -> None:
    body = proposal_body(title="Releasable Link Checker")
    definition = body.product_definition
    assert definition is not None
    revised_definition = definition.model_copy(
        update={
            "delivery_maturity": definition.delivery_maturity.model_copy(
                update={
                    "level": DeliveryMaturity.RELEASABLE_SMALL_PRODUCT,
                    "disposition": ProductDefinitionDisposition.EXPLICIT_INPUT,
                    "source": "releasable small product",
                }
            )
        }
    )
    coordinator = AdaptivePlanningCoordinator(
        executor=ScriptedAgentExecutor(
            [
                response(proposal_response()),
                response(
                    proposal_response(
                        body.model_copy(
                            update={"product_definition": revised_definition}
                        )
                    )
                ),
            ]
        ),
        store=PlanningStore(tmp_path / "planning"),
        policy=policy(response_repair_limit=0),
        clock=AdvancingClock(),
    )
    planning_request = request()
    first = coordinator.start(
        planning_request,
        answer_question=lambda _question: pytest.fail("unexpected question"),
    )
    assert first is not None

    revised = coordinator.revise(
        planning_request,
        first,
        "Make the delivery a releasable small product.",
        answer_question=lambda _question: pytest.fail("unexpected question"),
    )

    assert revised is not None
    assert revised.body.product_definition is not None
    assert (
        revised.body.product_definition.delivery_maturity.level
        is DeliveryMaturity.RELEASABLE_SMALL_PRODUCT
    )


def test_clarity_gate_rejects_a_writer_claimed_as_independent_verifier() -> None:
    body = proposal_body()
    criteria = (
        body.acceptance_criteria[0].model_copy(
            update={"verification_agent_ids": ("cli_developer",)}
        ),
        body.acceptance_criteria[1],
    )

    with pytest.raises(PlanningError, match="not read-only quality"):
        preview_adaptive_proposal(
            request(),
            proposal(body=body.model_copy(update={"acceptance_criteria": criteria})),
            policy(),
            created_at=FIXED_TIME,
        )


def test_clarity_gate_rejects_an_authorized_choice_hidden_as_an_assumption() -> None:
    body = proposal_body()
    decisions = (
        *(
            decision
            for decision in body.decisions
            if decision.id != "DECISION_SCAN_STRUCTURE"
        ),
        PlanningDecisionRecord(
            id="DECISION_SCAN_STRUCTURE",
            category=PlanningDecisionCategory.RISK_TRADEOFF,
            authority=PlanningDecisionAuthority.USER,
            provenance=PlanningDecisionProvenance(
                kind=PlanningDecisionProvenanceKind.RESOLVED_QUESTION,
                source="network_risk",
            ),
            summary="Assume the user accepts network reliability risk.",
            rationale="This deliberately hides an unresolved risk decision.",
        ),
    )

    with pytest.raises(PlanningError, match="not an autonomous implementation choice"):
        preview_adaptive_proposal(
            request(),
            proposal(
                body=body.model_copy(
                    update={
                        "assumptions": (
                            "Assume the user accepts network reliability risk.",
                        ),
                        "decisions": decisions,
                    }
                )
            ),
            policy(),
            created_at=FIXED_TIME,
        )


def test_clarity_gate_requires_acceptance_coverage_for_every_requirement() -> None:
    body = proposal_body()
    criteria = tuple(
        criterion.model_copy(update={"requirement_ids": ("REQ_SCAN",)})
        for criterion in body.acceptance_criteria
    )

    with pytest.raises(PlanningError, match="requirements lack observable"):
        preview_adaptive_proposal(
            request(),
            proposal(body=body.model_copy(update={"acceptance_criteria": criteria})),
            policy(),
            created_at=FIXED_TIME,
        )


def test_absolute_criterion_requires_all_review_entry_boundaries() -> None:
    body = proposal_body()
    absolute = ProposedCriterion(
        id="AC_LINK_SAFETY",
        description="The scanner must not follow a symlink at any depth.",
        verification="Challenge every entry boundary with symlink fixtures.",
        requirement_ids=("REQ_SCAN",),
        verification_agent_ids=("quality_reviewer",),
    )

    tasks = (
        body.tasks[0].model_copy(
            update={
                "acceptance_criteria": (
                    *body.tasks[0].acceptance_criteria,
                    absolute.id,
                )
            }
        ),
    )
    incomplete = PlanningProposalBody.model_validate(
        body.model_copy(
            update={
                "acceptance_criteria": (*body.acceptance_criteria, absolute),
                "tasks": tasks,
            }
        )
    )
    with pytest.raises(PlanningError, match="must require top-level"):
        preview_adaptive_proposal(
            request(),
            proposal(body=incomplete),
            policy(),
            created_at=FIXED_TIME,
        )

    complete = absolute.model_copy(
        update={
            "review_boundaries": tuple(ReviewBoundaryKind),
            "verification_agent_ids": ("security_assessor",),
        }
    )
    security_assessor = ProposedAgent(
        id="security_assessor",
        label="Symlink Security Assessor",
        responsibility="Assess the approved symlink trust boundaries.",
        rationale="The absolute containment claim needs security authority.",
        capability=AgentCapability.REVIEW,
        specialization=AgentSpecialization.SECURITY_ASSESSMENT,
        stage_id="verify",
        dependencies=("cli_developer",),
        workspace_scope="repository",
        workload=AgentWorkload.ROUTINE,
    )
    accepted = PlanningProposalBody.model_validate(
        body.model_copy(
            update={
                "acceptance_criteria": (*body.acceptance_criteria, complete),
                "tasks": tasks,
                "agents": (*body.agents, security_assessor),
            }
        )
    )

    assert accepted.acceptance_criteria[-1].review_boundaries == tuple(
        ReviewBoundaryKind
    )
    preview = preview_adaptive_proposal(
        request(),
        proposal(body=accepted),
        policy(),
        created_at=FIXED_TIME,
    )
    overview = render_planning_overview(preview, include_fixed_policy=True)
    assert (
        "Review boundaries: top_level_input, nested_input, "
        "alias_or_indirection, failure_path"
    ) in overview
    assert "Review boundary definitions:" in overview
    assert "root itself is the top-level input" in overview
    assert "immediate first-level child, is nested input" in overview

    absolute_request = request().model_copy(
        update={
            "source_request": request().source_request + " It must not follow symlinks."
        }
    )
    with pytest.raises(PlanningError, match="no proposed acceptance criterion"):
        preview_adaptive_proposal(
            absolute_request,
            proposal(),
            policy(),
            created_at=FIXED_TIME,
        )


def test_response_normalizer_canonicalizes_only_safe_presentation_variants() -> None:
    payload = proposal_response().model_dump(mode="json")
    payload.pop("kind")
    payload["proposal"]["tasks"][0]["expected_paths"] = [
        " tests/ ",
        "./src//link_checker.py",
    ]
    payload["proposal"]["agents"][0]["workspace_scope"] = "repository/"
    original = json.loads(json.dumps(payload))

    normalized, changes = planning._normalize_planning_response_payload(payload)
    parsed = PlanningModelResponse.model_validate(normalized)

    assert payload == original
    assert parsed.kind is PlanningResponseKind.PROPOSAL
    assert parsed.proposal is not None
    assert parsed.proposal.tasks[0].expected_paths == (
        "tests",
        "src/link_checker.py",
    )
    assert parsed.proposal.agents[0].workspace_scope == "repository"
    assert changes == (
        "inferred response kind as proposal",
        "canonicalized proposal.tasks[0].expected_paths[0]",
        "canonicalized proposal.tasks[0].expected_paths[1]",
        "canonicalized proposal.agents[0].workspace_scope",
    )


def test_response_normalizer_uses_parallel_requirement_ids_and_disposition() -> None:
    payload = proposal_response().model_dump(mode="json")
    proposal_payload = payload["proposal"]
    proposal_payload["requirements"][0] = (
        "REQ_SCAN: REQ_SCAN: Scan Markdown files below a selected path."
    )
    proposal_payload["requirements"][1] = (
        "REQ_SCAN: Report broken local links with source locations."
    )
    target_users = proposal_payload["product_definition"]["target_users"]
    target_users.update(
        {
            "disposition": "not_material",
            "source": "planner",
            "rationale": "The approved throwaway has no material audience.",
            "requirement_ids": ["REQ_SCAN"],
            "criterion_ids": ["AC_SCAN"],
            "decision_ids": ["DECISION_TEAM"],
        }
    )
    original = json.loads(json.dumps(payload))

    normalized, changes = planning._normalize_planning_response_payload(payload)
    parsed = PlanningModelResponse.model_validate(normalized)

    assert payload == original
    assert parsed.proposal is not None
    assert parsed.proposal.requirements[0] == (
        "Scan Markdown files below a selected path."
    )
    assert parsed.proposal.requirements[1].startswith("REQ_SCAN:")
    normalized_target = parsed.proposal.product_definition
    assert normalized_target is not None
    assert not normalized_target.target_users.requirement_ids
    assert not normalized_target.target_users.criterion_ids
    assert not normalized_target.target_users.decision_ids
    assert changes[:2] == (
        "removed redundant stable ID prefix from proposal.requirements[0]",
        (
            "removed downstream references from not-material "
            "proposal.product_definition.target_users"
        ),
    )


def test_response_normalizer_compiles_atomic_requirements_without_mutating_raw() -> (
    None
):
    payload = proposal_response().model_dump(mode="json")
    proposal_payload = payload["proposal"]
    descriptions = proposal_payload["requirements"]
    requirement_ids = proposal_payload["requirement_ids"]
    proposal_payload["requirements"] = [
        {
            "id": requirement_id,
            "description": f"{requirement_id}: {description}",
        }
        for requirement_id, description in zip(
            requirement_ids,
            descriptions,
            strict=True,
        )
    ]
    proposal_payload["requirement_ids"] = ["REQ_CONFLICTING_REDUNDANT_FIELD"]
    original = json.loads(json.dumps(payload))

    normalized, changes = planning._normalize_planning_response_payload(payload)
    parsed = PlanningModelResponse.model_validate(normalized)

    assert payload == original
    assert parsed.proposal is not None
    assert parsed.proposal.requirement_ids == tuple(requirement_ids)
    assert parsed.proposal.requirements == tuple(descriptions)
    assert changes == (
        (
            "compiled atomic proposal.requirements into canonical descriptions "
            "and stable IDs"
        ),
        "removed redundant stable ID prefix from proposal.requirements[0]",
        "removed redundant stable ID prefix from proposal.requirements[1]",
    )


def test_response_normalizer_rejects_mixed_requirement_shapes() -> None:
    payload = proposal_response().model_dump(mode="json")
    payload["proposal"]["requirements"] = [
        {"id": "REQ_SCAN", "description": "Scan Markdown files."},
        "Report broken links.",
    ]

    with pytest.raises(PlanningError, match="cannot be mixed"):
        planning._normalize_planning_response_payload(payload)


def test_response_normalizer_compiles_atomic_assumptions_without_mutating_raw() -> None:
    payload = proposal_response().model_dump(mode="json")
    proposal_payload = payload["proposal"]
    statements = [
        "Use one single-process scan.",
        "Keep the first implementation local and synchronous.",
    ]
    proposal_payload["assumptions"] = [
        {
            "statement": statement,
            "decision_id": "DECISION_SCAN_STRUCTURE",
        }
        for statement in statements
    ]
    proposal_payload["assumption_decision_ids"] = ["DECISION_CONFLICTING"]
    original = json.loads(json.dumps(payload))

    normalized, changes = planning._normalize_planning_response_payload(payload)
    parsed = PlanningModelResponse.model_validate(normalized)

    assert payload == original
    assert parsed.proposal is not None
    assert parsed.proposal.assumptions == tuple(statements)
    assert parsed.proposal.assumption_decision_ids == (
        "DECISION_SCAN_STRUCTURE",
        "DECISION_SCAN_STRUCTURE",
    )
    assert changes == (
        "compiled atomic proposal.assumptions into canonical statements "
        "and autonomous decision references",
    )


def test_response_normalizer_rejects_mixed_assumption_shapes() -> None:
    payload = proposal_response().model_dump(mode="json")
    payload["proposal"]["assumptions"] = [
        {
            "statement": "Use one single-process scan.",
            "decision_id": "DECISION_SCAN_STRUCTURE",
        },
        "Keep the first implementation local.",
    ]

    with pytest.raises(PlanningError, match="cannot be mixed"):
        planning._normalize_planning_response_payload(payload)


def test_current_proposal_context_uses_atomic_relations_without_rewriting_body() -> (
    None
):
    body = proposal_body()
    canonical = body.model_dump(mode="json")

    projected = planning._planning_proposal_body_for_model(body)

    assert body.model_dump(mode="json") == canonical
    assert "requirement_ids" not in projected
    assert projected["requirements"] == [
        {"id": requirement_id, "description": description}
        for requirement_id, description in zip(
            body.requirement_ids,
            body.requirements,
            strict=True,
        )
    ]
    assert "assumption_decision_ids" not in projected
    assert projected["assumptions"] == [
        {"statement": statement, "decision_id": decision_id}
        for statement, decision_id in zip(
            body.assumptions,
            body.assumption_decision_ids,
            strict=True,
        )
    ]


def test_response_normalizer_canonicalizes_unambiguous_decision_tokens() -> None:
    payload = proposal_response().model_dump(mode="json")
    decisions = payload["proposal"]["decisions"]
    canonical_ids = [decision["id"] for decision in decisions]
    lowercase_ids = [
        "DECISION_" + decision_id.removeprefix("DECISION_").lower()
        for decision_id in canonical_ids
    ]
    for decision, lowercase_id in zip(decisions, lowercase_ids, strict=True):
        decision["id"] = lowercase_id
    payload["proposal"]["assumption_decision_ids"] = ["DECISION_scan_structure"]
    original = json.loads(json.dumps(payload))

    normalized, changes = planning._normalize_planning_response_payload(payload)
    parsed = PlanningModelResponse.model_validate(normalized)

    assert payload == original
    assert parsed.proposal is not None
    assert [decision.id for decision in parsed.proposal.decisions] == canonical_ids
    assert parsed.proposal.assumption_decision_ids == ("DECISION_SCAN_STRUCTURE",)
    assert len(changes) == len(canonical_ids) + 1
    assert changes[0] == (
        "canonicalized proposal.decisions[0].id as DECISION_ACCEPTANCE"
    )
    assert changes[-1] == (
        "canonicalized proposal.assumption_decision_ids[0] as DECISION_SCAN_STRUCTURE"
    )


def test_response_normalizer_compiles_decision_authority_and_legacy_sources() -> None:
    payload = proposal_response().model_dump(mode="json")
    proposal_payload = payload["proposal"]
    for decision in proposal_payload["decisions"]:
        decision.pop("provenance")
        decision.pop("authority")
    proposal_payload["decisions"][1]["authority"] = "user"
    proposal_payload["decisions"][:0] = [
        {
            "id": "DECISION_TARGET_USERS",
            "category": "product_requirement",
            "authority": "user",
            "summary": "A developer is the intended user.",
            "rationale": "This interpretation came from the product definition.",
        },
        {
            "id": "DECISION_WORKFLOW",
            "category": "product_requirement",
            "authority": "user",
            "summary": "The user checks Markdown links.",
            "rationale": "This interpretation came from the product definition.",
        },
    ]
    proposal_payload["decisions"].append(
        {
            "id": "DECISION_NO_REMOTE_FETCH",
            "category": "privacy_or_data",
            "summary": "without fetching remote URLs",
            "rationale": "The request states this boundary directly.",
        }
    )
    definition = proposal_payload["product_definition"]
    definition["target_users"]["decision_ids"] = ["DECISION_TARGET_USERS"]
    definition["primary_workflow"]["decision_ids"] = ["DECISION_WORKFLOW"]
    definition["delivery_maturity"]["decision_ids"] = ["DECISION_DELIVERY"]
    original = json.loads(json.dumps(payload))

    normalized, changes = planning._normalize_planning_response_payload(
        payload,
        user_inputs=(request().source_request,),
    )
    parsed = PlanningModelResponse.model_validate(normalized)

    assert payload == original
    assert parsed.proposal is not None
    assert [item.id for item in parsed.proposal.decisions] == [
        "DECISION_ACCEPTANCE",
        "DECISION_DELIVERY",
        "DECISION_TEAM",
        "DECISION_MODEL_ROUTE",
        "DECISION_SCAN_STRUCTURE",
        "DECISION_NO_REMOTE_FETCH",
    ]
    assert all(item.provenance is not None for item in parsed.proposal.decisions)
    assert parsed.proposal.decisions[1].authority is (
        PlanningDecisionAuthority.PLANNER_PROPOSAL
    )
    assert (
        parsed.proposal.decisions[1].provenance.kind
        is PlanningDecisionProvenanceKind.PLANNER_RECOMMENDATION
    )
    assert not parsed.proposal.product_definition.target_users.decision_ids
    assert not parsed.proposal.product_definition.primary_workflow.decision_ids
    assert not parsed.proposal.product_definition.delivery_maturity.decision_ids
    assert parsed.proposal.decisions[-1].provenance == PlanningDecisionProvenance(
        kind=PlanningDecisionProvenanceKind.EXPLICIT_INPUT,
        source="without fetching remote URLs",
    )
    assert any("compiled proposal.decisions[3].authority" in item for item in changes)
    assert any(
        "removed redundant direct-input decision DECISION_TARGET_USERS" in item
        for item in changes
    )


def test_response_normalizer_removes_current_direct_product_duplicate() -> None:
    payload = proposal_response().model_dump(mode="json")
    proposal_payload = payload["proposal"]
    maturity = proposal_payload["product_definition"]["delivery_maturity"]
    assert maturity["disposition"] == "explicit_input"
    assert maturity["decision_ids"] == []
    proposal_payload["decisions"].insert(
        0,
        {
            "id": "DECISION_DUPLICATE_MATURITY",
            "category": "delivery",
            "authority": "user",
            "provenance": {
                "kind": "explicit_input",
                "source": maturity["source"],
            },
            "summary": "Paraphrased delivery maturity.",
            "rationale": "The product definition already carries this fact.",
        },
    )
    original = json.loads(json.dumps(payload))

    normalized, changes = planning._normalize_planning_response_payload(payload)
    parsed = PlanningModelResponse.model_validate(normalized)

    assert payload == original
    assert parsed.proposal is not None
    assert "DECISION_DUPLICATE_MATURITY" not in {
        item.id for item in parsed.proposal.decisions
    }
    assert changes == (
        "compiled proposal.decisions[0].authority from category delivery",
        "compiled proposal.decisions[0].summary from exact direct-input source",
        "removed redundant direct-input decision DECISION_DUPLICATE_MATURITY "
        "from proposal.decisions[0]",
    )


def test_response_normalizer_preserves_same_source_independent_user_decision() -> None:
    payload = proposal_response().model_dump(mode="json")
    proposal_payload = payload["proposal"]
    operations = proposal_payload["product_definition"]["operational_expectations"]
    operations.update(
        {
            "statement": "Do not fetch remote URLs.",
            "disposition": "explicit_input",
            "source": "without fetching remote URLs",
            "rationale": "The user supplied this operational boundary.",
            "criterion_ids": ["AC_SCAN"],
            "decision_ids": [],
        }
    )
    proposal_payload["decisions"].append(
        {
            "id": "DECISION_PRIVACY_BOUNDARY",
            "category": "privacy_or_data",
            "provenance": {
                "kind": "explicit_input",
                "source": "without fetching remote URLs",
            },
            "summary": "without fetching remote URLs",
            "rationale": "The same words also establish a data-access boundary.",
        }
    )
    original = json.loads(json.dumps(payload))

    normalized, changes = planning._normalize_planning_response_payload(
        payload,
        user_inputs=(request().source_request,),
    )
    parsed = PlanningModelResponse.model_validate(normalized)

    assert payload == original
    assert parsed.proposal is not None
    privacy_decision = next(
        item
        for item in parsed.proposal.decisions
        if item.id == "DECISION_PRIVACY_BOUNDARY"
    )
    assert privacy_decision.authority is PlanningDecisionAuthority.USER
    assert privacy_decision.provenance == PlanningDecisionProvenance(
        kind=PlanningDecisionProvenanceKind.EXPLICIT_INPUT,
        source="without fetching remote URLs",
    )
    assert not any(
        "removed redundant direct-input decision DECISION_PRIVACY_BOUNDARY" in item
        for item in changes
    )


def test_current_decision_schema_exposes_source_but_not_derived_authority() -> None:
    schema = planning._planning_response_schema()
    decision_schema = schema["$defs"]["PlanningDecisionRecord"]

    branches = decision_schema["oneOf"]
    assert len(branches) == 4
    actual = {
        (
            tuple(branch["properties"]["category"]["enum"]),
            branch["properties"]["provenance"]["properties"]["kind"]["const"],
            branch["properties"]["provenance"]["properties"]["source"].get("const"),
        )
        for branch in branches
    }
    user_categories = (
        "product_requirement",
        "risk_tradeoff",
        "privacy_or_data",
        "external_action",
        "organization_policy",
    )
    assert actual == {
        (user_categories, "explicit_input", None),
        (user_categories, "resolved_question", None),
        (
            (
                "acceptance_scope",
                "delivery",
                "resource_budget",
                "team",
                "model_route",
            ),
            "planner_recommendation",
            "planner",
        ),
        (("local_implementation", "scheduling"), "agent_autonomy", "agent"),
    }
    for branch in branches:
        assert branch["required"] == [
            "id",
            "category",
            "provenance",
            "summary",
            "rationale",
        ]
        assert "authority" not in branch["properties"]
        assert "question_id" not in branch["properties"]
        source_schema = branch["properties"]["provenance"]["properties"]["source"]
        if "const" not in source_schema:
            assert source_schema["minLength"] == 1
            assert source_schema["maxLength"] == 2000


def test_response_normalizer_compiles_question_owner_from_category() -> None:
    payload = product_intent_question_response().model_dump(mode="json")
    payload["question"].pop("decision_owner")
    original = json.loads(json.dumps(payload))

    normalized, changes = planning._normalize_planning_response_payload(payload)
    parsed = PlanningModelResponse.model_validate(normalized)

    assert payload == original
    assert parsed.question is not None
    assert parsed.question.decision_owner is PlanningDecisionAuthority.USER
    assert changes == (
        "compiled question.decision_owner from category product_requirement",
    )


def test_response_normalizer_leaves_case_collisions_for_strict_rejection() -> None:
    payload = proposal_response().model_dump(mode="json")
    payload["proposal"]["decisions"][0]["id"] = "DECISION_COLLISION"
    payload["proposal"]["decisions"][1]["id"] = "DECISION_collision"

    normalized, changes = planning._normalize_planning_response_payload(payload)

    assert normalized["proposal"]["decisions"][0]["id"] == "DECISION_COLLISION"
    assert normalized["proposal"]["decisions"][1]["id"] == "DECISION_collision"
    assert changes == ()
    with pytest.raises(ValidationError, match="String should match pattern"):
        PlanningModelResponse.model_validate(normalized)


def test_response_normalizer_leaves_unsafe_paths_for_strict_rejection() -> None:
    payload = proposal_response().model_dump(mode="json")
    payload["proposal"]["tasks"][0]["expected_paths"] = ["../secret/"]

    normalized, changes = planning._normalize_planning_response_payload(payload)

    assert normalized["proposal"]["tasks"][0]["expected_paths"] == ["../secret/"]
    assert changes == ()
    with pytest.raises(ValidationError, match="canonical safe relative POSIX paths"):
        PlanningModelResponse.model_validate(normalized)


def test_response_normalizer_removes_only_active_profile_criterion_echoes() -> None:
    profile_criterion = AcceptanceCriterion(
        id="AC_PROFILE",
        description="The controller owns this canonical contract.",
        verification="Run the controller-owned profile gate.",
    )
    payload = proposal_response().model_dump(mode="json")
    payload["proposal"]["acceptance_criteria"].append(
        {
            "id": "AC_PROFILE",
            "description": "A model-authored rewrite must not replace the contract.",
            "verification": "A model-authored verification must not be authoritative.",
            "review_boundaries": ["top_level_input"],
        }
    )
    payload["proposal"]["tasks"].append(
        quality_task_payload(acceptance_criteria=["AC_PROFILE"])
    )
    original = json.loads(json.dumps(payload))

    normalized, changes = planning._normalize_planning_response_payload(
        payload,
        profile_criterion_ids=(profile_criterion.id,),
    )
    parsed = PlanningModelResponse.model_validate(normalized)

    assert payload == original
    assert parsed.proposal is not None
    assert [item.id for item in parsed.proposal.acceptance_criteria] == [
        "AC_SCAN",
        "AC_REPORT",
    ]
    assert parsed.proposal.tasks[-1].acceptance_criteria == ("AC_PROFILE",)
    assert changes == (
        "removed controller-owned profile criterion AC_PROFILE from "
        "proposal.acceptance_criteria[2]",
    )

    unchanged, unchanged_changes = planning._normalize_planning_response_payload(
        payload
    )
    assert unchanged == payload
    assert unchanged_changes == ()
    with pytest.raises(
        ValidationError,
        match="writer tasks do not cover proposal acceptance criteria: AC_PROFILE",
    ):
        PlanningModelResponse.model_validate(unchanged)


def quality_task_payload(
    *,
    acceptance_criteria: list[str] | None = None,
    expected_paths: list[str] | None = None,
) -> dict[str, object]:
    """Return explicit quality-stage work like a live Planner may propose."""

    return {
        "id": "TASK_REVIEW",
        "owner_agent_id": "quality_reviewer",
        "description": "Independently review the implementation and evidence.",
        "dependencies": ["TASK_IMPLEMENT"],
        "acceptance_criteria": acceptance_criteria or ["AC_SCAN", "AC_PROFILE"],
        "expected_paths": expected_paths or ["src", "tests", "README.md"],
    }


def multi_task_quality_payload(*, review_dependencies: list[str]) -> dict[str, object]:
    """Return a Journey-43-shaped proposal with two writer tasks."""

    payload = proposal_response().model_dump(mode="json")
    tasks = payload["proposal"]["tasks"]
    tasks.append(
        {
            "id": "TASK_TESTS",
            "owner_agent_id": "cli_developer",
            "description": "Add deterministic tests for the implementation.",
            "dependencies": ["TASK_IMPLEMENT"],
            "acceptance_criteria": ["AC_SCAN", "AC_REPORT"],
            "expected_paths": ["tests"],
        }
    )
    tasks.append(
        {
            "id": "TASK_REVIEW",
            "owner_agent_id": "quality_reviewer",
            "description": "Independently review all writing tasks.",
            "dependencies": review_dependencies,
            "acceptance_criteria": ["AC_SCAN", "AC_REPORT"],
            "expected_paths": ["src", "tests", "README.md"],
        }
    )
    return payload


def test_response_normalizer_compiles_cross_agent_task_dependencies() -> None:
    payload = multi_task_quality_payload(review_dependencies=[])
    payload["proposal"]["tasks"][-1].pop("dependencies")
    original = deepcopy(payload)

    normalized, changes = planning._normalize_planning_response_payload(payload)
    parsed = PlanningModelResponse.model_validate(normalized)

    assert payload == original
    assert parsed.proposal is not None
    tasks = {task.id: task for task in parsed.proposal.tasks}
    assert tasks["TASK_TESTS"].dependencies == ("TASK_IMPLEMENT",)
    assert tasks["TASK_REVIEW"].dependencies == (
        "TASK_IMPLEMENT",
        "TASK_TESTS",
    )
    assert changes == (
        "compiled cross-Agent task dependencies from the authoritative Agent DAG",
    )
    persisted = PlanningProposal(
        run_id=request().run_id,
        revision=1,
        created_at=FIXED_TIME,
        source=PlanningProposalSource.MODEL,
        source_turn_sequence=1,
        body=parsed.proposal,
    )
    assert persisted.body.tasks[-1].dependencies == (
        "TASK_IMPLEMENT",
        "TASK_TESTS",
    )


def test_current_proposal_rejects_uncompiled_task_dependency_projection() -> None:
    raw = PlanningModelResponse.model_validate(
        multi_task_quality_payload(review_dependencies=[])
    )
    assert raw.proposal is not None

    with pytest.raises(ValidationError, match="Controller projection"):
        PlanningProposal(
            run_id=request().run_id,
            revision=1,
            created_at=FIXED_TIME,
            source=PlanningProposalSource.MODEL,
            source_turn_sequence=1,
            body=raw.proposal,
        )

    legacy = PlanningProposal(
        schema_version=15,
        run_id=request().run_id,
        revision=1,
        created_at=FIXED_TIME,
        source=PlanningProposalSource.MODEL,
        source_turn_sequence=1,
        body=raw.proposal,
    )
    assert legacy.body.tasks[-1].dependencies == ()


def test_task_dependency_projection_follows_direct_agent_dag_only() -> None:
    payload = multi_task_quality_payload(review_dependencies=["TASK_IMPLEMENT"])
    agents = payload["proposal"]["agents"]
    reviewer = next(agent for agent in agents if agent["id"] == "quality_reviewer")
    reviewer["dependencies"] = ["acceptance_tester"]
    payload["proposal"]["tasks"].insert(
        2,
        {
            "id": "TASK_ACCEPTANCE",
            "owner_agent_id": "acceptance_tester",
            "description": "Inspect the implemented behavior against acceptance.",
            "dependencies": [],
            "acceptance_criteria": ["AC_SCAN", "AC_REPORT"],
            "expected_paths": ["src", "tests"],
        },
    )

    normalized, changes = planning._normalize_planning_response_payload(payload)
    parsed = PlanningModelResponse.model_validate(normalized)

    assert parsed.proposal is not None
    tasks = {task.id: task for task in parsed.proposal.tasks}
    assert tasks["TASK_ACCEPTANCE"].dependencies == (
        "TASK_IMPLEMENT",
        "TASK_TESTS",
    )
    assert tasks["TASK_REVIEW"].dependencies == ("TASK_ACCEPTANCE",)
    assert changes == (
        "compiled cross-Agent task dependencies from the authoritative Agent DAG",
    )


def test_task_dependency_projection_keeps_unrelated_edges_for_rejection() -> None:
    payload = multi_task_quality_payload(review_dependencies=[])
    payload["proposal"]["tasks"].insert(
        2,
        {
            "id": "TASK_ACCEPTANCE",
            "owner_agent_id": "acceptance_tester",
            "description": "Inspect the implemented behavior against acceptance.",
            "dependencies": [],
            "acceptance_criteria": ["AC_SCAN"],
            "expected_paths": ["src"],
        },
    )
    payload["proposal"]["tasks"][-1]["dependencies"] = ["TASK_ACCEPTANCE"]

    normalized, _ = planning._normalize_planning_response_payload(payload)

    with pytest.raises(ValidationError, match="does not depend on acceptance_tester"):
        PlanningModelResponse.model_validate(normalized)


def test_coordinator_persists_one_canonical_dependency_projection(
    tmp_path: Path,
) -> None:
    payload = multi_task_quality_payload(review_dependencies=[])
    executor = ScriptedAgentExecutor([json.dumps(payload)])
    store = PlanningStore(tmp_path / "planning")
    coordinator = AdaptivePlanningCoordinator(
        executor=executor,
        store=store,
        policy=policy(response_repair_limit=0),
        clock=AdvancingClock(),
    )

    created = coordinator.start(
        request(),
        answer_question=lambda _question: pytest.fail("unexpected question"),
    )

    assert created is not None
    assert len(executor.requests) == 1
    assert created.body.tasks[-1].dependencies == (
        "TASK_IMPLEMENT",
        "TASK_TESTS",
    )
    turn = store.load_turn(request().run_id, 1)
    assert turn.parsed_response is not None
    assert turn.parsed_response.proposal is not None
    assert turn.parsed_response.proposal.tasks[-1].dependencies == (
        "TASK_IMPLEMENT",
        "TASK_TESTS",
    )
    assert turn.response_normalizations == (
        "compiled cross-Agent task dependencies from the authoritative Agent DAG",
    )
    preview = preview_adaptive_proposal(
        request(),
        created,
        policy(response_repair_limit=0),
        created_at=FIXED_TIME,
    )
    assert preview.implementation_plan.tasks[-1].dependencies == (
        "TASK_IMPLEMENT",
        "TASK_TESTS",
    )
    assert "dependencies: TASK_IMPLEMENT, TASK_TESTS" in render_planning_overview(
        preview
    )


def test_proposal_preserves_quality_owned_work_without_granting_authority() -> None:
    payload = proposal_response().model_dump(mode="json")
    payload["proposal"]["tasks"].append(quality_task_payload())
    parsed = PlanningModelResponse.model_validate(payload)
    profile_criterion = AcceptanceCriterion(
        id="AC_PROFILE",
        description="The project satisfies the fixed runtime contract.",
        verification="Run the profile contract gate.",
    )
    assert parsed.proposal is not None

    preview = preview_adaptive_proposal(
        request(),
        proposal(body=parsed.proposal),
        policy(profile_acceptance_criteria=(profile_criterion,)),
        created_at=FIXED_TIME,
    )

    assert [task.id for task in preview.implementation_plan.tasks] == [
        "TASK_IMPLEMENT",
        "TASK_REVIEW",
    ]
    assert preview.team_plan.execution_waves() == (
        ("cli_developer",),
        ("acceptance_tester", "quality_reviewer"),
    )
    overview = render_planning_overview(preview)
    assert "TASK_REVIEW -> quality_reviewer" in overview
    assert "read-only verification focus; no project changes permitted" in overview
    assert "permission: read_only" in overview


@pytest.mark.parametrize(
    ("invalid_case", "message"),
    (
        ("unknown_owner", "tasks reference unknown Agent owners: absent_reviewer"),
        (
            "sole_proposal_coverage",
            "writer tasks do not cover proposal acceptance criteria: AC_REPORT",
        ),
        (
            "inverted_dependency",
            "task TASK_IMPLEMENT depends on TASK_REVIEW, but Agent cli_developer "
            "does not depend on quality_reviewer",
        ),
    ),
)
def test_quality_owned_tasks_cannot_bypass_plan_authority(
    invalid_case: str,
    message: str,
) -> None:
    payload = proposal_response().model_dump(mode="json")
    quality_task = quality_task_payload()
    if invalid_case == "unknown_owner":
        quality_task["owner_agent_id"] = "absent_reviewer"
    elif invalid_case == "sole_proposal_coverage":
        payload["proposal"]["tasks"][0]["acceptance_criteria"] = ["AC_SCAN"]
        quality_task["acceptance_criteria"] = ["AC_REPORT"]
    else:
        payload["proposal"]["tasks"][0]["dependencies"] = ["TASK_REVIEW"]
        quality_task["dependencies"] = []
    payload["proposal"]["tasks"].append(quality_task)

    with pytest.raises(ValidationError, match=message):
        PlanningModelResponse.model_validate(payload)


def test_proposed_workspace_scope_cannot_repeat_the_destination_directory() -> None:
    payload = proposal_response().model_dump(mode="json")
    payload["proposal"]["agents"][0]["workspace_scope"] = "link-checker"

    with pytest.raises(ValidationError, match="must start at repository"):
        PlanningModelResponse.model_validate(payload)


def test_proposal_compiles_to_complete_controller_owned_authority() -> None:
    preview = preview_adaptive_proposal(
        request(),
        proposal(),
        policy(),
        created_at=FIXED_TIME,
    )

    assert preview.task_brief.confirmed
    assert preview.team_plan.origin is TeamPlanOrigin.ADAPTIVE_PLANNING
    assert preview.team_plan.execution_waves() == (
        ("cli_developer",),
        ("acceptance_tester", "quality_reviewer"),
    )
    assert (
        preview.team_plan.get_agent("cli_developer").permission_profile
        is PermissionProfile.WORKSPACE_WRITE
    )
    assert (
        preview.team_plan.get_agent("quality_reviewer").permission_profile
        is PermissionProfile.READ_ONLY
    )
    assert preview.team_plan.model_routes.routes[0].model == "provider/model"
    assert preview.team_plan.task_brief_sha256 == canonical_model_sha256(
        preview.task_brief
    )

    overview = render_planning_overview(preview)
    assert "Destination: /tmp/link-checker" in overview
    assert "Small greenfield Python 3.12 project" in overview
    assert "Runtime Agents" in overview
    assert (
        "execution order: cli_developer -> acceptance_tester + quality_reviewer"
        in overview
    )
    assert "permission: workspace_write" in overview
    assert "workspace changes permitted within approved scope" in overview
    assert "timeout: 600 seconds" in overview
    assert "model: provider/model" in overview
    assert "cost authority: metered against shared task ceiling $25" in overview
    assert "model calls: 14" in overview
    assert "cumulative Agent time: 7200 seconds" in overview
    assert "estimated cost ceiling: $25" in overview


def test_materially_different_requests_compile_distinct_specialist_contracts() -> None:
    def preview_for(source_request: str, body: PlanningProposalBody) -> PlanningPreview:
        return preview_adaptive_proposal(
            request(source_request=source_request),
            proposal(body=body),
            policy(),
            created_at=FIXED_TIME,
        )

    ordinary_body = proposal_body()
    assert ordinary_body.product_definition is not None
    security_criterion = ProposedCriterion(
        id="AC_SECURITY",
        description="A link target outside the selected root is rejected.",
        verification="Probe direct and nested untrusted path inputs.",
        review_boundaries=tuple(ReviewBoundaryKind),
        requirement_ids=("REQ_SECURITY",),
        verification_agent_ids=("security_assessor",),
    )
    security_body = ordinary_body.model_copy(
        update={
            "requirements": (
                *ordinary_body.requirements,
                "Reject link targets that escape the selected root.",
            ),
            "requirement_ids": (*ordinary_body.requirement_ids, "REQ_SECURITY"),
            "acceptance_criteria": (
                *ordinary_body.acceptance_criteria,
                security_criterion,
            ),
            "tasks": (
                ordinary_body.tasks[0].model_copy(
                    update={
                        "acceptance_criteria": (
                            *ordinary_body.tasks[0].acceptance_criteria,
                            "AC_SECURITY",
                        )
                    }
                ),
            ),
            "agents": (
                *ordinary_body.agents,
                ProposedAgent(
                    id="security_assessor",
                    label="Path Security Assessor",
                    responsibility=(
                        "Assess untrusted path boundaries and residual risk."
                    ),
                    rationale=(
                        "The request introduces an explicit untrusted-input surface."
                    ),
                    capability=AgentCapability.REVIEW,
                    specialization=AgentSpecialization.SECURITY_ASSESSMENT,
                    stage_id="verify",
                    dependencies=("cli_developer",),
                    workspace_scope="repository",
                    workload=AgentWorkload.ROUTINE,
                ),
            ),
            "max_concurrency": 3,
        }
    )
    experience_agents = tuple(
        agent.model_copy(
            update={
                "id": "experience_assessor",
                "label": "CLI Experience Assessor",
                "responsibility": (
                    "Assess the target user's failure and recovery workflow."
                ),
                "rationale": (
                    "The request makes interactive recovery a delivery concern."
                ),
                "specialization": AgentSpecialization.EXPERIENCE_ASSESSMENT,
                "dependencies": ("acceptance_tester",),
            }
        )
        if agent.id == "quality_reviewer"
        else agent
        for agent in ordinary_body.agents
    )
    experience_criteria = tuple(
        criterion.model_copy(
            update={
                "verification_agent_ids": tuple(
                    "experience_assessor"
                    if agent_id == "quality_reviewer"
                    else agent_id
                    for agent_id in criterion.verification_agent_ids
                )
            }
        )
        for criterion in ordinary_body.acceptance_criteria
    )
    experience_body = ordinary_body.model_copy(
        update={
            "agents": experience_agents,
            "acceptance_criteria": experience_criteria,
            "product_definition": ordinary_body.product_definition.model_copy(
                update={
                    "primary_workflow": (
                        ordinary_body.product_definition.primary_workflow.model_copy(
                            update={"criterion_ids": ("AC_REPORT",)}
                        )
                    )
                }
            ),
        }
    )

    ordinary = preview_for(request().source_request, ordinary_body)
    security = preview_for(
        (
            "For developers who will use it repeatedly, build a usable local product "
            "that checks Markdown links in files and fragments without fetching "
            "remote URLs. Treat note paths as untrusted security inputs."
        ),
        security_body,
    )
    experience = preview_for(
        (
            "For developers who will use it repeatedly, build a usable local product "
            "that checks Markdown links in files and fragments without fetching "
            "remote URLs. Make the terminal failure and recovery workflow usable."
        ),
        experience_body,
    )

    security_agent = security.team_plan.get_agent("security_assessor")
    experience_agent = experience.team_plan.get_agent("experience_assessor")
    assert security_agent.specialization is AgentSpecialization.SECURITY_ASSESSMENT
    assert security_agent.expected_output is ArtifactKind.SECURITY_ASSESSMENT
    assert experience_agent.specialization is (
        AgentSpecialization.EXPERIENCE_ASSESSMENT
    )
    assert experience_agent.expected_output is ArtifactKind.EXPERIENCE_ASSESSMENT
    assert {agent.id for agent in ordinary.team_plan.agents} == {
        "cli_developer",
        "acceptance_tester",
        "quality_reviewer",
    }
    assert {agent.id for agent in security.team_plan.agents} == {
        "cli_developer",
        "acceptance_tester",
        "quality_reviewer",
        "security_assessor",
    }
    assert {agent.id for agent in experience.team_plan.agents} == {
        "cli_developer",
        "acceptance_tester",
        "experience_assessor",
    }
    assert security.team_plan.execution_waves() == (
        ("cli_developer",),
        ("acceptance_tester", "quality_reviewer", "security_assessor"),
    )
    assert experience.team_plan.execution_waves() == (
        ("cli_developer",),
        ("acceptance_tester",),
        ("experience_assessor",),
    )
    security_acceptance = {
        criterion.id: criterion.verification_agent_ids
        for criterion in security.implementation_plan.acceptance_criteria
    }
    experience_acceptance = {
        criterion.id: criterion.verification_agent_ids
        for criterion in experience.implementation_plan.acceptance_criteria
    }
    assert security_acceptance["AC_SECURITY"] == ("security_assessor",)
    assert experience_acceptance["AC_REPORT"] == (
        "acceptance_tester",
        "experience_assessor",
    )
    assert security.review_scope_by_agent == {
        "quality_reviewer": ("AC_SCAN", "AC_REPORT"),
        "security_assessor": ("AC_SECURITY",),
    }
    assert experience.review_scope_by_agent == {
        "experience_assessor": ("AC_SCAN", "AC_REPORT"),
    }
    assert security.team_plan.model_dump(
        mode="json"
    ) != experience.team_plan.model_dump(mode="json")
    security_overview = render_planning_overview(security)
    experience_overview = render_planning_overview(experience)
    assert "acceptance authority: security" in security_overview
    assert "output: security_assessment" in security_overview
    assert "acceptance authority: experience" not in security_overview
    assert "acceptance authority: user_experience" in experience_overview
    assert "output: experience_assessment" in experience_overview
    assert "acceptance authority: security" not in experience_overview
    assert (
        "security_assessor [security_assessment; authority=security]: AC_SECURITY"
    ) in security_overview


def test_controller_projects_team_narrative_from_the_typed_agent_graph() -> None:
    body = proposal_body()
    assert body.product_definition is not None
    stale = "Two runtime Agents: one writer and one general Reviewer."
    definition = body.product_definition.model_copy(
        update={
            "impact": body.product_definition.impact.model_copy(update={"team": stale})
        }
    )
    decisions = tuple(
        decision.model_copy(update={"summary": stale, "rationale": stale})
        if decision.category is PlanningDecisionCategory.TEAM
        else decision
        for decision in body.decisions
    )
    security_criterion = ProposedCriterion(
        id="AC_SECURITY",
        description="No untrusted path can escape the selected root.",
        verification="Probe every approved path-entry boundary.",
        review_boundaries=tuple(ReviewBoundaryKind),
        requirement_ids=("REQ_SECURITY",),
        verification_agent_ids=("security_assessor",),
    )
    security_agent = ProposedAgent(
        id="security_assessor",
        label="Path Security Assessor",
        responsibility="Assess untrusted path boundaries and residual risk.",
        rationale="The request introduces an untrusted-input boundary.",
        capability=AgentCapability.REVIEW,
        specialization=AgentSpecialization.SECURITY_ASSESSMENT,
        stage_id="verify",
        dependencies=("cli_developer",),
        workspace_scope="repository",
        workload=AgentWorkload.ROUTINE,
    )
    current = body.model_copy(
        update={
            "product_definition": definition,
            "decisions": decisions,
            "requirements": (*body.requirements, "Contain every untrusted path."),
            "requirement_ids": (*body.requirement_ids, "REQ_SECURITY"),
            "acceptance_criteria": (*body.acceptance_criteria, security_criterion),
            "tasks": (
                body.tasks[0].model_copy(
                    update={
                        "acceptance_criteria": (
                            *body.tasks[0].acceptance_criteria,
                            "AC_SECURITY",
                        )
                    }
                ),
            ),
            "agents": (*body.agents, security_agent),
        }
    )

    preview = preview_adaptive_proposal(
        request(),
        proposal(body=current),
        policy(),
        created_at=FIXED_TIME,
    )

    expected = (
        "Controller-derived from the typed Agent graph: 4 runtime Agents; "
        "specialization composition: product_implementation=1, "
        "deterministic_testing=1, general_review=1, security_assessment=1. "
        "The typed Agent identities and dependencies are the sole runtime-team "
        "authority."
    )
    assert preview.task_brief.product_definition is not None
    assert preview.task_brief.product_definition.impact.team == expected
    assert preview.implementation_plan.product_definition is not None
    assert preview.implementation_plan.product_definition.impact.team == expected
    team_decision = next(
        decision
        for decision in preview.implementation_plan.decisions
        if decision.category is PlanningDecisionCategory.TEAM
    )
    assert team_decision.summary == expected
    assert team_decision.rationale == (
        "The Planner proposes the typed Agent graph; the Controller validates its "
        "capabilities, permissions, dependencies, outputs, and acceptance scopes. "
        "Per-Agent rationales below explain the task-specific composition."
    )
    overview = render_planning_overview(preview)
    assert stale not in overview
    assert expected in overview
    assert len(preview.team_plan.agents) == 4
    assert current.product_definition is not None
    assert current.product_definition.impact.team == stale
    assert (
        next(
            decision
            for decision in current.decisions
            if decision.category is PlanningDecisionCategory.TEAM
        ).summary
        == stale
    )


def test_controller_rejects_review_task_outside_compiled_scope() -> None:
    body = proposal_body()
    security_criterion = ProposedCriterion(
        id="AC_SECURITY",
        description="No untrusted path can escape the selected root.",
        verification="Probe every approved path-entry boundary.",
        review_boundaries=tuple(ReviewBoundaryKind),
        requirement_ids=("REQ_SECURITY",),
        verification_agent_ids=("security_assessor",),
    )
    security_agent = ProposedAgent(
        id="security_assessor",
        label="Path Security Assessor",
        responsibility="Assess untrusted path boundaries and residual risk.",
        rationale="The request introduces an untrusted-input boundary.",
        capability=AgentCapability.REVIEW,
        specialization=AgentSpecialization.SECURITY_ASSESSMENT,
        stage_id="verify",
        dependencies=("cli_developer",),
        workspace_scope="repository",
        workload=AgentWorkload.ROUTINE,
    )
    mismatched_review_task = ProposedTask(
        id="TASK_SECURITY_REVIEW",
        owner_agent_id="security_assessor",
        description="Review general reporting behavior.",
        dependencies=("TASK_IMPLEMENT",),
        acceptance_criteria=("AC_REPORT",),
    )
    invalid = body.model_copy(
        update={
            "requirements": (*body.requirements, "Contain every untrusted path."),
            "requirement_ids": (*body.requirement_ids, "REQ_SECURITY"),
            "acceptance_criteria": (*body.acceptance_criteria, security_criterion),
            "tasks": (
                body.tasks[0].model_copy(
                    update={
                        "acceptance_criteria": (
                            *body.tasks[0].acceptance_criteria,
                            "AC_SECURITY",
                        )
                    }
                ),
                mismatched_review_task,
            ),
            "agents": (*body.agents, security_agent),
        }
    )

    with pytest.raises(PlanningError, match="outside its compiled Review scope"):
        preview_adaptive_proposal(
            request(),
            proposal(body=invalid),
            policy(),
            created_at=FIXED_TIME,
        )

    valid = invalid.model_copy(
        update={
            "tasks": (
                invalid.tasks[0],
                mismatched_review_task.model_copy(
                    update={"acceptance_criteria": ("AC_SECURITY",)}
                ),
            )
        }
    )
    preview = preview_adaptive_proposal(
        request(),
        proposal(body=valid),
        policy(),
        created_at=FIXED_TIME,
    )
    corrupted_implementation = preview.implementation_plan.model_copy(
        update={"tasks": invalid.tasks}
    )
    corrupted_team = preview.team_plan.model_copy(
        update={
            "implementation_plan_sha256": canonical_model_sha256(
                corrupted_implementation
            )
        }
    )
    approval = planning.PlanningApproval(
        run_id=request().run_id,
        revision=1,
        approved_at=FIXED_TIME,
        confirmation="user_approved",
        proposal_sha256="a" * 64,
        task_brief_sha256=canonical_model_sha256(preview.task_brief),
        implementation_plan_sha256=canonical_model_sha256(corrupted_implementation),
        team_plan_sha256=canonical_model_sha256(corrupted_team),
        timeout_resolutions=preview.timeout_resolutions,
    )
    corrupted_approved = ApprovedPlanningResult(
        task_brief=preview.task_brief,
        implementation_plan=corrupted_implementation,
        team_plan=corrupted_team,
        approval=approval,
    )

    with pytest.raises(PlanningError, match="approved Review task contract"):
        compile_approved_review_scopes(corrupted_approved)


def test_controller_rejects_security_boundaries_assigned_to_general_review() -> None:
    body = proposal_body()
    security_criterion = ProposedCriterion(
        id="AC_SECURITY",
        description=(
            "No untrusted path can escape the selected root under any input shape."
        ),
        verification="Probe every approved path-entry boundary.",
        review_boundaries=tuple(ReviewBoundaryKind),
        requirement_ids=("REQ_SECURITY",),
        verification_agent_ids=("quality_reviewer",),
    )
    invalid = body.model_copy(
        update={
            "requirements": (*body.requirements, "Contain every untrusted path."),
            "requirement_ids": (*body.requirement_ids, "REQ_SECURITY"),
            "acceptance_criteria": (*body.acceptance_criteria, security_criterion),
            "tasks": (
                body.tasks[0].model_copy(
                    update={
                        "acceptance_criteria": (
                            *body.tasks[0].acceptance_criteria,
                            "AC_SECURITY",
                        )
                    }
                ),
            ),
        }
    )

    with pytest.raises(PlanningError, match="security acceptance authority"):
        preview_adaptive_proposal(
            request(
                source_request=(
                    f"{request().source_request} Treat every path as untrusted and "
                    "never permit a root escape."
                )
            ),
            proposal(body=invalid),
            policy(),
            created_at=FIXED_TIME,
        )


def test_controller_rejects_overlapping_general_and_security_review() -> None:
    body = proposal_body()
    security_agent = ProposedAgent(
        id="security_assessor",
        label="Path Security Assessor",
        responsibility="Assess untrusted path boundaries and residual risk.",
        rationale="The request introduces an untrusted-input boundary.",
        capability=AgentCapability.REVIEW,
        specialization=AgentSpecialization.SECURITY_ASSESSMENT,
        stage_id="verify",
        dependencies=("cli_developer",),
        workspace_scope="repository",
        workload=AgentWorkload.ROUTINE,
    )
    security_criterion = ProposedCriterion(
        id="AC_SECURITY",
        description="No untrusted path can escape the selected root.",
        verification="Probe every approved path-entry boundary.",
        review_boundaries=tuple(ReviewBoundaryKind),
        requirement_ids=("REQ_SECURITY",),
        verification_agent_ids=("quality_reviewer", "security_assessor"),
    )
    invalid = body.model_copy(
        update={
            "requirements": (*body.requirements, "Contain every untrusted path."),
            "requirement_ids": (*body.requirement_ids, "REQ_SECURITY"),
            "acceptance_criteria": (*body.acceptance_criteria, security_criterion),
            "tasks": (
                body.tasks[0].model_copy(
                    update={
                        "acceptance_criteria": (
                            *body.tasks[0].acceptance_criteria,
                            "AC_SECURITY",
                        )
                    }
                ),
            ),
            "agents": (*body.agents, security_agent),
        }
    )

    with pytest.raises(PlanningError, match="exactly one Review owner"):
        preview_adaptive_proposal(
            request(),
            proposal(body=invalid),
            policy(),
            created_at=FIXED_TIME,
        )


def test_controller_rejects_interactive_workflow_assigned_to_general_review() -> None:
    body = proposal_body()
    assert body.product_definition is not None
    definition = body.product_definition.model_copy(
        update={
            "primary_workflow": body.product_definition.primary_workflow.model_copy(
                update={"criterion_ids": ("AC_REPORT",)}
            )
        }
    )
    invalid = body.model_copy(update={"product_definition": definition})

    with pytest.raises(PlanningError, match="user-experience acceptance authority"):
        preview_adaptive_proposal(
            request(),
            proposal(body=invalid),
            policy(),
            created_at=FIXED_TIME,
        )


def test_planning_repairs_missing_security_specialist_through_typed_slots(
    tmp_path: Path,
) -> None:
    body = proposal_body()
    security_criterion = ProposedCriterion(
        id="AC_SECURITY",
        description="No untrusted path can ever escape the selected root.",
        verification="Probe every approved path-entry boundary.",
        review_boundaries=tuple(ReviewBoundaryKind),
        requirement_ids=("REQ_SECURITY",),
        verification_agent_ids=("security_assessor",),
    )
    security_agent = ProposedAgent(
        id="security_assessor",
        label="Path Security Assessor",
        responsibility="Assess untrusted path boundaries and residual risk.",
        rationale="The request introduces an untrusted-input boundary.",
        capability=AgentCapability.REVIEW,
        specialization=AgentSpecialization.SECURITY_ASSESSMENT,
        stage_id="verify",
        dependencies=("cli_developer",),
        workspace_scope="repository",
        workload=AgentWorkload.ROUTINE,
    )
    valid = body.model_copy(
        update={
            "requirements": (*body.requirements, "Contain every untrusted path."),
            "requirement_ids": (*body.requirement_ids, "REQ_SECURITY"),
            "acceptance_criteria": (*body.acceptance_criteria, security_criterion),
            "tasks": (
                body.tasks[0].model_copy(
                    update={
                        "acceptance_criteria": (
                            *body.tasks[0].acceptance_criteria,
                            "AC_SECURITY",
                        )
                    }
                ),
            ),
            "agents": (*body.agents, security_agent),
        }
    )
    invalid = valid.model_copy(
        update={
            "agents": body.agents,
            "acceptance_criteria": (
                *body.acceptance_criteria,
                security_criterion.model_copy(
                    update={"verification_agent_ids": ("quality_reviewer",)}
                ),
            ),
        }
    )
    invalid_payload = json.loads(
        response(
            PlanningModelResponse(
                kind=PlanningResponseKind.PROPOSAL,
                proposal=invalid,
            )
        )
    )
    valid_payload = json.loads(
        response(
            PlanningModelResponse(
                kind=PlanningResponseKind.PROPOSAL,
                proposal=valid,
            )
        )
    )
    source_request = (
        f"{request().source_request} Treat every path as untrusted and never "
        "permit a root escape."
    )
    correction_base, _ = planning._normalize_planning_response_payload(
        invalid_payload,
        profile_criterion_ids=(),
        user_inputs=(source_request,),
    )
    criterion_path = "/proposal/acceptance_criteria/2/verification_agent_ids"
    executor = ScriptedAgentExecutor(
        [
            json.dumps(invalid_payload),
            correction_response(
                correction_base,
                {
                    "/proposal/agents": valid_payload["proposal"]["agents"],
                    criterion_path: valid_payload["proposal"]["acceptance_criteria"][2][
                        "verification_agent_ids"
                    ],
                },
            ),
        ]
    )
    store = PlanningStore(tmp_path / "planning")
    coordinator = AdaptivePlanningCoordinator(
        executor=executor,
        store=store,
        policy=policy(response_repair_limit=None),
        clock=AdvancingClock(),
    )

    planning_request = request(source_request=source_request)
    created = coordinator.start(
        planning_request,
        answer_question=lambda _question: pytest.fail("unexpected question"),
    )

    assert created is not None
    assert created.body == valid
    first = store.load_turn(request().run_id, 1)
    assert first.response_validation is not None
    assert first.response_validation.correction_paths == (
        criterion_path,
        "/proposal/agents",
    )
    assert store.load_turn(request().run_id, 2).semantic_correction_outcome == (
        "accepted"
    )
    approved = coordinator.approve(planning_request, created)
    assert compile_approved_review_scopes(approved) == {
        "quality_reviewer": ("AC_SCAN", "AC_REPORT"),
        "security_assessor": ("AC_SECURITY",),
    }


def test_planning_binds_review_task_corrections_to_each_compiled_scope(
    tmp_path: Path,
) -> None:
    body = proposal_body()
    security_criterion = ProposedCriterion(
        id="AC_SECURITY",
        description="No untrusted path can ever escape the selected root.",
        verification="Probe every approved path-entry boundary.",
        review_boundaries=tuple(ReviewBoundaryKind),
        requirement_ids=("REQ_SECURITY",),
        verification_agent_ids=("security_assessor",),
    )
    security_agent = ProposedAgent(
        id="security_assessor",
        label="Path Security Assessor",
        responsibility="Assess untrusted path boundaries and residual risk.",
        rationale="The request introduces an untrusted-input boundary.",
        capability=AgentCapability.REVIEW,
        specialization=AgentSpecialization.SECURITY_ASSESSMENT,
        stage_id="verify",
        dependencies=("cli_developer",),
        workspace_scope="repository",
        workload=AgentWorkload.ROUTINE,
    )
    security_task = ProposedTask(
        id="TASK_SECURITY_REVIEW",
        owner_agent_id="security_assessor",
        description="Review every untrusted path boundary.",
        dependencies=("TASK_IMPLEMENT",),
        acceptance_criteria=("AC_SECURITY",),
    )
    general_task = ProposedTask(
        id="TASK_GENERAL_REVIEW",
        owner_agent_id="quality_reviewer",
        description="Review general correctness and reporting behavior.",
        dependencies=("TASK_IMPLEMENT",),
        acceptance_criteria=("AC_REPORT",),
    )
    valid = body.model_copy(
        update={
            "requirements": (*body.requirements, "Contain every untrusted path."),
            "requirement_ids": (*body.requirement_ids, "REQ_SECURITY"),
            "acceptance_criteria": (*body.acceptance_criteria, security_criterion),
            "tasks": (
                body.tasks[0].model_copy(
                    update={
                        "acceptance_criteria": (
                            *body.tasks[0].acceptance_criteria,
                            "AC_SECURITY",
                        )
                    }
                ),
                general_task,
                security_task,
            ),
            "agents": (*body.agents, security_agent),
        }
    )
    invalid = valid.model_copy(
        update={
            "tasks": (
                valid.tasks[0],
                general_task.model_copy(
                    update={"acceptance_criteria": ("AC_REPORT", "AC_SECURITY")}
                ),
                security_task.model_copy(
                    update={"acceptance_criteria": ("AC_REPORT",)}
                ),
            )
        }
    )
    invalid_payload = json.loads(
        response(
            PlanningModelResponse(
                kind=PlanningResponseKind.PROPOSAL,
                proposal=invalid,
            )
        )
    )
    valid_payload = json.loads(
        response(
            PlanningModelResponse(
                kind=PlanningResponseKind.PROPOSAL,
                proposal=valid,
            )
        )
    )
    source_request = (
        f"{request().source_request} Treat every path as untrusted and never "
        "permit a root escape."
    )
    correction_base, _ = planning._normalize_planning_response_payload(
        invalid_payload,
        profile_criterion_ids=(),
        user_inputs=(source_request,),
    )
    task_paths = ("/proposal/tasks/1", "/proposal/tasks/2")
    executor = ScriptedAgentExecutor(
        [
            json.dumps(invalid_payload),
            correction_response(
                correction_base,
                {
                    path: valid_payload["proposal"]["tasks"][index]
                    for index, path in enumerate(task_paths, start=1)
                },
            ),
        ]
    )
    store = PlanningStore(tmp_path / "planning")
    coordinator = AdaptivePlanningCoordinator(
        executor=executor,
        store=store,
        policy=policy(response_repair_limit=None),
        clock=AdvancingClock(),
    )
    planning_request = request(source_request=source_request)

    created = coordinator.start(
        planning_request,
        answer_question=lambda _question: pytest.fail("unexpected user question"),
    )

    assert created is not None
    assert created.body == valid
    first = store.load_turn(request().run_id, 1)
    assert first.response_validation is not None
    assert first.response_validation.correction_paths == task_paths
    contract = executor.requests[1].submission_contract
    assert contract is not None
    variants = contract.parameters_schema()["properties"]["replacements"]["items"][
        "oneOf"
    ]
    assert len(variants) == 2
    expected = (
        ("TASK_GENERAL_REVIEW", "quality_reviewer", ["AC_SCAN", "AC_REPORT"]),
        ("TASK_SECURITY_REVIEW", "security_assessor", ["AC_SECURITY"]),
    )
    for variant, (task_id, owner, criterion_ids) in zip(
        variants, expected, strict=True
    ):
        task_schema = variant["properties"]["replacement_value"]
        properties = task_schema["properties"]
        assert properties["id"] == {"const": task_id, "type": "string"}
        assert properties["owner_agent_id"] == {
            "const": owner,
            "type": "string",
        }
        acceptance = properties["acceptance_criteria"]
        assert acceptance["items"]["enum"] == criterion_ids
        assert acceptance["uniqueItems"] is True
        assert "AC_UNKNOWN" not in acceptance["items"]["enum"]
    assert store.load_turn(request().run_id, 2).semantic_correction_outcome == (
        "accepted"
    )


def test_controller_resolves_visible_per_agent_model_routes_before_approval() -> None:
    default = ModelProfile(
        id="default",
        model="provider/model",
        capabilities=(
            AgentCapability.CLARIFICATION,
            AgentCapability.PLANNING,
            AgentCapability.IMPLEMENTATION,
            AgentCapability.TESTING,
            AgentCapability.REVIEW,
        ),
        input_cost_per_million_usd="0.50",
        output_cost_per_million_usd="1.50",
    )
    quality = ModelProfile(
        id="quality",
        model="provider/quality",
        capabilities=(AgentCapability.TESTING, AgentCapability.REVIEW),
        priority=10,
    )
    routing = ModelRoutingPolicy(
        mode=ModelRoutingMode.POLICY,
        profiles=(default, quality),
        default_profile_id="default",
        capability_profile_overrides={AgentCapability.TESTING: "quality"},
        authorized_switch_conditions=(ModelSwitchCondition.PROVIDER_FAILURE,),
    )

    preview = preview_adaptive_proposal(
        request(),
        proposal(),
        policy(model_routing=routing),
        created_at=FIXED_TIME,
    )

    assignments = {
        assignment.agent_id: assignment
        for assignment in preview.team_plan.model_routes.assignments
    }
    assert assignments["cli_developer"].primary_route_id == "default"
    assert assignments["acceptance_tester"].primary_route_id == "quality"
    assert (
        assignments["acceptance_tester"].selection_source
        is ModelRouteSelectionSource.CAPABILITY_OVERRIDE
    )
    assert assignments["quality_reviewer"].primary_route_id == "default"
    overview = render_planning_overview(preview)
    assert "provider/quality (profile quality; capability_override)" in overview
    assert "model reason:" in overview
    assert "model pricing: not configured" in overview
    assert "$0.50 input / $1.50 output per million tokens" in overview
    assert (
        "authorized fallback profiles: default: provider/model "
        "(pricing: $0.50 input / $1.50 output per million tokens; "
        "cache pricing unknown)"
    ) in overview
    assert "model routing: policy" in overview


def test_user_can_override_one_agent_model_without_editing_plan_json() -> None:
    routing = ModelRoutingPolicy(
        mode=ModelRoutingMode.POLICY,
        profiles=(
            ModelProfile(
                id="default",
                model="provider/model",
                capabilities=tuple(AgentCapability),
            ),
            ModelProfile(
                id="quality",
                model="provider/quality",
                capabilities=(AgentCapability.TESTING, AgentCapability.REVIEW),
            ),
        ),
        default_profile_id="default",
    )
    edited = apply_structured_edit(
        proposal(),
        StructuredPlanEdit(
            kind=StructuredEditKind.AGENT_MODEL,
            agent_id="quality_reviewer",
            value="quality",
        ),
        created_at=FIXED_TIME + timedelta(seconds=1),
    )

    preview = preview_adaptive_proposal(
        request(),
        edited,
        policy(model_routing=routing),
        created_at=edited.created_at,
    )

    assignment = preview.team_plan.model_routes.get_assignment("quality_reviewer")
    assert assignment.primary_route_id == "quality"
    assert assignment.selection_source is ModelRouteSelectionSource.AGENT_OVERRIDE
    assert edited.model_profile_overrides == {"quality_reviewer": "quality"}


def test_single_profile_plan_editor_does_not_offer_a_hidden_model_option() -> None:
    strict_routing = ModelRoutingPolicy(
        mode=ModelRoutingMode.STRICT,
        profiles=(
            ModelProfile(
                id="default",
                model="provider/model",
                capabilities=tuple(AgentCapability),
            ),
        ),
        default_profile_id="default",
    )
    answers = iter(("3", "x"))
    output: list[str] = []

    edit = planning._read_structured_edit(
        proposal(),
        model_routing=strict_routing,
        read=lambda _prompt: next(answers),
        write=output.append,
    )

    assert edit is None
    assert "  3. One Agent model profile" not in output
    assert "One Agent timeout" not in output
    assert "Choose 1, 2, or x." in output


def test_planning_rejects_missing_model_capability_and_fallback_overbudget() -> None:
    missing_testing = ModelRoutingPolicy(
        mode=ModelRoutingMode.POLICY,
        profiles=(
            ModelProfile(
                id="default",
                model="provider/model",
                capabilities=(
                    AgentCapability.CLARIFICATION,
                    AgentCapability.PLANNING,
                    AgentCapability.IMPLEMENTATION,
                    AgentCapability.REVIEW,
                ),
            ),
        ),
        default_profile_id="default",
    )
    with pytest.raises(PlanningError, match="no authorized model profile"):
        preview_adaptive_proposal(
            request(),
            proposal(),
            policy(model_routing=missing_testing),
            created_at=FIXED_TIME,
        )

    switching = ModelRoutingPolicy(
        mode=ModelRoutingMode.POLICY,
        profiles=(
            ModelProfile(
                id="default",
                model="provider/model",
                capabilities=tuple(AgentCapability),
                priority=1,
            ),
            ModelProfile(
                id="fallback",
                model="provider/fallback",
                capabilities=tuple(AgentCapability),
                priority=2,
            ),
        ),
        default_profile_id="default",
        authorized_switch_conditions=(ModelSwitchCondition.PROVIDER_FAILURE,),
    )
    constrained_budget = AgentBudget(
        max_calls=10,
        max_input_tokens=1_000_000,
        max_output_tokens=200_000,
        max_agent_duration_seconds=7_200,
        max_estimated_cost_usd="25",
    )
    with pytest.raises(ValueError, match="fallback invocations exceed"):
        preview_adaptive_proposal(
            request(),
            proposal(),
            policy(model_routing=switching, budget=constrained_budget),
            created_at=FIXED_TIME,
        )


def test_controller_resolves_workload_classes_without_model_timeout_authority() -> None:
    body = proposal_body()
    workloads = {
        "cli_developer": AgentWorkload.COMPLEX,
        "acceptance_tester": AgentWorkload.SUBSTANTIAL,
        "quality_reviewer": AgentWorkload.ROUTINE,
    }
    agents = tuple(
        agent.model_copy(update={"workload": workloads[agent.id]})
        for agent in body.agents
    )

    preview = preview_adaptive_proposal(
        request(),
        proposal(body=body.model_copy(update={"agents": agents})),
        policy(),
        created_at=FIXED_TIME,
    )

    assert {agent.id: agent.timeout_seconds for agent in preview.team_plan.agents} == {
        "cli_developer": 900,
        "acceptance_tester": 270,
        "quality_reviewer": 240,
    }
    assert all(
        resolution.source == "policy_workload"
        for resolution in preview.timeout_resolutions
    )
    assert "timeout_seconds" not in proposal_response().model_dump_json()


def test_controller_raises_reviewer_timeout_floor_from_exact_scope() -> None:
    profile_criteria = tuple(
        AcceptanceCriterion(
            id=f"AC_PROFILE_{index}",
            description=f"The project satisfies profile condition {index}.",
            verification=f"Verify profile condition {index} independently.",
        )
        for index in range(1, 5)
    )
    configured = policy(profile_acceptance_criteria=profile_criteria)

    preview = preview_adaptive_proposal(
        request(),
        proposal(),
        configured,
        created_at=FIXED_TIME,
    )

    reviewer = preview.team_plan.get_agent("quality_reviewer")
    resolution = next(
        item
        for item in preview.timeout_resolutions
        if item.agent_id == "quality_reviewer"
    )
    assert len(preview.task_brief.acceptance_criteria) == 6
    assert reviewer.timeout_seconds == 270
    assert resolution.source == "policy_scope_floor"
    assert resolution.workload is AgentWorkload.ROUTINE
    assert resolution.minimum_seconds == 270
    assert resolution.scope_criterion_count == 6
    assert (
        "controller review-scope floor for 6 criteria + 0 boundary obligations "
        "(6 work units)"
    ) in (render_planning_overview(preview))
    assert "allowed 270..300" in render_planning_overview(preview)

    too_short = proposal().model_copy(
        update={"timeout_overrides_seconds": {"quality_reviewer": 250}}
    )
    with pytest.raises(PlanningError, match=r"policy envelope of 270\.\.300s"):
        preview_adaptive_proposal(
            request(),
            too_short,
            configured,
            created_at=FIXED_TIME,
        )


def test_review_scope_timeout_thresholds_are_deterministic_and_ordered() -> None:
    configured = policy()

    assert configured.review_scope_workload(5) is AgentWorkload.ROUTINE
    assert configured.review_scope_workload(6) is AgentWorkload.SUBSTANTIAL
    assert configured.review_scope_workload(9) is AgentWorkload.SUBSTANTIAL
    assert configured.review_scope_workload(10) is AgentWorkload.SUBSTANTIAL
    assert configured.review_scope_workload(11) is AgentWorkload.COMPLEX
    assert configured.review_scope_workload(12) is AgentWorkload.COMPLEX
    assert configured.review_scope_workload(13) is AgentWorkload.COMPLEX
    assert configured.review_scope_workload(10, 20) is AgentWorkload.COMPLEX
    with pytest.raises(ValidationError, match="thresholds must be ordered"):
        policy(
            review_substantial_work_unit_threshold=11,
            review_complex_work_unit_threshold=11,
        )


def test_controller_counts_explicit_review_boundaries_as_scope_work() -> None:
    boundaries = tuple(ReviewBoundaryKind)
    body = proposal_body()
    criteria = tuple(
        criterion.model_copy(
            update={
                "review_boundaries": boundaries,
                "verification_agent_ids": tuple(
                    dict.fromkeys(
                        (*criterion.verification_agent_ids, "quality_reviewer")
                    )
                ),
            }
        )
        for criterion in body.acceptance_criteria
    )
    agents = tuple(
        agent.model_copy(
            update={"specialization": AgentSpecialization.SECURITY_ASSESSMENT}
        )
        if agent.id == "quality_reviewer"
        else agent
        for agent in body.agents
    )
    profile_criterion = AcceptanceCriterion(
        id="AC_PROFILE_BOUNDARY",
        description="The profile guarantee holds across every approved boundary.",
        verification="Probe every approved profile boundary independently.",
        review_boundaries=boundaries,
    )

    preview = preview_adaptive_proposal(
        request(),
        proposal(
            body=body.model_copy(
                update={"acceptance_criteria": criteria, "agents": agents}
            )
        ),
        policy(profile_acceptance_criteria=(profile_criterion,)),
        created_at=FIXED_TIME,
    )

    reviewer = preview.team_plan.get_agent("quality_reviewer")
    resolution = next(
        item
        for item in preview.timeout_resolutions
        if item.agent_id == "quality_reviewer"
    )
    assert reviewer.timeout_seconds == 300
    assert resolution.source == "policy_scope_floor"
    assert resolution.scope_criterion_count == 3
    assert resolution.scope_boundary_obligation_count == 12
    assert resolution.minimum_seconds == 300
    assert (
        "controller review-scope floor for 3 criteria + 12 boundary obligations "
        "(15 work units)"
    ) in render_planning_overview(preview)


def test_controller_maps_eleven_criterion_review_to_complex_timeout() -> None:
    profile_criteria = tuple(
        AcceptanceCriterion(
            id=f"AC_PROFILE_{index}",
            description=f"The project satisfies profile condition {index}.",
            verification=f"Verify profile condition {index} independently.",
        )
        for index in range(1, 10)
    )
    configured = policy(profile_acceptance_criteria=profile_criteria)

    preview = preview_adaptive_proposal(
        request(),
        proposal(),
        configured,
        created_at=FIXED_TIME,
    )

    reviewer = preview.team_plan.get_agent("quality_reviewer")
    resolution = next(
        item
        for item in preview.timeout_resolutions
        if item.agent_id == "quality_reviewer"
    )
    assert len(preview.task_brief.acceptance_criteria) == 11
    assert reviewer.timeout_seconds == 300
    assert resolution.source == "policy_scope_floor"
    assert resolution.workload is AgentWorkload.ROUTINE
    assert resolution.minimum_seconds == 300
    assert resolution.scope_criterion_count == 11


def test_controller_adds_profile_criteria_without_model_echo() -> None:
    profile_criterion = AcceptanceCriterion(
        id="AC_PROFILE",
        description="The project satisfies the fixed runtime contract.",
        verification="Run the profile contract gate.",
    )
    configured = policy(
        profile_acceptance_criteria=(profile_criterion,),
        require_review_agent=True,
    )

    preview = preview_adaptive_proposal(
        request(),
        proposal(),
        configured,
        created_at=FIXED_TIME,
    )

    assert [criterion.id for criterion in preview.task_brief.acceptance_criteria] == [
        "AC_SCAN",
        "AC_REPORT",
        "AC_PROFILE",
    ]
    assert "AC_PROFILE" not in {
        criterion.id for criterion in proposal().body.acceptance_criteria
    }


def test_controller_preserves_known_profile_criterion_task_bindings(
    tmp_path: Path,
) -> None:
    profile_criterion = AcceptanceCriterion(
        id="AC_PROFILE",
        description="The project satisfies the fixed runtime contract.",
        verification="Run the profile contract gate.",
    )
    payload = proposal_response().model_dump(mode="json")
    payload["proposal"]["tasks"][0]["acceptance_criteria"].append("AC_PROFILE")

    parsed = PlanningModelResponse.model_validate(payload)

    assert parsed.proposal is not None
    configured = policy(profile_acceptance_criteria=(profile_criterion,))
    store = PlanningStore(tmp_path / "planning")
    coordinator = AdaptivePlanningCoordinator(
        executor=ScriptedAgentExecutor([response(parsed)]),
        store=store,
        policy=configured,
        clock=AdvancingClock(),
    )
    created = coordinator.start(
        request(),
        answer_question=lambda _question: pytest.fail("unexpected question"),
    )

    assert created is not None
    approved = coordinator.approve(request(), created)
    preview = preview_adaptive_proposal(
        request(),
        created,
        configured,
        created_at=FIXED_TIME,
    )
    assert [criterion.id for criterion in preview.task_brief.acceptance_criteria] == [
        "AC_SCAN",
        "AC_REPORT",
        "AC_PROFILE",
    ]
    assert preview.implementation_plan.tasks[0].acceptance_criteria == (
        "AC_SCAN",
        "AC_REPORT",
        "AC_PROFILE",
    )
    assert "acceptance: AC_SCAN, AC_REPORT, AC_PROFILE" in render_planning_overview(
        preview
    )
    assert approved.implementation_plan.tasks == preview.implementation_plan.tasks
    assert store.load_session(request().run_id).status is PlanningSessionStatus.APPROVED
    assert store.load_session(request().run_id).turn_count == 1


def test_controller_rejects_unknown_task_criterion_after_contextual_parse() -> None:
    payload = proposal_response().model_dump(mode="json")
    payload["proposal"]["tasks"][0]["acceptance_criteria"].append("AC_UNKNOWN")

    parsed = PlanningModelResponse.model_validate(payload)

    assert parsed.proposal is not None
    with pytest.raises(
        PlanningError,
        match="tasks reference unknown acceptance criteria: AC_UNKNOWN",
    ):
        preview_adaptive_proposal(
            request(),
            proposal(body=parsed.proposal),
            policy(),
            created_at=FIXED_TIME,
        )


def test_proposal_owned_criteria_still_require_task_coverage() -> None:
    payload = proposal_response().model_dump(mode="json")
    payload["proposal"]["tasks"][0]["acceptance_criteria"] = ["AC_SCAN"]

    with pytest.raises(
        ValidationError,
        match="writer tasks do not cover proposal acceptance criteria: AC_REPORT",
    ):
        PlanningModelResponse.model_validate(payload)


def test_controller_rejects_profile_criterion_echo_and_missing_reviewer() -> None:
    profile_criterion = AcceptanceCriterion(
        id="AC_PROFILE",
        description="The project satisfies the fixed runtime contract.",
        verification="Run the profile contract gate.",
    )
    configured = policy(
        profile_acceptance_criteria=(profile_criterion,),
        require_review_agent=True,
    )
    body = proposal_body()
    echoed = ProposedCriterion(
        id="AC_PROFILE",
        description=profile_criterion.description,
        verification=profile_criterion.verification,
        requirement_ids=("REQ_SCAN",),
        verification_agent_ids=("quality_reviewer",),
    )
    echoed_tasks = (
        body.tasks[0].model_copy(
            update={
                "acceptance_criteria": (
                    *body.tasks[0].acceptance_criteria,
                    "AC_PROFILE",
                )
            }
        ),
    )
    echoed_body = PlanningProposalBody.model_validate(
        body.model_copy(
            update={
                "acceptance_criteria": (*body.acceptance_criteria, echoed),
                "tasks": echoed_tasks,
            }
        )
    )
    with pytest.raises(PlanningError, match="controller-owned profile criteria"):
        preview_adaptive_proposal(
            request(),
            proposal(body=echoed_body),
            configured,
            created_at=FIXED_TIME,
        )

    without_reviewer = PlanningProposalBody.model_validate(
        body.model_copy(
            update={
                "agents": tuple(
                    agent
                    for agent in body.agents
                    if agent.capability is not AgentCapability.REVIEW
                ),
                "acceptance_criteria": tuple(
                    criterion.model_copy(
                        update={"verification_agent_ids": ("acceptance_tester",)}
                    )
                    for criterion in body.acceptance_criteria
                ),
                "max_concurrency": 1,
            }
        )
    )
    with pytest.raises(PlanningError, match="requires an independent review Agent"):
        preview_adaptive_proposal(
            request(),
            proposal(body=without_reviewer),
            configured,
            created_at=FIXED_TIME,
        )


def test_small_task_may_use_one_independent_quality_agent() -> None:
    body = proposal_body()
    agents = tuple(agent for agent in body.agents if agent.id != "acceptance_tester")
    criteria = tuple(
        criterion.model_copy(update={"verification_agent_ids": ("quality_reviewer",)})
        for criterion in body.acceptance_criteria
    )
    smaller = body.model_copy(
        update={
            "acceptance_criteria": criteria,
            "agents": agents,
            "max_concurrency": 1,
        }
    )

    preview = preview_adaptive_proposal(
        request(),
        proposal(body=PlanningProposalBody.model_validate(smaller)),
        policy(),
        created_at=FIXED_TIME,
    )

    assert tuple(agent.id for agent in preview.team_plan.agents) == (
        "cli_developer",
        "quality_reviewer",
    )
    assert preview.team_plan.execution_waves() == (
        ("cli_developer",),
        ("quality_reviewer",),
    )


def test_proposal_rejects_an_implementation_agent_without_tasks() -> None:
    body = proposal_body()
    extra = ProposedAgent(
        id="docs_developer",
        label="Documentation Developer",
        responsibility="Write task documentation.",
        rationale="Documentation has a separate write scope.",
        capability=AgentCapability.IMPLEMENTATION,
        stage_id="implement",
        workspace_scope="repository/docs",
        workload=AgentWorkload.ROUTINE,
    )
    agents = tuple(
        agent.model_copy(
            update={"dependencies": (*agent.dependencies, extra.id)}
            if agent.capability in {AgentCapability.TESTING, AgentCapability.REVIEW}
            else {}
        )
        for agent in (*body.agents, extra)
    )

    with pytest.raises(ValidationError, match="must own at least one task"):
        PlanningProposalBody.model_validate(body.model_copy(update={"agents": agents}))


def test_cross_agent_task_dependencies_require_matching_agent_dependencies() -> None:
    body = proposal_body()
    fixture_agent = ProposedAgent(
        id="fixture_developer",
        label="Fixture Developer",
        responsibility="Build deterministic test fixtures.",
        rationale="Fixture work has an isolated write scope.",
        capability=AgentCapability.IMPLEMENTATION,
        stage_id="implement",
        workspace_scope="repository/tests",
        workload=AgentWorkload.ROUTINE,
    )
    agents = tuple(
        agent.model_copy(
            update={"dependencies": (*agent.dependencies, fixture_agent.id)}
            if agent.capability in {AgentCapability.TESTING, AgentCapability.REVIEW}
            else {}
        )
        for agent in (*body.agents, fixture_agent)
    )
    tasks = (
        ProposedTask(
            id="TASK_FIXTURES",
            owner_agent_id=fixture_agent.id,
            description="Create deterministic valid and broken link fixtures.",
            acceptance_criteria=("AC_SCAN",),
            expected_paths=("tests/fixtures",),
        ),
        body.tasks[0].model_copy(update={"dependencies": ("TASK_FIXTURES",)}),
    )

    with pytest.raises(ValidationError, match="does not depend on fixture_developer"):
        PlanningProposalBody.model_validate(
            body.model_copy(update={"agents": agents, "tasks": tasks})
        )


def test_proposal_cannot_split_final_commit_coverage_across_quality_agents() -> None:
    body = proposal_body()
    fixture_agent = ProposedAgent(
        id="fixture_developer",
        label="Fixture Developer",
        responsibility="Build deterministic test fixtures.",
        rationale="Fixture work has an isolated write scope.",
        capability=AgentCapability.IMPLEMENTATION,
        stage_id="implement",
        workspace_scope="repository/tests",
        workload=AgentWorkload.ROUTINE,
    )
    agents = tuple(
        agent.model_copy(
            update={"dependencies": ("cli_developer",)}
            if agent.id == "acceptance_tester"
            else {"dependencies": ("fixture_developer",)}
            if agent.id == "quality_reviewer"
            else {}
        )
        for agent in (*body.agents, fixture_agent)
    )
    tasks = (
        *body.tasks,
        ProposedTask(
            id="TASK_FIXTURES",
            owner_agent_id=fixture_agent.id,
            description="Create deterministic link-checker fixtures.",
            acceptance_criteria=("AC_SCAN",),
            expected_paths=("tests/fixtures",),
        ),
    )

    with pytest.raises(ValidationError, match="every implementation path"):
        PlanningProposalBody.model_validate(
            body.model_copy(update={"agents": agents, "tasks": tasks})
        )


def test_controller_preserves_quality_dependency_and_concurrency_authority() -> None:
    chained_agents = tuple(
        agent.model_copy(
            update={"dependencies": ("acceptance_tester",)}
            if agent.id == "quality_reviewer"
            else {}
        )
        for agent in proposal_body().agents
    )
    chained = proposal(
        body=proposal_body().model_copy(update={"agents": chained_agents})
    )

    preview = preview_adaptive_proposal(
        request(),
        chained,
        policy(),
        created_at=FIXED_TIME,
    )

    assert preview.team_plan.execution_waves() == (
        ("cli_developer",),
        ("acceptance_tester",),
        ("quality_reviewer",),
    )

    excessive_concurrency = proposal(
        body=proposal_body().model_copy(update={"max_concurrency": 3})
    )
    with pytest.raises(PlanningError, match="policy ceiling of 2"):
        preview_adaptive_proposal(
            request(),
            excessive_concurrency,
            policy(max_concurrency=2),
            created_at=FIXED_TIME,
        )


def test_structured_edits_create_valid_revisions_without_internal_json() -> None:
    first = proposal()

    concurrency = apply_structured_edit(
        first,
        StructuredPlanEdit(kind=StructuredEditKind.MAX_CONCURRENCY, value=1),
        created_at=FIXED_TIME + timedelta(seconds=1),
    )
    iteration = apply_structured_edit(
        concurrency,
        StructuredPlanEdit(kind=StructuredEditKind.ITERATION_LIMIT, value=1),
        created_at=FIXED_TIME + timedelta(seconds=2),
    )
    larger_iteration = apply_structured_edit(
        iteration,
        StructuredPlanEdit(kind=StructuredEditKind.ITERATION_LIMIT, value=4),
        created_at=FIXED_TIME + timedelta(seconds=3),
    )

    assert concurrency.revision == 2
    assert concurrency.source is PlanningProposalSource.STRUCTURED_EDIT
    assert concurrency.body.max_concurrency == 1
    assert iteration.body.iteration_limit == 1
    assert not iteration.body.revision_enabled
    assert larger_iteration.body.iteration_limit == 4
    assert larger_iteration.body.revision_enabled


def test_store_detects_changed_append_only_turn_evidence(tmp_path: Path) -> None:
    store = PlanningStore(tmp_path / "planning")
    executor = ScriptedAgentExecutor([response(proposal_response())])
    coordinator = AdaptivePlanningCoordinator(
        executor=executor,
        store=store,
        policy=policy(),
        clock=AdvancingClock(),
    )

    created = coordinator.start(
        request(),
        answer_question=lambda _question: pytest.fail("unexpected question"),
    )

    assert created is not None
    turn_path = tmp_path / "planning" / request().run_id / "turns" / "001.json"
    payload = json.loads(turn_path.read_text(encoding="utf-8"))
    assert payload["response_normalizations"] == [
        (
            "compiled atomic proposal.requirements into canonical descriptions "
            "and stable IDs"
        ),
        (
            "compiled atomic proposal.assumptions into canonical statements "
            "and autonomous decision references"
        ),
    ]
    payload["user_message"] = "changed after persistence"
    turn_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(PlanningIntegrityError, match="head digest changed"):
        store.load_session(request().run_id)


def test_schema_two_proposal_keeps_hash_and_structured_edit_compatibility() -> None:
    payload = proposal().model_dump(mode="json")
    payload["schema_version"] = 2
    body = payload["body"]
    assert isinstance(body, dict)
    for field in (
        "product_definition",
        "requirement_ids",
        "non_goals",
        "assumption_decision_ids",
        "decisions",
    ):
        body.pop(field)
    criteria = body["acceptance_criteria"]
    assert isinstance(criteria, list)
    for criterion in criteria:
        criterion.pop("requirement_ids")
        criterion.pop("verification_agent_ids")
    expected = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()

    loaded = PlanningProposal.model_validate(payload)

    assert loaded.schema_version == 2
    assert loaded.model_dump(mode="json") == payload
    assert canonical_model_sha256(loaded) == expected

    edited = apply_structured_edit(
        loaded,
        StructuredPlanEdit(kind=StructuredEditKind.MAX_CONCURRENCY, value=1),
        created_at=FIXED_TIME + timedelta(seconds=1),
    )
    assert edited.schema_version == 2
    assert edited.body.max_concurrency == 1
    preview = preview_adaptive_proposal(
        request(),
        edited,
        policy(),
        created_at=FIXED_TIME + timedelta(seconds=1),
    )
    assert preview.implementation_plan.schema_version == 2


def test_schema_four_proposal_keeps_canonical_bytes_without_product_definition() -> (
    None
):
    payload = proposal().model_dump(mode="json")
    payload["schema_version"] = 4
    body = payload["body"]
    assert isinstance(body, dict)
    strip_v8_decision_fields(body)
    body.pop("product_definition")
    expected = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()

    loaded = PlanningProposal.model_validate(payload)

    assert loaded.schema_version == 4
    assert loaded.body.product_definition is None
    assert loaded.model_dump(mode="json") == payload
    assert canonical_model_sha256(loaded) == expected
    preview = preview_adaptive_proposal(
        request(),
        loaded,
        policy(),
        created_at=FIXED_TIME,
    )
    assert preview.implementation_plan.schema_version == 4
    assert preview.implementation_plan.product_definition is None
    assert preview.task_brief.product_definition is None
    assert "unavailable in legacy Planning evidence" in render_planning_overview(
        preview
    )


def test_schema_five_product_definition_proposal_remains_canonical() -> None:
    payload = proposal().model_dump(mode="json")
    payload["schema_version"] = 5
    body = payload["body"]
    assert isinstance(body, dict)
    strip_v8_decision_fields(body)
    body["product_definition"]["delivery_maturity"]["decision_ids"] = [
        "DECISION_DELIVERY"
    ]

    loaded = PlanningProposal.model_validate(payload)
    preview = preview_adaptive_proposal(
        request(),
        loaded,
        policy(),
        created_at=FIXED_TIME,
    )

    assert loaded.schema_version == 5
    assert loaded.body.product_definition is not None
    assert loaded.model_dump(mode="json") == payload
    assert preview.implementation_plan.schema_version == 5


def test_schema_three_turn_remains_readable_without_correction_evidence(
    tmp_path: Path,
) -> None:
    store = PlanningStore(tmp_path / "planning")
    coordinator = AdaptivePlanningCoordinator(
        executor=ScriptedAgentExecutor([response(proposal_response())]),
        store=store,
        policy=policy(),
        clock=AdvancingClock(),
    )
    coordinator.start(
        request(),
        answer_question=lambda _question: pytest.fail("unexpected question"),
    )
    payload = store.load_turn(request().run_id, 1).model_dump(mode="json")
    payload["schema_version"] = 3
    payload["execution"].pop("invocation_lifecycle", None)
    parsed_body = payload["parsed_response"]["proposal"]
    assert isinstance(parsed_body, dict)
    strip_v8_decision_fields(parsed_body)
    payload.pop("response_validation", None)
    payload.pop("semantic_correction_request", None)
    payload.pop("semantic_correction_outcome", None)
    payload.pop("submission_payload", None)
    payload.pop("submission_evidence", None)

    loaded = PlanningTurn.model_validate(payload)

    assert loaded.schema_version == 3
    assert (
        canonical_model_sha256(loaded)
        == hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
    )
    assert loaded.response_validation is None
    assert loaded.semantic_correction_request is None
    assert loaded.semantic_correction_outcome is None


def test_schema_six_turn_remains_canonical_without_typed_submission(
    tmp_path: Path,
) -> None:
    store = PlanningStore(tmp_path / "planning")
    coordinator = AdaptivePlanningCoordinator(
        executor=ScriptedAgentExecutor([response(proposal_response())]),
        store=store,
        policy=policy(),
        clock=AdvancingClock(),
    )
    coordinator.start(
        request(),
        answer_question=lambda _question: pytest.fail("unexpected question"),
    )
    payload = store.load_turn(request().run_id, 1).model_dump(mode="json")
    payload["schema_version"] = 6
    payload["execution"].pop("invocation_lifecycle", None)
    parsed_body = payload["parsed_response"]["proposal"]
    assert isinstance(parsed_body, dict)
    strip_v8_decision_fields(parsed_body)
    payload.pop("submission_payload")
    payload.pop("submission_evidence")
    expected = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()

    loaded = PlanningTurn.model_validate(payload)

    assert loaded.schema_version == 6
    assert loaded.submission_payload is None
    assert loaded.submission_evidence is None
    assert loaded.model_dump(mode="json") == payload
    assert canonical_model_sha256(loaded) == expected


def test_schema_seven_turn_remains_canonical_without_decision_provenance(
    tmp_path: Path,
) -> None:
    store = PlanningStore(tmp_path / "planning")
    coordinator = AdaptivePlanningCoordinator(
        executor=ScriptedAgentExecutor(
            [
                ScriptedAgentResponse(
                    text="ignored",
                    submission_payload=proposal_response().model_dump(mode="json"),
                )
            ]
        ),
        store=store,
        policy=policy(),
        clock=AdvancingClock(),
    )
    coordinator.start(
        request(),
        answer_question=lambda _question: pytest.fail("unexpected question"),
    )
    payload = store.load_turn(request().run_id, 1).model_dump(mode="json")
    payload["schema_version"] = 7
    payload["execution"].pop("invocation_lifecycle", None)
    submission_body = payload["submission_payload"]["proposal"]
    parsed_body = payload["parsed_response"]["proposal"]
    assert isinstance(submission_body, dict)
    assert isinstance(parsed_body, dict)
    strip_v8_decision_fields(submission_body)
    strip_v8_decision_fields(parsed_body)
    payload["submission_evidence"]["semantic_payload_sha256"] = (
        planning.canonical_json_sha256(payload["submission_payload"])
    )
    expected = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()

    loaded = PlanningTurn.model_validate(payload)

    assert loaded.schema_version == 7
    assert loaded.model_dump(mode="json") == payload
    assert canonical_model_sha256(loaded) == expected


def test_current_planning_turn_rejects_tampered_submission_payload(
    tmp_path: Path,
) -> None:
    store = PlanningStore(tmp_path / "planning")
    coordinator = AdaptivePlanningCoordinator(
        executor=ScriptedAgentExecutor([response(proposal_response())]),
        store=store,
        policy=policy(),
        clock=AdvancingClock(),
    )
    coordinator.start(
        request(),
        answer_question=lambda _question: pytest.fail("unexpected question"),
    )
    payload = store.load_turn(request().run_id, 1).model_dump(mode="json")
    assert isinstance(payload["submission_payload"], dict)
    payload["submission_payload"]["unexpected"] = True

    with pytest.raises(ValidationError, match="matching accepted evidence"):
        PlanningTurn.model_validate(payload)


def test_planning_persists_runtime_rejections_without_legacy_reinterpretation(
    tmp_path: Path,
) -> None:
    class ExecutorWithRejection(ScriptedAgentExecutor):
        def execute(self, *args, **kwargs):
            result = super().execute(*args, **kwargs)
            payload = result.telemetry.model_dump(mode="json")
            payload["runtime_rejections"] = [
                {
                    "reason": "unknown_tool",
                    "record_index": 1,
                    "tool_name": "missing_tool",
                    "external_call_sha256": "b" * 64,
                    "record_sha256": "c" * 64,
                }
            ]
            telemetry = AgentExecutionTelemetry.model_validate(payload)
            return result.model_copy(update={"telemetry": telemetry})

    store = PlanningStore(tmp_path / "planning")
    coordinator = AdaptivePlanningCoordinator(
        executor=ExecutorWithRejection(
            [
                ScriptedAgentResponse(
                    text=proposal_response().model_dump_json(),
                )
            ]
        ),
        store=store,
        policy=policy(),
        clock=AdvancingClock(),
    )
    coordinator.start(
        request(), answer_question=lambda _: pytest.fail("unexpected question")
    )
    turn = store.load_turn(request().run_id, 1)
    assert turn.schema_version == planning.PLANNING_SCHEMA_VERSION
    evidence = turn.execution.runtime_rejection_evidence
    assert evidence is not None
    assert evidence.rejections[0].tool_name == "missing_tool"
    payload = turn.model_dump(mode="json")
    payload["schema_version"] = 11
    payload["execution"].pop("invocation_lifecycle", None)
    with pytest.raises(ValidationError, match="legacy Planning turns"):
        PlanningTurn.model_validate(payload)
    del payload["execution"]["runtime_rejection_evidence"]
    assert PlanningTurn.model_validate(payload).model_dump(mode="json") == payload


def test_planning_store_accepts_evidence_indexes_beyond_three_digits(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "turns"
    evidence.mkdir()
    (evidence / "999.json").write_text("{}\n", encoding="utf-8")
    (evidence / "1000.json").write_text("{}\n", encoding="utf-8")

    assert PlanningStore._indexed_files(evidence) == {999, 1000}


@pytest.mark.parametrize("cache_read_tokens", [0, 1_000_000])
@pytest.mark.parametrize("invalid_proposal", [False, True])
def test_planning_uses_the_shared_task_cost_ledger_and_persists_source(
    tmp_path: Path,
    cache_read_tokens: int,
    invalid_proposal: bool,
) -> None:
    task_budget = AgentBudget(
        authority=BudgetAuthority.USER_TASK,
        max_estimated_cost_usd="1.00",
    )
    ledger = AgentBudgetLedger(task_budget)
    store = PlanningStore(tmp_path / "planning")
    submitted = proposal_response().model_dump(mode="json")
    if invalid_proposal:
        submitted["proposal"]["non_goals"] = []
    executor = ScriptedAgentExecutor(
        [
            ScriptedAgentResponse(
                text=json.dumps(submitted),
                model="provider/model",
                provider="provider",
                usage=AgentTokenUsage(
                    input_tokens=100_000,
                    output_tokens=20_000,
                    cache_read_tokens=cache_read_tokens,
                    cache_write_tokens=0,
                ),
                duration_ms=125,
            )
        ]
    )
    coordinator = AdaptivePlanningCoordinator(
        executor=executor,
        store=store,
        policy=policy(budget=task_budget, response_repair_limit=0),
        budget_ledger=ledger,
        pricing=ModelPricing(
            model="provider/model",
            input_cost_per_million_usd="2.50",
            output_cost_per_million_usd="10.00",
            pricing_source=ModelMetadataSource.RUNTIME_CATALOG,
            pricing_observed_at=FIXED_TIME,
            cache_pricing=CachePricing(
                read_cost_per_million_usd="0.014",
                write_cost_per_million_usd=0,
                source=ModelMetadataSource.USER_SUPPLIED,
                observed_at=FIXED_TIME,
            ),
        ),
        route_id="default",
        clock=AdvancingClock(),
    )

    if invalid_proposal:
        with pytest.raises(PlanningError):
            coordinator.start(
                request(),
                answer_question=lambda _question: pytest.fail("unexpected question"),
            )
    else:
        assert (
            coordinator.start(
                request(),
                answer_question=lambda _question: pytest.fail("unexpected question"),
            )
            is not None
        )

    usage = ledger.snapshot()
    assert usage.calls_started == 1
    assert usage.calls_completed == 1
    expected = (
        Decimal("0.45") + Decimal(cache_read_tokens) * Decimal("0.014") / 1_000_000
    )
    assert usage.known_estimated_cost_usd == expected
    execution = store.load_turn(request().run_id, 1).execution
    assert execution.estimated_cost_usd == expected
    assert ledger.call_records()[0].cache_usage.read_tokens == cache_read_tokens
    assert execution.cost_record == ledger.call_records()[0]
    assert execution.cost_record.cost_usd == expected
    assert execution.pricing_source is ModelMetadataSource.RUNTIME_CATALOG
    assert execution.budget_usage == usage
    assert execution.budget_error is None
    turn = store.load_turn(request().run_id, 1)
    serialized = turn.model_dump(mode="json")
    assert (
        PlanningTurn.model_validate(serialized).execution.cost_record
        == execution.cost_record
    )
    legacy = json.loads(json.dumps(serialized))
    legacy["schema_version"] = 10
    with pytest.raises(ValidationError, match="legacy Planning turns"):
        PlanningTurn.model_validate(legacy)
    del legacy["execution"]["cost_record"]
    legacy["execution"].pop("invocation_lifecycle", None)
    assert PlanningTurn.model_validate(legacy).model_dump(mode="json") == legacy
    missing = json.loads(json.dumps(serialized))
    del missing["execution"]["cost_record"]
    with pytest.raises(ValidationError, match="requires its cost record"):
        PlanningTurn.model_validate(missing)
    foreign = json.loads(json.dumps(serialized))
    foreign["execution"]["cost_record"]["run_id"] = "another-run"
    with pytest.raises(ValidationError, match="another invocation"):
        PlanningTurn.model_validate(foreign)


def test_planning_cost_progress_and_approval_overview_show_remaining_authority() -> (
    None
):
    output: list[str] = []
    progress = TerminalPlanningProgress(write=output.append)
    usage = AgentBudgetUsage(
        calls_started=1,
        calls_completed=1,
        active_calls=0,
        input_tokens=100_000,
        output_tokens=20_000,
        agent_duration_ms=125,
        known_estimated_cost_usd="0.45",
        unpriced_calls=0,
        unreported_token_calls=0,
    )
    progress(
        PlanningActivity(
            kind=PlanningActivityKind.BUDGET_UPDATED,
            attempt=1,
            maximum_attempts=2,
            model="provider/model",
            budget_usage=usage,
            budget_ceiling_usd=Decimal("1.00"),
            pricing_source=ModelMetadataSource.RUNTIME_CATALOG,
        )
    )
    product_budget = AgentBudget(
        authority=BudgetAuthority.USER_TASK,
        max_estimated_cost_usd="1.00",
    )
    preview = preview_adaptive_proposal(
        request(),
        proposal(),
        policy(budget=product_budget),
        created_at=FIXED_TIME,
    )
    overview = render_planning_overview(preview, budget_usage=usage)

    assert "$0.450000 estimated / $1.00 authorized" in output[-1]
    assert "price source runtime_catalog" in output[-1]
    assert "recorded Planning spend: $0.450000 estimated" in overview
    assert "recorded budget remaining before execution: $0.550000" in overview
    assert "absolute billing cap: requires a provider-side" in overview


def test_planning_persists_provider_liveness_evidence(tmp_path: Path) -> None:
    liveness = ProviderLivenessEvidence(
        mode="enforced",
        policy_source="test provider contract",
        silence_seconds=120,
        stall_grace_seconds=30,
        raw_stream_observed=True,
        session_observed=True,
        provider_activity_observations=3,
        tool_started_count=1,
        tool_completed_count=1,
        stall_suspected_count=1,
        stall_recovered_count=1,
        stalled=False,
    )
    store = PlanningStore(tmp_path / "planning")
    coordinator = AdaptivePlanningCoordinator(
        executor=ScriptedAgentExecutor(
            [
                ScriptedAgentResponse(
                    text=response(proposal_response()),
                    model="provider/model",
                    provider="provider",
                    duration_ms=125,
                    provider_liveness=liveness,
                )
            ]
        ),
        store=store,
        policy=policy(),
        clock=AdvancingClock(),
    )

    created = coordinator.start(
        request(),
        answer_question=lambda _question: pytest.fail("unexpected question"),
    )

    assert created is not None
    assert store.load_turn(request().run_id, 1).execution.provider_liveness == liveness


def test_store_rejects_unanchored_planning_evidence(tmp_path: Path) -> None:
    store = PlanningStore(tmp_path / "planning")
    coordinator = AdaptivePlanningCoordinator(
        executor=ScriptedAgentExecutor([response(proposal_response())]),
        store=store,
        policy=policy(),
        clock=AdvancingClock(),
    )
    assert (
        coordinator.start(
            request(),
            answer_question=lambda _question: pytest.fail("unexpected question"),
        )
        is not None
    )
    turns = tmp_path / "planning" / request().run_id / "turns"
    (turns / "002.json").write_bytes((turns / "001.json").read_bytes())

    with pytest.raises(PlanningIntegrityError, match="differ from the session anchor"):
        store.load_session(request().run_id)


def test_dialogue_revision_structured_edit_and_approval_are_recoverable(
    tmp_path: Path,
) -> None:
    planning_request = request(source_request=AMBIGUOUS_LINK_REQUEST)
    answered_body = proposal_body(question_id="link_scope")
    revised_body = proposal_body(
        title="Local Markdown Link Checker",
        question_id="link_scope",
    )
    executor = ScriptedAgentExecutor(
        [
            response(question_response()),
            response(proposal_response(answered_body)),
            response(proposal_response(revised_body)),
        ]
    )
    store = PlanningStore(tmp_path / "planning")
    coordinator = AdaptivePlanningCoordinator(
        executor=executor,
        store=store,
        policy=policy(),
        clock=AdvancingClock(),
    )
    questions: list[str] = []

    def answer(question: PlanningQuestion) -> str:
        questions.append(question.text)
        return "Only local file and fragment links."

    first = coordinator.start(planning_request, answer_question=answer)
    assert first is not None
    second = coordinator.revise(
        planning_request,
        first,
        "Make the local-only scope explicit in the title.",
        answer_question=answer,
    )
    assert second is not None
    third = coordinator.structured_edit(
        planning_request,
        second,
        StructuredPlanEdit(kind=StructuredEditKind.MAX_CONCURRENCY, value=1),
    )
    approved = coordinator.approve(planning_request, third)

    assert questions == ["Should the first version check web links too?"]
    assert approved.task_brief.title == "Local Markdown Link Checker"
    assert approved.team_plan.max_concurrency == 1
    assert approved.approval.revision == 3
    assert approved.approval.team_plan_sha256 == canonical_model_sha256(
        approved.team_plan
    )
    assert {
        resolution.agent_id: resolution.resolved_seconds
        for resolution in approved.approval.timeout_resolutions
    } == {agent.id: agent.timeout_seconds for agent in approved.team_plan.agents}
    session = store.load_session(request().run_id)
    assert session.status is PlanningSessionStatus.APPROVED
    assert session.turn_count == 3
    assert session.latest_proposal_revision == 3
    assert session.approved_revision == 3
    assert len(executor.requests) == 3
    assert all(call.role.value == "clarifier" for call in executor.requests)
    assert all(call.timeout_seconds == 180 for call in executor.requests)
    assert "unqualified prohibition" in executor.requests[0].prompt
    assert "top-level input" in executor.requests[0].prompt
    assert "`review_boundaries`" in executor.requests[0].prompt
    compact_prompt = " ".join(executor.requests[0].prompt.split())
    assert "do not repeat their definitions" in compact_prompt
    assert "A task may reference a listed profile" in compact_prompt
    assert "`tasks` array describes work assigned" in compact_prompt
    assert "testing or review Agent may own tasks" in compact_prompt
    assert "do not create an Agent, grant write access" in compact_prompt
    assert "Reviewer may depend on a Tester" in compact_prompt
    assert "does not impose a hidden peer-only quality topology" in compact_prompt
    assert "assign every task that creates or modifies project code" in compact_prompt
    assert "quality-owned task may describe only inspection" in compact_prompt
    assert "protocol identifiers, not informal descriptions of depth" in compact_prompt
    assert "do not repeat, paraphrase, shorten, or broaden" in compact_prompt
    assert "establish a product-depth contract" in compact_prompt
    assert "Never silently choose those three" in compact_prompt
    assert "decorative prose" in compact_prompt
    planning_context_text = (
        executor.requests[0]
        .prompt.split("PLANNING_CONTEXT_JSON\n", 1)[1]
        .split("\n\nRESPONSE_SCHEMA_JSON", 1)[0]
    )
    planning_context = json.loads(planning_context_text)
    assert set(planning_context["request"]) == {
        "project_name",
        "source_request",
        "execution_profile",
        "base_constraints",
    }
    assert "authorization" not in planning_context_text
    assert "authorized_at" not in planning_context_text
    boundary_definitions = planning_context["controller_policy"][
        "review_boundary_definitions"
    ]
    assert (
        "root itself is the top-level input" in boundary_definitions["top_level_input"]
    )
    assert (
        "Immediate children and deeper descendants"
        in boundary_definitions["nested_input"]
    )
    schema_text = (
        executor.requests[0]
        .prompt.split(
            "RESPONSE_SCHEMA_JSON\n",
            1,
        )[1]
        .split("\n\nREPAIR_CONTEXT_JSON", 1)[0]
    )
    response_schema = json.loads(schema_text)
    question_schema = response_schema["$defs"]["PlanningQuestion"]
    option_schema = response_schema["$defs"]["PlanningOption"]
    criterion_schema = response_schema["$defs"]["ProposedCriterion"]
    agent_schema = response_schema["$defs"]["ProposedAgent"]
    proposal_schema = response_schema["$defs"]["PlanningProposalBody"]
    assert {
        "decision_category",
        "missing_evidence",
        "material_consequences",
        "product_definition_dimensions",
    }.issubset(question_schema["required"])
    assert question_schema["properties"]["decision_category"] == {
        "$ref": "#/$defs/PlanningDecisionCategory"
    }
    assert "decision_owner" not in question_schema["properties"]
    assert "decision_owner" not in question_schema["required"]
    assert question_schema["properties"]["product_definition_dimensions"][
        "maxItems"
    ] == len(ProductDefinitionDimension)
    assert "product_definition_values" in option_schema["required"]
    assert option_schema["properties"]["product_definition_values"]["maxItems"] == len(
        ProductDefinitionDimension
    )
    assert {"requirement_ids", "verification_agent_ids"}.issubset(
        criterion_schema["required"]
    )
    assert "review_boundaries" in criterion_schema["required"]
    assert "default" not in criterion_schema["properties"]["review_boundaries"]
    assert "specialization" in agent_schema["required"]
    catalog = planning_context["controller_policy"]["specialization_catalog"]
    assert planning_context["controller_policy"]["specialization_catalog_version"] == 1
    assert {entry["id"] for entry in catalog} == {
        "product_implementation",
        "system_integration",
        "deterministic_testing",
        "general_review",
        "security_assessment",
        "experience_assessment",
    }
    security_contract = next(
        entry for entry in catalog if entry["id"] == "security_assessment"
    )
    assert security_contract["compatible_capabilities"] == ["review"]
    assert security_contract["permission_ceiling"] == "read_only"
    assert security_contract["expected_output"] == "security_assessment"
    assert security_contract["prompt_module"] == (
        "specialization_security_assessment.md"
    )
    assert {
        "product_definition",
        "non_goals",
        "decisions",
    }.issubset(proposal_schema["required"])
    assert "requirement_ids" not in proposal_schema["properties"]
    assert "requirement_ids" not in proposal_schema["required"]
    assert "assumption_decision_ids" not in proposal_schema["properties"]
    assert "assumption_decision_ids" not in proposal_schema["required"]
    requirement_schema = response_schema["$defs"]["ProposedRequirement"]
    assert proposal_schema["properties"]["requirements"]["items"] == {
        "$ref": "#/$defs/ProposedRequirement"
    }
    assert requirement_schema["additionalProperties"] is False
    assert set(requirement_schema["required"]) == {"id", "description"}
    assumption_schema = response_schema["$defs"]["ProposedAssumption"]
    assert proposal_schema["properties"]["assumptions"]["items"] == {
        "$ref": "#/$defs/ProposedAssumption"
    }
    assert assumption_schema["additionalProperties"] is False
    assert set(assumption_schema["required"]) == {"statement", "decision_id"}
    assert proposal_schema["properties"]["product_definition"] == {
        "$ref": "#/$defs/ProductDefinition"
    }

    tampered = approved.model_dump(mode="json")
    tampered["team_plan"]["agents"][0]["timeout_seconds"] += 1
    with pytest.raises(ValidationError, match="does not bind the supplied TeamPlan"):
        ApprovedPlanningResult.model_validate(tampered)

    tampered_resolution = approved.model_dump(mode="json")
    resolution = tampered_resolution["approval"]["timeout_resolutions"][0]
    resolution["source"] = "user_override"
    resolution["resolved_seconds"] = (
        resolution["default_seconds"]
        if resolution["resolved_seconds"] != resolution["default_seconds"]
        else resolution["ceiling_seconds"]
    )
    with pytest.raises(ValidationError, match="do not match the TeamPlan"):
        ApprovedPlanningResult.model_validate(tampered_resolution)


def test_store_loads_approval_written_before_scope_timeout_evidence(
    tmp_path: Path,
) -> None:
    store = PlanningStore(tmp_path / "planning")
    coordinator = AdaptivePlanningCoordinator(
        executor=ScriptedAgentExecutor([response(proposal_response())]),
        store=store,
        policy=policy(),
        clock=AdvancingClock(),
    )
    created = coordinator.start(
        request(),
        answer_question=lambda _question: pytest.fail("unexpected question"),
    )
    assert created is not None
    coordinator.approve(request(), created)

    approval_path = tmp_path / "planning" / request().run_id / "approvals" / "001.json"
    payload = json.loads(approval_path.read_text(encoding="utf-8"))
    for resolution in payload["timeout_resolutions"]:
        resolution.pop("minimum_seconds")
        resolution.pop("scope_criterion_count")
        resolution.pop("scope_boundary_obligation_count")
    approval_path.write_text(json.dumps(payload), encoding="utf-8")

    session = store.load_session(request().run_id)
    loaded = store.load_approval(request().run_id, 1)

    assert session.status is PlanningSessionStatus.APPROVED
    assert all(item.minimum_seconds is None for item in loaded.timeout_resolutions)
    assert all(
        item.scope_criterion_count is None for item in loaded.timeout_resolutions
    )


def test_invalid_complete_proposal_is_repaired_before_it_is_shown(
    tmp_path: Path,
) -> None:
    valid_payload = proposal_response().model_dump(mode="json")
    invalid_payload = proposal_response().model_dump(mode="json")
    invalid_payload["proposal"]["tasks"][0]["owner_agent_id"] = "absent_agent"
    correction = json.loads(
        correction_response(
            invalid_payload,
            {
                "/proposal/tasks/0/owner_agent_id": valid_payload["proposal"]["tasks"][
                    0
                ]["owner_agent_id"],
            },
        )
    )
    executor = ScriptedAgentExecutor(
        [
            json.dumps(invalid_payload),
            ScriptedAgentResponse(
                text=(
                    '{"kind"="semantic_correction_v2","base_response_sha256"=unquoted}'
                ),
                submission_payload=correction,
            ),
        ]
    )
    store = PlanningStore(tmp_path / "planning")
    coordinator = AdaptivePlanningCoordinator(
        executor=executor,
        store=store,
        policy=policy(response_repair_limit=1),
        clock=AdvancingClock(),
    )
    activities: list[PlanningActivity] = []

    created = coordinator.start(
        request(),
        answer_question=lambda _question: pytest.fail("unexpected question"),
        activity_handler=activities.append,
    )

    assert created is not None
    assert store.load_session(request().run_id).turn_count == 2
    rejected = store.load_turn(request().run_id, 1)
    assert rejected.parsed_response is None
    assert rejected.validation_error is not None
    assert "tasks reference unknown Agent owners: absent_agent" in (
        rejected.validation_error
    )
    assert (
        executor.requests[0].submission_contract is not None
        and executor.requests[0].submission_contract.purpose
        is AgentSubmissionPurpose.PLANNING_RESPONSE
    )
    assert "Call `sat_submit_artifact` exactly once" in executor.requests[0].prompt
    assert (
        executor.requests[1].submission_contract is not None
        and executor.requests[1].submission_contract.purpose
        is AgentSubmissionPurpose.SEMANTIC_CORRECTION
    )
    correction_schema = executor.requests[1].submission_contract.parameters_schema()
    assert set(correction_schema["properties"]) == {"replacements"}
    replacement_schema = correction_schema["properties"]["replacements"]
    replacement_variant = replacement_schema["items"]["oneOf"][0]
    assert replacement_variant["properties"]["slot_handle"]["const"].startswith("slot_")
    assert replacement_variant["properties"]["replacement_value"]["type"] == ("string")
    assert replacement_variant["properties"]["replacement_value"]["enum"] == [
        agent["id"] for agent in invalid_payload["proposal"]["agents"]
    ]
    assert executor.requests[1].submission_contract.transport_payload_schema() == {
        "type": "object",
        "additionalProperties": True,
    }
    assert "TARGETED_SEMANTIC_CORRECTION_SLOTS_V3" in executor.requests[1].prompt
    assert "Do not regenerate or repeat that object" in executor.requests[1].prompt
    assert (
        "Return only the supplied short request-local slot IDs"
        in executor.requests[1].prompt
    )
    assert rejected.response_validation is not None
    assert rejected.response_validation.correction_paths == (
        "/proposal/tasks/0/owner_agent_id",
    )
    corrected = store.load_turn(request().run_id, 2)
    assert rejected.submission_payload == invalid_payload
    assert rejected.submission_evidence is not None
    assert corrected.submission_payload == correction
    assert corrected.submission_evidence is not None
    assert corrected.response_text == (
        '{"kind"="semantic_correction_v2","base_response_sha256"=unquoted}'
    )
    assert corrected.semantic_correction_request is not None
    assert corrected.semantic_correction_outcome == "accepted"
    invocation = [
        PlanningActivityKind.WAITING_MODEL,
        PlanningActivityKind.INVOCATION_LAUNCHED,
        PlanningActivityKind.INITIALIZING,
        PlanningActivityKind.PROVIDER_WAIT,
        PlanningActivityKind.STOPPING,
        PlanningActivityKind.COLLECTING_EVIDENCE,
        PlanningActivityKind.STOPPED,
        PlanningActivityKind.RESPONSE_RECEIVED,
    ]
    assert [activity.kind for activity in activities] == [
        *invocation,
        PlanningActivityKind.CORRECTION_SCHEDULED,
        *invocation,
        PlanningActivityKind.RESPONSE_VALIDATED,
    ]
    assert [
        (activity.attempt, activity.maximum_attempts) for activity in activities
    ] == [(1, 2)] * 9 + [(2, 2)] * 9


def test_missing_product_decision_returns_to_atomic_clarification(
    tmp_path: Path,
) -> None:
    source_request = (
        "Build a tool that checks Markdown links in files and fragments without "
        "fetching remote URLs."
    )
    target_question_id = "target_users"
    maturity_question_id = "delivery_maturity"
    target_question = PlanningModelResponse(
        kind=PlanningResponseKind.QUESTION,
        question=PlanningQuestion(
            id=target_question_id,
            text="Who will use this link checker?",
            why="The audience changes usability and delivery expectations.",
            decision_category=PlanningDecisionCategory.PRODUCT_REQUIREMENT,
            decision_owner=PlanningDecisionAuthority.USER,
            missing_evidence=("The request does not name an audience.",),
            material_consequences=("Audience changes the product quality bar.",),
            product_definition_dimensions=(ProductDefinitionDimension.TARGET_USERS,),
            options=(
                PlanningOption(
                    id="developers",
                    label="Developers",
                    description="Developers use it repeatedly.",
                    product_definition_values=(
                        PlanningOptionValue(
                            dimension=ProductDefinitionDimension.TARGET_USERS,
                            value="Developers",
                        ),
                    ),
                ),
                PlanningOption(
                    id="maintainers",
                    label="Maintainers",
                    description="Repository maintainers use it in reviews.",
                    product_definition_values=(
                        PlanningOptionValue(
                            dimension=ProductDefinitionDimension.TARGET_USERS,
                            value="Repository maintainers",
                        ),
                    ),
                ),
            ),
            allow_custom=True,
        ),
    )
    maturity_question = PlanningModelResponse(
        kind=PlanningResponseKind.QUESTION,
        question=PlanningQuestion(
            id=maturity_question_id,
            text="How mature should the delivered tool be?",
            why="Maturity changes packaging, verification, and documentation.",
            decision_category=PlanningDecisionCategory.PRODUCT_REQUIREMENT,
            decision_owner=PlanningDecisionAuthority.USER,
            missing_evidence=("The request does not state delivery maturity.",),
            material_consequences=("Maturity changes the delivery quality bar.",),
            product_definition_dimensions=(
                ProductDefinitionDimension.DELIVERY_MATURITY,
            ),
            options=(
                PlanningOption(
                    id="throwaway",
                    label="Throwaway prototype",
                    description="Prove the workflow once with minimal polish.",
                    product_definition_values=(
                        PlanningOptionValue(
                            dimension=ProductDefinitionDimension.DELIVERY_MATURITY,
                            value=DeliveryMaturity.THROWAWAY_PROTOTYPE.value,
                        ),
                    ),
                ),
                PlanningOption(
                    id="usable",
                    label="Usable local product",
                    description="Support repeated local use with tests and docs.",
                    product_definition_values=(
                        PlanningOptionValue(
                            dimension=ProductDefinitionDimension.DELIVERY_MATURITY,
                            value=DeliveryMaturity.USABLE_LOCAL_PRODUCT.value,
                        ),
                    ),
                ),
                PlanningOption(
                    id="releasable",
                    label="Releasable small product",
                    description="Add packaging and distribution readiness.",
                    product_definition_values=(
                        PlanningOptionValue(
                            dimension=ProductDefinitionDimension.DELIVERY_MATURITY,
                            value=DeliveryMaturity.RELEASABLE_SMALL_PRODUCT.value,
                        ),
                    ),
                ),
            ),
            allow_custom=True,
        ),
    )

    initial = proposal_body(question_id=target_question_id)
    initial_definition = initial.product_definition
    assert initial_definition is not None
    invalid = initial.model_copy(
        update={
            "product_definition": initial_definition.model_copy(
                update={
                    "target_users": initial_definition.target_users.model_copy(
                        update={
                            "statement": "Operators",
                            "disposition": (
                                ProductDefinitionDisposition.RESOLVED_QUESTION
                            ),
                            "source": target_question_id,
                            "decision_ids": ("DECISION_LINK_SCOPE_ANSWER",),
                        }
                    ),
                    "delivery_maturity": (
                        initial_definition.delivery_maturity.model_copy(
                            update={
                                "disposition": (
                                    ProductDefinitionDisposition.PLANNER_RECOMMENDATION
                                ),
                                "source": "planner",
                                "decision_ids": ("DECISION_DELIVERY",),
                            }
                        )
                    ),
                }
            )
        }
    )
    maturity_decision = PlanningDecisionRecord(
        id="DECISION_MATURITY_ANSWER",
        category=PlanningDecisionCategory.PRODUCT_REQUIREMENT,
        authority=PlanningDecisionAuthority.USER,
        provenance=PlanningDecisionProvenance(
            kind=PlanningDecisionProvenanceKind.RESOLVED_QUESTION,
            source=maturity_question_id,
        ),
        summary="Usable local product",
        rationale="The user selected the reusable local delivery quality bar.",
    )
    final_definition = initial_definition.model_copy(
        update={
            "target_users": initial_definition.target_users.model_copy(
                update={
                    "statement": "Developers",
                    "disposition": ProductDefinitionDisposition.RESOLVED_QUESTION,
                    "source": target_question_id,
                    "decision_ids": ("DECISION_LINK_SCOPE_ANSWER",),
                }
            ),
            "delivery_maturity": initial_definition.delivery_maturity.model_copy(
                update={
                    "level": DeliveryMaturity.USABLE_LOCAL_PRODUCT,
                    "disposition": ProductDefinitionDisposition.RESOLVED_QUESTION,
                    "source": maturity_question_id,
                    "decision_ids": ("DECISION_MATURITY_ANSWER",),
                }
            ),
        }
    )
    final = initial.model_copy(
        update={
            "product_definition": final_definition,
            "decisions": (*initial.decisions, maturity_decision),
        }
    )
    executor = ScriptedAgentExecutor(
        [
            response(target_question),
            response(
                PlanningModelResponse(
                    kind=PlanningResponseKind.PROPOSAL,
                    proposal=invalid,
                )
            ),
            response(maturity_question),
            response(
                PlanningModelResponse(
                    kind=PlanningResponseKind.PROPOSAL,
                    proposal=final,
                )
            ),
        ]
    )
    store = PlanningStore(tmp_path / "planning")
    coordinator = AdaptivePlanningCoordinator(
        executor=executor,
        store=store,
        policy=policy(response_repair_limit=0),
        clock=AdvancingClock(),
    )
    answered: list[tuple[str, PlanningQuestionOrigin]] = []
    activities: list[PlanningActivity] = []
    clarification_output: list[str] = []
    clarification_choices = iter(("1", "2"))
    interactive_answer = planning._interactive_question_answerer(
        read=lambda _prompt: next(clarification_choices),
        write=clarification_output.append,
    )

    def answer(
        question: PresentedPlanningQuestion,
    ) -> PlanningQuestionAnswer | None:
        answered.append((question.id, question.admission.origin))
        return interactive_answer(question)

    created = coordinator.start(
        request(source_request=source_request),
        answer_question=answer,
        activity_handler=activities.append,
    )

    assert created is not None
    assert created.body == final
    assert answered == [
        (target_question_id, PlanningQuestionOrigin.PLANNER_SUGGESTION),
        (maturity_question_id, PlanningQuestionOrigin.CONTROLLER_REQUIREMENT),
    ]
    rendered_clarification = "\n".join(clarification_output)
    assert "Question source: Planner-selected clarification for this task" in (
        rendered_clarification
    )
    assert "Question source: Controller validation requires this user-owned" in (
        rendered_clarification
    )
    assert "Controller invariants: planning_product_user_decision_required" in (
        rendered_clarification
    )
    assert len(executor.requests) == 4
    invalid_turn = store.load_turn(request().run_id, 2)
    assert invalid_turn.response_validation is not None
    assert invalid_turn.response_validation.failure_class is (
        ResponseFailureClass.MISSING_USER_DECISION
    )
    assert invalid_turn.response_validation.correction_paths == ()
    assert {
        (issue.invariant_id, issue.authority)
        for issue in invalid_turn.response_validation.issues
    } == {
        (
            "planning_product_user_decision_required",
            ResponseIssueAuthority.USER,
        ),
    }
    recovery_contract = executor.requests[2].submission_contract
    assert recovery_contract is not None
    assert recovery_contract.purpose is AgentSubmissionPurpose.PLANNING_RESPONSE
    recovery_schema = recovery_contract.parameters_schema()
    assert recovery_schema["properties"]["kind"]["const"] == "question"
    assert "proposal" not in recovery_schema["properties"]
    question_definition = recovery_schema["$defs"]["PlanningQuestion"]
    assert question_definition["properties"]["product_definition_dimensions"] == {
        "items": {"const": "delivery_maturity", "type": "string"},
        "maxItems": 1,
        "minItems": 1,
        "type": "array",
    }
    option_definition = recovery_schema["$defs"]["PlanningOption"]
    assert option_definition["properties"]["product_definition_values"]["minItems"] == 1
    assert option_definition["properties"]["product_definition_values"]["maxItems"] == 1
    assert recovery_schema["$defs"]["PlanningOptionValue"]["properties"][
        "dimension"
    ] == {"const": "delivery_maturity", "type": "string"}
    target_admission = store.load_turn(request().run_id, 1).question_admission
    assert target_admission is not None
    assert target_admission.origin is PlanningQuestionOrigin.PLANNER_SUGGESTION
    maturity_admission = store.load_turn(request().run_id, 3).question_admission
    assert maturity_admission is not None
    assert maturity_admission.origin is PlanningQuestionOrigin.CONTROLLER_REQUIREMENT
    assert maturity_admission.controller_invariant_ids == (
        "planning_product_user_decision_required",
    )
    assert '"required_clarification"' in executor.requests[2].prompt
    assert '"delivery_maturity"' in executor.requests[2].prompt
    clarification = [
        activity
        for activity in activities
        if activity.kind is PlanningActivityKind.CLARIFICATION_SCHEDULED
    ]
    assert len(clarification) == 1
    assert clarification[0].clarification_dimension is (
        ProductDefinitionDimension.DELIVERY_MATURITY
    )
    output: list[str] = []
    TerminalPlanningProgress(write=output.append)(clarification[0])
    assert output == [
        "↻ Planning proposal needs a user decision; requesting one focused "
        "delivery_maturity question"
    ]


def test_product_decision_correction_can_repair_the_shared_decision_relation(
    tmp_path: Path,
) -> None:
    source_request = (
        "Build a tool that checks Markdown links in files and fragments without "
        "fetching remote URLs."
    )
    target_response = product_intent_question_response()
    target_question = target_response.question
    assert target_question is not None
    maturity_question_id = "delivery_maturity"
    maturity_response = PlanningModelResponse(
        kind=PlanningResponseKind.QUESTION,
        question=PlanningQuestion(
            id=maturity_question_id,
            text="How mature should the delivered tool be?",
            why="Maturity changes packaging, verification, and documentation.",
            decision_category=PlanningDecisionCategory.PRODUCT_REQUIREMENT,
            decision_owner=PlanningDecisionAuthority.USER,
            missing_evidence=("The request does not state delivery maturity.",),
            material_consequences=("Maturity changes the delivery quality bar.",),
            product_definition_dimensions=(
                ProductDefinitionDimension.DELIVERY_MATURITY,
            ),
            options=(
                PlanningOption(
                    id="throwaway",
                    label="Throwaway prototype",
                    description="Prove the workflow once with minimal polish.",
                    product_definition_values=(
                        PlanningOptionValue(
                            dimension=ProductDefinitionDimension.DELIVERY_MATURITY,
                            value=DeliveryMaturity.THROWAWAY_PROTOTYPE.value,
                        ),
                    ),
                ),
                PlanningOption(
                    id="usable",
                    label="Usable local product",
                    description="Support repeated local use with tests and docs.",
                    product_definition_values=(
                        PlanningOptionValue(
                            dimension=ProductDefinitionDimension.DELIVERY_MATURITY,
                            value=DeliveryMaturity.USABLE_LOCAL_PRODUCT.value,
                        ),
                    ),
                ),
            ),
            allow_custom=True,
        ),
    )

    body = proposal_body(question_id=target_question.id)
    definition = body.product_definition
    assert definition is not None
    target_answer = (
        "Developers and researchers use the reports for local Markdown collections."
    )
    maturity_answer = "Usable local product with tests and docs."
    maturity_decision = PlanningDecisionRecord(
        id="DECISION_MATURITY_ANSWER",
        category=PlanningDecisionCategory.PRODUCT_REQUIREMENT,
        authority=PlanningDecisionAuthority.USER,
        provenance=PlanningDecisionProvenance(
            kind=PlanningDecisionProvenanceKind.RESOLVED_QUESTION,
            source=maturity_question_id,
        ),
        summary="Usable local product",
        rationale="The user selected the reusable local delivery quality bar.",
    )
    valid_definition = definition.model_copy(
        update={
            "target_users": definition.target_users.model_copy(
                update={
                    "statement": target_answer,
                    "disposition": ProductDefinitionDisposition.RESOLVED_QUESTION,
                    "source": target_question.id,
                    "decision_ids": ("DECISION_LINK_SCOPE_ANSWER",),
                }
            ),
            "delivery_maturity": definition.delivery_maturity.model_copy(
                update={
                    "level": DeliveryMaturity.USABLE_LOCAL_PRODUCT,
                    "disposition": ProductDefinitionDisposition.RESOLVED_QUESTION,
                    "source": maturity_question_id,
                    "decision_ids": ("DECISION_MATURITY_ANSWER",),
                }
            ),
        }
    )
    valid = body.model_copy(
        update={
            "product_definition": valid_definition,
            "decisions": (*body.decisions, maturity_decision),
        }
    )
    invalid_definition = valid_definition.model_copy(
        update={
            dimension.value: item.model_copy(update={"decision_ids": ()})
            for dimension, item in valid_definition.dimensions()
            if dimension
            in {
                ProductDefinitionDimension.TARGET_USERS,
                ProductDefinitionDimension.DELIVERY_MATURITY,
                ProductDefinitionDimension.USABILITY_EXPECTATIONS,
                ProductDefinitionDimension.OPERATIONAL_EXPECTATIONS,
                ProductDefinitionDimension.DELIVERY_EXPECTATIONS,
            }
        }
    )
    invalid = valid.model_copy(
        update={
            "product_definition": invalid_definition,
            "decisions": tuple(
                decision
                for decision in valid.decisions
                if decision.authority is PlanningDecisionAuthority.AGENT_AUTONOMY
            ),
        }
    )
    invalid_response = PlanningModelResponse(
        kind=PlanningResponseKind.PROPOSAL,
        proposal=invalid,
    )
    valid_response = PlanningModelResponse(
        kind=PlanningResponseKind.PROPOSAL,
        proposal=valid,
    )
    invalid_payload = json.loads(response(invalid_response))
    correction_base, _ = planning._normalize_planning_response_payload(
        invalid_payload,
        profile_criterion_ids=(
            criterion.id for criterion in policy().profile_acceptance_criteria
        ),
        user_inputs=(source_request,),
        question_answers={
            target_question.id: target_answer,
            maturity_question_id: maturity_answer,
        },
    )
    valid_payload = json.loads(response(valid_response))
    valid_proposal_payload = valid_payload["proposal"]
    assert isinstance(valid_proposal_payload, dict)
    corrected_paths = {
        "/proposal/decisions",
        *(
            f"/proposal/product_definition/{dimension.value}"
            for dimension in (
                ProductDefinitionDimension.TARGET_USERS,
                ProductDefinitionDimension.DELIVERY_MATURITY,
                ProductDefinitionDimension.USABILITY_EXPECTATIONS,
                ProductDefinitionDimension.OPERATIONAL_EXPECTATIONS,
                ProductDefinitionDimension.DELIVERY_EXPECTATIONS,
            )
        ),
    }
    replacements = {
        path: (
            valid_proposal_payload["decisions"]
            if path == "/proposal/decisions"
            else valid_proposal_payload["product_definition"][path.rsplit("/", 1)[1]]
        )
        for path in corrected_paths
    }
    executor = ScriptedAgentExecutor(
        [
            response(target_response),
            response(maturity_response),
            response(invalid_response),
            correction_response(correction_base, replacements),
        ]
    )
    store = PlanningStore(tmp_path / "planning")
    coordinator = AdaptivePlanningCoordinator(
        executor=executor,
        store=store,
        policy=policy(response_repair_limit=1),
        clock=AdvancingClock(),
    )

    answers = {
        target_question.id: target_answer,
        maturity_question_id: maturity_answer,
    }
    created = coordinator.start(
        request(source_request=source_request),
        answer_question=lambda question: answers[question.id],
    )

    assert created is not None
    assert created.body == valid
    rejected = store.load_turn(request().run_id, 3)
    assert rejected.response_validation is not None
    assert set(rejected.response_validation.correction_paths) == corrected_paths
    assert {issue.invariant_id for issue in rejected.response_validation.issues} == {
        "planning_product_question_source",
        "planning_product_recommendation_source",
    }
    correction_contract = executor.requests[3].submission_contract
    assert correction_contract is not None
    replacement_variants = correction_contract.parameters_schema()["properties"][
        "replacements"
    ]["items"]["oneOf"]
    product_replacements = [
        variant["properties"]["replacement_value"]
        for variant in replacement_variants
        if isinstance(
            variant["properties"]["replacement_value"].get("properties"),
            dict,
        )
        and "decision_ids" in variant["properties"]["replacement_value"]["properties"]
    ]
    assert product_replacements
    assert all(
        "enum" not in replacement["properties"]["decision_ids"]["items"]
        for replacement in product_replacements
    )
    corrected = store.load_turn(request().run_id, 4)
    assert corrected.semantic_correction_outcome == "accepted"


def test_planning_projects_response_finalization_as_a_distinct_phase() -> None:
    activities: list[PlanningActivity] = []
    for activity in (
        AgentExecutionActivity(
            kind=AgentExecutionActivityKind.INVOCATION_FINALIZING_RESPONSE,
            agent_id="planner",
            session_key="agent:planner:finalizing",
            model="provider/model",
            elapsed_ms=100,
            invocation_phase=InvocationPhase.FINALIZING_RESPONSE,
            action="Terminal response observed",
        ),
        AgentExecutionActivity(
            kind=AgentExecutionActivityKind.FINALIZATION_STALL_SUSPECTED,
            agent_id="planner",
            session_key="agent:planner:finalizing",
            model="provider/model",
            elapsed_ms=50_000,
            inactivity_ms=50_000,
            silence_seconds=60,
            stall_grace_seconds=10,
            policy_source="test response-finalization contract",
        ),
    ):
        AdaptivePlanningCoordinator._emit_execution_activity(
            activities.append,
            activity,
            attempt=1,
            maximum_attempts=2,
            model="provider/model",
        )

    assert [activity.kind for activity in activities] == [
        PlanningActivityKind.FINALIZING_RESPONSE,
        PlanningActivityKind.FINALIZATION_STALL_SUSPECTED,
    ]
    assert activities[0].invocation_phase is InvocationPhase.FINALIZING_RESPONSE
    assert activities[1].inactivity_ms == 50_000


def test_decision_identifier_case_is_normalized_without_a_model_call(
    tmp_path: Path,
) -> None:
    payload = proposal_response().model_dump(mode="json")
    for decision in payload["proposal"]["decisions"]:
        decision_id = decision["id"]
        decision["id"] = "DECISION_" + decision_id.removeprefix("DECISION_").lower()
    payload["proposal"]["assumption_decision_ids"] = ["DECISION_scan_structure"]
    executor = ScriptedAgentExecutor([json.dumps(payload)])
    store = PlanningStore(tmp_path / "planning")
    coordinator = AdaptivePlanningCoordinator(
        executor=executor,
        store=store,
        policy=policy(response_repair_limit=None),
        clock=AdvancingClock(),
    )

    created = coordinator.start(
        request(),
        answer_question=lambda _question: pytest.fail("unexpected question"),
    )

    assert created is not None
    assert len(executor.requests) == 1
    turn = store.load_turn(request().run_id, 1)
    assert turn.response_validation is None
    assert turn.response_normalizations is not None
    assert (
        len(turn.response_normalizations) == len(payload["proposal"]["decisions"]) + 1
    )
    assert turn.parsed_response is not None
    assert turn.parsed_response.proposal is not None
    assert turn.parsed_response.proposal.assumption_decision_ids == (
        "DECISION_SCAN_STRUCTURE",
    )


def test_product_planning_continues_only_when_targeted_correction_improves(
    tmp_path: Path,
) -> None:
    valid_payload = proposal_response().model_dump(mode="json")
    invalid_payload = proposal_response().model_dump(mode="json")
    invalid_payload["proposal"]["tasks"][0]["owner_agent_id"] = "absent_agent"
    invalid_payload["proposal"]["max_concurrency"] = 99
    first_corrected = json.loads(json.dumps(invalid_payload))
    first_corrected["proposal"]["tasks"][0]["owner_agent_id"] = valid_payload[
        "proposal"
    ]["tasks"][0]["owner_agent_id"]
    executor = ScriptedAgentExecutor(
        [
            json.dumps(invalid_payload),
            correction_response(
                invalid_payload,
                {
                    "/proposal/tasks/0/owner_agent_id": valid_payload["proposal"][
                        "tasks"
                    ][0]["owner_agent_id"]
                },
            ),
            correction_response(
                first_corrected,
                {
                    "/proposal/max_concurrency": valid_payload["proposal"][
                        "max_concurrency"
                    ]
                },
            ),
        ]
    )
    store = PlanningStore(tmp_path / "planning")
    coordinator = AdaptivePlanningCoordinator(
        executor=executor,
        store=store,
        policy=policy(response_repair_limit=None),
        clock=AdvancingClock(),
    )

    created = coordinator.start(
        request(),
        answer_question=lambda _question: pytest.fail("unexpected question"),
    )

    assert created is not None
    assert len(executor.requests) == 3
    assert store.load_turn(request().run_id, 2).semantic_correction_outcome == (
        "improved"
    )
    assert store.load_turn(request().run_id, 3).semantic_correction_outcome == (
        "accepted"
    )


def test_product_planning_distinguishes_new_relational_invariant_and_continues(
    tmp_path: Path,
) -> None:
    valid_payload = proposal_response().model_dump(mode="json")
    invalid_payload = json.loads(json.dumps(valid_payload))
    invalid_payload["proposal"]["tasks"][0]["acceptance_criteria"] = ["AC_REPORT"]
    invalid_payload["proposal"]["acceptance_criteria"][0]["verification_agent_ids"] = [
        "cli_developer",
        "acceptance_tester",
    ]

    first_corrected = json.loads(json.dumps(invalid_payload))
    first_corrected["proposal"]["tasks"] = valid_payload["proposal"]["tasks"]
    second_corrected = json.loads(json.dumps(first_corrected))
    second_corrected["proposal"]["acceptance_criteria"][0]["verification_agent_ids"] = (
        valid_payload["proposal"]["acceptance_criteria"][0]["verification_agent_ids"]
    )
    executor = ScriptedAgentExecutor(
        [
            json.dumps(invalid_payload),
            correction_response(
                invalid_payload,
                {
                    "/proposal/tasks/0/acceptance_criteria": (
                        valid_payload["proposal"]["tasks"][0]["acceptance_criteria"]
                    )
                },
            ),
            correction_response(
                first_corrected,
                {
                    "/proposal/acceptance_criteria/0/verification_agent_ids": (
                        valid_payload["proposal"]["acceptance_criteria"][0][
                            "verification_agent_ids"
                        ]
                    )
                },
            ),
        ]
    )
    store = PlanningStore(tmp_path / "planning")
    coordinator = AdaptivePlanningCoordinator(
        executor=executor,
        store=store,
        policy=policy(response_repair_limit=None),
        clock=AdvancingClock(),
    )

    created = coordinator.start(
        request(),
        answer_question=lambda _question: pytest.fail("unexpected question"),
    )

    assert created is not None
    assert len(executor.requests) == 3
    first = store.load_turn(request().run_id, 1)
    assert first.response_validation is not None
    assert first.response_validation.schema_version == 2
    assert first.response_validation.correction_paths == (
        "/proposal/tasks/0/acceptance_criteria",
    )
    assert first.response_validation.issues[0].invariant_id == (
        "planning_writer_criterion_coverage"
    )
    assert [
        item.model_dump(mode="json")
        for item in first.response_validation.issues[0].subjects
    ] == [{"kind": "criterion", "identifier": "AC_SCAN"}]
    contract = executor.requests[1].submission_contract
    assert contract is not None
    replacement = contract.parameters_schema()["properties"]["replacements"]["items"][
        "oneOf"
    ][0]["properties"]["replacement_value"]
    assert replacement["type"] == "array"
    assert replacement["items"]["enum"] == ["AC_SCAN", "AC_REPORT"]
    assert replacement["uniqueItems"] is True
    assert replacement["allOf"] == [{"contains": {"const": "AC_SCAN"}}]

    second = store.load_turn(request().run_id, 2)
    assert second.semantic_correction_outcome == "improved"
    assert second.response_validation is not None
    assert second.response_validation.correction_paths == (
        "/proposal/acceptance_criteria/0/verification_agent_ids",
    )
    assert second.response_validation.issues[0].invariant_id == (
        "planning_criterion_verifier_capability"
    )
    assert [
        item.model_dump(mode="json")
        for item in second.response_validation.issues[0].subjects
    ] == [
        {"kind": "agent", "identifier": "cli_developer"},
        {"kind": "criterion", "identifier": "AC_SCAN"},
    ]
    assert "planning_criterion_verifier_capability" in executor.requests[2].prompt
    assert store.load_turn(request().run_id, 3).semantic_correction_outcome == (
        "accepted"
    )


def test_writer_coverage_correction_preserves_multiple_writer_choice(
    tmp_path: Path,
) -> None:
    body = proposal_body()
    primary_writer = body.agents[0].model_copy(
        update={"workspace_scope": "repository/src"}
    )
    second_writer = ProposedAgent(
        id="docs_developer",
        label="Documentation Developer",
        responsibility="Implement the documented user workflow.",
        rationale="The separate documentation scope can proceed independently.",
        capability=AgentCapability.IMPLEMENTATION,
        specialization=AgentSpecialization.PRODUCT_IMPLEMENTATION,
        stage_id="implement",
        workspace_scope="repository/docs",
        workload=AgentWorkload.ROUTINE,
    )
    quality_agents = tuple(
        agent.model_copy(update={"dependencies": (primary_writer.id, second_writer.id)})
        for agent in body.agents[1:]
    )
    primary_task = body.tasks[0].model_copy(
        update={
            "acceptance_criteria": ("AC_SCAN",),
            "expected_paths": ("src",),
        }
    )
    secondary_task = ProposedTask(
        id="TASK_DOCUMENT",
        owner_agent_id=second_writer.id,
        description="Implement documentation for the reported result.",
        acceptance_criteria=("AC_SCAN",),
        expected_paths=("docs",),
    )
    invalid = body.model_copy(
        update={
            "agents": (primary_writer, second_writer, *quality_agents),
            "tasks": (primary_task, secondary_task),
        }
    )
    valid = invalid.model_copy(
        update={
            "tasks": (
                primary_task,
                secondary_task.model_copy(
                    update={"acceptance_criteria": ("AC_REPORT",)}
                ),
            )
        }
    )
    valid_payload = proposal_response(valid).model_dump(mode="json")
    invalid_payload = deepcopy(valid_payload)
    invalid_payload["proposal"]["tasks"] = [
        task.model_dump(mode="json") for task in invalid.tasks
    ]
    executor = ScriptedAgentExecutor(
        [
            json.dumps(invalid_payload),
            correction_response(
                invalid_payload,
                {"/proposal/tasks": valid_payload["proposal"]["tasks"]},
            ),
        ]
    )
    store = PlanningStore(tmp_path / "planning")
    coordinator = AdaptivePlanningCoordinator(
        executor=executor,
        store=store,
        policy=policy(response_repair_limit=None),
        clock=AdvancingClock(),
    )

    created = coordinator.start(
        request(),
        answer_question=lambda _question: pytest.fail("unexpected question"),
    )

    assert created is not None
    assert created.body == valid
    contract = executor.requests[1].submission_contract
    assert contract is not None
    replacement = contract.parameters_schema()["properties"]["replacements"]["items"][
        "oneOf"
    ][0]["properties"]["replacement_value"]
    assert [
        item["properties"]["owner_agent_id"]["const"]
        for item in replacement["prefixItems"]
    ] == [primary_writer.id, second_writer.id]
    assert replacement["allOf"][0]["contains"]["properties"]["owner_agent_id"][
        "enum"
    ] == [primary_writer.id, second_writer.id]
    assert replacement["allOf"][0]["contains"]["properties"]["acceptance_criteria"][
        "contains"
    ] == {"const": "AC_REPORT"}
    assert store.load_turn(request().run_id, 2).semantic_correction_outcome == (
        "accepted"
    )


def test_planning_exposes_review_boundary_siblings_after_prior_correction(
    tmp_path: Path,
) -> None:
    valid_payload = proposal_response().model_dump(mode="json")
    invalid_payload = json.loads(json.dumps(valid_payload))
    invalid_payload["proposal"]["product_definition"]["primary_workflow"].update(
        disposition="not_material",
        source="planner",
    )
    criteria = invalid_payload["proposal"]["acceptance_criteria"]
    criteria[0].update(
        description="Files that differ in content must not be reported as duplicates.",
        review_boundaries=[ReviewBoundaryKind.TOP_LEVEL_INPUT.value],
    )
    criteria[1].update(
        description=(
            "Content comparison must not hold every file content in memory at once."
        ),
        review_boundaries=[ReviewBoundaryKind.NESTED_INPUT.value],
    )
    first_corrected = json.loads(json.dumps(invalid_payload))
    first_corrected["proposal"]["product_definition"]["primary_workflow"] = (
        valid_payload["proposal"]["product_definition"]["primary_workflow"]
    )
    all_boundaries = [boundary.value for boundary in ReviewBoundaryKind]
    final_payload = json.loads(json.dumps(first_corrected))
    for criterion in final_payload["proposal"]["acceptance_criteria"][:2]:
        criterion["review_boundaries"] = all_boundaries
    specialized_payload = json.loads(json.dumps(final_payload))
    specialized_payload["proposal"]["agents"][2]["specialization"] = (
        AgentSpecialization.SECURITY_ASSESSMENT.value
    )
    specialized_payload["proposal"]["acceptance_criteria"][0][
        "verification_agent_ids"
    ].append("quality_reviewer")

    boundary_paths = (
        "/proposal/acceptance_criteria/0/review_boundaries",
        "/proposal/acceptance_criteria/1/review_boundaries",
    )
    executor = ScriptedAgentExecutor(
        [
            json.dumps(invalid_payload),
            correction_response(
                invalid_payload,
                {
                    "/proposal/product_definition/primary_workflow": (
                        valid_payload["proposal"]["product_definition"][
                            "primary_workflow"
                        ]
                    )
                },
            ),
            correction_response(
                first_corrected,
                {path: all_boundaries for path in boundary_paths},
            ),
            correction_response(
                final_payload,
                {
                    "/proposal/agents": specialized_payload["proposal"]["agents"],
                    "/proposal/acceptance_criteria/0/verification_agent_ids": (
                        specialized_payload["proposal"]["acceptance_criteria"][0][
                            "verification_agent_ids"
                        ]
                    ),
                },
                target_paths=(
                    "/proposal/acceptance_criteria/0/verification_agent_ids",
                    "/proposal/acceptance_criteria/1/verification_agent_ids",
                    "/proposal/agents",
                ),
            ),
        ]
    )
    store = PlanningStore(tmp_path / "planning")
    coordinator = AdaptivePlanningCoordinator(
        executor=executor,
        store=store,
        policy=policy(response_repair_limit=None),
        clock=AdvancingClock(),
    )

    created = coordinator.start(
        request(),
        answer_question=lambda _question: pytest.fail("unexpected question"),
    )

    assert created is not None
    expected = PlanningModelResponse.model_validate(specialized_payload).proposal
    assert created.body == expected
    assert len(executor.requests) == 4
    first = store.load_turn(request().run_id, 1)
    assert first.response_validation is not None
    assert first.response_validation.correction_paths == (
        "/proposal/product_definition/primary_workflow",
    )
    second = store.load_turn(request().run_id, 2)
    assert second.semantic_correction_outcome == "improved"
    assert second.response_validation is not None
    assert second.response_validation.correction_paths == boundary_paths
    assert {issue.invariant_id for issue in second.response_validation.issues} == {
        "planning_criterion_review_boundaries"
    }
    assert {
        subject.identifier
        for issue in second.response_validation.issues
        for subject in issue.subjects
    } == {criteria[0]["id"], criteria[1]["id"]}
    third = store.load_turn(request().run_id, 3)
    assert third.semantic_correction_outcome == "improved"
    assert third.response_validation is not None
    assert third.response_validation.correction_paths == (
        "/proposal/acceptance_criteria/0/verification_agent_ids",
        "/proposal/acceptance_criteria/1/verification_agent_ids",
        "/proposal/agents",
    )
    assert store.load_turn(request().run_id, 4).semantic_correction_outcome == (
        "accepted"
    )


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("verification_agent_ids", "cli_developer"),
        ("verification_agent_ids", "missing_verifier"),
        ("requirement_ids", "MISSING_REQUIREMENT"),
    ],
)
@pytest.mark.parametrize("invalid_count", [1, 2])
def test_planning_corrects_independent_criterion_failures_in_one_turn(
    tmp_path: Path, field: str, invalid_value: str, invalid_count: int
) -> None:
    valid_payload = proposal_response().model_dump(mode="json")
    invalid_payload = json.loads(json.dumps(valid_payload))
    criteria = invalid_payload["proposal"]["acceptance_criteria"]
    replacements = {}
    for index, criterion in enumerate(criteria[:invalid_count]):
        replacements[f"/proposal/acceptance_criteria/{index}/{field}"] = valid_payload[
            "proposal"
        ]["acceptance_criteria"][index][field]
        criterion[field] = [invalid_value]
    original = json.dumps(invalid_payload, sort_keys=True)
    executor = ScriptedAgentExecutor(
        [
            json.dumps(invalid_payload),
            correction_response(invalid_payload, dict(reversed(replacements.items()))),
        ]
    )
    store = PlanningStore(tmp_path / "planning")
    coordinator = AdaptivePlanningCoordinator(
        executor=executor,
        store=store,
        policy=policy(response_repair_limit=None),
        clock=AdvancingClock(),
    )

    created = coordinator.start(
        request(),
        answer_question=lambda _question: pytest.fail("unexpected question"),
    )

    assert created is not None
    assert len(executor.requests) == 2
    first = store.load_turn(request().run_id, 1)
    assert first.response_validation is not None
    assert first.response_validation.correction_paths == tuple(sorted(replacements))
    if field == "verification_agent_ids":
        assert len(first.response_validation.issues) == invalid_count
        assert {
            subject.identifier
            for issue in first.response_validation.issues
            for subject in issue.subjects
            if subject.kind.value == "criterion"
        } == {criterion["id"] for criterion in criteria[:invalid_count]}
        contract = executor.requests[1].submission_contract
        assert contract is not None
        variants = contract.parameters_schema()["properties"]["replacements"]["items"][
            "oneOf"
        ]
        expected_agents = [
            agent["id"] for agent in invalid_payload["proposal"]["agents"]
        ]
        assert all(
            variant["properties"]["replacement_value"]["items"]["enum"]
            == expected_agents
            for variant in variants
        )
    else:
        # Requirement normalization/schema checks already aggregate these
        # failures before the semantic relation validator is reached.
        assert first.response_validation.failure_class is (
            ResponseFailureClass.SEMANTIC_SCHEMA
        )
    final = store.load_turn(request().run_id, 2)
    assert final.semantic_correction_outcome == "accepted"
    assert final.parsed_response is not None
    assert final.parsed_response.proposal == proposal_response().proposal
    assert json.dumps(invalid_payload, sort_keys=True) == original


def test_planning_accepts_partial_sibling_correction_then_narrows_to_remainder(
    tmp_path: Path,
) -> None:
    valid_payload = proposal_response().model_dump(mode="json")
    invalid_payload = json.loads(json.dumps(valid_payload))
    criteria = invalid_payload["proposal"]["acceptance_criteria"]
    correction_paths = (
        "/proposal/acceptance_criteria/0/verification_agent_ids",
        "/proposal/acceptance_criteria/1/verification_agent_ids",
    )
    for criterion in criteria[:2]:
        criterion["verification_agent_ids"] = ["cli_developer"]
    correction_base, _ = planning._normalize_planning_response_payload(
        invalid_payload,
        profile_criterion_ids=(
            criterion.id for criterion in policy().profile_acceptance_criteria
        ),
        user_inputs=(request().source_request,),
    )
    partial_payload = json.loads(json.dumps(correction_base))
    partial_payload["proposal"]["acceptance_criteria"][0]["verification_agent_ids"] = (
        valid_payload["proposal"]["acceptance_criteria"][0]["verification_agent_ids"]
    )
    executor = ScriptedAgentExecutor(
        [
            json.dumps(invalid_payload),
            correction_response(
                correction_base,
                {
                    correction_paths[0]: valid_payload["proposal"][
                        "acceptance_criteria"
                    ][0]["verification_agent_ids"]
                },
            ),
            correction_response(
                partial_payload,
                {
                    correction_paths[1]: valid_payload["proposal"][
                        "acceptance_criteria"
                    ][1]["verification_agent_ids"]
                },
            ),
        ]
    )
    store = PlanningStore(tmp_path / "planning")
    coordinator = AdaptivePlanningCoordinator(
        executor=executor,
        store=store,
        policy=policy(response_repair_limit=None),
        clock=AdvancingClock(),
    )

    created = coordinator.start(
        request(),
        answer_question=lambda _question: pytest.fail("unexpected question"),
    )

    assert created is not None
    assert len(executor.requests) == 3
    first_contract = executor.requests[1].submission_contract
    assert first_contract is not None
    first_replacements = first_contract.parameters_schema()["properties"][
        "replacements"
    ]
    assert first_replacements["minItems"] == 1
    assert first_replacements["maxItems"] == 2
    first_correction = store.load_turn(request().run_id, 2)
    assert first_correction.semantic_correction_outcome == "improved"
    assert first_correction.response_validation is not None
    assert first_correction.response_validation.correction_paths == (
        correction_paths[1],
    )
    final = store.load_turn(request().run_id, 3)
    assert final.semantic_correction_outcome == "accepted"
    assert final.parsed_response is not None
    assert final.parsed_response.proposal == proposal_response().proposal


def test_product_planning_preserves_normalization_and_targets_new_root_cause(
    tmp_path: Path,
) -> None:
    valid_payload = proposal_response().model_dump(mode="json")
    invalid_payload = json.loads(json.dumps(valid_payload))
    requirement_ids = invalid_payload["proposal"].pop("requirement_ids")
    invalid_payload["proposal"]["requirent_ids"] = requirement_ids
    invalid_payload["proposal"]["acceptance_criteria"][0]["requirement_ids"] = [
        "REQ_scan"
    ]

    first_base = json.loads(json.dumps(invalid_payload))
    first_base["proposal"].pop("requirent_ids")
    second_base = json.loads(json.dumps(first_base))
    second_base["proposal"]["acceptance_criteria"][0]["requirement_ids"] = ["REQ_SCAN"]
    executor = ScriptedAgentExecutor(
        [
            json.dumps(invalid_payload),
            correction_response(
                first_base,
                {"/proposal/acceptance_criteria/0/requirement_ids": ["REQ_SCAN"]},
            ),
            correction_response(
                second_base,
                {
                    "/proposal/requirements": [
                        {"id": requirement_id, "description": description}
                        for requirement_id, description in zip(
                            requirement_ids,
                            valid_payload["proposal"]["requirements"],
                            strict=True,
                        )
                    ]
                },
            ),
        ]
    )
    store = PlanningStore(tmp_path / "planning")
    coordinator = AdaptivePlanningCoordinator(
        executor=executor,
        store=store,
        policy=policy(response_repair_limit=None),
        clock=AdvancingClock(),
    )

    created = coordinator.start(
        request(),
        answer_question=lambda _question: pytest.fail("unexpected question"),
    )

    assert created is not None
    assert len(executor.requests) == 3
    first = store.load_turn(request().run_id, 1)
    assert first.response_normalizations == (
        "removed schema-forbidden field /proposal/requirent_ids",
    )
    second = store.load_turn(request().run_id, 2)
    assert second.semantic_correction_outcome == "improved"
    assert second.response_validation is not None
    assert second.response_validation.correction_paths == ("/proposal/requirements",)
    correction = executor.requests[2].prompt.rsplit(
        "TARGETED_SEMANTIC_CORRECTION_SLOTS_V3", 1
    )[1]
    assert '"$ref": "#/$defs/ProposedRequirement"' not in correction
    assert '"Stable requirement identity' in correction
    assert store.load_turn(request().run_id, 3).semantic_correction_outcome == (
        "accepted"
    )


def test_product_planning_repairs_one_requirement_relation_not_an_id_array(
    tmp_path: Path,
) -> None:
    payload = proposal_response().model_dump(mode="json")
    proposal_payload = payload["proposal"]
    extra_ids = tuple(f"REQ_EXTRA_{index}" for index in range(1, 9))
    proposal_payload["requirement_ids"].extend(extra_ids)
    proposal_payload["acceptance_criteria"][0]["requirement_ids"].extend(extra_ids)
    atomic_requirements = [
        {"id": requirement_id, "description": description}
        for requirement_id, description in zip(
            proposal_body().requirement_ids,
            proposal_body().requirements,
            strict=True,
        )
    ] + [
        {
            "id": requirement_id,
            "description": f"Exercise independent requirement {index}.",
        }
        for index, requirement_id in enumerate(extra_ids, start=1)
    ]
    executor = ScriptedAgentExecutor(
        [
            json.dumps(payload),
            correction_response(
                payload,
                {"/proposal/requirements": atomic_requirements},
            ),
        ]
    )
    store = PlanningStore(tmp_path / "planning")
    coordinator = AdaptivePlanningCoordinator(
        executor=executor,
        store=store,
        policy=policy(response_repair_limit=None),
        clock=AdvancingClock(),
    )

    created = coordinator.start(
        request(),
        answer_question=lambda _question: pytest.fail("unexpected question"),
    )

    assert created is not None
    assert len(created.body.requirements) == 10
    assert created.body.requirement_ids == (
        *proposal_body().requirement_ids,
        *extra_ids,
    )
    first = store.load_turn(request().run_id, 1)
    assert first.response_validation is not None
    assert first.response_validation.issues[0].invariant_id == (
        "planning_requirement_id_cardinality"
    )
    assert first.response_validation.correction_paths == ("/proposal/requirements",)
    assert store.load_turn(request().run_id, 2).semantic_correction_outcome == (
        "accepted"
    )


def test_product_planning_repairs_assumption_relation_as_atomic_records(
    tmp_path: Path,
) -> None:
    invalid_payload = proposal_response().model_dump(mode="json")
    statements = [
        "The project uses a src layout.",
        "The CLI uses the current directory by default.",
    ]
    invalid_payload["proposal"]["assumptions"] = statements
    invalid_payload["proposal"]["assumption_decision_ids"] = ["DECISION_SCAN_STRUCTURE"]
    atomic_assumptions = [
        {
            "statement": statement,
            "decision_id": "DECISION_SCAN_STRUCTURE",
        }
        for statement in statements
    ]
    executor = ScriptedAgentExecutor(
        [
            json.dumps(invalid_payload),
            correction_response(
                invalid_payload,
                {"/proposal/assumptions": atomic_assumptions},
            ),
        ]
    )
    store = PlanningStore(tmp_path / "planning")
    coordinator = AdaptivePlanningCoordinator(
        executor=executor,
        store=store,
        policy=policy(response_repair_limit=None),
        clock=AdvancingClock(),
    )

    created = coordinator.start(
        request(),
        answer_question=lambda _question: pytest.fail("unexpected question"),
    )

    assert created is not None
    assert created.body.assumptions == tuple(statements)
    assert created.body.assumption_decision_ids == (
        "DECISION_SCAN_STRUCTURE",
        "DECISION_SCAN_STRUCTURE",
    )
    first = store.load_turn(request().run_id, 1)
    assert first.response_validation is not None
    assert first.response_validation.issues[0].invariant_id == (
        "planning_assumption_decision_cardinality"
    )
    assert first.response_validation.correction_paths == ("/proposal/assumptions",)
    correction = executor.requests[1].prompt.rsplit(
        "TARGETED_SEMANTIC_CORRECTION_SLOTS_V3", 1
    )[1]
    assert '"target_path": "/proposal/assumptions"' in correction
    assert '"$ref": "#/$defs/ProposedAssumption"' not in correction
    assert "autonomy decision that authorizes this assumption" in correction
    correction_schema_text = correction.split("CORRECTION_SCHEMA_JSON\n", 1)[1].split(
        "\nCall `sat_submit_artifact`", 1
    )[0]
    correction_schema = json.loads(correction_schema_text)
    assumption_array_schema = correction_schema["properties"]["replacements"]["items"][
        "oneOf"
    ][0]["properties"]["replacement_value"]
    assert assumption_array_schema["items"]["properties"]["decision_id"]["enum"] == [
        "DECISION_SCAN_STRUCTURE"
    ]
    accepted = store.load_turn(request().run_id, 2)
    assert accepted.semantic_correction_outcome == "accepted"
    assert accepted.response_normalizations == (
        "compiled atomic proposal.assumptions into canonical statements "
        "and autonomous decision references",
    )


def test_product_planning_stops_after_a_non_improving_correction(
    tmp_path: Path,
) -> None:
    invalid_payload = proposal_response().model_dump(mode="json")
    invalid_payload["proposal"]["tasks"][0]["owner_agent_id"] = "absent_agent"
    executor = ScriptedAgentExecutor(
        [
            json.dumps(invalid_payload),
            correction_response(
                invalid_payload,
                {"/proposal/tasks/0/owner_agent_id": "absent_agent"},
            ),
        ]
    )
    store = PlanningStore(tmp_path / "planning")
    coordinator = AdaptivePlanningCoordinator(
        executor=executor,
        store=store,
        policy=policy(response_repair_limit=None),
        clock=AdvancingClock(),
    )

    with pytest.raises(PlanningError, match="remained invalid"):
        coordinator.start(
            request(),
            answer_question=lambda _question: pytest.fail("unexpected question"),
        )

    assert len(executor.requests) == 2
    assert store.load_turn(request().run_id, 2).semantic_correction_outcome == (
        "no_improvement"
    )
    failed_session = store.load_session(request().run_id)
    assert failed_session.status is PlanningSessionStatus.FAILED
    assert failed_session.turn_count == 2


def test_invalid_correction_slot_identity_is_typed_model_input(tmp_path: Path) -> None:
    invalid_payload = proposal_response().model_dump(mode="json")
    invalid_payload["proposal"]["tasks"][0]["owner_agent_id"] = "absent_agent"
    invalid_correction = {
        "replacements": [
            {
                "slot_handle": "slot_99",
                "replacement_value": "builder",
            }
        ]
    }
    executor = ScriptedAgentExecutor(
        [
            json.dumps(invalid_payload),
            ScriptedAgentResponse(
                text=json.dumps(invalid_correction),
                submission_payload=invalid_correction,
            ),
        ]
    )
    store = PlanningStore(tmp_path / "planning")
    coordinator = AdaptivePlanningCoordinator(
        executor=executor,
        store=store,
        policy=policy(response_repair_limit=None),
        clock=AdvancingClock(),
    )
    with pytest.raises(PlanningError, match="slot outside controller authority"):
        coordinator.start(
            request(), answer_question=lambda _: pytest.fail("unexpected question")
        )
    assert len(executor.requests) == 2
    turn = store.load_turn(request().run_id, 2)
    assert turn.semantic_correction_outcome == "invalid_submission"
    assert turn.parsed_response is None
    assert all(issue.authority == "model" for issue in turn.response_validation.issues)


def test_correction_value_shape_error_returns_to_controller_without_tool_retry(
    tmp_path: Path,
) -> None:
    payload = proposal_response().model_dump(mode="json")
    payload["proposal"]["tasks"][0]["owner_agent_id"] = "absent_agent"
    executor = ScriptedAgentExecutor(
        [
            json.dumps(payload),
            correction_response(payload, {"/proposal/tasks/0/owner_agent_id": []}),
        ]
    )
    store = PlanningStore(tmp_path / "planning")
    coordinator = AdaptivePlanningCoordinator(
        executor=executor,
        store=store,
        policy=policy(response_repair_limit=None),
        clock=AdvancingClock(),
    )
    with pytest.raises(PlanningError, match="remained invalid"):
        coordinator.start(request(), answer_question=lambda _: pytest.fail("question"))
    assert len(executor.requests) == 2
    contract = executor.requests[1].submission_contract
    assert contract is not None
    assert contract.transport_payload_schema() == {
        "type": "object",
        "additionalProperties": True,
    }
    turn = store.load_turn(request().run_id, 2)
    assert turn.parsed_response is None
    assert turn.submission_payload["replacements"][0]["replacement_value"] == []
    assert turn.response_validation is not None
    assert turn.response_validation.correction_paths == (
        "/proposal/tasks/0/owner_agent_id",
    )
    assert turn.response_validation.issues[0].code == "string_type"
    assert turn.semantic_correction_outcome != "accepted"


def test_terminal_planning_progress_shows_heartbeat_and_stops_cleanly() -> None:
    output: list[str] = []
    progress = TerminalPlanningProgress(
        write=output.append,
        heartbeat_seconds=0.01,
    )
    progress(
        PlanningActivity(
            kind=PlanningActivityKind.WAITING_MODEL,
            attempt=1,
            maximum_attempts=2,
            model="provider/model",
        )
    )
    time.sleep(0.025)
    progress(
        PlanningActivity(
            kind=PlanningActivityKind.RESPONSE_RECEIVED,
            attempt=1,
            maximum_attempts=2,
            model="provider/model",
            duration_ms=25,
            execution_status=planning.AgentExecutionStatus.COMPLETED,
        )
    )
    rendered_at_completion = tuple(output)
    time.sleep(0.025)
    progress.close()

    assert any(
        "Planning invocation is queued for provider/model" in line for line in output
    )
    assert any("Planning invocation is queued" in line for line in output)
    assert any("response received in 0.0s (completed)" in line for line in output)
    assert tuple(output) == rendered_at_completion


def test_terminal_planning_progress_shows_safe_tool_action() -> None:
    output: list[str] = []
    progress = TerminalPlanningProgress(write=output.append)

    progress(
        PlanningActivity(
            kind=PlanningActivityKind.TOOL_STARTED,
            attempt=1,
            maximum_attempts=2,
            model="provider/model",
            tool_action_class=AgentToolActionClass.TESTING,
            tool_target_class=AgentToolTargetClass.QUALITY_CHECKS,
            tool_detail="pytest",
        )
    )
    progress(
        PlanningActivity(
            kind=PlanningActivityKind.TOOL_COMPLETED,
            attempt=1,
            maximum_attempts=2,
            model="provider/model",
            tool_action_class=AgentToolActionClass.TESTING,
            tool_target_class=AgentToolTargetClass.QUALITY_CHECKS,
            tool_detail="pytest",
        )
    )
    progress.close()

    assert output == [
        "  Planning started testing quality checks (pytest)",
        "  Planning completed testing quality checks (pytest)",
    ]


def test_terminal_planning_progress_explains_stall_policy_and_recovery() -> None:
    output: list[str] = []
    progress = TerminalPlanningProgress(write=output.append)

    progress(
        PlanningActivity(
            kind=PlanningActivityKind.STALL_SUSPECTED,
            attempt=1,
            maximum_attempts=1,
            model="provider/model",
            inactivity_ms=90_000,
            silence_seconds=120,
            stall_grace_seconds=30,
            policy_source="test provider contract",
        )
    )
    progress(
        PlanningActivity(
            kind=PlanningActivityKind.STALL_RECOVERED,
            attempt=1,
            maximum_attempts=1,
            model="provider/model",
            inactivity_ms=0,
            silence_seconds=120,
            stall_grace_seconds=30,
            policy_source="test provider contract",
        )
    )

    assert "no trusted activity for 90.0s" in output[0]
    assert "another 30s" in output[0]
    assert "test provider contract" in output[0]
    assert "recovered during the 30s grace period" in output[1]


def test_safe_response_variants_do_not_consume_a_model_repair_call(
    tmp_path: Path,
) -> None:
    payload = proposal_response().model_dump(mode="json")
    payload.pop("kind")
    payload["proposal"]["tasks"][0]["expected_paths"] = ["tests/"]
    payload["proposal"]["agents"][0]["workspace_scope"] = "repository/"
    raw_response = json.dumps(payload)
    executor = ScriptedAgentExecutor([raw_response])
    store = PlanningStore(tmp_path / "planning")
    coordinator = AdaptivePlanningCoordinator(
        executor=executor,
        store=store,
        policy=policy(response_repair_limit=0),
        clock=AdvancingClock(),
    )

    created = coordinator.start(
        request(),
        answer_question=lambda _question: pytest.fail("unexpected question"),
    )

    assert created is not None
    assert len(executor.requests) == 1
    turn = store.load_turn(request().run_id, 1)
    assert turn.response_text == raw_response
    assert turn.parsed_response is not None
    assert turn.parsed_response.proposal is not None
    assert turn.parsed_response.proposal.tasks[0].expected_paths == ("tests",)
    assert turn.parsed_response.proposal.agents[0].workspace_scope == "repository"
    assert turn.response_normalizations == (
        "inferred response kind as proposal",
        "canonicalized proposal.tasks[0].expected_paths[0]",
        "canonicalized proposal.agents[0].workspace_scope",
    )


def test_bare_proposal_body_is_framed_without_a_model_repair_call(
    tmp_path: Path,
) -> None:
    expected = proposal_response()
    assert expected.proposal is not None
    payload = expected.proposal.model_dump(mode="json")
    executor = ScriptedAgentExecutor(
        [ScriptedAgentResponse(text="ignored", submission_payload=payload)]
    )
    store = PlanningStore(tmp_path / "planning")
    coordinator = AdaptivePlanningCoordinator(
        executor=executor,
        store=store,
        policy=policy(response_repair_limit=0),
        clock=AdvancingClock(),
    )

    created = coordinator.start(
        request(),
        answer_question=lambda _question: pytest.fail("unexpected question"),
    )

    assert created is not None
    assert len(executor.requests) == 1
    turn = store.load_turn(request().run_id, 1)
    assert turn.submission_payload == payload
    assert turn.parsed_response == expected
    assert turn.response_validation is None
    assert turn.response_normalizations == (
        "framed bare proposal body as proposal response",
    )


def test_bare_question_body_has_one_unambiguous_response_frame() -> None:
    expected = question_response()
    assert expected.question is not None
    payload = expected.question.model_dump(mode="json")

    normalized, changes = planning._normalize_planning_response_payload(payload)

    assert PlanningModelResponse.model_validate(normalized) == expected
    assert changes == ("framed bare question body as question response",)


@pytest.mark.parametrize(
    "payload",
    [
        {"title": "Partial", "objective": "Not a complete proposal body."},
        {
            **proposal_response().proposal.model_dump(mode="json"),
            **question_response().question.model_dump(mode="json"),
        },
    ],
    ids=("partial", "mixed-question-and-proposal"),
)
def test_bare_response_framing_rejects_partial_or_ambiguous_shapes(
    payload: dict[str, object],
) -> None:
    normalized, changes = planning._normalize_planning_response_payload(payload)

    assert normalized == payload
    assert not any(change.startswith("framed bare ") for change in changes)
    with pytest.raises(ValidationError):
        PlanningModelResponse.model_validate(normalized)


def test_valid_quality_task_does_not_consume_a_model_repair_call(
    tmp_path: Path,
) -> None:
    profile_criterion = AcceptanceCriterion(
        id="AC_PROFILE",
        description="The project satisfies the fixed runtime contract.",
        verification="Run the profile contract gate.",
    )
    payload = proposal_response().model_dump(mode="json")
    payload["proposal"]["tasks"].append(quality_task_payload())
    raw_response = json.dumps(payload)
    executor = ScriptedAgentExecutor([raw_response])
    store = PlanningStore(tmp_path / "planning")
    coordinator = AdaptivePlanningCoordinator(
        executor=executor,
        store=store,
        policy=policy(
            response_repair_limit=0,
            profile_acceptance_criteria=(profile_criterion,),
        ),
        clock=AdvancingClock(),
    )

    created = coordinator.start(
        request(),
        answer_question=lambda _question: pytest.fail("unexpected question"),
    )

    assert created is not None
    assert len(executor.requests) == 1
    assert [task.id for task in created.body.tasks] == [
        "TASK_IMPLEMENT",
        "TASK_REVIEW",
    ]
    assert {agent.id for agent in created.body.agents} == {
        "cli_developer",
        "acceptance_tester",
        "quality_reviewer",
    }
    turn = store.load_turn(request().run_id, 1)
    assert turn.response_text == raw_response
    assert turn.response_normalizations == ()


def test_profile_criterion_echo_does_not_consume_a_model_repair_call(
    tmp_path: Path,
) -> None:
    profile_criterion = AcceptanceCriterion(
        id="AC_PROFILE",
        description="The project satisfies the fixed runtime contract.",
        verification="Run the profile contract gate.",
    )
    payload = proposal_response().model_dump(mode="json")
    payload["proposal"]["acceptance_criteria"].append(
        {
            "id": "AC_PROFILE",
            "description": "The model repeats and rewrites the fixed contract.",
            "verification": "Trust the model-authored check.",
            "review_boundaries": ["failure_path"],
        }
    )
    payload["proposal"]["tasks"].append(
        quality_task_payload(acceptance_criteria=["AC_PROFILE"])
    )
    raw_response = json.dumps(payload)
    executor = ScriptedAgentExecutor([raw_response])
    store = PlanningStore(tmp_path / "planning")
    configured = policy(
        response_repair_limit=0,
        profile_acceptance_criteria=(profile_criterion,),
    )
    coordinator = AdaptivePlanningCoordinator(
        executor=executor,
        store=store,
        policy=configured,
        clock=AdvancingClock(),
    )

    created = coordinator.start(
        request(),
        answer_question=lambda _question: pytest.fail("unexpected question"),
    )

    assert created is not None
    assert len(executor.requests) == 1
    assert [item.id for item in created.body.acceptance_criteria] == [
        "AC_SCAN",
        "AC_REPORT",
    ]
    assert created.body.tasks[-1].acceptance_criteria == ("AC_PROFILE",)
    preview = preview_adaptive_proposal(
        request(),
        created,
        configured,
        created_at=FIXED_TIME,
    )
    materialized = next(
        item
        for item in preview.task_brief.acceptance_criteria
        if item.id == "AC_PROFILE"
    )
    assert materialized == profile_criterion
    turn = store.load_turn(request().run_id, 1)
    assert turn.response_text == raw_response
    assert turn.response_normalizations == (
        "removed controller-owned profile criterion AC_PROFILE from "
        "proposal.acceptance_criteria[2]",
    )


def test_profile_collision_deconflicts_required_relational_binding_without_call(
    tmp_path: Path,
) -> None:
    profile_criterion = AcceptanceCriterion(
        id="AC_PROFILE",
        description="The project satisfies the fixed runtime contract.",
        verification="Run the controller-owned profile gate.",
    )
    payload = proposal_response().model_dump(mode="json")
    payload["proposal"]["requirements"].append("Pass the profile test contract.")
    payload["proposal"]["requirement_ids"].append("REQ_PROFILE_TEST")
    payload["proposal"]["acceptance_criteria"].append(
        {
            "id": "AC_PROFILE",
            "description": "The task-specific behavior passes its profile test.",
            "verification": "Inspect the behavior and execute the profile test.",
            "requirement_ids": ["REQ_PROFILE_TEST"],
            "verification_agent_ids": ["quality_reviewer"],
            "review_boundaries": ["failure_path"],
        }
    )
    payload["proposal"]["tasks"][0]["acceptance_criteria"].append("AC_PROFILE")
    raw_response = json.dumps(payload)
    executor = ScriptedAgentExecutor([raw_response])
    store = PlanningStore(tmp_path / "planning")
    configured = policy(
        response_repair_limit=0,
        profile_acceptance_criteria=(profile_criterion,),
    )
    coordinator = AdaptivePlanningCoordinator(
        executor=executor,
        store=store,
        policy=configured,
        clock=AdvancingClock(),
    )

    created = coordinator.start(
        request(),
        answer_question=lambda _question: pytest.fail("unexpected question"),
    )

    assert created is not None
    assert len(executor.requests) == 1
    task_criterion = next(
        item
        for item in created.body.acceptance_criteria
        if item.id == "AC_TASK_PROFILE"
    )
    assert task_criterion.requirement_ids == ("REQ_PROFILE_TEST",)
    assert task_criterion.verification_agent_ids == ("quality_reviewer",)
    assert task_criterion.review_boundaries == (ReviewBoundaryKind.FAILURE_PATH,)
    assert created.body.tasks[0].acceptance_criteria[-2:] == (
        "AC_TASK_PROFILE",
        "AC_PROFILE",
    )
    preview = preview_adaptive_proposal(
        request(),
        created,
        configured,
        created_at=FIXED_TIME,
    )
    criteria_by_id = {item.id: item for item in preview.task_brief.acceptance_criteria}
    assert criteria_by_id["AC_PROFILE"] == profile_criterion
    assert criteria_by_id["AC_TASK_PROFILE"].description == (
        "The task-specific behavior passes its profile test."
    )
    turn = store.load_turn(request().run_id, 1)
    assert turn.response_normalizations == (
        "deconflicted model criterion AC_PROFILE as AC_TASK_PROFILE from "
        "controller-owned profile ID",
    )


def test_profile_collision_preserves_relation_before_writer_binding_correction(
    tmp_path: Path,
) -> None:
    profile_criterion = AcceptanceCriterion(
        id="AC_DOCUMENTATION",
        description="The project satisfies the fixed documentation contract.",
        verification="Run the controller-owned documentation gate.",
    )
    payload = proposal_response().model_dump(mode="json")
    payload["proposal"]["requirements"].append("Document the task-specific usage.")
    payload["proposal"]["requirement_ids"].append("REQ_README")
    payload["proposal"]["acceptance_criteria"].append(
        {
            "id": "AC_DOCUMENTATION",
            "description": "The README documents the task-specific usage.",
            "verification": "Inspect the README against the requested workflow.",
            "requirement_ids": ["REQ_README"],
            "verification_agent_ids": ["quality_reviewer"],
            "review_boundaries": [],
        }
    )
    corrected_tasks = json.loads(json.dumps(payload["proposal"]["tasks"]))
    corrected_tasks[0]["acceptance_criteria"].append("AC_TASK_DOCUMENTATION")
    configured = policy(
        response_repair_limit=None,
        profile_acceptance_criteria=(profile_criterion,),
    )
    correction_base, _ = planning._normalize_planning_response_payload(
        payload,
        profile_criterion_ids=(
            criterion.id for criterion in configured.profile_acceptance_criteria
        ),
        user_inputs=(request().source_request,),
    )
    executor = ScriptedAgentExecutor(
        [
            json.dumps(payload),
            correction_response(
                correction_base,
                {
                    "/proposal/tasks/0/acceptance_criteria": corrected_tasks[0][
                        "acceptance_criteria"
                    ]
                },
            ),
        ]
    )
    store = PlanningStore(tmp_path / "planning")
    coordinator = AdaptivePlanningCoordinator(
        executor=executor,
        store=store,
        policy=configured,
        clock=AdvancingClock(),
    )

    created = coordinator.start(
        request(),
        answer_question=lambda _question: pytest.fail("unexpected question"),
    )

    assert created is not None
    assert len(executor.requests) == 2
    first = store.load_turn(request().run_id, 1)
    assert first.response_normalizations == (
        "deconflicted model criterion AC_DOCUMENTATION as "
        "AC_TASK_DOCUMENTATION from controller-owned profile ID",
    )
    assert first.response_validation is not None
    assert first.response_validation.correction_paths == (
        "/proposal/tasks/0/acceptance_criteria",
    )
    assert first.response_validation.issues[0].invariant_id == (
        "planning_writer_criterion_coverage"
    )
    assert [
        item.model_dump(mode="json")
        for item in first.response_validation.issues[0].subjects
    ] == [{"kind": "criterion", "identifier": "AC_TASK_DOCUMENTATION"}]
    accepted = store.load_turn(request().run_id, 2)
    assert accepted.semantic_correction_outcome == "accepted"
    assert {item.id for item in created.body.acceptance_criteria} == {
        "AC_SCAN",
        "AC_REPORT",
        "AC_TASK_DOCUMENTATION",
    }
    assert created.body.tasks[0].acceptance_criteria[-1] == ("AC_TASK_DOCUMENTATION")


@pytest.mark.parametrize("failure", [KeyboardInterrupt, RuntimeError])
@pytest.mark.parametrize("returned", [False, True])
@pytest.mark.parametrize(
    "detail",
    ["original failure", "original failure" + "x" * 5000],
    ids=["short", "long"],
)
def test_planning_interruption_and_exception_settle_and_persist_before_propagation(
    tmp_path: Path,
    failure: type[BaseException],
    returned: bool,
    detail: str,
) -> None:
    from software_agent_team.execution import execution_exception_result

    class FailingExecutor:
        def execute(self, execution_request, **kwargs):
            error = failure(detail)
            if not returned:
                raise error
            return execution_exception_result(
                execution_request,
                error,
                started_at=FIXED_TIME,
                finished_at=FIXED_TIME + timedelta(seconds=2),
            )

    budget = AgentBudget(
        authority=BudgetAuthority.USER_TASK,
        max_estimated_cost_usd="2",
    )
    ledger = AgentBudgetLedger(budget)
    store = PlanningStore(tmp_path / "planning")
    coordinator = AdaptivePlanningCoordinator(
        executor=FailingExecutor(),
        store=store,
        route_id="default",
        policy=policy(budget=budget),
        budget_ledger=ledger,
        pricing=ModelPricing(
            model="provider/model",
            input_cost_per_million_usd="1",
            output_cost_per_million_usd="2",
            pricing_source=ModelMetadataSource.USER_SUPPLIED,
            cache_pricing=CachePricing(
                read_cost_per_million_usd="0.1",
                write_cost_per_million_usd="0",
                source=ModelMetadataSource.USER_SUPPLIED,
                observed_at=FIXED_TIME,
            ),
        ),
        clock=AdvancingClock(),
    )
    expected = PlanningError if returned and failure is RuntimeError else failure
    with pytest.raises(expected) as caught:
        coordinator.start(request(), answer_question=lambda _: pytest.fail("question"))
    if expected is not KeyboardInterrupt or not returned:
        assert "original failure" in str(caught.value)
    session = store.load_session(request().run_id)
    assert session.status is (
        PlanningSessionStatus.CANCELLED
        if failure is KeyboardInterrupt
        else PlanningSessionStatus.FAILED
    )
    assert session.turn_count == 1
    turn = store.load_turn(request().run_id, 1)
    assert turn.execution.error.endswith(detail)
    assert len(turn.validation_error) <= 2000
    assert turn.execution.duration_ms > 0
    assert turn.execution.invocation_lifecycle is None  # Unknown, not fabricated.
    assert turn.execution.input_tokens is None
    assert turn.execution.estimated_cost_usd is None
    usage = ledger.snapshot()
    assert usage.calls_started == usage.calls_completed == 1
    assert usage.active_calls == 0
    assert usage.unreported_token_calls == 1
    assert turn.execution.cost_record == ledger.call_records()[0]
    assert session.turn_head_sha256 == canonical_model_sha256(turn)
    assert turn.parsed_response is None
    if failure is RuntimeError:
        legacy = session.model_dump(mode="json")
        legacy["schema_version"] = 12
        with pytest.raises(ValidationError, match="legacy Planning sessions"):
            planning.PlanningSession.model_validate(legacy)


def test_real_cli_sigint_persists_planning_lifecycle_before_exit_130(
    tmp_path: Path,
) -> None:
    """Exercise CLI signal handling with an owned child, without a provider call."""

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(20)
    address = listener.getsockname()
    binary = tmp_path / "openclaw"
    binary.write_text(
        f"#!{sys.executable}\nimport socket, time\n"
        f"with socket.create_connection({address!r}, 10) as connection:\n"
        "    connection.sendall(b'ready')\n"
        "time.sleep(60)\n",
    )
    binary.chmod(0o700)
    script = f"""
import runpy
from pathlib import Path
from software_agent_team import cli
from software_agent_team.execution import OpenClawSubprocessExecutor
from software_agent_team.process_lifecycle import ProcessLeaseStore
fixtures = runpy.run_path({str(Path(__file__).resolve())!r})
store = fixtures['PlanningStore'](Path({str(tmp_path / "planning")!r}))
executor = OpenClawSubprocessExecutor(
    openclaw_binary=Path({str(binary)!r}), process_grace_seconds=1,
    liveness_poll_seconds=5,
    process_lease_store=ProcessLeaseStore(Path({str(tmp_path / "leases")!r})),
)
budget = fixtures['AgentBudget'](
    authority=fixtures['BudgetAuthority'].USER_TASK,
    max_estimated_cost_usd='2',
)
coordinator = fixtures['AdaptivePlanningCoordinator'](
    executor=executor, store=store, policy=fixtures['policy'](budget=budget),
    route_id='default',
    budget_ledger=fixtures['AgentBudgetLedger'](budget),
    pricing=fixtures['ModelPricing'](
        model='provider/model', input_cost_per_million_usd='1',
        output_cost_per_million_usd='2',
        pricing_source=fixtures['ModelMetadataSource'].USER_SUPPLIED,
        cache_pricing=fixtures['CachePricing'](
            read_cost_per_million_usd='0.1', write_cost_per_million_usd='0',
            source=fixtures['ModelMetadataSource'].USER_SUPPLIED,
            observed_at=fixtures['FIXED_TIME'],
        ),
    ),
)
cli._run_product = lambda: coordinator.start(
    fixtures['request'](), answer_question=lambda question: None,
)
raise SystemExit(cli.main([]))
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        try:
            connection, _ = listener.accept()
        except TimeoutError:
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                pytest.fail(f"CLI exited before child readiness: {stdout}\n{stderr}")
            raise
        with listener, connection:
            assert connection.recv(5) == b"ready"
        os.kill(process.pid, signal.SIGINT)
        stdout, stderr = process.communicate(timeout=20)
        assert process.returncode == 130, (stdout, stderr)
        assert "Build interrupted" in stdout
        assert "Traceback" not in stderr
        store = PlanningStore(tmp_path / "planning")
        session = store.load_session(request().run_id)
        assert session.status is PlanningSessionStatus.CANCELLED
        assert session.turn_count == 1
        turn = store.load_turn(request().run_id, 1)
        assert turn.execution.status.value == "interrupted"
        lifecycle = turn.execution.invocation_lifecycle
        assert lifecycle is not None
        assert lifecycle.shutdown.reason.value == "user_interrupt"
        assert lifecycle.shutdown.cleanup_completed
        # Reloaded durable evidence must preserve accounting uncertainty without
        # replacing the actual interruption or requiring the exited CLI's memory.
        usage = turn.execution.budget_usage
        assert usage is not None
        assert usage.calls_started == usage.calls_completed == 1
        assert usage.active_calls == 0
        assert usage.unreported_token_calls == usage.unpriced_calls == 1
        assert turn.execution.budget_error is not None
        assert turn.execution.estimated_cost_usd is None
        record = turn.execution.cost_record
        assert record is not None
        assert record.input_tokens is record.output_tokens is None
        assert record.cost_usd is None
        assert session.turn_head_sha256 == canonical_model_sha256(turn)
        from software_agent_team.process_lifecycle import ProcessLeaseStore

        assert not ProcessLeaseStore(tmp_path / "leases").inspect().processes
        legacy = turn.model_dump(mode="json")
        assert legacy["schema_version"] == planning.PLANNING_SCHEMA_VERSION
        assert legacy["execution"]["invocation_lifecycle"]["schema_version"] == 5
        legacy["schema_version"] = 16
        assert PlanningTurn.model_validate(legacy).model_dump(mode="json") == legacy
        legacy["schema_version"] = 14
        with pytest.raises(ValidationError, match="lifecycle v5"):
            PlanningTurn.model_validate(legacy)
        legacy["execution"]["invocation_lifecycle"]["schema_version"] = 4
        legacy["execution"]["invocation_lifecycle"]["initialization"].pop(
            "process_activity_observations",
            None,
        )
        assert PlanningTurn.model_validate(legacy).model_dump(mode="json") == legacy
        legacy["schema_version"] = 13
        with pytest.raises(ValidationError, match="lifecycle v4"):
            PlanningTurn.model_validate(legacy)
        legacy["execution"]["invocation_lifecycle"]["schema_version"] = 3
        assert PlanningTurn.model_validate(legacy).model_dump(mode="json") == legacy
        legacy["schema_version"] = 12
        with pytest.raises(ValidationError, match="legacy Planning turns"):
            PlanningTurn.model_validate(legacy)
        del legacy["execution"]["invocation_lifecycle"]
        assert PlanningTurn.model_validate(legacy).model_dump(mode="json") == legacy
    finally:
        listener.close()
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        # Exact leases also identify the separate OpenClaw process group if the
        # intentionally failing old implementation escaped CLI cleanup.
        from software_agent_team.process_lifecycle import ProcessLeaseStore

        leases = ProcessLeaseStore(tmp_path / "leases")
        leases.reclaim_orphans(grace_seconds=1)


def test_cancellation_stops_before_a_proposal_or_approval(tmp_path: Path) -> None:
    planning_request = request(source_request=AMBIGUOUS_LINK_REQUEST)
    store = PlanningStore(tmp_path / "planning")
    coordinator = AdaptivePlanningCoordinator(
        executor=ScriptedAgentExecutor([response(question_response())]),
        store=store,
        policy=policy(),
        clock=AdvancingClock(),
    )

    created = coordinator.start(
        planning_request,
        answer_question=lambda _question: None,
    )

    assert created is None
    session = store.load_session(request().run_id)
    assert session.status is PlanningSessionStatus.CANCELLED
    assert session.latest_proposal_revision is None


def test_ordinary_user_can_answer_revise_edit_and_approve_without_json(
    tmp_path: Path,
) -> None:
    planning_request = request(source_request=AMBIGUOUS_LINK_REQUEST)
    answered_body = proposal_body(question_id="link_scope")
    executor = ScriptedAgentExecutor(
        [
            response(question_response()),
            response(proposal_response(answered_body)),
            response(
                proposal_response(
                    proposal_body(
                        title="Local Markdown Link Checker",
                        question_id="link_scope",
                    )
                )
            ),
        ]
    )
    coordinator = AdaptivePlanningCoordinator(
        executor=executor,
        store=PlanningStore(tmp_path / "planning"),
        policy=policy(),
        clock=AdvancingClock(),
    )
    answers = iter(
        (
            "1",
            "f",
            "f",
            "r",
            "Make local-only scope explicit in the title.",
            "e",
            "1",
            "1",
            "a",
        )
    )
    prompts: list[str] = []
    output: list[str] = []

    def read(prompt: str) -> str:
        prompts.append(prompt)
        return next(answers)

    approved = run_interactive_planning(
        coordinator,
        planning_request,
        read=read,
        write=output.append,
    )

    assert approved is not None
    assert approved.task_brief.title == "Local Markdown Link Checker"
    assert approved.team_plan.max_concurrency == 1
    assert approved.approval.revision == 3
    assert len(executor.requests) == 3
    rendered = "\n".join(output)
    assert "Planning question" in rendered
    assert "Decision boundary: product_requirement / user" in rendered
    assert "Missing evidence:" in rendered
    assert "What this can change:" in rendered
    assert "Planning is waiting for provider/model" in rendered
    assert "Planning response validated" in rendered
    assert "Custom answer" in rendered
    assert "Planning overview" in rendered
    assert "Fixed policy details are now shown." in rendered
    assert "Fixed policy details are now hidden." in rendered
    assert "Plan revision 3 approved." in rendered
    assert "Additional user decisions resolved during clarification:" in rendered
    assert (
        "DECISION_LINK_SCOPE_ANSWER [product_requirement; question=link_scope]"
        in rendered
    )
    assert "Runtime Agents" in rendered
    assert "Request changes in your own words" in rendered
    assert "Edit safe limits" in rendered
    assert "controller may now create only the Agents shown above" in rendered
    assert not any("JSON" in line for line in output)
    assert prompts[-1] == "Review choice: "
