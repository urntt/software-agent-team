"""Phase 1 function-specialized workflow orchestration and reporting."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Protocol, cast

from pydantic import ValidationError

from software_agent_team.artifact_store import (
    ArtifactStore,
    ArtifactStoreError,
    FinalReportBundle,
)
from software_agent_team.artifacts import (
    AgentRole,
    ArtifactKind,
    ArtifactReference,
    CheckStatus,
    CommandEvidence,
    FinalReport,
    FinalStatus,
    HandoffEnvelope,
    HandoffStatus,
    ImplementationPlan,
    IterationDecision,
    IterationRecord,
    PhaseArtifact,
    ReviewReport,
    ReviewTerminationReason,
    ReviewVerdict,
    TaskBrief,
    TestReport,
    WorkResult,
    resolve_acceptance_results,
)
from software_agent_team.assembly import (
    ArtifactAssemblyError,
    assemble_review_report,
    assemble_test_report,
    assemble_work_result,
    validate_verification_assignment,
)
from software_agent_team.budgets import (
    AgentBudget,
    AgentBudgetExceeded,
    AgentBudgetLedger,
    AgentCallReservation,
    BudgetLedgerRecord,
    ModelPricing,
    budget_ledger_sha256,
)
from software_agent_team.execution import (
    AgentExecutionRequest,
    AgentExecutionResult,
    AgentExecutionStatus,
    AgentExecutor,
)
from software_agent_team.git_workspace import (
    GitSnapshot,
    GitWorkspace,
    GitWorkspaceError,
    GitWorkspaceManager,
    WorkspaceIntegrityError,
    validate_work_result_snapshot,
)
from software_agent_team.integrity import canonical_model_sha256
from software_agent_team.invocation import persist_agent_invocation
from software_agent_team.progress import (
    ProgressDraftHandler,
    ProgressEvent,
    ProgressEventKind,
    ProgressHandler,
    RunEvent,
    RunEventJournal,
)
from software_agent_team.prompting import (
    AgentPromptInputs,
    build_agent_execution_request,
    build_semantic_repair_request,
)
from software_agent_team.quality_gates import (
    QualityGateBudgetExceeded,
    QualityGateError,
    SandboxUnavailableError,
)
from software_agent_team.reporting import (
    render_minimal_terminal_report,
    render_run_report,
)
from software_agent_team.responses import (
    AgentArtifactResponseError,
    AgentResponseBody,
    GroundedReviewReportResponse,
    ImplementationPlanResponse,
    TestReportResponse,
    WorkResultResponse,
    controller_fields_for,
    parse_agent_response,
)
from software_agent_team.run_control import (
    RunController,
    RunPhase,
    RunRecord,
    RunStore,
    TerminationReason,
)
from software_agent_team.runtime_configuration import RuntimeConfigurationError
from software_agent_team.teams import TeamManifest, TeamPlan, compile_fixed_team_plan
from software_agent_team.versioning import SoftwareVersionReport

PHASE1_TEAM_ID = "function_specialized"
PHASE1_ITERATION_LIMIT = 2


class WorkflowError(RuntimeError):
    """Base error for deterministic Phase 1 orchestration."""


class WorkflowEvidenceError(WorkflowError):
    """Raised when independent evidence disagrees with an Agent claim."""


class AgentInvocationError(WorkflowError):
    """Raised after one Agent invocation exhausts its allowed repair."""

    def __init__(self, detail: str, reason: TerminationReason) -> None:
        super().__init__(detail)
        self.reason = reason


class QualityGate(Protocol):
    """Deterministic gate boundary required by the workflow."""

    def run(self, *, iteration: int) -> tuple[CommandEvidence, ...]:
        """Execute the fixed checks for one immutable snapshot."""


type QualityGateFactory = Callable[[Path, Path, ProgressDraftHandler], QualityGate]
type RuntimeSetup = Callable[[GitWorkspace, Path], None]
type Clock = Callable[[], datetime]
type ArtifactAssembler = Callable[[AgentResponseBody], PhaseArtifact]


def _system_clock() -> datetime:
    return datetime.now(UTC)


def _utc(clock: Clock) -> datetime:
    value = clock()
    if value.tzinfo is None or value.utcoffset() is None:
        raise WorkflowError("workflow clock must include a timezone")
    return value.astimezone(UTC)


def _unique(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value.strip() for value in values if value.strip()))


@dataclass(frozen=True)
class WorkflowOutcome:
    """Terminal state and evidence index returned to the CLI."""

    record: RunRecord
    final_report: ArtifactReference
    human_report_path: str
    execution_records: tuple[ArtifactReference, ...]
    handoffs: tuple[ArtifactReference, ...]
    events: tuple[RunEvent, ...]


@dataclass
class _WorkflowContext:
    brief: TaskBrief
    team_plan: TeamPlan
    controller: RunController
    artifact_store: ArtifactStore
    event_journal: RunEventJournal
    run_directory: Path
    execution_records: list[ArtifactReference] = field(default_factory=list)
    handoffs: list[ArtifactReference] = field(default_factory=list)
    iteration_records: list[ArtifactReference] = field(default_factory=list)
    command_evidence: list[CommandEvidence] = field(default_factory=list)
    last_test: TestReport | None = None
    last_review: ReviewReport | None = None
    last_iteration: IterationRecord | None = None
    execution_lock: Lock = field(default_factory=Lock, repr=False)
    budget_ledger: AgentBudgetLedger | None = field(default=None, repr=False)


class WorkflowCoordinator:
    """Execute the complete function-specialized Phase 1 state machine."""

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
        team = manifest.get_team(PHASE1_TEAM_ID)
        if AgentRole.GENERALIST_DEVELOPER not in team.roles:
            raise WorkflowError("Phase 1 team is missing the generalist developer")
        if (
            isinstance(iteration_limit, bool)
            or not 1 <= iteration_limit <= team.max_iterations
        ):
            raise WorkflowError(
                "iteration limit must be between 1 and "
                f"{team.max_iterations} for {team.id}"
            )
        self.manifest = manifest
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

    @staticmethod
    def _emit(context: _WorkflowContext, event: ProgressEvent) -> RunEvent:
        """Persist one controller event before notifying presentation code."""

        record = context.controller.load(context.brief.run_id)
        return context.event_journal.append(
            event,
            lifecycle_revision=record.revision,
            phase=record.phase,
        )

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
            team_id=PHASE1_TEAM_ID,
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
            return self._execute_planned_run(
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
                current_record: RunRecord = record,
                current_input_commit: str = input_commit,
                snapshots: list[GitSnapshot] = verified_snapshot,
            ) -> PhaseArtifact:
                snapshot = workspace_manager.verify_snapshot(
                    workspace,
                    iteration=current_record.current_iteration,
                    input_commit=current_input_commit,
                )
                snapshots.append(snapshot)
                return self._assemble_work_result(context, body, snapshot=snapshot)

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
                    summary=("Developer supplied controller-verified commit evidence."),
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

    def _validate_verification_assignment(
        self,
        brief: TaskBrief,
        commands: tuple[CommandEvidence, ...],
    ) -> None:
        try:
            validate_verification_assignment(
                brief,
                commands,
                self.manual_review_criteria,
            )
        except ArtifactAssemblyError as error:
            raise WorkflowEvidenceError(str(error)) from error

    def _assemble_plan(
        self,
        context: _WorkflowContext,
        body: AgentResponseBody,
    ) -> ImplementationPlan:
        if not isinstance(body, ImplementationPlanResponse):
            raise WorkflowEvidenceError("Planner returned the wrong semantic body")
        return ImplementationPlan(
            run_id=context.brief.run_id,
            team_id=context.team_plan.team_id,
            created_at=_utc(self.clock),
            objective=body.objective,
            approach=body.approach,
            tasks=body.tasks,
            risks=body.risks,
            assumptions=body.assumptions,
        )

    def _assemble_work_result(
        self,
        context: _WorkflowContext,
        body: AgentResponseBody,
        *,
        snapshot: GitSnapshot,
    ) -> WorkResult:
        if not isinstance(body, WorkResultResponse):
            raise WorkflowEvidenceError("Developer returned the wrong semantic body")
        return assemble_work_result(
            body,
            task_brief=context.brief,
            team_id=context.team_plan.team_id,
            agent=context.team_plan.get_agent(AgentRole.GENERALIST_DEVELOPER.value),
            snapshot=snapshot,
            created_at=_utc(self.clock),
        )

    def _assemble_test_report(
        self,
        context: _WorkflowContext,
        body: AgentResponseBody,
        *,
        iteration: int,
        input_commit: str,
        commands: tuple[CommandEvidence, ...],
    ) -> TestReport:
        if not isinstance(body, TestReportResponse):
            raise WorkflowEvidenceError("Tester returned the wrong semantic body")
        return assemble_test_report(
            body,
            task_brief=context.brief,
            team_id=context.team_plan.team_id,
            agent=context.team_plan.get_agent(AgentRole.TESTER.value),
            iteration=iteration,
            input_commit=input_commit,
            commands=commands,
            manual_review_criteria=self.manual_review_criteria,
            created_at=_utc(self.clock),
        )

    def _assemble_review_report(
        self,
        context: _WorkflowContext,
        body: AgentResponseBody,
        *,
        iteration: int,
        input_commit: str,
    ) -> ReviewReport:
        if not isinstance(body, GroundedReviewReportResponse):
            raise WorkflowEvidenceError("Reviewer returned the wrong semantic body")
        return assemble_review_report(
            body,
            task_brief=context.brief,
            team_id=context.team_plan.team_id,
            agent=context.team_plan.get_agent(AgentRole.REVIEWER.value),
            iteration=iteration,
            input_commit=input_commit,
            reviewed_criteria=self.manual_review_criteria,
            created_at=_utc(self.clock),
        )

    def _invoke(
        self,
        context: _WorkflowContext,
        inputs: AgentPromptInputs,
        *,
        stage: str,
        assembler: ArtifactAssembler,
    ) -> tuple[PhaseArtifact, ArtifactReference, ArtifactReference]:
        stage_timeout = context.team_plan.timeout_for_role(inputs.role)
        base_request = build_agent_execution_request(
            inputs,
            timeout_seconds=stage_timeout,
            model=self.pricing.model,
        )
        last_error = "Agent did not return a semantic response"
        for attempt in range(1, self.artifact_repair_limit + 2):
            request = self._repair_request(
                base_request,
                last_error,
                attempt,
            )
            assert context.budget_ledger is not None
            try:
                reservation = context.budget_ledger.reserve_call(
                    request.agent_id,
                    run_id=context.brief.run_id,
                    stage=stage,
                    attempt=attempt,
                    route_id=context.team_plan.model_routes.default_route_id,
                    pricing=self.pricing,
                )
            except AgentBudgetExceeded as error:
                raise AgentInvocationError(
                    str(error),
                    TerminationReason.RESOURCE_LIMIT_REACHED,
                ) from error
            role_name = request.role.value.replace("_", " ").title()
            self._emit(
                context,
                ProgressEvent(
                    kind=ProgressEventKind.AGENT_STARTED,
                    message=f"{role_name} is working",
                    agent_id=request.role.value,
                    iteration=request.iteration,
                    attempt=attempt,
                ),
            )
            result = self.executor.execute(request)
            response: PhaseArtifact | None = None
            response_reference: ArtifactReference | None = None
            record_error: str | None = None
            repairable = False
            failure_reason: TerminationReason | None = None
            ignored_controller_fields: tuple[str, ...] = ()
            assembly_error: Exception | None = None
            if result.status is AgentExecutionStatus.COMPLETED:
                reported_model = result.telemetry.model
                if reported_model is None:
                    record_error = "successful execution omitted model metadata"
                    failure_reason = TerminationReason.DEPENDENCY_UNAVAILABLE
                elif reported_model != self.pricing.model:
                    record_error = (
                        "Agent model differs from the frozen run model: "
                        f"{reported_model}"
                    )
                    failure_reason = TerminationReason.SAFETY_BOUNDARY_CROSSED
                elif (
                    result.telemetry.usage is None
                    or result.telemetry.usage.input_tokens is None
                    or result.telemetry.usage.output_tokens is None
                ):
                    record_error = "successful execution omitted token usage"
                    failure_reason = TerminationReason.DEPENDENCY_UNAVAILABLE
                elif sandbox_error := self._sandbox_runtime_error(result):
                    record_error = sandbox_error
                    failure_reason = TerminationReason.DEPENDENCY_UNAVAILABLE
                else:
                    try:
                        parsed = parse_agent_response(
                            result,
                            request,
                            task_brief=context.brief,
                            team_roles=context.team_plan.legacy_roles,
                            iteration_limit=self.iteration_limit,
                        )
                    except AgentArtifactResponseError as error:
                        record_error = self._error_detail(error)
                        repairable = True
                    else:
                        ignored_controller_fields = parsed.ignored_controller_fields
                        try:
                            response = assembler(parsed.body)
                            response_reference = context.artifact_store.write(
                                response,
                                description=(
                                    "Controller-assembled response from "
                                    f"{request.role.value}."
                                ),
                            )
                        except Exception as error:
                            assembly_error = error
                            record_error = self._error_detail(error)
            else:
                record_error = result.error or (
                    f"Agent execution ended as {result.status.value}"
                )
                repairable = result.status is AgentExecutionStatus.INVALID_RESPONSE
                if result.status in {
                    AgentExecutionStatus.PROVIDER_FAILED,
                    AgentExecutionStatus.PROVIDER_STALLED,
                }:
                    failure_reason = TerminationReason.DEPENDENCY_UNAVAILABLE

            execution_reference = self._record_execution(
                context,
                request=request,
                result=result,
                stage=stage,
                attempt=attempt,
                response_reference=response_reference,
                error=record_error,
                controller_supplied_fields=controller_fields_for(request.expected_kind),
                ignored_controller_fields=ignored_controller_fields,
                stage_timeout_seconds=stage_timeout,
                remaining_timeout_seconds=request.timeout_seconds,
                reservation=reservation,
            )
            self._emit(
                context,
                ProgressEvent(
                    kind=ProgressEventKind.AGENT_COMPLETED,
                    message=(
                        f"{role_name} response recorded "
                        f"({result.telemetry.duration_ms / 1000:.1f}s)"
                    ),
                    agent_id=request.role.value,
                    iteration=request.iteration,
                    attempt=attempt,
                    duration_ms=result.telemetry.duration_ms,
                ),
            )
            if assembly_error is not None:
                raise assembly_error
            if response is not None and response_reference is not None:
                return response, response_reference, execution_reference
            last_error = record_error or last_error
            if repairable and attempt <= self.artifact_repair_limit:
                self._emit(
                    context,
                    ProgressEvent(
                        kind=ProgressEventKind.AGENT_RETRY,
                        message=f"{role_name} response needs one bounded repair",
                        agent_id=request.role.value,
                        iteration=request.iteration,
                        attempt=attempt,
                    ),
                )
                continue
            raise AgentInvocationError(
                f"{request.role.value} failed: {last_error}",
                failure_reason
                or self._agent_termination_reason(result, repairable=repairable),
            )
        raise AssertionError("unreachable Agent repair state")

    @staticmethod
    def _repair_request(
        request: AgentExecutionRequest,
        previous_error: str,
        attempt: int,
    ) -> AgentExecutionRequest:
        if attempt == 1:
            return request
        return build_semantic_repair_request(request, previous_error)

    def _record_execution(
        self,
        context: _WorkflowContext,
        *,
        request: AgentExecutionRequest,
        result: AgentExecutionResult,
        stage: str,
        attempt: int,
        response_reference: ArtifactReference | None,
        error: str | None,
        controller_supplied_fields: tuple[str, ...],
        ignored_controller_fields: tuple[str, ...],
        stage_timeout_seconds: int,
        remaining_timeout_seconds: int,
        reservation: AgentCallReservation,
    ) -> ArtifactReference:
        assert context.budget_ledger is not None
        persisted = persist_agent_invocation(
            artifact_store=context.artifact_store,
            budget_ledger=context.budget_ledger,
            reservation=reservation,
            request=request,
            result=result,
            stage=stage,
            attempt=attempt,
            response_reference=response_reference,
            error=error,
            controller_supplied_fields=controller_supplied_fields,
            ignored_controller_fields=ignored_controller_fields,
            pricing=self.pricing,
            stage_timeout_seconds=stage_timeout_seconds,
            remaining_timeout_seconds=remaining_timeout_seconds,
        )
        with context.execution_lock:
            context.execution_records.append(persisted.reference)
        if persisted.budget_error is not None:
            raise AgentInvocationError(
                persisted.budget_error,
                TerminationReason.RESOURCE_LIMIT_REACHED,
            )
        return persisted.reference

    def _handoff(
        self,
        context: _WorkflowContext,
        *,
        iteration: int,
        stage: str,
        source: AgentRole,
        target: AgentRole | None,
        input_commit: str,
        artifacts: tuple[ArtifactReference, ...],
        summary: str,
    ) -> ArtifactReference:
        handoff = HandoffEnvelope(
            run_id=context.brief.run_id,
            team_id=context.team_plan.team_id,
            iteration=iteration,
            stage=stage,
            sequence=1,
            source_agent_id=source.value,
            target_agent_id=None if target is None else target.value,
            status=HandoffStatus.COMPLETED,
            created_at=_utc(self.clock),
            summary=summary,
            input_commit=input_commit,
            artifacts=list(artifacts),
        )
        reference = context.artifact_store.write(
            handoff,
            description=f"Handoff from {source.value} to "
            f"{target.value if target is not None else 'controller'}.",
        )
        context.handoffs.append(reference)
        return reference

    @staticmethod
    def _decide(
        test: TestReport,
        review: ReviewReport,
        *,
        iteration: int,
        iteration_limit: int,
    ) -> IterationDecision:
        if test.status is CheckStatus.BLOCKED or review.verdict is ReviewVerdict.FAIL:
            return IterationDecision.FAIL
        if test.status is CheckStatus.PASSED and review.verdict is ReviewVerdict.ACCEPT:
            return IterationDecision.ACCEPT
        if iteration < iteration_limit:
            return IterationDecision.REVISE
        return IterationDecision.FAIL

    @staticmethod
    def _blocking_reasons(test: TestReport) -> tuple[str, ...]:
        if test.status is CheckStatus.PASSED:
            return ()
        reasons = [*test.blockers, *test.findings]
        if not reasons:
            reasons.append(f"Tester status is {test.status.value}.")
        return _unique(reasons)

    @staticmethod
    def _decision_summary(
        decision: IterationDecision,
        test: TestReport,
        review: ReviewReport,
    ) -> str:
        return (
            f"Controller decision: {decision.value}; "
            f"test={test.status.value}; review={review.verdict.value}."
        )

    @staticmethod
    def _decision_termination_reason(
        test: TestReport,
        review: ReviewReport,
        *,
        iteration: int,
        iteration_limit: int,
    ) -> TerminationReason:
        if test.status is CheckStatus.BLOCKED:
            return TerminationReason.DEPENDENCY_UNAVAILABLE
        if review.verdict is ReviewVerdict.FAIL:
            if (
                review.termination_reason
                is ReviewTerminationReason.EVIDENCE_INTEGRITY_COMPROMISED
            ):
                return TerminationReason.ARTIFACT_INVALID
            return TerminationReason.SAFETY_BOUNDARY_CROSSED
        if iteration >= iteration_limit:
            return TerminationReason.ITERATION_LIMIT_REACHED
        return TerminationReason.EXECUTION_FAILED

    def _complete(
        self,
        context: _WorkflowContext,
        record: RunRecord,
        test: TestReport,
        review: ReviewReport,
    ) -> WorkflowOutcome:
        final_report = FinalReport(
            run_id=record.run_id,
            team_id=record.team_id,
            created_at=_utc(self.clock),
            software_version=self.software_version,
            status=FinalStatus.COMPLETED,
            termination_reason=TerminationReason.SUCCEEDED.value,
            final_commit=record.current_commit,
            iterations=tuple(context.iteration_records),
            acceptance_results=resolve_acceptance_results(test, review),
            unresolved_findings=tuple(
                finding.description
                for finding in review.findings
                if not finding.blocking
            ),
            summary="The implementation passed deterministic gates and review.",
        )
        bundle = self._write_terminal_report(
            context,
            record,
            final_report,
            description="Authoritative terminal report.",
        )
        record = context.artifact_store.bind_final_report_bundle(
            bundle,
            lambda reference: context.controller.complete(
                record.run_id,
                expected_revision=record.revision,
                detail=final_report.summary,
                final_report=reference,
            ),
        )
        self._emit(
            context,
            ProgressEvent(
                kind=ProgressEventKind.RUN_COMPLETED,
                message="Build accepted; preparing the verified delivery",
                phase=RunPhase.COMPLETED,
                iteration=record.current_iteration,
            ),
        )
        return self._outcome(
            context,
            record,
            bundle.reference,
            bundle.markdown_path,
        )

    def _fail(
        self,
        context: _WorkflowContext,
        record: RunRecord,
        *,
        reason: TerminationReason,
        detail: str,
    ) -> WorkflowOutcome:
        unresolved = [detail]
        if context.last_test is not None:
            unresolved.extend(context.last_test.blockers)
            unresolved.extend(context.last_test.findings)
        if context.last_review is not None:
            unresolved.extend(
                finding.description
                for finding in context.last_review.findings
                if finding.blocking
            )
        final_report = FinalReport(
            run_id=record.run_id,
            team_id=record.team_id,
            created_at=_utc(self.clock),
            software_version=self.software_version,
            status=FinalStatus.FAILED,
            termination_reason=reason.value,
            final_commit=record.current_commit,
            iterations=tuple(context.iteration_records),
            acceptance_results=(
                () if context.last_test is None else context.last_test.criteria
            ),
            unresolved_findings=_unique(unresolved),
            summary=f"The run failed: {detail}",
        )
        bundle = self._write_terminal_report(
            context,
            record,
            final_report,
            description="Authoritative terminal failure report.",
            allow_minimal_fallback=True,
        )
        record = context.artifact_store.bind_final_report_bundle(
            bundle,
            lambda reference: context.controller.fail(
                record.run_id,
                expected_revision=record.revision,
                reason=reason,
                detail=detail,
                final_report=reference,
            ),
        )
        self._emit(
            context,
            ProgressEvent(
                kind=ProgressEventKind.RUN_FAILED,
                message=(
                    "Build stopped during "
                    f"{record.transitions[-1].source.value}; see the final report"
                ),
                phase=RunPhase.FAILED,
                iteration=record.current_iteration,
            ),
        )
        return self._outcome(
            context,
            record,
            bundle.reference,
            bundle.markdown_path,
        )

    @staticmethod
    def _outcome(
        context: _WorkflowContext,
        record: RunRecord,
        final_reference: ArtifactReference,
        markdown_path: str,
    ) -> WorkflowOutcome:
        record = context.controller.load(record.run_id)
        return WorkflowOutcome(
            record=record,
            final_report=final_reference,
            human_report_path=markdown_path,
            execution_records=tuple(
                sorted(context.execution_records, key=lambda item: item.path)
            ),
            handoffs=tuple(sorted(context.handoffs, key=lambda item: item.path)),
            events=context.event_journal.load(),
        )

    def _render_report(
        self,
        context: _WorkflowContext,
        record: RunRecord,
        report: FinalReport,
        budget_record: BudgetLedgerRecord,
        budget_digest: str,
    ) -> str:
        return render_run_report(
            artifact_store=context.artifact_store,
            record=record,
            report=report,
            execution_records=tuple(context.execution_records),
            handoffs=tuple(context.handoffs),
            command_evidence=tuple(context.command_evidence),
            budget_ledger=budget_record,
            budget_ledger_sha256=budget_digest,
        )

    def _write_terminal_report(
        self,
        context: _WorkflowContext,
        record: RunRecord,
        report: FinalReport,
        *,
        description: str,
        allow_minimal_fallback: bool = False,
    ) -> FinalReportBundle:
        """Prepare terminal evidence before committing its canonical bundle."""

        assert context.budget_ledger is not None
        budget_record = context.budget_ledger.terminal_record()
        budget_digest = budget_ledger_sha256(budget_record)
        try:
            markdown = self._render_report(
                context,
                record,
                report,
                budget_record,
                budget_digest,
            )
        except Exception as error:
            if not allow_minimal_fallback:
                raise
            markdown = render_minimal_terminal_report(
                report=report,
                budget_ledger=budget_record,
                budget_ledger_sha256=budget_digest,
                rendering_error=error,
            )
        bundle = context.artifact_store.write_final_report_bundle(
            report,
            markdown,
            budget_record,
            description=description,
        )
        return bundle

    @staticmethod
    def _agent_termination_reason(
        result: AgentExecutionResult,
        *,
        repairable: bool,
    ) -> TerminationReason:
        if repairable:
            return TerminationReason.ARTIFACT_INVALID
        if result.status is AgentExecutionStatus.TIMED_OUT:
            return TerminationReason.RESOURCE_LIMIT_REACHED
        if result.status is AgentExecutionStatus.LAUNCH_FAILED:
            return TerminationReason.DEPENDENCY_UNAVAILABLE
        if result.status in {
            AgentExecutionStatus.PROVIDER_FAILED,
            AgentExecutionStatus.PROVIDER_STALLED,
        }:
            return TerminationReason.DEPENDENCY_UNAVAILABLE
        return TerminationReason.EXECUTION_FAILED

    @staticmethod
    def _sandbox_runtime_error(result: AgentExecutionResult) -> str | None:
        """Recognize OpenClaw-owned Docker failures without trusting Agent text."""

        for line in result.telemetry.stderr.splitlines():
            if "[tools]" not in line or " failed:" not in line:
                continue
            if (
                "Error response from daemon: container " in line
                and " is not running" in line
            ) or "Cannot connect to the Docker daemon" in line:
                return "OpenClaw sandbox became unavailable during Agent tool use"
        return None

    @staticmethod
    def _termination_reason(error: Exception) -> TerminationReason:
        if isinstance(error, AgentInvocationError):
            return error.reason
        if isinstance(error, QualityGateBudgetExceeded):
            return TerminationReason.RESOURCE_LIMIT_REACHED
        if isinstance(error, (SandboxUnavailableError, RuntimeConfigurationError)):
            return TerminationReason.DEPENDENCY_UNAVAILABLE
        if isinstance(error, WorkspaceIntegrityError):
            if "no new commit" in str(error) or "no changed files" in str(error):
                return TerminationReason.NO_RELEVANT_CHANGE
            return TerminationReason.SAFETY_BOUNDARY_CROSSED
        if isinstance(error, GitWorkspaceError):
            return TerminationReason.SAFETY_BOUNDARY_CROSSED
        if isinstance(
            error,
            (
                WorkflowEvidenceError,
                AgentArtifactResponseError,
                ArtifactStoreError,
                ValidationError,
            ),
        ):
            return TerminationReason.ARTIFACT_INVALID
        if isinstance(error, QualityGateError):
            return TerminationReason.EXECUTION_FAILED
        return TerminationReason.CONTROLLER_ERROR

    @staticmethod
    def _error_detail(error: Exception) -> str:
        message = str(error).strip()
        return message or error.__class__.__name__
