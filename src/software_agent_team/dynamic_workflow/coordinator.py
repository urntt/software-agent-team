"""Adaptive dynamic workflow coordinator driving controlled lifecycle execution."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from software_agent_team.artifact_store import ArtifactStore
from software_agent_team.budgets import (
    AgentBudgetLedger,
    ModelPricing,
)
from software_agent_team.controls import ControlCommandStore
from software_agent_team.dynamic_workflow.decisions import (
    _blocking_reasons,
    _complete,
    _decide,
    _decision_summary,
    _decision_termination_reason,
    _error_detail,
    _fail,
    _outcome,
    _render_report,
    _stop_by_user,
    _termination_reason,
    _write_terminal_report,
)
from software_agent_team.dynamic_workflow.invocation import (
    _capture_runner_outputs,
    _event_digest,
    _execute_approved_plan,
    _load_artifacts,
    _references_for_capabilities,
    _validate_work_chain,
)
from software_agent_team.dynamic_workflow.types import (
    Clock,
    ControlStoreCleanup,
    ControlStoreHandler,
    DynamicQualityGateFactory,
    DynamicWorkflowError,
    DynamicWorkflowOutcome,
    RuntimeSetup,
    _DynamicWorkflowContext,
    _system_clock,
)
from software_agent_team.execution import AgentExecutor
from software_agent_team.git_workspace import GitWorkspaceManager
from software_agent_team.planning import (
    ApprovedPlanningResult,
    PlanningError,
    compile_approved_review_scopes,
)
from software_agent_team.progress import (
    ProgressEvent,
    ProgressEventKind,
    ProgressHandler,
    RunEvent,
    RunEventJournal,
)
from software_agent_team.run_control import (
    RunController,
    RunPhase,
    RunStore,
)
from software_agent_team.runtime_controls import RuntimeControlChannel
from software_agent_team.teams import TeamPlanOrigin
from software_agent_team.versioning import SoftwareVersionReport


def _emit(
    context: _DynamicWorkflowContext,
    event: ProgressEvent,
) -> RunEvent:
    """Record a lifecycle progress event to the run journal."""
    current = context.controller.load(context.approved.task_brief.run_id)
    return context.event_journal.append(
        event,
        lifecycle_revision=current.revision,
        phase=current.phase,
    )


class DynamicWorkflowCoordinator:
    """Advance one approved adaptive plan through the authoritative lifecycle."""

    _emit = staticmethod(_emit)
    _event_digest = staticmethod(_event_digest)
    _references_for_capabilities = staticmethod(_references_for_capabilities)
    _validate_work_chain = staticmethod(_validate_work_chain)
    _load_artifacts = staticmethod(_load_artifacts)
    _capture_runner_outputs = staticmethod(_capture_runner_outputs)
    _decide = staticmethod(_decide)
    _blocking_reasons = staticmethod(_blocking_reasons)
    _decision_summary = staticmethod(_decision_summary)
    _decision_termination_reason = staticmethod(_decision_termination_reason)
    _outcome = staticmethod(_outcome)
    _render_report = staticmethod(_render_report)
    _write_terminal_report = _write_terminal_report
    _termination_reason = staticmethod(_termination_reason)
    _error_detail = staticmethod(_error_detail)
    _execute_approved_plan = _execute_approved_plan
    _complete = _complete
    _fail = _fail
    _stop_by_user = _stop_by_user

    def __init__(
        self,
        *,
        runs_root: Path,
        workspaces_root: Path,
        executor: AgentExecutor,
        quality_gate_factory: DynamicQualityGateFactory,
        pricing_by_model: Mapping[str, ModelPricing],
        software_version: SoftwareVersionReport,
        budget_ledger: AgentBudgetLedger | None = None,
        runtime_setup: RuntimeSetup | None = None,
        manual_review_criteria: tuple[str, ...] = (),
        review_scope_by_agent: Mapping[str, tuple[str, ...]] | None = None,
        artifact_repair_limit: int | None = None,
        progress_handler: ProgressHandler | None = None,
        control_store_handler: ControlStoreHandler | None = None,
        clock: Clock = _system_clock,
    ) -> None:
        if artifact_repair_limit not in {None, 0, 1}:
            raise DynamicWorkflowError(
                "controlled dynamic evaluation permits zero or one correction"
            )
        criteria = tuple(item.strip() for item in manual_review_criteria)
        if any(not item for item in criteria) or len(criteria) != len(set(criteria)):
            raise DynamicWorkflowError(
                "manual-review criterion IDs must be non-empty and unique"
            )
        prices = dict(pricing_by_model)
        if not prices or any(model != price.model for model, price in prices.items()):
            raise DynamicWorkflowError(
                "pricing keys must exactly identify their configured models"
            )
        self.runs_root = runs_root
        self.workspaces_root = workspaces_root
        self.executor = executor
        self.quality_gate_factory = quality_gate_factory
        self.pricing_by_model = prices
        self.software_version = software_version
        self.budget_ledger = budget_ledger
        self.runtime_setup = runtime_setup
        self.manual_review_criteria = criteria
        self.review_scope_by_agent = review_scope_by_agent
        self.artifact_repair_limit = artifact_repair_limit
        self.progress_handler = progress_handler
        self.control_store_handler = control_store_handler
        self.clock = clock

    def execute(
        self,
        approved: ApprovedPlanningResult,
        *,
        source_repository: Path,
        base_ref: str = "HEAD",
    ) -> DynamicWorkflowOutcome:
        """Execute a fresh user-approved adaptive run to one terminal report."""
        team_plan = approved.team_plan
        if team_plan.origin is not TeamPlanOrigin.ADAPTIVE_PLANNING:
            raise DynamicWorkflowError("dynamic workflow requires an adaptive TeamPlan")
        known_criteria = {
            criterion.id for criterion in approved.task_brief.acceptance_criteria
        }
        if not set(self.manual_review_criteria).issubset(known_criteria):
            raise DynamicWorkflowError(
                "manual-review scope references an unknown criterion"
            )
        try:
            approved_review_scopes = compile_approved_review_scopes(
                approved,
                manual_review_criteria=self.manual_review_criteria,
            )
        except PlanningError as error:
            raise DynamicWorkflowError(str(error)) from error
        if approved_review_scopes is not None:
            if (
                self.review_scope_by_agent is not None
                and dict(self.review_scope_by_agent) != approved_review_scopes
            ):
                raise DynamicWorkflowError(
                    "runtime Review scopes differ from the approved acceptance strategy"
                )
            review_scope_by_agent = approved_review_scopes
        else:
            review_scope_by_agent = self.review_scope_by_agent
        route_models = {route.model for route in team_plan.model_routes.routes}
        if set(self.pricing_by_model) != route_models:
            raise DynamicWorkflowError(
                "pricing evidence must exactly cover approved model routes"
            )
        if (
            self.budget_ledger is not None
            and self.budget_ledger.budget != team_plan.budget
        ):
            raise DynamicWorkflowError(
                "shared task budget ledger does not match the approved TeamPlan"
            )

        controller = RunController(RunStore(self.runs_root), None, clock=self.clock)
        record = controller.create(approved.task_brief, team_plan=team_plan)
        run_directory = self.runs_root / record.run_id

        def read_event_anchor() -> tuple[int, str | None]:
            current = controller.load(record.run_id)
            return current.event_count, current.event_head_sha256

        control_store = ControlCommandStore(
            run_directory,
            run_id=record.run_id,
            clock=self.clock,
        )
        event_journal = RunEventJournal(
            run_directory,
            run_id=record.run_id,
            handler=self.progress_handler,
            clock=self.clock,
            anchor_writer=lambda event: controller.record_event_head(
                event.run_id,
                event_sequence=event.sequence,
                event_sha256=self._event_digest(event),
                occurred_at=event.occurred_at,
            ),
            anchor_reader=read_event_anchor,
        )

        def emit_control(event: ProgressEvent) -> None:
            current = controller.load(record.run_id)
            event_journal.append(
                event,
                lifecycle_revision=current.revision,
                phase=current.phase,
            )

        interrupt_agent = getattr(self.executor, "interrupt", None)
        interrupt_all = getattr(self.executor, "interrupt_all", None)
        control_channel = RuntimeControlChannel(
            store=control_store,
            team_plan=team_plan,
            interrupt_agent=(
                interrupt_agent if callable(interrupt_agent) else lambda agent_id: 0
            ),
            interrupt_all=(interrupt_all if callable(interrupt_all) else lambda: 0),
            phase_reader=lambda: controller.load(record.run_id).phase,
            event_handler=emit_control,
        )
        context = _DynamicWorkflowContext(
            approved=approved,
            controller=controller,
            artifact_store=ArtifactStore(
                run_directory,
                task_brief=approved.task_brief,
                team_plan=team_plan,
            ),
            event_journal=event_journal,
            run_directory=run_directory,
            budget_ledger=(self.budget_ledger or AgentBudgetLedger(team_plan.budget)),
            control_channel=control_channel,
        )
        control_cleanup: ControlStoreCleanup | None = None
        self._emit(
            context,
            ProgressEvent(
                kind=ProgressEventKind.RUN_STARTED,
                message=f"Build started (run {record.run_id})",
                phase=RunPhase.CREATED,
                iteration=1,
            ),
        )

        try:
            if self.control_store_handler is not None:
                control_cleanup = self.control_store_handler(control_store, team_plan)
            record = controller.advance(
                record.run_id,
                expected_revision=record.revision,
                target=RunPhase.PREPARING_WORKSPACE,
                reason="prepare isolated Git workspace",
            )
            workspace_manager = GitWorkspaceManager(
                self.workspaces_root,
                clock=self.clock,
            )
            workspace = workspace_manager.prepare(
                record.run_id,
                source_repository=source_repository,
                base_ref=base_ref,
            )
            record = controller.attach_workspace(
                record.run_id,
                expected_revision=record.revision,
                workspace=workspace,
            )
            if self.runtime_setup is not None:
                self.runtime_setup(workspace, run_directory)
            self._emit(
                context,
                ProgressEvent(
                    kind=ProgressEventKind.WORKSPACE_READY,
                    message="Isolated workspace and runtime verified",
                    phase=RunPhase.PLANNING,
                    iteration=record.current_iteration,
                ),
            )
            quality_gate = self.quality_gate_factory(
                run_directory,
                Path(workspace.workspace_path),
                lambda event: self._emit(context, event),
            )
            return self._execute_approved_plan(
                context,
                record,
                workspace,
                workspace_manager,
                quality_gate,
                review_scope_by_agent,
            )
        except Exception as error:
            current = controller.load(record.run_id)
            if current.phase.is_terminal:
                raise
            return self._fail(
                context,
                current,
                reason=self._termination_reason(error),
                detail=self._error_detail(error),
            )
        finally:
            if control_cleanup is not None:
                control_cleanup()


__all__ = [
    "DynamicWorkflowCoordinator",
    "_emit",
]
