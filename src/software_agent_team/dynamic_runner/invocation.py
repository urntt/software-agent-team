"""Agent invocation execution loop and retry/fallback management mixin.

This module provides the AgentInvocationMixin which manages the attempt loop for an
approved AgentSpec, budget reservations, continuation handling (partial workspace
progress or read-only tools), semantic correction loops, and approved provider model
fallback switching.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, cast

from software_agent_team.artifacts import (
    AgentRuntimeFailureCode,
    ArtifactReference,
    CommandEvidence,
    TaskBrief,
    WorkResult,
)
from software_agent_team.budgets import AgentBudgetLedger, ModelPricing
from software_agent_team.execution import (
    AgentExecutionStatus,
)
from software_agent_team.git_workspace import (
    GitSnapshot,
    GitWorkspace,
    GitWorkspaceManager,
    GitWorkspaceProgress,
)
from software_agent_team.invocation import persist_agent_invocation
from software_agent_team.invocation_lifecycle import InvocationPhase
from software_agent_team.planning import AdaptiveImplementationPlan
from software_agent_team.progress import (
    ProgressEventKind,
    RunEventReference,
    RunEventReferenceKind,
)
from software_agent_team.prompting import (
    DynamicAgentPromptInputs,
    DynamicRevisionFeedback,
    DynamicUserGuidance,
    build_dynamic_agent_execution_request,
    build_readonly_upstream_continuation_request,
    build_semantic_correction_request,
    build_upstream_continuation_request,
)
from software_agent_team.response_corrections import (
    ResponseValidationDiagnostic,
    SemanticCorrectionOutcome,
    SemanticCorrectionPlan,
)
from software_agent_team.responses import (
    ReviewToolEvidenceAttempt,
    controller_fields_for,
)
from software_agent_team.run_control import TerminationReason
from software_agent_team.scheduling import AgentRunOutcome
from software_agent_team.submissions import RECOVERABLE_SUBMISSION_EVIDENCE_CODES
from software_agent_team.teams import (
    AgentCapability,
    AgentSpec,
    ModelSwitchCondition,
    PermissionProfile,
    TeamPlan,
)

from .types import (
    DynamicAgentRunnerError,
    GuidanceProvider,
    InvocationStopProvider,
    _ReadOnlyContinuation,
    _UpstreamContinuation,
)

if TYPE_CHECKING:
    from software_agent_team.artifact_store import ArtifactStore


class AgentInvocationMixin:
    """Methods for executing the attempt loop and retries for one approved AgentSpec."""

    artifact_store: ArtifactStore
    workspace: GitWorkspace
    workspace_manager: GitWorkspaceManager
    budget_ledger: AgentBudgetLedger
    task_brief: TaskBrief
    implementation_plan: AdaptiveImplementationPlan
    team_plan: TeamPlan
    iteration: int
    input_commit: str
    revision_feedback: DynamicRevisionFeedback | None
    revision_command_evidence: tuple[CommandEvidence, ...]
    manual_review_criteria: tuple[str, ...]
    review_scope_by_agent: dict[str, tuple[str, ...]]
    pricing_by_model: Mapping[str, ModelPricing]
    artifact_repair_limit: int | None
    guidance_provider: GuidanceProvider | None
    invocation_stop_provider: InvocationStopProvider | None

    def _invoke_agent(
        self,
        agent: AgentSpec,
        upstream: Mapping[str, AgentRunOutcome],
    ) -> ArtifactReference:
        """Execute the attempt loop for an agent until success or terminal stop."""
        if agent.permission_profile is PermissionProfile.WORKSPACE_WRITE:
            input_commit = self._begin_writer(agent)  # type: ignore[attr-defined]
            commands = self.revision_command_evidence
            manual_scope: tuple[str, ...] = ()
        else:
            commands, input_commit = self._ensure_quality_evidence()  # type: ignore[attr-defined]
            manual_scope = (
                self.review_scope_by_agent[agent.id]
                if agent.capability is AgentCapability.REVIEW
                else self.manual_review_criteria
            )
        upstream_results = self._upstream_results(agent, upstream)  # type: ignore[attr-defined]
        assigned_task_ids = tuple(
            task.id
            for task in self.implementation_plan.tasks
            if task.owner_agent_id == agent.id
        )
        guidance_by_id: dict[str, DynamicUserGuidance] = {}
        previous_error = "Agent did not return a semantic response"
        correction_plan: SemanticCorrectionPlan | None = None
        seen_correction_fingerprints: set[str] = set()
        semantic_corrections = 0
        frozen_writer_snapshot: GitSnapshot | None = None
        frozen_writer_commit: str | None = None
        continuation: _UpstreamContinuation | None = None
        seen_continuation_states: set[str] = set()
        readonly_continuation: _ReadOnlyContinuation | None = None
        seen_readonly_tool_states: set[str] = set()
        attempt = 1
        review_evidence_attempts: list[ReviewToolEvidenceAttempt] = []
        route_ids = self.team_plan.model_routes.authorized_route_ids(agent.id)
        route_index = 0
        provider_switching = ModelSwitchCondition.PROVIDER_FAILURE in (
            self.team_plan.model_routes.authorized_switch_conditions
        )
        stream_retries = 0

        while True:
            route_id = route_ids[route_index]
            if self.guidance_provider is not None:
                for guidance in self.guidance_provider(agent.id):
                    guidance_by_id[guidance.command_id] = guidance
            prompt_inputs = DynamicAgentPromptInputs(
                task_brief=self.task_brief,
                implementation_plan=self.implementation_plan,
                team_plan=self.team_plan,
                agent_id=agent.id,
                active_model_route_id=route_id,
                iteration=self.iteration,
                iteration_input_commit=self.input_commit,
                input_commit=input_commit,
                upstream_results=upstream_results,
                command_evidence=commands,
                manual_review_criteria=manual_scope,
                revision_feedback=self.revision_feedback,
                user_guidance=tuple(guidance_by_id.values()),
            )
            base_request = build_dynamic_agent_execution_request(prompt_inputs)
            current_continuation = continuation
            continuation = None
            current_readonly_continuation = readonly_continuation
            readonly_continuation = None
            if self.invocation_stop_provider is not None:
                invocation_stop = self.invocation_stop_provider(agent.id)
                if invocation_stop is not None:
                    if invocation_stop not in {
                        TerminationReason.USER_INTERRUPTED,
                        TerminationReason.USER_CANCELLED,
                    }:
                        raise DynamicAgentRunnerError(
                            "invocation stop provider returned an invalid reason",
                            TerminationReason.CONTROLLER_ERROR,
                        )
                    raise DynamicAgentRunnerError(
                        "Agent invocation was not started because the user "
                        "stopped this work.",
                        invocation_stop,
                    )
            if (
                current_continuation is not None
                or current_readonly_continuation is not None
            ) and correction_plan is not None:
                raise DynamicAgentRunnerError(
                    "semantic correction and upstream continuation cannot overlap",
                    TerminationReason.CONTROLLER_ERROR,
                )
            if current_continuation is not None:
                request = build_upstream_continuation_request(
                    base_request,
                    workspace_state_sha256=(current_continuation.progress.state_sha256),
                    changed_path_count=len(current_continuation.progress.changed_files),
                )
            elif current_readonly_continuation is not None:
                request = build_readonly_upstream_continuation_request(
                    base_request,
                    verified_commit=input_commit,
                    completed_tool_operations=(
                        current_readonly_continuation.completed_tool_operations
                    ),
                )
            elif correction_plan is not None:
                request = build_semantic_correction_request(
                    base_request,
                    correction_plan,
                    session_generation=attempt,
                )
            else:
                request = base_request
            pricing = self.pricing_by_model[cast(str, request.model)]

            def check_budget_wait() -> None:
                if self.invocation_stop_provider is not None:
                    stop = self.invocation_stop_provider(agent.id)
                    if stop is not None:
                        if stop not in {
                            TerminationReason.USER_INTERRUPTED,
                            TerminationReason.USER_CANCELLED,
                        }:
                            raise DynamicAgentRunnerError(
                                "invocation stop provider returned an invalid reason",
                                TerminationReason.CONTROLLER_ERROR,
                            )
                        raise DynamicAgentRunnerError(
                            "Agent invocation was stopped while waiting for "
                            "budget admission.",
                            stop,
                        )

            reservation = self.budget_ledger.reserve_call(
                agent.id,
                run_id=self.task_brief.run_id,
                stage=agent.stage_id,
                attempt=attempt,
                route_id=route_id,
                pricing=pricing,
                wait_for_active=True,
                wait_check=check_budget_wait,
            )
            if current_continuation is not None:
                self._emit_activity(  # type: ignore[attr-defined]
                    agent,
                    kind=ProgressEventKind.AGENT_RETRY,
                    message=(
                        f"{agent.label} tool-bearing upstream turn ended before "
                        "submission; workspace identity, ancestry, scope, "
                        f"and {len(current_continuation.progress.changed_files)} "
                        "changed path(s) were verified. Continuing the same task "
                        "and session under the approved budget."
                    ),
                    attempt=attempt,
                    model=request.model,
                    references=(
                        RunEventReference(
                            kind=RunEventReferenceKind.ARTIFACT,
                            id=f"{agent.id}-invocation-{attempt - 1}",
                            path=current_continuation.prior_execution.path,
                            sha256=current_continuation.prior_execution.sha256,
                        ),
                    ),
                    checkpoint=self._checkpoint_snapshot(  # type: ignore[attr-defined]
                        agent,
                        phase=InvocationPhase.STOPPED,
                        last_verified_checkpoint=(
                            "Partial workspace identity, ancestry, permission scope, "
                            "and state digest were verified"
                        ),
                        next_controller_checkpoint=(
                            "Resume the same Agent session and require commit plus "
                            "typed submission"
                        ),
                        completed_tool_operations=(
                            current_continuation.completed_tool_operations
                        ),
                    ),
                )
            elif current_readonly_continuation is not None:
                self._emit_activity(  # type: ignore[attr-defined]
                    agent,
                    kind=ProgressEventKind.AGENT_RETRY,
                    message=(
                        f"{agent.label} ended after attributable tools without a "
                        "typed submission; the read-only workspace and session "
                        "evidence were verified. Continuing the same task and "
                        "session under the approved budget."
                    ),
                    attempt=attempt,
                    model=request.model,
                    references=(
                        RunEventReference(
                            kind=RunEventReferenceKind.ARTIFACT,
                            id=f"{agent.id}-invocation-{attempt - 1}",
                            path=current_readonly_continuation.prior_execution.path,
                            sha256=current_readonly_continuation.prior_execution.sha256,
                        ),
                    ),
                    checkpoint=self._checkpoint_snapshot(  # type: ignore[attr-defined]
                        agent,
                        phase=InvocationPhase.STOPPED,
                        last_verified_checkpoint=(
                            "Read-only workspace and attributable tool evidence "
                            "were verified"
                        ),
                        next_controller_checkpoint=(
                            "Resume the same Agent session and require typed submission"
                        ),
                        completed_tool_operations=(
                            current_readonly_continuation.completed_tool_operations
                        ),
                    ),
                )
            result = self._execute(  # type: ignore[attr-defined]
                request,
                activity_handler=lambda activity, current_attempt=attempt: (
                    self._observe_execution_activity(  # type: ignore[attr-defined]
                        agent,
                        attempt=current_attempt,
                        activity=activity,
                    )
                ),
            )
            recoverable_stream_failure = (
                stream_retries < 1
                and result.status is AgentExecutionStatus.PROVIDER_FAILED
                and result.telemetry.runtime_failure_code
                is AgentRuntimeFailureCode.OPENCLAW_MODEL_STREAM_TERMINATED
                and result.semantic_submission is None
                and result.submission_evidence is not None
                and result.submission_evidence.tool_call_id is None
                and result.submission_evidence.diagnostic_code
                in RECOVERABLE_SUBMISSION_EVIDENCE_CODES
                and result.telemetry.invocation_lifecycle is not None
                and result.telemetry.invocation_lifecycle.shutdown.cleanup_completed
            )
            response_reference: ArtifactReference | None = None
            ignored_fields: tuple[str, ...] = ()
            response_normalizations: tuple[str, ...] = ()
            response_validation: ResponseValidationDiagnostic | None = None
            correction_request = (
                None if correction_plan is None else correction_plan.evidence
            )
            current_correction_outcome = (
                None
                if correction_plan is None
                else SemanticCorrectionOutcome.NOT_EVALUATED
            )
            next_correction_plan: SemanticCorrectionPlan | None = None
            record_error: str | None = None
            failure: Exception | None = None
            current_review_evidence: ReviewToolEvidenceAttempt | None = None
            next_continuation_progress: GitWorkspaceProgress | None = None
            next_readonly_continuation = False
            try:
                self._validate_execution_result(result, request)  # type: ignore[attr-defined]
                if (
                    current_readonly_continuation is not None
                    and result.telemetry.session_id
                    != current_readonly_continuation.session_id
                ):
                    raise DynamicAgentRunnerError(
                        "read-only continuation changed its OpenClaw session",
                        TerminationReason.SAFETY_BOUNDARY_CROSSED,
                    )
                if (
                    agent.capability is AgentCapability.REVIEW
                    and result.status is AgentExecutionStatus.COMPLETED
                ):
                    current_review_evidence = ReviewToolEvidenceAttempt(
                        execution_attempt=attempt,
                        tool_calls=result.telemetry.tool_calls,
                    )
                if result.status is AgentExecutionStatus.INTERRUPTED:
                    snapshot = None
                    record_error = result.error or (
                        f"Agent execution ended as {result.status.value}"
                    )
                    failure = DynamicAgentRunnerError(
                        record_error,
                        self._execution_termination_reason(result.status),  # type: ignore[attr-defined]
                    )
                elif result.status is not AgentExecutionStatus.COMPLETED:
                    snapshot = None
                    record_error = result.error or (
                        f"Agent execution ended as {result.status.value}"
                    )
                    execution_failure = DynamicAgentRunnerError(
                        record_error,
                        self._execution_termination_reason(result.status),  # type: ignore[attr-defined]
                    )
                    disposition = self._classify_incomplete_attempt(  # type: ignore[attr-defined]
                        agent=agent,
                        request=request,
                        result=result,
                        input_commit=input_commit,
                        correction_plan=correction_plan,
                        recoverable_stream_failure=recoverable_stream_failure,
                        seen_continuation_states=seen_continuation_states,
                        seen_readonly_tool_states=seen_readonly_tool_states,
                        attempt=attempt,
                        execution_failure=execution_failure,
                    )
                    failure = disposition.failure
                    next_continuation_progress = disposition.writer_progress
                    next_readonly_continuation = disposition.readonly_continuation
                    if disposition.review_evidence is not None:
                        current_review_evidence = disposition.review_evidence
                elif (
                    correction_plan is not None
                    and agent.permission_profile is PermissionProfile.WORKSPACE_WRITE
                ):
                    assert frozen_writer_commit is not None
                    self.workspace_manager.verify_workspace(
                        self.workspace,
                        expected_commit=frozen_writer_commit,
                        require_clean=True,
                    )
                    snapshot = frozen_writer_snapshot
                else:
                    snapshot = self._verify_workspace_after_call(agent, input_commit)  # type: ignore[attr-defined]
                    if agent.permission_profile is PermissionProfile.WORKSPACE_WRITE:
                        frozen_writer_snapshot = snapshot
                        frozen_writer_commit = (
                            input_commit if snapshot is None else snapshot.output_commit
                        )
                if result.status is AgentExecutionStatus.COMPLETED:
                    completed = self._accept_completed_attempt(  # type: ignore[attr-defined]
                        agent=agent,
                        request=request,
                        base_request=base_request,
                        result=result,
                        assigned_task_ids=assigned_task_ids,
                        manual_scope=manual_scope,
                        review_evidence_attempts=review_evidence_attempts,
                        current_review_evidence=current_review_evidence,
                        commands=commands,
                        correction_plan=correction_plan,
                        seen_correction_fingerprints=seen_correction_fingerprints,
                        input_commit=input_commit,
                        snapshot=snapshot,
                        upstream=upstream,
                    )
                    response_reference = completed.response_reference
                    ignored_fields = completed.ignored_fields
                    response_normalizations = completed.normalizations
                    response_validation = completed.validation
                    next_correction_plan = completed.next_correction_plan
                    current_correction_outcome = completed.correction_outcome
                    record_error = completed.record_error
                    failure = completed.failure
            except Exception as error:
                if failure is None:
                    failure = error
                    record_error = self._error_detail(error)  # type: ignore[attr-defined]

            persisted = persist_agent_invocation(
                artifact_store=self.artifact_store,
                budget_ledger=self.budget_ledger,
                reservation=reservation,
                request=request,
                result=result,
                stage=agent.stage_id,
                attempt=attempt,
                response_reference=response_reference,
                error=record_error,
                controller_supplied_fields=controller_fields_for(request.expected_kind),
                ignored_controller_fields=ignored_fields,
                pricing=pricing,
                response_normalizations=response_normalizations,
                response_validation=response_validation,
                semantic_correction_request=correction_request,
                semantic_correction_outcome=current_correction_outcome,
            )
            self._record_execution_reference(agent.id, persisted.reference)  # type: ignore[attr-defined]
            if current_review_evidence is not None:
                review_evidence_attempts.append(current_review_evidence)
            budget_usage = self.budget_ledger.snapshot()
            budget_remaining = budget_usage.remaining_estimated_cost_usd(
                self.budget_ledger.budget
            )
            pricing_source = (
                "unknown"
                if pricing.pricing_source is None
                else pricing.pricing_source.value
            )
            self._emit_activity(  # type: ignore[attr-defined]
                agent,
                kind=ProgressEventKind.AGENT_INVOCATION_COMPLETED,
                message=(
                    f"{agent.label} invocation {attempt} returned "
                    f"{result.status.value}; task model spend "
                    f"${budget_usage.known_estimated_cost_usd:.6f} estimated / "
                    f"${self.budget_ledger.budget.max_estimated_cost_usd} "
                    f"authorized, ${budget_remaining:.6f} recorded remaining "
                    f"(price source {pricing_source})"
                ),
                attempt=attempt,
                model=request.model,
                duration_ms=result.telemetry.duration_ms,
                budget_usage=budget_usage,
                references=(
                    RunEventReference(
                        kind=RunEventReferenceKind.ARTIFACT,
                        id=f"{agent.id}-invocation-{attempt}",
                        path=persisted.reference.path,
                        sha256=persisted.reference.sha256,
                    ),
                    RunEventReference(
                        kind=RunEventReferenceKind.MODEL_ROUTE,
                        id=route_id,
                    ),
                ),
                checkpoint=self._checkpoint_snapshot(  # type: ignore[attr-defined]
                    agent,
                    phase=InvocationPhase.STOPPED,
                    last_verified_checkpoint=(
                        "Invocation evidence and model usage were persisted"
                    ),
                    next_controller_checkpoint=(
                        "Validate the typed response and choose completion, "
                        "correction, fallback, or failure"
                    ),
                    completed_tool_operations=len(result.telemetry.tool_calls),
                    review_grounded=response_reference is not None,
                ),
            )
            if persisted.budget_error is not None:
                # Accounting is an independent terminal evidence dimension. If
                # this invocation already has an attributable failure, unknown
                # usage must stop any further call without reclassifying that
                # primary runtime or response failure.
                if failure is not None:
                    if response_validation is not None:
                        raise DynamicAgentRunnerError(
                            record_error or self._error_detail(failure),  # type: ignore[attr-defined]
                            TerminationReason.ARTIFACT_INVALID,
                        ) from failure
                    raise failure
                raise DynamicAgentRunnerError(
                    persisted.budget_error,
                    TerminationReason.RESOURCE_LIMIT_REACHED,
                )
            if response_reference is not None:
                if agent.permission_profile is PermissionProfile.WORKSPACE_WRITE:
                    work = cast(
                        WorkResult, self.artifact_store.load(response_reference)
                    )
                    self._finish_writer(  # type: ignore[attr-defined]
                        agent,
                        input_commit,
                        work.output_commit,
                    )
                return response_reference
            if next_continuation_progress is not None:
                if recoverable_stream_failure:
                    stream_retries += 1
                continuation = _UpstreamContinuation(
                    progress=next_continuation_progress,
                    prior_execution=persisted.reference,
                    completed_tool_operations=len(result.telemetry.tool_calls),
                )
                attempt += 1
                continue
            if next_readonly_continuation:
                assert result.telemetry.session_id is not None
                readonly_continuation = _ReadOnlyContinuation(
                    prior_execution=persisted.reference,
                    completed_tool_operations=len(result.telemetry.tool_calls),
                    session_id=result.telemetry.session_id,
                )
                attempt += 1
                continue
            if (
                recoverable_stream_failure
                and isinstance(failure, DynamicAgentRunnerError)
                and failure.reason is TerminationReason.DEPENDENCY_UNAVAILABLE
            ):
                stream_retries += 1
                self._emit_activity(  # type: ignore[attr-defined]
                    agent,
                    kind=ProgressEventKind.AGENT_RETRY,
                    message=(
                        f"{agent.label} model stream terminated before a trusted "
                        "submission; retrying the same approved route once "
                        "under the remaining task budget. The failed call "
                        "remains recorded and may be billable."
                    ),
                    attempt=attempt,
                    model=request.model,
                )
                attempt += 1
                continue
            safe_provider_fallback = (
                result.status is AgentExecutionStatus.PROVIDER_FAILED
                and result.semantic_submission is None
                and result.submission_evidence is not None
                and result.submission_evidence.diagnostic_code
                in RECOVERABLE_SUBMISSION_EVIDENCE_CODES
                and (
                    result.telemetry.invocation_lifecycle is None
                    or result.telemetry.invocation_lifecycle.shutdown.cleanup_completed
                )
            )
            if (
                safe_provider_fallback
                and provider_switching
                and route_index + 1 < len(route_ids)
                and isinstance(failure, DynamicAgentRunnerError)
                and failure.reason is TerminationReason.DEPENDENCY_UNAVAILABLE
            ):
                previous_route = route_id
                route_index += 1
                route_id = route_ids[route_index]
                next_route = self.team_plan.model_routes.get_route(route_id)
                attempt += 1
                self._emit_activity(  # type: ignore[attr-defined]
                    agent,
                    kind=ProgressEventKind.MODEL_ROUTE_SWITCHED,
                    message=(
                        f"{agent.label} provider route {previous_route} failed; "
                        f"switching to the explicitly approved {route_id} profile. "
                        "The failed call remains recorded and may be billable."
                    ),
                    attempt=attempt,
                    model=next_route.model,
                    references=(
                        RunEventReference(
                            kind=RunEventReferenceKind.MODEL_ROUTE,
                            id=previous_route,
                        ),
                        RunEventReference(
                            kind=RunEventReferenceKind.MODEL_ROUTE,
                            id=route_id,
                        ),
                    ),
                )
                continue
            previous_error = record_error or previous_error
            correction_allowed = next_correction_plan is not None and (
                self.artifact_repair_limit is None
                or semantic_corrections < self.artifact_repair_limit
            )
            if correction_allowed:
                semantic_corrections += 1
                correction_plan = next_correction_plan
                self._emit_activity(  # type: ignore[attr-defined]
                    agent,
                    kind=ProgressEventKind.AGENT_RETRY,
                    message=(
                        f"{agent.label} response failed validation; requesting "
                        "only controller-identified semantic fields: "
                        + ", ".join(correction_plan.evidence.target_paths)
                    ),
                    attempt=attempt,
                    model=request.model,
                )
                attempt += 1
                continue
            if failure is not None:
                if response_validation is not None:
                    raise DynamicAgentRunnerError(
                        previous_error,
                        TerminationReason.ARTIFACT_INVALID,
                    ) from failure
                raise failure
            raise DynamicAgentRunnerError(
                previous_error,
                TerminationReason.EXECUTION_FAILED,
            )
