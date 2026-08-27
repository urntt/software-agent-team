"""Security, timeout, and evidence tests for the immutable probe runner."""

from __future__ import annotations

import importlib.util
import os
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

REPOSITORY_ROOT = Path(__file__).parents[1]
HELPER = REPOSITORY_ROOT / "runtime" / "python" / "sat_probe_run.py"


def load_helper() -> ModuleType:
    """Load the standalone image helper for direct boundary tests."""

    spec = importlib.util.spec_from_file_location("sat_probe_run", HELPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def unique_target() -> Path:
    """Return one collision-resistant valid probe target."""

    return Path("/tmp") / f"sat-review-probe-runner-{uuid.uuid4().hex}.py"


@pytest.fixture
def probe_target() -> Iterator[Path]:
    target = unique_target()
    yield target
    target.unlink(missing_ok=True)


def write_probe(target: Path, content: str) -> None:
    """Create the same owner-only file produced by sat-probe-write."""

    target.write_text(content, encoding="utf-8")
    target.chmod(0o600)


def test_runner_emits_bounded_streams_and_terminal_success_marker(
    tmp_path: Path,
    probe_target: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    helper = load_helper()
    helper.PROJECT_DIRECTORY = str(tmp_path)
    helper.PYTHON = sys.executable
    write_probe(
        probe_target,
        "import sys\nprint('PROBE_OK')\nprint('probe detail', file=sys.stderr)\n",
    )

    result = helper.main([str(probe_target)])
    captured = capsys.readouterr()

    assert result == 0
    assert "SAT_PROBE_STDOUT_BEGIN\nPROBE_OK\nSAT_PROBE_STDOUT_END" in captured.out
    assert "SAT_PROBE_STDERR_BEGIN\nprobe detail\nSAT_PROBE_STDERR_END" in captured.out
    assert captured.out.endswith(
        'SAT_PROBE_RESULT_V1 {"exit_code":0,"timed_out":false}\n'
    )
    assert captured.err == ""


def test_runner_preserves_assertion_failure_in_terminal_marker(
    tmp_path: Path,
    probe_target: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    helper = load_helper()
    helper.PROJECT_DIRECTORY = str(tmp_path)
    helper.PYTHON = sys.executable
    write_probe(probe_target, "assert False, 'observable product defect'\n")

    result = helper.main([str(probe_target)])
    captured = capsys.readouterr()

    assert result == 1
    assert "AssertionError: observable product defect" in captured.out
    assert captured.out.endswith(
        'SAT_PROBE_RESULT_V1 {"exit_code":1,"timed_out":false}\n'
    )


def test_runner_kills_a_timed_out_probe_and_marks_timeout(
    tmp_path: Path,
    probe_target: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    helper = load_helper()
    helper.PROJECT_DIRECTORY = str(tmp_path)
    helper.PYTHON = sys.executable
    helper.TIMEOUT_SECONDS = 0.01
    write_probe(probe_target, "import time\ntime.sleep(5)\n")

    result = helper.main([str(probe_target)])
    captured = capsys.readouterr()

    assert result == helper.EXIT_TIMEOUT
    assert captured.out.endswith(
        'SAT_PROBE_RESULT_V1 {"exit_code":124,"timed_out":true}\n'
    )


@pytest.mark.parametrize(
    "target",
    (
        "sat-review-probe-relative.py",
        "/tmp/../tmp/sat-review-probe-traversal.py",
        "/tmp/sat-review-probe-wrong.txt",
        "/tmp/sat-review-probe-UPPER.py",
        "/var/tmp/sat-review-probe-wrong.py",
    ),
)
def test_runner_refuses_noncanonical_targets(target: str) -> None:
    helper = load_helper()

    with pytest.raises(helper.ProbeRunRefused):
        helper._validated_target(target)


def test_runner_refuses_symlinks_hardlinks_and_non_owner_only_mode(
    tmp_path: Path,
    probe_target: Path,
) -> None:
    helper = load_helper()
    referent = tmp_path / "referent.py"
    write_probe(referent, "pass\n")

    probe_target.symlink_to(referent)
    with pytest.raises(helper.ProbeRunFailure, match="open probe safely"):
        helper._open_probe(str(probe_target))
    probe_target.unlink()

    os.link(referent, probe_target)
    with pytest.raises(helper.ProbeRunRefused, match="exactly one"):
        helper._open_probe(str(probe_target))
    probe_target.unlink()

    write_probe(probe_target, "pass\n")
    probe_target.chmod(0o644)
    with pytest.raises(helper.ProbeRunRefused, match="0600"):
        helper._open_probe(str(probe_target))


def test_runner_self_test_has_no_project_or_probe_dependency(
    capsys: pytest.CaptureFixture[str],
) -> None:
    helper = load_helper()

    result = helper.main(["--self-test"])

    assert result == 0
    assert capsys.readouterr().out == (
        'SAT_PROBE_RESULT_V1 {"exit_code":0,"timed_out":false}\n'
    )
