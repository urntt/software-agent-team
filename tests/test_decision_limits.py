"""Contract tests for decision-relevant limit ownership metadata."""

from pathlib import Path

from software_agent_team.decision_limits import (
    DECISION_LIMIT_REGISTRY,
    DecisionLimitCategory,
    DecisionLimitConfigurability,
    DecisionLimitVisibility,
    validate_decision_limit_registry,
)

REPOSITORY_ROOT = Path(__file__).parents[1]


def test_every_registered_limit_has_one_resolvable_authority() -> None:
    validate_decision_limit_registry(REPOSITORY_ROOT)

    assert len({item.id for item in DECISION_LIMIT_REGISTRY}) == len(
        DECISION_LIMIT_REGISTRY
    )
    assert len({item.value.reference for item in DECISION_LIMIT_REGISTRY}) == len(
        DECISION_LIMIT_REGISTRY
    )
    assert {item.category for item in DECISION_LIMIT_REGISTRY} == set(
        DecisionLimitCategory
    )


def test_product_resource_authority_classes_remain_distinct() -> None:
    by_id = {item.id: item for item in DECISION_LIMIT_REGISTRY}

    assert by_id["user.task-cost-usd"].configurability is (
        DecisionLimitConfigurability.USER_PER_TASK
    )
    assert by_id["user.optional-run-deadline"].configurability is (
        DecisionLimitConfigurability.USER_PER_TASK
    )
    assert by_id["model.context-window"].configurability is (
        DecisionLimitConfigurability.AUTO_DISCOVERED
    )
    assert by_id["provider.stream-inactivity"].visibility is (
        DecisionLimitVisibility.ON_TRIGGER
    )
    assert by_id["provider.stream-inactivity"].category is (
        DecisionLimitCategory.INFRASTRUCTURE_GUARD
    )
    assert by_id["provider.stream-inactivity"].configurability is (
        DecisionLimitConfigurability.MAINTAINER_POLICY
    )


def test_evaluation_limits_cannot_be_mistaken_for_product_limits() -> None:
    evaluation = tuple(
        item
        for item in DECISION_LIMIT_REGISTRY
        if item.category is DecisionLimitCategory.CONTROLLED_EXPERIMENT_VARIABLE
    )

    assert evaluation
    assert all(
        item.visibility is DecisionLimitVisibility.EVALUATION_ONLY
        and item.configurability is DecisionLimitConfigurability.CONTROLLED_EVALUATION
        for item in evaluation
    )
