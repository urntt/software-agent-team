"""Adaptive Planning dialogue, proposal validation, approval, and evidence."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from collections.abc import Callable, Collection, Mapping
from contextlib import suppress
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
    ValidationError,
    field_validator,
    model_validator,
)

from software_agent_team.artifacts import (
    AcceptanceCriterion,
    AgentRole,
    ArtifactKind,
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
    ModelPricing,
)
from software_agent_team.execution import (
    AgentExecutionActivity,
    AgentExecutionActivityKind,
    AgentExecutionRequest,
    AgentExecutionResult,
    AgentExecutionStatus,
    AgentExecutor,
)
from software_agent_team.integrity import canonical_model_sha256
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
    ResponseValidationDiagnostic,
    SemanticCorrectionOutcome,
    SemanticCorrectionPlan,
    SemanticCorrectionRequestEvidence,
    apply_semantic_correction,
    build_semantic_correction_plan,
    correction_outcome,
    correction_prompt,
    deterministically_remove_forbidden_fields,
    diagnostic_from_message,
    diagnostic_from_validation_error,
)
from software_agent_team.responses import (
    AgentArtifactResponseError,
    parse_json_object_response,
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

PLANNING_SCHEMA_VERSION = 4
MINIMUM_READABLE_PLANNING_SCHEMA_VERSION = 2
PLANNING_TEMPLATE = Path(__file__).with_name("prompt_templates") / "adaptive_planner.md"
MAX_PLANNING_EVIDENCE_CHARACTERS = 1_000_000
MAX_RESPONSE_NORMALIZATIONS = 100
MAX_RESPONSE_NORMALIZATION_CHARACTERS = 200


class PlanningError(RuntimeError):
    """Raised when an adaptive Planning session cannot continue safely."""


class PlanningIntegrityError(PlanningError):
    """Raised when persisted Planning evidence is incomplete or changed."""


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
    PROVIDER_ACTIVITY = "provider_activity"
    TOOL_STARTED = "tool_started"
    TOOL_COMPLETED = "tool_completed"
    LIVENESS_DEGRADED = "liveness_degraded"
    STALL_SUSPECTED = "stall_suspected"
    STALL_RECOVERED = "stall_recovered"
    PROVIDER_STALLED = "provider_stalled"
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
            self.close()
            attempt_label = (
                str(activity.attempt)
                if activity.maximum_attempts is None
                else f"{activity.attempt}/{activity.maximum_attempts}"
            )
            self._print(
                f"● Planning is waiting for {activity.model} (attempt {attempt_label})"
            )
            stop = threading.Event()
            started = self.monotonic()
            thread = threading.Thread(
                target=self._heartbeat,
                args=(stop, started, activity),
                name="sat-planning-progress",
                daemon=True,
            )
            with self._lock:
                self._waiting = (stop, thread)
            thread.start()
            return

        intermediate = {
            PlanningActivityKind.PROVIDER_ACTIVITY: (
                "  Planning received provider stream activity"
            ),
            PlanningActivityKind.TOOL_STARTED: (
                "  Planning started a sandboxed tool operation"
            ),
            PlanningActivityKind.TOOL_COMPLETED: (
                "  Planning completed a sandboxed tool operation"
            ),
            PlanningActivityKind.LIVENESS_DEGRADED: (
                "! Planning provider liveness is degraded: "
                f"{activity.degradation_reason}; SAT will preserve the call "
                "instead of inferring a stall from silence"
            ),
            PlanningActivityKind.STALL_SUSPECTED: (
                "? Planning has produced no trusted activity for "
                f"{(activity.inactivity_ms or 0) / 1000:.1f}s; checking the "
                "private stream and attributable tool state for another "
                f"{(activity.stall_grace_seconds or 0):g}s before interruption "
                f"({activity.policy_source})"
            ),
            PlanningActivityKind.STALL_RECOVERED: (
                "↻ Planning provider activity recovered during the "
                f"{(activity.stall_grace_seconds or 0):g}s grace period"
            ),
            PlanningActivityKind.PROVIDER_STALLED: (
                "✗ Planning provider remained silent for "
                f"{(activity.silence_seconds or 0):g}s; interrupting only this "
                "invocation "
                "and preserving its evidence"
            ),
        }.get(activity.kind)
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

    def _heartbeat(
        self,
        stop: threading.Event,
        started: float,
        activity: PlanningActivity,
    ) -> None:
        while not stop.wait(self.heartbeat_seconds):
            elapsed = max(0, int(self.monotonic() - started))
            minutes, seconds = divmod(elapsed, 60)
            attempt_label = (
                str(activity.attempt)
                if activity.maximum_attempts is None
                else f"{activity.attempt}/{activity.maximum_attempts}"
            )
            self._print(
                "  Planning is still waiting for the model "
                f"(attempt {attempt_label}): {minutes:02d}:{seconds:02d} elapsed"
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

    proposal = normalized.get("proposal")
    if not isinstance(proposal, dict):
        return normalized, tuple(changes)
    acceptance_criteria = proposal.get("acceptance_criteria")
    controller_owned_ids = set(profile_criterion_ids)
    if isinstance(acceptance_criteria, list) and controller_owned_ids:
        retained_criteria: list[object] = []
        for criterion_index, criterion in enumerate(acceptance_criteria):
            criterion_id = criterion.get("id") if isinstance(criterion, dict) else None
            if isinstance(criterion_id, str) and criterion_id in controller_owned_ids:
                changes.append(
                    "removed controller-owned profile criterion "
                    f"{criterion_id} from proposal.acceptance_criteria"
                    f"[{criterion_index}]"
                )
                continue
            retained_criteria.append(criterion)
        proposal["acceptance_criteria"] = retained_criteria
    tasks = proposal.get("tasks")
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
    agents = proposal.get("agents")
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


def _planning_context_paths(
    detail: str,
    parsed: PlanningModelResponse | None,
) -> tuple[str, ...]:
    """Map controller post-schema checks to the smallest useful model fields."""

    lowered = detail.casefold()
    if parsed is not None and parsed.kind is PlanningResponseKind.QUESTION:
        if "category" in lowered or "owner" in lowered:
            return ("/question/decision_category", "/question/decision_owner")
        if "missing evidence" in lowered:
            return ("/question/missing_evidence",)
        if "material consequence" in lowered:
            return ("/question/material_consequences",)
        if "already used" in lowered:
            return ("/question/id",)
        return ("/question",)
    if "assumption" in lowered:
        return (
            "/proposal/assumption_decision_ids",
            "/proposal/assumptions",
            "/proposal/decisions",
        )
    if "decision" in lowered or "question" in lowered or "provenance" in lowered:
        return ("/proposal/decisions",)
    if "criterion" in lowered or "acceptance" in lowered or "requirement" in lowered:
        return (
            "/proposal/acceptance_criteria",
            "/proposal/requirement_ids",
            "/proposal/requirements",
            "/proposal/tasks",
        )
    if "concurrency" in lowered:
        return ("/proposal/max_concurrency",)
    if "iteration" in lowered or "revision" in lowered:
        return ("/proposal/iteration_limit", "/proposal/revision_enabled")
    if "agent" in lowered or "task" in lowered or "workspace" in lowered:
        return ("/proposal/agents", "/proposal/tasks")
    return (
        "/proposal/acceptance_criteria",
        "/proposal/agents",
        "/proposal/decisions",
        "/proposal/tasks",
    )


def _planning_validation_diagnostic(
    error: ValidationError,
    payload: dict[str, object],
) -> ResponseValidationDiagnostic:
    """Recover precise fields from proposal-level relational validators."""

    issues = error.errors(
        include_url=False,
        include_context=False,
        include_input=False,
    )
    if (
        len(issues) == 1
        and tuple(issues[0]["loc"]) == ("proposal",)
        and str(issues[0]["type"]).startswith("value_error")
    ):
        detail = _safe_validation_detail(error)
        paths = _planning_proposal_error_paths(detail, payload)
        return diagnostic_from_message(
            payload,
            failure_class=ResponseFailureClass.SEMANTIC_CONTEXT,
            authority=ResponseIssueAuthority.MODEL,
            code="planning_context",
            message=detail,
            paths=paths,
        )
    return diagnostic_from_validation_error(error, payload)


def _planning_proposal_error_paths(
    detail: str,
    payload: dict[str, object],
) -> tuple[str, ...]:
    """Locate relational proposal failures when their values are unambiguous."""

    proposal = payload.get("proposal")
    if not isinstance(proposal, dict):
        return _planning_context_paths(detail, None)
    tasks = proposal.get("tasks")
    agents = proposal.get("agents")
    if "one stable ID for every requirement" in detail:
        return ("/proposal/requirement_ids",)
    if (
        "tasks reference unknown Agent owners" in detail
        and isinstance(tasks, list)
        and isinstance(agents, list)
    ):
        known_agent_ids = {
            item.get("id")
            for item in agents
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        paths = tuple(
            f"/proposal/tasks/{index}/owner_agent_id"
            for index, task in enumerate(tasks)
            if isinstance(task, dict)
            and task.get("owner_agent_id") not in known_agent_ids
        )
        if paths:
            return paths
    return _planning_context_paths(detail, None)


class PlanningRequest(BaseModel):
    """Direct user input and explicit authorization before any model work."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[2, 3, PLANNING_SCHEMA_VERSION] = PLANNING_SCHEMA_VERSION
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
        return self


