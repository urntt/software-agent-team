"""Tests for approved AgentSpec prompts and dynamic semantic responses."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from software_agent_team.artifacts import (
    AcceptanceCriterion,
    AgentToolCallEvidence,
    ArtifactKind,
    CommandEvidence,
    HandoffStatus,
    ReviewBoundaryKind,
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
from software_agent_team.response_corrections import ResponseIssueAuthority
from software_agent_team.responses import (
    AgentArtifactResponseError,
    GroundedReviewReportResponse,
    ReviewBoundaryCheckResponse,
    ReviewCriterionAssessmentResponse,
    ReviewReportResponse,
    ReviewToolEvidenceAttempt,
    ReviewToolEvidenceClaim,
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


def review_tool_claim(
    observable: str = "scripted-review-observation",
) -> ReviewToolEvidenceClaim:
    """Select the scripted adapter's explicit review observation."""

    return ReviewToolEvidenceClaim(observable=observable)


def captured_tool_call(
    index: int,
    output: str,
    *,
    executable: str | None = None,
    failed: bool = False,
) -> AgentToolCallEvidence:
    """Return one controller-numbered result for semantic grounding tests."""

    encoded = output.encode()
    return AgentToolCallEvidence(
        id=f"tool-{index:03d}",
        tool_name="exec" if executable is not None else "read",
        executable=executable,
        external_call_sha256=f"{index:064x}",
        arguments_sha256=f"{index + 100:064x}",
        outcome="failed" if failed else "succeeded",
        is_error=failed,
        exit_code=1 if failed else (0 if executable is not None else None),
        output_sha256=f"{index + 200:064x}",
        output_bytes=len(encoded),
        output_excerpt=output,
    )


def framed_probe_output(
    *,
    stdout: str = "",
    stderr: str = "",
    exit_code: int = 0,
    timed_out: bool = False,
) -> str:
    """Render the immutable runner's framed child streams and terminal result."""

    lines = ["SAT_PROBE_STDOUT_BEGIN"]
    if stdout:
        lines.append(stdout)
    lines.extend(("SAT_PROBE_STDOUT_END", "SAT_PROBE_STDERR_BEGIN"))
    if stderr:
        lines.append(stderr)
    lines.extend(
        (
            "SAT_PROBE_STDERR_END",
            "SAT_PROBE_RESULT_V1 "
            + json.dumps(
                {"exit_code": exit_code, "timed_out": timed_out},
                separators=(",", ":"),
            ),
        )
    )
    return "\n".join(lines)


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


def boundary_task_brief() -> TaskBrief:
    """Return a brief whose absolute link guarantee owns four obligations."""

    brief = task_brief()
    criterion = brief.acceptance_criteria[0].model_copy(
        update={"review_boundaries": tuple(ReviewBoundaryKind)}
    )
    return brief.model_copy(update={"acceptance_criteria": [criterion]})


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


def team_plan(brief: TaskBrief | None = None) -> TeamPlan:
    brief = brief or task_brief()
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
    assert "top-level user input" in rendered
    assert '"review_boundary_definitions": {' in rendered
    assert "root itself is the top-level input" in rendered
    assert "immediate first-level child, is nested input" in rendered
    assert "protocol identifiers, not informal filesystem depth labels" in " ".join(
        rendered.split()
    )
    assert "unqualified prohibition" in rendered
    assert "profile-owned setup and test command" in rendered
    assert "TaskBrief constraints are authoritative" in rendered
    assert "documented setup command" in rendered
    assert "explicit ignore policy" in rendered
    assert "without appending arguments" in rendered
    assert "clean-workspace pytest entrypoint" in rendered
    assert "pytest's import path" in rendered
    assert "exact shell form" in rendered


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


def test_dynamic_prompt_rejects_unknown_task_criterion_reference() -> None:
    plan = implementation_plan()
    tasks = (
        plan.tasks[0].model_copy(
            update={"acceptance_criteria": ("AC_LINKS", "AC_UNKNOWN")}
        ),
    )
    changed_plan = plan.model_copy(update={"tasks": tasks})
    changed_team = team_plan().model_copy(
        update={
            "implementation_plan_sha256": canonical_model_sha256(changed_plan),
        }
    )
    payload = developer_inputs().model_dump(mode="json")
    payload["implementation_plan"] = changed_plan.model_dump(mode="json")
    payload["team_plan"] = changed_team.model_dump(mode="json")

    with pytest.raises(
        ValidationError,
        match="dynamic implementation plan tasks reference unknown acceptance "
        "criteria: AC_UNKNOWN",
    ):
        DynamicAgentPromptInputs.model_validate(payload)


def test_quality_prompt_contains_read_only_evidence_not_write_authority() -> None:
    rendered = render_dynamic_agent_prompt(quality_inputs())

    assert '"access": "read_only"' in rendered
    assert '"id": "CHECK_TEST"' in rendered
    assert "Do not modify files or execute additional commands" in rendered
    assert "TASK_LINKS" in rendered


def test_review_prompt_requires_adversarial_absolute_claim_boundaries() -> None:
    inputs = quality_inputs().model_copy(
        update={
            "agent_id": "quality_reviewer",
            "manual_review_criteria": ("AC_LINKS",),
        }
    )

    rendered = render_dynamic_agent_prompt(inputs)
    compact = " ".join(rendered.split())

    assert "top-level user input" in rendered
    assert '"review_boundary_definitions": {' in rendered
    assert "root itself is the top-level input" in rendered
    assert "immediate first-level child, is nested input" in rendered
    assert "protocol identifiers, not informal filesystem depth labels" in compact
    assert "sat-probe-write /tmp/sat-review-probe-boundaries-7f3a.py" in rendered
    assert "sat-probe-run /tmp/sat-review-probe-<suffix>.py" in rendered
    assert "SAT_PROBE_RESULT_V1" in rendered
    assert "Do not use `python -c`" in rendered
    assert "project access is read-only" in compact
    assert "Do not modify source or project files" in compact
    assert "write tool and general file-mutation tools are unavailable" in compact
    assert "helper reports success" in compact
    assert "shell redirection" in compact
    assert "covers related criteria" in rendered
    assert "Do not modify\nfiles" not in rendered
    assert "call mutating tools" not in rendered
    assert "one concrete counterexample" in rendered
    assert "silently dirty" in rendered
    assert "exact start argv" in rendered
    assert "without adding arguments" in compact
    assert "clean-workspace pytest evidence" in rendered
    assert "criterion_assessments" in rendered
    assert "bounded foreground commands" in rendered
    assert "negative, empty, singleton, boundary" in rendered
    assert "passing substring from an overall failed result" in rendered
    assert "return `boundary_checks` explicitly" in rendered
    assert "distinct from every other" in rendered
    schema_text = rendered.split("RESPONSE_SCHEMA_JSON\n", 1)[1].split(
        "\n\nFINAL_RESPONSE_CONTRACT",
        1,
    )[0]
    response_schema = json.loads(schema_text)
    assert "criterion_assessments" in response_schema["required"]
    assert "default" not in response_schema["properties"]["criterion_assessments"]
    assessment_schema = response_schema["$defs"]["ReviewCriterionAssessmentResponse"]
    assert "tool_evidence" in assessment_schema["required"]
    assert "default" not in assessment_schema["properties"]["tool_evidence"]
    assert "boundary_checks" in assessment_schema["required"]
    assert "default" not in assessment_schema["properties"]["boundary_checks"]
    boundary_schema = response_schema["$defs"]["ReviewBoundaryCheckResponse"]
    assert set(boundary_schema["required"]) == {
        "boundary",
        "adversarial_check",
        "tool_evidence",
    }
    claim_schema = response_schema["$defs"]["ReviewToolEvidenceClaim"]
    assert claim_schema["required"] == ["observable"]
    assert "tool_call_id" not in claim_schema["properties"]
    assert "execution_attempt" not in claim_schema["properties"]


