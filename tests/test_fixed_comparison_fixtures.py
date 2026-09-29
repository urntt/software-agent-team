"""Cross-file contracts for the expanded frozen topology comparison."""

from __future__ import annotations

from pathlib import Path

import pytest

from software_agent_team.quality_gates import load_quality_gate_configuration

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "configs/run-policy.json"


@pytest.mark.parametrize("name", ("markdown_links", "csv_invoice", "json_merge"))
def test_expanded_comparison_fixture_has_one_complete_quality_contract(
    name: str,
) -> None:
    fixture = ROOT / "benchmarks" / name
    loaded = load_quality_gate_configuration(POLICY, fixture / "benchmark.json")
    criterion_ids = {
        criterion.id for criterion in loaded.task_brief.acceptance_criteria
    }
    gate_coverage = {
        criterion_id
        for gate in loaded.manifest.gates
        for criterion_id in gate.criterion_ids
    }

    assert loaded.task_brief.confirmed
    assert criterion_ids == {"AC_OUTPUT", "AC_ERRORS", "AC_QUALITY"}
    assert gate_coverage == criterion_ids
    assert len(loaded.input_mounts) == 1
    assert loaded.input_mounts[0].source == (fixture / "acceptance").resolve()
    assert (fixture / "acceptance/run.py").is_file()
    assert (fixture / "seed/README.md").is_file()
    assert (fixture / "seed/pyproject.toml").is_file()
