"""Behavior tests for the one-command Linux/WSL installer."""

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


def prepare_checkout(tmp_path: Path) -> Path:
    """Copy the installer-owned files into a minimal clean checkout."""

    checkout = tmp_path / "checkout"
    for relative in (
        "scripts/install.sh",
        "scripts/openclaw-environment.sh",
        "scripts/uninstall.sh",
        "scripts/setup.sh",
        "configs/run-policy.json",
        "configs/product-policy.json",
        "profiles/python/quality.json",
        "profiles/python/contract-template.json",
        "profiles/python/validation/run.py",
        "profiles/python/seed/pyproject.toml",
        "runtime/python/Dockerfile",
        "runtime/python/requirements.lock",
        "pyproject.toml",
        "uv.lock",
    ):
        source = REPOSITORY_ROOT / relative
        destination = checkout / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    return checkout


def fake_environment(
    tmp_path: Path,
    checkout: Path,
) -> tuple[dict[str, str], Path, Path, Path]:
    """Return isolated command stubs and installer destinations."""

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    uv_log = tmp_path / "uv.log"
    docker_log = tmp_path / "docker.log"
    home = tmp_path / "home"
    existing_openclaw = home / ".openclaw"
    openclaw_prefix = checkout / ".sat/openclaw"
    install_bin = home / ".local/bin"
    (existing_openclaw / "bin").mkdir(parents=True)
    (existing_openclaw / "openclaw.json").write_text(
        '{"existing":"must stay unchanged"}\n',
        encoding="utf-8",
    )
    write_executable(
        existing_openclaw / "bin/openclaw",
        """#!/usr/bin/env bash
printf 'executed\\n' >> "${FAKE_EXISTING_OPENCLAW_LOG:?}"
echo 'OpenClaw existing-user-version'
""",
    )
    (openclaw_prefix / "bin").mkdir(parents=True)
    (openclaw_prefix / "tools/node-v24.15.0/bin").mkdir(parents=True)
    (openclaw_prefix / ".sat-owned-runtime").write_text(
        f"software-agent-team-openclaw-runtime-v1\nroot={openclaw_prefix}\n",
        encoding="utf-8",
    )

    write_executable(
        fake_bin / "id",
        """#!/usr/bin/env bash
set -euo pipefail
case "${1:-}" in
  -u) echo "${FAKE_ID_UID:-1000}" ;;
  -g) echo "${FAKE_ID_GID:-1000}" ;;
  *) exit 2 ;;
esac
""",
    )
    write_executable(
        fake_bin / "uname",
        """#!/usr/bin/env bash
case "${1:-}" in
  -m) echo x86_64 ;;
  *) echo Linux ;;
esac
""",
    )
    for command in ("curl", "git"):
        write_executable(
            fake_bin / command,
            """#!/usr/bin/env bash
exit 0
""",
        )
    write_executable(
        fake_bin / "docker",
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "${FAKE_DOCKER_LOG:?}"
case "${1:-}" in
  info)
    [[ "${FAKE_DOCKER_INFO_FAIL:-0}" != "1" ]] || exit 1
    if [[ "${2:-}" == "--format" ]]; then
      echo linux
    fi
    ;;
  build)
    ;;
  image)
    [[ "${2:-}" == "inspect" ]]
    printf 'sha256:'
    printf 'a%.0s' {1..64}
    printf '\n'
    ;;
  *)
    exit 2
    ;;
esac
""",
    )
    write_executable(
        fake_bin / "uv",
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "${FAKE_UV_LOG:?}"
if [[ "$*" == "sync --locked" ]]; then
  mkdir -p .venv/bin
  printf '%s\n' '#!/usr/bin/env bash' 'exit 0' > .venv/bin/sat
  chmod 755 .venv/bin/sat
elif [[ "${1:-}" == "run" && "${2:-}" == "--frozen" && \
        "${3:-}" == "python" && "${4:-}" == "-c" ]]; then
  echo sat-python-quality:phase1-v1
elif [[ "${1:-}" == "run" && "${2:-}" == "--frozen" && \
        "${3:-}" == "python" && "${4:-}" == "-" ]]; then
  cat >/dev/null
fi
""",
    )
    write_executable(
        openclaw_prefix / "bin/openclaw",
        """#!/usr/bin/env bash
echo 'OpenClaw 2026.7.1-2 (test)'
""",
    )
    write_executable(
        openclaw_prefix / "tools/node-v24.15.0/bin/node",
        """#!/usr/bin/env bash
echo 'v24.15.0'
""",
    )

    environment = {
        **os.environ,
        "PATH": f"{fake_bin}:{install_bin}:/usr/bin:/bin",
        "HOME": str(home),
        "UV_BIN": str(fake_bin / "uv"),
        "OPENCLAW_PREFIX": str(existing_openclaw),
        "SAT_BIN_DIR": str(install_bin),
        "FAKE_EXISTING_OPENCLAW_LOG": str(tmp_path / "existing-openclaw.log"),
        "FAKE_UV_LOG": str(uv_log),
        "FAKE_DOCKER_LOG": str(docker_log),
    }
    return environment, install_bin, uv_log, docker_log


