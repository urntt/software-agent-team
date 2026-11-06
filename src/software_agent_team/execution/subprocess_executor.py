"""OpenClaw child subprocess executor with lease monitoring and liveness tracking."""

from __future__ import annotations

import hashlib
import math
import os
import secrets
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from software_agent_team.artifacts import (
    AgentRuntimeFailureCode,
    AgentToolEvidenceStatus,
    ProviderLivenessEvidence,
)
from software_agent_team.docker_engine import verify_bound_docker_engine
from software_agent_team.execution.constants import (
    DEFAULT_INITIALIZATION_NO_PROGRESS_SECONDS,
    DEFAULT_INITIALIZATION_STALL_GRACE_SECONDS,
    DEFAULT_LIVENESS_POLL_SECONDS,
    DEFAULT_PROCESS_SHUTDOWN_GRACE_SECONDS,
    DEFAULT_RESPONSE_FINALIZATION_NO_PROGRESS_SECONDS,
    DEFAULT_RESPONSE_FINALIZATION_STALL_GRACE_SECONDS,
)
from software_agent_team.execution.liveness import (
    InitializationLivenessPolicy,
    MonotonicClock,
    ProviderLivenessPolicy,
    ResponseFinalizationPolicy,
    WallClock,
    _decode_process_output,
    _InitializationLivenessMonitor,
    _InvocationLifecycleRecorder,
    _ProviderLivenessMonitor,
    _ResponseFinalizationMonitor,
    _system_clock,
    resolve_provider_liveness_policy,
)
from software_agent_team.execution.models import (
    AgentExecutionActivity,
    AgentExecutionActivityHandler,
    AgentExecutionActivityKind,
    AgentExecutionError,
    AgentExecutionRequest,
    AgentExecutionResult,
    AgentExecutionStatus,
    AgentExecutionTelemetry,
    AgentTokenUsage,
)
from software_agent_team.execution.parsers import (
    _attributable_historical_tool_warning,
    _canonical_model_reference,
    _OpenClawResponse,
    _OpenClawStreamFailure,
    _parse_openclaw_compaction_failure,
    _parse_openclaw_payload,
    _parse_openclaw_provider_failure,
    _parse_openclaw_stream_failure,
)
from software_agent_team.invocation_lifecycle import InvocationStopReason
from software_agent_team.openclaw_session_evidence import (
    CapturedOpenClawTerminalDiagnostic,
    CapturedOpenClawTerminalResponse,
    CapturedOpenClawToolEvidence,
    OpenClawInitializationBaseline,
    OpenClawInvocationTerminalState,
    OpenClawSessionEvidenceError,
    capture_openclaw_initialization_baseline,
    capture_openclaw_invocation_diagnostic,
    capture_openclaw_terminal_diagnostic,
    capture_openclaw_terminal_response,
    capture_openclaw_tool_evidence,
)
from software_agent_team.process_diagnostics import snapshot_initialization_wait
from software_agent_team.process_lifecycle import (
    InvocationProcessLease,
    ProcessIdentity,
    ProcessLeaseStore,
    ProcessLifecycleError,
    read_linux_process_identity,
)
from software_agent_team.submissions import (
    AgentSubmissionEvidence,
    AgentSubmissionStatus,
    SubmissionFileCapture,
    canonical_json_bytes,
    capture_submission_file,
    rejected_submission_evidence,
    validate_submission_capture,
)

