"""Execution liveness monitoring, heartbeat tracking, and stall detection."""

from __future__ import annotations

import os
import stat
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

import software_agent_team.execution
from software_agent_team.artifacts import ProviderLivenessEvidence
from software_agent_team.execution.constants import (
    COMPACTION_LINEAGE_ERROR,
    COMPACTION_LINEAGE_RECHECK_SECONDS,
    DEFAULT_CLOUD_PROVIDER_SILENCE_SECONDS,
    DEFAULT_LOCAL_PROVIDER_SILENCE_SECONDS,
    DEFAULT_PROVIDER_STALL_GRACE_SECONDS,
    INITIALIZATION_PROCESS_SAMPLES_PER_GRACE,
    MAX_INITIALIZATION_PROCESS_SAMPLE_SECONDS,
    PROVIDER_ACTIVITY_REPORT_SECONDS,
)
from software_agent_team.execution.models import (
    AgentExecutionActivity,
    AgentExecutionActivityHandler,
    AgentExecutionActivityKind,
    AgentExecutionError,
    AgentExecutionRequest,
    AgentExecutionTelemetry,
    _classify_tool_activity,
)
from software_agent_team.invocation_lifecycle import (
    InitializationCheckpoint,
    InitializationLivenessEvidence,
    InvocationLifecycleEvidence,
    InvocationLifecycleTransition,
    InvocationPhase,
    InvocationShutdownEvidence,
    InvocationStopReason,
    ResponseFinalizationEvidence,
)
from software_agent_team.openclaw_session_evidence import (
    OpenClawInitializationBaseline,
    OpenClawSessionEvidenceError,
    OpenClawToolActivity,
)
from software_agent_team.process_diagnostics import (
    InitializationWaitDiagnostic,
    initialization_process_activity_detected,
    snapshot_initialization_wait,
)
from software_agent_team.process_lifecycle import ProcessIdentity
from software_agent_team.submissions import AgentSubmissionEvidence
from software_agent_team.terminal_presentation import (
    InvocationPresentation,
    display_text,
    native_stream_run_id,
    stream_preview,
)

MonotonicClock = Callable[[], float]
WallClock = Callable[[], datetime]


def resolve_initialization_process_sample_interval(
    policy: InitializationLivenessPolicy,
) -> float:
    """Bound process sampling while retaining multiple observations per grace."""

    return min(
        MAX_INITIALIZATION_PROCESS_SAMPLE_SECONDS,
        policy.stall_grace_seconds / INITIALIZATION_PROCESS_SAMPLES_PER_GRACE,
    )


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


