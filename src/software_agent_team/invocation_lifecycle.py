"""Versioned, content-free lifecycle evidence for one Agent invocation."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from software_agent_team.process_diagnostics import InitializationWaitDiagnostic


class InvocationPhase(StrEnum):
    """Controller-owned phases for an invocation and its exact process."""

    LAUNCHED = "launched"
    INITIALIZING = "initializing"
    PROVIDER_WAIT = "provider_wait"
    TOOL_ACTIVE = "tool_active"
    FINALIZING_RESPONSE = "finalizing_response"
    STOPPING = "stopping"
    COLLECTING_EVIDENCE = "collecting_evidence"
    STOPPED = "stopped"


class InvocationStopReason(StrEnum):
    """Authority that caused one invocation to reach its terminal phase."""

    COMPLETED = "completed"
    INITIALIZATION_STALL = "initialization_stall"
    PROVIDER_STALL = "provider_stall"
    RESPONSE_FINALIZATION_STALL = "response_finalization_stall"
    USER_INTERRUPT = "user_interrupt"
    USER_CANCEL = "user_cancel"
    RUN_DEADLINE = "run_deadline"
    EVALUATION_TIMEOUT = "evaluation_timeout"
    PROCESS_FAILURE = "process_failure"
    PROVIDER_FAILURE = "provider_failure"
    UPSTREAM_INCOMPLETE = "upstream_incomplete"
    INVALID_RESPONSE = "invalid_response"
    LAUNCH_FAILURE = "launch_failure"


class InitializationCheckpoint(StrEnum):
    """Finite, attributable OpenClaw launch-to-current-turn checkpoints."""

    PROCESS_LAUNCHED = "process_launched"
    SESSION_DIRECTORY = "session_directory"
    SESSION_INDEX = "session_index"
    SESSION_BOUND = "session_bound"
    TRANSCRIPT_HEADER = "transcript_header"
    CURRENT_TURN = "current_turn"
    PROVIDER_STREAM = "provider_stream"


_INITIALIZATION_ORDER = {
    checkpoint: index
    for index, checkpoint in enumerate(InitializationCheckpoint, start=1)
}

_ALLOWED_PHASE_SUCCESSORS: dict[InvocationPhase, frozenset[InvocationPhase]] = {
    InvocationPhase.LAUNCHED: frozenset(
        {InvocationPhase.INITIALIZING, InvocationPhase.STOPPING}
    ),
    InvocationPhase.INITIALIZING: frozenset(
        {
            InvocationPhase.INITIALIZING,
            InvocationPhase.PROVIDER_WAIT,
            InvocationPhase.STOPPING,
        }
    ),
    InvocationPhase.PROVIDER_WAIT: frozenset(
        {
            InvocationPhase.TOOL_ACTIVE,
            InvocationPhase.FINALIZING_RESPONSE,
            InvocationPhase.STOPPING,
        }
    ),
    InvocationPhase.TOOL_ACTIVE: frozenset(
        {
            InvocationPhase.PROVIDER_WAIT,
            InvocationPhase.TOOL_ACTIVE,
            InvocationPhase.FINALIZING_RESPONSE,
            InvocationPhase.STOPPING,
        }
    ),
    InvocationPhase.FINALIZING_RESPONSE: frozenset({InvocationPhase.STOPPING}),
    InvocationPhase.STOPPING: frozenset({InvocationPhase.COLLECTING_EVIDENCE}),
    InvocationPhase.COLLECTING_EVIDENCE: frozenset({InvocationPhase.STOPPED}),
    InvocationPhase.STOPPED: frozenset(),
}


class InvocationLifecycleTransition(BaseModel):
    """One immutable transition emitted by the execution adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence: int = Field(ge=1)
    phase: InvocationPhase
    elapsed_ms: int = Field(ge=0)
    stop_reason: InvocationStopReason | None = None
    initialization_checkpoint: InitializationCheckpoint | None = None
    action: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def bind_stop_reason(self) -> Self:
        terminal = {
            InvocationPhase.STOPPING,
            InvocationPhase.COLLECTING_EVIDENCE,
            InvocationPhase.STOPPED,
        }
        if (self.phase in terminal) != (self.stop_reason is not None):
            raise ValueError("shutdown phases require exactly one stop reason")
        if self.initialization_checkpoint is not None and self.phase not in {
            InvocationPhase.INITIALIZING,
            InvocationPhase.PROVIDER_WAIT,
        }:
            raise ValueError(
                "initialization checkpoints belong to initialization or provider wait"
            )
        return self


