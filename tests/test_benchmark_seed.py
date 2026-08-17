"""Tests for safe benchmark seed repository preparation."""

import shutil
import subprocess
from pathlib import Path

import pytest

from software_agent_team.benchmark_seed import (
    BenchmarkSeedError,
    prepare_benchmark_seed,
)

REPOSITORY_ROOT = Path(__file__).parents[1]
SEED = REPOSITORY_ROOT / "benchmarks" / "task_manager" / "seed"


def git(repository: Path, *arguments: str) -> str:
    """Read test-owned Git state without a shell."""

    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_prepare_benchmark_seed_creates_one_clean_base_commit(tmp_path: Path) -> None:
    seed = tmp_path / "seed"
    shutil.copytree(SEED, seed)
    cache = seed / ".ruff_cache" / "test-state"
    cache.parent.mkdir(exist_ok=True)
    cache.write_text("ignored", encoding="utf-8")
    destination = tmp_path / "benchmark"

    commit = prepare_benchmark_seed(seed, destination)

    assert len(commit) == 40
    assert git(destination, "rev-parse", "HEAD") == commit
    assert git(destination, "status", "--porcelain=v1") == ""
    assert (destination / "pyproject.toml").is_file()
    assert git(destination, "log", "-1", "--format=%an <%ae>") == (
        "urntt <urntts@gmail.com>"
    )
    assert git(destination, "config", "--local", "--get", "user.name") == "urntt"
    assert (
        git(destination, "config", "--local", "--get", "user.email")
        == "urntts@gmail.com"
    )
    assert not (destination / ".ruff_cache").exists()
    assert prepare_benchmark_seed(seed, tmp_path / "second-benchmark") == commit
    with pytest.raises(BenchmarkSeedError, match="already exists"):
        prepare_benchmark_seed(SEED, destination)


def test_prepare_benchmark_seed_rejects_a_symlinked_input(tmp_path: Path) -> None:
    source = tmp_path / "seed"
    source.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (source / "link.txt").symlink_to(outside)

    with pytest.raises(BenchmarkSeedError, match="symbolic links"):
        prepare_benchmark_seed(source, tmp_path / "destination")


def test_prepare_benchmark_seed_preserves_an_invalid_destination(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "destination"
    destination.write_text("user-owned", encoding="utf-8")

    with pytest.raises(BenchmarkSeedError, match="already exists"):
        prepare_benchmark_seed(SEED, destination)

    assert destination.read_text(encoding="utf-8") == "user-owned"
