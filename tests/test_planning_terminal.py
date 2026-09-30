"""Real PTY Planning cancellation across the production subprocess boundary."""

from __future__ import annotations

import fcntl
import os
import pty
import select
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

from software_agent_team.controls import ControlCommandStatus, ControlCommandStore
from software_agent_team.execution import AgentExecutionStatus
from software_agent_team.planning import PlanningSessionStatus, PlanningStore


def test_planning_cancel_stops_process_and_persists_command_result_and_session(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "openclaw"
    binary.write_text(f"#!{sys.executable}\nimport time\nwhile True: time.sleep(1)\n")
    binary.chmod(0o755)
    (tmp_path / "runtime").mkdir()
    program = """
import sys
from pathlib import Path
from planning_factories import request, policy
from software_agent_team.execution import OpenClawSubprocessExecutor
from software_agent_team.planning import (
    AdaptivePlanningCoordinator, PlanningStore, run_interactive_planning,
)

root = Path(sys.argv[1])
executor = OpenClawSubprocessExecutor(
    openclaw_binary=root / 'openclaw', process_grace_seconds=1,
    environment={'OPENCLAW_STATE_DIR': str(root / 'runtime')},
)
coordinator = AdaptivePlanningCoordinator(
    executor=executor, store=PlanningStore(root / 'planning'), policy=policy(),
)
result = run_interactive_planning(coordinator, request(), output=sys.stdout)
print('PLANNING_RETURNED=' + str(result), flush=True)
"""
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
    process = subprocess.Popen(
        [sys.executable, "-c", program, str(tmp_path)],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        close_fds=True,
        env={
            **os.environ,
            "TERM": "xterm-256color",
            "PYTHONPATH": os.pathsep.join(
                [str(Path(__file__).parent), os.environ.get("PYTHONPATH", "")]
            ),
        },
    )
    os.close(slave)
    captured = bytearray()

    def until(marker: bytes) -> None:
        deadline = time.monotonic() + 8
        while marker not in captured and time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.05)
            if ready:
                try:
                    captured.extend(os.read(master, 65536))
                except OSError:
                    break
        assert marker in captured, bytes(captured)[-3000:]

    try:
        until(b"initializing")
        os.write(master, b"/pause\r")
        until(b"unavailable during Planning")
        assert process.poll() is None
        os.write(master, b"\x03/cancel confirm\r")
        until(b"PLANNING_RETURNED=None")
        assert process.wait(timeout=3) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
        os.close(master)
    store = PlanningStore(tmp_path / "planning")
    session = store.load_session("sat-adaptive-001")
    assert session.status is PlanningSessionStatus.CANCELLED
    turn = store.load_turn(session.run_id, 1)
    assert turn.execution.status is AgentExecutionStatus.INTERRUPTED
    commands = ControlCommandStore(store.root / session.run_id, run_id=session.run_id)
    assert commands.list_latest()[0].status is ControlCommandStatus.APPLIED
