"""End-to-end tests for approved adaptive lifecycle convergence."""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

import software_agent_team.dynamic_workflow as dynamic_workflow_module
from software_agent_team.artifact_store import ArtifactStore
from software_agent_team.artifacts import (
    AcceptanceCriterion,
    AgentExecutionRecord,
    AgentToolCallEvidence,
    AgentToolEvidenceStatus,
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
    WorkResult,
)
from software_agent_team.budgets import (
    AgentBudget,
    AgentBudgetLedger,
    BudgetAuthority,
    ModelPricing,
)
from software_agent_team.controls import (
    ControlApplicationBoundary,
    ControlCommandStatus,
    ControlCommandStore,
    ControlCommandType,
    ControlTarget,
    ControlTargetKind,
)
from software_agent_team.dynamic_workflow import (
    DynamicWorkflowCoordinator,
    DynamicWorkflowError,
    DynamicWorkflowOutcome,
)
from software_agent_team.execution import (
    AgentExecutionActivity,
    AgentExecutionActivityHandler,
    AgentExecutionActivityKind,
    AgentExecutionRequest,
    AgentExecutionResult,
    AgentExecutionStatus,
    AgentExecutionTelemetry,
    AgentExecutor,
    AgentTokenUsage,
)
from software_agent_team.git_workspace import GitSnapshot
from software_agent_team.integrity import canonical_model_sha256
from software_agent_team.invocation_lifecycle import (
    InitializationCheckpoint,
    InvocationPhase,
    InvocationStopReason,
)
from software_agent_team.model_costs import CachePricing, CacheTokenUsage
from software_agent_team.model_metadata import ModelMetadataSource
from software_agent_team.planning import (
    AdaptiveImplementationPlan,
    AgentTimeoutResolution,
    AgentWorkload,
    ApprovedPlanningResult,
    PlanningApproval,
    ProposedCriterion,
    ProposedTask,
)
from software_agent_team.progress import (
    ProgressDraftHandler,
    ProgressEvent,
    ProgressEventKind,
)
from software_agent_team.responses import (
    ReviewCriterionAssessmentResponse,
    ReviewReportResponse,
    ReviewToolEvidenceClaim,
    WorkResultResponse,
)
from software_agent_team.responses import (
    TestReportResponse as SemanticTestReportResponse,
)
from software_agent_team.run_control import RunControlError, RunPhase, TerminationReason
from software_agent_team.runtime_controls import RuntimeControlDecision
from software_agent_team.scheduling import ScheduleStatus
from software_agent_team.schema_compatibility import supported_schemas
from software_agent_team.submissions import (
    AgentSemanticSubmission,
    AgentSubmissionEvidence,
    AgentSubmissionPurpose,
    AgentSubmissionStatus,
    canonical_json_sha256,
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
from software_agent_team.versioning import (
    IdentityStatus,
    InstallMode,
    SoftwareVersionReport,
)

FIXED_TIME = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
MODEL = "test/provider-model"


def software_version() -> SoftwareVersionReport:
    """Return exact deterministic SAT provenance for dynamic run evidence."""

    return SoftwareVersionReport(
        release_version="0.1.0",
        display_version="0.1.0+gaaaaaaaaaaaa",
        source_revision="a" * 40,
        dirty=False,
        install_mode=InstallMode.SOURCE,
        channel=None,
        source_ref=None,
        repository_url=None,
        application_path="/opt/software-agent-team",
        artifact_digest=None,
        installed_at=None,
        identity_status=IdentityStatus.VERIFIED,
        provenance_source="git",
        schema_support=supported_schemas(),
    )


def review_tool_claim() -> ReviewToolEvidenceClaim:
    """Select the adaptive fixture's attributable read observation."""

    return ReviewToolEvidenceClaim(observable="adaptive-review-observation")


def review_tool_call() -> AgentToolCallEvidence:
    """Return the adaptive fixture's sanitized read result."""

    return AgentToolCallEvidence(
        id="tool-001",
        tool_name="read",
        external_call_sha256="a" * 64,
        arguments_sha256="b" * 64,
        outcome="succeeded",
        is_error=False,
        output_sha256="c" * 64,
        output_bytes=27,
        output_excerpt="adaptive-review-observation",
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
    run_budget: AgentBudget | None = None,
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
        budget=(
            run_budget
            or AgentBudget(
                max_calls=8,
                max_input_tokens=10_000,
                max_output_tokens=5_000,
                max_agent_duration_seconds=120,
                max_estimated_cost_usd="5",
            )
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
        timeout_second_review: bool = False,
        invalid_review_selectors: bool = False,
    ) -> None:
        self.workspace = workspace
        self.revise_first = revise_first
        self.always_revise = always_revise
        self.omit_builder_model = omit_builder_model
        self.timeout_second_review = timeout_second_review
        self.invalid_review_selectors = invalid_review_selectors
        self.requests: list[AgentExecutionRequest] = []
        self.counts: dict[str, int] = {}

    def execute(
        self,
        request: AgentExecutionRequest,
        *,
        activity_handler: AgentExecutionActivityHandler | None = None,
    ) -> AgentExecutionResult:
        self._emit_start(request, activity_handler)
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
            if self.timeout_second_review and count == 2:
                self._emit_stop(
                    request,
                    activity_handler,
                    InvocationStopReason.EVALUATION_TIMEOUT,
                )
                return AgentExecutionResult(
                    status=AgentExecutionStatus.TIMED_OUT,
                    error="review exceeded its approved invocation timeout",
                    telemetry=AgentExecutionTelemetry(
                        role=None,
                        agent_id=request.agent_id,
                        capability=request.capability,
                        session_key=request.session_key,
                        command=("fake-agent", request.agent_id),
                        started_at=FIXED_TIME,
                        finished_at=FIXED_TIME,
                        duration_ms=request.timeout_seconds * 1000,
                        timed_out=True,
                        exit_code=None,
                        stdout="",
                        stderr="review timed out",
                    ),
                )
            if self.always_revise or (self.revise_first and count == 1):
                body = ReviewReportResponse(
                    verdict="revise",
                    criterion_assessments=(
                        ReviewCriterionAssessmentResponse(
                            criterion_id="AC_REVIEW",
                            status="blocked",
                            adversarial_check=(
                                "Compared the documented result type with the "
                                "implemented return value."
                            ),
                            evidence=(
                                "README.md omits the string result type exposed by "
                                "greeting.py."
                            ),
                            tool_evidence=(review_tool_claim(),),
                        ),
                    ),
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
                    criterion_assessments=(
                        ReviewCriterionAssessmentResponse(
                            criterion_id="AC_REVIEW",
                            status="satisfied",
                            adversarial_check=(
                                "Compared the documented result type with the "
                                "implemented return value."
                            ),
                            evidence=(
                                "README.md now documents the string returned by "
                                "greeting.py."
                            ),
                            tool_evidence=(review_tool_claim(),),
                        ),
                    ),
                    summary="The final commit satisfies the review scope.",
                ).model_dump_json()
        else:  # pragma: no cover - the fixture owns every Agent
            raise AssertionError(f"unexpected Agent: {request.agent_id}")
        is_review = request.capability is AgentCapability.REVIEW
        contract = request.submission_contract
        assert contract is not None
        submission_payload = json.loads(body)
        if is_review and self.invalid_review_selectors:
            submission_payload["criterion_assessments"][0]["tool_evidence"] = [
                {"observable": "invented-first-observation"},
                {"observable": "invented-second-observation"},
            ]
            body = json.dumps(submission_payload)
        review_calls = (review_tool_call(),) if is_review else ()
        external_id = f"workflow-submission-{len(review_calls) + 1:03d}"
        output = b"workflow-semantic-submission"
        submission_call = AgentToolCallEvidence(
            id=f"tool-{len(review_calls) + 1:03d}",
            tool_name=contract.tool_name,
            external_call_sha256=hashlib.sha256(external_id.encode()).hexdigest(),
            arguments_sha256=canonical_json_sha256({"artifact": submission_payload}),
            outcome="succeeded",
            is_error=False,
            output_sha256=hashlib.sha256(output).hexdigest(),
            output_bytes=len(output),
            output_excerpt=output.decode(),
        )
        tool_calls = (*review_calls, submission_call)
        binding_sha256 = hashlib.sha256(
            f"{request.session_key}\x00{contract.schema_sha256}".encode()
        ).hexdigest()
        submission_evidence = AgentSubmissionEvidence(
            protocol=contract.protocol,
            purpose=contract.purpose,
            status=AgentSubmissionStatus.ACCEPTED,
            schema_sha256=contract.schema_sha256,
            binding_sha256=binding_sha256,
            tool_call_id=submission_call.id,
            payload_sha256=canonical_json_sha256({"artifact": submission_payload}),
            semantic_payload_sha256=canonical_json_sha256(submission_payload),
        )
        self._emit_stop(request, activity_handler, InvocationStopReason.COMPLETED)
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
                    cache_read_tokens=0,
                    cache_write_tokens=0,
                    total_tokens=15,
                ),
                tool_evidence_status=AgentToolEvidenceStatus.CAPTURED,
                session_transcript_sha256="d" * 64,
                session_record_count=max(3, 2 + len(tool_calls)),
                tool_calls=tool_calls,
            ),
            semantic_submission=AgentSemanticSubmission(
                payload=submission_payload,
                evidence=submission_evidence,
            ),
            submission_evidence=submission_evidence,
        )

    @staticmethod
    def _emit_start(
        request: AgentExecutionRequest,
        activity_handler: AgentExecutionActivityHandler | None,
    ) -> None:
        if activity_handler is None:
            return
        for kind, phase, checkpoint in (
            (
                AgentExecutionActivityKind.INVOCATION_LAUNCHED,
                InvocationPhase.LAUNCHED,
                None,
            ),
            (
                AgentExecutionActivityKind.INVOCATION_INITIALIZING,
                InvocationPhase.INITIALIZING,
                InitializationCheckpoint.PROCESS_LAUNCHED,
            ),
            (
                AgentExecutionActivityKind.INVOCATION_PROVIDER_WAIT,
                InvocationPhase.PROVIDER_WAIT,
                InitializationCheckpoint.CURRENT_TURN,
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
                )
            )

    @staticmethod
    def _emit_stop(
        request: AgentExecutionRequest,
        activity_handler: AgentExecutionActivityHandler | None,
        reason: InvocationStopReason,
    ) -> None:
        if activity_handler is None:
            return
        for kind, phase in (
            (
                AgentExecutionActivityKind.INVOCATION_STOPPING,
                InvocationPhase.STOPPING,
            ),
            (
                AgentExecutionActivityKind.INVOCATION_COLLECTING_EVIDENCE,
                InvocationPhase.COLLECTING_EVIDENCE,
            ),
            (
                AgentExecutionActivityKind.INVOCATION_STOPPED,
                InvocationPhase.STOPPED,
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
                    shutdown_grace_seconds=(
                        1
                        if kind is AgentExecutionActivityKind.INVOCATION_STOPPING
                        else None
                    ),
                )
            )


