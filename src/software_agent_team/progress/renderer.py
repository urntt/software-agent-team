"""Terminal progress renderer and heartbeat observation."""

from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import ClassVar, TextIO

from wcwidth import wcswidth

from software_agent_team.integrity import canonical_model_sha256
from software_agent_team.invocation_lifecycle import InvocationPhase
from software_agent_team.terminal_dashboard import TerminalDashboard
from software_agent_team.terminal_metrics import TerminalRunMetrics
from software_agent_team.terminal_presentation import (
    InvocationPresentation,
    colorize_metrics,
)

from .display import terminal_color_enabled, terminal_presentation_capable
from .models import (
    _INVOCATION_EVENT_KINDS,
    RunEvent,
    _current_agent_state,
)
from .types import (
    _VISIBILITY_RANK,
    DEFAULT_LIVE_REFRESH_SECONDS,
    DEFAULT_PROGRESS_HEARTBEAT_SECONDS,
    AgentRunState,
    ProgressEventKind,
    RunEventCategory,
    RunEventVisibility,
    TerminalColorMode,
    TerminalProgressDisplay,
)


@dataclass
class _HeartbeatObservation:
    """Latest rendered observation and its phase clock; not lifecycle authority."""

    event: RunEvent
    started: float
    stop: threading.Event
    thread: threading.Thread | None = None


