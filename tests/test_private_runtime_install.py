"""Offline tests for the SAT-owned dependency installer, not an upstream stub."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tarfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


@contextmanager
def temporary_directory_mode(path: Path, mode: int) -> Iterator[None]:
    """Apply a restrictive test mode without leaking it into pytest cleanup."""

    original_mode = path.stat().st_mode & 0o777
    path.chmod(mode)
    try:
        yield
    finally:
        path.chmod(original_mode)


@contextmanager
def temporary_directory_owner(path: Path, uid: int, gid: int) -> Iterator[None]:
    """Apply a foreign test owner and restore the test runner's ownership."""

    metadata = path.stat()
    os.chown(path, uid, gid)
    try:
        yield
    finally:
        os.chown(path, metadata.st_uid, metadata.st_gid)


def executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o700)


def fixture(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    root = tmp_path / "checkout with spaces"
    for name in (
        "scripts/install-openclaw.sh",
        "scripts/openclaw-environment.sh",
        "configs/toolchain.sh",
    ):
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, target)
    prefix = root / ".sat/openclaw"
    prefix.mkdir(parents=True)
    (prefix / ".sat-owned-runtime").write_text(
        f"software-agent-team-openclaw-runtime-v1\nroot={prefix}\n"
    )
    home = root / ".sat/.install-home.test"
    home.mkdir()
    node_tree = tmp_path / "node-fixture"
    executable(
        node_tree / "bin/node",
        """#!/bin/bash
set -euo pipefail
[[ -z "${STATE_DIRECTORY:-}${NODE_OPTIONS:-}${NODE_PATH:-}" ]] || exit 13
if [[ -n "${EXPECTED_CACHE:-}" ]]; then
  [[ "${NODE_COMPILE_CACHE:-}" == "$EXPECTED_CACHE" ]] || exit 14
  [[ ! -v NODE_DISABLE_COMPILE_CACHE ]] || exit 15
fi
printf '%s\\n' "$*" >> "$CALL_LOG"
case "$1" in
  --version) echo v24.19.0 ;;
  -e) exit "${SQLITE_EXIT:-0}" ;;
  */npm-cli.js)
    [[ -z "${NPM_CONFIG_PREFIX:-}" ]] || exit 8
    [[ "$NPM_CONFIG_USERCONFIG" == "$HOME/.npmrc" ]] || exit 9
    exit "${NPM_EXIT:-0}" ;;
  */entry.js)
    [[ "$2" == --version ]] || exit 10
    echo "OpenClaw ${REPORTED_VERSION:-2026.7.1-2}" ;;
  *) exit 11 ;;
esac
""",
    )
    archive = tmp_path / "node.tar.gz"
    with tarfile.open(archive, "w:gz") as stream:
        stream.add(node_tree, arcname="node-dist")
    package = tmp_path / "openclaw.tgz"
    package.write_bytes(b"test-only pinned package")
    manifest = root / "configs/toolchain.sh"
    text = manifest.read_text()
    for field, artifact in (
        ("task_node_x64_sha256", archive),
        ("task_node_arm64_sha256", archive),
        ("task_openclaw_sha256", package),
    ):
        text = (
            "\n".join(
                f'{field}="{hashlib.sha256(artifact.read_bytes()).hexdigest()}"'
                if line.startswith(field + "=")
                else line
                for line in text.splitlines()
            )
            + "\n"
        )
    manifest.write_text(text)
    fake_bin = tmp_path / "bin"
    executable(
        fake_bin / "curl",
        """#!/bin/bash
set -euo pipefail
printf '%s\\n' "$*" >> "$DOWNLOAD_LOG"
url=""; out=""
while (($#)); do
 case "$1" in
  https:*) url="$1"; shift ;;
  -o) out="$2"; shift 2 ;;
  *) shift ;;
 esac
done
[[ "${DOWNLOAD_EXIT:-0}" == 0 ]] || exit "$DOWNLOAD_EXIT"
case "$url" in
 https://nodejs.org/dist/v24.19.0/node-v24.19.0-linux-*.tar.gz)
   cp "$NODE_ARCHIVE" "$out"; kind=node ;;
 https://registry.npmjs.org/openclaw/-/openclaw-2026.7.1-2.tgz)
   cp "$PACKAGE_ARCHIVE" "$out"; kind=package ;;
 *) exit 12 ;;
esac
[[ "${CORRUPT:-}" != "$kind" ]] || printf bad >> "$out"
""",
    )
    environment = dict(
        os.environ,
        HOME=str(home),
        PATH=f"{fake_bin}:{os.environ['PATH']}",
        NODE_ARCHIVE=str(archive),
        PACKAGE_ARCHIVE=str(package),
        CALL_LOG=str(tmp_path / "calls.log"),
        DOWNLOAD_LOG=str(tmp_path / "downloads.log"),
        NPM_CONFIG_PREFIX=str(tmp_path / "foreign-prefix"),
    )
    return root, prefix, environment


