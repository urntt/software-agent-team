"""Offline validation for team and sanitized OpenClaw configurations."""

from pathlib import Path
from typing import Any

import json5

from software_agent_team.artifacts import AgentRole
from software_agent_team.teams import TeamManifest, load_team_manifest

READ_ONLY_ROLES = {
    AgentRole.CLARIFIER,
    AgentRole.PLANNER,
    AgentRole.TESTER,
    AgentRole.REVIEWER,
}
WRITE_ROLES = {
    AgentRole.SINGLE_AGENT,
    AgentRole.GENERALIST_DEVELOPER,
    AgentRole.FRONTEND_DEVELOPER,
    AgentRole.BACKEND_DEVELOPER,
    AgentRole.INTEGRATOR,
}
MUTATING_TOOLS = {"write", "edit", "apply_patch", "exec", "process"}
UNTRACKED_AGENT_TOOLS = {
    "sessions_spawn",
    "sessions_yield",
    "subagents",
    "llm_task",
}


def load_openclaw_template(
    path: Path,
    manifest: TeamManifest,
) -> dict[str, Any]:
    """Load the JSON5 template and verify roles, workspaces, and permissions."""

    raw = path.read_text(encoding="utf-8")
    config = json5.loads(raw)

    try:
        agents = config["agents"]["list"]
        defaults = config["agents"]["defaults"]
    except (KeyError, TypeError) as error:
        raise ValueError(
            "OpenClaw template must define agents.defaults and list"
        ) from error

    if not isinstance(agents, list) or not agents:
        raise ValueError("OpenClaw template must contain at least one Agent")

    configured_ids = [agent.get("id") for agent in agents]
    if any(not isinstance(agent_id, str) for agent_id in configured_ids):
        raise ValueError("every OpenClaw Agent requires a string id")
    if len(configured_ids) != len(set(configured_ids)):
        raise ValueError("OpenClaw Agent IDs must be unique")

    expected_ids = {role.value for role in manifest.required_roles}
    if set(configured_ids) != expected_ids:
        raise ValueError(
            "OpenClaw Agent IDs must exactly match the team manifest role registry"
        )

    default_ids = [agent["id"] for agent in agents if agent.get("default") is True]
    if default_ids != [AgentRole.CLARIFIER.value]:
        raise ValueError("clarifier must be the only default OpenClaw Agent")

    default_access = defaults.get("sandbox", {}).get("workspaceAccess")
    if default_access != "ro":
        raise ValueError("OpenClaw Agent workspaces must be read-only by default")
    if defaults.get("skills") != []:
        raise ValueError("OpenClaw Agents must disable ambient runtime skills")

    for agent in agents:
        role = AgentRole(agent["id"])
        expected_workspace_suffix = f"/openclaw/workspaces/{role.value}"
        workspace = agent.get("workspace")
        if not isinstance(workspace, str) or not workspace.endswith(
            expected_workspace_suffix
        ):
            raise ValueError(
                f"OpenClaw workspace for {role.value} must use its stable role path"
            )

        access = agent.get("sandbox", {}).get("workspaceAccess", default_access)
        denied_tools = set(agent.get("tools", {}).get("deny", []))
        if not UNTRACKED_AGENT_TOOLS.issubset(denied_tools):
            raise ValueError(
                f"{role.value} must deny Agent calls outside controller accounting"
            )

        if role in READ_ONLY_ROLES:
            if access != "ro":
                raise ValueError(f"{role.value} must have read-only workspace access")
            required_denied = MUTATING_TOOLS - (
                {"exec"} if role is AgentRole.REVIEWER else set()
            )
            if not required_denied.issubset(denied_tools):
                raise ValueError(
                    f"{role.value} must deny filesystem and shell mutation tools"
                )
        elif role in WRITE_ROLES and access != "rw":
            raise ValueError(f"{role.value} must have read-write workspace access")
        else:
            if role not in READ_ONLY_ROLES | WRITE_ROLES:
                raise ValueError(f"no permission policy exists for {role.value}")

    return config


def validate_environment_configuration(
    team_path: Path,
    openclaw_path: Path,
) -> tuple[TeamManifest, dict[str, Any]]:
    """Validate the complete checked-in configuration boundary."""

    manifest = load_team_manifest(team_path)
    openclaw = load_openclaw_template(openclaw_path, manifest)
    return manifest, openclaw
