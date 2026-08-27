"""Secret-free model profiles and deterministic plan-time route resolution."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from software_agent_team.teams import (
    AgentCapability,
    ModelRoute,
    ModelRouteAssignment,
    ModelRoutePlan,
    ModelRouteSelectionSource,
    ModelRoutingMode,
    ModelSwitchCondition,
)


class ModelRoutingError(ValueError):
    """Raised when authorized profiles cannot satisfy an approved Agent plan."""


class ModelProfile(BaseModel):
    """One secret-free provider/model option approved for specific capabilities."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    model: str = Field(min_length=3)
    capabilities: tuple[AgentCapability, ...] = Field(min_length=1)
    priority: int = Field(default=100, ge=1, le=1000)
    input_cost_per_million_usd: Decimal | None = Field(
        default=None,
        ge=0,
        le=10_000,
    )
    output_cost_per_million_usd: Decimal | None = Field(
        default=None,
        ge=0,
        le=10_000,
    )

    @field_validator("model")
    @classmethod
    def require_canonical_model(cls, value: str) -> str:
        """Require one explicit OpenClaw provider/model reference."""

        cleaned = value.strip()
        provider, separator, model = cleaned.partition("/")
        if (
            not separator
            or not provider
            or not model
            or any(character.isspace() for character in cleaned)
        ):
            raise ValueError("model profiles require a canonical provider/model")
        return cleaned

    @field_validator("capabilities")
    @classmethod
    def require_unique_capabilities(
        cls,
        values: tuple[AgentCapability, ...],
    ) -> tuple[AgentCapability, ...]:
        if len(values) != len(set(values)):
            raise ValueError("model profile capabilities must be unique")
        return values

    @model_validator(mode="after")
    def require_complete_price_pair(self) -> Self:
        if (self.input_cost_per_million_usd is None) != (
            self.output_cost_per_million_usd is None
        ):
            raise ValueError(
                "model profile input and output prices must be configured together"
            )
        return self

    def supports(self, capability: AgentCapability) -> bool:
        """Return whether this profile is user-authorized for one SAT capability."""

        return capability in self.capabilities


