"""Command-line entry point for configuration and the Agent-team harness."""

import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, ValidationError

from software_agent_team.artifacts import (
    CheckStatus,
    CommandEvidence,
    FinalReport,
    HandoffEnvelope,
    TaskBrief,
    parse_phase_artifact,
)
from software_agent_team.benchmark_seed import prepare_benchmark_seed
from software_agent_team.budgets import ModelPricing
from software_agent_team.configuration import validate_environment_configuration
from software_agent_team.execution import OpenClawSubprocessExecutor
from software_agent_team.git_workspace import GitWorkspace, GitWorkspaceManager
from software_agent_team.openclaw_runtime import isolated_openclaw_environment
from software_agent_team.paths import user_state_root
from software_agent_team.product import (
    ProductFlowError,
    ProductStatePaths,
    build_product_task_brief,
    deliver_product_workspace,
    ensure_product_state,
    generate_product_run_id,
    inspect_startup_environment,
    load_project_commands,
    prepare_product_source,
    render_startup_diagnostics,
    validate_project_destination,
)
from software_agent_team.progress import (
    ProgressEvent,
    ProgressEventKind,
    ProgressHandler,
    TerminalProgressRenderer,
)
from software_agent_team.quality_gates import (
    DockerSandboxBackend,
    QualityGateRunner,
    load_quality_gate_configuration,
)
from software_agent_team.run_control import RunPhase
from software_agent_team.runtime_configuration import (
    OpenClawModelInspection,
    RuntimeConfigurationError,
    has_model_compatibility,
    inspect_openclaw_model,
    inspect_runtime_preflight,
    inspect_sandbox_image,
    materialize_model_check_configuration,
    materialize_run_configuration,
    persist_runtime_preflight,
)
from software_agent_team.sandbox_lifecycle import cleanup_run_sandbox_containers
from software_agent_team.teams import load_team_manifest
from software_agent_team.user_configuration import (
    UserConfiguration,
    load_user_configuration,
    save_user_configuration,
    user_configuration_path,
)
from software_agent_team.workflow import WorkflowCoordinator, WorkflowOutcome

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TEAM_CONFIG = PROJECT_ROOT / "configs/teams.json"
DEFAULT_OPENCLAW_CONFIG = PROJECT_ROOT / "configs/openclaw.example.json5"
DEFAULT_RUN_POLICY = PROJECT_ROOT / "configs/run-policy.json"
DEFAULT_BENCHMARK = PROJECT_ROOT / "benchmarks/task_manager/benchmark.json"
DEFAULT_BENCHMARK_SEED = PROJECT_ROOT / "benchmarks/task_manager/seed"
DEFAULT_PRODUCT_POLICY = PROJECT_ROOT / "configs/product-policy.json"
DEFAULT_PRODUCT_PROFILE = PROJECT_ROOT / "profiles/python/quality.json"
DEFAULT_PRODUCT_SEED = PROJECT_ROOT / "profiles/python/seed"
DEFAULT_STATE_ROOT = user_state_root()
DEFAULT_RUNS_ROOT = DEFAULT_STATE_ROOT / "runs"
DEFAULT_WORKSPACES_ROOT = DEFAULT_STATE_ROOT / "workspaces"
DEFAULT_OPENCLAW_BINARY = PROJECT_ROOT / ".sat" / "openclaw" / "bin" / "openclaw"
EVALUATION_ITERATION_LIMIT = 2
PRODUCT_ITERATION_LIMIT = 3


@dataclass(frozen=True)
class _WorkflowLaunchOptions:
    """Resolved internal inputs shared by product and evaluation launches."""

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
    sandbox_binary: str
    model: str
    input_cost_per_million_usd: Decimal | None
    output_cost_per_million_usd: Decimal | None
    stage_timeout_seconds: int | None
    artifact_repair_limit: int
    iteration_limit: int
    verification_concurrency: int
    progress_handler: ProgressHandler | None = None


def _print_configuration(configuration: UserConfiguration, path: Path) -> None:
    input_cost = configuration.input_cost_per_million_usd
    output_cost = configuration.output_cost_per_million_usd
    print(f"configuration: {path}")
    print(f"model: {configuration.model}")
    print(
        "input cost per million tokens (USD): "
        f"{input_cost if input_cost is not None else 'not configured'}"
    )
    print(
        "output cost per million tokens (USD): "
        f"{output_cost if output_cost is not None else 'not configured'}"
    )
    print(f"verification concurrency: {configuration.verification_concurrency}")
    timeout = configuration.stage_timeout_seconds
    print(
        "global Agent stage timeout override (seconds): "
        f"{timeout if timeout is not None else 'role defaults'}"
    )


def _load_user_configuration(path: Path | None = None) -> UserConfiguration | None:
    """Load defaults while making one-way configuration migration visible."""

    return load_user_configuration(
        path,
        on_migration=lambda message: print(f"configuration migration: {message}"),
    )


