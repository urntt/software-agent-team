"""End-to-end tests for approved adaptive lifecycle convergence."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

from software_agent_team.artifact_store import ArtifactStore
from software_agent_team.artifacts import (
    AcceptanceCriterion,
    ArtifactKind,
    CheckStatus,
    CommandEvidence,
    FinalReport,
    FinalStatus,
    IterationDecision,
    IterationRecord,
    ReviewFinding,
    ReviewSeverity,
    TaskBrief,
)
from software_agent_team.budgets import AgentBudget, ModelPricing
from software_agent_team.dynamic_workflow import (
    DynamicWorkflowCoordinator,
    DynamicWorkflowOutcome,
)
from software_agent_team.execution import (
    AgentExecutionRequest,
    AgentExecutionResult,
    AgentExecutionStatus,
    AgentExecutionTelemetry,
    AgentTokenUsage,
)
from software_agent_team.integrity import canonical_model_sha256
from software_agent_team.planning import (
    AdaptiveImplementationPlan,
    AgentTimeoutResolution,
    AgentWorkload,
    ApprovedPlanningResult,
    PlanningApproval,
    ProposedTask,
)
from software_agent_team.progress import (
    ProgressDraftHandler,
    ProgressEvent,
    ProgressEventKind,
)
from software_agent_team.responses import (
    ReviewReportResponse,
    WorkResultResponse,
)
from software_agent_team.responses import (
    TestReportResponse as SemanticTestReportResponse,
)
from software_agent_team.run_control import RunPhase, TerminationReason
from software_agent_team.scheduling import ScheduleStatus
from software_agent_team.teams import (
    AgentCapability,
    AgentSpec,
    ModelRoute,
    ModelRoutePlan,
    ModelRoutingMode,
    PermissionProfile,
    PlanApprovalSource,
    TeamPlan,
    TeamPlanOrigin,
)

FIXED_TIME = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
MODEL = "test/provider-model"


def git(repository: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a bounded test-owned Git command without a shell."""

    return subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )


def initialize_source(root: Path) -> Path:
    """Create one clean seed repository."""

    source = root / "source"
    source.mkdir()
    git(source, "init", "-b", "main")
    git(source, "config", "user.name", "urntt")
    git(source, "config", "user.email", "urntts@gmail.com")
    (source / "README.md").write_text("# Seed\n", encoding="utf-8")
    git(source, "add", "README.md")
    git(source, "commit", "-m", "chore: seed repository")
    return source


