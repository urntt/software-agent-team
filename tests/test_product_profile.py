"""Contract tests for the task-independent Python product profile."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

from software_agent_team.quality_gates import load_quality_gate_configuration

REPOSITORY_ROOT = Path(__file__).parents[1]
PROFILE_ROOT = REPOSITORY_ROOT / "profiles" / "python"


def write_valid_project(root: Path) -> None:
    """Create the smallest project accepted by the trusted profile validator."""

    subprocess.run(
        ["git", "init", "--quiet", str(root)],
        check=True,
        capture_output=True,
        text=True,
    )
    (root / ".gitignore").write_text(".venv/\nuv.lock\n", encoding="utf-8")
    (root / "README.md").write_text(
        """# Link Checker

## Installation

`uv sync --dev`

## Usage

`uv run link-checker .`

## Testing

`uv run pytest`

## Known limitations

Local files only.
""",
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text(
        "[project]\nname='link-checker'\n", encoding="utf-8"
    )
    (root / "sat-project.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "setup": ["uv", "sync", "--dev"],
                "start": ["uv", "run", "link-checker", "."],
                "test": ["uv", "run", "pytest"],
            }
        ),
        encoding="utf-8",
    )
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_links.py").write_text(
        "def test_placeholder():\n    assert True\n", encoding="utf-8"
    )


def run_validator(repository: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(PROFILE_ROOT / "validation" / "run.py"),
            "--repository",
            str(repository),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def commit_project(repository: Path) -> None:
    """Commit the test project so clean-copy validation has a Git authority."""

    for arguments in (
        ("config", "user.name", "urntt"),
        ("config", "user.email", "urntts@gmail.com"),
        ("add", "."),
        ("commit", "--quiet", "-m", "test: initialize generated project"),
    ):
        subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=True,
            capture_output=True,
            text=True,
        )


def write_fake_uv(path: Path) -> Path:
    """Create a deterministic exact-command executable for host-side tests."""

    executable = path / "uv"
    executable.write_text(
        """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

cwd = Path.cwd()
argv = sys.argv[1:]
log = Path(os.environ["FAKE_UV_LOG"])
with log.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps({"argv": argv, "cwd": str(cwd)}) + "\\n")
if (cwd / ".git").exists() or (cwd / "local-only.txt").exists():
    raise SystemExit(31)
if os.environ.get("FAKE_UV_FAIL") == " ".join(argv):
    raise SystemExit(32)
if argv == ["sync", "--dev"]:
    (cwd / ".venv").mkdir()
    (cwd / ".venv" / "ready").write_text("ready", encoding="utf-8")
elif argv == ["run", "pytest"]:
    if not (cwd / ".venv" / "ready").is_file():
        raise SystemExit(33)
elif argv == ["run", "link-checker", "."]:
    if not (cwd / ".venv" / "ready").is_file():
        raise SystemExit(34)
else:
    raise SystemExit(35)
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def run_command_validator(
    repository: Path,
    *,
    environment: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(PROFILE_ROOT / "validation" / "run_commands.py"),
            "--repository",
            str(repository),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=20,
    )


def test_product_profile_is_separate_from_the_task_manager_evaluation() -> None:
    configuration = load_quality_gate_configuration(
        REPOSITORY_ROOT / "configs" / "product-policy.json",
        PROFILE_ROOT / "quality.json",
    )

    assert configuration.policy.id == "product_python_v1"
    assert configuration.manifest.id == "python_product_v1"
    assert configuration.policy.sandbox.image == "sat-python-quality:phase1-v5"
    assert configuration.policy.limits.total_timeout_seconds == 420
    serialized = json.dumps(
        {
            "policy": configuration.policy.model_dump(mode="json"),
            "profile": configuration.manifest.model_dump(mode="json"),
            "brief": configuration.task_brief.model_dump(mode="json"),
        }
    )
    assert "task-manager" not in serialized


