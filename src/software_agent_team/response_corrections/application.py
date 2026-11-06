"""Payload correction application and outcome determination."""

from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy

from pydantic import JsonValue, ValidationError

from software_agent_team.response_corrections.diagnostics import (
    _decode_pointer,
    _paths_overlap,
    _schema_constraint_rank,
    diagnostic_from_invariant,
)
from software_agent_team.response_corrections.plans import (
    _semantic_correction_slot_bindings,
    attach_semantic_correction_candidates,
    build_semantic_correction_plan,
)
from software_agent_team.response_corrections.schemas import (
    _structured_schema_root_types,
    correction_value_schema,
)
from software_agent_team.response_corrections.types import (
    MAX_SERIALIZED_CORRECTION_VALUE_BYTES,
    ResponseFailureClass,
    ResponseIssueAuthority,
    ResponseValidationDiagnostic,
    SemanticCorrectionApplication,
    SemanticCorrectionOutcome,
    SemanticCorrectionPlan,
    SemanticCorrectionSubmission,
    SemanticCorrectionSubmissionError,
)


def apply_semantic_correction(
    submission_payload: dict[str, object],
    plan: SemanticCorrectionPlan,
    *,
    response_schema: Mapping[str, JsonValue] | None = None,
) -> dict[str, object]:
    """Apply exactly the authorized replacements to a copied base payload."""

    return apply_semantic_correction_with_evidence(
        submission_payload,
        plan,
        response_schema=response_schema,
    ).payload


