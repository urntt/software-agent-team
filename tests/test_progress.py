"""Tests for persisted controller events and terminal rendering."""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

from software_agent_team.budgets import AgentBudgetUsage
from software_agent_team.integrity import canonical_model_sha256
from software_agent_team.invocation_lifecycle import InvocationPhase
from software_agent_team.progress import (
    ProgressCheckpointSnapshot,
    ProgressEvent,
    ProgressEventKind,
    RunEvent,
    RunEventJournal,
    RunEventVisibility,
    TerminalProgressRenderer,
)
from software_agent_team.run_control import RunPhase

FIXED_TIME = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


def journal(
    tmp_path: Path,
    *,
    handler=None,
) -> RunEventJournal:
    """Create a journal below one test-owned run directory."""

    run_directory = tmp_path / "runs" / "event-run-001"
    run_directory.mkdir(parents=True)
    return RunEventJournal(
        run_directory,
        run_id="event-run-001",
        handler=handler,
        clock=lambda: FIXED_TIME,
    )


def test_progress_renderer_shows_elapsed_waiting_and_verified_completion(
    tmp_path: Path,
) -> None:
    output = StringIO()
    renderer = TerminalProgressRenderer(output=output, heartbeat_seconds=0.01)
    event_journal = journal(tmp_path, handler=renderer)
    event_journal.append(
        ProgressEvent(
            kind=ProgressEventKind.AGENT_STARTED,
            message="Planner is working",
            agent_id="planner",
            iteration=1,
            attempt=1,
        ),
        lifecycle_revision=2,
        phase=RunPhase.PLANNING,
    )
    time.sleep(0.03)
    event_journal.append(
        ProgressEvent(
            kind=ProgressEventKind.AGENT_COMPLETED,
            message="Planner response recorded (0.1s)",
            agent_id="planner",
            iteration=1,
            attempt=1,
            duration_ms=100,
        ),
        lifecycle_revision=2,
        phase=RunPhase.PLANNING,
    )
    renderer.close()

    rendered = output.getvalue()
    assert "Planner is working" in rendered
    assert "elapsed" in rendered
    assert "Planner response recorded" in rendered
    assert "reasoning" not in rendered.casefold()


def test_progress_renderer_closes_multiple_independent_verifiers(
    tmp_path: Path,
) -> None:
    output = StringIO()
    renderer = TerminalProgressRenderer(output=output, heartbeat_seconds=1)
    event_journal = journal(tmp_path, handler=renderer)
    for agent_id in ("tester", "reviewer"):
        event_journal.append(
            ProgressEvent(
                kind=ProgressEventKind.AGENT_STARTED,
                message=f"{agent_id} is working",
                agent_id=agent_id,
                iteration=1,
                attempt=1,
            ),
            lifecycle_revision=5,
            phase=RunPhase.VERIFYING,
        )

    renderer.close()

    assert "tester is working" in output.getvalue()
    assert "reviewer is working" in output.getvalue()


def test_hidden_invocation_checkpoint_stops_standard_heartbeat(
    tmp_path: Path,
) -> None:
    output = StringIO()
    renderer = TerminalProgressRenderer(output=output, heartbeat_seconds=0.01)
    event_journal = journal(tmp_path, handler=renderer)
    event_journal.append(
        ProgressEvent(
            kind=ProgressEventKind.AGENT_WAITING_PROVIDER,
            message="Builder is waiting for provider/model",
            agent_id="builder",
            iteration=1,
            attempt=2,
        ),
        lifecycle_revision=3,
        phase=RunPhase.IMPLEMENTING,
    )
    time.sleep(0.025)
    event_journal.append(
        ProgressEvent(
            kind=ProgressEventKind.AGENT_INVOCATION_COMPLETED,
            message="Builder invocation 2 returned completed",
            agent_id="builder",
            iteration=1,
            attempt=2,
            duration_ms=25,
        ),
        lifecycle_revision=3,
        phase=RunPhase.IMPLEMENTING,
    )
    rendered_at_completion = output.getvalue()
    time.sleep(0.025)
    renderer.close()

    assert "builder is waiting for the model" in rendered_at_completion
    assert output.getvalue() == rendered_at_completion


