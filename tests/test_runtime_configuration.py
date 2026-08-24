"""Tests for run-scoped OpenClaw configuration and offline preflight."""

import json
import os
from pathlib import Path

import pytest

import software_agent_team.runtime_configuration as runtime_configuration
from software_agent_team.configuration import READ_ONLY_ROLES, WRITE_ROLES
from software_agent_team.runtime_configuration import (
    RuntimeConfigurationError,
    inspect_runtime_preflight,
    materialize_run_configuration,
    persist_runtime_preflight,
    probe_sandbox_runtime,
)
from software_agent_team.teams import load_team_manifest

REPOSITORY_ROOT = Path(__file__).parents[1]
TEAM_CONFIG = REPOSITORY_ROOT / "configs" / "teams.json"
OPENCLAW_TEMPLATE = REPOSITORY_ROOT / "configs" / "openclaw.example.json5"


def test_materialized_config_binds_every_role_to_one_run_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    destination = tmp_path / "run" / "openclaw.runtime.json"

    materialize_run_configuration(
        OPENCLAW_TEMPLATE,
        destination,
        manifest=load_team_manifest(TEAM_CONFIG),
        workspace=workspace,
        sandbox_image="sat-agent:phase1",
        sandbox_user="1000:1000",
        model="provider/model",
    )

    payload = json.loads(destination.read_text(encoding="utf-8"))
    defaults = payload["agents"]["defaults"]
    assert defaults["repoRoot"] == str(workspace.resolve())
    assert defaults["skipBootstrap"] is True
    assert defaults["skills"] == []
    assert defaults["model"] == {
        "primary": "provider/model",
        "fallbacks": [],
    }
    assert defaults["sandbox"]["scope"] == "session"
    assert defaults["sandbox"]["docker"] == {
        "image": "sat-agent:phase1",
        "network": "none",
        "readOnlyRoot": True,
        "capDrop": ["ALL"],
        "user": "1000:1000",
        "env": {
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "HOME": "/tmp",
            "LANG": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
            "RUFF_CACHE_DIR": "/tmp/ruff-cache",
            "TMPDIR": "/tmp",
            "XDG_CACHE_HOME": "/tmp/cache",
            "XDG_CONFIG_HOME": "/tmp/config",
        },
        "pidsLimit": 128,
        "memory": "512m",
        "memorySwap": "512m",
        "cpus": 1.0,
        "tmpfs": [
            "/tmp:rw,nosuid,nodev,size=128m",
            "/var/tmp:rw,nosuid,nodev,size=32m",
            "/run:rw,nosuid,nodev,size=16m",
        ],
        "ulimits": {
            "nofile": {"soft": 1024, "hard": 1024},
        },
    }
    agents = {item["id"]: item for item in payload["agents"]["list"]}
    assert {item["workspace"] for item in agents.values()} == {str(workspace.resolve())}
    for role in READ_ONLY_ROLES:
        assert (
            agents[role.value].get("sandbox", {}).get("workspaceAccess", "ro") == "ro"
        )
    for role in WRITE_ROLES:
        assert agents[role.value]["sandbox"]["workspaceAccess"] == "rw"
    assert destination.stat().st_mode & 0o777 == 0o600


def test_materialization_is_write_once_and_rejects_missing_workspace(
    tmp_path: Path,
) -> None:
    manifest = load_team_manifest(TEAM_CONFIG)
    destination = tmp_path / "run" / "openclaw.runtime.json"

    with pytest.raises(RuntimeConfigurationError, match="does not exist"):
        materialize_run_configuration(
            OPENCLAW_TEMPLATE,
            destination,
            manifest=manifest,
            workspace=tmp_path / "missing",
            sandbox_image="sat-agent:phase1",
            sandbox_user="1000:1000",
        )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    materialize_run_configuration(
        OPENCLAW_TEMPLATE,
        destination,
        manifest=manifest,
        workspace=workspace,
        sandbox_image="sat-agent:phase1",
        sandbox_user="1000:1000",
    )
    before = destination.read_bytes()
    with pytest.raises(RuntimeConfigurationError, match="already exists"):
        materialize_run_configuration(
            OPENCLAW_TEMPLATE,
            destination,
            manifest=manifest,
            workspace=workspace,
            sandbox_image="sat-agent:phase1",
            sandbox_user="1000:1000",
        )
    assert destination.read_bytes() == before


