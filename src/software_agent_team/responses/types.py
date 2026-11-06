"""Data types, error classes, and evidence structures for Agent responses."""

from __future__ import annotations

from dataclasses import dataclass

from software_agent_team.artifacts import AgentToolCallEvidence, CommandEvidence
from software_agent_team.response_corrections import ResponseValidationDiagnostic


class AgentArtifactResponseError(ValueError):
    """Raised when an Agent response cannot become attributable run evidence."""

    def __init__(
        self,
        detail: str,
        *,
        semantic_payload: dict[str, object] | None = None,
        diagnostic: ResponseValidationDiagnostic | None = None,
        response_normalizations: tuple[str, ...] = (),
    ) -> None:
        super().__init__(detail)
        self.semantic_payload = semantic_payload
        self.diagnostic = diagnostic
        self.response_normalizations = response_normalizations


class _ReviewEvidenceGroundingError(ValueError):
    """Locate one model-owned Review selector rejected by evidence binding."""

    def __init__(
        self,
        detail: str,
        *,
        path: str,
        invariant_id: str,
        criterion_id: str,
    ) -> None:
        super().__init__(detail)
        self.path = path
        self.invariant_id = invariant_id
        self.criterion_id = criterion_id


class _UnsafeSatisfiedEvidenceError(_ReviewEvidenceGroundingError):
    """Identify a positive Review claim that matched only unsafe evidence."""


class _ReviewEvidenceGroundingErrors(ValueError):
    """Preserve every independently invalid Review evidence selector."""

    def __init__(self, errors: tuple[_ReviewEvidenceGroundingError, ...]) -> None:
        if not errors:
            raise ValueError("Review evidence error collection must not be empty")
        detail = str(errors[0])
        if len(errors) > 1:
            detail += f"; {len(errors) - 1} additional evidence selector(s) failed"
        super().__init__(detail)
        self.errors = errors


class _SpecializedReviewScopeError(ValueError):
    """Locate a specialist-owned scope mismatch in its semantic entries."""

    def __init__(self, detail: str, *, path: str) -> None:
        super().__init__(detail)
        self.path = path


@dataclass(frozen=True)
class ReviewToolEvidenceAttempt:
    """One integrity-checked attempt in a bounded Reviewer response chain."""

    execution_attempt: int
    tool_calls: tuple[AgentToolCallEvidence, ...]

    def __post_init__(self) -> None:
        if self.execution_attempt < 1:
            raise ValueError("review evidence attempt must be positive")


@dataclass(frozen=True)
class _ReviewEvidenceMatches:
    """One selector's matches under the canonical Review evidence policy."""

    tool_matches: tuple[tuple[int, AgentToolCallEvidence], ...]
    ineligible_probe_matches: tuple[tuple[int, AgentToolCallEvidence], ...]
    command_matches: tuple[CommandEvidence, ...]
