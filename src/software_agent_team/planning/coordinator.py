"""Adaptive Planning coordinator, runtime execution, and interactive loop.

This module coordinates:
    - AdaptivePlanningCoordinator: Multi-turn dialogue controller between user,
      planning LLM, and execution environments. Manages proposal revisions,
      clarification questions, semantic correction attempts, and budget tracking.
    - PlanningRouteRuntime: Multi-model fallback runtime router.
    - Interactive loop: run_interactive_planning terminal runner.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Collection, Mapping
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from string import Template
from typing import TextIO

from pydantic import ValidationError

from software_agent_team.artifacts import (
    ArtifactKind,
    ProductDefinitionDimension,
    ProductDefinitionDisposition,
    review_boundary_definition_map,
)
from software_agent_team.budgets import (
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
    AgentExecutionStatus,
    AgentExecutor,
    execution_exception_result,
)
from software_agent_team.integrity import canonical_model_sha256
from software_agent_team.model_costs import CacheTokenUsage
from software_agent_team.model_routing import (
    ModelRoutingMode,
    ModelRoutingPolicy,
)
from software_agent_team.model_runtime import (
    ModelRuntimeProfile,
    openclaw_invocation_thinking_level,
    runtime_profile_for_model,
)
from software_agent_team.planning.common import (
    _clean_text,
    _render_prefixed_text,
    _utc,
)
from software_agent_team.planning.invariants import (
    PlanningError,
    PlanningIntegrityError,
    _planning_context_invariant,
    _PlanningClarificationRecovery,
    _PlanningContextInvariantError,
    _PlanningContextInvariantsError,
    _PlanningModelInvariantError,
)
from software_agent_team.planning.models_basic import (
    AgentWorkload,
    PlanningActivity,
    PlanningActivityHandler,
    PlanningActivityKind,
    PlanningDecisionCategory,
    PlanningProposalSource,
    PlanningQuestionOrigin,
    PlanningRecoveryAction,
    PlanningResponseKind,
    PlanningSessionStatus,
    StructuredEditKind,
)
from software_agent_team.planning.presentation import TerminalPlanningProgress
from software_agent_team.planning.projections import (
    _bind_planning_correction_candidates,
    _clarification_recovery_from_diagnostic,
    _normalize_planning_response_payload,
    _planning_correction_reasons,
    _planning_invariant_diagnostic,
    _planning_invariants_diagnostic,
    _planning_validation_diagnostic,
    _preserve_agents_during_missing_specialist_correction,
    _preserve_tasks_during_writer_coverage_correction,
    _requires_full_proposal_regeneration,
    _safe_validation_detail,
)
from software_agent_team.planning.proposals import (
    PlanningModelResponse,
    _planning_proposal_body_for_model,
    _planning_question_response_schema,
    _planning_regeneration_transport_schema,
    _planning_request_prompt_context,
    _planning_response_schema,
    _planning_response_schema_for_correction,
    _validate_current_planning_response_wire,
    validate_planning_clarity,
    validate_question_admission,
)
from software_agent_team.planning.questions import (
    PlanningOptionValue,
    PlanningQuestion,
    PlanningQuestionAdmission,
    PlanningQuestionAnswer,
    PlanningRequest,
    PresentedPlanningQuestion,
    _PlanningQuestionContract,
)
from software_agent_team.planning.session import (
    RECOVERABLE_PLANNING_EXECUTION_STATUSES,
    ApprovedPlanningResult,
    Clock,
    InputReader,
    OutputWriter,
    PlanningApproval,
    PlanningPolicy,
    PlanningPreview,
    PlanningProposal,
    PlanningTurn,
    QuestionAnswerer,
    StructuredPlanEdit,
    _normalize_question_answer,
    _system_clock,
    apply_structured_edit,
    preview_adaptive_proposal,
    render_planning_overview,
)
from software_agent_team.planning.store import PlanningStore
from software_agent_team.progress import (
    RunEventVisibility,
    TerminalColorMode,
    TerminalProgressDisplay,
)
from software_agent_team.response_corrections import (
    ResponseFailureClass,
    ResponseIssueAuthority,
    ResponseValidationDiagnostic,
    SemanticCorrectionOutcome,
    SemanticCorrectionPlan,
    SemanticCorrectionSubmissionError,
    apply_semantic_correction_with_evidence,
    build_semantic_correction_plan,
    correction_outcome,
    correction_prompt,
    deterministically_remove_forbidden_fields,
    diagnostic_from_invariant,
    diagnostic_from_transport,
    require_all_semantic_correction_targets,
    semantic_correction_schema,
)
from software_agent_team.responses import AgentArtifactResponseError
from software_agent_team.submissions import (
    ARTIFACT_SUBMISSION_TOOL,
    RECOVERABLE_SUBMISSION_EVIDENCE_CODES,
    AgentSubmissionContract,
    AgentSubmissionPurpose,
)
from software_agent_team.teams import (
    SPECIALIZATION_CATALOG,
    SPECIALIZATION_CATALOG_VERSION,
    AgentCapability,
    AgentRole,
    AgentSpecialization,
)
from software_agent_team.terminal_metrics import TerminalRunMetrics

SUBMISSION_EVIDENCE_RECOVERY_LIMIT = 1
_TEMPLATE_PARENT = (
    Path(__file__).parent.parent
    if Path(__file__).parent.name == "planning"
    else Path(__file__).parent
)
PLANNING_TEMPLATE = _TEMPLATE_PARENT / "prompt_templates" / "adaptive_planner.md"


@dataclass(frozen=True)
class _Invocation:
    response: PlanningModelResponse
    turn: PlanningTurn

    def presented_question(self) -> PresentedPlanningQuestion:
        """Bind a model question to the Controller admission stored with its turn."""

        question = self.response.question
        admission = self.turn.question_admission
        if question is None or admission is None:
            raise PlanningIntegrityError(
                "Planning question is missing Controller admission evidence"
            )
        return PresentedPlanningQuestion.model_validate(
            {
                **question.model_dump(mode="json"),
                "admission": admission.model_dump(mode="json"),
            }
        )


@dataclass(frozen=True)
class PlanningRouteRuntime:
    """One authorized bootstrap route and its isolated execution boundary."""

    route_id: str
    model: str
    executor: AgentExecutor
    pricing: ModelPricing
    runtime_profile: ModelRuntimeProfile

    def __post_init__(self) -> None:
        if re.fullmatch(r"[a-z][a-z0-9_]*", self.route_id) is None:
            raise ValueError("Planning route ID is invalid")
        if self.pricing.model != self.model:
            raise ValueError("Planning route pricing belongs to another model")
        provider, separator, leaf = self.model.partition("/")
        if (
            not separator
            or not provider
            or not leaf
            or self.runtime_profile.provider_id != provider
        ):
            raise ValueError("Planning route runtime profile differs from its model")


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
        runtime_profile: ModelRuntimeProfile | None = None,
        fallback_routes: tuple[PlanningRouteRuntime, ...] = (),
        provider_failure_fallback: bool = False,
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
        if fallback_routes and not provider_failure_fallback:
            raise ValueError(
                "Planning fallback routes require provider-failure authority"
            )
        if fallback_routes and (
            budget_ledger is None
            or pricing is None
            or route_id is None
            or runtime_profile is None
        ):
            raise ValueError(
                "Planning fallback routes require an attributable primary route"
            )
        route_ids = (() if route_id is None else (route_id,)) + tuple(
            route.route_id for route in fallback_routes
        )
        if len(route_ids) != len(set(route_ids)):
            raise ValueError("Planning route IDs must be unique")
        fallback_models = tuple(route.model for route in fallback_routes)
        if len(fallback_models) != len(set(fallback_models)):
            raise ValueError("Planning fallback models must be unique")
        self.executor = executor
        self.store = store
        self.policy = policy
        self.budget_ledger = budget_ledger
        self.pricing = pricing
        self.route_id = route_id
        self.runtime_profile = runtime_profile
        self.fallback_routes = fallback_routes
        self.provider_failure_fallback = provider_failure_fallback
        self._active_route_index = 0
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
        try:
            return self._start_dialogue(
                request,
                answer_question=answer_question,
                activity_handler=activity_handler,
            )
        except PlanningError:
            self.store.fail(request.run_id, now=self.clock())
            raise

    def _start_dialogue(
        self,
        request: PlanningRequest,
        *,
        answer_question: QuestionAnswerer,
        activity_handler: PlanningActivityHandler | None,
    ) -> PlanningProposal | None:
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
            presented_question = invocation.presented_question()
            answer_value = answer_question(presented_question)
            if answer_value is None:
                self.store.cancel(request.run_id, now=self.clock())
                return None
            answer = _normalize_question_answer(
                presented_question,
                answer_value,
            )
            transcript.append(
                {
                    "question": response.question.model_dump(mode="json"),
                    "question_admission": presented_question.admission.model_dump(
                        mode="json"
                    ),
                    "answer": answer.text,
                    "selected_option_id": answer.selected_option_id,
                    "product_definition_values": [
                        item.model_dump(mode="json")
                        for item in answer.product_definition_values
                    ],
                }
            )
            user_message = answer.text
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

        try:
            return self._revise_dialogue(
                request,
                proposal,
                change_request,
                answer_question=answer_question,
                activity_handler=activity_handler,
            )
        except PlanningError:
            session = self.store.load_session(request.run_id)
            if session.status is PlanningSessionStatus.CLARIFYING:
                self.store.restore_proposed_revision(
                    request.run_id,
                    proposal.revision,
                    now=self.clock(),
                )
            raise

    def _revise_dialogue(
        self,
        request: PlanningRequest,
        proposal: PlanningProposal,
        change_request: str,
        *,
        answer_question: QuestionAnswerer,
        activity_handler: PlanningActivityHandler | None,
    ) -> PlanningProposal | None:
        self._require_persisted_proposal(request, proposal)
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
                self._validate_preview(
                    request, revision, additional_user_inputs=(change_request,)
                )
                self.store.append_proposal(revision, now=self.clock())
                return revision
            assert response.question is not None
            if (
                self.policy.max_clarification_rounds is not None
                and clarification_rounds >= self.policy.max_clarification_rounds
            ):
                raise PlanningError("Planning revision exceeded its question limit")
            presented_question = invocation.presented_question()
            answer_value = answer_question(presented_question)
            if answer_value is None:
                self.store.cancel(request.run_id, now=self.clock())
                return None
            answer = _normalize_question_answer(
                presented_question,
                answer_value,
            )
            transcript.append(
                {
                    "question": response.question.model_dump(mode="json"),
                    "question_admission": presented_question.admission.model_dump(
                        mode="json"
                    ),
                    "answer": answer.text,
                    "selected_option_id": answer.selected_option_id,
                    "product_definition_values": [
                        item.model_dump(mode="json")
                        for item in answer.product_definition_values
                    ],
                }
            )
            user_message = answer.text
            clarification_rounds += 1

    def structured_edit(
        self,
        request: PlanningRequest,
        proposal: PlanningProposal,
        edit: StructuredPlanEdit,
    ) -> PlanningProposal:
        """Validate and persist one safe non-model proposal revision."""

        self._require_persisted_proposal(request, proposal)
        if (
            self.policy.max_proposal_revisions is not None
            and proposal.revision >= self.policy.max_proposal_revisions
        ):
            raise PlanningError("Planning reached its proposal-revision limit")
        revision = apply_structured_edit(proposal, edit, created_at=self.clock())
        self._validate_preview(request, revision)
        self.store.append_proposal(revision, now=self.clock())
        return revision

    def _require_persisted_proposal(
        self, request: PlanningRequest, proposal: PlanningProposal
    ) -> None:
        if proposal.run_id != request.run_id:
            raise PlanningIntegrityError(
                "proposal belongs to a different Planning request"
            )
        persisted = self.store.load_proposal(request.run_id, proposal.revision)
        if persisted != proposal:
            raise PlanningIntegrityError(
                "Planning proposal differs from its stored revision"
            )

    def preview(
        self,
        request: PlanningRequest,
        proposal: PlanningProposal,
        *,
        created_at: datetime | None = None,
    ) -> PlanningPreview:
        """Validate a stored revision against its complete user-input history."""

        self._require_persisted_proposal(request, proposal)
        return self._validate_preview(request, proposal, created_at=created_at)

    def approve(
        self,
        request: PlanningRequest,
        proposal: PlanningProposal,
    ) -> ApprovedPlanningResult:
        """Freeze the exact validated TeamPlan only after explicit user approval."""

        approved_at = _utc(self.clock())
        preview = self.preview(
            request,
            proposal,
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
        *,
        additional_user_inputs: Collection[str] = (),
        created_at: datetime | None = None,
    ) -> PlanningPreview:
        try:
            return preview_adaptive_proposal(
                request,
                proposal,
                self.policy,
                created_at=proposal.created_at if created_at is None else created_at,
                additional_user_inputs=(
                    *self.store.load_user_revision_inputs(
                        request, through_revision=proposal.revision
                    ),
                    *additional_user_inputs,
                ),
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
                    required_decision_id=decision.id,
                )
        for entry in transcript:
            question = PlanningQuestion.model_validate(entry["question"])
            admission_payload = entry.get("question_admission")
            admission = (
                None
                if admission_payload is None
                else PlanningQuestionAdmission.model_validate(admission_payload)
            )
            answer = entry.get("answer")
            if not isinstance(answer, str) or not answer.strip():
                raise PlanningError(
                    "persisted Planning transcript has an invalid user answer"
                )
            answer_values_payload = entry.get("product_definition_values", ())
            if not isinstance(answer_values_payload, (list, tuple)):
                raise PlanningError(
                    "persisted Planning transcript has invalid product values"
                )
            answer_values = tuple(
                PlanningOptionValue.model_validate(item)
                for item in answer_values_payload
            )
            if any(
                item.dimension not in question.product_definition_dimensions
                for item in answer_values
            ):
                raise PlanningError(
                    "persisted Planning answer exceeds its question dimension scope"
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
                answer_dimension_values=tuple(
                    (item.dimension, item.value) for item in answer_values
                ),
                required_decision_id=(
                    None if admission is None else admission.controller_decision_id
                ),
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
        question_contracts = self._question_contracts(transcript, current_proposal)
        previous_user_revisions = self.store.load_user_revision_inputs(request)
        additional_user_inputs = (
            *previous_user_revisions,
            *(() if change_request is None else (change_request,)),
        )
        correction_plan: SemanticCorrectionPlan | None = None
        proposal_regeneration_diagnostic: ResponseValidationDiagnostic | None = None
        proposal_regenerations = 0
        clarification_recovery: _PlanningClarificationRecovery | None = None
        seen_correction_fingerprints: set[str] = set()
        semantic_corrections = 0
        maximum_attempts = (
            None
            if self.policy.response_repair_limit is None
            else self.policy.response_repair_limit + 1
        )
        attempt = 1
        evidence_recoveries = 0
        route_index = self._active_route_index
        if any(route.model == request.model for route in self.fallback_routes):
            raise PlanningError("Planning fallback route duplicates the primary model")
        while True:
            if route_index == 0:
                active_executor = self.executor
                active_model = request.model
                active_pricing = self.pricing
                active_route_id = self.route_id
                active_runtime_profile = self.runtime_profile
            else:
                active_route = self.fallback_routes[route_index - 1]
                active_executor = active_route.executor
                active_model = active_route.model
                active_pricing = active_route.pricing
                active_route_id = active_route.route_id
                active_runtime_profile = active_route.runtime_profile
            if active_pricing is not None and active_pricing.model != active_model:
                raise PlanningError("Planning route pricing differs from its model")
            base_response_schema = (
                _planning_response_schema()
                if clarification_recovery is None
                else _planning_question_response_schema(clarification_recovery)
            )
            response_schema = (
                base_response_schema
                if correction_plan is None
                else _planning_response_schema_for_correction(
                    correction_plan,
                    base_schema=base_response_schema,
                    profile_criterion_ids=tuple(
                        criterion.id
                        for criterion in self.policy.profile_acceptance_criteria
                    ),
                )
            )
            prompt = self._prompt(
                request,
                transcript=transcript,
                current_proposal=current_proposal,
                change_request=change_request,
                previous_user_revisions=previous_user_revisions,
                correction_plan=correction_plan,
                proposal_regeneration_diagnostic=proposal_regeneration_diagnostic,
                clarification_recovery=clarification_recovery,
                response_schema=response_schema,
            )
            regenerating_proposal = proposal_regeneration_diagnostic is not None
            proposal_regeneration_diagnostic = None
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
                    _planning_regeneration_transport_schema(response_schema)
                    if regenerating_proposal
                    else None
                ),
            )
            # ``attempt`` restarts for each dialogue or revision. Persisted turn
            # order keeps every invocation isolated across correction, bounded
            # evidence recovery, and revision boundaries.
            session_generation = self.store.load_session(request.run_id).turn_count + 1
            execution_request = AgentExecutionRequest(
                run_id=request.run_id,
                team_id="adaptive_planning",
                iteration=1,
                role=AgentRole.CLARIFIER,
                expected_kind=ArtifactKind.CLARIFICATION_RECORD,
                prompt=prompt,
                timeout_seconds=self.policy.planning_timeout_seconds,
                model=active_model,
                thinking_level=openclaw_invocation_thinking_level(
                    active_runtime_profile or runtime_profile_for_model(active_model)
                ),
                submission_contract=submission_contract,
                session_generation=session_generation,
            )
            self._emit_activity(
                activity_handler,
                PlanningActivity(
                    kind=PlanningActivityKind.WAITING_MODEL,
                    attempt=attempt,
                    maximum_attempts=maximum_attempts,
                    model=active_model,
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
                    route_id=active_route_id,
                    pricing=active_pricing,
                )
            )

            def observe_execution_activity(
                execution_activity: AgentExecutionActivity,
                *,
                current_attempt: int = attempt,
                current_model: str = active_model,
            ) -> None:
                self._emit_execution_activity(
                    activity_handler,
                    execution_activity,
                    attempt=current_attempt,
                    maximum_attempts=maximum_attempts,
                    model=current_model,
                )

            execution_started_at = _utc(self.clock())
            execution_exception: BaseException | None = None
            try:
                result = active_executor.execute(
                    execution_request,
                    activity_handler=observe_execution_activity,
                )
            except BaseException as error:
                execution_exception = error
                result = execution_exception_result(
                    execution_request,
                    error,
                    started_at=execution_started_at,
                    finished_at=_utc(self.clock()),
                )
            self._emit_activity(
                activity_handler,
                PlanningActivity(
                    kind=PlanningActivityKind.RESPONSE_RECEIVED,
                    attempt=attempt,
                    maximum_attempts=maximum_attempts,
                    model=active_model,
                    duration_ms=result.telemetry.duration_ms,
                    execution_status=result.status,
                ),
            )
            parsed: PlanningModelResponse | None = None
            question_admission: PlanningQuestionAdmission | None = None
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
            next_clarification_recovery: _PlanningClarificationRecovery | None = None
            estimated_cost: Decimal | None = None
            budget_usage: AgentBudgetUsage | None = None
            budget_error: str | None = None
            cost_record: ModelCallCostRecord | None = None
            if self.budget_ledger is not None and reservation is not None:
                assert active_pricing is not None
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
                estimated_cost = cost_record.cost_usd
                self._emit_activity(
                    activity_handler,
                    PlanningActivity(
                        kind=PlanningActivityKind.BUDGET_UPDATED,
                        attempt=attempt,
                        maximum_attempts=maximum_attempts,
                        model=active_model,
                        budget_usage=budget_usage,
                        budget_ceiling_usd=(
                            self.budget_ledger.budget.max_estimated_cost_usd
                        ),
                        pricing_source=active_pricing.pricing_source,
                    ),
                )
            if result.status is not AgentExecutionStatus.COMPLETED:
                validation_error = _safe_validation_detail(
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
                            response_schema=response_schema,
                        )
                        (
                            task_preserved_payload,
                            task_preservation_normalizations,
                        ) = _preserve_tasks_during_writer_coverage_correction(
                            application.payload,
                            correction_plan,
                        )
                        (
                            payload,
                            specialist_preservation_normalizations,
                        ) = _preserve_agents_during_missing_specialist_correction(
                            task_preserved_payload,
                            correction_plan,
                            profile_criterion_ids=(
                                criterion.id
                                for criterion in self.policy.profile_acceptance_criteria
                            ),
                        )
                        correction_binding_normalizations = (
                            *application.normalizations,
                            *task_preservation_normalizations,
                            *specialist_preservation_normalizations,
                        )
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
                                *additional_user_inputs,
                            ),
                            question_answers={
                                question_id: contract.answer
                                for question_id, contract in question_contracts.items()
                                if contract.answer is not None
                            },
                            question_dimension_values={
                                question_id: dict(contract.answer_dimension_values)
                                for question_id, contract in question_contracts.items()
                                if contract.answer_dimension_values
                            },
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
                    if (
                        clarification_recovery is not None
                        and parsed.kind is not PlanningResponseKind.QUESTION
                    ):
                        clarification_scope = (
                            clarification_recovery.dimension.value
                            if clarification_recovery.dimension is not None
                            else clarification_recovery.question_id
                            or clarification_recovery.decision_category.value
                        )
                        raise _planning_context_invariant(
                            "planning_required_clarification_response",
                            (
                                "Planning must ask the requested user-owned "
                                f"{clarification_scope} question "
                                "before returning a proposal"
                            ),
                            paths=("/",),
                            authority=ResponseIssueAuthority.CONTROLLER,
                        )
                    if parsed.kind is PlanningResponseKind.QUESTION:
                        assert parsed.question is not None
                        validate_question_admission(
                            parsed.question,
                            previous_question_ids=question_contracts,
                        )
                        question_admission = PlanningQuestionAdmission(
                            origin=(
                                PlanningQuestionOrigin.PLANNER_SUGGESTION
                                if clarification_recovery is None
                                else PlanningQuestionOrigin.CONTROLLER_REQUIREMENT
                            ),
                            product_definition_dimensions=(
                                parsed.question.product_definition_dimensions
                            ),
                            controller_invariant_ids=(
                                ()
                                if clarification_recovery is None
                                else clarification_recovery.invariant_ids
                            ),
                            controller_decision_id=(
                                None
                                if clarification_recovery is None
                                else clarification_recovery.decision_id
                            ),
                        )
                    else:
                        assert parsed.proposal is not None
                        validate_planning_clarity(
                            parsed.proposal,
                            source_request=request.source_request,
                            additional_user_inputs=additional_user_inputs,
                            question_contracts=question_contracts,
                            allowed_criterion_ids=(
                                criterion.id
                                for criterion in self.policy.profile_acceptance_criteria
                            ),
                            enforce_specialized_review_authority=True,
                            enforce_test_suite_writer_ownership=True,
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
                        self._validate_preview(
                            request,
                            candidate,
                            additional_user_inputs=(
                                () if change_request is None else (change_request,)
                            ),
                        )
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
                except SemanticCorrectionSubmissionError as error:
                    validation_error = _safe_validation_detail(error)
                    response_validation = error.diagnostic
                    response_normalizations = error.normalizations
                    next_correction_plan = error.recovery_plan
                    current_correction_outcome = (
                        SemanticCorrectionOutcome.IMPROVED
                        if next_correction_plan is not None
                        else SemanticCorrectionOutcome.INVALID_SUBMISSION
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
                    next_clarification_recovery = (
                        _clarification_recovery_from_diagnostic(
                            response_validation,
                            payload=payload,
                        )
                    )
                    if correction_plan is None:
                        if next_clarification_recovery is None and payload is not None:
                            next_correction_plan = _bind_planning_correction_candidates(
                                build_semantic_correction_plan(
                                    payload,
                                    response_validation,
                                )
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
                            and next_clarification_recovery is None
                            and payload is not None
                        ):
                            next_correction_plan = _bind_planning_correction_candidates(
                                build_semantic_correction_plan(
                                    payload,
                                    response_validation,
                                )
                            )
                            seen_correction_fingerprints.add(
                                response_validation.fingerprint
                            )
                elif parsed is not None and correction_plan is not None:
                    current_correction_outcome = SemanticCorrectionOutcome.ACCEPTED
            safe_unaccepted_submission = (
                result.semantic_submission is None
                and result.submission_evidence is not None
                and result.submission_evidence.diagnostic_code
                in RECOVERABLE_SUBMISSION_EVIDENCE_CODES
                and (
                    result.telemetry.invocation_lifecycle is None
                    or result.telemetry.invocation_lifecycle.shutdown.cleanup_completed
                )
            )
            fallback_scheduled = (
                execution_exception is None
                and result.status is AgentExecutionStatus.PROVIDER_FAILED
                and budget_error is None
                and safe_unaccepted_submission
                and self.provider_failure_fallback
                and route_index < len(self.fallback_routes)
            )
            evidence_recovery_scheduled = (
                execution_exception is None
                and result.status in RECOVERABLE_PLANNING_EXECUTION_STATUSES
                and budget_error is None
                and safe_unaccepted_submission
                and evidence_recoveries < SUBMISSION_EVIDENCE_RECOVERY_LIMIT
            )
            recovery_action = (
                PlanningRecoveryAction.FALLBACK_ROUTE
                if fallback_scheduled
                else PlanningRecoveryAction.SAME_ROUTE_RETRY
                if evidence_recovery_scheduled
                else None
            )
            turn = self.store.append_turn(
                run_id=request.run_id,
                user_message=user_message,
                prompt=prompt,
                result=result,
                parsed_response=parsed,
                question_admission=question_admission,
                response_normalizations=response_normalizations,
                validation_error=validation_error,
                response_validation=response_validation,
                semantic_correction_request=correction_request,
                semantic_correction_outcome=current_correction_outcome,
                now=self.clock(),
                estimated_cost_usd=estimated_cost,
                pricing_source=(
                    None if active_pricing is None else active_pricing.pricing_source
                ),
                budget_usage=budget_usage,
                budget_error=budget_error,
                cost_record=cost_record,
                recovery_action=recovery_action,
            )
            if execution_exception is not None:
                raise execution_exception
            if result.status is AgentExecutionStatus.INTERRUPTED:
                raise KeyboardInterrupt
            if result.status is not AgentExecutionStatus.COMPLETED:
                if fallback_scheduled:
                    next_route = self.fallback_routes[route_index]
                    route_index += 1
                    self._active_route_index = route_index
                    self._emit_activity(
                        activity_handler,
                        PlanningActivity(
                            kind=PlanningActivityKind.MODEL_ROUTE_SWITCHED,
                            attempt=attempt,
                            maximum_attempts=maximum_attempts,
                            model=next_route.model,
                        ),
                    )
                    attempt += 1
                    continue
                if evidence_recovery_scheduled:
                    # Nothing was accepted, so the identical request is reissued
                    # without consuming the semantic-correction budget. The failed
                    # invocation, its usage and its cost are already settled in the
                    # persisted turn above.
                    evidence_recoveries += 1
                    self._emit_activity(
                        activity_handler,
                        PlanningActivity(
                            kind=PlanningActivityKind.SUBMISSION_EVIDENCE_RECOVERY,
                            attempt=attempt,
                            maximum_attempts=maximum_attempts,
                            model=active_model,
                        ),
                    )
                    attempt += 1
                    continue
                raise PlanningError(result.error or "Planning invocation failed")
            if budget_error is not None:
                raise PlanningError(budget_error)
            if parsed is not None:
                self._emit_activity(
                    activity_handler,
                    PlanningActivity(
                        kind=PlanningActivityKind.RESPONSE_VALIDATED,
                        attempt=attempt,
                        maximum_attempts=maximum_attempts,
                        model=active_model,
                    ),
                )
                return _Invocation(response=parsed, turn=turn)
            if next_clarification_recovery is not None:
                correction_plan = None
                clarification_recovery = next_clarification_recovery
                self._emit_activity(
                    activity_handler,
                    PlanningActivity(
                        kind=PlanningActivityKind.CLARIFICATION_SCHEDULED,
                        attempt=attempt,
                        maximum_attempts=maximum_attempts,
                        model=active_model,
                        clarification_dimension=(next_clarification_recovery.dimension),
                        clarification_category=(
                            next_clarification_recovery.decision_category
                        ),
                    ),
                )
                attempt += 1
                continue
            correction_allowed = next_correction_plan is not None and (
                self.policy.response_repair_limit is None
                or semantic_corrections < self.policy.response_repair_limit
            )
            if not correction_allowed:
                if (
                    next_correction_plan is not None
                    and proposal_regenerations
                    and _requires_full_proposal_regeneration(next_correction_plan)
                ):
                    raise PlanningError(
                        "Planning proposal remains structurally invalid after one "
                        "complete regeneration; select a stronger Planning model "
                        "or simplify the request"
                    )
                raise PlanningError(
                    f"Planning response remained invalid: {validation_error}"
                )
            if _requires_full_proposal_regeneration(next_correction_plan):
                if proposal_regenerations:
                    raise PlanningError(
                        "Planning proposal remains structurally invalid after one "
                        "complete regeneration; select a stronger Planning model "
                        "or simplify the request"
                    )
                proposal_regenerations += 1
                semantic_corrections += 1
                proposal_regeneration_diagnostic = next_correction_plan.diagnostic
                correction_plan = None
                self._emit_activity(
                    activity_handler,
                    PlanningActivity(
                        kind=PlanningActivityKind.PROPOSAL_REGENERATION_SCHEDULED,
                        attempt=attempt,
                        maximum_attempts=maximum_attempts,
                        model=active_model,
                    ),
                )
                attempt += 1
                continue
            semantic_corrections += 1
            correction_plan = (
                require_all_semantic_correction_targets(next_correction_plan)
                if (
                    self.policy.response_repair_limit is not None
                    and semantic_corrections == self.policy.response_repair_limit
                )
                else next_correction_plan
            )
            self._emit_activity(
                activity_handler,
                PlanningActivity(
                    kind=PlanningActivityKind.CORRECTION_SCHEDULED,
                    attempt=attempt,
                    maximum_attempts=maximum_attempts,
                    model=active_model,
                    correction_reasons=_planning_correction_reasons(
                        correction_plan.diagnostic
                    ),
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
                presentation=activity.presentation,
            ),
        )

    def _prompt(
        self,
        request: PlanningRequest,
        *,
        transcript: list[dict[str, object]],
        current_proposal: PlanningProposal | None,
        change_request: str | None,
        previous_user_revisions: tuple[str, ...],
        correction_plan: SemanticCorrectionPlan | None,
        proposal_regeneration_diagnostic: ResponseValidationDiagnostic | None,
        clarification_recovery: _PlanningClarificationRecovery | None,
        response_schema: dict[str, object],
    ) -> str:
        context = {
            "request": _planning_request_prompt_context(request),
            "dialogue": transcript,
            "current_proposal": (
                None
                if current_proposal is None
                else _planning_proposal_body_for_model(current_proposal.body)
            ),
            "change_request": change_request,
            "previous_user_revisions": previous_user_revisions,
            "required_clarification": (
                None
                if clarification_recovery is None
                else {
                    "question_id": clarification_recovery.question_id,
                    "decision_id": clarification_recovery.decision_id,
                    "decision_category": (
                        clarification_recovery.decision_category.value
                    ),
                    "invalid_model_decision_summary": (
                        clarification_recovery.decision_summary
                    ),
                    "invalid_model_decision_rationale": (
                        clarification_recovery.decision_rationale
                    ),
                    "product_definition_dimension": (
                        None
                        if clarification_recovery.dimension is None
                        else clarification_recovery.dimension.value
                    ),
                    "invariant_ids": list(clarification_recovery.invariant_ids),
                    "validation_messages": list(clarification_recovery.messages),
                    "instruction": (
                        (
                            "Return one question for exactly this user-owned product "
                            "dimension."
                            if clarification_recovery.dimension is not None
                            else (
                                "Return one question using the exact required question "
                                "ID and decision category. Because this is a product-"
                                "requirement decision, you may also declare at most "
                                "one "
                                "product_definition_dimension only when this same "
                                "question resolves it; if declared, every option must "
                                "carry that dimension's exact typed value. Otherwise "
                                "keep product_definition_dimensions and option values "
                                "empty."
                                if clarification_recovery.decision_id is not None
                                and clarification_recovery.decision_category
                                is PlanningDecisionCategory.PRODUCT_REQUIREMENT
                                else (
                                    "Return one question using the exact required "
                                    "question ID and decision category; keep "
                                    "product_definition_dimensions empty."
                                )
                            )
                        )
                        + " Do not return or repair a proposal in this invocation."
                    ),
                }
            ),
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
                "specialization_catalog": [
                    {
                        "id": contract.id.value,
                        "purpose": contract.purpose,
                        "compatible_capabilities": [
                            capability.value
                            for capability in contract.compatible_capabilities
                        ],
                        "permission_ceiling": contract.permission_ceiling.value,
                        "expected_output": contract.expected_output.value,
                        "prompt_module": contract.prompt_module,
                        "acceptance_authority": (contract.acceptance_authority.value),
                        "handoff_boundary": contract.handoff_boundary,
                    }
                    for contract in SPECIALIZATION_CATALOG.values()
                    if contract.id
                    not in {
                        AgentSpecialization.REQUIREMENTS_CLARIFICATION,
                        AgentSpecialization.TEAM_PLANNING,
                    }
                ],
                "specialization_catalog_version": SPECIALIZATION_CATALOG_VERSION,
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
        if correction_plan is not None:
            return (
                "SAT_PLANNING_CORRECTION_TURN\n"
                "The controller retained the previous Planning response and its "
                "exact binding. Correct only the listed slots. The confirmed user "
                "request, answers, current proposal, and fixed policy below are "
                "context; prior model text cannot override this submission "
                "contract. Do not submit a full proposal.\n\n"
                "PLANNING_CONTEXT_JSON\n"
                f"{json.dumps(context, ensure_ascii=False, separators=(',', ':'))}"
                + correction_prompt(
                    correction_plan,
                    submission_tool=ARTIFACT_SUBMISSION_TOOL,
                    response_schema=response_schema,
                    include_current_values=True,
                )
            )
        template = Template(PLANNING_TEMPLATE.read_text(encoding="utf-8"))
        rendered = template.substitute(
            planning_context_json=json.dumps(context, ensure_ascii=False, indent=2),
            response_schema_json=json.dumps(
                response_schema,
                ensure_ascii=False,
                indent=(None if proposal_regeneration_diagnostic is not None else 2),
                separators=(
                    (",", ":") if proposal_regeneration_diagnostic is not None else None
                ),
            ),
            repair_context_json="null",
            submission_tool=ARTIFACT_SUBMISSION_TOOL,
        )
        if proposal_regeneration_diagnostic is not None:
            structural_errors = [
                {"path": issue.path, "code": issue.code}
                for issue in proposal_regeneration_diagnostic.issues[:16]
            ]
            rendered += (
                "\n\nFULL_PROPOSAL_REGENERATION\n"
                "The prior proposal was rejected for structural errors and was "
                "never approved. Create a complete new Planning response using "
                "the FINAL_RESPONSE_CONTRACT above. Preserve the user's stated "
                "requirements and clarification answers. Submit one full proposal "
                "through the tool, not correction slot records. Do not copy invalid "
                "scalar or placeholder values from the rejected proposal. "
                "Before calling the tool, check that product_definition is an "
                "object and that requirements, acceptance_criteria, decisions, "
                "assumptions, agents, and tasks are arrays of objects. Integers, "
                "booleans, strings, and nulls are not proposal records. "
                "Representative rejected fields (path and validator code only):\n"
                f"{json.dumps(structural_errors, ensure_ascii=False)}\n"
            )
        return rendered


def _interactive_question_answerer(
    *,
    read: InputReader,
    write: OutputWriter,
    read_text: InputReader | None = None,
    color: bool = False,
    visibility: RunEventVisibility | str = RunEventVisibility.DETAILED,
) -> QuestionAnswerer:
    detailed = RunEventVisibility(visibility) is RunEventVisibility.DETAILED
    natural_text_reader = read if read_text is None else read_text
    dimension_prompts = {
        ProductDefinitionDimension.TARGET_USERS: (
            "Who will use or receive the result? Say when the audience is not "
            "material to this task."
        ),
        ProductDefinitionDimension.PRIMARY_WORKFLOW: (
            "What end-to-end activity should the result support?"
        ),
        ProductDefinitionDimension.DELIVERY_MATURITY: (
            "Should SAT target a throwaway prototype, usable local product, or "
            "releasable small product?"
        ),
        ProductDefinitionDimension.USABILITY_EXPECTATIONS: (
            "What usability outcome is material for this result?"
        ),
        ProductDefinitionDimension.OPERATIONAL_EXPECTATIONS: (
            "What operational behavior is material for this result?"
        ),
        ProductDefinitionDimension.DELIVERY_EXPECTATIONS: (
            "What must be included in the delivered result?"
        ),
    }

    def answer(
        question: PresentedPlanningQuestion,
    ) -> PlanningQuestionAnswer | None:
        admission = (
            question.admission
            if isinstance(question, PresentedPlanningQuestion)
            else PlanningQuestionAdmission(
                origin=PlanningQuestionOrigin.PLANNER_SUGGESTION,
                product_definition_dimensions=(question.product_definition_dimensions),
            )
        )
        write("")
        write(
            "\x1b[1;36mPlanning clarification\x1b[0m"
            if color
            else "Planning clarification"
        )
        write("  " + "─" * 58 if color else "  " + "-" * 58)
        assert question.decision_category is not None
        assert question.decision_owner is not None
        write(
            "Decision boundary: "
            + (
                f"{question.decision_category.value} / {question.decision_owner.value}"
                if detailed
                else question.decision_category.value.replace("_", " ")
                + " / your choice"
            )
        )
        if question.product_definition_dimensions:
            decision_scope = (
                ""
                if admission.controller_decision_id is None or not detailed
                else (
                    f"{question.decision_category.value} / "
                    f"{admission.controller_decision_id}; product definition: "
                )
            )
            write(
                "Decision scope: "
                + decision_scope
                + ", ".join(
                    dimension.value if detailed else dimension.value.replace("_", " ")
                    for dimension in question.product_definition_dimensions
                )
            )
        elif admission.controller_decision_id is not None and detailed:
            write(
                "Decision scope: "
                f"{question.decision_category.value} / "
                f"{admission.controller_decision_id}"
            )
        if admission.origin is PlanningQuestionOrigin.CONTROLLER_REQUIREMENT:
            write(
                "Question source: Controller validation requires this user-owned "
                "decision before the plan can be approved."
            )
            if detailed:
                write(
                    "Controller invariants: "
                    + ", ".join(admission.controller_invariant_ids)
                )
        else:
            write(
                "Question source: Planner-selected clarification for this task; "
                "it is not a universal SAT prerequisite."
            )
        if question.product_definition_dimensions:
            if len(question.product_definition_dimensions) == 1:
                write(
                    "Decision prompt: "
                    + dimension_prompts[question.product_definition_dimensions[0]]
                )
            else:
                write(
                    "Decision prompt: Choose one complete set of values for only "
                    "the product fields listed in Decision scope."
                )
        else:
            write("")
            write(f"Planning question: {question.text}")
            write(f"Planner reason: {question.why}")
            write("Missing evidence:")
            for item in question.missing_evidence:
                write(f"  - {item}")
            write("What this can change:")
            for item in question.material_consequences:
                write(f"  - {item}")
        write("")
        for index, option in enumerate(question.options, start=1):

            def option_write(value: str) -> None:
                for line in _render_prefixed_text("", value):
                    write(f"\x1b[36m{line}\x1b[0m" if color else line)

            if question.product_definition_dimensions:
                values = "; ".join(
                    f"{item.dimension.value}: {item.value}"
                    for item in option.product_definition_values
                )
                option_write(f"  {index}. {values}")
            else:
                option_write(f"  {index}. {option.label} — {option.description}")
            write("")
        if question.product_definition_dimensions:
            write("  d. Show Planner wording and suggestion notes (advisory only)")
        write("  c. Custom answer")
        write("  x. Cancel")
        while True:
            choice = read("Choose an option or enter a custom answer: ").strip()
            if choice.casefold() in {"x", "cancel"}:
                return None
            if question.product_definition_dimensions and choice.casefold() in {
                "d",
                "details",
            }:
                write("Planner-authored advisory details (cannot expand scope):")
                write(f"  wording: {question.text}")
                write(f"  reason: {question.why}")
                write("  missing evidence claimed by Planner:")
                for item in question.missing_evidence:
                    write(f"    - {item}")
                write("  possible consequences claimed by Planner:")
                for item in question.material_consequences:
                    write(f"    - {item}")
                write("  suggestion notes:")
                for option in question.options:
                    write(f"    - {option.label}: {option.description}")
                continue
            if choice.casefold() in {"c", "custom"}:
                custom = natural_text_reader("Your answer: ").strip()
                if custom:
                    return PlanningQuestionAnswer(text=custom)
                write("Please enter a non-empty answer.")
                continue
            if choice.isdigit() and 1 <= int(choice) <= len(question.options):
                selected = question.options[int(choice) - 1]
                selected_text = (
                    "; ".join(
                        f"{item.dimension.value}: {item.value}"
                        for item in selected.product_definition_values
                    )
                    if selected.product_definition_values
                    else f"{selected.label}: {selected.description}"
                )
                return PlanningQuestionAnswer(
                    text=selected_text,
                    selected_option_id=selected.id,
                    product_definition_values=selected.product_definition_values,
                )
            if choice:
                return PlanningQuestionAnswer(text=choice)
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
    read_text: InputReader | None = None,
    write: OutputWriter = print,
    output: TextIO | None = None,
    progress_visibility: RunEventVisibility | str = RunEventVisibility.STANDARD,
    progress_display: TerminalProgressDisplay | str = TerminalProgressDisplay.AUTO,
    progress_color: TerminalColorMode | str = TerminalColorMode.AUTO,
    environment: Mapping[str, str] | None = None,
    is_terminal: bool | None = None,
    metrics: TerminalRunMetrics | None = None,
    visibility_handler: Callable[[str], None] | None = None,
) -> ApprovedPlanningResult | None:
    """Run Planning with one terminal owner and phase-appropriate commands."""

    from software_agent_team.planning_terminal import run_interactive_planning as run

    return run(
        coordinator,
        request,
        read=read,
        read_text=read_text,
        write=write,
        output=output,
        progress_visibility=progress_visibility,
        progress_display=progress_display,
        progress_color=progress_color,
        environment=environment,
        is_terminal=is_terminal,
        metrics=metrics,
        visibility_handler=visibility_handler,
    )


def _run_interactive_planning(
    coordinator: AdaptivePlanningCoordinator,
    request: PlanningRequest,
    *,
    read: InputReader = input,
    read_text: InputReader | None = None,
    write: OutputWriter = print,
    output: TextIO | None = None,
    progress_visibility: RunEventVisibility | str = RunEventVisibility.STANDARD,
    progress_display: TerminalProgressDisplay | str = TerminalProgressDisplay.AUTO,
    progress_color: TerminalColorMode | str = TerminalColorMode.AUTO,
    environment: Mapping[str, str] | None = None,
    is_terminal: bool | None = None,
    progress: TerminalPlanningProgress | None = None,
    activity_handler: PlanningActivityHandler | None = None,
    cancellation_requested: Callable[[], bool] | None = None,
    approval_guard: AbstractContextManager | None = None,
) -> ApprovedPlanningResult | None:
    """Run the user-facing clarification, overview, revision, and approval loop."""

    natural_text_reader = read if read_text is None else read_text
    progress = progress or TerminalPlanningProgress(
        write=write,
        output=output,
        visibility=progress_visibility,
        display=progress_display,
        color=progress_color,
        environment=environment,
        is_terminal=is_terminal,
    )
    answer_question = _interactive_question_answerer(
        read=read,
        read_text=natural_text_reader,
        write=write,
        color=progress.color_enabled,
        visibility=progress.visibility,
    )
    write("")
    write("Planning started. No runtime Agent has been created yet.")
    try:
        proposal = coordinator.start(
            request,
            answer_question=answer_question,
            activity_handler=activity_handler or progress,
        )
    finally:
        progress.close()
    if proposal is None:
        write("Planning cancelled; no runtime Agent was created.")
        return None

    show_fixed_policy = False
    show_technical_details = progress.visibility is RunEventVisibility.DETAILED
    while True:
        preview = coordinator.preview(request, proposal)
        write("")
        write(
            render_planning_overview(
                preview,
                budget_usage=(
                    None
                    if coordinator.budget_ledger is None
                    else coordinator.budget_ledger.snapshot()
                ),
                include_fixed_policy=show_fixed_policy,
                visibility=(
                    "detailed" if show_technical_details else progress.visibility.value
                ),
                color=progress.color_enabled,
            )
        )
        write("")
        write("  a. Approve and allow the controller to create this team")
        write("  r. Request changes in your own words")
        write("  e. Edit safe limits")
        write(
            "  d. "
            + ("Hide" if show_technical_details else "Show")
            + " technical plan details"
        )
        write(
            "  f. "
            + ("Hide" if show_fixed_policy else "Show")
            + " fixed policy details (no plan revision)"
        )
        write("  c. Cancel")
        choice = read("Review choice: ").strip().casefold()
        if choice in {"a", "approve"}:
            with approval_guard if approval_guard is not None else nullcontext():
                if cancellation_requested is not None and cancellation_requested():
                    raise KeyboardInterrupt
                approved = coordinator.approve(request, proposal)
            write(
                f"Plan revision {approved.approval.revision} approved. "
                "The controller may now create only the Agents shown above."
            )
            return approved
        if choice in {"d", "details"}:
            show_technical_details = not show_technical_details
            write(
                "Technical plan details are now "
                + ("shown." if show_technical_details else "hidden.")
            )
            continue
        if choice in {"c", "cancel"}:
            coordinator.store.cancel(request.run_id, now=coordinator.clock())
            write("Planning cancelled; no runtime Agent was created.")
            return None
        if choice in {"f", "fixed"}:
            show_fixed_policy = not show_fixed_policy
            write(
                "Fixed policy details are now "
                + ("shown." if show_fixed_policy else "hidden.")
                + " The proposal and model-call count are unchanged."
            )
            continue
        if choice in {"r", "revise"}:
            change = natural_text_reader("Describe the changes you want: ").strip()
            if not change:
                write("Change request must not be blank.")
                continue
            try:
                revised = coordinator.revise(
                    request,
                    proposal,
                    change,
                    answer_question=answer_question,
                    activity_handler=activity_handler or progress,
                )
            except PlanningError as error:
                session = coordinator.store.load_session(request.run_id)
                if session.status is PlanningSessionStatus.FAILED:
                    raise
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
        write("Choose a, r, e, d, f, or c.")
