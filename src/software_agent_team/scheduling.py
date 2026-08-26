"""Deterministic DAG scheduling for approved run-scoped Agent teams."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import UTC, datetime
from enum import StrEnum
from time import monotonic
from typing import Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from software_agent_team.artifacts import ArtifactReference
from software_agent_team.teams import AgentSpec, PermissionProfile, TeamPlan


class AgentRunStatus(StrEnum):
    """Terminal result returned by one bounded Agent runner."""

    COMPLETED = "completed"
    FAILED = "failed"


class ScheduledAgentState(StrEnum):
    """Controller-owned terminal state for one approved AgentSpec."""

    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class ScheduleStatus(StrEnum):
    """Overall result of one DAG scheduling pass."""

    COMPLETED = "completed"
    FAILED = "failed"


class ScheduleEventKind(StrEnum):
    """User-safe scheduler transition emitted by the controller."""

    AGENT_STARTED = "agent_started"
    AGENT_COMPLETED = "agent_completed"
    AGENT_FAILED = "agent_failed"
    AGENT_SKIPPED = "agent_skipped"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("scheduler timestamps must include a timezone")
    return value.astimezone(UTC)


class AgentRunOutcome(BaseModel):
    """Typed result returned by the runtime adapter for one AgentSpec."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    status: AgentRunStatus
    output: ArtifactReference | None = None
    evidence: tuple[ArtifactReference, ...] = ()
    summary: str = Field(min_length=1, max_length=2000)
    error: str | None = Field(default=None, min_length=1, max_length=2000)

    @field_validator("summary", "error")
    @classmethod
    def require_clean_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("scheduler outcome text must not be blank")
        return cleaned

    @model_validator(mode="after")
    def validate_terminal_evidence(self) -> Self:
        if self.status is AgentRunStatus.COMPLETED:
            if self.output is None or self.error is not None:
                raise ValueError(
                    "completed Agent outcomes require output without error"
                )
        elif self.output is not None or self.error is None:
            raise ValueError("failed Agent outcomes require error without output")
        paths = [item.path for item in self.evidence]
        if len(paths) != len(set(paths)):
            raise ValueError("Agent outcome evidence references must be unique")
        return self