class InitializationLivenessEvidence(BaseModel):
    """Bounded launch-to-turn readiness observations without prompt content."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: str = Field(pattern=r"^(enforced|degraded|unavailable)$")
    policy_source: str = Field(min_length=1, max_length=300)
    no_progress_seconds: float = Field(gt=0)
    stall_grace_seconds: float = Field(gt=0)
    baseline_checkpoint: InitializationCheckpoint | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    baseline_matching_turn_count: int = Field(
        default=0,
        ge=0,
        exclude_if=lambda value: value == 0,
    )
    checkpoints: tuple[InitializationCheckpoint, ...] = ()
    process_activity_observations: int = Field(
        default=0,
        ge=0,
        exclude_if=lambda value: value == 0,
    )
    stall_suspected_count: int = Field(default=0, ge=0)
    stall_recovered_count: int = Field(default=0, ge=0)
    maximum_no_progress_ms: int = Field(default=0, ge=0)
    stalled: bool = False
    degradation_reason: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_observation(self) -> Self:
        if self.stall_grace_seconds >= self.no_progress_seconds:
            raise ValueError("initialization grace must be shorter than its ceiling")
        if (self.mode == "enforced") == (self.degradation_reason is not None):
            raise ValueError(
                "non-enforced initialization evidence requires exactly one reason"
            )
        if self.stalled and self.mode != "enforced":
            raise ValueError("unobservable initialization cannot declare a stall")
        if (
            self.baseline_matching_turn_count
            and self.baseline_checkpoint is not InitializationCheckpoint.CURRENT_TURN
        ):
            raise ValueError(
                "matching-turn baseline requires a pre-existing current turn"
            )
        order = [_INITIALIZATION_ORDER[item] for item in self.checkpoints]
        if order != sorted(set(order)):
            raise ValueError("initialization checkpoints must advance exactly once")
        if self.stalled and InitializationCheckpoint.CURRENT_TURN in self.checkpoints:
            raise ValueError("ready current turns cannot be initialization-stalled")
        return self


class InvocationShutdownEvidence(BaseModel):
    """Exact process and evidence-collection outcome after work stopped."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: InvocationStopReason
    shutdown_grace_seconds: float = Field(gt=0)
    process_started: bool
    process_group_targeted: bool = False
    terminate_sent: bool = False
    kill_sent: bool = False
    exit_code: int | None = None
    signal: int | None = Field(default=None, ge=1)
    stdout_collected: bool
    stderr_collected: bool
    session_evidence_status: str = Field(min_length=1, max_length=100)
    submission_evidence_status: str = Field(min_length=1, max_length=100)
    process_lease_released: bool
    cleanup_completed: bool

    @model_validator(mode="after")
    def validate_process_outcome(self) -> Self:
        if not self.process_started and any(
            (
                self.process_group_targeted,
                self.terminate_sent,
                self.kill_sent,
                self.exit_code is not None,
                self.signal is not None,
            )
        ):
            raise ValueError("an unstarted process cannot have termination evidence")
        if self.signal is not None and self.exit_code is not None:
            raise ValueError("process outcome cannot be both exit code and signal")
        if self.kill_sent and not self.terminate_sent:
            raise ValueError("forceful cleanup requires a preceding terminate request")
        if self.cleanup_completed and not self.process_lease_released:
            raise ValueError("completed cleanup requires process-lease release")
        return self


