"""Adaptive Planning dialogue, proposal validation, approval, and evidence."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import unicodedata
from collections.abc import Callable, Collection, Mapping
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path, PurePosixPath
from string import Template
from typing import Literal, Self
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    ValidationError,
    field_validator,
    model_validator,
)

from software_agent_team.artifacts import (
    AcceptanceCriterion,
    AgentRole,
    ArtifactKind,
    DeliveryMaturity,
    ProductDefinition,
    ProductDefinitionBasis,
    ProductDefinitionDimension,
    ProductDefinitionDisposition,
    ProviderLivenessEvidence,
    ReviewBoundaryKind,
    TaskBrief,
    review_boundary_definition_map,
)
from software_agent_team.budgets import (
    AgentBudget,
    AgentBudgetExceeded,
    AgentBudgetLedger,
    AgentBudgetUsage,
    BudgetAuthority,
    ModelCallCostRecord,
    ModelPricing,
)
from software_agent_team.execution import (
    AgentExecutionActivity,
    AgentExecutionActivityKind,
    AgentExecutionRequest,
    AgentExecutionResult,
    AgentExecutionStatus,
    AgentExecutor,
    AgentToolActionClass,
    AgentToolTargetClass,
)
from software_agent_team.integrity import canonical_model_sha256
from software_agent_team.invocation_lifecycle import (
    InitializationCheckpoint,
    InvocationPhase,
    InvocationStopReason,
)
from software_agent_team.model_costs import CacheTokenUsage
from software_agent_team.model_metadata import ModelMetadataSource
from software_agent_team.model_routing import (
    ModelProfile,
    ModelRoutingError,
    ModelRoutingPolicy,
    resolve_model_route_plan,
)
from software_agent_team.response_corrections import (
    ResponseFailureClass,
    ResponseIssueAuthority,
    ResponseIssueSubject,
    ResponseIssueSubjectKind,
    ResponseValidationDiagnostic,
    SemanticCorrectionOutcome,
    SemanticCorrectionPlan,
    SemanticCorrectionRequestEvidence,
    apply_semantic_correction_with_evidence,
    build_semantic_correction_plan,
    correction_outcome,
    correction_prompt,
    deterministically_remove_forbidden_fields,
    diagnostic_from_invariant,
    diagnostic_from_transport,
    diagnostic_from_validation_error,
    semantic_correction_schema,
)
from software_agent_team.responses import (
    AgentArtifactResponseError,
)
from software_agent_team.submissions import (
    ARTIFACT_SUBMISSION_TOOL,
    AgentSubmissionContract,
    AgentSubmissionEvidence,
    AgentSubmissionPurpose,
    AgentSubmissionStatus,
    canonical_json_sha256,
)
from software_agent_team.teams import (
    AgentCapability,
    AgentSpec,
    ModelRoute,
    ModelRoutingMode,
    PermissionProfile,
    PlanApprovalSource,
    TeamPlan,
    TeamPlanOrigin,
    expected_output_for_capability,
    permission_for_capability,
)

PLANNING_SCHEMA_VERSION = 11
MINIMUM_READABLE_PLANNING_SCHEMA_VERSION = 2
PLANNING_TEMPLATE = Path(__file__).with_name("prompt_templates") / "adaptive_planner.md"
MAX_PLANNING_EVIDENCE_CHARACTERS = 1_000_000
MAX_RESPONSE_NORMALIZATIONS = 100
MAX_RESPONSE_NORMALIZATION_CHARACTERS = 200


class PlanningError(RuntimeError):
    """Raised when an adaptive Planning session cannot continue safely."""


class PlanningIntegrityError(PlanningError):
    """Raised when persisted Planning evidence is incomplete or changed."""


@dataclass(frozen=True)
class _PlanningInvariant:
    """Validator-owned identity and correction authority for one defect."""

    invariant_id: str
    message: str
    paths: tuple[str, ...]
    subjects: tuple[ResponseIssueSubject, ...] = ()


class _PlanningModelInvariantError(ValueError):
    """Carry a typed proposal invariant through Pydantic validation."""

    def __init__(self, invariant: _PlanningInvariant) -> None:
        self.invariant = invariant
        super().__init__(invariant.message)


class _PlanningContextInvariantError(PlanningError):
    """Carry a typed post-schema Planning invariant to response compilation."""

    def __init__(
        self,
        invariant: _PlanningInvariant,
        *,
        failure_class: ResponseFailureClass = ResponseFailureClass.SEMANTIC_CONTEXT,
        authority: ResponseIssueAuthority = ResponseIssueAuthority.MODEL,
    ) -> None:
        self.invariant = invariant
        self.failure_class = failure_class
        self.authority = authority
        super().__init__(invariant.message)


class _PlanningContextInvariantsError(PlanningError):
    """Carry independent typed Planning invariants from one validation pass."""

    def __init__(self, invariants: tuple[_PlanningInvariant, ...]) -> None:
        if not invariants:
            raise ValueError("Planning invariant collection cannot be empty")
        self.invariants = invariants
        super().__init__("; ".join(item.message for item in invariants)[:1500])


def _planning_subjects(
    *items: tuple[ResponseIssueSubjectKind, str],
) -> tuple[ResponseIssueSubject, ...]:
    """Build one canonical structured subject set."""

    return tuple(
        ResponseIssueSubject(kind=kind, identifier=identifier)
        for kind, identifier in sorted(
            set(items),
            key=lambda item: (item[0].value, item[1]),
        )
    )


def _planning_model_invariant(
    invariant_id: str,
    message: str,
    *,
    paths: tuple[str, ...],
    subjects: tuple[ResponseIssueSubject, ...] = (),
) -> _PlanningModelInvariantError:
    return _PlanningModelInvariantError(
        _PlanningInvariant(
            invariant_id=invariant_id,
            message=message,
            paths=tuple(sorted(set(paths))),
            subjects=subjects,
        )
    )


def _planning_context_invariant(
    invariant_id: str,
    message: str,
    *,
    paths: tuple[str, ...],
    subjects: tuple[ResponseIssueSubject, ...] = (),
    failure_class: ResponseFailureClass = ResponseFailureClass.SEMANTIC_CONTEXT,
    authority: ResponseIssueAuthority = ResponseIssueAuthority.MODEL,
) -> _PlanningContextInvariantError:
    return _PlanningContextInvariantError(
        _PlanningInvariant(
            invariant_id=invariant_id,
            message=message,
            paths=tuple(sorted(set(paths))),
            subjects=subjects,
        ),
        failure_class=failure_class,
        authority=authority,
    )


class PlanningSessionStatus(StrEnum):
    """Controller-owned state of one pre-execution Planning session."""

    AUTHORIZED = "authorized"
    CLARIFYING = "clarifying"
    PROPOSED = "proposed"
    APPROVED = "approved"
    CANCELLED = "cancelled"


class PlanningResponseKind(StrEnum):
    """Allowed semantic outcomes of one bootstrap Planner turn."""

    QUESTION = "question"
    PROPOSAL = "proposal"


class PlanningDecisionCategory(StrEnum):
    """Stable category used to assign one Planning decision owner."""

    PRODUCT_REQUIREMENT = "product_requirement"
    RISK_TRADEOFF = "risk_tradeoff"
    PRIVACY_OR_DATA = "privacy_or_data"
    EXTERNAL_ACTION = "external_action"
    ORGANIZATION_POLICY = "organization_policy"
    ACCEPTANCE_SCOPE = "acceptance_scope"
    DELIVERY = "delivery"
    RESOURCE_BUDGET = "resource_budget"
    TEAM = "team"
    MODEL_ROUTE = "model_route"
    LOCAL_IMPLEMENTATION = "local_implementation"
    SCHEDULING = "scheduling"
    SAFETY_INVARIANT = "safety_invariant"
    EVIDENCE_INTEGRITY = "evidence_integrity"


class PlanningDecisionAuthority(StrEnum):
    """Authority that may resolve one category of Planning decision."""

    USER = "user"
    PLANNER_PROPOSAL = "planner_proposal_user_approval"
    AGENT_AUTONOMY = "agent_or_controller_autonomy"
    CONTROLLER_POLICY = "controller_policy"


class PlanningDecisionProvenanceKind(StrEnum):
    """Auditable source kind for one Planning decision."""

    EXPLICIT_INPUT = "explicit_input"
    RESOLVED_QUESTION = "resolved_question"
    PLANNER_RECOMMENDATION = "planner_recommendation"
    AGENT_AUTONOMY = "agent_autonomy"


class PlanningDecisionProvenance(BaseModel):
    """Typed source that supports one current Planning decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: PlanningDecisionProvenanceKind
    source: str = Field(min_length=1, max_length=2000)

    @field_validator("source")
    @classmethod
    def require_clean_source(cls, value: str) -> str:
        return _clean_text(value, label="Planning decision provenance source")

    @model_validator(mode="after")
    def require_canonical_controller_source(self) -> Self:
        expected = {
            PlanningDecisionProvenanceKind.PLANNER_RECOMMENDATION: "planner",
            PlanningDecisionProvenanceKind.AGENT_AUTONOMY: "agent",
        }.get(self.kind)
        if expected is not None and self.source != expected:
            raise ValueError(
                f"{self.kind.value} decision provenance source must be {expected!r}"
            )
        if (
            self.kind is PlanningDecisionProvenanceKind.RESOLVED_QUESTION
            and re.fullmatch(r"[a-z][a-z0-9_]*", self.source) is None
        ):
            raise ValueError(
                "resolved-question decision provenance requires a stable question ID"
            )
        return self


_DECISION_AUTHORITY = {
    PlanningDecisionCategory.PRODUCT_REQUIREMENT: PlanningDecisionAuthority.USER,
    PlanningDecisionCategory.RISK_TRADEOFF: PlanningDecisionAuthority.USER,
    PlanningDecisionCategory.PRIVACY_OR_DATA: PlanningDecisionAuthority.USER,
    PlanningDecisionCategory.EXTERNAL_ACTION: PlanningDecisionAuthority.USER,
    PlanningDecisionCategory.ORGANIZATION_POLICY: PlanningDecisionAuthority.USER,
    PlanningDecisionCategory.ACCEPTANCE_SCOPE: (
        PlanningDecisionAuthority.PLANNER_PROPOSAL
    ),
    PlanningDecisionCategory.DELIVERY: PlanningDecisionAuthority.PLANNER_PROPOSAL,
    PlanningDecisionCategory.RESOURCE_BUDGET: (
        PlanningDecisionAuthority.PLANNER_PROPOSAL
    ),
    PlanningDecisionCategory.TEAM: PlanningDecisionAuthority.PLANNER_PROPOSAL,
    PlanningDecisionCategory.MODEL_ROUTE: PlanningDecisionAuthority.PLANNER_PROPOSAL,
    PlanningDecisionCategory.LOCAL_IMPLEMENTATION: (
        PlanningDecisionAuthority.AGENT_AUTONOMY
    ),
    PlanningDecisionCategory.SCHEDULING: PlanningDecisionAuthority.AGENT_AUTONOMY,
    PlanningDecisionCategory.SAFETY_INVARIANT: (
        PlanningDecisionAuthority.CONTROLLER_POLICY
    ),
    PlanningDecisionCategory.EVIDENCE_INTEGRITY: (
        PlanningDecisionAuthority.CONTROLLER_POLICY
    ),
}

_PRODUCT_DIMENSION_DECISION_CATEGORY = {
    ProductDefinitionDimension.TARGET_USERS: (
        PlanningDecisionCategory.PRODUCT_REQUIREMENT
    ),
    ProductDefinitionDimension.PRIMARY_WORKFLOW: (
        PlanningDecisionCategory.PRODUCT_REQUIREMENT
    ),
    ProductDefinitionDimension.DELIVERY_MATURITY: PlanningDecisionCategory.DELIVERY,
    ProductDefinitionDimension.USABILITY_EXPECTATIONS: (
        PlanningDecisionCategory.ACCEPTANCE_SCOPE
    ),
    ProductDefinitionDimension.OPERATIONAL_EXPECTATIONS: (
        PlanningDecisionCategory.ACCEPTANCE_SCOPE
    ),
    ProductDefinitionDimension.DELIVERY_EXPECTATIONS: PlanningDecisionCategory.DELIVERY,
}


class PlanningProposalSource(StrEnum):
    """Attributable origin of one immutable proposal revision."""

    MODEL = "model"
    STRUCTURED_EDIT = "structured_edit"


class StructuredEditKind(StrEnum):
    """Fields that the controller can edit without accepting internal JSON."""

    MAX_CONCURRENCY = "max_concurrency"
    ITERATION_LIMIT = "iteration_limit"
    AGENT_MODEL = "agent_model"


class AgentWorkload(StrEnum):
    """Planner-owned qualitative workload estimate for one runtime Agent."""

    ROUTINE = "routine"
    SUBSTANTIAL = "substantial"
    COMPLEX = "complex"


class PlanningActivityKind(StrEnum):
    """User-safe checkpoints around one blocking Planning invocation."""

    WAITING_MODEL = "waiting_model"
    INVOCATION_LAUNCHED = "invocation_launched"
    INITIALIZING = "initializing"
    INITIALIZATION_PROGRESS = "initialization_progress"
    INITIALIZATION_LIVENESS_DEGRADED = "initialization_liveness_degraded"
    INITIALIZATION_STALL_SUSPECTED = "initialization_stall_suspected"
    INITIALIZATION_STALL_RECOVERED = "initialization_stall_recovered"
    INITIALIZATION_STALLED = "initialization_stalled"
    PROVIDER_WAIT = "provider_wait"
    TOOL_ACTIVE = "tool_active"
    FINALIZING_RESPONSE = "finalizing_response"
    STOPPING = "stopping"
    COLLECTING_EVIDENCE = "collecting_evidence"
    STOPPED = "stopped"
    PROVIDER_ACTIVITY = "provider_activity"
    TOOL_STARTED = "tool_started"
    TOOL_COMPLETED = "tool_completed"
    LIVENESS_DEGRADED = "liveness_degraded"
    STALL_SUSPECTED = "stall_suspected"
    STALL_RECOVERED = "stall_recovered"
    PROVIDER_STALLED = "provider_stalled"
    FINALIZATION_PROGRESS = "finalization_progress"
    FINALIZATION_STALL_SUSPECTED = "finalization_stall_suspected"
    FINALIZATION_STALL_RECOVERED = "finalization_stall_recovered"
    RESPONSE_FINALIZATION_STALLED = "response_finalization_stalled"
    RESPONSE_RECEIVED = "response_received"
    BUDGET_UPDATED = "budget_updated"
    CORRECTION_SCHEDULED = "correction_scheduled"
    RESPONSE_VALIDATED = "response_validated"


@dataclass(frozen=True)
class PlanningActivity:
    """Ephemeral Planning progress without exposing prompts or reasoning."""

    kind: PlanningActivityKind
    attempt: int
    maximum_attempts: int | None
    model: str
    duration_ms: int | None = None
    execution_status: AgentExecutionStatus | None = None
    inactivity_ms: int | None = None
    silence_seconds: float | None = None
    stall_grace_seconds: float | None = None
    policy_source: str | None = None
    degradation_reason: str | None = None
    invocation_phase: InvocationPhase | None = None
    stop_reason: InvocationStopReason | None = None
    initialization_checkpoint: InitializationCheckpoint | None = None
    shutdown_grace_seconds: float | None = None
    action: str | None = None
    tool_action_class: AgentToolActionClass | None = None
    tool_target_class: AgentToolTargetClass | None = None
    tool_detail: str | None = None
    budget_usage: AgentBudgetUsage | None = None
    budget_ceiling_usd: Decimal | None = None
    pricing_source: ModelMetadataSource | None = None


PlanningActivityHandler = Callable[[PlanningActivity], None]


