"""Tests for shell-free OpenClaw and scripted Agent execution adapters."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import software_agent_team.execution as execution
from software_agent_team.artifacts import AgentRole, ArtifactKind
from software_agent_team.execution import (
    AgentExecutionActivity,
    AgentExecutionActivityKind,
    AgentExecutionError,
    AgentExecutionRequest,
    AgentExecutionResult,
    AgentExecutionStatus,
    AgentExecutor,
    AgentTokenUsage,
    AgentToolActionClass,
    AgentToolTargetClass,
    InitializationLivenessPolicy,
    OpenClawSubprocessExecutor,
    ProviderLivenessPolicy,
    ResponseFinalizationPolicy,
    ScriptedAgentExecutor,
    ScriptedResponseExhaustedError,
    resolve_provider_liveness_policy,
    stable_agent_session_key,
    stable_session_key,
)
from software_agent_team.invocation_lifecycle import (
    InitializationCheckpoint,
    InvocationPhase,
    InvocationStopReason,
)
from software_agent_team.process_lifecycle import ProcessLeaseStore
from software_agent_team.submissions import (
    ARTIFACT_SUBMISSION_PROTOCOL,
    AgentSubmissionContract,
    AgentSubmissionPurpose,
    AgentSubmissionStatus,
    canonical_json_sha256,
)
from software_agent_team.teams import AgentCapability

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


def test_invocation_timeout_schema_does_not_create_a_one_day_product_cap() -> None:
    assert request(timeout_seconds=86_401).timeout_seconds == 86_401


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


def live_liveness_executor(
    tmp_path: Path,
    program: str,
    *,
    initialization_policy: InitializationLivenessPolicy | None = None,
    process_lease_store: ProcessLeaseStore | None = None,
    process_grace_seconds: float = 1,
    run_deadline_at: datetime | None = None,
    response_finalization_policy: ResponseFinalizationPolicy | None = None,
) -> OpenClawSubprocessExecutor:
    binary = tmp_path / "fake-openclaw"
    binary.write_text(
        f"#!{sys.executable}\n" + program,
        encoding="utf-8",
    )
    binary.chmod(0o700)
    state = tmp_path / "state"
    state.mkdir()
    policy = ProviderLivenessPolicy(
        model="provider/model",
        silence_seconds=0.30,
        stall_grace_seconds=0.10,
        source="test provider contract",
    )
    return OpenClawSubprocessExecutor(
        openclaw_binary=binary,
        environment={"OPENCLAW_STATE_DIR": str(state)},
        process_grace_seconds=process_grace_seconds,
        run_deadline_at=run_deadline_at,
        liveness_poll_seconds=0.02,
        liveness_policies={policy.model: policy},
        initialization_policy=initialization_policy,
        response_finalization_policy=response_finalization_policy,
        process_lease_store=process_lease_store,
    )


FAKE_OPENCLAW_SETUP = r"""
import json
import os
import sys
import time
from pathlib import Path

def argument(name):
    return sys.argv[sys.argv.index(name) + 1]

agent_id = argument("--agent")
session_key = argument("--session-key")
prompt = Path(argument("--message-file")).read_text(encoding="utf-8")
session_id = "liveness-session"
sessions = Path(os.environ["OPENCLAW_STATE_DIR"]) / "agents" / agent_id / "sessions"
sessions.mkdir(parents=True, exist_ok=True)
transcript = sessions / f"{session_id}.jsonl"
records = [
    {"type": "session", "id": session_id},
    {"type": "message", "message": {"role": "user", "content": prompt}},
]

def write_records():
    transcript.write_text(
        "\n".join(json.dumps(item) for item in records) + "\n",
        encoding="utf-8",
    )

def append_raw(value):
    raw_path = Path(os.environ["OPENCLAW_RAW_STREAM_PATH"])
    with raw_path.open("a", encoding="utf-8") as stream:
        payload = {"event": "assistant_text_stream", "content": value}
        stream.write(json.dumps(payload) + "\n")

def finish():
    records.append({
        "type": "message",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
    })
    write_records()
    print(json.dumps({
        "payloads": [{"text": "{\\\"kind\\\":\\\"implementation_plan\\\"}"}],
        "meta": {"agentMeta": {
            "sessionId": session_id,
            "provider": "provider",
            "model": "model",
            "usage": {"input": 1, "output": 1},
        }},
    }))