class PlanningDecisionRecord(BaseModel):
    """Attributable resolution or proposal for one material decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^DECISION_[A-Z0-9_]+$")
    category: PlanningDecisionCategory
    authority: PlanningDecisionAuthority
    summary: str = Field(min_length=1, max_length=500)
    rationale: str = Field(min_length=1, max_length=500)
    question_id: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]*$",
        exclude_if=lambda value: value is None,
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
        if (
            self.authority is PlanningDecisionAuthority.USER
            and self.question_id is None
        ):
            raise ValueError("user decisions must reference their Planning question")
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


_ALL_REVIEW_BOUNDARIES = tuple(ReviewBoundaryKind)
_ABSOLUTE_GUARANTEE_PATTERN = re.compile(
    r"(?:\bnever\b|\b(?:must|shall|may|can|does?|is|are)\s+not\b|"
    r"\bcannot\b|"
    r"\bunder\s+no\s+circumstances\b|\bat\s+any\s+(?:depth|level|time)\b|"
    r"不得|禁止|永不|绝不|任何(?:层级|深度|情况下)?.{0,8}(?:不|无))",
    flags=re.IGNORECASE,
)


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
) -> None:
    known = set(nodes)
    for node, required in dependencies.items():
        unknown = set(required) - known
        if unknown:
            raise ValueError(
                f"{label} {node} references unknown dependencies: "
                f"{', '.join(sorted(unknown))}"
            )
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            raise ValueError(f"{label} dependencies must be acyclic")
        if node in visited:
            return
        visiting.add(node)
        for dependency in dependencies[node]:
            visit(dependency)
        visiting.remove(node)
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
        raise ValueError("proposed task IDs must be unique")
    _validate_dag(
        task_ids,
        {task.id: task.dependencies for task in tasks},
        label="task",
    )

    known_agent_ids = set(agent_dependencies)
    unknown_task_owners = {task.owner_agent_id for task in tasks} - known_agent_ids
    if unknown_task_owners:
        raise ValueError(
            "tasks reference unknown Agent owners: "
            + ", ".join(sorted(unknown_task_owners))
        )

    writers = set(writer_agent_ids)
    task_owners = {task.owner_agent_id for task in tasks}
    unassigned_writers = writers - task_owners
    if unassigned_writers:
        raise ValueError(
            "every implementation Agent must own at least one task: "
            + ", ".join(sorted(unassigned_writers))
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
                raise ValueError(
                    f"task {task.id} depends on {dependency_id}, but Agent "
                    f"{task.owner_agent_id} does not depend on {dependency_owner}"
                )


def validate_task_criterion_references(
    tasks: tuple[ProposedTask, ...],
    known_criterion_ids: Collection[str],
) -> None:
    """Reject task bindings outside a controller-known criterion contract."""

    covered = {criterion for task in tasks for criterion in task.acceptance_criteria}
    unknown = covered - set(known_criterion_ids)
    if unknown:
        raise ValueError(
            "tasks reference unknown acceptance criteria: " + ", ".join(sorted(unknown))
        )


class PlanningProposalBody(BaseModel):
    """Complete semantic proposal returned before user approval."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str = Field(min_length=1, max_length=120)
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
            raise ValueError("proposal acceptance criterion IDs must be unique")
        agent_ids = tuple(agent.id for agent in self.agents)
        if len(agent_ids) != len(set(agent_ids)):
            raise ValueError("proposed Agent IDs must be unique")
        decision_ids = tuple(item.id for item in self.decisions)
        if len(decision_ids) != len(set(decision_ids)):
            raise ValueError("proposal decision IDs must be unique")
        _validate_dag(
            agent_ids,
            {agent.id: agent.dependencies for agent in self.agents},
            label="Agent",
        )
        implementation_agents = {
            agent.id
            for agent in self.agents
            if agent.capability
            in {AgentCapability.IMPLEMENTATION, AgentCapability.INTEGRATION}
        }
        if not implementation_agents:
            raise ValueError("proposal requires an implementation Agent")
        quality_agents = {
            agent.id
            for agent in self.agents
            if agent.capability in {AgentCapability.TESTING, AgentCapability.REVIEW}
        }
        if not quality_agents:
            raise ValueError("proposal requires an independent quality Agent")

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

        for quality_agent in quality_agents:
            if any(
                not transitively_depends(quality_agent, implementation_agent)
                for implementation_agent in implementation_agents
            ):
                raise ValueError(
                    "every quality Agent must depend on every implementation path"
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
            raise ValueError(
                "writer tasks do not cover proposal acceptance criteria: "
                + ", ".join(sorted(missing))
            )
        if self.max_concurrency > len(self.agents):
            raise ValueError("proposal concurrency cannot exceed its Agent count")
        if self.revision_enabled != (self.iteration_limit > 1):
            raise ValueError(
                "revision_enabled must equal whether iteration_limit exceeds one"
            )
        return self


def validate_question_admission(
    question: PlanningQuestion,
    *,
    previous_question_ids: Collection[str] = (),
) -> None:
    """Reject questions outside the deterministic responsibility matrix."""

    if question.id in set(previous_question_ids):
        raise PlanningError(f"Planning question ID was already used: {question.id}")
    if question.decision_category is None or question.decision_owner is None:
        raise PlanningError("Planning question is missing decision category or owner")
    expected = _DECISION_AUTHORITY[question.decision_category]
    if question.decision_owner is not expected:
        raise PlanningError(
            f"Planning question category {question.decision_category.value} belongs "
            f"to {expected.value}, not {question.decision_owner.value}"
        )
    if expected in {
        PlanningDecisionAuthority.AGENT_AUTONOMY,
        PlanningDecisionAuthority.CONTROLLER_POLICY,
    }:
        raise PlanningError(
            f"Planning cannot ask the user to decide {question.decision_category.value}"
        )
    if not question.missing_evidence:
        raise PlanningError("Planning question must name the missing evidence")
    if not question.material_consequences:
        raise PlanningError("Planning question must name a material consequence")


def validate_planning_clarity(
    body: PlanningProposalBody,
    *,
    question_contracts: Mapping[
        str,
        tuple[PlanningDecisionCategory, PlanningDecisionAuthority],
    ]
    | None = None,
) -> None:
    """Enforce the current decision and requirement-to-evidence contract."""

    if len(body.requirement_ids) != len(body.requirements):
        raise PlanningError(
            "current proposals require one stable ID for every requirement"
        )
    if not body.non_goals:
        raise PlanningError("current proposals must state at least one non-goal")
    if not body.decisions:
        raise PlanningError("current proposals must record decision provenance")

    decisions = {decision.id: decision for decision in body.decisions}
    if len(decisions) != len(body.decisions):
        raise PlanningError("proposal decision IDs must be unique")
    if any(
        decision.authority is PlanningDecisionAuthority.CONTROLLER_POLICY
        for decision in body.decisions
    ):
        raise PlanningError(
            "Planner output cannot claim controller-policy decision authority"
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
        raise PlanningError(
            "proposal omits Planner recommendation provenance for: "
            + ", ".join(sorted(item.value for item in missing_recommendations))
        )

    if len(body.assumption_decision_ids) != len(body.assumptions):
        raise PlanningError(
            "every assumption must identify its autonomous decision record"
        )
    for decision_id in body.assumption_decision_ids:
        decision = decisions.get(decision_id)
        if decision is None:
            raise PlanningError(
                f"assumption references an unknown decision: {decision_id}"
            )
        if decision.authority is not PlanningDecisionAuthority.AGENT_AUTONOMY:
            raise PlanningError(
                f"assumption {decision_id} is not an autonomous implementation choice"
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

    for criterion in body.acceptance_criteria:
        unknown_requirements = set(criterion.requirement_ids) - requirement_ids
        if unknown_requirements:
            raise PlanningError(
                f"criterion {criterion.id} references unknown requirements: "
                + ", ".join(sorted(unknown_requirements))
            )
        if not criterion.requirement_ids:
            raise PlanningError(
                f"criterion {criterion.id} must reference at least one requirement"
            )
        covered_requirements.update(criterion.requirement_ids)
        writers = {
            task.owner_agent_id
            for task in tasks_by_criterion[criterion.id]
            if agents[task.owner_agent_id].capability
            in {AgentCapability.IMPLEMENTATION, AgentCapability.INTEGRATION}
        }
        if not writers:
            raise PlanningError(
                f"criterion {criterion.id} has no responsible writer task"
            )
        if not criterion.verification_agent_ids:
            raise PlanningError(
                f"criterion {criterion.id} must name an independent verifier"
            )
        for verifier_id in criterion.verification_agent_ids:
            verifier = agents.get(verifier_id)
            if verifier is None:
                raise PlanningError(
                    f"criterion {criterion.id} references unknown verifier "
                    f"{verifier_id}"
                )
            if verifier.capability not in {
                AgentCapability.TESTING,
                AgentCapability.REVIEW,
            }:
                raise PlanningError(
                    f"criterion {criterion.id} verifier {verifier_id} is not "
                    "read-only quality"
                )
            if any(not transitively_depends(verifier_id, writer) for writer in writers):
                raise PlanningError(
                    f"criterion {criterion.id} verifier {verifier_id} is not "
                    "downstream "
                    "of every responsible writer"
                )

    missing_requirement_coverage = requirement_ids - covered_requirements
    if missing_requirement_coverage:
        raise PlanningError(
            "requirements lack observable acceptance coverage: "
            + ", ".join(sorted(missing_requirement_coverage))
        )

    if question_contracts is None:
        return
    linked = {
        decision.question_id: decision
        for decision in body.decisions
        if decision.question_id is not None
    }
    if len(linked) != sum(
        decision.question_id is not None for decision in body.decisions
    ):
        raise PlanningError("a Planning question can resolve only one decision record")
    if set(linked) != set(question_contracts):
        missing = set(question_contracts) - set(linked)
        invented = set(linked) - set(question_contracts)
        details = []
        if missing:
            details.append("missing " + ", ".join(sorted(missing)))
        if invented:
            details.append("unknown " + ", ".join(sorted(invented)))
        raise PlanningError(
            "proposal question-decision provenance is incomplete: " + "; ".join(details)
        )
    for question_id, (category, owner) in question_contracts.items():
        decision = linked[question_id]
        if decision.category is not category or decision.authority is not owner:
            raise PlanningError(
                f"decision for question {question_id} changed its category or owner"
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
            "decision_owner",
            "missing_evidence",
            "material_consequences",
        ),
        "ProposedCriterion": (
            "requirement_ids",
            "verification_agent_ids",
            "review_boundaries",
        ),
        "PlanningProposalBody": (
            "requirement_ids",
            "non_goals",
            "assumption_decision_ids",
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
    for field_name in ("decision_category", "decision_owner"):
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
    return schema


class AdaptiveImplementationPlan(BaseModel):
    """Approved task-to-Agent intent bound by an adaptive TeamPlan."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[2, 3, PLANNING_SCHEMA_VERSION] = PLANNING_SCHEMA_VERSION
    run_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    team_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    revision: int = Field(ge=1)
    created_at: datetime
    objective: str
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

    @field_validator("started_at", "finished_at")
    @classmethod
    def require_timestamp(cls, value: datetime) -> datetime:
        return _utc(value)


class PlanningTurn(BaseModel):
    """One append-only model invocation, including invalid response evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[2, 3, PLANNING_SCHEMA_VERSION] = PLANNING_SCHEMA_VERSION
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
        if _digest_text(self.prompt) != self.prompt_sha256:
            raise ValueError("Planning prompt digest does not match its content")
        if self.response_text is None:
            if (
                self.response_sha256 is not None
                or self.parsed_response is not None
                or self.response_normalizations
            ):
                raise ValueError("missing response text cannot have parsed evidence")
        elif _digest_text(self.response_text) != self.response_sha256:
            raise ValueError("Planning response digest does not match its content")
        if self.parsed_response is not None and self.validation_error is not None:
            raise ValueError("valid Planning turns cannot contain a validation error")
        if (self.semantic_correction_request is None) != (
            self.semantic_correction_outcome is None
        ):
            raise ValueError(
                "Planning correction request and outcome must appear together"
            )
        if self.execution.status is AgentExecutionStatus.COMPLETED:
            if self.response_text is None:
                raise ValueError("completed Planning execution requires response text")
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

    schema_version: Literal[2, 3, PLANNING_SCHEMA_VERSION] = PLANNING_SCHEMA_VERSION
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

    schema_version: Literal[2, 3, PLANNING_SCHEMA_VERSION] = PLANNING_SCHEMA_VERSION
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

    schema_version: Literal[2, 3, PLANNING_SCHEMA_VERSION] = PLANNING_SCHEMA_VERSION
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
    if proposal.schema_version == PLANNING_SCHEMA_VERSION:
        validate_planning_clarity(body)
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
    lines = [
        "Planning overview",
        "  Outcome and scope:",
        f"  Product: {brief.title}",
        f"  Request: {brief.source_request}",
        f"  Destination: {preview.destination}",
        "  Execution profile:",
        *(f"    - {item}" for item in preview.execution_profile),
        "  Requirements:",
        *(
            f"    - {requirement_id}: {text}"
            for requirement_id, text in requirement_pairs
        ),
        "  Non-goals:",
        *(
            (f"    - {item}" for item in implementation.non_goals)
            if implementation.non_goals
            else ("    - unavailable in legacy Planning evidence",)
        ),
    ]
    if preview.execution_profile_constraints:
        lines.append("  Execution-profile constraints (controller-owned):")
        lines.extend(f"    - {item}" for item in preview.execution_profile_constraints)
    if preview.planner_constraints:
        lines.append("  Additional task constraints proposed by Planning:")
        lines.extend(f"    - {item}" for item in preview.planner_constraints)
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
            question = (
                ""
                if decision.question_id is None
                else f"; question={decision.question_id}"
            )
            lines.append(
                f"      - {decision.id} [{decision.category.value}{question}]: "
                f"{decision.summary} (why: {decision.rationale})"
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
            lines.append(f"      - {decision_id}: {assumption}")
    else:
        lines.extend(
            f"      - legacy/unowned: {item}" for item in implementation.assumptions
        )
    lines.extend(
        (
            "    Non-negotiable Controller policy:",
            "      - secret isolation and least-privilege permissions",
            "      - immutable evidence and fail-closed lifecycle transitions",
            "      - cleanup limited to resources proven to be SAT-owned",
            "      - only a verified accepted workspace may be delivered",
        )
    )
    lines.extend(
        (
            "  Acceptance criteria:",
            *(
                f"    - {item.id}: {item.description} (verify: "
                f"{item.verification}; Review boundaries: "
                + (
                    ", ".join(boundary.value for boundary in item.review_boundaries)
                    or "none"
                )
                + ")"
                for item in brief.acceptance_criteria
            ),
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
    lines.extend(
        (
            "  Implementation approach:",
            *(f"    - {item}" for item in implementation.approach),
            "  Tasks:",
        )
    )
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
            (
                f"    - {task.id} -> {task.owner_agent_id}: {task.description}",
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
            (
                f"    - {agent.id} ({agent.label})",
                f"      responsibility: {agent.responsibility}",
                f"      why: {agent.rationale}",
                f"      capability: {agent.capability.value}",
                f"      dependencies: {dependencies}",
                f"      permission: {agent.permission_profile.value}",
                f"      workspace: {agent.workspace_scope}",
                f"      inputs: {inputs}",
                f"      output: {agent.expected_output.value}",
                f"      handoff: {handoff}",
                f"      model: {route.model} (profile {route.id}; "
                f"{assignment.selection_source.value})",
                f"      model reason: {assignment.reason}",
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
        lines.extend(f"    - {item}" for item in implementation.risks)
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
        except (ValueError, PlanningError) as error:
            raise PlanningError(f"proposed TeamPlan is invalid: {error}") from error

    @staticmethod
    def _question_contracts(
        transcript: list[dict[str, object]],
        current_proposal: PlanningProposal | None,
    ) -> dict[
        str,
        tuple[PlanningDecisionCategory, PlanningDecisionAuthority],
    ]:
        """Recover every answered question contract without trusting prose."""

        contracts: dict[
            str,
            tuple[PlanningDecisionCategory, PlanningDecisionAuthority],
        ] = {}
        if current_proposal is not None:
            for decision in current_proposal.body.decisions:
                if decision.question_id is None:
                    continue
                contracts[decision.question_id] = (
                    decision.category,
                    decision.authority,
                )
        for entry in transcript:
            question = PlanningQuestion.model_validate(entry["question"])
            if question.decision_category is None or question.decision_owner is None:
                raise PlanningError(
                    "persisted Planning transcript has an incomplete question contract"
                )
            if question.id in contracts:
                raise PlanningError(
                    f"Planning question ID was already used: {question.id}"
                )
            contracts[question.id] = (
                question.decision_category,
                question.decision_owner,
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
            prompt = self._prompt(
                request,
                transcript=transcript,
                current_proposal=current_proposal,
                change_request=change_request,
                correction_plan=correction_plan,
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
                    )
                try:
                    budget_usage = self.budget_ledger.complete_call(
                        reservation,
                        input_tokens=None if usage is None else usage.input_tokens,
                        output_tokens=None if usage is None else usage.output_tokens,
                        duration_ms=result.telemetry.duration_ms,
                    )
                except AgentBudgetExceeded as error:
                    budget_usage = error.usage
                    budget_error = str(error)
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
                    payload = parse_json_object_response(result.response_text)
                    if correction_plan is not None:
                        payload = apply_semantic_correction(payload, correction_plan)
                        correction_applied = True
                    payload, initial_normalizations = (
                        _normalize_planning_response_payload(
                            payload,
                            profile_criterion_ids=(
                                criterion.id
                                for criterion in self.policy.profile_acceptance_criteria
                            ),
                        )
                    )
                    normalization_list = list(initial_normalizations)
                    response_normalizations = tuple(normalization_list)
                    while True:
                        try:
                            parsed = PlanningModelResponse.model_validate(payload)
                            break
                        except ValidationError as error:
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
                            question_contracts=question_contracts,
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
                except (PlanningError, ValueError) as error:
                    validation_error = _safe_validation_detail(error)
                    if payload is not None:
                        response_validation = diagnostic_from_message(
                            payload,
                            failure_class=ResponseFailureClass.SEMANTIC_CONTEXT,
                            authority=ResponseIssueAuthority.MODEL,
                            code="planning_context",
                            message=validation_error,
                            paths=(
                                _planning_proposal_error_paths(
                                    validation_error,
                                    payload,
                                )
                                if parsed is not None
                                and parsed.kind is PlanningResponseKind.PROPOSAL
                                else _planning_context_paths(
                                    validation_error,
                                    parsed,
                                )
                            ),
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
    ) -> str:
        template = Template(PLANNING_TEMPLATE.read_text(encoding="utf-8"))
        context = {
            "request": request.model_dump(mode="json"),
            "dialogue": transcript,
            "current_proposal": (
                None
                if current_proposal is None
                else current_proposal.body.model_dump(mode="json")
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
                _planning_response_schema(),
                ensure_ascii=False,
                indent=2,
            ),
            repair_context_json="null",
        )
        if correction_plan is not None:
            rendered += correction_prompt(correction_plan)
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
