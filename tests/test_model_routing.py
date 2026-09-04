"""Tests for secret-free profiles and deterministic model-route resolution."""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from software_agent_team.model_metadata import ModelMetadataSource
from software_agent_team.model_routing import (
    ModelProfile,
    ModelRoutingError,
    ModelRoutingPolicy,
    resolve_model_route_plan,
)
from software_agent_team.teams import (
    AgentCapability,
    ModelRouteSelectionSource,
    ModelRoutingMode,
    ModelSwitchCondition,
)


def agent(
    agent_id: str,
    capability: AgentCapability,
    *,
    stage_id: str,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=agent_id,
        capability=capability,
        stage_id=stage_id,
    )


def profile(
    profile_id: str,
    model: str,
    capabilities: tuple[AgentCapability, ...],
    *,
    priority: int = 100,
) -> ModelProfile:
    return ModelProfile(
        id=profile_id,
        model=model,
        capabilities=capabilities,
        priority=priority,
    )


BOOTSTRAP = (AgentCapability.CLARIFICATION, AgentCapability.PLANNING)


def test_strict_routing_pins_every_agent_to_one_profile() -> None:
    policy = ModelRoutingPolicy(
        mode=ModelRoutingMode.STRICT,
        profiles=(
            profile(
                "pinned",
                "provider/pinned",
                (*BOOTSTRAP, *tuple(AgentCapability)[2:]),
            ),
        ),
        default_profile_id="pinned",
    )

    plan = resolve_model_route_plan(
        policy,
        (
            agent("developer", AgentCapability.IMPLEMENTATION, stage_id="build"),
            agent("reviewer", AgentCapability.REVIEW, stage_id="verify"),
        ),
    )

    assert plan.mode is ModelRoutingMode.STRICT
    assert tuple(route.id for route in plan.routes) == ("pinned",)
    assert {
        assignment.agent_id: assignment.selection_source
        for assignment in plan.assignments
    } == {
        "developer": ModelRouteSelectionSource.STRICT_PIN,
        "reviewer": ModelRouteSelectionSource.STRICT_PIN,
    }


def test_policy_routing_uses_agent_stage_capability_then_default_precedence() -> None:
    default = profile(
        "balanced",
        "provider/balanced",
        (*BOOTSTRAP, AgentCapability.IMPLEMENTATION, AgentCapability.REVIEW),
    )
    coding = profile(
        "coding",
        "provider/coding",
        (AgentCapability.IMPLEMENTATION, AgentCapability.INTEGRATION),
    )
    quality = profile(
        "quality",
        "provider/quality",
        (AgentCapability.TESTING, AgentCapability.REVIEW),
    )
    alternate = profile(
        "alternate",
        "provider/alternate",
        (AgentCapability.REVIEW,),
    )
    policy = ModelRoutingPolicy(
        mode=ModelRoutingMode.POLICY,
        profiles=(default, coding, quality, alternate),
        default_profile_id="balanced",
        capability_profile_overrides={AgentCapability.IMPLEMENTATION: "coding"},
        stage_profile_overrides={"verify": "quality"},
    )

    plan = resolve_model_route_plan(
        policy,
        (
            agent("developer", AgentCapability.IMPLEMENTATION, stage_id="build"),
            agent("tester", AgentCapability.TESTING, stage_id="verify"),
            agent("reviewer", AgentCapability.REVIEW, stage_id="review"),
        ),
        agent_profile_overrides={"reviewer": "alternate"},
    )

    assignments = {item.agent_id: item for item in plan.assignments}
    assert assignments["developer"].primary_route_id == "coding"
    assert (
        assignments["developer"].selection_source
        is ModelRouteSelectionSource.CAPABILITY_OVERRIDE
    )
    assert assignments["tester"].primary_route_id == "quality"
    assert (
        assignments["tester"].selection_source
        is ModelRouteSelectionSource.STAGE_OVERRIDE
    )
    assert assignments["reviewer"].primary_route_id == "alternate"
    assert (
        assignments["reviewer"].selection_source
        is ModelRouteSelectionSource.AGENT_OVERRIDE
    )


