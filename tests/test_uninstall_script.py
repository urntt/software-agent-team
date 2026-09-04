"""Behavior tests for safe one-command uninstallation."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from software_agent_team.managed_install import (
    MANAGED_ROOT_MARKER_NAME,
    ManagedApplicationMarker,
    ManagedRootMarker,
)
from software_agent_team.versioning import (
    ManagedChannel,
    make_installation_record,
    save_installation_record,
)

REPOSITORY_ROOT = Path(__file__).parents[1]


def write_executable(path: Path, content: str) -> None:
    """Create one test-owned executable."""

    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def prepare_installation(
    tmp_path: Path,
) -> tuple[Path, Path, Path, dict[str, str]]:
    """Create isolated installation-owned and user-owned test state."""

    checkout = tmp_path / "checkout"
    script = checkout / "scripts/uninstall.sh"
    script.parent.mkdir(parents=True)
    shutil.copy2(REPOSITORY_ROOT / "scripts/uninstall.sh", script)

    sat_target = checkout / ".venv/bin/sat"
    sat_target.parent.mkdir(parents=True)
    write_executable(sat_target, "#!/usr/bin/env bash\nexit 0\n")
    private_openclaw = checkout / ".sat/openclaw"
    (private_openclaw / "bin").mkdir(parents=True)
    write_executable(
        private_openclaw / "bin/openclaw",
        "#!/usr/bin/env bash\nexit 0\n",
    )
    (private_openclaw / ".sat-owned-runtime").write_text(
        f"software-agent-team-openclaw-runtime-v1\nroot={private_openclaw}\n",
        encoding="utf-8",
    )
    state = tmp_path / "state"
    (state / "runs/example").mkdir(parents=True)
    (state / "runs/example/final-report.md").write_text(
        "completed\n",
        encoding="utf-8",
    )
    (state / "workspaces/example").mkdir(parents=True)
    (state / "workspaces/example/result.py").write_text(
        "print('result')\n",
        encoding="utf-8",
    )
    (state / "sources/example").mkdir(parents=True)
    (state / "sources/example/README.md").write_text("seed\n", encoding="utf-8")
    (state / "planning/example").mkdir(parents=True)
    (state / "planning/example/session.json").write_text(
        "planning evidence\n",
        encoding="utf-8",
    )
    (state / "openclaw/credentials").mkdir(parents=True)
    (state / "openclaw/credentials/provider.json").write_text(
        "private SAT credential state\n",
        encoding="utf-8",
    )
    (state / ".sat-state-v1").write_text(
        f"software-agent-team-state-v1\nroot={state}\n",
        encoding="utf-8",
    )

    home = tmp_path / "home"
    install_bin = home / ".local/bin"
    install_bin.mkdir(parents=True)
    (install_bin / "sat").symlink_to(sat_target)
    (install_bin / "sat-uninstall").symlink_to(script)
    configuration = home / ".config/software-agent-team/config.json"
    configuration.parent.mkdir(parents=True)
    configuration.write_text('{"schema_version": 1}\n', encoding="utf-8")
    configuration.chmod(0o600)
    existing_openclaw = home / ".openclaw"
    existing_openclaw.mkdir(parents=True)
    (existing_openclaw / "openclaw.json").write_text(
        "existing user OpenClaw\n",
        encoding="utf-8",
    )

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    write_executable(
        fake_bin / "id",
        """#!/usr/bin/env bash
set -euo pipefail
case "${1:-}" in
  -u) echo 1000 ;;
  -g) echo 1000 ;;
  *) exit 2 ;;
