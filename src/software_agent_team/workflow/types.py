"""Workflow type definitions, protocols, context, and time utilities."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Protocol

from software_agent_team.artifact_store import ArtifactStore
from software_agent_team.artifacts import (
    ArtifactReference,
    CommandEvidence,
    IterationRecord,
    PhaseArtifact,
    ReviewReport,
    TaskBrief,
    TestReport,
)
from software_agent_team.budgets import AgentBudgetLedger
from software_agent_team.git_workspace import GitWorkspace
from software_agent_team.progress import (
    ProgressDraftHandler,
    RunEvent,
    RunEventJournal,
)
from software_agent_team.responses import AgentResponseBody
from software_agent_team.run_control import (
    RunController,
    RunRecord,
    TerminationReason,
)
from software_agent_team.teams import TeamPlan

PHASE1_TEAM_ID = "function_specialized"
PHASE1_ITERATION_LIMIT = 2


class WorkflowError(RuntimeError):
    """Base error for deterministic Phase 1 orchestration."""


class WorkflowEvidenceError(WorkflowError):
    """Raised when independent evidence disagrees with an Agent claim."""


class AgentInvocationError(WorkflowError):
    """Raised after one Agent invocation exhausts its allowed repair."""

    def __init__(self, detail: str, reason: TerminationReason) -> None:
        super().__init__(detail)
        self.reason = reason


class QualityGate(Protocol):
    """Deterministic gate boundary required by the workflow."""

    def run(self, *, iteration: int) -> tuple[CommandEvidence, ...]:
        """Execute the fixed checks for one immutable snapshot."""


type QualityGateFactory = Callable[[Path, Path, ProgressDraftHandler], QualityGate]
type RuntimeSetup = Callable[[GitWorkspace, Path], None]
type Clock = Callable[[], datetime]
type ArtifactAssembler = Callable[[AgentResponseBody], PhaseArtifact]
type InvocationGuard = Callable[[bool], None]


def _system_clock() -> datetime:
    return datetime.now(UTC)


def _utc(clock: Clock) -> datetime:
    value = clock()
    if value.tzinfo is None or value.utcoffset() is None:
        raise WorkflowError("workflow clock must include a timezone")
    return value.astimezone(UTC)


def _unique(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value.strip() for value in values if value.strip()))


@dataclass(frozen=True)
class WorkflowOutcome:
    """Terminal state and evidence index returned to the CLI."""

    record: RunRecord
    final_report: ArtifactReference
    human_report_path: str
    execution_records: tuple[ArtifactReference, ...]
    handoffs: tuple[ArtifactReference, ...]
    events: tuple[RunEvent, ...]


@dataclass
class _WorkflowContext:
    brief: TaskBrief
    team_plan: TeamPlan
    controller: RunController
    artifact_store: ArtifactStore
    event_journal: RunEventJournal
    run_directory: Path
    execution_records: list[ArtifactReference] = field(default_factory=list)
    handoffs: list[ArtifactReference] = field(default_factory=list)
    iteration_records: list[ArtifactReference] = field(default_factory=list)
    command_evidence: list[CommandEvidence] = field(default_factory=list)
    last_test: TestReport | None = None
    last_review: ReviewReport | None = None
    review_history: list[ReviewReport] = field(default_factory=list)
    last_iteration: IterationRecord | None = None
    execution_lock: Lock = field(default_factory=Lock, repr=False)
    budget_ledger: AgentBudgetLedger | None = field(default=None, repr=False)
