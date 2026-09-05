"""Persisted controller events and user-safe terminal rendering."""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Literal, Self, TextIO
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from software_agent_team.artifacts import IterationDecision
from software_agent_team.budgets import AgentBudgetUsage
from software_agent_team.integrity import canonical_model_sha256
from software_agent_team.invocation_lifecycle import InvocationPhase
from software_agent_team.run_control import RunPhase

RUN_EVENT_SCHEMA_VERSION = 3
MINIMUM_READABLE_RUN_EVENT_SCHEMA_VERSION = 2
EVENTS_DIRECTORY = "events"
EVENT_FILENAME_PATTERN = re.compile(r"^(?P<sequence>[0-9]{6})\.json$")
DEFAULT_PROGRESS_HEARTBEAT_SECONDS = 10.0
MAX_PROGRESS_SUMMARY_CHARACTERS = 500


class ProgressEventKind(StrEnum):
    """Stable event identities emitted by the deterministic controller."""

    RUN_STARTED = "run_started"
    WORKSPACE_READY = "workspace_ready"
    AGENT_QUEUED = "agent_queued"
    AGENT_READY = "agent_ready"
    AGENT_STARTED = "agent_started"
    AGENT_INVOCATION_LAUNCHED = "agent_invocation_launched"
    AGENT_INITIALIZING = "agent_initializing"
    AGENT_INITIALIZATION_PROGRESS = "agent_initialization_progress"
    AGENT_INITIALIZATION_LIVENESS_DEGRADED = "agent_initialization_liveness_degraded"
    AGENT_INITIALIZATION_STALL_SUSPECTED = "agent_initialization_stall_suspected"
    AGENT_INITIALIZATION_STALL_RECOVERED = "agent_initialization_stall_recovered"
    AGENT_INITIALIZATION_STALLED = "agent_initialization_stalled"
    AGENT_WAITING_PROVIDER = "agent_waiting_provider"
    AGENT_PROVIDER_ACTIVITY = "agent_provider_activity"
    AGENT_TOOL_ACTIVE = "agent_tool_active"
    AGENT_TOOL_STARTED = "agent_tool_started"
    AGENT_TOOL_COMPLETED = "agent_tool_completed"
    AGENT_LIVENESS_DEGRADED = "agent_liveness_degraded"
    AGENT_STALL_SUSPECTED = "agent_stall_suspected"
    AGENT_STALL_RECOVERED = "agent_stall_recovered"
    AGENT_PROVIDER_STALLED = "agent_provider_stalled"
    AGENT_STOPPING = "agent_stopping"
    AGENT_COLLECTING_EVIDENCE = "agent_collecting_evidence"
    AGENT_STOPPED = "agent_stopped"
    AGENT_INVOCATION_COMPLETED = "agent_invocation_completed"
    MODEL_ROUTE_SWITCHED = "model_route_switched"
    AGENT_COMPLETED = "agent_completed"
    AGENT_RETRY = "agent_retry"
    AGENT_FAILED = "agent_failed"
    AGENT_SKIPPED = "agent_skipped"
    AGENT_PAUSED = "agent_paused"
    AGENT_RESUMED = "agent_resumed"
    AGENT_INTERRUPTED = "agent_interrupted"
    AGENT_CANCELLED = "agent_cancelled"
    SNAPSHOT_VERIFIED = "snapshot_verified"
    QUALITY_GATES_STARTED = "quality_gates_started"
    QUALITY_GATE_COMPLETED = "quality_gate_completed"
    QUALITY_GATE_PASSED = "quality_gate_passed"
    QUALITY_GATE_FAILED = "quality_gate_failed"
    DECISION_RECORDED = "decision_recorded"
    CONTROL_RECEIVED = "control_received"
    CONTROL_APPLIED = "control_applied"
    CONTROL_REJECTED = "control_rejected"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"
    RUN_CANCELLED = "run_cancelled"


class RunEventCategory(StrEnum):
    """User-facing grouping independent from a renderer layout."""

    LIFECYCLE = "lifecycle"
    AGENT = "agent"
    GIT = "git"
    QUALITY_GATE = "quality_gate"
    DECISION = "decision"


class RunEventVisibility(StrEnum):
    """Lowest detail level at which one safe event should be rendered."""

    COMPACT = "compact"
    STANDARD = "standard"
    DETAILED = "detailed"


class AgentRunState(StrEnum):
    """Controller-observed state for one run-scoped Agent."""

    QUEUED = "queued"
    READY = "ready"
    RUNNING = "running"
    LAUNCHED = "launched"
    INITIALIZING = "initializing"
    WAITING_PROVIDER = "waiting_provider"
    TOOL_ACTIVE = "tool_active"
    STOPPING = "stopping"
    COLLECTING_EVIDENCE = "collecting_evidence"
    STOPPED = "stopped"
    WAITING_DEPENDENCY = "waiting_dependency"
    BLOCKED = "blocked"
    PAUSED = "paused"
    COMPLETED = "completed"
    WAITING_REPAIR = "waiting_repair"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"
    FAILED = "failed"


