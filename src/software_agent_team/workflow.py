"""Phase 1 function-specialized workflow orchestration and reporting."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from threading import Lock
from typing import Protocol, cast

from pydantic import ValidationError

from software_agent_team.artifact_store import ArtifactStore, ArtifactStoreError
from software_agent_team.artifacts import (
    IMPLEMENTATION_ROLES,
    AgentExecutionRecord,
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
from software_agent_team.budgets import AgentBudget, ModelPricing
from software_agent_team.execution import (
    AgentExecutionRequest,
    AgentExecutionResult,
    AgentExecutionStatus,
    AgentExecutor,
)
from software_agent_team.git_workspace import (
    GitWorkspace,
    GitWorkspaceError,
    GitWorkspaceManager,
    WorkspaceIntegrityError,
    validate_work_result_snapshot,
)
from software_agent_team.prompting import (
    AgentPromptInputs,
    build_agent_execution_request,
)
from software_agent_team.quality_gates import (
    QualityGateBudgetExceeded,
    QualityGateError,
    SandboxUnavailableError,
)
from software_agent_team.responses import (
    AgentArtifactResponseError,
    parse_agent_artifact,
)
from software_agent_team.run_control import (
    RunController,
    RunPhase,
    RunRecord,
    RunStore,
    TerminationReason,
)
from software_agent_team.runtime_configuration import RuntimeConfigurationError
from software_agent_team.teams import TeamDefinition, TeamManifest

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


type QualityGateFactory = Callable[[Path, Path], QualityGate]
type RuntimeSetup = Callable[[GitWorkspace, Path], None]
type Clock = Callable[[], datetime]


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


@dataclass
class _WorkflowContext:
    brief: TaskBrief
    team: TeamDefinition
    controller: RunController
    artifact_store: ArtifactStore
    run_directory: Path
    execution_records: list[ArtifactReference] = field(default_factory=list)
    handoffs: list[ArtifactReference] = field(default_factory=list)
    iteration_records: list[ArtifactReference] = field(default_factory=list)
    command_evidence: list[CommandEvidence] = field(default_factory=list)
    last_test: TestReport | None = None
    last_review: ReviewReport | None = None
    last_iteration: IterationRecord | None = None
    execution_lock: Lock = field(default_factory=Lock, repr=False)
    calls_started: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    agent_duration_ms: int = 0
    estimated_cost_usd: Decimal = Decimal(0)


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
        runtime_setup: RuntimeSetup | None = None,
        manual_review_criteria: tuple[str, ...] = (),
        agent_timeout_seconds: int = 600,
        artifact_repair_limit: int = 1,
        verification_concurrency: int = 2,
        clock: Clock = _system_clock,
    ) -> None:
        if agent_timeout_seconds < 1:
            raise WorkflowError("Agent timeout must be positive")
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
        self.manifest = manifest
        self.runs_root = runs_root
        self.workspaces_root = workspaces_root
        self.executor = executor
        self.quality_gate_factory = quality_gate_factory
        self.budget = budget
        self.pricing = pricing
        self.runtime_setup = runtime_setup
        self.manual_review_criteria = cleaned_manual_criteria
        self.agent_timeout_seconds = agent_timeout_seconds
        self.artifact_repair_limit = artifact_repair_limit
        self.verification_concurrency = verification_concurrency
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
        team = self.manifest.get_team(PHASE1_TEAM_ID)
        controller = RunController(
            RunStore(self.runs_root),
            self.manifest,
            clock=self.clock,
        )
        record = controller.create(
            task_brief,
            team_id=team.id,
            iteration_limit=PHASE1_ITERATION_LIMIT,
        )
        run_directory = self.runs_root / task_brief.run_id
        context = _WorkflowContext(
            brief=task_brief,
            team=team,
            controller=controller,
            artifact_store=ArtifactStore(
                run_directory,
                task_brief=task_brief,
                team=team,
                iteration_limit=PHASE1_ITERATION_LIMIT,
            ),
            run_directory=run_directory,
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
            quality_gate = self.quality_gate_factory(
                run_directory,
                Path(workspace.workspace_path),
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
                team_id=context.team.id,
                team_roles=frozenset(context.team.roles),
                iteration=1,
                iteration_limit=PHASE1_ITERATION_LIMIT,
                role=AgentRole.PLANNER,
                expected_kind=ArtifactKind.IMPLEMENTATION_PLAN,
            ),
            stage="plan",
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
            work_artifact, work_reference, work_execution = self._invoke(
                context,
                AgentPromptInputs(
                    task_brief=context.brief,
                    team_id=context.team.id,
                    team_roles=frozenset(context.team.roles),
                    iteration=record.current_iteration,
                    iteration_limit=PHASE1_ITERATION_LIMIT,
                    role=AgentRole.GENERALIST_DEVELOPER,
                    expected_kind=ArtifactKind.WORK_RESULT,
                    input_commit=input_commit,
                    upstream_artifacts=upstream,
                ),
                stage="implement",
            )
            work = cast(WorkResult, work_artifact)
            if work.input_commit != input_commit:
                raise WorkflowEvidenceError(
                    "Developer work result uses the wrong input commit"
                )
            record = context.controller.advance(
                record.run_id,
                expected_revision=record.revision,
                target=RunPhase.SNAPSHOTTING,
                reason="Developer reported a committed implementation",
                artifacts=(work_reference,),
            )
            snapshot = workspace_manager.verify_snapshot(
                workspace,
                iteration=record.current_iteration,
                input_commit=input_commit,
            )
            validate_work_result_snapshot(work, snapshot)
            record = context.controller.record_snapshot(
                record.run_id,
                expected_revision=record.revision,
                snapshot=snapshot,
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
                work_result=work_reference,
                test_report=test_reference,
                review_report=review_reference,
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
                reason="one bounded evidence-driven revision is required",
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
                    team_id=context.team.id,
                    team_roles=frozenset(context.team.roles),
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
            )

        if self.verification_concurrency == 1:
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
        expected = {criterion.id for criterion in brief.acceptance_criteria}
        if any(not command.criterion_ids for command in commands):
            raise WorkflowEvidenceError(
                "quality-gate evidence is missing criterion coverage"
            )
        deterministic = {
            criterion_id
            for command in commands
            for criterion_id in command.criterion_ids
        }
        if not deterministic.issubset(expected):
            raise WorkflowEvidenceError(
                "quality-gate evidence references an unknown criterion"
            )
        if deterministic | set(self.manual_review_criteria) != expected:
            raise WorkflowEvidenceError(
                "verification assignment does not cover every criterion"
            )

    def _invoke(
        self,
        context: _WorkflowContext,
        inputs: AgentPromptInputs,
        *,
        stage: str,
    ) -> tuple[PhaseArtifact, ArtifactReference, ArtifactReference]:
        base_request = build_agent_execution_request(
            inputs,
            timeout_seconds=self.agent_timeout_seconds,
            model=self.pricing.model,
        )
        last_error = "Agent did not return an artifact"
        for attempt in range(1, self.artifact_repair_limit + 2):
            with context.execution_lock:
                if context.calls_started >= self.budget.max_calls:
                    raise AgentInvocationError(
                        "Agent call budget is exhausted",
                        TerminationReason.RESOURCE_LIMIT_REACHED,
                    )
                context.calls_started += 1
            request = self._repair_request(base_request, last_error, attempt)
            result = self.executor.execute(request)
            response: PhaseArtifact | None = None
            response_reference: ArtifactReference | None = None
            record_error: str | None = None
            repairable = False
            failure_reason: TerminationReason | None = None
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
                else:
                    try:
                        response = parse_agent_artifact(
                            result,
                            request,
                            task_brief=context.brief,
                            team_roles=context.team.roles,
                            iteration_limit=PHASE1_ITERATION_LIMIT,
                        )
                        response_reference = context.artifact_store.write(
                            response,
                            description=(
                                f"Validated response from {request.role.value}."
                            ),
                        )
                    except (AgentArtifactResponseError, ArtifactStoreError) as error:
                        record_error = self._error_detail(error)
                        repairable = True
            else:
                record_error = result.error or (
                    f"Agent execution ended as {result.status.value}"
                )
                repairable = result.status is AgentExecutionStatus.INVALID_RESPONSE
                if result.status is AgentExecutionStatus.PROVIDER_FAILED:
                    failure_reason = TerminationReason.DEPENDENCY_UNAVAILABLE

            execution_reference = self._record_execution(
                context,
                request=request,
                result=result,
                stage=stage,
                attempt=attempt,
                response_reference=response_reference,
                error=record_error,
            )
            if response is not None and response_reference is not None:
                return response, response_reference, execution_reference
            last_error = record_error or last_error
            if repairable and attempt <= self.artifact_repair_limit:
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
        if request.role is AgentRole.PLANNER:
            role_check = (
                "Recompute the union of tasks[].acceptance_criteria and make it "
                "equal every criterion ID in the TaskBrief; require every "
                "tasks[].id to begin with TASK_ and match "
                "^TASK_[A-Z0-9_]+$; verify every task dependency exactly names "
                "one of those task IDs in the same response."
            )
        elif request.role in IMPLEMENTATION_ROLES:
            role_check = (
                "Before responding, run git status --short, git rev-parse HEAD, "
                "and git diff --name-only <input_commit> HEAD --. Require a clean "
                "status and copy the exact full commit and changed paths from "
                "those commands, replacing <input_commit> with run.input_commit "
                "from RUN_CONTEXT_JSON; never reuse or invent a hash."
            )
        elif request.role is AgentRole.TESTER:
            role_check = (
                "Recheck that every TaskBrief criterion appears exactly once and "
                "that every command field exactly reproduces controller evidence. "
                "The top-level status accepts only passed, failed, or blocked, "
                "never pending_review. Set the top-level status to passed when all "
                "commands and deterministic-only criteria pass, manual-review "
                "criteria alone are pending_review, and blockers is empty; use "
                "pending_review only in criteria[].status for IDs copied into "
                "manual_review_criteria."
            )
        elif request.role is AgentRole.REVIEWER:
            role_check = (
                "Recheck that the verdict, findings, criterion IDs, and input "
                "commit agree with the supplied immutable evidence. Use revise, "
                "not fail, for a correctable implementation or acceptance-gate "
                "defect; fail requires a terminal safety or evidence-integrity "
                "reason that makes another revision unsafe."
            )
        else:  # pragma: no cover - executable roles are exhaustively mapped
            role_check = "Recheck every field against the supplied run evidence."
        repair = (
            "\n\nCONTROLLED_RESPONSE_REPAIR\n"
            "Your previous response was rejected for this reason: "
            f"{previous_error}\n"
            "Revalidate the entire response rather than only the reported error. "
            "Use each key exactly once in every JSON object. Include every "
            "required schema field, including schema_version, kind, run_id, "
            "team_id, producer, created_at, and iteration. "
            f"{role_check} "
            "Return one corrected JSON object only. Do not repeat the invalid form."
        )
        return request.model_copy(update={"prompt": f"{request.prompt}{repair}"})

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
    ) -> ArtifactReference:
        telemetry = result.telemetry
        usage = telemetry.usage
        estimated_cost = None
        budget_error = None
        if (
            usage is not None
            and usage.input_tokens is not None
            and usage.output_tokens is not None
        ):
            estimated_cost = self.pricing.estimate_cost(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
            )
        with context.execution_lock:
            prospective_input = context.input_tokens + (
                0 if usage is None or usage.input_tokens is None else usage.input_tokens
            )
            prospective_output = context.output_tokens + (
                0
                if usage is None or usage.output_tokens is None
                else usage.output_tokens
            )
            prospective_duration = context.agent_duration_ms + telemetry.duration_ms
            prospective_cost = context.estimated_cost_usd + (
                estimated_cost or Decimal(0)
            )
            if prospective_input > self.budget.max_input_tokens:
                budget_error = "Agent input-token budget was exceeded"
            elif prospective_output > self.budget.max_output_tokens:
                budget_error = "Agent output-token budget was exceeded"
            elif prospective_duration > self.budget.max_agent_duration_seconds * 1000:
                budget_error = "Agent duration budget was exceeded"
            elif prospective_cost > self.budget.max_estimated_cost_usd:
                budget_error = "Agent estimated-cost budget was exceeded"
            context.input_tokens = prospective_input
            context.output_tokens = prospective_output
            context.agent_duration_ms = prospective_duration
            context.estimated_cost_usd = prospective_cost
        effective_error = error or budget_error
        outputs = context.artifact_store.write_execution_outputs(
            iteration=request.iteration,
            stage=stage,
            role=request.role,
            attempt=attempt,
            stdout=telemetry.stdout,
            stderr=telemetry.stderr,
        )
        record = AgentExecutionRecord(
            run_id=request.run_id,
            team_id=request.team_id,
            iteration=request.iteration,
            stage=stage,
            attempt=attempt,
            role=request.role,
            session_key=request.session_key,
            session_id=telemetry.session_id,
            model=telemetry.model,
            provider=telemetry.provider,
            started_at=telemetry.started_at,
            finished_at=telemetry.finished_at,
            duration_ms=telemetry.duration_ms,
            exit_code=telemetry.exit_code,
            timed_out=telemetry.timed_out,
            input_tokens=None if usage is None else usage.input_tokens,
            output_tokens=None if usage is None else usage.output_tokens,
            estimated_cost_usd=estimated_cost,
            stdout_path=outputs.stdout_path,
            stderr_path=outputs.stderr_path,
            stdout_sha256=outputs.stdout_sha256,
            stderr_sha256=outputs.stderr_sha256,
            response_artifact=response_reference,
            error=effective_error,
        )
        reference = context.artifact_store.write(
            record,
            description=f"Agent execution telemetry for {request.role.value}.",
        )
        with context.execution_lock:
            context.execution_records.append(reference)
        if budget_error is not None:
            raise AgentInvocationError(
                budget_error,
                TerminationReason.RESOURCE_LIMIT_REACHED,
            )
        return reference

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
            team_id=context.team.id,
            iteration=iteration,
            stage=stage,
            sequence=1,
            source_role=source,
            target_role=target,
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
        final_reference = context.artifact_store.write(
            final_report,
            description="Authoritative terminal report.",
        )
        markdown_path = context.artifact_store.write_final_report_markdown(
            final_reference,
            self._render_report(context, record, final_report),
        )
        record = context.controller.complete(
            record.run_id,
            expected_revision=record.revision,
            detail=final_report.summary,
            final_report=final_reference,
        )
        return self._outcome(context, record, final_reference, markdown_path)

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
        final_reference = context.artifact_store.write(
            final_report,
            description="Authoritative terminal failure report.",
        )
        markdown_path = context.artifact_store.write_final_report_markdown(
            final_reference,
            self._render_report(context, record, final_report),
        )
        record = context.controller.fail(
            record.run_id,
            expected_revision=record.revision,
            reason=reason,
            detail=detail,
            final_report=final_reference,
        )
        return self._outcome(context, record, final_reference, markdown_path)

    @staticmethod
    def _outcome(
        context: _WorkflowContext,
        record: RunRecord,
        final_reference: ArtifactReference,
        markdown_path: str,
    ) -> WorkflowOutcome:
        return WorkflowOutcome(
            record=record,
            final_report=final_reference,
            human_report_path=markdown_path,
            execution_records=tuple(
                sorted(context.execution_records, key=lambda item: item.path)
            ),
            handoffs=tuple(sorted(context.handoffs, key=lambda item: item.path)),
        )

    def _render_report(
        self,
        context: _WorkflowContext,
        record: RunRecord,
        report: FinalReport,
    ) -> str:
        execution_records = [
            cast(AgentExecutionRecord, context.artifact_store.load(reference))
            for reference in sorted(
                context.execution_records,
                key=lambda item: item.path,
            )
        ]
        calls = len(execution_records)
        failures = sum(item.error is not None for item in execution_records)
        identities = Counter(
            (item.iteration, item.stage, item.role) for item in execution_records
        )
        retries = sum(max(0, count - 1) for count in identities.values())
        duration_ms = sum(item.duration_ms for item in execution_records)
        input_tokens = [
            item.input_tokens
            for item in execution_records
            if item.input_tokens is not None
        ]
        output_tokens = [
            item.output_tokens
            for item in execution_records
            if item.output_tokens is not None
        ]
        gate_duration_ms = sum(
            command.duration_ms for command in context.command_evidence
        )
        estimated_cost = sum(
            (
                item.estimated_cost_usd
                for item in execution_records
                if item.estimated_cost_usd is not None
            ),
            Decimal(0),
        )
        token_text = (
            f"{sum(input_tokens)} input / {sum(output_tokens)} output"
            if input_tokens or output_tokens
            else "not reported"
        )
        lines = [
            f"# Run report: {report.run_id}",
            "",
            f"- Status: `{report.status.value}`",
            f"- Team: `{report.team_id}`",
            f"- Termination reason: `{report.termination_reason}`",
            f"- Final commit: `{report.final_commit or 'not available'}`",
            f"- Iterations recorded: {len(report.iterations)}",
            "",
            "## Summary",
            "",
            report.summary,
            "",
            "## Acceptance results",
            "",
            "| Criterion | Status | Detail |",
            "| --- | --- | --- |",
        ]
        if report.acceptance_results:
            lines.extend(
                f"| {item.criterion_id} | {item.status.value} | "
                f"{item.detail.replace('|', '\\|')} |"
                for item in report.acceptance_results
            )
        else:
            lines.append(
                "| _none recorded_ | blocked | No test report was available. |"
            )
        lines.extend(
            [
                "",
                "## Execution metrics",
                "",
                f"- Agent calls: {calls}",
                f"- Controlled response repairs: {retries}",
                f"- Failed Agent attempts: {failures}",
                f"- Agent duration: {duration_ms} ms",
                f"- Deterministic-gate duration: {gate_duration_ms} ms",
                f"- Reported tokens: {token_text}",
                f"- Estimated model cost: ${estimated_cost:.6f}",
                "",
                "## Evidence index",
                "",
                "### Iteration decisions",
                "",
            ]
        )
        lines.extend(
            f"- `{reference.path}` (`{reference.sha256}`)"
            for reference in report.iterations
        )
        if not report.iterations:
            lines.append("- No complete iteration decision was recorded.")
        lines.extend(["", "### Agent executions", ""])
        lines.extend(
            f"- `{reference.path}` (`{reference.sha256}`)"
            for reference in sorted(
                context.execution_records,
                key=lambda item: item.path,
            )
        )
        if not context.execution_records:
            lines.append(
                "- No Agent execution completed far enough to record telemetry."
            )
        lines.extend(["", "### Handoffs", ""])
        lines.extend(
            f"- `{reference.path}` (`{reference.sha256}`)"
            for reference in sorted(context.handoffs, key=lambda item: item.path)
        )
        if not context.handoffs:
            lines.append("- No cross-role handoff was recorded.")
        if report.unresolved_findings:
            lines.extend(["", "## Unresolved findings", ""])
            lines.extend(f"- {item}" for item in report.unresolved_findings)
        lines.extend(
            [
                "",
                "## Workspace",
                "",
                (
                    "The isolated result remains at "
                    f"`{record.workspace.workspace_path}`."
                    if record.workspace is not None
                    else "No isolated workspace was attached."
                ),
                "",
                "This report is derived from the immutable JSON artifacts in this run.",
                "",
            ]
        )
        return "\n".join(lines)

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
        if result.status is AgentExecutionStatus.PROVIDER_FAILED:
            return TerminationReason.DEPENDENCY_UNAVAILABLE
        return TerminationReason.EXECUTION_FAILED

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
