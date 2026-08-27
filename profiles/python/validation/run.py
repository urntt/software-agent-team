#!/usr/bin/env python3
"""Validate the generated-project command and documentation contract."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

MAX_MANIFEST_BYTES = 65_536
MAX_LOCK_BYTES = 1_048_576
EXPECTED_KEYS = {"schema_version", "setup", "start", "test"}


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
    if lock.exists() or lock.is_symlink():
        require_regular_file(lock, "uv.lock", max_bytes=MAX_LOCK_BYTES)
    elif not rules.intersection({"uv.lock", "/uv.lock"}):
        fail("uv.lock must be committed or explicitly excluded by .gitignore")
    else:
        require_effective_ignore(repository, "uv.lock", "uv.lock")


def require_effective_ignore(repository: Path, relative_path: str, label: str) -> None:
    """Verify the checked-in rule wins after Git applies later negations."""

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
    if result.returncode == 1:
        fail(f".gitignore must effectively exclude {label}")
    if result.returncode != 0:
        fail(f"cannot verify the .gitignore setup policy for {label}")


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


def validate(repository: Path) -> None:
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
    missing = {
        word
        for word in ("setup", "start", "test", "limitation")
        if word not in readme.lower()
    }
    if missing:
        fail(f"README.md is missing guidance for: {', '.join(sorted(missing))}")
    tests = repository / "tests"
    if tests.is_symlink() or not tests.is_dir() or not any(tests.rglob("test_*.py")):
        fail("tests must contain at least one test_*.py file")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True, type=Path)
    args = parser.parse_args()
    validate(args.repository)
    print("project contract: passed")


if __name__ == "__main__":
    main()
