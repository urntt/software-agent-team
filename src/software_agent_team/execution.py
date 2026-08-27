"""Replaceable Agent execution adapters and observable subprocess evidence."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol, Self, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from software_agent_team.artifacts import (
    AgentRole,
    ArtifactKind,
    PhaseArtifact,
)
from software_agent_team.teams import (
    AgentCapability,
    capability_for_legacy_role,
    expected_output_for_capability,
)

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


class AgentExecutionStatus(StrEnum):
    """Observable terminal state of one adapter invocation."""

    COMPLETED = "completed"
    PROCESS_FAILED = "process_failed"
    PROVIDER_FAILED = "provider_failed"
    TIMED_OUT = "timed_out"
    INVALID_RESPONSE = "invalid_response"
    LAUNCH_FAILED = "launch_failed"
    INTERRUPTED = "interrupted"


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
    """Complete bounded input to an Agent execution adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    team_id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    iteration: int = Field(ge=1, le=3)
    role: AgentRole | None = None
    agent_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    capability: AgentCapability
    expected_kind: ArtifactKind
    prompt: str = Field(min_length=1)
    timeout_seconds: int = Field(ge=1, le=3600)
    model: str | None = None

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
        if self.timed_out and self.exit_code not in {None, 0}:
            raise ValueError(
                "timed-out Agent executions require no exit code or a zero "
                "OpenClaw wrapper exit"
            )
        if self.timed_out and self.interrupted:
            raise ValueError("an Agent execution cannot be timed out and interrupted")
        return self


