"""Dynamic workflow type definitions, protocols, context, and time utilities."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from software_agent_team.artifact_store import ArtifactStore
from software_agent_team.artifacts import (
    ArtifactReference,
    CommandEvidence,
    ReviewReport,
    TestReport,
    WorkResult,
)
from software_agent_team.budgets import AgentBudgetLedger
from software_agent_team.controls import ControlCommandStore
from software_agent_team.dynamic_runner import DynamicQualityGate
from software_agent_team.git_workspace import GitWorkspace
from software_agent_team.planning import ApprovedPlanningResult
from software_agent_team.progress import (
    ProgressDraftHandler,
    RunEvent,
    RunEventJournal,
)
from software_agent_team.run_control import RunController, RunRecord
from software_agent_team.runtime_controls import (
    RuntimeControlChannel,
    RuntimeControlDecision,
)
from software_agent_team.scheduling import DagScheduleResult
from software_agent_team.teams import TeamPlan


class DynamicWorkflowError(RuntimeError):
    """Raised when approved dynamic evidence cannot form a controlled run."""


class DynamicQualityGateFactory(Protocol):
    """Create one run-scoped deterministic gate for an isolated workspace."""

    def __call__(
        self,
        run_directory: Path,
        workspace: Path,
        event_handler: ProgressDraftHandler,
    ) -> DynamicQualityGate: ...


type RuntimeSetup = Callable[[GitWorkspace, Path], None]
type ControlStoreCleanup = Callable[[], None]
type ControlStoreHandler = Callable[
    [ControlCommandStore, TeamPlan], ControlStoreCleanup | None
]
type Clock = Callable[[], datetime]


def _system_clock() -> datetime:
    """Return the current system time in UTC timezone."""
    return datetime.now(UTC)


def _utc(clock: Clock) -> datetime:
    """Normalize a clock reading into timezone-aware UTC datetime."""
    value = clock()
    if value.tzinfo is None or value.utcoffset() is None:
        raise DynamicWorkflowError("dynamic workflow clock must include a timezone")
    return value.astimezone(UTC)


def _unique(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Preserve insertion order while removing duplicates and empty strings."""
    return tuple(dict.fromkeys(value.strip() for value in values if value.strip()))


def _unique_references(
    values: tuple[ArtifactReference, ...] | list[ArtifactReference],
) -> tuple[ArtifactReference, ...]:
    """Deduplicate artifact references by canonical file path."""
    return tuple({reference.path: reference for reference in values}.values())


def _safe_event_summary(value: str, *, fallback: str) -> str:
    """Collapse bounded Agent output into one renderer-safe evidence summary."""
    cleaned = " ".join(value.split())
    return (cleaned or fallback)[:500]


@dataclass(frozen=True)
class DynamicWorkflowOutcome:
    """Terminal dynamic state and its complete controller evidence index."""

    record: RunRecord
    final_report: ArtifactReference
    human_report_path: str
    execution_records: tuple[ArtifactReference, ...]
    handoffs: tuple[ArtifactReference, ...]
    events: tuple[RunEvent, ...]
    schedules: tuple[DagScheduleResult, ...]
    control_stop: RuntimeControlDecision | None = None
    correction_instruction: str | None = None


@dataclass
class _DynamicWorkflowContext:
    """Execution state passed through the dynamic workflow lifecycle."""

    approved: ApprovedPlanningResult
    controller: RunController
    artifact_store: ArtifactStore
    event_journal: RunEventJournal
    run_directory: Path
    budget_ledger: AgentBudgetLedger
    control_channel: RuntimeControlChannel
    execution_records: list[ArtifactReference] = field(default_factory=list)
    handoffs: list[ArtifactReference] = field(default_factory=list)
    iteration_records: list[ArtifactReference] = field(default_factory=list)
    schedules: list[DagScheduleResult] = field(default_factory=list)
    command_evidence: list[CommandEvidence] = field(default_factory=list)
    last_works: tuple[WorkResult, ...] = ()
    last_tests: tuple[TestReport, ...] = ()
    last_reviews: tuple[ReviewReport, ...] = ()
    review_history: list[ReviewReport] = field(default_factory=list)


__all__ = [
    "Clock",
    "ControlStoreCleanup",
    "ControlStoreHandler",
    "DynamicQualityGateFactory",
    "DynamicWorkflowError",
    "DynamicWorkflowOutcome",
    "RuntimeSetup",
    "_DynamicWorkflowContext",
    "_safe_event_summary",
    "_system_clock",
    "_unique",
    "_unique_references",
    "_utc",
]
