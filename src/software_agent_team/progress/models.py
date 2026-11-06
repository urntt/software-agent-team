"""Pydantic and dataclass models for controller run events and checkpoints."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import PurePosixPath
from typing import Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)

from software_agent_team.artifacts import IterationDecision
from software_agent_team.budgets import AgentBudgetUsage
from software_agent_team.invocation_lifecycle import InvocationPhase
from software_agent_team.run_control import RunPhase
from software_agent_team.terminal_presentation import InvocationPresentation

from .types import (
    _ATTEMPT_EVENT_KINDS,
    _EVENT_METADATA,
    _RUN_EVENT_SCHEMA_FOUR_KINDS,
    MAX_PROGRESS_SUMMARY_CHARACTERS,
    RUN_EVENT_SCHEMA_VERSION,
    AgentRunState,
    ProgressEventKind,
    RunEventCategory,
    RunEventReferenceKind,
    RunEventSource,
    RunEventVisibility,
)


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
    review_criterion_ids: tuple[str, ...] = ()
    review_coverage_state: Literal[
        "not_applicable",
        "unverified",
        "grounded",
    ] = "not_applicable"
    known_estimated_cost_usd: Decimal = Field(ge=0)
    authorized_cost_usd: Decimal = Field(gt=0)
    remaining_estimated_cost_usd: Decimal = Field(ge=0)
    active_reserved_cost_usd: Decimal = Field(default=Decimal(0), ge=0)
    cost_accounting_state: Literal[
        "settled",
        "active_unsettled",
        "terminal_incomplete",
    ] = "settled"
    unsettled_model_calls: int = Field(default=0, ge=0)

    @field_validator("approved_task_ids")
    @classmethod
    def require_task_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)) or any(
            re.fullmatch(r"TASK_[A-Z0-9_]+", value) is None for value in values
        ):
            raise ValueError("checkpoint task IDs must be unique canonical IDs")
        return values

    @field_validator("review_criterion_ids")
    @classmethod
    def require_review_criterion_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)) or any(
            re.fullmatch(r"[A-Z][A-Z0-9_]*", value) is None for value in values
        ):
            raise ValueError("checkpoint review criteria must be unique canonical IDs")
        return values

    @field_validator("last_verified_checkpoint", "next_controller_checkpoint")
    @classmethod
    def require_safe_checkpoint_text(cls, value: str) -> str:
        return _clean_summary(value)

    @model_validator(mode="after")
    def validate_cost_snapshot(self) -> Self:
        expected = max(
            Decimal(0),
            self.authorized_cost_usd
            - self.known_estimated_cost_usd
            - self.active_reserved_cost_usd,
        )
        if self.remaining_estimated_cost_usd != expected:
            raise ValueError(
                "checkpoint remaining cost must match known and reserved task spend"
            )
        if self.review_coverage_state == "not_applicable":
            if self.review_criterion_ids:
                raise ValueError(
                    "non-Review checkpoints cannot claim criterion coverage"
                )
        elif not self.review_criterion_ids:
            raise ValueError("Review coverage requires assigned criterion IDs")
        if self.cost_accounting_state == "active_unsettled":
            if self.unsettled_model_calls < 1:
                raise ValueError("active cost accounting requires an unsettled call")
        elif self.unsettled_model_calls:
            raise ValueError("only active cost accounting may contain unsettled calls")
        if not self.unsettled_model_calls and self.active_reserved_cost_usd:
            raise ValueError("settled checkpoint cannot retain a call reservation")
        return self

    @model_serializer(mode="wrap")
    def serialize_active_reservation(self, handler):
        """Keep unchanged historical checkpoint bytes stable."""

        result = handler(self)
        if self.active_reserved_cost_usd == 0:
            result.pop("active_reserved_cost_usd", None)
        return result


# Scheduler decisions describe the Agent, not its most recent invocation.
_INVOCATION_EVENT_KINDS = _ATTEMPT_EVENT_KINDS - {
    ProgressEventKind.AGENT_STARTED,
    ProgressEventKind.AGENT_COMPLETED,
    ProgressEventKind.AGENT_RETRY,
    ProgressEventKind.AGENT_FAILED,
    ProgressEventKind.AGENT_INTERRUPTED,
    ProgressEventKind.MODEL_ROUTE_SWITCHED,
}


def _current_agent_state(
    kind: ProgressEventKind, checkpoint: ProgressCheckpointSnapshot | None
) -> AgentRunState | None:
    """Project invocation state from its observation, never its history delta."""

    if checkpoint is not None and kind in _INVOCATION_EVENT_KINDS:
        phase = checkpoint.invocation_phase
        return (
            AgentRunState.WAITING_PROVIDER
            if phase is InvocationPhase.PROVIDER_WAIT
            else AgentRunState(phase.value)
        )
    return _EVENT_METADATA[kind][2]


class RunEvent(BaseModel):
    """One immutable, attributable, user-safe controller event."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[2, 3, 4, 5, RUN_EVENT_SCHEMA_VERSION] = (
        RUN_EVENT_SCHEMA_VERSION
    )
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
    specialization: str | None = Field(
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
    presentation: InvocationPresentation | None = Field(default=None, exclude=True)
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
        if self.schema_version < 4 and self.kind in _RUN_EVENT_SCHEMA_FOUR_KINDS:
            raise ValueError(
                "legacy RunEvents cannot contain response-finalization state"
            )
        category, visibility, agent_state = _EVENT_METADATA[self.kind]
        if self.schema_version >= 5:
            agent_state = _current_agent_state(self.kind, self.checkpoint)
        if self.category is not category:
            raise ValueError("RunEvent category does not match its kind")
        if self.minimum_visibility is not visibility:
            raise ValueError("RunEvent visibility does not match its kind")
        if self.agent_state is not agent_state:
            raise ValueError("RunEvent Agent state does not match its authority")
        if agent_state is not None:
            if self.agent_id is None:
                raise ValueError("Agent events require an Agent ID")
            if (self.kind in _ATTEMPT_EVENT_KINDS) != (self.attempt is not None):
                raise ValueError("RunEvent attempt does not match its Agent event kind")
            if self.schema_version >= 6 and (
                (self.capability is None) != (self.specialization is None)
            ):
                raise ValueError(
                    "current Agent events must pair capability and specialization"
                )
        elif (
            any(
                value is not None
                for value in (
                    self.agent_id,
                    self.attempt,
                    self.capability,
                    self.specialization,
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
            if self.schema_version < 3:
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
    specialization: str | None = None
    stage_id: str | None = None
    model: str | None = None
    dependency_ids: tuple[str, ...] = ()
    budget_usage: AgentBudgetUsage | None = None
    checkpoint: ProgressCheckpointSnapshot | None = None
    presentation: InvocationPresentation | None = None
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
