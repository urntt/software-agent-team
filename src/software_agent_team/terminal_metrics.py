"""Read-only, independently refreshed run telemetry for terminal presentation."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from software_agent_team.budgets import AgentBudgetLedger
from software_agent_team.git_workspace import GitWorkspaceError, GitWorkspaceManager
from software_agent_team.terminal_presentation import InvocationPresentation


class TerminalRunMetrics:
    """Keep display observations separate from budget and Git authority."""

    def __init__(
        self,
        *,
        ledger: AgentBudgetLedger,
        model_windows: Mapping[str, int | None],
        monotonic: Callable[[], float] = time.monotonic,
        started_monotonic: float | None = None,
    ):
        self.ledger = ledger
        self.model_windows = model_windows
        self.monotonic = monotonic
        self.started = monotonic() if started_monotonic is None else started_monotonic
        self._lock = threading.Lock()
        self._contexts: dict[str, tuple[str | None, int | None]] = {}
        self._git: tuple[int, int, bool] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def observe(
        self,
        agent_id: str,
        model: str | None,
        presentation: InvocationPresentation | None,
    ) -> None:
        with self._lock:
            self._contexts[agent_id] = (
                model,
                None if presentation is None else presentation.context_input_tokens,
            )

    def watch_git(self, repository: Path, *, base_commit: str) -> None:
        self.stop_git()
        self._stop.clear()
        manager = GitWorkspaceManager(repository.parent, timeout_seconds=2)

        def sample() -> None:
            while not self._stop.is_set():
                try:
                    counts = manager.live_change_counts(
                        repository, base_commit=base_commit
                    )
                except (GitWorkspaceError, OSError):
                    counts = None
                with self._lock:
                    self._git = counts
                self._stop.wait(3)

        self._thread = threading.Thread(
            target=sample, name="sat-git-display", daemon=True
        )
        self._thread.start()

    def stop_git(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                raise RuntimeError("the read-only Git sampler did not stop")
            self._thread = None

    def __call__(self) -> list[str]:
        elapsed = max(0, int(self.monotonic() - self.started))
        hours, remainder = divmod(elapsed, 3600)
        minutes, seconds = divmod(remainder, 60)
        duration = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        usage = self.ledger.snapshot()
        unsettled = f" · {usage.active_calls} unsettled" if usage.active_calls else ""
        remaining = usage.remaining_estimated_cost_usd(self.ledger.budget)
        lines = [
            f"Run · {duration} elapsed · ${usage.known_estimated_cost_usd:.4f} settled",
            f"Budget · ${remaining:.4f} headroom / "
            f"${self.ledger.budget.max_estimated_cost_usd} authorized{unsettled}",
        ]
        if usage.unpriced_calls:
            lines.append("Cost · incomplete provider usage; headroom is unconfirmed")
        with self._lock:
            contexts = tuple(self._contexts.items())
            git = self._git
        if not contexts:
            lines.append("Context · unavailable until a provider reports request usage")
        for agent_id, (model, tokens) in contexts:
            window = self.model_windows.get(model or "")
            lines.append(
                f"Context · {agent_id}: "
                f"{tokens if tokens is not None else 'unavailable'} / "
                f"{window if window is not None else 'unknown'} tokens "
                "(last reported request)"
            )
        lines.append(
            "Git · unavailable before workspace preparation"
            if git is None
            else f"Git · +{git[0]} / -{git[1]} lines since build base"
            + (" (partial; binary/large/unreadable files omitted)" if git[2] else "")
        )
        return lines
