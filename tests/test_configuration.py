"""Tests for checked-in environment and configuration examples."""

from pathlib import Path

from software_agent_team.artifacts import AgentRole
from software_agent_team.configuration import (
    READ_ONLY_ROLES,
    UNTRACKED_AGENT_TOOLS,
    WRITE_ROLES,
    validate_environment_configuration,
)

REPOSITORY_ROOT = Path(__file__).parents[1]
TEAM_CONFIG = REPOSITORY_ROOT / "configs" / "teams.json"
OPENCLAW_CONFIG = REPOSITORY_ROOT / "configs" / "openclaw.example.json5"


def test_environment_configuration_is_coherent() -> None:
    manifest, config = validate_environment_configuration(
        TEAM_CONFIG,
        OPENCLAW_CONFIG,
    )

    configured_ids = {agent["id"] for agent in config["agents"]["list"]}
    assert configured_ids == {role.value for role in manifest.required_roles}


def test_openclaw_permissions_match_role_responsibilities() -> None:
    _, config = validate_environment_configuration(TEAM_CONFIG, OPENCLAW_CONFIG)
    agents = {agent["id"]: agent for agent in config["agents"]["list"]}

    for role in READ_ONLY_ROLES:
        assert "sandbox" not in agents[role.value]
        assert {"write", "edit", "apply_patch", "exec", "process"}.issubset(
            agents[role.value]["tools"]["deny"]
        )

    for role in WRITE_ROLES:
        assert agents[role.value]["sandbox"]["workspaceAccess"] == "rw"

    for role in AgentRole:
        assert UNTRACKED_AGENT_TOOLS.issubset(agents[role.value]["tools"]["deny"])


def test_deterministic_controller_is_not_an_openclaw_agent() -> None:
    _, config = validate_environment_configuration(TEAM_CONFIG, OPENCLAW_CONFIG)

    configured_ids = {agent["id"] for agent in config["agents"]["list"]}
    assert "coordinator" not in configured_ids
    assert "controller" not in configured_ids


def test_openclaw_template_contains_no_provider_secrets() -> None:
    raw = OPENCLAW_CONFIG.read_text(encoding="utf-8")

    assert "API_KEY" not in raw
    assert "token:" not in raw.lower()


def test_benchmark_specification_is_present() -> None:
    specification = REPOSITORY_ROOT / "benchmarks" / "task_manager" / "requirements.md"

    assert specification.is_file()
    assert "## Acceptance Criteria" in specification.read_text(encoding="utf-8")


def test_public_project_documents_are_present() -> None:
    assert (REPOSITORY_ROOT / "README.md").is_file()
    assert (REPOSITORY_ROOT / "VISION.md").is_file()


def test_every_agent_role_has_a_permission_policy() -> None:
    assert READ_ONLY_ROLES.isdisjoint(WRITE_ROLES)
    assert set(AgentRole) == READ_ONLY_ROLES | WRITE_ROLES
