"""Authoritative persisted-schema compatibility declarations."""

from __future__ import annotations

import json
import os
import re
import stat
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_SCHEMA_FILE_BYTES = 16 * 1024 * 1024
MAX_SCHEMA_FILES = 100_000
RUN_STATE_ENTRY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class SchemaFamily(StrEnum):
    """Persisted SAT contract families with independent evolution."""

    INSTALLATION = "installation"
    USER_CONFIGURATION = "user_configuration"
    RUN = "run"
    PLANNING = "planning"
    ARTIFACT = "artifact"
    RUN_EVENT = "run_event"
    CONTROL_COMMAND = "control_command"
    TEAM_PLAN = "team_plan"
    BUDGET = "budget"
    SELF_CHECK = "self_check"
    PROCESS_LEASE = "process_lease"


class SchemaSupport(BaseModel):
    """Readable schema interval implemented by one SAT release."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    family: SchemaFamily
    current: int = Field(ge=1)
    minimum_readable: int = Field(ge=1)
    maximum_readable: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_interval(self) -> SchemaSupport:
        if not self.minimum_readable <= self.current <= self.maximum_readable:
            raise ValueError("current schema must be inside its readable interval")
        return self

    def supports(self, version: int) -> bool:
        """Return whether this software can safely read one schema version."""

        return self.minimum_readable <= version <= self.maximum_readable


class PersistedSchemaObservation(BaseModel):
    """One observed top-level schema version at a known persistence path."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    family: SchemaFamily
    path: str
    version: int = Field(ge=1)
    supported: bool


