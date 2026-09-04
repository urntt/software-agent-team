"""Immutable release manifests and stable-channel resolution."""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from software_agent_team.schema_compatibility import SchemaFamily, SchemaSupport
from software_agent_team.versioning import (
    InstallationRecord,
    ManagedChannel,
    compare_release_versions,
    parse_release_version,
)

RELEASE_MANIFEST_SCHEMA_VERSION = 1
RELEASE_MANIFEST_ASSET_NAME = "sat-release.json"
DEFAULT_REPOSITORY_URL = "https://github.com/urntt/software-agent-team.git"
DEFAULT_LATEST_RELEASE_API_URL = (
    "https://api.github.com/repos/urntt/software-agent-team/releases/latest"
)
MAX_RELEASE_METADATA_BYTES = 1_048_576
MAX_RELEASE_MANIFEST_BYTES = 262_144


class ReleaseResolutionError(RuntimeError):
    """Raised when a channel cannot resolve one verified immutable target."""


class ReleaseArtifactKind(StrEnum):
    """Deterministic source representation covered by the release digest."""

    GIT_ARCHIVE_TAR = "git_archive_tar"


class ReleaseManifest(BaseModel):
    """Release asset binding one SemVer to exact source and compatibility."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[RELEASE_MANIFEST_SCHEMA_VERSION] = (
        RELEASE_MANIFEST_SCHEMA_VERSION
    )
    release_version: str
    source_revision: str
    source_ref: str
    repository_url: str
    artifact_kind: Literal[ReleaseArtifactKind.GIT_ARCHIVE_TAR] = (
        ReleaseArtifactKind.GIT_ARCHIVE_TAR
    )
    artifact_digest: str
    schema_support: tuple[SchemaSupport, ...] = Field(min_length=1)
    created_at: datetime

    @field_validator("release_version")
    @classmethod
    def validate_release_version(cls, value: str) -> str:
        parse_release_version(value)
        return value

    @field_validator("source_revision")
    @classmethod
    def validate_source_revision(cls, value: str) -> str:
        if len(value) not in {40, 64} or any(
            character not in "0123456789abcdef" for character in value
        ):
            raise ValueError("source revision must be a full lowercase Git object ID")
        return value

    @field_validator("source_ref", "repository_url")
    @classmethod
    def validate_clean_source(cls, value: str) -> str:
        if (
            not value
            or value != value.strip()
            or any(character.isspace() for character in value)
        ):
            raise ValueError(
                "release source values must be non-blank without whitespace"
            )
        return value

    @field_validator("artifact_digest")
    @classmethod
    def validate_artifact_digest(cls, value: str) -> str:
        _parse_sha256_digest(value, label="release artifact digest")
        return value

    @field_validator("created_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must include a UTC offset")
        return value

    @model_validator(mode="after")
    def require_consistent_release(self) -> ReleaseManifest:
        if self.source_ref != f"v{self.release_version}":
            raise ValueError("release source_ref must exactly match its SemVer tag")
        families = tuple(item.family for item in self.schema_support)
        if len(families) != len(set(families)):
            raise ValueError("release schema support contains duplicate families")
        if set(families) != set(SchemaFamily):
            raise ValueError("release schema support must cover every schema family")
        return self


class ReleaseAssetMetadata(BaseModel):
    """Trusted subset of one GitHub release asset response."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    name: str
    state: str
    size: int = Field(ge=1, le=MAX_RELEASE_MANIFEST_BYTES)
    digest: str
    browser_download_url: str

    @field_validator("digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        _parse_sha256_digest(value, label="release-manifest asset digest")
        return value

    @field_validator("browser_download_url")
    @classmethod
    def require_https(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("release asset URL must use HTTPS")
        return value


class LatestReleaseMetadata(BaseModel):
    """Trusted subset of GitHub's latest-release response."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    tag_name: str
    draft: bool
    prerelease: bool
    published_at: datetime | None
    assets: tuple[ReleaseAssetMetadata, ...]


class ResolvedReleaseTarget(BaseModel):
    """Verified stable target consumed by install and update transactions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    channel: Literal[ManagedChannel.STABLE] = ManagedChannel.STABLE
    manifest: ReleaseManifest
    release_api_url: str
    manifest_url: str
    manifest_digest: str


class UpdateAvailability(StrEnum):
    """Meaningful stable-channel comparison result."""

    CURRENT = "current"
    AVAILABLE = "available"
    LOCAL_NEWER = "local_newer"
    INCONSISTENT = "inconsistent"


class StableUpdateCheck(BaseModel):
    """One deterministic comparison between an install and stable release."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: UpdateAvailability
    current_version: str
    target_version: str
    current_revision: str
    target_revision: str
    detail: str

    @property
    def update_available(self) -> bool:
        return self.status is UpdateAvailability.AVAILABLE


FetchBytes = Callable[[str, int], bytes]


def fetch_https_bytes(url: str, maximum_bytes: int) -> bytes:
    """Fetch one bounded HTTPS resource without accepting a silent oversize."""

    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ReleaseResolutionError("release metadata URL must use HTTPS")
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "software-agent-team",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            declared_length = response.headers.get("Content-Length")
            if declared_length is not None and int(declared_length) > maximum_bytes:
                raise ReleaseResolutionError("release response exceeds its size limit")
            payload = response.read(maximum_bytes + 1)
    except (OSError, ValueError, urllib.error.URLError) as error:
        if isinstance(error, ReleaseResolutionError):
            raise
        raise ReleaseResolutionError(
            f"could not fetch release metadata: {url}"
        ) from error
    if len(payload) > maximum_bytes:
        raise ReleaseResolutionError("release response exceeds its size limit")
    return payload


def resolve_latest_stable_release(
    *,
    release_api_url: str = DEFAULT_LATEST_RELEASE_API_URL,
    expected_repository_url: str = DEFAULT_REPOSITORY_URL,
    fetch: FetchBytes = fetch_https_bytes,
) -> ResolvedReleaseTarget:
    """Resolve GitHub's latest published stable release and verified manifest."""

    metadata = _load_json_model(
        fetch(release_api_url, MAX_RELEASE_METADATA_BYTES),
        LatestReleaseMetadata,
        "latest-release metadata",
    )
    if metadata.draft or metadata.prerelease or metadata.published_at is None:
        raise ReleaseResolutionError(
            "the latest-release endpoint did not return a published stable release"
        )
    if not metadata.tag_name.startswith("v"):
        raise ReleaseResolutionError("stable release tag must be vMAJOR.MINOR.PATCH")
    release_version = metadata.tag_name[1:]
    try:
        parse_release_version(release_version)
    except ValueError as error:
        raise ReleaseResolutionError(
            "stable release tag is not valid SemVer"
        ) from error
    matching_assets = tuple(
        asset for asset in metadata.assets if asset.name == RELEASE_MANIFEST_ASSET_NAME
    )
    if len(matching_assets) != 1:
        raise ReleaseResolutionError(
            "stable release must contain exactly one "
            f"{RELEASE_MANIFEST_ASSET_NAME} asset"
        )
    asset = matching_assets[0]
    if asset.state != "uploaded":
        raise ReleaseResolutionError("stable release manifest asset is not uploaded")
    manifest_bytes = fetch(asset.browser_download_url, MAX_RELEASE_MANIFEST_BYTES)
    actual_manifest_digest = f"sha256:{hashlib.sha256(manifest_bytes).hexdigest()}"
    if actual_manifest_digest != asset.digest:
        raise ReleaseResolutionError(
            "stable release manifest digest does not match GitHub"
        )
    manifest = _load_json_model(manifest_bytes, ReleaseManifest, "release manifest")
    if manifest.release_version != release_version:
        raise ReleaseResolutionError(
            "release manifest version does not match its GitHub tag"
        )
    if manifest.source_ref != metadata.tag_name:
        raise ReleaseResolutionError(
            "release manifest ref does not match its GitHub tag"
        )
    if manifest.repository_url != expected_repository_url:
        raise ReleaseResolutionError(
            "release manifest repository is not the expected source"
        )
    return ResolvedReleaseTarget(
        manifest=manifest,
        release_api_url=release_api_url,
        manifest_url=asset.browser_download_url,
        manifest_digest=actual_manifest_digest,
    )


def compare_stable_target(
    current: InstallationRecord,
    *,
    release_version: str,
    source_revision: str,
    source_ref: str,
    artifact_digest: str,
) -> StableUpdateCheck:
    """Compare stable release identity without treating commit drift as an update."""

    if current.channel is not ManagedChannel.STABLE:
        raise ReleaseResolutionError(
            "stable update comparison requires a stable install"
        )
    ordering = compare_release_versions(
        current.release_version,
        release_version,
    )
    if ordering < 0:
        status = UpdateAvailability.AVAILABLE
        detail = f"SAT {release_version} is available; run `sat update` to install it"
    elif ordering > 0:
        status = UpdateAvailability.LOCAL_NEWER
        detail = "the installed release is newer than the published stable release"
    elif (
        current.source_revision != source_revision
        or current.artifact_digest != artifact_digest
        or current.source_ref != source_ref
    ):
        status = UpdateAvailability.INCONSISTENT
        detail = "the same release version is bound to conflicting immutable provenance"
    else:
        status = UpdateAvailability.CURRENT
        detail = "the installed stable release is current"
    return StableUpdateCheck(
        status=status,
        current_version=current.release_version,
        target_version=release_version,
        current_revision=current.source_revision,
        target_revision=source_revision,
        detail=detail,
    )


def release_manifest_bytes(manifest: ReleaseManifest) -> bytes:
    """Serialize a release manifest canonically for hashing and publication."""

    payload = manifest.model_dump(mode="json")
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()


def git_archive_digest(repository: Path, revision: str = "HEAD") -> str:
    """Hash the deterministic uncompressed Git archive for one revision."""

    import subprocess

    try:
        completed = subprocess.run(
            ["git", "-C", str(repository), "archive", "--format=tar", revision],
            check=True,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise ReleaseResolutionError("could not create the source archive") from error
    return f"sha256:{hashlib.sha256(completed.stdout).hexdigest()}"


def _parse_sha256_digest(value: str, *, label: str) -> bytes:
    prefix = "sha256:"
    if not value.startswith(prefix):
        raise ValueError(f"{label} must be a sha256 digest")
    digest = value.removeprefix(prefix)
    if len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise ValueError(f"{label} must be a sha256 digest")
    return bytes.fromhex(digest)


def _load_json_model[ModelT: BaseModel](
    payload: bytes,
    model: type[ModelT],
    label: str,
) -> ModelT:
    try:
        value = json.loads(payload)
        return model.model_validate(value)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
        raise ReleaseResolutionError(f"{label} is invalid") from error
