"""Workflow coordinator and top-level execution routines."""

from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import cast

from software_agent_team.artifact_store import ArtifactStore
from software_agent_team.artifacts import (
    AgentRole,
    ArtifactKind,
    ArtifactReference,
    CheckStatus,
    CommandEvidence,
    ImplementationPlan,
    IterationDecision,
    IterationRecord,
    PhaseArtifact,
    ReviewReport,
    TaskBrief,
    TestReport,
    WorkResult,
)
from software_agent_team.assembly import assemble_test_report
from software_agent_team.budgets import (
    AgentBudget,
    AgentBudgetLedger,
    ModelPricing,
)
from software_agent_team.execution import AgentExecutor
from software_agent_team.git_workspace import (
    GitSnapshot,
    GitWorkspace,
    GitWorkspaceManager,
    validate_work_result_snapshot,
)
from software_agent_team.integrity import canonical_model_sha256
from software_agent_team.progress import (
    ProgressEvent,
    ProgressEventKind,
    ProgressHandler,
    RunEvent,
    RunEventJournal,
)
from software_agent_team.prompting import AgentPromptInputs
from software_agent_team.responses import AgentResponseBody
from software_agent_team.run_control import (
    RunController,
    RunPhase,
    RunRecord,
    RunStore,
    TerminationReason,
)
from software_agent_team.teams import TeamManifest, compile_fixed_team_plan
from software_agent_team.versioning import SoftwareVersionReport
from software_agent_team.workflow.assembly import (
    _assemble_plan,
    _assemble_review_report,
    _assemble_test_report,
    _assemble_work_result,
    _validate_verification_assignment,
)
from software_agent_team.workflow.decisions import (
    _blocking_reasons,
    _complete,
    _decide,
    _decision_summary,
    _decision_termination_reason,
    _fail,
    _outcome,
    _render_report,
    _write_terminal_report,
)
from software_agent_team.workflow.invocation import (
    _agent_termination_reason,
    _error_detail,
    _execute_agent,
    _handoff,
    _invoke,
    _record_execution,
    _sandbox_runtime_error,
    _termination_reason,
)
from software_agent_team.workflow.types import (
    PHASE1_ITERATION_LIMIT,
    PHASE1_TEAM_ID,
    Clock,
    QualityGate,
    QualityGateFactory,
    RuntimeSetup,
    WorkflowError,
    WorkflowEvidenceError,
    WorkflowOutcome,
    _system_clock,
    _unique,
    _utc,
    _WorkflowContext,
)


def _emit(context: _WorkflowContext, event: ProgressEvent) -> RunEvent:
    """Persist one controller event before notifying presentation code."""
    record = context.controller.load(context.brief.run_id)
    return context.event_journal.append(
        event,
        lifecycle_revision=record.revision,
        phase=record.phase,
    )