write_records()
session_index = sessions / "sessions.json"
session_index_temporary = sessions / ".sessions.json.tmp"
session_index_temporary.write_text(
    json.dumps({session_key: {"sessionId": session_id}}),
    encoding="utf-8",
)
os.replace(session_index_temporary, session_index)
"""


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


def test_product_invocation_disables_wall_clock_without_disabling_openclaw() -> None:
    observed: dict[str, object] = {}

    def runner(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        observed["command"] = tuple(command)
        observed["kwargs"] = kwargs
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=openclaw_result(),
            stderr="",
        )

    result = executor_with_clocks(runner).execute(request(timeout_seconds=0))

    command = observed["command"]
    assert command[command.index("--timeout") + 1] == "0"
    assert "timeout" not in observed["kwargs"]
    assert result.status is AgentExecutionStatus.COMPLETED


def test_user_run_deadline_bounds_product_call_by_remaining_time() -> None:
    observed: dict[str, object] = {}

    def runner(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        observed["command"] = tuple(command)
        observed["kwargs"] = kwargs
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=openclaw_result(),
            stderr="",
        )

    result = executor_with_clocks(
        runner,
        run_deadline_at=STARTED + timedelta(seconds=75, milliseconds=1),
    ).execute(request(timeout_seconds=0))

    command = observed["command"]
    assert command[command.index("--timeout") + 1] == "76"
    assert observed["kwargs"]["timeout"] == pytest.approx(110.001)
    assert result.status is AgentExecutionStatus.COMPLETED


def test_expired_user_run_deadline_refuses_provider_call() -> None:
    called = False

    def runner(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        nonlocal called
        called = True
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    result = executor_with_clocks(
        runner,
        run_deadline_at=STARTED,
    ).execute(request(timeout_seconds=0))

    assert not called
    assert result.status is AgentExecutionStatus.TIMED_OUT
    assert result.telemetry.timed_out
    assert result.error == "User-authorized whole-run deadline expired before this call"


def test_run_deadline_requires_timezone() -> None:
    with pytest.raises(AgentExecutionError, match="UTC offset"):
        OpenClawSubprocessExecutor(
            run_deadline_at=datetime(2026, 8, 10, 12, 0),
        )


def test_liveness_policy_follows_model_locality_and_provider_timeout() -> None:
    cloud = resolve_provider_liveness_policy(
        model="provider/cloud",
        local=False,
    )
    local = resolve_provider_liveness_policy(
        model="provider/local",
        local=True,
    )
    configured = resolve_provider_liveness_policy(
        model="provider/configured",
        local=True,
        provider_request_timeout_seconds=40,
    )
    extended = resolve_provider_liveness_policy(
        model="provider/slow-cloud",
        local=False,
        provider_request_timeout_seconds=300,
    )

    assert cloud.silence_seconds == 120
    assert cloud.suspect_after_seconds == 90
    assert local.silence_seconds == 300
    assert local.suspect_after_seconds == 270
    assert configured.silence_seconds == 40
    assert configured.stall_grace_seconds == 10
    assert configured.suspect_after_seconds == 30
    assert extended.silence_seconds == 300
    assert extended.suspect_after_seconds == 270


def test_lifecycle_activity_kind_must_match_its_exact_phase() -> None:
    with pytest.raises(ValidationError, match="kind must match"):
        AgentExecutionActivity(
            kind=AgentExecutionActivityKind.INVOCATION_LAUNCHED,
            agent_id="planner",
            session_key="agent:planner:test",
            elapsed_ms=0,
            invocation_phase=InvocationPhase.INITIALIZING,
        )


@pytest.mark.parametrize(
    "late_kind",
    [
        AgentExecutionActivityKind.TOOL_STARTED,
        AgentExecutionActivityKind.TOOL_COMPLETED,
        AgentExecutionActivityKind.PROVIDER_STREAM,
        AgentExecutionActivityKind.STALL_RECOVERED,
    ],
)
def test_shutdown_owns_late_provider_progress(
    late_kind: AgentExecutionActivityKind,
) -> None:
    activities: list[AgentExecutionActivity] = []
    invocation = request(model="provider/model")
    lifecycle = execution._InvocationLifecycleRecorder(
        request=invocation,
        started_monotonic=0,
        process_grace_seconds=35,
        initialization_policy=InitializationLivenessPolicy(
            no_progress_seconds=90, stall_grace_seconds=15, source="test"
        ),
        response_finalization_policy=ResponseFinalizationPolicy(
            no_progress_seconds=60, stall_grace_seconds=10, source="test"
        ),
        activity_handler=activities.append,
        monotonic=lambda: 0,
    )
    lifecycle.process_launched(now=0, process_group_targeted=True)
    lifecycle.provider_ready(InitializationCheckpoint.CURRENT_TURN, now=1)
    monitor = execution._ProviderLivenessMonitor(
        request=invocation,
        policy=ProviderLivenessPolicy(
            model="provider/model",
            silence_seconds=300,
            stall_grace_seconds=30,
            source="test",
        ),
        raw_stream_path=None,
        state_dir=None,
        started_monotonic=0,
        activity_handler=activities.append,
        initialization_monitor=None,
        lifecycle=lifecycle,
    )
    lifecycle.request_stop(
        InvocationStopReason.USER_CANCEL, now=2, action="User cancelled"
    )
    before = list(activities)
    # The final session poll still contributes complete historical counters.
    monitor.previous_tool_completed = 383
    lifecycle.tool_state(0, 383, now=3)
    monitor._emit(late_kind, 3)
    assert activities == before
    assert lifecycle.phase is InvocationPhase.STOPPING
    assert lifecycle.completed_tool_count == 383
    assert monitor.evidence().tool_completed_count == 383
    # A delayed initialization observer must not reopen the lifecycle either.
    lifecycle.initialization_progress(InitializationCheckpoint.CURRENT_TURN, now=4)
    assert activities == before
    assert lifecycle.phase is InvocationPhase.STOPPING


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


def test_openclaw_adapter_accepts_bound_submission_after_async_process_chain(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    semantic_payload = {"summary": "typed result"}
    semantic_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"summary": {"type": "string"}},
        "required": ["summary"],
    }
    transport_schema = {"type": "object", "additionalProperties": True}
    submission_contract = AgentSubmissionContract.from_schema(
        semantic_schema,
        purpose=AgentSubmissionPurpose.ARTIFACT,
        transport_schema=transport_schema,
    )

    def runner(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        session_id = "typed-session"
        agent_id = command[command.index("--agent") + 1]
        session_key = command[command.index("--session-key") + 1]
        prompt_path = Path(command[command.index("--message-file") + 1])
        prompt = prompt_path.read_text(encoding="utf-8")
        schema_path = Path(environment["SAT_ARTIFACT_SUBMISSION_SCHEMA_PATH"])
        expected_tool_schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {"artifact": transport_schema},
            "required": ["artifact"],
        }
        assert json.loads(schema_path.read_text(encoding="utf-8")) == (
            expected_tool_schema
        )
        assert environment["SAT_ARTIFACT_SUBMISSION_SCHEMA_SHA256"] == (
            canonical_json_sha256(semantic_schema)
        )
        assert environment["SAT_ARTIFACT_SUBMISSION_PARAMETERS_SHA256"] == (
            canonical_json_sha256(expected_tool_schema)
        )
        external_id = "provider-call-1"
        output_path = Path(environment["SAT_ARTIFACT_SUBMISSION_OUTPUT_PATH"])
        output_path.write_text(
            json.dumps(
                {
                    "protocol": ARTIFACT_SUBMISSION_PROTOCOL,
                    "binding_sha256": environment[
                        "SAT_ARTIFACT_SUBMISSION_BINDING_SHA256"
                    ],
                    "schema_sha256": environment[
                        "SAT_ARTIFACT_SUBMISSION_SCHEMA_SHA256"
                    ],
                    "tool_call_id": external_id,
                    "payload": semantic_payload,
                }
            ),
            encoding="utf-8",
        )
        output_path.chmod(0o600)
        sessions = state / "agents" / agent_id / "sessions"
        sessions.mkdir(parents=True)
        records = [
            {"type": "session", "id": session_id},
            {"type": "message", "message": {"role": "user", "content": prompt}},
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "toolCall",
                            "id": "async-exec",
                            "name": "exec",
                            "arguments": {"command": "uv run pytest"},
                        }
                    ],
                },
            },
            {
                "type": "message",
                "message": {
                    "role": "toolResult",
                    "toolCallId": "async-exec",
                    "toolName": "exec",
                    "isError": False,
                    "content": [{"type": "text", "text": "Command still running."}],
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
                    "content": [{"type": "text", "text": "tests passed"}],
                    "details": {
                        "status": "completed",
                        "sessionId": "brisk-meadow",
                        "exitCode": 0,
                        "exitReason": "exit",
                        "aggregated": "tests passed",
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
                            "id": external_id,
                            "name": "sat_submit_artifact",
                            "arguments": {"artifact": semantic_payload},
                        }
                    ],
                },
            },
            {
                "type": "message",
                "message": {
                    "role": "toolResult",
                    "toolCallId": external_id,
                    "toolName": "sat_submit_artifact",
                    "isError": False,
                    "content": [{"type": "text", "text": "accepted"}],
                    "details": {"status": "completed"},
                },
            },
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "not valid JSON"}],
                },
            },
        ]
        transcript = sessions / f"{session_id}.jsonl"
        transcript.write_text(
            "\n".join(json.dumps(record) for record in records) + "\n",
            encoding="utf-8",
        )
        (sessions / "sessions.json").write_text(
            json.dumps(
                {
                    session_key: {
                        "sessionId": session_id,
                        "sessionFile": str(transcript),
                    }
                }
            ),
            encoding="utf-8",
        )
        response = json.loads(openclaw_result("not valid JSON"))
        response["meta"]["agentMeta"]["sessionId"] = session_id
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(response),
            stderr="",
        )

    result = executor_with_clocks(
        runner,
        environment={"OPENCLAW_STATE_DIR": str(state)},
    ).execute(request(submission_contract=submission_contract))

    assert result.status is AgentExecutionStatus.COMPLETED
    assert result.response_text == "not valid JSON"
    assert result.semantic_submission is not None
    assert result.semantic_submission.payload == semantic_payload
    assert result.submission_evidence is not None
    assert result.submission_evidence.status is AgentSubmissionStatus.ACCEPTED
    assert tuple(call.outcome.value for call in result.telemetry.tool_calls) == (
        "deferred",
        "succeeded",
        "succeeded",
    )
    assert result.telemetry.tool_calls[-1].tool_name == "sat_submit_artifact"


def test_openclaw_adapter_rejects_missing_required_submission() -> None:
    submission_contract = AgentSubmissionContract.from_schema(
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
        },
        purpose=AgentSubmissionPurpose.ARTIFACT,
    )

    def runner(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=openclaw_result("fallback prose"),
            stderr="",
        )

    result = executor_with_clocks(runner).execute(
        request(submission_contract=submission_contract)
    )

    assert result.status is AgentExecutionStatus.INVALID_RESPONSE
    assert result.response_text is None
    assert result.semantic_submission is None
    assert result.submission_evidence is not None
    assert result.submission_evidence.status is AgentSubmissionStatus.MISSING
    assert result.submission_evidence.diagnostic_code == "submission_missing"


def test_openclaw_adapter_classifies_tool_result_termination_before_submission(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    submission_contract = AgentSubmissionContract.from_schema(
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
        },
        purpose=AgentSubmissionPurpose.ARTIFACT,
    )

    def runner(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        session_id = "incomplete-session"
        agent_id = command[command.index("--agent") + 1]
        session_key = command[command.index("--session-key") + 1]
        prompt = Path(command[command.index("--message-file") + 1]).read_text(
            encoding="utf-8"
        )
        sessions = state / "agents" / agent_id / "sessions"
        sessions.mkdir(parents=True)
        records = [
            {"type": "session", "id": session_id},
            {"type": "message", "message": {"role": "user", "content": prompt}},
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "toolCall",
                            "id": "provider-edit-1",
                            "name": "edit",
                            "arguments": {
                                "path": "README.md",
                                "oldText": "same",
                                "newText": "same",
                            },
                        }
                    ],
                },
            },
            {
                "type": "message",
                "message": {
                    "role": "toolResult",
                    "toolCallId": "provider-edit-1",
                    "toolName": "edit",
                    "isError": False,
                    "content": [{"type": "text", "text": "No changes made."}],
                    "details": {"status": "completed"},
                },
            },
        ]
        transcript = sessions / f"{session_id}.jsonl"
        transcript.write_text(
            "\n".join(json.dumps(record) for record in records) + "\n",
            encoding="utf-8",
        )
        (sessions / "sessions.json").write_text(
            json.dumps(
                {
                    session_key: {
                        "sessionId": session_id,
                        "sessionFile": str(transcript),
                    }
                }
            ),
            encoding="utf-8",
        )
        response = json.loads(openclaw_result("No changes made."))
        response["meta"]["agentMeta"]["sessionId"] = session_id
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(response),
            stderr="",
        )

    result = executor_with_clocks(
        runner,
        environment={"OPENCLAW_STATE_DIR": str(state)},
    ).execute(request(submission_contract=submission_contract))

    assert result.status is AgentExecutionStatus.UPSTREAM_INCOMPLETE
    assert result.response_text is None
    assert result.semantic_submission is None
    assert result.submission_evidence is not None
    assert result.submission_evidence.status is AgentSubmissionStatus.MISSING
    assert result.submission_evidence.diagnostic_code == (
        "upstream_incomplete_after_tool_result"
    )
    assert result.telemetry.tool_calls[-1].tool_name == "edit"
    assert result.telemetry.invocation_lifecycle is not None
    assert (
        result.telemetry.invocation_lifecycle.shutdown.reason
        is InvocationStopReason.UPSTREAM_INCOMPLETE
    )


def test_live_openclaw_process_acquires_and_releases_durable_ownership(
    tmp_path: Path,
) -> None:
    class TrackingStore(ProcessLeaseStore):
        acquired = 0
        released = 0

        def acquire(self, **kwargs):  # type: ignore[no-untyped-def]
            lease = super().acquire(**kwargs)
            self.acquired += 1
            return lease

        def release(self, lease):  # type: ignore[no-untyped-def]
            super().release(lease)
            self.released += 1

    store = TrackingStore(tmp_path / "process-leases")
    executor = live_liveness_executor(
        tmp_path,
        FAKE_OPENCLAW_SETUP + "\nfinish()\n",
        process_lease_store=store,
    )

    result = executor.execute(request(timeout_seconds=0, model="provider/model"))

    assert result.status is AgentExecutionStatus.COMPLETED
    lifecycle = result.telemetry.invocation_lifecycle
    assert lifecycle is not None
    assert lifecycle.initialization.mode == "enforced"
    assert InitializationCheckpoint.CURRENT_TURN in (
        lifecycle.initialization.checkpoints
    )
    assert store.acquired == 1
    assert store.released == 1
    assert store.inspect().processes == ()


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
                    "payloads": [
                        {"text": "thinking", "isReasoning": True},
                        {"text": "comment", "isCommentary": True},
                    ],
                }
            ),
            "at least one visible",
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


def test_openclaw_adapter_combines_visible_payloads_and_preserves_metadata() -> None:
    envelope = json.loads(openclaw_result())
    envelope["payloads"] = [
        {"text": '{"kind":"implementation_plan"}'},
        {"text": "⚠️ ✍️ Write: to /tmp/placeholder (2 chars) failed"},
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
    assert result.response_text == (
        '{"kind":"implementation_plan"}\n\n'
        "⚠️ ✍️ Write: to /tmp/placeholder (2 chars) failed"
    )
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


@pytest.mark.parametrize("stream_first", [False, True])
def test_provider_stream_activity_renews_lease_beyond_total_wall_clock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stream_first: bool,
) -> None:
    if stream_first:
        # Raw transport may become attributable before either session observer.
        # Force that ordering without depending on interpreter scheduling.
        monkeypatch.setattr(
            execution, "inspect_openclaw_initialization", lambda **_: None
        )
        monkeypatch.setattr(
            execution, "inspect_openclaw_session_activity", lambda **_: None
        )
    executor = live_liveness_executor(
        tmp_path,
        FAKE_OPENCLAW_SETUP
        + r"""
