"""Cost of disjoint normalized token buckets, never provider prompt totals."""

from datetime import UTC, datetime
from decimal import Decimal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)

from software_agent_team.model_metadata import ModelMetadataSource


class CachePricing(BaseModel):
    """Explicit read/write rates with their own attributable observation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    read_cost_per_million_usd: Decimal = Field(ge=0, allow_inf_nan=False)
    write_cost_per_million_usd: Decimal = Field(ge=0, allow_inf_nan=False)
    source: ModelMetadataSource
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def require_aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("cache pricing time must include a UTC offset")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_zero_confirmation(self) -> "CachePricing":
        if self.source is ModelMetadataSource.CONFIRMED_ZERO and (
            self.read_cost_per_million_usd != 0 or self.write_cost_per_million_usd != 0
        ):
            raise ValueError("confirmed-zero cache pricing requires two zero rates")
        return self


class CacheTokenUsage(BaseModel):
    """Disjoint cached-input buckets; missing evidence remains unknown."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    read_tokens: int | None = Field(default=None, ge=0, strict=True)
    write_tokens: int | None = Field(default=None, ge=0, strict=True)


class CachePriceSupport(BaseModel):
    """Append-only price extension without rewriting historical canonical bytes."""

    cache_pricing: CachePricing | None = None

    @model_serializer(mode="wrap")
    def serialize_cache_extensions(self, handler):
        result = handler(self)
        for name in ("cache_pricing", "cache_usage"):
            if result.get(name) is None:
                result.pop(name, None)
        return result


class CacheAccountingSupport(CachePriceSupport):
    """A call's frozen cache prices and separately reported cache usage."""

    cache_usage: CacheTokenUsage | None = None


def estimate_model_cost(
    *,
    input_tokens: int | None,
    output_tokens: int | None,
    input_price: Decimal | None,
    output_price: Decimal | None,
    cache_usage: CacheTokenUsage | None = None,
    cache_pricing: CachePricing | None = None,
) -> Decimal | None:
    """Price normalized usage once; absent extensions read legacy evidence only.

    Input excludes cache reads/writes; output already includes billable reasoning.
    Neither a provider's total nor reasoning subset is an additional bucket.
    Current invocations must supply cache_usage, even when it is unknown. The
    two absent extensions preserve calculation of historical two-bucket records,
    not a claim that their unrecorded cache usage was free.
    """

    if any(
        value is None
        for value in (input_tokens, output_tokens, input_price, output_price)
    ):
        return None
    assert input_tokens is not None and output_tokens is not None
    assert input_price is not None and output_price is not None
    if input_tokens < 0 or output_tokens < 0:
        raise ValueError("token counts cannot be negative")
    amount = Decimal(input_tokens) * input_price + Decimal(output_tokens) * output_price
    if cache_usage is None:
        return amount / Decimal(1_000_000) if cache_pricing is None else None
    if cache_usage.read_tokens is None or cache_usage.write_tokens is None:
        return None
    if cache_pricing is None:
        if cache_usage.read_tokens or cache_usage.write_tokens:
            return None
    else:
        amount += (
            Decimal(cache_usage.read_tokens) * cache_pricing.read_cost_per_million_usd
            + Decimal(cache_usage.write_tokens)
            * cache_pricing.write_cost_per_million_usd
        )
    return amount / Decimal(1_000_000)