def _timeout_flag(args: argparse.Namespace) -> tuple[bool, int | None]:
    """Resolve new and deprecated timeout flags without parallel semantics."""

    if args.use_role_timeouts:
        return True, None
    if args.stage_timeout_seconds is not None:
        return True, args.stage_timeout_seconds
    if args.deprecated_agent_timeout_seconds is not None:
        print(
            "warning: --agent-timeout-seconds is deprecated; it now means one "
            "shared stage budget including response repair. Use "
            "--stage-timeout-seconds instead."
        )
        return True, args.deprecated_agent_timeout_seconds
    return False, None


def _prompt_value(label: str, current: object | None = None) -> str:
    suffix = "" if current is None else f" [{current}]"
    response = input(f"{label}{suffix}: ").strip()
    if response:
        return response
    if current is None:
        raise ValueError(f"{label} is required")
    return str(current)


def _prompt_product_text(
    prompt: str,
    *,
    label: str,
    required: bool = False,
    maximum_length: int | None = None,
) -> str:
    """Read one product answer without accepting undecodable terminal bytes."""

    while True:
        response = input(prompt).strip()
        try:
            response.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            print(f"Invalid terminal text in {label}; please type it again.")
            continue
        if required and not response:
            print(f"{label.capitalize()} must not be blank; please type it again.")
            continue
        if maximum_length is not None and len(response) > maximum_length:
            print(
                f"{label.capitalize()} must be at most {maximum_length} "
                "characters; please shorten it."
            )
            continue
        return response


def _configure(args: argparse.Namespace) -> int:
    """Create or replace non-secret user run defaults."""

    path = user_configuration_path()
    current = _load_user_configuration(path)
    timeout_supplied, supplied_timeout = _timeout_flag(args)
    supplied = (
        any(
            value is not None
            for value in (
                args.model,
                args.input_cost_per_million_usd,
                args.output_cost_per_million_usd,
                args.verification_concurrency,
                supplied_timeout,
            )
        )
        or timeout_supplied
    )
    if args.show:
        if supplied:
            raise ValueError("--show cannot be combined with configuration values")
        if current is None:
            print(f"configuration: not configured ({path})")
            print("next: run sat in an interactive terminal")
        else:
            _print_configuration(current, path)
            print("next: enter a project parent directory and run sat")
        return 0

    interactive = not args.non_interactive and sys.stdin.isatty() and not supplied
    if interactive:
        state_paths = ProductStatePaths.below(user_state_root())
        ensure_product_state(state_paths)
        openclaw_config = state_paths.openclaw / "openclaw.json"
        if openclaw_config.is_symlink():
            raise RuntimeConfigurationError(
                "SAT OpenClaw configuration must not be a symbolic link"
            )
        print("SAT uses its own isolated OpenClaw runtime and provider state.")
        print("Existing OpenClaw installations and configuration are never used.")
        if _prompt_yes_no(
            "Open SAT's isolated OpenClaw provider setup now?",
            default=not openclaw_config.is_file(),
        ):
            _run_openclaw_configuration(
                DEFAULT_OPENCLAW_BINARY,
                state_dir=state_paths.openclaw,
                config_path=openclaw_config,
            )
        discovered_model = _discover_openclaw_default_model(
            DEFAULT_OPENCLAW_BINARY,
            state_dir=state_paths.openclaw,
            config_path=openclaw_config,
        )
        if discovered_model is not None:
            print(f"OpenClaw default model detected: {discovered_model}")
        print("Press Enter to keep a value shown in brackets.")
        default_model = discovered_model or (current.model if current else None)
        model = _prompt_value(
            "OpenClaw model reference",
            default_model,
        )
        same_model = current is not None and model == current.model
        input_cost = current.input_cost_per_million_usd if same_model else None
        output_cost = current.output_cost_per_million_usd if same_model else None
        concurrency = current.verification_concurrency if current is not None else 1
        timeout = current.stage_timeout_seconds if current is not None else None
    else:
        if not supplied:
            raise ValueError(
                "interactive configuration requires a terminal; supply configuration "
                "flags or use --show"
            )
        model = (
            args.model if args.model is not None else current.model if current else None
        )
        price_flags = (
            args.input_cost_per_million_usd is not None,
            args.output_cost_per_million_usd is not None,
        )
        if price_flags[0] != price_flags[1]:
            raise ValueError("input and output price flags must be supplied together")
        model_changed = (
            current is not None and args.model is not None and model != current.model
        )
        if all(price_flags):
            input_cost = args.input_cost_per_million_usd
            output_cost = args.output_cost_per_million_usd
        elif current is not None and not model_changed:
            input_cost = current.input_cost_per_million_usd
            output_cost = current.output_cost_per_million_usd
        else:
            input_cost = None
            output_cost = None
        concurrency = (
            args.verification_concurrency
            if args.verification_concurrency is not None
            else current.verification_concurrency
            if current
            else 1
        )
        timeout = (
            supplied_timeout
            if timeout_supplied
            else current.stage_timeout_seconds
            if current
            else None
        )
        if model is None:
            raise ValueError(
                "first-time non-interactive configuration requires --model"
            )

    configuration = UserConfiguration(
        model=model,
        input_cost_per_million_usd=input_cost,
        output_cost_per_million_usd=output_cost,
        verification_concurrency=concurrency,
        stage_timeout_seconds=timeout,
    )
    if interactive:
        inspection = _inspect_selected_model(
            DEFAULT_OPENCLAW_BINARY,
            configuration.model,
            state_dir=state_paths.openclaw,
            config_path=openclaw_config,
        )
        if not inspection.available:
            raise RuntimeConfigurationError(
                f"selected model is not locally ready: {inspection.error}"
            )
    save_user_configuration(configuration, path)
    print("configuration saved")
    _print_configuration(configuration, path)
    print("provider credentials: not stored by SAT")
    print("next: enter a project parent directory and run sat")
    return 0


