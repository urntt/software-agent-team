#!/usr/local/bin/python
"""Seed the offline uv cache and supervise project tests for silent hangs."""

from __future__ import annotations

import os
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from typing import NoReturn

PUBLIC_CACHE = Path("/opt/software-agent-team/uv-public-cache")
DEFAULT_CACHE = Path("/tmp/uv-cache")
REAL_UV = Path("/usr/local/bin/uv-real")
CACHE_MARKER = ".sat-public-cache-v1"
MARKER_CONTENT = "software-agent-team-public-uv-cache-v1\n"
TEST_SILENCE_SECONDS = 90.0
TEST_SHUTDOWN_SECONDS = 5.0


def _fail(message: str) -> NoReturn:
    print(f"uv: SAT offline cache error: {message}", file=sys.stderr)
    raise SystemExit(2)


def _cache_target() -> Path:
    raw = os.environ.get("UV_CACHE_DIR", str(DEFAULT_CACHE))
    target = Path(os.path.normpath(raw))
    temporary_root = Path("/tmp")
    if not target.is_absolute() or target == temporary_root:
        _fail("UV_CACHE_DIR must name a directory below /tmp")
    try:
        target.relative_to(temporary_root)
    except ValueError:
        _fail("UV_CACHE_DIR must name a directory below /tmp")

    cursor = temporary_root
    for part in target.relative_to(temporary_root).parts[:-1]:
        cursor /= part
        if cursor.is_symlink():
            _fail("UV_CACHE_DIR cannot traverse a symlink")
    return target


def _ready(cache: Path) -> bool:
    marker = cache / CACHE_MARKER
    if cache.is_symlink() or marker.is_symlink() or not marker.is_file():
        return False
    try:
        return marker.read_text(encoding="utf-8") == MARKER_CONTENT
    except OSError:
        return False


def _make_writable(cache: Path) -> None:
    for path in (cache, *cache.rglob("*")):
        if path.is_symlink():
            continue
        path.chmod(path.stat().st_mode | stat.S_IWUSR)


def _seed(target: Path) -> None:
    if _ready(target):
        return
    if target.exists() or target.is_symlink():
        _fail(f"cache target exists without a valid marker: {target}")
    if not _ready(PUBLIC_CACHE):
        _fail("frozen public package cache is unavailable")

    target.parent.mkdir(parents=True, exist_ok=True)
    candidate = Path(tempfile.mkdtemp(prefix=".sat-uv-cache-", dir=str(target.parent)))
    candidate.rmdir()
    try:
        shutil.copytree(PUBLIC_CACHE, candidate, symlinks=True)
        _make_writable(candidate)
        try:
            candidate.rename(target)
        except OSError:
            if not _ready(target):
                raise
    finally:
        if candidate.exists():
            shutil.rmtree(candidate)


def _stop_process_group(process: subprocess.Popen[bytes]) -> None:
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    with suppress(subprocess.TimeoutExpired):
        process.wait(timeout=TEST_SHUTDOWN_SECONDS)
    # The uv parent may have exited while a test descendant still owns its
    # pipes. Signal the exact command group once more before leaving.
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    process.wait()


def _forward_output(destination: int, chunk: bytes) -> None:
    while chunk:
        written = os.write(destination, chunk)
        chunk = chunk[written:]


def _run_project_tests(
    arguments: list[str],
    environment: dict[str, str],
    *,
    silence_seconds: float = TEST_SILENCE_SECONDS,
) -> int:
    """Let uv run normally, but return a bounded error after silent tests."""

    process = subprocess.Popen(
        [str(REAL_UV), *arguments],
        env=environment,
        stdin=None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    assert process.stdout is not None and process.stderr is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, sys.stdout.fileno())
    selector.register(process.stderr, selectors.EVENT_READ, sys.stderr.fileno())
    last_output = time.monotonic()
    try:
        while selector.get_map() or process.poll() is None:
            remaining = silence_seconds - (time.monotonic() - last_output)
            if remaining <= 0:
                print(
                    "uv: project tests produced no output for "
                    f"{silence_seconds:g}s; stopping the test command. "
                    "Run pytest -vv without piping to tail, find the blocked "
                    "test, and fix it before retrying.",
                    file=sys.stderr,
                    flush=True,
                )
                _stop_process_group(process)
                return 124
            if not selector.get_map():
                time.sleep(min(remaining, 0.25))
                continue
            for key, _ in selector.select(timeout=min(remaining, 0.25)):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                last_output = time.monotonic()
                _forward_output(key.data, chunk)
        result = process.wait()
        return result if result >= 0 else 128 - result
    except (BrokenPipeError, KeyboardInterrupt):
        _stop_process_group(process)
        return 130
    finally:
        selector.close()
        if process.poll() is None:
            _stop_process_group(process)


def main() -> None:
    target = _cache_target()
    _seed(target)
    environment = os.environ.copy()
    environment["UV_CACHE_DIR"] = str(target)
    if sys.argv[1:3] == ["run", "pytest"]:
        raise SystemExit(_run_project_tests(sys.argv[1:], environment))
    os.execve(REAL_UV, [str(REAL_UV), *sys.argv[1:]], environment)


if __name__ == "__main__":
    main()
