"""Exercise the sandbox uv wrapper across real child-process boundaries."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

WRAPPER = Path(__file__).parents[1] / "runtime" / "python" / "sat_uv.py"


def _wrapper(tmp_path: Path):
    spec = importlib.util.spec_from_file_location("sat_uv_test_module", WRAPPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    executable = tmp_path / "uv-real"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys, time\n"
        "mode = sys.argv[3]\n"
        "Path = __import__('pathlib').Path\n"
        "pid_file = Path(sys.argv[4])\n"
        "time.sleep(0.35)\n"
        "if mode == 'silent':\n"
        "    print('tests/test_prompt.py::test_prompt', flush=True)\n"
        "    pid_file.write_text(str(os.getpid()))\n"
        "    time.sleep(30)\n"
        "elif mode == 'progress':\n"
        "    print('test 0 passed', flush=True)\n"
        "    pid_file.write_text(str(os.getpid()))\n"
        "    for index in range(1, 5):\n"
        "        time.sleep(0.30)\n"
        "        print(f'test {index} passed', flush=True)\n"
        "elif mode == 'closed':\n"
        "    os.close(1)\n"
        "    os.close(2)\n"
        "    pid_file.write_text(str(os.getpid()))\n"
        "    time.sleep(30)\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    module.REAL_UV = executable
    return module


def _run_ready_project_test(
    wrapper: object, mode: str, pid_file: Path, *, silence_seconds: float
) -> tuple[int, float]:
    """Start the short silence window only after the real child is ready."""

    ready_at: float | None = None

    def start_ready(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        nonlocal ready_at
        process = subprocess.Popen(*args, **kwargs)
        deadline = time.monotonic() + 10
        while not pid_file.is_file():
            if process.poll() is not None or time.monotonic() >= deadline:
                wrapper._stop_process_group(process)
                raise AssertionError("project-test fixture did not become ready")
            time.sleep(0.01)
        ready_at = time.monotonic()
        return process

    wrapper.subprocess = SimpleNamespace(
        Popen=start_ready,
        PIPE=subprocess.PIPE,
        TimeoutExpired=subprocess.TimeoutExpired,
    )
    result = wrapper._run_project_tests(
        ["run", "pytest", mode, str(pid_file)],
        os.environ.copy(),
        silence_seconds=silence_seconds,
    )
    assert ready_at is not None
    return result, time.monotonic() - ready_at


def test_silent_project_test_returns_diagnostic_and_stops_child(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    wrapper = _wrapper(tmp_path)
    pid_file = tmp_path / "test.pid"

    result, elapsed = _run_ready_project_test(
        wrapper, "silent", pid_file, silence_seconds=1.0
    )

    assert result == 124
    assert elapsed < 3
    output = capfd.readouterr()
    assert "tests/test_prompt.py::test_prompt" in output.out
    assert "no output" in output.err
    assert "without piping to tail" in output.err
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)


def test_project_test_output_renews_silence_window(tmp_path: Path) -> None:
    wrapper = _wrapper(tmp_path)
    pid_file = tmp_path / "test.pid"

    result, elapsed = _run_ready_project_test(
        wrapper, "progress", pid_file, silence_seconds=1.0
    )

    assert result == 0
    assert elapsed >= 1.15


def test_closed_test_output_cannot_bypass_silence_bound(tmp_path: Path) -> None:
    wrapper = _wrapper(tmp_path)
    pid_file = tmp_path / "test.pid"

    result, elapsed = _run_ready_project_test(
        wrapper, "closed", pid_file, silence_seconds=1.0
    )

    assert result == 124
    assert elapsed < 3
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)
