"""Safe preparation of the fixed Phase 1 benchmark seed repository."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


class BenchmarkSeedError(RuntimeError):
    """Raised when the controlled benchmark seed cannot be prepared safely."""


SEED_IGNORE_PATTERNS = (
    "__pycache__",
    "*.pyc",
    "*.pyo",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "*.db",
    "*.sqlite",
    "*.sqlite3",
)


def _git_environment() -> dict[str, str]:
    return {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_DATE": "2026-08-09T00:00:00Z",
        "GIT_COMMITTER_DATE": "2026-08-09T00:00:00Z",
    }


def _run_git(repository: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-C",
                str(repository),
                *arguments,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
            shell=False,
            stdin=subprocess.DEVNULL,
            env=_git_environment(),
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise BenchmarkSeedError(
            "Git could not initialize the benchmark seed"
        ) from error
    return result.stdout.strip()


def prepare_benchmark_seed(
    seed: Path,
    destination: Path,
    *,
    author_name: str = "urntt",
    author_email: str = "urntts@gmail.com",
) -> str:
    """Copy the trusted seed once, initialize Git, and return its base commit."""

    if not author_name.strip() or not author_email.strip():
        raise BenchmarkSeedError("benchmark commit identity must not be blank")
    try:
        resolved_seed = seed.resolve(strict=True)
    except OSError as error:
        raise BenchmarkSeedError("benchmark seed does not exist") from error
    if not resolved_seed.is_dir() or resolved_seed.is_symlink():
        raise BenchmarkSeedError("benchmark seed must be a real directory")
    if (resolved_seed / ".git").exists() or (resolved_seed / ".git").is_symlink():
        raise BenchmarkSeedError("benchmark seed cannot contain Git metadata")
    if any(path.is_symlink() for path in resolved_seed.rglob("*")):
        raise BenchmarkSeedError("benchmark seed cannot contain symbolic links")
    if destination.exists() or destination.is_symlink():
        raise BenchmarkSeedError(f"benchmark destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.parent.is_symlink() or not destination.parent.is_dir():
        raise BenchmarkSeedError("benchmark destination parent must be a directory")
    resolved_parent = destination.parent.resolve(strict=True)
    if resolved_parent == resolved_seed or resolved_parent.is_relative_to(
        resolved_seed
    ):
        raise BenchmarkSeedError("benchmark destination cannot be inside the seed")

    shutil.copytree(
        resolved_seed,
        destination,
        ignore=shutil.ignore_patterns(*SEED_IGNORE_PATTERNS),
    )
    _run_git(destination, "init", "-b", "main")
    _run_git(destination, "config", "--local", "user.name", author_name.strip())
    _run_git(destination, "config", "--local", "user.email", author_email.strip())
    _run_git(destination, "add", ".")
    _run_git(
        destination,
        "commit",
        "--no-verify",
        "-m",
        "chore: initialize task-manager benchmark",
    )
    return _run_git(destination, "rev-parse", "HEAD")