def test_scheduler_terminal_event_stops_every_repair_attempt_heartbeat(
    tmp_path: Path,
) -> None:
    output = StringIO()
    renderer = TerminalProgressRenderer(output=output, heartbeat_seconds=0.01)
    event_journal = journal(tmp_path, handler=renderer)
    event_journal.append(
        ProgressEvent(
            kind=ProgressEventKind.AGENT_WAITING_PROVIDER,
            message="Builder is waiting after repair",
            agent_id="builder",
            iteration=1,
            attempt=2,
        ),
        lifecycle_revision=3,
        phase=RunPhase.IMPLEMENTING,
    )
    time.sleep(0.025)
    event_journal.append(
        ProgressEvent(
            kind=ProgressEventKind.AGENT_COMPLETED,
            message="Builder completed",
            agent_id="builder",
            iteration=1,
            attempt=1,
            duration_ms=50,
        ),
        lifecycle_revision=4,
        phase=RunPhase.SNAPSHOTTING,
    )
    rendered_at_completion = output.getvalue()
    time.sleep(0.025)
    renderer.close()

    assert output.getvalue() == rendered_at_completion


def test_progress_renderer_distinguishes_failed_and_passing_quality_gates(
    tmp_path: Path,
) -> None:
    output = StringIO()
    renderer = TerminalProgressRenderer(output=output)
    event_journal = journal(tmp_path, handler=renderer)
    for completed, kind, state in (
        (1, ProgressEventKind.QUALITY_GATE_FAILED, "failed"),
        (2, ProgressEventKind.QUALITY_GATE_PASSED, "passed"),
    ):
        event_journal.append(
            ProgressEvent(
                kind=kind,
                message=f"Quality gate {completed}/2 CHECK_{completed}: {state}",
                iteration=1,
                completed=completed,
                total=2,
            ),
            lifecycle_revision=5,
            phase=RunPhase.VERIFYING,
        )

    rendered = output.getvalue()
    assert "✗ Quality gate 1/2 CHECK_1: failed" in rendered
    assert "✓ Quality gate 2/2 CHECK_2: passed" in rendered


def test_detailed_renderer_projects_agent_state_route_dependencies_and_budget(
    tmp_path: Path,
) -> None:
    output = StringIO()
    renderer = TerminalProgressRenderer(
        output=output,
        visibility=RunEventVisibility.DETAILED,
        heartbeat_seconds=1,
    )
    event_journal = journal(tmp_path, handler=renderer)
    common = {
        "agent_id": "api_builder",
        "iteration": 1,
        "capability": "implementation",
        "stage_id": "implement",
        "model": "provider/model",
        "dependency_ids": ("schema_builder",),
    }
    event_journal.append(
        ProgressEvent(
            kind=ProgressEventKind.AGENT_QUEUED,
            message="API Builder queued after schema_builder.",
            **common,
        ),
        lifecycle_revision=3,
        phase=RunPhase.IMPLEMENTING,
    )
    event_journal.append(
        ProgressEvent(
            kind=ProgressEventKind.AGENT_WAITING_PROVIDER,
            message="API Builder is waiting for provider/model",
            attempt=1,
            **common,
        ),
        lifecycle_revision=3,
        phase=RunPhase.IMPLEMENTING,
    )
    event_journal.append(
        ProgressEvent(
            kind=ProgressEventKind.AGENT_INVOCATION_COMPLETED,
            message="API Builder invocation 1 returned completed",
            attempt=1,
            duration_ms=250,
            budget_usage=AgentBudgetUsage(
                calls_started=1,
                calls_completed=1,
                active_calls=0,
                input_tokens=120,
                output_tokens=40,
                agent_duration_ms=250,
                known_estimated_cost_usd="0.01",
                unpriced_calls=0,
                unreported_token_calls=0,
            ),
            **common,
        ),
        lifecycle_revision=3,
        phase=RunPhase.IMPLEMENTING,
    )
    renderer.close()

    rendered = output.getvalue()
    assert "state=queued" in rendered
    assert "model=provider/model" in rendered
    assert "dependencies=schema_builder" in rendered
    assert "input=120 output=40" in rendered


