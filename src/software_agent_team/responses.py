"""Strict conversion of untrusted Agent text into existing artifact contracts."""

from __future__ import annotations

import json
from collections.abc import Collection

from pydantic import ValidationError

from software_agent_team.artifacts import (
    AgentRole,
    IterationArtifact,
    PhaseArtifact,
    TaskBrief,
    parse_phase_artifact,
    validate_artifact_context,
)
from software_agent_team.execution import (
    AgentExecutionRequest,
    AgentExecutionResult,
    AgentExecutionStatus,
)


class AgentArtifactResponseError(ValueError):
    """Raised when an Agent response cannot become attributable run evidence."""


def _reject_nonstandard_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError(f"duplicate JSON object key: {key}")
        payload[key] = value
    return payload


def parse_agent_artifact(
    result: AgentExecutionResult,
    request: AgentExecutionRequest,
    *,
    task_brief: TaskBrief,
    team_roles: Collection[AgentRole],
    iteration_limit: int,
) -> PhaseArtifact:
    """Validate one pure-JSON Agent response against its complete run context."""

    if result.status is not AgentExecutionStatus.COMPLETED:
        raise AgentArtifactResponseError(
            f"Agent execution did not complete: {result.status.value}"
        )
    if result.telemetry.role is not request.role:
        raise AgentArtifactResponseError(
            "execution telemetry role does not match request"
        )
    if result.telemetry.session_key != request.session_key:
        raise AgentArtifactResponseError(
            "execution telemetry session does not match request"
        )
    if not task_brief.confirmed:
        raise AgentArtifactResponseError(
            "Agent responses require a confirmed task brief"
        )
    if task_brief.run_id != request.run_id:
        raise AgentArtifactResponseError("request run ID does not match the task brief")
    if request.role not in team_roles:
        raise AgentArtifactResponseError("requested Agent role is not part of the team")
    if not 1 <= iteration_limit <= 3 or request.iteration > iteration_limit:
        raise AgentArtifactResponseError("request exceeds the run iteration limit")

    try:
        payload = json.loads(
            result.response_text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonstandard_constant,
        )
    except (TypeError, ValueError) as error:
        raise AgentArtifactResponseError(
            "Agent response must contain exactly one JSON object "
            "without prose or fences"
        ) from error
    if not isinstance(payload, dict):
        raise AgentArtifactResponseError("Agent response must be a JSON object")

    try:
        artifact = parse_phase_artifact(payload)
    except (ValueError, ValidationError) as error:
        raise AgentArtifactResponseError(
            "Agent response artifact is invalid"
        ) from error
    if not isinstance(artifact, PhaseArtifact):
        raise AgentArtifactResponseError("Agent returned a non-phase runtime artifact")
    if artifact.kind is not request.expected_kind:
        raise AgentArtifactResponseError(
            f"Agent returned {artifact.kind.value}; "
            f"expected {request.expected_kind.value}"
        )
    if artifact.producer is not request.role:
        raise AgentArtifactResponseError(
            "Agent response producer does not match the invoked role"
        )
    if artifact.run_id != request.run_id or artifact.team_id != request.team_id:
        raise AgentArtifactResponseError(
            "Agent response does not match the requested run context"
        )
    if (
        isinstance(artifact, IterationArtifact)
        and artifact.iteration != request.iteration
    ):
        raise AgentArtifactResponseError(
            "Agent response iteration does not match the request"
        )
    try:
        validate_artifact_context(
            artifact,
            task_brief=task_brief,
            team_id=request.team_id,
            team_roles=set(team_roles),
            iteration_limit=iteration_limit,
        )
    except ValueError as error:
        raise AgentArtifactResponseError(
            "Agent response is invalid in the frozen run context"
        ) from error
    return artifact
