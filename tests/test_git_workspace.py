"""Tests for isolated Git worktrees and verified iteration snapshots."""

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
    WorktreeAlreadyExistsError,
    WorktreeIntegrityError,
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


def initialize_repository(root: Path, name: str = "source") -> Path:
    """Create a clean one-commit source repository."""

    repository = root / name
    repository.mkdir()
    git(repository, "init", "-b", "main")
    git(repository, "config", "user.name", "urntt")
    git(repository, "config", "user.email", "urntts@gmail.com")
    (repository / "README.md").write_text("# Seed\n", encoding="utf-8")
    git(repository, "add", "README.md")
    git(repository, "commit", "-m", "chore: seed repository")
    return repository


def manager(root: Path) -> GitWorkspaceManager:
    """Create a deterministic manager for tests."""

    return GitWorkspaceManager(root, clock=lambda: FIXED_TIME)


def commit_change(worktree: Path, name: str = "app.py") -> str:
    """Commit one implementation change and return its full commit ID."""

    (worktree / name).write_text("print('ready')\n", encoding="utf-8")
    git(worktree, "add", name)
    git(worktree, "commit", "--no-verify", "-m", f"feat: add {name}")
    return git(worktree, "rev-parse", "HEAD").stdout.strip()


def test_prepare_creates_a_clean_detached_worktree(tmp_path: Path) -> None:
    source = initialize_repository(tmp_path)
    base_commit = git(source, "rev-parse", "HEAD").stdout.strip()
    worktrees = tmp_path / "worktrees"
    workspace_manager = manager(worktrees)

    workspace = workspace_manager.prepare(
        "task-manager-001",
        source_repository=source,
    )

    assert workspace.base_commit == base_commit
    assert Path(workspace.worktree_path) == worktrees / "task-manager-001"
    assert (
        workspace_manager.verify_workspace(
            workspace,
            expected_commit=base_commit,
            require_clean=True,
        )
        == base_commit
    )
    symbolic = git(
        Path(workspace.worktree_path), "symbolic-ref", "-q", "HEAD", check=False
    )
    assert symbolic.returncode == 1


def test_snapshot_records_a_clean_descendant_without_moving_source_branch(
    tmp_path: Path,
) -> None:
    source = initialize_repository(tmp_path)
    source_head = git(source, "rev-parse", "HEAD").stdout.strip()
    workspace_manager = manager(tmp_path / "worktrees")
    workspace = workspace_manager.prepare(
        "task-manager-001",
        source_repository=source,
    )
    worktree = Path(workspace.worktree_path)

    output_commit = commit_change(worktree)
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
    workspace_manager = manager(tmp_path / "worktrees")
    workspace = workspace_manager.prepare("run-001", source_repository=source)
    commit_change(Path(workspace.worktree_path))
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

    with pytest.raises(WorktreeIntegrityError, match="changed files"):
        validate_work_result_snapshot(result, snapshot)


def test_snapshot_counts_multiple_agent_commits(tmp_path: Path) -> None:
    source = initialize_repository(tmp_path)
    workspace_manager = manager(tmp_path / "worktrees")
    workspace = workspace_manager.prepare("run-001", source_repository=source)
    worktree = Path(workspace.worktree_path)
    commit_change(worktree, "app.py")
    commit_change(worktree, "tests.py")

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
        manager(tmp_path / "worktrees").prepare(
            "run-001",
            source_repository=source,
        )


def test_nested_source_path_is_rejected(tmp_path: Path) -> None:
    source = initialize_repository(tmp_path)

    with pytest.raises(RepositoryValidationError, match="Git working tree"):
        manager(tmp_path / "worktrees").prepare(
            "run-001",
            source_repository=source / ".git",
        )


def test_existing_worktree_path_is_not_deleted(tmp_path: Path) -> None:
    source = initialize_repository(tmp_path)
    occupied = tmp_path / "worktrees" / "run-001"
    occupied.mkdir(parents=True)
    marker = occupied / "keep.txt"
    marker.write_text("keep\n", encoding="utf-8")

    with pytest.raises(WorktreeAlreadyExistsError, match="already exists"):
        manager(tmp_path / "worktrees").prepare(
            "run-001",
            source_repository=source,
        )

    assert marker.read_text(encoding="utf-8") == "keep\n"


def test_explicit_recovery_adopts_only_a_matching_prepared_worktree(
    tmp_path: Path,
) -> None:
    source = initialize_repository(tmp_path)
    workspace_manager = manager(tmp_path / "worktrees")
    prepared = workspace_manager.prepare("run-001", source_repository=source)

    recovered = manager(tmp_path / "worktrees").recover_prepared(
        "run-001",
        source_repository=source,
    )

    assert recovered == prepared


def test_explicit_recovery_rejects_an_unrelated_existing_directory(
    tmp_path: Path,
) -> None:
    source = initialize_repository(tmp_path)
    occupied = tmp_path / "worktrees" / "run-001"
    occupied.mkdir(parents=True)
    (occupied / "keep.txt").write_text("keep\n", encoding="utf-8")

    with pytest.raises(WorktreeIntegrityError, match="cannot be recovered"):
        manager(tmp_path / "worktrees").recover_prepared(
            "run-001",
            source_repository=source,
        )

    assert (occupied / "keep.txt").is_file()


