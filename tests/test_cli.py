"""Tests for the unified foundation CLI."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

import software_agent_team.cli as cli
from software_agent_team.artifacts import AgentRole, TaskBrief
from software_agent_team.benchmark_seed import prepare_benchmark_seed
from software_agent_team.cli import main
from software_agent_team.model_routing import ModelProfile
from software_agent_team.run_control import RunPhase
from software_agent_team.runtime_configuration import (
    OpenClawModelInspection,
    RuntimePreflight,
    SandboxImageInspection,
)
from software_agent_team.teams import AgentCapability, ModelRoutingMode
from software_agent_team.user_configuration import (
    UserConfiguration,
    load_user_configuration,
    save_user_configuration,
)

REPOSITORY_ROOT = Path(__file__).parents[1]


def test_replacement_planning_request_preserves_scope_and_versions_correction() -> None:
    original = cli.PlanningRequest(
        run_id="sat-original",
        project_name="link-checker",
        source_request="Build a Markdown link checker.",
        destination="/tmp/link-checker",
        execution_profile=("A small Python project.",),
        base_constraints=("Do not use network access.",),
        model="provider/model",
        authorization="user_confirmed",
        authorized_at=datetime(2026, 8, 27, 12, 0, tzinfo=UTC),
    )

    replacement = cli._replacement_planning_request(
        original,
        run_id="sat-replacement",
        correction_instruction="Support remote HTTPS links as well.",
    )

    assert replacement.run_id == "sat-replacement"
    assert replacement.source_request == original.source_request
    assert replacement.destination == original.destination
    assert replacement.base_constraints[:-1] == original.base_constraints
    assert "Support remote HTTPS links as well." in replacement.base_constraints[-1]


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
        lambda _state_paths: UserConfiguration(
            model="provider/model",
            progress_visibility="detailed",
        ),
    )
    source = tmp_path / "prepared-source"
    source.mkdir()
    monkeypatch.setattr(cli, "prepare_product_source", lambda **_kwargs: source)
    observed: dict[str, object] = {}
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    approved = SimpleNamespace(task_brief=SimpleNamespace(run_id="approved-run"))

    def fake_planning(request: object, **kwargs: object) -> object:
        observed["planning_request"] = request
        observed["planning_kwargs"] = kwargs
        return approved

    def fake_execute(
        supplied: object,
        options: object,
        **kwargs: object,
    ) -> SimpleNamespace:
        observed["approved"] = supplied
        observed["options"] = options
        observed["execution_kwargs"] = kwargs
        return SimpleNamespace(
            final_report=SimpleNamespace(path="final-report.json", sha256="a" * 64),
            control_stop=None,
            record=SimpleNamespace(
                run_id=observed["planning_request"].run_id,
                phase=RunPhase.COMPLETED,
                workspace=SimpleNamespace(workspace_path=str(workspace)),
                current_commit="a" * 40,
            ),
        )

    monkeypatch.setattr(cli, "_run_product_planning", fake_planning)
    monkeypatch.setattr(cli, "_execute_dynamic_workflow", fake_execute)
    monkeypatch.setattr(
        cli,
        "_execute_workflow",
        lambda *_args, **_kwargs: pytest.fail(
            "bare sat must not use the fixed evaluation workflow"
        ),
    )
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

    planning_request = observed["planning_request"]
    options = observed["options"]
    assert planning_request.source_request == (
        "Build a CLI that checks Markdown links."
    )
    assert planning_request.project_name == "link-checker"
    assert planning_request.authorization == "user_confirmed"
    assert planning_request.model == "provider/model"
    assert observed["approved"] is approved
    assert options.source_repository == source
    assert options.model == "provider/model"
    assert options.progress_handler.visibility is cli.RunEventVisibility.DETAILED
    assert options.policy == cli.DEFAULT_PRODUCT_POLICY
    assert options.quality_manifest == cli.DEFAULT_PRODUCT_PROFILE
    output = capsys.readouterr().out
    assert "What would you like to build?" in output
    assert "task-management" not in output
    assert "Planning authorization" in output
    assert "Next commands" in output
    assert "link-checker ." in output
    assert str(tmp_path / "link-checker") in output


def test_guided_request_reprompts_invalid_unicode_before_planning_authorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    answers = iter(
        (
            "\udce5broken request",
            "Build a local timer",
            "yes",
            "timer",
            "yes",
        )
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    collected = cli._collect_product_request(
        working_directory=tmp_path,
        run_id="sat-guided-unicode",
        configuration=UserConfiguration(model="provider/model"),
        execution_profile=("A new Python project.",),
        base_constraints=("No runtime network access.",),
    )

    assert collected is not None
    request, destination = collected
    assert request.source_request == "Build a local timer"
    assert request.base_constraints == ("No runtime network access.",)
    assert request.authorization == "user_confirmed"
    assert destination == tmp_path / "timer"
    assert "Invalid terminal text in software request" in capsys.readouterr().out


def test_product_planning_uses_one_bootstrap_agent_and_cleans_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_paths = cli.ProductStatePaths.below(tmp_path / "state")
    cli.ensure_product_state(state_paths)
    planning_workspace = tmp_path / "profile-seed"
    planning_workspace.mkdir()
    request = cli.PlanningRequest(
        run_id="sat-product-planning",
        project_name="link-checker",
        source_request="Build a Markdown link checker.",
        destination=str(tmp_path / "link-checker"),
        execution_profile=("A new Python project.",),
        base_constraints=("No runtime network access.",),
        model="provider/model",
        authorization="user_confirmed",
        authorized_at=datetime(2026, 8, 26, 12, 0, tzinfo=UTC),
    )
    quality = cli.load_quality_gate_configuration(
        cli.DEFAULT_PRODUCT_POLICY,
        cli.DEFAULT_PRODUCT_PROFILE,
    )
    configuration = UserConfiguration(
        model="provider/model",
        max_concurrency=4,
    )
    observed: dict[str, object] = {}
    runtime_paths: list[Path] = []
    approved = object()

    monkeypatch.setattr(
        cli,
        "inspect_sandbox_image",
        lambda **kwargs: SandboxImageInspection(
            sandbox_binary="/usr/bin/docker",
            sandbox_version="Docker version test",
            sandbox_image=str(kwargs["sandbox_image"]),
            sandbox_image_id=f"sha256:{'a' * 64}",
            sandbox_image_present=True,
        ),
    )

    def fake_materialize(*args: object, **kwargs: object) -> Path:
        destination = args[1]
        assert isinstance(destination, Path)
        runtime_paths.append(destination)
        observed["materialize"] = kwargs
        destination.write_text("{}\n", encoding="utf-8")
        return destination

    monkeypatch.setattr(cli, "materialize_run_configuration", fake_materialize)
    monkeypatch.setattr(
        cli,
        "inspect_runtime_preflight",
        lambda **kwargs: RuntimePreflight(
            openclaw_binary="/opt/openclaw",
            openclaw_version="OpenClaw test",
            openclaw_state_dir=str(kwargs["openclaw_state_dir"]),
            runtime_config=str(kwargs["runtime_config"]),
            sandbox_binary="/usr/bin/docker",
            sandbox_version="Docker version test",
            sandbox_image=quality.policy.sandbox.image,
            sandbox_image_id=f"sha256:{'a' * 64}",
            config_valid=True,
            sandbox_image_present=True,
            sandbox_container_ready=True,
            model="provider/model",
            model_available=True,
        ),
    )

    def fake_interactive(coordinator: object, supplied: object) -> object:
        observed["coordinator"] = coordinator
        observed["request"] = supplied
        return approved

    monkeypatch.setattr(cli, "run_interactive_planning", fake_interactive)
    cleanup_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        cli,
        "cleanup_run_sandbox_containers",
        lambda **kwargs: cleanup_calls.append(kwargs) or SimpleNamespace(removed=()),
    )

    result = cli._run_product_planning(
        request,
        source_repository=planning_workspace,
        state_paths=state_paths,
        quality=quality,
        configuration=configuration,
    )

    assert result is approved
    assert observed["request"] == request
    materialize = observed["materialize"]
    assert materialize["bootstrap_capability"] is cli.AgentCapability.CLARIFICATION
    assert materialize["workspace"] == planning_workspace
    assert materialize["model"] == "provider/model"
    coordinator = observed["coordinator"]
    assert coordinator.store.root == state_paths.planning
    assert coordinator.policy.max_concurrency == 4
    assert coordinator.policy.max_review_agents == 1
    assert coordinator.policy.require_review_agent
    assert coordinator.policy.planning_timeout_seconds == 180
    implementation_timeout = coordinator.policy.capability_timeouts[
        cli.AgentCapability.IMPLEMENTATION
    ]
    testing_timeout = coordinator.policy.capability_timeouts[
        cli.AgentCapability.TESTING
    ]
    assert (
        implementation_timeout.default_seconds,
        implementation_timeout.ceiling_seconds,
    ) == (900, 1800)
    assert (testing_timeout.default_seconds, testing_timeout.ceiling_seconds) == (
        300,
        600,
    )
    assert {item.id for item in coordinator.policy.profile_acceptance_criteria} == {
        "AC_REQUEST",
        "AC_RUNNABLE",
        "AC_TESTS",
        "AC_QUALITY",
        "AC_DOCUMENTATION",
    }
    assert cleanup_calls == [
        {
            "sandbox_binary": "docker",
            "run_id": request.run_id,
            "openclaw_state_dir": state_paths.openclaw,
            "workspace_dir": planning_workspace,
            "iteration_limit": 1,
            "roles": (AgentRole.CLARIFIER,),
        }
    ]
    assert len(runtime_paths) == 1
    assert not runtime_paths[0].exists()


def test_product_global_timeout_override_is_an_exact_controller_policy() -> None:
    quality = cli.load_quality_gate_configuration(
        cli.DEFAULT_PRODUCT_POLICY,
        cli.DEFAULT_PRODUCT_PROFILE,
    )

    policy = cli._product_planning_policy(
        quality,
        UserConfiguration(
            model="provider/model",
            stage_timeout_seconds=1200,
        ),
    )

    assert policy.planning_timeout_seconds == 1200
    assert {
        (timeout.default_seconds, timeout.ceiling_seconds)
        for timeout in policy.capability_timeouts.values()
    } == {(1200, 1200)}


def test_dynamic_product_launch_uses_approved_agents_and_manual_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = SimpleNamespace(
        model="provider/model",
        input_cost_per_million_usd=None,
        output_cost_per_million_usd=None,
    )
    agents = (SimpleNamespace(id="builder"), SimpleNamespace(id="reviewer"))
    team_plan = SimpleNamespace(
        model_routes=SimpleNamespace(routes=(route,)),
        agents=agents,
        iteration_limit=2,
    )
    task_brief = SimpleNamespace(
        run_id="sat-dynamic-product",
        acceptance_criteria=(
            SimpleNamespace(id="AC_USER"),
            SimpleNamespace(id="AC_TESTS"),
        ),
    )
    approved = SimpleNamespace(task_brief=task_brief, team_plan=team_plan)
    options = cli._AdaptiveWorkflowLaunchOptions(
        source_repository=tmp_path / "source",
        base_ref="HEAD",
        teams=cli.DEFAULT_TEAM_CONFIG,
        openclaw=cli.DEFAULT_OPENCLAW_CONFIG,
        policy=cli.DEFAULT_PRODUCT_POLICY,
        quality_manifest=cli.DEFAULT_PRODUCT_PROFILE,
        runs_root=tmp_path / "runs",
        workspaces_root=tmp_path / "workspaces",
        openclaw_binary=Path("/opt/openclaw"),
        openclaw_state_dir=tmp_path / "openclaw",
        sandbox_binary="docker",
        model="provider/model",
        input_cost_per_million_usd=None,
        output_cost_per_million_usd=None,
    )
    observed: dict[str, object] = {}
    boundary = SimpleNamespace(
        executor=object(),
        quality_gate_factory=object(),
        runtime_setup=object(),
    )

    def fake_boundary(**kwargs: object) -> object:
        observed["boundary"] = kwargs
        return boundary

    class FakeDynamicCoordinator:
        def __init__(self, **kwargs: object) -> None:
            observed["coordinator"] = kwargs

        def execute(self, supplied: object, **kwargs: object) -> object:
            observed["approved"] = supplied
            observed["execute"] = kwargs
            return "dynamic-outcome"

    monkeypatch.setattr(cli, "_prepare_runtime_boundary", fake_boundary)
    monkeypatch.setattr(cli, "DynamicWorkflowCoordinator", FakeDynamicCoordinator)
    cleanup_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        cli,
        "cleanup_run_sandbox_containers",
        lambda **kwargs: cleanup_calls.append(kwargs) or SimpleNamespace(removed=()),
    )

    outcome = cli._execute_dynamic_workflow(approved, options)

    assert outcome == "dynamic-outcome"
    assert observed["boundary"]["team_plan"] is team_plan
    coordinator = observed["coordinator"]
    assert coordinator["manual_review_criteria"] == ("AC_USER", "AC_TESTS")
    assert set(coordinator["pricing_by_model"]) == {"provider/model"}
    assert observed["approved"] is approved
    assert observed["execute"] == {
        "source_repository": options.source_repository,
        "base_ref": "HEAD",
    }
    assert cleanup_calls == [
        {
            "sandbox_binary": "docker",
            "run_id": task_brief.run_id,
            "openclaw_state_dir": options.openclaw_state_dir,
            "workspace_dir": (options.workspaces_root / task_brief.run_id).resolve(
                strict=False
            ),
            "iteration_limit": 2,
            "agents": agents,
        }
    ]


def test_dynamic_runtime_preflight_checks_every_approved_model_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route_values = (
        SimpleNamespace(model="provider/default"),
        SimpleNamespace(model="provider/quality"),
    )

    class Routes:
        default_route_id = "default"
        routes = route_values

        @staticmethod
        def get_route(route_id: str) -> SimpleNamespace:
            return route_values[0] if route_id == "default" else route_values[1]

    team_plan = SimpleNamespace(model_routes=Routes())
    options = cli._AdaptiveWorkflowLaunchOptions(
        source_repository=tmp_path / "source",
        base_ref="HEAD",
        teams=cli.DEFAULT_TEAM_CONFIG,
        openclaw=cli.DEFAULT_OPENCLAW_CONFIG,
        policy=cli.DEFAULT_PRODUCT_POLICY,
        quality_manifest=cli.DEFAULT_PRODUCT_PROFILE,
        runs_root=tmp_path / "runs",
        workspaces_root=tmp_path / "workspaces",
        openclaw_binary=Path("/opt/openclaw"),
        openclaw_state_dir=tmp_path / "openclaw",
        sandbox_binary="docker",
        model="provider/default",
        input_cost_per_million_usd=None,
        output_cost_per_million_usd=None,
    )
    monkeypatch.setattr(
        cli,
        "inspect_sandbox_image",
        lambda **_kwargs: SandboxImageInspection(
            sandbox_binary="/usr/bin/docker",
            sandbox_version="Docker version test",
            sandbox_image="sat-python-quality:phase1-v5",
            sandbox_image_id=f"sha256:{'a' * 64}",
            sandbox_image_present=True,
        ),
    )

    def materialize(*args: object, **_kwargs: object) -> Path:
        destination = args[1]
        assert isinstance(destination, Path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("{}", encoding="utf-8")
        return destination

    monkeypatch.setattr(cli, "materialize_run_configuration", materialize)
    monkeypatch.setattr(
        cli,
        "inspect_runtime_preflight",
        lambda **kwargs: RuntimePreflight(
            openclaw_binary="/opt/openclaw",
            openclaw_version="OpenClaw test",
            openclaw_state_dir=str(kwargs["openclaw_state_dir"]),
            runtime_config=str(kwargs["runtime_config"]),
            sandbox_binary="/usr/bin/docker",
            sandbox_version="Docker version test",
            sandbox_image="sat-python-quality:phase1-v5",
            sandbox_image_id=f"sha256:{'a' * 64}",
            config_valid=True,
            sandbox_image_present=True,
            sandbox_container_ready=True,
            model="provider/default",
            model_available=True,
        ),
    )
    inspected: list[str] = []

    def inspect_model(**kwargs: object) -> OpenClawModelInspection:
        model = str(kwargs["model"])
        inspected.append(model)
        return OpenClawModelInspection(model=model, available=True)

    monkeypatch.setattr(cli, "inspect_openclaw_model", inspect_model)
    boundary = cli._prepare_runtime_boundary(
        run_id="sat-routing-preflight",
        options=options,
        team_plan=team_plan,
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_directory = options.runs_root / "sat-routing-preflight"
    run_directory.mkdir(parents=True)

    boundary.runtime_setup(
        SimpleNamespace(workspace_path=str(workspace)),
        run_directory,
    )

    assert inspected == ["provider/quality"]
    evidence = json.loads(
        (run_directory / "runtime-preflight.json").read_text(encoding="utf-8")
    )
    assert [item["model"] for item in evidence["model_inspections"]] == [
        "provider/default",
        "provider/quality",
    ]


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
                "--max-concurrency",
                "4",
                "--progress-visibility",
                "detailed",
                "--stage-timeout-seconds",
                "1200",
            ]
        )
        == 0
    )
    first = load_user_configuration(path)
    assert first is not None
    assert first.model == "provider/model-a"
    assert first.max_concurrency == 4
    assert first.progress_visibility == "detailed"

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
    assert second.max_concurrency == first.max_concurrency
    assert second.progress_visibility == first.progress_visibility
    assert second.stage_timeout_seconds == first.stage_timeout_seconds
    output = capsys.readouterr().out
    assert "provider credentials: not stored by SAT" in output


def test_cli_configures_auditable_adaptive_model_profiles(
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
                "provider/default",
            ]
        )
        == 0
    )

    assert (
        main(
            [
                "configure",
                "--non-interactive",
                "--add-model-profile",
                "quality=provider/quality",
                "--profile-capabilities",
                "quality=testing,review",
                "--profile-priority",
                "quality=10",
                "--profile-pricing",
                "quality=0.25,1.00",
                "--routing-mode",
                "policy",
                "--route-capability",
                "testing=quality",
                "--allow-provider-switch",
                "--max-model-switches",
                "1",
            ]
        )
        == 0
    )

    configured = load_user_configuration(path)
    assert configured is not None
    assert configured.routing_mode.value == "policy"
    assert tuple(profile.id for profile in configured.model_profiles) == (
        "default",
        "quality",
    )
    assert configured.capability_profile_overrides[AgentCapability.TESTING] == "quality"
    assert configured.max_model_switches_per_agent == 1
    assert configured.model_profiles[1].input_cost_per_million_usd == Decimal("0.25")

    assert main(["configure", "--show"]) == 0
    output = capsys.readouterr().out
    assert "model routing: policy" in output
    assert "quality: provider/quality" in output
    assert "testing: quality" in output
    assert "provider_failure" in output


def test_cli_can_return_adaptive_routing_to_one_strict_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "config.json"
    monkeypatch.setenv("SAT_CONFIG_PATH", str(path))
    save_user_configuration(
        UserConfiguration.model_validate(
            {
                "model_profiles": (
                    {
                        "id": "default",
                        "model": "provider/default",
                        "capabilities": ("clarification", "planning"),
                    },
                    {
                        "id": "quality",
                        "model": "provider/quality",
                        "capabilities": ("testing", "review"),
                    },
                ),
                "routing_mode": "policy",
                "default_model_profile_id": "default",
            }
        ),
        path,
    )

    assert (
        main(
            [
                "configure",
                "--non-interactive",
                "--clear-model-routing",
            ]
        )
        == 0
    )

    configured = load_user_configuration(path)
    assert configured is not None
    assert configured.routing_mode.value == "strict"
    assert tuple(profile.id for profile in configured.model_profiles) == ("default",)
    assert configured.default_model_profile.capabilities == tuple(AgentCapability)


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
    assert configuration.max_concurrency == 2
    assert configuration.progress_visibility == "standard"
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
    assert payload["schema_version"] == 6
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
    assert "Saved bootstrap model is not locally ready" in output
    assert "First-run model setup" in output


def test_unavailable_optional_model_profile_warns_without_resetting_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    configuration_path = tmp_path / "config.json"
    monkeypatch.setenv("SAT_CONFIG_PATH", str(configuration_path))
    capabilities = tuple(AgentCapability)
    configured = UserConfiguration(
        model_profiles=(
            ModelProfile(
                id="default",
                model="provider/default",
                capabilities=capabilities,
            ),
            ModelProfile(
                id="optional",
                model="provider/optional",
                capabilities=(AgentCapability.TESTING,),
            ),
        ),
        routing_mode=ModelRoutingMode.POLICY,
    )
    save_user_configuration(configured, configuration_path)
    state_paths = cli.ProductStatePaths.below(tmp_path / "state")
    cli.ensure_product_state(state_paths)
    (state_paths.openclaw / "openclaw.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        cli,
        "_inspect_selected_model",
        lambda _binary, model, **_kwargs: OpenClawModelInspection(
            model=model,
            available=model == "provider/default",
            error=None if model == "provider/default" else "route unavailable",
        ),
    )
    monkeypatch.setattr(
        "builtins.input",
        lambda _prompt: pytest.fail("optional profile must not restart setup"),
    )

    result = cli._ensure_product_configuration(state_paths)

    assert result == configured
    assert load_user_configuration(configuration_path) == configured
    output = capsys.readouterr().out
    assert "Optional model profiles are not locally ready" in output
    assert "provider/optional: route unavailable" in output
    assert "First-run model setup" not in output


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
            sandbox_image="sat-python-quality:phase1-v5",
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
    cleanup_calls: list[dict[str, object]] = []

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
            sandbox_image="sat-python-quality:phase1-v5",
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
    monkeypatch.setattr(
        cli,
        "cleanup_run_sandbox_containers",
        lambda **kwargs: cleanup_calls.append(kwargs) or SimpleNamespace(removed=()),
    )
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
    assert cleanup_calls == [
        {
            "sandbox_binary": "docker",
            "run_id": "task-manager-trial-2",
            "openclaw_state_dir": tmp_path / "state" / "openclaw",
            "workspace_dir": (tmp_path / "workspaces" / "task-manager-trial-2"),
            "iteration_limit": 2,
            "roles": (
                AgentRole.PLANNER,
                AgentRole.GENERALIST_DEVELOPER,
                AgentRole.TESTER,
                AgentRole.REVIEWER,
            ),
        }
    ]
    gate_factory = observed["quality_gate_factory"]
    assert callable(gate_factory)
    run_directory = tmp_path / "gate-run"
    workspace = tmp_path / "gate-workspace"
    run_directory.mkdir()
    workspace.mkdir()
    gate_runner = gate_factory(run_directory, workspace, lambda _event: None)
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


def test_execute_workflow_cleans_run_sandboxes_after_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    state = tmp_path / "state" / "openclaw"
    state.mkdir(parents=True)
    cleanup_calls: list[dict[str, object]] = []

    class InterruptedCoordinator:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def execute(self, *_args: object, **_kwargs: object) -> object:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli, "WorkflowCoordinator", InterruptedCoordinator)
    monkeypatch.setattr(
        cli,
        "inspect_sandbox_image",
        lambda **kwargs: SandboxImageInspection(
            sandbox_binary="/usr/bin/docker",
            sandbox_version="Docker version test",
            sandbox_image=str(kwargs["sandbox_image"]),
            sandbox_image_id=f"sha256:{'a' * 64}",
            sandbox_image_present=True,
        ),
    )
    monkeypatch.setattr(
        cli,
        "cleanup_run_sandbox_containers",
        lambda **kwargs: cleanup_calls.append(kwargs) or SimpleNamespace(removed=()),
    )
    task_brief = TaskBrief.model_validate_json(
        (REPOSITORY_ROOT / "benchmarks/task_manager/task-brief.json").read_text(
            encoding="utf-8"
        )
    )
    workspaces = tmp_path / "workspaces"
    options = cli._WorkflowLaunchOptions(
        source_repository=source,
        base_ref="HEAD",
        teams=REPOSITORY_ROOT / "configs/teams.json",
        openclaw=REPOSITORY_ROOT / "configs/openclaw.example.json5",
        policy=REPOSITORY_ROOT / "configs/run-policy.json",
        quality_manifest=REPOSITORY_ROOT / "benchmarks/task_manager/benchmark.json",
        runs_root=tmp_path / "runs",
        workspaces_root=workspaces,
        openclaw_binary=Path("/opt/openclaw"),
        openclaw_state_dir=state,
        sandbox_binary="docker",
        model="provider/model",
        input_cost_per_million_usd=None,
        output_cost_per_million_usd=None,
        stage_timeout_seconds=None,
        artifact_repair_limit=1,
        iteration_limit=2,
        verification_concurrency=1,
    )

    with pytest.raises(KeyboardInterrupt):
        cli._execute_workflow(task_brief, options)

    assert cleanup_calls == [
        {
            "sandbox_binary": "docker",
            "run_id": task_brief.run_id,
            "openclaw_state_dir": state,
            "workspace_dir": (workspaces / task_brief.run_id).resolve(strict=False),
            "iteration_limit": 2,
            "roles": (
                AgentRole.PLANNER,
                AgentRole.GENERALIST_DEVELOPER,
                AgentRole.TESTER,
                AgentRole.REVIEWER,
            ),
        }
    ]


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
            max_concurrency=1,
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
    monkeypatch.setattr(
        cli,
        "cleanup_run_sandbox_containers",
        lambda **_kwargs: SimpleNamespace(removed=()),
    )

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
