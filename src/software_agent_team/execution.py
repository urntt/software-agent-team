"""Replaceable Agent execution adapters and observable subprocess evidence."""

from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import signal
import stat
import subprocess
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import ClassVar, Protocol, Self, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.types import JsonValue

from software_agent_team.artifacts import (
    AgentExecutionStatus,
    AgentRole,
    AgentToolCallEvidence,
    AgentToolCallOutcome,
    AgentToolEvidenceStatus,
    ArtifactKind,
    PhaseArtifact,
    ProviderLivenessEvidence,
    validate_tool_evidence_collection,
)
from software_agent_team.invocation_lifecycle import (
    InitializationCheckpoint,
    InitializationLivenessEvidence,
    InvocationLifecycleEvidence,
    InvocationLifecycleTransition,
    InvocationPhase,
    InvocationShutdownEvidence,
    InvocationStopReason,
)
from software_agent_team.openclaw_session_evidence import (
    CapturedOpenClawToolEvidence,
    OpenClawSessionEvidenceError,
    capture_openclaw_tool_evidence,
    inspect_openclaw_initialization,
    inspect_openclaw_session_activity,
)
from software_agent_team.process_lifecycle import (
    InvocationProcessLease,
    ProcessLeaseStore,
)
from software_agent_team.submissions import (
    AgentSemanticSubmission,
    AgentSubmissionContract,
    AgentSubmissionEvidence,
    AgentSubmissionStatus,
    SubmissionFileCapture,
    canonical_json_sha256,
    capture_submission_file,
    rejected_submission_evidence,
    validate_submission_capture,
)
from software_agent_team.teams import (
    AgentCapability,
    capability_for_legacy_role,
    expected_output_for_capability,
)

DEFAULT_PROCESS_SHUTDOWN_GRACE_SECONDS = 35
DEFAULT_INITIALIZATION_NO_PROGRESS_SECONDS = 90.0
DEFAULT_INITIALIZATION_STALL_GRACE_SECONDS = 15.0
DEFAULT_CLOUD_PROVIDER_SILENCE_SECONDS = 120.0
DEFAULT_LOCAL_PROVIDER_SILENCE_SECONDS = 300.0
DEFAULT_PROVIDER_STALL_GRACE_SECONDS = 30.0
DEFAULT_LIVENESS_POLL_SECONDS = 0.25
PROVIDER_ACTIVITY_REPORT_SECONDS = 10.0

ROLE_ARTIFACT_KINDS: dict[AgentRole, frozenset[ArtifactKind]] = {
    AgentRole.CLARIFIER: frozenset({ArtifactKind.CLARIFICATION_RECORD}),
    AgentRole.SINGLE_AGENT: frozenset({ArtifactKind.WORK_RESULT}),
    AgentRole.PLANNER: frozenset({ArtifactKind.IMPLEMENTATION_PLAN}),
    AgentRole.GENERALIST_DEVELOPER: frozenset({ArtifactKind.WORK_RESULT}),
    AgentRole.FRONTEND_DEVELOPER: frozenset({ArtifactKind.WORK_RESULT}),
    AgentRole.BACKEND_DEVELOPER: frozenset({ArtifactKind.WORK_RESULT}),
    AgentRole.INTEGRATOR: frozenset({ArtifactKind.WORK_RESULT}),
    AgentRole.TESTER: frozenset({ArtifactKind.TEST_REPORT}),
    AgentRole.REVIEWER: frozenset({ArtifactKind.REVIEW_REPORT}),
}


class AgentExecutionError(RuntimeError):
    """Base error raised by the replaceable Agent execution boundary."""


class ScriptedResponseExhaustedError(AgentExecutionError):
    """Raised when an offline scripted executor has no response left."""


class AgentExecutionActivityKind(StrEnum):
    """Content-free activity emitted while one provider invocation is active."""

    INVOCATION_LAUNCHED = "invocation_launched"
    INVOCATION_INITIALIZING = "invocation_initializing"
    INITIALIZATION_PROGRESS = "initialization_progress"
    INITIALIZATION_LIVENESS_DEGRADED = "initialization_liveness_degraded"
    INITIALIZATION_STALL_SUSPECTED = "initialization_stall_suspected"
    INITIALIZATION_STALL_RECOVERED = "initialization_stall_recovered"
    INITIALIZATION_STALLED = "initialization_stalled"
    INVOCATION_PROVIDER_WAIT = "invocation_provider_wait"
    INVOCATION_TOOL_ACTIVE = "invocation_tool_active"
    INVOCATION_STOPPING = "invocation_stopping"
    INVOCATION_COLLECTING_EVIDENCE = "invocation_collecting_evidence"
    INVOCATION_STOPPED = "invocation_stopped"
    PROVIDER_STREAM = "provider_stream"
    TOOL_STARTED = "tool_started"
    TOOL_COMPLETED = "tool_completed"
    LIVENESS_DEGRADED = "liveness_degraded"
    STALL_SUSPECTED = "stall_suspected"
    STALL_RECOVERED = "stall_recovered"
    PROVIDER_STALLED = "provider_stalled"


class ProviderLivenessPolicy(BaseModel):
    """Provider/model-aware renewable silence lease for one invocation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str = Field(min_length=1)
    silence_seconds: float = Field(gt=0)
    stall_grace_seconds: float = Field(gt=0)
    source: str = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def require_a_warning_window(self) -> Self:
        if self.stall_grace_seconds >= self.silence_seconds:
            raise ValueError("provider stall grace must be shorter than the lease")
        return self

    @property
    def suspect_after_seconds(self) -> float:
        """Return when the visible grace phase starts."""

        return self.silence_seconds - self.stall_grace_seconds


class InitializationLivenessPolicy(BaseModel):
    """Infrastructure guard for launch-to-current-turn progress only."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    no_progress_seconds: float = Field(gt=0)
    stall_grace_seconds: float = Field(gt=0)
    source: str = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def require_a_warning_window(self) -> Self:
        if self.stall_grace_seconds >= self.no_progress_seconds:
            raise ValueError("initialization grace must be shorter than its ceiling")
        return self

    @property
    def suspect_after_seconds(self) -> float:
        """Return when the visible initialization diagnostic starts."""

        return self.no_progress_seconds - self.stall_grace_seconds


def resolve_provider_liveness_policy(
    *,
    model: str,
    local: bool | None,
    provider_request_timeout_seconds: int | None = None,
) -> ProviderLivenessPolicy:
    """Mirror the pinned OpenClaw stream boundary without a work budget."""

    upstream_seconds = (
        DEFAULT_LOCAL_PROVIDER_SILENCE_SECONDS
        if local is True
        else DEFAULT_CLOUD_PROVIDER_SILENCE_SECONDS
    )
    sources = [
        "pinned OpenClaw local first-event boundary"
        if local is True
        else "pinned OpenClaw cloud first-event boundary"
    ]
    if provider_request_timeout_seconds is not None:
        if provider_request_timeout_seconds < 1:
            raise ValueError("provider request timeout must be positive")
        # The pinned runtime treats an explicit provider timeout as an override,
        # including when it deliberately extends the implicit cloud boundary for
        # a slow model. Taking the smaller value here would recreate the fixed
        # cutoff that this renewable-liveness contract replaces.
        upstream_seconds = provider_request_timeout_seconds
        sources.append("configured provider request timeout")
    grace = min(
        DEFAULT_PROVIDER_STALL_GRACE_SECONDS,
        upstream_seconds / 4,
    )
    return ProviderLivenessPolicy(
        model=model,
        silence_seconds=upstream_seconds,
        stall_grace_seconds=grace,
        source="; ".join(sources),
    )


class AgentExecutionActivity(BaseModel):
    """Safe controller observation; never contains provider or tool content."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: AgentExecutionActivityKind
    agent_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    session_key: str = Field(min_length=1)
    model: str | None = Field(default=None, min_length=1)
    elapsed_ms: int = Field(ge=0)
    trusted_activity_count: int = Field(default=0, ge=0)
    active_tool_count: int = Field(default=0, ge=0)
    completed_tool_count: int = Field(default=0, ge=0)
    inactivity_ms: int = Field(default=0, ge=0)
    silence_seconds: float | None = Field(default=None, gt=0)
    stall_grace_seconds: float | None = Field(default=None, gt=0)
    policy_source: str | None = Field(default=None, min_length=1, max_length=300)
    degradation_reason: str | None = Field(default=None, min_length=1, max_length=500)
    invocation_phase: InvocationPhase | None = None
    stop_reason: InvocationStopReason | None = None
    initialization_checkpoint: InitializationCheckpoint | None = None
    shutdown_grace_seconds: float | None = Field(default=None, gt=0)
    action: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def bind_degradation_reason(self) -> Self:
        degraded_kinds = {
            AgentExecutionActivityKind.LIVENESS_DEGRADED,
            AgentExecutionActivityKind.INITIALIZATION_LIVENESS_DEGRADED,
        }
        if (self.kind in degraded_kinds) != (self.degradation_reason is not None):
            raise ValueError(
                "only degraded liveness activity may contain a degradation reason"
            )
        provider_kinds = {
            AgentExecutionActivityKind.PROVIDER_STREAM,
            AgentExecutionActivityKind.TOOL_STARTED,
            AgentExecutionActivityKind.TOOL_COMPLETED,
            AgentExecutionActivityKind.LIVENESS_DEGRADED,
            AgentExecutionActivityKind.STALL_SUSPECTED,
            AgentExecutionActivityKind.STALL_RECOVERED,
            AgentExecutionActivityKind.PROVIDER_STALLED,
        }
        initialization_kinds = {
            AgentExecutionActivityKind.INITIALIZATION_PROGRESS,
            AgentExecutionActivityKind.INITIALIZATION_LIVENESS_DEGRADED,
            AgentExecutionActivityKind.INITIALIZATION_STALL_SUSPECTED,
            AgentExecutionActivityKind.INITIALIZATION_STALL_RECOVERED,
            AgentExecutionActivityKind.INITIALIZATION_STALLED,
        }
        if self.kind in provider_kinds and any(
            value is None
            for value in (
                self.silence_seconds,
                self.stall_grace_seconds,
                self.policy_source,
            )
        ):
            raise ValueError("provider activity requires its liveness policy")
        if self.kind in initialization_kinds and any(
            value is None
            for value in (
                self.silence_seconds,
                self.stall_grace_seconds,
                self.policy_source,
            )
        ):
            raise ValueError("initialization activity requires its guard policy")
        lifecycle_phases = {
            AgentExecutionActivityKind.INVOCATION_LAUNCHED: InvocationPhase.LAUNCHED,
            AgentExecutionActivityKind.INVOCATION_INITIALIZING: (
                InvocationPhase.INITIALIZING
            ),
            AgentExecutionActivityKind.INVOCATION_PROVIDER_WAIT: (
                InvocationPhase.PROVIDER_WAIT
            ),
            AgentExecutionActivityKind.INVOCATION_TOOL_ACTIVE: (
                InvocationPhase.TOOL_ACTIVE
            ),
            AgentExecutionActivityKind.INVOCATION_STOPPING: InvocationPhase.STOPPING,
            AgentExecutionActivityKind.INVOCATION_COLLECTING_EVIDENCE: (
                InvocationPhase.COLLECTING_EVIDENCE
            ),
            AgentExecutionActivityKind.INVOCATION_STOPPED: InvocationPhase.STOPPED,
        }
        expected_phase = lifecycle_phases.get(self.kind)
        if (expected_phase is not None) != (self.invocation_phase is not None):
            raise ValueError("lifecycle activity requires exactly one invocation phase")
        if expected_phase is not None and self.invocation_phase is not expected_phase:
            raise ValueError("lifecycle activity kind must match its invocation phase")
        stopping_kinds = {
            AgentExecutionActivityKind.INVOCATION_STOPPING,
            AgentExecutionActivityKind.INVOCATION_COLLECTING_EVIDENCE,
            AgentExecutionActivityKind.INVOCATION_STOPPED,
        }
        if (self.kind in stopping_kinds) != (self.stop_reason is not None):
            raise ValueError("shutdown activity requires exactly one typed stop reason")
        if self.shutdown_grace_seconds is not None and self.kind is not (
            AgentExecutionActivityKind.INVOCATION_STOPPING
        ):
            raise ValueError("shutdown grace belongs only to the stopping transition")
        if self.initialization_checkpoint is not None and self.kind not in {
            AgentExecutionActivityKind.INVOCATION_INITIALIZING,
            AgentExecutionActivityKind.INVOCATION_PROVIDER_WAIT,
            AgentExecutionActivityKind.INITIALIZATION_PROGRESS,
            AgentExecutionActivityKind.INITIALIZATION_STALL_SUSPECTED,
            AgentExecutionActivityKind.INITIALIZATION_STALL_RECOVERED,
            AgentExecutionActivityKind.INITIALIZATION_STALLED,
        }:
            raise ValueError(
                "initialization checkpoints belong only to initialization readiness"
            )
        return self


AgentExecutionActivityHandler = Callable[[AgentExecutionActivity], None]


def validate_role_artifact_kind(role: AgentRole, kind: ArtifactKind) -> None:
    """Require one of the output contracts assigned to an executable role."""

    expected = ROLE_ARTIFACT_KINDS.get(role)
    if expected is None:
        raise ValueError(f"role {role.value} has no implemented artifact output")
    if kind not in expected:
        allowed = ", ".join(sorted(item.value for item in expected))
        raise ValueError(
            f"role {role.value} cannot produce {kind.value}; expected {allowed}"
        )


def stable_session_key(
    *,
    run_id: str,
    role: AgentRole,
    iteration: int,
    expected_kind: ArtifactKind,
) -> str:
    """Build the deterministic, role-scoped OpenClaw session key for one phase."""

    return stable_agent_session_key(
        run_id=run_id,
        agent_id=role.value,
        iteration=iteration,
        expected_kind=expected_kind,
    )


def stable_agent_session_key(
    *,
    run_id: str,
    agent_id: str,
    iteration: int,
    expected_kind: ArtifactKind,
) -> str:
    """Build one deterministic session key for a run-scoped Agent invocation."""

    return (
        f"agent:{agent_id}:"
        f"sat-{run_id}-i{iteration}-{expected_kind.value.replace('_', '-')}"
    )


class AgentExecutionRequest(BaseModel):
    """Complete input to one Agent execution adapter invocation.

    ``timeout_seconds`` is a wall-clock limit only when it is positive.  Zero
    explicitly disables the OpenClaw Agent runtime timeout so product work is
    governed by provider stream liveness and an optional user-approved run
    deadline instead of a hidden per-Agent duration budget.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    team_id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    iteration: int = Field(ge=1)
    role: AgentRole | None = None
    agent_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    capability: AgentCapability
    expected_kind: ArtifactKind
    prompt: str = Field(min_length=1)
    timeout_seconds: int = Field(ge=0)
    model: str | None = None
    submission_contract: AgentSubmissionContract | None = None

    @model_validator(mode="before")
    @classmethod
    def populate_legacy_identity(cls, value: object) -> object:
        """Expand legacy role-only callers into explicit runtime identity."""

        if not isinstance(value, dict):
            return value
        payload = dict(value)
        role_value = payload.get("role")
        if role_value is None:
            return payload
        try:
            role = AgentRole(role_value)
        except ValueError:
            return payload
        payload.setdefault("agent_id", role.value)
        payload.setdefault("capability", capability_for_legacy_role(role).value)
        return payload

    @field_validator("prompt")
    @classmethod
    def require_nonblank_prompt(cls, value: str) -> str:
        """Reject messages that OpenClaw would treat as empty."""

        if not value.strip():
            raise ValueError("Agent prompts must not be blank")
        return value

    @field_validator("model")
    @classmethod
    def clean_optional_model(cls, value: str | None) -> str | None:
        """Normalize a model override without inventing a default."""

        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("model override must not be blank")
        return cleaned

    @model_validator(mode="after")
    def validate_output_contract(self) -> Self:
        """Keep run identity, capability, and response schema coherent."""

        if self.role is not None:
            if self.agent_id != self.role.value:
                raise ValueError("legacy role identity must match its Agent ID")
            if self.capability is not capability_for_legacy_role(self.role):
                raise ValueError("legacy role and Agent capability are inconsistent")
            validate_role_artifact_kind(self.role, self.expected_kind)
        elif self.expected_kind is not expected_output_for_capability(self.capability):
            raise ValueError(
                f"Agent capability {self.capability.value} cannot produce "
                f"{self.expected_kind.value}; expected "
                f"{expected_output_for_capability(self.capability).value}"
            )
        if self.capability is AgentCapability.PLANNING and self.iteration != 1:
            raise ValueError("the implementation plan is produced only in iteration 1")
        return self

    @property
    def session_key(self) -> str:
        """Return the stable OpenClaw session identity for this phase."""

        return stable_agent_session_key(
            run_id=self.run_id,
            agent_id=self.agent_id,
            iteration=self.iteration,
            expected_kind=self.expected_kind,
        )


