"""Strict conversion of untrusted Agent text into semantic response bodies."""

from __future__ import annotations

import json
import re
from collections.abc import Collection
from dataclasses import dataclass
from difflib import SequenceMatcher

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from software_agent_team.artifacts import (
    REVIEW_ARTIFACT_KINDS,
    AgentRole,
    AgentToolCallEvidence,
    AgentToolCallOutcome,
    AgentToolEvidenceStatus,
    ArtifactKind,
    CommandEvidence,
    ExperienceWorkflowAssessment,
    ImplementationPlan,
    PlanTask,
    ReviewBoundaryCheck,
    ReviewBoundaryKind,
    ReviewCriterionAssessment,
    ReviewCriterionStatus,
    ReviewFinding,
    ReviewReport,
    ReviewSeverity,
    ReviewTerminationReason,
    ReviewToolEvidenceReference,
    ReviewVerdict,
    SecuritySurfaceAssessment,
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
    MAX_CORRECTION_FIELDS,
    ResponseFailureClass,
    ResponseIssueAuthority,
    ResponseIssueSubject,
    ResponseIssueSubjectKind,
    ResponseValidationDiagnostic,
    ResponseValidationIssue,
    SemanticCorrectionCandidate,
    SemanticCorrectionCandidateSlot,
    SemanticCorrectionPlan,
    attach_semantic_correction_candidates,
    deterministically_remove_forbidden_fields,
    diagnostic_from_invariant,
    diagnostic_from_message,
    diagnostic_from_transport,
    diagnostic_from_validation_error,
    semantic_payload_sha256,
)
from software_agent_team.submissions import AgentSubmissionPurpose
from software_agent_team.teams import TeamPlan, capability_for_legacy_role


class AgentArtifactResponseError(ValueError):
    """Raised when an Agent response cannot become attributable run evidence."""

    def __init__(
        self,
        detail: str,
        *,
        semantic_payload: dict[str, object] | None = None,
        diagnostic: ResponseValidationDiagnostic | None = None,
        response_normalizations: tuple[str, ...] = (),
    ) -> None:
        super().__init__(detail)
        self.semantic_payload = semantic_payload
        self.diagnostic = diagnostic
        self.response_normalizations = response_normalizations


class _ReviewEvidenceGroundingError(ValueError):
    """Locate one model-owned Review selector rejected by evidence binding."""

    def __init__(
        self,
        detail: str,
        *,
        path: str,
        invariant_id: str,
        criterion_id: str,
    ) -> None:
        super().__init__(detail)
        self.path = path
        self.invariant_id = invariant_id
        self.criterion_id = criterion_id


class _UnsafeSatisfiedEvidenceError(_ReviewEvidenceGroundingError):
    """Identify a positive Review claim that matched only unsafe evidence."""


class _ReviewEvidenceGroundingErrors(ValueError):
    """Preserve every independently invalid Review evidence selector."""

    def __init__(self, errors: tuple[_ReviewEvidenceGroundingError, ...]) -> None:
        if not errors:
            raise ValueError("Review evidence error collection must not be empty")
        detail = str(errors[0])
        if len(errors) > 1:
            detail += f"; {len(errors) - 1} additional evidence selector(s) failed"
        super().__init__(detail)
        self.errors = errors


@dataclass(frozen=True)
class ReviewToolEvidenceAttempt:
    """One integrity-checked attempt in a bounded Reviewer response chain."""

    execution_attempt: int
    tool_calls: tuple[AgentToolCallEvidence, ...]

    def __post_init__(self) -> None:
        if self.execution_attempt < 1:
            raise ValueError("review evidence attempt must be positive")


@dataclass(frozen=True)
class _ReviewEvidenceMatches:
    """One selector's matches under the canonical Review evidence policy."""

    tool_matches: tuple[tuple[int, AgentToolCallEvidence], ...]
    ineligible_probe_matches: tuple[tuple[int, AgentToolCallEvidence], ...]
    command_matches: tuple[CommandEvidence, ...]


def _clean_unique_text(values: tuple[str, ...]) -> tuple[str, ...]:
    cleaned = tuple(value.strip() for value in values)
    if any(not value for value in cleaned):
        raise ValueError("response text values must not be blank")
    if len(cleaned) != len(set(cleaned)):
        raise ValueError("response text values must be unique")
    return cleaned


def _json_whitespace_projection(value: str) -> str | None:
    """Remove only RFC JSON whitespace outside strings from a keyed fragment."""

    projected: list[str] = []
    in_string = False
    escaped = False
    just_closed_string = False
    saw_key_value = False
    for character in value:
        if in_string:
            projected.append(character)
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
                just_closed_string = True
            continue
        if character == '"':
            in_string = True
            just_closed_string = False
            projected.append(character)
            continue
        if character in " \t\r\n":
            continue
        if character == ":" and just_closed_string:
            saw_key_value = True
        projected.append(character)
        just_closed_string = False
    if in_string or escaped or not saw_key_value:
        return None
    return "".join(projected)


def _observable_matches_output(observable: str, output: str) -> bool:
    """Match exact text or one JSON-keyed whitespace-only presentation variant."""

    if observable in output:
        return True
    projected_observable = _json_whitespace_projection(observable)
    if projected_observable is None:
        return False
    projected_output = _json_whitespace_projection(output)
    return projected_output is not None and projected_observable in projected_output


_PROBE_RESULT_PREFIX = "SAT_PROBE_RESULT_V1 "
_PROBE_STDOUT_BEGIN = "SAT_PROBE_STDOUT_BEGIN"
_PROBE_STDOUT_END = "SAT_PROBE_STDOUT_END"
_PROBE_STDERR_BEGIN = "SAT_PROBE_STDERR_BEGIN"
_PROBE_STDERR_END = "SAT_PROBE_STDERR_END"
_PROBE_EXECUTABLES = {"sat-probe-run", "/usr/local/bin/sat-probe-run"}
_LEGACY_EXIT_MARKER = re.compile(r"^EXIT=(-?[0-9]+)$")


