"""Durable state persistence, atomic staging, and file-level locking.

运行状态持久化与并发控制 - 提供基于文件系统和 fcntl 排他锁的原子存储实现。
确保多进程或多协程并发访问下的数据完整性与崩溃一致性。
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from fcntl import LOCK_EX, LOCK_UN, flock
from pathlib import Path
from typing import IO
from uuid import uuid4

from pydantic import ValidationError

from software_agent_team.artifacts import ArtifactKind, TaskBrief
from software_agent_team.integrity import canonical_model_sha256
from software_agent_team.teams import TeamPlan, TeamPlanOrigin

from .types import (
    RUN_ID_PATTERN,
    RUN_STATE_FILENAME,
    TASK_BRIEF_FILENAME,
    TEAM_PLAN_FILENAME,
    RunAlreadyExistsError,
    RunConflictError,
    RunControlError,
    RunIntegrityError,
    RunNotFoundError,
    RunPhase,
    RunRecord,
    _serialized_model,
)


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


def _system_clock() -> datetime:
    return datetime.now(UTC)


class RunStore:
    """Atomic local persistence for frozen inputs and recoverable run state."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def create(
        self,
        task_brief: TaskBrief,
        team_plan: TeamPlan,
        record: RunRecord,
    ) -> RunRecord:
        """Persist a new run without overwriting any existing run state."""

        if not task_brief.confirmed:
            raise RunIntegrityError("a run requires a confirmed task brief")
        if task_brief.run_id != record.run_id:
            raise RunIntegrityError("task brief and run record IDs must match")
        if team_plan.run_id != record.run_id:
            raise RunIntegrityError("team plan and run record IDs must match")
        if canonical_model_sha256(task_brief) != record.task_brief_sha256:
            raise RunIntegrityError("task brief digest does not match the run record")
        if team_plan.task_brief_sha256 != record.task_brief_sha256:
            raise RunIntegrityError("team plan does not bind the frozen task brief")
        if canonical_model_sha256(team_plan) != record.team_plan_sha256:
            raise RunIntegrityError("team plan digest does not match the run record")

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
                    staging / TEAM_PLAN_FILENAME,
                    _serialized_model(team_plan),
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

    def load(self, run_id: str) -> tuple[TaskBrief, TeamPlan, RunRecord]:
        """Load and verify the frozen input and latest complete run record."""

        run_directory = self._run_directory(run_id)
        if not run_directory.is_dir():
            raise RunNotFoundError(f"run not found: {run_id}")

        try:
            task_brief = TaskBrief.model_validate_json(
                (run_directory / TASK_BRIEF_FILENAME).read_text(encoding="utf-8")
            )
            team_plan = TeamPlan.model_validate_json(
                (run_directory / TEAM_PLAN_FILENAME).read_text(encoding="utf-8")
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
        if team_plan.run_id != run_id:
            raise RunIntegrityError("persisted team plan run ID does not match")
        if canonical_model_sha256(task_brief) != record.task_brief_sha256:
            raise RunIntegrityError("persisted task brief digest does not match")
        if team_plan.task_brief_sha256 != record.task_brief_sha256:
            raise RunIntegrityError("persisted TeamPlan binds a different task brief")
        if canonical_model_sha256(team_plan) != record.team_plan_sha256:
            raise RunIntegrityError("persisted team plan digest does not match")
        self._validate_transition_plan_boundary(team_plan, record)
        return task_brief, team_plan, record

    def replace(self, previous: RunRecord, updated: RunRecord) -> RunRecord:
        """Atomically append exactly one transition to a current run record."""

        run_directory = self._run_directory(previous.run_id)
        with _exclusive_lock(run_directory / ".lock"):
            _, team_plan, persisted = self.load(previous.run_id)
            if persisted.revision != previous.revision:
                raise RunConflictError(
                    f"run revision conflict: expected {previous.revision}, "
                    f"found {persisted.revision}"
                )
            if persisted != previous:
                raise RunConflictError("persisted run differs from the expected record")

            self._validate_replacement(previous, updated)
            self._validate_transition_plan_boundary(team_plan, updated)
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

    def replace_event_head(
        self,
        previous: RunRecord,
        updated: RunRecord,
    ) -> RunRecord:
        """Atomically anchor one newly persisted append-only RunEvent."""

        run_directory = self._run_directory(previous.run_id)
        with _exclusive_lock(run_directory / ".lock"):
            _, _, persisted = self.load(previous.run_id)
            if persisted != previous:
                raise RunConflictError(
                    "persisted run changed before its RunEvent was anchored"
                )
            stable_fields = set(RunRecord.model_fields) - {
                "updated_at",
                "event_count",
                "event_head_sha256",
            }
            if any(
                getattr(previous, field) != getattr(updated, field)
                for field in stable_fields
            ):
                raise RunIntegrityError(
                    "RunEvent anchoring cannot change lifecycle state"
                )
            if updated.event_count != previous.event_count + 1:
                raise RunIntegrityError("RunEvent count must increment exactly once")
            if updated.event_head_sha256 is None:
                raise RunIntegrityError("RunEvent anchoring requires a head digest")
            if updated.updated_at < previous.updated_at:
                raise RunIntegrityError("RunEvent timestamp predates run state")
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
            "team_plan_revision",
            "team_plan_sha256",
            "agent_timeouts_seconds",
            "event_count",
            "event_head_sha256",
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
                and latest.source is RunPhase.PREPARING_WORKSPACE
                and latest.target is RunPhase.PLANNING
            ):
                raise RunIntegrityError("workspace metadata cannot be replaced")
        elif (
            latest.source is RunPhase.PREPARING_WORKSPACE
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

    @staticmethod
    def _validate_transition_plan_boundary(
        team_plan: TeamPlan,
        record: RunRecord,
    ) -> None:
        """Bind lifecycle plan and review evidence to the frozen TeamPlan."""

        for transition in record.transitions:
            if (
                transition.source is RunPhase.PLANNING
                and transition.target is RunPhase.IMPLEMENTING
            ):
                if team_plan.origin is TeamPlanOrigin.ADAPTIVE_PLANNING:
                    if transition.implementation_plan_sha256 != (
                        team_plan.implementation_plan_sha256
                    ):
                        raise RunIntegrityError(
                            "planning transition differs from the approved "
                            "implementation plan"
                        )
                elif transition.implementation_plan_sha256 is not None:
                    raise RunIntegrityError(
                        "fixed planning transition cannot claim an adaptive digest"
                    )
                if team_plan.team_id == "single_agent":
                    if transition.artifacts:
                        raise RunIntegrityError(
                            "single-agent baseline cannot claim a planning artifact"
                        )
                elif team_plan.origin is TeamPlanOrigin.FIXED_MANIFEST and not any(
                    item.kind is ArtifactKind.IMPLEMENTATION_PLAN
                    for item in transition.artifacts
                ):
                    raise RunIntegrityError(
                        "fixed team planning transition lacks its plan artifact"
                    )


__all__ = [
    "RunStore",
    "_exclusive_lock",
    "_fsync_directory",
    "_system_clock",
    "_write_new_file",
]