class RunEventSource(StrEnum):
    """Authority behind a bounded event summary."""

    CONTROLLER = "controller"
    AGENT_SAFE_SUMMARY = "agent_safe_summary"


class RunEventReferenceKind(StrEnum):
    """Typed evidence that a RunEvent may point to without embedding it."""

    ARTIFACT = "artifact"
    HANDOFF = "handoff"
    QUALITY_GATE = "quality_gate"
    GIT = "git"
    BUDGET = "budget"
    MODEL_ROUTE = "model_route"
    CONTROL_COMMAND = "control_command"


_EVENT_METADATA: dict[
    ProgressEventKind,
    tuple[RunEventCategory, RunEventVisibility, AgentRunState | None],
] = {
    ProgressEventKind.RUN_STARTED: (
        RunEventCategory.LIFECYCLE,
        RunEventVisibility.COMPACT,
        None,
    ),
    ProgressEventKind.WORKSPACE_READY: (
        RunEventCategory.LIFECYCLE,
        RunEventVisibility.STANDARD,
        None,
    ),
    ProgressEventKind.AGENT_QUEUED: (
        RunEventCategory.AGENT,
        RunEventVisibility.STANDARD,
        AgentRunState.QUEUED,
    ),
    ProgressEventKind.AGENT_READY: (
        RunEventCategory.AGENT,
        RunEventVisibility.STANDARD,
        AgentRunState.READY,
    ),
    ProgressEventKind.AGENT_STARTED: (
        RunEventCategory.AGENT,
        RunEventVisibility.STANDARD,
        AgentRunState.RUNNING,
    ),
    ProgressEventKind.AGENT_INVOCATION_LAUNCHED: (
        RunEventCategory.AGENT,
        RunEventVisibility.STANDARD,
        AgentRunState.LAUNCHED,
    ),
    ProgressEventKind.AGENT_INITIALIZING: (
        RunEventCategory.AGENT,
        RunEventVisibility.STANDARD,
        AgentRunState.INITIALIZING,
    ),
    ProgressEventKind.AGENT_INITIALIZATION_PROGRESS: (
        RunEventCategory.AGENT,
        RunEventVisibility.DETAILED,
        AgentRunState.INITIALIZING,
    ),
    ProgressEventKind.AGENT_INITIALIZATION_LIVENESS_DEGRADED: (
        RunEventCategory.AGENT,
        RunEventVisibility.COMPACT,
        AgentRunState.INITIALIZING,
    ),
    ProgressEventKind.AGENT_INITIALIZATION_STALL_SUSPECTED: (
        RunEventCategory.AGENT,
        RunEventVisibility.COMPACT,
        AgentRunState.INITIALIZING,
    ),
    ProgressEventKind.AGENT_INITIALIZATION_STALL_RECOVERED: (
        RunEventCategory.AGENT,
        RunEventVisibility.STANDARD,
        AgentRunState.INITIALIZING,
    ),
    ProgressEventKind.AGENT_INITIALIZATION_STALLED: (
        RunEventCategory.AGENT,
        RunEventVisibility.COMPACT,
        AgentRunState.INITIALIZING,
    ),
    ProgressEventKind.AGENT_WAITING_PROVIDER: (
        RunEventCategory.AGENT,
        RunEventVisibility.STANDARD,
        AgentRunState.WAITING_PROVIDER,
    ),
    ProgressEventKind.AGENT_PROVIDER_ACTIVITY: (
        RunEventCategory.AGENT,
        RunEventVisibility.DETAILED,
        AgentRunState.WAITING_PROVIDER,
    ),
    ProgressEventKind.AGENT_TOOL_ACTIVE: (
        RunEventCategory.AGENT,
        RunEventVisibility.STANDARD,
        AgentRunState.TOOL_ACTIVE,
    ),
    ProgressEventKind.AGENT_TOOL_STARTED: (
        RunEventCategory.AGENT,
        RunEventVisibility.STANDARD,
        AgentRunState.RUNNING,
    ),
    ProgressEventKind.AGENT_TOOL_COMPLETED: (
        RunEventCategory.AGENT,
        RunEventVisibility.STANDARD,
        AgentRunState.RUNNING,
    ),
    ProgressEventKind.AGENT_LIVENESS_DEGRADED: (
        RunEventCategory.AGENT,
        RunEventVisibility.COMPACT,
        AgentRunState.WAITING_PROVIDER,
    ),
    ProgressEventKind.AGENT_STALL_SUSPECTED: (
        RunEventCategory.AGENT,
        RunEventVisibility.COMPACT,
        AgentRunState.WAITING_PROVIDER,
    ),
    ProgressEventKind.AGENT_STALL_RECOVERED: (
        RunEventCategory.AGENT,
        RunEventVisibility.STANDARD,
        AgentRunState.WAITING_PROVIDER,
    ),
    ProgressEventKind.AGENT_PROVIDER_STALLED: (
        RunEventCategory.AGENT,
        RunEventVisibility.COMPACT,
        AgentRunState.WAITING_PROVIDER,
    ),
    ProgressEventKind.AGENT_STOPPING: (
        RunEventCategory.AGENT,
        RunEventVisibility.COMPACT,
        AgentRunState.STOPPING,
    ),
    ProgressEventKind.AGENT_COLLECTING_EVIDENCE: (
        RunEventCategory.AGENT,
        RunEventVisibility.COMPACT,
        AgentRunState.COLLECTING_EVIDENCE,
    ),
    ProgressEventKind.AGENT_STOPPED: (
        RunEventCategory.AGENT,
        RunEventVisibility.COMPACT,
        AgentRunState.STOPPED,
    ),
    ProgressEventKind.AGENT_INVOCATION_COMPLETED: (
        RunEventCategory.AGENT,
        RunEventVisibility.STANDARD,
        AgentRunState.RUNNING,
    ),
    ProgressEventKind.MODEL_ROUTE_SWITCHED: (
        RunEventCategory.AGENT,
        RunEventVisibility.COMPACT,
        AgentRunState.WAITING_PROVIDER,
    ),
    ProgressEventKind.AGENT_COMPLETED: (
        RunEventCategory.AGENT,
        RunEventVisibility.STANDARD,
        AgentRunState.COMPLETED,
    ),
    ProgressEventKind.AGENT_RETRY: (
        RunEventCategory.AGENT,
        RunEventVisibility.COMPACT,
        AgentRunState.WAITING_REPAIR,
    ),
    ProgressEventKind.AGENT_FAILED: (
        RunEventCategory.AGENT,
        RunEventVisibility.COMPACT,
        AgentRunState.FAILED,
    ),
    ProgressEventKind.AGENT_SKIPPED: (
        RunEventCategory.AGENT,
        RunEventVisibility.STANDARD,
        AgentRunState.BLOCKED,
    ),
    ProgressEventKind.AGENT_PAUSED: (
        RunEventCategory.AGENT,
        RunEventVisibility.STANDARD,
        AgentRunState.PAUSED,
    ),
    ProgressEventKind.AGENT_RESUMED: (
        RunEventCategory.AGENT,
        RunEventVisibility.STANDARD,
        AgentRunState.WAITING_DEPENDENCY,
    ),
    ProgressEventKind.AGENT_INTERRUPTED: (
        RunEventCategory.AGENT,
        RunEventVisibility.COMPACT,
        AgentRunState.INTERRUPTED,
    ),
    ProgressEventKind.AGENT_CANCELLED: (
        RunEventCategory.AGENT,
        RunEventVisibility.STANDARD,
        AgentRunState.CANCELLED,
    ),
    ProgressEventKind.SNAPSHOT_VERIFIED: (
        RunEventCategory.GIT,
        RunEventVisibility.STANDARD,
        None,
    ),
    ProgressEventKind.QUALITY_GATES_STARTED: (
        RunEventCategory.QUALITY_GATE,
        RunEventVisibility.STANDARD,
        None,
    ),
    ProgressEventKind.QUALITY_GATE_COMPLETED: (
        RunEventCategory.QUALITY_GATE,
        RunEventVisibility.STANDARD,
        None,
    ),
    ProgressEventKind.QUALITY_GATE_PASSED: (
        RunEventCategory.QUALITY_GATE,
        RunEventVisibility.STANDARD,
        None,
    ),
    ProgressEventKind.QUALITY_GATE_FAILED: (
        RunEventCategory.QUALITY_GATE,
        RunEventVisibility.STANDARD,
        None,
    ),
    ProgressEventKind.DECISION_RECORDED: (
        RunEventCategory.DECISION,
        RunEventVisibility.COMPACT,
        None,
    ),
    ProgressEventKind.CONTROL_RECEIVED: (
        RunEventCategory.DECISION,
        RunEventVisibility.COMPACT,
        None,
    ),
    ProgressEventKind.CONTROL_APPLIED: (
        RunEventCategory.DECISION,
        RunEventVisibility.COMPACT,
        None,
    ),
    ProgressEventKind.CONTROL_REJECTED: (
        RunEventCategory.DECISION,
        RunEventVisibility.COMPACT,
        None,
    ),
    ProgressEventKind.RUN_COMPLETED: (
        RunEventCategory.LIFECYCLE,
        RunEventVisibility.COMPACT,
        None,
    ),
    ProgressEventKind.RUN_FAILED: (
        RunEventCategory.LIFECYCLE,
        RunEventVisibility.COMPACT,
        None,
    ),
    ProgressEventKind.RUN_CANCELLED: (
        RunEventCategory.LIFECYCLE,
        RunEventVisibility.COMPACT,
        None,
    ),
}

