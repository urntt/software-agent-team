"""Run-scoped cleanup for OpenClaw's long-lived Agent sandboxes."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from software_agent_team.artifacts import AgentRole
from software_agent_team.execution import (
    ROLE_ARTIFACT_KINDS,
    stable_agent_session_key,
    stable_session_key,
)
from software_agent_team.teams import AgentCapability, AgentSpec

_CONTAINER_ID = re.compile(r"^[a-f0-9]{12,64}$")


class SandboxCleanupError(RuntimeError):
    """Raised when SAT cannot prove that its run sandboxes were removed."""


@dataclass(frozen=True)
class RemovedSandbox:
    """One exact SAT-owned OpenClaw sandbox removed after a run."""

    container_id: str
    container_name: str
    session_key: str


@dataclass(frozen=True)
class SandboxCleanupResult:
    """Observable result of one bounded run-scoped cleanup."""

    run_id: str
    removed: tuple[RemovedSandbox, ...]


ProcessRunner = Callable[..., subprocess.CompletedProcess[str]]


def _run_command(
    argv: list[str],
    *,
    runner: ProcessRunner,
    timeout_seconds: int,
) -> subprocess.CompletedProcess[str]:
    try:
        return runner(
            argv,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            shell=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise SandboxCleanupError(
            f"sandbox cleanup command could not complete: {argv[1]}"
        ) from error


def _expected_session_keys(
    run_id: str,
    iteration_limit: int,
    *,
    roles: Sequence[AgentRole] | None,
    agents: Sequence[AgentSpec] | None,
) -> frozenset[str]:
    if agents is not None:
        return frozenset(
            stable_agent_session_key(
                run_id=run_id,
                agent_id=agent.id,
                iteration=iteration,
                expected_kind=agent.expected_output,
            )
            for agent in agents
            for iteration in (
                (1,)
                if agent.capability
                in {AgentCapability.CLARIFICATION, AgentCapability.PLANNING}
                else range(1, iteration_limit + 1)
            )
        )
    assert roles is not None
    return frozenset(
        stable_session_key(
            run_id=run_id,
            role=role,
            iteration=iteration,
            expected_kind=kind,
        )
        for role in roles
        for kind in ROLE_ARTIFACT_KINDS[role]
        for iteration in (
            (1,) if role is AgentRole.PLANNER else range(1, iteration_limit + 1)
        )
    )


def _owned_mount(
    value: object,
    *,
    state_root: Path,
    workspace_root: Path,
) -> bool:
    if not isinstance(value, str) or not value.startswith("/"):
        return False
    source = Path(value).resolve(strict=False)
    return (
        source in (state_root, workspace_root)
        or source.is_relative_to(state_root)
        or source.is_relative_to(workspace_root)
    )


def cleanup_run_sandbox_containers(
    *,
    sandbox_binary: str,
    run_id: str,
    openclaw_state_dir: Path,
    workspace_dir: Path,
    iteration_limit: int,
    roles: Sequence[AgentRole] | None = None,
    agents: Sequence[AgentSpec] | None = None,
    timeout_seconds: int = 30,
    runner: ProcessRunner = subprocess.run,
) -> SandboxCleanupResult:
    """Remove only live or stopped OpenClaw sandboxes owned by one SAT run.

    OpenClaw intentionally keeps session-scoped containers alive for reuse.
    SAT session keys are immutable and run-specific, so retaining those
    containers can only leak processes and device resources. Selection requires
    both an exact controller-generated session key and a bind mount beneath this
    SAT state/workspace boundary; another OpenClaw installation is never a
    cleanup target.
    """

    if not sandbox_binary.strip():
        raise SandboxCleanupError("sandbox binary must not be blank")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", run_id):
        raise SandboxCleanupError("run ID is not safe for sandbox cleanup")
    if iteration_limit < 1 or iteration_limit > 3:
        raise SandboxCleanupError("iteration limit is outside the supported range")
    if (roles is None) == (agents is None):
        raise SandboxCleanupError(
            "sandbox cleanup requires exactly one role or AgentSpec collection"
        )
    if roles is not None:
        if not roles or any(role not in ROLE_ARTIFACT_KINDS for role in roles):
            raise SandboxCleanupError("sandbox cleanup roles are not executable")
        if len(roles) != len(set(roles)):
            raise SandboxCleanupError("sandbox cleanup roles must be unique")
    if agents is not None:
        agent_ids = [agent.id for agent in agents]
        if not agents or len(agent_ids) != len(set(agent_ids)):
            raise SandboxCleanupError(
                "sandbox cleanup AgentSpecs must be non-empty and unique"
            )
    if timeout_seconds < 1:
        raise SandboxCleanupError("sandbox cleanup timeout must be positive")
    if not openclaw_state_dir.is_absolute() or not workspace_dir.is_absolute():
        raise SandboxCleanupError("sandbox cleanup paths must be absolute")

    state_root = openclaw_state_dir.resolve(strict=False)
    workspace_root = workspace_dir.resolve(strict=False)
    expected_sessions = _expected_session_keys(
        run_id,
        iteration_limit,
        roles=roles,
        agents=agents,
    )

    discovered_ids: set[str] = set()
    for session_key in sorted(expected_sessions):
        listed = _run_command(
            [
                sandbox_binary,
                "container",
                "ls",
                "--all",
                "--quiet",
                "--filter",
                "label=openclaw.sandbox=1",
                "--filter",
                f"label=openclaw.sessionKey={session_key}",
            ],
            runner=runner,
            timeout_seconds=timeout_seconds,
        )
        if listed.returncode != 0:
            raise SandboxCleanupError("Docker could not list run-scoped sandboxes")
        discovered_ids.update(
            line.strip() for line in listed.stdout.splitlines() if line.strip()
        )
    container_ids = tuple(sorted(discovered_ids))
    if any(_CONTAINER_ID.fullmatch(item) is None for item in container_ids):
        raise SandboxCleanupError("Docker returned an invalid container ID")
    if not container_ids:
        return SandboxCleanupResult(run_id=run_id, removed=())

    inspected = _run_command(
        [sandbox_binary, "container", "inspect", *container_ids],
        runner=runner,
        timeout_seconds=timeout_seconds,
    )
    if inspected.returncode != 0:
        raise SandboxCleanupError("Docker could not inspect OpenClaw sandboxes")
    try:
        payload = json.loads(inspected.stdout)
    except json.JSONDecodeError as error:
        raise SandboxCleanupError(
            "Docker sandbox inspection was not valid JSON"
        ) from error
    if not isinstance(payload, list):
        raise SandboxCleanupError("Docker sandbox inspection must be a JSON array")

    targets: list[RemovedSandbox] = []
    unowned_matches: list[str] = []
    for item in payload:
        if not isinstance(item, dict):
            raise SandboxCleanupError("Docker returned an invalid sandbox record")
        config = item.get("Config")
        labels = config.get("Labels") if isinstance(config, dict) else None
        if not isinstance(labels, dict):
            continue
        session_key = labels.get("openclaw.sessionKey")
        if session_key not in expected_sessions:
            continue
        container_id = item.get("Id")
        container_name = item.get("Name")
        mounts = item.get("Mounts")
        if (
            not isinstance(container_id, str)
            or _CONTAINER_ID.fullmatch(container_id) is None
            or not isinstance(container_name, str)
            or not isinstance(mounts, list)
        ):
            raise SandboxCleanupError(
                "Docker returned incomplete sandbox ownership data"
            )
        owned = any(
            isinstance(mount, dict)
            and _owned_mount(
                mount.get("Source"),
                state_root=state_root,
                workspace_root=workspace_root,
            )
            for mount in mounts
        )
        if not owned:
            unowned_matches.append(container_name)
            continue
        targets.append(
            RemovedSandbox(
                container_id=container_id,
                container_name=container_name.removeprefix("/"),
                session_key=session_key,
            )
        )

    if unowned_matches:
        raise SandboxCleanupError(
            "refusing to remove a matching sandbox outside SAT-owned paths"
        )

    removed: list[RemovedSandbox] = []
    failures: list[str] = []
    for target in targets:
        completed = _run_command(
            [
                sandbox_binary,
                "container",
                "rm",
                "--force",
                target.container_id,
            ],
            runner=runner,
            timeout_seconds=timeout_seconds,
        )
        if completed.returncode == 0:
            removed.append(target)
        else:
            failures.append(target.container_name)
    if failures:
        raise SandboxCleanupError(
            f"Docker could not remove {len(failures)} run-scoped sandbox container(s)"
        )
    return SandboxCleanupResult(run_id=run_id, removed=tuple(removed))
