"""Interactive product delivery workflow for SAT.

Guides the user through:
1. Startup diagnostics & configuration check/repair
2. Source request collection & task resource authorization
3. Task self-checks & pre-planning admissions
4. Interactive model-backed planning
5. Plan execution checkpoint & dynamic workflow execution
6. Final workspace delivery & run report presentation
"""

from __future__ import annotations

import hashlib
import json
import shlex
import shutil
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from software_agent_team import cli
from software_agent_team.artifacts import (
    AgentRole,
    CheckStatus,
    FinalReport,
)
from software_agent_team.budgets import (
    AgentBudget,
    AgentBudgetLedger,
    BudgetAuthority,
    ModelPricing,
)
from software_agent_team.control_console import TerminalControlConsole
from software_agent_team.controls import ControlCommandStore
from software_agent_team.dynamic_workflow import DynamicWorkflowOutcome
from software_agent_team.execution import OpenClawSubprocessExecutor
from software_agent_team.git_workspace import (
    GitWorkspaceError,
    GitWorkspaceManager,
)
from software_agent_team.model_routing import resolve_bootstrap_model_profiles
from software_agent_team.openclaw_runtime import isolated_openclaw_environment
from software_agent_team.paths import user_state_root
from software_agent_team.planning import (
    AdaptivePlanningCoordinator,
    ApprovedPlanningResult,
    CapabilityTimeoutPolicy,
    PlanningPolicy,
    PlanningRequest,
    PlanningRouteRuntime,
    PlanningStore,
)
from software_agent_team.process_lifecycle import ProcessLeaseStore
from software_agent_team.product import (
    ProductFlowError,
    ProductStatePaths,
    StartupDiagnostics,
    ensure_product_state,
    generate_product_run_id,
    limit_concurrency_to_host_capacity,
    validate_project_destination,
)
from software_agent_team.progress import (
    RunEventVisibility,
    TerminalColorMode,
    TerminalProgressDisplay,
    TerminalProgressRenderer,
    terminal_color_enabled,
)
from software_agent_team.quality_gates import (
    QualityGateConfiguration,
    load_quality_gate_configuration,
)
from software_agent_team.run_control import RunPhase
from software_agent_team.runtime_configuration import (
    MODEL_INSPECTION_TIMEOUT_SECONDS,
    RuntimeConfigurationError,
    RuntimePreflight,
)
from software_agent_team.runtime_controls import RuntimeControlDecision
from software_agent_team.schema_compatibility import (
    PersistedSchemaCompatibilityReport,
    SchemaCompatibilityError,
    inspect_persisted_schema_compatibility,
)
from software_agent_team.self_check import (
    TaskSelfCheckReport,
    TaskSelfCheckStore,
    render_self_check_report,
)
from software_agent_team.self_check_evaluation import (
    build_plan_execution_report,
    build_task_admission_report,
)
from software_agent_team.teams import (
    AgentCapability,
    TeamPlan,
    load_team_manifest,
)
from software_agent_team.terminal_metrics import TerminalRunMetrics
from software_agent_team.updates import (
    ForegroundUpdateObservation,
)
from software_agent_team.user_configuration import (
    UserConfiguration,
    user_configuration_path,
)
from software_agent_team.versioning import installation_record_path
from software_agent_team.workflow import WorkflowOutcome

from .commands.configure import (
    _commit_configuration_transaction,
    _complete_model_metadata,
    _staged_openclaw_state,
)
from .commands.version import (
    SoftwareVersionReport,
)
from .constants import (
    DEFAULT_OPENCLAW_BINARY,
    DEFAULT_OPENCLAW_CONFIG,
    DEFAULT_PRODUCT_POLICY,
    DEFAULT_PRODUCT_PROFILE,
    DEFAULT_PRODUCT_SEED,
    DEFAULT_TEAM_CONFIG,
)
from .model_inspection import (
    OpenClawModelInspection,
    _bind_private_runtime_profiles,
)
from .prompts import (
    TaskResourceAuthorization,
)
from .workflow import (
    _AdaptiveWorkflowLaunchOptions,
)


