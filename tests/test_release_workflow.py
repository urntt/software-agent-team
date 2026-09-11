"""Static safety checks for repository-owned release automation."""

from __future__ import annotations

from pathlib import Path

REPOSITORY_ROOT = Path(__file__).parents[1]
UPLOAD_ARTIFACT_NODE24_REVISION = "043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"


def test_release_workflow_gates_exact_tag_before_one_release_publication() -> None:
    workflow = (REPOSITORY_ROOT / ".github/workflows/release.yml").read_text(
        encoding="utf-8"
    )

    assert "tags:" in workflow
    assert '"v*.*.*"' in workflow
    assert "fetch-depth: 0" in workflow
    assert "persist-credentials: false" in workflow
    assert 'UV_BIN="$(command -v uv)" make setup' in workflow
    assert 'make check UV="$(command -v uv)"' in workflow
    assert "uv run pytest" not in workflow
    assert "ruff check" not in workflow
    assert "if: always()" in workflow
    assert "path: artifacts/generated/full-gate/" in workflow
    assert "continue-on-error" not in workflow
    assert workflow.index("make check") < workflow.index("Retain canonical")
    assert workflow.index("Retain canonical") < workflow.index("scripts/release.py")
    assert workflow.index("scripts/release.py") < workflow.index("gh release create")
    manifest_step = workflow.split("- name: Build release identity manifest")[1]
    assert "if: always()" not in manifest_step
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


def test_release_workflow_preserves_gate_artifact_contract_on_node24() -> None:
    workflow = (REPOSITORY_ROOT / ".github/workflows/release.yml").read_text(
        encoding="utf-8"
    )
    upload_step = workflow.split("- name: Retain canonical gate evidence", maxsplit=1)[
        1
    ].split("- name: Build release identity manifest", maxsplit=1)[0]

    assert "if: always()" in upload_step
    assert (
        f"uses: actions/upload-artifact@{UPLOAD_ARTIFACT_NODE24_REVISION}"
        in upload_step
    )
    assert "name: release-gate-${{ github.sha }}" in upload_step
    assert "path: artifacts/generated/full-gate/" in upload_step
    assert "if-no-files-found: warn" in upload_step
    assert "retention-days: 0" in upload_step
    assert "archive: true" in upload_step
    assert "continue-on-error" not in upload_step


def test_release_workflow_keeps_minimal_repository_permissions() -> None:
    workflow = (REPOSITORY_ROOT / ".github/workflows/release.yml").read_text(
        encoding="utf-8"
    )
    permissions = workflow.split("permissions:", maxsplit=1)[1].split(
        "concurrency:", maxsplit=1
    )[0]

    assert permissions.strip() == "contents: write"
