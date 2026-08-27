"""Tests for versioned quality-gate policy and sandbox execution."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest
from pydantic import ValidationError

from software_agent_team.artifacts import AgentRole
from software_agent_team.quality_gates import (
    BenchmarkManifest,
    DockerSandboxBackend,
    DockerSandboxPolicy,
    FakeSandboxBackend,
    HostTestBackend,
    QualityGateBudgetExceeded,
    QualityGateConfigurationError,
    QualityGateDefinition,
    QualityGateEvidenceError,
    QualityGateRunner,
    ReadOnlyInputMount,
    ResolvedInputMount,
    RunPolicy,
    SandboxExecution,
    SandboxInvocation,
    SandboxLimits,
    load_quality_gate_configuration,
)

REPOSITORY_ROOT = Path(__file__).parents[1]
POLICY_PATH = REPOSITORY_ROOT / "configs" / "run-policy.json"
BENCHMARK_PATH = REPOSITORY_ROOT / "benchmarks" / "task_manager" / "benchmark.json"


@pytest.fixture
def configuration():
    """Load the checked-in Phase 1 gate configuration."""

    return load_quality_gate_configuration(POLICY_PATH, BENCHMARK_PATH)


@pytest.fixture
def run_paths(tmp_path: Path) -> tuple[Path, Path]:
    """Create separate controller and generated-workspace directories."""

    run_directory = tmp_path / "runs" / "task-manager-phase1"
    workspace = tmp_path / "workspaces" / "task-manager-phase1"
    run_directory.mkdir(parents=True)
    workspace.mkdir(parents=True)
    return run_directory, workspace


def successful_executions(count: int = 4) -> list[SandboxExecution]:
    """Return scripted passing executions for every checked-in gate."""

    return [
        SandboxExecution(
            exit_code=0,
            timed_out=False,
            duration_ms=index + 1,
            stdout=f"gate {index} passed\n".encode(),
            stderr=b"",
        )
        for index in range(count)
    ]


def make_invocation(
    configuration,
    workspace: Path,
    *,
    argv: tuple[str, ...] = ("python", "-m", "pytest"),
    mounts: tuple[ResolvedInputMount, ...] | None = None,
    timeout_seconds: float = 10,
) -> SandboxInvocation:
    """Build one backend-level invocation using checked-in limits."""

    return SandboxInvocation(
        gate_id="CHECK_TEST",
        argv=argv,
        working_directory=PurePosixPath("."),
        workspace=workspace,
        input_mounts=configuration.input_mounts if mounts is None else mounts,
        environment=tuple(configuration.policy.sandbox.environment.items()),
        sandbox=configuration.policy.sandbox.model_copy(update={"user": "1000:1000"}),
        limits=configuration.policy.limits,
        timeout_seconds=timeout_seconds,
    )


def test_checked_in_manifests_are_complete_and_hashed(configuration) -> None:
    assert configuration.policy.id == "phase1_deterministic"
    assert configuration.policy.agent_budget.max_calls == 14
    assert configuration.policy.agent_stage_timeouts_seconds == {
        AgentRole.CLARIFIER: 120,
        AgentRole.SINGLE_AGENT: 900,
        AgentRole.PLANNER: 180,
        AgentRole.GENERALIST_DEVELOPER: 900,
        AgentRole.FRONTEND_DEVELOPER: 900,
        AgentRole.BACKEND_DEVELOPER: 900,
        AgentRole.INTEGRATOR: 900,
        AgentRole.TESTER: 300,
        AgentRole.REVIEWER: 300,
    }
    assert configuration.benchmark.id == "task_manager_phase1_v2"
    assert configuration.task_brief.confirmed is True
    assert len(configuration.benchmark.gates) == 4
    assert len(configuration.policy_sha256) == 64
    assert len(configuration.benchmark_sha256) == 64
    assert len(configuration.task_brief_sha256) == 64
    assert configuration.input_mounts[0].source.is_dir()
    assert configuration.input_mounts[0].target == PurePosixPath(
        "/opt/software-agent-team/inputs/task-manager-acceptance"
    )
    assert configuration.policy.sandbox.user == "host"
    assert configuration.policy.sandbox.environment["PYTHONPYCACHEPREFIX"] == (
        "/tmp/pycache"
    )


def test_checked_in_manifest_assigns_every_acceptance_criterion(configuration) -> None:
    known = {criterion.id for criterion in configuration.task_brief.acceptance_criteria}
    assigned = {
        criterion_id
        for gate in configuration.benchmark.gates
        for criterion_id in gate.criterion_ids
    } | set(configuration.benchmark.manual_review_criteria)

    assert assigned == known
    acceptance = configuration.benchmark.gates[-1]
    assert acceptance.argv == (
        "python",
        "/opt/software-agent-team/inputs/task-manager-acceptance/run.py",
        "--repository",
        "/workspace",
    )


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (("sh", "-c", "pytest"), "command shell"),
        (("/bin/python", "-m", "pytest"), "bare image PATH"),
        (("python", "-c", "print('opaque')"), "inline interpreter"),
        (("python", "line\nbreak"), "single-line"),
    ],
)
def test_gate_definition_rejects_unsafe_argv(
    argv: tuple[str, ...], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        QualityGateDefinition(
            id="CHECK_UNSAFE",
            argv=argv,
            criterion_ids=("AC_QUALITY",),
        )


@pytest.mark.parametrize("working_directory", ("../outside", "/workspace", "a\\b"))
def test_gate_definition_rejects_unsafe_working_directory(
    working_directory: str,
) -> None:
    with pytest.raises(ValidationError, match="path"):
        QualityGateDefinition(
            id="CHECK_PATH",
            argv=("python", "-m", "pytest"),
            working_directory=working_directory,
            criterion_ids=("AC_QUALITY",),
        )


def test_policy_rejects_mutable_image_and_relaxed_isolation() -> None:
    sandbox = json.loads(POLICY_PATH.read_text(encoding="utf-8"))["sandbox"]

    with pytest.raises(ValidationError, match="non-latest"):
        DockerSandboxPolicy.model_validate({**sandbox, "image": "python:latest"})
    with pytest.raises(ValidationError, match="non-latest"):
        DockerSandboxPolicy.model_validate(
            {**sandbox, "image": "registry.example:5000/python"}
        )
    with pytest.raises(ValidationError):
        DockerSandboxPolicy.model_validate({**sandbox, "network": "bridge"})
    with pytest.raises(ValidationError):
        DockerSandboxPolicy.model_validate(
            {**sandbox, "read_only_root_filesystem": False}
        )
    with pytest.raises(ValidationError):
        DockerSandboxPolicy.model_validate({**sandbox, "user": "0:0"})


def test_policy_rejects_incoherent_or_unbounded_limits() -> None:
    limits = json.loads(POLICY_PATH.read_text(encoding="utf-8"))["limits"]

    with pytest.raises(ValidationError, match="total timeout"):
        SandboxLimits.model_validate(
            {
                **limits,
                "command_timeout_seconds": 120,
                "total_timeout_seconds": 60,
            }
        )
    with pytest.raises(ValidationError):
        SandboxLimits.model_validate({**limits, "memory_mb": 0})
    with pytest.raises(ValidationError):
        SandboxLimits.model_validate({**limits, "writable_tmpfs_mb": 100_000})
    with pytest.raises(ValidationError):
        SandboxLimits.model_validate({**limits, "stdout_max_bytes": 1})


def test_policy_requires_a_bounded_timeout_for_every_agent_role() -> None:
    payload = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    payload["agent_stage_timeouts_seconds"].pop("reviewer")

    with pytest.raises(ValidationError, match="missing roles: reviewer"):
        RunPolicy.model_validate(payload)

    payload = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    payload["agent_stage_timeouts_seconds"]["planner"] = 3601
    with pytest.raises(ValidationError):
        RunPolicy.model_validate(payload)


def test_mount_targets_must_be_read_only_and_isolated() -> None:
    with pytest.raises(ValidationError):
        ReadOnlyInputMount(
            id="suite",
            source="acceptance",
            target="/workspace/acceptance",
        )
    with pytest.raises(ValidationError):
        ReadOnlyInputMount(
            id="suite",
            source="../acceptance",
            target="/opt/software-agent-team/inputs/suite",
        )
    with pytest.raises(ValidationError):
        ReadOnlyInputMount(
            id="suite",
            source="acceptance",
            target="/opt/software-agent-team/inputs/suite",
            read_only=False,
        )


def test_benchmark_rejects_overlapping_mount_targets() -> None:
    payload = json.loads(BENCHMARK_PATH.read_text(encoding="utf-8"))
    payload["input_mounts"].append(
        {
            "id": "nested",
            "source": "acceptance/run.py",
            "target": (
                "/opt/software-agent-team/inputs/task-manager-acceptance/nested"
            ),
            "read_only": True,
        }
    )

    with pytest.raises(ValidationError, match="cannot overlap"):
        BenchmarkManifest.model_validate(payload)


def test_loader_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    policy = tmp_path / "policy.json"
    policy.write_text('{"schema_version": 1, "schema_version": 1}', encoding="utf-8")

    with pytest.raises(QualityGateConfigurationError, match="duplicate JSON key"):
        load_quality_gate_configuration(policy, BENCHMARK_PATH)


def write_benchmark_copy(tmp_path: Path, payload: dict[str, object]) -> Path:
    """Write a mutable manifest beside trusted task/suite test fixtures."""

    benchmark_root = tmp_path / "benchmark"
    benchmark_root.mkdir()
    (benchmark_root / "task-brief.json").write_bytes(
        (BENCHMARK_PATH.parent / "task-brief.json").read_bytes()
    )
    (benchmark_root / "acceptance").mkdir()
    (benchmark_root / "acceptance" / "run.py").write_text(
        "print('fixture')\n", encoding="utf-8"
    )
    destination = benchmark_root / "benchmark.json"
    destination.write_text(json.dumps(payload), encoding="utf-8")
    return destination


def test_loader_rejects_missing_criterion_assignment(tmp_path: Path) -> None:
    payload = json.loads(BENCHMARK_PATH.read_text(encoding="utf-8"))
    payload["manual_review_criteria"] = ["AC_ACCESSIBILITY"]
    benchmark = write_benchmark_copy(tmp_path, payload)

    with pytest.raises(QualityGateConfigurationError, match="AC_DOCUMENTATION"):
        load_quality_gate_configuration(POLICY_PATH, benchmark)


def test_loader_rejects_unknown_criterion_reference(tmp_path: Path) -> None:
    payload = json.loads(BENCHMARK_PATH.read_text(encoding="utf-8"))
    payload["gates"][0]["criterion_ids"].append("AC_UNKNOWN")
    benchmark = write_benchmark_copy(tmp_path, payload)

    with pytest.raises(QualityGateConfigurationError, match="AC_UNKNOWN"):
        load_quality_gate_configuration(POLICY_PATH, benchmark)


def test_loader_rejects_gate_timeout_above_policy(tmp_path: Path) -> None:
    payload = json.loads(BENCHMARK_PATH.read_text(encoding="utf-8"))
    payload["gates"][0]["timeout_seconds"] = 121
    benchmark = write_benchmark_copy(tmp_path, payload)

    with pytest.raises(QualityGateConfigurationError, match="timeout exceeds"):
        load_quality_gate_configuration(POLICY_PATH, benchmark)


def test_loader_rejects_gate_timeouts_above_total_budget(tmp_path: Path) -> None:
    payload = json.loads(BENCHMARK_PATH.read_text(encoding="utf-8"))
    for gate in payload["gates"]:
        gate["timeout_seconds"] = 120
    benchmark = write_benchmark_copy(tmp_path, payload)

    with pytest.raises(QualityGateConfigurationError, match="sum of gate timeouts"):
        load_quality_gate_configuration(POLICY_PATH, benchmark)


def test_loader_rejects_environment_path_outside_mounts(tmp_path: Path) -> None:
    policy_payload = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    policy_payload["sandbox"]["environment"]["HOME"] = "/home/generated"
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps(policy_payload), encoding="utf-8")

    with pytest.raises(QualityGateConfigurationError, match="environment HOME"):
        load_quality_gate_configuration(policy, BENCHMARK_PATH)


def test_loader_rejects_unmounted_absolute_argv_path(tmp_path: Path) -> None:
    payload = json.loads(BENCHMARK_PATH.read_text(encoding="utf-8"))
    payload["gates"][0]["argv"].append("/etc/passwd")
    benchmark = write_benchmark_copy(tmp_path, payload)

    with pytest.raises(QualityGateConfigurationError, match="unmounted absolute"):
        load_quality_gate_configuration(POLICY_PATH, benchmark)


def test_loader_rejects_mount_symlink_that_escapes_benchmark(tmp_path: Path) -> None:
    payload = json.loads(BENCHMARK_PATH.read_text(encoding="utf-8"))
    benchmark = write_benchmark_copy(tmp_path, payload)
    outside = tmp_path / "outside"
    outside.mkdir()
    acceptance = benchmark.parent / "acceptance"
    acceptance.rename(benchmark.parent / "original-acceptance")
    acceptance.symlink_to(outside, target_is_directory=True)

    with pytest.raises(QualityGateConfigurationError, match="escapes"):
        load_quality_gate_configuration(POLICY_PATH, benchmark)


def test_docker_argv_enforces_every_isolation_limit(
    configuration, run_paths: tuple[Path, Path]
) -> None:
    _, workspace = run_paths
    invocation = make_invocation(configuration, workspace)
    argv = DockerSandboxBackend().build_argv(invocation, container_name="sat-qg-test")

    assert argv[:3] == ("docker", "run", "--rm")
    assert argv[argv.index("--pull") : argv.index("--pull") + 2] == (
        "--pull",
        "never",
    )
    assert argv[argv.index("--network") : argv.index("--network") + 2] == (
        "--network",
        "none",
    )
    assert "--read-only" in argv
    assert argv[argv.index("--cap-drop") : argv.index("--cap-drop") + 2] == (
        "--cap-drop",
        "ALL",
    )
    assert argv[argv.index("--pids-limit") : argv.index("--pids-limit") + 2] == (
        "--pids-limit",
        "128",
    )
    assert argv[argv.index("--memory") : argv.index("--memory") + 2] == (
        "--memory",
        "512m",
    )
    assert argv[argv.index("--memory-swap") : argv.index("--memory-swap") + 2] == (
        "--memory-swap",
        "512m",
    )
    assert argv[argv.index("--cpus") : argv.index("--cpus") + 2] == (
        "--cpus",
        "1.0",
    )
    assert any(
        value.startswith("/tmp:rw,nosuid,nodev,noexec,size=128m") for value in argv
    )
    mounts = [argv[index + 1] for index, value in enumerate(argv) if value == "--mount"]
    assert len(mounts) == 2
    assert all(value.endswith(",readonly") for value in mounts)
    assert argv[-4:] == ("sat-python-quality:phase1-v4", *invocation.argv)


def test_runner_defaults_to_docker(configuration, run_paths: tuple[Path, Path]) -> None:
    run_directory, workspace = run_paths
    runner = QualityGateRunner(
        configuration, run_directory=run_directory, workspace=workspace
    )

    assert isinstance(runner.backend, DockerSandboxBackend)


def test_runner_resolves_the_quality_gate_user_to_the_host_identity(
    configuration,
    run_paths: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_directory, workspace = run_paths
    backend = FakeSandboxBackend(successful_executions())
    backend.kind = "docker"
    monkeypatch.setattr(os, "getuid", lambda: 1234)
    monkeypatch.setattr(os, "getgid", lambda: 5678)
    runner = QualityGateRunner(
        configuration,
        run_directory=run_directory,
        workspace=workspace,
        backend=backend,
    )

    runner.run(iteration=1)

    assert {call.sandbox.user for call in backend.invocations} == {"1234:5678"}


def test_runner_rejects_root_for_live_quality_gates(
    configuration,
    run_paths: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_directory, workspace = run_paths
    backend = FakeSandboxBackend(successful_executions())
    backend.kind = "docker"
    monkeypatch.setattr(os, "getuid", lambda: 0)
    monkeypatch.setattr(os, "getgid", lambda: 0)
    runner = QualityGateRunner(
        configuration,
        run_directory=run_directory,
        workspace=workspace,
        backend=backend,
    )

    with pytest.raises(QualityGateConfigurationError, match="unprivileged"):
        runner.run(iteration=1)

    assert backend.invocations == []


def test_runner_uses_the_frozen_sandbox_image_identity(
    configuration, run_paths: tuple[Path, Path]
) -> None:
    run_directory, workspace = run_paths
    fake = FakeSandboxBackend(successful_executions())
    image_id = f"sha256:{'a' * 64}"
    runner = QualityGateRunner(
        configuration,
        run_directory=run_directory,
        workspace=workspace,
        sandbox_image_id=image_id,
        backend=fake,
        allow_test_backends=True,
    )

    runner.run(iteration=1)

    assert fake.invocations
    assert {invocation.sandbox.image for invocation in fake.invocations} == {image_id}


def test_runner_rejects_a_non_digest_image_override(
    configuration, run_paths: tuple[Path, Path]
) -> None:
    run_directory, workspace = run_paths

    with pytest.raises(QualityGateConfigurationError, match="immutable SHA-256"):
        QualityGateRunner(
            configuration,
            run_directory=run_directory,
            workspace=workspace,
            sandbox_image_id="mutable:tag",
        )


def test_runner_requires_explicit_test_backend_opt_in(
    configuration, run_paths: tuple[Path, Path]
) -> None:
    run_directory, workspace = run_paths
    fake = FakeSandboxBackend(successful_executions())

    with pytest.raises(QualityGateConfigurationError, match="allow_test_backends"):
        QualityGateRunner(
            configuration,
            run_directory=run_directory,
            workspace=workspace,
            backend=fake,
        )


def test_runner_generates_command_evidence_and_persists_outputs(
    configuration, run_paths: tuple[Path, Path]
) -> None:
    run_directory, workspace = run_paths
    fake = FakeSandboxBackend(successful_executions())
    runner = QualityGateRunner(
        configuration,
        run_directory=run_directory,
        workspace=workspace,
        backend=fake,
        allow_test_backends=True,
    )

    evidence = runner.run(iteration=1)

    assert [item.id for item in evidence] == [
        gate.id for gate in configuration.benchmark.gates
    ]
    assert all(item.exit_code == 0 and not item.timed_out for item in evidence)
    assert evidence[0].argv == configuration.benchmark.gates[0].argv
    assert evidence[0].criterion_ids == (configuration.benchmark.gates[0].criterion_ids)
    assert evidence[0].stdout_path == (
        "iterations/01/commands/check_compile.stdout.txt"
    )
    assert (run_directory / evidence[0].stdout_path).read_bytes() == b"gate 0 passed\n"
    assert (run_directory / evidence[0].stderr_path).read_bytes() == b""
    assert evidence[0].stdout_tail == "gate 0 passed\n"
    assert evidence[0].stderr_tail == ""
    assert evidence[0].stdout_truncated is False
    assert evidence[0].stderr_truncated is False
    assert len(fake.invocations) == 4
    assert all(call.workspace == workspace for call in fake.invocations)
    assert all(
        call.working_directory == PurePosixPath(".") for call in fake.invocations
    )
    assert all(
        call.input_mounts == configuration.input_mounts for call in fake.invocations
    )


def test_runner_refuses_to_overwrite_or_reexecute_evidence(
    configuration, run_paths: tuple[Path, Path]
) -> None:
    run_directory, workspace = run_paths
    first = FakeSandboxBackend(successful_executions())
    QualityGateRunner(
        configuration,
        run_directory=run_directory,
        workspace=workspace,
        backend=first,
        allow_test_backends=True,
    ).run(iteration=1)
    second = FakeSandboxBackend(successful_executions())
    runner = QualityGateRunner(
        configuration,
        run_directory=run_directory,
        workspace=workspace,
        backend=second,
        allow_test_backends=True,
    )

    with pytest.raises(QualityGateEvidenceError, match="already exists"):
        runner.run(iteration=1)
    assert second.invocations == []


def test_runner_reports_each_persisted_gate_result(
    configuration, run_paths: tuple[Path, Path]
) -> None:
    run_directory, workspace = run_paths
    observed: list[tuple[str, int, int, int]] = []
    runner = QualityGateRunner(
        configuration,
        run_directory=run_directory,
        workspace=workspace,
        backend=FakeSandboxBackend(successful_executions()),
        allow_test_backends=True,
        result_handler=lambda command, iteration, completed, total: observed.append(
            (command.id, iteration, completed, total)
        ),
    )

    runner.run(iteration=2)

    assert observed == [
        (gate.id, 2, index, len(configuration.benchmark.gates))
        for index, gate in enumerate(configuration.benchmark.gates, start=1)
    ]


def test_runner_represents_timeout_as_command_evidence(
    configuration, run_paths: tuple[Path, Path]
) -> None:
    run_directory, workspace = run_paths
    executions = successful_executions()
    executions[0] = SandboxExecution(
        exit_code=None,
        timed_out=True,
        duration_ms=30_000,
        stdout=b"partial",
        stderr=b"",
    )
    runner = QualityGateRunner(
        configuration,
        run_directory=run_directory,
        workspace=workspace,
        backend=FakeSandboxBackend(executions),
        allow_test_backends=True,
    )

    evidence = runner.run(iteration=2)

    assert evidence[0].timed_out is True
    assert evidence[0].exit_code is None
    assert "Timed out" in evidence[0].summary
    assert evidence[0].stdout_path.startswith("iterations/02/")


def test_runner_enforces_total_time_budget_before_execution(
    configuration, run_paths: tuple[Path, Path]
) -> None:
    run_directory, workspace = run_paths
    clock = iter((0.0, 301.0))
    fake = FakeSandboxBackend(successful_executions())
    runner = QualityGateRunner(
        configuration,
        run_directory=run_directory,
        workspace=workspace,
        backend=fake,
        allow_test_backends=True,
        monotonic=lambda: next(clock),
    )

    with pytest.raises(QualityGateBudgetExceeded, match="exhausted"):
        runner.run(iteration=1)
    assert fake.invocations == []


def test_runner_bounds_backend_output_and_records_failure(
    configuration, run_paths: tuple[Path, Path]
) -> None:
    run_directory, workspace = run_paths
    limit = configuration.policy.limits.stdout_max_bytes
    executions = successful_executions()
    executions[0] = SandboxExecution(
        exit_code=0,
        timed_out=False,
        duration_ms=10,
        stdout=b"x" * (limit + 1),
        stderr=b"",
    )
    runner = QualityGateRunner(
        configuration,
        run_directory=run_directory,
        workspace=workspace,
        backend=FakeSandboxBackend(executions),
        allow_test_backends=True,
    )

    evidence = runner.run(iteration=1)
    persisted = (run_directory / evidence[0].stdout_path).read_bytes()

    assert evidence[0].exit_code == 137
    assert "output exceeded" in evidence[0].summary
    assert len(persisted) == limit
    assert b"output limit exceeded" in persisted
    assert len(evidence[0].stdout_tail) <= 4096
    assert evidence[0].stdout_truncated is True
    assert "output limit exceeded" in evidence[0].stdout_tail


def test_runner_embeds_bounded_failure_diagnostics(
    configuration, run_paths: tuple[Path, Path]
) -> None:
    run_directory, workspace = run_paths
    executions = successful_executions()
    executions[0] = SandboxExecution(
        exit_code=1,
        timed_out=False,
        duration_ms=10,
        stdout=b"before\n" + b"x" * 5000,
        stderr=b"Traceback\nAssertionError: canonical URL missing\n",
    )
    runner = QualityGateRunner(
        configuration,
        run_directory=run_directory,
        workspace=workspace,
        backend=FakeSandboxBackend(executions),
        allow_test_backends=True,
    )

    evidence = runner.run(iteration=1)[0]

    assert evidence.exit_code == 1
    assert len(evidence.stdout_tail) == 4096
    assert evidence.stdout_truncated is True
    assert evidence.stderr_tail.endswith("canonical URL missing\n")
    assert evidence.stderr_truncated is False


def test_runner_rejects_working_directory_symlink_escape(
    configuration, run_paths: tuple[Path, Path], tmp_path: Path
) -> None:
    run_directory, workspace = run_paths
    outside = tmp_path / "outside-cwd"
    outside.mkdir()
    (workspace / "escape").symlink_to(outside, target_is_directory=True)
    changed_gate = configuration.benchmark.gates[0].model_copy(
        update={"working_directory": "escape"}
    )
    changed_benchmark = configuration.benchmark.model_copy(
        update={
            "gates": (changed_gate, *configuration.benchmark.gates[1:]),
        }
    )
    changed_configuration = replace(configuration, manifest=changed_benchmark)
    runner = QualityGateRunner(
        changed_configuration,
        run_directory=run_directory,
        workspace=workspace,
        backend=FakeSandboxBackend(successful_executions()),
        allow_test_backends=True,
    )

    with pytest.raises(QualityGateConfigurationError, match="working directory"):
        runner.run(iteration=1)


def test_runner_rejects_trusted_input_inside_generated_workspace(
    configuration, run_paths: tuple[Path, Path]
) -> None:
    run_directory, workspace = run_paths
    suite = workspace / "suite"
    suite.mkdir()
    changed = replace(
        configuration,
        input_mounts=(
            ResolvedInputMount(
                id="suite",
                source=suite,
                target=PurePosixPath("/opt/software-agent-team/inputs/suite"),
            ),
        ),
    )

    with pytest.raises(QualityGateConfigurationError, match="outside"):
        QualityGateRunner(
            changed,
            run_directory=run_directory,
            workspace=workspace,
        )


def test_host_backend_requires_two_explicit_opt_ins(
    configuration, run_paths: tuple[Path, Path]
) -> None:
    run_directory, workspace = run_paths
    with pytest.raises(QualityGateConfigurationError, match="allow_unsafe"):
        HostTestBackend()
    host = HostTestBackend(allow_unsafe_host_execution=True)

    with pytest.raises(QualityGateConfigurationError, match="allow_test_backends"):
        QualityGateRunner(
            configuration,
            run_directory=run_directory,
            workspace=workspace,
            backend=host,
        )


def test_host_backend_maps_read_only_inputs_and_sanitizes_environment(
    configuration, run_paths: tuple[Path, Path], tmp_path: Path, monkeypatch
) -> None:
    _, workspace = run_paths
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    script = trusted / "probe.py"
    script.write_text(
        "from pathlib import Path\n"
        "import os, sys\n"
        "print(Path.cwd().name)\n"
        "print(Path(sys.argv[1]).resolve() == Path.cwd())\n"
        "print(os.environ.get('SHOULD_NOT_LEAK', 'missing'))\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SHOULD_NOT_LEAK", "secret")
    mounts = (
        ResolvedInputMount(
            id="trusted",
            source=trusted,
            target=PurePosixPath("/opt/software-agent-team/inputs/trusted"),
        ),
    )
    invocation = make_invocation(
        configuration,
        workspace,
        argv=(
            "python",
            "/opt/software-agent-team/inputs/trusted/probe.py",
            "/workspace",
        ),
        mounts=mounts,
    )

    result = HostTestBackend(allow_unsafe_host_execution=True).execute(invocation)

    assert result.exit_code == 0
    assert result.stdout.decode().splitlines() == [workspace.name, "True", "missing"]


def test_host_backend_enforces_timeout(
    configuration, run_paths: tuple[Path, Path], tmp_path: Path
) -> None:
    _, workspace = run_paths
    trusted = tmp_path / "trusted-timeout"
    trusted.mkdir()
    (trusted / "sleep.py").write_text("import time\ntime.sleep(10)\n", encoding="utf-8")
    mounts = (
        ResolvedInputMount(
            id="trusted",
            source=trusted,
            target=PurePosixPath("/opt/software-agent-team/inputs/trusted"),
        ),
    )
    invocation = make_invocation(
        configuration,
        workspace,
        argv=("python", "/opt/software-agent-team/inputs/trusted/sleep.py"),
        mounts=mounts,
        timeout_seconds=0.05,
    )

    result = HostTestBackend(allow_unsafe_host_execution=True).execute(invocation)

    assert result.timed_out is True
    assert result.exit_code is None
    assert result.duration_ms < 2000


def test_sandbox_execution_requires_coherent_exit_state() -> None:
    with pytest.raises(ValueError, match="cannot have an exit code"):
        SandboxExecution(exit_code=1, timed_out=True, duration_ms=1)
    with pytest.raises(ValueError, match="requires an exit code"):
        SandboxExecution(exit_code=None, timed_out=False, duration_ms=1)


def test_run_policy_forbids_unknown_fields() -> None:
    payload = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    payload["legacy_host_fallback"] = True

    with pytest.raises(ValidationError, match="Extra inputs"):
        RunPolicy.model_validate(payload)


def test_host_backend_does_not_copy_provider_like_host_variables(
    configuration, run_paths: tuple[Path, Path], tmp_path: Path, monkeypatch
) -> None:
    _, workspace = run_paths
    trusted = tmp_path / "trusted-env"
    trusted.mkdir()
    (trusted / "env.py").write_text(
        "import os\nprint(os.environ.get('PROVIDER_API_KEY', 'absent'))\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PROVIDER_API_KEY", "must-not-cross-boundary")
    mounts = (
        ResolvedInputMount(
            id="trusted",
            source=trusted,
            target=PurePosixPath("/opt/software-agent-team/inputs/trusted"),
        ),
    )
    invocation = make_invocation(
        configuration,
        workspace,
        argv=("python", "/opt/software-agent-team/inputs/trusted/env.py"),
        mounts=mounts,
    )

    result = HostTestBackend(allow_unsafe_host_execution=True).execute(invocation)

    assert result.stdout == b"absent\n"
    assert os.environ["PROVIDER_API_KEY"] == "must-not-cross-boundary"
