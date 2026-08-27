"""Versioned team fixtures, run-scoped plans, validation, and compilation."""

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from software_agent_team.artifacts import AgentRole, ArtifactKind
from software_agent_team.budgets import AgentBudget
from software_agent_team.integrity import canonical_model_sha256

TEAM_PLAN_SCHEMA_VERSION = 1


class TeamKind(StrEnum):
    """Experimental category for a team definition."""

    BASELINE = "baseline"
    MULTI_AGENT = "multi_agent"


class StageMode(StrEnum):
    """Execution relationship among roles in one stage."""

    SEQUENTIAL = "sequential"
    PARALLEL = "parallel"


class TeamPlanOrigin(StrEnum):
    """Authority that produced one approved run-scoped team plan."""

    FIXED_MANIFEST = "fixed_manifest"
    ADAPTIVE_PLANNING = "adaptive_planning"


class PlanApprovalSource(StrEnum):
    """Actor that authorized one immutable plan revision."""

    COMPATIBILITY_POLICY = "compatibility_policy"
    USER = "user"


class AgentCapability(StrEnum):
    """Controller-known work contract independent from a user-facing label."""

    CLARIFICATION = "clarification"
    PLANNING = "planning"
    IMPLEMENTATION = "implementation"
    INTEGRATION = "integration"
    TESTING = "testing"
    REVIEW = "review"


class PermissionProfile(StrEnum):
    """Versioned least-privilege profiles assignable to an AgentSpec."""

    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"


class ModelRoutingMode(StrEnum):
    """Whether a run pins one route or permits authorized policy resolution."""

    STRICT = "strict"
    POLICY = "policy"


class ModelSwitchCondition(StrEnum):
    """User-authorized reason for advancing to an approved fallback route."""

    PROVIDER_FAILURE = "provider_failure"


class ModelRouteSelectionSource(StrEnum):
    """Attributable precedence rule that selected one Agent's primary route."""

    STRICT_PIN = "strict_pin"
    AGENT_OVERRIDE = "agent_override"
    STAGE_OVERRIDE = "stage_override"
    CAPABILITY_OVERRIDE = "capability_override"
    DEFAULT_PROFILE = "default_profile"
    AUTO_CAPABILITY = "auto_capability"


_CAPABILITY_OUTPUTS = {
    AgentCapability.CLARIFICATION: ArtifactKind.CLARIFICATION_RECORD,
    AgentCapability.PLANNING: ArtifactKind.IMPLEMENTATION_PLAN,
    AgentCapability.IMPLEMENTATION: ArtifactKind.WORK_RESULT,
    AgentCapability.INTEGRATION: ArtifactKind.WORK_RESULT,
    AgentCapability.TESTING: ArtifactKind.TEST_REPORT,
    AgentCapability.REVIEW: ArtifactKind.REVIEW_REPORT,
}
_READ_ONLY_CAPABILITIES = {
    AgentCapability.CLARIFICATION,
    AgentCapability.PLANNING,
    AgentCapability.TESTING,
    AgentCapability.REVIEW,
}


def expected_output_for_capability(capability: AgentCapability) -> ArtifactKind:
    """Return the controller-owned output contract for one capability."""

    return _CAPABILITY_OUTPUTS[capability]


def permission_for_capability(capability: AgentCapability) -> PermissionProfile:
    """Resolve the least-privilege profile assigned to one capability."""

    if capability in _READ_ONLY_CAPABILITIES:
        return PermissionProfile.READ_ONLY
    return PermissionProfile.WORKSPACE_WRITE


def _clean_unique_text(values: tuple[str, ...], *, label: str) -> tuple[str, ...]:
    cleaned = tuple(value.strip() for value in values)
    if any(not value for value in cleaned):
        raise ValueError(f"{label} entries must not be blank")
    if len(cleaned) != len(set(cleaned)):
        raise ValueError(f"{label} entries must be unique")
    return cleaned


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("team-plan timestamps must include a timezone")
    return value.astimezone(UTC)


