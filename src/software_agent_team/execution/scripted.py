"""Scripted test-double executor for offline and deterministic testing."""

from __future__ import annotations

import hashlib
import json
import time
from collections import deque
from collections.abc import Iterable, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.types import JsonValue

from software_agent_team.artifacts import (
    AgentToolCallEvidence,
    AgentToolCallOutcome,
    AgentToolEvidenceStatus,
    PhaseArtifact,
    ProviderLivenessEvidence,
)
from software_agent_team.execution.constants import (
    DEFAULT_INITIALIZATION_NO_PROGRESS_SECONDS,
    DEFAULT_INITIALIZATION_STALL_GRACE_SECONDS,
    DEFAULT_PROCESS_SHUTDOWN_GRACE_SECONDS,
    DEFAULT_RESPONSE_FINALIZATION_NO_PROGRESS_SECONDS,
    DEFAULT_RESPONSE_FINALIZATION_STALL_GRACE_SECONDS,
)
from software_agent_team.execution.liveness import (
    InitializationLivenessPolicy,
    ResponseFinalizationPolicy,
    WallClock,
    _InvocationLifecycleRecorder,
    _system_clock,
)
from software_agent_team.execution.models import (
    AgentCapability,
    AgentExecutionActivityHandler,
    AgentExecutionRequest,
    AgentExecutionResult,
    AgentExecutionStatus,
    AgentExecutionTelemetry,
    AgentExecutor,
    AgentTokenUsage,
    ScriptedResponseExhaustedError,
)
from software_agent_team.invocation_lifecycle import (
    InitializationCheckpoint,
    InitializationLivenessEvidence,
    InvocationPhase,
    InvocationStopReason,
)
from software_agent_team.submissions import (
    ARTIFACT_SUBMISSION_ARGUMENT,
    AgentSemanticSubmission,
    AgentSubmissionEvidence,
    AgentSubmissionStatus,
    canonical_json_sha256,
)


