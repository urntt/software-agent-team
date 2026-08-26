"""Integration tests for controller-owned dynamic Agent invocation."""

from __future__ import annotations

import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from software_agent_team.artifact_store import ArtifactStore
from software_agent_team.artifacts import (
    AcceptanceCriterion,
    AgentExecutionRecord,
    ArtifactKind,
    CommandEvidence,
    HandoffEnvelope,
    HandoffStatus,
    ReviewReport,
    TaskBrief,
    WorkResult,
)
from software_agent_team.artifacts import (
    TestReport as PhaseTestReport,
)
from software_agent_team.budgets import (
    AgentBudget,
    AgentBudgetLedger,
    ModelPricing,
)
from software_agent_team.dynamic_runner import DynamicAgentRunner
from software_agent_team.execution import (
    AgentExecutionRequest,
    AgentExecutionResult,
    AgentExecutionStatus,
    AgentExecutionTelemetry,
    AgentTokenUsage,
)
from software_agent_team.git_workspace import GitWorkspace, GitWorkspaceManager
from software_agent_team.integrity import canonical_model_sha256
from software_agent_team.planning import AdaptiveImplementationPlan, ProposedTask
from software_agent_team.responses import (
    ReviewReportResponse,
    WorkResultResponse,
)
from software_agent_team.responses import (
    TestReportResponse as SemanticTestReportResponse,
)
from software_agent_team.run_control import TerminationReason
from software_agent_team.scheduling import (
    DagScheduler,
    ScheduledAgentState,
    ScheduleStatus,
)
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
    """Create a clean source repository with an explicit public identity."""

    source = root / "source"
    source.mkdir()
    git(source, "init", "-b", "main")
    git(source, "config", "user.name", "urntt")
    git(source, "config", "user.email", "urntts@gmail.com")
    (source / "README.md").write_text("# Seed\n", encoding="utf-8")
    git(source, "add", "README.md")
    git(source, "commit", "-m", "chore: seed repository")
    return source


def brief() -> TaskBrief:
    """Return a small confirmed brief with deterministic and manual criteria."""

    return TaskBrief(
        run_id="dynamic-run-001",
        title="Greeting utility",
        source_request="Build a documented greeting utility.",
        requirements=["Provide a greeting function and focused test."],
        acceptance_criteria=[
            AcceptanceCriterion(
                id="AC_CODE",
                description="The greeting behavior passes its test.",
                verification="Run the deterministic test suite.",
            ),
            AcceptanceCriterion(
                id="AC_REVIEW",
                description="The result is clearly documented.",
                verification="Review the public usage documentation.",
            ),
        ],
        constraints=["Keep the implementation small."],
        confirmed=True,
    )


def budget(**updates: object) -> AgentBudget:
    """Return a bounded dynamic-run budget."""

    values: dict[str, object] = {
        "max_calls": 8,
        "max_input_tokens": 10_000,
        "max_output_tokens": 5_000,
        "max_agent_duration_seconds": 120,
        "max_estimated_cost_usd": "5",
    }
    values.update(updates)
    return AgentBudget.model_validate(values)


def dynamic_inputs(
    *,
    include_tester: bool = True,
    run_budget: AgentBudget | None = None,
    writer_scope: str = "repository",
) -> tuple[TaskBrief, AdaptiveImplementationPlan, TeamPlan]:
    """Build one internally coherent approved plan for runner tests."""

    task_brief = brief()
    implementation_plan = AdaptiveImplementationPlan(
        run_id=task_brief.run_id,
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
                acceptance_criteria=("AC_CODE", "AC_REVIEW"),
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
            dependencies=(),
            expected_output=ArtifactKind.WORK_RESULT,
            model_route_id="default",
            timeout_seconds=71,
            workspace_scope=writer_scope,
        )
    ]
    if include_tester:
        agents.append(
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
            )
        )
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
    team_plan = TeamPlan(
        plan_id="dynamic-run-001-team-r1",
        revision=1,
        run_id=task_brief.run_id,
        task_brief_sha256=canonical_model_sha256(task_brief),
        implementation_plan_sha256=canonical_model_sha256(implementation_plan),
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
        budget=run_budget or budget(),
        iteration_limit=1,
        max_concurrency=2 if include_tester else 1,
        independent_review=True,
        revision_enabled=False,
    )
    return task_brief, implementation_plan, team_plan