def _ensure_product_configuration(
    state_paths: ProductStatePaths,
) -> tuple[UserConfiguration, tuple[OpenClawModelInspection, ...]]:
    """Load or guide the first secret-free product configuration."""

    path = user_configuration_path()
    current = cli._load_user_configuration(path)
    openclaw_config = state_paths.openclaw / "openclaw.json"
    cli._validate_optional_openclaw_configuration(openclaw_config)
    if current is not None:
        current = _bind_private_runtime_profiles(
            current,
            config_path=openclaw_config,
        )
        try:
            inspected = cli._inspect_model_configuration(
                current,
                state_dir=state_paths.openclaw,
                config_path=openclaw_config,
            )
        except RuntimeConfigurationError as error:
            print("! Saved model configuration is not locally ready:")
            print(f"  {error}")
        else:
            completed = _complete_model_metadata(
                current,
                inspected,
                offer_price_change=False,
            )
            if completed != current:
                cli.save_user_configuration(completed, path)
                current = completed
            print(f"✓ Bootstrap model: {current.default_model_profile.model}")
            if len(inspected) > 1:
                print(f"✓ Additional model profiles: {len(inspected) - 1} ready")
            for profile in current.model_profiles:
                print(f"  {profile.id}: {profile.model}")
            print(f"✓ Isolated OpenClaw state: {state_paths.openclaw}")
            return current, tuple(inspected)

    print(
        "\nModel configuration repair"
        if current is not None
        else "\nFirst-run model setup"
    )
    print("SAT uses an isolated OpenClaw runtime and private provider state.")
    print(
        "Existing OpenClaw installations, credentials, and configuration "
        "stay untouched."
    )
    if current is not None:
        print(f"Saved SAT model reference: {current.model}")
    with _staged_openclaw_state(state_paths.openclaw) as (
        candidate_state,
        candidate_config,
    ):
        provider_setup_changed = cli._prompt_yes_no(
            "Open SAT's isolated OpenClaw provider setup now? "
            "Choose no only when credentials come from the current shell environment.",
            default=True,
        )
        if provider_setup_changed:
            cli._run_openclaw_configuration(
                cli.DEFAULT_OPENCLAW_BINARY,
                state_dir=candidate_state,
                config_path=candidate_config,
            )
        discovered_model = cli._discover_openclaw_default_model(
            cli.DEFAULT_OPENCLAW_BINARY,
            state_dir=candidate_state,
            config_path=candidate_config,
        )
        if discovered_model is not None:
            print(f"✓ OpenClaw default model detected: {discovered_model}")
        default_model = current.model if current is not None else discovered_model
        model = cli._prompt_value(
            "OpenClaw model reference (provider/model)",
            default_model,
        )
        if current is not None and model == current.model:
            configuration = current
        else:
            configuration = UserConfiguration(
                model=model,
                max_concurrency=(current.max_concurrency if current is not None else 2),
                progress_visibility=(
                    current.progress_visibility if current is not None else "standard"
                ),
            )
        configuration = _bind_private_runtime_profiles(
            configuration,
            config_path=candidate_config,
            refresh_private_configuration=provider_setup_changed,
        )
        inspections = cli._inspect_model_configuration(
            configuration,
            state_dir=candidate_state,
            config_path=candidate_config,
        )
        configuration = _complete_model_metadata(
            configuration,
            inspections,
            offer_price_change=True,
        )
        _commit_configuration_transaction(
            configuration,
            user_path=path,
            live_openclaw_state=state_paths.openclaw,
            staged_openclaw_state=(candidate_state if provider_setup_changed else None),
        )
    print(f"✓ Saved secret-free model configuration to {path}")
    if cli._prompt_yes_no(
        "Run one minimal provider check now? This may incur provider usage.",
        default=False,
    ):
        cli._run_provider_smoke(
            cli.DEFAULT_OPENCLAW_BINARY,
            configuration.default_model_profile,
            state_dir=state_paths.openclaw,
            config_path=openclaw_config,
        )
        print("✓ Provider check completed")
    return configuration, inspections


def _collect_product_request(
    *,
    working_directory: Path,
    run_id: str,
    configuration: UserConfiguration,
    execution_profile: tuple[str, ...],
    base_constraints: tuple[str, ...],
) -> tuple[PlanningRequest, Path, TaskResourceAuthorization] | None:
    """Collect the direct inputs and authorization required before Planning."""

    print("\nWhat would you like to build?")
    source_request = cli._prompt_product_text(
        "> ",
        label="software request",
        required=True,
        maximum_length=2000,
    )

    print("\nCurrent execution profile")
    for item in execution_profile:
        print(f"  {item}")
    if not cli._prompt_yes_no(
        "Build your request with this execution profile?",
        default=True,
    ):
        print(
            "No build was started. This release will not silently change your request."
        )
        return None

    while True:
        project_name = cli._prompt_product_text(
            "Project directory [software-project]: ",
            label="project directory",
            multiline=False,
        )
        project_name = project_name or "software-project"
        try:
            destination = validate_project_destination(
                working_directory,
                project_name,
            )
        except ProductFlowError as error:
            print(f"That project directory cannot be used: {error}")
            continue
        break
    print("\nPlanning authorization")
    print(f"  Request: {source_request}")
    print(f"  Destination: {destination}")
    print(f"  Planning model: {configuration.model}")
    print(f"  Runtime model routing: {configuration.routing_mode.value}")
    for profile in configuration.model_profiles:
        print(f"    - {profile.id}: {profile.model}")
    print("  Planning may ask focused questions before proposing a team.")
    print("  No execution Agent is created until you approve the full overview.")
    print("  Multiple model requests may be made and may incur provider usage.")
    print("  SAT cannot determine your organization's model or Docker policy.")
    resource_authorization = cli._collect_task_resource_authorization(configuration)
    if resource_authorization is None:
        return None
    if not cli._prompt_yes_no(
        "Start model-backed Planning for this request?",
        default=False,
    ):
        print("Build cancelled; no model request was made.")
        return None
    planning_request = PlanningRequest(
        run_id=run_id,
        project_name=project_name,
        source_request=source_request,
        destination=str(destination),
        execution_profile=execution_profile,
        base_constraints=base_constraints,
        model=configuration.model,
        authorization="user_confirmed",
        authorized_at=datetime.now(UTC),
    )
    return planning_request, destination, resource_authorization