def _is_direct_probe(call: AgentToolCallEvidence) -> bool:
    """Return whether one captured call directly invoked the immutable runner."""

    return call.executable in _PROBE_EXECUTABLES


def _positive_probe_match_surface(call: AgentToolCallEvidence) -> str:
    """Return only protocol-authorized positive evidence from one direct probe.

    Direct probes admit child stdout and the terminal runner result, never child
    stderr. Missing, ambiguous, or partial framing deliberately produces no
    positive surface instead of guessing where a traceback ends. Historical
    unframed records remain immutable audit evidence but are not reinterpreted
    as proof under the current protocol.
    """

    if not _is_direct_probe(call):
        return call.output_excerpt
    lines = call.output_excerpt.splitlines()
    frame_markers = (
        _PROBE_STDOUT_BEGIN,
        _PROBE_STDOUT_END,
        _PROBE_STDERR_BEGIN,
        _PROBE_STDERR_END,
    )
    if not any(line in frame_markers for line in lines):
        return ""
    positions = {
        marker: tuple(index for index, line in enumerate(lines) if line == marker)
        for marker in frame_markers
    }
    if any(len(indexes) != 1 for indexes in positions.values()):
        return ""
    result_indexes = tuple(
        index
        for index, line in enumerate(lines)
        if line.startswith(_PROBE_RESULT_PREFIX)
    )
    if not result_indexes:
        return ""
    stdout_begin = positions[_PROBE_STDOUT_BEGIN][0]
    stdout_end = positions[_PROBE_STDOUT_END][0]
    stderr_begin = positions[_PROBE_STDERR_BEGIN][0]
    stderr_end = positions[_PROBE_STDERR_END][0]
    result_index = result_indexes[-1]
    if not (stdout_begin < stdout_end < stderr_begin < stderr_end < result_index):
        return ""
    return "\n".join((*lines[stdout_begin + 1 : stdout_end], lines[result_index]))


def _probe_result_failed(call: AgentToolCallEvidence) -> bool | None:
    """Resolve the immutable probe runner's terminal child result marker.

    OpenClaw may append its own command diagnostic after the program output, so
    the runner marker need not be the final physical line. The runner itself
    writes its authoritative marker after child stdout and stderr; the last
    well-formed marker is therefore the relevant result for an attributable
    direct runner invocation.
    """

    if not _is_direct_probe(call):
        return None
    marker_lines = tuple(
        line
        for line in call.output_excerpt.splitlines()
        if line.startswith(_PROBE_RESULT_PREFIX)
    )
    if not marker_lines:
        return True
    try:
        payload = json.loads(marker_lines[-1][len(_PROBE_RESULT_PREFIX) :])
    except (json.JSONDecodeError, TypeError, RecursionError):
        return True
    if not isinstance(payload, dict) or set(payload) != {"exit_code", "timed_out"}:
        return True
    exit_code = payload["exit_code"]
    timed_out = payload["timed_out"]
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        return True
    if not isinstance(timed_out, bool):
        return True
    return timed_out or exit_code != 0


def _tool_result_ineligible_for_satisfied_claim(
    call: AgentToolCallEvidence,
) -> bool:
    """Reject nonterminal or failed results as positive Review evidence."""

    if call.outcome is not AgentToolCallOutcome.SUCCEEDED:
        return True
    probe_failed = _probe_result_failed(call)
    if probe_failed is not None:
        return probe_failed
    nonempty_lines = tuple(
        line.strip() for line in call.output_excerpt.splitlines() if line.strip()
    )
    if not nonempty_lines:
        return False
    legacy = _LEGACY_EXIT_MARKER.fullmatch(nonempty_lines[-1])
    return legacy is not None and int(legacy.group(1)) != 0


def _matched_command_failed(command: CommandEvidence) -> bool:
    """Return whether deterministic command evidence did not complete cleanly."""

    return command.timed_out or command.exit_code != 0


def _match_review_evidence(
    observable: str,
    *,
    status: ReviewCriterionStatus,
    evidence_attempts: tuple[ReviewToolEvidenceAttempt, ...],
    command_evidence: tuple[CommandEvidence, ...],
) -> _ReviewEvidenceMatches:
    """Apply the one authoritative match policy used by catalog and grounding."""

    satisfied = status is ReviewCriterionStatus.SATISFIED
    tool_matches = tuple(
        (attempt.execution_attempt, call)
        for attempt in evidence_attempts
        for call in attempt.tool_calls
        if _observable_matches_output(
            observable,
            _positive_probe_match_surface(call) if satisfied else call.output_excerpt,
        )
    )
    ineligible_probe_matches = tuple(
        (attempt.execution_attempt, call)
        for attempt in evidence_attempts
        for call in attempt.tool_calls
        if satisfied
        and _is_direct_probe(call)
        and _observable_matches_output(observable, call.output_excerpt)
        and not _observable_matches_output(
            observable,
            _positive_probe_match_surface(call),
        )
    )
    command_matches = tuple(
        command
        for command in command_evidence
        if _observable_matches_output(observable, command.stdout_tail)
        or _observable_matches_output(observable, command.stderr_tail)
    )
    if satisfied and any(
        _is_direct_probe(call) and not _tool_result_ineligible_for_satisfied_claim(call)
        for _, call in tool_matches
    ):
        tool_matches = tuple(
            match
            for match in tool_matches
            if not (
                _is_direct_probe(match[1])
                and _tool_result_ineligible_for_satisfied_claim(match[1])
            )
        )
    return _ReviewEvidenceMatches(
        tool_matches=tool_matches,
        ineligible_probe_matches=ineligible_probe_matches,
        command_matches=command_matches,
    )