def _reject_duplicate_correction_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    """Reject lossy duplicate keys while decoding one structured replacement."""

    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _decode_serialized_structured_replacement(
    value: JsonValue,
    *,
    value_schema: Mapping[str, JsonValue] | None,
) -> tuple[JsonValue, str | None]:
    """Decode one JSON-serialized container only under an exact typed contract."""

    if not isinstance(value, str) or value_schema is None:
        return value, None
    expected_types = _structured_schema_root_types(value_schema)
    if not expected_types or len(value.encode("utf-8")) > (
        MAX_SERIALIZED_CORRECTION_VALUE_BYTES
    ):
        return value, None
    try:
        decoded = json.loads(
            value,
            object_pairs_hook=_reject_duplicate_correction_keys,
            parse_constant=lambda name: (_ for _ in ()).throw(
                ValueError(f"non-standard JSON constant: {name}")
            ),
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        return value, None
    if isinstance(decoded, dict) and "object" in expected_types:
        return decoded, "object"
    if isinstance(decoded, list) and "array" in expected_types:
        return decoded, "array"
    return value, None


def apply_semantic_correction_with_evidence(
    submission_payload: dict[str, object],
    plan: SemanticCorrectionPlan,
    *,
    response_schema: Mapping[str, JsonValue] | None = None,
) -> SemanticCorrectionApplication:
    """Apply replacements and record every controller-owned candidate binding."""

    try:
        submission = SemanticCorrectionSubmission.model_validate(submission_payload)
    except ValidationError as error:
        raise SemanticCorrectionSubmissionError(
            "semantic correction requires unique typed slot/value records",
            plan=plan,
        ) from error
    expected_handles = _semantic_correction_slot_bindings(plan)
    submitted_by_handle = {
        item.slot_handle: item.replacement_value for item in submission.replacements
    }
    submitted_handles = set(submitted_by_handle)
    unknown_handles = submitted_handles - set(expected_handles)
    if unknown_handles:
        raise SemanticCorrectionSubmissionError(
            "semantic correction includes a slot outside controller authority",
            plan=plan,
        )
    if plan.require_all_targets:
        missing_handles = set(expected_handles) - submitted_handles
        if missing_handles:
            missing_paths = tuple(
                path
                for handle, path in expected_handles.items()
                if handle in missing_handles
            )
            raise SemanticCorrectionSubmissionError(
                "final bounded semantic correction must cover every authorized slot",
                plan=plan,
                paths=missing_paths,
            )

    candidate_slots = {slot.target_path: slot for slot in plan.candidate_slots}
    resolved_values: dict[str, JsonValue] = {}
    normalizations: list[str] = []
    invalid_paths: list[str] = []
    handles_by_path = {path: handle for handle, path in expected_handles.items()}
    for path in plan.evidence.target_paths:
        handle = handles_by_path[path]
        if handle not in submitted_by_handle:
            continue
        submitted_value = submitted_by_handle[handle]
        candidate_slot = candidate_slots.get(path)
        if candidate_slot is None:
            value_schema = (
                None
                if response_schema is None
                else correction_value_schema(response_schema, path)
            )
            resolved_value, decoded_type = _decode_serialized_structured_replacement(
                submitted_value,
                value_schema=value_schema,
            )
            expected_types = (
                frozenset()
                if value_schema is None
                else _structured_schema_root_types(value_schema)
            )
            resolved_type = (
                "object"
                if isinstance(resolved_value, dict)
                else "array"
                if isinstance(resolved_value, list)
                else None
            )
            if expected_types and resolved_type not in expected_types:
                invalid_paths.append(path)
                continue
            resolved_values[path] = resolved_value
            if decoded_type is not None:
                normalizations.append(
                    "decoded JSON-serialized "
                    f"{decoded_type} correction value for {path}"
                )
            continue
        candidates = {item.handle: item for item in candidate_slot.candidates}
        if not isinstance(submitted_value, str) or submitted_value not in candidates:
            invalid_paths.append(path)
            continue
        selected = candidates[submitted_value]
        resolved_values[path] = selected.replacement_value
        normalizations.append(
            f"bound controller evidence candidate {selected.handle} to {path}"
        )

    if invalid_paths and len(candidate_slots) != len(plan.evidence.target_paths):
        # Unvalidated free-form siblings cannot establish selection progress.
        resolved_values = {}
        normalizations = []
    corrected = _apply_resolved_correction_values(plan.base_payload, resolved_values)
    if invalid_paths:
        recovery = None
        if resolved_values:
            diagnostic = diagnostic_from_invariant(
                corrected,
                failure_class=ResponseFailureClass.SEMANTIC_SCHEMA,
                authority=ResponseIssueAuthority.MODEL,
                code="correction_candidate_invalid",
                invariant_id="correction_candidate_membership",
                subjects=(),
                message="Select an exact candidate handle from this slot's catalog.",
                paths=tuple(invalid_paths),
            )
            pending = build_semantic_correction_plan(corrected, diagnostic)
            assert pending is not None
            recovery = attach_semantic_correction_candidates(
                pending,
                tuple(candidate_slots[path] for path in pending.evidence.target_paths),
            )
        raise SemanticCorrectionSubmissionError(
            "submitted value is not authorized for correction slot "
            + ", ".join(invalid_paths),
            plan=plan,
            paths=tuple(invalid_paths),
            recovery_plan=recovery,
            normalizations=tuple(normalizations),
        )
    return SemanticCorrectionApplication(
        payload=corrected,
        normalizations=tuple(normalizations),
    )


def _apply_resolved_correction_values(
    base_payload: dict[str, object], resolved_values: dict[str, JsonValue]
) -> dict[str, object]:
    """Stage verified bindings; unreachable Controller pointers remain faults."""

    corrected: object = deepcopy(base_payload)
    for path, replacement_value in resolved_values.items():
        parts = _decode_pointer(path)
        parent = corrected
        for part in parts[:-1]:
            if isinstance(parent, dict):
                if part not in parent:
                    raise ValueError(f"semantic correction parent is missing: {path}")
                parent = parent[part]
            elif isinstance(parent, list):
                if not part.isdecimal() or int(part) >= len(parent):
                    raise ValueError(f"semantic correction index is invalid: {path}")
                parent = parent[int(part)]
            else:
                raise ValueError(
                    f"semantic correction parent is not a container: {path}"
                )
        final = parts[-1]
        if isinstance(parent, dict):
            parent[final] = deepcopy(replacement_value)
        elif isinstance(parent, list):
            if not final.isdecimal() or int(final) >= len(parent):
                raise ValueError(f"semantic correction index is invalid: {path}")
            parent[int(final)] = deepcopy(replacement_value)
        else:
            raise ValueError(f"semantic correction target is not a container: {path}")
    assert isinstance(corrected, dict)
    return corrected


def correction_outcome(
    plan: SemanticCorrectionPlan,
    diagnostic: ResponseValidationDiagnostic | None,
    *,
    seen_fingerprints: frozenset[str],
) -> SemanticCorrectionOutcome:
    """Continue only after strict typed progress without a schema regression."""

    if diagnostic is None:
        return SemanticCorrectionOutcome.ACCEPTED
    if diagnostic.fingerprint in seen_fingerprints:
        return SemanticCorrectionOutcome.NO_IMPROVEMENT
    if diagnostic.failure_class is ResponseFailureClass.SEMANTIC_SCHEMA:
        for target in plan.evidence.target_paths:
            previous_ranks = tuple(
                _schema_constraint_rank(issue.code)
                for issue in plan.diagnostic.issues
                if _paths_overlap(issue.path, target)
            )
            previous_rank = min(previous_ranks, default=2)
            if any(
                _paths_overlap(issue.path, target)
                and _schema_constraint_rank(issue.code) < previous_rank
                for issue in diagnostic.issues
            ):
                # A correction may expose a more specific declarative or
                # semantic invariant. Moving backwards to a coarser type/shape
                # failure in the same authority slot is a regression, even
                # when it has a different fingerprint.
                return SemanticCorrectionOutcome.NO_IMPROVEMENT
    prior_issues = {
        issue.identity
        for issue in plan.diagnostic.issues
        if any(
            _paths_overlap(issue.path, target) for target in plan.evidence.target_paths
        )
    }
    current_issues = {issue.identity for issue in diagnostic.issues}
    if prior_issues <= current_issues:
        # Retaining every prior issue is not progress, even if another issue
        # became visible. Removing at least one independently identified
        # sibling is measurable progress: the next plan narrows to the
        # remaining issue set, and its fingerprint stops an unchanged retry.
        return SemanticCorrectionOutcome.NO_IMPROVEMENT
    return SemanticCorrectionOutcome.IMPROVED


__all__ = [
    "_apply_resolved_correction_values",
    "_decode_serialized_structured_replacement",
    "_reject_duplicate_correction_keys",
    "apply_semantic_correction",
    "apply_semantic_correction_with_evidence",
    "correction_outcome",
]
