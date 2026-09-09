"""Tests for field-targeted semantic response correction."""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from software_agent_team.response_corrections import (
    ResponseFailureClass,
    ResponseIssueAuthority,
    ResponseIssueSubject,
    ResponseIssueSubjectKind,
    ResponseValidationDiagnostic,
    ResponseValidationIssue,
    SemanticCorrectionCandidate,
    SemanticCorrectionCandidateSlot,
    SemanticCorrectionOutcome,
    SemanticCorrectionPlan,
    SemanticCorrectionSubmissionError,
    apply_semantic_correction,
    apply_semantic_correction_with_evidence,
    attach_semantic_correction_candidates,
    build_semantic_correction_plan,
    correction_outcome,
    correction_prompt,
    deterministically_remove_forbidden_fields,
    diagnostic_from_invariant,
    diagnostic_from_message,
    diagnostic_from_validation_error,
    semantic_correction_schema,
    semantic_correction_slot_handle,
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


def correction_submission(
    plan: SemanticCorrectionPlan,
    replacements: dict[str, object],
) -> dict[str, object]:
    """Bind test values to the exact controller-issued correction slots."""

    return {
        "replacements": [
            {
                "slot_handle": semantic_correction_slot_handle(
                    plan.evidence.base_response_sha256,
                    path,
                ),
                "replacement_value": value,
            }
            for path, value in replacements.items()
        ]
    }


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
        correction_submission(plan, {"/summary": "valid summary"}),
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
            correction_submission(plan, {"/summary": "valid"}),
            plan,
        )
    except ValueError as error:
        assert "value count differs: expected 2, received 1" in str(error)
    else:
        raise AssertionError("incomplete semantic correction was accepted")


def test_correction_rejects_duplicate_unknown_cross_plan_and_positional_payloads() -> (
    None
):
    payload: dict[str, object] = {
        "summary": "",
        "tasks": [],
        "preserved": "keep",
    }
    plan = build_semantic_correction_plan(payload, diagnostic(payload))
    assert plan is not None
    valid = correction_submission(
        plan,
        {
            "/summary": "valid",
            "/tasks": ["TASK_ONE"],
        },
    )
    records = valid["replacements"]
    assert isinstance(records, list)

    other_payload = {**payload, "preserved": "different base"}
    other_plan = build_semantic_correction_plan(
        other_payload,
        diagnostic(other_payload),
    )
    assert other_plan is not None

    invalid_payloads = (
        {"replacements": [records[0], records[0]]},
        {
            "replacements": [
                records[0],
                {
                    "slot_handle": "slot_ffffffffffffffff",
                    "replacement_value": ["TASK_ONE"],
                },
            ]
        },
        correction_submission(
            other_plan,
            {
                "/summary": "valid",
                "/tasks": ["TASK_ONE"],
            },
        ),
        {"replacement_values": ["valid", ["TASK_ONE"]]},
    )

    for invalid in invalid_payloads:
        with pytest.raises((ValidationError, ValueError)):
            apply_semantic_correction(invalid, plan)
        assert plan.base_payload == payload


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

    assert "TARGETED_SEMANTIC_CORRECTION_SLOTS_V2" in prompt
    assert "Return only the supplied opaque handles, not target paths" in prompt
    assert "derived parent error must not be requested" not in prompt
    assert '"target_path": "/items/0/id"' in prompt
    assert '"target_path": "/items/1/id"' in prompt
    assert '"replacements"' in schema
    assert '"slot_handle"' in schema
    assert '"replacement_value"' in schema
    assert '"minItems": 2' in schema
    assert '"maxItems": 2' in schema
    assert '"path"' not in schema
    assert '"kind"' not in schema
    assert '"base_response_sha256"' not in schema

    tool_prompt = correction_prompt(plan, submission_tool="sat_submit_artifact")
    assert "Call `sat_submit_artifact` exactly once" in tool_prompt
    assert "Do not serialize the values in assistant text" in tool_prompt
    assert plan.evidence.base_response_sha256 not in tool_prompt

    corrected = apply_semantic_correction(
        correction_submission(
            plan,
            {
                "/items/1/id": "SECOND",
                "/items/0/id": "FIRST",
            },
        ),
        plan,
    )
    assert corrected == {
        "items": [{"id": "FIRST"}, {"id": "SECOND"}],
        "preserved": "keep",
    }

    with pytest.raises(
        SemanticCorrectionSubmissionError, match="unique typed slot/value"
    ):
        apply_semantic_correction(
            {
                "kind": "semantic_correction_v2",
                "base_response_sha256": plan.evidence.base_response_sha256,
                **correction_submission(
                    plan,
                    {
                        "/items/0/id": "FIRST",
                        "/items/1/id": "SECOND",
                    },
                ),
            },
            plan,
        )