def _product_planning_policy(
    quality: QualityGateConfiguration,
    configuration: UserConfiguration,
    resource_authorization: TaskResourceAuthorization,
) -> PlanningPolicy:
    """Compile user-authorized product resources into Planning authority."""

    provider_activity = CapabilityTimeoutPolicy(
        default_seconds=0,
        ceiling_seconds=0,
    )

    return PlanningPolicy(
        max_clarification_rounds=None,
        max_proposal_revisions=None,
        planning_timeout_seconds=0,
        max_agents=None,
        max_concurrency=configuration.max_concurrency,
        max_review_agents=None,
        max_iterations=None,
        run_deadline_seconds=resource_authorization.run_deadline_seconds,
        budget=AgentBudget(
            authority=BudgetAuthority.USER_TASK,
            max_estimated_cost_usd=(resource_authorization.maximum_estimated_cost_usd),
        ),
        capability_timeouts={
            AgentCapability.IMPLEMENTATION: provider_activity,
            AgentCapability.INTEGRATION: provider_activity,
            AgentCapability.TESTING: provider_activity,
            AgentCapability.REVIEW: provider_activity,
        },
        model_routing=configuration.model_routing_policy(),
        profile_acceptance_criteria=quality.task_brief.acceptance_criteria,
        require_review_agent=True,
    )