def test_product_profile_exposes_fixed_command_ownership_to_planning() -> None:
    configuration = load_quality_gate_configuration(
        REPOSITORY_ROOT / "configs" / "product-policy.json",
        PROFILE_ROOT / "quality.json",
    )

    command_constraint = next(
        constraint
        for constraint in configuration.task_brief.constraints
        if "sat-project.json" in constraint
    )

    assert '["uv", "sync", "--dev"]' in command_constraint
    assert '["uv", "run", "pytest"]' in command_constraint
    assert "replace only the start placeholder" in command_constraint
    assert any(
        "directly usable from the project root" in constraint
        for constraint in configuration.task_brief.constraints
    )
    assert any(
        "clean quality workspace before setup" in constraint
        for constraint in configuration.task_brief.constraints
    )


def test_product_test_gate_matches_the_delivered_pytest_entrypoint() -> None:
    configuration = load_quality_gate_configuration(
        REPOSITORY_ROOT / "configs" / "product-policy.json",
        PROFILE_ROOT / "quality.json",
    )
    gate = next(
        gate
        for gate in configuration.manifest.gates
        if gate.id == "CHECK_PROJECT_TESTS"
    )
    project_contract = json.loads(
        (PROFILE_ROOT / "seed" / "sat-project.json").read_text(encoding="utf-8")
    )

    assert project_contract["test"] == ["uv", "run", "pytest"]
    assert gate.argv == ("pytest", "-q", "-p", "no:cacheprovider")
    exact_gate = next(
        gate
        for gate in configuration.manifest.gates
        if gate.id == "CHECK_EXACT_PROJECT_COMMANDS"
    )
    assert exact_gate.argv == (
        "python",
        "/opt/software-agent-team/inputs/python-product-contract/run_commands.py",
        "--repository",
        "/workspace",
    )
    assert exact_gate.criterion_ids == ("AC_RUNNABLE", "AC_TESTS")
    seed_configuration = tomllib.loads(
        (PROFILE_ROOT / "seed" / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert seed_configuration["tool"]["pytest"]["ini_options"]["pythonpath"] == [
        ".",
        "src",
    ]


def test_product_contract_validator_accepts_project_specific_commands(
    tmp_path: Path,
) -> None:
    write_valid_project(tmp_path)

    result = run_validator(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "passed" in result.stdout


def test_product_contract_validator_rejects_the_starter_placeholder(
    tmp_path: Path,
) -> None:
    write_valid_project(tmp_path)
    payload = json.loads((tmp_path / "sat-project.json").read_text(encoding="utf-8"))
    payload["start"] = ["uv", "run", "replace-with-project-entrypoint"]
    (tmp_path / "sat-project.json").write_text(json.dumps(payload), encoding="utf-8")

    result = run_validator(tmp_path)

    assert result.returncode == 1
    assert "starter placeholder" in result.stderr


def test_product_contract_accepts_ordinary_documentation_headings(
    tmp_path: Path,
) -> None:
    write_valid_project(tmp_path)

    result = run_validator(tmp_path)

    assert result.returncode == 0, result.stderr
    readme = (tmp_path / "README.md").read_text(encoding="utf-8").casefold()
    assert "setup" not in readme
    assert "start" not in readme


def test_product_contract_requires_each_exact_manifest_command(
    tmp_path: Path,
) -> None:
    write_valid_project(tmp_path)
    readme = (tmp_path / "README.md").read_text(encoding="utf-8")
    (tmp_path / "README.md").write_text(
        readme.replace("`uv run link-checker .`", "`uv run link-checker --help`"),
        encoding="utf-8",
    )

    result = run_validator(tmp_path)

    assert result.returncode == 1
    assert "missing exact command guidance for: start" in result.stderr


def test_product_contract_requires_clean_setup_artifact_policy(
    tmp_path: Path,
) -> None:
    write_valid_project(tmp_path)
    (tmp_path / ".gitignore").write_text(".venv/\n", encoding="utf-8")

    result = run_validator(tmp_path)

    assert result.returncode == 1
    assert "uv.lock must be committed or explicitly excluded" in result.stderr


def test_product_contract_accepts_a_present_bounded_uv_lock(
    tmp_path: Path,
) -> None:
    write_valid_project(tmp_path)
    (tmp_path / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    (tmp_path / "uv.lock").write_text("version = 1\n", encoding="utf-8")

    result = run_validator(tmp_path)

    assert result.returncode == 0, result.stderr


def test_product_contract_rejects_a_symlinked_uv_lock(tmp_path: Path) -> None:
    write_valid_project(tmp_path)
    (tmp_path / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    outside = tmp_path / "outside.lock"
    outside.write_text("version = 1\n", encoding="utf-8")
    (tmp_path / "uv.lock").symlink_to(outside)

    result = run_validator(tmp_path)

    assert result.returncode == 1
    assert "uv.lock must be a regular file" in result.stderr


def test_product_contract_rejects_an_oversized_uv_lock(tmp_path: Path) -> None:
    write_valid_project(tmp_path)
    (tmp_path / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    (tmp_path / "uv.lock").write_bytes(b"x" * 1_048_577)

    result = run_validator(tmp_path)

    assert result.returncode == 1
    assert "uv.lock is too large" in result.stderr


def test_product_contract_requires_the_setup_environment_to_be_ignored(
    tmp_path: Path,
) -> None:
    write_valid_project(tmp_path)
    (tmp_path / ".gitignore").write_text("uv.lock\n", encoding="utf-8")

    result = run_validator(tmp_path)

    assert result.returncode == 1
    assert "root .venv setup directory" in result.stderr


def test_product_contract_rejects_a_negated_setup_ignore_rule(
    tmp_path: Path,
) -> None:
    write_valid_project(tmp_path)
    (tmp_path / ".gitignore").write_text(
        ".venv/\nuv.lock\n!uv.lock\n",
        encoding="utf-8",
    )

    result = run_validator(tmp_path)

    assert result.returncode == 1
    assert ".gitignore must effectively exclude uv.lock" in result.stderr


def test_exact_command_gate_uses_only_committed_files_in_fresh_scratch(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    write_valid_project(project)
    commit_project(project)
    (project / "local-only.txt").write_text("must not be copied", encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    write_fake_uv(fake_bin)
    log = tmp_path / "uv.jsonl"
    environment = {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_UV_LOG": str(log),
    }

    result = run_command_validator(project, environment=environment)

    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary == {
        "setup": "passed",
        "source": "committed_tracked_files",
        "start": "exited_zero",
        "test": "passed",
        "workspace": "fresh_sandbox_scratch_copy",
    }
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [call["argv"] for call in calls] == [
        ["sync", "--dev"],
        ["run", "pytest"],
        ["run", "link-checker", "."],
    ]
    assert len({call["cwd"] for call in calls}) == 1
    assert calls[0]["cwd"] != str(project)
    assert not (project / ".venv").exists()


def test_exact_command_gate_reports_setup_failure_without_running_later_commands(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    write_valid_project(project)
    commit_project(project)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    write_fake_uv(fake_bin)
    log = tmp_path / "uv.jsonl"
    environment = {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_UV_LOG": str(log),
        "FAKE_UV_FAIL": "sync --dev",
    }

    result = run_command_validator(project, environment=environment)

    assert result.returncode == 1
    assert "setup command failed" in result.stderr
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [call["argv"] for call in calls] == [["sync", "--dev"]]


def test_exact_command_gate_rejects_modified_tracked_files(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    write_valid_project(project)
    commit_project(project)
    readme = (project / "README.md").read_text(encoding="utf-8")
    (project / "README.md").write_text(
        f"{readme}\nPost-commit change.\n",
        encoding="utf-8",
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    write_fake_uv(fake_bin)
    environment = {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_UV_LOG": str(tmp_path / "uv.jsonl"),
    }

    result = run_command_validator(project, environment=environment)

    assert result.returncode == 1
    assert "differ from the immutable commit" in result.stderr


def test_exact_command_gate_rejects_a_tracked_symlink(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    write_valid_project(project)
    (project / "linked-readme").symlink_to("README.md")
    commit_project(project)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    write_fake_uv(fake_bin)
    environment = {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_UV_LOG": str(tmp_path / "uv.jsonl"),
    }

    result = run_command_validator(project, environment=environment)

    assert result.returncode == 1
    assert "tracked entries must be regular files: linked-readme" in result.stderr
