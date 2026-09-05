"""Tests for adaptive Planning dialogue, validation, approval, and evidence."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

import software_agent_team.planning as planning
from software_agent_team.artifacts import (
    AcceptanceCriterion,
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
    AgentExecutionResult,
    AgentExecutionStatus,
    AgentExecutionTelemetry,
    AgentTokenUsage,
    ScriptedAgentExecutor,
    ScriptedAgentResponse,
)
from software_agent_team.integrity import canonical_model_sha256
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
    PlanningDecisionRecord,
    PlanningError,
    PlanningIntegrityError,
    PlanningModelResponse,
    PlanningOption,
    PlanningPolicy,
    PlanningProposal,
    PlanningProposalBody,
    PlanningProposalSource,
    PlanningQuestion,
    PlanningRequest,
    PlanningResponseKind,
    PlanningSessionStatus,
    PlanningStore,
    PlanningTurn,
    ProposedAgent,
    ProposedCriterion,
    ProposedTask,
    StructuredEditKind,
    StructuredPlanEdit,
    TerminalPlanningProgress,
    apply_structured_edit,
    preview_adaptive_proposal,
    render_planning_overview,
    run_interactive_planning,
)
from software_agent_team.response_corrections import semantic_payload_sha256
from software_agent_team.teams import (
    AgentCapability,
    ModelRouteSelectionSource,
    ModelRoutingMode,
    ModelSwitchCondition,
    PermissionProfile,
    TeamPlanOrigin,
)

FIXED_TIME = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
AMBIGUOUS_LINK_REQUEST = "Build a CLI that checks Markdown links."


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
        "Build a local-only CLI that checks Markdown file and fragment links "
        "without fetching remote URLs."
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
            summary="Verify observable scan failures and diagnostic locations.",
            rationale="These behaviors make the requested CLI testable.",
        ),
        PlanningDecisionRecord(
            id="DECISION_DELIVERY",
            category=PlanningDecisionCategory.DELIVERY,
            authority=PlanningDecisionAuthority.PLANNER_PROPOSAL,
            summary="Deliver one runnable local CLI project.",
            rationale="The request is for a local command-line tool.",
        ),
        PlanningDecisionRecord(
            id="DECISION_TEAM",
            category=PlanningDecisionCategory.TEAM,
            authority=PlanningDecisionAuthority.PLANNER_PROPOSAL,
            summary="Use one writer and two downstream quality Agents.",
            rationale="The cohesive implementation still needs independent checks.",
        ),
        PlanningDecisionRecord(
            id="DECISION_MODEL_ROUTE",
            category=PlanningDecisionCategory.MODEL_ROUTE,
            authority=PlanningDecisionAuthority.PLANNER_PROPOSAL,
            summary="Use capability-compatible configured model routes.",
            rationale="No task evidence justifies a route override.",
        ),
        PlanningDecisionRecord(
            id="DECISION_SCAN_STRUCTURE",
            category=PlanningDecisionCategory.LOCAL_IMPLEMENTATION,
            authority=PlanningDecisionAuthority.AGENT_AUTONOMY,
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
                summary="Keep the first release limited to local links.",
                rationale="The user selected the deterministic local-only option.",
                question_id=question_id,
            ),
        )
    return PlanningProposalBody(
        title=title,
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
    assert "Execution-profile constraints (controller-owned):" in overview
    assert "Additional task constraints proposed by Planning:" in overview
    assert preview.planner_constraints == ("Use only the standard library at runtime.",)
    assert overview.count("Use the versioned uv environment") == 1
    assert overview.count("Use only the standard library at runtime.") == 1
    assert "Outcome and scope:" in overview
    assert "REQ_SCAN: Scan Markdown files" in overview
    assert "Non-goals:" in overview
    assert "Decisions and assumptions:" in overview
    assert "Planning recommendations requiring approval:" in overview
    assert "none beyond the user-owned source request shown above" in overview
    assert "Non-negotiable Controller policy:" in overview
    assert "Requirement-to-evidence traceability:" in overview
    assert "AC_SCAN: writers=TASK_IMPLEMENT->cli_developer" in overview
    assert "independent verification=acceptance_tester" in overview
    assert "inputs: approved TaskBrief and implementation plan" in overview
    assert "output: work_result" in overview
    assert (
        "handoff: durable artifact to acceptance_tester, quality_reviewer" in overview
    )
    assert "Failure and delivery boundary:" in overview


def response(value: PlanningModelResponse) -> str:
    return value.model_dump_json()


def correction_response(
    base_payload: dict[str, object],
    replacements: dict[str, object],
) -> str:
    return json.dumps(
        {
            "kind": "semantic_correction_v1",
            "base_response_sha256": semantic_payload_sha256(base_payload),
            "replacements": [
                {"path": path, "value": value} for path, value in replacements.items()
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


def test_question_requires_suggestions_and_preserves_custom_answers() -> None:
    question = question_response().question

    assert question is not None
    assert len(question.options) == 2
    assert question.allow_custom


@pytest.mark.parametrize(
    ("category", "owner", "message"),
    (
        (
            PlanningDecisionCategory.LOCAL_IMPLEMENTATION,
            PlanningDecisionAuthority.AGENT_AUTONOMY,
            "cannot ask the user to decide",
        ),
        (
            PlanningDecisionCategory.PRODUCT_REQUIREMENT,
            PlanningDecisionAuthority.PLANNER_PROPOSAL,
            "belongs to user",
        ),
    ),
)
def test_controller_rejects_questions_outside_the_responsibility_matrix(
    tmp_path: Path,
    category: PlanningDecisionCategory,
    owner: PlanningDecisionAuthority,
    message: str,
) -> None:
    question = question_response().question
    assert question is not None
    invalid = question.model_copy(
        update={
            "decision_category": category,
            "decision_owner": owner,
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

    with pytest.raises(PlanningError, match=message):
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


@pytest.mark.parametrize(
    ("invalid_body", "message"),
    (
        (
            proposal_body().model_copy(update={"non_goals": ()}),
            "must state at least one non-goal",
        ),
        (
            proposal_body().model_copy(update={"requirement_ids": ("REQ_SCAN",)}),
            "one stable ID for every requirement",
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
            summary="Assume the user accepts network reliability risk.",
            rationale="This deliberately hides an unresolved risk decision.",
            question_id="network_risk",
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
        update={"review_boundaries": tuple(ReviewBoundaryKind)}
    )
    accepted = PlanningProposalBody.model_validate(
        body.model_copy(
            update={
                "acceptance_criteria": (*body.acceptance_criteria, complete),
                "tasks": tasks,
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
    overview = render_planning_overview(preview)
    assert (
        "Review boundaries: top_level_input, nested_input, "
        "alias_or_indirection, failure_path"
    ) in overview
    assert "Review boundary definitions:" in overview
    assert "root itself is the top-level input" in overview
    assert "immediate first-level child, is nested input" in overview

    absolute_request = request().model_copy(
        update={"source_request": "Build a scanner that must not follow symlinks."}
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
    assert "model calls: 14" in overview
    assert "cumulative Agent time: 7200 seconds" in overview
    assert "estimated cost ceiling: $25" in overview


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
        "(pricing: $0.50 input / $1.50 output per million tokens)"
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
        criterion.model_copy(update={"review_boundaries": boundaries})
        for criterion in body.acceptance_criteria
    )
    profile_criterion = AcceptanceCriterion(
        id="AC_PROFILE_BOUNDARY",
        description="The profile guarantee holds across every approved boundary.",
        verification="Probe every approved profile boundary independently.",
        review_boundaries=boundaries,
    )

    preview = preview_adaptive_proposal(
        request(),
        proposal(body=body.model_copy(update={"acceptance_criteria": criteria})),
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
    assert "response_normalizations" not in payload
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
    payload.pop("response_validation", None)
    payload.pop("semantic_correction_request", None)
    payload.pop("semantic_correction_outcome", None)

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


def test_planning_store_accepts_evidence_indexes_beyond_three_digits(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "turns"
    evidence.mkdir()
    (evidence / "999.json").write_text("{}\n", encoding="utf-8")
    (evidence / "1000.json").write_text("{}\n", encoding="utf-8")

    assert PlanningStore._indexed_files(evidence) == {999, 1000}


def test_planning_uses_the_shared_task_cost_ledger_and_persists_source(
    tmp_path: Path,
) -> None:
    task_budget = AgentBudget(
        authority=BudgetAuthority.USER_TASK,
        max_estimated_cost_usd="1.00",
    )
    ledger = AgentBudgetLedger(task_budget)
    store = PlanningStore(tmp_path / "planning")
    executor = ScriptedAgentExecutor(
        [
            ScriptedAgentResponse(
                text=response(proposal_response()),
                model="provider/model",
                provider="provider",
                usage=AgentTokenUsage(input_tokens=100_000, output_tokens=20_000),
                duration_ms=125,
            )
        ]
    )
    coordinator = AdaptivePlanningCoordinator(
        executor=executor,
        store=store,
        policy=policy(budget=task_budget),
        budget_ledger=ledger,
        pricing=ModelPricing(
            model="provider/model",
            input_cost_per_million_usd="2.50",
            output_cost_per_million_usd="10.00",
            pricing_source=ModelMetadataSource.RUNTIME_CATALOG,
            pricing_observed_at=FIXED_TIME,
        ),
        route_id="default",
        clock=AdvancingClock(),
    )

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
    assert usage.known_estimated_cost_usd == Decimal("0.45")
    execution = store.load_turn(request().run_id, 1).execution
    assert execution.estimated_cost_usd == Decimal("0.45")
    assert execution.pricing_source is ModelMetadataSource.RUNTIME_CATALOG
    assert execution.budget_usage == usage
    assert execution.budget_error is None


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
    execution_result = AgentExecutionResult(
        status=AgentExecutionStatus.COMPLETED,
        response_text=response(proposal_response()),
        telemetry=AgentExecutionTelemetry(
            role=planning.AgentRole.CLARIFIER,
            agent_id="clarifier",
            capability=AgentCapability.CLARIFICATION,
            session_key="agent:clarifier:test",
            command=("fake-openclaw",),
            started_at=FIXED_TIME,
            finished_at=FIXED_TIME,
            duration_ms=125,
            exit_code=0,
            provider="provider",
            model="provider/model",
            provider_liveness=liveness,
        ),
    )
    store = PlanningStore(tmp_path / "planning")
    coordinator = AdaptivePlanningCoordinator(
        executor=ScriptedAgentExecutor([execution_result]),
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
    planning_context_text = (
        executor.requests[0]
        .prompt.split("PLANNING_CONTEXT_JSON\n", 1)[1]
        .split("\n\nRESPONSE_SCHEMA_JSON", 1)[0]
    )
    planning_context = json.loads(planning_context_text)
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
    criterion_schema = response_schema["$defs"]["ProposedCriterion"]
    proposal_schema = response_schema["$defs"]["PlanningProposalBody"]
    assert {
        "decision_category",
        "decision_owner",
        "missing_evidence",
        "material_consequences",
    }.issubset(question_schema["required"])
    assert question_schema["properties"]["decision_category"] == {
        "$ref": "#/$defs/PlanningDecisionCategory"
    }
    assert question_schema["properties"]["decision_owner"] == {
        "$ref": "#/$defs/PlanningDecisionAuthority"
    }
    assert {"requirement_ids", "verification_agent_ids"}.issubset(
        criterion_schema["required"]
    )
    assert "review_boundaries" in criterion_schema["required"]
    assert "default" not in criterion_schema["properties"]["review_boundaries"]
    assert {
        "requirement_ids",
        "non_goals",
        "assumption_decision_ids",
        "decisions",
    }.issubset(proposal_schema["required"])

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
    correction = {
        "kind": "semantic_correction_v1",
        "base_response_sha256": semantic_payload_sha256(invalid_payload),
        "replacements": [
            {
                "path": "/proposal/tasks/0/owner_agent_id",
                "value": valid_payload["proposal"]["tasks"][0]["owner_agent_id"],
            },
        ],
    }
    executor = ScriptedAgentExecutor(
        [
            json.dumps(invalid_payload),
            json.dumps(correction),
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
    assert "TARGETED_SEMANTIC_CORRECTION_V1" in executor.requests[1].prompt
    assert "Do not regenerate or repeat that object" in executor.requests[1].prompt
    assert rejected.response_validation is not None
    assert rejected.response_validation.correction_paths == (
        "/proposal/tasks/0/owner_agent_id",
    )
    corrected = store.load_turn(request().run_id, 2)
    assert corrected.semantic_correction_request is not None
    assert corrected.semantic_correction_outcome == "accepted"
    assert [activity.kind for activity in activities] == [
        PlanningActivityKind.WAITING_MODEL,
        PlanningActivityKind.RESPONSE_RECEIVED,
        PlanningActivityKind.CORRECTION_SCHEDULED,
        PlanningActivityKind.WAITING_MODEL,
        PlanningActivityKind.RESPONSE_RECEIVED,
        PlanningActivityKind.RESPONSE_VALIDATED,
    ]
    assert [
        (activity.attempt, activity.maximum_attempts) for activity in activities
    ] == [(1, 2), (1, 2), (1, 2), (2, 2), (2, 2), (2, 2)]


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

    assert any("Planning is waiting for provider/model" in line for line in output)
    assert any("still waiting for the model" in line for line in output)
    assert any("response received in 0.0s (completed)" in line for line in output)
    assert tuple(output) == rendered_at_completion


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
    rendered = "\n".join(output)
    assert "Planning question" in rendered
    assert "Decision boundary: product_requirement / user" in rendered
    assert "Missing evidence:" in rendered
    assert "What this can change:" in rendered
    assert "Planning is waiting for provider/model" in rendered
    assert "Planning response validated" in rendered
    assert "Custom answer" in rendered
    assert "Planning overview" in rendered
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
