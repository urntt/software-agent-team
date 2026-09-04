"""Repository-owned SemVer impact and release-candidate gates."""

from __future__ import annotations

import json
import subprocess
import tomllib
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from software_agent_team.releases import (
    DEFAULT_REPOSITORY_URL,
    ReleaseManifest,
    git_archive_digest,
)
from software_agent_team.schema_compatibility import supported_schemas
from software_agent_team.versioning import (
    compare_release_versions,
    highest_release_version,
    parse_release_version,
)

CHANGE_IMPACT_SCHEMA_VERSION = 1
DEFAULT_CHANGE_IMPACT_PATH = Path("release/change-impact.json")


class ReleaseGateError(RuntimeError):
    """Raised when a commit cannot be identified as a valid release candidate."""


class ChangeImpact(StrEnum):
    """Minimum user-visible SemVer effect of one release change."""

    PATCH = "patch"
    MINOR = "minor"
    MAJOR = "major"


class ReleaseChange(BaseModel):
    """One machine-readable change included in a release candidate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,63}$")
    impact: ChangeImpact
    summary: str = Field(min_length=10, max_length=300)

    @field_validator("summary")
    @classmethod
    def require_clean_summary(cls, value: str) -> str:
        if value != value.strip() or "\n" in value or "\r" in value:
            raise ValueError("release change summary must be one clean line")
        return value


class ReleaseChangeSet(BaseModel):
    """Complete change classification since the prior stable release."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[CHANGE_IMPACT_SCHEMA_VERSION] = CHANGE_IMPACT_SCHEMA_VERSION
    baseline_version: str | None
    target_version: str
    changes: tuple[ReleaseChange, ...] = Field(min_length=1)

    @field_validator("baseline_version", "target_version")
    @classmethod
    def validate_versions(cls, value: str | None) -> str | None:
        if value is not None:
            parse_release_version(value)
        return value

    @model_validator(mode="after")
    def require_unique_changes_and_sufficient_increment(self) -> ReleaseChangeSet:
        identifiers = tuple(change.id for change in self.changes)
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("release change IDs must be unique")
        minimum = minimum_target_version(
            self.baseline_version,
            max((change.impact for change in self.changes), key=_impact_rank),
        )
        if compare_release_versions(self.target_version, minimum) < 0:
            raise ValueError(
                f"target version {self.target_version} is below required {minimum}"
            )
        return self


def minimum_target_version(
    baseline: str | None,
    impact: ChangeImpact,
) -> str:
    """Calculate the minimum release version required by one impact class."""

    if baseline is None:
        return "0.1.0"
    major, minor, patch = parse_release_version(baseline)
    if impact is ChangeImpact.PATCH:
        return f"{major}.{minor}.{patch + 1}"
    if impact is ChangeImpact.MINOR or major == 0:
        return f"{major}.{minor + 1}.0"
    return f"{major + 1}.0.0"


def load_release_change_set(path: Path) -> ReleaseChangeSet:
    """Load the checked-in release impact ledger."""

    if path.is_symlink() or not path.is_file():
        raise ReleaseGateError(f"release change-impact file is invalid: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return ReleaseChangeSet.model_validate(payload)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        raise ReleaseGateError(
            f"release change-impact file is invalid: {path}"
        ) from error


def build_release_manifest(
    *,
    repository: Path,
    tag: str,
    repository_url: str = DEFAULT_REPOSITORY_URL,
    require_tag: bool,
    created_at: datetime | None = None,
) -> ReleaseManifest:
    """Validate an exact candidate and construct its publishable manifest."""

    root = repository.resolve()
    if not tag.startswith("v"):
        raise ReleaseGateError("release tag must be vMAJOR.MINOR.PATCH")
    version = tag[1:]
    try:
        parse_release_version(version)
    except ValueError as error:
        raise ReleaseGateError("release tag must be vMAJOR.MINOR.PATCH") from error
    if _git(root, "status", "--porcelain", "--untracked-files=all"):
        raise ReleaseGateError("release candidate working tree must be clean")
    revision = _git(root, "rev-parse", "HEAD")
    if require_tag:
        try:
            tagged_revision = _git(root, "rev-parse", f"refs/tags/{tag}^{{commit}}")
        except ReleaseGateError as error:
            raise ReleaseGateError(f"release tag does not exist: {tag}") from error
        if tagged_revision != revision:
            raise ReleaseGateError(
                "release tag does not point to the checked-out commit"
            )
    elif _git_tag_exists(root, tag):
        tagged_revision = _git(root, "rev-parse", f"refs/tags/{tag}^{{commit}}")
        if tagged_revision != revision:
            raise ReleaseGateError("candidate version tag is already bound elsewhere")
    package_version = _project_version(root)
    if package_version != version:
        raise ReleaseGateError("package version does not match the release tag")
    if _project_lock_version(root) != package_version:
        raise ReleaseGateError("locked project version does not match package metadata")
    change_set = load_release_change_set(root / DEFAULT_CHANGE_IMPACT_PATH)
    if change_set.target_version != version:
        raise ReleaseGateError("change-impact target does not match the release tag")
    prior_version = _prior_release_version(root, current_version=version)
    if change_set.baseline_version != prior_version:
        expected = prior_version or "null for the first release"
        raise ReleaseGateError(
            f"change-impact baseline does not match prior release: expected {expected}"
        )
    return ReleaseManifest(
        release_version=version,
        source_revision=revision,
        source_ref=tag,
        repository_url=repository_url,
        artifact_digest=git_archive_digest(root),
        schema_support=supported_schemas(),
        created_at=created_at or datetime.now(UTC),
    )


def _project_version(repository: Path) -> str:
    try:
        payload = tomllib.loads(
            (repository / "pyproject.toml").read_text(encoding="utf-8")
        )
        value = payload["project"]["version"]
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as error:
        raise ReleaseGateError("package version is unavailable") from error
    if not isinstance(value, str):
        raise ReleaseGateError("package version is invalid")
    return value


def _project_lock_version(repository: Path) -> str:
    try:
        payload = tomllib.loads((repository / "uv.lock").read_text(encoding="utf-8"))
        packages = payload["package"]
        matches = [
            package["version"]
            for package in packages
            if package.get("name") == "software-agent-team"
        ]
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as error:
        raise ReleaseGateError("locked project version is unavailable") from error
    if len(matches) != 1 or not isinstance(matches[0], str):
        raise ReleaseGateError("locked project version is invalid")
    return matches[0]


def _prior_release_version(
    repository: Path,
    *,
    current_version: str,
) -> str | None:
    tags = _git(repository, "tag", "--list", "v*.*.*").splitlines()
    versions: list[str] = []
    for tag in tags:
        candidate = tag.removeprefix("v")
        if candidate == current_version:
            continue
        try:
            parse_release_version(candidate)
        except ValueError:
            continue
        versions.append(candidate)
    return highest_release_version(versions) if versions else None


def _git(repository: Path, *arguments: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise ReleaseGateError(
            f"Git release check failed: {' '.join(arguments)}"
        ) from error
    return completed.stdout.strip()


def _git_tag_exists(repository: Path, tag: str) -> bool:
    try:
        _git(repository, "show-ref", "--verify", "--quiet", f"refs/tags/{tag}")
    except ReleaseGateError:
        return False
    return True


def _impact_rank(impact: ChangeImpact) -> int:
    return {
        ChangeImpact.PATCH: 1,
        ChangeImpact.MINOR: 2,
        ChangeImpact.MAJOR: 3,
    }[impact]
