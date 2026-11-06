"""Implementation plans, developer work results, and tester reports.

This module provides data models for planning tasks, tracking code modifications,
evaluating command outputs, verifying acceptance criteria, and producing
deterministic test reports.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .envelope import IterationArtifact
from .types import (
    AGENT_ID_PATTERN,
    COMMIT_PATTERN,
    AgentRole,
    ArtifactKind,
    CheckStatus,
    _require_clean_unique_items,
    _require_safe_relative_path,
)


class PlanTask(BaseModel):
    """One attributable implementation task in a Planner artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, pattern=r"^TASK_[A-Z0-9_]+$")
    owner: AgentRole
    description: str = Field(min_length=1)
    dependencies: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = Field(min_length=1)
    expected_paths: tuple[str, ...] = Field(
        default=(),
        description=(
            "Non-binding forecasts of repository paths likely to be relevant; "
            "they do not require creation or grant write authority."
        ),
    )

    @field_validator("dependencies", "acceptance_criteria")
    @classmethod
    def require_clean_unique_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Reject ambiguous task and criterion references."""
        return _require_clean_unique_items(values)

    @field_validator("expected_paths")
    @classmethod
    def require_safe_expected_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Keep expected repository paths relative and portable."""
        cleaned = _require_clean_unique_items(values)
        return tuple(_require_safe_relative_path(value) for value in cleaned)


