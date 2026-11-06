"""CLI execution boundaries and workflow execution runners."""

from __future__ import annotations

import argparse
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from software_agent_team import cli
from software_agent_team.artifacts import (
    CommandEvidence,
    TaskBrief,
)
from software_agent_team.budgets import (
    AgentBudgetLedger,
    ModelPricing,
)
from software_agent_team.cli.commands.configure import (
    _timeout_flag,
)
from software_agent_team.cli.commands.version import (
    _load_json_model,
)
from software_agent_team.cli.constants import (
    DEFAULT_OPENCLAW_CONFIG,
    DEFAULT_TEAM_CONFIG,
    EVALUATION_ITERATION_LIMIT,
)
from software_agent_team.controls import ControlCommandStore
from software_agent_team.dynamic_workflow import (
    DynamicWorkflowOutcome,
)
from software_agent_team.execution import OpenClawSubprocessExecutor
from software_agent_team.git_workspace import GitWorkspace
from software_agent_team.model_costs import CachePricing
from software_agent_team.model_metadata import ModelMetadataSource
from software_agent_team.model_runtime import (
    ModelRuntimeProfile,
    runtime_profile_for_model,
)
from software_agent_team.openclaw_runtime import isolated_openclaw_environment
from software_agent_team.paths import user_state_root
from software_agent_team.planning import ApprovedPlanningResult
from software_agent_team.process_lifecycle import ProcessLeaseStore
from software_agent_team.product import ProductStatePaths, ensure_product_state
from software_agent_team.progress import (
    ProgressDraftHandler,
    ProgressEvent,
    ProgressEventKind,
    ProgressHandler,
)
from software_agent_team.quality_gates import (
    DockerSandboxBackend,
    QualityGateConfiguration,
    QualityGateRunner,
    bind_run_task_brief_mount,
    load_quality_gate_configuration,
)
from software_agent_team.run_control import RunPhase
from software_agent_team.runtime_configuration import (
    OpenClawModelInspection,
    RuntimeConfigurationError,
    RuntimePreflight,
    persist_runtime_preflight,
)
from software_agent_team.teams import (
    TeamManifest,
    TeamPlan,
    load_team_manifest,
)
from software_agent_team.versioning import SoftwareVersionReport
from software_agent_team.workflow import (
    WorkflowOutcome,
)
from software_agent_team.workspace_mounts import (
    prepare_sandbox_skill_mountpoint,
)


@dataclass(frozen=True)
class _RuntimeLaunchOptions:
    """Resolved machine and provider inputs shared by runtime launchers."""

    source_repository: Path
    base_ref: str
    teams: Path
    openclaw: Path
    policy: Path
    quality_manifest: Path
    runs_root: Path
    workspaces_root: Path
    openclaw_binary: Path
    openclaw_state_dir: Path
    process_leases_root: Path
    sandbox_binary: str
    model: str
    input_cost_per_million_usd: Decimal | None
    output_cost_per_million_usd: Decimal | None
    cache_pricing: CachePricing | None = field(default=None, kw_only=True)
    model_runtime_profile: ModelRuntimeProfile | None = field(
        default=None,
        kw_only=True,
    )


@dataclass(frozen=True)
class _WorkflowLaunchOptions(_RuntimeLaunchOptions):
    """Compatibility-only options for a fixed evaluation workflow."""

    team_id: str
    stage_timeout_seconds: int | None
    artifact_repair_limit: int
    iteration_limit: int
    verification_concurrency: int
    progress_handler: ProgressHandler | None = None


@dataclass(frozen=True)
class _AdaptiveWorkflowLaunchOptions(_RuntimeLaunchOptions):
    """Resolved inputs for one user-approved dynamic workflow."""

    artifact_repair_limit: int | None = None
    progress_handler: ProgressHandler | None = None
    budget_ledger: AgentBudgetLedger | None = None
    run_deadline_at: datetime | None = None


@dataclass(frozen=True)
class _RuntimeBoundary:
    """One frozen sandbox, runtime setup callback, and quality-gate factory."""

    configuration: QualityGateConfiguration
    manifest: TeamManifest
    executor: OpenClawSubprocessExecutor
    runtime_setup: Callable[[GitWorkspace, Path], None]
    quality_gate_factory: Callable[
        [Path, Path, ProgressDraftHandler],
        QualityGateRunner,
    ]


