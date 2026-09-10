"""Production JS plugin -> session parser -> bound capture -> semantic correction.

The host's session IO is simulated; this is not full OpenClaw/provider acceptance.
No accepted submission, tool digest, or correction result is fabricated.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from software_agent_team.openclaw_session_evidence import capture_openclaw_tool_evidence
from software_agent_team.response_corrections import (
    SemanticCorrectionSubmissionError,
    apply_semantic_correction,
    build_semantic_correction_plan,
    diagnostic_from_validation_error,
    semantic_correction_schema,
    semantic_correction_slot_handle,
)
from software_agent_team.submissions import (
    AgentSubmissionContract,
    AgentSubmissionPurpose,
    AgentSubmissionStatus,
    canonical_json_bytes,
    capture_submission_file,
    validate_submission_capture,
)

ROOT = Path(__file__).resolve().parents[1]


def capture_controller_correction(tmp_path: Path, request, payload):
    """Capture a real terminal plugin result; prior work remains caller-owned."""
    pins = (ROOT / "configs/toolchain.sh").read_text()
    match = re.search(r'^task_node_version="([^"]+)"$', pins, re.MULTILINE)
    assert match is not None
    node = ROOT / ".sat/openclaw/tools" / f"node-v{match[1]}/bin/node"
    contract = request.submission_contract
    assert contract is not None
    tmp_path.mkdir()
    schema = tmp_path / "parameters.json"
    schema.write_bytes(canonical_json_bytes(contract.transport_schema()))
    output = tmp_path / "submission.json"
    binding = "b" * 64
    completed = subprocess.run(
        [
            str(node),
            str(ROOT / "tests/fixtures/submission_bridge.mjs"),
            str(
                ROOT
                / "src/software_agent_team/openclaw_plugins"
                / "artifact_submission/index.js"
            ),
        ],
        input=json.dumps([{"artifact": payload}]),
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
        env={
            "HOME": str(tmp_path),
            "SAT_ARTIFACT_SUBMISSION_SCHEMA_PATH": str(schema),
            "SAT_ARTIFACT_SUBMISSION_OUTPUT_PATH": str(output),
            "SAT_ARTIFACT_SUBMISSION_SCHEMA_SHA256": contract.schema_sha256,
            "SAT_ARTIFACT_SUBMISSION_PARAMETERS_SHA256": (
                contract.transport_schema_sha256
            ),
            "SAT_ARTIFACT_SUBMISSION_BINDING_SHA256": binding,
        },
    )
    observed = json.loads(completed.stdout)
    session_id = "controller-bridge"
    sessions = tmp_path / "state/agents" / request.agent_id / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "sessions.json").write_text(
        json.dumps({request.session_key: {"sessionId": session_id}})
    )
    records = [
        {"type": "session", "id": session_id},
        {"type": "message", "message": {"role": "user", "content": request.prompt}},
        *observed["records"],
    ]
    (sessions / f"{session_id}.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records)
    )
    evidence = capture_openclaw_tool_evidence(
        state_dir=tmp_path / "state",
        agent_id=request.agent_id,
        session_key=request.session_key,
        session_id=session_id,
        prompt=request.prompt,
    )
    captured, status = validate_submission_capture(
        contract,
        binding_sha256=binding,
        capture=capture_submission_file(output),
        tool_calls=evidence.tool_calls,
        tool_evidence_error=None,
    )
    assert captured is not None
    assert status.status is AgentSubmissionStatus.ACCEPTED
    return captured, status, evidence


class CorrectedBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    audience: str = Field(min_length=1)
    workflow: str = Field(min_length=1)
    preserved: str


@pytest.mark.parametrize(
    "case",
    [
        "valid",
        "invalid_value",
        "duplicate_slot",
        "double_envelope",
        "rejected_then_valid",
        "action_after_success",
        "wrong_binding",
        "runtime_rejection_then_valid",
        "deferred_work_then_valid",
    ],
)
def test_production_submission_correction_bridge(tmp_path: Path, case: str) -> None:
    pins = (ROOT / "configs/toolchain.sh").read_text()
    match = re.search(r'^task_node_version="([^"]+)"$', pins, re.MULTILINE)
    assert match is not None
    node = ROOT / ".sat/openclaw/tools" / f"node-v{match[1]}/bin/node"
    assert node.is_file(), "run make setup to install the pinned Node runtime"
    base = {"audience": "", "workflow": "", "preserved": "unchanged"}
    with pytest.raises(ValidationError) as invalid:
        CorrectedBody.model_validate(base)
    diagnostic = diagnostic_from_validation_error(invalid.value, base)
    plan = build_semantic_correction_plan(base, diagnostic)
    assert plan is not None
    replacements = [
        {
            "slot_handle": semantic_correction_slot_handle(
                plan.evidence.target_paths, path
            ),
            "replacement_value": value,
        }
        for path, value in (("/workflow", "Scan notes"), ("/audience", "Local user"))
    ]
    if case == "invalid_value":
        replacements[0]["replacement_value"] = []
    if case == "duplicate_slot":
        replacements[1]["slot_handle"] = replacements[0]["slot_handle"]
    payload = {"replacements": replacements}
    arguments = {"artifact": payload}
    if case == "double_envelope":
        arguments = {"artifact": arguments}
    calls = [arguments]
    if case == "rejected_then_valid":
        calls.insert(0, payload)
    if case == "action_after_success":
        calls.append(arguments)

    contract = AgentSubmissionContract.from_schema(
        semantic_correction_schema(
            plan, response_schema=CorrectedBody.model_json_schema()
        ),
        purpose=AgentSubmissionPurpose.SEMANTIC_CORRECTION,
    )
    schema = tmp_path / "parameters.json"
    schema.write_bytes(canonical_json_bytes(contract.transport_schema()))
    output = tmp_path / "submission.json"
    binding = "b" * 64
    completed = subprocess.run(
        [
            str(node),
            str(ROOT / "tests/fixtures/submission_bridge.mjs"),
            str(
                ROOT
                / "src/software_agent_team/openclaw_plugins"
                / "artifact_submission/index.js"
            ),
        ],
        input=json.dumps(calls),
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
        env={
            "HOME": str(tmp_path),
            "SAT_ARTIFACT_SUBMISSION_SCHEMA_PATH": str(schema),
            "SAT_ARTIFACT_SUBMISSION_OUTPUT_PATH": str(output),
            "SAT_ARTIFACT_SUBMISSION_SCHEMA_SHA256": contract.schema_sha256,
            "SAT_ARTIFACT_SUBMISSION_PARAMETERS_SHA256": (
                contract.transport_schema_sha256
            ),
            "SAT_ARTIFACT_SUBMISSION_BINDING_SHA256": binding,
        },
    )
    observed = json.loads(completed.stdout)
    assert observed["parameters"] == contract.transport_schema()
    session_id, session_key, prompt = (
        "bridge-session",
        "bridge-key",
        "Submit correction",
    )
    sessions = tmp_path / "state/agents/planner/sessions"
    sessions.mkdir(parents=True)
    (sessions / "sessions.json").write_text(
        json.dumps({session_key: {"sessionId": session_id}})
    )
    prior_records = []
    if case == "runtime_rejection_then_valid":
        prior_records.append(
            {
                "type": "message",
                "message": {
                    "role": "toolResult",
                    "toolCallId": "unavailable-call",
                    "toolName": "missing_tool",
                    "isError": True,
                    "content": [
                        {"type": "text", "text": "Tool missing_tool not found"}
                    ],
                    "details": {},
                },
            }
        )
    if case == "deferred_work_then_valid":
        # Captured host shapes: async start is not successful work evidence.
        for call_id, name, arguments, details in (
            (
                "work-start",
                "exec",
                {"command": "pytest"},
                {
                    "status": "running",
                    "sessionId": "test-job",
                    "pid": 123,
                    "startedAt": 1788749665172,
                    "cwd": "/workspace",
                    "tail": "",
                },
            ),
            (
                "work-poll",
                "process",
                {"action": "poll", "sessionId": "test-job"},
                {
                    "status": "completed",
                    "sessionId": "test-job",
                    "exitCode": 0,
                    "exitReason": "exit",
                    "aggregated": "tests passed",
                    "name": "pytest",
                },
            ),
        ):
            prior_records.extend(
                [
                    {
                        "type": "message",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "toolCall",
                                    "id": call_id,
                                    "name": name,
                                    "arguments": arguments,
                                }
                            ],
                        },
                    },
                    {
                        "type": "message",
                        "message": {
                            "role": "toolResult",
                            "toolCallId": call_id,
                            "toolName": name,
                            "isError": False,
                            "content": [
                                {"type": "text", "text": "fixture work observation"}
                            ],
                            "details": details,
                        },
                    },
                ]
            )
    records = [
        {"type": "session", "id": session_id},
        {"type": "message", "message": {"role": "user", "content": prompt}},
        *prior_records,
        *observed["records"],
    ]
    (sessions / f"{session_id}.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records)
    )
    evidence = capture_openclaw_tool_evidence(
        state_dir=tmp_path / "state",
        agent_id="planner",
        session_key=session_key,
        session_id=session_id,
        prompt=prompt,
    )
    if case == "runtime_rejection_then_valid":
        assert len(evidence.runtime_rejections) == 1
        assert len(evidence.tool_calls) == 1
    if case == "deferred_work_then_valid":
        assert [call.outcome.value for call in evidence.tool_calls] == [
            "deferred",
            "succeeded",
            "succeeded",
        ]
    captured, status = validate_submission_capture(
        contract,
        binding_sha256="c" * 64 if case == "wrong_binding" else binding,
        capture=capture_submission_file(output),
        tool_calls=evidence.tool_calls,
        tool_evidence_error=None,
    )
    if case in {"action_after_success", "wrong_binding"}:
        assert captured is None
        assert status.status is not AgentSubmissionStatus.ACCEPTED
        return
    assert captured is not None
    assert status.status is AgentSubmissionStatus.ACCEPTED
    if case in {"duplicate_slot", "double_envelope"}:
        with pytest.raises(SemanticCorrectionSubmissionError):
            apply_semantic_correction(captured.payload, plan)
    else:
        corrected = apply_semantic_correction(captured.payload, plan)
        if case == "invalid_value":
            with pytest.raises(ValidationError):
                CorrectedBody.model_validate(corrected)
        else:
            assert CorrectedBody.model_validate(corrected).model_dump() == {
                "audience": "Local user",
                "workflow": "Scan notes",
                "preserved": "unchanged",
            }
    assert base == {"audience": "", "workflow": "", "preserved": "unchanged"}