class AgentTokenUsage(BaseModel):
    """Provider-independent token counters reported by OpenClaw."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cache_read_tokens: int | None = Field(default=None, ge=0)
    cache_write_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)


class AgentExecutionTelemetry(BaseModel):
    """Raw, attributable evidence for one Agent process invocation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: AgentRole | None = None
    agent_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    capability: AgentCapability
    session_key: str = Field(min_length=1)
    command: tuple[str, ...] = Field(min_length=1)
    started_at: datetime
    finished_at: datetime
    duration_ms: int = Field(ge=0)
    openclaw_duration_ms: int | None = Field(default=None, ge=0)
    exit_code: int | None = None
    timed_out: bool = False
    interrupted: bool = False
    stdout: str = ""
    stderr: str = ""
    openclaw_run_id: str | None = None
    session_id: str | None = None
    provider: str | None = None
    model: str | None = None
    usage: AgentTokenUsage | None = None
    provider_liveness: ProviderLivenessEvidence | None = None
    invocation_lifecycle: InvocationLifecycleEvidence | None = None
    tool_evidence_status: AgentToolEvidenceStatus = AgentToolEvidenceStatus.NOT_CAPTURED
    session_transcript_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    session_record_count: int | None = Field(default=None, ge=1, le=4096)
    tool_calls: tuple[AgentToolCallEvidence, ...] = ()
    tool_evidence_error: str | None = Field(default=None, min_length=1, max_length=2000)

    @model_validator(mode="before")
    @classmethod
    def populate_legacy_identity(cls, value: object) -> object:
        """Keep existing telemetry constructors compatible and explicit."""

        if not isinstance(value, dict):
            return value
        payload = dict(value)
        role_value = payload.get("role")
        if role_value is None:
            return payload
        try:
            role = AgentRole(role_value)
        except ValueError:
            return payload
        payload.setdefault("agent_id", role.value)
        payload.setdefault("capability", capability_for_legacy_role(role).value)
        return payload

    @field_validator("started_at", "finished_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        """Persist timezone-aware telemetry in UTC."""

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("execution timestamps must include a timezone")
        return value.astimezone(UTC)

    @field_validator(
        "openclaw_run_id",
        "session_id",
        "provider",
        "model",
        "tool_evidence_error",
    )
    @classmethod
    def clean_optional_text(cls, value: str | None) -> str | None:
        """Turn empty external metadata into an explicit absence."""

        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @model_validator(mode="after")
    def validate_process_outcome(self) -> Self:
        """Represent either an external process or OpenClaw-declared timeout."""

        if self.role is not None and (
            self.agent_id != self.role.value
            or self.capability is not capability_for_legacy_role(self.role)
        ):
            raise ValueError("telemetry legacy role identity is inconsistent")
        if self.finished_at < self.started_at:
            raise ValueError("execution cannot finish before it starts")
        if self.timed_out and self.interrupted:
            raise ValueError("an Agent execution cannot be timed out and interrupted")
        if (
            self.provider_liveness is not None
            and self.provider_liveness.stalled
            and (self.timed_out or self.interrupted)
        ):
            raise ValueError(
                "provider-stalled telemetry cannot also be timed out or interrupted"
            )
        validate_tool_evidence_collection(
            status=self.tool_evidence_status,
            transcript_sha256=self.session_transcript_sha256,
            record_count=self.session_record_count,
            tool_calls=self.tool_calls,
            error=self.tool_evidence_error,
        )
        return self


class AgentExecutionResult(BaseModel):
    """Adapter result before untrusted semantic values become an artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: AgentExecutionStatus
    response_text: str | None = None
    error: str | None = None
    telemetry: AgentExecutionTelemetry
    semantic_submission: AgentSemanticSubmission | None = None
    submission_evidence: AgentSubmissionEvidence | None = None

    @field_validator("response_text", "error")
    @classmethod
    def clean_optional_text(cls, value: str | None) -> str | None:
        """Keep absence distinct from a blank external response."""

        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        """Bind successful text or typed submission to the process outcome."""

        if self.status is AgentExecutionStatus.COMPLETED:
            if self.response_text is None and self.semantic_submission is None:
                raise ValueError(
                    "completed Agent execution requires response text or a semantic "
                    "submission"
                )
            if self.error is not None:
                raise ValueError("completed Agent execution cannot contain an error")
            if self.telemetry.timed_out or self.telemetry.exit_code != 0:
                raise ValueError(
                    "completed Agent execution requires a zero process exit"
                )
        else:
            if self.error is None:
                raise ValueError("unsuccessful Agent execution requires an error")
            if self.response_text is not None:
                raise ValueError(
                    "unsuccessful Agent execution cannot expose response text"
                )
            if self.semantic_submission is not None:
                raise ValueError(
                    "unsuccessful Agent execution cannot expose a semantic submission"
                )
        if self.semantic_submission is not None:
            if self.submission_evidence != self.semantic_submission.evidence:
                raise ValueError(
                    "semantic submission and submission evidence must match"
                )
        elif (
            self.submission_evidence is not None
            and self.submission_evidence.status is AgentSubmissionStatus.ACCEPTED
        ):
            raise ValueError(
                "accepted submission evidence requires its semantic payload"
            )
        if self.status is AgentExecutionStatus.TIMED_OUT:
            if not self.telemetry.timed_out:
                raise ValueError("timed-out result requires timed-out telemetry")
        elif self.telemetry.timed_out:
            raise ValueError("only timed-out results may contain timed-out telemetry")
        if self.status is AgentExecutionStatus.PROVIDER_STALLED:
            if (
                self.telemetry.provider_liveness is None
                or not self.telemetry.provider_liveness.stalled
            ):
                raise ValueError(
                    "provider-stalled result requires provider liveness evidence"
                )
        elif (
            self.telemetry.provider_liveness is not None
            and self.telemetry.provider_liveness.stalled
        ):
            raise ValueError(
                "only provider-stalled results may contain terminal stall evidence"
            )
        if self.status is AgentExecutionStatus.INTERRUPTED:
            if not self.telemetry.interrupted:
                raise ValueError("interrupted result requires interrupted telemetry")
        elif self.telemetry.interrupted:
            raise ValueError(
                "only interrupted results may contain interrupted telemetry"
            )
        return self


@runtime_checkable
class AgentExecutor(Protocol):
    """Replaceable synchronous execution boundary used by the controller."""

    def execute(
        self,
        request: AgentExecutionRequest,
        *,
        activity_handler: AgentExecutionActivityHandler | None = None,
    ) -> AgentExecutionResult:
        """Execute one bounded Agent turn without advancing workflow state."""


class _OpenClawResponse(BaseModel):
    """Normalized reply payloads and metadata from one OpenClaw response."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    visible_texts: tuple[str, ...]
    has_error_payload: bool = False
    provider_failed: bool = False
    declared_timeout: bool = False
    openclaw_run_id: str | None = None
    session_id: str | None = None
    provider: str | None = None
    model: str | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    usage: AgentTokenUsage | None = None


def _optional_nonblank(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _optional_nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _parse_usage(value: object) -> AgentTokenUsage | None:
    if not isinstance(value, dict):
        return None
    fields = {
        "input_tokens": _optional_nonnegative_int(value.get("input")),
        "output_tokens": _optional_nonnegative_int(value.get("output")),
        "cache_read_tokens": _optional_nonnegative_int(value.get("cacheRead")),
        "cache_write_tokens": _optional_nonnegative_int(value.get("cacheWrite")),
        "reasoning_tokens": _optional_nonnegative_int(value.get("reasoningTokens")),
        "total_tokens": _optional_nonnegative_int(value.get("total")),
    }
    if all(item is None for item in fields.values()):
        return None
    return AgentTokenUsage(**fields)


def _canonical_model_reference(
    *,
    provider: str | None,
    model: str | None,
) -> str | None:
    """Normalize OpenClaw's split provider/model metadata for run comparison."""

    if model is None or provider is None or model.startswith(f"{provider}/"):
        return model
    return f"{provider}/{model}"


_OPENCLAW_TIMEOUT_PREFIX = "Request timed out before a response was generated."
_OPENCLAW_PROVIDER_FAILURE_TEXT = "LLM request failed."
_OPENCLAW_DIAGNOSTIC_PREFIXES = ("⚠️ 🛠️ Exec failed:",)


def _parse_openclaw_payload(stdout: str) -> _OpenClawResponse:
    """Parse local or Gateway JSON emitted by ``openclaw agent --json``."""

    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise ValueError("OpenClaw stdout is not one JSON object") from error
    if not isinstance(envelope, dict):
        raise ValueError("OpenClaw response must be a JSON object")

    openclaw_run_id: str | None = None
    if "payloads" in envelope:
        result = envelope
    else:
        status = envelope.get("status")
        if status != "ok":
            raise ValueError(
                f"OpenClaw response did not complete successfully: {status!r}"
            )
        result = envelope.get("result")
        if not isinstance(result, dict):
            raise ValueError("OpenClaw response is missing result metadata")
        openclaw_run_id = _optional_nonblank(envelope.get("runId"))

    payloads = result.get("payloads")
    if not isinstance(payloads, list):
        raise ValueError("OpenClaw response is missing reply payloads")

    visible: list[str] = []
    has_error_payload = False
    saw_provider_failure = False
    for payload in payloads:
        if not isinstance(payload, dict):
            raise ValueError("OpenClaw reply payload must be an object")
        if payload.get("isError") is True:
            has_error_payload = True
        if payload.get("isReasoning") is True or payload.get("isCommentary") is True:
            continue
        text = payload.get("text")
        if isinstance(text, str) and text.strip():
            cleaned = text.strip()
            if cleaned == _OPENCLAW_PROVIDER_FAILURE_TEXT:
                saw_provider_failure = True
                continue
            if cleaned.startswith(_OPENCLAW_DIAGNOSTIC_PREFIXES):
                continue
            visible.append(cleaned)

    meta = result.get("meta")
    if meta is None:
        meta = {}
    if not isinstance(meta, dict):
        raise ValueError("OpenClaw result metadata must be an object")
    agent_meta = meta.get("agentMeta")
    if agent_meta is None:
        agent_meta = {}
    if not isinstance(agent_meta, dict):
        raise ValueError("OpenClaw Agent metadata must be an object")

    provider = _optional_nonblank(agent_meta.get("provider"))
    model = _optional_nonblank(agent_meta.get("model"))

    return _OpenClawResponse(
        visible_texts=tuple(visible),
        has_error_payload=has_error_payload,
        provider_failed=saw_provider_failure and not visible,
        declared_timeout=any(
            text.startswith(_OPENCLAW_TIMEOUT_PREFIX) for text in visible
        ),
        openclaw_run_id=openclaw_run_id,
        session_id=_optional_nonblank(agent_meta.get("sessionId")),
        provider=provider,
        model=_canonical_model_reference(provider=provider, model=model),
        duration_ms=_optional_nonnegative_int(meta.get("durationMs")),
        usage=_parse_usage(agent_meta.get("usage")),
    )


WallClock = Callable[[], datetime]
MonotonicClock = Callable[[], float]
ProcessRunner = Callable[..., subprocess.CompletedProcess[str]]
ActiveProcess = tuple[AgentExecutionRequest, subprocess.Popen[str]]


def _system_clock() -> datetime:
    return datetime.now(UTC)


def _decode_process_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _consume_private_raw_stream(path: Path) -> bool:
    """Observe and discard a private raw-stream batch without reading content."""

    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError("raw stream is not a regular file")
        if metadata.st_uid != os.geteuid():
            raise OSError("raw stream owner changed")
        if metadata.st_size == 0:
            return False
        os.ftruncate(descriptor, 0)
        return True
    finally:
        os.close(descriptor)


class _InvocationLifecycleRecorder:
    """Single execution-adapter authority for live phases and terminal evidence."""

    _PHASE_ACTIVITY: ClassVar[dict[InvocationPhase, AgentExecutionActivityKind]] = {
        InvocationPhase.LAUNCHED: AgentExecutionActivityKind.INVOCATION_LAUNCHED,
        InvocationPhase.INITIALIZING: (
            AgentExecutionActivityKind.INVOCATION_INITIALIZING
        ),
        InvocationPhase.PROVIDER_WAIT: (
            AgentExecutionActivityKind.INVOCATION_PROVIDER_WAIT
        ),
        InvocationPhase.TOOL_ACTIVE: AgentExecutionActivityKind.INVOCATION_TOOL_ACTIVE,
        InvocationPhase.STOPPING: AgentExecutionActivityKind.INVOCATION_STOPPING,
        InvocationPhase.COLLECTING_EVIDENCE: (
            AgentExecutionActivityKind.INVOCATION_COLLECTING_EVIDENCE
        ),
        InvocationPhase.STOPPED: AgentExecutionActivityKind.INVOCATION_STOPPED,
    }

    def __init__(
        self,
        *,
        request: AgentExecutionRequest,
        started_monotonic: float,
        process_grace_seconds: float,
        initialization_policy: InitializationLivenessPolicy,
        activity_handler: AgentExecutionActivityHandler | None,
        monotonic: MonotonicClock,
    ) -> None:
        self.request = request
        self.started_monotonic = started_monotonic
        self.process_grace_seconds = process_grace_seconds
        self.initialization_policy = initialization_policy
        self.activity_handler = activity_handler
        self.monotonic = monotonic
        self._lock = threading.RLock()
        self.transitions: list[InvocationLifecycleTransition] = []
        self.phase: InvocationPhase | None = None
        self.stop_reason: InvocationStopReason | None = None
        self.stop_started_monotonic: float | None = None
        self.process_started = False
        self.process_group_targeted = False
        self.terminate_sent = False
        self.kill_sent = False
        self.process_lease_released = True
        self.initialization_evidence = InitializationLivenessEvidence(
            mode="unavailable",
            policy_source=initialization_policy.source,
            no_progress_seconds=initialization_policy.no_progress_seconds,
            stall_grace_seconds=initialization_policy.stall_grace_seconds,
            degradation_reason="subprocess initialization was not observed",
        )
        self.transition(
            InvocationPhase.LAUNCHED,
            now=started_monotonic,
            action="Controller handed the invocation to the execution adapter",
        )

    def elapsed_ms(self, now: float | None = None) -> int:
        current = self.monotonic() if now is None else now
        return max(0, round((current - self.started_monotonic) * 1000))

    def transition(
        self,
        phase: InvocationPhase,
        *,
        now: float | None = None,
        reason: InvocationStopReason | None = None,
        checkpoint: InitializationCheckpoint | None = None,
        action: str | None = None,
    ) -> None:
        current = self.monotonic() if now is None else now
        with self._lock:
            candidate = InvocationLifecycleTransition(
                sequence=len(self.transitions) + 1,
                phase=phase,
                elapsed_ms=self.elapsed_ms(current),
                stop_reason=reason,
                initialization_checkpoint=checkpoint,
                action=action,
            )
            if self.transitions:
                previous = self.transitions[-1]
                if (
                    previous.phase is candidate.phase
                    and previous.stop_reason is candidate.stop_reason
                    and previous.initialization_checkpoint
                    is candidate.initialization_checkpoint
                    and previous.action == candidate.action
                ):
                    return
            self.transitions.append(candidate)
            self.phase = phase
        self._emit_phase(candidate)

    def process_launched(self, *, now: float, process_group_targeted: bool) -> None:
        with self._lock:
            self.process_started = True
            self.process_group_targeted = process_group_targeted
            self.process_lease_released = False
        self.transition(
            InvocationPhase.INITIALIZING,
            now=now,
            checkpoint=InitializationCheckpoint.PROCESS_LAUNCHED,
            action=(
                "OpenClaw process launched; waiting for an attributable current turn"
            ),
        )

    def initialization_progress(
        self,
        checkpoint: InitializationCheckpoint,
        *,
        now: float,
    ) -> None:
        self.transition(
            InvocationPhase.INITIALIZING,
            now=now,
            checkpoint=checkpoint,
            action=f"OpenClaw initialization reached {checkpoint.value}",
        )
        self.emit_initialization(
            AgentExecutionActivityKind.INITIALIZATION_PROGRESS,
            now=now,
            checkpoint=checkpoint,
        )

    def provider_ready(
        self,
        checkpoint: InitializationCheckpoint,
        *,
        now: float,
    ) -> None:
        with self._lock:
            if self.phase in {
                InvocationPhase.PROVIDER_WAIT,
                InvocationPhase.TOOL_ACTIVE,
                InvocationPhase.STOPPING,
                InvocationPhase.COLLECTING_EVIDENCE,
                InvocationPhase.STOPPED,
            }:
                return
        self.transition(
            InvocationPhase.PROVIDER_WAIT,
            now=now,
            checkpoint=checkpoint,
            action="Provider activity lease is now attributable to this invocation",
        )

    def tool_state(self, active_tool_count: int, *, now: float) -> None:
        with self._lock:
            if self.phase in {
                InvocationPhase.STOPPING,
                InvocationPhase.COLLECTING_EVIDENCE,
                InvocationPhase.STOPPED,
            }:
                return
        if active_tool_count > 0:
            self.transition(
                InvocationPhase.TOOL_ACTIVE,
                now=now,
                action=f"{active_tool_count} attributable tool operation(s) active",
            )
        else:
            self.transition(
                InvocationPhase.PROVIDER_WAIT,
                now=now,
                action="Attributable tools completed; waiting for provider activity",
            )

    def request_stop(
        self,
        reason: InvocationStopReason,
        *,
        now: float,
        action: str,
    ) -> bool:
        with self._lock:
            if self.stop_reason is not None:
                return False
            self.stop_reason = reason
            self.stop_started_monotonic = now
        self.transition(
            InvocationPhase.STOPPING,
            now=now,
            reason=reason,
            action=action,
        )
        return True

    def mark_terminate_sent(self) -> None:
        with self._lock:
            self.terminate_sent = True

    def mark_kill_sent(self) -> None:
        with self._lock:
            self.kill_sent = True

    def mark_process_lease_released(self) -> None:
        with self._lock:
            self.process_lease_released = True

    def set_initialization_evidence(
        self, evidence: InitializationLivenessEvidence
    ) -> None:
        with self._lock:
            self.initialization_evidence = evidence

    def finalize(
        self,
        *,
        reason: InvocationStopReason,
        telemetry: AgentExecutionTelemetry,
        submission_evidence: AgentSubmissionEvidence | None,
    ) -> InvocationLifecycleEvidence:
        now = self.started_monotonic + (telemetry.duration_ms / 1000)
        with self._lock:
            owned_reason = self.stop_reason or reason
            if self.stop_reason is None:
                self.stop_reason = owned_reason
        self.transition(
            InvocationPhase.COLLECTING_EVIDENCE,
            now=now,
            reason=owned_reason,
            action=(
                "Collected stdout/stderr and checked attributable session and "
                "submission evidence"
            ),
        )
        self.transition(
            InvocationPhase.STOPPED,
            now=now,
            reason=owned_reason,
            action="Exact invocation process outcome and cleanup are known",
        )
        returncode = telemetry.exit_code if self.process_started else None
        exit_code = returncode if returncode is None or returncode >= 0 else None
        process_signal = (
            -returncode if returncode is not None and returncode < 0 else None
        )
        shutdown = InvocationShutdownEvidence(
            reason=owned_reason,
            shutdown_grace_seconds=self.process_grace_seconds,
            process_started=self.process_started,
            process_group_targeted=(
                self.process_group_targeted and self.terminate_sent
            ),
            terminate_sent=self.terminate_sent,
            kill_sent=self.kill_sent,
            exit_code=exit_code,
            signal=process_signal,
            stdout_collected=True,
            stderr_collected=True,
            session_evidence_status=telemetry.tool_evidence_status.value,
            submission_evidence_status=(
                "not_configured"
                if submission_evidence is None
                else submission_evidence.status.value
            ),
            process_lease_released=self.process_lease_released,
            cleanup_completed=(
                (not self.process_started or telemetry.exit_code is not None)
                and self.process_lease_released
            ),
        )
        return InvocationLifecycleEvidence(
            transitions=tuple(self.transitions),
            initialization=self.initialization_evidence,
            shutdown=shutdown,
        )

    def emit_initialization(
        self,
        kind: AgentExecutionActivityKind,
        *,
        now: float,
        checkpoint: InitializationCheckpoint | None = None,
        inactivity_ms: int = 0,
    ) -> None:
        self._emit(
            AgentExecutionActivity(
                kind=kind,
                agent_id=self.request.agent_id,
                session_key=self.request.session_key,
                model=self.request.model,
                elapsed_ms=self.elapsed_ms(now),
                inactivity_ms=inactivity_ms,
                silence_seconds=self.initialization_policy.no_progress_seconds,
                stall_grace_seconds=self.initialization_policy.stall_grace_seconds,
                policy_source=self.initialization_policy.source,
                initialization_checkpoint=checkpoint,
                degradation_reason=(
                    self.initialization_evidence.degradation_reason
                    if kind
                    is AgentExecutionActivityKind.INITIALIZATION_LIVENESS_DEGRADED
                    else None
                ),
            )
        )

    def _emit_phase(self, transition: InvocationLifecycleTransition) -> None:
        self._emit(
            AgentExecutionActivity(
                kind=self._PHASE_ACTIVITY[transition.phase],
                agent_id=self.request.agent_id,
                session_key=self.request.session_key,
                model=self.request.model,
                elapsed_ms=transition.elapsed_ms,
                invocation_phase=transition.phase,
                stop_reason=transition.stop_reason,
                initialization_checkpoint=transition.initialization_checkpoint,
                shutdown_grace_seconds=(
                    self.process_grace_seconds
                    if transition.phase is InvocationPhase.STOPPING
                    else None
                ),
                action=transition.action,
            )
        )

    def _emit(self, activity: AgentExecutionActivity) -> None:
        if self.activity_handler is None:
            return
        try:
            self.activity_handler(activity)
        except Exception:
            return


class _InitializationLivenessMonitor:
    """Enforce finite exact progress before provider liveness can begin."""

    def __init__(
        self,
        *,
        request: AgentExecutionRequest,
        policy: InitializationLivenessPolicy,
        state_dir: Path | None,
        started_monotonic: float,
        lifecycle: _InvocationLifecycleRecorder,
    ) -> None:
        self.request = request
        self.policy = policy
        self.state_dir = state_dir
        self.started_monotonic = started_monotonic
        self.last_progress = started_monotonic
        self.lifecycle = lifecycle
        self.checkpoints: list[InitializationCheckpoint] = [
            InitializationCheckpoint.PROCESS_LAUNCHED
        ]
        self.suspected = False
        self.ready = False
        self.stalled = False
        self.stall_suspected_count = 0
        self.stall_recovered_count = 0
        self.maximum_no_progress_ms = 0
        self.degradation_reason: str | None = None
        if state_dir is None:
            self.degradation_reason = (
                "OpenClaw state directory is unavailable for initialization observation"
            )
            self.lifecycle.set_initialization_evidence(self.evidence())
            self.lifecycle.emit_initialization(
                AgentExecutionActivityKind.INITIALIZATION_LIVENESS_DEGRADED,
                now=started_monotonic,
            )

    def poll(self, now: float) -> bool:
        """Return true only after an attributable initialization stall."""

        if self.ready or self.stalled or self.degradation_reason is not None:
            return self.stalled
        assert self.state_dir is not None
        try:
            observation = inspect_openclaw_initialization(
                state_dir=self.state_dir,
                agent_id=self.request.agent_id,
                session_key=self.request.session_key,
                prompt=self.request.prompt,
            )
        except OpenClawSessionEvidenceError:
            self.degradation_reason = (
                "OpenClaw initialization progress could not be attributed"
            )
            self.lifecycle.set_initialization_evidence(self.evidence())
            self.lifecycle.emit_initialization(
                AgentExecutionActivityKind.INITIALIZATION_LIVENESS_DEGRADED,
                now=now,
            )
            return False
        if observation is not None:
            self._advance(observation.checkpoint, now=now)
        inactive = max(0.0, now - self.last_progress)
        self.maximum_no_progress_ms = max(
            self.maximum_no_progress_ms,
            round(inactive * 1000),
        )
        if self.ready:
            self.lifecycle.set_initialization_evidence(self.evidence())
            return False
        if not self.suspected and inactive >= self.policy.suspect_after_seconds:
            self.suspected = True
            self.stall_suspected_count += 1
            self.lifecycle.set_initialization_evidence(self.evidence())
            self.lifecycle.emit_initialization(
                AgentExecutionActivityKind.INITIALIZATION_STALL_SUSPECTED,
                now=now,
                checkpoint=self.checkpoints[-1],
                inactivity_ms=round(inactive * 1000),
            )
        if self.suspected and inactive >= self.policy.no_progress_seconds:
            self.stalled = True
            self.lifecycle.set_initialization_evidence(self.evidence())
            self.lifecycle.emit_initialization(
                AgentExecutionActivityKind.INITIALIZATION_STALLED,
                now=now,
                checkpoint=self.checkpoints[-1],
                inactivity_ms=round(inactive * 1000),
            )
            return True
        self.lifecycle.set_initialization_evidence(self.evidence())
        return False

    def provider_stream_ready(self, now: float) -> None:
        """Accept a private stream as an attributable provider-start boundary."""

        if self.ready:
            return
        self._advance(InitializationCheckpoint.PROVIDER_STREAM, now=now)

    def finalize(self, now: float) -> InitializationLivenessEvidence:
        if not self.ready and self.degradation_reason is None and not self.stalled:
            self.poll(now)
        evidence = self.evidence()
        self.lifecycle.set_initialization_evidence(evidence)
        return evidence

    def _advance(self, checkpoint: InitializationCheckpoint, *, now: float) -> None:
        order = list(InitializationCheckpoint)
        if checkpoint in self.checkpoints:
            return
        if order.index(checkpoint) < order.index(self.checkpoints[-1]):
            self.degradation_reason = "OpenClaw initialization progress moved backwards"
            self.lifecycle.set_initialization_evidence(self.evidence())
            self.lifecycle.emit_initialization(
                AgentExecutionActivityKind.INITIALIZATION_LIVENESS_DEGRADED,
                now=now,
            )
            return
        implied = (
            (checkpoint,)
            if checkpoint is InitializationCheckpoint.PROVIDER_STREAM
            else tuple(
                candidate
                for candidate in order
                if candidate is not InitializationCheckpoint.PROVIDER_STREAM
                and order.index(self.checkpoints[-1])
                < order.index(candidate)
                <= order.index(checkpoint)
            )
        )
        for candidate in implied:
            self.checkpoints.append(candidate)
            self.lifecycle.initialization_progress(candidate, now=now)
        self.last_progress = now
        if self.suspected:
            self.suspected = False
            self.stall_recovered_count += 1
            self.lifecycle.set_initialization_evidence(self.evidence())
            self.lifecycle.emit_initialization(
                AgentExecutionActivityKind.INITIALIZATION_STALL_RECOVERED,
                now=now,
                checkpoint=checkpoint,
            )
        if checkpoint in {
            InitializationCheckpoint.CURRENT_TURN,
            InitializationCheckpoint.PROVIDER_STREAM,
        }:
            self.ready = True
            self.lifecycle.provider_ready(checkpoint, now=now)

    def evidence(self) -> InitializationLivenessEvidence:
        return InitializationLivenessEvidence(
            mode=(
                "unavailable"
                if self.state_dir is None
                else ("degraded" if self.degradation_reason is not None else "enforced")
            ),
            policy_source=self.policy.source,
            no_progress_seconds=self.policy.no_progress_seconds,
            stall_grace_seconds=self.policy.stall_grace_seconds,
            checkpoints=tuple(self.checkpoints),
            stall_suspected_count=self.stall_suspected_count,
            stall_recovered_count=self.stall_recovered_count,
            maximum_no_progress_ms=self.maximum_no_progress_ms,
            stalled=self.stalled,
            degradation_reason=self.degradation_reason,
        )


class _ProviderLivenessMonitor:
    """Renew a silence lease from trusted, content-free OpenClaw activity."""

    def __init__(
        self,
        *,
        request: AgentExecutionRequest,
        policy: ProviderLivenessPolicy,
        raw_stream_path: Path,
        state_dir: Path,
        started_monotonic: float,
        activity_handler: AgentExecutionActivityHandler | None,
        initialization_monitor: _InitializationLivenessMonitor,
        lifecycle: _InvocationLifecycleRecorder,
    ) -> None:
        self.request = request
        self.policy = policy
        self.raw_stream_path = raw_stream_path
        self.state_dir = state_dir
        self.started_monotonic = started_monotonic
        self.activity_handler = activity_handler
        self.initialization_monitor = initialization_monitor
        self.lifecycle = lifecycle
        self.last_activity: float | None = None
        self.lease_start_source: str | None = None
        self.last_provider_report = float("-inf")
        self.previous_trusted_records = 0
        self.previous_tool_started = 0
        self.previous_tool_completed = 0
        self.active_tool_count = 0
        self.raw_stream_observed = False
        self.session_observed = False
        self.provider_activity_observations = 0
        self.stall_suspected_count = 0
        self.stall_recovered_count = 0
        self.maximum_inactivity_ms = 0
        self.suspected = False
        self.stalled = False
        self.degradation_reason: str | None = None

    def poll(self, now: float, *, enforce_stall: bool = True) -> bool:
        """Observe activity and optionally enforce the live silence boundary."""

        if self.last_activity is not None:
            inactivity_before_poll = max(0.0, now - self.last_activity)
            self.maximum_inactivity_ms = max(
                self.maximum_inactivity_ms,
                round(inactivity_before_poll * 1000),
            )
        trusted_activity = False
        try:
            raw_activity = _consume_private_raw_stream(self.raw_stream_path)
        except OSError:
            self._degrade("private provider-stream observer became unavailable", now)
            raw_activity = False
        if raw_activity:
            trusted_activity = True
            self._start_lease(now, source="provider_stream")
            self.raw_stream_observed = True
            self.provider_activity_observations += 1

        try:
            session = inspect_openclaw_session_activity(
                state_dir=self.state_dir,
                agent_id=self.request.agent_id,
                session_key=self.request.session_key,
                prompt=self.request.prompt,
            )
        except OpenClawSessionEvidenceError:
            self._degrade("OpenClaw session activity could not be attributed", now)
            session = None
        if session is not None:
            first_session_observation = not self.session_observed
            self.session_observed = True
            if first_session_observation:
                # The exact current prompt in OpenClaw's current-turn record is
                # the earliest attributable checkpoint available to the SAT
                # subprocess adapter. Process/interpreter startup before this
                # point is not provider silence.
                self._start_lease(now, source="current_turn")
                trusted_activity = True
            if session.trusted_record_count > self.previous_trusted_records:
                trusted_activity = True
            started_delta = session.tool_started_count - self.previous_tool_started
            completed_delta = (
                session.tool_completed_count - self.previous_tool_completed
            )
            if started_delta < 0 or completed_delta < 0:
                self._degrade("OpenClaw session activity moved backwards", now)
            else:
                for _ in range(started_delta):
                    self._emit(AgentExecutionActivityKind.TOOL_STARTED, now)
                for _ in range(completed_delta):
                    self._emit(AgentExecutionActivityKind.TOOL_COMPLETED, now)
            self.previous_trusted_records = session.trusted_record_count
            self.previous_tool_started = session.tool_started_count
            self.previous_tool_completed = session.tool_completed_count
            self.active_tool_count = session.active_tool_count
            if started_delta > 0 or completed_delta > 0:
                self.lifecycle.tool_state(self.active_tool_count, now=now)

        if trusted_activity:
            self.last_activity = now
            if self.suspected:
                self.suspected = False
                self.stall_recovered_count += 1
                self._emit(AgentExecutionActivityKind.STALL_RECOVERED, now)
            if raw_activity and (
                self.provider_activity_observations == 1
                or now - self.last_provider_report >= PROVIDER_ACTIVITY_REPORT_SECONDS
            ):
                self.last_provider_report = now
                self._emit(AgentExecutionActivityKind.PROVIDER_STREAM, now)

        if (
            not enforce_stall
            or self.degradation_reason is not None
            or self.active_tool_count > 0
        ):
            return False
        if self.last_activity is None:
            return False
        inactive = max(0.0, now - self.last_activity)
        if not self.suspected and inactive >= self.policy.suspect_after_seconds:
            if not self.session_observed:
                self._degrade(
                    "OpenClaw session observer was not ready before the stall probe",
                    now,
                )
                return False
            self.suspected = True
            self.stall_suspected_count += 1
            self._emit(AgentExecutionActivityKind.STALL_SUSPECTED, now)
        if self.suspected and inactive >= self.policy.silence_seconds:
            self.stalled = True
            self._emit(AgentExecutionActivityKind.PROVIDER_STALLED, now)
            return True
        return False

    def finalize(self, now: float) -> ProviderLivenessEvidence:
        """Collect terminal counters without changing an already-owned outcome."""

        self.poll(now, enforce_stall=False)
        if self.last_activity is None:
            self._degrade(
                "OpenClaw current-turn activity was never attributable",
                now,
            )
        return self.evidence()

    def _start_lease(self, now: float, *, source: str) -> None:
        if self.last_activity is not None:
            return
        self.last_activity = now
        self.lease_start_source = source
        checkpoint = (
            InitializationCheckpoint.PROVIDER_STREAM
            if source == "provider_stream"
            else InitializationCheckpoint.CURRENT_TURN
        )
        if source == "provider_stream":
            self.initialization_monitor.provider_stream_ready(now)
        self.lifecycle.provider_ready(checkpoint, now=now)

    def _degrade(self, reason: str, now: float) -> None:
        if self.degradation_reason is not None:
            return
        self.degradation_reason = reason
        self._emit(AgentExecutionActivityKind.LIVENESS_DEGRADED, now)

    def _emit(self, kind: AgentExecutionActivityKind, now: float) -> None:
        if self.activity_handler is None:
            return
        inactivity = (
            0.0 if self.last_activity is None else max(0.0, now - self.last_activity)
        )
        activity = AgentExecutionActivity(
            kind=kind,
            agent_id=self.request.agent_id,
            session_key=self.request.session_key,
            model=self.request.model,
            elapsed_ms=max(0, round((now - self.started_monotonic) * 1000)),
            trusted_activity_count=(
                self.provider_activity_observations + self.previous_trusted_records
            ),
            active_tool_count=self.active_tool_count,
            completed_tool_count=self.previous_tool_completed,
            inactivity_ms=max(0, round(inactivity * 1000)),
            silence_seconds=self.policy.silence_seconds,
            stall_grace_seconds=self.policy.stall_grace_seconds,
            policy_source=self.policy.source,
            degradation_reason=(
                self.degradation_reason
                if kind is AgentExecutionActivityKind.LIVENESS_DEGRADED
                else None
            ),
        )
        try:
            self.activity_handler(activity)
        except Exception:
            self.degradation_reason = "provider liveness activity could not be reported"

    def evidence(self) -> ProviderLivenessEvidence:
        """Freeze the safe counters after the child process has stopped."""

        return ProviderLivenessEvidence(
            mode="degraded" if self.degradation_reason is not None else "enforced",
            policy_source=self.policy.source,
            silence_seconds=self.policy.silence_seconds,
            stall_grace_seconds=self.policy.stall_grace_seconds,
            lease_started=self.last_activity is not None,
            lease_start_source=self.lease_start_source,
            raw_stream_observed=self.raw_stream_observed,
            session_observed=self.session_observed,
            provider_activity_observations=self.provider_activity_observations,
            tool_started_count=self.previous_tool_started,
            tool_completed_count=self.previous_tool_completed,
            stall_suspected_count=self.stall_suspected_count,
            stall_recovered_count=self.stall_recovered_count,
            maximum_inactivity_ms=self.maximum_inactivity_ms,
            stalled=self.stalled,
            degradation_reason=self.degradation_reason,
        )


class OpenClawSubprocessExecutor:
    """Invoke one OpenClaw Agent turn through a shell-free subprocess.

    Positive request timeouts remain controlled-evaluation limits.  A zero
    request timeout is passed through to OpenClaw as its documented
    no-wall-clock-limit value. SAT separately observes the pinned runtime's
    private raw stream and attributable tool lifecycle to enforce a renewable
    provider-silence lease without limiting productive wall-clock work.
    """

    def __init__(
        self,
        *,
        openclaw_binary: str | Path = "openclaw",
        environment: Mapping[str, str] | None = None,
        local: bool = True,
        process_grace_seconds: float = DEFAULT_PROCESS_SHUTDOWN_GRACE_SECONDS,
        run_deadline_at: datetime | None = None,
        runner: ProcessRunner | None = None,
        clock: WallClock = _system_clock,
        monotonic: MonotonicClock = time.monotonic,
        liveness_poll_seconds: float = DEFAULT_LIVENESS_POLL_SECONDS,
        liveness_policies: Mapping[str, ProviderLivenessPolicy] | None = None,
        initialization_policy: InitializationLivenessPolicy | None = None,
        process_lease_store: ProcessLeaseStore | None = None,
    ) -> None:
        if process_grace_seconds <= 0:
            raise AgentExecutionError("OpenClaw process grace period must be positive")
        binary = str(openclaw_binary)
        if not binary:
            raise AgentExecutionError("OpenClaw binary must not be empty")
        if liveness_poll_seconds <= 0:
            raise AgentExecutionError(
                "provider liveness poll interval must be positive"
            )
        self.openclaw_binary = binary
        self.environment = None if environment is None else dict(environment)
        self.local = local
        self.process_grace_seconds = process_grace_seconds
        if run_deadline_at is not None:
            if run_deadline_at.tzinfo is None or run_deadline_at.utcoffset() is None:
                raise AgentExecutionError("run deadline must include a UTC offset")
            run_deadline_at = run_deadline_at.astimezone(UTC)
        self.run_deadline_at = run_deadline_at
        self.runner = runner
        self.clock = clock
        self.monotonic = monotonic
        self.liveness_poll_seconds = liveness_poll_seconds
        self.process_lease_store = process_lease_store
        self.initialization_policy = initialization_policy or (
            InitializationLivenessPolicy(
                no_progress_seconds=DEFAULT_INITIALIZATION_NO_PROGRESS_SECONDS,
                stall_grace_seconds=DEFAULT_INITIALIZATION_STALL_GRACE_SECONDS,
                source="SAT/OpenClaw attributable initialization checkpoints",
            )
        )
        self._liveness_policies = dict(liveness_policies or {})
        if any(key != policy.model for key, policy in self._liveness_policies.items()):
            raise AgentExecutionError("provider liveness policy keys must match models")
        self._process_lock = threading.Lock()
        self._active_processes: dict[str, ActiveProcess] = {}
        self._active_lifecycles: dict[str, _InvocationLifecycleRecorder] = {}
        self._interrupt_requests: dict[str, InvocationStopReason] = {}

    def register_model_liveness(
        self,
        *,
        model: str,
        local: bool | None,
        provider_request_timeout_seconds: int | None = None,
    ) -> ProviderLivenessPolicy:
        """Register inspected model locality before any invocation starts."""

        policy = resolve_provider_liveness_policy(
            model=model,
            local=local,
            provider_request_timeout_seconds=provider_request_timeout_seconds,
        )
        existing = self._liveness_policies.get(model)
        if existing is not None and existing != policy:
            raise AgentExecutionError(
                f"provider liveness policy changed after registration: {model}"
            )
        self._liveness_policies[model] = policy
        return policy

    def execute(
        self,
        request: AgentExecutionRequest,
        *,
        activity_handler: AgentExecutionActivityHandler | None = None,
    ) -> AgentExecutionResult:
        """Run ``openclaw agent`` and retain all process and usage evidence."""

        started_at = self.clock()
        started_monotonic = self.monotonic()
        lifecycle = _InvocationLifecycleRecorder(
            request=request,
            started_monotonic=started_monotonic,
            process_grace_seconds=self.process_grace_seconds,
            initialization_policy=self.initialization_policy,
            activity_handler=activity_handler,
            monotonic=self.monotonic,
        )
        submission_binding_sha256: str | None = None
        submission_capture: SubmissionFileCapture | None = None
        with tempfile.TemporaryDirectory(prefix="sat-openclaw-") as temporary:
            temporary_path = Path(temporary)
            prompt_path = Path(temporary) / "prompt.md"
            prompt_path.write_text(request.prompt, encoding="utf-8")
            prompt_path.chmod(0o600)
            raw_stream_path = Path(temporary) / "provider-stream.jsonl"
            raw_stream_path.touch(mode=0o600, exist_ok=False)
            submission_environment: dict[str, str] = {}
            submission_output_path: Path | None = None
            if request.submission_contract is not None:
                submission_schema_path = temporary_path / "submission-schema.json"
                submission_schema_path.write_text(
                    request.submission_contract.parameters_schema_json,
                    encoding="utf-8",
                )
                submission_schema_path.chmod(0o600)
                submission_output_path = temporary_path / "semantic-submission.json"
                submission_binding_sha256 = hashlib.sha256(
                    b"\x00".join(
                        (
                            secrets.token_bytes(32),
                            request.session_key.encode("utf-8"),
                            request.submission_contract.schema_sha256.encode("ascii"),
                        )
                    )
                ).hexdigest()
                submission_environment = {
                    "SAT_ARTIFACT_SUBMISSION_SCHEMA_PATH": str(
                        submission_schema_path.resolve()
                    ),
                    "SAT_ARTIFACT_SUBMISSION_OUTPUT_PATH": str(
                        submission_output_path.resolve()
                    ),
                    "SAT_ARTIFACT_SUBMISSION_SCHEMA_SHA256": (
                        request.submission_contract.schema_sha256
                    ),
                    "SAT_ARTIFACT_SUBMISSION_BINDING_SHA256": (
                        submission_binding_sha256
                    ),
                }
            (
                runtime_timeout_seconds,
                process_timeout_seconds,
                deadline_expired,
                timeout_reason,
            ) = self._runtime_timeout(
                request,
                now=started_at,
            )
            command = self._command(
                request,
                prompt_path,
                runtime_timeout_seconds=runtime_timeout_seconds,
            )
            if deadline_expired:
                return self._finalize_lifecycle_result(
                    self._expired_deadline_result(
                        request=request,
                        command=command,
                        started_at=started_at,
                        started_monotonic=started_monotonic,
                    ),
                    lifecycle=lifecycle,
                    reason=InvocationStopReason.RUN_DEADLINE,
                )
            try:
                if self.runner is None:
                    completed, stop_reason, liveness = self._run_interruptible_process(
                        request,
                        command,
                        runtime_timeout_seconds=runtime_timeout_seconds,
                        process_timeout_seconds=process_timeout_seconds,
                        raw_stream_path=raw_stream_path,
                        submission_environment=submission_environment,
                        activity_handler=activity_handler,
                        lifecycle=lifecycle,
                        timeout_reason=timeout_reason,
                    )
                else:
                    runner_kwargs: dict[str, object] = {
                        "check": False,
                        "capture_output": True,
                        "text": True,
                        "encoding": "utf-8",
                        "errors": "replace",
                        "shell": False,
                        "stdin": subprocess.DEVNULL,
                        "env": self._environment(
                            submission_environment=submission_environment
                        ),
                    }
                    if process_timeout_seconds is not None:
                        runner_kwargs["timeout"] = (
                            process_timeout_seconds + self.process_grace_seconds
                        )
                    completed = self.runner(list(command), **runner_kwargs)
                    lifecycle.process_started = True
                    lifecycle.process_lease_released = True
                    stop_reason = None
                    liveness = None
            except subprocess.TimeoutExpired as error:
                return self._finalize_lifecycle_result(
                    self._timeout_result(
                        request=request,
                        command=command,
                        started_at=started_at,
                        started_monotonic=started_monotonic,
                        error=error,
                    ),
                    lifecycle=lifecycle,
                    reason=timeout_reason or InvocationStopReason.EVALUATION_TIMEOUT,
                )
            except OSError as error:
                return self._finalize_lifecycle_result(
                    self._launch_failure_result(
                        request=request,
                        command=command,
                        started_at=started_at,
                        started_monotonic=started_monotonic,
                        error=error,
                    ),
                    lifecycle=lifecycle,
                    reason=InvocationStopReason.LAUNCH_FAILURE,
                )
            if submission_output_path is not None:
                submission_capture = capture_submission_file(submission_output_path)

        stdout = _decode_process_output(completed.stdout)
        stderr = _decode_process_output(completed.stderr)
        if stop_reason in {
            InvocationStopReason.USER_INTERRUPT,
            InvocationStopReason.USER_CANCEL,
        }:
            return self._finalize_lifecycle_result(
                self._interrupted_result(
                    request=request,
                    command=command,
                    started_at=started_at,
                    started_monotonic=started_monotonic,
                    exit_code=completed.returncode,
                    stdout=stdout,
                    stderr=stderr,
                    provider_liveness=liveness,
                    stop_reason=stop_reason,
                ),
                lifecycle=lifecycle,
                reason=stop_reason,
            )
        if stop_reason is InvocationStopReason.INITIALIZATION_STALL:
            return self._finalize_lifecycle_result(
                self._initialization_stalled_result(
                    request=request,
                    command=command,
                    started_at=started_at,
                    started_monotonic=started_monotonic,
                    exit_code=completed.returncode,
                    stdout=stdout,
                    stderr=stderr,
                    provider_liveness=liveness,
                ),
                lifecycle=lifecycle,
                reason=stop_reason,
            )
        if stop_reason is InvocationStopReason.PROVIDER_STALL:
            return self._finalize_lifecycle_result(
                self._provider_stalled_result(
                    request=request,
                    command=command,
                    started_at=started_at,
                    started_monotonic=started_monotonic,
                    exit_code=completed.returncode,
                    stdout=stdout,
                    stderr=stderr,
                    provider_liveness=liveness,
                ),
                lifecycle=lifecycle,
                reason=stop_reason,
            )
        if stop_reason in {
            InvocationStopReason.RUN_DEADLINE,
            InvocationStopReason.EVALUATION_TIMEOUT,
        }:
            timeout_error = subprocess.TimeoutExpired(
                command,
                (process_timeout_seconds or 0) + self.process_grace_seconds,
                output=stdout,
                stderr=stderr,
            )
            return self._finalize_lifecycle_result(
                self._timeout_result(
                    request=request,
                    command=command,
                    started_at=started_at,
                    started_monotonic=started_monotonic,
                    error=timeout_error,
                    exit_code=completed.returncode,
                    provider_liveness=liveness,
                ),
                lifecycle=lifecycle,
                reason=stop_reason,
            )
        if completed.returncode != 0:
            telemetry = self._telemetry(
                request=request,
                command=command,
                started_at=started_at,
                started_monotonic=started_monotonic,
                exit_code=completed.returncode,
                stdout=stdout,
                stderr=stderr,
                provider_liveness=liveness,
            )
            return self._finalize_lifecycle_result(
                AgentExecutionResult(
                    status=AgentExecutionStatus.PROCESS_FAILED,
                    error=f"OpenClaw exited with status {completed.returncode}",
                    telemetry=telemetry,
                ),
                lifecycle=lifecycle,
                reason=InvocationStopReason.PROCESS_FAILURE,
            )

        try:
            payload = _parse_openclaw_payload(stdout)
        except ValueError as error:
            telemetry = self._telemetry(
                request=request,
                command=command,
                started_at=started_at,
                started_monotonic=started_monotonic,
                exit_code=completed.returncode,
                stdout=stdout,
                stderr=stderr,
                provider_liveness=liveness,
            )
            return self._finalize_lifecycle_result(
                AgentExecutionResult(
                    status=AgentExecutionStatus.INVALID_RESPONSE,
                    error=str(error),
                    telemetry=telemetry,
                ),
                lifecycle=lifecycle,
                reason=InvocationStopReason.INVALID_RESPONSE,
            )

        captured_tools, tool_evidence_error = self._capture_tool_evidence(
            request,
            payload,
        )
        declared_provider_stall = (
            payload.declared_timeout
            and liveness is not None
            and liveness.stall_suspected_count > 0
            and not liveness.degradation_reason
            and liveness.maximum_inactivity_ms >= round(liveness.silence_seconds * 1000)
            and (
                runtime_timeout_seconds == 0
                or runtime_timeout_seconds > liveness.silence_seconds
            )
        )
        if declared_provider_stall and not liveness.stalled:
            liveness = liveness.model_copy(update={"stalled": True})
            self._emit_declared_provider_stall(
                request=request,
                activity_handler=activity_handler,
                evidence=liveness,
                elapsed_ms=max(
                    0,
                    round((self.monotonic() - started_monotonic) * 1000),
                ),
            )
        telemetry = self._telemetry(
            request=request,
            command=command,
            started_at=started_at,
            started_monotonic=started_monotonic,
            exit_code=completed.returncode,
            stdout=stdout,
            stderr=stderr,
            payload=payload,
            timed_out=payload.declared_timeout and not declared_provider_stall,
            captured_tools=captured_tools,
            tool_evidence_error=tool_evidence_error,
            provider_liveness=liveness,
        )
        if payload.declared_timeout:
            if declared_provider_stall:
                return self._finalize_lifecycle_result(
                    AgentExecutionResult(
                        status=AgentExecutionStatus.PROVIDER_STALLED,
                        error="OpenClaw reported sustained provider inactivity",
                        telemetry=telemetry,
                    ),
                    lifecycle=lifecycle,
                    reason=InvocationStopReason.PROVIDER_STALL,
                )
            return self._finalize_lifecycle_result(
                AgentExecutionResult(
                    status=AgentExecutionStatus.TIMED_OUT,
                    error="OpenClaw reported an Agent timeout",
                    telemetry=telemetry,
                ),
                lifecycle=lifecycle,
                reason=timeout_reason or InvocationStopReason.EVALUATION_TIMEOUT,
            )
        if payload.provider_failed:
            return self._finalize_lifecycle_result(
                AgentExecutionResult(
                    status=AgentExecutionStatus.PROVIDER_FAILED,
                    error="OpenClaw reported an upstream model-provider failure",
                    telemetry=telemetry,
                ),
                lifecycle=lifecycle,
                reason=InvocationStopReason.PROVIDER_FAILURE,
            )
        if request.submission_contract is not None:
            assert submission_binding_sha256 is not None
            assert submission_capture is not None
            semantic_submission, submission_evidence = validate_submission_capture(
                request.submission_contract,
                binding_sha256=submission_binding_sha256,
                capture=submission_capture,
                tool_calls=(
                    () if captured_tools is None else captured_tools.tool_calls
                ),
                tool_evidence_error=tool_evidence_error,
            )
            if semantic_submission is None:
                assert submission_evidence.diagnostic_code is not None
                return self._finalize_lifecycle_result(
                    AgentExecutionResult(
                        status=AgentExecutionStatus.INVALID_RESPONSE,
                        error=(
                            "typed artifact submission rejected: "
                            f"{submission_evidence.diagnostic_code}"
                        ),
                        telemetry=telemetry,
                        submission_evidence=submission_evidence,
                    ),
                    lifecycle=lifecycle,
                    reason=InvocationStopReason.INVALID_RESPONSE,
                )
            if payload.has_error_payload:
                rejected = rejected_submission_evidence(
                    request.submission_contract,
                    binding_sha256=submission_binding_sha256,
                    status=AgentSubmissionStatus.INVALID,
                    code="openclaw_error_after_submission",
                    detail="OpenClaw returned an error reply payload after submission",
                    tool_call_id=submission_evidence.tool_call_id,
                    payload_sha256=submission_evidence.payload_sha256,
                )
                return self._finalize_lifecycle_result(
                    AgentExecutionResult(
                        status=AgentExecutionStatus.INVALID_RESPONSE,
                        error=(
                            "OpenClaw returned an error reply payload after submission"
                        ),
                        telemetry=telemetry,
                        submission_evidence=rejected,
                    ),
                    lifecycle=lifecycle,
                    reason=InvocationStopReason.INVALID_RESPONSE,
                )
            return self._finalize_lifecycle_result(
                AgentExecutionResult(
                    status=AgentExecutionStatus.COMPLETED,
                    response_text=(
                        "\n\n".join(payload.visible_texts)
                        if payload.visible_texts
                        else None
                    ),
                    telemetry=telemetry,
                    semantic_submission=semantic_submission,
                    submission_evidence=submission_evidence,
                ),
                lifecycle=lifecycle,
                reason=InvocationStopReason.COMPLETED,
            )
        if payload.has_error_payload:
            return self._finalize_lifecycle_result(
                AgentExecutionResult(
                    status=AgentExecutionStatus.INVALID_RESPONSE,
                    error="OpenClaw returned an error reply payload",
                    telemetry=telemetry,
                ),
                lifecycle=lifecycle,
                reason=InvocationStopReason.INVALID_RESPONSE,
            )
        if not payload.visible_texts:
            return self._finalize_lifecycle_result(
                AgentExecutionResult(
                    status=AgentExecutionStatus.INVALID_RESPONSE,
                    error="OpenClaw must return at least one visible text payload",
                    telemetry=telemetry,
                ),
                lifecycle=lifecycle,
                reason=InvocationStopReason.INVALID_RESPONSE,
            )
        return self._finalize_lifecycle_result(
            AgentExecutionResult(
                status=AgentExecutionStatus.COMPLETED,
                # OpenClaw may emit a semantic answer and a separate, user-visible
                # tool diagnostic. Preserve their order and let the strict response
                # parser decide whether the combined text contains exactly one
                # unambiguous semantic object. Raw payload boundaries remain in
                # telemetry.stdout for audit.
                response_text="\n\n".join(payload.visible_texts),
                telemetry=telemetry,
            ),
            lifecycle=lifecycle,
            reason=InvocationStopReason.COMPLETED,
        )

    def interrupt(
        self,
        agent_id: str,
        *,
        reason: InvocationStopReason = InvocationStopReason.USER_INTERRUPT,
    ) -> int:
        """Request typed, exact-process termination for one active Agent."""

        self._require_user_stop_reason(reason)
        with self._process_lock:
            matches = [
                (session_key, process, self._active_lifecycles[session_key])
                for session_key, (request, process) in self._active_processes.items()
                if request.agent_id == agent_id and process.poll() is None
            ]
            for session_key, _, _ in matches:
                self._interrupt_requests.setdefault(session_key, reason)
        for _, process, lifecycle in matches:
            self._begin_stop(
                process,
                lifecycle,
                reason=reason,
                now=self.monotonic(),
            )
        return len(matches)

    def interrupt_all(
        self,
        *,
        reason: InvocationStopReason = InvocationStopReason.USER_CANCEL,
    ) -> int:
        """Request typed termination of every active SAT-owned invocation."""

        self._require_user_stop_reason(reason)
        with self._process_lock:
            matches = [
                (session_key, process, self._active_lifecycles[session_key])
                for session_key, (_, process) in self._active_processes.items()
                if process.poll() is None
            ]
            for session_key, _, _ in matches:
                self._interrupt_requests.setdefault(session_key, reason)
        for _, process, lifecycle in matches:
            self._begin_stop(
                process,
                lifecycle,
                reason=reason,
                now=self.monotonic(),
            )
        return len(matches)

    def _run_interruptible_process(
        self,
        request: AgentExecutionRequest,
        command: tuple[str, ...],
        *,
        runtime_timeout_seconds: int,
        process_timeout_seconds: float | None,
        raw_stream_path: Path,
        submission_environment: Mapping[str, str],
        activity_handler: AgentExecutionActivityHandler | None,
        lifecycle: _InvocationLifecycleRecorder,
        timeout_reason: InvocationStopReason | None,
    ) -> tuple[
        subprocess.CompletedProcess[str],
        InvocationStopReason | None,
        ProviderLivenessEvidence | None,
    ]:
        """Run one process whose exact session may be interrupted by control input."""

        state_dir = self._state_directory()
        process = subprocess.Popen(
            list(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            stdin=subprocess.DEVNULL,
            env=self._environment(
                raw_stream_path=raw_stream_path,
                submission_environment=submission_environment,
            ),
            start_new_session=os.name == "posix",
        )
        process_started = self.monotonic()
        lifecycle.process_launched(
            now=process_started,
            process_group_targeted=os.name == "posix",
        )
        initialization_monitor = _InitializationLivenessMonitor(
            request=request,
            policy=self.initialization_policy,
            state_dir=state_dir,
            started_monotonic=process_started,
            lifecycle=lifecycle,
        )
        liveness_monitor = (
            None
            if state_dir is None
            else _ProviderLivenessMonitor(
                request=request,
                policy=self._liveness_policy(request),
                raw_stream_path=raw_stream_path,
                state_dir=state_dir,
                started_monotonic=process_started,
                activity_handler=activity_handler,
                initialization_monitor=initialization_monitor,
                lifecycle=lifecycle,
            )
        )
        process_lease: InvocationProcessLease | None = None
        if self.process_lease_store is not None:
            try:
                process_lease = self.process_lease_store.acquire(
                    run_id=request.run_id,
                    agent_id=request.agent_id,
                    session_key=request.session_key,
                    child_pid=process.pid,
                    command=command,
                )
            except BaseException:
                self._begin_stop(
                    process,
                    lifecycle,
                    reason=InvocationStopReason.PROCESS_FAILURE,
                    now=self.monotonic(),
                )
                self._await_process_stop(process, lifecycle)
                raise
        with self._process_lock:
            self._active_processes[request.session_key] = (request, process)
            self._active_lifecycles[request.session_key] = lifecycle
        stdout = ""
        stderr = ""
        try:
            while True:
                try:
                    stdout, stderr = process.communicate(
                        timeout=self.liveness_poll_seconds
                    )
                    initialization_monitor.finalize(self.monotonic())
                    if liveness_monitor is not None:
                        # Initialization owns launch-to-turn ordering. Once it
                        # has advanced, collect final provider counters without
                        # retroactively turning a returned response into a stall.
                        liveness_monitor.finalize(self.monotonic())
                    break
                except subprocess.TimeoutExpired:
                    now = self.monotonic()
                    with self._process_lock:
                        requested_reason = self._interrupt_requests.get(
                            request.session_key
                        )
                    if requested_reason is not None:
                        self._begin_stop(
                            process,
                            lifecycle,
                            reason=requested_reason,
                            now=now,
                        )
                        stdout, stderr = self._await_process_stop(process, lifecycle)
                        break
                    if initialization_monitor.poll(now):
                        self._begin_stop(
                            process,
                            lifecycle,
                            reason=InvocationStopReason.INITIALIZATION_STALL,
                            now=now,
                        )
                        stdout, stderr = self._await_process_stop(process, lifecycle)
                        break
                    if initialization_monitor.degradation_reason is not None:
                        self._begin_stop(
                            process,
                            lifecycle,
                            reason=InvocationStopReason.PROCESS_FAILURE,
                            now=now,
                        )
                        stdout, stderr = self._await_process_stop(process, lifecycle)
                        break
                    if liveness_monitor is not None and liveness_monitor.poll(now):
                        self._begin_stop(
                            process,
                            lifecycle,
                            reason=InvocationStopReason.PROVIDER_STALL,
                            now=now,
                        )
                        stdout, stderr = self._await_process_stop(process, lifecycle)
                        break
                    if (
                        process_timeout_seconds is not None
                        and now - process_started >= process_timeout_seconds
                    ):
                        owned_timeout_reason = timeout_reason or (
                            InvocationStopReason.EVALUATION_TIMEOUT
                        )
                        self._begin_stop(
                            process,
                            lifecycle,
                            reason=owned_timeout_reason,
                            now=now,
                        )
                        stdout, stderr = self._await_process_stop(process, lifecycle)
                        break
        finally:
            with self._process_lock:
                self._active_processes.pop(request.session_key, None)
                self._active_lifecycles.pop(request.session_key, None)
                self._interrupt_requests.pop(request.session_key, None)
            if process_lease is not None:
                assert self.process_lease_store is not None
                self.process_lease_store.release(process_lease)
            lifecycle.mark_process_lease_released()
            lifecycle.set_initialization_evidence(
                initialization_monitor.finalize(self.monotonic())
            )
        return (
            subprocess.CompletedProcess(
                list(command),
                process.returncode,
                stdout=stdout,
                stderr=stderr,
            ),
            lifecycle.stop_reason,
            None if liveness_monitor is None else liveness_monitor.evidence(),
        )

    @staticmethod
    def _require_user_stop_reason(reason: InvocationStopReason) -> None:
        if reason not in {
            InvocationStopReason.USER_INTERRUPT,
            InvocationStopReason.USER_CANCEL,
        }:
            raise AgentExecutionError("runtime controls require a user stop reason")

    def _begin_stop(
        self,
        process: subprocess.Popen[str],
        lifecycle: _InvocationLifecycleRecorder,
        *,
        reason: InvocationStopReason,
        now: float,
    ) -> None:
        first = lifecycle.request_stop(
            reason,
            now=now,
            action=(
                f"Sending SIGTERM to the exact invocation process group; "
                f"forceful cleanup is bounded by {self.process_grace_seconds:g}s"
            ),
        )
        if first:
            self._signal_process(process)
            lifecycle.mark_terminate_sent()

    def _await_process_stop(
        self,
        process: subprocess.Popen[str],
        lifecycle: _InvocationLifecycleRecorder,
    ) -> tuple[str, str]:
        """Wait the declared shutdown grace, then synchronously reap the group."""

        assert lifecycle.stop_started_monotonic is not None
        deadline = lifecycle.stop_started_monotonic + self.process_grace_seconds
        while True:
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                self._kill_process(process)
                lifecycle.mark_kill_sent()
                return process.communicate()
            try:
                return process.communicate(
                    timeout=min(self.liveness_poll_seconds, remaining)
                )
            except subprocess.TimeoutExpired:
                continue

    @staticmethod
    def _signal_process(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
        except (OSError, ProcessLookupError):
            return

    @staticmethod
    def _kill_process(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except (OSError, ProcessLookupError):
            return

    def _command(
        self,
        request: AgentExecutionRequest,
        prompt_path: Path,
        *,
        runtime_timeout_seconds: int,
    ) -> tuple[str, ...]:
        command = [
            self.openclaw_binary,
            "agent",
            "--agent",
            request.agent_id,
            "--message-file",
            str(prompt_path),
            "--session-key",
            request.session_key,
            "--json",
            "--timeout",
            str(runtime_timeout_seconds),
        ]
        if self.local:
            command.append("--local")
        if request.model is not None:
            command.extend(["--model", request.model])
        return tuple(command)

    def _runtime_timeout(
        self,
        request: AgentExecutionRequest,
        *,
        now: datetime,
    ) -> tuple[int, float | None, bool, InvocationStopReason | None]:
        """Resolve an evaluation limit or remaining user-approved run deadline.

        A zero result means no whole-invocation wall-clock limit.  It does not
        disable OpenClaw's provider stream idle watchdog or tool-level guards.
        """

        if self.run_deadline_at is None:
            return (
                request.timeout_seconds,
                (
                    float(request.timeout_seconds)
                    if request.timeout_seconds > 0
                    else None
                ),
                False,
                (
                    InvocationStopReason.EVALUATION_TIMEOUT
                    if request.timeout_seconds > 0
                    else None
                ),
            )
        remaining = (self.run_deadline_at - now.astimezone(UTC)).total_seconds()
        if remaining <= 0:
            return 0, None, True, InvocationStopReason.RUN_DEADLINE
        deadline_seconds = max(1, math.ceil(remaining))
        if request.timeout_seconds == 0:
            return (
                deadline_seconds,
                remaining,
                False,
                InvocationStopReason.RUN_DEADLINE,
            )
        if deadline_seconds <= request.timeout_seconds:
            return (
                deadline_seconds,
                remaining,
                False,
                InvocationStopReason.RUN_DEADLINE,
            )
        return (
            request.timeout_seconds,
            float(request.timeout_seconds),
            False,
            InvocationStopReason.EVALUATION_TIMEOUT,
        )

    def _state_directory(self) -> Path | None:
        if self.environment is None:
            return None
        raw = self.environment.get("OPENCLAW_STATE_DIR")
        if raw is None:
            return None
        candidate = Path(raw)
        if not candidate.is_absolute() or not candidate.is_dir():
            return None
        return candidate

    def _liveness_policy(
        self,
        request: AgentExecutionRequest,
    ) -> ProviderLivenessPolicy:
        model = request.model or "unresolved/default"
        return self._liveness_policies.get(model) or resolve_provider_liveness_policy(
            model=model,
            local=None,
        )

    def _environment(
        self,
        *,
        raw_stream_path: Path | None = None,
        submission_environment: Mapping[str, str] | None = None,
    ) -> Mapping[str, str] | None:
        if (
            self.environment is None
            and raw_stream_path is None
            and not submission_environment
        ):
            return None
        environment = {**os.environ, **(self.environment or {})}
        if raw_stream_path is not None:
            environment.update(
                {
                    "OPENCLAW_RAW_STREAM": "1",
                    "OPENCLAW_RAW_STREAM_PATH": str(raw_stream_path.resolve()),
                }
            )
        environment.update(submission_environment or {})
        return environment

    def _capture_tool_evidence(
        self,
        request: AgentExecutionRequest,
        payload: _OpenClawResponse,
    ) -> tuple[CapturedOpenClawToolEvidence | None, str | None]:
        """Read the exact session turn when SAT supplied an isolated state root."""

        if self.environment is None:
            return None, None
        state_value = self.environment.get("OPENCLAW_STATE_DIR")
        if state_value is None:
            return None, None
        if payload.session_id is None:
            return None, "OpenClaw omitted the session ID required for tool evidence"
        try:
            captured = capture_openclaw_tool_evidence(
                state_dir=Path(state_value),
                agent_id=request.agent_id,
                session_key=request.session_key,
                session_id=payload.session_id,
                prompt=request.prompt,
            )
        except OpenClawSessionEvidenceError as error:
            return None, str(error)
        return captured, None

    def _telemetry(
        self,
        *,
        request: AgentExecutionRequest,
        command: tuple[str, ...],
        started_at: datetime,
        started_monotonic: float,
        exit_code: int | None,
        stdout: str,
        stderr: str,
        timed_out: bool = False,
        interrupted: bool = False,
        payload: _OpenClawResponse | None = None,
        captured_tools: CapturedOpenClawToolEvidence | None = None,
        tool_evidence_error: str | None = None,
        provider_liveness: ProviderLivenessEvidence | None = None,
    ) -> AgentExecutionTelemetry:
        finished_at = self.clock()
        elapsed = max(0, round((self.monotonic() - started_monotonic) * 1000))
        return AgentExecutionTelemetry(
            role=request.role,
            agent_id=request.agent_id,
            capability=request.capability,
            session_key=request.session_key,
            command=command,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=elapsed,
            openclaw_duration_ms=None if payload is None else payload.duration_ms,
            exit_code=exit_code,
            timed_out=timed_out,
            interrupted=interrupted,
            stdout=stdout,
            stderr=stderr,
            openclaw_run_id=None if payload is None else payload.openclaw_run_id,
            session_id=None if payload is None else payload.session_id,
            provider=None if payload is None else payload.provider,
            model=None if payload is None else payload.model,
            usage=None if payload is None else payload.usage,
            provider_liveness=provider_liveness,
            tool_evidence_status=(
                AgentToolEvidenceStatus.INVALID
                if tool_evidence_error is not None
                else (
                    AgentToolEvidenceStatus.CAPTURED
                    if captured_tools is not None
                    else AgentToolEvidenceStatus.NOT_CAPTURED
                )
            ),
            session_transcript_sha256=(
                None if captured_tools is None else captured_tools.transcript_sha256
            ),
            session_record_count=(
                None if captured_tools is None else captured_tools.record_count
            ),
            tool_calls=(() if captured_tools is None else captured_tools.tool_calls),
            tool_evidence_error=tool_evidence_error,
        )

    @staticmethod
    def _finalize_lifecycle_result(
        result: AgentExecutionResult,
        *,
        lifecycle: _InvocationLifecycleRecorder,
        reason: InvocationStopReason,
    ) -> AgentExecutionResult:
        """Attach terminal lifecycle evidence only after result evidence is known."""

        lifecycle.request_stop(
            reason,
            now=lifecycle.started_monotonic + (result.telemetry.duration_ms / 1000),
            action="Invocation work ended; entering the controlled stop protocol",
        )
        evidence = lifecycle.finalize(
            reason=reason,
            telemetry=result.telemetry,
            submission_evidence=result.submission_evidence,
        )
        telemetry = result.telemetry.model_copy(
            update={"invocation_lifecycle": evidence}
        )
        return result.model_copy(update={"telemetry": telemetry})

    def _timeout_result(
        self,
        *,
        request: AgentExecutionRequest,
        command: tuple[str, ...],
        started_at: datetime,
        started_monotonic: float,
        error: subprocess.TimeoutExpired,
        exit_code: int | None = None,
        provider_liveness: ProviderLivenessEvidence | None = None,
    ) -> AgentExecutionResult:
        telemetry = self._telemetry(
            request=request,
            command=command,
            started_at=started_at,
            started_monotonic=started_monotonic,
            exit_code=exit_code,
            timed_out=True,
            stdout=_decode_process_output(error.stdout),
            stderr=_decode_process_output(error.stderr),
            provider_liveness=provider_liveness,
        )
        return AgentExecutionResult(
            status=AgentExecutionStatus.TIMED_OUT,
            error=(
                "OpenClaw did not exit within the user-authorized run deadline "
                f"and process shutdown grace ({error.timeout} seconds total)"
                if self.run_deadline_at is not None
                else f"OpenClaw exceeded the controlled-evaluation process timeout "
                f"of {error.timeout} seconds"
            ),
            telemetry=telemetry,
        )

    def _expired_deadline_result(
        self,
        *,
        request: AgentExecutionRequest,
        command: tuple[str, ...],
        started_at: datetime,
        started_monotonic: float,
    ) -> AgentExecutionResult:
        """Refuse a new provider call after the whole-run deadline expired."""

        telemetry = self._telemetry(
            request=request,
            command=command,
            started_at=started_at,
            started_monotonic=started_monotonic,
            exit_code=None,
            timed_out=True,
            stdout="",
            stderr="",
        )
        return AgentExecutionResult(
            status=AgentExecutionStatus.TIMED_OUT,
            error="User-authorized whole-run deadline expired before this call",
            telemetry=telemetry,
        )

    def _interrupted_result(
        self,
        *,
        request: AgentExecutionRequest,
        command: tuple[str, ...],
        started_at: datetime,
        started_monotonic: float,
        exit_code: int | None,
        stdout: str,
        stderr: str,
        provider_liveness: ProviderLivenessEvidence | None = None,
        stop_reason: InvocationStopReason = InvocationStopReason.USER_INTERRUPT,
    ) -> AgentExecutionResult:
        telemetry = self._telemetry(
            request=request,
            command=command,
            started_at=started_at,
            started_monotonic=started_monotonic,
            exit_code=exit_code,
            interrupted=True,
            stdout=stdout,
            stderr=stderr,
            provider_liveness=provider_liveness,
        )
        return AgentExecutionResult(
            status=AgentExecutionStatus.INTERRUPTED,
            error=(
                "Agent invocation was cancelled by user control"
                if stop_reason is InvocationStopReason.USER_CANCEL
                else "Agent invocation was interrupted by user control"
            ),
            telemetry=telemetry,
        )

    def _initialization_stalled_result(
        self,
        *,
        request: AgentExecutionRequest,
        command: tuple[str, ...],
        started_at: datetime,
        started_monotonic: float,
        exit_code: int | None,
        stdout: str,
        stderr: str,
        provider_liveness: ProviderLivenessEvidence | None,
    ) -> AgentExecutionResult:
        telemetry = self._telemetry(
            request=request,
            command=command,
            started_at=started_at,
            started_monotonic=started_monotonic,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            provider_liveness=provider_liveness,
        )
        return AgentExecutionResult(
            status=AgentExecutionStatus.INITIALIZATION_STALLED,
            error=(
                "OpenClaw made no attributable initialization progress through "
                "the visible diagnostic grace period"
            ),
            telemetry=telemetry,
        )

    def _provider_stalled_result(
        self,
        *,
        request: AgentExecutionRequest,
        command: tuple[str, ...],
        started_at: datetime,
        started_monotonic: float,
        exit_code: int | None,
        stdout: str,
        stderr: str,
        provider_liveness: ProviderLivenessEvidence | None,
    ) -> AgentExecutionResult:
        if provider_liveness is None or not provider_liveness.stalled:
            raise AgentExecutionError(
                "terminal provider stall requires liveness evidence"
            )
        telemetry = self._telemetry(
            request=request,
            command=command,
            started_at=started_at,
            started_monotonic=started_monotonic,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            provider_liveness=provider_liveness,
        )
        return AgentExecutionResult(
            status=AgentExecutionStatus.PROVIDER_STALLED,
            error=(
                "Provider produced no trusted activity through the visible stall "
                "probe and grace period"
            ),
            telemetry=telemetry,
        )

    @staticmethod
    def _emit_declared_provider_stall(
        *,
        request: AgentExecutionRequest,
        activity_handler: AgentExecutionActivityHandler | None,
        evidence: ProviderLivenessEvidence,
        elapsed_ms: int,
    ) -> None:
        """Project a provider-owned timeout only when silence proves the cause."""

        if activity_handler is None:
            return
        activity = AgentExecutionActivity(
            kind=AgentExecutionActivityKind.PROVIDER_STALLED,
            agent_id=request.agent_id,
            session_key=request.session_key,
            model=request.model,
            elapsed_ms=elapsed_ms,
            trusted_activity_count=(
                evidence.provider_activity_observations
                + evidence.tool_started_count
                + evidence.tool_completed_count
            ),
            active_tool_count=max(
                0,
                evidence.tool_started_count - evidence.tool_completed_count,
            ),
            completed_tool_count=evidence.tool_completed_count,
            inactivity_ms=evidence.maximum_inactivity_ms,
            silence_seconds=evidence.silence_seconds,
            stall_grace_seconds=evidence.stall_grace_seconds,
            policy_source=evidence.policy_source,
        )
        try:
            activity_handler(activity)
        except Exception:
            # Liveness evidence remains authoritative even if an optional
            # presentation callback fails after OpenClaw has already stopped.
            return

    def _launch_failure_result(
        self,
        *,
        request: AgentExecutionRequest,
        command: tuple[str, ...],
        started_at: datetime,
        started_monotonic: float,
        error: OSError,
    ) -> AgentExecutionResult:
        telemetry = self._telemetry(
            request=request,
            command=command,
            started_at=started_at,
            started_monotonic=started_monotonic,
            exit_code=None,
            stdout="",
            stderr=str(error),
        )
        return AgentExecutionResult(
            status=AgentExecutionStatus.LAUNCH_FAILED,
            error=f"cannot launch OpenClaw: {error}",
            telemetry=telemetry,
        )


class ScriptedAgentResponse(BaseModel):
    """One deterministic reply consumed by the offline execution adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(min_length=1)
    model: str = "scripted/model"
    provider: str = "scripted"
    session_id: str | None = None
    usage: AgentTokenUsage | None = None
    duration_ms: int = Field(default=0, ge=0)
    stderr: str = ""
    tool_calls: tuple[AgentToolCallEvidence, ...] = ()
    submission_payload: dict[str, JsonValue] | None = None

    @field_validator("text", "model", "provider")
    @classmethod
    def require_nonblank_text(cls, value: str) -> str:
        """Reject unusable scripted metadata and responses."""

        if not value.strip():
            raise ValueError("scripted response fields must not be blank")
        return value


type ScriptedInput = ScriptedAgentResponse | AgentExecutionResult | PhaseArtifact | str


def _scripted_review_tool_call() -> AgentToolCallEvidence:
    """Return explicit synthetic tool evidence for the offline scripted adapter."""

    output = b"scripted-review-observation"
    return AgentToolCallEvidence(
        id="tool-001",
        tool_name="read",
        external_call_sha256=hashlib.sha256(b"scripted-tool-001").hexdigest(),
        arguments_sha256=hashlib.sha256(b'{"path":"/agent"}').hexdigest(),
        outcome=AgentToolCallOutcome.SUCCEEDED,
        is_error=False,
        output_sha256=hashlib.sha256(output).hexdigest(),
        output_bytes=len(output),
        output_excerpt=output.decode("utf-8"),
    )


class ScriptedAgentExecutor:
    """FIFO Agent adapter for deterministic offline workflow and recovery tests."""

    def __init__(
        self,
        responses: Iterable[ScriptedInput],
        *,
        clock: WallClock = _system_clock,
    ) -> None:
        self._responses = deque(responses)
        self.clock = clock
        self.requests: list[AgentExecutionRequest] = []

    @property
    def remaining(self) -> int:
        """Return the number of unused scripted responses."""

        return len(self._responses)

    def execute(
        self,
        request: AgentExecutionRequest,
        *,
        activity_handler: AgentExecutionActivityHandler | None = None,
    ) -> AgentExecutionResult:
        """Consume one predeclared response and record the exact request."""

        started_monotonic = time.monotonic()
        lifecycle = _InvocationLifecycleRecorder(
            request=request,
            started_monotonic=started_monotonic,
            process_grace_seconds=DEFAULT_PROCESS_SHUTDOWN_GRACE_SECONDS,
            initialization_policy=InitializationLivenessPolicy(
                no_progress_seconds=DEFAULT_INITIALIZATION_NO_PROGRESS_SECONDS,
                stall_grace_seconds=DEFAULT_INITIALIZATION_STALL_GRACE_SECONDS,
                source="scripted deterministic adapter",
            ),
            activity_handler=activity_handler,
            monotonic=time.monotonic,
        )
        lifecycle.transition(
            InvocationPhase.INITIALIZING,
            now=started_monotonic,
            action="Scripted invocation initialized its attributable current turn",
        )
        lifecycle.set_initialization_evidence(
            InitializationLivenessEvidence(
                mode="enforced",
                policy_source="scripted deterministic adapter",
                no_progress_seconds=DEFAULT_INITIALIZATION_NO_PROGRESS_SECONDS,
                stall_grace_seconds=DEFAULT_INITIALIZATION_STALL_GRACE_SECONDS,
                checkpoints=(InitializationCheckpoint.CURRENT_TURN,),
            )
        )
        lifecycle.provider_ready(
            InitializationCheckpoint.CURRENT_TURN,
            now=started_monotonic,
        )
        self.requests.append(request)
        if not self._responses:
            raise ScriptedResponseExhaustedError(
                f"no scripted response remains for {request.agent_id}"
            )
        scripted = self._responses.popleft()
        if isinstance(scripted, AgentExecutionResult):
            return scripted
        if isinstance(scripted, BaseModel):
            if isinstance(scripted, ScriptedAgentResponse):
                response = scripted
            else:
                response = ScriptedAgentResponse(
                    text=json.dumps(
                        scripted.model_dump(mode="json"),
                        ensure_ascii=False,
                    )
                )
        else:
            response = ScriptedAgentResponse(text=scripted)

        now = self.clock()
        tool_calls = (
            response.tool_calls or (_scripted_review_tool_call(),)
            if request.capability is AgentCapability.REVIEW
            else response.tool_calls
        )
        submission_payload = response.submission_payload
        if submission_payload is None and request.submission_contract is not None:
            try:
                candidate = json.loads(response.text)
            except (TypeError, ValueError, json.JSONDecodeError):
                candidate = None
            if isinstance(candidate, dict):
                submission_payload = candidate
        semantic_submission: AgentSemanticSubmission | None = None
        submission_evidence: AgentSubmissionEvidence | None = None
        if request.submission_contract is not None and submission_payload is not None:
            external_id = f"scripted-submission-{len(tool_calls) + 1:03d}"
            output = b"scripted-semantic-submission"
            submission_call = AgentToolCallEvidence(
                id=f"tool-{len(tool_calls) + 1:03d}",
                tool_name=request.submission_contract.tool_name,
                external_call_sha256=hashlib.sha256(
                    external_id.encode("utf-8")
                ).hexdigest(),
                arguments_sha256=canonical_json_sha256(submission_payload),
                outcome=AgentToolCallOutcome.SUCCEEDED,
                is_error=False,
                output_sha256=hashlib.sha256(output).hexdigest(),
                output_bytes=len(output),
                output_excerpt=output.decode("utf-8"),
            )
            tool_calls = (*tool_calls, submission_call)
            binding_sha256 = hashlib.sha256(
                (
                    f"{request.session_key}\x00"
                    f"{request.submission_contract.schema_sha256}"
                ).encode()
            ).hexdigest()
            submission_evidence = AgentSubmissionEvidence(
                purpose=request.submission_contract.purpose,
                status=AgentSubmissionStatus.ACCEPTED,
                schema_sha256=request.submission_contract.schema_sha256,
                binding_sha256=binding_sha256,
                tool_call_id=submission_call.id,
                payload_sha256=canonical_json_sha256(submission_payload),
            )
            semantic_submission = AgentSemanticSubmission(
                payload=submission_payload,
                evidence=submission_evidence,
            )
        transcript_sha256 = (
            hashlib.sha256(
                f"{request.session_key}\n{request.prompt}\n{response.text}".encode()
            ).hexdigest()
            if tool_calls
            else None
        )
        telemetry = AgentExecutionTelemetry(
            role=request.role,
            agent_id=request.agent_id,
            capability=request.capability,
            session_key=request.session_key,
            command=("scripted-agent", request.agent_id),
            started_at=now,
            finished_at=now,
            duration_ms=response.duration_ms,
            openclaw_duration_ms=response.duration_ms,
            exit_code=0,
            stdout=response.text,
            stderr=response.stderr,
            openclaw_run_id=f"scripted-{request.run_id}-{request.iteration}",
            session_id=response.session_id
            or f"scripted-{request.run_id}-{request.agent_id}-{request.iteration}",
            provider=response.provider,
            model=response.model,
            usage=response.usage,
            tool_evidence_status=(
                AgentToolEvidenceStatus.CAPTURED
                if tool_calls
                else AgentToolEvidenceStatus.NOT_CAPTURED
            ),
            session_transcript_sha256=transcript_sha256,
            session_record_count=(max(3, 2 + len(tool_calls)) if tool_calls else None),
            tool_calls=tool_calls,
        )
        result = AgentExecutionResult(
            status=AgentExecutionStatus.COMPLETED,
            response_text=response.text,
            telemetry=telemetry,
            semantic_submission=semantic_submission,
            submission_evidence=submission_evidence,
        )
        lifecycle.request_stop(
            InvocationStopReason.COMPLETED,
            now=started_monotonic + (response.duration_ms / 1000),
            action="Scripted invocation completed; collecting deterministic evidence",
        )
        evidence = lifecycle.finalize(
            reason=InvocationStopReason.COMPLETED,
            telemetry=telemetry,
            submission_evidence=submission_evidence,
        )
        return result.model_copy(
            update={
                "telemetry": telemetry.model_copy(
                    update={"invocation_lifecycle": evidence}
                )
            }
        )


def scripted_executor(responses: Sequence[ScriptedInput]) -> AgentExecutor:
    """Return a protocol-typed scripted executor for dependency injection."""

    return ScriptedAgentExecutor(responses)
