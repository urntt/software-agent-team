"""Concrete plan-execution evaluation logic for SAT readiness checkpoints.

计划执行评估逻辑: 在创建运行期 Agent 或工作空间之前, 全面评估已获批准的
执行计划 (TeamPlan)、Prompt 模版、运行期 Docker/OpenClaw 预检、模型路由、
种子工作区以及交付目录边界。
"""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path

from software_agent_team.integrity import canonical_model_sha256
from software_agent_team.runtime_configuration import RuntimePreflight
from software_agent_team.self_check import (
    SelfCheckCategory,
    SelfCheckCheckpoint,
    SelfCheckOwner,
    SelfCheckResult,
    SelfCheckSeverity,
    SelfCheckStatus,
    TaskSelfCheckReport,
)
from software_agent_team.teams import (
    SPECIALIZATION_CATALOG_VERSION,
    TeamPlan,
    specialization_contract,
)

from .common import PROMPT_TEMPLATE_ROOT, _result, _utc_now


def _get_prompt_template_root() -> Path:
    """Return the active prompt template directory, honoring package overrides.

    获取当前生效的 prompt 模版根目录;
    动态查询 package 模块以支持测试打桩 (monkeypatch)。
    """
    import software_agent_team.self_check_evaluation as pkg

    return getattr(pkg, "PROMPT_TEMPLATE_ROOT", PROMPT_TEMPLATE_ROOT)


def _evaluate_plan_approval(
    team_plan: TeamPlan,
    checked_at: datetime,
) -> SelfCheckResult:
    """Evaluate approved TeamPlan revision, digest, and Agent count.

    评估经批准的 TeamPlan 版本、哈希摘要及包含的 Agent 数量。
    """
    plan_digest = canonical_model_sha256(team_plan)
    return _result(
        check_id="plan.approval",
        checkpoint=SelfCheckCheckpoint.PLAN_EXECUTION,
        category=SelfCheckCategory.TASK,
        owner=SelfCheckOwner.APPROVED_PLAN,
        dependencies=("task.request", "budget.authorization"),
        observed_fact=(
            f"approved TeamPlan revision {team_plan.revision} binds "
            f"{len(team_plan.agents)} Agent(s)"
        ),
        evidence_kind="controller_contract",
        evidence_reference=f"TeamPlan sha256:{plan_digest}",
        input_value=team_plan.model_dump(mode="json"),
        checked_at=checked_at,
        rerun_rule="Re-run after plan approval, revision, or task authority changes.",
    )


def _evaluate_specialization_catalog(
    team_plan: TeamPlan,
    checked_at: datetime,
) -> SelfCheckResult:
    """Verify that all specialization prompt modules required by Agents exist on disk.

    验证计划中各 Agent 所需的专业化 Prompt 模版文件在本地磁盘上均存在且可安全读取。
    """
    prompt_root = _get_prompt_template_root()
    specialization_modules: list[dict[str, object]] = []
    specialization_problems: list[str] = []
    for agent in team_plan.agents:
        if agent.legacy_role is not None:
            continue
        module = specialization_contract(agent.specialization).prompt_module
        if module is None:
            specialization_problems.append(
                f"{agent.id} has no runtime specialization prompt module"
            )
            specialization_modules.append(
                {
                    "agent_id": agent.id,
                    "specialization": agent.specialization.value,
                    "module": None,
                    "sha256": None,
                }
            )
            continue
        module_path = prompt_root / module
        digest: str | None = None
        try:
            if module_path.is_file() and not module_path.is_symlink():
                digest = hashlib.sha256(module_path.read_bytes()).hexdigest()
        except OSError:
            digest = None
        if digest is None:
            specialization_problems.append(
                f"{agent.id} requires unavailable prompt module {module}"
            )
        specialization_modules.append(
            {
                "agent_id": agent.id,
                "specialization": agent.specialization.value,
                "module": module,
                "sha256": digest,
            }
        )
    specialization_ready = not specialization_problems
    return _result(
        check_id="agent.specialization_catalog",
        checkpoint=SelfCheckCheckpoint.PLAN_EXECUTION,
        category=SelfCheckCategory.AGENT,
        owner=SelfCheckOwner.SAT,
        dependencies=("plan.approval",),
        observed_fact=(
            f"specialization catalog v{SPECIALIZATION_CATALOG_VERSION}; "
            f"runtime modules={len(specialization_modules)}; "
            f"ready={specialization_ready}"
        ),
        evidence_kind="local_observation",
        evidence_reference=(
            f"packaged specialization catalog v{SPECIALIZATION_CATALOG_VERSION}"
        ),
        input_value={
            "catalog_version": SPECIALIZATION_CATALOG_VERSION,
            "modules": specialization_modules,
            "problems": specialization_problems,
        },
        checked_at=checked_at,
        status=(
            SelfCheckStatus.PASS if specialization_ready else SelfCheckStatus.BLOCKED
        ),
        severity=(
            SelfCheckSeverity.INFO
            if specialization_ready
            else SelfCheckSeverity.REQUIRED
        ),
        consequence=(
            None
            if specialization_ready
            else "Approved specialization prompts cannot be compiled safely."
        ),
        remediation=(
            None
            if specialization_ready
            else "Reinstall or repair the exact SAT application revision."
        ),
        rerun_rule=(
            "Re-run after SAT installation, source revision, catalog, or "
            "approved Agent specialization changes."
        ),
    )


