"""Regression coverage for npm host links and partial state removal."""

from __future__ import annotations

import errno
import json
import os
import shutil
from pathlib import Path

import pytest

from software_agent_team import cli, uninstall_state, workspace_mounts
from software_agent_team.product import ProductStatePaths, ensure_product_state
from software_agent_team.runtime_configuration import RuntimeConfigurationError
from software_agent_team.user_configuration import UserConfiguration


def private_runtime(path: Path) -> Path:
    root = path / ".sat/openclaw"
    host = root / "runtime/node_modules/openclaw"
    host.mkdir(parents=True)
    (root / ".sat-owned-runtime").write_text(
        f"software-agent-team-openclaw-runtime-v1\nroot={root}\n"
    )
    (host / "sentinel").write_bytes(b"external SDK bytes must stay untouched")
    return root


def test_configuration_stage_rebinds_npm_peers_without_copying_sdk_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = private_runtime(tmp_path / "old")
    current = private_runtime(tmp_path / "current")
    monkeypatch.setattr(cli, "DEFAULT_OPENCLAW_BINARY", current / "bin/openclaw")
    state = tmp_path / "state/openclaw"
    package = state / "npm/projects/provider/node_modules/@openclaw/deepseek-provider"
    peer = package / "node_modules/openclaw"
    peer.parent.mkdir(parents=True)
    peer.symlink_to(old / "runtime/node_modules/openclaw")
    executable = package / "bin/provider.js"
    executable.parent.mkdir()
    executable.write_bytes(b"provider command")
    bin_link = package.parent.parent / ".bin/provider"
    bin_link.parent.mkdir()
    bin_link.symlink_to(executable)
    credential = state / "credentials/auth.json"
    credential.parent.mkdir()
    credential.write_bytes(b"opaque credential fixture")
    config = tmp_path / "config.json"

    with cli._staged_openclaw_state(state) as (candidate, _candidate_config):
        staged_peer = candidate / peer.relative_to(state)
        assert staged_peer.is_symlink()
        assert os.readlink(staged_peer) == str(
            current / "runtime/node_modules/openclaw"
        )
        assert (
            candidate / bin_link.relative_to(state)
        ).read_bytes() == b"provider command"
        assert os.readlink(peer) == str(old / "runtime/node_modules/openclaw")
        assert (
            candidate / credential.relative_to(state)
        ).read_bytes() == credential.read_bytes()
        cli._commit_configuration_transaction(
            UserConfiguration(model="provider/model"),
            user_path=config,
            live_openclaw_state=state,
            staged_openclaw_state=candidate,
        )

    # Internal links must still work after the candidate directory is renamed.
    assert bin_link.read_bytes() == b"provider command"
    assert peer.resolve() == current / "runtime/node_modules/openclaw"
    assert credential.read_bytes() == b"opaque credential fixture"
    assert not tuple(state.parent.glob(".openclaw.*-*"))
    for root in (old, current):
        assert (
            root / "runtime/node_modules/openclaw/sentinel"
        ).read_bytes() == b"external SDK bytes must stay untouched"


@pytest.mark.parametrize(
    "relative", ["credentials/auth.json", "npm/projects/provider/node_modules/external"]
)
def test_configuration_stage_rejects_external_authority_links_without_mutation(
    tmp_path: Path,
    relative: str,
) -> None:
    state = tmp_path / "state/openclaw"
    link = state / relative
    link.parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "auth.json"
    sentinel.write_bytes(b"external credential")
    link.symlink_to(outside, target_is_directory=True)
    with (
        pytest.raises(
            RuntimeConfigurationError, match="unsupported symbolic link"
        ) as failure,
        cli._staged_openclaw_state(state),
    ):
        pytest.fail("unsafe state must not be exposed to the SDK")
    assert str(link) in str(failure.value)
    assert sentinel.read_bytes() == b"external credential"
    assert link.is_symlink()
    assert not tuple(state.parent.glob(".openclaw.candidate-*"))


def sdk_skills_cache(paths: ProductStatePaths) -> Path:
    workspace = (
        paths.openclaw
        / "sandbox/skills-workspaces/agent-generalist_developer-sat-s-932e92a1"
    )
    leaf = workspace / workspace_mounts.SANDBOX_SKILL_MOUNT
    leaf.mkdir(parents=True)
    return leaf


