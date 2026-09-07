"""Exercise checkout identity with real Git and isolated runtime probes."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def prepare(root: Path) -> dict[str, str]:
    root.mkdir()
    for directory in ("scripts", "configs", "profiles", "runtime", "benchmarks"):
        shutil.copytree(ROOT / directory, root / directory)
    for name in ("README.md", "VISION.md", ".gitignore"):
        shutil.copy2(ROOT / name, root / name)
    prefix = root / ".sat/openclaw"
    for relative, output in (
        ("bin/openclaw", "OpenClaw 2026.7.1-2"),
        ("tools/node-v24.19.0/bin/node", "v24.19.0"),
    ):
        executable = prefix / relative
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_text(
            "#!/bin/sh\n"
            '[ -z "${STATE_DIRECTORY-}${NODE_OPTIONS-}${NODE_PATH-}" ] || exit 9\n'
            f"echo '{output}'\n",
            encoding="utf-8",
        )
        executable.chmod(0o700)
    (prefix / ".sat-owned-runtime").write_text(
        f"software-agent-team-openclaw-runtime-v1\nroot={prefix}\n",
        encoding="utf-8",
    )
    uv = root / ".sat/uv"
    uv.write_text("#!/bin/sh\necho 3.12\n", encoding="utf-8")
    uv.chmod(0o700)
    environment = dict(os.environ, UV_BIN=str(uv))
    environment.update(
        STATE_DIRECTORY="/foreign/state",
        NODE_OPTIONS="--require=/foreign/preload.js",
        NODE_PATH="/foreign/modules",
    )
    return environment


@pytest.mark.parametrize(
    "checkout", ["main", "feature", "detached", "unborn", "parent"]
)
def test_doctor_requires_committed_independent_checkout(
    tmp_path: Path, checkout: str
) -> None:
    root = tmp_path / "checkout"
    environment = prepare(root)
    git_root = tmp_path if checkout == "parent" else root
    git(git_root, "init", "--initial-branch=main")
    if checkout != "unborn":
        git(
            git_root,
            "-c",
            "user.name=urntt",
            "-c",
            "user.email=urntts@gmail.com",
            "commit",
            "--allow-empty",
            "-m",
            "test: initialize checkout",
        )
    if checkout == "feature":
        git(root, "switch", "-c", "test-feature")
    elif checkout == "detached":
        git(root, "tag", "v0.1.0")
        git(root, "checkout", "--detach", "v0.1.0")
    result = subprocess.run(
        ["bash", str(root / "scripts/doctor.sh")],
        cwd=root,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    if checkout == "unborn":
        assert result.returncode != 0
        assert "committed HEAD" in result.stderr
    elif checkout == "parent":
        assert result.returncode != 0
        assert "independent Git repository" in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        assert "repository boundaries are valid" in result.stdout