for index in range(6):
    time.sleep(0.10)
    append_raw(f"private streamed content {index}")
finish()
""",
    )
    activities = []

    result = executor.execute(
        request(timeout_seconds=0, model="provider/model"),
        activity_handler=activities.append,
    )

    assert result.status is AgentExecutionStatus.COMPLETED
    assert result.telemetry.duration_ms >= 500
    assert result.telemetry.provider_liveness is not None
    assert result.telemetry.provider_liveness.raw_stream_observed
    assert result.telemetry.provider_liveness.lease_started
    assert result.telemetry.provider_liveness.lease_start_source in {
        "current_turn",
        "provider_stream",
    }
    if stream_first:
        assert (
            result.telemetry.provider_liveness.lease_start_source == "provider_stream"
        )
    assert result.telemetry.provider_liveness.provider_activity_observations >= 5
    assert not result.telemetry.provider_liveness.stalled
    assert AgentExecutionActivityKind.PROVIDER_STREAM in {
        activity.kind for activity in activities
    }
    assert "private streamed content" not in result.model_dump_json()
    assert "private streamed content" not in " ".join(map(str, activities))


def test_sustained_provider_silence_warns_then_stops_exact_process(
    tmp_path: Path,
) -> None:
    executor = live_liveness_executor(
        tmp_path,
        FAKE_OPENCLAW_SETUP + "\ntime.sleep(30)\n",
    )
    activities = []

    result = executor.execute(
        request(timeout_seconds=0, model="provider/model"),
        activity_handler=activities.append,
    )

    assert result.status is AgentExecutionStatus.PROVIDER_STALLED
    assert result.telemetry.duration_ms < 2_000
    assert result.telemetry.timed_out is False
    assert result.telemetry.provider_liveness is not None
    assert result.telemetry.provider_liveness.stalled
    assert result.telemetry.provider_liveness.stall_suspected_count == 1
    assert result.telemetry.provider_liveness.maximum_inactivity_ms >= 300
    lifecycle = result.telemetry.invocation_lifecycle
    assert lifecycle is not None
    assert lifecycle.shutdown.reason is InvocationStopReason.PROVIDER_STALL
    assert [transition.phase for transition in lifecycle.transitions][-3:] == [
        InvocationPhase.STOPPING,
        InvocationPhase.COLLECTING_EVIDENCE,
        InvocationPhase.STOPPED,
    ]
    assert lifecycle.shutdown.cleanup_completed
    assert [
        activity.kind
        for activity in activities
        if activity.kind
        in {
            AgentExecutionActivityKind.STALL_SUSPECTED,
            AgentExecutionActivityKind.PROVIDER_STALLED,
        }
    ] == [
        AgentExecutionActivityKind.STALL_SUSPECTED,
        AgentExecutionActivityKind.PROVIDER_STALLED,
    ]
    assert executor.interrupt("planner") == 0


def test_provider_activity_during_stall_grace_recovers_without_cutoff(
    tmp_path: Path,
) -> None:
    executor = live_liveness_executor(
        tmp_path,
        FAKE_OPENCLAW_SETUP
        + r"""
time.sleep(0.24)
append_raw("recovered private response")
time.sleep(0.10)
finish()
""",
    )
    activities = []

    result = executor.execute(
        request(timeout_seconds=0, model="provider/model"),
        activity_handler=activities.append,
    )

    assert result.status is AgentExecutionStatus.COMPLETED
    assert result.telemetry.provider_liveness is not None
    assert result.telemetry.provider_liveness.stall_suspected_count == 1
    assert result.telemetry.provider_liveness.stall_recovered_count == 1
    kinds = [activity.kind for activity in activities]
    assert kinds.index(AgentExecutionActivityKind.STALL_SUSPECTED) < kinds.index(
        AgentExecutionActivityKind.STALL_RECOVERED
    )


def test_process_completion_cannot_be_retroactively_classified_as_a_stall(
    tmp_path: Path,
) -> None:
    executor = live_liveness_executor(
        tmp_path,
        FAKE_OPENCLAW_SETUP
        + r"""