def approved_inputs(
    *,
    run_id: str,
    iteration_limit: int = 1,
    include_reviewer: bool = True,
) -> ApprovedPlanningResult:
    """Build one coherent user-approved adaptive input bundle."""

    criteria = [
        AcceptanceCriterion(
            id="AC_CODE",
            description="The greeting behavior passes its test.",
            verification="Run the deterministic test suite.",
        )
    ]
    if include_reviewer:
        criteria.append(
            AcceptanceCriterion(
                id="AC_REVIEW",
                description="The result is clearly documented.",
                verification="Review the public usage documentation.",
            )
        )
    brief = TaskBrief(
        run_id=run_id,
        title="Greeting utility",
        source_request="Build a documented greeting utility.",
        requirements=["Provide a greeting function and focused test."],
        acceptance_criteria=criteria,
        constraints=["Keep the implementation small."],
        confirmed=True,
    )
    implementation = AdaptiveImplementationPlan(
        run_id=run_id,
        team_id="adaptive_team",
        revision=1,
        created_at=FIXED_TIME,
        objective="Implement and independently verify the greeting utility.",
        approach=("Implement one cohesive change.", "Verify the final commit."),
        tasks=(
            ProposedTask(
                id="TASK_BUILD",
                owner_agent_id="builder",
                description="Implement and document the greeting utility.",
                acceptance_criteria=tuple(item.id for item in criteria),
                expected_paths=("greeting.py", "README.md"),
            ),
        ),
        risks=("Documentation could diverge from behavior.",),
        assumptions=("Python is available in the target profile.",),
    )
    agents = [
        AgentSpec(
            id="builder",
            label="Builder",
            responsibility="Implement the approved greeting task.",
            rationale="One cohesive writer is sufficient for this small task.",
            capability=AgentCapability.IMPLEMENTATION,
            permission_profile=PermissionProfile.WORKSPACE_WRITE,
            stage_id="implement",
            expected_output=ArtifactKind.WORK_RESULT,
            model_route_id="default",
            timeout_seconds=71,
            workspace_scope="repository",
        ),
        AgentSpec(
            id="tester",
            label="Tester",
            responsibility="Analyze deterministic acceptance evidence.",
            rationale="Testing remains independent from implementation.",
            capability=AgentCapability.TESTING,
            permission_profile=PermissionProfile.READ_ONLY,
            stage_id="verify",
            dependencies=("builder",),
            expected_output=ArtifactKind.TEST_REPORT,
            model_route_id="default",
            timeout_seconds=43,
            workspace_scope="repository",
        ),
    ]
    if include_reviewer:
        agents.append(
            AgentSpec(
                id="reviewer",
                label="Reviewer",
                responsibility="Review the final commit and manual criterion.",
                rationale="The writer cannot approve its own result.",
                capability=AgentCapability.REVIEW,
                permission_profile=PermissionProfile.READ_ONLY,
                stage_id="verify",
                dependencies=("builder",),
                expected_output=ArtifactKind.REVIEW_REPORT,
                model_route_id="default",
                timeout_seconds=47,
                workspace_scope="repository",
            )
        )
    team = TeamPlan(
        plan_id=f"{run_id}-team-r1",
        revision=1,
        run_id=run_id,
        task_brief_sha256=canonical_model_sha256(brief),
        implementation_plan_sha256=canonical_model_sha256(implementation),
        team_id="adaptive_team",
        origin=TeamPlanOrigin.ADAPTIVE_PLANNING,
        approval_source=PlanApprovalSource.USER,
        created_at=FIXED_TIME,
        agents=tuple(agents),
        model_routes=ModelRoutePlan(
            mode=ModelRoutingMode.STRICT,
            default_route_id="default",
            routes=(ModelRoute(id="default", model=MODEL),),
        ),
        budget=AgentBudget(
            max_calls=8,
            max_input_tokens=10_000,
            max_output_tokens=5_000,
            max_agent_duration_seconds=120,
            max_estimated_cost_usd="5",
        ),
        iteration_limit=iteration_limit,
        max_concurrency=2,
        independent_review=True,
        revision_enabled=iteration_limit > 1,
    )
    approval = PlanningApproval(
        run_id=run_id,
        revision=1,
        approved_at=FIXED_TIME,
        confirmation="user_approved",
        proposal_sha256="a" * 64,
        task_brief_sha256=canonical_model_sha256(brief),
        implementation_plan_sha256=canonical_model_sha256(implementation),
        team_plan_sha256=canonical_model_sha256(team),
        timeout_resolutions=tuple(
            AgentTimeoutResolution(
                agent_id=agent.id,
                workload=AgentWorkload.ROUTINE,
                default_seconds=agent.timeout_seconds,
                ceiling_seconds=agent.timeout_seconds,
                resolved_seconds=agent.timeout_seconds,
                source="policy_workload",
            )
            for agent in team.agents
        ),
    )
    return ApprovedPlanningResult(
        task_brief=brief,
        implementation_plan=implementation,
        team_plan=team,
        approval=approval,
    )


