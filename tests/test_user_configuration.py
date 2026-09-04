"""Tests for secret-free user-local live-run defaults."""

import json
import stat
from pathlib import Path

import pytest

from software_agent_team.model_routing import ModelProfile
from software_agent_team.teams import AgentCapability
from software_agent_team.user_configuration import (
    USER_CONFIGURATION_SCHEMA_VERSION,
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
    assert migrated.schema_version == USER_CONFIGURATION_SCHEMA_VERSION
    assert migrated.model == "provider/model"
    assert migrated.max_concurrency == 1
    assert not hasattr(migrated, "stage_timeout_seconds")
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 1


def test_v2_configuration_retires_product_timeout_with_a_notice(
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
    assert migrated.schema_version == USER_CONFIGURATION_SCHEMA_VERSION
    assert migrated.input_cost_per_million_usd == 1
    assert migrated.output_cost_per_million_usd == 2
    assert migrated.max_concurrency == 2
    assert not hasattr(migrated, "stage_timeout_seconds")


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
    assert migrated.schema_version == USER_CONFIGURATION_SCHEMA_VERSION
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
    assert migrated.schema_version == USER_CONFIGURATION_SCHEMA_VERSION
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


def test_product_model_profile_count_is_not_an_orchestration_limit() -> None:
    profiles = tuple(
        ModelProfile(
            id=f"route_{index}",
            model=f"provider/model-{index}",
            capabilities=tuple(AgentCapability),
        )
        for index in range(20)
    )

    configuration = UserConfiguration(
        model_profiles=profiles,
        default_model_profile_id="route_0",
        routing_mode="policy",
    )

    assert len(configuration.model_profiles) == 20


def test_v5_configuration_migrates_to_one_strict_profile(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 5,
                "model": "provider/model",
                "input_cost_per_million_usd": "1",
                "output_cost_per_million_usd": "2",
                "max_concurrency": 3,
                "stage_timeout_seconds": None,
                "progress_visibility": "compact",
            }
        ),
        encoding="utf-8",
    )

    with pytest.warns(UserWarning, match="strict default model profile"):
        migrated = load_user_configuration(path)

    assert migrated is not None
    assert migrated.schema_version == USER_CONFIGURATION_SCHEMA_VERSION
    assert migrated.model == "provider/model"
    assert migrated.default_model_profile.id == "default"
    assert migrated.routing_mode.value == "strict"
    assert migrated.max_concurrency == 3
    assert migrated.progress_visibility == "compact"


def test_v6_configuration_migrates_missing_model_metadata(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 6,
                "model_profiles": [
                    {
                        "id": "default",
                        "model": "provider/model",
                        "capabilities": [
                            "clarification",
                            "planning",
                            "implementation",
                            "integration",
                            "testing",
                            "review",
                        ],
                        "priority": 100,
                        "input_cost_per_million_usd": "1",
                        "output_cost_per_million_usd": "2",
                    }
                ],
                "default_model_profile_id": "default",
                "routing_mode": "strict",
                "capability_profile_overrides": {},
                "stage_profile_overrides": {},
                "authorized_switch_conditions": [],
                "max_model_switches_per_agent": 0,
                "max_concurrency": 2,
                "stage_timeout_seconds": None,
                "progress_visibility": "standard",
            }
        ),
        encoding="utf-8",
    )

    with pytest.warns(UserWarning, match="schema v6"):
        migrated = load_user_configuration(path)

    assert migrated is not None
    assert migrated.schema_version == USER_CONFIGURATION_SCHEMA_VERSION
    assert migrated.default_model_profile.pricing_source == "user_supplied"
    assert migrated.default_model_profile.context_window_tokens is None


def test_v7_configuration_preserves_routing_and_retires_timeout(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.json"
    payload = sample_configuration().model_dump(mode="json")
    payload["schema_version"] = 7
    payload["stage_timeout_seconds"] = 900
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.warns(UserWarning, match="schema v7"):
        migrated = load_user_configuration(path)

    assert migrated is not None
    assert migrated.schema_version == USER_CONFIGURATION_SCHEMA_VERSION
    assert migrated.model == "provider/model"
    assert migrated.max_concurrency == 4
    assert not hasattr(migrated, "stage_timeout_seconds")


def test_current_configuration_persists_profiles_as_single_source_of_truth(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.json"
    save_user_configuration(sample_configuration(), path)

    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["schema_version"] == USER_CONFIGURATION_SCHEMA_VERSION
    assert payload["model_profiles"][0]["model"] == "provider/model"
    assert "model" not in payload
    assert "input_cost_per_million_usd" not in payload
    assert "stage_timeout_seconds" not in payload


def test_user_configuration_refuses_a_symbolic_link(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "config.json"
    link.symlink_to(target)

    with pytest.raises(UserConfigurationError, match="symbolic link"):
        load_user_configuration(link)
    with pytest.raises(UserConfigurationError, match="symbolic link"):
        save_user_configuration(sample_configuration(), link)