class FakeQualityGate:
    """Return one deterministic command while counting shared execution."""

    def __init__(self) -> None:
        self.calls = 0
        self._lock = threading.Lock()

    def run(self, *, iteration: int) -> tuple[CommandEvidence, ...]:
        with self._lock:
            self.calls += 1
        return (
            CommandEvidence(
                id="CHECK_TESTS",
                argv=("pytest", "-q"),
                criterion_ids=("AC_CODE",),
                exit_code=0,
                duration_ms=25,
                stdout_path=f"iterations/{iteration:02d}/commands/tests.stdout.txt",
                stderr_path=f"iterations/{iteration:02d}/commands/tests.stderr.txt",
                summary="Deterministic quality gate passed.",
            ),
        )


class DynamicExecutor:
    """Script semantic responses and perform real writer Git commits."""

    def __init__(
        self,
        workspace: Path,
        *,
        invalid_writer_once: bool = False,
        omit_model_for: str | None = None,
        mutate_reader: str | None = None,
        synchronize_quality: bool = False,
    ) -> None:
        self.workspace = workspace
        self.invalid_writer_once = invalid_writer_once
        self.omit_model_for = omit_model_for
        self.mutate_reader = mutate_reader
        self.requests: list[AgentExecutionRequest] = []
        self._counts: dict[str, int] = {}
        self._lock = threading.Lock()
        self._quality_barrier = threading.Barrier(2) if synchronize_quality else None

    def execute(self, request: AgentExecutionRequest) -> AgentExecutionResult:
        with self._lock:
            self.requests.append(request)
            count = self._counts.get(request.agent_id, 0) + 1
            self._counts[request.agent_id] = count
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
                git(self.workspace, "add", "greeting.py", "README.md")
                git(self.workspace, "commit", "-m", "feat: add greeting utility")
            response_text = WorkResultResponse(
                summary="Implemented and documented the greeting utility.",
                completed_tasks=("TASK_BUILD",),
                unresolved_issues=(),
            ).model_dump_json()
            if self.invalid_writer_once and count == 1:
                response_text = "not valid JSON"
        elif request.agent_id == "tester":
            self._wait_for_quality_peer()
            response_text = SemanticTestReportResponse(
                findings=(),
                summary="Deterministic evidence covers the implemented behavior.",
            ).model_dump_json()
        elif request.agent_id == "reviewer":
            if self.mutate_reader == request.agent_id:
                (self.workspace / "MUTATION.txt").write_text(
                    "read-only Agent mutation\n",
                    encoding="utf-8",
                )
            self._wait_for_quality_peer()
            response_text = ReviewReportResponse(
                verdict="accept",
                findings=(),
                summary="The final commit satisfies the assigned review scope.",
            ).model_dump_json()
        else:  # pragma: no cover - the fixture owns the complete team
            raise AssertionError(f"unexpected Agent: {request.agent_id}")
        return self._result(request, response_text)

    def _wait_for_quality_peer(self) -> None:
        if self._quality_barrier is None:
            return
        self._quality_barrier.wait(timeout=2)

    def _result(
        self,
        request: AgentExecutionRequest,
        response_text: str,
    ) -> AgentExecutionResult:
        return AgentExecutionResult(
            status=AgentExecutionStatus.COMPLETED,
            response_text=response_text,
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
                stdout=response_text,
                stderr="",
                session_id=f"session-{request.agent_id}",
                provider="test",
                model=(
                    None if self.omit_model_for == request.agent_id else request.model
                ),
                usage=AgentTokenUsage(
                    input_tokens=10,
                    output_tokens=5,
                    total_tokens=15,
                ),
            ),
        )


def runtime(
    tmp_path: Path,
    *,
    include_tester: bool = True,
    run_budget: AgentBudget | None = None,
    writer_scope: str = "repository",
    executor_options: dict[str, object] | None = None,
) -> tuple[DynamicAgentRunner, TeamPlan, DynamicExecutor, FakeQualityGate, Path]:
    """Prepare a real isolated workspace and dynamic runner."""

    task_brief, implementation_plan, team_plan = dynamic_inputs(
        include_tester=include_tester,
        run_budget=run_budget,
        writer_scope=writer_scope,
    )
    source = initialize_source(tmp_path)
    manager = GitWorkspaceManager(
        tmp_path / "workspaces",
        clock=lambda: FIXED_TIME,
    )
    workspace: GitWorkspace = manager.prepare(
        task_brief.run_id,
        source_repository=source,
    )
    run_directory = tmp_path / "runs" / task_brief.run_id
    run_directory.mkdir(parents=True)
    store = ArtifactStore(
        run_directory,
        task_brief=task_brief,
        team_plan=team_plan,
    )
    executor = DynamicExecutor(
        Path(workspace.workspace_path),
        **(executor_options or {}),
    )
    quality_gate = FakeQualityGate()
    runner = DynamicAgentRunner(
        task_brief=task_brief,
        implementation_plan=implementation_plan,
        team_plan=team_plan,
        workspace=workspace,
        workspace_manager=manager,
        artifact_store=store,
        executor=executor,
        quality_gate=quality_gate,
        budget_ledger=AgentBudgetLedger(team_plan.budget),
        pricing_by_model={MODEL: ModelPricing(model=MODEL)},
        manual_review_criteria=("AC_REVIEW",),
        clock=lambda: FIXED_TIME,
    )
    return runner, team_plan, executor, quality_gate, Path(workspace.workspace_path)


