"""Tests for bounded ownership of short-lived subprocesses."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from software_agent_team.bounded_process import (
    BoundedProcessResidualError,
    BoundedProcessTimeoutError,
    run_bounded_process,
)
from software_agent_team.process_lifecycle import read_linux_process_identity

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="requires procfs")


def _write_executable(path: Path, source: str) -> None:
    path.write_text(f"#!{sys.executable}\n{source}", encoding="utf-8")
    path.chmod(0o755)


def test_bounded_process_returns_content_only_after_clean_success(
    tmp_path: Path,
) -> None:
    command = tmp_path / "success"
    _write_executable(
        command,
        "import json\nprint(json.dumps({'status': 'ok'}))\n",
    )

    result = run_bounded_process(
        [str(command)],
        environment=os.environ,
        timeout_seconds=5,
        termination_grace_seconds=1,
    )

    assert result.returncode == 0
    assert json.loads(result.stdout) == {"status": "ok"}
    assert result.stderr == ""


def test_bounded_process_timeout_kills_a_detached_descendant(tmp_path: Path) -> None:
    child_pid_path = tmp_path / "detached.pid"
    command = tmp_path / "timeout"
    _write_executable(
        command,
        "\n".join(
            (
                "import os, signal, subprocess, sys, time",
                "child_code = ('import signal,time; '",
                "              'signal.signal(signal.SIGTERM, signal.SIG_IGN); '",
                "              'time.sleep(30)')",
                "child = subprocess.Popen(",
                "    [sys.executable, '-c', child_code],",
                "    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,",
                "    stderr=subprocess.DEVNULL, start_new_session=True,",
                "    env=os.environ,",
                ")",
                "open(os.environ['SAT_TEST_PID_PATH'], 'w').write(str(child.pid))",
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)",
                "time.sleep(30)",
            )
        )
        + "\n",
    )
    environment = dict(os.environ)
    environment["SAT_TEST_PID_PATH"] = str(child_pid_path)

    with pytest.raises(BoundedProcessTimeoutError, match=r"timed out after 0\.5"):
        run_bounded_process(
            [str(command)],
            environment=environment,
            timeout_seconds=0.5,
            termination_grace_seconds=0.3,
        )

    detached_pid = int(child_pid_path.read_text(encoding="utf-8"))
    assert read_linux_process_identity(detached_pid) is None


def test_bounded_process_rejects_a_descendant_left_after_success(
    tmp_path: Path,
) -> None:
    child_pid_path = tmp_path / "detached.pid"
    command = tmp_path / "residual"
    _write_executable(
        command,
        "\n".join(
            (
                "import os, subprocess, sys",
                "child = subprocess.Popen(",
                "    [sys.executable, '-c', 'import time; time.sleep(30)'],",
                "    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,",
                "    stderr=subprocess.DEVNULL, start_new_session=True,",
                "    env=os.environ,",
                ")",
                "open(os.environ['SAT_TEST_PID_PATH'], 'w').write(str(child.pid))",
            )
        )
        + "\n",
    )
    environment = dict(os.environ)
    environment["SAT_TEST_PID_PATH"] = str(child_pid_path)

    with pytest.raises(
        BoundedProcessResidualError,
        match="owned descendant was still running",
    ):
        run_bounded_process(
            [str(command)],
            environment=environment,
            timeout_seconds=5,
            termination_grace_seconds=0.5,
        )

    detached_pid = int(child_pid_path.read_text(encoding="utf-8"))
    assert read_linux_process_identity(detached_pid) is None
