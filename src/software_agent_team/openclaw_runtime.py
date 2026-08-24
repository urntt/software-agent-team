"""Process environment boundaries for SAT-owned OpenClaw state."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

# Empty values deliberately neutralize caller-owned OpenClaw selectors before
# the SAT-owned paths are applied. The executor merges these overrides into the
# trusted caller environment, so ordinary provider API-key variables remain
# available while existing OpenClaw state, Gateway, and debug policy do not.
_ALWAYS_NEUTRALIZED = (
    "OPENCLAW_AGENT_DIR",
    "OPENCLAW_GATEWAY_PASSWORD",
    "OPENCLAW_GATEWAY_PORT",
    "OPENCLAW_GATEWAY_SECRET",
    "OPENCLAW_GATEWAY_TOKEN",
    "OPENCLAW_GATEWAY_URL",
    "OPENCLAW_PROFILE",
    "PI_CODING_AGENT_DIR",
)


def isolated_openclaw_environment(
    *,
    state_dir: Path,
    config_path: Path,
    ambient_environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return overrides that isolate every mutable OpenClaw path for SAT.

    The caller may merge these values into a trusted host environment so model
    provider variables remain available. The returned values always override
    ambient OpenClaw profile, config, credential, workspace, and Agent paths.
    """

    if not state_dir.is_absolute() or state_dir == Path(state_dir.anchor):
        raise ValueError("OpenClaw state directory must be a specific absolute path")
    if not config_path.is_absolute() or config_path == Path(config_path.anchor):
        raise ValueError("OpenClaw config path must be a specific absolute path")
    resolved_state = state_dir.resolve(strict=False)
    resolved_config = config_path.resolve(strict=False)
    ambient = os.environ if ambient_environment is None else ambient_environment
    environment = {
        name: ""
        for name in set(_ALWAYS_NEUTRALIZED).union(
            name for name in ambient if name.startswith("OPENCLAW_")
        )
    }
    environment.update(
        {
            "OPENCLAW_AUTH_PROFILE_SECRET_DIR": str(resolved_state / "credentials"),
            "OPENCLAW_CONFIG_DIR": str(resolved_state),
            "OPENCLAW_CONFIG_PATH": str(resolved_config),
            "OPENCLAW_HOME": str(resolved_state),
            "OPENCLAW_OAUTH_DIR": str(resolved_state / "credentials"),
            "OPENCLAW_STATE_DIR": str(resolved_state),
            "OPENCLAW_WORKSPACE_DIR": str(resolved_state / "workspace"),
        }
    )
    return environment
