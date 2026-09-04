"""Tests for explicit Agent resource and estimated-cost budgets."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from pydantic import ValidationError

from software_agent_team.budgets import (
    AgentBudget,
    AgentBudgetExceeded,
    AgentBudgetLedger,
    BudgetAuthority,
    ModelPricing,
)


def budget(**updates: object) -> AgentBudget:
    """Return a bounded budget with optional test-specific changes."""

    payload: dict[str, object] = {
        "max_calls": 4,
        "max_input_tokens": 100,
        "max_output_tokens": 50,
        "max_agent_duration_seconds": 10,
        "max_estimated_cost_usd": "1.00",
    }
    payload.update(updates)
    return AgentBudget.model_validate(payload)


def test_model_pricing_estimates_token_cost() -> None:
    pricing = ModelPricing(
        model="provider/model",
        input_cost_per_million_usd="2.50",
        output_cost_per_million_usd="10.00",
    )
    assert pricing.estimate_cost(input_tokens=100_000, output_tokens=20_000) == (
        Decimal("0.45")
    )


def test_model_pricing_reports_unknown_cost_without_inventing_zero() -> None:
    pricing = ModelPricing(model="provider/model")

    assert pricing.estimate_cost(input_tokens=100, output_tokens=20) is None

    with pytest.raises(ValidationError, match="configured together"):
        ModelPricing(
            model="provider/model",
            input_cost_per_million_usd="1",
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


def test_user_task_budget_has_only_one_usd_ceiling() -> None:
    budget = AgentBudget(
        authority=BudgetAuthority.USER_TASK,
        max_estimated_cost_usd="5.00",
    )

    assert budget.max_calls is None
    assert budget.max_input_tokens is None
    assert budget.max_output_tokens is None
    assert budget.max_agent_duration_seconds is None


def test_free_user_task_may_authorize_zero_total_cost() -> None:
    budget = AgentBudget(
        authority=BudgetAuthority.USER_TASK,
        max_estimated_cost_usd="0",
    )

    assert budget.max_estimated_cost_usd == 0


def test_user_task_cost_and_model_prices_are_not_capped_by_legacy_defaults() -> None:
    authorized = AgentBudget(
        authority=BudgetAuthority.USER_TASK,
        max_estimated_cost_usd="10000.01",
    )
    pricing = ModelPricing(
        model="provider/specialized-model",
        input_cost_per_million_usd="10000.01",
        output_cost_per_million_usd="20000.02",
    )

    assert authorized.max_estimated_cost_usd == Decimal("10000.01")
    assert pricing.output_cost_per_million_usd == Decimal("20000.02")


def test_user_task_rejects_evaluation_only_count_limit() -> None:
    with pytest.raises(ValidationError, match="only one USD ceiling"):
        AgentBudget(
            authority=BudgetAuthority.USER_TASK,
            max_calls=10,
            max_estimated_cost_usd="5.00",
        )


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


def test_budget_ledger_enforces_call_limit_before_parallel_launch() -> None:
    ledger = AgentBudgetLedger(budget())

    def reserve(index: int) -> bool:
        try:
            ledger.reserve_call(f"agent_{index}")
        except AgentBudgetExceeded:
            return False
        return True

    with ThreadPoolExecutor(max_workers=8) as executor:
        accepted = tuple(executor.map(reserve, range(8)))

    assert sum(accepted) == 4
    assert ledger.snapshot().calls_started == 4
    assert ledger.snapshot().active_calls == 4


def test_budget_ledger_records_usage_before_post_call_rejection() -> None:
    ledger = AgentBudgetLedger(budget(max_input_tokens=10))
    reservation = ledger.reserve_call("builder")

    with pytest.raises(AgentBudgetExceeded, match="input-token") as captured:
        ledger.complete_call(
            reservation,
            input_tokens=11,
            output_tokens=2,
            duration_ms=500,
            estimated_cost_usd=Decimal("0.25"),
        )

    usage = captured.value.usage
    assert usage.calls_completed == 1
    assert usage.active_calls == 0
    assert usage.input_tokens == 11
    assert usage.known_estimated_cost_usd == Decimal("0.25")


def test_budget_ledger_preserves_unknown_usage_and_price() -> None:
    ledger = AgentBudgetLedger(budget())
    reservation = ledger.reserve_call("reviewer")

    usage = ledger.complete_call(
        reservation,
        input_tokens=None,
        output_tokens=None,
        duration_ms=25,
        estimated_cost_usd=None,
    )

    assert usage.input_tokens == 0
    assert usage.output_tokens == 0
    assert usage.unreported_token_calls == 1
    assert usage.known_estimated_cost_usd == 0
    assert usage.unpriced_calls == 1


def test_budget_ledger_rejects_reusing_a_completed_reservation() -> None:
    ledger = AgentBudgetLedger(budget())
    reservation = ledger.reserve_call("tester")
    ledger.complete_call(
        reservation,
        input_tokens=1,
        output_tokens=1,
        duration_ms=1,
        estimated_cost_usd=Decimal("0"),
    )

    with pytest.raises(ValueError, match="not active"):
        ledger.complete_call(
            reservation,
            input_tokens=1,
            output_tokens=1,
            duration_ms=1,
            estimated_cost_usd=Decimal("0"),
        )