class ScheduledAgentRecord(BaseModel):
    """Controller-owned terminal scheduling evidence for one AgentSpec."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    capability: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    stage_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    dependencies: tuple[str, ...] = ()
    state: ScheduledAgentState
    timeout_seconds: int = Field(ge=1, le=3600)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    output: ArtifactReference | None = None
    evidence: tuple[ArtifactReference, ...] = ()
    summary: str = Field(min_length=1, max_length=2000)
    error: str | None = Field(default=None, min_length=1, max_length=2000)

    @field_validator("started_at", "finished_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)

    @model_validator(mode="after")
    def validate_state_evidence(self) -> Self:
        timing = (self.started_at, self.finished_at, self.duration_ms)
        if self.state is ScheduledAgentState.SKIPPED:
            if any(value is not None for value in timing):
                raise ValueError("skipped Agents cannot contain execution timing")
            if self.output is not None or self.evidence:
                raise ValueError("skipped Agents cannot claim runtime evidence")
            if self.error is None:
                raise ValueError("skipped Agents require a controller reason")
        else:
            if any(value is None for value in timing):
                raise ValueError("executed Agents require complete timing evidence")
            assert self.started_at is not None
            assert self.finished_at is not None
            if self.finished_at < self.started_at:
                raise ValueError("Agent finish time cannot precede start time")
            if self.state is ScheduledAgentState.COMPLETED:
                if self.output is None or self.error is not None:
                    raise ValueError("completed Agents require output without error")
            elif self.output is not None or self.error is None:
                raise ValueError("failed Agents require error without output")
        return self


class ScheduleEvent(BaseModel):
    """Ordered controller transition suitable for progress projection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence: int = Field(ge=1)
    kind: ScheduleEventKind
    agent_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    occurred_at: datetime
    active_count: int = Field(ge=0)
    message: str = Field(min_length=1, max_length=500)

    @field_validator("occurred_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        return _utc(value)


class DagScheduleResult(BaseModel):
    """Complete deterministic result of one approved TeamPlan scheduling pass."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    plan_id: str
    iteration: int = Field(ge=1, le=3)
    status: ScheduleStatus
    records: tuple[ScheduledAgentRecord, ...]
    events: tuple[ScheduleEvent, ...]
    completion_order: tuple[str, ...]
    max_observed_concurrency: int = Field(ge=0)
    failed_agent_id: str | None = None

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        record_ids = [record.agent_id for record in self.records]
        if len(record_ids) != len(set(record_ids)):
            raise ValueError("schedule records must contain unique Agent IDs")
        if len(self.completion_order) != len(set(self.completion_order)):
            raise ValueError("completion order cannot repeat an Agent ID")
        if set(self.completion_order) != {
            record.agent_id
            for record in self.records
            if record.state is not ScheduledAgentState.SKIPPED
        }:
            raise ValueError("completion order must cover every executed Agent")
        failed = [
            record.agent_id
            for record in self.records
            if record.state is ScheduledAgentState.FAILED
        ]
        if self.status is ScheduleStatus.COMPLETED:
            if failed or self.failed_agent_id is not None:
                raise ValueError("completed schedules cannot contain failures")
            if any(
                record.state is not ScheduledAgentState.COMPLETED
                for record in self.records
            ):
                raise ValueError("completed schedules require every Agent to complete")
        elif not failed or self.failed_agent_id not in failed:
            raise ValueError("failed schedule identity must name a failed Agent")
        return self


class AgentRunner(Protocol):
    """Runtime callback invoked once for a ready approved AgentSpec."""

    def __call__(
        self,
        agent: AgentSpec,
        upstream: Mapping[str, AgentRunOutcome],
    ) -> AgentRunOutcome: ...


ScheduleObserver = Callable[[ScheduleEvent], None]
Clock = Callable[[], datetime]
MonotonicClock = Callable[[], float]


def _system_clock() -> datetime:
    return datetime.now(UTC)


class DagScheduler:
    """Execute ready DAG nodes within policy and shared-Git safety limits.

    The current workspace backend exposes one clone and one Git index. A writer
    therefore runs exclusively: it cannot overlap another writer or a reader.
    Read-only Agents may run together up to the approved concurrency cap.
    """

    def __init__(
        self,
        *,
        clock: Clock = _system_clock,
        monotonic_clock: MonotonicClock = monotonic,
        observer: ScheduleObserver | None = None,
    ) -> None:
        self.clock = clock
        self.monotonic_clock = monotonic_clock
        self.observer = observer

    def execute(
        self,
        team_plan: TeamPlan,
        runner: AgentRunner,
        *,
        iteration: int = 1,
    ) -> DagScheduleResult:
        """Run each approved Agent at most once and stop launching after failure."""

        if not 1 <= iteration <= team_plan.iteration_limit:
            raise ValueError("schedule iteration exceeds the approved TeamPlan")

        agents = {agent.id: agent for agent in team_plan.agents}
        plan_order = {agent.id: index for index, agent in enumerate(team_plan.agents)}
        pending = set(agents)
        completed: dict[str, AgentRunOutcome] = {}
        records: dict[str, ScheduledAgentRecord] = {}
        completion_order: list[str] = []
        events: list[ScheduleEvent] = []
        active: dict[
            Future[AgentRunOutcome],
            tuple[AgentSpec, datetime, float],
        ] = {}
        first_failure: str | None = None
        max_observed_concurrency = 0

        def emit(
            kind: ScheduleEventKind,
            agent_id: str,
            message: str,
            *,
            active_count: int,
        ) -> None:
            event = ScheduleEvent(
                sequence=len(events) + 1,
                kind=kind,
                agent_id=agent_id,
                occurred_at=_utc(self.clock()),
                active_count=active_count,
                message=message,
            )
            events.append(event)
            if self.observer is not None:
                self.observer(event)

        def invoke(
            agent: AgentSpec,
            upstream: Mapping[str, AgentRunOutcome],
        ) -> AgentRunOutcome:
            try:
                outcome = runner(agent, upstream)
            except Exception as error:  # controller must retain runner failures
                detail = str(error).strip() or type(error).__name__
                return AgentRunOutcome(
                    agent_id=agent.id,
                    status=AgentRunStatus.FAILED,
                    summary="The Agent runner raised an exception.",
                    error=detail[:2000],
                )
            try:
                if outcome.agent_id != agent.id:
                    raise ValueError("Agent runner returned a different Agent ID")
                if (
                    outcome.status is AgentRunStatus.COMPLETED
                    and outcome.output is not None
                    and outcome.output.kind is not agent.expected_output
                ):
                    raise ValueError("Agent output kind differs from its AgentSpec")
            except ValueError as error:
                return AgentRunOutcome(
                    agent_id=agent.id,
                    status=AgentRunStatus.FAILED,
                    summary="The Agent runner returned invalid evidence.",
                    error=str(error),
                )
            return outcome

        with ThreadPoolExecutor(max_workers=team_plan.max_concurrency) as executor:
            while pending or active:
                if first_failure is None:
                    ready = [
                        agent
                        for agent in team_plan.agents
                        if agent.id in pending
                        and set(agent.dependencies).issubset(completed)
                    ]
                    for agent in ready:
                        if len(active) >= team_plan.max_concurrency:
                            break
                        if not self._can_launch(agent, active):
                            continue
                        upstream = {
                            dependency: completed[dependency]
                            for dependency in agent.dependencies
                        }
                        started_at = _utc(self.clock())
                        started_monotonic = self.monotonic_clock()
                        pending.remove(agent.id)
                        emit(
                            ScheduleEventKind.AGENT_STARTED,
                            agent.id,
                            f"{agent.label} started.",
                            active_count=len(active) + 1,
                        )
                        future = executor.submit(invoke, agent, upstream)
                        active[future] = (
                            agent,
                            started_at,
                            started_monotonic,
                        )
                        max_observed_concurrency = max(
                            max_observed_concurrency,
                            len(active),
                        )
                if not active:
                    if pending and first_failure is None:
                        raise RuntimeError(
                            "approved TeamPlan has no schedulable ready Agent"
                        )
                    break

                finished, _ = wait(tuple(active), return_when=FIRST_COMPLETED)
                for future in sorted(
                    finished,
                    key=lambda item: plan_order[active[item][0].id],
                ):
                    agent, started_at, started_monotonic = active.pop(future)
                    outcome = future.result()
                    finished_at = _utc(self.clock())
                    duration_ms = max(
                        0,
                        round((self.monotonic_clock() - started_monotonic) * 1000),
                    )
                    completion_order.append(agent.id)
                    if outcome.status is AgentRunStatus.COMPLETED:
                        completed[agent.id] = outcome
                        state = ScheduledAgentState.COMPLETED
                        event_kind = ScheduleEventKind.AGENT_COMPLETED
                        error = None
                    else:
                        state = ScheduledAgentState.FAILED
                        event_kind = ScheduleEventKind.AGENT_FAILED
                        error = outcome.error
                        if first_failure is None:
                            first_failure = agent.id
                    records[agent.id] = ScheduledAgentRecord(
                        agent_id=agent.id,
                        capability=agent.capability.value,
                        stage_id=agent.stage_id,
                        dependencies=agent.dependencies,
                        state=state,
                        timeout_seconds=agent.timeout_seconds,
                        started_at=started_at,
                        finished_at=finished_at,
                        duration_ms=duration_ms,
                        output=outcome.output,
                        evidence=outcome.evidence,
                        summary=outcome.summary,
                        error=error,
                    )
                    emit(
                        event_kind,
                        agent.id,
                        outcome.summary,
                        active_count=len(active),
                    )

        if pending:
            reason = f"Not started after Agent {first_failure} failed."
            for agent in team_plan.agents:
                if agent.id not in pending:
                    continue
                records[agent.id] = ScheduledAgentRecord(
                    agent_id=agent.id,
                    capability=agent.capability.value,
                    stage_id=agent.stage_id,
                    dependencies=agent.dependencies,
                    state=ScheduledAgentState.SKIPPED,
                    timeout_seconds=agent.timeout_seconds,
                    summary=reason,
                    error=reason,
                )
                emit(
                    ScheduleEventKind.AGENT_SKIPPED,
                    agent.id,
                    reason,
                    active_count=0,
                )

        ordered_records = tuple(records[agent.id] for agent in team_plan.agents)
        return DagScheduleResult(
            run_id=team_plan.run_id,
            plan_id=team_plan.plan_id,
            iteration=iteration,
            status=(
                ScheduleStatus.COMPLETED
                if first_failure is None
                else ScheduleStatus.FAILED
            ),
            records=ordered_records,
            events=tuple(events),
            completion_order=tuple(completion_order),
            max_observed_concurrency=max_observed_concurrency,
            failed_agent_id=first_failure,
        )

    @staticmethod
    def _can_launch(
        candidate: AgentSpec,
        active: Mapping[
            Future[AgentRunOutcome],
            tuple[AgentSpec, datetime, float],
        ],
    ) -> bool:
        """Apply the exclusive-writer rule of the shared Git workspace backend."""

        if not active:
            return True
        candidate_writes = (
            candidate.permission_profile is PermissionProfile.WORKSPACE_WRITE
        )
        active_writes = any(
            agent.permission_profile is PermissionProfile.WORKSPACE_WRITE
            for agent, _, _ in active.values()
        )
        return not candidate_writes and not active_writes
