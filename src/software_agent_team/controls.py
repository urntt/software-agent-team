"""Persisted user-control commands and integrity-checked state changes."""

from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from enum import StrEnum
from fcntl import LOCK_EX, LOCK_UN, flock
from pathlib import Path
from threading import Lock
from typing import IO, Literal, Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from software_agent_team.integrity import canonical_model_sha256
from software_agent_team.run_control import RunPhase

CONTROL_COMMAND_SCHEMA_VERSION = 2
CONTROLS_DIRECTORY = "controls"
CONTROL_ID_PATTERN = re.compile(r"^ctl-[a-z0-9][a-z0-9-]*$")
CONTROL_REVISION_FILENAME_PATTERN = re.compile(r"^(?P<revision>[0-9]{6})\.json$")


class ControlCommandError(ValueError):
    """Base error for control-command validation and persistence."""


class ControlCommandConflictError(ControlCommandError):
    """Raised when a caller resolves an obsolete command revision."""


class ControlCommandType(StrEnum):
    """Supported user intents at the controller boundary."""

    GUIDE = "guide"
    CORRECT = "correct"
    PAUSE = "pause"
    RESUME = "resume"
    INTERRUPT = "interrupt"
    CANCEL = "cancel"


class ControlCommandStatus(StrEnum):
    """Persisted application state for one requested command."""

    QUEUED = "queued"
    APPLIED = "applied"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"
    BEST_EFFORT_FAILED = "best_effort_failed"

    @property
    def is_terminal(self) -> bool:
        return self is not ControlCommandStatus.QUEUED


class ControlRequester(StrEnum):
    """Authenticated actor that submitted a control request."""

    USER = "user"


class ControlTargetKind(StrEnum):
    """Scope to which one command is intended to apply."""

    RUN = "run"
    AGENT = "agent"
    PHASE = "phase"
    FUTURE_WORK = "future_work"


class ControlApplicationBoundary(StrEnum):
    """Controller checkpoint requested for command application."""

    IMMEDIATE = "immediate"
    BEFORE_NEXT_INVOCATION = "before_next_invocation"
    NEXT_SAFE_CHECKPOINT = "next_safe_checkpoint"
    PLANNING_REVISION = "planning_revision"


