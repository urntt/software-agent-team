"""Tests for controller-owned dynamic artifact assembly."""

from datetime import UTC, datetime

import pytest

from software_agent_team.artifacts import (
    AcceptanceCriterion,
    ArtifactKind,
    CheckStatus,
    CommandEvidence,
    ReviewCriterionAssessment,
    ReviewToolEvidenceReference,
    ReviewVerdict,
    TaskBrief,
)
from software_agent_team.assembly import (
    ArtifactAssemblyError,
    assemble_review_report,
    assemble_test_report,
    assemble_work_result,
    validate_verification_assignment,
)
from software_agent_team.git_workspace import GitSnapshot
from software_agent_team.responses import (
    GroundedReviewReportResponse,
    WorkResultResponse,
)
from software_agent_team.responses import (
    TestReportResponse as AgentTestReportResponse,
)
from software_agent_team.teams import (
    AgentCapability,
    AgentSpec,
    PermissionProfile,
)

NOW = datetime(2026, 8, 26, 16, 0, tzinfo=UTC)
INPUT_COMMIT = "a" * 40
OUTPUT_COMMIT = "b" * 40


def task_brief() -> TaskBrief:
    """Return a confirmed task with deterministic and manual acceptance."""

    return TaskBrief(
        run_id="dynamic-run",
        title="Small Python service",
        source_request="Build a small Python service.",
        requirements=["Expose one working endpoint."],
        acceptance_criteria=[
            AcceptanceCriterion(
                id="AC_TESTS",
                description="The automated checks pass.",
                verification="Run the checked-in test command.",
            ),
            AcceptanceCriterion(
                id="AC_DOCUMENTATION",
                description="The usage documentation is understandable.",
                verification="Review the documented commands.",
            ),
        ],
        confirmed=True,
    )


def agent(agent_id: str, capability: AgentCapability) -> AgentSpec:
    """Create one coherent run-scoped AgentSpec."""

    writable = capability in {
        AgentCapability.IMPLEMENTATION,
        AgentCapability.INTEGRATION,
    }
    output = {
        AgentCapability.IMPLEMENTATION: ArtifactKind.WORK_RESULT,
        AgentCapability.INTEGRATION: ArtifactKind.WORK_RESULT,
        AgentCapability.TESTING: ArtifactKind.TEST_REPORT,
        AgentCapability.REVIEW: ArtifactKind.REVIEW_REPORT,
    }[capability]
    return AgentSpec(
        id=agent_id,
        label=agent_id.replace("_", " ").title(),
        responsibility=f"Perform {capability.value} work.",
        rationale="The approved plan requires this responsibility.",
        capability=capability,
        permission_profile=(
            PermissionProfile.WORKSPACE_WRITE
            if writable
            else PermissionProfile.READ_ONLY
        ),
        stage_id="implement" if writable else "verify",
        expected_output=output,
        model_route_id="default",
        timeout_seconds=300,
        workspace_scope="repository",
    )


def snapshot(*, run_id: str = "dynamic-run") -> GitSnapshot:
    """Return controller-verified Git evidence."""

    return GitSnapshot(
        run_id=run_id,
        iteration=1,
        input_commit=INPUT_COMMIT,
        output_commit=OUTPUT_COMMIT,
        commit_count=1,
        changed_files=("src/app.py", "tests/test_app.py"),
        recorded_at=NOW,
    )


def command(
    *,
    criterion_ids: tuple[str, ...] = ("AC_TESTS",),
    exit_code: int | None = 0,
    timed_out: bool = False,
) -> CommandEvidence:
    """Return one controller-recorded quality command."""

    return CommandEvidence(
        id="CHECK_TESTS",
        argv=("pytest", "-q"),
        criterion_ids=criterion_ids,
        exit_code=exit_code,
        timed_out=timed_out,
        duration_ms=25,
        stdout_path="iterations/01/commands/tests.stdout.txt",
        stderr_path="iterations/01/commands/tests.stderr.txt",
        summary="The controller recorded the quality command.",
    )


