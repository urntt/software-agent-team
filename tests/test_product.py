"""Tests for startup diagnostics, request materialization, and delivery."""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

import software_agent_team.product as product
from software_agent_team.process_lifecycle import (
    InvocationProcessLease,
    ObservedInvocationProcess,
    ProcessIdentity,
    ProcessLeaseStatus,
    ProcessResourceObservation,
)
from software_agent_team.product import (
    DiagnosticState,
    HostCapacitySnapshot,
    ProductFlowError,
    ProductStatePaths,
    deliver_product_workspace,
    ensure_product_state,
    generate_product_run_id,
    inspect_host_capacity,
    inspect_startup_environment,
    load_project_commands,
    prepare_product_source,
    validate_project_destination,
)
from software_agent_team.sandbox_lifecycle import (
    ObservedSandbox,
    SandboxResourceObservation,
)

REPOSITORY_ROOT = Path(__file__).parents[1]


def ready_host_capacity() -> HostCapacitySnapshot:
    return HostCapacitySnapshot(
        available_memory_bytes=4 * 1024 * 1024 * 1024,
        available_pids=1024,
        pids_unbounded=False,
        memory_sources=("test memory",),
        pid_sources=("test pids",),
    )


def completed(argv: object, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, returncode, "", "")


def test_startup_diagnostics_report_a_ready_local_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    for name in ("sat", "git", "docker"):
        path = bin_directory / name
        path.write_text("#!/bin/sh\n", encoding="utf-8")
        path.chmod(0o755)
    openclaw = tmp_path / "openclaw"
    openclaw.write_text("#!/bin/sh\n", encoding="utf-8")
    openclaw.chmod(0o755)
    monkeypatch.setattr(product.platform, "system", lambda: "Linux")
    monkeypatch.setattr(product.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(product.os, "getuid", lambda: 1000)
    monkeypatch.setattr(product.os, "getgid", lambda: 1000)
    monkeypatch.setattr(
        product.shutil,
        "disk_usage",
        lambda _path: product.shutil._ntuple_diskusage(10, 1, 2**30),
    )

    diagnostics = inspect_startup_environment(
        working_directory=tmp_path,
        openclaw_binary=openclaw,
        sandbox_image="sat-image:v1",
        state_root=(tmp_path / "state").resolve(),
        required_memory_mb=512,
        required_pids=128,
        command_finder=lambda name: str(bin_directory / name),
        command_runner=lambda argv, _timeout: completed(argv),
        environment={"PATH": str(bin_directory)},
        host_capacity=ready_host_capacity(),
        sandbox_resources=SandboxResourceObservation(containers=()),
    )

    assert diagnostics.ready
    assert all(
        check.state is not DiagnosticState.ACTION_REQUIRED
        for check in diagnostics.checks
    )


def test_startup_diagnostics_explain_unavailable_docker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    openclaw = tmp_path / "openclaw"
    openclaw.write_text("#!/bin/sh\n", encoding="utf-8")
    openclaw.chmod(0o755)
    monkeypatch.setattr(product.platform, "system", lambda: "Linux")
    monkeypatch.setattr(product.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(product.os, "getuid", lambda: 1000)
    monkeypatch.setattr(product.os, "getgid", lambda: 1000)

    diagnostics = inspect_startup_environment(
        working_directory=tmp_path,
        openclaw_binary=openclaw,
        sandbox_image="sat-image:v1",
        state_root=(tmp_path / "state").resolve(),
        required_memory_mb=512,
        required_pids=128,
        command_finder=lambda name: f"/bin/{name}",
        command_runner=lambda argv, _timeout: completed(argv, returncode=1),
        environment={"PATH": "/bin"},
        host_capacity=ready_host_capacity(),
        sandbox_resources=SandboxResourceObservation(containers=()),
    )

    assert not diagnostics.ready
    docker = next(check for check in diagnostics.checks if check.id == "docker_daemon")
    assert docker.state is DiagnosticState.ACTION_REQUIRED
    assert docker.action is not None
    assert "Start Docker" in docker.action


def test_host_capacity_uses_the_tightest_linux_and_cgroup_headroom() -> None:
    gib = 1024 * 1024 * 1024
    values = {
        Path("/proc/meminfo"): "MemAvailable: 2097152 kB\n",
        Path("/proc/self/cgroup"): "0::/user.slice\n",
        Path("/sys/fs/cgroup/user.slice/memory.max"): str(gib),
        Path("/sys/fs/cgroup/user.slice/memory.current"): str(gib // 4),
        Path("/sys/fs/cgroup/user.slice/pids.max"): "200",
        Path("/sys/fs/cgroup/user.slice/pids.current"): "20",
    }

    def read(path: Path) -> str:
        try:
            return values[path]
        except KeyError as error:
            raise FileNotFoundError(path) from error

    capacity = inspect_host_capacity(file_reader=read)

    assert capacity.available_memory_bytes == 3 * gib // 4
    assert capacity.available_pids == 180
    assert not capacity.pids_unbounded


def test_startup_warns_when_headroom_is_below_sandbox_resource_ceilings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = tmp_path / "bin"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setattr(product.platform, "system", lambda: "Linux")
    monkeypatch.setattr(product.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(product.os, "getuid", lambda: 1000)
    monkeypatch.setattr(product.os, "getgid", lambda: 1000)
    monkeypatch.setattr(
        product.shutil,
        "disk_usage",
        lambda _path: product.shutil._ntuple_diskusage(10, 1, 2**30),
    )
    capacity = HostCapacitySnapshot(
        available_memory_bytes=511 * 1024 * 1024,
        available_pids=127,
        pids_unbounded=False,
        memory_sources=("test memory",),
        pid_sources=("test pids",),
    )

    diagnostics = inspect_startup_environment(
        working_directory=tmp_path,
        openclaw_binary=binary,
        sandbox_image="sat-image:v1",
        state_root=(tmp_path / "state").resolve(),
        required_memory_mb=512,
        required_pids=128,
        command_finder=lambda _name: str(binary),
        command_runner=lambda argv, _timeout: completed(argv),
        environment={"PATH": str(tmp_path)},
        host_capacity=capacity,
        sandbox_resources=SandboxResourceObservation(containers=()),
    )

    checks = {check.id: check for check in diagnostics.checks}
    assert checks["memory_capacity"].state is DiagnosticState.WARNING
    assert checks["pid_capacity"].state is DiagnosticState.WARNING
    assert diagnostics.ready

    monkeypatch.setattr(
        product.shutil,
        "disk_usage",
        lambda _path: product.shutil._ntuple_diskusage(
            10,
            1,
            product.MINIMUM_FREE_BYTES - 1,
        ),
    )
    low_disk = inspect_startup_environment(
        working_directory=tmp_path,
        openclaw_binary=binary,
        sandbox_image="sat-image:v1",
        state_root=(tmp_path / "state").resolve(),
        required_memory_mb=512,
        required_pids=128,
        command_finder=lambda _name: str(binary),
        command_runner=lambda argv, _timeout: completed(argv),
        environment={"PATH": str(tmp_path)},
        host_capacity=ready_host_capacity(),
        sandbox_resources=SandboxResourceObservation(containers=()),
    )
    storage = next(item for item in low_disk.checks if item.id == "storage")
    assert storage.state is DiagnosticState.ACTION_REQUIRED
    assert not low_disk.ready


def test_startup_reports_existing_owned_sandboxes_without_removing_them(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = tmp_path / "bin"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setattr(product.platform, "system", lambda: "Linux")
    monkeypatch.setattr(product.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(product.os, "getuid", lambda: 1000)
    monkeypatch.setattr(product.os, "getgid", lambda: 1000)
    monkeypatch.setattr(
        product.shutil,
        "disk_usage",
        lambda _path: product.shutil._ntuple_diskusage(10, 1, 2**30),
    )
    observation = SandboxResourceObservation(
        containers=(
            ObservedSandbox(
                container_id="a" * 64,
                container_name="/sat-sandbox",
                session_key="agent:builder:sat-example-i1-work-result",
                running=True,
            ),
        )
    )

    diagnostics = inspect_startup_environment(
        working_directory=tmp_path,
        openclaw_binary=binary,
        sandbox_image="sat-image:v1",
        state_root=(tmp_path / "state").resolve(),
        required_memory_mb=512,
        required_pids=128,
        command_finder=lambda _name: str(binary),
        command_runner=lambda argv, _timeout: completed(argv),
        environment={"PATH": str(tmp_path)},
        host_capacity=ready_host_capacity(),
        sandbox_resources=observation,
    )

    existing = next(
        item for item in diagnostics.checks if item.id == "sat_sandbox_resources"
    )
    assert existing.state is DiagnosticState.WARNING
    assert "1 running" in existing.detail
    assert "aaaaaaaaaaaa" in existing.detail
    assert diagnostics.ready


def test_startup_blocks_on_proven_orphaned_provider_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = tmp_path / "bin"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setattr(product.platform, "system", lambda: "Linux")
    monkeypatch.setattr(product.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(product.os, "getuid", lambda: 1000)
    monkeypatch.setattr(product.os, "getgid", lambda: 1000)
    monkeypatch.setattr(
        product.shutil,
        "disk_usage",
        lambda _path: product.shutil._ntuple_diskusage(10, 1, 2**30),
    )
    identity = ProcessIdentity(pid=1234, process_group_id=1234, start_time_ticks=5)
    process_resources = ProcessResourceObservation(
        processes=(
            ObservedInvocationProcess(
                lease=InvocationProcessLease(
                    lease_id="a" * 32,
                    run_id="sat-orphaned",
                    agent_id="builder",
                    session_key="agent:builder:sat-orphaned-i1-work-result",
                    owner=ProcessIdentity(
                        pid=1200,
                        process_group_id=1200,
                        start_time_ticks=4,
                    ),
                    child=identity,
                    command_sha256="b" * 64,
                    created_at=datetime(2026, 9, 5, 12, 0, tzinfo=UTC),
                ),
                status=ProcessLeaseStatus.ORPHANED,
            ),
        )
    )

    diagnostics = inspect_startup_environment(
        working_directory=tmp_path,
        openclaw_binary=binary,
        sandbox_image="sat-image:v1",
        state_root=(tmp_path / "state").resolve(),
        required_memory_mb=512,
        required_pids=128,
        command_finder=lambda _name: str(binary),
        command_runner=lambda argv, _timeout: completed(argv),
        environment={"PATH": str(tmp_path)},
        host_capacity=ready_host_capacity(),
        sandbox_resources=SandboxResourceObservation(containers=()),
        process_resources=process_resources,
    )

    existing = next(
        item for item in diagnostics.checks if item.id == "sat_process_resources"
    )
    assert existing.state is DiagnosticState.ACTION_REQUIRED
    assert "builder:1234" in existing.detail
    assert existing.action == "Run `sat cleanup --orphans`, then start this task again."
    assert not diagnostics.ready


def test_product_state_is_private_and_rejects_a_symlink(tmp_path: Path) -> None:
    paths = ProductStatePaths.below(tmp_path / "state")

    ensure_product_state(paths)

    assert all(
        path.is_dir()
        for path in (
            paths.root,
            paths.runs,
            paths.workspaces,
            paths.sources,
            paths.planning,
            paths.self_checks,
            paths.process_leases,
            paths.openclaw,
        )
    )
    assert all(
        path.stat().st_mode & 0o777 == 0o700
        for path in (
            paths.root,
            paths.runs,
            paths.workspaces,
            paths.sources,
            paths.planning,
            paths.self_checks,
            paths.process_leases,
            paths.openclaw,
        )
    )

    outside = tmp_path / "outside"
    outside.mkdir()
    paths.runs.rmdir()
    paths.runs.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ProductFlowError, match="real directory"):
        ensure_product_state(paths)


def test_product_state_refuses_to_adopt_an_unowned_nonempty_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    user_file = root / "user-file.txt"
    user_file.write_text("keep\n", encoding="utf-8")

    with pytest.raises(ProductFlowError, match="not owned by SAT"):
        ensure_product_state(ProductStatePaths.below(root))

    assert user_file.read_text(encoding="utf-8") == "keep\n"
    assert root.stat().st_mode & 0o777 == 0o755


def test_product_run_id_is_independent_of_the_evaluation_fixture() -> None:
    run_id = generate_product_run_id(
        clock=lambda: datetime(2026, 8, 24, 12, 34, 56, tzinfo=UTC),
        random_suffix=lambda: "a1b2c3d4",
    )

    assert run_id == "sat-20260824-123456-a1b2c3d4"


def test_project_commands_are_loaded_from_the_generated_project(tmp_path: Path) -> None:
    (tmp_path / "sat-project.json").write_text(
        """{
  "schema_version": 1,
  "setup": ["uv", "sync", "--dev"],
  "start": ["uv", "run", "markdown-link-checker", "docs"],
  "test": ["uv", "run", "pytest"]
}\n""",
        encoding="utf-8",
    )

    commands = load_project_commands(tmp_path)

    assert commands.start == ("uv", "run", "markdown-link-checker", "docs")


def test_project_commands_reject_a_shell_entrypoint(tmp_path: Path) -> None:
    (tmp_path / "sat-project.json").write_text(
        """{
  "schema_version": 1,
  "setup": ["uv", "sync", "--dev"],
  "start": ["uv", "run", "sh", "-c", "unsafe"],
  "test": ["uv", "run", "pytest"]
}\n""",
        encoding="utf-8",
    )

    with pytest.raises(ProductFlowError, match="cannot invoke a command shell"):
        load_project_commands(tmp_path)


def test_project_commands_reject_duplicate_json_keys(tmp_path: Path) -> None:
    (tmp_path / "sat-project.json").write_text(
        """{
  "schema_version": 1,
  "schema_version": 1,
  "setup": ["uv", "sync", "--dev"],
  "start": ["uv", "run", "link-checker"],
  "test": ["uv", "run", "pytest"]
}\n""",
        encoding="utf-8",
    )

    with pytest.raises(ProductFlowError, match="duplicate JSON key"):
        load_project_commands(tmp_path)


def test_product_source_uses_the_generic_profile_seed(tmp_path: Path) -> None:
    paths = ProductStatePaths.below(tmp_path / "state")
    ensure_product_state(paths)

    source = prepare_product_source(
        seed=REPOSITORY_ROOT / "profiles" / "python" / "seed",
        state_paths=paths,
        run_id="sat-test-source",
    )

    assert git(source, "log", "-1", "--format=%s") == (
        "chore: initialize software project"
    )
    assert "task-manager" not in (source / "README.md").read_text(encoding="utf-8")
    gitignore = (source / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".venv/" in gitignore
    assert "uv.lock" in gitignore


def test_destination_requires_one_new_direct_child(tmp_path: Path) -> None:
    assert validate_project_destination(tmp_path, "my-tasks") == (tmp_path / "my-tasks")
    (tmp_path / "existing").mkdir()
    with pytest.raises(ProductFlowError, match="will not be overwritten"):
        validate_project_destination(tmp_path, "existing")
    with pytest.raises(ProductFlowError, match="cannot contain a path separator"):
        validate_project_destination(tmp_path, "nested/project")


def git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_delivery_materializes_a_clean_main_branch_without_overwrite(
    tmp_path: Path,
) -> None:
    source = tmp_path / "workspace"
    source.mkdir()
    git(source, "init", "-b", "main")
    git(source, "config", "user.name", "urntt")
    git(source, "config", "user.email", "urntts@gmail.com")
    (source / "README.md").write_text("ready\n", encoding="utf-8")
    git(source, "add", ".")
    git(source, "commit", "-m", "feat: build result")
    expected_commit = git(source, "rev-parse", "HEAD")
    git(source, "checkout", "--detach")
    destination = tmp_path / "task-manager"

    delivered = deliver_product_workspace(
        source,
        destination,
        expected_commit=expected_commit,
    )

    assert delivered == destination
    assert (destination / "README.md").read_text(encoding="utf-8") == "ready\n"
    assert git(destination, "branch", "--show-current") == "main"
    assert git(destination, "status", "--short") == ""
    assert git(destination, "remote") == ""
    assert not list(tmp_path.glob(".*.sat-*.tmp"))


def test_delivery_rejects_generated_symbolic_links(tmp_path: Path) -> None:
    source = tmp_path / "workspace"
    source.mkdir()
    git(source, "init", "-b", "main")
    git(source, "config", "user.name", "urntt")
    git(source, "config", "user.email", "urntts@gmail.com")
    (source / "outside-link").symlink_to(Path(os.devnull))
    git(source, "add", "outside-link")
    git(source, "commit", "-m", "feat: add unsafe link")
    expected_commit = git(source, "rev-parse", "HEAD")

    with pytest.raises(ProductFlowError, match="symbolic link"):
        deliver_product_workspace(
            source,
            tmp_path / "result",
            expected_commit=expected_commit,
        )


def test_delivery_materializes_only_the_accepted_commit(tmp_path: Path) -> None:
    source = tmp_path / "workspace"
    source.mkdir()
    git(source, "init", "-b", "main")
    git(source, "config", "user.name", "urntt")
    git(source, "config", "user.email", "urntts@gmail.com")
    (source / "result.txt").write_text("accepted\n", encoding="utf-8")
    git(source, "add", ".")
    git(source, "commit", "-m", "feat: accepted result")
    accepted = git(source, "rev-parse", "HEAD")
    (source / "untracked.txt").write_text("late change\n", encoding="utf-8")

    delivered = deliver_product_workspace(
        source,
        tmp_path / "result",
        expected_commit=accepted,
    )

    assert (delivered / "result.txt").is_file()
    assert not (delivered / "untracked.txt").exists()
    assert git(delivered, "rev-parse", "HEAD") == accepted


def test_delivery_never_replaces_a_destination_that_appears_late(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "workspace"
    source.mkdir()
    git(source, "init", "-b", "main")
    git(source, "config", "user.name", "urntt")
    git(source, "config", "user.email", "urntts@gmail.com")
    (source / "result.txt").write_text("accepted\n", encoding="utf-8")
    git(source, "add", ".")
    git(source, "commit", "-m", "feat: accepted result")
    accepted = git(source, "rev-parse", "HEAD")
    destination = tmp_path / "result"
    publish = product._rename_no_replace

    def race(staging: Path, target: Path) -> None:
        target.mkdir()
        (target / "user-file.txt").write_text("keep\n", encoding="utf-8")
        publish(staging, target)

    monkeypatch.setattr(product, "_rename_no_replace", race)

    with pytest.raises(ProductFlowError, match="appeared during the run"):
        deliver_product_workspace(
            source,
            destination,
            expected_commit=accepted,
        )

    assert (destination / "user-file.txt").read_text(encoding="utf-8") == "keep\n"
    assert not list(tmp_path.glob(".*.sat-*.tmp"))