def test_correction_prompt_projects_each_target_value_schema() -> None:
    payload: dict[str, object] = {
        "items": [{"id": "invalid"}],
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
                message="ID must use the stable uppercase form",
                authority=ResponseIssueAuthority.MODEL,
            ),
        ),
        correction_paths=("/items/0/id",),
    )
    plan = build_semantic_correction_plan(payload, report)
    assert plan is not None
    response_schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "$ref": "#/$defs/Item",
                },
            },
        },
        "$defs": {
            "Item": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "pattern": "^[A-Z][A-Z0-9_]+$",
                    },
                },
            },
        },
    }

    prompt = correction_prompt(plan, response_schema=response_schema)
    target_json = prompt.split("TARGET_SLOTS_AND_ERRORS\n", maxsplit=1)[1].split(
        "\nCORRECTION_SCHEMA_JSON",
        maxsplit=1,
    )[0]
    slots = json.loads(target_json)
    slot_handle = semantic_correction_slot_handle(
        plan.evidence.base_response_sha256,
        "/items/0/id",
    )

    assert slots == [
        {
            "slot_handle": slot_handle,
            "target_path": "/items/0/id",
            "errors": [
                {
                    "code": "invalid_id",
                    "invariant_id": "invalid_id",
                    "subjects": [],
                    "message": "ID must use the stable uppercase form",
                }
            ],
            "value_schema": {
                "type": "string",
                "pattern": "^[A-Z][A-Z0-9_]+$",
            },
        }
    ]

    schema = semantic_correction_schema(plan, response_schema=response_schema)
    replacement_schema = schema["properties"]["replacements"]
    assert replacement_schema["items"]["oneOf"] == [
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "slot_handle": {"type": "string", "const": slot_handle},
                "replacement_value": {
                    "type": "string",
                    "pattern": "^[A-Z][A-Z0-9_]+$",
                },
            },
            "required": ["slot_handle", "replacement_value"],
        }
    ]
    assert replacement_schema["minItems"] == 1
    assert replacement_schema["maxItems"] == 1


def test_controller_candidate_handles_replace_exact_values_without_model_bytes() -> (
    None
):
    payload: dict[str, object] = {
        "summary": "",
        "tasks": ["TASK_ONE"],
        "preserved": "keep this exact value",
    }
    plan = build_semantic_correction_plan(payload, diagnostic(payload))
    assert plan is not None
    candidate = SemanticCorrectionCandidate(
        handle="evidence_0123456789abcdef",
        replacement_value="exact\ncontroller-owned output",
        source="attempt 1 tool-004 read result",
    )
    bound = attach_semantic_correction_candidates(
        plan,
        (
            SemanticCorrectionCandidateSlot(
                target_path="/summary",
                candidates=(candidate,),
            ),
        ),
    )

    prompt = correction_prompt(bound)
    schema = semantic_correction_schema(bound)
    replacement_schema = schema["properties"]["replacements"]
    slot_handle = semantic_correction_slot_handle(
        bound.evidence.base_response_sha256,
        "/summary",
    )
    assert "EVIDENCE_CANDIDATE_CATALOG" in prompt
    assert "exact\\ncontroller-owned output" in prompt
    assert replacement_schema["items"]["oneOf"] == [
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "slot_handle": {"type": "string", "const": slot_handle},
                "replacement_value": {
                    "type": "string",
                    "enum": ["evidence_0123456789abcdef"],
                },
            },
            "required": ["slot_handle", "replacement_value"],
        }
    ]

    application = apply_semantic_correction_with_evidence(
        correction_submission(
            bound,
            {"/summary": "evidence_0123456789abcdef"},
        ),
        bound,
    )
    assert application.payload == {
        "summary": "exact\ncontroller-owned output",
        "tasks": ["TASK_ONE"],
        "preserved": "keep this exact value",
    }
    assert application.normalizations == (
        "bound controller evidence candidate evidence_0123456789abcdef to /summary",
    )

    with pytest.raises(ValueError, match="not authorized for correction slot"):
        apply_semantic_correction(
            correction_submission(
                bound,
                {"/summary": "evidence_ffffffffffffffff"},
            ),
            bound,
        )


