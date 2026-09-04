"""Tests for the authoritative persisted-schema compatibility registry."""

from __future__ import annotations

from software_agent_team.schema_compatibility import (
    SchemaFamily,
    schema_support_map,
    supported_schemas,
)
from software_agent_team.user_configuration import USER_CONFIGURATION_SCHEMA_VERSION


def test_schema_registry_has_one_unique_entry_per_family() -> None:
    support = supported_schemas()

    assert len(support) == len(SchemaFamily)
    assert len({item.family for item in support}) == len(support)
    assert tuple(item.family.value for item in support) == tuple(
        sorted(item.family.value for item in support)
    )


def test_only_configuration_declares_historical_read_support() -> None:
    support = schema_support_map()

    configuration = support[SchemaFamily.USER_CONFIGURATION]
    assert configuration.current == USER_CONFIGURATION_SCHEMA_VERSION
    assert configuration.minimum_readable == 1
    assert configuration.supports(1)
    assert configuration.supports(USER_CONFIGURATION_SCHEMA_VERSION)
    assert not configuration.supports(USER_CONFIGURATION_SCHEMA_VERSION + 1)

    for family, item in support.items():
        if family is SchemaFamily.USER_CONFIGURATION:
            continue
        assert item.minimum_readable == item.current == item.maximum_readable