class RecordingQualityGateFactory:
    """Create deterministic passing gates and retain their iteration calls."""

    def __init__(self) -> None:
        self.calls: list[int] = []

    def __call__(
        self,
        run_directory: Path,
        workspace: Path,
        event_handler: ProgressDraftHandler,
    ):
        del run_directory, workspace
        factory = self

        class Gate:
            def run(self, *, iteration: int) -> tuple[CommandEvidence, ...]:
                factory.calls.append(iteration)
                event_handler(
                    ProgressEvent(
                        kind=ProgressEventKind.QUALITY_GATE_COMPLETED,
                        message="Quality gate 1/1 CHECK_TESTS: passed",
                        phase=RunPhase.VERIFYING,
                        iteration=iteration,
                        completed=1,
                        total=1,
                    )
                )
                return (
                    CommandEvidence(
                        id="CHECK_TESTS",
                        argv=("pytest", "-q"),
                        criterion_ids=("AC_CODE",),
                        exit_code=0,
                        duration_ms=25,
                        stdout_path=(
                            f"iterations/{iteration:02d}/commands/tests.stdout.txt"
                        ),
                        stderr_path=(
                            f"iterations/{iteration:02d}/commands/tests.stderr.txt"
                        ),
                        summary="Deterministic quality gate passed.",
                    ),
                )

        return Gate()


class AdaptiveExecutor:
    """Commit real revisions and return bounded semantic Agent responses."""

    def __init__(
        self,
        workspace: Path,
        *,
        revise_first: bool = False,
        always_revise: bool = False,
        omit_builder_model: bool = False,
    ) -> None:
        self.workspace = workspace
        self.revise_first = revise_first
        self.always_revise = always_revise
        self.omit_builder_model = omit_builder_model
        self.requests: list[AgentExecutionRequest] = []
        self.counts: dict[str, int] = {}

    def execute(self, request: AgentExecutionRequest) -> AgentExecutionResult:
        self.requests.append(request)
        count = self.counts.get(request.agent_id, 0) + 1
        self.counts[request.agent_id] = count
        if request.agent_id == "builder":
            if count == 1:
                (self.workspace / "greeting.py").write_text(
                    "def greet(name: str) -> str:\n    return f'Hello, {name}!'\n",
                    encoding="utf-8",
                )
                with (self.workspace / "README.md").open(
                    "a", encoding="utf-8"
                ) as readme:
                    readme.write("\nUse `greet(name)` to create a greeting.\n")
            else:
                with (self.workspace / "README.md").open(
                    "a", encoding="utf-8"
                ) as readme:
                    readme.write("A greeting returns a normal Python string.\n")
            git(self.workspace, "add", "greeting.py", "README.md")
            git(
                self.workspace,
                "commit",
                "-m",
                f"feat: implement greeting iteration {count}",
            )
            body = WorkResultResponse(
                summary="Implemented the assigned greeting behavior.",
                completed_tasks=("TASK_BUILD",),
            ).model_dump_json()
        elif request.agent_id == "tester":
            body = SemanticTestReportResponse(
                summary="Deterministic evidence covers the greeting behavior."
            ).model_dump_json()
        elif request.agent_id == "reviewer":
            if self.always_revise or (self.revise_first and count == 1):
                body = ReviewReportResponse(
                    verdict="revise",
                    findings=(
                        ReviewFinding(
                            id="FINDING_DOCS",
                            severity=ReviewSeverity.HIGH,
                            blocking=True,
                            category="documentation",
                            description="The usage result type is not documented.",
                            recommendation="Document the returned string type.",
                            path="README.md",
                            criterion_ids=("AC_REVIEW",),
                        ),
                    ),
                    summary="One documentation blocker requires revision.",
                ).model_dump_json()
            else:
                body = ReviewReportResponse(
                    verdict="accept",
                    summary="The final commit satisfies the review scope.",
                ).model_dump_json()
        else:  # pragma: no cover - the fixture owns every Agent
            raise AssertionError(f"unexpected Agent: {request.agent_id}")
        return AgentExecutionResult(
            status=AgentExecutionStatus.COMPLETED,
            response_text=body,
            telemetry=AgentExecutionTelemetry(
                role=None,
                agent_id=request.agent_id,
                capability=request.capability,
                session_key=request.session_key,
                command=("fake-agent", request.agent_id),
                started_at=FIXED_TIME,
                finished_at=FIXED_TIME,
                duration_ms=10,
                exit_code=0,
                stdout=body,
                provider="test",
                model=(
                    None
                    if self.omit_builder_model and request.agent_id == "builder"
                    else request.model
                ),
                usage=AgentTokenUsage(
                    input_tokens=10,
                    output_tokens=5,
                    total_tokens=15,
                ),
            ),
        )