def selection_plan() -> SemanticCorrectionPlan:
    payload: dict[str, object] = {"first": "bad", "second": "bad", "keep": [1, 2]}
    diagnostic = diagnostic_from_invariant(
        payload,
        failure_class=ResponseFailureClass.EVIDENCE_GROUNDING,
        authority=ResponseIssueAuthority.MODEL,
        code="review_evidence_grounding",
        invariant_id="review_evidence_fragment_unmatched",
        subjects=(),
        message="Select evidence for each claim.",
        paths=("/first", "/second"),
    )
    plan = build_semantic_correction_plan(payload, diagnostic)
    assert plan is not None
    return attach_semantic_correction_candidates(
        plan,
        tuple(
            SemanticCorrectionCandidateSlot(
                target_path=path,
                candidates=(
                    SemanticCorrectionCandidate(
                        handle=f"evidence_{index:016x}",
                        replacement_value=f"exact evidence {index}",
                        source=f"tool-{index:03d}",
                    ),
                ),
            )
            for index, path in enumerate(plan.evidence.target_paths, 1)
        ),
    )


def test_mixed_candidate_selection_stages_only_verified_bindings() -> None:
    plan = selection_plan()
    original = json.dumps(plan.base_payload, sort_keys=True)
    with pytest.raises(SemanticCorrectionSubmissionError) as caught:
        apply_semantic_correction(
            correction_submission(
                plan,
                {
                    "/second": "evidence_0000000000000001",
                    "/first": "evidence_0000000000000001",
                },
            ),
            plan,
        )
    pending = caught.value.recovery_plan
    assert pending is not None
    assert pending.evidence.target_paths == ("/second",)
    assert pending.base_payload == {
        "first": "exact evidence 1",
        "second": "bad",
        "keep": [1, 2],
    }
    assert pending.candidate_slots == (plan.candidate_slots[1],)
    assert len(caught.value.normalizations) == 1
    assert all(
        issue.authority is ResponseIssueAuthority.MODEL
        for issue in caught.value.diagnostic.issues
    )
    # Repeating a wrong selection cannot buy another call or erase staged work.
    with pytest.raises(SemanticCorrectionSubmissionError) as repeated:
        apply_semantic_correction(
            correction_submission(pending, {"/second": "evidence_ffffffffffffffff"}),
            pending,
        )
    assert repeated.value.recovery_plan is None
    assert repeated.value.normalizations == ()
    corrected = apply_semantic_correction(
        correction_submission(pending, {"/second": "evidence_0000000000000002"}),
        pending,
    )
    assert corrected == {
        "first": "exact evidence 1",
        "second": "exact evidence 2",
        "keep": [1, 2],
    }
    assert json.dumps(plan.base_payload, sort_keys=True) == original


