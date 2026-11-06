"""Fixed evaluation fixture compilation and deterministic plan verification.

固定基准与多智能体评测配置编译: 将静态 TeamManifest 编译为运行期 TeamPlan 契约。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from software_agent_team.artifacts import AgentRole
from software_agent_team.budgets import AgentBudget
from software_agent_team.integrity import canonical_model_sha256

from .definitions import AgentSpec, TeamManifest, TeamPlan
from .model_routing import ModelRoute, ModelRoutePlan, ModelRoutingMode
from .types import (
    _READ_ONLY_CAPABILITIES,
    AgentCapability,
    PermissionProfile,
    PlanApprovalSource,
    TeamPlanOrigin,
    default_specialization_for_capability,
    expected_output_for_specialization,
)

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
    """Resolve one fixed-fixture role into the shared capability contract.

    将固定实验角色映射解析为核心能力契约。
    """
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
    """Compile one fixed evaluation fixture into the run-scoped plan contract.

    将固定实验评测夹具配置编译为具备完整运行期权威的 TeamPlan 契约。
    """
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
            specialization = default_specialization_for_capability(capability)
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
                    specialization=specialization,
                    permission_profile=permission,
                    stage_id=stage.id,
                    dependencies=tuple(item.value for item in previous_stage_roles),
                    expected_output=expected_output_for_specialization(specialization),
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
    """Prove that a fixed-origin plan is the exact compiled fixture plus run inputs.

    验证固定来源的 TeamPlan 必须严格等价于其来源 Manifest 的确定性编译结果。
    """
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
