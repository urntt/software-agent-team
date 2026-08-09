"""Validated contracts for requests and cross-agent handoffs."""

from enum import StrEnum
from pathlib import PurePosixPath
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class AgentRole(StrEnum):
    """Agent responsibilities available to versioned team configurations."""

    CLARIFIER = "clarifier"
    SINGLE_AGENT = "single_agent"
    PLANNER = "planner"
    GENERALIST_DEVELOPER = "generalist_developer"
    FRONTEND_DEVELOPER = "frontend_developer"
    BACKEND_DEVELOPER = "backend_developer"
    INTEGRATOR = "integrator"
    TESTER = "tester"
    REVIEWER = "reviewer"


class HandoffStatus(StrEnum):
    """Terminal status for one Agent execution."""

    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"


class ArtifactKind(StrEnum):
    """Persisted artifact types used by the workflow."""

    CLARIFICATION_RECORD = "clarification_record"
    TASK_BRIEF = "task_brief"
    IMPLEMENTATION_PLAN = "implementation_plan"
    WORK_RESULT = "work_result"
    TEST_REPORT = "test_report"
    REVIEW_REPORT = "review_report"
    ITERATION_RECORD = "iteration_record"
    FINAL_REPORT = "final_report"


class AcceptanceCriterion(BaseModel):
    """One independently verifiable condition for accepting a result."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, pattern=r"^[A-Z][A-Z0-9_-]*$")
    description: str = Field(min_length=1)
    verification: str = Field(min_length=1)


class TaskBrief(BaseModel):
    """Confirmed requirements passed unchanged to comparable team runs."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    title: str = Field(min_length=1)
    source_request: str = Field(min_length=1)
    requirements: list[str] = Field(min_length=1)
    acceptance_criteria: list[AcceptanceCriterion] = Field(min_length=1)
    constraints: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    confirmed: bool = False

    @field_validator("requirements", "constraints", "assumptions", "open_questions")
    @classmethod
    def require_clean_unique_items(cls, values: list[str]) -> list[str]:
        """Reject blank or duplicate list entries."""

        cleaned = [value.strip() for value in values]
        if any(not value for value in cleaned):
            raise ValueError("task brief list entries must not be blank")
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("task brief list entries must be unique")
        return cleaned

    @model_validator(mode="after")
    def validate_confirmation(self) -> Self:
        """A confirmed brief cannot retain unresolved questions."""

        criterion_ids = [criterion.id for criterion in self.acceptance_criteria]
        if len(criterion_ids) != len(set(criterion_ids)):
            raise ValueError("acceptance criterion IDs must be unique")
        if self.confirmed and self.open_questions:
            raise ValueError("a confirmed task brief cannot contain open questions")
        return self


class ArtifactReference(BaseModel):
    """Run-relative reference to a persisted workflow artifact."""

    model_config = ConfigDict(extra="forbid")

    kind: ArtifactKind
    path: str = Field(min_length=1)
    description: str = ""

    @field_validator("path")
    @classmethod
    def require_safe_relative_path(cls, value: str) -> str:
        """Reject paths that can escape a run artifact directory."""

        if "\\" in value:
            raise ValueError("artifact paths must use POSIX separators")
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or path == PurePosixPath("."):
            raise ValueError("artifact paths must be safe run-relative paths")
        return value


class HandoffEnvelope(BaseModel):
    """Common metadata exchanged between Agent responsibilities."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    team_id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    iteration: int = Field(ge=1, le=3)
    source_role: AgentRole
    target_role: AgentRole | None = None
    status: HandoffStatus
    summary: str = Field(min_length=1)
    input_commit: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{7,64}$",
    )
    artifacts: list[ArtifactReference] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)

    @field_validator("blockers")
    @classmethod
    def require_clean_unique_blockers(cls, values: list[str]) -> list[str]:
        """Keep failure evidence concise and unambiguous."""

        cleaned = [value.strip() for value in values]
        if any(not value for value in cleaned):
            raise ValueError("blockers must not be blank")
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("blockers must be unique")
        return cleaned

    @model_validator(mode="after")
    def validate_handoff(self) -> Self:
        """Enforce evidence for failures and meaningful role boundaries."""

        if (
            self.status in {HandoffStatus.BLOCKED, HandoffStatus.FAILED}
            and not self.blockers
        ):
            raise ValueError("blocked or failed handoffs must identify a blocker")
        if self.target_role is not None and self.target_role == self.source_role:
            raise ValueError("source and target roles must differ")
        return self
