"""Command-line entry point and facade for the software-agent-team (SAT) CLI.

Modularized submodules:
- `constants`: Path defaults, configurations, timeouts, iterations
- `prompts`: Interactive prompts, validations, authorization collection
- `model_inspection`: OpenClaw provider smoke tests, model discovery, runtime preflight
- `workflow`: Static and adaptive dynamic workflow orchestration
- `product`: Guided interactive software delivery experience
- `parser`: Argument parser definitions for all commands
- `commands/configure`: Interactive and non-interactive profile configuration
- `commands/version`: Channel management, version reports, managed updates
- `commands/validation`: Validation commands for handoff, configs, artifacts, preflight
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Sequence

from pydantic import ValidationError

# External re-exports needed by callers/tests
from software_agent_team.artifacts import (
    AgentRole,
    CheckStatus,
    CommandEvidence,
    FinalReport,
    HandoffEnvelope,
    TaskBrief,
    parse_phase_artifact,
)
from software_agent_team.benchmark_seed import prepare_benchmark_seed
from software_agent_team.bounded_process import (
    BoundedProcessError,
    BoundedProcessTimeoutError,
    run_bounded_process,
)
from software_agent_team.budgets import (
    AgentBudget,
    AgentBudgetLedger,
    BudgetAuthority,
    ModelPricing,
)
from software_agent_team.configuration import validate_environment_configuration
from software_agent_team.control_console import TerminalControlConsole
from software_agent_team.controls import ControlCommandStore
from software_agent_team.decision_limits import validate_decision_limit_registry
from software_agent_team.docker_engine import bound_docker_engine
from software_agent_team.dynamic_workflow import (
    DynamicWorkflowCoordinator,
    DynamicWorkflowOutcome,
)
from software_agent_team.execution import OpenClawSubprocessExecutor
from software_agent_team.git_workspace import (
    GitWorkspace,
    GitWorkspaceError,
    GitWorkspaceManager,
)
from software_agent_team.managed_install import (
    MANAGED_MARKER_NAME,
    ManagedChannel,
    ManagedInstallError,
    ManagedInstallPaths,
    finalize_staged_sandbox_image_transition,
    install_managed_target,
    is_retired_managed_application,
    managed_foreground_task_lease,
    prepare_staged_sandbox_image_transition,
    promote_legacy_docker_engine_record,
    reconcile_pending_sandbox_image_transition,
    resolve_dev_target,
    target_from_stable_release,
)
from software_agent_team.model_costs import CachePricing
from software_agent_team.model_metadata import ModelMetadataSource
from software_agent_team.model_routing import (
    ModelProfile,
    ModelRoutingMode,
    resolve_bootstrap_model_profiles,
)
from software_agent_team.model_runtime import (
    ArtifactSubmissionPolicy,
    CredentialSource,
    ModelApi,
    ModelEndpointKind,
    ModelRuntimeProfile,
    ModelRuntimeProfileSource,
    openclaw_invocation_thinking_level,
    runtime_profile_for_model,
    runtime_profile_from_openclaw_configuration,
)
from software_agent_team.openclaw_runtime import (
    is_owned_openclaw_runtime,
    isolated_openclaw_environment,
)
from software_agent_team.paths import user_state_root
from software_agent_team.planning import (
    AdaptivePlanningCoordinator,
    ApprovedPlanningResult,
    CapabilityTimeoutPolicy,
    PlanningPolicy,
    PlanningRequest,
    PlanningRouteRuntime,
    PlanningStore,
    run_interactive_planning,
)
from software_agent_team.process_lifecycle import ProcessLeaseStore
from software_agent_team.product import (
    ProductFlowError,
    ProductStatePaths,
    StartupDiagnostics,
    deliver_product_workspace,
    ensure_product_state,
    generate_product_run_id,
    inspect_startup_environment,
    limit_concurrency_to_host_capacity,
    load_project_commands,
    prepare_product_source,
    render_startup_diagnostics,
    validate_project_destination,
)
from software_agent_team.progress import (
    ProgressDraftHandler,
    ProgressEvent,
    ProgressEventKind,
    ProgressHandler,
    RunEventVisibility,
    TerminalColorMode,
    TerminalProgressDisplay,
    TerminalProgressRenderer,
    terminal_color_enabled,
)
from software_agent_team.quality_gates import (
    DockerSandboxBackend,
    QualityGateConfiguration,
    QualityGateRunner,
    bind_run_task_brief_mount,
    load_quality_gate_configuration,
)
from software_agent_team.releases import (
    DEFAULT_LATEST_RELEASE_API_URL,
    ReleaseResolutionError,
    resolve_latest_stable_release,
)
from software_agent_team.run_control import RunPhase
from software_agent_team.runtime_configuration import (
    MODEL_INSPECTION_TIMEOUT_SECONDS,
    RuntimeConfigurationError,
    RuntimePreflight,
    inspect_openclaw_model,
    inspect_runtime_preflight,
    inspect_sandbox_image,
    materialize_run_configuration,
)
from software_agent_team.runtime_controls import RuntimeControlDecision
from software_agent_team.sandbox_lifecycle import cleanup_run_sandbox_containers
from software_agent_team.schema_compatibility import (
    PersistedSchemaCompatibilityReport,
    SchemaCompatibilityError,
    inspect_persisted_schema_compatibility,
)
from software_agent_team.sdk_state_migration import migrate_copied_provider_databases
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
    TeamManifest,
    TeamPlan,
    load_team_manifest,
)
from software_agent_team.terminal_metrics import TerminalRunMetrics
from software_agent_team.updates import (
    ForegroundUpdateObservation,
    inspect_task_admission_update,
    resolve_requested_target,
    validate_current_managed_install,
)
from software_agent_team.user_configuration import (
    UserConfiguration,
    save_user_configuration,
    user_configuration_path,
)
from software_agent_team.versioning import (
    SoftwareVersionReport,
    inspect_software_version,
    installation_record_path,
    render_short_version,
    render_version_report,
)
from software_agent_team.workflow import (
    WorkflowCoordinator,
    WorkflowOutcome,
)

from .commands.configure import (
    _apply_runtime_profile_arguments,
    _commit_configuration_transaction,
    _complete_model_metadata,
    _configuration_assignment_map,
    _configuration_bool,
    _configure,
    _configured_model_fields,
    _is_openclaw_host_peer,
    _load_user_configuration,
    _print_configuration,
    _replace_exact_private_file,
    _replace_profile,
    _restore_user_configuration,
    _run_openclaw_configuration,
    _split_configuration_assignment,
    _staged_npm_link_target,
    _staged_openclaw_state,
)
from .commands.validation import (
    _cleanup_owned_resources,
    _list_teams,
    _preflight,
    _prepare_benchmark,
    _validate_artifact,
    _validate_config,
    _validate_handoff,
    _validate_task_brief,
)
from .commands.version import (
    _apply_managed_change,
    _bootstrap_managed_install,
    _inspect_candidate_managed_state,
    _load_json_model,
    _managed_install_context,
    _record_managed_image_transition,
    _render_managed_change,
    _resolve_managed_change,
    _show_channel,
    _show_version,
    _software_version_report,
    _switch_channel,
    _update_managed_install,
)
from .constants import (
    DEFAULT_BENCHMARK,
    DEFAULT_BENCHMARK_SEED,
    DEFAULT_OPENCLAW_BINARY,
    DEFAULT_OPENCLAW_CONFIG,
    DEFAULT_PRODUCT_POLICY,
    DEFAULT_PRODUCT_PROFILE,
    DEFAULT_PRODUCT_SEED,
    DEFAULT_RUN_POLICY,
    DEFAULT_RUNS_ROOT,
    DEFAULT_STATE_ROOT,
    DEFAULT_TEAM_CONFIG,
    DEFAULT_WORKSPACES_ROOT,
    EVALUATION_ITERATION_LIMIT,
    PROJECT_ROOT,
)
from .model_inspection import (
    _ANSI_ESCAPE,
    _PROVIDER_SMOKE_DETAIL_MAX_CHARACTERS,
    _PROVIDER_SMOKE_PARSE_MAX_CHARACTERS,
    OpenClawModelInspection,
    _bind_private_runtime_profiles,
    _discover_openclaw_default_model,
    _effective_model_configuration,
    _first_json_object,
    _inspect_model_configuration,
    _inspect_selected_model,
    _merge_discovered_model_metadata,
    _provider_error_fields,
    _provider_smoke_failure_detail,
    _read_private_openclaw_configuration,
    _render_model_inspection_start,
    _run_provider_smoke,
    _sanitize_provider_smoke_detail,
    _validate_optional_openclaw_configuration,
)
from .parser import build_parser
from .product import (
    _collect_product_request,
    _constrain_product_concurrency,
    _ensure_product_configuration,
    _load_final_report,
    _plan_execution_checkpoint,
    _product_planning_policy,
    _render_product_outcome,
    _replacement_planning_request,
    _run_product,
    _run_product_planning,
    _task_admission_checkpoint,
)
from .prompts import (
    TaskModelMetadata,
    TaskResourceAuthorization,
    _collect_task_resource_authorization,
    _prompt_context_window,
    _prompt_nonnegative_decimal,
    _prompt_optional_run_deadline,
    _prompt_product_text,
    _prompt_task_cost_ceiling,
    _prompt_value,
    _prompt_yes_no,
    _read_natural_text,
    _read_short,
)
from .workflow import (
    _AdaptiveWorkflowLaunchOptions,
    _execute_dynamic_workflow,
    _execute_workflow,
    _inspect_approved_plan_runtime,
    _prepare_runtime_boundary,
    _run_workflow,
    _RuntimeBoundary,
    _RuntimeLaunchOptions,
    _timeout_flag,
    _with_team_plan_model_inspections,
    _WorkflowLaunchOptions,
)


def _managed_engine_binding(args: argparse.Namespace):
    """Read the active release's recorded engine before any Docker cleanup."""

    if (
        args.version
        or args.command == "version"
        or args.command
        in {
            "_managed-install",
            "_managed-state-compatibility",
            "_managed-image-transition",
        }
        or os.environ.get("SAT_INSTALL_STAGE_ONLY") == "1"
        or not (PROJECT_ROOT / MANAGED_MARKER_NAME).is_file()
    ):
        return None
    paths = ManagedInstallPaths.from_environment()
    record, _marker = validate_current_managed_install(
        project_root=PROJECT_ROOT, paths=paths
    )
    if record.schema_version == 1:
        record = promote_legacy_docker_engine_record(
            project_root=PROJECT_ROOT,
            paths=paths,
            expected_record=record,
        )
    return record.docker_engine


