"""Tests for the unified foundation CLI."""

from pathlib import Path

import pytest

from software_agent_team.cli import main

REPOSITORY_ROOT = Path(__file__).parents[1]


def test_cli_accepts_the_checked_in_handoff(
    capsys: pytest.CaptureFixture[str],
) -> None:
    example = REPOSITORY_ROOT / "examples" / "handoff.json"

    assert main(["validate-handoff", str(example)]) == 0
    assert "valid handoff" in capsys.readouterr().out


def test_cli_accepts_the_checked_in_task_brief(
    capsys: pytest.CaptureFixture[str],
) -> None:
    example = REPOSITORY_ROOT / "examples" / "task-brief.json"

    assert main(["validate-task-brief", str(example)]) == 0
    assert "state=confirmed" in capsys.readouterr().out


def test_cli_accepts_the_checked_in_phase_artifact(
    capsys: pytest.CaptureFixture[str],
) -> None:
    example = REPOSITORY_ROOT / "examples" / "implementation-plan.json"

    assert main(["validate-artifact", str(example)]) == 0
    output = capsys.readouterr().out
    assert "kind=implementation_plan" in output
    assert "iteration=1" in output


def test_cli_validates_the_complete_configuration(
    capsys: pytest.CaptureFixture[str],
) -> None:
    teams = REPOSITORY_ROOT / "configs" / "teams.json"
    openclaw = REPOSITORY_ROOT / "configs" / "openclaw.example.json5"

    assert (
        main(
            [
                "validate-config",
                "--teams",
                str(teams),
                "--openclaw",
                str(openclaw),
            ]
        )
        == 0
    )
    assert "teams=3" in capsys.readouterr().out


def test_cli_lists_the_default_team(
    capsys: pytest.CaptureFixture[str],
) -> None:
    teams = REPOSITORY_ROOT / "configs" / "teams.json"

    assert main(["list-teams", "--config", str(teams)]) == 0
    output = capsys.readouterr().out
    assert "* function_specialized" in output
    assert "implementation_domain_specialized" in output


def test_cli_rejects_invalid_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    invalid = tmp_path / "invalid.json"
    invalid.write_text('{"run_id": "incomplete"}', encoding="utf-8")

    assert main(["validate-handoff", str(invalid)]) == 1
    assert "error:" in capsys.readouterr().out