class ResponseFinalizationPolicy(BaseModel):
    """Infrastructure guard after a terminal response leaves model generation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    no_progress_seconds: float = Field(gt=0)
    stall_grace_seconds: float = Field(gt=0)
    source: str = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def require_a_warning_window(self) -> Self:
        if self.stall_grace_seconds >= self.no_progress_seconds:
            raise ValueError(
                "response-finalization grace must be shorter than its ceiling"
            )
        return self

    @property
    def suspect_after_seconds(self) -> float:
        """Return when finalization enters its visible diagnostic window."""

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


def _system_clock() -> datetime:
    return datetime.now(UTC)


def _decode_process_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _consume_private_raw_stream(
    path: Path, *, preview: bytearray | None = None
) -> bool:
    """Observe and discard a batch, optionally collecting a bounded live preview."""

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
        if preview is not None:
            preview.extend(os.read(descriptor, min(metadata.st_size, 256 * 1024)))
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
        InvocationPhase.FINALIZING_RESPONSE: (
            AgentExecutionActivityKind.INVOCATION_FINALIZING_RESPONSE
        ),
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
        response_finalization_policy: ResponseFinalizationPolicy,
        activity_handler: AgentExecutionActivityHandler | None,
        monotonic: MonotonicClock,
    ) -> None:
        self.request = request
        self.started_monotonic = started_monotonic
        self.process_grace_seconds = process_grace_seconds
        self.initialization_policy = initialization_policy
        self.response_finalization_policy = response_finalization_policy
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
        self.active_tool_count = 0
        self.completed_tool_count = 0
        self.initialization_wait: list[InitializationWaitDiagnostic] = []
        self.initialization_evidence = InitializationLivenessEvidence(
            mode="unavailable",
            policy_source=initialization_policy.source,
            no_progress_seconds=initialization_policy.no_progress_seconds,
            stall_grace_seconds=initialization_policy.stall_grace_seconds,
            degradation_reason="subprocess initialization was not observed",
        )
        self.response_finalization_evidence = ResponseFinalizationEvidence(
            mode="not_observed",
            policy_source=response_finalization_policy.source,
            no_progress_seconds=response_finalization_policy.no_progress_seconds,
            stall_grace_seconds=response_finalization_policy.stall_grace_seconds,
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
            if self.stop_reason is not None and phase not in {
                InvocationPhase.STOPPING,
                InvocationPhase.COLLECTING_EVIDENCE,
                InvocationPhase.STOPPED,
            }:
                return
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
                InvocationPhase.FINALIZING_RESPONSE,
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

    def tool_state(
        self,
        active_tool_count: int,
        completed_tool_count: int,
        *,
        now: float,
    ) -> None:
        desired_phase = (
            InvocationPhase.TOOL_ACTIVE
            if active_tool_count > 0
            else InvocationPhase.PROVIDER_WAIT
        )
        with self._lock:
            self.active_tool_count = active_tool_count
            self.completed_tool_count = completed_tool_count
            if (
                self.phase
                in {
                    InvocationPhase.STOPPING,
                    InvocationPhase.COLLECTING_EVIDENCE,
                    InvocationPhase.STOPPED,
                    InvocationPhase.FINALIZING_RESPONSE,
                }
                or self.phase is desired_phase
            ):
                # Session counters describe historical tool events, while the
                # active count is only the state observed at this snapshot. A
                # start and completion may therefore be coalesced into one
                # poll. Keep the events, but do not invent a repeated phase.
                return
        if desired_phase is InvocationPhase.TOOL_ACTIVE:
            self.transition(
                desired_phase,
                now=now,
                action=f"{active_tool_count} attributable tool operation(s) active",
            )
        else:
            self.transition(
                desired_phase,
                now=now,
                action="Attributable tools completed; waiting for provider activity",
            )

    def response_finalizing(self, *, now: float) -> None:
        """Transfer authority away from provider liveness exactly once."""

        with self._lock:
            if self.phase in {
                InvocationPhase.FINALIZING_RESPONSE,
                InvocationPhase.STOPPING,
                InvocationPhase.COLLECTING_EVIDENCE,
                InvocationPhase.STOPPED,
            }:
                return
        self.transition(
            InvocationPhase.FINALIZING_RESPONSE,
            now=now,
            action=(
                "Terminal provider response observed; OpenClaw is finalizing its "
                "result envelope and process"
            ),
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

    def set_response_finalization_evidence(
        self, evidence: ResponseFinalizationEvidence
    ) -> None:
        with self._lock:
            self.response_finalization_evidence = evidence

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
            initialization_wait=tuple(self.initialization_wait),
            initialization=self.initialization_evidence,
            response_finalization=self.response_finalization_evidence,
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
                active_tool_count=self.active_tool_count,
                completed_tool_count=self.completed_tool_count,
            )
        )

    def _emit(self, activity: AgentExecutionActivity) -> bool:
        # All observers publish through the lifecycle's serialization boundary.
        # Final session collection may still discover historical tools after a
        # stop request; retain those records/counters, not live-working events.
        with self._lock:
            if self.stop_reason is not None and activity.invocation_phase not in {
                InvocationPhase.STOPPING,
                InvocationPhase.COLLECTING_EVIDENCE,
                InvocationPhase.STOPPED,
            }:
                return True
            if self.activity_handler is None:
                return True
            try:
                self.activity_handler(activity)
            except Exception:
                return False
            return True


class _InitializationLivenessMonitor:
    """Enforce inactivity without mistaking activity for readiness."""

    def __init__(
        self,
        *,
        request: AgentExecutionRequest,
        policy: InitializationLivenessPolicy,
        state_dir: Path | None,
        baseline: OpenClawInitializationBaseline | None,
        baseline_error: bool,
        process_identity: ProcessIdentity | None,
        expected_uid: int,
        started_monotonic: float,
        lifecycle: _InvocationLifecycleRecorder,
    ) -> None:
        self.request = request
        self.policy = policy
        self.state_dir = state_dir
        self.baseline = baseline
        self.started_monotonic = started_monotonic
        self.last_progress = started_monotonic
        self.process_identity = process_identity
        self.expected_uid = expected_uid
        self.process_activity_observations = 0
        self.process_sample_interval = resolve_initialization_process_sample_interval(
            policy
        )
        self.next_process_sample = started_monotonic
        self.previous_process_snapshot: InitializationWaitDiagnostic | None = None
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
        elif baseline_error:
            self.degradation_reason = (
                "OpenClaw initialization baseline could not be attributed"
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
            observation = software_agent_team.execution.inspect_openclaw_initialization(
                state_dir=self.state_dir,
                agent_id=self.request.agent_id,
                session_key=self.request.session_key,
                prompt=self.request.prompt,
                baseline=self.baseline,
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
        if not self.ready:
            self._observe_process_activity(now)
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

    def mark_ready(
        self,
        checkpoint: InitializationCheckpoint,
        *,
        now: float,
    ) -> None:
        """Own the single transition from initialization to provider readiness."""

        if self.ready:
            return
        if checkpoint not in {
            InitializationCheckpoint.CURRENT_TURN,
            InitializationCheckpoint.PROVIDER_STREAM,
        }:
            raise AgentExecutionError(
                "provider readiness requires current-turn or stream evidence"
            )
        self._advance(checkpoint, now=now)

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
        baseline_checkpoint = (
            None if self.baseline is None else self.baseline.checkpoint
        )
        implied = (
            (checkpoint,)
            if (
                checkpoint is InitializationCheckpoint.PROVIDER_STREAM
                or (
                    baseline_checkpoint is not None
                    and order.index(checkpoint) <= order.index(baseline_checkpoint)
                )
            )
            else tuple(
                candidate
                for candidate in order
                if candidate is not InitializationCheckpoint.PROVIDER_STREAM
                and order.index(self.checkpoints[-1])
                < order.index(candidate)
                <= order.index(checkpoint)
                and (
                    baseline_checkpoint is None
                    or order.index(candidate) > order.index(baseline_checkpoint)
                )
            )
        )
        for candidate in implied:
            self.checkpoints.append(candidate)
            self.lifecycle.initialization_progress(candidate, now=now)
        self.last_progress = now
        if self.suspected:
            self._recover(now=now, checkpoint=checkpoint)
        if checkpoint in {
            InitializationCheckpoint.CURRENT_TURN,
            InitializationCheckpoint.PROVIDER_STREAM,
        }:
            self.ready = True
            self.lifecycle.provider_ready(checkpoint, now=now)

    def _observe_process_activity(self, now: float) -> None:
        """Renew inactivity from exact counters without granting readiness."""

        if self.process_identity is None or now < self.next_process_sample:
            return
        snapshot = snapshot_initialization_wait(
            self.process_identity,
            expected_uid=self.expected_uid,
            reason="suspected",
            elapsed_ms=self.lifecycle.elapsed_ms(now),
        )
        previous = self.previous_process_snapshot
        self.previous_process_snapshot = snapshot
        self.next_process_sample = now + self.process_sample_interval
        if previous is None or not initialization_process_activity_detected(
            previous,
            snapshot,
        ):
            return
        self.process_activity_observations += 1
        self.last_progress = now
        if self.suspected:
            self._recover(now=now, checkpoint=None)

    def _recover(
        self,
        *,
        now: float,
        checkpoint: InitializationCheckpoint | None,
    ) -> None:
        self.suspected = False
        self.stall_recovered_count += 1
        self.lifecycle.set_initialization_evidence(self.evidence())
        self.lifecycle.emit_initialization(
            AgentExecutionActivityKind.INITIALIZATION_STALL_RECOVERED,
            now=now,
            checkpoint=checkpoint,
        )

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
            baseline_checkpoint=(
                None if self.baseline is None else self.baseline.checkpoint
            ),
            baseline_matching_turn_count=(
                0 if self.baseline is None else self.baseline.matching_turn_count
            ),
            checkpoints=tuple(self.checkpoints),
            process_activity_observations=self.process_activity_observations,
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
        self.previous_progress_records = 0
        self.previous_tool_started = 0
        self.previous_tool_completed = 0
        self.active_tool_count = 0
        self.repeating_no_progress_poll = False
        self.raw_stream_observed = False
        self.session_observed = False
        self.provider_activity_observations = 0
        self.qualified_provider_activity_observations = 0
        self.stall_suspected_count = 0
        self.stall_recovered_count = 0
        self.maximum_inactivity_ms = 0
        self.suspected = False
        self.stalled = False
        self.terminal_response_observed = False
        self.degradation_reason: str | None = None
        self.compaction_lineage_error_since: float | None = None
        self.presentation: InvocationPresentation | None = None
        self._stream_session_id: str | None = None
        self._stream_runtime_run_id: str | None = None
        self._stream_text = ""

    def poll(self, now: float, *, enforce_stall: bool = True) -> bool:
        """Observe activity and optionally enforce the live silence boundary."""

        if self.last_activity is not None and not self.terminal_response_observed:
            inactivity_before_poll = max(0.0, now - self.last_activity)
            self.maximum_inactivity_ms = max(
                self.maximum_inactivity_ms,
                round(inactivity_before_poll * 1000),
            )
        trusted_activity = False
        stream_batch = bytearray()
        try:
            raw_activity = _consume_private_raw_stream(
                self.raw_stream_path,
                preview=stream_batch if self.activity_handler is not None else None,
            )
        except OSError:
            self._degrade("private provider-stream observer became unavailable", now)
            raw_activity = False
        if raw_activity:
            self.raw_stream_observed = True
            self.provider_activity_observations += 1
        if self._stream_runtime_run_id is None:
            self._stream_runtime_run_id = native_stream_run_id(bytes(stream_batch))

        try:
            session = software_agent_team.execution.inspect_openclaw_session_activity(
                state_dir=self.state_dir,
                agent_id=self.request.agent_id,
                session_key=self.request.session_key,
                prompt=self.request.prompt,
                baseline=self.initialization_monitor.baseline,
                include_presentation=self.activity_handler is not None,
                model=self.request.model,
            )
        except OpenClawSessionEvidenceError as error:
            # This adapter's errors contain only controller-owned labels, never
            # session content, tool arguments, or arbitrary executable names.
            if str(error) == COMPACTION_LINEAGE_ERROR:
                # OpenClaw writes the index, checkpoint, and successor segment
                # separately. One live poll can see that incomplete transition.
                # Wait briefly without granting activity or enforcing a stall;
                # terminal capture still validates the full chain strictly.
                if self.compaction_lineage_error_since is None:
                    self.compaction_lineage_error_since = now
            else:
                self._degrade(
                    f"OpenClaw session activity could not be attributed: {error}",
                    now,
                )
            session = None
        if session is not None:
            self.presentation = session.presentation
            if (
                self.presentation is not None
                and self.presentation.session_id is not None
            ):
                if self._stream_session_id != self.presentation.session_id:
                    self._stream_session_id = self.presentation.session_id
                    self._stream_text = ""
                self._stream_text = stream_preview(
                    bytes(stream_batch),
                    session_id=self.presentation.session_id,
                    previous=self._stream_text,
                    runtime_run_id=self._stream_runtime_run_id,
                )
                if self._stream_text and not session.terminal_response_observed:
                    self.presentation = self.presentation.model_copy(
                        update={
                            "model_text": display_text(
                                self._stream_text, limit=1000, tail=True
                            ),
                            "streaming": True,
                        }
                    )
            self.compaction_lineage_error_since = None
            first_session_observation = not self.session_observed
            self.session_observed = True
            if first_session_observation:
                # The exact current prompt in OpenClaw's current-turn record is
                # the earliest attributable checkpoint available to the SAT
                # subprocess adapter. Process/interpreter startup before this
                # point is not provider silence.
                self._start_lease(now, source="current_turn")
                trusted_activity = True
            progress_delta = (
                session.progress_record_count - self.previous_progress_records
            )
            if progress_delta < 0:
                self._degrade("OpenClaw session progress moved backwards", now)
            elif progress_delta > 0 and not session.repeating_no_progress_poll:
                trusted_activity = True
            started_delta = session.tool_started_count - self.previous_tool_started
            completed_delta = (
                session.tool_completed_count - self.previous_tool_completed
            )
            started_tools = session.started_tools[self.previous_tool_started :]
            completed_tools = session.completed_tools[self.previous_tool_completed :]
            self.previous_progress_records = session.progress_record_count
            self.previous_tool_started = session.tool_started_count
            self.previous_tool_completed = session.tool_completed_count
            self.active_tool_count = session.active_tool_count
            self.repeating_no_progress_poll = session.repeating_no_progress_poll
            if started_delta < 0 or completed_delta < 0:
                self._degrade("OpenClaw session activity moved backwards", now)
            else:
                # Publish every history delta against the same current session
                # snapshot. A poll can observe a tool start and completion at
                # once, so the event kind alone is not the current lifecycle
                # state.
                if started_delta > 0 or completed_delta > 0:
                    self.lifecycle.tool_state(
                        self.active_tool_count,
                        self.previous_tool_completed,
                        now=now,
                    )
                if len(started_tools) != started_delta:
                    self._degrade("OpenClaw tool-start metadata is inconsistent", now)
                else:
                    for tool_activity in started_tools:
                        self._emit(
                            AgentExecutionActivityKind.TOOL_STARTED,
                            now,
                            tool_activity=tool_activity,
                        )
                if len(completed_tools) != completed_delta:
                    self._degrade(
                        "OpenClaw tool-completion metadata is inconsistent",
                        now,
                    )
                else:
                    for tool_activity in completed_tools:
                        self._emit(
                            AgentExecutionActivityKind.TOOL_COMPLETED,
                            now,
                            tool_activity=tool_activity,
                        )
            if (
                session.terminal_response_observed
                and not self.terminal_response_observed
            ):
                self.terminal_response_observed = True
                trusted_activity = True
                self.lifecycle.response_finalizing(now=now)
        elif (
            self.compaction_lineage_error_since is not None
            and now - self.compaction_lineage_error_since
            >= COMPACTION_LINEAGE_RECHECK_SECONDS
        ):
            self._degrade(
                f"OpenClaw session activity could not be attributed: "
                f"{COMPACTION_LINEAGE_ERROR}",
                now,
            )

        qualified_raw_activity = raw_activity and not self.repeating_no_progress_poll
        if qualified_raw_activity:
            trusted_activity = True
            self.qualified_provider_activity_observations += 1
            self._start_lease(now, source="provider_stream")

        if trusted_activity:
            self.last_activity = now
            if self.suspected:
                self.suspected = False
                self.stall_recovered_count += 1
                self._emit(AgentExecutionActivityKind.STALL_RECOVERED, now)
            if qualified_raw_activity and (
                self.provider_activity_observations == 1
                or now - self.last_provider_report >= PROVIDER_ACTIVITY_REPORT_SECONDS
            ):
                self.last_provider_report = now
                self._emit(AgentExecutionActivityKind.PROVIDER_STREAM, now)

        if (
            not enforce_stall
            or self.degradation_reason is not None
            or self.compaction_lineage_error_since is not None
            or (self.active_tool_count > 0 and not self.repeating_no_progress_poll)
            or self.terminal_response_observed
        ):
            return False
        if self.last_activity is None:
            return False
        inactive = max(0.0, now - self.last_activity)
        if not self.suspected and inactive >= self.policy.suspect_after_seconds:
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
        if self.compaction_lineage_error_since is not None:
            self._degrade(
                f"OpenClaw session activity could not be attributed: "
                f"{COMPACTION_LINEAGE_ERROR}",
                now,
            )
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
        self.initialization_monitor.mark_ready(checkpoint, now=now)

    def _degrade(self, reason: str, now: float) -> None:
        if self.degradation_reason is not None:
            return
        self.degradation_reason = reason
        self._emit(AgentExecutionActivityKind.LIVENESS_DEGRADED, now)

    def _emit(
        self,
        kind: AgentExecutionActivityKind,
        now: float,
        *,
        tool_activity: OpenClawToolActivity | None = None,
    ) -> None:
        if self.activity_handler is None:
            return
        inactivity = (
            0.0 if self.last_activity is None else max(0.0, now - self.last_activity)
        )
        tool_classification = (
            None if tool_activity is None else _classify_tool_activity(tool_activity)
        )
        activity = AgentExecutionActivity(
            kind=kind,
            agent_id=self.request.agent_id,
            session_key=self.request.session_key,
            model=self.request.model,
            elapsed_ms=max(0, round((now - self.started_monotonic) * 1000)),
            trusted_activity_count=(
                self.qualified_provider_activity_observations
                + self.previous_progress_records
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
            tool_action_class=(
                None if tool_classification is None else tool_classification[0]
            ),
            tool_target_class=(
                None if tool_classification is None else tool_classification[1]
            ),
            tool_detail=(
                None if tool_classification is None else tool_classification[2]
            ),
            presentation=(
                self.presentation.model_copy(
                    update={"tool": tool_activity.presentation}
                )
                if self.presentation is not None and tool_activity is not None
                else self.presentation
            ),
        )
        if not self.lifecycle._emit(activity):
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
            terminal_response_observed=self.terminal_response_observed,
            degradation_reason=self.degradation_reason,
        )


class _ResponseFinalizationMonitor:
    """Guard OpenClaw result serialization after model generation is complete."""

    def __init__(
        self,
        *,
        request: AgentExecutionRequest,
        policy: ResponseFinalizationPolicy,
        started_monotonic: float,
        activity_handler: AgentExecutionActivityHandler | None,
    ) -> None:
        self.request = request
        self.policy = policy
        self.started_monotonic = started_monotonic
        self.activity_handler = activity_handler
        self.terminal_response_observed = False
        self.last_progress: float | None = None
        self.stdout_characters = 0
        self.stderr_characters = 0
        self.output_progress_observations = 0
        self.stall_suspected_count = 0
        self.stall_recovered_count = 0
        self.maximum_no_progress_ms = 0
        self.suspected = False
        self.stalled = False

    def observe_terminal(
        self,
        now: float,
        *,
        stdout: str,
        stderr: str,
    ) -> None:
        """Begin finalization only from an attributable final assistant record."""

        if self.terminal_response_observed:
            return
        self.terminal_response_observed = True
        self.last_progress = now
        self.stdout_characters = len(stdout)
        self.stderr_characters = len(stderr)

    def poll(
        self,
        now: float,
        *,
        stdout: str,
        stderr: str,
        enforce_stall: bool = True,
    ) -> bool:
        """Renew from process output and enforce only post-response no-progress."""

        if not self.terminal_response_observed:
            return False
        if self.stalled:
            return enforce_stall
        assert self.last_progress is not None
        output_progress = (
            len(stdout) > self.stdout_characters or len(stderr) > self.stderr_characters
        )
        self.stdout_characters = max(self.stdout_characters, len(stdout))
        self.stderr_characters = max(self.stderr_characters, len(stderr))
        if output_progress:
            self.last_progress = now
            self.output_progress_observations += 1
            self._emit(AgentExecutionActivityKind.FINALIZATION_PROGRESS, now)
            if self.suspected:
                self.suspected = False
                self.stall_recovered_count += 1
                self._emit(
                    AgentExecutionActivityKind.FINALIZATION_STALL_RECOVERED,
                    now,
                )
        inactive = max(0.0, now - self.last_progress)
        self.maximum_no_progress_ms = max(
            self.maximum_no_progress_ms,
            round(inactive * 1000),
        )
        if not enforce_stall:
            return False
        if not self.suspected and inactive >= self.policy.suspect_after_seconds:
            self.suspected = True
            self.stall_suspected_count += 1
            self._emit(
                AgentExecutionActivityKind.FINALIZATION_STALL_SUSPECTED,
                now,
            )
        if self.suspected and inactive >= self.policy.no_progress_seconds:
            self.stalled = True
            self._emit(
                AgentExecutionActivityKind.RESPONSE_FINALIZATION_STALLED,
                now,
            )
            return True
        return False

    def finalize(
        self,
        now: float,
        *,
        stdout: str,
        stderr: str,
    ) -> ResponseFinalizationEvidence:
        """Collect terminal finalization counters without changing the outcome."""

        self.poll(
            now,
            stdout=stdout,
            stderr=stderr,
            enforce_stall=False,
        )
        return self.evidence()

    def _emit(self, kind: AgentExecutionActivityKind, now: float) -> None:
        if self.activity_handler is None:
            return
        assert self.last_progress is not None
        inactivity = max(0.0, now - self.last_progress)
        try:
            self.activity_handler(
                AgentExecutionActivity(
                    kind=kind,
                    agent_id=self.request.agent_id,
                    session_key=self.request.session_key,
                    model=self.request.model,
                    elapsed_ms=max(
                        0,
                        round((now - self.started_monotonic) * 1000),
                    ),
                    trusted_activity_count=self.output_progress_observations,
                    inactivity_ms=max(0, round(inactivity * 1000)),
                    silence_seconds=self.policy.no_progress_seconds,
                    stall_grace_seconds=self.policy.stall_grace_seconds,
                    policy_source=self.policy.source,
                )
            )
        except Exception:
            return

    def evidence(self) -> ResponseFinalizationEvidence:
        """Freeze content-free response-finalization evidence."""

        return ResponseFinalizationEvidence(
            mode=("enforced" if self.terminal_response_observed else "not_observed"),
            policy_source=self.policy.source,
            no_progress_seconds=self.policy.no_progress_seconds,
            stall_grace_seconds=self.policy.stall_grace_seconds,
            terminal_response_observed=self.terminal_response_observed,
            output_progress_observations=self.output_progress_observations,
            stall_suspected_count=self.stall_suspected_count,
            stall_recovered_count=self.stall_recovered_count,
            maximum_no_progress_ms=self.maximum_no_progress_ms,
            stalled=self.stalled,
        )