class TerminalPlanningProgress:
    """Show bounded Planning wait heartbeats and validation checkpoints."""

    def __init__(
        self,
        *,
        write: Callable[[str], None] = print,
        heartbeat_seconds: float = 10.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if heartbeat_seconds <= 0:
            raise ValueError("Planning heartbeat must be positive")
        self.write = write
        self.heartbeat_seconds = heartbeat_seconds
        self.monotonic = monotonic
        self._lock = threading.Lock()
        self._waiting: tuple[threading.Event, threading.Thread] | None = None

    def __call__(self, activity: PlanningActivity) -> None:
        if activity.kind is PlanningActivityKind.WAITING_MODEL:
            attempt_label = (
                str(activity.attempt)
                if activity.maximum_attempts is None
                else f"{activity.attempt}/{activity.maximum_attempts}"
            )
            self._start_waiting(
                activity,
                visible=(
                    f"● Planning invocation is queued for {activity.model} "
                    f"(attempt {attempt_label})"
                ),
                heartbeat=(f"Planning invocation is queued (attempt {attempt_label})"),
            )
            return

        if activity.kind in {
            PlanningActivityKind.INITIALIZING,
            PlanningActivityKind.PROVIDER_WAIT,
            PlanningActivityKind.TOOL_ACTIVE,
            PlanningActivityKind.FINALIZING_RESPONSE,
        }:
            label = {
                PlanningActivityKind.INITIALIZING: (
                    "Planning is initializing an attributable OpenClaw turn"
                ),
                PlanningActivityKind.PROVIDER_WAIT: (
                    f"Planning is waiting for {activity.model}"
                ),
                PlanningActivityKind.TOOL_ACTIVE: (
                    "Planning has attributable tool operations active"
                ),
                PlanningActivityKind.FINALIZING_RESPONSE: (
                    "Planning received the terminal model response and OpenClaw "
                    "is finalizing the result"
                ),
            }[activity.kind]
            self._start_waiting(
                activity,
                visible=f"● {label}",
                heartbeat=label,
            )
            return

        if activity.kind in {
            PlanningActivityKind.STOPPING,
            PlanningActivityKind.COLLECTING_EVIDENCE,
            PlanningActivityKind.STOPPED,
        }:
            self.close()
            reason = (
                "unknown"
                if activity.stop_reason is None
                else activity.stop_reason.value
            )
            if activity.kind is PlanningActivityKind.STOPPING:
                message = (
                    f"■ Planning is stopping ({reason}); shutdown ceiling "
                    f"{(activity.shutdown_grace_seconds or 0):g}s"
                )
            elif activity.kind is PlanningActivityKind.COLLECTING_EVIDENCE:
                message = "… Planning process stopped; collecting attributable evidence"
            else:
                message = (
                    f"■ Planning invocation stopped ({reason}); process outcome "
                    "and evidence are known"
                )
            self._print(message)
            return

        checkpoint = (
            "unknown"
            if activity.initialization_checkpoint is None
            else activity.initialization_checkpoint.value
        )
        intermediate: str | None = None
        if activity.kind is PlanningActivityKind.INVOCATION_LAUNCHED:
            intermediate = "  Planning entered the execution adapter"
        elif activity.kind is PlanningActivityKind.INITIALIZATION_PROGRESS:
            intermediate = f"  Planning initialization verified {checkpoint}"
        elif activity.kind is PlanningActivityKind.INITIALIZATION_LIVENESS_DEGRADED:
            intermediate = (
                "! Planning initialization observer is unavailable: "
                f"{activity.degradation_reason}"
            )
        elif activity.kind is PlanningActivityKind.INITIALIZATION_STALL_SUSPECTED:
            intermediate = (
                "? Planning initialization has made no progress for "
                f"{(activity.inactivity_ms or 0) / 1000:.1f}s; waiting the final "
                f"{(activity.stall_grace_seconds or 0):g}s diagnostic window"
            )
        elif activity.kind is PlanningActivityKind.INITIALIZATION_STALL_RECOVERED:
            intermediate = (
                f"↻ Planning initialization advanced to {checkpoint} during grace"
            )
        elif activity.kind is PlanningActivityKind.INITIALIZATION_STALLED:
            intermediate = (
                "! Planning initialization remained stalled; a typed stop follows"
            )
        elif activity.kind is PlanningActivityKind.PROVIDER_ACTIVITY:
            intermediate = "  Planning received provider stream activity"
        elif activity.kind is PlanningActivityKind.FINALIZATION_PROGRESS:
            intermediate = "  Planning result finalization made observable progress"
        elif activity.kind is PlanningActivityKind.FINALIZATION_STALL_SUSPECTED:
            intermediate = (
                "? Planning result finalization has made no observable progress for "
                f"{(activity.inactivity_ms or 0) / 1000:.1f}s; waiting the final "
                f"{(activity.stall_grace_seconds or 0):g}s diagnostic window"
            )
        elif activity.kind is PlanningActivityKind.FINALIZATION_STALL_RECOVERED:
            intermediate = "↻ Planning result finalization resumed during grace"
        elif activity.kind is PlanningActivityKind.RESPONSE_FINALIZATION_STALLED:
            intermediate = (
                "! Planning received the terminal model response, but OpenClaw "
                "result finalization remained stalled; a typed stop follows"
            )
        elif activity.kind is PlanningActivityKind.TOOL_STARTED:
            action = (
                "using"
                if activity.tool_action_class is None
                else activity.tool_action_class.value
            )
            target = (
                "a sandboxed tool operation"
                if activity.tool_target_class is None
                else activity.tool_target_class.value.replace("_", " ")
            )
            detail = (
                "" if activity.tool_detail is None else f" ({activity.tool_detail})"
            )
            intermediate = f"  Planning started {action} {target}{detail}"
        elif activity.kind is PlanningActivityKind.TOOL_COMPLETED:
            action = (
                "using"
                if activity.tool_action_class is None
                else activity.tool_action_class.value
            )
            target = (
                "a sandboxed tool operation"
                if activity.tool_target_class is None
                else activity.tool_target_class.value.replace("_", " ")
            )
            detail = (
                "" if activity.tool_detail is None else f" ({activity.tool_detail})"
            )
            intermediate = f"  Planning completed {action} {target}{detail}"
        elif activity.kind is PlanningActivityKind.LIVENESS_DEGRADED:
            intermediate = (
                "! Planning provider liveness is degraded: "
                f"{activity.degradation_reason}; SAT will preserve the call "
                "instead of inferring a stall from silence"
            )
        elif activity.kind is PlanningActivityKind.STALL_SUSPECTED:
            intermediate = (
                "? Planning has produced no trusted activity for "
                f"{(activity.inactivity_ms or 0) / 1000:.1f}s; checking the "
                "private stream and attributable tool state for another "
                f"{(activity.stall_grace_seconds or 0):g}s before interruption "
                f"({activity.policy_source})"
            )
        elif activity.kind is PlanningActivityKind.STALL_RECOVERED:
            intermediate = (
                "↻ Planning provider activity recovered during the "
                f"{(activity.stall_grace_seconds or 0):g}s grace period"
            )
        elif activity.kind is PlanningActivityKind.PROVIDER_STALLED:
            intermediate = (
                "! Planning provider remained silent for "
                f"{(activity.silence_seconds or 0):g}s; stall confirmed and the "
                "separate stopping transition follows"
            )
        if intermediate is not None:
            self._print(intermediate)
            return

        if activity.kind is PlanningActivityKind.BUDGET_UPDATED:
            assert activity.budget_usage is not None
            assert activity.budget_ceiling_usd is not None
            usage = activity.budget_usage
            remaining = max(
                Decimal(0),
                activity.budget_ceiling_usd - usage.known_estimated_cost_usd,
            )
            source = (
                "unknown"
                if activity.pricing_source is None
                else activity.pricing_source.value
            )
            self._print(
                "  Task model spend: "
                f"${usage.known_estimated_cost_usd:.6f} estimated / "
                f"${activity.budget_ceiling_usd} authorized; "
                f"${remaining:.6f} recorded remaining; price source {source}"
            )
            return

        self.close()
        if activity.kind is PlanningActivityKind.RESPONSE_RECEIVED:
            duration = 0 if activity.duration_ms is None else activity.duration_ms
            status = (
                "unknown"
                if activity.execution_status is None
                else activity.execution_status.value
            )
            self._print(
                f"→ Planning response received in {duration / 1000:.1f}s ({status})"
            )
        elif activity.kind is PlanningActivityKind.CORRECTION_SCHEDULED:
            next_attempt = activity.attempt + 1
            attempt_label = (
                str(next_attempt)
                if activity.maximum_attempts is None
                else f"{next_attempt}/{activity.maximum_attempts}"
            )
            self._print(
                "↻ Planning response has targeted model-owned fields; "
                f"requesting correction attempt {attempt_label}"
            )
        else:
            self._print("✓ Planning response validated")

    def close(self) -> None:
        with self._lock:
            waiting = self._waiting
            self._waiting = None
        if waiting is not None:
            waiting[0].set()
            waiting[1].join(timeout=min(self.heartbeat_seconds, 0.2))

    def _start_waiting(
        self,
        activity: PlanningActivity,
        *,
        visible: str,
        heartbeat: str,
    ) -> None:
        self.close()
        self._print(visible)
        stop = threading.Event()
        started = self.monotonic()
        attempt_label = (
            str(activity.attempt)
            if activity.maximum_attempts is None
            else f"{activity.attempt}/{activity.maximum_attempts}"
        )
        thread = threading.Thread(
            target=self._heartbeat,
            args=(stop, started, heartbeat, attempt_label),
            name="sat-planning-progress",
            daemon=True,
        )
        with self._lock:
            self._waiting = (stop, thread)
        thread.start()

    def _heartbeat(
        self,
        stop: threading.Event,
        started: float,
        message: str,
        attempt_label: str,
    ) -> None:
        while not stop.wait(self.heartbeat_seconds):
            elapsed = max(0, int(self.monotonic() - started))
            minutes, seconds = divmod(elapsed, 60)
            self._print(
                f"  {message} (attempt {attempt_label}): "
                f"{minutes:02d}:{seconds:02d} elapsed"
            )

    def _print(self, value: str) -> None:
        with self._lock:
            self.write(value)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Planning timestamps must include a timezone")
    return value.astimezone(UTC)


def _clean_text(value: str, *, label: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{label} must not be blank")
    return cleaned


def _clean_unique(values: tuple[str, ...], *, label: str) -> tuple[str, ...]:
    cleaned = tuple(value.strip() for value in values)
    if any(not value for value in cleaned):
        raise ValueError(f"{label} entries must not be blank")
    if len(cleaned) != len(set(cleaned)):
        raise ValueError(f"{label} entries must be unique")
    return cleaned


def _strip_redundant_stable_id_prefix(value: str, stable_id: str) -> str:
    """Remove only repeated copies of the requirement's stable-ID presentation."""

    return re.sub(rf"^(?:{re.escape(stable_id)}\s*:\s*)+", "", value)


def _render_prefixed_text(prefix: str, value: str) -> tuple[str, ...]:
    """Render untrusted text without letting continuations escape their field."""

    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    visible = "".join(
        character
        if character == "\n"
        or unicodedata.category(character) not in {"Cc", "Cf", "Zl", "Zp"}
        else character.encode("unicode_escape").decode("ascii")
        for character in normalized
    )
    fragments = visible.split("\n")
    continuation = " " * len(prefix)
    return (
        prefix + fragments[0],
        *(continuation + fragment for fragment in fragments[1:]),
    )


def _safe_path(value: str) -> str:
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
        raise ValueError("paths must be canonical safe relative POSIX paths")
    return cleaned


def _canonicalize_model_path(value: object) -> object:
    """Normalize only unambiguous safe relative-path presentation variants."""

    if not isinstance(value, str):
        return value
    cleaned = value.strip()
    path = PurePosixPath(cleaned)
    if (
        not cleaned
        or "\\" in cleaned
        or path.is_absolute()
        or path == PurePosixPath(".")
        or ".." in path.parts
    ):
        return value
    return str(path)


def _normalize_planning_response_payload(
    payload: dict[str, object],
    *,
    profile_criterion_ids: Collection[str] = (),
    user_inputs: Collection[str] = (),
) -> tuple[dict[str, object], tuple[str, ...]]:
    """Apply bounded semantic-preserving normalization before strict validation."""

    normalized: dict[str, object] = json.loads(json.dumps(payload))
    changes: list[str] = []
    if "kind" not in normalized:
        candidates = tuple(
            name
            for name in ("question", "proposal")
            if normalized.get(name) is not None
        )
        if len(candidates) == 1:
            normalized["kind"] = candidates[0]
            changes.append(f"inferred response kind as {candidates[0]}")

    question = normalized.get("question")
    if isinstance(question, dict):
        try:
            question_category = PlanningDecisionCategory(
                question.get("decision_category")
            )
        except (TypeError, ValueError):
            question_category = None
        if question_category is not None:
            expected_owner = _DECISION_AUTHORITY[question_category]
            if question.get("decision_owner") != expected_owner.value:
                question["decision_owner"] = expected_owner.value
                changes.append(
                    "compiled question.decision_owner from category "
                    f"{question_category.value}"
                )

    proposal = normalized.get("proposal")
    if not isinstance(proposal, dict):
        return normalized, tuple(changes)
    requirements = proposal.get("requirements")
    if isinstance(requirements, list) and any(
        isinstance(item, dict) for item in requirements
    ):
        if not all(isinstance(item, dict) for item in requirements):
            raise _planning_context_invariant(
                "planning_requirement_atom_shape",
                (
                    "proposal requirements must be one array of atomic objects; "
                    "string and object entries cannot be mixed"
                ),
                paths=("/proposal/requirements",),
            )
        compiled_requirements: list[ProposedRequirement] = []
        for requirement_index, item in enumerate(requirements):
            try:
                compiled_requirements.append(ProposedRequirement.model_validate(item))
            except ValidationError as error:
                raise _planning_context_invariant(
                    "planning_requirement_atom_schema",
                    (
                        f"proposal requirement {requirement_index} must contain "
                        "exactly one stable REQ_ id and one non-empty description: "
                        f"{_safe_validation_detail(error)}"
                    ),
                    paths=(f"/proposal/requirements/{requirement_index}",),
                ) from error
        compiled_ids = tuple(item.id for item in compiled_requirements)
        compiled_descriptions = tuple(
            item.description for item in compiled_requirements
        )
        if len(compiled_ids) != len(set(compiled_ids)):
            raise _planning_context_invariant(
                "planning_requirement_atom_id_unique",
                "proposal requirement objects must use unique stable REQ_ IDs",
                paths=("/proposal/requirements",),
            )
        if len(compiled_descriptions) != len(set(compiled_descriptions)):
            raise _planning_context_invariant(
                "planning_requirement_atom_description_unique",
                "proposal requirement objects must use unique descriptions",
                paths=("/proposal/requirements",),
            )
        proposal["requirements"] = list(compiled_descriptions)
        proposal["requirement_ids"] = list(compiled_ids)
        requirements = proposal["requirements"]
        changes.append(
            "compiled atomic proposal.requirements into canonical descriptions "
            "and stable IDs"
        )
    requirement_ids = proposal.get("requirement_ids")
    if (
        isinstance(requirements, list)
        and isinstance(requirement_ids, list)
        and len(requirements) == len(requirement_ids)
    ):
        for requirement_index, (description, requirement_id) in enumerate(
            zip(requirements, requirement_ids, strict=True)
        ):
            if (
                not isinstance(description, str)
                or not isinstance(requirement_id, str)
                or re.fullmatch(r"REQ_[A-Z0-9_]+", requirement_id) is None
            ):
                continue
            canonical_description = _strip_redundant_stable_id_prefix(
                description,
                requirement_id,
            )
            if canonical_description != description:
                requirements[requirement_index] = canonical_description
                changes.append(
                    "removed redundant stable ID prefix from "
                    f"proposal.requirements[{requirement_index}]"
                )

    assumptions = proposal.get("assumptions")
    if isinstance(assumptions, list) and any(
        isinstance(item, dict) for item in assumptions
    ):
        if not all(isinstance(item, dict) for item in assumptions):
            raise _planning_context_invariant(
                "planning_assumption_atom_shape",
                (
                    "proposal assumptions must be one array of atomic objects; "
                    "string and object entries cannot be mixed"
                ),
                paths=("/proposal/assumptions",),
            )
        compiled_assumptions: list[ProposedAssumption] = []
        for assumption_index, item in enumerate(assumptions):
            try:
                compiled_assumptions.append(ProposedAssumption.model_validate(item))
            except ValidationError as error:
                raise _planning_context_invariant(
                    "planning_assumption_atom_schema",
                    (
                        f"proposal assumption {assumption_index} must contain "
                        "exactly one statement and one stable autonomous decision "
                        f"reference: {_safe_validation_detail(error)}"
                    ),
                    paths=(f"/proposal/assumptions/{assumption_index}",),
                ) from error
        compiled_statements = tuple(item.statement for item in compiled_assumptions)
        if len(compiled_statements) != len(set(compiled_statements)):
            raise _planning_context_invariant(
                "planning_assumption_atom_statement_unique",
                "proposal assumption objects must use unique statements",
                paths=("/proposal/assumptions",),
            )
        proposal["assumptions"] = list(compiled_statements)
        proposal["assumption_decision_ids"] = [
            item.decision_id for item in compiled_assumptions
        ]
        changes.append(
            "compiled atomic proposal.assumptions into canonical statements "
            "and autonomous decision references"
        )

    product_definition = proposal.get("product_definition")
    product_dimensions = (
        product_definition if isinstance(product_definition, dict) else {}
    )
    for dimension in ProductDefinitionDimension:
        if dimension is ProductDefinitionDimension.PRIMARY_WORKFLOW:
            # Core workflow materiality is never normalized away. Preserve the
            # proposed trace so current validation can request an atomic repair.
            continue
        item = product_dimensions.get(dimension.value)
        if (
            not isinstance(item, dict)
            or item.get("disposition")
            != ProductDefinitionDisposition.NOT_MATERIAL.value
        ):
            continue
        removed_references = False
        for field_name in ("requirement_ids", "criterion_ids", "decision_ids"):
            references = item.get(field_name)
            if isinstance(references, list) and references:
                item[field_name] = []
                removed_references = True
        if removed_references:
            changes.append(
                "removed downstream references from not-material "
                f"proposal.product_definition.{dimension.value}"
            )

    decisions = proposal.get("decisions")
    assumption_decision_ids = proposal.get("assumption_decision_ids")
    if isinstance(decisions, list):
        canonical_id_counts: dict[str, int] = {}
        for decision in decisions:
            decision_id = decision.get("id") if isinstance(decision, dict) else None
            if (
                isinstance(decision_id, str)
                and re.fullmatch(r"DECISION_[A-Za-z0-9_]+", decision_id) is not None
            ):
                canonical_id = decision_id.upper()
                canonical_id_counts[canonical_id] = (
                    canonical_id_counts.get(canonical_id, 0) + 1
                )
        for decision_index, decision in enumerate(decisions):
            if not isinstance(decision, dict):
                continue
            decision_id = decision.get("id")
            if (
                not isinstance(decision_id, str)
                or re.fullmatch(r"DECISION_[A-Za-z0-9_]+", decision_id) is None
            ):
                continue
            canonical_id = decision_id.upper()
            if (
                canonical_id == decision_id
                or canonical_id_counts.get(canonical_id) != 1
            ):
                continue
            decision["id"] = canonical_id
            changes.append(
                f"canonicalized proposal.decisions[{decision_index}].id as "
                f"{canonical_id}"
            )
        if isinstance(assumption_decision_ids, list):
            for reference_index, reference in enumerate(assumption_decision_ids):
                if (
                    not isinstance(reference, str)
                    or re.fullmatch(r"DECISION_[A-Za-z0-9_]+", reference) is None
                ):
                    continue
                canonical_id = reference.upper()
                if (
                    canonical_id == reference
                    or canonical_id_counts.get(canonical_id) != 1
                ):
                    continue
                assumption_decision_ids[reference_index] = canonical_id
                changes.append(
                    "canonicalized "
                    f"proposal.assumption_decision_ids[{reference_index}] as "
                    f"{canonical_id}"
                )

        explicit_sources_by_decision: dict[str, set[str]] = {}
        for dimension in ProductDefinitionDimension:
            item = product_dimensions.get(dimension.value)
            if not isinstance(item, dict):
                continue
            references = item.get("decision_ids")
            if not isinstance(references, list):
                continue
            for reference_index, reference in enumerate(references):
                if (
                    not isinstance(reference, str)
                    or re.fullmatch(r"DECISION_[A-Za-z0-9_]+", reference) is None
                ):
                    continue
                canonical_id = reference.upper()
                if (
                    canonical_id != reference
                    and canonical_id_counts.get(canonical_id) == 1
                ):
                    references[reference_index] = canonical_id
                    changes.append(
                        "canonicalized proposal.product_definition."
                        f"{dimension.value}.decision_ids[{reference_index}] as "
                        f"{canonical_id}"
                    )
                if item.get(
                    "disposition"
                ) == ProductDefinitionDisposition.EXPLICIT_INPUT.value and isinstance(
                    item.get("source"), str
                ):
                    explicit_sources_by_decision.setdefault(canonical_id, set()).add(
                        item["source"]
                    )

        explicit_product_sources = {
            (
                _PRODUCT_DIMENSION_DECISION_CATEGORY[dimension],
                _normalized_evidence_text(item["source"]),
            )
            for dimension in ProductDefinitionDimension
            if isinstance((item := product_dimensions.get(dimension.value)), dict)
            and item.get("disposition")
            == ProductDefinitionDisposition.EXPLICIT_INPUT.value
            and isinstance(item.get("source"), str)
            and item["source"].strip()
        }
        normalized_user_inputs = tuple(
            _normalized_evidence_text(value)
            for value in user_inputs
            if isinstance(value, str) and value.strip()
        )
        redundant_direct_product_decision_ids: set[str] = set()
        for decision_index, decision in enumerate(decisions):
            if not isinstance(decision, dict):
                continue
            category_value = decision.get("category")
            try:
                category = PlanningDecisionCategory(category_value)
            except (TypeError, ValueError):
                continue
            expected_authority = _DECISION_AUTHORITY[category]
            if decision.get("authority") != expected_authority.value:
                decision["authority"] = expected_authority.value
                changes.append(
                    "compiled proposal.decisions"
                    f"[{decision_index}].authority from category {category.value}"
                )

            provenance = decision.get("provenance")
            legacy_question_id = decision.get("question_id")
            if provenance is None:
                compiled_provenance: dict[str, str] | None = None
                if isinstance(legacy_question_id, str) and legacy_question_id:
                    compiled_provenance = {
                        "kind": PlanningDecisionProvenanceKind.RESOLVED_QUESTION.value,
                        "source": legacy_question_id,
                    }
                elif expected_authority is PlanningDecisionAuthority.PLANNER_PROPOSAL:
                    compiled_provenance = {
                        "kind": (
                            PlanningDecisionProvenanceKind.PLANNER_RECOMMENDATION.value
                        ),
                        "source": "planner",
                    }
                elif expected_authority is PlanningDecisionAuthority.AGENT_AUTONOMY:
                    compiled_provenance = {
                        "kind": PlanningDecisionProvenanceKind.AGENT_AUTONOMY.value,
                        "source": "agent",
                    }
                elif expected_authority is PlanningDecisionAuthority.USER:
                    decision_id = decision.get("id")
                    candidates = (
                        explicit_sources_by_decision.get(decision_id, set())
                        if isinstance(decision_id, str)
                        else set()
                    )
                    came_from_product_definition = bool(candidates)
                    summary = decision.get("summary")
                    if (
                        not candidates
                        and isinstance(summary, str)
                        and any(
                            _normalized_evidence_text(summary) in value
                            for value in normalized_user_inputs
                        )
                    ):
                        candidates = {summary}
                    if len(candidates) == 1:
                        compiled_provenance = {
                            "kind": PlanningDecisionProvenanceKind.EXPLICIT_INPUT.value,
                            "source": next(iter(candidates)),
                        }
                        decision_id = decision.get("id")
                        if came_from_product_definition and isinstance(
                            decision_id, str
                        ):
                            redundant_direct_product_decision_ids.add(decision_id)
                if compiled_provenance is not None:
                    decision["provenance"] = compiled_provenance
                    changes.append(
                        "compiled proposal.decisions"
                        f"[{decision_index}].provenance from existing decision source"
                    )
            effective_provenance = decision.get("provenance")
            if (
                isinstance(effective_provenance, dict)
                and effective_provenance.get("kind")
                == PlanningDecisionProvenanceKind.EXPLICIT_INPUT.value
                and isinstance(effective_provenance.get("source"), str)
                and decision.get("summary") != effective_provenance["source"]
            ):
                decision["summary"] = effective_provenance["source"]
                changes.append(
                    "compiled proposal.decisions"
                    f"[{decision_index}].summary from exact direct-input source"
                )
            if (
                isinstance(decision.get("id"), str)
                and isinstance(effective_provenance, dict)
                and effective_provenance.get("kind")
                == PlanningDecisionProvenanceKind.EXPLICIT_INPUT.value
                and isinstance(effective_provenance.get("source"), str)
                and (
                    category,
                    _normalized_evidence_text(effective_provenance["source"]),
                )
                in explicit_product_sources
            ):
                redundant_direct_product_decision_ids.add(decision["id"])
            if (
                isinstance(legacy_question_id, str)
                and isinstance(decision.get("provenance"), dict)
                and decision["provenance"].get("kind")
                == PlanningDecisionProvenanceKind.RESOLVED_QUESTION.value
                and decision["provenance"].get("source") == legacy_question_id
            ):
                del decision["question_id"]
                changes.append(
                    f"retired legacy proposal.decisions[{decision_index}].question_id"
                )

        for dimension in ProductDefinitionDimension:
            item = product_dimensions.get(dimension.value)
            if not isinstance(item, dict):
                continue
            references = item.get("decision_ids")
            if (
                item.get("disposition")
                == ProductDefinitionDisposition.EXPLICIT_INPUT.value
                and isinstance(references, list)
                and references
                and (item.get("requirement_ids") or item.get("criterion_ids"))
            ):
                item["decision_ids"] = []
                changes.append(
                    "removed redundant decision references from "
                    f"proposal.product_definition.{dimension.value}"
                )

        remaining_decision_references = {
            reference
            for dimension in ProductDefinitionDimension
            if isinstance((item := product_dimensions.get(dimension.value)), dict)
            and isinstance(item.get("decision_ids"), list)
            for reference in item["decision_ids"]
            if isinstance(reference, str)
        }
        if isinstance(assumption_decision_ids, list):
            remaining_decision_references.update(
                reference
                for reference in assumption_decision_ids
                if isinstance(reference, str)
            )
        retained_decisions: list[object] = []
        for decision_index, decision in enumerate(decisions):
            decision_id = decision.get("id") if isinstance(decision, dict) else None
            if (
                isinstance(decision_id, str)
                and decision_id in redundant_direct_product_decision_ids
                and decision_id not in remaining_decision_references
            ):
                changes.append(
                    "removed redundant direct-input decision "
                    f"{decision_id} from proposal.decisions[{decision_index}]"
                )
                continue
            retained_decisions.append(decision)
        proposal["decisions"] = retained_decisions
    acceptance_criteria = proposal.get("acceptance_criteria")
    tasks = proposal.get("tasks")
    agents = proposal.get("agents")
    controller_owned_ids = set(profile_criterion_ids)
    if isinstance(acceptance_criteria, list) and controller_owned_ids:

        def string_list(value: object) -> tuple[str, ...]:
            return (
                tuple(item for item in value if isinstance(item, str))
                if isinstance(value, list)
                else ()
            )

        criterion_ids = [
            criterion.get("id") if isinstance(criterion, dict) else None
            for criterion in acceptance_criteria
        ]
        criterion_id_counts = {
            criterion_id: criterion_ids.count(criterion_id)
            for criterion_id in criterion_ids
            if isinstance(criterion_id, str)
        }
        declared_requirement_ids = set(string_list(proposal.get("requirement_ids")))
        covered_requirement_ids = {
            requirement_id
            for criterion in acceptance_criteria
            if isinstance(criterion, dict)
            and criterion.get("id") not in controller_owned_ids
            for requirement_id in string_list(criterion.get("requirement_ids"))
        }
        uncovered_requirement_ids = declared_requirement_ids - covered_requirement_ids
        deconfliction_candidates = [
            (
                criterion_index,
                set(string_list(criterion.get("requirement_ids"))),
            )
            for criterion_index, criterion in enumerate(acceptance_criteria)
            if isinstance(criterion, dict)
            and isinstance(criterion.get("id"), str)
            and criterion["id"] in controller_owned_ids
            and criterion_id_counts[criterion["id"]] == 1
        ]
        retained_collision_indexes: set[int] = set()
        while uncovered_requirement_ids:
            eligible = [
                (index, requirements)
                for index, requirements in deconfliction_candidates
                if index not in retained_collision_indexes
                and requirements & uncovered_requirement_ids
            ]
            if not eligible:
                break
            selected_index, selected_requirements = min(
                eligible,
                key=lambda item: (
                    -len(item[1] & uncovered_requirement_ids),
                    item[0],
                ),
            )
            retained_collision_indexes.add(selected_index)
            uncovered_requirement_ids -= selected_requirements

        used_criterion_ids = {
            criterion_id
            for criterion_id in criterion_ids
            if isinstance(criterion_id, str)
        } | controller_owned_ids
        renamed_criterion_ids: dict[str, str] = {}
        retained_criteria: list[object] = []
        for criterion_index, criterion in enumerate(acceptance_criteria):
            criterion_id = criterion.get("id") if isinstance(criterion, dict) else None
            if isinstance(criterion_id, str) and criterion_id in controller_owned_ids:
                if criterion_index in retained_collision_indexes:
                    suffix = (
                        criterion_id.removeprefix("AC_")
                        if criterion_id.startswith("AC_")
                        else criterion_id
                    )
                    base_id = f"AC_TASK_{suffix}"
                    replacement_id = base_id
                    counter = 2
                    while replacement_id in used_criterion_ids:
                        replacement_id = f"{base_id}_{counter}"
                        counter += 1
                    assert isinstance(criterion, dict)
                    criterion["id"] = replacement_id
                    used_criterion_ids.add(replacement_id)
                    renamed_criterion_ids[criterion_id] = replacement_id
                    retained_criteria.append(criterion)
                    changes.append(
                        f"deconflicted model criterion {criterion_id} as "
                        f"{replacement_id} from controller-owned profile ID"
                    )
                    continue
                changes.append(
                    "removed controller-owned profile criterion "
                    f"{criterion_id} from proposal.acceptance_criteria"
                    f"[{criterion_index}]"
                )
                continue
            retained_criteria.append(criterion)
        proposal["acceptance_criteria"] = retained_criteria
        if isinstance(tasks, list) and renamed_criterion_ids:
            for task in tasks:
                if not isinstance(task, dict):
                    continue
                bindings = task.get("acceptance_criteria")
                if not isinstance(bindings, list):
                    continue
                expanded_bindings: list[object] = []
                for binding in bindings:
                    replacement = renamed_criterion_ids.get(binding)
                    if replacement is not None:
                        expanded_bindings.append(replacement)
                    expanded_bindings.append(binding)
                deduplicated_bindings: list[object] = []
                for binding in expanded_bindings:
                    if binding not in deduplicated_bindings:
                        deduplicated_bindings.append(binding)
                task["acceptance_criteria"] = deduplicated_bindings
    if isinstance(tasks, list):
        for task_index, task in enumerate(tasks):
            if not isinstance(task, dict):
                continue
            paths = task.get("expected_paths")
            if not isinstance(paths, list):
                continue
            for path_index, value in enumerate(paths):
                canonical = _canonicalize_model_path(value)
                if canonical != value:
                    paths[path_index] = canonical
                    changes.append(
                        "canonicalized "
                        f"proposal.tasks[{task_index}].expected_paths[{path_index}]"
                    )
    if isinstance(agents, list):
        for agent_index, agent in enumerate(agents):
            if not isinstance(agent, dict) or "workspace_scope" not in agent:
                continue
            value = agent["workspace_scope"]
            canonical = _canonicalize_model_path(value)
            if canonical != value:
                agent["workspace_scope"] = canonical
                changes.append(
                    f"canonicalized proposal.agents[{agent_index}].workspace_scope"
                )
    return normalized, tuple(changes)


def _digest_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _safe_validation_detail(error: ValueError) -> str:
    if isinstance(error, ValidationError):
        details = []
        for issue in error.errors(
            include_url=False,
            include_context=False,
            include_input=False,
        ):
            location = ".".join(str(item) for item in issue["loc"]) or "response"
            details.append(f"{location}: {issue['msg']}")
        return "; ".join(details)[:1500]
    return str(error)[:1500]


def _planning_invariant_diagnostic(
    payload: dict[str, object],
    invariant: _PlanningInvariant,
    *,
    authority: ResponseIssueAuthority = ResponseIssueAuthority.MODEL,
    failure_class: ResponseFailureClass = ResponseFailureClass.SEMANTIC_CONTEXT,
) -> ResponseValidationDiagnostic:
    """Compile validator-owned identity without parsing human error text."""

    return diagnostic_from_invariant(
        payload,
        failure_class=failure_class,
        authority=authority,
        code="planning_context",
        invariant_id=invariant.invariant_id,
        subjects=invariant.subjects,
        message=invariant.message,
        paths=invariant.paths,
    )


def _planning_invariants_diagnostic(
    payload: dict[str, object],
    invariants: tuple[_PlanningInvariant, ...],
) -> ResponseValidationDiagnostic:
    """Compile independent atomic invariants into one correction request."""

    diagnostics = tuple(
        _planning_invariant_diagnostic(payload, invariant) for invariant in invariants
    )
    return ResponseValidationDiagnostic(
        failure_class=ResponseFailureClass.SEMANTIC_CONTEXT,
        response_sha256=canonical_json_sha256(payload),
        issues=tuple(
            issue for diagnostic in diagnostics for issue in diagnostic.issues
        ),
        correction_paths=tuple(
            sorted(
                {
                    path
                    for diagnostic in diagnostics
                    for path in diagnostic.correction_paths
                }
            )
        ),
    )


def _planning_validation_diagnostic(
    error: ValidationError,
    payload: dict[str, object],
) -> ResponseValidationDiagnostic:
    """Recover precise fields from proposal-level relational validators."""

    issues = error.errors(
        include_url=False,
        include_context=True,
        include_input=False,
    )
    typed = tuple(
        context_error.invariant
        for issue in issues
        if isinstance((context := issue.get("ctx")), dict)
        and isinstance(
            (context_error := context.get("error")),
            _PlanningModelInvariantError,
        )
    )
    if len(typed) == 1:
        return _planning_invariant_diagnostic(payload, typed[0])
    if typed:
        return diagnostic_from_invariant(
            payload,
            failure_class=ResponseFailureClass.SEMANTIC_CONTEXT,
            authority=ResponseIssueAuthority.CONTROLLER,
            code="planning_context_unclassified",
            invariant_id="planning_multiple_invariants_unclassified",
            subjects=(),
            message="Planning validation produced multiple relational invariants",
            paths=("/",),
        )
    if any(
        tuple(issue["loc"]) == ("proposal",)
        and str(issue["type"]).startswith("value_error")
        for issue in issues
    ):
        return diagnostic_from_invariant(
            payload,
            failure_class=ResponseFailureClass.SEMANTIC_CONTEXT,
            authority=ResponseIssueAuthority.CONTROLLER,
            code="planning_context_unclassified",
            invariant_id="planning_relational_invariant_unclassified",
            subjects=(),
            message="Planning relational validation has no typed correction authority",
            paths=("/",),
        )
    diagnostic = diagnostic_from_validation_error(error, payload)
    if any(
        issue.path == "/question" or issue.path.startswith("/question/")
        for issue in diagnostic.issues
    ):
        correction_paths = tuple(
            sorted(
                {
                    (
                        "/question"
                        if path == "/question" or path.startswith("/question/")
                        else path
                    )
                    for path in diagnostic.correction_paths
                }
            )
        )
        return diagnostic.model_copy(update={"correction_paths": correction_paths})
    return diagnostic


class PlanningRequest(BaseModel):
    """Direct user input and explicit authorization before any model work."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[2, 3, 4, 5, 6, 7, 8, 9, 10, PLANNING_SCHEMA_VERSION] = (
        PLANNING_SCHEMA_VERSION
    )
    run_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    project_name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    source_request: str = Field(min_length=1, max_length=2000)
    destination: str = Field(min_length=1, max_length=4096)
    execution_profile: tuple[str, ...] = Field(min_length=1)
    base_constraints: tuple[str, ...] = ()
    model: str = Field(min_length=3)
    authorization: Literal["user_confirmed"]
    authorized_at: datetime

    @field_validator("source_request", "destination", "model")
    @classmethod
    def require_clean_text(cls, value: str) -> str:
        return _clean_text(value, label="Planning request text")

    @field_validator("execution_profile", "base_constraints")
    @classmethod
    def require_clean_unique_text(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _clean_unique(values, label="Planning request")

    @field_validator("model")
    @classmethod
    def require_model_reference(cls, value: str) -> str:
        cleaned = value.strip()
        provider, separator, model = cleaned.partition("/")
        if (
            not separator
            or not provider
            or not model
            or any(character.isspace() for character in cleaned)
        ):
            raise ValueError("Planning requires a canonical provider/model reference")
        return cleaned

    @field_validator("authorized_at")
    @classmethod
    def require_authorized_timestamp(cls, value: datetime) -> datetime:
        return _utc(value)


class PlanningOption(BaseModel):
    """One suggested answer while preserving a custom-answer path."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    label: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=300)

    @field_validator("label", "description")
    @classmethod
    def require_clean_text(cls, value: str) -> str:
        return _clean_text(value, label="Planning option text")


class PlanningQuestion(BaseModel):
    """One high-value clarification selected by the bootstrap Planner."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    text: str = Field(min_length=1, max_length=500)
    why: str = Field(min_length=1, max_length=500)
    decision_category: PlanningDecisionCategory | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    decision_owner: PlanningDecisionAuthority | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    missing_evidence: tuple[str, ...] = Field(
        default=(),
        min_length=1,
        exclude_if=lambda values: not values,
    )
    material_consequences: tuple[str, ...] = Field(
        default=(),
        min_length=1,
        exclude_if=lambda values: not values,
    )
    product_definition_dimensions: tuple[ProductDefinitionDimension, ...] = Field(
        default=(),
        exclude_if=lambda values: not values,
        description=(
            "The single product-depth dimension this answer resolves; empty for "
            "a material product decision outside the six adequacy dimensions."
        ),
    )
    options: tuple[PlanningOption, ...] = Field(min_length=2, max_length=3)
    allow_custom: Literal[True] = True

    @field_validator("text", "why")
    @classmethod
    def require_clean_text(cls, value: str) -> str:
        return _clean_text(value, label="Planning question text")

    @field_validator("missing_evidence", "material_consequences")
    @classmethod
    def require_clean_context(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _clean_unique(values, label="Planning question context")

    @model_validator(mode="after")
    def require_unique_options(self) -> Self:
        option_ids = [option.id for option in self.options]
        if len(option_ids) != len(set(option_ids)):
            raise ValueError("Planning question option IDs must be unique")
        if len(self.product_definition_dimensions) != len(
            set(self.product_definition_dimensions)
        ):
            raise ValueError(
                "Planning question product-definition dimensions must be unique"
            )
        return self


@dataclass(frozen=True)
class _PlanningQuestionContract:
    """Controller-recovered authority and adequacy scope for one question."""

    category: PlanningDecisionCategory
    owner: PlanningDecisionAuthority
    product_definition_dimensions: tuple[ProductDefinitionDimension, ...] = ()
    answer: str | None = None
    approved_dimension_values: tuple[tuple[ProductDefinitionDimension, str], ...] = ()


class PlanningDecisionRecord(BaseModel):
    """Attributable resolution or proposal for one material decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^DECISION_[A-Z0-9_]+$")
    category: PlanningDecisionCategory
    authority: PlanningDecisionAuthority
    provenance: PlanningDecisionProvenance | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description=(
            "Current live decisions use an explicit typed source. Absence is kept "
            "only so schema-v2 through schema-v7 evidence remains readable."
        ),
    )
    summary: str = Field(min_length=1, max_length=500)
    rationale: str = Field(min_length=1, max_length=500)
    question_id: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]*$",
        exclude_if=lambda value: value is None,
        description="Legacy schema-v2 through schema-v7 question provenance.",
    )

    @field_validator("summary", "rationale")
    @classmethod
    def require_clean_text(cls, value: str) -> str:
        return _clean_text(value, label="Planning decision text")

    @model_validator(mode="after")
    def require_category_authority(self) -> Self:
        expected = _DECISION_AUTHORITY[self.category]
        if self.authority is not expected:
            raise ValueError(
                f"decision category {self.category.value} belongs to {expected.value}"
            )
        if self.provenance is not None:
            expected_provenance_authority = {
                PlanningDecisionProvenanceKind.EXPLICIT_INPUT: (
                    PlanningDecisionAuthority.USER
                ),
                PlanningDecisionProvenanceKind.RESOLVED_QUESTION: (
                    PlanningDecisionAuthority.USER
                ),
                PlanningDecisionProvenanceKind.PLANNER_RECOMMENDATION: (
                    PlanningDecisionAuthority.PLANNER_PROPOSAL
                ),
                PlanningDecisionProvenanceKind.AGENT_AUTONOMY: (
                    PlanningDecisionAuthority.AGENT_AUTONOMY
                ),
            }[self.provenance.kind]
            if self.authority is not expected_provenance_authority:
                raise ValueError(
                    f"{self.provenance.kind.value} provenance belongs to "
                    f"{expected_provenance_authority.value}"
                )
            if self.question_id is not None and (
                self.provenance.kind
                is not PlanningDecisionProvenanceKind.RESOLVED_QUESTION
                or self.provenance.source != self.question_id
            ):
                raise ValueError(
                    "legacy question_id must match resolved-question provenance"
                )
        if (
            self.authority
            in {
                PlanningDecisionAuthority.AGENT_AUTONOMY,
                PlanningDecisionAuthority.CONTROLLER_POLICY,
            }
            and self.question_id is not None
        ):
            raise ValueError(
                "autonomous or controller-policy decisions cannot claim a user question"
            )
        return self

    @property
    def resolved_question_id(self) -> str | None:
        """Return current or legacy question provenance through one authority."""

        if (
            self.provenance is not None
            and self.provenance.kind is PlanningDecisionProvenanceKind.RESOLVED_QUESTION
        ):
            return self.provenance.source
        return self.question_id


