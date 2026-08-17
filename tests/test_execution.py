"""Tests for shell-free OpenClaw and scripted Agent execution adapters."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from software_agent_team.artifacts import AgentRole, ArtifactKind
from software_agent_team.execution import (
    AgentExecutionRequest,
    AgentExecutionStatus,
    AgentExecutor,
    AgentTokenUsage,
    OpenClawSubprocessExecutor,
    ScriptedAgentExecutor,
    ScriptedResponseExhaustedError,
    stable_session_key,
)

STARTED = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


def request(**updates: object) -> AgentExecutionRequest:
    payload: dict[str, object] = {
        "run_id": "task-manager-001",
        "team_id": "function_specialized",
        "iteration": 1,
        "role": AgentRole.PLANNER,
        "expected_kind": ArtifactKind.IMPLEMENTATION_PLAN,
        "prompt": "Return the implementation plan as JSON.",
        "timeout_seconds": 30,
    }
    payload.update(updates)
    return AgentExecutionRequest.model_validate(payload)


def openclaw_result(
    text: str = '{"kind":"implementation_plan"}',
) -> str:
    return json.dumps(
        {
            "payloads": [{"text": text}],
            "meta": {
                "durationMs": 912,
                "agentMeta": {
                    "sessionId": "session-123",
                    "provider": "test-provider",
                    "model": "test-model",
                    "usage": {
                        "input": 101,
                        "output": 37,
                        "cacheRead": 11,
                        "cacheWrite": 3,
                        "reasoningTokens": 7,
                        "total": 159,
                    },
                },
            },
        }
    )


def executor_with_clocks(
    runner: Any,
    **kwargs: object,
) -> OpenClawSubprocessExecutor:
    wall_times = iter([STARTED, STARTED + timedelta(milliseconds=125)])
    monotonic_times = iter([10.0, 10.125])
    return OpenClawSubprocessExecutor(
        openclaw_binary="/opt/openclaw",
        runner=runner,
        clock=lambda: next(wall_times),
        monotonic=lambda: next(monotonic_times),
        **kwargs,
    )


def test_openclaw_adapter_uses_message_file_and_no_shell() -> None:
    observed: dict[str, object] = {}

    def runner(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        observed["command"] = tuple(command)
        observed["kwargs"] = kwargs
        prompt_path = Path(command[command.index("--message-file") + 1])
        observed["prompt"] = prompt_path.read_text(encoding="utf-8")
        observed["mode"] = prompt_path.stat().st_mode & 0o777
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=openclaw_result(),
            stderr="gateway diagnostic\n",
        )

    result = executor_with_clocks(
        runner,
        environment={"SAT_TEST_ENV": "available"},
    ).execute(request(model="provider/model"))

    command = observed["command"]
    assert isinstance(command, tuple)
    assert command[:4] == ("/opt/openclaw", "agent", "--agent", "planner")
    assert "--local" in command
    assert "--message-file" in command
    assert "--message" not in command
    assert ("--session-key", request().session_key) == (
        command[command.index("--session-key")],
        command[command.index("--session-key") + 1],
    )
    assert command[-3:] == ("--local", "--model", "provider/model")
    assert observed["prompt"] == request().prompt
    assert observed["mode"] == 0o600
    run_kwargs = observed["kwargs"]
    assert isinstance(run_kwargs, dict)
    assert run_kwargs["shell"] is False
    assert run_kwargs["stdin"] is subprocess.DEVNULL
    assert run_kwargs["timeout"] == 65
    assert run_kwargs["env"]["SAT_TEST_ENV"] == "available"
    assert result.status is AgentExecutionStatus.COMPLETED
    assert result.response_text == '{"kind":"implementation_plan"}'
    assert result.telemetry.stderr == "gateway diagnostic\n"


def test_openclaw_adapter_captures_runtime_telemetry() -> None:
    def runner(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=openclaw_result(),
            stderr="",
        )

    result = executor_with_clocks(runner).execute(request())

    telemetry = result.telemetry
    assert telemetry.started_at == STARTED
    assert telemetry.finished_at == STARTED + timedelta(milliseconds=125)
    assert telemetry.duration_ms == 125
    assert telemetry.openclaw_duration_ms == 912
    assert telemetry.openclaw_run_id is None
    assert telemetry.session_id == "session-123"
    assert telemetry.provider == "test-provider"
    assert telemetry.model == "test-provider/test-model"
    assert telemetry.usage == AgentTokenUsage(
        input_tokens=101,
        output_tokens=37,
        cache_read_tokens=11,
        cache_write_tokens=3,
        reasoning_tokens=7,
        total_tokens=159,
    )


def test_openclaw_adapter_ignores_reasoning_payload_for_artifact_text() -> None:
    envelope = json.loads(openclaw_result("final"))
    envelope["payloads"] = [
        {"text": "thinking", "isReasoning": True},
        {"text": "comment", "isCommentary": True},
        {"text": '{"kind":"implementation_plan"}'},
    ]

    def runner(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(envelope),
            stderr="",
        )

    result = executor_with_clocks(runner).execute(request())

    assert result.status is AgentExecutionStatus.COMPLETED
    assert result.response_text == '{"kind":"implementation_plan"}'


def test_openclaw_adapter_ignores_pinned_tool_diagnostic_payload() -> None:
    envelope = json.loads(openclaw_result("final"))
    envelope["payloads"] = [
        {"text": '{"kind":"implementation_plan"}'},
        {"text": "⚠️ 🛠️ Exec failed: `ruff check .` (workspace)"},
    ]

    def runner(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(envelope),
            stderr="",
        )

    result = executor_with_clocks(runner).execute(request())

    assert result.status is AgentExecutionStatus.COMPLETED
    assert result.response_text == '{"kind":"implementation_plan"}'


def test_openclaw_adapter_parses_gateway_result_envelope() -> None:
    gateway_response = {
        "runId": "openclaw-run-123",
        "status": "ok",
        "result": json.loads(openclaw_result()),
    }

    def runner(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(gateway_response),
            stderr="",
        )

    result = executor_with_clocks(runner).execute(request())

    assert result.status is AgentExecutionStatus.COMPLETED
    assert result.telemetry.openclaw_run_id == "openclaw-run-123"
    assert result.telemetry.model == "test-provider/test-model"


def test_openclaw_adapter_preserves_qualified_model_reference() -> None:
    response = json.loads(openclaw_result())
    response["meta"]["agentMeta"]["model"] = "test-provider/test-model"

    def runner(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(response),
            stderr="",
        )

    result = executor_with_clocks(runner).execute(request())

    assert result.status is AgentExecutionStatus.COMPLETED
    assert result.telemetry.model == "test-provider/test-model"


def test_openclaw_adapter_qualifies_nested_provider_model_id() -> None:
    response = json.loads(openclaw_result())
    response["meta"]["agentMeta"]["model"] = "upstream/test-model"

    def runner(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(response),
            stderr="",
        )

    result = executor_with_clocks(runner).execute(request())

    assert result.status is AgentExecutionStatus.COMPLETED
    assert result.telemetry.model == "test-provider/upstream/test-model"


@pytest.mark.parametrize(
    ("stdout", "error"),
    [
        ("not JSON", "not one JSON object"),
        (json.dumps({"status": "error"}), "did not complete"),
        (
            json.dumps(
                {
                    "payloads": [{"text": "one"}, {"text": "two"}],
                }
            ),
            "exactly one visible",
        ),
        (
            json.dumps(
                {
                    "payloads": [{"text": "failure", "isError": True}],
                }
            ),
            "error reply",
        ),
    ],
)
def test_openclaw_adapter_rejects_invalid_protocol_response(
    stdout: str,
    error: str,
) -> None:
    def runner(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    result = executor_with_clocks(runner).execute(request())

    assert result.status is AgentExecutionStatus.INVALID_RESPONSE
    assert error in (result.error or "")
    assert result.telemetry.stdout == stdout
    assert result.telemetry.exit_code == 0


def test_openclaw_adapter_preserves_metadata_for_an_invalid_reply_count() -> None:
    envelope = json.loads(openclaw_result())
    envelope["payloads"] = [{"text": "one"}, {"text": "two"}]

    def runner(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(envelope),
            stderr="",
        )

    result = executor_with_clocks(runner).execute(request())

    assert result.status is AgentExecutionStatus.INVALID_RESPONSE
    assert result.telemetry.provider == "test-provider"
    assert result.telemetry.model == "test-provider/test-model"
    assert result.telemetry.usage is not None
    assert result.telemetry.usage.total_tokens == 159


def test_openclaw_adapter_classifies_internal_timeout_and_preserves_usage() -> None:
    envelope = json.loads(openclaw_result())
    envelope["payloads"] = [
        {"text": "LLM request failed."},
        {
            "text": (
                "Request timed out before a response was generated. Please try again."
            )
        },
    ]

    def runner(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(envelope),
            stderr="embedded run timeout",
        )

    result = executor_with_clocks(runner).execute(request())

    assert result.status is AgentExecutionStatus.TIMED_OUT
    assert result.error == "OpenClaw reported an Agent timeout"
    assert result.telemetry.timed_out is True
    assert result.telemetry.exit_code == 0
    assert result.telemetry.provider == "test-provider"
    assert result.telemetry.model == "test-provider/test-model"
    assert result.telemetry.usage is not None
    assert result.telemetry.usage.input_tokens == 101


def test_openclaw_adapter_classifies_a_declared_provider_failure() -> None:
    envelope = json.loads(openclaw_result())
    envelope["payloads"] = [{"text": "LLM request failed."}]
    envelope["meta"]["agentMeta"].pop("usage")

    def runner(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(envelope),
            stderr="provider returned status 409",
        )

    result = executor_with_clocks(runner).execute(request())

    assert result.status is AgentExecutionStatus.PROVIDER_FAILED
    assert "provider failure" in (result.error or "")
    assert result.telemetry.exit_code == 0
    assert result.telemetry.provider == "test-provider"


def test_openclaw_adapter_ignores_a_recovered_provider_diagnostic() -> None:
    envelope = json.loads(openclaw_result())
    envelope["payloads"] = [
        {"text": "LLM request failed."},
        {"text": '{"kind":"implementation_plan"}'},
    ]

    def runner(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(envelope),
            stderr="provider recovered",
        )

    result = executor_with_clocks(runner).execute(request())

    assert result.status is AgentExecutionStatus.COMPLETED
    assert result.response_text == '{"kind":"implementation_plan"}'


def test_openclaw_adapter_preserves_nonzero_exit_evidence() -> None:
    def runner(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            17,
            stdout="partial output",
            stderr="provider unavailable",
        )

    result = executor_with_clocks(runner).execute(request())

    assert result.status is AgentExecutionStatus.PROCESS_FAILED
    assert result.response_text is None
    assert result.telemetry.exit_code == 17
    assert result.telemetry.stdout == "partial output"
    assert result.telemetry.stderr == "provider unavailable"


def test_openclaw_adapter_preserves_timeout_evidence() -> None:
    def runner(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(
            command,
            kwargs["timeout"],
            output=b"partial stdout",
            stderr=b"partial stderr",
        )

    result = executor_with_clocks(runner).execute(request())

    assert result.status is AgentExecutionStatus.TIMED_OUT
    assert result.telemetry.timed_out is True
    assert result.telemetry.exit_code is None
    assert result.telemetry.stdout == "partial stdout"
    assert result.telemetry.stderr == "partial stderr"


def test_openclaw_adapter_records_launch_failure() -> None:
    def runner(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError(command[0])

    result = executor_with_clocks(runner).execute(request())

    assert result.status is AgentExecutionStatus.LAUNCH_FAILED
    assert result.telemetry.exit_code is None
    assert "/opt/openclaw" in result.telemetry.stderr


def test_stable_session_key_is_deterministic_and_phase_scoped() -> None:
    first = stable_session_key(
        run_id="task-manager-001",
        role=AgentRole.TESTER,
        iteration=2,
        expected_kind=ArtifactKind.TEST_REPORT,
    )
    repeated = stable_session_key(
        run_id="task-manager-001",
        role=AgentRole.TESTER,
        iteration=2,
        expected_kind=ArtifactKind.TEST_REPORT,
    )

    assert first == repeated
    assert first == "agent:tester:sat-task-manager-001-i2-test-report"
    assert first != request().session_key


def test_request_rejects_a_role_output_mismatch() -> None:
    with pytest.raises(ValidationError, match="cannot produce"):
        request(expected_kind=ArtifactKind.REVIEW_REPORT)


def test_scripted_executor_is_protocol_compatible_and_fifo() -> None:
    scripted = ScriptedAgentExecutor(['{"sequence":1}', '{"sequence":2}'])

    assert isinstance(scripted, AgentExecutor)
    first = scripted.execute(request())
    second = scripted.execute(request())

    assert first.response_text == '{"sequence":1}'
    assert second.response_text == '{"sequence":2}'
    assert scripted.requests == [request(), request()]
    assert scripted.remaining == 0
    assert first.telemetry.provider == "scripted"
    assert first.telemetry.session_key == request().session_key


def test_scripted_executor_fails_when_the_script_is_exhausted() -> None:
    scripted = ScriptedAgentExecutor([])

    with pytest.raises(ScriptedResponseExhaustedError, match="planner"):
        scripted.execute(request())
