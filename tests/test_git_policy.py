"""Cross real host Git consumers with ambient and repository callback traps."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from software_agent_team import (
    benchmark_seed,
    full_gate,
    managed_install,
    product,
    release_tools,
    releases,
    updates,
    versioning,
)
from software_agent_team.git_workspace import GitWorkspaceManager


def git(repository: Path, *arguments: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repository), *arguments], text=True
    ).strip()


def repository(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    root.mkdir()
    git(root, "init", "--initial-branch=main")
    git(root, "config", "user.name", "urntt")
    git(root, "config", "user.email", "urntts@gmail.com")
    git(root, "commit", "--allow-empty", "-m", "test: initialize source")
    return root


def test_real_host_git_consumers_ignore_ambient_callback_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = repository(tmp_path)
    marker = tmp_path / "callback-ran"
    callback = tmp_path / "callback"
    callback.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n")
    callback.chmod(0o755)
    config = tmp_path / "global-config"
    config.write_text(f"[core]\nfsmonitor = {callback}\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    # An inherited command-scope injection must not escape the clean environment.
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(callback))
    assert product._git_output(root, "status", "--porcelain") == ""
    assert versioning._run_git(root, "status", "--porcelain") == ""
    assert updates._git_output(root, "status", "--porcelain") == ""
    assert release_tools._git(root, "status", "--porcelain") == ""
    assert benchmark_seed._run_git(root, "status", "--porcelain") == ""
    assert (
        managed_install._capture_command(
            ("git", "-C", str(root), "status", "--porcelain")
        )
        == ""
    )
    assert full_gate._git_fact(root)["dirty"] is False
    assert releases.git_archive_digest(root).startswith("sha256:")
    assert (
        GitWorkspaceManager(tmp_path / "workspaces")._git_text(
            root, ["status", "--porcelain"]
        )
        == ""
    )
    cloned = tmp_path / "cloned"
    product._clone_git_result(root, cloned)
    assert product._git_output(cloned, "rev-parse", "HEAD") == git(
        root, "rev-parse", "HEAD"
    )
    assert not marker.exists()


@pytest.mark.parametrize(
    "key", ["core.fsmonitor", "core.hooksPath", "filter.trap.smudge"]
)
def test_delivery_refuses_repository_callbacks_before_clone(
    tmp_path: Path, key: str
) -> None:
    root = repository(tmp_path)
    revision = git(root, "rev-parse", "HEAD")
    marker = tmp_path / "callback-ran"
    git(root, "config", key, f"touch {marker}")
    destination = tmp_path / "result"
    with pytest.raises(product.ProductFlowError, match="unsafe Git callbacks"):
        product.deliver_product_workspace(root, destination, expected_commit=revision)
    assert not destination.exists()
    assert not marker.exists()
