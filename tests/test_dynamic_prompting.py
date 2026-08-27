"""Tests for approved AgentSpec prompts and dynamic semantic responses."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from software_agent_team.artifacts import (
    AcceptanceCriterion,
    ArtifactKind,
    CommandEvidence,
    HandoffStatus,
    ReviewFinding,
    ReviewSeverity,
    TaskBrief,
)
from software_agent_team.budgets import AgentBudget
from software_agent_team.execution import ScriptedAgentExecutor, ScriptedAgentResponse
from software_agent_team.integrity import canonical_model_sha256
from software_agent_team.planning import AdaptiveImplementationPlan, ProposedTask
from software_agent_team.prompting import (
    DynamicAgentPromptInputs,
    DynamicRevisionFeedback,
    DynamicUpstreamResult,
    DynamicUserGuidance,
    build_dynamic_agent_execution_request,
    render_dynamic_agent_prompt,
)
from software_agent_team.responses import (
    AgentArtifactResponseError,
    WorkResultResponse,
    parse_dynamic_agent_response,
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

CREATED_AT = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
INPUT_COMMIT = "1" * 40
OUTPUT_COMMIT = "2" * 40


def task_brief() -> TaskBrief:
    return TaskBrief(
        run_id="sat-dynamic-prompt",
        title="Markdown link checker",
        source_request="Build a CLI that checks Markdown links.",
        requirements=["Report broken local links."],
        acceptance_criteria=[
            AcceptanceCriterion(
                id="AC_LINKS",
                description="Broken links produce a non-zero exit status.",
                verification="Run the CLI against a broken fixture.",
            )
        ],
        constraints=["Do not fetch network resources."],
        confirmed=True,
    )


def implementation_plan() -> AdaptiveImplementationPlan:
    return AdaptiveImplementationPlan(
        run_id="sat-dynamic-prompt",
        team_id="adaptive_team",
        revision=1,
        created_at=CREATED_AT,
        objective="Deliver a tested local-link CLI.",
        approach=("Separate scanning from CLI presentation.",),
        tasks=(
            ProposedTask(
                id="TASK_LINKS",
                owner_agent_id="cli_developer",
                description="Implement scanning and CLI behavior.",
                acceptance_criteria=("AC_LINKS",),
                expected_paths=("src", "tests"),
            ),
        ),
        risks=("Markdown syntax has edge cases.",),
        assumptions=("Only local links are in scope.",),
    )


def team_plan() -> TeamPlan:
    brief = task_brief()
    plan = implementation_plan()
    return TeamPlan(
        plan_id="sat-dynamic-prompt-team-r1",
        revision=1,
        run_id=brief.run_id,
        task_brief_sha256=canonical_model_sha256(brief),
        implementation_plan_sha256=canonical_model_sha256(plan),
        team_id="adaptive_team",
        origin=TeamPlanOrigin.ADAPTIVE_PLANNING,
        approval_source=PlanApprovalSource.USER,
        created_at=CREATED_AT,
        agents=(
            AgentSpec(
                id="cli_developer",
                label="CLI Developer",
                responsibility="Implement the assigned CLI task.",
                rationale="The task has one cohesive write path.",
                capability=AgentCapability.IMPLEMENTATION,
                permission_profile=PermissionProfile.WORKSPACE_WRITE,
                stage_id="implement",
                expected_output=ArtifactKind.WORK_RESULT,
                model_route_id="default",
                timeout_seconds=600,
                workspace_scope="repository",
            ),
            AgentSpec(
                id="acceptance_tester",
                label="Acceptance Tester",
                responsibility="Analyze every deterministic acceptance result.",
                rationale="Testing remains independent from implementation.",
                capability=AgentCapability.TESTING,
                permission_profile=PermissionProfile.READ_ONLY,
                stage_id="verify",
                dependencies=("cli_developer",),
                expected_output=ArtifactKind.TEST_REPORT,
                model_route_id="default",
                timeout_seconds=240,
                workspace_scope="repository",
            ),
            AgentSpec(
                id="quality_reviewer",
                label="Quality Reviewer",
                responsibility="Review manual acceptance and source quality.",
                rationale="The writer cannot approve its own result.",
                capability=AgentCapability.REVIEW,
                permission_profile=PermissionProfile.READ_ONLY,
                stage_id="verify",
                dependencies=("cli_developer",),
                expected_output=ArtifactKind.REVIEW_REPORT,
                model_route_id="default",
                timeout_seconds=240,
                workspace_scope="repository",
            ),
        ),
        model_routes=ModelRoutePlan(
            mode=ModelRoutingMode.STRICT,
            default_route_id="default",
            routes=(ModelRoute(id="default", model="provider/model"),),
        ),
        budget=AgentBudget(
            max_calls=14,
            max_input_tokens=1_000_000,
            max_output_tokens=200_000,
            max_agent_duration_seconds=7_200,
            max_estimated_cost_usd="25",
        ),
        iteration_limit=2,
        max_concurrency=2,
        independent_review=True,
        revision_enabled=True,
    )


def command_evidence() -> tuple[CommandEvidence, ...]:
    return (
        CommandEvidence(
            id="CHECK_TEST",
            argv=("pytest",),
            criterion_ids=("AC_LINKS",),
            exit_code=0,
            duration_ms=25,
            stdout_path="iterations/01/check-test.stdout",
            stderr_path="iterations/01/check-test.stderr",
            stdout_tail="1 passed\n",
            summary="All tests passed.",
        ),
    )


def upstream_result() -> DynamicUpstreamResult:
    return DynamicUpstreamResult(
        agent_id="cli_developer",
        status=HandoffStatus.COMPLETED,
        summary="Implemented and committed the assigned CLI behavior.",
        output_commit=OUTPUT_COMMIT,
        completed_task_ids=("TASK_LINKS",),
    )


def developer_inputs() -> DynamicAgentPromptInputs:
    return DynamicAgentPromptInputs(
        task_brief=task_brief(),
        implementation_plan=implementation_plan(),
        team_plan=team_plan(),
        agent_id="cli_developer",
        iteration=1,
        iteration_input_commit=INPUT_COMMIT,
        input_commit=INPUT_COMMIT,
    )


def quality_inputs() -> DynamicAgentPromptInputs:
    return DynamicAgentPromptInputs(
        task_brief=task_brief(),
        implementation_plan=implementation_plan(),
        team_plan=team_plan(),
        agent_id="acceptance_tester",
        iteration=1,
        iteration_input_commit=INPUT_COMMIT,
        input_commit=OUTPUT_COMMIT,
        upstream_results=(upstream_result(),),
        command_evidence=command_evidence(),
    )


def revision_feedback() -> DynamicRevisionFeedback:
    return DynamicRevisionFeedback(
        previous_iteration=1,
        output_commit=OUTPUT_COMMIT,
        blocking_findings=(
            ReviewFinding(
                id="FINDING_DOCS",
                severity=ReviewSeverity.HIGH,
                blocking=True,
                category="documentation",
                description="The usage example omits the failure exit status.",
                recommendation="Document the failure behavior with an example.",
                path="README.md",
                criterion_ids=("AC_LINKS",),
            ),
        ),
        summary="Correct the documented failure behavior.",
    )


def test_dynamic_prompt_is_compiled_from_the_approved_agent_spec() -> None:
    inputs = developer_inputs()

    rendered = render_dynamic_agent_prompt(inputs)
    request = build_dynamic_agent_execution_request(inputs)

    assert "CLI Developer" in rendered
    assert '"id": "cli_developer"' in rendered
    assert '"id": "TASK_LINKS"' in rendered
    assert "quality_reviewer" not in rendered
    assert request.agent_id == "cli_developer"
    assert request.role is None
    assert request.capability is AgentCapability.IMPLEMENTATION
    assert request.expected_kind is ArtifactKind.WORK_RESULT
    assert request.timeout_seconds == 600
    assert request.model == "provider/model"


def test_dynamic_prompt_includes_unique_persisted_user_guidance() -> None:
    guidance = DynamicUserGuidance(
        command_id="ctl-prompt-guide",
        instruction="Keep error output concise and actionable.",
    )
    inputs = developer_inputs().model_copy(update={"user_guidance": (guidance,)})

    rendered = render_dynamic_agent_prompt(inputs)

    assert '"command_id": "ctl-prompt-guide"' in rendered
    assert "Keep error output concise and actionable." in rendered
    payload = inputs.model_dump(mode="json")
    payload["user_guidance"].append(payload["user_guidance"][0])
    with pytest.raises(ValidationError, match="repeat user guidance"):
        DynamicAgentPromptInputs.model_validate(payload)


def test_quality_prompt_requires_exact_completed_dependency_handoffs() -> None:
    payload = quality_inputs().model_dump(mode="json")
    payload["upstream_results"] = []

    with pytest.raises(ValidationError, match="exactly cover dependencies"):
        DynamicAgentPromptInputs.model_validate(payload)

    payload = quality_inputs().model_dump(mode="json")
    payload["upstream_results"][0]["status"] = "blocked"
    with pytest.raises(ValidationError, match="must be completed"):
        DynamicAgentPromptInputs.model_validate(payload)


def test_quality_prompt_contains_read_only_evidence_not_write_authority() -> None:
    rendered = render_dynamic_agent_prompt(quality_inputs())

    assert '"access": "read_only"' in rendered
    assert '"id": "CHECK_TEST"' in rendered
    assert "Do not modify files or execute additional commands" in rendered
    assert "TASK_LINKS" in rendered


def test_dynamic_revision_requires_commit_bound_blocking_feedback() -> None:
    inputs = developer_inputs().model_copy(
        update={
            "iteration": 2,
            "iteration_input_commit": OUTPUT_COMMIT,
            "input_commit": OUTPUT_COMMIT,
            "revision_feedback": revision_feedback(),
        }
    )

    rendered = render_dynamic_agent_prompt(inputs)

    assert '"previous_iteration": 1' in rendered
    assert '"id": "FINDING_DOCS"' in rendered
    assert "correct every attributable blocker" in rendered

    payload = inputs.model_dump(mode="json")
    payload["revision_feedback"] = None
    with pytest.raises(ValidationError, match="requires prior blocking feedback"):
        DynamicAgentPromptInputs.model_validate(payload)

    payload = inputs.model_dump(mode="json")
    payload["revision_feedback"]["output_commit"] = INPUT_COMMIT
    with pytest.raises(ValidationError, match="iteration input commit"):
        DynamicAgentPromptInputs.model_validate(payload)


def test_dynamic_response_binds_identity_and_exact_assigned_tasks() -> None:
    execution_request = build_dynamic_agent_execution_request(developer_inputs())
    result = ScriptedAgentExecutor(
        [
            ScriptedAgentResponse(
                text=WorkResultResponse(
                    summary="Implemented the local-link checker.",
                    completed_tasks=("TASK_LINKS",),
                ).model_dump_json(),
                model="provider/model",
            )
        ],
        clock=lambda: CREATED_AT,
    ).execute(execution_request)

    parsed = parse_dynamic_agent_response(
        result,
        execution_request,
        task_brief=task_brief(),
        team_plan=team_plan(),
        assigned_task_ids=("TASK_LINKS",),
    )

    assert isinstance(parsed.body, WorkResultResponse)
    assert parsed.body.completed_tasks == ("TASK_LINKS",)


def test_dynamic_response_rejects_unassigned_tasks_and_fixed_role_aliases() -> None:
    execution_request = build_dynamic_agent_execution_request(developer_inputs())
    result = ScriptedAgentExecutor(
        [
            ScriptedAgentResponse(
                text=json.dumps(
                    {
                        "summary": "Claimed unrelated work.",
                        "completed_tasks": ["TASK_UNASSIGNED"],
                        "unresolved_issues": [],
                    }
                ),
                model="provider/model",
            )
        ],
        clock=lambda: CREATED_AT,
    ).execute(execution_request)

    with pytest.raises(AgentArtifactResponseError, match="exactly match"):
        parse_dynamic_agent_response(
            result,
            execution_request,
            task_brief=task_brief(),
            team_plan=team_plan(),
            assigned_task_ids=("TASK_LINKS",),
        )

    aliased_request = execution_request.model_copy(
        update={"role": "generalist_developer"}
    )
    with pytest.raises(AgentArtifactResponseError, match="fixed legacy role"):
        parse_dynamic_agent_response(
            result,
            aliased_request,
            task_brief=task_brief(),
            team_plan=team_plan(),
            assigned_task_ids=("TASK_LINKS",),
        )


def test_dynamic_response_rejects_unapproved_model_or_timeout() -> None:
    execution_request = build_dynamic_agent_execution_request(developer_inputs())
    result = ScriptedAgentExecutor(
        [
            ScriptedAgentResponse(
                text=WorkResultResponse(
                    summary="Implemented the local-link checker.",
                    completed_tasks=("TASK_LINKS",),
                ).model_dump_json(),
                model="provider/model",
            )
        ],
        clock=lambda: CREATED_AT,
    ).execute(execution_request)

    for unauthorized in (
        execution_request.model_copy(update={"timeout_seconds": 601}),
        execution_request.model_copy(update={"model": "provider/other"}),
    ):
        with pytest.raises(AgentArtifactResponseError, match="timeout or model"):
            parse_dynamic_agent_response(
                result,
                unauthorized,
                task_brief=task_brief(),
                team_plan=team_plan(),
                assigned_task_ids=("TASK_LINKS",),
            )


def test_dynamic_response_rejects_unapproved_telemetry_model() -> None:
    execution_request = build_dynamic_agent_execution_request(developer_inputs())
    result = ScriptedAgentExecutor(
        [
            ScriptedAgentResponse(
                text=WorkResultResponse(
                    summary="Implemented the local-link checker.",
                    completed_tasks=("TASK_LINKS",),
                ).model_dump_json(),
                model="provider/other",
            )
        ],
        clock=lambda: CREATED_AT,
    ).execute(execution_request)

    with pytest.raises(AgentArtifactResponseError, match="telemetry model"):
        parse_dynamic_agent_response(
            result,
            execution_request,
            task_brief=task_brief(),
            team_plan=team_plan(),
            assigned_task_ids=("TASK_LINKS",),
        )
