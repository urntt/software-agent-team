"""Write bounded, atomic pytest progress for the repository gate supervisor."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

_STATE_ENVIRONMENT_VARIABLE = "SAT_FULL_GATE_PYTEST_STATE"


def _state_path() -> Path | None:
    raw = os.environ.get(_STATE_ENVIRONMENT_VARIABLE)
    return Path(raw) if raw else None


def _write_state(**updates: Any) -> None:
    path = _state_path()
    if path is None:
        return
    state: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            loaded = None
        if isinstance(loaded, dict):
            state.update(loaded)
    state.update(updates)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def pytest_sessionstart(session: Any) -> None:
    """Record that collection and execution have started."""

    del session
    _write_state(status="running", current_node_id=None, last_completed_node_id=None)


def pytest_runtest_logstart(nodeid: str, location: Any) -> None:
    """Persist the exact test whose protocol is currently executing."""

    del location
    _write_state(status="running", current_node_id=nodeid)


def pytest_runtest_logreport(report: Any) -> None:
    """Advance the completed checkpoint after the test teardown report."""

    if report.when == "teardown":
        _write_state(
            status="running",
            current_node_id=None,
            last_completed_node_id=report.nodeid,
        )


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    """Persist pytest's terminal status independently from terminal output."""

    del session
    _write_state(status="completed", current_node_id=None, exit_status=exitstatus)
