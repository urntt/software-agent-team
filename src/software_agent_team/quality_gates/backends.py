"""Sandbox execution backends and bounded process supervision.

Provides execution backends and process lifecycle boundaries:
- SandboxBackend: Abstract execution protocol.
- DockerSandboxBackend: Production Docker backend with strict isolation.
- HostTestBackend: Unsafe local execution for offline runner tests only.
- FakeSandboxBackend: Scripted execution replay for offline unit tests.
- Process boundary helpers: timeouts, output limits, and diagnostic tails.
"""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import tempfile
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Protocol

from software_agent_team.docker_engine import (
    current_docker_engine,
    verify_bound_docker_engine,
)
from software_agent_team.quality_gates.types import (
    QualityGateConfigurationError,
    ResolvedInputMount,
    SandboxExecution,
    SandboxInvocation,
    SandboxUnavailableError,
    _is_within_posix,
    _validate_argv,
)

_OUTPUT_LIMIT_MARKER = b"\n[software-agent-team: output limit exceeded]\n"
_EVIDENCE_TAIL_BYTES = 4096


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


def _evidence_tail(value: bytes) -> tuple[str, bool]:
    """Return a bounded diagnostic tail suitable for an Agent prompt."""

    truncated = len(value) > _EVIDENCE_TAIL_BYTES
    retained = value[-_EVIDENCE_TAIL_BYTES:]
    return retained.decode("utf-8", errors="replace"), truncated


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
        if invocation.sandbox.user == "0:0" and not (
            (engine := current_docker_engine()) is not None and engine.rootless
        ):
            raise QualityGateConfigurationError(
                "container root is allowed only on a verified rootless Docker engine"
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
                f"{invocation.sandbox.tmpfs_target}:rw,exec,nosuid,nodev,"
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
        verify_bound_docker_engine()
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
        verify_bound_docker_engine()
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