def test_policy_auto_selects_highest_priority_eligible_profile() -> None:
    policy = ModelRoutingPolicy(
        mode=ModelRoutingMode.POLICY,
        profiles=(
            profile("bootstrap", "provider/bootstrap", BOOTSTRAP),
            profile(
                "slow_quality",
                "provider/slow-quality",
                (AgentCapability.TESTING,),
                priority=200,
            ),
            profile(
                "fast_quality",
                "provider/fast-quality",
                (AgentCapability.TESTING,),
                priority=10,
            ),
        ),
        default_profile_id="bootstrap",
    )

    plan = resolve_model_route_plan(
        policy,
        (agent("tester", AgentCapability.TESTING, stage_id="verify"),),
    )

    assignment = plan.get_assignment("tester")
    assert assignment.primary_route_id == "fast_quality"
    assert assignment.selection_source is ModelRouteSelectionSource.AUTO_CAPABILITY
    assert "highest-priority" in assignment.reason


def test_policy_rejects_capability_mismatch_and_unknown_agent_override() -> None:
    policy = ModelRoutingPolicy(
        mode=ModelRoutingMode.POLICY,
        profiles=(profile("bootstrap", "provider/bootstrap", BOOTSTRAP),),
        default_profile_id="bootstrap",
    )
    runtime_agent = agent(
        "developer",
        AgentCapability.IMPLEMENTATION,
        stage_id="build",
    )

    with pytest.raises(ModelRoutingError, match="no authorized model profile"):
        resolve_model_route_plan(policy, (runtime_agent,))
    with pytest.raises(ModelRoutingError, match="unknown Agents"):
        resolve_model_route_plan(
            policy,
            (runtime_agent,),
            agent_profile_overrides={"missing": "bootstrap"},
        )


def test_policy_freezes_the_finite_eligible_provider_fallback_chain() -> None:
    policy = ModelRoutingPolicy(
        mode=ModelRoutingMode.POLICY,
        profiles=(
            profile(
                "primary",
                "provider/primary",
                (*BOOTSTRAP, AgentCapability.IMPLEMENTATION),
                priority=1,
            ),
            profile(
                "fallback",
                "provider/fallback",
                (AgentCapability.IMPLEMENTATION,),
                priority=2,
            ),
            profile(
                "unused",
                "provider/unused",
                (AgentCapability.IMPLEMENTATION,),
                priority=3,
            ),
        ),
        default_profile_id="primary",
        authorized_switch_conditions=(ModelSwitchCondition.PROVIDER_FAILURE,),
    )

    plan = resolve_model_route_plan(
        policy,
        (agent("developer", AgentCapability.IMPLEMENTATION, stage_id="build"),),
    )

    assert plan.get_assignment("developer").fallback_route_ids == (
        "fallback",
        "unused",
    )
    assert tuple(route.id for route in plan.routes) == (
        "primary",
        "fallback",
        "unused",
    )


def test_policy_does_not_impose_legacy_profile_or_metadata_ceilings() -> None:
    profiles = tuple(
        ModelProfile(
            id=f"route_{index}",
            model=f"provider/model-{index}",
            capabilities=(*BOOTSTRAP, AgentCapability.IMPLEMENTATION),
            priority=1000 + index,
            input_cost_per_million_usd=10_001,
            output_cost_per_million_usd=20_001,
            pricing_source=ModelMetadataSource.USER_SUPPLIED,
            context_window_tokens=100_000_001,
            context_source=ModelMetadataSource.USER_SUPPLIED,
        )
        for index in range(17)
    )

    policy = ModelRoutingPolicy(
        mode=ModelRoutingMode.POLICY,
        profiles=profiles,
        default_profile_id="route_0",
    )

    assert len(policy.profiles) == 17
    assert policy.profiles[-1].priority == 1016
    assert policy.profiles[-1].context_window_tokens == 100_000_001


def test_strict_policy_rejects_hidden_profiles_or_switches() -> None:
    primary = profile(
        "primary",
        "provider/primary",
        (*BOOTSTRAP, AgentCapability.IMPLEMENTATION),
    )
    fallback = profile(
        "fallback",
        "provider/fallback",
        (AgentCapability.IMPLEMENTATION,),
    )

    with pytest.raises(ValidationError, match="strict model routing"):
        ModelRoutingPolicy(
            mode=ModelRoutingMode.STRICT,
            profiles=(primary, fallback),
            default_profile_id="primary",
        )


def test_policy_rejects_stage_override_ids_that_cannot_match_planned_stages() -> None:
    primary = profile(
        "primary",
        "provider/primary",
        (*BOOTSTRAP, AgentCapability.IMPLEMENTATION),
    )

    with pytest.raises(ValidationError, match="safe stage IDs"):
        ModelRoutingPolicy(
            mode=ModelRoutingMode.POLICY,
            profiles=(primary,),
            default_profile_id="primary",
            stage_profile_overrides={"Build": "primary"},
        )