class ModelRoutingPolicy(BaseModel):
    """Controller input for strict pinning or deterministic adaptive selection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: ModelRoutingMode = ModelRoutingMode.STRICT
    profiles: tuple[ModelProfile, ...] = Field(min_length=1, max_length=16)
    default_profile_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    capability_profile_overrides: dict[AgentCapability, str] = Field(
        default_factory=dict
    )
    stage_profile_overrides: dict[str, str] = Field(default_factory=dict)
    authorized_switch_conditions: tuple[ModelSwitchCondition, ...] = ()
    max_switches_per_agent: int = Field(default=0, ge=0, le=3)

    @field_validator("stage_profile_overrides")
    @classmethod
    def require_safe_stage_overrides(cls, values: dict[str, str]) -> dict[str, str]:
        for stage_id, profile_id in values.items():
            if re.fullmatch(r"[a-z][a-z0-9_]*", stage_id) is None:
                raise ValueError("model stage overrides require safe stage IDs")
            if re.fullmatch(r"[a-z][a-z0-9_]*", profile_id) is None:
                raise ValueError("model stage overrides require safe profile IDs")
        return values

    @model_validator(mode="after")
    def validate_policy(self) -> Self:
        profile_ids = [profile.id for profile in self.profiles]
        if len(profile_ids) != len(set(profile_ids)):
            raise ValueError("model profile IDs must be unique")
        models = [profile.model for profile in self.profiles]
        if len(models) != len(set(models)):
            raise ValueError("model profiles must identify distinct models")
        if self.default_profile_id not in profile_ids:
            raise ValueError("default model profile is not configured")
        known = set(profile_ids)
        referenced = set(self.capability_profile_overrides.values()) | set(
            self.stage_profile_overrides.values()
        )
        if unknown := referenced - known:
            raise ValueError(
                "model routing overrides reference unknown profiles: "
                + ", ".join(sorted(unknown))
            )
        for capability, profile_id in self.capability_profile_overrides.items():
            if not self.get_profile(profile_id).supports(capability):
                raise ValueError(
                    f"model profile {profile_id} is not authorized for "
                    f"{capability.value}"
                )
        default = self.get_profile(self.default_profile_id)
        bootstrap = {AgentCapability.CLARIFICATION, AgentCapability.PLANNING}
        if not bootstrap.issubset(default.capabilities):
            raise ValueError(
                "default model profile must support clarification and planning"
            )
        if self.mode is ModelRoutingMode.STRICT:
            if (
                len(self.profiles) != 1
                or self.capability_profile_overrides
                or self.stage_profile_overrides
                or self.authorized_switch_conditions
                or self.max_switches_per_agent
            ):
                raise ValueError(
                    "strict model routing requires one profile and no overrides "
                    "or switches"
                )
        elif bool(self.authorized_switch_conditions) != bool(
            self.max_switches_per_agent
        ):
            raise ValueError(
                "model switch conditions and a positive switch limit belong together"
            )
        return self

    def get_profile(self, profile_id: str) -> ModelProfile:
        """Return one configured profile or reject an unknown reference."""

        for profile in self.profiles:
            if profile.id == profile_id:
                return profile
        raise ValueError(f"unknown model profile: {profile_id}")


class RoutableAgent(Protocol):
    """Minimum approved-planning fields required for route resolution."""

    id: str
    stage_id: str
    capability: AgentCapability


def _profile_order(policy: ModelRoutingPolicy) -> dict[str, int]:
    return {profile.id: index for index, profile in enumerate(policy.profiles)}


def _eligible_profiles(
    policy: ModelRoutingPolicy,
    capability: AgentCapability,
) -> tuple[ModelProfile, ...]:
    order = _profile_order(policy)
    return tuple(
        sorted(
            (profile for profile in policy.profiles if profile.supports(capability)),
            key=lambda profile: (profile.priority, order[profile.id]),
        )
    )


def resolve_model_route_plan(
    policy: ModelRoutingPolicy,
    agents: Sequence[RoutableAgent],
    *,
    agent_profile_overrides: Mapping[str, str] | None = None,
) -> ModelRoutePlan:
    """Resolve exact per-Agent routes using explicit deterministic precedence."""

    if not agents:
        raise ModelRoutingError("model routing requires at least one runtime Agent")
    overrides = dict(agent_profile_overrides or {})
    known_agents = {agent.id for agent in agents}
    if unknown := set(overrides) - known_agents:
        raise ModelRoutingError(
            "model overrides reference unknown Agents: " + ", ".join(sorted(unknown))
        )
    default = policy.get_profile(policy.default_profile_id)
    assignments: list[ModelRouteAssignment] = []

    for agent in agents:
        eligible = _eligible_profiles(policy, agent.capability)
        if not eligible:
            raise ModelRoutingError(
                f"no authorized model profile supports {agent.capability.value} "
                f"for Agent {agent.id}"
            )

        if policy.mode is ModelRoutingMode.STRICT:
            selected = default
            source = ModelRouteSelectionSource.STRICT_PIN
            reason = "Strict routing pins every Agent to the approved default profile."
        elif agent.id in overrides:
            selected = policy.get_profile(overrides[agent.id])
            source = ModelRouteSelectionSource.AGENT_OVERRIDE
            reason = f"The user selected profile {selected.id} for this Agent."
        elif agent.stage_id in policy.stage_profile_overrides:
            selected = policy.get_profile(
                policy.stage_profile_overrides[agent.stage_id]
            )
            source = ModelRouteSelectionSource.STAGE_OVERRIDE
            reason = (
                f"Stage {agent.stage_id} is configured to use profile {selected.id}."
            )
        elif agent.capability in policy.capability_profile_overrides:
            selected = policy.get_profile(
                policy.capability_profile_overrides[agent.capability]
            )
            source = ModelRouteSelectionSource.CAPABILITY_OVERRIDE
            reason = (
                f"Capability {agent.capability.value} is configured to use "
                f"profile {selected.id}."
            )
        elif default.supports(agent.capability):
            selected = default
            source = ModelRouteSelectionSource.DEFAULT_PROFILE
            reason = "The default profile supports this Agent capability."
        else:
            selected = eligible[0]
            source = ModelRouteSelectionSource.AUTO_CAPABILITY
            reason = (
                "The controller selected the highest-priority profile authorized "
                f"for {agent.capability.value}."
            )

        if not selected.supports(agent.capability):
            raise ModelRoutingError(
                f"model profile {selected.id} is not authorized for Agent "
                f"{agent.id} ({agent.capability.value})"
            )
        fallback_ids: tuple[str, ...] = ()
        if policy.authorized_switch_conditions:
            fallback_ids = tuple(
                profile.id for profile in eligible if profile.id != selected.id
            )[: policy.max_switches_per_agent]
        assignments.append(
            ModelRouteAssignment(
                agent_id=agent.id,
                primary_route_id=selected.id,
                fallback_route_ids=fallback_ids,
                selection_source=source,
                reason=reason,
            )
        )

    used_routes = {
        policy.default_profile_id,
        *(
            route_id
            for assignment in assignments
            for route_id in (
                assignment.primary_route_id,
                *assignment.fallback_route_ids,
            )
        ),
    }
    routes = tuple(
        ModelRoute(
            id=profile.id,
            model=profile.model,
            eligible_capabilities=profile.capabilities,
            input_cost_per_million_usd=profile.input_cost_per_million_usd,
            output_cost_per_million_usd=profile.output_cost_per_million_usd,
        )
        for profile in policy.profiles
        if profile.id in used_routes
    )
    return ModelRoutePlan(
        mode=policy.mode,
        default_route_id=policy.default_profile_id,
        routes=routes,
        assignments=tuple(assignments),
        authorized_switch_conditions=policy.authorized_switch_conditions,
    )
