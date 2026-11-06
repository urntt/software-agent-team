"""Shared types, error classes, dispositions, and helper functions for dynamic runner.

This module provides data structures for tracking incomplete/completed attempt
dispositions, read-only/upstream continuations, quality gate protocols, and
summary projection helpers.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from software_agent_team.artifacts import (
    ArtifactReference,
    CommandEvidence,
)
from software_agent_team.git_workspace import GitWorkspaceProgress
from software_agent_team.prompting import DynamicUserGuidance
from software_agent_team.response_corrections import (
    ResponseValidationDiagnostic,
    SemanticCorrectionOutcome,
    SemanticCorrectionPlan,
)
from software_agent_team.responses import ReviewToolEvidenceAttempt
from software_agent_team.run_control import TerminationReason


class DynamicQualityGate(Protocol):
    """Deterministic quality boundary shared by all quality Agents."""

    def run(self, *, iteration: int) -> tuple[CommandEvidence, ...]:
        """Execute the approved commands for one immutable commit."""


class DynamicAgentRunnerError(RuntimeError):
    """A classified failure returned through the deterministic scheduler."""

    def __init__(self, detail: str, reason: TerminationReason) -> None:
        super().__init__(detail)
        self.reason = reason


@dataclass(frozen=True)
class _UpstreamContinuation:
    """Verified state and evidence binding for one same-session continuation."""

    progress: GitWorkspaceProgress
    prior_execution: ArtifactReference
    completed_tool_operations: int


@dataclass(frozen=True)
class _CompletedAttemptDisposition:
    """Persisted result or bounded correction from one completed Agent call."""

    response_reference: ArtifactReference | None
    ignored_fields: tuple[str, ...]
    normalizations: tuple[str, ...]
    validation: ResponseValidationDiagnostic | None
    next_correction_plan: SemanticCorrectionPlan | None
    correction_outcome: SemanticCorrectionOutcome | None
    record_error: str | None
    failure: Exception | None


@dataclass(frozen=True)
class _IncompleteAttemptDisposition:
    """Verified continuation and failure decision for a nonterminal attempt."""

    failure: DynamicAgentRunnerError | None
    writer_progress: GitWorkspaceProgress | None
    readonly_continuation: bool
    review_evidence: ReviewToolEvidenceAttempt | None


@dataclass(frozen=True)
class _ReadOnlyContinuation:
    """Captured tools from an incomplete, clean read-only invocation."""

    prior_execution: ArtifactReference
    completed_tool_operations: int
    session_id: str


_SCHEDULER_SUMMARY_LIMIT = 2_000
_UPSTREAM_SUMMARY_LIMIT = 1_000


def _bounded_artifact_summary(value: str, *, limit: int) -> str:
    """Project complete artifact text into an attributable bounded context."""
    cleaned = value.strip()
    if len(cleaned) <= limit:
        return cleaned
    digest = hashlib.sha256(cleaned.encode()).hexdigest()
    marker = (
        "\n[Controller projection: source summary truncated from "
        f"{len(cleaned)} characters; source sha256={digest}; full text remains "
        "in immutable artifact evidence.]"
    )
    if len(marker) >= limit:
        raise ValueError("artifact summary projection limit is too small")
    prefix = cleaned[: limit - len(marker)].rstrip()
    return f"{prefix}{marker}"


type GuidanceProvider = Callable[[str], tuple[DynamicUserGuidance, ...]]
type InvocationStopProvider = Callable[[str], TerminationReason | None]


def _system_clock() -> datetime:
    return datetime.now(UTC)


def _utc(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if value.tzinfo is None or value.utcoffset() is None:
        raise DynamicAgentRunnerError(
            "dynamic runner clock must include a timezone",
            TerminationReason.CONTROLLER_ERROR,
        )
    return value.astimezone(UTC)