def _with_team_plan_model_inspections(
    preflight: RuntimePreflight,
    *,
    team_plan: TeamPlan,
    openclaw_binary: Path,
    openclaw_state_dir: Path,
    runtime_config: Path,
) -> RuntimePreflight:
    """Attach one local catalog/auth observation for every approved route."""

    inspections: list[OpenClawModelInspection] = []
    primary_inspection = next(
        (item for item in preflight.model_inspections if item.model == preflight.model),
        None,
    )
    for route in team_plan.model_routes.routes:
        if route.model == preflight.model and primary_inspection is not None:
            inspections.append(primary_inspection)
        else:
            inspections.append(
                cli.inspect_openclaw_model(
                    openclaw_binary=openclaw_binary,
                    openclaw_state_dir=openclaw_state_dir,
                    config_path=runtime_config,
                    model=route.model,
                    runtime_profile=route.runtime_profile,
                )
            )
    return RuntimePreflight.model_validate(
        {
            **preflight.model_dump(mode="json"),
            "model_inspections": tuple(inspections),
        }
    )


def _inspect_approved_plan_runtime(
    *,
    team_plan: TeamPlan,
    source_repository: Path,
    state_paths: ProductStatePaths,
    quality: QualityGateConfiguration,
) -> RuntimePreflight:
    """Validate approved routes and policies before creating a run workspace."""

    manifest = load_team_manifest(DEFAULT_TEAM_CONFIG)
    sandbox = cli.inspect_sandbox_image(
        sandbox_binary="docker",
        sandbox_image=quality.policy.sandbox.image,
    )
    if not sandbox.ready or sandbox.sandbox_image_id is None:
        raise RuntimeConfigurationError(
            "approved-plan self-check cannot resolve the pinned sandbox image"
        )
    descriptor, raw_path = tempfile.mkstemp(
        prefix=f".plan-self-check-{team_plan.run_id}-",
        suffix=".json",
        dir=state_paths.root,
    )
    os.close(descriptor)
    runtime_path = Path(raw_path)
    runtime_path.unlink()
    try:
        limits = quality.policy.limits
        cli.materialize_run_configuration(
            cli.DEFAULT_OPENCLAW_CONFIG,
            runtime_path,
            manifest=manifest,
            workspace=source_repository,
            sandbox_image=sandbox.sandbox_image_id,
            sandbox_memory_mb=limits.memory_mb,
            sandbox_cpus=limits.cpu_cores,
            sandbox_pids_limit=limits.pids,
            sandbox_open_files=limits.open_files,
            sandbox_tmpfs_mb=limits.writable_tmpfs_mb,
            team_plan=team_plan,
        )
        default_model = team_plan.model_routes.get_route(
            team_plan.model_routes.default_route_id
        ).model
        preflight = cli.inspect_runtime_preflight(
            openclaw_binary=cli.DEFAULT_OPENCLAW_BINARY,
            openclaw_state_dir=state_paths.openclaw,
            runtime_config=runtime_path,
            sandbox_binary="docker",
            sandbox_image=quality.policy.sandbox.image,
            expected_sandbox_image_id=sandbox.sandbox_image_id,
            expected_model=default_model,
            expected_runtime_profile=team_plan.model_routes.get_route(
                team_plan.model_routes.default_route_id
            ).runtime_profile,
        )
        return _with_team_plan_model_inspections(
            preflight,
            team_plan=team_plan,
            openclaw_binary=cli.DEFAULT_OPENCLAW_BINARY,
            openclaw_state_dir=state_paths.openclaw,
            runtime_config=runtime_path,
        )
    finally:
        runtime_path.unlink(missing_ok=True)


