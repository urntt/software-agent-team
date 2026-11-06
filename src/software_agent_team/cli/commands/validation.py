"""CLI commands for config, handoff, artifact validation, and environment preflight."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from software_agent_team import cli
from software_agent_team.artifacts import (
    AgentRole,
    HandoffEnvelope,
    TaskBrief,
    parse_phase_artifact,
)
from software_agent_team.benchmark_seed import prepare_benchmark_seed
from software_agent_team.cli.commands.version import _load_json_model
from software_agent_team.cli.constants import (
    DEFAULT_BENCHMARK,
    DEFAULT_OPENCLAW_BINARY,
    DEFAULT_OPENCLAW_CONFIG,
    PROJECT_ROOT,
)
from software_agent_team.configuration import validate_environment_configuration
from software_agent_team.decision_limits import validate_decision_limit_registry
from software_agent_team.git_workspace import GitWorkspaceManager
from software_agent_team.paths import user_state_root
from software_agent_team.process_lifecycle import ProcessLeaseStore
from software_agent_team.product import ProductStatePaths, ensure_product_state
from software_agent_team.quality_gates import load_quality_gate_configuration
from software_agent_team.sandbox_lifecycle import cleanup_sat_sandbox_sessions
from software_agent_team.teams import load_team_manifest


def _validate_handoff(args: argparse.Namespace) -> int:
    handoff = _load_json_model(args.path, HandoffEnvelope)
    manifest = load_team_manifest(args.teams)
    try:
        source_role = AgentRole(handoff.source_agent_id)
        target_role = (
            None
            if handoff.target_agent_id is None
            else AgentRole(handoff.target_agent_id)
        )
    except ValueError as error:
        raise ValueError(
            "fixed-manifest validation requires compatibility Agent IDs"
        ) from error
    manifest.validate_handoff_boundary(
        team_id=handoff.team_id,
        iteration=handoff.iteration,
        source_role=source_role,
        target_role=target_role,
    )
    print(
        "valid handoff: "
        f"run={handoff.run_id} team={handoff.team_id} "
        f"iteration={handoff.iteration} source={handoff.source_agent_id}"
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
    validate_decision_limit_registry(PROJECT_ROOT)
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


def _cleanup_owned_resources(args: argparse.Namespace) -> int:
    """Inspect or explicitly reclaim only proven orphaned SAT resources."""

    state_paths = ProductStatePaths.below(user_state_root())
    ensure_product_state(state_paths)
    store = ProcessLeaseStore(state_paths.process_leases)
    observation = store.inspect()
    print("SAT provider-process ownership")
    print(f"  Active: {len(observation.active)}")
    print(f"  Orphaned: {len(observation.orphaned)}")
    print(f"  Stale leases: {len(observation.stale)}")
    for item in observation.processes:
        print(
            f"  - {item.status.value}: run={item.lease.run_id} "
            f"agent={item.lease.agent_id} pid={item.lease.child.pid}"
        )
    if not args.orphans:
        if observation.orphaned or observation.stale:
            print("Run `sat cleanup --orphans` to reclaim only these proven remnants.")
        return 0
    candidates = (*observation.orphaned, *observation.stale)
    if not candidates:
        print("No orphaned process or stale lease requires cleanup.")
        return 0
    if not args.yes and not cli._prompt_yes_no(
        "Stop the proven orphaned process groups and their SAT sandboxes?",
        default=False,
    ):
        print("Cleanup cancelled; no process or container was changed.")
        return 0

    recovery = store.reclaim_orphans(
        lease_ids=tuple(item.lease.lease_id for item in candidates),
        release_leases=False,
    )
    sessions = tuple(dict.fromkeys(item.session_key for item in recovery.reclaimed))
    sandbox_cleanup = cleanup_sat_sandbox_sessions(
        sandbox_binary="docker",
        session_keys=sessions,
        state_root=state_paths.root,
    )
    for lease in recovery.reclaimed:
        store.release(lease)
    print(
        f"Recovered {len(recovery.reclaimed)} process lease(s) and removed "
        f"{len(sandbox_cleanup.removed)} sandbox container(s)."
    )
    if recovery.active:
        print(f"Left {len(recovery.active)} active SAT invocation(s) unchanged.")
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
        cli.materialize_run_configuration(
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
        result = cli.inspect_runtime_preflight(
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
