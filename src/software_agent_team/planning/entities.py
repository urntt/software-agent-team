"""Atomic proposed entities: requirements, assumptions, criteria, agents, and tasks."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from software_agent_team.artifacts import (
    ReviewBoundaryKind,
)
from software_agent_team.planning.common import (
    _clean_text,
    _clean_unique,
    _safe_path,
)
from software_agent_team.planning.models_basic import AgentWorkload
from software_agent_team.teams import (
    AgentCapability,
    AgentSpecialization,
    default_specialization_for_capability,
    specialization_contract,
)


class ProposedRequirement(BaseModel):
    """One model-owned requirement whose identity cannot drift from its meaning."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(
        pattern=r"^REQ_[A-Z0-9_]+$",
        description=(
            "Stable requirement identity. Use one unique uppercase REQ_ identifier."
        ),
    )
    description: str = Field(
        min_length=1,
        max_length=500,
        description=(
            "Requirement meaning only. Do not repeat the stable ID as a prefix."
        ),
    )

    @field_validator("description")
    @classmethod
    def require_clean_description(cls, value: str) -> str:
        return _clean_text(value, label="requirement description")


class ProposedAssumption(BaseModel):
    """One model-owned assumption bound atomically to its autonomy decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    statement: str = Field(
        min_length=1,
        max_length=500,
        description="The implementation or scheduling assumption being made.",
    )
    decision_id: str = Field(
        pattern=r"^DECISION_[A-Z0-9_]+$",
        description=(
            "Stable identity of the Agent-autonomy decision that authorizes this "
            "assumption."
        ),
    )

    @field_validator("statement")
    @classmethod
    def require_clean_statement(cls, value: str) -> str:
        return _clean_text(value, label="assumption statement")


class ProposedCriterion(BaseModel):
    """User-facing acceptance condition proposed during Planning."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[A-Z][A-Z0-9_-]*$")
    description: str = Field(min_length=1, max_length=500)
    verification: str = Field(min_length=1, max_length=500)
    requirement_ids: tuple[str, ...] = Field(
        default=(),
        min_length=1,
        exclude_if=lambda values: not values,
    )
    verification_agent_ids: tuple[str, ...] = Field(
        default=(),
        min_length=1,
        exclude_if=lambda values: not values,
    )
    review_boundaries: tuple[ReviewBoundaryKind, ...] = ()

    @field_validator("description", "verification")
    @classmethod
    def require_clean_text(cls, value: str) -> str:
        return _clean_text(value, label="acceptance criterion text")

    @field_validator("review_boundaries")
    @classmethod
    def require_unique_review_boundaries(
        cls,
        values: tuple[ReviewBoundaryKind, ...],
    ) -> tuple[ReviewBoundaryKind, ...]:
        if len(values) != len(set(values)):
            raise ValueError("proposed Review boundaries must be unique")
        return values

    @field_validator("requirement_ids")
    @classmethod
    def require_requirement_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = _clean_unique(values, label="criterion requirement")
        if any(re.fullmatch(r"REQ_[A-Z0-9_]+", value) is None for value in cleaned):
            raise ValueError("criterion requirements must use stable REQ_ IDs")
        return cleaned

    @field_validator("verification_agent_ids")
    @classmethod
    def require_verification_agents(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        cleaned = _clean_unique(values, label="criterion verifier")
        if any(re.fullmatch(r"[a-z][a-z0-9_]*", value) is None for value in cleaned):
            raise ValueError("criterion verifiers must use stable Agent IDs")
        return cleaned


class ProposedAgent(BaseModel):
    """Planner recommendation before the controller assigns runtime authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    label: str = Field(min_length=1, max_length=80)
    responsibility: str = Field(min_length=1, max_length=500)
    rationale: str = Field(min_length=1, max_length=500)
    capability: AgentCapability
    specialization: AgentSpecialization
    stage_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    dependencies: tuple[str, ...] = ()
    workspace_scope: str = Field(
        min_length=1,
        max_length=200,
        description=(
            "Use repository for whole-project access or repository/path for a "
            "narrower canonical scope; never repeat the destination directory."
        ),
    )
    workload: AgentWorkload

    @model_validator(mode="before")
    @classmethod
    def infer_legacy_specialization(cls, value: object) -> object:
        """Read proposal records created before specialization became explicit."""

        if not isinstance(value, dict) or value.get("specialization") is not None:
            return value
        payload = dict(value)
        try:
            capability = AgentCapability(payload.get("capability"))
        except (TypeError, ValueError):
            return payload
        payload["specialization"] = default_specialization_for_capability(
            capability
        ).value
        return payload

    @field_validator("label", "responsibility", "rationale")
    @classmethod
    def require_clean_text(cls, value: str) -> str:
        return _clean_text(value, label="proposed Agent text")

    @field_validator("dependencies")
    @classmethod
    def require_unique_dependencies(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _clean_unique(values, label="Agent dependency")

    @field_validator("workspace_scope")
    @classmethod
    def require_safe_scope(cls, value: str) -> str:
        cleaned = _safe_path(value)
        if PurePosixPath(cleaned).parts[0] != "repository":
            raise ValueError("workspace scopes must start at repository or repository/")
        return cleaned

    @model_validator(mode="after")
    def reject_bootstrap_capability(self) -> Self:
        if self.capability in {
            AgentCapability.CLARIFICATION,
            AgentCapability.PLANNING,
        }:
            raise ValueError(
                "bootstrap Planning and Clarification capabilities are outside "
                "the runtime team"
            )
        if self.id in self.dependencies:
            raise ValueError("a proposed Agent cannot depend on itself")
        if (
            self.capability
            not in specialization_contract(self.specialization).compatible_capabilities
        ):
            raise ValueError(
                "proposed Agent specialization and capability are incompatible"
            )
        return self


class ProposedTask(BaseModel):
    """Run-scoped work intent assigned to one proposed runtime Agent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^TASK_[A-Z0-9_]+$")
    owner_agent_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    description: str = Field(min_length=1, max_length=500)
    dependencies: tuple[str, ...] = Field(
        default=(),
        description=(
            "Local ordering between tasks owned by the same Agent. The Controller "
            "compiles cross-Agent task dependencies from the Agent DAG."
        ),
    )
    acceptance_criteria: tuple[str, ...] = Field(min_length=1)
    expected_paths: tuple[str, ...] = Field(
        default=(),
        description=(
            "Non-binding forecasts of repository paths likely to be relevant to "
            "the task. They are not required outputs, a completion checklist, or "
            "write authority. Paths are canonical and relative to the repository "
            "root; directory paths must not end with a slash. A writer task may "
            "forecast only paths inside its owner's workspace scope."
        ),
    )

    @field_validator("description")
    @classmethod
    def require_clean_description(cls, value: str) -> str:
        return _clean_text(value, label="task description")

    @field_validator("dependencies")
    @classmethod
    def require_clean_dependencies(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _clean_unique(values, label="task reference")

    @field_validator("acceptance_criteria")
    @classmethod
    def require_criterion_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = _clean_unique(values, label="task acceptance criterion")
        if any(re.fullmatch(r"[A-Z][A-Z0-9_-]*", value) is None for value in cleaned):
            raise ValueError(
                "task acceptance criteria must use stable uppercase criterion IDs"
            )
        return cleaned

    @field_validator("expected_paths")
    @classmethod
    def require_safe_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = _clean_unique(values, label="expected path")
        return tuple(_safe_path(value) for value in cleaned)


