"""Strict conversion of untrusted Agent text into semantic response bodies."""

from __future__ import annotations

import json
from collections.abc import Collection
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from software_agent_team.artifacts import (
    AgentRole,
    AgentToolEvidenceStatus,
    ArtifactKind,
    ImplementationPlan,
    PlanTask,
    ReviewCriterionAssessment,
    ReviewCriterionStatus,
    ReviewFinding,
    ReviewReport,
    ReviewTerminationReason,
    ReviewToolEvidenceReference,
    ReviewVerdict,
    TaskBrief,
    validate_artifact_context,
)
from software_agent_team.execution import (
    AgentExecutionRequest,
    AgentExecutionResult,
    AgentExecutionStatus,
)
from software_agent_team.integrity import canonical_model_sha256
from software_agent_team.teams import TeamPlan, capability_for_legacy_role


class AgentArtifactResponseError(ValueError):
    """Raised when an Agent response cannot become attributable run evidence."""


def _clean_unique_text(values: tuple[str, ...]) -> tuple[str, ...]:
    cleaned = tuple(value.strip() for value in values)
    if any(not value for value in cleaned):
        raise ValueError("response text values must not be blank")
    if len(cleaned) != len(set(cleaned)):
        raise ValueError("response text values must be unique")
    return cleaned


def _ground_review_tool_evidence(
    body: ReviewReportResponse,
    result: AgentExecutionResult,
) -> GroundedReviewReportResponse:
    """Resolve model-visible result fragments to controller-owned tool-call IDs."""

    if not body.criterion_assessments:
        return GroundedReviewReportResponse(
            verdict=body.verdict,
            termination_reason=body.termination_reason,
            findings=body.findings,
            summary=body.summary,
        )
    telemetry = result.telemetry
    if telemetry.tool_evidence_status is AgentToolEvidenceStatus.INVALID:
        raise ValueError(
            "review tool evidence is invalid: "
            f"{telemetry.tool_evidence_error or 'unknown session error'}"
        )
    if telemetry.tool_evidence_status is not AgentToolEvidenceStatus.CAPTURED:
        raise ValueError("review tool evidence was not captured")
    grounded_assessments: list[ReviewCriterionAssessment] = []
    for assessment in body.criterion_assessments:
        references: list[ReviewToolEvidenceReference] = []
        for claim in assessment.tool_evidence:
            matches = tuple(
                call
                for call in telemetry.tool_calls
                if claim.observable in call.output_excerpt
            )
            if not matches:
                raise ValueError(
                    f"criterion {assessment.criterion_id} evidence fragment does "
                    "not match any current tool result"
                )
            if len(matches) != 1:
                raise ValueError(
                    f"criterion {assessment.criterion_id} evidence fragment "
                    "matches multiple current tool results"
                )
            references.append(
                ReviewToolEvidenceReference(
                    tool_call_id=matches[0].id,
                    observable=claim.observable,
                )
            )
        identifiers = [reference.tool_call_id for reference in references]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError(
                f"criterion {assessment.criterion_id} cites the same current "
                "tool result more than once"
            )
        grounded_assessments.append(
            ReviewCriterionAssessment(
                criterion_id=assessment.criterion_id,
                status=assessment.status,
                adversarial_check=assessment.adversarial_check,
                evidence=assessment.evidence,
                tool_evidence=tuple(references),
            )
        )
    return GroundedReviewReportResponse(
        verdict=body.verdict,
        termination_reason=body.termination_reason,
        criterion_assessments=tuple(grounded_assessments),
        findings=body.findings,
        summary=body.summary,
    )


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