def _load_json_model[ModelT: BaseModel](path: Path, model: type[ModelT]) -> ModelT:
    """Read a JSON file and validate it as a Pydantic model."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    return model.model_validate(payload)


def _validate_handoff(args: argparse.Namespace) -> int:
    handoff = _load_json_model(args.path, HandoffEnvelope)
    manifest = load_team_manifest(args.teams)
    manifest.validate_handoff_boundary(
        team_id=handoff.team_id,
        iteration=handoff.iteration,
        source_role=handoff.source_role,
        target_role=handoff.target_role,
    )
    print(
        "valid handoff: "
        f"run={handoff.run_id} team={handoff.team_id} "
        f"iteration={handoff.iteration} source={handoff.source_role}"
    )
    return 0


def _validate_task_brief(args: argparse.Namespace) -> int:
    task_brief = _load_json_model(args.path, TaskBrief)
    state = "confirmed" if task_brief.confirmed else "draft"
    print(
        f"valid task brief: run={task_brief.run_id} "
        f"criteria={len(task_brief.acceptance_criteria)} state={state}"
    )
    return 0


def _validate_artifact(args: argparse.Namespace) -> int:
    payload = json.loads(args.path.read_text(encoding="utf-8"))
    artifact = parse_phase_artifact(payload)
    iteration = getattr(artifact, "iteration", None)
    suffix = "" if iteration is None else f" iteration={iteration}"
    print(
        f"valid artifact: kind={artifact.kind.value} run={artifact.run_id}"
        f" team={artifact.team_id}{suffix}"
    )
    return 0


def _validate_config(args: argparse.Namespace) -> int:
    manifest, _ = validate_environment_configuration(args.teams, args.openclaw)
    quality_manifest = args.quality_manifest or args.benchmark or DEFAULT_BENCHMARK
    quality = load_quality_gate_configuration(args.policy, quality_manifest)
    print(
        "valid configuration: "
        f"teams={len(manifest.teams)} roles={len(manifest.required_roles)} "
        f"default={manifest.default_team} policy={quality.policy.id} "
        f"quality_manifest={quality.manifest.id} gates={len(quality.manifest.gates)}"
    )
    return 0


def _list_teams(args: argparse.Namespace) -> int:
    manifest = load_team_manifest(args.config)
    for team in manifest.teams:
        marker = "*" if team.id == manifest.default_team else " "
        roles = ",".join(role.value for role in team.roles)
        print(f"{marker} {team.id}: {roles}")
    return 0


def _prepare_benchmark(args: argparse.Namespace) -> int:
    commit = prepare_benchmark_seed(
        args.seed,
        args.destination,
        author_name=args.author_name,
        author_email=args.author_email,
    )
    print(f"prepared benchmark: path={args.destination} commit={commit}")
    return 0


def _preflight(args: argparse.Namespace) -> int:
    state_paths = ProductStatePaths.below(user_state_root())
    ensure_product_state(state_paths)
    manifest = load_team_manifest(args.teams)
    configuration = load_quality_gate_configuration(args.policy, args.benchmark)
    with tempfile.TemporaryDirectory(prefix="sat-preflight-") as temporary:
        temporary_root = Path(temporary)
        source_commit = GitWorkspaceManager(
            temporary_root / "workspaces"
        ).validate_source_repository(
            args.source_repository,
            base_ref=args.base_ref,
        )
        runtime_config = temporary_root / "openclaw.runtime.json"
        limits = configuration.policy.limits
        materialize_run_configuration(
            DEFAULT_OPENCLAW_CONFIG,
            runtime_config,
            manifest=manifest,
            workspace=args.source_repository,
            sandbox_image=configuration.policy.sandbox.image,
            sandbox_memory_mb=limits.memory_mb,
            sandbox_cpus=limits.cpu_cores,
            sandbox_pids_limit=limits.pids,
            sandbox_open_files=limits.open_files,
            sandbox_tmpfs_mb=limits.writable_tmpfs_mb,
        )
        result = inspect_runtime_preflight(
            openclaw_binary=DEFAULT_OPENCLAW_BINARY,
            openclaw_state_dir=state_paths.openclaw,
            runtime_config=runtime_config,
            sandbox_binary=args.sandbox_binary,
            sandbox_image=configuration.policy.sandbox.image,
        )
    state = "ready" if result.ready else "not-ready"
    print(
        f"runtime preflight: {state} openclaw={result.openclaw_version} "
        f"config={result.config_valid} image={result.sandbox_image_present} "
        f"container={result.sandbox_container_ready} "
        f"image_id={result.sandbox_image_id or 'none'} "
        f"source_commit={source_commit}"
    )
    return 0 if result.ready else 2


def _execute_workflow(
    task_brief: TaskBrief,
    options: _WorkflowLaunchOptions,
) -> WorkflowOutcome:
    """Execute one confirmed brief through the shared deterministic engine."""

    manifest = load_team_manifest(options.teams)
    configuration = load_quality_gate_configuration(
        options.policy,
        options.quality_manifest,
    )
    sandbox_inspection = inspect_sandbox_image(
        sandbox_binary=options.sandbox_binary,
        sandbox_image=configuration.policy.sandbox.image,
    )
    if not sandbox_inspection.ready or sandbox_inspection.sandbox_image_id is None:
        raise RuntimeConfigurationError(
            "the configured sandbox image is not present locally"
        )
    frozen_sandbox_image = sandbox_inspection.sandbox_image_id
    runtime_path = options.runs_root / task_brief.run_id / "openclaw.runtime.json"
    openclaw_environment = isolated_openclaw_environment(
        state_dir=options.openclaw_state_dir,
        config_path=runtime_path,
    )
    executor = OpenClawSubprocessExecutor(
        openclaw_binary=options.openclaw_binary,
        environment=openclaw_environment,
        local=True,
    )

    def runtime_setup(workspace: GitWorkspace, run_directory: Path) -> None:
        workspace_path = workspace.workspace_path
        limits = configuration.policy.limits
        materialize_run_configuration(
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
            model=options.model,
        )
        preflight = inspect_runtime_preflight(
            openclaw_binary=options.openclaw_binary,
            openclaw_state_dir=options.openclaw_state_dir,
            runtime_config=runtime_path,
            sandbox_binary=options.sandbox_binary,
            sandbox_image=configuration.policy.sandbox.image,
            expected_sandbox_image_id=frozen_sandbox_image,
            expected_model=options.model,
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
                f"model_error={preflight.model_error or 'none'}"
            )

    def report_gate(
        command: CommandEvidence,
        iteration: int,
        completed: int,
        total: int,
    ) -> None:
        if options.progress_handler is None:
            return
        passed = command.exit_code == 0 and not command.timed_out
        state = "passed" if passed else "failed"
        options.progress_handler(
            ProgressEvent(
                kind=ProgressEventKind.QUALITY_GATE_COMPLETED,
                message=(f"Quality gate {completed}/{total} {command.id}: {state}"),
                phase=RunPhase.VERIFYING,
                iteration=iteration,
                completed=completed,
                total=total,
            )
        )

    def gate_factory(run_directory: Path, workspace: Path) -> QualityGateRunner:
        return QualityGateRunner(
            configuration,
            run_directory=run_directory,
            workspace=workspace,
            sandbox_image_id=frozen_sandbox_image,
            backend=DockerSandboxBackend(options.sandbox_binary),
            result_handler=report_gate,
        )

    coordinator = WorkflowCoordinator(
        manifest=manifest,
        runs_root=options.runs_root,
        workspaces_root=options.workspaces_root,
        executor=executor,
        quality_gate_factory=gate_factory,
        budget=configuration.policy.agent_budget,
        pricing=ModelPricing(
            model=options.model,
            input_cost_per_million_usd=options.input_cost_per_million_usd,
            output_cost_per_million_usd=options.output_cost_per_million_usd,
        ),
        runtime_setup=runtime_setup,
        manual_review_criteria=configuration.manifest.manual_review_criteria,
        role_timeout_seconds=configuration.policy.agent_stage_timeouts_seconds,
        stage_timeout_seconds=options.stage_timeout_seconds,
        artifact_repair_limit=options.artifact_repair_limit,
        iteration_limit=options.iteration_limit,
        verification_concurrency=options.verification_concurrency,
        progress_handler=options.progress_handler,
    )
    cleanup_roles = tuple(manifest.get_team(manifest.default_team).roles)
    try:
        outcome = coordinator.execute(
            task_brief,
            source_repository=options.source_repository,
            base_ref=options.base_ref,
        )
    except BaseException as error:
        try:
            cleanup_run_sandbox_containers(
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

    cleanup = cleanup_run_sandbox_containers(
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
        user_configuration = _load_user_configuration()

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
        if user_configuration is not None
        else None
    )
    output_cost = (
        args.output_cost_per_million_usd
        if args.output_cost_per_million_usd is not None
        else user_configuration.output_cost_per_million_usd
        if user_configuration is not None
        else None
    )
    timeout = (
        supplied_timeout
        if timeout_supplied
        else user_configuration.stage_timeout_seconds
        if user_configuration is not None
        else None
    )
    concurrency = (
        args.verification_concurrency
        if args.verification_concurrency is not None
        else user_configuration.verification_concurrency
        if user_configuration is not None
        else 2
    )
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
    expected_brief = configuration.task_brief.model_copy(
        update={"run_id": task_brief.run_id}
    )
    if task_brief != expected_brief:
        raise ValueError(
            "Phase 1 run permits only run_id to differ from the frozen TaskBrief"
        )
    outcome = _execute_workflow(
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
            openclaw_binary=DEFAULT_OPENCLAW_BINARY,
            openclaw_state_dir=state_paths.openclaw,
            sandbox_binary=args.sandbox_binary,
            model=model,
            input_cost_per_million_usd=input_cost,
            output_cost_per_million_usd=output_cost,
            stage_timeout_seconds=timeout,
            artifact_repair_limit=args.artifact_repair_limit,
            iteration_limit=EVALUATION_ITERATION_LIMIT,
            verification_concurrency=concurrency,
        ),
    )
    print(
        f"run {outcome.record.phase.value}: run={outcome.record.run_id} "
        f"commit={outcome.record.current_commit or 'none'} "
        f"report={args.runs_root / outcome.record.run_id / outcome.human_report_path}"
    )
    return 0 if outcome.record.phase is RunPhase.COMPLETED else 2


def _prompt_yes_no(label: str, *, default: bool) -> bool:
    """Read one explicit bounded confirmation from an interactive terminal."""

    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        answer = input(f"{label} {suffix} ").strip().casefold()
        if not answer:
            return default
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        print("Please answer y or n.")


def _run_openclaw_configuration(
    openclaw_binary: Path,
    *,
    state_dir: Path,
    config_path: Path,
) -> None:
    """Delegate credential entry to OpenClaw's trusted interactive boundary."""

    try:
        result = subprocess.run(
            [str(openclaw_binary), "configure", "--section", "model"],
            check=False,
            env={
                **os.environ,
                **isolated_openclaw_environment(
                    state_dir=state_dir,
                    config_path=config_path,
                ),
            },
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeConfigurationError(
            "OpenClaw provider configuration could not start"
        ) from error
    if result.returncode != 0:
        raise RuntimeConfigurationError(
            "OpenClaw provider configuration did not complete successfully"
        )


@contextmanager
def _effective_model_configuration(
    *,
    model: str,
    configured_path: Path,
) -> Iterator[Path]:
    """Yield the private config plus any pinned model-catalog supplement."""

    if not has_model_compatibility(model) and configured_path.is_file():
        yield configured_path
        return
    with tempfile.TemporaryDirectory(prefix="sat-model-check-") as temporary:
        compatibility_path = Path(temporary) / "openclaw.model.json"
        materialize_model_check_configuration(
            compatibility_path,
            model=model,
        )
        yield compatibility_path


def _inspect_selected_model(
    openclaw_binary: Path,
    model: str,
    *,
    state_dir: Path,
    config_path: Path,
) -> OpenClawModelInspection:
    """Inspect the exact effective model without making a provider request."""

    with _effective_model_configuration(
        model=model,
        configured_path=config_path,
    ) as effective_config:
        return inspect_openclaw_model(
            openclaw_binary=openclaw_binary,
            openclaw_state_dir=state_dir,
            config_path=effective_config,
            model=model,
        )


def _run_provider_smoke(
    openclaw_binary: Path,
    model: str,
    *,
    state_dir: Path,
    config_path: Path,
) -> None:
    """Run one explicitly authorized minimal provider request."""

    with _effective_model_configuration(
        model=model,
        configured_path=config_path,
    ) as effective_config:
        try:
            result = subprocess.run(
                [
                    str(openclaw_binary),
                    "infer",
                    "model",
                    "run",
                    "--local",
                    "--model",
                    model,
                    "--prompt",
                    'Reply with exactly: {"status":"ok"}',
                    "--json",
                ],
                check=False,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=180,
                env={
                    **os.environ,
                    **isolated_openclaw_environment(
                        state_dir=state_dir,
                        config_path=effective_config,
                    ),
                },
                shell=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise RuntimeConfigurationError(
                "the provider smoke check could not run"
            ) from error
    if result.returncode != 0:
        raise RuntimeConfigurationError("the provider smoke check failed")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeConfigurationError(
            "the provider smoke check returned invalid JSON"
        ) from error
    outputs = payload.get("outputs") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or payload.get("ok") is not True
        or not isinstance(outputs, list)
        or not outputs
    ):
        raise RuntimeConfigurationError(
            "the provider smoke check did not return a successful response"
        )


def _discover_openclaw_default_model(
    openclaw_binary: Path,
    *,
    state_dir: Path,
    config_path: Path,
) -> str | None:
    """Read OpenClaw's local default model without probing the provider."""

    try:
        result = subprocess.run(
            [str(openclaw_binary), "models", "status", "--json"],
            check=False,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=30,
            env={
                **os.environ,
                **isolated_openclaw_environment(
                    state_dir=state_dir,
                    config_path=config_path,
                ),
            },
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    for key in ("resolvedDefault", "defaultModel"):
        model = payload.get(key)
        if isinstance(model, str):
            try:
                return UserConfiguration(model=model).model
            except ValidationError:
                continue
    return None


def _ensure_product_configuration(
    state_paths: ProductStatePaths,
) -> UserConfiguration:
    """Load or guide the first secret-free product configuration."""

    path = user_configuration_path()
    current = _load_user_configuration(path)
    openclaw_config = state_paths.openclaw / "openclaw.json"
    if openclaw_config.is_symlink():
        raise RuntimeConfigurationError(
            "SAT OpenClaw configuration must not be a symbolic link"
        )
    if current is not None and openclaw_config.is_file():
        inspection = _inspect_selected_model(
            DEFAULT_OPENCLAW_BINARY,
            current.model,
            state_dir=state_paths.openclaw,
            config_path=openclaw_config,
        )
        if inspection.available:
            print(f"✓ Model configuration: {current.model}")
            print(f"✓ Isolated OpenClaw state: {state_paths.openclaw}")
            return current
        print(f"! Saved model is not locally ready: {current.model}")
        print(f"  {inspection.error}")

    print("\nFirst-run model setup")
    print("SAT uses an isolated OpenClaw runtime and private provider state.")
    print(
        "Existing OpenClaw installations, credentials, and configuration "
        "stay untouched."
    )
    if current is not None:
        print(f"Saved SAT model reference: {current.model}")
    if _prompt_yes_no(
        "Open SAT's isolated OpenClaw provider setup now? "
        "Choose no only when credentials come from the current shell environment.",
        default=True,
    ):
        _run_openclaw_configuration(
            DEFAULT_OPENCLAW_BINARY,
            state_dir=state_paths.openclaw,
            config_path=openclaw_config,
        )
    discovered_model = _discover_openclaw_default_model(
        DEFAULT_OPENCLAW_BINARY,
        state_dir=state_paths.openclaw,
        config_path=openclaw_config,
    )
    if discovered_model is not None:
        print(f"✓ OpenClaw default model detected: {discovered_model}")
    default_model = discovered_model or (current.model if current else None)
    model = _prompt_value(
        "OpenClaw model reference (provider/model)",
        default_model,
    )
    if current is not None and model == current.model:
        configuration = current
    else:
        configuration = UserConfiguration(
            model=model,
            verification_concurrency=(
                current.verification_concurrency if current is not None else 1
            ),
            stage_timeout_seconds=(
                current.stage_timeout_seconds if current is not None else None
            ),
        )
    inspection = _inspect_selected_model(
        DEFAULT_OPENCLAW_BINARY,
        configuration.model,
        state_dir=state_paths.openclaw,
        config_path=openclaw_config,
    )
    if not inspection.available:
        raise RuntimeConfigurationError(
            f"selected model is not locally ready: {inspection.error}"
        )
    save_user_configuration(configuration, path)
    print(f"✓ Saved secret-free model configuration to {path}")
    if _prompt_yes_no(
        "Run one minimal provider check now? This may incur provider usage.",
        default=False,
    ):
        _run_provider_smoke(
            DEFAULT_OPENCLAW_BINARY,
            configuration.model,
            state_dir=state_paths.openclaw,
            config_path=openclaw_config,
        )
        print("✓ Provider check completed")
    return configuration


def _collect_product_request(
    *,
    working_directory: Path,
    run_id: str,
) -> tuple[TaskBrief, Path] | None:
    """Collect and confirm one user-owned request for the current runtime profile."""

    print("\nWhat would you like to build?")
    source_request = _prompt_product_text(
        "> ",
        label="software request",
        required=True,
        maximum_length=2000,
    )

    print("\nCurrent execution profile")
    print("  A small greenfield Python 3.12 project.")
    print("  Web apps, CLI tools, and local automation are supported.")
    print("  The quality sandbox has no network access or external services.")
    if not _prompt_yes_no(
        "Build your request with this execution profile?",
        default=True,
    ):
        print(
            "No build was started. This release will not silently change your request."
        )
        return None

    success_text = _prompt_product_text(
        "Success conditions (separate multiple items with semicolons) "
        "[use the request as written]: ",
        label="success conditions",
    )
    constraint_text = _prompt_product_text(
        "Additional constraints (separate multiple items with semicolons) [none]: ",
        label="additional constraints",
    )
    if constraint_text.casefold() in {"none", "n/a"}:
        constraint_text = ""
    success_conditions = tuple(
        item.strip() for item in success_text.split(";") if item.strip()
    )
    constraints = tuple(
        item.strip() for item in constraint_text.split(";") if item.strip()
    )

    while True:
        project_name = _prompt_product_text(
            "Project directory [software-project]: ",
            label="project directory",
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
    task_brief = build_product_task_brief(
        run_id=run_id,
        project_name=project_name,
        source_request=source_request,
        success_conditions=success_conditions,
        user_constraints=constraints,
    )
    print("\nRequirements summary")
    print(f"  Request: {task_brief.source_request}")
    print("  Profile: local Python 3.12 project")
    print("  Success conditions:")
    for item in success_conditions or (task_brief.source_request,):
        print(f"    - {item}")
    if constraints:
        print("  Additional constraints:")
        for item in constraints:
            print(f"    - {item}")
    print(f"  Destination: {destination}")
    print("  Verification: project contract, compile, lint, tests, and review")
    print("  Provider: multiple model requests may be made")
    print("  SAT cannot determine your organization's model or Docker policy.")
    if not _prompt_yes_no(
        "Start the model-backed build with these requirements?",
        default=False,
    ):
        print("Build cancelled; no model request was made.")
        return None
    return task_brief, destination


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
    outcome: WorkflowOutcome,
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


def _run_product() -> int:
    """Run the primary diagnostics-to-delivery product journey."""

    if not sys.stdin.isatty():
        raise ValueError(
            "the guided product flow requires an interactive terminal; "
            "use an explicit subcommand for automation"
        )

    quality = load_quality_gate_configuration(
        DEFAULT_PRODUCT_POLICY,
        DEFAULT_PRODUCT_PROFILE,
    )
    working_directory = Path.cwd()
    diagnostics = inspect_startup_environment(
        working_directory=working_directory,
        openclaw_binary=DEFAULT_OPENCLAW_BINARY,
        sandbox_image=quality.policy.sandbox.image,
    )
    render_startup_diagnostics(diagnostics)
    if not diagnostics.ready:
        print("\nSAT is not ready. Complete the actions above and run sat again.")
        return 2

    state_paths = ProductStatePaths.below(user_state_root())
    ensure_product_state(state_paths)
    configuration = _ensure_product_configuration(state_paths)
    run_id = generate_product_run_id()
    request = _collect_product_request(
        working_directory=working_directory,
        run_id=run_id,
    )
    if request is None:
        return 0
    task_brief, destination = request

    source_repository = prepare_product_source(
        seed=DEFAULT_PRODUCT_SEED,
        state_paths=state_paths,
        run_id=run_id,
    )
    renderer = TerminalProgressRenderer()
    try:
        outcome = _execute_workflow(
            task_brief,
            _WorkflowLaunchOptions(
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
                sandbox_binary="docker",
                model=configuration.model,
                input_cost_per_million_usd=(configuration.input_cost_per_million_usd),
                output_cost_per_million_usd=(configuration.output_cost_per_million_usd),
                stage_timeout_seconds=configuration.stage_timeout_seconds,
                artifact_repair_limit=1,
                iteration_limit=PRODUCT_ITERATION_LIMIT,
                verification_concurrency=configuration.verification_concurrency,
                progress_handler=renderer,
            ),
        )
    finally:
        renderer.close()

    report_path = state_paths.runs / run_id / outcome.final_report.path
    report = _load_final_report(
        report_path,
        expected_sha256=outcome.final_report.sha256,
    )
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
        delivered = deliver_product_workspace(
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
    project_commands = load_project_commands(delivered)
    _render_product_outcome(
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


def build_parser() -> argparse.ArgumentParser:
    """Build the product CLI parser for implemented commands."""

    parser = argparse.ArgumentParser(
        prog="sat",
        description=(
            "Build a confirmed software request with a guided Agent team, "
            "or use an explicit subcommand for configuration and evaluation."
        ),
    )
    commands = parser.add_subparsers(dest="command")

    configure = commands.add_parser(
        "configure",
        help="Create or replace secret-free model and advanced run defaults.",
    )
    configure.add_argument(
        "--show",
        action="store_true",
        help="Show saved defaults and the run guide without changing anything.",
    )
    configure.add_argument(
        "--non-interactive",
        action="store_true",
        help="Never prompt; useful when supplying configuration flags in scripts.",
    )
    configure.add_argument("--model")
    configure.add_argument("--input-cost-per-million-usd", type=Decimal)
    configure.add_argument("--output-cost-per-million-usd", type=Decimal)
    configure.add_argument(
        "--verification-concurrency",
        type=int,
        choices=(1, 2),
    )
    configure_timeout = configure.add_mutually_exclusive_group()
    configure_timeout.add_argument(
        "--stage-timeout-seconds",
        type=int,
        help=(
            "Optional global budget for one role stage, including response repair; "
            "otherwise use checked-in role defaults."
        ),
    )
    configure_timeout.add_argument(
        "--agent-timeout-seconds",
        dest="deprecated_agent_timeout_seconds",
        type=int,
        help=argparse.SUPPRESS,
    )
    configure_timeout.add_argument(
        "--use-role-timeouts",
        action="store_true",
        help="Clear the global override and use checked-in per-role stage budgets.",
    )
    configure.set_defaults(handler=_configure)

    handoff = commands.add_parser(
        "validate-handoff",
        help="Validate a persisted handoff envelope.",
    )
    handoff.add_argument("path", type=Path)
    handoff.add_argument("--teams", type=Path, default=DEFAULT_TEAM_CONFIG)
    handoff.set_defaults(handler=_validate_handoff)

    task_brief = commands.add_parser(
        "validate-task-brief",
        help="Validate a clarified task brief.",
    )
    task_brief.add_argument("path", type=Path)
    task_brief.set_defaults(handler=_validate_task_brief)

    artifact = commands.add_parser(
        "validate-artifact",
        help="Validate the structure of a persisted phase artifact.",
    )
    artifact.add_argument("path", type=Path)
    artifact.set_defaults(handler=_validate_artifact)

    config = commands.add_parser(
        "validate-config",
        help="Validate all checked-in team, runtime, policy, and benchmark config.",
    )
    config.add_argument("--teams", type=Path, default=DEFAULT_TEAM_CONFIG)
    config.add_argument("--openclaw", type=Path, default=DEFAULT_OPENCLAW_CONFIG)
    config.add_argument("--policy", type=Path, default=DEFAULT_RUN_POLICY)
    quality_manifest = config.add_mutually_exclusive_group()
    quality_manifest.add_argument(
        "--quality-manifest",
        type=Path,
        help="Quality profile or evaluation manifest; defaults to the benchmark.",
    )
    quality_manifest.add_argument(
        "--benchmark",
        type=Path,
        help="Compatibility spelling for an evaluation quality manifest.",
    )
    config.set_defaults(handler=_validate_config)

    teams = commands.add_parser(
        "list-teams",
        help="List versioned experimental team configurations.",
    )
    teams.add_argument("--config", type=Path, default=DEFAULT_TEAM_CONFIG)
    teams.set_defaults(handler=_list_teams)

    benchmark = commands.add_parser(
        "prepare-benchmark",
        help="Create the fixed task-manager seed as a clean Git repository.",
    )
    benchmark.add_argument("destination", type=Path)
    benchmark.add_argument("--seed", type=Path, default=DEFAULT_BENCHMARK_SEED)
    benchmark.add_argument("--author-name", default="urntt")
    benchmark.add_argument("--author-email", default="urntts@gmail.com")
    benchmark.set_defaults(handler=_prepare_benchmark)

    preflight = commands.add_parser(
        "preflight",
        help=(
            "Check the source repository, OpenClaw configuration, and sandbox image "
            "without a model call."
        ),
    )
    preflight.add_argument("source_repository", type=Path)
    preflight.add_argument("--base-ref", default="HEAD")
    preflight.add_argument("--teams", type=Path, default=DEFAULT_TEAM_CONFIG)
    preflight.add_argument("--policy", type=Path, default=DEFAULT_RUN_POLICY)
    preflight.add_argument("--benchmark", type=Path, default=DEFAULT_BENCHMARK)
    preflight.add_argument("--sandbox-binary", default="docker")
    preflight.set_defaults(handler=_preflight)

    run = commands.add_parser(
        "run",
        help="Execute the complete function-specialized Phase 1 workflow.",
    )
    run.add_argument("task_brief", type=Path)
    run.add_argument("source_repository", type=Path)
    run.add_argument("--base-ref", default="HEAD")
    run.add_argument("--teams", type=Path, default=DEFAULT_TEAM_CONFIG)
    run.add_argument("--policy", type=Path, default=DEFAULT_RUN_POLICY)
    run.add_argument("--benchmark", type=Path, default=DEFAULT_BENCHMARK)
    run.add_argument("--runs-root", type=Path, default=DEFAULT_RUNS_ROOT)
    run.add_argument("--workspaces-root", type=Path, default=DEFAULT_WORKSPACES_ROOT)
    run.add_argument("--sandbox-binary", default="docker")
    run.add_argument(
        "--model",
        help="Exact OpenClaw model reference; defaults to 'sat configure' value.",
    )
    run.add_argument(
        "--input-cost-per-million-usd",
        type=Decimal,
        help="Exact input price; defaults to 'sat configure' value.",
    )
    run.add_argument(
        "--output-cost-per-million-usd",
        type=Decimal,
        help="Exact output price; defaults to 'sat configure' value.",
    )
    run_timeout = run.add_mutually_exclusive_group()
    run_timeout.add_argument(
        "--stage-timeout-seconds",
        type=int,
        help=(
            "Global budget for each role stage, including response repair; "
            "defaults to the saved override or checked-in per-role budgets."
        ),
    )
    run_timeout.add_argument(
        "--agent-timeout-seconds",
        dest="deprecated_agent_timeout_seconds",
        type=int,
        help=argparse.SUPPRESS,
    )
    run_timeout.add_argument(
        "--use-role-timeouts",
        action="store_true",
        help="Ignore a saved global override for this run and use role defaults.",
    )
    run.add_argument("--artifact-repair-limit", type=int, choices=(0, 1), default=1)
    run.add_argument(
        "--verification-concurrency",
        type=int,
        choices=(1, 2),
        help=(
            "Maximum concurrent Tester/Reviewer calls; defaults to the configured "
            "value or 2. Use 1 for providers with one generation slot."
        ),
    )
    run.set_defaults(handler=_run_workflow)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one CLI command and return a process exit code."""

    args = build_parser().parse_args(argv)
    try:
        if args.command is None:
            return _run_product()
        return args.handler(args)
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
