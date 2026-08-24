"""Run-scoped OpenClaw configuration materialization and offline preflight."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from software_agent_team.configuration import load_openclaw_template
from software_agent_team.openclaw_runtime import isolated_openclaw_environment
from software_agent_team.teams import TeamManifest


class RuntimeConfigurationError(ValueError):
    """Raised when a safe run-scoped Agent configuration cannot be created."""


class SandboxImageInspection(BaseModel):
    """Non-secret identity returned for one local Docker image reference."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sandbox_binary: str = Field(min_length=1)
    sandbox_version: str = Field(min_length=1)
    sandbox_image: str = Field(min_length=1)
    sandbox_image_id: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    sandbox_image_present: bool

    @property
    def ready(self) -> bool:
        """Return whether the reference resolves to a valid local image ID."""

        return self.sandbox_image_present and self.sandbox_image_id is not None


class SandboxRuntimeProbe(BaseModel):
    """Result of starting and removing one restricted sandbox container."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sandbox_binary: str = Field(min_length=1)
    sandbox_image_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    sandbox_container_ready: bool
    error: str | None = Field(default=None, max_length=1000)

    @property
    def ready(self) -> bool:
        """Return whether the image stayed alive under sandbox restrictions."""

        return self.sandbox_container_ready and self.error is None


class RuntimePreflight(BaseModel):
    """Non-secret evidence that the local runtime is ready for a live run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    openclaw_binary: str = Field(min_length=1)
    openclaw_version: str = Field(min_length=1)
    openclaw_state_dir: str = Field(min_length=1)
    runtime_config: str = Field(min_length=1)
    sandbox_binary: str = Field(min_length=1)
    sandbox_version: str = Field(min_length=1)
    sandbox_image: str = Field(min_length=1)
    sandbox_image_id: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    config_valid: bool
    sandbox_image_present: bool
    sandbox_container_ready: bool
    sandbox_container_error: str | None = Field(default=None, max_length=1000)

    @property
    def ready(self) -> bool:
        """Return whether every offline prerequisite is available."""

        return (
            self.config_valid
            and self.sandbox_image_present
            and self.sandbox_image_id is not None
            and self.sandbox_container_ready
            and self.sandbox_container_error is None
        )


