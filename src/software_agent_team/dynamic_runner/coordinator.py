"""Progress checkpoint coordination, execution invocation, and response assembly mixin.

This module provides the CoordinatorMixin which coordinates execution requests,
execution result validation, response assembly (work result, test report, review
report), and progress checkpoint snapshotting.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from threading import RLock
from typing import TYPE_CHECKING

from software_agent_team.artifacts import (
    AgentToolEvidenceStatus,
    CommandEvidence,
    ReviewReport,
    TaskBrief,
    TestReport,
    WorkResult,
)
from software_agent_team.assembly import (
    ArtifactAssemblyError,
    assemble_review_report,
    assemble_test_report,
    assemble_unchanged_integration_result,
    assemble_work_result,
)
from software_agent_team.budgets import AgentBudgetLedger
from software_agent_team.execution import (
    AgentExecutionActivity,
    AgentExecutionRequest,
    AgentExecutionResult,
    AgentExecutionStatus,
    AgentExecutor,
    execution_exception_result,
)
from software_agent_team.git_workspace import GitSnapshot
from software_agent_team.invocation_lifecycle import InvocationPhase
from software_agent_team.planning import AdaptiveImplementationPlan
from software_agent_team.progress import ProgressCheckpointSnapshot
from software_agent_team.responses import (
    GroundedReviewReportResponse,
    TestReportResponse,
    WorkResultResponse,
)
from software_agent_team.run_control import TerminationReason
from software_agent_team.scheduling import AgentRunOutcome
from software_agent_team.teams import (
    AgentCapability,
    AgentSpec,
    PermissionProfile,
    TeamPlan,
)

from .types import DynamicAgentRunnerError, _utc

if TYPE_CHECKING:
    pass


class CoordinatorMixin:
    """Methods for checkpoints, request execution, and response assembly."""

    executor: AgentExecutor
    budget_ledger: AgentBudgetLedger
    implementation_plan: AdaptiveImplementationPlan
    task_brief: TaskBrief
    team_plan: TeamPlan
    iteration: int
    clock: Callable[[], datetime]
    manual_review_criteria: tuple[str, ...]
    review_scope_by_agent: dict[str, tuple[str, ...]]

    _state_lock: RLock
    _quality_state: str
    _last_verified_checkpoint: dict[str, str]

    def _checkpoint_snapshot(
        self,
        agent: AgentSpec,
        *,
        phase: InvocationPhase,
        last_verified_checkpoint: str | None,
        next_controller_checkpoint: str,
        completed_tool_operations: int,
        review_grounded: bool = False,
    ) -> ProgressCheckpointSnapshot:
        """Freeze one safe task-progress projection from Controller-owned facts."""
        with self._state_lock:
            if last_verified_checkpoint is not None:
                self._last_verified_checkpoint[agent.id] = last_verified_checkpoint
            last = self._last_verified_checkpoint.get(
                agent.id,
                "Controller admitted this invocation under the approved plan",
            )
            quality_state = self._quality_state
        usage = self.budget_ledger.snapshot()
        authorized = self.budget_ledger.budget.max_estimated_cost_usd
        assigned_task_ids = tuple(
            task.id
            for task in self.implementation_plan.tasks
            if task.owner_agent_id == agent.id
        )
        gate_state = {
            "pending": "not_started",
            "running": "running",
            "ready": "passed",
            "failed": "failed",
        }[quality_state]
        review_criteria = (
            self.review_scope_by_agent.get(agent.id, ())
            if agent.capability is AgentCapability.REVIEW
            else ()
        )
        if usage.active_calls:
            cost_accounting_state = "active_unsettled"
        elif usage.unpriced_calls:
            cost_accounting_state = "terminal_incomplete"
        else:
            cost_accounting_state = "settled"
        return ProgressCheckpointSnapshot(
            approved_task_ids=assigned_task_ids,
            invocation_phase=phase,
            last_verified_checkpoint=last,
            next_controller_checkpoint=next_controller_checkpoint,
            completed_tool_operations=completed_tool_operations,
            git_state=(
                "working"
                if agent.permission_profile is PermissionProfile.WORKSPACE_WRITE
                else "verified"
            ),
            gate_state=gate_state,
            review_state=(
                "running"
                if agent.capability is AgentCapability.REVIEW
                else "not_applicable"
            ),
            review_criterion_ids=review_criteria,
            review_coverage_state=(
                "grounded"
                if review_grounded and review_criteria
                else "unverified"
                if review_criteria
                else "not_applicable"
            ),
            known_estimated_cost_usd=usage.known_estimated_cost_usd,
            authorized_cost_usd=authorized,
            remaining_estimated_cost_usd=usage.remaining_estimated_cost_usd(
                self.budget_ledger.budget
            ),
            active_reserved_cost_usd=usage.active_reserved_cost_usd,
            cost_accounting_state=cost_accounting_state,
            unsettled_model_calls=usage.active_calls,
        )

    def _execute(
        self,
        request: AgentExecutionRequest,
        *,
        activity_handler: Callable[[AgentExecutionActivity], None],
    ) -> AgentExecutionResult:
        """Run an execution request through the configured AgentExecutor."""
        started_at = _utc(self.clock)
        try:
            return self.executor.execute(
                request,
                activity_handler=activity_handler,
            )
        except Exception as error:
            return execution_exception_result(
                request, error, started_at=started_at, finished_at=_utc(self.clock)
            )

    def _validate_execution_result(
        self,
        result: AgentExecutionResult,
        request: AgentExecutionRequest,
    ) -> None:
        """Enforce execution telemetry invariants before accepting an invocation."""
        telemetry = result.telemetry
        if (
            telemetry.role is not None
            or telemetry.agent_id != request.agent_id
            or telemetry.capability is not request.capability
            or telemetry.specialization is not request.specialization
            or telemetry.session_key != request.session_key
        ):
            raise DynamicAgentRunnerError(
                "execution telemetry identity differs from the approved AgentSpec",
                TerminationReason.SAFETY_BOUNDARY_CROSSED,
            )
        if result.status is not AgentExecutionStatus.COMPLETED:
            return
        if telemetry.model is None:
            raise DynamicAgentRunnerError(
                "successful execution omitted model metadata",
                TerminationReason.DEPENDENCY_UNAVAILABLE,
            )
        if telemetry.model != request.model:
            raise DynamicAgentRunnerError(
                "execution model differs from the approved model route",
                TerminationReason.SAFETY_BOUNDARY_CROSSED,
            )
        if telemetry.provider is None:
            raise DynamicAgentRunnerError(
                "successful execution omitted provider metadata",
                TerminationReason.DEPENDENCY_UNAVAILABLE,
            )
        route = next(
            route
            for route in self.team_plan.model_routes.routes
            if route.model == request.model
        )
        if telemetry.provider != route.runtime_profile.provider_id:
            raise DynamicAgentRunnerError(
                "execution provider differs from the approved runtime profile",
                TerminationReason.SAFETY_BOUNDARY_CROSSED,
            )
        if (
            telemetry.usage is None
            or telemetry.usage.input_tokens is None
            or telemetry.usage.output_tokens is None
        ):
            raise DynamicAgentRunnerError(
                "successful execution omitted token usage",
                TerminationReason.DEPENDENCY_UNAVAILABLE,
            )
        if sandbox_error := self._sandbox_runtime_error(result):  # type: ignore[attr-defined]
            raise DynamicAgentRunnerError(
                sandbox_error,
                TerminationReason.DEPENDENCY_UNAVAILABLE,
            )
        if request.capability is AgentCapability.REVIEW:
            if telemetry.tool_evidence_status is AgentToolEvidenceStatus.INVALID:
                raise DynamicAgentRunnerError(
                    "Reviewer session evidence failed integrity validation: "
                    f"{telemetry.tool_evidence_error or 'unknown session error'}",
                    TerminationReason.SAFETY_BOUNDARY_CROSSED,
                )
            if telemetry.tool_evidence_status is not AgentToolEvidenceStatus.CAPTURED:
                raise DynamicAgentRunnerError(
                    "Reviewer execution omitted attributable session tool evidence",
                    TerminationReason.DEPENDENCY_UNAVAILABLE,
                )

    def _assemble_response(
        self,
        agent: AgentSpec,
        body: object,
        *,
        input_commit: str,
        commands: tuple[CommandEvidence, ...],
        manual_scope: tuple[str, ...],
        snapshot: GitSnapshot | None,
        result: AgentExecutionResult,
        upstream: Mapping[str, AgentRunOutcome],
    ) -> WorkResult | TestReport | ReviewReport:
        """Assemble a typed response artifact from raw execution output."""
        created_at = _utc(self.clock)
        if agent.capability in {
            AgentCapability.IMPLEMENTATION,
            AgentCapability.INTEGRATION,
        }:
            if not isinstance(body, WorkResultResponse):
                raise ArtifactAssemblyError(
                    "implementation Agent returned the wrong semantic body"
                )
            if snapshot is None:
                if self._verified_unchanged_integration(  # type: ignore[attr-defined]
                    agent,
                    body=body,
                    input_commit=input_commit,
                    result=result,
                    upstream=upstream,
                ):
                    return assemble_unchanged_integration_result(
                        body,
                        task_brief=self.task_brief,
                        team_id=self.team_plan.team_id,
                        agent=agent,
                        iteration=self.iteration,
                        input_commit=input_commit,
                        created_at=created_at,
                    )
                raise DynamicAgentRunnerError(
                    f"{agent.label} produced no controller-verified committed change",
                    TerminationReason.NO_RELEVANT_CHANGE,
                )
            return assemble_work_result(
                body,
                task_brief=self.task_brief,
                team_id=self.team_plan.team_id,
                agent=agent,
                snapshot=snapshot,
                created_at=created_at,
            )
        if agent.capability is AgentCapability.TESTING:
            if not isinstance(body, TestReportResponse):
                raise ArtifactAssemblyError("Tester returned the wrong semantic body")
            return assemble_test_report(
                body,
                task_brief=self.task_brief,
                team_id=self.team_plan.team_id,
                agent=agent,
                iteration=self.iteration,
                input_commit=input_commit,
                commands=commands,
                manual_review_criteria=self.manual_review_criteria,
                created_at=created_at,
            )
        if agent.capability is AgentCapability.REVIEW:
            if not isinstance(body, GroundedReviewReportResponse):
                raise ArtifactAssemblyError("Reviewer returned the wrong semantic body")
            return assemble_review_report(
                body,
                task_brief=self.task_brief,
                team_id=self.team_plan.team_id,
                agent=agent,
                iteration=self.iteration,
                input_commit=input_commit,
                reviewed_criteria=manual_scope,
                created_at=created_at,
            )
        raise ArtifactAssemblyError(
            f"unsupported dynamic capability: {agent.capability.value}"
        )
