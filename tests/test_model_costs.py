"""Disjoint input/cache/output pricing and truthful unknown-cost boundaries."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from software_agent_team import cli
from software_agent_team.execution import _parse_usage
from software_agent_team.model_costs import (
    CachePricing,
    CacheTokenUsage,
    estimate_model_cost,
)
from software_agent_team.model_metadata import ModelMetadataSource
from software_agent_team.model_routing import ModelProfile
from software_agent_team.runtime_configuration import OpenClawModelInspection
from software_agent_team.teams import AgentCapability
from software_agent_team.user_configuration import (
    UserConfiguration,
    load_user_configuration,
)


def pricing(read: str = "0.014", write: str = "0") -> CachePricing:
    return CachePricing(
        read_cost_per_million_usd=Decimal(read),
        write_cost_per_million_usd=Decimal(write),
        source=ModelMetadataSource.USER_SUPPLIED,
        observed_at=datetime(2026, 9, 8, tzinfo=UTC),
    )


def test_aggregate_cached_input_is_billed_separately() -> None:
    # Aggregate usage is not the last model request's context-sized total.
    assert estimate_model_cost(
        input_tokens=38737,
        output_tokens=18631,
        input_price=Decimal("0.44"),
        output_price=Decimal("1.32"),
        cache_usage=CacheTokenUsage(read_tokens=4742144, write_tokens=0),
        cache_pricing=pricing(),
    ) == Decimal("0.108027216")


@pytest.mark.parametrize("usage", [CacheTokenUsage(), CacheTokenUsage(read_tokens=0)])
def test_missing_cache_usage_is_not_zero_even_with_free_rates(
    usage: CacheTokenUsage,
) -> None:
    assert (
        estimate_model_cost(
            input_tokens=1,
            output_tokens=2,
            input_price=Decimal(0),
            output_price=Decimal(0),
            cache_usage=usage,
            cache_pricing=pricing("0", "0"),
        )
        is None
    )


def test_unpriced_positive_cache_is_not_silently_ignored() -> None:
    assert (
        estimate_model_cost(
            input_tokens=1,
            output_tokens=2,
            input_price=Decimal(1),
            output_price=Decimal(2),
            cache_usage=CacheTokenUsage(read_tokens=1, write_tokens=0),
        )
        is None
    )


def test_cache_write_rate_is_independent_of_input_and_read() -> None:
    assert estimate_model_cost(
        input_tokens=100,
        output_tokens=10,
        input_price=Decimal(3),
        output_price=Decimal(15),
        cache_usage=CacheTokenUsage(read_tokens=200, write_tokens=300),
        cache_pricing=pricing("0.3", "3.75"),
    ) == Decimal("0.001635")


@pytest.mark.parametrize("invalid", [-1, True, "1"])
def test_cache_usage_rejects_invalid_counts(invalid: object) -> None:
    with pytest.raises(ValidationError):
        CacheTokenUsage(read_tokens=invalid)


def test_historical_two_bucket_estimate_remains_readable() -> None:
    assert estimate_model_cost(
        input_tokens=38737,
        output_tokens=18631,
        input_price=Decimal("0.44"),
        output_price=Decimal("1.32"),
    ) == Decimal("0.0416372")


def test_pinned_usage_elides_zero_buckets_without_changing_aggregate_cache() -> None:
    usage = _parse_usage(
        {"input": 38737, "output": 18631, "cacheRead": 4742144, "total": 48270}
    )
    assert usage is not None
    assert usage.cache_usage == CacheTokenUsage(read_tokens=4742144, write_tokens=0)
    assert usage.total_tokens == 48270
    cached_only = _parse_usage({"cacheRead": 10})
    assert cached_only is not None
    assert cached_only.input_tokens == cached_only.output_tokens == 0


def test_explicit_unknown_and_total_only_usage_stay_unknown() -> None:
    explicit = _parse_usage(
        {"input": 1, "output": 2, "cacheRead": None, "cacheWrite": "bad"}
    )
    assert explicit is not None
    assert explicit.cache_usage == CacheTokenUsage()
    total_only = _parse_usage({"total": 12})
    assert total_only is not None
    assert total_only.input_tokens is None
    assert total_only.cache_usage == CacheTokenUsage()


def test_admission_completes_unknown_cache_rates_and_preserves_user_prices(
    monkeypatch,
    capsys,
) -> None:
    when = datetime(2026, 9, 8, tzinfo=UTC)
    configuration = UserConfiguration(
        model_profiles=(
            ModelProfile(
                id="default",
                model="provider/model",
                capabilities=(AgentCapability.CLARIFICATION, AgentCapability.PLANNING),
                input_cost_per_million_usd=1,
                output_cost_per_million_usd=2,
                pricing_source=ModelMetadataSource.USER_SUPPLIED,
                context_window_tokens=120000,
                context_source=ModelMetadataSource.USER_SUPPLIED,
            ),
        )
    )
    inspection = OpenClawModelInspection(
        model="provider/model",
        available=True,
        input_cost_per_million_usd=3,
        output_cost_per_million_usd=4,
    )
    answers = iter(("0.014", "0", "2", "no", "yes"))
    prompts = []

    def answer(prompt):
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr("builtins.input", answer)
    completed = cli._complete_model_metadata(
        configuration,
        (inspection,),
        offer_price_change=False,
        observed_at=when,
    )
    assert completed.input_cost_per_million_usd == 1
    assert completed.output_cost_per_million_usd == 2
    assert completed.default_model_profile.cache_pricing == pricing()
    authorization = cli._collect_task_resource_authorization(
        completed, authorized_at=when
    )
    assert authorization is not None
    assert authorization.model_metadata[0].cache_pricing == pricing()
    assert authorization.maximum_estimated_cost_usd == 2
    assert any("Cache read" in prompt for prompt in prompts)
    assert any("Cache write" in prompt for prompt in prompts)
    assert not any("Context-window" in prompt for prompt in prompts)
    authorization_view = capsys.readouterr().out.split("Models available to this task")[
        1
    ]
    assert "$1 input / $2 output" in authorization_view
    assert "$0.014 cache read / $0 cache write" in authorization_view
    assert "per million tokens (user_supplied)" in authorization_view
    assert "provider-side spending or quota limit" in authorization_view


@pytest.mark.parametrize("confirm_zero", [True, False])
def test_unknown_model_price_requires_explicit_zero_confirmation_before_authorization(
    monkeypatch, capsys, confirm_zero: bool
) -> None:
    configuration = UserConfiguration(model="provider/model")
    inspection = OpenClawModelInspection(
        model="provider/model", available=True, context_window_tokens=120000
    )
    prompts = []
    answers = iter(
        ["", "0", "0", "yes" if confirm_zero else "no"]
        + (["0", "0", "yes", "2", "no", "yes"] if confirm_zero else [])
    )

    def answer(prompt):
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr("builtins.input", answer)
    if not confirm_zero:
        with pytest.raises(
            cli.RuntimeConfigurationError, match="confirmation was declined"
        ):
            cli._complete_model_metadata(
                configuration, (inspection,), offer_price_change=False
            )
        assert not any("Maximum total model spend" in prompt for prompt in prompts)
        return
    completed = cli._complete_model_metadata(
        configuration, (inspection,), offer_price_change=False
    )
    authorization = cli._collect_task_resource_authorization(completed)
    assert authorization is not None
    frozen = authorization.model_metadata[0]
    assert frozen.pricing_source is ModelMetadataSource.CONFIRMED_ZERO
    assert frozen.cache_pricing.source is ModelMetadataSource.CONFIRMED_ZERO
    assert frozen.input_cost_per_million_usd == frozen.output_cost_per_million_usd == 0
    assert frozen.cache_pricing.read_cost_per_million_usd == 0
    assert frozen.cache_pricing.write_cost_per_million_usd == 0
    assert sum("Input price per million" in prompt for prompt in prompts) == 2
    assert not any("Context-window" in prompt for prompt in prompts)
    view = capsys.readouterr().out.split("Models available to this task")[1]
    assert "$0 input / $0 output" in view
    assert "$0 cache read / $0 cache write" in view
    assert view.count("confirmed_zero") == 2


def test_legacy_pricing_extension_does_not_change_serialization() -> None:
    from software_agent_team.budgets import ModelPricing

    legacy = {
        "model": "provider/model",
        "input_cost_per_million_usd": "1",
        "output_cost_per_million_usd": "2",
        "pricing_source": "user_supplied",
        "pricing_observed_at": None,
    }
    assert ModelPricing.model_validate(legacy).model_dump(mode="json") == legacy


def test_noninteractive_profile_cache_price_configuration(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "config.json"
    monkeypatch.setenv("SAT_CONFIG_PATH", str(path))
    monkeypatch.setenv("SAT_STATE_ROOT", str(tmp_path / "state"))
    assert (
        cli.main(
            [
                "configure",
                "--non-interactive",
                "--model",
                "provider/model",
                "--input-cost-per-million-usd",
                "0.44",
                "--output-cost-per-million-usd",
                "1.32",
                "--profile-cache-pricing",
                "default=0.014,0",
            ]
        )
        == 0
    )
    configured = load_user_configuration(path)
    assert configured is not None
    assert (
        configured.default_model_profile.cache_pricing.read_cost_per_million_usd
        == Decimal("0.014")
    )
