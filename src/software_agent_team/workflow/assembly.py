"""Artifact assembly functions and verification assignment validation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from software_agent_team.artifacts import (
    AgentRole,
    CommandEvidence,
    ImplementationPlan,
    ReviewReport,
    TaskBrief,
    TestReport,
    WorkResult,
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
    AgentResponseBody,
    GroundedReviewReportResponse,
    ImplementationPlanResponse,
    TestReportResponse,
    WorkResultResponse,
)
from software_agent_team.workflow.types import (
    WorkflowEvidenceError,
    _utc,
    _WorkflowContext,
)

if TYPE_CHECKING:
    from software_agent_team.workflow.coordinator import WorkflowCoordinator


def _validate_verification_assignment(
    coordinator: WorkflowCoordinator,
    brief: TaskBrief,
    commands: tuple[CommandEvidence, ...],
) -> None:
    try:
        validate_verification_assignment(
            brief,
            commands,
            coordinator.manual_review_criteria,
        )
    except ArtifactAssemblyError as error:
        raise WorkflowEvidenceError(str(error)) from error


def _assemble_plan(
    coordinator: WorkflowCoordinator,
    context: _WorkflowContext,
    body: AgentResponseBody,
) -> ImplementationPlan:
    if not isinstance(body, ImplementationPlanResponse):
        raise WorkflowEvidenceError("Planner returned the wrong semantic body")
    return ImplementationPlan(
        run_id=context.brief.run_id,
        team_id=context.team_plan.team_id,
        created_at=_utc(coordinator.clock),
        objective=body.objective,
        approach=body.approach,
        tasks=body.tasks,
        risks=body.risks,
        assumptions=body.assumptions,
    )


def _assemble_work_result(
    coordinator: WorkflowCoordinator,
    context: _WorkflowContext,
    body: AgentResponseBody,
    *,
    snapshot: GitSnapshot,
    role: AgentRole = AgentRole.GENERALIST_DEVELOPER,
) -> WorkResult:
    if not isinstance(body, WorkResultResponse):
        raise WorkflowEvidenceError("Developer returned the wrong semantic body")
    return assemble_work_result(
        body,
        task_brief=context.brief,
        team_id=context.team_plan.team_id,
        agent=context.team_plan.get_agent(role.value),
        snapshot=snapshot,
        created_at=_utc(coordinator.clock),
    )


def _assemble_test_report(
    coordinator: WorkflowCoordinator,
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
        manual_review_criteria=coordinator.manual_review_criteria,
        created_at=_utc(coordinator.clock),
    )


def _assemble_review_report(
    coordinator: WorkflowCoordinator,
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
        reviewed_criteria=coordinator.manual_review_criteria,
        created_at=_utc(coordinator.clock),
    )
