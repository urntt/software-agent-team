"""Controller lifecycle integration for one approved adaptive Agent team."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from software_agent_team.artifact_store import ArtifactStore, ArtifactStoreError
from software_agent_team.artifacts import (
    ArtifactReference,
    CheckStatus,
    CommandEvidence,
    FinalReport,
    FinalStatus,
    IterationDecision,
    IterationRecord,
    ReviewReport,
    ReviewTerminationReason,
    ReviewVerdict,
    TestReport,
    WorkResult,
    resolve_acceptance_results,
)
from software_agent_team.budgets import AgentBudgetLedger, ModelPricing
from software_agent_team.dynamic_runner import (
    DynamicAgentRunner,
    DynamicAgentRunnerError,
    DynamicQualityGate,
)
from software_agent_team.execution import AgentExecutor
from software_agent_team.git_workspace import (
    GitSnapshot,
    GitWorkspace,
    GitWorkspaceError,
    GitWorkspaceManager,
    WorkspaceIntegrityError,
)
from software_agent_team.integrity import canonical_model_sha256
from software_agent_team.planning import ApprovedPlanningResult
from software_agent_team.progress import (
    ProgressDraftHandler,
    ProgressEvent,
    ProgressEventKind,
    ProgressHandler,
    RunEvent,
    RunEventJournal,
)
from software_agent_team.prompting import DynamicRevisionFeedback
from software_agent_team.quality_gates import (
    QualityGateBudgetExceeded,
    QualityGateError,
    SandboxUnavailableError,
)
from software_agent_team.reporting import render_run_report
from software_agent_team.run_control import (
    RunControlError,
    RunController,
    RunPhase,
    RunRecord,
    RunStore,
    TerminationReason,
)
from software_agent_team.runtime_configuration import RuntimeConfigurationError
from software_agent_team.scheduling import (
    DagScheduler,
    DagScheduleResult,
    ScheduleEvent,
    ScheduleEventKind,
    ScheduleStatus,
)
from software_agent_team.teams import AgentCapability, TeamPlan, TeamPlanOrigin


class DynamicWorkflowError(RuntimeError):
    """Raised when approved dynamic evidence cannot form a controlled run."""


class DynamicQualityGateFactory(Protocol):
    """Create one run-scoped deterministic gate for an isolated workspace."""

    def __call__(
        self,
        run_directory: Path,
        workspace: Path,
        event_handler: ProgressDraftHandler,
    ) -> DynamicQualityGate: ...


type RuntimeSetup = Callable[[GitWorkspace, Path], None]
type Clock = Callable[[], datetime]


def _system_clock() -> datetime:
    return datetime.now(UTC)


def _utc(clock: Clock) -> datetime:
    value = clock()
    if value.tzinfo is None or value.utcoffset() is None:
        raise DynamicWorkflowError("dynamic workflow clock must include a timezone")
    return value.astimezone(UTC)


def _unique(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value.strip() for value in values if value.strip()))


def _unique_references(
    values: tuple[ArtifactReference, ...] | list[ArtifactReference],
) -> tuple[ArtifactReference, ...]:
    return tuple({reference.path: reference for reference in values}.values())


@dataclass(frozen=True)
class DynamicWorkflowOutcome:
    """Terminal dynamic state and its complete controller evidence index."""

    record: RunRecord
    final_report: ArtifactReference
    human_report_path: str
    execution_records: tuple[ArtifactReference, ...]
    handoffs: tuple[ArtifactReference, ...]
    events: tuple[RunEvent, ...]
    schedules: tuple[DagScheduleResult, ...]


@dataclass
class _DynamicWorkflowContext:
    approved: ApprovedPlanningResult
    controller: RunController
    artifact_store: ArtifactStore
    event_journal: RunEventJournal
    run_directory: Path
    budget_ledger: AgentBudgetLedger
    execution_records: list[ArtifactReference] = field(default_factory=list)
    handoffs: list[ArtifactReference] = field(default_factory=list)
    iteration_records: list[ArtifactReference] = field(default_factory=list)
    schedules: list[DagScheduleResult] = field(default_factory=list)
    command_evidence: list[CommandEvidence] = field(default_factory=list)
    last_works: tuple[WorkResult, ...] = ()
    last_tests: tuple[TestReport, ...] = ()
    last_reviews: tuple[ReviewReport, ...] = ()


class DynamicWorkflowCoordinator:
    """Advance one approved adaptive plan through the authoritative lifecycle."""

    def __init__(
        self,
        *,
        runs_root: Path,
        workspaces_root: Path,
        executor: AgentExecutor,
        quality_gate_factory: DynamicQualityGateFactory,
        pricing_by_model: Mapping[str, ModelPricing],
        runtime_setup: RuntimeSetup | None = None,
        manual_review_criteria: tuple[str, ...] = (),
        review_scope_by_agent: Mapping[str, tuple[str, ...]] | None = None,
        artifact_repair_limit: int = 1,
        progress_handler: ProgressHandler | None = None,
        clock: Clock = _system_clock,
    ) -> None:
        if artifact_repair_limit not in {0, 1}:
            raise DynamicWorkflowError(
                "dynamic execution permits zero or one semantic repair"
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
        self.runtime_setup = runtime_setup
        self.manual_review_criteria = criteria
        self.review_scope_by_agent = review_scope_by_agent
        self.artifact_repair_limit = artifact_repair_limit
        self.progress_handler = progress_handler
        self.clock = clock

    @staticmethod
    def _emit(
        context: _DynamicWorkflowContext,
        event: ProgressEvent,
    ) -> RunEvent:
        current = context.controller.load(context.approved.task_brief.run_id)
        return context.event_journal.append(
            event,
            lifecycle_revision=current.revision,
            phase=current.phase,
        )

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
        route_models = {route.model for route in team_plan.model_routes.routes}
        if set(self.pricing_by_model) != route_models:
            raise DynamicWorkflowError(
                "pricing evidence must exactly cover approved model routes"
            )

        controller = RunController(RunStore(self.runs_root), None, clock=self.clock)
        record = controller.create(approved.task_brief, team_plan=team_plan)
        run_directory = self.runs_root / record.run_id

        def read_event_anchor() -> tuple[int, str | None]:
            current = controller.load(record.run_id)
            return current.event_count, current.event_head_sha256

        context = _DynamicWorkflowContext(
            approved=approved,
            controller=controller,
            artifact_store=ArtifactStore(
                run_directory,
                task_brief=approved.task_brief,
                team_plan=team_plan,
            ),
            event_journal=RunEventJournal(
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
            ),
            run_directory=run_directory,
            budget_ledger=AgentBudgetLedger(team_plan.budget),
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
            return self._execute_approved_plan(
                context,
                record,
                workspace,
                workspace_manager,
                quality_gate,
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

    @staticmethod
    def _event_digest(event: RunEvent) -> str:
        return canonical_model_sha256(event)

    def _execute_approved_plan(
        self,
        context: _DynamicWorkflowContext,
        record: RunRecord,
        workspace: GitWorkspace,
        workspace_manager: GitWorkspaceManager,
        quality_gate: DynamicQualityGate,
    ) -> DynamicWorkflowOutcome:
        team_plan = context.approved.team_plan
        plan_digest = team_plan.implementation_plan_sha256
        assert plan_digest is not None
        record = context.controller.advance(
            record.run_id,
            expected_revision=record.revision,
            target=RunPhase.IMPLEMENTING,
            reason="user-approved adaptive implementation plan is ready",
            implementation_plan_sha256=plan_digest,
        )

        revision_feedback: DynamicRevisionFeedback | None = None
        previous_signature: (
            tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]] | None
        ) = None
        previous_blocking_ids: tuple[str, ...] = ()
        while True:
            input_commit = record.current_commit
            if input_commit is None:
                raise DynamicWorkflowError("implementation input commit is missing")
            runner = DynamicAgentRunner(
                task_brief=context.approved.task_brief,
                implementation_plan=context.approved.implementation_plan,
                team_plan=team_plan,
                workspace=workspace,
                workspace_manager=workspace_manager,
                artifact_store=context.artifact_store,
                executor=self.executor,
                quality_gate=quality_gate,
                budget_ledger=context.budget_ledger,
                pricing_by_model=self.pricing_by_model,
                manual_review_criteria=self.manual_review_criteria,
                review_scope_by_agent=self.review_scope_by_agent,
                iteration=record.current_iteration,
                input_commit=input_commit,
                artifact_repair_limit=self.artifact_repair_limit,
                revision_feedback=revision_feedback,
                clock=self.clock,
            )
            snapshots: list[GitSnapshot] = []

            def observe(
                event: ScheduleEvent,
                *,
                current_runner: DynamicAgentRunner = runner,
                current_snapshots: list[GitSnapshot] = snapshots,
                current_input_commit: str = input_commit,
            ) -> None:
                nonlocal record
                agent = team_plan.get_agent(event.agent_id)
                is_quality = agent.capability in {
                    AgentCapability.TESTING,
                    AgentCapability.REVIEW,
                }
                if (
                    event.kind is ScheduleEventKind.AGENT_STARTED
                    and is_quality
                    and not current_snapshots
                ):
                    work_references = self._references_for_capabilities(
                        team_plan,
                        current_runner.outputs,
                        {AgentCapability.IMPLEMENTATION, AgentCapability.INTEGRATION},
                    )
                    snapshot = workspace_manager.verify_snapshot(
                        workspace,
                        iteration=record.current_iteration,
                        input_commit=current_input_commit,
                    )
                    self._validate_work_chain(
                        context.artifact_store,
                        work_references,
                        snapshot,
                    )
                    record = context.controller.advance(
                        record.run_id,
                        expected_revision=record.revision,
                        target=RunPhase.SNAPSHOTTING,
                        reason="all approved writers produced a verified commit chain",
                        artifacts=work_references,
                    )
                    record = context.controller.record_snapshot(
                        record.run_id,
                        expected_revision=record.revision,
                        snapshot=snapshot,
                    )
                    current_snapshots.append(snapshot)
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
                    self._emit(
                        context,
                        ProgressEvent(
                            kind=ProgressEventKind.QUALITY_GATES_STARTED,
                            message="Running deterministic quality gates",
                            phase=RunPhase.VERIFYING,
                            iteration=record.current_iteration,
                        ),
                    )
                if event.kind is ScheduleEventKind.AGENT_STARTED:
                    self._emit(
                        context,
                        ProgressEvent(
                            kind=ProgressEventKind.AGENT_STARTED,
                            message=f"{agent.label} is working",
                            agent_id=agent.id,
                            iteration=record.current_iteration,
                            attempt=1,
                        ),
                    )
                elif event.kind is ScheduleEventKind.AGENT_COMPLETED:
                    self._emit(
                        context,
                        ProgressEvent(
                            kind=ProgressEventKind.AGENT_COMPLETED,
                            message=f"{agent.label} completed its approved work",
                            agent_id=agent.id,
                            iteration=record.current_iteration,
                            attempt=1,
                        ),
                    )

            schedule: DagScheduleResult | None = None
            try:
                schedule = DagScheduler(
                    clock=self.clock,
                    observer=observe,
                ).execute(
                    team_plan,
                    runner,
                    iteration=record.current_iteration,
                )
            finally:
                context.execution_records.extend(runner.execution_records)
                context.handoffs.extend(runner.handoffs)
                context.execution_records[:] = list(
                    _unique_references(context.execution_records)
                )
                context.handoffs[:] = list(_unique_references(context.handoffs))
                self._capture_runner_outputs(context, runner)

            assert schedule is not None
            context.schedules.append(schedule)
            if schedule.status is ScheduleStatus.FAILED:
                if (
                    context.last_tests
                    and context.last_tests[0].iteration == record.current_iteration
                ):
                    context.command_evidence.extend(context.last_tests[0].commands)
                failed_id = schedule.failed_agent_id
                assert failed_id is not None
                failed_record = next(
                    item for item in schedule.records if item.agent_id == failed_id
                )
                return self._fail(
                    context,
                    record,
                    reason=runner.termination_reasons.get(
                        failed_id,
                        TerminationReason.EXECUTION_FAILED,
                    ),
                    detail=failed_record.error or failed_record.summary,
                )
            if len(snapshots) != 1 or record.phase is not RunPhase.VERIFYING:
                raise DynamicWorkflowError(
                    "completed dynamic schedule did not cross one snapshot boundary"
                )
            snapshot = snapshots[0]
            outputs = runner.outputs
            work_references = self._references_for_capabilities(
                team_plan,
                outputs,
                {AgentCapability.IMPLEMENTATION, AgentCapability.INTEGRATION},
            )
            test_references = self._references_for_capabilities(
                team_plan,
                outputs,
                {AgentCapability.TESTING},
            )
            if not test_references:
                controller_test = runner.controller_test_reference
                if controller_test is None:
                    raise DynamicWorkflowError(
                        "dynamic quality pass produced no deterministic TestReport"
                    )
                test_references = (controller_test,)
            review_references = self._references_for_capabilities(
                team_plan,
                outputs,
                {AgentCapability.REVIEW},
            )
            works = self._load_artifacts(
                context.artifact_store,
                work_references,
                WorkResult,
            )
            tests = self._load_artifacts(
                context.artifact_store,
                test_references,
                TestReport,
            )
            reviews = self._load_artifacts(
                context.artifact_store,
                review_references,
                ReviewReport,
            )
            context.last_works = works
            context.last_tests = tests
            context.last_reviews = reviews
            context.command_evidence.extend(tests[0].commands)

            record = context.controller.advance(
                record.run_id,
                expected_revision=record.revision,
                target=RunPhase.REVIEWING,
                reason="deterministic test evidence is recorded",
                artifacts=test_references,
            )
            decision = self._decide(
                tests,
                reviews,
                iteration=record.current_iteration,
                iteration_limit=record.iteration_limit,
            )
            blocking_findings = tuple(
                finding
                for review in reviews
                for finding in review.findings
                if finding.blocking
            )
            blocking_ids = tuple(finding.id for finding in blocking_findings)
            blocking_reasons = self._blocking_reasons(tests)
            signature = (
                tuple(sorted(blocking_ids)),
                tuple(sorted(finding.description for finding in blocking_findings)),
                tuple(sorted(blocking_reasons)),
            )
            correctable_nonacceptance = (
                not any(test.status is CheckStatus.BLOCKED for test in tests)
                and not any(review.verdict is ReviewVerdict.FAIL for review in reviews)
                and not (
                    all(test.status is CheckStatus.PASSED for test in tests)
                    and all(
                        review.verdict is ReviewVerdict.ACCEPT for review in reviews
                    )
                )
            )
            repeated = (
                correctable_nonacceptance
                and previous_signature is not None
                and signature == previous_signature
            )
            if repeated:
                decision = IterationDecision.FAIL
            resolved_ids = tuple(
                identifier
                for identifier in previous_blocking_ids
                if identifier not in set(blocking_ids)
            )
            summary = self._decision_summary(decision, tests, reviews)
            iteration_record = IterationRecord(
                run_id=record.run_id,
                team_id=record.team_id,
                created_at=_utc(self.clock),
                iteration=record.current_iteration,
                input_commit=snapshot.input_commit,
                output_commit=snapshot.output_commit,
                implementation_plan_sha256=plan_digest,
                work_results=work_references,
                test_reports=test_references,
                review_reports=review_references,
                decision=decision,
                blocking_finding_ids=blocking_ids,
                blocking_reasons=blocking_reasons,
                resolved_finding_ids=resolved_ids,
                summary=summary,
            )
            iteration_reference = context.artifact_store.write(
                iteration_record,
                description="Controller decision for this immutable iteration.",
            )
            context.iteration_records.append(iteration_reference)
            record = context.controller.advance(
                record.run_id,
                expected_revision=record.revision,
                target=RunPhase.DECIDING,
                reason="independent quality and controller decision are recorded",
                artifacts=(*review_references, iteration_reference),
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
                return self._complete(context, record, tests, reviews)
            if decision is IterationDecision.FAIL:
                return self._fail(
                    context,
                    record,
                    reason=self._decision_termination_reason(
                        tests,
                        reviews,
                        iteration=record.current_iteration,
                        iteration_limit=record.iteration_limit,
                        repeated=repeated,
                    ),
                    detail=summary,
                )

            record = context.controller.advance(
                record.run_id,
                expected_revision=record.revision,
                target=RunPhase.IMPLEMENTING,
                reason="another bounded evidence-driven revision is required",
                decision=IterationDecision.REVISE,
            )
            revision_feedback = DynamicRevisionFeedback(
                previous_iteration=iteration_record.iteration,
                output_commit=iteration_record.output_commit,
                blocking_findings=blocking_findings,
                blocking_reasons=blocking_reasons,
                summary=summary,
            )
            previous_signature = signature
            previous_blocking_ids = blocking_ids

    @staticmethod
    def _references_for_capabilities(
        team_plan: TeamPlan,
        outputs: Mapping[str, ArtifactReference],
        capabilities: set[AgentCapability],
    ) -> tuple[ArtifactReference, ...]:
        selected: list[ArtifactReference] = []
        for agent in team_plan.agents:
            if agent.capability not in capabilities:
                continue
            reference = outputs.get(agent.id)
            if reference is None:
                raise DynamicWorkflowError(
                    f"completed schedule omitted output from Agent {agent.id}"
                )
            selected.append(reference)
        return tuple(selected)

    @staticmethod
    def _validate_work_chain(
        store: ArtifactStore,
        references: tuple[ArtifactReference, ...],
        snapshot: GitSnapshot,
    ) -> None:
        works = DynamicWorkflowCoordinator._load_artifacts(
            store,
            references,
            WorkResult,
        )
        expected = snapshot.input_commit
        for work in works:
            if work.input_commit != expected:
                raise DynamicWorkflowError(
                    "dynamic WorkResults do not form the scheduler commit chain"
                )
            expected = work.output_commit
        if expected != snapshot.output_commit:
            raise DynamicWorkflowError(
                "dynamic writer chain differs from the aggregate Git snapshot"
            )

    @staticmethod
    def _load_artifacts[ArtifactT: WorkResult | TestReport | ReviewReport](
        store: ArtifactStore,
        references: tuple[ArtifactReference, ...],
        model: type[ArtifactT],
    ) -> tuple[ArtifactT, ...]:
        loaded: list[ArtifactT] = []
        for reference in references:
            artifact = store.load(reference)
            if not isinstance(artifact, model):
                raise DynamicWorkflowError(
                    f"artifact {reference.path} has the wrong runtime type"
                )
            loaded.append(artifact)
        return tuple(loaded)

    @staticmethod
    def _capture_runner_outputs(
        context: _DynamicWorkflowContext,
        runner: DynamicAgentRunner,
    ) -> None:
        works: list[WorkResult] = []
        tests: list[TestReport] = []
        reviews: list[ReviewReport] = []
        for reference in runner.outputs.values():
            artifact = context.artifact_store.load(reference)
            if isinstance(artifact, WorkResult):
                works.append(artifact)
            elif isinstance(artifact, TestReport):
                tests.append(artifact)
            elif isinstance(artifact, ReviewReport):
                reviews.append(artifact)
        if runner.controller_test_reference is not None:
            artifact = context.artifact_store.load(runner.controller_test_reference)
            if isinstance(artifact, TestReport):
                tests.append(artifact)
        context.last_works = tuple(works) or context.last_works
        context.last_tests = tuple(tests) or context.last_tests
        context.last_reviews = tuple(reviews) or context.last_reviews

    @staticmethod
    def _decide(
        tests: tuple[TestReport, ...],
        reviews: tuple[ReviewReport, ...],
        *,
        iteration: int,
        iteration_limit: int,
    ) -> IterationDecision:
        if any(test.status is CheckStatus.BLOCKED for test in tests) or any(
            review.verdict is ReviewVerdict.FAIL for review in reviews
        ):
            return IterationDecision.FAIL
        if all(test.status is CheckStatus.PASSED for test in tests) and all(
            review.verdict is ReviewVerdict.ACCEPT for review in reviews
        ):
            return IterationDecision.ACCEPT
        if iteration < iteration_limit:
            return IterationDecision.REVISE
        return IterationDecision.FAIL

    @staticmethod
    def _blocking_reasons(tests: tuple[TestReport, ...]) -> tuple[str, ...]:
        reasons: list[str] = []
        for test in tests:
            if test.status is CheckStatus.PASSED:
                continue
            reasons.extend(test.blockers)
            reasons.extend(test.findings)
            if not test.blockers and not test.findings:
                reasons.append(f"Tester {test.producer} status is {test.status.value}.")
        return _unique(reasons)

    @staticmethod
    def _decision_summary(
        decision: IterationDecision,
        tests: tuple[TestReport, ...],
        reviews: tuple[ReviewReport, ...],
    ) -> str:
        test_summary = ",".join(
            f"{test.producer}:{test.status.value}" for test in tests
        )
        review_summary = (
            ",".join(f"{review.producer}:{review.verdict.value}" for review in reviews)
            or "none"
        )
        return (
            f"Controller decision: {decision.value}; tests={test_summary}; "
            f"reviews={review_summary}."
        )

    @staticmethod
    def _decision_termination_reason(
        tests: tuple[TestReport, ...],
        reviews: tuple[ReviewReport, ...],
        *,
        iteration: int,
        iteration_limit: int,
        repeated: bool,
    ) -> TerminationReason:
        if repeated:
            return TerminationReason.REPEATED_BLOCKER
        if any(test.status is CheckStatus.BLOCKED for test in tests):
            return TerminationReason.DEPENDENCY_UNAVAILABLE
        failed_reviews = tuple(
            review for review in reviews if review.verdict is ReviewVerdict.FAIL
        )
        if failed_reviews:
            if any(
                review.termination_reason
                is ReviewTerminationReason.EVIDENCE_INTEGRITY_COMPROMISED
                for review in failed_reviews
            ):
                return TerminationReason.ARTIFACT_INVALID
            return TerminationReason.SAFETY_BOUNDARY_CROSSED
        if iteration >= iteration_limit:
            return TerminationReason.ITERATION_LIMIT_REACHED
        return TerminationReason.EXECUTION_FAILED

    def _complete(
        self,
        context: _DynamicWorkflowContext,
        record: RunRecord,
        tests: tuple[TestReport, ...],
        reviews: tuple[ReviewReport, ...],
    ) -> DynamicWorkflowOutcome:
        acceptance = resolve_acceptance_results(tests[0], reviews)
        limitations = _unique(
            [issue for work in context.last_works for issue in work.unresolved_issues]
        )
        nonblocking = _unique(
            [
                finding.description
                for review in reviews
                for finding in review.findings
                if not finding.blocking
            ]
        )
        report = FinalReport(
            run_id=record.run_id,
            team_id=record.team_id,
            created_at=_utc(self.clock),
            status=FinalStatus.COMPLETED,
            termination_reason=TerminationReason.SUCCEEDED.value,
            final_commit=record.current_commit,
            iterations=tuple(context.iteration_records),
            acceptance_results=acceptance,
            unresolved_findings=nonblocking,
            known_limitations=limitations,
            summary="The adaptive team passed deterministic gates and review.",
        )
        reference = context.artifact_store.write(
            report,
            description="Authoritative terminal report.",
        )
        markdown = context.artifact_store.write_final_report_markdown(
            reference,
            self._render_report(context, record, report),
        )
        record = context.controller.complete(
            record.run_id,
            expected_revision=record.revision,
            detail=report.summary,
            final_report=reference,
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
        return self._outcome(context, record, reference, markdown)

    def _fail(
        self,
        context: _DynamicWorkflowContext,
        record: RunRecord,
        *,
        reason: TerminationReason,
        detail: str,
    ) -> DynamicWorkflowOutcome:
        unresolved = [detail]
        for test in context.last_tests:
            unresolved.extend(test.blockers)
            unresolved.extend(test.findings)
        unresolved.extend(
            finding.description
            for review in context.last_reviews
            for finding in review.findings
            if finding.blocking
        )
        limitations = _unique(
            [issue for work in context.last_works for issue in work.unresolved_issues]
        )
        report = FinalReport(
            run_id=record.run_id,
            team_id=record.team_id,
            created_at=_utc(self.clock),
            status=FinalStatus.FAILED,
            termination_reason=reason.value,
            final_commit=record.current_commit,
            iterations=tuple(context.iteration_records),
            acceptance_results=(
                () if not context.last_tests else context.last_tests[0].criteria
            ),
            unresolved_findings=_unique(unresolved),
            known_limitations=limitations,
            summary=f"The adaptive run failed: {detail}",
        )
        reference = context.artifact_store.write(
            report,
            description="Authoritative terminal failure report.",
        )
        markdown = context.artifact_store.write_final_report_markdown(
            reference,
            self._render_report(context, record, report),
        )
        record = context.controller.fail(
            record.run_id,
            expected_revision=record.revision,
            reason=reason,
            detail=detail,
            final_report=reference,
        )
        self._emit(
            context,
            ProgressEvent(
                kind=ProgressEventKind.RUN_FAILED,
                message="Build stopped; see the final report",
                phase=RunPhase.FAILED,
                iteration=record.current_iteration,
            ),
        )
        return self._outcome(context, record, reference, markdown)

    @staticmethod
    def _outcome(
        context: _DynamicWorkflowContext,
        record: RunRecord,
        final_report: ArtifactReference,
        markdown: str,
    ) -> DynamicWorkflowOutcome:
        return DynamicWorkflowOutcome(
            record=context.controller.load(record.run_id),
            final_report=final_report,
            human_report_path=markdown,
            execution_records=tuple(
                sorted(context.execution_records, key=lambda item: item.path)
            ),
            handoffs=tuple(sorted(context.handoffs, key=lambda item: item.path)),
            events=context.event_journal.load(),
            schedules=tuple(context.schedules),
        )

    @staticmethod
    def _render_report(
        context: _DynamicWorkflowContext,
        record: RunRecord,
        report: FinalReport,
    ) -> str:
        return render_run_report(
            artifact_store=context.artifact_store,
            record=record,
            report=report,
            execution_records=tuple(context.execution_records),
            handoffs=tuple(context.handoffs),
            command_evidence=tuple(context.command_evidence),
        )

    @staticmethod
    def _termination_reason(error: Exception) -> TerminationReason:
        if isinstance(error, DynamicAgentRunnerError):
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
                DynamicWorkflowError,
                ArtifactStoreError,
                RunControlError,
                ValidationError,
            ),
        ):
            return TerminationReason.ARTIFACT_INVALID
        if isinstance(error, QualityGateError):
            return TerminationReason.EXECUTION_FAILED
        return TerminationReason.CONTROLLER_ERROR

    @staticmethod
    def _error_detail(error: Exception) -> str:
        return str(error).strip() or type(error).__name__