def test_work_result_binds_dynamic_agent_to_verified_git_facts() -> None:
    body = WorkResultResponse(
        summary="Implemented the approved endpoint.",
        completed_tasks=("TASK_ENDPOINT",),
    )

    result = assemble_work_result(
        body,
        task_brief=task_brief(),
        team_id="dynamic_team",
        agent=agent("service_builder", AgentCapability.IMPLEMENTATION),
        snapshot=snapshot(),
        created_at=NOW,
    )

    assert result.producer == "service_builder"
    assert result.input_commit == INPUT_COMMIT
    assert result.output_commit == OUTPUT_COMMIT
    assert result.changed_files == ("src/app.py", "tests/test_app.py")


@pytest.mark.parametrize(
    ("spec", "git_snapshot", "message"),
    [
        (
            agent("quality_auditor", AgentCapability.TESTING),
            snapshot(),
            "implementation capability",
        ),
        (
            agent("service_builder", AgentCapability.IMPLEMENTATION),
            snapshot(run_id="another-run"),
            "different run",
        ),
    ],
)
def test_work_result_rejects_unapproved_capability_or_run(
    spec: AgentSpec,
    git_snapshot: GitSnapshot,
    message: str,
) -> None:
    body = WorkResultResponse(
        summary="Claimed implementation work.",
        completed_tasks=("TASK_ENDPOINT",),
    )

    with pytest.raises(ArtifactAssemblyError, match=message):
        assemble_work_result(
            body,
            task_brief=task_brief(),
            team_id="dynamic_team",
            agent=spec,
            snapshot=git_snapshot,
            created_at=NOW,
        )


def test_test_report_derives_acceptance_from_controller_evidence() -> None:
    body = AgentTestReportResponse(
        findings=("The deterministic checks completed without a failure.",),
        summary="Automated verification passed.",
    )

    report = assemble_test_report(
        body,
        task_brief=task_brief(),
        team_id="dynamic_team",
        agent=agent("quality_auditor", AgentCapability.TESTING),
        iteration=1,
        input_commit=OUTPUT_COMMIT,
        commands=(command(),),
        manual_review_criteria=("AC_DOCUMENTATION",),
        created_at=NOW,
    )

    assert report.producer == "quality_auditor"
    assert report.status is CheckStatus.PASSED
    assert tuple(item.status for item in report.criteria) == (
        CheckStatus.PASSED,
        CheckStatus.PENDING_REVIEW,
    )
    assert report.criteria[0].command_ids == ("CHECK_TESTS",)
    assert report.criteria[1].command_ids == ()
    assert report.findings == body.findings


def test_controller_can_assemble_test_evidence_without_a_testing_agent() -> None:
    report = assemble_test_report(
        None,
        task_brief=task_brief(),
        team_id="dynamic_team",
        agent=None,
        iteration=1,
        input_commit=OUTPUT_COMMIT,
        commands=(command(),),
        manual_review_criteria=("AC_DOCUMENTATION",),
        created_at=NOW,
    )

    assert report.producer == "controller"
    assert report.findings == ()
    assert report.summary == "Controller assembled deterministic acceptance evidence."


def test_test_report_rejects_a_non_testing_agent() -> None:
    with pytest.raises(ArtifactAssemblyError, match="testing capability"):
        assemble_test_report(
            AgentTestReportResponse(summary="Claimed test result."),
            task_brief=task_brief(),
            team_id="dynamic_team",
            agent=agent("service_builder", AgentCapability.IMPLEMENTATION),
            iteration=1,
            input_commit=OUTPUT_COMMIT,
            commands=(command(),),
            manual_review_criteria=("AC_DOCUMENTATION",),
            created_at=NOW,
        )


