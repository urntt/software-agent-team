"""Shared valid Planning inputs for behavior-focused test modules."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from software_agent_team.artifacts import (
    DeliveryMaturity,
    ProductDefinition,
    ProductDefinitionDisposition,
    ProductDefinitionImpact,
    ProductDefinitionStatement,
    ProductMaturityDefinition,
)
from software_agent_team.budgets import (
    AgentBudget,
)
from software_agent_team.planning import (
    AgentWorkload,
    CapabilityTimeoutPolicy,
    PlanningDecisionAuthority,
    PlanningDecisionCategory,
    PlanningDecisionProvenance,
    PlanningDecisionProvenanceKind,
    PlanningDecisionRecord,
    PlanningModelResponse,
    PlanningPolicy,
    PlanningProposal,
    PlanningProposalBody,
    PlanningProposalSource,
    PlanningRequest,
    PlanningResponseKind,
    ProposedAgent,
    ProposedCriterion,
    ProposedTask,
)
from software_agent_team.teams import (
    AgentCapability,
)

FIXED_TIME = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


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


def proposal_response(
    body: PlanningProposalBody | None = None,
) -> PlanningModelResponse:
    return PlanningModelResponse(
        kind=PlanningResponseKind.PROPOSAL,
        proposal=body or proposal_body(),
    )