def _run_product_planning(
    request: PlanningRequest,
    *,
    source_repository: Path,
    state_paths: ProductStatePaths,
    quality: QualityGateConfiguration,
    configuration: UserConfiguration,
    resource_authorization: TaskResourceAuthorization,
    budget_ledger: AgentBudgetLedger,
    terminal_metrics: TerminalRunMetrics | None = None,
    visibility_handler: Callable[[str], None] | None = None,
) -> ApprovedPlanningResult | None:
    """Run one isolated bootstrap Planning session and clean its sandbox."""

    print("\nChecking the isolated Planning runtime...")
    print(
        "  Verifying the selected model and restricted sandbox without "
        "generating content."
    )
    print(
        "  A cold local model check may take up to "
        f"{MODEL_INSPECTION_TIMEOUT_SECONDS} seconds."
    )
    manifest = load_team_manifest(DEFAULT_TEAM_CONFIG)
    try:
        inspection = cli.inspect_sandbox_image(
            sandbox_binary="docker",
            sandbox_image=quality.policy.sandbox.image,
        )
    except RuntimeConfigurationError as error:
        raise RuntimeConfigurationError(
            f"Planning runtime check failed before any Agent was started: {error}"
        ) from error
    if not inspection.ready or inspection.sandbox_image_id is None:
        raise RuntimeConfigurationError(
            "the configured sandbox image is not present locally"
        )
    limits = quality.policy.limits
    with tempfile.TemporaryDirectory(
        prefix=".planning-runtime-",
        dir=state_paths.root,
    ) as temporary:
        planning_profiles = resolve_bootstrap_model_profiles(
            configuration.model_routing_policy()
        )
        if planning_profiles[0].model != request.model:
            raise RuntimeConfigurationError(
                "Planning request differs from the configured default model"
            )
        route_runtimes: list[PlanningRouteRuntime] = []
        for profile in planning_profiles:
            runtime_path = Path(temporary) / f"openclaw.runtime.{profile.id}.json"
            cli.materialize_run_configuration(
                DEFAULT_OPENCLAW_CONFIG,
                runtime_path,
                manifest=manifest,
                workspace=source_repository,
                sandbox_image=inspection.sandbox_image_id,
                sandbox_memory_mb=limits.memory_mb,
                sandbox_cpus=limits.cpu_cores,
                sandbox_pids_limit=limits.pids,
                sandbox_open_files=limits.open_files,
                sandbox_tmpfs_mb=limits.writable_tmpfs_mb,
                model=profile.model,
                model_runtime_profile=profile.runtime_profile,
                bootstrap_capability=AgentCapability.CLARIFICATION,
            )
            try:
                preflight = cli.inspect_runtime_preflight(
                    openclaw_binary=cli.DEFAULT_OPENCLAW_BINARY,
                    openclaw_state_dir=state_paths.openclaw,
                    runtime_config=runtime_path,
                    sandbox_binary="docker",
                    sandbox_image=quality.policy.sandbox.image,
                    expected_sandbox_image_id=inspection.sandbox_image_id,
                    expected_model=profile.model,
                    expected_runtime_profile=profile.runtime_profile,
                )
            except RuntimeConfigurationError as error:
                raise RuntimeConfigurationError(
                    "Planning runtime check failed before any Agent was started: "
                    f"{error} (profile {profile.id})"
                ) from error
            if not preflight.ready:
                raise RuntimeConfigurationError(
                    f"Planning runtime preflight failed for profile {profile.id}: "
                    f"config_valid={preflight.config_valid}, "
                    f"sandbox_container_ready={preflight.sandbox_container_ready}, "
                    "sandbox_container_error="
                    f"{preflight.sandbox_container_error or 'none'}, "
                    f"model_available={preflight.model_available}, "
                    f"model_error={preflight.model_error or 'none'}"
                )
            planning_metadata = next(
                (
                    item
                    for item in resource_authorization.model_metadata
                    if item.profile_id == profile.id and item.model == profile.model
                ),
                None,
            )
            if planning_metadata is None:
                raise RuntimeConfigurationError(
                    f"task authorization does not cover Planning profile {profile.id}"
                )
            executor = OpenClawSubprocessExecutor(
                openclaw_binary=cli.DEFAULT_OPENCLAW_BINARY,
                environment=isolated_openclaw_environment(
                    state_dir=state_paths.openclaw,
                    config_path=runtime_path,
                ),
                local=True,
                run_deadline_at=resource_authorization.deadline_at,
                process_lease_store=ProcessLeaseStore(state_paths.process_leases),
            )
            executor.register_model_liveness(
                model=profile.model,
                local=preflight.model_local,
                provider_request_timeout_seconds=(
                    preflight.model_request_timeout_seconds
                ),
            )
            route_runtimes.append(
                PlanningRouteRuntime(
                    route_id=profile.id,
                    model=profile.model,
                    executor=executor,
                    pricing=ModelPricing(
                        model=planning_metadata.model,
                        input_cost_per_million_usd=(
                            planning_metadata.input_cost_per_million_usd
                        ),
                        output_cost_per_million_usd=(
                            planning_metadata.output_cost_per_million_usd
                        ),
                        pricing_source=planning_metadata.pricing_source,
                        pricing_observed_at=planning_metadata.observed_at,
                        cache_pricing=planning_metadata.cache_pricing,
                        max_input_tokens=profile.context_window_tokens,
                        max_output_tokens=(
                            profile.runtime_profile.invocation_max_tokens
                            or profile.runtime_profile.max_output_tokens
                            or profile.context_window_tokens
                        ),
                    ),
                    runtime_profile=profile.runtime_profile,
                )
            )
        print(
            "✓ Planning runtime: isolated workspace, sandbox, and model ready"
            + (
                ""
                if len(route_runtimes) == 1
                else f" ({len(route_runtimes)} authorized routes)"
            )
        )
        primary_route, *fallback_routes = route_runtimes
        coordinator = AdaptivePlanningCoordinator(
            executor=primary_route.executor,
            store=PlanningStore(state_paths.planning),
            policy=_product_planning_policy(
                quality,
                configuration,
                resource_authorization,
            ),
            budget_ledger=budget_ledger,
            pricing=primary_route.pricing,
            route_id=primary_route.route_id,
            runtime_profile=primary_route.runtime_profile,
            fallback_routes=tuple(fallback_routes),
            provider_failure_fallback=bool(fallback_routes),
        )
        cleanup_arguments = {
            "sandbox_binary": "docker",
            "run_id": request.run_id,
            "openclaw_state_dir": state_paths.openclaw,
            "workspace_dir": source_repository.resolve(strict=False),
            "iteration_limit": 1,
            "roles": (AgentRole.CLARIFIER,),
        }
        try:
            approved = cli.run_interactive_planning(
                coordinator,
                request,
                read=cli._read_short,
                read_text=cli._read_natural_text,
                output=sys.stdout,
                progress_visibility=configuration.progress_visibility,
                progress_display=configuration.progress_display,
                progress_color=configuration.progress_color,
                metrics=terminal_metrics,
                visibility_handler=visibility_handler,
            )
        except BaseException as error:
            try:
                cli.cleanup_run_sandbox_containers(**cleanup_arguments)
            except Exception as cleanup_error:
                error.add_note(f"Planning sandbox cleanup also failed: {cleanup_error}")
            raise
        cleanup = cli.cleanup_run_sandbox_containers(**cleanup_arguments)
        print(
            "Planning cleanup: "
            f"removed {len(cleanup.removed)} bootstrap sandbox container(s)"
        )
        return approved


def _load_final_report(path: Path, *, expected_sha256: str) -> FinalReport:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("final report is not a regular file")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise RuntimeError("final report digest changed before product delivery")
    payload = json.loads(raw)
    artifact = FinalReport.model_validate(payload)
    return artifact