def _prepare_runtime_boundary(
    *,
    run_id: str,
    options: _RuntimeLaunchOptions,
    team_plan: TeamPlan | None = None,
) -> _RuntimeBoundary:
    """Freeze one runtime image and build shared execution callbacks."""

    manifest = load_team_manifest(options.teams)
    configuration = load_quality_gate_configuration(
        options.policy,
        options.quality_manifest,
    )
    sandbox_inspection = cli.inspect_sandbox_image(
        sandbox_binary=options.sandbox_binary,
        sandbox_image=configuration.policy.sandbox.image,
    )
    if not sandbox_inspection.ready or sandbox_inspection.sandbox_image_id is None:
        raise RuntimeConfigurationError(
            "the configured sandbox image is not present locally"
        )
    frozen_sandbox_image = sandbox_inspection.sandbox_image_id
    runtime_path = options.runs_root / run_id / "openclaw.runtime.json"
    openclaw_environment = isolated_openclaw_environment(
        state_dir=options.openclaw_state_dir,
        config_path=runtime_path,
    )
    executor = OpenClawSubprocessExecutor(
        openclaw_binary=options.openclaw_binary,
        environment=openclaw_environment,
        local=True,
        run_deadline_at=getattr(options, "run_deadline_at", None),
        process_lease_store=ProcessLeaseStore(options.process_leases_root),
    )

    def runtime_setup(workspace: GitWorkspace, run_directory: Path) -> None:
        workspace_path = workspace.workspace_path
        limits = configuration.policy.limits
        prepare_sandbox_skill_mountpoint(Path(workspace_path))
        cli.materialize_run_configuration(
            options.openclaw,
            runtime_path,
            manifest=manifest,
            workspace=Path(workspace_path),
            sandbox_image=frozen_sandbox_image,
            sandbox_memory_mb=limits.memory_mb,
            sandbox_cpus=limits.cpu_cores,
            sandbox_pids_limit=limits.pids,
            sandbox_open_files=limits.open_files,
            sandbox_tmpfs_mb=limits.writable_tmpfs_mb,
            model=options.model if team_plan is None else None,
            model_runtime_profile=(
                options.model_runtime_profile if team_plan is None else None
            ),
            team_plan=team_plan,
        )
        preflight = cli.inspect_runtime_preflight(
            openclaw_binary=options.openclaw_binary,
            openclaw_state_dir=options.openclaw_state_dir,
            runtime_config=runtime_path,
            sandbox_binary=options.sandbox_binary,
            sandbox_image=configuration.policy.sandbox.image,
            expected_sandbox_image_id=frozen_sandbox_image,
            expected_model=(
                options.model
                if team_plan is None
                else team_plan.model_routes.get_route(
                    team_plan.model_routes.default_route_id
                ).model
            ),
            expected_runtime_profile=(
                options.model_runtime_profile
                if team_plan is None
                else team_plan.model_routes.get_route(
                    team_plan.model_routes.default_route_id
                ).runtime_profile
            ),
        )
        if team_plan is not None:
            preflight = _with_team_plan_model_inspections(
                preflight,
                team_plan=team_plan,
                openclaw_binary=options.openclaw_binary,
                openclaw_state_dir=options.openclaw_state_dir,
                runtime_config=runtime_path,
            )
        liveness_inspections = preflight.model_inspections
        if not liveness_inspections and preflight.model is not None:
            liveness_inspections = (
                OpenClawModelInspection(
                    model=preflight.model,
                    available=preflight.model_available is True,
                    error=preflight.model_error,
                    local=preflight.model_local,
                    provider_request_timeout_seconds=(
                        preflight.model_request_timeout_seconds
                    ),
                ),
            )
        for model_inspection in liveness_inspections:
            executor.register_model_liveness(
                model=model_inspection.model,
                local=model_inspection.local,
                provider_request_timeout_seconds=(
                    model_inspection.provider_request_timeout_seconds
                ),
            )
        persist_runtime_preflight(
            preflight,
            run_directory / "runtime-preflight.json",
        )
        if not preflight.ready:
            raise RuntimeConfigurationError(
                "runtime preflight failed: "
                f"config_valid={preflight.config_valid}, "
                f"sandbox_image_present={preflight.sandbox_image_present}, "
                f"sandbox_container_ready={preflight.sandbox_container_ready}, "
                "sandbox_container_error="
                f"{preflight.sandbox_container_error or 'none'}, "
                f"model={preflight.model or 'none'}, "
                f"model_available={preflight.model_available}, "
                f"model_error={preflight.model_error or 'none'}, "
                "unavailable_routes="
                + (
                    ",".join(
                        inspection.model
                        for inspection in preflight.model_inspections
                        if not inspection.available
                    )
                    or "none"
                )
            )

    def gate_factory(
        run_directory: Path,
        workspace: Path,
        event_handler: ProgressDraftHandler,
    ) -> QualityGateRunner:
        def report_gate(
            command: CommandEvidence,
            iteration: int,
            completed: int,
            total: int,
        ) -> None:
            passed = command.exit_code == 0 and not command.timed_out
            state = "passed" if passed else "failed"
            event_handler(
                ProgressEvent(
                    kind=(
                        ProgressEventKind.QUALITY_GATE_PASSED
                        if passed
                        else ProgressEventKind.QUALITY_GATE_FAILED
                    ),
                    message=(f"Quality gate {completed}/{total} {command.id}: {state}"),
                    phase=RunPhase.VERIFYING,
                    iteration=iteration,
                    completed=completed,
                    total=total,
                )
            )

        gate_configuration = (
            bind_run_task_brief_mount(
                configuration,
                run_directory=run_directory,
                expected_run_id=run_id,
                expected_sha256=team_plan.task_brief_sha256,
            )
            if team_plan is not None
            else configuration
        )
        return QualityGateRunner(
            gate_configuration,
            run_directory=run_directory,
            workspace=workspace,
            sandbox_image_id=frozen_sandbox_image,
            backend=DockerSandboxBackend(options.sandbox_binary),
            result_handler=report_gate,
        )

    return _RuntimeBoundary(
        configuration=configuration,
        manifest=manifest,
        executor=executor,
        runtime_setup=runtime_setup,
        quality_gate_factory=gate_factory,
    )


