"""Command-line entry point for configuration and the Agent-team harness."""

import argparse
import json
import sys
import tempfile
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, ValidationError

from software_agent_team.artifacts import (
    HandoffEnvelope,
    TaskBrief,
    parse_phase_artifact,
)
from software_agent_team.benchmark_seed import prepare_benchmark_seed
from software_agent_team.budgets import ModelPricing
from software_agent_team.configuration import validate_environment_configuration
from software_agent_team.execution import OpenClawSubprocessExecutor
from software_agent_team.git_workspace import GitWorkspace, GitWorkspaceManager
from software_agent_team.quality_gates import (
    DockerSandboxBackend,
    QualityGateRunner,
    load_quality_gate_configuration,
)
from software_agent_team.run_control import RunPhase
from software_agent_team.runtime_configuration import (
    RuntimeConfigurationError,
    inspect_runtime_preflight,
    inspect_sandbox_image,
    materialize_run_configuration,
    persist_runtime_preflight,
)
from software_agent_team.teams import load_team_manifest
from software_agent_team.user_configuration import (
    UserConfiguration,
    load_user_configuration,
    save_user_configuration,
    user_configuration_path,
)
from software_agent_team.workflow import WorkflowCoordinator

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TEAM_CONFIG = PROJECT_ROOT / "configs/teams.json"
DEFAULT_OPENCLAW_CONFIG = PROJECT_ROOT / "configs/openclaw.example.json5"
DEFAULT_RUN_POLICY = PROJECT_ROOT / "configs/run-policy.json"
DEFAULT_BENCHMARK = PROJECT_ROOT / "benchmarks/task_manager/benchmark.json"
DEFAULT_BENCHMARK_SEED = PROJECT_ROOT / "benchmarks/task_manager/seed"
DEFAULT_TASK_BRIEF = PROJECT_ROOT / "benchmarks/task_manager/task-brief.json"
DEFAULT_RUNS_ROOT = PROJECT_ROOT / "runs"
DEFAULT_WORKSPACES_ROOT = PROJECT_ROOT / "workspaces"
DEFAULT_OPENCLAW_BINARY = Path.home() / ".openclaw" / "bin" / "openclaw"


def _print_run_guide(*, configured: bool) -> None:
    """Print the shortest safe path from installation to one live run."""

    print("\nConfigure and verify the provider (credentials stay in OpenClaw):")
    print(f"  {DEFAULT_OPENCLAW_BINARY} configure --section model")
    print(f"  {DEFAULT_OPENCLAW_BINARY} models status --check")
    if not configured:
        print("\nSave non-secret run defaults:")
        print("  sat configure")
    print("\nPrepare and check the frozen benchmark:")
    print(f"  cd {PROJECT_ROOT}")
    print("  sat prepare-benchmark ./task-manager-source")
    print("  sat preflight ./task-manager-source")
    print("\nStart the live Agent workflow (this can incur provider usage):")
    print(f"  sat run {DEFAULT_TASK_BRIEF} ./task-manager-source")
    print("\nReview saved settings at any time with: sat configure --show")
    print("Reconfigure at any time with: sat configure")
    print("Uninstall with preservation/export choices: sat-uninstall --help")


def _show_welcome() -> int:
    """Show first-launch onboarding or the next-run guide."""

    path = user_configuration_path()
    configuration = _load_user_configuration(path)
    if configuration is None:
        print("Software Agent Team is installed but not configured yet.")
        print(f"Configuration will be saved to: {path}")
        print("SAT never stores provider API keys in this file.")
        _print_run_guide(configured=False)
    else:
        print("Software Agent Team is configured and ready for preflight.")
        _print_configuration(configuration, path)
        _print_run_guide(configured=True)
    return 0


