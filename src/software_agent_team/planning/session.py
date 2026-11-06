"""Planning sessions, adaptive plans, approvals, previews, and overviews.

This module owns:
    - AdaptiveImplementationPlan: The bound DAG and Agent assignments.
    - PlanningSession: Controller state machine across clarification turns.
    - PlanningTurn: Immutable turn record with prompts, model responses, telemetry.
    - PlanningProposal: Proposed requirements, criteria, agents, and dependencies.
    - PlanningApproval: User-confirmed binding to a specific proposal revision.
    - Overview Renderers: render_planning_overview, preview_adaptive_proposal.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from software_agent_team.artifacts import (
    AcceptanceCriterion,
    AgentToolEvidenceStatus,
    ProductDefinition,
    ProviderLivenessEvidence,
    RuntimeRejectionEvidence,
    TaskBrief,
    review_boundary_definition_map,
    validate_tool_evidence_collection,
)
from software_agent_team.budgets import (
    AgentBudget,
    AgentBudgetUsage,
    ModelCallCostRecord,
)
from software_agent_team.execution import (
    AgentExecutionStatus,
)
from software_agent_team.integrity import canonical_model_sha256
from software_agent_team.invocation_lifecycle import InvocationLifecycleEvidence
from software_agent_team.model_metadata import ModelMetadataSource
from software_agent_team.model_routing import (
    ModelProfile,
    ModelRoute,
    ModelRoutingError,
    ModelRoutingMode,
    ModelRoutingPolicy,
    resolve_model_route_plan,
)
from software_agent_team.planning.common import (
    MAX_PLANNING_EVIDENCE_CHARACTERS,
    MAX_RESPONSE_NORMALIZATION_CHARACTERS,
    MAX_RESPONSE_NORMALIZATIONS,
    PLANNING_SCHEMA_VERSION,
    _clean_unique,
    _digest_text,
    _render_prefixed_text,
    _utc,
)
from software_agent_team.planning.entities import (
    ProposedAgent,
    ProposedCriterion,
    ProposedTask,
)
from software_agent_team.planning.invariants import (
    PlanningError,
    _PlanningContextInvariantError,
    _PlanningContextInvariantsError,
    _PlanningModelInvariantError,
)
from software_agent_team.planning.models_basic import (
    AgentWorkload,
    PlanningDecisionAuthority,
    PlanningDecisionCategory,
    PlanningDecisionProvenanceKind,
    PlanningProposalSource,
    PlanningRecoveryAction,
    PlanningSessionStatus,
    StructuredEditKind,
)
from software_agent_team.planning.projections import (
    _compile_model_route_projection,
    _compile_team_topology_projection,
)
from software_agent_team.planning.proposals import (
    PlanningModelResponse,
    PlanningProposalBody,
    validate_planning_clarity,
    validate_task_agent_bindings,
    validate_task_criterion_references,
    validate_writer_task_scope_forecasts,
)
from software_agent_team.planning.questions import (
    PlanningDecisionRecord,
    PlanningQuestionAdmission,
    PlanningQuestionAnswer,
    PlanningRequest,
    PresentedPlanningQuestion,
)
from software_agent_team.planning.scopes import (
    _resolve_review_scope_assignments,
    _review_task_scope_invariants,
)
from software_agent_team.planning_normalization import strip_redundant_stable_id_prefix
from software_agent_team.response_corrections import (
    ResponseValidationDiagnostic,
    SemanticCorrectionOutcome,
    SemanticCorrectionRequestEvidence,
)
from software_agent_team.submissions import (
    RECOVERABLE_SUBMISSION_EVIDENCE_CODES,
    AgentSubmissionEvidence,
    AgentSubmissionPurpose,
    AgentSubmissionStatus,
    canonical_json_sha256,
)
from software_agent_team.teams import (
    AgentCapability,
    AgentSpec,
    PermissionProfile,
    PlanApprovalSource,
    TeamPlan,
    TeamPlanOrigin,
    expected_output_for_specialization,
    permission_for_capability,
    specialization_contract,
)

RECOVERABLE_PLANNING_EXECUTION_STATUSES = frozenset(
    {
        AgentExecutionStatus.INVALID_RESPONSE,
        AgentExecutionStatus.PROCESS_FAILED,
        AgentExecutionStatus.UPSTREAM_INCOMPLETE,
    }
)


class AdaptiveImplementationPlan(BaseModel):
    """Approved task-to-Agent intent bound by an adaptive TeamPlan."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[
        2,
        3,
        4,
        5,
        6,
        7,
        8,
        9,
        10,
        11,
        12,
        13,
        14,
        15,
        16,
        17,
        18,
        19,
        20,
        21,
        22,
        PLANNING_SCHEMA_VERSION,
    ] = PLANNING_SCHEMA_VERSION
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
    tool_evidence_status: AgentToolEvidenceStatus | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    session_transcript_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        exclude_if=lambda value: value is None,
    )
    session_record_count: int | None = Field(
        default=None,
        ge=1,
        le=4096,
        exclude_if=lambda value: value is None,
    )
    tool_evidence_error: str | None = Field(
        default=None,
        min_length=1,
        max_length=2000,
        exclude_if=lambda value: value is None,
    )
    error: str | None = None
    invocation_lifecycle: InvocationLifecycleEvidence | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    cost_record: ModelCallCostRecord | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    runtime_rejection_evidence: RuntimeRejectionEvidence | None = Field(
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
        if self.tool_evidence_status is None:
            if (
                self.session_transcript_sha256 is not None
                or self.session_record_count is not None
                or self.tool_evidence_error is not None
            ):
                raise ValueError(
                    "Planning tool evidence details require a collection status"
                )
        else:
            validate_tool_evidence_collection(
                status=self.tool_evidence_status,
                transcript_sha256=self.session_transcript_sha256,
                record_count=self.session_record_count,
                tool_calls=(),
                error=self.tool_evidence_error,
            )
        return self

    @field_validator("started_at", "finished_at")
    @classmethod
    def require_timestamp(cls, value: datetime) -> datetime:
        return _utc(value)


class PlanningTurn(BaseModel):
    """One append-only model invocation, including invalid response evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[
        2,
        3,
        4,
        5,
        6,
        7,
        8,
        9,
        10,
        11,
        12,
        13,
        14,
        15,
        16,
        17,
        18,
        19,
        20,
        21,
        22,
        PLANNING_SCHEMA_VERSION,
    ] = PLANNING_SCHEMA_VERSION
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
    question_admission: PlanningQuestionAdmission | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
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
    recovery_action: PlanningRecoveryAction | None = Field(
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
        parsed_question = (
            None if self.parsed_response is None else self.parsed_response.question
        )
        if self.schema_version >= 17:
            if (parsed_question is None) != (self.question_admission is None):
                raise ValueError(
                    "current Planning question requires Controller admission evidence"
                )
            if (
                parsed_question is not None
                and self.question_admission is not None
                and self.question_admission.product_definition_dimensions
                != parsed_question.product_definition_dimensions
            ):
                raise ValueError(
                    "Planning question admission scope differs from the question"
                )
            if (
                parsed_question is not None
                and self.question_admission is not None
                and self.question_admission.controller_decision_id is not None
                and self.question_admission.product_definition_dimensions
                and parsed_question.decision_category
                is not PlanningDecisionCategory.PRODUCT_REQUIREMENT
            ):
                raise ValueError(
                    "only a product-requirement question may bind a Controller "
                    "decision and product dimension together"
                )
        elif self.question_admission is not None:
            raise ValueError(
                "legacy Planning turns cannot contain question admission evidence"
            )
        if (
            self.schema_version < 18
            and self.question_admission is not None
            and self.question_admission.controller_decision_id is not None
        ):
            raise ValueError(
                "legacy Planning turns cannot contain Controller decision binding"
            )
        if (
            self.schema_version < 20
            and self.question_admission is not None
            and self.question_admission.controller_decision_id is not None
            and self.question_admission.product_definition_dimensions
        ):
            raise ValueError(
                "legacy Planning turns cannot combine Controller decision and "
                "product-dimension binding"
            )
        lifecycle = self.execution.invocation_lifecycle
        if (
            self.schema_version < 15
            and lifecycle is not None
            and lifecycle.schema_version >= 5
        ):
            raise ValueError("legacy Planning turns cannot contain lifecycle v5")
        if (
            self.schema_version < 14
            and lifecycle is not None
            and lifecycle.schema_version >= 4
        ):
            raise ValueError("legacy Planning turns cannot contain lifecycle v4")
        if self.schema_version < 13 and (
            "invocation_lifecycle" in self.execution.model_fields_set
        ):
            raise ValueError(
                "legacy Planning turns cannot contain invocation lifecycle"
            )
        if self.schema_version < 12 and (
            "runtime_rejection_evidence" in self.execution.model_fields_set
        ):
            raise ValueError("legacy Planning turns cannot contain runtime rejections")
        tool_evidence_fields = {
            "tool_evidence_status",
            "session_transcript_sha256",
            "session_record_count",
            "tool_evidence_error",
        }
        if self.schema_version < 21 and tool_evidence_fields.intersection(
            self.execution.model_fields_set
        ):
            raise ValueError("legacy Planning turns cannot contain tool evidence state")
        if self.schema_version < 21 and self.recovery_action is not None:
            raise ValueError("legacy Planning turns cannot schedule typed recovery")
        if self.schema_version >= 21 and self.execution.tool_evidence_status is None:
            raise ValueError("current Planning turns require tool evidence state")
        if self.recovery_action is not None:
            diagnostic_code = (
                None
                if self.submission_evidence is None
                else self.submission_evidence.diagnostic_code
            )
            if (
                self.execution.status
                in {
                    AgentExecutionStatus.COMPLETED,
                    AgentExecutionStatus.INTERRUPTED,
                }
                or self.execution.budget_error is not None
                or self.submission_payload is not None
                or diagnostic_code not in RECOVERABLE_SUBMISSION_EVIDENCE_CODES
            ):
                raise ValueError("Planning recovery lacks a safe failed invocation")
            lifecycle = self.execution.invocation_lifecycle
            if lifecycle is not None and not lifecycle.shutdown.cleanup_completed:
                raise ValueError("Planning recovery requires completed cleanup")
            if (self.recovery_action is PlanningRecoveryAction.FALLBACK_ROUTE) != (
                self.execution.status is AgentExecutionStatus.PROVIDER_FAILED
            ):
                raise ValueError("Planning recovery action differs from failure class")
            if (
                self.recovery_action is PlanningRecoveryAction.SAME_ROUTE_RETRY
                and self.execution.status not in RECOVERABLE_PLANNING_EXECUTION_STATUSES
            ):
                raise ValueError("Planning failure class cannot be retried")
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
        if (
            self.schema_version >= 16
            and self.parsed_response is not None
            and self.parsed_response.proposal is not None
        ):
            proposal = self.parsed_response.proposal
            validate_task_agent_bindings(
                proposal.tasks,
                {agent.id: agent.dependencies for agent in proposal.agents},
                {
                    agent.id
                    for agent in proposal.agents
                    if agent.capability
                    in {AgentCapability.IMPLEMENTATION, AgentCapability.INTEGRATION}
                },
                require_canonical_cross_agent_projection=True,
            )
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

    schema_version: Literal[
        2,
        3,
        4,
        5,
        6,
        7,
        8,
        9,
        10,
        11,
        12,
        13,
        14,
        15,
        16,
        17,
        18,
        19,
        20,
        21,
        22,
        PLANNING_SCHEMA_VERSION,
    ] = PLANNING_SCHEMA_VERSION
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
        if self.schema_version >= 16:
            validate_task_agent_bindings(
                self.body.tasks,
                {agent.id: agent.dependencies for agent in self.body.agents},
                {
                    agent.id
                    for agent in self.body.agents
                    if agent.capability
                    in {AgentCapability.IMPLEMENTATION, AgentCapability.INTEGRATION}
                },
                require_canonical_cross_agent_projection=True,
            )
        if self.schema_version >= 22:
            validate_writer_task_scope_forecasts(self.body.tasks, self.body.agents)
        return self


class PlanningSession(BaseModel):
    """Atomic index anchoring all write-once Planning evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[
        2,
        3,
        4,
        5,
        6,
        7,
        8,
        9,
        10,
        11,
        12,
        13,
        14,
        15,
        16,
        17,
        18,
        19,
        20,
        21,
        22,
        PLANNING_SCHEMA_VERSION,
    ] = PLANNING_SCHEMA_VERSION
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
        if self.schema_version < 13 and self.status is PlanningSessionStatus.FAILED:
            raise ValueError("legacy Planning sessions cannot contain failed state")
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

    schema_version: Literal[
        2,
        3,
        4,
        5,
        6,
        7,
        8,
        9,
        10,
        11,
        12,
        13,
        14,
        15,
        16,
        17,
        18,
        19,
        20,
        21,
        22,
        PLANNING_SCHEMA_VERSION,
    ] = PLANNING_SCHEMA_VERSION
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
    review_scope_by_agent: dict[str, tuple[str, ...]] = Field(default_factory=dict)


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

        if self.implementation_plan.schema_version >= 16:
            validate_task_agent_bindings(
                self.implementation_plan.tasks,
                {agent.id: agent.dependencies for agent in self.team_plan.agents},
                {
                    agent.id
                    for agent in self.team_plan.agents
                    if agent.capability
                    in {AgentCapability.IMPLEMENTATION, AgentCapability.INTEGRATION}
                },
                require_canonical_cross_agent_projection=True,
            )
        if self.implementation_plan.schema_version >= 22:
            validate_writer_task_scope_forecasts(
                self.implementation_plan.tasks, self.team_plan.agents
            )

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


def compile_approved_review_scopes(
    approved: ApprovedPlanningResult,
    *,
    manual_review_criteria: Collection[str] | None = None,
) -> dict[str, tuple[str, ...]] | None:
    """Compile the approved criterion contract into runtime Reviewer scopes."""

    implementation = approved.implementation_plan
    if implementation.schema_version < 19 or not implementation.acceptance_criteria:
        return None
    proposed_by_id = {
        criterion.id: criterion for criterion in implementation.acceptance_criteria
    }
    proposed_ids = set(proposed_by_id)
    task_brief_ids = tuple(
        criterion.id for criterion in approved.task_brief.acceptance_criteria
    )
    if not proposed_ids.issubset(set(task_brief_ids)):
        raise PlanningError(
            "approved implementation criteria are absent from the TaskBrief"
        )
    manual_ids = (
        task_brief_ids
        if manual_review_criteria is None
        else tuple(dict.fromkeys(manual_review_criteria))
    )
    if not set(manual_ids).issubset(set(task_brief_ids)):
        raise PlanningError("manual Review scope references an unknown criterion")
    selected_criteria = tuple(
        proposed_by_id[criterion_id]
        for criterion_id in manual_ids
        if criterion_id in proposed_by_id
    )
    profile_criterion_ids = tuple(
        criterion_id for criterion_id in manual_ids if criterion_id not in proposed_ids
    )
    scopes, invariants = _resolve_review_scope_assignments(
        criteria=selected_criteria,
        definition=implementation.product_definition,
        agents=approved.team_plan.agents,
        profile_criterion_ids=profile_criterion_ids,
    )
    if invariants:
        raise PlanningError(
            "approved review-scope contract is invalid: "
            + "; ".join(invariant.message for invariant in invariants)
        )
    task_invariants = _review_task_scope_invariants(
        tasks=implementation.tasks,
        review_scope_by_agent=scopes,
    )
    if task_invariants:
        raise PlanningError(
            "approved Review task contract is invalid: "
            + "; ".join(invariant.message for invariant in task_invariants)
        )
    return scopes


Clock = Callable[[], datetime]
QuestionAnswerer = Callable[
    [PresentedPlanningQuestion],
    str | PlanningQuestionAnswer | None,
]
InputReader = Callable[[str], str]
OutputWriter = Callable[[str], None]


def _system_clock() -> datetime:
    return datetime.now(UTC)


def _normalize_question_answer(
    question: PresentedPlanningQuestion,
    value: str | PlanningQuestionAnswer,
) -> PlanningQuestionAnswer:
    """Accept custom text or verify one exact Controller-presented option."""

    answer = (
        PlanningQuestionAnswer(text=value)
        if isinstance(value, str)
        else PlanningQuestionAnswer.model_validate(value)
    )
    if answer.selected_option_id is None:
        if answer.product_definition_values:
            raise PlanningError(
                "custom Planning answers cannot claim suggested product values"
            )
        return answer
    selected = next(
        (
            option
            for option in question.options
            if option.id == answer.selected_option_id
        ),
        None,
    )
    if selected is None:
        raise PlanningError("Planning answer selected an unknown option")
    if answer.product_definition_values != selected.product_definition_values:
        raise PlanningError("Planning answer changed the selected option values")
    return answer


def _compiled_writer_role(
    agent: ProposedAgent,
    tasks: tuple[ProposedTask, ...],
) -> tuple[str, str]:
    """Use assigned tasks and scope, rather than parallel prose, for writer work."""

    assigned = tuple(task for task in tasks if task.owner_agent_id == agent.id)
    if not assigned:
        raise PlanningError(f"writable Agent {agent.id} has no assigned task")
    descriptions = tuple(task.description for task in assigned)
    summary = " ".join(descriptions)
    responsibility = (
        summary
        if len(summary) <= 500
        else (
            f"Complete {len(assigned)} approved assigned tasks; "
            "see the task list for their full scope."
        )
    )
    rationale = (
        f"Owns {len(assigned)} approved task(s) within {agent.workspace_scope}; "
        "assigned tasks define executable work."
    )
    return responsibility, rationale


def preview_adaptive_proposal(
    request: PlanningRequest,
    proposal: PlanningProposal,
    policy: PlanningPolicy,
    *,
    created_at: datetime,
    additional_user_inputs: Collection[str] = (),
) -> PlanningPreview:
    """Compile approval authority using caller-verified user revision inputs."""

    if proposal.run_id != request.run_id:
        raise PlanningError("proposal belongs to a different Planning request")
    body = proposal.body
    profile_criterion_ids = tuple(
        criterion.id for criterion in policy.profile_acceptance_criteria
    )
    if proposal.schema_version >= 5:
        validate_planning_clarity(
            body,
            source_request=request.source_request,
            additional_user_inputs=additional_user_inputs,
            allowed_criterion_ids=profile_criterion_ids,
            require_current_decision_provenance=proposal.schema_version >= 8,
            allow_legacy_product_decision_links=proposal.schema_version < 8,
            allow_legacy_workflow_exemption=proposal.schema_version < 9,
            enforce_specialized_review_authority=proposal.schema_version >= 19,
            enforce_test_suite_writer_ownership=proposal.schema_version >= 23,
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
    profile_criterion_id_set = set(profile_criterion_ids)
    proposed_criterion_ids = {criterion.id for criterion in body.acceptance_criteria}
    collisions = profile_criterion_id_set & proposed_criterion_ids
    if collisions:
        raise PlanningError(
            "proposal repeats controller-owned profile criteria: "
            + ", ".join(sorted(collisions))
        )
    try:
        validate_task_criterion_references(
            body.tasks,
            proposed_criterion_ids | profile_criterion_id_set,
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
    review_scope_by_agent: dict[str, tuple[str, ...]] = {}
    if proposal.schema_version >= 19:
        review_scope_by_agent, review_scope_invariants = (
            _resolve_review_scope_assignments(
                criteria=body.acceptance_criteria,
                definition=body.product_definition,
                agents=body.agents,
                profile_criterion_ids=profile_criterion_ids,
            )
        )
        if review_scope_invariants:
            raise _PlanningContextInvariantsError(review_scope_invariants)

    if proposal.schema_version >= 19:
        product_definition, decisions = _compile_team_topology_projection(body)
    else:
        product_definition, decisions = body.product_definition, body.decisions

    constraints = tuple(dict.fromkeys((*request.base_constraints, *body.constraints)))
    task_brief = TaskBrief(
        run_id=request.run_id,
        title=body.title,
        source_request=request.source_request,
        requirements=list(body.requirements),
        product_definition=product_definition,
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
        product_definition=product_definition,
        requirement_ids=body.requirement_ids,
        requirements=body.requirements,
        acceptance_criteria=body.acceptance_criteria,
        non_goals=body.non_goals,
        approach=body.approach,
        tasks=body.tasks,
        risks=body.risks,
        assumptions=body.assumptions,
        assumption_decision_ids=body.assumption_decision_ids,
        decisions=decisions,
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
    if proposal.schema_version >= 19:
        implementation_plan = implementation_plan.model_copy(
            update={
                "decisions": _compile_model_route_projection(
                    implementation_plan.decisions,
                    model_routes,
                )
            }
        )
    assignments = {
        assignment.agent_id: assignment for assignment in model_routes.assignments
    }

    agents = []
    timeout_resolutions = []
    for proposed in body.agents:
        if proposed.capability in {
            AgentCapability.IMPLEMENTATION,
            AgentCapability.INTEGRATION,
        }:
            responsibility, rationale = _compiled_writer_role(proposed, body.tasks)
        else:
            responsibility, rationale = proposed.responsibility, proposed.rationale
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
                    responsibility=responsibility,
                    rationale=rationale,
                    capability=proposed.capability,
                    specialization=proposed.specialization,
                    permission_profile=permission_for_capability(proposed.capability),
                    stage_id=proposed.stage_id,
                    dependencies=proposed.dependencies,
                    expected_output=expected_output_for_specialization(
                        proposed.specialization
                    ),
                    model_route_id=assignments[proposed.id].primary_route_id,
                    timeout_seconds=0,
                    workspace_scope=proposed.workspace_scope,
                )
            )
            continue

        review_scope = (
            tuple(task_brief.acceptance_criteria)
            if proposed.capability is AgentCapability.REVIEW
            and proposal.schema_version < 19
            else tuple(
                criterion
                for criterion in task_brief.acceptance_criteria
                if criterion.id in review_scope_by_agent.get(proposed.id, ())
            )
            if proposed.capability is AgentCapability.REVIEW
            else ()
        )
        scope_criterion_count = (
            len(review_scope) if proposed.capability is AgentCapability.REVIEW else None
        )
        scope_boundary_obligation_count = (
            sum(len(criterion.review_boundaries) for criterion in review_scope)
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
                responsibility=responsibility,
                rationale=rationale,
                capability=proposed.capability,
                specialization=proposed.specialization,
                permission_profile=permission_for_capability(proposed.capability),
                stage_id=proposed.stage_id,
                dependencies=proposed.dependencies,
                expected_output=expected_output_for_specialization(
                    proposed.specialization
                ),
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
        review_scope_by_agent=review_scope_by_agent,
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


def _style_planning_overview(lines: list[str], *, color: bool) -> str:
    """Apply a small semantic palette after content has been safely wrapped."""

    if not color:
        return "\n".join(lines)
    rendered: list[str] = []
    for line in lines:
        stripped = line.strip()
        code: str | None = None
        if line == "Planning overview":
            code = "1;36"
        elif stripped.endswith(":") and not stripped.startswith("-"):
            code = "1"
        elif stripped.startswith(("Budget:", "Execution:")):
            code = "36"
        elif stripped.startswith(("Risk:", "Risks:")):
            code = "33"
        if code is not None:
            rendered.append(f"\x1b[{code}m{line}\x1b[0m")
        else:
            rendered.append(line)
    return "\n".join(rendered)


def _render_concise_planning_overview(
    preview: PlanningPreview,
    *,
    budget_usage: AgentBudgetUsage | None,
    include_fixed_policy: bool,
    visibility: Literal["compact", "standard"],
    color: bool,
) -> str:
    """Render the decisions a person needs without controller graph internals."""

    brief = preview.task_brief
    implementation = preview.implementation_plan
    plan = preview.team_plan
    definition = implementation.product_definition
    lines = ["Planning overview"]
    lines.extend(_render_prefixed_text("  Product: ", brief.title))
    if definition is not None:
        lines.extend(
            _render_prefixed_text(
                "  For: ",
                definition.target_users.statement,
            )
        )
        lines.extend(
            _render_prefixed_text(
                "  Main workflow: ",
                definition.primary_workflow.statement,
            )
        )
        lines.append(
            "  Delivery level: "
            + definition.delivery_maturity.level.value.replace("_", " ")
        )
    lines.extend(_render_prefixed_text("  Destination: ", preview.destination))

    if visibility == "standard":
        lines.append("  What the team will build:")
        for requirement in implementation.requirements:
            lines.extend(_render_prefixed_text("    - ", requirement))
        lines.append("  Acceptance checks:")
        for criterion in brief.acceptance_criteria:
            lines.extend(_render_prefixed_text("    - ", criterion.description))
        if implementation.non_goals:
            lines.append("  Outside this build:")
            for item in implementation.non_goals:
                lines.extend(_render_prefixed_text("    - ", item))
        decisions = tuple(
            decision
            for decision in implementation.decisions
            if decision.authority
            in {
                PlanningDecisionAuthority.USER,
                PlanningDecisionAuthority.PLANNER_PROPOSAL,
            }
        )
        if decisions or implementation.assumptions:
            lines.append("  Decisions and assumptions to approve:")
            for decision in decisions:
                lines.extend(_render_prefixed_text("    - ", decision.summary))
            for assumption in implementation.assumptions:
                lines.extend(_render_prefixed_text("    - Assumption: ", assumption))
        lines.append("  Team:")
        for agent in plan.agents:
            route = plan.model_routes.get_route(agent.model_route_id)
            permission = (
                "can edit the project"
                if agent.permission_profile is PermissionProfile.WORKSPACE_WRITE
                else "read-only"
            )
            lines.extend(
                _render_prefixed_text(
                    f"    - {agent.label} "
                    f"[{agent.specialization.value.replace('_', ' ')}]: ",
                    f"{agent.responsibility} ({permission}; {route.model})",
                )
            )
    else:
        lines.append(
            "  Scope: "
            f"{len(implementation.requirements)} requirement(s), "
            f"{len(brief.acceptance_criteria)} acceptance check(s), "
            f"{len(plan.agents)} Agent(s)"
        )

    waves = " → ".join(" + ".join(wave) for wave in plan.execution_waves())
    lines.append(f"  Execution: {waves}")
    lines.append(
        "  Limits: "
        f"up to {plan.max_concurrency} Agent(s) at once; "
        f"{plan.iteration_limit} implementation iteration(s)"
    )
    budget = f"${plan.budget.max_estimated_cost_usd} task ceiling"
    if budget_usage is not None:
        budget += (
            f"; ${budget_usage.known_estimated_cost_usd:.6f} used in Planning; "
            f"${budget_usage.remaining_estimated_cost_usd(plan.budget):.6f} remaining"
        )
    lines.append(f"  Budget: {budget}")
    if implementation.risks:
        lines.append("  Risks:")
        for item in implementation.risks:
            lines.extend(_render_prefixed_text("    - ", item))
    else:
        lines.append("  Risks: none identified")

    if include_fixed_policy:
        lines.extend(
            (
                "  Fixed controller policy:",
                "    - isolate secrets and use least-privilege permissions",
                "    - preserve immutable evidence and fail closed",
                "    - clean up only resources proven to be SAT-owned",
                "    - deliver only a verified, accepted workspace",
            )
        )
        for item in preview.execution_profile_constraints:
            lines.extend(_render_prefixed_text("    - ", item))
    else:
        lines.append(
            "  Technical details: hidden; choose d to inspect the full task graph, "
            "bindings, routes, and evidence contract."
        )
        lines.append("  Fixed policy: hidden; choose f to inspect it.")
    return _style_planning_overview(lines, color=color)


def render_planning_overview(
    preview: PlanningPreview,
    *,
    budget_usage: AgentBudgetUsage | None = None,
    include_fixed_policy: bool = False,
    visibility: Literal["compact", "standard", "detailed"] = "detailed",
    color: bool = False,
) -> str:
    """Render task authority, with lossless fixed-policy details on request."""

    if visibility in {"compact", "standard"}:
        return _render_concise_planning_overview(
            preview,
            budget_usage=budget_usage,
            include_fixed_policy=include_fixed_policy,
            visibility="standard",
            color=color,
        )

    brief = preview.task_brief
    implementation = preview.implementation_plan
    plan = preview.team_plan
    used_boundaries = tuple(
        dict.fromkeys(
            boundary
            for criterion in brief.acceptance_criteria
            for boundary in criterion.review_boundaries
        )
    )
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
        "  Task-specific plan (requires approval):",
        "  Product definition and scope:",
    ]
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
    lines.append("  Selected execution profile (controller-owned):")
    for item in preview.execution_profile:
        lines.extend(_render_prefixed_text("    - ", item))
    lines.append("  Requirements:")
    for requirement_id, description in requirement_pairs:
        canonical_description = strip_redundant_stable_id_prefix(
            description,
            requirement_id,
        )
        lines.extend(
            _render_prefixed_text(
                f"    - {requirement_id}: ",
                canonical_description,
            )
        )
    if include_fixed_policy and preview.execution_profile_constraints:
        lines.append("  Execution-profile constraints (controller-owned):")
        for item in preview.execution_profile_constraints:
            lines.extend(_render_prefixed_text("    - ", item))
    elif not include_fixed_policy:
        lines.append(
            "  Fixed policy details: hidden "
            f"({len(preview.execution_profile_constraints)} execution-profile "
            "constraints, 4 lifecycle safeguards, "
            f"{len(used_boundaries)} Review boundary definitions)."
        )
        lines.append(
            "    - Use review choice f to show them without revising the plan."
        )
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
    if include_fixed_policy:
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
    if include_fixed_policy and used_boundaries:
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
    lines.append("  Review acceptance strategy:")
    if not preview.review_scope_by_agent:
        lines.append("    - unavailable in legacy Planning evidence")
    else:
        for agent in plan.agents:
            if agent.id not in preview.review_scope_by_agent:
                continue
            contract = specialization_contract(agent.specialization)
            scope = ", ".join(preview.review_scope_by_agent[agent.id])
            lines.append(
                f"    - {agent.id} [{agent.specialization.value}; "
                f"authority={contract.acceptance_authority.value}]: {scope}"
            )
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
                f"      specialization: {agent.specialization.value}",
                "      acceptance authority: "
                f"{specialization_contract(agent.specialization).acceptance_authority.value}",
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
                "      cost authority: metered against shared task ceiling "
                f"${plan.budget.max_estimated_cost_usd}; no per-Agent cost cap",
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
    return _style_planning_overview(lines, color=color)
