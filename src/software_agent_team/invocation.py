"""Controller-owned persistence and accounting for one Agent invocation."""

from __future__ import annotations

from dataclasses import dataclass

from software_agent_team.artifact_store import ArtifactStore
from software_agent_team.artifacts import (
    AgentExecutionRecord,
    ArtifactKind,
    ArtifactReference,
)
from software_agent_team.budgets import (
    AgentBudgetExceeded,
    AgentBudgetLedger,
    AgentCallReservation,
    ModelPricing,
)
from software_agent_team.execution import AgentExecutionRequest, AgentExecutionResult
from software_agent_team.response_corrections import (
    ResponseValidationDiagnostic,
    SemanticCorrectionOutcome,
    SemanticCorrectionRequestEvidence,
)


@dataclass(frozen=True)
class PersistedInvocation:
    """Execution reference plus any post-call budget rejection."""

    reference: ArtifactReference
    budget_error: str | None


def persist_agent_invocation(
    *,
    artifact_store: ArtifactStore,
    budget_ledger: AgentBudgetLedger,
    reservation: AgentCallReservation,
    request: AgentExecutionRequest,
    result: AgentExecutionResult,
    stage: str,
    attempt: int,
    response_reference: ArtifactReference | None,
    error: str | None,
    controller_supplied_fields: tuple[str, ...],
    ignored_controller_fields: tuple[str, ...],
    pricing: ModelPricing | None,
    response_normalizations: tuple[str, ...] = (),
    response_validation: ResponseValidationDiagnostic | None = None,
    semantic_correction_request: SemanticCorrectionRequestEvidence | None = None,
    semantic_correction_outcome: SemanticCorrectionOutcome | None = None,
    stage_timeout_seconds: int | None = None,
    remaining_timeout_seconds: int | None = None,
) -> PersistedInvocation:
    """Persist raw output and telemetry after accounting for the completed call.

    Post-call budget rejection is returned only after the over-limit usage and
    execution record have been retained. The caller remains responsible for
    the lifecycle decision.
    """

    if pricing is not None and request.model != pricing.model:
        raise ValueError("invocation pricing does not match the requested model")
    if stage_timeout_seconds is None:
        stage_timeout_seconds = request.timeout_seconds
    if remaining_timeout_seconds is None:
        remaining_timeout_seconds = request.timeout_seconds
    telemetry = result.telemetry
    usage = telemetry.usage
    estimated_cost = reservation.estimate_cost(
        input_tokens=None if usage is None else usage.input_tokens,
        output_tokens=None if usage is None else usage.output_tokens,
    )

    budget_error: str | None = None
    try:
        budget_ledger.complete_call(
            reservation,
            input_tokens=None if usage is None else usage.input_tokens,
            output_tokens=None if usage is None else usage.output_tokens,
            duration_ms=telemetry.duration_ms,
        )
    except AgentBudgetExceeded as budget_exception:
        budget_error = str(budget_exception)

    outputs = artifact_store.write_execution_outputs(
        iteration=request.iteration,
        stage=stage,
        agent_id=request.agent_id,
        attempt=attempt,
        stdout=telemetry.stdout,
        stderr=telemetry.stderr,
    )
    record_error = error
    if budget_error is not None:
        record_error = (
            budget_error
            if record_error is None
            else f"{record_error}; budget rejection: {budget_error}"
        )
    record = AgentExecutionRecord(
        run_id=request.run_id,
        team_id=request.team_id,
        iteration=request.iteration,
        stage=stage,
        attempt=attempt,
        agent_id=request.agent_id,
        capability=request.capability.value,
        execution_status=result.status,
        session_key=request.session_key,
        session_id=telemetry.session_id,
        model=telemetry.model,
        provider=telemetry.provider,
        started_at=telemetry.started_at,
        finished_at=telemetry.finished_at,
        duration_ms=telemetry.duration_ms,
        exit_code=telemetry.exit_code,
        timed_out=telemetry.timed_out,
        provider_liveness=telemetry.provider_liveness,
        invocation_lifecycle=telemetry.invocation_lifecycle,
        input_tokens=None if usage is None else usage.input_tokens,
        output_tokens=None if usage is None else usage.output_tokens,
        estimated_cost_usd=estimated_cost,
        stdout_path=outputs.stdout_path,
        stderr_path=outputs.stderr_path,
        stdout_sha256=outputs.stdout_sha256,
        stderr_sha256=outputs.stderr_sha256,
        response_contract=(
            "semantic_body_v4"
            if request.expected_kind is ArtifactKind.REVIEW_REPORT
            else "semantic_body_v1"
        ),
        response_transport=(
            "typed_submission_v1" if request.submission_contract is not None else None
        ),
        controller_supplied_fields=controller_supplied_fields,
        ignored_controller_fields=ignored_controller_fields,
        response_normalizations=response_normalizations,
        response_validation=response_validation,
        semantic_correction_request=semantic_correction_request,
        semantic_correction_outcome=semantic_correction_outcome,
        submission_evidence=result.submission_evidence,
        tool_evidence_status=telemetry.tool_evidence_status,
        session_transcript_sha256=telemetry.session_transcript_sha256,
        session_record_count=telemetry.session_record_count,
        tool_calls=telemetry.tool_calls,
        tool_evidence_error=telemetry.tool_evidence_error,
        stage_timeout_seconds=stage_timeout_seconds,
        remaining_timeout_seconds=remaining_timeout_seconds,
        response_artifact=response_reference,
        error=record_error,
    )
    reference = artifact_store.write(
        record,
        description=f"Agent execution telemetry for {request.agent_id}.",
    )
    return PersistedInvocation(reference=reference, budget_error=budget_error)
