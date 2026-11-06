"""Enums, constants, and common validation primitives for artifacts.

Defines schemas and invariants shared across all workflow execution artifacts,
task briefs, evidence envelopes, reviews, and test reports.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

# --- Schema Versions and Patterns ---
ARTIFACT_SCHEMA_VERSION = 15
MINIMUM_READABLE_ARTIFACT_SCHEMA_VERSION = 2
COMMIT_PATTERN = r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"
AGENT_ID_PATTERN = r"^[a-z][a-z0-9_]*$"


class AgentRole(StrEnum):
    """Agent responsibilities available to versioned team configurations."""

    CLARIFIER = "clarifier"
    SINGLE_AGENT = "single_agent"
    PLANNER = "planner"
    GENERALIST_DEVELOPER = "generalist_developer"
    FRONTEND_DEVELOPER = "frontend_developer"
    BACKEND_DEVELOPER = "backend_developer"
    INTEGRATOR = "integrator"
    TESTER = "tester"
    REVIEWER = "reviewer"


class ProductDefinitionDisposition(StrEnum):
    """Attributable authority for one approved product-definition dimension."""

    EXPLICIT_INPUT = "explicit_input"
    RESOLVED_QUESTION = "resolved_question"
    PLANNER_RECOMMENDATION = "planner_recommendation"
    NOT_MATERIAL = "not_material"


class ProductDefinitionDimension(StrEnum):
    """Stable product-depth dimensions checked before plan approval."""

    TARGET_USERS = "target_users"
    PRIMARY_WORKFLOW = "primary_workflow"
    DELIVERY_MATURITY = "delivery_maturity"
    USABILITY_EXPECTATIONS = "usability_expectations"
    OPERATIONAL_EXPECTATIONS = "operational_expectations"
    DELIVERY_EXPECTATIONS = "delivery_expectations"


class DeliveryMaturity(StrEnum):
    """User-visible maturity levels that imply materially different deliveries."""

    THROWAWAY_PROTOTYPE = "throwaway_prototype"
    USABLE_LOCAL_PRODUCT = "usable_local_product"
    RELEASABLE_SMALL_PRODUCT = "releasable_small_product"


class HandoffStatus(StrEnum):
    """Terminal status for one Agent execution."""

    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"


class AgentExecutionStatus(StrEnum):
    """Observable terminal state of one Agent adapter invocation."""

    COMPLETED = "completed"
    PROCESS_FAILED = "process_failed"
    PROVIDER_FAILED = "provider_failed"
    PROVIDER_STALLED = "provider_stalled"
    INITIALIZATION_STALLED = "initialization_stalled"
    RESPONSE_FINALIZATION_STALLED = "response_finalization_stALLED".lower()
    TIMED_OUT = "timed_out"
    UPSTREAM_INCOMPLETE = "upstream_incomplete"
    INVALID_RESPONSE = "invalid_response"
    LAUNCH_FAILED = "launch_failed"
    INTERRUPTED = "interrupted"


class AgentRuntimeFailureCode(StrEnum):
    """Controller-classified failures emitted by the pinned local runtime."""

    OPENCLAW_COMPACTION_TIMEOUT = "openclaw_compaction_timeout"
    OPENCLAW_MODEL_STREAM_TERMINATED = "openclaw_model_stream_terminated"


class ProviderLivenessEvidence(BaseModel):
    """Persistable, content-free evidence from a renewable activity lease."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["enforced", "degraded"]
    policy_source: str = Field(min_length=1, max_length=300)
    silence_seconds: float = Field(gt=0)
    stall_grace_seconds: float = Field(gt=0)
    lease_started: bool = False
    lease_start_source: Literal["current_turn", "provider_stream"] | None = None
    raw_stream_observed: bool = False
    session_observed: bool = False
    provider_activity_observations: int = Field(ge=0)
    tool_started_count: int = Field(ge=0)
    tool_completed_count: int = Field(ge=0)
    stall_suspected_count: int = Field(ge=0)
    stall_recovered_count: int = Field(ge=0)
    maximum_inactivity_ms: int = Field(default=0, ge=0)
    stalled: bool = False
    terminal_response_observed: bool = Field(
        default=False,
        exclude_if=lambda value: not value,
    )
    degradation_reason: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def bind_mode_and_reason(self) -> Self:
        if (self.mode == "degraded") != (self.degradation_reason is not None):
            raise ValueError("degraded liveness requires exactly one reason")
        if self.stalled and self.mode != "enforced":
            raise ValueError("degraded liveness cannot declare a provider stall")
        if self.lease_started != (self.lease_start_source is not None):
            raise ValueError("started liveness requires exactly one start source")
        if self.stalled and not self.lease_started:
            raise ValueError("provider stall requires a started liveness lease")
        if self.stalled and self.terminal_response_observed:
            raise ValueError("a terminal provider response cannot be provider-stalled")
        return self