def test_test_report_records_timeout_as_controller_blocking_evidence() -> None:
    report = assemble_test_report(
        AgentTestReportResponse(summary="The command did not complete."),
        task_brief=task_brief(),
        team_id="dynamic_team",
        agent=agent("quality_auditor", AgentCapability.TESTING),
        iteration=1,
        input_commit=OUTPUT_COMMIT,
        commands=(command(exit_code=None, timed_out=True),),
        manual_review_criteria=("AC_DOCUMENTATION",),
        created_at=NOW,
    )

    assert report.status is CheckStatus.BLOCKED
    assert report.criteria[0].status is CheckStatus.BLOCKED
    assert report.blockers == ("Deterministic command CHECK_TESTS timed out.",)


@pytest.mark.parametrize(
    ("commands", "manual", "message"),
    [
        ((), ("AC_TESTS", "AC_DOCUMENTATION"), "no command evidence"),
        (
            (command(criterion_ids=("AC_UNKNOWN",)),),
            ("AC_DOCUMENTATION",),
            "unknown criterion",
        ),
        ((command(),), (), "does not cover every criterion"),
    ],
)
def test_verification_assignment_requires_exact_confirmed_coverage(
    commands: tuple[CommandEvidence, ...],
    manual: tuple[str, ...],
    message: str,
) -> None:
    with pytest.raises(ArtifactAssemblyError, match=message):
        validate_verification_assignment(task_brief(), commands, manual)


def test_review_report_binds_dynamic_reviewer_and_controller_scope() -> None:
    body = GroundedReviewReportResponse(
        verdict=ReviewVerdict.ACCEPT,
        criterion_assessments=(
            ReviewCriterionAssessment(
                criterion_id="AC_DOCUMENTATION",
                status="satisfied",
                adversarial_check="Checked a clean first-use setup path.",
                evidence="README documents the validated setup and run commands.",
                tool_evidence=(
                    ReviewToolEvidenceReference(
                        tool_call_id="tool-001",
                        observable="README setup command",
                    ),
                ),
            ),
        ),
        summary="The usage documentation is clear and executable.",
    )

    report = assemble_review_report(
        body,
        task_brief=task_brief(),
        team_id="dynamic_team",
        agent=agent("documentation_reviewer", AgentCapability.REVIEW),
        iteration=1,
        input_commit=OUTPUT_COMMIT,
        reviewed_criteria=("AC_DOCUMENTATION",),
        created_at=NOW,
    )

    assert report.producer == "documentation_reviewer"
    assert report.input_commit == OUTPUT_COMMIT
    assert report.reviewed_criteria == ("AC_DOCUMENTATION",)
    assert report.criterion_assessments == body.criterion_assessments


@pytest.mark.parametrize(
    ("spec", "scope", "message"),
    [
        (
            agent("quality_auditor", AgentCapability.TESTING),
            ("AC_DOCUMENTATION",),
            "review capability",
        ),
        (
            agent("documentation_reviewer", AgentCapability.REVIEW),
            ("AC_UNKNOWN",),
            "unknown criterion",
        ),
    ],
)
def test_review_report_rejects_wrong_capability_or_scope(
    spec: AgentSpec,
    scope: tuple[str, ...],
    message: str,
) -> None:
    body = GroundedReviewReportResponse(
        verdict=ReviewVerdict.ACCEPT,
        criterion_assessments=(
            ReviewCriterionAssessment(
                criterion_id="AC_DOCUMENTATION",
                status="satisfied",
                adversarial_check="Checked the documented setup path.",
                evidence="The public README provides the required commands.",
                tool_evidence=(
                    ReviewToolEvidenceReference(
                        tool_call_id="tool-001",
                        observable="README commands",
                    ),
                ),
            ),
        ),
        summary="Claimed review result.",
    )

    with pytest.raises(ArtifactAssemblyError, match=message):
        assemble_review_report(
            body,
            task_brief=task_brief(),
            team_id="dynamic_team",
            agent=spec,
            iteration=1,
            input_commit=OUTPUT_COMMIT,
            reviewed_criteria=scope,
            created_at=NOW,
        )