class ModelRoute(BaseModel):
    """One explicitly authorized provider/model route."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    model: str = Field(min_length=3)
    required_capabilities: tuple[str, ...] = ()
    eligible_capabilities: tuple[AgentCapability, ...] = tuple(AgentCapability)
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
        return self


class ModelRouteAssignment(BaseModel):
    """Exact approved primary and fallback routes for one runtime Agent."""

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
    """Approved model candidates and switching policy for one TeamPlan."""

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


class AgentSpec(BaseModel):
    """One run-scoped responsibility proposed or compiled for the controller."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    label: str = Field(min_length=1, max_length=80)
    responsibility: str = Field(min_length=1, max_length=500)
    rationale: str = Field(min_length=1, max_length=500)
    capability: AgentCapability
    permission_profile: PermissionProfile
    stage_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    dependencies: tuple[str, ...] = ()
    expected_output: ArtifactKind
    model_route_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    timeout_seconds: int = Field(ge=1, le=3600)
    workspace_scope: str = Field(min_length=1, max_length=200)
    legacy_role: AgentRole | None = None

    @field_validator("label", "responsibility", "rationale")
    @classmethod
    def require_clean_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("AgentSpec text must not be blank")
        return cleaned

    @field_validator("dependencies")
    @classmethod
    def require_unique_dependencies(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _clean_unique_text(values, label="Agent dependency")

    @field_validator("workspace_scope")
    @classmethod
    def require_safe_workspace_scope(cls, value: str) -> str:
        cleaned = value.strip()
        path = PurePosixPath(cleaned)
        if (
            not cleaned
            or "\\" in cleaned
            or path.is_absolute()
            or path == PurePosixPath(".")
            or ".." in path.parts
            or str(path) != cleaned
        ):
            raise ValueError("workspace scopes must be canonical safe relative paths")
        if path.parts[0] != "repository":
            raise ValueError("workspace scopes must start at repository or repository/")
        return cleaned

    @model_validator(mode="after")
    def validate_capability_boundary(self) -> Self:
        if self.expected_output is not _CAPABILITY_OUTPUTS[self.capability]:
            raise ValueError("Agent capability and expected output are inconsistent")
        if self.capability in _READ_ONLY_CAPABILITIES:
            if self.permission_profile is not PermissionProfile.READ_ONLY:
                raise ValueError("planning and quality capabilities must be read-only")
        elif self.permission_profile is not PermissionProfile.WORKSPACE_WRITE:
            raise ValueError("implementation capabilities require workspace write")
        if self.id in self.dependencies:
            raise ValueError("an Agent cannot depend on itself")
        return self


class TeamPlan(BaseModel):
    """Approved run-scoped authority for Agent creation and scheduling."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[TEAM_PLAN_SCHEMA_VERSION] = TEAM_PLAN_SCHEMA_VERSION
    plan_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    revision: int = Field(ge=1, le=99)
    run_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    task_brief_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    implementation_plan_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    team_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    origin: TeamPlanOrigin
    approval_source: PlanApprovalSource
    created_at: datetime
    source_manifest_version: int | None = Field(default=None, ge=1)
    source_team_id: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    source_team_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    agents: tuple[AgentSpec, ...] = Field(min_length=1, max_length=16)
    model_routes: ModelRoutePlan
    budget: AgentBudget
    iteration_limit: int = Field(ge=1, le=3)
    max_concurrency: int = Field(ge=1, le=16)
    independent_review: bool
    revision_enabled: bool

    @field_validator("created_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def validate_plan(self) -> Self:
        if self.origin is TeamPlanOrigin.FIXED_MANIFEST:
            if self.approval_source is not PlanApprovalSource.COMPATIBILITY_POLICY:
                raise ValueError("fixed plans require compatibility-policy approval")
            if self.implementation_plan_sha256 is not None:
                raise ValueError(
                    "fixed compatibility plans create their implementation "
                    "plan at runtime"
                )
            if None in {
                self.source_manifest_version,
                self.source_team_id,
                self.source_team_sha256,
            }:
                raise ValueError("fixed plans require complete manifest provenance")
            if self.source_team_id != self.team_id:
                raise ValueError("fixed plan team ID must match its source fixture")
            if any(agent.legacy_role is None for agent in self.agents):
                raise ValueError("fixed plans require a legacy role for every Agent")
        else:
            if self.approval_source is not PlanApprovalSource.USER:
                raise ValueError("adaptive plans require user approval")
            if self.implementation_plan_sha256 is None:
                raise ValueError(
                    "adaptive plans must bind an approved implementation plan"
                )
            if any(
                value is not None
                for value in (
                    self.source_manifest_version,
                    self.source_team_id,
                    self.source_team_sha256,
                )
            ):
                raise ValueError(
                    "adaptive plans cannot claim fixed-manifest provenance"
                )
            if any(agent.legacy_role is not None for agent in self.agents):
                raise ValueError("adaptive plans cannot use fixed-fixture roles")
            if any(
                agent.capability
                in {AgentCapability.CLARIFICATION, AgentCapability.PLANNING}
                for agent in self.agents
            ):
                raise ValueError(
                    "adaptive runtime plans cannot include bootstrap capabilities"
                )
            if not self.independent_review:
                raise ValueError("adaptive plans require independent quality control")

        agent_ids = [agent.id for agent in self.agents]
        if len(agent_ids) != len(set(agent_ids)):
            raise ValueError("TeamPlan Agent IDs must be unique")
        known_agents = set(agent_ids)
        dependencies = {agent.id: set(agent.dependencies) for agent in self.agents}
        for agent_id, required in dependencies.items():
            unknown = required - known_agents
            if unknown:
                raise ValueError(
                    f"Agent {agent_id} references unknown dependencies: "
                    f"{', '.join(sorted(unknown))}"
                )

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(agent_id: str) -> None:
            if agent_id in visiting:
                raise ValueError("TeamPlan dependencies must be acyclic")
            if agent_id in visited:
                return
            visiting.add(agent_id)
            for dependency in dependencies[agent_id]:
                visit(dependency)
            visiting.remove(agent_id)
            visited.add(agent_id)

        for agent_id in agent_ids:
            visit(agent_id)

        route_ids = {route.id for route in self.model_routes.routes}
        unknown_routes = {agent.model_route_id for agent in self.agents} - route_ids
        if unknown_routes:
            raise ValueError(
                "Agent model routes are not authorized: "
                f"{', '.join(sorted(unknown_routes))}"
            )
        for agent in self.agents:
            route = self.model_routes.get_route(agent.model_route_id)
            if agent.capability not in route.eligible_capabilities:
                raise ValueError(
                    f"model route {route.id} is not eligible for Agent "
                    f"{agent.id} ({agent.capability.value})"
                )
        if self.model_routes.mode is ModelRoutingMode.POLICY:
            assignments = {
                assignment.agent_id: assignment
                for assignment in self.model_routes.assignments
            }
            if set(assignments) != known_agents:
                missing = known_agents - set(assignments)
                unknown = set(assignments) - known_agents
                raise ValueError(
                    "policy model assignments must exactly cover TeamPlan Agents "
                    f"(missing: {', '.join(sorted(missing)) or 'none'}; "
                    f"unknown: {', '.join(sorted(unknown)) or 'none'})"
                )
            for agent in self.agents:
                assignment = assignments[agent.id]
                if assignment.primary_route_id != agent.model_route_id:
                    raise ValueError(
                        f"Agent {agent.id} primary model assignment differs from "
                        "its AgentSpec"
                    )
                for fallback_id in assignment.fallback_route_ids:
                    fallback = self.model_routes.get_route(fallback_id)
                    if agent.capability not in fallback.eligible_capabilities:
                        raise ValueError(
                            f"fallback model route {fallback.id} is not eligible for "
                            f"Agent {agent.id} ({agent.capability.value})"
                        )
        if self.max_concurrency > len(self.agents):
            raise ValueError("TeamPlan concurrency cannot exceed its Agent count")
        if len(self.agents) > self.budget.max_calls:
            raise ValueError("TeamPlan Agent count exceeds the run call budget")
        if (
            self.origin is TeamPlanOrigin.ADAPTIVE_PLANNING
            and len(self.agents) * self.iteration_limit > self.budget.max_calls
        ):
            raise ValueError(
                "TeamPlan planned Agent invocations exceed the run call budget"
            )
        if self.origin is TeamPlanOrigin.ADAPTIVE_PLANNING:
            fallback_calls = (
                sum(
                    len(assignment.fallback_route_ids)
                    for assignment in self.model_routes.assignments
                )
                * self.iteration_limit
            )
            planned_calls = len(self.agents) * self.iteration_limit + fallback_calls
            if planned_calls > self.budget.max_calls:
                raise ValueError(
                    "TeamPlan primary and authorized fallback invocations exceed "
                    "the run call budget"
                )

        implementation_agents = {
            agent.id
            for agent in self.agents
            if agent.capability
            in {AgentCapability.IMPLEMENTATION, AgentCapability.INTEGRATION}
        }
        if not implementation_agents:
            raise ValueError("TeamPlan requires an implementation capability")

        testing_agents = {
            agent.id
            for agent in self.agents
            if agent.capability is AgentCapability.TESTING
        }
        review_agents = {
            agent.id
            for agent in self.agents
            if agent.capability is AgentCapability.REVIEW
        }
        quality_agents = testing_agents | review_agents
        if self.independent_review and not quality_agents:
            raise ValueError("independent review requires a quality capability")
        if self.origin is TeamPlanOrigin.ADAPTIVE_PLANNING and not quality_agents:
            raise ValueError("adaptive plans require an independent quality Agent")
        if self.revision_enabled and not quality_agents:
            raise ValueError("evidence-driven revision requires a quality capability")
        if not self.revision_enabled and self.iteration_limit != 1:
            raise ValueError("a non-revising TeamPlan requires one iteration")

        def transitively_depends(agent_id: str, target: str) -> bool:
            pending = list(dependencies[agent_id])
            seen: set[str] = set()
            while pending:
                current = pending.pop()
                if current == target:
                    return True
                if current not in seen:
                    seen.add(current)
                    pending.extend(dependencies[current])
            return False

        if (
            testing_agents
            and review_agents
            and any(
                transitively_depends(tester, reviewer)
                or transitively_depends(reviewer, tester)
                for tester in testing_agents
                for reviewer in review_agents
            )
        ):
            raise ValueError("testing and review capabilities must remain independent")
        if self.independent_review:
            for quality_agent in quality_agents:
                if any(
                    not transitively_depends(quality_agent, implementation_agent)
                    for implementation_agent in implementation_agents
                ):
                    raise ValueError(
                        "every quality Agent must depend on every implementation path"
                    )

        def scopes_overlap(first: str, second: str) -> bool:
            first_parts = PurePosixPath(first).parts
            second_parts = PurePosixPath(second).parts
            common_length = min(len(first_parts), len(second_parts))
            return first_parts[:common_length] == second_parts[:common_length]

        for index, agent in enumerate(self.agents):
            for other in self.agents[index + 1 :]:
                if (
                    agent.permission_profile is PermissionProfile.READ_ONLY
                    and other.permission_profile is PermissionProfile.READ_ONLY
                ):
                    continue
                if not scopes_overlap(agent.workspace_scope, other.workspace_scope):
                    continue
                if not (
                    transitively_depends(agent.id, other.id)
                    or transitively_depends(other.id, agent.id)
                ):
                    raise ValueError(
                        "overlapping workspace access with a writer must be "
                        "dependency ordered"
                    )

        if self.origin is TeamPlanOrigin.FIXED_MANIFEST:
            legacy_roles = [agent.legacy_role for agent in self.agents]
            if len(legacy_roles) != len(set(legacy_roles)):
                raise ValueError("fixed TeamPlan legacy roles must be unique")
        return self

    def get_agent(self, agent_id: str) -> AgentSpec:
        """Return one AgentSpec or reject an unknown run-scoped identity."""

        for agent in self.agents:
            if agent.id == agent_id:
                return agent
        raise ValueError(f"unknown TeamPlan Agent: {agent_id}")

    @property
    def agent_capabilities(self) -> dict[str, str]:
        """Return the approved capability keyed by run-scoped Agent ID."""

        return {agent.id: agent.capability.value for agent in self.agents}

    @property
    def stage_agents(self) -> dict[str, set[str]]:
        """Return stage membership keyed by run-scoped Agent ID."""

        stages: dict[str, set[str]] = {}
        for agent in self.agents:
            stages.setdefault(agent.stage_id, set()).add(agent.id)
        return stages

    @property
    def legacy_roles(self) -> tuple[AgentRole, ...]:
        """Return compatibility roles for the current fixed execution adapter."""

        roles = tuple(agent.legacy_role for agent in self.agents)
        if any(role is None for role in roles):
            raise ValueError("TeamPlan contains an Agent without a legacy role")
        return tuple(role for role in roles if role is not None)

    @property
    def legacy_role_timeouts(self) -> dict[AgentRole, int]:
        """Return exact timeout evidence keyed by the current adapter role."""

        return {
            agent.legacy_role: agent.timeout_seconds
            for agent in self.agents
            if agent.legacy_role is not None
        }

    @property
    def legacy_stage_roles(self) -> dict[str, set[AgentRole]]:
        """Return current artifact-stage membership compiled from AgentSpecs."""

        stages: dict[str, set[AgentRole]] = {}
        for agent in self.agents:
            if agent.legacy_role is None:
                raise ValueError("TeamPlan contains an Agent without a legacy role")
            stages.setdefault(agent.stage_id, set()).add(agent.legacy_role)
        return stages

    def timeout_for_role(self, role: AgentRole) -> int:
        """Resolve one current-adapter invocation timeout from the TeamPlan."""

        for agent in self.agents:
            if agent.legacy_role is role:
                return agent.timeout_seconds
        raise ValueError(f"role {role.value} is not part of TeamPlan {self.plan_id}")

    def execution_waves(self) -> tuple[tuple[str, ...], ...]:
        """Return deterministic topological waves for inspection and scheduling."""

        remaining = {agent.id: set(agent.dependencies) for agent in self.agents}
        waves: list[tuple[str, ...]] = []
        completed: set[str] = set()
        while remaining:
            ready = tuple(
                agent.id
                for agent in self.agents
                if agent.id in remaining and remaining[agent.id].issubset(completed)
            )
            if not ready:
                raise ValueError("TeamPlan dependencies cannot be scheduled")
            waves.append(ready)
            completed.update(ready)
            for agent_id in ready:
                del remaining[agent_id]
        return tuple(waves)


class TeamStage(BaseModel):
    """One ordered stage in the initial team execution plan."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    mode: StageMode
    roles: list[AgentRole] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_stage(self) -> Self:
        """Reject duplicate roles and meaningless parallel stages."""

        if len(self.roles) != len(set(self.roles)):
            raise ValueError("a stage cannot contain a role more than once")
        if self.mode is StageMode.PARALLEL and len(self.roles) < 2:
            raise ValueError("a parallel stage requires at least two roles")
        return self


class TeamDefinition(BaseModel):
    """A complete, comparable Agent-team configuration."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    kind: TeamKind
    description: str = Field(min_length=1)
    roles: list[AgentRole] = Field(min_length=1)
    stages: list[TeamStage] = Field(min_length=1)
    max_iterations: int = Field(ge=1, le=3)
    independent_review: bool
    revision_enabled: bool

    @model_validator(mode="after")
    def validate_definition(self) -> Self:
        """Keep role membership, stage ordering, and experiment type coherent."""

        if len(self.roles) != len(set(self.roles)):
            raise ValueError("team roles must be unique")

        stage_ids = [stage.id for stage in self.stages]
        if len(stage_ids) != len(set(stage_ids)):
            raise ValueError("team stage IDs must be unique")

        staged_roles = [role for stage in self.stages for role in stage.roles]
        if len(staged_roles) != len(set(staged_roles)):
            raise ValueError("each role must appear in exactly one initial stage")
        if set(staged_roles) != set(self.roles):
            raise ValueError("team roles must exactly match roles assigned to stages")

        if self.kind is TeamKind.BASELINE:
            if self.roles != [AgentRole.SINGLE_AGENT]:
                raise ValueError("the baseline must contain only the single_agent role")
            if self.max_iterations != 1:
                raise ValueError(
                    "the baseline must use exactly one implementation pass"
                )
            if self.independent_review or self.revision_enabled:
                raise ValueError("the baseline cannot enable review-driven revision")
        else:
            required = {
                AgentRole.PLANNER,
                AgentRole.TESTER,
                AgentRole.REVIEWER,
            }
            if not required.issubset(self.roles):
                raise ValueError(
                    "multi-agent teams require planner, tester, and reviewer roles"
                )
            if not self.independent_review or not self.revision_enabled:
                raise ValueError(
                    "multi-agent teams require independent review and revision"
                )

        return self


class TeamManifest(BaseModel):
    """Versioned collection of all experimental team configurations."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(ge=1)
    default_team: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    shared_roles: list[AgentRole] = Field(default_factory=list)
    teams: list[TeamDefinition] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_manifest(self) -> Self:
        """Enforce unique teams and a complete Agent registry."""

        if len(self.shared_roles) != len(set(self.shared_roles)):
            raise ValueError("shared roles must be unique")
        if AgentRole.CLARIFIER not in self.shared_roles:
            raise ValueError("the clarifier must be registered as a shared role")

        team_ids = [team.id for team in self.teams]
        if len(team_ids) != len(set(team_ids)):
            raise ValueError("team IDs must be unique")
        if self.default_team not in team_ids:
            raise ValueError("default_team must reference a configured team")

        shared = set(self.shared_roles)
        for team in self.teams:
            if shared.intersection(team.roles):
                raise ValueError("shared roles cannot be duplicated inside a team")
        return self

    @property
    def required_roles(self) -> set[AgentRole]:
        """Return every Agent role required by the manifest."""

        return set(self.shared_roles).union(
            role for team in self.teams for role in team.roles
        )

    def get_team(self, team_id: str) -> TeamDefinition:
        """Return one team definition or raise a concise configuration error."""

        for team in self.teams:
            if team.id == team_id:
                return team
        raise ValueError(f"unknown team configuration: {team_id}")

    def validate_handoff_boundary(
        self,
        *,
        team_id: str,
        iteration: int,
        source_role: AgentRole,
        target_role: AgentRole | None,
    ) -> None:
        """Verify that a handoff belongs to its selected team and run budget."""

        team = self.get_team(team_id)
        if iteration > team.max_iterations:
            raise ValueError(
                f"iteration {iteration} exceeds {team_id} limit "
                f"of {team.max_iterations}"
            )

        allowed_roles = set(self.shared_roles).union(team.roles)
        for label, role in (("source", source_role), ("target", target_role)):
            if role is not None and role not in allowed_roles:
                raise ValueError(
                    f"{label} role {role.value} is not part of team {team_id}"
                )


def load_team_manifest(path: Path) -> TeamManifest:
    """Load and validate a team manifest from JSON."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    return TeamManifest.model_validate(payload)


_ROLE_CAPABILITIES = {
    AgentRole.CLARIFIER: AgentCapability.CLARIFICATION,
    AgentRole.SINGLE_AGENT: AgentCapability.IMPLEMENTATION,
    AgentRole.PLANNER: AgentCapability.PLANNING,
    AgentRole.GENERALIST_DEVELOPER: AgentCapability.IMPLEMENTATION,
    AgentRole.FRONTEND_DEVELOPER: AgentCapability.IMPLEMENTATION,
    AgentRole.BACKEND_DEVELOPER: AgentCapability.IMPLEMENTATION,
    AgentRole.INTEGRATOR: AgentCapability.INTEGRATION,
    AgentRole.TESTER: AgentCapability.TESTING,
    AgentRole.REVIEWER: AgentCapability.REVIEW,
}


def capability_for_legacy_role(role: AgentRole) -> AgentCapability:
    """Resolve one fixed-fixture role into the shared capability contract."""

    return _ROLE_CAPABILITIES[role]


_ROLE_RESPONSIBILITIES = {
    AgentRole.SINGLE_AGENT: "Implement the confirmed task as one accountable owner.",
    AgentRole.PLANNER: "Translate the confirmed task into an implementation plan.",
    AgentRole.GENERALIST_DEVELOPER: (
        "Implement and revise the complete product from the approved plan."
    ),
    AgentRole.FRONTEND_DEVELOPER: (
        "Implement and revise the user-interface portion of the approved plan."
    ),
    AgentRole.BACKEND_DEVELOPER: (
        "Implement and revise the server and persistence portion of the plan."
    ),
    AgentRole.INTEGRATOR: (
        "Integrate independently owned implementation work into one coherent result."
    ),
    AgentRole.TESTER: (
        "Analyze controller-run command evidence against confirmed acceptance."
    ),
    AgentRole.REVIEWER: (
        "Independently review the immutable result and configured manual scope."
    ),
}
_ROLE_RATIONALES = {
    AgentRole.SINGLE_AGENT: "This fixture measures a one-owner baseline.",
    AgentRole.PLANNER: "Planning is separated from source mutation in this fixture.",
    AgentRole.GENERALIST_DEVELOPER: (
        "One implementation owner avoids integration conflicts in the vertical slice."
    ),
    AgentRole.FRONTEND_DEVELOPER: (
        "The domain-specialized fixture isolates interface implementation context."
    ),
    AgentRole.BACKEND_DEVELOPER: (
        "The domain-specialized fixture isolates server and persistence context."
    ),
    AgentRole.INTEGRATOR: (
        "Separate domain work requires explicit controller-visible integration."
    ),
    AgentRole.TESTER: "Testing remains independent from source authorship.",
    AgentRole.REVIEWER: "Acceptance requires judgment independent from implementation.",
}
_ROLE_WORKSPACE_SCOPES = {
    AgentRole.SINGLE_AGENT: "repository",
    AgentRole.PLANNER: "repository",
    AgentRole.GENERALIST_DEVELOPER: "repository",
    AgentRole.FRONTEND_DEVELOPER: "repository/frontend",
    AgentRole.BACKEND_DEVELOPER: "repository/backend",
    AgentRole.INTEGRATOR: "repository",
    AgentRole.TESTER: "repository",
    AgentRole.REVIEWER: "repository",
}


def compile_fixed_team_plan(
    manifest: TeamManifest,
    *,
    team_id: str,
    run_id: str,
    task_brief_sha256: str,
    model: str,
    budget: AgentBudget,
    role_timeout_seconds: Mapping[AgentRole, int],
    iteration_limit: int,
    max_concurrency: int,
    created_at: datetime,
) -> TeamPlan:
    """Compile one fixed evaluation fixture into the run-scoped plan contract."""

    team = manifest.get_team(team_id)
    if not 1 <= iteration_limit <= team.max_iterations:
        raise ValueError(
            f"iteration limit must be between 1 and {team.max_iterations} for {team.id}"
        )
    if not 1 <= max_concurrency <= len(team.roles):
        raise ValueError("team concurrency must be between one and its Agent count")
    missing_timeouts = set(team.roles) - set(role_timeout_seconds)
    if missing_timeouts:
        names = ", ".join(sorted(role.value for role in missing_timeouts))
        raise ValueError(f"TeamPlan invocation timeouts are missing roles: {names}")

    agents: list[AgentSpec] = []
    previous_stage_roles: tuple[AgentRole, ...] = ()
    for stage in team.stages:
        for role in stage.roles:
            capability = _ROLE_CAPABILITIES[role]
            permission = (
                PermissionProfile.READ_ONLY
                if capability in _READ_ONLY_CAPABILITIES
                else PermissionProfile.WORKSPACE_WRITE
            )
            agents.append(
                AgentSpec(
                    id=role.value,
                    label=role.value.replace("_", " ").title(),
                    responsibility=_ROLE_RESPONSIBILITIES[role],
                    rationale=_ROLE_RATIONALES[role],
                    capability=capability,
                    permission_profile=permission,
                    stage_id=stage.id,
                    dependencies=tuple(item.value for item in previous_stage_roles),
                    expected_output=_CAPABILITY_OUTPUTS[capability],
                    model_route_id="primary",
                    timeout_seconds=role_timeout_seconds[role],
                    workspace_scope=_ROLE_WORKSPACE_SCOPES[role],
                    legacy_role=role,
                )
            )
        previous_stage_roles = tuple(stage.roles)

    return TeamPlan(
        plan_id=f"{run_id}-team-v1",
        revision=1,
        run_id=run_id,
        task_brief_sha256=task_brief_sha256,
        team_id=team.id,
        origin=TeamPlanOrigin.FIXED_MANIFEST,
        approval_source=PlanApprovalSource.COMPATIBILITY_POLICY,
        created_at=created_at,
        source_manifest_version=manifest.schema_version,
        source_team_id=team.id,
        source_team_sha256=canonical_model_sha256(team),
        agents=tuple(agents),
        model_routes=ModelRoutePlan(
            mode=ModelRoutingMode.STRICT,
            default_route_id="primary",
            routes=(ModelRoute(id="primary", model=model),),
        ),
        budget=budget,
        iteration_limit=iteration_limit,
        max_concurrency=max_concurrency,
        independent_review=team.independent_review,
        revision_enabled=team.revision_enabled,
    )


def validate_fixed_team_plan(plan: TeamPlan, manifest: TeamManifest) -> None:
    """Prove that a fixed-origin plan is the exact compiled fixture plus run inputs."""

    if plan.origin is not TeamPlanOrigin.FIXED_MANIFEST:
        raise ValueError("only fixed-origin TeamPlans use manifest validation")
    if plan.source_manifest_version != manifest.schema_version:
        raise ValueError("TeamPlan uses a different team manifest version")
    team = manifest.get_team(plan.team_id)
    if plan.source_team_sha256 != canonical_model_sha256(team):
        raise ValueError("TeamPlan uses a different fixed team definition")

    route = plan.model_routes.get_route(plan.model_routes.default_route_id)
    expected = compile_fixed_team_plan(
        manifest,
        team_id=plan.team_id,
        run_id=plan.run_id,
        task_brief_sha256=plan.task_brief_sha256,
        model=route.model,
        budget=plan.budget,
        role_timeout_seconds=plan.legacy_role_timeouts,
        iteration_limit=plan.iteration_limit,
        max_concurrency=plan.max_concurrency,
        created_at=plan.created_at,
    )
    if expected != plan:
        raise ValueError("TeamPlan differs from its fixed manifest compilation")
