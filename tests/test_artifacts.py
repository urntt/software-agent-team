"""Tests for task-brief and cross-Agent handoff contracts."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from software_agent_team.artifacts import (
    AgentExecutionRecord,
    HandoffEnvelope,
    HandoffStatus,
    ReviewBoundaryCheck,
    ReviewBoundaryKind,
    ReviewReport,
    TaskBrief,
    review_boundary_definition_map,
)


def valid_handoff_payload() -> dict[str, object]:
    """Return a minimal valid implementation handoff."""

    return {
        "run_id": "task-manager-001",
        "team_id": "function_specialized",
        "iteration": 1,
        "source_agent_id": "generalist_developer",
        "target_agent_id": "tester",
        "status": "completed",
        "summary": "Implemented the first task workflow.",
        "input_commit": "3a12f72",
        "artifacts": [
            {
                "kind": "work_result",
                "path": ("iterations/01/agents/generalist_developer/work-result.json"),
                "sha256": (
                    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
                ),
            }
        ],
        "blockers": [],
    }


def valid_task_brief_payload() -> dict[str, object]:
    """Return a minimal confirmed task brief."""

    return {
        "run_id": "task-manager-001",
        "title": "Task manager",
        "source_request": "Build a task-management Web application.",
        "requirements": ["Persist tasks."],
        "acceptance_criteria": [
            {
                "id": "AC_PERSIST",
                "description": "Tasks persist across restarts.",
                "verification": "Run the persistence test.",
            }
        ],
        "constraints": ["Use SQLite."],
        "assumptions": [],
        "open_questions": [],
        "confirmed": True,
    }


def test_valid_handoff_is_accepted() -> None:
    handoff = HandoffEnvelope.model_validate(valid_handoff_payload())

    assert handoff.status is HandoffStatus.COMPLETED
    assert handoff.team_id == "function_specialized"


@pytest.mark.parametrize("iteration", [0, 4])
def test_iteration_must_respect_the_vision_limit(iteration: int) -> None:
    payload = valid_handoff_payload()
    payload["iteration"] = iteration

    with pytest.raises(ValidationError):
        HandoffEnvelope.model_validate(payload)


def test_blocked_handoff_requires_a_reason() -> None:
    payload = valid_handoff_payload()
    payload["status"] = "blocked"

    with pytest.raises(ValidationError, match="must identify a blocker"):
        HandoffEnvelope.model_validate(payload)


@pytest.mark.parametrize(
    "path",
    ["/tmp/report.json", "../report.json", r"..\report.json", "."],
)
def test_artifacts_cannot_escape_the_run_directory(path: str) -> None:
    payload = valid_handoff_payload()
    artifacts = payload["artifacts"]
    assert isinstance(artifacts, list)
    artifacts[0]["path"] = path

    with pytest.raises(ValidationError):
        HandoffEnvelope.model_validate(payload)


def test_handoff_must_cross_an_agent_boundary() -> None:
    payload = valid_handoff_payload()
    payload["target_agent_id"] = "generalist_developer"

    with pytest.raises(ValidationError, match="must differ"):
        HandoffEnvelope.model_validate(payload)


def test_confirmed_task_brief_is_accepted() -> None:
    task_brief = TaskBrief.model_validate(valid_task_brief_payload())

    assert task_brief.confirmed
    assert task_brief.acceptance_criteria[0].id == "AC_PERSIST"


def test_review_boundary_protocol_has_controller_owned_exact_meanings() -> None:
    definitions = review_boundary_definition_map()

    assert tuple(definitions) == tuple(
        boundary.value for boundary in ReviewBoundaryKind
    )
    assert "root itself is the top-level input" in definitions["top_level_input"]
    assert (
        "immediate first-level child, is nested input" in definitions["top_level_input"]
    )
    assert "Immediate children and deeper descendants" in definitions["nested_input"]
    definitions["top_level_input"] = "mutated caller copy"
    assert (
        review_boundary_definition_map()["top_level_input"]
        != definitions["top_level_input"]
    )


def test_confirmed_task_brief_cannot_retain_open_questions() -> None:
    payload = valid_task_brief_payload()
    payload["open_questions"] = ["Which authentication method should be used?"]

    with pytest.raises(ValidationError, match="cannot contain open questions"):
        TaskBrief.model_validate(payload)


def test_acceptance_criterion_ids_must_be_unique() -> None:
    payload = valid_task_brief_payload()
    criteria = payload["acceptance_criteria"]
    assert isinstance(criteria, list)
    criteria.append(deepcopy(criteria[0]))

    with pytest.raises(ValidationError, match="must be unique"):
        TaskBrief.model_validate(payload)


def test_unknown_fields_are_rejected() -> None:
    payload = valid_handoff_payload()
    payload["private_state"] = "must not cross the handoff boundary"

    with pytest.raises(ValidationError):
        HandoffEnvelope.model_validate(payload)


def valid_execution_payload() -> dict[str, object]:
    """Return structurally valid telemetry for one Agent invocation."""

    started_at = datetime(2026, 8, 10, 10, 0, tzinfo=UTC)
    return {
        "run_id": "task-manager-001",
        "team_id": "function_specialized",
        "iteration": 1,
        "stage": "implement",
        "agent_id": "generalist_developer",
        "capability": "implementation",
        "session_key": "agent:generalist_developer:test-session",
        "session_id": "session-generalist-developer",
        "model": "test-provider/test-model",
        "started_at": started_at,
        "finished_at": started_at + timedelta(seconds=2),
        "duration_ms": 2000,
        "exit_code": 0,
        "stdout_path": (
            "iterations/01/executions/implement/"
            "generalist_developer-attempt-01.stdout.txt"
        ),
        "stderr_path": (
            "iterations/01/executions/implement/"
            "generalist_developer-attempt-01.stderr.txt"
        ),
        "stdout_sha256": "a" * 64,
        "stderr_sha256": "b" * 64,
        "response_artifact": {
            "kind": "work_result",
            "path": "iterations/01/agents/generalist_developer/work-result.json",
            "sha256": "c" * 64,
        },
    }


def test_valid_agent_execution_record_is_accepted() -> None:
    record = AgentExecutionRecord.model_validate(valid_execution_payload())

    assert record.agent_id == "generalist_developer"
    assert record.capability == "implementation"
    assert record.duration_ms == 2000


def test_execution_record_preserves_response_binding_and_stage_budget() -> None:
    payload = valid_execution_payload()
    payload.update(
        {
            "response_contract": "semantic_body_v1",
            "controller_supplied_fields": [
                "kind",
                "run_id",
                "input_commit",
                "changed_files",
            ],
            "ignored_controller_fields": ["kind", "input_commit"],
            "stage_timeout_seconds": 900,
            "remaining_timeout_seconds": 275,
        }
    )

    record = AgentExecutionRecord.model_validate(payload)

    assert record.response_contract == "semantic_body_v1"
    assert record.ignored_controller_fields == ("kind", "input_commit")
    assert record.remaining_timeout_seconds == 275


def test_execution_record_preserves_sanitized_tool_session_evidence() -> None:
    payload = valid_execution_payload()
    payload.update(
        {
            "tool_evidence_status": "captured",
            "session_transcript_sha256": "d" * 64,
            "session_record_count": 4,
            "tool_calls": [
                {
                    "id": "tool-001",
                    "tool_name": "exec",
                    "executable": "sat-probe-write",
                    "external_call_sha256": "e" * 64,
                    "arguments_sha256": "f" * 64,
                    "outcome": "succeeded",
                    "is_error": False,
                    "reported_status": "completed",
                    "exit_code": 0,
                    "duration_ms": 17,
                    "output_sha256": "1" * 64,
                    "output_bytes": 11,
                    "output_excerpt": "BOUNDARY_OK",
                }
            ],
        }
    )

    record = AgentExecutionRecord.model_validate(payload)

    assert record.session_record_count == 4
    assert record.tool_calls[0].id == "tool-001"
    assert record.tool_calls[0].output_excerpt == "BOUNDARY_OK"


@pytest.mark.parametrize(
    "updates",
    [
        {
            "tool_evidence_status": "captured",
            "session_record_count": 2,
        },
        {
            "tool_evidence_status": "invalid",
        },
        {
            "tool_evidence_status": "not_captured",
            "session_transcript_sha256": "d" * 64,
        },
    ],
)
def test_execution_record_rejects_incoherent_tool_evidence(
    updates: dict[str, object],
) -> None:
    payload = valid_execution_payload()
    payload.update(updates)

    with pytest.raises(ValidationError, match="tool evidence"):
        AgentExecutionRecord.model_validate(payload)


def test_execution_record_rejects_incoherent_response_binding_or_timeout() -> None:
    payload = valid_execution_payload()
    payload.update(
        {
            "response_contract": "semantic_body_v1",
            "controller_supplied_fields": ["kind"],
            "ignored_controller_fields": ["input_commit"],
            "stage_timeout_seconds": 120,
            "remaining_timeout_seconds": 121,
        }
    )

    with pytest.raises(ValidationError):
        AgentExecutionRecord.model_validate(payload)


def test_execution_finish_cannot_precede_start() -> None:
    payload = valid_execution_payload()
    payload["finished_at"] = payload["started_at"] - timedelta(milliseconds=1)

    with pytest.raises(ValidationError, match="finish time"):
        AgentExecutionRecord.model_validate(payload)


def test_timed_out_execution_requires_error_without_exit_or_response() -> None:
    payload = valid_execution_payload()
    payload["timed_out"] = True
    payload["exit_code"] = None
    payload["response_artifact"] = None

    with pytest.raises(ValidationError, match="record an error"):
        AgentExecutionRecord.model_validate(payload)


def test_openclaw_declared_timeout_can_preserve_zero_wrapper_exit() -> None:
    payload = valid_execution_payload()
    payload["timed_out"] = True
    payload["exit_code"] = 0
    payload["response_artifact"] = None
    payload["error"] = "OpenClaw reported an Agent timeout"

    record = AgentExecutionRecord.model_validate(payload)

    assert record.timed_out is True
    assert record.exit_code == 0


def test_launch_failure_can_lack_session_model_and_exit_code() -> None:
    payload = valid_execution_payload()
    payload["session_id"] = None
    payload["model"] = None
    payload["exit_code"] = None
    payload["response_artifact"] = None
    payload["error"] = "OpenClaw could not be launched."

    record = AgentExecutionRecord.model_validate(payload)

    assert record.exit_code is None
    assert record.model is None


def valid_review_payload() -> dict[str, object]:
    """Return a review with criterion-level adversarial evidence."""

    return {
        "run_id": "task-manager-001",
        "team_id": "adaptive_team",
        "producer": "quality_reviewer",
        "created_at": datetime(2026, 8, 27, 12, 0, tzinfo=UTC),
        "iteration": 1,
        "input_commit": "d" * 40,
        "verdict": "accept",
        "reviewed_criteria": ["AC_PERSIST"],
        "criterion_assessments": [
            {
                "criterion_id": "AC_PERSIST",
                "status": "satisfied",
                "adversarial_check": "Restarted the service after writing one task.",
                "evidence": "The task remained visible after restart.",
                "tool_evidence": [
                    {
                        "tool_call_id": "tool-001",
                        "observable": "task remained visible",
                    }
                ],
            }
        ],
        "summary": "The assigned criterion is satisfied.",
    }


def test_review_assessments_exactly_cover_controller_scope() -> None:
    report = ReviewReport.model_validate(valid_review_payload())

    assert report.criterion_assessments[0].criterion_id == "AC_PERSIST"
    assert report.criterion_assessments[0].tool_evidence[0].tool_call_id == ("tool-001")
    serialized = report.model_dump(mode="json")
    assert "command_evidence_ids" not in serialized["criterion_assessments"][0]
    serialized_reference = serialized["criterion_assessments"][0]["tool_evidence"][0]
    assert "execution_attempt" not in serialized_reference

    explicit = valid_review_payload()
    explicit["criterion_assessments"][0]["tool_evidence"][0]["execution_attempt"] = 1
    explicit_report = ReviewReport.model_validate(explicit)
    explicit_serialized = explicit_report.model_dump(mode="json")
    assert (
        explicit_serialized["criterion_assessments"][0]["tool_evidence"][0][
            "execution_attempt"
        ]
        == 1
    )

    with_command = valid_review_payload()
    with_command["criterion_assessments"][0]["command_evidence_ids"] = [
        "CHECK_EXACT_PROJECT_COMMANDS"
    ]
    command_report = ReviewReport.model_validate(with_command)
    assert command_report.model_dump(mode="json")["criterion_assessments"][0][
        "command_evidence_ids"
    ] == ["CHECK_EXACT_PROJECT_COMMANDS"]


def test_review_boundary_check_accepts_command_only_grounding() -> None:
    check = ReviewBoundaryCheck(
        boundary=ReviewBoundaryKind.FAILURE_PATH,
        adversarial_check="Ran the exact controller-owned failure-path command.",
        command_evidence_ids=("CHECK_EXACT_PROJECT_COMMANDS",),
    )

    serialized = check.model_dump(mode="json")

    assert serialized["command_evidence_ids"] == ["CHECK_EXACT_PROJECT_COMMANDS"]
    assert "tool_evidence" not in serialized
    with pytest.raises(ValidationError, match="command or tool evidence"):
        ReviewBoundaryCheck(
            boundary=ReviewBoundaryKind.FAILURE_PATH,
            adversarial_check="Claimed a check without attributable evidence.",
        )


def test_review_assessment_rejects_duplicate_tool_references() -> None:
    payload = valid_review_payload()
    assessments = payload["criterion_assessments"]
    assert isinstance(assessments, list)
    references = assessments[0]["tool_evidence"]
    references.append(deepcopy(references[0]))

    with pytest.raises(ValidationError, match="references must be unique"):
        ReviewReport.model_validate(payload)

    payload = valid_review_payload()
    assessments = payload["criterion_assessments"]
    assert isinstance(assessments, list)
    references = assessments[0]["tool_evidence"]
    second_attempt = deepcopy(references[0])
    second_attempt["execution_attempt"] = 2
    references.append(second_attempt)
    report = ReviewReport.model_validate(payload)
    assert [
        reference.execution_attempt
        for reference in report.criterion_assessments[0].tool_evidence
    ] == [1, 2]

    payload = valid_review_payload()
    payload["reviewed_criteria"] = ["AC_PERSIST", "AC_OTHER"]
    with pytest.raises(ValidationError, match="exactly cover"):
        ReviewReport.model_validate(payload)


def test_blocked_review_assessment_requires_matching_blocking_finding() -> None:
    payload = valid_review_payload()
    payload["verdict"] = "revise"
    assessments = payload["criterion_assessments"]
    assert isinstance(assessments, list)
    assessments[0]["status"] = "blocked"

    with pytest.raises(ValidationError, match="exactly match"):
        ReviewReport.model_validate(payload)

    payload["findings"] = [
        {
            "id": "FINDING_PERSISTENCE",
            "severity": "high",
            "blocking": True,
            "category": "behavior",
            "description": "The saved task disappeared after restart.",
            "recommendation": "Persist tasks before reporting success.",
            "criterion_ids": ["AC_PERSIST"],
        }
    ]
    report = ReviewReport.model_validate(payload)
    assert report.verdict.value == "revise"
