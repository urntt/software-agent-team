"""Evidence matching, grounding, and verification for Review responses."""

from __future__ import annotations

import json
import re

from software_agent_team.artifacts import (
    AgentToolCallEvidence,
    AgentToolCallOutcome,
    AgentToolEvidenceStatus,
    CommandEvidence,
    ReviewBoundaryCheck,
    ReviewCriterionAssessment,
    ReviewCriterionStatus,
    ReviewFinding,
    ReviewSeverity,
    ReviewToolEvidenceReference,
    ReviewVerdict,
)
from software_agent_team.execution import AgentExecutionResult

from .models import (
    ExperienceAssessmentResponse,
    GroundedExperienceAssessmentResponse,
    GroundedReviewReportResponse,
    GroundedSecurityAssessmentResponse,
    ReviewCriterionAssessmentResponse,
    ReviewReportResponse,
    ReviewToolEvidenceClaim,
    SecurityAssessmentResponse,
)
from .types import (
    ReviewToolEvidenceAttempt,
    _ReviewEvidenceGroundingError,
    _ReviewEvidenceGroundingErrors,
    _ReviewEvidenceMatches,
    _UnsafeSatisfiedEvidenceError,
)

_PROBE_RESULT_PREFIX = "SAT_PROBE_RESULT_V1 "
_PROBE_STDOUT_BEGIN = "SAT_PROBE_STDOUT_BEGIN"
_PROBE_STDOUT_END = "SAT_PROBE_STDOUT_END"
_PROBE_STDERR_BEGIN = "SAT_PROBE_STDERR_BEGIN"
_PROBE_STDERR_END = "SAT_PROBE_STDERR_END"
_PROBE_EXECUTABLES = {"sat-probe-run", "/usr/local/bin/sat-probe-run"}
_LEGACY_EXIT_MARKER = re.compile(r"^EXIT=(-?[0-9]+)$")


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
    if not isinstance(payload, dict) or set(payload) != {
        "exit_code",
        "timed_out",
    }:
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
            (_positive_probe_match_surface(call) if satisfied else call.output_excerpt),
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
    successful_direct_matches = tuple(
        match
        for match in tool_matches
        if _is_direct_probe(match[1])
        and not _tool_result_ineligible_for_satisfied_claim(match[1])
    )
    if satisfied and successful_direct_matches:
        # A complete successful direct probe is the authoritative emission for
        # this exact fragment. Script writes, source displays, and failed
        # probes remain in the audit chain, but their incidental echoes do not
        # become grounded positive citations or invalidate the observation.
        tool_matches = successful_direct_matches
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
                    f"The controller could not verify the positive Review "
                    f"assessment for {criterion_id}: {detail}"
                ),
                recommendation=(
                    "Run a fresh isolated protocol-eligible check and cite "
                    "evidence from a successful result before accepting this "
                    "criterion."
                ),
                criterion_ids=(criterion_id,),
            )
        )
    return _build_grounded_review_response(
        body,
        criterion_assessments=grounded,
        findings=_bind_unambiguous_blocking_finding_scope(tuple(findings), grounded),
    )
