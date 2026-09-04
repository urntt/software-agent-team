"""Authoritative persisted-schema compatibility declarations."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


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


def supported_schemas() -> tuple[SchemaSupport, ...]:
    """Return the single current compatibility registry.

    Imports remain local so low-level contract modules do not depend on this
    registry and cannot form an import cycle.
    """

    from software_agent_team.artifacts import ARTIFACT_SCHEMA_VERSION
    from software_agent_team.budgets import BUDGET_SCHEMA_VERSION
    from software_agent_team.controls import CONTROL_COMMAND_SCHEMA_VERSION
    from software_agent_team.planning import PLANNING_SCHEMA_VERSION
    from software_agent_team.progress import RUN_EVENT_SCHEMA_VERSION
    from software_agent_team.run_control import RUN_SCHEMA_VERSION
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