def test_dynamic_runner_executes_writer_then_parallel_quality_on_one_commit(
    tmp_path: Path,
) -> None:
    runner, team_plan, executor, quality_gate, _ = runtime(
        tmp_path,
        executor_options={"synchronize_quality": True},
    )

    result = DagScheduler().execute(team_plan, runner)

    assert result.status is ScheduleStatus.COMPLETED
    assert result.max_observed_concurrency == 2
    assert quality_gate.calls == runner.quality_gate_calls == 1
    assert {request.agent_id for request in executor.requests} == {
        "builder",
        "tester",
        "reviewer",
    }
    assert {
        request.agent_id: request.timeout_seconds for request in executor.requests
    } == {"builder": 71, "tester": 43, "reviewer": 47}
    assert all(request.model == MODEL for request in executor.requests)
    assert len(runner.execution_records) == 3
    execution_records = [
        runner.artifact_store.load(reference) for reference in runner.execution_records
    ]
    assert all(isinstance(item, AgentExecutionRecord) for item in execution_records)
    assert {item.agent_id for item in execution_records} == {
        "builder",
        "tester",
        "reviewer",
    }

    work = runner.artifact_store.load(runner.outputs["builder"])
    test = runner.artifact_store.load(runner.outputs["tester"])
    review = runner.artifact_store.load(runner.outputs["reviewer"])
    assert isinstance(work, WorkResult)
    assert isinstance(test, PhaseTestReport)
    assert isinstance(review, ReviewReport)
    assert test.input_commit == review.input_commit == work.output_commit
    assert test.manual_review_criteria == ("AC_REVIEW",)
    assert review.reviewed_criteria == ("AC_REVIEW",)

    assert len(runner.handoffs) == 4
    handoffs = [runner.artifact_store.load(reference) for reference in runner.handoffs]
    assert all(isinstance(item, HandoffEnvelope) for item in handoffs)
    assert all(item.status is HandoffStatus.COMPLETED for item in handoffs)
    assert {(item.source_agent_id, item.target_agent_id) for item in handoffs} == {
        ("builder", "tester"),
        ("builder", "reviewer"),
        ("tester", None),
        ("reviewer", None),
    }
    usage = runner.budget_ledger.snapshot()
    assert usage.calls_started == usage.calls_completed == 3
    assert usage.active_calls == 0


def test_dynamic_writer_semantic_repair_keeps_full_timeout_and_git_evidence(
    tmp_path: Path,
) -> None:
    runner, team_plan, executor, _, _ = runtime(
        tmp_path,
        executor_options={"invalid_writer_once": True},
    )

    result = DagScheduler().execute(team_plan, runner)

    assert result.status is ScheduleStatus.COMPLETED
    writer_requests = [
        request for request in executor.requests if request.agent_id == "builder"
    ]
    assert len(writer_requests) == 2
    assert [request.timeout_seconds for request in writer_requests] == [71, 71]
    assert "CONTROLLED_RESPONSE_REPAIR" in writer_requests[1].prompt
    assert len(runner.execution_records) == 4
    writer_records = [
        runner.artifact_store.load(reference)
        for reference in runner.execution_records
        if "/implement/builder-" in reference.path
    ]
    assert len(writer_records) == 2
    assert isinstance(writer_records[0], AgentExecutionRecord)
    assert writer_records[0].error is not None
    assert isinstance(writer_records[1], AgentExecutionRecord)
    assert writer_records[1].response_artifact == runner.outputs["builder"]


