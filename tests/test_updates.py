"""Tests for managed update planning and active-install validation."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from software_agent_team.managed_install import (
    MANAGED_ROOT_MARKER_NAME,
    ManagedApplicationMarker,
    ManagedInstallError,
    ManagedInstallPaths,
    ManagedRootMarker,
    ManagedTarget,
)
from software_agent_team.schema_compatibility import supported_schemas
from software_agent_team.updates import (
    ManagedChangeStatus,
    plan_managed_change,
    validate_current_managed_install,
)
from software_agent_team.versioning import (
    ManagedChannel,
    make_installation_record,
    save_installation_record,
)


def make_paths(tmp_path: Path) -> ManagedInstallPaths:
    root = tmp_path / "managed"
    return ManagedInstallPaths(
        managed_root=root,
        application_link=root / "app",
        versions_root=root / "versions",
        installation_record=root / "installation.json",
        lock=root / "update.lock",
        bin_directory=tmp_path / "bin",
        state_root=tmp_path / "state",
        configuration_path=tmp_path / "config/config.json",
    )


def mark_managed_root(paths: ManagedInstallPaths) -> None:
    paths.managed_root.mkdir(parents=True, exist_ok=True)
    marker = ManagedRootMarker(
        managed_root=str(paths.managed_root),
        application_link=str(paths.application_link),
        versions_root=str(paths.versions_root),
        installation_record=str(paths.installation_record),
        bin_directory=str(paths.bin_directory),
    )
    (paths.managed_root / MANAGED_ROOT_MARKER_NAME).write_text(
        marker.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )


def record(
    tmp_path: Path,
    *,
    channel: ManagedChannel = ManagedChannel.STABLE,
    version: str = "0.1.0",
    revision: str = "a" * 40,
):
    paths = make_paths(tmp_path)
    return make_installation_record(
        channel=channel,
        release_version=version,
        source_revision=revision,
        source_ref=f"v{version}" if channel is ManagedChannel.STABLE else "main",
        repository_url="https://example.invalid/software-agent-team.git",
        application_path=paths.application_link,
        artifact_digest="sha256:" + "c" * 64,
        installed_at=datetime(2026, 9, 4, tzinfo=UTC),
    )


def target(
    *,
    channel: ManagedChannel = ManagedChannel.STABLE,
    version: str | None = "0.2.0",
    revision: str = "b" * 40,
    ref: str | None = None,
) -> ManagedTarget:
    source_ref = ref or (f"v{version}" if channel is ManagedChannel.STABLE else "main")
    return ManagedTarget(
        channel=channel,
        release_version=version,
        source_revision=revision,
        source_ref=source_ref,
        repository_url="https://example.invalid/software-agent-team.git",
        artifact_digest="sha256:" + "d" * 64,
        schema_support=(
            supported_schemas() if channel is ManagedChannel.STABLE else None
        ),
    )


def test_plan_distinguishes_stable_update_current_and_rebinding(
    tmp_path: Path,
) -> None:
    installed = record(tmp_path)

    available = plan_managed_change(installed, target())
    current = plan_managed_change(
        installed,
        target(
            version="0.1.0",
            revision=installed.source_revision,
            ref="v0.1.0",
        ).model_copy(update={"artifact_digest": installed.artifact_digest}),
    )
    rebound = plan_managed_change(
        installed,
        target(version="0.1.0", ref="v0.1.0"),
    )

    assert available.status is ManagedChangeStatus.UPDATE_AVAILABLE
    assert available.requires_activation
    assert current.status is ManagedChangeStatus.CURRENT
    assert not current.requires_activation
    assert rebound.status is ManagedChangeStatus.INCONSISTENT


def test_plan_treats_channel_change_as_explicit_switch(tmp_path: Path) -> None:
    installed = record(tmp_path)
    dev = target(
        channel=ManagedChannel.DEV,
        version=None,
        ref="main",
    )

    plan = plan_managed_change(installed, dev)

    assert plan.status is ManagedChangeStatus.CHANNEL_SWITCH
    assert plan.current_channel is ManagedChannel.STABLE
    assert plan.target_channel is ManagedChannel.DEV
    assert plan.requires_activation


def test_dev_plan_compares_revision_not_release_number(tmp_path: Path) -> None:
    installed = record(tmp_path, channel=ManagedChannel.DEV)

    current = plan_managed_change(
        installed,
        target(
            channel=ManagedChannel.DEV,
            version=None,
            revision=installed.source_revision,
            ref="main",
        ),
    )
    changed = plan_managed_change(
        installed,
        target(
            channel=ManagedChannel.DEV,
            version=None,
            ref="main",
        ),
    )

    assert current.status is ManagedChangeStatus.CURRENT
    assert changed.status is ManagedChangeStatus.UPDATE_AVAILABLE


def test_validate_current_install_binds_process_link_marker_and_record(
    tmp_path: Path,
) -> None:
    paths = make_paths(tmp_path)
    mark_managed_root(paths)
    active = paths.versions_root / "0.1.0-gaaaaaaaaaaaa"
    active.mkdir(parents=True)
    paths.application_link.symlink_to(active)
    installed = record(tmp_path)
    save_installation_record(installed, paths.installation_record)
    marker = ManagedApplicationMarker(
        application_link=str(paths.application_link),
        channel=installed.channel,
        release_version=installed.release_version,
        source_revision=installed.source_revision,
        source_ref=installed.source_ref,
        repository_url=installed.repository_url,
        artifact_digest=installed.artifact_digest,
    )
    (active / ".sat-managed-install").write_text(
        marker.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )

    actual_record, actual_marker = validate_current_managed_install(
        project_root=active,
        paths=paths,
    )

    assert actual_record == installed
    assert actual_marker == marker


def test_validate_current_install_refuses_source_or_conflicting_identity(
    tmp_path: Path,
) -> None:
    paths = make_paths(tmp_path)
    mark_managed_root(paths)
    source = tmp_path / "source"
    source.mkdir()

    with pytest.raises(ManagedInstallError, match="no managed identity"):
        validate_current_managed_install(project_root=source, paths=paths)

    active = paths.versions_root / "release"
    active.mkdir(parents=True)
    paths.application_link.symlink_to(active)
    installed = record(tmp_path)
    save_installation_record(installed, paths.installation_record)
    marker = ManagedApplicationMarker(
        application_link=str(paths.application_link),
        channel=installed.channel,
        release_version=installed.release_version,
        source_revision="b" * 40,
        source_ref=installed.source_ref,
        repository_url=installed.repository_url,
        artifact_digest=installed.artifact_digest,
    )
    (active / ".sat-managed-install").write_text(
        marker.model_dump_json(),
        encoding="utf-8",
    )

    with pytest.raises(ManagedInstallError, match="marker conflicts"):
        validate_current_managed_install(project_root=active, paths=paths)


def test_validate_current_install_rejects_link_escape_and_git_drift(
    tmp_path: Path,
) -> None:
    paths = make_paths(tmp_path)
    mark_managed_root(paths)
    outside = tmp_path / "outside"
    outside.mkdir()
    paths.application_link.parent.mkdir(parents=True, exist_ok=True)
    paths.application_link.symlink_to(outside)
    installed = record(tmp_path)
    save_installation_record(installed, paths.installation_record)
    marker = ManagedApplicationMarker(
        application_link=str(paths.application_link),
        channel=installed.channel,
        release_version=installed.release_version,
        source_revision=installed.source_revision,
        source_ref=installed.source_ref,
        repository_url=installed.repository_url,
        artifact_digest=installed.artifact_digest,
    )
    (outside / ".sat-managed-install").write_text(
        marker.model_dump_json(), encoding="utf-8"
    )

    with pytest.raises(ManagedInstallError, match="escapes the versions root"):
        validate_current_managed_install(project_root=outside, paths=paths)

    paths.application_link.unlink()
    active = paths.versions_root / "release"
    active.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main", active], check=True)
    subprocess.run(["git", "-C", active, "config", "user.name", "urntt"], check=True)
    subprocess.run(
        ["git", "-C", active, "config", "user.email", "urntts@gmail.com"],
        check=True,
    )
    (active / "source.txt").write_text("tracked\n", encoding="utf-8")
    subprocess.run(["git", "-C", active, "add", "."], check=True)
    subprocess.run(
        ["git", "-C", active, "commit", "-m", "test: create active source"],
        check=True,
        capture_output=True,
    )
    revision = subprocess.run(
        ["git", "-C", active, "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    installed = installed.model_copy(update={"source_revision": revision})
    save_installation_record(installed, paths.installation_record)
    marker = marker.model_copy(update={"source_revision": revision})
    (active / ".sat-managed-install").write_text(
        marker.model_dump_json(), encoding="utf-8"
    )
    (active / ".gitignore").write_text(".sat-managed-install\n", encoding="utf-8")
    subprocess.run(["git", "-C", active, "add", ".gitignore"], check=True)
    subprocess.run(
        ["git", "-C", active, "commit", "-m", "test: ignore marker"],
        check=True,
        capture_output=True,
    )
    revision = subprocess.run(
        ["git", "-C", active, "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    from software_agent_team.releases import git_archive_digest

    installed = installed.model_copy(
        update={
            "source_revision": revision,
            "artifact_digest": git_archive_digest(active),
        }
    )
    save_installation_record(installed, paths.installation_record)
    marker = marker.model_copy(
        update={
            "source_revision": revision,
            "artifact_digest": installed.artifact_digest,
        }
    )
    (active / ".sat-managed-install").write_text(
        marker.model_dump_json(), encoding="utf-8"
    )
    paths.application_link.symlink_to(active)
    (active / "unexpected.txt").write_text("drift\n", encoding="utf-8")

    with pytest.raises(ManagedInstallError, match="source drift"):
        validate_current_managed_install(project_root=active, paths=paths)