def coordinator(
    tmp_path: Path,
    approved: ApprovedPlanningResult,
    executor: AdaptiveExecutor,
    gates: RecordingQualityGateFactory,
) -> DynamicWorkflowCoordinator:
    """Build the dynamic coordinator from test-owned boundaries."""

    manual = (
        ("AC_REVIEW",)
        if any(
            criterion.id == "AC_REVIEW"
            for criterion in approved.task_brief.acceptance_criteria
        )
        else ()
    )
    return DynamicWorkflowCoordinator(
        runs_root=tmp_path / "runs",
        workspaces_root=tmp_path / "workspaces",
        executor=executor,
        quality_gate_factory=gates,
        pricing_by_model={MODEL: ModelPricing(model=MODEL)},
        manual_review_criteria=manual,
        clock=lambda: FIXED_TIME,
    )


def load_report(
    tmp_path: Path,
    outcome: DynamicWorkflowOutcome,
    approved: ApprovedPlanningResult,
):
    """Load one final report through its contextual artifact store."""

    run_directory = tmp_path / "runs" / approved.task_brief.run_id
    store = ArtifactStore(
        run_directory,
        task_brief=approved.task_brief,
        team_plan=approved.team_plan,
    )
    reference = outcome.final_report
    report = store.load(reference)
    assert isinstance(report, FinalReport)
    return store, report


def test_dynamic_workflow_accepts_one_iteration_with_live_lifecycle_order(
    tmp_path: Path,
) -> None:
    approved = approved_inputs(run_id="adaptive-accept")
    source = initialize_source(tmp_path)
    executor = AdaptiveExecutor(tmp_path / "workspaces" / approved.task_brief.run_id)
    gates = RecordingQualityGateFactory()

    outcome = coordinator(tmp_path, approved, executor, gates).execute(
        approved,
        source_repository=source,
    )
    store, report = load_report(tmp_path, outcome, approved)

    assert outcome.record.phase is RunPhase.COMPLETED
    assert report.status is FinalStatus.COMPLETED
    assert all(item.status is CheckStatus.PASSED for item in report.acceptance_results)
    assert gates.calls == [1]
    assert len(outcome.schedules) == 1
    assert outcome.schedules[0].status is ScheduleStatus.COMPLETED
    assert [transition.target for transition in outcome.record.transitions] == [
        RunPhase.PREPARING_WORKSPACE,
        RunPhase.PLANNING,
        RunPhase.IMPLEMENTING,
        RunPhase.SNAPSHOTTING,
        RunPhase.VERIFYING,
        RunPhase.REVIEWING,
        RunPhase.DECIDING,
        RunPhase.DELIVERING,
        RunPhase.COMPLETED,
    ]
    iteration = store.load(report.iterations[0])
    assert isinstance(iteration, IterationRecord)
    assert iteration.decision is IterationDecision.ACCEPT
    quality_event = next(
        event
        for event in outcome.events
        if event.kind is ProgressEventKind.QUALITY_GATE_COMPLETED
    )
    assert quality_event.phase is RunPhase.VERIFYING