class ControlTarget(BaseModel):
    """Typed target without free-form controller authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ControlTargetKind
    agent_id: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    attempt: int | None = Field(default=None, ge=1, le=99)
    phase: RunPhase | None = None

    @model_validator(mode="after")
    def validate_target(self) -> Self:
        if self.kind is ControlTargetKind.AGENT:
            if self.agent_id is None:
                raise ValueError("Agent control targets require an Agent ID")
            if self.phase is not None:
                raise ValueError("Agent control targets cannot claim a phase")
        elif self.agent_id is not None or self.attempt is not None:
            raise ValueError("only Agent control targets may identify an attempt")
        if self.kind is ControlTargetKind.PHASE:
            if self.phase is None:
                raise ValueError("phase control targets require a phase")
        elif self.phase is not None:
            raise ValueError("only phase control targets may identify a phase")
        return self


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("control-command timestamps must include a timezone")
    return value.astimezone(UTC)


def _clean_text(value: str, *, label: str, allow_newlines: bool) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{label} must not be blank")
    if "\x00" in cleaned or (
        not allow_newlines and ("\n" in cleaned or "\r" in cleaned)
    ):
        raise ValueError(f"{label} contains unsupported control characters")
    return cleaned


class ControlCommand(BaseModel):
    """One immutable revision in a user-control request history."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[CONTROL_COMMAND_SCHEMA_VERSION] = (
        CONTROL_COMMAND_SCHEMA_VERSION
    )
    command_id: str = Field(pattern=r"^ctl-[a-z0-9][a-z0-9-]*$")
    run_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    request_sequence: int = Field(ge=1)
    revision: int = Field(ge=1, le=99)
    requested_at: datetime
    updated_at: datetime
    requester: ControlRequester = ControlRequester.USER
    command: ControlCommandType
    instruction: str | None = Field(default=None, max_length=2_000)
    target: ControlTarget
    application_boundary: ControlApplicationBoundary
    status: ControlCommandStatus
    consequence: str | None = Field(default=None, max_length=500)
    provider_cost_caveat: str | None = Field(default=None, max_length=500)
    resulting_plan_revision: int | None = Field(default=None, ge=1, le=99)
    resulting_lifecycle_revision: int | None = Field(default=None, ge=0)
    previous_revision_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )

    @field_validator("requested_at", "updated_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @field_validator("instruction")
    @classmethod
    def require_clean_instruction(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _clean_text(value, label="control instruction", allow_newlines=True)

    @field_validator("consequence", "provider_cost_caveat")
    @classmethod
    def require_safe_result_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _clean_text(value, label="control result", allow_newlines=False)

    @model_validator(mode="after")
    def validate_command(self) -> Self:
        if self.updated_at < self.requested_at:
            raise ValueError("control command cannot predate its request")
        if self.revision == 1:
            if self.previous_revision_sha256 is not None:
                raise ValueError("the first control revision has no predecessor")
            if self.status is not ControlCommandStatus.QUEUED:
                raise ValueError("a new control command must be queued")
        elif self.previous_revision_sha256 is None:
            raise ValueError("later control revisions require a predecessor digest")

        if self.status is ControlCommandStatus.QUEUED:
            if self.revision != 1 or self.updated_at != self.requested_at:
                raise ValueError("only the first control revision may remain queued")
            if any(
                value is not None
                for value in (
                    self.consequence,
                    self.provider_cost_caveat,
                    self.resulting_plan_revision,
                    self.resulting_lifecycle_revision,
                )
            ):
                raise ValueError("queued control commands cannot contain a result")
        elif self.consequence is None:
            raise ValueError("resolved control commands require a consequence")

        if self.status is not ControlCommandStatus.APPLIED and any(
            value is not None
            for value in (
                self.resulting_plan_revision,
                self.resulting_lifecycle_revision,
            )
        ):
            raise ValueError("only applied controls may record resulting revisions")

        if self.command in {ControlCommandType.GUIDE, ControlCommandType.CORRECT}:
            if self.instruction is None:
                raise ValueError(f"{self.command.value} requires an instruction")
        elif self.instruction is not None:
            raise ValueError(f"{self.command.value} does not accept an instruction")

        allowed_targets = {
            ControlCommandType.GUIDE: {
                ControlTargetKind.AGENT,
                ControlTargetKind.PHASE,
                ControlTargetKind.FUTURE_WORK,
            },
            ControlCommandType.CORRECT: {
                ControlTargetKind.RUN,
                ControlTargetKind.FUTURE_WORK,
            },
            ControlCommandType.PAUSE: {ControlTargetKind.RUN},
            ControlCommandType.RESUME: {ControlTargetKind.RUN},
            ControlCommandType.INTERRUPT: {ControlTargetKind.AGENT},
            ControlCommandType.CANCEL: {ControlTargetKind.RUN},
        }[self.command]
        if self.target.kind not in allowed_targets:
            raise ValueError(f"invalid target for {self.command.value}")

        allowed_boundaries = {
            ControlCommandType.GUIDE: {
                ControlApplicationBoundary.BEFORE_NEXT_INVOCATION,
                ControlApplicationBoundary.NEXT_SAFE_CHECKPOINT,
            },
            ControlCommandType.CORRECT: {
                ControlApplicationBoundary.PLANNING_REVISION,
            },
            ControlCommandType.PAUSE: {
                ControlApplicationBoundary.NEXT_SAFE_CHECKPOINT,
            },
            ControlCommandType.RESUME: {
                ControlApplicationBoundary.NEXT_SAFE_CHECKPOINT,
            },
            ControlCommandType.INTERRUPT: {
                ControlApplicationBoundary.IMMEDIATE,
            },
            ControlCommandType.CANCEL: {
                ControlApplicationBoundary.IMMEDIATE,
            },
        }[self.command]
        if self.application_boundary not in allowed_boundaries:
            raise ValueError(f"invalid application boundary for {self.command.value}")
        return self


ControlClock = Callable[[], datetime]


def _system_clock() -> datetime:
    return datetime.now(UTC)


def new_control_command_id() -> str:
    """Return a collision-resistant public identifier without hidden meaning."""

    return f"ctl-{uuid4().hex}"


def _serialized_command(command: ControlCommand) -> bytes:
    payload = json.dumps(
        command.model_dump(mode="json"),
        ensure_ascii=False,
        indent=2,
    )
    return f"{payload}\n".encode()


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


class ControlCommandStore:
    """Controller-owned revision store for one run's local control mailbox."""

    def __init__(
        self,
        run_directory: Path,
        *,
        run_id: str,
        clock: ControlClock = _system_clock,
    ) -> None:
        if run_directory.is_symlink() or not run_directory.is_dir():
            raise ControlCommandError(
                "control store requires an existing run directory"
            )
        self.run_directory = run_directory
        self.run_id = run_id
        self.clock = clock
        self.controls_directory = run_directory / CONTROLS_DIRECTORY
        if self.controls_directory.is_symlink():
            raise ControlCommandError("control directory cannot be a symbolic link")
        self.controls_directory.mkdir(mode=0o700, exist_ok=True)
        self._thread_lock = Lock()

    def request(
        self,
        *,
        command: ControlCommandType,
        target: ControlTarget,
        application_boundary: ControlApplicationBoundary,
        instruction: str | None = None,
        command_id: str | None = None,
    ) -> ControlCommand:
        """Persist one queued request without applying it to execution."""

        with self._thread_lock, _exclusive_lock(self.controls_directory / ".lock"):
            entries = self._validated_command_directories_unlocked()
            histories = [self._load_unlocked(path.name) for path in entries]
            sequences = sorted(history[0].request_sequence for history in histories)
            if sequences != list(range(1, len(sequences) + 1)):
                raise ControlCommandError(
                    "control mailbox request sequences must be contiguous"
                )
            requested_at = _require_utc(self.clock())
            record = ControlCommand(
                command_id=command_id or new_control_command_id(),
                run_id=self.run_id,
                request_sequence=len(sequences) + 1,
                revision=1,
                requested_at=requested_at,
                updated_at=requested_at,
                command=command,
                instruction=instruction,
                target=target,
                application_boundary=application_boundary,
                status=ControlCommandStatus.QUEUED,
            )
            command_directory = self.controls_directory / record.command_id
            if command_directory.exists():
                raise ControlCommandError(
                    f"control command already exists: {record.command_id}"
                )
            staging = (
                self.controls_directory / f".{record.command_id}.{uuid4().hex}.tmp"
            )
            staging.mkdir(mode=0o700)
            try:
                _write_new_file(staging / "000001.json", _serialized_command(record))
                _fsync_directory(staging)
                os.rename(staging, command_directory)
                _fsync_directory(self.controls_directory)
            except Exception:
                if staging.exists():
                    shutil.rmtree(staging)
                raise
        return record

    def resolve(
        self,
        command_id: str,
        *,
        expected_revision: int,
        status: ControlCommandStatus,
        consequence: str,
        provider_cost_caveat: str | None = None,
        resulting_plan_revision: int | None = None,
        resulting_lifecycle_revision: int | None = None,
    ) -> ControlCommand:
        """Record one terminal controller decision for a queued command."""

        if not status.is_terminal:
            raise ControlCommandError("resolve requires a terminal control status")
        self._validate_command_id(command_id)
        with self._thread_lock, _exclusive_lock(self.controls_directory / ".lock"):
            history = self._load_unlocked(command_id)
            current = history[-1]
            if current.revision != expected_revision:
                raise ControlCommandConflictError(
                    "control revision conflict: "
                    f"expected {expected_revision}, found {current.revision}"
                )
            if current.status.is_terminal:
                raise ControlCommandError("resolved control commands are immutable")
            updated = ControlCommand(
                **{
                    **current.model_dump(),
                    "revision": current.revision + 1,
                    "updated_at": _require_utc(self.clock()),
                    "status": status,
                    "consequence": consequence,
                    "provider_cost_caveat": provider_cost_caveat,
                    "resulting_plan_revision": resulting_plan_revision,
                    "resulting_lifecycle_revision": resulting_lifecycle_revision,
                    "previous_revision_sha256": canonical_model_sha256(current),
                }
            )
            destination = (
                self.controls_directory / command_id / f"{updated.revision:06d}.json"
            )
            temporary = destination.parent / f".{destination.name}.{uuid4().hex}.tmp"
            try:
                _write_new_file(temporary, _serialized_command(updated))
                os.rename(temporary, destination)
                _fsync_directory(destination.parent)
            except Exception:
                temporary.unlink(missing_ok=True)
                raise
        return updated

    def load(self, command_id: str) -> tuple[ControlCommand, ...]:
        """Load and verify the complete revision chain for one command."""

        self._validate_command_id(command_id)
        with self._thread_lock, _exclusive_lock(self.controls_directory / ".lock"):
            return self._load_unlocked(command_id)

    def list_latest(self) -> tuple[ControlCommand, ...]:
        """Return the verified latest revision of every requested command."""

        with self._thread_lock, _exclusive_lock(self.controls_directory / ".lock"):
            entries = self._validated_command_directories_unlocked()
            command_ids = [path.name for path in entries]
            latest = tuple(
                self._load_unlocked(command_id)[-1] for command_id in command_ids
            )
            ordered = tuple(sorted(latest, key=lambda item: item.request_sequence))
            if [item.request_sequence for item in ordered] != list(
                range(1, len(ordered) + 1)
            ):
                raise ControlCommandError(
                    "control mailbox request sequences must be contiguous"
                )
            return ordered

    def _validated_command_directories_unlocked(self) -> tuple[Path, ...]:
        entries = tuple(
            sorted(
                path
                for path in self.controls_directory.iterdir()
                if not path.name.startswith(".")
            )
        )
        for path in entries:
            if (
                path.is_symlink()
                or not path.is_dir()
                or CONTROL_ID_PATTERN.fullmatch(path.name) is None
            ):
                raise ControlCommandError("control mailbox contains an invalid entry")
        return entries

    def _load_unlocked(self, command_id: str) -> tuple[ControlCommand, ...]:
        command_directory = self.controls_directory / command_id
        if command_directory.is_symlink() or not command_directory.is_dir():
            raise ControlCommandError(f"control command not found: {command_id}")
        paths = sorted(
            path
            for path in command_directory.iterdir()
            if not path.name.startswith(".")
        )
        history: list[ControlCommand] = []
        for expected_revision, path in enumerate(paths, start=1):
            match = CONTROL_REVISION_FILENAME_PATTERN.fullmatch(path.name)
            if match is None or path.is_symlink() or not path.is_file():
                raise ControlCommandError("control history contains an invalid entry")
            if int(match.group("revision")) != expected_revision:
                raise ControlCommandError("control revisions must be contiguous")
            try:
                record = ControlCommand.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError) as error:
                raise ControlCommandError(
                    "control history contains invalid JSON"
                ) from error
            if (
                record.command_id != command_id
                or record.run_id != self.run_id
                or record.revision != expected_revision
            ):
                raise ControlCommandError("control identity does not match its path")
            previous = history[-1] if history else None
            expected_digest = (
                None if previous is None else canonical_model_sha256(previous)
            )
            if record.previous_revision_sha256 != expected_digest:
                raise ControlCommandError("control predecessor digest does not match")
            if previous is not None:
                immutable_fields = (
                    "command_id",
                    "run_id",
                    "request_sequence",
                    "requested_at",
                    "requester",
                    "command",
                    "instruction",
                    "target",
                    "application_boundary",
                )
                if any(
                    getattr(record, field) != getattr(previous, field)
                    for field in immutable_fields
                ):
                    raise ControlCommandError("control request metadata is immutable")
                if previous.status.is_terminal:
                    raise ControlCommandError(
                        "terminal control history cannot continue"
                    )
            history.append(record)
        if not history:
            raise ControlCommandError(f"control command not found: {command_id}")
        return tuple(history)

    @staticmethod
    def _validate_command_id(command_id: str) -> None:
        if CONTROL_ID_PATTERN.fullmatch(command_id) is None:
            raise ControlCommandError(f"invalid control command ID: {command_id}")
