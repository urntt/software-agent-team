"""Main dynamic agent runner class coordinating execution across DAG steps.

This module provides DynamicAgentRunner, combining workspace validation, handoff
orchestration, agent invocation with fallback/retries, telemetry observation,
and progress checkpoint coordination into a unified callable scheduler adapter.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from threading import Condition, RLock
from typing import cast

from software_agent_team.artifact_store import ArtifactStore
from software_agent_team.artifacts import (
    ArtifactReference,
    CheckStatus,
    CommandEvidence,
    ReviewReport,
    TaskBrief,
    TestReport,
    WorkResult,
)
from software_agent_team.budgets import AgentBudgetLedger, ModelPricing
from software_agent_team.execution import AgentExecutor
from software_agent_team.git_workspace import GitWorkspace, GitWorkspaceManager
from software_agent_team.integrity import canonical_model_sha256
from software_agent_team.planning import (
    AdaptiveImplementationPlan,
    validate_task_agent_bindings,
    validate_writer_task_scope_forecasts,
)
from software_agent_team.progress import ProgressDraftHandler
from software_agent_team.prompting import DynamicRevisionFeedback
from software_agent_team.run_control import TerminationReason
from software_agent_team.scheduling import AgentRunOutcome, AgentRunStatus
from software_agent_team.teams import (
    AgentCapability,
    AgentSpec,
    TeamPlan,
    TeamPlanOrigin,
)

from .coordinator import CoordinatorMixin
from .dispositions import AttemptDispositionMixin
from .handoffs import HandoffMixin
from .invocation import AgentInvocationMixin
from .telemetry import AgentTelemetryMixin
from .types import (
    _SCHEDULER_SUMMARY_LIMIT,
    DynamicAgentRunnerError,
    DynamicQualityGate,
    GuidanceProvider,
    InvocationStopProvider,
    _bounded_artifact_summary,
    _system_clock,
)
from .workspace import WorkspaceValidationMixin


class DynamicAgentRunner(
    AgentInvocationMixin,
    AgentTelemetryMixin,
    CoordinatorMixin,
    AttemptDispositionMixin,
    WorkspaceValidationMixin,
    HandoffMixin,
):
    """Invoke, verify, assemble, account for, and hand off approved Agents.

    ``DagScheduler`` remains the sole scheduling authority. This adapter owns
    the bounded lifecycle of one ready ``AgentSpec`` and never creates another
    Agent, changes the approved DAG, or extends an approved timeout.
    """

    def __init__(
        self,
        *,
        task_brief: TaskBrief,
        implementation_plan: AdaptiveImplementationPlan,
        team_plan: TeamPlan,
        workspace: GitWorkspace,
        workspace_manager: GitWorkspaceManager,
        artifact_store: ArtifactStore,
        executor: AgentExecutor,
        quality_gate: DynamicQualityGate,
        budget_ledger: AgentBudgetLedger,
        pricing_by_model: Mapping[str, ModelPricing],
        manual_review_criteria: tuple[str, ...] = (),
        review_scope_by_agent: Mapping[str, tuple[str, ...]] | None = None,
        iteration: int = 1,
        input_commit: str | None = None,
        artifact_repair_limit: int | None = None,
        revision_feedback: DynamicRevisionFeedback | None = None,
        revision_command_evidence: tuple[CommandEvidence, ...] = (),
        guidance_provider: GuidanceProvider | None = None,
        invocation_stop_provider: InvocationStopProvider | None = None,
        activity_handler: ProgressDraftHandler | None = None,
        clock: Callable[[], datetime] = _system_clock,
    ) -> None:
        if team_plan.origin is not TeamPlanOrigin.ADAPTIVE_PLANNING:
            raise ValueError("dynamic runner requires an adaptive TeamPlan")
        if not task_brief.confirmed:
            raise ValueError("dynamic runner requires a confirmed TaskBrief")
        if task_brief.run_id != implementation_plan.run_id:
            raise ValueError("implementation plan belongs to a different run")
        if (
            task_brief.run_id != team_plan.run_id
            or workspace.run_id != team_plan.run_id
        ):
            raise ValueError("dynamic runtime inputs use different run IDs")
        if implementation_plan.team_id != team_plan.team_id:
            raise ValueError("implementation plan belongs to a different team")
        if implementation_plan.revision != team_plan.revision:
            raise ValueError("implementation plan and TeamPlan revisions differ")
        if canonical_model_sha256(task_brief) != team_plan.task_brief_sha256:
            raise ValueError("TeamPlan does not bind the supplied TaskBrief")
        if canonical_model_sha256(implementation_plan) != (
            team_plan.implementation_plan_sha256
        ):
            raise ValueError("TeamPlan does not bind the implementation plan")
        if (
            artifact_store.task_brief != task_brief
            or artifact_store.team_plan != team_plan
        ):
            raise ValueError("artifact store binds different dynamic inputs")
        if not 1 <= iteration <= team_plan.iteration_limit:
            raise ValueError("dynamic iteration exceeds the approved TeamPlan")
        if artifact_repair_limit not in {None, 0, 1}:
            raise ValueError(
                "controlled dynamic evaluation permits zero or one correction"
            )
        revision_command_ids = [item.id for item in revision_command_evidence]
        if len(revision_command_ids) != len(set(revision_command_ids)):
            raise ValueError("revision command evidence IDs must be unique")
        if revision_command_evidence and revision_feedback is None:
            raise ValueError("revision command evidence requires revision feedback")

        writer_ids = {
            agent.id
            for agent in team_plan.agents
            if agent.capability
            in {AgentCapability.IMPLEMENTATION, AgentCapability.INTEGRATION}
        }
        try:
            validate_task_agent_bindings(
                implementation_plan.tasks,
                {agent.id: agent.dependencies for agent in team_plan.agents},
                writer_ids,
            )
            if implementation_plan.schema_version >= 22:
                validate_writer_task_scope_forecasts(
                    implementation_plan.tasks, team_plan.agents
                )
        except ValueError as error:
            raise ValueError(f"dynamic implementation plan {error}") from error

        criteria = tuple(item.strip() for item in manual_review_criteria)
        if any(not item for item in criteria) or len(criteria) != len(set(criteria)):
            raise ValueError("manual-review criterion IDs must be non-empty and unique")
        known_criteria = {item.id for item in task_brief.acceptance_criteria}
        if not set(criteria).issubset(known_criteria):
            raise ValueError("manual-review scope references an unknown criterion")
        reviewer_ids = {
            agent.id
            for agent in team_plan.agents
            if agent.capability is AgentCapability.REVIEW
        }
        scopes = self._resolve_review_scopes(
            reviewer_ids,
            criteria,
            review_scope_by_agent,
        )

        route_models = {route.model for route in team_plan.model_routes.routes}
        if set(pricing_by_model) != route_models:
            raise ValueError("pricing evidence must exactly cover authorized models")
        prices = dict(pricing_by_model)
        if any(model != pricing.model for model, pricing in prices.items()):
            raise ValueError("pricing keys and model identities differ")

        starting_commit = input_commit or workspace.base_commit
        verified_commit = workspace_manager.verify_workspace(
            workspace,
            expected_commit=starting_commit,
            require_clean=True,
        )

        self.task_brief = task_brief
        self.implementation_plan = implementation_plan
        self.team_plan = team_plan
        self.workspace = workspace
        self.workspace_manager = workspace_manager
        self.artifact_store = artifact_store
        self.executor = executor
        self.quality_gate = quality_gate
        self.budget_ledger = budget_ledger
        self.pricing_by_model = prices
        self.manual_review_criteria = criteria
        self.review_scope_by_agent = scopes
        self.iteration = iteration
        self.input_commit = verified_commit
        self.artifact_repair_limit = artifact_repair_limit
        self.revision_feedback = revision_feedback
        self.revision_command_evidence = revision_command_evidence
        self.guidance_provider = guidance_provider
        self.invocation_stop_provider = invocation_stop_provider
        self.activity_handler = activity_handler
        self.clock = clock

        self._state_lock = RLock()
        self._quality_condition = Condition(self._state_lock)
        self._latest_commit = verified_commit
        self._outputs: dict[str, ArtifactReference] = {}
        self._execution_records: list[ArtifactReference] = []
        self._execution_by_agent: dict[str, list[ArtifactReference]] = {}
        self._handoffs: list[ArtifactReference] = []
        self._handoff_sequences: dict[str, int] = {}
        self._termination_reasons: dict[str, TerminationReason] = {}
        self._quality_state = "pending"
        self._quality_error: DynamicAgentRunnerError | None = None
        self._quality_commands: tuple[CommandEvidence, ...] = ()
        self._quality_commit: str | None = None
        self._quality_gate_calls = 0
        self._controller_test_reference: ArtifactReference | None = None
        self._last_verified_checkpoint: dict[str, str] = {}

    @staticmethod
    def _resolve_review_scopes(
        reviewer_ids: set[str],
        manual_criteria: tuple[str, ...],
        supplied: Mapping[str, tuple[str, ...]] | None,
    ) -> dict[str, tuple[str, ...]]:
        """Validate and distribute manual-review criteria across approved reviewers."""
        if manual_criteria and not reviewer_ids:
            raise ValueError("manual-review criteria require an approved Reviewer")
        if supplied is None:
            if len(reviewer_ids) == 1:
                return {next(iter(reviewer_ids)): manual_criteria}
            if manual_criteria:
                raise ValueError(
                    "multiple Reviewers require an explicit non-overlapping scope"
                )
            return {agent_id: () for agent_id in reviewer_ids}
        if set(supplied) != reviewer_ids:
            raise ValueError("review-scope keys must exactly cover approved Reviewers")
        resolved: dict[str, tuple[str, ...]] = {}
        flattened: list[str] = []
        for agent_id in sorted(reviewer_ids):
            scope = tuple(item.strip() for item in supplied[agent_id])
            if any(not item for item in scope) or len(scope) != len(set(scope)):
                raise ValueError("Reviewer scopes must contain unique criterion IDs")
            resolved[agent_id] = scope
            flattened.extend(scope)
        if len(flattened) != len(set(flattened)):
            raise ValueError("Reviewer scopes cannot overlap")
        if set(flattened) != set(manual_criteria):
            raise ValueError("Reviewer scopes must exactly cover manual criteria")
        return resolved

    @property
    def execution_records(self) -> tuple[ArtifactReference, ...]:
        """Return every recorded invocation in actual persistence order."""
        with self._state_lock:
            return tuple(self._execution_records)

    @property
    def handoffs(self) -> tuple[ArtifactReference, ...]:
        """Return every completed or failed durable handoff."""
        with self._state_lock:
            return tuple(self._handoffs)

    @property
    def outputs(self) -> dict[str, ArtifactReference]:
        """Return successful phase output references keyed by Agent ID."""
        with self._state_lock:
            return dict(self._outputs)

    @property
    def termination_reasons(self) -> dict[str, TerminationReason]:
        """Return controller classifications for failed Agents."""
        with self._state_lock:
            return dict(self._termination_reasons)

    @property
    def quality_gate_calls(self) -> int:
        """Expose deterministic-gate execution count for evidence and tests."""
        with self._state_lock:
            return self._quality_gate_calls

    @property
    def controller_test_reference(self) -> ArtifactReference | None:
        """Return controller-owned deterministic evidence when no Tester exists."""
        with self._state_lock:
            return self._controller_test_reference

    def __call__(
        self,
        agent: AgentSpec,
        upstream: Mapping[str, AgentRunOutcome],
    ) -> AgentRunOutcome:
        """Run one scheduler-approved Agent without changing scheduling policy."""
        if self.team_plan.get_agent(agent.id) != agent:
            return self._failed_outcome(
                agent,
                "scheduler supplied an AgentSpec outside the approved TeamPlan",
                TerminationReason.SAFETY_BOUNDARY_CROSSED,
                (),
            )
        incoming: tuple[ArtifactReference, ...] = ()
        try:
            incoming = self._persist_incoming_handoffs(agent, upstream)
            output = self._invoke_agent(agent, upstream)
            with self._state_lock:
                self._outputs[agent.id] = output
            executions = self._execution_references(agent.id)
            auxiliary = self._quality_auxiliary_references(agent)
            terminal = self._persist_terminal_handoff(
                agent,
                output=output,
                executions=executions,
                failed_detail=None,
            )
            evidence = self._unique_references(
                (*incoming, *executions, *auxiliary, *terminal)
            )
            artifact = self.artifact_store.load(output)
            typed_artifact = cast(WorkResult | TestReport | ReviewReport, artifact)
            summary = typed_artifact.summary
            return AgentRunOutcome(
                agent_id=agent.id,
                status=AgentRunStatus.COMPLETED,
                output=output,
                evidence=evidence,
                requires_quality_decision=(
                    isinstance(typed_artifact, TestReport)
                    and typed_artifact.status is not CheckStatus.PASSED
                ),
                summary=_bounded_artifact_summary(
                    summary,
                    limit=_SCHEDULER_SUMMARY_LIMIT,
                ),
            )
        except Exception as error:
            reason = self._classify_exception(error)
            detail = self._error_detail(error)
            with self._state_lock:
                self._termination_reasons[agent.id] = reason
            executions = self._execution_references(agent.id)
            auxiliary = self._quality_auxiliary_references(agent)
            terminal = self._persist_terminal_handoff(
                agent,
                output=None,
                executions=executions,
                failed_detail=detail,
            )
            evidence = self._unique_references(
                (*incoming, *executions, *auxiliary, *terminal)
            )
            return self._failed_outcome(agent, detail, reason, evidence)
