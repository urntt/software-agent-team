"""Explicit Agent-call, token, duration, and estimated-cost budgets."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

BUDGET_SCHEMA_VERSION = 1


class AgentBudget(BaseModel):
    """Stop thresholds for one bounded Agent workflow run.

    The call count is checked before launch. Token, duration, and cost usage are
    provider-reported measurements, so crossing those thresholds fails the run
    after that invocation and prevents another one.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[BUDGET_SCHEMA_VERSION] = BUDGET_SCHEMA_VERSION
    max_calls: int = Field(ge=4, le=50)
    max_input_tokens: int = Field(ge=1, le=10_000_000)
    max_output_tokens: int = Field(ge=1, le=2_000_000)
    max_agent_duration_seconds: int = Field(ge=1, le=86_400)
    max_estimated_cost_usd: Decimal = Field(gt=0, le=10_000)


class ModelPricing(BaseModel):
    """Frozen model identity and optional per-million-token prices for one run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str = Field(min_length=1)
    input_cost_per_million_usd: Decimal | None = Field(
        default=None,
        ge=0,
        le=10_000,
    )
    output_cost_per_million_usd: Decimal | None = Field(
        default=None,
        ge=0,
        le=10_000,
    )

    @field_validator("model")
    @classmethod
    def require_clean_model(cls, value: str) -> str:
        """Keep the recorded comparison model explicit and stable."""

        cleaned = value.strip()
        if not cleaned:
            raise ValueError("model must not be blank")
        return cleaned

    @model_validator(mode="after")
    def require_complete_price_pair(self) -> ModelPricing:
        """Never estimate cost from only one token direction."""

        if (self.input_cost_per_million_usd is None) != (
            self.output_cost_per_million_usd is None
        ):
            raise ValueError("input and output prices must be configured together")
        return self

    def estimate_cost(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
    ) -> Decimal | None:
        """Estimate one invocation when a frozen price table is available."""

        if (
            self.input_cost_per_million_usd is None
            or self.output_cost_per_million_usd is None
        ):
            return None

        million = Decimal(1_000_000)
        return (
            Decimal(input_tokens) * self.input_cost_per_million_usd
            + Decimal(output_tokens) * self.output_cost_per_million_usd
        ) / million
