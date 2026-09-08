"""Tests for invocation-bound typed Agent artifact submission."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from software_agent_team.artifacts import AgentToolCallEvidence
from software_agent_team.submissions import (
    ARTIFACT_SUBMISSION_ARGUMENT,
    ARTIFACT_SUBMISSION_PROTOCOL,
    AgentSubmissionContract,
    AgentSubmissionPurpose,
    AgentSubmissionStatus,
    SubmissionFileCapture,
    canonical_json_bytes,
    canonical_json_sha256,
    capture_submission_file,
    validate_submission_capture,
)

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
}
BINDING = "b" * 64


def contract() -> AgentSubmissionContract:
    """Return one controller-frozen schema contract."""

    return AgentSubmissionContract.from_schema(
        SCHEMA,
        purpose=AgentSubmissionPurpose.ARTIFACT,
    )


def tool_call(
    payload: dict[str, object],
    *,
    normalized_id: str = "tool-001",
    external_id: str = "provider-call-1",
    tool_name: str = "sat_submit_artifact",
    outcome: str = "succeeded",
    is_error: bool = False,
) -> AgentToolCallEvidence:
    """Return attributable controller evidence for one submission call."""

    output = b"Semantic artifact accepted"
    return AgentToolCallEvidence(
        id=normalized_id,
        tool_name=tool_name,
        external_call_sha256=hashlib.sha256(external_id.encode()).hexdigest(),
        arguments_sha256=canonical_json_sha256({ARTIFACT_SUBMISSION_ARGUMENT: payload}),
        outcome=outcome,
        is_error=is_error,
        output_sha256=hashlib.sha256(output).hexdigest(),
        output_bytes=len(output),
        output_excerpt=output.decode(),
    )


def capture(
    payload: dict[str, object],
    *,
    external_id: str = "provider-call-1",
    binding: str = BINDING,
    schema_sha256: str | None = None,
) -> SubmissionFileCapture:
    """Return the private-file envelope written by the plugin."""

    envelope = {
        "protocol": ARTIFACT_SUBMISSION_PROTOCOL,
        "binding_sha256": binding,
        "schema_sha256": schema_sha256 or contract().schema_sha256,
        "tool_call_id": external_id,
        "payload": payload,
    }
    return SubmissionFileCapture(
        content=json.dumps(envelope, separators=(",", ":")).encode()
    )


def test_submission_contract_freezes_canonical_schema_identity() -> None:
    value = contract()

    assert value.parameters_schema_json == canonical_json_bytes(SCHEMA).decode()
    assert value.parameters_schema() == SCHEMA
    assert value.schema_sha256 == canonical_json_sha256(SCHEMA)

    with pytest.raises(ValidationError, match="canonical encoding"):
        AgentSubmissionContract(
            purpose=AgentSubmissionPurpose.ARTIFACT,
            parameters_schema_json=json.dumps(SCHEMA, indent=2),
            schema_sha256=value.schema_sha256,
        )


def test_submission_contract_separates_transport_from_semantic_schema() -> None:
    transport_schema = {"type": "object", "additionalProperties": True}

    value = AgentSubmissionContract.from_schema(
        SCHEMA,
        purpose=AgentSubmissionPurpose.PLANNING_RESPONSE,
        transport_schema=transport_schema,
    )

    assert value.parameters_schema() == SCHEMA
    assert value.schema_sha256 == canonical_json_sha256(SCHEMA)
    assert value.transport_payload_schema() == transport_schema
    expected_tool_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"artifact": transport_schema},
        "required": ["artifact"],
    }
    assert value.transport_schema() == expected_tool_schema
    assert value.transport_schema_sha256 == canonical_json_sha256(expected_tool_schema)


@pytest.mark.parametrize(
    "purpose",
    [
        AgentSubmissionPurpose.PLANNING_RESPONSE,
        AgentSubmissionPurpose.SEMANTIC_CORRECTION,
    ],
)
def test_controller_validated_submission_captures_once_before_semantic_validation(
    purpose: AgentSubmissionPurpose,
) -> None:
    """A value error must reach the Controller, not an upstream tool retry loop."""

    value = AgentSubmissionContract.from_schema(SCHEMA, purpose=purpose)
    assert value.parameters_schema() == SCHEMA
    assert value.transport_payload_schema() == {
        "type": "object",
        "additionalProperties": True,
    }
    invalid = {"summary": [], "unexpected": "preserve for Controller diagnosis"}
    submission, evidence = validate_submission_capture(
        value,
        binding_sha256=BINDING,
        capture=capture(invalid, schema_sha256=value.schema_sha256),
        tool_calls=(tool_call(invalid),),
        tool_evidence_error=None,
    )
    assert submission is not None and submission.payload == invalid
    assert evidence.status is AgentSubmissionStatus.ACCEPTED
    # Capture acceptance is transport provenance, not semantic or work acceptance.
    assert contract().transport_payload_schema() == SCHEMA


def test_submission_transport_envelope_lifts_semantic_definitions() -> None:
    semantic_schema = {
        "$defs": {
            "Summary": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            }
        },
        "type": "object",
        "properties": {"summary": {"$ref": "#/$defs/Summary"}},
        "required": ["summary"],
    }

    value = AgentSubmissionContract.from_schema(
        semantic_schema,
        purpose=AgentSubmissionPurpose.ARTIFACT,
    )

    tool_schema = value.transport_schema()
    assert tool_schema["$defs"] == semantic_schema["$defs"]
    artifact_schema = tool_schema["properties"]["artifact"]
    assert "$defs" not in artifact_schema
    assert artifact_schema["properties"]["summary"] == {"$ref": "#/$defs/Summary"}


def test_submission_capture_accepts_one_final_bound_call() -> None:
    payload = {"summary": "complete"}

    submission, evidence = validate_submission_capture(
        contract(),
        binding_sha256=BINDING,
        capture=capture(payload),
        tool_calls=(tool_call(payload),),
        tool_evidence_error=None,
    )

    assert submission is not None
    assert submission.payload == payload
    assert evidence.status is AgentSubmissionStatus.ACCEPTED
    assert evidence.tool_call_id == "tool-001"
    assert evidence.payload_sha256 == canonical_json_sha256({"artifact": payload})
    assert evidence.semantic_payload_sha256 == canonical_json_sha256(payload)


def test_submission_capture_accepts_one_final_success_after_schema_rejection() -> None:
    """A failed transport attempt has no semantic authority or file binding."""

    invalid_payload = {"summary": 42}
    payload = {"summary": "complete"}
    failed = tool_call(
        invalid_payload,
        external_id="provider-call-1",
        outcome="failed",
        is_error=True,
    )
    succeeded = tool_call(
        payload,
        normalized_id="tool-002",
        external_id="provider-call-2",
    )

    submission, evidence = validate_submission_capture(
        contract(),
        binding_sha256=BINDING,
        capture=capture(payload, external_id="provider-call-2"),
        tool_calls=(failed, succeeded),
        tool_evidence_error=None,
    )

    assert submission is not None
    assert submission.payload == payload
    assert evidence.status is AgentSubmissionStatus.ACCEPTED
    assert evidence.tool_call_id == "tool-002"


def test_submission_capture_rejects_success_followed_by_failed_retry() -> None:
    """A successful semantic submission must remain the terminal tool action."""

    payload = {"summary": "complete"}
    later_failure = tool_call(
        {"summary": 42},
        normalized_id="tool-002",
        external_id="provider-call-2",
        outcome="failed",
        is_error=True,
    )

    submission, evidence = validate_submission_capture(
        contract(),
        binding_sha256=BINDING,
        capture=capture(payload),
        tool_calls=(tool_call(payload), later_failure),
        tool_evidence_error=None,
    )

    assert submission is None
    assert evidence.status is AgentSubmissionStatus.UNAUTHORIZED
    assert evidence.diagnostic_code == "submission_not_final"


def test_submission_capture_rejects_unbound_direct_or_double_envelope_arguments() -> (
    None
):
    payload = {"summary": "complete"}
    direct_call = tool_call(payload).model_copy(
        update={"arguments_sha256": canonical_json_sha256(payload)}
    )
    double_call = tool_call(payload).model_copy(
        update={
            "arguments_sha256": canonical_json_sha256(
                {"artifact": {"artifact": payload}}
            )
        }
    )

    for call in (direct_call, double_call):
        submission, evidence = validate_submission_capture(
            contract(),
            binding_sha256=BINDING,
            capture=capture(payload),
            tool_calls=(call,),
            tool_evidence_error=None,
        )
        assert submission is None
        assert evidence.status is AgentSubmissionStatus.UNAUTHORIZED
        assert evidence.diagnostic_code == "submission_binding_mismatch"


@pytest.mark.parametrize(
    ("file_capture", "calls", "expected_status", "expected_code"),
    [
        (
            SubmissionFileCapture(content=None),
            (),
            AgentSubmissionStatus.MISSING,
            "submission_missing",
        ),
        (
            capture({"summary": "complete"}),
            (),
            AgentSubmissionStatus.UNAUTHORIZED,
            "unattributed_submission_file",
        ),
        (
            capture({"summary": "complete"}),
            (
                tool_call({"summary": "complete"}),
                tool_call(
                    {"summary": "complete"},
                    normalized_id="tool-002",
                    external_id="provider-call-2",
                ),
            ),
            AgentSubmissionStatus.DUPLICATE,
            "duplicate_submission_calls",
        ),
    ],
)
def test_submission_capture_rejects_missing_unattributed_or_duplicate_calls(
    file_capture: SubmissionFileCapture,
    calls: tuple[AgentToolCallEvidence, ...],
    expected_status: AgentSubmissionStatus,
    expected_code: str,
) -> None:
    submission, evidence = validate_submission_capture(
        contract(),
        binding_sha256=BINDING,
        capture=file_capture,
        tool_calls=calls,
        tool_evidence_error=None,
    )

    assert submission is None
    assert evidence.status is expected_status
    assert evidence.diagnostic_code == expected_code


def test_submission_capture_requires_submission_to_be_final() -> None:
    payload = {"summary": "complete"}
    read_call = tool_call(
        {"path": "/agent"},
        normalized_id="tool-002",
        external_id="read-call",
        tool_name="read",
    )

    submission, evidence = validate_submission_capture(
        contract(),
        binding_sha256=BINDING,
        capture=capture(payload),
        tool_calls=(tool_call(payload), read_call),
        tool_evidence_error=None,
    )

    assert submission is None
    assert evidence.status is AgentSubmissionStatus.UNAUTHORIZED
    assert evidence.diagnostic_code == "submission_not_final"


@pytest.mark.parametrize(
    ("file_capture", "call", "tool_error", "expected_status", "expected_code"),
    [
        (
            SubmissionFileCapture(content=b"not-json"),
            tool_call({"summary": "complete"}),
            None,
            AgentSubmissionStatus.INVALID,
            "invalid_submission_file",
        ),
        (
            SubmissionFileCapture(content=None),
            tool_call({"summary": "complete"}),
            None,
            AgentSubmissionStatus.INVALID,
            "submission_file_missing",
        ),
        (
            capture({"summary": "complete"}),
            tool_call({"summary": "complete"}, outcome="failed", is_error=True),
            None,
            AgentSubmissionStatus.INVALID,
            "submission_tool_failed",
        ),
        (
            capture({"summary": "complete"}),
            tool_call({"summary": "complete"}),
            "session transcript identity mismatch",
            AgentSubmissionStatus.UNAUTHORIZED,
            "tool_evidence_unavailable",
        ),
    ],
)
def test_submission_capture_rejects_invalid_or_unverifiable_evidence(
    file_capture: SubmissionFileCapture,
    call: AgentToolCallEvidence,
    tool_error: str | None,
    expected_status: AgentSubmissionStatus,
    expected_code: str,
) -> None:
    submission, evidence = validate_submission_capture(
        contract(),
        binding_sha256=BINDING,
        capture=file_capture,
        tool_calls=(call,),
        tool_evidence_error=tool_error,
    )

    assert submission is None
    assert evidence.status is expected_status
    assert evidence.diagnostic_code == expected_code


@pytest.mark.parametrize(
    "file_capture",
    [
        capture({"summary": "complete"}, binding="c" * 64),
        capture({"summary": "complete"}, schema_sha256="d" * 64),
        capture({"summary": "different"}),
    ],
)
def test_submission_capture_rejects_binding_or_argument_mismatch(
    file_capture: SubmissionFileCapture,
) -> None:
    payload = {"summary": "complete"}

    submission, evidence = validate_submission_capture(
        contract(),
        binding_sha256=BINDING,
        capture=file_capture,
        tool_calls=(tool_call(payload),),
        tool_evidence_error=None,
    )

    assert submission is None
    assert evidence.status is AgentSubmissionStatus.UNAUTHORIZED
    assert evidence.diagnostic_code == "submission_binding_mismatch"


def test_capture_submission_file_requires_private_direct_regular_file(
    tmp_path: Path,
) -> None:
    private_file = tmp_path / "submission.json"
    private_file.write_bytes(b"{}")
    private_file.chmod(0o600)

    assert capture_submission_file(private_file).content == b"{}"

    private_file.chmod(0o644)
    unsafe = capture_submission_file(private_file)
    assert unsafe.content is None
    assert unsafe.diagnostic_code == "unsafe_submission_file"

    link = tmp_path / "submission-link.json"
    link.symlink_to(private_file)
    linked = capture_submission_file(link)
    assert linked.content is None
    assert linked.diagnostic_code == "unsafe_submission_file"