time.sleep(0.35)
finish()
""",
    )
    # Let the exact process complete between observations, after the synthetic
    # lease used by this test. The returned response owns the terminal state.
    executor.liveness_poll_seconds = 0.50
    activities = []

    result = executor.execute(
        request(timeout_seconds=0, model="provider/model"),
        activity_handler=activities.append,
    )

    assert result.status is AgentExecutionStatus.COMPLETED
    assert result.telemetry.provider_liveness is not None
    assert not result.telemetry.provider_liveness.stalled
    assert AgentExecutionActivityKind.PROVIDER_STALLED not in {
        activity.kind for activity in activities
    }


def test_active_tool_suspends_provider_lease_until_tool_result(
    tmp_path: Path,
) -> None:
    executor = live_liveness_executor(
        tmp_path,
        FAKE_OPENCLAW_SETUP
        + r"""
time.sleep(0.05)
records.append({
    "type": "message",
    "message": {"role": "assistant", "content": [{
        "type": "toolCall", "id": "tool-1", "name": "exec",
        "arguments": {"command": "pytest -q"}
    }]},
})
write_records()
time.sleep(0.50)
records.append({
    "type": "message",
    "message": {
        "role": "toolResult", "toolCallId": "tool-1", "toolName": "exec",
        "isError": False, "content": [{"type": "text", "text": "private tool output"}],
        "details": {"status": "completed", "exitCode": 0},
    },
})
write_records()
append_raw("provider resumed")
finish()
""",
    )
    activities = []

    result = executor.execute(
        request(timeout_seconds=0, model="provider/model"),
        activity_handler=activities.append,
    )

    assert result.status is AgentExecutionStatus.COMPLETED
    assert result.telemetry.duration_ms >= 500
    assert result.telemetry.provider_liveness is not None
    assert result.telemetry.provider_liveness.tool_started_count == 1
    assert result.telemetry.provider_liveness.tool_completed_count == 1
    assert result.telemetry.provider_liveness.stall_suspected_count == 0
    started = next(
        activity
        for activity in activities
        if activity.kind is AgentExecutionActivityKind.TOOL_STARTED
    )
    completed = next(
        activity
        for activity in activities
        if activity.kind is AgentExecutionActivityKind.TOOL_COMPLETED
    )
    assert started.active_tool_count == 1
    assert started.completed_tool_count == 0
    assert started.tool_action_class is AgentToolActionClass.TESTING
    assert started.tool_target_class is AgentToolTargetClass.QUALITY_CHECKS
    assert started.tool_detail == "pytest"
    assert completed.active_tool_count == 0
    assert completed.completed_tool_count == 1
    assert completed.tool_action_class is AgentToolActionClass.TESTING
    assert completed.tool_target_class is AgentToolTargetClass.QUALITY_CHECKS
    assert completed.tool_detail == "pytest"
    assert (
        "private tool output"
        not in result.telemetry.provider_liveness.model_dump_json()
    )


def test_coalesced_tool_history_does_not_repeat_provider_wait_phase(
    tmp_path: Path,
) -> None:
    executor = live_liveness_executor(
        tmp_path,
        FAKE_OPENCLAW_SETUP
        + r"""
records.extend([
    {
        "type": "message",
        "message": {"role": "assistant", "content": [{
            "type": "toolCall", "id": "tool-1", "name": "exec",
            "arguments": {"command": "test"}
        }]},
    },
    {
        "type": "message",
        "message": {
            "role": "toolResult", "toolCallId": "tool-1", "toolName": "exec",
            "isError": False, "content": [{"type": "text", "text": "done"}],
            "details": {"status": "completed", "exitCode": 0},
        },
    },
])
write_records()
finish()
""",
    )
    # Ensure the first session snapshot coalesces the historical start and
    # completion instead of observing an active interval between them.
    executor.liveness_poll_seconds = 0.20
    activities = []

    result = executor.execute(
        request(timeout_seconds=0, model="provider/model"),
        activity_handler=activities.append,
    )

    assert result.status is AgentExecutionStatus.COMPLETED
    liveness = result.telemetry.provider_liveness
    assert liveness is not None
    assert liveness.tool_started_count == 1
    assert liveness.tool_completed_count == 1
    lifecycle = result.telemetry.invocation_lifecycle
    assert lifecycle is not None
    phases = [transition.phase for transition in lifecycle.transitions]
    assert phases.count(InvocationPhase.PROVIDER_WAIT) == 1
    assert InvocationPhase.TOOL_ACTIVE not in phases
    kinds = [activity.kind for activity in activities]
    assert AgentExecutionActivityKind.TOOL_STARTED in kinds
    assert AgentExecutionActivityKind.TOOL_COMPLETED in kinds
    coalesced = [
        activity
        for activity in activities
        if activity.kind
        in {
            AgentExecutionActivityKind.TOOL_STARTED,
            AgentExecutionActivityKind.TOOL_COMPLETED,
        }
    ]
    assert all(activity.active_tool_count == 0 for activity in coalesced)
    assert all(activity.completed_tool_count == 1 for activity in coalesced)


def test_repeated_active_tool_snapshots_do_not_repeat_tool_active_phase(
    tmp_path: Path,
) -> None:
    executor = live_liveness_executor(
        tmp_path,
        FAKE_OPENCLAW_SETUP
        + r"""
records.append({
    "type": "message",
    "message": {"role": "assistant", "content": [{
        "type": "toolCall", "id": "tool-1", "name": "exec",
        "arguments": {"command": "one"}
    }]},
})
write_records()
time.sleep(0.08)
records.append({
    "type": "message",
    "message": {"role": "assistant", "content": [{
        "type": "toolCall", "id": "tool-2", "name": "exec",
        "arguments": {"command": "two"}
    }]},
})
write_records()
time.sleep(0.08)
for tool_id in ("tool-1", "tool-2"):
    records.append({
        "type": "message",
        "message": {
            "role": "toolResult", "toolCallId": tool_id, "toolName": "exec",
            "isError": False, "content": [{"type": "text", "text": "done"}],
            "details": {"status": "completed", "exitCode": 0},
        },
    })
write_records()
time.sleep(0.08)
finish()
""",
    )

    result = executor.execute(request(timeout_seconds=0, model="provider/model"))

    assert result.status is AgentExecutionStatus.COMPLETED
    liveness = result.telemetry.provider_liveness
    assert liveness is not None
    assert liveness.tool_started_count == 2
    assert liveness.tool_completed_count == 2
    lifecycle = result.telemetry.invocation_lifecycle
    assert lifecycle is not None
    phases = [transition.phase for transition in lifecycle.transitions]
    assert phases.count(InvocationPhase.TOOL_ACTIVE) == 1
    assert phases.count(InvocationPhase.PROVIDER_WAIT) == 2


def test_session_activity_readiness_cannot_overtake_initialization_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_activity_seen = False
    inspect_initialization = execution.inspect_openclaw_initialization
    inspect_activity = execution.inspect_openclaw_session_activity

    def delay_initialization_until_session_activity(**kwargs: object) -> object:
        if not session_activity_seen:
            return None
        return inspect_initialization(**kwargs)

    def observe_session_activity(**kwargs: object) -> object:
        nonlocal session_activity_seen
        observation = inspect_activity(**kwargs)
        if observation is not None:
            session_activity_seen = True
        return observation

    monkeypatch.setattr(
        execution,
        "inspect_openclaw_initialization",
        delay_initialization_until_session_activity,
    )
    monkeypatch.setattr(
        execution,
        "inspect_openclaw_session_activity",
        observe_session_activity,
    )
    executor = live_liveness_executor(
        tmp_path,
        FAKE_OPENCLAW_SETUP + "\ntime.sleep(0.15)\nfinish()\n",
    )

    result = executor.execute(request(timeout_seconds=0, model="provider/model"))

    assert result.status is AgentExecutionStatus.COMPLETED
    lifecycle = result.telemetry.invocation_lifecycle
    assert lifecycle is not None
    assert InitializationCheckpoint.CURRENT_TURN in lifecycle.initialization.checkpoints
    phases = [transition.phase for transition in lifecycle.transitions]
    assert phases.count(InvocationPhase.PROVIDER_WAIT) == 1
    assert phases.index(InvocationPhase.INITIALIZING) < phases.index(
        InvocationPhase.PROVIDER_WAIT
    )


def test_private_stream_can_be_the_single_initialization_ready_boundary(
    tmp_path: Path,
) -> None:
    executor = live_liveness_executor(
        tmp_path,
        r"""
import json
import os
import time
from pathlib import Path