def _review_result(response: ReviewReportResponse | str):
    inputs = quality_inputs().model_copy(
        update={
            "agent_id": "quality_reviewer",
            "manual_review_criteria": ("AC_LINKS",),
        }
    )
    request = build_dynamic_agent_execution_request(inputs)
    result = ScriptedAgentExecutor(
        [
            ScriptedAgentResponse(
                text=(
                    response.model_dump_json()
                    if isinstance(response, ReviewReportResponse)
                    else response
                ),
                model="provider/model",
            )
        ],
        clock=lambda: CREATED_AT,
    ).execute(request)
    return request, result


def test_dynamic_review_response_requires_exact_criterion_assessments() -> None:
    assessment = ReviewCriterionAssessmentResponse(
        criterion_id="AC_LINKS",
        status="satisfied",
        adversarial_check="Ran the CLI against a missing local-link fixture.",
        evidence="The CLI returned non-zero and named the broken link.",
        tool_evidence=(review_tool_claim(),),
    )
    request, result = _review_result(
        ReviewReportResponse(
            verdict="accept",
            criterion_assessments=(assessment,),
            summary="The assigned criterion is satisfied.",
        )
    )

    parsed = parse_dynamic_agent_response(
        result,
        request,
        task_brief=task_brief(),
        team_plan=team_plan(),
        reviewed_criterion_ids=("AC_LINKS",),
    )

    assert isinstance(parsed.body, GroundedReviewReportResponse)
    assert parsed.body.criterion_assessments[0].criterion_id == assessment.criterion_id
    reference = parsed.body.criterion_assessments[0].tool_evidence[0]
    assert reference.execution_attempt == 1
    assert reference.tool_call_id == "tool-001"
    assert reference.observable == "scripted-review-observation"


def test_controller_resolves_observables_after_unrelated_preliminary_calls() -> None:
    helper_output = "created /tmp/sat-review-probe-boundary.py bytes=226"
    marker_output = "SAT_REVIEWER_HELPER_OK\nSAT_AGENT_WRITE_BLOCKED"
    assessment = ReviewCriterionAssessmentResponse(
        criterion_id="AC_LINKS",
        status="satisfied",
        adversarial_check="Exercised the missing-link boundary with a probe.",
        evidence="The helper and Python result established the behavior.",
        tool_evidence=(
            review_tool_claim(helper_output),
            review_tool_claim(marker_output),
        ),
    )
    request, result = _review_result(
        ReviewReportResponse(
            verdict="accept",
            criterion_assessments=(assessment,),
            summary="The assigned criterion is satisfied.",
        )
    )
    calls = tuple(
        captured_tool_call(index, output)
        for index, output in enumerate(
            (
                "initial listing",
                "helper availability",
                "source inspection",
                helper_output,
                "clean workspace",
                marker_output,
            ),
            start=1,
        )
    )
    result = result.model_copy(
        update={"telemetry": result.telemetry.model_copy(update={"tool_calls": calls})}
    )

    parsed = parse_dynamic_agent_response(
        result,
        request,
        task_brief=task_brief(),
        team_plan=team_plan(),
        reviewed_criterion_ids=("AC_LINKS",),
    )

    assert isinstance(parsed.body, GroundedReviewReportResponse)
    references = parsed.body.criterion_assessments[0].tool_evidence
    assert [reference.tool_call_id for reference in references] == [
        "tool-004",
        "tool-006",
    ]


def test_dynamic_review_response_rejects_an_unmatched_tool_claim() -> None:
    assessment = ReviewCriterionAssessmentResponse(
        criterion_id="AC_LINKS",
        status="satisfied",
        adversarial_check="Ran the CLI against a missing local-link fixture.",
        evidence="Claimed an observation that must be controller-bound.",
        tool_evidence=(review_tool_claim("fabricated observation"),),
    )
    request, result = _review_result(
        ReviewReportResponse(
            verdict="accept",
            criterion_assessments=(assessment,),
            summary="Claimed the assigned criterion is satisfied.",
        )
    )

    with pytest.raises(AgentArtifactResponseError, match="does not match any"):
        parse_dynamic_agent_response(
            result,
            request,
            task_brief=task_brief(),
            team_plan=team_plan(),
            reviewed_criterion_ids=("AC_LINKS",),
        )


def test_controller_matches_only_json_outside_string_whitespace_variants() -> None:
    assessment = ReviewCriterionAssessmentResponse(
        criterion_id="AC_LINKS",
        status="satisfied",
        adversarial_check="Exercised the keyed JSON boundary.",
        evidence="The captured result preserved the exact value and structure.",
        tool_evidence=(review_tool_claim('"result":"two words","paths":["a","b"]'),),
    )
    request, result = _review_result(
        ReviewReportResponse(
            verdict="accept",
            criterion_assessments=(assessment,),
            summary="The keyed JSON evidence is grounded.",
        )
    )
    pretty_output = """{
  "result": "two words",
  "paths": [
    "a",
    "b"
  ]
}"""
    result = result.model_copy(
        update={
            "telemetry": result.telemetry.model_copy(
                update={"tool_calls": (captured_tool_call(1, pretty_output),)}
            )
        }
    )

    parsed = parse_dynamic_agent_response(
        result,
        request,
        task_brief=task_brief(),
        team_plan=team_plan(),
        reviewed_criterion_ids=("AC_LINKS",),
    )

    assert isinstance(parsed.body, GroundedReviewReportResponse)
    reference = parsed.body.criterion_assessments[0].tool_evidence[0]
    assert reference.tool_call_id == "tool-001"
    assert reference.observable == '"result":"two words","paths":["a","b"]'

    for unsupported in (
        '"result":"twowords","paths":["a","b"]',
        '"result":"two words","paths":["b","a"]',
        "result: two words paths: a b",
    ):
        changed = assessment.model_copy(
            update={"tool_evidence": (review_tool_claim(unsupported),)}
        )
        _, changed_result = _review_result(
            ReviewReportResponse(
                verdict="accept",
                criterion_assessments=(changed,),
                summary="Claimed unsupported evidence.",
            )
        )
        changed_result = changed_result.model_copy(
            update={
                "telemetry": changed_result.telemetry.model_copy(
                    update={"tool_calls": (captured_tool_call(1, pretty_output),)}
                )
            }
        )
        with pytest.raises(AgentArtifactResponseError, match="does not match any"):
            parse_dynamic_agent_response(
                changed_result,
                request,
                task_brief=task_brief(),
                team_plan=team_plan(),
                reviewed_criterion_ids=("AC_LINKS",),
            )