def _is_safe_satisfied_evidence_candidate(
    observable: str,
    *,
    evidence_attempts: tuple[ReviewToolEvidenceAttempt, ...],
    command_evidence: tuple[CommandEvidence, ...],
) -> bool:
    """Return whether final grounding can accept one catalog candidate."""

    matches = _match_review_evidence(
        observable,
        status=ReviewCriterionStatus.SATISFIED,
        evidence_attempts=evidence_attempts,
        command_evidence=command_evidence,
    )
    if not matches.tool_matches and not matches.command_matches:
        return False
    return not any(
        _tool_result_ineligible_for_satisfied_claim(call)
        for _, call in matches.tool_matches
    ) and not any(
        _matched_command_failed(command) for command in matches.command_matches
    )


def _bind_unambiguous_blocking_finding_scope(
    findings: tuple[ReviewFinding, ...],
    assessments: tuple[ReviewCriterionAssessment, ...],
) -> tuple[ReviewFinding, ...]:
    """Bind one unscoped blocker to the blocked criteria it uniquely explains.

    The raw model response remains in execution evidence. This normalization only
    removes redundant identifier repetition when there is exactly one possible
    binding; ambiguous finding-to-criterion mappings remain invalid.
    """

    blocked_criteria = {
        assessment.criterion_id
        for assessment in assessments
        if assessment.status is ReviewCriterionStatus.BLOCKED
    }
    scoped_blocking_criteria = {
        criterion_id
        for finding in findings
        if finding.blocking and finding.criterion_ids
        for criterion_id in finding.criterion_ids
    }
    unscoped_blocking_indexes = tuple(
        index
        for index, finding in enumerate(findings)
        if finding.blocking and not finding.criterion_ids
    )
    uncovered_criteria = blocked_criteria - scoped_blocking_criteria
    if len(unscoped_blocking_indexes) != 1 or not uncovered_criteria:
        return findings

    index = unscoped_blocking_indexes[0]
    normalized = list(findings)
    normalized[index] = findings[index].model_copy(
        update={"criterion_ids": tuple(sorted(uncovered_criteria))}
    )
    return tuple(normalized)


