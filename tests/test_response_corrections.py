"""Tests for field-targeted semantic response correction."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from software_agent_team.response_corrections import (
    ResponseFailureClass,
    ResponseIssueAuthority,
    SemanticCorrectionOutcome,
    apply_semantic_correction,
    build_semantic_correction_plan,
    correction_outcome,
    deterministically_remove_forbidden_fields,
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
            "kind": "semantic_correction_v1",
            "base_response_sha256": semantic_payload_sha256(payload),
            "replacements": [{"path": "/summary", "value": "valid summary"}],
        },
        plan,
    )

    assert corrected == {
        "summary": "valid summary",
        "tasks": ["TASK_ONE"],
        "preserved": "keep this exact value",
    }


def test_correction_rejects_an_unauthorized_or_incomplete_path_set() -> None:
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
                "kind": "semantic_correction_v1",
                "base_response_sha256": semantic_payload_sha256(payload),
                "replacements": [
                    {"path": "/summary", "value": "valid"},
                    {"path": "/preserved", "value": "changed"},
                ],
            },
            plan,
        )
    except ValueError as error:
        assert "paths differ" in str(error)
    else:
        raise AssertionError("unauthorized semantic correction was accepted")


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
