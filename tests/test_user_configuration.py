"""Tests for secret-free user-local live-run defaults."""

import json
import stat
from pathlib import Path

import pytest

from software_agent_team.user_configuration import (
    UserConfiguration,
    UserConfigurationError,
    load_user_configuration,
    save_user_configuration,
    user_configuration_path,
)


def sample_configuration(**updates: object) -> UserConfiguration:
    payload: dict[str, object] = {
        "model": "provider/model",
        "input_cost_per_million_usd": "0.50",
        "output_cost_per_million_usd": "1.50",
        "verification_concurrency": 1,
        "stage_timeout_seconds": 900,
    }
    payload.update(updates)
    return UserConfiguration.model_validate(payload)


def test_user_configuration_uses_xdg_or_explicit_path(tmp_path: Path) -> None:
    xdg = tmp_path / "xdg"
    explicit = tmp_path / "explicit.json"

    assert user_configuration_path({"XDG_CONFIG_HOME": str(xdg)}) == (
        xdg / "software-agent-team/config.json"
    )
    assert user_configuration_path({"SAT_CONFIG_PATH": str(explicit)}) == explicit


def test_user_configuration_requires_an_absolute_override() -> None:
    with pytest.raises(UserConfigurationError, match="must be absolute"):
        user_configuration_path({"SAT_CONFIG_PATH": "relative/config.json"})


def test_user_configuration_round_trips_atomically_with_private_mode(
    tmp_path: Path,
) -> None:
    path = tmp_path / "configuration" / "config.json"
    first = sample_configuration()
    second = sample_configuration(model="provider/reconfigured-model")

    assert load_user_configuration(path) is None
    assert save_user_configuration(first, path) == path
    assert load_user_configuration(path) == first
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    save_user_configuration(second, path)

    assert load_user_configuration(path) == second
    assert not list(path.parent.glob(".*.tmp"))


def test_user_configuration_rejects_unknown_or_malformed_values(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "model": "provider/model",
                "input_cost_per_million_usd": "1",
                "output_cost_per_million_usd": "2",
                "verification_concurrency": 3,
                "stage_timeout_seconds": 600,
                "api_key": "must-not-be-supported",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        load_user_configuration(path)


def test_v1_configuration_drops_the_legacy_timeout_with_a_notice(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "model": "provider/model",
                "input_cost_per_million_usd": "1",
                "output_cost_per_million_usd": "2",
                "verification_concurrency": 1,
                "agent_timeout_seconds": 2400,
            }
        ),
        encoding="utf-8",
    )

    with pytest.warns(UserWarning, match="without its legacy"):
        migrated = load_user_configuration(path)

    assert migrated is not None
    assert migrated.schema_version == 2
    assert migrated.model == "provider/model"
    assert migrated.verification_concurrency == 1
    assert migrated.stage_timeout_seconds is None
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 1


def test_user_configuration_refuses_a_symbolic_link(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "config.json"
    link.symlink_to(target)

    with pytest.raises(UserConfigurationError, match="symbolic link"):
        load_user_configuration(link)
    with pytest.raises(UserConfigurationError, match="symbolic link"):
        save_user_configuration(sample_configuration(), link)
