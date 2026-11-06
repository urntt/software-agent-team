"""Sandbox container lifecycle and cleanup management for loopback validation.

Architecture & Intent / 架构与设计意图:
Handles inspection of running containers under disposable scenario state directories
and deterministic removal of session-keyed Docker sandbox resources.
Ensures no dangling Docker containers leak across scenarios or test runs.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from software_agent_team.sandbox_lifecycle import (
    cleanup_sat_sandbox_sessions,
    inspect_sat_sandbox_resources,
)


def sandbox_container_ids(docker_binary: str, state_root: Path) -> list[str]:
    """Observe the disposable scenario through the shared mount-ownership owner."""
    observation = inspect_sat_sandbox_resources(
        sandbox_binary=docker_binary, state_root=state_root
    )
    return sorted(item.container_id for item in observation.containers)


def remove_sandbox_sessions(
    docker_binary: str,
    session_keys: Sequence[str],
    *,
    state_root: Path,
) -> dict[str, object]:
    """Recover actual cache labels only inside this scenario's private state."""
    before = inspect_sat_sandbox_resources(
        sandbox_binary=docker_binary, state_root=state_root
    )
    actual_keys = sorted({item.session_key for item in before.containers})
    removed = (
        cleanup_sat_sandbox_sessions(
            sandbox_binary=docker_binary,
            session_keys=actual_keys,
            state_root=state_root,
        ).removed
        if actual_keys
        else ()
    )
    after = sandbox_container_ids(docker_binary, state_root)
    return {
        "session_keys": list(dict.fromkeys(session_keys)),
        "observed_session_keys": actual_keys,
        "container_ids_before_cleanup": sorted(
            item.container_id for item in before.containers
        ),
        "removed_container_ids": sorted(item.container_id for item in removed),
        "container_ids_after_cleanup": after,
        "completed": not after,
    }