esac
""",
    )
    write_executable(fake_bin / "uname", "#!/usr/bin/env bash\necho Linux\n")
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "HOME": str(home),
        "SAT_BIN_DIR": str(install_bin),
        "SAT_CONFIG_PATH": str(configuration),
        "SAT_STATE_ROOT": str(state),
    }
    return checkout, install_bin, configuration, environment


def prepare_managed_v2_installation(
    tmp_path: Path,
    *,
    custom_layout: bool = False,
) -> tuple[Path, Path, Path, Path, dict[str, str]]:
    """Create a versioned managed installation with bound lifecycle metadata."""

    checkout, old_bin, _configuration, environment = prepare_installation(tmp_path)
    home = Path(environment["HOME"])
    if custom_layout:
        application = tmp_path / "applications/sat"
        managed_root = application.parent / ".sat.sat-managed"
        install_bin = tmp_path / "commands"
        installation_record = tmp_path / "metadata/installation.json"
    else:
        managed_root = home / ".local/share/software-agent-team"
        application = managed_root / "app"
        install_bin = old_bin
        installation_record = managed_root / "installation.json"
    versions = managed_root / "versions"
    release = versions / "0.1.0-gaaaaaaaaaaaa"
    versions.mkdir(parents=True)
    shutil.move(checkout, release)
    private_openclaw = release / ".sat/openclaw"
    (private_openclaw / ".sat-owned-runtime").write_text(
        f"software-agent-team-openclaw-runtime-v1\nroot={private_openclaw}\n",
        encoding="utf-8",
    )
    python = release / ".venv/bin/python"
    python.symlink_to(sys.executable)
    marker = ManagedApplicationMarker(
        application_link=str(application),
        channel=ManagedChannel.DEV,
        release_version="0.1.0",
        source_revision="a" * 40,
        source_ref="main",
        repository_url="https://example.invalid/software-agent-team.git",
        artifact_digest="sha256:" + "b" * 64,
    )
    (release / ".sat-managed-install").write_text(
        marker.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    root_marker = ManagedRootMarker(
        managed_root=str(managed_root),
        application_link=str(application),
        versions_root=str(versions),
        installation_record=str(installation_record),
        bin_directory=str(install_bin),
    )
    (managed_root / MANAGED_ROOT_MARKER_NAME).write_text(
        root_marker.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    (managed_root / "update.lock").touch(mode=0o600)
    application.parent.mkdir(parents=True, exist_ok=True)
    application.symlink_to(os.path.relpath(release, start=application.parent))
    record = make_installation_record(
        channel=marker.channel,
        release_version=marker.release_version,
        source_revision=marker.source_revision,
        source_ref=marker.source_ref,
        repository_url=marker.repository_url,
        application_path=application,
        artifact_digest=marker.artifact_digest,
        installed_at=datetime(2026, 9, 4, tzinfo=UTC),
    )
    save_installation_record(record, installation_record)
    install_bin.mkdir(parents=True, exist_ok=True)
    for existing in (old_bin / "sat", old_bin / "sat-uninstall"):
        existing.unlink(missing_ok=True)
    (install_bin / "sat").symlink_to(application / ".venv/bin/sat")
    (install_bin / "sat-uninstall").symlink_to(application / "scripts/uninstall.sh")
    environment["SAT_BIN_DIR"] = str(install_bin)
    return release, managed_root, application, installation_record, environment


def run_uninstaller(
    checkout: Path,
    environment: dict[str, str],
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    """Run the installed symlink without an interactive terminal."""

    return subprocess.run(
        [str(Path(environment["SAT_BIN_DIR"]) / "sat-uninstall"), *arguments],
        cwd=checkout,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.skipif(sys.platform != "linux", reason="uninstaller supports Linux/WSL")
def test_uninstaller_preserves_configuration_and_generated_data_by_default(
    tmp_path: Path,
) -> None:
    checkout, install_bin, configuration, environment = prepare_installation(tmp_path)

    completed = run_uninstaller(checkout, environment, "--yes")

    assert completed.returncode == 0, completed.stderr
    assert not (install_bin / "sat").exists()
    assert not (install_bin / "sat-uninstall").exists()
    assert not (checkout / ".venv").exists()
    assert not (checkout / ".sat/openclaw").exists()
    assert configuration.is_file()
    state = Path(environment["SAT_STATE_ROOT"])
    assert (state / "runs/example/final-report.md").is_file()
    assert (state / "workspaces/example/result.py").is_file()
    assert (state / "sources/example/README.md").is_file()
    assert (state / "planning/example/session.json").is_file()
    assert (state / "openclaw/credentials/provider.json").is_file()
    assert (Path(environment["HOME"]) / ".openclaw/openclaw.json").read_text(
        encoding="utf-8"
    ) == "existing user OpenClaw\n"
    assert (checkout / "scripts/uninstall.sh").is_file()
    assert "preserved SAT configuration" in completed.stdout
    assert (
        "preserved runs, workspaces, sources, and Planning evidence" in completed.stdout
    )
    assert "development checkout preserved" in completed.stdout


@pytest.mark.skipif(sys.platform != "linux", reason="uninstaller supports Linux/WSL")
def test_uninstaller_exports_before_explicit_purge(tmp_path: Path) -> None:
    checkout, install_bin, configuration, environment = prepare_installation(tmp_path)
    export = tmp_path / "sat-export"

    completed = run_uninstaller(
        checkout,
        environment,
        "--export-to",
        str(export),
        "--purge-config",
        "--purge-data",
        "--yes",
    )

    assert completed.returncode == 0, completed.stderr
    assert not configuration.exists()
    state = Path(environment["SAT_STATE_ROOT"])
    assert not (state / "runs").exists()
    assert not (state / "workspaces").exists()
    assert not (state / "sources").exists()
    assert not (state / "planning").exists()
    assert (state / "openclaw/credentials/provider.json").is_file()
    assert not (checkout / ".venv").exists()
    assert not (install_bin / "sat").exists()
    assert (export / "configuration/config.json").is_file()
    assert (export / "data/runs/example/final-report.md").is_file()
    assert (export / "data/workspaces/example/result.py").is_file()
    assert (export / "data/sources/example/README.md").is_file()
    assert (export / "data/planning/example/session.json").is_file()
    manifest = (export / "EXPORT.txt").read_text(encoding="utf-8")
    assert "configuration=yes" in manifest
    assert "runs=yes" in manifest
    assert "workspaces=yes" in manifest
    assert "sources=yes" in manifest
    assert "planning=yes" in manifest
    assert "provider_credentials=excluded" in manifest
    assert "exported preserved state" in completed.stdout


@pytest.mark.skipif(sys.platform != "linux", reason="uninstaller supports Linux/WSL")
def test_uninstaller_requires_confirmation_without_a_terminal(tmp_path: Path) -> None:
    checkout, install_bin, configuration, environment = prepare_installation(tmp_path)

    completed = run_uninstaller(checkout, environment)

    assert completed.returncode == 1
    assert "interactive confirmation is unavailable; use --yes" in completed.stderr
    assert (install_bin / "sat").is_symlink()
    assert (checkout / ".venv").is_dir()
    assert configuration.is_file()


@pytest.mark.skipif(sys.platform != "linux", reason="uninstaller supports Linux/WSL")
def test_uninstaller_validates_every_purge_target_before_deleting(
    tmp_path: Path,
) -> None:
    checkout, install_bin, configuration, environment = prepare_installation(tmp_path)
    state = Path(environment["SAT_STATE_ROOT"])
    shutil.rmtree(state / "workspaces")
    (state / "workspaces").symlink_to(tmp_path / "outside-workspaces")

    completed = run_uninstaller(
        checkout,
        environment,
        "--purge-config",
        "--purge-data",
        "--yes",
    )

    assert completed.returncode == 1
    assert "symbolic-link SAT state directories" in completed.stderr
    assert configuration.is_file()
    assert (state / "runs/example/final-report.md").is_file()
    assert (checkout / ".venv").is_dir()
    assert (install_bin / "sat").is_symlink()


@pytest.mark.skipif(sys.platform != "linux", reason="uninstaller supports Linux/WSL")
def test_uninstaller_removes_only_a_marked_managed_application(
    tmp_path: Path,
) -> None:
    checkout, install_bin, configuration, environment = prepare_installation(tmp_path)
    (checkout / ".sat-managed-install").write_text(
        f"software-agent-team-managed-v1\nroot={checkout}\n",
        encoding="utf-8",
    )

    completed = run_uninstaller(checkout, environment, "--yes")

    assert completed.returncode == 0, completed.stderr
    assert not checkout.exists()
    assert not (install_bin / "sat").exists()
    assert configuration.is_file()
    assert Path(environment["SAT_STATE_ROOT"]).is_dir()
    assert "removed managed SAT application" in completed.stdout


@pytest.mark.skipif(sys.platform != "linux", reason="uninstaller supports Linux/WSL")
def test_uninstaller_refuses_to_purge_an_unowned_state_root(tmp_path: Path) -> None:
    checkout, install_bin, configuration, environment = prepare_installation(tmp_path)
    state = Path(environment["SAT_STATE_ROOT"])
    (state / ".sat-state-v1").unlink()

    completed = run_uninstaller(
        checkout,
        environment,
        "--purge-config",
        "--purge-data",
        "--yes",
    )

    assert completed.returncode == 1
    assert "missing its ownership marker" in completed.stderr
    assert configuration.is_file()
    assert (state / "runs/example/final-report.md").is_file()
    assert (checkout / ".venv").is_dir()
    assert (install_bin / "sat").is_symlink()


@pytest.mark.skipif(sys.platform != "linux", reason="uninstaller supports Linux/WSL")
def test_uninstaller_can_purge_only_sat_provider_state_without_touching_openclaw(
    tmp_path: Path,
) -> None:
    checkout, _, _, environment = prepare_installation(tmp_path)
    state = Path(environment["SAT_STATE_ROOT"])
    existing_config = Path(environment["HOME"]) / ".openclaw/openclaw.json"

    completed = run_uninstaller(
        checkout,
        environment,
        "--purge-provider-state",
        "--yes",
    )

    assert completed.returncode == 0, completed.stderr
    assert not (state / "openclaw").exists()
    assert (state / "runs/example/final-report.md").is_file()
    assert existing_config.read_text(encoding="utf-8") == "existing user OpenClaw\n"
    assert "other OpenClaw installations" in completed.stdout


@pytest.mark.skipif(sys.platform != "linux", reason="uninstaller supports Linux/WSL")
def test_uninstaller_removes_a_versioned_managed_lifecycle_but_preserves_state(
    tmp_path: Path,
) -> None:
    release, managed_root, application, record, environment = (
        prepare_managed_v2_installation(tmp_path)
    )
    old_release = managed_root / "versions/0.0.9-gbbbbbbbbbbbb"
    old_release.mkdir()
    (old_release / "retained.txt").write_text("old release\n", encoding="utf-8")
    configuration = Path(environment["SAT_CONFIG_PATH"])
    state = Path(environment["SAT_STATE_ROOT"])

    completed = run_uninstaller(release, environment, "--yes")

    assert completed.returncode == 0, completed.stderr
    assert not managed_root.exists()
    assert not application.exists()
    assert not application.is_symlink()
    assert not record.exists()
    assert not (Path(environment["SAT_BIN_DIR"]) / "sat").exists()
    assert not (Path(environment["SAT_BIN_DIR"]) / "sat-uninstall").exists()
    assert configuration.is_file()
    assert (state / "runs/example/final-report.md").is_file()
    assert (Path(environment["HOME"]) / ".openclaw/openclaw.json").is_file()
    assert f"removed managed SAT application {application}" in completed.stdout


@pytest.mark.skipif(sys.platform != "linux", reason="uninstaller supports Linux/WSL")
def test_versioned_uninstall_uses_owned_sidecar_and_preserves_siblings(
    tmp_path: Path,
) -> None:
    release, managed_root, application, record, environment = (
        prepare_managed_v2_installation(tmp_path, custom_layout=True)
    )
    sibling = application.parent / "versions"
    sibling.mkdir()
    (sibling / "user.txt").write_text("preserve\n", encoding="utf-8")

    completed = run_uninstaller(release, environment, "--yes")

    assert completed.returncode == 0, completed.stderr
    assert not managed_root.exists()
    assert not application.exists()
    assert not record.exists()
    assert (sibling / "user.txt").read_text(encoding="utf-8") == "preserve\n"


@pytest.mark.skipif(sys.platform != "linux", reason="uninstaller supports Linux/WSL")
def test_versioned_uninstall_refuses_conflicting_metadata_before_purge(
    tmp_path: Path,
) -> None:
    release, managed_root, application, record, environment = (
        prepare_managed_v2_installation(tmp_path)
    )
    marker_path = managed_root / MANAGED_ROOT_MARKER_NAME
    payload = json.loads(marker_path.read_text(encoding="utf-8"))
    payload["application_link"] = str(tmp_path / "different-application")
    marker_path.write_text(json.dumps(payload), encoding="utf-8")
    configuration = Path(environment["SAT_CONFIG_PATH"])

    completed = run_uninstaller(
        release,
        environment,
        "--purge-config",
        "--purge-data",
        "--yes",
    )

    assert completed.returncode == 1
    assert "managed installation metadata cannot be verified" in completed.stderr
    assert managed_root.is_dir()
    assert application.is_symlink()
    assert record.is_file()
    assert configuration.is_file()
    assert (Path(environment["SAT_STATE_ROOT"]) / "runs/example").is_dir()


@pytest.mark.skipif(sys.platform != "linux", reason="uninstaller supports Linux/WSL")
def test_versioned_uninstall_refuses_an_active_run_without_changing_files(
    tmp_path: Path,
) -> None:
    release, managed_root, application, record, environment = (
        prepare_managed_v2_installation(tmp_path)
    )
    active = Path(environment["SAT_STATE_ROOT"]) / "runs/active/run.json"
    active.parent.mkdir()
    active.write_text('{"schema_version":6,"phase":"implementing"}\n', encoding="utf-8")

    completed = run_uninstaller(release, environment, "--yes")

    assert completed.returncode == 1
    assert "active SAT run blocks uninstall: active" in completed.stderr
    assert managed_root.is_dir()
    assert application.is_symlink()
    assert record.is_file()
    assert (Path(environment["SAT_BIN_DIR"]) / "sat").is_symlink()


@pytest.mark.skipif(sys.platform != "linux", reason="uninstaller supports Linux/WSL")
def test_versioned_uninstall_refuses_during_an_install_or_update(
    tmp_path: Path,
) -> None:
    release, managed_root, application, record, environment = (
        prepare_managed_v2_installation(tmp_path)
    )
    descriptor = os.open(managed_root / "update.lock", os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        completed = run_uninstaller(release, environment, "--yes")
    finally:
        os.close(descriptor)

    assert completed.returncode == 1
    assert "another managed install or update is active" in completed.stderr
    assert managed_root.is_dir()
    assert application.is_symlink()
    assert record.is_file()
