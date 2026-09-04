"""Tests for immutable stable-release resolution and comparison."""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from software_agent_team.releases import (
    RELEASE_MANIFEST_ASSET_NAME,
    ReleaseManifest,
    ReleaseResolutionError,
    UpdateAvailability,
    compare_stable_target,
    git_archive_digest,
    release_manifest_bytes,
    resolve_latest_stable_release,
)
from software_agent_team.schema_compatibility import supported_schemas
from software_agent_team.versioning import ManagedChannel, make_installation_record


def manifest(*, version: str = "0.2.0", revision: str = "b" * 40) -> ReleaseManifest:
    return ReleaseManifest(
        release_version=version,
        source_revision=revision,
        source_ref=f"v{version}",
        repository_url="https://example.invalid/software-agent-team.git",
        artifact_digest="sha256:" + "c" * 64,
        schema_support=supported_schemas(),
        created_at=datetime(2026, 9, 4, tzinfo=UTC),
    )


def release_payload(
    manifest_payload: bytes,
    *,
    tag: str = "v0.2.0",
    draft: bool = False,
    prerelease: bool = False,
    digest: str | None = None,
) -> bytes:
    return json.dumps(
        {
            "tag_name": tag,
            "draft": draft,
            "prerelease": prerelease,
            "published_at": "2026-09-04T00:00:00Z",
            "assets": [
                {
                    "name": RELEASE_MANIFEST_ASSET_NAME,
                    "state": "uploaded",
                    "size": len(manifest_payload),
                    "digest": digest
                    or "sha256:" + hashlib.sha256(manifest_payload).hexdigest(),
                    "browser_download_url": (
                        "https://example.invalid/releases/v0.2.0/sat-release.json"
                    ),
                }
            ],
        }
    ).encode()


def test_latest_stable_release_requires_matching_asset_digest_and_identity() -> None:
    encoded_manifest = release_manifest_bytes(manifest())
    encoded_release = release_payload(encoded_manifest)
    responses = {
        "https://example.invalid/latest": encoded_release,
        "https://example.invalid/releases/v0.2.0/sat-release.json": encoded_manifest,
    }

    target = resolve_latest_stable_release(
        release_api_url="https://example.invalid/latest",
        expected_repository_url="https://example.invalid/software-agent-team.git",
        fetch=lambda url, _limit: responses[url],
    )

    assert target.manifest.release_version == "0.2.0"
    assert target.manifest.source_revision == "b" * 40
    assert target.manifest_digest == (
        "sha256:" + hashlib.sha256(encoded_manifest).hexdigest()
    )


@pytest.mark.parametrize(
    ("release_options", "manifest_override", "message"),
    [
        ({"draft": True}, None, "published stable"),
        ({"prerelease": True}, None, "published stable"),
        ({"tag": "preview"}, None, "vMAJOR"),
        ({"digest": "sha256:" + "0" * 64}, None, "digest does not match"),
        ({"tag": "v0.3.0"}, None, "version does not match"),
        ({}, {"repository_url": "https://evil.invalid/repo.git"}, "repository"),
    ],
)
def test_latest_stable_release_rejects_ambiguous_or_mutated_metadata(
    release_options: dict[str, object],
    manifest_override: dict[str, object] | None,
    message: str,
) -> None:
    payload = manifest().model_dump(mode="json")
    if manifest_override:
        payload.update(manifest_override)
    encoded_manifest = (json.dumps(payload) + "\n").encode()
    encoded_release = release_payload(encoded_manifest, **release_options)
    responses = {
        "https://example.invalid/latest": encoded_release,
        "https://example.invalid/releases/v0.2.0/sat-release.json": encoded_manifest,
    }

    with pytest.raises(ReleaseResolutionError, match=message):
        resolve_latest_stable_release(
            release_api_url="https://example.invalid/latest",
            expected_repository_url="https://example.invalid/software-agent-team.git",
            fetch=lambda url, _limit: responses[url],
        )


def test_release_manifest_requires_all_schema_families_exactly_once() -> None:
    support = supported_schemas()
    with pytest.raises(ValidationError, match="every schema family"):
        ReleaseManifest(
            **{
                **manifest().model_dump(),
                "schema_support": support[:-1],
            }
        )
    with pytest.raises(ValidationError, match="duplicate families"):
        ReleaseManifest(
            **{
                **manifest().model_dump(),
                "schema_support": (*support, support[0]),
            }
        )


def test_stable_comparison_uses_semver_and_detects_same_version_rebinding(
    tmp_path: Path,
) -> None:
    current = make_installation_record(
        channel=ManagedChannel.STABLE,
        release_version="0.1.0",
        source_revision="a" * 40,
        source_ref="v0.1.0",
        repository_url="https://example.invalid/software-agent-team.git",
        application_path=tmp_path / "app",
        artifact_digest="sha256:" + "d" * 64,
    )

    available = compare_stable_target(
        current,
        release_version=manifest().release_version,
        source_revision=manifest().source_revision,
        source_ref=manifest().source_ref,
        artifact_digest=manifest().artifact_digest,
    )
    assert available.status is UpdateAvailability.AVAILABLE
    assert available.update_available
    assert "sat update" in available.detail

    rebound = manifest(version="0.1.0", revision="b" * 40)
    inconsistent = compare_stable_target(
        current,
        release_version=rebound.release_version,
        source_revision=rebound.source_revision,
        source_ref=rebound.source_ref,
        artifact_digest=rebound.artifact_digest,
    )
    assert inconsistent.status is UpdateAvailability.INCONSISTENT
    assert not inconsistent.update_available


def test_git_archive_digest_is_stable_for_one_commit_and_changes_with_source(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-b", "main", repository], check=True)
    subprocess.run(
        ["git", "-C", repository, "config", "user.name", "urntt"], check=True
    )
    subprocess.run(
        ["git", "-C", repository, "config", "user.email", "urntts@gmail.com"],
        check=True,
    )
    source = repository / "source.txt"
    source.write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "-C", repository, "add", "."], check=True)
    subprocess.run(
        ["git", "-C", repository, "commit", "-m", "test: add source"],
        check=True,
        capture_output=True,
    )
    first = git_archive_digest(repository)
    assert git_archive_digest(repository) == first
    source.write_text("two\n", encoding="utf-8")
    subprocess.run(["git", "-C", repository, "add", "."], check=True)
    subprocess.run(
        ["git", "-C", repository, "commit", "-m", "test: change source"],
        check=True,
        capture_output=True,
    )
    assert git_archive_digest(repository) != first


def resolve_target_for_test(release: ReleaseManifest):
    encoded_manifest = release_manifest_bytes(release)
    encoded_release = release_payload(
        encoded_manifest,
        tag=release.source_ref,
    )
    responses = {
        "https://example.invalid/latest": encoded_release,
        "https://example.invalid/releases/v0.2.0/sat-release.json": encoded_manifest,
    }
    return resolve_latest_stable_release(
        release_api_url="https://example.invalid/latest",
        expected_repository_url="https://example.invalid/software-agent-team.git",
        fetch=lambda url, _limit: responses[url],
    )
