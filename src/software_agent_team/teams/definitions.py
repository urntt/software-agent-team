"""Core data models: Agent specifications, team plans, stages, and definitions.

核心数据结构: Agent 规范 (AgentSpec)、运行期团队计划 (TeamPlan)、阶段与清单定义。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import PurePosixPath
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from software_agent_team.artifacts import AgentRole, ArtifactKind
from software_agent_team.budgets import AgentBudget

from .common import _clean_unique_text, _require_utc
from .model_routing import ModelRoutePlan, ModelRoutingMode
from .types import (
    _READ_ONLY_CAPABILITIES,
    SPECIALIZATION_CATALOG_VERSION,
    AgentCapability,
    AgentSpecialization,
    PermissionProfile,
    PlanApprovalSource,
    StageMode,
    TeamKind,
    TeamPlanOrigin,
    default_specialization_for_capability,
    specialization_contract,
    workspace_scopes_overlap,
)

TEAM_PLAN_SCHEMA_VERSION = 4


class AgentSpec(BaseModel):
    """One run-scoped responsibility proposed or compiled for the controller.

    单个由控制器编排与调度的运行时 Agent 角色规范契约。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    label: str = Field(min_length=1, max_length=80)
    responsibility: str = Field(min_length=1, max_length=500)
    rationale: str = Field(min_length=1, max_length=500)
    capability: AgentCapability
    specialization: AgentSpecialization
    permission_profile: PermissionProfile
    stage_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    dependencies: tuple[str, ...] = ()
    expected_output: ArtifactKind
    model_route_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    # Positive values are controlled-evaluation wall-clock limits. Adaptive
    # product plans use zero, which OpenClaw defines as no Agent runtime
    # timeout; provider stream inactivity and an optional task deadline are
    # separate authorities.
    timeout_seconds: int = Field(ge=0)
    workspace_scope: str = Field(min_length=1, max_length=200)
    legacy_role: AgentRole | None = None

    @model_validator(mode="before")
    @classmethod
    def infer_legacy_specialization(cls, value: object) -> object:
        """Upgrade pre-catalog AgentSpec payloads without changing their authority."""
        if not isinstance(value, dict) or value.get("specialization") is not None:
            return value
        payload = dict(value)
        try:
            capability = AgentCapability(payload.get("capability"))
        except (TypeError, ValueError):
            return payload
        payload["specialization"] = default_specialization_for_capability(
            capability
        ).value
        return payload

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
        contract = specialization_contract(self.specialization)
        if self.capability not in contract.compatible_capabilities:
            raise ValueError(
                "Agent specialization and executable capability are incompatible"
            )
        if self.expected_output is not contract.expected_output:
            raise ValueError(
                "Agent specialization and expected output are inconsistent"
            )
        if self.permission_profile is not contract.permission_ceiling:
            raise ValueError(
                "Agent permission exceeds or differs from its specialization ceiling"
            )
        if self.capability in _READ_ONLY_CAPABILITIES:
            if self.permission_profile is not PermissionProfile.READ_ONLY:
                raise ValueError("planning and quality capabilities must be read-only")
        elif self.permission_profile is not PermissionProfile.WORKSPACE_WRITE:
            raise ValueError("implementation capabilities require workspace write")
        if self.id in self.dependencies:
            raise ValueError("an Agent cannot depend on itself")
        return self


class TeamPlan(BaseModel):
    """Approved run-scoped authority for Agent creation and scheduling.

    经授权的单次运行周期团队执行计划与 DAG 拓扑权威。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1, 2, 3, TEAM_PLAN_SCHEMA_VERSION] = (
        TEAM_PLAN_SCHEMA_VERSION
    )
    specialization_catalog_version: Literal[SPECIALIZATION_CATALOG_VERSION] = (
        SPECIALIZATION_CATALOG_VERSION
    )
    plan_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    revision: int = Field(ge=1)
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
    agents: tuple[AgentSpec, ...] = Field(min_length=1)
    model_routes: ModelRoutePlan
    budget: AgentBudget
    run_deadline_seconds: int | None = Field(default=None, ge=1)
    iteration_limit: int = Field(ge=1)
    max_concurrency: int = Field(ge=1)
    independent_review: bool
    revision_enabled: bool

    @model_validator(mode="before")
    @classmethod
    def require_current_specialization_identity(cls, value: object) -> object:
        """Keep old plans readable while current plans name catalog authority."""
        if not isinstance(value, dict):
            return value
        version = value.get("schema_version", TEAM_PLAN_SCHEMA_VERSION)
        if version != TEAM_PLAN_SCHEMA_VERSION:
            return value
        agents = value.get("agents")
        if isinstance(agents, (list, tuple)) and any(
            isinstance(agent, dict) and "specialization" not in agent
            for agent in agents
        ):
            raise ValueError("current TeamPlan Agents require specialization IDs")
        return value

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
            if any(agent.timeout_seconds == 0 for agent in self.agents):
                raise ValueError(
                    "fixed evaluation plans require positive invocation timeouts"
                )
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
        if (
            self.budget.max_calls is not None
            and len(self.agents) > self.budget.max_calls
        ):
            raise ValueError("TeamPlan Agent count exceeds the run call budget")
        if (
            self.origin is TeamPlanOrigin.ADAPTIVE_PLANNING
            and self.budget.max_calls is not None
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
            if (
                self.budget.max_calls is not None
                and planned_calls > self.budget.max_calls
            ):
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

        if self.independent_review:
            for quality_agent in quality_agents:
                if any(
                    not transitively_depends(quality_agent, implementation_agent)
                    for implementation_agent in implementation_agents
                ):
                    raise ValueError(
                        "every quality Agent must depend on every implementation path"
                    )

        for index, agent in enumerate(self.agents):
            for other in self.agents[index + 1 :]:
                if (
                    agent.permission_profile is PermissionProfile.READ_ONLY
                    and other.permission_profile is PermissionProfile.READ_ONLY
                ):
                    continue
                if not workspace_scopes_overlap(
                    agent.workspace_scope,
                    other.workspace_scope,
                ):
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
    def agent_specializations(self) -> dict[str, str]:
        """Return the approved specialization keyed by run-scoped Agent ID."""
        return {agent.id: agent.specialization.value for agent in self.agents}

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
    """One ordered stage in the initial team execution plan.

    团队初始执行计划中的一个有序执行阶段。
    """

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
    """A complete, comparable Agent-team configuration.

    完整的、可对比评估的 Agent 团队配置定义。
    """

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
    """Versioned collection of all experimental team configurations.

    版本化的所有实验团队配置集合 (清单)。
    """

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
