"""Attempt classification, response disposition, and error mapping mixin.

This module provides the AttemptDispositionMixin which handles outcome creation,
continuation classifications (for both write and read-only agents), semantic
correction evaluation, and exception classification for the dynamic runner.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import TYPE_CHECKING

from pydantic import ValidationError

from software_agent_team.artifact_store import ArtifactStoreError
from software_agent_team.artifacts import (
    AgentSubmissionStatus,
    AgentToolEvidenceStatus,
    ArtifactReference,
    CommandEvidence,
    TaskBrief,
)
from software_agent_team.assembly import ArtifactAssemblyError
from software_agent_team.budgets import AgentBudgetExceeded
from software_agent_team.execution import (
    AgentExecutionRequest,
    AgentExecutionResult,
    AgentExecutionStatus,
)
from software_agent_team.git_workspace import (
    GitSnapshot,
    GitWorkspaceError,
    GitWorkspaceProgress,
    WorkspaceIntegrityError,
)
from software_agent_team.prompting import AgentPromptError
from software_agent_team.quality_gates import (
    QualityGateBudgetExceeded,
    QualityGateError,
    SandboxUnavailableError,
)
from software_agent_team.response_corrections import (
    ResponseValidationDiagnostic,
    SemanticCorrectionOutcome,
    SemanticCorrectionPlan,
    SemanticCorrectionSubmissionError,
    apply_semantic_correction_with_evidence,
    build_semantic_correction_plan,
    correction_outcome,
)
from software_agent_team.responses import (
    AgentArtifactResponseError,
    ReviewToolEvidenceAttempt,
    bind_review_evidence_correction_candidates,
    parse_dynamic_agent_response,
)
from software_agent_team.run_control import TerminationReason
from software_agent_team.scheduling import AgentRunOutcome, AgentRunStatus
from software_agent_team.teams import (
    AgentCapability,
    AgentSpec,
    PermissionProfile,
    TeamPlan,
)

from .types import (
    DynamicAgentRunnerError,
    _CompletedAttemptDisposition,
    _IncompleteAttemptDisposition,
)

if TYPE_CHECKING:
    from software_agent_team.artifact_store import ArtifactStore
    from software_agent_team.git_workspace import GitWorkspace, GitWorkspaceManager


class AttemptDispositionMixin:
    """Methods for attempt results, errors, and continuation classifications."""

    artifact_store: ArtifactStore
    workspace: GitWorkspace
    workspace_manager: GitWorkspaceManager
    task_brief: TaskBrief
    team_plan: TeamPlan

    def _failed_outcome(
        self,
        agent: AgentSpec,
        detail: str,
        reason: TerminationReason,
        evidence: tuple[ArtifactReference, ...],
    ) -> AgentRunOutcome:
        """Construct an AgentRunOutcome representing failure or user interruption."""
        safe_detail = detail[:2000]
        return AgentRunOutcome(
            agent_id=agent.id,
            status=(
                AgentRunStatus.INTERRUPTED
                if reason
                in {
                    TerminationReason.USER_INTERRUPTED,
                    TerminationReason.USER_CANCELLED,
                }
                else AgentRunStatus.FAILED
            ),
            evidence=evidence,
            summary=f"{agent.label} failed ({reason.value})."[:2000],
            error=safe_detail,
        )

    def _classify_incomplete_attempt(
        self,
        *,
        agent: AgentSpec,
        request: AgentExecutionRequest,
        result: AgentExecutionResult,
        input_commit: str,
        correction_plan: SemanticCorrectionPlan | None,
        recoverable_stream_failure: bool,
        seen_continuation_states: set[str],
        seen_readonly_tool_states: set[str],
        attempt: int,
        execution_failure: DynamicAgentRunnerError,
    ) -> _IncompleteAttemptDisposition:
        """Verify workspace evidence before allowing a same-session continuation."""
        record_error = str(execution_failure)
        failure: DynamicAgentRunnerError | None = None
        next_continuation_progress: GitWorkspaceProgress | None = None
        next_readonly_continuation = False
        current_review_evidence: ReviewToolEvidenceAttempt | None = None
        if agent.permission_profile is PermissionProfile.WORKSPACE_WRITE:
            progress = self.workspace_manager.inspect_progress(
                self.workspace,
                input_commit=input_commit,
            )
            self._validate_workspace_scope(agent, progress.changed_files)  # type: ignore[attr-defined]
            if (
                result.status is AgentExecutionStatus.UPSTREAM_INCOMPLETE
                and correction_plan is None
            ):
                if not progress.made_progress:
                    failure = DynamicAgentRunnerError(
                        f"{record_error}; no verifiable workspace progress "
                        "exists for controlled continuation",
                        TerminationReason.DEPENDENCY_UNAVAILABLE,
                    )
                elif progress.state_sha256 in seen_continuation_states:
                    failure = DynamicAgentRunnerError(
                        f"{record_error}; controlled continuation made no "
                        "measurable workspace progress and was stopped",
                        TerminationReason.DEPENDENCY_UNAVAILABLE,
                    )
                else:
                    seen_continuation_states.add(progress.state_sha256)
                    next_continuation_progress = progress
            elif (
                recoverable_stream_failure
                and correction_plan is None
                and progress.made_progress
            ):
                seen_continuation_states.add(progress.state_sha256)
                next_continuation_progress = progress
                failure = execution_failure
            else:
                failure = execution_failure
        else:
            self.workspace_manager.verify_workspace(
                self.workspace,
                expected_commit=input_commit,
                require_clean=True,
            )
            transcript_sha256 = result.telemetry.session_transcript_sha256
            lifecycle = result.telemetry.invocation_lifecycle
            safe_readonly_continuation = (
                result.status is AgentExecutionStatus.UPSTREAM_INCOMPLETE
                and correction_plan is None
                and result.semantic_submission is None
                and result.submission_evidence is not None
                and result.submission_evidence.status is AgentSubmissionStatus.MISSING
                and result.submission_evidence.diagnostic_code
                == "upstream_incomplete_after_terminal_response"
                and result.submission_evidence.tool_call_id is None
                and result.telemetry.tool_evidence_status
                is AgentToolEvidenceStatus.CAPTURED
                and transcript_sha256 is not None
                and result.telemetry.session_id is not None
                and bool(result.telemetry.tool_calls)
                and request.submission_contract is not None
                and all(
                    call.tool_name != request.submission_contract.tool_name
                    for call in result.telemetry.tool_calls
                )
                and lifecycle is not None
                and (lifecycle.response_finalization.terminal_response_observed)
                and lifecycle.shutdown.cleanup_completed
            )
            if safe_readonly_continuation:
                tool_state_sha256 = hashlib.sha256(
                    json.dumps(
                        [
                            (
                                call.tool_name,
                                call.arguments_sha256,
                                call.output_sha256,
                                call.outcome,
                                call.is_error,
                            )
                            for call in result.telemetry.tool_calls
                        ],
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()
                if tool_state_sha256 in seen_readonly_tool_states:
                    failure = DynamicAgentRunnerError(
                        f"{record_error}; read-only continuation "
                        "repeated the same tool observations",
                        TerminationReason.DEPENDENCY_UNAVAILABLE,
                    )
                else:
                    seen_readonly_tool_states.add(tool_state_sha256)
                    next_readonly_continuation = True
                    if agent.capability is AgentCapability.REVIEW:
                        current_review_evidence = ReviewToolEvidenceAttempt(
                            execution_attempt=attempt,
                            tool_calls=result.telemetry.tool_calls,
                        )
            else:
                failure = execution_failure
        return _IncompleteAttemptDisposition(
            failure=failure,
            writer_progress=next_continuation_progress,
            readonly_continuation=next_readonly_continuation,
            review_evidence=current_review_evidence,
        )

    def _accept_completed_attempt(
        self,
        *,
        agent: AgentSpec,
        request: AgentExecutionRequest,
        base_request: AgentExecutionRequest,
        result: AgentExecutionResult,
        assigned_task_ids: tuple[str, ...],
        manual_scope: tuple[str, ...],
        review_evidence_attempts: list[ReviewToolEvidenceAttempt],
        current_review_evidence: ReviewToolEvidenceAttempt | None,
        commands: tuple[CommandEvidence, ...],
        correction_plan: SemanticCorrectionPlan | None,
        seen_correction_fingerprints: set[str],
        input_commit: str,
        snapshot: GitSnapshot | None,
        upstream: Mapping[str, AgentRunOutcome],
    ) -> _CompletedAttemptDisposition:
        """Bind one completed typed response to verified facts and correction state."""
        response_reference: ArtifactReference | None = None
        ignored_fields: tuple[str, ...] = ()
        response_normalizations: tuple[str, ...] = ()
        correction_binding_normalizations: tuple[str, ...] = ()
        response_validation: ResponseValidationDiagnostic | None = None
        next_correction_plan: SemanticCorrectionPlan | None = None
        current_correction_outcome = (
            None if correction_plan is None else SemanticCorrectionOutcome.NOT_EVALUATED
        )
        correction_applied = correction_plan is None
        record_error: str | None = None
        failure: Exception | None = None
        try:
            controller_semantic_payload: dict[str, object] | None = None
            if correction_plan is not None:
                if result.semantic_submission is None:
                    raise AgentArtifactResponseError(
                        "semantic correction omitted its typed submission"
                    )
                application = apply_semantic_correction_with_evidence(
                    result.semantic_submission.payload,
                    correction_plan,
                    response_schema=(
                        None
                        if base_request.submission_contract is None
                        else (base_request.submission_contract.parameters_schema())
                    ),
                )
                controller_semantic_payload = application.payload
                correction_binding_normalizations = application.normalizations
                correction_applied = True
            parsed = parse_dynamic_agent_response(
                result,
                request,
                task_brief=self.task_brief,
                team_plan=self.team_plan,
                assigned_task_ids=assigned_task_ids,
                reviewed_criterion_ids=manual_scope,
                review_tool_evidence_attempts=(
                    *review_evidence_attempts,
                    *(
                        ()
                        if current_review_evidence is None
                        else (current_review_evidence,)
                    ),
                ),
                review_command_evidence=commands,
                controller_semantic_payload=controller_semantic_payload,
            )
        except SemanticCorrectionSubmissionError as error:
            record_error = self._error_detail(error)
            response_validation = error.diagnostic
            response_normalizations = error.normalizations
            next_correction_plan = error.recovery_plan
            current_correction_outcome = (
                SemanticCorrectionOutcome.IMPROVED
                if next_correction_plan is not None
                else SemanticCorrectionOutcome.INVALID_SUBMISSION
            )
            failure = error
        except AgentArtifactResponseError as error:
            record_error = self._error_detail(error)
            response_normalizations = tuple(
                dict.fromkeys(
                    (
                        *correction_binding_normalizations,
                        *error.response_normalizations,
                    )
                )
            )
            response_validation = error.diagnostic
            if correction_plan is None:
                if error.semantic_payload is not None and error.diagnostic is not None:
                    next_correction_plan = build_semantic_correction_plan(
                        error.semantic_payload,
                        error.diagnostic,
                    )
                    if (
                        next_correction_plan is not None
                        and agent.capability is AgentCapability.REVIEW
                    ):
                        next_correction_plan = (
                            bind_review_evidence_correction_candidates(
                                next_correction_plan,
                                evidence_attempts=(
                                    *review_evidence_attempts,
                                    *(
                                        ()
                                        if current_review_evidence is None
                                        else (current_review_evidence,)
                                    ),
                                ),
                                command_evidence=commands,
                            )
                        )
                    seen_correction_fingerprints.add(error.diagnostic.fingerprint)
            elif not correction_applied:
                current_correction_outcome = (
                    SemanticCorrectionOutcome.INVALID_SUBMISSION
                )
            elif error.diagnostic is not None:
                current_correction_outcome = correction_outcome(
                    correction_plan,
                    error.diagnostic,
                    seen_fingerprints=frozenset(seen_correction_fingerprints),
                )
                if (
                    current_correction_outcome is SemanticCorrectionOutcome.IMPROVED
                    and error.semantic_payload is not None
                ):
                    next_correction_plan = build_semantic_correction_plan(
                        error.semantic_payload,
                        error.diagnostic,
                    )
                    if (
                        next_correction_plan is not None
                        and agent.capability is AgentCapability.REVIEW
                    ):
                        next_correction_plan = (
                            bind_review_evidence_correction_candidates(
                                next_correction_plan,
                                evidence_attempts=(
                                    *review_evidence_attempts,
                                    *(
                                        ()
                                        if current_review_evidence is None
                                        else (current_review_evidence,)
                                    ),
                                ),
                                command_evidence=commands,
                            )
                        )
                    seen_correction_fingerprints.add(error.diagnostic.fingerprint)
            else:
                current_correction_outcome = (
                    SemanticCorrectionOutcome.INVALID_SUBMISSION
                )
            failure = error
        except (ValueError, ValidationError) as error:
            record_error = self._error_detail(error)
            if correction_plan is not None:
                current_correction_outcome = (
                    SemanticCorrectionOutcome.INVALID_SUBMISSION
                )
            failure = error
        else:
            ignored_fields = parsed.ignored_controller_fields
            response_normalizations = tuple(
                dict.fromkeys(
                    (
                        *correction_binding_normalizations,
                        *parsed.response_normalizations,
                    )
                )
            )
            if correction_plan is not None:
                current_correction_outcome = SemanticCorrectionOutcome.ACCEPTED
            artifact = self._assemble_response(  # type: ignore[attr-defined]
                agent,
                parsed.body,
                input_commit=input_commit,
                commands=commands,
                manual_scope=manual_scope,
                snapshot=snapshot,
                result=result,
                upstream=upstream,
            )
            response_reference = self.artifact_store.write(
                artifact,
                description=(f"Controller-assembled response from {agent.id}."),
            )
        return _CompletedAttemptDisposition(
            response_reference=response_reference,
            ignored_fields=ignored_fields,
            normalizations=response_normalizations,
            validation=response_validation,
            next_correction_plan=next_correction_plan,
            correction_outcome=current_correction_outcome,
            record_error=record_error,
            failure=failure,
        )

    @staticmethod
    def _execution_termination_reason(
        status: AgentExecutionStatus,
    ) -> TerminationReason:
        """Map raw execution status to scheduler termination reasons."""
        if status is AgentExecutionStatus.TIMED_OUT:
            return TerminationReason.RESOURCE_LIMIT_REACHED
        if status is AgentExecutionStatus.INTERRUPTED:
            return TerminationReason.USER_INTERRUPTED
        if status in {
            AgentExecutionStatus.LAUNCH_FAILED,
            AgentExecutionStatus.INITIALIZATION_STALLED,
            AgentExecutionStatus.RESPONSE_FINALIZATION_STALLED,
            AgentExecutionStatus.PROVIDER_FAILED,
            AgentExecutionStatus.PROVIDER_STALLED,
            AgentExecutionStatus.UPSTREAM_INCOMPLETE,
        }:
            return TerminationReason.DEPENDENCY_UNAVAILABLE
        if status is AgentExecutionStatus.INVALID_RESPONSE:
            return TerminationReason.ARTIFACT_INVALID
        return TerminationReason.EXECUTION_FAILED

    @staticmethod
    def _classify_exception(error: Exception) -> TerminationReason:
        """Classify exceptions into authoritative TerminationReasons."""
        if isinstance(error, DynamicAgentRunnerError):
            return error.reason
        if isinstance(error, AgentBudgetExceeded):
            return TerminationReason.RESOURCE_LIMIT_REACHED
        if isinstance(error, QualityGateBudgetExceeded):
            return TerminationReason.RESOURCE_LIMIT_REACHED
        if isinstance(error, SandboxUnavailableError):
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
                AgentArtifactResponseError,
                AgentPromptError,
                ArtifactAssemblyError,
                ArtifactStoreError,
                ValidationError,
            ),
        ):
            return TerminationReason.ARTIFACT_INVALID
        if isinstance(error, QualityGateError):
            return TerminationReason.EXECUTION_FAILED
        return TerminationReason.CONTROLLER_ERROR

    @staticmethod
    def _sandbox_runtime_error(result: AgentExecutionResult) -> str | None:
        """Extract OpenClaw sandbox connection failures from stderr."""
        for line in result.telemetry.stderr.splitlines():
            if "[tools]" not in line or " failed:" not in line:
                continue
            if (
                "Error response from daemon: container " in line
                and " is not running" in line
            ) or "Cannot connect to the Docker daemon" in line:
                return "OpenClaw sandbox became unavailable during Agent tool use"
        return None

    @staticmethod
    def _error_detail(error: Exception) -> str:
        """Format a concise non-empty error message."""
        return str(error).strip() or type(error).__name__
