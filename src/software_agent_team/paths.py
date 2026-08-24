"""User-local paths owned by the product surface."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

USER_STATE_ENVIRONMENT_VARIABLE = "SAT_STATE_ROOT"


class UserPathError(ValueError):
    """Raised when a user-local path override is unsafe or ambiguous."""


def user_state_root(environment: Mapping[str, str] | None = None) -> Path:
    """Resolve the private root for product runs, sources, and workspaces."""

    values = os.environ if environment is None else environment
    override = values.get(USER_STATE_ENVIRONMENT_VARIABLE)
    if override is not None:
        if not override.strip() or override != override.strip():
            raise UserPathError(
                f"{USER_STATE_ENVIRONMENT_VARIABLE} must be a clean path"
            )
        root = Path(override).expanduser()
    else:
        xdg_root = values.get("XDG_STATE_HOME")
        if xdg_root:
            root = Path(xdg_root).expanduser() / "software-agent-team"
        else:
            home = values.get("HOME")
            base = Path(home).expanduser() if home else Path.home()
            root = base / ".local" / "state" / "software-agent-team"
    if not root.is_absolute() or root == Path(root.anchor):
        raise UserPathError("the SAT state root must be a specific absolute path")
    return root
