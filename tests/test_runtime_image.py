"""Reproducibility checks for the shared Python sandbox image."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).parents[1]
RUNTIME_ROOT = REPOSITORY_ROOT / "runtime" / "python"
VALIDATION_ROOT = REPOSITORY_ROOT / "profiles" / "python" / "validation"
PORTABLE_LOCK = REPOSITORY_ROOT / "tests" / "fixtures" / "portable-registry.uv.lock"
RUNTIME_IMAGE = "sat-python-quality:phase1-v7"
PINNED_REQUIREMENT = re.compile(r"^[a-z0-9][a-z0-9._-]*==[^ ;]+(?: ; [a-z0-9_' .=]+)?$")


def requirement_lines(path: Path) -> tuple[str, ...]:
    """Return meaningful requirement lines without comments."""

    return tuple(
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def test_runtime_image_uses_content_pinned_base_and_dependency_lock() -> None:
    dockerfile = (RUNTIME_ROOT / "Dockerfile").read_text(encoding="utf-8")
    first_line = dockerfile.splitlines()[0]

    assert re.fullmatch(
        r"FROM python:3\.12\.13-slim-bookworm@sha256:[0-9a-f]{64}",
        first_line,
    )
    assert "COPY requirements.lock /opt/software-agent-team/requirements.lock" in (
        dockerfile
    )
    assert "COPY uv-offline.toml /opt/software-agent-team/uv-offline.toml" in dockerfile
    assert "pip install --no-cache-dir --requirement" in dockerfile
    assert "pip download --no-cache-dir --only-binary=:all:" in dockerfile
    assert "UV_CACHE_DIR=/tmp/uv-cache" in dockerfile
    assert "UV_CONFIG_FILE=/opt/software-agent-team/uv-offline.toml" in dockerfile
    assert "requirements.lock colorama==0.4.6" in dockerfile
    assert "COPY sat_probe_write.py /usr/local/bin/sat-probe-write" in dockerfile
    assert "COPY sat_probe_run.py /usr/local/bin/sat-probe-run" in dockerfile
    assert "chown 0:0 /usr/local/bin/sat-probe-write /usr/local/bin/sat-probe-run" in (
        dockerfile
    )
    assert "chmod 0555 /usr/local/bin/sat-probe-write /usr/local/bin/sat-probe-run" in (
        dockerfile
    )
    assert 'CMD ["sleep", "infinity"]' in dockerfile

    uv_configuration = (RUNTIME_ROOT / "uv-offline.toml").read_text(encoding="utf-8")
    assert uv_configuration == (
        "offline = true\n"
        "no-index = true\n"
        'find-links = ["/opt/software-agent-team/wheels"]\n'
    )

    helper = (RUNTIME_ROOT / "sat_probe_write.py").read_text(encoding="utf-8")
    assert helper.startswith("#!/usr/local/bin/python\n")
    assert 'TMP_DIRECTORY = "/tmp"' in helper
    assert "os.O_EXCL" in helper
    assert "os.O_NOFOLLOW" in helper

    runner = (RUNTIME_ROOT / "sat_probe_run.py").read_text(encoding="utf-8")
    assert runner.startswith("#!/usr/local/bin/python\n")
    assert 'RESULT_PREFIX = "SAT_PROBE_RESULT_V1 "' in runner
    assert "os.O_NOFOLLOW" in runner
    assert "start_new_session=True" in runner
    assert "resource.RLIMIT_FSIZE" in runner


def test_runtime_dependency_lock_contains_only_exact_unique_versions() -> None:
    direct = requirement_lines(RUNTIME_ROOT / "requirements.in")
    locked = requirement_lines(RUNTIME_ROOT / "requirements.lock")

    assert len(locked) == len(set(locked))
    assert all(PINNED_REQUIREMENT.fullmatch(requirement) for requirement in locked)
    assert set(direct) <= set(locked)
    assert "hatchling==1.27.0" in locked
    assert "editables==0.5" in locked


def test_runtime_image_consumes_a_portable_registry_lock_offline(
    tmp_path: Path,
) -> None:
    """Exercise the exact command runner with real uv and the bundled wheels."""

    docker = shutil.which("docker")
    if docker is None:
        pytest.skip(
            "Docker is not installed; the static image contract remains covered"
        )
    inspected = subprocess.run(
        [docker, "image", "inspect", "--format", "{{.Id}}", RUNTIME_IMAGE],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if inspected.returncode != 0:
        pytest.skip("the configured runtime image is not built in this checkout")
    image_id = inspected.stdout.strip()
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", image_id)

    project = tmp_path / "project"
    project.mkdir(mode=0o755)
    files = {
        ".gitignore": ".venv/\n",
        "README.md": """# Portable registry fixture

## Installation

`uv sync --dev`

## Usage

`uv run python -c pass`

## Testing

`uv run pytest`

## Known limitations

This fixture only validates the offline dependency boundary.
""",
        "pyproject.toml": """[build-system]
requires = ["hatchling>=1.27,<2"]
build-backend = "hatchling.build"

[project]
name = "markdown-link-auditor"
version = "0.1.0"
requires-python = ">=3.12,<3.13"
dependencies = []

[dependency-groups]
dev = [
  "pytest==8.4.2",
  "ruff==0.12.8",
]

[tool.uv]
package = true

[tool.pytest.ini_options]
testpaths = ["tests"]
""",
        "sat-project.json": json.dumps(
            {
                "schema_version": 1,
                "setup": ["uv", "sync", "--dev"],
                "start": ["uv", "run", "python", "-c", "pass"],
                "test": ["uv", "run", "pytest"],
            }
        ),
        "src/markdown_link_auditor/__init__.py": '"""Fixture package."""\n',
        "tests/test_fixture.py": "def test_fixture():\n    assert True\n",
        "uv.lock": PORTABLE_LOCK.read_text(encoding="utf-8"),
    }
    for relative, content in files.items():
        destination = project / relative
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        destination.write_text(content, encoding="utf-8")
        destination.chmod(0o644)

    for arguments in (
        ("init", "--quiet"),
        ("config", "user.name", "urntt"),
        ("config", "user.email", "urntts@gmail.com"),
        ("add", "."),
        ("commit", "--quiet", "-m", "test: initialize portable fixture"),
    ):
        subprocess.run(
            ["git", "-C", str(project), *arguments],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )

    # The container runs as an unrelated non-root identity. Make only this
    # throwaway fixture world-readable so Git can verify the mounted commit.
    for path in project.rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o644)

    validator = tmp_path / "validator"
    validator.mkdir(mode=0o755)
    for name in ("run.py", "run_commands.py"):
        destination = validator / name
        shutil.copyfile(VALIDATION_ROOT / name, destination)
        destination.chmod(0o755)

    completed = subprocess.run(
        [
            docker,
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "128",
            "--memory",
            "768m",
            "--cpus",
            "1",
            "--user",
            "65532:65532",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=512m,mode=1777",
            "--env",
            "HOME=/tmp/home",
            "--env",
            "GIT_CONFIG_COUNT=1",
            "--env",
            "GIT_CONFIG_KEY_0=safe.directory",
            "--env",
            "GIT_CONFIG_VALUE_0=/project",
            "--volume",
            f"{project}:/project:ro",
            "--volume",
            f"{validator}:/validator:ro",
            image_id,
            "python",
            "/validator/run_commands.py",
            "--repository",
            "/project",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=150,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "setup": "passed",
        "source": "committed_tracked_files",
        "start": "exited_zero",
        "test": "passed",
        "workspace": "fresh_sandbox_scratch_copy",
    }