def _execute_workflow(
    task_brief: TaskBrief,
    options: _WorkflowLaunchOptions,
    *,
    software_version: SoftwareVersionReport,
) -> WorkflowOutcome:
    """Execute one fixed evaluation brief through the compatibility engine."""

    boundary = cli._prepare_runtime_boundary(
        run_id=task_brief.run_id,
        options=options,
    )
    configuration = boundary.configuration
    manifest = boundary.manifest

    coordinator = cli.WorkflowCoordinator(
        manifest=manifest,
        runs_root=options.runs_root,
        workspaces_root=options.workspaces_root,
        executor=boundary.executor,
        quality_gate_factory=boundary.quality_gate_factory,
        budget=configuration.policy.evaluation_budget,
        pricing=ModelPricing(
            model=options.model,
            input_cost_per_million_usd=options.input_cost_per_million_usd,
            output_cost_per_million_usd=options.output_cost_per_million_usd,
            cache_pricing=options.cache_pricing,
        ),
        software_version=software_version,
        runtime_setup=boundary.runtime_setup,
        manual_review_criteria=configuration.manifest.manual_review_criteria,
        role_timeout_seconds=configuration.policy.evaluation_timeouts,
        stage_timeout_seconds=options.stage_timeout_seconds,
        artifact_repair_limit=options.artifact_repair_limit,
        iteration_limit=options.iteration_limit,
        verification_concurrency=options.verification_concurrency,
        team_id=options.team_id,
        progress_handler=options.progress_handler,
    )
    cleanup_roles = tuple(manifest.get_team(options.team_id).roles)
    try:
        outcome = coordinator.execute(
            task_brief,
            source_repository=options.source_repository,
            base_ref=options.base_ref,
        )
    except BaseException as error:
        boundary.executor.interrupt_all()
        try:
            cli.cleanup_run_sandbox_containers(
                sandbox_binary=options.sandbox_binary,
                run_id=task_brief.run_id,
                openclaw_state_dir=options.openclaw_state_dir,
                workspace_dir=(options.workspaces_root / task_brief.run_id).resolve(
                    strict=False
                ),
                iteration_limit=options.iteration_limit,
                roles=cleanup_roles,
            )
        except Exception as cleanup_error:
            error.add_note(f"run-scoped sandbox cleanup also failed: {cleanup_error}")
        raise

    cleanup = cli.cleanup_run_sandbox_containers(
        sandbox_binary=options.sandbox_binary,
        run_id=task_brief.run_id,
        openclaw_state_dir=options.openclaw_state_dir,
        workspace_dir=(options.workspaces_root / task_brief.run_id).resolve(
            strict=False
        ),
        iteration_limit=options.iteration_limit,
        roles=cleanup_roles,
    )
    print(
        "runtime cleanup: "
        f"removed {len(cleanup.removed)} run-scoped Agent sandbox container(s)"
    )
    return outcome


