"""Tests for the unified foundation CLI."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import software_agent_team.cli as cli
from software_agent_team.benchmark_seed import prepare_benchmark_seed
from software_agent_team.cli import main
from software_agent_team.run_control import RunPhase
from software_agent_team.runtime_configuration import (
    RuntimePreflight,
    SandboxImageInspection,
)
from software_agent_team.user_configuration import (
    UserConfiguration,
    load_user_configuration,
    save_user_configuration,
)

REPOSITORY_ROOT = Path(__file__).parents[1]


def test_cli_first_launch_explains_configuration_and_run_flow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    configuration = tmp_path / "config.json"
    monkeypatch.setenv("SAT_CONFIG_PATH", str(configuration))

    assert main([]) == 0

    output = capsys.readouterr().out
    assert "installed but not configured yet" in output
    assert "sat configure" in output
    assert "sat prepare-benchmark" in output
    assert "sat preflight" in output
    assert "sat run" in output
    assert "sat-uninstall --help" in output
    assert not configuration.exists()


def test_cli_noninteractive_configuration_is_private_and_reconfigurable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "config.json"
    monkeypatch.setenv("SAT_CONFIG_PATH", str(path))

    assert (
        main(
            [
                "configure",
                "--non-interactive",
                "--model",
                "provider/model-a",
                "--input-cost-per-million-usd",
                "0.25",
                "--output-cost-per-million-usd",
                "1.75",
                "--verification-concurrency",
                "1",
                "--agent-timeout-seconds",
                "1200",
            ]
        )
        == 0
    )
    first = load_user_configuration(path)
    assert first is not None
    assert first.model == "provider/model-a"
    assert first.verification_concurrency == 1

    assert (
        main(
            [
                "configure",
                "--non-interactive",
                "--model",
                "provider/model-b",
            ]
        )
        == 0
    )
    second = load_user_configuration(path)
    assert second is not None
    assert second.model == "provider/model-b"
    assert second.input_cost_per_million_usd == first.input_cost_per_million_usd
    assert second.output_cost_per_million_usd == first.output_cost_per_million_usd
    assert second.verification_concurrency == first.verification_concurrency
    assert second.agent_timeout_seconds == first.agent_timeout_seconds
    output = capsys.readouterr().out
    assert "provider credentials: not stored by SAT" in output


def test_cli_interactive_configuration_prompts_for_first_run_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "config.json"
    answers = iter(("provider/model", "0.10", "0.20", "1", "1500"))
    monkeypatch.setenv("SAT_CONFIG_PATH", str(path))
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    assert main(["configure"]) == 0

    configuration = load_user_configuration(path)
    assert configuration is not None
    assert configuration.model == "provider/model"
    assert str(configuration.input_cost_per_million_usd) == "0.10"
    assert configuration.verification_concurrency == 1
    assert configuration.agent_timeout_seconds == 1500


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
    policy = REPOSITORY_ROOT / "configs" / "run-policy.json"
    benchmark = REPOSITORY_ROOT / "benchmarks" / "task_manager" / "benchmark.json"

    assert (
        main(
            [
                "validate-config",
                "--teams",
                str(teams),
                "--openclaw",
                str(openclaw),
                "--policy",
                str(policy),
                "--benchmark",
                str(benchmark),
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "teams=3" in output
    assert "policy=phase1_deterministic" in output
    assert "benchmark=task_manager_phase1" in output
    assert "gates=4" in output


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
    prepare_benchmark_seed(
        REPOSITORY_ROOT / "benchmarks" / "task_manager" / "seed",
        workspace,
    )
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
            sandbox_image_id=f"sha256:{'a' * 64}",
            config_valid=True,
            sandbox_image_present=True,
        )

    monkeypatch.setattr(cli, "materialize_run_configuration", fake_materialize)
    monkeypatch.setattr(cli, "inspect_runtime_preflight", fake_inspect)

    assert main(["preflight", str(workspace)]) == 0
    assert calls == ["materialize", "inspect"]
    output = capsys.readouterr().out
    assert "runtime preflight: ready" in output
    assert "source_commit=" in output


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
                run_id=task_brief.run_id,
                current_commit="a" * 40,
            )
            return SimpleNamespace(
                record=record,
                human_report_path="final-report.md",
            )

    def fake_inspect_sandbox_image(**kwargs: object) -> SandboxImageInspection:
        return SandboxImageInspection(
            sandbox_binary="/usr/bin/docker",
            sandbox_version="Docker version test",
            sandbox_image=str(kwargs["sandbox_image"]),
            sandbox_image_id=f"sha256:{'a' * 64}",
            sandbox_image_present=True,
        )

    monkeypatch.setattr(cli, "WorkflowCoordinator", FakeCoordinator)
    monkeypatch.setattr(cli, "inspect_sandbox_image", fake_inspect_sandbox_image)
    frozen_brief = REPOSITORY_ROOT / "benchmarks" / "task_manager" / "task-brief.json"
    payload = json.loads(frozen_brief.read_text(encoding="utf-8"))
    payload["run_id"] = "task-manager-trial-2"
    brief = tmp_path / "trial-task-brief.json"
    brief.write_text(json.dumps(payload), encoding="utf-8")

    result = main(
        [
            "run",
            str(brief),
            str(source),
            "--runs-root",
            str(tmp_path / "runs"),
            "--workspaces-root",
            str(tmp_path / "workspaces"),
            "--model",
            "provider/model",
            "--input-cost-per-million-usd",
            "2.50",
            "--output-cost-per-million-usd",
            "10.00",
            "--verification-concurrency",
            "1",
        ]
    )

    assert result == 0
    pricing = observed["pricing"]
    assert pricing.model == "provider/model"
    assert observed["budget"].max_calls == 14
    assert observed["verification_concurrency"] == 1
    assert observed["execute"] == {
        "source_repository": source,
        "base_ref": "HEAD",
    }
    assert observed["task_brief"].run_id == "task-manager-trial-2"
    gate_factory = observed["quality_gate_factory"]
    assert callable(gate_factory)
    run_directory = tmp_path / "gate-run"
    workspace = tmp_path / "gate-workspace"
    run_directory.mkdir()
    workspace.mkdir()
    gate_runner = gate_factory(run_directory, workspace)
    assert gate_runner.sandbox.image == f"sha256:{'a' * 64}"
    assert "run completed" in capsys.readouterr().out


def test_cli_run_uses_saved_defaults_when_flags_are_omitted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    observed: dict[str, object] = {}
    configuration_path = tmp_path / "config.json"
    monkeypatch.setenv("SAT_CONFIG_PATH", str(configuration_path))
    save_user_configuration(
        UserConfiguration(
            model="provider/saved-model",
            input_cost_per_million_usd="0.75",
            output_cost_per_million_usd="2.25",
            verification_concurrency=1,
            agent_timeout_seconds=1800,
        ),
        configuration_path,
    )

    class FakeCoordinator:
        def __init__(self, **kwargs: object) -> None:
            observed.update(kwargs)

        def execute(self, task_brief: object, **kwargs: object) -> SimpleNamespace:
            record = SimpleNamespace(
                phase=RunPhase.COMPLETED,
                run_id=task_brief.run_id,
                current_commit="a" * 40,
            )
            return SimpleNamespace(record=record, human_report_path="final-report.md")

    def fake_inspect_sandbox_image(**kwargs: object) -> SandboxImageInspection:
        return SandboxImageInspection(
            sandbox_binary="/usr/bin/docker",
            sandbox_version="Docker version test",
            sandbox_image=str(kwargs["sandbox_image"]),
            sandbox_image_id=f"sha256:{'a' * 64}",
            sandbox_image_present=True,
        )

    monkeypatch.setattr(cli, "WorkflowCoordinator", FakeCoordinator)
    monkeypatch.setattr(cli, "inspect_sandbox_image", fake_inspect_sandbox_image)

    assert (
        main(
            [
                "run",
                str(REPOSITORY_ROOT / "benchmarks/task_manager/task-brief.json"),
                str(source),
                "--runs-root",
                str(tmp_path / "runs"),
                "--workspaces-root",
                str(tmp_path / "workspaces"),
            ]
        )
        == 0
    )

    pricing = observed["pricing"]
    assert pricing.model == "provider/saved-model"
    assert str(pricing.input_cost_per_million_usd) == "0.75"
    assert observed["verification_concurrency"] == 1
    assert observed["agent_timeout_seconds"] == 1800


def test_cli_run_rejects_changes_to_frozen_benchmark(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    frozen_brief = REPOSITORY_ROOT / "benchmarks" / "task_manager" / "task-brief.json"
    payload = json.loads(frozen_brief.read_text(encoding="utf-8"))
    payload["title"] = "Changed benchmark"
    changed = tmp_path / "changed-task-brief.json"
    changed.write_text(json.dumps(payload), encoding="utf-8")

    assert (
        main(
            [
                "run",
                str(changed),
                str(tmp_path / "source"),
                "--model",
                "provider/model",
                "--input-cost-per-million-usd",
                "1",
                "--output-cost-per-million-usd",
                "2",
            ]
        )
        == 1
    )
    assert "permits only run_id" in capsys.readouterr().out
