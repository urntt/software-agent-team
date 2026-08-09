"""Versioned team-configuration contracts and loaders."""

import json
from enum import StrEnum
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from software_agent_team.artifacts import AgentRole


class TeamKind(StrEnum):
    """Experimental category for a team definition."""

    BASELINE = "baseline"
    MULTI_AGENT = "multi_agent"


class StageMode(StrEnum):
    """Execution relationship among roles in one stage."""

    SEQUENTIAL = "sequential"
    PARALLEL = "parallel"


class TeamStage(BaseModel):
    """One ordered stage in the initial team execution plan."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    mode: StageMode
    roles: list[AgentRole] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_stage(self) -> Self:
        """Reject duplicate roles and meaningless parallel stages."""

        if len(self.roles) != len(set(self.roles)):
            raise ValueError("a stage cannot contain a role more than once")
        if self.mode is StageMode.PARALLEL and len(self.roles) < 2:
            raise ValueError("a parallel stage requires at least two roles")
        return self


class TeamDefinition(BaseModel):
    """A complete, comparable Agent-team configuration."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    kind: TeamKind
    description: str = Field(min_length=1)
    roles: list[AgentRole] = Field(min_length=1)
    stages: list[TeamStage] = Field(min_length=1)
    max_iterations: int = Field(ge=1, le=3)
    independent_review: bool
    revision_enabled: bool

    @model_validator(mode="after")
    def validate_definition(self) -> Self:
        """Keep role membership, stage ordering, and experiment type coherent."""

        if len(self.roles) != len(set(self.roles)):
            raise ValueError("team roles must be unique")

        stage_ids = [stage.id for stage in self.stages]
        if len(stage_ids) != len(set(stage_ids)):
            raise ValueError("team stage IDs must be unique")

        staged_roles = [role for stage in self.stages for role in stage.roles]
        if len(staged_roles) != len(set(staged_roles)):
            raise ValueError("each role must appear in exactly one initial stage")
        if set(staged_roles) != set(self.roles):
            raise ValueError("team roles must exactly match roles assigned to stages")

        if self.kind is TeamKind.BASELINE:
            if self.roles != [AgentRole.SINGLE_AGENT]:
                raise ValueError("the baseline must contain only the single_agent role")
            if self.max_iterations != 1:
                raise ValueError(
                    "the baseline must use exactly one implementation pass"
                )
            if self.independent_review or self.revision_enabled:
                raise ValueError("the baseline cannot enable review-driven revision")
        else:
            required = {
                AgentRole.PLANNER,
                AgentRole.TESTER,
                AgentRole.REVIEWER,
            }
            if not required.issubset(self.roles):
                raise ValueError(
                    "multi-agent teams require planner, tester, and reviewer roles"
                )
            if not self.independent_review or not self.revision_enabled:
                raise ValueError(
                    "multi-agent teams require independent review and revision"
                )

        return self


class TeamManifest(BaseModel):
    """Versioned collection of all experimental team configurations."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(ge=1)
    default_team: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    shared_roles: list[AgentRole] = Field(default_factory=list)
    teams: list[TeamDefinition] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_manifest(self) -> Self:
        """Enforce unique teams and a complete Agent registry."""

        if len(self.shared_roles) != len(set(self.shared_roles)):
            raise ValueError("shared roles must be unique")
        if AgentRole.CLARIFIER not in self.shared_roles:
            raise ValueError("the clarifier must be registered as a shared role")

        team_ids = [team.id for team in self.teams]
        if len(team_ids) != len(set(team_ids)):
            raise ValueError("team IDs must be unique")
        if self.default_team not in team_ids:
            raise ValueError("default_team must reference a configured team")

        shared = set(self.shared_roles)
        for team in self.teams:
            if shared.intersection(team.roles):
                raise ValueError("shared roles cannot be duplicated inside a team")
        return self

    @property
    def required_roles(self) -> set[AgentRole]:
        """Return every Agent role required by the manifest."""

        return set(self.shared_roles).union(
            role for team in self.teams for role in team.roles
        )

    def get_team(self, team_id: str) -> TeamDefinition:
        """Return one team definition or raise a concise configuration error."""

        for team in self.teams:
            if team.id == team_id:
                return team
        raise ValueError(f"unknown team configuration: {team_id}")

    def validate_handoff_boundary(
        self,
        *,
        team_id: str,
        iteration: int,
        source_role: AgentRole,
        target_role: AgentRole | None,
    ) -> None:
        """Verify that a handoff belongs to its selected team and run budget."""

        team = self.get_team(team_id)
        if iteration > team.max_iterations:
            raise ValueError(
                f"iteration {iteration} exceeds {team_id} limit "
                f"of {team.max_iterations}"
            )

        allowed_roles = set(self.shared_roles).union(team.roles)
        for label, role in (("source", source_role), ("target", target_role)):
            if role is not None and role not in allowed_roles:
                raise ValueError(
                    f"{label} role {role.value} is not part of team {team_id}"
                )


def load_team_manifest(path: Path) -> TeamManifest:
    """Load and validate a team manifest from JSON."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    return TeamManifest.model_validate(payload)