def _evaluate_runtime_plan(
    runtime_preflight: RuntimePreflight | None,
    runtime_error: str | None,
    checked_at: datetime,
) -> SelfCheckResult:
    """Evaluate runtime preflight readiness and sandbox container prerequisites.

    评估运行期预检状态 (Docker 沙箱容器就绪、镜像存在及配置合法性)。
    """
    runtime_ready = runtime_preflight is not None and runtime_preflight.ready
    runtime_fact = (
        runtime_error
        if runtime_preflight is None
        else (
            f"config={runtime_preflight.config_valid}; "
            f"image={runtime_preflight.sandbox_image_present}; "
            f"container={runtime_preflight.sandbox_container_ready}"
            + (
                f"; {runtime_preflight.config_error}"
                if runtime_preflight.config_error
                else ""
            )
        )
    )
    return _result(
        check_id="runtime.plan",
        checkpoint=SelfCheckCheckpoint.PLAN_EXECUTION,
        category=SelfCheckCategory.RUNTIME,
        owner=SelfCheckOwner.SAT,
        dependencies=("plan.approval", "runtime.sandbox_image"),
        observed_fact=runtime_fact or "approved-plan runtime check failed",
        evidence_kind="local_observation",
        evidence_reference="approved-plan runtime preflight",
        input_value=(
            runtime_error
            if runtime_preflight is None
            else runtime_preflight.model_dump(mode="json")
        ),
        checked_at=checked_at,
        status=SelfCheckStatus.PASS if runtime_ready else SelfCheckStatus.BLOCKED,
        severity=(
            SelfCheckSeverity.INFO if runtime_ready else SelfCheckSeverity.REQUIRED
        ),
        consequence=(
            None if runtime_ready else "Runtime Agents cannot be created safely."
        ),
        remediation=(
            None
            if runtime_ready
            else "Repair the reported runtime, image, or configuration failure."
        ),
        rerun_rule=(
            "Re-run after plan, runtime configuration, Docker, or OpenClaw changes."
        ),
    )