def test_controller_binds_same_iteration_deterministic_command_output() -> None:
    assessment = ReviewCriterionAssessmentResponse(
        criterion_id="AC_LINKS",
        status="satisfied",
        adversarial_check="Checked the exact project start boundary.",
        evidence="The controller-owned exact-command gate reached a clean start.",
        tool_evidence=(review_tool_claim('"start":"exited_zero"'),),
    )
    request, result = _review_result(
        ReviewReportResponse(
            verdict="accept",
            criterion_assessments=(assessment,),
            summary="The same-iteration deterministic command is grounded.",
        )
    )
    result = result.model_copy(
        update={"telemetry": result.telemetry.model_copy(update={"tool_calls": ()})}
    )
    command = CommandEvidence(
        id="CHECK_EXACT_PROJECT_COMMANDS",
        argv=("python", "run_commands.py"),
        criterion_ids=("AC_OTHER",),
        exit_code=0,
        duration_ms=12,
        stdout_path="iterations/01/commands/exact.stdout.txt",
        stderr_path="iterations/01/commands/exact.stderr.txt",
        stdout_tail='{"setup": "passed", "start": "exited_zero"}',
        summary="The exact project commands passed.",
    )

    parsed = parse_dynamic_agent_response(
        result,
        request,
        task_brief=task_brief(),
        team_plan=team_plan(),
        reviewed_criterion_ids=("AC_LINKS",),
        review_command_evidence=(command,),
    )

    assert isinstance(parsed.body, GroundedReviewReportResponse)
    grounded = parsed.body.criterion_assessments[0]
    assert grounded.command_evidence_ids == ("CHECK_EXACT_PROJECT_COMMANDS",)
    assert grounded.tool_evidence == ()
    assert grounded.model_dump(mode="json")["command_evidence_ids"] == [
        "CHECK_EXACT_PROJECT_COMMANDS"
    ]

    unrelated = command.model_copy(
        update={"stdout_tail": '{"setup": "passed", "start": "failed"}'}
    )
    with pytest.raises(AgentArtifactResponseError, match="does not match any"):
        parse_dynamic_agent_response(
            result,
            request,
            task_brief=task_brief(),
            team_plan=team_plan(),
            reviewed_criterion_ids=("AC_LINKS",),
            review_command_evidence=(unrelated,),
        )


def test_dynamic_review_response_binds_every_matching_tool_result() -> None:
    assessment = ReviewCriterionAssessmentResponse(
        criterion_id="AC_LINKS",
        status="satisfied",
        adversarial_check="Ran two probes with an indistinguishable result.",
        evidence="The controller must preserve every matching result.",
        tool_evidence=(review_tool_claim(),),
    )
    request, result = _review_result(
        ReviewReportResponse(
            verdict="accept",
            criterion_assessments=(assessment,),
            summary="Claimed the assigned criterion is satisfied.",
        )
    )
    first = result.telemetry.tool_calls[0]
    duplicate = first.model_copy(
        update={
            "id": "tool-002",
            "external_call_sha256": "e" * 64,
        }
    )
    result = result.model_copy(
        update={
            "telemetry": result.telemetry.model_copy(
                update={"tool_calls": (first, duplicate)}
            )
        }
    )

    parsed = parse_dynamic_agent_response(
        result,
        request,
        task_brief=task_brief(),
        team_plan=team_plan(),
        reviewed_criterion_ids=("AC_LINKS",),
    )

    assert isinstance(parsed.body, GroundedReviewReportResponse)
    references = parsed.body.criterion_assessments[0].tool_evidence
    assert [reference.tool_call_id for reference in references] == [
        "tool-001",
        "tool-002",
    ]
    assert {reference.observable for reference in references} == {
        "scripted-review-observation"
    }


def test_satisfied_review_rejects_a_matching_failed_tool_result() -> None:
    assessment = ReviewCriterionAssessmentResponse(
        criterion_id="AC_LINKS",
        status="satisfied",
        adversarial_check="Ran a boundary probe whose command failed overall.",
        evidence="A passing-looking substring cannot override the command result.",
        tool_evidence=(review_tool_claim("partial check looked good"),),
    )
    request, result = _review_result(
        ReviewReportResponse(
            verdict="accept",
            criterion_assessments=(assessment,),
            summary="Claimed success from a failed result.",
        )
    )
    result = result.model_copy(
        update={
            "telemetry": result.telemetry.model_copy(
                update={
                    "tool_calls": (
                        captured_tool_call(
                            1,
                            "partial check looked good\ncommand failed",
                            executable="python",
                            failed=True,
                        ),
                    )
                }
            )
        }
    )

    with pytest.raises(AgentArtifactResponseError, match="overall failed tool"):
        parse_dynamic_agent_response(
            result,
            request,
            task_brief=task_brief(),
            team_plan=team_plan(),
            reviewed_criterion_ids=("AC_LINKS",),
        )


def test_controller_downgrades_unsafe_positive_evidence_in_existing_revision() -> None:
    """A useful revise report must not be lost to one unsafe positive claim."""

    brief = task_brief().model_copy(
        update={
            "acceptance_criteria": [
                *task_brief().acceptance_criteria,
                AcceptanceCriterion(
                    id="AC_JSON",
                    description="Duplicate groups are reported as structured JSON.",
                    verification="Run the CLI against a duplicate-group fixture.",
                ),
            ]
        }
    )
    blocked = ReviewCriterionAssessmentResponse(
        criterion_id="AC_LINKS",
        status="blocked",
        adversarial_check="Exercised the configured exclusion pattern.",
        evidence="The direct probe preserved the counterexample.",
        tool_evidence=(review_tool_claim("EXCLUDE_PATTERN_FAILED"),),
    )
    unsafe_positive = ReviewCriterionAssessmentResponse(
        criterion_id="AC_JSON",
        status="satisfied",
        adversarial_check="Exercised duplicate JSON grouping.",
        evidence="Claimed a marker emitted before a later assertion failed.",
        tool_evidence=(review_tool_claim("JSON_GROUP_DIGEST_OK"),),
    )
    request, result = _review_result(
        ReviewReportResponse(
            verdict="revise",
            criterion_assessments=(blocked, unsafe_positive),
            findings=(
                ReviewFinding(
                    id="FINDING_EXCLUDE_PATTERN",
                    severity=ReviewSeverity.HIGH,
                    blocking=True,
                    category="correctness",
                    description="Configured wildcard exclusions are not applied.",
                    recommendation="Apply wildcard matching before grouping paths.",
                    criterion_ids=("AC_LINKS",),
                ),
            ),
            summary="The implementation requires revision for a confirmed defect.",
        )
    )
    result = result.model_copy(
        update={
            "telemetry": result.telemetry.model_copy(
                update={
                    "tool_calls": (
                        captured_tool_call(
                            1,
                            framed_probe_output(
                                stdout="EXCLUDE_PATTERN_FAILED",
                                exit_code=1,
                            ),
                            executable="sat-probe-run",
                            failed=True,
                        ),
                        captured_tool_call(
                            2,
                            framed_probe_output(
                                stdout="JSON_GROUP_DIGEST_OK",
                                exit_code=1,
                            ),
                            executable="sat-probe-run",
                            failed=True,
                        ),
                    )
                }
            )
        }
    )

    parsed = parse_dynamic_agent_response(
        result,
        request,
        task_brief=brief,
        team_plan=team_plan(brief),
        reviewed_criterion_ids=("AC_LINKS", "AC_JSON"),
    )

    assert isinstance(parsed.body, GroundedReviewReportResponse)
    assert parsed.body.verdict.value == "revise"
    assessments = {
        item.criterion_id: item for item in parsed.body.criterion_assessments
    }
    assert assessments["AC_LINKS"].status.value == "blocked"
    assert assessments["AC_JSON"].status.value == "blocked"
    assert assessments["AC_JSON"].tool_evidence[0].tool_call_id == "tool-002"
    assert "controller downgraded" in assessments["AC_JSON"].evidence.casefold()
    findings = {finding.id: finding for finding in parsed.body.findings}
    generated = findings["FINDING_UNVERIFIED_REVIEW_EVIDENCE_AC_JSON"]
    assert generated.blocking is True
    assert generated.criterion_ids == ("AC_JSON",)
    assert '"status":"satisfied"' in result.response_text


