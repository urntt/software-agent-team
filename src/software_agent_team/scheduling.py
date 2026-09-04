"""Deterministic DAG scheduling for approved run-scoped Agent teams."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import UTC, datetime
from enum import StrEnum
from time import monotonic, sleep
from typing import Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from software_agent_team.artifacts import ArtifactReference
from software_agent_team.runtime_controls import (
    RuntimeControlChannel,
    RuntimeControlDecision,
)
from software_agent_team.teams import AgentSpec, PermissionProfile, TeamPlan


class AgentRunStatus(StrEnum):
    """Terminal result returned by one bounded Agent runner."""

    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class ScheduledAgentState(StrEnum):
    """Controller-owned terminal state for one approved AgentSpec."""

    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"


class ScheduleStatus(StrEnum):
    """Overall result of one DAG scheduling pass."""

    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    CORRECTION_REQUESTED = "correction_requested"


class ScheduleEventKind(StrEnum):
    """User-safe scheduler transition emitted by the controller."""

    AGENT_QUEUED = "agent_queued"
    AGENT_READY = "agent_ready"
    AGENT_STARTED = "agent_started"
    AGENT_COMPLETED = "agent_completed"
    AGENT_FAILED = "agent_failed"
    AGENT_SKIPPED = "agent_skipped"
    AGENT_INTERRUPTED = "agent_interrupted"
    AGENT_CANCELLED = "agent_cancelled"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("scheduler timestamps must include a timezone")
    return value.astimezone(UTC)


def _safe_schedule_message(value: str) -> str:
    cleaned = " ".join(value.split())
    cleaned = cleaned or "Scheduler state changed."
    limit = 500
    suffix = " … [truncated]"
    if len(cleaned) <= limit:
        return cleaned
    prefix = cleaned[: limit - len(suffix)].rstrip()
    word_boundary = prefix.rfind(" ")
    if word_boundary >= (limit - len(suffix)) // 2:
        prefix = prefix[:word_boundary].rstrip()
    return f"{prefix}{suffix}"


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
    timeout_seconds: int = Field(ge=0)
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
        if self.state in {
            ScheduledAgentState.SKIPPED,
            ScheduledAgentState.CANCELLED,
        }:
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
    duration_ms: int | None = Field(default=None, ge=0)

    @field_validator("occurred_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def require_terminal_duration(self) -> Self:
        terminal = self.kind in {
            ScheduleEventKind.AGENT_COMPLETED,
            ScheduleEventKind.AGENT_FAILED,
            ScheduleEventKind.AGENT_INTERRUPTED,
        }
        if terminal != (self.duration_ms is not None):
            raise ValueError("scheduler duration must match an executed terminal event")
        return self


class DagScheduleResult(BaseModel):
    """Complete deterministic result of one approved TeamPlan scheduling pass."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    plan_id: str
    iteration: int = Field(ge=1)
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
            if record.state
            not in {ScheduledAgentState.SKIPPED, ScheduledAgentState.CANCELLED}
        }:
            raise ValueError("completion order must cover every executed Agent")
        failed = [
            record.agent_id
            for record in self.records
            if record.state is ScheduledAgentState.FAILED
        ]
        interrupted = [
            record.agent_id
            for record in self.records
            if record.state is ScheduledAgentState.INTERRUPTED
        ]
        if self.status is ScheduleStatus.COMPLETED:
            if failed or interrupted or self.failed_agent_id is not None:
                raise ValueError("completed schedules cannot contain failures")
            if any(
                record.state is not ScheduledAgentState.COMPLETED
                for record in self.records
            ):
                raise ValueError("completed schedules require every Agent to complete")
        elif self.status is ScheduleStatus.FAILED:
            if not (failed or interrupted) or self.failed_agent_id not in {
                *failed,
                *interrupted,
            }:
                raise ValueError("failed schedule identity must name a failed Agent")
        elif self.failed_agent_id is not None:
            raise ValueError("controlled stops cannot claim a failed Agent identity")
        elif self.status is ScheduleStatus.CORRECTION_REQUESTED and interrupted:
            raise ValueError("cooperative correction cannot interrupt an Agent")
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
        control_channel: RuntimeControlChannel | None = None,
        control_poll_seconds: float = 0.1,
        control_waiter: Callable[[float], None] = sleep,
    ) -> None:
        if control_poll_seconds <= 0:
            raise ValueError("control polling interval must be positive")
        self.clock = clock
        self.monotonic_clock = monotonic_clock
        self.observer = observer
        self.control_channel = control_channel
        self.control_poll_seconds = control_poll_seconds
        self.control_waiter = control_waiter

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
        controlled_stop: RuntimeControlDecision | None = None
        max_observed_concurrency = 0

        def emit(
            kind: ScheduleEventKind,
            agent_id: str,
            message: str,
            *,
            active_count: int,
            duration_ms: int | None = None,
        ) -> None:
            event = ScheduleEvent(
                sequence=len(events) + 1,
                kind=kind,
                agent_id=agent_id,
                occurred_at=_utc(self.clock()),
                active_count=active_count,
                message=_safe_schedule_message(message),
                duration_ms=duration_ms,
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

        for agent in team_plan.agents:
            dependency_summary = (
                ", ".join(agent.dependencies) if agent.dependencies else "scheduler"
            )
            emit(
                ScheduleEventKind.AGENT_QUEUED,
                agent.id,
                f"{agent.label} queued after {dependency_summary}: "
                f"{agent.responsibility}",
                active_count=0,
            )

        ready_announced: set[str] = set()
        with ThreadPoolExecutor(max_workers=team_plan.max_concurrency) as executor:
            while pending or active:
                decision = RuntimeControlDecision.CONTINUE
                if self.control_channel is not None:
                    decision = self.control_channel.poll(
                        active_agent_ids=tuple(
                            agent.id for agent, _, _ in active.values()
                        ),
                        pending_agent_ids=tuple(
                            agent.id
                            for agent in team_plan.agents
                            if agent.id in pending
                        ),
                    )
                    if decision in {
                        RuntimeControlDecision.CANCEL,
                        RuntimeControlDecision.CORRECT,
                    }:
                        controlled_stop = decision
                launches_allowed = (
                    first_failure is None
                    and controlled_stop is None
                    and decision is RuntimeControlDecision.CONTINUE
                )
                if launches_allowed:
                    ready = [
                        agent
                        for agent in team_plan.agents
                        if agent.id in pending
                        and set(agent.dependencies).issubset(completed)
                    ]
                    for agent in ready:
                        if agent.id not in ready_announced:
                            emit(
                                ScheduleEventKind.AGENT_READY,
                                agent.id,
                                f"{agent.label} is ready: {agent.responsibility}",
                                active_count=len(active),
                            )
                            ready_announced.add(agent.id)
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
                            f"{agent.label} started: {agent.responsibility}",
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
                    if controlled_stop is not None:
                        if self.control_channel is not None:
                            decision = self.control_channel.poll(
                                active_agent_ids=(),
                                pending_agent_ids=tuple(
                                    agent.id
                                    for agent in team_plan.agents
                                    if agent.id in pending
                                ),
                            )
                            if decision in {
                                RuntimeControlDecision.CANCEL,
                                RuntimeControlDecision.CORRECT,
                            }:
                                controlled_stop = decision
                        break
                    if decision is RuntimeControlDecision.HOLD:
                        self.control_waiter(self.control_poll_seconds)
                        continue
                    if pending and first_failure is None:
                        raise RuntimeError(
                            "approved TeamPlan has no schedulable ready Agent"
                        )
                    break

                finished, _ = wait(
                    tuple(active),
                    timeout=(
                        self.control_poll_seconds
                        if self.control_channel is not None
                        else None
                    ),
                    return_when=FIRST_COMPLETED,
                )
                if not finished:
                    continue
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
                    elif outcome.status is AgentRunStatus.FAILED:
                        state = ScheduledAgentState.FAILED
                        event_kind = ScheduleEventKind.AGENT_FAILED
                        error = outcome.error
                        if first_failure is None and controlled_stop is None:
                            first_failure = agent.id
                    else:
                        state = ScheduledAgentState.INTERRUPTED
                        event_kind = ScheduleEventKind.AGENT_INTERRUPTED
                        error = outcome.error
                        if first_failure is None and controlled_stop is None:
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
                        duration_ms=duration_ms,
                    )

        if pending:
            if controlled_stop is RuntimeControlDecision.CANCEL:
                reason = "Not started because the user cancelled the run."
                skipped_state = ScheduledAgentState.CANCELLED
                skipped_event = ScheduleEventKind.AGENT_CANCELLED
            elif controlled_stop is RuntimeControlDecision.CORRECT:
                reason = "Not started because replacement Planning was requested."
                skipped_state = ScheduledAgentState.SKIPPED
                skipped_event = ScheduleEventKind.AGENT_SKIPPED
            else:
                reason = f"Not started after Agent {first_failure} failed."
                skipped_state = ScheduledAgentState.SKIPPED
                skipped_event = ScheduleEventKind.AGENT_SKIPPED
            for agent in team_plan.agents:
                if agent.id not in pending:
                    continue
                records[agent.id] = ScheduledAgentRecord(
                    agent_id=agent.id,
                    capability=agent.capability.value,
                    stage_id=agent.stage_id,
                    dependencies=agent.dependencies,
                    state=skipped_state,
                    timeout_seconds=agent.timeout_seconds,
                    summary=reason,
                    error=reason,
                )
                emit(
                    skipped_event,
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
                ScheduleStatus.CANCELLED
                if controlled_stop is RuntimeControlDecision.CANCEL
                else ScheduleStatus.CORRECTION_REQUESTED
                if controlled_stop is RuntimeControlDecision.CORRECT
                else ScheduleStatus.COMPLETED
                if first_failure is None
                else ScheduleStatus.FAILED
            ),
            records=ordered_records,
            events=tuple(events),
            completion_order=tuple(completion_order),
            max_observed_concurrency=max_observed_concurrency,
            failed_agent_id=None if controlled_stop is not None else first_failure,
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
