"""Plain-language terminal controls for one active adaptive run."""

from __future__ import annotations

import select
import sys
import threading
from collections.abc import Callable
from typing import TextIO

from software_agent_team.controls import (
    ControlApplicationBoundary,
    ControlCommand,
    ControlCommandStore,
    ControlCommandType,
    ControlTarget,
    ControlTargetKind,
)
from software_agent_team.run_control import RunPhase
from software_agent_team.teams import TeamPlan


class ControlConsoleError(ValueError):
    """Raised when a terminal control command cannot be queued safely."""


type NoticeHandler = Callable[[str], None]
type VisibilityHandler = Callable[[str], None]


def control_help() -> str:
    """Return the concise interactive command guide."""

    return (
        "Controls: /guide <agent|future|phase:name> <instruction>; "
        "/correct <instruction>; /pause; /resume; /interrupt <agent>; "
        "/cancel confirm; /visibility <compact|standard|detailed>; "
        "/controls; /help"
    )


def _queued_message(command: ControlCommand) -> str:
    return (
        f"Queued {command.command.value} ({command.command_id}); "
        f"application boundary: {command.application_boundary.value}."
    )


def submit_control_line(
    line: str,
    *,
    store: ControlCommandStore,
    team_plan: TeamPlan,
    visibility_handler: VisibilityHandler | None = None,
) -> str:
    """Validate one slash command and persist its controller-owned request."""

    value = line.strip()
    if not value:
        return ""
    if not value.startswith("/"):
        raise ControlConsoleError(
            "Run controls begin with '/'. Type /help for examples."
        )
    command, _, remainder = value[1:].partition(" ")
    command = command.lower()
    remainder = remainder.strip()

    if command == "help":
        return control_help()
    if command == "controls":
        latest = store.list_latest()
        if not latest:
            return "No control command has been requested for this run."
        return "Controls: " + "; ".join(
            f"{item.command.value}={item.status.value} ({item.command_id})"
            for item in latest
        )
    if command == "visibility":
        if remainder not in {"compact", "standard", "detailed"}:
            raise ControlConsoleError(
                "Visibility must be compact, standard, or detailed."
            )
        if visibility_handler is None:
            raise ControlConsoleError("This terminal cannot change visibility.")
        visibility_handler(remainder)
        return f"Progress visibility is now {remainder}. Execution was not changed."
    if command == "pause":
        if remainder:
            raise ControlConsoleError("Usage: /pause")
        requested = store.request(
            command=ControlCommandType.PAUSE,
            target=ControlTarget(kind=ControlTargetKind.RUN),
            application_boundary=ControlApplicationBoundary.NEXT_SAFE_CHECKPOINT,
        )
        return _queued_message(requested)
    if command == "resume":
        if remainder:
            raise ControlConsoleError("Usage: /resume")
        requested = store.request(
            command=ControlCommandType.RESUME,
            target=ControlTarget(kind=ControlTargetKind.RUN),
            application_boundary=ControlApplicationBoundary.NEXT_SAFE_CHECKPOINT,
        )
        return _queued_message(requested)
    if command == "cancel":
        if remainder != "confirm":
            return (
                "Cancellation is terminal and active provider usage may remain "
                "billable. Type /cancel confirm to proceed."
            )
        requested = store.request(
            command=ControlCommandType.CANCEL,
            target=ControlTarget(kind=ControlTargetKind.RUN),
            application_boundary=ControlApplicationBoundary.IMMEDIATE,
        )
        return _queued_message(requested)
    if command == "interrupt":
        agent_id = remainder
        if not agent_id or " " in agent_id:
            raise ControlConsoleError("Usage: /interrupt <active-agent-id>")
        _require_agent(team_plan, agent_id)
        requested = store.request(
            command=ControlCommandType.INTERRUPT,
            target=ControlTarget(
                kind=ControlTargetKind.AGENT,
                agent_id=agent_id,
            ),
            application_boundary=ControlApplicationBoundary.IMMEDIATE,
        )
        return _queued_message(requested)
    if command == "correct":
        if not remainder:
            raise ControlConsoleError("Usage: /correct <replacement requirement>")
        requested = store.request(
            command=ControlCommandType.CORRECT,
            instruction=remainder,
            target=ControlTarget(kind=ControlTargetKind.RUN),
            application_boundary=ControlApplicationBoundary.PLANNING_REVISION,
        )
        return _queued_message(requested)
    if command == "guide":
        target_text, separator, instruction = remainder.partition(" ")
        instruction = instruction.strip()
        if not separator or not instruction:
            raise ControlConsoleError(
                "Usage: /guide <agent|future|phase:name> <instruction>"
            )
        if target_text == "future":
            target = ControlTarget(kind=ControlTargetKind.FUTURE_WORK)
        elif target_text.startswith("phase:"):
            phase_text = target_text.removeprefix("phase:")
            try:
                phase = RunPhase(phase_text)
            except ValueError as error:
                raise ControlConsoleError(
                    f"Unknown lifecycle phase: {phase_text}"
                ) from error
            target = ControlTarget(kind=ControlTargetKind.PHASE, phase=phase)
        else:
            _require_agent(team_plan, target_text)
            target = ControlTarget(
                kind=ControlTargetKind.AGENT,
                agent_id=target_text,
            )
        requested = store.request(
            command=ControlCommandType.GUIDE,
            instruction=instruction,
            target=target,
            application_boundary=ControlApplicationBoundary.BEFORE_NEXT_INVOCATION,
        )
        return _queued_message(requested)
    raise ControlConsoleError(f"Unknown run control: /{command}. Type /help.")


def _require_agent(team_plan: TeamPlan, agent_id: str) -> None:
    try:
        team_plan.get_agent(agent_id)
    except ValueError as error:
        known = ", ".join(agent.id for agent in team_plan.agents)
        raise ControlConsoleError(
            f"Unknown Agent '{agent_id}'. Current Agents: {known}."
        ) from error


class TerminalControlConsole:
    """Read optional slash commands without blocking foreground execution."""

    def __init__(
        self,
        *,
        store: ControlCommandStore,
        team_plan: TeamPlan,
        input_stream: TextIO | None = None,
        notice_handler: NoticeHandler | None = None,
        visibility_handler: VisibilityHandler | None = None,
        poll_seconds: float = 0.2,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("control-console polling interval must be positive")
        self.store = store
        self.team_plan = team_plan
        self.input_stream = sys.stdin if input_stream is None else input_stream
        self.notice_handler = notice_handler or (lambda value: print(value, flush=True))
        self.visibility_handler = visibility_handler
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start one daemon reader after the approved run exists."""

        if self._thread is not None:
            raise RuntimeError("the control console is already running")
        self.notice_handler(control_help())
        self._thread = threading.Thread(
            target=self._read_loop,
            name=f"sat-controls-{self.store.run_id}",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        """Stop polling without consuming input from a later Planning session."""

        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(0.5, self.poll_seconds * 2))

    def _read_loop(self) -> None:
        while not self._stop.is_set():
            if not self._input_ready():
                continue
            line = self.input_stream.readline()
            if line == "":
                return
            try:
                message = submit_control_line(
                    line,
                    store=self.store,
                    team_plan=self.team_plan,
                    visibility_handler=self.visibility_handler,
                )
            except (ControlConsoleError, ValueError) as error:
                message = f"Control not queued: {error}"
            if message:
                self.notice_handler(message)

    def _input_ready(self) -> bool:
        try:
            descriptor = self.input_stream.fileno()
        except (AttributeError, OSError):
            return True
        ready, _, _ = select.select([descriptor], [], [], self.poll_seconds)
        return bool(ready)
