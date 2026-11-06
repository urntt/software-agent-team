"""Runtime preflight execution, validation diagnostics, and evidence persistence."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

from software_agent_team.model_runtime import ModelRuntimeProfile
from software_agent_team.openclaw_runtime import isolated_openclaw_environment
from software_agent_team.terminal_presentation import display_text

from .types import (
    CONFIG_VALIDATION_TIMEOUT_SECONDS,
    MODEL_INSPECTION_TIMEOUT_SECONDS,
    PREFLIGHT_COMMAND_TIMEOUT_SECONDS,
    OpenClawModelInspection,
    RuntimeConfigurationError,
    RuntimePreflight,
)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _persist_private_json(
    payload: Mapping[str, Any],
    destination: Path,
    *,
    label: str,
) -> Path:
    destination_parent = destination.parent
    destination_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if destination_parent.is_symlink() or not destination_parent.is_dir():
        raise RuntimeConfigurationError(f"{label} parent must be a real directory")
    if destination.exists() or destination.is_symlink():
        raise RuntimeConfigurationError(f"{label} already exists: {destination}")

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
                f"{label} already exists: {destination}"
            ) from error
        os.chmod(destination, 0o600)
        _fsync_directory(destination_parent)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


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


def _run_openclaw_preflight_command(
    *,
    label: str,
    argv: list[str],
    environment: Mapping[str, str],
    timeout_seconds: int,
    check: bool,
) -> subprocess.CompletedProcess[str]:
    """Run one named OpenClaw check with bounded, non-secret diagnostics."""

    try:
        return subprocess.run(
            argv,
            check=check,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=environment,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeConfigurationError(
            f"{label} timed out after {timeout_seconds} seconds"
        ) from error
    except subprocess.CalledProcessError as error:
        raise RuntimeConfigurationError(
            f"{label} exited with status {error.returncode}"
        ) from error
    except OSError as error:
        errno_detail = "" if error.errno is None else f"; errno={error.errno}"
        raise RuntimeConfigurationError(
            f"{label} could not start ({type(error).__name__}{errno_detail})"
        ) from error
    except subprocess.SubprocessError as error:
        raise RuntimeConfigurationError(
            f"{label} failed ({type(error).__name__})"
        ) from error


def _config_validation_error(stdout: str, stderr: str, returncode: int) -> str:
    """Preserve bounded runtime validation paths without leaking credentials."""

    messages: list[str] = []
    try:
        payload = json.loads(stdout)
    except (ValueError, TypeError):
        payload = None
    if isinstance(payload, dict) and isinstance(payload.get("issues"), list):
        for issue in payload["issues"][:8]:
            if isinstance(issue, dict):
                path, message = issue.get("path"), issue.get("message")
                if isinstance(path, str) and isinstance(message, str):
                    messages.append(f"{path}: {message}")
    detail = display_text("; ".join(messages) or stderr or stdout, limit=900)
    return f"OpenClaw configuration validation exited {returncode}" + (
        f": {detail}" if detail else ""
    )


def inspect_runtime_preflight(
    *,
    openclaw_binary: Path,
    openclaw_state_dir: Path,
    runtime_config: Path,
    sandbox_binary: str,
    sandbox_image: str,
    expected_sandbox_image_id: str | None = None,
    expected_model: str | None = None,
    expected_runtime_profile: ModelRuntimeProfile | None = None,
    timeout_seconds: int = PREFLIGHT_COMMAND_TIMEOUT_SECONDS,
    config_validation_timeout_seconds: int = CONFIG_VALIDATION_TIMEOUT_SECONDS,
    model_inspection_timeout_seconds: int = MODEL_INSPECTION_TIMEOUT_SECONDS,
) -> RuntimePreflight:
    """Check config, selected model, and sandbox execution without model calls."""

    import software_agent_team.runtime_configuration as runtime_configuration

    if expected_model is None and expected_runtime_profile is not None:
        raise RuntimeConfigurationError(
            "runtime preflight profile requires an expected model"
        )
    if timeout_seconds < 1:
        raise RuntimeConfigurationError("preflight timeout must be positive")
    if config_validation_timeout_seconds < 1:
        raise RuntimeConfigurationError(
            "configuration validation timeout must be positive"
        )
    if model_inspection_timeout_seconds < 1:
        raise RuntimeConfigurationError("model inspection timeout must be positive")
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
    version = _run_openclaw_preflight_command(
        label="OpenClaw version check",
        argv=[str(openclaw_binary), "--version"],
        environment=environment,
        timeout_seconds=timeout_seconds,
        check=True,
    )
    config = _run_openclaw_preflight_command(
        label="OpenClaw configuration validation",
        argv=[str(openclaw_binary), "config", "validate", "--json"],
        environment=environment,
        timeout_seconds=config_validation_timeout_seconds,
        check=False,
    )

    model_inspection: OpenClawModelInspection | None = None
    if expected_model is not None:
        if config.returncode == 0:
            model_inspection = runtime_configuration.inspect_openclaw_model(
                openclaw_binary=openclaw_binary,
                openclaw_state_dir=openclaw_state_dir,
                config_path=runtime_config,
                model=expected_model,
                runtime_profile=expected_runtime_profile,
                timeout_seconds=model_inspection_timeout_seconds,
            )
        else:
            model_inspection = OpenClawModelInspection(
                model=expected_model,
                runtime_profile_sha256=(
                    expected_runtime_profile.sha256
                    if expected_runtime_profile is not None
                    else None
                ),
                api=(
                    expected_runtime_profile.api
                    if expected_runtime_profile is not None
                    else None
                ),
                endpoint_kind=(
                    expected_runtime_profile.endpoint_kind
                    if expected_runtime_profile is not None
                    else None
                ),
                available=False,
                error="OpenClaw configuration is invalid",
            )

    sandbox = runtime_configuration.inspect_sandbox_image(
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
        probe = runtime_configuration.probe_sandbox_runtime(
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
        command_timeout_seconds=timeout_seconds,
        config_validation_timeout_seconds=config_validation_timeout_seconds,
        model_inspection_timeout_seconds=model_inspection_timeout_seconds,
        sandbox_binary=sandbox.sandbox_binary,
        sandbox_version=sandbox.sandbox_version,
        sandbox_image=sandbox.sandbox_image,
        sandbox_image_id=sandbox.sandbox_image_id,
        config_valid=config.returncode == 0,
        config_error=(
            None
            if config.returncode == 0
            else _config_validation_error(
                config.stdout, config.stderr, config.returncode
            )
        ),
        sandbox_image_present=sandbox.sandbox_image_present,
        sandbox_container_ready=sandbox_container_ready,
        sandbox_container_error=sandbox_container_error,
        model=(model_inspection.model if model_inspection is not None else None),
        model_available=(
            model_inspection.available if model_inspection is not None else None
        ),
        model_error=(model_inspection.error if model_inspection is not None else None),
        model_local=(model_inspection.local if model_inspection is not None else None),
        model_request_timeout_seconds=(
            model_inspection.provider_request_timeout_seconds
            if model_inspection is not None
            else None
        ),
        model_inspections=(() if model_inspection is None else (model_inspection,)),
    )


__all__ = [
    "_config_validation_error",
    "_fsync_directory",
    "_persist_private_json",
    "_run_openclaw_preflight_command",
    "inspect_runtime_preflight",
    "persist_runtime_preflight",
]