_ALL_REVIEW_BOUNDARIES = tuple(ReviewBoundaryKind)
_ABSOLUTE_GUARANTEE_PATTERN = re.compile(
    r"(?:\bnever\b|\b(?:must|shall|may|can|does?|is|are)\s+not\b|"
    r"\bcannot\b|"
    r"\bunder\s+no\s+circumstances\b|\bat\s+any\s+(?:depth|level|time)\b|"
    r"不得|禁止|永不|绝不|任何(?:层级|深度|情况下)?.{0,8}(?:不|无))",
    flags=re.IGNORECASE,
)


class ProposedRequirement(BaseModel):
    """One model-owned requirement whose identity cannot drift from its meaning."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(
        pattern=r"^REQ_[A-Z0-9_]+$",
        description=(
            "Stable requirement identity. Use one unique uppercase REQ_ identifier."
        ),
    )
    description: str = Field(
        min_length=1,
        max_length=500,
        description=(
            "Requirement meaning only. Do not repeat the stable ID as a prefix."
        ),
    )

    @field_validator("description")
    @classmethod
    def require_clean_description(cls, value: str) -> str:
        return _clean_text(value, label="requirement description")


class ProposedAssumption(BaseModel):
    """One model-owned assumption bound atomically to its autonomy decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    statement: str = Field(
        min_length=1,
        max_length=500,
        description="The implementation or scheduling assumption being made.",
    )
    decision_id: str = Field(
        pattern=r"^DECISION_[A-Z0-9_]+$",
        description=(
            "Stable identity of the Agent-autonomy decision that authorizes this "
            "assumption."
        ),
    )

    @field_validator("statement")
    @classmethod
    def require_clean_statement(cls, value: str) -> str:
        return _clean_text(value, label="assumption statement")