raw_path = Path(os.environ["OPENCLAW_RAW_STREAM_PATH"])
raw_path.write_text(
    json.dumps({"event": "assistant_text_stream", "content": "private"}) + "\n",
    encoding="utf-8",
)
time.sleep(0.10)
print(json.dumps({
    "payloads": [{"text": "{\"kind\":\"implementation_plan\"}"}],
    "meta": {"agentMeta": {
        "sessionId": "stream-only-session",
        "provider": "provider",
        "model": "model",
        "usage": {"input": 1, "output": 1},
    }},
}))
""",
    )

    result = executor.execute(request(timeout_seconds=0, model="provider/model"))

    assert result.status is AgentExecutionStatus.COMPLETED
    lifecycle = result.telemetry.invocation_lifecycle
    assert lifecycle is not None
    assert lifecycle.initialization.checkpoints == (
        InitializationCheckpoint.PROCESS_LAUNCHED,
        InitializationCheckpoint.PROVIDER_STREAM,
    )
    phases = [transition.phase for transition in lifecycle.transitions]
    assert phases.count(InvocationPhase.PROVIDER_WAIT) == 1
    assert result.telemetry.provider_liveness is not None
    assert result.telemetry.provider_liveness.lease_start_source == "provider_stream"


def test_private_stream_without_session_remains_enforced_and_stalls(
    tmp_path: Path,
) -> None:
    executor = live_liveness_executor(
        tmp_path,
        r"""
import json
import os
import time
from pathlib import Path

raw_path = Path(os.environ["OPENCLAW_RAW_STREAM_PATH"])
raw_path.write_text(
    json.dumps({"event": "assistant_text_stream", "content": "private"}) + "\n",
    encoding="utf-8",
)
time.sleep(30)
""",
        process_grace_seconds=0.10,
    )

    result = executor.execute(request(timeout_seconds=0, model="provider/model"))

    assert result.status is AgentExecutionStatus.PROVIDER_STALLED
    liveness = result.telemetry.provider_liveness
    assert liveness is not None
    assert liveness.mode == "enforced"
    assert liveness.lease_start_source == "provider_stream"
    assert not liveness.session_observed
    assert liveness.stalled


def test_terminal_response_transfers_from_provider_to_finalization_guard(
    tmp_path: Path,
) -> None:
    executor = live_liveness_executor(
        tmp_path,
        FAKE_OPENCLAW_SETUP + "\nfinish()\ntime.sleep(0.42)\n",
        response_finalization_policy=ResponseFinalizationPolicy(
            no_progress_seconds=0.70,
            stall_grace_seconds=0.20,
            source="test response-finalization contract",
        ),
    )
    activities = []

    result = executor.execute(
        request(timeout_seconds=0, model="provider/model"),
        activity_handler=activities.append,
    )

    assert result.status is AgentExecutionStatus.COMPLETED
    assert result.telemetry.duration_ms >= 350
    liveness = result.telemetry.provider_liveness
    assert liveness is not None
    assert liveness.terminal_response_observed
    assert not liveness.stalled
    lifecycle = result.telemetry.invocation_lifecycle
    assert lifecycle is not None
    finalization = lifecycle.response_finalization
    assert finalization is not None
    assert finalization.mode == "enforced"
    assert finalization.terminal_response_observed
    assert not finalization.stalled
    assert liveness.maximum_inactivity_ms < finalization.maximum_no_progress_ms
    phases = [transition.phase for transition in lifecycle.transitions]
    assert phases.index(InvocationPhase.PROVIDER_WAIT) < phases.index(
        InvocationPhase.FINALIZING_RESPONSE
    )
    assert phases.index(InvocationPhase.FINALIZING_RESPONSE) < phases.index(
        InvocationPhase.STOPPING
    )
    assert AgentExecutionActivityKind.INVOCATION_FINALIZING_RESPONSE in {
        activity.kind for activity in activities
    }
    assert AgentExecutionActivityKind.PROVIDER_STALLED not in {
        activity.kind for activity in activities
    }


def test_response_finalization_progress_renews_guard_beyond_total_wall_clock(
    tmp_path: Path,
) -> None:
    program = (
        FAKE_OPENCLAW_SETUP
        + r"""
records.append({
    "type": "message",
    "message": {
        "role": "assistant",
        "content": [{"type": "text", "text": "done"}],
    },
})
write_records()
for index in range(3):
    time.sleep(0.16)
    print(f"finalization checkpoint {index}", file=sys.stderr, flush=True)
print(json.dumps({
    "payloads": [{"text": "{\"kind\":\"implementation_plan\"}"}],
    "meta": {"agentMeta": {
        "sessionId": session_id,
        "provider": "provider",
        "model": "model",
        "usage": {"input": 1, "output": 1},
    }},
}))
"""
    )
    executor = live_liveness_executor(
        tmp_path,
        program,
        response_finalization_policy=ResponseFinalizationPolicy(
            no_progress_seconds=0.22,
            stall_grace_seconds=0.10,
            source="test response-finalization contract",
        ),
    )
    activities = []

    result = executor.execute(
        request(timeout_seconds=0, model="provider/model"),
        activity_handler=activities.append,
    )

    assert result.status is AgentExecutionStatus.COMPLETED
    assert result.telemetry.duration_ms >= 400
    lifecycle = result.telemetry.invocation_lifecycle
    assert lifecycle is not None
    finalization = lifecycle.response_finalization
    assert finalization is not None
    assert finalization.output_progress_observations >= 2
    assert finalization.stall_suspected_count >= 1
    assert finalization.stall_recovered_count == finalization.stall_suspected_count
    assert not finalization.stalled
    kinds = [activity.kind for activity in activities]
    assert AgentExecutionActivityKind.FINALIZATION_PROGRESS in kinds
    assert AgentExecutionActivityKind.FINALIZATION_STALL_RECOVERED in kinds


def test_response_finalization_hang_has_distinct_typed_stop_and_cleanup(
    tmp_path: Path,
) -> None:
    executor = live_liveness_executor(
        tmp_path,
        FAKE_OPENCLAW_SETUP + "\nfinish()\ntime.sleep(30)\n",
        process_grace_seconds=0.10,
        response_finalization_policy=ResponseFinalizationPolicy(
            no_progress_seconds=0.24,
            stall_grace_seconds=0.08,
            source="test response-finalization contract",
        ),
    )
    activities = []

    result = executor.execute(
        request(timeout_seconds=0, model="provider/model"),
        activity_handler=activities.append,
    )

    assert result.status is AgentExecutionStatus.RESPONSE_FINALIZATION_STALLED
    liveness = result.telemetry.provider_liveness
    assert liveness is not None
    assert liveness.terminal_response_observed
    assert not liveness.stalled
    lifecycle = result.telemetry.invocation_lifecycle
    assert lifecycle is not None
    assert lifecycle.shutdown.reason is (
        InvocationStopReason.RESPONSE_FINALIZATION_STALL
    )
    assert lifecycle.shutdown.cleanup_completed
    finalization = lifecycle.response_finalization
    assert finalization is not None
    assert finalization.stalled
    relevant = [
        activity.kind
        for activity in activities
        if activity.kind
        in {
            AgentExecutionActivityKind.INVOCATION_FINALIZING_RESPONSE,
            AgentExecutionActivityKind.FINALIZATION_STALL_SUSPECTED,
            AgentExecutionActivityKind.RESPONSE_FINALIZATION_STALLED,
            AgentExecutionActivityKind.INVOCATION_STOPPING,
            AgentExecutionActivityKind.INVOCATION_COLLECTING_EVIDENCE,
            AgentExecutionActivityKind.INVOCATION_STOPPED,
        }
    ]
    assert relevant == [
        AgentExecutionActivityKind.INVOCATION_FINALIZING_RESPONSE,
        AgentExecutionActivityKind.FINALIZATION_STALL_SUSPECTED,
        AgentExecutionActivityKind.RESPONSE_FINALIZATION_STALLED,
        AgentExecutionActivityKind.INVOCATION_STOPPING,
        AgentExecutionActivityKind.INVOCATION_COLLECTING_EVIDENCE,
        AgentExecutionActivityKind.INVOCATION_STOPPED,
    ]


def test_missing_attributable_session_degrades_instead_of_guessing_stall(
    tmp_path: Path,
) -> None:
    executor = live_liveness_executor(
        tmp_path,
        r"""