@pytest.mark.parametrize(
    "defect",
    [
        "unknown_values",
        "wrong_slots",
        "duplicate_slot",
        "unknown_slot",
        "missing_slot",
        "shape",
    ],
)
def test_invalid_candidate_submission_fails_typed_without_guess_or_retry(
    defect: str,
) -> None:
    plan = selection_plan()
    payload = correction_submission(
        plan,
        {"/first": "evidence_0000000000000001", "/second": "evidence_0000000000000002"},
    )
    replacements = payload["replacements"]
    if defect == "unknown_values":
        for item in replacements:
            item["replacement_value"] = "evidence_ffffffffffffffff"
    elif defect == "wrong_slots":
        replacements[0]["replacement_value"], replacements[1]["replacement_value"] = (
            replacements[1]["replacement_value"],
            replacements[0]["replacement_value"],
        )
    elif defect == "duplicate_slot":
        replacements[1]["slot_handle"] = replacements[0]["slot_handle"]
    elif defect == "unknown_slot":
        replacements[1]["slot_handle"] = "slot_ffffffffffffffff"
    elif defect == "missing_slot":
        replacements.pop()
    else:
        payload = {"replacements": "not an array"}
    with pytest.raises(SemanticCorrectionSubmissionError) as caught:
        apply_semantic_correction(payload, plan)
    assert caught.value.recovery_plan is None
    assert caught.value.normalizations == ()
    assert caught.value.diagnostic.failure_class is ResponseFailureClass.SEMANTIC_SCHEMA
    assert plan.base_payload == {"first": "bad", "second": "bad", "keep": [1, 2]}


def test_unvalidated_free_form_sibling_cannot_authorize_selection_recovery() -> None:
    original = selection_plan()
    plan = SemanticCorrectionPlan(
        base_payload=original.base_payload,
        diagnostic=original.diagnostic,
        evidence=original.evidence,
        candidate_slots=(original.candidate_slots[0],),
    )
    with pytest.raises(SemanticCorrectionSubmissionError) as caught:
        apply_semantic_correction(
            correction_submission(
                plan, {"/first": "unknown", "/second": {"arbitrary": "unvalidated"}}
            ),
            plan,
        )
    assert caught.value.recovery_plan is None
    assert caught.value.normalizations == ()
    assert plan.base_payload["second"] == "bad"


def test_unreachable_controller_pointer_is_not_a_model_submission_error() -> None:
    original = selection_plan()
    plan = SemanticCorrectionPlan(
        base_payload={"first": []},
        diagnostic=original.diagnostic,
        evidence=original.evidence.model_copy(update={"target_paths": ("/first/9",)}),
    )
    with pytest.raises(ValueError, match="index is invalid") as caught:
        apply_semantic_correction(
            correction_submission(plan, {"/first/9": "value"}), plan
        )
    assert not isinstance(caught.value, SemanticCorrectionSubmissionError)


