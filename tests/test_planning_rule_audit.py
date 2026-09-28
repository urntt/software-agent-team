"""The Planning rule audit must classify behavior without echoing run content."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_rule_audit_counts_normalization_and_rejection_without_raw_text(
    tmp_path: Path,
) -> None:
    turn_path = tmp_path / "001.json"
    secret = "private-request-marker-123"
    turn_path.write_text(
        json.dumps(
            {
                "prompt": secret,
                "response_text": secret,
                "execution": {"model": f"deepseek/{secret}"},
                "response_normalizations": [
                    "compiled proposal.decisions[0].authority from category delivery",
                    "canonicalized proposal.tasks[0].expected_paths[0]",
                ],
                "response_validation": {
                    "failure_class": "semantic_context",
                    "issues": [
                        {
                            "invariant_id": "planning_writer_criterion_coverage",
                            "message": secret,
                        }
                    ],
                },
                "semantic_correction_outcome": "improved",
                "validation_error": secret,
            }
        ),
        encoding="utf-8",
    )
    script = Path(__file__).resolve().parents[1] / "scripts/audit_planning_rules.py"
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--source-version",
            "v0.4.31",
            str(turn_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert secret not in result.stdout
    report = json.loads(result.stdout)
    assert report["unclassified_normalization_count"] == 0
    assert {row["rule_id"] for row in report["rules"]} == {
        "decision_authority_compilation",
        "task_path",
        "planning_writer_criterion_coverage",
    }
    assert {row["turn_result"] for row in report["rules"]} == {"rejected"}
    assert {row["correction_outcome"] for row in report["rules"]} == {"improved"}