def test_materialization_rejects_root_host_user(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(RuntimeConfigurationError, match="unprivileged host user"):
        materialize_run_configuration(
            OPENCLAW_TEMPLATE,
            tmp_path / "runtime.json",
            manifest=load_team_manifest(TEAM_CONFIG),
            workspace=workspace,
            sandbox_image="sat-agent:phase1",
            sandbox_user="0:0",
        )


def test_preflight_executes_explicit_commands_without_model_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    openclaw = tmp_path / "openclaw"
    openclaw.write_text("binary", encoding="utf-8")
    openclaw.chmod(0o755)
    config = tmp_path / "runtime.json"
    config.write_text("{}", encoding="utf-8")
    state = tmp_path / "sat-state/openclaw"
    state.mkdir(parents=True)
    original_state = tmp_path / "existing-openclaw"
    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(original_state))
    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(original_state / "openclaw.json"))
    monkeypatch.setenv("OPENCLAW_AGENT_DIR", str(original_state / "agent"))
    calls: list[list[str]] = []

    class Result:
        def __init__(self, returncode: int, stdout: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = ""

    def fake_run(argv: list[str], **kwargs: object) -> Result:
        assert "shell" not in kwargs
        environment = kwargs["env"]
        assert environment["OPENCLAW_STATE_DIR"] == str(state)
        assert environment["OPENCLAW_CONFIG_PATH"] == str(config)
        assert environment["OPENCLAW_AGENT_DIR"] == ""
        calls.append(argv)
        if argv[-1] == "--version":
            name = (
                "Docker version test" if argv[0] == "/bin/docker" else "OpenClaw test"
            )
            return Result(0, name)
        if argv[1:3] == ["image", "inspect"]:
            return Result(0, f"sha256:{'a' * 64}")
        if argv[1] == "run":
            return Result(0, "b" * 64)
        if argv[1] == "exec":
            return Result(0)
        if argv[1:3] == ["container", "inspect"]:
            return Result(
                0,
                json.dumps(
                    {
                        "Status": "running",
                        "Running": True,
                        "ExitCode": 0,
                        "OOMKilled": False,
                        "Error": "",
                    }
                ),
            )
        if argv[1:3] == ["container", "rm"]:
            return Result(0, "removed")
        return Result(0, "{}")

    monkeypatch.setattr(runtime_configuration.shutil, "which", lambda _: "/bin/docker")
    monkeypatch.setattr(runtime_configuration.subprocess, "run", fake_run)
    monkeypatch.setattr(runtime_configuration.time, "sleep", lambda _: None)

    result = inspect_runtime_preflight(
        openclaw_binary=openclaw,
        openclaw_state_dir=state,
        runtime_config=config,
        sandbox_binary="docker",
        sandbox_image="sat-agent:phase1",
    )

    assert result.ready
    assert calls[:4] == [
        [str(openclaw), "--version"],
        [str(openclaw), "config", "validate", "--json"],
        ["/bin/docker", "--version"],
        [
            "/bin/docker",
            "image",
            "inspect",
            "--format",
            "{{.Id}}",
            "sat-agent:phase1",
        ],
    ]
    assert calls[4][0:4] == [
        "/bin/docker",
        "run",
        "--detach",
        "--name",
    ]
    probe_name = calls[4][4]
    assert probe_name.startswith("sat-runtime-probe-")
    assert calls[4][-3:] == [f"sha256:{'a' * 64}", "sleep", "infinity"]
    assert "nproc" not in " ".join(calls[4])
    assert calls[5][0:4] == [
        "/bin/docker",
        "exec",
        "--workdir",
        "/workspace",
    ]
    assert calls[6] == [
        "/bin/docker",
        "container",
        "inspect",
        "--format",
        "{{json .State}}",
        probe_name,
    ]
    assert calls[7] == [
        "/bin/docker",
        "container",
        "rm",
        "--force",
        probe_name,
    ]
    assert result.sandbox_image_id == f"sha256:{'a' * 64}"
    assert result.sandbox_container_ready is True
    assert result.sandbox_container_error is None
    assert os.environ["OPENCLAW_STATE_DIR"] == str(original_state)
    assert os.environ["OPENCLAW_CONFIG_PATH"] == str(original_state / "openclaw.json")
    assert os.environ["OPENCLAW_AGENT_DIR"] == str(original_state / "agent")

    evidence = tmp_path / "run" / "runtime-preflight.json"
    persist_runtime_preflight(result, evidence)
    persisted = json.loads(evidence.read_text(encoding="utf-8"))
    assert persisted["config_valid"] is True
    assert persisted["openclaw_state_dir"] == str(state)
    assert persisted["sandbox_image_id"] == f"sha256:{'a' * 64}"
    assert persisted["sandbox_container_ready"] is True
    with pytest.raises(RuntimeConfigurationError, match="already exists"):
        persist_runtime_preflight(result, evidence)


def test_preflight_rejects_invalid_docker_image_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    openclaw = tmp_path / "openclaw"
    openclaw.write_text("binary", encoding="utf-8")
    openclaw.chmod(0o755)
    config = tmp_path / "runtime.json"
    config.write_text("{}", encoding="utf-8")
    state = tmp_path / "sat-state/openclaw"
    state.mkdir(parents=True)

    class Result:
        def __init__(self, stdout: str) -> None:
            self.returncode = 0
            self.stdout = stdout
            self.stderr = ""

    def fake_run(argv: list[str], **kwargs: object) -> Result:
        if argv[-1] == "--version":
            return Result(
                "Docker version test" if argv[0] == "/bin/docker" else "OpenClaw test"
            )
        if "inspect" in argv:
            return Result("mutable-image-tag")
        return Result("{}")

    monkeypatch.setattr(runtime_configuration.shutil, "which", lambda _: "/bin/docker")
    monkeypatch.setattr(runtime_configuration.subprocess, "run", fake_run)

    with pytest.raises(RuntimeConfigurationError, match="invalid sandbox image ID"):
        inspect_runtime_preflight(
            openclaw_binary=openclaw,
            openclaw_state_dir=state,
            runtime_config=config,
            sandbox_binary="docker",
            sandbox_image="sat-agent:phase1",
        )


def test_preflight_rejects_a_changed_frozen_image_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    openclaw = tmp_path / "openclaw"
    openclaw.write_text("binary", encoding="utf-8")
    openclaw.chmod(0o755)
    config = tmp_path / "runtime.json"
    config.write_text("{}", encoding="utf-8")
    state = tmp_path / "sat-state/openclaw"
    state.mkdir(parents=True)

    class Result:
        returncode = 0
        stderr = ""

        def __init__(self, stdout: str) -> None:
            self.stdout = stdout

    def fake_run(argv: list[str], **kwargs: object) -> Result:
        if argv[-1] == "--version":
            return Result(
                "Docker version test" if argv[0] == "/bin/docker" else "OpenClaw test"
            )
        if "inspect" in argv:
            return Result(f"sha256:{'b' * 64}")
        return Result("{}")

    monkeypatch.setattr(runtime_configuration.shutil, "which", lambda _: "/bin/docker")
    monkeypatch.setattr(runtime_configuration.subprocess, "run", fake_run)

    with pytest.raises(RuntimeConfigurationError, match="identity changed"):
        inspect_runtime_preflight(
            openclaw_binary=openclaw,
            openclaw_state_dir=state,
            runtime_config=config,
            sandbox_binary="docker",
            sandbox_image="sat-agent:phase1",
            expected_sandbox_image_id=f"sha256:{'a' * 64}",
        )


def test_preflight_rejects_an_image_whose_container_exits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    openclaw = tmp_path / "openclaw"
    openclaw.write_text("binary", encoding="utf-8")
    openclaw.chmod(0o755)
    config = tmp_path / "runtime.json"
    config.write_text("{}", encoding="utf-8")
    state = tmp_path / "sat-state/openclaw"
    state.mkdir(parents=True)

    class Result:
        def __init__(
            self,
            returncode: int = 0,
            stdout: str = "",
        ) -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = ""

    def fake_run(argv: list[str], **_kwargs: object) -> Result:
        if argv[-1] == "--version":
            return Result(
                stdout=(
                    "Docker version test"
                    if argv[0] == "/bin/docker"
                    else "OpenClaw test"
                )
            )
        if argv[1:3] == ["image", "inspect"]:
            return Result(stdout=f"sha256:{'a' * 64}")
        if argv[1] == "run":
            return Result(stdout="b" * 64)
        if argv[1] == "exec":
            return Result()
        if argv[1:3] == ["container", "inspect"]:
            return Result(
                stdout=json.dumps(
                    {
                        "Status": "exited",
                        "Running": False,
                        "ExitCode": 0,
                        "OOMKilled": False,
                        "Error": "",
                    }
                )
            )
        return Result(stdout="{}")

    monkeypatch.setattr(runtime_configuration.shutil, "which", lambda _: "/bin/docker")
    monkeypatch.setattr(runtime_configuration.subprocess, "run", fake_run)
    monkeypatch.setattr(runtime_configuration.time, "sleep", lambda _: None)

    result = inspect_runtime_preflight(
        openclaw_binary=openclaw,
        openclaw_state_dir=state,
        runtime_config=config,
        sandbox_binary="docker",
        sandbox_image="sat-agent:phase1",
    )

    assert not result.ready
    assert result.sandbox_image_present is True
    assert result.sandbox_container_ready is False
    assert result.sandbox_container_error == (
        "sandbox probe exited before tool execution "
        "(status=exited, exit_code=0, oom_killed=false)"
    )


def test_runtime_probe_attempts_cleanup_after_docker_start_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    class Result:
        stdout = ""
        stderr = ""

        def __init__(self, returncode: int) -> None:
            self.returncode = returncode

    def fake_run(argv: list[str], **_kwargs: object) -> Result:
        calls.append(argv)
        return Result(125 if argv[1] == "run" else 1)

    monkeypatch.setattr(runtime_configuration.shutil, "which", lambda _: "/bin/docker")
    monkeypatch.setattr(runtime_configuration.subprocess, "run", fake_run)

    probe = probe_sandbox_runtime(
        sandbox_binary="docker",
        sandbox_image_id=f"sha256:{'a' * 64}",
        settle_seconds=0,
    )

    assert not probe.ready
    assert probe.error == "Docker could not start the sandbox probe (exit code 125)"
    assert calls[0][1] == "run"
    assert calls[1][1:4] == ["container", "rm", "--force"]


def test_runtime_probe_rejects_a_running_container_without_tool_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    class Result:
        stderr = ""

        def __init__(self, returncode: int = 0, stdout: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout

    def fake_run(argv: list[str], **_kwargs: object) -> Result:
        calls.append(argv)
        if argv[1] == "run":
            return Result(stdout="b" * 64)
        if argv[1] == "exec":
            return Result(returncode=126)
        if argv[1:3] == ["container", "inspect"]:
            return Result(
                stdout=json.dumps(
                    {
                        "Status": "running",
                        "Running": True,
                        "ExitCode": 0,
                        "OOMKilled": False,
                    }
                )
            )
        return Result()

    monkeypatch.setattr(runtime_configuration.shutil, "which", lambda _: "/bin/docker")
    monkeypatch.setattr(runtime_configuration.subprocess, "run", fake_run)

    probe = probe_sandbox_runtime(
        sandbox_binary="docker",
        sandbox_image_id=f"sha256:{'a' * 64}",
        settle_seconds=0,
    )

    assert not probe.ready
    assert probe.error == (
        "sandbox probe could not execute its Python tool helper (exit_code=126)"
    )
    assert calls[-1][1:4] == ["container", "rm", "--force"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"sandbox_memory_mb": 0},
        {"sandbox_cpus": 0},
        {"sandbox_pids_limit": 0},
        {"sandbox_open_files": 0},
        {"sandbox_tmpfs_mb": 0},
    ],
)
def test_materialization_rejects_missing_resource_limits(
    tmp_path: Path,
    overrides: dict[str, int | float],
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(RuntimeConfigurationError, match="limit"):
        materialize_run_configuration(
            OPENCLAW_TEMPLATE,
            tmp_path / "runtime.json",
            manifest=load_team_manifest(TEAM_CONFIG),
            workspace=workspace,
            sandbox_image="sat-agent:phase1",
            sandbox_user="1000:1000",
            **overrides,
        )
