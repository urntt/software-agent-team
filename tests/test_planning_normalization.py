"""Planning proposal normalization and schema-projection behavior."""

from __future__ import annotations

import json

import pytest
from planning_factories import proposal_body, proposal_response, request
from pydantic import ValidationError

import software_agent_team.planning as planning
from software_agent_team.planning import (
    PlanningDecisionAuthority,
    PlanningDecisionProvenance,
    PlanningDecisionProvenanceKind,
    PlanningError,
    PlanningModelResponse,
    PlanningResponseKind,
)


def test_response_normalizer_canonicalizes_only_safe_presentation_variants() -> None:
    payload = proposal_response().model_dump(mode="json")
    payload.pop("kind")
    payload["proposal"]["tasks"][0]["expected_paths"] = [
        " tests/ ",
        "./src//link_checker.py",
    ]
    payload["proposal"]["agents"][0]["workspace_scope"] = "repository/"
    original = json.loads(json.dumps(payload))

    normalized, changes = planning._normalize_planning_response_payload(payload)
    parsed = PlanningModelResponse.model_validate(normalized)

    assert payload == original
    assert parsed.kind is PlanningResponseKind.PROPOSAL
    assert parsed.proposal is not None
    assert parsed.proposal.tasks[0].expected_paths == (
        "tests",
        "src/link_checker.py",
    )
    assert parsed.proposal.agents[0].workspace_scope == "repository"
    assert changes == (
        "inferred response kind as proposal",
        "canonicalized proposal.tasks[0].expected_paths[0]",
        "canonicalized proposal.tasks[0].expected_paths[1]",
        "canonicalized proposal.agents[0].workspace_scope",
    )


def test_response_normalizer_uses_parallel_requirement_ids_and_disposition() -> None:
    payload = proposal_response().model_dump(mode="json")
    proposal_payload = payload["proposal"]
    proposal_payload["requirements"][0] = (
        "REQ_SCAN: REQ_SCAN: Scan Markdown files below a selected path."
    )
    proposal_payload["requirements"][1] = (
        "REQ_SCAN: Report broken local links with source locations."
    )
    target_users = proposal_payload["product_definition"]["target_users"]
    target_users.update(
        {
            "disposition": "not_material",
            "source": "planner",
            "rationale": "The approved throwaway has no material audience.",
            "requirement_ids": ["REQ_SCAN"],
            "criterion_ids": ["AC_SCAN"],
            "decision_ids": ["DECISION_TEAM"],
        }
    )
    original = json.loads(json.dumps(payload))

    normalized, changes = planning._normalize_planning_response_payload(payload)
    parsed = PlanningModelResponse.model_validate(normalized)

    assert payload == original
    assert parsed.proposal is not None
    assert parsed.proposal.requirements[0] == (
        "Scan Markdown files below a selected path."
    )
    assert parsed.proposal.requirements[1].startswith("REQ_SCAN:")
    normalized_target = parsed.proposal.product_definition
    assert normalized_target is not None
    assert not normalized_target.target_users.requirement_ids
    assert not normalized_target.target_users.criterion_ids
    assert not normalized_target.target_users.decision_ids
    assert changes[:2] == (
        "removed redundant stable ID prefix from proposal.requirements[0]",
        (
            "removed downstream references from not-material "
            "proposal.product_definition.target_users"
        ),
    )


def test_response_normalizer_compiles_atomic_requirements_without_mutating_raw() -> (
    None
):
    payload = proposal_response().model_dump(mode="json")
    proposal_payload = payload["proposal"]
    descriptions = proposal_payload["requirements"]
    requirement_ids = proposal_payload["requirement_ids"]
    proposal_payload["requirements"] = [
        {
            "id": requirement_id,
            "description": f"{requirement_id}: {description}",
        }
        for requirement_id, description in zip(
            requirement_ids,
            descriptions,
            strict=True,
        )
    ]
    proposal_payload["requirement_ids"] = ["REQ_CONFLICTING_REDUNDANT_FIELD"]
    original = json.loads(json.dumps(payload))

    normalized, changes = planning._normalize_planning_response_payload(payload)
    parsed = PlanningModelResponse.model_validate(normalized)

    assert payload == original
    assert parsed.proposal is not None
    assert parsed.proposal.requirement_ids == tuple(requirement_ids)
    assert parsed.proposal.requirements == tuple(descriptions)
    assert changes == (
        (
            "compiled atomic proposal.requirements into canonical descriptions "
            "and stable IDs"
        ),
        "removed redundant stable ID prefix from proposal.requirements[0]",
        "removed redundant stable ID prefix from proposal.requirements[1]",
    )