def test_standard_renderer_shows_model_spend_after_each_invocation(
    tmp_path: Path,
) -> None:
    output = StringIO()
    renderer = TerminalProgressRenderer(
        output=output,
        visibility=RunEventVisibility.STANDARD,
    )
    event_journal = journal(tmp_path, handler=renderer)
    event_journal.append(
        ProgressEvent(
            kind=ProgressEventKind.AGENT_INVOCATION_COMPLETED,
            message=(
                "Builder invocation 1 returned completed; task model spend "
                "$0.010000 estimated / $1.00 authorized, $0.990000 recorded "
                "remaining (price source runtime_catalog)"
            ),
            agent_id="builder",
            iteration=1,
            attempt=1,
            budget_usage=AgentBudgetUsage(
                calls_started=1,
                calls_completed=1,
                active_calls=0,
                input_tokens=120,
                output_tokens=40,
                agent_duration_ms=250,
                known_estimated_cost_usd="0.01",
                unpriced_calls=0,
                unreported_token_calls=0,
            ),
        ),
        lifecycle_revision=3,
        phase=RunPhase.IMPLEMENTING,
    )

    rendered = output.getvalue()
    assert "task model spend $0.010000 estimated / $1.00 authorized" in rendered
    assert "known_cost_usd=" not in rendered


def test_liveness_events_keep_stall_visible_and_stream_detail_optional(
    tmp_path: Path,
) -> None:
    compact_output = StringIO()
    detailed_output = StringIO()
    compact_journal = journal(
        tmp_path / "compact",
        handler=TerminalProgressRenderer(
            output=compact_output,
            visibility=RunEventVisibility.COMPACT,
        ),
    )
    detailed_journal = journal(
        tmp_path / "detailed",
        handler=TerminalProgressRenderer(
            output=detailed_output,
            visibility=RunEventVisibility.DETAILED,
        ),
    )
    drafts = (
        ProgressEvent(
            kind=ProgressEventKind.AGENT_PROVIDER_ACTIVITY,
            message="Builder received provider stream activity",
            agent_id="builder",
            iteration=1,
            attempt=1,
        ),
        ProgressEvent(
            kind=ProgressEventKind.AGENT_STALL_SUSPECTED,
            message=(
                "Builder has produced no trusted activity for 90.0s; checking "
                "for another 30s before interruption (test provider contract)"
            ),
            agent_id="builder",
            iteration=1,
            attempt=1,
        ),
        ProgressEvent(
            kind=ProgressEventKind.AGENT_PROVIDER_STALLED,
            message=(
                "Builder provider remained silent for 120s; interrupting only "
                "this invocation and preserving its evidence"
            ),
            agent_id="builder",
            iteration=1,
            attempt=1,
        ),
    )
    for draft in drafts:
        compact_journal.append(
            draft,
            lifecycle_revision=3,
            phase=RunPhase.IMPLEMENTING,
        )
        detailed_journal.append(
            draft,
            lifecycle_revision=3,
            phase=RunPhase.IMPLEMENTING,
        )

    assert "provider stream activity" not in compact_output.getvalue()
    assert "no trusted activity for 90.0s" in compact_output.getvalue()
    assert "preserving its evidence" in compact_output.getvalue()
    assert "provider stream activity" in detailed_output.getvalue()


def test_checkpoint_projection_is_hidden_in_compact_and_explained_in_standard(
    tmp_path: Path,
) -> None:
    snapshot = ProgressCheckpointSnapshot(
        approved_task_ids=("TASK_BUILD",),
        invocation_phase=InvocationPhase.TOOL_ACTIVE,
        last_verified_checkpoint="Completed 2 attributable tool operations",
        next_controller_checkpoint="Observe completion of the active operation",
        completed_tool_operations=2,
        git_state="working",
        gate_state="not_started",
        review_state="not_applicable",
        known_estimated_cost_usd="0.125",
        authorized_cost_usd="1.00",
        remaining_estimated_cost_usd="0.875",
    )
    compact_output = StringIO()
    standard_output = StringIO()
    for root, output, visibility in (
        (tmp_path / "compact", compact_output, RunEventVisibility.COMPACT),
        (tmp_path / "standard", standard_output, RunEventVisibility.STANDARD),
    ):
        journal(
            root,
            handler=TerminalProgressRenderer(
                output=output,
                visibility=visibility,
                heartbeat_seconds=1,
            ),
        ).append(
            ProgressEvent(
                kind=ProgressEventKind.AGENT_TOOL_ACTIVE,
                message="Builder has an attributable tool operation active",
                agent_id="builder",
                iteration=1,
                attempt=1,
                checkpoint=snapshot,
            ),
            lifecycle_revision=3,
            phase=RunPhase.IMPLEMENTING,
        )

    assert "progress phase=" not in compact_output.getvalue()
    rendered = standard_output.getvalue()
    assert "phase=tool_active tasks=TASK_BUILD" in rendered
    assert "Completed 2 attributable tool operations" in rendered
    assert "next=Observe completion of the active operation" in rendered
    assert "$0.125000 estimated / $1.00 authorized" in rendered


