"""Integration tests for controller-owned dynamic Agent invocation."""

from __future__ import annotations

import hashlib
import json
import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from software_agent_team.artifact_store import ArtifactStore
from software_agent_team.artifacts import (
    AcceptanceCriterion,
    AgentExecutionRecord,
    AgentToolCallEvidence,
    AgentToolEvidenceStatus,
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
    AgentExecutionActivity,
    AgentExecutionActivityHandler,
    AgentExecutionActivityKind,
    AgentExecutionRequest,
    AgentExecutionResult,
    AgentExecutionStatus,
    AgentExecutionTelemetry,
    AgentTokenUsage,
    ProviderLivenessEvidence,
)
from software_agent_team.git_workspace import GitWorkspace, GitWorkspaceManager
from software_agent_team.integrity import canonical_model_sha256
from software_agent_team.invocation_lifecycle import (
    InitializationCheckpoint,
    InvocationPhase,
    InvocationStopReason,
)
from software_agent_team.planning import AdaptiveImplementationPlan, ProposedTask
from software_agent_team.progress import ProgressEvent, ProgressEventKind
from software_agent_team.response_corrections import semantic_payload_sha256
from software_agent_team.responses import (
    ReviewCriterionAssessmentResponse,
    ReviewReportResponse,
    ReviewToolEvidenceClaim,
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
from software_agent_team.submissions import (
    AgentSemanticSubmission,
    AgentSubmissionEvidence,
    AgentSubmissionStatus,
    canonical_json_sha256,
    rejected_submission_evidence,
)
from software_agent_team.teams import (
    AgentCapability,
    AgentSpec,
    ModelRoute,
    ModelRouteAssignment,
    ModelRoutePlan,
    ModelRouteSelectionSource,
    ModelRoutingMode,
    ModelSwitchCondition,
    PermissionProfile,
    PlanApprovalSource,
    TeamPlan,
    TeamPlanOrigin,
)

FIXED_TIME = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
MODEL = "test/provider-model"


def semantic_correction_response(
    base_payload: dict[str, object],
    replacements: dict[str, object],
) -> str:
    """Return the exact field-only correction contract used by SAT."""

    return json.dumps(
        {
            "kind": "semantic_correction_v2",
            "base_response_sha256": semantic_payload_sha256(base_payload),
            "replacement_values": [replacements[path] for path in sorted(replacements)],
        }
    )


def review_tool_claim() -> ReviewToolEvidenceClaim:
    """Select the fake executor's attributable read result."""

    return ReviewToolEvidenceClaim(observable="fake-review-observation")


def review_tool_call() -> AgentToolCallEvidence:
    """Return the fake executor's sanitized read record."""

    output = b"fake-review-observation"
    return AgentToolCallEvidence(
        id="tool-001",
        tool_name="read",
        external_call_sha256=hashlib.sha256(b"fake-review-call").hexdigest(),
        arguments_sha256=hashlib.sha256(b'{"path":"/agent"}').hexdigest(),
        outcome="succeeded",
        is_error=False,
        output_sha256=hashlib.sha256(output).hexdigest(),
        output_bytes=len(output),
        output_excerpt=output.decode("utf-8"),
    )


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
    include_quality_tasks: bool = False,
    chain_quality: bool = False,
    run_budget: AgentBudget | None = None,
    writer_scope: str = "repository",
) -> tuple[TaskBrief, AdaptiveImplementationPlan, TeamPlan]:
    """Build one internally coherent approved plan for runner tests."""

    task_brief = brief()
    tasks = [
        ProposedTask(
            id="TASK_BUILD",
            owner_agent_id="builder",
            description="Implement and document the greeting utility.",
            acceptance_criteria=("AC_CODE", "AC_REVIEW"),
            expected_paths=("greeting.py", "README.md"),
        )
    ]
    if include_quality_tasks and include_tester:
        tasks.append(
            ProposedTask(
                id="TASK_TEST",
                owner_agent_id="tester",
                description="Analyze the deterministic greeting checks.",
                dependencies=("TASK_BUILD",),
                acceptance_criteria=("AC_CODE",),
                expected_paths=("greeting.py",),
            )
        )
    if include_quality_tasks:
        tasks.append(
            ProposedTask(
                id="TASK_REVIEW",
                owner_agent_id="reviewer",
                description="Review documentation against the final behavior.",
                dependencies=(
                    ("TASK_TEST",)
                    if chain_quality and include_tester
                    else ("TASK_BUILD",)
                ),
                acceptance_criteria=("AC_REVIEW",),
                expected_paths=("greeting.py", "README.md"),
            )
        )
    implementation_plan = AdaptiveImplementationPlan(
        run_id=task_brief.run_id,
        team_id="adaptive_team",
        revision=1,
        created_at=FIXED_TIME,
        objective="Implement and independently verify the greeting utility.",
        approach=("Implement one cohesive change.", "Verify the final commit."),
        tasks=tuple(tasks),
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
            dependencies=(
                ("tester",) if chain_quality and include_tester else ("builder",)
            ),
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
        invalid_writer_transport: bool = False,
        omit_model_for: str | None = None,
        mutate_reader: str | None = None,
        synchronize_quality: bool = False,
        provider_fail_once_for: str | None = None,
        provider_stall_once_for: str | None = None,
        zero_review_tool_calls_once: bool = False,
        invalid_review_response_once: bool = False,
        invalid_review_evidence: bool = False,
        unapproved_review_boundaries: bool = False,
        writer_presentation_arrays: bool = False,
        writer_summary: str = "Implemented and documented the greeting utility.",
    ) -> None:
        self.workspace = workspace
        self.invalid_writer_once = invalid_writer_once
        self.invalid_writer_transport = invalid_writer_transport
        self.omit_model_for = omit_model_for
        self.mutate_reader = mutate_reader
        self.provider_fail_once_for = provider_fail_once_for
        self.provider_stall_once_for = provider_stall_once_for
        self.zero_review_tool_calls_once = zero_review_tool_calls_once
        self.invalid_review_response_once = invalid_review_response_once
        self.invalid_review_evidence = invalid_review_evidence
        self.unapproved_review_boundaries = unapproved_review_boundaries
        self.writer_presentation_arrays = writer_presentation_arrays
        self.writer_summary = writer_summary
        self.requests: list[AgentExecutionRequest] = []
        self._counts: dict[str, int] = {}
        self._lock = threading.Lock()
        self._quality_barrier = threading.Barrier(2) if synchronize_quality else None

    def execute(
        self,
        request: AgentExecutionRequest,
        *,
        activity_handler: AgentExecutionActivityHandler | None = None,
    ) -> AgentExecutionResult:
        with self._lock:
            self.requests.append(request)
            count = self._counts.get(request.agent_id, 0) + 1
            self._counts[request.agent_id] = count
        self._emit_lifecycle_start(request, activity_handler)
        if self.provider_fail_once_for == request.agent_id and count == 1:
            self._emit_lifecycle_stop(
                request,
                activity_handler,
                InvocationStopReason.PROVIDER_FAILURE,
            )
            return AgentExecutionResult(
                status=AgentExecutionStatus.PROVIDER_FAILED,
                error="scripted provider failure",
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
                    stdout="",
                    stderr="scripted provider failure",
                    session_id=f"session-{request.agent_id}",
                    provider="test",
                    model=request.model,
                ),
            )
        if self.provider_stall_once_for == request.agent_id and count == 1:
            if activity_handler is not None:
                for kind, elapsed_ms, inactivity_ms in (
                    (AgentExecutionActivityKind.STALL_SUSPECTED, 90_000, 90_000),
                    (AgentExecutionActivityKind.PROVIDER_STALLED, 120_000, 120_000),
                ):
                    activity_handler(
                        AgentExecutionActivity(
                            kind=kind,
                            agent_id=request.agent_id,
                            session_key=request.session_key,
                            model=request.model,
                            elapsed_ms=elapsed_ms,
                            trusted_activity_count=0,
                            active_tool_count=0,
                            inactivity_ms=inactivity_ms,
                            silence_seconds=120,
                            stall_grace_seconds=30,
                            policy_source="test provider contract",
                        )
                    )
            self._emit_lifecycle_stop(
                request,
                activity_handler,
                InvocationStopReason.PROVIDER_STALL,
            )
            return AgentExecutionResult(
                status=AgentExecutionStatus.PROVIDER_STALLED,
                error="scripted provider stall",
                telemetry=AgentExecutionTelemetry(
                    role=None,
                    agent_id=request.agent_id,
                    capability=request.capability,
                    session_key=request.session_key,
                    command=("fake-agent", request.agent_id),
                    started_at=FIXED_TIME,
                    finished_at=FIXED_TIME,
                    duration_ms=10,
                    exit_code=-15,
                    stdout="",
                    stderr="",
                    session_id=f"session-{request.agent_id}",
                    provider="test",
                    model=request.model,
                    provider_liveness=ProviderLivenessEvidence(
                        mode="enforced",
                        policy_source="test provider contract",
                        silence_seconds=120,
                        stall_grace_seconds=30,
                        lease_started=True,
                        lease_start_source="current_turn",
                        session_observed=True,
                        provider_activity_observations=0,
                        tool_started_count=0,
                        tool_completed_count=0,
                        stall_suspected_count=1,
                        stall_recovered_count=0,
                        maximum_inactivity_ms=120_000,
                        stalled=True,
                    ),
                ),
            )
        if request.agent_id == "builder":
            if not (self.workspace / "greeting.py").exists():
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
            valid_payload = WorkResultResponse(
                summary=self.writer_summary,
                completed_tasks=("TASK_BUILD",),
                unresolved_issues=(),
            ).model_dump(mode="json")
            response_text = json.dumps(valid_payload)
            submission_payload: dict[str, object] | None = valid_payload
            if self.writer_presentation_arrays:
                response_text = (
                    'Verified setup ["uv", "sync", "--dev"] and tests '
                    f'["uv", "run", "pytest"].\n{response_text}'
                )
            if self.invalid_writer_transport:
                response_text = "not valid JSON"
                submission_payload = None
            elif self.invalid_writer_once:
                invalid_payload = dict(valid_payload)
                invalid_payload["completed_tasks"] = ["TASK_UNKNOWN"]
                if count == 1:
                    submission_payload = invalid_payload
                    response_text = json.dumps(invalid_payload)
                else:
                    response_text = semantic_correction_response(
                        invalid_payload,
                        {"/completed_tasks": valid_payload["completed_tasks"]},
                    )
                    submission_payload = json.loads(response_text)
        elif request.agent_id == "tester":
            self._wait_for_quality_peer()
            submission_payload = SemanticTestReportResponse(
                findings=(),
                summary="Deterministic evidence covers the implemented behavior.",
            ).model_dump(mode="json")
            response_text = json.dumps(submission_payload)
        elif request.agent_id == "reviewer":
            if self.mutate_reader == request.agent_id:
                (self.workspace / "MUTATION.txt").write_text(
                    "read-only Agent mutation\n",
                    encoding="utf-8",
                )
            self._wait_for_quality_peer()
            valid_payload = ReviewReportResponse(
                verdict="accept",
                criterion_assessments=(
                    ReviewCriterionAssessmentResponse(
                        criterion_id="AC_REVIEW",
                        status="satisfied",
                        adversarial_check=(
                            "Compared the documented return behavior with the "
                            "implemented function."
                        ),
                        evidence=(
                            "README.md and greeting.py describe the same string result."
                        ),
                        tool_evidence=(review_tool_claim(),),
                    ),
                ),
                findings=(),
                summary="The final commit satisfies the assigned review scope.",
            ).model_dump(mode="json")
            if self.unapproved_review_boundaries:
                assessments = valid_payload["criterion_assessments"]
                assert isinstance(assessments, list)
                assessment = assessments[0]
                assert isinstance(assessment, dict)
                assessment["boundary_checks"] = [
                    {
                        "boundary": "top_level_input",
                        "adversarial_check": (
                            "Added a direct-input check outside the approved scope."
                        ),
                        "tool_evidence": [
                            {"observable": "duplicate-unapproved-marker"}
                        ],
                    },
                    {
                        "boundary": "nested_input",
                        "adversarial_check": (
                            "Added a nested-input check outside the approved scope."
                        ),
                        "tool_evidence": [
                            {"observable": "duplicate-unapproved-marker"}
                        ],
                    },
                ]
            response_text = json.dumps(valid_payload)
            submission_payload = valid_payload
            if self.invalid_review_response_once:
                invalid_payload = dict(valid_payload)
                invalid_payload["summary"] = ""
                if count == 1:
                    submission_payload = invalid_payload
                    response_text = json.dumps(invalid_payload)
                else:
                    response_text = semantic_correction_response(
                        invalid_payload,
                        {"/summary": valid_payload["summary"]},
                    )
                    submission_payload = json.loads(response_text)
            elif self.zero_review_tool_calls_once and count == 2:
                response_text = semantic_correction_response(
                    valid_payload,
                    {"/criterion_assessments": valid_payload["criterion_assessments"]},
                )
                submission_payload = json.loads(response_text)
        else:  # pragma: no cover - the fixture owns the complete team
            raise AssertionError(f"unexpected Agent: {request.agent_id}")
        self._emit_lifecycle_stop(
            request,
            activity_handler,
            InvocationStopReason.COMPLETED,
        )
        return self._result(request, response_text, submission_payload)

    @staticmethod
    def _emit_lifecycle_start(
        request: AgentExecutionRequest,
        activity_handler: AgentExecutionActivityHandler | None,
    ) -> None:
        if activity_handler is None:
            return
        for kind, phase, checkpoint, action in (
            (
                AgentExecutionActivityKind.INVOCATION_LAUNCHED,
                InvocationPhase.LAUNCHED,
                None,
                "Test adapter accepted the invocation",
            ),
            (
                AgentExecutionActivityKind.INVOCATION_INITIALIZING,
                InvocationPhase.INITIALIZING,
                InitializationCheckpoint.CURRENT_TURN,
                "Test adapter established an attributable current turn",
            ),
            (
                AgentExecutionActivityKind.INVOCATION_PROVIDER_WAIT,
                InvocationPhase.PROVIDER_WAIT,
                InitializationCheckpoint.CURRENT_TURN,
                "Test adapter is waiting for the approved model",
            ),
        ):
            activity_handler(
                AgentExecutionActivity(
                    kind=kind,
                    agent_id=request.agent_id,
                    session_key=request.session_key,
                    model=request.model,
                    elapsed_ms=0,
                    invocation_phase=phase,
                    initialization_checkpoint=checkpoint,
                    action=action,
                )
            )

    @staticmethod
    def _emit_lifecycle_stop(
        request: AgentExecutionRequest,
        activity_handler: AgentExecutionActivityHandler | None,
        reason: InvocationStopReason,
    ) -> None:
        if activity_handler is None:
            return
        for kind, phase, grace, action in (
            (
                AgentExecutionActivityKind.INVOCATION_STOPPING,
                InvocationPhase.STOPPING,
                35.0,
                "Test adapter entered controlled shutdown",
            ),
            (
                AgentExecutionActivityKind.INVOCATION_COLLECTING_EVIDENCE,
                InvocationPhase.COLLECTING_EVIDENCE,
                None,
                "Test adapter is collecting invocation evidence",
            ),
            (
                AgentExecutionActivityKind.INVOCATION_STOPPED,
                InvocationPhase.STOPPED,
                None,
                "Test adapter collected process and cleanup evidence",
            ),
        ):
            activity_handler(
                AgentExecutionActivity(
                    kind=kind,
                    agent_id=request.agent_id,
                    session_key=request.session_key,
                    model=request.model,
                    elapsed_ms=10,
                    invocation_phase=phase,
                    stop_reason=reason,
                    shutdown_grace_seconds=grace,
                    action=action,
                )
            )

    def _wait_for_quality_peer(self) -> None:
        if self._quality_barrier is None:
            return
        self._quality_barrier.wait(timeout=2)

    def _result(
        self,
        request: AgentExecutionRequest,
        response_text: str,
        submission_payload: dict[str, object] | None,
    ) -> AgentExecutionResult:
        is_review = request.capability is AgentCapability.REVIEW
        omit_review_call = is_review and (
            (self.zero_review_tool_calls_once and self._counts[request.agent_id] == 1)
            or (
                self.invalid_review_response_once
                and self._counts[request.agent_id] == 2
            )
        )
        invalid_review_evidence = is_review and self.invalid_review_evidence
        contract = request.submission_contract
        assert contract is not None
        binding_sha256 = hashlib.sha256(
            f"{request.session_key}\x00{contract.schema_sha256}".encode()
        ).hexdigest()
        review_calls = (
            ()
            if omit_review_call or invalid_review_evidence
            else ((review_tool_call(),) if is_review else ())
        )
        semantic_submission = None
        submission_evidence = None
        tool_calls = review_calls
        if submission_payload is not None and not invalid_review_evidence:
            external_id = f"fake-submission-{len(review_calls) + 1:03d}"
            output = b"fake-semantic-submission"
            submission_call = AgentToolCallEvidence(
                id=f"tool-{len(review_calls) + 1:03d}",
                tool_name=contract.tool_name,
                external_call_sha256=hashlib.sha256(external_id.encode()).hexdigest(),
                arguments_sha256=canonical_json_sha256(submission_payload),
                outcome="succeeded",
                is_error=False,
                output_sha256=hashlib.sha256(output).hexdigest(),
                output_bytes=len(output),
                output_excerpt=output.decode(),
            )
            tool_calls = (*review_calls, submission_call)
            submission_evidence = AgentSubmissionEvidence(
                purpose=contract.purpose,
                status=AgentSubmissionStatus.ACCEPTED,
                schema_sha256=contract.schema_sha256,
                binding_sha256=binding_sha256,
                tool_call_id=submission_call.id,
                payload_sha256=canonical_json_sha256(submission_payload),
            )
            semantic_submission = AgentSemanticSubmission(
                payload=submission_payload,
                evidence=submission_evidence,
            )
        elif invalid_review_evidence:
            submission_evidence = rejected_submission_evidence(
                contract,
                binding_sha256=binding_sha256,
                status=AgentSubmissionStatus.UNAUTHORIZED,
                code="tool_evidence_unavailable",
                detail=(
                    "submission cannot be attributed because tool evidence is invalid"
                ),
            )
        else:
            submission_evidence = rejected_submission_evidence(
                contract,
                binding_sha256=binding_sha256,
                status=AgentSubmissionStatus.MISSING,
                code="submission_missing",
                detail=(
                    "the Agent completed without calling the required submission tool"
                ),
            )
        telemetry = AgentExecutionTelemetry(
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
            model=(None if self.omit_model_for == request.agent_id else request.model),
            usage=AgentTokenUsage(
                input_tokens=10,
                output_tokens=5,
                total_tokens=15,
            ),
            tool_evidence_status=(
                AgentToolEvidenceStatus.INVALID
                if invalid_review_evidence
                else (
                    AgentToolEvidenceStatus.CAPTURED
                    if tool_calls
                    else AgentToolEvidenceStatus.NOT_CAPTURED
                )
            ),
            session_transcript_sha256=(
                "d" * 64 if tool_calls and not invalid_review_evidence else None
            ),
            session_record_count=(
                max(3, 2 + len(tool_calls))
                if tool_calls and not invalid_review_evidence
                else None
            ),
            tool_calls=tool_calls,
            tool_evidence_error=(
                "session transcript identity mismatch"
                if invalid_review_evidence
                else None
            ),
        )
        if semantic_submission is None:
            assert submission_evidence is not None
            return AgentExecutionResult(
                status=AgentExecutionStatus.INVALID_RESPONSE,
                error=(
                    "typed artifact submission rejected: "
                    f"{submission_evidence.diagnostic_code}"
                ),
                telemetry=telemetry,
                submission_evidence=submission_evidence,
            )
        return AgentExecutionResult(
            status=AgentExecutionStatus.COMPLETED,
            response_text=response_text,
            telemetry=telemetry,
            semantic_submission=semantic_submission,
            submission_evidence=submission_evidence,
        )


