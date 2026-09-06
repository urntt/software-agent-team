"""Tests for the authoritative SAT state lifecycle transaction."""

from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path

import pytest

from software_agent_team.product import (
    ProductStatePaths,
    ensure_product_state,
)
from software_agent_team.state_layout import (
    PRODUCT_STATE_CATEGORIES,
    StateLifecycleGroup,
)
from software_agent_team.uninstall_state import (
    UninstallPolicy,
    UninstallStateError,
    UninstallStateRequest,
    apply_uninstall_state,
    preflight_uninstall_state,
)


def prepare_state(tmp_path: Path) -> tuple[ProductStatePaths, Path]:
    """Create every authoritative state category with representative content."""

    paths = ProductStatePaths.below((tmp_path / "state").resolve())
    ensure_product_state(paths)
    (paths.runs / "finished").mkdir()
    (paths.runs / "finished/run.json").write_text(
        json.dumps({"phase": "completed"}) + "\n",
        encoding="utf-8",
    )
    (paths.workspaces / "finished").mkdir()
    (paths.workspaces / "finished/result.py").write_text(
        "print('done')\n",
        encoding="utf-8",
    )
    (paths.sources / "finished").mkdir()
    (paths.sources / "finished/README.md").write_text("source\n", encoding="utf-8")
    (paths.planning / "finished").mkdir()
    (paths.planning / "finished/turn.json").write_text("{}\n", encoding="utf-8")
    (paths.self_checks / "finished").mkdir()
    (paths.self_checks / "finished/0001.json").write_text("{}\n", encoding="utf-8")
    (paths.openclaw / "credentials").mkdir()
    (paths.openclaw / "credentials/provider.json").write_text(
        "private\n",
        encoding="utf-8",
    )
    config = (tmp_path / "configuration/config.json").resolve()
    config.parent.mkdir()
    config.write_text("{}\n", encoding="utf-8")
    return paths, config


def request_for(
    paths: ProductStatePaths,
    config: Path,
    *,
    config_policy: UninstallPolicy = UninstallPolicy.KEEP,
    data_policy: UninstallPolicy = UninstallPolicy.KEEP,
    provider_policy: UninstallPolicy = UninstallPolicy.KEEP,
    export_to: Path | None = None,
) -> UninstallStateRequest:
    """Build one explicit state lifecycle request."""

    return UninstallStateRequest(
        state_root=paths.root,
        config_path=config,
        config_policy=config_policy,
        data_policy=data_policy,
        provider_policy=provider_policy,
        export_to=export_to,
    )


def test_state_manifest_exactly_covers_product_state_paths() -> None:
    manifest_attributes = {category.attribute for category in PRODUCT_STATE_CATEGORIES}
    model_attributes = {field.name for field in fields(ProductStatePaths)} - {"root"}

    assert manifest_attributes == model_attributes
    assert len(manifest_attributes) == len(PRODUCT_STATE_CATEGORIES)
    assert {
        category.attribute
        for category in PRODUCT_STATE_CATEGORIES
        if category.lifecycle_group is StateLifecycleGroup.DATA
    } == {"runs", "workspaces", "sources", "planning", "self_checks"}


def test_export_includes_every_data_category_then_full_purge_removes_state(
    tmp_path: Path,
) -> None:
    paths, config = prepare_state(tmp_path)
    export = (tmp_path / "backup").resolve()

    result = apply_uninstall_state(
        request_for(
            paths,
            config,
            config_policy=UninstallPolicy.PURGE,
            data_policy=UninstallPolicy.PURGE,
            provider_policy=UninstallPolicy.PURGE,
            export_to=export,
        )
    )

    assert result.state_root_removed is True
    assert not paths.root.exists()
    assert not config.exists()
    assert (export / "configuration/config.json").is_file()
    assert (export / "data/runs/finished/run.json").is_file()
    assert (export / "data/workspaces/finished/result.py").is_file()
    assert (export / "data/sources/finished/README.md").is_file()
    assert (export / "data/planning/finished/turn.json").is_file()
    assert (export / "data/self-checks/finished/0001.json").is_file()
    assert not (export / "data/process-leases").exists()
    assert not (export / "data/openclaw").exists()
    manifest = (export / "EXPORT.txt").read_text(encoding="utf-8")
    assert "self_checks=yes" in manifest
    assert "process_leases=excluded" in manifest
    assert "provider_credentials=excluded" in manifest


