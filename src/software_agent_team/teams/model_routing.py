"""Model routing strategies, route authorization, and route plans for teams.

团队模型路由策略: 包含模型候选、降级路由 (Fallback)、选择来源及切换条件。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from software_agent_team.model_costs import CachePriceSupport
from software_agent_team.model_metadata import ModelMetadataSource
from software_agent_team.model_runtime import (
    ModelRuntimeProfile,
    runtime_profile_for_model,
)

from .common import _clean_unique_text, _require_utc
from .types import AgentCapability


class ModelRoutingMode(StrEnum):
    """Whether a run pins one route or permits authorized policy resolution.

    模型路由模式: 严格固定单模型或基于策略授权解析。
    """

    STRICT = "strict"
    POLICY = "policy"


class ModelSwitchCondition(StrEnum):
    """User-authorized reason for advancing to an approved fallback route.

    模型自动故障切换条件: 如提供商故障。
    """

    PROVIDER_FAILURE = "provider_failure"


class ModelRouteSelectionSource(StrEnum):
    """Attributable precedence rule that selected one Agent's primary route.

    模型路由选路决策来源与优先级规则归因。
    """

    STRICT_PIN = "strict_pin"
    AGENT_OVERRIDE = "agent_override"
    STAGE_OVERRIDE = "stage_override"
    CAPABILITY_OVERRIDE = "capability_override"
    DEFAULT_PROFILE = "default_profile"
    AUTO_CAPABILITY = "auto_capability"


class ModelRoute(CachePriceSupport):
    """One explicitly authorized provider/model route.

    经过显式授权的单条提供商/模型路由配置。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    model: str = Field(min_length=3)
    runtime_profile: ModelRuntimeProfile
    runtime_profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    required_capabilities: tuple[str, ...] = ()
    eligible_capabilities: tuple[AgentCapability, ...] = tuple(AgentCapability)
    input_cost_per_million_usd: Decimal | None = Field(
        default=None,
        ge=0,
    )
    output_cost_per_million_usd: Decimal | None = Field(
        default=None,
        ge=0,
    )
    pricing_source: ModelMetadataSource | None = None
    pricing_observed_at: datetime | None = None
    context_window_tokens: int | None = Field(
        default=None,
        ge=1,
    )
    context_source: ModelMetadataSource | None = None
    context_observed_at: datetime | None = None

    @model_validator(mode="before")
    @classmethod
    def infer_explicit_metadata_sources(cls, value: object) -> object:
        """Attribute legacy explicit metadata without claiming discovery."""
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        if payload.get("runtime_profile") is None and isinstance(
            payload.get("model"), str
        ):
            runtime_profile = runtime_profile_for_model(payload["model"])
            payload["runtime_profile"] = runtime_profile.model_dump(mode="json")
            payload.setdefault("runtime_profile_sha256", runtime_profile.sha256)
        elif isinstance(payload.get("runtime_profile"), dict):
            runtime_profile = ModelRuntimeProfile.model_validate(
                payload["runtime_profile"]
            )
            payload.setdefault("runtime_profile_sha256", runtime_profile.sha256)
        if (
            payload.get("input_cost_per_million_usd") is not None
            and payload.get("output_cost_per_million_usd") is not None
            and payload.get("pricing_source") is None
        ):
            payload["pricing_source"] = ModelMetadataSource.USER_SUPPLIED.value
        if (
            payload.get("context_window_tokens") is not None
            and payload.get("context_source") is None
        ):
            payload["context_source"] = ModelMetadataSource.USER_SUPPLIED.value
        return payload

    @field_validator("model")
    @classmethod
    def require_canonical_model(cls, value: str) -> str:
        """Require an explicit provider/model identity without whitespace."""
        cleaned = value.strip()
        provider, separator, model = cleaned.partition("/")
        if (
            not separator
            or not provider
            or not model
            or any(character.isspace() for character in cleaned)
        ):
            raise ValueError("model routes require a canonical provider/model")
        return cleaned

    @field_validator("required_capabilities")
    @classmethod
    def require_unique_capabilities(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _clean_unique_text(values, label="model capability")

    @field_validator("eligible_capabilities")
    @classmethod
    def require_unique_eligible_capabilities(
        cls,
        values: tuple[AgentCapability, ...],
    ) -> tuple[AgentCapability, ...]:
        if not values or len(values) != len(set(values)):
            raise ValueError("eligible model capabilities must be non-empty and unique")
        return values

    @model_validator(mode="after")
    def require_complete_price_pair(self) -> Self:
        if (self.input_cost_per_million_usd is None) != (
            self.output_cost_per_million_usd is None
        ):
            raise ValueError("model route input and output prices belong together")
        prices_known = self.input_cost_per_million_usd is not None
        if prices_known != (self.pricing_source is not None):
            raise ValueError("known model route prices require one source")
        context_known = self.context_window_tokens is not None
        if context_known != (self.context_source is not None):
            raise ValueError("known model route context requires one source")
        if self.pricing_source is ModelMetadataSource.CONFIRMED_ZERO and (
            self.input_cost_per_million_usd != 0
            or self.output_cost_per_million_usd != 0
        ):
            raise ValueError("confirmed-zero route pricing requires two zero prices")
        provider_id, _, _ = self.model.partition("/")
        if self.runtime_profile.provider_id != provider_id:
            raise ValueError("model route runtime provider must match its model")
        if self.runtime_profile_sha256 != self.runtime_profile.sha256:
            raise ValueError("model route runtime profile digest does not match")
        return self

    @field_validator("pricing_observed_at", "context_observed_at")
    @classmethod
    def require_aware_metadata_time(
        cls,
        value: datetime | None,
    ) -> datetime | None:
        if value is None:
            return None
        return _require_utc(value)


class ModelRouteAssignment(BaseModel):
    """Exact approved primary and fallback routes for one runtime Agent.

    单个运行时 Agent 批准的主选及备选模型路由绑定。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    primary_route_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    fallback_route_ids: tuple[str, ...] = ()
    selection_source: ModelRouteSelectionSource
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("fallback_route_ids")
    @classmethod
    def require_unique_fallbacks(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _clean_unique_text(values, label="fallback model route")

    @field_validator("reason")
    @classmethod
    def require_clean_reason(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("model route selection reason must not be blank")
        return cleaned

    @model_validator(mode="after")
    def reject_primary_as_fallback(self) -> Self:
        if self.primary_route_id in self.fallback_route_ids:
            raise ValueError("primary model route cannot also be a fallback")
        return self


class ModelRoutePlan(BaseModel):
    """Approved model candidates and switching policy for one TeamPlan.

    整个 TeamPlan 的已批准模型候选集与故障转移路由方案。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: ModelRoutingMode
    default_route_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    routes: tuple[ModelRoute, ...] = Field(min_length=1)
    assignments: tuple[ModelRouteAssignment, ...] = ()
    authorized_switch_conditions: tuple[ModelSwitchCondition, ...] = ()

    @field_validator("authorized_switch_conditions")
    @classmethod
    def require_unique_switch_conditions(
        cls,
        values: tuple[ModelSwitchCondition, ...],
    ) -> tuple[ModelSwitchCondition, ...]:
        if len(values) != len(set(values)):
            raise ValueError("model switch conditions must be unique")
        return values

    @model_validator(mode="after")
    def validate_routes(self) -> Self:
        route_ids = [route.id for route in self.routes]
        if len(route_ids) != len(set(route_ids)):
            raise ValueError("model route IDs must be unique")
        route_models = [route.model for route in self.routes]
        if len(route_models) != len(set(route_models)):
            raise ValueError("model routes must identify distinct provider/models")
        if self.default_route_id not in route_ids:
            raise ValueError("default model route must reference an authorized route")
        assignment_agents = [assignment.agent_id for assignment in self.assignments]
        if len(assignment_agents) != len(set(assignment_agents)):
            raise ValueError("model route assignments must identify unique Agents")
        known_routes = set(route_ids)
        for assignment in self.assignments:
            referenced = {
                assignment.primary_route_id,
                *assignment.fallback_route_ids,
            }
            if unknown := referenced - known_routes:
                raise ValueError(
                    f"Agent {assignment.agent_id} references unknown model routes: "
                    + ", ".join(sorted(unknown))
                )
        if self.mode is ModelRoutingMode.STRICT and (
            len(self.routes) != 1 or self.authorized_switch_conditions
        ):
            raise ValueError("strict model routing requires one route and no switches")
        if self.mode is ModelRoutingMode.STRICT and any(
            assignment.fallback_route_ids for assignment in self.assignments
        ):
            raise ValueError("strict model routing cannot assign fallback routes")
        if any(assignment.fallback_route_ids for assignment in self.assignments) and (
            not self.authorized_switch_conditions
        ):
            raise ValueError("fallback routes require an authorized switch condition")
        return self

    def get_route(self, route_id: str) -> ModelRoute:
        """Return one authorized route or reject an unknown reference."""
        for route in self.routes:
            if route.id == route_id:
                return route
        raise ValueError(f"unknown model route: {route_id}")

    def get_assignment(self, agent_id: str) -> ModelRouteAssignment:
        """Return one approved Agent assignment or reject missing route evidence."""
        for assignment in self.assignments:
            if assignment.agent_id == agent_id:
                return assignment
        raise ValueError(f"missing model route assignment for Agent: {agent_id}")

    def authorized_route_ids(self, agent_id: str) -> tuple[str, ...]:
        """Return the exact ordered routes one Agent may use during this run."""
        if not self.assignments and self.mode is ModelRoutingMode.STRICT:
            return (self.default_route_id,)
        assignment = self.get_assignment(agent_id)
        return (assignment.primary_route_id, *assignment.fallback_route_ids)
