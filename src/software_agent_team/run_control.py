"""Deterministic run lifecycle, persistence, and recovery."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from enum import StrEnum
from fcntl import LOCK_EX, LOCK_UN, flock
from pathlib import Path
from typing import IO, Literal, Self
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from software_agent_team.artifacts import (
    ArtifactKind,
    ArtifactReference,
    IterationDecision,
    TaskBrief,
)
from software_agent_team.git_workspace import GitSnapshot, GitWorkspace
from software_agent_team.teams import TeamManifest

RUN_SCHEMA_VERSION = 3
RUN_STATE_FILENAME = "run.json"
TASK_BRIEF_FILENAME = "task-brief.json"
RUN_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


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
    PREPARING_WORKTREE = "preparing_worktree"
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


_LEGAL_NEXT_PHASES: dict[RunPhase, frozenset[RunPhase]] = {
    RunPhase.CREATED: frozenset({RunPhase.PREPARING_WORKTREE}),
    RunPhase.PREPARING_WORKTREE: frozenset({RunPhase.PLANNING}),
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


class RunTransition(BaseModel):
    """One durable, controller-authorized lifecycle transition."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence: int = Field(ge=1)
    source: RunPhase
    target: RunPhase
    iteration_before: int = Field(ge=1, le=3)
    iteration_after: int = Field(ge=1, le=3)
    occurred_at: datetime
    reason: str = Field(min_length=1)
    artifacts: tuple[ArtifactReference, ...] = ()
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
        required: set[ArtifactKind] = set()
        if self.target is RunPhase.FAILED:
            required = {ArtifactKind.FINAL_REPORT}
        else:
            required = {
                (RunPhase.PLANNING, RunPhase.IMPLEMENTING): {
                    ArtifactKind.IMPLEMENTATION_PLAN
                },
                (RunPhase.IMPLEMENTING, RunPhase.SNAPSHOTTING): {
                    ArtifactKind.WORK_RESULT
                },
                (RunPhase.VERIFYING, RunPhase.REVIEWING): {ArtifactKind.TEST_REPORT},
                (RunPhase.REVIEWING, RunPhase.DECIDING): {
                    ArtifactKind.REVIEW_REPORT,
                    ArtifactKind.ITERATION_RECORD,
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
    team_manifest_version: int = Field(ge=1)
    team_definition_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    phase: RunPhase = RunPhase.CREATED
    current_iteration: int = Field(default=1, ge=1, le=3)
    iteration_limit: int = Field(ge=1, le=3)
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

    @model_validator(mode="after")
    def validate_record(self) -> Self:
        """Validate lifecycle history, iteration, and terminal metadata."""

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
            RunPhase.PREPARING_WORKTREE,
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
                        "early worktree failure cannot contain workspace evidence"
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


def _serialized_model(model: BaseModel) -> bytes:
    payload = json.dumps(
        model.model_dump(mode="json"),
        ensure_ascii=False,
        indent=2,
    )
    return f"{payload}\n".encode()


def _model_digest(model: BaseModel) -> str:
    canonical = json.dumps(
        model.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def _write_new_file(path: Path, content: bytes) -> None:
    with path.open("xb") as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[IO[bytes]]:
    with path.open("a+b") as lock_file:
        flock(lock_file.fileno(), LOCK_EX)
        try:
            yield lock_file
        finally:
            flock(lock_file.fileno(), LOCK_UN)


class RunStore:
    """Atomic local persistence for frozen inputs and recoverable run state."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def create(self, task_brief: TaskBrief, record: RunRecord) -> RunRecord:
        """Persist a new run without overwriting any existing run state."""

        if not task_brief.confirmed:
            raise RunIntegrityError("a run requires a confirmed task brief")
        if task_brief.run_id != record.run_id:
            raise RunIntegrityError("task brief and run record IDs must match")
        if _model_digest(task_brief) != record.task_brief_sha256:
            raise RunIntegrityError("task brief digest does not match the run record")

        self.root.mkdir(parents=True, exist_ok=True)
        with _exclusive_lock(self.root / ".lock"):
            run_directory = self._run_directory(record.run_id)
            if run_directory.exists():
                raise RunAlreadyExistsError(f"run already exists: {record.run_id}")

            staging = self.root / f".{record.run_id}.{uuid4().hex}.tmp"
            staging.mkdir(mode=0o700)
            try:
                _write_new_file(
                    staging / TASK_BRIEF_FILENAME,
                    _serialized_model(task_brief),
                )
                _write_new_file(
                    staging / RUN_STATE_FILENAME,
                    _serialized_model(record),
                )
                _fsync_directory(staging)
                os.rename(staging, run_directory)
                _fsync_directory(self.root)
            except Exception:
                if staging.exists():
                    shutil.rmtree(staging)
                raise
        return record

    def load(self, run_id: str) -> tuple[TaskBrief, RunRecord]:
        """Load and verify the frozen input and latest complete run record."""

        run_directory = self._run_directory(run_id)
        if not run_directory.is_dir():
            raise RunNotFoundError(f"run not found: {run_id}")

        try:
            task_brief = TaskBrief.model_validate_json(
                (run_directory / TASK_BRIEF_FILENAME).read_text(encoding="utf-8")
            )
            record = RunRecord.model_validate_json(
                (run_directory / RUN_STATE_FILENAME).read_text(encoding="utf-8")
            )
        except (OSError, ValidationError) as error:
            raise RunIntegrityError(f"persisted run is invalid: {run_id}") from error

        if not task_brief.confirmed:
            raise RunIntegrityError("persisted task brief is not confirmed")
        if task_brief.run_id != run_id or record.run_id != run_id:
            raise RunIntegrityError("persisted run ID does not match its directory")
        if _model_digest(task_brief) != record.task_brief_sha256:
            raise RunIntegrityError("persisted task brief digest does not match")
        return task_brief, record

    def replace(self, previous: RunRecord, updated: RunRecord) -> RunRecord:
        """Atomically append exactly one transition to a current run record."""

        run_directory = self._run_directory(previous.run_id)
        with _exclusive_lock(run_directory / ".lock"):
            _, persisted = self.load(previous.run_id)
            if persisted.revision != previous.revision:
                raise RunConflictError(
                    f"run revision conflict: expected {previous.revision}, "
                    f"found {persisted.revision}"
                )
            if persisted != previous:
                raise RunConflictError("persisted run differs from the expected record")

            self._validate_replacement(previous, updated)
            destination = run_directory / RUN_STATE_FILENAME
            temporary = run_directory / f".{RUN_STATE_FILENAME}.{uuid4().hex}.tmp"
            try:
                _write_new_file(temporary, _serialized_model(updated))
                os.replace(temporary, destination)
                _fsync_directory(run_directory)
            except Exception:
                temporary.unlink(missing_ok=True)
                raise
        return updated

    def _run_directory(self, run_id: str) -> Path:
        if not RUN_ID_PATTERN.fullmatch(run_id):
            raise RunControlError(f"invalid run ID: {run_id}")
        return self.root / run_id

    @staticmethod
    def _validate_replacement(previous: RunRecord, updated: RunRecord) -> None:
        stable_fields = (
            "schema_version",
            "run_id",
            "team_id",
            "team_manifest_version",
            "team_definition_sha256",
            "iteration_limit",
            "task_brief_sha256",
            "created_at",
        )
        if any(
            getattr(previous, field) != getattr(updated, field)
            for field in stable_fields
        ):
            raise RunIntegrityError("immutable run metadata cannot change")
        if updated.revision != previous.revision + 1:
            raise RunIntegrityError("a run update must increment revision once")
        if len(updated.transitions) != len(previous.transitions) + 1:
            raise RunIntegrityError("a run update must append one transition")
        if updated.transitions[:-1] != previous.transitions:
            raise RunIntegrityError("existing transition history cannot change")
        latest = updated.transitions[-1]
        if latest.source is not previous.phase:
            raise RunIntegrityError("new transition must start at the persisted phase")
        if latest.iteration_before != previous.current_iteration:
            raise RunIntegrityError(
                "new transition must start at the persisted iteration"
            )

        if previous.workspace != updated.workspace:
            if not (
                previous.workspace is None
                and updated.workspace is not None
                and latest.source is RunPhase.PREPARING_WORKTREE
                and latest.target is RunPhase.PLANNING
            ):
                raise RunIntegrityError("workspace metadata cannot be replaced")
        elif (
            latest.source is RunPhase.PREPARING_WORKTREE
            and latest.target is RunPhase.PLANNING
        ):
            raise RunIntegrityError("planning transition must attach a workspace")

        if previous.snapshots != updated.snapshots:
            if not (
                len(updated.snapshots) == len(previous.snapshots) + 1
                and updated.snapshots[:-1] == previous.snapshots
                and latest.source is RunPhase.SNAPSHOTTING
                and latest.target is RunPhase.VERIFYING
            ):
                raise RunIntegrityError("snapshot history must append exactly once")
        elif (
            latest.source is RunPhase.SNAPSHOTTING
            and latest.target is RunPhase.VERIFYING
        ):
            raise RunIntegrityError("verification transition must attach a snapshot")


Clock = Callable[[], datetime]


def _system_clock() -> datetime:
    return datetime.now(UTC)


class RunController:
    """Only authority allowed to create and advance persisted run state."""

    def __init__(
        self,
        store: RunStore,
        manifest: TeamManifest,
        *,
        clock: Clock = _system_clock,
    ) -> None:
        self.store = store
        self.manifest = manifest
        self.clock = clock

    def create(
        self,
        task_brief: TaskBrief,
        *,
        team_id: str,
        iteration_limit: int,
    ) -> RunRecord:
        """Create the initial recoverable record for a confirmed task brief."""

        if not task_brief.confirmed:
            raise RunControlError("a run requires a confirmed task brief")
        team = self.manifest.get_team(team_id)
        if not 1 <= iteration_limit <= team.max_iterations:
            raise RunControlError(
                f"iteration limit must be between 1 and {team.max_iterations} "
                f"for {team_id}"
            )

        now = _require_utc(self.clock())
        record = RunRecord(
            run_id=task_brief.run_id,
            team_id=team_id,
            team_manifest_version=self.manifest.schema_version,
            team_definition_sha256=_model_digest(team),
            iteration_limit=iteration_limit,
            task_brief_sha256=_model_digest(task_brief),
            created_at=now,
            updated_at=now,
        )
        return self.store.create(task_brief, record)

    def load(self, run_id: str) -> RunRecord:
        """Recover the latest complete state without inferring new work."""

        _, record = self.store.load(run_id)
        self._validate_manifest_boundary(record)
        return record

    def advance(
        self,
        run_id: str,
        *,
        expected_revision: int,
        target: RunPhase,
        reason: str,
        artifacts: tuple[ArtifactReference, ...] = (),
        decision: IterationDecision | None = None,
    ) -> RunRecord:
        """Apply one non-terminal lifecycle transition."""

        if target.is_terminal:
            raise InvalidRunTransitionError(
                "use complete() or fail() for terminal transitions"
            )
        current = self._load_expected(run_id, expected_revision)
        if current.phase is RunPhase.PREPARING_WORKTREE and target is RunPhase.PLANNING:
            raise InvalidRunTransitionError("use attach_workspace() to enter planning")
        if current.phase is RunPhase.SNAPSHOTTING and target is RunPhase.VERIFYING:
            raise InvalidRunTransitionError(
                "use record_snapshot() to enter verification"
            )
        if not _is_legal_transition(current.phase, target):
            raise InvalidRunTransitionError(
                f"illegal run transition: {current.phase.value} -> {target.value}"
            )

        iteration_after = current.current_iteration
        if current.phase is RunPhase.DECIDING and target is RunPhase.IMPLEMENTING:
            if current.current_iteration >= current.iteration_limit:
                raise InvalidRunTransitionError(
                    "run iteration limit prevents another revision"
                )
            iteration_after += 1

        return self._transition(
            current,
            target=target,
            iteration_after=iteration_after,
            reason=reason,
            artifacts=artifacts,
            decision=decision,
        )

    def attach_workspace(
        self,
        run_id: str,
        *,
        expected_revision: int,
        workspace: GitWorkspace,
        reason: str = "isolated worktree prepared",
    ) -> RunRecord:
        """Attach verified worktree evidence and enter planning."""

        current = self._load_expected(run_id, expected_revision)
        if current.phase is not RunPhase.PREPARING_WORKTREE:
            raise InvalidRunTransitionError(
                f"cannot attach a workspace from {current.phase.value}"
            )
        if workspace.run_id != current.run_id:
            raise RunIntegrityError("workspace belongs to a different run")
        return self._transition(
            current,
            target=RunPhase.PLANNING,
            iteration_after=current.current_iteration,
            reason=reason,
            record_updates={"workspace": workspace},
        )

    def record_snapshot(
        self,
        run_id: str,
        *,
        expected_revision: int,
        snapshot: GitSnapshot,
        reason: str = "iteration commit snapshot verified",
    ) -> RunRecord:
        """Append verified commit evidence and enter deterministic verification."""

        current = self._load_expected(run_id, expected_revision)
        if current.phase is not RunPhase.SNAPSHOTTING:
            raise InvalidRunTransitionError(
                f"cannot record a snapshot from {current.phase.value}"
            )
        if snapshot.run_id != current.run_id:
            raise RunIntegrityError("snapshot belongs to a different run")
        if snapshot.iteration != current.current_iteration:
            raise RunIntegrityError("snapshot iteration does not match the run")
        if snapshot.input_commit != current.current_commit:
            raise RunIntegrityError("snapshot input does not match the current commit")
        return self._transition(
            current,
            target=RunPhase.VERIFYING,
            iteration_after=current.current_iteration,
            reason=reason,
            record_updates={"snapshots": (*current.snapshots, snapshot)},
        )

    def complete(
        self,
        run_id: str,
        *,
        expected_revision: int,
        detail: str,
        final_report: ArtifactReference,
    ) -> RunRecord:
        """Complete a delivering run with an explicit success record."""

        current = self._load_expected(run_id, expected_revision)
        if current.phase is not RunPhase.DELIVERING:
            raise InvalidRunTransitionError(
                f"cannot complete a run from {current.phase.value}"
            )
        return self._transition(
            current,
            target=RunPhase.COMPLETED,
            iteration_after=current.current_iteration,
            reason="delivery completed",
            artifacts=(final_report,),
            termination_reason=TerminationReason.SUCCEEDED,
            termination_detail=detail,
        )

    def fail(
        self,
        run_id: str,
        *,
        expected_revision: int,
        reason: TerminationReason,
        detail: str,
        final_report: ArtifactReference,
    ) -> RunRecord:
        """Persist an explicit failure from any non-terminal run phase."""

        if reason is TerminationReason.SUCCEEDED:
            raise InvalidRunTransitionError("failure cannot use the succeeded reason")
        current = self._load_expected(run_id, expected_revision)
        if current.phase.is_terminal:
            raise InvalidRunTransitionError("terminal runs cannot transition again")
        return self._transition(
            current,
            target=RunPhase.FAILED,
            iteration_after=current.current_iteration,
            reason=detail,
            artifacts=(final_report,),
            termination_reason=reason,
            termination_detail=detail,
        )

    def _load_expected(self, run_id: str, expected_revision: int) -> RunRecord:
        current = self.load(run_id)
        if current.revision != expected_revision:
            raise RunConflictError(
                f"run revision conflict: expected {expected_revision}, "
                f"found {current.revision}"
            )
        if current.phase.is_terminal:
            raise InvalidRunTransitionError("terminal runs cannot transition again")
        return current

    def _transition(
        self,
        current: RunRecord,
        *,
        target: RunPhase,
        iteration_after: int,
        reason: str,
        artifacts: tuple[ArtifactReference, ...] = (),
        decision: IterationDecision | None = None,
        termination_reason: TerminationReason | None = None,
        termination_detail: str | None = None,
        record_updates: Mapping[str, object] | None = None,
    ) -> RunRecord:
        now = _require_utc(self.clock())
        if now < current.updated_at:
            raise RunControlError("controller clock moved backwards")
        transition = RunTransition(
            sequence=current.revision + 1,
            source=current.phase,
            target=target,
            iteration_before=current.current_iteration,
            iteration_after=iteration_after,
            occurred_at=now,
            reason=reason,
            artifacts=artifacts,
            decision=decision,
        )
        updates: dict[str, object] = {
            "phase": target,
            "current_iteration": iteration_after,
            "revision": current.revision + 1,
            "updated_at": now,
            "termination_reason": termination_reason,
            "termination_detail": termination_detail,
            "transitions": (*current.transitions, transition),
        }
        if record_updates:
            reserved = set(updates) & set(record_updates)
            if reserved:
                raise RunIntegrityError(
                    f"record update cannot replace controller fields: "
                    f"{', '.join(sorted(reserved))}"
                )
            updates.update(record_updates)
        updated = current.model_copy(update=updates)
        updated = RunRecord.model_validate(updated.model_dump())
        return self.store.replace(current, updated)

    def _validate_manifest_boundary(self, record: RunRecord) -> None:
        if record.team_manifest_version != self.manifest.schema_version:
            raise RunIntegrityError(
                "persisted run uses a different team manifest version"
            )
        team = self.manifest.get_team(record.team_id)
        if record.team_definition_sha256 != _model_digest(team):
            raise RunIntegrityError("persisted run uses a different team definition")
        if record.iteration_limit > team.max_iterations:
            raise RunIntegrityError(
                "persisted iteration limit exceeds the selected team limit"
            )
