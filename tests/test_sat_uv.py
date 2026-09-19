"""Exercise the sandbox uv wrapper across real child-process boundaries."""

from __future__ import annotations

import importlib.util
import os
import time
from pathlib import Path

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
        "if mode == 'silent':\n"
        "    print('tests/test_prompt.py::test_prompt', flush=True)\n"
        "    Path = __import__('pathlib').Path\n"
        "    Path(sys.argv[4]).write_text(str(os.getpid()))\n"
        "    time.sleep(30)\n"
        "elif mode == 'progress':\n"
        "    for index in range(5):\n"
        "        print(f'test {index} passed', flush=True)\n"
        "        time.sleep(0.09)\n"
        "elif mode == 'closed':\n"
        "    os.close(1)\n"
        "    os.close(2)\n"
        "    time.sleep(30)\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    module.REAL_UV = executable
    return module


def test_silent_project_test_returns_diagnostic_and_stops_child(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    wrapper = _wrapper(tmp_path)
    pid_file = tmp_path / "test.pid"

    started = time.monotonic()
    result = wrapper._run_project_tests(
        ["run", "pytest", "silent", str(pid_file)],
        os.environ.copy(),
        silence_seconds=0.25,
    )

    assert result == 124
    assert time.monotonic() - started < 3
    output = capfd.readouterr()
    assert "tests/test_prompt.py::test_prompt" in output.out
    assert "no output" in output.err
    assert "without piping to tail" in output.err
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)


def test_project_test_output_renews_silence_window(tmp_path: Path) -> None:
    wrapper = _wrapper(tmp_path)

    started = time.monotonic()
    result = wrapper._run_project_tests(
        ["run", "pytest", "progress"],
        os.environ.copy(),
        silence_seconds=0.20,
    )

    assert result == 0
    assert time.monotonic() - started >= 0.40


def test_closed_test_output_cannot_bypass_silence_bound(tmp_path: Path) -> None:
    wrapper = _wrapper(tmp_path)

    started = time.monotonic()
    result = wrapper._run_project_tests(
        ["run", "pytest", "closed"],
        os.environ.copy(),
        silence_seconds=0.25,
    )

    assert result == 124
    assert time.monotonic() - started < 3