def test_response_normalizer_rejects_mixed_requirement_shapes() -> None:
    payload = proposal_response().model_dump(mode="json")
    payload["proposal"]["requirements"] = [
        {"id": "REQ_SCAN", "description": "Scan Markdown files."},
        "Report broken links.",
    ]

    with pytest.raises(PlanningError, match="cannot be mixed"):
        planning._normalize_planning_response_payload(payload)


def test_response_normalizer_compiles_atomic_assumptions_without_mutating_raw() -> None:
    payload = proposal_response().model_dump(mode="json")
    proposal_payload = payload["proposal"]
    statements = [
        "Use one single-process scan.",
        "Keep the first implementation local and synchronous.",
    ]
    proposal_payload["assumptions"] = [
        {
            "statement": statement,
            "decision_id": "DECISION_SCAN_STRUCTURE",
        }
        for statement in statements
    ]
    proposal_payload["assumption_decision_ids"] = ["DECISION_CONFLICTING"]
    original = json.loads(json.dumps(payload))

    normalized, changes = planning._normalize_planning_response_payload(payload)
    parsed = PlanningModelResponse.model_validate(normalized)

    assert payload == original
    assert parsed.proposal is not None
    assert parsed.proposal.assumptions == tuple(statements)
    assert parsed.proposal.assumption_decision_ids == (
        "DECISION_SCAN_STRUCTURE",
        "DECISION_SCAN_STRUCTURE",
    )
    assert changes == (
        "compiled atomic proposal.assumptions into canonical statements "
        "and autonomous decision references",
    )


def test_response_normalizer_rejects_mixed_assumption_shapes() -> None:
    payload = proposal_response().model_dump(mode="json")
    payload["proposal"]["assumptions"] = [
        {
            "statement": "Use one single-process scan.",
            "decision_id": "DECISION_SCAN_STRUCTURE",
        },
        "Keep the first implementation local.",
    ]

    with pytest.raises(PlanningError, match="cannot be mixed"):
        planning._normalize_planning_response_payload(payload)


def test_current_proposal_context_uses_atomic_relations_without_rewriting_body() -> (
    None
):
    body = proposal_body()
    canonical = body.model_dump(mode="json")

    projected = planning._planning_proposal_body_for_model(body)

    assert body.model_dump(mode="json") == canonical
    assert "requirement_ids" not in projected
    assert projected["requirements"] == [
        {"id": requirement_id, "description": description}
        for requirement_id, description in zip(
            body.requirement_ids,
            body.requirements,
            strict=True,
        )
    ]
    assert "assumption_decision_ids" not in projected
    assert projected["assumptions"] == [
        {"statement": statement, "decision_id": decision_id}
        for statement, decision_id in zip(
            body.assumptions,
            body.assumption_decision_ids,
            strict=True,
        )
    ]


