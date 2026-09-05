"""Tests for the authoritative persisted-schema compatibility registry."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from software_agent_team.artifacts import (
    ARTIFACT_SCHEMA_VERSION,
    MINIMUM_READABLE_ARTIFACT_SCHEMA_VERSION,
)
from software_agent_team.planning import PLANNING_SCHEMA_VERSION
from software_agent_team.schema_compatibility import (
    CANDIDATE_COMPATIBILITY_PROTOCOL_VERSION,
    CandidateCompatibilityEnvelope,
    SchemaCompatibilityError,
    SchemaFamily,
    SchemaSupport,
    inspect_candidate_persisted_state,
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


def test_registry_declares_only_intentional_historical_read_support() -> None:
    support = schema_support_map()

    configuration = support[SchemaFamily.USER_CONFIGURATION]
    assert configuration.current == USER_CONFIGURATION_SCHEMA_VERSION
    assert configuration.minimum_readable == 1
    assert configuration.supports(1)
    assert configuration.supports(USER_CONFIGURATION_SCHEMA_VERSION)
    assert not configuration.supports(USER_CONFIGURATION_SCHEMA_VERSION + 1)

    planning = support[SchemaFamily.PLANNING]
    assert planning.minimum_readable == 2
    assert planning.current == planning.maximum_readable == PLANNING_SCHEMA_VERSION
    assert planning.supports(2)

    artifact = support[SchemaFamily.ARTIFACT]
    assert artifact.minimum_readable == MINIMUM_READABLE_ARTIFACT_SCHEMA_VERSION
    assert artifact.current == artifact.maximum_readable == ARTIFACT_SCHEMA_VERSION
    assert artifact.supports(MINIMUM_READABLE_ARTIFACT_SCHEMA_VERSION)

    for family, item in support.items():
        if family in {
            SchemaFamily.USER_CONFIGURATION,
            SchemaFamily.PLANNING,
            SchemaFamily.ARTIFACT,
        }:
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
        state / "runs/run-1/budget-ledger.json": 1,
        state / "runs/run-1/events/000001.json": 2,
        state / "runs/run-1/controls/control-1/000001.json": 2,
        state / "runs/run-1/implementation-plan.json": 2,
        state / "runs/run-1/iterations/01/iteration-record.json": 2,
        state / "planning/plan-1/request.json": 2,
        state / "self-checks/run-1/0001-task_admission.json": 1,
        state / "process-leases/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.json": 1,
    }
    for path, version in paths.items():
        write_schema(path, version)
    run_lock = state / "runs/.lock"
    run_lock.touch(mode=0o644)

    report = inspect_persisted_schema_compatibility(
        configuration_path=configuration,
        installation_record_path=installation,
        state_root=state,
        candidate_support=supported_schemas(),
    )

    assert report.compatible
    assert len(report.observations) == len(paths)
    assert run_lock.is_file()
    assert {item.family for item in report.observations} >= {
        SchemaFamily.USER_CONFIGURATION,
        SchemaFamily.INSTALLATION,
        SchemaFamily.RUN,
        SchemaFamily.TEAM_PLAN,
        SchemaFamily.RUN_EVENT,
        SchemaFamily.CONTROL_COMMAND,
        SchemaFamily.ARTIFACT,
        SchemaFamily.PLANNING,
        SchemaFamily.SELF_CHECK,
        SchemaFamily.PROCESS_LEASE,
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


@pytest.mark.parametrize("unsafe_lock", ["nonempty", "symlink", "executable"])
def test_persisted_schema_scan_rejects_unsafe_run_store_lock(
    tmp_path: Path,
    unsafe_lock: str,
) -> None:
    runs = tmp_path / "state/runs"
    runs.mkdir(parents=True)
    lock = runs / ".lock"
    if unsafe_lock == "symlink":
        target = tmp_path / "outside-lock"
        target.touch()
        lock.symlink_to(target)
    else:
        lock.write_text("content" if unsafe_lock == "nonempty" else "")
        if unsafe_lock == "executable":
            lock.chmod(0o700)

    with pytest.raises(SchemaCompatibilityError, match="owner-bound empty lock"):
        inspect_persisted_schema_compatibility(
            configuration_path=tmp_path / "missing-config.json",
            installation_record_path=tmp_path / "missing-installation.json",
            state_root=tmp_path / "state",
            candidate_support=supported_schemas(),
        )


def test_persisted_schema_scan_rejects_unknown_or_incomplete_run_entries(
    tmp_path: Path,
) -> None:
    runs = tmp_path / "state/runs"
    unknown = runs / ".abandoned.tmp"
    unknown.mkdir(parents=True)

    with pytest.raises(SchemaCompatibilityError, match="invalid identity"):
        inspect_persisted_schema_compatibility(
            configuration_path=tmp_path / "missing-config.json",
            installation_record_path=tmp_path / "missing-installation.json",
            state_root=tmp_path / "state",
            candidate_support=supported_schemas(),
        )

    unknown.rename(runs / "incomplete-run")
    report = inspect_persisted_schema_compatibility(
        configuration_path=tmp_path / "missing-config.json",
        installation_record_path=tmp_path / "missing-installation.json",
        state_root=tmp_path / "state",
        candidate_support=supported_schemas(),
    )
    assert not report.compatible
    assert "not a regular file" in report.problems[0]


def test_candidate_envelope_attributes_its_complete_compatibility_result(
    tmp_path: Path,
) -> None:
    runs = tmp_path / "state/runs"
    runs.mkdir(parents=True)
    (runs / ".unexpected").write_text("unknown\n", encoding="utf-8")

    envelope = inspect_candidate_persisted_state(
        source_revision="a" * 40,
        configuration_path=tmp_path / "missing-config.json",
        installation_record_path=tmp_path / "missing-installation.json",
        state_root=tmp_path / "state",
    )

    assert envelope.protocol_version == CANDIDATE_COMPATIBILITY_PROTOCOL_VERSION
    assert envelope.source_revision == "a" * 40
    assert len(envelope.schema_support) == len(SchemaFamily)
    assert not envelope.compatibility.compatible
    assert "not a real directory" in envelope.compatibility.problems[0]


def test_candidate_envelope_rejects_incomplete_or_unattributed_results() -> None:
    payload = inspect_candidate_persisted_state(
        source_revision="a" * 40,
        configuration_path=Path("/missing-config.json"),
        installation_record_path=Path("/missing-installation.json"),
        state_root=Path("/missing-state"),
    ).model_dump(mode="json")

    payload["source_revision"] = "short"
    with pytest.raises(ValueError, match="source revision"):
        CandidateCompatibilityEnvelope.model_validate(payload)

    payload["source_revision"] = "a" * 40
    payload["schema_support"] = payload["schema_support"][:-1]
    with pytest.raises(ValueError, match="every family"):
        CandidateCompatibilityEnvelope.model_validate(payload)


def test_candidate_not_the_active_updater_interprets_run_liveness(
    tmp_path: Path,
) -> None:
    active = tmp_path / "state/runs/active/run.json"
    terminal = tmp_path / "state/runs/terminal/run.json"
    write_schema(active, 6)
    write_schema(terminal, 6)
    for path, phase in ((active, "reviewing"), (terminal, "failed")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["phase"] = phase
        path.write_text(json.dumps(payload), encoding="utf-8")

    envelope = inspect_candidate_persisted_state(
        source_revision="a" * 40,
        configuration_path=tmp_path / "missing-config.json",
        installation_record_path=tmp_path / "missing-installation.json",
        state_root=tmp_path / "state",
    )

    assert envelope.compatibility.compatible
    assert envelope.active_run_ids == ("active",)


def test_candidate_fails_closed_when_run_liveness_cannot_be_interpreted(
    tmp_path: Path,
) -> None:
    run = tmp_path / "state/runs/run-1/run.json"
    write_schema(run, 6)

    envelope = inspect_candidate_persisted_state(
        source_revision="a" * 40,
        configuration_path=tmp_path / "missing-config.json",
        installation_record_path=tmp_path / "missing-installation.json",
        state_root=tmp_path / "state",
    )

    assert not envelope.compatibility.compatible
    assert envelope.active_run_ids == ()
    assert "phase is invalid" in envelope.compatibility.problems[0]