def _dispatch_main(args: argparse.Namespace) -> int:
    version_only = args.version or args.command == "version"
    if (
        args.command
        not in {
            "_managed-install",
            "_managed-state-compatibility",
            "_managed-image-transition",
        }
        and not version_only
    ):
        try:
            reconcile_pending_sandbox_image_transition(PROJECT_ROOT)
        except ManagedInstallError as error:
            print(
                f"warning: pending sandbox image cleanup was deferred: {error}",
                file=sys.stderr,
            )
    if args.version:
        print(render_short_version(_software_version_report()))
        return 0
    if args.command is None:
        with managed_foreground_task_lease(PROJECT_ROOT):
            if not any(
                (
                    args.progress_visibility,
                    args.progress_display,
                    args.progress_color,
                )
            ):
                return _run_product()
            return _run_product(
                progress_visibility=args.progress_visibility,
                progress_display=args.progress_display,
                progress_color=args.progress_color,
            )
    return args.handler(args)


def main(argv: Sequence[str] | None = None) -> int:
    """Run one CLI command and return a process exit code."""

    arguments = tuple(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ("uninstall",):
        from software_agent_team.uninstall_command import uninstall

        return uninstall(arguments[1:])
    args = build_parser().parse_args(arguments)
    try:
        identity = _managed_engine_binding(args)
        with bound_docker_engine(
            identity, verify=not (args.version or args.command == "version")
        ):
            return _dispatch_main(args)
    except KeyboardInterrupt:
        print("\nBuild interrupted. SAT did not claim a successful delivery.")
        return 130
    except (
        OSError,
        json.JSONDecodeError,
        ValidationError,
        ValueError,
        RuntimeError,
    ) as error:
        print(f"error: {error}")
        return 1


__all__ = [
    "DEFAULT_BENCHMARK",
    "DEFAULT_BENCHMARK_SEED",
    "DEFAULT_LATEST_RELEASE_API_URL",
    "DEFAULT_OPENCLAW_BINARY",
    "DEFAULT_OPENCLAW_CONFIG",
    "DEFAULT_PRODUCT_POLICY",
    "DEFAULT_PRODUCT_PROFILE",
    "DEFAULT_PRODUCT_SEED",
    "DEFAULT_RUNS_ROOT",
    "DEFAULT_RUN_POLICY",
    "DEFAULT_STATE_ROOT",
    "DEFAULT_TEAM_CONFIG",
    "DEFAULT_WORKSPACES_ROOT",
    "EVALUATION_ITERATION_LIMIT",
    "MANAGED_MARKER_NAME",
    "MODEL_INSPECTION_TIMEOUT_SECONDS",
    "PROJECT_ROOT",
    "_ANSI_ESCAPE",
    "_PROVIDER_SMOKE_DETAIL_MAX_CHARACTERS",
    "_PROVIDER_SMOKE_PARSE_MAX_CHARACTERS",
    "AdaptivePlanningCoordinator",
    "AgentBudget",
    "AgentBudgetLedger",
    "AgentCapability",
    "AgentRole",
    "ApprovedPlanningResult",
    "ArtifactSubmissionPolicy",
    "BoundedProcessError",
    "BoundedProcessTimeoutError",
    "BudgetAuthority",
    "CachePricing",
    "CapabilityTimeoutPolicy",
    "CheckStatus",
    "CommandEvidence",
    "ControlCommandStore",
    "CredentialSource",
    "DockerSandboxBackend",
    "DynamicWorkflowCoordinator",
    "DynamicWorkflowOutcome",
    "FinalReport",
    "ForegroundUpdateObservation",
    "GitWorkspace",
    "GitWorkspaceError",
    "GitWorkspaceManager",
    "HandoffEnvelope",
    "ManagedChannel",
    "ManagedInstallError",
    "ManagedInstallPaths",
    "ModelApi",
    "ModelEndpointKind",
    "ModelMetadataSource",
    "ModelPricing",
    "ModelProfile",
    "ModelRoutingMode",
    "ModelRuntimeProfile",
    "ModelRuntimeProfileSource",
    "OpenClawModelInspection",
    "OpenClawSubprocessExecutor",
    "PersistedSchemaCompatibilityReport",
    "PlanningPolicy",
    "PlanningRequest",
    "PlanningRouteRuntime",
    "PlanningStore",
    "ProcessLeaseStore",
    "ProductFlowError",
    "ProductStatePaths",
    "ProgressDraftHandler",
    "ProgressEvent",
    "ProgressEventKind",
    "ProgressHandler",
    "QualityGateConfiguration",
    "QualityGateRunner",
    "ReleaseResolutionError",
    "RunEventVisibility",
    "RunPhase",
    "RuntimeConfigurationError",
    "RuntimeControlDecision",
    "RuntimePreflight",
    "SchemaCompatibilityError",
    "SoftwareVersionReport",
    "StartupDiagnostics",
    "TaskBrief",
    "TaskModelMetadata",
    "TaskResourceAuthorization",
    "TaskSelfCheckReport",
    "TaskSelfCheckStore",
    "TeamManifest",
    "TeamPlan",
    "TerminalColorMode",
    "TerminalControlConsole",
    "TerminalProgressDisplay",
    "TerminalProgressRenderer",
    "TerminalRunMetrics",
    "UserConfiguration",
    "WorkflowCoordinator",
    "WorkflowOutcome",
    "_AdaptiveWorkflowLaunchOptions",
    "_RuntimeBoundary",
    "_RuntimeLaunchOptions",
    "_WorkflowLaunchOptions",
    "_apply_managed_change",
    "_apply_runtime_profile_arguments",
    "_bind_private_runtime_profiles",
    "_bootstrap_managed_install",
    "_cleanup_owned_resources",
    "_collect_product_request",
    "_collect_task_resource_authorization",
    "_commit_configuration_transaction",
    "_complete_model_metadata",
    "_configuration_assignment_map",
    "_configuration_bool",
    "_configure",
    "_configured_model_fields",
    "_constrain_product_concurrency",
    "_discover_openclaw_default_model",
    "_dispatch_main",
    "_effective_model_configuration",
    "_ensure_product_configuration",
    "_execute_dynamic_workflow",
    "_execute_workflow",
    "_first_json_object",
    "_inspect_approved_plan_runtime",
    "_inspect_candidate_managed_state",
    "_inspect_model_configuration",
    "_inspect_selected_model",
    "_is_openclaw_host_peer",
    "_list_teams",
    "_load_final_report",
    "_load_json_model",
    "_load_user_configuration",
    "_managed_engine_binding",
    "_managed_install_context",
    "_merge_discovered_model_metadata",
    "_plan_execution_checkpoint",
    "_preflight",
    "_prepare_benchmark",
    "_prepare_runtime_boundary",
    "_print_configuration",
    "_product_planning_policy",
    "_prompt_context_window",
    "_prompt_nonnegative_decimal",
    "_prompt_optional_run_deadline",
    "_prompt_product_text",
    "_prompt_task_cost_ceiling",
    "_prompt_value",
    "_prompt_yes_no",
    "_provider_error_fields",
    "_provider_smoke_failure_detail",
    "_read_natural_text",
    "_read_private_openclaw_configuration",
    "_read_short",
    "_record_managed_image_transition",
    "_render_managed_change",
    "_render_model_inspection_start",
    "_render_product_outcome",
    "_replace_exact_private_file",
    "_replace_profile",
    "_replacement_planning_request",
    "_resolve_managed_change",
    "_restore_user_configuration",
    "_run_openclaw_configuration",
    "_run_product",
    "_run_product_planning",
    "_run_provider_smoke",
    "_run_workflow",
    "_sanitize_provider_smoke_detail",
    "_show_channel",
    "_show_version",
    "_software_version_report",
    "_split_configuration_assignment",
    "_staged_npm_link_target",
    "_staged_openclaw_state",
    "_switch_channel",
    "_task_admission_checkpoint",
    "_timeout_flag",
    "_update_managed_install",
    "_validate_artifact",
    "_validate_config",
    "_validate_handoff",
    "_validate_optional_openclaw_configuration",
    "_validate_task_brief",
    "_with_team_plan_model_inspections",
    "bind_run_task_brief_mount",
    "bound_docker_engine",
    "build_parser",
    "build_plan_execution_report",
    "build_task_admission_report",
    "cleanup_run_sandbox_containers",
    "deliver_product_workspace",
    "ensure_product_state",
    "finalize_staged_sandbox_image_transition",
    "generate_product_run_id",
    "inspect_openclaw_model",
    "inspect_persisted_schema_compatibility",
    "inspect_runtime_preflight",
    "inspect_sandbox_image",
    "inspect_software_version",
    "inspect_startup_environment",
    "inspect_task_admission_update",
    "install_managed_target",
    "installation_record_path",
    "is_owned_openclaw_runtime",
    "is_retired_managed_application",
    "isolated_openclaw_environment",
    "limit_concurrency_to_host_capacity",
    "load_project_commands",
    "load_quality_gate_configuration",
    "load_team_manifest",
    "main",
    "managed_foreground_task_lease",
    "materialize_run_configuration",
    "migrate_copied_provider_databases",
    "openclaw_invocation_thinking_level",
    "os",
    "parse_phase_artifact",
    "prepare_benchmark_seed",
    "prepare_product_source",
    "prepare_staged_sandbox_image_transition",
    "promote_legacy_docker_engine_record",
    "reconcile_pending_sandbox_image_transition",
    "render_self_check_report",
    "render_short_version",
    "render_startup_diagnostics",
    "render_version_report",
    "resolve_bootstrap_model_profiles",
    "resolve_dev_target",
    "resolve_latest_stable_release",
    "resolve_requested_target",
    "run_bounded_process",
    "run_interactive_planning",
    "runtime_profile_for_model",
    "runtime_profile_from_openclaw_configuration",
    "save_user_configuration",
    "subprocess",
    "sys",
    "target_from_stable_release",
    "tempfile",
    "terminal_color_enabled",
    "user_configuration_path",
    "user_state_root",
    "validate_current_managed_install",
    "validate_decision_limit_registry",
    "validate_environment_configuration",
    "validate_project_destination",
]
