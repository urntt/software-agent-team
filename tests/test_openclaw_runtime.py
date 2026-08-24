"""Tests for the SAT-owned OpenClaw process boundary."""

from pathlib import Path

import pytest

from software_agent_team.openclaw_runtime import isolated_openclaw_environment


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
