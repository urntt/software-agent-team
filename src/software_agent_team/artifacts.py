"""Validated contracts for requests, phase artifacts, and Agent handoffs."""

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import PurePosixPath
from types import MappingProxyType
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

ARTIFACT_SCHEMA_VERSION = 2
COMMIT_PATTERN = r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"
AGENT_ID_PATTERN = r"^[a-z][a-z0-9_]*$"


class AgentRole(StrEnum):
    """Agent responsibilities available to versioned team configurations."""

    CLARIFIER = "clarifier"
    SINGLE_AGENT = "single_agent"
    PLANNER = "planner"
    GENERALIST_DEVELOPER = "generalist_developer"
    FRONTEND_DEVELOPER = "frontend_developer"
    BACKEND_DEVELOPER = "backend_developer"
    INTEGRATOR = "integrator"
    TESTER = "tester"
    REVIEWER = "reviewer"


class HandoffStatus(StrEnum):
    """Terminal status for one Agent execution."""

    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"


class AgentExecutionStatus(StrEnum):
    """Observable terminal state of one Agent adapter invocation."""

    COMPLETED = "completed"
    PROCESS_FAILED = "process_failed"
    PROVIDER_FAILED = "provider_failed"
    PROVIDER_STALLED = "provider_stalled"
    TIMED_OUT = "timed_out"
    INVALID_RESPONSE = "invalid_response"
    LAUNCH_FAILED = "launch_failed"
    INTERRUPTED = "interrupted"


class ProviderLivenessEvidence(BaseModel):
    """Persistable, content-free evidence from a renewable activity lease."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["enforced", "degraded"]
    policy_source: str = Field(min_length=1, max_length=300)
    silence_seconds: float = Field(gt=0)
    stall_grace_seconds: float = Field(gt=0)
    lease_started: bool = False
    lease_start_source: Literal["current_turn", "provider_stream"] | None = None
    raw_stream_observed: bool = False
    session_observed: bool = False
    provider_activity_observations: int = Field(ge=0)
    tool_started_count: int = Field(ge=0)
    tool_completed_count: int = Field(ge=0)
    stall_suspected_count: int = Field(ge=0)
    stall_recovered_count: int = Field(ge=0)
    maximum_inactivity_ms: int = Field(default=0, ge=0)
    stalled: bool = False
    degradation_reason: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def bind_mode_and_reason(self) -> Self:
        if (self.mode == "degraded") != (self.degradation_reason is not None):
            raise ValueError("degraded liveness requires exactly one reason")
        if self.stalled and self.mode != "enforced":
            raise ValueError("degraded liveness cannot declare a provider stall")
        if self.lease_started != (self.lease_start_source is not None):
            raise ValueError("started liveness requires exactly one start source")
        if self.stalled and not self.lease_started:
            raise ValueError("provider stall requires a started liveness lease")
        return self


class ArtifactKind(StrEnum):
    """Persisted artifact types used by the workflow."""

    CLARIFICATION_RECORD = "clarification_record"
    TASK_BRIEF = "task_brief"
    IMPLEMENTATION_PLAN = "implementation_plan"
    WORK_RESULT = "work_result"
    TEST_REPORT = "test_report"
    REVIEW_REPORT = "review_report"
    ITERATION_RECORD = "iteration_record"
    FINAL_REPORT = "final_report"
    HANDOFF_ENVELOPE = "handoff_envelope"
    AGENT_EXECUTION_RECORD = "agent_execution_record"


class CheckStatus(StrEnum):
    """Outcome of a deterministic check or acceptance criterion."""

    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"
    PENDING_REVIEW = "pending_review"


class ReviewSeverity(StrEnum):
    """Impact category for an independent review finding."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ReviewVerdict(StrEnum):
    """Reviewer's semantic recommendation to the controller."""

    ACCEPT = "accept"
    REVISE = "revise"
    FAIL = "fail"


class ReviewCriterionStatus(StrEnum):
    """Reviewer's explicit outcome for one assigned manual criterion."""

    SATISFIED = "satisfied"
    BLOCKED = "blocked"


class ReviewBoundaryKind(StrEnum):
    """Entry boundaries required for an absolute Review claim."""

    TOP_LEVEL_INPUT = "top_level_input"
    NESTED_INPUT = "nested_input"
    ALIAS_OR_INDIRECTION = "alias_or_indirection"
    FAILURE_PATH = "failure_path"


REVIEW_BOUNDARY_DEFINITIONS: Mapping[ReviewBoundaryKind, str] = MappingProxyType(
    {
        ReviewBoundaryKind.TOP_LEVEL_INPUT: (
            "The primary input value, object, resource, or entry point selected or "
            "supplied directly by the user or upstream caller, before traversal, "
            "expansion, or decomposition. If a path or directory is selected as a "
            "root, that root itself is the top-level input; every child inside it, "
            "including an immediate first-level child, is nested input."
        ),
        ReviewBoundaryKind.NESTED_INPUT: (
            "An input discovered inside or below the primary input after traversal, "
            "expansion, or decomposition. Immediate children and deeper descendants "
            "are both nested input."
        ),
        ReviewBoundaryKind.ALIAS_OR_INDIRECTION: (
            "The same logical input reached through an alias, symlink, redirect, "
            "wrapper, reference, configuration indirection, or another non-canonical "
            "route rather than its direct primary form."
        ),
        ReviewBoundaryKind.FAILURE_PATH: (
            "A missing, malformed, invalid, inaccessible, unsupported, rejected, or "
            "otherwise failing input or operation for the same requirement. Review "
            "must verify the observable failure behavior, not merely the absence of "
            "a crash."
        ),
    }
)


def review_boundary_definition_map() -> dict[str, str]:
    """Return the controller-owned boundary protocol as JSON-ready values."""

    return {
        boundary.value: definition
        for boundary, definition in REVIEW_BOUNDARY_DEFINITIONS.items()
    }


