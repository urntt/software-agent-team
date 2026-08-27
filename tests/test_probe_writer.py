"""Security and behavior tests for the immutable Reviewer probe helper."""

from __future__ import annotations

import errno
import importlib.util
import os
import stat
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

REPOSITORY_ROOT = Path(__file__).parents[1]
HELPER = REPOSITORY_ROOT / "runtime" / "python" / "sat_probe_write.py"


def load_helper() -> ModuleType:
    """Load the standalone image helper for failure-path unit tests."""

    spec = importlib.util.spec_from_file_location("sat_probe_write", HELPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def unique_target(extension: str = "py") -> Path:
    """Return one collision-resistant valid direct child of /tmp."""

    return Path("/tmp") / f"sat-review-probe-test-{uuid.uuid4().hex}.{extension}"


@pytest.fixture
def probe_target() -> Iterator[Path]:
    target = unique_target()
    yield target
    target.unlink(missing_ok=True)


def run_helper(target: str | Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Invoke the helper through the same argv boundary used by the image."""

    return subprocess.run(
        [sys.executable, str(HELPER), str(target), *arguments],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_helper_creates_exact_bounded_content_without_overwrite(
    probe_target: Path,
) -> None:
    result = run_helper(
        probe_target,
        "--line",
        "from pathlib import Path",
        "--line",
        "",
        "--line",
        "print(Path('/tmp').is_dir())",
    )

    assert result.returncode == 0
    assert result.stderr == ""
    assert result.stdout == f"created {probe_target} bytes=55\n"
    assert probe_target.read_text(encoding="utf-8") == (
        "from pathlib import Path\n\nprint(Path('/tmp').is_dir())\n"
    )
    assert stat.S_IMODE(probe_target.stat().st_mode) == 0o600


def test_helper_refuses_existing_target_and_preserves_content(
    probe_target: Path,
) -> None:
    probe_target.write_text("preserve me\n", encoding="utf-8")

    result = run_helper(probe_target, "--line", "replacement")

    assert result.returncode == 4
    assert "target already exists" in result.stderr
    assert result.stdout == ""
    assert probe_target.read_text(encoding="utf-8") == "preserve me\n"


def test_helper_refuses_symlink_and_preserves_referent(tmp_path: Path) -> None:
    referent = tmp_path / "referent.txt"
    referent.write_text("trusted\n", encoding="utf-8")
    target = unique_target("txt")
    target.symlink_to(referent)
    try:
        result = run_helper(target, "--line", "replacement")

        assert result.returncode == 4
        assert target.is_symlink()
        assert referent.read_text(encoding="utf-8") == "trusted\n"
    finally:
        target.unlink(missing_ok=True)


@pytest.mark.parametrize(
    "target",
    (
        "sat-review-probe-relative.py",
        "/tmp/../tmp/sat-review-probe-traversal.py",
        "/tmp//sat-review-probe-duplicate.py",
        "/tmp/nested/sat-review-probe-nested.py",
        "/var/tmp/sat-review-probe-wrong-root.py",
        "/tmp/review-probe-wrong-prefix.py",
        "/tmp/sat-review-probe-UPPER.py",
        "/tmp/sat-review-probe-wrong.sh",
        "/tmp/sat-review-probe-name_with_underscore.py",
        f"/tmp/sat-review-probe-{'a' * 65}.py",
    ),
)
def test_helper_refuses_noncanonical_or_out_of_scope_targets(target: str) -> None:
    result = run_helper(target, "--line", "content")

    assert result.returncode == 3
    assert result.stdout == ""
    assert "sat-probe-write: refused:" in result.stderr


@pytest.mark.parametrize(
    "arguments",
    (
        tuple(item for _ in range(257) for item in ("--line", "x")),
        ("--line", "x" * 4097),
        tuple(item for _ in range(17) for item in ("--line", "x" * 4096)),
        ("--line", "first\nsecond"),
    ),
)
def test_helper_refuses_line_and_total_size_limit_violations(
    probe_target: Path,
    arguments: tuple[str, ...],
) -> None:
    result = run_helper(probe_target, *arguments)

    assert result.returncode == 3
    assert not probe_target.exists()


def test_helper_refuses_nul_content_before_creating_target(probe_target: Path) -> None:
    helper = load_helper()

    with pytest.raises(helper.ProbeWriteRefused, match="NUL"):
        helper.write_probe(str(probe_target), ("unsafe\x00content",))

    assert not probe_target.exists()


def test_helper_removes_same_inode_after_partial_write_failure(
    monkeypatch: pytest.MonkeyPatch,
    probe_target: Path,
) -> None:
    helper = load_helper()
    original_write = os.write
    calls = 0

    def fail_after_partial_write(file_descriptor: int, content: bytes) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            return original_write(file_descriptor, content[:2])
        raise OSError(errno.ENOSPC, "simulated full tmpfs")

    monkeypatch.setattr(helper.os, "write", fail_after_partial_write)

    with pytest.raises(helper.ProbeWriteFailure, match="simulated full tmpfs"):
        helper.write_probe(str(probe_target), ("partial content",))

    assert calls == 2
    assert not probe_target.exists()
