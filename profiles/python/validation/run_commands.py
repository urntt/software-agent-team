#!/usr/bin/env python3
"""Execute a generated project's exact command contract from a clean copy."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
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
MAX_SETUP_ARTIFACTS = 20_000
SETUP_TIMEOUT_SECONDS = 45
TEST_TIMEOUT_SECONDS = 75
START_GRACE_SECONDS = 5
EXAMPLE_TIMEOUT_SECONDS = 10
MAX_README_EXAMPLES = 3
SHUTDOWN_SECONDS = 5
PUBLIC_UV_CACHE = Path("/opt/software-agent-team/uv-public-cache")
FENCED_BLOCK = re.compile(r"^```([^\n]*)\n(.*?)^```[ \t]*$", re.MULTILINE | re.DOTALL)
EXPECTED_JSON_LABEL = re.compile(
    r"^[ \t]*(?:#{1,6}[ \t]+)?expected[ \t]+json[ \t]+output[ \t]*:[ \t]*$",
    re.MULTILINE | re.IGNORECASE,
)
CODE_POINT_KEYS = ("code_point_count", "code_points")
FIXTURE_HINT = (
    "use printf '%s' 'hello world' > example.txt for plain text, or a "
    "printf format containing only \\n, \\r, \\t, and \\\\ escapes"
)


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


def _file_digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _copy_committed_files(
    repository: Path,
    destination: Path,
) -> dict[Path, tuple[str, int]]:
    _require_clean_tracked_files(repository)
    total_bytes = 0
    snapshot: dict[Path, tuple[str, int]] = {}
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
        snapshot[relative] = (_file_digest(target), metadata.st_mode & 0o111)
    return snapshot


def _generated_leaf_paths(
    repository: Path,
    snapshot: dict[Path, tuple[str, int]],
) -> tuple[Path, ...]:
    generated: list[Path] = []
    for candidate in repository.rglob("*"):
        relative = candidate.relative_to(repository)
        if relative in snapshot or (candidate.is_dir() and not candidate.is_symlink()):
            continue
        generated.append(relative)
        if len(generated) > MAX_SETUP_ARTIFACTS:
            fail("setup command generated too many repository artifacts")
    return tuple(generated)


def _ignored_paths(
    clean: Path,
    paths: tuple[Path, ...],
    snapshot: dict[Path, tuple[str, int]],
) -> set[Path]:
    if not paths:
        return set()
    with tempfile.TemporaryDirectory(prefix="sat-project-ignore-") as temporary:
        probe = Path(temporary)
        result = subprocess.run(
            ["git", "init", "--quiet", str(probe)],
            check=False,
            capture_output=True,
            timeout=15,
        )
        if result.returncode != 0:
            fail("cannot initialize the setup-artifact ignore probe")
        for relative in snapshot:
            if relative.name != ".gitignore":
                continue
            target = probe / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(clean / relative, target, follow_symlinks=False)
        rendered = "\0".join(path.as_posix() for path in paths) + "\0"
        result = subprocess.run(
            [
                "git",
                "-c",
                "core.excludesFile=/dev/null",
                "-C",
                str(probe),
                "check-ignore",
                "--no-index",
                "-z",
                "--stdin",
            ],
            input=rendered,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode not in {0, 1}:
            fail("cannot verify setup-generated artifacts against .gitignore")
        return {
            _safe_relative_path(value) for value in result.stdout.split("\0") if value
        }


def _require_delivery_preserved(
    clean: Path,
    snapshot: dict[Path, tuple[str, int]],
    *,
    command_label: str,
) -> None:
    for relative, (expected_digest, expected_executable_bits) in snapshot.items():
        candidate = clean / relative
        try:
            metadata = candidate.lstat()
        except OSError:
            fail(
                f"{command_label} command removed committed file: {relative.as_posix()}"
            )
        if candidate.is_symlink() or not candidate.is_file():
            fail(
                f"{command_label} command replaced committed file: "
                f"{relative.as_posix()}"
            )
        if (
            _file_digest(candidate) != expected_digest
            or metadata.st_mode & 0o111 != expected_executable_bits
        ):
            fail(
                f"{command_label} command modified committed file: "
                f"{relative.as_posix()}"
            )
    generated = _generated_leaf_paths(clean, snapshot)
    unignored = sorted(set(generated) - _ignored_paths(clean, generated, snapshot))
    if unignored:
        rendered = ", ".join(path.as_posix() for path in unignored[:8])
        suffix = " ..." if len(unignored) > 8 else ""
        fail(
            f"{command_label} command generated unignored repository artifacts: "
            f"{rendered}{suffix}; commit reproducibility metadata or ignore only "
            "local runtime artifacts"
        )


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
    keep_stdin_open: bool = False,
    environment_overrides: dict[str, str] | None = None,
    clear_environment_prefixes: tuple[str, ...] = (),
) -> CommandResult:
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        environment = os.environ.copy()
        for name in tuple(environment):
            if name.startswith(clear_environment_prefixes):
                environment.pop(name)
        if environment_overrides is not None:
            environment.update(environment_overrides)
        try:
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                env=environment,
                stdin=subprocess.PIPE if keep_stdin_open else subprocess.DEVNULL,
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
        if process.stdin is not None:
            process.stdin.close()
        result = CommandResult(
            exit_code=None if timed_out else process.returncode,
            timed_out=timed_out,
            stdout_tail=_tail(stdout),
            stderr_tail=_tail(stderr),
        )
    return result


def _require_success(
    label: str,
    result: CommandResult,
    *,
    completed_stages: tuple[str, ...] = (),
) -> None:
    if result.timed_out or result.exit_code != 0:
        detail = (result.stderr_tail or result.stdout_tail).strip()[-2000:]
        completed = (
            f"; completed stages: {', '.join(completed_stages)}"
            if completed_stages
            else ""
        )
        fail(
            f"{label} failed (exit={result.exit_code}, "
            f"timed_out={str(result.timed_out).lower()}): {detail}{completed}"
        )


def _readme_json_examples(readme: str) -> tuple[tuple[str, object], ...]:
    """Select explicitly claimed JSON output and its preceding shell example."""

    blocks = tuple(FENCED_BLOCK.finditer(readme))
    labels = tuple(
        label
        for label in EXPECTED_JSON_LABEL.finditer(readme)
        if not any(block.start() <= label.start() < block.end() for block in blocks)
    )
    matched_labels: set[int] = set()
    examples: list[tuple[str, object]] = []
    for index, block in enumerate(blocks):
        if block.group(1).strip().casefold() != "json":
            continue
        previous = blocks[index - 1] if index else None
        earlier = blocks[index - 2] if index > 1 else None
        spans = (
            (previous.end(), block.start()) if previous else (0, block.start()),
            (earlier.end() if earlier else 0, previous.start()) if previous else (0, 0),
        )
        eligible_labels = [
            label
            for label in labels
            if any(
                start <= label.start() and label.end() <= end for start, end in spans
            )
        ]
        if not eligible_labels:
            continue
        if previous is None or previous.group(1).strip().casefold() not in {
            "bash",
            "sh",
            "shell",
        }:
            fail("README expected JSON output needs a preceding shell example")
        if len(eligible_labels) != 1 or eligible_labels[0].start() in matched_labels:
            fail("README expected JSON output has ambiguous example labels")
        matched_labels.add(eligible_labels[0].start())
        try:
            expected = json.loads(block.group(2))
        except json.JSONDecodeError:
            fail("README expected JSON output is invalid JSON")
        examples.append((previous.group(2), expected))
        if len(examples) > MAX_README_EXAMPLES:
            fail("README has too many expected JSON examples")
    if len(matched_labels) != len(labels):
        fail("README expected JSON output needs adjacent shell and JSON blocks")
    return tuple(examples)


def _fixture_content(tokens: list[str]) -> str:
    def decode_escapes(value: str, label: str) -> str:
        unsupported = re.search(r"\\[^nrt\\]", value)
        if unsupported:
            fail(
                f"README shell example uses an unsupported {label} escape "
                f"{unsupported.group(0)!r}; {FIXTURE_HINT}"
            )
        return re.sub(
            r"\\([nrt\\])",
            lambda match: {"n": "\n", "r": "\r", "t": "\t", "\\": "\\"}[match.group(1)],
            value,
        )

    if tokens[:2] == ["echo", "-e"] and len(tokens) == 3:
        return decode_escapes(tokens[2], "echo") + "\n"
    if tokens[:2] == ["printf", "%s"] and len(tokens) == 3:
        return tokens[2]
    if tokens[:1] == ["printf"] and len(tokens) == 2:
        if "%" in tokens[1]:
            fail(
                "README shell example uses an unsupported printf format; "
                f"{FIXTURE_HINT}"
            )
        return decode_escapes(tokens[1], "printf")
    if tokens[:1] == ["echo"]:
        fail(f"README JSON example fixture uses unsupported echo flags; {FIXTURE_HINT}")
    fail(
        "README JSON example fixture must use one supported printf or echo -e "
        f"form; {FIXTURE_HINT}"
    )


def _verify_code_point_newlines(
    index: int,
    argv: tuple[str, ...],
    fixture: Path,
    expected: object,
    clean: Path,
    snapshot: dict[Path, tuple[str, int]],
    environment_overrides: dict[str, str] | None,
) -> None:
    """Check a documented file code-point claim against preserved CRLF bytes."""

    if not isinstance(expected, dict):
        return
    keys = tuple(key for key in CODE_POINT_KEYS if key in expected)
    if not keys:
        return
    if any(type(expected[key]) is not int for key in keys):
        fail(f"README JSON example {index} code-point claim must be an integer")
    positions = [
        position
        for position, argument in enumerate(argv)
        if argument in {fixture.name, f"./{fixture.name}"}
    ]
    if len(positions) != 1:
        fail(f"README JSON example {index} cannot bind its code-point fixture")
    variant = clean / f"sat-example-{index}-mixed-newlines.txt"
    if (
        variant.relative_to(clean) in snapshot
        or variant.exists()
        or variant.is_symlink()
    ):
        fail(f"README JSON example {index} mixed-newline fixture already exists")
    content = "A\r\nβ\n"
    variant.write_bytes(content.encode("utf-8"))
    variant_argv = list(argv)
    variant_argv[positions[0]] = variant.name
    try:
        result = _run(
            tuple(variant_argv),
            cwd=clean,
            timeout_seconds=EXAMPLE_TIMEOUT_SECONDS,
            environment_overrides=environment_overrides,
        )
        _require_success(f"README JSON example {index} mixed-newline check", result)
        try:
            actual = json.loads(result.stdout_tail)
        except json.JSONDecodeError:
            fail(f"README JSON example {index} mixed-newline check did not emit JSON")
        if not isinstance(actual, dict):
            fail(f"README JSON example {index} mixed-newline check needs a JSON object")
        for key in keys:
            observed = actual.get(key)
            if type(observed) is not int or observed != len(content):
                detail = (
                    str(observed) if type(observed) is int else type(observed).__name__
                )
                fail(
                    f"README JSON example {index} {key} miscounts mixed CRLF/LF "
                    f"Unicode code points: expected={len(content)}, "
                    f"actual={detail}"
                )
    finally:
        variant.unlink(missing_ok=True)


def _example_fixture(
    line: str, clean: Path, snapshot: dict[Path, tuple[str, int]]
) -> Path:
    try:
        lexer = shlex.shlex(line, posix=True, punctuation_chars=">")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        fail("README JSON example has invalid shell quoting")
    if len(tokens) < 4 or tokens[-2] != ">":
        fail("README JSON example fixture needs one supported file redirection")
    relative = _safe_relative_path(tokens[-1])
    if relative in snapshot or len(relative.parts) != 1:
        fail("README JSON example fixture must be a new root-level file")
    candidate = clean / relative
    if candidate.exists() or candidate.is_symlink():
        fail("README JSON example fixture already exists")
    content = _fixture_content(tokens[:-2])
    if len(content.encode("utf-8")) > 8192:
        fail("README JSON example fixture is too large")
    candidate.write_text(content, encoding="utf-8")
    return candidate


def _verify_readme_examples(
    clean: Path,
    snapshot: dict[Path, tuple[str, int]],
    commands: ProjectCommands,
    environment_overrides: dict[str, str] | None,
) -> None:
    readme = (clean / "README.md").read_text(encoding="utf-8")
    for index, (script, expected) in enumerate(_readme_json_examples(readme), start=1):
        lines = [line.strip() for line in script.splitlines() if line.strip()]
        if len(lines) != 2:
            fail(f"README JSON example {index} needs one fixture and one command")
        fixture = _example_fixture(lines[0], clean, snapshot)
        try:
            try:
                argv = tuple(shlex.split(lines[1]))
            except ValueError:
                fail(f"README JSON example {index} has invalid command quoting")
            if argv[: len(commands.start)] != commands.start:
                fail(f"README JSON example {index} must invoke the exact start command")
            result = _run(
                argv,
                cwd=clean,
                timeout_seconds=EXAMPLE_TIMEOUT_SECONDS,
                environment_overrides=environment_overrides,
            )
            _require_success(f"README JSON example {index}", result)
            try:
                actual = json.loads(result.stdout_tail)
            except json.JSONDecodeError:
                fail(f"README JSON example {index} did not emit JSON on stdout")
            if actual != expected:
                fail(
                    f"README JSON example {index} output differs from documented "
                    f"JSON: actual={json.dumps(actual, ensure_ascii=False)[:500]}"
                )
            _verify_code_point_newlines(
                index, argv, fixture, expected, clean, snapshot, environment_overrides
            )
        finally:
            fixture.unlink(missing_ok=True)
        _require_delivery_preserved(
            clean, snapshot, command_label=f"README JSON example {index}"
        )


def execute(repository: Path) -> None:
    commands: ProjectCommands = validate(repository)
    with tempfile.TemporaryDirectory(prefix="sat-project-commands-") as temporary:
        clean = Path(temporary) / "project"
        clean.mkdir()
        snapshot = _copy_committed_files(repository.resolve(strict=True), clean)
        lock_environment = {
            "UV_CONFIG_FILE": "/dev/null",
            "UV_DEFAULT_INDEX": "https://pypi.org/simple",
            "UV_OFFLINE": "1",
        }
        command_environment: dict[str, str] | None = None
        if PUBLIC_UV_CACHE.is_dir():
            lock_cache = Path(temporary) / "uv-public-cache"
            shutil.copytree(PUBLIC_UV_CACHE, lock_cache, symlinks=True)
            for path in (lock_cache, *lock_cache.rglob("*")):
                if not path.is_symlink():
                    path.chmod(path.stat().st_mode | 0o200)
            lock_environment["UV_CACHE_DIR"] = str(lock_cache)
            command_environment = {"UV_CACHE_DIR": str(lock_cache)}
        lock_check = _run(
            ("uv", "lock", "--check", "--offline"),
            cwd=clean,
            timeout_seconds=SETUP_TIMEOUT_SECONDS,
            environment_overrides=lock_environment,
            clear_environment_prefixes=("UV_",),
        )
        _require_success("portable lock consistency check", lock_check)
        setup = _run(
            commands.setup,
            cwd=clean,
            timeout_seconds=SETUP_TIMEOUT_SECONDS,
            environment_overrides=command_environment,
        )
        _require_success(
            "setup command", setup, completed_stages=("portable lock check",)
        )
        _require_delivery_preserved(clean, snapshot, command_label="setup")
        test = _run(
            commands.test,
            cwd=clean,
            timeout_seconds=TEST_TIMEOUT_SECONDS,
            environment_overrides=command_environment,
        )
        _require_success(
            "test command",
            test,
            completed_stages=("portable lock check", "setup"),
        )
        _require_delivery_preserved(clean, snapshot, command_label="test")
        start = _run(
            commands.start,
            cwd=clean,
            timeout_seconds=START_GRACE_SECONDS,
            keep_stdin_open=True,
            environment_overrides=command_environment,
        )
        if not start.timed_out:
            _require_success(
                "start command",
                start,
                completed_stages=("portable lock check", "setup", "test"),
            )
        _require_delivery_preserved(clean, snapshot, command_label="start")
        _verify_readme_examples(clean, snapshot, commands, command_environment)
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
