"""Tests for the unified foundation CLI."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import software_agent_team.cli as cli
from software_agent_team.artifacts import AgentRole
from software_agent_team.benchmark_seed import prepare_benchmark_seed
from software_agent_team.cli import main
from software_agent_team.run_control import RunPhase
from software_agent_team.runtime_configuration import (
    OpenClawModelInspection,
    RuntimePreflight,
    SandboxImageInspection,
)
from software_agent_team.user_configuration import (
    UserConfiguration,
    load_user_configuration,
    save_user_configuration,
)

REPOSITORY_ROOT = Path(__file__).parents[1]


def test_cli_no_command_requires_an_interactive_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    configuration = tmp_path / "config.json"
    monkeypatch.setenv("SAT_CONFIG_PATH", str(configuration))

    assert main([]) == 1

    output = capsys.readouterr().out
    assert "guided product flow requires an interactive terminal" in output
    assert not configuration.exists()


def test_cli_no_command_runs_the_guided_product_journey(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SAT_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    answers = iter(
        (
            "Build a CLI that checks Markdown links.",
            "yes",
            "Exit non-zero for broken links; print file and line",
            "Use the standard library at runtime",
            "link-checker",
            "yes",
        )
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr(
        cli,
        "inspect_startup_environment",
        lambda **_kwargs: SimpleNamespace(ready=True, checks=()),
    )
    monkeypatch.setattr(cli, "render_startup_diagnostics", lambda _report: None)
    monkeypatch.setattr(
        cli,
        "_ensure_product_configuration",
        lambda _state_paths: UserConfiguration(model="provider/model"),
    )
    source = tmp_path / "prepared-source"
    source.mkdir()
    monkeypatch.setattr(cli, "prepare_product_source", lambda **_kwargs: source)
    observed: dict[str, object] = {}
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def fake_execute(task_brief: object, options: object) -> SimpleNamespace:
        observed["task_brief"] = task_brief
        observed["options"] = options
        return SimpleNamespace(
            final_report=SimpleNamespace(path="final-report.json", sha256="a" * 64),
            record=SimpleNamespace(
                phase=RunPhase.COMPLETED,
                workspace=SimpleNamespace(workspace_path=str(workspace)),
                current_commit="a" * 40,
            ),
        )

    monkeypatch.setattr(cli, "_execute_workflow", fake_execute)
    monkeypatch.setattr(
        cli,
        "_load_final_report",
        lambda _path, **_kwargs: object(),
    )
    monkeypatch.setattr(cli, "_render_product_outcome", lambda **_kwargs: None)
    monkeypatch.setattr(
        cli,
        "load_project_commands",
        lambda _path: SimpleNamespace(
            setup=("uv", "sync", "--dev"),
            start=("uv", "run", "link-checker", "."),
            test=("uv", "run", "pytest"),
        ),
    )
    monkeypatch.setattr(
        cli,
        "deliver_product_workspace",
        lambda _source, destination, **_kwargs: destination,
    )

    assert main([]) == 0

    brief = observed["task_brief"]
    options = observed["options"]
    assert brief.source_request == "Build a CLI that checks Markdown links."
    assert brief.title == "Link Checker"
    assert "broken links" in brief.acceptance_criteria[0].description
    assert options.source_repository == source
    assert options.model == "provider/model"
    assert options.policy == cli.DEFAULT_PRODUCT_POLICY
    assert options.quality_manifest == cli.DEFAULT_PRODUCT_PROFILE
    assert options.iteration_limit == 3
    output = capsys.readouterr().out
    assert "What would you like to build?" in output
    assert "task-management" not in output
    assert "Requirements summary" in output
    assert "Next commands" in output
    assert "link-checker ." in output
    assert str(tmp_path / "link-checker") in output


def test_guided_request_reprompts_invalid_unicode_and_treats_none_as_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    answers = iter(
        (
            "\udce5broken request",
            "Build a local timer",
            "yes",
            "Start and stop the timer",
            "none",
            "timer",
            "yes",
        )
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    collected = cli._collect_product_request(
        working_directory=tmp_path,
        run_id="sat-guided-unicode",
    )

    assert collected is not None
    brief, destination = collected
    assert brief.source_request == "Build a local timer"
    assert "none" not in brief.constraints
    assert destination == tmp_path / "timer"
    assert "Invalid terminal text in software request" in capsys.readouterr().out


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
                "--stage-timeout-seconds",
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
    assert second.input_cost_per_million_usd is None
    assert second.output_cost_per_million_usd is None
    assert second.verification_concurrency == first.verification_concurrency
    assert second.stage_timeout_seconds == first.stage_timeout_seconds
    output = capsys.readouterr().out
    assert "provider credentials: not stored by SAT" in output


def test_cli_interactive_configuration_prompts_for_first_run_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "config.json"
    answers = iter(("no", "provider/model"))
    monkeypatch.setenv("SAT_CONFIG_PATH", str(path))
    monkeypatch.setenv("SAT_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr(
        cli,
        "_inspect_selected_model",
        lambda *_args, **_kwargs: OpenClawModelInspection(
            model="provider/model",
            available=True,
        ),
    )

    assert main(["configure"]) == 0

    configuration = load_user_configuration(path)
    assert configuration is not None
    assert configuration.model == "provider/model"
    assert configuration.input_cost_per_million_usd is None
    assert configuration.output_cost_per_million_usd is None
    assert configuration.verification_concurrency == 1
    assert configuration.stage_timeout_seconds is None


def test_cli_requires_a_complete_price_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("SAT_CONFIG_PATH", str(tmp_path / "config.json"))

    assert (
        main(
            [
                "configure",
                "--non-interactive",
                "--model",
                "provider/model",
                "--input-cost-per-million-usd",
                "1",
            ]
        )
        == 1
    )
    assert "price flags must be supplied together" in capsys.readouterr().out


def test_first_run_model_setup_keeps_credentials_and_prices_outside_sat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "config.json"
    answers = iter(("no", "provider/model", "no"))
    monkeypatch.setenv("SAT_CONFIG_PATH", str(path))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr(
        cli,
        "_run_openclaw_configuration",
        lambda _binary, **_kwargs: pytest.fail(
            "OpenClaw setup should have been declined"
        ),
    )
    monkeypatch.setattr(
        cli,
        "_run_provider_smoke",
        lambda _binary, _model, **_kwargs: pytest.fail(
            "provider smoke should have been declined"
        ),
    )
    monkeypatch.setattr(
        cli,
        "_discover_openclaw_default_model",
        lambda _binary, **_kwargs: None,
    )
    monkeypatch.setattr(
        cli,
        "_inspect_selected_model",
        lambda *_args, **_kwargs: OpenClawModelInspection(
            model="provider/model",
            available=True,
        ),
    )

    state_paths = cli.ProductStatePaths.below(tmp_path / "state")
    cli.ensure_product_state(state_paths)

    configured = cli._ensure_product_configuration(state_paths)

    assert configured.model == "provider/model"
    assert configured.input_cost_per_million_usd is None
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 3
    assert "api_key" not in payload


def test_saved_model_is_rechecked_before_the_product_questions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    configuration_path = tmp_path / "config.json"
    monkeypatch.setenv("SAT_CONFIG_PATH", str(configuration_path))
    save_user_configuration(
        UserConfiguration(model="provider/missing-model"),
        configuration_path,
    )
    state_paths = cli.ProductStatePaths.below(tmp_path / "state")
    cli.ensure_product_state(state_paths)
    (state_paths.openclaw / "openclaw.json").write_text("{}", encoding="utf-8")
    answers = iter(
        (
            "no",
            "",
        )
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr(
        cli,
        "_discover_openclaw_default_model",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        cli,
        "_inspect_selected_model",
        lambda *_args, **_kwargs: OpenClawModelInspection(
            model="provider/missing-model",
            available=False,
            error="OpenClaw does not recognize the configured model",
        ),
    )

    with pytest.raises(
        cli.RuntimeConfigurationError,
        match="selected model is not locally ready",
    ):
        cli._ensure_product_configuration(state_paths)

    output = capsys.readouterr().out
    assert "Saved model is not locally ready" in output
    assert "First-run model setup" in output


def test_provider_smoke_uses_the_selected_model_without_exposing_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}
    monkeypatch.setenv("OPENCLAW_STATE_DIR", "/tmp/existing-openclaw")
    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", "/tmp/existing-openclaw/config.json")
    monkeypatch.setenv("OPENCLAW_AGENT_DIR", "/tmp/existing-openclaw/agent")
    monkeypatch.setenv("OPENCLAW_GATEWAY_URL", "ws://existing.example")
    monkeypatch.setenv("OPENAI_API_KEY", "trusted-provider-key")

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        observed["argv"] = argv
        observed["kwargs"] = kwargs
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"ok": True, "outputs": [{"text": '{"status":"ok"}'}]}),
        )

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    state = tmp_path / "sat-openclaw-state"
    state.mkdir()
    config = state / "openclaw.json"
    config.write_text("{}", encoding="utf-8")
    cli._run_provider_smoke(
        Path("/opt/openclaw"),
        "provider/model",
        state_dir=state,
        config_path=config,
    )

    argv = observed["argv"]
    assert argv[0] == "/opt/openclaw"
    assert argv[argv.index("--model") + 1] == "provider/model"
    assert observed["kwargs"]["shell"] is False
    assert observed["kwargs"]["capture_output"] is True
    assert observed["kwargs"]["env"]["OPENCLAW_STATE_DIR"] == str(state)
    assert observed["kwargs"]["env"]["OPENCLAW_CONFIG_PATH"] == str(config)
    assert observed["kwargs"]["env"]["OPENCLAW_AGENT_DIR"] == ""
    assert observed["kwargs"]["env"]["OPENCLAW_GATEWAY_URL"] == ""
    assert observed["kwargs"]["env"]["OPENCLAW_OAUTH_DIR"] == str(state / "credentials")
    assert observed["kwargs"]["env"]["OPENAI_API_KEY"] == "trusted-provider-key"


def test_provider_smoke_registers_the_deepseek_vision_compatibility_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        effective_path = Path(kwargs["env"]["OPENCLAW_CONFIG_PATH"])
        observed["argv"] = argv
        observed["effective_path"] = effective_path
        observed["payload"] = json.loads(effective_path.read_text(encoding="utf-8"))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"ok": True, "outputs": [{"text": '{"status":"ok"}'}]}),
        )

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    state = tmp_path / "state"
    state.mkdir()
    configured_path = state / "openclaw.json"
    configured_path.write_text("{}", encoding="utf-8")

    cli._run_provider_smoke(
        Path("/opt/openclaw"),
        "deepseek/deepseek-v4-flash-vision-exp",
        state_dir=state,
        config_path=configured_path,
    )

    payload = observed["payload"]
    assert payload["models"]["providers"]["deepseek"]["models"][0]["id"] == (
        "deepseek-v4-flash-vision-exp"
    )
    assert "apiKey" not in json.dumps(payload)
    assert observed["effective_path"] != configured_path
    assert observed["argv"][observed["argv"].index("--model") + 1] == (
        "deepseek/deepseek-v4-flash-vision-exp"
    )


def test_openclaw_configuration_has_no_hidden_setup_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        observed["argv"] = argv
        observed["kwargs"] = kwargs
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    state = Path("/tmp/sat-openclaw-state")
    config = state / "openclaw.json"
    cli._run_openclaw_configuration(
        Path("/opt/openclaw"),
        state_dir=state,
        config_path=config,
    )

    assert observed["argv"] == [
        "/opt/openclaw",
        "configure",
        "--section",
        "model",
    ]
    assert "timeout" not in observed["kwargs"]
    assert observed["kwargs"]["env"]["OPENCLAW_STATE_DIR"] == str(state)
    assert observed["kwargs"]["env"]["OPENCLAW_CONFIG_PATH"] == str(config)


def test_openclaw_default_model_is_discovered_without_a_provider_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        observed["argv"] = argv
        observed["kwargs"] = kwargs
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "resolvedDefault": "provider/detected-model",
                    "auth": {"private": "ignored"},
                }
            ),
        )

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    state = Path("/tmp/sat-openclaw-state")
    config = state / "openclaw.json"
    model = cli._discover_openclaw_default_model(
        Path("/opt/openclaw"),
        state_dir=state,
        config_path=config,
    )

    assert model == "provider/detected-model"
    assert observed["argv"] == [
        "/opt/openclaw",
        "models",
        "status",
        "--json",
    ]
    assert "--probe" not in observed["argv"]
    assert observed["kwargs"]["capture_output"] is True
    assert observed["kwargs"]["env"]["OPENCLAW_STATE_DIR"] == str(state)
    assert observed["kwargs"]["env"]["OPENCLAW_CONFIG_PATH"] == str(config)


def test_product_delivery_refuses_a_changed_final_report(tmp_path: Path) -> None:
    report = tmp_path / "final-report.json"
    report.write_text("{}\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="digest changed"):
        cli._load_final_report(report, expected_sha256="0" * 64)


def test_cli_deprecates_the_old_timeout_flag_and_can_restore_role_defaults(
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
                "provider/model",
                "--input-cost-per-million-usd",
                "1",
                "--output-cost-per-million-usd",
                "2",
                "--agent-timeout-seconds",
                "75",
            ]
        )
        == 0
    )
    configured = load_user_configuration(path)
    assert configured is not None
    assert configured.stage_timeout_seconds == 75
    assert "is deprecated" in capsys.readouterr().out

    assert main(["configure", "--non-interactive", "--use-role-timeouts"]) == 0
    configured = load_user_configuration(path)
    assert configured is not None
    assert configured.stage_timeout_seconds is None
    assert "role defaults" in capsys.readouterr().out


def test_cli_reports_v1_timeout_migration_without_reusing_the_old_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "model": "provider/model",
                "input_cost_per_million_usd": "1",
                "output_cost_per_million_usd": "2",
                "verification_concurrency": 2,
                "agent_timeout_seconds": 2400,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SAT_CONFIG_PATH", str(path))

    assert main(["configure", "--show"]) == 0

    output = capsys.readouterr().out
    assert "configuration migration:" in output
    assert "without its legacy" in output
    assert "role defaults" in output


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
    assert "quality_manifest=task_manager_phase1" in output
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
    monkeypatch.setenv("SAT_STATE_ROOT", str(tmp_path / "state"))
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
            openclaw_state_dir=str(kwargs["openclaw_state_dir"]),
            runtime_config=str(kwargs["runtime_config"]),
            sandbox_binary="/usr/bin/docker",
            sandbox_version="Docker version test",
            sandbox_image="sat-python-quality:phase1-v2",
            sandbox_image_id=f"sha256:{'a' * 64}",
            config_valid=True,
            sandbox_image_present=True,
            sandbox_container_ready=True,
            sandbox_container_error=None,
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
    monkeypatch.setenv("SAT_STATE_ROOT", str(tmp_path / "state"))
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

    def fake_materialize(*args: object, **_kwargs: object) -> Path:
        destination = args[1]
        assert isinstance(destination, Path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("{}", encoding="utf-8")
        return destination

    def fake_inspect_runtime(**kwargs: object) -> RuntimePreflight:
        observed["preflight_kwargs"] = kwargs
        return RuntimePreflight(
            openclaw_binary="/opt/openclaw",
            openclaw_version="OpenClaw test",
            openclaw_state_dir=str(kwargs["openclaw_state_dir"]),
            runtime_config=str(kwargs["runtime_config"]),
            sandbox_binary="/usr/bin/docker",
            sandbox_version="Docker version test",
            sandbox_image="sat-python-quality:phase1-v2",
            sandbox_image_id=f"sha256:{'a' * 64}",
            config_valid=True,
            sandbox_image_present=True,
            sandbox_container_ready=True,
            model="provider/model",
            model_available=True,
        )

    monkeypatch.setattr(cli, "WorkflowCoordinator", FakeCoordinator)
    monkeypatch.setattr(cli, "inspect_sandbox_image", fake_inspect_sandbox_image)
    monkeypatch.setattr(cli, "materialize_run_configuration", fake_materialize)
    monkeypatch.setattr(cli, "inspect_runtime_preflight", fake_inspect_runtime)
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
    assert observed["iteration_limit"] == 2
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
    runtime_workspace = tmp_path / "runtime-workspace"
    runtime_workspace.mkdir()
    runtime_run = tmp_path / "runtime-run"
    runtime_run.mkdir()
    observed["runtime_setup"](
        SimpleNamespace(workspace_path=str(runtime_workspace)),
        runtime_run,
    )
    assert observed["preflight_kwargs"]["expected_model"] == "provider/model"
    persisted = json.loads(
        (runtime_run / "runtime-preflight.json").read_text(encoding="utf-8")
    )
    assert persisted["model"] == "provider/model"
    assert persisted["model_available"] is True
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
    monkeypatch.setenv("SAT_STATE_ROOT", str(tmp_path / "state"))
    save_user_configuration(
        UserConfiguration(
            model="provider/saved-model",
            input_cost_per_million_usd="0.75",
            output_cost_per_million_usd="2.25",
            verification_concurrency=1,
            stage_timeout_seconds=1800,
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
    assert observed["iteration_limit"] == 2
    assert observed["stage_timeout_seconds"] == 1800
    assert observed["role_timeout_seconds"][AgentRole.PLANNER] == 180


def test_cli_run_rejects_changes_to_frozen_benchmark(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("SAT_STATE_ROOT", str(tmp_path / "state"))
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