def test_dynamic_runner_creates_controller_test_evidence_without_tester(
    tmp_path: Path,
) -> None:
    runner, team_plan, _, quality_gate, _ = runtime(
        tmp_path,
        include_tester=False,
    )

    result = DagScheduler().execute(team_plan, runner)

    assert result.status is ScheduleStatus.COMPLETED
    assert quality_gate.calls == 1
    assert runner.controller_test_reference is not None
    report = runner.artifact_store.load(runner.controller_test_reference)
    assert isinstance(report, PhaseTestReport)
    assert report.producer == "controller"
    assert set(runner.outputs) == {"builder", "reviewer"}
    reviewer_record = next(
        record for record in result.records if record.agent_id == "reviewer"
    )
    assert runner.controller_test_reference in reviewer_record.evidence


def test_post_call_budget_rejection_is_persisted_before_schedule_stops(
    tmp_path: Path,
) -> None:
    run_budget = budget(max_input_tokens=5)
    runner, team_plan, _, quality_gate, _ = runtime(
        tmp_path,
        run_budget=run_budget,
    )

    result = DagScheduler().execute(team_plan, runner)

    assert result.status is ScheduleStatus.FAILED
    assert result.failed_agent_id == "builder"
    assert result.records[0].state is ScheduledAgentState.FAILED
    assert all(
        record.state is ScheduledAgentState.SKIPPED for record in result.records[1:]
    )
    assert runner.termination_reasons["builder"] is (
        TerminationReason.RESOURCE_LIMIT_REACHED
    )
    assert quality_gate.calls == 0
    assert len(runner.execution_records) == 1
    record = runner.artifact_store.load(runner.execution_records[0])
    assert isinstance(record, AgentExecutionRecord)
    assert "input-token budget" in (record.error or "")
    usage = runner.budget_ledger.snapshot()
    assert usage.input_tokens == 10
    assert usage.calls_completed == 1
    assert usage.active_calls == 0


def test_missing_success_model_is_dependency_failure_without_semantic_repair(
    tmp_path: Path,
) -> None:
    runner, team_plan, executor, _, _ = runtime(
        tmp_path,
        executor_options={"omit_model_for": "builder"},
    )

    result = DagScheduler().execute(team_plan, runner)

    assert result.status is ScheduleStatus.FAILED
    assert runner.termination_reasons["builder"] is (
        TerminationReason.DEPENDENCY_UNAVAILABLE
    )
    assert [request.agent_id for request in executor.requests] == ["builder"]
    record = runner.artifact_store.load(runner.execution_records[0])
    assert isinstance(record, AgentExecutionRecord)
    assert record.model is None
    assert "omitted model metadata" in (record.error or "")


def test_read_only_agent_workspace_mutation_crosses_safety_boundary(
    tmp_path: Path,
) -> None:
    runner, team_plan, _, _, workspace = runtime(
        tmp_path,
        include_tester=False,
        executor_options={"mutate_reader": "reviewer"},
    )

    result = DagScheduler().execute(team_plan, runner)

    assert result.status is ScheduleStatus.FAILED
    assert result.failed_agent_id == "reviewer"
    assert runner.termination_reasons["reviewer"] is (
        TerminationReason.SAFETY_BOUNDARY_CROSSED
    )
    assert (workspace / "MUTATION.txt").exists()
    record = runner.artifact_store.load(runner.execution_records[-1])
    assert isinstance(record, AgentExecutionRecord)
    assert "uncommitted changes" in (record.error or "")


def test_writer_cannot_commit_outside_approved_workspace_scope(
    tmp_path: Path,
) -> None:
    runner, team_plan, _, _, _ = runtime(
        tmp_path,
        writer_scope="repository/src",
    )

    result = DagScheduler().execute(team_plan, runner)

    assert result.status is ScheduleStatus.FAILED
    assert runner.termination_reasons["builder"] is (
        TerminationReason.SAFETY_BOUNDARY_CROSSED
    )
    assert "outside repository/src" in (result.records[0].error or "")


def test_multiple_reviewer_scopes_must_be_explicit_disjoint_and_complete() -> None:
    with pytest.raises(ValueError, match="cannot overlap"):
        DynamicAgentRunner._resolve_review_scopes(
            {"reviewer_a", "reviewer_b"},
            ("AC_CODE", "AC_REVIEW"),
            {
                "reviewer_a": ("AC_CODE",),
                "reviewer_b": ("AC_CODE", "AC_REVIEW"),
            },
        )

    with pytest.raises(ValueError, match="explicit non-overlapping scope"):
        DynamicAgentRunner._resolve_review_scopes(
            {"reviewer_a", "reviewer_b"},
            ("AC_CODE", "AC_REVIEW"),
            None,
        )