@pytest.mark.parametrize(
    ("executable", "output", "error"),
    (
        ("cd", "expected paths found\nEXIT=1", "overall failed tool"),
        (
            "sat-probe-run",
            framed_probe_output(stdout="expected paths found", exit_code=1),
            "overall failed tool",
        ),
        (
            "sat-probe-run",
            framed_probe_output(
                stdout="expected paths found",
                exit_code=0,
                timed_out=True,
            ),
            "overall failed tool",
        ),
        (
            "sat-probe-run",
            "SAT_PROBE_STDOUT_BEGIN\nexpected paths found\n"
            "SAT_PROBE_STDOUT_END\nSAT_PROBE_STDERR_BEGIN\n"
            "SAT_PROBE_STDERR_END",
            "protocol-eligible child stdout",
        ),
    ),
)
def test_satisfied_review_rejects_shell_masked_or_invalid_probe_failure(
    executable: str,
    output: str,
    error: str,
) -> None:
    assessment = ReviewCriterionAssessmentResponse(
        criterion_id="AC_LINKS",
        status="satisfied",
        adversarial_check="Exercised an absolute behavior boundary.",
        evidence="Claimed one positive fragment from an overall failed probe.",
        tool_evidence=(review_tool_claim("expected paths found"),),
    )
    request, result = _review_result(
        ReviewReportResponse(
            verdict="accept",
            criterion_assessments=(assessment,),
            summary="Claimed the failed probe passed.",
        )
    )
    result = result.model_copy(
        update={
            "telemetry": result.telemetry.model_copy(
                update={
                    "tool_calls": (
                        captured_tool_call(1, output, executable=executable),
                    )
                }
            )
        }
    )

    with pytest.raises(AgentArtifactResponseError, match=error):
        parse_dynamic_agent_response(
            result,
            request,
            task_brief=task_brief(),
            team_plan=team_plan(),
            reviewed_criterion_ids=("AC_LINKS",),
        )


def test_satisfied_review_accepts_a_successful_probe_terminal_marker() -> None:
    assessment = ReviewCriterionAssessmentResponse(
        criterion_id="AC_LINKS",
        status="satisfied",
        adversarial_check="Exercised the broken-link boundary with assertions.",
        evidence="The bounded probe completed with its authoritative marker.",
        tool_evidence=(review_tool_claim("BROKEN_LINK_BOUNDARY_OK"),),
    )
    request, result = _review_result(
        ReviewReportResponse(
            verdict="accept",
            criterion_assessments=(assessment,),
            summary="The criterion has successful attributable evidence.",
        )
    )
    output = framed_probe_output(stdout="BROKEN_LINK_BOUNDARY_OK")
    result = result.model_copy(
        update={
            "telemetry": result.telemetry.model_copy(
                update={
                    "tool_calls": (
                        captured_tool_call(1, output, executable="sat-probe-run"),
                    )
                }
            )
        }
    )

    parsed = parse_dynamic_agent_response(
        result,
        request,
        task_brief=task_brief(),
        team_plan=team_plan(),
        reviewed_criterion_ids=("AC_LINKS",),
    )

    assert isinstance(parsed.body, GroundedReviewReportResponse)
    assert parsed.body.criterion_assessments[0].tool_evidence[0].tool_call_id == (
        "tool-001"
    )


def test_satisfied_review_rejects_unframed_direct_probe_output() -> None:
    assessment = ReviewCriterionAssessmentResponse(
        criterion_id="AC_LINKS",
        status="satisfied",
        adversarial_check="Claimed a direct probe without stream framing.",
        evidence="Historical unframed output is not current positive evidence.",
        tool_evidence=(review_tool_claim("UNFRAMED_PROBE_OK"),),
    )
    request, result = _review_result(
        ReviewReportResponse(
            verdict="accept",
            criterion_assessments=(assessment,),
            summary="Claimed an output outside the current probe protocol.",
        )
    )
    output = 'UNFRAMED_PROBE_OK\nSAT_PROBE_RESULT_V1 {"exit_code":0,"timed_out":false}'
    result = result.model_copy(
        update={
            "telemetry": result.telemetry.model_copy(
                update={
                    "tool_calls": (
                        captured_tool_call(1, output, executable="sat-probe-run"),
                    )
                }
            )
        }
    )

    with pytest.raises(
        AgentArtifactResponseError,
        match="protocol-eligible child stdout",
    ):
        parse_dynamic_agent_response(
            result,
            request,
            task_brief=task_brief(),
            team_plan=team_plan(),
            reviewed_criterion_ids=("AC_LINKS",),
        )


def test_satisfied_review_uses_successful_probe_emission_after_failed_attempts() -> (
    None
):
    """A traceback source line must not poison a later successful direct probe."""

    marker = "NESTED_DUPLICATE_OK"
    assessment = ReviewCriterionAssessmentResponse(
        criterion_id="AC_LINKS",
        status="satisfied",
        adversarial_check="Exercised nested duplicate handling with assertions.",
        evidence="The final direct probe emitted the marker after all assertions.",
        tool_evidence=(review_tool_claim(marker),),
    )
    request, result = _review_result(
        ReviewReportResponse(
            verdict="accept",
            criterion_assessments=(assessment,),
            summary="The successful direct probe establishes the criterion.",
        )
    )
    prior_traceback = captured_tool_call(
        1,
        framed_probe_output(
            stderr=(
                "Traceback (most recent call last):\n"
                f'  File "/tmp/probe.py", line 8, in <module>\n'
                f'    print("{marker}")\nAssertionError'
            ),
            exit_code=1,
        ),
        executable="sat-probe-run",
        failed=True,
    )
    same_attempt_failure = captured_tool_call(
        3,
        framed_probe_output(stdout=marker, exit_code=1),
        executable="sat-probe-run",
        failed=True,
    )
    successful = captured_tool_call(
        5,
        framed_probe_output(stdout=marker),
        executable="sat-probe-run",
    )
    current_calls = (same_attempt_failure, successful)
    result = result.model_copy(
        update={
            "telemetry": result.telemetry.model_copy(
                update={"tool_calls": current_calls}
            )
        }
    )

    parsed = parse_dynamic_agent_response(
        result,
        request,
        task_brief=task_brief(),
        team_plan=team_plan(),
        reviewed_criterion_ids=("AC_LINKS",),
        review_tool_evidence_attempts=(
            ReviewToolEvidenceAttempt(
                execution_attempt=1,
                tool_calls=(prior_traceback,),
            ),
            ReviewToolEvidenceAttempt(
                execution_attempt=2,
                tool_calls=current_calls,
            ),
        ),
    )

    assert isinstance(parsed.body, GroundedReviewReportResponse)
    references = parsed.body.criterion_assessments[0].tool_evidence
    assert [(item.execution_attempt, item.tool_call_id) for item in references] == [
        (2, "tool-005")
    ]


