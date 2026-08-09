"""Tests for the unified foundation CLI."""

from pathlib import Path
from types import SimpleNamespace

import pytest

import software_agent_team.cli as cli
from software_agent_team.cli import main
from software_agent_team.run_control import RunPhase
from software_agent_team.runtime_configuration import RuntimePreflight

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


def test_cli_prepares_the_frozen_benchmark_seed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    destination = tmp_path / "benchmark"

    assert main(["prepare-benchmark", str(destination)]) == 0

    assert (destination / ".git").is_dir()
    assert "prepared benchmark" in capsys.readouterr().out


def test_cli_preflight_makes_no_model_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    calls: list[str] = []

    def fake_materialize(*args: object, **kwargs: object) -> Path:
        calls.append("materialize")
        destination = args[1]
        assert isinstance(destination, Path)
        destination.write_text("{}", encoding="utf-8")
        return destination

    def fake_inspect(**kwargs: object) -> RuntimePreflight:
        calls.append("inspect")
        return RuntimePreflight(
            openclaw_binary="/opt/openclaw",
            openclaw_version="OpenClaw test",
            runtime_config=str(kwargs["runtime_config"]),
            sandbox_binary="/usr/bin/docker",
            sandbox_version="Docker version test",
            sandbox_image="sat-task-manager-quality:phase1-v1",
            config_valid=True,
            sandbox_image_present=True,
        )

    monkeypatch.setattr(cli, "materialize_run_configuration", fake_materialize)
    monkeypatch.setattr(cli, "inspect_runtime_preflight", fake_inspect)

    assert main(["preflight", str(workspace)]) == 0
    assert calls == ["materialize", "inspect"]
    assert "runtime preflight: ready" in capsys.readouterr().out


def test_cli_run_constructs_the_real_phase1_boundary_without_invoking_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    observed: dict[str, object] = {}

    class FakeCoordinator:
        def __init__(self, **kwargs: object) -> None:
            observed.update(kwargs)

        def execute(self, task_brief: object, **kwargs: object) -> SimpleNamespace:
            observed["task_brief"] = task_brief
            observed["execute"] = kwargs
            record = SimpleNamespace(
                phase=RunPhase.COMPLETED,
                run_id="task-manager-phase1",
                current_commit="a" * 40,
            )
            return SimpleNamespace(
                record=record,
                human_report_path="final-report.md",
            )

    monkeypatch.setattr(cli, "WorkflowCoordinator", FakeCoordinator)
    brief = REPOSITORY_ROOT / "benchmarks" / "task_manager" / "task-brief.json"

    result = main(
        [
            "run",
            str(brief),
            str(source),
            "--runs-root",
            str(tmp_path / "runs"),
            "--worktrees-root",
            str(tmp_path / "worktrees"),
            "--model",
            "provider/model",
            "--input-cost-per-million-usd",
            "2.50",
            "--output-cost-per-million-usd",
            "10.00",
        ]
    )

    assert result == 0
    pricing = observed["pricing"]
    assert pricing.model == "provider/model"
    assert observed["budget"].max_calls == 14
    assert observed["execute"] == {
        "source_repository": source,
        "base_ref": "HEAD",
    }
    assert "run completed" in capsys.readouterr().out