def _execute_dynamic_workflow(
    approved: ApprovedPlanningResult,
    options: _AdaptiveWorkflowLaunchOptions,
    *,
    software_version: SoftwareVersionReport,
    control_store_handler: (
        Callable[[ControlCommandStore, TeamPlan], Callable[[], None] | None] | None
    ) = None,
) -> DynamicWorkflowOutcome:
    """Execute exactly one user-approved adaptive plan and clean its sandboxes."""

    team_plan = approved.team_plan
    boundary = cli._prepare_runtime_boundary(
        run_id=approved.task_brief.run_id,
        options=options,
        team_plan=team_plan,
    )
    pricing_by_model = {
        route.model: ModelPricing(
            model=route.model,
            input_cost_per_million_usd=route.input_cost_per_million_usd,
            output_cost_per_million_usd=route.output_cost_per_million_usd,
            pricing_source=route.pricing_source,
            pricing_observed_at=route.pricing_observed_at,
            cache_pricing=route.cache_pricing,
            max_input_tokens=route.context_window_tokens,
            max_output_tokens=(
                route.runtime_profile.invocation_max_tokens
                or route.runtime_profile.max_output_tokens
                or route.context_window_tokens
            ),
        )
        for route in team_plan.model_routes.routes
    }
    manual_review_criteria = tuple(
        criterion.id for criterion in approved.task_brief.acceptance_criteria
    )
    coordinator = cli.DynamicWorkflowCoordinator(
        runs_root=options.runs_root,
        workspaces_root=options.workspaces_root,
        executor=boundary.executor,
        quality_gate_factory=boundary.quality_gate_factory,
        pricing_by_model=pricing_by_model,
        software_version=software_version,
        budget_ledger=options.budget_ledger,
        runtime_setup=boundary.runtime_setup,
        manual_review_criteria=manual_review_criteria,
        artifact_repair_limit=options.artifact_repair_limit,
        progress_handler=options.progress_handler,
        control_store_handler=control_store_handler,
    )

    cleanup_arguments = {
        "sandbox_binary": options.sandbox_binary,
        "run_id": approved.task_brief.run_id,
        "openclaw_state_dir": options.openclaw_state_dir,
        "workspace_dir": (options.workspaces_root / approved.task_brief.run_id).resolve(
            strict=False
        ),
        "iteration_limit": team_plan.iteration_limit,
        "agents": team_plan.agents,
    }
    try:
        outcome = coordinator.execute(
            approved,
            source_repository=options.source_repository,
            base_ref=options.base_ref,
        )
    except BaseException as error:
        boundary.executor.interrupt_all()
        try:
            cli.cleanup_run_sandbox_containers(**cleanup_arguments)
        except Exception as cleanup_error:
            error.add_note(f"run-scoped sandbox cleanup also failed: {cleanup_error}")
        raise

    cleanup = cli.cleanup_run_sandbox_containers(**cleanup_arguments)
    print(
        "runtime cleanup: "
        f"removed {len(cleanup.removed)} run-scoped Agent sandbox container(s)"
    )
    return outcome