def test_satisfied_review_rejects_a_fragment_seen_only_in_probe_stderr() -> None:
    marker = "TRACEBACK_SOURCE_ONLY"
    assessment = ReviewCriterionAssessmentResponse(
        criterion_id="AC_LINKS",
        status="satisfied",
        adversarial_check="Attempted to exercise a boundary.",
        evidence="Mistook a traceback source line for a successful emission.",
        tool_evidence=(review_tool_claim(marker),),
    )
    request, result = _review_result(
        ReviewReportResponse(
            verdict="accept",
            criterion_assessments=(assessment,),
            summary="Claimed evidence that was never emitted on child stdout.",
        )
    )
    call = captured_tool_call(
        1,
        framed_probe_output(
            stderr=f'  print("{marker}")\nAssertionError',
            exit_code=1,
        ),
        executable="sat-probe-run",
        failed=True,
    )
    result = result.model_copy(
        update={
            "telemetry": result.telemetry.model_copy(update={"tool_calls": (call,)})
        }
    )

    with pytest.raises(AgentArtifactResponseError, match="child stdout"):
        parse_dynamic_agent_response(
            result,
            request,
            task_brief=task_brief(),
            team_plan=team_plan(),
            reviewed_criterion_ids=("AC_LINKS",),
        )


def test_blocked_review_may_ground_a_counterexample_from_probe_stderr() -> None:
    marker = "BROKEN_LINK_COUNTEREXAMPLE"
    assessment = ReviewCriterionAssessmentResponse(
        criterion_id="AC_LINKS",
        status="blocked",
        adversarial_check="Ran a negative fixture that disproved the criterion.",
        evidence="The failed assertion preserved the concrete counterexample.",
        tool_evidence=(review_tool_claim(marker),),
    )
    request, result = _review_result(
        ReviewReportResponse(
            verdict="revise",
            criterion_assessments=(assessment,),
            findings=(
                ReviewFinding(
                    id="FINDING_COUNTEREXAMPLE",
                    severity=ReviewSeverity.HIGH,
                    blocking=True,
                    category="correctness",
                    description="A broken-link fixture returned a passing status.",
                    recommendation="Return a non-zero status for the fixture.",
                    criterion_ids=("AC_LINKS",),
                ),
            ),
            summary="A grounded counterexample requires revision.",
        )
    )
    call = captured_tool_call(
        1,
        framed_probe_output(
            stderr=f"AssertionError: {marker}",
            exit_code=1,
        ),
        executable="sat-probe-run",
        failed=True,
    )
    result = result.model_copy(
        update={
            "telemetry": result.telemetry.model_copy(update={"tool_calls": (call,)})
        }
    )

    parsed = parse_dynamic_agent_response(
        result,
        request,
        task_brief=task_brief(),
        team_plan=team_plan(),
        reviewed_criterion_ids=("AC_LINKS",),
    )

    assert isinstance(parsed.body, GroundedReviewReportResponse)
    reference = parsed.body.criterion_assessments[0].tool_evidence[0]
    assert reference.tool_call_id == "tool-001"


def boundary_checks(
    boundaries: tuple[ReviewBoundaryKind, ...] = tuple(ReviewBoundaryKind),
) -> tuple[ReviewBoundaryCheckResponse, ...]:
    """Return distinct model-visible evidence for approved entry boundaries."""

    return tuple(
        ReviewBoundaryCheckResponse(
            boundary=boundary,
            adversarial_check=f"Challenged {boundary.value} with a focused fixture.",
            tool_evidence=(review_tool_claim(f"BOUNDARY_{index}_OK"),),
        )
        for index, boundary in enumerate(boundaries, start=1)
    )


def test_review_boundary_checks_allow_controller_command_only_grounding() -> None:
    brief = boundary_task_brief()
    plan = team_plan(brief)
    assessment = ReviewCriterionAssessmentResponse(
        criterion_id="AC_LINKS",
        status="satisfied",
        adversarial_check="Exercised every approved boundary with trusted commands.",
        evidence="The same-iteration command emitted each distinct boundary marker.",
        tool_evidence=(review_tool_claim("GENERAL_COMMAND_OK"),),
        boundary_checks=boundary_checks(),
    )
    request, result = _review_result(
        ReviewReportResponse(
            verdict="accept",
            criterion_assessments=(assessment,),
            summary="Every boundary is grounded in controller command evidence.",
        )
    )
    result = result.model_copy(
        update={"telemetry": result.telemetry.model_copy(update={"tool_calls": ()})}
    )
    command = CommandEvidence(
        id="CHECK_EXACT_PROJECT_COMMANDS",
        argv=("python", "run_commands.py"),
        exit_code=0,
        duration_ms=12,
        stdout_path="iterations/01/commands/exact.stdout.txt",
        stderr_path="iterations/01/commands/exact.stderr.txt",
        stdout_tail=(
            "GENERAL_COMMAND_OK\n"
            "BOUNDARY_1_OK\nBOUNDARY_2_OK\n"
            "BOUNDARY_3_OK\nBOUNDARY_4_OK\n"
        ),
        summary="The exact project commands passed.",
    )

    parsed = parse_dynamic_agent_response(
        result,
        request,
        task_brief=brief,
        team_plan=plan,
        reviewed_criterion_ids=("AC_LINKS",),
        review_command_evidence=(command,),
    )

    assert isinstance(parsed.body, GroundedReviewReportResponse)
    grounded = parsed.body.criterion_assessments[0]
    assert grounded.command_evidence_ids == ("CHECK_EXACT_PROJECT_COMMANDS",)
    assert grounded.tool_evidence == ()
    assert len(grounded.boundary_checks) == 4
    assert all(
        check.command_evidence_ids == ("CHECK_EXACT_PROJECT_COMMANDS",)
        and check.tool_evidence == ()
        for check in grounded.boundary_checks
    )


def test_satisfied_review_requires_every_approved_boundary_with_distinct_evidence() -> (
    None
):
    brief = boundary_task_brief()
    plan = team_plan(brief)
    assessment = ReviewCriterionAssessmentResponse(
        criterion_id="AC_LINKS",
        status="satisfied",
        adversarial_check="Challenged every approved link entry boundary.",
        evidence="All four assertion markers and the runner result were successful.",
        tool_evidence=(review_tool_claim("GENERAL_BOUNDARY_PROBE_OK"),),
        boundary_checks=boundary_checks(),
    )
    request, result = _review_result(
        ReviewReportResponse(
            verdict="accept",
            criterion_assessments=(assessment,),
            summary="Every approved boundary has attributable evidence.",
        )
    )
    output = framed_probe_output(
        stdout=(
            "GENERAL_BOUNDARY_PROBE_OK\n"
            "BOUNDARY_1_OK\nBOUNDARY_2_OK\nBOUNDARY_3_OK\nBOUNDARY_4_OK"
        )
    )
    result = result.model_copy(
        update={
            "telemetry": result.telemetry.model_copy(
                update={
                    "tool_calls": (
                        captured_tool_call(1, output, executable="sat-probe-run"),
                    )
                }
            )
        }
    )

    parsed = parse_dynamic_agent_response(
        result,
        request,
        task_brief=brief,
        team_plan=plan,
        reviewed_criterion_ids=("AC_LINKS",),
    )

    assert isinstance(parsed.body, GroundedReviewReportResponse)
    grounded = parsed.body.criterion_assessments[0]
    assert tuple(check.boundary for check in grounded.boundary_checks) == tuple(
        ReviewBoundaryKind
    )
    assert all(
        check.tool_evidence[0].tool_call_id == "tool-001"
        for check in grounded.boundary_checks
    )

    missing = assessment.model_copy(
        update={"boundary_checks": boundary_checks(tuple(ReviewBoundaryKind)[:-1])}
    )
    _, missing_result = _review_result(
        ReviewReportResponse(
            verdict="accept",
            criterion_assessments=(missing,),
            summary="Claimed incomplete boundary coverage.",
        )
    )
    missing_result = missing_result.model_copy(
        update={
            "telemetry": missing_result.telemetry.model_copy(
                update={
                    "tool_calls": (
                        captured_tool_call(1, output, executable="sat-probe-run"),
                    )
                }
            )
        }
    )
    with pytest.raises(AgentArtifactResponseError, match="must check every"):
        parse_dynamic_agent_response(
            missing_result,
            request,
            task_brief=brief,
            team_plan=plan,
            reviewed_criterion_ids=("AC_LINKS",),
        )


