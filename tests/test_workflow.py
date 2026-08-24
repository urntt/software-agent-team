"""Offline end-to-end tests for the Phase 1 workflow coordinator."""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from software_agent_team.artifacts import (
    AgentRole,
    PlanTask,
    ReviewFinding,
    ReviewSeverity,
    ReviewTerminationReason,
    ReviewVerdict,
    TaskBrief,
)
from software_agent_team.budgets import AgentBudget, ModelPricing
from software_agent_team.execution import (
    AgentExecutionRequest,
    AgentExecutionResult,
    AgentExecutionStatus,
    AgentExecutionTelemetry,
    AgentExecutor,
    AgentTokenUsage,
)
from software_agent_team.progress import ProgressEvent, ProgressEventKind
from software_agent_team.quality_gates import (
    FakeSandboxBackend,
    QualityGateRunner,
    SandboxExecution,
    load_quality_gate_configuration,
)
from software_agent_team.responses import (
    ImplementationPlanResponse,
    ReviewReportResponse,
    WorkResultResponse,
)
from software_agent_team.responses import (
    TestReportResponse as SemanticTestReport,
)
from software_agent_team.run_control import RunPhase, TerminationReason
from software_agent_team.teams import load_team_manifest
from software_agent_team.workflow import WorkflowCoordinator

REPOSITORY_ROOT = Path(__file__).parents[1]
TEAM_CONFIG = REPOSITORY_ROOT / "configs" / "teams.json"
POLICY = REPOSITORY_ROOT / "configs" / "run-policy.json"
BENCHMARK = REPOSITORY_ROOT / "benchmarks" / "task_manager" / "benchmark.json"
SEED = REPOSITORY_ROOT / "benchmarks" / "task_manager" / "seed"
FIXED_TIME = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


def fixed_monotonic() -> float:
    return 0.0


