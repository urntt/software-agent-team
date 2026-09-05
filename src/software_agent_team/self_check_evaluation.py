"""Concrete evaluators for SAT's two task-readiness checkpoints."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from software_agent_team.integrity import canonical_model_sha256
from software_agent_team.product import (
    DiagnosticState,
    StartupDiagnostics,
)
from software_agent_team.runtime_configuration import (
    OpenClawModelInspection,
    RuntimePreflight,
)
from software_agent_team.schema_compatibility import (
    PersistedSchemaCompatibilityReport,
)
from software_agent_team.self_check import (
    SelfCheckCategory,
    SelfCheckCheckpoint,
    SelfCheckEvidence,
    SelfCheckOwner,
    SelfCheckResult,
    SelfCheckSeverity,
    SelfCheckStatus,
    TaskResourceAuthorization,
    TaskSelfCheckReport,
    observation_sha256,
)
from software_agent_team.teams import TeamPlan
from software_agent_team.updates import (
    ForegroundUpdateObservation,
    ForegroundUpdateStatus,
)
from software_agent_team.user_configuration import UserConfiguration
from software_agent_team.versioning import IdentityStatus, SoftwareVersionReport


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _result(
    *,
    check_id: str,
    checkpoint: SelfCheckCheckpoint,
    category: SelfCheckCategory,
    owner: SelfCheckOwner,
    observed_fact: str,
    evidence_kind: Literal[
        "controller_contract",
        "local_observation",
        "persisted_record",
        "remote_observation",
        "user_authorization",
    ],
    evidence_reference: str,
    input_value: object,
    checked_at: datetime,
    dependencies: tuple[str, ...] = (),
    status: SelfCheckStatus = SelfCheckStatus.PASS,
    severity: SelfCheckSeverity = SelfCheckSeverity.INFO,
    consequence: str | None = None,
    remediation: str | None = None,
    rerun_rule: str,
) -> SelfCheckResult:
    return SelfCheckResult(
        id=check_id,
        checkpoint=checkpoint,
        category=category,
        owner=owner,
        dependencies=dependencies,
        input_sha256=observation_sha256(input_value),
        checked_at=checked_at,
        severity=severity,
        status=status,
        observed_fact=observed_fact,
        evidence=(
            SelfCheckEvidence(
                kind=evidence_kind,
                reference=evidence_reference,
            ),
        ),
        consequence=consequence,
        remediation=remediation,
        rerun_rule=rerun_rule,
    )


_STARTUP_CHECK_IDS = {
    "platform": ("system.platform", SelfCheckCategory.SYSTEM),
    "architecture": ("system.architecture", SelfCheckCategory.SYSTEM),
    "identity": ("system.identity", SelfCheckCategory.SYSTEM),
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
    "launcher": ("application.launcher", SelfCheckCategory.APPLICATION),
}

_STARTUP_DEPENDENCIES = {
    "runtime.docker": ("tool.docker",),
    "runtime.sandbox_image": ("runtime.docker",),
    "runtime.sat_sandbox_resources": ("runtime.docker",),
}


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
    state_root: Path,
    resource_authorization: TaskResourceAuthorization | None,
    checked_at: datetime | None = None,
) -> TaskSelfCheckReport:
    """Evaluate all facts consumed before the first semantic model call."""

    when = (checked_at or _utc_now()).astimezone(UTC)
    checks: list[SelfCheckResult] = []

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
    checks.append(
        _result(
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
            checked_at=when,
            status=version_status,
            severity=version_severity,
            consequence=version_consequence,
            remediation=version_remediation,
            rerun_rule="Re-run after the active SAT application identity changes.",
        )
    )

    schema_status = (
        SelfCheckStatus.PASS
        if schema_compatibility.compatible
        else SelfCheckStatus.BLOCKED
    )
    update_target = update_observation.target_version or "none"
    update_target_revision = update_observation.target_revision or "none"
    checks.append(
        _result(
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
            checked_at=when,
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
    )

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
    checks.append(
        _result(
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
            checked_at=when,
            status=update_status,
            severity=update_severity,
            consequence=update_consequence,
            remediation=update_remediation,
            rerun_rule="Fresh-check once for every new bare-sat task admission.",
        )
    )

    diagnostics_by_id = {item.id: item for item in diagnostics.checks}
    if len(diagnostics_by_id) != len(diagnostics.checks):
        raise ValueError("startup diagnostics contain duplicate IDs")
    if unknown_diagnostics := set(diagnostics_by_id) - set(_STARTUP_CHECK_IDS):
        raise ValueError(
            "startup diagnostics are not registered in task self-check: "
            + ", ".join(sorted(unknown_diagnostics))
        )
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
                owner=SelfCheckOwner.HOST,
                dependencies=_STARTUP_DEPENDENCIES.get(check_id, ()),
                observed_fact=f"{label}: {detail}",
                evidence_kind="local_observation",
                evidence_reference=f"startup diagnostic:{diagnostic_id}",
                input_value={
                    "id": diagnostic_id,
                    "state": diagnostic_state,
                    "detail": detail,
                },
                checked_at=when,
                status=status,
                severity=severity,
                consequence=consequence,
                remediation=remediation,
                rerun_rule=(
                    "Re-run when the host, working directory, or runtime changes."
                ),
            )
        )

    state_owned = (
        state_root.is_absolute()
        and state_root.is_dir()
        and not state_root.is_symlink()
        and os.access(state_root, os.W_OK | os.X_OK)
    )
    checks.append(
        _result(
            check_id="application.state",
            checkpoint=SelfCheckCheckpoint.TASK_ADMISSION,
            category=SelfCheckCategory.APPLICATION,
            owner=SelfCheckOwner.SAT,
            dependencies=("application.schema",),
            observed_fact=f"private SAT state root ready={state_owned}: {state_root}",
            evidence_kind="local_observation",
            evidence_reference=str(state_root),
            input_value={"path": str(state_root), "ready": state_owned},
            checked_at=when,
            status=SelfCheckStatus.PASS if state_owned else SelfCheckStatus.BLOCKED,
            severity=(
                SelfCheckSeverity.INFO if state_owned else SelfCheckSeverity.REQUIRED
            ),
            consequence=(
                None if state_owned else "SAT cannot persist task evidence safely."
            ),
            remediation=(
                None
                if state_owned
                else "Repair the SAT-owned state directory and run sat again."
            ),
            rerun_rule="Re-run when SAT state ownership or permissions change.",
        )
    )

    inspection_by_model = {item.model: item for item in model_inspections}
    model_check_ids: list[str] = []
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
                checked_at=when,
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
                    "source": (
                        None
                        if profile.pricing_source is None
                        else profile.pricing_source.value
                    ),
                },
                checked_at=when,
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
                checked_at=when,
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

    request_ready = bool(source_request.strip())
    checks.append(
        _result(
            check_id="task.request",
            checkpoint=SelfCheckCheckpoint.TASK_ADMISSION,
            category=SelfCheckCategory.TASK,
            owner=SelfCheckOwner.USER,
            observed_fact="software request is present"
            if request_ready
            else "software request is missing",
            evidence_kind="user_authorization",
            evidence_reference="guided task request",
            input_value=source_request,
            checked_at=when,
            status=SelfCheckStatus.PASS
            if request_ready
            else SelfCheckStatus.NEEDS_INPUT,
            severity=SelfCheckSeverity.INFO
            if request_ready
            else SelfCheckSeverity.REQUIRED,
            consequence=None
            if request_ready
            else "Planning has no user-owned task intent.",
            remediation=None
            if request_ready
            else "Describe the software you want to build.",
            rerun_rule="Re-run after the user request changes.",
        )
    )
    destination_ready = (
        destination.is_absolute()
        and not destination.exists()
        and destination.parent.is_dir()
        and not destination.parent.is_symlink()
        and os.access(destination.parent, os.W_OK | os.X_OK)
    )
    checks.append(
        _result(
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
            checked_at=when,
            status=SelfCheckStatus.PASS
            if destination_ready
            else SelfCheckStatus.BLOCKED,
            severity=SelfCheckSeverity.INFO
            if destination_ready
            else SelfCheckSeverity.REQUIRED,
            consequence=None
            if destination_ready
            else "SAT cannot deliver without overwriting existing data.",
            remediation=None
            if destination_ready
            else "Choose an unused name in a writable real directory.",
            rerun_rule=(
                "Re-run after the destination path or parent permissions change."
            ),
        )
    )

    authorization_ready = resource_authorization is not None
    checks.append(
        _result(
            check_id="budget.authorization",
            checkpoint=SelfCheckCheckpoint.TASK_ADMISSION,
            category=SelfCheckCategory.BUDGET,
            owner=SelfCheckOwner.USER,
            dependencies=tuple(model_check_ids),
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
            checked_at=when,
            status=(
                SelfCheckStatus.PASS
                if authorization_ready
                else SelfCheckStatus.NEEDS_INPUT
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
            rerun_rule=(
                "Re-run after route metadata or user resource authority changes."
            ),
        )
    )
    default_profile = configuration.default_model_profile
    checks.append(
        _result(
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
            checked_at=when,
            rerun_rule=(
                "Re-run after model routing, metadata, or budget authority changes."
            ),
        )
    )
    return TaskSelfCheckReport(
        run_id=run_id,
        checkpoint=SelfCheckCheckpoint.TASK_ADMISSION,
        revision=1,
        created_at=when,
        resource_authorization=resource_authorization,
        checks=tuple(checks),
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
    """Evaluate approved dynamic authority before Agent or workspace creation."""

    if not admission_report.ready:
        raise ValueError("plan execution cannot extend an unready admission report")
    if team_plan.run_id != admission_report.run_id:
        raise ValueError("TeamPlan and admission report use different run IDs")
    when = (checked_at or _utc_now()).astimezone(UTC)
    checks = list(admission_report.checks)
    plan_digest = canonical_model_sha256(team_plan)

    checks.append(
        _result(
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
            checked_at=when,
            rerun_rule=(
                "Re-run after plan approval, revision, or task authority changes."
            ),
        )
    )

    if (runtime_preflight is None) == (runtime_error is None):
        raise ValueError(
            "plan execution requires exactly one runtime preflight or error"
        )
    runtime_ready = runtime_preflight is not None and runtime_preflight.ready
    runtime_fact = (
        runtime_error
        if runtime_preflight is None
        else (
            f"config={runtime_preflight.config_valid}; "
            f"image={runtime_preflight.sandbox_image_present}; "
            f"container={runtime_preflight.sandbox_container_ready}"
        )
    )
    checks.append(
        _result(
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
            checked_at=when,
            status=SelfCheckStatus.PASS if runtime_ready else SelfCheckStatus.BLOCKED,
            severity=SelfCheckSeverity.INFO
            if runtime_ready
            else SelfCheckSeverity.REQUIRED,
            consequence=None
            if runtime_ready
            else "Runtime Agents cannot be created safely.",
            remediation=None
            if runtime_ready
            else "Repair the reported runtime, image, or configuration failure.",
            rerun_rule=(
                "Re-run after plan, runtime configuration, Docker, or OpenClaw changes."
            ),
        )
    )

    inspections = {
        item.model: item
        for item in (
            () if runtime_preflight is None else runtime_preflight.model_inspections
        )
    }
    for route in team_plan.model_routes.routes:
        inspection = inspections.get(route.model)
        route_ready = inspection is not None and inspection.available
        metadata_ready = (
            route.input_cost_per_million_usd is not None
            and route.output_cost_per_million_usd is not None
            and route.pricing_source is not None
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
                checked_at=when,
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

    for agent in team_plan.agents:
        checks.append(
            _result(
                check_id=f"agent.{agent.id}",
                checkpoint=SelfCheckCheckpoint.PLAN_EXECUTION,
                category=SelfCheckCategory.AGENT,
                owner=SelfCheckOwner.APPROVED_PLAN,
                dependencies=("plan.approval", f"route.{agent.model_route_id}"),
                observed_fact=(
                    f"{agent.label}: capability={agent.capability.value}; "
                    f"permission={agent.permission_profile.value}; "
                    f"workspace={agent.workspace_scope}; route={agent.model_route_id}"
                ),
                evidence_kind="controller_contract",
                evidence_reference=f"TeamPlan Agent:{agent.id}",
                input_value=agent.model_dump(mode="json"),
                checked_at=when,
                rerun_rule=(
                    "Re-run after Agent, permission, route, or workspace authority "
                    "changes."
                ),
            )
        )

    source_ready = (
        source_repository.is_absolute()
        and source_repository.is_dir()
        and not source_repository.is_symlink()
    )
    checks.append(
        _result(
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
            checked_at=when,
            status=SelfCheckStatus.PASS if source_ready else SelfCheckStatus.BLOCKED,
            severity=SelfCheckSeverity.INFO
            if source_ready
            else SelfCheckSeverity.REQUIRED,
            consequence=None
            if source_ready
            else "SAT cannot create an isolated run workspace.",
            remediation=None
            if source_ready
            else "Restore the verified execution-profile seed.",
            rerun_rule="Re-run after the execution-profile seed changes.",
        )
    )
    destination_ready = (
        destination.is_absolute()
        and not destination.exists()
        and destination.parent.is_dir()
        and not destination.parent.is_symlink()
        and os.access(destination.parent, os.W_OK | os.X_OK)
    )
    checks.append(
        _result(
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
            checked_at=when,
            status=SelfCheckStatus.PASS
            if destination_ready
            else SelfCheckStatus.BLOCKED,
            severity=SelfCheckSeverity.INFO
            if destination_ready
            else SelfCheckSeverity.REQUIRED,
            consequence=None
            if destination_ready
            else "Delivery would overwrite or escape the approved boundary.",
            remediation=None
            if destination_ready
            else "Choose or restore an unused writable destination.",
            rerun_rule="Re-run immediately before workspace creation and delivery.",
        )
    )

    return TaskSelfCheckReport(
        run_id=admission_report.run_id,
        checkpoint=SelfCheckCheckpoint.PLAN_EXECUTION,
        revision=admission_report.revision + 1,
        previous_report_sha256=admission_report.sha256,
        created_at=when,
        resource_authorization=admission_report.resource_authorization,
        checks=tuple(checks),
    )