def run_installer(
    checkout: Path,
    environment: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    """Run the copied installer without a shell wrapper."""

    return subprocess.run(
        [str(checkout / "scripts/install.sh")],
        cwd=checkout,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.skipif(sys.platform != "linux", reason="installer supports Linux/WSL")
def test_installer_prepares_cli_image_and_checks_idempotently(tmp_path: Path) -> None:
    checkout = prepare_checkout(tmp_path)
    environment, install_bin, uv_log, docker_log = fake_environment(tmp_path, checkout)

    first = run_installer(checkout, environment)
    second = run_installer(checkout, environment)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    launcher = install_bin / "sat"
    uninstaller = install_bin / "sat-uninstall"
    assert launcher.is_symlink()
    assert launcher.readlink() == checkout / ".venv/bin/sat"
    assert uninstaller.is_symlink()
    assert uninstaller.readlink() == checkout / "scripts/uninstall.sh"
    assert (checkout / "openclaw/workspaces").is_dir()
    assert "install: Software Agent Team is ready" in first.stdout
    assert "image_id=sha256:" + "a" * 64 in first.stdout
    credential_notice = (
        "existing OpenClaw installations and configuration were not read"
    )
    assert credential_notice in first.stdout
    assert f"openclaw={checkout / '.sat/openclaw/bin/openclaw'}" in first.stdout
    existing_config = Path(environment["HOME"]) / ".openclaw/openclaw.json"
    assert existing_config.read_text(encoding="utf-8") == (
        '{"existing":"must stay unchanged"}\n'
    )
    assert not (tmp_path / "existing-openclaw.log").exists()
    assert "install: next=sat" in first.stdout
    assert "install: uninstall=sat-uninstall" in first.stdout
    docker_calls = docker_log.read_text(encoding="utf-8")
    assert "info" in docker_calls
    assert "build --pull=false --tag sat-python-quality:phase1-v1 runtime/python" in (
        docker_calls
    )
    assert "image inspect --format {{.Id}}" in docker_calls
    uv_calls = uv_log.read_text(encoding="utf-8")
    assert "python install 3.12" in uv_calls
    assert "sync --locked" in uv_calls
    assert "run --frozen pytest" in uv_calls


@pytest.mark.skipif(sys.platform != "linux", reason="installer supports Linux/WSL")
def test_managed_installer_leaves_the_next_action_to_the_bootstrap(
    tmp_path: Path,
) -> None:
    checkout = prepare_checkout(tmp_path)
    (checkout / ".sat-managed-install").write_text(
        "software-agent-team-managed-v1\n",
        encoding="utf-8",
    )
    environment, _, _, _ = fake_environment(tmp_path, checkout)
    environment["SAT_MANAGED_INSTALL"] = "1"

    completed = run_installer(checkout, environment)

    assert completed.returncode == 0, completed.stderr
    assert "install: next=" not in completed.stdout
    assert "install: uninstall=" not in completed.stdout


@pytest.mark.skipif(sys.platform != "linux", reason="installer supports Linux/WSL")
def test_installer_rejects_root_before_mutating_the_checkout(tmp_path: Path) -> None:
    checkout = prepare_checkout(tmp_path)
    environment, install_bin, _, docker_log = fake_environment(tmp_path, checkout)
    environment["FAKE_ID_UID"] = "0"

    completed = run_installer(checkout, environment)

    assert completed.returncode == 1
    assert "run the installer as an unprivileged user" in completed.stderr
    assert not (install_bin / "sat").exists()
    assert not docker_log.exists()


@pytest.mark.skipif(sys.platform != "linux", reason="installer supports Linux/WSL")
def test_installer_stops_when_the_docker_daemon_is_unavailable(tmp_path: Path) -> None:
    checkout = prepare_checkout(tmp_path)
    environment, install_bin, uv_log, _ = fake_environment(tmp_path, checkout)
    environment["FAKE_DOCKER_INFO_FAIL"] = "1"

    completed = run_installer(checkout, environment)

    assert completed.returncode == 1
    assert "Docker daemon is unavailable to this user" in completed.stderr
    assert not (install_bin / "sat").exists()
    assert not uv_log.exists()


@pytest.mark.skipif(sys.platform != "linux", reason="installer supports Linux/WSL")
def test_installer_refuses_an_unowned_private_runtime_and_preserves_openclaw(
    tmp_path: Path,
) -> None:
    checkout = prepare_checkout(tmp_path)
    environment, install_bin, _, _ = fake_environment(tmp_path, checkout)
    private_marker = checkout / ".sat/openclaw/.sat-owned-runtime"
    private_marker.unlink()
    existing_config = Path(environment["HOME"]) / ".openclaw/openclaw.json"

    completed = run_installer(checkout, environment)

    assert completed.returncode == 1
    assert "existing OpenClaw runtime is not owned by SAT" in completed.stderr
    assert not (install_bin / "sat").exists()
    assert existing_config.read_text(encoding="utf-8") == (
        '{"existing":"must stay unchanged"}\n'
    )
    assert not (tmp_path / "existing-openclaw.log").exists()


@pytest.mark.skipif(sys.platform != "linux", reason="setup supports Linux/WSL")
def test_setup_bootstraps_openclaw_with_isolated_state_and_home(
    tmp_path: Path,
) -> None:
    checkout = prepare_checkout(tmp_path)
    environment, _, _, _ = fake_environment(tmp_path, checkout)
    private_runtime = checkout / ".sat/openclaw"
    shutil.rmtree(private_runtime)
    installer_log = tmp_path / "openclaw-installer-environment.log"
    environment["FAKE_OPENCLAW_INSTALL_LOG"] = str(installer_log)
    environment["OPENCLAW_SHOW_SECRETS"] = "1"
    fake_curl = Path(environment["PATH"].split(":", maxsplit=1)[0]) / "curl"
    write_executable(
        fake_curl,
        """#!/usr/bin/env bash
cat <<'INSTALLER'
#!/usr/bin/env bash
set -euo pipefail
prefix=""
version=""
node_version=""
while (($#)); do
  case "$1" in
    --prefix) prefix="$2"; shift 2 ;;
    --version) version="$2"; shift 2 ;;
    --node-version) node_version="$2"; shift 2 ;;
    --no-onboard) shift ;;
    *) exit 2 ;;
  esac
done
{
  printf 'HOME=%s\n' "$HOME"
  printf 'STATE=%s\n' "$OPENCLAW_STATE_DIR"
  printf 'CONFIG=%s\n' "$OPENCLAW_CONFIG_PATH"
  printf 'OAUTH=%s\n' "$OPENCLAW_OAUTH_DIR"
  printf 'WORKSPACE=%s\n' "$OPENCLAW_WORKSPACE_DIR"
  printf 'AGENT=%s\n' "$OPENCLAW_AGENT_DIR"
  printf 'PROFILE=%s\n' "$OPENCLAW_PROFILE"
  printf 'AMBIENT_PREFIX=%s\n' "${OPENCLAW_PREFIX-unset}"
  printf 'AMBIENT_SHOW_SECRETS=%s\n' "${OPENCLAW_SHOW_SECRETS-unset}"
  printf 'PREFIX=%s\n' "$prefix"
} > "$FAKE_OPENCLAW_INSTALL_LOG"
mkdir -p "$prefix/bin" "$prefix/tools/node-v$node_version/bin"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  '[[ "${OPENCLAW_STATE_DIR:-}" == "$HOME/state" ]] || exit 4' \
  '[[ "${OPENCLAW_CONFIG_PATH:-}" == "$HOME/state/openclaw.json" ]] || exit 5' \
  "echo 'OpenClaw $version (test)'" > "$prefix/bin/openclaw"
printf '%s\n' '#!/usr/bin/env bash' "echo 'v$node_version'" > \
  "$prefix/tools/node-v$node_version/bin/node"
chmod 755 "$prefix/bin/openclaw" "$prefix/tools/node-v$node_version/bin/node"
INSTALLER
""",
    )

    completed = subprocess.run(
        [str(checkout / "scripts/setup.sh")],
        cwd=checkout,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    recorded = dict(
        line.split("=", maxsplit=1)
        for line in installer_log.read_text(encoding="utf-8").splitlines()
    )
    installer_home = Path(recorded["HOME"])
    assert installer_home.parent == checkout / ".sat"
    assert installer_home.name.startswith(".install-home.")
    assert recorded["STATE"] == str(installer_home / "state")
    assert recorded["CONFIG"] == str(installer_home / "state/openclaw.json")
    assert recorded["OAUTH"] == str(installer_home / "state/credentials")
    assert recorded["WORKSPACE"] == str(installer_home / "state/workspace")
    assert recorded["AGENT"] == ""
    assert recorded["PROFILE"] == ""
    assert recorded["AMBIENT_PREFIX"] == "unset"
    assert recorded["AMBIENT_SHOW_SECRETS"] == "unset"
    assert recorded["PREFIX"] == str(private_runtime)
    assert not installer_home.exists()
    existing_config = Path(environment["HOME"]) / ".openclaw/openclaw.json"
    assert existing_config.read_text(encoding="utf-8") == (
        '{"existing":"must stay unchanged"}\n'
    )
    assert not (tmp_path / "existing-openclaw.log").exists()