def _evaluate_model_routes(
    team_plan: TeamPlan,
    runtime_preflight: RuntimePreflight | None,
    checked_at: datetime,
) -> list[SelfCheckResult]:
    """Verify that every model route bound by the approved plan is ready.

    验证执行计划绑定的每条模型路由在本地均已配置就绪且元数据完备。
    """
    inspections = {
        item.model: item
        for item in (
            () if runtime_preflight is None else runtime_preflight.model_inspections
        )
    }
    checks: list[SelfCheckResult] = []
    for route in team_plan.model_routes.routes:
        inspection = inspections.get(route.model)
        route_ready = inspection is not None and inspection.available
        metadata_ready = (
            route.input_cost_per_million_usd is not None
            and route.output_cost_per_million_usd is not None
            and route.pricing_source is not None
            and route.cache_pricing is not None
            and route.context_window_tokens is not None
            and route.context_source is not None
        )
        checks.append(
            _result(
                check_id=f"route.{route.id}",
                checkpoint=SelfCheckCheckpoint.PLAN_EXECUTION,
                category=SelfCheckCategory.ROUTE,
                owner=SelfCheckOwner.APPROVED_PLAN,
                dependencies=("runtime.plan",),
                observed_fact=(
                    f"{route.model}; available={route_ready}; "
                    f"metadata_complete={metadata_ready}; eligible="
                    + ",".join(item.value for item in route.eligible_capabilities)
                ),
                evidence_kind="local_observation",
                evidence_reference=f"approved route:{route.id}",
                input_value={
                    "route": route.model_dump(mode="json"),
                    "inspection": (
                        None
                        if inspection is None
                        else inspection.model_dump(mode="json")
                    ),
                },
                checked_at=checked_at,
                status=(
                    SelfCheckStatus.PASS
                    if route_ready and metadata_ready
                    else SelfCheckStatus.BLOCKED
                ),
                severity=(
                    SelfCheckSeverity.INFO
                    if route_ready and metadata_ready
                    else SelfCheckSeverity.REQUIRED
                ),
                consequence=(
                    None
                    if route_ready and metadata_ready
                    else (
                        "An approved Agent route cannot be launched with complete "
                        "authority."
                    )
                ),
                remediation=(
                    None
                    if route_ready and metadata_ready
                    else "Repair this model route or approve a revised TeamPlan."
                ),
                rerun_rule=(
                    "Re-run after route, metadata, auth, or model catalog changes."
                ),
            )
        )
    return checks


def _evaluate_agents(
    team_plan: TeamPlan,
    checked_at: datetime,
) -> list[SelfCheckResult]:
    """Verify that every Agent defined in the approved plan is fully specified.

    验证计划中所有 Agent 的能力、专业分工、权限配置及工作空间作用域均清晰完整。
    """
    checks: list[SelfCheckResult] = []
    for agent in team_plan.agents:
        checks.append(
            _result(
                check_id=f"agent.{agent.id}",
                checkpoint=SelfCheckCheckpoint.PLAN_EXECUTION,
                category=SelfCheckCategory.AGENT,
                owner=SelfCheckOwner.APPROVED_PLAN,
                dependencies=(
                    "plan.approval",
                    "agent.specialization_catalog",
                    f"route.{agent.model_route_id}",
                ),
                observed_fact=(
                    f"{agent.label}: capability={agent.capability.value}; "
                    f"specialization={agent.specialization.value}; "
                    f"permission={agent.permission_profile.value}; "
                    f"workspace={agent.workspace_scope}; "
                    f"output={agent.expected_output.value}; "
                    f"route={agent.model_route_id}"
                ),
                evidence_kind="controller_contract",
                evidence_reference=f"TeamPlan Agent:{agent.id}",
                input_value=agent.model_dump(mode="json"),
                checked_at=checked_at,
                rerun_rule=(
                    "Re-run after Agent, permission, route, or workspace authority "
                    "changes."
                ),
            )
        )
    return checks


def _evaluate_workspace_source(
    source_repository: Path,
    checked_at: datetime,
) -> SelfCheckResult:
    """Verify that the execution seed source repository exists and is a valid directory.

    验证用于生成隔离工作区的执行模版/源码种子目录真实存在且不是软链接。
    """
    source_ready = (
        source_repository.is_absolute()
        and source_repository.is_dir()
        and not source_repository.is_symlink()
    )
    return _result(
        check_id="workspace.source",
        checkpoint=SelfCheckCheckpoint.PLAN_EXECUTION,
        category=SelfCheckCategory.WORKSPACE,
        owner=SelfCheckOwner.SAT,
        dependencies=("runtime.plan",),
        observed_fact=(
            f"verified seed workspace ready={source_ready}: {source_repository}"
        ),
        evidence_kind="local_observation",
        evidence_reference=str(source_repository),
        input_value={"path": str(source_repository), "ready": source_ready},
        checked_at=checked_at,
        status=SelfCheckStatus.PASS if source_ready else SelfCheckStatus.BLOCKED,
        severity=(
            SelfCheckSeverity.INFO if source_ready else SelfCheckSeverity.REQUIRED
        ),
        consequence=(
            None if source_ready else "SAT cannot create an isolated run workspace."
        ),
        remediation=(
            None if source_ready else "Restore the verified execution-profile seed."
        ),
        rerun_rule="Re-run after the execution-profile seed changes.",
    )


