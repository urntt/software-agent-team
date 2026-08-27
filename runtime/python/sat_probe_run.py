#!/usr/local/bin/python
"""Execute one bounded Reviewer probe and report its authoritative result.

The Reviewer can author a small probe through ``sat-probe-write`` while the
project mount remains read-only.  This companion runner executes only one
validated Python probe from that namespace, captures bounded output, and emits
a terminal machine-readable result marker that the controller can verify even
when an outer shell reports a misleading exit status.
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import signal
import stat
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import PurePosixPath

TMP_DIRECTORY = "/tmp"
PROJECT_DIRECTORY = "/agent"
PYTHON = "/usr/local/bin/python"
MAX_PROBE_BYTES = 65536
MAX_OUTPUT_BYTES = 1024 * 1024
TIMEOUT_SECONDS = 30
RESULT_PREFIX = "SAT_PROBE_RESULT_V1 "

EXIT_REFUSED = 3
EXIT_IO_ERROR = 5
EXIT_TIMEOUT = 124


class ProbeRunRefused(ValueError):
    """The requested probe is outside the fixed execution boundary."""


class ProbeRunFailure(OSError):
    """The runner could not start or observe an otherwise valid probe."""


def _validated_target(raw_target: str) -> str:
    """Return a canonical direct-child Python probe path."""

    if not raw_target or "\x00" in raw_target or "\\" in raw_target:
        raise ProbeRunRefused("target must be a canonical POSIX path")
    target = PurePosixPath(raw_target)
    if str(target) != raw_target or target.parent != PurePosixPath(TMP_DIRECTORY):
        raise ProbeRunRefused("target must be a direct child of /tmp")
    name = target.name
    prefix = "sat-review-probe-"
    if not name.startswith(prefix) or not name.endswith(".py"):
        raise ProbeRunRefused("target must be a sat-review-probe-*.py file")
    stem = name[len(prefix) : -3]
    if (
        not 1 <= len(stem) <= 64
        or not stem[0].isalnum()
        or not stem[-1].isalnum()
        or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789-"
            for character in stem
        )
    ):
        raise ProbeRunRefused(
            "target name must use lowercase letters, digits, or hyphens"
        )
    return raw_target


def _open_probe(raw_target: str) -> int:
    """Open and validate one immutable view of the requested probe."""

    target = _validated_target(raw_target)
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        descriptor = os.open(target, flags)
    except OSError as error:
        raise ProbeRunFailure(
            f"cannot open probe safely: {error.strerror or error}"
        ) from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ProbeRunRefused("target must be a regular file")
        if metadata.st_uid != os.geteuid():
            raise ProbeRunRefused("target must be owned by the current sandbox user")
        if metadata.st_nlink != 1:
            raise ProbeRunRefused("target must have exactly one filesystem link")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ProbeRunRefused("target mode must be 0600")
        if metadata.st_size > MAX_PROBE_BYTES:
            raise ProbeRunRefused(
                f"target exceeds the {MAX_PROBE_BYTES}-byte probe limit"
            )
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _limit_child_output() -> None:
    """Limit each redirected child stream before executing untrusted code."""

    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT_BYTES, MAX_OUTPUT_BYTES))


def _read_bounded(stream) -> bytes:
    stream.seek(0)
    return stream.read(MAX_OUTPUT_BYTES)


def _emit_stream(label: str, payload: bytes, destination) -> None:
    """Emit a bounded stream with explicit framing and UTF-8 replacement."""

    text = payload.decode("utf-8", errors="replace")
    print(f"SAT_PROBE_{label}_BEGIN", file=destination)
    if text:
        print(text, end="" if text.endswith("\n") else "\n", file=destination)
    print(f"SAT_PROBE_{label}_END", file=destination)


def _emit_result(*, exit_code: int, timed_out: bool) -> None:
    payload = json.dumps(
        {"exit_code": exit_code, "timed_out": timed_out},
        sort_keys=True,
        separators=(",", ":"),
    )
    print(f"{RESULT_PREFIX}{payload}")


def run_probe(raw_target: str) -> tuple[int, bool, bytes, bytes]:
    """Run one fixed probe path and return normalized bounded evidence."""

    descriptor = _open_probe(raw_target)
    try:
        with (
            tempfile.TemporaryFile(dir=TMP_DIRECTORY) as stdout,
            tempfile.TemporaryFile(dir=TMP_DIRECTORY) as stderr,
        ):
            try:
                process = subprocess.Popen(
                    [PYTHON, f"/proc/self/fd/{descriptor}"],
                    cwd=PROJECT_DIRECTORY,
                    env={
                        **os.environ,
                        "PYTHONPATH": f"{PROJECT_DIRECTORY}/src:{PROJECT_DIRECTORY}",
                    },
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    pass_fds=(descriptor,),
                    start_new_session=True,
                    preexec_fn=_limit_child_output,
                )
            except OSError as error:
                raise ProbeRunFailure(
                    f"cannot start probe: {error.strerror or error}"
                ) from error
            timed_out = False
            try:
                return_code = process.wait(timeout=TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                return_code = EXIT_TIMEOUT
            if return_code < 0:
                return_code = 128 + abs(return_code)
            return (
                return_code,
                timed_out,
                _read_bounded(stdout),
                _read_bounded(stderr),
            )
    finally:
        os.close(descriptor)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sat-probe-run",
        description=(
            "Execute one bounded /tmp/sat-review-probe-*.py file and emit an "
            "authoritative terminal result marker."
        ),
    )
    parser.add_argument("target", nargs="?", help="canonical Python probe path")
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="verify that the immutable runner can start",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the bounded command-line interface."""

    arguments = _parser().parse_args(argv)
    if arguments.self_test:
        if arguments.target is not None:
            _parser().error("--self-test does not accept a target")
        _emit_result(exit_code=0, timed_out=False)
        return 0
    if arguments.target is None:
        _parser().error("a probe target is required")
    try:
        exit_code, timed_out, stdout, stderr = run_probe(arguments.target)
    except ProbeRunRefused as error:
        print(f"sat-probe-run: refused: {error}", file=sys.stderr)
        _emit_result(exit_code=EXIT_REFUSED, timed_out=False)
        return EXIT_REFUSED
    except ProbeRunFailure as error:
        print(f"sat-probe-run: failed: {error}", file=sys.stderr)
        _emit_result(exit_code=EXIT_IO_ERROR, timed_out=False)
        return EXIT_IO_ERROR
    _emit_stream("STDOUT", stdout, sys.stdout)
    _emit_stream("STDERR", stderr, sys.stdout)
    _emit_result(exit_code=exit_code, timed_out=timed_out)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