class AgentExecutionResult(BaseModel):
    """Adapter result before the untrusted Agent text is parsed as an artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: AgentExecutionStatus
    response_text: str | None = None
    error: str | None = None
    telemetry: AgentExecutionTelemetry

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
        """Require response text only for a successful adapter invocation."""

        if self.status is AgentExecutionStatus.COMPLETED:
            if self.response_text is None:
                raise ValueError("completed Agent execution requires response text")
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
        if self.status is AgentExecutionStatus.TIMED_OUT:
            if not self.telemetry.timed_out:
                raise ValueError("timed-out result requires timed-out telemetry")
        elif self.telemetry.timed_out:
            raise ValueError("only timed-out results may contain timed-out telemetry")
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

    def execute(self, request: AgentExecutionRequest) -> AgentExecutionResult:
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


class OpenClawSubprocessExecutor:
    """Invoke one OpenClaw Agent turn through a bounded, shell-free subprocess."""

    def __init__(
        self,
        *,
        openclaw_binary: str | Path = "openclaw",
        environment: Mapping[str, str] | None = None,
        local: bool = True,
        process_grace_seconds: int = 35,
        runner: ProcessRunner | None = None,
        clock: WallClock = _system_clock,
        monotonic: MonotonicClock = time.monotonic,
    ) -> None:
        if process_grace_seconds < 1:
            raise AgentExecutionError("OpenClaw process grace period must be positive")
        binary = str(openclaw_binary)
        if not binary:
            raise AgentExecutionError("OpenClaw binary must not be empty")
        self.openclaw_binary = binary
        self.environment = None if environment is None else dict(environment)
        self.local = local
        self.process_grace_seconds = process_grace_seconds
        self.runner = runner
        self.clock = clock
        self.monotonic = monotonic
        self._process_lock = threading.Lock()
        self._active_processes: dict[str, ActiveProcess] = {}
        self._interrupt_requests: set[str] = set()

    def execute(self, request: AgentExecutionRequest) -> AgentExecutionResult:
        """Run ``openclaw agent`` and retain all process and usage evidence."""

        started_at = self.clock()
        started_monotonic = self.monotonic()
        with tempfile.TemporaryDirectory(prefix="sat-openclaw-") as temporary:
            prompt_path = Path(temporary) / "prompt.md"
            prompt_path.write_text(request.prompt, encoding="utf-8")
            prompt_path.chmod(0o600)
            command = self._command(request, prompt_path)
            try:
                if self.runner is None:
                    completed, interrupted = self._run_interruptible_process(
                        request,
                        command,
                    )
                else:
                    completed = self.runner(
                        list(command),
                        check=False,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=request.timeout_seconds + self.process_grace_seconds,
                        shell=False,
                        stdin=subprocess.DEVNULL,
                        env=self._environment(),
                    )
                    interrupted = False
            except subprocess.TimeoutExpired as error:
                return self._timeout_result(
                    request=request,
                    command=command,
                    started_at=started_at,
                    started_monotonic=started_monotonic,
                    error=error,
                )
            except OSError as error:
                return self._launch_failure_result(
                    request=request,
                    command=command,
                    started_at=started_at,
                    started_monotonic=started_monotonic,
                    error=error,
                )

        stdout = _decode_process_output(completed.stdout)
        stderr = _decode_process_output(completed.stderr)
        if interrupted:
            return self._interrupted_result(
                request=request,
                command=command,
                started_at=started_at,
                started_monotonic=started_monotonic,
                exit_code=completed.returncode,
                stdout=stdout,
                stderr=stderr,
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
            )
            return AgentExecutionResult(
                status=AgentExecutionStatus.PROCESS_FAILED,
                error=f"OpenClaw exited with status {completed.returncode}",
                telemetry=telemetry,
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
            )
            return AgentExecutionResult(
                status=AgentExecutionStatus.INVALID_RESPONSE,
                error=str(error),
                telemetry=telemetry,
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
            timed_out=payload.declared_timeout,
        )
        if payload.declared_timeout:
            return AgentExecutionResult(
                status=AgentExecutionStatus.TIMED_OUT,
                error="OpenClaw reported an Agent timeout",
                telemetry=telemetry,
            )
        if payload.provider_failed:
            return AgentExecutionResult(
                status=AgentExecutionStatus.PROVIDER_FAILED,
                error="OpenClaw reported an upstream model-provider failure",
                telemetry=telemetry,
            )
        if payload.has_error_payload:
            return AgentExecutionResult(
                status=AgentExecutionStatus.INVALID_RESPONSE,
                error="OpenClaw returned an error reply payload",
                telemetry=telemetry,
            )
        if len(payload.visible_texts) != 1:
            return AgentExecutionResult(
                status=AgentExecutionStatus.INVALID_RESPONSE,
                error="OpenClaw must return exactly one visible text payload",
                telemetry=telemetry,
            )
        return AgentExecutionResult(
            status=AgentExecutionStatus.COMPLETED,
            response_text=payload.visible_texts[0],
            telemetry=telemetry,
        )

    def interrupt(self, agent_id: str) -> int:
        """Request best-effort termination of active calls for one Agent."""

        with self._process_lock:
            matches = [
                (session_key, process)
                for session_key, (request, process) in self._active_processes.items()
                if request.agent_id == agent_id and process.poll() is None
            ]
            self._interrupt_requests.update(session_key for session_key, _ in matches)
        for _, process in matches:
            self._request_process_termination(process)
        return len(matches)

    def interrupt_all(self) -> int:
        """Request best-effort termination of every active SAT-owned call."""

        with self._process_lock:
            matches = [
                (session_key, process)
                for session_key, (_, process) in self._active_processes.items()
                if process.poll() is None
            ]
            self._interrupt_requests.update(session_key for session_key, _ in matches)
        for _, process in matches:
            self._request_process_termination(process)
        return len(matches)

    def _run_interruptible_process(
        self,
        request: AgentExecutionRequest,
        command: tuple[str, ...],
    ) -> tuple[subprocess.CompletedProcess[str], bool]:
        """Run one process whose exact session may be interrupted by control input."""

        process = subprocess.Popen(
            list(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            stdin=subprocess.DEVNULL,
            env=self._environment(),
            start_new_session=os.name == "posix",
        )
        with self._process_lock:
            self._active_processes[request.session_key] = (request, process)
        timeout = request.timeout_seconds + self.process_grace_seconds
        try:
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired as error:
                self._signal_process(process)
                try:
                    stdout, stderr = process.communicate(
                        timeout=min(5, self.process_grace_seconds)
                    )
                except subprocess.TimeoutExpired:
                    self._kill_process(process)
                    stdout, stderr = process.communicate()
                raise subprocess.TimeoutExpired(
                    command,
                    timeout,
                    output=stdout or error.output,
                    stderr=stderr or error.stderr,
                ) from error
        finally:
            with self._process_lock:
                self._active_processes.pop(request.session_key, None)
                interrupted = request.session_key in self._interrupt_requests
                self._interrupt_requests.discard(request.session_key)
        return (
            subprocess.CompletedProcess(
                list(command),
                process.returncode,
                stdout=stdout,
                stderr=stderr,
            ),
            interrupted,
        )

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

    def _request_process_termination(self, process: subprocess.Popen[str]) -> None:
        """Send termination now and bounded escalation if the process ignores it."""

        self._signal_process(process)
        escalation = threading.Timer(
            min(5, self.process_grace_seconds),
            self._kill_process,
            args=(process,),
        )
        escalation.daemon = True
        escalation.start()

    def _command(
        self,
        request: AgentExecutionRequest,
        prompt_path: Path,
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
            str(request.timeout_seconds),
        ]
        if self.local:
            command.append("--local")
        if request.model is not None:
            command.extend(["--model", request.model])
        return tuple(command)

    def _environment(self) -> Mapping[str, str] | None:
        if self.environment is None:
            return None
        return {**os.environ, **self.environment}

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
        )

    def _timeout_result(
        self,
        *,
        request: AgentExecutionRequest,
        command: tuple[str, ...],
        started_at: datetime,
        started_monotonic: float,
        error: subprocess.TimeoutExpired,
    ) -> AgentExecutionResult:
        telemetry = self._telemetry(
            request=request,
            command=command,
            started_at=started_at,
            started_monotonic=started_monotonic,
            exit_code=None,
            timed_out=True,
            stdout=_decode_process_output(error.stdout),
            stderr=_decode_process_output(error.stderr),
        )
        return AgentExecutionResult(
            status=AgentExecutionStatus.TIMED_OUT,
            error=f"OpenClaw exceeded the process timeout of {error.timeout} seconds",
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
        )
        return AgentExecutionResult(
            status=AgentExecutionStatus.INTERRUPTED,
            error="Agent invocation was interrupted by user control",
            telemetry=telemetry,
        )

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

    @field_validator("text", "model", "provider")
    @classmethod
    def require_nonblank_text(cls, value: str) -> str:
        """Reject unusable scripted metadata and responses."""

        if not value.strip():
            raise ValueError("scripted response fields must not be blank")
        return value


type ScriptedInput = ScriptedAgentResponse | AgentExecutionResult | PhaseArtifact | str


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

    def execute(self, request: AgentExecutionRequest) -> AgentExecutionResult:
        """Consume one predeclared response and record the exact request."""

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
        )
        return AgentExecutionResult(
            status=AgentExecutionStatus.COMPLETED,
            response_text=response.text,
            telemetry=telemetry,
        )


def scripted_executor(responses: Sequence[ScriptedInput]) -> AgentExecutor:
    """Return a protocol-typed scripted executor for dependency injection."""

    return ScriptedAgentExecutor(responses)
