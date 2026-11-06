"""Review evidence correction candidate binding and failure diagnostics."""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from pydantic import ValidationError

from software_agent_team.artifacts import (
    CommandEvidence,
    ReviewCriterionStatus,
)
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
    build_semantic_correction_plan,
    diagnostic_from_invariant,
    diagnostic_from_message,
    semantic_payload_sha256,
)

from .evidence_matching import (
    _is_safe_satisfied_evidence_candidate,
    _matched_command_failed,
    _positive_probe_match_surface,
    _tool_result_ineligible_for_satisfied_claim,
)
from .models import (
    ImplementationPlanResponse,
    ParsedAgentResponse,
    TestReportResponse,
    WorkResultResponse,
)
from .types import (
    ReviewToolEvidenceAttempt,
    _ReviewEvidenceGroundingError,
    _ReviewEvidenceGroundingErrors,
    _SpecializedReviewScopeError,
)

_MAX_REVIEW_EVIDENCE_CANDIDATES_PER_SLOT = 16
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

    plan = _target_duplicate_boundary_observables(plan)
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
                    (
                        f"deterministic command {command.id} stdout",
                        command.stdout_tail,
                    ),
                    (
                        f"deterministic command {command.id} stderr",
                        command.stderr_tail,
                    ),
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


def _target_duplicate_boundary_observables(
    plan: SemanticCorrectionPlan,
) -> SemanticCorrectionPlan:
    """Narrow a list-level distinctness error to the repeated evidence leaves.

    The model must choose independently grounded fragments, but it need not
    rewrite otherwise valid boundary objects to do so. Leave malformed or
    oversized values on the original fail-closed correction path.
    """

    issues = plan.diagnostic.issues
    if not issues or any(
        issue.authority is not ResponseIssueAuthority.MODEL
        or not re.fullmatch(r"/criterion_assessments/\d+/boundary_checks", issue.path)
        or issue.message
        != "Value error, criterion boundary checks require distinct evidence fragments"
        for issue in issues
    ):
        return plan
    repeated_paths: list[str] = []
    for issue in issues:
        try:
            checks = _semantic_value_at_pointer(plan.base_payload, issue.path)
        except ValueError:
            return plan
        if not isinstance(checks, list):
            return plan
        seen: set[str] = set()
        for check_index, check in enumerate(checks):
            if not isinstance(check, dict) or not isinstance(
                claims := check.get("tool_evidence"), list
            ):
                return plan
            for claim_index, claim in enumerate(claims):
                if not isinstance(claim, dict) or not isinstance(
                    observable := claim.get("observable"), str
                ):
                    return plan
                if observable in seen:
                    repeated_paths.append(
                        f"{issue.path}/{check_index}/tool_evidence/"
                        f"{claim_index}/observable"
                    )
                seen.add(observable)
    if not repeated_paths or len(repeated_paths) > MAX_CORRECTION_FIELDS:
        return plan
    diagnostic = diagnostic_from_invariant(
        plan.base_payload,
        failure_class=ResponseFailureClass.SEMANTIC_SCHEMA,
        authority=ResponseIssueAuthority.MODEL,
        code="review_boundary_evidence_duplicate",
        invariant_id="review_boundary_evidence_distinct",
        subjects=(),
        message="Select a distinct, grounded result fragment for this boundary.",
        paths=tuple(repeated_paths),
    )
    return build_semantic_correction_plan(plan.base_payload, diagnostic) or plan


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
                "Review response has too many independent evidence selectors "
                "for one safe targeted correction"
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
    if isinstance(error, _SpecializedReviewScopeError):
        return diagnostic_from_invariant(
            parsed.semantic_payload,
            failure_class=ResponseFailureClass.SEMANTIC_CONTEXT,
            authority=ResponseIssueAuthority.MODEL,
            code="specialized_review_scope",
            invariant_id="specialized_review_scope",
            subjects=(),
            message=detail,
            paths=(error.path,),
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
        elif "verdict" in lowered or "assessment" in lowered:
            paths = ("/criterion_assessments", "/findings", "/verdict")
        elif "finding" in lowered:
            paths = ("/findings",)
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
