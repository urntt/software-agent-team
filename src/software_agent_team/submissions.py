"""Controller-bound semantic artifact submission for OpenClaw Agent turns."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    model_validator,
)

ARTIFACT_SUBMISSION_PLUGIN_ID = "sat-artifact-submission"
ARTIFACT_SUBMISSION_TOOL = "sat_submit_artifact"
ARTIFACT_SUBMISSION_PROTOCOL = "sat_artifact_submission_v1"
MAX_SUBMISSION_SCHEMA_BYTES = 512 * 1024
MAX_SUBMISSION_FILE_BYTES = 2 * 1024 * 1024


class AgentSubmissionPurpose(StrEnum):
    """The semantic contract active for one model invocation."""

    ARTIFACT = "artifact"
    SEMANTIC_CORRECTION = "semantic_correction"


class AgentSubmissionStatus(StrEnum):
    """Controller conclusion for one requested typed submission."""

    ACCEPTED = "accepted"
    MISSING = "missing"
    INVALID = "invalid"
    DUPLICATE = "duplicate"
    UNAUTHORIZED = "unauthorized"


def _reject_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def canonical_json_bytes(value: object) -> bytes:
    """Encode one JSON value with the same identity used for tool evidence."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_json_sha256(value: object) -> str:
    """Return the canonical JSON digest for a schema or semantic payload."""

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


class AgentSubmissionContract(BaseModel):
    """Immutable tool schema selected by the controller for one invocation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol: Literal[ARTIFACT_SUBMISSION_PROTOCOL] = ARTIFACT_SUBMISSION_PROTOCOL
    purpose: AgentSubmissionPurpose
    tool_name: Literal[ARTIFACT_SUBMISSION_TOOL] = ARTIFACT_SUBMISSION_TOOL
    parameters_schema_json: str = Field(
        min_length=2,
        max_length=MAX_SUBMISSION_SCHEMA_BYTES,
    )
    schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def from_schema(
        cls,
        schema: dict[str, JsonValue],
        *,
        purpose: AgentSubmissionPurpose,
    ) -> Self:
        """Freeze a generated JSON Schema and its exact content identity."""

        encoded = canonical_json_bytes(schema)
        return cls(
            purpose=purpose,
            parameters_schema_json=encoded.decode("utf-8"),
            schema_sha256=hashlib.sha256(encoded).hexdigest(),
        )

    @model_validator(mode="after")
    def validate_schema_identity(self) -> Self:
        try:
            schema = json.loads(
                self.parameters_schema_json,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ValueError(f"non-standard JSON constant: {value}")
                ),
            )
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("submission schema must be strict JSON") from error
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise ValueError("submission schema must describe one JSON object")
        canonical = canonical_json_bytes(schema)
        if canonical.decode("utf-8") != self.parameters_schema_json:
            raise ValueError("submission schema JSON must use canonical encoding")
        if hashlib.sha256(canonical).hexdigest() != self.schema_sha256:
            raise ValueError("submission schema digest does not match its content")
        return self

    def parameters_schema(self) -> dict[str, JsonValue]:
        """Decode the already-validated schema for display or file materialization."""

        value = json.loads(self.parameters_schema_json)
        assert isinstance(value, dict)
        return value


class AgentSubmissionEvidence(BaseModel):
    """Content-free provenance and diagnosis for one typed submission attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol: Literal[ARTIFACT_SUBMISSION_PROTOCOL] = ARTIFACT_SUBMISSION_PROTOCOL
    purpose: AgentSubmissionPurpose
    status: AgentSubmissionStatus
    tool_name: Literal[ARTIFACT_SUBMISSION_TOOL] = ARTIFACT_SUBMISSION_TOOL
    schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    tool_call_id: str | None = Field(default=None, pattern=r"^tool-[0-9]{3}$")
    payload_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    diagnostic_code: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]{0,99}$",
    )
    diagnostic_detail: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def bind_status_to_evidence(self) -> Self:
        accepted = self.status is AgentSubmissionStatus.ACCEPTED
        if accepted:
            if self.tool_call_id is None or self.payload_sha256 is None:
                raise ValueError(
                    "accepted submission requires call and payload evidence"
                )
            if self.diagnostic_code is not None or self.diagnostic_detail is not None:
                raise ValueError("accepted submission cannot contain a diagnostic")
        elif self.diagnostic_code is None or self.diagnostic_detail is None:
            raise ValueError("rejected submission requires a typed diagnostic")
        return self