def test_review_strips_unapproved_and_rejects_duplicate_approved_boundaries() -> None:
    duplicate = (
        ReviewBoundaryCheckResponse(
            boundary=ReviewBoundaryKind.TOP_LEVEL_INPUT,
            adversarial_check="Checked the direct input.",
            tool_evidence=(review_tool_claim("SAME_MARKER"),),
        ),
        ReviewBoundaryCheckResponse(
            boundary=ReviewBoundaryKind.NESTED_INPUT,
            adversarial_check="Checked nested input.",
            tool_evidence=(review_tool_claim("SAME_MARKER"),),
        ),
    )
    with pytest.raises(ValidationError, match="distinct evidence fragments"):
        ReviewCriterionAssessmentResponse(
            criterion_id="AC_LINKS",
            status="satisfied",
            adversarial_check="Claimed two boundaries.",
            evidence="Reused one ambiguous marker.",
            tool_evidence=(review_tool_claim(),),
            boundary_checks=duplicate,
        )

    assessment = ReviewCriterionAssessmentResponse(
        criterion_id="AC_LINKS",
        status="satisfied",
        adversarial_check="Added a boundary absent from the approved brief.",
        evidence="The model cannot expand its own Review obligations.",
        tool_evidence=(review_tool_claim("GENERAL_OK"),),
        boundary_checks=boundary_checks((ReviewBoundaryKind.TOP_LEVEL_INPUT,)),
    )
    request, result = _review_result(
        ReviewReportResponse(
            verdict="accept",
            criterion_assessments=(assessment,),
            summary="Claimed an unapproved boundary.",
        )
    )
    result = result.model_copy(
        update={
            "telemetry": result.telemetry.model_copy(
                update={
                    "tool_calls": (
                        captured_tool_call(
                            1,
                            "GENERAL_OK\nBOUNDARY_1_OK",
                            executable="python",
                        ),
                    )
                }
            )
        }
    )

    parsed = parse_dynamic_agent_response(
        result,
        request,
        task_brief=task_brief(),
        team_plan=team_plan(),
        reviewed_criterion_ids=("AC_LINKS",),
    )

    assert isinstance(parsed.body, GroundedReviewReportResponse)
    assert parsed.body.criterion_assessments[0].boundary_checks == ()
    assert parsed.response_normalizations == (
        "removed 1 unapproved boundary_checks from criterion AC_LINKS (approved: none)",
    )

    brief = task_brief()
    criterion = brief.acceptance_criteria[0].model_copy(
        update={
            "review_boundaries": (
                ReviewBoundaryKind.TOP_LEVEL_INPUT,
                ReviewBoundaryKind.NESTED_INPUT,
            )
        }
    )
    brief = brief.model_copy(update={"acceptance_criteria": [criterion]})
    raw_payload = ReviewReportResponse(
        verdict="accept",
        criterion_assessments=(
            ReviewCriterionAssessmentResponse(
                criterion_id="AC_LINKS",
                status="satisfied",
                adversarial_check="Claimed complete approved boundary coverage.",
                evidence="Reused one ambiguous marker.",
                tool_evidence=(review_tool_claim("GENERAL_OK"),),
            ),
        ),
        summary="Claimed duplicate evidence for approved boundaries.",
    ).model_dump(mode="json")
    raw_payload["criterion_assessments"][0]["boundary_checks"] = [
        check.model_dump(mode="json") for check in duplicate
    ]
    request, result = _review_result(json.dumps(raw_payload))

    with pytest.raises(AgentArtifactResponseError, match="distinct evidence fragments"):
        parse_dynamic_agent_response(
            result,
            request,
            task_brief=brief,
            team_plan=team_plan(brief),
            reviewed_criterion_ids=("AC_LINKS",),
        )


def test_review_strips_duplicate_boundary_content_when_scope_is_empty() -> None:
    raw_payload = ReviewReportResponse(
        verdict="accept",
        criterion_assessments=(
            ReviewCriterionAssessmentResponse(
                criterion_id="AC_LINKS",
                status="satisfied",
                adversarial_check="Verified the approved criterion.",
                evidence="The general result grounds the approved criterion.",
                tool_evidence=(review_tool_claim("GENERAL_OK"),),
            ),
        ),
        summary="The approved criterion is satisfied.",
    ).model_dump(mode="json")
    raw_payload["criterion_assessments"][0]["boundary_checks"] = [
        {
            "boundary": "top_level_input",
            "adversarial_check": "Added an unapproved direct-input check.",
            "tool_evidence": [{"observable": "REPEATED_MARKER"}],
        },
        {
            "boundary": "nested_input",
            "adversarial_check": "Added an unapproved nested-input check.",
            "tool_evidence": [{"observable": "REPEATED_MARKER"}],
        },
    ]
    request, result = _review_result(json.dumps(raw_payload))
    result = result.model_copy(
        update={
            "telemetry": result.telemetry.model_copy(
                update={
                    "tool_calls": (
                        captured_tool_call(1, "GENERAL_OK", executable="python"),
                    )
                }
            )
        }
    )

    parsed = parse_dynamic_agent_response(
        result,
        request,
        task_brief=task_brief(),
        team_plan=team_plan(),
        reviewed_criterion_ids=("AC_LINKS",),
    )

    assert isinstance(parsed.body, GroundedReviewReportResponse)
    assert parsed.body.criterion_assessments[0].boundary_checks == ()
    assert parsed.response_normalizations == (
        "removed 2 unapproved boundary_checks from criterion AC_LINKS (approved: none)",
    )


