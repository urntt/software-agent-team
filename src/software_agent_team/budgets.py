"""Explicit model-call usage, price attribution, and resource authority."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from threading import Lock
from typing import Literal, Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from software_agent_team.model_metadata import ModelMetadataSource

BUDGET_SCHEMA_VERSION = 1
BUDGET_LEDGER_FILENAME = "budget-ledger.json"


class BudgetAuthority(StrEnum):
    """Why one run budget exists and which fields may stop work."""

    CONTROLLED_EVALUATION = "controlled_evaluation"
    USER_TASK = "user_task"


class ModelCostSource(StrEnum):
    """How one recorded USD amount was obtained."""

    ESTIMATED = "estimated"
    PROVIDER_REPORTED = "provider_reported"
    UNKNOWN = "unknown"


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
    run_id: str | None = Field(
        default=None,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
    )
    stage: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]*$")
    attempt: int = Field(default=1, ge=1)
    route_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]*$")
    model: str | None = Field(default=None, min_length=1)
    input_cost_per_million_usd: Decimal | None = Field(default=None, ge=0)
    output_cost_per_million_usd: Decimal | None = Field(default=None, ge=0)
    pricing_source: ModelMetadataSource | None = None
    pricing_observed_at: datetime | None = None

    @model_validator(mode="after")
    def require_complete_pricing(self) -> Self:
        prices_known = self.input_cost_per_million_usd is not None
        if prices_known != (self.output_cost_per_million_usd is not None):
            raise ValueError("call reservation prices must be configured together")
        if prices_known != (self.pricing_source is not None):
            raise ValueError("known call reservation prices require a source")
        if prices_known and self.model is None:
            raise ValueError("known call reservation prices require a model")
        return self

    @field_validator("pricing_observed_at")
    @classmethod
    def require_aware_pricing_time(
        cls,
        value: datetime | None,
    ) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("call reservation pricing time must include a UTC offset")
        return value.astimezone(UTC)

    def estimate_cost(
        self,
        *,
        input_tokens: int | None,
        output_tokens: int | None,
    ) -> Decimal | None:
        """Calculate cost only from this call's frozen prices and usage."""

        if (
            input_tokens is None
            or output_tokens is None
            or self.input_cost_per_million_usd is None
            or self.output_cost_per_million_usd is None
        ):
            return None
        return (
            Decimal(input_tokens) * self.input_cost_per_million_usd
            + Decimal(output_tokens) * self.output_cost_per_million_usd
        ) / Decimal(1_000_000)


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

    def remaining_estimated_cost_usd(self, budget: AgentBudget) -> Decimal:
        """Return the non-negative recorded estimated spend still authorized."""

        return max(
            Decimal(0),
            budget.max_estimated_cost_usd - self.known_estimated_cost_usd,
        )