def test_stopping_transition_prevents_stale_working_heartbeat(tmp_path: Path) -> None:
    output = StringIO()
    renderer = TerminalProgressRenderer(output=output, heartbeat_seconds=0.01)
    event_journal = journal(tmp_path, handler=renderer)
    event_journal.append(
        ProgressEvent(
            kind=ProgressEventKind.AGENT_WAITING_PROVIDER,
            message="Builder is waiting for the approved model",
            agent_id="builder",
            iteration=1,
            attempt=1,
        ),
        lifecycle_revision=3,
        phase=RunPhase.IMPLEMENTING,
    )
    time.sleep(0.025)
    event_journal.append(
        ProgressEvent(
            kind=ProgressEventKind.AGENT_STOPPING,
            message="Builder is stopping after a user interrupt",
            agent_id="builder",
            iteration=1,
            attempt=1,
        ),
        lifecycle_revision=3,
        phase=RunPhase.IMPLEMENTING,
    )
    stopped_at = output.getvalue()
    time.sleep(0.025)
    renderer.close()

    assert "waiting for the model" in stopped_at
    assert "stopping after a user interrupt" in stopped_at
    assert output.getvalue() == stopped_at


def test_schema_two_run_event_round_trips_without_new_checkpoint_bytes(
    tmp_path: Path,
) -> None:
    persisted = journal(tmp_path).append(
        ProgressEvent(
            kind=ProgressEventKind.AGENT_STARTED,
            message="Builder started",
            agent_id="builder",
            iteration=1,
            attempt=1,
        ),
        lifecycle_revision=2,
        phase=RunPhase.IMPLEMENTING,
    )
    payload = persisted.model_dump(mode="json")
    payload["schema_version"] = 2
    assert "checkpoint" not in payload

    restored = RunEvent.model_validate_json(
        json.dumps(payload, ensure_ascii=False, sort_keys=True)
    )

    assert restored.schema_version == 2
    assert restored.checkpoint is None
    assert restored.model_dump(mode="json") == payload


def test_journal_persists_a_contiguous_hash_chain(tmp_path: Path) -> None:
    event_journal = journal(tmp_path)
    first = event_journal.append(
        ProgressEvent(
            kind=ProgressEventKind.RUN_STARTED,
            message="Build started",
            phase=RunPhase.CREATED,
            iteration=1,
        ),
        lifecycle_revision=0,
        phase=RunPhase.CREATED,
    )
    second = event_journal.append(
        ProgressEvent(
            kind=ProgressEventKind.WORKSPACE_READY,
            message="Workspace ready",
            phase=RunPhase.PLANNING,
            iteration=1,
        ),
        lifecycle_revision=2,
        phase=RunPhase.PLANNING,
    )

    assert first.sequence == 1
    assert first.previous_event_sha256 is None
    assert second.sequence == 2
    assert second.previous_event_sha256 is not None
    assert event_journal.load() == (first, second)
    assert sorted(path.name for path in event_journal.events_directory.iterdir()) == [
        "000001.json",
        "000002.json",
    ]


def test_journal_rejects_a_phase_not_owned_by_controller_state(
    tmp_path: Path,
) -> None:
    event_journal = journal(tmp_path)

    with pytest.raises(ValueError, match="phase differs"):
        event_journal.append(
            ProgressEvent(
                kind=ProgressEventKind.WORKSPACE_READY,
                message="Workspace ready",
                phase=RunPhase.PLANNING,
                iteration=1,
            ),
            lifecycle_revision=1,
            phase=RunPhase.PREPARING_WORKSPACE,
        )