def test_worktree_root_inside_source_repository_is_rejected(tmp_path: Path) -> None:
    source = initialize_repository(tmp_path)

    with pytest.raises(GitWorkspaceError, match="outside the source"):
        manager(source / "worktrees").prepare(
            "run-001",
            source_repository=source,
        )

    assert not (source / "worktrees").exists()


def test_snapshot_rejects_uncommitted_changes(tmp_path: Path) -> None:
    source = initialize_repository(tmp_path)
    workspace_manager = manager(tmp_path / "worktrees")
    workspace = workspace_manager.prepare("run-001", source_repository=source)
    worktree = Path(workspace.worktree_path)
    (worktree / "dirty.py").write_text("dirty = True\n", encoding="utf-8")

    with pytest.raises(WorktreeIntegrityError, match="uncommitted"):
        workspace_manager.verify_snapshot(
            workspace,
            iteration=1,
            input_commit=workspace.base_commit,
        )


def test_snapshot_rejects_unchanged_head(tmp_path: Path) -> None:
    source = initialize_repository(tmp_path)
    workspace_manager = manager(tmp_path / "worktrees")
    workspace = workspace_manager.prepare("run-001", source_repository=source)

    with pytest.raises(WorktreeIntegrityError, match="no new commit"):
        workspace_manager.verify_snapshot(
            workspace,
            iteration=1,
            input_commit=workspace.base_commit,
        )


def test_snapshot_rejects_an_unknown_input_commit(tmp_path: Path) -> None:
    source = initialize_repository(tmp_path)
    workspace_manager = manager(tmp_path / "worktrees")
    workspace = workspace_manager.prepare("run-001", source_repository=source)
    commit_change(Path(workspace.worktree_path))

    with pytest.raises(WorktreeIntegrityError, match="not available"):
        workspace_manager.verify_snapshot(
            workspace,
            iteration=1,
            input_commit="f" * 40,
        )


def test_snapshot_rejects_a_non_descendant_commit(tmp_path: Path) -> None:
    source = initialize_repository(tmp_path)
    workspace_manager = manager(tmp_path / "worktrees")
    workspace = workspace_manager.prepare("run-001", source_repository=source)
    worktree = Path(workspace.worktree_path)
    tree = git(worktree, "rev-parse", "HEAD^{tree}").stdout.strip()
    unrelated = git(
        worktree,
        "commit-tree",
        tree,
        input_text="unrelated commit\n",
    ).stdout.strip()
    git(worktree, "reset", "--hard", unrelated)

    with pytest.raises(WorktreeIntegrityError, match="not a descendant"):
        workspace_manager.verify_snapshot(
            workspace,
            iteration=1,
            input_commit=workspace.base_commit,
        )


def test_workspace_from_a_different_repository_is_rejected(tmp_path: Path) -> None:
    source = initialize_repository(tmp_path, "source-a")
    other = initialize_repository(tmp_path, "source-b")
    workspace_manager = manager(tmp_path / "worktrees")
    workspace = workspace_manager.prepare("run-001", source_repository=source)
    incorrect = workspace.model_copy(update={"source_repository": str(other)})

    with pytest.raises(WorktreeIntegrityError, match="different repository"):
        workspace_manager.verify_workspace(incorrect)


def test_executable_repository_hook_is_rejected_without_execution(
    tmp_path: Path,
) -> None:
    source = initialize_repository(tmp_path)
    marker = tmp_path / "hook-ran"
    hook = source / ".git" / "hooks" / "post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
    hook.chmod(0o755)

    with pytest.raises(UnsafeRepositoryError, match="executable Git hook"):
        manager(tmp_path / "worktrees").prepare(
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
        manager(tmp_path / "worktrees").prepare(
            "run-001",
            source_repository=source,
        )


def test_tracked_checkout_filter_is_rejected(tmp_path: Path) -> None:
    source = initialize_repository(tmp_path)
    (source / ".gitattributes").write_text("*.txt filter=danger\n", encoding="utf-8")
    git(source, "add", ".gitattributes")
    git(source, "commit", "-m", "test: add unsafe attributes")

    with pytest.raises(UnsafeRepositoryError, match="attributes"):
        manager(tmp_path / "worktrees").prepare(
            "run-001",
            source_repository=source,
        )


def test_snapshot_rechecks_repository_safety_before_status(tmp_path: Path) -> None:
    source = initialize_repository(tmp_path)
    git(source, "config", "extensions.worktreeConfig", "true")
    workspace_manager = manager(tmp_path / "worktrees")
    workspace = workspace_manager.prepare("run-001", source_repository=source)
    worktree = Path(workspace.worktree_path)
    marker = tmp_path / "filter-ran"
    git(
        worktree,
        "config",
        "--worktree",
        "filter.danger.clean",
        f"touch '{marker}'",
    )
    (worktree / ".gitattributes").write_text(
        "*.py filter=danger\n",
        encoding="utf-8",
    )
    (worktree / "app.py").write_text("print('unsafe')\n", encoding="utf-8")

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
        manager(tmp_path / "worktrees").prepare(
            "run-001",
            source_repository=source,
        )


def test_controller_persists_real_workspace_and_snapshot_evidence(
    tmp_path: Path,
) -> None:
    source = initialize_repository(tmp_path)
    workspace_manager = manager(tmp_path / "worktrees")
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
        target=RunPhase.PREPARING_WORKTREE,
        reason="prepare isolated worktree",
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
    commit_change(Path(workspace.worktree_path))
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
