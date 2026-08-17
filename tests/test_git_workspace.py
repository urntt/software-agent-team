"""Tests for isolated Git clones and verified iteration snapshots."""

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from software_agent_team.artifacts import (
    AgentRole,
    ArtifactKind,
    ArtifactReference,
    TaskBrief,
    WorkResult,
)
from software_agent_team.git_workspace import (
    GitWorkspaceError,
    GitWorkspaceManager,
    RepositoryValidationError,
    UnsafeRepositoryError,
    WorkspaceAlreadyExistsError,
    WorkspaceIntegrityError,
    validate_work_result_snapshot,
)
from software_agent_team.run_control import RunController, RunPhase, RunStore
from software_agent_team.teams import load_team_manifest

REPOSITORY_ROOT = Path(__file__).parents[1]
TEAM_CONFIG = REPOSITORY_ROOT / "configs" / "teams.json"
TASK_BRIEF = REPOSITORY_ROOT / "examples" / "task-brief.json"
FIXED_TIME = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
SHA256 = "a" * 64


def git(
    repository: Path,
    *args: str,
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run Git without a shell in a test-owned repository."""

    return subprocess.run(
        ["git", "-C", str(repository), *args],
        check=check,
        capture_output=True,
        text=True,
        input=input_text,
    )


def initialize_repository(
    root: Path,
    name: str = "source",
    seed: str = "# Seed\n",
) -> Path:
    """Create a clean one-commit source repository."""

    repository = root / name
    repository.mkdir()
    git(repository, "init", "-b", "main")
    git(repository, "config", "user.name", "urntt")
    git(repository, "config", "user.email", "urntts@gmail.com")
    (repository / "README.md").write_text(seed, encoding="utf-8")
    git(repository, "add", "README.md")
    git(repository, "commit", "-m", "chore: seed repository")
    return repository


def manager(root: Path) -> GitWorkspaceManager:
    """Create a deterministic manager for tests."""

    return GitWorkspaceManager(root, clock=lambda: FIXED_TIME)


def commit_change(workspace: Path, name: str = "app.py") -> str:
    """Commit one implementation change and return its full commit ID."""

    (workspace / name).write_text("print('ready')\n", encoding="utf-8")
    git(workspace, "add", name)
    git(workspace, "commit", "--no-verify", "-m", f"feat: add {name}")
    return git(workspace, "rev-parse", "HEAD").stdout.strip()


def test_prepare_creates_a_clean_detached_standalone_clone(tmp_path: Path) -> None:
    source = initialize_repository(tmp_path)
    base_commit = git(source, "rev-parse", "HEAD").stdout.strip()
    workspaces = tmp_path / "workspaces"
    workspace_manager = manager(workspaces)

    workspace = workspace_manager.prepare(
        "task-manager-001",
        source_repository=source,
    )

    assert workspace.base_commit == base_commit
    workspace_path = Path(workspace.workspace_path)
    assert workspace_path == workspaces / "task-manager-001"
    assert (
        workspace_manager.verify_workspace(
            workspace,
            expected_commit=base_commit,
            require_clean=True,
        )
        == base_commit
    )
    symbolic = git(workspace_path, "symbolic-ref", "-q", "HEAD", check=False)
    assert symbolic.returncode == 1
    assert (workspace_path / ".git").is_dir()
    assert git(workspace_path, "remote").stdout == ""
    assert not (workspace_path / ".git" / "objects" / "info" / "alternates").exists()


def test_snapshot_records_a_clean_descendant_without_moving_source_branch(
    tmp_path: Path,
) -> None:
    source = initialize_repository(tmp_path)
    source_head = git(source, "rev-parse", "HEAD").stdout.strip()
    workspace_manager = manager(tmp_path / "workspaces")
    workspace = workspace_manager.prepare(
        "task-manager-001",
        source_repository=source,
    )
    run_workspace = Path(workspace.workspace_path)

    output_commit = commit_change(run_workspace)
    snapshot = workspace_manager.verify_snapshot(
        workspace,
        iteration=1,
        input_commit=source_head,
    )

    assert snapshot.input_commit == source_head
    assert snapshot.output_commit == output_commit
    assert snapshot.commit_count == 1
    assert snapshot.changed_files == ("app.py",)
    assert git(source, "rev-parse", "main").stdout.strip() == source_head
    assert not (source / "app.py").exists()
    assert git(source, "cat-file", "-e", output_commit, check=False).returncode != 0

    result = WorkResult(
        run_id=workspace.run_id,
        team_id="function_specialized",
        producer=AgentRole.GENERALIST_DEVELOPER,
        created_at=FIXED_TIME,
        iteration=1,
        input_commit=snapshot.input_commit,
        output_commit=snapshot.output_commit,
        summary="Implemented the application entry point.",
        completed_tasks=("TASK_APPLICATION",),
        changed_files=snapshot.changed_files,
    )
    validate_work_result_snapshot(result, snapshot)


def test_work_result_must_match_verified_snapshot(tmp_path: Path) -> None:
    source = initialize_repository(tmp_path)
    workspace_manager = manager(tmp_path / "workspaces")
    workspace = workspace_manager.prepare("run-001", source_repository=source)
    commit_change(Path(workspace.workspace_path))
    snapshot = workspace_manager.verify_snapshot(
        workspace,
        iteration=1,
        input_commit=workspace.base_commit,
    )
    result = WorkResult(
        run_id=workspace.run_id,
        team_id="function_specialized",
        producer=AgentRole.GENERALIST_DEVELOPER,
        created_at=FIXED_TIME,
        iteration=1,
        input_commit=snapshot.input_commit,
        output_commit=snapshot.output_commit,
        summary="Claimed a different file set.",
        completed_tasks=("TASK_APPLICATION",),
        changed_files=("different.py",),
    )

    with pytest.raises(WorkspaceIntegrityError, match="changed files"):
        validate_work_result_snapshot(result, snapshot)


def test_snapshot_counts_multiple_agent_commits(tmp_path: Path) -> None:
    source = initialize_repository(tmp_path)
    workspace_manager = manager(tmp_path / "workspaces")
    workspace = workspace_manager.prepare("run-001", source_repository=source)
    run_workspace = Path(workspace.workspace_path)
    commit_change(run_workspace, "app.py")
    commit_change(run_workspace, "tests.py")

    snapshot = workspace_manager.verify_snapshot(
        workspace,
        iteration=1,
        input_commit=workspace.base_commit,
    )

    assert snapshot.commit_count == 2
    assert snapshot.changed_files == ("app.py", "tests.py")


def test_dirty_source_repository_is_rejected(tmp_path: Path) -> None:
    source = initialize_repository(tmp_path)
    (source / "untracked.txt").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(RepositoryValidationError, match="must be clean"):
        manager(tmp_path / "workspaces").prepare(
            "run-001",
            source_repository=source,
        )


def test_source_repository_requires_local_commit_identity(tmp_path: Path) -> None:
    source = initialize_repository(tmp_path)
    git(source, "config", "--local", "--unset", "user.email")

    with pytest.raises(
        RepositoryValidationError,
        match=r"user\.name and user\.email",
    ):
        manager(tmp_path / "workspaces").prepare(
            "run-001",
            source_repository=source,
        )


def test_source_preflight_validates_without_creating_a_workspace(
    tmp_path: Path,
) -> None:
    source = initialize_repository(tmp_path)
    workspace_root = tmp_path / "workspaces"
    workspace_manager = manager(workspace_root)

    commit = workspace_manager.validate_source_repository(source)

    assert commit == git(source, "rev-parse", "HEAD").stdout.strip()
    assert not workspace_root.exists()


def test_source_preflight_rejects_missing_local_commit_identity(
    tmp_path: Path,
) -> None:
    source = initialize_repository(tmp_path)
    git(source, "config", "--local", "--unset", "user.name")

    with pytest.raises(
        RepositoryValidationError,
        match=r"user\.name and user\.email",
    ):
        manager(tmp_path / "workspaces").validate_source_repository(source)

    assert not (tmp_path / "workspaces").exists()


def test_nested_source_path_is_rejected(tmp_path: Path) -> None:
    source = initialize_repository(tmp_path)

    with pytest.raises(RepositoryValidationError, match="Git working tree"):
        manager(tmp_path / "workspaces").prepare(
            "run-001",
            source_repository=source / ".git",
        )


def test_existing_workspace_path_is_not_deleted(tmp_path: Path) -> None:
    source = initialize_repository(tmp_path)
    occupied = tmp_path / "workspaces" / "run-001"
    occupied.mkdir(parents=True)
    marker = occupied / "keep.txt"
    marker.write_text("keep\n", encoding="utf-8")

    with pytest.raises(WorkspaceAlreadyExistsError, match="already exists"):
        manager(tmp_path / "workspaces").prepare(
            "run-001",
            source_repository=source,
        )

    assert marker.read_text(encoding="utf-8") == "keep\n"


def test_explicit_recovery_adopts_only_a_matching_prepared_workspace(
    tmp_path: Path,
) -> None:
    source = initialize_repository(tmp_path)
    workspace_manager = manager(tmp_path / "workspaces")
    prepared = workspace_manager.prepare("run-001", source_repository=source)

    recovered = manager(tmp_path / "workspaces").recover_prepared(
        "run-001",
        source_repository=source,
    )

    assert recovered == prepared


def test_explicit_recovery_rejects_an_unrelated_existing_directory(
    tmp_path: Path,
) -> None:
    source = initialize_repository(tmp_path)
    occupied = tmp_path / "workspaces" / "run-001"
    occupied.mkdir(parents=True)
    (occupied / "keep.txt").write_text("keep\n", encoding="utf-8")

    with pytest.raises(WorkspaceIntegrityError, match="cannot be recovered"):
        manager(tmp_path / "workspaces").recover_prepared(
            "run-001",
            source_repository=source,
        )

    assert (occupied / "keep.txt").is_file()


def test_workspace_root_inside_source_repository_is_rejected(tmp_path: Path) -> None:
    source = initialize_repository(tmp_path)

    with pytest.raises(GitWorkspaceError, match="outside the source"):
        manager(source / "workspaces").prepare(
            "run-001",
            source_repository=source,
        )

    assert not (source / "workspaces").exists()


def test_snapshot_rejects_uncommitted_changes(tmp_path: Path) -> None:
    source = initialize_repository(tmp_path)
    workspace_manager = manager(tmp_path / "workspaces")
    workspace = workspace_manager.prepare("run-001", source_repository=source)
    run_workspace = Path(workspace.workspace_path)
    (run_workspace / "dirty.py").write_text("dirty = True\n", encoding="utf-8")

    with pytest.raises(WorkspaceIntegrityError, match="uncommitted"):
        workspace_manager.verify_snapshot(
            workspace,
            iteration=1,
            input_commit=workspace.base_commit,
        )


def test_snapshot_rejects_unchanged_head(tmp_path: Path) -> None:
    source = initialize_repository(tmp_path)
    workspace_manager = manager(tmp_path / "workspaces")
    workspace = workspace_manager.prepare("run-001", source_repository=source)

    with pytest.raises(WorkspaceIntegrityError, match="no new commit"):
        workspace_manager.verify_snapshot(
            workspace,
            iteration=1,
            input_commit=workspace.base_commit,
        )


def test_snapshot_rejects_an_unknown_input_commit(tmp_path: Path) -> None:
    source = initialize_repository(tmp_path)
    workspace_manager = manager(tmp_path / "workspaces")
    workspace = workspace_manager.prepare("run-001", source_repository=source)
    commit_change(Path(workspace.workspace_path))

    with pytest.raises(WorkspaceIntegrityError, match="not available"):
        workspace_manager.verify_snapshot(
            workspace,
            iteration=1,
            input_commit="f" * 40,
        )


def test_snapshot_rejects_a_non_descendant_commit(tmp_path: Path) -> None:
    source = initialize_repository(tmp_path)
    workspace_manager = manager(tmp_path / "workspaces")
    workspace = workspace_manager.prepare("run-001", source_repository=source)
    run_workspace = Path(workspace.workspace_path)
    tree = git(run_workspace, "rev-parse", "HEAD^{tree}").stdout.strip()
    unrelated = git(
        run_workspace,
        "commit-tree",
        tree,
        input_text="unrelated commit\n",
    ).stdout.strip()
    git(run_workspace, "reset", "--hard", unrelated)

    with pytest.raises(WorkspaceIntegrityError, match="not a descendant"):
        workspace_manager.verify_snapshot(
            workspace,
            iteration=1,
            input_commit=workspace.base_commit,
        )


def test_workspace_from_a_different_repository_is_rejected(tmp_path: Path) -> None:
    source = initialize_repository(tmp_path, "source-a")
    other = initialize_repository(tmp_path, "source-b", "# Other history\n")
    workspace_manager = manager(tmp_path / "workspaces")
    workspace = workspace_manager.prepare("run-001", source_repository=source)
    incorrect = workspace.model_copy(update={"source_repository": str(other)})

    with pytest.raises(
        WorkspaceIntegrityError, match="absent from the recorded source"
    ):
        workspace_manager.verify_workspace(incorrect)


def test_workspace_with_a_remote_is_rejected(tmp_path: Path) -> None:
    source = initialize_repository(tmp_path)
    workspace_manager = manager(tmp_path / "workspaces")
    workspace = workspace_manager.prepare("run-001", source_repository=source)
    git(Path(workspace.workspace_path), "remote", "add", "origin", str(source))

    with pytest.raises(WorkspaceIntegrityError, match="cannot retain a remote"):
        workspace_manager.verify_workspace(workspace)


def test_executable_repository_hook_is_rejected_without_execution(
    tmp_path: Path,
) -> None:
    source = initialize_repository(tmp_path)
    marker = tmp_path / "hook-ran"
    hook = source / ".git" / "hooks" / "post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
    hook.chmod(0o755)

    with pytest.raises(UnsafeRepositoryError, match="executable Git hook"):
        manager(tmp_path / "workspaces").prepare(
            "run-001",
            source_repository=source,
        )

    assert not marker.exists()


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("core.fsmonitor", "dangerous-command"),
        ("filter.danger.smudge", "dangerous-command"),
        ("core.hooksPath", "/tmp/dangerous-hooks"),
    ],
)
def test_unsafe_local_git_configuration_is_rejected(
    tmp_path: Path,
    key: str,
    value: str,
) -> None:
    source = initialize_repository(tmp_path)
    git(source, "config", key, value)

    with pytest.raises(UnsafeRepositoryError, match="config"):
        manager(tmp_path / "workspaces").prepare(
            "run-001",
            source_repository=source,
        )


def test_tracked_checkout_filter_is_rejected(tmp_path: Path) -> None:
    source = initialize_repository(tmp_path)
    (source / ".gitattributes").write_text("*.txt filter=danger\n", encoding="utf-8")
    git(source, "add", ".gitattributes")
    git(source, "commit", "-m", "test: add unsafe attributes")

    with pytest.raises(UnsafeRepositoryError, match="attributes"):
        manager(tmp_path / "workspaces").prepare(
            "run-001",
            source_repository=source,
        )


def test_snapshot_rechecks_repository_safety_before_status(tmp_path: Path) -> None:
    source = initialize_repository(tmp_path)
    workspace_manager = manager(tmp_path / "workspaces")
    workspace = workspace_manager.prepare("run-001", source_repository=source)
    run_workspace = Path(workspace.workspace_path)
    marker = tmp_path / "filter-ran"
    git(
        run_workspace,
        "config",
        "--local",
        "filter.danger.clean",
        f"touch '{marker}'",
    )
    (run_workspace / ".gitattributes").write_text(
        "*.py filter=danger\n",
        encoding="utf-8",
    )
    (run_workspace / "app.py").write_text("print('unsafe')\n", encoding="utf-8")

    with pytest.raises(UnsafeRepositoryError, match="config"):
        workspace_manager.verify_snapshot(
            workspace,
            iteration=1,
            input_commit=workspace.base_commit,
        )

    assert not marker.exists()


def test_gitlink_submodule_entry_is_rejected(tmp_path: Path) -> None:
    source = initialize_repository(tmp_path)
    dependency = initialize_repository(tmp_path, "dependency")
    git(
        source,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        str(dependency),
        "vendor/dependency",
    )
    git(source, "commit", "-m", "test: add submodule")

    with pytest.raises(UnsafeRepositoryError, match="submodules"):
        manager(tmp_path / "workspaces").prepare(
            "run-001",
            source_repository=source,
        )


def test_controller_persists_real_workspace_and_snapshot_evidence(
    tmp_path: Path,
) -> None:
    source = initialize_repository(tmp_path)
    workspace_manager = manager(tmp_path / "workspaces")
    manifest = load_team_manifest(TEAM_CONFIG)
    controller = RunController(
        RunStore(tmp_path / "runs"),
        manifest,
        clock=lambda: FIXED_TIME,
    )
    brief = TaskBrief.model_validate_json(TASK_BRIEF.read_text(encoding="utf-8"))
    record = controller.create(
        brief,
        team_id="function_specialized",
        iteration_limit=2,
    )
    record = controller.advance(
        record.run_id,
        expected_revision=record.revision,
        target=RunPhase.PREPARING_WORKSPACE,
        reason="prepare isolated workspace",
    )
    workspace = workspace_manager.prepare(
        record.run_id,
        source_repository=source,
    )
    record = controller.attach_workspace(
        record.run_id,
        expected_revision=record.revision,
        workspace=workspace,
    )
    record = controller.advance(
        record.run_id,
        expected_revision=record.revision,
        target=RunPhase.IMPLEMENTING,
        reason="begin implementation",
        artifacts=(
            ArtifactReference(
                kind=ArtifactKind.IMPLEMENTATION_PLAN,
                path="implementation-plan.json",
                sha256=SHA256,
            ),
        ),
    )
    commit_change(Path(workspace.workspace_path))
    record = controller.advance(
        record.run_id,
        expected_revision=record.revision,
        target=RunPhase.SNAPSHOTTING,
        reason="verify implementation commit",
        artifacts=(
            ArtifactReference(
                kind=ArtifactKind.WORK_RESULT,
                path="iterations/01/work-result.json",
                sha256=SHA256,
            ),
        ),
    )
    snapshot = workspace_manager.verify_snapshot(
        workspace,
        iteration=1,
        input_commit=workspace.base_commit,
    )

    record = controller.record_snapshot(
        record.run_id,
        expected_revision=record.revision,
        snapshot=snapshot,
    )

    assert record.phase is RunPhase.VERIFYING
    assert record.workspace == workspace
    assert record.snapshots == (snapshot,)
    assert controller.load(record.run_id) == record
