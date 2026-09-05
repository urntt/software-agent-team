"""Behavior tests for the minimal remote managed-install bootstrap."""

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


def prepare_helper_repository(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    (source / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    git(source, "init", "-b", "main")
    git(source, "config", "user.name", "urntt")
    git(source, "config", "user.email", "urntts@gmail.com")
    git(source, "add", ".")
    git(source, "commit", "-m", "test: initialize bootstrap helper")
    return source


def fake_environment(tmp_path: Path, source: Path) -> tuple[dict[str, str], Path]:
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
    write_executable(
        fake_bin / "uv",
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "${FAKE_UV_LOG:?}"
if [[ "${1:-}" == "run" ]]; then
  printf 'root=%s args=%s\n' "${SAT_INSTALL_ROOT-unset}" "$*" >> "${FAKE_INSTALL_LOG:?}"
  if [[ "${FAKE_INSTALL_FAIL_ONCE:-0}" == "1" && \
        ! -e "${FAKE_INSTALL_ATTEMPT_MARKER:?}" ]]; then
    : > "${FAKE_INSTALL_ATTEMPT_MARKER:?}"
    exit 17
  fi
fi
""",
    )
    home = tmp_path / "home"
    home.mkdir()
    install_root = home / ".local/share/software-agent-team/app"
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "HOME": str(home),
        "UV_BIN": str(fake_bin / "uv"),
        "SAT_REPOSITORY_URL": str(source),
        "SAT_INSTALL_ROOT": str(install_root),
        "FAKE_UV_LOG": str(tmp_path / "uv.log"),
        "FAKE_INSTALL_LOG": str(tmp_path / "install.log"),
        "FAKE_INSTALL_ATTEMPT_MARKER": str(tmp_path / "install-attempted"),
    }
    return environment, install_root


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
def test_bootstrap_defaults_to_stable_and_never_passes_a_moving_ref(
    tmp_path: Path,
) -> None:
    source = prepare_helper_repository(tmp_path)
    environment, install_root = fake_environment(tmp_path, source)

    first = run_bootstrap(environment)
    second = run_bootstrap(environment)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    calls = (tmp_path / "install.log").read_text(encoding="utf-8").splitlines()
    assert len(calls) == 2
    assert all("_managed-install --channel stable" in call for call in calls)
    assert all(" --ref " not in call for call in calls)
    assert all(f"root={install_root}" in call for call in calls)
    assert "bootstrap: channel=stable" in first.stdout
    assert first.stdout.splitlines()[-2:] == [
        "bootstrap: uninstall=sat-uninstall",
        "bootstrap: next=sat",
    ]
    assert not tuple(install_root.parent.glob(".sat-bootstrap.*"))


@pytest.mark.skipif(sys.platform != "linux", reason="bootstrap supports Linux/WSL")
def test_bootstrap_leaves_default_path_selection_to_the_managed_lifecycle(
    tmp_path: Path,
) -> None:
    source = prepare_helper_repository(tmp_path)
    environment, install_root = fake_environment(tmp_path, source)
    environment.pop("SAT_INSTALL_ROOT")

    completed = run_bootstrap(environment)

    assert completed.returncode == 0, completed.stderr
    call = (tmp_path / "install.log").read_text(encoding="utf-8")
    assert "root=unset" in call
    assert f"bootstrap: managed application={install_root}" in completed.stdout


@pytest.mark.skipif(sys.platform != "linux", reason="bootstrap supports Linux/WSL")
def test_bootstrap_passes_an_explicit_dev_ref_only_to_the_dev_resolver(
    tmp_path: Path,
) -> None:
    source = prepare_helper_repository(tmp_path)
    environment, _ = fake_environment(tmp_path, source)
    environment["SAT_INSTALL_CHANNEL"] = "dev"
    environment["SAT_INSTALL_REF"] = "candidate-17"

    completed = run_bootstrap(environment)

    assert completed.returncode == 0, completed.stderr
    call = (tmp_path / "install.log").read_text(encoding="utf-8")
    assert "_managed-install --channel dev" in call
    assert "--ref candidate-17" in call
    assert "bootstrap: channel=dev" in completed.stdout


@pytest.mark.skipif(sys.platform != "linux", reason="bootstrap supports Linux/WSL")
def test_bootstrap_propagates_failure_and_is_idempotently_retryable(
    tmp_path: Path,
) -> None:
    source = prepare_helper_repository(tmp_path)
    environment, _ = fake_environment(tmp_path, source)
    environment["FAKE_INSTALL_FAIL_ONCE"] = "1"

    first = run_bootstrap(environment)
    second = run_bootstrap(environment)

    assert first.returncode == 17
    assert second.returncode == 0, second.stderr
    assert len((tmp_path / "install.log").read_text(encoding="utf-8").splitlines()) == 2
    assert not tuple(
        (Path(environment["SAT_INSTALL_ROOT"]).parent).glob(".sat-bootstrap.*")
    )


@pytest.mark.skipif(sys.platform != "linux", reason="bootstrap supports Linux/WSL")
def test_bootstrap_recovery_uses_fresh_helper_not_installed_predecessor(
    tmp_path: Path,
) -> None:
    source = prepare_helper_repository(tmp_path)
    environment, _ = fake_environment(tmp_path, source)
    stale_bin = Path(environment["HOME"]) / ".local/bin"
    stale_bin.mkdir(parents=True)
    stale_call = tmp_path / "stale-sat-called"
    write_executable(
        stale_bin / "sat",
        f"#!/usr/bin/env bash\ntouch {stale_call}\nexit 91\n",
    )
    environment["PATH"] = f"{stale_bin}:{environment['PATH']}"

    completed = run_bootstrap(environment)

    assert completed.returncode == 0, completed.stderr
    assert not stale_call.exists()
    call = (tmp_path / "install.log").read_text(encoding="utf-8")
    assert "_managed-install --channel stable" in call


@pytest.mark.skipif(sys.platform != "linux", reason="bootstrap supports Linux/WSL")
@pytest.mark.parametrize(
    ("variable", "value", "message"),
    [
        ("SAT_INSTALL_CHANNEL", "preview", "stable or dev"),
        ("SAT_INSTALL_REF", "../escape", "SAT_INSTALL_REF"),
        ("SAT_RELEASE_API_URL", "http://example.invalid/latest", "HTTPS URL"),
    ],
)
def test_bootstrap_rejects_invalid_channel_ref_or_release_endpoint_before_helper(
    tmp_path: Path,
    variable: str,
    value: str,
    message: str,
) -> None:
    source = prepare_helper_repository(tmp_path)
    environment, _ = fake_environment(tmp_path, source)
    environment[variable] = value

    completed = run_bootstrap(environment)

    assert completed.returncode == 1
    assert message in completed.stderr
    assert not (tmp_path / "install.log").exists()
