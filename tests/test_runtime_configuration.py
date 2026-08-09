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
)
from software_agent_team.teams import load_team_manifest

REPOSITORY_ROOT = Path(__file__).parents[1]
TEAM_CONFIG = REPOSITORY_ROOT / "configs" / "teams.json"
OPENCLAW_TEMPLATE = REPOSITORY_ROOT / "configs" / "openclaw.example.json5"


def test_materialized_config_binds_every_role_to_one_run_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    destination = tmp_path / "run" / "openclaw.runtime.json"

    materialize_run_configuration(
        OPENCLAW_TEMPLATE,
        destination,
        manifest=load_team_manifest(TEAM_CONFIG),
        workspace=workspace,
        sandbox_image="sat-agent:phase1",
    )

    payload = json.loads(destination.read_text(encoding="utf-8"))
    defaults = payload["agents"]["defaults"]
    assert defaults["repoRoot"] == str(workspace.resolve())
    assert defaults["skipBootstrap"] is True
    assert defaults["sandbox"]["scope"] == "session"
    assert defaults["sandbox"]["docker"] == {
        "image": "sat-agent:phase1",
        "network": "none",
        "readOnlyRoot": True,
        "capDrop": ["ALL"],
        "user": f"{os.getuid()}:{os.getgid()}",
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
        )

    workspace = tmp_path / "worktree"
    workspace.mkdir()
    materialize_run_configuration(
        OPENCLAW_TEMPLATE,
        destination,
        manifest=manifest,
        workspace=workspace,
        sandbox_image="sat-agent:phase1",
    )
    before = destination.read_bytes()
    with pytest.raises(RuntimeConfigurationError, match="already exists"):
        materialize_run_configuration(
            OPENCLAW_TEMPLATE,
            destination,
            manifest=manifest,
            workspace=workspace,
            sandbox_image="sat-agent:phase1",
        )
    assert destination.read_bytes() == before


def test_preflight_executes_explicit_commands_without_model_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    openclaw = tmp_path / "openclaw"
    openclaw.write_text("binary", encoding="utf-8")
    openclaw.chmod(0o755)
    config = tmp_path / "runtime.json"
    config.write_text("{}", encoding="utf-8")
    calls: list[list[str]] = []

    class Result:
        def __init__(self, returncode: int, stdout: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = ""

    def fake_run(argv: list[str], **kwargs: object) -> Result:
        assert "shell" not in kwargs
        calls.append(argv)
        if argv[-1] == "--version":
            return Result(0, "OpenClaw test")
        return Result(0, "{}")

    monkeypatch.setattr(runtime_configuration.shutil, "which", lambda _: "/bin/docker")
    monkeypatch.setattr(runtime_configuration.subprocess, "run", fake_run)

    result = inspect_runtime_preflight(
        openclaw_binary=openclaw,
        runtime_config=config,
        sandbox_binary="docker",
        sandbox_image="sat-agent:phase1",
    )

    assert result.ready
    assert calls == [
        [str(openclaw), "--version"],
        [str(openclaw), "config", "validate", "--json"],
        ["/bin/docker", "image", "inspect", "sat-agent:phase1"],
    ]
    assert os.environ.get("OPENCLAW_CONFIG_PATH") is None
