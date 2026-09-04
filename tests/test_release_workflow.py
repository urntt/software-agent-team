"""Static safety checks for repository-owned release automation."""

from __future__ import annotations

from pathlib import Path

REPOSITORY_ROOT = Path(__file__).parents[1]


def test_release_workflow_gates_exact_tag_before_one_release_publication() -> None:
    workflow = (REPOSITORY_ROOT / ".github/workflows/release.yml").read_text(
        encoding="utf-8"
    )

    assert "tags:" in workflow
    assert '"v*.*.*"' in workflow
    assert "fetch-depth: 0" in workflow
    assert "persist-credentials: false" in workflow
    assert "ruff format --check" in workflow
    assert "ruff check" in workflow
    assert "uv run pytest" in workflow
    assert "scripts/release.py" in workflow
    assert '--tag "${GITHUB_REF_NAME}"' in workflow
    assert "gh release view" in workflow
    assert "gh release create" in workflow
    assert "--verify-tag" in workflow
    assert "dist/sat-release.json" in workflow


def test_release_workflow_pins_third_party_actions_to_full_commits() -> None:
    workflow = (REPOSITORY_ROOT / ".github/workflows/release.yml").read_text(
        encoding="utf-8"
    )
    uses_lines = [
        line.strip()
        for line in workflow.splitlines()
        if line.strip().startswith("uses:")
    ]

    assert uses_lines
    for line in uses_lines:
        revision = line.split("@", maxsplit=1)[1].split(maxsplit=1)[0]
        assert len(revision) == 40
        assert all(character in "0123456789abcdef" for character in revision)
