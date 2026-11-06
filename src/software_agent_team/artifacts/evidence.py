"""Controller-recorded execution telemetry and tool call evidence models.

This module provides data models for tracking Agent tool calls, submission receipts,
runtime rejections, session transcripts, and complete Agent invocation execution
records.
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from typing import Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

from software_agent_team.invocation_lifecycle import (
    InvocationLifecycleEvidence,
    InvocationStopReason,
)
from software_agent_team.response_corrections import (
    ResponseValidationDiagnostic,
    SemanticCorrectionOutcome,
    SemanticCorrectionRequestEvidence,
)
from software_agent_team.submissions import (
    ARTIFACT_SUBMISSION_PROTOCOL,
    ARTIFACT_SUBMISSION_PROTOCOL_V1,
    ARTIFACT_SUBMISSION_TOOL,
    AgentSubmissionEvidence,
    AgentSubmissionStatus,
)

from .envelope import ArtifactReference
from .types import (
    AGENT_ID_PATTERN,
    ARTIFACT_SCHEMA_VERSION,
    AgentExecutionStatus,
    AgentRuntimeFailureCode,
    AgentToolCallOutcome,
    AgentToolEvidenceStatus,
    ArtifactKind,
    ProviderLivenessEvidence,
    _require_safe_relative_path,
    _require_utc,
)


class AgentSubmissionReceiptEvidence(BaseModel):
    """Bound plugin receipt captured from one successful tool result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol: Literal[ARTIFACT_SUBMISSION_PROTOCOL]
    submission_status: Literal["accepted"]
    schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    external_call_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class AgentToolCallEvidence(BaseModel):
    """Sanitized controller record of one OpenClaw tool call and result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^tool-[0-9]{3}$")
    tool_name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    executable: str | None = Field(default=None, min_length=1, max_length=512)
    external_call_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    arguments_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    outcome: AgentToolCallOutcome
    is_error: bool
    reported_status: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_-]{0,63}$",
    )
    exit_code: int | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    output_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_bytes: int = Field(ge=0, le=1_048_576)
    output_excerpt: str = Field(default="", max_length=4096)
    submission_receipt: AgentSubmissionReceiptEvidence | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @field_validator("executable")
    @classmethod
    def require_clean_executable(cls, value: str | None) -> str | None:
        """Keep the direct executable inspectable without accepting whitespace."""
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned or cleaned != value or "\x00" in cleaned:
            raise ValueError("tool evidence executable must be clean text")
        return cleaned

    @field_validator("output_excerpt")
    @classmethod
    def reject_nul_excerpt(cls, value: str) -> str:
        """Keep persisted output excerpts text-safe."""
        if "\x00" in value:
            raise ValueError("tool output excerpts must not contain NUL")
        return value

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        """Keep the normalized outcome consistent with concrete result fields."""
        reported_failure = self.reported_status in {
            "cancelled",
            "error",
            "failed",
            "timed_out",
            "timeout",
        }
        reported_deferred = self.reported_status == "running"
        if self.outcome is AgentToolCallOutcome.SUCCEEDED and (
            self.is_error
            or self.exit_code not in {None, 0}
            or reported_failure
            or reported_deferred
        ):
            raise ValueError(
                "successful tool evidence requires a terminal non-error result"
            )
        if self.outcome is AgentToolCallOutcome.FAILED and (
            reported_deferred
            or (
                not self.is_error
                and self.exit_code in {None, 0}
                and not reported_failure
            )
        ):
            raise ValueError("failed tool evidence requires an observable failure")
        if self.outcome is AgentToolCallOutcome.DEFERRED and (
            self.is_error
            or self.exit_code is not None
            or not reported_deferred
            or self.tool_name not in {"exec", "process"}
        ):
            raise ValueError(
                "deferred tool evidence requires a nonterminal async process result"
            )
        if (self.tool_name == "exec") != (self.executable is not None):
            raise ValueError("only exec tool evidence requires an executable")
        if self.submission_receipt is not None and (
            self.tool_name != ARTIFACT_SUBMISSION_TOOL
            or self.outcome is not AgentToolCallOutcome.SUCCEEDED
            or self.is_error
            or self.submission_receipt.external_call_sha256 != self.external_call_sha256
        ):
            raise ValueError(
                "submission receipt requires its successful attributed tool call"
            )
        return self


class RuntimeToolRejection(BaseModel):
    """Pinned-runtime negative diagnostic, never evidence of tool execution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: Literal["unknown_tool"] = "unknown_tool"
    record_index: int = Field(ge=1, le=4096)
    tool_name: str = Field(pattern=r"^[A-Za-z0-9_:.-]{1,64}$")
    external_call_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    record_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReviewToolEvidenceReference(BaseModel):
    """Reviewer citation bound to one controller-recorded tool result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    execution_attempt: int = Field(default=1, ge=1)
    tool_call_id: str = Field(pattern=r"^tool-[0-9]{3}$")
    observable: str = Field(min_length=1, max_length=256)

    @model_serializer(mode="wrap")
    def preserve_legacy_attempt_omission(
        self,
        handler: SerializerFunctionWrapHandler,
    ) -> object:
        """Keep existing schema-v2 artifacts byte-stable when loaded again."""
        serialized = handler(self)
        if (
            isinstance(serialized, dict)
            and "execution_attempt" not in self.model_fields_set
        ):
            serialized.pop("execution_attempt", None)
        return serialized

    @field_validator("observable")
    @classmethod
    def require_clean_observable(cls, value: str) -> str:
        """Require a small exact result fragment that the controller can match."""
        cleaned = value.strip()
        if not cleaned or "\x00" in cleaned:
            raise ValueError("tool evidence observables must be nonblank text")
        return cleaned


def validate_tool_evidence_collection(
    *,
    status: AgentToolEvidenceStatus,
    transcript_sha256: str | None,
    record_count: int | None,
    tool_calls: tuple[AgentToolCallEvidence, ...],
    error: str | None,
    runtime_rejections: tuple[RuntimeToolRejection, ...] = (),
) -> None:
    """Validate one complete, absent, or rejected tool-evidence collection."""
    identifiers = [item.id for item in tool_calls]
    expected = [f"tool-{index:03d}" for index in range(1, len(tool_calls) + 1)]
    if identifiers != expected:
        raise ValueError("tool evidence IDs must be contiguous and ordered")
    if runtime_rejections:
        if status is not AgentToolEvidenceStatus.CAPTURED:
            raise ValueError("runtime rejections require captured session provenance")
        identities = [item.external_call_sha256 for item in runtime_rejections]
        positions = [item.record_index for item in runtime_rejections]
        if (
            len(set(identities)) != len(identities)
            or positions != sorted(set(positions))
            or record_count is None
            or any(position >= record_count for position in positions)
            or set(identities).intersection(
                call.external_call_sha256 for call in tool_calls
            )
        ):
            raise ValueError(
                "runtime rejection identities must be distinct and ordered"
            )
    if status is AgentToolEvidenceStatus.NOT_CAPTURED:
        if transcript_sha256 is not None or record_count is not None or tool_calls:
            raise ValueError("uncaptured tool evidence cannot contain session records")
        if error is not None:
            raise ValueError("uncaptured tool evidence cannot contain an error")
    elif status is AgentToolEvidenceStatus.CAPTURED:
        if transcript_sha256 is None or record_count is None:
            raise ValueError("captured tool evidence requires transcript provenance")
        if error is not None:
            raise ValueError("captured tool evidence cannot contain an error")
    elif status is AgentToolEvidenceStatus.INVALID:
        if error is None:
            raise ValueError("invalid tool evidence requires an error")
        if tool_calls:
            raise ValueError("invalid tool evidence cannot expose partial tool calls")


class RuntimeRejectionEvidence(BaseModel):
    """Session-bound negative diagnostics for consumers without tool claims."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    transcript_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    record_count: int = Field(ge=1, le=4096)
    rejections: tuple[RuntimeToolRejection, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def require_bound_diagnostics(self) -> Self:
        validate_tool_evidence_collection(
            status=AgentToolEvidenceStatus.CAPTURED,
            transcript_sha256=self.transcript_sha256,
            record_count=self.record_count,
            tool_calls=(),
            error=None,
            runtime_rejections=self.rejections,
        )
        return self


class AgentExecutionRecord(BaseModel):
    """Controller-recorded telemetry for one Agent invocation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[
        2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, ARTIFACT_SCHEMA_VERSION
    ] = ARTIFACT_SCHEMA_VERSION
    kind: Literal[ArtifactKind.AGENT_EXECUTION_RECORD] = (
        ArtifactKind.AGENT_EXECUTION_RECORD
    )
    run_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    team_id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    iteration: int = Field(ge=1)
    stage: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    attempt: int = Field(default=1, ge=1)
    agent_id: str = Field(pattern=AGENT_ID_PATTERN)
    capability: str = Field(pattern=AGENT_ID_PATTERN)
    specialization: str | None = Field(
        default=None,
        pattern=AGENT_ID_PATTERN,
        exclude_if=lambda value: value is None,
    )
    execution_status: AgentExecutionStatus | None = None
    runtime_failure_code: AgentRuntimeFailureCode | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    session_key: str = Field(min_length=1)
    session_id: str | None = Field(default=None, min_length=1)
    model: str | None = Field(default=None, min_length=1)
    provider: str | None = Field(default=None, min_length=1)
    started_at: datetime
    finished_at: datetime
    duration_ms: int = Field(ge=0)
    exit_code: int | None = None
    timed_out: bool = False
    provider_liveness: ProviderLivenessEvidence | None = None
    invocation_lifecycle: InvocationLifecycleEvidence | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    estimated_cost_usd: Decimal | None = Field(default=None, ge=0)
    stdout_path: str = Field(min_length=1)
    stderr_path: str = Field(min_length=1)
    stdout_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    stderr_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    response_contract: (
        Literal[
            "semantic_body_v1",
            "semantic_body_v2",
            "semantic_body_v3",
            "semantic_body_v4",
        ]
        | None
    ) = None
    response_transport: (
        Literal[
            "typed_submission_v1",
            "typed_submission_v2",
        ]
        | None
    ) = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    controller_supplied_fields: tuple[str, ...] = ()
    ignored_controller_fields: tuple[str, ...] = ()
    response_normalizations: tuple[str, ...] = Field(
        default=(),
        exclude_if=lambda values: not values,
    )
    response_validation: ResponseValidationDiagnostic | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    semantic_correction_request: SemanticCorrectionRequestEvidence | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    semantic_correction_outcome: SemanticCorrectionOutcome | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    submission_evidence: AgentSubmissionEvidence | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    tool_evidence_status: AgentToolEvidenceStatus = AgentToolEvidenceStatus.NOT_CAPTURED
    session_transcript_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    session_record_count: int | None = Field(default=None, ge=1, le=4096)
    tool_calls: tuple[AgentToolCallEvidence, ...] = ()
    runtime_rejections: tuple[RuntimeToolRejection, ...] = Field(
        default=(),
        exclude_if=lambda value: not value,
    )
    tool_evidence_error: str | None = Field(default=None, min_length=1, max_length=2000)
    # Zero records that product execution did not impose a per-Agent wall-clock
    # limit. Positive values remain exact controlled-evaluation evidence.
    stage_timeout_seconds: int | None = Field(default=None, ge=0)
    remaining_timeout_seconds: int | None = Field(default=None, ge=0)
    response_artifact: ArtifactReference | None = None
    error: str | None = Field(default=None, min_length=1)

    @field_validator("started_at", "finished_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        """Normalize execution timestamps to UTC."""
        return _require_utc(value)

    @field_validator("stdout_path", "stderr_path")
    @classmethod
    def require_safe_output_path(cls, value: str) -> str:
        """Keep captured Agent output within the run directory."""
        return _require_safe_relative_path(value)

    @field_validator(
        "session_key",
        "session_id",
        "model",
        "provider",
        "specialization",
        "tool_evidence_error",
        "error",
    )
    @classmethod
    def reject_blank_optional_text(cls, value: str | None) -> str | None:
        """Reject whitespace-only runtime identifiers and errors."""
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("execution text fields must not be blank")
        return cleaned

    @field_validator("controller_supplied_fields", "ignored_controller_fields")
    @classmethod
    def require_unique_field_names(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Keep response-binding provenance compact and unambiguous."""
        if len(values) != len(set(values)) or any(
            re.fullmatch(r"[a-z][a-z0-9_]*", value) is None for value in values
        ):
            raise ValueError("response binding fields must be valid and unique")
        return values

    @field_validator("response_normalizations")
    @classmethod
    def require_unique_response_normalizations(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        if (
            len(values) > 100
            or len(values) != len(set(values))
            or any(not value.strip() or len(value) > 500 for value in values)
        ):
            raise ValueError("response normalizations must be bounded and unique")
        return values

    @model_validator(mode="after")
    def validate_execution(self) -> Self:
        """Keep timing, exit, timeout, and response evidence coherent."""
        recovered_terminal_submission = (
            self.execution_status is AgentExecutionStatus.COMPLETED
            and self.invocation_lifecycle is not None
            and self.invocation_lifecycle.shutdown.reason
            in {
                InvocationStopReason.RESPONSE_FINALIZATION_STALL,
                InvocationStopReason.PROCESS_FAILURE,
            }
            and (
                self.invocation_lifecycle.shutdown.reason
                is not InvocationStopReason.PROCESS_FAILURE
                or self.exit_code not in {None, 0}
            )
        )
        if self.schema_version >= 13 and self.specialization is None:
            raise ValueError("current execution records require specialization")
        if self.runtime_failure_code is not None:
            expected_status = (
                AgentExecutionStatus.PROVIDER_FAILED
                if self.runtime_failure_code
                is AgentRuntimeFailureCode.OPENCLAW_MODEL_STREAM_TERMINATED
                else AgentExecutionStatus.PROCESS_FAILED
            )
            if self.execution_status is not expected_status or self.exit_code in {
                None,
                0,
            }:
                raise ValueError(
                    "model stream termination requires a nonzero provider failure"
                    if expected_status is AgentExecutionStatus.PROVIDER_FAILED
                    else "runtime failure codes require a nonzero process failure"
                )
        if self.schema_version < 15 and self.runtime_failure_code is not None:
            raise ValueError("runtime failure codes require artifact schema 15")
        if self.schema_version < 14 and any(
            call.submission_receipt is not None for call in self.tool_calls
        ):
            raise ValueError("submission receipts require artifact schema 14")
        if self.finished_at < self.started_at:
            raise ValueError("execution finish time cannot precede start time")
        if (self.stage_timeout_seconds is None) != (
            self.remaining_timeout_seconds is None
        ):
            raise ValueError(
                "stage and remaining timeout evidence must appear together"
            )
        if (
            self.stage_timeout_seconds is not None
            and self.remaining_timeout_seconds is not None
            and self.remaining_timeout_seconds > self.stage_timeout_seconds
        ):
            raise ValueError(
                "effective invocation timeout cannot exceed its configured limit"
            )
        if not set(self.ignored_controller_fields).issubset(
            self.controller_supplied_fields
        ):
            raise ValueError("ignored response fields must be controller-owned")
        if self.response_contract is None and (
            self.controller_supplied_fields
            or self.ignored_controller_fields
            or self.response_normalizations
            or self.response_validation is not None
            or self.semantic_correction_request is not None
            or self.submission_evidence is not None
            or self.response_transport is not None
        ):
            raise ValueError("response binding fields require a response contract")
        if self.submission_evidence is not None and self.response_transport not in {
            "typed_submission_v1",
            "typed_submission_v2",
        }:
            raise ValueError(
                "submission evidence requires the typed submission transport"
            )
        if (
            self.response_transport in {"typed_submission_v1", "typed_submission_v2"}
            and self.execution_status is AgentExecutionStatus.COMPLETED
            and self.submission_evidence is None
        ):
            raise ValueError(
                "completed typed submission transport requires submission evidence"
            )
        if (
            self.submission_evidence is not None
            and self.submission_evidence.status is AgentSubmissionStatus.ACCEPTED
        ):
            expected_transport = (
                "typed_submission_v2"
                if self.submission_evidence.protocol == ARTIFACT_SUBMISSION_PROTOCOL
                else "typed_submission_v1"
            )
            if self.submission_evidence.protocol not in {
                ARTIFACT_SUBMISSION_PROTOCOL_V1,
                ARTIFACT_SUBMISSION_PROTOCOL,
            }:
                raise ValueError("submission evidence uses an unknown protocol")
            matching_submission_calls = tuple(
                call
                for call in self.tool_calls
                if call.id == self.submission_evidence.tool_call_id
                and call.tool_name == self.submission_evidence.tool_name
            )
            receipt = (
                None
                if len(matching_submission_calls) != 1
                else matching_submission_calls[0].submission_receipt
            )
            receipt_matches = (
                receipt is not None
                and receipt.protocol == self.submission_evidence.protocol
                and receipt.schema_sha256 == self.submission_evidence.schema_sha256
                and receipt.binding_sha256 == self.submission_evidence.binding_sha256
                and receipt.external_call_sha256
                == matching_submission_calls[0].external_call_sha256
            )
            recorded_arguments_match = (
                len(matching_submission_calls) == 1
                and matching_submission_calls[0].arguments_sha256
                == self.submission_evidence.payload_sha256
            )
            receipt_is_consistent = receipt is None or receipt_matches
            if (
                self.response_transport != expected_transport
                or self.tool_evidence_status is not AgentToolEvidenceStatus.CAPTURED
                or len(matching_submission_calls) != 1
                or self.tool_calls[-1] is not matching_submission_calls[0]
                or matching_submission_calls[0].outcome
                is not AgentToolCallOutcome.SUCCEEDED
                or matching_submission_calls[0].is_error
                or not receipt_is_consistent
                or not (recorded_arguments_match or receipt_matches)
            ):
                raise ValueError(
                    "accepted submission evidence must bind the final successful "
                    "captured tool call"
                )
        if (self.semantic_correction_request is None) != (
            self.semantic_correction_outcome is None
        ):
            raise ValueError(
                "semantic correction request and outcome must appear together"
            )
        validate_tool_evidence_collection(
            status=self.tool_evidence_status,
            transcript_sha256=self.session_transcript_sha256,
            record_count=self.session_record_count,
            tool_calls=self.tool_calls,
            runtime_rejections=self.runtime_rejections,
            error=self.tool_evidence_error,
        )
        if self.timed_out:
            if self.exit_code is not None and self.exit_code > 0:
                shutdown = (
                    None
                    if self.invocation_lifecycle is None
                    else self.invocation_lifecycle.shutdown
                )
                if not (
                    shutdown is not None
                    and shutdown.reason
                    in {
                        InvocationStopReason.RUN_DEADLINE,
                        InvocationStopReason.EVALUATION_TIMEOUT,
                    }
                    and shutdown.terminate_sent
                    and shutdown.exit_code == self.exit_code
                ):
                    raise ValueError(
                        "timed-out executions require no exit, a zero wrapper "
                        "exit, a terminating signal, or an owned timeout "
                        "lifecycle matching the wrapper exit"
                    )
            if self.error is None:
                raise ValueError("timed-out executions must record an error")
            if self.response_artifact is not None:
                raise ValueError(
                    "timed-out executions cannot report a response artifact"
                )
        else:
            if self.exit_code is None and self.error is None:
                raise ValueError("executions without an exit code require an error")
            if (
                self.exit_code != 0
                and self.error is None
                and not recovered_terminal_submission
            ):
                raise ValueError("failed executions must record an error")
            if (
                self.exit_code == 0
                and self.error is None
                and self.response_artifact is None
            ):
                raise ValueError("successful executions require a response artifact")
            if self.exit_code == 0 and self.error is None and self.model is None:
                raise ValueError("successful executions require model metadata")
            if recovered_terminal_submission and self.model is None:
                raise ValueError("recovered finalization requires model metadata")
            if (
                recovered_terminal_submission
                and self.error is None
                and self.response_artifact is None
            ):
                raise ValueError("recovered finalization requires a response artifact")
        if self.execution_status is AgentExecutionStatus.TIMED_OUT:
            if not self.timed_out:
                raise ValueError("typed timeout status requires timeout evidence")
        elif self.execution_status is not None and self.timed_out:
            raise ValueError("timeout evidence requires the typed timeout status")
        if self.execution_status is AgentExecutionStatus.PROVIDER_STALLED:
            if self.provider_liveness is None or not self.provider_liveness.stalled:
                raise ValueError(
                    "provider-stalled status requires terminal liveness evidence"
                )
        elif self.provider_liveness is not None and self.provider_liveness.stalled:
            raise ValueError(
                "terminal liveness evidence requires provider-stalled status"
            )
        if self.invocation_lifecycle is not None:
            if self.schema_version < 5:
                raise ValueError(
                    "legacy execution records cannot contain invocation lifecycle"
                )
            if self.execution_status is None:
                raise ValueError(
                    "invocation lifecycle requires a typed execution status"
                )
            expected_lifecycle_schema = (
                1
                if self.schema_version == 5
                else (
                    2
                    if self.schema_version < 9
                    else (
                        3
                        if self.schema_version < 11
                        else (4 if self.schema_version < 12 else 5)
                    )
                )
            )
            if self.invocation_lifecycle.schema_version != expected_lifecycle_schema:
                raise ValueError(
                    "execution-record schema does not match invocation-lifecycle schema"
                )
            allowed_reasons = {
                AgentExecutionStatus.COMPLETED: {
                    InvocationStopReason.COMPLETED,
                    InvocationStopReason.RESPONSE_FINALIZATION_STALL,
                    InvocationStopReason.PROCESS_FAILURE,
                },
                AgentExecutionStatus.PROCESS_FAILED: {
                    InvocationStopReason.PROCESS_FAILURE
                },
                AgentExecutionStatus.PROVIDER_FAILED: {
                    InvocationStopReason.PROVIDER_FAILURE
                },
                AgentExecutionStatus.PROVIDER_STALLED: {
                    InvocationStopReason.PROVIDER_STALL
                },
                AgentExecutionStatus.INITIALIZATION_STALLED: {
                    InvocationStopReason.INITIALIZATION_STALL
                },
                AgentExecutionStatus.RESPONSE_FINALIZATION_STALLED: {
                    InvocationStopReason.RESPONSE_FINALIZATION_STALL
                },
                AgentExecutionStatus.TIMED_OUT: {
                    InvocationStopReason.RUN_DEADLINE,
                    InvocationStopReason.EVALUATION_TIMEOUT,
                },
                AgentExecutionStatus.UPSTREAM_INCOMPLETE: {
                    InvocationStopReason.UPSTREAM_INCOMPLETE
                },
                AgentExecutionStatus.INVALID_RESPONSE: {
                    InvocationStopReason.INVALID_RESPONSE
                },
                AgentExecutionStatus.LAUNCH_FAILED: {
                    InvocationStopReason.LAUNCH_FAILURE
                },
                AgentExecutionStatus.INTERRUPTED: {
                    InvocationStopReason.USER_INTERRUPT,
                    InvocationStopReason.USER_CANCEL,
                },
            }[self.execution_status]
            if self.invocation_lifecycle.shutdown.reason not in allowed_reasons:
                raise ValueError(
                    "invocation lifecycle reason does not match execution status"
                )
            if recovered_terminal_submission and (
                self.invocation_lifecycle.response_finalization is None
                or (
                    self.invocation_lifecycle.shutdown.reason
                    is InvocationStopReason.RESPONSE_FINALIZATION_STALL
                    and not self.invocation_lifecycle.response_finalization.stalled
                )
                or self.provider_liveness is None
                or not self.provider_liveness.terminal_response_observed
                or self.submission_evidence is None
                or self.submission_evidence.status is not AgentSubmissionStatus.ACCEPTED
            ):
                raise ValueError(
                    "recovered terminal completion requires terminal state and "
                    "accepted submission evidence"
                )
        if self.schema_version < 6 and (
            self.execution_status is AgentExecutionStatus.RESPONSE_FINALIZATION_STALLED
            or (
                self.provider_liveness is not None
                and self.provider_liveness.terminal_response_observed
            )
        ):
            raise ValueError(
                "legacy execution records cannot contain response-finalization state"
            )
        if (
            self.schema_version < 7
            and self.execution_status is AgentExecutionStatus.UPSTREAM_INCOMPLETE
        ):
            raise ValueError(
                "legacy execution records cannot contain upstream-incomplete state"
            )
        if self.schema_version < 8 and any(
            call.outcome is AgentToolCallOutcome.DEFERRED for call in self.tool_calls
        ):
            raise ValueError(
                "legacy execution records cannot contain deferred tool evidence"
            )
        if self.schema_version < 10 and "runtime_rejections" in self.model_fields_set:
            raise ValueError(
                "legacy execution records cannot contain runtime rejections"
            )
        return self