def runtime(
    tmp_path: Path,
    *,
    include_tester: bool = True,
    include_quality_tasks: bool = False,
    chain_quality: bool = False,
    run_budget: AgentBudget | None = None,
    writer_scope: str = "repository",
    executor_options: dict[str, object] | None = None,
    model_switching: bool = False,
) -> tuple[DynamicAgentRunner, TeamPlan, DynamicExecutor, FakeQualityGate, Path]:
    """Prepare a real isolated workspace and dynamic runner."""

    task_brief, implementation_plan, team_plan = dynamic_inputs(
        include_tester=include_tester,
        include_quality_tasks=include_quality_tasks,
        chain_quality=chain_quality,
        run_budget=run_budget,
        writer_scope=writer_scope,
    )
    if model_switching:
        assignments = tuple(
            ModelRouteAssignment(
                agent_id=agent.id,
                primary_route_id="default",
                fallback_route_ids=("fallback",),
                selection_source=ModelRouteSelectionSource.DEFAULT_PROFILE,
                reason="Test-authorized default with one provider fallback.",
            )
            for agent in team_plan.agents
        )
        team_plan = TeamPlan.model_validate(
            {
                **team_plan.model_dump(mode="json"),
                "model_routes": ModelRoutePlan(
                    mode=ModelRoutingMode.POLICY,
                    default_route_id="default",
                    routes=(
                        ModelRoute(id="default", model=MODEL),
                        ModelRoute(id="fallback", model="test/fallback-model"),
                    ),
                    assignments=assignments,
                    authorized_switch_conditions=(
                        ModelSwitchCondition.PROVIDER_FAILURE,
                    ),
                ).model_dump(mode="json"),
            }
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
        pricing_by_model={
            route.model: ModelPricing(model=route.model)
            for route in team_plan.model_routes.routes
        },
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


def test_dynamic_runner_preserves_quality_tasks_as_read_only_prompt_focus(
    tmp_path: Path,
) -> None:
    runner, team_plan, executor, quality_gate, _ = runtime(
        tmp_path,
        include_quality_tasks=True,
        executor_options={"synchronize_quality": True},
    )

    result = DagScheduler().execute(team_plan, runner)

    assert result.status is ScheduleStatus.COMPLETED
    assert result.max_observed_concurrency == 2
    assert quality_gate.calls == 1
    prompts = {request.agent_id: request.prompt for request in executor.requests}
    assert '"id": "TASK_TEST"' in prompts["tester"]
    assert '"owner_agent_id": "tester"' in prompts["tester"]
    assert '"id": "TASK_REVIEW"' in prompts["reviewer"]
    assert '"owner_agent_id": "reviewer"' in prompts["reviewer"]
    compact_tester = " ".join(prompts["tester"].split())
    compact_reviewer = " ".join(prompts["reviewer"].split())
    assert "do not grant write access" in compact_tester
    assert "never treat it as permission" in compact_reviewer
    assert all(
        agent.permission_profile is PermissionProfile.READ_ONLY
        for agent in team_plan.agents
        if agent.id in {"tester", "reviewer"}
    )


def test_dynamic_runner_executes_an_approved_testing_to_review_handoff(
    tmp_path: Path,
) -> None:
    runner, team_plan, executor, quality_gate, _ = runtime(
        tmp_path,
        include_quality_tasks=True,
        chain_quality=True,
    )

    result = DagScheduler().execute(team_plan, runner)

    assert result.status is ScheduleStatus.COMPLETED
    assert result.max_observed_concurrency == 1
    assert result.completion_order == ("builder", "tester", "reviewer")
    assert quality_gate.calls == 1
    reviewer_request = next(
        request for request in executor.requests if request.agent_id == "reviewer"
    )
    assert '"agent_id": "tester"' in reviewer_request.prompt
    assert "Deterministic evidence covers the implemented behavior." in (
        reviewer_request.prompt
    )
    handoffs = [runner.artifact_store.load(reference) for reference in runner.handoffs]
    assert any(
        isinstance(item, HandoffEnvelope)
        and item.source_agent_id == "tester"
        and item.target_agent_id == "reviewer"
        for item in handoffs
    )


def test_dynamic_runner_projects_long_artifact_summaries_without_failing_handoff(
    tmp_path: Path,
) -> None:
    writer_summary = "Implemented the complete requested behavior.\n" + "x" * 4_000
    runner, team_plan, executor, quality_gate, _ = runtime(
        tmp_path,
        executor_options={"writer_summary": writer_summary},
    )

    result = DagScheduler().execute(team_plan, runner)

    assert result.status is ScheduleStatus.COMPLETED
    assert quality_gate.calls == 1
    assert len(executor.requests) == 3
    work = runner.artifact_store.load(runner.outputs["builder"])
    assert isinstance(work, WorkResult)
    assert work.summary == writer_summary
    writer_record = next(
        record for record in result.records if record.agent_id == "builder"
    )
    assert len(writer_record.summary) <= 2_000
    assert "Controller projection" in writer_record.summary
    digest = hashlib.sha256(writer_summary.encode()).hexdigest()
    quality_requests = [
        request
        for request in executor.requests
        if request.agent_id in {"tester", "reviewer"}
    ]
    assert len(quality_requests) == 2
    assert all(
        "truncated from 4045 characters" in item.prompt for item in quality_requests
    )
    assert all(f"source sha256={digest}" in item.prompt for item in quality_requests)
    assert all(writer_summary not in item.prompt for item in quality_requests)


def test_dynamic_writer_targeted_correction_keeps_timeout_and_git_evidence(
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
    assert "TARGETED_SEMANTIC_CORRECTION_V2" in writer_requests[1].prompt
    assert "Do not regenerate or repeat that object" in writer_requests[1].prompt
    assert len(runner.execution_records) == 4
    writer_records = [
        runner.artifact_store.load(reference)
        for reference in runner.execution_records
        if "/implement/builder-" in reference.path
    ]
    assert len(writer_records) == 2
    assert isinstance(writer_records[0], AgentExecutionRecord)
    assert writer_records[0].error is not None
    assert writer_records[0].response_validation is not None
    assert writer_records[0].response_validation.correction_paths == (
        "/completed_tasks",
    )
    assert isinstance(writer_records[1], AgentExecutionRecord)
    assert writer_records[1].semantic_correction_request is not None
    assert writer_records[1].semantic_correction_outcome == "accepted"
    assert writer_records[1].response_artifact == runner.outputs["builder"]


def test_dynamic_writer_missing_typed_submission_fails_without_correction(
    tmp_path: Path,
) -> None:
    runner, team_plan, executor, _, _ = runtime(
        tmp_path,
        executor_options={"invalid_writer_transport": True},
    )

    result = DagScheduler().execute(team_plan, runner)

    assert result.status is ScheduleStatus.FAILED
    writer_requests = [
        request for request in executor.requests if request.agent_id == "builder"
    ]
    assert len(writer_requests) == 1
    writer_record = next(
        runner.artifact_store.load(reference)
        for reference in runner.execution_records
        if "/implement/builder-" in reference.path
    )
    assert isinstance(writer_record, AgentExecutionRecord)
    assert writer_record.response_validation is None
    assert writer_record.submission_evidence is not None
    assert writer_record.submission_evidence.status is AgentSubmissionStatus.MISSING
    assert writer_record.submission_evidence.diagnostic_code == "submission_missing"
    assert writer_record.semantic_correction_request is None


def test_dynamic_reviewer_repairs_a_zero_call_fabricated_tool_citation(
    tmp_path: Path,
) -> None:
    runner, team_plan, executor, _, _ = runtime(
        tmp_path,
        executor_options={"zero_review_tool_calls_once": True},
    )

    result = DagScheduler().execute(team_plan, runner)

    assert result.status is ScheduleStatus.COMPLETED
    reviewer_requests = [
        request for request in executor.requests if request.agent_id == "reviewer"
    ]
    assert len(reviewer_requests) == 2
    assert [request.timeout_seconds for request in reviewer_requests] == [47, 47]
    assert "TARGETED_SEMANTIC_CORRECTION_V2" in reviewer_requests[1].prompt
    normalized_prompt = " ".join(reviewer_requests[1].prompt.split())
    assert "provide only a bounded result fragment" in normalized_prompt
    assert "deterministic command stdout/stderr from this immutable" in (
        normalized_prompt
    )
    assert "controller binds every protocol-eligible result" in normalized_prompt
    reviewer_records = [
        runner.artifact_store.load(reference)
        for reference in runner.execution_records
        if "/verify/reviewer-" in reference.path
    ]
    assert len(reviewer_records) == 2
    assert isinstance(reviewer_records[0], AgentExecutionRecord)
    assert reviewer_records[0].tool_evidence_status is AgentToolEvidenceStatus.CAPTURED
    assert reviewer_records[0].response_contract == "semantic_body_v4"
    assert reviewer_records[0].response_transport == "typed_submission_v1"
    assert [call.tool_name for call in reviewer_records[0].tool_calls] == [
        "sat_submit_artifact"
    ]
    assert "does not match any eligible review-chain tool result" in (
        reviewer_records[0].error or ""
    )
    assert isinstance(reviewer_records[1], AgentExecutionRecord)
    assert reviewer_records[1].semantic_correction_request is not None
    assert reviewer_records[1].semantic_correction_outcome == "accepted"
    assert [call.tool_name for call in reviewer_records[1].tool_calls] == [
        "read",
        "sat_submit_artifact",
    ]
    assert reviewer_records[1].response_artifact == runner.outputs["reviewer"]


def test_dynamic_reviewer_repair_reuses_prior_attempt_tool_evidence(
    tmp_path: Path,
) -> None:
    runner, team_plan, executor, _, _ = runtime(
        tmp_path,
        executor_options={"invalid_review_response_once": True},
    )

    result = DagScheduler().execute(team_plan, runner)

    assert result.status is ScheduleStatus.COMPLETED
    reviewer_requests = [
        request for request in executor.requests if request.agent_id == "reviewer"
    ]
    assert len(reviewer_requests) == 2
    normalized_prompt = " ".join(reviewer_requests[1].prompt.split())
    assert "earlier integrity-checked attempt" in normalized_prompt
    assert "does not need to rerun" in normalized_prompt
    reviewer_records = [
        runner.artifact_store.load(reference)
        for reference in runner.execution_records
        if "/verify/reviewer-" in reference.path
    ]
    assert len(reviewer_records) == 2
    assert isinstance(reviewer_records[0], AgentExecutionRecord)
    assert [call.tool_name for call in reviewer_records[0].tool_calls] == [
        "read",
        "sat_submit_artifact",
    ]
    assert "summary: String should have at least 1 character" in (
        reviewer_records[0].error or ""
    )
    assert reviewer_records[0].response_validation is not None
    assert reviewer_records[0].response_validation.correction_paths == ("/summary",)
    assert isinstance(reviewer_records[1], AgentExecutionRecord)
    assert [call.tool_name for call in reviewer_records[1].tool_calls] == [
        "sat_submit_artifact"
    ]
    assert reviewer_records[1].response_artifact == runner.outputs["reviewer"]
    review = runner.artifact_store.load(runner.outputs["reviewer"])
    assert isinstance(review, ReviewReport)
    reference = review.criterion_assessments[0].tool_evidence[0]
    assert reference.execution_attempt == 1
    assert reference.tool_call_id == "tool-001"


def test_dynamic_reviewer_session_integrity_failure_is_not_semantically_repaired(
    tmp_path: Path,
) -> None:
    runner, team_plan, executor, _, _ = runtime(
        tmp_path,
        executor_options={"invalid_review_evidence": True},
    )

    result = DagScheduler().execute(team_plan, runner)

    assert result.status is ScheduleStatus.FAILED
    reviewer_requests = [
        request for request in executor.requests if request.agent_id == "reviewer"
    ]
    assert len(reviewer_requests) == 1
    reviewer_record = next(
        runner.artifact_store.load(reference)
        for reference in runner.execution_records
        if "/verify/reviewer-" in reference.path
    )
    assert isinstance(reviewer_record, AgentExecutionRecord)
    assert reviewer_record.tool_evidence_status is AgentToolEvidenceStatus.INVALID
    assert reviewer_record.tool_evidence_error == "session transcript identity mismatch"
    assert reviewer_record.submission_evidence is not None
    assert (
        reviewer_record.submission_evidence.status is AgentSubmissionStatus.UNAUTHORIZED
    )
    assert (
        reviewer_record.submission_evidence.diagnostic_code
        == "tool_evidence_unavailable"
    )
    assert "tool_evidence_unavailable" in (reviewer_record.error or "")
    assert "reviewer" not in runner.outputs


def test_dynamic_writer_argv_prose_does_not_spend_a_semantic_repair(
    tmp_path: Path,
) -> None:
    runner, team_plan, executor, _, _ = runtime(
        tmp_path,
        executor_options={"writer_presentation_arrays": True},
    )

    result = DagScheduler().execute(team_plan, runner)

    assert result.status is ScheduleStatus.COMPLETED
    writer_requests = [
        request for request in executor.requests if request.agent_id == "builder"
    ]
    assert len(writer_requests) == 1
    writer_records = [
        runner.artifact_store.load(reference)
        for reference in runner.execution_records
        if "/implement/builder-" in reference.path
    ]
    assert len(writer_records) == 1
    assert isinstance(writer_records[0], AgentExecutionRecord)
    assert writer_records[0].error is None
    assert writer_records[0].response_artifact == runner.outputs["builder"]


def test_dynamic_reviewer_unapproved_boundaries_do_not_spend_a_correction(
    tmp_path: Path,
) -> None:
    runner, team_plan, executor, _, _ = runtime(
        tmp_path,
        executor_options={"unapproved_review_boundaries": True},
    )

    result = DagScheduler().execute(team_plan, runner)

    assert result.status is ScheduleStatus.COMPLETED
    reviewer_requests = [
        request for request in executor.requests if request.agent_id == "reviewer"
    ]
    assert len(reviewer_requests) == 1
    reviewer_record = next(
        runner.artifact_store.load(reference)
        for reference in runner.execution_records
        if "/verify/reviewer-" in reference.path
    )
    assert isinstance(reviewer_record, AgentExecutionRecord)
    assert reviewer_record.error is None
    assert reviewer_record.response_normalizations == (
        "removed 2 unapproved boundary_checks from criterion AC_REVIEW "
        "(approved: none)",
    )
    assert reviewer_record.semantic_correction_request is None
    review = runner.artifact_store.load(runner.outputs["reviewer"])
    assert isinstance(review, ReviewReport)
    assert review.criterion_assessments[0].boundary_checks == ()


def test_dynamic_runner_switches_only_after_approved_provider_failure(
    tmp_path: Path,
) -> None:
    runner, team_plan, executor, _, _ = runtime(
        tmp_path,
        model_switching=True,
        executor_options={"provider_fail_once_for": "builder"},
    )
    events: list[ProgressEvent] = []
    runner.activity_handler = events.append

    result = DagScheduler().execute(team_plan, runner)

    assert result.status is ScheduleStatus.COMPLETED
    builder_requests = [
        request for request in executor.requests if request.agent_id == "builder"
    ]
    assert [request.model for request in builder_requests] == [
        MODEL,
        "test/fallback-model",
    ]
    builder_records = [
        runner.artifact_store.load(reference)
        for reference in runner.execution_records
        if "/implement/builder-" in reference.path
    ]
    assert len(builder_records) == 2
    assert isinstance(builder_records[0], AgentExecutionRecord)
    assert builder_records[0].error == "scripted provider failure"
    assert isinstance(builder_records[1], AgentExecutionRecord)
    assert builder_records[1].model == "test/fallback-model"
    switch = next(
        event
        for event in events
        if event.kind is ProgressEventKind.MODEL_ROUTE_SWITCHED
    )
    assert switch.agent_id == "builder"
    assert switch.model == "test/fallback-model"
    assert "may be billable" in switch.message
    assert tuple(reference.id for reference in switch.references) == (
        "default",
        "fallback",
    )


def test_dynamic_runner_can_use_approved_fallback_after_provider_stall(
    tmp_path: Path,
) -> None:
    runner, team_plan, executor, _, _ = runtime(
        tmp_path,
        model_switching=True,
        executor_options={"provider_stall_once_for": "builder"},
    )
    events: list[ProgressEvent] = []
    runner.activity_handler = events.append

    result = DagScheduler().execute(team_plan, runner)

    assert result.status is ScheduleStatus.COMPLETED
    builder_requests = [
        request for request in executor.requests if request.agent_id == "builder"
    ]
    assert [request.model for request in builder_requests] == [
        MODEL,
        "test/fallback-model",
    ]
    first = next(
        runner.artifact_store.load(reference)
        for reference in runner.execution_records
        if "/implement/builder-attempt-01" in reference.path
    )
    assert isinstance(first, AgentExecutionRecord)
    assert first.execution_status is AgentExecutionStatus.PROVIDER_STALLED
    assert first.provider_liveness is not None
    assert first.provider_liveness.stalled
    suspected = next(
        event
        for event in events
        if event.kind is ProgressEventKind.AGENT_STALL_SUSPECTED
    )
    stalled = next(
        event
        for event in events
        if event.kind is ProgressEventKind.AGENT_PROVIDER_STALLED
    )
    assert "90.0s" in suspected.message
    assert "another 30s" in suspected.message
    assert "test provider contract" in suspected.message
    assert "silent for 120s" in stalled.message
    assert "separate stopping transition follows" in stalled.message
    assert stalled.checkpoint is not None
    assert stalled.checkpoint.invocation_phase is InvocationPhase.PROVIDER_WAIT
    event_kinds = [event.kind for event in events]
    ordered = [
        ProgressEventKind.AGENT_PROVIDER_STALLED,
        ProgressEventKind.AGENT_STOPPING,
        ProgressEventKind.AGENT_COLLECTING_EVIDENCE,
        ProgressEventKind.AGENT_STOPPED,
        ProgressEventKind.AGENT_INVOCATION_COMPLETED,
        ProgressEventKind.MODEL_ROUTE_SWITCHED,
    ]
    positions = [event_kinds.index(kind) for kind in ordered]
    assert positions == sorted(positions)


def test_dynamic_runner_refuses_unapproved_provider_fallback(
    tmp_path: Path,
) -> None:
    runner, team_plan, executor, _, _ = runtime(
        tmp_path,
        executor_options={"provider_fail_once_for": "builder"},
    )

    result = DagScheduler().execute(team_plan, runner)

    assert result.status is ScheduleStatus.FAILED
    assert [request.model for request in executor.requests] == [MODEL]
    assert runner.termination_reasons["builder"] is (
        TerminationReason.DEPENDENCY_UNAVAILABLE
    )


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