def _render_product_outcome(
    *,
    outcome: WorkflowOutcome | DynamicWorkflowOutcome,
    report: FinalReport,
    report_path: Path,
    destination: Path | None,
) -> None:
    """Show one concise delivery or failure view from verified evidence."""

    elapsed = outcome.record.updated_at - outcome.record.created_at
    accepted = sum(
        result.status is CheckStatus.PASSED for result in report.acceptance_results
    )
    total = len(report.acceptance_results)
    print("\nBuild result")
    print(f"  Status: {report.status.value}")
    if destination is not None:
        print(f"  Project: {destination}")
    print(f"  Summary: {report.summary}")
    print(f"  Acceptance: {accepted}/{total} passed")
    print(f"  Elapsed: {int(elapsed.total_seconds())} seconds")
    print(f"  Report: {report_path}")
    if report.known_limitations:
        print("  Known limitations:")
        for limitation in report.known_limitations:
            print(f"    - {limitation}")
    if report.unresolved_findings:
        print("  Unresolved findings:")
        for finding in report.unresolved_findings:
            print(f"    - {finding}")


def _replacement_planning_request(
    request: PlanningRequest,
    *,
    run_id: str,
    correction_instruction: str,
) -> PlanningRequest:
    """Preserve the original request while recording one explicit correction."""

    correction = (
        "User correction that supersedes conflicting earlier requirements: "
        f"{correction_instruction.strip()}"
    )
    constraints = (*request.base_constraints, correction)
    return PlanningRequest(
        **{
            **request.model_dump(),
            "run_id": run_id,
            "base_constraints": constraints,
            "authorized_at": datetime.now(UTC),
        }
    )


def _task_admission_checkpoint(
    *,
    planning_request: PlanningRequest,
    destination: Path,
    state_paths: ProductStatePaths,
    diagnostics: StartupDiagnostics,
    configuration: UserConfiguration,
    model_inspections: tuple[OpenClawModelInspection, ...],
    resource_authorization: TaskResourceAuthorization,
    update_observation: ForegroundUpdateObservation | None = None,
) -> tuple[
    TaskSelfCheckReport,
    ForegroundUpdateObservation,
    SoftwareVersionReport,
]:
    """Persist and render one complete pre-Planning readiness snapshot."""

    version = cli._software_version_report()
    if update_observation is None:
        update_observation = cli.inspect_task_admission_update(
            project_root=cli.PROJECT_ROOT,
            version_report=version,
        )
    try:
        schemas = inspect_persisted_schema_compatibility(
            configuration_path=user_configuration_path(),
            installation_record_path=installation_record_path(),
            state_root=state_paths.root,
            candidate_support=version.schema_support,
        )
    except SchemaCompatibilityError as error:
        schemas = PersistedSchemaCompatibilityReport(
            compatible=False,
            observations=(),
            problems=(str(error),),
        )
    observed = build_task_admission_report(
        run_id=planning_request.run_id,
        diagnostics=diagnostics,
        software_version=version,
        schema_compatibility=schemas,
        update_observation=update_observation,
        configuration=configuration,
        model_inspections=model_inspections,
        source_request=planning_request.source_request,
        destination=destination,
        resource_authorization=resource_authorization,
    )
    store = TaskSelfCheckStore(state_paths.self_checks)
    report, path = store.record_observation(observed)
    print()
    print(
        render_self_check_report(
            report,
            visibility=configuration.progress_visibility,
            color=terminal_color_enabled(
                sys.stdout,
                configuration.progress_color,
            ),
        )
    )
    if configuration.progress_visibility == "detailed":
        if path is None:
            print(f"  Evidence unchanged at revision {report.revision}")
        else:
            print(f"  Evidence: {path}")
    return report, update_observation, version


def _constrain_product_concurrency(
    configuration: UserConfiguration,
    diagnostics: StartupDiagnostics,
    *,
    sandbox_memory_mb: int,
    sandbox_pids: int,
) -> UserConfiguration:
    """Apply a visible host-capacity ceiling to this run without changing defaults."""

    if diagnostics.host_capacity is None:
        return configuration
    decision = limit_concurrency_to_host_capacity(
        configured_max_concurrency=configuration.max_concurrency,
        capacity=diagnostics.host_capacity,
        sandbox_memory_mb=sandbox_memory_mb,
        sandbox_pids=sandbox_pids,
    )
    if not decision.constrained:
        return configuration

    print("\nRuntime concurrency adjusted for current host capacity")
    print(
        "  This run will use at most "
        f"{decision.effective_max_concurrency} concurrent Agent(s)."
    )
    print(
        "  Saved maximum remains "
        f"{decision.configured_max_concurrency}; the approved TeamPlan will show "
        "the effective value."
    )
    if (
        decision.memory_slots is not None
        and decision.memory_slots < decision.configured_max_concurrency
    ):
        print(
            "  Memory headroom currently covers "
            f"{decision.memory_slots} complete sandbox ceiling(s)."
        )
    if (
        decision.pid_slots is not None
        and decision.pid_slots < decision.configured_max_concurrency
    ):
        print(
            "  PID headroom currently covers "
            f"{decision.pid_slots} complete sandbox ceiling(s)."
        )
    return configuration.model_copy(
        update={"max_concurrency": decision.effective_max_concurrency}
    )


