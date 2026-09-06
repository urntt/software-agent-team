"""Tests for user-owned sandbox mountpoints and constrained legacy repair."""

from __future__ import annotations

import os
import subprocess
from collections import deque
from pathlib import Path

import pytest

from software_agent_team.workspace_mounts import (
    SANDBOX_SKILL_MOUNT,
    WorkspaceMountError,
    prepare_sandbox_skill_mountpoint,
    repair_legacy_sandbox_skill_mountpoints,
)


class ScriptedRunner:
    """Return deterministic subprocess results while retaining exact argv."""

    def __init__(self, responses: list[subprocess.CompletedProcess[str]]) -> None:
        self.responses = deque(responses)
        self.calls: list[tuple[str, ...]] = []

    def __call__(
        self, argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(tuple(argv))
        response = self.responses.popleft()
        if len(self.calls) == 2 and response.returncode == 0:
            mount = argv[argv.index("--mount") + 1]
            source = Path(mount.split(",source=", 1)[1].split(",target=", 1)[0])
            for value in argv[argv.index("--") + 1 :]:
                relative = Path(value).relative_to("/sat-workspace")
                (source / relative).rmdir()
        return response


def completed(
    stdout: str = "", *, returncode: int = 0
) -> subprocess.CompletedProcess[str]:
    """Build one typed fake subprocess result."""

    return subprocess.CompletedProcess(
        args=("docker",),
        returncode=returncode,
        stdout=stdout,
        stderr="",
    )


def write_policy(path: Path) -> None:
    """Write the minimum exact policy consumed by legacy repair."""

    path.write_text(
        '{"sandbox":{"backend":"docker","image":"quality:test","pull":"never"}}\n',
        encoding="utf-8",
    )


def test_prepare_sandbox_skill_mountpoint_is_user_owned_and_idempotent(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()

    first = prepare_sandbox_skill_mountpoint(workspace)
    second = prepare_sandbox_skill_mountpoint(workspace)

    assert first == workspace / SANDBOX_SKILL_MOUNT
    assert second == first
    for relative in (Path(".openclaw"), Path(".openclaw/sandbox-skills"), first):
        path = relative if relative.is_absolute() else workspace / relative
        assert path.is_dir()
        assert not path.is_symlink()
        assert path.stat().st_uid == os.geteuid()


def test_prepare_sandbox_skill_mountpoint_refuses_a_symlink_collision(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / ".openclaw").symlink_to(outside, target_is_directory=True)

    with pytest.raises(WorkspaceMountError, match="real directory"):
        prepare_sandbox_skill_mountpoint(workspace)

    assert list(outside.iterdir()) == []


def test_legacy_repair_uses_one_constrained_exact_docker_mount(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspaces = (tmp_path / "workspaces").resolve()
    workspace = workspaces / "run-1"
    target = workspace / SANDBOX_SKILL_MOUNT
    target.mkdir(parents=True)
    policy = tmp_path / "policy.json"
    write_policy(policy)
    monkeypatch.setattr(os, "geteuid", lambda: 1001)
    image_id = "sha256:" + "a" * 64
    runner = ScriptedRunner([completed(image_id + "\n"), completed()])

    repairs = repair_legacy_sandbox_skill_mountpoints(
        workspaces_root=workspaces,
        policy_path=policy,
        runner=runner,
    )

    assert len(repairs) == 1
    assert repairs[0].workspace == workspace
    assert not (workspace / ".openclaw").exists()
    assert runner.calls[0] == (
        "docker",
        "image",
        "inspect",
        "--format",
        "{{.Id}}",
        "quality:test",
    )
    run = runner.calls[1]
    assert "--network" in run and run[run.index("--network") + 1] == "none"
    assert "--cap-drop" in run and run[run.index("--cap-drop") + 1] == "ALL"
    assert "--cap-add" in run and run[run.index("--cap-add") + 1] == "DAC_OVERRIDE"
    assert "--read-only" in run
    assert "--user" in run and run[run.index("--user") + 1] == "0:0"
    assert str(workspace) in run[run.index("--mount") + 1]
    assert run[-3:] == (
        "/sat-workspace/.openclaw/sandbox-skills/skills",
        "/sat-workspace/.openclaw/sandbox-skills",
        "/sat-workspace/.openclaw",
    )


def test_legacy_repair_refuses_foreign_nonempty_content_before_docker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspaces = (tmp_path / "workspaces").resolve()
    target = workspaces / "run-1" / SANDBOX_SKILL_MOUNT
    target.mkdir(parents=True)
    (target / "unexpected.txt").write_text("preserve\n", encoding="utf-8")
    policy = tmp_path / "policy.json"
    write_policy(policy)
    monkeypatch.setattr(os, "geteuid", lambda: 1001)
    runner = ScriptedRunner([])

    with pytest.raises(WorkspaceMountError, match="non-empty"):
        repair_legacy_sandbox_skill_mountpoints(
            workspaces_root=workspaces,
            policy_path=policy,
            runner=runner,
        )

    assert (target / "unexpected.txt").is_file()
    assert runner.calls == []


def test_legacy_repair_does_not_require_docker_without_foreign_mountpoints(
    tmp_path: Path,
) -> None:
    workspaces = (tmp_path / "workspaces").resolve()
    target = workspaces / "run-1" / SANDBOX_SKILL_MOUNT
    target.mkdir(parents=True)
    runner = ScriptedRunner([])

    repairs = repair_legacy_sandbox_skill_mountpoints(
        workspaces_root=workspaces,
        policy_path=tmp_path / "missing-policy.json",
        runner=runner,
    )

    assert repairs == ()
    assert target.is_dir()
    assert runner.calls == []