def foreign_cache_owner(
    paths: ProductStatePaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Model the external kernel ownership boundary without root-only tests."""

    cache = paths.openclaw / "sandbox"
    original = Path.lstat

    def metadata(path: Path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path == cache:
            fields = list(result)
            fields[4] = 1 if os.geteuid() == 0 else 0
            return os.stat_result(fields)
        return result

    monkeypatch.setattr(Path, "lstat", metadata)


def test_sdk_cache_repair_refuses_nonempty_mount_before_any_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = ProductStatePaths.below(tmp_path / "state")
    ensure_product_state(paths)
    leaf = sdk_skills_cache(paths)
    foreign_cache_owner(paths, monkeypatch)
    sentinel = leaf / "preserved.txt"
    sentinel.write_bytes(b"unknown data")
    with pytest.raises(workspace_mounts.WorkspaceMountError, match="non-empty"):
        workspace_mounts.repair_legacy_openclaw_skill_workspaces(
            openclaw_state=paths.openclaw,
            policy_path=tmp_path / "absent-policy",
            runner=lambda *a, **k: pytest.fail(
                "no Docker command before shape validation"
            ),
        )
    assert sentinel.read_bytes() == b"unknown data"


def test_sdk_cache_repair_refuses_redirected_skill_leaf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = ProductStatePaths.below(tmp_path / "state")
    ensure_product_state(paths)
    leaf = sdk_skills_cache(paths)
    foreign_cache_owner(paths, monkeypatch)
    leaf.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    leaf.symlink_to(outside, target_is_directory=True)
    with pytest.raises(workspace_mounts.WorkspaceMountError, match="real directory"):
        workspace_mounts.repair_legacy_openclaw_skill_workspaces(
            openclaw_state=paths.openclaw,
            policy_path=tmp_path / "absent-policy",
            runner=lambda *a, **k: pytest.fail(
                "no Docker command for redirected cache"
            ),
        )
    assert not tuple(outside.iterdir())
    assert leaf.is_symlink()


def test_owned_nonempty_sdk_cache_does_not_need_privileged_repair(
    tmp_path: Path,
) -> None:
    paths = ProductStatePaths.below(tmp_path / "state")
    ensure_product_state(paths)
    leaf = sdk_skills_cache(paths)
    payload = leaf / "materialized-skill.md"
    payload.write_bytes(b"SDK materialized skill")
    assert (
        workspace_mounts.repair_legacy_openclaw_skill_workspaces(
            openclaw_state=paths.openclaw,
            policy_path=tmp_path / "missing-policy",
            runner=lambda *a, **k: pytest.fail(
                "owned caches need no Docker or image lookup"
            ),
        )
        == ()
    )
    assert payload.read_bytes() == b"SDK materialized skill"


def test_partial_provider_purge_reports_real_completed_export_and_syscall(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths = ProductStatePaths.below(tmp_path / "state")
    ensure_product_state(paths)
    config = tmp_path / "config.json"
    config.write_text("{}\n")
    run = paths.runs / "finished/run.json"
    run.parent.mkdir()
    run.write_text(json.dumps({"phase": "completed"}))
    leaf = sdk_skills_cache(paths)
    credential = paths.openclaw / "credentials/auth.json"
    credential.parent.mkdir()
    credential.write_bytes(b"opaque credential never exported")
    original_remove = shutil.rmtree

    def deny_provider_remove(path: Path, *args, **kwargs):
        if path == paths.openclaw:
            raise PermissionError(errno.EACCES, "Permission denied", str(leaf))
        return original_remove(path, *args, **kwargs)

    monkeypatch.setattr(uninstall_state.shutil, "rmtree", deny_provider_remove)
    export = tmp_path / "export"
    status = uninstall_state.main(
        [
            "apply",
            "--state-root",
            str(paths.root),
            "--config-path",
            str(config),
            "--config-policy",
            "purge",
            "--data-policy",
            "purge",
            "--provider-policy",
            "purge",
            "--export-to",
            str(export),
        ]
    )
    captured = capsys.readouterr()
    assert status == 1
    assert str(export) in captured.out and "exported preserved state" in captured.out
    assert "deleted SAT configuration" in captured.out
    assert "errno=13 (EACCES)" in captured.err and str(leaf) in captured.err
    assert (export / "EXPORT.txt").is_file()
    assert (export / "data/runs/finished/run.json").is_file()
    assert not (export / "data/openclaw").exists()
    assert not config.exists() and not paths.runs.exists()
    assert credential.read_bytes() == b"opaque credential never exported"
    assert "opaque credential" not in captured.out + captured.err
    assert (paths.root / ".sat-state-v1").is_file()
