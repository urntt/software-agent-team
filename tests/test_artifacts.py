"""Tests for task-brief and cross-Agent handoff contracts."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from software_agent_team.artifacts import (
    HandoffEnvelope,
    HandoffStatus,
    TaskBrief,
)


def valid_handoff_payload() -> dict[str, object]:
    """Return a minimal valid implementation handoff."""

    return {
        "run_id": "task-manager-001",
        "team_id": "function_specialized",
        "iteration": 1,
        "source_role": "generalist_developer",
        "target_role": "tester",
        "status": "completed",
        "summary": "Implemented the first task workflow.",
        "input_commit": "3a12f72",
        "artifacts": [
            {
                "kind": "work_result",
                "path": "iterations/01/work-result.json",
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


def test_handoff_must_cross_a_role_boundary() -> None:
    payload = valid_handoff_payload()
    payload["target_role"] = "generalist_developer"

    with pytest.raises(ValidationError, match="must differ"):
        HandoffEnvelope.model_validate(payload)


def test_confirmed_task_brief_is_accepted() -> None:
    task_brief = TaskBrief.model_validate(valid_task_brief_payload())

    assert task_brief.confirmed
    assert task_brief.acceptance_criteria[0].id == "AC_PERSIST"


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
