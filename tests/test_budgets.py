"""Tests for explicit Agent resource and estimated-cost budgets."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from software_agent_team.budgets import AgentBudget, ModelPricing


def test_model_pricing_estimates_token_cost() -> None:
    pricing = ModelPricing(
        model="provider/model",
        input_cost_per_million_usd="2.50",
        output_cost_per_million_usd="10.00",
    )

    assert pricing.estimate_cost(input_tokens=100_000, output_tokens=20_000) == (
        Decimal("0.45")
    )


def test_agent_budget_requires_every_aggregate_ceiling() -> None:
    budget = AgentBudget(
        max_calls=14,
        max_input_tokens=1_000_000,
        max_output_tokens=200_000,
        max_agent_duration_seconds=7200,
        max_estimated_cost_usd="25.00",
    )

    assert budget.max_calls == 14
    assert budget.max_estimated_cost_usd == Decimal("25.00")


@pytest.mark.parametrize(
    "field",
    (
        "max_calls",
        "max_input_tokens",
        "max_output_tokens",
        "max_agent_duration_seconds",
        "max_estimated_cost_usd",
    ),
)
def test_agent_budget_rejects_missing_or_zero_ceiling(field: str) -> None:
    payload: dict[str, object] = {
        "max_calls": 14,
        "max_input_tokens": 1_000_000,
        "max_output_tokens": 200_000,
        "max_agent_duration_seconds": 7200,
        "max_estimated_cost_usd": "25.00",
    }
    payload[field] = 0

    with pytest.raises(ValidationError):
        AgentBudget.model_validate(payload)