def test_partial_purge_preserves_unselected_data_and_marker(tmp_path: Path) -> None:
    paths, config = prepare_state(tmp_path)

    result = apply_uninstall_state(
        request_for(
            paths,
            config,
            provider_policy=UninstallPolicy.PURGE,
        )
    )

    assert result.state_root_removed is False
    assert config.is_file()
    assert (paths.self_checks / "finished/0001.json").is_file()
    assert (paths.workspaces / "finished/result.py").is_file()
    assert not paths.openclaw.exists()
    assert not paths.process_leases.exists()
    assert (paths.root / ".sat-state-v1").is_file()


def test_active_run_blocks_every_uninstall_mode_before_changes(tmp_path: Path) -> None:
    paths, config = prepare_state(tmp_path)
    active = paths.runs / "active/run.json"
    active.parent.mkdir()
    active.write_text('{"phase":"implementing"}\n', encoding="utf-8")
    export = (tmp_path / "backup").resolve()
    request = request_for(
        paths,
        config,
        config_policy=UninstallPolicy.PURGE,
        data_policy=UninstallPolicy.PURGE,
        provider_policy=UninstallPolicy.PURGE,
        export_to=export,
    )

    with pytest.raises(UninstallStateError, match="active SAT run"):
        preflight_uninstall_state(request)
    with pytest.raises(UninstallStateError, match="active SAT run"):
        apply_uninstall_state(request)

    assert config.is_file()
    assert active.is_file()
    assert (paths.openclaw / "credentials/provider.json").is_file()
    assert not export.exists()


def test_unknown_category_blocks_before_selected_deletion(tmp_path: Path) -> None:
    paths, config = prepare_state(tmp_path)
    unknown = paths.root / "future-state"
    unknown.mkdir()
    request = request_for(
        paths,
        config,
        config_policy=UninstallPolicy.PURGE,
        data_policy=UninstallPolicy.PURGE,
        provider_policy=UninstallPolicy.PURGE,
    )

    with pytest.raises(UninstallStateError, match="unknown lifecycle category"):
        apply_uninstall_state(request)

    assert config.is_file()
    assert unknown.is_dir()
    assert (paths.self_checks / "finished/0001.json").is_file()


def test_symbolic_run_entry_is_never_followed_during_liveness_check(
    tmp_path: Path,
) -> None:
    paths, config = prepare_state(tmp_path)
    outside = tmp_path / "outside-run"
    outside.mkdir()
    (outside / "run.json").write_text('{"phase":"completed"}\n', encoding="utf-8")
    (paths.runs / "redirected").symlink_to(outside, target_is_directory=True)

    with pytest.raises(UninstallStateError, match="must not be a symbolic link"):
        preflight_uninstall_state(request_for(paths, config))

    assert (outside / "run.json").is_file()


def test_full_purge_is_idempotent_when_selected_state_is_already_absent(
    tmp_path: Path,
) -> None:
    paths = ProductStatePaths.below((tmp_path / "missing-state").resolve())
    config = (tmp_path / "missing-config.json").resolve()
    request = request_for(
        paths,
        config,
        config_policy=UninstallPolicy.PURGE,
        data_policy=UninstallPolicy.PURGE,
        provider_policy=UninstallPolicy.PURGE,
    )

    first = apply_uninstall_state(request)
    second = apply_uninstall_state(request)

    assert first.state_root_removed is False
    assert second == first
