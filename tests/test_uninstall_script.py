"""Behavior tests for safe one-command uninstallation."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

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
    assert configuration.is_file()
    state = Path(environment["SAT_STATE_ROOT"])
    assert (state / "runs/example/final-report.md").is_file()
    assert (state / "workspaces/example/result.py").is_file()
    assert (state / "sources/example/README.md").is_file()
    assert (checkout / "scripts/uninstall.sh").is_file()
    assert "preserved SAT configuration" in completed.stdout
    assert "preserved generated runs, workspaces, and sources" in completed.stdout
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
    assert not (checkout / ".venv").exists()
    assert not (install_bin / "sat").exists()
    assert (export / "configuration/config.json").is_file()
    assert (export / "data/runs/example/final-report.md").is_file()
    assert (export / "data/workspaces/example/result.py").is_file()
    assert (export / "data/sources/example/README.md").is_file()
    manifest = (export / "EXPORT.txt").read_text(encoding="utf-8")
    assert "configuration=yes" in manifest
    assert "runs=yes" in manifest
    assert "workspaces=yes" in manifest
    assert "sources=yes" in manifest
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
    assert "symbolic-link generated-data directories" in completed.stderr
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
