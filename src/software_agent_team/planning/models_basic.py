"""Basic Planning enums, activity events, and provenance models."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from software_agent_team.artifacts import (
    ProductDefinitionDimension,
)
from software_agent_team.budgets import AgentBudgetUsage
from software_agent_team.execution import (
    AgentExecutionStatus,
    AgentToolActionClass,
    AgentToolTargetClass,
)
from software_agent_team.invocation_lifecycle import (
    InitializationCheckpoint,
    InvocationPhase,
    InvocationStopReason,
)
from software_agent_team.model_metadata import ModelMetadataSource
from software_agent_team.planning.common import _clean_text
from software_agent_team.terminal_presentation import InvocationPresentation


class PlanningSessionStatus(StrEnum):
    """Controller-owned state of one pre-execution Planning session."""

    AUTHORIZED = "authorized"
    CLARIFYING = "clarifying"
    PROPOSED = "proposed"
    APPROVED = "approved"
    CANCELLED = "cancelled"
    FAILED = "failed"


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


class PlanningQuestionOrigin(StrEnum):
    """Controller-owned reason that one question reached the user."""

    PLANNER_SUGGESTION = "planner_suggestion"
    CONTROLLER_REQUIREMENT = "controller_requirement"


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


_DECISION_PROVENANCE_AUTHORITY = {
    PlanningDecisionProvenanceKind.EXPLICIT_INPUT: PlanningDecisionAuthority.USER,
    PlanningDecisionProvenanceKind.RESOLVED_QUESTION: PlanningDecisionAuthority.USER,
    PlanningDecisionProvenanceKind.PLANNER_RECOMMENDATION: (
        PlanningDecisionAuthority.PLANNER_PROPOSAL
    ),
    PlanningDecisionProvenanceKind.AGENT_AUTONOMY: (
        PlanningDecisionAuthority.AGENT_AUTONOMY
    ),
}


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


class PlanningRecoveryAction(StrEnum):
    """Controller-owned continuation selected after one failed Planning turn."""

    SAME_ROUTE_RETRY = "same_route_retry"
    FALLBACK_ROUTE = "fallback_route"


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
    PROPOSAL_REGENERATION_SCHEDULED = "proposal_regeneration_scheduled"
    SUBMISSION_EVIDENCE_RECOVERY = "submission_evidence_recovery"
    MODEL_ROUTE_SWITCHED = "model_route_switched"
    CLARIFICATION_SCHEDULED = "clarification_scheduled"
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
    clarification_dimension: ProductDefinitionDimension | None = None
    clarification_category: PlanningDecisionCategory | None = None
    correction_reasons: tuple[str, ...] = ()
    presentation: InvocationPresentation | None = None


PlanningActivityHandler = Callable[[PlanningActivity], None]
