"""Thread-safe, append-only, hash-chained RunEvent journal persistence."""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from software_agent_team.integrity import canonical_model_sha256
from software_agent_team.run_control import RunPhase

from .models import (
    EventAnchorReader,
    EventAnchorWriter,
    EventClock,
    ProgressEvent,
    ProgressHandler,
    RunEvent,
    _current_agent_state,
    _require_utc,
)
from .types import (
    _EVENT_METADATA,
    EVENT_FILENAME_PATTERN,
    EVENTS_DIRECTORY,
)


def _system_clock() -> datetime:
    return datetime.now(UTC)


def _serialized_event(event: RunEvent) -> bytes:
    payload = json.dumps(event.model_dump(mode="json"), ensure_ascii=False, indent=2)
    return f"{payload}\n".encode()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class RunEventJournal:
    """Thread-safe, append-only, hash-chained RunEvent persistence."""

    def __init__(
        self,
        run_directory: Path,
        *,
        run_id: str,
        handler: ProgressHandler | None = None,
        clock: EventClock = _system_clock,
        anchor_writer: EventAnchorWriter | None = None,
        anchor_reader: EventAnchorReader | None = None,
    ) -> None:
        if run_directory.is_symlink() or not run_directory.is_dir():
            raise ValueError("RunEvent journal requires an existing run directory")
        self.run_directory = run_directory
        self.run_id = run_id
        self.handler = handler
        self.clock = clock
        if (anchor_writer is None) != (anchor_reader is None):
            raise ValueError("RunEvent anchoring requires both read and write sides")
        self.anchor_writer = anchor_writer
        self.anchor_reader = anchor_reader
        self.events_directory = run_directory / EVENTS_DIRECTORY
        if self.events_directory.is_symlink():
            raise ValueError("RunEvent directory cannot be a symbolic link")
        self.events_directory.mkdir(mode=0o700, exist_ok=True)
        self._lock = threading.Lock()
        self.render_errors: list[str] = []

    def append(
        self,
        draft: ProgressEvent,
        *,
        lifecycle_revision: int,
        phase: RunPhase,
    ) -> RunEvent:
        """Enrich, persist, and then render one controller event."""

        if draft.phase is not None and draft.phase is not phase:
            raise ValueError("RunEvent phase differs from controller state")
        with self._lock:
            existing = self._load_unlocked()
            self._verify_anchor_unlocked(existing)
            previous = existing[-1] if existing else None
            occurred_at = _require_utc(self.clock())
            if previous is not None and occurred_at < previous.occurred_at:
                raise ValueError("RunEvent timestamps must be monotonic")
            category, visibility, _ = _EVENT_METADATA[draft.kind]
            event = RunEvent(
                run_id=self.run_id,
                sequence=len(existing) + 1,
                occurred_at=occurred_at,
                lifecycle_revision=lifecycle_revision,
                kind=draft.kind,
                category=category,
                minimum_visibility=visibility,
                source=draft.source,
                summary=draft.message,
                phase=draft.phase or phase,
                agent_id=draft.agent_id,
                agent_state=_current_agent_state(draft.kind, draft.checkpoint),
                iteration=draft.iteration,
                attempt=draft.attempt,
                duration_ms=draft.duration_ms,
                capability=draft.capability,
                specialization=draft.specialization,
                stage_id=draft.stage_id,
                model=draft.model,
                dependency_ids=draft.dependency_ids,
                budget_usage=draft.budget_usage,
                checkpoint=draft.checkpoint,
                presentation=draft.presentation,
                completed=draft.completed,
                total=draft.total,
                changed_files=draft.changed_files,
                decision=draft.decision,
                references=draft.references,
                control_command_id=draft.control_command_id,
                previous_event_sha256=(
                    None if previous is None else canonical_model_sha256(previous)
                ),
            )
            destination = self._write_unlocked(event)
            if self.anchor_writer is not None:
                try:
                    self.anchor_writer(event)
                except Exception:
                    destination.unlink(missing_ok=True)
                    _fsync_directory(self.events_directory)
                    raise

        if self.handler is not None:
            try:
                self.handler(event)
            except Exception as error:
                detail = " ".join(str(error).split()) or "renderer failed"
                self.render_errors.append(f"{type(error).__name__}: {detail[:400]}")
        return event

    def load(self) -> tuple[RunEvent, ...]:
        """Load and verify every complete event and its predecessor chain."""

        with self._lock:
            events = self._load_unlocked()
            self._verify_anchor_unlocked(events)
            return events

    def _load_unlocked(self) -> tuple[RunEvent, ...]:
        paths = sorted(
            path
            for path in self.events_directory.iterdir()
            if not path.name.startswith(".")
        )
        events: list[RunEvent] = []
        for expected_sequence, path in enumerate(paths, start=1):
            match = EVENT_FILENAME_PATTERN.fullmatch(path.name)
            if match is None or path.is_symlink() or not path.is_file():
                raise ValueError("RunEvent journal contains an invalid entry")
            if int(match.group("sequence")) != expected_sequence:
                raise ValueError("RunEvent filenames must form a contiguous sequence")
            try:
                event = RunEvent.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                raise ValueError("RunEvent journal contains invalid JSON") from error
            if event.run_id != self.run_id or event.sequence != expected_sequence:
                raise ValueError("RunEvent identity does not match its journal")
            previous = events[-1] if events else None
            expected_digest = (
                None if previous is None else canonical_model_sha256(previous)
            )
            if event.previous_event_sha256 != expected_digest:
                raise ValueError("RunEvent predecessor digest does not match")
            if previous is not None:
                if event.occurred_at < previous.occurred_at:
                    raise ValueError("RunEvent timestamps must be monotonic")
                if event.lifecycle_revision < previous.lifecycle_revision:
                    raise ValueError("RunEvent lifecycle revisions must be monotonic")
            events.append(event)
        return tuple(events)

    def _verify_anchor_unlocked(self, events: tuple[RunEvent, ...]) -> None:
        if self.anchor_reader is None:
            return
        count, digest = self.anchor_reader()
        expected_digest = None if not events else canonical_model_sha256(events[-1])
        if count != len(events) or digest != expected_digest:
            raise ValueError("RunEvent journal does not match its run-state anchor")

    def _write_unlocked(self, event: RunEvent) -> Path:
        destination = self.events_directory / f"{event.sequence:06d}.json"
        if destination.exists():
            raise ValueError("RunEvent sequence already exists")
        temporary = self.events_directory / f".{event.sequence:06d}.{uuid4().hex}.tmp"
        try:
            with temporary.open("xb") as output:
                output.write(_serialized_event(event))
                output.flush()
                os.fsync(output.fileno())
            os.rename(temporary, destination)
            _fsync_directory(self.events_directory)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return destination