def _plan_execution_checkpoint(
    *,
    admission_report: TaskSelfCheckReport,
    approved: ApprovedPlanningResult,
    destination: Path,
    state_paths: ProductStatePaths,
    quality: QualityGateConfiguration,
    configuration: UserConfiguration,
) -> TaskSelfCheckReport:
    """Persist and render approved-plan readiness before workspace mutation."""

    preflight: RuntimePreflight | None = None
    error_text: str | None = None
    try:
        preflight = cli._inspect_approved_plan_runtime(
            team_plan=approved.team_plan,
            source_repository=DEFAULT_PRODUCT_SEED,
            state_paths=state_paths,
            quality=quality,
        )
    except (OSError, RuntimeConfigurationError, ValueError) as error:
        error_text = f"approved-plan runtime check failed: {error}"
    observed = build_plan_execution_report(
        admission_report=admission_report,
        team_plan=approved.team_plan,
        runtime_preflight=preflight,
        runtime_error=error_text,
        source_repository=DEFAULT_PRODUCT_SEED.resolve(strict=True),
        destination=destination,
    )
    store = TaskSelfCheckStore(state_paths.self_checks)
    report, path = store.record_observation(observed)
    print()
    print(
        render_self_check_report(
            report,
            visibility=configuration.progress_visibility,
            color=terminal_color_enabled(
                sys.stdout,
                configuration.progress_color,
            ),
        )
    )
    if configuration.progress_visibility == "detailed":
        if path is None:
            print(f"  Evidence unchanged at revision {report.revision}")
        else:
            print(f"  Evidence: {path}")
    return report


