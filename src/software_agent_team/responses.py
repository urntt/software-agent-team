"""Strict conversion of untrusted Agent text into semantic response bodies."""

from __future__ import annotations

import json
from collections.abc import Collection
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from software_agent_team.artifacts import (
    AgentRole,
    ArtifactKind,
    ImplementationPlan,
    PlanTask,
    ReviewFinding,
    ReviewReport,
    ReviewTerminationReason,
    ReviewVerdict,
    TaskBrief,
    validate_artifact_context,
)
from software_agent_team.execution import (
    AgentExecutionRequest,
    AgentExecutionResult,
    AgentExecutionStatus,
)


class AgentArtifactResponseError(ValueError):
    """Raised when an Agent response cannot become attributable run evidence."""


def _clean_unique_text(values: tuple[str, ...]) -> tuple[str, ...]:
    cleaned = tuple(value.strip() for value in values)
    if any(not value for value in cleaned):
        raise ValueError("response text values must not be blank")
    if len(cleaned) != len(set(cleaned)):
        raise ValueError("response text values must be unique")
    return cleaned


class ImplementationPlanResponse(BaseModel):
    """Planner reasoning; the controller supplies the artifact envelope."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    objective: str = Field(min_length=1)
    approach: tuple[str, ...] = Field(min_length=1)
    tasks: tuple[PlanTask, ...] = Field(min_length=1)
    risks: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()

    @field_validator("objective")
    @classmethod
    def require_clean_objective(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("objective must not be blank")
        return cleaned

    @field_validator("approach", "risks", "assumptions")
    @classmethod
    def require_clean_unique_text(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _clean_unique_text(values)


class WorkResultResponse(BaseModel):
    """Developer-authored semantic result; Git facts remain controller-owned."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: str = Field(min_length=1)
    completed_tasks: tuple[str, ...] = Field(min_length=1)
    unresolved_issues: tuple[str, ...] = ()

    @field_validator("summary")
    @classmethod
    def require_clean_summary(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("summary must not be blank")
        return cleaned

    @field_validator("completed_tasks", "unresolved_issues")
    @classmethod
    def require_clean_unique_text(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _clean_unique_text(values)


class TestReportResponse(BaseModel):
    """Tester analysis; deterministic statuses and evidence are controller-owned."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    findings: tuple[str, ...] = ()
    summary: str = Field(min_length=1)

    @field_validator("findings")
    @classmethod
    def require_clean_unique_findings(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        return _clean_unique_text(values)

    @field_validator("summary")
    @classmethod
    def require_clean_summary(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("summary must not be blank")
        return cleaned


class ReviewReportResponse(BaseModel):
    """Reviewer's semantic verdict; immutable commit and scope are controller-owned."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    verdict: ReviewVerdict = Field(
        description=(
            "Use revise for correctable defects. Use fail only when continuing "
            "would cross a safety or evidence-integrity boundary."
        )
    )
    termination_reason: ReviewTerminationReason | None = None
    findings: tuple[ReviewFinding, ...] = ()
    summary: str = Field(min_length=1)

    @field_validator("summary")
    @classmethod
    def require_clean_summary(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("summary must not be blank")
        return cleaned


type AgentResponseBody = (
    ImplementationPlanResponse
    | WorkResultResponse
    | TestReportResponse
    | ReviewReportResponse
)


RESPONSE_BODY_MODELS: dict[ArtifactKind, type[AgentResponseBody]] = {
    ArtifactKind.IMPLEMENTATION_PLAN: ImplementationPlanResponse,
    ArtifactKind.WORK_RESULT: WorkResultResponse,
    ArtifactKind.TEST_REPORT: TestReportResponse,
    ArtifactKind.REVIEW_REPORT: ReviewReportResponse,
}

_COMMON_CONTROLLER_FIELDS = {
    "schema_version",
    "kind",
    "run_id",
    "team_id",
    "producer",
    "created_at",
    "iteration",
}
_CONTROLLER_FIELDS: dict[ArtifactKind, frozenset[str]] = {
    ArtifactKind.IMPLEMENTATION_PLAN: frozenset(_COMMON_CONTROLLER_FIELDS),
    ArtifactKind.WORK_RESULT: frozenset(
        _COMMON_CONTROLLER_FIELDS | {"input_commit", "output_commit", "changed_files"}
    ),
    ArtifactKind.TEST_REPORT: frozenset(
        _COMMON_CONTROLLER_FIELDS
        | {
            "input_commit",
            "status",
            "commands",
            "criteria",
            "manual_review_criteria",
            "blockers",
        }
    ),
    ArtifactKind.REVIEW_REPORT: frozenset(
        _COMMON_CONTROLLER_FIELDS | {"input_commit", "reviewed_criteria"}
    ),
}


def controller_fields_for(kind: ArtifactKind) -> tuple[str, ...]:
    """Return fields that the controller binds for one response contract."""

    fields = _CONTROLLER_FIELDS.get(kind)
    if fields is None:
        raise ValueError(f"no Agent response contract exists for {kind.value}")
    return tuple(sorted(fields))


@dataclass(frozen=True)
class ParsedAgentResponse:
    """Validated semantic body plus ignored controller-owned response fields."""

    body: AgentResponseBody
    ignored_controller_fields: tuple[str, ...]


def _safe_validation_detail(error: ValueError) -> str:
    """Return bounded schema diagnostics without reflecting raw response values."""

    if isinstance(error, ValidationError):
        issues = []
        for issue in error.errors(
            include_url=False,
            include_context=False,
            include_input=False,
        ):
            location = ".".join(str(item) for item in issue["loc"]) or "response"
            issues.append(f"{location}: {issue['msg']}")
        return "; ".join(issues)[:1000]
    return str(error)[:1000]


def _reject_nonstandard_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError(f"duplicate JSON object key: {key}")
        payload[key] = value
    return payload


def _safe_json_detail(error: TypeError | ValueError) -> str:
    """Describe JSON structure failures without reflecting response values."""

    if isinstance(error, json.JSONDecodeError):
        return f"{error.msg} at line {error.lineno} column {error.colno}"
    detail = str(error)
    if detail.startswith(("duplicate JSON object key:", "non-standard JSON constant:")):
        return detail[:200]
    return "response could not be decoded as one JSON object"


def _unwrap_single_json_fence(value: str) -> str:
    """Normalize one unambiguous ``json`` fence and presentation-only prose."""

    lines = value.strip().splitlines()
    openings = [
        index
        for index, line in enumerate(lines)
        if line.strip().casefold() == "```json"
    ]
    closings = [index for index, line in enumerate(lines) if line.strip() == "```"]
    if len(openings) != 1 or len(closings) != 1:
        return value

    opening = openings[0]
    closing = closings[0]
    if opening >= closing or closing - opening < 2:
        return value

    outside = "\n".join(lines[:opening] + lines[closing + 1 :]).strip()
    if "```" in outside or any(character in outside for character in "{}[]"):
        return value
    return "\n".join(lines[opening + 1 : closing])


def _unwrap_single_json_object(value: str) -> str:
    """Extract one unambiguous object with presentation-only transport noise."""

    stripped = value.strip()
    opening = stripped.find("{")
    if opening < 0:
        return value
    prefix = stripped[:opening].strip()
    try:
        parsed, closing = json.JSONDecoder().raw_decode(stripped, idx=opening)
    except ValueError:
        return value
    if not isinstance(parsed, dict):
        return value
    suffix = stripped[closing:].strip()
    if 1 <= len(suffix) <= 4 and all(character in "]}" for character in suffix):
        # Some providers close an already-complete top-level object again. A
        # short suffix containing only unmatched closing delimiters cannot
        # introduce another value, so discarding it is deterministic. Raw
        # transport output remains in the execution evidence.
        suffix = ""
    outside = "\n".join(part for part in (prefix, suffix) if part)
    if "```" in outside or any(character in outside for character in "{}[]"):
        return value
    return stripped[opening:closing]


def _validate_response_context(
    body: AgentResponseBody,
    request: AgentExecutionRequest,
    result: AgentExecutionResult,
    *,
    task_brief: TaskBrief,
    team_roles: Collection[AgentRole],
    iteration_limit: int,
) -> None:
    """Apply semantic checks that depend on the frozen controller context."""

    artifact: ImplementationPlan | ReviewReport | None = None
    if isinstance(body, ImplementationPlanResponse):
        artifact = ImplementationPlan(
            run_id=request.run_id,
            team_id=request.team_id,
            created_at=result.telemetry.finished_at,
            objective=body.objective,
            approach=body.approach,
            tasks=body.tasks,
            risks=body.risks,
            assumptions=body.assumptions,
        )
    elif isinstance(body, ReviewReportResponse):
        if body.verdict is ReviewVerdict.FAIL and body.termination_reason is None:
            raise ValueError("failed reviews require a terminal review reason")
        artifact = ReviewReport(
            run_id=request.run_id,
            team_id=request.team_id,
            created_at=result.telemetry.finished_at,
            iteration=request.iteration,
            input_commit="0" * 40,
            verdict=body.verdict,
            termination_reason=body.termination_reason,
            reviewed_criteria=(),
            findings=body.findings,
            summary=body.summary,
        )
    if artifact is None:
        return
    validate_artifact_context(
        artifact,
        task_brief=task_brief,
        team_id=request.team_id,
        team_roles=set(team_roles),
        iteration_limit=iteration_limit,
    )


def parse_agent_response(
    result: AgentExecutionResult,
    request: AgentExecutionRequest,
    *,
    task_brief: TaskBrief,
    team_roles: Collection[AgentRole],
    iteration_limit: int,
) -> ParsedAgentResponse:
    """Validate one Agent semantic body and bind it to its execution context."""

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
        normalized = _unwrap_single_json_object(
            _unwrap_single_json_fence(result.response_text)
        )
        payload = json.loads(
            normalized,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonstandard_constant,
        )
    except (TypeError, ValueError) as error:
        raise AgentArtifactResponseError(
            "Agent response JSON is invalid: "
            f"{_safe_json_detail(error)}. The response must contain exactly one "
            "unambiguous JSON object; a single json fence or presentation-only "
            "surrounding prose and a bounded redundant closing-delimiter suffix "
            "are normalized, but multiple fences or outside JSON structures are "
            "forbidden"
        ) from error
    if not isinstance(payload, dict):
        raise AgentArtifactResponseError("Agent response must be a JSON object")

    model = RESPONSE_BODY_MODELS.get(request.expected_kind)
    controller_fields = _CONTROLLER_FIELDS.get(request.expected_kind)
    if model is None or controller_fields is None:
        raise AgentArtifactResponseError(
            f"no response body contract exists for {request.expected_kind.value}"
        )
    ignored = tuple(sorted(controller_fields.intersection(payload)))
    semantic_payload = {
        key: value for key, value in payload.items() if key not in controller_fields
    }
    try:
        body = model.model_validate(semantic_payload)
        _validate_response_context(
            body,
            request,
            result,
            task_brief=task_brief,
            team_roles=team_roles,
            iteration_limit=iteration_limit,
        )
    except (ValueError, ValidationError) as error:
        raise AgentArtifactResponseError(
            f"Agent semantic response is invalid: {_safe_validation_detail(error)}"
        ) from error
    return ParsedAgentResponse(
        body=body,
        ignored_controller_fields=ignored,
    )
