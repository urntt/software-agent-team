"""Plain-language terminal controls for one active adaptive run."""

from __future__ import annotations

import select
import sys
import threading
from collections.abc import Callable
from typing import TextIO

from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document

from software_agent_team.controls import (
    ControlApplicationBoundary,
    ControlCommand,
    ControlCommandStore,
    ControlCommandType,
    ControlTarget,
    ControlTargetKind,
)
from software_agent_team.progress import terminal_presentation_capable
from software_agent_team.run_control import RunPhase
from software_agent_team.teams import TeamPlan
from software_agent_team.terminal_dashboard import TerminalDashboard


class ControlConsoleError(ValueError):
    """Raised when a terminal control command cannot be queued safely."""


type NoticeHandler = Callable[[str], None]
type VisibilityHandler = Callable[[str], None]
_COMMAND_GUIDE = {
    "help": ("", "Show this command guide"),
    "visibility": ("compact|standard|detailed", "Change the whole terminal display"),
    "controls": ("", "List requests and their controller status"),
    "guide": ("agent|future|phase:name instruction", "Guide future work"),
    "correct": ("instruction", "Request a Planning revision"),
    "pause": ("", "Pause at the next safe checkpoint"),
    "resume": ("", "Resume paused scheduling"),
    "interrupt": ("agent", "Stop one active Agent"),
    "cancel": ("confirm", "Stop the run; requires confirmation"),
}


class ControlCompleter(Completer):
    """Complete commands and finite arguments without submitting input."""

    def __init__(self, *, agents: tuple[str, ...] = (), planning: bool = False):
        self.agents = agents
        self.planning = planning

    def get_completions(self, document: Document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        command, separator, arguments = text.partition(" ")
        if not separator:
            names = (
                ("help", "status", "visibility", "cancel")
                if self.planning
                else _COMMAND_GUIDE
            )
            for name in names:
                candidate = "/" + name
                if candidate.startswith(command):
                    description = (
                        "Show Planning status"
                        if name == "status"
                        else _COMMAND_GUIDE[name][1]
                    )
                    yield Completion(
                        candidate,
                        start_position=-len(command),
                        display_meta=description,
                    )
            return
        if " " in arguments:
            return
        choices = {
            "/visibility": ("compact", "standard", "detailed"),
            "/cancel": ("confirm",),
            "/interrupt": self.agents,
            "/guide": (
                *self.agents,
                "future",
                *("phase:" + phase.value for phase in RunPhase),
            ),
        }.get(command, ())
        for choice in choices:
            if choice.startswith(arguments):
                yield Completion(choice, start_position=-len(arguments))


def control_help() -> str:
    """Return the interactive command guide from the completion registry."""

    return "Controls (Tab completes commands and targets):\n" + "\n".join(
        f"  /{command} {usage:<35} {description}"
        for command, (usage, description) in _COMMAND_GUIDE.items()
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
        output_stream: TextIO | None = None,
        live_lines: Callable[[], list[str]] | None = None,
        detail_lines: Callable[[], list[str]] | None = None,
        color: bool = True,
        poll_seconds: float = 0.2,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("control-console polling interval must be positive")
        self.store = store
        self.team_plan = team_plan
        self.input_stream = sys.stdin if input_stream is None else input_stream
        self.notice_handler = notice_handler or (lambda value: print(value, flush=True))
        self.visibility_handler = visibility_handler
        self.output_stream = sys.stdout if output_stream is None else output_stream
        self.live_lines = live_lines or (lambda: [])
        self.detail_lines = detail_lines
        self.color = color
        self.dashboard: TerminalDashboard | None = None
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start one daemon reader after the approved run exists."""

        if self._thread is not None:
            raise RuntimeError("the control console is already running")
        if self._is_interactive_terminal():
            self.dashboard = TerminalDashboard(
                submit=self._handle_line,
                completer=ControlCompleter(
                    agents=tuple(agent.id for agent in self.team_plan.agents)
                ),
                live_lines=self.live_lines,
                detail_lines=self.detail_lines,
                input_stream=self.input_stream,
                output_stream=self.output_stream,
                color=self.color,
            )
            self.dashboard.start()
            self._thread = self.dashboard._thread
            return
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
        if self.dashboard is not None:
            self.dashboard.close()
            return
        if self._thread is not None:
            self._thread.join(timeout=max(2.0, self.poll_seconds * 2))

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

    def _handle_line(self, line: str) -> str:
        return submit_control_line(
            line,
            store=self.store,
            team_plan=self.team_plan,
            visibility_handler=self.visibility_handler,
        )

    def _is_interactive_terminal(self) -> bool:
        try:
            return (
                self.input_stream.isatty()
                and self.input_stream.fileno() >= 0
                and terminal_presentation_capable(self.output_stream)
            )
        except (AttributeError, OSError):
            return False

    def _input_ready(self) -> bool:
        try:
            descriptor = self.input_stream.fileno()
        except (AttributeError, OSError):
            return True
        ready, _, _ = select.select([descriptor], [], [], self.poll_seconds)
        return bool(ready)
