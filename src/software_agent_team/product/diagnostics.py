"""Environment preflight inspection, state layout security, and diagnostics.

运行环境启动前置检查、状态布局安全校验及诊断渲染。
提供无损检测宿主环境 (操作系统、架构、Docker、OpenClaw、磁盘及内存余量)、
安全初始化私有状态目录; 并渲染紧凑/标准格式的用户反馈。
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Literal

from software_agent_team.process_lifecycle import (
    ProcessLeaseStore,
    ProcessLifecycleError,
    ProcessResourceObservation,
)
from software_agent_team.sandbox_lifecycle import (
    SandboxCleanupError,
    SandboxResourceObservation,
    inspect_sat_sandbox_resources,
)
from software_agent_team.state_layout import (
    PRODUCT_STATE_CATEGORIES,
    STATE_MARKER_NAME,
    StateLayoutObservation,
    StateLayoutOperation,
    describe_state_layout_failure,
    inspect_state_layout,
)

from .capacity import inspect_host_capacity
from .delivery import _run_command
from .types import (
    MINIMUM_FREE_BYTES,
    SUPPORTED_ARCHITECTURES,
    CommandRunner,
    DiagnosticCheck,
    DiagnosticState,
    HostCapacitySnapshot,
    ProductFlowError,
    ProductStatePaths,
    StartupDiagnostics,
)


def ensure_product_state(paths: ProductStatePaths) -> None:
    """Create private real directories for product-owned run state."""

    observed = inspect_state_layout(paths.root)
    if observed.initialized and not observed.ready:
        raise ProductFlowError(_state_layout_failure(observed))
    try:
        _secure_product_state(paths, root_existed=observed.initialized)
    except ProductFlowError:
        raise
    except OSError as error:
        failure_path = Path(error.filename) if error.filename else paths.root
        reason = error.strerror or type(error).__name__
        raise ProductFlowError(
            f"SAT state path could not be secured for uid {os.geteuid()}: "
            f"{failure_path} ({reason}). Have the operating-system administrator "
            "restore this exact SAT path to the invoking user, then retry."
        ) from error


def _secure_product_state(
    paths: ProductStatePaths,
    *,
    root_existed: bool,
) -> None:
    """Create and normalize one layout after its read-only ownership check."""

    paths.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if paths.root.is_symlink() or not paths.root.is_dir():
        raise ProductFlowError(f"SAT state root must be a real directory: {paths.root}")
    resolved_root = paths.root.resolve(strict=True)
    marker = paths.root / STATE_MARKER_NAME
    expected_marker = f"software-agent-team-state-v1\nroot={resolved_root}\n".encode()
    if marker.exists() or marker.is_symlink():
        if marker.is_symlink() or not marker.is_file():
            raise ProductFlowError("SAT state ownership marker must be a regular file")
        if marker.read_bytes() != expected_marker:
            raise ProductFlowError("SAT state ownership marker is invalid")
    else:
        if root_existed:
            raise ProductFlowError(
                f"existing state root is not owned by SAT: {paths.root}"
            )
        descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(expected_marker)
            output.flush()
            os.fsync(output.fileno())
        marker.chmod(0o600)

    paths.root.chmod(0o700)
    for category in PRODUCT_STATE_CATEGORIES:
        path = getattr(paths, category.attribute)
        path.mkdir(exist_ok=True, mode=0o700)
        if path.is_symlink() or not path.is_dir():
            raise ProductFlowError(f"SAT state path must be a real directory: {path}")
        path.chmod(0o700)
        try:
            path.resolve(strict=True).relative_to(resolved_root)
        except ValueError as error:
            raise ProductFlowError(
                f"SAT state path escapes its root: {path}"
            ) from error


def _state_layout_failure(observation: StateLayoutObservation) -> str:
    """Render one bounded state failure with an executable recovery boundary."""

    detail, remediation = describe_state_layout_failure(
        observation,
        operation=StateLayoutOperation.FIRST_RUN,
    )
    return f"{detail}. {remediation}"


def _state_layout_diagnostic(state_root: Path) -> DiagnosticCheck:
    observation = inspect_state_layout(state_root)
    if observation.ready:
        detail = (
            f"owned layout ready at {state_root}"
            if observation.initialized
            else f"new owned layout will be initialized at {state_root}"
        )
        return DiagnosticCheck(
            id="state",
            label="SAT state ownership",
            state=DiagnosticState.READY,
            detail=detail,
        )
    detail, remediation = describe_state_layout_failure(
        observation,
        operation=StateLayoutOperation.FIRST_RUN,
        separator="; {count} additional problem(s)",
    )
    return DiagnosticCheck(
        id="state",
        label="SAT state ownership",
        state=DiagnosticState.ACTION_REQUIRED,
        detail=detail,
        action=remediation,
    )


def inspect_startup_environment(
    *,
    working_directory: Path,
    openclaw_binary: Path,
    sandbox_image: str,
    state_root: Path,
    required_memory_mb: int,
    required_pids: int,
    sandbox_binary: str = "docker",
    command_finder: Callable[[str], str | None] = shutil.which,
    command_runner: CommandRunner = _run_command,
    environment: Mapping[str, str] | None = None,
    host_capacity: HostCapacitySnapshot | None = None,
    sandbox_resources: SandboxResourceObservation | None = None,
    process_resources: ProcessResourceObservation | None = None,
) -> StartupDiagnostics:
    """Inspect local prerequisites without changing them or calling a provider."""

    if (
        isinstance(required_memory_mb, bool)
        or required_memory_mb < 1
        or isinstance(required_pids, bool)
        or required_pids < 1
    ):
        raise ValueError("required memory and PID capacity must be positive")
    if not state_root.is_absolute():
        raise ValueError("SAT state root must be absolute")

    values = os.environ if environment is None else environment
    checks: list[DiagnosticCheck] = []
    system = platform.system()
    checks.append(
        DiagnosticCheck(
            id="platform",
            label="Linux or WSL",
            state=(
                DiagnosticState.READY
                if system == "Linux"
                else DiagnosticState.ACTION_REQUIRED
            ),
            detail=f"detected {system or 'unknown'}",
            action=(
                None
                if system == "Linux"
                else "Run SAT on Linux or inside a WSL distribution."
            ),
        )
    )
    architecture = platform.machine().casefold()
    checks.append(
        DiagnosticCheck(
            id="architecture",
            label="Supported architecture",
            state=(
                DiagnosticState.READY
                if architecture in SUPPORTED_ARCHITECTURES
                else DiagnosticState.ACTION_REQUIRED
            ),
            detail=f"detected {architecture or 'unknown'}",
            action=(
                None
                if architecture in SUPPORTED_ARCHITECTURES
                else "Use an x86-64 or ARM64 Linux environment."
            ),
        )
    )
    uid = os.getuid()
    gid = os.getgid()
    unprivileged = uid != 0 and gid != 0
    checks.append(
        DiagnosticCheck(
            id="identity",
            label="Unprivileged user",
            state=(
                DiagnosticState.READY
                if unprivileged
                else DiagnosticState.ACTION_REQUIRED
            ),
            detail=f"uid={uid} gid={gid}",
            action=(None if unprivileged else "Run SAT as a normal user, not root."),
        )
    )
    checks.append(_state_layout_diagnostic(state_root))

    try:
        resolved_working_directory = working_directory.resolve(strict=True)
        directory_ready = (
            resolved_working_directory.is_dir()
            and not working_directory.is_symlink()
            and os.access(resolved_working_directory, os.W_OK | os.X_OK)
        )
    except OSError:
        resolved_working_directory = working_directory
        directory_ready = False
    checks.append(
        DiagnosticCheck(
            id="working_directory",
            label="Writable project parent",
            state=(
                DiagnosticState.READY
                if directory_ready
                else DiagnosticState.ACTION_REQUIRED
            ),
            detail=str(resolved_working_directory),
            action=(
                None
                if directory_ready
                else "Enter a writable real directory and run sat again."
            ),
        )
    )

    for command in ("git", sandbox_binary):
        found = command_finder(command)
        checks.append(
            DiagnosticCheck(
                id=f"command_{command}",
                label=f"{command} command",
                state=(
                    DiagnosticState.READY
                    if found is not None
                    else DiagnosticState.ACTION_REQUIRED
                ),
                detail=found or "not found",
                action=(
                    None
                    if found is not None
                    else f"Install {command} and run sat again."
                ),
            )
        )

    openclaw_ready = openclaw_binary.is_file() and os.access(openclaw_binary, os.X_OK)
    checks.append(
        DiagnosticCheck(
            id="openclaw",
            label="OpenClaw runtime",
            state=(
                DiagnosticState.READY
                if openclaw_ready
                else DiagnosticState.ACTION_REQUIRED
            ),
            detail=str(openclaw_binary),
            action=(
                None
                if openclaw_ready
                else "Re-run the SAT installer to restore the pinned runtime."
            ),
        )
    )

    docker_found = command_finder(sandbox_binary) is not None
    docker_ready = False
    image_ready = False
    if docker_found:
        try:
            docker_ready = command_runner((sandbox_binary, "info"), 15).returncode == 0
        except (OSError, subprocess.SubprocessError):
            docker_ready = False
        if docker_ready:
            try:
                image_ready = (
                    command_runner(
                        (sandbox_binary, "image", "inspect", sandbox_image),
                        15,
                    ).returncode
                    == 0
                )
            except (OSError, subprocess.SubprocessError):
                image_ready = False
    checks.append(
        DiagnosticCheck(
            id="docker_daemon",
            label="Docker daemon",
            state=(
                DiagnosticState.READY
                if docker_ready
                else DiagnosticState.ACTION_REQUIRED
            ),
            detail=(
                "available to the current user"
                if docker_ready
                else "unavailable to the current user"
            ),
            action=(
                None
                if docker_ready
                else "Start Docker in Linux-container mode and grant this user access."
            ),
        )
    )
    checks.append(
        DiagnosticCheck(
            id="sandbox_image",
            label="Pinned sandbox image",
            state=(
                DiagnosticState.READY
                if image_ready
                else DiagnosticState.ACTION_REQUIRED
            ),
            detail=sandbox_image,
            action=(
                None
                if image_ready
                else "Re-run the SAT installer to build the pinned sandbox image."
            ),
        )
    )

    try:
        free_bytes = shutil.disk_usage(resolved_working_directory).free
    except OSError:
        free_bytes = 0
    enough_storage = free_bytes >= MINIMUM_FREE_BYTES
    checks.append(
        DiagnosticCheck(
            id="storage",
            label="Available storage",
            state=(
                DiagnosticState.READY
                if enough_storage
                else DiagnosticState.ACTION_REQUIRED
            ),
            detail=f"{free_bytes // (1024 * 1024)} MiB free",
            action=(
                None if enough_storage else "Free at least 1 GiB before a long build."
            ),
        )
    )

    capacity = host_capacity or inspect_host_capacity()
    required_memory_bytes = required_memory_mb * 1024 * 1024
    memory_available = capacity.available_memory_bytes
    memory_ready = (
        memory_available is not None and memory_available >= required_memory_bytes
    )
    memory_sources = ", ".join(capacity.memory_sources) or "unavailable"
    checks.append(
        DiagnosticCheck(
            id="memory_capacity",
            label="Available host memory",
            state=(DiagnosticState.READY if memory_ready else DiagnosticState.WARNING),
            detail=(
                f"{memory_available // (1024 * 1024)} MiB available; "
                f"{required_memory_mb} MiB configured sandbox ceiling; "
                f"source={memory_sources}"
                if memory_available is not None
                else f"capacity unavailable; source={memory_sources}"
            ),
            action=(
                None
                if memory_ready
                else (
                    "Free host memory or increase the WSL/Docker allocation before "
                    "a resource-intensive build; the restricted runtime probe will "
                    "decide whether this task can start."
                    if memory_available is not None
                    else "Restore Linux /proc and cgroup capacity reporting; the "
                    "restricted runtime probe will still test the sandbox."
                )
            ),
        )
    )

    pids_available = capacity.available_pids
    pids_ready = capacity.pids_unbounded or (
        pids_available is not None and pids_available >= required_pids
    )
    pid_sources = ", ".join(capacity.pid_sources) or "current cgroup"
    checks.append(
        DiagnosticCheck(
            id="pid_capacity",
            label="Available process capacity",
            state=(DiagnosticState.READY if pids_ready else DiagnosticState.WARNING),
            detail=(
                f"no finite PID limit discovered; {required_pids} is the "
                "configured sandbox ceiling"
                if capacity.pids_unbounded
                else (
                    f"{pids_available} PIDs available; {required_pids} is the "
                    f"configured sandbox ceiling; source={pid_sources}"
                    if pids_available is not None
                    else "capacity unavailable; source=current cgroup"
                )
            ),
            action=(
                None
                if pids_ready
                else (
                    "Stop unneeded processes or increase WSL/Docker PID capacity "
                    "before a resource-intensive build; the restricted runtime "
                    "probe will decide whether this task can start."
                    if pids_available is not None
                    else "Restore Linux cgroup PID-capacity reporting; the "
                    "restricted runtime probe will still test the sandbox."
                )
            ),
        )
    )

    observation_error: str | None = None
    if sandbox_resources is None and docker_ready:
        try:
            sandbox_resources = inspect_sat_sandbox_resources(
                sandbox_binary=sandbox_binary,
                state_root=state_root,
                runner=lambda argv, **kwargs: command_runner(
                    tuple(argv), float(kwargs["timeout"])
                ),
            )
        except SandboxCleanupError as error:
            observation_error = str(error)
    if sandbox_resources is None:
        checks.append(
            DiagnosticCheck(
                id="sat_sandbox_resources",
                label="Existing SAT sandbox resources",
                state=DiagnosticState.WARNING,
                detail=observation_error
                or "not inspected because Docker is unavailable",
                action="Restore Docker access and rerun SAT's startup check.",
            )
        )
    elif sandbox_resources.containers:
        identifiers = ", ".join(
            item.container_id[:12] for item in sandbox_resources.containers
        )
        checks.append(
            DiagnosticCheck(
                id="sat_sandbox_resources",
                label="Existing SAT sandbox resources",
                state=DiagnosticState.WARNING,
                detail=(
                    f"{len(sandbox_resources.running)} running and "
                    f"{len(sandbox_resources.stopped)} stopped SAT-owned "
                    f"container(s): {identifiers}"
                ),
                action=(
                    "Finish any active SAT task. If none is active, inspect these "
                    "exact containers before starting a resource-intensive build."
                ),
            )
        )
    else:
        checks.append(
            DiagnosticCheck(
                id="sat_sandbox_resources",
                label="Existing SAT sandbox resources",
                state=DiagnosticState.READY,
                detail="no SAT-owned OpenClaw sandbox containers",
            )
        )

    process_observation_error: str | None = None
    if process_resources is None:
        try:
            process_resources = ProcessLeaseStore(
                state_root / "process-leases"
            ).inspect()
        except ProcessLifecycleError as error:
            process_observation_error = str(error)
    if process_resources is None:
        checks.append(
            DiagnosticCheck(
                id="sat_process_resources",
                label="Existing SAT provider processes",
                state=DiagnosticState.WARNING,
                detail=process_observation_error or "process leases could not be read",
                action="Repair SAT's private process-lease state and run sat again.",
            )
        )
    elif process_resources.orphaned:
        identifiers = ", ".join(
            f"{item.lease.agent_id}:{item.lease.child.pid}"
            for item in process_resources.orphaned
        )
        checks.append(
            DiagnosticCheck(
                id="sat_process_resources",
                label="Existing SAT provider processes",
                state=DiagnosticState.ACTION_REQUIRED,
                detail=(
                    f"{len(process_resources.orphaned)} proven orphaned, "
                    f"{len(process_resources.active)} active, "
                    f"{len(process_resources.stale)} stale lease(s): {identifiers}"
                ),
                action="Run `sat cleanup --orphans`, then start this task again.",
            )
        )
    elif process_resources.active or process_resources.stale:
        checks.append(
            DiagnosticCheck(
                id="sat_process_resources",
                label="Existing SAT provider processes",
                state=DiagnosticState.WARNING,
                detail=(
                    f"{len(process_resources.active)} active and "
                    f"{len(process_resources.stale)} stale SAT process lease(s)"
                ),
                action=(
                    "Let active SAT tasks finish. Run `sat cleanup --orphans` to "
                    "discard leases whose exact process no longer exists."
                ),
            )
        )
    else:
        checks.append(
            DiagnosticCheck(
                id="sat_process_resources",
                label="Existing SAT provider processes",
                state=DiagnosticState.READY,
                detail="no active, orphaned, or stale SAT provider processes",
            )
        )

    path_entries = values.get("PATH", "").split(os.pathsep)
    launcher_visible = any(
        entry and (Path(entry).expanduser() / "sat").exists() for entry in path_entries
    )
    checks.append(
        DiagnosticCheck(
            id="launcher",
            label="sat launcher",
            state=(
                DiagnosticState.READY if launcher_visible else DiagnosticState.WARNING
            ),
            detail="available on PATH" if launcher_visible else "current process only",
            action=(
                None
                if launcher_visible
                else "Add the installer-reported SAT bin directory to PATH."
            ),
        )
    )
    return StartupDiagnostics(checks=tuple(checks), host_capacity=capacity)


def render_startup_diagnostics(
    diagnostics: StartupDiagnostics,
    *,
    visibility: Literal["compact", "standard", "detailed"] = "standard",
    color: bool = False,
) -> None:
    """Print one concise startup report with actionable failures."""

    counts = {
        state: sum(check.state is state for check in diagnostics.checks)
        for state in DiagnosticState
    }
    lines = ["Device check"]
    if visibility == "compact":
        lines.append(
            f"  {len(diagnostics.checks)} checks: "
            f"{counts[DiagnosticState.READY]} ready, "
            f"{counts[DiagnosticState.WARNING]} warning, "
            f"{counts[DiagnosticState.ACTION_REQUIRED]} need action"
        )
    visible = (
        diagnostics.checks
        if visibility != "compact"
        else tuple(
            check
            for check in diagnostics.checks
            if check.state is not DiagnosticState.READY
        )
    )
    for check in visible:
        symbol = {
            DiagnosticState.READY: "✓",
            DiagnosticState.WARNING: "!",
            DiagnosticState.ACTION_REQUIRED: "✗",
        }[check.state]
        lines.append(f"{symbol} {check.label}: {check.detail}")
        if check.action is not None:
            lines.append(f"  Action: {check.action}")
    if visibility == "compact":
        lines.append("  Result: ready" if diagnostics.ready else "  Result: not ready")
    for line in lines:
        if not color:
            print(line)
            continue
        stripped = line.lstrip()
        if line == "Device check":
            code = "1;36"
        elif stripped.startswith(("✓", "Result: ready")):
            code = "32"
        elif stripped.startswith("!"):
            code = "33"
        elif stripped.startswith(("✗", "Result: not ready")):
            code = "31"
        elif " checks:" in line:
            code = "2"
        else:
            code = "0"
        print(f"\x1b[{code}m{line}\x1b[0m")
