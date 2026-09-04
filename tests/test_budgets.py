"""Tests for explicit Agent resource and estimated-cost budgets."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from software_agent_team.budgets import (
    AgentBudget,
    AgentBudgetExceeded,
    AgentBudgetLedger,
    AgentCallReservation,
    BudgetAuthority,
    ModelCostSource,
    ModelPricing,
    load_budget_ledger,
    persist_budget_ledger,
)
from software_agent_team.model_metadata import ModelMetadataSource


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
        )

    usage = captured.value.usage
    assert usage.calls_completed == 1
    assert usage.active_calls == 0
    assert usage.input_tokens == 11
    assert usage.known_estimated_cost_usd == 0
    assert usage.unpriced_calls == 1


def test_budget_ledger_preserves_unknown_usage_and_price() -> None:
    ledger = AgentBudgetLedger(budget())
    reservation = ledger.reserve_call("reviewer")

    usage = ledger.complete_call(
        reservation,
        input_tokens=None,
        output_tokens=None,
        duration_ms=25,
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
    )

    with pytest.raises(ValueError, match="not active"):
        ledger.complete_call(
            reservation,
            input_tokens=1,
            output_tokens=1,
            duration_ms=1,
        )


def user_task_budget(maximum: str = "1.00") -> AgentBudget:
    return AgentBudget(
        authority=BudgetAuthority.USER_TASK,
        max_estimated_cost_usd=maximum,
    )


def priced_model(
    *,
    input_price: str = "2.50",
    output_price: str = "10.00",
) -> ModelPricing:
    return ModelPricing(
        model="provider/model",
        input_cost_per_million_usd=input_price,
        output_cost_per_million_usd=output_price,
        pricing_source=ModelMetadataSource.RUNTIME_CATALOG,
        pricing_observed_at=datetime(2026, 9, 4, 12, 0, tzinfo=UTC),
    )


def reserve_user_call(
    ledger: AgentBudgetLedger,
    *,
    attempt: int = 1,
    pricing: ModelPricing | None = None,
) -> AgentCallReservation:
    return ledger.reserve_call(
        "clarifier" if attempt == 1 else "builder",
        run_id="task-1",
        stage="planning" if attempt == 1 else "implementation",
        attempt=attempt,
        route_id="default",
        pricing=pricing or priced_model(),
    )


def test_user_task_rejects_unknown_pricing_before_launch() -> None:
    ledger = AgentBudgetLedger(user_task_budget())

    with pytest.raises(AgentBudgetExceeded, match="pricing is unknown") as captured:
        ledger.reserve_call(
            "clarifier",
            run_id="task-1",
            stage="planning",
            route_id="default",
        )

    assert captured.value.usage.calls_started == 0
    assert ledger.call_records() == ()


def test_user_task_rejects_unattributed_call_before_launch() -> None:
    ledger = AgentBudgetLedger(user_task_budget())

    with pytest.raises(ValueError, match="attribution is incomplete"):
        ledger.reserve_call("clarifier", pricing=priced_model())

    assert ledger.snapshot().calls_started == 0


def test_user_task_rejects_next_paid_call_after_recorded_ceiling() -> None:
    ledger = AgentBudgetLedger(user_task_budget("0.45"))
    first = reserve_user_call(ledger)
    usage = ledger.complete_call(
        first,
        input_tokens=100_000,
        output_tokens=20_000,
        duration_ms=125,
    )

    assert usage.remaining_estimated_cost_usd(ledger.budget) == 0
    with pytest.raises(AgentBudgetExceeded, match="exhausted before launch"):
        reserve_user_call(ledger, attempt=2)
    assert ledger.snapshot().calls_started == 1


def test_zero_price_task_can_run_at_zero_authorized_spend() -> None:
    ledger = AgentBudgetLedger(user_task_budget("0"))
    zero = priced_model(input_price="0", output_price="0").model_copy(
        update={"pricing_source": ModelMetadataSource.CONFIRMED_ZERO}
    )

    for attempt in (1, 2):
        reservation = reserve_user_call(ledger, attempt=attempt, pricing=zero)
        ledger.complete_call(
            reservation,
            input_tokens=50,
            output_tokens=10,
            duration_ms=5,
        )

    assert ledger.snapshot().known_estimated_cost_usd == 0
    assert ledger.snapshot().calls_completed == 2


def test_user_task_stops_when_provider_usage_cannot_account_cost() -> None:
    ledger = AgentBudgetLedger(user_task_budget())
    reservation = reserve_user_call(ledger)

    with pytest.raises(AgentBudgetExceeded, match="could not be accounted"):
        ledger.complete_call(
            reservation,
            input_tokens=None,
            output_tokens=None,
            duration_ms=25,
        )
    with pytest.raises(AgentBudgetExceeded, match="cannot be accounted"):
        reserve_user_call(ledger, attempt=2)


def test_terminal_budget_ledger_persists_attributable_cost_evidence(
    tmp_path: Path,
) -> None:
    ledger = AgentBudgetLedger(user_task_budget("2.00"))
    planning = reserve_user_call(ledger)
    ledger.complete_call(
        planning,
        input_tokens=100_000,
        output_tokens=20_000,
        duration_ms=125,
    )
    runtime = reserve_user_call(ledger, attempt=2)
    ledger.complete_call(
        runtime,
        input_tokens=10_000,
        output_tokens=5_000,
        duration_ms=250,
    )

    record, digest = persist_budget_ledger(tmp_path, ledger)
    loaded = load_budget_ledger(tmp_path / "budget-ledger.json")

    assert loaded == record
    assert len(digest) == 64
    assert loaded.usage.known_estimated_cost_usd == Decimal("0.525")
    assert [call.stage for call in loaded.calls] == ["planning", "implementation"]
    assert all(call.route_id == "default" for call in loaded.calls)
    assert all(call.cost_source is ModelCostSource.ESTIMATED for call in loaded.calls)
    assert all(
        call.pricing_source is ModelMetadataSource.RUNTIME_CATALOG
        for call in loaded.calls
    )
    assert all(call.pricing_observed_at is not None for call in loaded.calls)


def test_terminal_budget_ledger_rejects_an_active_call() -> None:
    ledger = AgentBudgetLedger(user_task_budget())
    reserve_user_call(ledger)

    with pytest.raises(ValueError, match="active calls"):
        ledger.terminal_record()
