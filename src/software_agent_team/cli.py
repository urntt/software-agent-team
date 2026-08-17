"""Command-line entry point for validation and the Phase 1 vertical slice."""

import argparse
import json
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
from software_agent_team.workflow import WorkflowCoordinator

DEFAULT_TEAM_CONFIG = Path("configs/teams.json")
DEFAULT_OPENCLAW_CONFIG = Path("configs/openclaw.example.json5")
DEFAULT_RUN_POLICY = Path("configs/run-policy.json")
DEFAULT_BENCHMARK = Path("benchmarks/task_manager/benchmark.json")
DEFAULT_BENCHMARK_SEED = Path("benchmarks/task_manager/seed")
DEFAULT_RUNS_ROOT = Path("runs")
DEFAULT_WORKSPACES_ROOT = Path("workspaces")
DEFAULT_OPENCLAW_BINARY = Path.home() / ".openclaw" / "bin" / "openclaw"


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
            model=args.model,
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
            model=args.model,
            input_cost_per_million_usd=args.input_cost_per_million_usd,
            output_cost_per_million_usd=args.output_cost_per_million_usd,
        ),
        runtime_setup=runtime_setup,
        agent_timeout_seconds=args.agent_timeout_seconds,
        artifact_repair_limit=args.artifact_repair_limit,
        verification_concurrency=args.verification_concurrency,
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
        description="Run and validate the software Agent team harness.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

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
    run.add_argument("--model", required=True)
    run.add_argument(
        "--input-cost-per-million-usd",
        type=Decimal,
        required=True,
    )
    run.add_argument(
        "--output-cost-per-million-usd",
        type=Decimal,
        required=True,
    )
    run.add_argument("--agent-timeout-seconds", type=int, default=600)
    run.add_argument("--artifact-repair-limit", type=int, choices=(0, 1), default=1)
    run.add_argument(
        "--verification-concurrency",
        type=int,
        choices=(1, 2),
        default=2,
        help=(
            "Maximum concurrent Tester/Reviewer calls; use 1 for providers "
            "that serve only one generation at a time."
        ),
    )
    run.set_defaults(handler=_run_workflow)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one CLI command and return a process exit code."""

    args = build_parser().parse_args(argv)
    try:
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
