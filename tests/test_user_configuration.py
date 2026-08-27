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
        "max_concurrency": 4,
        "stage_timeout_seconds": 900,
        "progress_visibility": "detailed",
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
                "schema_version": 4,
                "model": "provider/model",
                "input_cost_per_million_usd": "1",
                "output_cost_per_million_usd": "2",
                "max_concurrency": 17,
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
    assert migrated.schema_version == 5
    assert migrated.model == "provider/model"
    assert migrated.max_concurrency == 1
    assert migrated.stage_timeout_seconds is None
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 1


def test_v2_configuration_preserves_evaluation_defaults_with_a_notice(
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
                "verification_concurrency": 2,
                "stage_timeout_seconds": 900,
            }
        ),
        encoding="utf-8",
    )

    with pytest.warns(UserWarning, match="schema v2"):
        migrated = load_user_configuration(path)

    assert migrated is not None
    assert migrated.schema_version == 5
    assert migrated.input_cost_per_million_usd == 1
    assert migrated.output_cost_per_million_usd == 2
    assert migrated.max_concurrency == 2
    assert migrated.stage_timeout_seconds == 900


def test_v3_configuration_renames_verification_concurrency_with_a_notice(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "model": "provider/model",
                "input_cost_per_million_usd": None,
                "output_cost_per_million_usd": None,
                "verification_concurrency": 2,
                "stage_timeout_seconds": None,
            }
        ),
        encoding="utf-8",
    )

    with pytest.warns(UserWarning, match="max_concurrency"):
        migrated = load_user_configuration(path)

    assert migrated is not None
    assert migrated.schema_version == 5
    assert migrated.max_concurrency == 2


def test_v4_configuration_defaults_to_standard_progress_with_a_notice(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 4,
                "model": "provider/model",
                "input_cost_per_million_usd": None,
                "output_cost_per_million_usd": None,
                "max_concurrency": 4,
                "stage_timeout_seconds": None,
            }
        ),
        encoding="utf-8",
    )

    with pytest.warns(UserWarning, match="standard progress visibility"):
        migrated = load_user_configuration(path)

    assert migrated is not None
    assert migrated.schema_version == 5
    assert migrated.max_concurrency == 4
    assert migrated.progress_visibility == "standard"


def test_product_configuration_can_omit_local_price_estimates() -> None:
    configuration = UserConfiguration(model="provider/model")

    assert configuration.input_cost_per_million_usd is None
    assert configuration.output_cost_per_million_usd is None
    assert configuration.max_concurrency == 2
    assert configuration.progress_visibility == "standard"

    with pytest.raises(ValueError):
        UserConfiguration(
            model="provider/model",
            progress_visibility="everything",
        )

    with pytest.raises(ValueError, match="configured together"):
        UserConfiguration(
            model="provider/model",
            input_cost_per_million_usd="1",
        )


def test_user_configuration_refuses_a_symbolic_link(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "config.json"
    link.symlink_to(target)

    with pytest.raises(UserConfigurationError, match="symbolic link"):
        load_user_configuration(link)
    with pytest.raises(UserConfigurationError, match="symbolic link"):
        save_user_configuration(sample_configuration(), link)