def test_candidate_schema_does_not_invent_cross_slot_distinctness() -> None:
    payload: dict[str, object] = {
        "summary": "bad one",
        "tasks": ["TASK_ONE"],
        "preserved": "bad two",
    }
    issue = diagnostic_from_invariant(
        payload,
        failure_class=ResponseFailureClass.EVIDENCE_GROUNDING,
        authority=ResponseIssueAuthority.MODEL,
        code="review_evidence_grounding",
        invariant_id="review_evidence_fragment_unmatched",
        subjects=(),
        message="two unrelated claims need grounding",
        paths=("/summary", "/preserved"),
    )
    plan = build_semantic_correction_plan(payload, issue)
    assert plan is not None
    shared = SemanticCorrectionCandidate(
        handle="evidence_0123456789abcdef",
        replacement_value="shared eligible observation",
        source="one eligible result",
    )
    bound = attach_semantic_correction_candidates(
        plan,
        tuple(
            SemanticCorrectionCandidateSlot(
                target_path=target_path,
                candidates=(shared,),
            )
            for target_path in plan.evidence.target_paths
        ),
    )
    values_schema = semantic_correction_schema(bound)["properties"]["replacements"]

    assert "uniqueItems" not in values_schema
    assert apply_semantic_correction(
        correction_submission(
            bound,
            {
                "/summary": "evidence_0123456789abcdef",
                "/preserved": "evidence_0123456789abcdef",
            },
        ),
        bound,
    ) == {
        "summary": "shared eligible observation",
        "tasks": ["TASK_ONE"],
        "preserved": "shared eligible observation",
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


def test_outcome_continues_after_sibling_reduction_then_stops_cycle() -> None:
    payload: dict[str, object] = {
        "first": "duplicate evidence",
        "second": "duplicate evidence",
        "preserved": "keep",
    }
    initial = diagnostic_from_invariant(
        payload,
        failure_class=ResponseFailureClass.SEMANTIC_SCHEMA,
        authority=ResponseIssueAuthority.MODEL,
        code="value_error",
        invariant_id="review_evidence_fragments_distinct",
        subjects=(),
        message="criterion boundary checks require distinct evidence fragments",
        paths=("/first", "/second"),
    )
    plan = build_semantic_correction_plan(payload, initial)
    assert plan is not None
    reduced_payload = {
        **payload,
        "first": "independent evidence",
    }
    reduced = diagnostic_from_invariant(
        reduced_payload,
        failure_class=ResponseFailureClass.SEMANTIC_SCHEMA,
        authority=ResponseIssueAuthority.MODEL,
        code="value_error",
        invariant_id="review_evidence_fragments_distinct",
        subjects=(),
        message="criterion boundary checks require distinct evidence fragments",
        paths=("/second",),
    )

    assert initial.fingerprint != reduced.fingerprint
    assert (
        correction_outcome(
            plan,
            reduced,
            seen_fingerprints=frozenset({initial.fingerprint}),
        )
        is SemanticCorrectionOutcome.IMPROVED
    )

    narrowed_plan = build_semantic_correction_plan(reduced_payload, reduced)
    assert narrowed_plan is not None
    assert (
        correction_outcome(
            narrowed_plan,
            reduced,
            seen_fingerprints=frozenset({initial.fingerprint, reduced.fingerprint}),
        )
        is SemanticCorrectionOutcome.NO_IMPROVEMENT
    )


def test_outcome_rejects_a_schema_regression_in_the_same_authority_slot() -> None:
    payload: dict[str, object] = {
        "summary": "candidate",
        "tasks": ["TASK_ONE"],
        "preserved": "keep",
    }
    first = diagnostic_from_invariant(
        payload,
        failure_class=ResponseFailureClass.EVIDENCE_GROUNDING,
        authority=ResponseIssueAuthority.MODEL,
        code="review_evidence_grounding",
        invariant_id="review_evidence_fragments_distinct",
        subjects=(),
        message="the evidence fragments must be distinct",
        paths=("/summary",),
    )
    plan = build_semantic_correction_plan(payload, first)
    assert plan is not None
    regressed_payload = {**payload, "summary": []}
    regressed = diagnostic_from_invariant(
        regressed_payload,
        failure_class=ResponseFailureClass.SEMANTIC_SCHEMA,
        authority=ResponseIssueAuthority.MODEL,
        code="tuple_type",
        invariant_id="tuple_type",
        subjects=(),
        message="Input should be a valid tuple",
        paths=("/summary",),
    )

    assert first.fingerprint != regressed.fingerprint
    assert (
        correction_outcome(
            plan,
            regressed,
            seen_fingerprints=frozenset({first.fingerprint}),
        )
        is SemanticCorrectionOutcome.NO_IMPROVEMENT
    )


def test_outcome_allows_a_more_specific_constraint_in_the_same_slot() -> None:
    payload: dict[str, object] = {
        "summary": [],
        "tasks": ["TASK_ONE"],
        "preserved": "keep",
    }
    first = diagnostic_from_invariant(
        payload,
        failure_class=ResponseFailureClass.SEMANTIC_SCHEMA,
        authority=ResponseIssueAuthority.MODEL,
        code="string_type",
        invariant_id="string_type",
        subjects=(),
        message="Input should be a valid string",
        paths=("/summary",),
    )
    plan = build_semantic_correction_plan(payload, first)
    assert plan is not None
    refined_payload = {**payload, "summary": "duplicate evidence"}
    refined = diagnostic_from_invariant(
        refined_payload,
        failure_class=ResponseFailureClass.SEMANTIC_SCHEMA,
        authority=ResponseIssueAuthority.MODEL,
        code="value_error",
        invariant_id="review_evidence_fragments_distinct",
        subjects=(),
        message="the evidence fragments must be distinct",
        paths=("/summary",),
    )

    assert (
        correction_outcome(
            plan,
            refined,
            seen_fingerprints=frozenset({first.fingerprint}),
        )
        is SemanticCorrectionOutcome.IMPROVED
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
        correction_submission(plan, {"/summary": "valid"}),
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
