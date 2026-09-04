"""Tests for the authoritative persisted-schema compatibility registry."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from software_agent_team.schema_compatibility import (
    SchemaCompatibilityError,
    SchemaFamily,
    SchemaSupport,
    inspect_persisted_schema_compatibility,
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


def write_schema(path: Path, version: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema_version": version}) + "\n",
        encoding="utf-8",
    )


def test_persisted_schema_scan_classifies_known_state_paths(tmp_path: Path) -> None:
    configuration = tmp_path / "config/config.json"
    installation = tmp_path / "data/installation.json"
    state = tmp_path / "state"
    paths = {
        configuration: 1,
        installation: 1,
        state / "runs/run-1/run.json": 6,
        state / "runs/run-1/team-plan.json": 1,
        state / "runs/run-1/events/000001.json": 2,
        state / "runs/run-1/controls/control-1/000001.json": 2,
        state / "runs/run-1/implementation-plan.json": 2,
        state / "runs/run-1/iterations/01/iteration-record.json": 2,
        state / "planning/plan-1/request.json": 2,
    }
    for path, version in paths.items():
        write_schema(path, version)

    report = inspect_persisted_schema_compatibility(
        configuration_path=configuration,
        installation_record_path=installation,
        state_root=state,
        candidate_support=supported_schemas(),
    )

    assert report.compatible
    assert len(report.observations) == len(paths)
    assert {item.family for item in report.observations} >= {
        SchemaFamily.USER_CONFIGURATION,
        SchemaFamily.INSTALLATION,
        SchemaFamily.RUN,
        SchemaFamily.TEAM_PLAN,
        SchemaFamily.RUN_EVENT,
        SchemaFamily.CONTROL_COMMAND,
        SchemaFamily.ARTIFACT,
        SchemaFamily.PLANNING,
    }


def test_persisted_schema_scan_reports_unsupported_newer_data(tmp_path: Path) -> None:
    configuration = tmp_path / "config.json"
    write_schema(configuration, USER_CONFIGURATION_SCHEMA_VERSION + 1)

    report = inspect_persisted_schema_compatibility(
        configuration_path=configuration,
        installation_record_path=tmp_path / "missing-installation.json",
        state_root=tmp_path / "state",
        candidate_support=supported_schemas(),
    )

    assert not report.compatible
    assert report.observations[0].supported is False
    assert "outside readable range" in report.problems[0]


def test_persisted_schema_scan_fails_closed_on_malformed_or_symlinked_data(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    malformed = state / "runs/run-1/run.json"
    malformed.parent.mkdir(parents=True)
    malformed.write_text("not-json\n", encoding="utf-8")
    configuration_target = tmp_path / "target.json"
    write_schema(configuration_target, USER_CONFIGURATION_SCHEMA_VERSION)
    configuration = tmp_path / "config.json"
    configuration.symlink_to(configuration_target)

    report = inspect_persisted_schema_compatibility(
        configuration_path=configuration,
        installation_record_path=tmp_path / "missing.json",
        state_root=state,
        candidate_support=supported_schemas(),
    )

    assert not report.compatible
    assert len(report.problems) == 2
    assert any("not a regular file" in problem for problem in report.problems)
    assert any("invalid JSON" in problem for problem in report.problems)


def test_persisted_schema_scan_requires_a_complete_candidate_registry(
    tmp_path: Path,
) -> None:
    incomplete = (
        SchemaSupport(
            family=SchemaFamily.RUN,
            current=6,
            minimum_readable=6,
            maximum_readable=6,
        ),
    )

    with pytest.raises(SchemaCompatibilityError, match="every family"):
        inspect_persisted_schema_compatibility(
            configuration_path=tmp_path / "config.json",
            installation_record_path=tmp_path / "installation.json",
            state_root=tmp_path / "state",
            candidate_support=incomplete,
        )