class ProposedCriterion(BaseModel):
    """User-facing acceptance condition proposed during Planning."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[A-Z][A-Z0-9_-]*$")
    description: str = Field(min_length=1, max_length=500)
    verification: str = Field(min_length=1, max_length=500)
    requirement_ids: tuple[str, ...] = Field(
        default=(),
        min_length=1,
        exclude_if=lambda values: not values,
    )
    verification_agent_ids: tuple[str, ...] = Field(
        default=(),
        min_length=1,
        exclude_if=lambda values: not values,
    )
    review_boundaries: tuple[ReviewBoundaryKind, ...] = ()

    @field_validator("description", "verification")
    @classmethod
    def require_clean_text(cls, value: str) -> str:
        return _clean_text(value, label="acceptance criterion text")

    @field_validator("review_boundaries")
    @classmethod
    def require_unique_review_boundaries(
        cls,
        values: tuple[ReviewBoundaryKind, ...],
    ) -> tuple[ReviewBoundaryKind, ...]:
        if len(values) != len(set(values)):
            raise ValueError("proposed Review boundaries must be unique")
        return values

    @field_validator("requirement_ids")
    @classmethod
    def require_requirement_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = _clean_unique(values, label="criterion requirement")
        if any(re.fullmatch(r"REQ_[A-Z0-9_]+", value) is None for value in cleaned):
            raise ValueError("criterion requirements must use stable REQ_ IDs")
        return cleaned

    @field_validator("verification_agent_ids")
    @classmethod
    def require_verification_agents(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        cleaned = _clean_unique(values, label="criterion verifier")
        if any(re.fullmatch(r"[a-z][a-z0-9_]*", value) is None for value in cleaned):
            raise ValueError("criterion verifiers must use stable Agent IDs")
        return cleaned


class ProposedAgent(BaseModel):
    """Planner recommendation before the controller assigns runtime authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    label: str = Field(min_length=1, max_length=80)
    responsibility: str = Field(min_length=1, max_length=500)
    rationale: str = Field(min_length=1, max_length=500)
    capability: AgentCapability
    stage_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    dependencies: tuple[str, ...] = ()
    workspace_scope: str = Field(
        min_length=1,
        max_length=200,
        description=(
            "Use repository for whole-project access or repository/path for a "
            "narrower canonical scope; never repeat the destination directory."
        ),
    )
    workload: AgentWorkload

    @field_validator("label", "responsibility", "rationale")
    @classmethod
    def require_clean_text(cls, value: str) -> str:
        return _clean_text(value, label="proposed Agent text")

    @field_validator("dependencies")
    @classmethod
    def require_unique_dependencies(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _clean_unique(values, label="Agent dependency")

    @field_validator("workspace_scope")
    @classmethod
    def require_safe_scope(cls, value: str) -> str:
        cleaned = _safe_path(value)
        if PurePosixPath(cleaned).parts[0] != "repository":
            raise ValueError("workspace scopes must start at repository or repository/")
        return cleaned

    @model_validator(mode="after")
    def reject_bootstrap_capability(self) -> Self:
        if self.capability in {
            AgentCapability.CLARIFICATION,
            AgentCapability.PLANNING,
        }:
            raise ValueError(
                "bootstrap Planning and Clarification capabilities are outside "
                "the runtime team"
            )
        if self.id in self.dependencies:
            raise ValueError("a proposed Agent cannot depend on itself")
        return self


class ProposedTask(BaseModel):
    """Run-scoped work intent assigned to one proposed runtime Agent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^TASK_[A-Z0-9_]+$")
    owner_agent_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    description: str = Field(min_length=1, max_length=500)
    dependencies: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = Field(min_length=1)
    expected_paths: tuple[str, ...] = Field(
        default=(),
        description=(
            "Canonical paths relative to the repository root; directory paths "
            "must not end with a slash."
        ),
    )

    @field_validator("description")
    @classmethod
    def require_clean_description(cls, value: str) -> str:
        return _clean_text(value, label="task description")

    @field_validator("dependencies")
    @classmethod
    def require_clean_dependencies(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _clean_unique(values, label="task reference")

    @field_validator("acceptance_criteria")
    @classmethod
    def require_criterion_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = _clean_unique(values, label="task acceptance criterion")
        if any(re.fullmatch(r"[A-Z][A-Z0-9_-]*", value) is None for value in cleaned):
            raise ValueError(
                "task acceptance criteria must use stable uppercase criterion IDs"
            )
        return cleaned

    @field_validator("expected_paths")
    @classmethod
    def require_safe_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = _clean_unique(values, label="expected path")
        return tuple(_safe_path(value) for value in cleaned)


def _validate_dag(
    nodes: tuple[str, ...],
    dependencies: Mapping[str, tuple[str, ...]],
    *,
    label: str,
    path_prefix: str,
    subject_kind: ResponseIssueSubjectKind,
) -> None:
    known = set(nodes)
    indexes = {node: index for index, node in enumerate(nodes)}
    for node, required in dependencies.items():
        unknown = set(required) - known
        if unknown:
            message = (
                f"{label} {node} references unknown dependencies: "
                f"{', '.join(sorted(unknown))}"
            )
            raise _planning_model_invariant(
                f"planning_{subject_kind.value}_dependency_reference",
                message,
                paths=(f"{path_prefix}/{indexes[node]}/dependencies",),
                subjects=_planning_subjects(
                    (subject_kind, node),
                    *((subject_kind, item) for item in sorted(unknown)),
                ),
            )
    visiting: list[str] = []
    active: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in active:
            cycle = tuple(visiting[visiting.index(node) :])
            raise _planning_model_invariant(
                f"planning_{subject_kind.value}_dependency_cycle",
                f"{label} dependencies must be acyclic",
                paths=tuple(
                    f"{path_prefix}/{indexes[item]}/dependencies" for item in cycle
                ),
                subjects=_planning_subjects(*((subject_kind, item) for item in cycle)),
            )
        if node in visited:
            return
        visiting.append(node)
        active.add(node)
        for dependency in dependencies[node]:
            visit(dependency)
        visiting.pop()
        active.remove(node)
        visited.add(node)

    for node in nodes:
        visit(node)


def validate_task_agent_bindings(
    tasks: tuple[ProposedTask, ...],
    agent_dependencies: Mapping[str, tuple[str, ...]],
    writer_agent_ids: Collection[str],
) -> None:
    """Validate task ownership and ordering against one approved Agent DAG."""

    task_ids = tuple(task.id for task in tasks)
    if len(task_ids) != len(set(task_ids)):
        duplicates = tuple(
            sorted({task_id for task_id in task_ids if task_ids.count(task_id) > 1})
        )
        raise _planning_model_invariant(
            "planning_task_id_unique",
            "proposed task IDs must be unique",
            paths=("/proposal/tasks",),
            subjects=_planning_subjects(
                *((ResponseIssueSubjectKind.TASK, item) for item in duplicates)
            ),
        )
    _validate_dag(
        task_ids,
        {task.id: task.dependencies for task in tasks},
        label="task",
        path_prefix="/proposal/tasks",
        subject_kind=ResponseIssueSubjectKind.TASK,
    )

    known_agent_ids = set(agent_dependencies)
    unknown_task_owners = {task.owner_agent_id for task in tasks} - known_agent_ids
    if unknown_task_owners:
        message = "tasks reference unknown Agent owners: " + ", ".join(
            sorted(unknown_task_owners)
        )
        raise _planning_model_invariant(
            "planning_task_owner_reference",
            message,
            paths=tuple(
                f"/proposal/tasks/{index}/owner_agent_id"
                for index, task in enumerate(tasks)
                if task.owner_agent_id in unknown_task_owners
            ),
            subjects=_planning_subjects(
                *(
                    (ResponseIssueSubjectKind.AGENT, item)
                    for item in unknown_task_owners
                ),
                *(
                    (ResponseIssueSubjectKind.TASK, task.id)
                    for task in tasks
                    if task.owner_agent_id in unknown_task_owners
                ),
            ),
        )

    writers = set(writer_agent_ids)
    task_owners = {task.owner_agent_id for task in tasks}
    unassigned_writers = writers - task_owners
    if unassigned_writers:
        message = "every implementation Agent must own at least one task: " + ", ".join(
            sorted(unassigned_writers)
        )
        raise _planning_model_invariant(
            "planning_writer_task_required",
            message,
            paths=("/proposal/tasks",),
            subjects=_planning_subjects(
                *((ResponseIssueSubjectKind.AGENT, item) for item in unassigned_writers)
            ),
        )

    def transitively_depends(agent_id: str, target: str) -> bool:
        pending = list(agent_dependencies[agent_id])
        seen: set[str] = set()
        while pending:
            current = pending.pop()
            if current == target:
                return True
            if current not in seen:
                seen.add(current)
                pending.extend(agent_dependencies[current])
        return False

    task_owner_by_id = {task.id: task.owner_agent_id for task in tasks}
    for task in tasks:
        for dependency_id in task.dependencies:
            dependency_owner = task_owner_by_id[dependency_id]
            if dependency_owner != task.owner_agent_id and not transitively_depends(
                task.owner_agent_id,
                dependency_owner,
            ):
                message = (
                    f"task {task.id} depends on {dependency_id}, but Agent "
                    f"{task.owner_agent_id} does not depend on {dependency_owner}"
                )
                raise _planning_model_invariant(
                    "planning_task_owner_dependency",
                    message,
                    paths=(f"/proposal/tasks/{task_ids.index(task.id)}/dependencies",),
                    subjects=_planning_subjects(
                        (ResponseIssueSubjectKind.AGENT, dependency_owner),
                        (ResponseIssueSubjectKind.AGENT, task.owner_agent_id),
                        (ResponseIssueSubjectKind.TASK, dependency_id),
                        (ResponseIssueSubjectKind.TASK, task.id),
                    ),
                )


def validate_task_criterion_references(
    tasks: tuple[ProposedTask, ...],
    known_criterion_ids: Collection[str],
) -> None:
    """Reject task bindings outside a controller-known criterion contract."""

    covered = {criterion for task in tasks for criterion in task.acceptance_criteria}
    unknown = covered - set(known_criterion_ids)
    if unknown:
        message = "tasks reference unknown acceptance criteria: " + ", ".join(
            sorted(unknown)
        )
        raise _planning_model_invariant(
            "planning_task_criterion_reference",
            message,
            paths=tuple(
                f"/proposal/tasks/{index}/acceptance_criteria"
                for index, task in enumerate(tasks)
                if set(task.acceptance_criteria) & unknown
            ),
            subjects=_planning_subjects(
                *((ResponseIssueSubjectKind.CRITERION, item) for item in unknown),
                *(
                    (ResponseIssueSubjectKind.TASK, task.id)
                    for task in tasks
                    if set(task.acceptance_criteria) & unknown
                ),
            ),
        )


class PlanningProposalBody(BaseModel):
    """Complete semantic proposal returned before user approval."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str = Field(min_length=1, max_length=120)
    product_definition: ProductDefinition | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    requirements: tuple[str, ...] = Field(min_length=1)
    requirement_ids: tuple[str, ...] = Field(
        default=(),
        min_length=1,
        exclude_if=lambda values: not values,
    )
    non_goals: tuple[str, ...] = Field(
        default=(),
        min_length=1,
        exclude_if=lambda values: not values,
    )
    acceptance_criteria: tuple[ProposedCriterion, ...] = Field(
        min_length=1,
    )
    constraints: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    assumption_decision_ids: tuple[str, ...] = Field(
        default=(),
        exclude_if=lambda values: not values,
    )
    decisions: tuple[PlanningDecisionRecord, ...] = Field(
        default=(),
        min_length=1,
        exclude_if=lambda values: not values,
    )
    objective: str = Field(min_length=1, max_length=1000)
    approach: tuple[str, ...] = Field(min_length=1)
    tasks: tuple[ProposedTask, ...] = Field(
        min_length=1,
        description=(
            "Work intent for proposed runtime Agents. Tasks describe approved "
            "focus but do not create Agents or grant permissions."
        ),
    )
    risks: tuple[str, ...] = ()
    agents: tuple[ProposedAgent, ...] = Field(min_length=2)
    iteration_limit: int = Field(ge=1)
    max_concurrency: int = Field(ge=1)
    revision_enabled: bool

    @field_validator("title", "objective")
    @classmethod
    def require_clean_text(cls, value: str) -> str:
        return _clean_text(value, label="proposal text")

    @field_validator(
        "requirements",
        "non_goals",
        "constraints",
        "assumptions",
        "approach",
        "risks",
    )
    @classmethod
    def require_clean_unique_text(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _clean_unique(values, label="proposal")

    @field_validator("requirement_ids")
    @classmethod
    def require_stable_requirement_ids(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        cleaned = _clean_unique(values, label="proposal requirement ID")
        if any(re.fullmatch(r"REQ_[A-Z0-9_]+", value) is None for value in cleaned):
            raise ValueError("proposal requirements must use stable REQ_ IDs")
        return cleaned

    @field_validator("assumption_decision_ids")
    @classmethod
    def require_assumption_decision_ids(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        if any(re.fullmatch(r"DECISION_[A-Z0-9_]+", value) is None for value in values):
            raise ValueError("assumptions must reference stable DECISION_ IDs")
        return values

    @model_validator(mode="after")
    def validate_complete_proposal(self) -> Self:
        criterion_ids = tuple(item.id for item in self.acceptance_criteria)
        if len(criterion_ids) != len(set(criterion_ids)):
            duplicates = tuple(
                sorted(
                    {
                        criterion_id
                        for criterion_id in criterion_ids
                        if criterion_ids.count(criterion_id) > 1
                    }
                )
            )
            raise _planning_model_invariant(
                "planning_criterion_id_unique",
                "proposal acceptance criterion IDs must be unique",
                paths=("/proposal/acceptance_criteria",),
                subjects=_planning_subjects(
                    *((ResponseIssueSubjectKind.CRITERION, item) for item in duplicates)
                ),
            )
        agent_ids = tuple(agent.id for agent in self.agents)
        if len(agent_ids) != len(set(agent_ids)):
            duplicates = tuple(
                sorted(
                    {
                        agent_id
                        for agent_id in agent_ids
                        if agent_ids.count(agent_id) > 1
                    }
                )
            )
            raise _planning_model_invariant(
                "planning_agent_id_unique",
                "proposed Agent IDs must be unique",
                paths=("/proposal/agents",),
                subjects=_planning_subjects(
                    *((ResponseIssueSubjectKind.AGENT, item) for item in duplicates)
                ),
            )
        decision_ids = tuple(item.id for item in self.decisions)
        if len(decision_ids) != len(set(decision_ids)):
            duplicates = tuple(
                sorted(
                    {
                        decision_id
                        for decision_id in decision_ids
                        if decision_ids.count(decision_id) > 1
                    }
                )
            )
            raise _planning_model_invariant(
                "planning_decision_id_unique",
                "proposal decision IDs must be unique",
                paths=("/proposal/decisions",),
                subjects=_planning_subjects(
                    *((ResponseIssueSubjectKind.DECISION, item) for item in duplicates)
                ),
            )
        _validate_dag(
            agent_ids,
            {agent.id: agent.dependencies for agent in self.agents},
            label="Agent",
            path_prefix="/proposal/agents",
            subject_kind=ResponseIssueSubjectKind.AGENT,
        )
        implementation_agents = {
            agent.id
            for agent in self.agents
            if agent.capability
            in {AgentCapability.IMPLEMENTATION, AgentCapability.INTEGRATION}
        }
        if not implementation_agents:
            raise _planning_model_invariant(
                "planning_implementation_agent_required",
                "proposal requires an implementation Agent",
                paths=("/proposal/agents",),
                subjects=_planning_subjects(
                    (ResponseIssueSubjectKind.CAPABILITY, "implementation")
                ),
            )
        quality_agents = {
            agent.id
            for agent in self.agents
            if agent.capability in {AgentCapability.TESTING, AgentCapability.REVIEW}
        }
        if not quality_agents:
            raise _planning_model_invariant(
                "planning_quality_agent_required",
                "proposal requires an independent quality Agent",
                paths=("/proposal/agents",),
                subjects=_planning_subjects(
                    (ResponseIssueSubjectKind.CAPABILITY, "read_only_quality")
                ),
            )

        dependencies = {agent.id: agent.dependencies for agent in self.agents}

        def transitively_depends(agent_id: str, target: str) -> bool:
            pending = list(dependencies[agent_id])
            seen: set[str] = set()
            while pending:
                current = pending.pop()
                if current == target:
                    return True
                if current not in seen:
                    seen.add(current)
                    pending.extend(dependencies[current])
            return False

        for quality_agent in sorted(quality_agents):
            missing_dependencies = tuple(
                sorted(
                    implementation_agent
                    for implementation_agent in implementation_agents
                    if not transitively_depends(quality_agent, implementation_agent)
                )
            )
            if missing_dependencies:
                raise _planning_model_invariant(
                    "planning_quality_dependency_coverage",
                    "every quality Agent must depend on every implementation path",
                    paths=(
                        f"/proposal/agents/{agent_ids.index(quality_agent)}/dependencies",
                    ),
                    subjects=_planning_subjects(
                        (ResponseIssueSubjectKind.AGENT, quality_agent),
                        *(
                            (ResponseIssueSubjectKind.AGENT, item)
                            for item in missing_dependencies
                        ),
                    ),
                )

        validate_task_agent_bindings(
            self.tasks,
            dependencies,
            implementation_agents,
        )
        covered = {
            criterion
            for task in self.tasks
            if task.owner_agent_id in implementation_agents
            for criterion in task.acceptance_criteria
        }
        expected = set(criterion_ids)
        missing = expected - covered
        if missing:
            message = (
                "writer tasks do not cover proposal acceptance criteria: "
                + ", ".join(sorted(missing))
            )
            raise _planning_model_invariant(
                "planning_writer_criterion_coverage",
                message,
                paths=("/proposal/tasks",),
                subjects=_planning_subjects(
                    *((ResponseIssueSubjectKind.CRITERION, item) for item in missing)
                ),
            )
        if self.max_concurrency > len(self.agents):
            raise _planning_model_invariant(
                "planning_concurrency_agent_bound",
                "proposal concurrency cannot exceed its Agent count",
                paths=("/proposal/max_concurrency",),
            )
        if self.revision_enabled != (self.iteration_limit > 1):
            raise _planning_model_invariant(
                "planning_revision_iteration_consistency",
                "revision_enabled must equal whether iteration_limit exceeds one",
                paths=(
                    "/proposal/iteration_limit",
                    "/proposal/revision_enabled",
                ),
            )
        return self


def validate_question_admission(
    question: PlanningQuestion,
    *,
    previous_question_ids: Collection[str] = (),
) -> None:
    """Reject questions outside the deterministic responsibility matrix."""

    if question.id in set(previous_question_ids):
        raise _planning_context_invariant(
            "planning_question_id_reused",
            f"Planning question ID was already used: {question.id}",
            paths=("/question",),
            subjects=_planning_subjects(
                (ResponseIssueSubjectKind.QUESTION, question.id)
            ),
        )
    if question.decision_category is None or question.decision_owner is None:
        raise _planning_context_invariant(
            "planning_question_contract_required",
            "Planning question is missing decision category or owner",
            paths=("/question",),
            subjects=_planning_subjects(
                (ResponseIssueSubjectKind.QUESTION, question.id)
            ),
        )
    expected = _DECISION_AUTHORITY[question.decision_category]
    if question.decision_owner is not expected:
        message = (
            f"Planning question category {question.decision_category.value} belongs "
            f"to {expected.value}, not {question.decision_owner.value}"
        )
        raise _planning_context_invariant(
            "planning_question_authority",
            message,
            paths=("/question",),
            subjects=_planning_subjects(
                (ResponseIssueSubjectKind.QUESTION, question.id)
            ),
        )
    if expected in {
        PlanningDecisionAuthority.AGENT_AUTONOMY,
        PlanningDecisionAuthority.CONTROLLER_POLICY,
    }:
        raise _planning_context_invariant(
            "planning_question_user_authority",
            (
                "Planning cannot ask the user to decide "
                f"{question.decision_category.value}"
            ),
            paths=("/question",),
            subjects=_planning_subjects(
                (ResponseIssueSubjectKind.QUESTION, question.id)
            ),
        )
    if (
        question.product_definition_dimensions
        and question.decision_category
        is not PlanningDecisionCategory.PRODUCT_REQUIREMENT
    ):
        raise _planning_context_invariant(
            "planning_product_question_authority",
            "product-definition clarification belongs to user product requirements",
            paths=("/question",),
            subjects=_planning_subjects(
                (ResponseIssueSubjectKind.QUESTION, question.id)
            ),
        )
    if len(question.product_definition_dimensions) > 1:
        raise _planning_context_invariant(
            "planning_product_question_atomic_dimension",
            (
                "one free-text Planning answer cannot authorize multiple "
                "product-definition dimensions"
            ),
            paths=("/question",),
            subjects=_planning_subjects(
                (ResponseIssueSubjectKind.QUESTION, question.id)
            ),
        )
    if not question.missing_evidence:
        raise _planning_context_invariant(
            "planning_question_missing_evidence",
            "Planning question must name the missing evidence",
            paths=("/question",),
            subjects=_planning_subjects(
                (ResponseIssueSubjectKind.QUESTION, question.id)
            ),
        )
    if not question.material_consequences:
        raise _planning_context_invariant(
            "planning_question_material_consequence",
            "Planning question must name a material consequence",
            paths=("/question",),
            subjects=_planning_subjects(
                (ResponseIssueSubjectKind.QUESTION, question.id)
            ),
        )


def _normalized_evidence_text(value: str) -> str:
    """Normalize user-visible text only enough for attributable quote matching."""

    return " ".join(value.casefold().split())


def _validate_decision_provenance(
    body: PlanningProposalBody,
    *,
    normalized_inputs: tuple[str, ...],
    require_current_provenance: bool,
) -> None:
    """Separate model-authored quote defects from genuinely missing user input."""

    quote_invariants: list[_PlanningInvariant] = []
    for decision_index, decision in enumerate(body.decisions):
        provenance = decision.provenance
        if provenance is None:
            if not require_current_provenance:
                if (
                    decision.authority is PlanningDecisionAuthority.USER
                    and decision.question_id is None
                ):
                    raise _planning_context_invariant(
                        "planning_legacy_user_decision_source",
                        (
                            f"decision {decision.id} has neither direct-input nor "
                            "question provenance"
                        ),
                        paths=(f"/proposal/decisions/{decision_index}",),
                        subjects=_planning_subjects(
                            (ResponseIssueSubjectKind.DECISION, decision.id)
                        ),
                        failure_class=ResponseFailureClass.MISSING_USER_DECISION,
                        authority=ResponseIssueAuthority.USER,
                    )
                continue
            authority = (
                ResponseIssueAuthority.USER
                if decision.authority is PlanningDecisionAuthority.USER
                else ResponseIssueAuthority.MODEL
            )
            failure_class = (
                ResponseFailureClass.MISSING_USER_DECISION
                if authority is ResponseIssueAuthority.USER
                else ResponseFailureClass.SEMANTIC_CONTEXT
            )
            raise _planning_context_invariant(
                "planning_decision_provenance_required",
                f"current decision {decision.id} requires typed provenance",
                paths=(f"/proposal/decisions/{decision_index}/provenance",),
                subjects=_planning_subjects(
                    (ResponseIssueSubjectKind.DECISION, decision.id)
                ),
                failure_class=failure_class,
                authority=authority,
            )
        if provenance.kind is PlanningDecisionProvenanceKind.EXPLICIT_INPUT and not any(
            _normalized_evidence_text(provenance.source) in value
            for value in normalized_inputs
        ):
            invariant = _PlanningInvariant(
                invariant_id="planning_decision_explicit_source",
                message=(
                    f"decision {decision.id} claims explicit user input that is "
                    "not present in the Planning request or a user revision; "
                    "source must quote one contiguous verbatim substring of "
                    "the existing user input, not a label or invented permission"
                ),
                paths=(f"/proposal/decisions/{decision_index}/provenance/source",),
                subjects=_planning_subjects(
                    (ResponseIssueSubjectKind.DECISION, decision.id)
                ),
            )
            if not normalized_inputs:
                raise _PlanningContextInvariantError(
                    invariant,
                    failure_class=ResponseFailureClass.MISSING_USER_DECISION,
                    authority=ResponseIssueAuthority.USER,
                )
            quote_invariants.append(invariant)
            continue
        if (
            provenance.kind is PlanningDecisionProvenanceKind.EXPLICIT_INPUT
            and _normalized_evidence_text(decision.summary)
            != _normalized_evidence_text(provenance.source)
        ):
            raise _planning_context_invariant(
                "planning_decision_explicit_summary",
                (
                    f"decision {decision.id} must preserve its exact direct-input "
                    "source as the user-owned summary"
                ),
                paths=(f"/proposal/decisions/{decision_index}/summary",),
                subjects=_planning_subjects(
                    (ResponseIssueSubjectKind.DECISION, decision.id)
                ),
            )

    if quote_invariants:
        raise _PlanningContextInvariantsError(tuple(quote_invariants))


def _product_definition_dimension_invariant(
    *,
    dimension: ProductDefinitionDimension,
    item: ProductDefinitionBasis,
    definition: ProductDefinition,
    requirement_ids: set[str],
    criterion_ids: set[str],
    decision_ids: set[str],
    decisions: Mapping[str, PlanningDecisionRecord],
    normalized_inputs: tuple[str, ...],
    question_contracts: Mapping[str, _PlanningQuestionContract] | None,
    allow_legacy_decision_links: bool,
    allow_legacy_workflow_exemption: bool,
) -> _PlanningInvariant | None:
    """Return one atomic, coherently repairable issue for a product dimension."""

    path = f"/proposal/product_definition/{dimension.value}"

    def issue(
        invariant_id: str,
        message: str,
        *,
        subjects: tuple[ResponseIssueSubject, ...] = (),
    ) -> _PlanningInvariant:
        return _PlanningInvariant(
            invariant_id=invariant_id,
            message=message,
            paths=(path,),
            subjects=subjects,
        )

    if (
        dimension is ProductDefinitionDimension.PRIMARY_WORKFLOW
        and item.disposition is ProductDefinitionDisposition.NOT_MATERIAL
        and not allow_legacy_workflow_exemption
    ):
        return issue(
            "planning_product_workflow_required",
            "primary workflow is always material, including a throwaway prototype; "
            "preserve its explicit user input or ask a focused question, and link "
            "the workflow to the requirements it drives",
        )

    if (
        item.disposition is ProductDefinitionDisposition.NOT_MATERIAL
        and not allow_legacy_decision_links
        and (item.requirement_ids or item.criterion_ids or item.decision_ids)
    ):
        return issue(
            "planning_product_not_material_trace",
            (
                f"{dimension.value} is not material and cannot claim a "
                "downstream requirement, criterion, or decision effect"
            ),
        )

    unknown_requirements = set(item.requirement_ids) - requirement_ids
    unknown_criteria = set(item.criterion_ids) - criterion_ids
    unknown_decisions = set(item.decision_ids) - decision_ids
    if unknown_requirements or unknown_criteria or unknown_decisions:
        details = []
        if unknown_requirements:
            details.append("requirements " + ", ".join(sorted(unknown_requirements)))
        if unknown_criteria:
            details.append("criteria " + ", ".join(sorted(unknown_criteria)))
        if unknown_decisions:
            details.append("decisions " + ", ".join(sorted(unknown_decisions)))
        return issue(
            "planning_product_definition_reference",
            f"{dimension.value} references unknown downstream " + "; ".join(details),
        )

    if item.disposition is ProductDefinitionDisposition.EXPLICIT_INPUT:
        source_is_exact = bool(normalized_inputs) and any(
            _normalized_evidence_text(item.source) in value
            for value in normalized_inputs
        )
        statement = getattr(item, "statement", None)
        statement_preserves_source = statement is None or (
            _normalized_evidence_text(statement)
            == _normalized_evidence_text(item.source)
        )
        if not source_is_exact or not statement_preserves_source:
            return issue(
                "planning_product_explicit_provenance",
                (
                    f"{dimension.value} explicit_input requires source to be one "
                    "contiguous verbatim substring of a user input, with no label, "
                    "quote delimiters, stitched excerpts, ellipsis, or commentary; "
                    "statement must preserve that exact wording. If the dimension "
                    "is not explicit, choose the truthful disposition and its source."
                ),
            )
        if item.decision_ids and not allow_legacy_decision_links:
            return issue(
                "planning_product_explicit_decision_trace",
                (
                    f"{dimension.value} explicit_input is already attributable to "
                    "its exact user source and cannot cite a separate decision record"
                ),
            )
        return None

    if item.disposition is ProductDefinitionDisposition.RESOLVED_QUESTION:
        linked = tuple(
            decision
            for decision in decisions.values()
            if decision.resolved_question_id == item.source
        )
        if (
            len(linked) != 1
            or linked[0].authority is not PlanningDecisionAuthority.USER
            or linked[0].category is not PlanningDecisionCategory.PRODUCT_REQUIREMENT
        ):
            return issue(
                "planning_product_question_source",
                f"{dimension.value} must reference one user-owned product question",
            )
        if (
            linked[0].id not in item.decision_ids
            if allow_legacy_decision_links
            else set(item.decision_ids) != {linked[0].id}
        ):
            return issue(
                "planning_product_question_decision_trace",
                (f"{dimension.value} must cite only its resolved question decision"),
            )
        if question_contracts is None:
            return None
        contract = question_contracts.get(item.source)
        subjects = _planning_subjects((ResponseIssueSubjectKind.QUESTION, item.source))
        if (
            contract is None
            or contract.category is not PlanningDecisionCategory.PRODUCT_REQUIREMENT
            or contract.owner is not PlanningDecisionAuthority.USER
            or dimension not in contract.product_definition_dimensions
        ):
            return issue(
                "planning_product_question_dimension",
                f"{dimension.value} was not resolved by its declared question",
                subjects=subjects,
            )
        resolved_value = (
            definition.delivery_maturity.level.value.replace("_", " ")
            if dimension is ProductDefinitionDimension.DELIVERY_MATURITY
            else getattr(item, "statement", "")
        )
        if contract.answer is not None:
            if _normalized_evidence_text(
                resolved_value
            ) not in _normalized_evidence_text(contract.answer):
                return issue(
                    "planning_product_question_answer",
                    f"{dimension.value} is not preserved in its user answer",
                    subjects=subjects,
                )
        else:
            approved_values = dict(contract.approved_dimension_values)
            previous_value = approved_values.get(dimension)
            if previous_value is None or _normalized_evidence_text(
                resolved_value
            ) != _normalized_evidence_text(previous_value):
                return issue(
                    "planning_product_question_revision",
                    f"{dimension.value} changed without a new user answer",
                    subjects=subjects,
                )
        return None

    if item.disposition is ProductDefinitionDisposition.PLANNER_RECOMMENDATION:
        if dimension in {
            ProductDefinitionDimension.TARGET_USERS,
            ProductDefinitionDimension.PRIMARY_WORKFLOW,
            ProductDefinitionDimension.DELIVERY_MATURITY,
        }:
            return issue(
                "planning_product_user_decision_required",
                f"{dimension.value} cannot be silently chosen by Planning",
            )
        if item.source != "planner" or not item.decision_ids:
            return issue(
                "planning_product_recommendation_source",
                f"{dimension.value} Planner recommendation needs decision provenance",
            )
        if any(
            decisions[decision_id].authority
            is not PlanningDecisionAuthority.PLANNER_PROPOSAL
            for decision_id in item.decision_ids
        ):
            return issue(
                "planning_product_recommendation_authority",
                f"{dimension.value} references a non-Planner decision",
            )
        expected_category = {
            ProductDefinitionDimension.USABILITY_EXPECTATIONS: (
                PlanningDecisionCategory.ACCEPTANCE_SCOPE
            ),
            ProductDefinitionDimension.OPERATIONAL_EXPECTATIONS: (
                PlanningDecisionCategory.ACCEPTANCE_SCOPE
            ),
            ProductDefinitionDimension.DELIVERY_EXPECTATIONS: (
                PlanningDecisionCategory.DELIVERY
            ),
        }[dimension]
        category_matches = tuple(
            decisions[decision_id].category is expected_category
            for decision_id in item.decision_ids
        )
        if not (
            any(category_matches)
            if allow_legacy_decision_links
            else all(category_matches)
        ):
            return issue(
                "planning_product_recommendation_category",
                (
                    f"{dimension.value} may cite only its corresponding "
                    "Planner decision category"
                ),
            )
        return None

    if dimension is ProductDefinitionDimension.DELIVERY_MATURITY:
        return issue(
            "planning_product_maturity_required",
            "delivery maturity is always material to the approved product",
        )
    if (
        dimension
        in {
            ProductDefinitionDimension.TARGET_USERS,
            ProductDefinitionDimension.PRIMARY_WORKFLOW,
        }
        and definition.delivery_maturity.level
        is not DeliveryMaturity.THROWAWAY_PROTOTYPE
    ):
        return issue(
            "planning_product_user_decision_required",
            f"{dimension.value} may be not_material only for a throwaway prototype",
        )
    if item.source != "planner":
        return issue(
            "planning_product_not_material_source",
            (
                f"{dimension.value} not_material disposition must be "
                "attributable to Planning"
            ),
        )
    return None


def _validate_product_definition(
    body: PlanningProposalBody,
    *,
    decisions: Mapping[str, PlanningDecisionRecord],
    user_inputs: Collection[str],
    question_contracts: Mapping[str, _PlanningQuestionContract] | None,
    allowed_criterion_ids: Collection[str] = (),
    allow_legacy_decision_links: bool = False,
    allow_legacy_workflow_exemption: bool = False,
) -> None:
    """Require attributable product depth with real downstream plan effects."""

    definition = body.product_definition
    if definition is None:
        raise _planning_context_invariant(
            "planning_product_definition_required",
            "current proposals require an approved product definition",
            paths=("/proposal/product_definition",),
        )

    requirement_ids = set(body.requirement_ids)
    criterion_ids = {
        *(criterion.id for criterion in body.acceptance_criteria),
        *allowed_criterion_ids,
    }
    decision_ids = set(decisions)
    normalized_inputs = tuple(_normalized_evidence_text(value) for value in user_inputs)
    dimension_invariants = tuple(
        invariant
        for dimension, item in definition.dimensions()
        if (
            invariant := _product_definition_dimension_invariant(
                dimension=dimension,
                item=item,
                definition=definition,
                requirement_ids=requirement_ids,
                criterion_ids=criterion_ids,
                decision_ids=decision_ids,
                decisions=decisions,
                normalized_inputs=normalized_inputs,
                question_contracts=question_contracts,
                allow_legacy_decision_links=allow_legacy_decision_links,
                allow_legacy_workflow_exemption=allow_legacy_workflow_exemption,
            )
        )
        is not None
    )
    if dimension_invariants:
        raise _PlanningContextInvariantsError(dimension_invariants)

    if (
        definition.target_users.disposition
        is not ProductDefinitionDisposition.NOT_MATERIAL
        and not definition.target_users.requirement_ids
    ):
        raise _planning_context_invariant(
            "planning_product_audience_effect",
            "target users must affect at least one requirement",
            paths=("/proposal/product_definition/target_users/requirement_ids",),
        )
    if (
        definition.primary_workflow.disposition
        is not ProductDefinitionDisposition.NOT_MATERIAL
        and not definition.primary_workflow.requirement_ids
    ):
        raise _planning_context_invariant(
            "planning_product_workflow_effect",
            "primary workflow must affect at least one requirement",
            paths=("/proposal/product_definition/primary_workflow/requirement_ids",),
        )
    for dimension, item in (
        (
            ProductDefinitionDimension.USABILITY_EXPECTATIONS,
            definition.usability_expectations,
        ),
        (
            ProductDefinitionDimension.OPERATIONAL_EXPECTATIONS,
            definition.operational_expectations,
        ),
    ):
        if (
            item.disposition is not ProductDefinitionDisposition.NOT_MATERIAL
            and not item.criterion_ids
        ):
            raise _planning_context_invariant(
                "planning_product_quality_effect",
                f"{dimension.value} must affect at least one acceptance criterion",
                paths=(
                    f"/proposal/product_definition/{dimension.value}/criterion_ids",
                ),
            )


def validate_planning_clarity(
    body: PlanningProposalBody,
    *,
    source_request: str | None = None,
    additional_user_inputs: Collection[str] = (),
    question_contracts: Mapping[str, _PlanningQuestionContract] | None = None,
    allowed_criterion_ids: Collection[str] = (),
    require_current_decision_provenance: bool = True,
    allow_legacy_product_decision_links: bool = False,
    allow_legacy_workflow_exemption: bool = False,
) -> None:
    """Enforce the current decision and requirement-to-evidence contract."""

    if len(body.requirement_ids) != len(body.requirements):
        raise _planning_context_invariant(
            "planning_requirement_id_cardinality",
            (
                "legacy proposal requirement fields do not contain one stable ID "
                "for every description; replace the complete requirements relation "
                "with atomic id/description objects"
            ),
            paths=("/proposal/requirements",),
        )
    if not body.non_goals:
        raise _planning_context_invariant(
            "planning_non_goal_required",
            "current proposals must state at least one non-goal",
            paths=("/proposal/non_goals",),
        )
    if not body.decisions:
        raise _planning_context_invariant(
            "planning_decision_provenance_required",
            "current proposals must record decision provenance",
            paths=("/proposal/decisions",),
        )

    decisions = {decision.id: decision for decision in body.decisions}
    if len(decisions) != len(body.decisions):
        decision_ids = tuple(decision.id for decision in body.decisions)
        duplicates = tuple(
            sorted(
                {
                    decision_id
                    for decision_id in decision_ids
                    if decision_ids.count(decision_id) > 1
                }
            )
        )
        raise _planning_context_invariant(
            "planning_decision_id_unique",
            "proposal decision IDs must be unique",
            paths=("/proposal/decisions",),
            subjects=_planning_subjects(
                *((ResponseIssueSubjectKind.DECISION, item) for item in duplicates)
            ),
        )
    if any(
        decision.authority is PlanningDecisionAuthority.CONTROLLER_POLICY
        for decision in body.decisions
    ):
        invalid_decisions = tuple(
            (index, decision)
            for index, decision in enumerate(body.decisions)
            if decision.authority is PlanningDecisionAuthority.CONTROLLER_POLICY
        )
        raise _planning_context_invariant(
            "planning_controller_authority_claim",
            "Planner output cannot claim controller-policy decision authority",
            paths=tuple(
                f"/proposal/decisions/{index}/category"
                for index, _decision in invalid_decisions
            ),
            subjects=_planning_subjects(
                *(
                    (ResponseIssueSubjectKind.DECISION, decision.id)
                    for _index, decision in invalid_decisions
                )
            ),
            authority=ResponseIssueAuthority.CONTROLLER,
        )

    normalized_inputs = tuple(
        _normalized_evidence_text(value)
        for value in (source_request, *additional_user_inputs)
        if value is not None
    )
    _validate_decision_provenance(
        body,
        normalized_inputs=normalized_inputs,
        require_current_provenance=require_current_decision_provenance,
    )

    _validate_product_definition(
        body,
        decisions=decisions,
        user_inputs=tuple(
            value
            for value in (source_request, *additional_user_inputs)
            if value is not None
        ),
        question_contracts=question_contracts,
        allowed_criterion_ids=allowed_criterion_ids,
        allow_legacy_decision_links=allow_legacy_product_decision_links,
        allow_legacy_workflow_exemption=allow_legacy_workflow_exemption,
    )

    required_recommendations = {
        PlanningDecisionCategory.ACCEPTANCE_SCOPE,
        PlanningDecisionCategory.DELIVERY,
        PlanningDecisionCategory.TEAM,
        PlanningDecisionCategory.MODEL_ROUTE,
    }
    recorded_recommendations = {
        decision.category
        for decision in body.decisions
        if decision.authority is PlanningDecisionAuthority.PLANNER_PROPOSAL
    }
    missing_recommendations = required_recommendations - recorded_recommendations
    if missing_recommendations:
        message = "proposal omits Planner recommendation provenance for: " + ", ".join(
            sorted(item.value for item in missing_recommendations)
        )
        raise _planning_context_invariant(
            "planning_recommendation_provenance",
            message,
            paths=("/proposal/decisions",),
            subjects=_planning_subjects(
                *(
                    (ResponseIssueSubjectKind.DECISION, item.value)
                    for item in missing_recommendations
                )
            ),
        )

    if len(body.assumption_decision_ids) != len(body.assumptions):
        raise _planning_context_invariant(
            "planning_assumption_decision_cardinality",
            "every assumption must identify its autonomous decision record",
            paths=("/proposal/assumptions",),
        )
    for decision_id in body.assumption_decision_ids:
        decision = decisions.get(decision_id)
        if decision is None:
            raise _planning_context_invariant(
                "planning_assumption_decision_reference",
                f"assumption references an unknown decision: {decision_id}",
                paths=("/proposal/assumptions",),
                subjects=_planning_subjects(
                    (ResponseIssueSubjectKind.DECISION, decision_id)
                ),
            )
        if decision.authority is not PlanningDecisionAuthority.AGENT_AUTONOMY:
            raise _planning_context_invariant(
                "planning_assumption_decision_authority",
                f"assumption {decision_id} is not an autonomous implementation choice",
                paths=("/proposal/assumptions",),
                subjects=_planning_subjects(
                    (ResponseIssueSubjectKind.DECISION, decision_id)
                ),
            )

    requirement_ids = set(body.requirement_ids)
    covered_requirements: set[str] = set()
    agents = {agent.id: agent for agent in body.agents}
    tasks_by_criterion: dict[str, list[ProposedTask]] = {
        criterion.id: [] for criterion in body.acceptance_criteria
    }
    for task in body.tasks:
        for criterion_id in task.acceptance_criteria:
            if criterion_id in tasks_by_criterion:
                tasks_by_criterion[criterion_id].append(task)

    dependencies = {agent.id: agent.dependencies for agent in body.agents}

    def transitively_depends(agent_id: str, target: str) -> bool:
        pending = list(dependencies[agent_id])
        seen: set[str] = set()
        while pending:
            current = pending.pop()
            if current == target:
                return True
            if current not in seen:
                seen.add(current)
                pending.extend(dependencies[current])
        return False

    agent_indexes = {agent.id: index for index, agent in enumerate(body.agents)}
    for criterion_index, criterion in enumerate(body.acceptance_criteria):
        unknown_requirements = set(criterion.requirement_ids) - requirement_ids
        if unknown_requirements:
            message = (
                f"criterion {criterion.id} references unknown requirements: "
                + ", ".join(sorted(unknown_requirements))
            )
            raise _planning_context_invariant(
                "planning_criterion_requirement_reference",
                message,
                paths=(
                    f"/proposal/acceptance_criteria/{criterion_index}/requirement_ids",
                ),
                subjects=_planning_subjects(
                    (ResponseIssueSubjectKind.CRITERION, criterion.id),
                    *(
                        (ResponseIssueSubjectKind.REQUIREMENT, item)
                        for item in unknown_requirements
                    ),
                ),
            )
        if not criterion.requirement_ids:
            raise _planning_context_invariant(
                "planning_criterion_requirement_required",
                f"criterion {criterion.id} must reference at least one requirement",
                paths=(
                    f"/proposal/acceptance_criteria/{criterion_index}/requirement_ids",
                ),
                subjects=_planning_subjects(
                    (ResponseIssueSubjectKind.CRITERION, criterion.id)
                ),
            )
        covered_requirements.update(criterion.requirement_ids)
        writers = {
            task.owner_agent_id
            for task in tasks_by_criterion[criterion.id]
            if agents[task.owner_agent_id].capability
            in {AgentCapability.IMPLEMENTATION, AgentCapability.INTEGRATION}
        }
        if not writers:
            raise _planning_context_invariant(
                "planning_criterion_writer_required",
                f"criterion {criterion.id} has no responsible writer task",
                paths=("/proposal/tasks",),
                subjects=_planning_subjects(
                    (ResponseIssueSubjectKind.CRITERION, criterion.id)
                ),
            )
        if not criterion.verification_agent_ids:
            raise _planning_context_invariant(
                "planning_criterion_verifier_required",
                f"criterion {criterion.id} must name an independent verifier",
                paths=(
                    f"/proposal/acceptance_criteria/{criterion_index}/verification_agent_ids",
                ),
                subjects=_planning_subjects(
                    (ResponseIssueSubjectKind.CRITERION, criterion.id)
                ),
            )
        for verifier_id in criterion.verification_agent_ids:
            verifier = agents.get(verifier_id)
            if verifier is None:
                message = (
                    f"criterion {criterion.id} references unknown verifier "
                    f"{verifier_id}"
                )
                raise _planning_context_invariant(
                    "planning_criterion_verifier_reference",
                    message,
                    paths=(
                        f"/proposal/acceptance_criteria/{criterion_index}/verification_agent_ids",
                    ),
                    subjects=_planning_subjects(
                        (ResponseIssueSubjectKind.AGENT, verifier_id),
                        (ResponseIssueSubjectKind.CRITERION, criterion.id),
                    ),
                )
            if verifier.capability not in {
                AgentCapability.TESTING,
                AgentCapability.REVIEW,
            }:
                message = (
                    f"criterion {criterion.id} verifier {verifier_id} is not "
                    "read-only quality"
                )
                raise _planning_context_invariant(
                    "planning_criterion_verifier_capability",
                    message,
                    paths=(
                        f"/proposal/acceptance_criteria/{criterion_index}/verification_agent_ids",
                    ),
                    subjects=_planning_subjects(
                        (ResponseIssueSubjectKind.AGENT, verifier_id),
                        (ResponseIssueSubjectKind.CRITERION, criterion.id),
                    ),
                )
            missing_writers = tuple(
                sorted(
                    writer
                    for writer in writers
                    if not transitively_depends(verifier_id, writer)
                )
            )
            if missing_writers:
                message = (
                    f"criterion {criterion.id} verifier {verifier_id} is not "
                    "downstream of every responsible writer"
                )
                raise _planning_context_invariant(
                    "planning_criterion_verifier_dependency",
                    message,
                    paths=(
                        f"/proposal/agents/{agent_indexes[verifier_id]}/dependencies",
                    ),
                    subjects=_planning_subjects(
                        (ResponseIssueSubjectKind.AGENT, verifier_id),
                        (ResponseIssueSubjectKind.CRITERION, criterion.id),
                        *(
                            (ResponseIssueSubjectKind.AGENT, item)
                            for item in missing_writers
                        ),
                    ),
                )

    missing_requirement_coverage = requirement_ids - covered_requirements
    if missing_requirement_coverage:
        message = "requirements lack observable acceptance coverage: " + ", ".join(
            sorted(missing_requirement_coverage)
        )
        raise _planning_context_invariant(
            "planning_requirement_acceptance_coverage",
            message,
            paths=("/proposal/acceptance_criteria",),
            subjects=_planning_subjects(
                *(
                    (ResponseIssueSubjectKind.REQUIREMENT, item)
                    for item in missing_requirement_coverage
                )
            ),
        )

    if question_contracts is None:
        return
    linked = {
        decision.resolved_question_id: decision
        for decision in body.decisions
        if decision.resolved_question_id is not None
    }
    if len(linked) != sum(
        decision.resolved_question_id is not None for decision in body.decisions
    ):
        duplicate_questions = tuple(
            sorted(
                {
                    decision.resolved_question_id
                    for decision in body.decisions
                    if decision.resolved_question_id is not None
                    and sum(
                        item.resolved_question_id == decision.resolved_question_id
                        for item in body.decisions
                    )
                    > 1
                }
            )
        )
        raise _planning_context_invariant(
            "planning_question_decision_unique",
            "a Planning question can resolve only one decision record",
            paths=("/proposal/decisions",),
            subjects=_planning_subjects(
                *(
                    (ResponseIssueSubjectKind.QUESTION, item)
                    for item in duplicate_questions
                )
            ),
        )
    if set(linked) != set(question_contracts):
        missing = set(question_contracts) - set(linked)
        invented = set(linked) - set(question_contracts)
        details = []
        if missing:
            details.append("missing " + ", ".join(sorted(missing)))
        if invented:
            details.append("unknown " + ", ".join(sorted(invented)))
        message = "proposal question-decision provenance is incomplete: " + "; ".join(
            details
        )
        raise _planning_context_invariant(
            "planning_question_decision_completeness",
            message,
            paths=("/proposal/decisions",),
            subjects=_planning_subjects(
                *((ResponseIssueSubjectKind.QUESTION, item) for item in missing),
                *((ResponseIssueSubjectKind.QUESTION, item) for item in invented),
            ),
        )
    for question_id, contract in question_contracts.items():
        decision = linked[question_id]
        if (
            decision.category is not contract.category
            or decision.authority is not contract.owner
        ):
            decision_index = tuple(item.id for item in body.decisions).index(
                decision.id
            )
            raise _planning_context_invariant(
                "planning_question_decision_contract",
                f"decision for question {question_id} changed its category",
                paths=(f"/proposal/decisions/{decision_index}/category",),
                subjects=_planning_subjects(
                    (ResponseIssueSubjectKind.DECISION, decision.id),
                    (ResponseIssueSubjectKind.QUESTION, question_id),
                ),
            )


class PlanningModelResponse(BaseModel):
    """Strict either-question-or-proposal response from bootstrap Planning."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: PlanningResponseKind
    question: PlanningQuestion | None = None
    proposal: PlanningProposalBody | None = None

    @model_validator(mode="after")
    def require_exact_payload(self) -> Self:
        if self.kind is PlanningResponseKind.QUESTION:
            if self.question is None or self.proposal is not None:
                raise ValueError("question responses require only question")
        elif self.proposal is None or self.question is not None:
            raise ValueError("proposal responses require only proposal")
        return self


def _planning_response_schema() -> dict[str, object]:
    """Require every field in the current live Planning contract."""

    schema = PlanningModelResponse.model_json_schema()
    definitions = schema.get("$defs")
    if not isinstance(definitions, dict):
        raise PlanningError("Planning response schema has no definitions")
    required_by_definition = {
        "PlanningQuestion": (
            "decision_category",
            "missing_evidence",
            "material_consequences",
            "product_definition_dimensions",
        ),
        "ProposedCriterion": (
            "requirement_ids",
            "verification_agent_ids",
            "review_boundaries",
        ),
        "PlanningDecisionRecord": ("provenance",),
        "PlanningProposalBody": (
            "product_definition",
            "non_goals",
            "decisions",
        ),
    }
    for definition_name, field_names in required_by_definition.items():
        definition = definitions.get(definition_name)
        if not isinstance(definition, dict):
            raise PlanningError(
                f"Planning response schema has no {definition_name} definition"
            )
        properties = definition.get("properties")
        required = definition.setdefault("required", [])
        if not isinstance(properties, dict) or not isinstance(required, list):
            raise PlanningError(
                f"Planning response schema has invalid {definition_name} fields"
            )
        for field_name in field_names:
            field_schema = properties.get(field_name)
            if not isinstance(field_schema, dict):
                raise PlanningError(
                    f"Planning response schema has no {definition_name}.{field_name}"
                )
            field_schema.pop("default", None)
            if field_name not in required:
                required.append(field_name)
    question_properties = definitions["PlanningQuestion"]["properties"]
    dimension_schema = question_properties["product_definition_dimensions"]
    dimension_schema["maxItems"] = 1
    for field_name in ("decision_category",):
        field_schema = question_properties[field_name]
        options = field_schema.get("anyOf")
        if not isinstance(options, list):
            raise PlanningError(
                f"Planning response schema has no nullable {field_name} union"
            )
        non_null = [option for option in options if option.get("type") != "null"]
        if len(non_null) != 1:
            raise PlanningError(
                f"Planning response schema has an invalid {field_name} union"
            )
        question_properties[field_name] = non_null[0]
    question_required = definitions["PlanningQuestion"]["required"]
    question_properties.pop("decision_owner", None)
    while "decision_owner" in question_required:
        question_required.remove("decision_owner")
    decision_definition = definitions["PlanningDecisionRecord"]
    decision_properties = decision_definition["properties"]
    provenance_schema = decision_properties["provenance"]
    provenance_options = provenance_schema.get("anyOf")
    if not isinstance(provenance_options, list):
        raise PlanningError(
            "Planning response schema has no nullable decision provenance union"
        )
    if (
        len([option for option in provenance_options if option.get("type") != "null"])
        != 1
    ):
        raise PlanningError("Planning response schema has invalid decision provenance")
    common_properties = {
        field: json.loads(json.dumps(decision_properties[field]))
        for field in ("id", "summary", "rationale")
    }

    def decision_branch(
        *,
        categories: tuple[PlanningDecisionCategory, ...],
        provenance_kind: PlanningDecisionProvenanceKind,
        source_schema: dict[str, object],
    ) -> dict[str, object]:
        return {
            "additionalProperties": False,
            "properties": {
                **json.loads(json.dumps(common_properties)),
                "category": {
                    "enum": [item.value for item in categories],
                    "type": "string",
                },
                "provenance": {
                    "additionalProperties": False,
                    "properties": {
                        "kind": {
                            "const": provenance_kind.value,
                            "type": "string",
                        },
                        "source": source_schema,
                    },
                    "required": ["kind", "source"],
                    "type": "object",
                },
            },
            "required": ["id", "category", "provenance", "summary", "rationale"],
            "type": "object",
        }

    user_categories = tuple(
        category
        for category, authority in _DECISION_AUTHORITY.items()
        if authority is PlanningDecisionAuthority.USER
    )
    planner_categories = tuple(
        category
        for category, authority in _DECISION_AUTHORITY.items()
        if authority is PlanningDecisionAuthority.PLANNER_PROPOSAL
    )
    agent_categories = tuple(
        category
        for category, authority in _DECISION_AUTHORITY.items()
        if authority is PlanningDecisionAuthority.AGENT_AUTONOMY
    )
    definitions["PlanningDecisionRecord"] = {
        "description": (
            "One decision whose category and typed provenance form an authorized "
            "atomic combination."
        ),
        "oneOf": [
            decision_branch(
                categories=user_categories,
                provenance_kind=PlanningDecisionProvenanceKind.EXPLICIT_INPUT,
                source_schema={"minLength": 1, "maxLength": 2000, "type": "string"},
            ),
            decision_branch(
                categories=user_categories,
                provenance_kind=PlanningDecisionProvenanceKind.RESOLVED_QUESTION,
                source_schema={
                    "maxLength": 2000,
                    "minLength": 1,
                    "pattern": "^[a-z][a-z0-9_]*$",
                    "type": "string",
                },
            ),
            decision_branch(
                categories=planner_categories,
                provenance_kind=PlanningDecisionProvenanceKind.PLANNER_RECOMMENDATION,
                source_schema={"const": "planner", "type": "string"},
            ),
            decision_branch(
                categories=agent_categories,
                provenance_kind=PlanningDecisionProvenanceKind.AGENT_AUTONOMY,
                source_schema={"const": "agent", "type": "string"},
            ),
        ],
        "title": "PlanningDecisionRecord",
    }
    proposal_properties = definitions["PlanningProposalBody"]["properties"]
    requirement_definition = ProposedRequirement.model_json_schema()
    nested_definitions = requirement_definition.pop("$defs", None)
    if nested_definitions:
        raise PlanningError("requirement response schema unexpectedly has definitions")
    definitions["ProposedRequirement"] = requirement_definition
    proposal_properties["requirements"] = {
        "description": (
            "Atomic requirement records. The controller compiles their IDs and "
            "descriptions into the backward-compatible internal representation."
        ),
        "items": {"$ref": "#/$defs/ProposedRequirement"},
        "minItems": 1,
        "title": "Requirements",
        "type": "array",
    }
    proposal_properties.pop("requirement_ids", None)
    proposal_required = definitions["PlanningProposalBody"]["required"]
    while "requirement_ids" in proposal_required:
        proposal_required.remove("requirement_ids")
    assumption_definition = ProposedAssumption.model_json_schema()
    nested_definitions = assumption_definition.pop("$defs", None)
    if nested_definitions:
        raise PlanningError("assumption response schema unexpectedly has definitions")
    definitions["ProposedAssumption"] = assumption_definition
    proposal_properties["assumptions"] = {
        "description": (
            "Atomic assumption records. The controller compiles each statement "
            "and its autonomy decision reference into the backward-compatible "
            "internal representation."
        ),
        "items": {"$ref": "#/$defs/ProposedAssumption"},
        "title": "Assumptions",
        "type": "array",
    }
    proposal_properties.pop("assumption_decision_ids", None)
    while "assumption_decision_ids" in proposal_required:
        proposal_required.remove("assumption_decision_ids")
    product_definition_schema = proposal_properties["product_definition"]
    product_options = product_definition_schema.get("anyOf")
    if not isinstance(product_options, list):
        raise PlanningError(
            "Planning response schema has no nullable product_definition union"
        )
    product_non_null = [
        option for option in product_options if option.get("type") != "null"
    ]
    if len(product_non_null) != 1:
        raise PlanningError(
            "Planning response schema has an invalid product_definition union"
        )
    proposal_properties["product_definition"] = product_non_null[0]
    workflow_schema = deepcopy(definitions["ProductDefinitionStatement"])
    workflow_schema["title"] = "PrimaryWorkflowStatement"
    workflow_schema["description"] = (
        "The product's core user workflow is always material, even for a "
        "one-time prototype. Reuse explicit input without unnecessary questions; "
        "ask only when the workflow is missing. Link it to its requirements."
    )
    workflow_properties = workflow_schema["properties"]
    workflow_properties["disposition"] = {
        "type": "string",
        "enum": [
            ProductDefinitionDisposition.EXPLICIT_INPUT.value,
            ProductDefinitionDisposition.RESOLVED_QUESTION.value,
        ],
    }
    workflow_properties["requirement_ids"].pop("default", None)
    workflow_properties["requirement_ids"]["minItems"] = 1
    workflow_schema["required"].append("requirement_ids")
    definitions["PrimaryWorkflowStatement"] = workflow_schema
    definitions["ProductDefinition"]["properties"]["primary_workflow"] = {
        "$ref": "#/$defs/PrimaryWorkflowStatement"
    }
    return schema


def _validate_current_planning_response_wire(
    payload: dict[str, object],
    *,
    response_schema: dict[str, object],
) -> None:
    """Enforce current question keys without breaking persisted legacy records."""

    if payload.get("kind") != PlanningResponseKind.QUESTION.value:
        return
    question = payload.get("question")
    if not isinstance(question, dict):
        return
    definitions = response_schema.get("$defs")
    definition = (
        None
        if not isinstance(definitions, dict)
        else definitions.get("PlanningQuestion")
    )
    if not isinstance(definition, dict):
        raise PlanningError("Planning response schema has no question definition")
    properties = definition.get("properties")
    required = definition.get("required")
    if not isinstance(properties, dict) or not isinstance(required, list):
        raise PlanningError("Planning response schema has invalid question fields")
    missing = sorted(set(required) - set(question))
    # decision_owner is compiled by the Controller after transport capture and is
    # therefore valid in the normalized payload even though models cannot submit it.
    unknown = sorted(set(question) - set(properties) - {"decision_owner"})
    if not missing and not unknown:
        return
    details: list[str] = []
    if missing:
        details.append("missing current fields: " + ", ".join(missing))
    if unknown:
        details.append("unknown current fields: " + ", ".join(unknown))
    question_id = question.get("id")
    subjects = (
        ()
        if not isinstance(question_id, str) or not question_id
        else _planning_subjects((ResponseIssueSubjectKind.QUESTION, question_id))
    )
    raise _planning_model_invariant(
        "planning_current_question_wire_contract",
        "Planning question violates its current wire contract ("
        + "; ".join(details)
        + ")",
        paths=("/question",),
        subjects=subjects,
    )


def _planning_proposal_body_for_model(
    body: PlanningProposalBody,
) -> dict[str, object]:
    """Project canonical persisted fields into the current model-facing contract."""

    payload = body.model_dump(mode="json")
    if len(body.requirements) == len(body.requirement_ids):
        payload["requirements"] = [
            {"id": requirement_id, "description": description}
            for requirement_id, description in zip(
                body.requirement_ids,
                body.requirements,
                strict=True,
            )
        ]
        payload.pop("requirement_ids", None)
    if len(body.assumptions) == len(body.assumption_decision_ids):
        payload["assumptions"] = [
            {"statement": statement, "decision_id": decision_id}
            for statement, decision_id in zip(
                body.assumptions,
                body.assumption_decision_ids,
                strict=True,
            )
        ]
        payload.pop("assumption_decision_ids", None)
    return payload


def _planning_response_schema_for_correction(
    plan: SemanticCorrectionPlan,
) -> dict[str, object]:
    """Bind correction-only assumption references to existing autonomy IDs."""

    schema = _planning_response_schema()
    if "/question" in plan.evidence.target_paths:
        question_schema = schema["properties"]["question"]
        question_options = question_schema.get("anyOf")
        if not isinstance(question_options, list):
            raise PlanningError(
                "Planning response schema has no nullable question union"
            )
        question_non_null = [
            option for option in question_options if option.get("type") != "null"
        ]
        if len(question_non_null) != 1:
            raise PlanningError(
                "Planning response schema has an invalid question union"
            )
        schema["properties"]["question"] = question_non_null[0]
    proposal = plan.base_payload.get("proposal")
    decisions = proposal.get("decisions") if isinstance(proposal, dict) else None
    autonomous_ids: list[str] = []
    if isinstance(decisions, list):
        for decision in decisions:
            if not isinstance(decision, dict):
                continue
            decision_id = decision.get("id")
            authority = decision.get("authority")
            if (
                isinstance(decision_id, str)
                and authority == PlanningDecisionAuthority.AGENT_AUTONOMY.value
            ):
                autonomous_ids.append(decision_id)
    definitions = schema["$defs"]
    assumption = definitions["ProposedAssumption"]
    decision_id_schema = assumption["properties"]["decision_id"]
    decision_id_schema["enum"] = list(dict.fromkeys(autonomous_ids))
    return schema


def _planning_submission_transport_schema() -> dict[str, object]:
    """Capture one JSON object before Controller-owned semantic validation."""

    return {"type": "object", "additionalProperties": True}


def _planning_request_prompt_context(request: PlanningRequest) -> dict[str, object]:
    """Project only model-relevant request facts into the Planning prompt."""

    return {
        "project_name": request.project_name,
        "source_request": request.source_request,
        "execution_profile": list(request.execution_profile),
        "base_constraints": list(request.base_constraints),
    }


class AdaptiveImplementationPlan(BaseModel):
    """Approved task-to-Agent intent bound by an adaptive TeamPlan."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[2, 3, 4, 5, 6, 7, 8, 9, 10, PLANNING_SCHEMA_VERSION] = (
        PLANNING_SCHEMA_VERSION
    )
    run_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    team_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    revision: int = Field(ge=1)
    created_at: datetime
    objective: str
    product_definition: ProductDefinition | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    requirement_ids: tuple[str, ...] = Field(
        default=(),
        exclude_if=lambda values: not values,
    )
    requirements: tuple[str, ...] = Field(
        default=(),
        exclude_if=lambda values: not values,
    )
    acceptance_criteria: tuple[ProposedCriterion, ...] = Field(
        default=(),
        exclude_if=lambda values: not values,
    )
    non_goals: tuple[str, ...] = Field(
        default=(),
        exclude_if=lambda values: not values,
    )
    approach: tuple[str, ...]
    tasks: tuple[ProposedTask, ...]
    risks: tuple[str, ...]
    assumptions: tuple[str, ...]
    assumption_decision_ids: tuple[str, ...] = Field(
        default=(),
        exclude_if=lambda values: not values,
    )
    decisions: tuple[PlanningDecisionRecord, ...] = Field(
        default=(),
        exclude_if=lambda values: not values,
    )

    @field_validator("created_at")
    @classmethod
    def require_timestamp(cls, value: datetime) -> datetime:
        return _utc(value)


class PlanningExecutionEvidence(BaseModel):
    """Bounded provider and usage evidence copied from one execution result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: AgentExecutionStatus
    session_key: str
    started_at: datetime
    finished_at: datetime
    duration_ms: int = Field(ge=0)
    provider: str | None = None
    model: str | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    estimated_cost_usd: Decimal | None = Field(default=None, ge=0)
    pricing_source: ModelMetadataSource | None = None
    budget_usage: AgentBudgetUsage | None = None
    budget_error: str | None = Field(default=None, min_length=1, max_length=2000)
    provider_liveness: ProviderLivenessEvidence | None = None
    error: str | None = None
    cost_record: ModelCallCostRecord | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def require_consistent_cost_record(self) -> Self:
        record = self.cost_record
        if record is not None and (
            record.input_tokens != self.input_tokens
            or record.output_tokens != self.output_tokens
            or record.duration_ms != self.duration_ms
            or record.cost_usd != self.estimated_cost_usd
            or record.pricing_source != self.pricing_source
        ):
            raise ValueError("Planning cost record differs from execution evidence")
        return self

    @field_validator("started_at", "finished_at")
    @classmethod
    def require_timestamp(cls, value: datetime) -> datetime:
        return _utc(value)


class PlanningTurn(BaseModel):
    """One append-only model invocation, including invalid response evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[2, 3, 4, 5, 6, 7, 8, 9, 10, PLANNING_SCHEMA_VERSION] = (
        PLANNING_SCHEMA_VERSION
    )
    run_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    sequence: int = Field(ge=1)
    previous_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    user_message: str = Field(min_length=1, max_length=10_000)
    prompt: str = Field(min_length=1, max_length=MAX_PLANNING_EVIDENCE_CHARACTERS)
    prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    response_text: str | None = Field(
        default=None,
        max_length=MAX_PLANNING_EVIDENCE_CHARACTERS,
    )
    response_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    submission_payload: dict[str, JsonValue] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    submission_evidence: AgentSubmissionEvidence | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    parsed_response: PlanningModelResponse | None = None
    response_normalizations: tuple[str, ...] = Field(
        default=(),
        exclude_if=lambda values: not values,
    )
    validation_error: str | None = Field(default=None, min_length=1, max_length=2000)
    response_validation: ResponseValidationDiagnostic | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    semantic_correction_request: SemanticCorrectionRequestEvidence | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    semantic_correction_outcome: SemanticCorrectionOutcome | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    execution: PlanningExecutionEvidence

    @field_validator("response_normalizations")
    @classmethod
    def require_unique_normalizations(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) > MAX_RESPONSE_NORMALIZATIONS:
            raise ValueError("too many Planning response normalizations")
        if any(len(value) > MAX_RESPONSE_NORMALIZATION_CHARACTERS for value in values):
            raise ValueError("Planning response normalization entries are too long")
        return _clean_unique(values, label="Planning response normalization")

    @model_validator(mode="after")
    def validate_evidence(self) -> Self:
        cost_record = self.execution.cost_record
        if self.schema_version < 11 and cost_record is not None:
            raise ValueError("legacy Planning turns cannot contain a cost record")
        if self.schema_version >= 11:
            if self.execution.budget_usage is not None and cost_record is None:
                raise ValueError("accounted Planning turn requires its cost record")
            if cost_record is not None and (
                cost_record.run_id != self.run_id
                or cost_record.stage != "planning"
                or cost_record.agent_id != "clarifier"
            ):
                raise ValueError("Planning cost record belongs to another invocation")
        if self.schema_version < 7 and (
            self.submission_payload is not None or self.submission_evidence is not None
        ):
            raise ValueError(
                "legacy Planning turns cannot contain typed submission evidence"
            )
        if self.schema_version < 6 and (
            self.execution.status is AgentExecutionStatus.RESPONSE_FINALIZATION_STALLED
            or (
                self.execution.provider_liveness is not None
                and self.execution.provider_liveness.terminal_response_observed
            )
        ):
            raise ValueError(
                "legacy Planning turns cannot contain response-finalization state"
            )
        if _digest_text(self.prompt) != self.prompt_sha256:
            raise ValueError("Planning prompt digest does not match its content")
        if self.response_text is None:
            if self.response_sha256 is not None:
                raise ValueError("missing response text cannot have a digest")
            if self.schema_version < 7 and (
                self.parsed_response is not None or self.response_normalizations
            ):
                raise ValueError(
                    "legacy missing response text cannot have parsed evidence"
                )
        elif _digest_text(self.response_text) != self.response_sha256:
            raise ValueError("Planning response digest does not match its content")
        if self.submission_payload is not None:
            if (
                self.submission_evidence is None
                or self.submission_evidence.status is not AgentSubmissionStatus.ACCEPTED
                or (
                    self.submission_evidence.semantic_payload_sha256
                    or self.submission_evidence.payload_sha256
                )
                != canonical_json_sha256(self.submission_payload)
            ):
                raise ValueError(
                    "Planning submission payload requires matching accepted evidence"
                )
        elif (
            self.submission_evidence is not None
            and self.submission_evidence.status is AgentSubmissionStatus.ACCEPTED
        ):
            raise ValueError(
                "accepted Planning submission evidence requires its payload"
            )
        if self.schema_version >= 7 and self.submission_evidence is not None:
            expected_purpose = (
                AgentSubmissionPurpose.SEMANTIC_CORRECTION
                if self.semantic_correction_request is not None
                else AgentSubmissionPurpose.PLANNING_RESPONSE
            )
            if self.submission_evidence.purpose is not expected_purpose:
                raise ValueError(
                    "Planning submission purpose does not match the turn contract"
                )
        if (
            self.schema_version >= 7
            and self.parsed_response is not None
            and self.submission_payload is None
        ):
            raise ValueError(
                "current parsed Planning response requires its typed submission"
            )
        if self.parsed_response is not None and self.validation_error is not None:
            raise ValueError("valid Planning turns cannot contain a validation error")
        if (self.semantic_correction_request is None) != (
            self.semantic_correction_outcome is None
        ):
            raise ValueError(
                "Planning correction request and outcome must appear together"
            )
        if self.execution.status is AgentExecutionStatus.COMPLETED:
            if self.schema_version >= 7 and (
                self.submission_evidence is None
                or self.submission_evidence.status is not AgentSubmissionStatus.ACCEPTED
            ):
                raise ValueError(
                    "current completed Planning execution requires accepted typed "
                    "submission evidence"
                )
            if self.response_text is None and self.submission_payload is None:
                raise ValueError(
                    "completed Planning execution requires response text or a typed "
                    "submission"
                )
            if self.parsed_response is None and self.validation_error is None:
                raise ValueError(
                    "completed Planning response requires validation state"
                )
        elif self.validation_error is None:
            raise ValueError("failed Planning execution requires a validation error")
        return self


class PlanningProposal(BaseModel):
    """Immutable, validated proposal revision shown to the user."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[2, 3, 4, 5, 6, 7, 8, 9, 10, PLANNING_SCHEMA_VERSION] = (
        PLANNING_SCHEMA_VERSION
    )
    run_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    revision: int = Field(ge=1)
    created_at: datetime
    source: PlanningProposalSource
    source_turn_sequence: int | None = Field(default=None, ge=1)
    change_request: str | None = Field(default=None, min_length=1, max_length=2000)
    body: PlanningProposalBody
    timeout_overrides_seconds: dict[str, int] = Field(default_factory=dict)
    model_profile_overrides: dict[str, str] = Field(default_factory=dict)

    @field_validator("created_at")
    @classmethod
    def require_timestamp(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def require_source_evidence(self) -> Self:
        if self.source is PlanningProposalSource.MODEL:
            if self.source_turn_sequence is None:
                raise ValueError("model proposal requires its source turn")
            if self.timeout_overrides_seconds:
                raise ValueError("model proposals cannot authorize timeout overrides")
            if self.model_profile_overrides:
                raise ValueError("model proposals cannot authorize model overrides")
        elif self.source_turn_sequence is not None:
            raise ValueError("structured edit cannot claim a model turn")
        known_agents = {agent.id for agent in self.body.agents}
        unknown_agents = set(self.timeout_overrides_seconds) - known_agents
        if unknown_agents:
            raise ValueError(
                "timeout overrides reference unknown Agents: "
                + ", ".join(sorted(unknown_agents))
            )
        unknown_model_agents = set(self.model_profile_overrides) - known_agents
        if unknown_model_agents:
            raise ValueError(
                "model overrides reference unknown Agents: "
                + ", ".join(sorted(unknown_model_agents))
            )
        if any(
            re.fullmatch(r"[a-z][a-z0-9_]*", profile_id) is None
            for profile_id in self.model_profile_overrides.values()
        ):
            raise ValueError("model overrides require safe profile IDs")
        if any(
            not 30 <= seconds <= 3600
            for seconds in self.timeout_overrides_seconds.values()
        ):
            raise ValueError("timeout overrides must be within 30..3600s")
        return self


class PlanningSession(BaseModel):
    """Atomic index anchoring all write-once Planning evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[2, 3, 4, 5, 6, 7, 8, 9, 10, PLANNING_SCHEMA_VERSION] = (
        PLANNING_SCHEMA_VERSION
    )
    run_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: PlanningSessionStatus
    created_at: datetime
    updated_at: datetime
    turn_count: int = Field(default=0, ge=0)
    turn_head_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    latest_proposal_revision: int | None = Field(default=None, ge=1)
    approved_revision: int | None = Field(default=None, ge=1)

    @field_validator("created_at", "updated_at")
    @classmethod
    def require_timestamp(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        if self.updated_at < self.created_at:
            raise ValueError("Planning session cannot update before creation")
        if (self.turn_count == 0) != (self.turn_head_sha256 is None):
            raise ValueError("Planning turn count and head digest are inconsistent")
        if (
            self.status is PlanningSessionStatus.PROPOSED
            and self.latest_proposal_revision is None
        ):
            raise ValueError("proposed Planning session requires a proposal")
        if self.status is PlanningSessionStatus.APPROVED:
            if self.approved_revision != self.latest_proposal_revision:
                raise ValueError("approved Planning session must bind latest proposal")
        elif self.approved_revision is not None:
            raise ValueError("only approved Planning sessions have approval evidence")
        return self


class StructuredPlanEdit(BaseModel):
    """One safe controller-owned edit selected through the product UI."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: StructuredEditKind
    value: int | str
    agent_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]*$")

    @model_validator(mode="after")
    def require_target(self) -> Self:
        if self.kind is StructuredEditKind.AGENT_MODEL:
            if self.agent_id is None:
                raise ValueError("Agent-specific edits require an Agent target")
        elif self.agent_id is not None:
            raise ValueError("only Agent-specific edits accept an Agent target")
        if self.kind is StructuredEditKind.AGENT_MODEL:
            if (
                not isinstance(self.value, str)
                or re.fullmatch(r"[a-z][a-z0-9_]*", self.value) is None
            ):
                raise ValueError("Agent model edits require a safe profile ID")
            return self
        if not isinstance(self.value, int):
            raise ValueError("numeric plan edits require an integer")
        if self.value < 1:
            raise ValueError("numeric plan edits require a positive value")
        return self


class CapabilityTimeoutPolicy(BaseModel):
    """Controller-owned evaluation timeout or disabled product timeout."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    default_seconds: int = Field(ge=0)
    ceiling_seconds: int = Field(ge=0)

    @model_validator(mode="after")
    def require_ordered_envelope(self) -> Self:
        if self.default_seconds > self.ceiling_seconds:
            raise ValueError("timeout default cannot exceed its ceiling")
        if (self.default_seconds == 0) != (self.ceiling_seconds == 0):
            raise ValueError("a disabled product timeout requires a zero-only envelope")
        if 0 < self.default_seconds < 30:
            raise ValueError("positive invocation timeouts must be at least 30s")
        return self

    def resolve(self, workload: AgentWorkload) -> int:
        """Map a qualitative estimate to a deterministic policy value."""

        if workload is AgentWorkload.ROUTINE:
            return self.default_seconds
        if workload is AgentWorkload.COMPLEX:
            return self.ceiling_seconds
        return self.default_seconds + (self.ceiling_seconds - self.default_seconds) // 2


class AgentTimeoutResolution(BaseModel):
    """Explain how the controller resolved one approved invocation timeout."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    workload: AgentWorkload
    default_seconds: int = Field(ge=0)
    ceiling_seconds: int = Field(ge=0)
    minimum_seconds: int | None = Field(default=None, ge=0)
    scope_criterion_count: int | None = Field(default=None, ge=1, le=100)
    scope_boundary_obligation_count: int | None = Field(
        default=None,
        ge=0,
        le=400,
    )
    resolved_seconds: int = Field(ge=0)
    source: Literal[
        "policy_workload",
        "policy_scope_floor",
        "user_override",
        "provider_activity",
    ]

    @model_validator(mode="after")
    def require_valid_resolution(self) -> Self:
        if self.source == "provider_activity":
            if any(
                value != 0
                for value in (
                    self.default_seconds,
                    self.ceiling_seconds,
                    self.resolved_seconds,
                )
            ):
                raise ValueError(
                    "provider-activity liveness cannot carry a wall-clock limit"
                )
            if self.minimum_seconds not in {None, 0}:
                raise ValueError(
                    "provider-activity liveness cannot carry a timeout minimum"
                )
            if (
                self.scope_criterion_count is not None
                or self.scope_boundary_obligation_count is not None
            ):
                raise ValueError(
                    "provider-activity liveness cannot use review scope as time"
                )
            return self
        policy = CapabilityTimeoutPolicy(
            default_seconds=self.default_seconds,
            ceiling_seconds=self.ceiling_seconds,
        )
        minimum = self.minimum_seconds or self.default_seconds
        if not self.default_seconds <= minimum <= self.ceiling_seconds:
            raise ValueError("timeout minimum must remain inside its policy envelope")
        if self.scope_criterion_count is None and minimum != self.default_seconds:
            raise ValueError("a raised timeout minimum requires review scope evidence")
        if self.scope_criterion_count is not None and self.minimum_seconds is None:
            raise ValueError("review scope evidence requires an explicit minimum")
        if (
            self.scope_boundary_obligation_count is not None
            and self.scope_criterion_count is None
        ):
            raise ValueError("boundary obligations require criterion scope evidence")
        if not minimum <= self.resolved_seconds <= self.ceiling_seconds:
            raise ValueError("resolved timeout must remain inside its policy envelope")
        workload_seconds = policy.resolve(self.workload)
        if self.source == "policy_workload":
            if self.resolved_seconds != workload_seconds or workload_seconds < minimum:
                raise ValueError("policy timeout does not match the workload mapping")
        elif self.source == "policy_scope_floor" and (
            self.scope_criterion_count is None
            or minimum <= workload_seconds
            or self.resolved_seconds != minimum
        ):
            raise ValueError("scope timeout does not match its controller floor")
        return self


class PlanningApproval(BaseModel):
    """Explicit user authorization for one exact proposal and compiled plan."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[2, 3, 4, 5, 6, 7, 8, 9, 10, PLANNING_SCHEMA_VERSION] = (
        PLANNING_SCHEMA_VERSION
    )
    run_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    revision: int = Field(ge=1)
    approved_at: datetime
    confirmation: Literal["user_approved"]
    proposal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    task_brief_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    implementation_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    team_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    timeout_resolutions: tuple[AgentTimeoutResolution, ...] = Field(min_length=2)

    @field_validator("approved_at")
    @classmethod
    def require_timestamp(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("timeout_resolutions")
    @classmethod
    def require_unique_timeout_agents(
        cls,
        values: tuple[AgentTimeoutResolution, ...],
    ) -> tuple[AgentTimeoutResolution, ...]:
        agent_ids = [resolution.agent_id for resolution in values]
        if len(agent_ids) != len(set(agent_ids)):
            raise ValueError("timeout resolutions must identify unique Agents")
        return values


class PlanningPolicy(BaseModel):
    """Controller limits around dialogue and adaptive proposal compilation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_clarification_rounds: int | None = Field(default=3, ge=0)
    max_proposal_revisions: int | None = Field(default=3, ge=1)
    response_repair_limit: int | None = Field(default=None, ge=0, le=2)
    planning_timeout_seconds: int = Field(default=180, ge=0)
    max_agents: int | None = Field(default=8, ge=2)
    max_concurrency: int = Field(default=4, ge=1)
    max_review_agents: int | None = Field(default=16, ge=1)
    max_iterations: int | None = Field(default=3, ge=1)
    run_deadline_seconds: int | None = Field(default=None, ge=1)
    review_substantial_work_unit_threshold: int = Field(default=6, ge=2, le=499)
    review_complex_work_unit_threshold: int = Field(default=11, ge=3, le=500)
    budget: AgentBudget
    capability_timeouts: dict[AgentCapability, CapabilityTimeoutPolicy]
    model_routing: ModelRoutingPolicy | None = None
    profile_acceptance_criteria: tuple[AcceptanceCriterion, ...] = ()
    require_review_agent: bool = False

    @field_validator("capability_timeouts")
    @classmethod
    def require_runtime_capabilities(
        cls,
        values: dict[AgentCapability, CapabilityTimeoutPolicy],
    ) -> dict[AgentCapability, CapabilityTimeoutPolicy]:
        required = {
            AgentCapability.IMPLEMENTATION,
            AgentCapability.INTEGRATION,
            AgentCapability.TESTING,
            AgentCapability.REVIEW,
        }
        if set(values) != required:
            raise ValueError(
                "Planning policy requires every runtime capability timeout"
            )
        return values

    @field_validator("profile_acceptance_criteria")
    @classmethod
    def require_unique_profile_criteria(
        cls,
        values: tuple[AcceptanceCriterion, ...],
    ) -> tuple[AcceptanceCriterion, ...]:
        identifiers = [criterion.id for criterion in values]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("profile acceptance criterion IDs must be unique")
        return values

    @model_validator(mode="after")
    def require_ordered_review_scope_thresholds(self) -> Self:
        if (
            self.review_substantial_work_unit_threshold
            >= self.review_complex_work_unit_threshold
        ):
            raise ValueError("review scope timeout thresholds must be ordered")
        return self

    def review_scope_workload(
        self,
        criterion_count: int,
        boundary_obligation_count: int = 0,
    ) -> AgentWorkload:
        """Classify Review work from criteria plus explicit boundary obligations."""

        if not 1 <= criterion_count <= 100:
            raise ValueError("review criterion count must be within 1..100")
        if not 0 <= boundary_obligation_count <= 400:
            raise ValueError("review boundary obligation count must be within 0..400")
        work_units = criterion_count + boundary_obligation_count
        if work_units >= self.review_complex_work_unit_threshold:
            return AgentWorkload.COMPLEX
        if work_units >= self.review_substantial_work_unit_threshold:
            return AgentWorkload.SUBSTANTIAL
        return AgentWorkload.ROUTINE


class PlanningPreview(BaseModel):
    """Validated controller interpretation shown before user approval."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    destination: str
    execution_profile: tuple[str, ...]
    execution_profile_constraints: tuple[str, ...]
    planner_constraints: tuple[str, ...]
    task_brief: TaskBrief
    implementation_plan: AdaptiveImplementationPlan
    team_plan: TeamPlan
    timeout_resolutions: tuple[AgentTimeoutResolution, ...]


class ApprovedPlanningResult(BaseModel):
    """Approved inputs ready for the dynamic controller runtime."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_brief: TaskBrief
    implementation_plan: AdaptiveImplementationPlan
    team_plan: TeamPlan
    approval: PlanningApproval

    @model_validator(mode="after")
    def validate_approval_boundary(self) -> Self:
        """Bind execution inputs to the exact proposal revision the user approved."""

        run_ids = {
            self.task_brief.run_id,
            self.implementation_plan.run_id,
            self.team_plan.run_id,
            self.approval.run_id,
        }
        if len(run_ids) != 1:
            raise ValueError("approved Planning inputs use different run IDs")
        if (
            self.implementation_plan.revision != self.approval.revision
            or self.team_plan.revision != self.approval.revision
        ):
            raise ValueError("approved Planning inputs use different revisions")
        if self.implementation_plan.team_id != self.team_plan.team_id:
            raise ValueError(
                "approved implementation and team plans use different teams"
            )
        if self.team_plan.task_brief_sha256 != canonical_model_sha256(self.task_brief):
            raise ValueError("approved TeamPlan does not bind the supplied TaskBrief")
        if self.team_plan.implementation_plan_sha256 != canonical_model_sha256(
            self.implementation_plan
        ):
            raise ValueError(
                "approved TeamPlan does not bind the supplied implementation plan"
            )
        expected_digests = {
            "task brief": (
                self.approval.task_brief_sha256,
                canonical_model_sha256(self.task_brief),
            ),
            "implementation plan": (
                self.approval.implementation_plan_sha256,
                canonical_model_sha256(self.implementation_plan),
            ),
            "TeamPlan": (
                self.approval.team_plan_sha256,
                canonical_model_sha256(self.team_plan),
            ),
        }
        mismatched = [
            label
            for label, (approved, actual) in expected_digests.items()
            if approved != actual
        ]
        if mismatched:
            raise ValueError(
                "Planning approval does not bind the supplied " + ", ".join(mismatched)
            )
        agents_by_id = {agent.id: agent for agent in self.team_plan.agents}
        resolutions_by_id = {
            resolution.agent_id: resolution
            for resolution in self.approval.timeout_resolutions
        }
        if set(resolutions_by_id) != set(agents_by_id):
            raise ValueError(
                "Planning approval timeout resolutions do not cover the TeamPlan Agents"
            )
        mismatched_timeouts = [
            agent_id
            for agent_id, resolution in resolutions_by_id.items()
            if resolution.resolved_seconds != agents_by_id[agent_id].timeout_seconds
        ]
        if mismatched_timeouts:
            raise ValueError(
                "Planning approval timeout resolutions do not match the TeamPlan for "
                + ", ".join(sorted(mismatched_timeouts))
            )
        return self


Clock = Callable[[], datetime]
QuestionAnswerer = Callable[[PlanningQuestion], str | None]
InputReader = Callable[[str], str]
OutputWriter = Callable[[str], None]


def _system_clock() -> datetime:
    return datetime.now(UTC)


def preview_adaptive_proposal(
    request: PlanningRequest,
    proposal: PlanningProposal,
    policy: PlanningPolicy,
    *,
    created_at: datetime,
) -> PlanningPreview:
    """Compile and validate the exact authority that approval would create."""

    if proposal.run_id != request.run_id:
        raise PlanningError("proposal belongs to a different Planning request")
    body = proposal.body
    if proposal.schema_version >= 5:
        validate_planning_clarity(
            body,
            source_request=request.source_request,
            additional_user_inputs=(
                () if proposal.change_request is None else (proposal.change_request,)
            ),
            allowed_criterion_ids=(
                criterion.id for criterion in policy.profile_acceptance_criteria
            ),
            require_current_decision_provenance=proposal.schema_version >= 8,
            allow_legacy_product_decision_links=proposal.schema_version < 8,
            allow_legacy_workflow_exemption=proposal.schema_version < 9,
        )
    if policy.max_agents is not None and len(body.agents) > policy.max_agents:
        raise PlanningError(
            f"proposal has {len(body.agents)} Agents; policy permits "
            f"{policy.max_agents}"
        )
    if body.max_concurrency > policy.max_concurrency:
        raise PlanningError(
            f"proposal concurrency {body.max_concurrency} exceeds the policy "
            f"ceiling of {policy.max_concurrency}"
        )
    if (
        policy.max_iterations is not None
        and body.iteration_limit > policy.max_iterations
    ):
        raise PlanningError(
            f"proposal has {body.iteration_limit} iterations; policy permits "
            f"{policy.max_iterations}"
        )
    if (
        policy.budget.max_calls is not None
        and len(body.agents) > policy.budget.max_calls
    ):
        raise PlanningError("proposal Agent count exceeds the approved call budget")
    planned_calls = len(body.agents) * body.iteration_limit
    if policy.budget.max_calls is not None and planned_calls > policy.budget.max_calls:
        raise PlanningError(
            f"proposal requires up to {planned_calls} planned Agent calls, but the "
            f"approved budget permits {policy.budget.max_calls}"
        )
    incomplete_absolute_boundaries = tuple(
        criterion.id
        for criterion in body.acceptance_criteria
        if _ABSOLUTE_GUARANTEE_PATTERN.search(criterion.description)
        and set(criterion.review_boundaries) != set(_ALL_REVIEW_BOUNDARIES)
    )
    if incomplete_absolute_boundaries:
        raise PlanningError(
            "unqualified prohibitions and safety guarantees must require "
            "top-level, nested, alias-or-indirection, and failure-path Review "
            "boundaries: " + ", ".join(incomplete_absolute_boundaries)
        )
    if _ABSOLUTE_GUARANTEE_PATTERN.search(request.source_request) and not any(
        set(criterion.review_boundaries) == set(_ALL_REVIEW_BOUNDARIES)
        for criterion in body.acceptance_criteria
    ):
        raise PlanningError(
            "the user request contains an unqualified prohibition or safety "
            "guarantee, but no proposed acceptance criterion preserves all four "
            "Review boundaries"
        )
    profile_criterion_ids = {
        criterion.id for criterion in policy.profile_acceptance_criteria
    }
    proposed_criterion_ids = {criterion.id for criterion in body.acceptance_criteria}
    collisions = profile_criterion_ids & proposed_criterion_ids
    if collisions:
        raise PlanningError(
            "proposal repeats controller-owned profile criteria: "
            + ", ".join(sorted(collisions))
        )
    try:
        validate_task_criterion_references(
            body.tasks,
            proposed_criterion_ids | profile_criterion_ids,
        )
    except _PlanningModelInvariantError as error:
        raise _PlanningContextInvariantError(error.invariant) from error
    except ValueError as error:
        raise PlanningError(str(error)) from error
    if policy.require_review_agent and not any(
        agent.capability is AgentCapability.REVIEW for agent in body.agents
    ):
        raise PlanningError(
            "this execution profile requires an independent review Agent"
        )
    review_count = sum(
        agent.capability is AgentCapability.REVIEW for agent in body.agents
    )
    if policy.max_review_agents is not None and review_count > policy.max_review_agents:
        raise PlanningError(
            f"proposal has {review_count} review Agents; this profile permits "
            f"{policy.max_review_agents}"
        )

    constraints = tuple(dict.fromkeys((*request.base_constraints, *body.constraints)))
    task_brief = TaskBrief(
        run_id=request.run_id,
        title=body.title,
        source_request=request.source_request,
        requirements=list(body.requirements),
        product_definition=body.product_definition,
        acceptance_criteria=[
            AcceptanceCriterion(
                id=item.id,
                description=item.description,
                verification=item.verification,
                review_boundaries=item.review_boundaries,
            )
            for item in body.acceptance_criteria
        ]
        + list(policy.profile_acceptance_criteria),
        constraints=list(constraints),
        assumptions=list(body.assumptions),
        open_questions=[],
        confirmed=True,
    )
    implementation_plan = AdaptiveImplementationPlan(
        schema_version=proposal.schema_version,
        run_id=request.run_id,
        team_id="adaptive_team",
        revision=proposal.revision,
        created_at=created_at,
        objective=body.objective,
        product_definition=body.product_definition,
        requirement_ids=body.requirement_ids,
        requirements=body.requirements,
        acceptance_criteria=body.acceptance_criteria,
        non_goals=body.non_goals,
        approach=body.approach,
        tasks=body.tasks,
        risks=body.risks,
        assumptions=body.assumptions,
        assumption_decision_ids=body.assumption_decision_ids,
        decisions=body.decisions,
    )

    routing_policy = policy.model_routing or ModelRoutingPolicy(
        mode=ModelRoutingMode.STRICT,
        profiles=(
            ModelProfile(
                id="default",
                model=request.model,
                capabilities=tuple(AgentCapability),
            ),
        ),
        default_profile_id="default",
    )
    if routing_policy.get_profile(routing_policy.default_profile_id).model != (
        request.model
    ):
        raise PlanningError(
            "Planning request model differs from the routing policy bootstrap model"
        )
    try:
        model_routes = resolve_model_route_plan(
            routing_policy,
            body.agents,
            agent_profile_overrides=proposal.model_profile_overrides,
        )
    except ModelRoutingError as error:
        raise PlanningError(str(error)) from error
    assignments = {
        assignment.agent_id: assignment for assignment in model_routes.assignments
    }

    agents = []
    timeout_resolutions = []
    for proposed in body.agents:
        timeout_policy = policy.capability_timeouts[proposed.capability]
        override = proposal.timeout_overrides_seconds.get(proposed.id)
        if timeout_policy.default_seconds == 0:
            if override is not None:
                raise PlanningError(
                    "product plans cannot override provider-activity liveness "
                    f"with a wall-clock timeout ({proposed.id})"
                )
            timeout_resolutions.append(
                AgentTimeoutResolution(
                    agent_id=proposed.id,
                    workload=proposed.workload,
                    default_seconds=0,
                    ceiling_seconds=0,
                    resolved_seconds=0,
                    source="provider_activity",
                )
            )
            agents.append(
                AgentSpec(
                    id=proposed.id,
                    label=proposed.label,
                    responsibility=proposed.responsibility,
                    rationale=proposed.rationale,
                    capability=proposed.capability,
                    permission_profile=permission_for_capability(proposed.capability),
                    stage_id=proposed.stage_id,
                    dependencies=proposed.dependencies,
                    expected_output=expected_output_for_capability(proposed.capability),
                    model_route_id=assignments[proposed.id].primary_route_id,
                    timeout_seconds=0,
                    workspace_scope=proposed.workspace_scope,
                )
            )
            continue

        scope_criterion_count = (
            len(task_brief.acceptance_criteria)
            if proposed.capability is AgentCapability.REVIEW
            else None
        )
        scope_boundary_obligation_count = (
            sum(
                len(criterion.review_boundaries)
                for criterion in task_brief.acceptance_criteria
            )
            if proposed.capability is AgentCapability.REVIEW
            else None
        )
        minimum_timeout = timeout_policy.default_seconds
        if scope_criterion_count is not None:
            minimum_timeout = timeout_policy.resolve(
                policy.review_scope_workload(
                    scope_criterion_count,
                    scope_boundary_obligation_count or 0,
                )
            )
        if override is not None and not (
            minimum_timeout <= override <= timeout_policy.ceiling_seconds
        ):
            raise PlanningError(
                f"Agent {proposed.id} timeout override {override}s is outside the "
                f"{proposed.capability.value} policy envelope of "
                f"{minimum_timeout}.."
                f"{timeout_policy.ceiling_seconds}s"
            )
        workload_timeout = timeout_policy.resolve(proposed.workload)
        policy_timeout = max(workload_timeout, minimum_timeout)
        resolved_timeout = policy_timeout if override is None else override
        resolution_source = (
            "user_override"
            if override is not None
            else "policy_scope_floor"
            if minimum_timeout > workload_timeout
            else "policy_workload"
        )
        timeout_resolutions.append(
            AgentTimeoutResolution(
                agent_id=proposed.id,
                workload=proposed.workload,
                default_seconds=timeout_policy.default_seconds,
                ceiling_seconds=timeout_policy.ceiling_seconds,
                minimum_seconds=minimum_timeout,
                scope_criterion_count=scope_criterion_count,
                scope_boundary_obligation_count=scope_boundary_obligation_count,
                resolved_seconds=resolved_timeout,
                source=resolution_source,
            )
        )
        agents.append(
            AgentSpec(
                id=proposed.id,
                label=proposed.label,
                responsibility=proposed.responsibility,
                rationale=proposed.rationale,
                capability=proposed.capability,
                permission_profile=permission_for_capability(proposed.capability),
                stage_id=proposed.stage_id,
                dependencies=proposed.dependencies,
                expected_output=expected_output_for_capability(proposed.capability),
                model_route_id=assignments[proposed.id].primary_route_id,
                timeout_seconds=resolved_timeout,
                workspace_scope=proposed.workspace_scope,
            )
        )
    team_plan = TeamPlan(
        plan_id=f"{request.run_id}-team-r{proposal.revision}",
        revision=proposal.revision,
        run_id=request.run_id,
        task_brief_sha256=canonical_model_sha256(task_brief),
        implementation_plan_sha256=canonical_model_sha256(implementation_plan),
        team_id="adaptive_team",
        origin=TeamPlanOrigin.ADAPTIVE_PLANNING,
        approval_source=PlanApprovalSource.USER,
        created_at=created_at,
        agents=tuple(agents),
        model_routes=model_routes,
        budget=policy.budget,
        run_deadline_seconds=policy.run_deadline_seconds,
        iteration_limit=body.iteration_limit,
        max_concurrency=body.max_concurrency,
        independent_review=True,
        revision_enabled=body.revision_enabled,
    )
    return PlanningPreview(
        destination=request.destination,
        execution_profile=request.execution_profile,
        execution_profile_constraints=request.base_constraints,
        planner_constraints=tuple(
            constraint
            for constraint in body.constraints
            if constraint not in request.base_constraints
        ),
        task_brief=task_brief,
        implementation_plan=implementation_plan,
        team_plan=team_plan,
        timeout_resolutions=tuple(timeout_resolutions),
    )


def apply_structured_edit(
    proposal: PlanningProposal,
    edit: StructuredPlanEdit,
    *,
    created_at: datetime,
) -> PlanningProposal:
    """Create one new proposal revision through a bounded safe edit."""

    body = proposal.body
    model_overrides = dict(proposal.model_profile_overrides)
    if edit.kind is StructuredEditKind.MAX_CONCURRENCY:
        assert isinstance(edit.value, int)
        body = body.model_copy(update={"max_concurrency": edit.value})
        description = f"Set maximum concurrency to {edit.value}."
    elif edit.kind is StructuredEditKind.ITERATION_LIMIT:
        assert isinstance(edit.value, int)
        body = body.model_copy(
            update={
                "iteration_limit": edit.value,
                "revision_enabled": edit.value > 1,
            }
        )
        description = f"Set iteration limit to {edit.value}."
    else:
        if edit.agent_id not in {agent.id for agent in body.agents}:
            raise PlanningError(f"unknown Agent for model edit: {edit.agent_id}")
        assert edit.agent_id is not None
        assert isinstance(edit.value, str)
        model_overrides[edit.agent_id] = edit.value
        description = f"Set {edit.agent_id} model profile to {edit.value}."
    body = PlanningProposalBody.model_validate(body.model_dump(mode="json"))
    return PlanningProposal(
        schema_version=proposal.schema_version,
        run_id=proposal.run_id,
        revision=proposal.revision + 1,
        created_at=created_at,
        source=PlanningProposalSource.STRUCTURED_EDIT,
        change_request=description,
        body=body,
        timeout_overrides_seconds=proposal.timeout_overrides_seconds,
        model_profile_overrides=model_overrides,
    )


def _render_model_pricing(route: ModelRoute) -> str:
    """Render one secret-free route price without inventing a zero estimate."""

    if route.input_cost_per_million_usd is None:
        return "not configured"
    return (
        f"${route.input_cost_per_million_usd} input / "
        f"${route.output_cost_per_million_usd} output per million tokens"
        + (
            "; cache pricing unknown"
            if route.cache_pricing is None
            else f"; cache ${route.cache_pricing.read_cost_per_million_usd} read / "
            f"${route.cache_pricing.write_cost_per_million_usd} write per million "
            f"({route.cache_pricing.source.value})"
        )
    )


def render_planning_overview(
    preview: PlanningPreview,
    *,
    budget_usage: AgentBudgetUsage | None = None,
) -> str:
    """Render every material decision a user approves before execution."""

    brief = preview.task_brief
    implementation = preview.implementation_plan
    plan = preview.team_plan
    requirement_pairs = (
        tuple(
            zip(
                implementation.requirement_ids,
                implementation.requirements,
                strict=True,
            )
        )
        if implementation.requirement_ids
        else tuple(
            (f"REQ_LEGACY_{index}", text)
            for index, text in enumerate(brief.requirements, start=1)
        )
    )
    lines = ["Planning overview", "  Product definition and scope:"]
    lines.extend(_render_prefixed_text("  Product: ", brief.title))
    lines.extend(_render_prefixed_text("  Request: ", brief.source_request))
    definition = implementation.product_definition
    if definition is None:
        lines.append("  Product depth: unavailable in legacy Planning evidence")
    else:
        lines.append("  Audience and killer workflow:")
        lines.extend(
            _render_prefixed_text(
                f"    - target users [{definition.target_users.disposition.value}]: ",
                definition.target_users.statement,
            )
        )
        lines.extend(
            _render_prefixed_text(
                "    - primary workflow "
                f"[{definition.primary_workflow.disposition.value}]: ",
                definition.primary_workflow.statement,
            )
        )
        lines.append(
            "    - delivery maturity: "
            + definition.delivery_maturity.level.value
            + f" [{definition.delivery_maturity.disposition.value}]"
        )
        lines.append("  Quality and delivery expectations:")
        for label, item in (
            ("usability", definition.usability_expectations),
            ("operations", definition.operational_expectations),
            ("delivery", definition.delivery_expectations),
        ):
            lines.extend(
                _render_prefixed_text(
                    f"    - {label} [{item.disposition.value}]: ",
                    item.statement,
                )
            )
        lines.append("  Product-definition effects:")
        for label, value in (
            ("architecture", definition.impact.architecture),
            ("team", definition.impact.team),
            ("cost", definition.impact.cost),
            ("delivery", definition.impact.delivery),
        ):
            lines.extend(_render_prefixed_text(f"    - {label}: ", value))

    lines.append("  Non-goals:")
    if implementation.non_goals:
        for item in implementation.non_goals:
            lines.extend(_render_prefixed_text("    - ", item))
    else:
        lines.append("    - unavailable in legacy Planning evidence")
    lines.extend(_render_prefixed_text("  Destination: ", preview.destination))
    lines.append("  Execution profile:")
    for item in preview.execution_profile:
        lines.extend(_render_prefixed_text("    - ", item))
    lines.append("  Requirements:")
    for requirement_id, description in requirement_pairs:
        canonical_description = _strip_redundant_stable_id_prefix(
            description,
            requirement_id,
        )
        lines.extend(
            _render_prefixed_text(
                f"    - {requirement_id}: ",
                canonical_description,
            )
        )
    if preview.execution_profile_constraints:
        lines.append("  Execution-profile constraints (controller-owned):")
        for item in preview.execution_profile_constraints:
            lines.extend(_render_prefixed_text("    - ", item))
    if preview.planner_constraints:
        lines.append("  Additional task constraints proposed by Planning:")
        for item in preview.planner_constraints:
            lines.extend(_render_prefixed_text("    - ", item))
    lines.append("  Decisions and assumptions:")
    decision_groups = (
        (
            PlanningDecisionAuthority.USER,
            "Additional user decisions resolved during clarification",
        ),
        (
            PlanningDecisionAuthority.PLANNER_PROPOSAL,
            "Planning recommendations requiring approval",
        ),
        (
            PlanningDecisionAuthority.AGENT_AUTONOMY,
            "Agent or Controller autonomy within the approved boundary",
        ),
    )
    for authority, label in decision_groups:
        records = tuple(
            decision
            for decision in implementation.decisions
            if decision.authority is authority
        )
        lines.append(f"    {label}:")
        if not records:
            lines.append(
                "      - none beyond the user-owned source request shown above"
                if authority is PlanningDecisionAuthority.USER
                else "      - none"
            )
        for decision in records:
            question_id = decision.resolved_question_id
            question = "" if question_id is None else f"; question={question_id}"
            direct_source = (
                ""
                if decision.provenance is None
                or decision.provenance.kind
                is not PlanningDecisionProvenanceKind.EXPLICIT_INPUT
                else f"; source={decision.provenance.source!r}"
            )
            lines.extend(
                _render_prefixed_text(
                    f"      - {decision.id} "
                    f"[{decision.category.value}{question}{direct_source}]: ",
                    f"{decision.summary} (why: {decision.rationale})",
                )
            )
    lines.append("    Assumptions:")
    if not implementation.assumptions:
        lines.append("      - none")
    if implementation.assumption_decision_ids:
        for assumption, decision_id in zip(
            implementation.assumptions,
            implementation.assumption_decision_ids,
            strict=True,
        ):
            lines.extend(_render_prefixed_text(f"      - {decision_id}: ", assumption))
    else:
        for item in implementation.assumptions:
            lines.extend(_render_prefixed_text("      - legacy/unowned: ", item))
    lines.extend(
        (
            "    Non-negotiable Controller policy:",
            "      - secret isolation and least-privilege permissions",
            "      - immutable evidence and fail-closed lifecycle transitions",
            "      - cleanup limited to resources proven to be SAT-owned",
            "      - only a verified accepted workspace may be delivered",
        )
    )
    lines.append("  Acceptance criteria:")
    for item in brief.acceptance_criteria:
        review_boundaries = (
            ", ".join(boundary.value for boundary in item.review_boundaries) or "none"
        )
        lines.extend(
            _render_prefixed_text(
                f"    - {item.id}: ",
                f"{item.description} (verify: {item.verification}; "
                f"Review boundaries: {review_boundaries})",
            )
        )
    used_boundaries = tuple(
        dict.fromkeys(
            boundary
            for criterion in brief.acceptance_criteria
            for boundary in criterion.review_boundaries
        )
    )
    if used_boundaries:
        definitions = review_boundary_definition_map()
        lines.extend(
            (
                "  Review boundary definitions:",
                *(
                    f"    - {boundary.value}: {definitions[boundary.value]}"
                    for boundary in used_boundaries
                ),
            )
        )
    lines.append("  Requirement-to-evidence traceability:")
    if not implementation.acceptance_criteria:
        lines.append("    - unavailable in legacy Planning evidence")
    proposal_criteria = {item.id: item for item in implementation.acceptance_criteria}
    rendered_criterion_ids: set[str] = set()
    for requirement_id, _ in requirement_pairs:
        criteria = tuple(
            item
            for item in implementation.acceptance_criteria
            if requirement_id in item.requirement_ids
        )
        rendered_criterion_ids.update(item.id for item in criteria)
        lines.append(f"    - {requirement_id}")
        for criterion in criteria:
            writers = tuple(
                task
                for task in implementation.tasks
                if criterion.id in task.acceptance_criteria
                and plan.get_agent(task.owner_agent_id).capability
                in {AgentCapability.IMPLEMENTATION, AgentCapability.INTEGRATION}
            )
            writer_text = ", ".join(
                f"{task.id}->{task.owner_agent_id}" for task in writers
            )
            verifier_text = ", ".join(criterion.verification_agent_ids)
            lines.append(
                f"      - {criterion.id}: writers={writer_text}; "
                f"independent verification={verifier_text}"
            )
    if set(proposal_criteria) != rendered_criterion_ids:
        raise PlanningError("rendered traceability omitted a proposal criterion")
    lines.append("  Implementation approach:")
    for item in implementation.approach:
        lines.extend(_render_prefixed_text("    - ", item))
    lines.append("  Tasks:")
    for task in implementation.tasks:
        task_dependencies = ", ".join(task.dependencies) or "none"
        task_criteria = ", ".join(task.acceptance_criteria)
        task_owner = plan.get_agent(task.owner_agent_id)
        task_authority = (
            "workspace changes permitted within approved scope"
            if task_owner.permission_profile is PermissionProfile.WORKSPACE_WRITE
            else "read-only verification focus; no project changes permitted"
        )
        lines.extend(
            _render_prefixed_text(
                f"    - {task.id} -> {task.owner_agent_id}: ",
                task.description,
            )
        )
        lines.extend(
            (
                f"      authority: {task_authority}",
                f"      acceptance: {task_criteria}",
                f"      dependencies: {task_dependencies}",
            )
        )
    lines.append("  Runtime Agents:")
    resolutions = {
        resolution.agent_id: resolution for resolution in preview.timeout_resolutions
    }
    for agent in plan.agents:
        dependencies = ", ".join(agent.dependencies) or "none"
        route = plan.model_routes.get_route(agent.model_route_id)
        assignment = plan.model_routes.get_assignment(agent.id)
        fallback_routes = tuple(
            plan.model_routes.get_route(route_id)
            for route_id in assignment.fallback_route_ids
        )
        timeout = resolutions[agent.id]
        if timeout.source == "policy_workload":
            timeout_source = f"controller policy from {timeout.workload.value} workload"
        elif timeout.source == "policy_scope_floor":
            boundary_obligations = timeout.scope_boundary_obligation_count or 0
            work_units = (timeout.scope_criterion_count or 0) + boundary_obligations
            timeout_source = (
                "controller review-scope floor for "
                f"{timeout.scope_criterion_count} criteria + "
                f"{boundary_obligations} boundary obligations "
                f"({work_units} work units)"
            )
        elif timeout.source == "user_override":
            timeout_source = "user override"
        else:
            timeout_source = "provider activity"
        minimum_timeout = timeout.minimum_seconds or timeout.default_seconds
        time_boundary = (
            "      time boundary: provider activity liveness; "
            "no per-Agent wall-clock limit"
            if timeout.source == "provider_activity"
            else (
                f"      timeout: {agent.timeout_seconds} seconds ({timeout_source}; "
                f"allowed {minimum_timeout}..{timeout.ceiling_seconds})"
            )
        )
        inputs = (
            "approved TaskBrief and implementation plan"
            if not agent.dependencies
            else "durable outputs from " + ", ".join(agent.dependencies)
        )
        dependents = tuple(
            candidate.id
            for candidate in plan.agents
            if agent.id in candidate.dependencies
        )
        handoff = (
            "Controller terminal decision"
            if not dependents
            else "durable artifact to " + ", ".join(dependents)
        )
        lines.extend(
            _render_prefixed_text(
                f"    - {agent.id} (",
                agent.label + ")",
            )
        )
        lines.extend(
            _render_prefixed_text("      responsibility: ", agent.responsibility)
        )
        lines.extend(_render_prefixed_text("      why: ", agent.rationale))
        lines.extend(
            (
                f"      capability: {agent.capability.value}",
                f"      dependencies: {dependencies}",
                f"      permission: {agent.permission_profile.value}",
                f"      workspace: {agent.workspace_scope}",
                f"      inputs: {inputs}",
                f"      output: {agent.expected_output.value}",
                f"      handoff: {handoff}",
                f"      model: {route.model} (profile {route.id}; "
                f"{assignment.selection_source.value})",
            )
        )
        lines.extend(_render_prefixed_text("      model reason: ", assignment.reason))
        lines.extend(
            (
                "      authorized fallback profiles: "
                + (
                    ", ".join(
                        f"{fallback.id}: {fallback.model} "
                        f"(pricing: {_render_model_pricing(fallback)})"
                        for fallback in fallback_routes
                    )
                    if fallback_routes
                    else "none"
                ),
                f"      model pricing: {_render_model_pricing(route)}",
                f"      workload: {timeout.workload.value}",
                time_boundary,
            )
        )
    waves = " -> ".join(" + ".join(wave) for wave in plan.execution_waves())
    lines.extend(
        (
            "  Controller limits:",
            f"    - execution order: {waves}",
            f"    - maximum parallel Agents: {plan.max_concurrency}",
            f"    - implementation iterations: {plan.iteration_limit}",
            f"    - model routing: {plan.model_routes.mode.value}",
            "    - authorized model switches: "
            + (
                ", ".join(
                    condition.value
                    for condition in plan.model_routes.authorized_switch_conditions
                )
                if plan.model_routes.authorized_switch_conditions
                else "none"
            ),
            (
                f"    - model calls: {plan.budget.max_calls}"
                if plan.budget.max_calls is not None
                else "    - model calls: measured, not capped for this product task"
            ),
            (
                f"    - input tokens: {plan.budget.max_input_tokens}"
                if plan.budget.max_input_tokens is not None
                else "    - input tokens: measured for cost, not independently capped"
            ),
            (
                f"    - output tokens: {plan.budget.max_output_tokens}"
                if plan.budget.max_output_tokens is not None
                else "    - output tokens: measured for cost, not independently capped"
            ),
            (
                "    - cumulative Agent time: "
                f"{plan.budget.max_agent_duration_seconds} seconds"
                if plan.budget.max_agent_duration_seconds is not None
                else "    - cumulative Agent time: observed, not independently capped"
            ),
            f"    - estimated cost ceiling: ${plan.budget.max_estimated_cost_usd}",
            *(
                (
                    "    - recorded Planning spend: "
                    f"${budget_usage.known_estimated_cost_usd:.6f} estimated",
                    "    - recorded budget remaining before execution: "
                    f"${budget_usage.remaining_estimated_cost_usd(plan.budget):.6f}",
                )
                if budget_usage is not None
                else ()
            ),
            (
                "    - absolute billing cap: requires a provider-side spending "
                "or quota limit"
            ),
            (
                f"    - whole-run deadline: {plan.run_deadline_seconds} seconds"
                if plan.run_deadline_seconds is not None
                else "    - whole-run deadline: none"
            ),
            "    - independent downstream quality judgment: required",
        )
    )
    if implementation.risks:
        lines.append("  Risks:")
        for item in implementation.risks:
            lines.extend(_render_prefixed_text("    - ", item))
    else:
        lines.extend(("  Risks:", "    - none identified"))
    lines.extend(
        (
            "  Failure and delivery boundary:",
            "    - failed or cancelled work remains inspectable evidence and is "
            "not delivered",
            "    - only the Controller may accept evidence and publish the "
            "verified workspace",
            "    - destination mutation begins only after the approved-plan "
            "readiness checkpoint",
        )
    )
    return "\n".join(lines)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class PlanningStore:
    """Write-once turns/proposals with an atomic, integrity-checked session index."""

    def __init__(self, root: Path) -> None:
        if not root.is_absolute():
            raise PlanningError("Planning store root must be absolute")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if root.is_symlink() or not root.is_dir():
            raise PlanningError("Planning store root must be a real directory")
        root.chmod(0o700)
        self.root = root

    def _directory(self, run_id: str) -> Path:
        if re.fullmatch(r"[a-z0-9][a-z0-9_-]*", run_id) is None:
            raise PlanningError("Planning run ID is invalid")
        return self.root / run_id

    @staticmethod
    def _indexed_files(directory: Path) -> set[int]:
        if not directory.exists():
            return set()
        if directory.is_symlink() or not directory.is_dir():
            raise PlanningIntegrityError(
                f"Planning evidence path is not a real directory: {directory}"
            )
        indexes: set[int] = set()
        for path in directory.iterdir():
            if path.is_symlink() or not path.is_file():
                raise PlanningIntegrityError(
                    f"Planning evidence entry is not a regular file: {path}"
                )
            match = re.fullmatch(r"([0-9]{3,})\.json", path.name)
            if match is None:
                raise PlanningIntegrityError(
                    f"unexpected Planning evidence file: {path.name}"
                )
            indexes.add(int(match.group(1)))
        return indexes

    def _write_once(self, destination: Path, model: BaseModel) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if destination.exists() or destination.is_symlink():
            raise PlanningIntegrityError(
                f"Planning evidence already exists: {destination}"
            )
        content = (json.dumps(model.model_dump(mode="json"), indent=2) + "\n").encode()
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        _fsync_directory(destination.parent)

    def _write_session(self, session: PlanningSession) -> None:
        directory = self._directory(session.run_id)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = directory / "session.json"
        if destination.is_symlink():
            raise PlanningIntegrityError("Planning session index cannot be a symlink")
        content = (
            json.dumps(session.model_dump(mode="json"), indent=2) + "\n"
        ).encode()
        temporary = directory / f".session.{uuid4().hex}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, destination)
            _fsync_directory(directory)
        finally:
            if temporary.exists():
                temporary.unlink()

    def create(self, request: PlanningRequest) -> PlanningSession:
        directory = self._directory(request.run_id)
        if directory.exists() or directory.is_symlink():
            raise PlanningIntegrityError(
                f"Planning session already exists: {request.run_id}"
            )
        directory.mkdir(parents=True, mode=0o700)
        self._write_once(directory / "request.json", request)
        session = PlanningSession(
            run_id=request.run_id,
            request_sha256=canonical_model_sha256(request),
            status=PlanningSessionStatus.AUTHORIZED,
            created_at=request.authorized_at,
            updated_at=request.authorized_at,
        )
        self._write_session(session)
        return session

    def load_request(self, run_id: str) -> PlanningRequest:
        path = self._directory(run_id) / "request.json"
        if path.is_symlink() or not path.is_file():
            raise PlanningIntegrityError("Planning request evidence is missing")
        request = PlanningRequest.model_validate_json(path.read_text(encoding="utf-8"))
        if request.run_id != run_id:
            raise PlanningIntegrityError("Planning request belongs to a different run")
        return request

    def load_session(self, run_id: str, *, verify: bool = True) -> PlanningSession:
        path = self._directory(run_id) / "session.json"
        if path.is_symlink() or not path.is_file():
            raise PlanningIntegrityError("Planning session index is missing")
        session = PlanningSession.model_validate_json(path.read_text(encoding="utf-8"))
        if session.run_id != run_id:
            raise PlanningIntegrityError("Planning session belongs to a different run")
        if canonical_model_sha256(self.load_request(run_id)) != session.request_sha256:
            raise PlanningIntegrityError("Planning request digest changed")
        if verify:
            turn_indexes = self._indexed_files(self._directory(run_id) / "turns")
            if turn_indexes != set(range(1, session.turn_count + 1)):
                raise PlanningIntegrityError(
                    "Planning turn files differ from the session anchor"
                )
            previous: str | None = None
            for sequence in range(1, session.turn_count + 1):
                turn = self.load_turn(run_id, sequence)
                if turn.previous_sha256 != previous:
                    raise PlanningIntegrityError("Planning turn hash chain is broken")
                previous = canonical_model_sha256(turn)
            if previous != session.turn_head_sha256:
                raise PlanningIntegrityError("Planning turn head digest changed")
            proposal_count = session.latest_proposal_revision or 0
            proposal_indexes = self._indexed_files(
                self._directory(run_id) / "proposals"
            )
            if proposal_indexes != set(range(1, proposal_count + 1)):
                raise PlanningIntegrityError(
                    "Planning proposal files differ from the session anchor"
                )
            for revision in range(1, proposal_count + 1):
                self.load_proposal(run_id, revision)
            approval_indexes = self._indexed_files(
                self._directory(run_id) / "approvals"
            )
            if session.approved_revision is not None:
                if approval_indexes != {session.approved_revision}:
                    raise PlanningIntegrityError(
                        "Planning approval files differ from the session anchor"
                    )
                self.load_approval(run_id, session.approved_revision)
            elif approval_indexes:
                raise PlanningIntegrityError(
                    "unapproved Planning session contains approval evidence"
                )
        return session

    def load_turn(self, run_id: str, sequence: int) -> PlanningTurn:
        path = self._directory(run_id) / "turns" / f"{sequence:03d}.json"
        if path.is_symlink() or not path.is_file():
            raise PlanningIntegrityError(f"Planning turn {sequence} is missing")
        turn = PlanningTurn.model_validate_json(path.read_text(encoding="utf-8"))
        if turn.run_id != run_id or turn.sequence != sequence:
            raise PlanningIntegrityError(
                "Planning turn context does not match its path"
            )
        return turn

    def append_turn(
        self,
        *,
        run_id: str,
        user_message: str,
        prompt: str,
        result: AgentExecutionResult,
        parsed_response: PlanningModelResponse | None,
        response_normalizations: tuple[str, ...],
        validation_error: str | None,
        now: datetime,
        response_validation: ResponseValidationDiagnostic | None = None,
        semantic_correction_request: SemanticCorrectionRequestEvidence | None = None,
        semantic_correction_outcome: SemanticCorrectionOutcome | None = None,
        estimated_cost_usd: Decimal | None = None,
        pricing_source: ModelMetadataSource | None = None,
        budget_usage: AgentBudgetUsage | None = None,
        budget_error: str | None = None,
        cost_record: ModelCallCostRecord | None = None,
    ) -> PlanningTurn:
        session = self.load_session(run_id)
        if session.status in {
            PlanningSessionStatus.APPROVED,
            PlanningSessionStatus.CANCELLED,
        }:
            raise PlanningError("terminal Planning session cannot accept another turn")
        sequence = session.turn_count + 1
        response_text = result.response_text
        usage = result.telemetry.usage
        turn = PlanningTurn(
            run_id=run_id,
            sequence=sequence,
            previous_sha256=session.turn_head_sha256,
            user_message=user_message,
            prompt=prompt,
            prompt_sha256=_digest_text(prompt),
            response_text=response_text,
            response_sha256=(
                None if response_text is None else _digest_text(response_text)
            ),
            submission_payload=(
                None
                if result.semantic_submission is None
                else result.semantic_submission.payload
            ),
            submission_evidence=result.submission_evidence,
            parsed_response=parsed_response,
            response_normalizations=response_normalizations,
            validation_error=validation_error,
            response_validation=response_validation,
            semantic_correction_request=semantic_correction_request,
            semantic_correction_outcome=semantic_correction_outcome,
            execution=PlanningExecutionEvidence(
                status=result.status,
                session_key=result.telemetry.session_key,
                started_at=result.telemetry.started_at,
                finished_at=result.telemetry.finished_at,
                duration_ms=result.telemetry.duration_ms,
                provider=result.telemetry.provider,
                model=result.telemetry.model,
                input_tokens=None if usage is None else usage.input_tokens,
                output_tokens=None if usage is None else usage.output_tokens,
                estimated_cost_usd=estimated_cost_usd,
                pricing_source=pricing_source,
                budget_usage=budget_usage,
                budget_error=budget_error,
                cost_record=cost_record,
                provider_liveness=result.telemetry.provider_liveness,
                error=result.error,
            ),
        )
        self._write_once(
            self._directory(run_id) / "turns" / f"{sequence:03d}.json",
            turn,
        )
        self._write_session(
            session.model_copy(
                update={
                    "status": PlanningSessionStatus.CLARIFYING,
                    "updated_at": _utc(now),
                    "turn_count": sequence,
                    "turn_head_sha256": canonical_model_sha256(turn),
                }
            )
        )
        return turn

    def append_proposal(self, proposal: PlanningProposal, *, now: datetime) -> None:
        session = self.load_session(proposal.run_id)
        expected = (session.latest_proposal_revision or 0) + 1
        if proposal.revision != expected:
            raise PlanningIntegrityError(
                f"proposal revision must be {expected}, got {proposal.revision}"
            )
        if proposal.source_turn_sequence is not None:
            turn = self.load_turn(proposal.run_id, proposal.source_turn_sequence)
            if (
                turn.parsed_response is None
                or turn.parsed_response.kind is not PlanningResponseKind.PROPOSAL
                or turn.parsed_response.proposal != proposal.body
            ):
                raise PlanningIntegrityError("proposal does not match its model turn")
        self._write_once(
            self._directory(proposal.run_id)
            / "proposals"
            / f"{proposal.revision:03d}.json",
            proposal,
        )
        self._write_session(
            session.model_copy(
                update={
                    "status": PlanningSessionStatus.PROPOSED,
                    "updated_at": _utc(now),
                    "latest_proposal_revision": proposal.revision,
                }
            )
        )

    def load_proposal(self, run_id: str, revision: int) -> PlanningProposal:
        path = self._directory(run_id) / "proposals" / f"{revision:03d}.json"
        if path.is_symlink() or not path.is_file():
            raise PlanningIntegrityError(f"Planning proposal {revision} is missing")
        proposal = PlanningProposal.model_validate_json(
            path.read_text(encoding="utf-8")
        )
        if proposal.run_id != run_id or proposal.revision != revision:
            raise PlanningIntegrityError(
                "Planning proposal context does not match its path"
            )
        return proposal

    def approve(self, approval: PlanningApproval, *, now: datetime) -> None:
        session = self.load_session(approval.run_id)
        if session.status is not PlanningSessionStatus.PROPOSED:
            raise PlanningError("only a proposed Planning session can be approved")
        if approval.revision != session.latest_proposal_revision:
            raise PlanningIntegrityError("approval must bind the latest proposal")
        proposal = self.load_proposal(approval.run_id, approval.revision)
        if canonical_model_sha256(proposal) != approval.proposal_sha256:
            raise PlanningIntegrityError("approval proposal digest does not match")
        self._write_once(
            self._directory(approval.run_id)
            / "approvals"
            / f"{approval.revision:03d}.json",
            approval,
        )
        self._write_session(
            session.model_copy(
                update={
                    "status": PlanningSessionStatus.APPROVED,
                    "updated_at": _utc(now),
                    "approved_revision": approval.revision,
                }
            )
        )

    def load_approval(self, run_id: str, revision: int) -> PlanningApproval:
        path = self._directory(run_id) / "approvals" / f"{revision:03d}.json"
        if path.is_symlink() or not path.is_file():
            raise PlanningIntegrityError(f"Planning approval {revision} is missing")
        approval = PlanningApproval.model_validate_json(
            path.read_text(encoding="utf-8")
        )
        if approval.run_id != run_id or approval.revision != revision:
            raise PlanningIntegrityError(
                "Planning approval context does not match its path"
            )
        return approval

    def cancel(self, run_id: str, *, now: datetime) -> None:
        session = self.load_session(run_id)
        if session.status is PlanningSessionStatus.APPROVED:
            raise PlanningError("approved Planning session cannot be cancelled")
        self._write_session(
            session.model_copy(
                update={
                    "status": PlanningSessionStatus.CANCELLED,
                    "updated_at": _utc(now),
                }
            )
        )


@dataclass(frozen=True)
class _Invocation:
    response: PlanningModelResponse
    turn: PlanningTurn


class AdaptivePlanningCoordinator:
    """Run bounded dialogue while retaining approval and lifecycle authority."""

    def __init__(
        self,
        *,
        executor: AgentExecutor,
        store: PlanningStore,
        policy: PlanningPolicy,
        budget_ledger: AgentBudgetLedger | None = None,
        pricing: ModelPricing | None = None,
        route_id: str | None = None,
        clock: Clock = _system_clock,
    ) -> None:
        if (budget_ledger is None) != (pricing is None):
            raise ValueError("Planning budget ledger and pricing belong together")
        if budget_ledger is not None and budget_ledger.budget != policy.budget:
            raise ValueError("Planning budget ledger does not match the policy")
        if (
            budget_ledger is not None
            and budget_ledger.budget.authority is BudgetAuthority.USER_TASK
            and route_id is None
        ):
            raise ValueError("User-task Planning requires an attributable model route")
        self.executor = executor
        self.store = store
        self.policy = policy
        self.budget_ledger = budget_ledger
        self.pricing = pricing
        self.route_id = route_id
        self.clock = clock

    def start(
        self,
        request: PlanningRequest,
        *,
        answer_question: QuestionAnswerer,
        activity_handler: PlanningActivityHandler | None = None,
    ) -> PlanningProposal | None:
        """Ask only high-value questions, then persist one validated proposal."""

        self.store.create(request)
        transcript: list[dict[str, object]] = []
        user_message = request.source_request
        clarification_rounds = 0
        while True:
            invocation = self._invoke(
                request,
                user_message=user_message,
                transcript=transcript,
                current_proposal=None,
                activity_handler=activity_handler,
            )
            response = invocation.response
            if response.kind is PlanningResponseKind.PROPOSAL:
                assert response.proposal is not None
                proposal = PlanningProposal(
                    run_id=request.run_id,
                    revision=1,
                    created_at=_utc(self.clock()),
                    source=PlanningProposalSource.MODEL,
                    source_turn_sequence=invocation.turn.sequence,
                    body=response.proposal,
                )
                self._validate_preview(request, proposal)
                self.store.append_proposal(proposal, now=self.clock())
                return proposal
            assert response.question is not None
            if (
                self.policy.max_clarification_rounds is not None
                and clarification_rounds >= self.policy.max_clarification_rounds
            ):
                raise PlanningError("Planning exceeded its clarification-round limit")
            answer = answer_question(response.question)
            if answer is None:
                self.store.cancel(request.run_id, now=self.clock())
                return None
            answer = _clean_text(answer, label="clarification answer")
            transcript.append(
                {
                    "question": response.question.model_dump(mode="json"),
                    "answer": answer,
                }
            )
            user_message = answer
            clarification_rounds += 1

    def revise(
        self,
        request: PlanningRequest,
        proposal: PlanningProposal,
        change_request: str,
        *,
        answer_question: QuestionAnswerer,
        activity_handler: PlanningActivityHandler | None = None,
    ) -> PlanningProposal | None:
        """Use natural language to produce a complete replacement revision."""

        if (
            self.policy.max_proposal_revisions is not None
            and proposal.revision >= self.policy.max_proposal_revisions
        ):
            raise PlanningError("Planning reached its proposal-revision limit")
        change_request = _clean_text(change_request, label="proposal change request")
        transcript: list[dict[str, object]] = []
        user_message = change_request
        clarification_rounds = 0
        while True:
            invocation = self._invoke(
                request,
                user_message=user_message,
                transcript=transcript,
                current_proposal=proposal,
                change_request=change_request,
                activity_handler=activity_handler,
            )
            response = invocation.response
            if response.kind is PlanningResponseKind.PROPOSAL:
                assert response.proposal is not None
                revision = PlanningProposal(
                    run_id=request.run_id,
                    revision=proposal.revision + 1,
                    created_at=_utc(self.clock()),
                    source=PlanningProposalSource.MODEL,
                    source_turn_sequence=invocation.turn.sequence,
                    change_request=change_request,
                    body=response.proposal,
                )
                self._validate_preview(request, revision)
                self.store.append_proposal(revision, now=self.clock())
                return revision
            assert response.question is not None
            if (
                self.policy.max_clarification_rounds is not None
                and clarification_rounds >= self.policy.max_clarification_rounds
            ):
                raise PlanningError("Planning revision exceeded its question limit")
            answer = answer_question(response.question)
            if answer is None:
                self.store.cancel(request.run_id, now=self.clock())
                return None
            answer = _clean_text(answer, label="clarification answer")
            transcript.append(
                {
                    "question": response.question.model_dump(mode="json"),
                    "answer": answer,
                }
            )
            user_message = answer
            clarification_rounds += 1

    def structured_edit(
        self,
        request: PlanningRequest,
        proposal: PlanningProposal,
        edit: StructuredPlanEdit,
    ) -> PlanningProposal:
        """Validate and persist one safe non-model proposal revision."""

        if (
            self.policy.max_proposal_revisions is not None
            and proposal.revision >= self.policy.max_proposal_revisions
        ):
            raise PlanningError("Planning reached its proposal-revision limit")
        revision = apply_structured_edit(proposal, edit, created_at=self.clock())
        self._validate_preview(request, revision)
        self.store.append_proposal(revision, now=self.clock())
        return revision

    def approve(
        self,
        request: PlanningRequest,
        proposal: PlanningProposal,
    ) -> ApprovedPlanningResult:
        """Freeze the exact validated TeamPlan only after explicit user approval."""

        approved_at = _utc(self.clock())
        preview = preview_adaptive_proposal(
            request,
            proposal,
            self.policy,
            created_at=approved_at,
        )
        approval = PlanningApproval(
            run_id=request.run_id,
            revision=proposal.revision,
            approved_at=approved_at,
            confirmation="user_approved",
            proposal_sha256=canonical_model_sha256(proposal),
            task_brief_sha256=canonical_model_sha256(preview.task_brief),
            implementation_plan_sha256=canonical_model_sha256(
                preview.implementation_plan
            ),
            team_plan_sha256=canonical_model_sha256(preview.team_plan),
            timeout_resolutions=preview.timeout_resolutions,
        )
        self.store.approve(approval, now=approved_at)
        return ApprovedPlanningResult(
            task_brief=preview.task_brief,
            implementation_plan=preview.implementation_plan,
            team_plan=preview.team_plan,
            approval=approval,
        )

    def _validate_preview(
        self,
        request: PlanningRequest,
        proposal: PlanningProposal,
    ) -> PlanningPreview:
        try:
            return preview_adaptive_proposal(
                request,
                proposal,
                self.policy,
                created_at=proposal.created_at,
            )
        except (_PlanningContextInvariantError, _PlanningContextInvariantsError):
            raise
        except _PlanningModelInvariantError as error:
            raise _PlanningContextInvariantError(error.invariant) from error
        except (ValueError, PlanningError) as error:
            raise PlanningError(f"proposed TeamPlan is invalid: {error}") from error

    @staticmethod
    def _question_contracts(
        transcript: list[dict[str, object]],
        current_proposal: PlanningProposal | None,
    ) -> dict[str, _PlanningQuestionContract]:
        """Recover every answered question contract without trusting prose."""

        contracts: dict[str, _PlanningQuestionContract] = {}
        if current_proposal is not None:
            dimensions_by_question: dict[str, list[ProductDefinitionDimension]] = {}
            values_by_question: dict[
                str,
                list[tuple[ProductDefinitionDimension, str]],
            ] = {}
            if current_proposal.body.product_definition is not None:
                for (
                    dimension,
                    item,
                ) in current_proposal.body.product_definition.dimensions():
                    if (
                        item.disposition
                        is ProductDefinitionDisposition.RESOLVED_QUESTION
                    ):
                        dimensions_by_question.setdefault(item.source, []).append(
                            dimension
                        )
                        value = (
                            current_proposal.body.product_definition.delivery_maturity.level.value.replace(
                                "_", " "
                            )
                            if dimension is ProductDefinitionDimension.DELIVERY_MATURITY
                            else item.statement
                        )
                        values_by_question.setdefault(item.source, []).append(
                            (dimension, value)
                        )
            for decision in current_proposal.body.decisions:
                question_id = decision.resolved_question_id
                if question_id is None:
                    continue
                contracts[question_id] = _PlanningQuestionContract(
                    category=decision.category,
                    owner=decision.authority,
                    product_definition_dimensions=tuple(
                        dimensions_by_question.get(question_id, ())
                    ),
                    approved_dimension_values=tuple(
                        values_by_question.get(question_id, ())
                    ),
                )
        for entry in transcript:
            question = PlanningQuestion.model_validate(entry["question"])
            answer = entry.get("answer")
            if not isinstance(answer, str) or not answer.strip():
                raise PlanningError(
                    "persisted Planning transcript has an invalid user answer"
                )
            if question.decision_category is None or question.decision_owner is None:
                raise PlanningError(
                    "persisted Planning transcript has an incomplete question contract"
                )
            if question.id in contracts:
                raise PlanningError(
                    f"Planning question ID was already used: {question.id}"
                )
            contracts[question.id] = _PlanningQuestionContract(
                category=question.decision_category,
                owner=question.decision_owner,
                product_definition_dimensions=(question.product_definition_dimensions),
                answer=answer,
            )
        return contracts

    def _invoke(
        self,
        request: PlanningRequest,
        *,
        user_message: str,
        transcript: list[dict[str, object]],
        current_proposal: PlanningProposal | None,
        change_request: str | None = None,
        activity_handler: PlanningActivityHandler | None = None,
    ) -> _Invocation:
        correction_plan: SemanticCorrectionPlan | None = None
        seen_correction_fingerprints: set[str] = set()
        semantic_corrections = 0
        maximum_attempts = (
            None
            if self.policy.response_repair_limit is None
            else self.policy.response_repair_limit + 1
        )
        attempt = 1
        while True:
            response_schema = (
                _planning_response_schema()
                if correction_plan is None
                else _planning_response_schema_for_correction(correction_plan)
            )
            prompt = self._prompt(
                request,
                transcript=transcript,
                current_proposal=current_proposal,
                change_request=change_request,
                correction_plan=correction_plan,
                response_schema=response_schema,
            )
            submission_contract = AgentSubmissionContract.from_schema(
                (
                    response_schema
                    if correction_plan is None
                    else semantic_correction_schema(
                        correction_plan,
                        response_schema=response_schema,
                    )
                ),
                purpose=(
                    AgentSubmissionPurpose.PLANNING_RESPONSE
                    if correction_plan is None
                    else AgentSubmissionPurpose.SEMANTIC_CORRECTION
                ),
                transport_schema=(
                    _planning_submission_transport_schema()
                    if correction_plan is None
                    else None
                ),
            )
            execution_request = AgentExecutionRequest(
                run_id=request.run_id,
                team_id="adaptive_planning",
                iteration=1,
                role=AgentRole.CLARIFIER,
                expected_kind=ArtifactKind.CLARIFICATION_RECORD,
                prompt=prompt,
                timeout_seconds=self.policy.planning_timeout_seconds,
                model=request.model,
                submission_contract=submission_contract,
            )
            self._emit_activity(
                activity_handler,
                PlanningActivity(
                    kind=PlanningActivityKind.WAITING_MODEL,
                    attempt=attempt,
                    maximum_attempts=maximum_attempts,
                    model=request.model,
                ),
            )
            reservation = (
                None
                if self.budget_ledger is None
                else self.budget_ledger.reserve_call(
                    "clarifier",
                    run_id=request.run_id,
                    stage="planning",
                    attempt=attempt,
                    route_id=self.route_id,
                    pricing=self.pricing,
                )
            )

            def observe_execution_activity(
                execution_activity: AgentExecutionActivity,
                *,
                current_attempt: int = attempt,
            ) -> None:
                self._emit_execution_activity(
                    activity_handler,
                    execution_activity,
                    attempt=current_attempt,
                    maximum_attempts=maximum_attempts,
                    model=request.model,
                )

            try:
                result = self.executor.execute(
                    execution_request,
                    activity_handler=observe_execution_activity,
                )
            except BaseException:
                if self.budget_ledger is not None and reservation is not None:
                    with suppress(AgentBudgetExceeded):
                        self.budget_ledger.complete_call(
                            reservation,
                            input_tokens=None,
                            output_tokens=None,
                            duration_ms=0,
                        )
                raise
            self._emit_activity(
                activity_handler,
                PlanningActivity(
                    kind=PlanningActivityKind.RESPONSE_RECEIVED,
                    attempt=attempt,
                    maximum_attempts=maximum_attempts,
                    model=request.model,
                    duration_ms=result.telemetry.duration_ms,
                    execution_status=result.status,
                ),
            )
            parsed: PlanningModelResponse | None = None
            response_normalizations: tuple[str, ...] = ()
            correction_binding_normalizations: tuple[str, ...] = ()
            validation_error: str | None = None
            response_validation: ResponseValidationDiagnostic | None = None
            correction_request = (
                None if correction_plan is None else correction_plan.evidence
            )
            current_correction_outcome = (
                None
                if correction_plan is None
                else SemanticCorrectionOutcome.NOT_EVALUATED
            )
            correction_applied = correction_plan is None
            next_correction_plan: SemanticCorrectionPlan | None = None
            estimated_cost: Decimal | None = None
            budget_usage: AgentBudgetUsage | None = None
            budget_error: str | None = None
            cost_record: ModelCallCostRecord | None = None
            if self.budget_ledger is not None and reservation is not None:
                assert self.pricing is not None
                usage = result.telemetry.usage
                if (
                    usage is not None
                    and usage.input_tokens is not None
                    and usage.output_tokens is not None
                ):
                    estimated_cost = reservation.estimate_cost(
                        input_tokens=usage.input_tokens,
                        output_tokens=usage.output_tokens,
                        cache_usage=usage.cache_usage,
                    )
                try:
                    budget_usage = self.budget_ledger.complete_call(
                        reservation,
                        input_tokens=None if usage is None else usage.input_tokens,
                        output_tokens=None if usage is None else usage.output_tokens,
                        duration_ms=result.telemetry.duration_ms,
                        cache_usage=CacheTokenUsage()
                        if usage is None
                        else usage.cache_usage,
                    )
                except AgentBudgetExceeded as error:
                    budget_usage = error.usage
                    budget_error = str(error)
                cost_record = next(
                    record
                    for record in self.budget_ledger.call_records()
                    if record.sequence == reservation.sequence
                )
                self._emit_activity(
                    activity_handler,
                    PlanningActivity(
                        kind=PlanningActivityKind.BUDGET_UPDATED,
                        attempt=attempt,
                        maximum_attempts=maximum_attempts,
                        model=request.model,
                        budget_usage=budget_usage,
                        budget_ceiling_usd=(
                            self.budget_ledger.budget.max_estimated_cost_usd
                        ),
                        pricing_source=self.pricing.pricing_source,
                    ),
                )
            if result.status is not AgentExecutionStatus.COMPLETED:
                validation_error = (
                    result.error or f"Planning execution ended as {result.status.value}"
                )
            else:
                payload: dict[str, object] | None = None
                try:
                    semantic_submission = result.semantic_submission
                    if semantic_submission is None:
                        raise PlanningError(
                            "Planning execution omitted its required typed submission"
                        )
                    if (
                        semantic_submission.evidence.purpose
                        is not submission_contract.purpose
                        or semantic_submission.evidence.schema_sha256
                        != submission_contract.schema_sha256
                    ):
                        raise PlanningError(
                            "Planning typed submission differs from its invocation "
                            "contract"
                        )
                    payload = dict(semantic_submission.payload)
                    if correction_plan is not None:
                        application = apply_semantic_correction_with_evidence(
                            payload,
                            correction_plan,
                        )
                        payload = application.payload
                        correction_binding_normalizations = application.normalizations
                        correction_applied = True
                    payload, initial_normalizations = (
                        _normalize_planning_response_payload(
                            payload,
                            profile_criterion_ids=(
                                criterion.id
                                for criterion in self.policy.profile_acceptance_criteria
                            ),
                            user_inputs=(
                                request.source_request,
                                *(() if change_request is None else (change_request,)),
                            ),
                        )
                    )
                    normalization_list = [
                        *correction_binding_normalizations,
                        *initial_normalizations,
                    ]
                    response_normalizations = tuple(normalization_list)
                    _validate_current_planning_response_wire(
                        payload,
                        response_schema=response_schema,
                    )
                    while True:
                        try:
                            parsed = PlanningModelResponse.model_validate(payload)
                            break
                        except ValidationError as error:
                            if any(
                                issue["loc"] and issue["loc"][0] == "question"
                                for issue in error.errors(
                                    include_url=False,
                                    include_context=False,
                                    include_input=False,
                                )
                            ):
                                raise
                            normalized, removed = (
                                deterministically_remove_forbidden_fields(
                                    payload,
                                    error,
                                )
                            )
                            if not removed:
                                raise
                            payload = normalized
                            normalization_list.extend(
                                f"removed schema-forbidden field {path}"
                                for path in removed
                            )
                            response_normalizations = tuple(normalization_list)
                    response_normalizations = tuple(normalization_list)
                    question_contracts = self._question_contracts(
                        transcript,
                        current_proposal,
                    )
                    if parsed.kind is PlanningResponseKind.QUESTION:
                        assert parsed.question is not None
                        validate_question_admission(
                            parsed.question,
                            previous_question_ids=question_contracts,
                        )
                    else:
                        assert parsed.proposal is not None
                        validate_planning_clarity(
                            parsed.proposal,
                            source_request=request.source_request,
                            additional_user_inputs=(
                                () if change_request is None else (change_request,)
                            ),
                            question_contracts=question_contracts,
                            allowed_criterion_ids=(
                                criterion.id
                                for criterion in self.policy.profile_acceptance_criteria
                            ),
                        )
                        candidate = PlanningProposal(
                            run_id=request.run_id,
                            revision=(
                                1
                                if current_proposal is None
                                else current_proposal.revision + 1
                            ),
                            created_at=_utc(self.clock()),
                            source=PlanningProposalSource.MODEL,
                            source_turn_sequence=1,
                            change_request=change_request,
                            body=parsed.proposal,
                        )
                        self._validate_preview(request, candidate)
                except AgentArtifactResponseError as error:
                    validation_error = _safe_validation_detail(error)
                    response_validation = error.diagnostic
                    if correction_plan is not None:
                        current_correction_outcome = (
                            SemanticCorrectionOutcome.INVALID_SUBMISSION
                        )
                    parsed = None
                except ValidationError as error:
                    validation_error = _safe_validation_detail(error)
                    assert payload is not None
                    response_validation = _planning_validation_diagnostic(
                        error,
                        payload,
                    )
                    if correction_plan is not None and not correction_applied:
                        current_correction_outcome = (
                            SemanticCorrectionOutcome.INVALID_SUBMISSION
                        )
                    parsed = None
                except _PlanningContextInvariantsError as error:
                    validation_error = _safe_validation_detail(error)
                    if payload is not None:
                        response_validation = _planning_invariants_diagnostic(
                            payload,
                            error.invariants,
                        )
                    if correction_plan is not None and not correction_applied:
                        current_correction_outcome = (
                            SemanticCorrectionOutcome.INVALID_SUBMISSION
                        )
                    parsed = None
                except _PlanningContextInvariantError as error:
                    validation_error = _safe_validation_detail(error)
                    if payload is not None:
                        response_validation = _planning_invariant_diagnostic(
                            payload,
                            error.invariant,
                            authority=error.authority,
                            failure_class=error.failure_class,
                        )
                    if correction_plan is not None and not correction_applied:
                        current_correction_outcome = (
                            SemanticCorrectionOutcome.INVALID_SUBMISSION
                        )
                    parsed = None
                except _PlanningModelInvariantError as error:
                    validation_error = _safe_validation_detail(error)
                    if payload is not None:
                        response_validation = _planning_invariant_diagnostic(
                            payload,
                            error.invariant,
                        )
                    if correction_plan is not None and not correction_applied:
                        current_correction_outcome = (
                            SemanticCorrectionOutcome.INVALID_SUBMISSION
                        )
                    parsed = None
                except (PlanningError, ValueError) as error:
                    validation_error = _safe_validation_detail(error)
                    if payload is not None:
                        response_validation = diagnostic_from_invariant(
                            payload,
                            failure_class=ResponseFailureClass.SEMANTIC_CONTEXT,
                            authority=ResponseIssueAuthority.CONTROLLER,
                            code="planning_context_unclassified",
                            invariant_id="planning_context_unclassified",
                            subjects=(),
                            message=validation_error,
                            paths=("/",),
                        )
                    else:
                        response_validation = diagnostic_from_transport(
                            result.response_text or "",
                            code="planning_typed_submission_missing",
                            message=validation_error,
                        )
                    if correction_plan is not None and not correction_applied:
                        current_correction_outcome = (
                            SemanticCorrectionOutcome.INVALID_SUBMISSION
                        )
                    parsed = None

                if parsed is None and response_validation is not None:
                    if correction_plan is None:
                        if payload is not None:
                            next_correction_plan = build_semantic_correction_plan(
                                payload,
                                response_validation,
                            )
                        seen_correction_fingerprints.add(
                            response_validation.fingerprint
                        )
                    elif correction_applied:
                        current_correction_outcome = correction_outcome(
                            correction_plan,
                            response_validation,
                            seen_fingerprints=frozenset(seen_correction_fingerprints),
                        )
                        if (
                            current_correction_outcome
                            is SemanticCorrectionOutcome.IMPROVED
                            and payload is not None
                        ):
                            next_correction_plan = build_semantic_correction_plan(
                                payload,
                                response_validation,
                            )
                            seen_correction_fingerprints.add(
                                response_validation.fingerprint
                            )
                elif parsed is not None and correction_plan is not None:
                    current_correction_outcome = SemanticCorrectionOutcome.ACCEPTED
            turn = self.store.append_turn(
                run_id=request.run_id,
                user_message=user_message,
                prompt=prompt,
                result=result,
                parsed_response=parsed,
                response_normalizations=response_normalizations,
                validation_error=validation_error,
                response_validation=response_validation,
                semantic_correction_request=correction_request,
                semantic_correction_outcome=current_correction_outcome,
                now=self.clock(),
                estimated_cost_usd=estimated_cost,
                pricing_source=(
                    None if self.pricing is None else self.pricing.pricing_source
                ),
                budget_usage=budget_usage,
                budget_error=budget_error,
                cost_record=cost_record,
            )
            if budget_error is not None:
                raise PlanningError(budget_error)
            if parsed is not None:
                self._emit_activity(
                    activity_handler,
                    PlanningActivity(
                        kind=PlanningActivityKind.RESPONSE_VALIDATED,
                        attempt=attempt,
                        maximum_attempts=maximum_attempts,
                        model=request.model,
                    ),
                )
                return _Invocation(response=parsed, turn=turn)
            correction_allowed = next_correction_plan is not None and (
                self.policy.response_repair_limit is None
                or semantic_corrections < self.policy.response_repair_limit
            )
            if not correction_allowed:
                raise PlanningError(
                    f"Planning response remained invalid: {validation_error}"
                )
            semantic_corrections += 1
            correction_plan = next_correction_plan
            self._emit_activity(
                activity_handler,
                PlanningActivity(
                    kind=PlanningActivityKind.CORRECTION_SCHEDULED,
                    attempt=attempt,
                    maximum_attempts=maximum_attempts,
                    model=request.model,
                ),
            )
            attempt += 1

    @staticmethod
    def _emit_activity(
        handler: PlanningActivityHandler | None,
        activity: PlanningActivity,
    ) -> None:
        if handler is None:
            return
        try:
            handler(activity)
        except Exception:
            # Ephemeral terminal rendering cannot change persisted Planning.
            return

    @classmethod
    def _emit_execution_activity(
        cls,
        handler: PlanningActivityHandler | None,
        activity: AgentExecutionActivity,
        *,
        attempt: int,
        maximum_attempts: int | None,
        model: str,
    ) -> None:
        kind = {
            AgentExecutionActivityKind.INVOCATION_LAUNCHED: (
                PlanningActivityKind.INVOCATION_LAUNCHED
            ),
            AgentExecutionActivityKind.INVOCATION_INITIALIZING: (
                PlanningActivityKind.INITIALIZING
            ),
            AgentExecutionActivityKind.INITIALIZATION_PROGRESS: (
                PlanningActivityKind.INITIALIZATION_PROGRESS
            ),
            AgentExecutionActivityKind.INITIALIZATION_LIVENESS_DEGRADED: (
                PlanningActivityKind.INITIALIZATION_LIVENESS_DEGRADED
            ),
            AgentExecutionActivityKind.INITIALIZATION_STALL_SUSPECTED: (
                PlanningActivityKind.INITIALIZATION_STALL_SUSPECTED
            ),
            AgentExecutionActivityKind.INITIALIZATION_STALL_RECOVERED: (
                PlanningActivityKind.INITIALIZATION_STALL_RECOVERED
            ),
            AgentExecutionActivityKind.INITIALIZATION_STALLED: (
                PlanningActivityKind.INITIALIZATION_STALLED
            ),
            AgentExecutionActivityKind.INVOCATION_PROVIDER_WAIT: (
                PlanningActivityKind.PROVIDER_WAIT
            ),
            AgentExecutionActivityKind.INVOCATION_TOOL_ACTIVE: (
                PlanningActivityKind.TOOL_ACTIVE
            ),
            AgentExecutionActivityKind.INVOCATION_FINALIZING_RESPONSE: (
                PlanningActivityKind.FINALIZING_RESPONSE
            ),
            AgentExecutionActivityKind.INVOCATION_STOPPING: (
                PlanningActivityKind.STOPPING
            ),
            AgentExecutionActivityKind.INVOCATION_COLLECTING_EVIDENCE: (
                PlanningActivityKind.COLLECTING_EVIDENCE
            ),
            AgentExecutionActivityKind.INVOCATION_STOPPED: (
                PlanningActivityKind.STOPPED
            ),
            AgentExecutionActivityKind.PROVIDER_STREAM: (
                PlanningActivityKind.PROVIDER_ACTIVITY
            ),
            AgentExecutionActivityKind.TOOL_STARTED: PlanningActivityKind.TOOL_STARTED,
            AgentExecutionActivityKind.TOOL_COMPLETED: (
                PlanningActivityKind.TOOL_COMPLETED
            ),
            AgentExecutionActivityKind.LIVENESS_DEGRADED: (
                PlanningActivityKind.LIVENESS_DEGRADED
            ),
            AgentExecutionActivityKind.STALL_SUSPECTED: (
                PlanningActivityKind.STALL_SUSPECTED
            ),
            AgentExecutionActivityKind.STALL_RECOVERED: (
                PlanningActivityKind.STALL_RECOVERED
            ),
            AgentExecutionActivityKind.PROVIDER_STALLED: (
                PlanningActivityKind.PROVIDER_STALLED
            ),
            AgentExecutionActivityKind.FINALIZATION_PROGRESS: (
                PlanningActivityKind.FINALIZATION_PROGRESS
            ),
            AgentExecutionActivityKind.FINALIZATION_STALL_SUSPECTED: (
                PlanningActivityKind.FINALIZATION_STALL_SUSPECTED
            ),
            AgentExecutionActivityKind.FINALIZATION_STALL_RECOVERED: (
                PlanningActivityKind.FINALIZATION_STALL_RECOVERED
            ),
            AgentExecutionActivityKind.RESPONSE_FINALIZATION_STALLED: (
                PlanningActivityKind.RESPONSE_FINALIZATION_STALLED
            ),
        }[activity.kind]
        cls._emit_activity(
            handler,
            PlanningActivity(
                kind=kind,
                attempt=attempt,
                maximum_attempts=maximum_attempts,
                model=model,
                duration_ms=activity.elapsed_ms,
                inactivity_ms=activity.inactivity_ms,
                silence_seconds=activity.silence_seconds,
                stall_grace_seconds=activity.stall_grace_seconds,
                policy_source=activity.policy_source,
                degradation_reason=activity.degradation_reason,
                invocation_phase=activity.invocation_phase,
                stop_reason=activity.stop_reason,
                initialization_checkpoint=activity.initialization_checkpoint,
                shutdown_grace_seconds=activity.shutdown_grace_seconds,
                action=activity.action,
                tool_action_class=activity.tool_action_class,
                tool_target_class=activity.tool_target_class,
                tool_detail=activity.tool_detail,
            ),
        )

    def _prompt(
        self,
        request: PlanningRequest,
        *,
        transcript: list[dict[str, object]],
        current_proposal: PlanningProposal | None,
        change_request: str | None,
        correction_plan: SemanticCorrectionPlan | None,
        response_schema: dict[str, object],
    ) -> str:
        template = Template(PLANNING_TEMPLATE.read_text(encoding="utf-8"))
        context = {
            "request": _planning_request_prompt_context(request),
            "dialogue": transcript,
            "current_proposal": (
                None
                if current_proposal is None
                else _planning_proposal_body_for_model(current_proposal.body)
            ),
            "change_request": change_request,
            "controller_policy": {
                "maximum_agents": self.policy.max_agents,
                "maximum_concurrency": self.policy.max_concurrency,
                "maximum_iterations": self.policy.max_iterations,
                "maximum_agent_calls": self.policy.budget.max_calls,
                "maximum_estimated_cost_usd": str(
                    self.policy.budget.max_estimated_cost_usd
                ),
                "run_deadline_seconds": self.policy.run_deadline_seconds,
                "maximum_review_agents": self.policy.max_review_agents,
                "profile_acceptance_criteria": [
                    criterion.model_dump(mode="json")
                    for criterion in self.policy.profile_acceptance_criteria
                ],
                "review_boundary_definitions": review_boundary_definition_map(),
                "requires_independent_review_agent": (self.policy.require_review_agent),
                "capability_timeout_profiles": {
                    capability.value: {
                        "routine_seconds": timeout.default_seconds,
                        "substantial_seconds": timeout.resolve(
                            AgentWorkload.SUBSTANTIAL
                        ),
                        "complex_seconds": timeout.ceiling_seconds,
                    }
                    for capability, timeout in self.policy.capability_timeouts.items()
                },
                "review_scope_timeout_floor": {
                    "work_unit_definition": (
                        "one unit per criterion plus one unit per explicit "
                        "Review boundary obligation"
                    ),
                    "routine_below_work_units": (
                        self.policy.review_substantial_work_unit_threshold
                    ),
                    "substantial_from_work_units": (
                        self.policy.review_substantial_work_unit_threshold
                    ),
                    "complex_from_work_units": (
                        self.policy.review_complex_work_unit_threshold
                    ),
                    "instruction": (
                        "The controller may raise Reviewer timeout from exact "
                        "criterion and boundary scope; the Planner still proposes "
                        "only workload."
                    ),
                },
                "runtime_capabilities": [
                    AgentCapability.IMPLEMENTATION.value,
                    AgentCapability.INTEGRATION.value,
                    AgentCapability.TESTING.value,
                    AgentCapability.REVIEW.value,
                ],
                "model_routing": (
                    {
                        "mode": self.policy.model_routing.mode.value,
                        "profiles": [
                            {
                                "id": profile.id,
                                "model": profile.model,
                                "eligible_agent_capabilities": [
                                    capability.value
                                    for capability in profile.capabilities
                                ],
                            }
                            for profile in self.policy.model_routing.profiles
                        ],
                        "instruction": (
                            "Describe capability and workload needs only. The "
                            "controller resolves model profiles; do not add model "
                            "fields to the proposal."
                        ),
                    }
                    if self.policy.model_routing is not None
                    else {
                        "mode": ModelRoutingMode.STRICT.value,
                        "instruction": "All runtime Agents use the pinned model.",
                    }
                ),
            },
        }
        rendered = template.substitute(
            planning_context_json=json.dumps(context, ensure_ascii=False, indent=2),
            response_schema_json=json.dumps(
                response_schema,
                ensure_ascii=False,
                indent=2,
            ),
            repair_context_json="null",
            submission_tool=ARTIFACT_SUBMISSION_TOOL,
        )
        if correction_plan is not None:
            rendered += correction_prompt(
                correction_plan,
                submission_tool=ARTIFACT_SUBMISSION_TOOL,
                response_schema=response_schema,
            )
        return rendered


def _interactive_question_answerer(
    *,
    read: InputReader,
    write: OutputWriter,
) -> QuestionAnswerer:
    def answer(question: PlanningQuestion) -> str | None:
        write("")
        write(f"Planning question: {question.text}")
        assert question.decision_category is not None
        assert question.decision_owner is not None
        write(
            "Decision boundary: "
            f"{question.decision_category.value} / {question.decision_owner.value}"
        )
        if question.product_definition_dimensions:
            write(
                "Product definition affected: "
                + ", ".join(
                    dimension.value
                    for dimension in question.product_definition_dimensions
                )
            )
        write(f"Why this matters: {question.why}")
        write("Missing evidence:")
        for item in question.missing_evidence:
            write(f"  - {item}")
        write("What this can change:")
        for item in question.material_consequences:
            write(f"  - {item}")
        for index, option in enumerate(question.options, start=1):
            write(f"  {index}. {option.label} — {option.description}")
        write("  c. Custom answer")
        write("  x. Cancel")
        while True:
            choice = read("Choose an option or enter a custom answer: ").strip()
            if choice.casefold() in {"x", "cancel"}:
                return None
            if choice.casefold() in {"c", "custom"}:
                custom = read("Your answer: ").strip()
                if custom:
                    return custom
                write("Please enter a non-empty answer.")
                continue
            if choice.isdigit() and 1 <= int(choice) <= len(question.options):
                selected = question.options[int(choice) - 1]
                return f"{selected.label}: {selected.description}"
            if choice:
                return choice
            write("Please choose a suggestion, enter a custom answer, or cancel.")

    return answer


def _read_positive_integer(
    prompt: str,
    *,
    read: InputReader,
    write: OutputWriter,
) -> int | None:
    while True:
        value = read(prompt).strip()
        if value.casefold() in {"x", "cancel"}:
            return None
        if value.isdigit() and int(value) > 0:
            return int(value)
        write("Enter a positive integer, or x to return to the overview.")


def _read_structured_edit(
    proposal: PlanningProposal,
    *,
    model_routing: ModelRoutingPolicy | None,
    read: InputReader,
    write: OutputWriter,
) -> StructuredPlanEdit | None:
    allow_model_edit = model_routing is not None and len(model_routing.profiles) > 1
    write("")
    write("Safe plan edits")
    write("  1. Maximum parallel Agents")
    write("  2. Implementation iteration limit")
    if allow_model_edit:
        write("  3. One Agent model profile")
    write("  x. Return to overview")
    while True:
        choice = read("Edit: ").strip().casefold()
        if choice in {"x", "cancel"}:
            return None
        if choice == "1":
            value = _read_positive_integer(
                "Maximum parallel Agents: ", read=read, write=write
            )
            return (
                None
                if value is None
                else StructuredPlanEdit(
                    kind=StructuredEditKind.MAX_CONCURRENCY,
                    value=value,
                )
            )
        if choice == "2":
            value = _read_positive_integer(
                "Implementation iterations: ", read=read, write=write
            )
            return (
                None
                if value is None
                else StructuredPlanEdit(
                    kind=StructuredEditKind.ITERATION_LIMIT,
                    value=value,
                )
            )
        if choice == "3" and allow_model_edit:
            assert model_routing is not None
            write("Runtime Agents:")
            for agent in proposal.body.agents:
                write(f"  - {agent.id}: {agent.capability.value}")
            agent_id = read("Agent ID: ").strip()
            if not agent_id:
                write("Agent ID must not be blank.")
                continue
            write("Configured model profiles:")
            for profile in model_routing.profiles:
                capabilities = ", ".join(
                    capability.value for capability in profile.capabilities
                )
                write(f"  - {profile.id}: {profile.model} ({capabilities})")
            profile_id = read("Model profile ID: ").strip()
            if not profile_id:
                write("Model profile ID must not be blank.")
                continue
            return StructuredPlanEdit(
                kind=StructuredEditKind.AGENT_MODEL,
                agent_id=agent_id,
                value=profile_id,
            )
        choices = "1, 2, 3, or x" if allow_model_edit else "1, 2, or x"
        write(f"Choose {choices}.")


def run_interactive_planning(
    coordinator: AdaptivePlanningCoordinator,
    request: PlanningRequest,
    *,
    read: InputReader = input,
    write: OutputWriter = print,
) -> ApprovedPlanningResult | None:
    """Run the user-facing clarification, overview, revision, and approval loop."""

    answer_question = _interactive_question_answerer(read=read, write=write)
    progress = TerminalPlanningProgress(write=write)
    write("")
    write("Planning started. No runtime Agent has been created yet.")
    try:
        proposal = coordinator.start(
            request,
            answer_question=answer_question,
            activity_handler=progress,
        )
    finally:
        progress.close()
    if proposal is None:
        write("Planning cancelled; no runtime Agent was created.")
        return None

    while True:
        preview = preview_adaptive_proposal(
            request,
            proposal,
            coordinator.policy,
            created_at=proposal.created_at,
        )
        write("")
        write(
            render_planning_overview(
                preview,
                budget_usage=(
                    None
                    if coordinator.budget_ledger is None
                    else coordinator.budget_ledger.snapshot()
                ),
            )
        )
        write("")
        write("  a. Approve and allow the controller to create this team")
        write("  r. Request changes in your own words")
        write("  e. Edit safe limits")
        write("  c. Cancel")
        choice = read("Review choice: ").strip().casefold()
        if choice in {"a", "approve"}:
            approved = coordinator.approve(request, proposal)
            write(
                f"Plan revision {approved.approval.revision} approved. "
                "The controller may now create only the Agents shown above."
            )
            return approved
        if choice in {"c", "cancel"}:
            coordinator.store.cancel(request.run_id, now=coordinator.clock())
            write("Planning cancelled; no runtime Agent was created.")
            return None
        if choice in {"r", "revise"}:
            change = read("Describe the changes you want: ").strip()
            if not change:
                write("Change request must not be blank.")
                continue
            try:
                revised = coordinator.revise(
                    request,
                    proposal,
                    change,
                    answer_question=answer_question,
                    activity_handler=progress,
                )
            except PlanningError as error:
                write(f"Plan was not changed: {error}")
                continue
            finally:
                progress.close()
            if revised is None:
                write("Planning cancelled; no runtime Agent was created.")
                return None
            proposal = revised
            continue
        if choice in {"e", "edit"}:
            try:
                edit = _read_structured_edit(
                    proposal,
                    model_routing=coordinator.policy.model_routing,
                    read=read,
                    write=write,
                )
                if edit is not None:
                    proposal = coordinator.structured_edit(request, proposal, edit)
            except (PlanningError, ValidationError, ValueError) as error:
                write(f"Plan was not changed: {_safe_validation_detail(error)}")
            continue
        write("Choose a, r, e, or c.")
