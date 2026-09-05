"""Tests for concrete task-admission and approved-plan self-checks."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from software_agent_team.budgets import AgentBudget, BudgetAuthority
from software_agent_team.model_metadata import ModelMetadataSource
from software_agent_team.model_routing import ModelProfile
from software_agent_team.product import (
    DiagnosticCheck,
    DiagnosticState,
    StartupDiagnostics,
)
from software_agent_team.runtime_configuration import (
    OpenClawModelInspection,
    RuntimePreflight,
)
from software_agent_team.schema_compatibility import (
    PersistedSchemaCompatibilityReport,
    supported_schemas,
)
from software_agent_team.self_check import (
    SelfCheckStatus,
    TaskModelMetadata,
    TaskResourceAuthorization,
)
from software_agent_team.self_check_evaluation import (
    build_plan_execution_report,
    build_task_admission_report,
)
from software_agent_team.teams import (
    AgentCapability,
    AgentSpec,
    ModelRoute,
    ModelRoutePlan,
    ModelRoutingMode,
    PermissionProfile,
    PlanApprovalSource,
    TeamPlan,
    TeamPlanOrigin,
)
from software_agent_team.updates import (
    ForegroundUpdateObservation,
    ForegroundUpdateStatus,
)
from software_agent_team.user_configuration import UserConfiguration
from software_agent_team.versioning import (
    IdentityStatus,
    InstallMode,
    SoftwareVersionReport,
)

NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
RUN_ID = "sat-20260905-self-check"
MODEL = "provider/model"


def ready_diagnostics(tmp_path: Path) -> StartupDiagnostics:
    """Return every stable startup check consumed by task admission."""

    values = {
        "platform": ("Linux or WSL", "detected Linux"),
        "architecture": ("Supported architecture", "detected x86_64"),
        "identity": ("Unprivileged user", "uid=1000 gid=1000"),
        "working_directory": ("Writable project parent", str(tmp_path)),
        "command_git": ("git command", "/usr/bin/git"),
        "command_docker": ("docker command", "/usr/bin/docker"),
        "openclaw": ("OpenClaw runtime", "/opt/sat/openclaw"),
        "docker_daemon": ("Docker daemon", "available"),
        "sandbox_image": ("Pinned sandbox image", "sha256:" + "a" * 64),
        "storage": ("Available storage", "2048 MiB free"),
        "launcher": ("sat launcher", "available on PATH"),
    }
    return StartupDiagnostics(
        checks=tuple(
            DiagnosticCheck(
                id=check_id,
                label=label,
                state=DiagnosticState.READY,
                detail=detail,
            )
            for check_id, (label, detail) in values.items()
        )
    )


def ready_configuration() -> UserConfiguration:
    return UserConfiguration(
        model_profiles=(
            ModelProfile(
                id="default",
                model=MODEL,
                capabilities=tuple(AgentCapability),
                input_cost_per_million_usd="1.00",
                output_cost_per_million_usd="2.00",
                pricing_source=ModelMetadataSource.USER_SUPPLIED,
                pricing_observed_at=NOW,
                context_window_tokens=120_000,
                context_source=ModelMetadataSource.USER_SUPPLIED,
                context_observed_at=NOW,
            ),
        )
    )


def resource_authorization() -> TaskResourceAuthorization:
    return TaskResourceAuthorization(
        maximum_estimated_cost_usd="5.00",
        model_metadata=(
            TaskModelMetadata(
                profile_id="default",
                model=MODEL,
                input_cost_per_million_usd="1.00",
                output_cost_per_million_usd="2.00",
                pricing_source=ModelMetadataSource.USER_SUPPLIED,
                context_window_tokens=120_000,
                context_source=ModelMetadataSource.USER_SUPPLIED,
                observed_at=NOW,
            ),
        ),
        authorized_at=NOW,
    )


def version_report(
    status: IdentityStatus = IdentityStatus.VERIFIED,
) -> SoftwareVersionReport:
    return SoftwareVersionReport(
        release_version="0.1.0",
        display_version="0.1.0+gaaaaaaaaaaaa",
        source_revision="a" * 40,
        dirty=False,
        install_mode=InstallMode.SOURCE,
        channel=None,
        source_ref=None,
        repository_url=None,
        application_path="/opt/sat",
        artifact_digest=None,
        installed_at=None,
        identity_status=status,
        provenance_source="git",
        schema_support=supported_schemas(),
        problems=(),
    )


def update_observation(
    status: ForegroundUpdateStatus = ForegroundUpdateStatus.NOT_APPLICABLE,
) -> ForegroundUpdateObservation:
    return ForegroundUpdateObservation(
        status=status,
        current_channel=None,
        current_version="0.1.0",
        current_revision="a" * 40,
        network_attempted=status is not ForegroundUpdateStatus.NOT_APPLICABLE,
        detail=(
            "source installations are not changed by the managed updater"
            if status is ForegroundUpdateStatus.NOT_APPLICABLE
            else "update metadata is unavailable"
        ),
    )


def admission_report(tmp_path: Path):
    state = tmp_path / "state"
    state.mkdir()
    return build_task_admission_report(
        run_id=RUN_ID,
        diagnostics=ready_diagnostics(tmp_path),
        software_version=version_report(),
        schema_compatibility=PersistedSchemaCompatibilityReport(
            compatible=True,
            observations=(),
        ),
        update_observation=update_observation(),
        configuration=ready_configuration(),
        model_inspections=(OpenClawModelInspection(model=MODEL, available=True),),
        source_request="Build a small link checker.",
        destination=tmp_path / "link-checker",
        state_root=state,
        resource_authorization=resource_authorization(),
        checked_at=NOW,
    )


def team_plan() -> TeamPlan:
    route = ModelRoute(
        id="default",
        model=MODEL,
        input_cost_per_million_usd="1.00",
        output_cost_per_million_usd="2.00",
        pricing_source=ModelMetadataSource.USER_SUPPLIED,
        pricing_observed_at=NOW,
        context_window_tokens=120_000,
        context_source=ModelMetadataSource.USER_SUPPLIED,
        context_observed_at=NOW,
    )
    return TeamPlan(
        plan_id="self-check-plan-r1",
        revision=1,
        run_id=RUN_ID,
        task_brief_sha256="b" * 64,
        implementation_plan_sha256="c" * 64,
        team_id="adaptive_team",
        origin=TeamPlanOrigin.ADAPTIVE_PLANNING,
        approval_source=PlanApprovalSource.USER,
        created_at=NOW,
        agents=(
            AgentSpec(
                id="developer",
                label="Developer",
                responsibility="Implement the approved project.",
                rationale="The task needs one coherent write path.",
                capability=AgentCapability.IMPLEMENTATION,
                permission_profile=PermissionProfile.WORKSPACE_WRITE,
                stage_id="implementation",
                expected_output="work_result",
                model_route_id="default",
                timeout_seconds=0,
                workspace_scope="repository",
            ),
            AgentSpec(
                id="tester",
                label="Tester",
                responsibility="Verify the implemented behavior.",
                rationale="Independent evidence is required.",
                capability=AgentCapability.TESTING,
                permission_profile=PermissionProfile.READ_ONLY,
                stage_id="verification",
                dependencies=("developer",),
                expected_output="test_report",
                model_route_id="default",
                timeout_seconds=0,
                workspace_scope="repository",
            ),
        ),
        model_routes=ModelRoutePlan(
            mode=ModelRoutingMode.STRICT,
            default_route_id="default",
            routes=(route,),
        ),
        budget=AgentBudget(
            authority=BudgetAuthority.USER_TASK,
            max_estimated_cost_usd="5.00",
        ),
        iteration_limit=2,
        max_concurrency=2,
        independent_review=True,
        revision_enabled=True,
    )


def runtime_preflight(*, available: bool = True) -> RuntimePreflight:
    return RuntimePreflight(
        openclaw_binary="/opt/sat/openclaw",
        openclaw_version="OpenClaw test",
        openclaw_state_dir="/tmp/state/openclaw",
        runtime_config="/tmp/state/runtime.json",
        sandbox_binary="/usr/bin/docker",
        sandbox_version="Docker test",
        sandbox_image="sha256:" + "a" * 64,
        sandbox_image_id="sha256:" + "a" * 64,
        config_valid=True,
        sandbox_image_present=True,
        sandbox_container_ready=True,
        model=MODEL,
        model_available=available,
        model_error=None if available else "route unavailable",
        model_inspections=(
            OpenClawModelInspection(
                model=MODEL,
                available=available,
                error=None if available else "route unavailable",
            ),
        ),
    )


def test_task_admission_unifies_required_facts_and_allows_nonblocking_warnings(
    tmp_path: Path,
) -> None:
    report = admission_report(tmp_path)

    assert report.ready
    assert report.resource_authorization == resource_authorization()
    assert {item.id for item in report.checks} >= {
        "application.version",
        "application.schema",
        "application.update",
        "system.platform",
        "tool.docker",
        "runtime.sandbox_image",
        "model.default.available",
        "model.default.pricing",
        "model.default.context",
        "task.request",
        "task.destination",
        "budget.authorization",
        "route.planning",
    }

    unavailable = build_task_admission_report(
        **{
            "run_id": RUN_ID,
            "diagnostics": ready_diagnostics(tmp_path),
            "software_version": version_report(),
            "schema_compatibility": PersistedSchemaCompatibilityReport(
                compatible=True,
                observations=(),
            ),
            "update_observation": update_observation(
                ForegroundUpdateStatus.UNAVAILABLE
            ),
            "configuration": ready_configuration(),
            "model_inspections": (
                OpenClawModelInspection(model=MODEL, available=True),
            ),
            "source_request": "Build a small link checker.",
            "destination": tmp_path / "another-link-checker",
            "state_root": tmp_path / "state",
            "resource_authorization": resource_authorization(),
            "checked_at": NOW,
        }
    )
    assert unavailable.ready
    assert (
        next(
            item for item in unavailable.checks if item.id == "application.update"
        ).status
        is SelfCheckStatus.WARNING
    )


def test_task_admission_blocks_incompatible_version_and_missing_authority(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    report = build_task_admission_report(
        run_id=RUN_ID,
        diagnostics=ready_diagnostics(tmp_path),
        software_version=version_report(IdentityStatus.INCONSISTENT),
        schema_compatibility=PersistedSchemaCompatibilityReport(
            compatible=False,
            observations=(),
            problems=("run schema 99 is unsupported",),
        ),
        update_observation=update_observation(),
        configuration=ready_configuration(),
        model_inspections=(OpenClawModelInspection(model=MODEL, available=True),),
        source_request="Build a small link checker.",
        destination=tmp_path / "link-checker",
        state_root=state,
        resource_authorization=None,
        checked_at=NOW,
    )

    assert not report.ready
    statuses = {item.id: item.status for item in report.checks}
    assert statuses["application.version"] is SelfCheckStatus.BLOCKED
    assert statuses["application.schema"] is SelfCheckStatus.BLOCKED
    assert statuses["budget.authorization"] is SelfCheckStatus.NEEDS_INPUT


def test_task_admission_turns_a_missing_required_probe_into_blocking_evidence(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    diagnostics = ready_diagnostics(tmp_path)
    report = build_task_admission_report(
        run_id=RUN_ID,
        diagnostics=StartupDiagnostics(checks=diagnostics.checks[:-1]),
        software_version=version_report(),
        schema_compatibility=PersistedSchemaCompatibilityReport(
            compatible=True,
            observations=(),
        ),
        update_observation=update_observation(),
        configuration=ready_configuration(),
        model_inspections=(OpenClawModelInspection(model=MODEL, available=True),),
        source_request="Build a small link checker.",
        destination=tmp_path / "link-checker",
        state_root=state,
        resource_authorization=resource_authorization(),
        checked_at=NOW,
    )

    launcher = next(item for item in report.checks if item.id == "application.launcher")
    assert launcher.status is SelfCheckStatus.BLOCKED
    assert launcher.remediation is not None
    assert not report.ready


def test_plan_execution_covers_every_route_agent_runtime_and_delivery_boundary(
    tmp_path: Path,
) -> None:
    admission = admission_report(tmp_path)
    source = tmp_path / "source"
    source.mkdir()

    report = build_plan_execution_report(
        admission_report=admission,
        team_plan=team_plan(),
        runtime_preflight=runtime_preflight(),
        source_repository=source,
        destination=tmp_path / "link-checker",
        checked_at=NOW,
    )

    assert report.ready
    assert report.revision == 2
    assert report.previous_report_sha256 == admission.sha256
    assert {item.id for item in report.checks} >= {
        "plan.approval",
        "runtime.plan",
        "route.default",
        "agent.developer",
        "agent.tester",
        "workspace.source",
        "delivery.boundary",
    }

    blocked = build_plan_execution_report(
        admission_report=admission,
        team_plan=team_plan(),
        runtime_preflight=None,
        runtime_error="Docker sandbox probe failed",
        source_repository=source,
        destination=tmp_path / "link-checker",
        checked_at=NOW,
    )
    statuses = {item.id: item.status for item in blocked.checks}
    assert not blocked.ready
    assert statuses["runtime.plan"] is SelfCheckStatus.BLOCKED
    assert statuses["route.default"] is SelfCheckStatus.BLOCKED
