"""Controller-backed progress events and terminal rendering."""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TextIO

from software_agent_team.artifacts import AgentRole, IterationDecision
from software_agent_team.run_control import RunPhase


class ProgressEventKind(StrEnum):
    """Stable user-safe workflow events emitted by the controller."""

    RUN_STARTED = "run_started"
    WORKSPACE_READY = "workspace_ready"
    AGENT_STARTED = "agent_started"
    AGENT_COMPLETED = "agent_completed"
    AGENT_RETRY = "agent_retry"
    SNAPSHOT_VERIFIED = "snapshot_verified"
    QUALITY_GATES_STARTED = "quality_gates_started"
    QUALITY_GATE_COMPLETED = "quality_gate_completed"
    DECISION_RECORDED = "decision_recorded"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"


@dataclass(frozen=True)
class ProgressEvent:
    """One attributable event derived from controller-owned state or evidence."""

    kind: ProgressEventKind
    message: str
    phase: RunPhase | None = None
    role: AgentRole | None = None
    iteration: int | None = None
    attempt: int | None = None
    duration_ms: int | None = None
    completed: int | None = None
    total: int | None = None
    changed_files: tuple[str, ...] = ()
    decision: IterationDecision | None = None


ProgressHandler = Callable[[ProgressEvent], None]


class TerminalProgressRenderer:
    """Render safe summaries and elapsed waiting time without model reasoning."""

    def __init__(
        self,
        *,
        output: TextIO | None = None,
        heartbeat_seconds: float = 10.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if heartbeat_seconds <= 0:
            raise ValueError("progress heartbeat must be positive")
        self.output = sys.stdout if output is None else output
        self.heartbeat_seconds = heartbeat_seconds
        self.monotonic = monotonic
        self._lock = threading.Lock()
        self._waiting: dict[
            tuple[AgentRole, int, int], tuple[threading.Event, threading.Thread]
        ] = {}

    def __call__(self, event: ProgressEvent) -> None:
        """Render one event and manage any matching elapsed-time heartbeat."""

        if event.kind is ProgressEventKind.AGENT_STARTED:
            self._start_waiting(event)
            return
        if event.kind in {
            ProgressEventKind.AGENT_COMPLETED,
            ProgressEventKind.AGENT_RETRY,
        }:
            self._stop_waiting(event)

        symbol = {
            ProgressEventKind.RUN_STARTED: "●",
            ProgressEventKind.WORKSPACE_READY: "✓",
            ProgressEventKind.AGENT_COMPLETED: "✓",
            ProgressEventKind.AGENT_RETRY: "↻",
            ProgressEventKind.SNAPSHOT_VERIFIED: "✓",
            ProgressEventKind.QUALITY_GATES_STARTED: "●",
            ProgressEventKind.QUALITY_GATE_COMPLETED: "✓",
            ProgressEventKind.DECISION_RECORDED: "✓",
            ProgressEventKind.RUN_COMPLETED: "✓",
            ProgressEventKind.RUN_FAILED: "✗",
        }[event.kind]
        self._print(f"{symbol} {event.message}")
        if event.kind in {
            ProgressEventKind.RUN_COMPLETED,
            ProgressEventKind.RUN_FAILED,
        }:
            self.close()

    def close(self) -> None:
        """Stop every outstanding heartbeat thread."""

        with self._lock:
            waiting = tuple(self._waiting.values())
            self._waiting.clear()
        for stop, thread in waiting:
            stop.set()
            thread.join(timeout=min(self.heartbeat_seconds, 0.2))

    def _key(self, event: ProgressEvent) -> tuple[AgentRole, int, int] | None:
        if event.role is None or event.iteration is None or event.attempt is None:
            return None
        return event.role, event.iteration, event.attempt

    def _start_waiting(self, event: ProgressEvent) -> None:
        key = self._key(event)
        if key is None:
            self._print(f"● {event.message}")
            return
        self._print(f"● {event.message}")
        stop = threading.Event()
        started = self.monotonic()
        thread = threading.Thread(
            target=self._heartbeat,
            args=(stop, started, event.message),
            name=f"sat-progress-{event.role.value}",
            daemon=True,
        )
        with self._lock:
            previous = self._waiting.pop(key, None)
            self._waiting[key] = (stop, thread)
        if previous is not None:
            previous[0].set()
        thread.start()

    def _stop_waiting(self, event: ProgressEvent) -> None:
        key = self._key(event)
        if key is None:
            return
        with self._lock:
            waiting = self._waiting.pop(key, None)
        if waiting is not None:
            waiting[0].set()
            waiting[1].join(timeout=min(self.heartbeat_seconds, 0.2))

    def _heartbeat(
        self,
        stop: threading.Event,
        started: float,
        message: str,
    ) -> None:
        while not stop.wait(self.heartbeat_seconds):
            elapsed = max(0, int(self.monotonic() - started))
            minutes, seconds = divmod(elapsed, 60)
            self._print(f"  {message} {minutes:02d}:{seconds:02d} elapsed")

    def _print(self, value: str) -> None:
        with self._lock:
            print(value, file=self.output, flush=True)
