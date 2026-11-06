"""Terminal progress presentation and live status cards for Planning."""

from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import TextIO

from wcwidth import wcswidth

from software_agent_team.planning.models_basic import (
    PlanningActivity,
    PlanningActivityKind,
)
from software_agent_team.progress import (
    RunEventVisibility,
    TerminalColorMode,
    TerminalProgressDisplay,
    terminal_color_enabled,
    terminal_presentation_capable,
)
from software_agent_team.terminal_dashboard import TerminalDashboard
from software_agent_team.terminal_metrics import TerminalRunMetrics
from software_agent_team.terminal_presentation import (
    InvocationPresentation,
    colorize_metrics,
)


@dataclass
class _PlanningProgressObservation:
    """Current user-facing Planning state; never lifecycle authority."""

    activity: PlanningActivity
    started: float
    message: str
    attempt_label: str
    stop: threading.Event
    thread: threading.Thread | None = None


class TerminalPlanningProgress:
    """Show semantic Planning history plus one bounded live state card."""

    def __init__(
        self,
        *,
        write: Callable[[str], None] = print,
        output: TextIO | None = None,
        visibility: RunEventVisibility | str = RunEventVisibility.STANDARD,
        display: TerminalProgressDisplay | str = TerminalProgressDisplay.AUTO,
        color: TerminalColorMode | str = TerminalColorMode.AUTO,
        heartbeat_seconds: float = 10.0,
        live_refresh_seconds: float = 1.0,
        monotonic: Callable[[], float] = time.monotonic,
        environment: Mapping[str, str] | None = None,
        is_terminal: bool | None = None,
        terminal_width: Callable[[], int] | None = None,
        metrics: TerminalRunMetrics | None = None,
    ) -> None:
        if heartbeat_seconds <= 0:
            raise ValueError("Planning heartbeat must be positive")
        if live_refresh_seconds <= 0:
            raise ValueError("Planning live refresh must be positive")
        self.output = sys.stdout if output is None and write is print else output
        self.write = (
            (lambda value: print(value, file=self.output, flush=True))
            if output is not None and write is print
            else write
        )
        self.visibility = RunEventVisibility(visibility)
        self.display = TerminalProgressDisplay(display)
        self.color = TerminalColorMode(color)
        self.heartbeat_seconds = heartbeat_seconds
        self.live_refresh_seconds = min(heartbeat_seconds, live_refresh_seconds)
        self.monotonic = monotonic
        self.environment = os.environ if environment is None else environment
        self.terminal_width = terminal_width
        self.metrics = metrics
        self._presentation: InvocationPresentation | None = None
        self.live_enabled = bool(
            self.output is not None
            and self.display is not TerminalProgressDisplay.LOG
            and terminal_presentation_capable(
                self.output,
                environment=self.environment,
                is_terminal=is_terminal,
            )
        )
        self.color_enabled = bool(
            self.output is not None
            and terminal_color_enabled(
                self.output,
                self.color,
                environment=self.environment,
                is_terminal=is_terminal,
            )
        )
        self.unicode_enabled = self._output_supports_unicode()
        self._lock = threading.Lock()
        self._waiting: _PlanningProgressObservation | None = None
        self._live_line_count = 0
        self._dashboard: TerminalDashboard | None = None

    def __call__(self, activity: PlanningActivity) -> None:
        if activity.presentation is not None:
            self._presentation = activity.presentation
            if self.metrics is not None:
                self.metrics.observe("planner", activity.model, activity.presentation)
        if activity.kind is PlanningActivityKind.WAITING_MODEL:
            attempt_label = (
                str(activity.attempt)
                if activity.maximum_attempts is None
                else f"{activity.attempt}/{activity.maximum_attempts}"
            )
            self._start_waiting(
                activity,
                visible=(
                    f"● Planning invocation is queued for {activity.model} "
                    f"(attempt {attempt_label})"
                ),
                heartbeat=(f"Planning invocation is queued (attempt {attempt_label})"),
            )
            return

        if activity.kind in {
            PlanningActivityKind.INITIALIZING,
            PlanningActivityKind.PROVIDER_WAIT,
            PlanningActivityKind.TOOL_ACTIVE,
            PlanningActivityKind.FINALIZING_RESPONSE,
        }:
            label = {
                PlanningActivityKind.INITIALIZING: (
                    "Planning is initializing an attributable OpenClaw turn"
                ),
                PlanningActivityKind.PROVIDER_WAIT: (
                    f"Planning is waiting for {activity.model}"
                ),
                PlanningActivityKind.TOOL_ACTIVE: (
                    "Planning has attributable tool operations active"
                ),
                PlanningActivityKind.FINALIZING_RESPONSE: (
                    "Planning received the terminal model response and OpenClaw "
                    "is finalizing the result"
                ),
            }[activity.kind]
            self._start_waiting(
                activity,
                visible=f"● {label}",
                heartbeat=label,
            )
            return

        if activity.kind in {
            PlanningActivityKind.STOPPING,
            PlanningActivityKind.COLLECTING_EVIDENCE,
            PlanningActivityKind.STOPPED,
        }:
            self.close()
            reason = (
                "unknown"
                if activity.stop_reason is None
                else activity.stop_reason.value
            )
            if activity.kind is PlanningActivityKind.STOPPING:
                message = (
                    f"■ Planning is stopping ({reason}); shutdown ceiling "
                    f"{(activity.shutdown_grace_seconds or 0):g}s"
                )
            elif activity.kind is PlanningActivityKind.COLLECTING_EVIDENCE:
                message = "… Planning process stopped; collecting attributable evidence"
            else:
                message = (
                    f"■ Planning invocation stopped ({reason}); process outcome "
                    "and evidence are known"
                )
            if self.visibility is RunEventVisibility.DETAILED:
                self._print(message)
            return

        checkpoint = (
            "unknown"
            if activity.initialization_checkpoint is None
            else activity.initialization_checkpoint.value
        )
        intermediate: str | None = None
        if activity.kind is PlanningActivityKind.INVOCATION_LAUNCHED:
            intermediate = "  Planning entered the execution adapter"
        elif activity.kind is PlanningActivityKind.INITIALIZATION_PROGRESS:
            intermediate = f"  Planning initialization verified {checkpoint}"
        elif activity.kind is PlanningActivityKind.INITIALIZATION_LIVENESS_DEGRADED:
            intermediate = (
                "! Planning initialization observer is unavailable: "
                f"{activity.degradation_reason}"
            )
        elif activity.kind is PlanningActivityKind.INITIALIZATION_STALL_SUSPECTED:
            intermediate = (
                "? Planning initialization has made no progress for "
                f"{(activity.inactivity_ms or 0) / 1000:.1f}s; waiting the final "
                f"{(activity.stall_grace_seconds or 0):g}s diagnostic window"
            )
        elif activity.kind is PlanningActivityKind.INITIALIZATION_STALL_RECOVERED:
            intermediate = (
                f"↻ Planning initialization advanced to {checkpoint} during grace"
            )
        elif activity.kind is PlanningActivityKind.INITIALIZATION_STALLED:
            intermediate = (
                "! Planning initialization remained stalled; a typed stop follows"
            )
        elif activity.kind is PlanningActivityKind.PROVIDER_ACTIVITY:
            intermediate = "  Planning received provider stream activity"
        elif activity.kind is PlanningActivityKind.FINALIZATION_PROGRESS:
            intermediate = "  Planning result finalization made observable progress"
        elif activity.kind is PlanningActivityKind.FINALIZATION_STALL_SUSPECTED:
            intermediate = (
                "? Planning result finalization has made no observable progress for "
                f"{(activity.inactivity_ms or 0) / 1000:.1f}s; waiting the final "
                f"{(activity.stall_grace_seconds or 0):g}s diagnostic window"
            )
        elif activity.kind is PlanningActivityKind.FINALIZATION_STALL_RECOVERED:
            intermediate = "↻ Planning result finalization resumed during grace"
        elif activity.kind is PlanningActivityKind.RESPONSE_FINALIZATION_STALLED:
            intermediate = (
                "! Planning received the terminal model response, but OpenClaw "
                "result finalization remained stalled; a typed stop follows"
            )
        elif activity.kind is PlanningActivityKind.TOOL_STARTED:
            action = (
                "using"
                if activity.tool_action_class is None
                else activity.tool_action_class.value
            )
            target = (
                "a sandboxed tool operation"
                if activity.tool_target_class is None
                else activity.tool_target_class.value.replace("_", " ")
            )
            detail = (
                "" if activity.tool_detail is None else f" ({activity.tool_detail})"
            )
            intermediate = f"  Planning started {action} {target}{detail}"
        elif activity.kind is PlanningActivityKind.TOOL_COMPLETED:
            action = (
                "using"
                if activity.tool_action_class is None
                else activity.tool_action_class.value
            )
            target = (
                "a sandboxed tool operation"
                if activity.tool_target_class is None
                else activity.tool_target_class.value.replace("_", " ")
            )
            detail = (
                "" if activity.tool_detail is None else f" ({activity.tool_detail})"
            )
            intermediate = f"  Planning completed {action} {target}{detail}"
        elif activity.kind is PlanningActivityKind.LIVENESS_DEGRADED:
            intermediate = (
                "! Planning provider liveness is degraded: "
                f"{activity.degradation_reason}; SAT will preserve the call "
                "instead of inferring a stall from silence"
            )
        elif activity.kind is PlanningActivityKind.STALL_SUSPECTED:
            intermediate = (
                "? Planning has produced no trusted activity for "
                f"{(activity.inactivity_ms or 0) / 1000:.1f}s; checking the "
                "private stream and attributable tool state for another "
                f"{(activity.stall_grace_seconds or 0):g}s before interruption "
                f"({activity.policy_source})"
            )
        elif activity.kind is PlanningActivityKind.STALL_RECOVERED:
            intermediate = (
                "↻ Planning provider activity recovered during the "
                f"{(activity.stall_grace_seconds or 0):g}s grace period"
            )
        elif activity.kind is PlanningActivityKind.PROVIDER_STALLED:
            intermediate = (
                "! Planning provider remained silent for "
                f"{(activity.silence_seconds or 0):g}s; stall confirmed and the "
                "separate stopping transition follows"
            )
        if intermediate is not None:
            if self.visibility is RunEventVisibility.DETAILED or activity.kind in {
                PlanningActivityKind.INITIALIZATION_LIVENESS_DEGRADED,
                PlanningActivityKind.INITIALIZATION_STALL_SUSPECTED,
                PlanningActivityKind.INITIALIZATION_STALL_RECOVERED,
                PlanningActivityKind.INITIALIZATION_STALLED,
                PlanningActivityKind.FINALIZATION_STALL_SUSPECTED,
                PlanningActivityKind.FINALIZATION_STALL_RECOVERED,
                PlanningActivityKind.RESPONSE_FINALIZATION_STALLED,
                PlanningActivityKind.LIVENESS_DEGRADED,
                PlanningActivityKind.STALL_SUSPECTED,
                PlanningActivityKind.STALL_RECOVERED,
                PlanningActivityKind.PROVIDER_STALLED,
            }:
                self._print(intermediate)
            return

        if activity.kind is PlanningActivityKind.BUDGET_UPDATED:
            assert activity.budget_usage is not None
            assert activity.budget_ceiling_usd is not None
            usage = activity.budget_usage
            remaining = max(
                Decimal(0),
                activity.budget_ceiling_usd - usage.known_estimated_cost_usd,
            )
            source = (
                "unknown"
                if activity.pricing_source is None
                else activity.pricing_source.value
            )
            if self.visibility is RunEventVisibility.DETAILED:
                message = (
                    "  Task model spend: "
                    f"${usage.known_estimated_cost_usd:.6f} estimated / "
                    f"${activity.budget_ceiling_usd} authorized; "
                    f"${remaining:.6f} recorded remaining; price source {source}"
                )
            else:
                message = (
                    "  Planning spend: "
                    f"${usage.known_estimated_cost_usd:.6f} of "
                    f"${activity.budget_ceiling_usd} authorized "
                    f"· ${remaining:.6f} remaining"
                )
            self._print(message)
            return

        self.close()
        if activity.kind is PlanningActivityKind.RESPONSE_RECEIVED:
            duration = 0 if activity.duration_ms is None else activity.duration_ms
            status = (
                "unknown"
                if activity.execution_status is None
                else activity.execution_status.value
            )
            self._print(
                f"→ Planning response received in {duration / 1000:.1f}s ({status})"
            )
        elif activity.kind is PlanningActivityKind.CORRECTION_SCHEDULED:
            next_attempt = activity.attempt + 1
            attempt_label = (
                str(next_attempt)
                if activity.maximum_attempts is None
                else f"{next_attempt}/{activity.maximum_attempts}"
            )
            reasons = ", ".join(activity.correction_reasons) or "proposal validation"
            self._print(f"↻ Planning correction attempt {attempt_label}: {reasons}")
        elif activity.kind is PlanningActivityKind.PROPOSAL_REGENERATION_SCHEDULED:
            next_attempt = activity.attempt + 1
            attempt_label = (
                str(next_attempt)
                if activity.maximum_attempts is None
                else f"{next_attempt}/{activity.maximum_attempts}"
            )
            self._print(
                "↻ Planning proposal has widespread structural errors; "
                f"requesting one fresh proposal as attempt {attempt_label}"
            )
        elif activity.kind is PlanningActivityKind.SUBMISSION_EVIDENCE_RECOVERY:
            next_attempt = activity.attempt + 1
            attempt_label = (
                str(next_attempt)
                if activity.maximum_attempts is None
                else f"{next_attempt}/{activity.maximum_attempts}"
            )
            self._print(
                "↻ Planning response produced no attributable typed submission; "
                f"reissuing the same request once as attempt {attempt_label}"
            )
        elif activity.kind is PlanningActivityKind.MODEL_ROUTE_SWITCHED:
            self._print(
                "↻ Planning provider route failed with settled usage and no "
                f"accepted submission; switching to {activity.model}"
            )
        elif activity.kind is PlanningActivityKind.CLARIFICATION_SCHEDULED:
            dimension = (
                activity.clarification_dimension.value
                if activity.clarification_dimension is not None
                else (
                    "a product requirement"
                    if activity.clarification_category is None
                    else activity.clarification_category.value.replace("_", " ")
                )
            )
            self._print(
                "↻ Planning proposal needs a user decision; "
                f"requesting one focused {dimension} question"
            )
        else:
            self._print("✓ Planning response validated")

    def close(self) -> None:
        with self._lock:
            waiting = self._waiting
            self._waiting = None
            self._clear_live_locked()
        if waiting is not None:
            waiting.stop.set()
            assert waiting.thread is not None
            waiting.thread.join(
                timeout=min(
                    self.live_refresh_seconds
                    if self.live_enabled
                    else self.heartbeat_seconds,
                    0.2,
                )
            )

    def _start_waiting(
        self,
        activity: PlanningActivity,
        *,
        visible: str,
        heartbeat: str,
    ) -> None:
        attempt_label = (
            str(activity.attempt)
            if activity.maximum_attempts is None
            else f"{activity.attempt}/{activity.maximum_attempts}"
        )
        show_visible = not self.live_enabled and (
            self.visibility is RunEventVisibility.DETAILED
            or activity.kind
            in {
                PlanningActivityKind.WAITING_MODEL,
                PlanningActivityKind.PROVIDER_WAIT,
                PlanningActivityKind.TOOL_ACTIVE,
                PlanningActivityKind.FINALIZING_RESPONSE,
            }
        )
        with self._lock:
            previous = self._waiting
            if (
                previous is not None
                and activity.kind is not PlanningActivityKind.WAITING_MODEL
                and previous.activity.attempt == activity.attempt
                and previous.activity.model == activity.model
            ):
                changed = (
                    previous.activity.kind != activity.kind
                    or previous.message != heartbeat
                    or previous.attempt_label != attempt_label
                )
                if self.live_enabled and changed:
                    self._clear_live_locked()
                previous.activity = activity
                previous.message = heartbeat
                previous.attempt_label = attempt_label
                if self.live_enabled and changed:
                    self._draw_live_locked()
                reused = True
            else:
                reused = False
        if reused:
            if show_visible:
                self._print(visible)
            return

        self.close()
        stop = threading.Event()
        started = self.monotonic()
        observation = _PlanningProgressObservation(
            activity=activity,
            started=started,
            message=heartbeat,
            attempt_label=attempt_label,
            stop=stop,
        )
        observation.thread = threading.Thread(
            target=self._heartbeat,
            args=(observation,),
            name="sat-planning-progress",
            daemon=True,
        )
        with self._lock:
            self._waiting = observation
            if self.live_enabled:
                self._draw_live_locked()
        if show_visible:
            self._print(visible)
        observation.thread.start()

    def _heartbeat(
        self,
        observation: _PlanningProgressObservation,
    ) -> None:
        interval = (
            self.live_refresh_seconds if self.live_enabled else self.heartbeat_seconds
        )
        while not observation.stop.wait(interval):
            if self.live_enabled:
                with self._lock:
                    if self._waiting is not observation:
                        return
                    self._clear_live_locked()
                    self._draw_live_locked()
                continue
            elapsed = max(0, int(self.monotonic() - observation.started))
            minutes, seconds = divmod(elapsed, 60)
            self._print(
                f"  {observation.message} "
                f"(attempt {observation.attempt_label}): "
                f"{minutes:02d}:{seconds:02d} elapsed"
            )

    def _print(self, value: str) -> None:
        with self._lock:
            if self._dashboard is not None:
                self._dashboard.write(self._colorize(value, self._line_color(value)))
                self._dashboard.invalidate()
                return
            self._clear_live_locked()
            self.write(self._colorize(value, self._line_color(value)))
            self._draw_live_locked()

    def _draw_live_locked(self) -> None:
        if self._dashboard is not None:
            self._dashboard.invalidate()
            return
        self._write_live_lines_locked()

    def attach_dashboard(self, dashboard: TerminalDashboard | None) -> None:
        with self._lock:
            self._clear_live_locked()
            self._dashboard = dashboard

    def live_lines(self) -> list[str]:
        with self._lock:
            return self._live_lines_locked()

    def detail_lines(self) -> list[str]:
        with self._lock:
            return self._presentation_lines_locked()

    def _presentation_lines_locked(self) -> list[str]:
        if (
            not self.live_enabled
            or self.visibility is not RunEventVisibility.DETAILED
            or self._presentation is None
        ):
            return []
        detail = self._presentation
        lines = [self._colorize("Planning details", "1;36")]
        if detail.tool is not None:
            lines.append(
                self._colorize(
                    self._fit(
                        "Tool / command: " + detail.tool.label, self._terminal_columns()
                    ),
                    "35",
                )
            )
            lines.extend("  " + line for line in detail.tool.arguments.splitlines()[:1])
            lines.append(self._colorize("Result / output:", "34"))
            lines.extend(
                "  " + line
                for line in detail.tool.result.splitlines()[:1]
                or [
                    "completed; no text output"
                    if detail.tool.completed
                    else "waiting for the tool result"
                ]
            )
        lines.append(
            self._colorize(
                "Model " + ("stream:" if detail.streaming else "output:"), "36"
            )
        )
        lines.extend(
            "  " + line
            for line in detail.model_text.splitlines()[:2]
            or ["no visible text reported"]
        )
        return [
            self._fit(line, self._terminal_columns()) if "\x1b" not in line else line
            for line in lines
        ]

    def _write_live_lines_locked(self) -> None:
        lines = self._live_lines_locked()
        if self.output is None:
            return
        for line in lines:
            self.output.write(line + "\n")
        self.output.flush()
        self._live_line_count = len(lines)

    def _live_lines_locked(self) -> list[str]:
        if not self.live_enabled:
            return []
        if self._waiting is None:
            return (
                colorize_metrics(
                    (
                        self._fit(line, self._terminal_columns())
                        for line in self.metrics()
                    ),
                    color=self.color_enabled,
                )
                if self.metrics is not None
                and self.visibility is not RunEventVisibility.COMPACT
                else []
            )
        assert self.output is not None
        observation = self._waiting
        elapsed = max(0, int(self.monotonic() - observation.started))
        minutes, seconds = divmod(elapsed, 60)
        activity = observation.activity
        state = {
            PlanningActivityKind.WAITING_MODEL: "queued",
            PlanningActivityKind.INITIALIZING: "initializing",
            PlanningActivityKind.PROVIDER_WAIT: "waiting for model",
            PlanningActivityKind.TOOL_ACTIVE: "using tools",
            PlanningActivityKind.FINALIZING_RESPONSE: "finalizing response",
        }.get(activity.kind, "working")
        symbol = "●" if self.unicode_enabled else "*"
        branch = "├─" if self.unicode_enabled else "|-"
        end_branch = "└─" if self.unicode_enabled else "`-"
        dot = "·" if self.unicode_enabled else "|"
        width = self._terminal_columns()
        lines = [
            "",
            self._colorize(
                self._fit(
                    f"{symbol} Planning  {state}  {minutes:02d}:{seconds:02d}",
                    width,
                ),
                "1;33"
                if activity.kind
                in {
                    PlanningActivityKind.PROVIDER_WAIT,
                    PlanningActivityKind.WAITING_MODEL,
                }
                else "1;36",
            ),
            self._fit(
                f"  {branch} attempt {observation.attempt_label} {dot} "
                f"{activity.model}",
                width,
            ),
            self._colorize(
                self._fit(f"  {end_branch} {observation.message}", width),
                "2",
            ),
        ]
        if (
            self.visibility is not RunEventVisibility.COMPACT
            and self.metrics is not None
        ):
            lines = [
                *colorize_metrics(
                    (self._fit(line, width) for line in self.metrics()),
                    color=self.color_enabled,
                ),
                *lines,
            ]
        if self._dashboard is None:
            lines.extend(self._presentation_lines_locked())
        return [
            self._fit(line, width) if "\x1b" not in line else line for line in lines
        ]

    def _clear_live_locked(self) -> None:
        if not self.live_enabled or self._live_line_count == 0:
            return
        assert self.output is not None
        line_count = self._live_line_count
        self.output.write(f"\x1b[{line_count}A")
        for _ in range(line_count):
            self.output.write("\r\x1b[2K\x1b[1B")
        self.output.write(f"\x1b[{line_count}A")
        self.output.flush()
        self._live_line_count = 0

    def _colorize(self, value: str, code: str) -> str:
        if not self.color_enabled:
            return value
        return f"\x1b[{code}m{value}\x1b[0m"

    def _terminal_columns(self) -> int:
        if self.terminal_width is not None:
            return max(20, self.terminal_width())
        assert self.output is not None
        try:
            return max(20, os.get_terminal_size(self.output.fileno()).columns)
        except (AttributeError, OSError):
            return 80

    def _fit(self, value: str, width: int) -> str:
        if wcswidth(value) <= width:
            return value
        marker = "…" if self.unicode_enabled else "..."
        target = max(1, width - wcswidth(marker))
        result: list[str] = []
        used = 0
        for character in value:
            character_width = max(0, wcswidth(character))
            if used + character_width > target:
                break
            result.append(character)
            used += character_width
        return "".join(result) + marker

    def _output_supports_unicode(self) -> bool:
        if self.output is None:
            return True
        encoding = getattr(self.output, "encoding", None) or "utf-8"
        try:
            "●├─└─·…".encode(encoding)
        except (LookupError, UnicodeEncodeError):
            return False
        return True

    @staticmethod
    def _line_color(value: str) -> str:
        stripped = value.lstrip()
        if stripped.startswith(("✓", "→")):
            return "32"
        if stripped.startswith("!"):
            return "31"
        if stripped.startswith(("?", "↻")):
            return "33"
        if stripped.startswith(("■", "…")):
            return "36"
        return "2" if value.startswith("  ") else "36"
