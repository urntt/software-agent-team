"""JSON payload normalization and boundary scope sanitization for Agent responses."""

from __future__ import annotations

import json

from software_agent_team.artifacts import TaskBrief
from software_agent_team.response_corrections import diagnostic_from_transport

from .types import AgentArtifactResponseError


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


def _reject_nonstandard_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _reject_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
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
