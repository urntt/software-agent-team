"""Tests for field-targeted semantic response correction."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from software_agent_team.response_corrections import (
    ResponseFailureClass,
    ResponseIssueAuthority,
    ResponseIssueSubject,
    ResponseIssueSubjectKind,
    ResponseValidationDiagnostic,
    ResponseValidationIssue,
    SemanticCorrectionOutcome,
    apply_semantic_correction,
    build_semantic_correction_plan,
    correction_outcome,
    correction_prompt,
    deterministically_remove_forbidden_fields,
    diagnostic_from_invariant,
    diagnostic_from_message,
    diagnostic_from_validation_error,
    semantic_payload_sha256,
)


class ExampleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1)
    tasks: tuple[str, ...] = Field(min_length=1)
    preserved: str


def diagnostic(payload: dict[str, object]):  # type: ignore[no-untyped-def]
    try:
        ExampleBody.model_validate(payload)
    except ValidationError as error:
        return diagnostic_from_validation_error(error, payload)
    raise AssertionError("test payload unexpectedly validated")


def test_plan_targets_only_invalid_fields_and_preserves_other_content() -> None:
    payload: dict[str, object] = {
        "summary": "",
        "tasks": ["TASK_ONE"],
        "preserved": "keep this exact value",
    }
    report = diagnostic(payload)
    plan = build_semantic_correction_plan(payload, report)

    assert plan is not None
    assert plan.evidence.target_paths == ("/summary",)
    assert plan.evidence.preserved_top_level_paths == ("/preserved", "/tasks")

    corrected = apply_semantic_correction(
        {
            "kind": "semantic_correction_v2",
            "base_response_sha256": semantic_payload_sha256(payload),
            "replacement_values": ["valid summary"],
        },
        plan,
    )
    assert corrected == {
        "summary": "valid summary",
        "tasks": ["TASK_ONE"],
        "preserved": "keep this exact value",
    }


def test_correction_rejects_a_value_count_that_cannot_bind_all_targets() -> None:
    payload: dict[str, object] = {
        "summary": "",
        "tasks": [],
        "preserved": "keep",
    }
    plan = build_semantic_correction_plan(payload, diagnostic(payload))
    assert plan is not None

    try:
        apply_semantic_correction(
            {
                "kind": "semantic_correction_v2",
                "base_response_sha256": semantic_payload_sha256(payload),
                "replacement_values": ["valid"],
            },
            plan,
        )
    except ValueError as error:
        assert "value count differs: expected 2, received 1" in str(error)
    else:
        raise AssertionError("incomplete semantic correction was accepted")


def test_correction_prompt_keeps_path_authority_in_the_controller() -> None:
    payload: dict[str, object] = {
        "items": [{"id": "first"}, {"id": "second"}],
        "preserved": "keep",
    }
    report = ResponseValidationDiagnostic(
        failure_class=ResponseFailureClass.SEMANTIC_SCHEMA,
        response_sha256=semantic_payload_sha256(payload),
        issues=(
            ResponseValidationIssue(
                path="/items/0/id",
                code="invalid_id",
                invariant_id="invalid_id",
                message="first ID is invalid",
                authority=ResponseIssueAuthority.MODEL,
            ),
            ResponseValidationIssue(
                path="/items/1/id",
                code="invalid_id",
                invariant_id="invalid_id",
                message="second ID is invalid",
                authority=ResponseIssueAuthority.MODEL,
            ),
            ResponseValidationIssue(
                path="/items",
                code="derived_container_error",
                invariant_id="derived_container_error",
                message="derived parent error must not be requested",
                authority=ResponseIssueAuthority.MODEL,
            ),
        ),
        correction_paths=("/items/0/id", "/items/1/id"),
    )
    plan = build_semantic_correction_plan(payload, report)
    assert plan is not None

    prompt = correction_prompt(plan)
    schema = prompt.split("CORRECTION_SCHEMA_JSON\n", maxsplit=1)[1]

    assert "TARGETED_SEMANTIC_CORRECTION_V2" in prompt
    assert "Do not repeat or choose target paths" in prompt
    assert "derived parent error must not be requested" not in prompt
    assert '"target_path": "/items/0/id"' in prompt
    assert '"target_path": "/items/1/id"' in prompt
    assert '"replacement_values"' in schema
    assert '"minItems": 2' in schema
    assert '"maxItems": 2' in schema
    assert '"path"' not in schema

    corrected = apply_semantic_correction(
        {
            "kind": "semantic_correction_v2",
            "base_response_sha256": semantic_payload_sha256(payload),
            "replacement_values": ["FIRST", "SECOND"],
        },
        plan,
    )
    assert corrected == {
        "items": [{"id": "FIRST"}, {"id": "SECOND"}],
        "preserved": "keep",
    }


def test_outcome_requires_targeted_errors_to_disappear_and_rejects_cycles() -> None:
    first: dict[str, object] = {
        "summary": "",
        "tasks": ["TASK_ONE"],
        "preserved": "keep",
    }
    first_diagnostic = diagnostic(first)
    plan = build_semantic_correction_plan(first, first_diagnostic)
    assert plan is not None

    same_path: dict[str, object] = {
        "summary": "",
        "tasks": ["TASK_ONE"],
        "preserved": "keep",
    }
    assert (
        correction_outcome(
            plan,
            diagnostic(same_path),
            seen_fingerprints=frozenset({first_diagnostic.fingerprint}),
        )
        is SemanticCorrectionOutcome.NO_IMPROVEMENT
    )

    new_path: dict[str, object] = {
        "summary": "valid",
        "tasks": [],
        "preserved": "keep",
    }
    assert (
        correction_outcome(
            plan,
            diagnostic(new_path),
            seen_fingerprints=frozenset({first_diagnostic.fingerprint}),
        )
        is SemanticCorrectionOutcome.IMPROVED
    )
    assert (
        correction_outcome(
            plan,
            None,
            seen_fingerprints=frozenset({first_diagnostic.fingerprint}),
        )
        is SemanticCorrectionOutcome.ACCEPTED
    )


def test_outcome_distinguishes_a_new_container_error_from_the_fixed_child() -> None:
    payload: dict[str, object] = {
        "summary": "",
        "tasks": ["TASK_ONE"],
        "preserved": "keep",
    }
    first_diagnostic = diagnostic(payload)
    plan = build_semantic_correction_plan(payload, first_diagnostic)
    assert plan is not None
    corrected = apply_semantic_correction(
        {
            "kind": "semantic_correction_v2",
            "base_response_sha256": semantic_payload_sha256(payload),
            "replacement_values": ["valid"],
        },
        plan,
    )
    relational = diagnostic_from_message(
        corrected,
        failure_class=ResponseFailureClass.SEMANTIC_CONTEXT,
        authority=ResponseIssueAuthority.MODEL,
        code="relational_context",
        message="the collection needs another authority field",
        paths=("/",),
    )

    assert (
        correction_outcome(
            plan,
            relational,
            seen_fingerprints=frozenset({first_diagnostic.fingerprint}),
        )
        is SemanticCorrectionOutcome.IMPROVED
    )


def test_outcome_uses_invariant_and_subject_instead_of_message_or_path() -> None:
    first_payload: dict[str, object] = {
        "kind": "proposal",
        "proposal": {"tasks": [], "acceptance_criteria": ["AC_COUNTS"]},
    }
    first = diagnostic_from_invariant(
        first_payload,
        failure_class=ResponseFailureClass.SEMANTIC_CONTEXT,
        authority=ResponseIssueAuthority.MODEL,
        code="planning_context",
        invariant_id="planning_writer_criterion_coverage",
        subjects=(
            ResponseIssueSubject(
                kind=ResponseIssueSubjectKind.CRITERION,
                identifier="AC_COUNTS",
            ),
        ),
        message="writer coverage failed",
        paths=("/proposal/tasks",),
    )
    plan = build_semantic_correction_plan(first_payload, first)
    assert plan is not None
    second_payload: dict[str, object] = {
        "kind": "proposal",
        "proposal": {
            "tasks": ["TASK_COUNTS"],
            "acceptance_criteria": ["AC_SCAN"],
        },
    }
    newly_exposed = diagnostic_from_invariant(
        second_payload,
        failure_class=ResponseFailureClass.SEMANTIC_CONTEXT,
        authority=ResponseIssueAuthority.MODEL,
        code="planning_context",
        invariant_id="planning_criterion_verifier_capability",
        subjects=(
            ResponseIssueSubject(
                kind=ResponseIssueSubjectKind.AGENT,
                identifier="impl",
            ),
            ResponseIssueSubject(
                kind=ResponseIssueSubjectKind.CRITERION,
                identifier="AC_SCAN",
            ),
        ),
        message="a completely different human-readable message",
        paths=("/proposal/tasks",),
    )

    assert first.fingerprint != newly_exposed.fingerprint
    assert (
        correction_outcome(
            plan,
            newly_exposed,
            seen_fingerprints=frozenset({first.fingerprint}),
        )
        is SemanticCorrectionOutcome.IMPROVED
    )

    same_invariant_at_a_refined_path = diagnostic_from_invariant(
        second_payload,
        failure_class=ResponseFailureClass.SEMANTIC_CONTEXT,
        authority=ResponseIssueAuthority.MODEL,
        code="planning_context",
        invariant_id="planning_writer_criterion_coverage",
        subjects=(
            ResponseIssueSubject(
                kind=ResponseIssueSubjectKind.CRITERION,
                identifier="AC_COUNTS",
            ),
        ),
        message="rewritten message text",
        paths=("/proposal/tasks/0/acceptance_criteria",),
    )
    assert (
        correction_outcome(
            plan,
            same_invariant_at_a_refined_path,
            seen_fingerprints=frozenset({first.fingerprint}),
        )
        is SemanticCorrectionOutcome.NO_IMPROVEMENT
    )


def test_legacy_diagnostic_remains_readable_with_path_based_identity() -> None:
    payload = {
        "schema_version": 1,
        "failure_class": "semantic_context",
        "response_sha256": "a" * 64,
        "issues": [
            {
                "path": "/summary",
                "code": "too_short",
                "message": "legacy issue",
                "authority": "model",
            }
        ],
        "correction_paths": ["/summary"],
    }
    legacy = ResponseValidationDiagnostic.model_validate(payload)

    assert legacy.issues[0].invariant_id is None
    assert legacy.issues[0].identity[2] == "/summary"
    assert legacy.model_dump(mode="json") == payload
    assert legacy.fingerprint == semantic_payload_sha256(
        {
            "failure_class": "semantic_context",
            "issues": [
                {
                    "path": "/summary",
                    "code": "too_short",
                    "authority": "model",
                }
            ],
            "correction_paths": ["/summary"],
        }
    )


def test_current_diagnostic_requires_a_stable_invariant_id() -> None:
    with pytest.raises(ValidationError, match="stable invariant ID"):
        ResponseValidationDiagnostic.model_validate(
            {
                "schema_version": 2,
                "failure_class": "semantic_context",
                "response_sha256": "a" * 64,
                "issues": [
                    {
                        "path": "/summary",
                        "code": "too_short",
                        "message": "current issue without an invariant",
                        "authority": "model",
                    }
                ],
                "correction_paths": ["/summary"],
            }
        )


def test_unlocated_whole_response_error_cannot_create_a_correction_plan() -> None:
    payload: dict[str, object] = {
        "summary": "valid",
        "tasks": ["TASK_ONE"],
        "preserved": "keep",
    }
    report = diagnostic_from_validation_error(
        ValidationError.from_exception_data(
            "ExampleBody",
            [
                {
                    "type": "value_error",
                    "loc": (),
                    "ctx": {"error": ValueError("x")},
                    "input": payload,
                }
            ],
        ),
        payload,
        failure_class=ResponseFailureClass.SEMANTIC_CONTEXT,
    )

    assert report.correction_paths == ()
    assert build_semantic_correction_plan(payload, report) is None


def test_validation_issue_overflow_fails_closed_instead_of_partially_repairing() -> (
    None
):
    payload: dict[str, object] = {
        "summary": "valid",
        "tasks": ["TASK_ONE"],
        "preserved": "keep",
    }
    report = diagnostic_from_validation_error(
        ValidationError.from_exception_data(
            "ExampleBody",
            [
                {
                    "type": "missing",
                    "loc": (f"field_{index}",),
                    "input": payload,
                }
                for index in range(65)
            ],
        ),
        payload,
    )

    assert report.issues[0].code == "validation_issue_overflow"
    assert report.correction_paths == ()
    assert build_semantic_correction_plan(payload, report) is None


def test_forbidden_field_overflow_is_not_partially_normalized() -> None:
    payload: dict[str, object] = {
        "summary": "valid",
        "tasks": ["TASK_ONE"],
        "preserved": "keep",
        **{f"extra_{index}": index for index in range(65)},
    }
    try:
        ExampleBody.model_validate(payload)
    except ValidationError as error:
        normalized, removed = deterministically_remove_forbidden_fields(payload, error)
    else:
        raise AssertionError("test payload unexpectedly validated")

    assert normalized == payload
    assert removed == ()


def test_protected_child_prevents_cascading_parent_correction() -> None:
    payload: dict[str, object] = {
        "items": [{"tool_call_id": "tool-001"}],
    }
    error = ValidationError.from_exception_data(
        "ProtectedContainer",
        [
            {
                "type": "extra_forbidden",
                "loc": ("items", 0, "tool_call_id"),
                "input": "tool-001",
            },
            {
                "type": "too_short",
                "loc": ("items",),
                "ctx": {"field_type": "Tuple", "min_length": 1, "actual_length": 0},
                "input": [],
            },
        ],
    )

    report = diagnostic_from_validation_error(
        error,
        payload,
        protected_field_names=frozenset({"tool_call_id"}),
    )

    assert report.correction_paths == ()
    assert report.issues[0].authority is ResponseIssueAuthority.CONTROLLER
    assert report.issues[1].authority is ResponseIssueAuthority.MODEL