class AgentBudget(BaseModel):
    """Controller budget for a controlled evaluation or one user task.

    Controlled evaluations may freeze call, token, duration, and cost ceilings.
    Ordinary product tasks authorize only one USD ceiling; all other usage is
    telemetry and cannot shape the team or stop the run.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[BUDGET_SCHEMA_VERSION] = BUDGET_SCHEMA_VERSION
    authority: BudgetAuthority = BudgetAuthority.CONTROLLED_EVALUATION
    max_calls: int | None = Field(default=None, ge=1, le=1_000_000)
    max_input_tokens: int | None = Field(default=None, ge=1, le=1_000_000_000)
    max_output_tokens: int | None = Field(default=None, ge=1, le=1_000_000_000)
    max_agent_duration_seconds: int | None = Field(
        default=None,
        ge=1,
        le=31_536_000,
    )
    max_estimated_cost_usd: Decimal = Field(ge=0)

    @model_validator(mode="after")
    def require_authority_specific_limits(self) -> AgentBudget:
        optional_limits = (
            self.max_calls,
            self.max_input_tokens,
            self.max_output_tokens,
            self.max_agent_duration_seconds,
        )
        if self.authority is BudgetAuthority.CONTROLLED_EVALUATION:
            if any(value is None for value in optional_limits):
                raise ValueError(
                    "controlled-evaluation budgets require call, token, and "
                    "duration ceilings"
                )
            if self.max_estimated_cost_usd <= 0:
                raise ValueError(
                    "controlled-evaluation budgets require a positive cost ceiling"
                )
        elif any(value is not None for value in optional_limits):
            raise ValueError(
                "ordinary user-task budgets may contain only one USD ceiling"
            )
        return self


class ModelCallCostRecord(BaseModel):
    """One immutable, attributable model-call accounting entry."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence: int = Field(ge=1)
    agent_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    run_id: str | None = Field(
        default=None,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
    )
    stage: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]*$")
    attempt: int = Field(ge=1)
    route_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]*$")
    model: str | None = Field(default=None, min_length=1)
    pricing_source: ModelMetadataSource | None = None
    input_cost_per_million_usd: Decimal | None = Field(default=None, ge=0)
    output_cost_per_million_usd: Decimal | None = Field(default=None, ge=0)
    pricing_observed_at: datetime | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    duration_ms: int = Field(ge=0)
    cost_source: ModelCostSource
    cost_usd: Decimal | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def require_consistent_cost_evidence(self) -> Self:
        prices_known = self.input_cost_per_million_usd is not None
        if prices_known != (self.output_cost_per_million_usd is not None):
            raise ValueError("call cost prices must be configured together")
        if prices_known != (self.pricing_source is not None):
            raise ValueError("known call cost prices require a source")
        if prices_known and self.model is None:
            raise ValueError("known call cost prices require a model")
        if (self.input_tokens is None) != (self.output_tokens is None):
            raise ValueError("call token usage must be reported together")
        if self.cost_source is ModelCostSource.UNKNOWN:
            if self.cost_usd is not None:
                raise ValueError("unknown call cost cannot contain a USD amount")
        elif self.cost_usd is None:
            raise ValueError("known call cost requires a USD amount")
        if self.cost_source is ModelCostSource.ESTIMATED:
            if not prices_known or self.input_tokens is None:
                raise ValueError("estimated call cost requires prices and token usage")
            expected = (
                Decimal(self.input_tokens) * self.input_cost_per_million_usd
                + Decimal(self.output_tokens or 0) * self.output_cost_per_million_usd
            ) / Decimal(1_000_000)
            if self.cost_usd != expected:
                raise ValueError("estimated call cost differs from frozen pricing")
        return self

    @field_validator("pricing_observed_at")
    @classmethod
    def require_aware_pricing_time(
        cls,
        value: datetime | None,
    ) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("call cost pricing time must include a UTC offset")
        return value.astimezone(UTC)


