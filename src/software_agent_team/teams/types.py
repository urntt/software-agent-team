"""Domain types, enums, and specialization contracts for software agent teams.

团队领域核心类型系统: 枚举、特化契约 (Specialization Contract) 以及能力映射工具。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from types import MappingProxyType

from software_agent_team.artifacts import ArtifactKind

SPECIALIZATION_CATALOG_VERSION = 1


def workspace_scopes_overlap(first: str, second: str) -> bool:
    """Return whether two canonical repository scopes share one subtree.

    判断两个仓库规范路径范围是否在同一子树存在重叠冲突。
    """
    first_parts = PurePosixPath(first).parts
    second_parts = PurePosixPath(second).parts
    common_length = min(len(first_parts), len(second_parts))
    return first_parts[:common_length] == second_parts[:common_length]


class TeamKind(StrEnum):
    """Experimental category for a team definition.

    团队定义实验分类: 单 Agent 基准或多 Agent 协同。
    """

    BASELINE = "baseline"
    MULTI_AGENT = "multi_agent"


class StageMode(StrEnum):
    """Execution relationship among roles in one stage.

    阶段内角色的执行关系: 串行或并行。
    """

    SEQUENTIAL = "sequential"
    PARALLEL = "parallel"


class TeamPlanOrigin(StrEnum):
    """Authority that produced one approved run-scoped team plan.

    团队计划来源权威: 固定清单配置或自适应规划生成。
    """

    FIXED_MANIFEST = "fixed_manifest"
    ADAPTIVE_PLANNING = "adaptive_planning"


class PlanApprovalSource(StrEnum):
    """Actor that authorized one immutable plan revision.

    计划审批来源: 策略预授权或用户显式确认。
    """

    COMPATIBILITY_POLICY = "compatibility_policy"
    USER = "user"


class AgentCapability(StrEnum):
    """Controller-known work contract independent from a user-facing label.

    控制器认知的不可变工作能力契约。
    """

    CLARIFICATION = "clarification"
    PLANNING = "planning"
    IMPLEMENTATION = "implementation"
    INTEGRATION = "integration"
    TESTING = "testing"
    REVIEW = "review"


class AgentSpecialization(StrEnum):
    """Controller-owned professional contract selected for one task Agent.

    控制器拥有的专业分工特化契约。
    """

    REQUIREMENTS_CLARIFICATION = "requirements_clarification"
    TEAM_PLANNING = "team_planning"
    PRODUCT_IMPLEMENTATION = "product_implementation"
    SYSTEM_INTEGRATION = "system_integration"
    DETERMINISTIC_TESTING = "deterministic_testing"
    GENERAL_REVIEW = "general_review"
    SECURITY_ASSESSMENT = "security_assessment"
    EXPERIENCE_ASSESSMENT = "experience_assessment"


class AcceptanceAuthority(StrEnum):
    """Criterion claim class one specialization may support.

    特化角色所具备的验收断言权限类别。
    """

    NONE = "none"
    DETERMINISTIC_EVIDENCE = "deterministic_evidence"
    GENERAL_REVIEW = "general_review"
    SECURITY = "security"
    USER_EXPERIENCE = "user_experience"


class PermissionProfile(StrEnum):
    """Versioned least-privilege profiles assignable to an AgentSpec.

    最小权限安全配置文件: 只读或工作区写入权限。
    """

    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"


@dataclass(frozen=True)
class SpecializationContract:
    """Immutable Controller catalog entry for one professional specialization.

    专业特化角色的不可变元数据与边界契约。
    """

    id: AgentSpecialization
    purpose: str
    compatible_capabilities: tuple[AgentCapability, ...]
    permission_ceiling: PermissionProfile
    expected_output: ArtifactKind
    prompt_module: str | None
    acceptance_authority: AcceptanceAuthority
    handoff_boundary: str


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

_DEFAULT_SPECIALIZATIONS = {
    AgentCapability.CLARIFICATION: AgentSpecialization.REQUIREMENTS_CLARIFICATION,
    AgentCapability.PLANNING: AgentSpecialization.TEAM_PLANNING,
    AgentCapability.IMPLEMENTATION: AgentSpecialization.PRODUCT_IMPLEMENTATION,
    AgentCapability.INTEGRATION: AgentSpecialization.SYSTEM_INTEGRATION,
    AgentCapability.TESTING: AgentSpecialization.DETERMINISTIC_TESTING,
    AgentCapability.REVIEW: AgentSpecialization.GENERAL_REVIEW,
}

SPECIALIZATION_CATALOG: Mapping[AgentSpecialization, SpecializationContract] = (
    MappingProxyType(
        {
            AgentSpecialization.REQUIREMENTS_CLARIFICATION: SpecializationContract(
                id=AgentSpecialization.REQUIREMENTS_CLARIFICATION,
                purpose="Clarify unresolved product intent without executing work.",
                compatible_capabilities=(AgentCapability.CLARIFICATION,),
                permission_ceiling=PermissionProfile.READ_ONLY,
                expected_output=ArtifactKind.CLARIFICATION_RECORD,
                prompt_module=None,
                acceptance_authority=AcceptanceAuthority.NONE,
                handoff_boundary="confirmed requirement evidence",
            ),
            AgentSpecialization.TEAM_PLANNING: SpecializationContract(
                id=AgentSpecialization.TEAM_PLANNING,
                purpose="Derive a user-reviewable implementation and Agent plan.",
                compatible_capabilities=(AgentCapability.PLANNING,),
                permission_ceiling=PermissionProfile.READ_ONLY,
                expected_output=ArtifactKind.IMPLEMENTATION_PLAN,
                prompt_module=None,
                acceptance_authority=AcceptanceAuthority.NONE,
                handoff_boundary="user-approved plan",
            ),
            AgentSpecialization.PRODUCT_IMPLEMENTATION: SpecializationContract(
                id=AgentSpecialization.PRODUCT_IMPLEMENTATION,
                purpose="Implement approved product behavior in the project workspace.",
                compatible_capabilities=(AgentCapability.IMPLEMENTATION,),
                permission_ceiling=PermissionProfile.WORKSPACE_WRITE,
                expected_output=ArtifactKind.WORK_RESULT,
                prompt_module="specialization_product_implementation.md",
                acceptance_authority=AcceptanceAuthority.NONE,
                handoff_boundary="controller-verified Git result",
            ),
            AgentSpecialization.SYSTEM_INTEGRATION: SpecializationContract(
                id=AgentSpecialization.SYSTEM_INTEGRATION,
                purpose="Integrate dependency-ordered implementation results.",
                compatible_capabilities=(AgentCapability.INTEGRATION,),
                permission_ceiling=PermissionProfile.WORKSPACE_WRITE,
                expected_output=ArtifactKind.WORK_RESULT,
                prompt_module="specialization_system_integration.md",
                acceptance_authority=AcceptanceAuthority.NONE,
                handoff_boundary="controller-verified integrated Git result",
            ),
            AgentSpecialization.DETERMINISTIC_TESTING: SpecializationContract(
                id=AgentSpecialization.DETERMINISTIC_TESTING,
                purpose="Analyze controller-recorded deterministic command evidence.",
                compatible_capabilities=(AgentCapability.TESTING,),
                permission_ceiling=PermissionProfile.READ_ONLY,
                expected_output=ArtifactKind.TEST_REPORT,
                prompt_module="specialization_deterministic_testing.md",
                acceptance_authority=AcceptanceAuthority.DETERMINISTIC_EVIDENCE,
                handoff_boundary="controller-bound deterministic test report",
            ),
            AgentSpecialization.GENERAL_REVIEW: SpecializationContract(
                id=AgentSpecialization.GENERAL_REVIEW,
                purpose="Independently review assigned acceptance criteria.",
                compatible_capabilities=(AgentCapability.REVIEW,),
                permission_ceiling=PermissionProfile.READ_ONLY,
                expected_output=ArtifactKind.REVIEW_REPORT,
                prompt_module="specialization_general_review.md",
                acceptance_authority=AcceptanceAuthority.GENERAL_REVIEW,
                handoff_boundary="grounded criterion review",
            ),
            AgentSpecialization.SECURITY_ASSESSMENT: SpecializationContract(
                id=AgentSpecialization.SECURITY_ASSESSMENT,
                purpose=(
                    "Assess approved security boundaries, threat surfaces, controls, "
                    "and residual risk."
                ),
                compatible_capabilities=(AgentCapability.REVIEW,),
                permission_ceiling=PermissionProfile.READ_ONLY,
                expected_output=ArtifactKind.SECURITY_ASSESSMENT,
                prompt_module="specialization_security_assessment.md",
                acceptance_authority=AcceptanceAuthority.SECURITY,
                handoff_boundary="grounded security assessment",
            ),
            AgentSpecialization.EXPERIENCE_ASSESSMENT: SpecializationContract(
                id=AgentSpecialization.EXPERIENCE_ASSESSMENT,
                purpose=(
                    "Assess the approved target user's workflow, friction, outcome, "
                    "and recovery behavior."
                ),
                compatible_capabilities=(AgentCapability.REVIEW,),
                permission_ceiling=PermissionProfile.READ_ONLY,
                expected_output=ArtifactKind.EXPERIENCE_ASSESSMENT,
                prompt_module="specialization_experience_assessment.md",
                acceptance_authority=AcceptanceAuthority.USER_EXPERIENCE,
                handoff_boundary="grounded user-experience assessment",
            ),
        }
    )
)


def default_specialization_for_capability(
    capability: AgentCapability,
) -> AgentSpecialization:
    """Return the compatibility specialization for one executable capability.

    获取能力的默认兼容特化类型。
    """
    return _DEFAULT_SPECIALIZATIONS[capability]


def specialization_contract(
    specialization: AgentSpecialization,
) -> SpecializationContract:
    """Return one immutable Controller-owned specialization contract.

    查询指定专业特化的不可变契约定义。
    """
    return SPECIALIZATION_CATALOG[specialization]


def expected_output_for_specialization(
    specialization: AgentSpecialization,
) -> ArtifactKind:
    """Return the typed output selected by one approved specialization.

    获取特化角色所对应的期望产物类型。
    """
    return specialization_contract(specialization).expected_output


def expected_output_for_capability(capability: AgentCapability) -> ArtifactKind:
    """Return the controller-owned output contract for one capability.

    获取基础能力所对应的期望产物类型。
    """
    return _CAPABILITY_OUTPUTS[capability]


def permission_for_capability(capability: AgentCapability) -> PermissionProfile:
    """Resolve the least-privilege profile assigned to one capability.

    解析基础能力对应的最低权限配置。
    """
    if capability in _READ_ONLY_CAPABILITIES:
        return PermissionProfile.READ_ONLY
    return PermissionProfile.WORKSPACE_WRITE
