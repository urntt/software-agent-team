"""Behavior tests for the remote managed-install bootstrap."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).parents[1]


def write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def git(repository: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )


def prepare_source(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    (source / "scripts").mkdir(parents=True)
    write_executable(
        source / "scripts/install.sh",
        """#!/usr/bin/env bash
set -euo pipefail
printf 'root=%s managed=%s\n' "$(pwd)" "${SAT_MANAGED_INSTALL:-0}" >> "${INSTALL_LOG:?}"
""",
    )
    (source / ".gitignore").write_text(".sat-managed-install\n", encoding="utf-8")
    git(source, "init", "-b", "main")
    git(source, "config", "user.name", "urntt")
    git(source, "config", "user.email", "urntts@gmail.com")
    git(source, "add", ".")
    git(source, "commit", "-m", "chore: initialize bootstrap fixture")
    return source


def fake_environment(tmp_path: Path, source: Path) -> tuple[dict[str, str], Path, Path]:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    write_executable(
        fake_bin / "id",
        """#!/usr/bin/env bash
case "${1:-}" in
  -u|-g) echo 1000 ;;
  *) exit 2 ;;
esac
""",
    )
    write_executable(fake_bin / "uname", "#!/usr/bin/env bash\necho Linux\n")
    write_executable(fake_bin / "curl", "#!/usr/bin/env bash\nexit 0\n")
    home = tmp_path / "home"
    home.mkdir()
    install_root = home / ".local/share/software-agent-team/app"
    install_log = tmp_path / "install.log"
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "HOME": str(home),
        "SAT_REPOSITORY_URL": str(source),
        "SAT_INSTALL_REF": "main",
        "SAT_INSTALL_ROOT": str(install_root),
        "INSTALL_LOG": str(install_log),
    }
    return environment, install_root, install_log


def run_bootstrap(environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(REPOSITORY_ROOT / "scripts/bootstrap.sh")],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.skipif(sys.platform != "linux", reason="bootstrap supports Linux/WSL")
def test_bootstrap_creates_and_reuses_one_owned_managed_install(
    tmp_path: Path,
) -> None:
    source = prepare_source(tmp_path)
    environment, install_root, install_log = fake_environment(tmp_path, source)

    first = run_bootstrap(environment)
    second = run_bootstrap(environment)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert (install_root / ".git").is_dir()
    marker = (install_root / ".sat-managed-install").read_text(encoding="utf-8")
    assert marker == f"software-agent-team-managed-v1\nroot={install_root}\n"
    calls = install_log.read_text(encoding="utf-8").splitlines()
    assert calls == [
        f"root={install_root} managed=1",
        f"root={install_root} managed=1",
    ]
    assert "bootstrap: next=sat" in first.stdout
    assert first.stdout.splitlines()[-2:] == [
        "bootstrap: uninstall=sat-uninstall",
        "bootstrap: next=sat",
    ]


@pytest.mark.skipif(sys.platform != "linux", reason="bootstrap supports Linux/WSL")
def test_bootstrap_refuses_an_unowned_existing_destination(tmp_path: Path) -> None:
    source = prepare_source(tmp_path)
    environment, install_root, install_log = fake_environment(tmp_path, source)
    install_root.mkdir(parents=True)
    (install_root / "user-file.txt").write_text("keep\n", encoding="utf-8")

    completed = run_bootstrap(environment)

    assert completed.returncode == 1
    assert "not owned by SAT" in completed.stderr
    assert (install_root / "user-file.txt").is_file()
    assert not install_log.exists()


@pytest.mark.skipif(sys.platform != "linux", reason="bootstrap supports Linux/WSL")
def test_bootstrap_refuses_unexpected_changes_in_a_managed_install(
    tmp_path: Path,
) -> None:
    source = prepare_source(tmp_path)
    environment, install_root, _ = fake_environment(tmp_path, source)
    first = run_bootstrap(environment)
    assert first.returncode == 0, first.stderr
    unexpected = install_root / "unexpected.txt"
    unexpected.write_text("keep\n", encoding="utf-8")

    second = run_bootstrap(environment)

    assert second.returncode == 1
    assert "unexpected file changes" in second.stderr
    assert unexpected.read_text(encoding="utf-8") == "keep\n"