class ImplementationPlan(IterationArtifact):
    """Planner-owned implementation plan for a confirmed task brief."""

    kind: Literal[ArtifactKind.IMPLEMENTATION_PLAN] = ArtifactKind.IMPLEMENTATION_PLAN
    producer: Literal["planner"] = "planner"
    iteration: Literal[1] = 1
    objective: str = Field(min_length=1)
    approach: tuple[str, ...] = Field(min_length=1)
    tasks: tuple[PlanTask, ...] = Field(min_length=1)
    risks: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()

    @field_validator("approach", "risks", "assumptions")
    @classmethod
    def require_clean_unique_text(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Reject blank and duplicate planning statements."""
        return _require_clean_unique_items(values)

    @model_validator(mode="after")
    def validate_tasks(self) -> Self:
        """Require unique, resolvable, acyclic task dependencies."""
        task_ids = [task.id for task in self.tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("implementation plan task IDs must be unique")
        known = set(task_ids)
        dependencies = {task.id: set(task.dependencies) for task in self.tasks}
        for task_id, task_dependencies in dependencies.items():
            unknown = task_dependencies - known
            if unknown:
                raise ValueError(
                    f"task {task_id} references unknown dependencies: "
                    f"{', '.join(sorted(unknown))}"
                )
            if task_id in task_dependencies:
                raise ValueError("a plan task cannot depend on itself")

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(task_id: str) -> None:
            if task_id in visiting:
                raise ValueError("implementation plan dependencies must be acyclic")
            if task_id in visited:
                return
            visiting.add(task_id)
            for dependency in dependencies[task_id]:
                visit(dependency)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in task_ids:
            visit(task_id)
        return self


class WorkResult(IterationArtifact):
    """Verified source result attributed to one implementation Agent."""

    kind: Literal[ArtifactKind.WORK_RESULT] = ArtifactKind.WORK_RESULT
    producer: str = Field(pattern=AGENT_ID_PATTERN)
    input_commit: str = Field(pattern=COMMIT_PATTERN)
    output_commit: str = Field(pattern=COMMIT_PATTERN)
    summary: str = Field(min_length=1)
    completed_tasks: tuple[str, ...] = Field(min_length=1)
    changed_files: tuple[str, ...] = ()
    unresolved_issues: tuple[str, ...] = ()

    @field_validator("completed_tasks", "unresolved_issues")
    @classmethod
    def require_clean_unique_text(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Reject blank and duplicate implementation statements."""
        return _require_clean_unique_items(values)

    @field_validator("changed_files")
    @classmethod
    def require_safe_changed_files(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Require concrete repository-relative changed paths."""
        cleaned = _require_clean_unique_items(values)
        return tuple(_require_safe_relative_path(value) for value in cleaned)

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        """Keep the commit range coherent with the verified changed paths.

        The run-scoped TeamPlan, rather than this context-free model, decides
        whether a producer may report a controller-verified unchanged result.
        """
        unchanged = self.input_commit == self.output_commit
        if unchanged != (not self.changed_files):
            raise ValueError(
                "work result requires changed files exactly when its commits differ"
            )
        return self


class CommandEvidence(BaseModel):
    """Controller-recorded evidence for one deterministic command."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, pattern=r"^CHECK_[A-Z0-9_]+$")
    argv: tuple[str, ...] = Field(min_length=1)
    criterion_ids: tuple[str, ...] = ()
    exit_code: int | None = None
    timed_out: bool = False
    duration_ms: int = Field(ge=0)
    stdout_path: str = Field(min_length=1)
    stderr_path: str = Field(min_length=1)
    stdout_tail: str = Field(default="", max_length=4096)
    stderr_tail: str = Field(default="", max_length=4096)
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    summary: str = ""

    @field_validator("argv")
    @classmethod
    def require_clean_argv(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Persist an unambiguous argv rather than a shell command string."""
        if any(not value for value in values):
            raise ValueError("command arguments must not be empty")
        return values

    @field_validator("criterion_ids")
    @classmethod
    def require_clean_criterion_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Keep deterministic criterion coverage explicit and unambiguous."""
        return _require_clean_unique_items(values)

    @field_validator("stdout_path", "stderr_path")
    @classmethod
    def require_safe_output_path(cls, value: str) -> str:
        """Keep command output inside the run directory."""
        return _require_safe_relative_path(value)

    @model_validator(mode="after")
    def validate_exit(self) -> Self:
        """Distinguish process timeout from a normal process exit."""
        if self.timed_out and self.exit_code is not None:
            raise ValueError("timed-out commands cannot report an exit code")
        if not self.timed_out and self.exit_code is None:
            raise ValueError("completed commands require an exit code")
        if self.stdout_truncated and not self.stdout_tail:
            raise ValueError("truncated stdout requires a retained tail")
        if self.stderr_truncated and not self.stderr_tail:
            raise ValueError("truncated stderr requires a retained tail")
        return self


class CriterionResult(BaseModel):
    """Evidence-backed result for one confirmed acceptance criterion."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    criterion_id: str = Field(min_length=1, pattern=r"^[A-Z][A-Z0-9_-]*$")
    status: CheckStatus
    command_ids: tuple[str, ...] = ()
    detail: str = Field(min_length=1)

    @field_validator("command_ids")
    @classmethod
    def require_clean_command_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Reject ambiguous command references."""
        return _require_clean_unique_items(values)


class TestReport(IterationArtifact):
    """Tester analysis grounded in deterministic command evidence."""

    kind: Literal[ArtifactKind.TEST_REPORT] = ArtifactKind.TEST_REPORT
    producer: str = Field(default="tester", pattern=AGENT_ID_PATTERN)
    input_commit: str = Field(pattern=COMMIT_PATTERN)
    status: Literal[
        CheckStatus.PASSED,
        CheckStatus.FAILED,
        CheckStatus.BLOCKED,
    ] = Field(
        description=(
            "Overall deterministic Tester result. Use passed when every command "
            "and deterministic-only criterion passes, manual-review criteria are "
            "pending_review, and no blocker exists. pending_review is valid only "
            "for individual manual-review criteria, never for this field."
        )
    )
    commands: tuple[CommandEvidence, ...] = Field(min_length=1)
    criteria: tuple[CriterionResult, ...] = Field(min_length=1)
    manual_review_criteria: tuple[str, ...] = ()
    findings: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    summary: str = Field(min_length=1)

    @field_validator("findings", "blockers", "manual_review_criteria")
    @classmethod
    def require_clean_unique_text(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Reject blank and duplicate verification findings."""
        return _require_clean_unique_items(values)

    @model_validator(mode="after")
    def validate_evidence(self) -> Self:
        """Keep overall test status consistent with recorded evidence."""
        command_ids = [command.id for command in self.commands]
        if len(command_ids) != len(set(command_ids)):
            raise ValueError("test command IDs must be unique")
        criterion_ids = [criterion.criterion_id for criterion in self.criteria]
        if len(criterion_ids) != len(set(criterion_ids)):
            raise ValueError("test criterion IDs must be unique")
        known_commands = set(command_ids)
        command_coverage = {
            command.id: set(command.criterion_ids) for command in self.commands
        }
        coverage_available = all(command.criterion_ids for command in self.commands)
        for criterion in self.criteria:
            unknown = set(criterion.command_ids) - known_commands
            if unknown:
                raise ValueError(
                    f"criterion {criterion.criterion_id} references unknown commands"
                )
            if coverage_available:
                mismatched = [
                    command_id
                    for command_id in criterion.command_ids
                    if criterion.criterion_id not in command_coverage[command_id]
                ]
                if mismatched:
                    raise ValueError(
                        f"criterion {criterion.criterion_id} references commands "
                        "without declared coverage"
                    )

        known_criteria = set(criterion_ids)
        manual_criteria = set(self.manual_review_criteria)
        unknown_manual = manual_criteria - known_criteria
        if unknown_manual:
            raise ValueError("manual-review criteria must exist in the test report")
        for criterion in self.criteria:
            pending = criterion.status is CheckStatus.PENDING_REVIEW
            if pending and criterion.criterion_id not in manual_criteria:
                raise ValueError("only manual-review criteria may be pending review")
            if (
                criterion.criterion_id in manual_criteria
                and criterion.status is CheckStatus.PASSED
            ):
                raise ValueError(
                    "Tester cannot pass criteria assigned to independent review"
                )

        commands_passed = all(
            not command.timed_out and command.exit_code == 0
            for command in self.commands
        )
        tester_scope_passed = all(
            criterion.status is CheckStatus.PASSED
            or (
                criterion.criterion_id in manual_criteria
                and criterion.status is CheckStatus.PENDING_REVIEW
            )
            for criterion in self.criteria
        )
        if self.status is CheckStatus.PASSED:
            if not commands_passed or not tester_scope_passed or self.blockers:
                raise ValueError("passed test reports require all evidence to pass")
        elif self.status is CheckStatus.BLOCKED:
            if not self.blockers:
                raise ValueError("blocked test reports must identify a blocker")
            if not any(
                criterion.status is CheckStatus.BLOCKED for criterion in self.criteria
            ):
                raise ValueError("blocked test reports require a blocked criterion")
        else:
            if not any(
                criterion.status is CheckStatus.FAILED for criterion in self.criteria
            ):
                raise ValueError("failed test reports require a failed criterion")
            if commands_passed and tester_scope_passed:
                raise ValueError("failed test reports require failing evidence")
        return self