_ATTEMPT_EVENT_KINDS = {
    ProgressEventKind.AGENT_STARTED,
    ProgressEventKind.AGENT_INVOCATION_LAUNCHED,
    ProgressEventKind.AGENT_INITIALIZING,
    ProgressEventKind.AGENT_INITIALIZATION_PROGRESS,
    ProgressEventKind.AGENT_INITIALIZATION_LIVENESS_DEGRADED,
    ProgressEventKind.AGENT_INITIALIZATION_STALL_SUSPECTED,
    ProgressEventKind.AGENT_INITIALIZATION_STALL_RECOVERED,
    ProgressEventKind.AGENT_INITIALIZATION_STALLED,
    ProgressEventKind.AGENT_WAITING_PROVIDER,
    ProgressEventKind.AGENT_PROVIDER_ACTIVITY,
    ProgressEventKind.AGENT_TOOL_ACTIVE,
    ProgressEventKind.AGENT_TOOL_STARTED,
    ProgressEventKind.AGENT_TOOL_COMPLETED,
    ProgressEventKind.AGENT_LIVENESS_DEGRADED,
    ProgressEventKind.AGENT_STALL_SUSPECTED,
    ProgressEventKind.AGENT_STALL_RECOVERED,
    ProgressEventKind.AGENT_PROVIDER_STALLED,
    ProgressEventKind.AGENT_STOPPING,
    ProgressEventKind.AGENT_COLLECTING_EVIDENCE,
    ProgressEventKind.AGENT_STOPPED,
    ProgressEventKind.AGENT_INVOCATION_COMPLETED,
    ProgressEventKind.MODEL_ROUTE_SWITCHED,
    ProgressEventKind.AGENT_COMPLETED,
    ProgressEventKind.AGENT_RETRY,
    ProgressEventKind.AGENT_FAILED,
    ProgressEventKind.AGENT_INTERRUPTED,
}


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("RunEvent timestamps must include a timezone")
    return value.astimezone(UTC)


