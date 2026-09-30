"""Phase-appropriate Planning controls using the shared terminal owner."""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable, Mapping
from typing import TextIO

from software_agent_team.control_console import ControlCompleter, ControlConsoleError
from software_agent_team.controls import (
    ControlApplicationBoundary,
    ControlCommand,
    ControlCommandStatus,
    ControlCommandStore,
    ControlCommandType,
    ControlTarget,
    ControlTargetKind,
)
from software_agent_team.planning import (
    AdaptivePlanningCoordinator,
    ApprovedPlanningResult,
    InputReader,
    OutputWriter,
    PlanningActivity,
    PlanningError,
    PlanningRequest,
    PlanningSessionStatus,
    TerminalPlanningProgress,
    _run_interactive_planning,
)
from software_agent_team.progress import (
    RunEventVisibility,
    TerminalColorMode,
    TerminalProgressDisplay,
    terminal_presentation_capable,
)
from software_agent_team.terminal_dashboard import TerminalDashboard
from software_agent_team.terminal_metrics import TerminalRunMetrics


def run_interactive_planning(
    coordinator: AdaptivePlanningCoordinator,
    request: PlanningRequest,
    *,
    read: InputReader = input,
    read_text: InputReader | None = None,
    write: OutputWriter = print,
    output: TextIO | None = None,
    progress_visibility: RunEventVisibility | str = RunEventVisibility.STANDARD,
    progress_display: TerminalProgressDisplay | str = TerminalProgressDisplay.AUTO,
    progress_color: TerminalColorMode | str = TerminalColorMode.AUTO,
    environment: Mapping[str, str] | None = None,
    is_terminal: bool | None = None,
    metrics: TerminalRunMetrics | None = None,
    visibility_handler: Callable[[str], None] | None = None,
) -> ApprovedPlanningResult | None:
    """Share one editor between Planning answers and phase-appropriate controls."""

    progress = TerminalPlanningProgress(
        write=write,
        output=output,
        visibility=progress_visibility,
        display=progress_display,
        color=progress_color,
        environment=environment,
        is_terminal=is_terminal,
        metrics=metrics,
    )
    dashboard: TerminalDashboard | None = None
    cancelled = threading.Event()
    approval_lock = threading.RLock()
    cancel_control: tuple[ControlCommandStore, ControlCommand] | None = None

    def stop_calls() -> None:
        for executor in (
            coordinator.executor,
            *(route.executor for route in coordinator.fallback_routes),
        ):
            interrupt = getattr(executor, "interrupt_all", None)
            if interrupt is not None:
                interrupt()

    def submit(line: str) -> str:
        nonlocal cancel_control
        command, _, argument = line.strip().partition(" ")
        if command == "/help":
            return (
                "Planning controls (Tab completes):\n"
                "  /status                            Current Planning state\n"
                "  /visibility compact|standard|detailed  Display detail\n"
                "  /cancel confirm                    Stop Planning\n"
                "Execution guidance and pause/resume become available after approval."
            )
        if command == "/status":
            return (
                "\n".join(progress.live_lines())
                or "Planning is waiting for your answer."
            )
        if command == "/visibility":
            try:
                progress.visibility = RunEventVisibility(argument.strip())
            except ValueError as error:
                raise ControlConsoleError(
                    "Use /visibility compact, standard, or detailed."
                ) from error
            if visibility_handler is not None:
                visibility_handler(progress.visibility.value)
            return f"Planning visibility is now {progress.visibility.value}."
        if command == "/cancel":
            if argument.strip() != "confirm":
                return (
                    "Use /cancel confirm to stop Planning. "
                    "Incurred provider usage remains billable."
                )
            with approval_lock:
                if cancelled.is_set():
                    return "Planning cancellation is already in progress."
                session = coordinator.store.load_session(request.run_id)
                if session.status is PlanningSessionStatus.APPROVED:
                    raise ControlConsoleError(
                        "This plan is already approved; use execution controls."
                    )
                store = ControlCommandStore(
                    coordinator.store.root / request.run_id,
                    run_id=request.run_id,
                )
                control = store.request(
                    command=ControlCommandType.CANCEL,
                    target=ControlTarget(kind=ControlTargetKind.RUN),
                    application_boundary=ControlApplicationBoundary.IMMEDIATE,
                )
                cancel_control = store, control
                cancelled.set()
            stop_calls()
            if dashboard is not None:
                dashboard.cancel_answer()
            return "Stopping Planning and collecting invocation evidence…"
        raise ControlConsoleError(
            "This command is unavailable during Planning. Use /help."
        )

    def observe(activity: PlanningActivity) -> None:
        progress(activity)
        if cancelled.is_set():
            stop_calls()

    def read_answer(prompt: str, *, multiline: bool = False) -> str:
        if cancelled.is_set():
            raise KeyboardInterrupt
        assert dashboard is not None
        value = dashboard.read(prompt, multiline=multiline)
        if cancelled.is_set():
            raise KeyboardInterrupt
        return value

    options = dict(
        read=read,
        read_text=read_text,
        write=write,
        output=output,
        progress_visibility=progress_visibility,
        progress_display=progress_display,
        progress_color=progress_color,
        environment=environment,
        is_terminal=is_terminal,
        progress=progress,
    )
    capable = (
        output is not None
        and output.isatty()
        and sys.stdin.isatty()
        and terminal_presentation_capable(output, environment=environment)
    )
    if capable:
        dashboard = TerminalDashboard(
            submit=submit,
            completer=ControlCompleter(planning=True),
            live_lines=progress.live_lines,
            detail_lines=progress.detail_lines,
            input_stream=sys.stdin,
            output_stream=output,
            color=progress.color_enabled,
        )
        dashboard.start()
        progress.attach_dashboard(dashboard)
        options.update(
            read=read_answer,
            read_text=lambda prompt: read_answer(prompt, multiline=True),
            write=dashboard.write,
        )
    try:
        return _run_interactive_planning(
            coordinator,
            request,
            activity_handler=observe,
            cancellation_requested=cancelled.is_set,
            approval_guard=approval_lock,
            **options,
        )
    except (KeyboardInterrupt, EOFError, PlanningError):
        if not cancelled.is_set():
            raise
        coordinator.store.cancel(request.run_id, now=coordinator.clock())
        if cancel_control is not None:
            store, control = cancel_control
            store.resolve(
                control.command_id,
                expected_revision=control.revision,
                status=ControlCommandStatus.APPLIED,
                consequence="Planning cancelled after invocation evidence collection.",
                provider_cost_caveat="Incurred provider usage remains billable.",
            )
        progress._print("Planning cancelled; no runtime Agent was created.")
        return None
    finally:
        progress.close()
        if dashboard is not None:
            dashboard.close()
            progress.attach_dashboard(None)