class AgentSemanticSubmission(BaseModel):
    """Validated transport payload retained only until artifact assembly."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    payload: dict[str, JsonValue]
    evidence: AgentSubmissionEvidence

    @model_validator(mode="after")
    def validate_payload_binding(self) -> Self:
        if self.evidence.status is not AgentSubmissionStatus.ACCEPTED:
            raise ValueError("semantic payload requires accepted submission evidence")
        if canonical_json_sha256(self.payload) != self.evidence.payload_sha256:
            raise ValueError("semantic payload differs from submission evidence")
        return self


@dataclass(frozen=True)
class SubmissionFileCapture:
    """Bounded private-file read retained after the invocation temp dir is removed."""

    content: bytes | None
    diagnostic_code: str | None = None
    diagnostic_detail: str | None = None


class SubmissionToolEvidence(Protocol):
    """Structural subset of persisted OpenClaw tool evidence used for binding."""

    id: str
    tool_name: str
    external_call_sha256: str
    arguments_sha256: str
    outcome: object
    is_error: bool


def artifact_submission_plugin_path() -> Path:
    """Return the immutable package-owned OpenClaw submission plugin directory."""

    candidate = Path(__file__).with_name("openclaw_plugins") / "artifact_submission"
    try:
        metadata = candidate.lstat()
    except OSError as error:
        raise RuntimeError("artifact submission plugin is unavailable") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError("artifact submission plugin path is not a direct directory")
    for name in ("index.js", "openclaw.plugin.json", "package.json"):
        path = candidate / name
        try:
            entry = path.lstat()
        except OSError as error:
            raise RuntimeError(
                f"artifact submission plugin is missing {name}"
            ) from error
        if stat.S_ISLNK(entry.st_mode) or not stat.S_ISREG(entry.st_mode):
            raise RuntimeError(f"artifact submission plugin {name} is unsafe")
    return candidate.resolve(strict=True)


def capture_submission_file(path: Path) -> SubmissionFileCapture:
    """Read a plugin output without following links or accepting unsafe ownership."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return SubmissionFileCapture(content=None)
    except OSError:
        return SubmissionFileCapture(
            content=None,
            diagnostic_code="unsafe_submission_file",
            diagnostic_detail="the submission file could not be opened safely",
        )
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
        ):
            return SubmissionFileCapture(
                content=None,
                diagnostic_code="unsafe_submission_file",
                diagnostic_detail=(
                    "the submission file has unsafe type, mode, owner, or link count"
                ),
            )
        if metadata.st_size > MAX_SUBMISSION_FILE_BYTES:
            return SubmissionFileCapture(
                content=None,
                diagnostic_code="oversized_submission_file",
                diagnostic_detail="the submission file exceeds its bounded size",
            )
        chunks: list[bytes] = []
        remaining = MAX_SUBMISSION_FILE_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        if len(content) > MAX_SUBMISSION_FILE_BYTES:
            return SubmissionFileCapture(
                content=None,
                diagnostic_code="oversized_submission_file",
                diagnostic_detail="the submission file exceeds its bounded size",
            )
        return SubmissionFileCapture(content=content)
    finally:
        os.close(descriptor)


def rejected_submission_evidence(
    contract: AgentSubmissionContract,
    *,
    binding_sha256: str,
    status: AgentSubmissionStatus,
    code: str,
    detail: str,
    tool_call_id: str | None = None,
    payload_sha256: str | None = None,
) -> AgentSubmissionEvidence:
    """Build one bounded failure result without retaining semantic content."""

    if status is AgentSubmissionStatus.ACCEPTED:
        raise ValueError("rejected submission helper cannot accept a payload")
    return AgentSubmissionEvidence(
        purpose=contract.purpose,
        status=status,
        schema_sha256=contract.schema_sha256,
        binding_sha256=binding_sha256,
        tool_call_id=tool_call_id,
        payload_sha256=payload_sha256,
        diagnostic_code=code,
        diagnostic_detail=" ".join(detail.split())[:500],
    )


