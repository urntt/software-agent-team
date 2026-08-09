"""Explicit Agent-call, token, duration, and estimated-cost budgets."""

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

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
    """Frozen model identity and per-million-token prices for one run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str = Field(min_length=1)
    input_cost_per_million_usd: Decimal = Field(ge=0, le=10_000)
    output_cost_per_million_usd: Decimal = Field(ge=0, le=10_000)

    @field_validator("model")
    @classmethod
    def require_clean_model(cls, value: str) -> str:
        """Keep the recorded comparison model explicit and stable."""

        cleaned = value.strip()
        if not cleaned:
            raise ValueError("model must not be blank")
        return cleaned

    def estimate_cost(self, *, input_tokens: int, output_tokens: int) -> Decimal:
        """Estimate one invocation using the frozen comparison price table."""

        million = Decimal(1_000_000)
        return (
            Decimal(input_tokens) * self.input_cost_per_million_usd
            + Decimal(output_tokens) * self.output_cost_per_million_usd
        ) / million
