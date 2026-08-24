"""Static reproducibility checks for the shared Python sandbox image recipe."""

import re
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).parents[1]
RUNTIME_ROOT = REPOSITORY_ROOT / "runtime" / "python"
PINNED_REQUIREMENT = re.compile(r"^[a-z0-9][a-z0-9._-]*==[^ ;]+(?: ; [a-z0-9_' .=]+)?$")


def requirement_lines(path: Path) -> tuple[str, ...]:
    """Return meaningful requirement lines without comments."""

    return tuple(
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def test_runtime_image_uses_content_pinned_base_and_dependency_lock() -> None:
    dockerfile = (RUNTIME_ROOT / "Dockerfile").read_text(encoding="utf-8")
    first_line = dockerfile.splitlines()[0]

    assert re.fullmatch(
        r"FROM python:3\.12\.13-slim-bookworm@sha256:[0-9a-f]{64}",
        first_line,
    )
    assert "COPY requirements.lock /opt/software-agent-team/requirements.lock" in (
        dockerfile
    )
    assert "pip install --no-cache-dir --requirement" in dockerfile
    assert 'CMD ["sleep", "infinity"]' in dockerfile


def test_runtime_dependency_lock_contains_only_exact_unique_versions() -> None:
    direct = requirement_lines(RUNTIME_ROOT / "requirements.in")
    locked = requirement_lines(RUNTIME_ROOT / "requirements.lock")

    assert len(locked) == len(set(locked))
    assert all(PINNED_REQUIREMENT.fullmatch(requirement) for requirement in locked)
    assert set(direct) <= set(locked)
