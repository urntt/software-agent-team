"""OpenClaw session tool evidence and terminal diagnostic capture.

Extracts verifiable tool evidence, terminal diagnostics, and invocation
responses from OpenClaw session state transcripts.
"""

from __future__ import annotations

from pathlib import Path

from software_agent_team.submissions import ARTIFACT_SUBMISSION_TOOL

from .activity import (
    _inspect_openclaw_session_snapshot,
    _snapshot_is_new_for_invocation,
)
from .parsing import (
    _classify_runtime_rejections,
    _current_invocation_records,
    _extract_tool_calls,
    _invocation_terminal_state,
    _invocation_usage,
    _optional_usage_identity,
)
from .reader import (
    _load_json_object,
    _read_bound_transcript,
    _read_regular_file,
    _read_sqlite_bound_transcript,
    _require_safe_session_directory,
    _safe_session_id,
    _uses_sqlite_sessions,
)
from .types import (
    _MAX_INDEX_BYTES,
    CapturedOpenClawInvocationDiagnostic,
    CapturedOpenClawTerminalDiagnostic,
    CapturedOpenClawTerminalResponse,
    CapturedOpenClawToolEvidence,
    InitializationCheckpoint,
    OpenClawInitializationBaseline,
    OpenClawInvocationTerminalState,
    OpenClawSessionEvidenceError,
    _OpenClawTerminalContext,
)


def capture_openclaw_tool_evidence(
    *,
    state_dir: Path,
    agent_id: str,
    session_key: str,
    session_id: str,
    prompt: str,
) -> CapturedOpenClawToolEvidence:
    """Capture only the exact current invocation from SAT's pinned session state."""

    if not _safe_session_id(session_id):
        raise OpenClawSessionEvidenceError("OpenClaw session ID is unsafe")
    if _uses_sqlite_sessions(state_dir, agent_id):
        result = _read_sqlite_bound_transcript(
            state_dir, agent_id, session_key, expected_session_id=session_id
        )
        if result is None:
            raise OpenClawSessionEvidenceError(
                "OpenClaw SQLite session does not bind the invocation"
            )
        bound = result[1]
    else:
        sessions = _require_safe_session_directory(state_dir, agent_id)
        index_payload = _read_regular_file(
            sessions / "sessions.json",
            limit=_MAX_INDEX_BYTES,
            label="OpenClaw session index",
        )
        index = _load_json_object(index_payload, label="OpenClaw session index")
        entry = index.get(session_key)
        if not isinstance(entry, dict) or entry.get("sessionId") != session_id:
            raise OpenClawSessionEvidenceError(
                "OpenClaw session index does not bind the invocation session"
            )
        bound = _read_bound_transcript(
            sessions,
            entry,
            session_key=session_key,
            session_id=session_id,
            allow_incomplete=False,
        )
    if bound is None:
        raise OpenClawSessionEvidenceError("OpenClaw session transcript is missing")
    assert bound is not None
    invocation = _current_invocation_records(bound.records, prompt=prompt)
    execution_records, runtime_rejections = _classify_runtime_rejections(invocation)
    terminal_state = _invocation_terminal_state(invocation)
    last_message_index = max(
        index
        for index, record in enumerate(invocation)
        if record.get("type") == "message"
    )
    if any(item.record_index == last_message_index for item in runtime_rejections):
        terminal_state = OpenClawInvocationTerminalState.RUNTIME_REJECTION
    return CapturedOpenClawToolEvidence(
        transcript_sha256=bound.sha256,
        record_count=len(invocation),
        tool_calls=_extract_tool_calls(execution_records),
        terminal_state=terminal_state,
        runtime_rejections=runtime_rejections,
    )