class ArtifactKind(StrEnum):
    """Persisted artifact types used by the workflow."""

    CLARIFICATION_RECORD = "clarification_record"
    TASK_BRIEF = "task_brief"
    IMPLEMENTATION_PLAN = "implementation_plan"
    WORK_RESULT = "work_result"
    TEST_REPORT = "test_report"
    REVIEW_REPORT = "review_report"
    SECURITY_ASSESSMENT = "security_assessment"
    EXPERIENCE_ASSESSMENT = "experience_assessment"
    ITERATION_RECORD = "iteration_record"
    FINAL_REPORT = "final_report"
    HANDOFF_ENVELOPE = "handoff_envelope"
    AGENT_EXECUTION_RECORD = "agent_execution_record"


REVIEW_ARTIFACT_KINDS = frozenset(
    {
        ArtifactKind.REVIEW_REPORT,
        ArtifactKind.SECURITY_ASSESSMENT,
        ArtifactKind.EXPERIENCE_ASSESSMENT,
    }
)


class CheckStatus(StrEnum):
    """Outcome of a deterministic check or acceptance criterion."""

    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"
    PENDING_REVIEW = "pending_review"


class ReviewSeverity(StrEnum):
    """Impact category for an independent review finding."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ReviewVerdict(StrEnum):
    """Reviewer's semantic recommendation to the controller."""

    ACCEPT = "accept"
    REVISE = "revise"
    FAIL = "fail"


class ReviewCriterionStatus(StrEnum):
    """Reviewer's explicit outcome for one assigned manual criterion."""

    SATISFIED = "satisfied"
    BLOCKED = "blocked"


class ReviewBoundaryKind(StrEnum):
    """Entry boundaries required for an absolute Review claim."""

    TOP_LEVEL_INPUT = "top_level_input"
    NESTED_INPUT = "nested_input"
    ALIAS_OR_INDIRECTION = "alias_or_indirection"
    FAILURE_PATH = "failure_path"


REVIEW_BOUNDARY_DEFINITIONS: Mapping[ReviewBoundaryKind, str] = MappingProxyType(
    {
        ReviewBoundaryKind.TOP_LEVEL_INPUT: (
            "The primary input value, object, resource, or entry point selected or "
            "supplied directly by the user or upstream caller, before traversal, "
            "expansion, or decomposition. If a path or directory is selected as a "
            "root, that root itself is the top-level input; every child inside it, "
            "including an immediate first-level child, is nested input."
        ),
        ReviewBoundaryKind.NESTED_INPUT: (
            "An input discovered inside or below the primary input after traversal, "
            "expansion, or decomposition. Immediate children and deeper descendants "
            "are both nested input."
        ),
        ReviewBoundaryKind.ALIAS_OR_INDIRECTION: (
            "The same logical input reached through an alias, symlink, redirect, "
            "wrapper, reference, configuration indirection, or another non-canonical "
            "route rather than its direct primary form."
        ),
        ReviewBoundaryKind.FAILURE_PATH: (
            "A missing, malformed, invalid, inaccessible, unsupported, rejected, or "
            "otherwise failing input or operation for the same requirement. Review "
            "must verify the observable failure behavior, not merely the absence of "
            "a crash."
        ),
    }
)


def review_boundary_definition_map() -> dict[str, str]:
    """Return the controller-owned boundary protocol as JSON-ready values."""
    return {
        boundary.value: definition
        for boundary, definition in REVIEW_BOUNDARY_DEFINITIONS.items()
    }


class AgentToolCallOutcome(StrEnum):
    """Controller-normalized outcome of one attributable Agent tool call."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEFERRED = "deferred"


class AgentToolEvidenceStatus(StrEnum):
    """Collection state for sanitized OpenClaw session tool evidence."""

    NOT_CAPTURED = "not_captured"
    CAPTURED = "captured"
    INVALID = "invalid"


class ReviewTerminationReason(StrEnum):
    """Terminal review conditions that make another revision unsafe."""

    SAFETY_BOUNDARY_CROSSED = "safety_boundary_crossed"
    EVIDENCE_INTEGRITY_COMPROMISED = "evidence_integrity_compromised"


class IterationDecision(StrEnum):
    """Controller decision after test and review evidence is available."""

    ACCEPT = "accept"
    REVISE = "revise"
    FAIL = "fail"


class FinalStatus(StrEnum):
    """Human-readable final report outcome."""

    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


IMPLEMENTATION_ROLES = {
    AgentRole.SINGLE_AGENT,
    AgentRole.GENERALIST_DEVELOPER,
    AgentRole.FRONTEND_DEVELOPER,
    AgentRole.BACKEND_DEVELOPER,
    AgentRole.INTEGRATOR,
}


def _require_clean_unique_items(
    values: list[str] | tuple[str, ...],
) -> tuple[str, ...]:
    """Clean strings and verify all items are non-empty and unique."""
    cleaned = tuple(value.strip() for value in values)
    if any(not value for value in cleaned):
        raise ValueError("list entries must not be blank")
    if len(cleaned) != len(set(cleaned)):
        raise ValueError("list entries must be unique")
    return cleaned


def _require_safe_relative_path(value: str) -> str:
    """Validate that a path string is safe and purely relative POSIX."""
    if "\\" in value:
        raise ValueError("paths must use POSIX separators")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path == PurePosixPath("."):
        raise ValueError("paths must be safe relative paths")
    return value


def _require_utc(value: datetime) -> datetime:
    """Ensure timestamp has an explicit timezone and convert to UTC."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("artifact timestamps must include a timezone")
    return value.astimezone(UTC)
