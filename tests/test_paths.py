"""Tests for private user-local product state paths."""

from pathlib import Path

import pytest

from software_agent_team.paths import UserPathError, user_state_root


def test_user_state_root_uses_override_xdg_or_home(tmp_path: Path) -> None:
    explicit = tmp_path / "explicit"
    xdg = tmp_path / "xdg"
    home = tmp_path / "home"

    assert user_state_root({"SAT_STATE_ROOT": str(explicit)}) == explicit
    assert user_state_root({"XDG_STATE_HOME": str(xdg)}) == (
        xdg / "software-agent-team"
    )
    assert user_state_root({"HOME": str(home)}) == (
        home / ".local/state/software-agent-team"
    )


@pytest.mark.parametrize("value", ["", " relative", "relative", "/"])
def test_user_state_root_rejects_unsafe_overrides(value: str) -> None:
    with pytest.raises(UserPathError):
        user_state_root({"SAT_STATE_ROOT": value})