def _evaluate_delivery_boundary(
    destination: Path,
    checked_at: datetime,
) -> SelfCheckResult:
    """Verify that delivery destination remains safe and unwritable by outside changes.

    验证最终交付目标目录仍然保持未被创建、父目录真实且具备可写权限。
    """
    destination_ready = (
        destination.is_absolute()
        and not destination.exists()
        and destination.parent.is_dir()
        and not destination.parent.is_symlink()
        and os.access(destination.parent, os.W_OK | os.X_OK)
    )
    return _result(
        check_id="delivery.boundary",
        checkpoint=SelfCheckCheckpoint.PLAN_EXECUTION,
        category=SelfCheckCategory.DELIVERY,
        owner=SelfCheckOwner.SAT,
        dependencies=("task.destination", "workspace.source"),
        observed_fact=(
            f"delivery boundary remains unused and writable={destination_ready}"
        ),
        evidence_kind="local_observation",
        evidence_reference=str(destination),
        input_value={"path": str(destination), "ready": destination_ready},
        checked_at=checked_at,
        status=(SelfCheckStatus.PASS if destination_ready else SelfCheckStatus.BLOCKED),
        severity=(
            SelfCheckSeverity.INFO if destination_ready else SelfCheckSeverity.REQUIRED
        ),
        consequence=(
            None
            if destination_ready
            else "Delivery would overwrite or escape the approved boundary."
        ),
        remediation=(
            None
            if destination_ready
            else "Choose or restore an unused writable destination."
        ),
        rerun_rule="Re-run immediately before workspace creation and delivery.",
    )


def build_plan_execution_report(
    *,
    admission_report: TaskSelfCheckReport,
    team_plan: TeamPlan,
    runtime_preflight: RuntimePreflight | None,
    runtime_error: str | None = None,
    source_repository: Path,
    destination: Path,
    checked_at: datetime | None = None,
) -> TaskSelfCheckReport:
    """Evaluate approved dynamic authority before Agent or workspace creation.

    在创建运行期 Agent 或工作空间前, 评估已获批准的动态执行计划, 包括:
    - 批准的 TeamPlan 版本及 Agent 绑定
    - 专业化 Prompt 模版完整性 (Specialization Catalog)
    - 运行期 Docker / OpenClaw 预检状态
    - 模型路由元数据完整性与可用性
    - Agent 规格与权限完整性
    - 源码隔离种子目录 (Seed Workspace)
    - 交付目录边界安全性
    """
    if not admission_report.ready:
        raise ValueError("plan execution cannot extend an unready admission report")
    if team_plan.run_id != admission_report.run_id:
        raise ValueError("TeamPlan and admission report use different run IDs")
    if (runtime_preflight is None) == (runtime_error is None):
        raise ValueError(
            "plan execution requires exactly one runtime preflight or error"
        )

    when = (checked_at or _utc_now()).astimezone(UTC)
    checks = list(admission_report.checks)

    checks.append(_evaluate_plan_approval(team_plan, when))
    checks.append(_evaluate_specialization_catalog(team_plan, when))
    checks.append(_evaluate_runtime_plan(runtime_preflight, runtime_error, when))
    checks.extend(_evaluate_model_routes(team_plan, runtime_preflight, when))
    checks.extend(_evaluate_agents(team_plan, when))
    checks.append(_evaluate_workspace_source(source_repository, when))
    checks.append(_evaluate_delivery_boundary(destination, when))

    return TaskSelfCheckReport(
        run_id=admission_report.run_id,
        checkpoint=SelfCheckCheckpoint.PLAN_EXECUTION,
        revision=admission_report.revision + 1,
        previous_report_sha256=admission_report.sha256,
        created_at=when,
        resource_authorization=admission_report.resource_authorization,
        checks=tuple(checks),
    )


__all__ = [
    "PROMPT_TEMPLATE_ROOT",
    "_evaluate_agents",
    "_evaluate_delivery_boundary",
    "_evaluate_model_routes",
    "_evaluate_plan_approval",
    "_evaluate_runtime_plan",
    "_evaluate_specialization_catalog",
    "_evaluate_workspace_source",
    "_get_prompt_template_root",
    "build_plan_execution_report",
]
