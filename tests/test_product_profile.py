"""Contract tests for the task-independent Python product profile."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from software_agent_team.quality_gates import load_quality_gate_configuration

REPOSITORY_ROOT = Path(__file__).parents[1]
PROFILE_ROOT = REPOSITORY_ROOT / "profiles" / "python"


def write_valid_project(root: Path) -> None:
    """Create the smallest project accepted by the trusted profile validator."""

    (root / "README.md").write_text(
        "# Link Checker\n\nSetup, start, test, and known limitations.\n",
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


def test_product_profile_is_separate_from_the_task_manager_evaluation() -> None:
    configuration = load_quality_gate_configuration(
        REPOSITORY_ROOT / "configs" / "product-policy.json",
        PROFILE_ROOT / "quality.json",
    )

    assert configuration.policy.id == "product_python_v1"
    assert configuration.manifest.id == "python_product_v1"
    assert configuration.policy.sandbox.image == "sat-python-quality:phase1-v1"
    serialized = json.dumps(
        {
            "policy": configuration.policy.model_dump(mode="json"),
            "profile": configuration.manifest.model_dump(mode="json"),
            "brief": configuration.task_brief.model_dump(mode="json"),
        }
    )
    assert "task-manager" not in serialized


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
