"""Pointer utilities and diagnostic builders for response validation."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy

from pydantic import ValidationError

from software_agent_team.response_corrections.types import (
    MAX_CORRECTION_FIELDS,
    ResponseFailureClass,
    ResponseIssueAuthority,
    ResponseIssueSubject,
    ResponseValidationDiagnostic,
    ResponseValidationIssue,
)


def _json_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def semantic_payload_sha256(payload: dict[str, object]) -> str:
    """Return the canonical identity of one untrusted semantic payload."""

    return _json_sha256(payload)


def _encode_pointer(parts: tuple[str | int, ...]) -> str:
    if not parts:
        return "/"
    return "/" + "/".join(
        str(part).replace("~", "~0").replace("/", "~1") for part in parts
    )


def _decode_pointer(pointer: str) -> tuple[str, ...]:
    if pointer == "/":
        return ()
    parts: list[str] = []
    for raw in pointer[1:].split("/"):
        index = 0
        while index < len(raw):
            if raw[index] == "~":
                if index + 1 >= len(raw) or raw[index + 1] not in {"0", "1"}:
                    raise ValueError("JSON pointer contains an invalid escape")
                index += 2
            else:
                index += 1
        parts.append(raw.replace("~1", "/").replace("~0", "~"))
    return tuple(parts)


def _paths_overlap(left: str, right: str) -> bool:
    left_parts = _decode_pointer(left)
    right_parts = _decode_pointer(right)
    limit = min(len(left_parts), len(right_parts))
    return left_parts[:limit] == right_parts[:limit]


def _path_contains(parent: str, child: str) -> bool:
    parent_parts = _decode_pointer(parent)
    child_parts = _decode_pointer(child)
    return child_parts[: len(parent_parts)] == parent_parts


def _minimal_correction_paths(
    issues: tuple[ResponseValidationIssue, ...],
) -> tuple[str, ...]:
    non_model_paths = {
        issue.path
        for issue in issues
        if issue.authority is not ResponseIssueAuthority.MODEL
    }
    candidates = sorted(
        {
            issue.path
            for issue in issues
            if (
                issue.authority is ResponseIssueAuthority.MODEL
                and issue.path != "/"
                and not any(
                    _paths_overlap(issue.path, non_model_path)
                    for non_model_path in non_model_paths
                )
            )
        }
    )
    # Prefer a precise descendant over a cascading container error. If only a
    # container-level validator is available, that container remains the target.
    return tuple(
        path
        for path in candidates
        if not any(
            other != path
            and len(_decode_pointer(other)) > len(_decode_pointer(path))
            and _paths_overlap(path, other)
            for other in candidates
        )
    )


def diagnostic_from_validation_error(
    error: ValidationError,
    payload: dict[str, object],
    *,
    failure_class: ResponseFailureClass = ResponseFailureClass.SEMANTIC_SCHEMA,
    authority: ResponseIssueAuthority = ResponseIssueAuthority.MODEL,
    protected_field_names: frozenset[str] = frozenset(),
) -> ResponseValidationDiagnostic:
    """Convert Pydantic failures to a bounded content-free field set."""

    raw_issues = error.errors(
        include_url=False,
        include_context=False,
        include_input=False,
    )
    if len(raw_issues) > MAX_CORRECTION_FIELDS:
        issues = (
            ResponseValidationIssue(
                path="/",
                code="validation_issue_overflow",
                invariant_id="validation_issue_overflow",
                message=(
                    "Response has too many independent validation failures for "
                    "safe field-targeted correction"
                ),
                authority=ResponseIssueAuthority.CONTROLLER,
            ),
        )
        return ResponseValidationDiagnostic(
            failure_class=failure_class,
            response_sha256=semantic_payload_sha256(payload),
            issues=issues,
            correction_paths=(),
        )
    issues = tuple(
        ResponseValidationIssue(
            path=_encode_pointer(tuple(item for item in issue["loc"])),
            code=str(issue["type"]).replace(".", "_")[:100],
            invariant_id=str(issue["type"]).replace(".", "_")[:100],
            message=str(issue["msg"])[:500],
            authority=(
                ResponseIssueAuthority.CONTROLLER
                if (
                    issue["type"] == "extra_forbidden"
                    and issue["loc"]
                    and str(issue["loc"][-1]) in protected_field_names
                )
                else authority
            ),
        )
        for issue in raw_issues
    )
    return ResponseValidationDiagnostic(
        failure_class=failure_class,
        response_sha256=semantic_payload_sha256(payload),
        issues=issues,
        correction_paths=_minimal_correction_paths(issues),
    )


def diagnostic_from_message(
    payload: dict[str, object],
    *,
    failure_class: ResponseFailureClass,
    authority: ResponseIssueAuthority,
    code: str,
    message: str,
    paths: tuple[str, ...],
) -> ResponseValidationDiagnostic:
    """Create a typed diagnostic for a controller context validator."""

    issues = tuple(
        ResponseValidationIssue(
            path=path,
            code=code,
            invariant_id=code,
            message=message[:500],
            authority=authority,
        )
        for path in paths
    )
    return ResponseValidationDiagnostic(
        failure_class=failure_class,
        response_sha256=semantic_payload_sha256(payload),
        issues=issues,
        correction_paths=_minimal_correction_paths(issues),
    )


def diagnostic_from_invariant(
    payload: dict[str, object],
    *,
    failure_class: ResponseFailureClass,
    authority: ResponseIssueAuthority,
    code: str,
    invariant_id: str,
    subjects: tuple[ResponseIssueSubject, ...],
    message: str,
    paths: tuple[str, ...],
) -> ResponseValidationDiagnostic:
    """Create a diagnostic from a validator-owned invariant and entities."""

    issues = tuple(
        ResponseValidationIssue(
            path=path,
            code=code,
            invariant_id=invariant_id,
            subjects=subjects,
            message=message[:500],
            authority=authority,
        )
        for path in paths
    )
    return ResponseValidationDiagnostic(
        failure_class=failure_class,
        response_sha256=semantic_payload_sha256(payload),
        issues=issues,
        correction_paths=_minimal_correction_paths(issues),
    )


def diagnostic_from_transport(
    value: str,
    *,
    code: str,
    message: str,
) -> ResponseValidationDiagnostic:
    """Record an unparseable response without pretending a field can be patched."""

    issue = ResponseValidationIssue(
        path="/",
        code=code,
        invariant_id=code,
        message=message[:500],
        authority=ResponseIssueAuthority.TRANSPORT,
    )
    return ResponseValidationDiagnostic(
        failure_class=ResponseFailureClass.TRANSPORT,
        response_sha256=hashlib.sha256(value.encode()).hexdigest(),
        issues=(issue,),
        correction_paths=(),
    )


def deterministically_remove_forbidden_fields(
    payload: dict[str, object],
    error: ValidationError,
    *,
    protected_field_names: frozenset[str] = frozenset(),
) -> tuple[dict[str, object], tuple[str, ...]]:
    """Discard only non-authoritative fields the schema explicitly forbids."""

    paths = sorted(
        {
            _encode_pointer(tuple(item for item in issue["loc"]))
            for issue in error.errors(
                include_url=False,
                include_context=False,
                include_input=False,
            )
            if (
                issue["type"] == "extra_forbidden"
                and issue["loc"]
                and str(issue["loc"][-1]) not in protected_field_names
            )
        },
        key=lambda value: (-len(_decode_pointer(value)), value),
    )
    if len(paths) > MAX_CORRECTION_FIELDS:
        return deepcopy(payload), ()
    normalized: object = deepcopy(payload)
    removed: list[str] = []
    for path in paths:
        parts = _decode_pointer(path)
        parent = normalized
        for part in parts[:-1]:
            if isinstance(parent, dict) and part in parent:
                parent = parent[part]
            elif (
                isinstance(parent, list)
                and part.isdecimal()
                and int(part) < len(parent)
            ):
                parent = parent[int(part)]
            else:
                parent = None
                break
        final = parts[-1]
        if isinstance(parent, dict) and final in parent:
            del parent[final]
            removed.append(path)
    assert isinstance(normalized, dict)
    return normalized, tuple(sorted(removed))


def _schema_constraint_rank(code: str) -> int:
    """Order schema diagnostics from coarse shape to semantic constraints."""

    if code.endswith("_type") or code in {"model_attributes_type", "none_required"}:
        return 0
    if code in {"assertion_error", "value_error"}:
        return 2
    return 1


__all__ = [
    "_decode_pointer",
    "_encode_pointer",
    "_json_sha256",
    "_minimal_correction_paths",
    "_path_contains",
    "_paths_overlap",
    "_schema_constraint_rank",
    "deterministically_remove_forbidden_fields",
    "diagnostic_from_invariant",
    "diagnostic_from_message",
    "diagnostic_from_transport",
    "diagnostic_from_validation_error",
    "semantic_payload_sha256",
]