def test_blocked_absolute_criterion_may_stop_after_one_grounded_counterexample() -> (
    None
):
    brief = boundary_task_brief()
    plan = team_plan(brief)
    counterexample = ReviewBoundaryCheckResponse(
        boundary=ReviewBoundaryKind.TOP_LEVEL_INPUT,
        adversarial_check="Used a top-level alias that bypassed the prohibition.",
        tool_evidence=(review_tool_claim("TOP_LEVEL_COUNTEREXAMPLE"),),
    )
    assessment = ReviewCriterionAssessmentResponse(
        criterion_id="AC_LINKS",
        status="blocked",
        adversarial_check="The first required boundary disproved the absolute claim.",
        evidence="The runner preserved the failing assertion and counterexample.",
        tool_evidence=(review_tool_claim("TOP_LEVEL_COUNTEREXAMPLE"),),
        boundary_checks=(counterexample,),
    )
    request, result = _review_result(
        ReviewReportResponse(
            verdict="revise",
            criterion_assessments=(assessment,),
            findings=(
                ReviewFinding(
                    id="FINDING_TOP_LEVEL",
                    severity=ReviewSeverity.HIGH,
                    blocking=True,
                    category="correctness",
                    description="A top-level alias bypasses the absolute guarantee.",
                    recommendation="Reject or resolve the alias before traversal.",
                    criterion_ids=("AC_LINKS",),
                ),
            ),
            summary="One grounded boundary counterexample requires revision.",
        )
    )
    output = (
        "TOP_LEVEL_COUNTEREXAMPLE\n"
        'SAT_PROBE_RESULT_V1 {"exit_code":1,"timed_out":false}'
    )
    result = result.model_copy(
        update={
            "telemetry": result.telemetry.model_copy(
                update={
                    "tool_calls": (
                        captured_tool_call(
                            1,
                            output,
                            executable="sat-probe-run",
                            failed=True,
                        ),
                    )
                }
            )
        }
    )

    parsed = parse_dynamic_agent_response(
        result,
        request,
        task_brief=brief,
        team_plan=plan,
        reviewed_criterion_ids=("AC_LINKS",),
    )

    assert isinstance(parsed.body, GroundedReviewReportResponse)
    assert parsed.body.verdict.value == "revise"
    assert parsed.body.criterion_assessments[0].boundary_checks[0].boundary is (
        ReviewBoundaryKind.TOP_LEVEL_INPUT
    )


def test_satisfied_review_rejects_matching_failed_deterministic_command() -> None:
    assessment = ReviewCriterionAssessmentResponse(
        criterion_id="AC_LINKS",
        status="satisfied",
        adversarial_check="Checked the controller command result.",
        evidence="Claimed a partial line from a failed command.",
        tool_evidence=(review_tool_claim("one assertion passed"),),
    )
    request, result = _review_result(
        ReviewReportResponse(
            verdict="accept",
            criterion_assessments=(assessment,),
            summary="Claimed a failed deterministic command passed.",
        )
    )
    result = result.model_copy(
        update={"telemetry": result.telemetry.model_copy(update={"tool_calls": ()})}
    )
    command = CommandEvidence(
        id="CHECK_PROJECT_TESTS",
        argv=("python", "-m", "pytest"),
        exit_code=1,
        duration_ms=12,
        stdout_path="iterations/01/commands/tests.stdout.txt",
        stderr_path="iterations/01/commands/tests.stderr.txt",
        stdout_tail="one assertion passed\none assertion failed",
        summary="The project test command failed.",
    )

    with pytest.raises(AgentArtifactResponseError, match="failed or timed-out"):
        parse_dynamic_agent_response(
            result,
            request,
            task_brief=task_brief(),
            team_plan=team_plan(),
            reviewed_criterion_ids=("AC_LINKS",),
            review_command_evidence=(command,),
        )


def test_controller_grounds_a_repair_against_prior_attempt_evidence() -> None:
    assessment = ReviewCriterionAssessmentResponse(
        criterion_id="AC_LINKS",
        status="satisfied",
        adversarial_check="Ran the CLI against a missing local-link fixture.",
        evidence="The prior attempt observed the expected failure.",
        tool_evidence=(review_tool_claim(),),
    )
    request, result = _review_result(
        ReviewReportResponse(
            verdict="accept",
            criterion_assessments=(assessment,),
            summary="The repaired response preserves captured evidence.",
        )
    )
    prior_calls = result.telemetry.tool_calls
    result = result.model_copy(
        update={"telemetry": result.telemetry.model_copy(update={"tool_calls": ()})}
    )

    parsed = parse_dynamic_agent_response(
        result,
        request,
        task_brief=task_brief(),
        team_plan=team_plan(),
        reviewed_criterion_ids=("AC_LINKS",),
        review_tool_evidence_attempts=(
            ReviewToolEvidenceAttempt(
                execution_attempt=1,
                tool_calls=prior_calls,
            ),
            ReviewToolEvidenceAttempt(
                execution_attempt=2,
                tool_calls=(),
            ),
        ),
    )

    assert isinstance(parsed.body, GroundedReviewReportResponse)
    reference = parsed.body.criterion_assessments[0].tool_evidence[0]
    assert reference.execution_attempt == 1
    assert reference.tool_call_id == "tool-001"


def test_controller_rejects_a_review_chain_not_ending_at_current_attempt() -> None:
    assessment = ReviewCriterionAssessmentResponse(
        criterion_id="AC_LINKS",
        status="satisfied",
        adversarial_check="Ran the CLI against a missing local-link fixture.",
        evidence="Claimed prior evidence from a mismatched chain.",
        tool_evidence=(review_tool_claim(),),
    )
    request, result = _review_result(
        ReviewReportResponse(
            verdict="accept",
            criterion_assessments=(assessment,),
            summary="Claimed evidence from a mismatched chain.",
        )
    )

    with pytest.raises(AgentArtifactResponseError, match="does not end"):
        parse_dynamic_agent_response(
            result,
            request,
            task_brief=task_brief(),
            team_plan=team_plan(),
            reviewed_criterion_ids=("AC_LINKS",),
            review_tool_evidence_attempts=(
                ReviewToolEvidenceAttempt(
                    execution_attempt=1,
                    tool_calls=(),
                ),
            ),
        )


def test_dynamic_review_response_deduplicates_repeated_and_overlapping_selectors() -> (
    None
):
    assessment = ReviewCriterionAssessmentResponse(
        criterion_id="AC_LINKS",
        status="satisfied",
        adversarial_check="Ran one missing-link probe.",
        evidence="Repeated fragments still ground one result reference.",
        tool_evidence=(
            review_tool_claim("scripted-review-observation"),
            review_tool_claim("scripted-review-observation"),
            review_tool_claim("review-observation"),
        ),
    )
    request, result = _review_result(
        ReviewReportResponse(
            verdict="accept",
            criterion_assessments=(assessment,),
            summary="Claimed the assigned criterion is satisfied.",
        )
    )

    parsed = parse_dynamic_agent_response(
        result,
        request,
        task_brief=task_brief(),
        team_plan=team_plan(),
        reviewed_criterion_ids=("AC_LINKS",),
    )

    assert isinstance(parsed.body, GroundedReviewReportResponse)
    references = parsed.body.criterion_assessments[0].tool_evidence
    assert len(references) == 1
    reference = references[0]
    assert reference.tool_call_id == "tool-001"
    assert reference.observable == "scripted-review-observation"


def test_dynamic_review_response_structurally_requires_tool_evidence() -> None:
    response = {
        "verdict": "accept",
        "termination_reason": None,
        "criterion_assessments": [
            {
                "criterion_id": "AC_LINKS",
                "status": "satisfied",
                "adversarial_check": "Claimed to run a missing-link probe.",
                "evidence": "Claimed the CLI returned non-zero.",
            }
        ],
        "findings": [],
        "summary": "Claimed the assigned criterion is satisfied.",
    }
    request, result = _review_result(json.dumps(response))

    with pytest.raises(AgentArtifactResponseError, match="tool_evidence"):
        parse_dynamic_agent_response(
            result,
            request,
            task_brief=task_brief(),
            team_plan=team_plan(),
            reviewed_criterion_ids=("AC_LINKS",),
        )