def run(
    root: Path, prefix: Path, environment: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(root / "scripts/install-openclaw.sh"), str(prefix)],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def test_private_install_verifies_pins_and_never_calls_gateway(tmp_path: Path) -> None:
    root, prefix, environment = fixture(tmp_path)
    result = run(root, prefix, environment)
    assert result.returncode == 0, result.stderr
    launcher = prefix / "bin/openclaw"
    probe = subprocess.run(
        [str(launcher), "--version"], env=environment, capture_output=True, text=True
    )
    assert probe.returncode == 0
    assert "2026.7.1-2" in probe.stdout
    assert "gateway" not in Path(environment["CALL_LOG"]).read_text()
    downloads = Path(environment["DOWNLOAD_LOG"]).read_text()
    assert "install-cli" not in downloads
    assert not (tmp_path / "foreign-prefix").exists()
    assert not list(prefix.glob(".download.*"))
    second = run(root, prefix, environment)
    assert second.returncode == 0, second.stderr
    assert Path(environment["DOWNLOAD_LOG"]).read_text().count("nodejs.org") == 1


@pytest.mark.parametrize(
    ("setting", "value", "message"),
    [
        ("CORRUPT", "node", "Node archive checksum mismatch"),
        ("CORRUPT", "package", "OpenClaw package checksum mismatch"),
        ("DOWNLOAD_EXIT", "7", "Node download failed"),
        ("SQLITE_EXIT", "1", "WAL-reset safety boundary"),
        ("NPM_EXIT", "2", "package installation failed"),
        ("REPORTED_VERSION", "wrong", "OpenClaw version mismatch"),
    ],
)
def test_private_install_failure_preserves_launcher(
    tmp_path: Path,
    setting: str,
    value: str,
    message: str,
) -> None:
    root, prefix, environment = fixture(tmp_path)
    executable(prefix / "bin/openclaw", "#!/bin/sh\necho existing\n")
    original = (prefix / "bin/openclaw").read_bytes()
    environment[setting] = value
    result = run(root, prefix, environment)
    assert result.returncode != 0
    assert message in result.stderr
    assert (prefix / "bin/openclaw").read_bytes() == original
    assert not list(prefix.glob(".download.*"))
    if setting == "CORRUPT" and value == "node":
        assert not Path(environment["CALL_LOG"]).exists()


def test_private_install_refuses_ambient_home(tmp_path: Path) -> None:
    root, prefix, environment = fixture(tmp_path)
    environment["HOME"] = str(tmp_path)
    result = run(root, prefix, environment)
    assert result.returncode != 0
    assert "isolated installer home" in result.stderr
    assert not Path(environment["DOWNLOAD_LOG"]).exists()


def test_private_install_drops_ambient_runtime_state_and_preloads(
    tmp_path: Path,
) -> None:
    root, prefix, environment = fixture(tmp_path)
    environment.update(
        STATE_DIRECTORY=str(tmp_path / "foreign-state"),
        NODE_OPTIONS="--require=/foreign/preload.js",
        NODE_PATH="/foreign/modules",
    )
    result = run(root, prefix, environment)
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "foreign-state").exists()


