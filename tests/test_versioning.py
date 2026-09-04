"""Tests for SAT release and installation identity."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from software_agent_team.versioning import (
    IdentityStatus,
    InstallMode,
    ManagedChannel,
    VersionIdentityError,
    compare_release_versions,
    highest_release_version,
    inspect_software_version,
    load_installation_record,
    make_installation_record,
    parse_release_version,
    render_short_version,
    save_installation_record,
)


def test_release_versions_use_strict_stable_semver_ordering() -> None:
    assert parse_release_version("0.1.0") == (0, 1, 0)
    assert compare_release_versions("0.1.9", "0.2.0") == -1
    assert compare_release_versions("1.0.0", "1.0.0") == 0
    assert highest_release_version(("0.1.9", "0.10.0", "0.2.0")) == "0.10.0"

    for value in ("v0.1.0", "0.1", "01.2.3", "0.1.0-rc.1", "0.1.0+dev"):
        with pytest.raises(VersionIdentityError):
            parse_release_version(value)


def test_installation_record_requires_stable_tag_and_full_identity(
    tmp_path: Path,
) -> None:
    record = make_installation_record(
        channel=ManagedChannel.STABLE,
        release_version="0.1.0",
        source_revision="a" * 40,
        source_ref="v0.1.0",
        repository_url="https://example.invalid/repository.git",
        application_path=tmp_path / "app",
        artifact_digest="sha256:" + "b" * 64,
        installed_at=datetime(2026, 9, 4, tzinfo=UTC),
    )

    assert record.source_ref == "v0.1.0"
    with pytest.raises(ValidationError, match=r"stable source_ref must be v0\.1\.0"):
        make_installation_record(
            channel=ManagedChannel.STABLE,
            release_version="0.1.0",
            source_revision="a" * 40,
            source_ref="main",
            repository_url="https://example.invalid/repository.git",
            application_path=tmp_path / "app",
            artifact_digest=None,
        )


def test_managed_identity_round_trips_without_git_checkout(tmp_path: Path) -> None:
    application = tmp_path / "installed-package"
    application.mkdir()
    path = tmp_path / "state" / "installation.json"
    record = make_installation_record(
        channel=ManagedChannel.STABLE,
        release_version="0.1.0",
        source_revision="a" * 40,
        source_ref="v0.1.0",
        repository_url="https://example.invalid/repository.git",
        application_path=application,
        artifact_digest="sha256:" + "b" * 64,
        installed_at=datetime(2026, 9, 4, tzinfo=UTC),
    )
    save_installation_record(record, path)

    assert load_installation_record(path) == record
    assert path.stat().st_mode & 0o777 == 0o600
    report = inspect_software_version(
        project_root=application,
        environment={"SAT_INSTALL_METADATA_PATH": str(path)},
        installed_version="0.1.0",
    )

    assert report.install_mode is InstallMode.MANAGED
    assert report.channel is ManagedChannel.STABLE
    assert report.identity_status is IdentityStatus.VERIFIED
    assert report.provenance_source == "installation_record"
    assert report.display_version == "0.1.0+g" + "a" * 12
    assert render_short_version(report) == "sat 0.1.0+g" + "a" * 12 + " [stable]"


def test_managed_identity_fails_closed_on_record_drift(tmp_path: Path) -> None:
    application = tmp_path / "app"
    application.mkdir()
    path = tmp_path / "installation.json"
    save_installation_record(
        make_installation_record(
            channel=ManagedChannel.DEV,
            release_version="0.1.0",
            source_revision="a" * 40,
            source_ref="main",
            repository_url="https://example.invalid/repository.git",
            application_path=application,
            artifact_digest=None,
        ),
        path,
    )

    report = inspect_software_version(
        project_root=application,
        environment={"SAT_INSTALL_METADATA_PATH": str(path)},
        installed_version="0.2.0",
    )

    assert report.identity_status is IdentityStatus.INCONSISTENT
    assert report.problems == (
        "installation record release version does not match package metadata",
    )


def test_source_identity_reports_exact_revision_and_dirty_state(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "source"
    repository.mkdir()
    subprocess.run(["git", "init", "-b", "main", repository], check=True)
    subprocess.run(
        ["git", "-C", repository, "config", "user.name", "urntt"], check=True
    )
    subprocess.run(
        ["git", "-C", repository, "config", "user.email", "urntts@gmail.com"],
        check=True,
    )
    (repository / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    subprocess.run(["git", "-C", repository, "add", "."], check=True)
    subprocess.run(
        ["git", "-C", repository, "commit", "-m", "test: initialize source"],
        check=True,
        capture_output=True,
    )
    revision = subprocess.run(
        ["git", "-C", repository, "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    clean = inspect_software_version(
        project_root=repository,
        environment={"SAT_INSTALL_METADATA_PATH": str(tmp_path / "missing.json")},
        installed_version="0.1.0",
    )
    (repository / "untracked.txt").write_text("dirty\n", encoding="utf-8")
    dirty = inspect_software_version(
        project_root=repository,
        environment={"SAT_INSTALL_METADATA_PATH": str(tmp_path / "missing.json")},
        installed_version="0.1.0",
    )

    assert clean.source_revision == revision
    assert clean.identity_status is IdentityStatus.VERIFIED
    assert clean.dirty is False
    assert dirty.source_revision == revision
    assert dirty.identity_status is IdentityStatus.PARTIAL
    assert dirty.dirty is True
    assert dirty.display_version == clean.display_version
    assert render_short_version(dirty).endswith(" (dirty)")


def test_unrelated_managed_record_does_not_claim_a_source_checkout(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-b", "main", source], check=True)
    subprocess.run(["git", "-C", source, "config", "user.name", "urntt"], check=True)
    subprocess.run(
        ["git", "-C", source, "config", "user.email", "urntts@gmail.com"],
        check=True,
    )
    (source / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    subprocess.run(["git", "-C", source, "add", "."], check=True)
    subprocess.run(
        ["git", "-C", source, "commit", "-m", "test: initialize source"],
        check=True,
        capture_output=True,
    )
    record_path = tmp_path / "installation.json"
    save_installation_record(
        make_installation_record(
            channel=ManagedChannel.STABLE,
            release_version="0.1.0",
            source_revision="a" * 40,
            source_ref="v0.1.0",
            repository_url="https://example.invalid/repository.git",
            application_path=tmp_path / "different-application",
            artifact_digest=None,
        ),
        record_path,
    )

    report = inspect_software_version(
        project_root=source,
        environment={"SAT_INSTALL_METADATA_PATH": str(record_path)},
        installed_version="0.1.0",
    )

    assert report.install_mode is InstallMode.SOURCE
    assert report.provenance_source == "git"


def test_untracked_package_reports_partial_identity(tmp_path: Path) -> None:
    package = tmp_path / "package"
    package.mkdir()

    report = inspect_software_version(
        project_root=package,
        environment={"SAT_INSTALL_METADATA_PATH": str(tmp_path / "missing.json")},
        installed_version="0.1.0",
    )

    assert report.install_mode is InstallMode.PACKAGE
    assert report.source_revision is None
    assert report.identity_status is IdentityStatus.PARTIAL
    assert report.problems == ("source provenance is unavailable",)


def test_installation_record_refuses_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "installation.json"
    link.symlink_to(target)

    with pytest.raises(VersionIdentityError, match="must not be a symbolic link"):
        load_installation_record(link)