import json
import time
time.sleep(0.40)
print(json.dumps({
    "payloads": [{"text": "{\\\"kind\\\":\\\"implementation_plan\\\"}"}],
    "meta": {"agentMeta": {
        "sessionId": "missing-session", "provider": "provider", "model": "model",
        "usage": {"input": 1, "output": 1},
    }},
}))
""",
    )
    activities = []

    result = executor.execute(
        request(timeout_seconds=0, model="provider/model"),
        activity_handler=activities.append,
    )

    assert result.status is AgentExecutionStatus.COMPLETED
    assert result.telemetry.provider_liveness is not None
    assert result.telemetry.provider_liveness.mode == "degraded"
    assert not result.telemetry.provider_liveness.lease_started
    assert not result.telemetry.provider_liveness.stalled
    assert AgentExecutionActivityKind.LIVENESS_DEGRADED in {
        activity.kind for activity in activities
    }


def test_slow_initialization_reaches_current_turn_before_provider_lease(
    tmp_path: Path,
) -> None:
    executor = live_liveness_executor(
        tmp_path,
        "import time\ntime.sleep(0.18)\n"
        + FAKE_OPENCLAW_SETUP
        # This case owns current-turn startup, not transport/session ordering.
        + "\ntime.sleep(0.05)\nfinish()\n",
        initialization_policy=InitializationLivenessPolicy(
            no_progress_seconds=2.0,
            stall_grace_seconds=0.40,
            source="test initialization contract",
        ),
    )
    # This test owns delayed current-turn publication, not whether a newly
    # spawned interpreter receives CPU inside a subsecond race. Establish the
    # earlier attributed checkpoint before the timed child behavior begins.
    sessions = tmp_path / "state" / "agents" / "planner" / "sessions"
    sessions.mkdir(parents=True)
    activities = []

    result = executor.execute(
        request(timeout_seconds=0, model="provider/model"),
        activity_handler=activities.append,
    )

    assert result.status is AgentExecutionStatus.COMPLETED
    lifecycle = result.telemetry.invocation_lifecycle
    assert lifecycle is not None
    assert lifecycle.initialization.checkpoints == (
        InitializationCheckpoint.PROCESS_LAUNCHED,
        InitializationCheckpoint.SESSION_INDEX,
        InitializationCheckpoint.SESSION_BOUND,
        InitializationCheckpoint.TRANSCRIPT_HEADER,
        InitializationCheckpoint.CURRENT_TURN,
    )
    assert lifecycle.initialization.baseline_checkpoint is (
        InitializationCheckpoint.SESSION_DIRECTORY
    )
    assert result.telemetry.provider_liveness is not None
    assert result.telemetry.provider_liveness.lease_start_source == "current_turn"
    phases = [transition.phase for transition in lifecycle.transitions]
    assert phases.index(InvocationPhase.INITIALIZING) < phases.index(
        InvocationPhase.PROVIDER_WAIT
    )
    assert phases[-2:] == [
        InvocationPhase.COLLECTING_EVIDENCE,
        InvocationPhase.STOPPED,
    ]


def test_preexisting_current_turn_cannot_keep_a_new_invocation_alive(
    tmp_path: Path,
) -> None:
    invocation = request(timeout_seconds=0, model="provider/model")
    executor = live_liveness_executor(
        tmp_path,
        "import time\ntime.sleep(30)\n",
        initialization_policy=InitializationLivenessPolicy(
            no_progress_seconds=0.22,
            stall_grace_seconds=0.08,
            source="test initialization contract",
        ),
        process_grace_seconds=0.10,
    )
    sessions = tmp_path / "state" / "agents" / "planner" / "sessions"
    sessions.mkdir(parents=True)
    session_id = "reused-session"
    (sessions / "sessions.json").write_text(
        json.dumps({invocation.session_key: {"sessionId": session_id}}),
        encoding="utf-8",
    )
    (sessions / f"{session_id}.jsonl").write_text(
        "\n".join(
            (
                json.dumps({"type": "session", "id": session_id}),
                json.dumps(
                    {
                        "type": "message",
                        "message": {"role": "user", "content": invocation.prompt},
                    }
                ),
                json.dumps(
                    {
                        "type": "message",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "prior"}],
                        },
                    }
                ),
            )
        )
        + "\n",
        encoding="utf-8",
    )
    result = executor.execute(invocation)

    assert result.status is AgentExecutionStatus.INITIALIZATION_STALLED
    lifecycle = result.telemetry.invocation_lifecycle
    assert lifecycle is not None
    assert lifecycle.initialization.baseline_checkpoint is (
        InitializationCheckpoint.CURRENT_TURN
    )
    assert lifecycle.initialization.baseline_matching_turn_count == 1
    assert lifecycle.initialization.checkpoints == (
        InitializationCheckpoint.PROCESS_LAUNCHED,
    )
    assert result.telemetry.provider_liveness is not None
    assert not result.telemetry.provider_liveness.lease_started


def test_reused_session_waits_for_a_new_occurrence_of_the_same_prompt(
    tmp_path: Path,
) -> None:
    invocation = request(timeout_seconds=0, model="provider/model")
    sessions = tmp_path / "state" / "agents" / "planner" / "sessions"
    session_id = "reused-session"
    transcript = sessions / f"{session_id}.jsonl"
    prior_records = [
        {"type": "session", "id": session_id},
        {
            "type": "message",
            "message": {"role": "user", "content": invocation.prompt},
        },
        {
            "type": "message",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "prior"}],
            },
        },
    ]
    program = f"""
import json
import sys
import time
from pathlib import Path

prompt_path = Path(sys.argv[sys.argv.index("--message-file") + 1])
prompt = prompt_path.read_text(encoding="utf-8")
transcript = Path({str(transcript)!r})
lines = transcript.read_text(encoding="utf-8").splitlines()
records = [json.loads(line) for line in lines]
time.sleep(0.08)
records.extend((
    {{"type": "message", "message": {{"role": "user", "content": prompt}}}},
    {{
        "type": "message",
        "message": {{
            "role": "assistant",
            "content": [{{"type": "text", "text": "new"}}],
        }},
    }},
))
payload = "\\n".join(json.dumps(item) for item in records) + "\\n"
transcript.write_text(payload, encoding="utf-8")
print({openclaw_result()!r})
"""
    executor = live_liveness_executor(tmp_path, program)
    sessions.mkdir(parents=True)
    transcript.write_text(
        "\n".join(json.dumps(item) for item in prior_records) + "\n",
        encoding="utf-8",
    )
    (sessions / "sessions.json").write_text(
        json.dumps({invocation.session_key: {"sessionId": session_id}}),
        encoding="utf-8",
    )

    result = executor.execute(invocation)

    assert result.status is AgentExecutionStatus.COMPLETED
    lifecycle = result.telemetry.invocation_lifecycle
    assert lifecycle is not None
    assert lifecycle.initialization.baseline_checkpoint is (
        InitializationCheckpoint.CURRENT_TURN
    )
    assert lifecycle.initialization.baseline_matching_turn_count == 1
    assert lifecycle.initialization.checkpoints == (
        InitializationCheckpoint.PROCESS_LAUNCHED,
        InitializationCheckpoint.CURRENT_TURN,
    )
    assert result.telemetry.provider_liveness is not None
    assert result.telemetry.provider_liveness.lease_start_source == "current_turn"


def test_initialization_no_progress_warns_stops_collects_and_reaps(
    tmp_path: Path,
) -> None:
    executor = live_liveness_executor(
        tmp_path,
        "import time\ntime.sleep(30)\n",
        initialization_policy=InitializationLivenessPolicy(
            no_progress_seconds=0.22,
            stall_grace_seconds=0.08,
            source="test initialization contract",
        ),
        process_grace_seconds=0.10,
    )
    activities = []

    result = executor.execute(
        request(timeout_seconds=0, model="provider/model"),
        activity_handler=activities.append,
    )

    assert result.status is AgentExecutionStatus.INITIALIZATION_STALLED
    lifecycle = result.telemetry.invocation_lifecycle
    assert lifecycle is not None
    assert lifecycle.initialization.stalled
    assert lifecycle.initialization.checkpoints == (
        InitializationCheckpoint.PROCESS_LAUNCHED,
    )
    assert lifecycle.shutdown.reason is InvocationStopReason.INITIALIZATION_STALL
    assert lifecycle.shutdown.terminate_sent
    assert not lifecycle.shutdown.kill_sent
    assert lifecycle.shutdown.cleanup_completed
    lifecycle_kinds = [
        activity.kind
        for activity in activities
        if activity.kind
        in {
            AgentExecutionActivityKind.INITIALIZATION_STALL_SUSPECTED,
            AgentExecutionActivityKind.INITIALIZATION_STALLED,
            AgentExecutionActivityKind.INVOCATION_STOPPING,
            AgentExecutionActivityKind.INVOCATION_COLLECTING_EVIDENCE,
            AgentExecutionActivityKind.INVOCATION_STOPPED,
        }
    ]
    assert lifecycle_kinds == [
        AgentExecutionActivityKind.INITIALIZATION_STALL_SUSPECTED,
        AgentExecutionActivityKind.INITIALIZATION_STALLED,
        AgentExecutionActivityKind.INVOCATION_STOPPING,
        AgentExecutionActivityKind.INVOCATION_COLLECTING_EVIDENCE,
        AgentExecutionActivityKind.INVOCATION_STOPPED,
    ]
    assert executor.interrupt("planner") == 0


def test_initialization_checkpoint_recovers_visible_grace(tmp_path: Path) -> None:
    release_checkpoint = tmp_path / "release-initialization-checkpoint"
    prefix = f"""