class BudgetLedgerRecord(BaseModel):
    """Terminal snapshot of the shared task/evaluation model-spend ledger."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[BUDGET_SCHEMA_VERSION] = BUDGET_SCHEMA_VERSION
    budget: AgentBudget
    usage: AgentBudgetUsage
    calls: tuple[ModelCallCostRecord, ...]

    @model_validator(mode="after")
    def validate_aggregate(self) -> Self:
        if self.usage.active_calls:
            raise ValueError("terminal budget ledger cannot contain active calls")
        if self.usage.calls_started != self.usage.calls_completed:
            raise ValueError("terminal budget ledger requires every call to complete")
        expected_sequences = tuple(range(1, len(self.calls) + 1))
        if tuple(call.sequence for call in self.calls) != expected_sequences:
            raise ValueError("budget ledger call records must be contiguous")
        if len(self.calls) != self.usage.calls_completed:
            raise ValueError("budget ledger call count differs from aggregate usage")
        if self.budget.authority is BudgetAuthority.USER_TASK and any(
            call.run_id is None
            or call.stage is None
            or call.route_id is None
            or call.model is None
            or call.pricing_source is None
            for call in self.calls
        ):
            raise ValueError("user-task ledger calls require complete attribution")
        reported = tuple(
            call
            for call in self.calls
            if call.input_tokens is not None and call.output_tokens is not None
        )
        if sum(call.input_tokens or 0 for call in reported) != self.usage.input_tokens:
            raise ValueError("budget ledger input-token total is inconsistent")
        if (
            sum(call.output_tokens or 0 for call in reported)
            != self.usage.output_tokens
        ):
            raise ValueError("budget ledger output-token total is inconsistent")
        if sum(call.duration_ms for call in self.calls) != self.usage.agent_duration_ms:
            raise ValueError("budget ledger duration total is inconsistent")
        known_cost = sum(
            (call.cost_usd for call in self.calls if call.cost_usd is not None),
            Decimal(0),
        )
        if known_cost != self.usage.known_estimated_cost_usd:
            raise ValueError("budget ledger cost total is inconsistent")
        unknown = sum(
            call.cost_source is ModelCostSource.UNKNOWN for call in self.calls
        )
        if unknown != self.usage.unpriced_calls:
            raise ValueError("budget ledger unknown-cost total is inconsistent")
        unreported = sum(call.input_tokens is None for call in self.calls)
        if unreported != self.usage.unreported_token_calls:
            raise ValueError("budget ledger unreported-token total is inconsistent")
        return self


class AgentBudgetLedger:
    """Atomically reserve calls and retain all reported aggregate usage.

    Controlled-evaluation call count is enforced before launch. For ordinary
    tasks, every call must have frozen pricing and no new non-zero-priced call
    may start after recorded estimated spend reaches the user's ceiling.
    Provider token usage arrives only after a call, so an absolute billing cap
    still requires a provider-side spending/quota limit. Unknown pricing or
    missing token telemetry is recorded and stops an ordinary task rather than
    being converted into a zero-cost claim.
    """

    def __init__(self, budget: AgentBudget) -> None:
        self.budget = budget
        self._lock = Lock()
        self._calls_started = 0
        self._calls_completed = 0
        self._active: dict[int, AgentCallReservation] = {}
        self._input_tokens = 0
        self._output_tokens = 0
        self._agent_duration_ms = 0
        self._known_estimated_cost_usd = Decimal(0)
        self._unpriced_calls = 0
        self._unreported_token_calls = 0
        self._call_records: list[ModelCallCostRecord] = []

    def reserve_call(
        self,
        agent_id: str,
        *,
        run_id: str | None = None,
        stage: str | None = None,
        attempt: int = 1,
        route_id: str | None = None,
        pricing: ModelPricing | None = None,
    ) -> AgentCallReservation:
        """Reserve one call before launch without a parallel oversubscription race."""

        if re.fullmatch(r"[a-z][a-z0-9_]*", agent_id) is None:
            raise ValueError("budget reservations require a valid Agent ID")
        with self._lock:
            usage = self._snapshot_locked()
            if (
                self.budget.max_calls is not None
                and self._calls_started >= self.budget.max_calls
            ):
                raise AgentBudgetExceeded(
                    "Agent call budget is exhausted",
                    usage,
                )
            if self.budget.authority is BudgetAuthority.USER_TASK:
                if run_id is None or stage is None or route_id is None:
                    raise ValueError(
                        "Task model-call attribution is incomplete before launch"
                    )
                if pricing is None or pricing.pricing_source is None:
                    raise AgentBudgetExceeded(
                        "Task model pricing is unknown before launch",
                        usage,
                    )
                if self._unpriced_calls or self._unreported_token_calls:
                    raise AgentBudgetExceeded(
                        "Task model spend cannot be accounted before another call",
                        usage,
                    )
                non_zero_price = bool(
                    pricing.input_cost_per_million_usd
                    or pricing.output_cost_per_million_usd
                )
                if (
                    non_zero_price
                    and self._known_estimated_cost_usd
                    >= self.budget.max_estimated_cost_usd
                ):
                    raise AgentBudgetExceeded(
                        "Task model-spend authorization is exhausted before launch",
                        usage,
                    )
            self._calls_started += 1
            sequence = self._calls_started
            reservation = AgentCallReservation(
                sequence=sequence,
                agent_id=agent_id,
                run_id=run_id,
                stage=stage,
                attempt=attempt,
                route_id=route_id,
                model=None if pricing is None else pricing.model,
                input_cost_per_million_usd=(
                    None if pricing is None else pricing.input_cost_per_million_usd
                ),
                output_cost_per_million_usd=(
                    None if pricing is None else pricing.output_cost_per_million_usd
                ),
                pricing_source=None if pricing is None else pricing.pricing_source,
                pricing_observed_at=(
                    None if pricing is None else pricing.pricing_observed_at
                ),
            )
            self._active[sequence] = reservation
            return reservation

    def complete_call(
        self,
        reservation: AgentCallReservation,
        *,
        input_tokens: int | None,
        output_tokens: int | None,
        duration_ms: int,
    ) -> AgentBudgetUsage:
        """Price and record one terminal invocation, then enforce its authority."""

        if input_tokens is not None and input_tokens < 0:
            raise ValueError("reported input tokens cannot be negative")
        if output_tokens is not None and output_tokens < 0:
            raise ValueError("reported output tokens cannot be negative")
        if duration_ms < 0:
            raise ValueError("reported Agent duration cannot be negative")

        with self._lock:
            active_reservation = self._active.get(reservation.sequence)
            if active_reservation != reservation:
                raise ValueError("Agent call reservation is not active")
            estimated_cost_usd = reservation.estimate_cost(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
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
            self._call_records.append(
                ModelCallCostRecord(
                    sequence=reservation.sequence,
                    agent_id=reservation.agent_id,
                    run_id=reservation.run_id,
                    stage=reservation.stage,
                    attempt=reservation.attempt,
                    route_id=reservation.route_id,
                    model=reservation.model,
                    pricing_source=reservation.pricing_source,
                    pricing_observed_at=reservation.pricing_observed_at,
                    input_cost_per_million_usd=(reservation.input_cost_per_million_usd),
                    output_cost_per_million_usd=(
                        reservation.output_cost_per_million_usd
                    ),
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    duration_ms=duration_ms,
                    cost_source=(
                        ModelCostSource.UNKNOWN
                        if estimated_cost_usd is None
                        else ModelCostSource.ESTIMATED
                    ),
                    cost_usd=estimated_cost_usd,
                )
            )

            usage = self._snapshot_locked()
            detail = self._exceeded_detail(usage)
            if detail is not None:
                raise AgentBudgetExceeded(detail, usage)
            return usage

    def snapshot(self) -> AgentBudgetUsage:
        """Return an immutable current aggregate without changing accounting."""

        with self._lock:
            return self._snapshot_locked()

    def call_records(self) -> tuple[ModelCallCostRecord, ...]:
        """Return immutable per-call accounting in reservation order."""

        with self._lock:
            return tuple(sorted(self._call_records, key=lambda item: item.sequence))

    def terminal_record(self) -> BudgetLedgerRecord:
        """Freeze a complete ledger after every reserved call has terminated."""

        with self._lock:
            return BudgetLedgerRecord(
                budget=self.budget,
                usage=self._snapshot_locked(),
                calls=tuple(sorted(self._call_records, key=lambda item: item.sequence)),
            )

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
        if self.budget.authority is BudgetAuthority.USER_TASK and (
            usage.unpriced_calls or usage.unreported_token_calls
        ):
            return "Task model spend could not be accounted from provider usage"
        if (
            self.budget.max_input_tokens is not None
            and usage.input_tokens > self.budget.max_input_tokens
        ):
            return "Agent input-token budget was exceeded"
        if (
            self.budget.max_output_tokens is not None
            and usage.output_tokens > self.budget.max_output_tokens
        ):
            return "Agent output-token budget was exceeded"
        if (
            self.budget.max_agent_duration_seconds is not None
            and usage.agent_duration_ms > self.budget.max_agent_duration_seconds * 1000
        ):
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
    )
    output_cost_per_million_usd: Decimal | None = Field(
        default=None,
        ge=0,
    )
    pricing_source: ModelMetadataSource | None = None
    pricing_observed_at: datetime | None = None

    @model_validator(mode="before")
    @classmethod
    def infer_explicit_price_source(cls, value: object) -> object:
        """Attribute legacy explicit prices without claiming discovery."""

        if not isinstance(value, dict):
            return value
        payload = dict(value)
        if (
            payload.get("input_cost_per_million_usd") is not None
            and payload.get("output_cost_per_million_usd") is not None
            and payload.get("pricing_source") is None
        ):
            payload["pricing_source"] = ModelMetadataSource.USER_SUPPLIED.value
        return payload

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
        prices_known = self.input_cost_per_million_usd is not None
        if prices_known != (self.pricing_source is not None):
            raise ValueError("known model prices require one pricing source")
        if self.pricing_source is ModelMetadataSource.CONFIRMED_ZERO and (
            self.input_cost_per_million_usd != 0
            or self.output_cost_per_million_usd != 0
        ):
            raise ValueError("confirmed-zero pricing requires two zero prices")
        return self

    @field_validator("pricing_observed_at")
    @classmethod
    def require_aware_observation_time(
        cls,
        value: datetime | None,
    ) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("pricing observation time must include a UTC offset")
        return value.astimezone(UTC)

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


def persist_budget_ledger(
    run_directory: Path,
    ledger: AgentBudgetLedger,
) -> tuple[BudgetLedgerRecord, str]:
    """Write one terminal ledger snapshot immutably and return its digest."""

    if run_directory.is_symlink() or not run_directory.is_dir():
        raise ValueError("budget ledger requires a real run directory")
    record = ledger.terminal_record()
    content = serialize_budget_ledger(record)
    digest = hashlib.sha256(content).hexdigest()
    destination = run_directory / BUDGET_LEDGER_FILENAME
    temporary = run_directory / f".{BUDGET_LEDGER_FILENAME}.{uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError as error:
            raise ValueError("budget ledger evidence already exists") from error
        descriptor = os.open(run_directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)
    return record, digest


def serialize_budget_ledger(record: BudgetLedgerRecord) -> bytes:
    """Return the canonical bytes used by persistence and bundle validation."""

    return (
        json.dumps(record.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"
    ).encode()


def budget_ledger_sha256(record: BudgetLedgerRecord) -> str:
    """Return the digest named by human reports before bundle persistence."""

    return hashlib.sha256(serialize_budget_ledger(record)).hexdigest()


def load_budget_ledger(path: Path) -> BudgetLedgerRecord:
    """Load one immutable terminal ledger without following a symlink."""

    if path.is_symlink() or not path.is_file():
        raise ValueError("budget ledger evidence is not a regular file")
    try:
        return BudgetLedgerRecord.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise ValueError("budget ledger evidence is invalid") from error
