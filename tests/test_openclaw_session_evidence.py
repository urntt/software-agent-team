"""Tests for bounded, current-turn OpenClaw tool-evidence extraction."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

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
from software_agent_team.openclaw_session_evidence import (
    OpenClawSessionEvidenceError,
    capture_openclaw_tool_evidence,
)
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
    assert transcript.is_file()


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
    details["status"] = "running"
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