def persist_runtime_preflight(
    preflight: RuntimePreflight,
    destination: Path,
) -> Path:
    """Persist write-once, non-secret runtime readiness evidence."""

    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if destination.parent.is_symlink() or not destination.parent.is_dir():
        raise RuntimeConfigurationError("preflight parent must be a real directory")
    if destination.exists() or destination.is_symlink():
        raise RuntimeConfigurationError(
            f"runtime preflight already exists: {destination}"
        )
    content = (
        json.dumps(
            preflight.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    ).encode()
    temporary = destination.parent / f".{destination.name}.{uuid4().hex}.tmp"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError as error:
            raise RuntimeConfigurationError(
                f"runtime preflight already exists: {destination}"
            ) from error
        _fsync_directory(destination.parent)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def inspect_sandbox_image(
    *,
    sandbox_binary: str,
    sandbox_image: str,
    timeout_seconds: int = 30,
    environment: Mapping[str, str] | None = None,
) -> SandboxImageInspection:
    """Resolve a local Docker reference without pulling or running an image."""

    if timeout_seconds < 1:
        raise RuntimeConfigurationError("preflight timeout must be positive")
    if (
        not sandbox_image.strip()
        or sandbox_image != sandbox_image.strip()
        or sandbox_image.startswith("-")
        or any(character in sandbox_image for character in ("\x00", "\r", "\n"))
    ):
        raise RuntimeConfigurationError("sandbox image reference is invalid")
    resolved_sandbox = shutil.which(sandbox_binary)
    if resolved_sandbox is None:
        raise RuntimeConfigurationError(
            f"sandbox binary is unavailable: {sandbox_binary}"
        )
    try:
        sandbox_version = subprocess.run(
            [resolved_sandbox, "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=environment,
        )
        image = subprocess.run(
            [
                resolved_sandbox,
                "image",
                "inspect",
                "--format",
                "{{.Id}}",
                sandbox_image,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeConfigurationError("sandbox preflight command failed") from error

    sandbox_version_text = (
        sandbox_version.stdout.strip() or sandbox_version.stderr.strip()
    )
    if "docker version" not in sandbox_version_text.lower():
        raise RuntimeConfigurationError("sandbox binary is not Docker")
    image_id = image.stdout.strip() if image.returncode == 0 else None
    if image_id is not None and not (
        image_id.startswith("sha256:")
        and len(image_id) == 71
        and all(character in "0123456789abcdef" for character in image_id[7:])
    ):
        raise RuntimeConfigurationError("Docker returned an invalid sandbox image ID")

    return SandboxImageInspection(
        sandbox_binary=resolved_sandbox,
        sandbox_version=sandbox_version_text,
        sandbox_image=sandbox_image,
        sandbox_image_id=image_id,
        sandbox_image_present=image.returncode == 0,
    )


def probe_sandbox_runtime(
    *,
    sandbox_binary: str,
    sandbox_image_id: str,
    timeout_seconds: int = 30,
    settle_seconds: float = 0.2,
    environment: Mapping[str, str] | None = None,
) -> SandboxRuntimeProbe:
    """Verify that a restricted sandbox can start and execute a tool helper.

    OpenClaw starts a long-lived container and then executes file and process
    helpers inside it. Merely resolving an image or observing a successful
    ``docker run`` is insufficient. This probe supplies OpenClaw's explicit
    supervisor command, applies the same process and resource boundaries, runs
    one Python helper, inspects the resulting state, and removes the container.
    It never calls a model.
    """

    if timeout_seconds < 1:
        raise RuntimeConfigurationError("preflight timeout must be positive")
    if not 0 <= settle_seconds <= 5:
        raise RuntimeConfigurationError("sandbox settle duration is invalid")
    if not (
        sandbox_image_id.startswith("sha256:")
        and len(sandbox_image_id) == 71
        and all(character in "0123456789abcdef" for character in sandbox_image_id[7:])
    ):
        raise RuntimeConfigurationError("sandbox image ID is invalid")
    resolved_sandbox = shutil.which(sandbox_binary)
    if resolved_sandbox is None:
        raise RuntimeConfigurationError(
            f"sandbox binary is unavailable: {sandbox_binary}"
        )

    container_name = f"sat-runtime-probe-{uuid4().hex}"
    start_attempted = False
    created = False
    ready = False
    error_detail: str | None = None
    try:
        start_attempted = True
        started = subprocess.run(
            [
                resolved_sandbox,
                "run",
                "--detach",
                "--name",
                container_name,
                "--pull",
                "never",
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--user",
                f"{os.getuid()}:{os.getgid()}",
                "--env",
                "HOME=/tmp",
                "--tmpfs",
                "/tmp:rw,nosuid,nodev,size=128m",
                "--tmpfs",
                "/var/tmp:rw,nosuid,nodev,size=32m",
                "--tmpfs",
                "/run:rw,nosuid,nodev,size=16m",
                "--pids-limit",
                "128",
                "--memory",
                "512m",
                "--memory-swap",
                "512m",
                "--cpus",
                "1",
                "--ulimit",
                "nofile=1024:1024",
                "--workdir",
                "/workspace",
                "--label",
                "software-agent-team.runtime-probe=true",
                sandbox_image_id,
                "sleep",
                "infinity",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=environment,
        )
        created = started.returncode == 0
        if not created:
            error_detail = (
                "Docker could not start the sandbox probe "
                f"(exit code {started.returncode})"
            )
        else:
            time.sleep(settle_seconds)
            tool_check = subprocess.run(
                [
                    resolved_sandbox,
                    "exec",
                    "--workdir",
                    "/workspace",
                    container_name,
                    "python",
                    "-c",
                    "from pathlib import Path; assert Path('.').is_dir()",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                env=environment,
            )
            inspected = subprocess.run(
                [
                    resolved_sandbox,
                    "container",
                    "inspect",
                    "--format",
                    "{{json .State}}",
                    container_name,
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                env=environment,
            )
            if inspected.returncode != 0:
                error_detail = (
                    "Docker could not inspect the started sandbox probe "
                    f"(exit code {inspected.returncode})"
                )
            else:
                try:
                    state = json.loads(inspected.stdout)
                except (json.JSONDecodeError, TypeError):
                    error_detail = "Docker returned invalid sandbox probe state"
                else:
                    if not isinstance(state, dict):
                        error_detail = "Docker returned invalid sandbox probe state"
                    elif state.get("Running") is True:
                        if tool_check.returncode == 0:
                            ready = True
                        else:
                            error_detail = (
                                "sandbox probe could not execute its Python tool "
                                f"helper (exit_code={tool_check.returncode})"
                            )
                    else:
                        status = str(state.get("Status") or "unknown")
                        exit_code = state.get("ExitCode")
                        oom_killed = state.get("OOMKilled") is True
                        error_detail = (
                            "sandbox probe exited before tool execution "
                            f"(status={status}, exit_code={exit_code}, "
                            f"oom_killed={str(oom_killed).lower()})"
                        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeConfigurationError("sandbox runtime probe failed") from error
    finally:
        if created:
            try:
                removed = subprocess.run(
                    [
                        resolved_sandbox,
                        "container",
                        "rm",
                        "--force",
                        container_name,
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=timeout_seconds,
                    env=environment,
                )
            except (OSError, subprocess.SubprocessError) as cleanup_error:
                raise RuntimeConfigurationError(
                    "sandbox runtime probe cleanup failed"
                ) from cleanup_error
            if removed.returncode != 0:
                raise RuntimeConfigurationError("sandbox runtime probe cleanup failed")
        elif start_attempted:
            with suppress(OSError, subprocess.SubprocessError):
                subprocess.run(
                    [
                        resolved_sandbox,
                        "container",
                        "rm",
                        "--force",
                        container_name,
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=timeout_seconds,
                    env=environment,
                )

    return SandboxRuntimeProbe(
        sandbox_binary=resolved_sandbox,
        sandbox_image_id=sandbox_image_id,
        sandbox_container_ready=ready,
        error=error_detail,
    )


def materialize_run_configuration(
    template_path: Path,
    destination: Path,
    *,
    manifest: TeamManifest,
    workspace: Path,
    sandbox_image: str,
    sandbox_memory_mb: int = 512,
    sandbox_cpus: float = 1.0,
    sandbox_pids_limit: int = 128,
    sandbox_open_files: int = 1024,
    sandbox_tmpfs_mb: int = 128,
    sandbox_user: str | None = None,
    model: str | None = None,
) -> Path:
    """Create a secret-free OpenClaw config bound to one verified workspace.

    The checked-in template owns role permissions. This function changes only
    machine-local runtime values: every Agent's workspace, sandbox scope, and
    the prebuilt sandbox image. Provider credentials remain in OpenClaw's
    external state or the trusted caller environment.
    """

    if not sandbox_image.strip():
        raise RuntimeConfigurationError("sandbox image must not be blank")
    if not 64 <= sandbox_memory_mb <= 32_768:
        raise RuntimeConfigurationError("sandbox memory limit is invalid")
    if not 0 < sandbox_cpus <= 8:
        raise RuntimeConfigurationError("sandbox CPU limit is invalid")
    if not 8 <= sandbox_pids_limit <= 4096:
        raise RuntimeConfigurationError("sandbox process limit is invalid")
    if not 32 <= sandbox_open_files <= 65_536:
        raise RuntimeConfigurationError("sandbox open-file limit is invalid")
    if not 16 <= sandbox_tmpfs_mb <= 8192:
        raise RuntimeConfigurationError("sandbox tmpfs limit is invalid")
    if model is not None:
        model = model.strip()
        if not model:
            raise RuntimeConfigurationError("run model must not be blank")
    try:
        resolved_workspace = workspace.resolve(strict=True)
    except OSError as error:
        raise RuntimeConfigurationError("run workspace does not exist") from error
    if not resolved_workspace.is_dir() or resolved_workspace.is_symlink():
        raise RuntimeConfigurationError("run workspace must be a real directory")
    if sandbox_user is None:
        sandbox_uid = os.getuid()
        sandbox_gid = os.getgid()
    else:
        parts = sandbox_user.split(":")
        if len(parts) != 2 or not all(part.isdecimal() for part in parts):
            raise RuntimeConfigurationError("sandbox user must use numeric UID:GID")
        sandbox_uid, sandbox_gid = (int(part) for part in parts)
    if sandbox_uid == 0 or sandbox_gid == 0:
        raise RuntimeConfigurationError(
            "live Agent sandboxes require an unprivileged host user"
        )

    config = load_openclaw_template(template_path, manifest)
    # Round-trip through JSON so the caller-owned parsed template is not
    # accidentally mutated through shared nested values.
    payload: dict[str, Any] = json.loads(json.dumps(config))
    agents = payload["agents"]
    defaults = agents["defaults"]
    defaults["repoRoot"] = str(resolved_workspace)
    defaults["skipBootstrap"] = True
    if model is not None:
        defaults["model"] = {"primary": model, "fallbacks": []}
    sandbox = defaults["sandbox"]
    sandbox["scope"] = "session"
    docker = sandbox.setdefault("docker", {})
    docker.update(
        {
            "image": sandbox_image,
            "network": "none",
            "readOnlyRoot": True,
            "capDrop": ["ALL"],
            "user": f"{sandbox_uid}:{sandbox_gid}",
            "env": {
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "HOME": "/tmp",
                "LANG": "C.UTF-8",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONUNBUFFERED": "1",
                "RUFF_CACHE_DIR": "/tmp/ruff-cache",
                "TMPDIR": "/tmp",
                "XDG_CACHE_HOME": "/tmp/cache",
                "XDG_CONFIG_HOME": "/tmp/config",
            },
            "pidsLimit": sandbox_pids_limit,
            "memory": f"{sandbox_memory_mb}m",
            "memorySwap": f"{sandbox_memory_mb}m",
            "cpus": sandbox_cpus,
            "tmpfs": [
                f"/tmp:rw,nosuid,nodev,size={sandbox_tmpfs_mb}m",
                "/var/tmp:rw,nosuid,nodev,size=32m",
                "/run:rw,nosuid,nodev,size=16m",
            ],
            "ulimits": {
                "nofile": {
                    "soft": sandbox_open_files,
                    "hard": sandbox_open_files,
                }
            },
        }
    )
    for agent in agents["list"]:
        agent["workspace"] = str(resolved_workspace)

    destination_parent = destination.parent
    destination_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if destination_parent.is_symlink() or not destination_parent.is_dir():
        raise RuntimeConfigurationError(
            "runtime configuration parent must be a real directory"
        )
    if destination.exists() or destination.is_symlink():
        raise RuntimeConfigurationError(
            f"runtime configuration already exists: {destination}"
        )

    content = f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n".encode()
    temporary = destination_parent / f".{destination.name}.{uuid4().hex}.tmp"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError as error:
            raise RuntimeConfigurationError(
                f"runtime configuration already exists: {destination}"
            ) from error
        os.chmod(destination, 0o600)
        _fsync_directory(destination_parent)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def inspect_runtime_preflight(
    *,
    openclaw_binary: Path,
    openclaw_state_dir: Path,
    runtime_config: Path,
    sandbox_binary: str,
    sandbox_image: str,
    expected_sandbox_image_id: str | None = None,
    timeout_seconds: int = 30,
) -> RuntimePreflight:
    """Check binaries, config, and sandbox lifecycle without model calls."""

    if timeout_seconds < 1:
        raise RuntimeConfigurationError("preflight timeout must be positive")
    if not openclaw_binary.is_file() or not os.access(openclaw_binary, os.X_OK):
        raise RuntimeConfigurationError("OpenClaw binary is unavailable")
    if not runtime_config.is_file() or runtime_config.is_symlink():
        raise RuntimeConfigurationError("runtime configuration is unavailable")
    if not openclaw_state_dir.is_dir() or openclaw_state_dir.is_symlink():
        raise RuntimeConfigurationError("SAT OpenClaw state directory is unavailable")
    environment = {
        **os.environ,
        **isolated_openclaw_environment(
            state_dir=openclaw_state_dir,
            config_path=runtime_config,
        ),
    }
    try:
        version = subprocess.run(
            [str(openclaw_binary), "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=environment,
        )
        config = subprocess.run(
            [str(openclaw_binary), "config", "validate", "--json"],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeConfigurationError("runtime preflight command failed") from error

    sandbox = inspect_sandbox_image(
        sandbox_binary=sandbox_binary,
        sandbox_image=sandbox_image,
        timeout_seconds=timeout_seconds,
        environment=environment,
    )
    if (
        expected_sandbox_image_id is not None
        and sandbox.sandbox_image_id != expected_sandbox_image_id
    ):
        raise RuntimeConfigurationError(
            "sandbox image identity changed after it was frozen"
        )
    if sandbox.sandbox_image_id is None:
        sandbox_container_ready = False
        sandbox_container_error = "sandbox image is unavailable"
    else:
        probe = probe_sandbox_runtime(
            sandbox_binary=sandbox.sandbox_binary,
            sandbox_image_id=sandbox.sandbox_image_id,
            timeout_seconds=timeout_seconds,
            environment=environment,
        )
        sandbox_container_ready = probe.ready
        sandbox_container_error = probe.error

    return RuntimePreflight(
        openclaw_binary=str(openclaw_binary.resolve(strict=True)),
        openclaw_version=version.stdout.strip() or version.stderr.strip(),
        openclaw_state_dir=str(openclaw_state_dir.resolve(strict=True)),
        runtime_config=str(runtime_config.resolve(strict=True)),
        sandbox_binary=sandbox.sandbox_binary,
        sandbox_version=sandbox.sandbox_version,
        sandbox_image=sandbox.sandbox_image,
        sandbox_image_id=sandbox.sandbox_image_id,
        config_valid=config.returncode == 0,
        sandbox_image_present=sandbox.sandbox_image_present,
        sandbox_container_ready=sandbox_container_ready,
        sandbox_container_error=sandbox_container_error,
    )
