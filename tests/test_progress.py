"""Tests for controller-backed terminal progress rendering."""

import time
from io import StringIO

from software_agent_team.artifacts import AgentRole
from software_agent_team.progress import (
    ProgressEvent,
    ProgressEventKind,
    TerminalProgressRenderer,
)


def test_progress_renderer_shows_elapsed_waiting_and_verified_completion() -> None:
    output = StringIO()
    renderer = TerminalProgressRenderer(output=output, heartbeat_seconds=0.01)
    started = ProgressEvent(
        kind=ProgressEventKind.AGENT_STARTED,
        message="Planner is working",
        role=AgentRole.PLANNER,
        iteration=1,
        attempt=1,
    )
    renderer(started)
    time.sleep(0.03)
    renderer(
        ProgressEvent(
            kind=ProgressEventKind.AGENT_COMPLETED,
            message="Planner response recorded (0.1s)",
            role=AgentRole.PLANNER,
            iteration=1,
            attempt=1,
        )
    )
    renderer.close()

    rendered = output.getvalue()
    assert "Planner is working" in rendered
    assert "elapsed" in rendered
    assert "Planner response recorded" in rendered
    assert "reasoning" not in rendered.casefold()


def test_progress_renderer_closes_multiple_independent_verifiers() -> None:
    output = StringIO()
    renderer = TerminalProgressRenderer(output=output, heartbeat_seconds=1)
    for role in (AgentRole.TESTER, AgentRole.REVIEWER):
        renderer(
            ProgressEvent(
                kind=ProgressEventKind.AGENT_STARTED,
                message=f"{role.value} is working",
                role=role,
                iteration=1,
                attempt=1,
            )
        )

    renderer.close()

    assert "tester is working" in output.getvalue()
    assert "reviewer is working" in output.getvalue()
