"""Replaceable Agent execution adapters and observable subprocess evidence."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
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

ROLE_ARTIFACT_KINDS: dict[AgentRole, frozenset[ArtifactKind]] = {
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
    TIMED_OUT = "timed_out"
    INVALID_RESPONSE = "invalid_response"
    LAUNCH_FAILED = "launch_failed"


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

    return (
        f"agent:{role.value}:"
        f"sat-{run_id}-i{iteration}-{expected_kind.value.replace('_', '-')}"
    )


class AgentExecutionRequest(BaseModel):
    """Complete bounded input to an Agent execution adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    team_id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    iteration: int = Field(ge=1, le=3)
    role: AgentRole
    expected_kind: ArtifactKind
    prompt: str = Field(min_length=1)
    timeout_seconds: int = Field(default=600, ge=1, le=3600)
    model: str | None = None

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
        """Keep role dispatch and expected response schema coherent."""

        validate_role_artifact_kind(self.role, self.expected_kind)
        if self.role is AgentRole.PLANNER and self.iteration != 1:
            raise ValueError("the implementation plan is produced only in iteration 1")
        return self

    @property
    def session_key(self) -> str:
        """Return the stable OpenClaw session identity for this phase."""

        return stable_session_key(
            run_id=self.run_id,
            role=self.role,
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

    role: AgentRole
    session_key: str = Field(min_length=1)
    command: tuple[str, ...] = Field(min_length=1)
    started_at: datetime
    finished_at: datetime
    duration_ms: int = Field(ge=0)
    openclaw_duration_ms: int | None = Field(default=None, ge=0)
    exit_code: int | None = None
    timed_out: bool = False
    stdout: str = ""
    stderr: str = ""
    openclaw_run_id: str | None = None
    session_id: str | None = None
    provider: str | None = None
    model: str | None = None
    usage: AgentTokenUsage | None = None

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

        if self.finished_at < self.started_at:
            raise ValueError("execution cannot finish before it starts")
        if self.timed_out and self.exit_code not in {None, 0}:
            raise ValueError(
                "timed-out Agent executions require no exit code or a zero "
                "OpenClaw wrapper exit"
            )
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
    for payload in payloads:
        if not isinstance(payload, dict):
            raise ValueError("OpenClaw reply payload must be an object")
        if payload.get("isError") is True:
            has_error_payload = True
        if payload.get("isReasoning") is True or payload.get("isCommentary") is True:
            continue
        text = payload.get("text")
        if isinstance(text, str) and text.strip():
            visible.append(text.strip())

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
        runner: ProcessRunner = subprocess.run,
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

    def _command(
        self,
        request: AgentExecutionRequest,
        prompt_path: Path,
    ) -> tuple[str, ...]:
        command = [
            self.openclaw_binary,
            "agent",
            "--agent",
            request.role.value,
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
        payload: _OpenClawResponse | None = None,
    ) -> AgentExecutionTelemetry:
        finished_at = self.clock()
        elapsed = max(0, round((self.monotonic() - started_monotonic) * 1000))
        return AgentExecutionTelemetry(
            role=request.role,
            session_key=request.session_key,
            command=command,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=elapsed,
            openclaw_duration_ms=None if payload is None else payload.duration_ms,
            exit_code=exit_code,
            timed_out=timed_out,
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
                f"no scripted response remains for {request.role.value}"
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
            session_key=request.session_key,
            command=("scripted-agent", request.role.value),
            started_at=now,
            finished_at=now,
            duration_ms=response.duration_ms,
            openclaw_duration_ms=response.duration_ms,
            exit_code=0,
            stdout=response.text,
            stderr=response.stderr,
            openclaw_run_id=f"scripted-{request.run_id}-{request.iteration}",
            session_id=response.session_id
            or f"scripted-{request.run_id}-{request.role.value}-{request.iteration}",
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
