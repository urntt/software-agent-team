"""Versioned team fixtures, run-scoped plans, validation, and compilation.

版本化团队夹具定义、运行期团队计划 (TeamPlan)、模型路由策略及确定性编译器。
该子包对外提供 100% 向后兼容的符号导出。
"""

from __future__ import annotations

from software_agent_team.artifacts import AgentRole, ArtifactKind
from software_agent_team.budgets import AgentBudget

from .common import _clean_unique_text, _require_utc
from .compiler import (
    _ROLE_CAPABILITIES,
    _ROLE_RATIONALES,
    _ROLE_RESPONSIBILITIES,
    _ROLE_WORKSPACE_SCOPES,
    capability_for_legacy_role,
    compile_fixed_team_plan,
    validate_fixed_team_plan,
)
from .definitions import (
    TEAM_PLAN_SCHEMA_VERSION,
    AgentSpec,
    TeamDefinition,
    TeamManifest,
    TeamPlan,
    TeamStage,
)
from .manifest import load_team_manifest
from .model_routing import (
    ModelRoute,
    ModelRouteAssignment,
    ModelRoutePlan,
    ModelRouteSelectionSource,
    ModelRoutingMode,
    ModelSwitchCondition,
)
from .types import (
    _CAPABILITY_OUTPUTS,
    _DEFAULT_SPECIALIZATIONS,
    _READ_ONLY_CAPABILITIES,
    SPECIALIZATION_CATALOG,
    SPECIALIZATION_CATALOG_VERSION,
    AcceptanceAuthority,
    AgentCapability,
    AgentSpecialization,
    PermissionProfile,
    PlanApprovalSource,
    SpecializationContract,
    StageMode,
    TeamKind,
    TeamPlanOrigin,
    default_specialization_for_capability,
    expected_output_for_capability,
    expected_output_for_specialization,
    permission_for_capability,
    specialization_contract,
    workspace_scopes_overlap,
)

__all__ = [
    "SPECIALIZATION_CATALOG",
    "SPECIALIZATION_CATALOG_VERSION",
    "TEAM_PLAN_SCHEMA_VERSION",
    "_CAPABILITY_OUTPUTS",
    "_DEFAULT_SPECIALIZATIONS",
    "_READ_ONLY_CAPABILITIES",
    "_ROLE_CAPABILITIES",
    "_ROLE_RATIONALES",
    "_ROLE_RESPONSIBILITIES",
    "_ROLE_WORKSPACE_SCOPES",
    "AcceptanceAuthority",
    "AgentBudget",
    "AgentCapability",
    "AgentRole",
    "AgentSpec",
    "AgentSpecialization",
    "ArtifactKind",
    "ModelRoute",
    "ModelRouteAssignment",
    "ModelRoutePlan",
    "ModelRouteSelectionSource",
    "ModelRoutingMode",
    "ModelSwitchCondition",
    "PermissionProfile",
    "PlanApprovalSource",
    "SpecializationContract",
    "StageMode",
    "TeamDefinition",
    "TeamKind",
    "TeamManifest",
    "TeamPlan",
    "TeamPlanOrigin",
    "TeamStage",
    "_clean_unique_text",
    "_require_utc",
    "capability_for_legacy_role",
    "compile_fixed_team_plan",
    "default_specialization_for_capability",
    "expected_output_for_capability",
    "expected_output_for_specialization",
    "load_team_manifest",
    "permission_for_capability",
    "specialization_contract",
    "validate_fixed_team_plan",
    "workspace_scopes_overlap",
]
