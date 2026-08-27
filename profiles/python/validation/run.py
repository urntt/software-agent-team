#!/usr/bin/env python3
"""Validate the generated-project command and documentation contract."""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import urlsplit

MAX_MANIFEST_BYTES = 65_536
MAX_LOCK_BYTES = 1_048_576
EXPECTED_KEYS = {"schema_version", "setup", "start", "test"}


@dataclass(frozen=True)
class ProjectCommands:
    """Validated exact commands owned by one generated project."""

    setup: tuple[str, ...]
    start: tuple[str, ...]
    test: tuple[str, ...]


def documents_exact_command(readme: str, argv: tuple[str, ...]) -> bool:
    """Recognize one exact shell command without accepting a longer variant."""

    candidates: set[str] = set(re.findall(r"`([^`\r\n]+)`", readme))
    for line in readme.splitlines():
        candidate = line.strip().strip("`").strip()
        if candidate.startswith("$ "):
            candidate = candidate[2:].strip()
        candidates.add(candidate)
    rendered = {" ".join(argv), shlex.join(argv)}
    return not candidates.isdisjoint(rendered)


def documents_concept(readme: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, readme, flags=re.IGNORECASE) for pattern in patterns)


def fail(message: str) -> None:
    print(f"project contract: {message}", file=sys.stderr)
    raise SystemExit(1)


def require_regular_file(
    path: Path,
    label: str,
    *,
    max_bytes: int = MAX_MANIFEST_BYTES,
) -> bytes:
    if path.is_symlink() or not path.is_file():
        fail(f"{label} must be a regular file")
    content = path.read_bytes()
    if len(content) > max_bytes:
        fail(f"{label} is too large")
    return content