class PersistedSchemaCompatibilityReport(BaseModel):
    """Compatibility of current user data with one candidate software target."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    compatible: bool
    observations: tuple[PersistedSchemaObservation, ...]
    problems: tuple[str, ...] = ()


class SchemaCompatibilityError(RuntimeError):
    """Raised when persisted schema evidence cannot be inspected safely."""


def supported_schemas() -> tuple[SchemaSupport, ...]:
    """Return the single current compatibility registry.

    Imports remain local so low-level contract modules do not depend on this
    registry and cannot form an import cycle.
    """

    from software_agent_team.artifacts import (
        ARTIFACT_SCHEMA_VERSION,
        MINIMUM_READABLE_ARTIFACT_SCHEMA_VERSION,
    )
    from software_agent_team.budgets import BUDGET_SCHEMA_VERSION
    from software_agent_team.controls import CONTROL_COMMAND_SCHEMA_VERSION
    from software_agent_team.planning import (
        MINIMUM_READABLE_PLANNING_SCHEMA_VERSION,
        PLANNING_SCHEMA_VERSION,
    )
    from software_agent_team.process_lifecycle import PROCESS_LEASE_SCHEMA_VERSION
    from software_agent_team.progress import RUN_EVENT_SCHEMA_VERSION
    from software_agent_team.run_control import RUN_SCHEMA_VERSION
    from software_agent_team.self_check import SELF_CHECK_SCHEMA_VERSION
    from software_agent_team.teams import TEAM_PLAN_SCHEMA_VERSION
    from software_agent_team.user_configuration import (
        USER_CONFIGURATION_SCHEMA_VERSION,
    )
    from software_agent_team.versioning import INSTALLATION_RECORD_SCHEMA_VERSION

    exact = (
        (SchemaFamily.INSTALLATION, INSTALLATION_RECORD_SCHEMA_VERSION),
        (SchemaFamily.RUN, RUN_SCHEMA_VERSION),
        (SchemaFamily.PLANNING, PLANNING_SCHEMA_VERSION),
        (SchemaFamily.ARTIFACT, ARTIFACT_SCHEMA_VERSION),
        (SchemaFamily.RUN_EVENT, RUN_EVENT_SCHEMA_VERSION),
        (SchemaFamily.CONTROL_COMMAND, CONTROL_COMMAND_SCHEMA_VERSION),
        (SchemaFamily.TEAM_PLAN, TEAM_PLAN_SCHEMA_VERSION),
        (SchemaFamily.BUDGET, BUDGET_SCHEMA_VERSION),
        (SchemaFamily.SELF_CHECK, SELF_CHECK_SCHEMA_VERSION),
        (SchemaFamily.PROCESS_LEASE, PROCESS_LEASE_SCHEMA_VERSION),
    )
    support = [
        SchemaSupport(
            family=family,
            current=version,
            minimum_readable=version,
            maximum_readable=version,
        )
        for family, version in exact
    ]
    support = [
        (
            SchemaSupport(
                family=item.family,
                current=item.current,
                minimum_readable=MINIMUM_READABLE_ARTIFACT_SCHEMA_VERSION,
                maximum_readable=item.maximum_readable,
            )
            if item.family is SchemaFamily.ARTIFACT
            else item
        )
        for item in support
    ]
    support = [
        (
            SchemaSupport(
                family=item.family,
                current=item.current,
                minimum_readable=MINIMUM_READABLE_PLANNING_SCHEMA_VERSION,
                maximum_readable=item.maximum_readable,
            )
            if item.family is SchemaFamily.PLANNING
            else item
        )
        for item in support
    ]
    support.append(
        SchemaSupport(
            family=SchemaFamily.USER_CONFIGURATION,
            current=USER_CONFIGURATION_SCHEMA_VERSION,
            minimum_readable=1,
            maximum_readable=USER_CONFIGURATION_SCHEMA_VERSION,
        )
    )
    return tuple(sorted(support, key=lambda item: item.family.value))


def schema_support_map() -> dict[SchemaFamily, SchemaSupport]:
    """Index the compatibility registry by stable family identifier."""

    return {item.family: item for item in supported_schemas()}


def inspect_persisted_schema_compatibility(
    *,
    configuration_path: Path,
    installation_record_path: Path,
    state_root: Path,
    candidate_support: tuple[SchemaSupport, ...],
) -> PersistedSchemaCompatibilityReport:
    """Inspect known persisted contracts before changing active software."""

    support = {item.family: item for item in candidate_support}
    if len(support) != len(candidate_support) or set(support) != set(SchemaFamily):
        raise SchemaCompatibilityError(
            "candidate schema support must contain every family exactly once"
        )
    candidates = _persisted_schema_paths(
        configuration_path=configuration_path,
        installation_record_path=installation_record_path,
        state_root=state_root,
    )
    if len(candidates) > MAX_SCHEMA_FILES:
        raise SchemaCompatibilityError(
            "persisted schema file count exceeds safety limit"
        )
    observations: list[PersistedSchemaObservation] = []
    problems: list[str] = []
    for family, path in candidates:
        try:
            version = _read_schema_version(path)
        except SchemaCompatibilityError as error:
            problems.append(str(error))
            continue
        supported = support[family].supports(version)
        observations.append(
            PersistedSchemaObservation(
                family=family,
                path=str(path),
                version=version,
                supported=supported,
            )
        )
        if not supported:
            readable = support[family]
            problems.append(
                f"{family.value} schema {version} at {path} is outside readable "
                f"range {readable.minimum_readable}..{readable.maximum_readable}"
            )
    return PersistedSchemaCompatibilityReport(
        compatible=not problems,
        observations=tuple(observations),
        problems=tuple(problems),
    )


def _persisted_schema_paths(
    *,
    configuration_path: Path,
    installation_record_path: Path,
    state_root: Path,
) -> list[tuple[SchemaFamily, Path]]:
    candidates: list[tuple[SchemaFamily, Path]] = []
    _append_if_present(candidates, SchemaFamily.USER_CONFIGURATION, configuration_path)
    _append_if_present(candidates, SchemaFamily.INSTALLATION, installation_record_path)
    runs = state_root / "runs"
    if runs.exists():
        _require_real_directory(runs, "run state root")
        for run in sorted(runs.iterdir()):
            if run.name == ".lock":
                _require_controller_lock(run, "run store lock")
                continue
            if run.is_symlink() or not run.is_dir():
                raise SchemaCompatibilityError(
                    f"run state entry is not a real directory: {run}"
                )
            if RUN_STATE_ENTRY_PATTERN.fullmatch(run.name) is None:
                raise SchemaCompatibilityError(
                    f"run state directory has an invalid identity: {run}"
                )
            candidates.append((SchemaFamily.RUN, run / "run.json"))
            _append_if_present(
                candidates, SchemaFamily.TEAM_PLAN, run / "team-plan.json"
            )
            _append_if_present(
                candidates, SchemaFamily.BUDGET, run / "budget-ledger.json"
            )
            events = run / "events"
            if events.exists():
                _require_real_directory(events, "run event directory")
                for path in sorted(events.glob("*.json")):
                    candidates.append((SchemaFamily.RUN_EVENT, path))
            controls = run / "controls"
            if controls.exists():
                _require_real_directory(controls, "control directory")
                for path in sorted(controls.glob("*/*.json")):
                    candidates.append((SchemaFamily.CONTROL_COMMAND, path))
            artifact_patterns = (
                "implementation-plan.json",
                "final-report.json",
                "iterations/**/*.json",
            )
            reserved = {run / "run.json", run / "team-plan.json"}
            for pattern in artifact_patterns:
                for path in sorted(run.glob(pattern)):
                    if path not in reserved:
                        candidates.append((SchemaFamily.ARTIFACT, path))
    planning = state_root / "planning"
    if planning.exists():
        _require_real_directory(planning, "planning state root")
        for path in sorted(planning.glob("**/*.json")):
            candidates.append((SchemaFamily.PLANNING, path))
    self_checks = state_root / "self-checks"
    if self_checks.exists():
        _require_real_directory(self_checks, "self-check state root")
        for run in sorted(self_checks.iterdir()):
            _require_real_directory(run, "self-check task directory")
            for path in sorted(run.glob("*.json")):
                candidates.append((SchemaFamily.SELF_CHECK, path))
    process_leases = state_root / "process-leases"
    if process_leases.exists():
        _require_real_directory(process_leases, "process lease root")
        for path in sorted(process_leases.glob("*.json")):
            candidates.append((SchemaFamily.PROCESS_LEASE, path))
    return candidates


def _require_controller_lock(path: Path, label: str) -> None:
    """Recognize one content-free lock without treating it as persisted JSON."""

    try:
        metadata = path.lstat()
    except OSError as error:
        raise SchemaCompatibilityError(f"cannot inspect {label}: {path}") from error
    effective_uid = getattr(os, "geteuid", lambda: metadata.st_uid)()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != effective_uid
        or metadata.st_nlink != 1
        or metadata.st_size != 0
        or stat.S_IMODE(metadata.st_mode) & 0o111
    ):
        raise SchemaCompatibilityError(
            f"{label} is not a safe owner-bound empty lock file: {path}"
        )


def _append_if_present(
    candidates: list[tuple[SchemaFamily, Path]],
    family: SchemaFamily,
    path: Path,
) -> None:
    if path.exists() or path.is_symlink():
        candidates.append((family, path))


def _read_schema_version(path: Path) -> int:
    if path.is_symlink() or not path.is_file():
        raise SchemaCompatibilityError(
            f"persisted schema is not a regular file: {path}"
        )
    try:
        size = path.stat().st_size
    except OSError as error:
        raise SchemaCompatibilityError(
            f"persisted schema cannot be inspected: {path}"
        ) from error
    if size > MAX_SCHEMA_FILE_BYTES:
        raise SchemaCompatibilityError(f"persisted schema exceeds size limit: {path}")
    try:
        payload = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise SchemaCompatibilityError(
            f"persisted schema contains invalid JSON: {path}"
        ) from error
    if not isinstance(payload, dict):
        raise SchemaCompatibilityError(f"persisted schema is not a JSON object: {path}")
    version = payload.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise SchemaCompatibilityError(f"persisted schema version is invalid: {path}")
    return version


def _require_real_directory(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_dir():
        raise SchemaCompatibilityError(f"{label} must be a real directory: {path}")
