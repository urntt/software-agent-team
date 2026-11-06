"""Core execution request, result, and telemetry models."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol, Self, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from software_agent_team.artifacts import (
    AgentExecutionStatus,
    AgentRole,
    AgentRuntimeFailureCode,
    AgentToolCallEvidence,
    AgentToolEvidenceStatus,
    ArtifactKind,
    ProviderLivenessEvidence,
    RuntimeToolRejection,
    validate_tool_evidence_collection,
)
from software_agent_team.invocation_lifecycle import (
    InitializationCheckpoint,
    InvocationLifecycleEvidence,
    InvocationPhase,
    InvocationStopReason,
)
from software_agent_team.model_costs import CacheTokenUsage
from software_agent_team.model_runtime import OpenClawThinkingLevel
from software_agent_team.openclaw_session_evidence import (
    OpenClawToolActivity,
)
from software_agent_team.submissions import (
    AgentSemanticSubmission,
    AgentSubmissionContract,
    AgentSubmissionEvidence,
    AgentSubmissionStatus,
)
from software_agent_team.teams import (
    AgentCapability,
    AgentSpecialization,
    capability_for_legacy_role,
    default_specialization_for_capability,
    expected_output_for_specialization,
    specialization_contract,
)
from software_agent_team.terminal_presentation import InvocationPresentation

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
    INVOCATION_FINALIZING_RESPONSE = "invocation_finalizing_response"
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
    FINALIZATION_PROGRESS = "finalization_progress"
    FINALIZATION_STALL_SUSPECTED = "finalization_stall_suspected"
    FINALIZATION_STALL_RECOVERED = "finalization_stall_recovered"
    RESPONSE_FINALIZATION_STALLED = "response_finalization_stalled"


class AgentToolActionClass(StrEnum):
    """Safe controller classification of an attributable tool operation."""

    INSPECTING = "inspecting"
    EDITING = "editing"
    TESTING = "testing"
    BUILDING = "building"
    RUNNING = "running"
    PREPARING = "preparing"
    SUBMITTING = "submitting"


class AgentToolTargetClass(StrEnum):
    """Bounded target category that never exposes tool arguments or content."""

    PROJECT_CONTENT = "project_content"
    PROJECT_FILES = "project_files"
    QUALITY_CHECKS = "quality_checks"
    VERSION_CONTROL = "version_control"
    REVIEW_PROBE = "review_probe"
    TYPED_ARTIFACT = "typed_artifact"
    SANDBOX_WORKSPACE = "sandbox_workspace"


def _classify_tool_activity(
    activity: OpenClawToolActivity,
) -> tuple[AgentToolActionClass, AgentToolTargetClass, str | None]:
    """Project only allow-listed action metadata from a tool identity."""

    tool_name = activity.tool_name
    if tool_name == "read":
        return (
            AgentToolActionClass.INSPECTING,
            AgentToolTargetClass.PROJECT_CONTENT,
            "read",
        )
    if tool_name in {"write", "edit", "apply_patch"}:
        return (
            AgentToolActionClass.EDITING,
            AgentToolTargetClass.PROJECT_FILES,
            tool_name,
        )
    if tool_name == "sat_submit_artifact":
        return (
            AgentToolActionClass.SUBMITTING,
            AgentToolTargetClass.TYPED_ARTIFACT,
            None,
        )
    if tool_name != "exec" or activity.executable is None:
        return (
            AgentToolActionClass.RUNNING,
            AgentToolTargetClass.SANDBOX_WORKSPACE,
            None,
        )
    executable = activity.executable.rsplit("/", maxsplit=1)[-1]
    if executable in {"pytest", "ruff", "mypy", "pyright", "eslint"}:
        return (
            AgentToolActionClass.TESTING,
            AgentToolTargetClass.QUALITY_CHECKS,
            executable,
        )
    if executable == "git":
        return (
            AgentToolActionClass.INSPECTING,
            AgentToolTargetClass.VERSION_CONTROL,
            executable,
        )
    if executable == "sat-probe-run":
        return (
            AgentToolActionClass.TESTING,
            AgentToolTargetClass.REVIEW_PROBE,
            executable,
        )
    if executable == "sat-probe-write":
        return (
            AgentToolActionClass.PREPARING,
            AgentToolTargetClass.REVIEW_PROBE,
            executable,
        )
    if executable in {"uv", "pip", "npm", "pnpm", "yarn", "make"}:
        return (
            AgentToolActionClass.BUILDING,
            AgentToolTargetClass.SANDBOX_WORKSPACE,
            executable,
        )
    if executable in {
        "cat",
        "find",
        "grep",
        "head",
        "ls",
        "pwd",
        "read",
        "readlink",
        "rg",
        "sed",
        "stat",
        "tail",
    }:
        return (
            AgentToolActionClass.INSPECTING,
            AgentToolTargetClass.PROJECT_CONTENT,
            executable,
        )
    return (
        AgentToolActionClass.RUNNING,
        AgentToolTargetClass.SANDBOX_WORKSPACE,
        None,
    )


class AgentExecutionActivity(BaseModel):
    """Controller observation with an optional ephemeral terminal preview."""

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
    tool_action_class: AgentToolActionClass | None = None
    tool_target_class: AgentToolTargetClass | None = None
    tool_detail: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$",
    )
    presentation: InvocationPresentation | None = Field(default=None, exclude=True)

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
        finalization_kinds = {
            AgentExecutionActivityKind.FINALIZATION_PROGRESS,
            AgentExecutionActivityKind.FINALIZATION_STALL_SUSPECTED,
            AgentExecutionActivityKind.FINALIZATION_STALL_RECOVERED,
            AgentExecutionActivityKind.RESPONSE_FINALIZATION_STALLED,
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
        if self.kind in finalization_kinds and any(
            value is None
            for value in (
                self.silence_seconds,
                self.stall_grace_seconds,
                self.policy_source,
            )
        ):
            raise ValueError("response-finalization activity requires its guard policy")
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
            AgentExecutionActivityKind.INVOCATION_FINALIZING_RESPONSE: (
                InvocationPhase.FINALIZING_RESPONSE
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
        tool_kinds = {
            AgentExecutionActivityKind.TOOL_STARTED,
            AgentExecutionActivityKind.TOOL_COMPLETED,
        }
        tool_classified = (
            self.tool_action_class is not None and self.tool_target_class is not None
        )
        if (self.tool_action_class is None) != (self.tool_target_class is None):
            raise ValueError("tool action and target classes must appear together")
        if (tool_classified or self.tool_detail is not None) and self.kind not in (
            tool_kinds
        ):
            raise ValueError("tool classification belongs only to tool history events")
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
    generation: int = 1,
) -> str:
    """Build one deterministic session key for a run-scoped Agent invocation."""

    base = (
        f"agent:{agent_id}:"
        f"sat-{run_id}-i{iteration}-{expected_kind.value.replace('_', '-')}"
    )
    return base if generation == 1 else f"{base}-g{generation}"


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
    session_generation: int = Field(default=1, ge=1)
    role: AgentRole | None = None
    agent_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    capability: AgentCapability
    specialization: AgentSpecialization
    expected_kind: ArtifactKind
    prompt: str = Field(min_length=1)
    timeout_seconds: int = Field(ge=0)
    model: str | None = None
    thinking_level: OpenClawThinkingLevel | None = None
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
        payload.setdefault(
            "specialization",
            default_specialization_for_capability(
                capability_for_legacy_role(role)
            ).value,
        )
        return payload

    @model_validator(mode="before")
    @classmethod
    def populate_dynamic_compatibility_specialization(cls, value: object) -> object:
        """Keep pre-specialization dynamic callers on their exact old contract."""

        if not isinstance(value, dict) or value.get("specialization") is not None:
            return value
        payload = dict(value)
        try:
            capability = AgentCapability(payload.get("capability"))
        except (TypeError, ValueError):
            return payload
        payload["specialization"] = default_specialization_for_capability(
            capability
        ).value
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

        if self.thinking_level is not None and self.model is None:
            raise ValueError("thinking level requires an explicit model override")
        if (
            self.capability
            not in specialization_contract(self.specialization).compatible_capabilities
        ):
            raise ValueError("Agent capability and specialization are incompatible")
        if self.role is not None:
            if self.agent_id != self.role.value:
                raise ValueError("legacy role identity must match its Agent ID")
            if self.capability is not capability_for_legacy_role(self.role):
                raise ValueError("legacy role and Agent capability are inconsistent")
            if self.specialization is not default_specialization_for_capability(
                self.capability
            ):
                raise ValueError("legacy role and specialization are inconsistent")
            validate_role_artifact_kind(self.role, self.expected_kind)
        elif self.expected_kind is not expected_output_for_specialization(
            self.specialization
        ):
            raise ValueError(
                f"Agent specialization {self.specialization.value} cannot produce "
                f"{self.expected_kind.value}; expected "
                f"{expected_output_for_specialization(self.specialization).value}"
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
            generation=self.session_generation,
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

    @property
    def cache_usage(self) -> CacheTokenUsage:
        """Preserve unknown cache counters instead of silently dropping them."""
        return CacheTokenUsage(
            read_tokens=self.cache_read_tokens,
            write_tokens=self.cache_write_tokens,
        )


class AgentExecutionTelemetry(BaseModel):
    """Raw, attributable evidence for one Agent process invocation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: AgentRole | None = None
    agent_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    capability: AgentCapability
    specialization: AgentSpecialization
    session_key: str = Field(min_length=1)
    command: tuple[str, ...] = Field(min_length=1)
    started_at: datetime
    finished_at: datetime
    duration_ms: int = Field(ge=0)
    openclaw_duration_ms: int | None = Field(default=None, ge=0)
    exit_code: int | None = None
    timed_out: bool = False
    interrupted: bool = False
    runtime_failure_code: AgentRuntimeFailureCode | None = None
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
    runtime_rejections: tuple[RuntimeToolRejection, ...] = Field(
        default=(),
        exclude_if=lambda value: not value,
    )
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
        payload.setdefault(
            "specialization",
            default_specialization_for_capability(
                capability_for_legacy_role(role)
            ).value,
        )
        return payload

    @model_validator(mode="before")
    @classmethod
    def populate_dynamic_compatibility_specialization(cls, value: object) -> object:
        """Read telemetry emitted before specialization identity was added."""

        if not isinstance(value, dict) or value.get("specialization") is not None:
            return value
        payload = dict(value)
        try:
            capability = AgentCapability(payload.get("capability"))
        except (TypeError, ValueError):
            return payload
        payload["specialization"] = default_specialization_for_capability(
            capability
        ).value
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
            or self.specialization
            is not default_specialization_for_capability(self.capability)
        ):
            raise ValueError("telemetry legacy role identity is inconsistent")
        if (
            self.capability
            not in specialization_contract(self.specialization).compatible_capabilities
        ):
            raise ValueError("telemetry capability and specialization are incompatible")
        if self.finished_at < self.started_at:
            raise ValueError("execution cannot finish before it starts")
        if self.timed_out and self.interrupted:
            raise ValueError("an Agent execution cannot be timed out and interrupted")
        if self.runtime_failure_code is not None and self.exit_code in {None, 0}:
            raise ValueError("runtime failure codes require a nonzero process exit")
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
            runtime_rejections=self.runtime_rejections,
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

        lifecycle = self.telemetry.invocation_lifecycle
        recovered_terminal_submission = (
            lifecycle is not None
            and lifecycle.shutdown.reason
            in {
                InvocationStopReason.RESPONSE_FINALIZATION_STALL,
                InvocationStopReason.PROCESS_FAILURE,
            }
            and (
                lifecycle.shutdown.reason is not InvocationStopReason.PROCESS_FAILURE
                or self.telemetry.exit_code not in {None, 0}
            )
            and lifecycle.response_finalization is not None
            and (
                lifecycle.shutdown.reason is InvocationStopReason.PROCESS_FAILURE
                or lifecycle.response_finalization.stalled
            )
            and self.telemetry.provider_liveness is not None
            and self.telemetry.provider_liveness.terminal_response_observed
        )
        if self.status is AgentExecutionStatus.COMPLETED:
            if self.response_text is None and self.semantic_submission is None:
                raise ValueError(
                    "completed Agent execution requires response text or a semantic "
                    "submission"
                )
            if self.error is not None:
                raise ValueError("completed Agent execution cannot contain an error")
            if self.telemetry.timed_out or (
                self.telemetry.exit_code != 0 and not recovered_terminal_submission
            ):
                raise ValueError(
                    "completed Agent execution requires a zero process exit or "
                    "an attributable terminal submission recovery"
                )
            if recovered_terminal_submission and self.semantic_submission is None:
                raise ValueError(
                    "terminal recovery requires a typed semantic submission"
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


def execution_exception_result(
    request: AgentExecutionRequest,
    error: BaseException,
    *,
    started_at: datetime,
    finished_at: datetime,
) -> AgentExecutionResult:
    """Record an adapter exception without inventing process or provider evidence."""

    interrupted = isinstance(error, KeyboardInterrupt)
    detail = str(error).strip() or type(error).__name__
    return AgentExecutionResult(
        status=(
            AgentExecutionStatus.INTERRUPTED
            if interrupted
            else AgentExecutionStatus.LAUNCH_FAILED
        ),
        error=f"Agent executor raised {type(error).__name__}: {detail}",
        telemetry=AgentExecutionTelemetry(
            role=request.role,
            agent_id=request.agent_id,
            capability=request.capability,
            specialization=request.specialization,
            session_key=request.session_key,
            command=("agent-executor",),
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=max(
                0, round((finished_at - started_at).total_seconds() * 1000)
            ),
            exit_code=None,
            interrupted=interrupted,
        ),
    )


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
