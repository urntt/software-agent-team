#!/usr/local/bin/python
"""Create one bounded Reviewer probe file directly under ``/tmp``.

The sandbox project mount remains read-only.  This helper provides a small,
deterministic authoring surface for probe scripts and fixtures without exposing
a general-purpose file mutation tool to the Reviewer.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Sequence
from pathlib import PurePosixPath

TMP_DIRECTORY = "/tmp"
MAX_LINES = 256
MAX_LINE_BYTES = 4096
MAX_TOTAL_BYTES = 65536
TARGET_PATTERN = re.compile(
    r"^sat-review-probe-[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?\.(?:py|json|txt)$"
)

EXIT_REFUSED = 3
EXIT_EXISTS = 4
EXIT_IO_ERROR = 5


class ProbeWriteRefused(ValueError):
    """The requested target or content is outside the helper boundary."""


class ProbeTargetExists(FileExistsError):
    """The requested target already exists and cannot be overwritten."""


class ProbeWriteFailure(OSError):
    """The helper could not complete an otherwise valid atomic write."""


def _validated_target_name(raw_target: str) -> str:
    """Return a safe direct-child basename or reject the target."""

    if not raw_target or "\x00" in raw_target or "\\" in raw_target:
        raise ProbeWriteRefused("target must be a canonical POSIX path")
    target = PurePosixPath(raw_target)
    if str(target) != raw_target or target.parent != PurePosixPath(TMP_DIRECTORY):
        raise ProbeWriteRefused("target must be a direct child of /tmp")
    if TARGET_PATTERN.fullmatch(target.name) is None:
        raise ProbeWriteRefused(
            "target name must match "
            "sat-review-probe-<lowercase-name>.py, .json, or .txt"
        )
    return target.name


def _encoded_content(lines: Sequence[str]) -> bytes:
    """Encode bounded one-argument-per-line content."""

    if len(lines) > MAX_LINES:
        raise ProbeWriteRefused(f"content exceeds the {MAX_LINES}-line limit")
    for line in lines:
        if "\x00" in line:
            raise ProbeWriteRefused("content cannot contain NUL bytes")
        if "\n" in line or "\r" in line:
            raise ProbeWriteRefused("each --line value must contain exactly one line")
        if len(line.encode("utf-8")) > MAX_LINE_BYTES:
            raise ProbeWriteRefused(f"one line exceeds the {MAX_LINE_BYTES}-byte limit")
    content = ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")
    if len(content) > MAX_TOTAL_BYTES:
        raise ProbeWriteRefused(
            f"content exceeds the {MAX_TOTAL_BYTES}-byte total limit"
        )
    return content


def _remove_partial_file(directory_fd: int, name: str, file_fd: int) -> None:
    """Remove only the same inode this invocation created."""

    try:
        opened = os.fstat(file_fd)
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (opened.st_dev, opened.st_ino) == (current.st_dev, current.st_ino):
            os.unlink(name, dir_fd=directory_fd)
    except FileNotFoundError:
        return


def write_probe(raw_target: str, lines: Sequence[str]) -> int:
    """Atomically create one non-overwriting probe and return its byte count."""

    name = _validated_target_name(raw_target)
    content = _encoded_content(lines)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    file_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        directory_fd = os.open(TMP_DIRECTORY, directory_flags)
    except OSError as error:
        raise ProbeWriteFailure(
            f"cannot open the fixed /tmp directory: {error.strerror or error}"
        ) from error

    try:
        try:
            file_fd = os.open(name, file_flags, 0o600, dir_fd=directory_fd)
        except FileExistsError as error:
            raise ProbeTargetExists(
                "target already exists; choose a new unique probe name"
            ) from error
        except OSError as error:
            raise ProbeWriteFailure(
                f"cannot create the probe: {error.strerror or error}"
            ) from error

        try:
            os.fchmod(file_fd, 0o600)
            offset = 0
            while offset < len(content):
                written = os.write(file_fd, content[offset:])
                if written <= 0:
                    raise OSError("write returned no progress")
                offset += written
            os.fsync(file_fd)
        except OSError as error:
            _remove_partial_file(directory_fd, name, file_fd)
            raise ProbeWriteFailure(
                f"cannot complete the probe: {error.strerror or error}"
            ) from error
        finally:
            os.close(file_fd)
    finally:
        os.close(directory_fd)
    return len(content)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sat-probe-write",
        description=(
            "Atomically create one bounded sat-review-probe file directly under "
            "/tmp. Existing files are never overwritten."
        ),
    )
    parser.add_argument("target", help="canonical /tmp/sat-review-probe-* target")
    parser.add_argument(
        "--line",
        action="append",
        default=[],
        metavar="TEXT",
        help="append one UTF-8 line; repeat in file order",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the bounded command-line interface."""

    arguments = _parser().parse_args(argv)
    try:
        byte_count = write_probe(arguments.target, arguments.line)
    except ProbeWriteRefused as error:
        print(f"sat-probe-write: refused: {error}", file=sys.stderr)
        return EXIT_REFUSED
    except ProbeTargetExists as error:
        print(f"sat-probe-write: exists: {error}", file=sys.stderr)
        return EXIT_EXISTS
    except ProbeWriteFailure as error:
        print(f"sat-probe-write: failed: {error}", file=sys.stderr)
        return EXIT_IO_ERROR
    print(f"created {arguments.target} bytes={byte_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