class AgentToolCallOutcome(StrEnum):
    """Controller-normalized outcome of one attributable Agent tool call."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"


class AgentToolEvidenceStatus(StrEnum):
    """Collection state for sanitized OpenClaw session tool evidence."""

    NOT_CAPTURED = "not_captured"
    CAPTURED = "captured"
    INVALID = "invalid"


class ReviewTerminationReason(StrEnum):
    """Terminal review conditions that make another revision unsafe."""

    SAFETY_BOUNDARY_CROSSED = "safety_boundary_crossed"
    EVIDENCE_INTEGRITY_COMPROMISED = "evidence_integrity_compromised"


class IterationDecision(StrEnum):
    """Controller decision after test and review evidence is available."""

    ACCEPT = "accept"
    REVISE = "revise"
    FAIL = "fail"


class FinalStatus(StrEnum):
    """Human-readable final report outcome."""

    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


IMPLEMENTATION_ROLES = {
    AgentRole.SINGLE_AGENT,
    AgentRole.GENERALIST_DEVELOPER,
    AgentRole.FRONTEND_DEVELOPER,
    AgentRole.BACKEND_DEVELOPER,
    AgentRole.INTEGRATOR,
}


def _require_clean_unique_items(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    cleaned = tuple(value.strip() for value in values)
    if any(not value for value in cleaned):
        raise ValueError("list entries must not be blank")
    if len(cleaned) != len(set(cleaned)):
        raise ValueError("list entries must be unique")
    return cleaned


def _require_safe_relative_path(value: str) -> str:
    if "\\" in value:
        raise ValueError("paths must use POSIX separators")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path == PurePosixPath("."):
        raise ValueError("paths must be safe relative paths")
    return value


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("artifact timestamps must include a timezone")
    return value.astimezone(UTC)


class AcceptanceCriterion(BaseModel):
    """One independently verifiable condition for accepting a result."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, pattern=r"^[A-Z][A-Z0-9_-]*$")
    description: str = Field(min_length=1)
    verification: str = Field(min_length=1)
    review_boundaries: tuple[ReviewBoundaryKind, ...] = ()

    @field_validator("review_boundaries")
    @classmethod
    def require_unique_review_boundaries(
        cls,
        values: tuple[ReviewBoundaryKind, ...],
    ) -> tuple[ReviewBoundaryKind, ...]:
        """Keep explicit Review obligations ordered and unique."""

        if len(values) != len(set(values)):
            raise ValueError("acceptance review boundaries must be unique")
        return values

    @model_serializer(mode="wrap")
    def omit_empty_review_boundaries(
        self,
        handler: SerializerFunctionWrapHandler,
    ) -> object:
        """Preserve existing TaskBrief bytes when no boundary is required."""

        serialized = handler(self)
        if isinstance(serialized, dict) and not self.review_boundaries:
            serialized.pop("review_boundaries", None)
        return serialized