def coordinator(
    tmp_path: Path,
    approved: ApprovedPlanningResult,
    executor: AgentExecutor,
    gates: RecordingQualityGateFactory,
    *,
    control_store_handler=None,
    budget_ledger: AgentBudgetLedger | None = None,
    cache_read_rate: str = "0",
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
        pricing_by_model={
            MODEL: (
                ModelPricing(
                    model=MODEL,
                    input_cost_per_million_usd="1",
                    output_cost_per_million_usd="2",
                    pricing_source=ModelMetadataSource.USER_SUPPLIED,
                    pricing_observed_at=FIXED_TIME,
                    cache_pricing=CachePricing(
                        read_cost_per_million_usd=cache_read_rate,
                        write_cost_per_million_usd=0,
                        source=ModelMetadataSource.USER_SUPPLIED,
                        observed_at=FIXED_TIME,
                    ),
                )
                if approved.team_plan.budget.authority is BudgetAuthority.USER_TASK
                else ModelPricing(model=MODEL)
            )
        },
        software_version=software_version(),
        budget_ledger=budget_ledger,
        manual_review_criteria=manual,
        control_store_handler=control_store_handler,
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


def replace_semantic_payload(
    result: AgentExecutionResult,
    request: AgentExecutionRequest,
    payload: dict[str, object],
) -> AgentExecutionResult:
    """Bind a scripted provider payload to its actual invocation contract."""

    assert result.semantic_submission is not None
    assert result.submission_evidence is not None
    assert request.submission_contract is not None
    raw = json.dumps(payload)
    envelope_hash = canonical_json_sha256({"artifact": payload})
    evidence = result.submission_evidence.model_copy(
        update={
            "payload_sha256": envelope_hash,
            "semantic_payload_sha256": canonical_json_sha256(payload),
            "binding_sha256": hashlib.sha256(
                f"{request.session_key}\x00{request.submission_contract.schema_sha256}".encode()
            ).hexdigest(),
        }
    )
    calls = tuple(
        call.model_copy(update={"arguments_sha256": envelope_hash})
        if call.id == evidence.tool_call_id
        else call
        for call in result.telemetry.tool_calls
    )
    return result.model_copy(
        update={
            "response_text": raw,
            "telemetry": result.telemetry.model_copy(
                update={
                    "agent_id": request.agent_id,
                    "session_key": request.session_key,
                    "stdout": raw,
                    "tool_calls": calls,
                }
            ),
            "semantic_submission": AgentSemanticSubmission(
                payload=payload, evidence=evidence
            ),
            "submission_evidence": evidence,
        }
    )


class TwoWriterExecutor(AdaptiveExecutor):
    """Script two writers while retaining real Git, runner, and scheduler state."""

    def __init__(self, workspace: Path) -> None:
        super().__init__(workspace)
        self.agent_order: list[str] = []

    def execute(self, request, *, activity_handler=None):
        self.agent_order.append(request.agent_id)
        if request.agent_id != "finisher":
            return super().execute(request, activity_handler=activity_handler)
        result = super().execute(
            request.model_copy(update={"agent_id": "builder"}),
            activity_handler=activity_handler,
        )
        assert result.semantic_submission is not None
        payload = dict(result.semantic_submission.payload)
        payload["completed_tasks"] = ["TASK_FINISH"]
        return replace_semantic_payload(result, request, payload)


def approved_two_writer_proposal(
    tmp_path: Path, *, reverse: bool
) -> ApprovedPlanningResult:
    """Admit and approve the same DAG through the production Planning flow."""

    import test_planning as planning_fixture

    original = approved_inputs(run_id="planning-writer-order")
    body = planning_fixture.proposal_body()
    # Reuse the complete proposal contract with the greeting task's exact IDs.
    encoded = body.model_dump_json()
    for previous, current in (
        ("cli_developer", "builder"),
        ("acceptance_tester", "tester"),
        ("quality_reviewer", "reviewer"),
        ("AC_SCAN", "AC_CODE"),
        ("AC_REPORT", "AC_REVIEW"),
    ):
        encoded = encoded.replace(f'"{previous}"', f'"{current}"')
    body = planning_fixture.PlanningProposalBody.model_validate_json(encoded)
    builder, tester, reviewer = body.agents
    finisher = builder.model_copy(
        update={
            "id": "finisher",
            "label": "Finisher",
            "responsibility": "Complete the usage documentation.",
            "rationale": "A second serial writer integrates public documentation.",
            "dependencies": ("builder",),
        }
    )
    quality = tuple(
        agent.model_copy(update={"dependencies": ("finisher",)})
        for agent in (tester, reviewer)
    )
    definition = body.product_definition
    assert definition is not None
    body = body.model_copy(
        update={
            "title": original.task_brief.title,
            "requirements": (
                *original.task_brief.requirements,
                "Document the greeting return type.",
            ),
            "non_goals": ("Remote greeting generation is out of scope.",),
            "objective": original.implementation_plan.objective,
            "approach": original.implementation_plan.approach,
            "product_definition": definition.model_copy(
                update={
                    "primary_workflow": definition.primary_workflow.model_copy(
                        update={
                            "statement": "generates greetings",
                            "source": "generates greetings",
                        }
                    )
                }
            ),
            "acceptance_criteria": tuple(
                ProposedCriterion(
                    **criterion.model_dump(),
                    requirement_ids=(body.requirement_ids[index],),
                    verification_agent_ids=("tester",) if index == 0 else ("reviewer",),
                )
                for index, criterion in enumerate(
                    original.task_brief.acceptance_criteria
                )
            ),
            "tasks": (
                *original.implementation_plan.tasks,
                ProposedTask(
                    id="TASK_FINISH",
                    owner_agent_id="finisher",
                    description="Document the greeting return type.",
                    acceptance_criteria=("AC_REVIEW",),
                    expected_paths=("README.md",),
                    dependencies=("TASK_BUILD",),
                ),
            ),
            "agents": (finisher, builder, *quality)
            if reverse
            else (builder, finisher, *quality),
            "iteration_limit": 1,
            "revision_enabled": False,
        }
    )
    request = planning_fixture.request(
        source_request=(
            "Build a usable local product for developers that generates greetings."
        )
    ).model_copy(update={"model": MODEL})
    executor = planning_fixture.ScriptedAgentExecutor(
        [
            planning_fixture.ScriptedAgentResponse(
                text="Proposed a greeting implementation with two serial writers.",
                submission_payload=planning_fixture.proposal_response(body).model_dump(
                    mode="json"
                ),
            )
        ]
    )
    planner = planning_fixture.AdaptivePlanningCoordinator(
        executor=executor,
        store=planning_fixture.PlanningStore(tmp_path / "planning"),
        policy=planning_fixture.policy(),
        clock=lambda: FIXED_TIME,
    )
    proposal = planner.start(
        request, answer_question=lambda _: pytest.fail("unexpected clarification")
    )
    assert proposal is not None
    assert len(executor.requests) == 1
    return planner.approve(request, proposal)


@pytest.mark.parametrize("reverse", [False, True])
def test_writer_declaration_order_preserves_planning_to_workflow_acceptance(
    tmp_path: Path, reverse: bool
) -> None:
    approved = approved_two_writer_proposal(tmp_path, reverse=reverse)
    expected_writers = ("finisher", "builder") if reverse else ("builder", "finisher")
    assert (
        tuple(agent.id for agent in approved.team_plan.agents[:2]) == expected_writers
    )
    assert approved.team_plan.execution_waves() == (
        ("builder",),
        ("finisher",),
        ("tester", "reviewer"),
    )
    source = initialize_source(tmp_path)
    source_head = git(source, "rev-parse", "HEAD").stdout.strip()
    workspace = tmp_path / "workspaces" / approved.task_brief.run_id
    executor = TwoWriterExecutor(workspace)
    gates = RecordingQualityGateFactory()

    outcome = coordinator(tmp_path, approved, executor, gates).execute(
        approved, source_repository=source
    )
    store, report = load_report(tmp_path, outcome, approved)

    assert outcome.record.phase is RunPhase.COMPLETED, report.summary
    assert executor.agent_order[:2] == ["builder", "finisher"]
    assert gates.calls == [1]
    iteration = store.load(report.iterations[0])
    assert isinstance(iteration, IterationRecord)
    works = tuple(store.load(reference) for reference in iteration.work_results)
    assert all(isinstance(work, WorkResult) for work in works)
    assert tuple(work.producer for work in works) == ("builder", "finisher")
    assert works[0].input_commit == source_head
    assert works[0].output_commit == works[1].input_commit
    assert works[1].output_commit == report.final_commit
    assert report.final_commit == git(workspace, "rev-parse", "HEAD").stdout.strip()
    git(workspace, "merge-base", "--is-ancestor", source_head, report.final_commit)
    assert git(source, "rev-parse", "HEAD").stdout.strip() == source_head


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
    assert report.software_version == software_version()
    markdown = (
        tmp_path / "runs" / approved.task_brief.run_id / outcome.human_report_path
    ).read_text(encoding="utf-8")
    assert "SAT version: `0.1.0+gaaaaaaaaaaaa`" in markdown
    assert f"SAT source revision: `{'a' * 40}`" in markdown
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
    event_kinds = [event.kind for event in outcome.events]
    assert event_kinds.count(ProgressEventKind.AGENT_QUEUED) == len(
        approved.team_plan.agents
    )
    assert event_kinds.count(ProgressEventKind.AGENT_READY) == len(
        approved.team_plan.agents
    )
    assert event_kinds.count(ProgressEventKind.AGENT_WAITING_PROVIDER) == len(
        approved.team_plan.agents
    )
    assert event_kinds.count(ProgressEventKind.AGENT_INVOCATION_COMPLETED) == len(
        approved.team_plan.agents
    )
    terminal_agent_events = [
        event
        for event in outcome.events
        if event.kind is ProgressEventKind.AGENT_COMPLETED
    ]
    assert len(terminal_agent_events) == len(approved.team_plan.agents)
    assert all(event.duration_ms is not None for event in terminal_agent_events)
    assert all(
        event.source.value == "agent_safe_summary" for event in terminal_agent_events
    )
    invocation_event = next(
        event
        for event in outcome.events
        if event.kind is ProgressEventKind.AGENT_INVOCATION_COMPLETED
    )
    assert invocation_event.budget_usage is not None
    assert invocation_event.model == MODEL


def test_dynamic_workflow_rejects_scope_different_from_approved_strategy(
    tmp_path: Path,
) -> None:
    base = approved_inputs(run_id="adaptive-scope-binding")
    implementation = base.implementation_plan.model_copy(
        update={
            "acceptance_criteria": (
                ProposedCriterion(
                    id="AC_REVIEW",
                    description="The result is clearly documented.",
                    verification="Review the public usage documentation.",
                    requirement_ids=("REQ_REVIEW",),
                    verification_agent_ids=("reviewer",),
                ),
            )
        }
    )
    team = base.team_plan.model_copy(
        update={"implementation_plan_sha256": canonical_model_sha256(implementation)}
    )
    approval = base.approval.model_copy(
        update={
            "implementation_plan_sha256": canonical_model_sha256(implementation),
            "team_plan_sha256": canonical_model_sha256(team),
        }
    )
    approved = ApprovedPlanningResult(
        task_brief=base.task_brief,
        implementation_plan=implementation,
        team_plan=team,
        approval=approval,
    )
    workflow = coordinator(
        tmp_path,
        approved,
        AdaptiveExecutor(tmp_path / "workspaces" / approved.task_brief.run_id),
        RecordingQualityGateFactory(),
    )
    workflow.review_scope_by_agent = {"reviewer": ("AC_CODE",)}

    with pytest.raises(
        DynamicWorkflowError,
        match="runtime Review scopes differ from the approved acceptance strategy",
    ):
        workflow.execute(
            approved,
            source_repository=tmp_path / "unused-source",
        )


def test_dynamic_workflow_prices_cache_in_records_progress_and_report(
    tmp_path: Path,
) -> None:
    approved = approved_inputs(
        run_id="adaptive-cache-cost",
        run_budget=AgentBudget(
            authority=BudgetAuthority.USER_TASK, max_estimated_cost_usd="5"
        ),
    )
    source = initialize_source(tmp_path)
    seed = git(source, "rev-parse", "HEAD").stdout.strip()
    workspace = tmp_path / "workspaces" / approved.task_brief.run_id

    class CachedExecutor(AdaptiveExecutor):
        def execute(self, request, *, activity_handler=None):
            result = super().execute(request, activity_handler=activity_handler)
            assert result.telemetry.usage is not None
            usage = result.telemetry.usage.model_copy(
                update={"cache_read_tokens": 1_000_000}
            )
            return result.model_copy(
                update={
                    "telemetry": result.telemetry.model_copy(update={"usage": usage})
                }
            )

    ledger = AgentBudgetLedger(approved.team_plan.budget)
    executor = CachedExecutor(workspace)
    outcome = coordinator(
        tmp_path,
        approved,
        executor,
        RecordingQualityGateFactory(),
        budget_ledger=ledger,
        cache_read_rate="0.014",
    ).execute(approved, source_repository=source)
    assert outcome.record.phase is RunPhase.COMPLETED
    expected_call = Decimal("0.014020")
    assert ledger.snapshot().known_estimated_cost_usd == expected_call * len(
        approved.team_plan.agents
    )
    store, final_report = load_report(tmp_path, outcome, approved)
    workspace_head = git(workspace, "rev-parse", "HEAD").stdout.strip()
    assert final_report.status is FinalStatus.COMPLETED
    assert final_report.final_commit == workspace_head
    git(workspace, "merge-base", "--is-ancestor", seed, workspace_head)
    assert git(source, "rev-parse", "HEAD").stdout.strip() == seed
    assert git(source, "status", "--short").stdout == ""
    assert all(
        store.load(reference).estimated_cost_usd == expected_call
        for reference in outcome.execution_records
    )
    assert all(
        call.cache_usage.read_tokens == 1_000_000 for call in ledger.call_records()
    )
    final_event_usage = [
        event.budget_usage for event in outcome.events if event.budget_usage is not None
    ][-1]
    assert final_event_usage == ledger.snapshot()
    invocation_event = next(
        event
        for event in outcome.events
        if event.kind is ProgressEventKind.AGENT_INVOCATION_COMPLETED
    )
    assert invocation_event.checkpoint is not None
    assert invocation_event.budget_usage is not None
    assert invocation_event.budget_usage.known_estimated_cost_usd == expected_call
    assert outcome.events[-1].phase is RunPhase.COMPLETED
    report = (
        tmp_path / "runs" / approved.task_brief.run_id / "final-report.md"
    ).read_text()
    assert "1000000 cache read / 0 cache write" in report
    assert "$0.014020" in report


@pytest.mark.parametrize("ceiling", ["5", "0.00021"])
def test_dynamic_workflow_continues_one_shared_planning_budget_ledger(
    tmp_path: Path,
    ceiling: str,
) -> None:
    approved = approved_inputs(
        run_id="adaptive-shared-budget",
        run_budget=AgentBudget(
            authority=BudgetAuthority.USER_TASK, max_estimated_cost_usd=ceiling
        ),
    )
    ledger = AgentBudgetLedger(approved.team_plan.budget)
    source = initialize_source(tmp_path)
    executor = AdaptiveExecutor(tmp_path / "workspaces" / approved.task_brief.run_id)
    workflow = coordinator(
        tmp_path,
        approved,
        executor,
        RecordingQualityGateFactory(),
        budget_ledger=ledger,
    )
    planning_call = ledger.reserve_call(
        "clarifier",
        run_id=approved.task_brief.run_id,
        stage="planning",
        route_id="default",
        pricing=workflow.pricing_by_model[MODEL],
    )
    planning_usage = ledger.complete_call(
        planning_call,
        input_tokens=100,
        output_tokens=50,
        duration_ms=20,
        cache_usage=CacheTokenUsage(read_tokens=0, write_tokens=0),
    )
    outcome = workflow.execute(approved, source_repository=source)

    assert planning_usage.known_estimated_cost_usd == Decimal("0.0002")
    exhausted = ceiling != "5"
    assert outcome.record.phase is (
        RunPhase.FAILED if exhausted else RunPhase.COMPLETED
    )
    if exhausted:
        assert len(executor.requests) == 1
        assert (
            outcome.record.termination_reason
            is TerminationReason.RESOURCE_LIMIT_REACHED
        )
    usage = ledger.snapshot()
    assert usage.calls_completed == planning_usage.calls_completed + len(
        executor.requests
    )
    assert usage.calls_started == usage.calls_completed
    assert usage.active_calls == 0
    assert usage.known_estimated_cost_usd == Decimal("0.0002") + Decimal(
        "0.00002"
    ) * len(executor.requests)
    assert ledger.call_records()[0].stage == "planning"
    assert all(
        call.run_id == approved.task_brief.run_id for call in ledger.call_records()
    )
    run_directory = tmp_path / "runs" / approved.task_brief.run_id
    persisted = json.loads((run_directory / "budget-ledger.json").read_text())
    assert persisted["usage"] == usage.model_dump(mode="json")
    assert persisted["calls"] == [
        call.model_dump(mode="json") for call in ledger.call_records()
    ]
    report = (run_directory / "final-report.md").read_text()
    assert "`planning` | `clarifier`" in report
    assert "provider-side spending or quota limit" in report
    invocation_usages = tuple(
        event.budget_usage
        for event in outcome.events
        if event.kind is ProgressEventKind.AGENT_INVOCATION_COMPLETED
    )
    assert invocation_usages
    assert invocation_usages[0] is not None
    assert invocation_usages[0].calls_completed >= 2


@pytest.mark.parametrize("ending", ["complete", "no_progress", "budget", "cancel"])
def test_upstream_continuation_reaches_one_workflow_terminal_authority(
    tmp_path: Path, ending: str
) -> None:
    from test_dynamic_runner import DynamicExecutor

    approved = approved_inputs(
        run_id="adaptive-continuation-terminal",
        run_budget=AgentBudget(
            authority=BudgetAuthority.USER_TASK,
            max_estimated_cost_usd="0.00001" if ending == "budget" else "1",
        ),
    )
    source = initialize_source(tmp_path)
    seed = git(source, "rev-parse", "HEAD").stdout.strip()
    workspace = tmp_path / "workspaces" / approved.task_brief.run_id
    controls = []

    class InterruptedToolExecutor(AdaptiveExecutor):
        def execute(self, request, *, activity_handler=None):
            writer_calls = sum(item.agent_id == "builder" for item in self.requests)
            if request.agent_id == "builder" and (
                writer_calls == 0 or ending == "no_progress"
            ):
                self._emit_start(request, activity_handler)
                self.requests.append(request)
                if writer_calls == 0:
                    (self.workspace / "greeting.py").write_text(
                        "def greet(name):\n    return f'Hello, {name}!'\n"
                    )
                if ending == "cancel":
                    controls[0].request(
                        command=ControlCommandType.CANCEL,
                        target=ControlTarget(kind=ControlTargetKind.RUN),
                        application_boundary=ControlApplicationBoundary.IMMEDIATE,
                        command_id="ctl-between-continuation",
                    )
                self._emit_stop(
                    request, activity_handler, InvocationStopReason.UPSTREAM_INCOMPLETE
                )
                return DynamicExecutor._upstream_incomplete_result(request)
            return super().execute(request, activity_handler=activity_handler)

    executor = InterruptedToolExecutor(workspace)
    gates = RecordingQualityGateFactory()
    ledger = AgentBudgetLedger(approved.team_plan.budget)
    outcome = coordinator(
        tmp_path,
        approved,
        executor,
        gates,
        budget_ledger=ledger,
        control_store_handler=lambda store, _plan: controls.append(store),
    ).execute(approved, source_repository=source)
    store, report = load_report(tmp_path, outcome, approved)
    records = [store.load(reference) for reference in outcome.execution_records]
    assert records[0].execution_status is AgentExecutionStatus.UPSTREAM_INCOMPLETE
    assert records[0].response_artifact is None
    writer_requests = [item for item in executor.requests if item.agent_id == "builder"]
    assert len(writer_requests) == (2 if ending in {"complete", "no_progress"} else 1)
    if len(writer_requests) == 2:
        assert writer_requests[0].session_key == writer_requests[1].session_key
        assert writer_requests[0].model == writer_requests[1].model
        assert "CONTROLLED_UPSTREAM_CONTINUATION_V1" in writer_requests[1].prompt
    if ending == "complete":
        assert outcome.record.phase is RunPhase.COMPLETED
        assert report.status is FinalStatus.COMPLETED
        assert report.final_commit == git(workspace, "rev-parse", "HEAD").stdout.strip()
        git(workspace, "merge-base", "--is-ancestor", seed, report.final_commit)
        assert gates.calls == [1]
        assert any(item.agent_id == "reviewer" for item in executor.requests)
    else:
        expected = {
            "no_progress": TerminationReason.DEPENDENCY_UNAVAILABLE,
            "budget": TerminationReason.RESOURCE_LIMIT_REACHED,
            "cancel": TerminationReason.USER_CANCELLED,
        }[ending]
        assert outcome.record.termination_reason is expected
        assert report.status is (
            FinalStatus.CANCELLED if ending == "cancel" else FinalStatus.FAILED
        )
        assert gates.calls == []
        assert all(item.agent_id == "builder" for item in executor.requests)
        assert all(event.phase is not RunPhase.DELIVERING for event in outcome.events)
        assert git(workspace, "rev-parse", "HEAD").stdout.strip() == seed
        assert (workspace / "greeting.py").is_file()
    usage = ledger.snapshot()
    assert usage.calls_started == usage.calls_completed == len(executor.requests)
    assert usage.active_calls == 0
    assert usage.known_estimated_cost_usd == Decimal("0.00002") * len(executor.requests)
    persisted = json.loads(
        (
            tmp_path / "runs" / approved.task_brief.run_id / "budget-ledger.json"
        ).read_text()
    )
    assert persisted["usage"] == usage.model_dump(mode="json")
    assert git(source, "rev-parse", "HEAD").stdout.strip() == seed
    assert git(source, "status", "--short").stdout == ""


@pytest.mark.parametrize("valid_selection", [True, False])
def test_captured_multi_selector_correction_controls_workflow_delivery(
    tmp_path: Path, valid_selection: bool
) -> None:
    from test_submission_bridge import capture_controller_correction

    approved = approved_inputs(
        run_id="adaptive-selector-delivery",
        run_budget=AgentBudget(
            authority=BudgetAuthority.USER_TASK, max_estimated_cost_usd="1"
        ),
    )
    source = initialize_source(tmp_path)
    seed = git(source, "rev-parse", "HEAD").stdout.strip()
    workspace = tmp_path / "workspaces" / approved.task_brief.run_id
    captures = []

    class SelectorExecutor(AdaptiveExecutor):
        def execute(self, request, *, activity_handler=None):
            result = super().execute(request, activity_handler=activity_handler)
            contract = request.submission_contract
            assert contract is not None
            if contract.purpose is not AgentSubmissionPurpose.SEMANTIC_CORRECTION:
                return result
            variants = contract.parameters_schema()["properties"]["replacements"][
                "items"
            ]["oneOf"]
            payload = {
                "replacements": [
                    {
                        "slot_handle": variant["properties"]["slot_handle"]["const"],
                        "replacement_value": (
                            "candidate_99"
                            if not valid_selection
                            and (len(variants) == 1 or index == 1)
                            else variant["properties"]["replacement_value"]["enum"][0]
                        ),
                    }
                    for index, variant in enumerate(variants)
                ]
            }
            captured, status, evidence = capture_controller_correction(
                tmp_path / f"workflow-capture-{len(captures)}", request, payload
            )
            captures.append(evidence)
            return result.model_copy(
                update={
                    "response_text": None,
                    "semantic_submission": captured,
                    "submission_evidence": status,
                    "telemetry": result.telemetry.model_copy(
                        update={
                            "stdout": "",
                            "tool_calls": evidence.tool_calls,
                            "session_transcript_sha256": evidence.transcript_sha256,
                            "session_record_count": evidence.record_count,
                            "session_id": "controller-bridge",
                        }
                    ),
                }
            )

    executor = SelectorExecutor(workspace, invalid_review_selectors=True)
    ledger = AgentBudgetLedger(approved.team_plan.budget)
    gates = RecordingQualityGateFactory()
    outcome = coordinator(
        tmp_path, approved, executor, gates, budget_ledger=ledger
    ).execute(approved, source_repository=source)
    store, report = load_report(tmp_path, outcome, approved)
    reviews = [
        store.load(ref)
        for ref in outcome.execution_records
        if store.load(ref).agent_id == "reviewer"
    ]
    assert len(reviews) == (2 if valid_selection else 3)
    assert all(record.response_artifact is None for record in reviews[:-1])
    assert len(reviews[1].semantic_correction_request.target_paths) == 2
    assert all(len(capture.tool_calls) == 1 for capture in captures)
    assert (
        ledger.snapshot().calls_started
        == ledger.snapshot().calls_completed
        == len(executor.requests)
    )
    assert ledger.snapshot().active_calls == 0
    assert ledger.snapshot().known_estimated_cost_usd == Decimal("0.00002") * len(
        executor.requests
    )
    assert git(source, "rev-parse", "HEAD").stdout.strip() == seed
    assert gates.calls == [1]
    if valid_selection:
        assert reviews[-1].semantic_correction_outcome == "accepted"
        assert outcome.record.phase is RunPhase.COMPLETED
        assert report.status is FinalStatus.COMPLETED
        assert report.final_commit == git(workspace, "rev-parse", "HEAD").stdout.strip()
    else:
        assert reviews[1].semantic_correction_outcome == "improved"
        assert len(reviews[2].semantic_correction_request.target_paths) == 1
        assert reviews[-1].response_artifact is None
        assert outcome.record.phase is RunPhase.FAILED
        assert report.status is FinalStatus.FAILED
        assert outcome.record.termination_reason is TerminationReason.ARTIFACT_INVALID
        assert all(event.phase is not RunPhase.DELIVERING for event in outcome.events)


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


@pytest.mark.parametrize("last_iteration", [4, 6])
def test_user_task_workflow_accepts_after_multiple_verified_revisions(
    tmp_path: Path, last_iteration: int
) -> None:
    budget = AgentBudget(
        authority=BudgetAuthority.USER_TASK, max_estimated_cost_usd="5"
    )
    approved = approved_inputs(
        run_id="adaptive-later-revision",
        iteration_limit=last_iteration,
        run_budget=budget,
    )
    source = initialize_source(tmp_path)
    source_head = git(source, "rev-parse", "HEAD").stdout.strip()
    workspace = tmp_path / "workspaces" / approved.task_brief.run_id

    class ResolvingExecutor(AdaptiveExecutor):
        def execute(self, request, *, activity_handler=None):
            if request.agent_id == "reviewer":
                self.always_revise = self.counts.get("reviewer", 0) < last_iteration - 1
            result = super().execute(request, activity_handler=activity_handler)
            if request.agent_id != "reviewer" or not self.always_revise:
                return result
            assert result.semantic_submission is not None
            payload = dict(result.semantic_submission.payload)
            finding = payload["findings"][0]
            # Resolve one independently identified finding per actual Git revision.
            payload["findings"] = [
                {
                    **finding,
                    "id": f"FINDING_DOCS_{index}",
                    "description": f"Document greeting usage detail {index}.",
                }
                for index in range(self.counts["reviewer"], last_iteration)
            ]
            return replace_semantic_payload(result, request, payload)

    executor = ResolvingExecutor(workspace)
    gates = RecordingQualityGateFactory()
    ledger = AgentBudgetLedger(budget)
    outcome = coordinator(
        tmp_path, approved, executor, gates, budget_ledger=ledger
    ).execute(approved, source_repository=source)
    store, report = load_report(tmp_path, outcome, approved)

    assert outcome.record.phase is RunPhase.COMPLETED, report.summary
    assert gates.calls == list(range(1, last_iteration + 1))
    assert len(report.iterations) == last_iteration
    expected_input = source_head
    for number, reference in enumerate(report.iterations, start=1):
        iteration = store.load(reference)
        assert isinstance(iteration, IterationRecord)
        assert iteration.iteration == number
        assert iteration.input_commit == expected_input
        git(
            workspace,
            "merge-base",
            "--is-ancestor",
            iteration.input_commit,
            iteration.output_commit,
        )
        expected_input = iteration.output_commit
        assert iteration.decision is (
            IterationDecision.ACCEPT
            if number == last_iteration
            else IterationDecision.REVISE
        )
    assert report.final_commit == expected_input
    assert ledger.snapshot().calls_completed == last_iteration * 3
    assert ledger.snapshot().active_calls == 0
    assert git(source, "rev-parse", "HEAD").stdout.strip() == source_head


@pytest.mark.parametrize("defect", ["gap", "fork", "duplicate", "endpoint"])
def test_writer_chain_rejects_incomplete_or_conflicting_ranges(
    tmp_path: Path, defect: str
) -> None:
    approved = approved_two_writer_proposal(tmp_path, reverse=False)
    (tmp_path / "run").mkdir()
    store = ArtifactStore(
        tmp_path / "run",
        task_brief=approved.task_brief,
        team_plan=approved.team_plan,
    )
    first = WorkResult(
        run_id=approved.task_brief.run_id,
        team_id=approved.team_plan.team_id,
        producer="builder",
        created_at=FIXED_TIME,
        iteration=1,
        input_commit="a" * 40,
        output_commit="b" * 40,
        summary="First writer range.",
        completed_tasks=("TASK_BUILD",),
        changed_files=("greeting.py",),
    )
    first_reference = store.write(first)
    snapshot = GitSnapshot(
        run_id=approved.task_brief.run_id,
        iteration=1,
        input_commit=first.input_commit,
        output_commit="c" * 40,
        commit_count=2,
        changed_files=("greeting.py",),
        recorded_at=FIXED_TIME,
    )
    if defect == "duplicate":
        references = (first_reference, first_reference)
    elif defect == "endpoint":
        references = (first_reference,)
    else:
        second = first.model_copy(
            update={
                "producer": "finisher",
                "completed_tasks": ("TASK_FINISH",),
                "input_commit": "d" * 40 if defect == "gap" else "a" * 40,
                "output_commit": "c" * 40,
            }
        )
        references = (first_reference, store.write(second))

    with pytest.raises(DynamicWorkflowError, match="chain"):
        DynamicWorkflowCoordinator._validate_work_chain(store, references, snapshot)


def test_controlled_evaluation_rejects_proposal_above_its_iteration_limit() -> None:
    import test_planning as planning_fixture

    body = planning_fixture.proposal_body().model_copy(update={"iteration_limit": 4})
    policy = planning_fixture.policy(max_iterations=3)

    with pytest.raises(planning_fixture.PlanningError, match="policy permits 3"):
        planning_fixture.preview_adaptive_proposal(
            planning_fixture.request(),
            planning_fixture.proposal(body=body),
            policy,
            created_at=FIXED_TIME,
        )


@pytest.mark.parametrize("rewrite_from_seed", [False, True])
def test_revision_ancestry_controls_gates_report_and_settlement(
    tmp_path: Path, rewrite_from_seed: bool
) -> None:
    approved = approved_inputs(run_id="adaptive-revision-ancestry", iteration_limit=2)
    source = initialize_source(tmp_path)
    seed = git(source, "rev-parse", "HEAD").stdout.strip()
    workspace = tmp_path / "workspaces" / approved.task_brief.run_id
    revision_inputs: list[str] = []

    class RevisionExecutor(AdaptiveExecutor):
        def execute(self, request, *, activity_handler=None):
            if request.agent_id == "builder":
                revision_inputs.append(
                    git(self.workspace, "rev-parse", "HEAD").stdout.strip()
                )
                if rewrite_from_seed and len(revision_inputs) == 2:
                    # Reproduce a writer rebuilding on the starter, not its input.
                    git(self.workspace, "reset", "--soft", seed)
            return super().execute(request, activity_handler=activity_handler)

    executor = RevisionExecutor(workspace, revise_first=True)
    gates = RecordingQualityGateFactory()
    ledger = AgentBudgetLedger(approved.team_plan.budget)
    outcome = coordinator(
        tmp_path, approved, executor, gates, budget_ledger=ledger
    ).execute(approved, source_repository=source)
    _, report = load_report(tmp_path, outcome, approved)
    head = git(workspace, "rev-parse", "HEAD").stdout.strip()
    parent = git(workspace, "rev-parse", "HEAD^").stdout.strip()
    assert len(revision_inputs) == 2
    assert revision_inputs[0] == seed
    assert revision_inputs[1] != seed
    usage = ledger.snapshot()
    assert usage.calls_started == usage.calls_completed == len(executor.requests)
    assert usage.active_calls == 0
    assert git(source, "rev-parse", "HEAD").stdout.strip() == seed

    if rewrite_from_seed:
        assert parent == seed
        assert outcome.record.phase is RunPhase.FAILED
        assert (
            outcome.record.termination_reason
            is TerminationReason.SAFETY_BOUNDARY_CROSSED
        )
        assert report.status is FinalStatus.FAILED
        assert report.final_commit != head
        assert gates.calls == [1]
        assert executor.counts["reviewer"] == 1
        assert any(
            "not a descendant" in finding for finding in report.unresolved_findings
        )
        assert all(event.phase is not RunPhase.DELIVERING for event in outcome.events)
    else:
        assert parent == revision_inputs[1]
        assert outcome.record.phase is RunPhase.COMPLETED
        assert report.status is FinalStatus.COMPLETED
        assert report.final_commit == head
        assert gates.calls == [1, 2]
        assert executor.counts["reviewer"] == 2


def test_failed_reverification_distinguishes_prior_finding_from_current_commit(
    tmp_path: Path,
) -> None:
    approved = approved_inputs(run_id="adaptive-reverify-timeout", iteration_limit=2)
    source = initialize_source(tmp_path)
    executor = AdaptiveExecutor(
        tmp_path / "workspaces" / approved.task_brief.run_id,
        revise_first=True,
        timeout_second_review=True,
    )
    gates = RecordingQualityGateFactory()

    outcome = coordinator(tmp_path, approved, executor, gates).execute(
        approved,
        source_repository=source,
    )
    store, report = load_report(tmp_path, outcome, approved)
    first_iteration = store.load(report.iterations[0])

    assert isinstance(first_iteration, IterationRecord)
    assert outcome.record.phase is RunPhase.FAILED
    assert outcome.record.termination_reason is (
        TerminationReason.RESOURCE_LIMIT_REACHED
    )
    assert report.final_commit != first_iteration.output_commit
    carried = next(
        item
        for item in report.unresolved_findings
        if item.startswith("Finding FINDING_DOCS was discovered")
    )
    assert first_iteration.output_commit[:12] in carried
    assert report.final_commit is not None
    assert report.final_commit[:12] in carried
    assert "independent re-verification did not complete" in carried
    assert carried.endswith("The usage result type is not documented.")


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


def test_dynamic_workflow_preserves_runtime_failure_when_usage_is_unknown(
    tmp_path: Path,
) -> None:
    user_budget = AgentBudget(
        authority=BudgetAuthority.USER_TASK,
        max_estimated_cost_usd="5",
    )
    approved = approved_inputs(
        run_id="adaptive-initialization-stall",
        run_budget=user_budget,
    )
    source = initialize_source(tmp_path)

    class InitializationStallExecutor:
        def execute(
            self,
            request: AgentExecutionRequest,
            *,
            activity_handler: AgentExecutionActivityHandler | None = None,
        ) -> AgentExecutionResult:
            del activity_handler
            return AgentExecutionResult(
                status=AgentExecutionStatus.INITIALIZATION_STALLED,
                error="scripted initialization stall",
                telemetry=AgentExecutionTelemetry(
                    role=None,
                    agent_id=request.agent_id,
                    capability=request.capability,
                    session_key=request.session_key,
                    command=("fake-agent", request.agent_id),
                    started_at=FIXED_TIME,
                    finished_at=FIXED_TIME,
                    duration_ms=90_000,
                    exit_code=-15,
                    stdout="",
                    stderr="scripted initialization stall",
                ),
            )

    gates = RecordingQualityGateFactory()
    executor = InitializationStallExecutor()
    outcome = coordinator(
        tmp_path,
        approved,
        executor,
        gates,
    ).execute(approved, source_repository=source)
    store, report = load_report(tmp_path, outcome, approved)

    assert outcome.record.phase is RunPhase.FAILED
    assert outcome.record.termination_reason is TerminationReason.DEPENDENCY_UNAVAILABLE
    assert report.status is FinalStatus.FAILED
    assert report.termination_reason == TerminationReason.DEPENDENCY_UNAVAILABLE.value
    assert "scripted initialization stall" in report.unresolved_findings[0]
    execution = store.load(outcome.execution_records[0])
    assert isinstance(execution, AgentExecutionRecord)
    assert execution.execution_status is AgentExecutionStatus.INITIALIZATION_STALLED
    assert "budget rejection" in (execution.error or "")
    ledger = json.loads(
        (
            tmp_path / "runs" / approved.task_brief.run_id / "budget-ledger.json"
        ).read_text(encoding="utf-8")
    )
    assert ledger["usage"]["calls_started"] == 1
    assert ledger["usage"]["calls_completed"] == 1
    assert ledger["usage"]["active_calls"] == 0
    assert ledger["usage"]["unreported_token_calls"] == 1
    markdown = tmp_path / "runs" / approved.task_brief.run_id / "final-report.md"
    rendered = markdown.read_text(encoding="utf-8")
    assert "Termination reason: `dependency_unavailable`" in rendered
    assert "Calls with unknown cost: 1" in rendered


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


def test_dynamic_workflow_applies_guidance_to_the_next_agent_invocation(
    tmp_path: Path,
) -> None:
    approved = approved_inputs(run_id="adaptive-guidance")
    source = initialize_source(tmp_path)
    executor = AdaptiveExecutor(tmp_path / "workspaces" / approved.task_brief.run_id)
    gates = RecordingQualityGateFactory()

    def queue_guidance(store: ControlCommandStore, team_plan: TeamPlan):
        del team_plan
        store.request(
            command=ControlCommandType.GUIDE,
            instruction="Expose only the greet function as public API.",
            target=ControlTarget(
                kind=ControlTargetKind.AGENT,
                agent_id="builder",
            ),
            application_boundary=(ControlApplicationBoundary.BEFORE_NEXT_INVOCATION),
            command_id="ctl-workflow-guide",
        )

    outcome = coordinator(
        tmp_path,
        approved,
        executor,
        gates,
        control_store_handler=queue_guidance,
    ).execute(approved, source_repository=source)

    builder_request = next(
        request for request in executor.requests if request.agent_id == "builder"
    )
    assert outcome.record.phase is RunPhase.COMPLETED
    assert "Expose only the greet function as public API." in builder_request.prompt
    assert "ctl-workflow-guide" in builder_request.prompt
    control_events = [
        event
        for event in outcome.events
        if event.control_command_id == "ctl-workflow-guide"
    ]
    assert [event.kind for event in control_events] == [
        ProgressEventKind.CONTROL_RECEIVED,
        ProgressEventKind.CONTROL_APPLIED,
    ]


def test_dynamic_workflow_cancellation_is_distinct_and_runs_control_cleanup(
    tmp_path: Path,
) -> None:
    approved = approved_inputs(run_id="adaptive-cancel")
    source = initialize_source(tmp_path)
    executor = AdaptiveExecutor(tmp_path / "workspaces" / approved.task_brief.run_id)
    gates = RecordingQualityGateFactory()
    cleaned: list[bool] = []

    def queue_cancel(store: ControlCommandStore, team_plan: TeamPlan):
        del team_plan
        store.request(
            command=ControlCommandType.CANCEL,
            target=ControlTarget(kind=ControlTargetKind.RUN),
            application_boundary=ControlApplicationBoundary.IMMEDIATE,
            command_id="ctl-workflow-cancel",
        )
        return lambda: cleaned.append(True)

    outcome = coordinator(
        tmp_path,
        approved,
        executor,
        gates,
        control_store_handler=queue_cancel,
    ).execute(approved, source_repository=source)
    _, report = load_report(tmp_path, outcome, approved)

    assert outcome.record.phase is RunPhase.FAILED
    assert outcome.record.termination_reason is TerminationReason.USER_CANCELLED
    assert outcome.control_stop is RuntimeControlDecision.CANCEL
    assert report.status is FinalStatus.CANCELLED
    assert outcome.schedules[0].status is ScheduleStatus.CANCELLED
    assert executor.requests == []
    assert gates.calls == []
    assert cleaned == [True]
    assert outcome.events[-1].kind is ProgressEventKind.RUN_CANCELLED
    store = ControlCommandStore(
        tmp_path / "runs" / approved.task_brief.run_id,
        run_id=approved.task_brief.run_id,
        clock=lambda: FIXED_TIME,
    )
    assert store.load("ctl-workflow-cancel")[-1].status is (
        ControlCommandStatus.APPLIED
    )


def test_dynamic_workflow_correction_preserves_instruction_for_replanning(
    tmp_path: Path,
) -> None:
    approved = approved_inputs(run_id="adaptive-correct")
    source = initialize_source(tmp_path)
    executor = AdaptiveExecutor(tmp_path / "workspaces" / approved.task_brief.run_id)
    gates = RecordingQualityGateFactory()

    def queue_correction(store: ControlCommandStore, team_plan: TeamPlan):
        del team_plan
        store.request(
            command=ControlCommandType.CORRECT,
            instruction="Return a command-line utility instead.",
            target=ControlTarget(kind=ControlTargetKind.RUN),
            application_boundary=ControlApplicationBoundary.PLANNING_REVISION,
            command_id="ctl-workflow-correct",
        )

    outcome = coordinator(
        tmp_path,
        approved,
        executor,
        gates,
        control_store_handler=queue_correction,
    ).execute(approved, source_repository=source)
    _, report = load_report(tmp_path, outcome, approved)

    assert outcome.control_stop is RuntimeControlDecision.CORRECT
    assert outcome.correction_instruction == "Return a command-line utility instead."
    assert outcome.record.termination_reason is (
        TerminationReason.USER_CORRECTION_REQUESTED
    )
    assert report.status is FinalStatus.CANCELLED
    assert outcome.schedules[0].status is ScheduleStatus.CORRECTION_REQUESTED
    assert executor.requests == []


def test_dynamic_report_render_failure_uses_one_terminal_failure_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    approved = approved_inputs(run_id="adaptive-report-failure")
    source = initialize_source(tmp_path)
    executor = AdaptiveExecutor(tmp_path / "workspaces" / approved.task_brief.run_id)

    def fail_render(**_kwargs: object) -> str:
        raise RuntimeError("injected dynamic report failure")

    monkeypatch.setattr(
        dynamic_workflow_module,
        "render_run_report",
        fail_render,
    )
    outcome = coordinator(
        tmp_path,
        approved,
        executor,
        RecordingQualityGateFactory(),
    ).execute(approved, source_repository=source)

    run_directory = tmp_path / "runs" / approved.task_brief.run_id
    markdown = (run_directory / outcome.human_report_path).read_text(encoding="utf-8")
    assert outcome.record.phase is RunPhase.FAILED
    assert "injected dynamic report failure" in outcome.record.termination_detail
    assert "Report finalization diagnostic" in markdown
    assert (run_directory / "final-report.json").is_file()
    assert (run_directory / "budget-ledger.json").is_file()


def test_dynamic_success_bundle_rolls_back_before_controller_failure_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    approved = approved_inputs(run_id="adaptive-terminal-transition-failure")
    source = initialize_source(tmp_path)
    executor = AdaptiveExecutor(tmp_path / "workspaces" / approved.task_brief.run_id)

    def fail_complete(*_args: object, **_kwargs: object) -> object:
        raise RunControlError("injected dynamic terminal transition failure")

    monkeypatch.setattr(
        dynamic_workflow_module.RunController,
        "complete",
        fail_complete,
    )
    outcome = coordinator(
        tmp_path,
        approved,
        executor,
        RecordingQualityGateFactory(),
    ).execute(approved, source_repository=source)

    run_directory = tmp_path / "runs" / approved.task_brief.run_id
    report = json.loads((run_directory / "final-report.json").read_text())
    assert outcome.record.phase is RunPhase.FAILED
    assert "injected dynamic terminal transition failure" in (
        outcome.record.termination_detail
    )
    assert report["status"] == "failed"
    assert len(tuple(run_directory.glob("final-report.json"))) == 1
    assert (run_directory / "final-report.md").is_file()
    assert (run_directory / "budget-ledger.json").is_file()
