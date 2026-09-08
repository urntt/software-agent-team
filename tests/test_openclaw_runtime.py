"""Tests for the SAT-owned OpenClaw process boundary."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from software_agent_team.openclaw_runtime import isolated_openclaw_environment


def test_shell_and_runtime_neutralize_shared_foreign_selectors(tmp_path: Path) -> None:
    selectors = (
        "STATE_DIRECTORY",
        "NODE_OPTIONS",
        "NODE_PATH",
        "PI_CODING_AGENT_DIR",
        "NODE_COMPILE_CACHE",
    )
    ambient = {"PATH": os.defpath}
    ambient.update({name: "/foreign/runtime" for name in selectors})
    ambient.update(
        OPENAI_API_KEY="test-provider-value",
        HTTPS_PROXY="http://127.0.0.1:9",
        NODE_DISABLE_COMPILE_CACHE="foreign-disable-value",
    )
    state = tmp_path / "state"
    config = state / "openclaw.json"
    overrides = isolated_openclaw_environment(
        state_dir=state, config_path=config, ambient_environment=ambient
    )
    effective = ambient | overrides
    for name in selectors:
        assert not effective.get(name), name
    assert effective["OPENAI_API_KEY"] == ambient["OPENAI_API_KEY"]
    assert effective["HTTPS_PROXY"] == ambient["HTTPS_PROXY"]
    assert effective["NODE_DISABLE_COMPILE_CACHE"] == "1"

    helper = Path(__file__).parents[1] / "scripts/openclaw-environment.sh"
    probe = (
        "import json, os; print(json.dumps({k: os.environ.get(k) for k in "
        + repr(
            (*selectors, "OPENAI_API_KEY", "HTTPS_PROXY", "NODE_DISABLE_COMPILE_CACHE")
        )
        + "}))"
    )
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; shift; sat_run_openclaw_isolated "$@"',
            "probe",
            str(helper),
            str(tmp_path),
            str(state),
            str(config),
            sys.executable,
            "-c",
            probe,
        ],
        env=ambient,
        text=True,
        capture_output=True,
        check=True,
    )
    observed = json.loads(result.stdout)
    for name in selectors:
        assert not observed[name], name
    assert observed["OPENAI_API_KEY"] == effective["OPENAI_API_KEY"]
    assert observed["HTTPS_PROXY"] == effective["HTTPS_PROXY"]
    assert observed["NODE_DISABLE_COMPILE_CACHE"] == "1"


def test_openclaw_environment_neutralizes_ambient_path_selectors(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state/openclaw"
    config = tmp_path / "runs/example/openclaw.runtime.json"

    environment = isolated_openclaw_environment(
        state_dir=state,
        config_path=config,
        ambient_environment={
            "OPENCLAW_GATEWAY_URL": "ws://existing.example",
            "OPENCLAW_SHOW_SECRETS": "1",
            "OPENAI_API_KEY": "provider-key-remains-in-caller-environment",
        },
    )

    assert environment == {
        "OPENCLAW_AGENT_DIR": "",
        "OPENCLAW_GATEWAY_PASSWORD": "",
        "OPENCLAW_GATEWAY_PORT": "",
        "OPENCLAW_GATEWAY_SECRET": "",
        "OPENCLAW_GATEWAY_TOKEN": "",
        "OPENCLAW_GATEWAY_URL": "",
        "OPENCLAW_PROFILE": "",
        "OPENCLAW_SHOW_SECRETS": "",
        "PI_CODING_AGENT_DIR": "",
        "STATE_DIRECTORY": "",
        "NODE_OPTIONS": "",
        "NODE_PATH": "",
        "NODE_COMPILE_CACHE": "",
        "NODE_DISABLE_COMPILE_CACHE": "1",
        "OPENCLAW_AUTH_PROFILE_SECRET_DIR": str(state / "credentials"),
        "OPENCLAW_CONFIG_DIR": str(state),
        "OPENCLAW_CONFIG_PATH": str(config),
        "OPENCLAW_HOME": str(state),
        "OPENCLAW_OAUTH_DIR": str(state / "credentials"),
        "OPENCLAW_STATE_DIR": str(state),
        "OPENCLAW_WORKSPACE_DIR": str(state / "workspace"),
    }
    assert "OPENAI_API_KEY" not in environment


@pytest.mark.parametrize(
    ("state", "config"),
    [
        (Path("relative"), Path("/tmp/config.json")),
        (Path("/tmp/state"), Path("relative.json")),
        (Path("/"), Path("/tmp/config.json")),
        (Path("/tmp/state"), Path("/")),
    ],
)
def test_openclaw_environment_requires_specific_absolute_paths(
    state: Path,
    config: Path,
) -> None:
    with pytest.raises(ValueError, match="specific absolute path"):
        isolated_openclaw_environment(state_dir=state, config_path=config)