def _clean_summary(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("RunEvent summaries must not be blank")
    if any(character in cleaned for character in ("\n", "\r", "\x00")):
        raise ValueError("RunEvent summaries must be one safe line")
    return cleaned


class RunEventReference(BaseModel):
    """One bounded reference to controller-owned evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: RunEventReferenceKind
    id: str = Field(min_length=1, max_length=120)
    path: str | None = Field(default=None, max_length=300)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("id")
    @classmethod
    def require_clean_id(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("RunEvent reference IDs must not be blank")
        return cleaned

    @field_validator("path")
    @classmethod
    def require_safe_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        path = PurePosixPath(cleaned)
        if (
            not cleaned
            or "\\" in cleaned
            or path.is_absolute()
            or path == PurePosixPath(".")
            or ".." in path.parts
            or str(path) != cleaned
        ):
            raise ValueError("RunEvent reference paths must be canonical and relative")
        return cleaned


class ProgressCheckpointSnapshot(BaseModel):
    """Controller-owned, content-free answer to current/finished/next/cost."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    approved_task_ids: tuple[str, ...]
    invocation_phase: InvocationPhase
    last_verified_checkpoint: str = Field(min_length=1, max_length=300)
    next_controller_checkpoint: str = Field(min_length=1, max_length=300)
    completed_tool_operations: int = Field(default=0, ge=0)
    git_state: Literal["not_applicable", "verified", "working", "changed"]
    gate_state: Literal["not_started", "running", "passed", "failed"]
    review_state: Literal[
        "not_applicable",
        "not_started",
        "running",
        "accepted",
        "changes_requested",
        "blocked",
    ]
    known_estimated_cost_usd: Decimal = Field(ge=0)
    authorized_cost_usd: Decimal = Field(gt=0)
    remaining_estimated_cost_usd: Decimal = Field(ge=0)

    @field_validator("approved_task_ids")
    @classmethod
    def require_task_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)) or any(
            re.fullmatch(r"TASK_[A-Z0-9_]+", value) is None for value in values
        ):
            raise ValueError("checkpoint task IDs must be unique canonical IDs")
        return values

    @field_validator("last_verified_checkpoint", "next_controller_checkpoint")
    @classmethod
    def require_safe_checkpoint_text(cls, value: str) -> str:
        return _clean_summary(value)

    @model_validator(mode="after")
    def validate_cost_snapshot(self) -> Self:
        expected = max(
            Decimal(0),
            self.authorized_cost_usd - self.known_estimated_cost_usd,
        )
        if self.remaining_estimated_cost_usd != expected:
            raise ValueError("checkpoint remaining cost must match known task spend")
        return self