def _print_configuration(configuration: UserConfiguration, path: Path) -> None:
    print(f"configuration: {path}")
    print(f"model: {configuration.model}")
    print(
        "input cost per million tokens (USD): "
        f"{configuration.input_cost_per_million_usd}"
    )
    print(
        "output cost per million tokens (USD): "
        f"{configuration.output_cost_per_million_usd}"
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


def _prompt_stage_timeout(current: int | None) -> int | None:
    current_text: object = current if current is not None else "role defaults"
    response = input(
        f"Global Agent stage timeout in seconds, or 'role-defaults' [{current_text}]: "
    ).strip()
    if not response:
        return current
    if response.casefold() in {"role-defaults", "defaults", "default"}:
        return None
    return int(response)


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
            _print_run_guide(configured=False)
        else:
            _print_configuration(current, path)
            _print_run_guide(configured=True)
        return 0

    interactive = not args.non_interactive and sys.stdin.isatty() and not supplied
    if interactive:
        print("SAT configuration stores run defaults only, never provider credentials.")
        print("Press Enter to keep a value shown in brackets.")
        model = _prompt_value(
            "OpenClaw model reference", current.model if current else None
        )
        input_cost = _prompt_value(
            "Input cost per million tokens in USD",
            current.input_cost_per_million_usd if current else None,
        )
        output_cost = _prompt_value(
            "Output cost per million tokens in USD",
            current.output_cost_per_million_usd if current else None,
        )
        concurrency = int(
            _prompt_value(
                "Concurrent Tester/Reviewer calls (1 or 2)",
                current.verification_concurrency if current else 2,
            )
        )
        timeout = _prompt_stage_timeout(
            current.stage_timeout_seconds if current else None
        )
    else:
        if not supplied:
            raise ValueError(
                "interactive configuration requires a terminal; supply configuration "
                "flags or use --show"
            )
        model = (
            args.model if args.model is not None else current.model if current else None
        )
        input_cost = (
            args.input_cost_per_million_usd
            if args.input_cost_per_million_usd is not None
            else current.input_cost_per_million_usd
            if current
            else None
        )
        output_cost = (
            args.output_cost_per_million_usd
            if args.output_cost_per_million_usd is not None
            else current.output_cost_per_million_usd
            if current
            else None
        )
        concurrency = (
            args.verification_concurrency
            if args.verification_concurrency is not None
            else current.verification_concurrency
            if current
            else 2
        )
        timeout = (
            supplied_timeout
            if timeout_supplied
            else current.stage_timeout_seconds
            if current
            else None
        )
        missing = [
            name
            for name, value in (
                ("--model", model),
                ("--input-cost-per-million-usd", input_cost),
                ("--output-cost-per-million-usd", output_cost),
            )
            if value is None
        ]
        if missing:
            raise ValueError(
                "first-time non-interactive configuration requires "
                + ", ".join(missing)
            )

    configuration = UserConfiguration(
        model=model,
        input_cost_per_million_usd=input_cost,
        output_cost_per_million_usd=output_cost,
        verification_concurrency=concurrency,
        stage_timeout_seconds=timeout,
    )
    save_user_configuration(configuration, path)
    print("configuration saved")
    _print_configuration(configuration, path)
    print("provider credentials: not stored by SAT")
    _print_run_guide(configured=True)
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
    quality = load_quality_gate_configuration(args.policy, args.benchmark)
    print(
        "valid configuration: "
        f"teams={len(manifest.teams)} roles={len(manifest.required_roles)} "
        f"default={manifest.default_team} policy={quality.policy.id} "
        f"benchmark={quality.benchmark.id} gates={len(quality.benchmark.gates)}"
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
            args.openclaw,
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
            openclaw_binary=args.openclaw_binary,
            runtime_config=runtime_config,
            sandbox_binary=args.sandbox_binary,
            sandbox_image=configuration.policy.sandbox.image,
        )
    state = "ready" if result.ready else "not-ready"
    print(
        f"runtime preflight: {state} openclaw={result.openclaw_version} "
        f"config={result.config_valid} image={result.sandbox_image_present} "
        f"image_id={result.sandbox_image_id or 'none'} "
        f"source_commit={source_commit}"
    )
    return 0 if result.ready else 2


def _run_workflow(args: argparse.Namespace) -> int:
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
    manifest = load_team_manifest(args.teams)
    configuration = load_quality_gate_configuration(args.policy, args.benchmark)
    expected_brief = configuration.task_brief.model_copy(
        update={"run_id": task_brief.run_id}
    )
    if task_brief != expected_brief:
        raise ValueError(
            "Phase 1 run permits only run_id to differ from the frozen TaskBrief"
        )
    sandbox_inspection = inspect_sandbox_image(
        sandbox_binary=args.sandbox_binary,
        sandbox_image=configuration.policy.sandbox.image,
    )
    if not sandbox_inspection.ready or sandbox_inspection.sandbox_image_id is None:
        raise RuntimeConfigurationError(
            "the configured sandbox image is not present locally"
        )
    frozen_sandbox_image = sandbox_inspection.sandbox_image_id
    runtime_path = args.runs_root / task_brief.run_id / "openclaw.runtime.json"
    executor = OpenClawSubprocessExecutor(
        openclaw_binary=args.openclaw_binary,
        environment={"OPENCLAW_CONFIG_PATH": str(runtime_path.resolve())},
        local=True,
    )

    def runtime_setup(workspace: GitWorkspace, run_directory: Path) -> None:
        workspace_path = workspace.workspace_path
        limits = configuration.policy.limits
        materialize_run_configuration(
            args.openclaw,
            runtime_path,
            manifest=manifest,
            workspace=Path(workspace_path),
            sandbox_image=frozen_sandbox_image,
            sandbox_memory_mb=limits.memory_mb,
            sandbox_cpus=limits.cpu_cores,
            sandbox_pids_limit=limits.pids,
            sandbox_open_files=limits.open_files,
            sandbox_tmpfs_mb=limits.writable_tmpfs_mb,
            model=model,
        )
        preflight = inspect_runtime_preflight(
            openclaw_binary=args.openclaw_binary,
            runtime_config=runtime_path,
            sandbox_binary=args.sandbox_binary,
            sandbox_image=configuration.policy.sandbox.image,
            expected_sandbox_image_id=frozen_sandbox_image,
        )
        persist_runtime_preflight(
            preflight,
            run_directory / "runtime-preflight.json",
        )
        if not preflight.ready:
            raise RuntimeConfigurationError(
                "runtime preflight failed: "
                f"config_valid={preflight.config_valid}, "
                f"sandbox_image_present={preflight.sandbox_image_present}"
            )

    def gate_factory(run_directory: Path, workspace: Path) -> QualityGateRunner:
        return QualityGateRunner(
            configuration,
            run_directory=run_directory,
            workspace=workspace,
            sandbox_image_id=frozen_sandbox_image,
            backend=DockerSandboxBackend(args.sandbox_binary),
        )

    coordinator = WorkflowCoordinator(
        manifest=manifest,
        runs_root=args.runs_root,
        workspaces_root=args.workspaces_root,
        executor=executor,
        quality_gate_factory=gate_factory,
        budget=configuration.policy.agent_budget,
        pricing=ModelPricing(
            model=model,
            input_cost_per_million_usd=input_cost,
            output_cost_per_million_usd=output_cost,
        ),
        runtime_setup=runtime_setup,
        manual_review_criteria=configuration.benchmark.manual_review_criteria,
        role_timeout_seconds=configuration.policy.agent_stage_timeouts_seconds,
        stage_timeout_seconds=timeout,
        artifact_repair_limit=args.artifact_repair_limit,
        verification_concurrency=concurrency,
    )
    outcome = coordinator.execute(
        task_brief,
        source_repository=args.source_repository,
        base_ref=args.base_ref,
    )
    print(
        f"run {outcome.record.phase.value}: run={outcome.record.run_id} "
        f"commit={outcome.record.current_commit or 'none'} "
        f"report={args.runs_root / outcome.record.run_id / outcome.human_report_path}"
    )
    return 0 if outcome.record.phase is RunPhase.COMPLETED else 2


def build_parser() -> argparse.ArgumentParser:
    """Build the product CLI parser for implemented commands."""

    parser = argparse.ArgumentParser(
        prog="sat",
        description=(
            "Configure, run, and validate the software Agent team harness. "
            "Run without a command for onboarding."
        ),
    )
    commands = parser.add_subparsers(dest="command")

    configure = commands.add_parser(
        "configure",
        help="Interactively create or replace secret-free live-run defaults.",
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
    config.add_argument("--benchmark", type=Path, default=DEFAULT_BENCHMARK)
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
    preflight.add_argument("--openclaw", type=Path, default=DEFAULT_OPENCLAW_CONFIG)
    preflight.add_argument("--policy", type=Path, default=DEFAULT_RUN_POLICY)
    preflight.add_argument("--benchmark", type=Path, default=DEFAULT_BENCHMARK)
    preflight.add_argument(
        "--openclaw-binary",
        type=Path,
        default=DEFAULT_OPENCLAW_BINARY,
    )
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
    run.add_argument("--openclaw", type=Path, default=DEFAULT_OPENCLAW_CONFIG)
    run.add_argument("--policy", type=Path, default=DEFAULT_RUN_POLICY)
    run.add_argument("--benchmark", type=Path, default=DEFAULT_BENCHMARK)
    run.add_argument("--runs-root", type=Path, default=DEFAULT_RUNS_ROOT)
    run.add_argument("--workspaces-root", type=Path, default=DEFAULT_WORKSPACES_ROOT)
    run.add_argument(
        "--openclaw-binary",
        type=Path,
        default=DEFAULT_OPENCLAW_BINARY,
    )
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
            return _show_welcome()
        return args.handler(args)
    except (
        OSError,
        json.JSONDecodeError,
        ValidationError,
        ValueError,
        RuntimeError,
    ) as error:
        print(f"error: {error}")
        return 1
