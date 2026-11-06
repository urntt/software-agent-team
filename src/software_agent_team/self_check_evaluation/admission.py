"""Concrete task-admission evaluation logic for SAT readiness checkpoints.

任务准入评估逻辑: 在执行首个大语言模型调用前, 全面审查系统环境、版本身份、
持久化兼容性、可用模型、用户意图及预算授权等前置条件。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from software_agent_team.model_routing import ModelProfile
from software_agent_team.product import (
    DiagnosticState,
    StartupDiagnostics,
)
from software_agent_team.runtime_configuration import (
    OpenClawModelInspection,
)
from software_agent_team.schema_compatibility import (
    PersistedSchemaCompatibilityReport,
)
from software_agent_team.self_check import (
    SelfCheckCategory,
    SelfCheckCheckpoint,
    SelfCheckOwner,
    SelfCheckResult,
    SelfCheckSeverity,
    SelfCheckStatus,
    TaskResourceAuthorization,
    TaskSelfCheckReport,
)
from software_agent_team.updates import (
    ForegroundUpdateObservation,
    ForegroundUpdateStatus,
)
from software_agent_team.user_configuration import UserConfiguration
from software_agent_team.versioning import IdentityStatus, SoftwareVersionReport

from .common import _result, _utc_now

_STARTUP_CHECK_IDS: dict[str, tuple[str, SelfCheckCategory]] = {
    "platform": ("system.platform", SelfCheckCategory.SYSTEM),
    "architecture": ("system.architecture", SelfCheckCategory.SYSTEM),
    "identity": ("system.identity", SelfCheckCategory.SYSTEM),
    "state": ("application.state", SelfCheckCategory.APPLICATION),
    "working_directory": (
        "environment.working_directory",
        SelfCheckCategory.ENVIRONMENT,
    ),
    "command_git": ("tool.git", SelfCheckCategory.TOOL),
    "command_docker": ("tool.docker", SelfCheckCategory.TOOL),
    "openclaw": ("runtime.openclaw", SelfCheckCategory.RUNTIME),
    "docker_daemon": ("runtime.docker", SelfCheckCategory.RUNTIME),
    "sandbox_image": ("runtime.sandbox_image", SelfCheckCategory.RUNTIME),
    "storage": ("system.storage", SelfCheckCategory.SYSTEM),
    "memory_capacity": ("system.memory_capacity", SelfCheckCategory.SYSTEM),
    "pid_capacity": ("system.pid_capacity", SelfCheckCategory.SYSTEM),
    "sat_sandbox_resources": (
        "runtime.sat_sandbox_resources",
        SelfCheckCategory.RUNTIME,
    ),
    "sat_process_resources": (
        "runtime.sat_process_resources",
        SelfCheckCategory.RUNTIME,
    ),
    "launcher": ("application.launcher", SelfCheckCategory.APPLICATION),
}

_STARTUP_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "application.state": ("application.schema",),
    "runtime.docker": ("tool.docker",),
    "runtime.sandbox_image": ("runtime.docker",),
    "runtime.sat_sandbox_resources": ("runtime.docker",),
    "runtime.sat_process_resources": ("application.state",),
}


def _evaluate_version_check(
    software_version: SoftwareVersionReport,
    checked_at: datetime,
) -> SelfCheckResult:
    """Evaluate application software version identity and provenance.

    评估应用程序版本标识与安装源的一致性及可信度。
    """
    version_status = SelfCheckStatus.PASS
    version_severity = SelfCheckSeverity.INFO
    version_consequence = None
    version_remediation = None
    if software_version.identity_status is IdentityStatus.INCONSISTENT:
        version_status = SelfCheckStatus.BLOCKED
        version_severity = SelfCheckSeverity.REQUIRED
        version_consequence = "SAT cannot trust the active application identity."
        version_remediation = "Repair or reinstall SAT before starting the task."
    elif software_version.identity_status is IdentityStatus.PARTIAL:
        version_status = SelfCheckStatus.WARNING
        version_severity = SelfCheckSeverity.WARNING
        version_consequence = "Exact installed-artifact provenance is incomplete."
        version_remediation = (
            "Use a managed installation when immutable release provenance is required."
        )
    version_channel = (
        software_version.channel.value
        if software_version.channel is not None
        else "none"
    )
    version_revision = software_version.source_revision or "unavailable"
    version_artifact = software_version.artifact_digest or "unavailable"
    return _result(
        check_id="application.version",
        checkpoint=SelfCheckCheckpoint.TASK_ADMISSION,
        category=SelfCheckCategory.APPLICATION,
        owner=SelfCheckOwner.SAT,
        observed_fact=(
            f"SAT {software_version.display_version}; "
            f"identity={software_version.identity_status.value}; "
            f"channel={version_channel}; source_revision={version_revision}; "
            f"artifact={version_artifact}"
        ),
        evidence_kind="local_observation",
        evidence_reference="software-version API",
        input_value=software_version.model_dump(mode="json"),
        checked_at=checked_at,
        status=version_status,
        severity=version_severity,
        consequence=version_consequence,
        remediation=version_remediation,
        rerun_rule="Re-run after the active SAT application identity changes.",
    )


def _evaluate_schema_check(
    schema_compatibility: PersistedSchemaCompatibilityReport,
    checked_at: datetime,
) -> SelfCheckResult:
    """Evaluate backward-compatibility of persisted state schemas.

    评估持久化状态的 Schema 兼容性, 确保当前版本能够安全读写状态记录。
    """
    schema_status = (
        SelfCheckStatus.PASS
        if schema_compatibility.compatible
        else SelfCheckStatus.BLOCKED
    )
    return _result(
        check_id="application.schema",
        checkpoint=SelfCheckCheckpoint.TASK_ADMISSION,
        category=SelfCheckCategory.APPLICATION,
        owner=SelfCheckOwner.SAT,
        dependencies=("application.version",),
        observed_fact=(
            f"{len(schema_compatibility.observations)} persisted schema "
            f"observation(s); compatible={schema_compatibility.compatible}"
        ),
        evidence_kind="local_observation",
        evidence_reference="persisted-schema compatibility API",
        input_value=schema_compatibility.model_dump(mode="json"),
        checked_at=checked_at,
        status=schema_status,
        severity=(
            SelfCheckSeverity.INFO
            if schema_compatibility.compatible
            else SelfCheckSeverity.REQUIRED
        ),
        consequence=(
            None
            if schema_compatibility.compatible
            else "Current SAT cannot safely read all persisted task state."
        ),
        remediation=(
            None
            if schema_compatibility.compatible
            else "Use a compatible SAT release or an explicit supported migration."
        ),
        rerun_rule="Re-run after SAT or persisted state schema changes.",
    )


def _evaluate_update_check(
    update_observation: ForegroundUpdateObservation,
    checked_at: datetime,
) -> SelfCheckResult:
    """Evaluate foreground update observation for the active installation.

    评估前台更新检查结果, 确认是否存在关键安全更新或未完成更新。
    """
    update_target = update_observation.target_version or "none"
    update_target_revision = update_observation.target_revision or "none"
    update_status = SelfCheckStatus.PASS
    update_severity = SelfCheckSeverity.INFO
    update_consequence = None
    update_remediation = None
    if update_observation.status is ForegroundUpdateStatus.INCONSISTENT:
        update_status = SelfCheckStatus.BLOCKED
        update_severity = SelfCheckSeverity.REQUIRED
        update_consequence = "The managed installation cannot be updated safely."
        update_remediation = "Repair or reinstall SAT before starting the task."
    elif update_observation.status is ForegroundUpdateStatus.UPDATE_AVAILABLE:
        update_status = SelfCheckStatus.WARNING
        update_severity = SelfCheckSeverity.WARNING
        update_consequence = "The task can continue on the installed release."
        update_remediation = "Run `sat update` to install the newer stable release."
    elif update_observation.status is ForegroundUpdateStatus.UNAVAILABLE:
        update_status = SelfCheckStatus.WARNING
        update_severity = SelfCheckSeverity.WARNING
        update_consequence = (
            "Update availability is unknown; task readiness is unchanged."
        )
        update_remediation = "Check network access or run `sat update --check` later."
    return _result(
        check_id="application.update",
        checkpoint=SelfCheckCheckpoint.TASK_ADMISSION,
        category=SelfCheckCategory.APPLICATION,
        owner=SelfCheckOwner.SAT,
        dependencies=("application.version",),
        observed_fact=(
            f"{update_observation.detail}; target_version={update_target}; "
            f"target_revision={update_target_revision}"
        ),
        evidence_kind=(
            "remote_observation"
            if update_observation.network_attempted
            else "local_observation"
        ),
        evidence_reference="foreground task-admission update check",
        input_value=update_observation.model_dump(mode="json"),
        checked_at=checked_at,
        status=update_status,
        severity=update_severity,
        consequence=update_consequence,
        remediation=update_remediation,
        rerun_rule="Fresh-check once for every new bare-sat task admission.",
    )


def _evaluate_startup_diagnostics(
    diagnostics: StartupDiagnostics,
    checked_at: datetime,
) -> list[SelfCheckResult]:
    """Map startup diagnostics observations to admission self-check results.

    将宿主与运行期启动诊断观测转换为标准的任务准入自检结果。
    """
    diagnostics_by_id = {item.id: item for item in diagnostics.checks}
    if len(diagnostics_by_id) != len(diagnostics.checks):
        raise ValueError("startup diagnostics contain duplicate IDs")
    if unknown_diagnostics := set(diagnostics_by_id) - set(_STARTUP_CHECK_IDS):
        raise ValueError(
            "startup diagnostics are not registered in task self-check: "
            + ", ".join(sorted(unknown_diagnostics))
        )
    checks: list[SelfCheckResult] = []
    for diagnostic_id, (check_id, category) in _STARTUP_CHECK_IDS.items():
        diagnostic = diagnostics_by_id.get(diagnostic_id)
        if diagnostic is None:
            status = SelfCheckStatus.BLOCKED
            severity = SelfCheckSeverity.REQUIRED
            consequence = "Task admission is missing a required local observation."
            remediation = "Repair or reinstall SAT and run task self-check again."
            label = diagnostic_id.replace("_", " ")
            detail = "diagnostic result missing"
            diagnostic_state = "missing"
        elif diagnostic.state is DiagnosticState.READY:
            status = SelfCheckStatus.PASS
            severity = SelfCheckSeverity.INFO
            consequence = None
            remediation = None
            label = diagnostic.label
            detail = diagnostic.detail
            diagnostic_state = diagnostic.state.value
        elif diagnostic.state is DiagnosticState.WARNING:
            status = SelfCheckStatus.WARNING
            severity = SelfCheckSeverity.WARNING
            consequence = (
                "The task can continue, but this host condition may degrade it."
            )
            remediation = diagnostic.action or "Review this host condition."
            label = diagnostic.label
            detail = diagnostic.detail
            diagnostic_state = diagnostic.state.value
        else:
            status = SelfCheckStatus.BLOCKED
            severity = SelfCheckSeverity.REQUIRED
            consequence = "The task cannot safely consume this host prerequisite."
            remediation = diagnostic.action or "Repair this prerequisite."
            label = diagnostic.label
            detail = diagnostic.detail
            diagnostic_state = diagnostic.state.value
        checks.append(
            _result(
                check_id=check_id,
                checkpoint=SelfCheckCheckpoint.TASK_ADMISSION,
                category=category,
                owner=(
                    SelfCheckOwner.SAT
                    if check_id == "application.state"
                    else SelfCheckOwner.HOST
                ),
                dependencies=_STARTUP_DEPENDENCIES.get(check_id, ()),
                observed_fact=f"{label}: {detail}",
                evidence_kind="local_observation",
                evidence_reference=f"startup diagnostic:{diagnostic_id}",
                input_value={
                    "id": diagnostic_id,
                    "state": diagnostic_state,
                    "detail": detail,
                },
                checked_at=checked_at,
                status=status,
                severity=severity,
                consequence=consequence,
                remediation=remediation,
                rerun_rule=(
                    "Re-run when the host, working directory, or runtime changes."
                ),
            )
        )
    return checks


def _evaluate_model_profiles(
    configuration: UserConfiguration,
    model_inspections: tuple[OpenClawModelInspection, ...],
    checked_at: datetime,
) -> tuple[list[SelfCheckResult], list[str]]:
    """Evaluate model availability, pricing, and context limits for all routes.

    审查配置中所有模型路由的本地可用性、单价已知度以及上下文窗口边界。
    """
    inspection_by_model = {item.model: item for item in model_inspections}
    model_check_ids: list[str] = []
    checks: list[SelfCheckResult] = []
    for profile in configuration.model_profiles:
        availability_id = f"model.{profile.id}.available"
        pricing_id = f"model.{profile.id}.pricing"
        context_id = f"model.{profile.id}.context"
        model_check_ids.extend((availability_id, pricing_id, context_id))
        inspection = inspection_by_model.get(profile.model)
        available = inspection is not None and inspection.available
        required = profile.id == configuration.default_model_profile_id
        checks.append(
            _result(
                check_id=availability_id,
                checkpoint=SelfCheckCheckpoint.TASK_ADMISSION,
                category=SelfCheckCategory.MODEL,
                owner=SelfCheckOwner.PROVIDER,
                dependencies=("runtime.openclaw",),
                observed_fact=(
                    f"{profile.model} is "
                    + ("locally configured" if available else "not locally ready")
                ),
                evidence_kind="local_observation",
                evidence_reference=f"OpenClaw model catalog:{profile.model}",
                input_value=(
                    None if inspection is None else inspection.model_dump(mode="json")
                ),
                checked_at=checked_at,
                status=(
                    SelfCheckStatus.PASS
                    if available
                    else SelfCheckStatus.BLOCKED
                    if required
                    else SelfCheckStatus.WARNING
                ),
                severity=(
                    SelfCheckSeverity.INFO
                    if available
                    else SelfCheckSeverity.REQUIRED
                    if required
                    else SelfCheckSeverity.WARNING
                ),
                consequence=(
                    None
                    if available
                    else "Planning cannot start on this route."
                    if required
                    else "A later plan cannot use this optional route until repaired."
                ),
                remediation=(
                    None
                    if available
                    else "Run `sat configure` and repair this model route."
                ),
                rerun_rule=(
                    "Re-run after provider auth, model routing, or OpenClaw changes."
                ),
            )
        )
        prices_known = (
            profile.input_cost_per_million_usd is not None
            and profile.output_cost_per_million_usd is not None
            and profile.pricing_source is not None
            and profile.cache_pricing is not None
        )
        checks.append(
            _result(
                check_id=pricing_id,
                checkpoint=SelfCheckCheckpoint.TASK_ADMISSION,
                category=SelfCheckCategory.MODEL,
                owner=SelfCheckOwner.USER,
                observed_fact=(
                    f"{profile.model} pricing "
                    + (
                        f"known from {profile.pricing_source.value}"
                        if prices_known
                        else "is unknown"
                    )
                ),
                evidence_kind="persisted_record",
                evidence_reference=f"model profile:{profile.id}",
                input_value={
                    "input": str(profile.input_cost_per_million_usd),
                    "output": str(profile.output_cost_per_million_usd),
                    "cache_pricing": (
                        None
                        if profile.cache_pricing is None
                        else profile.cache_pricing.model_dump(mode="json")
                    ),
                    "source": (
                        None
                        if profile.pricing_source is None
                        else profile.pricing_source.value
                    ),
                },
                checked_at=checked_at,
                status=(
                    SelfCheckStatus.PASS
                    if prices_known
                    else SelfCheckStatus.NEEDS_INPUT
                ),
                severity=(
                    SelfCheckSeverity.INFO
                    if prices_known
                    else SelfCheckSeverity.REQUIRED
                ),
                consequence=(
                    None
                    if prices_known
                    else "SAT cannot obtain informed USD authorization for this route."
                ),
                remediation=(
                    None
                    if prices_known
                    else "Supply or explicitly confirm this route's input/output price."
                ),
                rerun_rule="Re-run after model price or source changes.",
            )
        )
        context_known = (
            profile.context_window_tokens is not None
            and profile.context_source is not None
        )
        checks.append(
            _result(
                check_id=context_id,
                checkpoint=SelfCheckCheckpoint.TASK_ADMISSION,
                category=SelfCheckCategory.MODEL,
                owner=SelfCheckOwner.USER,
                observed_fact=(
                    f"{profile.model} context "
                    + (
                        f"{profile.context_window_tokens} tokens from "
                        f"{profile.context_source.value}"
                        if context_known
                        else "is unknown"
                    )
                ),
                evidence_kind="persisted_record",
                evidence_reference=f"model profile:{profile.id}",
                input_value={
                    "tokens": profile.context_window_tokens,
                    "source": (
                        None
                        if profile.context_source is None
                        else profile.context_source.value
                    ),
                },
                checked_at=checked_at,
                status=(
                    SelfCheckStatus.PASS
                    if context_known
                    else SelfCheckStatus.NEEDS_INPUT
                ),
                severity=(
                    SelfCheckSeverity.INFO
                    if context_known
                    else SelfCheckSeverity.REQUIRED
                ),
                consequence=(
                    None
                    if context_known
                    else (
                        "SAT cannot validate prompts against the model context "
                        "boundary."
                    )
                ),
                remediation=(
                    None
                    if context_known
                    else "Supply the provider-documented context-window length."
                ),
                rerun_rule="Re-run after model context metadata changes.",
            )
        )
    return checks, model_check_ids


def _evaluate_request_check(
    source_request: str,
    checked_at: datetime,
) -> SelfCheckResult:
    """Evaluate whether user has provided a non-empty task prompt.

    评估用户提交的任务提示词是否非空有效。
    """
    request_ready = bool(source_request.strip())
    return _result(
        check_id="task.request",
        checkpoint=SelfCheckCheckpoint.TASK_ADMISSION,
        category=SelfCheckCategory.TASK,
        owner=SelfCheckOwner.USER,
        observed_fact=(
            "software request is present"
            if request_ready
            else "software request is missing"
        ),
        evidence_kind="user_authorization",
        evidence_reference="guided task request",
        input_value=source_request,
        checked_at=checked_at,
        status=(SelfCheckStatus.PASS if request_ready else SelfCheckStatus.NEEDS_INPUT),
        severity=(
            SelfCheckSeverity.INFO if request_ready else SelfCheckSeverity.REQUIRED
        ),
        consequence=(
            None if request_ready else "Planning has no user-owned task intent."
        ),
        remediation=(
            None if request_ready else "Describe the software you want to build."
        ),
        rerun_rule="Re-run after the user request changes.",
    )


def _evaluate_destination_check(
    destination: Path,
    checked_at: datetime,
) -> SelfCheckResult:
    """Evaluate delivery destination path safety, writability, and non-existence.

    评估交付目标目录路径的绝对性、未占用性以及父目录的可写权限。
    """
    destination_ready = (
        destination.is_absolute()
        and not destination.exists()
        and destination.parent.is_dir()
        and not destination.parent.is_symlink()
        and os.access(destination.parent, os.W_OK | os.X_OK)
    )
    return _result(
        check_id="task.destination",
        checkpoint=SelfCheckCheckpoint.TASK_ADMISSION,
        category=SelfCheckCategory.DELIVERY,
        owner=SelfCheckOwner.USER,
        dependencies=("environment.working_directory",),
        observed_fact=(
            f"unused writable destination ready={destination_ready}: {destination}"
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
            else "SAT cannot deliver without overwriting existing data."
        ),
        remediation=(
            None
            if destination_ready
            else "Choose an unused name in a writable real directory."
        ),
        rerun_rule=("Re-run after the destination path or parent permissions change."),
    )


def _evaluate_authorization_check(
    resource_authorization: TaskResourceAuthorization | None,
    model_check_ids: tuple[str, ...],
    checked_at: datetime,
) -> SelfCheckResult:
    """Evaluate whether the user provided explicit USD cost limits and deadlines.

    评估用户是否明确授权了任务最高 USD 预算上限与截止时间。
    """
    authorization_ready = resource_authorization is not None
    return _result(
        check_id="budget.authorization",
        checkpoint=SelfCheckCheckpoint.TASK_ADMISSION,
        category=SelfCheckCategory.BUDGET,
        owner=SelfCheckOwner.USER,
        dependencies=model_check_ids,
        observed_fact=(
            "task USD ceiling "
            f"${resource_authorization.maximum_estimated_cost_usd}; "
            f"deadline={resource_authorization.run_deadline_seconds or 'none'}"
            if resource_authorization is not None
            else "task USD ceiling and deadline choice are missing"
        ),
        evidence_kind="user_authorization",
        evidence_reference="task resource authorization",
        input_value=(
            None
            if resource_authorization is None
            else resource_authorization.model_dump(mode="json")
        ),
        checked_at=checked_at,
        status=(
            SelfCheckStatus.PASS if authorization_ready else SelfCheckStatus.NEEDS_INPUT
        ),
        severity=(
            SelfCheckSeverity.INFO
            if authorization_ready
            else SelfCheckSeverity.REQUIRED
        ),
        consequence=(
            None
            if authorization_ready
            else "No model call is authorized for this task."
        ),
        remediation=(
            None
            if authorization_ready
            else "Choose a total USD ceiling and whether to set a deadline."
        ),
        rerun_rule=("Re-run after route metadata or user resource authority changes."),
    )


def _evaluate_planning_route_check(
    default_profile: ModelProfile,
    checked_at: datetime,
) -> SelfCheckResult:
    """Evaluate default model route resolution for task planning.

    评估规划阶段解析所得的默认模型路由完整性与契约一致性。
    """
    return _result(
        check_id="route.planning",
        checkpoint=SelfCheckCheckpoint.TASK_ADMISSION,
        category=SelfCheckCategory.ROUTE,
        owner=SelfCheckOwner.SAT,
        dependencies=(
            f"model.{default_profile.id}.available",
            f"model.{default_profile.id}.pricing",
            f"model.{default_profile.id}.context",
            "budget.authorization",
        ),
        observed_fact=f"Planning route resolves to {default_profile.model}",
        evidence_kind="controller_contract",
        evidence_reference=f"model routing profile:{default_profile.id}",
        input_value=default_profile.model_dump(mode="json"),
        checked_at=checked_at,
        rerun_rule=(
            "Re-run after model routing, metadata, or budget authority changes."
        ),
    )


def build_task_admission_report(
    *,
    run_id: str,
    diagnostics: StartupDiagnostics,
    software_version: SoftwareVersionReport,
    schema_compatibility: PersistedSchemaCompatibilityReport,
    update_observation: ForegroundUpdateObservation,
    configuration: UserConfiguration,
    model_inspections: tuple[OpenClawModelInspection, ...],
    source_request: str,
    destination: Path,
    resource_authorization: TaskResourceAuthorization | None,
    checked_at: datetime | None = None,
) -> TaskSelfCheckReport:
    """Evaluate all facts consumed before the first semantic model call.

    在执行首个大语言模型调用之前, 全面评估任务准入的各项前置条件, 包括:
    - 应用版本与身份完整性
    - 持久化状态 Schema 兼容性
    - 前台更新检查与安全状态
    - 宿主系统与启动诊断检查
    - 模型可用性、单价及上下文限制
    - 任务提示词与交付目录有效性
    - 预算授权与规划路由可用性
    """
    when = (checked_at or _utc_now()).astimezone(UTC)
    checks: list[SelfCheckResult] = []

    checks.append(_evaluate_version_check(software_version, when))
    checks.append(_evaluate_schema_check(schema_compatibility, when))
    checks.append(_evaluate_update_check(update_observation, when))
    checks.extend(_evaluate_startup_diagnostics(diagnostics, when))
    model_checks, model_check_ids = _evaluate_model_profiles(
        configuration=configuration,
        model_inspections=model_inspections,
        checked_at=when,
    )
    checks.extend(model_checks)
    checks.append(_evaluate_request_check(source_request, when))
    checks.append(_evaluate_destination_check(destination, when))
    checks.append(
        _evaluate_authorization_check(
            resource_authorization,
            tuple(model_check_ids),
            when,
        )
    )
    checks.append(
        _evaluate_planning_route_check(configuration.default_model_profile, when)
    )

    return TaskSelfCheckReport(
        run_id=run_id,
        checkpoint=SelfCheckCheckpoint.TASK_ADMISSION,
        revision=1,
        created_at=when,
        resource_authorization=resource_authorization,
        checks=tuple(checks),
    )


__all__ = [
    "_STARTUP_CHECK_IDS",
    "_STARTUP_DEPENDENCIES",
    "_evaluate_authorization_check",
    "_evaluate_destination_check",
    "_evaluate_model_profiles",
    "_evaluate_planning_route_check",
    "_evaluate_request_check",
    "_evaluate_schema_check",
    "_evaluate_startup_diagnostics",
    "_evaluate_update_check",
    "_evaluate_version_check",
    "build_task_admission_report",
]
