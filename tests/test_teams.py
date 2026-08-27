"""Tests for versioned Agent-team definitions."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from software_agent_team.artifacts import AgentRole
from software_agent_team.budgets import AgentBudget
from software_agent_team.teams import (
    ModelRoutingMode,
    PermissionProfile,
    PlanApprovalSource,
    TeamManifest,
    TeamPlan,
    TeamPlanOrigin,
    compile_fixed_team_plan,
    load_team_manifest,
    validate_fixed_team_plan,
)

REPOSITORY_ROOT = Path(__file__).parents[1]
TEAM_CONFIG = REPOSITORY_ROOT / "configs" / "teams.json"
FIXED_TIME = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


def budget(*, max_calls: int = 14) -> AgentBudget:
    """Return a bounded controller budget for TeamPlan tests."""

    return AgentBudget(
        max_calls=max_calls,
        max_input_tokens=1_000_000,
        max_output_tokens=200_000,
        max_agent_duration_seconds=7_200,
        max_estimated_cost_usd="25",
    )


def compiled_plan(
    team_id: str = "function_specialized",
    *,
    max_concurrency: int = 2,
) -> TeamPlan:
    """Compile a checked-in fixture with stable run inputs."""

    return compile_fixed_team_plan(
        load_team_manifest(TEAM_CONFIG),
        team_id=team_id,
        run_id="adaptive-contract-001",
        task_brief_sha256="d" * 64,
        model="test/provider-model",
        budget=budget(),
        role_timeout_seconds={
            role: 100 + index for index, role in enumerate(AgentRole)
        },
        iteration_limit=1 if team_id == "single_agent" else 2,
        max_concurrency=max_concurrency,
        created_at=FIXED_TIME,
    )


def adaptive_payload(team_id: str = "function_specialized") -> dict[str, object]:
    """Convert a fixed fixture into mutable adaptive-plan input for validation."""

    source = compiled_plan(
        team_id,
        max_concurrency=1 if team_id == "single_agent" else 2,
    ).model_dump(mode="json")
    source.update(
        plan_id="adaptive-contract-001-team-v1",
        team_id="adaptive_team",
        origin=TeamPlanOrigin.ADAPTIVE_PLANNING.value,
        approval_source=PlanApprovalSource.USER.value,
        implementation_plan_sha256="e" * 64,
        source_manifest_version=None,
        source_team_id=None,
        source_team_sha256=None,
    )
    for agent in source["agents"]:
        agent["legacy_role"] = None
    source["agents"] = [
        agent for agent in source["agents"] if agent["capability"] != "planning"
    ]
    remaining_ids = {agent["id"] for agent in source["agents"]}
    for agent in source["agents"]:
        agent["dependencies"] = [
            dependency
            for dependency in agent["dependencies"]
            if dependency in remaining_ids
        ]
    return source


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


def test_fixed_fixture_compiles_to_a_complete_run_scoped_plan() -> None:
    plan = compiled_plan()

    assert plan.origin is TeamPlanOrigin.FIXED_MANIFEST
    assert plan.approval_source is PlanApprovalSource.COMPATIBILITY_POLICY
    assert plan.implementation_plan_sha256 is None
    assert plan.model_routes.mode is ModelRoutingMode.STRICT
    assert plan.model_routes.routes[0].model == "test/provider-model"
    assert plan.execution_waves() == (
        ("planner",),
        ("generalist_developer",),
        ("tester", "reviewer"),
    )
    assert plan.max_concurrency == 2
    assert plan.get_agent("planner").permission_profile is PermissionProfile.READ_ONLY
    assert (
        plan.get_agent("generalist_developer").permission_profile
        is PermissionProfile.WORKSPACE_WRITE
    )
    assert plan.timeout_for_role(AgentRole.PLANNER) == 102
    validate_fixed_team_plan(plan, load_team_manifest(TEAM_CONFIG))


def test_domain_fixture_preserves_parallel_ownership_and_integration() -> None:
    plan = compiled_plan("implementation_domain_specialized")

    assert plan.execution_waves() == (
        ("planner",),
        ("frontend_developer", "backend_developer"),
        ("integrator",),
        ("tester", "reviewer"),
    )
    assert (
        plan.get_agent("frontend_developer").workspace_scope
        != plan.get_agent("backend_developer").workspace_scope
    )
    assert plan.get_agent("integrator").dependencies == (
        "frontend_developer",
        "backend_developer",
    )


def test_baseline_fixture_compiles_without_inventing_quality_agents() -> None:
    plan = compiled_plan("single_agent", max_concurrency=1)

    assert plan.execution_waves() == (("single_agent",),)
    assert not plan.independent_review
    assert not plan.revision_enabled
    assert plan.iteration_limit == 1


def test_fixed_compiler_rejects_missing_timeout_authority() -> None:
    manifest = load_team_manifest(TEAM_CONFIG)

    with pytest.raises(ValueError, match="missing roles: reviewer"):
        compile_fixed_team_plan(
            manifest,
            team_id="function_specialized",
            run_id="adaptive-contract-001",
            task_brief_sha256="d" * 64,
            model="test/provider-model",
            budget=budget(),
            role_timeout_seconds={
                role: 300 for role in AgentRole if role is not AgentRole.REVIEWER
            },
            iteration_limit=2,
            max_concurrency=2,
            created_at=FIXED_TIME,
        )


def test_adaptive_plan_accepts_user_approved_run_scoped_agents() -> None:
    plan = TeamPlan.model_validate(adaptive_payload())

    assert plan.origin is TeamPlanOrigin.ADAPTIVE_PLANNING
    assert plan.approval_source is PlanApprovalSource.USER
    assert plan.source_manifest_version is None
    assert plan.execution_waves()[-1] == ("tester", "reviewer")


def test_adaptive_plan_budget_covers_every_planned_iteration_invocation() -> None:
    payload = adaptive_payload()
    payload["budget"]["max_calls"] = 4

    with pytest.raises(ValidationError, match="planned Agent invocations"):
        TeamPlan.model_validate(payload)


def test_adaptive_plan_requires_user_approval_and_run_scoped_identities() -> None:
    payload = adaptive_payload()
    payload["approval_source"] = PlanApprovalSource.COMPATIBILITY_POLICY.value

    with pytest.raises(ValidationError, match="require user approval"):
        TeamPlan.model_validate(payload)

    payload = adaptive_payload()
    payload["agents"][0]["legacy_role"] = AgentRole.PLANNER.value

    with pytest.raises(ValidationError, match="fixed-fixture roles"):
        TeamPlan.model_validate(payload)

    payload = adaptive_payload()
    payload["implementation_plan_sha256"] = None

    with pytest.raises(ValidationError, match="approved implementation plan"):
        TeamPlan.model_validate(payload)


@pytest.mark.parametrize(
    ("agent_id", "dependencies", "message"),
    [
        ("generalist_developer", ["reviewer"], "acyclic"),
        ("generalist_developer", ["missing_agent"], "unknown dependencies"),
    ],
)
def test_adaptive_plan_rejects_invalid_dependencies(
    agent_id: str,
    dependencies: list[str],
    message: str,
) -> None:
    payload = adaptive_payload()
    agent = next(item for item in payload["agents"] if item["id"] == agent_id)
    agent["dependencies"] = dependencies

    with pytest.raises(ValidationError, match=message):
        TeamPlan.model_validate(payload)


def test_adaptive_plan_rejects_parallel_writers_with_the_same_scope() -> None:
    payload = adaptive_payload("implementation_domain_specialized")
    frontend = next(
        item for item in payload["agents"] if item["id"] == "frontend_developer"
    )
    backend = next(
        item for item in payload["agents"] if item["id"] == "backend_developer"
    )
    backend["workspace_scope"] = frontend["workspace_scope"]

    with pytest.raises(ValidationError, match="overlapping workspace access"):
        TeamPlan.model_validate(payload)


def test_adaptive_plan_rejects_nested_parallel_write_scopes() -> None:
    payload = adaptive_payload("implementation_domain_specialized")
    frontend = next(
        item for item in payload["agents"] if item["id"] == "frontend_developer"
    )
    backend = next(
        item for item in payload["agents"] if item["id"] == "backend_developer"
    )
    backend["workspace_scope"] = f"{frontend['workspace_scope']}/api"

    with pytest.raises(ValidationError, match="overlapping workspace access"):
        TeamPlan.model_validate(payload)


def test_adaptive_plan_orders_quality_readers_after_all_writers() -> None:
    payload = adaptive_payload()
    tester = next(item for item in payload["agents"] if item["id"] == "tester")
    tester["dependencies"] = []

    with pytest.raises(ValidationError, match="every implementation path"):
        TeamPlan.model_validate(payload)


@pytest.mark.parametrize(
    "scope",
    ["repository//frontend", "repository/./frontend", "repository/frontend/"],
)
def test_agent_workspace_scope_must_be_canonical(scope: str) -> None:
    payload = adaptive_payload()
    payload["agents"][1]["workspace_scope"] = scope

    with pytest.raises(ValidationError, match="canonical safe relative paths"):
        TeamPlan.model_validate(payload)


def test_adaptive_plan_rejects_an_unauthorized_model_route() -> None:
    payload = adaptive_payload()
    payload["agents"][1]["model_route_id"] = "unapproved"

    with pytest.raises(ValidationError, match="not authorized"):
        TeamPlan.model_validate(payload)


def test_model_routes_reject_ambiguous_duplicate_provider_models() -> None:
    payload = adaptive_payload()
    payload["model_routes"]["mode"] = "policy"
    payload["model_routes"]["routes"].append(
        {"id": "alias", "model": payload["model_routes"]["routes"][0]["model"]}
    )

    with pytest.raises(ValidationError, match="distinct provider/models"):
        TeamPlan.model_validate(payload)


def test_adaptive_plan_keeps_testing_and_review_independent() -> None:
    payload = adaptive_payload()
    reviewer = next(item for item in payload["agents"] if item["id"] == "reviewer")
    reviewer["dependencies"] = ["tester"]

    with pytest.raises(ValidationError, match="must remain independent"):
        TeamPlan.model_validate(payload)


def test_adaptive_plan_requires_quality_coverage_for_every_writer() -> None:
    payload = adaptive_payload("implementation_domain_specialized")
    tester = next(item for item in payload["agents"] if item["id"] == "tester")
    reviewer = next(item for item in payload["agents"] if item["id"] == "reviewer")
    tester["dependencies"] = ["frontend_developer"]
    reviewer["dependencies"] = ["frontend_developer"]

    with pytest.raises(ValidationError, match="every implementation path"):
        TeamPlan.model_validate(payload)


def test_adaptive_plan_cannot_split_final_commit_coverage_across_quality_agents() -> (
    None
):
    payload = adaptive_payload("implementation_domain_specialized")
    tester = next(item for item in payload["agents"] if item["id"] == "tester")
    reviewer = next(item for item in payload["agents"] if item["id"] == "reviewer")
    tester["dependencies"] = ["frontend_developer"]
    reviewer["dependencies"] = ["backend_developer"]

    with pytest.raises(ValidationError, match="every implementation path"):
        TeamPlan.model_validate(payload)


def test_adaptive_plan_accepts_one_independent_quality_agent() -> None:
    payload = adaptive_payload()
    payload["agents"] = [
        agent for agent in payload["agents"] if agent["id"] != "tester"
    ]
    payload["max_concurrency"] = 1

    plan = TeamPlan.model_validate(payload)

    assert tuple(agent.id for agent in plan.agents) == (
        "generalist_developer",
        "reviewer",
    )
    assert plan.execution_waves() == (("generalist_developer",), ("reviewer",))


def test_adaptive_plan_rejects_bootstrap_capabilities() -> None:
    payload = adaptive_payload()
    payload["agents"][0]["capability"] = "planning"
    payload["agents"][0]["expected_output"] = "implementation_plan"
    payload["agents"][0]["permission_profile"] = "read_only"

    with pytest.raises(ValidationError, match="bootstrap capabilities"):
        TeamPlan.model_validate(payload)


def test_adaptive_plan_cannot_disable_independent_quality() -> None:
    payload = adaptive_payload()
    payload["independent_review"] = False

    with pytest.raises(ValidationError, match="independent quality control"):
        TeamPlan.model_validate(payload)