class ResponseFinalizationEvidence(BaseModel):
    """Content-free guard evidence after the provider response is complete."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["not_observed", "enforced"]
    policy_source: str = Field(min_length=1, max_length=300)
    no_progress_seconds: float = Field(gt=0)
    stall_grace_seconds: float = Field(gt=0)
    terminal_response_observed: bool = False
    output_progress_observations: int = Field(default=0, ge=0)
    stall_suspected_count: int = Field(default=0, ge=0)
    stall_recovered_count: int = Field(default=0, ge=0)
    maximum_no_progress_ms: int = Field(default=0, ge=0)
    stalled: bool = False

    @model_validator(mode="after")
    def validate_observation(self) -> Self:
        if self.stall_grace_seconds >= self.no_progress_seconds:
            raise ValueError(
                "response-finalization grace must be shorter than its ceiling"
            )
        if (self.mode == "enforced") != self.terminal_response_observed:
            raise ValueError(
                "enforced response finalization requires an observed terminal response"
            )
        if self.mode == "not_observed" and any(
            (
                self.output_progress_observations,
                self.stall_suspected_count,
                self.stall_recovered_count,
                self.maximum_no_progress_ms,
                self.stalled,
            )
        ):
            raise ValueError(
                "unobserved response finalization cannot contain progress evidence"
            )
        if self.stall_recovered_count > self.stall_suspected_count:
            raise ValueError("response-finalization recovery requires a suspicion")
        if self.stalled and self.mode != "enforced":
            raise ValueError("only enforced response finalization can stall")
        return self


class InvocationLifecycleEvidence(BaseModel):
    """Terminal lifecycle record shared by runtime and Planning invocations."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=5, ge=1, le=5)
    initialization_wait: tuple[InitializationWaitDiagnostic, ...] = Field(
        default=(),
        max_length=2,
        exclude_if=lambda value: not value,
    )
    transitions: tuple[InvocationLifecycleTransition, ...] = Field(min_length=2)
    initialization: InitializationLivenessEvidence
    response_finalization: ResponseFinalizationEvidence | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    shutdown: InvocationShutdownEvidence

    @model_validator(mode="after")
    def validate_lifecycle(self) -> Self:
        if self.schema_version < 5 and (
            "process_activity_observations" in self.initialization.model_fields_set
        ):
            raise ValueError(
                "legacy lifecycle evidence cannot contain process activity"
            )
        if self.schema_version < 4 and "initialization_wait" in self.model_fields_set:
            raise ValueError(
                "legacy lifecycle evidence cannot contain wait diagnostics"
            )
        reasons = [item.reason for item in self.initialization_wait]
        if reasons not in ([], ["suspected"], ["stalled"], ["suspected", "stalled"]):
            raise ValueError("initialization wait boundaries must occur once in order")
        if "stalled" in reasons and not self.initialization.stalled:
            raise ValueError("stall diagnostic requires an initialization stall")
        if (self.schema_version == 1) != (self.response_finalization is None):
            raise ValueError(
                "lifecycle schema v2 and later require response-finalization evidence"
            )
        if self.schema_version < 3 and (
            self.initialization.baseline_checkpoint is not None
            or self.initialization.baseline_matching_turn_count
        ):
            raise ValueError(
                "legacy lifecycle evidence cannot contain an invocation baseline"
            )
        if [item.sequence for item in self.transitions] != list(
            range(1, len(self.transitions) + 1)
        ):
            raise ValueError("invocation lifecycle sequence must be contiguous")
        elapsed = [item.elapsed_ms for item in self.transitions]
        if elapsed != sorted(elapsed):
            raise ValueError("invocation lifecycle elapsed time must be monotonic")
        if self.transitions[0].phase is not InvocationPhase.LAUNCHED:
            raise ValueError("invocation lifecycle must begin at launched")
        if self.transitions[-1].phase is not InvocationPhase.STOPPED:
            raise ValueError("invocation lifecycle must end at stopped")
        for previous, current in zip(
            self.transitions, self.transitions[1:], strict=False
        ):
            if current.phase not in _ALLOWED_PHASE_SUCCESSORS[previous.phase]:
                raise ValueError(
                    "invocation lifecycle contains an invalid phase transition: "
                    f"{previous.phase.value} -> {current.phase.value}"
                )
        terminal = self.transitions[-1]
        if terminal.stop_reason is not self.shutdown.reason:
            raise ValueError("terminal lifecycle reason must match shutdown evidence")
        collecting = tuple(
            item
            for item in self.transitions
            if item.phase is InvocationPhase.COLLECTING_EVIDENCE
        )
        if len(collecting) != 1:
            raise ValueError(
                "invocation lifecycle requires one evidence collection phase"
            )
        if collecting[0].stop_reason is not self.shutdown.reason:
            raise ValueError("evidence collection reason must match shutdown evidence")
        stopping = tuple(
            item for item in self.transitions if item.phase is InvocationPhase.STOPPING
        )
        if len(stopping) != 1 or stopping[0].stop_reason is not self.shutdown.reason:
            raise ValueError(
                "invocation lifecycle requires one matching stopping phase"
            )
        if any(
            item.stop_reason not in {None, self.shutdown.reason}
            for item in self.transitions
        ):
            raise ValueError("invocation lifecycle cannot change its stop reason")
        finalizing = tuple(
            item
            for item in self.transitions
            if item.phase is InvocationPhase.FINALIZING_RESPONSE
        )
        assert self.response_finalization is not None or self.schema_version == 1
        if self.schema_version >= 2:
            observed = self.response_finalization is not None and (
                self.response_finalization.terminal_response_observed
            )
            if observed != (len(finalizing) == 1):
                raise ValueError(
                    "terminal-response observation requires one finalizing phase"
                )
            if (
                self.shutdown.reason is InvocationStopReason.RESPONSE_FINALIZATION_STALL
            ) != bool(
                self.response_finalization and self.response_finalization.stalled
            ):
                raise ValueError(
                    "response-finalization stall reason must match guard evidence"
                )
        return self
