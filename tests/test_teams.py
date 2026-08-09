"""Tests for versioned Agent-team definitions."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from software_agent_team.artifacts import AgentRole
from software_agent_team.teams import TeamManifest, load_team_manifest

REPOSITORY_ROOT = Path(__file__).parents[1]
TEAM_CONFIG = REPOSITORY_ROOT / "configs" / "teams.json"


def load_payload() -> dict[str, object]:
    """Load a mutable copy of the checked-in team manifest."""

    return json.loads(TEAM_CONFIG.read_text(encoding="utf-8"))


def test_checked_in_team_manifest_defines_all_experiments() -> None:
    manifest = load_team_manifest(TEAM_CONFIG)

    assert manifest.default_team == "function_specialized"
    assert {team.id for team in manifest.teams} == {
        "single_agent",
        "function_specialized",
        "implementation_domain_specialized",
    }


def test_required_roles_include_clarification_and_both_topologies() -> None:
    manifest = load_team_manifest(TEAM_CONFIG)

    assert manifest.required_roles == set(AgentRole)
    assert "coordinator" not in {role.value for role in manifest.required_roles}


def test_function_specialized_team_is_the_initial_vertical_slice() -> None:
    manifest = load_team_manifest(TEAM_CONFIG)
    team = manifest.get_team("function_specialized")

    assert team.max_iterations == 3
    assert team.independent_review
    assert [stage.id for stage in team.stages] == ["plan", "implement", "verify"]


def test_domain_team_parallelizes_only_owned_work_and_verification() -> None:
    manifest = load_team_manifest(TEAM_CONFIG)
    team = manifest.get_team("implementation_domain_specialized")

    parallel_stages = [stage for stage in team.stages if stage.mode == "parallel"]
    assert [stage.id for stage in parallel_stages] == ["implement", "verify"]


def test_default_team_must_exist() -> None:
    payload = load_payload()
    payload["default_team"] = "missing"

    with pytest.raises(ValidationError, match="default_team"):
        TeamManifest.model_validate(payload)


def test_stage_roles_must_exactly_match_team_roles() -> None:
    payload = load_payload()
    teams = payload["teams"]
    assert isinstance(teams, list)
    function_team = teams[1]
    function_team["stages"][1]["roles"] = ["frontend_developer"]

    with pytest.raises(ValidationError, match="exactly match"):
        TeamManifest.model_validate(payload)


def test_baseline_cannot_enable_review_driven_revision() -> None:
    payload = load_payload()
    teams = payload["teams"]
    assert isinstance(teams, list)
    baseline = teams[0]
    baseline["revision_enabled"] = True

    with pytest.raises(ValidationError, match="baseline cannot enable"):
        TeamManifest.model_validate(payload)


def test_handoff_role_must_belong_to_the_selected_team() -> None:
    manifest = load_team_manifest(TEAM_CONFIG)

    with pytest.raises(ValueError, match="is not part of team"):
        manifest.validate_handoff_boundary(
            team_id="function_specialized",
            iteration=1,
            source_role=AgentRole.FRONTEND_DEVELOPER,
            target_role=AgentRole.TESTER,
        )


def test_handoff_iteration_uses_the_selected_team_limit() -> None:
    manifest = load_team_manifest(TEAM_CONFIG)

    with pytest.raises(ValueError, match="exceeds single_agent limit"):
        manifest.validate_handoff_boundary(
            team_id="single_agent",
            iteration=2,
            source_role=AgentRole.SINGLE_AGENT,
            target_role=None,
        )
