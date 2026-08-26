"""Explicit Agent-call, token, duration, and estimated-cost budgets."""

from __future__ import annotations

import re
from decimal import Decimal
from threading import Lock
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

BUDGET_SCHEMA_VERSION = 1


class AgentBudgetExceeded(RuntimeError):
    """Raised after a pre-call or post-call aggregate budget boundary is crossed."""

    def __init__(self, detail: str, usage: AgentBudgetUsage) -> None:
        super().__init__(detail)
        self.usage = usage


class AgentCallReservation(BaseModel):
    """Controller-issued identity for one atomically reserved Agent invocation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence: int = Field(ge=1)
    agent_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")


class AgentBudgetUsage(BaseModel):
    """Thread-safe aggregate usage snapshot for one run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    calls_started: int = Field(ge=0)
    calls_completed: int = Field(ge=0)
    active_calls: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    agent_duration_ms: int = Field(ge=0)
    known_estimated_cost_usd: Decimal = Field(ge=0)
    unpriced_calls: int = Field(ge=0)
    unreported_token_calls: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_counts(self) -> AgentBudgetUsage:
        """Keep started, active, and completed call counts coherent."""

        if self.calls_completed + self.active_calls != self.calls_started:
            raise ValueError("budget call counts are inconsistent")
        if self.unpriced_calls > self.calls_completed:
            raise ValueError("unpriced calls cannot exceed completed calls")
        if self.unreported_token_calls > self.calls_completed:
            raise ValueError("unreported-token calls cannot exceed completed calls")
        return self


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


class AgentBudgetLedger:
    """Atomically reserve calls and retain all reported aggregate usage.

    Call count is enforced before launch. Token, duration, and known estimated
    cost thresholds are checked after the invocation because providers report
    them only with the result. Unknown pricing and missing token telemetry are
    recorded explicitly and never converted into a zero-cost or zero-token
    claim.
    """

    def __init__(self, budget: AgentBudget) -> None:
        self.budget = budget
        self._lock = Lock()
        self._calls_started = 0
        self._calls_completed = 0
        self._active: dict[int, str] = {}
        self._input_tokens = 0
        self._output_tokens = 0
        self._agent_duration_ms = 0
        self._known_estimated_cost_usd = Decimal(0)
        self._unpriced_calls = 0
        self._unreported_token_calls = 0

    def reserve_call(self, agent_id: str) -> AgentCallReservation:
        """Reserve one call before launch without a parallel oversubscription race."""

        if re.fullmatch(r"[a-z][a-z0-9_]*", agent_id) is None:
            raise ValueError("budget reservations require a valid Agent ID")
        with self._lock:
            if self._calls_started >= self.budget.max_calls:
                raise AgentBudgetExceeded(
                    "Agent call budget is exhausted",
                    self._snapshot_locked(),
                )
            self._calls_started += 1
            sequence = self._calls_started
            self._active[sequence] = agent_id
            return AgentCallReservation(sequence=sequence, agent_id=agent_id)

    def complete_call(
        self,
        reservation: AgentCallReservation,
        *,
        input_tokens: int | None,
        output_tokens: int | None,
        duration_ms: int,
        estimated_cost_usd: Decimal | None,
    ) -> AgentBudgetUsage:
        """Record one terminal invocation, then reject any crossed threshold."""

        if input_tokens is not None and input_tokens < 0:
            raise ValueError("reported input tokens cannot be negative")
        if output_tokens is not None and output_tokens < 0:
            raise ValueError("reported output tokens cannot be negative")
        if duration_ms < 0:
            raise ValueError("reported Agent duration cannot be negative")
        if estimated_cost_usd is not None and estimated_cost_usd < 0:
            raise ValueError("estimated Agent cost cannot be negative")

        with self._lock:
            active_agent = self._active.get(reservation.sequence)
            if active_agent != reservation.agent_id:
                raise ValueError("Agent call reservation is not active")
            del self._active[reservation.sequence]
            self._calls_completed += 1
            if input_tokens is None or output_tokens is None:
                self._unreported_token_calls += 1
            else:
                self._input_tokens += input_tokens
                self._output_tokens += output_tokens
            self._agent_duration_ms += duration_ms
            if estimated_cost_usd is None:
                self._unpriced_calls += 1
            else:
                self._known_estimated_cost_usd += estimated_cost_usd

            usage = self._snapshot_locked()
            detail = self._exceeded_detail(usage)
            if detail is not None:
                raise AgentBudgetExceeded(detail, usage)
            return usage

    def snapshot(self) -> AgentBudgetUsage:
        """Return an immutable current aggregate without changing accounting."""

        with self._lock:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> AgentBudgetUsage:
        return AgentBudgetUsage(
            calls_started=self._calls_started,
            calls_completed=self._calls_completed,
            active_calls=len(self._active),
            input_tokens=self._input_tokens,
            output_tokens=self._output_tokens,
            agent_duration_ms=self._agent_duration_ms,
            known_estimated_cost_usd=self._known_estimated_cost_usd,
            unpriced_calls=self._unpriced_calls,
            unreported_token_calls=self._unreported_token_calls,
        )

    def _exceeded_detail(self, usage: AgentBudgetUsage) -> str | None:
        if usage.input_tokens > self.budget.max_input_tokens:
            return "Agent input-token budget was exceeded"
        if usage.output_tokens > self.budget.max_output_tokens:
            return "Agent output-token budget was exceeded"
        if usage.agent_duration_ms > self.budget.max_agent_duration_seconds * 1000:
            return "Agent duration budget was exceeded"
        if usage.known_estimated_cost_usd > self.budget.max_estimated_cost_usd:
            return "Agent estimated-cost budget was exceeded"
        return None


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