import json
import os
import sys
import time
from pathlib import Path

agent_id = sys.argv[sys.argv.index("--agent") + 1]
sessions = Path(os.environ["OPENCLAW_STATE_DIR"]) / "agents" / agent_id / "sessions"
sessions.mkdir(parents=True, exist_ok=True)
while not Path({str(release_checkpoint)!r}).exists():
    time.sleep(0.01)
initial_index = sessions / ".sessions.initial.json.tmp"
initial_index.write_text("{{}}", encoding="utf-8")
os.replace(initial_index, sessions / "sessions.json")
time.sleep(0.04)
"""
    executor = live_liveness_executor(
        tmp_path,
        prefix + FAKE_OPENCLAW_SETUP + "\nappend_raw('ready')\nfinish()\n",
        initialization_policy=InitializationLivenessPolicy(
            no_progress_seconds=3.0,
            stall_grace_seconds=2.0,
            source="test initialization contract",
        ),
    )
    activities: list[AgentExecutionActivity] = []

    def record_activity(activity: AgentExecutionActivity) -> None:
        activities.append(activity)
        if activity.kind is AgentExecutionActivityKind.INITIALIZATION_STALL_SUSPECTED:
            release_checkpoint.write_text("release\n", encoding="utf-8")

    result = executor.execute(
        request(timeout_seconds=0, model="provider/model"),
        activity_handler=record_activity,
    )

    assert result.status is AgentExecutionStatus.COMPLETED
    lifecycle = result.telemetry.invocation_lifecycle
    assert lifecycle is not None
    assert lifecycle.initialization.stall_suspected_count >= 1
    assert (
        lifecycle.initialization.stall_recovered_count
        == lifecycle.initialization.stall_suspected_count
    )
    assert not lifecycle.initialization.stalled
    kinds = [activity.kind for activity in activities]
    suspected = [
        index
        for index, kind in enumerate(kinds)
        if kind is AgentExecutionActivityKind.INITIALIZATION_STALL_SUSPECTED
    ]
    recovered = [
        index
        for index, kind in enumerate(kinds)
        if kind is AgentExecutionActivityKind.INITIALIZATION_STALL_RECOVERED
    ]
    assert all(
        suspected_index < recovered_index
        for suspected_index, recovered_index in zip(
            suspected,
            recovered,
            strict=True,
        )
    )


def test_initialization_observer_failure_stops_with_typed_process_evidence(
    tmp_path: Path,
) -> None:
    executor = live_liveness_executor(
        tmp_path,
        "import time\ntime.sleep(30)\n",
        initialization_policy=InitializationLivenessPolicy(
            no_progress_seconds=0.40,
            stall_grace_seconds=0.10,
            source="test initialization contract",
        ),
        process_grace_seconds=0.10,
    )
    # Malformed observer evidence is this fixture's precondition. Publish it
    # before process launch so the expected failure cannot lose a race with the
    # accelerated initialization-stall guard.
    sessions = tmp_path / "state" / "agents" / "planner" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "sessions.json").write_text("not-json", encoding="utf-8")
    activities = []

    result = executor.execute(
        request(timeout_seconds=0, model="provider/model"),
        activity_handler=activities.append,
    )

    assert result.status is AgentExecutionStatus.PROCESS_FAILED
    lifecycle = result.telemetry.invocation_lifecycle
    assert lifecycle is not None
    assert lifecycle.initialization.mode == "degraded"
    assert lifecycle.initialization.degradation_reason == (
        "OpenClaw initialization baseline could not be attributed"
    )
    assert lifecycle.shutdown.reason is InvocationStopReason.PROCESS_FAILURE
    assert lifecycle.shutdown.cleanup_completed
    assert AgentExecutionActivityKind.INITIALIZATION_LIVENESS_DEGRADED in {
        activity.kind for activity in activities
    }


def test_missing_state_directory_fails_closed_and_reaps_process(tmp_path: Path) -> None:
    binary = tmp_path / "unobservable-openclaw"
    binary.write_text(
        f"#!{sys.executable}\n"
        "import time\n"
        f"print({openclaw_result()!r}, flush=True)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    binary.chmod(0o700)
    executor = OpenClawSubprocessExecutor(
        openclaw_binary=binary,
        environment={},
        process_grace_seconds=0.10,
        liveness_poll_seconds=0.02,
        initialization_policy=InitializationLivenessPolicy(
            no_progress_seconds=0.40,
            stall_grace_seconds=0.10,
            source="test initialization contract",
        ),
    )

    result = executor.execute(request(timeout_seconds=0, model="provider/model"))

    assert result.status is AgentExecutionStatus.PROCESS_FAILED
    lifecycle = result.telemetry.invocation_lifecycle
    assert lifecycle is not None
    assert lifecycle.initialization.mode == "unavailable"
    assert lifecycle.shutdown.reason is InvocationStopReason.PROCESS_FAILURE
    assert lifecycle.shutdown.terminate_sent
    assert lifecycle.shutdown.cleanup_completed


def test_user_deadline_stops_at_exact_remaining_time_and_preserves_reason(
    tmp_path: Path,
) -> None:
    executor = live_liveness_executor(
        tmp_path,
        FAKE_OPENCLAW_SETUP + "\ntime.sleep(30)\n",
        process_grace_seconds=0.10,
        run_deadline_at=datetime.now(UTC) + timedelta(seconds=0.25),
    )

    result = executor.execute(request(timeout_seconds=0, model="provider/model"))

    assert result.status is AgentExecutionStatus.TIMED_OUT
    assert result.telemetry.duration_ms < 900
    lifecycle = result.telemetry.invocation_lifecycle
    assert lifecycle is not None
    assert lifecycle.shutdown.reason is InvocationStopReason.RUN_DEADLINE
    assert lifecycle.shutdown.cleanup_completed


def test_controlled_evaluation_timeout_has_distinct_lifecycle_authority(
    tmp_path: Path,
) -> None:
    executor = live_liveness_executor(
        tmp_path,
        FAKE_OPENCLAW_SETUP
        + "\nfor index in range(300):\n"
        + "    time.sleep(0.05)\n"
        + "    append_raw(f'progress {index}')\n",
        process_grace_seconds=0.10,
    )

    result = executor.execute(request(timeout_seconds=1, model="provider/model"))

    assert result.status is AgentExecutionStatus.TIMED_OUT
    lifecycle = result.telemetry.invocation_lifecycle
    assert lifecycle is not None
    assert lifecycle.shutdown.reason is InvocationStopReason.EVALUATION_TIMEOUT
    assert lifecycle.shutdown.cleanup_completed


def test_cancel_all_uses_user_cancel_reason_and_reaps_active_process(
    tmp_path: Path,
) -> None:
    executor = live_liveness_executor(
        tmp_path,
        "import time\ntime.sleep(30)\n",
        initialization_policy=InitializationLivenessPolicy(
            no_progress_seconds=5,
            stall_grace_seconds=1,
            source="test initialization contract",
        ),
        process_grace_seconds=0.10,
    )
    observed: dict[str, AgentExecutionResult] = {}
    worker = threading.Thread(
        target=lambda: observed.setdefault(
            "result",
            executor.execute(request(timeout_seconds=0, model="provider/model")),
        )
    )
    worker.start()
    interrupted = 0
    for _ in range(100):
        interrupted = executor.interrupt_all()
        if interrupted:
            break
        time.sleep(0.01)
    worker.join(timeout=5)

    assert interrupted == 1
    assert not worker.is_alive()
    result = observed["result"]
    assert result.status is AgentExecutionStatus.INTERRUPTED
    lifecycle = result.telemetry.invocation_lifecycle
    assert lifecycle is not None
    assert lifecycle.shutdown.reason is InvocationStopReason.USER_CANCEL
    assert lifecycle.shutdown.cleanup_completed


def test_default_openclaw_process_can_be_interrupted_by_agent_identity(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "slow-openclaw"
    binary.write_text(
        "#!/usr/bin/env python3\nimport time\ntime.sleep(30)\n",
        encoding="utf-8",
    )
    binary.chmod(0o700)
    executor = OpenClawSubprocessExecutor(
        openclaw_binary=binary,
        process_grace_seconds=1,
    )
    observed: dict[str, object] = {}

    def run() -> None:
        observed["result"] = executor.execute(request())

    worker = threading.Thread(target=run)
    worker.start()
    interrupted = 0
    for _ in range(100):
        interrupted = executor.interrupt("planner")
        if interrupted:
            break
        time.sleep(0.01)
    worker.join(timeout=5)

    assert interrupted == 1
    assert not worker.is_alive()
    result = observed["result"]
    assert result.status is AgentExecutionStatus.INTERRUPTED
    assert result.telemetry.interrupted
    assert result.telemetry.timed_out is False
    lifecycle = result.telemetry.invocation_lifecycle
    assert lifecycle is not None
    assert lifecycle.shutdown.reason is InvocationStopReason.USER_INTERRUPT
    assert lifecycle.shutdown.terminate_sent
    assert lifecycle.shutdown.cleanup_completed
    assert executor.interrupt("planner") == 0


def test_interrupt_escalates_when_the_process_ignores_termination(
    tmp_path: Path,
) -> None:
    ready_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    ready_socket.bind(("127.0.0.1", 0))
    ready_host, ready_port = ready_socket.getsockname()
    ready_socket.listen(1)
    ready_socket.settimeout(15)
    executor = live_liveness_executor(
        tmp_path,
        "import signal\n"
        "import socket\n"
        "import time\n"
        "signal.signal(signal.SIGTERM, lambda *_: None)\n"
        f"ready = socket.create_connection(({ready_host!r}, {ready_port}), 5)\n"
        "ready.sendall(b'ready')\n"
        "ready.close()\n"
        "time.sleep(30)\n",
        process_grace_seconds=1,
    )
    observed: dict[str, object] = {}
    worker = threading.Thread(
        target=lambda: observed.setdefault("result", executor.execute(request()))
    )
    worker.start()
    with ready_socket:
        try:
            connection, _ = ready_socket.accept()
        except TimeoutError:
            result = observed.get("result")
            result_diagnostic = (
                "not_available"
                if result is None
                else (
                    f"status={result.status.value}, error={result.error!r}, "
                    f"exit_code={result.telemetry.exit_code}, "
                    f"stderr_tail={result.telemetry.stderr[-1000:]!r}"
                )
            )
            diagnostic = (
                "fake OpenClaw did not reach its explicit signal-ready checkpoint "
                f"(worker_alive={worker.is_alive()}, "
                f"result={result_diagnostic})"
            )
            for _ in range(100):
                if executor.interrupt("planner") or not worker.is_alive():
                    break
                time.sleep(0.02)
            worker.join(timeout=5)
            pytest.fail(diagnostic + f", cleanup_finished={not worker.is_alive()}")
        with connection:
            assert connection.recv(5) == b"ready"

    assert executor.interrupt("planner") == 1
    assert executor.interrupt("planner") == 0
    worker.join(timeout=5)

    assert not worker.is_alive()
    result = observed["result"]
    assert result.status is AgentExecutionStatus.INTERRUPTED
    lifecycle = result.telemetry.invocation_lifecycle
    assert lifecycle is not None
    assert lifecycle.shutdown.kill_sent
    assert lifecycle.shutdown.signal == 9
    assert lifecycle.shutdown.cleanup_completed


@pytest.mark.parametrize("control", ["agent", "all"])
def test_interrupt_reports_only_newly_accepted_stop_authority(
    tmp_path: Path,
    control: str,
) -> None:
    ready_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    ready_socket.bind(("127.0.0.1", 0))
    ready_host, ready_port = ready_socket.getsockname()
    ready_socket.listen(1)
    ready_socket.settimeout(5)
    binary = tmp_path / "stubborn-openclaw"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import signal\n"
        "import socket\n"
        "import time\n"
        "signal.signal(signal.SIGTERM, lambda *_: None)\n"
        f"ready = socket.create_connection(({ready_host!r}, {ready_port}), 5)\n"
        "ready.sendall(b'ready')\n"
        "ready.close()\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    binary.chmod(0o700)
    executor = OpenClawSubprocessExecutor(
        openclaw_binary=binary,
        process_grace_seconds=0.5,
        liveness_poll_seconds=1.0,
    )
    stopping = threading.Event()
    observed: dict[str, AgentExecutionResult] = {}

    def observe_activity(activity: AgentExecutionActivity) -> None:
        if (
            activity.kind is AgentExecutionActivityKind.INVOCATION_STOPPING
            and activity.stop_reason is InvocationStopReason.PROCESS_FAILURE
        ):
            stopping.set()

    worker = threading.Thread(
        target=lambda: observed.setdefault(
            "result",
            executor.execute(
                request(timeout_seconds=0),
                activity_handler=observe_activity,
            ),
        )
    )
    worker.start()
    with ready_socket:
        connection, _ = ready_socket.accept()
        with connection:
            assert connection.recv(5) == b"ready"

    assert stopping.wait(timeout=5)
    accepted = (
        executor.interrupt("planner")
        if control == "agent"
        else executor.interrupt_all()
    )
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert accepted == 0
    result = observed["result"]
    assert result.status is AgentExecutionStatus.PROCESS_FAILED
    lifecycle = result.telemetry.invocation_lifecycle
    assert lifecycle is not None
    assert lifecycle.shutdown.reason is InvocationStopReason.PROCESS_FAILURE
    assert lifecycle.shutdown.kill_sent
    assert lifecycle.shutdown.cleanup_completed


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


def test_dynamic_agent_identity_drives_command_session_and_telemetry() -> None:
    dynamic = AgentExecutionRequest(
        run_id="task-manager-001",
        team_id="adaptive_team",
        iteration=2,
        agent_id="cli_developer",
        capability=AgentCapability.IMPLEMENTATION,
        expected_kind=ArtifactKind.WORK_RESULT,
        prompt="Implement the approved CLI tasks.",
        timeout_seconds=90,
        model="provider/model",
    )
    observed: dict[str, tuple[str, ...]] = {}

    def runner(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        observed["command"] = tuple(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=openclaw_result('{"summary":"done"}'),
            stderr="",
        )

    result = executor_with_clocks(runner).execute(dynamic)

    command = observed["command"]
    assert command[command.index("--agent") + 1] == "cli_developer"
    assert dynamic.role is None
    assert dynamic.session_key == (
        "agent:cli_developer:sat-task-manager-001-i2-work-result"
    )
    assert result.telemetry.agent_id == "cli_developer"
    assert result.telemetry.capability is AgentCapability.IMPLEMENTATION
    assert result.telemetry.role is None


def test_dynamic_agent_request_rejects_a_capability_output_mismatch() -> None:
    with pytest.raises(ValidationError, match="cannot produce test_report"):
        AgentExecutionRequest(
            run_id="task-manager-001",
            team_id="adaptive_team",
            iteration=1,
            agent_id="cli_developer",
            capability=AgentCapability.IMPLEMENTATION,
            expected_kind=ArtifactKind.TEST_REPORT,
            prompt="Return tests.",
            timeout_seconds=90,
        )


def test_generic_session_key_rejects_no_identity_aliasing() -> None:
    assert (
        stable_agent_session_key(
            run_id="task-manager-001",
            agent_id="quality_reviewer",
            iteration=1,
            expected_kind=ArtifactKind.REVIEW_REPORT,
        )
        == "agent:quality_reviewer:sat-task-manager-001-i1-review-report"
    )


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
