"""Exercise warning provenance through the adapter and real submission plugin.

Only the external OpenClaw process/session IO is replaced. The plugin writes
the bound capture and receipt; the production adapter validates both.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from pinned_runtime import pinned_openclaw_node

from software_agent_team.artifacts import AgentRole, ArtifactKind
from software_agent_team.execution import (
    AgentExecutionRequest,
    AgentExecutionStatus,
    OpenClawSubprocessExecutor,
)
from software_agent_team.submissions import (
    AgentSubmissionContract,
    AgentSubmissionPurpose,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "case",
    [
        "process",
        "read",
        "aborted",
        "provider_error",
        "unknown_payload",
        "extra_error",
        "wrong_tool",
        "wrong_count",
        "wrong_failures",
        "failed_trace",
        "missing_receipt",
        "wrong_binding",
        "after_submission",
    ],
)
def test_historical_tool_warning_requires_native_and_session_provenance(
    tmp_path: Path, case: str
) -> None:
    node = pinned_openclaw_node(ROOT)
    state = tmp_path / "state"
    contract = AgentSubmissionContract.from_schema(
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {"status": {"type": "string"}},
            "required": ["status"],
        },
        purpose=AgentSubmissionPurpose.ARTIFACT,
    )
    request = AgentExecutionRequest(
        run_id="warning-provenance",
        team_id="function_specialized",
        iteration=1,
        role=AgentRole.PLANNER,
        expected_kind=ArtifactKind.IMPLEMENTATION_PLAN,
        prompt="Submit the checked result.",
        timeout_seconds=0,
        model="deepseek/deepseek-flash",
        submission_contract=contract,
    )

    def runner(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        env = kwargs["env"]
        assert isinstance(env, dict)
        observed = subprocess.run(
            [
                str(node),
                str(ROOT / "tests/fixtures/submission_bridge.mjs"),
                str(
                    ROOT
                    / "src/software_agent_team/openclaw_plugins"
                    / "artifact_submission/index.js"
                ),
            ],
            input=json.dumps([{"artifact": {"status": "ok"}}]),
            text=True,
            capture_output=True,
            check=True,
            timeout=30,
            env=env,
        )
        plugin = json.loads(observed.stdout)
        name = "read" if case == "read" else "process"
        sid = "warning-session"
        aid = command[command.index("--agent") + 1]
        key = command[command.index("--session-key") + 1]
        prompt = Path(command[command.index("--message-file") + 1]).read_text()
        records = [
            {"type": "session", "id": sid},
            {"type": "message", "message": {"role": "user", "content": prompt}},
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "toolCall",
                            "id": "historical-failure",
                            "name": name,
                            "arguments": {
                                "action": "poll",
                                "sessionId": "failed-child",
                            },
                        }
                    ],
                },
            },
            {
                "type": "message",
                "message": {
                    "role": "toolResult",
                    "toolCallId": "historical-failure",
                    "toolName": name,
                    "isError": True,
                    "content": [{"type": "text", "text": "child timed out"}],
                    "details": {"status": "failed"},
                },
            },
            *plugin["records"],
        ]
        if case == "missing_receipt":
            records.pop()
        elif case == "wrong_binding":
            records[-1]["message"]["details"]["binding_sha256"] = "a" * 64
        elif case == "after_submission":
            records.append(
                {
                    "type": "message",
                    "message": {
                        "role": "assistant",
                        "stopReason": "error",
                        "content": [{"type": "text", "text": "provider terminated"}],
                    },
                }
            )
        sessions = state / "agents" / aid / "sessions"
        sessions.mkdir(parents=True)
        (sessions / f"{sid}.jsonl").write_text(
            "".join(json.dumps(record) + "\n" for record in records)
        )
        (sessions / "sessions.json").write_text(json.dumps({key: {"sessionId": sid}}))
        warning = "⚠️ Read failed" if case == "read" else "⚠️ Process failed (timed out)."
        response = {
            "payloads": [{"text": warning, "isError": True}],
            "meta": {
                "agentMeta": {
                    "sessionId": sid,
                    "provider": "deepseek",
                    "model": "deepseek-flash",
                    "usage": {"input": 101, "output": 23},
                },
                "aborted": False,
                "stopReason": "toolUse",
                "completion": {"stopReason": "toolUse", "finishReason": "toolUse"},
                "toolSummary": {
                    "calls": 2,
                    "failures": 1,
                    "unresolvedError": {"toolName": name},
                },
                "executionTrace": {
                    "fallbackUsed": False,
                    "attempts": [{"result": "success", "stage": "assistant"}],
                },
            },
        }
        if case == "aborted":
            response["meta"]["aborted"] = True
        elif case == "provider_error":
            response["meta"]["error"] = {"message": "provider terminated"}
        elif case == "unknown_payload":
            response["payloads"][0]["text"] = "Unknown runtime failure"
        elif case == "extra_error":
            response["payloads"].append(
                {"text": "provider terminated", "isError": True}
            )
        elif case == "wrong_tool":
            response["meta"]["toolSummary"]["unresolvedError"]["toolName"] = "read"
        elif case == "wrong_count":
            response["meta"]["toolSummary"]["calls"] = 3
        elif case == "wrong_failures":
            response["meta"]["toolSummary"]["failures"] = 2
        elif case == "failed_trace":
            response["meta"]["executionTrace"]["attempts"][0]["result"] = "error"
        return subprocess.CompletedProcess(command, 0, json.dumps(response), "")

    result = OpenClawSubprocessExecutor(
        openclaw_binary="external-fixture",
        runner=runner,
        environment={"OPENCLAW_STATE_DIR": str(state)},
    ).execute(request)
    success = case in {"process", "read"}
    assert result.status is (
        AgentExecutionStatus.COMPLETED
        if success
        else AgentExecutionStatus.INVALID_RESPONSE
    )
    assert result.telemetry.usage.input_tokens == 101
    assert result.telemetry.usage.output_tokens == 23
    if case == "missing_receipt":
        assert result.telemetry.tool_evidence_error is not None
    else:
        assert result.telemetry.tool_calls[0].is_error is True
    if success:
        assert result.semantic_submission.payload == {"status": "ok"}
        assert result.telemetry.tool_calls[-1].submission_receipt is not None