ProcessRunner = Callable[..., subprocess.CompletedProcess[str]]
ActiveProcess = tuple[AgentExecutionRequest, subprocess.Popen[str]]


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
        response_finalization_policy: ResponseFinalizationPolicy | None = None,
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
        self.response_finalization_policy = response_finalization_policy or (
            ResponseFinalizationPolicy(
                no_progress_seconds=(DEFAULT_RESPONSE_FINALIZATION_NO_PROGRESS_SECONDS),
                stall_grace_seconds=(DEFAULT_RESPONSE_FINALIZATION_STALL_GRACE_SECONDS),
                source=("SAT/OpenClaw terminal-response result finalization progress"),
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
            response_finalization_policy=self.response_finalization_policy,
            activity_handler=activity_handler,
            monotonic=self.monotonic,
        )
        submission_binding_sha256: str | None = None
        submission_capture: SubmissionFileCapture | None = None
        initialization_baseline: OpenClawInitializationBaseline | None = None
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
                    canonical_json_bytes(
                        request.submission_contract.transport_schema()
                    ).decode("utf-8"),
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
                    "SAT_ARTIFACT_SUBMISSION_PARAMETERS_SHA256": (
                        request.submission_contract.transport_schema_sha256
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
                    (
                        completed,
                        stop_reason,
                        liveness,
                        initialization_baseline,
                    ) = self._run_interruptible_process(
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
        if stop_reason is InvocationStopReason.RESPONSE_FINALIZATION_STALL:
            return self._finalization_stall_result(
                request=request,
                command=command,
                started_at=started_at,
                started_monotonic=started_monotonic,
                exit_code=completed.returncode,
                stdout=stdout,
                stderr=stderr,
                provider_liveness=liveness,
                lifecycle=lifecycle,
                initialization_baseline=initialization_baseline,
                submission_binding_sha256=submission_binding_sha256,
                submission_capture=submission_capture,
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
            compaction_failure = _parse_openclaw_compaction_failure(
                stderr,
                requested_model=request.model,
            )
            if compaction_failure is not None or (
                liveness is not None and liveness.terminal_response_observed
            ):
                return self._terminal_response_recovery_result(
                    request=request,
                    command=command,
                    started_at=started_at,
                    started_monotonic=started_monotonic,
                    exit_code=completed.returncode,
                    stdout=stdout,
                    stderr=stderr,
                    provider_liveness=liveness,
                    lifecycle=lifecycle,
                    initialization_baseline=initialization_baseline,
                    submission_binding_sha256=submission_binding_sha256,
                    submission_capture=submission_capture,
                    failure_status=AgentExecutionStatus.PROCESS_FAILED,
                    failure_error=(
                        f"OpenClaw exited with status {completed.returncode}"
                        if compaction_failure is None
                        else compaction_failure.error
                    ),
                    stop_reason=InvocationStopReason.PROCESS_FAILURE,
                    runtime_failure_code=(
                        None
                        if compaction_failure is None
                        else AgentRuntimeFailureCode.OPENCLAW_COMPACTION_TIMEOUT
                    ),
                    runtime_failure_payload=(
                        None
                        if compaction_failure is None
                        else compaction_failure.response()
                    ),
                )
            provider_failure = _parse_openclaw_provider_failure(
                stderr,
                requested_model=request.model,
            ) or _parse_openclaw_stream_failure(
                stderr,
                requested_model=request.model,
            )
            telemetry = self._telemetry(
                request=request,
                command=command,
                started_at=started_at,
                started_monotonic=started_monotonic,
                exit_code=completed.returncode,
                stdout=stdout,
                stderr=stderr,
                payload=(
                    None if provider_failure is None else provider_failure.response()
                ),
                provider_liveness=liveness,
                runtime_failure_code=(
                    AgentRuntimeFailureCode.OPENCLAW_MODEL_STREAM_TERMINATED
                    if isinstance(provider_failure, _OpenClawStreamFailure)
                    else None
                ),
            )
            return self._finalize_lifecycle_result(
                AgentExecutionResult(
                    status=(
                        AgentExecutionStatus.PROCESS_FAILED
                        if provider_failure is None
                        else AgentExecutionStatus.PROVIDER_FAILED
                    ),
                    error=(
                        f"OpenClaw exited with status {completed.returncode}"
                        if provider_failure is None
                        else provider_failure.error
                    ),
                    telemetry=telemetry,
                ),
                lifecycle=lifecycle,
                reason=(
                    InvocationStopReason.PROCESS_FAILURE
                    if provider_failure is None
                    else InvocationStopReason.PROVIDER_FAILURE
                ),
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
                if (
                    submission_evidence.diagnostic_code == "submission_missing"
                    and captured_tools is not None
                    and captured_tools.tool_calls
                    and captured_tools.terminal_state
                    in {
                        OpenClawInvocationTerminalState.TOOL_RESULT,
                        OpenClawInvocationTerminalState.ASSISTANT_RESPONSE,
                    }
                ):
                    final_tool = captured_tools.tool_calls[-1]
                    ended_after_tool_result = (
                        captured_tools.terminal_state
                        is OpenClawInvocationTerminalState.TOOL_RESULT
                    )
                    submission_evidence = rejected_submission_evidence(
                        request.submission_contract,
                        binding_sha256=submission_binding_sha256,
                        status=AgentSubmissionStatus.MISSING,
                        code=(
                            "upstream_incomplete_after_tool_result"
                            if ended_after_tool_result
                            else "upstream_incomplete_after_terminal_response"
                        ),
                        detail=(
                            "the attributable OpenClaw turn ended after the paired "
                            f"{final_tool.tool_name} tool result before the required "
                            "terminal submission"
                            if ended_after_tool_result
                            else (
                                "the attributable OpenClaw turn ended with an "
                                "assistant response after tool use but before the "
                                "required terminal submission"
                            )
                        ),
                    )
                    return self._finalize_lifecycle_result(
                        AgentExecutionResult(
                            status=AgentExecutionStatus.UPSTREAM_INCOMPLETE,
                            error=(
                                "OpenClaw ended the tool-bearing invocation before "
                                "the required typed submission"
                            ),
                            telemetry=telemetry,
                            submission_evidence=submission_evidence,
                        ),
                        lifecycle=lifecycle,
                        reason=InvocationStopReason.UPSTREAM_INCOMPLETE,
                    )
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
            if payload.has_error_payload and not _attributable_historical_tool_warning(
                payload, captured_tools, submission_evidence
            ):
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
        """Claim stop authority for one Agent and return accepted invocations."""

        self._require_user_stop_reason(reason)
        with self._process_lock:
            matches = [
                (session_key, process, self._active_lifecycles[session_key])
                for session_key, (request, process) in self._active_processes.items()
                if request.agent_id == agent_id and process.poll() is None
            ]
        return self._claim_interrupts(matches, reason=reason)

    def interrupt_all(
        self,
        *,
        reason: InvocationStopReason = InvocationStopReason.USER_CANCEL,
    ) -> int:
        """Claim stop authority for all active SAT-owned invocations."""

        self._require_user_stop_reason(reason)
        with self._process_lock:
            matches = [
                (session_key, process, self._active_lifecycles[session_key])
                for session_key, (_, process) in self._active_processes.items()
                if process.poll() is None
            ]
        return self._claim_interrupts(matches, reason=reason)

    def _claim_interrupts(
        self,
        matches: Sequence[
            tuple[str, subprocess.Popen[str], _InvocationLifecycleRecorder]
        ],
        *,
        reason: InvocationStopReason,
    ) -> int:
        """Stop matched processes whose lifecycle still accepts this reason."""

        accepted = 0
        for session_key, process, lifecycle in matches:
            if not self._begin_stop(
                process,
                lifecycle,
                reason=reason,
                now=self.monotonic(),
            ):
                continue
            accepted += 1
            with self._process_lock:
                current = self._active_processes.get(session_key)
                if current is not None and current[1] is process:
                    self._interrupt_requests[session_key] = reason
        return accepted

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
        OpenClawInitializationBaseline | None,
    ]:
        """Run one process whose exact session may be interrupted by control input."""

        state_dir = self._state_directory()
        initialization_baseline: OpenClawInitializationBaseline | None = None
        initialization_baseline_error = False
        if state_dir is not None:
            try:
                initialization_baseline = capture_openclaw_initialization_baseline(
                    state_dir=state_dir,
                    agent_id=request.agent_id,
                    session_key=request.session_key,
                    prompt=request.prompt,
                )
            except OpenClawSessionEvidenceError:
                initialization_baseline_error = True
        verify_bound_docker_engine()
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
        try:
            diagnostic_identity = read_linux_process_identity(process.pid)
        except ProcessLifecycleError:
            diagnostic_identity = None
        lifecycle.process_launched(
            now=process_started,
            process_group_targeted=os.name == "posix",
        )
        initialization_monitor = _InitializationLivenessMonitor(
            request=request,
            policy=self.initialization_policy,
            state_dir=state_dir,
            baseline=initialization_baseline,
            baseline_error=initialization_baseline_error,
            process_identity=diagnostic_identity,
            expected_uid=os.getuid(),
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
        finalization_monitor = _ResponseFinalizationMonitor(
            request=request,
            policy=self.response_finalization_policy,
            started_monotonic=process_started,
            activity_handler=activity_handler,
        )
        process_lease: InvocationProcessLease | None = None
        stdout = ""
        stderr = ""
        try:
            if self.process_lease_store is not None:
                process_lease = self.process_lease_store.acquire(
                    run_id=request.run_id,
                    agent_id=request.agent_id,
                    session_key=request.session_key,
                    child_pid=process.pid,
                    command=command,
                )
            with self._process_lock:
                self._active_processes[request.session_key] = (request, process)
                self._active_lifecycles[request.session_key] = lifecycle
            while True:
                try:
                    stdout, stderr = process.communicate(
                        timeout=self.liveness_poll_seconds
                    )
                    now = self.monotonic()
                    initialization_monitor.finalize(now)
                    if liveness_monitor is not None:
                        # Initialization owns launch-to-turn ordering. Once it
                        # has advanced, collect final provider counters without
                        # retroactively turning a returned response into a stall.
                        liveness_monitor.finalize(now)
                        if liveness_monitor.terminal_response_observed:
                            finalization_monitor.observe_terminal(
                                now,
                                stdout=stdout,
                                stderr=stderr,
                            )
                    finalization_monitor.finalize(
                        now,
                        stdout=stdout,
                        stderr=stderr,
                    )
                    break
                except subprocess.TimeoutExpired as error:
                    now = self.monotonic()
                    partial_stdout = _decode_process_output(error.stdout)
                    partial_stderr = _decode_process_output(error.stderr)
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
                    initialization_stalled = initialization_monitor.poll(now)
                    for boundary, should_capture in (
                        ("suspected", initialization_monitor.suspected),
                        ("stalled", initialization_stalled),
                    ):
                        if should_capture and not any(
                            item.reason == boundary
                            for item in lifecycle.initialization_wait
                        ):
                            lifecycle.initialization_wait.append(
                                snapshot_initialization_wait(
                                    diagnostic_identity,
                                    expected_uid=os.getuid(),
                                    reason=boundary,
                                    elapsed_ms=lifecycle.elapsed_ms(now),
                                )
                            )
                    if initialization_stalled:
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
                    provider_stalled = (
                        False
                        if liveness_monitor is None
                        else liveness_monitor.poll(now)
                    )
                    if (
                        liveness_monitor is not None
                        and liveness_monitor.terminal_response_observed
                    ):
                        finalization_monitor.observe_terminal(
                            now,
                            stdout=partial_stdout,
                            stderr=partial_stderr,
                        )
                    if finalization_monitor.poll(
                        now,
                        stdout=partial_stdout,
                        stderr=partial_stderr,
                    ):
                        self._begin_stop(
                            process,
                            lifecycle,
                            reason=(InvocationStopReason.RESPONSE_FINALIZATION_STALL),
                            now=now,
                        )
                        stdout, stderr = self._await_process_stop(process, lifecycle)
                        break
                    if provider_stalled:
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
        except KeyboardInterrupt:
            # A terminal SIGINT interrupts communicate on the main thread. It
            # must converge through the same stop/result path as runtime controls,
            # rather than bypassing evidence capture and releasing a live lease.
            self._begin_stop(
                process,
                lifecycle,
                reason=InvocationStopReason.USER_INTERRUPT,
                now=self.monotonic(),
            )
            stdout, stderr = self._await_process_stop(process, lifecycle)
        except BaseException:
            self._begin_stop(
                process,
                lifecycle,
                reason=InvocationStopReason.PROCESS_FAILURE,
                now=self.monotonic(),
            )
            stdout, stderr = self._await_process_stop(process, lifecycle)
            raise
        finally:
            with self._process_lock:
                self._active_processes.pop(request.session_key, None)
                self._active_lifecycles.pop(request.session_key, None)
                self._interrupt_requests.pop(request.session_key, None)
            if self._release_process_lease(
                request=request,
                process=process,
                acquired_lease=process_lease,
                observed_child=diagnostic_identity,
            ):
                lifecycle.mark_process_lease_released()
            lifecycle.set_initialization_evidence(
                initialization_monitor.finalize(self.monotonic())
            )
            lifecycle.set_response_finalization_evidence(
                finalization_monitor.finalize(
                    self.monotonic(),
                    stdout=stdout,
                    stderr=stderr,
                )
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
            initialization_baseline,
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
    ) -> bool:
        """Atomically claim the terminal reason and signal only its winner."""

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
        return first

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
            try:
                if remaining <= 0:
                    self._kill_process(process)
                    lifecycle.mark_kill_sent()
                    return process.communicate()
                return process.communicate(
                    timeout=min(self.liveness_poll_seconds, remaining)
                )
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                # Repeated Ctrl+C cannot abandon an owned child or renew grace.
                continue

    def _release_process_lease(
        self,
        *,
        request: AgentExecutionRequest,
        process: subprocess.Popen[str],
        acquired_lease: InvocationProcessLease | None,
        observed_child: ProcessIdentity | None,
    ) -> bool:
        """Release the exact lease even if acquisition was interrupted after publish."""

        store = self.process_lease_store
        if store is None:
            return True
        if acquired_lease is not None:
            store.release(acquired_lease)
            return True
        try:
            candidates = [
                item.lease
                for item in store.inspect().processes
                if item.lease.run_id == request.run_id
                and item.lease.agent_id == request.agent_id
                and item.lease.session_key == request.session_key
                and item.lease.child.pid == process.pid
            ]
        except (OSError, ProcessLifecycleError):
            return False
        if not candidates:
            return True
        if observed_child is None:
            return False
        exact = [lease for lease in candidates if lease.child == observed_child]
        if len(exact) != 1 or len(candidates) != 1:
            return False
        try:
            store.release(exact[0])
        except (OSError, ProcessLifecycleError):
            return False
        return True

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
        if request.thinking_level is not None:
            command.extend(["--thinking", request.thinking_level])
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
        invalid_session_transcript_sha256: str | None = None,
        invalid_session_record_count: int | None = None,
        provider_liveness: ProviderLivenessEvidence | None = None,
        runtime_failure_code: AgentRuntimeFailureCode | None = None,
    ) -> AgentExecutionTelemetry:
        finished_at = self.clock()
        elapsed = max(0, round((self.monotonic() - started_monotonic) * 1000))
        return AgentExecutionTelemetry(
            role=request.role,
            agent_id=request.agent_id,
            capability=request.capability,
            specialization=request.specialization,
            session_key=request.session_key,
            command=command,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=elapsed,
            openclaw_duration_ms=None if payload is None else payload.duration_ms,
            exit_code=exit_code,
            timed_out=timed_out,
            interrupted=interrupted,
            runtime_failure_code=runtime_failure_code,
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
                invalid_session_transcript_sha256
                if captured_tools is None
                else captured_tools.transcript_sha256
            ),
            session_record_count=(
                invalid_session_record_count
                if captured_tools is None
                else captured_tools.record_count
            ),
            tool_calls=(() if captured_tools is None else captured_tools.tool_calls),
            runtime_rejections=(
                () if captured_tools is None else captured_tools.runtime_rejections
            ),
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

    def _finalization_stall_result(
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
        lifecycle: _InvocationLifecycleRecorder,
        initialization_baseline: OpenClawInitializationBaseline | None,
        submission_binding_sha256: str | None,
        submission_capture: SubmissionFileCapture | None,
    ) -> AgentExecutionResult:
        """Recover authoritative typed output while retaining wrapper failure."""

        return self._terminal_response_recovery_result(
            request=request,
            command=command,
            started_at=started_at,
            started_monotonic=started_monotonic,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            provider_liveness=provider_liveness,
            lifecycle=lifecycle,
            initialization_baseline=initialization_baseline,
            submission_binding_sha256=submission_binding_sha256,
            submission_capture=submission_capture,
            failure_status=AgentExecutionStatus.RESPONSE_FINALIZATION_STALLED,
            failure_error=(
                "OpenClaw received a terminal provider response but made no "
                "observable result-finalization progress through the visible "
                "diagnostic grace period"
            ),
            stop_reason=InvocationStopReason.RESPONSE_FINALIZATION_STALL,
        )

    def _terminal_response_recovery_result(
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
        lifecycle: _InvocationLifecycleRecorder,
        initialization_baseline: OpenClawInitializationBaseline | None,
        submission_binding_sha256: str | None,
        submission_capture: SubmissionFileCapture | None,
        failure_status: AgentExecutionStatus,
        failure_error: str,
        stop_reason: InvocationStopReason,
        runtime_failure_code: AgentRuntimeFailureCode | None = None,
        runtime_failure_payload: _OpenClawResponse | None = None,
    ) -> AgentExecutionResult:
        """Recover attributable terminal evidence before classifying failure."""

        # The paired compaction terminal is the final runtime failure. Earlier
        # transport retries in the same stderr stream must not reclassify this
        # process failure as a provider outage and trigger model fallback.
        provider_failure = (
            None
            if runtime_failure_code
            is AgentRuntimeFailureCode.OPENCLAW_COMPACTION_TIMEOUT
            else (
                _parse_openclaw_provider_failure(stderr, requested_model=request.model)
                or _parse_openclaw_stream_failure(
                    stderr,
                    requested_model=request.model,
                )
            )
        )
        if isinstance(provider_failure, _OpenClawStreamFailure):
            runtime_failure_code = (
                AgentRuntimeFailureCode.OPENCLAW_MODEL_STREAM_TERMINATED
            )
        effective_failure_status = (
            failure_status
            if provider_failure is None
            else AgentExecutionStatus.PROVIDER_FAILED
        )
        effective_failure_error = (
            failure_error if provider_failure is None else provider_failure.error
        )
        effective_stop_reason = (
            stop_reason
            if provider_failure is None
            else InvocationStopReason.PROVIDER_FAILURE
        )

        def failure_result(
            *,
            payload: _OpenClawResponse | None = None,
            captured_tools: CapturedOpenClawToolEvidence | None = None,
            tool_evidence_error: str | None = None,
            invalid_session_transcript_sha256: str | None = None,
            invalid_session_record_count: int | None = None,
            submission_evidence: AgentSubmissionEvidence | None = None,
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
                payload=payload,
                captured_tools=captured_tools,
                tool_evidence_error=tool_evidence_error,
                invalid_session_transcript_sha256=(invalid_session_transcript_sha256),
                invalid_session_record_count=invalid_session_record_count,
                runtime_failure_code=runtime_failure_code,
            )
            return self._finalize_lifecycle_result(
                AgentExecutionResult(
                    status=effective_failure_status,
                    error=effective_failure_error,
                    telemetry=telemetry,
                    submission_evidence=submission_evidence,
                ),
                lifecycle=lifecycle,
                reason=effective_stop_reason,
            )

        contract = request.submission_contract
        state_dir = self._state_directory()
        compaction_timeout = (
            runtime_failure_code is AgentRuntimeFailureCode.OPENCLAW_COMPACTION_TIMEOUT
        )
        if state_dir is None or (
            not compaction_timeout
            and (
                provider_liveness is None
                or not provider_liveness.terminal_response_observed
            )
        ):
            return failure_result(
                payload=(
                    runtime_failure_payload
                    if provider_failure is None
                    else provider_failure.response()
                )
            )

        if compaction_timeout:
            assert runtime_failure_code is not None
            return self._compaction_failure_diagnostic_result(
                request=request,
                state_dir=state_dir,
                command=command,
                started_at=started_at,
                started_monotonic=started_monotonic,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                provider_liveness=provider_liveness,
                lifecycle=lifecycle,
                initialization_baseline=initialization_baseline,
                submission_binding_sha256=submission_binding_sha256,
                submission_capture=submission_capture,
                failure_status=effective_failure_status,
                failure_error=effective_failure_error,
                stop_reason=effective_stop_reason,
                runtime_failure_code=runtime_failure_code,
                runtime_failure_payload=runtime_failure_payload,
                terminal_evidence_error=(
                    "OpenClaw compaction timeout has no trusted terminal response"
                ),
            )

        def terminal_payload(
            terminal: (
                CapturedOpenClawTerminalDiagnostic | CapturedOpenClawTerminalResponse
            ),
        ) -> _OpenClawResponse:
            usage_values = (
                terminal.input_tokens,
                terminal.output_tokens,
                terminal.cache_read_tokens,
                terminal.cache_write_tokens,
                terminal.reasoning_tokens,
                terminal.total_tokens,
            )
            provider = (
                provider_failure.provider
                if provider_failure is not None
                else terminal.provider
            )
            model = (
                provider_failure.model
                if provider_failure is not None
                else terminal.model
            )
            return _OpenClawResponse(
                visible_texts=(),
                provider_failed=provider_failure is not None,
                session_id=terminal.session_id,
                provider=provider,
                model=_canonical_model_reference(provider=provider, model=model),
                usage=(
                    None
                    if all(value is None for value in usage_values)
                    else AgentTokenUsage(
                        input_tokens=terminal.input_tokens,
                        output_tokens=terminal.output_tokens,
                        cache_read_tokens=terminal.cache_read_tokens,
                        cache_write_tokens=terminal.cache_write_tokens,
                        reasoning_tokens=terminal.reasoning_tokens,
                        total_tokens=terminal.total_tokens,
                    )
                ),
            )

        try:
            terminal = capture_openclaw_terminal_response(
                state_dir=state_dir,
                agent_id=request.agent_id,
                session_key=request.session_key,
                prompt=request.prompt,
                baseline=initialization_baseline,
            )
        except OpenClawSessionEvidenceError as error:
            evidence_error = str(error)
            try:
                diagnostic = capture_openclaw_terminal_diagnostic(
                    state_dir=state_dir,
                    agent_id=request.agent_id,
                    session_key=request.session_key,
                    prompt=request.prompt,
                    baseline=initialization_baseline,
                )
            except OpenClawSessionEvidenceError:
                return failure_result(
                    payload=(
                        None
                        if provider_failure is None
                        else provider_failure.response()
                    ),
                    tool_evidence_error=evidence_error,
                )
            submission_evidence = None
            if (
                contract is not None
                and submission_binding_sha256 is not None
                and submission_capture is not None
            ):
                _, submission_evidence = validate_submission_capture(
                    contract,
                    binding_sha256=submission_binding_sha256,
                    capture=submission_capture,
                    tool_calls=(),
                    tool_evidence_error=evidence_error,
                    invalid_submission_tool_call_observed=(
                        diagnostic.submission_tool_call_observed
                    ),
                )
            return failure_result(
                payload=terminal_payload(diagnostic),
                tool_evidence_error=evidence_error,
                invalid_session_transcript_sha256=(diagnostic.transcript_sha256),
                invalid_session_record_count=diagnostic.record_count,
                submission_evidence=submission_evidence,
            )

        captured_tools = terminal.tool_evidence
        payload = terminal_payload(terminal)
        if (
            contract is None
            or submission_binding_sha256 is None
            or submission_capture is None
        ):
            return failure_result(payload=payload, captured_tools=captured_tools)
        semantic_submission, submission_evidence = validate_submission_capture(
            contract,
            binding_sha256=submission_binding_sha256,
            capture=submission_capture,
            tool_calls=captured_tools.tool_calls,
            tool_evidence_error=None,
        )
        if semantic_submission is None:
            return failure_result(
                payload=payload,
                captured_tools=captured_tools,
                submission_evidence=submission_evidence,
            )

        telemetry = self._telemetry(
            request=request,
            command=command,
            started_at=started_at,
            started_monotonic=started_monotonic,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            payload=payload,
            captured_tools=captured_tools,
            provider_liveness=provider_liveness,
        )
        lifecycle.request_stop(
            stop_reason,
            now=lifecycle.started_monotonic + (telemetry.duration_ms / 1000),
            action="Invocation work ended; entering the controlled stop protocol",
        )
        evidence = lifecycle.finalize(
            reason=stop_reason,
            telemetry=telemetry,
            submission_evidence=submission_evidence,
        )
        telemetry = telemetry.model_copy(update={"invocation_lifecycle": evidence})
        return AgentExecutionResult(
            status=AgentExecutionStatus.COMPLETED,
            telemetry=telemetry,
            semantic_submission=semantic_submission,
            submission_evidence=submission_evidence,
        )

    def _compaction_failure_diagnostic_result(
        self,
        *,
        request: AgentExecutionRequest,
        state_dir: Path,
        command: tuple[str, ...],
        started_at: datetime,
        started_monotonic: float,
        exit_code: int | None,
        stdout: str,
        stderr: str,
        provider_liveness: ProviderLivenessEvidence | None,
        lifecycle: _InvocationLifecycleRecorder,
        initialization_baseline: OpenClawInitializationBaseline | None,
        submission_binding_sha256: str | None,
        submission_capture: SubmissionFileCapture | None,
        failure_status: AgentExecutionStatus,
        failure_error: str,
        stop_reason: InvocationStopReason,
        runtime_failure_code: AgentRuntimeFailureCode,
        runtime_failure_payload: _OpenClawResponse | None,
        terminal_evidence_error: str,
    ) -> AgentExecutionResult:
        """Retain safe current-turn provenance after compaction aborts a run."""

        try:
            diagnostic = capture_openclaw_invocation_diagnostic(
                state_dir=state_dir,
                agent_id=request.agent_id,
                session_key=request.session_key,
                prompt=request.prompt,
                baseline=initialization_baseline,
            )
        except OpenClawSessionEvidenceError as error:
            tool_evidence_error = (
                f"{terminal_evidence_error}; invocation diagnostic: {error}"
            )[:2000]
            submission_evidence = None
            if (
                request.submission_contract is not None
                and submission_binding_sha256 is not None
                and submission_capture is not None
            ):
                _, submission_evidence = validate_submission_capture(
                    request.submission_contract,
                    binding_sha256=submission_binding_sha256,
                    capture=submission_capture,
                    tool_calls=(),
                    tool_evidence_error=tool_evidence_error,
                )
            telemetry = self._telemetry(
                request=request,
                command=command,
                started_at=started_at,
                started_monotonic=started_monotonic,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                payload=runtime_failure_payload,
                provider_liveness=provider_liveness,
                tool_evidence_error=tool_evidence_error,
                runtime_failure_code=runtime_failure_code,
            )
            return self._finalize_lifecycle_result(
                AgentExecutionResult(
                    status=failure_status,
                    error=failure_error,
                    telemetry=telemetry,
                    submission_evidence=submission_evidence,
                ),
                lifecycle=lifecycle,
                reason=stop_reason,
            )

        fallback_provider = (
            None
            if runtime_failure_payload is None
            else runtime_failure_payload.provider
        )
        fallback_model = (
            None if runtime_failure_payload is None else runtime_failure_payload.model
        )
        provider = diagnostic.provider or fallback_provider
        model = _canonical_model_reference(
            provider=provider,
            model=diagnostic.model or fallback_model,
        )
        if model != request.model:
            provider = fallback_provider
            model = fallback_model
        payload = _OpenClawResponse(
            visible_texts=(),
            session_id=diagnostic.session_id,
            provider=provider,
            model=model,
        )

        captured_tools: CapturedOpenClawToolEvidence | None = None
        tool_evidence_error: str | None = None
        try:
            captured_tools = capture_openclaw_tool_evidence(
                state_dir=state_dir,
                agent_id=request.agent_id,
                session_key=request.session_key,
                session_id=diagnostic.session_id,
                prompt=request.prompt,
            )
        except OpenClawSessionEvidenceError as error:
            tool_evidence_error = str(error)

        submission_evidence = None
        if (
            request.submission_contract is not None
            and submission_binding_sha256 is not None
            and submission_capture is not None
        ):
            _, submission_evidence = validate_submission_capture(
                request.submission_contract,
                binding_sha256=submission_binding_sha256,
                capture=submission_capture,
                tool_calls=(
                    () if captured_tools is None else captured_tools.tool_calls
                ),
                tool_evidence_error=tool_evidence_error,
                invalid_submission_tool_call_observed=(
                    diagnostic.submission_tool_call_observed
                ),
            )

        telemetry = self._telemetry(
            request=request,
            command=command,
            started_at=started_at,
            started_monotonic=started_monotonic,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            payload=payload,
            captured_tools=captured_tools,
            tool_evidence_error=tool_evidence_error,
            invalid_session_transcript_sha256=(
                diagnostic.transcript_sha256 if captured_tools is None else None
            ),
            invalid_session_record_count=(
                diagnostic.record_count if captured_tools is None else None
            ),
            provider_liveness=provider_liveness,
            runtime_failure_code=runtime_failure_code,
        )
        return self._finalize_lifecycle_result(
            AgentExecutionResult(
                status=failure_status,
                error=failure_error,
                telemetry=telemetry,
                submission_evidence=submission_evidence,
            ),
            lifecycle=lifecycle,
            reason=stop_reason,
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