def _run_product(
    *,
    progress_visibility: str | None = None,
    progress_display: str | None = None,
    progress_color: str | None = None,
) -> int:
    """Run the primary diagnostics-to-delivery product journey."""

    product_started_monotonic = time.monotonic()

    if not sys.stdin.isatty():
        raise ValueError(
            "the guided product flow requires an interactive terminal; "
            "use an explicit subcommand for automation"
        )

    quality = load_quality_gate_configuration(
        DEFAULT_PRODUCT_POLICY,
        DEFAULT_PRODUCT_PROFILE,
    )
    saved_configuration = cli._load_user_configuration(user_configuration_path())
    startup_visibility = (
        progress_visibility
        if progress_visibility is not None
        else saved_configuration.progress_visibility
        if saved_configuration is not None
        else "standard"
    )
    startup_color = (
        progress_color
        if progress_color is not None
        else saved_configuration.progress_color
        if saved_configuration is not None
        else "auto"
    )
    working_directory = Path.cwd()
    diagnostics = cli.inspect_startup_environment(
        working_directory=working_directory,
        openclaw_binary=DEFAULT_OPENCLAW_BINARY,
        sandbox_image=quality.policy.sandbox.image,
        state_root=user_state_root(),
        required_memory_mb=quality.policy.limits.memory_mb,
        required_pids=quality.policy.limits.pids,
    )
    cli.render_startup_diagnostics(
        diagnostics,
        visibility=startup_visibility,
        color=terminal_color_enabled(sys.stdout, startup_color),
    )
    if not diagnostics.ready:
        print("\nSAT is not ready. Complete the actions above and run sat again.")
        return 2

    state_paths = ProductStatePaths.below(user_state_root())
    ensure_product_state(state_paths)
    configuration, model_inspections = cli._ensure_product_configuration(state_paths)
    presentation_overrides = {
        key: value
        for key, value in {
            "progress_visibility": progress_visibility,
            "progress_display": progress_display,
            "progress_color": progress_color,
        }.items()
        if value is not None
    }
    if presentation_overrides:
        configuration = configuration.model_copy(update=presentation_overrides)
    configuration = _constrain_product_concurrency(
        configuration,
        diagnostics,
        sandbox_memory_mb=quality.policy.limits.memory_mb,
        sandbox_pids=quality.policy.limits.pids,
    )
    run_id = generate_product_run_id()
    execution_profile = (
        "A new, small Python 3.12 project.",
        "Web apps, CLI tools, and local automation are supported.",
        "Deterministic verification has no network or external services.",
    )
    request = _collect_product_request(
        working_directory=working_directory,
        run_id=run_id,
        configuration=configuration,
        execution_profile=execution_profile,
        base_constraints=tuple(quality.task_brief.constraints),
    )
    if request is None:
        return 0
    planning_request, destination, resource_authorization = request
    (
        admission_report,
        update_observation,
        software_version,
    ) = cli._task_admission_checkpoint(
        planning_request=planning_request,
        destination=destination,
        state_paths=state_paths,
        diagnostics=diagnostics,
        configuration=configuration,
        model_inspections=model_inspections,
        resource_authorization=resource_authorization,
    )
    while not admission_report.ready:
        print("\nSAT did not start Planning because required self-checks failed.")
        if not cli._prompt_yes_no(
            "Apply the listed actions, then recheck this task now?",
            default=True,
        ):
            return 2
        diagnostics = cli.inspect_startup_environment(
            working_directory=working_directory,
            openclaw_binary=DEFAULT_OPENCLAW_BINARY,
            sandbox_image=quality.policy.sandbox.image,
            state_root=state_paths.root,
            required_memory_mb=quality.policy.limits.memory_mb,
            required_pids=quality.policy.limits.pids,
        )
        model_inspections = tuple(
            cli._inspect_selected_model(
                cli.DEFAULT_OPENCLAW_BINARY,
                profile,
                state_dir=state_paths.openclaw,
                config_path=state_paths.openclaw / "openclaw.json",
            )
            for profile in configuration.model_profiles
        )
        admission_report, update_observation, software_version = (
            _task_admission_checkpoint(
                planning_request=planning_request,
                destination=destination,
                state_paths=state_paths,
                diagnostics=diagnostics,
                configuration=configuration,
                model_inspections=model_inspections,
                resource_authorization=resource_authorization,
                update_observation=update_observation,
            )
        )
    budget_ledger = AgentBudgetLedger(
        AgentBudget(
            authority=BudgetAuthority.USER_TASK,
            max_estimated_cost_usd=(resource_authorization.maximum_estimated_cost_usd),
        )
    )

    terminal_metrics = TerminalRunMetrics(
        ledger=budget_ledger,
        started_monotonic=product_started_monotonic,
        model_windows={
            profile.model: profile.context_window_tokens
            for profile in configuration.model_profiles
        },
    )
    renderer = TerminalProgressRenderer(
        visibility=RunEventVisibility(configuration.progress_visibility),
        display=TerminalProgressDisplay(configuration.progress_display),
        color=TerminalColorMode(configuration.progress_color),
        metrics=terminal_metrics,
    )
    try:
        while True:
            approved = cli._run_product_planning(
                planning_request,
                source_repository=DEFAULT_PRODUCT_SEED,
                state_paths=state_paths,
                quality=quality,
                configuration=configuration,
                resource_authorization=resource_authorization,
                budget_ledger=budget_ledger,
                terminal_metrics=terminal_metrics,
                visibility_handler=renderer.set_visibility,
            )
            if approved is None:
                return 0

            run_id = planning_request.run_id
            execution_report = cli._plan_execution_checkpoint(
                admission_report=admission_report,
                approved=approved,
                destination=destination,
                state_paths=state_paths,
                quality=quality,
                configuration=configuration,
            )
            while not execution_report.ready:
                print(
                    "\nSAT did not create runtime Agents or a workspace because "
                    "required self-checks failed."
                )
                if not cli._prompt_yes_no(
                    "Apply the listed actions, then recheck the approved plan now?",
                    default=True,
                ):
                    return 2
                execution_report = cli._plan_execution_checkpoint(
                    admission_report=admission_report,
                    approved=approved,
                    destination=destination,
                    state_paths=state_paths,
                    quality=quality,
                    configuration=configuration,
                )
            source_repository = cli.prepare_product_source(
                seed=DEFAULT_PRODUCT_SEED,
                state_paths=state_paths,
                run_id=run_id,
            )

            def start_controls(
                store: ControlCommandStore,
                team_plan: TeamPlan,
                *,
                source: Path = source_repository,
                current_run_id: str = run_id,
            ) -> Callable[[], None]:
                console = TerminalControlConsole(
                    store=store,
                    team_plan=team_plan,
                    notice_handler=renderer.write_notice,
                    visibility_handler=renderer.set_visibility,
                    live_lines=renderer.live_lines,
                    detail_lines=renderer.detail_lines,
                    color=renderer.color_enabled,
                )
                console.start()
                renderer.attach_dashboard(console.dashboard)
                if console.dashboard is not None:
                    manager = GitWorkspaceManager(state_paths.workspaces)
                    try:
                        base_commit = manager.validate_source_repository(source)
                    except (GitWorkspaceError, OSError):
                        # Optional display telemetry cannot change the run outcome.
                        pass
                    else:
                        terminal_metrics.watch_git(
                            state_paths.workspaces / current_run_id,
                            base_commit=base_commit,
                        )

                def close_controls() -> None:
                    console.close()
                    terminal_metrics.stop_git()
                    renderer.attach_dashboard(None)

                return close_controls

            outcome = cli._execute_dynamic_workflow(
                approved,
                _AdaptiveWorkflowLaunchOptions(
                    source_repository=source_repository,
                    base_ref="HEAD",
                    teams=DEFAULT_TEAM_CONFIG,
                    openclaw=DEFAULT_OPENCLAW_CONFIG,
                    policy=DEFAULT_PRODUCT_POLICY,
                    quality_manifest=DEFAULT_PRODUCT_PROFILE,
                    runs_root=state_paths.runs,
                    workspaces_root=state_paths.workspaces,
                    openclaw_binary=DEFAULT_OPENCLAW_BINARY,
                    openclaw_state_dir=state_paths.openclaw,
                    process_leases_root=state_paths.process_leases,
                    sandbox_binary="docker",
                    model=configuration.model,
                    input_cost_per_million_usd=(
                        configuration.input_cost_per_million_usd
                    ),
                    output_cost_per_million_usd=(
                        configuration.output_cost_per_million_usd
                    ),
                    artifact_repair_limit=None,
                    progress_handler=renderer,
                    budget_ledger=budget_ledger,
                    run_deadline_at=resource_authorization.deadline_at,
                ),
                software_version=software_version,
                control_store_handler=start_controls,
            )
            report_path = (
                state_paths.runs / outcome.record.run_id / outcome.final_report.path
            )
            report = cli._load_final_report(
                report_path,
                expected_sha256=outcome.final_report.sha256,
            )
            if outcome.control_stop is not RuntimeControlDecision.CORRECT:
                break
            instruction = outcome.correction_instruction
            if instruction is None:
                raise RuntimeError("replacement Planning omitted the correction")
            cli._render_product_outcome(
                outcome=outcome,
                report=report,
                report_path=report_path,
                destination=None,
            )
            print(
                "  Previous work and evidence were preserved but will not be delivered."
            )
            print("\nStarting replacement Planning from your corrected requirement.")
            planning_request = _replacement_planning_request(
                planning_request,
                run_id=generate_product_run_id(),
                correction_instruction=instruction,
            )
            refreshed_diagnostics = cli.inspect_startup_environment(
                working_directory=working_directory,
                openclaw_binary=DEFAULT_OPENCLAW_BINARY,
                sandbox_image=quality.policy.sandbox.image,
                state_root=state_paths.root,
                required_memory_mb=quality.policy.limits.memory_mb,
                required_pids=quality.policy.limits.pids,
            )
            refreshed_inspections = tuple(
                cli._inspect_selected_model(
                    cli.DEFAULT_OPENCLAW_BINARY,
                    profile,
                    state_dir=state_paths.openclaw,
                    config_path=state_paths.openclaw / "openclaw.json",
                )
                for profile in configuration.model_profiles
            )
            admission_report, _, software_version = cli._task_admission_checkpoint(
                planning_request=planning_request,
                destination=destination,
                state_paths=state_paths,
                diagnostics=refreshed_diagnostics,
                configuration=configuration,
                model_inspections=refreshed_inspections,
                resource_authorization=resource_authorization,
                update_observation=update_observation,
            )
            while not admission_report.ready:
                print(
                    "\nReplacement Planning did not start because a refreshed "
                    "self-check failed."
                )
                if not cli._prompt_yes_no(
                    "Apply the listed actions, then recheck this replacement task?",
                    default=True,
                ):
                    return 2
                refreshed_diagnostics = cli.inspect_startup_environment(
                    working_directory=working_directory,
                    openclaw_binary=DEFAULT_OPENCLAW_BINARY,
                    sandbox_image=quality.policy.sandbox.image,
                    state_root=state_paths.root,
                    required_memory_mb=quality.policy.limits.memory_mb,
                    required_pids=quality.policy.limits.pids,
                )
                refreshed_inspections = tuple(
                    cli._inspect_selected_model(
                        DEFAULT_OPENCLAW_BINARY,
                        profile,
                        state_dir=state_paths.openclaw,
                        config_path=state_paths.openclaw / "openclaw.json",
                    )
                    for profile in configuration.model_profiles
                )
                admission_report, _, software_version = cli._task_admission_checkpoint(
                    planning_request=planning_request,
                    destination=destination,
                    state_paths=state_paths,
                    diagnostics=refreshed_diagnostics,
                    configuration=configuration,
                    model_inspections=refreshed_inspections,
                    resource_authorization=resource_authorization,
                    update_observation=update_observation,
                )
    finally:
        renderer.close()
        terminal_metrics.stop_git()

    if outcome.record.phase is not RunPhase.COMPLETED:
        _render_product_outcome(
            outcome=outcome,
            report=report,
            report_path=report_path,
            destination=None,
        )
        print("  No project was delivered; preserved evidence can be inspected safely.")
        return 2
    if outcome.record.workspace is None:
        raise RuntimeError("completed run is missing its verified workspace")
    workspace_path = Path(outcome.record.workspace.workspace_path)
    try:
        delivered = cli.deliver_product_workspace(
            workspace_path,
            destination,
            expected_commit=outcome.record.current_commit,
        )
    except ProductFlowError as error:
        print("\nBuild accepted, but delivery did not complete.")
        print(f"  Reason: {error}")
        print(f"  Verified workspace: {workspace_path}")
        print(f"  Report: {report_path}")
        print("  No destination was reported as successful.")
        return 2
    project_commands = cli.load_project_commands(delivered)
    cli._render_product_outcome(
        outcome=outcome,
        report=report,
        report_path=report_path,
        destination=delivered,
    )
    uv = shutil.which("uv") or str(Path.home() / ".local/bin/uv")

    def render_command(argv: tuple[str, ...]) -> str:
        resolved = (uv, *argv[1:]) if argv[0] == "uv" else argv
        return shlex.join(resolved)

    print("\nNext commands")
    print(f"  cd {shlex.quote(str(delivered))}")
    print(f"  {render_command(project_commands.setup)}")
    print(f"  {render_command(project_commands.start)}")
    print(f"  {render_command(project_commands.test)}")
    return 0