def test_dynamic_workflow_revises_from_commit_bound_feedback_then_accepts(
    tmp_path: Path,
) -> None:
    approved = approved_inputs(run_id="adaptive-revise", iteration_limit=2)
    source = initialize_source(tmp_path)
    executor = AdaptiveExecutor(
        tmp_path / "workspaces" / approved.task_brief.run_id,
        revise_first=True,
    )
    gates = RecordingQualityGateFactory()

    outcome = coordinator(tmp_path, approved, executor, gates).execute(
        approved,
        source_repository=source,
    )
    store, report = load_report(tmp_path, outcome, approved)

    assert outcome.record.phase is RunPhase.COMPLETED
    assert gates.calls == [1, 2]
    assert len(report.iterations) == 2
    first = store.load(report.iterations[0])
    second = store.load(report.iterations[1])
    assert isinstance(first, IterationRecord)
    assert isinstance(second, IterationRecord)
    assert first.decision is IterationDecision.REVISE
    assert first.blocking_finding_ids == ("FINDING_DOCS",)
    assert second.decision is IterationDecision.ACCEPT
    assert second.resolved_finding_ids == ("FINDING_DOCS",)
    assert second.input_commit == first.output_commit
    second_builder = [
        request for request in executor.requests if request.agent_id == "builder"
    ][1]
    assert '"previous_iteration": 1' in second_builder.prompt
    assert '"id": "FINDING_DOCS"' in second_builder.prompt


def test_dynamic_workflow_persists_runner_failure_without_claiming_new_commit(
    tmp_path: Path,
) -> None:
    approved = approved_inputs(run_id="adaptive-failure")
    source = initialize_source(tmp_path)
    executor = AdaptiveExecutor(
        tmp_path / "workspaces" / approved.task_brief.run_id,
        omit_builder_model=True,
    )
    gates = RecordingQualityGateFactory()

    outcome = coordinator(tmp_path, approved, executor, gates).execute(
        approved,
        source_repository=source,
    )
    _, report = load_report(tmp_path, outcome, approved)

    assert outcome.record.phase is RunPhase.FAILED
    assert outcome.record.termination_reason is TerminationReason.DEPENDENCY_UNAVAILABLE
    assert report.status is FinalStatus.FAILED
    assert report.final_commit == outcome.record.workspace.base_commit
    assert report.iterations == ()
    assert gates.calls == []
    assert outcome.schedules[0].status is ScheduleStatus.FAILED
    assert len(outcome.execution_records) == 1


def test_dynamic_workflow_stops_an_unchanged_correctable_blocker(
    tmp_path: Path,
) -> None:
    approved = approved_inputs(run_id="adaptive-repeated", iteration_limit=2)
    source = initialize_source(tmp_path)
    executor = AdaptiveExecutor(
        tmp_path / "workspaces" / approved.task_brief.run_id,
        always_revise=True,
    )
    gates = RecordingQualityGateFactory()

    outcome = coordinator(tmp_path, approved, executor, gates).execute(
        approved,
        source_repository=source,
    )
    store, report = load_report(tmp_path, outcome, approved)
    last_iteration = store.load(report.iterations[-1])

    assert outcome.record.phase is RunPhase.FAILED
    assert outcome.record.termination_reason is TerminationReason.REPEATED_BLOCKER
    assert report.termination_reason == TerminationReason.REPEATED_BLOCKER.value
    assert isinstance(last_iteration, IterationRecord)
    assert last_iteration.decision is IterationDecision.FAIL
    assert last_iteration.blocking_finding_ids == ("FINDING_DOCS",)


def test_tester_only_dynamic_team_completes_without_review_artifact(
    tmp_path: Path,
) -> None:
    approved = approved_inputs(
        run_id="adaptive-tester-only",
        include_reviewer=False,
    )
    source = initialize_source(tmp_path)
    executor = AdaptiveExecutor(tmp_path / "workspaces" / approved.task_brief.run_id)
    gates = RecordingQualityGateFactory()

    outcome = coordinator(tmp_path, approved, executor, gates).execute(
        approved,
        source_repository=source,
    )
    store, report = load_report(tmp_path, outcome, approved)
    iteration = store.load(report.iterations[0])

    assert outcome.record.phase is RunPhase.COMPLETED
    assert isinstance(iteration, IterationRecord)
    assert iteration.review_reports == ()
    assert iteration.decision is IterationDecision.ACCEPT
