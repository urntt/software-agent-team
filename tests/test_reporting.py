"""Focused regressions for durable report classifications."""

from software_agent_team.budgets import AgentBudget, AgentBudgetLedger
from software_agent_team.model_costs import CacheTokenUsage
from software_agent_team.reporting import _cache_tokens


def _unknown_current_call():
    ledger = AgentBudgetLedger(
        AgentBudget(
            max_calls=1,
            max_input_tokens=1,
            max_output_tokens=1,
            max_agent_duration_seconds=1,
            max_estimated_cost_usd="1",
        )
    )
    reservation = ledger.reserve_call("reviewer")
    ledger.complete_call(
        reservation,
        input_tokens=None,
        output_tokens=None,
        duration_ms=1,
    )
    return ledger.call_records()[0]


def test_report_distinguishes_current_unknown_cache_usage() -> None:
    call = _unknown_current_call()

    assert call.cache_usage == CacheTokenUsage()
    assert _cache_tokens(call, ledger_schema_version=3) == (
        "; unknown cache read / unknown cache write"
    )


def test_report_names_the_exact_legacy_cache_schema() -> None:
    call = _unknown_current_call().model_copy(update={"cache_usage": None})

    assert _cache_tokens(call, ledger_schema_version=1) == (
        "; cache not recorded (legacy schema v1)"
    )
    assert _cache_tokens(call, ledger_schema_version=2) == (
        "; cache not recorded (legacy schema v2)"
    )
