"""Apply persisted user controls at deterministic dynamic-runtime checkpoints."""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from threading import RLock

from software_agent_team.controls import (
    ControlCommand,
    ControlCommandStatus,
    ControlCommandStore,
    ControlCommandType,
    ControlTargetKind,
)
from software_agent_team.integrity import canonical_model_sha256
from software_agent_team.progress import (
    ProgressDraftHandler,
    ProgressEvent,
    ProgressEventKind,
    RunEventReference,
    RunEventReferenceKind,
)
from software_agent_team.prompting import DynamicUserGuidance
from software_agent_team.run_control import RunPhase
from software_agent_team.teams import AgentCapability, TeamPlan


class RuntimeControlDecision(StrEnum):
    """Scheduling effect of all controls observed at one checkpoint."""

    CONTINUE = "continue"
    HOLD = "hold"
    CANCEL = "cancel"
    CORRECT = "correct"


InterruptAgent = Callable[[str], int]
InterruptAll = Callable[[], int]
PhaseReader = Callable[[], RunPhase]


class RuntimeControlChannel:
    """Resolve one run's local control mailbox without granting model authority."""

    def __init__(
        self,
        *,
        store: ControlCommandStore,
        team_plan: TeamPlan,
        interrupt_agent: InterruptAgent,
        interrupt_all: InterruptAll,
        phase_reader: PhaseReader,
        event_handler: ProgressDraftHandler | None = None,
    ) -> None:
        if store.run_id != team_plan.run_id:
            raise ValueError("control store and TeamPlan use different run IDs")
        self.store = store
        self.team_plan = team_plan
        self.interrupt_agent = interrupt_agent
        self.interrupt_all = interrupt_all
        self.phase_reader = phase_reader
        self.event_handler = event_handler
        self._lock = RLock()
        self._observed: set[str] = set()
        self._guidance: dict[str, list[DynamicUserGuidance]] = {
            agent.id: [] for agent in team_plan.agents
        }
        self._pause_request: ControlCommand | None = None
        self._paused = False
        self._correction_request: ControlCommand | None = None
        self._cancel_request: ControlCommand | None = None
        self._interrupt_requests: dict[str, ControlCommand] = {}

    @property
    def paused(self) -> bool:
        with self._lock:
            return self._paused

    @property
    def correction_instruction(self) -> str | None:
        with self._lock:
            if self._correction_request is None:
                return None
            return self._correction_request.instruction

    def poll(
        self,
        *,
        active_agent_ids: tuple[str, ...],
        pending_agent_ids: tuple[str, ...],
    ) -> RuntimeControlDecision:
        """Apply queued commands and return their current scheduling consequence."""

        active = set(active_agent_ids)
        pending = set(pending_agent_ids)
        known = {agent.id for agent in self.team_plan.agents}
        if not active.issubset(known) or not pending.issubset(known):
            raise ValueError("runtime control checkpoint references unknown Agents")
        if active & pending:
            raise ValueError("an Agent cannot be active and pending at one checkpoint")

        with self._lock:
            phase = self.phase_reader()
            for command in self.store.list_latest():
                if command.status is not ControlCommandStatus.QUEUED:
                    continue
                if command.command_id in self._observed:
                    continue
                self._observed.add(command.command_id)
                self._emit_command(
                    ProgressEventKind.CONTROL_RECEIVED,
                    command,
                    f"Control received: {command.command.value}",
                )
                self._accept_or_reject(command, active, pending, phase)

            self._advance_deferred(active, pending)
            if self._cancel_request is not None:
                if active:
                    self.interrupt_all()
                return RuntimeControlDecision.CANCEL
            if self._correction_request is not None:
                return RuntimeControlDecision.CORRECT
            if self._paused or self._pause_request is not None:
                return RuntimeControlDecision.HOLD
            return RuntimeControlDecision.CONTINUE

    def consume_guidance(self, agent_id: str) -> tuple[DynamicUserGuidance, ...]:
        """Return guidance not yet attached to this Agent's next invocation."""

        with self._lock:
            if agent_id not in self._guidance:
                raise ValueError(f"unknown Agent for guidance: {agent_id}")
            values = tuple(self._guidance[agent_id])
            self._guidance[agent_id].clear()
            return values

    def _accept_or_reject(
        self,
        command: ControlCommand,
        active: set[str],
        pending: set[str],
        phase: RunPhase,
    ) -> None:
        if command.command is ControlCommandType.GUIDE:
            self._apply_guidance(command, active, pending)
            return
        if command.command is ControlCommandType.CORRECT:
            if self._cancel_request is not None:
                self._resolve(
                    command,
                    ControlCommandStatus.REJECTED,
                    "The run is already cancelling.",
                )
            elif self._correction_request is not None:
                self._resolve(
                    command,
                    ControlCommandStatus.SUPERSEDED,
                    "An earlier correction already owns the safe checkpoint.",
                )
            else:
                self._correction_request = command
            return
        if command.command is ControlCommandType.PAUSE:
            if self._cancel_request is not None or self._correction_request is not None:
                self._resolve(
                    command,
                    ControlCommandStatus.REJECTED,
                    "A terminal stop request already owns the run.",
                )
            elif self._paused or self._pause_request is not None:
                self._resolve(
                    command,
                    ControlCommandStatus.SUPERSEDED,
                    "The run is already paused or reaching a pause checkpoint.",
                )
            else:
                self._pause_request = command
            return
        if command.command is ControlCommandType.RESUME:
            self._apply_resume(command, pending)
            return
        if command.command is ControlCommandType.INTERRUPT:
            assert command.target.agent_id is not None
            if command.target.agent_id not in active:
                self._resolve(
                    command,
                    ControlCommandStatus.REJECTED,
                    "The targeted Agent is not currently active.",
                )
            elif command.target.attempt not in {None, 1}:
                self._resolve(
                    command,
                    ControlCommandStatus.REJECTED,
                    "The targeted scheduler attempt is not active.",
                )
            else:
                self._interrupt_requests[command.command_id] = command
            return
        if command.command is ControlCommandType.CANCEL:
            self._apply_cancel(command, active)
            return
        self._resolve(
            command,
            ControlCommandStatus.REJECTED,
            f"Control {command.command.value} is unavailable in {phase.value}.",
        )

    def _apply_guidance(
        self,
        command: ControlCommand,
        active: set[str],
        pending: set[str],
    ) -> None:
        assert command.instruction is not None
        candidates = active | pending
        if command.target.kind is ControlTargetKind.AGENT:
            assert command.target.agent_id is not None
            targets = {command.target.agent_id} & candidates
        elif command.target.kind is ControlTargetKind.FUTURE_WORK:
            targets = set(pending)
        else:
            assert command.target.phase is not None
            targets = {
                agent_id
                for agent_id in candidates
                if self._agent_phase(agent_id) is command.target.phase
            }
        if not targets:
            self._resolve(
                command,
                ControlCommandStatus.REJECTED,
                "No incomplete Agent matches the guidance target.",
            )
            return
        guidance = DynamicUserGuidance(
            command_id=command.command_id,
            instruction=command.instruction,
        )
        for agent_id in sorted(targets):
            self._guidance[agent_id].append(guidance)
        self._resolve(
            command,
            ControlCommandStatus.APPLIED,
            "Guidance was attached to the next invocation of: "
            + ", ".join(sorted(targets)),
        )

    def _apply_resume(self, command: ControlCommand, pending: set[str]) -> None:
        if self._pause_request is not None:
            self._resolve(
                self._pause_request,
                ControlCommandStatus.SUPERSEDED,
                "Resume withdrew the pause before its safe checkpoint.",
            )
            self._pause_request = None
            self._resolve(
                command,
                ControlCommandStatus.APPLIED,
                "The pending pause was withdrawn; scheduling may continue.",
            )
            return
        if not self._paused:
            self._resolve(
                command,
                ControlCommandStatus.REJECTED,
                "The run is not paused.",
            )
            return
        self._paused = False
        resolved = self._resolve(
            command,
            ControlCommandStatus.APPLIED,
            "Evidence remained valid; scheduling resumed.",
        )
        for agent_id in sorted(pending):
            self._emit_agent_state(
                ProgressEventKind.AGENT_RESUMED,
                agent_id,
                resolved,
                "Scheduling resumed; the Agent is waiting for its dependencies.",
            )

    def _apply_cancel(self, command: ControlCommand, active: set[str]) -> None:
        if self._cancel_request is not None:
            self._resolve(
                command,
                ControlCommandStatus.SUPERSEDED,
                "The run is already cancelling.",
            )
            return
        if (
            self._correction_request is not None
            and self._correction_request.status.is_terminal
        ):
            self._resolve(
                command,
                ControlCommandStatus.SUPERSEDED,
                "Replacement Planning already owns the terminal safe checkpoint.",
            )
            return
        if self._pause_request is not None:
            self._resolve(
                self._pause_request,
                ControlCommandStatus.SUPERSEDED,
                "Cancellation superseded the pending pause.",
            )
            self._pause_request = None
        if self._correction_request is not None:
            self._resolve(
                self._correction_request,
                ControlCommandStatus.SUPERSEDED,
                "Cancellation superseded the pending correction.",
            )
            self._correction_request = None
        self._cancel_request = command
        interrupted = self.interrupt_all() if active else 0
        self._resolve(
            command,
            ControlCommandStatus.APPLIED,
            "Cancellation is terminal; new work stopped and "
            f"{interrupted} active invocation(s) received termination.",
            provider_cost_caveat=(
                "Provider usage incurred before termination may remain billable."
            ),
        )

    def _advance_deferred(self, active: set[str], pending: set[str]) -> None:
        for command_id, command in tuple(self._interrupt_requests.items()):
            assert command.target.agent_id is not None
            if command.target.agent_id not in active:
                self._resolve(
                    command,
                    ControlCommandStatus.BEST_EFFORT_FAILED,
                    "The invocation ended before process termination was confirmed.",
                    provider_cost_caveat=(
                        "Provider usage incurred before the request remains billable."
                    ),
                )
                del self._interrupt_requests[command_id]
                continue
            if self.interrupt_agent(command.target.agent_id) > 0:
                self._resolve(
                    command,
                    ControlCommandStatus.APPLIED,
                    "Best-effort termination was sent to the active invocation.",
                    provider_cost_caveat=(
                        "Provider usage incurred before termination may remain "
                        "billable."
                    ),
                )
                del self._interrupt_requests[command_id]

        if self._pause_request is not None and not active:
            command = self._pause_request
            self._pause_request = None
            self._paused = True
            resolved = self._resolve(
                command,
                ControlCommandStatus.APPLIED,
                "The run reached a safe checkpoint; no Agent invocation is active.",
            )
            for agent_id in sorted(pending):
                self._emit_agent_state(
                    ProgressEventKind.AGENT_PAUSED,
                    agent_id,
                    resolved,
                    "The Agent is paused before its next invocation.",
                )
        if self._correction_request is not None and not active:
            command = self._correction_request
            if command.status is ControlCommandStatus.QUEUED:
                self._correction_request = self._resolve(
                    command,
                    ControlCommandStatus.APPLIED,
                    "Scheduling stopped at a safe checkpoint for replacement Planning.",
                )

    def _resolve(
        self,
        command: ControlCommand,
        status: ControlCommandStatus,
        consequence: str,
        *,
        provider_cost_caveat: str | None = None,
    ) -> ControlCommand:
        resolved = self.store.resolve(
            command.command_id,
            expected_revision=command.revision,
            status=status,
            consequence=consequence,
            provider_cost_caveat=provider_cost_caveat,
        )
        kind = (
            ProgressEventKind.CONTROL_APPLIED
            if status is ControlCommandStatus.APPLIED
            else ProgressEventKind.CONTROL_REJECTED
        )
        self._emit_command(
            kind,
            resolved,
            f"Control {status.value}: {command.command.value} — {consequence}",
        )
        return resolved

    def _emit_command(
        self,
        kind: ProgressEventKind,
        command: ControlCommand,
        message: str,
    ) -> None:
        if self.event_handler is None:
            return
        path = f"controls/{command.command_id}/{command.revision:06d}.json"
        self.event_handler(
            ProgressEvent(
                kind=kind,
                message=(" ".join(message.split()) or "Control state changed")[:500],
                control_command_id=command.command_id,
                references=(
                    RunEventReference(
                        kind=RunEventReferenceKind.CONTROL_COMMAND,
                        id=command.command_id,
                        path=path,
                        sha256=canonical_model_sha256(command),
                    ),
                ),
            )
        )

    def _agent_phase(self, agent_id: str) -> RunPhase:
        capability = self.team_plan.get_agent(agent_id).capability
        if capability in {
            AgentCapability.IMPLEMENTATION,
            AgentCapability.INTEGRATION,
        }:
            return RunPhase.IMPLEMENTING
        if capability is AgentCapability.TESTING:
            return RunPhase.VERIFYING
        return RunPhase.REVIEWING

    def _emit_agent_state(
        self,
        kind: ProgressEventKind,
        agent_id: str,
        command: ControlCommand,
        message: str,
    ) -> None:
        if self.event_handler is None:
            return
        agent = self.team_plan.get_agent(agent_id)
        route = self.team_plan.model_routes.get_route(agent.model_route_id)
        self.event_handler(
            ProgressEvent(
                kind=kind,
                message=message,
                agent_id=agent.id,
                capability=agent.capability.value,
                stage_id=agent.stage_id,
                model=route.model,
                dependency_ids=agent.dependencies,
                references=(
                    RunEventReference(
                        kind=RunEventReferenceKind.CONTROL_COMMAND,
                        id=command.command_id,
                        path=(
                            f"controls/{command.command_id}/{command.revision:06d}.json"
                        ),
                        sha256=canonical_model_sha256(command),
                    ),
                ),
            )
        )
