"""Tests for minimum-context prompts and strict Agent artifact responses."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from software_agent_team.artifacts import (
    AcceptanceCriterion,
    AgentRole,
    ArtifactKind,
    CheckStatus,
    CommandEvidence,
    CriterionResult,
    ImplementationPlan,
    PlanTask,
    ReviewFinding,
    ReviewReport,
    ReviewSeverity,
    ReviewTerminationReason,
    ReviewVerdict,
    TaskBrief,
    WorkResult,
)
from software_agent_team.artifacts import (
    TestReport as PhaseTestReport,
)
from software_agent_team.execution import (
    AgentExecutionRequest,
    AgentExecutionStatus,
    ScriptedAgentExecutor,
)
from software_agent_team.prompting import (
    AgentPromptInputs,
    build_agent_execution_request,
    render_agent_prompt,
)
from software_agent_team.response_corrections import ResponseFailureClass
from software_agent_team.responses import (
    AgentArtifactResponseError,
    GroundedReviewReportResponse,
    ImplementationPlanResponse,
    ReviewReportResponse,
    WorkResultResponse,
    parse_agent_response,
)
from software_agent_team.responses import (
    TestReportResponse as SemanticTestReport,
)

CREATED_AT = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
INPUT_COMMIT = "1" * 40
OUTPUT_COMMIT = "2" * 40
TEAM_ROLES = frozenset(
    {
        AgentRole.PLANNER,
        AgentRole.GENERALIST_DEVELOPER,
        AgentRole.TESTER,
        AgentRole.REVIEWER,
    }
)


def task_brief() -> TaskBrief:
    return TaskBrief(
        run_id="task-manager-001",
        title="Task manager",
        source_request="Build a task manager.",
        requirements=["Create and persist tasks."],
        acceptance_criteria=[
            AcceptanceCriterion(
                id="AC_CREATE",
                description="Tasks can be created.",
                verification="Run the create test.",
            ),
            AcceptanceCriterion(
                id="AC_QUALITY",
                description="Quality checks pass.",
                verification="Run the quality checks.",
            ),
        ],
        confirmed=True,
    )


def plan() -> ImplementationPlan:
    return ImplementationPlan(
        run_id="task-manager-001",
        team_id="function_specialized",
        created_at=CREATED_AT,
        objective="Build the confirmed task manager.",
        approach=("Implement one tested vertical slice.",),
        tasks=(
            PlanTask(
                id="TASK_APP",
                owner=AgentRole.GENERALIST_DEVELOPER,
                description="Implement task creation.",
                acceptance_criteria=("AC_CREATE",),
                expected_paths=("app.py",),
            ),
            PlanTask(
                id="TASK_QUALITY",
                owner=AgentRole.GENERALIST_DEVELOPER,
                description="Add fixed quality checks.",
                dependencies=("TASK_APP",),
                acceptance_criteria=("AC_QUALITY",),
                expected_paths=("tests/test_app.py",),
            ),
        ),
    )


def work_result(
    *,
    iteration: int = 1,
    producer: AgentRole = AgentRole.GENERALIST_DEVELOPER,
    run_id: str = "task-manager-001",
) -> WorkResult:
    input_commit = INPUT_COMMIT if iteration == 1 else OUTPUT_COMMIT
    output_commit = OUTPUT_COMMIT if iteration == 1 else "3" * 40
    return WorkResult(
        run_id=run_id,
        team_id="function_specialized",
        producer=producer,
        created_at=CREATED_AT,
        iteration=iteration,
        input_commit=input_commit,
        output_commit=output_commit,
        summary="Implemented the planned task workflow.",
        completed_tasks=("TASK_APP", "TASK_QUALITY"),
        changed_files=("app.py", "tests/test_app.py"),
    )


def commands() -> tuple[CommandEvidence, ...]:
    return (
        CommandEvidence(
            id="CHECK_TEST",
            argv=("pytest",),
            criterion_ids=("AC_CREATE", "AC_QUALITY"),
            exit_code=0,
            duration_ms=25,
            stdout_path="iterations/01/check-test.stdout",
            stderr_path="iterations/01/check-test.stderr",
            stdout_tail="1 passed\n",
            summary="All tests passed.",
        ),
    )


def make_test_report(*, iteration: int = 1) -> PhaseTestReport:
    commit = OUTPUT_COMMIT if iteration == 1 else "3" * 40
    return PhaseTestReport(
        run_id="task-manager-001",
        team_id="function_specialized",
        created_at=CREATED_AT,
        iteration=iteration,
        input_commit=commit,
        status=CheckStatus.PASSED,
        commands=commands(),
        criteria=(
            CriterionResult(
                criterion_id="AC_CREATE",
                status=CheckStatus.PASSED,
                command_ids=("CHECK_TEST",),
                detail="The create test passed.",
            ),
            CriterionResult(
                criterion_id="AC_QUALITY",
                status=CheckStatus.PASSED,
                command_ids=("CHECK_TEST",),
                detail="The quality checks passed.",
            ),
        ),
        summary="All deterministic checks passed.",
    )


def test_failed_test_report_requires_a_failed_criterion() -> None:
    payload = make_test_report().model_dump(mode="json")
    payload["status"] = "failed"
    payload["commands"][0]["exit_code"] = 1

    with pytest.raises(ValidationError, match="failed criterion"):
        PhaseTestReport.model_validate(payload)


def test_passing_test_report_defers_manual_criteria_to_review() -> None:
    payload = make_test_report().model_dump(mode="json")
    payload["manual_review_criteria"] = ["AC_CREATE"]
    payload["criteria"][0]["status"] = "pending_review"

    report = PhaseTestReport.model_validate(payload)

    assert report.status is CheckStatus.PASSED
    assert report.criteria[0].status is CheckStatus.PENDING_REVIEW


def test_test_report_top_level_status_excludes_pending_review() -> None:
    status_schema = PhaseTestReport.model_json_schema()["properties"]["status"]
    payload = make_test_report().model_dump(mode="json")
    payload["status"] = "pending_review"

    assert status_schema["enum"] == ["passed", "failed", "blocked"]
    assert "never for this field" in status_schema["description"]
    with pytest.raises(ValidationError, match="Input should be"):
        PhaseTestReport.model_validate(payload)


def test_tester_cannot_pass_a_manual_review_criterion() -> None:
    payload = make_test_report().model_dump(mode="json")
    payload["manual_review_criteria"] = ["AC_CREATE"]

    with pytest.raises(ValidationError, match="cannot pass"):
        PhaseTestReport.model_validate(payload)


def review_report(*, iteration: int = 1) -> ReviewReport:
    commit = OUTPUT_COMMIT if iteration == 1 else "3" * 40
    return ReviewReport(
        run_id="task-manager-001",
        team_id="function_specialized",
        created_at=CREATED_AT,
        iteration=iteration,
        input_commit=commit,
        verdict=ReviewVerdict.ACCEPT,
        summary="No blocking findings remain.",
    )


def test_live_failed_review_requires_an_explicit_terminal_reason() -> None:
    payload = review_report().model_dump(mode="json")
    payload["verdict"] = "fail"
    payload["findings"] = [
        ReviewFinding(
            id="FINDING_BOUNDARY",
            severity=ReviewSeverity.CRITICAL,
            blocking=True,
            category="safety_boundary",
            description="The immutable evidence records a boundary violation.",
            recommendation="Stop the run for operator review.",
        ).model_dump(mode="json")
    ]

    legacy_compatible_report = ReviewReport.model_validate(payload)

    assert legacy_compatible_report.termination_reason is None
    with pytest.raises(AgentArtifactResponseError, match="terminal review reason"):
        parse_scripted(
            legacy_compatible_report,
            execution_request(AgentRole.REVIEWER, ArtifactKind.REVIEW_REPORT),
        )


def test_correctable_critical_finding_still_allows_revision() -> None:
    report = ReviewReport(
        run_id="task-manager-001",
        team_id="function_specialized",
        created_at=CREATED_AT,
        iteration=1,
        input_commit=OUTPUT_COMMIT,
        verdict=ReviewVerdict.REVISE,
        findings=(
            ReviewFinding(
                id="FINDING_ACCEPTANCE",
                severity=ReviewSeverity.CRITICAL,
                blocking=True,
                category="correctness",
                description="A required endpoint rejects a valid request.",
                recommendation="Correct the endpoint contract and rerun the gate.",
            ),
        ),
        summary="The correctable implementation defect requires revision.",
    )

    assert report.termination_reason is None


def test_failed_review_accepts_a_terminal_boundary_reason() -> None:
    report = ReviewReport(
        run_id="task-manager-001",
        team_id="function_specialized",
        created_at=CREATED_AT,
        iteration=1,
        input_commit=OUTPUT_COMMIT,
        verdict=ReviewVerdict.FAIL,
        termination_reason=ReviewTerminationReason.SAFETY_BOUNDARY_CROSSED,
        findings=(
            ReviewFinding(
                id="FINDING_BOUNDARY",
                severity=ReviewSeverity.CRITICAL,
                blocking=True,
                category="safety_boundary",
                description="The immutable evidence records a boundary violation.",
                recommendation="Stop the run for operator review.",
            ),
        ),
        summary="Continuing would cross the recorded safety boundary.",
    )

    assert report.termination_reason is ReviewTerminationReason.SAFETY_BOUNDARY_CROSSED


def test_nonfailed_review_rejects_a_terminal_reason() -> None:
    payload = review_report().model_dump(mode="json")
    payload["termination_reason"] = "safety_boundary_crossed"

    with pytest.raises(ValidationError, match="only failed reviews"):
        ReviewReport.model_validate(payload)


def prompt_inputs(**updates: object) -> AgentPromptInputs:
    payload: dict[str, object] = {
        "task_brief": task_brief(),
        "team_id": "function_specialized",
        "team_roles": TEAM_ROLES,
        "iteration": 1,
        "iteration_limit": 2,
        "role": AgentRole.PLANNER,
        "expected_kind": ArtifactKind.IMPLEMENTATION_PLAN,
    }
    payload.update(updates)
    return AgentPromptInputs.model_validate(payload)


def execution_request(
    role: AgentRole,
    kind: ArtifactKind,
    *,
    iteration: int = 1,
) -> AgentExecutionRequest:
    return AgentExecutionRequest(
        run_id="task-manager-001",
        team_id="function_specialized",
        iteration=iteration,
        role=role,
        expected_kind=kind,
        prompt="Return exactly one JSON object.",
        timeout_seconds=30,
    )


def parse_scripted(
    artifact: ImplementationPlan | WorkResult | PhaseTestReport | ReviewReport | str,
    request: AgentExecutionRequest,
):
    result = ScriptedAgentExecutor([artifact], clock=lambda: CREATED_AT).execute(
        request
    )
    return parse_agent_response(
        result,
        request,
        task_brief=task_brief(),
        team_roles=TEAM_ROLES,
        iteration_limit=2,
    )


def semantic_body(
    artifact: ImplementationPlan | WorkResult | PhaseTestReport | ReviewReport,
):
    if isinstance(artifact, ImplementationPlan):
        return ImplementationPlanResponse(
            objective=artifact.objective,
            approach=artifact.approach,
            tasks=artifact.tasks,
            risks=artifact.risks,
            assumptions=artifact.assumptions,
        )
    if isinstance(artifact, WorkResult):
        return WorkResultResponse(
            summary=artifact.summary,
            completed_tasks=artifact.completed_tasks,
            unresolved_issues=artifact.unresolved_issues,
        )
    if isinstance(artifact, PhaseTestReport):
        return SemanticTestReport(
            findings=artifact.findings,
            summary=artifact.summary,
        )
    return ReviewReportResponse(
        verdict=artifact.verdict,
        termination_reason=artifact.termination_reason,
        findings=artifact.findings,
        summary=artifact.summary,
    )


def test_planner_prompt_contains_only_confirmed_inputs_and_plan_schema() -> None:
    rendered = render_agent_prompt(prompt_inputs())

    assert '"run_id": "task-manager-001"' in rendered
    assert '"expected_artifact_kind": "implementation_plan"' in rendered
    assert '"generalist_developer"' in rendered
    assert "upstream_artifacts" not in rendered
    assert "deterministic_command_evidence" not in rendered
    response_schema = rendered.split("RESPONSE_SCHEMA_JSON\n", 1)[1].split(
        "\n\nFINAL_RESPONSE_CONTRACT",
        1,
    )[0]
    assert '"kind"' not in response_schema
    assert '"run_id"' not in response_schema
    assert "Return exactly one JSON object" in rendered
    assert "every nested object must use each\nkey exactly once" in rendered
    assert "FINAL_RESPONSE_CONTRACT" in rendered
    assert "task_brief.acceptance_criteria" in rendered
    assert "^TASK_[A-Z0-9_]+$" in rendered
    assert rendered.rfind("FINAL_RESPONSE_CONTRACT") > rendered.rfind(
        "RESPONSE_SCHEMA_JSON"
    )


def test_developer_prompt_receives_plan_but_not_quality_evidence() -> None:
    rendered = render_agent_prompt(
        prompt_inputs(
            role=AgentRole.GENERALIST_DEVELOPER,
            expected_kind=ArtifactKind.WORK_RESULT,
            input_commit=INPUT_COMMIT,
            upstream_artifacts=(plan(),),
        )
    )

    assert '"implementation_plan": {' in rendered
    assert '"input_commit": "1111111111111111111111111111111111111111"' in rendered
    assert '"kind": "test_report"' not in rendered
    assert '"kind": "review_report"' not in rendered
    assert "deterministic_command_evidence" not in rendered
    assert "Plan tool use before editing" in rendered
    assert "Complete the\nimplementation, checks, commit" in rendered
    assert "controller independently derives" in rendered


def test_tester_prompt_receives_work_and_deterministic_commands_only() -> None:
    rendered = render_agent_prompt(
        prompt_inputs(
            role=AgentRole.TESTER,
            expected_kind=ArtifactKind.TEST_REPORT,
            input_commit=OUTPUT_COMMIT,
            upstream_artifacts=(work_result(),),
            command_evidence=commands(),
        )
    )

    assert '"work_result": {' in rendered
    assert '"deterministic_command_evidence": [' in rendered
    assert '"id": "CHECK_TEST"' in rendered
    assert '"criterion_ids": [' in rendered
    assert '"stdout_tail": "1 passed\\n"' in rendered
    assert '"verification_scope": {' in rendered
    assert '"manual_review_criteria": []' in rendered
    assert '"root": "/agent"' in rendered
    assert '"verification_scope": {' in rendered
    assert "untrusted diagnostic evidence" in rendered
    assert "controller derives\nthe command list" in rendered
    assert '"implementation_plan": {' not in rendered
    assert '"review_report": {' not in rendered


def test_reviewer_prompt_receives_work_and_command_evidence_in_parallel() -> None:
    rendered = render_agent_prompt(
        prompt_inputs(
            role=AgentRole.REVIEWER,
            expected_kind=ArtifactKind.REVIEW_REPORT,
            input_commit=OUTPUT_COMMIT,
            upstream_artifacts=(work_result(),),
            command_evidence=commands(),
        )
    )
    compact = " ".join(rendered.split())

    assert '"work_result": {' in rendered
    assert '"deterministic_command_evidence": [' in rendered
    assert '"root": "/agent"' in rendered
    assert "mounted read-only at `/agent`" in compact
    assert "sat-probe-write /tmp/sat-review-probe-boundaries-7f3a.py" in compact
    assert "write tool and general file-mutation tools are unavailable" in compact
    assert "never overwrites" in compact
    assert "Do not use `python -c`" in compact
    assert "shell redirection" in compact
    assert "Use `revise` for\nevery correctable implementation defect" in rendered
    assert "Never\nuse `fail` merely because a deterministic command failed" in rendered
    assert '"termination_reason"' in rendered
    assert "controller binds that frozen scope" in rendered
    assert '"implementation_plan": {' not in rendered


def test_verifier_prompts_receive_the_frozen_manual_review_scope() -> None:
    rendered = render_agent_prompt(
        prompt_inputs(
            role=AgentRole.TESTER,
            expected_kind=ArtifactKind.TEST_REPORT,
            input_commit=OUTPUT_COMMIT,
            upstream_artifacts=(work_result(),),
            command_evidence=commands(),
            manual_review_criteria=("AC_CREATE",),
        )
    )

    assert '"manual_review_criteria": [' in rendered
    assert '"AC_CREATE"' in rendered
    assert "criterion statuses, overall status" in rendered
    assert "deterministic blockers" in rendered


def test_prompt_builder_binds_the_same_role_and_response_contract() -> None:
    inputs = prompt_inputs(
        role=AgentRole.GENERALIST_DEVELOPER,
        expected_kind=ArtifactKind.WORK_RESULT,
        input_commit=INPUT_COMMIT,
        upstream_artifacts=(plan(),),
    )

    request = build_agent_execution_request(
        inputs,
        timeout_seconds=45,
        model="provider/model",
    )

    assert request.run_id == inputs.task_brief.run_id
    assert request.team_id == inputs.team_id
    assert request.iteration == inputs.iteration
    assert request.role is inputs.role
    assert request.expected_kind is inputs.expected_kind
    assert request.timeout_seconds == 45
    assert request.model == "provider/model"
    assert request.prompt == render_agent_prompt(inputs)


@pytest.mark.parametrize(
    "updates",
    [
        {"upstream_artifacts": (plan(),)},
        {
            "role": AgentRole.TESTER,
            "expected_kind": ArtifactKind.TEST_REPORT,
            "input_commit": OUTPUT_COMMIT,
            "upstream_artifacts": (work_result(),),
        },
        {
            "role": AgentRole.REVIEWER,
            "expected_kind": ArtifactKind.REVIEW_REPORT,
            "input_commit": OUTPUT_COMMIT,
            "upstream_artifacts": (work_result(), plan()),
        },
        {
            "role": AgentRole.GENERALIST_DEVELOPER,
            "expected_kind": ArtifactKind.WORK_RESULT,
            "input_commit": INPUT_COMMIT,
            "upstream_artifacts": (),
        },
    ],
)
def test_prompt_boundary_rejects_missing_or_unrelated_context(
    updates: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        prompt_inputs(**updates)


def test_revision_prompt_requires_prior_iteration_feedback() -> None:
    with pytest.raises(ValidationError, match="prior test report"):
        prompt_inputs(
            role=AgentRole.GENERALIST_DEVELOPER,
            expected_kind=ArtifactKind.WORK_RESULT,
            iteration=2,
            input_commit=OUTPUT_COMMIT,
            upstream_artifacts=(plan(),),
        )


@pytest.mark.parametrize(
    ("artifact", "role", "kind"),
    [
        (plan(), AgentRole.PLANNER, ArtifactKind.IMPLEMENTATION_PLAN),
        (
            work_result(),
            AgentRole.GENERALIST_DEVELOPER,
            ArtifactKind.WORK_RESULT,
        ),
        (make_test_report(), AgentRole.TESTER, ArtifactKind.TEST_REPORT),
        (review_report(), AgentRole.REVIEWER, ArtifactKind.REVIEW_REPORT),
    ],
)
def test_strict_parser_accepts_each_phase_role_output(
    artifact: ImplementationPlan | WorkResult | PhaseTestReport | ReviewReport,
    role: AgentRole,
    kind: ArtifactKind,
) -> None:
    parsed = parse_scripted(artifact, execution_request(role, kind))

    expected = semantic_body(artifact)
    if isinstance(artifact, ReviewReport):
        assert isinstance(parsed.body, GroundedReviewReportResponse)
        assert parsed.body.model_dump(mode="json") == expected.model_dump(mode="json")
    else:
        assert parsed.body == expected
    assert "kind" in parsed.ignored_controller_fields


@pytest.mark.parametrize(
    "text",
    [
        "Here is the result: {}",
        "```\n{}\n```",
        "{} trailing prose",
        "[]",
        '{"kind":"implementation_plan","kind":"work_result"}',
        '{"kind":"implementation_plan","value":NaN}',
    ],
)
def test_strict_parser_rejects_prose_fences_and_non_objects(text: str) -> None:
    with pytest.raises(AgentArtifactResponseError, match=r"JSON|semantic"):
        parse_scripted(
            text, execution_request(AgentRole.PLANNER, ArtifactKind.IMPLEMENTATION_PLAN)
        )


def test_strict_parser_normalizes_one_outer_json_fence() -> None:
    artifact = plan()
    fenced = f"```json\n{json.dumps(artifact.model_dump(mode='json'))}\n```"

    parsed = parse_scripted(
        fenced,
        execution_request(AgentRole.PLANNER, ArtifactKind.IMPLEMENTATION_PLAN),
    )

    assert parsed.body == semantic_body(artifact)


def test_strict_parser_reports_a_duplicate_key_without_response_values() -> None:
    response = '{"kind":"implementation_plan","kind":"work_result"}'

    with pytest.raises(
        AgentArtifactResponseError,
        match="duplicate JSON object key: kind",
    ) as captured:
        parse_scripted(
            response,
            execution_request(AgentRole.PLANNER, ArtifactKind.IMPLEMENTATION_PLAN),
        )

    assert "work_result" not in str(captured.value)


def test_strict_parser_normalizes_presentation_prose_around_one_json_fence() -> None:
    fenced = (
        "Here is the result:\n```json\n"
        f"{json.dumps(plan().model_dump(mode='json'))}\n"
        "```\nThis is the requested artifact."
    )

    parsed = parse_scripted(
        fenced,
        execution_request(AgentRole.PLANNER, ArtifactKind.IMPLEMENTATION_PLAN),
    )

    assert parsed.body == semantic_body(plan())


def test_strict_parser_allows_non_json_bracket_notation_around_json_fence() -> None:
    payload = json.dumps(plan().model_dump(mode="json"))
    payload = f"```json\n{payload}\n```"
    response = (
        "Verified [project.scripts], ['uv', 'sync', '--dev'], and `{not JSON}`.\n"
        f"{payload}\n"
        "The committed implementation is ready."
    )

    parsed = parse_scripted(
        response,
        execution_request(AgentRole.PLANNER, ArtifactKind.IMPLEMENTATION_PLAN),
    )

    assert parsed.body == semantic_body(plan())


def test_strict_parser_normalizes_presentation_prose_around_one_object() -> None:
    response = (
        "I verified the commit and changed paths.\n"
        f"{json.dumps(plan().model_dump(mode='json'))}\n"
        "This is the requested artifact."
    )

    parsed = parse_scripted(
        response,
        execution_request(AgentRole.PLANNER, ArtifactKind.IMPLEMENTATION_PLAN),
    )

    assert parsed.body == semantic_body(plan())


def test_strict_parser_accepts_ancillary_tool_diagnostic_after_one_object() -> None:
    response = (
        f"{json.dumps(plan().model_dump(mode='json'))}\n\n"
        "⚠️ ✍️ Write: to /tmp/placeholder (2 chars) failed"
    )

    parsed = parse_scripted(
        response,
        execution_request(AgentRole.PLANNER, ArtifactKind.IMPLEMENTATION_PLAN),
    )

    assert parsed.body == semantic_body(plan())


def test_strict_parser_accepts_argv_arrays_before_one_semantic_object() -> None:
    response = (
        "The setup contract now uses "
        '["uv", "sync", "--dev"] and the test contract uses '
        '["uv", "run", "pytest"].\n'
        f"{json.dumps(plan().model_dump(mode='json'))}"
    )

    parsed = parse_scripted(
        response,
        execution_request(AgentRole.PLANNER, ArtifactKind.IMPLEMENTATION_PLAN),
    )

    assert parsed.body == semantic_body(plan())


@pytest.mark.parametrize("suffix", ["}", "]", "]}", "}}]]"])
def test_strict_parser_normalizes_bounded_redundant_closing_delimiters(
    suffix: str,
) -> None:
    response = f"{json.dumps(plan().model_dump(mode='json'))}{suffix}"

    parsed = parse_scripted(
        response,
        execution_request(AgentRole.PLANNER, ArtifactKind.IMPLEMENTATION_PLAN),
    )

    assert parsed.body == semantic_body(plan())


@pytest.mark.parametrize(
    "suffix",
    [
        "}}]]}",
        ']{"objective":"competing"}',
    ],
)
def test_strict_parser_rejects_unbounded_or_structured_closing_suffixes(
    suffix: str,
) -> None:
    response = f"{json.dumps(plan().model_dump(mode='json'))}{suffix}"

    with pytest.raises(
        AgentArtifactResponseError,
        match="outside JSON object candidates",
    ):
        parse_scripted(
            response,
            execution_request(AgentRole.PLANNER, ArtifactKind.IMPLEMENTATION_PLAN),
        )


@pytest.mark.parametrize(
    "outside",
    [
        "A competing object follows: {}",
        "A fence follows: ```text not json ```",
    ],
)
def test_strict_parser_rejects_structured_content_outside_unfenced_object(
    outside: str,
) -> None:
    response = f"{json.dumps(plan().model_dump(mode='json'))}\n{outside}"

    with pytest.raises(
        AgentArtifactResponseError,
        match="outside JSON object candidates",
    ):
        parse_scripted(
            response,
            execution_request(AgentRole.PLANNER, ArtifactKind.IMPLEMENTATION_PLAN),
        )


@pytest.mark.parametrize(
    "outside",
    [
        "A competing object follows: {}",
        "A second fence follows:\n```text\nnot json\n```",
    ],
)
def test_strict_parser_rejects_structured_content_outside_json_fence(
    outside: str,
) -> None:
    fenced = f"```json\n{json.dumps(plan().model_dump(mode='json'))}\n```\n{outside}"

    with pytest.raises(
        AgentArtifactResponseError,
        match="outside JSON object candidates",
    ):
        parse_scripted(
            fenced,
            execution_request(AgentRole.PLANNER, ArtifactKind.IMPLEMENTATION_PLAN),
        )


def test_strict_parser_rejects_the_wrong_artifact_kind() -> None:
    with pytest.raises(
        AgentArtifactResponseError, match="semantic response is invalid"
    ):
        parse_scripted(
            work_result(),
            execution_request(AgentRole.PLANNER, ArtifactKind.IMPLEMENTATION_PLAN),
        )


def test_non_object_transport_is_typed_and_has_no_semantic_correction_path() -> None:
    with pytest.raises(AgentArtifactResponseError) as captured:
        parse_scripted(
            '["not", "a", "semantic", "object"]',
            execution_request(AgentRole.PLANNER, ArtifactKind.IMPLEMENTATION_PLAN),
        )

    assert captured.value.semantic_payload is None
    assert captured.value.diagnostic is not None
    assert captured.value.diagnostic.failure_class is ResponseFailureClass.TRANSPORT
    assert captured.value.diagnostic.correction_paths == ()


def test_parser_does_not_require_an_artifact_kind_from_the_agent() -> None:
    payload = plan().model_dump(mode="json")
    del payload["kind"]

    parsed = parse_scripted(
        json.dumps(payload),
        execution_request(AgentRole.PLANNER, ArtifactKind.IMPLEMENTATION_PLAN),
    )

    assert parsed.body == semantic_body(plan())
    assert "kind" not in parsed.ignored_controller_fields


def test_parser_ignores_conflicting_controller_owned_fields() -> None:
    payload = plan().model_dump(mode="json")
    payload.update(
        {
            "kind": "work_result",
            "producer": "reviewer",
            "run_id": "another-run",
            "team_id": "single_agent",
            "iteration": 3,
        }
    )

    parsed = parse_scripted(
        json.dumps(payload),
        execution_request(AgentRole.PLANNER, ArtifactKind.IMPLEMENTATION_PLAN),
    )

    assert parsed.body == semantic_body(plan())
    assert set(parsed.ignored_controller_fields).issuperset(
        {"kind", "producer", "run_id", "team_id", "iteration"}
    )


def test_parser_deterministically_discards_an_unknown_semantic_field() -> None:
    payload = semantic_body(plan()).model_dump(mode="json")
    payload["unsupported_claim"] = True

    parsed = parse_scripted(
        json.dumps(payload),
        execution_request(AgentRole.PLANNER, ArtifactKind.IMPLEMENTATION_PLAN),
    )

    assert parsed.body == semantic_body(plan())
    assert parsed.response_normalizations == (
        "removed schema-forbidden field /unsupported_claim",
    )


def test_parser_ignores_a_model_supplied_producer() -> None:
    parsed = parse_scripted(
        work_result(producer=AgentRole.BACKEND_DEVELOPER),
        execution_request(
            AgentRole.GENERALIST_DEVELOPER,
            ArtifactKind.WORK_RESULT,
        ),
    )

    assert parsed.body == semantic_body(work_result())
    assert "producer" in parsed.ignored_controller_fields


def test_parser_ignores_model_supplied_run_context() -> None:
    parsed = parse_scripted(
        work_result(run_id="another-run"),
        execution_request(
            AgentRole.GENERALIST_DEVELOPER,
            ArtifactKind.WORK_RESULT,
        ),
    )

    assert parsed.body == semantic_body(work_result())
    assert "run_id" in parsed.ignored_controller_fields


def test_parser_ignores_a_model_supplied_iteration() -> None:
    parsed = parse_scripted(
        work_result(iteration=2),
        execution_request(
            AgentRole.GENERALIST_DEVELOPER,
            ArtifactKind.WORK_RESULT,
        ),
    )

    assert parsed.body.summary == "Implemented the planned task workflow."
    assert "iteration" in parsed.ignored_controller_fields


def test_strict_parser_rejects_incomplete_acceptance_coverage() -> None:
    incomplete = plan().model_copy(update={"tasks": plan().tasks[:1]})

    with pytest.raises(
        AgentArtifactResponseError,
        match=r"semantic response is invalid: .*missing:",
    ):
        parse_scripted(
            incomplete,
            execution_request(AgentRole.PLANNER, ArtifactKind.IMPLEMENTATION_PLAN),
        )


def test_strict_parser_rejects_failed_execution_before_reading_text() -> None:
    request = execution_request(AgentRole.PLANNER, ArtifactKind.IMPLEMENTATION_PLAN)
    result = ScriptedAgentExecutor([plan()], clock=lambda: CREATED_AT).execute(request)
    failed = result.model_copy(update={"status": AgentExecutionStatus.PROCESS_FAILED})

    with pytest.raises(AgentArtifactResponseError, match="did not complete"):
        parse_agent_response(
            failed,
            request,
            task_brief=task_brief(),
            team_roles=TEAM_ROLES,
            iteration_limit=2,
        )


@pytest.mark.parametrize(
    "outside",
    [
        "A command array follows: []",
        'The command is ["uv", "run", "pytest"].',
        "] another presentation note",
    ],
)
def test_strict_parser_accepts_non_object_json_arrays_outside_one_object(
    outside: str,
) -> None:
    response = f"{json.dumps(plan().model_dump(mode='json'))}\n{outside}"

    parsed = parse_scripted(
        response,
        execution_request(AgentRole.PLANNER, ArtifactKind.IMPLEMENTATION_PLAN),
    )

    assert parsed.body == semantic_body(plan())


def test_strict_parser_rejects_an_array_containing_a_competing_object() -> None:
    response = (
        f"{json.dumps(plan().model_dump(mode='json'))}\n"
        'A competing value follows: [{"objective":"other"}]'
    )

    with pytest.raises(
        AgentArtifactResponseError,
        match="outside JSON object candidates",
    ):
        parse_scripted(
            response,
            execution_request(AgentRole.PLANNER, ArtifactKind.IMPLEMENTATION_PLAN),
        )


def test_strict_parser_rejects_a_mismatched_session() -> None:
    request = execution_request(AgentRole.PLANNER, ArtifactKind.IMPLEMENTATION_PLAN)
    result = ScriptedAgentExecutor([plan()], clock=lambda: CREATED_AT).execute(request)
    telemetry = result.telemetry.model_copy(update={"session_key": "agent:other:key"})
    mismatched = result.model_copy(update={"telemetry": telemetry})

    with pytest.raises(AgentArtifactResponseError, match="session"):
        parse_agent_response(
            mismatched,
            request,
            task_brief=task_brief(),
            team_roles=TEAM_ROLES,
            iteration_limit=2,
        )