def validate_submission_capture(
    contract: AgentSubmissionContract,
    *,
    binding_sha256: str,
    capture: SubmissionFileCapture,
    tool_calls: tuple[SubmissionToolEvidence, ...],
    tool_evidence_error: str | None,
) -> tuple[AgentSemanticSubmission | None, AgentSubmissionEvidence]:
    """Bind exactly one plugin file to exactly one captured successful tool call."""

    if tool_evidence_error is not None:
        evidence = rejected_submission_evidence(
            contract,
            binding_sha256=binding_sha256,
            status=AgentSubmissionStatus.UNAUTHORIZED,
            code="tool_evidence_unavailable",
            detail="submission cannot be attributed because tool evidence is invalid",
        )
        return None, evidence

    matching = tuple(
        call for call in tool_calls if call.tool_name == contract.tool_name
    )
    if not matching:
        status = (
            AgentSubmissionStatus.MISSING
            if capture.content is None and capture.diagnostic_code is None
            else AgentSubmissionStatus.UNAUTHORIZED
        )
        evidence = rejected_submission_evidence(
            contract,
            binding_sha256=binding_sha256,
            status=status,
            code=(
                "submission_missing"
                if status is AgentSubmissionStatus.MISSING
                else "unattributed_submission_file"
            ),
            detail=(
                "the Agent completed without calling the required submission tool"
                if status is AgentSubmissionStatus.MISSING
                else "a submission file exists without an attributable tool call"
            ),
        )
        return None, evidence
    if len(matching) != 1:
        evidence = rejected_submission_evidence(
            contract,
            binding_sha256=binding_sha256,
            status=AgentSubmissionStatus.DUPLICATE,
            code="duplicate_submission_calls",
            detail="the Agent called the submission tool more than once",
        )
        return None, evidence

    call = matching[0]
    if tool_calls[-1] is not call:
        evidence = rejected_submission_evidence(
            contract,
            binding_sha256=binding_sha256,
            status=AgentSubmissionStatus.UNAUTHORIZED,
            code="submission_not_final",
            detail="the semantic submission was not the final tool call",
            tool_call_id=call.id,
        )
        return None, evidence
    outcome = getattr(call.outcome, "value", call.outcome)
    if outcome != "succeeded" or call.is_error:
        evidence = rejected_submission_evidence(
            contract,
            binding_sha256=binding_sha256,
            status=AgentSubmissionStatus.INVALID,
            code="submission_tool_failed",
            detail="the required submission tool call did not succeed",
            tool_call_id=call.id,
        )
        return None, evidence
    if capture.diagnostic_code is not None:
        evidence = rejected_submission_evidence(
            contract,
            binding_sha256=binding_sha256,
            status=AgentSubmissionStatus.INVALID,
            code=capture.diagnostic_code,
            detail=capture.diagnostic_detail or "the submission file is invalid",
            tool_call_id=call.id,
        )
        return None, evidence
    if capture.content is None:
        evidence = rejected_submission_evidence(
            contract,
            binding_sha256=binding_sha256,
            status=AgentSubmissionStatus.INVALID,
            code="submission_file_missing",
            detail="a successful submission tool call did not create its private file",
            tool_call_id=call.id,
        )
        return None, evidence

    try:
        decoded = capture.content.decode("utf-8", errors="strict")
        envelope = json.loads(
            decoded,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-standard JSON constant: {value}")
            ),
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError, RecursionError):
        evidence = rejected_submission_evidence(
            contract,
            binding_sha256=binding_sha256,
            status=AgentSubmissionStatus.INVALID,
            code="invalid_submission_file",
            detail="the submission file is not strict UTF-8 JSON",
            tool_call_id=call.id,
        )
        return None, evidence
    required_keys = {
        "protocol",
        "binding_sha256",
        "schema_sha256",
        "tool_call_id",
        "payload",
    }
    if not isinstance(envelope, dict) or set(envelope) != required_keys:
        evidence = rejected_submission_evidence(
            contract,
            binding_sha256=binding_sha256,
            status=AgentSubmissionStatus.INVALID,
            code="invalid_submission_envelope",
            detail="the submission envelope fields do not match the protocol",
            tool_call_id=call.id,
        )
        return None, evidence
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        evidence = rejected_submission_evidence(
            contract,
            binding_sha256=binding_sha256,
            status=AgentSubmissionStatus.INVALID,
            code="non_object_submission",
            detail="the semantic submission payload must be a JSON object",
            tool_call_id=call.id,
        )
        return None, evidence
    payload_sha256 = canonical_json_sha256(payload)
    expected_external_sha256 = hashlib.sha256(
        str(envelope.get("tool_call_id", "")).encode("utf-8")
    ).hexdigest()
    authorized = (
        envelope.get("protocol") == contract.protocol
        and envelope.get("binding_sha256") == binding_sha256
        and envelope.get("schema_sha256") == contract.schema_sha256
        and expected_external_sha256 == call.external_call_sha256
        and payload_sha256 == call.arguments_sha256
    )
    if not authorized:
        evidence = rejected_submission_evidence(
            contract,
            binding_sha256=binding_sha256,
            status=AgentSubmissionStatus.UNAUTHORIZED,
            code="submission_binding_mismatch",
            detail=(
                "submission protocol, invocation, schema, call, or arguments binding "
                "does not match controller evidence"
            ),
            tool_call_id=call.id,
            payload_sha256=payload_sha256,
        )
        return None, evidence

    evidence = AgentSubmissionEvidence(
        purpose=contract.purpose,
        status=AgentSubmissionStatus.ACCEPTED,
        schema_sha256=contract.schema_sha256,
        binding_sha256=binding_sha256,
        tool_call_id=call.id,
        payload_sha256=payload_sha256,
    )
    return AgentSemanticSubmission(payload=payload, evidence=evidence), evidence
