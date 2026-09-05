"""Regression checks for the public documentation ownership boundary."""

from pathlib import Path

from software_agent_team.user_configuration import (
    USER_CONFIGURATION_SCHEMA_VERSION,
)

REPOSITORY_ROOT = Path(__file__).parents[1]


def test_readme_remains_a_user_entrypoint_instead_of_a_status_ledger() -> None:
    readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")

    assert "[`STATUS.md`](STATUS.md)" in readme
    assert "## Current Scope and Maturity" not in readme
    assert "**Current milestone:**" not in readme
    assert "**Last updated:**" not in readme
    assert "fresh-account rehearsal" not in readme
    assert "provider-backed rehearsal" not in readme


def test_user_and_maintainer_release_guides_have_distinct_entries() -> None:
    installation = (REPOSITORY_ROOT / "docs/installation.md").read_text(
        encoding="utf-8"
    )
    release_guide = (REPOSITORY_ROOT / "docs/releases.md").read_text(encoding="utf-8")
    index = (REPOSITORY_ROOT / "docs/README.md").read_text(encoding="utf-8")

    assert "sat update --check" in installation
    assert "sat channel switch dev" in installation
    assert "not implemented yet" not in installation
    assert "release/change-impact.json" in release_guide
    assert "scripts/release.py" in release_guide
    assert "[`releases.md`](releases.md)" in index


def test_installation_guide_tracks_the_authoritative_configuration_schema() -> None:
    installation = (REPOSITORY_ROOT / "docs/installation.md").read_text(
        encoding="utf-8"
    )

    assert f"Schema version {USER_CONFIGURATION_SCHEMA_VERSION} stores" in installation
    assert (
        "Existing schema-v1 through "
        f"schema-v{USER_CONFIGURATION_SCHEMA_VERSION - 1} values migrate\n"
        f"one way into schema {USER_CONFIGURATION_SCHEMA_VERSION};"
    ) in installation
