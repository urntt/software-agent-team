"""Constants, regex patterns, and error definitions for managed install operations.

This module provides common invariants, environmental overrides, timeouts,
and error handling utilities for atomic, verifiable SAT installation management.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

# --- Marker & Schema Versions ---
MANAGED_MARKER_NAME = ".sat-managed-install"
MANAGED_MARKER_SCHEMA_VERSION = 2
MANAGED_ROOT_MARKER_NAME = ".sat-managed-root"
MANAGED_ROOT_MARKER_SCHEMA_VERSION = 1

# --- Environmental Overrides ---
INSTALL_ROOT_ENVIRONMENT_VARIABLE = "SAT_INSTALL_ROOT"
BIN_DIRECTORY_ENVIRONMENT_VARIABLE = "SAT_BIN_DIR"
LATEST_RELEASE_API_ENVIRONMENT_VARIABLE = "SAT_RELEASE_API_URL"
DEFAULT_MANAGED_DIRECTORY_NAME = "software-agent-team"

# --- Compatibility & Timeout Bounds ---
CANDIDATE_COMPATIBILITY_TIMEOUT_SECONDS = 30
MAX_CANDIDATE_COMPATIBILITY_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_RENDERED_STATE_PROBLEMS = 6

# --- Docker Sandbox Image Labels & Transition Parameters ---
SANDBOX_IMAGE_OWNER_LABEL = "software-agent-team.sandbox-image"
SANDBOX_IMAGE_REFERENCE_LABEL = "software-agent-team.image-reference"
SANDBOX_IMAGE_COMMAND_TIMEOUT_SECONDS = 30
SANDBOX_IMAGE_TRANSITION_SCHEMA_VERSION = 1
SANDBOX_IMAGE_TRANSITION_FILE_PREFIX = ".sat-sandbox-image-transition-"
SANDBOX_IMAGE_ROLLBACK_REPOSITORY = "software-agent-team-rollback"

# --- Validation Regex Patterns ---
_SOURCE_REF_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_SOURCE_REVISION_PATTERN = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_SANDBOX_IMAGE_ID_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


class ManagedInstallError(RuntimeError):
    """Raised when a managed install cannot preserve its safety contract."""


def _managed_failure_detail(error: BaseException) -> str:
    """Render one bounded install-owned cause without reflecting command data."""
    if isinstance(error, ManagedInstallError):
        detail = " ".join(str(error).split())
        if detail:
            return detail[:500]
    return type(error).__name__


def _docker_failure_detail(
    completed: subprocess.CompletedProcess[str],
    *,
    operation: str,
) -> str:
    """Classify one failed Docker call without reflecting arbitrary stderr."""
    diagnostic = " ".join(f"{completed.stderr}\n{completed.stdout}".lower().split())
    if "permission denied" in diagnostic and (
        "docker" in diagnostic or "unix" in diagnostic or "socket" in diagnostic
    ):
        return (
            "Docker socket permission denied; grant this user access to the "
            "configured Docker daemon"
        )
    if any(
        marker in diagnostic
        for marker in (
            "cannot connect to the docker daemon",
            "is the docker daemon running",
            "connection refused",
            "docker daemon is not running",
            "error during connect",
        )
    ):
        return (
            "Docker daemon is unavailable; start the configured Docker daemon and retry"
        )
    if any(
        marker in diagnostic
        for marker in (
            "context deadline exceeded",
            "i/o timeout",
            "operation timed out",
            "request canceled while waiting",
        )
    ):
        return "Docker daemon did not respond before the command deadline"
    return (
        f"Docker {operation} failed (exit status {completed.returncode}); "
        "run 'docker version' to diagnose the configured daemon"
    )


def _is_sha256_digest(value: str) -> bool:
    """Validate that a string conforms to sha256:64hex digest format."""
    prefix = "sha256:"
    digest = value.removeprefix(prefix)
    return (
        value.startswith(prefix)
        and len(digest) == 64
        and all(character in "0123456789abcdef" for character in digest)
    )


def _is_relative_to(path: Path, parent: Path) -> bool:
    """Safe cross-platform containment check."""
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True