def test_journal_detects_tampering_in_an_earlier_event(tmp_path: Path) -> None:
    event_journal = journal(tmp_path)
    for kind, message, revision, phase in (
        (ProgressEventKind.RUN_STARTED, "Build started", 0, RunPhase.CREATED),
        (
            ProgressEventKind.WORKSPACE_READY,
            "Workspace ready",
            2,
            RunPhase.PLANNING,
        ),
    ):
        event_journal.append(
            ProgressEvent(kind=kind, message=message, phase=phase, iteration=1),
            lifecycle_revision=revision,
            phase=phase,
        )
    first_path = event_journal.events_directory / "000001.json"
    payload = json.loads(first_path.read_text(encoding="utf-8"))
    payload["summary"] = "Tampered summary"
    first_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="predecessor digest"):
        event_journal.load()


def test_run_state_anchor_detects_tampering_in_the_latest_event(
    tmp_path: Path,
) -> None:
    run_directory = tmp_path / "runs" / "anchored-run-001"
    run_directory.mkdir(parents=True)
    anchor: dict[str, Any] = {"count": 0, "digest": None}

    def write_anchor(event) -> None:
        anchor["count"] = event.sequence
        anchor["digest"] = canonical_model_sha256(event)

    event_journal = RunEventJournal(
        run_directory,
        run_id="anchored-run-001",
        clock=lambda: FIXED_TIME,
        anchor_writer=write_anchor,
        anchor_reader=lambda: (anchor["count"], anchor["digest"]),
    )
    event_journal.append(
        ProgressEvent(
            kind=ProgressEventKind.RUN_STARTED,
            message="Build started",
            phase=RunPhase.CREATED,
            iteration=1,
        ),
        lifecycle_revision=0,
        phase=RunPhase.CREATED,
    )
    latest_path = event_journal.events_directory / "000001.json"
    payload = json.loads(latest_path.read_text(encoding="utf-8"))
    payload["summary"] = "Tampered latest summary"
    latest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="run-state anchor"):
        event_journal.load()


def test_concurrent_agent_events_receive_unique_sequences(tmp_path: Path) -> None:
    event_journal = journal(tmp_path)

    def append(index: int) -> None:
        event_journal.append(
            ProgressEvent(
                kind=ProgressEventKind.AGENT_STARTED,
                message=f"Agent {index} started",
                agent_id=f"worker_{index}",
                iteration=1,
                attempt=1,
            ),
            lifecycle_revision=4,
            phase=RunPhase.VERIFYING,
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        tuple(pool.map(append, range(1, 9)))

    events = event_journal.load()
    assert [event.sequence for event in events] == list(range(1, 9))
    assert {event.agent_id for event in events} == {
        f"worker_{index}" for index in range(1, 9)
    }


def test_renderer_failure_does_not_change_persisted_execution_state(
    tmp_path: Path,
) -> None:
    def broken_renderer(event) -> None:
        raise RuntimeError(f"cannot render event {event.sequence}")

    event_journal = journal(tmp_path, handler=broken_renderer)
    event = event_journal.append(
        ProgressEvent(
            kind=ProgressEventKind.RUN_STARTED,
            message="Build started",
            phase=RunPhase.CREATED,
            iteration=1,
        ),
        lifecycle_revision=0,
        phase=RunPhase.CREATED,
    )

    assert event_journal.load() == (event,)
    assert event_journal.render_errors == ["RuntimeError: cannot render event 1"]


def test_compact_visibility_hides_standard_detail(tmp_path: Path) -> None:
    output = StringIO()
    renderer = TerminalProgressRenderer(
        output=output,
        visibility=RunEventVisibility.COMPACT,
    )
    event_journal = journal(tmp_path, handler=renderer)
    event_journal.append(
        ProgressEvent(
            kind=ProgressEventKind.RUN_STARTED,
            message="Visible run summary",
            phase=RunPhase.CREATED,
            iteration=1,
        ),
        lifecycle_revision=0,
        phase=RunPhase.CREATED,
    )
    event_journal.append(
        ProgressEvent(
            kind=ProgressEventKind.WORKSPACE_READY,
            message="Hidden workspace detail",
            phase=RunPhase.PLANNING,
            iteration=1,
        ),
        lifecycle_revision=2,
        phase=RunPhase.PLANNING,
    )

    assert "Visible run summary" in output.getvalue()
    assert "Hidden workspace detail" not in output.getvalue()
