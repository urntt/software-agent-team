"""Contract tests for the task-manager black-box acceptance suite."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from software_agent_team.artifacts import TaskBrief

REPOSITORY_ROOT = Path(__file__).parents[1]
BENCHMARK_ROOT = REPOSITORY_ROOT / "benchmarks" / "task_manager"


def load_acceptance_module() -> ModuleType:
    """Load the executable suite without adding it to the product package."""

    path = BENCHMARK_ROOT / "acceptance" / "run.py"
    spec = importlib.util.spec_from_file_location("task_manager_acceptance", path)
    if spec is None or spec.loader is None:  # pragma: no cover - import invariant
        raise AssertionError("could not load task-manager acceptance suite")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_acceptance_allows_valid_server_rendered_validation_responses() -> None:
    acceptance = load_acceptance_module()

    assert frozenset({200, 400, 422}) == acceptance.INVALID_FORM_STATUSES


def test_acceptance_allows_canonical_or_humanized_status_text() -> None:
    acceptance = load_acceptance_module()

    acceptance.require_any_text("<dd>todo</dd>", "todo", "to do")
    acceptance.require_any_text("<dd>To do</dd>", "todo", "to do")
    with pytest.raises(AssertionError, match="does not contain any"):
        acceptance.require_any_text("<dd>pending</dd>", "todo", "to do")


def test_confirmed_brief_exposes_the_fixed_form_and_field_contract() -> None:
    brief = TaskBrief.model_validate_json(
        (BENCHMARK_ROOT / "task-brief.json").read_text(encoding="utf-8")
    )
    requirements = " ".join(brief.requirements)

    assert brief.confirmed is True
    assert "standard HTML form" in requirements
    assert "canonical /tasks/{id} detail URL" in requirements
    assert all(value in requirements for value in ("todo", "in_progress", "done"))
    assert all(value in requirements for value in ("low", "medium", "high"))