class WorkflowCoordinator:
    """Execute a fixed team or one-writer evaluation through shared boundaries."""

    _emit = staticmethod(_emit)
    _validate_verification_assignment = _validate_verification_assignment
    _assemble_plan = _assemble_plan
    _assemble_work_result = _assemble_work_result
    _assemble_test_report = _assemble_test_report
    _assemble_review_report = _assemble_review_report
    _invoke = _invoke
    _execute_agent = _execute_agent
    _record_execution = _record_execution
    _handoff = _handoff
    _decide = staticmethod(_decide)
    _blocking_reasons = staticmethod(_blocking_reasons)
    _decision_summary = staticmethod(_decision_summary)
    _decision_termination_reason = staticmethod(_decision_termination_reason)
    _complete = _complete
    _fail = _fail
    _outcome = staticmethod(_outcome)
    _render_report = _render_report
    _write_terminal_report = _write_terminal_report
    _agent_termination_reason = staticmethod(_agent_termination_reason)
    _sandbox_runtime_error = staticmethod(_sandbox_runtime_error)
    _termination_reason = staticmethod(_termination_reason)
    _error_detail = staticmethod(_error_detail)

    def __init__(
        self,
        *,
        manifest: TeamManifest,
        runs_root: Path,
        workspaces_root: Path,
        executor: AgentExecutor,
        quality_gate_factory: QualityGateFactory,
        budget: AgentBudget,
        pricing: ModelPricing,
        software_version: SoftwareVersionReport,
        runtime_setup: RuntimeSetup | None = None,
        manual_review_criteria: tuple[str, ...] = (),
        role_timeout_seconds: Mapping[AgentRole, int],
        stage_timeout_seconds: int | None = None,
        artifact_repair_limit: int = 1,
        iteration_limit: int = PHASE1_ITERATION_LIMIT,
        verification_concurrency: int = 2,
        team_id: str = PHASE1_TEAM_ID,
        progress_handler: ProgressHandler | None = None,
        clock: Clock = _system_clock,
    ) -> None:
        missing_timeouts = set(AgentRole) - set(role_timeout_seconds)
        if missing_timeouts:
            names = ", ".join(sorted(role.value for role in missing_timeouts))
            raise WorkflowError(f"Agent invocation timeouts are missing roles: {names}")
        if any(
            isinstance(seconds, bool) or not 1 <= seconds <= 3600
            for seconds in role_timeout_seconds.values()
        ):
            raise WorkflowError("Agent invocation timeouts must be between 1 and 3600")
        if stage_timeout_seconds is not None and not 1 <= stage_timeout_seconds <= 3600:
            raise WorkflowError(
                "global Agent invocation timeout must be 1 to 3600 seconds"
            )
        if artifact_repair_limit not in {0, 1}:
            raise WorkflowError("Phase 1 permits zero or one artifact repair")
        if verification_concurrency not in {1, 2}:
            raise WorkflowError("verification concurrency must be one or two")
        cleaned_manual_criteria = _unique(manual_review_criteria)
        if len(cleaned_manual_criteria) != len(manual_review_criteria):
            raise WorkflowError(
                "manual-review criterion IDs must be non-empty and unique"
            )
        if team_id not in {PHASE1_TEAM_ID, "single_agent"}:
            raise WorkflowError("fixed evaluation supports only the selected workflows")
        team = manifest.get_team(team_id)
        if (
            team_id == PHASE1_TEAM_ID
            and AgentRole.GENERALIST_DEVELOPER not in team.roles
        ):
            raise WorkflowError("Phase 1 team is missing the generalist developer")
        if team_id == "single_agent" and verification_concurrency != 1:
            raise WorkflowError("single-agent evaluation requires concurrency one")
        if team_id == "single_agent" and cleaned_manual_criteria:
            raise WorkflowError(
                "single-agent evaluation requires a fully deterministic acceptance "
                "manifest; manual review belongs to the external comparison"
            )
        if (
            isinstance(iteration_limit, bool)
            or not 1 <= iteration_limit <= team.max_iterations
        ):
            raise WorkflowError(
                "iteration limit must be between 1 and "
                f"{team.max_iterations} for {team.id}"
            )
        self.manifest = manifest
        self.team_id = team_id
        self.runs_root = runs_root
        self.workspaces_root = workspaces_root
        self.executor = executor
        self.quality_gate_factory = quality_gate_factory
        self.budget = budget
        self.pricing = pricing
        self.software_version = software_version
        self.runtime_setup = runtime_setup
        self.manual_review_criteria = cleaned_manual_criteria
        self.agent_stage_timeouts_seconds = {
            role: (
                stage_timeout_seconds
                if stage_timeout_seconds is not None
                else role_timeout_seconds[role]
            )
            for role in AgentRole
        }
        self.artifact_repair_limit = artifact_repair_limit
        self.iteration_limit = iteration_limit
        self.verification_concurrency = verification_concurrency
        self.progress_handler = progress_handler
        self.clock = clock

    def execute(
        self,
        task_brief: TaskBrief,
        *,
        source_repository: Path,
        base_ref: str = "HEAD",
    ) -> WorkflowOutcome:
        """Start a fresh run and return a valid completed or failed outcome."""
        known_criteria = {criterion.id for criterion in task_brief.acceptance_criteria}
        if not set(self.manual_review_criteria).issubset(known_criteria):
            raise WorkflowError("manual-review scope references an unknown criterion")
        team_plan = compile_fixed_team_plan(
            self.manifest,
            team_id=self.team_id,
            run_id=task_brief.run_id,
            task_brief_sha256=canonical_model_sha256(task_brief),
            model=self.pricing.model,
            budget=self.budget,
            role_timeout_seconds=self.agent_stage_timeouts_seconds,
            iteration_limit=self.iteration_limit,
            max_concurrency=self.verification_concurrency,
            created_at=_utc(self.clock),
        )
        controller = RunController(
            RunStore(self.runs_root),
            self.manifest,
            clock=self.clock,
        )
        record = controller.create(
            task_brief,
            team_plan=team_plan,
        )
        run_directory = self.runs_root / task_brief.run_id

        def read_event_anchor() -> tuple[int, str | None]:
            current = controller.load(task_brief.run_id)
            return current.event_count, current.event_head_sha256

        context = _WorkflowContext(
            brief=task_brief,
            team_plan=team_plan,
            controller=controller,
            artifact_store=ArtifactStore(
                run_directory,
                task_brief=task_brief,
                team_plan=team_plan,
            ),
            event_journal=RunEventJournal(
                run_directory,
                run_id=task_brief.run_id,
                handler=self.progress_handler,
                clock=self.clock,
                anchor_writer=lambda event: controller.record_event_head(
                    event.run_id,
                    event_sequence=event.sequence,
                    event_sha256=canonical_model_sha256(event),
                    occurred_at=event.occurred_at,
                ),
                anchor_reader=read_event_anchor,
            ),
            run_directory=run_directory,
            budget_ledger=AgentBudgetLedger(self.budget),
        )
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
            execute_run = (
                self._execute_single_agent_run
                if self.team_id == "single_agent"
                else self._execute_planned_run
            )
            return execute_run(
                context,
                record,
                workspace,
                workspace_manager,
                quality_gate,
            )
        except Exception as error:
            current = controller.load(task_brief.run_id)
            if current.phase.is_terminal:
                raise
            return self._fail(
                context,
                current,
                reason=self._termination_reason(error),
                detail=self._error_detail(error),
            )

    def _execute_planned_run(
        self,
        context: _WorkflowContext,
        record: RunRecord,
        workspace: GitWorkspace,
        workspace_manager: GitWorkspaceManager,
        quality_gate: QualityGate,
    ) -> WorkflowOutcome:
        plan_artifact, plan_reference, plan_execution = self._invoke(
            context,
            AgentPromptInputs(
                task_brief=context.brief,
                team_id=context.team_plan.team_id,
                team_roles=frozenset(context.team_plan.legacy_roles),
                iteration=1,
                iteration_limit=self.iteration_limit,
                role=AgentRole.PLANNER,
                expected_kind=ArtifactKind.IMPLEMENTATION_PLAN,
            ),
            stage="plan",
            assembler=lambda body: self._assemble_plan(context, body),
        )
        plan = cast(ImplementationPlan, plan_artifact)
        self._handoff(
            context,
            iteration=1,
            stage="plan",
            source=AgentRole.PLANNER,
            target=AgentRole.GENERALIST_DEVELOPER,
            input_commit=workspace.base_commit,
            artifacts=(plan_reference, plan_execution),
            summary="Planner supplied the frozen implementation plan.",
        )
        record = context.controller.advance(
            record.run_id,
            expected_revision=record.revision,
            target=RunPhase.IMPLEMENTING,
            reason="validated implementation plan is ready",
            artifacts=(plan_reference,),
        )

        feedback: tuple[TestReport | ReviewReport | IterationRecord, ...] = ()
        previous_blocking_ids: tuple[str, ...] = ()
        while True:
            input_commit = record.current_commit
            if input_commit is None:
                raise WorkflowEvidenceError("implementation input commit is missing")
            upstream: tuple[
                ImplementationPlan | TestReport | ReviewReport | IterationRecord,
                ...,
            ] = (plan, *feedback)
            verified_snapshot: list[GitSnapshot] = []

            def assemble_work(
                body: AgentResponseBody,
                snapshots: list[GitSnapshot] = verified_snapshot,
            ) -> PhaseArtifact:
                if len(snapshots) != 1:
                    raise WorkflowEvidenceError(
                        "Developer snapshot was not frozen after invocation"
                    )
                return self._assemble_work_result(
                    context,
                    body,
                    snapshot=snapshots[0],
                )

            def guard_work_invocation(
                is_correction: bool,
                current_record: RunRecord = record,
                current_input_commit: str = input_commit,
                snapshots: list[GitSnapshot] = verified_snapshot,
            ) -> None:
                if not snapshots:
                    snapshots.append(
                        workspace_manager.verify_snapshot(
                            workspace,
                            iteration=current_record.current_iteration,
                            input_commit=current_input_commit,
                        )
                    )
                    return
                if not is_correction:
                    raise WorkflowEvidenceError(
                        "Developer workspace guard observed an unexpected invocation"
                    )
                workspace_manager.verify_workspace(
                    workspace,
                    expected_commit=snapshots[0].output_commit,
                    require_clean=True,
                )

            work_artifact, work_reference, work_execution = self._invoke(
                context,
                AgentPromptInputs(
                    task_brief=context.brief,
                    team_id=context.team_plan.team_id,
                    team_roles=frozenset(context.team_plan.legacy_roles),
                    iteration=record.current_iteration,
                    iteration_limit=self.iteration_limit,
                    role=AgentRole.GENERALIST_DEVELOPER,
                    expected_kind=ArtifactKind.WORK_RESULT,
                    input_commit=input_commit,
                    upstream_artifacts=upstream,
                ),
                stage="implement",
                assembler=assemble_work,
                invocation_guard=guard_work_invocation,
            )
            work = cast(WorkResult, work_artifact)
            if len(verified_snapshot) != 1:
                raise WorkflowEvidenceError("Developer snapshot was not assembled once")
            snapshot = verified_snapshot[0]
            record = context.controller.advance(
                record.run_id,
                expected_revision=record.revision,
                target=RunPhase.SNAPSHOTTING,
                reason="Developer reported a committed implementation",
                artifacts=(work_reference,),
            )
            validate_work_result_snapshot(work, snapshot)
            record = context.controller.record_snapshot(
                record.run_id,
                expected_revision=record.revision,
                snapshot=snapshot,
            )
            self._emit(
                context,
                ProgressEvent(
                    kind=ProgressEventKind.SNAPSHOT_VERIFIED,
                    message=(
                        "Git snapshot verified: "
                        f"{len(snapshot.changed_files)} files changed"
                    ),
                    phase=RunPhase.VERIFYING,
                    iteration=record.current_iteration,
                    changed_files=snapshot.changed_files,
                ),
            )
            for target in (AgentRole.TESTER, AgentRole.REVIEWER):
                self._handoff(
                    context,
                    iteration=record.current_iteration,
                    stage="implement",
                    source=AgentRole.GENERALIST_DEVELOPER,
                    target=target,
                    input_commit=snapshot.output_commit,
                    artifacts=(work_reference, work_execution),
                    summary="Developer supplied controller-verified commit evidence.",
                )

            self._emit(
                context,
                ProgressEvent(
                    kind=ProgressEventKind.QUALITY_GATES_STARTED,
                    message="Running deterministic quality gates",
                    phase=RunPhase.VERIFYING,
                    iteration=record.current_iteration,
                ),
            )
            commands = quality_gate.run(iteration=record.current_iteration)
            if not commands:
                raise WorkflowEvidenceError("quality gate returned no command evidence")
            self._validate_verification_assignment(context.brief, commands)
            context.command_evidence.extend(commands)
            tester_result, reviewer_result = self._verify(
                context,
                record=record,
                work=work,
                commands=commands,
            )
            test, test_reference, test_execution = tester_result
            review, review_reference, review_execution = reviewer_result
            if test.commands != commands:
                raise WorkflowEvidenceError(
                    "Tester command evidence differs from controller evidence"
                )
            if test.input_commit != snapshot.output_commit:
                raise WorkflowEvidenceError("Tester reviewed the wrong commit")
            if review.input_commit != snapshot.output_commit:
                raise WorkflowEvidenceError("Reviewer reviewed the wrong commit")
            if test.manual_review_criteria != self.manual_review_criteria:
                raise WorkflowEvidenceError(
                    "Tester changed the controller manual-review scope"
                )
            if review.reviewed_criteria != self.manual_review_criteria:
                raise WorkflowEvidenceError(
                    "Reviewer changed the controller manual-review scope"
                )
            context.last_test = test
            context.last_review = review
            context.review_history.append(review)

            self._handoff(
                context,
                iteration=record.current_iteration,
                stage="verify",
                source=AgentRole.TESTER,
                target=None,
                input_commit=snapshot.output_commit,
                artifacts=(test_reference, test_execution),
                summary="Tester supplied evidence-grounded acceptance results.",
            )
            self._handoff(
                context,
                iteration=record.current_iteration,
                stage="verify",
                source=AgentRole.REVIEWER,
                target=None,
                input_commit=snapshot.output_commit,
                artifacts=(review_reference, review_execution),
                summary="Reviewer supplied an independent semantic verdict.",
            )
            record = context.controller.advance(
                record.run_id,
                expected_revision=record.revision,
                target=RunPhase.REVIEWING,
                reason="deterministic tests are recorded",
                artifacts=(test_reference,),
            )

            decision = self._decide(
                test,
                review,
                iteration=record.current_iteration,
                iteration_limit=record.iteration_limit,
            )
            blocking_ids = tuple(
                finding.id for finding in review.findings if finding.blocking
            )
            blocking_reasons = self._blocking_reasons(test)
            resolved_ids = tuple(
                identifier
                for identifier in previous_blocking_ids
                if identifier not in set(blocking_ids)
            )
            iteration_record = IterationRecord(
                run_id=record.run_id,
                team_id=record.team_id,
                created_at=_utc(self.clock),
                iteration=record.current_iteration,
                input_commit=snapshot.input_commit,
                output_commit=snapshot.output_commit,
                implementation_plan=plan_reference,
                work_results=(work_reference,),
                test_reports=(test_reference,),
                review_reports=(review_reference,),
                decision=decision,
                blocking_finding_ids=blocking_ids,
                blocking_reasons=blocking_reasons,
                resolved_finding_ids=resolved_ids,
                summary=self._decision_summary(decision, test, review),
            )
            iteration_reference = context.artifact_store.write(
                iteration_record,
                description="Controller decision for this immutable iteration.",
            )
            context.iteration_records.append(iteration_reference)
            context.last_iteration = iteration_record
            record = context.controller.advance(
                record.run_id,
                expected_revision=record.revision,
                target=RunPhase.DECIDING,
                reason="independent review and controller decision are recorded",
                artifacts=(review_reference, iteration_reference),
            )
            self._emit(
                context,
                ProgressEvent(
                    kind=ProgressEventKind.DECISION_RECORDED,
                    message=(
                        f"Iteration {record.current_iteration} decision: "
                        f"{decision.value}"
                    ),
                    phase=RunPhase.DECIDING,
                    iteration=record.current_iteration,
                    decision=decision,
                ),
            )

            if decision is IterationDecision.ACCEPT:
                record = context.controller.advance(
                    record.run_id,
                    expected_revision=record.revision,
                    target=RunPhase.DELIVERING,
                    reason="all acceptance evidence passed",
                    decision=IterationDecision.ACCEPT,
                )
                return self._complete(context, record, test, review)
            if decision is IterationDecision.FAIL:
                reason = self._decision_termination_reason(
                    test,
                    review,
                    iteration=record.current_iteration,
                    iteration_limit=record.iteration_limit,
                )
                return self._fail(
                    context,
                    record,
                    reason=reason,
                    detail=self._decision_summary(decision, test, review),
                )

            record = context.controller.advance(
                record.run_id,
                expected_revision=record.revision,
                target=RunPhase.IMPLEMENTING,
                reason="another bounded evidence-driven revision is required",
                decision=IterationDecision.REVISE,
            )
            feedback = (test, review, iteration_record)
            previous_blocking_ids = blocking_ids

    def _execute_single_agent_run(
        self,
        context: _WorkflowContext,
        record: RunRecord,
        workspace: GitWorkspace,
        workspace_manager: GitWorkspaceManager,
        quality_gate: QualityGate,
    ) -> WorkflowOutcome:
        """Run one writer once, then apply controller-owned deterministic checks."""
        record = context.controller.advance(
            record.run_id,
            expected_revision=record.revision,
            target=RunPhase.IMPLEMENTING,
            reason="single-agent baseline starts without a planning Agent",
        )
        input_commit = record.current_commit
        if input_commit is None:
            raise WorkflowEvidenceError("baseline input commit is missing")
        snapshots: list[GitSnapshot] = []

        def guard(is_correction: bool) -> None:
            if not snapshots:
                snapshots.append(
                    workspace_manager.verify_snapshot(
                        workspace,
                        iteration=1,
                        input_commit=input_commit,
                    )
                )
            elif is_correction:
                workspace_manager.verify_workspace(
                    workspace,
                    expected_commit=snapshots[0].output_commit,
                    require_clean=True,
                )
            else:
                raise WorkflowEvidenceError("baseline writer ran more than once")

        def assemble(body: AgentResponseBody) -> PhaseArtifact:
            if len(snapshots) != 1:
                raise WorkflowEvidenceError("baseline snapshot was not frozen")
            return self._assemble_work_result(
                context,
                body,
                snapshot=snapshots[0],
                role=AgentRole.SINGLE_AGENT,
            )

        work_artifact, work_reference, _ = self._invoke(
            context,
            AgentPromptInputs(
                task_brief=context.brief,
                team_id=context.team_plan.team_id,
                team_roles=frozenset(context.team_plan.legacy_roles),
                iteration=1,
                iteration_limit=1,
                role=AgentRole.SINGLE_AGENT,
                expected_kind=ArtifactKind.WORK_RESULT,
                input_commit=input_commit,
            ),
            stage="implement",
            assembler=assemble,
            invocation_guard=guard,
        )
        work = cast(WorkResult, work_artifact)
        snapshot = snapshots[0]
        record = context.controller.advance(
            record.run_id,
            expected_revision=record.revision,
            target=RunPhase.SNAPSHOTTING,
            reason="single Agent reported a committed implementation",
            artifacts=(work_reference,),
        )
        validate_work_result_snapshot(work, snapshot)
        record = context.controller.record_snapshot(
            record.run_id,
            expected_revision=record.revision,
            snapshot=snapshot,
        )
        self._emit(
            context,
            ProgressEvent(
                kind=ProgressEventKind.SNAPSHOT_VERIFIED,
                message=(
                    "Git snapshot verified: "
                    f"{len(snapshot.changed_files)} files changed"
                ),
                phase=RunPhase.VERIFYING,
                iteration=1,
                changed_files=snapshot.changed_files,
            ),
        )
        self._emit(
            context,
            ProgressEvent(
                kind=ProgressEventKind.QUALITY_GATES_STARTED,
                message="Running deterministic quality gates",
                phase=RunPhase.VERIFYING,
                iteration=1,
            ),
        )
        commands = quality_gate.run(iteration=1)
        if not commands:
            raise WorkflowEvidenceError("quality gate returned no command evidence")
        self._validate_verification_assignment(context.brief, commands)
        workspace_manager.verify_workspace(
            workspace,
            expected_commit=snapshot.output_commit,
            require_clean=True,
        )
        context.command_evidence.extend(commands)
        test = assemble_test_report(
            None,
            task_brief=context.brief,
            team_id=context.team_plan.team_id,
            agent=None,
            iteration=1,
            input_commit=snapshot.output_commit,
            commands=commands,
            manual_review_criteria=(),
            created_at=_utc(self.clock),
        )
        test_reference = context.artifact_store.write(
            test,
            description="Controller-owned deterministic baseline acceptance.",
        )
        context.last_test = test
        record = context.controller.advance(
            record.run_id,
            expected_revision=record.revision,
            target=RunPhase.REVIEWING,
            reason="deterministic baseline acceptance is recorded",
            artifacts=(test_reference,),
        )
        decision = (
            IterationDecision.ACCEPT
            if test.status is CheckStatus.PASSED
            else IterationDecision.FAIL
        )
        summary = (
            f"Controller decision: {decision.value}; deterministic "
            f"test={test.status.value}; independent Agent review=none."
        )
        iteration = IterationRecord(
            run_id=record.run_id,
            team_id=record.team_id,
            created_at=_utc(self.clock),
            iteration=1,
            input_commit=snapshot.input_commit,
            output_commit=snapshot.output_commit,
            work_results=(work_reference,),
            test_reports=(test_reference,),
            decision=decision,
            blocking_reasons=self._blocking_reasons(test),
            summary=summary,
        )
        iteration_reference = context.artifact_store.write(
            iteration,
            description="Controller decision for the one-pass baseline.",
        )
        context.iteration_records.append(iteration_reference)
        context.last_iteration = iteration
        record = context.controller.advance(
            record.run_id,
            expected_revision=record.revision,
            target=RunPhase.DECIDING,
            reason="one-pass deterministic baseline decision is recorded",
            artifacts=(iteration_reference,),
        )
        self._emit(
            context,
            ProgressEvent(
                kind=ProgressEventKind.DECISION_RECORDED,
                message=f"Iteration 1 decision: {decision.value}",
                phase=RunPhase.DECIDING,
                iteration=1,
                decision=decision,
            ),
        )
        if decision is IterationDecision.FAIL:
            return self._fail(
                context,
                record,
                reason=TerminationReason.ITERATION_LIMIT_REACHED,
                detail=summary,
            )
        record = context.controller.advance(
            record.run_id,
            expected_revision=record.revision,
            target=RunPhase.DELIVERING,
            reason="deterministic acceptance passed",
            decision=IterationDecision.ACCEPT,
        )
        return self._complete(context, record, test, None)

    def _verify(
        self,
        context: _WorkflowContext,
        *,
        record: RunRecord,
        work: WorkResult,
        commands: tuple[CommandEvidence, ...],
    ) -> tuple[
        tuple[TestReport, ArtifactReference, ArtifactReference],
        tuple[ReviewReport, ArtifactReference, ArtifactReference],
    ]:
        input_commit = record.current_commit
        if input_commit is None:
            raise WorkflowEvidenceError("verification input commit is missing")

        def invoke(
            role: AgentRole,
            kind: ArtifactKind,
        ) -> tuple[PhaseArtifact, ArtifactReference, ArtifactReference]:
            return self._invoke(
                context,
                AgentPromptInputs(
                    task_brief=context.brief,
                    team_id=context.team_plan.team_id,
                    team_roles=frozenset(context.team_plan.legacy_roles),
                    iteration=record.current_iteration,
                    iteration_limit=record.iteration_limit,
                    role=role,
                    expected_kind=kind,
                    input_commit=input_commit,
                    upstream_artifacts=(work,),
                    command_evidence=commands,
                    manual_review_criteria=self.manual_review_criteria,
                ),
                stage="verify",
                assembler=(
                    lambda body: (
                        self._assemble_test_report(
                            context,
                            body,
                            iteration=record.current_iteration,
                            input_commit=input_commit,
                            commands=commands,
                        )
                        if role is AgentRole.TESTER
                        else self._assemble_review_report(
                            context,
                            body,
                            iteration=record.current_iteration,
                            input_commit=input_commit,
                        )
                    )
                ),
            )

        if context.team_plan.max_concurrency == 1:
            tester = invoke(AgentRole.TESTER, ArtifactKind.TEST_REPORT)
            reviewer = invoke(AgentRole.REVIEWER, ArtifactKind.REVIEW_REPORT)
        else:
            with ThreadPoolExecutor(
                max_workers=2,
                thread_name_prefix="sat-verify",
            ) as pool:
                tester_future = pool.submit(
                    invoke,
                    AgentRole.TESTER,
                    ArtifactKind.TEST_REPORT,
                )
                reviewer_future = pool.submit(
                    invoke,
                    AgentRole.REVIEWER,
                    ArtifactKind.REVIEW_REPORT,
                )
                tester = tester_future.result()
                reviewer = reviewer_future.result()
        return (
            (cast(TestReport, tester[0]), tester[1], tester[2]),
            (cast(ReviewReport, reviewer[0]), reviewer[1], reviewer[2]),
        )