def _ground_review_tool_evidence(
    body: ReviewReportResponse,
    result: AgentExecutionResult,
    *,
    evidence_attempts: tuple[ReviewToolEvidenceAttempt, ...] = (),
    command_evidence: tuple[CommandEvidence, ...] = (),
) -> GroundedReviewReportResponse:
    """Bind fragments to eligible Reviewer results and deterministic commands."""

    if not body.criterion_assessments:
        return _build_grounded_review_response(
            body,
            criterion_assessments=(),
            findings=body.findings,
        )
    telemetry = result.telemetry
    if telemetry.tool_evidence_status is AgentToolEvidenceStatus.INVALID:
        raise ValueError(
            "review tool evidence is invalid: "
            f"{telemetry.tool_evidence_error or 'unknown session error'}"
        )
    if telemetry.tool_evidence_status is not AgentToolEvidenceStatus.CAPTURED:
        raise ValueError("review tool evidence was not captured")
    attempts = evidence_attempts or (
        ReviewToolEvidenceAttempt(
            execution_attempt=1,
            tool_calls=telemetry.tool_calls,
        ),
    )
    attempt_numbers = [item.execution_attempt for item in attempts]
    if attempt_numbers != sorted(set(attempt_numbers)):
        raise ValueError("review evidence attempts must be unique and ordered")
    if attempts[-1].tool_calls != telemetry.tool_calls:
        raise ValueError("review evidence chain does not end at the current attempt")

    def evaluate_claim(
        claim: ReviewToolEvidenceClaim,
        *,
        label: str,
        status: ReviewCriterionStatus,
        claim_path: str,
        criterion_id: str,
    ) -> tuple[
        tuple[tuple[int, AgentToolCallEvidence], ...],
        tuple[CommandEvidence, ...],
    ]:
        """Resolve one selector or raise its exact typed grounding failure."""

        matches = _match_review_evidence(
            claim.observable,
            status=status,
            evidence_attempts=attempts,
            command_evidence=command_evidence,
        )
        tool_matches = matches.tool_matches
        ineligible_probe_matches = matches.ineligible_probe_matches
        command_matches = matches.command_matches
        if not tool_matches and not command_matches:
            if ineligible_probe_matches:
                raise _UnsafeSatisfiedEvidenceError(
                    f"{label} satisfied evidence fragment is absent from "
                    "protocol-eligible child stdout in a complete direct probe; "
                    "rerun it and emit the fragment after the relevant assertion "
                    "passes",
                    path=claim_path,
                    invariant_id="review_evidence_positive_surface_unavailable",
                    criterion_id=criterion_id,
                )
            raise _ReviewEvidenceGroundingError(
                f"{label} evidence fragment does not match any eligible "
                "review-chain tool result or deterministic command output",
                path=claim_path,
                invariant_id="review_evidence_fragment_unmatched",
                criterion_id=criterion_id,
            )
        if status is ReviewCriterionStatus.SATISFIED:
            ineligible_tool_matches = tuple(
                call
                for _, call in tool_matches
                if _tool_result_ineligible_for_satisfied_claim(call)
            )
            if any(
                call.outcome is AgentToolCallOutcome.DEFERRED
                for call in ineligible_tool_matches
            ):
                raise _UnsafeSatisfiedEvidenceError(
                    f"{label} satisfied evidence selects a nonterminal "
                    "deferred tool result; wait for and cite a successful "
                    "terminal result",
                    path=claim_path,
                    invariant_id="review_evidence_deferred_tool",
                    criterion_id=criterion_id,
                )
            if ineligible_tool_matches:
                raise _UnsafeSatisfiedEvidenceError(
                    f"{label} satisfied evidence selects an overall failed "
                    "tool result; rerun the direct probe successfully and cite "
                    "a fragment emitted by child stdout",
                    path=claim_path,
                    invariant_id="review_evidence_failed_tool",
                    criterion_id=criterion_id,
                )
            if failed_commands := tuple(
                command.id
                for command in command_matches
                if _matched_command_failed(command)
            ):
                raise _UnsafeSatisfiedEvidenceError(
                    f"{label} satisfied evidence selects a failed or timed-out "
                    "deterministic command: " + ", ".join(failed_commands),
                    path=claim_path,
                    invariant_id="review_evidence_failed_command",
                    criterion_id=criterion_id,
                )
        return tool_matches, command_matches

    def resolve_claims(
        claims: tuple[ReviewToolEvidenceClaim, ...],
        *,
        label: str,
        status: ReviewCriterionStatus,
        path_prefix: str,
        criterion_id: str,
    ) -> tuple[tuple[ReviewToolEvidenceReference, ...], tuple[str, ...]]:
        """Bind one semantic claim collection to protocol-eligible evidence."""

        observable_by_tool_call: dict[tuple[int, str], str] = {}
        matching_command_ids: set[str] = set()
        for claim_index, claim in enumerate(claims):
            tool_matches, command_matches = evaluate_claim(
                claim,
                label=label,
                status=status,
                claim_path=f"{path_prefix}/{claim_index}/observable",
                criterion_id=criterion_id,
            )
            for execution_attempt, match in tool_matches:
                observable_by_tool_call.setdefault(
                    (execution_attempt, match.id),
                    claim.observable,
                )
            matching_command_ids.update(command.id for command in command_matches)
        references = tuple(
            ReviewToolEvidenceReference(
                execution_attempt=attempt.execution_attempt,
                tool_call_id=call.id,
                observable=observable_by_tool_call[
                    (attempt.execution_attempt, call.id)
                ],
            )
            for attempt in attempts
            for call in attempt.tool_calls
            if (attempt.execution_attempt, call.id) in observable_by_tool_call
        )
        command_ids = tuple(
            command.id
            for command in command_evidence
            if command.id in matching_command_ids
        )
        return references, command_ids

    def ground_assessment(
        assessment: ReviewCriterionAssessmentResponse,
        *,
        assessment_index: int,
        status: ReviewCriterionStatus,
        evidence: str | None = None,
    ) -> ReviewCriterionAssessment:
        """Ground one assessment under an explicit controller-owned status."""

        references, command_ids = resolve_claims(
            assessment.tool_evidence,
            label=f"criterion {assessment.criterion_id}",
            status=status,
            path_prefix=(f"/criterion_assessments/{assessment_index}/tool_evidence"),
            criterion_id=assessment.criterion_id,
        )
        boundary_checks = tuple(
            ReviewBoundaryCheck(
                boundary=boundary.boundary,
                adversarial_check=boundary.adversarial_check,
                command_evidence_ids=boundary_command_ids,
                tool_evidence=boundary_references,
            )
            for boundary_index, boundary in enumerate(assessment.boundary_checks)
            for boundary_references, boundary_command_ids in (
                resolve_claims(
                    boundary.tool_evidence,
                    label=(
                        f"criterion {assessment.criterion_id} boundary "
                        f"{boundary.boundary.value}"
                    ),
                    status=status,
                    path_prefix=(
                        f"/criterion_assessments/{assessment_index}/"
                        f"boundary_checks/{boundary_index}/tool_evidence"
                    ),
                    criterion_id=assessment.criterion_id,
                ),
            )
        )
        return ReviewCriterionAssessment(
            criterion_id=assessment.criterion_id,
            status=status,
            adversarial_check=assessment.adversarial_check,
            evidence=evidence or assessment.evidence,
            command_evidence_ids=command_ids,
            tool_evidence=references,
            boundary_checks=boundary_checks,
        )

    recoverable_revision = (
        body.verdict is ReviewVerdict.REVISE
        and any(
            assessment.status is ReviewCriterionStatus.BLOCKED
            for assessment in body.criterion_assessments
        )
        and any(finding.blocking for finding in body.findings)
    )
    grounding_errors: list[_ReviewEvidenceGroundingError] = []
    for assessment_index, assessment in enumerate(body.criterion_assessments):
        claim_groups = [
            (
                assessment.tool_evidence,
                f"criterion {assessment.criterion_id}",
                f"/criterion_assessments/{assessment_index}/tool_evidence",
            )
        ]
        claim_groups.extend(
            (
                boundary.tool_evidence,
                (
                    f"criterion {assessment.criterion_id} boundary "
                    f"{boundary.boundary.value}"
                ),
                (
                    f"/criterion_assessments/{assessment_index}/"
                    f"boundary_checks/{boundary_index}/tool_evidence"
                ),
            )
            for boundary_index, boundary in enumerate(assessment.boundary_checks)
        )
        for claims, label, path_prefix in claim_groups:
            for claim_index, claim in enumerate(claims):
                try:
                    evaluate_claim(
                        claim,
                        label=label,
                        status=assessment.status,
                        claim_path=f"{path_prefix}/{claim_index}/observable",
                        criterion_id=assessment.criterion_id,
                    )
                except _UnsafeSatisfiedEvidenceError as error:
                    if not recoverable_revision:
                        grounding_errors.append(error)
                except _ReviewEvidenceGroundingError as error:
                    grounding_errors.append(error)
    if grounding_errors:
        raise _ReviewEvidenceGroundingErrors(tuple(grounding_errors))

    grounded_assessments: list[ReviewCriterionAssessment] = []
    downgraded: dict[str, str] = {}
    for assessment_index, assessment in enumerate(body.criterion_assessments):
        try:
            grounded_assessments.append(
                ground_assessment(
                    assessment,
                    assessment_index=assessment_index,
                    status=assessment.status,
                )
            )
        except _UnsafeSatisfiedEvidenceError as error:
            if (
                assessment.status is not ReviewCriterionStatus.SATISFIED
                or not recoverable_revision
            ):
                raise
            detail = str(error)
            downgraded[assessment.criterion_id] = detail
            grounded_assessments.append(
                ground_assessment(
                    assessment,
                    assessment_index=assessment_index,
                    status=ReviewCriterionStatus.BLOCKED,
                    evidence=(
                        "The controller downgraded this positive assessment because "
                        f"its selected evidence was not safe proof: {detail}"
                    ),
                )
            )
    grounded = tuple(grounded_assessments)
    findings = list(body.findings)
    finding_ids = {finding.id for finding in findings}
    for criterion_id, detail in downgraded.items():
        if any(
            finding.blocking and criterion_id in finding.criterion_ids
            for finding in findings
        ):
            continue
        stem = re.sub(r"[^A-Z0-9_]", "_", criterion_id.upper())
        candidate = f"FINDING_UNVERIFIED_REVIEW_EVIDENCE_{stem}"
        suffix = 2
        while candidate in finding_ids:
            candidate = f"FINDING_UNVERIFIED_REVIEW_EVIDENCE_{stem}_{suffix}"
            suffix += 1
        finding_ids.add(candidate)
        findings.append(
            ReviewFinding(
                id=candidate,
                severity=ReviewSeverity.MEDIUM,
                blocking=True,
                category="review evidence",
                description=(
                    f"The controller could not verify the positive Review assessment "
                    f"for {criterion_id}: {detail}"
                ),
                recommendation=(
                    "Run a fresh isolated protocol-eligible check and cite evidence "
                    "from a successful result before accepting this criterion."
                ),
                criterion_ids=(criterion_id,),
            )
        )
    return _build_grounded_review_response(
        body,
        criterion_assessments=grounded,
        findings=_bind_unambiguous_blocking_finding_scope(tuple(findings), grounded),
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
    """Model-visible exact fragment used to select current tool results."""

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


_MAX_REVIEW_EVIDENCE_CANDIDATES_PER_SLOT = 64
_MAX_REVIEW_EVIDENCE_FRAGMENT_CHARACTERS = 256


def _review_evidence_candidate_fragments(value: str) -> tuple[str, ...]:
    """Return bounded exact substrings suitable for model-visible selection."""

    lines = value.splitlines()
    candidates: list[str] = []

    def add(candidate: str) -> None:
        cleaned = candidate.strip()
        if (
            not cleaned
            or "\x00" in cleaned
            or len(cleaned) > _MAX_REVIEW_EVIDENCE_FRAGMENT_CHARACTERS
            or cleaned in candidates
        ):
            return
        candidates.append(cleaned)

    add(value)
    for index, line in enumerate(lines):
        add(line)
        for width in (2, 3):
            add("\n".join(lines[index : index + width]))
        if len(line) > _MAX_REVIEW_EVIDENCE_FRAGMENT_CHARACTERS:
            step = _MAX_REVIEW_EVIDENCE_FRAGMENT_CHARACTERS - 64
            for start in range(0, len(line), step):
                add(line[start : start + _MAX_REVIEW_EVIDENCE_FRAGMENT_CHARACTERS])
    return tuple(candidates)


def _semantic_value_at_pointer(payload: dict[str, object], pointer: str) -> object:
    """Resolve one already-validated correction pointer without guessing."""

    current: object = payload
    for encoded in pointer.removeprefix("/").split("/"):
        part = encoded.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif (
            isinstance(current, list) and part.isdecimal() and int(part) < len(current)
        ):
            current = current[int(part)]
        else:
            raise ValueError("review evidence correction pointer is unreachable")
    return current


def _review_assessment_status_for_path(
    payload: dict[str, object],
    pointer: str,
) -> ReviewCriterionStatus:
    """Resolve the owning assessment status for one exact observable leaf."""

    parts = pointer.removeprefix("/").split("/")
    if (
        len(parts) < 5
        or parts[0] != "criterion_assessments"
        or not parts[1].isdecimal()
        or parts[-1] != "observable"
    ):
        raise ValueError("review evidence candidate path is not an observable leaf")
    assessments = payload.get("criterion_assessments")
    if not isinstance(assessments, list) or int(parts[1]) >= len(assessments):
        raise ValueError("review evidence assessment is unavailable")
    assessment = assessments[int(parts[1])]
    if not isinstance(assessment, dict):
        raise ValueError("review evidence assessment is invalid")
    return ReviewCriterionStatus(assessment.get("status"))


def _candidate_similarity(reference: str, candidate: str) -> tuple[float, float]:
    """Rank bounded exact choices by lexical relation to the rejected selector."""

    reference_tokens = set(re.findall(r"[a-z0-9_]+", reference.casefold()))
    candidate_tokens = set(re.findall(r"[a-z0-9_]+", candidate.casefold()))
    overlap = (
        0.0
        if not reference_tokens
        else len(reference_tokens & candidate_tokens) / len(reference_tokens)
    )
    return (
        overlap,
        SequenceMatcher(None, reference.casefold(), candidate.casefold()).ratio(),
    )


def bind_review_evidence_correction_candidates(
    plan: SemanticCorrectionPlan,
    *,
    evidence_attempts: tuple[ReviewToolEvidenceAttempt, ...],
    command_evidence: tuple[CommandEvidence, ...] = (),
) -> SemanticCorrectionPlan | None:
    """Replace free-form Review evidence bytes with controller-issued handles.

    The Reviewer still decides which eligible result supports a semantic claim.
    The controller owns the exact bytes and attributable result identity. If no
    eligible candidate exists for an observable slot, targeted string repair is
    unreachable and must not be offered as a random retry.
    """

    observable_paths = tuple(
        path for path in plan.evidence.target_paths if path.endswith("/observable")
    )
    if not observable_paths:
        return plan
    slots: list[SemanticCorrectionCandidateSlot] = []
    for path in observable_paths:
        try:
            rejected_value = _semantic_value_at_pointer(plan.base_payload, path)
            status = _review_assessment_status_for_path(plan.base_payload, path)
        except (TypeError, ValueError):
            return None
        if not isinstance(rejected_value, str):
            return None
        sources: list[tuple[str, str]] = []
        for attempt in evidence_attempts:
            for call in attempt.tool_calls:
                if call.tool_name == "sat_submit_artifact":
                    continue
                if (
                    status is ReviewCriterionStatus.SATISFIED
                    and _tool_result_ineligible_for_satisfied_claim(call)
                ):
                    continue
                surface = (
                    _positive_probe_match_surface(call)
                    if status is ReviewCriterionStatus.SATISFIED
                    else call.output_excerpt
                )
                executable = "" if call.executable is None else f"/{call.executable}"
                sources.append(
                    (
                        f"attempt {attempt.execution_attempt} {call.id} "
                        f"{call.tool_name}{executable} result",
                        surface,
                    )
                )
        for command in command_evidence:
            if status is ReviewCriterionStatus.SATISFIED and _matched_command_failed(
                command
            ):
                continue
            sources.extend(
                (
                    (f"deterministic command {command.id} stdout", command.stdout_tail),
                    (f"deterministic command {command.id} stderr", command.stderr_tail),
                )
            )

        ranked: list[tuple[tuple[float, float], int, str, str]] = []
        seen_values: set[str] = set()
        source_order = 0
        for source, surface in sources:
            for fragment in _review_evidence_candidate_fragments(surface):
                if fragment in seen_values:
                    continue
                seen_values.add(fragment)
                ranked.append(
                    (
                        _candidate_similarity(rejected_value, fragment),
                        source_order,
                        source,
                        fragment,
                    )
                )
                source_order += 1
        ranked.sort(key=lambda item: (-item[0][0], -item[0][1], item[1]))
        selected: list[tuple[tuple[float, float], int, str, str]] = []
        for candidate in ranked:
            fragment = candidate[3]
            if status is ReviewCriterionStatus.SATISFIED and not (
                _is_safe_satisfied_evidence_candidate(
                    fragment,
                    evidence_attempts=evidence_attempts,
                    command_evidence=command_evidence,
                )
            ):
                continue
            selected.append(candidate)
            if len(selected) == _MAX_REVIEW_EVIDENCE_CANDIDATES_PER_SLOT:
                break
        candidates: list[SemanticCorrectionCandidate] = []
        for index, (_, _, source, fragment) in enumerate(selected, start=1):
            candidates.append(
                SemanticCorrectionCandidate(
                    handle=f"candidate_{index}",
                    replacement_value=fragment,
                    source=source,
                )
            )
        if not candidates:
            return None
        slots.append(
            SemanticCorrectionCandidateSlot(
                target_path=path,
                candidates=tuple(candidates),
            )
        )
    return attach_semantic_correction_candidates(plan, tuple(slots))


class ReviewBoundaryCheckResponse(BaseModel):
    """Model-visible evidence selector for one required entry boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    boundary: ReviewBoundaryKind
    adversarial_check: str = Field(min_length=1, max_length=1000)
    tool_evidence: tuple[ReviewToolEvidenceClaim, ...] = Field(min_length=1)

    @field_validator("adversarial_check")
    @classmethod
    def require_clean_adversarial_check(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("boundary adversarial check must not be blank")
        return cleaned


class ReviewCriterionAssessmentResponse(BaseModel):
    """Reviewer assessment with model-visible result-fragment selectors."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    criterion_id: str = Field(min_length=1, pattern=r"^[A-Z][A-Z0-9_-]*$")
    status: ReviewCriterionStatus
    adversarial_check: str = Field(min_length=1, max_length=2000)
    evidence: str = Field(min_length=1, max_length=2000)
    tool_evidence: tuple[ReviewToolEvidenceClaim, ...] = Field(min_length=1)
    boundary_checks: tuple[ReviewBoundaryCheckResponse, ...] = ()

    @field_validator("adversarial_check", "evidence")
    @classmethod
    def require_clean_assessment_text(cls, value: str) -> str:
        """Reject empty presentation-only assessment text."""

        cleaned = value.strip()
        if not cleaned:
            raise ValueError("criterion assessment text must not be blank")
        return cleaned

    @field_validator("boundary_checks")
    @classmethod
    def require_unique_boundary_checks_and_observables(
        cls,
        values: tuple[ReviewBoundaryCheckResponse, ...],
    ) -> tuple[ReviewBoundaryCheckResponse, ...]:
        boundaries = [value.boundary for value in values]
        if len(boundaries) != len(set(boundaries)):
            raise ValueError("criterion boundary checks must be unique")
        observables = [
            claim.observable for value in values for claim in value.tool_evidence
        ]
        if len(observables) != len(set(observables)):
            raise ValueError(
                "criterion boundary checks require distinct evidence fragments"
            )
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


class SecurityAssessmentResponse(ReviewReportResponse):
    """Security-specialist semantics beyond the shared review contract."""

    surfaces: tuple[SecuritySurfaceAssessment, ...] = Field(min_length=1)
    residual_risks: tuple[str, ...] = ()

    @field_validator("residual_risks")
    @classmethod
    def require_clean_residual_risks(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        return _clean_unique_text(values)


class GroundedSecurityAssessmentResponse(GroundedReviewReportResponse):
    """Security semantics after controller evidence binding."""

    surfaces: tuple[SecuritySurfaceAssessment, ...] = Field(min_length=1)
    residual_risks: tuple[str, ...] = ()


class ExperienceAssessmentResponse(ReviewReportResponse):
    """User-experience specialist semantics beyond shared review fields."""

    workflows: tuple[ExperienceWorkflowAssessment, ...] = Field(min_length=1)
    usability_risks: tuple[str, ...] = ()

    @field_validator("usability_risks")
    @classmethod
    def require_clean_usability_risks(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        return _clean_unique_text(values)


class GroundedExperienceAssessmentResponse(GroundedReviewReportResponse):
    """User-experience semantics after controller evidence binding."""

    workflows: tuple[ExperienceWorkflowAssessment, ...] = Field(min_length=1)
    usability_risks: tuple[str, ...] = ()


def _build_grounded_review_response(
    body: ReviewReportResponse,
    *,
    criterion_assessments: tuple[ReviewCriterionAssessment, ...],
    findings: tuple[ReviewFinding, ...],
) -> GroundedReviewReportResponse:
    """Preserve the exact specialization while binding shared review evidence."""

    common = {
        "verdict": body.verdict,
        "termination_reason": body.termination_reason,
        "criterion_assessments": criterion_assessments,
        "findings": findings,
        "summary": body.summary,
    }
    if isinstance(body, SecurityAssessmentResponse):
        return GroundedSecurityAssessmentResponse(
            **common,
            surfaces=body.surfaces,
            residual_risks=body.residual_risks,
        )
    if isinstance(body, ExperienceAssessmentResponse):
        return GroundedExperienceAssessmentResponse(
            **common,
            workflows=body.workflows,
            usability_risks=body.usability_risks,
        )
    return GroundedReviewReportResponse(**common)


type AgentResponseBody = (
    ImplementationPlanResponse
    | WorkResultResponse
    | TestReportResponse
    | ReviewReportResponse
    | GroundedReviewReportResponse
    | SecurityAssessmentResponse
    | GroundedSecurityAssessmentResponse
    | ExperienceAssessmentResponse
    | GroundedExperienceAssessmentResponse
)


RESPONSE_BODY_MODELS: dict[ArtifactKind, type[AgentResponseBody]] = {
    ArtifactKind.IMPLEMENTATION_PLAN: ImplementationPlanResponse,
    ArtifactKind.WORK_RESULT: WorkResultResponse,
    ArtifactKind.TEST_REPORT: TestReportResponse,
    ArtifactKind.REVIEW_REPORT: ReviewReportResponse,
    ArtifactKind.SECURITY_ASSESSMENT: SecurityAssessmentResponse,
    ArtifactKind.EXPERIENCE_ASSESSMENT: ExperienceAssessmentResponse,
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
    ArtifactKind.SECURITY_ASSESSMENT: frozenset(
        _COMMON_CONTROLLER_FIELDS | {"input_commit", "reviewed_criteria"}
    ),
    ArtifactKind.EXPERIENCE_ASSESSMENT: frozenset(
        _COMMON_CONTROLLER_FIELDS | {"input_commit", "reviewed_criteria"}
    ),
}
_PROTECTED_CONTROLLER_FIELD_NAMES = frozenset(
    {
        *(field for fields in _CONTROLLER_FIELDS.values() for field in fields),
        "command_evidence_ids",
        "execution_attempt",
        "tool_call_id",
    }
)


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
    semantic_payload: dict[str, object]
    response_normalizations: tuple[str, ...] = ()


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


def _normalize_review_boundary_scope_payload(
    semantic_payload: dict[str, object],
    *,
    task_brief: TaskBrief,
) -> tuple[dict[str, object], tuple[str, ...]]:
    """Remove only Review boundary checks outside the approved TaskBrief scope.

    The TaskBrief, rather than the Reviewer response, owns which entry
    boundaries are acceptance obligations. This normalization runs before the
    generic response model validates the contents of ``boundary_checks`` so an
    unapproved model-authored check cannot create a semantic correction. Checks
    for approved boundaries remain untouched and retain every structural and
    evidence-grounding validator.
    """

    assessments = semantic_payload.get("criterion_assessments")
    if not isinstance(assessments, list):
        return semantic_payload, ()
    approved_by_criterion = {
        criterion.id: frozenset(
            boundary.value for boundary in criterion.review_boundaries
        )
        for criterion in task_brief.acceptance_criteria
    }
    normalized_assessments: list[object] = []
    normalizations: list[str] = []
    changed = False
    for assessment in assessments:
        if not isinstance(assessment, dict):
            normalized_assessments.append(assessment)
            continue
        criterion_id = assessment.get("criterion_id")
        if (
            not isinstance(criterion_id, str)
            or criterion_id not in approved_by_criterion
        ):
            normalized_assessments.append(assessment)
            continue
        checks = assessment.get("boundary_checks")
        if checks is None:
            normalized_assessments.append(assessment)
            continue
        approved = approved_by_criterion[criterion_id]
        if not approved:
            if checks == []:
                normalized_assessments.append(assessment)
                continue
            normalized = dict(assessment)
            normalized["boundary_checks"] = []
            normalized_assessments.append(normalized)
            removed_count = len(checks) if isinstance(checks, list) else 1
            normalizations.append(
                "removed "
                f"{removed_count} unapproved boundary_checks from criterion "
                f"{criterion_id} (approved: none)"
            )
            changed = True
            continue
        if not isinstance(checks, list):
            normalized_assessments.append(assessment)
            continue
        filtered_checks: list[object] = []
        removed_boundaries: list[str] = []
        for check in checks:
            boundary = check.get("boundary") if isinstance(check, dict) else None
            if isinstance(boundary, str) and boundary not in approved:
                removed_boundaries.append(boundary)
                continue
            filtered_checks.append(check)
        if not removed_boundaries:
            normalized_assessments.append(assessment)
            continue
        normalized = dict(assessment)
        normalized["boundary_checks"] = filtered_checks
        normalized_assessments.append(normalized)
        normalizations.append(
            "removed "
            f"{len(removed_boundaries)} unapproved boundary_checks from criterion "
            f"{criterion_id} (approved: {', '.join(sorted(approved))})"
        )
        changed = True
    if not changed:
        return semantic_payload, ()
    normalized_payload = dict(semantic_payload)
    normalized_payload["criterion_assessments"] = normalized_assessments
    return normalized_payload, tuple(normalizations)


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
        detail = (
            "Agent response JSON is invalid: "
            f"{_safe_json_detail(error)}. The response must contain exactly one "
            "unambiguous JSON object; a single json fence or presentation-only "
            "surrounding prose and a bounded redundant closing-delimiter suffix "
            "are normalized, but multiple fences or outside JSON object "
            "candidates are forbidden"
        )
        raise AgentArtifactResponseError(
            detail,
            diagnostic=diagnostic_from_transport(
                value,
                code="invalid_json_transport",
                message=detail,
            ),
        ) from error
    if not isinstance(payload, dict):
        detail = "Agent response must be a JSON object"
        raise AgentArtifactResponseError(
            detail,
            diagnostic=diagnostic_from_transport(
                value,
                code="non_object_transport",
                message=detail,
            ),
        )
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


def _context_failure_diagnostic(
    parsed: ParsedAgentResponse,
    error: ValueError,
) -> ResponseValidationDiagnostic:
    """Locate post-schema semantic failures without authorizing whole replacement."""

    detail = _safe_validation_detail(error)
    grounding_errors: tuple[_ReviewEvidenceGroundingError, ...] = ()
    if isinstance(error, _ReviewEvidenceGroundingErrors):
        grounding_errors = error.errors
    elif isinstance(error, _ReviewEvidenceGroundingError):
        grounding_errors = (error,)
    if len(grounding_errors) > MAX_CORRECTION_FIELDS:
        return diagnostic_from_invariant(
            parsed.semantic_payload,
            failure_class=ResponseFailureClass.EVIDENCE_GROUNDING,
            authority=ResponseIssueAuthority.CONTROLLER,
            code="review_evidence_issue_overflow",
            invariant_id="review_evidence_issue_overflow",
            subjects=(),
            message=(
                "Review response has too many independent evidence selectors for "
                "one safe targeted correction"
            ),
            paths=("/",),
        )
    if grounding_errors:
        issues = tuple(
            ResponseValidationIssue(
                path=item.path,
                code="review_evidence_grounding",
                invariant_id=item.invariant_id,
                subjects=(
                    ResponseIssueSubject(
                        kind=ResponseIssueSubjectKind.CRITERION,
                        identifier=item.criterion_id,
                    ),
                ),
                message=str(item),
                authority=ResponseIssueAuthority.MODEL,
            )
            for item in grounding_errors
        )
        return ResponseValidationDiagnostic(
            failure_class=ResponseFailureClass.EVIDENCE_GROUNDING,
            response_sha256=semantic_payload_sha256(parsed.semantic_payload),
            issues=issues,
            correction_paths=tuple(sorted({item.path for item in grounding_errors})),
        )
    body = parsed.body
    failure_class = ResponseFailureClass.SEMANTIC_CONTEXT
    if isinstance(body, ImplementationPlanResponse):
        paths = ("/tasks",)
    elif isinstance(body, WorkResultResponse):
        paths = ("/completed_tasks",)
    elif isinstance(body, TestReportResponse):
        paths = ("/findings", "/summary")
    else:
        lowered = detail.casefold()
        if "evidence" in lowered or "tool" in lowered or "command" in lowered:
            failure_class = ResponseFailureClass.EVIDENCE_GROUNDING
            paths = ("/criterion_assessments",)
        elif "finding" in lowered:
            paths = ("/findings",)
        elif "verdict" in lowered or "assessment" in lowered:
            paths = ("/criterion_assessments", "/findings", "/verdict")
        else:
            paths = ("/criterion_assessments", "/findings")
    return diagnostic_from_message(
        parsed.semantic_payload,
        failure_class=failure_class,
        authority=ResponseIssueAuthority.MODEL,
        code="context_validation",
        message=detail,
        paths=paths,
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
            if isinstance(body, GroundedSecurityAssessmentResponse):
                specialized_scope = {
                    criterion_id
                    for surface in body.surfaces
                    for criterion_id in surface.criterion_ids
                }
            elif isinstance(body, GroundedExperienceAssessmentResponse):
                specialized_scope = {
                    criterion_id
                    for workflow in body.workflows
                    for criterion_id in workflow.criterion_ids
                }
            if (
                specialized_scope is not None
                and specialized_scope != expected_review_scope
            ):
                raise ValueError(
                    "specialized assessment entries must exactly cover assigned "
                    "review scope"
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
