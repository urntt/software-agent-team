"""Versioned deterministic quality gates executed through a sandbox boundary.

The checked-in run policy owns resource and isolation limits.  A benchmark
manifest owns fixed command argv, working directories, acceptance-criterion
coverage, and trusted read-only inputs.  Generated repositories cannot change
either manifest.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import signal
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from software_agent_team.artifacts import CommandEvidence, TaskBrief
from software_agent_team.budgets import AgentBudget

QUALITY_GATE_SCHEMA_VERSION = 1
_MAX_MANIFEST_BYTES = 1_048_576
_SHELL_EXECUTABLES = {
    "bash",
    "cmd",
    "cmd.exe",
    "dash",
    "fish",
    "ksh",
    "powershell",
    "pwsh",
    "sh",
    "zsh",
}
_INLINE_CODE_FLAGS = {
    "node": {"-e", "--eval"},
    "nodejs": {"-e", "--eval"},
    "python": {"-c"},
    "python3": {"-c"},
}
_CONTAINER_INPUT_ROOT = PurePosixPath("/opt/software-agent-team/inputs")
_OUTPUT_LIMIT_MARKER = b"\n[software-agent-team: output limit exceeded]\n"


class QualityGateError(RuntimeError):
    """Base error for quality-gate configuration and execution."""


class QualityGateConfigurationError(QualityGateError):
    """A policy, benchmark manifest, or runtime path is unsafe or incoherent."""


class SandboxUnavailableError(QualityGateError):
    """The configured sandbox runtime cannot execute a gate."""


class QualityGateEvidenceError(QualityGateError):
    """Command evidence cannot be persisted without overwriting prior evidence."""


class QualityGateBudgetExceeded(QualityGateError):
    """The aggregate deterministic-gate time budget is exhausted."""


def _safe_relative_path(value: str, *, allow_dot: bool = False) -> str:
    if not value or "\\" in value or "\x00" in value:
        raise ValueError("paths must be non-empty and use POSIX separators")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("paths must be safe relative paths")
    if path == PurePosixPath(".") and not allow_dot:
        raise ValueError("path must identify a concrete relative entry")
    return value


def _is_within_posix(path: PurePosixPath, root: PurePosixPath) -> bool:
    return path == root or root in path.parents


def _validate_container_path(value: str) -> str:
    if "\\" in value or "\x00" in value:
        raise ValueError("container paths must use POSIX separators")
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts or str(path) != value:
        raise ValueError("container paths must be canonical absolute paths")
    return value


def _validate_argv(values: tuple[str, ...]) -> tuple[str, ...]:
    if not values or len(values) > 64:
        raise ValueError("command argv must contain between 1 and 64 arguments")
    if any(
        not value
        or len(value) > 4096
        or any(character in value for character in ("\x00", "\r", "\n"))
        for value in values
    ):
        raise ValueError("command arguments must be non-empty single-line values")
    if sum(len(value) for value in values) > 16_384:
        raise ValueError("command argv is too large")

    executable = PurePosixPath(values[0]).name.lower()
    if values[0] != executable and values[0].lower() != executable:
        raise ValueError("command executables must be bare image PATH names")
    if re.fullmatch(r"[a-z0-9][a-z0-9._+-]*", executable) is None:
        raise ValueError("command executable contains unsafe characters")
    if executable in _SHELL_EXECUTABLES:
        raise ValueError("quality gates cannot invoke a command shell")
    forbidden_flags = _INLINE_CODE_FLAGS.get(executable, set())
    if forbidden_flags.intersection(values[1:]):
        raise ValueError("quality gates cannot use opaque inline interpreter code")
    return values


class SandboxLimits(BaseModel):
    """Mandatory per-command and aggregate resource ceilings."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    command_timeout_seconds: int = Field(ge=1, le=3600)
    total_timeout_seconds: int = Field(ge=1, le=14_400)
    cpu_cores: float = Field(gt=0, le=8)
    memory_mb: int = Field(ge=64, le=32_768)
    pids: int = Field(ge=8, le=4096)
    open_files: int = Field(ge=32, le=65_536)
    writable_tmpfs_mb: int = Field(ge=16, le=8192)
    stdout_max_bytes: int = Field(ge=1024, le=16_777_216)
    stderr_max_bytes: int = Field(ge=1024, le=16_777_216)

    @model_validator(mode="after")
    def require_coherent_time_limits(self) -> Self:
        """The aggregate budget must permit at least one full command."""

        if self.total_timeout_seconds < self.command_timeout_seconds:
            raise ValueError("total timeout cannot be below the command timeout")
        return self