def _capture_openclaw_terminal_context(
    *,
    state_dir: Path,
    agent_id: str,
    session_key: str,
    prompt: str,
    baseline: OpenClawInitializationBaseline | None,
) -> _OpenClawTerminalContext:
    """Bind one fresh terminal assistant record before interpreting its outcome."""

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
        or snapshot.session_id is None
        or snapshot.transcript_sha256 is None
    ):
        raise OpenClawSessionEvidenceError(
            "OpenClaw terminal response is not attributable to this invocation"
        )
    if not snapshot.transcript_complete:
        raise OpenClawSessionEvidenceError("OpenClaw terminal transcript is incomplete")
    invocation = snapshot.invocation_records
    execution_records, runtime_rejections = _classify_runtime_rejections(invocation)
    last_message_index = max(
        index
        for index, record in enumerate(invocation)
        if record.get("type") == "message"
    )
    terminal_record = invocation[last_message_index]
    terminal_message = terminal_record.get("message")
    if not isinstance(terminal_message, dict) or terminal_message.get("role") != (
        "assistant"
    ):
        raise OpenClawSessionEvidenceError(
            "OpenClaw invocation has no terminal assistant response"
        )
    if any(item.record_index == last_message_index for item in runtime_rejections):
        raise OpenClawSessionEvidenceError(
            "OpenClaw terminal response is a runtime rejection"
        )
    return _OpenClawTerminalContext(
        session_id=snapshot.session_id,
        transcript_sha256=snapshot.transcript_sha256,
        invocation=invocation,
        execution_records=execution_records,
        runtime_rejections=runtime_rejections,
        terminal_state=OpenClawInvocationTerminalState.ASSISTANT_RESPONSE,
        terminal_message=terminal_message,
    )


def _submission_tool_call_observed(
    records: tuple[dict[str, object], ...],
) -> bool:
    """Detect a semantic submission attempt without reading its arguments."""

    for record in records:
        message = record.get("message") if record.get("type") == "message" else None
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        for item in content if isinstance(content, list) else ():
            if (
                isinstance(item, dict)
                and item.get("type") == "toolCall"
                and item.get("name") == ARTIFACT_SUBMISSION_TOOL
            ):
                return True
    return False


def capture_openclaw_terminal_diagnostic(
    *,
    state_dir: Path,
    agent_id: str,
    session_key: str,
    prompt: str,
    baseline: OpenClawInitializationBaseline | None,
) -> CapturedOpenClawTerminalDiagnostic:
    """Recover safe identity and usage without accepting invalid terminal tools."""

    context = _capture_openclaw_terminal_context(
        state_dir=state_dir,
        agent_id=agent_id,
        session_key=session_key,
        prompt=prompt,
        baseline=baseline,
    )
    terminal_message = context.terminal_message
    provider = _optional_usage_identity(
        terminal_message.get("provider"), label="provider"
    )
    model = _optional_usage_identity(terminal_message.get("model"), label="model")
    usage_buckets = _invocation_usage(
        context.invocation,
        provider=provider,
        model=model,
        runtime_rejections=context.runtime_rejections,
    )
    return CapturedOpenClawTerminalDiagnostic(
        session_id=context.session_id,
        provider=provider,
        model=model,
        input_tokens=usage_buckets["input"],
        output_tokens=usage_buckets["output"],
        cache_read_tokens=usage_buckets["cacheRead"],
        cache_write_tokens=usage_buckets["cacheWrite"],
        reasoning_tokens=usage_buckets["reasoningTokens"],
        total_tokens=usage_buckets["total"],
        transcript_sha256=context.transcript_sha256,
        record_count=len(context.invocation),
        submission_tool_call_observed=_submission_tool_call_observed(
            context.invocation
        ),
    )


