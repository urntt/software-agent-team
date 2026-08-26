"""Tests for persisted controller-owned user-control commands."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from software_agent_team.controls import (
    ControlApplicationBoundary,
    ControlCommandConflictError,
    ControlCommandError,
    ControlCommandStatus,
    ControlCommandStore,
    ControlCommandType,
    ControlTarget,
    ControlTargetKind,
)
from software_agent_team.run_control import RunPhase

FIXED_TIME = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


def store(
    tmp_path: Path,
    *,
    clock=lambda: FIXED_TIME,
) -> ControlCommandStore:
    """Create one control mailbox below a test-owned run directory."""

    run_directory = tmp_path / "runs" / "control-run-001"
    run_directory.mkdir(parents=True)
    return ControlCommandStore(
        run_directory,
        run_id="control-run-001",
        clock=clock,
    )


def guide_target() -> ControlTarget:
    """Return the default prospective guidance target."""

    return ControlTarget(kind=ControlTargetKind.FUTURE_WORK)


def test_guide_request_is_queued_without_applying_hidden_state(tmp_path: Path) -> None:
    command_store = store(tmp_path)
    requested = command_store.request(
        command=ControlCommandType.GUIDE,
        target=guide_target(),
        application_boundary=ControlApplicationBoundary.NEXT_SAFE_CHECKPOINT,
        instruction="Prefer a smaller dependency surface.",
        command_id="ctl-guide-001",
    )

    assert requested.status is ControlCommandStatus.QUEUED
    assert requested.revision == 1
    assert requested.consequence is None
    assert command_store.load(requested.command_id) == (requested,)
    assert command_store.list_latest() == (requested,)


def test_controller_resolution_appends_a_hash_chained_revision(tmp_path: Path) -> None:
    times = iter([FIXED_TIME, FIXED_TIME + timedelta(seconds=2)])
    command_store = store(tmp_path, clock=lambda: next(times))
    requested = command_store.request(
        command=ControlCommandType.CORRECT,
        target=ControlTarget(kind=ControlTargetKind.RUN),
        application_boundary=ControlApplicationBoundary.PLANNING_REVISION,
        instruction="The persistence requirement is wrong.",
        command_id="ctl-correct-001",
    )
    resolved = command_store.resolve(
        requested.command_id,
        expected_revision=1,
        status=ControlCommandStatus.APPLIED,
        consequence="Scheduling stopped and Planning revision 2 was opened.",
        resulting_plan_revision=2,
        resulting_lifecycle_revision=8,
    )

    assert resolved.revision == 2
    assert resolved.status is ControlCommandStatus.APPLIED
    assert resolved.previous_revision_sha256 is not None
    assert command_store.load(requested.command_id) == (requested, resolved)
    assert command_store.list_latest() == (resolved,)


def test_obsolete_control_revision_is_rejected(tmp_path: Path) -> None:
    command_store = store(tmp_path)
    requested = command_store.request(
        command=ControlCommandType.CANCEL,
        target=ControlTarget(kind=ControlTargetKind.RUN),
        application_boundary=ControlApplicationBoundary.IMMEDIATE,
        command_id="ctl-cancel-001",
    )
    command_store.resolve(
        requested.command_id,
        expected_revision=1,
        status=ControlCommandStatus.BEST_EFFORT_FAILED,
        consequence="The provider call had already returned.",
        provider_cost_caveat="Usage incurred before cancellation remains billable.",
    )

    with pytest.raises(ControlCommandConflictError, match="expected 1, found 2"):
        command_store.resolve(
            requested.command_id,
            expected_revision=1,
            status=ControlCommandStatus.REJECTED,
            consequence="This obsolete request cannot be resolved again.",
        )


@pytest.mark.parametrize(
    ("command", "target", "boundary", "instruction", "message"),
    [
        (
            ControlCommandType.GUIDE,
            ControlTarget(kind=ControlTargetKind.FUTURE_WORK),
            ControlApplicationBoundary.NEXT_SAFE_CHECKPOINT,
            None,
            "guide requires an instruction",
        ),
        (
            ControlCommandType.PAUSE,
            ControlTarget(kind=ControlTargetKind.RUN),
            ControlApplicationBoundary.NEXT_SAFE_CHECKPOINT,
            "Pause after tests.",
            "pause does not accept an instruction",
        ),
        (
            ControlCommandType.INTERRUPT,
            ControlTarget(kind=ControlTargetKind.RUN),
            ControlApplicationBoundary.IMMEDIATE,
            None,
            "invalid target for interrupt",
        ),
        (
            ControlCommandType.CANCEL,
            ControlTarget(kind=ControlTargetKind.RUN),
            ControlApplicationBoundary.NEXT_SAFE_CHECKPOINT,
            None,
            "invalid application boundary for cancel",
        ),
    ],
)
def test_command_semantics_are_validated_before_queueing(
    tmp_path: Path,
    command: ControlCommandType,
    target: ControlTarget,
    boundary: ControlApplicationBoundary,
    instruction: str | None,
    message: str,
) -> None:
    command_store = store(tmp_path)

    with pytest.raises(ValidationError, match=message):
        command_store.request(
            command=command,
            target=target,
            application_boundary=boundary,
            instruction=instruction,
        )


def test_agent_target_requires_a_valid_agent_identity() -> None:
    with pytest.raises(ValidationError, match="require an Agent ID"):
        ControlTarget(kind=ControlTargetKind.AGENT)

    with pytest.raises(ValidationError, match="only phase control targets"):
        ControlTarget(kind=ControlTargetKind.RUN, phase=RunPhase.IMPLEMENTING)


def test_duplicate_command_id_never_overwrites_a_request(tmp_path: Path) -> None:
    command_store = store(tmp_path)
    arguments = {
        "command": ControlCommandType.PAUSE,
        "target": ControlTarget(kind=ControlTargetKind.RUN),
        "application_boundary": ControlApplicationBoundary.NEXT_SAFE_CHECKPOINT,
        "command_id": "ctl-pause-001",
    }
    original = command_store.request(**arguments)

    with pytest.raises(ControlCommandError, match="already exists"):
        command_store.request(**arguments)

    assert command_store.load(original.command_id) == (original,)


def test_control_history_detects_tampering(tmp_path: Path) -> None:
    command_store = store(tmp_path)
    requested = command_store.request(
        command=ControlCommandType.GUIDE,
        target=guide_target(),
        application_boundary=ControlApplicationBoundary.BEFORE_NEXT_INVOCATION,
        instruction="Use the verified schema.",
        command_id="ctl-guide-002",
    )
    command_store.resolve(
        requested.command_id,
        expected_revision=1,
        status=ControlCommandStatus.APPLIED,
        consequence="Guidance was attached to future work.",
    )
    first_path = command_store.controls_directory / requested.command_id / "000001.json"
    payload = json.loads(first_path.read_text(encoding="utf-8"))
    payload["instruction"] = "Tampered guidance."
    first_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ControlCommandError, match="predecessor digest"):
        command_store.load(requested.command_id)


def test_control_request_metadata_cannot_change_between_revisions(
    tmp_path: Path,
) -> None:
    command_store = store(tmp_path)
    requested = command_store.request(
        command=ControlCommandType.PAUSE,
        target=ControlTarget(kind=ControlTargetKind.RUN),
        application_boundary=ControlApplicationBoundary.NEXT_SAFE_CHECKPOINT,
        command_id="ctl-pause-002",
    )
    command_store.resolve(
        requested.command_id,
        expected_revision=1,
        status=ControlCommandStatus.APPLIED,
        consequence="The run reached a safe pause boundary.",
        resulting_lifecycle_revision=6,
    )
    second_path = (
        command_store.controls_directory / requested.command_id / "000002.json"
    )
    payload = json.loads(second_path.read_text(encoding="utf-8"))
    payload["command"] = ControlCommandType.RESUME.value
    second_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ControlCommandError, match="metadata is immutable"):
        command_store.load(requested.command_id)


def test_unsafe_command_ids_are_rejected(tmp_path: Path) -> None:
    command_store = store(tmp_path)

    with pytest.raises(ControlCommandError, match="invalid control command ID"):
        command_store.load("../escaped")


def test_mailbox_rejects_unowned_entries(tmp_path: Path) -> None:
    command_store = store(tmp_path)
    (command_store.controls_directory / "unexpected.txt").write_text(
        "not a control command",
        encoding="utf-8",
    )

    with pytest.raises(ControlCommandError, match="invalid entry"):
        command_store.list_latest()