class DockerSandboxPolicy(BaseModel):
    """Fixed Docker isolation settings for generated-code execution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    backend: Literal["docker"] = "docker"
    image: str = Field(min_length=1, max_length=255)
    pull: Literal["never"] = "never"
    network: Literal["none"] = "none"
    read_only_root_filesystem: Literal[True] = True
    workspace_access: Literal["read_only"] = "read_only"
    workspace_target: Literal["/workspace"] = "/workspace"
    tmpfs_target: Literal["/tmp"] = "/tmp"
    user: str = Field(pattern=r"^(?:host|[1-9][0-9]*:[1-9][0-9]*)$")
    environment: dict[str, str] = Field(default_factory=dict)

    @field_validator("image")
    @classmethod
    def require_versioned_image(cls, value: str) -> str:
        """Reject mutable implicit/latest image references and CLI-like values."""

        final_component = value.rsplit("/", 1)[-1]
        if (
            value != value.strip()
            or any(character.isspace() for character in value)
            or value.startswith("-")
            or value.endswith(":latest")
            or (":" not in final_component and "@sha256:" not in value)
        ):
            raise ValueError("Docker image must use an explicit non-latest version")
        return value

    @field_validator("environment")
    @classmethod
    def require_literal_safe_environment(cls, values: dict[str, str]) -> dict[str, str]:
        """Only fixed literal values are passed; host variables are never copied."""

        for name, value in values.items():
            if re.fullmatch(r"[A-Z_][A-Z0-9_]*", name) is None:
                raise ValueError(
                    "sandbox environment names must be uppercase identifiers"
                )
            if not value or any(
                character in value for character in ("\x00", "\r", "\n")
            ):
                raise ValueError(
                    "sandbox environment values must be non-empty literals"
                )
        return dict(sorted(values.items()))


class RunPolicy(BaseModel):
    """Versioned authority for deterministic-run sandbox policy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[QUALITY_GATE_SCHEMA_VERSION]
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    agent_budget: AgentBudget
    sandbox: DockerSandboxPolicy
    limits: SandboxLimits


class ReadOnlyInputMount(BaseModel):
    """Trusted benchmark input mounted outside the generated workspace."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    source: str = Field(min_length=1)
    target: str = Field(min_length=1)
    read_only: Literal[True] = True

    @field_validator("source")
    @classmethod
    def require_safe_source(cls, value: str) -> str:
        return _safe_relative_path(value)

    @field_validator("target")
    @classmethod
    def require_isolated_target(cls, value: str) -> str:
        value = _validate_container_path(value)
        target = PurePosixPath(value)
        if target == _CONTAINER_INPUT_ROOT or not _is_within_posix(
            target, _CONTAINER_INPUT_ROOT
        ):
            raise ValueError(
                f"input mount targets must be below {_CONTAINER_INPUT_ROOT}"
            )
        return value


class QualityGateDefinition(BaseModel):
    """One fixed, non-shell deterministic command."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^CHECK_[A-Z0-9_]+$")
    argv: tuple[str, ...] = Field(min_length=1, max_length=64)
    working_directory: str = "."
    timeout_seconds: int | None = Field(default=None, ge=1, le=3600)
    criterion_ids: tuple[str, ...] = Field(min_length=1)

    @field_validator("argv")
    @classmethod
    def require_safe_non_shell_argv(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_argv(values)

    @field_validator("working_directory")
    @classmethod
    def require_safe_working_directory(cls, value: str) -> str:
        return _safe_relative_path(value, allow_dot=True)

    @field_validator("criterion_ids")
    @classmethod
    def require_unique_criteria(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)) or any(
            re.fullmatch(r"[A-Z][A-Z0-9_-]*", value) is None for value in values
        ):
            raise ValueError("gate criterion IDs must be valid and unique")
        return values


