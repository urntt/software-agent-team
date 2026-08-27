#!/usr/bin/env python3
"""Execute a generated project's exact command contract from a clean copy."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from run import ProjectCommands, validate

MAX_TRACKED_FILES = 4096
MAX_TRACKED_BYTES = 64 * 1024 * 1024
MAX_CAPTURE_BYTES = 64 * 1024
SETUP_TIMEOUT_SECONDS = 45
TEST_TIMEOUT_SECONDS = 75
START_GRACE_SECONDS = 5
SHUTDOWN_SECONDS = 5


@dataclass(frozen=True)
class CommandResult:
    """Bounded outcome from one exact project command."""

    exit_code: int | None
    timed_out: bool
    stdout_tail: str
    stderr_tail: str


def fail(message: str) -> None:
    print(f"exact project commands: {message}", file=sys.stderr)
    raise SystemExit(1)


def _safe_relative_path(value: str) -> Path:
    if not value or "\\" in value or "\x00" in value:
        fail("Git returned an unsafe tracked path")
    pure = PurePosixPath(value)
    if pure.is_absolute() or ".." in pure.parts or pure == PurePosixPath("."):
        fail("Git returned an unsafe tracked path")
    return Path(*pure.parts)


def _tracked_paths(repository: Path) -> tuple[Path, ...]:
    result = subprocess.run(
        ["git", "-C", str(repository), "ls-tree", "-r", "--name-only", "-z", "HEAD"],
        check=False,
        capture_output=True,
        timeout=15,
    )
    if result.returncode != 0:
        fail("cannot enumerate committed project files")
    try:
        names = result.stdout.decode("utf-8", errors="strict").split("\x00")
    except UnicodeDecodeError:
        fail("Git returned a non-UTF-8 tracked path")
    paths = tuple(_safe_relative_path(name) for name in names if name)
    if not paths or len(paths) > MAX_TRACKED_FILES or len(paths) != len(set(paths)):
        fail("committed project file inventory is empty, duplicated, or too large")
    return paths


def _require_clean_tracked_files(repository: Path) -> None:
    result = subprocess.run(
        ["git", "-C", str(repository), "diff", "--quiet", "HEAD", "--"],
        check=False,
        capture_output=True,
        timeout=15,
    )
    if result.returncode == 1:
        fail("tracked project files differ from the immutable commit")
    if result.returncode != 0:
        fail("cannot verify committed project files")


def _copy_committed_files(repository: Path, destination: Path) -> None:
    _require_clean_tracked_files(repository)
    total_bytes = 0
    for relative in _tracked_paths(repository):
        source = repository / relative
        try:
            metadata = source.lstat()
        except OSError:
            fail(f"tracked file is unavailable: {relative.as_posix()}")
        if source.is_symlink() or not source.is_file():
            fail(f"tracked entries must be regular files: {relative.as_posix()}")
        total_bytes += metadata.st_size
        if total_bytes > MAX_TRACKED_BYTES:
            fail("committed project files exceed the clean-copy size limit")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target, follow_symlinks=False)


def _tail(stream: object) -> str:
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    stream.seek(max(0, size - MAX_CAPTURE_BYTES))
    return stream.read().decode("utf-8", errors="replace")


def _terminate(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=SHUTDOWN_SECONDS)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=SHUTDOWN_SECONDS)


def _run(
    argv: tuple[str, ...],
    *,
    cwd: Path,
    timeout_seconds: int,
) -> CommandResult:
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                env=os.environ.copy(),
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
            )
        except OSError as error:
            return CommandResult(
                exit_code=None,
                timed_out=False,
                stdout_tail="",
                stderr_tail=f"launch failed: {error}",
            )
        timed_out = False
        try:
            process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate(process)
        result = CommandResult(
            exit_code=None if timed_out else process.returncode,
            timed_out=timed_out,
            stdout_tail=_tail(stdout),
            stderr_tail=_tail(stderr),
        )
    return result


def _require_success(label: str, result: CommandResult) -> None:
    if result.timed_out or result.exit_code != 0:
        detail = (result.stderr_tail or result.stdout_tail).strip()[-2000:]
        fail(
            f"{label} failed (exit={result.exit_code}, "
            f"timed_out={str(result.timed_out).lower()}): {detail}"
        )


def execute(repository: Path) -> None:
    commands: ProjectCommands = validate(repository)
    with tempfile.TemporaryDirectory(prefix="sat-project-commands-") as temporary:
        clean = Path(temporary) / "project"
        clean.mkdir()
        _copy_committed_files(repository.resolve(strict=True), clean)
        setup = _run(commands.setup, cwd=clean, timeout_seconds=SETUP_TIMEOUT_SECONDS)
        _require_success("setup command", setup)
        test = _run(commands.test, cwd=clean, timeout_seconds=TEST_TIMEOUT_SECONDS)
        _require_success("test command", test)
        start = _run(
            commands.start,
            cwd=clean,
            timeout_seconds=START_GRACE_SECONDS,
        )
        if not start.timed_out:
            _require_success("start command", start)
        mode = "running_after_grace" if start.timed_out else "exited_zero"
        print(
            json.dumps(
                {
                    "setup": "passed",
                    "test": "passed",
                    "start": mode,
                    "source": "committed_tracked_files",
                    "workspace": "fresh_sandbox_scratch_copy",
                },
                sort_keys=True,
            )
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True, type=Path)
    args = parser.parse_args()
    execute(args.repository)


if __name__ == "__main__":
    main()