def require_setup_artifact_policy(repository: Path) -> None:
    """Keep the documented setup command from dirtying a clean delivery."""

    gitignore = require_regular_file(
        repository / ".gitignore",
        ".gitignore",
    ).decode("utf-8", errors="strict")
    rules = {
        line.strip()
        for line in gitignore.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    if not rules.intersection({".venv", ".venv/", "/.venv", "/.venv/"}):
        fail(".gitignore must exclude the root .venv setup directory")
    require_effective_ignore(repository, ".venv/.sat-setup-probe", "root .venv")
    lock = repository / "uv.lock"
    lock_present = lock.exists() or lock.is_symlink()
    lock_tracked = is_git_tracked(repository, "uv.lock")
    lock_ignored = is_effectively_ignored(repository, "uv.lock", "uv.lock")
    if lock_tracked or (lock_present and not lock_ignored):
        content = require_regular_file(lock, "uv.lock", max_bytes=MAX_LOCK_BYTES)
        require_portable_uv_lock(repository, content)
    elif not rules.intersection({"uv.lock", "/uv.lock"}):
        fail("uv.lock must be committed or explicitly excluded by .gitignore")
    else:
        require_effective_ignore(repository, "uv.lock", "uv.lock")


def _require_portable_local_reference(
    repository: Path,
    value: object,
    *,
    label: str,
) -> None:
    """Require one lockfile-local source to remain inside the delivered project."""

    if not isinstance(value, str) or not value or "\x00" in value:
        fail(f"uv.lock {label} must be a non-empty path")
    parsed = urlsplit(value)
    path = PurePosixPath(value)
    windows_path = PureWindowsPath(value)
    if (
        parsed.scheme.casefold() == "file"
        or path.is_absolute()
        or windows_path.is_absolute()
        or bool(windows_path.drive)
        or "\\" in value
        or ".." in path.parts
    ):
        fail(f"uv.lock contains a non-portable local reference in {label}")
    candidate = repository.joinpath(*path.parts)
    cursor = repository
    for part in path.parts:
        cursor /= part
        if cursor.is_symlink():
            fail(f"uv.lock {label} cannot traverse a symlink")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(repository)
    except (OSError, ValueError):
        fail(f"uv.lock {label} must reference an existing path inside the project")


def _require_remote_lock_reference(value: object, *, label: str) -> None:
    """Reject host-local values in lock fields that must identify remote sources."""

    if not isinstance(value, str) or not value or "\x00" in value:
        fail(f"uv.lock {label} must be a non-empty URL")
    parsed = urlsplit(value)
    path = PurePosixPath(value)
    windows_path = PureWindowsPath(value)
    if (
        parsed.scheme.casefold() == "file"
        or path.is_absolute()
        or windows_path.is_absolute()
        or bool(windows_path.drive)
        or "\\" in value
        or ".." in path.parts
    ):
        fail(f"uv.lock contains a non-portable local reference in {label}")
    if label.endswith("registry") or label.endswith("url"):
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
            fail(f"uv.lock {label} must use an HTTP(S) URL")
    elif (
        parsed.scheme.casefold()
        not in {
            "git",
            "git+http",
            "git+https",
            "git+ssh",
            "http",
            "https",
            "ssh",
        }
        or not parsed.netloc
    ):
        fail(f"uv.lock {label} must use a remote Git URL")


def require_portable_uv_lock(repository: Path, content: bytes) -> None:
    """Reject lock sources that only exist inside SAT's verification sandbox."""

    try:
        payload = tomllib.loads(content.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        fail("uv.lock must contain valid UTF-8 TOML")

    local_keys = {"path", "editable", "directory", "virtual"}
    remote_keys = {"registry", "url", "git"}

    def inspect(value: object, location: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                child_location = f"{location}.{key}"
                if key in local_keys:
                    _require_portable_local_reference(
                        repository,
                        child,
                        label=child_location,
                    )
                elif key in remote_keys:
                    _require_remote_lock_reference(child, label=child_location)
                inspect(child, child_location)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                inspect(child, f"{location}[{index}]")

    inspect(payload, "root")


def is_effectively_ignored(repository: Path, relative_path: str, label: str) -> bool:
    """Return whether Git excludes one path after applying every ignore rule."""

    result = subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "check-ignore",
            "--no-index",
            "--quiet",
            "--",
            relative_path,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode in {0, 1}:
        return result.returncode == 0
    else:
        fail(f"cannot verify the .gitignore setup policy for {label}")


def require_effective_ignore(repository: Path, relative_path: str, label: str) -> None:
    """Verify the checked-in rule wins after Git applies later negations."""

    if not is_effectively_ignored(repository, relative_path, label):
        fail(f".gitignore must effectively exclude {label}")


def is_git_tracked(repository: Path, relative_path: str) -> bool:
    """Return whether one path belongs to the proposed Git delivery."""

    result = subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "ls-files",
            "--error-unmatch",
            "--",
            relative_path,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode in {0, 1}:
        return result.returncode == 0
    fail(f"cannot verify whether {relative_path} belongs to the Git delivery")


def validate_argv(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not 3 <= len(value) <= 32:
        fail(f"{label} must be an argv array with 3-32 entries")
    if any(
        not isinstance(item, str)
        or not item
        or len(item) > 1024
        or any(character in item for character in ("\x00", "\r", "\n"))
        for item in value
    ):
        fail(f"{label} contains an invalid argv entry")
    result = tuple(value)
    if sum(len(item) for item in result) > 4096:
        fail(f"{label} is too large")
    return result


def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            fail(f"sat-project.json contains duplicate key: {key}")
        result[key] = value
    return result


def validate(repository: Path) -> ProjectCommands:
    try:
        repository = repository.resolve(strict=True)
    except OSError:
        fail("repository does not exist")
    if not repository.is_dir():
        fail("repository must be a directory")

    raw = require_regular_file(repository / "sat-project.json", "sat-project.json")
    try:
        payload = json.loads(raw, object_pairs_hook=reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError):
        fail("sat-project.json must contain valid UTF-8 JSON")
    if not isinstance(payload, dict) or set(payload) != EXPECTED_KEYS:
        fail("sat-project.json must contain exactly the documented keys")
    if payload["schema_version"] != 1:
        fail("sat-project.json schema_version must be 1")

    setup = validate_argv(payload["setup"], "setup")
    start = validate_argv(payload["start"], "start")
    test = validate_argv(payload["test"], "test")
    if setup != ("uv", "sync", "--dev"):
        fail("setup must be: uv sync --dev")
    if start[:2] != ("uv", "run"):
        fail("start must begin with: uv run")
    if start[2] in {
        "bash",
        "cmd",
        "cmd.exe",
        "dash",
        "fish",
        "ksh",
        "powershell",
        "pwsh",
        "sh",
        "zsh",
    }:
        fail("start cannot invoke a command shell")
    if "replace-with-project-entrypoint" in start:
        fail("start still contains the starter placeholder")
    if test != ("uv", "run", "pytest"):
        fail("test must be: uv run pytest")

    require_setup_artifact_policy(repository)
    require_regular_file(repository / "pyproject.toml", "pyproject.toml")
    readme = require_regular_file(repository / "README.md", "README.md").decode(
        "utf-8", errors="strict"
    )
    guidance_patterns = {
        "setup": (r"\bsetup\b", r"\binstall(?:ation|ing)?\b"),
        "start": (r"\bstart(?:up|ing)?\b", r"\brun(?:ning)?\b", r"\busage\b"),
        "test": (r"\btests?\b", r"\btesting\b"),
        "limitation": (r"\blimitations?\b", r"\bknown issues?\b"),
    }
    missing = {
        label
        for label, patterns in guidance_patterns.items()
        if not documents_concept(readme, patterns)
    }
    if missing:
        fail(f"README.md is missing guidance for: {', '.join(sorted(missing))}")
    undocumented_commands = {
        label
        for label, argv in (("setup", setup), ("start", start), ("test", test))
        if not documents_exact_command(readme, argv)
    }
    if undocumented_commands:
        fail(
            "README.md is missing exact command guidance for: "
            + ", ".join(sorted(undocumented_commands))
        )
    tests = repository / "tests"
    if tests.is_symlink() or not tests.is_dir() or not any(tests.rglob("test_*.py")):
        fail("tests must contain at least one test_*.py file")
    return ProjectCommands(setup=setup, start=start, test=test)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True, type=Path)
    args = parser.parse_args()
    validate(args.repository)
    print("project contract: passed")


if __name__ == "__main__":
    main()