class RunEvent(BaseModel):
    """One immutable, attributable, user-safe controller event."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[2, RUN_EVENT_SCHEMA_VERSION] = RUN_EVENT_SCHEMA_VERSION
    run_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    sequence: int = Field(ge=1)
    occurred_at: datetime
    lifecycle_revision: int = Field(ge=0)
    kind: ProgressEventKind
    category: RunEventCategory
    minimum_visibility: RunEventVisibility
    source: RunEventSource = RunEventSource.CONTROLLER
    summary: str = Field(
        min_length=1,
        max_length=MAX_PROGRESS_SUMMARY_CHARACTERS,
    )
    phase: RunPhase
    agent_id: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    agent_state: AgentRunState | None = None
    iteration: int | None = Field(default=None, ge=1)
    attempt: int | None = Field(default=None, ge=1)
    duration_ms: int | None = Field(default=None, ge=0)
    capability: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    stage_id: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    model: str | None = Field(default=None, min_length=1, max_length=300)
    dependency_ids: tuple[str, ...] = ()
    budget_usage: AgentBudgetUsage | None = None
    checkpoint: ProgressCheckpointSnapshot | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    completed: int | None = Field(default=None, ge=0)
    total: int | None = Field(default=None, ge=1)
    changed_files: tuple[str, ...] = ()
    decision: IterationDecision | None = None
    references: tuple[RunEventReference, ...] = ()
    control_command_id: str | None = Field(
        default=None,
        pattern=r"^ctl-[a-z0-9][a-z0-9-]*$",
    )
    previous_event_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )

    @field_validator("occurred_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @field_validator("summary")
    @classmethod
    def require_safe_summary(cls, value: str) -> str:
        return _clean_summary(value)

    @field_validator("changed_files")
    @classmethod
    def require_safe_changed_files(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("RunEvent changed files must be unique")
        for value in values:
            path = PurePosixPath(value)
            if (
                not value
                or "\\" in value
                or path.is_absolute()
                or path == PurePosixPath(".")
                or ".." in path.parts
                or str(path) != value
            ):
                raise ValueError(
                    "RunEvent changed files must be canonical and relative"
                )
        return values

    @field_validator("model")
    @classmethod
    def require_clean_model(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("RunEvent model must not be blank")
        return cleaned

    @field_validator("dependency_ids")
    @classmethod
    def require_unique_dependencies(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)) or any(
            re.fullmatch(r"[a-z][a-z0-9_]*", value) is None for value in values
        ):
            raise ValueError("RunEvent dependencies must be unique Agent IDs")
        return values

    @model_validator(mode="after")
    def validate_event(self) -> Self:
        category, visibility, agent_state = _EVENT_METADATA[self.kind]
        if self.category is not category:
            raise ValueError("RunEvent category does not match its kind")
        if self.minimum_visibility is not visibility:
            raise ValueError("RunEvent visibility does not match its kind")
        if self.agent_state is not agent_state:
            raise ValueError("RunEvent Agent state does not match its kind")
        if agent_state is not None:
            if self.agent_id is None:
                raise ValueError("Agent events require an Agent ID")
            if (self.kind in _ATTEMPT_EVENT_KINDS) != (self.attempt is not None):
                raise ValueError("RunEvent attempt does not match its Agent event kind")
        elif (
            any(
                value is not None
                for value in (
                    self.agent_id,
                    self.attempt,
                    self.capability,
                    self.stage_id,
                )
            )
            or self.model is not None
            or self.dependency_ids
            or self.budget_usage is not None
        ):
            raise ValueError("non-Agent events cannot claim Agent execution metadata")
        if self.budget_usage is not None and self.kind is not (
            ProgressEventKind.AGENT_INVOCATION_COMPLETED
        ):
            raise ValueError("only completed invocations record budget usage")
        if self.checkpoint is not None:
            if self.agent_id is None:
                raise ValueError("checkpoint snapshots require an Agent event")
            if self.schema_version < RUN_EVENT_SCHEMA_VERSION:
                raise ValueError("legacy RunEvents cannot contain checkpoint snapshots")
        control_kinds = {
            ProgressEventKind.CONTROL_RECEIVED,
            ProgressEventKind.CONTROL_APPLIED,
            ProgressEventKind.CONTROL_REJECTED,
        }
        if (self.kind in control_kinds) != (self.control_command_id is not None):
            raise ValueError("RunEvent control identity does not match its kind")
        if (self.completed is None) != (self.total is None):
            raise ValueError("RunEvent gate progress requires completed and total")
        if self.completed is not None and self.completed > self.total:
            raise ValueError("RunEvent completed count cannot exceed its total")
        if self.kind in {
            ProgressEventKind.QUALITY_GATE_COMPLETED,
            ProgressEventKind.QUALITY_GATE_PASSED,
            ProgressEventKind.QUALITY_GATE_FAILED,
        }:
            if self.completed is None:
                raise ValueError("completed quality gates require progress counts")
        elif self.completed is not None:
            raise ValueError("only completed quality gates record progress counts")
        if self.kind is ProgressEventKind.DECISION_RECORDED:
            if self.decision is None:
                raise ValueError("decision events require a controller decision")
        elif self.decision is not None:
            raise ValueError("only decision events record a controller decision")
        if self.sequence == 1 and self.previous_event_sha256 is not None:
            raise ValueError("the first RunEvent cannot reference a predecessor")
        if self.sequence > 1 and self.previous_event_sha256 is None:
            raise ValueError("later RunEvents require a predecessor digest")
        reference_keys = [
            (reference.kind, reference.id, reference.path)
            for reference in self.references
        ]
        if len(reference_keys) != len(set(reference_keys)):
            raise ValueError("RunEvent references must be unique")
        return self

    @property
    def message(self) -> str:
        """Expose the previous renderer name without duplicating persisted data."""

        return self.summary


@dataclass(frozen=True)
class ProgressEvent:
    """Internal controller draft enriched and persisted by a RunEventJournal."""

    kind: ProgressEventKind
    message: str
    phase: RunPhase | None = None
    agent_id: str | None = None
    iteration: int | None = None
    attempt: int | None = None
    duration_ms: int | None = None
    capability: str | None = None
    stage_id: str | None = None
    model: str | None = None
    dependency_ids: tuple[str, ...] = ()
    budget_usage: AgentBudgetUsage | None = None
    checkpoint: ProgressCheckpointSnapshot | None = None
    completed: int | None = None
    total: int | None = None
    changed_files: tuple[str, ...] = ()
    decision: IterationDecision | None = None
    references: tuple[RunEventReference, ...] = ()
    control_command_id: str | None = None
    source: RunEventSource = RunEventSource.CONTROLLER


ProgressHandler = Callable[[RunEvent], None]
ProgressDraftHandler = Callable[[ProgressEvent], None]
EventClock = Callable[[], datetime]
EventAnchorWriter = Callable[[RunEvent], None]
EventAnchorReader = Callable[[], tuple[int, str | None]]


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
            category, visibility, agent_state = _EVENT_METADATA[draft.kind]
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
                agent_state=agent_state,
                iteration=draft.iteration,
                attempt=draft.attempt,
                duration_ms=draft.duration_ms,
                capability=draft.capability,
                stage_id=draft.stage_id,
                model=draft.model,
                dependency_ids=draft.dependency_ids,
                budget_usage=draft.budget_usage,
                checkpoint=draft.checkpoint,
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


_VISIBILITY_RANK = {
    RunEventVisibility.COMPACT: 0,
    RunEventVisibility.STANDARD: 1,
    RunEventVisibility.DETAILED: 2,
}


class TerminalProgressRenderer:
    """Render persisted safe summaries and elapsed waiting time."""

    def __init__(
        self,
        *,
        output: TextIO | None = None,
        visibility: RunEventVisibility = RunEventVisibility.STANDARD,
        heartbeat_seconds: float = DEFAULT_PROGRESS_HEARTBEAT_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if heartbeat_seconds <= 0:
            raise ValueError("progress heartbeat must be positive")
        self.output = sys.stdout if output is None else output
        self.visibility = visibility
        self.heartbeat_seconds = heartbeat_seconds
        self.monotonic = monotonic
        self._lock = threading.Lock()
        self._waiting: dict[
            tuple[str, int, int], tuple[threading.Event, threading.Thread]
        ] = {}

    def __call__(self, event: RunEvent) -> None:
        """Render one persisted event and manage its elapsed-time heartbeat."""

        visible = not (
            _VISIBILITY_RANK[event.minimum_visibility]
            > _VISIBILITY_RANK[self.visibility]
        )
        if event.kind is ProgressEventKind.AGENT_INVOCATION_COMPLETED:
            # Invocation checkpoints stop their provider heartbeat before the
            # standard cost summary is rendered.
            self._stop_waiting(event)
        elif event.kind in {
            ProgressEventKind.AGENT_COMPLETED,
            ProgressEventKind.AGENT_FAILED,
            ProgressEventKind.AGENT_SKIPPED,
            ProgressEventKind.AGENT_INTERRUPTED,
            ProgressEventKind.AGENT_CANCELLED,
            ProgressEventKind.AGENT_STOPPING,
            ProgressEventKind.AGENT_COLLECTING_EVIDENCE,
            ProgressEventKind.AGENT_STOPPED,
        }:
            # Scheduler terminal events currently identify the scheduling
            # attempt, while targeted semantic correction may have advanced the
            # invocation attempt. A terminal Agent state ends every heartbeat
            # for that Agent and iteration.
            self._stop_agent_waiting(event)
        elif event.kind in {
            ProgressEventKind.AGENT_RETRY,
            ProgressEventKind.AGENT_PAUSED,
            ProgressEventKind.AGENT_RESUMED,
        }:
            self._stop_waiting(event)

        if not visible:
            return
        if event.kind in {
            ProgressEventKind.AGENT_STARTED,
            ProgressEventKind.AGENT_INITIALIZING,
            ProgressEventKind.AGENT_WAITING_PROVIDER,
            ProgressEventKind.AGENT_TOOL_ACTIVE,
            ProgressEventKind.AGENT_TOOL_STARTED,
        }:
            self._start_waiting(event)
            self._print_details(event)
            return
        symbol = {
            ProgressEventKind.RUN_STARTED: "●",
            ProgressEventKind.WORKSPACE_READY: "✓",
            ProgressEventKind.AGENT_QUEUED: "○",
            ProgressEventKind.AGENT_READY: "→",
            ProgressEventKind.AGENT_INVOCATION_LAUNCHED: "●",
            ProgressEventKind.AGENT_INITIALIZING: "●",
            ProgressEventKind.AGENT_INITIALIZATION_PROGRESS: "·",
            ProgressEventKind.AGENT_INITIALIZATION_LIVENESS_DEGRADED: "!",
            ProgressEventKind.AGENT_INITIALIZATION_STALL_SUSPECTED: "?",
            ProgressEventKind.AGENT_INITIALIZATION_STALL_RECOVERED: "↻",
            ProgressEventKind.AGENT_INITIALIZATION_STALLED: "!",
            ProgressEventKind.AGENT_INVOCATION_COMPLETED: "·",
            ProgressEventKind.AGENT_PROVIDER_ACTIVITY: "·",
            ProgressEventKind.AGENT_TOOL_ACTIVE: "⚙",
            ProgressEventKind.AGENT_TOOL_STARTED: "⚙",
            ProgressEventKind.AGENT_TOOL_COMPLETED: "✓",
            ProgressEventKind.AGENT_LIVENESS_DEGRADED: "!",
            ProgressEventKind.AGENT_STALL_SUSPECTED: "?",
            ProgressEventKind.AGENT_STALL_RECOVERED: "↻",
            ProgressEventKind.AGENT_PROVIDER_STALLED: "!",
            ProgressEventKind.AGENT_STOPPING: "■",
            ProgressEventKind.AGENT_COLLECTING_EVIDENCE: "…",
            ProgressEventKind.AGENT_STOPPED: "■",
            ProgressEventKind.MODEL_ROUTE_SWITCHED: "⇄",
            ProgressEventKind.AGENT_COMPLETED: "✓",
            ProgressEventKind.AGENT_RETRY: "↻",
            ProgressEventKind.AGENT_FAILED: "✗",
            ProgressEventKind.AGENT_SKIPPED: "!",
            ProgressEventKind.AGENT_PAUSED: "Ⅱ",
            ProgressEventKind.AGENT_RESUMED: "▶",
            ProgressEventKind.AGENT_INTERRUPTED: "■",
            ProgressEventKind.AGENT_CANCELLED: "■",
            ProgressEventKind.SNAPSHOT_VERIFIED: "✓",
            ProgressEventKind.QUALITY_GATES_STARTED: "●",
            ProgressEventKind.QUALITY_GATE_COMPLETED: "✓",
            ProgressEventKind.QUALITY_GATE_PASSED: "✓",
            ProgressEventKind.QUALITY_GATE_FAILED: "✗",
            ProgressEventKind.DECISION_RECORDED: "✓",
            ProgressEventKind.CONTROL_RECEIVED: "◆",
            ProgressEventKind.CONTROL_APPLIED: "✓",
            ProgressEventKind.CONTROL_REJECTED: "!",
            ProgressEventKind.RUN_COMPLETED: "✓",
            ProgressEventKind.RUN_FAILED: "✗",
            ProgressEventKind.RUN_CANCELLED: "■",
        }[event.kind]
        self._print(f"{symbol} {event.summary}")
        self._print_details(event)
        if event.kind in {
            ProgressEventKind.RUN_COMPLETED,
            ProgressEventKind.RUN_FAILED,
            ProgressEventKind.RUN_CANCELLED,
        }:
            self.close()

    def close(self) -> None:
        """Stop every outstanding heartbeat thread."""

        with self._lock:
            waiting = tuple(self._waiting.values())
            self._waiting.clear()
        for stop, thread in waiting:
            stop.set()
            thread.join(timeout=min(self.heartbeat_seconds, 0.2))

    def set_visibility(self, visibility: RunEventVisibility | str) -> None:
        """Change rendering detail without changing controller execution."""

        resolved = RunEventVisibility(visibility)
        with self._lock:
            self.visibility = resolved

    def write_notice(self, value: str) -> None:
        """Print an interaction notice without racing a progress heartbeat."""

        cleaned = " ".join(value.split())
        if cleaned:
            self._print(cleaned)

    def _key(self, event: RunEvent) -> tuple[str, int, int] | None:
        if event.agent_id is None or event.iteration is None or event.attempt is None:
            return None
        return event.agent_id, event.iteration, event.attempt

    def _start_waiting(self, event: RunEvent) -> None:
        key = self._key(event)
        if key is None:
            self._print(f"● {event.summary}")
            return
        self._print(f"● {event.summary}")
        stop = threading.Event()
        started = self.monotonic()
        thread = threading.Thread(
            target=self._heartbeat,
            args=(stop, started, self._heartbeat_summary(event)),
            name=f"sat-progress-{event.agent_id}",
            daemon=True,
        )
        with self._lock:
            previous = self._waiting.pop(key, None)
            self._waiting[key] = (stop, thread)
        if previous is not None:
            previous[0].set()
        thread.start()

    def _stop_waiting(self, event: RunEvent) -> None:
        key = self._key(event)
        if key is None:
            return
        with self._lock:
            waiting = self._waiting.pop(key, None)
        if waiting is not None:
            waiting[0].set()
            waiting[1].join(timeout=min(self.heartbeat_seconds, 0.2))

    def _stop_agent_waiting(self, event: RunEvent) -> None:
        if event.agent_id is None or event.iteration is None:
            return
        with self._lock:
            keys = tuple(
                key
                for key in self._waiting
                if key[:2] == (event.agent_id, event.iteration)
            )
            waiting = tuple(self._waiting.pop(key) for key in keys)
        for stop, thread in waiting:
            stop.set()
            thread.join(timeout=min(self.heartbeat_seconds, 0.2))

    @staticmethod
    def _heartbeat_summary(event: RunEvent) -> str:
        assert event.agent_id is not None
        if event.kind is ProgressEventKind.AGENT_INITIALIZING:
            return f"{event.agent_id} is initializing its invocation"
        if event.kind is ProgressEventKind.AGENT_WAITING_PROVIDER:
            return f"{event.agent_id} is waiting for the model"
        if event.kind is ProgressEventKind.AGENT_TOOL_STARTED:
            return f"{event.agent_id} has an attributable tool operation active"
        if event.kind is ProgressEventKind.AGENT_TOOL_ACTIVE:
            return f"{event.agent_id} has attributable tool operations active"
        return f"{event.agent_id} is working"

    def _heartbeat(
        self,
        stop: threading.Event,
        started: float,
        message: str,
    ) -> None:
        while not stop.wait(self.heartbeat_seconds):
            elapsed = max(0, int(self.monotonic() - started))
            minutes, seconds = divmod(elapsed, 60)
            self._print(f"  {message} {minutes:02d}:{seconds:02d} elapsed")

    def _print_details(self, event: RunEvent) -> None:
        if (
            self.visibility is not RunEventVisibility.COMPACT
            and event.checkpoint is not None
        ):
            checkpoint = event.checkpoint
            task_ids = ",".join(checkpoint.approved_task_ids) or "none"
            self._print(
                "  progress "
                f"phase={checkpoint.invocation_phase.value} tasks={task_ids} "
                f"completed={checkpoint.last_verified_checkpoint}; "
                f"next={checkpoint.next_controller_checkpoint}"
            )
            self._print(
                "  task budget "
                f"${checkpoint.known_estimated_cost_usd:.6f} estimated / "
                f"${checkpoint.authorized_cost_usd} authorized; "
                f"${checkpoint.remaining_estimated_cost_usd:.6f} recorded remaining"
            )
        if self.visibility is not RunEventVisibility.DETAILED:
            return
        if event.agent_id is not None:
            fields = [f"agent={event.agent_id}"]
            if event.agent_state is not None:
                fields.append(f"state={event.agent_state.value}")
            if event.capability is not None:
                fields.append(f"capability={event.capability}")
            if event.stage_id is not None:
                fields.append(f"stage={event.stage_id}")
            if event.model is not None:
                fields.append(f"model={event.model}")
            if event.attempt is not None:
                fields.append(f"attempt={event.attempt}")
            if event.duration_ms is not None:
                fields.append(f"duration_ms={event.duration_ms}")
            dependencies = ",".join(event.dependency_ids) or "none"
            fields.append(f"dependencies={dependencies}")
            self._print("  " + " ".join(fields))
        if event.budget_usage is not None:
            usage = event.budget_usage
            self._print(
                "  budget "
                f"calls={usage.calls_completed}/{usage.calls_started} "
                f"active={usage.active_calls} input={usage.input_tokens} "
                f"output={usage.output_tokens} "
                f"duration_ms={usage.agent_duration_ms} "
                f"known_cost_usd={usage.known_estimated_cost_usd} "
                f"unpriced_calls={usage.unpriced_calls}"
            )

    def _print(self, value: str) -> None:
        with self._lock:
            print(value, file=self.output, flush=True)
