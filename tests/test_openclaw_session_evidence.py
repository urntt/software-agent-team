"""Tests for bounded, current-turn OpenClaw tool-evidence extraction."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

import software_agent_team.openclaw_session_evidence as session_evidence
from software_agent_team.artifacts import (
    AgentToolCallOutcome,
    AgentToolEvidenceStatus,
    ArtifactKind,
)
from software_agent_team.execution import (
    AgentExecutionRequest,
    AgentExecutionStatus,
    OpenClawSubprocessExecutor,
)
from software_agent_team.invocation_lifecycle import InitializationCheckpoint
from software_agent_team.openclaw_session_evidence import (
    OpenClawInvocationTerminalState,
    OpenClawSessionEvidenceError,
    capture_openclaw_initialization_baseline,
    capture_openclaw_terminal_response,
    capture_openclaw_tool_evidence,
    inspect_openclaw_initialization,
    inspect_openclaw_session_activity,
)
from software_agent_team.submissions import ARTIFACT_SUBMISSION_PROTOCOL
from software_agent_team.teams import AgentCapability

SESSION_ID = "2b1dc5c2-d735-4722-a390-3d28e5854fc4"


def request(*, prompt: str = "Inspect the current project.") -> AgentExecutionRequest:
    return AgentExecutionRequest(
        run_id="session-evidence",
        team_id="adaptive_team",
        iteration=1,
        agent_id="reviewer",
        capability=AgentCapability.REVIEW,
        expected_kind=ArtifactKind.REVIEW_REPORT,
        prompt=prompt,
        timeout_seconds=60,
        model="provider/model",
    )


def session_record(identifier: str = SESSION_ID) -> dict[str, object]:
    return {
        "type": "session",
        "id": identifier,
        "timestamp": "2026-08-27T00:00:00.000Z",
    }


def user_record(prompt: str) -> dict[str, object]:
    return {
        "type": "message",
        "id": "user-message",
        "message": {"role": "user", "content": prompt},
    }


def tool_call_record(
    external_id: str,
    *,
    command: str,
) -> dict[str, object]:
    return {
        "type": "message",
        "id": f"assistant-{external_id}",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "toolCall",
                    "id": external_id,
                    "name": "exec",
                    "arguments": {"command": command},
                }
            ],
        },
    }


def tool_result_record(
    external_id: str,
    *,
    output: str,
    exit_code: int = 0,
    is_error: bool = False,
) -> dict[str, object]:
    return {
        "type": "message",
        "id": f"result-{external_id}",
        "message": {
            "role": "toolResult",
            "toolCallId": external_id,
            "toolName": "exec",
            "isError": is_error,
            "content": [{"type": "text", "text": output}],
            "details": {
                "status": "failed" if is_error else "completed",
                "exitCode": exit_code,
                "durationMs": 17,
                "aggregated": output,
            },
        },
    }


def assistant_record(text: str = '{"verdict":"accept"}') -> dict[str, object]:
    return {
        "type": "message",
        "id": "assistant-final",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        },
    }


def write_session_state(
    root: Path,
    *,
    invocation: AgentExecutionRequest,
    records: list[dict[str, object]],
    session_id: str = SESSION_ID,
    trailing_newline: bool = True,
    indexed_session_id: str | None = None,
    session_file: str | None = None,
) -> Path:
    sessions = root / "agents" / invocation.agent_id / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    transcript = sessions / f"{session_id}.jsonl"
    serialized = "\n".join(json.dumps(item) for item in records)
    if trailing_newline:
        serialized += "\n"
    transcript.write_text(serialized, encoding="utf-8")
    entry: dict[str, object] = {
        "sessionId": indexed_session_id or session_id,
    }
    if session_file is not None:
        entry["sessionFile"] = session_file
    (sessions / "sessions.json").write_text(
        json.dumps({invocation.session_key: entry}),
        encoding="utf-8",
    )
    return transcript


def capture(root: Path, invocation: AgentExecutionRequest):
    return capture_openclaw_tool_evidence(
        state_dir=root,
        agent_id=invocation.agent_id,
        session_key=invocation.session_key,
        session_id=SESSION_ID,
        prompt=invocation.prompt,
    )


def terminal_record() -> dict[str, object]:
    record = assistant_record("submitted")
    record["message"].update(
        {
            "provider": "provider",
            "model": "model",
            "usage": {
                "input": 10,
                "output": 2,
                "cacheRead": 1,
                "cacheWrite": 0,
                "totalTokens": 13,
            },
            "stopReason": "stop",
        }
    )
    return record


def test_terminal_response_recovery_requires_a_fresh_matching_turn(
    tmp_path: Path,
) -> None:
    invocation = request()
    records = [session_record(), user_record(invocation.prompt), terminal_record()]
    write_session_state(tmp_path, invocation=invocation, records=records)
    baseline = capture_openclaw_initialization_baseline(
        state_dir=tmp_path,
        agent_id=invocation.agent_id,
        session_key=invocation.session_key,
        prompt=invocation.prompt,
    )

    with pytest.raises(OpenClawSessionEvidenceError, match="not attributable"):
        capture_openclaw_terminal_response(
            state_dir=tmp_path,
            agent_id=invocation.agent_id,
            session_key=invocation.session_key,
            prompt=invocation.prompt,
            baseline=baseline,
        )

    records.extend([user_record(invocation.prompt), terminal_record()])
    write_session_state(tmp_path, invocation=invocation, records=records)
    recovered = capture_openclaw_terminal_response(
        state_dir=tmp_path,
        agent_id=invocation.agent_id,
        session_key=invocation.session_key,
        prompt=invocation.prompt,
        baseline=baseline,
    )

    assert recovered.session_id == SESSION_ID
    assert recovered.provider == "provider"
    assert recovered.model == "model"
    assert recovered.input_tokens == 10
    assert recovered.output_tokens == 2
    assert recovered.cache_read_tokens == 1
    assert recovered.cache_write_tokens == 0
    assert recovered.total_tokens == 13
    assert recovered.tool_evidence.terminal_state is (
        OpenClawInvocationTerminalState.ASSISTANT_RESPONSE
    )


def test_terminal_response_recovery_rejects_incomplete_or_nonterminal_turn(
    tmp_path: Path,
) -> None:
    invocation = request()
    pending = assistant_record("submitting")
    pending["message"]["stopReason"] = "toolUse"
    write_session_state(
        tmp_path,
        invocation=invocation,
        records=[session_record(), user_record(invocation.prompt), pending],
    )

    with pytest.raises(OpenClawSessionEvidenceError):
        capture_openclaw_terminal_response(
            state_dir=tmp_path,
            agent_id=invocation.agent_id,
            session_key=invocation.session_key,
            prompt=invocation.prompt,
            baseline=None,
        )


def recover_terminal(root: Path, invocation: AgentExecutionRequest):
    return capture_openclaw_terminal_response(
        state_dir=root,
        agent_id=invocation.agent_id,
        session_key=invocation.session_key,
        prompt=invocation.prompt,
        baseline=None,
    )


def test_terminal_usage_accumulates_each_current_turn_assistant_once(
    tmp_path: Path,
) -> None:
    invocation = request()
    work = tool_call_record("work", command="pytest")
    work["message"].update(
        provider="provider",
        model="provider/model",
        usage={
            "input": 1000,
            "output": 200,
            "cacheRead": 300,
            "cacheWrite": 40,
            "reasoningTokens": 11,
            "totalTokens": 1540,
        },
    )
    final = terminal_record()
    final["message"]["usage"]["reasoningTokens"] = 1
    write_session_state(
        tmp_path,
        invocation=invocation,
        records=[
            session_record(),
            user_record("Prior invocation"),
            terminal_record(),
            user_record(invocation.prompt),
            work,
            tool_result_record("work", output="1 passed"),
            final,
            user_record("Later invocation"),
            terminal_record(),
        ],
    )

    recovered = recover_terminal(tmp_path, invocation)

    assert recovered.input_tokens == 1010
    assert recovered.output_tokens == 202
    assert recovered.cache_read_tokens == 301
    assert recovered.cache_write_tokens == 40
    assert recovered.reasoning_tokens == 12
    assert recovered.total_tokens == 1553
    assert len(recovered.tool_evidence.tool_calls) == 1


@pytest.mark.parametrize("boundary", ["current", "historical", "later", "custom"])
def test_terminal_usage_keeps_compaction_coverage_within_current_invocation(
    tmp_path: Path,
    boundary: str,
) -> None:
    invocation = request()
    marker = {
        "type": "compaction",
        "id": "compaction-summary",
        "firstKeptEntryId": "assistant-final",
        "tokensBefore": 5000,
        "summary": "Earlier context was summarized.",
    }
    records = [session_record()]
    if boundary == "historical":
        records.extend([user_record("Earlier request"), marker])
    records.extend([user_record(invocation.prompt), terminal_record()])
    if boundary == "current":
        records.insert(-1, marker)
    elif boundary == "later":
        records.extend([user_record("Later request"), marker, terminal_record()])
    elif boundary == "custom":
        records.insert(-1, {"type": "custom", "customType": "compaction"})
    write_session_state(tmp_path, invocation=invocation, records=records)

    recovered = recover_terminal(tmp_path, invocation)

    assert recovered.session_id == SESSION_ID
    assert recovered.provider == "provider"
    assert recovered.model == "model"
    assert recovered.tool_evidence.terminal_state is (
        OpenClawInvocationTerminalState.ASSISTANT_RESPONSE
    )
    expected = (None,) * 6 if boundary == "current" else (10, 2, 1, 0, None, 13)
    assert (
        recovered.input_tokens,
        recovered.output_tokens,
        recovered.cache_read_tokens,
        recovered.cache_write_tokens,
        recovered.reasoning_tokens,
        recovered.total_tokens,
    ) == expected


def test_terminal_recovery_rejects_rotation_without_current_prompt(
    tmp_path: Path,
) -> None:
    invocation = request()
    successor_id = "successor-session"
    header = session_record(successor_id)
    header["parentSession"] = f"{SESSION_ID}.jsonl"
    write_session_state(
        tmp_path,
        invocation=invocation,
        session_id=successor_id,
        records=[
            header,
            {
                "type": "compaction",
                "id": "compaction-summary",
                "firstKeptEntryId": "assistant-final",
                "tokensBefore": 5000,
                "summary": "The prior request was summarized.",
            },
            terminal_record(),
        ],
    )

    with pytest.raises(OpenClawSessionEvidenceError, match="not attributable"):
        recover_terminal(tmp_path, invocation)


@pytest.mark.parametrize("usage", [None, {}, {"totalTokens": 100}])
def test_terminal_usage_preserves_unknown_earlier_assistant_usage(
    tmp_path: Path,
    usage: object,
) -> None:
    invocation = request()
    earlier = terminal_record()
    earlier["message"]["usage"] = usage
    write_session_state(
        tmp_path,
        invocation=invocation,
        records=[
            session_record(),
            user_record(invocation.prompt),
            earlier,
            terminal_record(),
        ],
    )

    recovered = recover_terminal(tmp_path, invocation)

    assert recovered.input_tokens is None
    assert recovered.output_tokens is None
    assert recovered.cache_read_tokens is None
    assert recovered.cache_write_tokens is None


@pytest.mark.parametrize("field", ["input", "output", "cacheRead", "cacheWrite"])
@pytest.mark.parametrize("value", [None, -1, True, "100", 1.5])
def test_terminal_usage_checks_each_earlier_assistant_bucket(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    invocation = request()
    earlier = terminal_record()
    earlier["message"]["usage"][field] = value
    write_session_state(
        tmp_path,
        invocation=invocation,
        records=[
            session_record(),
            user_record(invocation.prompt),
            earlier,
            terminal_record(),
        ],
    )

    if value is not None:
        with pytest.raises(OpenClawSessionEvidenceError, match="usage field"):
            recover_terminal(tmp_path, invocation)
    else:
        recovered = recover_terminal(tmp_path, invocation)
        attribute = {
            "input": "input_tokens",
            "output": "output_tokens",
            "cacheRead": "cache_read_tokens",
            "cacheWrite": "cache_write_tokens",
        }[field]
        assert getattr(recovered, attribute) is None


@pytest.mark.parametrize("field", ["provider", "model"])
def test_terminal_usage_rejects_mixed_provider_or_model_attribution(
    tmp_path: Path,
    field: str,
) -> None:
    invocation = request()
    earlier = terminal_record()
    earlier["message"][field] = "other"
    write_session_state(
        tmp_path,
        invocation=invocation,
        records=[
            session_record(),
            user_record(invocation.prompt),
            earlier,
            terminal_record(),
        ],
    )

    with pytest.raises(OpenClawSessionEvidenceError, match="usage attribution"):
        recover_terminal(tmp_path, invocation)


@pytest.mark.parametrize("usage", [[], "100", 100])
def test_terminal_usage_rejects_malformed_earlier_usage(
    tmp_path: Path,
    usage: object,
) -> None:
    invocation = request()
    earlier = terminal_record()
    earlier["message"]["usage"] = usage
    write_session_state(
        tmp_path,
        invocation=invocation,
        records=[
            session_record(),
            user_record(invocation.prompt),
            earlier,
            terminal_record(),
        ],
    )

    with pytest.raises(
        OpenClawSessionEvidenceError, match="assistant usage is invalid"
    ):
        recover_terminal(tmp_path, invocation)


@pytest.mark.parametrize("field", ["provider", "model"])
@pytest.mark.parametrize("terminal", [False, True])
def test_terminal_usage_preserves_unknown_provider_or_model_attribution(
    tmp_path: Path,
    field: str,
    terminal: bool,
) -> None:
    invocation = request()
    earlier, final = terminal_record(), terminal_record()
    del (final if terminal else earlier)["message"][field]
    write_session_state(
        tmp_path,
        invocation=invocation,
        records=[session_record(), user_record(invocation.prompt), earlier, final],
    )

    recovered = recover_terminal(tmp_path, invocation)

    assert recovered.input_tokens is None
    assert recovered.output_tokens is None
    assert recovered.cache_read_tokens is None
    assert recovered.cache_write_tokens is None
    assert recovered.total_tokens is None


def test_terminal_recovery_rejects_a_runtime_rejection_terminal_record(
    tmp_path: Path,
) -> None:
    invocation = request()
    write_session_state(
        tmp_path,
        invocation=invocation,
        records=[
            session_record(),
            user_record(invocation.prompt),
            terminal_record(),
            rejected_tool_record(),
        ],
    )

    with pytest.raises(OpenClawSessionEvidenceError, match="no terminal assistant"):
        recover_terminal(tmp_path, invocation)


def test_terminal_recovery_retains_sanitizer_diagnostic_with_unknown_usage(
    tmp_path: Path,
) -> None:
    invocation = request()
    write_session_state(
        tmp_path,
        invocation=invocation,
        records=[
            session_record(),
            user_record(invocation.prompt),
            rejected_tool_record(),
            terminal_record(),
        ],
    )

    recovered = recover_terminal(tmp_path, invocation)

    assert len(recovered.tool_evidence.runtime_rejections) == 1
    assert recovered.input_tokens is None
    assert recovered.output_tokens is None
    assert recovered.cache_read_tokens is None
    assert recovered.cache_write_tokens is None
    assert recovered.total_tokens is None


def rejected_tool_record(external_id: str = "rejected-call") -> dict[str, object]:
    return {
        "type": "message",
        "id": "runtime-rejection",
        "message": {
            "role": "toolResult",
            "toolCallId": external_id,
            "toolName": "missing_tool",
            "isError": True,
            "content": [{"type": "text", "text": "Tool missing_tool not found"}],
            "details": {},
        },
    }


def test_runtime_rejection_is_diagnostic_not_execution_or_progress(tmp_path: Path):
    invocation = request()
    records = [session_record(), user_record(invocation.prompt)]
    write_session_state(tmp_path, invocation=invocation, records=records)
    before = inspect_openclaw_session_activity(
        state_dir=tmp_path,
        agent_id=invocation.agent_id,
        session_key=invocation.session_key,
        prompt=invocation.prompt,
    )
    records.append(rejected_tool_record())
    write_session_state(tmp_path, invocation=invocation, records=records)
    after = inspect_openclaw_session_activity(
        state_dir=tmp_path,
        agent_id=invocation.agent_id,
        session_key=invocation.session_key,
        prompt=invocation.prompt,
    )
    assert after == before
    records.extend(
        [
            tool_call_record("work", command="pytest"),
            tool_result_record("work", output="1 passed"),
            assistant_record(),
        ]
    )
    write_session_state(tmp_path, invocation=invocation, records=records)
    captured = capture(tmp_path, invocation)
    assert len(captured.tool_calls) == 1
    assert captured.tool_calls[0].id == "tool-001"
    assert len(captured.runtime_rejections) == 1
    rejection = captured.runtime_rejections[0]
    assert rejection.reason == "unknown_tool"
    assert rejection.tool_name == "missing_tool"
    assert rejection.record_index == 1
    assert not hasattr(rejection, "arguments_sha256")


@pytest.mark.parametrize(
    "mutation",
    [
        {"isError": False},
        {"details": {"status": "completed"}},
        {"content": [{"type": "text", "text": "Tool another_tool not found"}]},
        {"toolCallId": ""},
        {"toolName": "unsafe name"},
        {"content": [{"type": "text", "text": "permission denied"}]},
    ],
)
def test_unknown_orphan_shapes_still_fail_closed(tmp_path: Path, mutation):
    invocation = request()
    rejected = rejected_tool_record()
    rejected["message"].update(mutation)
    write_session_state(
        tmp_path,
        invocation=invocation,
        records=[
            session_record(),
            user_record(invocation.prompt),
            rejected,
            assistant_record(),
        ],
    )
    with pytest.raises(OpenClawSessionEvidenceError):
        capture(tmp_path, invocation)
    with pytest.raises(OpenClawSessionEvidenceError):
        inspect_openclaw_session_activity(
            state_dir=tmp_path,
            agent_id=invocation.agent_id,
            session_key=invocation.session_key,
            prompt=invocation.prompt,
        )


@pytest.mark.parametrize("extra", ["repeat", "later_call"])
def test_rejection_identity_cannot_be_reused(tmp_path: Path, extra: str):
    invocation = request()
    records = [session_record(), user_record(invocation.prompt), rejected_tool_record()]
    records.append(
        rejected_tool_record()
        if extra == "repeat"
        else tool_call_record("rejected-call", command="pytest")
    )
    write_session_state(tmp_path, invocation=invocation, records=records)
    with pytest.raises(OpenClawSessionEvidenceError):
        capture(tmp_path, invocation)
    with pytest.raises(OpenClawSessionEvidenceError):
        inspect_openclaw_session_activity(
            state_dir=tmp_path,
            agent_id=invocation.agent_id,
            session_key=invocation.session_key,
            prompt=invocation.prompt,
        )


def test_rejection_only_terminal_is_not_paired_tool_continuation(tmp_path: Path):
    invocation = request()
    write_session_state(
        tmp_path,
        invocation=invocation,
        records=[
            session_record(),
            user_record(invocation.prompt),
            assistant_record(),
            rejected_tool_record(),
        ],
    )
    captured = capture(tmp_path, invocation)
    assert captured.tool_calls == ()
    assert captured.terminal_state is OpenClawInvocationTerminalState.RUNTIME_REJECTION
    activity = inspect_openclaw_session_activity(
        state_dir=tmp_path,
        agent_id=invocation.agent_id,
        session_key=invocation.session_key,
        prompt=invocation.prompt,
    )
    assert not activity.terminal_response_observed
    assert activity.tool_completed_count == 0


def test_sanitized_tool_use_text_is_not_a_final_response(tmp_path: Path):
    invocation = request()
    pending = assistant_record("Attempting a tool")
    pending["message"]["stopReason"] = "toolUse"
    write_session_state(
        tmp_path,
        invocation=invocation,
        records=[
            session_record(),
            user_record(invocation.prompt),
            pending,
        ],
    )
    activity = inspect_openclaw_session_activity(
        state_dir=tmp_path,
        agent_id=invocation.agent_id,
        session_key=invocation.session_key,
        prompt=invocation.prompt,
    )
    assert not activity.terminal_response_observed
    with pytest.raises(OpenClawSessionEvidenceError, match="incomplete tool call"):
        capture(tmp_path, invocation)


def test_paired_not_found_remains_failed_tool_evidence(tmp_path: Path):
    invocation = request()
    call = tool_call_record("rejected-call", command="pytest")
    call["message"]["content"][0]["name"] = "missing_tool"
    write_session_state(
        tmp_path,
        invocation=invocation,
        records=[
            session_record(),
            user_record(invocation.prompt),
            call,
            rejected_tool_record(),
            assistant_record(),
        ],
    )
    captured = capture(tmp_path, invocation)
    assert captured.runtime_rejections == ()
    assert captured.tool_calls[0].outcome is AgentToolCallOutcome.FAILED


def test_rejection_cannot_follow_a_terminal_submission(tmp_path: Path):
    invocation = request()
    call = tool_call_record("submission", command="pytest")
    call["message"]["content"][0]["name"] = "sat_submit_artifact"
    result = tool_result_record("submission", output="accepted")
    result["message"]["toolName"] = "sat_submit_artifact"
    write_session_state(
        tmp_path,
        invocation=invocation,
        records=[
            session_record(),
            user_record(invocation.prompt),
            call,
            result,
            rejected_tool_record(),
            assistant_record(),
        ],
    )
    with pytest.raises(
        OpenClawSessionEvidenceError, match="follows terminal submission"
    ):
        capture(tmp_path, invocation)


def test_capture_excludes_prior_turns_and_pairs_current_tool_results(
    tmp_path: Path,
) -> None:
    invocation = request()
    prior_prompt = "Inspect an earlier project."
    records = [
        session_record(),
        user_record(prior_prompt),
        tool_call_record("prior-call", command="read /agent/old.py"),
        tool_result_record("prior-call", output="prior observation"),
        assistant_record("prior answer"),
        user_record(invocation.prompt),
        tool_call_record("current-call", command="python /tmp/probe.py"),
        tool_result_record("current-call", output="BOUNDARY_OK"),
        assistant_record(),
    ]
    transcript = write_session_state(
        tmp_path,
        invocation=invocation,
        records=records,
        session_file=str(
            tmp_path
            / "agents"
            / invocation.agent_id
            / "sessions"
            / f"{SESSION_ID}.jsonl"
        ),
    )

    captured = capture(tmp_path, invocation)

    assert captured.record_count == 4
    assert len(captured.tool_calls) == 1
    call = captured.tool_calls[0]
    assert call.id == "tool-001"
    assert call.tool_name == "exec"
    assert call.executable == "python"
    assert call.outcome is AgentToolCallOutcome.SUCCEEDED
    assert call.exit_code == 0
    assert call.duration_ms == 17
    assert call.output_excerpt == "BOUNDARY_OK"
    assert len(call.external_call_sha256) == 64
    assert len(call.arguments_sha256) == 64
    assert len(call.output_sha256) == 64
    assert captured.transcript_sha256 != call.output_sha256
    assert captured.terminal_state is OpenClawInvocationTerminalState.ASSISTANT_RESPONSE
    assert transcript.is_file()


def test_capture_preserves_deferred_exec_then_terminal_process_and_submission(
    tmp_path: Path,
) -> None:
    """A valid async start must not invalidate the later terminal evidence."""

    invocation = request()
    records = [
        session_record(),
        user_record(invocation.prompt),
        tool_call_record("async-exec", command="uv run pytest"),
        {
            "type": "message",
            "message": {
                "role": "toolResult",
                "toolCallId": "async-exec",
                "toolName": "exec",
                "isError": False,
                "content": [
                    {
                        "type": "text",
                        "text": "Command still running (session brisk-meadow).",
                    }
                ],
                "details": {
                    "status": "running",
                    "sessionId": "brisk-meadow",
                    "pid": 1537063,
                    "startedAt": 1788749665172,
                    "cwd": "/workspace",
                    "tail": "tests are still running",
                },
            },
        },
        {
            "type": "message",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "process-poll",
                        "name": "process",
                        "arguments": {
                            "action": "poll",
                            "sessionId": "brisk-meadow",
                        },
                    }
                ],
            },
        },
        {
            "type": "message",
            "message": {
                "role": "toolResult",
                "toolCallId": "process-poll",
                "toolName": "process",
                "isError": False,
                "content": [{"type": "text", "text": "243 tests passed"}],
                "details": {
                    "status": "completed",
                    "sessionId": "brisk-meadow",
                    "exitCode": 0,
                    "exitReason": "exit",
                    "aggregated": "243 tests passed",
                    "name": "uv run pytest",
                },
            },
        },
        {
            "type": "message",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "final-submission",
                        "name": "sat_submit_artifact",
                        "arguments": {"artifact": {"summary": "complete"}},
                    }
                ],
            },
        },
        {
            "type": "message",
            "message": {
                "role": "toolResult",
                "toolCallId": "final-submission",
                "toolName": "sat_submit_artifact",
                "isError": False,
                "content": [{"type": "text", "text": "accepted"}],
                "details": {
                    "status": "completed",
                    "submission_status": "accepted",
                },
            },
        },
        assistant_record(),
    ]
    write_session_state(tmp_path, invocation=invocation, records=records)

    captured = capture(tmp_path, invocation)

    assert captured.record_count == 8
    assert tuple(call.tool_name for call in captured.tool_calls) == (
        "exec",
        "process",
        "sat_submit_artifact",
    )
    assert tuple(call.outcome for call in captured.tool_calls) == (
        AgentToolCallOutcome.DEFERRED,
        AgentToolCallOutcome.SUCCEEDED,
        AgentToolCallOutcome.SUCCEEDED,
    )
    assert captured.tool_calls[0].reported_status == "running"
    assert captured.tool_calls[0].exit_code is None
    assert captured.tool_calls[1].exit_code == 0


def test_capture_preserves_bound_submission_plugin_receipt(tmp_path: Path) -> None:
    invocation = request()
    call = tool_call_record("bound-submission", command="unused")
    call["message"]["content"][0].update(
        {
            "name": "sat_submit_artifact",
            "arguments": {"artifact": {"summary": "complete"}},
        }
    )
    result = tool_result_record("bound-submission", output="accepted")
    result["message"].update(
        {
            "toolName": "sat_submit_artifact",
            "details": {
                "status": "completed",
                "submission_status": "accepted",
                "protocol": ARTIFACT_SUBMISSION_PROTOCOL,
                "schema_sha256": "a" * 64,
                "binding_sha256": "b" * 64,
            },
        }
    )
    write_session_state(
        tmp_path,
        invocation=invocation,
        records=[
            session_record(),
            user_record(invocation.prompt),
            call,
            result,
            assistant_record(),
        ],
    )

    captured = capture(tmp_path, invocation)

    receipt = captured.tool_calls[0].submission_receipt
    assert receipt is not None
    assert receipt.protocol == ARTIFACT_SUBMISSION_PROTOCOL
    assert receipt.schema_sha256 == "a" * 64
    assert receipt.binding_sha256 == "b" * 64


@pytest.mark.parametrize(
    "details",
    [
        {
            "status": "completed",
            "protocol": ARTIFACT_SUBMISSION_PROTOCOL,
        },
        {
            "status": "completed",
            "submission_status": "rejected",
            "protocol": ARTIFACT_SUBMISSION_PROTOCOL,
            "schema_sha256": "a" * 64,
            "binding_sha256": "b" * 64,
        },
        {
            "status": "completed",
            "submission_status": "accepted",
            "protocol": ARTIFACT_SUBMISSION_PROTOCOL,
            "schema_sha256": "not-a-digest",
            "binding_sha256": "b" * 64,
        },
    ],
)
def test_capture_rejects_incomplete_or_invalid_submission_receipt(
    tmp_path: Path,
    details: dict[str, object],
) -> None:
    invocation = request()
    call = tool_call_record("bad-receipt", command="unused")
    call["message"]["content"][0]["name"] = "sat_submit_artifact"
    result = tool_result_record("bad-receipt", output="accepted")
    result["message"].update(
        {
            "toolName": "sat_submit_artifact",
            "details": details,
        }
    )
    write_session_state(
        tmp_path,
        invocation=invocation,
        records=[
            session_record(),
            user_record(invocation.prompt),
            call,
            result,
            assistant_record(),
        ],
    )

    with pytest.raises(OpenClawSessionEvidenceError, match="pinned schema"):
        capture(tmp_path, invocation)


def test_capture_rejects_submission_receipt_on_another_tool(tmp_path: Path) -> None:
    invocation = request()
    result = tool_result_record("wrong-tool", output="accepted")
    result["message"]["details"].update(
        {
            "submission_status": "accepted",
            "protocol": ARTIFACT_SUBMISSION_PROTOCOL,
            "schema_sha256": "a" * 64,
            "binding_sha256": "b" * 64,
        }
    )
    write_session_state(
        tmp_path,
        invocation=invocation,
        records=[
            session_record(),
            user_record(invocation.prompt),
            tool_call_record("wrong-tool", command="pytest"),
            result,
            assistant_record(),
        ],
    )

    with pytest.raises(
        OpenClawSessionEvidenceError,
        match="non-submission tool result claims",
    ):
        capture(tmp_path, invocation)


def test_capture_uses_the_latest_matching_prompt_for_semantic_repair(
    tmp_path: Path,
) -> None:
    invocation = request()
    records = [
        session_record(),
        user_record(invocation.prompt),
        tool_call_record("first-attempt", command="read /agent/first.py"),
        tool_result_record("first-attempt", output="first observation"),
        assistant_record("invalid response"),
        user_record(invocation.prompt),
        tool_call_record("repair-attempt", command="read /agent/repaired.py"),
        tool_result_record("repair-attempt", output="repair observation"),
        assistant_record(),
    ]
    write_session_state(tmp_path, invocation=invocation, records=records)

    captured = capture(tmp_path, invocation)

    assert [item.output_excerpt for item in captured.tool_calls] == [
        "repair observation"
    ]


def test_capture_records_a_complete_zero_tool_invocation(tmp_path: Path) -> None:
    invocation = request()
    write_session_state(
        tmp_path,
        invocation=invocation,
        records=[session_record(), user_record(invocation.prompt), assistant_record()],
    )

    captured = capture(tmp_path, invocation)

    assert captured.record_count == 2
    assert captured.tool_calls == ()
    assert captured.terminal_state is OpenClawInvocationTerminalState.ASSISTANT_RESPONSE


def test_capture_distinguishes_a_turn_ending_after_a_paired_tool_result(
    tmp_path: Path,
) -> None:
    invocation = request()
    write_session_state(
        tmp_path,
        invocation=invocation,
        records=[
            session_record(),
            user_record(invocation.prompt),
            tool_call_record("terminating-call", command="read README.md"),
            tool_result_record("terminating-call", output="observed"),
        ],
    )

    captured = capture(tmp_path, invocation)

    assert len(captured.tool_calls) == 1
    assert captured.terminal_state is OpenClawInvocationTerminalState.TOOL_RESULT


def test_activity_inspection_tracks_current_tool_lifecycle_without_content(
    tmp_path: Path,
) -> None:
    invocation = request(prompt="Do not expose SECRET_ACTIVITY_CONTENT.")
    records = [
        session_record(),
        user_record(invocation.prompt),
        tool_call_record("current-call", command="read SECRET_ACTIVITY_CONTENT"),
    ]
    transcript = write_session_state(
        tmp_path,
        invocation=invocation,
        records=records,
    )

    active = inspect_openclaw_session_activity(
        state_dir=tmp_path,
        agent_id=invocation.agent_id,
        session_key=invocation.session_key,
        prompt=invocation.prompt,
    )

    assert active is not None
    assert active.trusted_record_count == 1
    assert active.tool_started_count == 1
    assert active.tool_completed_count == 0
    assert active.active_tool_count == 1
    assert [(item.tool_name, item.executable) for item in active.started_tools] == [
        ("exec", "read")
    ]
    assert active.completed_tools == ()
    assert not active.terminal_response_observed
    assert "SECRET_ACTIVITY_CONTENT" not in repr(active)

    records.append(tool_result_record("current-call", output="SECRET_ACTIVITY_CONTENT"))
    transcript.write_text(
        "\n".join(json.dumps(item) for item in records) + "\n",
        encoding="utf-8",
    )
    completed = inspect_openclaw_session_activity(
        state_dir=tmp_path,
        agent_id=invocation.agent_id,
        session_key=invocation.session_key,
        prompt=invocation.prompt,
    )

    assert completed is not None
    assert completed.trusted_record_count == 2
    assert completed.tool_started_count == 1
    assert completed.tool_completed_count == 1
    assert completed.active_tool_count == 0
    assert [
        (item.tool_name, item.executable) for item in completed.completed_tools
    ] == [("exec", "read")]
    assert not completed.terminal_response_observed
    assert "SECRET_ACTIVITY_CONTENT" not in repr(completed)

    records.append(assistant_record("SECRET_ACTIVITY_CONTENT"))
    transcript.write_text(
        "\n".join(json.dumps(item) for item in records) + "\n",
        encoding="utf-8",
    )
    terminal = inspect_openclaw_session_activity(
        state_dir=tmp_path,
        agent_id=invocation.agent_id,
        session_key=invocation.session_key,
        prompt=invocation.prompt,
    )

    assert terminal is not None
    assert terminal.trusted_record_count == 3
    assert terminal.active_tool_count == 0
    assert terminal.terminal_response_observed
    assert "SECRET_ACTIVITY_CONTENT" not in repr(terminal)


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("cd /workspace && git status --short", "git"),
        ('cd "/private/SECRET/path with spaces" && pytest -q', "pytest"),
        ("cd -- /workspace && uv run pytest", "uv"),
        ("# leading comment\ncd /workspace && git status", "git"),
        ("cd /workspace || git status", None),
        ("cd /workspace '&&' git status", None),
        ('cd /workspace "&&" git status', None),
        ("cd /workspace; git status", None),
        ("cd /workspace\n&& git status", None),
        ("cd - && git status", None),
        ("cd -P && git status", None),
        ('cd "$SECRET_PATH" && git status', None),
        ("cd $(printf /workspace) && git status", None),
        ("cd /workspace && /tmp/SECRET_EXECUTABLE", None),
        ("cd /workspace && $(printf git) status", None),
        ("cd /workspace &&", None),
        ("cd /workspace && cd child && git status", None),
    ],
)
def test_directory_wrapper_activity_preserves_direct_evidence(
    tmp_path: Path, command: str, expected: str | None
) -> None:
    invocation = request()
    write_session_state(
        tmp_path,
        invocation=invocation,
        records=[
            session_record(),
            user_record(invocation.prompt),
            tool_call_record("wrapped-call", command=command),
            tool_result_record("wrapped-call", output="SECRET_OUTPUT"),
        ],
    )
    activity = inspect_openclaw_session_activity(
        state_dir=tmp_path,
        agent_id=invocation.agent_id,
        session_key=invocation.session_key,
        prompt=invocation.prompt,
    )
    assert activity is not None
    assert activity.started_tools[0].executable == expected
    assert activity.completed_tools[0].executable == expected
    assert "SECRET" not in repr(activity)
    assert capture(tmp_path, invocation).tool_calls[0].executable == "cd"


def test_activity_identity_drops_unlisted_executable_content(tmp_path: Path) -> None:
    invocation = request(prompt="Inspect without exposing activity arguments.")
    write_session_state(
        tmp_path,
        invocation=invocation,
        records=[
            session_record(),
            user_record(invocation.prompt),
            tool_call_record(
                "current-call",
                command="/tmp/SECRET_ACTIVITY_EXECUTABLE --token hidden",
            ),
        ],
    )

    activity = inspect_openclaw_session_activity(
        state_dir=tmp_path,
        agent_id=invocation.agent_id,
        session_key=invocation.session_key,
        prompt=invocation.prompt,
    )

    assert activity is not None
    assert activity.started_tools[0].tool_name == "exec"
    assert activity.started_tools[0].executable is None
    assert "SECRET_ACTIVITY_EXECUTABLE" not in repr(activity)


def test_activity_inspection_returns_none_until_current_session_exists(
    tmp_path: Path,
) -> None:
    invocation = request()

    assert (
        inspect_openclaw_session_activity(
            state_dir=tmp_path,
            agent_id=invocation.agent_id,
            session_key=invocation.session_key,
            prompt=invocation.prompt,
        )
        is None
    )


def test_initialization_inspection_reports_only_finite_attributable_checkpoints(
    tmp_path: Path,
) -> None:
    invocation = request(prompt="Do not expose INITIALIZATION_SECRET.")
    assert (
        inspect_openclaw_initialization(
            state_dir=tmp_path,
            agent_id=invocation.agent_id,
            session_key=invocation.session_key,
            prompt=invocation.prompt,
        )
        is None
    )
    sessions = tmp_path / "agents" / invocation.agent_id / "sessions"
    sessions.mkdir(parents=True)
    directory = inspect_openclaw_initialization(
        state_dir=tmp_path,
        agent_id=invocation.agent_id,
        session_key=invocation.session_key,
        prompt=invocation.prompt,
    )
    assert directory is not None
    assert directory.checkpoint is InitializationCheckpoint.SESSION_DIRECTORY

    (sessions / "sessions.json").write_text("{}", encoding="utf-8")
    indexed = inspect_openclaw_initialization(
        state_dir=tmp_path,
        agent_id=invocation.agent_id,
        session_key=invocation.session_key,
        prompt=invocation.prompt,
    )
    assert indexed is not None
    assert indexed.checkpoint is InitializationCheckpoint.SESSION_INDEX

    (sessions / "sessions.json").write_text(
        json.dumps({invocation.session_key: {"sessionId": SESSION_ID}}),
        encoding="utf-8",
    )
    bound = inspect_openclaw_initialization(
        state_dir=tmp_path,
        agent_id=invocation.agent_id,
        session_key=invocation.session_key,
        prompt=invocation.prompt,
    )
    assert bound is not None
    assert bound.checkpoint is InitializationCheckpoint.SESSION_BOUND

    transcript = sessions / f"{SESSION_ID}.jsonl"
    transcript.write_text(
        json.dumps(session_record()) + "\n",
        encoding="utf-8",
    )
    header = inspect_openclaw_initialization(
        state_dir=tmp_path,
        agent_id=invocation.agent_id,
        session_key=invocation.session_key,
        prompt=invocation.prompt,
    )
    assert header is not None
    assert header.checkpoint is InitializationCheckpoint.TRANSCRIPT_HEADER

    transcript.write_text(
        "\n".join(
            (json.dumps(session_record()), json.dumps(user_record(invocation.prompt)))
        )
        + "\n",
        encoding="utf-8",
    )
    current = inspect_openclaw_initialization(
        state_dir=tmp_path,
        agent_id=invocation.agent_id,
        session_key=invocation.session_key,
        prompt=invocation.prompt,
    )
    assert current is not None
    assert current.checkpoint is InitializationCheckpoint.CURRENT_TURN
    assert "INITIALIZATION_SECRET" not in repr(current)


def test_initialization_baseline_requires_a_new_matching_turn_occurrence(
    tmp_path: Path,
) -> None:
    invocation = request(prompt="Repeat this exact prompt safely.")
    records = [
        session_record(),
        user_record(invocation.prompt),
        assistant_record("prior response"),
    ]
    transcript = write_session_state(
        tmp_path,
        invocation=invocation,
        records=records,
    )
    baseline = capture_openclaw_initialization_baseline(
        state_dir=tmp_path,
        agent_id=invocation.agent_id,
        session_key=invocation.session_key,
        prompt=invocation.prompt,
    )

    assert baseline.checkpoint is InitializationCheckpoint.CURRENT_TURN
    assert baseline.session_id == SESSION_ID
    assert baseline.matching_turn_count == 1
    assert (
        inspect_openclaw_initialization(
            state_dir=tmp_path,
            agent_id=invocation.agent_id,
            session_key=invocation.session_key,
            prompt=invocation.prompt,
            baseline=baseline,
        )
        is None
    )
    assert (
        inspect_openclaw_session_activity(
            state_dir=tmp_path,
            agent_id=invocation.agent_id,
            session_key=invocation.session_key,
            prompt=invocation.prompt,
            baseline=baseline,
        )
        is None
    )

    records.extend((user_record(invocation.prompt), assistant_record("new response")))
    transcript.write_text(
        "\n".join(json.dumps(item) for item in records) + "\n",
        encoding="utf-8",
    )
    current = inspect_openclaw_initialization(
        state_dir=tmp_path,
        agent_id=invocation.agent_id,
        session_key=invocation.session_key,
        prompt=invocation.prompt,
        baseline=baseline,
    )
    activity = inspect_openclaw_session_activity(
        state_dir=tmp_path,
        agent_id=invocation.agent_id,
        session_key=invocation.session_key,
        prompt=invocation.prompt,
        baseline=baseline,
    )

    assert current is not None
    assert current.checkpoint is InitializationCheckpoint.CURRENT_TURN
    assert activity is not None
    assert activity.terminal_response_observed


def test_initialization_index_publish_between_open_and_observation_is_missing_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invocation = request()
    sessions = tmp_path / "agents" / invocation.agent_id / "sessions"
    sessions.mkdir(parents=True)
    index_path = sessions / "sessions.json"
    real_open = os.open
    raced = False

    def open_during_publish(path: Path, flags: int) -> int:
        nonlocal raced
        if Path(path) == index_path and not raced:
            raced = True
            index_path.write_text("{}", encoding="utf-8")
            raise FileNotFoundError(2, "not published at open time", str(path))
        return real_open(path, flags)

    monkeypatch.setattr(session_evidence.os, "open", open_during_publish)

    first = inspect_openclaw_initialization(
        state_dir=tmp_path,
        agent_id=invocation.agent_id,
        session_key=invocation.session_key,
        prompt=invocation.prompt,
    )
    second = inspect_openclaw_initialization(
        state_dir=tmp_path,
        agent_id=invocation.agent_id,
        session_key=invocation.session_key,
        prompt=invocation.prompt,
    )

    assert raced
    assert first is not None
    assert first.checkpoint is InitializationCheckpoint.SESSION_DIRECTORY
    assert second is not None
    assert second.checkpoint is InitializationCheckpoint.SESSION_INDEX


def test_initialization_transcript_publish_between_checkpoints_is_missing_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invocation = request()
    sessions = tmp_path / "agents" / invocation.agent_id / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "sessions.json").write_text(
        json.dumps({invocation.session_key: {"sessionId": SESSION_ID}}),
        encoding="utf-8",
    )
    transcript_path = sessions / f"{SESSION_ID}.jsonl"
    real_open = os.open
    raced = False

    def open_during_publish(path: Path, flags: int) -> int:
        nonlocal raced
        if Path(path) == transcript_path and not raced:
            raced = True
            transcript_path.write_text(
                "\n".join(
                    (
                        json.dumps(session_record()),
                        json.dumps(user_record(invocation.prompt)),
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            raise FileNotFoundError(2, "not published at open time", str(path))
        return real_open(path, flags)

    monkeypatch.setattr(session_evidence.os, "open", open_during_publish)

    first = inspect_openclaw_initialization(
        state_dir=tmp_path,
        agent_id=invocation.agent_id,
        session_key=invocation.session_key,
        prompt=invocation.prompt,
    )
    second = inspect_openclaw_initialization(
        state_dir=tmp_path,
        agent_id=invocation.agent_id,
        session_key=invocation.session_key,
        prompt=invocation.prompt,
    )

    assert raced
    assert first is not None
    assert first.checkpoint is InitializationCheckpoint.SESSION_BOUND
    assert second is not None
    assert second.checkpoint is InitializationCheckpoint.CURRENT_TURN


def test_capture_keeps_only_the_executable_not_sensitive_exec_arguments(
    tmp_path: Path,
) -> None:
    invocation = request()
    command = (
        "SAT_PRIVATE_VALUE=do-not-persist sat-probe-write "
        "/tmp/sat-review-probe-safe.py --line pass"
    )
    write_session_state(
        tmp_path,
        invocation=invocation,
        records=[
            session_record(),
            user_record(invocation.prompt),
            tool_call_record("current-call", command=command),
            tool_result_record("current-call", output="created probe"),
            assistant_record(),
        ],
    )

    call = capture(tmp_path, invocation).tool_calls[0]
    serialized = call.model_dump_json()

    assert call.executable == "sat-probe-write"
    assert "do-not-persist" not in serialized
    assert "/tmp/sat-review-probe-safe.py" not in serialized


def test_capture_stops_after_an_attributable_exec_prefix(tmp_path: Path) -> None:
    invocation = request()
    command = (
        "SAT_PRIVATE_VALUE=do-not-persist sat-probe-write "
        "/tmp/sat-review-probe-safe.py --line pass "
        "# the shell ignores this unmatched quote: '"
    )
    write_session_state(
        tmp_path,
        invocation=invocation,
        records=[
            session_record(),
            user_record(invocation.prompt),
            tool_call_record("current-call", command=command),
            tool_result_record("current-call", output="created probe"),
            assistant_record(),
        ],
    )

    call = capture(tmp_path, invocation).tool_calls[0]

    assert call.executable == "sat-probe-write"
    assert call.outcome is AgentToolCallOutcome.SUCCEEDED
    assert "do-not-persist" not in call.model_dump_json()
    assert len(call.arguments_sha256) == 64


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (
            "\n  # explain a quote: ' and a token sat-probe-run\n# second\nprintf ok",
            "printf",
        ),
        (
            "# preceding comment\nMODE=local sat-probe-run /tmp/probe.py",
            "sat-probe-run",
        ),
        ("sat-probe-run#not-a-comment /tmp/probe.py", "sat-probe-run#not-a-comment"),
        ('"sat-probe-run#quoted" /tmp/probe.py', "sat-probe-run#quoted"),
    ],
)
def test_leading_shell_comments_preserve_capture_and_activity(
    tmp_path: Path, command: str, expected: str
) -> None:
    invocation = request()
    write_session_state(
        tmp_path,
        invocation=invocation,
        records=[
            session_record(),
            user_record(invocation.prompt),
            tool_call_record("commented-call", command=command),
            tool_result_record("commented-call", output="ok"),
            assistant_record(),
        ],
    )
    captured = capture(tmp_path, invocation)
    assert captured.tool_calls[0].executable == expected
    activity = inspect_openclaw_session_activity(
        state_dir=tmp_path,
        agent_id=invocation.agent_id,
        session_key=invocation.session_key,
        prompt=invocation.prompt,
    )
    assert activity is not None
    assert activity.tool_started_count == activity.tool_completed_count == 1
    assert activity.terminal_response_observed
    assert "explain a quote" not in captured.tool_calls[0].model_dump_json()


def test_capture_rejects_an_unparseable_exec_prefix(tmp_path: Path) -> None:
    invocation = request()
    write_session_state(
        tmp_path,
        invocation=invocation,
        records=[
            session_record(),
            user_record(invocation.prompt),
            tool_call_record("current-call", command="'unterminated"),
            tool_result_record(
                "current-call",
                output="shell syntax error",
                exit_code=2,
                is_error=True,
            ),
            assistant_record(),
        ],
    )

    with pytest.raises(
        OpenClawSessionEvidenceError,
        match="exec command cannot be attributed safely",
    ):
        capture(tmp_path, invocation)

    write_session_state(
        tmp_path,
        invocation=invocation,
        records=[
            session_record(),
            user_record(invocation.prompt),
            tool_call_record("comment-only", command="# no executable here"),
            tool_result_record("comment-only", output=""),
            assistant_record(),
        ],
    )
    with pytest.raises(
        OpenClawSessionEvidenceError,
        match="exec command cannot be attributed safely",
    ):
        capture(tmp_path, invocation)


def test_capture_normalizes_an_observable_failed_tool_result(tmp_path: Path) -> None:
    invocation = request()
    write_session_state(
        tmp_path,
        invocation=invocation,
        records=[
            session_record(),
            user_record(invocation.prompt),
            tool_call_record("failed-call", command="touch /agent/forbidden"),
            tool_result_record(
                "failed-call",
                output="Read-only file system",
                exit_code=1,
                is_error=True,
            ),
            assistant_record(),
        ],
    )

    call = capture(tmp_path, invocation).tool_calls[0]

    assert call.outcome is AgentToolCallOutcome.FAILED
    assert call.executable == "touch"
    assert call.is_error is True
    assert call.exit_code == 1
    assert "Read-only file system" in call.output_excerpt


def test_capture_rejects_unknown_status_or_invalid_tool_schema(tmp_path: Path) -> None:
    invocation = request()
    call = tool_call_record("current-call", command="read /agent/README.md")
    result = tool_result_record("current-call", output="observation")
    details = result["message"]["details"]
    details["status"] = "queued"
    write_session_state(
        tmp_path,
        invocation=invocation,
        records=[session_record(), user_record(invocation.prompt), call, result],
    )
    with pytest.raises(OpenClawSessionEvidenceError, match="status is unknown"):
        capture(tmp_path, invocation)

    call["message"]["content"][0]["name"] = "INVALID TOOL NAME"
    result["message"]["toolName"] = "INVALID TOOL NAME"
    details["status"] = "completed"
    write_session_state(
        tmp_path,
        invocation=invocation,
        records=[session_record(), user_record(invocation.prompt), call, result],
    )
    with pytest.raises(OpenClawSessionEvidenceError, match="pinned schema"):
        capture(tmp_path, invocation)


@pytest.mark.parametrize(
    ("tool_name", "details"),
    [
        (
            "exec",
            {
                "status": "running",
                "startedAt": 1788749665172,
                "cwd": "/workspace",
                "tail": "still running",
            },
        ),
        (
            "exec",
            {
                "status": "running",
                "sessionId": "brisk-meadow",
                "startedAt": 1788749665172,
                "cwd": "/workspace",
                "tail": "still running",
                "exitCode": 0,
            },
        ),
        (
            "process",
            {"status": "running", "sessionId": "brisk-meadow"},
        ),
        (
            "read",
            {
                "status": "running",
                "sessionId": "brisk-meadow",
                "name": "read",
            },
        ),
    ],
)
def test_capture_rejects_malformed_deferred_process_results(
    tmp_path: Path,
    tool_name: str,
    details: dict[str, object],
) -> None:
    invocation = request()
    call = tool_call_record("current-call", command="uv run pytest")
    result = tool_result_record("current-call", output="still running")
    call_item = call["message"]["content"][0]
    call_item["name"] = tool_name
    if tool_name != "exec":
        call_item["arguments"] = {
            "action": "poll",
            "sessionId": "brisk-meadow",
        }
    result["message"]["toolName"] = tool_name
    result["message"]["details"] = details
    write_session_state(
        tmp_path,
        invocation=invocation,
        records=[session_record(), user_record(invocation.prompt), call, result],
    )

    with pytest.raises(OpenClawSessionEvidenceError, match="OpenClaw"):
        capture(tmp_path, invocation)


@pytest.mark.parametrize(
    ("records", "error"),
    [
        (
            lambda invocation: [
                session_record(),
                user_record(invocation.prompt),
                tool_call_record("missing-result", command="read /agent/app.py"),
                assistant_record(),
            ],
            "do not pair exactly",
        ),
        (
            lambda invocation: [
                session_record(),
                user_record(invocation.prompt),
                tool_result_record("unknown-call", output="unexpected"),
                assistant_record(),
            ],
            "appears before its tool call",
        ),
        (
            lambda invocation: [
                session_record(),
                user_record("a different prompt"),
                assistant_record(),
            ],
            "prompt is absent",
        ),
    ],
)
def test_capture_rejects_unattributable_session_records(
    tmp_path: Path,
    records,
    error: str,
) -> None:
    invocation = request()
    write_session_state(
        tmp_path,
        invocation=invocation,
        records=records(invocation),
    )

    with pytest.raises(OpenClawSessionEvidenceError, match=error):
        capture(tmp_path, invocation)


def test_capture_rejects_index_identity_and_path_substitution(tmp_path: Path) -> None:
    invocation = request()
    records = [session_record(), user_record(invocation.prompt), assistant_record()]
    write_session_state(
        tmp_path,
        invocation=invocation,
        records=records,
        indexed_session_id="different-session",
    )
    with pytest.raises(OpenClawSessionEvidenceError, match="does not bind"):
        capture(tmp_path, invocation)

    write_session_state(
        tmp_path,
        invocation=invocation,
        records=records,
        session_file="/tmp/substituted-session.jsonl",
    )
    with pytest.raises(OpenClawSessionEvidenceError, match="points outside"):
        capture(tmp_path, invocation)


def test_capture_rejects_incomplete_or_symlinked_transcript(tmp_path: Path) -> None:
    invocation = request()
    records = [session_record(), user_record(invocation.prompt), assistant_record()]
    transcript = write_session_state(
        tmp_path,
        invocation=invocation,
        records=records,
        trailing_newline=False,
    )
    with pytest.raises(OpenClawSessionEvidenceError, match="empty or incomplete"):
        capture(tmp_path, invocation)

    target = tmp_path / "outside.jsonl"
    target.write_text(
        "\n".join(json.dumps(item) for item in records) + "\n",
        encoding="utf-8",
    )
    transcript.unlink()
    transcript.symlink_to(target)
    with pytest.raises(OpenClawSessionEvidenceError, match="cannot open"):
        capture(tmp_path, invocation)


def test_executor_persists_captured_current_turn_evidence(tmp_path: Path) -> None:
    invocation = request()

    def runner(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        write_session_state(
            tmp_path,
            invocation=invocation,
            records=[
                session_record(),
                user_record(invocation.prompt),
                tool_call_record("current-call", command="read /agent/README.md"),
                tool_result_record("current-call", output="README_BOUNDARY_OK"),
                assistant_record(),
            ],
        )
        stdout = json.dumps(
            {
                "payloads": [{"text": '{"verdict":"accept"}'}],
                "meta": {
                    "agentMeta": {
                        "sessionId": SESSION_ID,
                        "provider": "provider",
                        "model": "model",
                        "usage": {"input": 10, "output": 2},
                    }
                },
            }
        )
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    result = OpenClawSubprocessExecutor(
        openclaw_binary="/opt/openclaw",
        environment={"OPENCLAW_STATE_DIR": str(tmp_path)},
        runner=runner,
    ).execute(invocation)

    assert result.status is AgentExecutionStatus.COMPLETED
    assert result.telemetry.tool_evidence_status is AgentToolEvidenceStatus.CAPTURED
    assert result.telemetry.session_record_count == 4
    assert result.telemetry.tool_calls[0].output_excerpt == "README_BOUNDARY_OK"


def test_executor_marks_untrusted_session_evidence_invalid(tmp_path: Path) -> None:
    invocation = request()

    def runner(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        write_session_state(
            tmp_path,
            invocation=invocation,
            records=[session_record(), user_record("different"), assistant_record()],
        )
        stdout = json.dumps(
            {
                "payloads": [{"text": '{"verdict":"accept"}'}],
                "meta": {
                    "agentMeta": {
                        "sessionId": SESSION_ID,
                        "provider": "provider",
                        "model": "model",
                        "usage": {"input": 10, "output": 2},
                    }
                },
            }
        )
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    result = OpenClawSubprocessExecutor(
        openclaw_binary="/opt/openclaw",
        environment={"OPENCLAW_STATE_DIR": str(tmp_path)},
        runner=runner,
    ).execute(invocation)

    assert result.status is AgentExecutionStatus.COMPLETED
    assert result.telemetry.tool_evidence_status is AgentToolEvidenceStatus.INVALID
    assert result.telemetry.tool_calls == ()
    assert "prompt is absent" in (result.telemetry.tool_evidence_error or "")