class ReviewToolEvidenceClaim(BaseModel):
    """Model-visible exact fragment used to select one current tool result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    observable: str = Field(min_length=1, max_length=256)

    @field_validator("observable")
    @classmethod
    def require_clean_observable(cls, value: str) -> str:
        """Keep semantic selectors small, exact, and text-safe."""

        cleaned = value.strip()
        if not cleaned or "\x00" in cleaned:
            raise ValueError("tool evidence observables must be nonblank text")
        return cleaned


class ReviewCriterionAssessmentResponse(BaseModel):
    """Reviewer assessment with model-visible result-fragment selectors."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    criterion_id: str = Field(min_length=1, pattern=r"^[A-Z][A-Z0-9_-]*$")
    status: ReviewCriterionStatus
    adversarial_check: str = Field(min_length=1, max_length=2000)
    evidence: str = Field(min_length=1, max_length=2000)
    tool_evidence: tuple[ReviewToolEvidenceClaim, ...] = Field(min_length=1)

    @field_validator("adversarial_check", "evidence")
    @classmethod
    def require_clean_assessment_text(cls, value: str) -> str:
        """Reject empty presentation-only assessment text."""

        cleaned = value.strip()
        if not cleaned:
            raise ValueError("criterion assessment text must not be blank")
        return cleaned

    @field_validator("tool_evidence")
    @classmethod
    def require_unique_tool_evidence(
        cls,
        values: tuple[ReviewToolEvidenceClaim, ...],
    ) -> tuple[ReviewToolEvidenceClaim, ...]:
        """Make every semantic result selector distinct within one criterion."""

        observables = [value.observable for value in values]
        if len(observables) != len(set(observables)):
            raise ValueError("criterion tool-evidence observables must be unique")
        return values


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
    criterion_assessments: tuple[ReviewCriterionAssessmentResponse, ...] = ()
    findings: tuple[ReviewFinding, ...] = ()
    summary: str = Field(min_length=1)

    @field_validator("summary")
    @classmethod
    def require_clean_summary(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("summary must not be blank")
        return cleaned


class GroundedReviewReportResponse(BaseModel):
    """Controller-resolved Review semantics ready for artifact assembly."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    verdict: ReviewVerdict
    termination_reason: ReviewTerminationReason | None = None
    criterion_assessments: tuple[ReviewCriterionAssessment, ...] = ()
    findings: tuple[ReviewFinding, ...] = ()
    summary: str = Field(min_length=1)

    @field_validator("summary")
    @classmethod
    def require_clean_summary(cls, value: str) -> str:
        """Reject whitespace-only controller-bound summaries."""

        cleaned = value.strip()
        if not cleaned:
            raise ValueError("summary must not be blank")
        return cleaned


type AgentResponseBody = (
    ImplementationPlanResponse
    | WorkResultResponse
    | TestReportResponse
    | ReviewReportResponse
    | GroundedReviewReportResponse
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


def _parse_semantic_body(
    value: str,
    expected_kind: ArtifactKind,
) -> ParsedAgentResponse:
    payload = parse_json_object_response(value)
    model = RESPONSE_BODY_MODELS.get(expected_kind)
    controller_fields = _CONTROLLER_FIELDS.get(expected_kind)
    if model is None or controller_fields is None:
        raise AgentArtifactResponseError(
            f"no response body contract exists for {expected_kind.value}"
        )
    ignored = tuple(sorted(controller_fields.intersection(payload)))
    semantic_payload = {
        key: item for key, item in payload.items() if key not in controller_fields
    }
    try:
        body = model.model_validate(semantic_payload)
    except (ValueError, ValidationError) as error:
        raise AgentArtifactResponseError(
            f"Agent semantic response is invalid: {_safe_validation_detail(error)}"
        ) from error
    return ParsedAgentResponse(body=body, ignored_controller_fields=ignored)


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


def _contains_json_object(value: str) -> bool:
    """Return whether text contains another decodable JSON object candidate.

    The response contract requires an object. A JSON array used to present an
    argv sequence therefore cannot compete with the one semantic object. An
    object nested inside an array remains detectable from its opening brace.
    """

    decoder = json.JSONDecoder()
    for index, character in enumerate(value):
        if character != "{":
            continue
        try:
            parsed, _ = decoder.raw_decode(value, idx=index)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return True
    return False


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
    if "```" in outside or _contains_json_object(outside):
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
    if (
        "```" in outside
        or any(character in outside for character in "{}")
        or _contains_json_object(outside)
    ):
        return value
    return stripped[opening:closing]


def parse_json_object_response(value: str) -> dict[str, object]:
    """Decode one unambiguous model JSON object with bounded transport repair.

    This parser deliberately normalizes only presentation noise that cannot
    change the semantic object. Schema-specific validation remains the caller's
    responsibility.
    """

    try:
        normalized = _unwrap_single_json_object(_unwrap_single_json_fence(value))
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
            "are normalized, but multiple fences or outside JSON object "
            "candidates are forbidden"
        ) from error
    if not isinstance(payload, dict):
        raise AgentArtifactResponseError("Agent response must be a JSON object")
    return payload


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
    elif isinstance(body, GroundedReviewReportResponse):
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
            reviewed_criteria=tuple(
                assessment.criterion_id for assessment in body.criterion_assessments
            ),
            criterion_assessments=body.criterion_assessments,
            findings=body.findings,
            summary=body.summary,
        )
    if artifact is None:
        return
    validate_artifact_context(
        artifact,
        task_brief=task_brief,
        team_id=request.team_id,
        team_agents={
            role.value: capability_for_legacy_role(role).value for role in team_roles
        },
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
    if result.telemetry.agent_id != request.agent_id:
        raise AgentArtifactResponseError(
            "execution telemetry Agent ID does not match request"
        )
    if result.telemetry.capability is not request.capability:
        raise AgentArtifactResponseError(
            "execution telemetry capability does not match request"
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
    if request.role is None:
        raise AgentArtifactResponseError(
            "legacy artifact parsing requires a fixed Agent role"
        )
    if request.role not in team_roles:
        raise AgentArtifactResponseError("requested Agent role is not part of the team")
    if not 1 <= iteration_limit <= 3 or request.iteration > iteration_limit:
        raise AgentArtifactResponseError("request exceeds the run iteration limit")

    parsed = _parse_semantic_body(result.response_text, request.expected_kind)
    try:
        body = parsed.body
        if isinstance(body, ReviewReportResponse):
            body = _ground_review_tool_evidence(body, result)
            parsed = ParsedAgentResponse(
                body=body,
                ignored_controller_fields=parsed.ignored_controller_fields,
            )
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
    return parsed


def parse_dynamic_agent_response(
    result: AgentExecutionResult,
    request: AgentExecutionRequest,
    *,
    task_brief: TaskBrief,
    team_plan: TeamPlan,
    assigned_task_ids: Collection[str] = (),
    reviewed_criterion_ids: Collection[str] = (),
) -> ParsedAgentResponse:
    """Bind one semantic response to an approved run-scoped AgentSpec."""

    if result.status is not AgentExecutionStatus.COMPLETED:
        raise AgentArtifactResponseError(
            f"Agent execution did not complete: {result.status.value}"
        )
    if request.role is not None or result.telemetry.role is not None:
        raise AgentArtifactResponseError(
            "dynamic response parsing does not accept a fixed legacy role"
        )
    if result.telemetry.agent_id != request.agent_id:
        raise AgentArtifactResponseError(
            "execution telemetry Agent ID does not match request"
        )
    if result.telemetry.capability is not request.capability:
        raise AgentArtifactResponseError(
            "execution telemetry capability does not match request"
        )
    if result.telemetry.session_key != request.session_key:
        raise AgentArtifactResponseError(
            "execution telemetry session does not match request"
        )
    if not task_brief.confirmed:
        raise AgentArtifactResponseError(
            "Agent responses require a confirmed task brief"
        )
    if request.run_id != task_brief.run_id or team_plan.run_id != task_brief.run_id:
        raise AgentArtifactResponseError("dynamic response run IDs do not match")
    if request.team_id != team_plan.team_id:
        raise AgentArtifactResponseError("dynamic response team IDs do not match")
    if canonical_model_sha256(task_brief) != team_plan.task_brief_sha256:
        raise AgentArtifactResponseError("TeamPlan does not bind the TaskBrief")
    if request.iteration > team_plan.iteration_limit:
        raise AgentArtifactResponseError("request exceeds the TeamPlan iteration limit")
    try:
        agent = team_plan.get_agent(request.agent_id)
    except ValueError as error:
        raise AgentArtifactResponseError(str(error)) from error
    if (
        request.capability is not agent.capability
        or request.expected_kind is not agent.expected_output
    ):
        raise AgentArtifactResponseError(
            "dynamic request differs from the approved AgentSpec"
        )
    authorized_route_ids = team_plan.model_routes.authorized_route_ids(agent.id)
    authorized_routes = tuple(
        team_plan.model_routes.get_route(route_id) for route_id in authorized_route_ids
    )
    matching_routes = tuple(
        route for route in authorized_routes if route.model == request.model
    )
    if request.timeout_seconds != agent.timeout_seconds or len(matching_routes) != 1:
        raise AgentArtifactResponseError(
            "dynamic request timeout or model differs from the approved AgentSpec"
        )
    route = matching_routes[0]
    if result.telemetry.model != route.model:
        raise AgentArtifactResponseError(
            "execution telemetry model differs from the approved AgentSpec"
        )

    parsed = _parse_semantic_body(result.response_text, request.expected_kind)
    body = parsed.body
    if isinstance(body, ReviewReportResponse):
        try:
            body = _ground_review_tool_evidence(body, result)
        except (ValueError, ValidationError) as error:
            raise AgentArtifactResponseError(
                f"Agent semantic response is invalid: {_safe_validation_detail(error)}"
            ) from error
        parsed = ParsedAgentResponse(
            body=body,
            ignored_controller_fields=parsed.ignored_controller_fields,
        )
    criterion_ids = {criterion.id for criterion in task_brief.acceptance_criteria}
    try:
        if isinstance(body, WorkResultResponse):
            expected_tasks = set(assigned_task_ids)
            completed_tasks = set(body.completed_tasks)
            if completed_tasks != expected_tasks:
                raise ValueError(
                    "completed_tasks must exactly match the Agent's assigned task IDs"
                )
        elif isinstance(body, GroundedReviewReportResponse):
            if body.verdict is ReviewVerdict.FAIL and body.termination_reason is None:
                raise ValueError("failed reviews require a terminal review reason")
            expected_review_scope = set(reviewed_criterion_ids)
            unknown_scope = expected_review_scope - criterion_ids
            if unknown_scope:
                raise ValueError(
                    "assigned review scope references unknown acceptance criteria: "
                    f"{', '.join(sorted(unknown_scope))}"
                )
            assessed = [
                assessment.criterion_id for assessment in body.criterion_assessments
            ]
            if len(assessed) != len(set(assessed)):
                raise ValueError("criterion assessments must use unique criterion IDs")
            if set(assessed) != expected_review_scope:
                missing = sorted(expected_review_scope - set(assessed))
                unexpected = sorted(set(assessed) - expected_review_scope)
                detail = []
                if missing:
                    detail.append(f"missing: {', '.join(missing)}")
                if unexpected:
                    detail.append(f"outside scope: {', '.join(unexpected)}")
                raise ValueError(
                    "criterion assessments must exactly cover assigned review scope"
                    + (f" ({'; '.join(detail)})" if detail else "")
                )
            unknown = {
                criterion_id
                for finding in body.findings
                for criterion_id in finding.criterion_ids
                if criterion_id not in criterion_ids
            }
            if unknown:
                raise ValueError(
                    "review findings reference unknown acceptance criteria: "
                    f"{', '.join(sorted(unknown))}"
                )
            findings_without_scope = [
                finding.id for finding in body.findings if not finding.criterion_ids
            ]
            if findings_without_scope:
                raise ValueError(
                    "review findings must reference assigned criteria: "
                    f"{', '.join(findings_without_scope)}"
                )
            outside_scope = {
                criterion_id
                for finding in body.findings
                for criterion_id in finding.criterion_ids
                if criterion_id not in expected_review_scope
            }
            if outside_scope:
                raise ValueError(
                    "review findings reference criteria outside assigned scope: "
                    f"{', '.join(sorted(outside_scope))}"
                )
            blocked_assessments = {
                assessment.criterion_id
                for assessment in body.criterion_assessments
                if assessment.status is ReviewCriterionStatus.BLOCKED
            }
            blocking_findings = {
                criterion_id
                for finding in body.findings
                if finding.blocking
                for criterion_id in finding.criterion_ids
            }
            if blocked_assessments != blocking_findings:
                raise ValueError(
                    "blocked criterion assessments must exactly match blocking "
                    "finding criteria"
                )
            if body.verdict is ReviewVerdict.ACCEPT and blocked_assessments:
                raise ValueError(
                    "accepted reviews require every criterion assessment satisfied"
                )
            if (
                body.verdict in {ReviewVerdict.REVISE, ReviewVerdict.FAIL}
                and not blocked_assessments
            ):
                raise ValueError(
                    "non-accepted reviews require a blocked criterion assessment"
                )
    except ValueError as error:
        raise AgentArtifactResponseError(
            f"Agent semantic response is invalid: {_safe_validation_detail(error)}"
        ) from error
    return parsed