def test_response_normalizer_canonicalizes_unambiguous_decision_tokens() -> None:
    payload = proposal_response().model_dump(mode="json")
    decisions = payload["proposal"]["decisions"]
    canonical_ids = [decision["id"] for decision in decisions]
    lowercase_ids = [
        "DECISION_" + decision_id.removeprefix("DECISION_").lower()
        for decision_id in canonical_ids
    ]
    for decision, lowercase_id in zip(decisions, lowercase_ids, strict=True):
        decision["id"] = lowercase_id
    payload["proposal"]["assumption_decision_ids"] = ["DECISION_scan_structure"]
    original = json.loads(json.dumps(payload))

    normalized, changes = planning._normalize_planning_response_payload(payload)
    parsed = PlanningModelResponse.model_validate(normalized)

    assert payload == original
    assert parsed.proposal is not None
    assert [decision.id for decision in parsed.proposal.decisions] == canonical_ids
    assert parsed.proposal.assumption_decision_ids == ("DECISION_SCAN_STRUCTURE",)
    assert len(changes) == len(canonical_ids) + 1
    assert changes[0] == (
        "canonicalized proposal.decisions[0].id as DECISION_ACCEPTANCE"
    )
    assert changes[-1] == (
        "canonicalized proposal.assumption_decision_ids[0] as DECISION_SCAN_STRUCTURE"
    )


def test_response_normalizer_compiles_decision_authority_and_legacy_sources() -> None:
    payload = proposal_response().model_dump(mode="json")
    proposal_payload = payload["proposal"]
    for decision in proposal_payload["decisions"]:
        decision.pop("provenance")
        decision.pop("authority")
    proposal_payload["decisions"][1]["authority"] = "user"
    proposal_payload["decisions"][:0] = [
        {
            "id": "DECISION_TARGET_USERS",
            "category": "product_requirement",
            "authority": "user",
            "summary": "A developer is the intended user.",
            "rationale": "This interpretation came from the product definition.",
        },
        {
            "id": "DECISION_WORKFLOW",
            "category": "product_requirement",
            "authority": "user",
            "summary": "The user checks Markdown links.",
            "rationale": "This interpretation came from the product definition.",
        },
    ]
    proposal_payload["decisions"].append(
        {
            "id": "DECISION_NO_REMOTE_FETCH",
            "category": "privacy_or_data",
            "summary": "without fetching remote URLs",
            "rationale": "The request states this boundary directly.",
        }
    )
    definition = proposal_payload["product_definition"]
    definition["target_users"]["decision_ids"] = ["DECISION_TARGET_USERS"]
    definition["primary_workflow"]["decision_ids"] = ["DECISION_WORKFLOW"]
    definition["delivery_maturity"]["decision_ids"] = ["DECISION_DELIVERY"]
    original = json.loads(json.dumps(payload))

    normalized, changes = planning._normalize_planning_response_payload(
        payload,
        user_inputs=(request().source_request,),
    )
    parsed = PlanningModelResponse.model_validate(normalized)

    assert payload == original
    assert parsed.proposal is not None
    assert [item.id for item in parsed.proposal.decisions] == [
        "DECISION_ACCEPTANCE",
        "DECISION_DELIVERY",
        "DECISION_TEAM",
        "DECISION_MODEL_ROUTE",
        "DECISION_SCAN_STRUCTURE",
        "DECISION_NO_REMOTE_FETCH",
    ]
    assert all(item.provenance is not None for item in parsed.proposal.decisions)
    assert parsed.proposal.decisions[1].authority is (
        PlanningDecisionAuthority.PLANNER_PROPOSAL
    )
    assert (
        parsed.proposal.decisions[1].provenance.kind
        is PlanningDecisionProvenanceKind.PLANNER_RECOMMENDATION
    )
    assert not parsed.proposal.product_definition.target_users.decision_ids
    assert not parsed.proposal.product_definition.primary_workflow.decision_ids
    assert not parsed.proposal.product_definition.delivery_maturity.decision_ids
    assert parsed.proposal.decisions[-1].provenance == PlanningDecisionProvenance(
        kind=PlanningDecisionProvenanceKind.EXPLICIT_INPUT,
        source="without fetching remote URLs",
    )
    assert any("compiled proposal.decisions[3].authority" in item for item in changes)
    assert any(
        "removed redundant direct-input decision DECISION_TARGET_USERS" in item
        for item in changes
    )