class BenchmarkManifest(BaseModel):
    """Versioned authority for one benchmark's deterministic acceptance gates."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[QUALITY_GATE_SCHEMA_VERSION]
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    policy_id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    task_brief: str = Field(min_length=1)
    input_mounts: tuple[ReadOnlyInputMount, ...] = Field(min_length=1)
    gates: tuple[QualityGateDefinition, ...] = Field(min_length=1)
    manual_review_criteria: tuple[str, ...] = ()

    @field_validator("task_brief")
    @classmethod
    def require_safe_task_brief_path(cls, value: str) -> str:
        return _safe_relative_path(value)

    @field_validator("manual_review_criteria")
    @classmethod
    def require_unique_manual_criteria(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)) or any(
            re.fullmatch(r"[A-Z][A-Z0-9_-]*", value) is None for value in values
        ):
            raise ValueError("manual-review criterion IDs must be valid and unique")
        return values

    @model_validator(mode="after")
    def require_unique_definitions(self) -> Self:
        """Reject ambiguous IDs and overlapping container mount targets."""

        gate_ids = [gate.id for gate in self.gates]
        mount_ids = [mount.id for mount in self.input_mounts]
        if len(gate_ids) != len(set(gate_ids)):
            raise ValueError("quality-gate IDs must be unique")
        if len(mount_ids) != len(set(mount_ids)):
            raise ValueError("input-mount IDs must be unique")

        targets = [PurePosixPath(mount.target) for mount in self.input_mounts]
        for index, target in enumerate(targets):
            for other in targets[index + 1 :]:
                if _is_within_posix(target, other) or _is_within_posix(other, target):
                    raise ValueError("input-mount targets cannot overlap")
        return self


@dataclass(frozen=True)
class ResolvedInputMount:
    """Validated host source and container target for a trusted input."""

    id: str
    source: Path
    target: PurePosixPath


@dataclass(frozen=True)
class QualityGateConfiguration:
    """Loaded, cross-validated manifests plus their reproducibility hashes."""

    policy: RunPolicy
    benchmark: BenchmarkManifest
    task_brief: TaskBrief
    policy_path: Path
    benchmark_path: Path
    task_brief_path: Path
    policy_sha256: str
    benchmark_sha256: str
    task_brief_sha256: str
    input_mounts: tuple[ResolvedInputMount, ...]


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise QualityGateConfigurationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _read_json(path: Path) -> tuple[object, bytes]:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise QualityGateConfigurationError(
            f"cannot read manifest {path}: {error}"
        ) from error
    if len(raw) > _MAX_MANIFEST_BYTES:
        raise QualityGateConfigurationError(
            f"manifest exceeds {_MAX_MANIFEST_BYTES} bytes"
        )
    try:
        payload = json.loads(raw, object_pairs_hook=_reject_duplicate_json_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise QualityGateConfigurationError(
            f"invalid JSON in {path}: {error}"
        ) from error
    return payload, raw


def _resolve_trusted_relative(root: Path, relative: str, *, label: str) -> Path:
    candidate = root / relative
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise QualityGateConfigurationError(
            f"{label} does not exist: {relative}"
        ) from error
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise QualityGateConfigurationError(
            f"{label} escapes the benchmark directory"
        ) from error
    if "," in str(resolved):
        raise QualityGateConfigurationError(f"{label} path cannot contain a comma")
    return resolved


def _validate_manifest_commands(
    benchmark: BenchmarkManifest, policy: RunPolicy
) -> None:
    allowed_roots = {
        PurePosixPath(policy.sandbox.workspace_target),
        PurePosixPath(policy.sandbox.tmpfs_target),
        *(PurePosixPath(mount.target) for mount in benchmark.input_mounts),
    }
    maximum_gate_seconds = 0
    for gate in benchmark.gates:
        if (
            gate.timeout_seconds is not None
            and gate.timeout_seconds > policy.limits.command_timeout_seconds
        ):
            raise QualityGateConfigurationError(
                f"gate {gate.id} timeout exceeds the run policy"
            )
        maximum_gate_seconds += (
            gate.timeout_seconds or policy.limits.command_timeout_seconds
        )
        for argument in gate.argv[1:]:
            candidate = argument.split("=", 1)[-1] if "=" in argument else argument
            if not candidate.startswith("/"):
                continue
            path = PurePosixPath(candidate)
            if ".." in path.parts or not any(
                _is_within_posix(path, root) for root in allowed_roots
            ):
                raise QualityGateConfigurationError(
                    f"gate {gate.id} references an unmounted absolute path"
                )
    if maximum_gate_seconds > policy.limits.total_timeout_seconds:
        raise QualityGateConfigurationError(
            "sum of gate timeouts exceeds the total run-policy timeout"
        )

    for name, value in policy.sandbox.environment.items():
        if value.startswith("/"):
            path = PurePosixPath(value)
            if ".." in path.parts or not any(
                _is_within_posix(path, root) for root in allowed_roots
            ):
                raise QualityGateConfigurationError(
                    f"sandbox environment {name} references an unmounted path"
                )


def load_quality_gate_configuration(
    policy_path: Path | str, benchmark_path: Path | str
) -> QualityGateConfiguration:
    """Load and cross-validate the authoritative policy and benchmark files."""

    policy_path = Path(policy_path).resolve(strict=True)
    benchmark_path = Path(benchmark_path).resolve(strict=True)
    policy_payload, policy_raw = _read_json(policy_path)
    benchmark_payload, benchmark_raw = _read_json(benchmark_path)
    try:
        policy = RunPolicy.model_validate(policy_payload)
        benchmark = BenchmarkManifest.model_validate(benchmark_payload)
    except ValueError as error:
        raise QualityGateConfigurationError(str(error)) from error
    if benchmark.policy_id != policy.id:
        raise QualityGateConfigurationError("benchmark policy ID does not match policy")

    benchmark_root = benchmark_path.parent.resolve(strict=True)
    task_brief_path = _resolve_trusted_relative(
        benchmark_root, benchmark.task_brief, label="task brief"
    )
    if not task_brief_path.is_file():
        raise QualityGateConfigurationError("task brief must be a regular file")
    task_payload, task_raw = _read_json(task_brief_path)
    try:
        task_brief = TaskBrief.model_validate(task_payload)
    except ValueError as error:
        raise QualityGateConfigurationError(str(error)) from error
    if not task_brief.confirmed:
        raise QualityGateConfigurationError("benchmark task brief must be confirmed")

    resolved_mounts: list[ResolvedInputMount] = []
    for mount in benchmark.input_mounts:
        source = _resolve_trusted_relative(
            benchmark_root, mount.source, label=f"input mount {mount.id}"
        )
        resolved_mounts.append(
            ResolvedInputMount(
                id=mount.id,
                source=source,
                target=PurePosixPath(mount.target),
            )
        )

    known_criteria = {criterion.id for criterion in task_brief.acceptance_criteria}
    gate_criteria = {
        criterion_id for gate in benchmark.gates for criterion_id in gate.criterion_ids
    }
    declared_criteria = gate_criteria | set(benchmark.manual_review_criteria)
    unknown = declared_criteria - known_criteria
    missing = known_criteria - declared_criteria
    if unknown:
        raise QualityGateConfigurationError(
            f"benchmark references unknown criteria: {', '.join(sorted(unknown))}"
        )
    if missing:
        raise QualityGateConfigurationError(
            f"benchmark leaves criteria unassigned: {', '.join(sorted(missing))}"
        )
    _validate_manifest_commands(benchmark, policy)

    def digest(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    return QualityGateConfiguration(
        policy=policy,
        benchmark=benchmark,
        task_brief=task_brief,
        policy_path=policy_path,
        benchmark_path=benchmark_path,
        task_brief_path=task_brief_path,
        policy_sha256=digest(policy_raw),
        benchmark_sha256=digest(benchmark_raw),
        task_brief_sha256=digest(task_raw),
        input_mounts=tuple(resolved_mounts),
    )


@dataclass(frozen=True)
class SandboxInvocation:
    """One validated command invocation independent of backend transport."""

    gate_id: str
    argv: tuple[str, ...]
    working_directory: PurePosixPath
    workspace: Path
    input_mounts: tuple[ResolvedInputMount, ...]
    environment: tuple[tuple[str, str], ...]
    sandbox: DockerSandboxPolicy
    limits: SandboxLimits
    timeout_seconds: float


@dataclass(frozen=True)
class SandboxExecution:
    """Raw backend result before it is converted to persisted evidence."""

    exit_code: int | None
    timed_out: bool
    duration_ms: int
    stdout: bytes = b""
    stderr: bytes = b""
    output_limit_exceeded: bool = False

    def __post_init__(self) -> None:
        if self.duration_ms < 0:
            raise ValueError("sandbox duration cannot be negative")
        if self.timed_out and self.exit_code is not None:
            raise ValueError("timed-out sandbox execution cannot have an exit code")
        if not self.timed_out and self.exit_code is None:
            raise ValueError("completed sandbox execution requires an exit code")


class SandboxBackend(Protocol):
    """Execution boundary used by the deterministic quality-gate runner."""

    kind: str

    def execute(self, invocation: SandboxInvocation) -> SandboxExecution:
        """Execute exactly one already-validated invocation."""


@dataclass(frozen=True)
class _ProcessResult:
    exit_code: int | None
    timed_out: bool
    duration_ms: int
    stdout: bytes
    stderr: bytes
    output_limit_exceeded: bool


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        elif process.poll() is None:
            process.kill()
    except ProcessLookupError:
        pass


def _append_bounded(buffer: bytearray, chunk: bytes, limit: int) -> bool:
    remaining = limit - len(buffer)
    if len(chunk) <= remaining:
        buffer.extend(chunk)
        return False
    marker = _OUTPUT_LIMIT_MARKER[:limit]
    payload_limit = max(0, limit - len(marker))
    if len(buffer) > payload_limit:
        del buffer[payload_limit:]
    elif len(buffer) < payload_limit:
        buffer.extend(chunk[: payload_limit - len(buffer)])
    buffer.extend(marker)
    return True


def _run_bounded_process(
    argv: Sequence[str],
    *,
    timeout_seconds: float,
    stdout_limit: int,
    stderr_limit: int,
    cwd: Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> _ProcessResult:
    started = time.monotonic()
    process = subprocess.Popen(
        list(argv),
        cwd=cwd,
        env=None if environment is None else dict(environment),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=os.name == "posix",
    )
    assert process.stdout is not None
    assert process.stderr is not None
    streams = {
        process.stdout: (bytearray(), stdout_limit),
        process.stderr: (bytearray(), stderr_limit),
    }
    selector = selectors.DefaultSelector()
    for stream in streams:
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ)

    timed_out = False
    output_limit_exceeded = False
    deadline = started + timeout_seconds
    termination_deadline: float | None = None
    try:
        while selector.get_map():
            now = time.monotonic()
            if not timed_out and not output_limit_exceeded and now >= deadline:
                timed_out = True
                termination_deadline = now + 2
                _terminate_process(process)
            if termination_deadline is not None and now >= termination_deadline:
                for key in tuple(selector.get_map().values()):
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                break

            wait = 0.05
            if not timed_out and not output_limit_exceeded:
                wait = max(0.0, min(wait, deadline - now))
            for key, _ in selector.select(wait):
                stream = key.fileobj
                try:
                    chunk = os.read(stream.fileno(), 65_536)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(stream)
                    stream.close()
                    continue
                buffer, limit = streams[stream]
                if _append_bounded(buffer, chunk, limit):
                    output_limit_exceeded = True
                    termination_deadline = time.monotonic() + 2
                    _terminate_process(process)
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        _terminate_process(process)
        process.wait(timeout=2)
    finally:
        selector.close()
        for stream in streams:
            if not stream.closed:
                stream.close()

    duration_ms = max(0, round((time.monotonic() - started) * 1000))
    stdout = bytes(streams[process.stdout][0])
    stderr = bytes(streams[process.stderr][0])
    if timed_out:
        exit_code = None
    elif output_limit_exceeded:
        exit_code = 137
    else:
        exit_code = process.returncode
    return _ProcessResult(
        exit_code=exit_code,
        timed_out=timed_out,
        duration_ms=duration_ms,
        stdout=stdout,
        stderr=stderr,
        output_limit_exceeded=output_limit_exceeded,
    )


class DockerSandboxBackend:
    """Production backend using Docker without a shell or host-network access."""

    kind = "docker"

    def __init__(self, executable: str = "docker") -> None:
        if PurePosixPath(executable).name != executable or executable.startswith("-"):
            raise ValueError("Docker executable must be a bare PATH name")
        self.executable = executable

    def build_argv(
        self, invocation: SandboxInvocation, *, container_name: str
    ) -> tuple[str, ...]:
        """Build the exact list argv passed to Docker for audit and testing."""

        _validate_argv(invocation.argv)
        if invocation.sandbox.user == "host":
            raise QualityGateConfigurationError(
                "Docker sandbox host user must be resolved before execution"
            )
        workspace = invocation.workspace.resolve(strict=True)
        if not workspace.is_dir() or "," in str(workspace):
            raise QualityGateConfigurationError("workspace must be a safe directory")
        container_cwd = PurePosixPath(invocation.sandbox.workspace_target)
        if invocation.working_directory != PurePosixPath("."):
            container_cwd /= invocation.working_directory

        limits = invocation.limits
        argv: list[str] = [
            self.executable,
            "run",
            "--rm",
            "--init",
            "--name",
            container_name,
            "--pull",
            invocation.sandbox.pull,
            "--network",
            invocation.sandbox.network,
            "--ipc",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            invocation.sandbox.user,
            "--pids-limit",
            str(limits.pids),
            "--ulimit",
            f"nofile={limits.open_files}:{limits.open_files}",
            "--memory",
            f"{limits.memory_mb}m",
            "--memory-swap",
            f"{limits.memory_mb}m",
            "--cpus",
            str(limits.cpu_cores),
            "--tmpfs",
            (
                f"{invocation.sandbox.tmpfs_target}:rw,nosuid,nodev,noexec,"
                f"size={limits.writable_tmpfs_mb}m,mode=1777"
            ),
            "--workdir",
            str(container_cwd),
            "--mount",
            (
                f"type=bind,source={workspace},"
                f"target={invocation.sandbox.workspace_target},readonly"
            ),
        ]
        for mount in invocation.input_mounts:
            source = mount.source.resolve(strict=True)
            if "," in str(source):
                raise QualityGateConfigurationError(
                    "input mount path cannot contain comma"
                )
            argv.extend(
                [
                    "--mount",
                    f"type=bind,source={source},target={mount.target},readonly",
                ]
            )
        for name, value in invocation.environment:
            argv.extend(["--env", f"{name}={value}"])
        argv.extend([invocation.sandbox.image, *invocation.argv])
        return tuple(argv)

    def _cleanup_container(self, container_name: str) -> None:
        try:
            subprocess.run(
                [self.executable, "rm", "--force", container_name],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):  # pragma: no cover
            return

    def execute(self, invocation: SandboxInvocation) -> SandboxExecution:
        """Run one container and force-remove it after timeout/output overflow."""

        container_name = f"sat-qg-{uuid.uuid4().hex[:16]}"
        argv = self.build_argv(invocation, container_name=container_name)
        try:
            result = _run_bounded_process(
                argv,
                timeout_seconds=invocation.timeout_seconds,
                stdout_limit=invocation.limits.stdout_max_bytes,
                stderr_limit=invocation.limits.stderr_max_bytes,
            )
        except FileNotFoundError as error:
            raise SandboxUnavailableError(
                f"Docker executable is unavailable: {self.executable}"
            ) from error
        if result.timed_out or result.output_limit_exceeded:
            self._cleanup_container(container_name)
        if result.exit_code == 125 and not result.timed_out:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise SandboxUnavailableError(
                f"Docker could not start quality gate {invocation.gate_id}: {detail}"
            )
        return SandboxExecution(
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            duration_ms=result.duration_ms,
            stdout=result.stdout,
            stderr=result.stderr,
            output_limit_exceeded=result.output_limit_exceeded,
        )


def _map_container_path(
    value: str,
    *,
    workspace: Path,
    temporary: Path,
    mounts: tuple[ResolvedInputMount, ...],
) -> str:
    path = PurePosixPath(value)
    mappings = [
        (PurePosixPath("/workspace"), workspace),
        (PurePosixPath("/tmp"), temporary),
        *((mount.target, mount.source) for mount in mounts),
    ]
    for target, source in sorted(
        mappings, key=lambda item: len(item[0].parts), reverse=True
    ):
        if _is_within_posix(path, target):
            relative = path.relative_to(target)
            return str(source.joinpath(*relative.parts))
    raise QualityGateConfigurationError(f"host test backend cannot map path: {value}")


def _map_container_argument(
    argument: str,
    *,
    workspace: Path,
    temporary: Path,
    mounts: tuple[ResolvedInputMount, ...],
) -> str:
    if argument.startswith("/"):
        return _map_container_path(
            argument,
            workspace=workspace,
            temporary=temporary,
            mounts=mounts,
        )
    if "=" in argument:
        option, value = argument.split("=", 1)
        if value.startswith("/"):
            mapped = _map_container_path(
                value,
                workspace=workspace,
                temporary=temporary,
                mounts=mounts,
            )
            return f"{option}={mapped}"
    return argument


class HostTestBackend:
    """Explicitly unsafe host backend for offline runner tests only."""

    kind = "host-test"

    def __init__(self, *, allow_unsafe_host_execution: bool = False) -> None:
        if not allow_unsafe_host_execution:
            raise QualityGateConfigurationError(
                "host execution requires allow_unsafe_host_execution=True"
            )

    def execute(self, invocation: SandboxInvocation) -> SandboxExecution:
        """Execute with mapped mount paths and a sanitized host environment."""

        _validate_argv(invocation.argv)
        workspace = invocation.workspace.resolve(strict=True)
        cwd = workspace / invocation.working_directory
        cwd = cwd.resolve(strict=True)
        if not cwd.is_dir() or not cwd.is_relative_to(workspace):
            raise QualityGateConfigurationError(
                "gate working directory escapes workspace"
            )

        with tempfile.TemporaryDirectory(prefix="sat-host-test-") as raw_temporary:
            temporary = Path(raw_temporary)
            mapped_argv = tuple(
                _map_container_argument(
                    argument,
                    workspace=workspace,
                    temporary=temporary,
                    mounts=invocation.input_mounts,
                )
                for argument in invocation.argv
            )
            environment = {
                "PATH": os.environ.get("PATH", ""),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
            }
            for name, value in invocation.environment:
                environment[name] = (
                    _map_container_path(
                        value,
                        workspace=workspace,
                        temporary=temporary,
                        mounts=invocation.input_mounts,
                    )
                    if value.startswith("/")
                    else value
                )
            try:
                result = _run_bounded_process(
                    mapped_argv,
                    timeout_seconds=invocation.timeout_seconds,
                    stdout_limit=invocation.limits.stdout_max_bytes,
                    stderr_limit=invocation.limits.stderr_max_bytes,
                    cwd=cwd,
                    environment=environment,
                )
            except FileNotFoundError as error:
                raise SandboxUnavailableError(
                    f"host-test executable is unavailable: {mapped_argv[0]}"
                ) from error
        return SandboxExecution(
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            duration_ms=result.duration_ms,
            stdout=result.stdout,
            stderr=result.stderr,
            output_limit_exceeded=result.output_limit_exceeded,
        )


@dataclass
class FakeSandboxBackend:
    """Non-executing backend with scripted results for offline unit tests."""

    executions: list[SandboxExecution]
    kind: str = field(default="fake", init=False)
    invocations: list[SandboxInvocation] = field(default_factory=list, init=False)

    def execute(self, invocation: SandboxInvocation) -> SandboxExecution:
        self.invocations.append(invocation)
        if not self.executions:
            raise AssertionError("fake backend has no scripted execution")
        return self.executions.pop(0)


class QualityGateRunner:
    """Run fixed gates sequentially and persist existing ``CommandEvidence``."""

    def __init__(
        self,
        configuration: QualityGateConfiguration,
        *,
        run_directory: Path | str,
        workspace: Path | str,
        sandbox_image_id: str | None = None,
        backend: SandboxBackend | None = None,
        allow_test_backends: bool = False,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.configuration = configuration
        try:
            self.run_directory = Path(run_directory).resolve(strict=True)
            self.workspace = Path(workspace).resolve(strict=True)
        except OSError as error:
            raise QualityGateConfigurationError(
                "run directory and workspace must already exist"
            ) from error
        if not self.run_directory.is_dir() or not self.workspace.is_dir():
            raise QualityGateConfigurationError(
                "run directory and workspace must be directories"
            )
        for mount in configuration.input_mounts:
            if mount.source == self.workspace or mount.source.is_relative_to(
                self.workspace
            ):
                raise QualityGateConfigurationError(
                    "trusted benchmark inputs must be outside the generated workspace"
                )
        if sandbox_image_id is None:
            self.sandbox = configuration.policy.sandbox
        else:
            if re.fullmatch(r"sha256:[0-9a-f]{64}", sandbox_image_id) is None:
                raise QualityGateConfigurationError(
                    "sandbox image ID must be an immutable SHA-256 identity"
                )
            self.sandbox = configuration.policy.sandbox.model_copy(
                update={"image": sandbox_image_id}
            )

        selected_backend: SandboxBackend = backend or DockerSandboxBackend()
        if selected_backend.kind != "docker" and (
            not allow_test_backends
            or selected_backend.kind
            not in {
                "fake",
                "host-test",
            }
        ):
            raise QualityGateConfigurationError(
                "non-Docker backends require allow_test_backends=True"
            )
        self.backend = selected_backend
        self.monotonic = monotonic

    def _runtime_sandbox(self) -> DockerSandboxPolicy:
        """Resolve the portable host-user policy for the Docker boundary."""

        if self.backend.kind != "docker" or self.sandbox.user != "host":
            return self.sandbox
        uid = os.getuid()
        gid = os.getgid()
        if uid == 0 or gid == 0:
            raise QualityGateConfigurationError(
                "live quality gates require an unprivileged host user"
            )
        return self.sandbox.model_copy(update={"user": f"{uid}:{gid}"})

    def _working_directory(self, gate: QualityGateDefinition) -> PurePosixPath:
        relative = PurePosixPath(gate.working_directory)
        candidate = (self.workspace / relative).resolve(strict=True)
        if not candidate.is_dir() or not candidate.is_relative_to(self.workspace):
            raise QualityGateConfigurationError(
                f"gate {gate.id} working directory escapes or is not a directory"
            )
        return relative

    def _output_paths(
        self, gate: QualityGateDefinition, iteration: int
    ) -> tuple[Path, Path, str, str]:
        output_directory = (
            self.run_directory / "iterations" / f"{iteration:02d}" / "commands"
        )
        output_directory.mkdir(parents=True, exist_ok=True)
        resolved_directory = output_directory.resolve(strict=True)
        if not resolved_directory.is_relative_to(self.run_directory):
            raise QualityGateEvidenceError(
                "command output directory escapes run directory"
            )
        stem = gate.id.lower()
        stdout = resolved_directory / f"{stem}.stdout.txt"
        stderr = resolved_directory / f"{stem}.stderr.txt"
        stdout_relative = stdout.relative_to(self.run_directory).as_posix()
        stderr_relative = stderr.relative_to(self.run_directory).as_posix()
        return stdout, stderr, stdout_relative, stderr_relative

    @staticmethod
    def _persist_once(path: Path, content: bytes) -> None:
        try:
            with path.open("xb") as destination:
                destination.write(content)
                destination.flush()
                os.fsync(destination.fileno())
        except FileExistsError as error:
            raise QualityGateEvidenceError(
                f"quality-gate evidence already exists: {path.name}"
            ) from error
        except OSError as error:
            raise QualityGateEvidenceError(
                f"cannot persist quality-gate evidence: {path}"
            ) from error

    def run(self, *, iteration: int) -> tuple[CommandEvidence, ...]:
        """Execute every fixed gate once and persist bounded stdout/stderr."""

        if not 1 <= iteration <= 3:
            raise QualityGateConfigurationError("iteration must be between 1 and 3")
        limits = self.configuration.policy.limits
        runtime_sandbox = self._runtime_sandbox()
        started = self.monotonic()
        evidence: list[CommandEvidence] = []
        output_paths = {
            gate.id: self._output_paths(gate, iteration)
            for gate in self.configuration.benchmark.gates
        }
        existing = [
            path
            for paths in output_paths.values()
            for path in paths[:2]
            if path.exists()
        ]
        if existing:
            raise QualityGateEvidenceError(
                f"quality-gate evidence already exists: {existing[0].name}"
            )
        for gate in self.configuration.benchmark.gates:
            elapsed = self.monotonic() - started
            remaining = limits.total_timeout_seconds - elapsed
            if remaining < 1:
                raise QualityGateBudgetExceeded(
                    "deterministic quality-gate time budget is exhausted"
                )
            gate_timeout = gate.timeout_seconds or limits.command_timeout_seconds
            timeout = min(float(gate_timeout), remaining)
            invocation = SandboxInvocation(
                gate_id=gate.id,
                argv=gate.argv,
                working_directory=self._working_directory(gate),
                workspace=self.workspace,
                input_mounts=self.configuration.input_mounts,
                environment=tuple(runtime_sandbox.environment.items()),
                sandbox=runtime_sandbox,
                limits=limits,
                timeout_seconds=timeout,
            )
            result = self.backend.execute(invocation)
            output_limited = result.output_limit_exceeded
            stdout = result.stdout
            stderr = result.stderr
            if len(stdout) > limits.stdout_max_bytes:
                buffer = bytearray()
                _append_bounded(buffer, stdout, limits.stdout_max_bytes)
                stdout = bytes(buffer)
                output_limited = True
            if len(stderr) > limits.stderr_max_bytes:
                buffer = bytearray()
                _append_bounded(buffer, stderr, limits.stderr_max_bytes)
                stderr = bytes(buffer)
                output_limited = True

            stdout_path, stderr_path, stdout_relative, stderr_relative = output_paths[
                gate.id
            ]
            self._persist_once(stdout_path, stdout)
            try:
                self._persist_once(stderr_path, stderr)
            except QualityGateEvidenceError:
                stdout_path.unlink(missing_ok=True)
                raise

            exit_code = result.exit_code
            timed_out = result.timed_out
            if output_limited and not timed_out:
                exit_code = 137
            if timed_out:
                summary = f"Timed out after {result.duration_ms} ms."
            elif output_limited:
                summary = "Sandbox output exceeded the configured limit."
            elif exit_code == 0:
                summary = "Deterministic quality gate passed."
            else:
                summary = f"Deterministic quality gate exited with code {exit_code}."
            evidence.append(
                CommandEvidence(
                    id=gate.id,
                    argv=gate.argv,
                    exit_code=exit_code,
                    timed_out=timed_out,
                    duration_ms=result.duration_ms,
                    stdout_path=stdout_relative,
                    stderr_path=stderr_relative,
                    summary=summary,
                )
            )
        return tuple(evidence)