def test_dynamic_review_response_rejects_a_model_supplied_controller_tool_id() -> None:
    response = {
        "verdict": "accept",
        "termination_reason": None,
        "criterion_assessments": [
            {
                "criterion_id": "AC_LINKS",
                "status": "satisfied",
                "adversarial_check": "Ran a missing-link probe.",
                "evidence": "Observed the expected non-zero result.",
                "tool_evidence": [
                    {
                        "tool_call_id": "tool-001",
                        "observable": "scripted-review-observation",
                    }
                ],
            }
        ],
        "findings": [],
        "summary": "Claimed the assigned criterion is satisfied.",
    }
    request, result = _review_result(json.dumps(response))

    with pytest.raises(
        AgentArtifactResponseError,
        match="tool_call_id",
    ) as captured:
        parse_dynamic_agent_response(
            result,
            request,
            task_brief=task_brief(),
            team_plan=team_plan(),
            reviewed_criterion_ids=("AC_LINKS",),
        )

    diagnostic = captured.value.diagnostic
    assert diagnostic is not None
    assert diagnostic.correction_paths == ()
    assert diagnostic.issues[0].authority is ResponseIssueAuthority.CONTROLLER


def test_dynamic_review_response_rejects_a_citation_when_no_tool_was_called() -> None:
    assessment = ReviewCriterionAssessmentResponse(
        criterion_id="AC_LINKS",
        status="satisfied",
        adversarial_check="Claimed to run a missing-link probe.",
        evidence="Claimed the CLI returned non-zero.",
        tool_evidence=(review_tool_claim(),),
    )
    request, result = _review_result(
        ReviewReportResponse(
            verdict="accept",
            criterion_assessments=(assessment,),
            summary="Claimed the assigned criterion is satisfied.",
        )
    )
    result = result.model_copy(
        update={"telemetry": result.telemetry.model_copy(update={"tool_calls": ()})}
    )

    with pytest.raises(AgentArtifactResponseError, match="does not match any"):
        parse_dynamic_agent_response(
            result,
            request,
            task_brief=task_brief(),
            team_plan=team_plan(),
            reviewed_criterion_ids=("AC_LINKS",),
        )


def test_dynamic_review_response_rejects_missing_or_unscoped_evidence() -> None:
    request, result = _review_result(
        ReviewReportResponse(
            verdict="accept",
            summary="Claimed the assigned criterion is satisfied.",
        )
    )
    with pytest.raises(AgentArtifactResponseError, match="exactly cover"):
        parse_dynamic_agent_response(
            result,
            request,
            task_brief=task_brief(),
            team_plan=team_plan(),
            reviewed_criterion_ids=("AC_LINKS",),
        )

    request, result = _review_result(
        ReviewReportResponse(
            verdict="accept",
            criterion_assessments=(
                ReviewCriterionAssessmentResponse(
                    criterion_id="AC_OTHER",
                    status="satisfied",
                    adversarial_check="Checked another behavior.",
                    evidence="Observed another behavior.",
                    tool_evidence=(review_tool_claim(),),
                ),
            ),
            summary="Reviewed a criterion outside the assigned scope.",
        )
    )
    with pytest.raises(AgentArtifactResponseError, match="outside scope"):
        parse_dynamic_agent_response(
            result,
            request,
            task_brief=task_brief(),
            team_plan=team_plan(),
            reviewed_criterion_ids=("AC_LINKS",),
        )

    request, result = _review_result(
        ReviewReportResponse(
            verdict="accept",
            criterion_assessments=(
                ReviewCriterionAssessmentResponse(
                    criterion_id="AC_OTHER",
                    status="satisfied",
                    adversarial_check="Checked the assigned behavior.",
                    evidence="Observed the assigned behavior.",
                    tool_evidence=(review_tool_claim(),),
                ),
            ),
            summary="Claimed an unknown assigned criterion is satisfied.",
        )
    )
    with pytest.raises(AgentArtifactResponseError, match="unknown acceptance"):
        parse_dynamic_agent_response(
            result,
            request,
            task_brief=task_brief(),
            team_plan=team_plan(),
            reviewed_criterion_ids=("AC_OTHER",),
        )


def test_dynamic_review_response_binds_blocked_assessment_to_finding() -> None:
    blocked = ReviewCriterionAssessmentResponse(
        criterion_id="AC_LINKS",
        status="blocked",
        adversarial_check="Ran the CLI against a broken nested link.",
        evidence="The command returned zero despite the broken link.",
        tool_evidence=(review_tool_claim(),),
    )
    request, result = _review_result(
        ReviewReportResponse(
            verdict="revise",
            criterion_assessments=(blocked,),
            findings=(
                ReviewFinding(
                    id="FINDING_LINK_EXIT",
                    severity="high",
                    blocking=True,
                    category="behavior",
                    description="Broken links do not produce a failing exit code.",
                    recommendation="Return non-zero when any link is broken.",
                    criterion_ids=("AC_LINKS",),
                ),
            ),
            summary="One observed blocker requires revision.",
        )
    )

    parsed = parse_dynamic_agent_response(
        result,
        request,
        task_brief=task_brief(),
        team_plan=team_plan(),
        reviewed_criterion_ids=("AC_LINKS",),
    )
    assert isinstance(parsed.body, GroundedReviewReportResponse)

    missing_scope = ReviewReportResponse(
        verdict="revise",
        criterion_assessments=(blocked,),
        findings=tuple(
            finding.model_copy(update={"criterion_ids": ()})
            for finding in parsed.body.findings
        ),
        summary=parsed.body.summary,
    )
    request, result = _review_result(missing_scope)
    rebound = parse_dynamic_agent_response(
        result,
        request,
        task_brief=task_brief(),
        team_plan=team_plan(),
        reviewed_criterion_ids=("AC_LINKS",),
    )
    assert isinstance(rebound.body, GroundedReviewReportResponse)
    assert rebound.body.findings[0].criterion_ids == ("AC_LINKS",)


def test_dynamic_review_response_rejects_ambiguous_unscoped_blockers() -> None:
    blocked = ReviewCriterionAssessmentResponse(
        criterion_id="AC_LINKS",
        status="blocked",
        adversarial_check="Ran the CLI against a broken nested link.",
        evidence="The command returned zero despite the broken link.",
        tool_evidence=(review_tool_claim(),),
    )
    findings = tuple(
        ReviewFinding(
            id=f"FINDING_LINK_EXIT_{index}",
            severity="high",
            blocking=True,
            category="behavior",
            description=f"Candidate explanation {index} for the broken behavior.",
            recommendation=f"Apply candidate correction {index}.",
        )
        for index in (1, 2)
    )
    request, result = _review_result(
        ReviewReportResponse(
            verdict="revise",
            criterion_assessments=(blocked,),
            findings=findings,
            summary="Two blockers cannot be mapped without explicit criterion IDs.",
        )
    )

    with pytest.raises(AgentArtifactResponseError, match="must reference"):
        parse_dynamic_agent_response(
            result,
            request,
            task_brief=task_brief(),
            team_plan=team_plan(),
            reviewed_criterion_ids=("AC_LINKS",),
        )


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