def test_response_normalizer_removes_current_direct_product_duplicate() -> None:
    payload = proposal_response().model_dump(mode="json")
    proposal_payload = payload["proposal"]
    maturity = proposal_payload["product_definition"]["delivery_maturity"]
    assert maturity["disposition"] == "explicit_input"
    assert maturity["decision_ids"] == []
    proposal_payload["decisions"].insert(
        0,
        {
            "id": "DECISION_DUPLICATE_MATURITY",
            "category": "delivery",
            "authority": "user",
            "provenance": {
                "kind": "explicit_input",
                "source": maturity["source"],
            },
            "summary": "Paraphrased delivery maturity.",
            "rationale": "The product definition already carries this fact.",
        },
    )
    original = json.loads(json.dumps(payload))

    normalized, changes = planning._normalize_planning_response_payload(payload)
    parsed = PlanningModelResponse.model_validate(normalized)

    assert payload == original
    assert parsed.proposal is not None
    assert "DECISION_DUPLICATE_MATURITY" not in {
        item.id for item in parsed.proposal.decisions
    }
    assert changes == (
        "compiled proposal.decisions[0].authority from category delivery",
        "compiled proposal.decisions[0].summary from exact direct-input source",
        "removed redundant direct-input decision DECISION_DUPLICATE_MATURITY "
        "from proposal.decisions[0]",
    )


def test_response_normalizer_preserves_same_source_independent_user_decision() -> None:
    payload = proposal_response().model_dump(mode="json")
    proposal_payload = payload["proposal"]
    operations = proposal_payload["product_definition"]["operational_expectations"]
    operations.update(
        {
            "statement": "Do not fetch remote URLs.",
            "disposition": "explicit_input",
            "source": "without fetching remote URLs",
            "rationale": "The user supplied this operational boundary.",
            "criterion_ids": ["AC_SCAN"],
            "decision_ids": [],
        }
    )
    proposal_payload["decisions"].append(
        {
            "id": "DECISION_PRIVACY_BOUNDARY",
            "category": "privacy_or_data",
            "provenance": {
                "kind": "explicit_input",
                "source": "without fetching remote URLs",
            },
            "summary": "without fetching remote URLs",
            "rationale": "The same words also establish a data-access boundary.",
        }
    )
    original = json.loads(json.dumps(payload))

    normalized, changes = planning._normalize_planning_response_payload(
        payload,
        user_inputs=(request().source_request,),
    )
    parsed = PlanningModelResponse.model_validate(normalized)

    assert payload == original
    assert parsed.proposal is not None
    privacy_decision = next(
        item
        for item in parsed.proposal.decisions
        if item.id == "DECISION_PRIVACY_BOUNDARY"
    )
    assert privacy_decision.authority is PlanningDecisionAuthority.USER
    assert privacy_decision.provenance == PlanningDecisionProvenance(
        kind=PlanningDecisionProvenanceKind.EXPLICIT_INPUT,
        source="without fetching remote URLs",
    )
    assert not any(
        "removed redundant direct-input decision DECISION_PRIVACY_BOUNDARY" in item
        for item in changes
    )


def test_response_normalizer_leaves_case_collisions_for_strict_rejection() -> None:
    payload = proposal_response().model_dump(mode="json")
    payload["proposal"]["decisions"][0]["id"] = "DECISION_COLLISION"
    payload["proposal"]["decisions"][1]["id"] = "DECISION_collision"

    normalized, changes = planning._normalize_planning_response_payload(payload)

    assert normalized["proposal"]["decisions"][0]["id"] == "DECISION_COLLISION"
    assert normalized["proposal"]["decisions"][1]["id"] == "DECISION_collision"
    assert changes == ()
    with pytest.raises(ValidationError, match="String should match pattern"):
        PlanningModelResponse.model_validate(normalized)


def test_response_normalizer_leaves_unsafe_paths_for_strict_rejection() -> None:
    payload = proposal_response().model_dump(mode="json")
    payload["proposal"]["tasks"][0]["expected_paths"] = ["../secret/"]

    normalized, changes = planning._normalize_planning_response_payload(payload)

    assert normalized["proposal"]["tasks"][0]["expected_paths"] == ["../secret/"]
    assert changes == ()
    with pytest.raises(ValidationError, match="canonical safe relative POSIX paths"):
        PlanningModelResponse.model_validate(normalized)