def git(repository: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run Git without a shell in a test-owned repository."""

    return subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def initialize_source(tmp_path: Path) -> Path:
    """Create the frozen benchmark seed as a clean Git repository."""

    source = tmp_path / "source"
    shutil.copytree(SEED, source)
    git(source, "init", "-b", "main")
    git(source, "config", "user.name", "urntt")
    git(source, "config", "user.email", "urntts@gmail.com")
    git(source, "add", ".")
    git(source, "commit", "-m", "chore: initialize benchmark seed")
    return source


def task_brief() -> TaskBrief:
    """Load the frozen confirmed Phase 1 task."""

    return TaskBrief.model_validate_json(
        (BENCHMARK.parent / "task-brief.json").read_text(encoding="utf-8")
    )


def prompt_context(request: AgentExecutionRequest) -> dict[str, object]:
    """Extract controller-authored JSON context from a rendered test prompt."""

    body = request.prompt.split("RUN_CONTEXT_JSON\n", 1)[1]
    payload = body.split("\n\nRESPONSE_SCHEMA_JSON\n", 1)[0]
    parsed = json.loads(payload)
    assert isinstance(parsed, dict)
    return parsed


class DynamicWorkflowExecutor:
    """Generate attributable artifacts and real Developer commits offline."""

    def __init__(
        self,
        workspace: Path,
        *,
        review_verdicts: dict[int, ReviewVerdict] | None = None,
        invalid_plan_once: bool = False,
        invalid_tester_status_once: bool = False,
        tamper_test_commands: bool = False,
        inject_fake_git_fields: bool = False,
        commit_changes: bool = True,
        verify_barrier: bool = False,
        allow_single_verifier: bool = False,
        reported_model: str | None = "offline/test-model",
        report_usage: bool = True,
    ) -> None:
        self.workspace = workspace
        self.review_verdicts = review_verdicts or {}
        self.invalid_plan_once = invalid_plan_once
        self.invalid_tester_status_once = invalid_tester_status_once
        self.tamper_test_commands = tamper_test_commands
        self.inject_fake_git_fields = inject_fake_git_fields
        self.commit_changes = commit_changes
        self.reported_model = reported_model
        self.report_usage = report_usage
        self.allow_single_verifier = allow_single_verifier
        self.requests: list[AgentExecutionRequest] = []
        self._counts: dict[AgentRole, int] = {}
        self._lock = threading.Lock()
        self._barrier = threading.Barrier(2) if verify_barrier else None

    def execute(self, request: AgentExecutionRequest) -> AgentExecutionResult:
        with self._lock:
            self.requests.append(request)
            count = self._counts.get(request.role, 0) + 1
            self._counts[request.role] = count
        if request.role is AgentRole.PLANNER and self.invalid_plan_once and count == 1:
            return self._result(request, "not valid JSON")
        if request.role is AgentRole.PLANNER:
            artifact = self._plan(request)
        elif request.role is AgentRole.GENERALIST_DEVELOPER:
            artifact = self._work(request)
        elif request.role is AgentRole.TESTER:
            if self._barrier is not None:
                self._wait_for_verifier()
            artifact = self._test_report(request)
            if self.invalid_tester_status_once and count == 1:
                invalid = artifact.model_dump(mode="json")
                invalid["status"] = "pending_review"
                return self._result(
                    request,
                    json.dumps(invalid, ensure_ascii=False),
                )
        elif request.role is AgentRole.REVIEWER:
            if self._barrier is not None:
                self._wait_for_verifier()
            artifact = self._review_report(request)
        else:  # pragma: no cover - the Phase 1 team is fixed
            raise AssertionError(f"unexpected role: {request.role}")
        payload = artifact.model_dump(mode="json")
        if (
            request.role is AgentRole.GENERALIST_DEVELOPER
            and self.inject_fake_git_fields
        ):
            payload.update(
                {
                    "kind": "review_report",
                    "input_commit": "e" * 40,
                    "output_commit": "f" * 40,
                    "changed_files": ["invented.py"],
                }
            )
        if request.role is AgentRole.TESTER and self.tamper_test_commands:
            context = prompt_context(request)
            commands = context["deterministic_command_evidence"]
            assert isinstance(commands, list)
            payload["commands"] = commands
            payload["commands"][0]["summary"] = "Agent-altered summary"
        return self._result(
            request,
            json.dumps(payload, ensure_ascii=False),
        )

    def _wait_for_verifier(self) -> None:
        if self._barrier is None:  # pragma: no cover - guarded by the caller
            return
        try:
            self._barrier.wait(timeout=0.5)
        except threading.BrokenBarrierError:
            if not self.allow_single_verifier:
                raise

    def _plan(self, request: AgentExecutionRequest) -> ImplementationPlanResponse:
        brief = task_brief()
        return ImplementationPlanResponse(
            objective="Build the frozen task-management benchmark.",
            approach=(
                "Implement the complete local Web application.",
                "Add tests and user documentation.",
            ),
            tasks=(
                PlanTask(
                    id="TASK_BUILD",
                    owner=AgentRole.GENERALIST_DEVELOPER,
                    description="Implement and document the complete benchmark.",
                    acceptance_criteria=tuple(
                        criterion.id for criterion in brief.acceptance_criteria
                    ),
                    expected_paths=("app/", "tests/", "README.md"),
                ),
            ),
        )

    def _work(self, request: AgentExecutionRequest) -> WorkResultResponse:
        if self.commit_changes:
            application = self.workspace / "app" / "main.py"
            application.parent.mkdir(parents=True, exist_ok=True)
            application.write_text(
                f'"""Offline iteration {request.iteration}."""\n',
                encoding="utf-8",
            )
            git(self.workspace, "add", "app/main.py")
            git(
                self.workspace,
                "commit",
                "--no-verify",
                "-m",
                f"feat(app): implement iteration {request.iteration}",
            )
        return WorkResultResponse(
            summary=f"Implemented iteration {request.iteration}.",
            completed_tasks=("TASK_BUILD",),
        )

    def _test_report(self, request: AgentExecutionRequest) -> SemanticTestReport:
        return SemanticTestReport(
            summary="Analyzed the fixed offline gate evidence.",
        )

    def _review_report(self, request: AgentExecutionRequest) -> ReviewReportResponse:
        verdict = self.review_verdicts.get(
            request.iteration,
            ReviewVerdict.ACCEPT,
        )
        findings: tuple[ReviewFinding, ...] = ()
        if verdict is not ReviewVerdict.ACCEPT:
            severity = (
                ReviewSeverity.CRITICAL
                if verdict is ReviewVerdict.FAIL
                else ReviewSeverity.HIGH
            )
            findings = (
                ReviewFinding(
                    id=f"FINDING_ITERATION_{request.iteration}",
                    severity=severity,
                    blocking=True,
                    category=(
                        "safety_boundary"
                        if verdict is ReviewVerdict.FAIL
                        else "correctness"
                    ),
                    description=f"Iteration {request.iteration} needs correction.",
                    recommendation="Address the attributable issue.",
                    criterion_ids=("AC_QUALITY",),
                ),
            )
        return ReviewReportResponse(
            verdict=verdict,
            termination_reason=(
                ReviewTerminationReason.SAFETY_BOUNDARY_CROSSED
                if verdict is ReviewVerdict.FAIL
                else None
            ),
            findings=findings,
            summary=f"Reviewer verdict: {verdict.value}.",
        )

    def _result(
        self,
        request: AgentExecutionRequest,
        response_text: str,
    ) -> AgentExecutionResult:
        return AgentExecutionResult(
            status=AgentExecutionStatus.COMPLETED,
            response_text=response_text,
            telemetry=AgentExecutionTelemetry(
                role=request.role,
                session_key=request.session_key,
                command=("offline-agent", request.role.value),
                started_at=FIXED_TIME,
                finished_at=FIXED_TIME,
                duration_ms=5,
                openclaw_duration_ms=5,
                exit_code=0,
                stdout=response_text,
                stderr="",
                openclaw_run_id=f"offline-{request.role.value}",
                session_id=f"offline-{request.role.value}-{request.iteration}",
                provider="offline",
                model=self.reported_model,
                usage=(
                    AgentTokenUsage(
                        input_tokens=10,
                        output_tokens=5,
                        total_tokens=15,
                    )
                    if self.report_usage
                    else None
                ),
            ),
        )


def sandbox_executions(
    *,
    count: int = 8,
    timeout_first: bool = False,
) -> list[SandboxExecution]:
    """Return enough fake Docker-boundary results for two iterations."""

    results = [
        SandboxExecution(
            exit_code=0,
            timed_out=False,
            duration_ms=index + 1,
            stdout=f"gate {index} passed\n".encode(),
            stderr=b"",
        )
        for index in range(count)
    ]
    if timeout_first:
        results[0] = SandboxExecution(
            exit_code=None,
            timed_out=True,
            duration_ms=120_000,
            stdout=b"",
            stderr=b"gate timed out",
        )
    return results


def coordinator(
    tmp_path: Path,
    executor: AgentExecutor,
    *,
    executions: list[SandboxExecution] | None = None,
    budget: AgentBudget | None = None,
    pricing: ModelPricing | None = None,
    verification_concurrency: int = 2,
    stage_timeout_seconds: int | None = 30,
    monotonic: Callable[[], float] = fixed_monotonic,
    progress_handler: Callable[[ProgressEvent], None] | None = None,
) -> WorkflowCoordinator:
    """Build a coordinator with the real gate runner and fake sandbox backend."""

    configuration = load_quality_gate_configuration(POLICY, BENCHMARK)
    backend = FakeSandboxBackend(executions or sandbox_executions())

    def gate_factory(run_directory: Path, workspace: Path) -> QualityGateRunner:
        return QualityGateRunner(
            configuration,
            run_directory=run_directory,
            workspace=workspace,
            backend=backend,
            allow_test_backends=True,
        )

    return WorkflowCoordinator(
        manifest=load_team_manifest(TEAM_CONFIG),
        runs_root=tmp_path / "runs",
        workspaces_root=tmp_path / "workspaces",
        executor=executor,
        quality_gate_factory=gate_factory,
        budget=budget or configuration.policy.agent_budget,
        pricing=pricing
        or ModelPricing(
            model="offline/test-model",
            input_cost_per_million_usd="0",
            output_cost_per_million_usd="0",
        ),
        manual_review_criteria=configuration.benchmark.manual_review_criteria,
        clock=lambda: FIXED_TIME,
        role_timeout_seconds=configuration.policy.agent_stage_timeouts_seconds,
        stage_timeout_seconds=stage_timeout_seconds,
        verification_concurrency=verification_concurrency,
        progress_handler=progress_handler,
        monotonic=monotonic,
    )


def load_run_json(tmp_path: Path) -> dict[str, object]:
    """Load the terminal state written by one test run."""

    return json.loads(
        (tmp_path / "runs" / task_brief().run_id / "run.json").read_text(
            encoding="utf-8"
        )
    )


def test_offline_workflow_completes_with_parallel_independent_verification(
    tmp_path: Path,
) -> None:
    source = initialize_source(tmp_path)
    workspace = tmp_path / "workspaces" / task_brief().run_id
    executor = DynamicWorkflowExecutor(workspace, verify_barrier=True)

    outcome = coordinator(tmp_path, executor).execute(
        task_brief(),
        source_repository=source,
    )

    assert outcome.record.phase is RunPhase.COMPLETED
    assert outcome.record.termination_reason is TerminationReason.SUCCEEDED
    assert len(outcome.execution_records) == 4
    assert len(outcome.handoffs) == 5
    assert (workspace / "app" / "main.py").is_file()
    run_directory = tmp_path / "runs" / task_brief().run_id
    assert (run_directory / "final-report.json").is_file()
    markdown = (run_directory / outcome.human_report_path).read_text(encoding="utf-8")
    assert "Status: `completed`" in markdown
    assert "Agent calls: 4" in markdown
    assert len(list((run_directory / "iterations/01/commands").glob("*.txt"))) == 8
    test_report = json.loads(
        (run_directory / "iterations/01/test-report.json").read_text(encoding="utf-8")
    )
    review_report = json.loads(
        (run_directory / "iterations/01/review-report.json").read_text(encoding="utf-8")
    )
    final_report = json.loads(
        (run_directory / "final-report.json").read_text(encoding="utf-8")
    )
    expected_manual = ["AC_DOCUMENTATION", "AC_ACCESSIBILITY"]
    assert test_report["manual_review_criteria"] == expected_manual
    assert review_report["reviewed_criteria"] == expected_manual
    tester_statuses = {
        item["criterion_id"]: item["status"] for item in test_report["criteria"]
    }
    assert tester_statuses["AC_DOCUMENTATION"] == "pending_review"
    assert tester_statuses["AC_ACCESSIBILITY"] == "pending_review"
    tester_details = {
        item["criterion_id"]: item["detail"] for item in test_report["criteria"]
    }
    assert tester_details["AC_DOCUMENTATION"] == (
        "This criterion is assigned to independent review."
    )
    assert tester_details["AC_ACCESSIBILITY"] == (
        "Deterministic evidence passed; independent review is pending."
    )
    assert {item["status"] for item in final_report["acceptance_results"]} == {"passed"}
    state = load_run_json(tmp_path)
    transitions = state["transitions"]
    assert isinstance(transitions, list)
    assert transitions[-1]["artifacts"][0]["path"] == "final-report.json"


def test_workflow_emits_only_controller_backed_progress_events(tmp_path: Path) -> None:
    source = initialize_source(tmp_path)
    workspace = tmp_path / "workspaces" / task_brief().run_id
    executor = DynamicWorkflowExecutor(workspace)
    events: list[ProgressEvent] = []

    outcome = coordinator(
        tmp_path,
        executor,
        progress_handler=events.append,
    ).execute(task_brief(), source_repository=source)

    assert outcome.record.phase is RunPhase.COMPLETED
    kinds = [event.kind for event in events]
    assert kinds[0] is ProgressEventKind.RUN_STARTED
    assert ProgressEventKind.WORKSPACE_READY in kinds
    assert ProgressEventKind.SNAPSHOT_VERIFIED in kinds
    assert ProgressEventKind.QUALITY_GATES_STARTED in kinds
    assert ProgressEventKind.DECISION_RECORDED in kinds
    assert kinds[-1] is ProgressEventKind.RUN_COMPLETED
    started_roles = {
        event.role for event in events if event.kind is ProgressEventKind.AGENT_STARTED
    }
    assert started_roles == {
        AgentRole.PLANNER,
        AgentRole.GENERALIST_DEVELOPER,
        AgentRole.TESTER,
        AgentRole.REVIEWER,
    }
    rendered_messages = "\n".join(event.message for event in events)
    assert "RUN_CONTEXT_JSON" not in rendered_messages
    assert "response_text" not in rendered_messages


def test_workflow_reports_unconfigured_cost_without_inventing_zero(
    tmp_path: Path,
) -> None:
    source = initialize_source(tmp_path)
    workspace = tmp_path / "workspaces" / task_brief().run_id
    executor = DynamicWorkflowExecutor(workspace)

    outcome = coordinator(
        tmp_path,
        executor,
        pricing=ModelPricing(model="offline/test-model"),
    ).execute(task_brief(), source_repository=source)

    markdown = (
        tmp_path / "runs" / task_brief().run_id / outcome.human_report_path
    ).read_text(encoding="utf-8")
    assert "Estimated model cost: not configured" in markdown
    assert "Estimated model cost: $0.000000" not in markdown


def test_workflow_uses_verified_git_facts_instead_of_model_claims(
    tmp_path: Path,
) -> None:
    source = initialize_source(tmp_path)
    workspace = tmp_path / "workspaces" / task_brief().run_id
    executor = DynamicWorkflowExecutor(workspace, inject_fake_git_fields=True)

    outcome = coordinator(tmp_path, executor).execute(
        task_brief(),
        source_repository=source,
    )

    assert outcome.record.phase is RunPhase.COMPLETED
    work = json.loads(
        (
            tmp_path / "runs" / task_brief().run_id / "iterations/01/work-result.json"
        ).read_text(encoding="utf-8")
    )
    assert work["input_commit"] != "e" * 40
    assert work["output_commit"] != "f" * 40
    assert work["changed_files"] == ["app/main.py"]
    developer_record = next(
        json.loads(
            (tmp_path / "runs" / task_brief().run_id / reference.path).read_text(
                encoding="utf-8"
            )
        )
        for reference in outcome.execution_records
        if "/generalist_developer-attempt-" in reference.path
    )
    assert set(developer_record["ignored_controller_fields"]).issuperset(
        {"kind", "input_commit", "output_commit", "changed_files"}
    )


def test_offline_workflow_can_serialize_independent_verification(
    tmp_path: Path,
) -> None:
    source = initialize_source(tmp_path)
    workspace = tmp_path / "workspaces" / task_brief().run_id
    executor = DynamicWorkflowExecutor(
        workspace,
        verify_barrier=True,
        allow_single_verifier=True,
    )

    outcome = coordinator(
        tmp_path,
        executor,
        verification_concurrency=1,
    ).execute(task_brief(), source_repository=source)

    assert outcome.record.phase is RunPhase.COMPLETED
    verifier_roles = [
        request.role
        for request in executor.requests
        if request.role in {AgentRole.TESTER, AgentRole.REVIEWER}
    ]
    assert verifier_roles == [AgentRole.TESTER, AgentRole.REVIEWER]


def test_workflow_performs_exactly_one_evidence_driven_revision(
    tmp_path: Path,
) -> None:
    source = initialize_source(tmp_path)
    workspace = tmp_path / "workspaces" / task_brief().run_id
    executor = DynamicWorkflowExecutor(
        workspace,
        review_verdicts={1: ReviewVerdict.REVISE},
    )

    outcome = coordinator(tmp_path, executor).execute(
        task_brief(),
        source_repository=source,
    )

    assert outcome.record.phase is RunPhase.COMPLETED
    assert outcome.record.current_iteration == 2
    assert len(outcome.record.snapshots) == 2
    assert len(outcome.execution_records) == 7
    first = json.loads(
        (
            tmp_path
            / "runs"
            / task_brief().run_id
            / "iterations/01/iteration-record.json"
        ).read_text(encoding="utf-8")
    )
    second = json.loads(
        (
            tmp_path
            / "runs"
            / task_brief().run_id
            / "iterations/02/iteration-record.json"
        ).read_text(encoding="utf-8")
    )
    assert first["decision"] == "revise"
    assert second["decision"] == "accept"
    assert second["resolved_finding_ids"] == ["FINDING_ITERATION_1"]


def test_workflow_stops_on_a_terminal_reviewer_boundary(tmp_path: Path) -> None:
    source = initialize_source(tmp_path)
    workspace = tmp_path / "workspaces" / task_brief().run_id
    executor = DynamicWorkflowExecutor(
        workspace,
        review_verdicts={1: ReviewVerdict.FAIL},
    )

    outcome = coordinator(tmp_path, executor).execute(
        task_brief(),
        source_repository=source,
    )

    assert outcome.record.phase is RunPhase.FAILED
    assert (
        outcome.record.termination_reason is TerminationReason.SAFETY_BOUNDARY_CROSSED
    )
    assert outcome.record.current_iteration == 1


def test_workflow_repairs_one_invalid_agent_response(tmp_path: Path) -> None:
    source = initialize_source(tmp_path)
    workspace = tmp_path / "workspaces" / task_brief().run_id
    executor = DynamicWorkflowExecutor(workspace, invalid_plan_once=True)

    outcome = coordinator(tmp_path, executor).execute(
        task_brief(),
        source_repository=source,
    )

    assert outcome.record.phase is RunPhase.COMPLETED
    plan_records = [
        reference
        for reference in outcome.execution_records
        if "/plan/" in reference.path
    ]
    assert len(plan_records) == 2
    repair_prompt = executor.requests[1].prompt
    assert "Revalidate the entire response" in repair_prompt
    assert "Use each key exactly once" in repair_prompt
    assert "every semantic field required" in repair_prompt
    assert "Do not return controller-owned" in repair_prompt
    assert "union of tasks[].acceptance_criteria" in repair_prompt
    assert "tasks[].id to begin with TASK_" in repair_prompt
    first = json.loads(
        (tmp_path / "runs" / task_brief().run_id / plan_records[0].path).read_text(
            encoding="utf-8"
        )
    )
    assert first["attempt"] == 1
    assert first["error"] is not None


def test_response_repair_receives_only_the_remaining_stage_budget(
    tmp_path: Path,
) -> None:
    source = initialize_source(tmp_path)
    workspace = tmp_path / "workspaces" / task_brief().run_id
    elapsed = [0.0]

    class SlowFirstPlan(DynamicWorkflowExecutor):
        def execute(self, request: AgentExecutionRequest) -> AgentExecutionResult:
            result = super().execute(request)
            if request.role is AgentRole.PLANNER and len(self.requests) == 1:
                elapsed[0] = 20.2
            return result

    executor = SlowFirstPlan(workspace, invalid_plan_once=True)
    outcome = coordinator(
        tmp_path,
        executor,
        stage_timeout_seconds=30,
        monotonic=lambda: elapsed[0],
    ).execute(task_brief(), source_repository=source)

    assert outcome.record.phase is RunPhase.COMPLETED
    planner_requests = [
        request for request in executor.requests if request.role is AgentRole.PLANNER
    ]
    assert [request.timeout_seconds for request in planner_requests] == [30, 10]
    plan_records = [
        json.loads(
            (tmp_path / "runs" / task_brief().run_id / reference.path).read_text(
                encoding="utf-8"
            )
        )
        for reference in outcome.execution_records
        if "/plan/" in reference.path
    ]
    assert [record["stage_timeout_seconds"] for record in plan_records] == [30, 30]
    assert [record["remaining_timeout_seconds"] for record in plan_records] == [
        30,
        10,
    ]


def test_workflow_does_not_start_repair_after_the_stage_deadline(
    tmp_path: Path,
) -> None:
    source = initialize_source(tmp_path)
    workspace = tmp_path / "workspaces" / task_brief().run_id
    elapsed = [0.0]

    class ExpiredFirstPlan(DynamicWorkflowExecutor):
        def execute(self, request: AgentExecutionRequest) -> AgentExecutionResult:
            result = super().execute(request)
            elapsed[0] = 31.0
            return result

    executor = ExpiredFirstPlan(workspace, invalid_plan_once=True)
    outcome = coordinator(
        tmp_path,
        executor,
        stage_timeout_seconds=30,
        monotonic=lambda: elapsed[0],
    ).execute(task_brief(), source_repository=source)

    assert outcome.record.phase is RunPhase.FAILED
    assert outcome.record.termination_reason is TerminationReason.RESOURCE_LIMIT_REACHED
    assert len(executor.requests) == 1
    assert "exceeded its 30-second stage timeout" in (
        outcome.record.termination_detail or ""
    )


def test_checked_in_role_stage_budgets_are_frozen_into_the_run(
    tmp_path: Path,
) -> None:
    source = initialize_source(tmp_path)
    workspace = tmp_path / "workspaces" / task_brief().run_id
    executor = DynamicWorkflowExecutor(workspace)

    outcome = coordinator(
        tmp_path,
        executor,
        stage_timeout_seconds=None,
    ).execute(task_brief(), source_repository=source)

    assert outcome.record.phase is RunPhase.COMPLETED
    assert outcome.record.agent_stage_timeouts_seconds[AgentRole.PLANNER] == 120
    assert (
        outcome.record.agent_stage_timeouts_seconds[AgentRole.GENERALIST_DEVELOPER]
        == 900
    )
    assert outcome.record.agent_stage_timeouts_seconds[AgentRole.TESTER] == 300
    assert outcome.record.agent_stage_timeouts_seconds[AgentRole.REVIEWER] == 300
    requested = {request.role: request.timeout_seconds for request in executor.requests}
    assert requested[AgentRole.PLANNER] == 120
    assert requested[AgentRole.GENERALIST_DEVELOPER] == 900
    assert requested[AgentRole.TESTER] == 300
    assert requested[AgentRole.REVIEWER] == 300


def test_workflow_ignores_a_model_supplied_tester_status(tmp_path: Path) -> None:
    source = initialize_source(tmp_path)
    workspace = tmp_path / "workspaces" / task_brief().run_id
    executor = DynamicWorkflowExecutor(
        workspace,
        invalid_tester_status_once=True,
    )

    outcome = coordinator(tmp_path, executor).execute(
        task_brief(),
        source_repository=source,
    )

    assert outcome.record.phase is RunPhase.COMPLETED
    tester_requests = [
        request for request in executor.requests if request.role is AgentRole.TESTER
    ]
    assert len(tester_requests) == 1
    test_report = json.loads(
        (
            tmp_path / "runs" / task_brief().run_id / "iterations/01/test-report.json"
        ).read_text(encoding="utf-8")
    )
    assert test_report["status"] == "passed"
    tester_record = next(
        json.loads(
            (tmp_path / "runs" / task_brief().run_id / reference.path).read_text(
                encoding="utf-8"
            )
        )
        for reference in outcome.execution_records
        if "/tester-attempt-" in reference.path
    )
    assert "status" in tester_record["ignored_controller_fields"]


def test_workflow_ignores_tester_changes_to_controller_evidence(
    tmp_path: Path,
) -> None:
    source = initialize_source(tmp_path)
    workspace = tmp_path / "workspaces" / task_brief().run_id
    executor = DynamicWorkflowExecutor(workspace, tamper_test_commands=True)

    outcome = coordinator(tmp_path, executor).execute(
        task_brief(),
        source_repository=source,
    )

    assert outcome.record.phase is RunPhase.COMPLETED
    test_report = json.loads(
        (
            tmp_path / "runs" / task_brief().run_id / "iterations/01/test-report.json"
        ).read_text(encoding="utf-8")
    )
    assert test_report["commands"][0]["summary"] != "Agent-altered summary"
    tester_record = next(
        json.loads(
            (tmp_path / "runs" / task_brief().run_id / reference.path).read_text(
                encoding="utf-8"
            )
        )
        for reference in outcome.execution_records
        if "/tester-attempt-" in reference.path
    )
    assert "commands" in tester_record["ignored_controller_fields"]


def test_workflow_records_gate_timeout_as_dependency_failure(tmp_path: Path) -> None:
    source = initialize_source(tmp_path)
    workspace = tmp_path / "workspaces" / task_brief().run_id
    executor = DynamicWorkflowExecutor(workspace)

    outcome = coordinator(
        tmp_path,
        executor,
        executions=sandbox_executions(timeout_first=True),
    ).execute(task_brief(), source_repository=source)

    assert outcome.record.phase is RunPhase.FAILED
    assert outcome.record.termination_reason is TerminationReason.DEPENDENCY_UNAVAILABLE
    iteration = json.loads(
        (
            tmp_path
            / "runs"
            / task_brief().run_id
            / "iterations/01/iteration-record.json"
        ).read_text(encoding="utf-8")
    )
    assert iteration["decision"] == "fail"
    assert iteration["blocking_reasons"] == [
        "Deterministic command CHECK_COMPILE timed out."
    ]


def test_workflow_records_openclaw_declared_timeout_as_resource_limit(
    tmp_path: Path,
) -> None:
    source = initialize_source(tmp_path)

    class TimedOutExecutor:
        def execute(self, request: AgentExecutionRequest) -> AgentExecutionResult:
            return AgentExecutionResult(
                status=AgentExecutionStatus.TIMED_OUT,
                error="OpenClaw reported an Agent timeout",
                telemetry=AgentExecutionTelemetry(
                    role=request.role,
                    session_key=request.session_key,
                    command=("openclaw", "agent"),
                    started_at=FIXED_TIME,
                    finished_at=FIXED_TIME,
                    duration_ms=30_000,
                    openclaw_duration_ms=30_000,
                    exit_code=0,
                    timed_out=True,
                    stdout='{"payloads": []}',
                    stderr="embedded run timeout",
                    session_id="timeout-session",
                    provider="offline",
                    model="offline/test-model",
                    usage=AgentTokenUsage(
                        input_tokens=123,
                        output_tokens=7,
                        total_tokens=130,
                    ),
                ),
            )

    outcome = coordinator(tmp_path, TimedOutExecutor()).execute(
        task_brief(),
        source_repository=source,
    )

    assert outcome.record.phase is RunPhase.FAILED
    assert outcome.record.termination_reason is TerminationReason.RESOURCE_LIMIT_REACHED
    execution = json.loads(
        (
            tmp_path / "runs" / task_brief().run_id / outcome.execution_records[0].path
        ).read_text(encoding="utf-8")
    )
    assert execution["timed_out"] is True
    assert execution["exit_code"] == 0
    assert execution["provider"] == "offline"
    assert execution["model"] == "offline/test-model"
    assert execution["input_tokens"] == 123
    assert execution["stage_timeout_seconds"] == 30
    assert execution["remaining_timeout_seconds"] == 30


def test_workflow_stops_at_the_phase1_iteration_limit(tmp_path: Path) -> None:
    source = initialize_source(tmp_path)
    workspace = tmp_path / "workspaces" / task_brief().run_id
    executor = DynamicWorkflowExecutor(
        workspace,
        review_verdicts={1: ReviewVerdict.REVISE, 2: ReviewVerdict.REVISE},
    )

    outcome = coordinator(tmp_path, executor).execute(
        task_brief(),
        source_repository=source,
    )

    assert outcome.record.phase is RunPhase.FAILED
    assert (
        outcome.record.termination_reason is TerminationReason.ITERATION_LIMIT_REACHED
    )
    assert outcome.record.current_iteration == 2
    assert len(outcome.record.snapshots) == 2


def test_workflow_exposes_a_developer_that_produces_no_real_commit(
    tmp_path: Path,
) -> None:
    source = initialize_source(tmp_path)
    workspace = tmp_path / "workspaces" / task_brief().run_id
    executor = DynamicWorkflowExecutor(workspace, commit_changes=False)

    outcome = coordinator(tmp_path, executor).execute(
        task_brief(),
        source_repository=source,
    )

    assert outcome.record.phase is RunPhase.FAILED
    assert outcome.record.termination_reason is TerminationReason.NO_RELEVANT_CHANGE
    assert outcome.record.snapshots == ()


def test_workflow_fails_after_crossing_estimated_cost_threshold(
    tmp_path: Path,
) -> None:
    source = initialize_source(tmp_path)
    workspace = tmp_path / "workspaces" / task_brief().run_id
    executor = DynamicWorkflowExecutor(workspace)
    budget = AgentBudget(
        max_calls=14,
        max_input_tokens=1_000_000,
        max_output_tokens=200_000,
        max_agent_duration_seconds=7200,
        max_estimated_cost_usd="0.000001",
    )
    pricing = ModelPricing(
        model="offline/test-model",
        input_cost_per_million_usd="1000",
        output_cost_per_million_usd="1000",
    )

    outcome = coordinator(
        tmp_path,
        executor,
        budget=budget,
        pricing=pricing,
    ).execute(task_brief(), source_repository=source)

    assert outcome.record.phase is RunPhase.FAILED
    assert outcome.record.termination_reason is TerminationReason.RESOURCE_LIMIT_REACHED
    execution = json.loads(
        (
            tmp_path / "runs" / task_brief().run_id / outcome.execution_records[0].path
        ).read_text(encoding="utf-8")
    )
    assert execution["error"] == "Agent estimated-cost budget was exceeded"


def test_parallel_verification_never_exceeds_the_agent_call_limit(
    tmp_path: Path,
) -> None:
    source = initialize_source(tmp_path)
    workspace = tmp_path / "workspaces" / task_brief().run_id
    executor = DynamicWorkflowExecutor(
        workspace,
        review_verdicts={1: ReviewVerdict.REVISE},
        verify_barrier=True,
        allow_single_verifier=True,
    )
    budget = AgentBudget(
        max_calls=6,
        max_input_tokens=1_000_000,
        max_output_tokens=200_000,
        max_agent_duration_seconds=7200,
        max_estimated_cost_usd="25",
    )

    outcome = coordinator(tmp_path, executor, budget=budget).execute(
        task_brief(),
        source_repository=source,
    )

    assert outcome.record.phase is RunPhase.FAILED
    assert outcome.record.termination_reason is TerminationReason.RESOURCE_LIMIT_REACHED
    assert len(outcome.execution_records) == budget.max_calls
    assert len(executor.requests) == budget.max_calls


def test_workflow_rejects_success_without_reported_model(tmp_path: Path) -> None:
    source = initialize_source(tmp_path)
    workspace = tmp_path / "workspaces" / task_brief().run_id
    executor = DynamicWorkflowExecutor(workspace, reported_model=None)

    outcome = coordinator(tmp_path, executor).execute(
        task_brief(),
        source_repository=source,
    )

    assert outcome.record.phase is RunPhase.FAILED
    assert outcome.record.termination_reason is TerminationReason.DEPENDENCY_UNAVAILABLE
    assert len(outcome.execution_records) == 1
    execution = json.loads(
        (
            tmp_path / "runs" / task_brief().run_id / outcome.execution_records[0].path
        ).read_text(encoding="utf-8")
    )
    assert execution["model"] is None
    assert execution["error"] == "successful execution omitted model metadata"


def test_workflow_rejects_success_without_token_usage(tmp_path: Path) -> None:
    source = initialize_source(tmp_path)
    workspace = tmp_path / "workspaces" / task_brief().run_id
    executor = DynamicWorkflowExecutor(workspace, report_usage=False)

    outcome = coordinator(tmp_path, executor).execute(
        task_brief(),
        source_repository=source,
    )

    assert outcome.record.phase is RunPhase.FAILED
    assert outcome.record.termination_reason is TerminationReason.DEPENDENCY_UNAVAILABLE
    assert len(outcome.execution_records) == 1
    execution = json.loads(
        (
            tmp_path / "runs" / task_brief().run_id / outcome.execution_records[0].path
        ).read_text(encoding="utf-8")
    )
    assert execution["input_tokens"] is None
    assert execution["error"] == "successful execution omitted token usage"
