"""Tests for impact-driven release-candidate gates."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from software_agent_team.release_tools import (
    ChangeImpact,
    ReleaseChangeSet,
    ReleaseGateError,
    build_release_manifest,
    minimum_target_version,
)
from software_agent_team.schema_compatibility import SchemaFamily


def git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", repository, *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def prepare_candidate(tmp_path: Path, *, version: str = "0.1.0") -> Path:
    repository = tmp_path / "candidate"
    (repository / "release").mkdir(parents=True)
    (repository / "pyproject.toml").write_text(
        f'[project]\nname = "software-agent-team"\nversion = "{version}"\n',
        encoding="utf-8",
    )
    (repository / "uv.lock").write_text(
        f'''version = 1

[[package]]
name = "software-agent-team"
version = "{version}"
source = {{ editable = "." }}
''',
        encoding="utf-8",
    )
    (repository / "release/change-impact.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "baseline_version": None,
                "target_version": version,
                "changes": [
                    {
                        "id": "initial-release",
                        "impact": "minor",
                        "summary": "Initial user-visible product release",
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    git(repository, "init", "-b", "main")
    git(repository, "config", "user.name", "urntt")
    git(repository, "config", "user.email", "urntts@gmail.com")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "test: prepare release candidate")
    return repository


@pytest.mark.parametrize(
    ("baseline", "impact", "expected"),
    [
        (None, ChangeImpact.PATCH, "0.1.0"),
        ("0.1.0", ChangeImpact.PATCH, "0.1.1"),
        ("0.1.7", ChangeImpact.MINOR, "0.2.0"),
        ("0.1.7", ChangeImpact.MAJOR, "0.2.0"),
        ("1.4.2", ChangeImpact.MINOR, "1.5.0"),
        ("1.4.2", ChangeImpact.MAJOR, "2.0.0"),
    ],
)
def test_minimum_version_follows_impact_and_zero_major_policy(
    baseline: str | None,
    impact: ChangeImpact,
    expected: str,
) -> None:
    assert minimum_target_version(baseline, impact) == expected


def test_change_set_rejects_an_insufficient_increment() -> None:
    with pytest.raises(ValidationError, match=r"below required 0\.2\.0"):
        ReleaseChangeSet(
            baseline_version="0.1.0",
            target_version="0.1.1",
            changes=(
                {
                    "id": "new-command",
                    "impact": "minor",
                    "summary": "Add a new user-visible command surface",
                },
            ),
        )


def test_release_manifest_binds_clean_tag_commit_archive_and_schemas(
    tmp_path: Path,
) -> None:
    repository = prepare_candidate(tmp_path)
    git(repository, "tag", "v0.1.0")
    created_at = datetime(2026, 9, 4, tzinfo=UTC)

    manifest = build_release_manifest(
        repository=repository,
        tag="v0.1.0",
        repository_url="https://example.invalid/software-agent-team.git",
        require_tag=True,
        created_at=created_at,
    )

    assert manifest.release_version == "0.1.0"
    assert manifest.source_revision == git(repository, "rev-parse", "HEAD")
    assert manifest.source_ref == "v0.1.0"
    assert manifest.artifact_digest.startswith("sha256:")
    assert {item.family for item in manifest.schema_support} == set(SchemaFamily)
    assert manifest.created_at == created_at


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("dirty", "working tree must be clean"),
        ("version", "package version does not match"),
        ("lock-version", "locked project version does not match"),
        ("impact", "change-impact target does not match"),
        ("missing-tag", "tag does not exist"),
    ],
)
def test_release_gate_rejects_identity_drift(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    repository = prepare_candidate(tmp_path)
    if mutation != "missing-tag":
        git(repository, "tag", "v0.1.0")
    if mutation == "dirty":
        (repository / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    elif mutation == "version":
        (repository / "pyproject.toml").write_text(
            '[project]\nname = "software-agent-team"\nversion = "0.2.0"\n',
            encoding="utf-8",
        )
        git(repository, "add", ".")
        git(repository, "commit", "-m", "test: drift package version")
        git(repository, "tag", "-f", "v0.1.0")
    elif mutation == "lock-version":
        path = repository / "uv.lock"
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                'version = "0.1.0"', 'version = "0.2.0"'
            ),
            encoding="utf-8",
        )
        git(repository, "add", ".")
        git(repository, "commit", "-m", "test: drift locked project version")
        git(repository, "tag", "-f", "v0.1.0")
    elif mutation == "impact":
        path = repository / "release/change-impact.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["target_version"] = "0.2.0"
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        git(repository, "add", ".")
        git(repository, "commit", "-m", "test: drift impact target")
        git(repository, "tag", "-f", "v0.1.0")

    with pytest.raises(ReleaseGateError, match=message):
        build_release_manifest(
            repository=repository,
            tag="v0.1.0",
            require_tag=True,
        )


def test_untagged_candidate_refuses_a_version_tag_bound_elsewhere(
    tmp_path: Path,
) -> None:
    repository = prepare_candidate(tmp_path)
    git(repository, "tag", "v0.1.0")
    (repository / "next.txt").write_text("next\n", encoding="utf-8")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "test: move candidate")

    with pytest.raises(ReleaseGateError, match="already bound elsewhere"):
        build_release_manifest(
            repository=repository,
            tag="v0.1.0",
            require_tag=False,
        )


def test_release_gate_binds_change_impact_to_the_highest_prior_version(
    tmp_path: Path,
) -> None:
    repository = prepare_candidate(tmp_path, version="0.1.1")
    git(repository, "tag", "v0.1.0")

    with pytest.raises(ReleaseGateError, match=r"baseline.*expected 0\.1\.0"):
        build_release_manifest(
            repository=repository,
            tag="v0.1.1",
            require_tag=False,
        )

    path = repository / "release/change-impact.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["baseline_version"] = "0.1.0"
    payload["changes"][0]["impact"] = "patch"
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "test: bind prior release")

    manifest = build_release_manifest(
        repository=repository,
        tag="v0.1.1",
        require_tag=False,
    )

    assert manifest.release_version == "0.1.1"