def capture_openclaw_invocation_diagnostic(
    *,
    state_dir: Path,
    agent_id: str,
    session_key: str,
    prompt: str,
    baseline: OpenClawInitializationBaseline | None,
) -> CapturedOpenClawInvocationDiagnostic:
    """Recover identity from a fresh current turn that has no terminal response.

    This projection deliberately omits usage. A runtime failure such as a
    compaction timeout can consume an additional model call that never reaches
    the transcript, so summing the surviving assistant records would undercount.
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
        or snapshot.session_id is None
        or snapshot.transcript_sha256 is None
    ):
        raise OpenClawSessionEvidenceError(
            "OpenClaw invocation diagnostic is not attributable to this invocation"
        )
    if not snapshot.transcript_complete:
        raise OpenClawSessionEvidenceError(
            "OpenClaw invocation diagnostic transcript is incomplete"
        )

    identities: list[tuple[str, str]] = []
    for record in snapshot.invocation_records:
        message = record.get("message") if record.get("type") == "message" else None
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        provider = _optional_usage_identity(message.get("provider"), label="provider")
        model = _optional_usage_identity(message.get("model"), label="model")
        if provider is None and model is None:
            continue
        if provider is None or model is None:
            raise OpenClawSessionEvidenceError(
                "OpenClaw invocation diagnostic has incomplete route identity"
            )
        identity = (provider, model)
        if identities:
            prior_provider, prior_model = identities[-1]
            if provider != prior_provider or model.removeprefix(
                f"{provider}/"
            ) != prior_model.removeprefix(f"{prior_provider}/"):
                raise OpenClawSessionEvidenceError(
                    "OpenClaw invocation diagnostic changes route identity"
                )
        identities.append(identity)

    provider, model = identities[-1] if identities else (None, None)
    return CapturedOpenClawInvocationDiagnostic(
        session_id=snapshot.session_id,
        provider=provider,
        model=model,
        transcript_sha256=snapshot.transcript_sha256,
        record_count=len(snapshot.invocation_records),
        submission_tool_call_observed=_submission_tool_call_observed(
            snapshot.invocation_records
        ),
    )


def capture_openclaw_terminal_response(
    *,
    state_dir: Path,
    agent_id: str,
    session_key: str,
    prompt: str,
    baseline: OpenClawInitializationBaseline | None,
) -> CapturedOpenClawTerminalResponse:
    """Recover one fresh terminal turn without trusting a wrapper result envelope."""

    context = _capture_openclaw_terminal_context(
        state_dir=state_dir,
        agent_id=agent_id,
        session_key=session_key,
        prompt=prompt,
        baseline=baseline,
    )
    terminal_message = context.terminal_message
    terminal_state = _invocation_terminal_state(context.invocation)
    if terminal_state is not OpenClawInvocationTerminalState.ASSISTANT_RESPONSE:
        raise OpenClawSessionEvidenceError(
            "OpenClaw invocation has no terminal assistant response"
        )
    if terminal_message.get("stopReason") != "stop":
        raise OpenClawSessionEvidenceError(
            "OpenClaw terminal response did not finish with stop reason"
        )

    provider = _optional_usage_identity(
        terminal_message.get("provider"), label="provider"
    )
    model = _optional_usage_identity(terminal_message.get("model"), label="model")
    usage_buckets = _invocation_usage(
        context.invocation,
        provider=provider,
        model=model,
        runtime_rejections=context.runtime_rejections,
    )

    tool_evidence = CapturedOpenClawToolEvidence(
        transcript_sha256=context.transcript_sha256,
        record_count=len(context.invocation),
        tool_calls=_extract_tool_calls(context.execution_records),
        terminal_state=terminal_state,
        runtime_rejections=context.runtime_rejections,
    )
    return CapturedOpenClawTerminalResponse(
        session_id=context.session_id,
        provider=provider,
        model=model,
        input_tokens=usage_buckets["input"],
        output_tokens=usage_buckets["output"],
        cache_read_tokens=usage_buckets["cacheRead"],
        cache_write_tokens=usage_buckets["cacheWrite"],
        reasoning_tokens=usage_buckets["reasoningTokens"],
        total_tokens=usage_buckets["total"],
        tool_evidence=tool_evidence,
    )
