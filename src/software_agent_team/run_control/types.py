"""Lifecycle types, transitions, schemas, and invariants.

运行生命周期状态定义、转换规则、数据模式以及确定性校验。
定义了 RunPhase、TerminationReason、RunTransition、RunRecord 以及转换合法性检查。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from software_agent_team.artifacts import (
    ArtifactKind,
    ArtifactReference,
    IterationDecision,
)
from software_agent_team.git_workspace import GitSnapshot, GitWorkspace

RUN_SCHEMA_VERSION = 6
RUN_STATE_FILENAME = "run.json"
TASK_BRIEF_FILENAME = "task-brief.json"
TEAM_PLAN_FILENAME = "team-plan.json"
RUN_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

Clock = Callable[[], datetime]


class RunControlError(ValueError):
    """Base error for deterministic run-control failures."""


class RunAlreadyExistsError(RunControlError):
    """Raised when a run identifier already owns persisted state."""


class RunNotFoundError(RunControlError):
    """Raised when persisted state does not exist for a run identifier."""


class RunConflictError(RunControlError):
    """Raised when a caller tries to update an obsolete run revision."""


class RunIntegrityError(RunControlError):
    """Raised when persisted run state is incomplete or inconsistent."""


class InvalidRunTransitionError(RunControlError):
    """Raised when a requested lifecycle transition is not legal."""


class RunPhase(StrEnum):
    """Controller-owned lifecycle phases for one software build run."""

    CREATED = "created"
    PREPARING_WORKSPACE = "preparing_workspace"
    PLANNING = "planning"
    IMPLEMENTING = "implementing"
    SNAPSHOTTING = "snapshotting"
    VERIFYING = "verifying"
    REVIEWING = "reviewing"
    DECIDING = "deciding"
    DELIVERING = "delivering"
    COMPLETED = "completed"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        """Return whether no further lifecycle transition is allowed."""

        return self in {RunPhase.COMPLETED, RunPhase.FAILED}


class TerminationReason(StrEnum):
    """Stable categories for successful and unsuccessful termination."""

    SUCCEEDED = "succeeded"
    ITERATION_LIMIT_REACHED = "iteration_limit_reached"
    RESOURCE_LIMIT_REACHED = "resource_limit_reached"
    DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
    ARTIFACT_INVALID = "artifact_invalid"
    SAFETY_BOUNDARY_CROSSED = "safety_boundary_crossed"
    REPEATED_BLOCKER = "repeated_blocker"
    NO_RELEVANT_CHANGE = "no_relevant_change"
    EXECUTION_FAILED = "execution_failed"
    CONTROLLER_ERROR = "controller_error"
    USER_INTERRUPTED = "user_interrupted"
    USER_CANCELLED = "user_cancelled"
    USER_CORRECTION_REQUESTED = "user_correction_requested"


_LEGAL_NEXT_PHASES: dict[RunPhase, frozenset[RunPhase]] = {
    RunPhase.CREATED: frozenset({RunPhase.PREPARING_WORKSPACE}),
    RunPhase.PREPARING_WORKSPACE: frozenset({RunPhase.PLANNING}),
    RunPhase.PLANNING: frozenset({RunPhase.IMPLEMENTING}),
    RunPhase.IMPLEMENTING: frozenset({RunPhase.SNAPSHOTTING}),
    RunPhase.SNAPSHOTTING: frozenset({RunPhase.VERIFYING}),
    RunPhase.VERIFYING: frozenset({RunPhase.REVIEWING}),
    RunPhase.REVIEWING: frozenset({RunPhase.DECIDING}),
    RunPhase.DECIDING: frozenset({RunPhase.IMPLEMENTING, RunPhase.DELIVERING}),
    RunPhase.DELIVERING: frozenset({RunPhase.COMPLETED}),
    RunPhase.COMPLETED: frozenset(),
    RunPhase.FAILED: frozenset(),
}


def _is_legal_transition(source: RunPhase, target: RunPhase) -> bool:
    return target in _LEGAL_NEXT_PHASES[source] or (
        target is RunPhase.FAILED and not source.is_terminal
    )


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("run timestamps must include a timezone")
    return value.astimezone(UTC)


def _serialized_model(model: BaseModel) -> bytes:
    payload = json.dumps(
        model.model_dump(mode="json"),
        ensure_ascii=False,
        indent=2,
    )
    return f"{payload}\n".encode()


class RunTransition(BaseModel):
    """One durable, controller-authorized lifecycle transition."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence: int = Field(ge=1)
    source: RunPhase
    target: RunPhase
    iteration_before: int = Field(ge=1)
    iteration_after: int = Field(ge=1)
    occurred_at: datetime
    reason: str = Field(min_length=1)
    artifacts: tuple[ArtifactReference, ...] = ()
    implementation_plan_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    decision: IterationDecision | None = None

    @field_validator("occurred_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        """Normalize persisted transition timestamps to UTC."""

        return _require_utc(value)

    @field_validator("reason")
    @classmethod
    def require_clean_reason(cls, value: str) -> str:
        """Reject blank transition explanations."""

        cleaned = value.strip()
        if not cleaned:
            raise ValueError("transition reasons must not be blank")
        return cleaned

    @model_validator(mode="after")
    def validate_transition(self) -> Self:
        """Reject illegal phase, iteration, decision, and evidence changes."""

        if not _is_legal_transition(self.source, self.target):
            raise ValueError(
                f"illegal run transition: {self.source.value} -> {self.target.value}"
            )

        is_revision = (
            self.source is RunPhase.DECIDING and self.target is RunPhase.IMPLEMENTING
        )
        expected_iteration = (
            self.iteration_before + 1 if is_revision else self.iteration_before
        )
        if self.iteration_after != expected_iteration:
            raise ValueError("only a revision transition may increment iteration")

        paths = [reference.path for reference in self.artifacts]
        if len(paths) != len(set(paths)):
            raise ValueError("transition artifact references must be unique")
        kinds = {reference.kind for reference in self.artifacts}
        plan_transition = (
            self.source is RunPhase.PLANNING and self.target is RunPhase.IMPLEMENTING
        )
        has_plan_artifact = ArtifactKind.IMPLEMENTATION_PLAN in kinds
        if plan_transition:
            if has_plan_artifact and self.implementation_plan_sha256 is not None:
                raise ValueError(
                    "planning transition cannot bind both a plan artifact and digest"
                )
        elif self.implementation_plan_sha256 is not None:
            raise ValueError(
                "only a planning transition may bind an implementation-plan digest"
            )

        required: set[ArtifactKind] = set()
        if self.target is RunPhase.FAILED:
            required = {ArtifactKind.FINAL_REPORT}
        else:
            required = {
                (RunPhase.IMPLEMENTING, RunPhase.SNAPSHOTTING): {
                    ArtifactKind.WORK_RESULT
                },
                (RunPhase.VERIFYING, RunPhase.REVIEWING): {ArtifactKind.TEST_REPORT},
                (RunPhase.REVIEWING, RunPhase.DECIDING): {
                    ArtifactKind.ITERATION_RECORD
                },
                (RunPhase.DELIVERING, RunPhase.COMPLETED): {ArtifactKind.FINAL_REPORT},
            }.get((self.source, self.target), set())
        if not required.issubset(kinds):
            missing = ", ".join(sorted(kind.value for kind in required - kinds))
            raise ValueError(f"transition is missing required artifacts: {missing}")

        if self.source is RunPhase.DECIDING and self.target is not RunPhase.FAILED:
            expected_decision = (
                IterationDecision.REVISE
                if self.target is RunPhase.IMPLEMENTING
                else IterationDecision.ACCEPT
            )
            if self.decision is not expected_decision:
                raise ValueError(
                    f"{self.target.value} transition requires "
                    f"the {expected_decision.value} decision"
                )
        elif self.decision is not None:
            raise ValueError("only decision transitions may record a decision")
        return self


class RunRecord(BaseModel):
    """Complete recoverable state for one controlled run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[RUN_SCHEMA_VERSION] = RUN_SCHEMA_VERSION
    run_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    team_id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    team_plan_revision: int = Field(ge=1)
    team_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    agent_timeouts_seconds: dict[str, int] = Field(default_factory=dict)
    event_count: int = Field(default=0, ge=0)
    event_head_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    phase: RunPhase = RunPhase.CREATED
    current_iteration: int = Field(default=1, ge=1)
    iteration_limit: int = Field(ge=1)
    revision: int = Field(default=0, ge=0)
    task_brief_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime
    updated_at: datetime
    termination_reason: TerminationReason | None = None
    termination_detail: str | None = None
    workspace: GitWorkspace | None = None
    snapshots: tuple[GitSnapshot, ...] = ()
    transitions: tuple[RunTransition, ...] = ()

    @field_validator("created_at", "updated_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        """Normalize persisted run timestamps to UTC."""

        return _require_utc(value)

    @field_validator("termination_detail")
    @classmethod
    def clean_termination_detail(cls, value: str | None) -> str | None:
        """Reject blank terminal explanations."""

        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("termination detail must not be blank")
        return cleaned

    @field_validator("agent_timeouts_seconds")
    @classmethod
    def require_bounded_agent_timeouts(
        cls,
        values: dict[str, int],
    ) -> dict[str, int]:
        """Persist exact run-scoped invocation timeouts by Agent ID."""

        if not values or any(
            re.fullmatch(r"[a-z][a-z0-9_]*", agent_id) is None for agent_id in values
        ):
            raise ValueError("Agent timeout keys must be valid Agent IDs")
        if any(isinstance(seconds, bool) or seconds < 0 for seconds in values.values()):
            raise ValueError(
                "Agent invocation wall-clock limits must be between 0 and 86400"
            )
        return dict(sorted(values.items()))

    @model_validator(mode="after")
    def validate_record(self) -> Self:
        """Validate lifecycle history, iteration, and terminal metadata."""

        if (self.event_count == 0) != (self.event_head_sha256 is None):
            raise ValueError("run event count and head digest must agree")
        if self.current_iteration > self.iteration_limit:
            raise ValueError("current iteration exceeds the run iteration limit")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        if self.revision != len(self.transitions):
            raise ValueError("run revision must equal the transition count")

        expected_source = RunPhase.CREATED
        expected_iteration = 1
        previous_time = self.created_at
        artifact_paths: set[str] = set()
        for sequence, transition in enumerate(self.transitions, start=1):
            if transition.sequence != sequence:
                raise ValueError("transition sequences must be contiguous")
            if transition.source is not expected_source:
                raise ValueError("transition history must form one phase chain")
            if transition.iteration_before != expected_iteration:
                raise ValueError("transition history must form one iteration chain")
            if transition.occurred_at < previous_time:
                raise ValueError("transition timestamps must be monotonic")
            if transition.occurred_at > self.updated_at:
                raise ValueError("a transition cannot occur after updated_at")
            expected_source = transition.target
            expected_iteration = transition.iteration_after
            previous_time = transition.occurred_at
            transition_paths = {reference.path for reference in transition.artifacts}
            if artifact_paths.intersection(transition_paths):
                raise ValueError("run history cannot reference an artifact twice")
            artifact_paths.update(transition_paths)

        if self.phase is not expected_source:
            raise ValueError("current phase must match the transition history")
        if self.current_iteration != expected_iteration:
            raise ValueError("current iteration must match the transition history")

        if self.workspace is not None and self.workspace.run_id != self.run_id:
            raise ValueError("workspace run ID must match the run record")
        phases_before_workspace = {
            RunPhase.CREATED,
            RunPhase.PREPARING_WORKSPACE,
        }
        if self.phase in phases_before_workspace and self.workspace is not None:
            raise ValueError("workspace cannot exist before preparation completes")
        if (
            self.phase not in phases_before_workspace | {RunPhase.FAILED}
            and self.workspace is None
        ):
            raise ValueError("run phase requires attached workspace evidence")
        if self.snapshots and self.workspace is None:
            raise ValueError("snapshots require attached workspace evidence")

        expected_commit = None if self.workspace is None else self.workspace.base_commit
        for iteration, snapshot in enumerate(self.snapshots, start=1):
            if snapshot.run_id != self.run_id:
                raise ValueError("snapshot run ID must match the run record")
            if snapshot.iteration != iteration:
                raise ValueError("snapshot iterations must be contiguous")
            if snapshot.input_commit != expected_commit:
                raise ValueError("snapshot commits must form one chain")
            expected_commit = snapshot.output_commit
        if len(self.snapshots) > self.current_iteration:
            raise ValueError("snapshot count exceeds the current iteration")

        phases_before_current_snapshot = {
            RunPhase.PLANNING,
            RunPhase.IMPLEMENTING,
            RunPhase.SNAPSHOTTING,
        }
        phases_after_current_snapshot = {
            RunPhase.VERIFYING,
            RunPhase.REVIEWING,
            RunPhase.DECIDING,
            RunPhase.DELIVERING,
            RunPhase.COMPLETED,
        }
        if self.phase in phases_before_current_snapshot and (
            len(self.snapshots) != self.current_iteration - 1
        ):
            raise ValueError("current iteration cannot already have a snapshot")
        if self.phase in phases_after_current_snapshot and (
            len(self.snapshots) != self.current_iteration
        ):
            raise ValueError("run phase requires a snapshot for the current iteration")
        if self.phase is RunPhase.FAILED:
            failure_source = self.transitions[-1].source
            if failure_source in phases_before_workspace:
                if self.workspace is not None or self.snapshots:
                    raise ValueError(
                        "early workspace failure cannot contain workspace evidence"
                    )
            else:
                if self.workspace is None:
                    raise ValueError("failed run is missing prior workspace evidence")
                expected_snapshots = (
                    self.current_iteration
                    if failure_source in phases_after_current_snapshot
                    else self.current_iteration - 1
                )
                if len(self.snapshots) != expected_snapshots:
                    raise ValueError(
                        "failed run snapshot history does not match its source phase"
                    )

        if self.phase is RunPhase.COMPLETED:
            if self.termination_reason is not TerminationReason.SUCCEEDED:
                raise ValueError("completed runs require the succeeded reason")
        elif self.phase is RunPhase.FAILED:
            if self.termination_reason in {None, TerminationReason.SUCCEEDED}:
                raise ValueError("failed runs require a failure reason")
        elif self.termination_reason is not None or self.termination_detail is not None:
            raise ValueError("non-terminal runs cannot contain termination metadata")

        if self.phase.is_terminal and self.termination_detail is None:
            raise ValueError("terminal runs require termination detail")
        return self

    @property
    def current_commit(self) -> str | None:
        """Return the latest verified commit, or the workspace base commit."""

        if self.snapshots:
            return self.snapshots[-1].output_commit
        return None if self.workspace is None else self.workspace.base_commit

    @property
    def artifact_references(self) -> tuple[ArtifactReference, ...]:
        """Return every immutable artifact referenced by transition history."""

        return tuple(
            reference
            for transition in self.transitions
            for reference in transition.artifacts
        )


__all__ = [
    "RUN_ID_PATTERN",
    "RUN_SCHEMA_VERSION",
    "RUN_STATE_FILENAME",
    "TASK_BRIEF_FILENAME",
    "TEAM_PLAN_FILENAME",
    "_LEGAL_NEXT_PHASES",
    "Clock",
    "InvalidRunTransitionError",
    "RunAlreadyExistsError",
    "RunConflictError",
    "RunControlError",
    "RunIntegrityError",
    "RunNotFoundError",
    "RunPhase",
    "RunRecord",
    "RunTransition",
    "TerminationReason",
    "_is_legal_transition",
    "_require_utc",
    "_serialized_model",
]
