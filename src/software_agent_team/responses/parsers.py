"""Semantic response parsing, context validation, and dynamic agent response binding."""

from __future__ import annotations

from collections.abc import Collection

from pydantic import ValidationError

from software_agent_team.artifacts import (
    REVIEW_ARTIFACT_KINDS,
    AgentRole,
    ArtifactKind,
    CommandEvidence,
    ImplementationPlan,
    ReviewCriterionStatus,
    ReviewReport,
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
from software_agent_team.response_corrections import (
    ResponseFailureClass,
    ResponseIssueAuthority,
    deterministically_remove_forbidden_fields,
    diagnostic_from_message,
    diagnostic_from_validation_error,
)
from software_agent_team.submissions import AgentSubmissionPurpose
from software_agent_team.teams import TeamPlan, capability_for_legacy_role

from .candidate_binding import (
    _context_failure_diagnostic,
    _safe_validation_detail,
)
from .evidence_matching import _ground_review_tool_evidence
from .models import (
    _CONTROLLER_FIELDS,
    _PROTECTED_CONTROLLER_FIELD_NAMES,
    RESPONSE_BODY_MODELS,
    AgentResponseBody,
    GroundedExperienceAssessmentResponse,
    GroundedReviewReportResponse,
    GroundedSecurityAssessmentResponse,
    ImplementationPlanResponse,
    ParsedAgentResponse,
    ReviewReportResponse,
    WorkResultResponse,
)
from .normalization import (
    _normalize_review_boundary_scope_payload,
    parse_json_object_response,
)
from .types import (
    AgentArtifactResponseError,
    ReviewToolEvidenceAttempt,
    _SpecializedReviewScopeError,
)


def _parse_semantic_body(
    value: str,
    expected_kind: ArtifactKind,
    *,
    task_brief: TaskBrief | None = None,
) -> ParsedAgentResponse:
    return _parse_semantic_payload(
        parse_json_object_response(value),
        expected_kind,
        task_brief=task_brief,
    )


def _parse_semantic_payload(
    payload: dict[str, object],
    expected_kind: ArtifactKind,
    *,
    task_brief: TaskBrief | None = None,
) -> ParsedAgentResponse:
    """Validate semantic values already delivered through a typed channel."""

    model = RESPONSE_BODY_MODELS.get(expected_kind)
    controller_fields = _CONTROLLER_FIELDS.get(expected_kind)
    if model is None or controller_fields is None:
        raise AgentArtifactResponseError(
            f"no response body contract exists for {expected_kind.value}"
        )
    ignored = tuple(sorted(controller_fields.intersection(payload)))
    semantic_payload: dict[str, object] = {
        key: item for key, item in payload.items() if key not in controller_fields
    }
    normalizations: list[str] = []
    if expected_kind in REVIEW_ARTIFACT_KINDS and task_brief is not None:
        semantic_payload, boundary_normalizations = (
            _normalize_review_boundary_scope_payload(
                semantic_payload,
                task_brief=task_brief,
            )
        )
        normalizations.extend(boundary_normalizations)
    while True:
        try:
            body = model.model_validate(semantic_payload)
            break
        except ValidationError as error:
            normalized, removed = deterministically_remove_forbidden_fields(
                semantic_payload,
                error,
                protected_field_names=_PROTECTED_CONTROLLER_FIELD_NAMES,
            )
            if removed:
                semantic_payload = normalized
                normalizations.extend(
                    f"removed schema-forbidden field {path}" for path in removed
                )
                continue
            diagnostic = diagnostic_from_validation_error(
                error,
                semantic_payload,
                protected_field_names=_PROTECTED_CONTROLLER_FIELD_NAMES,
            )
            raise AgentArtifactResponseError(
                f"Agent semantic response is invalid: {_safe_validation_detail(error)}",
                semantic_payload=semantic_payload,
                diagnostic=diagnostic,
                response_normalizations=tuple(normalizations),
            ) from error
        except ValueError as error:
            diagnostic = diagnostic_from_message(
                semantic_payload,
                failure_class=ResponseFailureClass.SEMANTIC_SCHEMA,
                authority=ResponseIssueAuthority.MODEL,
                code="semantic_validation",
                message=_safe_validation_detail(error),
                paths=("/",),
            )
            raise AgentArtifactResponseError(
                f"Agent semantic response is invalid: {_safe_validation_detail(error)}",
                semantic_payload=semantic_payload,
                diagnostic=diagnostic,
                response_normalizations=tuple(normalizations),
            ) from error
    return ParsedAgentResponse(
        body=body,
        ignored_controller_fields=ignored,
        semantic_payload=semantic_payload,
        response_normalizations=tuple(normalizations),
    )


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
    if result.telemetry.specialization is not request.specialization:
        raise AgentArtifactResponseError(
            "execution telemetry specialization does not match request"
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

    if result.response_text is None:
        raise AgentArtifactResponseError(
            "Agent execution omitted its semantic response"
        )
    parsed = _parse_semantic_body(
        result.response_text,
        request.expected_kind,
        task_brief=task_brief,
    )
    try:
        body = parsed.body
        if isinstance(body, ReviewReportResponse):
            body = _ground_review_tool_evidence(body, result)
            parsed = ParsedAgentResponse(
                body=body,
                ignored_controller_fields=parsed.ignored_controller_fields,
                semantic_payload=parsed.semantic_payload,
                response_normalizations=parsed.response_normalizations,
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
        diagnostic = _context_failure_diagnostic(parsed, error)
        raise AgentArtifactResponseError(
            f"Agent semantic response is invalid: {_safe_validation_detail(error)}",
            semantic_payload=parsed.semantic_payload,
            diagnostic=diagnostic,
            response_normalizations=parsed.response_normalizations,
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
    review_tool_evidence_attempts: tuple[ReviewToolEvidenceAttempt, ...] = (),
    review_command_evidence: tuple[CommandEvidence, ...] = (),
    controller_semantic_payload: dict[str, object] | None = None,
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
    if result.telemetry.specialization is not request.specialization:
        raise AgentArtifactResponseError(
            "execution telemetry specialization does not match request"
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
        or request.specialization is not agent.specialization
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

    if controller_semantic_payload is not None:
        if (
            request.submission_contract is None
            or request.submission_contract.purpose
            is not AgentSubmissionPurpose.SEMANTIC_CORRECTION
            or result.semantic_submission is None
            or result.semantic_submission.evidence.purpose
            is not AgentSubmissionPurpose.SEMANTIC_CORRECTION
        ):
            raise AgentArtifactResponseError(
                "controller semantic payload requires an accepted correction submission"
            )
        parsed = _parse_semantic_payload(
            controller_semantic_payload,
            request.expected_kind,
            task_brief=task_brief,
        )
    elif request.submission_contract is not None:
        if result.semantic_submission is None:
            raise AgentArtifactResponseError(
                "dynamic Agent execution omitted its typed semantic submission"
            )
        parsed = _parse_semantic_payload(
            result.semantic_submission.payload,
            request.expected_kind,
            task_brief=task_brief,
        )
    else:
        if result.response_text is None:
            raise AgentArtifactResponseError(
                "dynamic Agent execution omitted its semantic response"
            )
        parsed = _parse_semantic_body(
            result.response_text,
            request.expected_kind,
            task_brief=task_brief,
        )
    body = parsed.body
    if isinstance(body, ReviewReportResponse):
        try:
            body = _ground_review_tool_evidence(
                body,
                result,
                evidence_attempts=review_tool_evidence_attempts,
                command_evidence=review_command_evidence,
            )
        except (ValueError, ValidationError) as error:
            diagnostic = _context_failure_diagnostic(parsed, error)
            raise AgentArtifactResponseError(
                f"Agent semantic response is invalid: {_safe_validation_detail(error)}",
                semantic_payload=parsed.semantic_payload,
                diagnostic=diagnostic,
                response_normalizations=parsed.response_normalizations,
            ) from error
        parsed = ParsedAgentResponse(
            body=body,
            ignored_controller_fields=parsed.ignored_controller_fields,
            semantic_payload=parsed.semantic_payload,
            response_normalizations=parsed.response_normalizations,
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
            specialized_scope: set[str] | None = None
            specialized_path: str | None = None
            if isinstance(body, GroundedSecurityAssessmentResponse):
                specialized_scope = {
                    criterion_id
                    for surface in body.surfaces
                    for criterion_id in surface.criterion_ids
                }
                specialized_path = "/surfaces"
            elif isinstance(body, GroundedExperienceAssessmentResponse):
                specialized_scope = {
                    criterion_id
                    for workflow in body.workflows
                    for criterion_id in workflow.criterion_ids
                }
                specialized_path = "/workflows"
            if (
                specialized_scope is not None
                and specialized_scope != expected_review_scope
            ):
                assert specialized_path is not None
                raise _SpecializedReviewScopeError(
                    "specialized assessment entries must exactly cover assigned "
                    "review scope",
                    path=specialized_path,
                )
            criteria_by_id = {
                criterion.id: criterion for criterion in task_brief.acceptance_criteria
            }
            for assessment in body.criterion_assessments:
                required_boundaries = set(
                    criteria_by_id[assessment.criterion_id].review_boundaries
                )
                checked_boundaries = {
                    check.boundary for check in assessment.boundary_checks
                }
                unexpected_boundaries = checked_boundaries - required_boundaries
                if unexpected_boundaries:
                    raise ValueError(
                        f"criterion {assessment.criterion_id} checked unapproved "
                        "Review boundaries: "
                        + ", ".join(
                            sorted(item.value for item in unexpected_boundaries)
                        )
                    )
                if (
                    assessment.status is ReviewCriterionStatus.SATISFIED
                    and checked_boundaries != required_boundaries
                ):
                    missing_boundaries = required_boundaries - checked_boundaries
                    raise ValueError(
                        f"criterion {assessment.criterion_id} satisfied assessment "
                        "must check every approved Review boundary: "
                        + ", ".join(sorted(item.value for item in missing_boundaries))
                    )
                if (
                    assessment.status is ReviewCriterionStatus.BLOCKED
                    and required_boundaries
                    and not checked_boundaries
                ):
                    raise ValueError(
                        f"criterion {assessment.criterion_id} blocked assessment "
                        "must ground at least one approved Review boundary"
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
        diagnostic = _context_failure_diagnostic(parsed, error)
        raise AgentArtifactResponseError(
            f"Agent semantic response is invalid: {_safe_validation_detail(error)}",
            semantic_payload=parsed.semantic_payload,
            diagnostic=diagnostic,
            response_normalizations=parsed.response_normalizations,
        ) from error
    return parsed
