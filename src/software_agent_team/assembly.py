"""Controller-owned assembly of Agent semantics and verified runtime facts."""

from __future__ import annotations

from datetime import UTC, datetime

from software_agent_team.artifacts import (
    CheckStatus,
    CommandEvidence,
    CriterionResult,
    ReviewReport,
    TaskBrief,
    TestReport,
    WorkResult,
)
from software_agent_team.git_workspace import GitSnapshot
from software_agent_team.responses import (
    GroundedReviewReportResponse,
    TestReportResponse,
    WorkResultResponse,
)
from software_agent_team.teams import AgentCapability, AgentSpec


class ArtifactAssemblyError(ValueError):
    """Raised when semantic content cannot bind to controller-owned evidence."""


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ArtifactAssemblyError("artifact assembly timestamps require a timezone")
    return value.astimezone(UTC)


def validate_verification_assignment(
    task_brief: TaskBrief,
    commands: tuple[CommandEvidence, ...],
    manual_review_criteria: tuple[str, ...],
) -> None:
    """Require deterministic and manual scopes to cover confirmed acceptance."""

    if not commands:
        raise ArtifactAssemblyError("quality gates returned no command evidence")
    expected = {criterion.id for criterion in task_brief.acceptance_criteria}
    if any(not command.criterion_ids for command in commands):
        raise ArtifactAssemblyError(
            "quality-gate evidence is missing criterion coverage"
        )
    deterministic = {
        criterion_id for command in commands for criterion_id in command.criterion_ids
    }
    manual = set(manual_review_criteria)
    if not deterministic.issubset(expected) or not manual.issubset(expected):
        raise ArtifactAssemblyError(
            "verification assignment references an unknown criterion"
        )
    if deterministic | manual != expected:
        raise ArtifactAssemblyError(
            "verification assignment does not cover every criterion"
        )


def assemble_work_result(
    body: WorkResultResponse,
    *,
    task_brief: TaskBrief,
    team_id: str,
    agent: AgentSpec,
    snapshot: GitSnapshot,
    created_at: datetime,
) -> WorkResult:
    """Bind Developer semantics to one controller-verified Git snapshot."""

    if agent.capability not in {
        AgentCapability.IMPLEMENTATION,
        AgentCapability.INTEGRATION,
    }:
        raise ArtifactAssemblyError(
            "work-result assembly requires an implementation capability"
        )
    if snapshot.run_id != task_brief.run_id:
        raise ArtifactAssemblyError("Git snapshot belongs to a different run")
    return WorkResult(
        run_id=task_brief.run_id,
        team_id=team_id,
        producer=agent.id,
        created_at=_utc(created_at),
        iteration=snapshot.iteration,
        input_commit=snapshot.input_commit,
        output_commit=snapshot.output_commit,
        changed_files=snapshot.changed_files,
        summary=body.summary,
        completed_tasks=body.completed_tasks,
        unresolved_issues=body.unresolved_issues,
    )


def assemble_test_report(
    body: TestReportResponse | None,
    *,
    task_brief: TaskBrief,
    team_id: str,
    agent: AgentSpec | None,
    iteration: int,
    input_commit: str,
    commands: tuple[CommandEvidence, ...],
    manual_review_criteria: tuple[str, ...],
    created_at: datetime,
) -> TestReport:
    """Bind optional Tester analysis to controller-owned deterministic evidence."""

    if body is None:
        if agent is not None:
            raise ArtifactAssemblyError(
                "controller-only test assembly cannot claim an Agent producer"
            )
        producer = "controller"
    else:
        if agent is None or agent.capability is not AgentCapability.TESTING:
            raise ArtifactAssemblyError(
                "test-report assembly requires a testing capability"
            )
        producer = agent.id
    validate_verification_assignment(
        task_brief,
        commands,
        manual_review_criteria,
    )
    if any(command.timed_out for command in commands):
        status = CheckStatus.BLOCKED
    elif any(command.exit_code != 0 for command in commands):
        status = CheckStatus.FAILED
    else:
        status = CheckStatus.PASSED

    manual = set(manual_review_criteria)
    criteria: list[CriterionResult] = []
    for criterion in task_brief.acceptance_criteria:
        evidence = tuple(
            command for command in commands if criterion.id in command.criterion_ids
        )
        command_ids = tuple(command.id for command in evidence)
        timed_out = tuple(command.id for command in evidence if command.timed_out)
        failed = tuple(
            command.id
            for command in evidence
            if not command.timed_out and command.exit_code != 0
        )
        if timed_out:
            criterion_status = CheckStatus.BLOCKED
            detail = f"Controller-recorded commands timed out: {', '.join(timed_out)}."
        elif failed:
            criterion_status = CheckStatus.FAILED
            detail = f"Controller-recorded commands failed: {', '.join(failed)}."
        elif criterion.id in manual:
            criterion_status = CheckStatus.PENDING_REVIEW
            detail = (
                "Deterministic evidence passed; independent review is pending."
                if command_ids
                else "This criterion is assigned to independent review."
            )
        else:
            criterion_status = CheckStatus.PASSED
            detail = f"Controller-recorded commands passed: {', '.join(command_ids)}."
        criteria.append(
            CriterionResult(
                criterion_id=criterion.id,
                status=criterion_status,
                command_ids=command_ids,
                detail=detail,
            )
        )

    blockers = tuple(
        f"Deterministic command {command.id} timed out."
        for command in commands
        if command.timed_out
    )
    return TestReport(
        run_id=task_brief.run_id,
        team_id=team_id,
        producer=producer,
        created_at=_utc(created_at),
        iteration=iteration,
        input_commit=input_commit,
        status=status,
        commands=commands,
        criteria=tuple(criteria),
        manual_review_criteria=manual_review_criteria,
        findings=() if body is None else body.findings,
        blockers=blockers,
        summary=(
            "Controller assembled deterministic acceptance evidence."
            if body is None
            else body.summary
        ),
    )


def assemble_review_report(
    body: GroundedReviewReportResponse,
    *,
    task_brief: TaskBrief,
    team_id: str,
    agent: AgentSpec,
    iteration: int,
    input_commit: str,
    reviewed_criteria: tuple[str, ...],
    created_at: datetime,
) -> ReviewReport:
    """Bind Reviewer semantics to the controller-owned commit and scope."""

    if agent.capability is not AgentCapability.REVIEW:
        raise ArtifactAssemblyError(
            "review-report assembly requires a review capability"
        )
    known = {criterion.id for criterion in task_brief.acceptance_criteria}
    if not set(reviewed_criteria).issubset(known):
        raise ArtifactAssemblyError("review scope references an unknown criterion")
    assessments_by_id = {
        assessment.criterion_id: assessment for assessment in body.criterion_assessments
    }
    if assessments_by_id and set(assessments_by_id) != set(reviewed_criteria):
        raise ArtifactAssemblyError(
            "review criterion assessments must exactly cover controller scope"
        )
    return ReviewReport(
        run_id=task_brief.run_id,
        team_id=team_id,
        producer=agent.id,
        created_at=_utc(created_at),
        iteration=iteration,
        input_commit=input_commit,
        verdict=body.verdict,
        termination_reason=body.termination_reason,
        reviewed_criteria=reviewed_criteria,
        criterion_assessments=(
            tuple(assessments_by_id[criterion_id] for criterion_id in reviewed_criteria)
            if assessments_by_id
            else ()
        ),
        findings=body.findings,
        summary=body.summary,
    )
