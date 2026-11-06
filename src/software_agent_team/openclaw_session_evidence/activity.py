"""OpenClaw session activity tracking and initialization observation.

Provides snapshot inspection, baseline capturing, and real-time activity
monitoring for OpenClaw session transcripts.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from ..terminal_presentation import (
    display_text,
    invocation_preview,
    tool_preview,
    visible_message,
)
from .parsing import (
    _classify_runtime_rejections,
    _exec_executable,
    _message_matches_prompt,
    _process_poll_identity,
    _running_process_poll_outcome,
)
from .reader import (
    _BoundOpenClawTranscript,
    _load_json_object,
    _OpenClawSessionFileMissing,
    _read_bound_transcript,
    _read_regular_file,
    _read_sqlite_bound_transcript,
    _require_safe_session_directory,
    _require_safe_sqlite_database,
    _safe_session_id,
    _sha256,
    _uses_sqlite_sessions,
)
from .types import (
    _MAX_INDEX_BYTES,
    InitializationCheckpoint,
    OpenClawInitializationBaseline,
    OpenClawInitializationObservation,
    OpenClawSessionActivity,
    OpenClawSessionEvidenceError,
    OpenClawToolActivity,
    _OpenClawSessionSnapshot,
    _safe_activity_identity,
)


def _inspect_openclaw_session_snapshot(
    *,
    state_dir: Path,
    agent_id: str,
    session_key: str,
    prompt: str,
) -> _OpenClawSessionSnapshot | None:
    """Resolve one exact session boundary and its highest safe checkpoint."""

    if _uses_sqlite_sessions(state_dir, agent_id):
        database = _require_safe_sqlite_database(state_dir, agent_id)
        directory_snapshot = _OpenClawSessionSnapshot(
            observation=OpenClawInitializationObservation(
                checkpoint=InitializationCheckpoint.SESSION_DIRECTORY
            )
        )
        if database is None:
            return directory_snapshot
        result = _read_sqlite_bound_transcript(
            state_dir, agent_id, session_key, allow_initializing=True
        )
        if result is None:
            return _OpenClawSessionSnapshot(
                observation=OpenClawInitializationObservation(
                    checkpoint=InitializationCheckpoint.SESSION_INDEX
                )
            )
        session_id, bound = result
        if bound is None:
            return _OpenClawSessionSnapshot(
                observation=OpenClawInitializationObservation(
                    checkpoint=InitializationCheckpoint.SESSION_BOUND
                ),
                session_id=session_id,
            )
        return _snapshot_from_bound_transcript(bound, session_id, prompt)

    candidate = state_dir / "agents" / agent_id / "sessions"
    try:
        candidate.lstat()
    except FileNotFoundError:
        return None
    sessions = _require_safe_session_directory(state_dir, agent_id)
    snapshot = _OpenClawSessionSnapshot(
        observation=OpenClawInitializationObservation(
            checkpoint=InitializationCheckpoint.SESSION_DIRECTORY
        )
    )
    try:
        index_payload = _read_regular_file(
            sessions / "sessions.json",
            limit=_MAX_INDEX_BYTES,
            label="OpenClaw session index",
        )
    except _OpenClawSessionFileMissing:
        return snapshot
    index = _load_json_object(index_payload, label="OpenClaw session index")
    snapshot = _OpenClawSessionSnapshot(
        observation=OpenClawInitializationObservation(
            checkpoint=InitializationCheckpoint.SESSION_INDEX
        )
    )
    entry = index.get(session_key)
    if entry is None:
        return snapshot
    if not isinstance(entry, dict):
        raise OpenClawSessionEvidenceError("OpenClaw session index entry is invalid")
    session_id = entry.get("sessionId")
    if not _safe_session_id(session_id):
        raise OpenClawSessionEvidenceError("OpenClaw session ID is unsafe")
    snapshot = _OpenClawSessionSnapshot(
        observation=OpenClawInitializationObservation(
            checkpoint=InitializationCheckpoint.SESSION_BOUND
        ),
        session_id=session_id,
    )
    try:
        bound = _read_bound_transcript(
            sessions,
            entry,
            session_key=session_key,
            session_id=session_id,
            allow_incomplete=True,
        )
    except _OpenClawSessionFileMissing:
        return snapshot
    if bound is None:
        return snapshot
    return _snapshot_from_bound_transcript(bound, session_id, prompt)


def _snapshot_from_bound_transcript(
    bound: _BoundOpenClawTranscript, session_id: str, prompt: str
) -> _OpenClawSessionSnapshot:
    """Apply the same current-turn attribution to both pinned store formats."""

    records = bound.records
    snapshot = _OpenClawSessionSnapshot(
        observation=OpenClawInitializationObservation(
            checkpoint=InitializationCheckpoint.TRANSCRIPT_HEADER
        ),
        session_id=session_id,
    )
    prompt_sha256 = _sha256(prompt.encode("utf-8"))
    matches = [
        index
        for index, record in enumerate(records)
        if record.get("type") == "message"
        and isinstance(record.get("message"), dict)
        and record["message"].get("role") == "user"
        and _message_matches_prompt(record["message"].get("content"), prompt_sha256)
    ]
    if not matches:
        return snapshot
    start = matches[-1]
    end = len(records)
    for index in range(start + 1, len(records)):
        record = records[index]
        message = record.get("message")
        if (
            record.get("type") == "message"
            and isinstance(message, dict)
            and message.get("role") == "user"
        ):
            end = index
            break
    return _OpenClawSessionSnapshot(
        observation=OpenClawInitializationObservation(
            checkpoint=InitializationCheckpoint.CURRENT_TURN
        ),
        invocation_records=records[start:end],
        session_id=session_id,
        matching_turn_count=len(matches),
        transcript_sha256=bound.sha256,
        transcript_complete=bound.complete,
    )


def capture_openclaw_initialization_baseline(
    *,
    state_dir: Path,
    agent_id: str,
    session_key: str,
    prompt: str,
) -> OpenClawInitializationBaseline:
    """Capture state published before process launch without retaining content."""

    snapshot = _inspect_openclaw_session_snapshot(
        state_dir=state_dir,
        agent_id=agent_id,
        session_key=session_key,
        prompt=prompt,
    )
    if snapshot is None:
        return OpenClawInitializationBaseline(
            checkpoint=None,
            session_id=None,
        )
    return OpenClawInitializationBaseline(
        checkpoint=snapshot.observation.checkpoint,
        session_id=snapshot.session_id,
        matching_turn_count=snapshot.matching_turn_count,
    )


def _snapshot_is_new_for_invocation(
    snapshot: _OpenClawSessionSnapshot,
    baseline: OpenClawInitializationBaseline | None,
) -> bool:
    """Exclude inherited session state from launch-to-turn progress."""

    if baseline is None or baseline.checkpoint is None:
        return True
    checkpoint = snapshot.observation.checkpoint
    if checkpoint is InitializationCheckpoint.CURRENT_TURN:
        return (
            snapshot.session_id != baseline.session_id
            or snapshot.matching_turn_count > baseline.matching_turn_count
        )
    if (
        snapshot.session_id is not None
        and baseline.session_id is not None
        and snapshot.session_id != baseline.session_id
    ):
        return True
    order = list(InitializationCheckpoint)
    return order.index(checkpoint) > order.index(baseline.checkpoint)


def inspect_openclaw_initialization(
    *,
    state_dir: Path,
    agent_id: str,
    session_key: str,
    prompt: str,
    baseline: OpenClawInitializationBaseline | None = None,
) -> OpenClawInitializationObservation | None:
    """Return finite launch-to-turn progress without retaining session content."""

    snapshot = _inspect_openclaw_session_snapshot(
        state_dir=state_dir,
        agent_id=agent_id,
        session_key=session_key,
        prompt=prompt,
    )
    if snapshot is None or not _snapshot_is_new_for_invocation(snapshot, baseline):
        return None
    return snapshot.observation


def inspect_openclaw_session_activity(
    *,
    state_dir: Path,
    agent_id: str,
    session_key: str,
    prompt: str,
    baseline: OpenClawInitializationBaseline | None = None,
    include_presentation: bool = False,
    model: str | None = None,
) -> OpenClawSessionActivity | None:
    """Inspect current-turn lifecycle records without retaining their content.

    ``None`` means the exact current turn is not ready. Malformed identities or
    records fail closed so callers never guess that a provider is active.
    """

    snapshot = _inspect_openclaw_session_snapshot(
        state_dir=state_dir,
        agent_id=agent_id,
        session_key=session_key,
        prompt=prompt,
    )
    if (
        snapshot is None
        or not _snapshot_is_new_for_invocation(snapshot, baseline)
        or snapshot.observation.checkpoint is not InitializationCheckpoint.CURRENT_TURN
        or snapshot.invocation_records is None
    ):
        return None
    invocation, _ = _classify_runtime_rejections(snapshot.invocation_records)

    started: dict[str, OpenClawToolActivity] = {}
    started_names: dict[str, str] = {}
    process_polls: dict[str, str] = {}
    process_poll_outcomes: dict[str, str] = {}
    completed: set[str] = set()
    completed_tools: list[OpenClawToolActivity] = []
    trusted_records = 0
    progress_records = 0
    repeating_no_progress_poll = False
    last_message_role: object = "user"
    last_assistant_has_tool_call = False
    for record in invocation[1:]:
        if record.get("type") != "message":
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            raise OpenClawSessionEvidenceError("OpenClaw message record is invalid")
        role = message.get("role")
        last_message_role = role
        if role == "assistant":
            trusted_records += 1
            content = message.get("content")
            # Sanitization can remove an unsupported call while retaining the
            # assistant's text. Its pinned stop reason still denotes tool use,
            # not a provider-final response.
            last_assistant_has_tool_call = message.get("stopReason") == "toolUse"
            if isinstance(content, str):
                progress_records += 1
                repeating_no_progress_poll = False
                continue
            if not isinstance(content, list):
                raise OpenClawSessionEvidenceError(
                    "OpenClaw assistant content is invalid"
                )
            has_tool_call = False
            has_non_poll_tool_call = False
            for item in content:
                if not isinstance(item, dict) or item.get("type") != "toolCall":
                    continue
                has_tool_call = True
                last_assistant_has_tool_call = True
                external_id = item.get("id")
                tool_name = item.get("name")
                if (
                    not isinstance(external_id, str)
                    or not external_id
                    or not isinstance(tool_name, str)
                    or not tool_name
                    or tool_name.strip() != tool_name
                ):
                    raise OpenClawSessionEvidenceError(
                        "OpenClaw tool-call identity is invalid"
                    )
                if external_id in started:
                    raise OpenClawSessionEvidenceError(
                        "OpenClaw session repeats a tool-call identity"
                    )
                executable = _exec_executable(tool_name, item.get("arguments"))
                started_names[external_id] = tool_name
                started[external_id] = _safe_activity_identity(
                    tool_name,
                    executable,
                    item.get("arguments"),
                )
                if include_presentation:
                    started[external_id] = replace(
                        started[external_id],
                        presentation=tool_preview(tool_name, item.get("arguments")),
                    )
                poll_identity = _process_poll_identity(
                    tool_name,
                    item.get("arguments"),
                )
                if poll_identity is None:
                    has_non_poll_tool_call = True
                else:
                    process_polls[external_id] = poll_identity
            if not has_tool_call or has_non_poll_tool_call:
                progress_records += 1
                repeating_no_progress_poll = False
        elif role == "toolResult":
            trusted_records += 1
            external_id = message.get("toolCallId")
            if not isinstance(external_id, str) or external_id not in started:
                raise OpenClawSessionEvidenceError(
                    "OpenClaw tool result has no attributable call identity"
                )
            if external_id in completed:
                raise OpenClawSessionEvidenceError(
                    "OpenClaw session repeats a tool result"
                )
            activity = started[external_id]
            if message.get("toolName") != started_names[external_id]:
                raise OpenClawSessionEvidenceError(
                    "OpenClaw tool result names a different tool"
                )
            completed.add(external_id)
            if include_presentation and activity.presentation is not None:
                activity = replace(
                    activity,
                    presentation=activity.presentation.model_copy(
                        update={
                            "completed": True,
                            "result": (
                                "typed submission received"
                                if activity.tool_name == "sat_submit_artifact"
                                else display_text(visible_message(message))
                            ),
                        }
                    ),
                )
            completed_tools.append(activity)
            poll_identity = process_polls.get(external_id)
            poll_outcome = _running_process_poll_outcome(message)
            if poll_identity is None or poll_outcome is None:
                progress_records += 1
                repeating_no_progress_poll = False
            else:
                previous_outcome = process_poll_outcomes.get(poll_identity)
                process_poll_outcomes[poll_identity] = poll_outcome
                if previous_outcome == poll_outcome:
                    repeating_no_progress_poll = True
                else:
                    progress_records += 1
                    repeating_no_progress_poll = False

    # Filtering a diagnostic must not make an earlier assistant message look
    # like the terminal response after a later runtime rejection.
    for raw_record in reversed(snapshot.invocation_records[1:]):
        if raw_record.get("type") == "message":
            raw_message = raw_record.get("message")
            if isinstance(raw_message, dict):
                last_message_role = raw_message.get("role")
            break
    return OpenClawSessionActivity(
        trusted_record_count=trusted_records,
        progress_record_count=progress_records,
        tool_started_count=len(started),
        tool_completed_count=len(completed),
        active_tool_count=len(set(started) - completed),
        repeating_no_progress_poll=repeating_no_progress_poll,
        terminal_response_observed=(
            last_message_role == "assistant"
            and not last_assistant_has_tool_call
            and not (set(started) - completed)
        ),
        started_tools=tuple(started.values()),
        completed_tools=tuple(completed_tools),
        presentation=(
            invocation_preview(invocation, session_id=snapshot.session_id, model=model)
            if include_presentation
            else None
        ),
    )
