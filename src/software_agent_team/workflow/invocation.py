"""Agent execution, handoff, and error classification helpers."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from pydantic import ValidationError

from software_agent_team.artifact_store import ArtifactStoreError
from software_agent_team.artifacts import (
    AgentRole,
    ArtifactReference,
    HandoffEnvelope,
    HandoffStatus,
    PhaseArtifact,
)
from software_agent_team.budgets import (
    AgentBudgetExceeded,
    AgentCallReservation,
)
from software_agent_team.execution import (
    AgentExecutionRequest,
    AgentExecutionResult,
    AgentExecutionStatus,
    AgentExecutionTelemetry,
)
from software_agent_team.git_workspace import (
    GitWorkspaceError,
    WorkspaceIntegrityError,
)
from software_agent_team.invocation import persist_agent_invocation
from software_agent_team.progress import (
    ProgressEvent,
    ProgressEventKind,
)
from software_agent_team.prompting import (
    AgentPromptInputs,
    build_agent_execution_request,
    build_semantic_correction_request,
    fixed_response_schema,
)
from software_agent_team.quality_gates import (
    QualityGateBudgetExceeded,
    QualityGateError,
    SandboxUnavailableError,
)
from software_agent_team.response_corrections import (
    ResponseValidationDiagnostic,
    SemanticCorrectionOutcome,
    SemanticCorrectionPlan,
    SemanticCorrectionRequestEvidence,
    apply_semantic_correction,
    build_semantic_correction_plan,
    correction_outcome,
)
from software_agent_team.responses import (
    RESPONSE_BODY_MODELS,
    AgentArtifactResponseError,
    controller_fields_for,
    parse_agent_response,
    parse_json_object_response,
)
from software_agent_team.run_control import TerminationReason
from software_agent_team.runtime_configuration import RuntimeConfigurationError
from software_agent_team.workflow.types import (
    AgentInvocationError,
    ArtifactAssembler,
    InvocationGuard,
    WorkflowEvidenceError,
    _utc,
    _WorkflowContext,
)

if TYPE_CHECKING:
    from software_agent_team.workflow.coordinator import WorkflowCoordinator


def _agent_termination_reason(
    result: AgentExecutionResult,
    *,
    repairable: bool,
) -> TerminationReason:
    if repairable:
        return TerminationReason.ARTIFACT_INVALID
    if result.status is AgentExecutionStatus.TIMED_OUT:
        return TerminationReason.RESOURCE_LIMIT_REACHED
    if result.status is AgentExecutionStatus.LAUNCH_FAILED:
        return TerminationReason.DEPENDENCY_UNAVAILABLE
    if result.status in {
        AgentExecutionStatus.PROVIDER_FAILED,
        AgentExecutionStatus.PROVIDER_STALLED,
        AgentExecutionStatus.RESPONSE_FINALIZATION_STALLED,
        AgentExecutionStatus.UPSTREAM_INCOMPLETE,
    }:
        return TerminationReason.DEPENDENCY_UNAVAILABLE
    return TerminationReason.EXECUTION_FAILED


def _sandbox_runtime_error(result: AgentExecutionResult) -> str | None:
    """Recognize OpenClaw-owned Docker failures without trusting Agent text."""
    for line in result.telemetry.stderr.splitlines():
        if "[tools]" not in line or " failed:" not in line:
            continue
        if (
            "Error response from daemon: container " in line
            and " is not running" in line
        ) or "Cannot connect to the Docker daemon" in line:
            return "OpenClaw sandbox became unavailable during Agent tool use"
    return None


def _termination_reason(error: Exception) -> TerminationReason:
    if isinstance(error, AgentInvocationError):
        return error.reason
    if isinstance(error, QualityGateBudgetExceeded):
        return TerminationReason.RESOURCE_LIMIT_REACHED
    if isinstance(error, (SandboxUnavailableError, RuntimeConfigurationError)):
        return TerminationReason.DEPENDENCY_UNAVAILABLE
    if isinstance(error, WorkspaceIntegrityError):
        if "no new commit" in str(error) or "no changed files" in str(error):
            return TerminationReason.NO_RELEVANT_CHANGE
        return TerminationReason.SAFETY_BOUNDARY_CROSSED
    if isinstance(error, GitWorkspaceError):
        return TerminationReason.SAFETY_BOUNDARY_CROSSED
    if isinstance(
        error,
        (
            WorkflowEvidenceError,
            AgentArtifactResponseError,
            ArtifactStoreError,
            ValidationError,
        ),
    ):
        return TerminationReason.ARTIFACT_INVALID
    if isinstance(error, QualityGateError):
        return TerminationReason.EXECUTION_FAILED
    return TerminationReason.CONTROLLER_ERROR


def _error_detail(error: Exception) -> str:
    message = str(error).strip()
    return message or error.__class__.__name__


def _execute_agent(
    coordinator: WorkflowCoordinator,
    request: AgentExecutionRequest,
) -> AgentExecutionResult:
    """Convert an executor exception into terminal attributable call evidence."""
    started_at = _utc(coordinator.clock)
    try:
        return coordinator.executor.execute(request)
    except Exception as error:
        finished_at = _utc(coordinator.clock)
        duration_ms = max(
            0,
            round((finished_at - started_at).total_seconds() * 1000),
        )
        detail = _error_detail(error)
        return AgentExecutionResult(
            status=AgentExecutionStatus.LAUNCH_FAILED,
            error=f"Agent executor raised {type(error).__name__}: {detail}",
            telemetry=AgentExecutionTelemetry(
                role=request.role,
                session_key=request.session_key,
                command=("agent-executor",),
                started_at=started_at,
                finished_at=finished_at,
                duration_ms=duration_ms,
                exit_code=None,
            ),
        )


def _record_execution(
    coordinator: WorkflowCoordinator,
    context: _WorkflowContext,
    *,
    request: AgentExecutionRequest,
    result: AgentExecutionResult,
    stage: str,
    attempt: int,
    response_reference: ArtifactReference | None,
    error: str | None,
    controller_supplied_fields: tuple[str, ...],
    ignored_controller_fields: tuple[str, ...],
    response_normalizations: tuple[str, ...],
    response_validation: ResponseValidationDiagnostic | None,
    semantic_correction_request: SemanticCorrectionRequestEvidence | None,
    semantic_correction_outcome: SemanticCorrectionOutcome | None,
    stage_timeout_seconds: int,
    remaining_timeout_seconds: int,
    reservation: AgentCallReservation,
) -> ArtifactReference:
    assert context.budget_ledger is not None
    persisted = persist_agent_invocation(
        artifact_store=context.artifact_store,
        budget_ledger=context.budget_ledger,
        reservation=reservation,
        request=request,
        result=result,
        stage=stage,
        attempt=attempt,
        response_reference=response_reference,
        error=error,
        controller_supplied_fields=controller_supplied_fields,
        ignored_controller_fields=ignored_controller_fields,
        response_normalizations=response_normalizations,
        response_validation=response_validation,
        semantic_correction_request=semantic_correction_request,
        semantic_correction_outcome=semantic_correction_outcome,
        pricing=coordinator.pricing,
        stage_timeout_seconds=stage_timeout_seconds,
        remaining_timeout_seconds=remaining_timeout_seconds,
    )
    with context.execution_lock:
        context.execution_records.append(persisted.reference)
    if persisted.budget_error is not None:
        raise AgentInvocationError(
            persisted.budget_error,
            TerminationReason.RESOURCE_LIMIT_REACHED,
        )
    return persisted.reference


def _handoff(
    coordinator: WorkflowCoordinator,
    context: _WorkflowContext,
    *,
    iteration: int,
    stage: str,
    source: AgentRole,
    target: AgentRole | None,
    input_commit: str,
    artifacts: tuple[ArtifactReference, ...],
    summary: str,
) -> ArtifactReference:
    handoff = HandoffEnvelope(
        run_id=context.brief.run_id,
        team_id=context.team_plan.team_id,
        iteration=iteration,
        stage=stage,
        sequence=1,
        source_agent_id=source.value,
        target_agent_id=None if target is None else target.value,
        status=HandoffStatus.COMPLETED,
        created_at=_utc(coordinator.clock),
        summary=summary,
        input_commit=input_commit,
        artifacts=list(artifacts),
    )
    reference = context.artifact_store.write(
        handoff,
        description=(
            f"Handoff from {source.value} to "
            f"{target.value if target is not None else 'controller'}."
        ),
    )
    context.handoffs.append(reference)
    return reference


def _invoke(
    coordinator: WorkflowCoordinator,
    context: _WorkflowContext,
    inputs: AgentPromptInputs,
    *,
    stage: str,
    assembler: ArtifactAssembler,
    invocation_guard: InvocationGuard | None = None,
) -> tuple[PhaseArtifact, ArtifactReference, ArtifactReference]:
    stage_timeout = context.team_plan.timeout_for_role(inputs.role)
    base_request = build_agent_execution_request(
        inputs,
        timeout_seconds=stage_timeout,
        model=coordinator.pricing.model,
    )
    last_error = "Agent did not return a semantic response"
    correction_plan: SemanticCorrectionPlan | None = None
    seen_correction_fingerprints: set[str] = set()
    for attempt in range(1, coordinator.artifact_repair_limit + 2):
        request = (
            base_request
            if correction_plan is None
            else build_semantic_correction_request(
                base_request,
                correction_plan,
                session_generation=attempt,
                response_schema=fixed_response_schema(inputs),
            )
        )
        assert context.budget_ledger is not None
        try:
            reservation = context.budget_ledger.reserve_call(
                request.agent_id,
                run_id=context.brief.run_id,
                stage=stage,
                attempt=attempt,
                route_id=context.team_plan.model_routes.default_route_id,
                pricing=coordinator.pricing,
            )
        except AgentBudgetExceeded as error:
            raise AgentInvocationError(
                str(error),
                TerminationReason.RESOURCE_LIMIT_REACHED,
            ) from error
        role_name = request.role.value.replace("_", " ").title()
        coordinator._emit(
            context,
            ProgressEvent(
                kind=ProgressEventKind.AGENT_STARTED,
                message=f"{role_name} is working",
                agent_id=request.role.value,
                iteration=request.iteration,
                attempt=attempt,
            ),
        )
        result = _execute_agent(coordinator, request)
        response: PhaseArtifact | None = None
        response_reference: ArtifactReference | None = None
        record_error: str | None = None
        failure_reason: TerminationReason | None = None
        ignored_controller_fields: tuple[str, ...] = ()
        response_normalizations: tuple[str, ...] = ()
        response_validation: ResponseValidationDiagnostic | None = None
        correction_request = (
            None if correction_plan is None else correction_plan.evidence
        )
        current_correction_outcome = (
            None if correction_plan is None else SemanticCorrectionOutcome.NOT_EVALUATED
        )
        next_correction_plan: SemanticCorrectionPlan | None = None
        assembly_error: Exception | None = None
        if result.status is AgentExecutionStatus.COMPLETED:
            reported_model = result.telemetry.model
            if reported_model is None:
                record_error = "successful execution omitted model metadata"
                failure_reason = TerminationReason.DEPENDENCY_UNAVAILABLE
            elif reported_model != coordinator.pricing.model:
                record_error = (
                    f"Agent model differs from the frozen run model: {reported_model}"
                )
                failure_reason = TerminationReason.SAFETY_BOUNDARY_CROSSED
            elif (
                result.telemetry.usage is None
                or result.telemetry.usage.input_tokens is None
                or result.telemetry.usage.output_tokens is None
            ):
                record_error = "successful execution omitted token usage"
                failure_reason = TerminationReason.DEPENDENCY_UNAVAILABLE
            elif sandbox_error := _sandbox_runtime_error(result):
                record_error = sandbox_error
                failure_reason = TerminationReason.DEPENDENCY_UNAVAILABLE
            else:
                try:
                    if invocation_guard is not None:
                        invocation_guard(correction_plan is not None)
                    parse_result = result
                    if correction_plan is not None:
                        envelope = parse_json_object_response(result.response_text)
                        corrected_payload = apply_semantic_correction(
                            envelope,
                            correction_plan,
                            response_schema=(
                                None
                                if (
                                    response_model := RESPONSE_BODY_MODELS.get(
                                        request.expected_kind
                                    )
                                )
                                is None
                                else response_model.model_json_schema()
                            ),
                        )
                        parse_result = result.model_copy(
                            update={
                                "response_text": json.dumps(
                                    corrected_payload,
                                    ensure_ascii=False,
                                )
                            }
                        )
                    parsed = parse_agent_response(
                        parse_result,
                        request,
                        task_brief=context.brief,
                        team_roles=context.team_plan.legacy_roles,
                        iteration_limit=coordinator.iteration_limit,
                    )
                except WorkspaceIntegrityError as error:
                    assembly_error = error
                    record_error = _error_detail(error)
                except AgentArtifactResponseError as error:
                    record_error = _error_detail(error)
                    response_normalizations = error.response_normalizations
                    response_validation = error.diagnostic
                    if (
                        correction_plan is None
                        and error.semantic_payload is not None
                        and error.diagnostic is not None
                    ):
                        next_correction_plan = build_semantic_correction_plan(
                            error.semantic_payload,
                            error.diagnostic,
                        )
                        seen_correction_fingerprints.add(error.diagnostic.fingerprint)
                    elif correction_plan is not None:
                        current_correction_outcome = (
                            SemanticCorrectionOutcome.INVALID_SUBMISSION
                            if error.diagnostic is None
                            else correction_outcome(
                                correction_plan,
                                error.diagnostic,
                                seen_fingerprints=frozenset(
                                    seen_correction_fingerprints
                                ),
                            )
                        )
                except (ValidationError, ValueError) as error:
                    record_error = _error_detail(error)
                    if correction_plan is not None:
                        current_correction_outcome = (
                            SemanticCorrectionOutcome.INVALID_SUBMISSION
                        )
                else:
                    ignored_controller_fields = parsed.ignored_controller_fields
                    response_normalizations = parsed.response_normalizations
                    if correction_plan is not None:
                        current_correction_outcome = SemanticCorrectionOutcome.ACCEPTED
                    try:
                        response = assembler(parsed.body)
                        response_reference = context.artifact_store.write(
                            response,
                            description=(
                                "Controller-assembled response from "
                                f"{request.role.value}."
                            ),
                        )
                    except Exception as error:
                        assembly_error = error
                        record_error = _error_detail(error)
        else:
            record_error = result.error or (
                f"Agent execution ended as {result.status.value}"
            )
            if result.status in {
                AgentExecutionStatus.PROVIDER_FAILED,
                AgentExecutionStatus.PROVIDER_STALLED,
                AgentExecutionStatus.RESPONSE_FINALIZATION_STALLED,
                AgentExecutionStatus.UPSTREAM_INCOMPLETE,
            }:
                failure_reason = TerminationReason.DEPENDENCY_UNAVAILABLE

        execution_reference = _record_execution(
            coordinator,
            context,
            request=request,
            result=result,
            stage=stage,
            attempt=attempt,
            response_reference=response_reference,
            error=record_error,
            controller_supplied_fields=controller_fields_for(request.expected_kind),
            ignored_controller_fields=ignored_controller_fields,
            response_normalizations=response_normalizations,
            response_validation=response_validation,
            semantic_correction_request=correction_request,
            semantic_correction_outcome=current_correction_outcome,
            stage_timeout_seconds=stage_timeout,
            remaining_timeout_seconds=request.timeout_seconds,
            reservation=reservation,
        )
        coordinator._emit(
            context,
            ProgressEvent(
                kind=ProgressEventKind.AGENT_COMPLETED,
                message=(
                    f"{role_name} response recorded "
                    f"({result.telemetry.duration_ms / 1000:.1f}s)"
                ),
                agent_id=request.role.value,
                iteration=request.iteration,
                attempt=attempt,
                duration_ms=result.telemetry.duration_ms,
            ),
        )
        if assembly_error is not None:
            raise assembly_error
        if response is not None and response_reference is not None:
            return response, response_reference, execution_reference
        last_error = record_error or last_error
        if (
            next_correction_plan is not None
            and attempt <= coordinator.artifact_repair_limit
        ):
            correction_plan = next_correction_plan
            coordinator._emit(
                context,
                ProgressEvent(
                    kind=ProgressEventKind.AGENT_RETRY,
                    message=(
                        f"{role_name} response needs targeted correction for "
                        + ", ".join(correction_plan.evidence.target_paths)
                    ),
                    agent_id=request.role.value,
                    iteration=request.iteration,
                    attempt=attempt,
                ),
            )
            continue
        raise AgentInvocationError(
            f"{request.role.value} failed: {last_error}",
            failure_reason
            or _agent_termination_reason(
                result,
                repairable=response_validation is not None,
            ),
        )
    raise AssertionError("unreachable Agent repair state")