class TerminalProgressRenderer:
    """Render append-only logs or a bounded, TTY-aware live progress panel."""

    _TRANSIENT_LIVE_KINDS: ClassVar[frozenset[ProgressEventKind]] = frozenset(
        {
            ProgressEventKind.AGENT_STARTED,
            ProgressEventKind.AGENT_INVOCATION_LAUNCHED,
            ProgressEventKind.AGENT_INITIALIZING,
            ProgressEventKind.AGENT_INITIALIZATION_PROGRESS,
            ProgressEventKind.AGENT_WAITING_PROVIDER,
            ProgressEventKind.AGENT_PROVIDER_ACTIVITY,
            ProgressEventKind.AGENT_TOOL_ACTIVE,
            ProgressEventKind.AGENT_TOOL_STARTED,
            ProgressEventKind.AGENT_TOOL_COMPLETED,
            ProgressEventKind.AGENT_FINALIZING_RESPONSE,
            ProgressEventKind.AGENT_FINALIZATION_PROGRESS,
            ProgressEventKind.AGENT_STOPPING,
            ProgressEventKind.AGENT_COLLECTING_EVIDENCE,
            ProgressEventKind.AGENT_STOPPED,
            ProgressEventKind.AGENT_INVOCATION_COMPLETED,
        }
    )
    _SUCCESS_KINDS: ClassVar[frozenset[ProgressEventKind]] = frozenset(
        {
            ProgressEventKind.WORKSPACE_READY,
            ProgressEventKind.AGENT_TOOL_COMPLETED,
            ProgressEventKind.AGENT_COMPLETED,
            ProgressEventKind.SNAPSHOT_VERIFIED,
            ProgressEventKind.QUALITY_GATE_COMPLETED,
            ProgressEventKind.QUALITY_GATE_PASSED,
            ProgressEventKind.DECISION_RECORDED,
            ProgressEventKind.CONTROL_APPLIED,
            ProgressEventKind.RUN_COMPLETED,
        }
    )
    _FAILURE_KINDS: ClassVar[frozenset[ProgressEventKind]] = frozenset(
        {
            ProgressEventKind.AGENT_INITIALIZATION_STALLED,
            ProgressEventKind.AGENT_RESPONSE_FINALIZATION_STALLED,
            ProgressEventKind.AGENT_PROVIDER_STALLED,
            ProgressEventKind.AGENT_FAILED,
            ProgressEventKind.QUALITY_GATE_FAILED,
            ProgressEventKind.RUN_FAILED,
        }
    )
    _WARNING_KINDS: ClassVar[frozenset[ProgressEventKind]] = frozenset(
        {
            ProgressEventKind.AGENT_INITIALIZATION_LIVENESS_DEGRADED,
            ProgressEventKind.AGENT_INITIALIZATION_STALL_SUSPECTED,
            ProgressEventKind.AGENT_FINALIZATION_STALL_SUSPECTED,
            ProgressEventKind.AGENT_LIVENESS_DEGRADED,
            ProgressEventKind.AGENT_STALL_SUSPECTED,
            ProgressEventKind.AGENT_SKIPPED,
            ProgressEventKind.CONTROL_REJECTED,
        }
    )
    _UNICODE_SYMBOLS: ClassVar[dict[ProgressEventKind, str]] = {
        ProgressEventKind.RUN_STARTED: "●",
        ProgressEventKind.WORKSPACE_READY: "✓",
        ProgressEventKind.AGENT_QUEUED: "○",
        ProgressEventKind.AGENT_READY: "→",
        ProgressEventKind.AGENT_STARTED: "●",
        ProgressEventKind.AGENT_WAITING_PROVIDER: "●",
        ProgressEventKind.AGENT_INVOCATION_LAUNCHED: "●",
        ProgressEventKind.AGENT_INITIALIZING: "●",
        ProgressEventKind.AGENT_INITIALIZATION_PROGRESS: "·",
        ProgressEventKind.AGENT_INITIALIZATION_LIVENESS_DEGRADED: "!",
        ProgressEventKind.AGENT_INITIALIZATION_STALL_SUSPECTED: "?",
        ProgressEventKind.AGENT_INITIALIZATION_STALL_RECOVERED: "↻",
        ProgressEventKind.AGENT_INITIALIZATION_STALLED: "!",
        ProgressEventKind.AGENT_INVOCATION_COMPLETED: "·",
        ProgressEventKind.AGENT_PROVIDER_ACTIVITY: "·",
        ProgressEventKind.AGENT_TOOL_ACTIVE: "●",
        ProgressEventKind.AGENT_TOOL_STARTED: "●",
        ProgressEventKind.AGENT_TOOL_COMPLETED: "✓",
        ProgressEventKind.AGENT_FINALIZING_RESPONSE: "…",
        ProgressEventKind.AGENT_FINALIZATION_PROGRESS: "·",
        ProgressEventKind.AGENT_FINALIZATION_STALL_SUSPECTED: "?",
        ProgressEventKind.AGENT_FINALIZATION_STALL_RECOVERED: "↻",
        ProgressEventKind.AGENT_RESPONSE_FINALIZATION_STALLED: "!",
        ProgressEventKind.AGENT_LIVENESS_DEGRADED: "!",
        ProgressEventKind.AGENT_STALL_SUSPECTED: "?",
        ProgressEventKind.AGENT_STALL_RECOVERED: "↻",
        ProgressEventKind.AGENT_PROVIDER_STALLED: "!",
        ProgressEventKind.AGENT_STOPPING: "■",
        ProgressEventKind.AGENT_COLLECTING_EVIDENCE: "…",
        ProgressEventKind.AGENT_STOPPED: "■",
        ProgressEventKind.MODEL_ROUTE_SWITCHED: "⇄",
        ProgressEventKind.AGENT_COMPLETED: "✓",
        ProgressEventKind.AGENT_RETRY: "↻",
        ProgressEventKind.AGENT_FAILED: "✗",
        ProgressEventKind.AGENT_SKIPPED: "!",
        ProgressEventKind.AGENT_PAUSED: "Ⅱ",
        ProgressEventKind.AGENT_RESUMED: "▶",
        ProgressEventKind.AGENT_INTERRUPTED: "■",
        ProgressEventKind.AGENT_CANCELLED: "■",
        ProgressEventKind.SNAPSHOT_VERIFIED: "✓",
        ProgressEventKind.QUALITY_GATES_STARTED: "●",
        ProgressEventKind.QUALITY_GATE_COMPLETED: "✓",
        ProgressEventKind.QUALITY_GATE_PASSED: "✓",
        ProgressEventKind.QUALITY_GATE_FAILED: "✗",
        ProgressEventKind.DECISION_RECORDED: "✓",
        ProgressEventKind.CONTROL_RECEIVED: "◆",
        ProgressEventKind.CONTROL_APPLIED: "✓",
        ProgressEventKind.CONTROL_REJECTED: "!",
        ProgressEventKind.RUN_COMPLETED: "✓",
        ProgressEventKind.RUN_FAILED: "✗",
        ProgressEventKind.RUN_CANCELLED: "■",
    }
    _CATEGORY_LABELS: ClassVar[dict[RunEventCategory, str]] = {
        RunEventCategory.LIFECYCLE: "run",
        RunEventCategory.AGENT: "agent",
        RunEventCategory.GIT: "git",
        RunEventCategory.QUALITY_GATE: "check",
        RunEventCategory.DECISION: "control",
    }

    def __init__(
        self,
        *,
        output: TextIO | None = None,
        visibility: RunEventVisibility = RunEventVisibility.STANDARD,
        display: TerminalProgressDisplay | str = TerminalProgressDisplay.AUTO,
        color: TerminalColorMode | str = TerminalColorMode.AUTO,
        heartbeat_seconds: float = DEFAULT_PROGRESS_HEARTBEAT_SECONDS,
        live_refresh_seconds: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        environment: Mapping[str, str] | None = None,
        is_terminal: bool | None = None,
        terminal_width: Callable[[], int] | None = None,
        metrics: TerminalRunMetrics | None = None,
    ) -> None:
        if heartbeat_seconds <= 0:
            raise ValueError("progress heartbeat must be positive")
        if live_refresh_seconds is not None and live_refresh_seconds <= 0:
            raise ValueError("live progress refresh must be positive")
        self.output = sys.stdout if output is None else output
        self.visibility = visibility
        self.display = TerminalProgressDisplay(display)
        self.color = TerminalColorMode(color)
        self.heartbeat_seconds = heartbeat_seconds
        self.live_refresh_seconds = (
            min(heartbeat_seconds, DEFAULT_LIVE_REFRESH_SECONDS)
            if live_refresh_seconds is None
            else live_refresh_seconds
        )
        self.monotonic = monotonic
        self.environment = os.environ if environment is None else environment
        terminal_capable = terminal_presentation_capable(
            self.output,
            environment=self.environment,
            is_terminal=is_terminal,
        )
        self.live_enabled = (
            self.display is not TerminalProgressDisplay.LOG and terminal_capable
        )
        self.color_enabled = terminal_color_enabled(
            self.output,
            self.color,
            environment=self.environment,
            is_terminal=is_terminal,
        )
        self.unicode_enabled = self._output_supports_unicode()
        self.terminal_width = terminal_width
        self.metrics = metrics
        self._presentations: dict[str, tuple[str | None, InvocationPresentation]] = {}
        self._lock = threading.Lock()
        self._waiting: dict[tuple[str, int, int], _HeartbeatObservation] = {}
        self._rendered_checkpoint_digests: dict[tuple[str, int, int], str] = {}
        self._live_line_count = 0
        self._dashboard: TerminalDashboard | None = None
        self._live_suspensions = 0
        self._pending_interaction_lines: list[str] = []
        self._last_live_elapsed_signature: tuple[
            tuple[tuple[str, int, int], int], ...
        ] = ()

    def __call__(self, event: RunEvent) -> None:
        """Render one persisted event and manage its elapsed-time heartbeat."""

        visible = self._event_visible(event)
        if (
            self.metrics is not None
            and event.agent_id is not None
            and (
                event.presentation is not None
                or event.kind
                in {
                    ProgressEventKind.AGENT_STARTED,
                    ProgressEventKind.AGENT_INVOCATION_LAUNCHED,
                }
            )
        ):
            self.metrics.observe(event.agent_id, event.model, event.presentation)
        with self._lock:
            if event.agent_id is not None and event.presentation is not None:
                previous = self._presentations.get(event.agent_id)
                presentation = event.presentation
                if (
                    presentation.tool is None
                    and previous is not None
                    and presentation.session_id is not None
                    and previous[1].session_id == presentation.session_id
                ):
                    presentation = presentation.model_copy(
                        update={"tool": previous[1].tool}
                    )
                self._presentations[event.agent_id] = (event.model, presentation)
                self._detail_agent = event.agent_id
            elif (
                event.agent_id is not None
                and event.kind is ProgressEventKind.AGENT_INVOCATION_LAUNCHED
            ):
                self._presentations[event.agent_id] = (
                    event.model,
                    InvocationPresentation(),
                )
                self._detail_agent = event.agent_id
        live_state_changed = False
        if event.kind is ProgressEventKind.AGENT_INVOCATION_COMPLETED:
            live_state_changed = self._stop_waiting(event)
        elif event.kind in {
            ProgressEventKind.AGENT_COMPLETED,
            ProgressEventKind.AGENT_FAILED,
            ProgressEventKind.AGENT_SKIPPED,
            ProgressEventKind.AGENT_INTERRUPTED,
            ProgressEventKind.AGENT_CANCELLED,
            ProgressEventKind.AGENT_STOPPING,
            ProgressEventKind.AGENT_COLLECTING_EVIDENCE,
            ProgressEventKind.AGENT_STOPPED,
        }:
            live_state_changed = self._stop_agent_waiting(event)
        elif event.kind in {
            ProgressEventKind.AGENT_RETRY,
            ProgressEventKind.AGENT_PAUSED,
            ProgressEventKind.AGENT_RESUMED,
        }:
            live_state_changed = self._stop_waiting(event)
        elif event.checkpoint is not None and event.kind in _INVOCATION_EVENT_KINDS:
            if event.checkpoint.invocation_phase in {
                InvocationPhase.STOPPING,
                InvocationPhase.COLLECTING_EVIDENCE,
                InvocationPhase.STOPPED,
            }:
                live_state_changed = self._stop_waiting(event)
            else:
                live_state_changed = self._start_waiting(event)
        elif event.kind in {
            ProgressEventKind.AGENT_STARTED,
            ProgressEventKind.AGENT_INITIALIZING,
            ProgressEventKind.AGENT_WAITING_PROVIDER,
            ProgressEventKind.AGENT_TOOL_ACTIVE,
            ProgressEventKind.AGENT_TOOL_STARTED,
            ProgressEventKind.AGENT_FINALIZING_RESPONSE,
        }:
            live_state_changed = self._start_waiting(event)

        if self.live_enabled and event.kind in self._TRANSIENT_LIVE_KINDS:
            if self.visibility is not RunEventVisibility.COMPACT and event.kind in {
                ProgressEventKind.AGENT_TOOL_STARTED,
                ProgressEventKind.AGENT_TOOL_COMPLETED,
            }:
                self._print(self._event_line(event))
            if live_state_changed or event.presentation is not None:
                self._redraw_live()
            return
        if not visible:
            if self.live_enabled and live_state_changed:
                self._redraw_live()
            return

        lines = [self._event_line(event), *self._detail_lines(event)]
        self._print_block(lines)
        if event.kind in {
            ProgressEventKind.RUN_COMPLETED,
            ProgressEventKind.RUN_FAILED,
            ProgressEventKind.RUN_CANCELLED,
        }:
            self.close()

    def close(self) -> None:
        """Stop every outstanding heartbeat thread and remove the live region."""

        with self._lock:
            waiting = tuple(self._waiting.values())
            self._waiting.clear()
            self._last_live_elapsed_signature = ()
            self._clear_live_locked()
        for observation in waiting:
            self._join_waiting(observation)

    def set_visibility(self, visibility: RunEventVisibility | str) -> None:
        """Change rendering detail without changing controller execution."""

        resolved = RunEventVisibility(visibility)
        with self._lock:
            self.visibility = resolved
            self._rendered_checkpoint_digests.clear()
            if self.live_enabled:
                self._clear_live_locked()
                self._draw_live_locked()

    def suspend_live(self) -> None:
        """Pause live redraw while another terminal interaction owns the cursor."""

        with self._lock:
            self._live_suspensions += 1
            if self._live_suspensions == 1:
                self._clear_live_locked()

    def attach_dashboard(self, dashboard: TerminalDashboard | None) -> None:
        """Delegate cursor ownership to the persistent shared editor."""

        with self._lock:
            self._clear_live_locked()
            self._dashboard = dashboard

    def live_lines(self) -> list[str]:
        with self._lock:
            return self._live_lines_locked()

    def detail_lines(self) -> list[str]:
        with self._lock:
            if (
                not self.live_enabled
                or self.visibility is not RunEventVisibility.DETAILED
                or not self._presentations
            ):
                return []
            agent_id = self._detail_agent
            model, presentation = self._presentations[agent_id]
            return self._presentation_lines(
                agent_id, model, presentation, self._terminal_columns()
            )

    def resume_live(self) -> None:
        """Flush progress after the interactive editor releases the cursor."""

        with self._lock:
            if self._live_suspensions == 0:
                raise RuntimeError("progress live rendering is not suspended")
            self._live_suspensions -= 1
            if self._live_suspensions == 0:
                for line in self._pending_interaction_lines:
                    print(line, file=self.output)
                self._pending_interaction_lines.clear()
                self.output.flush()
                self._draw_live_locked()

    def write_notice(self, value: str) -> None:
        """Print an interaction notice without racing a progress heartbeat."""

        cleaned = " ".join(value.split())
        if cleaned:
            self._print(cleaned)

    def _event_visible(self, event: RunEvent) -> bool:
        return _VISIBILITY_RANK[event.minimum_visibility] <= min(
            2, _VISIBILITY_RANK[self.visibility] + 1
        )

    def _key(self, event: RunEvent) -> tuple[str, int, int] | None:
        if event.agent_id is None or event.iteration is None or event.attempt is None:
            return None
        return event.agent_id, event.iteration, event.attempt

    def _start_waiting(self, event: RunEvent) -> bool:
        key = self._key(event)
        if key is None:
            return False
        with self._lock:
            previous = self._waiting.get(key)
            if previous is not None:
                previous_state = _current_agent_state(
                    previous.event.kind, previous.event.checkpoint
                )
                current_state = _current_agent_state(event.kind, event.checkpoint)
                changed = previous_state != current_state
                if self._heartbeat_summary(previous.event) != self._heartbeat_summary(
                    event
                ):
                    previous.started = self.monotonic()
                previous.event = event
                return changed
            observation = _HeartbeatObservation(
                event=event, started=self.monotonic(), stop=threading.Event()
            )
            observation.thread = threading.Thread(
                target=self._heartbeat,
                args=(key, observation.stop),
                name=f"sat-progress-{event.agent_id}",
                daemon=True,
            )
            self._waiting[key] = observation
            observation.thread.start()
            return True

    def _stop_waiting(self, event: RunEvent) -> bool:
        key = self._key(event)
        if key is None:
            return False
        with self._lock:
            waiting = self._waiting.pop(key, None)
            self._rendered_checkpoint_digests.pop(key, None)
        if waiting is not None:
            self._join_waiting(waiting)
        return waiting is not None

    def _stop_agent_waiting(self, event: RunEvent) -> bool:
        if event.agent_id is None or event.iteration is None:
            return False
        with self._lock:
            keys = tuple(
                key
                for key in self._waiting
                if key[:2] == (event.agent_id, event.iteration)
            )
            waiting = tuple(self._waiting.pop(key) for key in keys)
            for key in keys:
                self._rendered_checkpoint_digests.pop(key, None)
        for observation in waiting:
            self._join_waiting(observation)
        return bool(waiting)

    def _join_waiting(self, observation: _HeartbeatObservation) -> None:
        observation.stop.set()
        assert observation.thread is not None
        observation.thread.join(timeout=min(self.heartbeat_seconds, 0.2))

    @staticmethod
    def _heartbeat_summary(event: RunEvent) -> str:
        assert event.agent_id is not None
        state = _current_agent_state(event.kind, event.checkpoint)
        if state in {AgentRunState.LAUNCHED, AgentRunState.INITIALIZING}:
            return f"{event.agent_id} is initializing its invocation"
        if state is AgentRunState.WAITING_PROVIDER:
            return f"{event.agent_id} is waiting for the model"
        if state is AgentRunState.TOOL_ACTIVE:
            return f"{event.agent_id} has attributable tool operations active"
        if state is AgentRunState.FINALIZING_RESPONSE:
            return f"{event.agent_id} is finalizing the OpenClaw result"
        return f"{event.agent_id} is working"

    def _heartbeat(
        self,
        key: tuple[str, int, int],
        stop: threading.Event,
    ) -> None:
        interval = (
            self.live_refresh_seconds if self.live_enabled else self.heartbeat_seconds
        )
        while not stop.wait(interval):
            with self._lock:
                observation = self._waiting.get(key)
                if observation is None or observation.stop is not stop:
                    return
                if self.live_enabled:
                    signature = self._live_elapsed_signature_locked()
                    if signature == self._last_live_elapsed_signature:
                        continue
                    self._clear_live_locked()
                    self._draw_live_locked()
                    continue
                elapsed = max(0, int(self.monotonic() - observation.started))
                minutes, seconds = divmod(elapsed, 60)
                message = self._heartbeat_summary(observation.event)
                print(
                    f"  {message} {minutes:02d}:{seconds:02d} elapsed",
                    file=self.output,
                    flush=True,
                )

    def _event_line(self, event: RunEvent) -> str:
        symbol = (
            self._UNICODE_SYMBOLS[event.kind]
            if self.unicode_enabled
            else self._ascii_symbol(event.kind)
        )
        summary = event.summary
        if (
            event.presentation is not None
            and event.presentation.tool is not None
            and event.kind
            in {
                ProgressEventKind.AGENT_TOOL_STARTED,
                ProgressEventKind.AGENT_TOOL_COMPLETED,
            }
        ):
            verb = (
                "started"
                if event.kind is ProgressEventKind.AGENT_TOOL_STARTED
                else "finished"
            )
            summary = f"{event.agent_id} {verb} {event.presentation.tool.label}"
        if self.live_enabled:
            label = self._CATEGORY_LABELS[event.category]
            value = f"{symbol} [{label}] {summary}"
        else:
            value = f"{symbol} {summary}"
        return self._colorize(value, self._event_color(event.kind))

    def _ascii_symbol(self, kind: ProgressEventKind) -> str:
        if kind in self._SUCCESS_KINDS:
            return "ok"
        if kind in self._FAILURE_KINDS:
            return "x"
        if kind in self._WARNING_KINDS:
            return "!"
        return "*"

    def _event_color(self, kind: ProgressEventKind) -> str:
        if kind in self._SUCCESS_KINDS:
            return "32"
        if kind in self._FAILURE_KINDS:
            return "31"
        if kind in self._WARNING_KINDS:
            return "33"
        if kind in {
            ProgressEventKind.AGENT_CANCELLED,
            ProgressEventKind.AGENT_INTERRUPTED,
            ProgressEventKind.RUN_CANCELLED,
        }:
            return "35"
        return "36"

    def _detail_lines(self, event: RunEvent) -> list[str]:
        lines: list[str] = []
        if event.checkpoint is not None and self._checkpoint_details_changed(event):
            checkpoint = event.checkpoint
            task_ids = ",".join(checkpoint.approved_task_ids) or "none"
            lines.append(
                "  progress "
                f"phase={checkpoint.invocation_phase.value} tasks={task_ids} "
                f"completed_tools={checkpoint.completed_tool_operations} "
                f"completed={checkpoint.last_verified_checkpoint}; "
                f"next={checkpoint.next_controller_checkpoint}"
            )
            if checkpoint.review_coverage_state != "not_applicable":
                criteria = ",".join(checkpoint.review_criterion_ids)
                lines.append(
                    "  review coverage "
                    f"state={checkpoint.review_coverage_state} criteria={criteria}; "
                    "tool activity alone does not establish criterion coverage"
                )
            if checkpoint.cost_accounting_state == "settled":
                cost_detail = (
                    f"${checkpoint.remaining_estimated_cost_usd:.6f} recorded remaining"
                )
            elif checkpoint.cost_accounting_state == "active_unsettled":
                cost_detail = (
                    f"{checkpoint.unsettled_model_calls} active model call(s) are "
                    "unsettled; arithmetic headroom is not confirmed remaining budget"
                )
            else:
                cost_detail = (
                    "terminal provider usage is incomplete; arithmetic headroom is "
                    "not confirmed remaining budget"
                )
            lines.append(
                "  task budget "
                f"${checkpoint.known_estimated_cost_usd:.6f} settled estimate / "
                f"${checkpoint.authorized_cost_usd} authorized; {cost_detail}"
            )
        if self.visibility is RunEventVisibility.COMPACT:
            return [self._colorize(line, "2") for line in lines]
        if event.agent_id is not None:
            fields = [f"agent={event.agent_id}"]
            state = _current_agent_state(event.kind, event.checkpoint)
            if state is not None:
                fields.append(f"state={state.value}")
            if event.capability is not None:
                fields.append(f"capability={event.capability}")
            if event.specialization is not None:
                fields.append(f"specialization={event.specialization}")
            if event.stage_id is not None:
                fields.append(f"stage={event.stage_id}")
            if event.model is not None:
                fields.append(f"model={event.model}")
            if event.attempt is not None:
                fields.append(f"attempt={event.attempt}")
            if event.duration_ms is not None:
                fields.append(f"duration_ms={event.duration_ms}")
            dependencies = ",".join(event.dependency_ids) or "none"
            fields.append(f"dependencies={dependencies}")
            lines.append("  " + " ".join(fields))
        if event.budget_usage is not None:
            usage = event.budget_usage
            lines.append(
                "  budget "
                f"calls_completed={usage.calls_completed} "
                f"calls_started={usage.calls_started} "
                f"calls_active={usage.active_calls} input={usage.input_tokens} "
                f"output={usage.output_tokens} "
                f"duration_ms={usage.agent_duration_ms} "
                f"known_cost_usd={usage.known_estimated_cost_usd} "
                f"unpriced_calls={usage.unpriced_calls}"
            )
        return [self._colorize(line, "2") for line in lines]

    def _checkpoint_details_changed(self, event: RunEvent) -> bool:
        """Suppress repeated projections while retaining every persisted event."""

        key = self._key(event)
        assert event.checkpoint is not None
        if key is None:
            return True
        digest = canonical_model_sha256(event.checkpoint)
        with self._lock:
            previous = self._rendered_checkpoint_digests.get(key)
            self._rendered_checkpoint_digests[key] = digest
        return previous != digest

    def _redraw_live(self) -> None:
        if not self.live_enabled:
            return
        with self._lock:
            self._clear_live_locked()
            self._draw_live_locked()

    def _draw_live_locked(self) -> None:
        if self._dashboard is not None:
            self._dashboard.invalidate()
            return
        if not self.live_enabled or self._live_suspensions:
            return
        lines = self._live_lines_locked()
        for line in lines:
            self.output.write(line + "\n")
        self.output.flush()
        self._live_line_count = len(lines)
        self._last_live_elapsed_signature = self._live_elapsed_signature_locked()

    def _clear_live_locked(self) -> None:
        if not self.live_enabled or self._live_line_count == 0:
            return
        line_count = self._live_line_count
        self.output.write(f"\x1b[{line_count}A")
        for _ in range(line_count):
            self.output.write("\r\x1b[2K\x1b[1B")
        # Clearing walks to the row below the live panel. Return to the first
        # cleared row so the next panel or permanent event reuses those rows
        # instead of turning every refresh into blank scrollback.
        self.output.write(f"\x1b[{line_count}A")
        self.output.flush()
        self._live_line_count = 0

    def _live_lines_locked(self) -> list[str]:
        if not self.live_enabled:
            return []
        now = self.monotonic()
        width = self._terminal_columns()
        blocks: list[list[str]] = []
        branch = "├─" if self.unicode_enabled else "|-"
        end_branch = "└─" if self.unicode_enabled else "`-"
        dot = "·" if self.unicode_enabled else "|"
        for key in sorted(self._waiting):
            observation = self._waiting[key]
            event = observation.event
            elapsed = max(0, int(now - observation.started))
            minutes, seconds = divmod(elapsed, 60)
            state = _current_agent_state(event.kind, event.checkpoint)
            state_text = "working" if state is None else state.value.replace("_", " ")
            stage = "" if event.stage_id is None else f" [{event.stage_id}]"
            status = (
                f"{
                    self._UNICODE_SYMBOLS[event.kind]
                    if self.unicode_enabled
                    else self._ascii_symbol(event.kind)
                } "
                f"{event.agent_id}{stage}  {state_text}  {minutes:02d}:{seconds:02d}"
            )
            block = [
                self._colorize(
                    self._fit(status, width),
                    "1;33"
                    if state
                    in {
                        AgentRunState.WAITING_PROVIDER,
                        AgentRunState.WAITING_DEPENDENCY,
                        AgentRunState.PAUSED,
                    }
                    else "1;" + self._event_color(event.kind),
                ),
                self._fit(f"  {branch} {event.summary}", width),
            ]
            checkpoint = event.checkpoint
            if checkpoint is None:
                block.append(
                    self._colorize(
                        self._fit(f"  {end_branch} waiting for next checkpoint", width),
                        "2",
                    )
                )
                blocks.append(block)
                continue
            tasks = len(checkpoint.approved_task_ids)
            coverage = (
                ""
                if checkpoint.review_coverage_state == "not_applicable"
                else f" {dot} review {checkpoint.review_coverage_state}"
            )
            cost = f"${checkpoint.known_estimated_cost_usd:.3f}"
            detail = (
                f"  {end_branch} {checkpoint.invocation_phase.value} {dot} "
                f"{tasks} task(s) {dot} {checkpoint.completed_tool_operations} tool(s)"
                f"{coverage} {dot} {cost} settled"
            )
            block.append(self._colorize(self._fit(detail, width), "2"))
            if self.visibility is not RunEventVisibility.COMPACT:
                block.append(
                    self._colorize(
                        self._fit(
                            f"     next: {checkpoint.next_controller_checkpoint}",
                            width,
                        ),
                        "2",
                    )
                )
            blocks.append(block)
        lines: list[str] = []
        for block in blocks:
            if lines:
                lines.append("")
            lines.extend(block)
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
        if self.visibility is RunEventVisibility.DETAILED and self._dashboard is None:
            for agent_id, (model, presentation) in sorted(self._presentations.items()):
                lines.extend(
                    self._presentation_lines(agent_id, model, presentation, width)
                )
        if lines:
            lines.insert(0, "")
        return lines

    def _presentation_lines(
        self,
        agent_id: str,
        model: str | None,
        presentation: InvocationPresentation,
        width: int,
    ) -> list[str]:
        lines = [
            self._colorize(
                f"Details · {agent_id} · {model or 'model unavailable'}", "1;36"
            ),
        ]
        if presentation.tool is not None:
            tool = presentation.tool
            lines.append(
                self._colorize(
                    self._fit("  Tool / command: " + tool.label, width), "35"
                )
            )
            for line in tool.arguments.splitlines()[:1]:
                lines.append(self._fit("    " + line, width))
            lines.append(self._colorize("  Result / command output:", "34"))
            for line in tool.result.splitlines()[:1] or [
                "completed; no text output"
                if tool.completed
                else "waiting for the tool result"
            ]:
                lines.append(self._fit("    " + line, width))
        lines.append(
            self._colorize(
                "  Model " + ("stream:" if presentation.streaming else "output:"), "36"
            )
        )
        for line in presentation.model_text.splitlines()[:2] or [
            "no visible text reported"
        ]:
            lines.append(self._fit("    " + line, width))
        return lines

    def _live_elapsed_signature_locked(
        self,
    ) -> tuple[tuple[tuple[str, int, int], int], ...]:
        now = self.monotonic()
        return tuple(
            (key, max(0, int(now - observation.started)))
            for key, observation in sorted(self._waiting.items())
        )

    def _print_block(self, lines: list[str]) -> None:
        with self._lock:
            if self._dashboard is not None:
                self._dashboard.write("\n".join(lines))
                self._dashboard.invalidate()
                return
            if self._live_suspensions:
                self._pending_interaction_lines.extend(lines)
                return
            self._clear_live_locked()
            for line in lines:
                print(line, file=self.output)
            self.output.flush()
            self._draw_live_locked()

    def _print(self, value: str) -> None:
        self._print_block([value])

    def _colorize(self, value: str, code: str) -> str:
        if not self.color_enabled:
            return value
        return f"\x1b[{code}m{value}\x1b[0m"

    def _terminal_columns(self) -> int:
        if self.terminal_width is not None:
            return max(20, self.terminal_width())
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

    def _output_is_terminal(self) -> bool:
        try:
            return bool(self.output.isatty())
        except (AttributeError, OSError):
            return False

    def _output_supports_unicode(self) -> bool:
        encoding = getattr(self.output, "encoding", None) or "utf-8"
        try:
            "✓⚙…└─".encode(encoding)
        except (LookupError, UnicodeEncodeError):
            return False
        return True