class ScriptedAgentResponse(BaseModel):
    """One deterministic reply consumed by the offline execution adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(min_length=1)
    model: str = "scripted/model"
    provider: str = "scripted"
    session_id: str | None = None
    usage: AgentTokenUsage | None = None
    duration_ms: int = Field(default=0, ge=0)
    stderr: str = ""
    provider_liveness: ProviderLivenessEvidence | None = None
    tool_calls: tuple[AgentToolCallEvidence, ...] = ()
    submission_payload: dict[str, JsonValue] | None = None

    @field_validator("text", "model", "provider")
    @classmethod
    def require_nonblank_text(cls, value: str) -> str:
        """Reject unusable scripted metadata and responses."""

        if not value.strip():
            raise ValueError("scripted response fields must not be blank")
        return value


type ScriptedInput = ScriptedAgentResponse | AgentExecutionResult | PhaseArtifact | str


def _scripted_review_tool_call() -> AgentToolCallEvidence:
    """Return explicit synthetic tool evidence for the offline scripted adapter."""

    output = b"scripted-review-observation"
    return AgentToolCallEvidence(
        id="tool-001",
        tool_name="read",
        external_call_sha256=hashlib.sha256(b"scripted-tool-001").hexdigest(),
        arguments_sha256=hashlib.sha256(b'{"path":"/agent"}').hexdigest(),
        outcome=AgentToolCallOutcome.SUCCEEDED,
        is_error=False,
        output_sha256=hashlib.sha256(output).hexdigest(),
        output_bytes=len(output),
        output_excerpt=output.decode("utf-8"),
    )


class ScriptedAgentExecutor:
    """FIFO Agent adapter for deterministic offline workflow and recovery tests."""

    def __init__(
        self,
        responses: Iterable[ScriptedInput],
        *,
        clock: WallClock = _system_clock,
    ) -> None:
        self._responses = deque(responses)
        self.clock = clock
        self.requests: list[AgentExecutionRequest] = []

    @property
    def remaining(self) -> int:
        """Return the number of unused scripted responses."""

        return len(self._responses)

    def execute(
        self,
        request: AgentExecutionRequest,
        *,
        activity_handler: AgentExecutionActivityHandler | None = None,
    ) -> AgentExecutionResult:
        """Consume one predeclared response and record the exact request."""

        started_monotonic = time.monotonic()
        lifecycle = _InvocationLifecycleRecorder(
            request=request,
            started_monotonic=started_monotonic,
            process_grace_seconds=DEFAULT_PROCESS_SHUTDOWN_GRACE_SECONDS,
            initialization_policy=InitializationLivenessPolicy(
                no_progress_seconds=DEFAULT_INITIALIZATION_NO_PROGRESS_SECONDS,
                stall_grace_seconds=DEFAULT_INITIALIZATION_STALL_GRACE_SECONDS,
                source="scripted deterministic adapter",
            ),
            response_finalization_policy=ResponseFinalizationPolicy(
                no_progress_seconds=(DEFAULT_RESPONSE_FINALIZATION_NO_PROGRESS_SECONDS),
                stall_grace_seconds=(DEFAULT_RESPONSE_FINALIZATION_STALL_GRACE_SECONDS),
                source="scripted deterministic adapter",
            ),
            activity_handler=activity_handler,
            monotonic=time.monotonic,
        )
        lifecycle.transition(
            InvocationPhase.INITIALIZING,
            now=started_monotonic,
            action="Scripted invocation initialized its attributable current turn",
        )
        lifecycle.set_initialization_evidence(
            InitializationLivenessEvidence(
                mode="enforced",
                policy_source="scripted deterministic adapter",
                no_progress_seconds=DEFAULT_INITIALIZATION_NO_PROGRESS_SECONDS,
                stall_grace_seconds=DEFAULT_INITIALIZATION_STALL_GRACE_SECONDS,
                checkpoints=(InitializationCheckpoint.CURRENT_TURN,),
            )
        )
        lifecycle.provider_ready(
            InitializationCheckpoint.CURRENT_TURN,
            now=started_monotonic,
        )
        self.requests.append(request)
        if not self._responses:
            raise ScriptedResponseExhaustedError(
                f"no scripted response remains for {request.agent_id}"
            )
        scripted = self._responses.popleft()
        if isinstance(scripted, AgentExecutionResult):
            return scripted
        if isinstance(scripted, BaseModel):
            if isinstance(scripted, ScriptedAgentResponse):
                response = scripted
            else:
                response = ScriptedAgentResponse(
                    text=json.dumps(
                        scripted.model_dump(mode="json"),
                        ensure_ascii=False,
                    )
                )
        else:
            response = ScriptedAgentResponse(text=scripted)

        now = self.clock()
        tool_calls = (
            response.tool_calls or (_scripted_review_tool_call(),)
            if request.capability is AgentCapability.REVIEW
            else response.tool_calls
        )
        submission_payload = response.submission_payload
        if submission_payload is None and request.submission_contract is not None:
            try:
                candidate = json.loads(response.text)
            except (TypeError, ValueError, json.JSONDecodeError):
                candidate = None
            if isinstance(candidate, dict):
                submission_payload = candidate
        semantic_submission: AgentSemanticSubmission | None = None
        submission_evidence: AgentSubmissionEvidence | None = None
        if request.submission_contract is not None and submission_payload is not None:
            external_id = f"scripted-submission-{len(tool_calls) + 1:03d}"
            output = b"scripted-semantic-submission"
            submission_call = AgentToolCallEvidence(
                id=f"tool-{len(tool_calls) + 1:03d}",
                tool_name=request.submission_contract.tool_name,
                external_call_sha256=hashlib.sha256(
                    external_id.encode("utf-8")
                ).hexdigest(),
                arguments_sha256=canonical_json_sha256(
                    {ARTIFACT_SUBMISSION_ARGUMENT: submission_payload}
                ),
                outcome=AgentToolCallOutcome.SUCCEEDED,
                is_error=False,
                output_sha256=hashlib.sha256(output).hexdigest(),
                output_bytes=len(output),
                output_excerpt=output.decode("utf-8"),
            )
            tool_calls = (*tool_calls, submission_call)
            binding_sha256 = hashlib.sha256(
                (
                    f"{request.session_key}\x00"
                    f"{request.submission_contract.schema_sha256}"
                ).encode()
            ).hexdigest()
            submission_evidence = AgentSubmissionEvidence(
                protocol=request.submission_contract.protocol,
                purpose=request.submission_contract.purpose,
                status=AgentSubmissionStatus.ACCEPTED,
                schema_sha256=request.submission_contract.schema_sha256,
                binding_sha256=binding_sha256,
                tool_call_id=submission_call.id,
                payload_sha256=canonical_json_sha256(
                    {ARTIFACT_SUBMISSION_ARGUMENT: submission_payload}
                ),
                semantic_payload_sha256=canonical_json_sha256(submission_payload),
            )
            semantic_submission = AgentSemanticSubmission(
                payload=submission_payload,
                evidence=submission_evidence,
            )
        transcript_sha256 = (
            hashlib.sha256(
                f"{request.session_key}\n{request.prompt}\n{response.text}".encode()
            ).hexdigest()
            if tool_calls
            else None
        )
        telemetry = AgentExecutionTelemetry(
            role=request.role,
            agent_id=request.agent_id,
            capability=request.capability,
            specialization=request.specialization,
            session_key=request.session_key,
            command=("scripted-agent", request.agent_id),
            started_at=now,
            finished_at=now,
            duration_ms=response.duration_ms,
            openclaw_duration_ms=response.duration_ms,
            exit_code=0,
            stdout=response.text,
            stderr=response.stderr,
            openclaw_run_id=f"scripted-{request.run_id}-{request.iteration}",
            session_id=response.session_id
            or f"scripted-{request.run_id}-{request.agent_id}-{request.iteration}",
            provider=response.provider,
            model=response.model,
            usage=response.usage,
            provider_liveness=response.provider_liveness,
            tool_evidence_status=(
                AgentToolEvidenceStatus.CAPTURED
                if tool_calls
                else AgentToolEvidenceStatus.NOT_CAPTURED
            ),
            session_transcript_sha256=transcript_sha256,
            session_record_count=(max(3, 2 + len(tool_calls)) if tool_calls else None),
            tool_calls=tool_calls,
        )
        result = AgentExecutionResult(
            status=AgentExecutionStatus.COMPLETED,
            response_text=response.text,
            telemetry=telemetry,
            semantic_submission=semantic_submission,
            submission_evidence=submission_evidence,
        )
        lifecycle.request_stop(
            InvocationStopReason.COMPLETED,
            now=started_monotonic + (response.duration_ms / 1000),
            action="Scripted invocation completed; collecting deterministic evidence",
        )
        evidence = lifecycle.finalize(
            reason=InvocationStopReason.COMPLETED,
            telemetry=telemetry,
            submission_evidence=submission_evidence,
        )
        return result.model_copy(
            update={
                "telemetry": telemetry.model_copy(
                    update={"invocation_lifecycle": evidence}
                )
            }
        )


def scripted_executor(responses: Sequence[ScriptedInput]) -> AgentExecutor:
    """Return a protocol-typed scripted executor for dependency injection."""

    return ScriptedAgentExecutor(responses)