class TaskBrief(BaseModel):
    """Confirmed requirements passed unchanged to comparable team runs."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    title: str = Field(min_length=1)
    source_request: str = Field(min_length=1)
    requirements: list[str] = Field(min_length=1)
    acceptance_criteria: list[AcceptanceCriterion] = Field(min_length=1)
    constraints: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    confirmed: bool = False

    @field_validator("requirements", "constraints", "assumptions", "open_questions")
    @classmethod
    def require_clean_unique_items(cls, values: list[str]) -> list[str]:
        """Reject blank or duplicate list entries."""

        cleaned = [value.strip() for value in values]
        if any(not value for value in cleaned):
            raise ValueError("task brief list entries must not be blank")
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("task brief list entries must be unique")
        return cleaned

    @model_validator(mode="after")
    def validate_confirmation(self) -> Self:
        """A confirmed brief cannot retain unresolved questions."""

        criterion_ids = [criterion.id for criterion in self.acceptance_criteria]
        if len(criterion_ids) != len(set(criterion_ids)):
            raise ValueError("acceptance criterion IDs must be unique")
        if self.confirmed and self.open_questions:
            raise ValueError("a confirmed task brief cannot contain open questions")
        return self


class ArtifactReference(BaseModel):
    """Run-relative reference to a persisted workflow artifact."""

    model_config = ConfigDict(extra="forbid")

    kind: ArtifactKind
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    description: str = ""

    @field_validator("path")
    @classmethod
    def require_safe_relative_path(cls, value: str) -> str:
        """Reject paths that can escape a run artifact directory."""

        if "\\" in value:
            raise ValueError("artifact paths must use POSIX separators")
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or path == PurePosixPath("."):
            raise ValueError("artifact paths must be safe run-relative paths")
        return value


class HandoffEnvelope(BaseModel):
    """Durable metadata exchanged between Agent responsibilities.

    ``stage``, ``sequence``, and ``created_at`` have compatibility defaults so
    older envelopes remain structurally readable. The context-bound artifact
    store requires explicit, valid values before persisting an envelope.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[ARTIFACT_SCHEMA_VERSION] = ARTIFACT_SCHEMA_VERSION
    kind: Literal[ArtifactKind.HANDOFF_ENVELOPE] = ArtifactKind.HANDOFF_ENVELOPE
    run_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    team_id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    iteration: int = Field(ge=1)
    stage: str = Field(default="handoff", pattern=r"^[a-z][a-z0-9_]*$")
    sequence: int = Field(default=1, ge=1)
    source_agent_id: str = Field(pattern=AGENT_ID_PATTERN)
    target_agent_id: str | None = Field(default=None, pattern=AGENT_ID_PATTERN)
    status: HandoffStatus
    created_at: datetime | None = None
    summary: str = Field(min_length=1)
    input_commit: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{7,64}$",
    )
    artifacts: list[ArtifactReference] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)

    @field_validator("created_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime | None) -> datetime | None:
        """Normalize explicit durable timestamps while accepting legacy input."""

        return None if value is None else _require_utc(value)

    @field_validator("blockers")
    @classmethod
    def require_clean_unique_blockers(cls, values: list[str]) -> list[str]:
        """Keep failure evidence concise and unambiguous."""

        cleaned = [value.strip() for value in values]
        if any(not value for value in cleaned):
            raise ValueError("blockers must not be blank")
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("blockers must be unique")
        return cleaned

    @model_validator(mode="after")
    def validate_handoff(self) -> Self:
        """Enforce evidence for failures and meaningful role boundaries."""

        if (
            self.status in {HandoffStatus.BLOCKED, HandoffStatus.FAILED}
            and not self.blockers
        ):
            raise ValueError("blocked or failed handoffs must identify a blocker")
        if (
            self.target_agent_id is not None
            and self.target_agent_id == self.source_agent_id
        ):
            raise ValueError("source and target Agents must differ")
        artifact_paths = [artifact.path for artifact in self.artifacts]
        if len(artifact_paths) != len(set(artifact_paths)):
            raise ValueError("handoff artifact references must be unique")
        return self


class PhaseArtifact(BaseModel):
    """Metadata shared by every durable workflow artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[ARTIFACT_SCHEMA_VERSION] = ARTIFACT_SCHEMA_VERSION
    kind: ArtifactKind
    run_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    team_id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    producer: str = Field(pattern=AGENT_ID_PATTERN)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        """Normalize persisted timestamps to UTC."""

        return _require_utc(value)


class IterationArtifact(PhaseArtifact):
    """Metadata shared by artifacts produced within one implementation pass."""

    iteration: int = Field(ge=1)


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
        if self.outcome is AgentToolCallOutcome.SUCCEEDED and (
            self.is_error or self.exit_code not in {None, 0} or reported_failure
        ):
            raise ValueError("successful tool evidence cannot report an error")
        if self.outcome is AgentToolCallOutcome.FAILED and (
            not self.is_error and self.exit_code in {None, 0} and not reported_failure
        ):
            raise ValueError("failed tool evidence requires an observable failure")
        if (self.tool_name == "exec") != (self.executable is not None):
            raise ValueError("only exec tool evidence requires an executable")
        return self


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
) -> None:
    """Validate one complete, absent, or rejected tool-evidence collection."""

    identifiers = [item.id for item in tool_calls]
    expected = [f"tool-{index:03d}" for index in range(1, len(tool_calls) + 1)]
    if identifiers != expected:
        raise ValueError("tool evidence IDs must be contiguous and ordered")
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


class AgentExecutionRecord(BaseModel):
    """Controller-recorded telemetry for one Agent invocation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[ARTIFACT_SCHEMA_VERSION] = ARTIFACT_SCHEMA_VERSION
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
    execution_status: AgentExecutionStatus | None = None
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
    controller_supplied_fields: tuple[str, ...] = ()
    ignored_controller_fields: tuple[str, ...] = ()
    tool_evidence_status: AgentToolEvidenceStatus = AgentToolEvidenceStatus.NOT_CAPTURED
    session_transcript_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    session_record_count: int | None = Field(default=None, ge=1, le=4096)
    tool_calls: tuple[AgentToolCallEvidence, ...] = ()
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

    @model_validator(mode="after")
    def validate_execution(self) -> Self:
        """Keep timing, exit, timeout, and response evidence coherent."""

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
            self.controller_supplied_fields or self.ignored_controller_fields
        ):
            raise ValueError("response binding fields require a response contract")
        validate_tool_evidence_collection(
            status=self.tool_evidence_status,
            transcript_sha256=self.session_transcript_sha256,
            record_count=self.session_record_count,
            tool_calls=self.tool_calls,
            error=self.tool_evidence_error,
        )
        if self.timed_out:
            if self.exit_code not in {None, 0}:
                raise ValueError(
                    "timed-out executions require no exit code or a zero "
                    "OpenClaw wrapper exit"
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
            if self.exit_code != 0 and self.error is None:
                raise ValueError("failed executions must record an error")
            if (
                self.exit_code == 0
                and self.error is None
                and self.response_artifact is None
            ):
                raise ValueError("successful executions require a response artifact")
            if self.exit_code == 0 and self.error is None and self.model is None:
                raise ValueError("successful executions require model metadata")
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
        return self


class PlanTask(BaseModel):
    """One attributable implementation task in a Planner artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, pattern=r"^TASK_[A-Z0-9_]+$")
    owner: AgentRole
    description: str = Field(min_length=1)
    dependencies: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = Field(min_length=1)
    expected_paths: tuple[str, ...] = ()

    @field_validator("dependencies", "acceptance_criteria")
    @classmethod
    def require_clean_unique_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Reject ambiguous task and criterion references."""

        return _require_clean_unique_items(values)

    @field_validator("expected_paths")
    @classmethod
    def require_safe_expected_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Keep expected repository paths relative and portable."""

        cleaned = _require_clean_unique_items(values)
        return tuple(_require_safe_relative_path(value) for value in cleaned)


class ImplementationPlan(IterationArtifact):
    """Planner-owned implementation plan for a confirmed task brief."""

    kind: Literal[ArtifactKind.IMPLEMENTATION_PLAN] = ArtifactKind.IMPLEMENTATION_PLAN
    producer: Literal["planner"] = "planner"
    iteration: Literal[1] = 1
    objective: str = Field(min_length=1)
    approach: tuple[str, ...] = Field(min_length=1)
    tasks: tuple[PlanTask, ...] = Field(min_length=1)
    risks: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()

    @field_validator("approach", "risks", "assumptions")
    @classmethod
    def require_clean_unique_text(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Reject blank and duplicate planning statements."""

        return _require_clean_unique_items(values)

    @model_validator(mode="after")
    def validate_tasks(self) -> Self:
        """Require unique, resolvable, acyclic task dependencies."""

        task_ids = [task.id for task in self.tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("implementation plan task IDs must be unique")
        known = set(task_ids)
        dependencies = {task.id: set(task.dependencies) for task in self.tasks}
        for task_id, task_dependencies in dependencies.items():
            unknown = task_dependencies - known
            if unknown:
                raise ValueError(
                    f"task {task_id} references unknown dependencies: "
                    f"{', '.join(sorted(unknown))}"
                )
            if task_id in task_dependencies:
                raise ValueError("a plan task cannot depend on itself")

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(task_id: str) -> None:
            if task_id in visiting:
                raise ValueError("implementation plan dependencies must be acyclic")
            if task_id in visited:
                return
            visiting.add(task_id)
            for dependency in dependencies[task_id]:
                visit(dependency)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in task_ids:
            visit(task_id)
        return self


class WorkResult(IterationArtifact):
    """Verified source result attributed to one implementation Agent."""

    kind: Literal[ArtifactKind.WORK_RESULT] = ArtifactKind.WORK_RESULT
    producer: str = Field(pattern=AGENT_ID_PATTERN)
    input_commit: str = Field(pattern=COMMIT_PATTERN)
    output_commit: str = Field(pattern=COMMIT_PATTERN)
    summary: str = Field(min_length=1)
    completed_tasks: tuple[str, ...] = Field(min_length=1)
    changed_files: tuple[str, ...] = Field(min_length=1)
    unresolved_issues: tuple[str, ...] = ()

    @field_validator("completed_tasks", "unresolved_issues")
    @classmethod
    def require_clean_unique_text(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Reject blank and duplicate implementation statements."""

        return _require_clean_unique_items(values)

    @field_validator("changed_files")
    @classmethod
    def require_safe_changed_files(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Require concrete repository-relative changed paths."""

        cleaned = _require_clean_unique_items(values)
        return tuple(_require_safe_relative_path(value) for value in cleaned)

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        """Require a new immutable commit.

        The run-scoped TeamPlan, rather than this context-free model, decides
        whether the producer owns an implementation capability.
        """

        if self.input_commit == self.output_commit:
            raise ValueError("work result output commit must differ from input")
        return self


class CommandEvidence(BaseModel):
    """Controller-recorded evidence for one deterministic command."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, pattern=r"^CHECK_[A-Z0-9_]+$")
    argv: tuple[str, ...] = Field(min_length=1)
    criterion_ids: tuple[str, ...] = ()
    exit_code: int | None = None
    timed_out: bool = False
    duration_ms: int = Field(ge=0)
    stdout_path: str = Field(min_length=1)
    stderr_path: str = Field(min_length=1)
    stdout_tail: str = Field(default="", max_length=4096)
    stderr_tail: str = Field(default="", max_length=4096)
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    summary: str = ""

    @field_validator("argv")
    @classmethod
    def require_clean_argv(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Persist an unambiguous argv rather than a shell command string."""

        if any(not value for value in values):
            raise ValueError("command arguments must not be empty")
        return values

    @field_validator("criterion_ids")
    @classmethod
    def require_clean_criterion_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Keep deterministic criterion coverage explicit and unambiguous."""

        return _require_clean_unique_items(values)

    @field_validator("stdout_path", "stderr_path")
    @classmethod
    def require_safe_output_path(cls, value: str) -> str:
        """Keep command output inside the run directory."""

        return _require_safe_relative_path(value)

    @model_validator(mode="after")
    def validate_exit(self) -> Self:
        """Distinguish process timeout from a normal process exit."""

        if self.timed_out and self.exit_code is not None:
            raise ValueError("timed-out commands cannot report an exit code")
        if not self.timed_out and self.exit_code is None:
            raise ValueError("completed commands require an exit code")
        if self.stdout_truncated and not self.stdout_tail:
            raise ValueError("truncated stdout requires a retained tail")
        if self.stderr_truncated and not self.stderr_tail:
            raise ValueError("truncated stderr requires a retained tail")
        return self


class CriterionResult(BaseModel):
    """Evidence-backed result for one confirmed acceptance criterion."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    criterion_id: str = Field(min_length=1, pattern=r"^[A-Z][A-Z0-9_-]*$")
    status: CheckStatus
    command_ids: tuple[str, ...] = ()
    detail: str = Field(min_length=1)

    @field_validator("command_ids")
    @classmethod
    def require_clean_command_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Reject ambiguous command references."""

        return _require_clean_unique_items(values)


class TestReport(IterationArtifact):
    """Tester analysis grounded in deterministic command evidence."""

    kind: Literal[ArtifactKind.TEST_REPORT] = ArtifactKind.TEST_REPORT
    producer: str = Field(default="tester", pattern=AGENT_ID_PATTERN)
    input_commit: str = Field(pattern=COMMIT_PATTERN)
    status: Literal[
        CheckStatus.PASSED,
        CheckStatus.FAILED,
        CheckStatus.BLOCKED,
    ] = Field(
        description=(
            "Overall deterministic Tester result. Use passed when every command "
            "and deterministic-only criterion passes, manual-review criteria are "
            "pending_review, and no blocker exists. pending_review is valid only "
            "for individual manual-review criteria, never for this field."
        )
    )
    commands: tuple[CommandEvidence, ...] = Field(min_length=1)
    criteria: tuple[CriterionResult, ...] = Field(min_length=1)
    manual_review_criteria: tuple[str, ...] = ()
    findings: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    summary: str = Field(min_length=1)

    @field_validator("findings", "blockers", "manual_review_criteria")
    @classmethod
    def require_clean_unique_text(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Reject blank and duplicate verification findings."""

        return _require_clean_unique_items(values)

    @model_validator(mode="after")
    def validate_evidence(self) -> Self:
        """Keep overall test status consistent with recorded evidence."""

        command_ids = [command.id for command in self.commands]
        if len(command_ids) != len(set(command_ids)):
            raise ValueError("test command IDs must be unique")
        criterion_ids = [criterion.criterion_id for criterion in self.criteria]
        if len(criterion_ids) != len(set(criterion_ids)):
            raise ValueError("test criterion IDs must be unique")
        known_commands = set(command_ids)
        command_coverage = {
            command.id: set(command.criterion_ids) for command in self.commands
        }
        coverage_available = all(command.criterion_ids for command in self.commands)
        for criterion in self.criteria:
            unknown = set(criterion.command_ids) - known_commands
            if unknown:
                raise ValueError(
                    f"criterion {criterion.criterion_id} references unknown commands"
                )
            if coverage_available:
                mismatched = [
                    command_id
                    for command_id in criterion.command_ids
                    if criterion.criterion_id not in command_coverage[command_id]
                ]
                if mismatched:
                    raise ValueError(
                        f"criterion {criterion.criterion_id} references commands "
                        "without declared coverage"
                    )

        known_criteria = set(criterion_ids)
        manual_criteria = set(self.manual_review_criteria)
        unknown_manual = manual_criteria - known_criteria
        if unknown_manual:
            raise ValueError("manual-review criteria must exist in the test report")
        for criterion in self.criteria:
            pending = criterion.status is CheckStatus.PENDING_REVIEW
            if pending and criterion.criterion_id not in manual_criteria:
                raise ValueError("only manual-review criteria may be pending review")
            if (
                criterion.criterion_id in manual_criteria
                and criterion.status is CheckStatus.PASSED
            ):
                raise ValueError(
                    "Tester cannot pass criteria assigned to independent review"
                )

        commands_passed = all(
            not command.timed_out and command.exit_code == 0
            for command in self.commands
        )
        tester_scope_passed = all(
            criterion.status is CheckStatus.PASSED
            or (
                criterion.criterion_id in manual_criteria
                and criterion.status is CheckStatus.PENDING_REVIEW
            )
            for criterion in self.criteria
        )
        if self.status is CheckStatus.PASSED:
            if not commands_passed or not tester_scope_passed or self.blockers:
                raise ValueError("passed test reports require all evidence to pass")
        elif self.status is CheckStatus.BLOCKED:
            if not self.blockers:
                raise ValueError("blocked test reports must identify a blocker")
            if not any(
                criterion.status is CheckStatus.BLOCKED for criterion in self.criteria
            ):
                raise ValueError("blocked test reports require a blocked criterion")
        else:
            if not any(
                criterion.status is CheckStatus.FAILED for criterion in self.criteria
            ):
                raise ValueError("failed test reports require a failed criterion")
            if commands_passed and tester_scope_passed:
                raise ValueError("failed test reports require failing evidence")
        return self


class ReviewFinding(BaseModel):
    """One attributable independent-review finding."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, pattern=r"^FINDING_[A-Z0-9_]+$")
    severity: ReviewSeverity = Field(
        description=(
            "Product impact. Critical severity alone does not make a finding "
            "terminal when a Developer revision can correct it."
        )
    )
    blocking: bool
    category: str = Field(min_length=1)
    description: str = Field(min_length=1)
    recommendation: str = Field(min_length=1)
    path: str | None = None
    line: int | None = Field(default=None, ge=1)
    criterion_ids: tuple[str, ...] = ()

    @field_validator("path")
    @classmethod
    def require_safe_finding_path(cls, value: str | None) -> str | None:
        """Keep optional source locations repository-relative."""

        return None if value is None else _require_safe_relative_path(value)

    @field_validator("criterion_ids")
    @classmethod
    def require_clean_criterion_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Reject duplicate criterion references."""

        return _require_clean_unique_items(values)

    @model_validator(mode="after")
    def validate_finding(self) -> Self:
        """Keep locations and critical severity semantically coherent."""

        if self.line is not None and self.path is None:
            raise ValueError("a finding line requires a source path")
        if self.severity is ReviewSeverity.CRITICAL and not self.blocking:
            raise ValueError("critical review findings must be blocking")
        return self


class ReviewBoundaryCheck(BaseModel):
    """Controller-grounded evidence for one required Review entry boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    boundary: ReviewBoundaryKind
    adversarial_check: str = Field(min_length=1, max_length=1000)
    command_evidence_ids: tuple[str, ...] = ()
    tool_evidence: tuple[ReviewToolEvidenceReference, ...] = ()

    @field_validator("adversarial_check")
    @classmethod
    def require_clean_adversarial_check(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("boundary adversarial check must not be blank")
        return cleaned

    @field_validator("command_evidence_ids")
    @classmethod
    def require_unique_command_evidence(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        if len(values) != len(set(values)) or any(
            re.fullmatch(r"CHECK_[A-Z0-9_]+", value) is None for value in values
        ):
            raise ValueError("boundary command-evidence references must be valid")
        return values

    @field_validator("tool_evidence")
    @classmethod
    def require_unique_tool_evidence(
        cls,
        values: tuple[ReviewToolEvidenceReference, ...],
    ) -> tuple[ReviewToolEvidenceReference, ...]:
        identifiers = [
            (value.execution_attempt, value.tool_call_id) for value in values
        ]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("boundary tool-evidence references must be unique")
        return values

    @model_validator(mode="after")
    def require_grounded_evidence(self) -> Self:
        """Allow command-only boundaries while rejecting an ungrounded check."""

        if not self.command_evidence_ids and not self.tool_evidence:
            raise ValueError(
                "boundary checks require command or tool evidence references"
            )
        return self

    @model_serializer(mode="wrap")
    def omit_empty_evidence(
        self,
        handler: SerializerFunctionWrapHandler,
    ) -> object:
        serialized = handler(self)
        if isinstance(serialized, dict):
            if not self.command_evidence_ids:
                serialized.pop("command_evidence_ids", None)
            if not self.tool_evidence:
                serialized.pop("tool_evidence", None)
        return serialized


class ReviewCriterionAssessment(BaseModel):
    """Attributable evidence and adversarial reasoning for one criterion."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    criterion_id: str = Field(min_length=1, pattern=r"^[A-Z][A-Z0-9_-]*$")
    status: ReviewCriterionStatus
    adversarial_check: str = Field(min_length=1, max_length=2000)
    evidence: str = Field(min_length=1, max_length=2000)
    command_evidence_ids: tuple[str, ...] = ()
    tool_evidence: tuple[ReviewToolEvidenceReference, ...] = ()
    boundary_checks: tuple[ReviewBoundaryCheck, ...] = ()

    @field_validator("adversarial_check", "evidence")
    @classmethod
    def require_clean_assessment_text(cls, value: str) -> str:
        """Reject empty presentation-only evidence."""

        cleaned = value.strip()
        if not cleaned:
            raise ValueError("criterion assessment text must not be blank")
        return cleaned

    @field_validator("command_evidence_ids")
    @classmethod
    def require_unique_command_evidence(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        """Keep controller command references explicit and unambiguous."""

        if len(values) != len(set(values)) or any(
            re.fullmatch(r"CHECK_[A-Z0-9_]+", value) is None for value in values
        ):
            raise ValueError(
                "criterion command-evidence references must be valid and unique"
            )
        return values

    @field_validator("tool_evidence")
    @classmethod
    def require_unique_tool_evidence(
        cls,
        values: tuple[ReviewToolEvidenceReference, ...],
    ) -> tuple[ReviewToolEvidenceReference, ...]:
        """Keep controller tool references unambiguous within one criterion."""

        identifiers = [
            (value.execution_attempt, value.tool_call_id) for value in values
        ]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("criterion tool-evidence references must be unique")
        return values

    @field_validator("boundary_checks")
    @classmethod
    def require_unique_boundary_checks(
        cls,
        values: tuple[ReviewBoundaryCheck, ...],
    ) -> tuple[ReviewBoundaryCheck, ...]:
        boundaries = [value.boundary for value in values]
        if len(boundaries) != len(set(boundaries)):
            raise ValueError("criterion boundary checks must be unique")
        return values

    @model_serializer(mode="wrap")
    def omit_empty_command_evidence(
        self,
        handler: SerializerFunctionWrapHandler,
    ) -> object:
        """Keep pre-v3 Review artifacts byte-stable when no command is bound."""

        serialized = handler(self)
        if isinstance(serialized, dict) and not self.command_evidence_ids:
            serialized.pop("command_evidence_ids", None)
        if isinstance(serialized, dict) and not self.boundary_checks:
            serialized.pop("boundary_checks", None)
        return serialized


class ReviewReport(IterationArtifact):
    """Independent semantic review of one immutable implementation commit."""

    kind: Literal[ArtifactKind.REVIEW_REPORT] = ArtifactKind.REVIEW_REPORT
    producer: str = Field(default="reviewer", pattern=AGENT_ID_PATTERN)
    input_commit: str = Field(pattern=COMMIT_PATTERN)
    verdict: ReviewVerdict = Field(
        description=(
            "Use revise for every correctable implementation defect, including "
            "failed acceptance gates; fail is reserved for a terminal safety or "
            "evidence-integrity boundary."
        )
    )
    termination_reason: ReviewTerminationReason | None = Field(
        default=None,
        description=(
            "Required only with fail. It records the terminal boundary that makes "
            "another implementation revision unsafe."
        ),
    )
    reviewed_criteria: tuple[str, ...] = ()
    criterion_assessments: tuple[ReviewCriterionAssessment, ...] = ()
    findings: tuple[ReviewFinding, ...] = ()
    summary: str = Field(min_length=1)

    @field_validator("reviewed_criteria")
    @classmethod
    def require_clean_reviewed_criteria(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        """Keep the independent review scope explicit and attributable."""

        return _require_clean_unique_items(values)

    @model_validator(mode="after")
    def validate_verdict(self) -> Self:
        """Tie accept, revise, and fail recommendations to findings."""

        finding_ids = [finding.id for finding in self.findings]
        if len(finding_ids) != len(set(finding_ids)):
            raise ValueError("review finding IDs must be unique")
        assessment_ids = [
            assessment.criterion_id for assessment in self.criterion_assessments
        ]
        if len(assessment_ids) != len(set(assessment_ids)):
            raise ValueError("review criterion assessments must be unique")
        if self.criterion_assessments and set(assessment_ids) != set(
            self.reviewed_criteria
        ):
            raise ValueError(
                "review criterion assessments must exactly cover review scope"
            )
        blocking = [finding for finding in self.findings if finding.blocking]
        blocked_criteria = {
            assessment.criterion_id
            for assessment in self.criterion_assessments
            if assessment.status is ReviewCriterionStatus.BLOCKED
        }
        finding_blocked_criteria = {
            criterion_id
            for finding in blocking
            for criterion_id in finding.criterion_ids
        }
        if self.criterion_assessments and blocked_criteria != finding_blocked_criteria:
            raise ValueError(
                "blocked criterion assessments must exactly match blocking findings"
            )
        critical = [
            finding
            for finding in self.findings
            if finding.severity is ReviewSeverity.CRITICAL
        ]
        if (
            self.verdict is ReviewVerdict.ACCEPT
            and self.criterion_assessments
            and blocked_criteria
        ):
            raise ValueError("accepted reviews require every criterion to be satisfied")
        if (
            self.verdict in {ReviewVerdict.REVISE, ReviewVerdict.FAIL}
            and self.criterion_assessments
            and not blocked_criteria
        ):
            raise ValueError("non-accepted reviews require a blocked criterion")
        if self.verdict is ReviewVerdict.ACCEPT and blocking:
            raise ValueError("accepted reviews cannot contain blocking findings")
        if self.verdict is ReviewVerdict.REVISE and not blocking:
            raise ValueError("revision requires a blocking review finding")
        if self.verdict is ReviewVerdict.FAIL and not critical:
            raise ValueError("failed reviews require a critical finding")
        if (
            self.verdict is not ReviewVerdict.FAIL
            and self.termination_reason is not None
        ):
            raise ValueError("only failed reviews may declare a terminal reason")
        return self


def resolve_acceptance_results(
    test: TestReport,
    reviews: ReviewReport | tuple[ReviewReport, ...],
) -> tuple[CriterionResult, ...]:
    """Resolve manual criteria when accepted independent reviews cover them."""

    normalized_reviews = (reviews,) if isinstance(reviews, ReviewReport) else reviews
    manual = set(test.manual_review_criteria)
    reviewed = {
        criterion_id
        for review in normalized_reviews
        for criterion_id in review.reviewed_criteria
    }
    if reviewed != manual:
        raise ValueError("independent reviews must exactly cover manual criteria")
    if any(review.verdict is not ReviewVerdict.ACCEPT for review in normalized_reviews):
        raise ValueError("manual acceptance requires every review to be accepted")
    results: list[CriterionResult] = []
    for criterion in test.criteria:
        if criterion.criterion_id not in manual:
            results.append(criterion)
            continue
        if criterion.status is not CheckStatus.PENDING_REVIEW:
            raise ValueError("manual criterion is not pending independent review")
        results.append(
            criterion.model_copy(
                update={
                    "status": CheckStatus.PASSED,
                    "detail": (
                        f"{criterion.detail} Independent review accepted the "
                        "assigned manual criterion on the same immutable commit."
                    ),
                }
            )
        )
    return tuple(results)


class IterationRecord(IterationArtifact):
    """Controller-owned decision record for one implementation iteration."""

    kind: Literal[ArtifactKind.ITERATION_RECORD] = ArtifactKind.ITERATION_RECORD
    producer: Literal["controller"] = "controller"
    input_commit: str = Field(pattern=COMMIT_PATTERN)
    output_commit: str = Field(pattern=COMMIT_PATTERN)
    implementation_plan: ArtifactReference | None = None
    implementation_plan_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    work_results: tuple[ArtifactReference, ...] = Field(min_length=1)
    test_reports: tuple[ArtifactReference, ...] = Field(min_length=1)
    review_reports: tuple[ArtifactReference, ...] = ()
    decision: IterationDecision
    blocking_finding_ids: tuple[str, ...] = ()
    blocking_reasons: tuple[str, ...] = ()
    resolved_finding_ids: tuple[str, ...] = ()
    summary: str = Field(min_length=1)

    @field_validator(
        "blocking_finding_ids",
        "blocking_reasons",
        "resolved_finding_ids",
    )
    @classmethod
    def require_clean_finding_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Reject duplicate finding references."""

        return _require_clean_unique_items(values)

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        """Require exact artifact kinds and evidence for revision or failure."""

        if (self.implementation_plan is None) == (
            self.implementation_plan_sha256 is None
        ):
            raise ValueError(
                "iteration record requires exactly one implementation-plan binding"
            )
        if (
            self.implementation_plan is not None
            and self.implementation_plan.kind is not ArtifactKind.IMPLEMENTATION_PLAN
        ):
            raise ValueError("implementation_plan must reference implementation_plan")
        expected_collections = {
            "work_results": ArtifactKind.WORK_RESULT,
            "test_reports": ArtifactKind.TEST_REPORT,
            "review_reports": ArtifactKind.REVIEW_REPORT,
        }
        for field, expected in expected_collections.items():
            references = getattr(self, field)
            if any(reference.kind is not expected for reference in references):
                raise ValueError(f"{field} must reference only {expected.value}")
            paths = [reference.path for reference in references]
            if len(paths) != len(set(paths)):
                raise ValueError(f"{field} references must be unique")
        if self.input_commit == self.output_commit:
            raise ValueError("iteration output commit must differ from input")
        has_blocker = bool(self.blocking_finding_ids or self.blocking_reasons)
        if self.decision is IterationDecision.ACCEPT and has_blocker:
            raise ValueError("accepted iterations cannot retain blockers")
        if self.decision is not IterationDecision.ACCEPT and not has_blocker:
            raise ValueError("revision and failure require blocking evidence")
        overlap = set(self.blocking_finding_ids) & set(self.resolved_finding_ids)
        if overlap:
            raise ValueError("a finding cannot be both blocking and resolved")
        return self


class FinalReport(PhaseArtifact):
    """Controller-owned final delivery or failure report."""

    kind: Literal[ArtifactKind.FINAL_REPORT] = ArtifactKind.FINAL_REPORT
    producer: Literal["controller"] = "controller"
    status: FinalStatus
    termination_reason: str = Field(min_length=1)
    final_commit: str | None = Field(default=None, pattern=COMMIT_PATTERN)
    iterations: tuple[ArtifactReference, ...] = ()
    acceptance_results: tuple[CriterionResult, ...] = ()
    unresolved_findings: tuple[str, ...] = ()
    known_limitations: tuple[str, ...] = ()
    summary: str = Field(min_length=1)

    @field_validator("unresolved_findings", "known_limitations")
    @classmethod
    def require_clean_unique_text(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Reject blank and duplicate final-report statements."""

        return _require_clean_unique_items(values)

    @model_validator(mode="after")
    def validate_final_report(self) -> Self:
        """Tie a completed report to commit and acceptance evidence."""

        if any(
            reference.kind is not ArtifactKind.ITERATION_RECORD
            for reference in self.iterations
        ):
            raise ValueError("final report iterations must reference iteration records")
        paths = [reference.path for reference in self.iterations]
        if len(paths) != len(set(paths)):
            raise ValueError("final report iteration references must be unique")

        criterion_ids = [result.criterion_id for result in self.acceptance_results]
        if len(criterion_ids) != len(set(criterion_ids)):
            raise ValueError("final acceptance criterion IDs must be unique")
        if self.status is FinalStatus.COMPLETED:
            if self.final_commit is None:
                raise ValueError("completed final reports require a commit")
            if not self.iterations or not self.acceptance_results:
                raise ValueError(
                    "completed final reports require iteration and acceptance evidence"
                )
            if any(
                result.status is not CheckStatus.PASSED
                for result in self.acceptance_results
            ):
                raise ValueError("completed final reports require passed acceptance")
        return self


type PersistedArtifact = (
    ImplementationPlan
    | WorkResult
    | TestReport
    | ReviewReport
    | IterationRecord
    | FinalReport
    | HandoffEnvelope
    | AgentExecutionRecord
)


ARTIFACT_MODELS: dict[ArtifactKind, type[PersistedArtifact]] = {
    ArtifactKind.IMPLEMENTATION_PLAN: ImplementationPlan,
    ArtifactKind.WORK_RESULT: WorkResult,
    ArtifactKind.TEST_REPORT: TestReport,
    ArtifactKind.REVIEW_REPORT: ReviewReport,
    ArtifactKind.ITERATION_RECORD: IterationRecord,
    ArtifactKind.FINAL_REPORT: FinalReport,
    ArtifactKind.HANDOFF_ENVELOPE: HandoffEnvelope,
    ArtifactKind.AGENT_EXECUTION_RECORD: AgentExecutionRecord,
}


def parse_phase_artifact(payload: object) -> PersistedArtifact:
    """Dispatch an untrusted JSON payload to its one declared artifact model."""

    if not isinstance(payload, dict):
        raise ValueError("phase artifact must be a JSON object")
    try:
        kind = ArtifactKind(payload["kind"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("phase artifact requires a supported kind") from error
    model = ARTIFACT_MODELS.get(kind)
    if model is None:
        raise ValueError(f"unsupported phase artifact kind: {kind.value}")
    return model.model_validate(payload)


def validate_artifact_context(
    artifact: PersistedArtifact,
    *,
    task_brief: TaskBrief,
    team_id: str,
    team_agents: dict[str, str],
    iteration_limit: int,
    team_stages: dict[str, set[str]] | None = None,
) -> None:
    """Validate one phase artifact against its frozen run inputs."""

    if artifact.run_id != task_brief.run_id:
        raise ValueError("artifact run ID does not match the task brief")
    if artifact.team_id != team_id:
        raise ValueError("artifact team ID does not match the selected team")
    if (
        isinstance(artifact, (IterationArtifact, HandoffEnvelope, AgentExecutionRecord))
        and artifact.iteration > iteration_limit
    ):
        raise ValueError("artifact iteration exceeds the run iteration limit")
    producer = getattr(artifact, "producer", None)
    if (
        producer is not None
        and producer != "controller"
        and producer not in team_agents
    ):
        raise ValueError("artifact producer is not part of the approved TeamPlan")

    if isinstance(artifact, (HandoffEnvelope, AgentExecutionRecord)):
        if team_stages is None:
            raise ValueError("Agent runtime artifacts require team stage context")
        stage_agents = team_stages.get(artifact.stage)
        if stage_agents is None:
            raise ValueError("artifact stage is not part of the selected team")
        agent_id = (
            artifact.source_agent_id
            if isinstance(artifact, HandoffEnvelope)
            else artifact.agent_id
        )
        if agent_id not in team_agents:
            raise ValueError("artifact Agent is not part of the approved TeamPlan")
        if agent_id not in stage_agents:
            raise ValueError("artifact Agent is not assigned to its declared stage")
        if (
            isinstance(artifact, AgentExecutionRecord)
            and artifact.capability != team_agents[agent_id]
        ):
            raise ValueError("execution capability differs from its approved AgentSpec")

    if isinstance(artifact, HandoffEnvelope):
        if artifact.created_at is None:
            raise ValueError("persisted handoffs require a creation timestamp")
        if (
            artifact.target_agent_id is not None
            and artifact.target_agent_id not in team_agents
        ):
            raise ValueError("handoff target is not part of the approved TeamPlan")
        if artifact.input_commit is None:
            raise ValueError("persisted handoffs require an input commit")
        if re.fullmatch(COMMIT_PATTERN, artifact.input_commit) is None:
            raise ValueError("persisted handoffs require a full commit ID")

    expected_criteria = {criterion.id for criterion in task_brief.acceptance_criteria}
    referenced_criteria: set[str] = set()
    if isinstance(artifact, ImplementationPlan):
        for task in artifact.tasks:
            owner = task.owner.value
            if team_agents.get(owner) not in {"implementation", "integration"}:
                raise ValueError(
                    "plan task owner is not an approved implementation Agent"
                )
            referenced_criteria.update(task.acceptance_criteria)
        if referenced_criteria != expected_criteria:
            missing = sorted(expected_criteria - referenced_criteria)
            unknown = sorted(referenced_criteria - expected_criteria)
            details = []
            if missing:
                details.append(f"missing: {', '.join(missing)}")
            if unknown:
                details.append(f"unknown: {', '.join(unknown)}")
            raise ValueError(
                "implementation plan acceptance coverage differs from the task "
                f"brief ({'; '.join(details)})"
            )
    elif isinstance(artifact, WorkResult):
        if team_agents.get(artifact.producer) not in {
            "implementation",
            "integration",
        }:
            raise ValueError("work-result producer lacks implementation capability")
    elif isinstance(artifact, TestReport):
        if artifact.producer != "controller" and (
            team_agents.get(artifact.producer) != "testing"
        ):
            raise ValueError("test-report producer lacks testing capability")
        referenced_criteria = {
            criterion.criterion_id for criterion in artifact.criteria
        }
        if referenced_criteria != expected_criteria:
            raise ValueError(
                "test report must cover every confirmed acceptance criterion"
            )
        if not set(artifact.manual_review_criteria).issubset(expected_criteria):
            raise ValueError("test report references an unknown manual criterion")
    elif isinstance(artifact, ReviewReport):
        if team_agents.get(artifact.producer) != "review":
            raise ValueError("review-report producer lacks review capability")
        referenced_criteria = {
            criterion_id
            for finding in artifact.findings
            for criterion_id in finding.criterion_ids
        }
        referenced_criteria.update(artifact.reviewed_criteria)
        if not referenced_criteria.issubset(expected_criteria):
            raise ValueError("review report references an unknown criterion")
    elif isinstance(artifact, FinalReport):
        referenced_criteria = {
            result.criterion_id for result in artifact.acceptance_results
        }
        if artifact.status is FinalStatus.COMPLETED:
            if referenced_criteria != expected_criteria:
                raise ValueError(
                    "completed final report must cover every acceptance criterion"
                )
        elif not referenced_criteria.issubset(expected_criteria):
            raise ValueError("final report references an unknown criterion")