def test_private_runtime_owns_cache_before_node_and_on_direct_launch(
    tmp_path: Path,
) -> None:
    root, prefix, environment = fixture(tmp_path)
    foreign = tmp_path / "foreign-cache"
    foreign.mkdir(mode=0o700)
    sentinel = foreign / "keep"
    sentinel.write_text("foreign cache must stay untouched")
    with temporary_directory_mode(foreign, 0o500):
        environment.update(
            NODE_COMPILE_CACHE=str(foreign),
            NODE_DISABLE_COMPILE_CACHE="1",
            EXPECTED_CACHE=str(prefix / "compile-cache"),
        )
        result = run(root, prefix, environment)
        assert result.returncode == 0, result.stderr
        cache = prefix / "compile-cache"
        assert cache.is_dir() and cache.stat().st_mode & 0o777 == 0o700
        probe = subprocess.run(
            [str(prefix / "bin/openclaw"), "--version"],
            env=environment,
            capture_output=True,
            text=True,
        )
        assert probe.returncode == 0, probe.stderr
        assert sentinel.read_text() == "foreign cache must stay untouched"
        assert list(foreign.iterdir()) == [sentinel]


def test_private_runtime_refuses_redirected_cache_before_node(tmp_path: Path) -> None:
    root, prefix, environment = fixture(tmp_path)
    foreign = tmp_path / "foreign-cache"
    foreign.mkdir()
    (prefix / "compile-cache").symlink_to(foreign, target_is_directory=True)
    executable(prefix / "bin/openclaw", "#!/bin/sh\necho old\n")
    before = (prefix / "bin/openclaw").read_bytes()
    result = run(root, prefix, environment)
    assert result.returncode != 0
    assert "compile cache" in result.stderr
    assert (prefix / "bin/openclaw").read_bytes() == before
    assert not Path(environment["CALL_LOG"]).exists()
    assert not list(foreign.iterdir())


@pytest.mark.parametrize("mode", [0o500, 0o755, 0o770])
def test_private_runtime_preserves_invalid_cache_permissions(
    tmp_path: Path, mode: int
) -> None:
    root, prefix, environment = fixture(tmp_path)
    cache = prefix / "compile-cache"
    cache.mkdir(mode=0o700)
    with temporary_directory_mode(cache, mode):
        result = run(root, prefix, environment)
        assert result.returncode != 0
        assert "compile cache" in result.stderr
        assert cache.stat().st_mode & 0o777 == mode
        assert not Path(environment["CALL_LOG"]).exists()


def test_private_launcher_revalidates_cache_without_touching_redirect(
    tmp_path: Path,
) -> None:
    root, prefix, environment = fixture(tmp_path)
    assert run(root, prefix, environment).returncode == 0
    cache = prefix / "compile-cache"
    cache.rename(prefix / "preserved-cache")
    foreign = tmp_path / "foreign-cache"
    foreign.mkdir()
    cache.symlink_to(foreign, target_is_directory=True)
    calls = Path(environment["CALL_LOG"]).read_bytes()
    probe = subprocess.run(
        [str(prefix / "bin/openclaw"), "--version"],
        env=environment,
        capture_output=True,
        text=True,
    )
    assert probe.returncode != 0
    assert "compile cache" in probe.stderr
    assert Path(environment["CALL_LOG"]).read_bytes() == calls
    assert not list(foreign.iterdir())


def test_private_installations_use_distinct_reusable_caches(tmp_path: Path) -> None:
    caches = []
    for name in ("first", "second"):
        root, prefix, environment = fixture(tmp_path / name)
        environment["EXPECTED_CACHE"] = str(prefix / "compile-cache")
        assert run(root, prefix, environment).returncode == 0
        cache = prefix / "compile-cache"
        sentinel = cache / "retained"
        sentinel.write_text(name)
        before = cache.stat().st_ino
        assert run(root, prefix, environment).returncode == 0
        assert cache.stat().st_ino == before
        assert sentinel.read_text() == name
        caches.append(cache.resolve())
    assert caches[0] != caches[1]


@pytest.mark.skipif(os.getuid() != 0, reason="cross-UID fixture requires root")
def test_private_runtime_refuses_another_users_cache(tmp_path: Path) -> None:
    root, prefix, environment = fixture(tmp_path)
    cache = prefix / "compile-cache"
    cache.mkdir(mode=0o700)
    with temporary_directory_owner(cache, 65534, 65534):
        result = run(root, prefix, environment)
        assert result.returncode != 0
        assert "compile cache" in result.stderr
        assert cache.stat().st_uid == 65534
        assert not Path(environment["CALL_LOG"]).exists()