def _run_workflow(args: argparse.Namespace) -> int:
    state_paths = ProductStatePaths.below(user_state_root())
    ensure_product_state(state_paths)
    timeout_supplied, supplied_timeout = _timeout_flag(args)
    user_configuration = None
    if (
        any(
            value is None
            for value in (
                args.model,
                args.input_cost_per_million_usd,
                args.output_cost_per_million_usd,
                args.verification_concurrency,
            )
        )
        or not timeout_supplied
    ):
        user_configuration = cli._load_user_configuration()

    model = (
        args.model
        if args.model is not None
        else user_configuration.model
        if user_configuration is not None
        else None
    )
    input_cost = (
        args.input_cost_per_million_usd
        if args.input_cost_per_million_usd is not None
        else user_configuration.input_cost_per_million_usd
        if user_configuration is not None and user_configuration.model == model
        else None
    )
    output_cost = (
        args.output_cost_per_million_usd
        if args.output_cost_per_million_usd is not None
        else user_configuration.output_cost_per_million_usd
        if user_configuration is not None and user_configuration.model == model
        else None
    )
    cache_pricing = (
        user_configuration.default_model_profile.cache_pricing
        if user_configuration is not None and user_configuration.model == model
        else None
    )
    model_runtime_profile = (
        user_configuration.default_model_profile.runtime_profile
        if user_configuration is not None and user_configuration.model == model
        else runtime_profile_for_model(model)
        if model is not None
        else None
    )
    cache_read = args.cache_read_cost_per_million_usd
    cache_write = args.cache_write_cost_per_million_usd
    if (cache_read is None) != (cache_write is None):
        raise ValueError("cache read and write prices must be configured together")
    if cache_read is not None:
        cache_pricing = CachePricing(
            read_cost_per_million_usd=cache_read,
            write_cost_per_million_usd=cache_write,
            source=(
                ModelMetadataSource.CONFIRMED_ZERO
                if cache_read == cache_write == 0
                else ModelMetadataSource.USER_SUPPLIED
            ),
            observed_at=datetime.now(UTC),
        )
    timeout = supplied_timeout if timeout_supplied else None
    concurrency = (
        args.verification_concurrency
        if args.verification_concurrency is not None
        else min(user_configuration.max_concurrency, 2)
        if user_configuration is not None
        else 2
    )
    if args.team == "single_agent":
        if args.verification_concurrency not in (None, 1):
            raise ValueError("single_agent permits only concurrency one")
        concurrency = 1
    missing = [
        name
        for name, value in (
            ("model", model),
            ("input token price", input_cost),
            ("output token price", output_cost),
        )
        if value is None
    ]
    if missing:
        raise ValueError(
            "run defaults are not configured for "
            + ", ".join(missing)
            + "; run 'sat configure' or supply the equivalent flags"
        )

    task_brief = _load_json_model(args.task_brief, TaskBrief)
    configuration = load_quality_gate_configuration(args.policy, args.benchmark)
    if args.team == "single_agent" and configuration.manifest.manual_review_criteria:
        raise ValueError(
            "single_agent requires a benchmark with deterministic-only "
            "acceptance; use a comparison benchmark"
        )
    expected_brief = configuration.task_brief.model_copy(
        update={"run_id": task_brief.run_id}
    )
    if task_brief != expected_brief:
        raise ValueError(
            "Phase 1 run permits only run_id to differ from the frozen TaskBrief"
        )
    outcome = cli._execute_workflow(
        task_brief,
        _WorkflowLaunchOptions(
            source_repository=args.source_repository,
            base_ref=args.base_ref,
            teams=args.teams,
            openclaw=DEFAULT_OPENCLAW_CONFIG,
            policy=args.policy,
            quality_manifest=args.benchmark,
            runs_root=args.runs_root,
            workspaces_root=args.workspaces_root,
            openclaw_binary=cli.DEFAULT_OPENCLAW_BINARY,
            openclaw_state_dir=state_paths.openclaw,
            process_leases_root=state_paths.process_leases,
            sandbox_binary=args.sandbox_binary,
            model=model,
            input_cost_per_million_usd=input_cost,
            output_cost_per_million_usd=output_cost,
            model_runtime_profile=model_runtime_profile,
            stage_timeout_seconds=timeout,
            cache_pricing=cache_pricing,
            artifact_repair_limit=args.artifact_repair_limit,
            iteration_limit=(
                1 if args.team == "single_agent" else EVALUATION_ITERATION_LIMIT
            ),
            verification_concurrency=concurrency,
            team_id=args.team,
        ),
        software_version=cli._software_version_report(),
    )
    print(
        f"run {outcome.record.phase.value}: run={outcome.record.run_id} "
        f"commit={outcome.record.current_commit or 'none'} "
        f"report={args.runs_root / outcome.record.run_id / outcome.human_report_path}"
    )
    return 0 if outcome.record.phase is RunPhase.COMPLETED else 2
