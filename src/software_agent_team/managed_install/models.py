"""Data models, path schemas, and validation envelopes for managed installations.

Provides immutable schemas for markers, release targets, execution paths,
and Docker image transition records.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from software_agent_team.docker_engine import DockerEngineIdentity
from software_agent_team.managed_install.constants import (
    _SOURCE_REF_PATTERN,
    _SOURCE_REVISION_PATTERN,
    BIN_DIRECTORY_ENVIRONMENT_VARIABLE,
    DEFAULT_MANAGED_DIRECTORY_NAME,
    INSTALL_ROOT_ENVIRONMENT_VARIABLE,
    MANAGED_MARKER_SCHEMA_VERSION,
    MANAGED_ROOT_MARKER_SCHEMA_VERSION,
    SANDBOX_IMAGE_ROLLBACK_REPOSITORY,
    SANDBOX_IMAGE_TRANSITION_SCHEMA_VERSION,
    ManagedInstallError,
    _is_relative_to,
    _is_sha256_digest,
)
from software_agent_team.paths import user_state_root
from software_agent_team.schema_compatibility import SchemaSupport
from software_agent_team.user_configuration import user_configuration_path
from software_agent_team.versioning import (
    ManagedChannel,
    installation_record_path,
    parse_release_version,
)


def _absolute_override(value: str, variable: str) -> Path:
    """Validate and expand an absolute environmental path override."""
    if (
        not value
        or value != value.strip()
        or any(ord(character) < 32 for character in value)
    ):
        raise ManagedInstallError(f"{variable} must be a clean path")
    path = Path(value).expanduser()
    if not path.is_absolute() or path == Path(path.anchor):
        raise ManagedInstallError(f"{variable} must be a specific absolute path")
    return Path(os.path.normpath(path))


class ManagedApplicationMarker(BaseModel):
    """Release-local ownership and intended activation identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[MANAGED_MARKER_SCHEMA_VERSION] = (
        MANAGED_MARKER_SCHEMA_VERSION
    )
    application_link: str
    channel: ManagedChannel
    release_version: str
    source_revision: str
    source_ref: str
    repository_url: str
    artifact_digest: str | None

    @field_validator("application_link")
    @classmethod
    def require_absolute_application_link(cls, value: str) -> str:
        path = Path(value)
        if not path.is_absolute() or path == Path(path.anchor):
            raise ValueError("application link must be a specific absolute path")
        return value

    @field_validator("release_version")
    @classmethod
    def validate_release_version(cls, value: str) -> str:
        parse_release_version(value)
        return value

    @field_validator("source_revision")
    @classmethod
    def validate_source_revision(cls, value: str) -> str:
        if _SOURCE_REVISION_PATTERN.fullmatch(value) is None:
            raise ValueError("source revision must be a full lowercase Git object ID")
        return value

    @field_validator("source_ref", "repository_url")
    @classmethod
    def require_clean_source(cls, value: str) -> str:
        if (
            not value
            or value != value.strip()
            or any(character.isspace() for character in value)
        ):
            raise ValueError("managed source values must not contain whitespace")
        return value

    @field_validator("artifact_digest")
    @classmethod
    def validate_artifact_digest(cls, value: str | None) -> str | None:
        if value is not None and not _is_sha256_digest(value):
            raise ValueError("managed artifact digest must be a sha256 digest")
        return value

    @model_validator(mode="after")
    def require_channel_identity(self) -> ManagedApplicationMarker:
        if self.channel is ManagedChannel.STABLE:
            if self.source_ref != f"v{self.release_version}":
                raise ValueError("stable marker ref must match its release version")
            if self.artifact_digest is None:
                raise ValueError("stable marker requires an artifact digest")
        return self


class ManagedRootMarker(BaseModel):
    """Ownership boundary for paths changed by the managed lifecycle."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[MANAGED_ROOT_MARKER_SCHEMA_VERSION] = (
        MANAGED_ROOT_MARKER_SCHEMA_VERSION
    )
    managed_root: str
    application_link: str
    versions_root: str
    installation_record: str
    bin_directory: str

    @field_validator(
        "managed_root",
        "application_link",
        "versions_root",
        "installation_record",
        "bin_directory",
    )
    @classmethod
    def require_safe_absolute_path(cls, value: str) -> str:
        if value != value.strip() or any(ord(character) < 32 for character in value):
            raise ValueError("managed lifecycle paths must not contain control text")
        path = Path(value)
        if not path.is_absolute() or path == Path(path.anchor):
            raise ValueError("managed lifecycle paths must be specific and absolute")
        if os.path.normpath(value) != value:
            raise ValueError("managed lifecycle paths must be normalized")
        return value

    @model_validator(mode="after")
    def require_one_managed_layout(self) -> ManagedRootMarker:
        root = Path(self.managed_root)
        versions = Path(self.versions_root)
        application = Path(self.application_link)
        record = Path(self.installation_record)
        if versions != root / "versions":
            raise ValueError("versions root must belong to the managed root")
        if application == root or _is_relative_to(application, versions):
            raise ValueError("application link must remain outside release storage")
        if record == root or _is_relative_to(record, versions):
            raise ValueError("installation record must remain outside release storage")
        return self


class ManagedTarget(BaseModel):
    """Immutable source target shared by bootstrap, update, and channel switch."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    channel: ManagedChannel
    release_version: str | None
    source_revision: str
    source_ref: str
    repository_url: str
    artifact_digest: str | None
    schema_support: tuple[SchemaSupport, ...] | None = None

    @field_validator("release_version")
    @classmethod
    def validate_release_version(cls, value: str | None) -> str | None:
        if value is not None:
            parse_release_version(value)
        return value

    @field_validator("source_revision")
    @classmethod
    def validate_source_revision(cls, value: str) -> str:
        if _SOURCE_REVISION_PATTERN.fullmatch(value) is None:
            raise ValueError("source revision must be a full lowercase Git object ID")
        return value

    @field_validator("source_ref")
    @classmethod
    def validate_source_ref(cls, value: str) -> str:
        if _SOURCE_REF_PATTERN.fullmatch(value) is None or value.startswith("-"):
            raise ValueError("managed source ref is invalid")
        return value

    @field_validator("repository_url")
    @classmethod
    def validate_repository_url(cls, value: str) -> str:
        if (
            not value
            or value != value.strip()
            or value.startswith("-")
            or any(character.isspace() for character in value)
        ):
            raise ValueError("managed repository URL is invalid")
        return value

    @field_validator("artifact_digest")
    @classmethod
    def validate_artifact_digest(cls, value: str | None) -> str | None:
        if value is not None and not _is_sha256_digest(value):
            raise ValueError("managed artifact digest must be a sha256 digest")
        return value

    @model_validator(mode="after")
    def require_stable_identity(self) -> ManagedTarget:
        if self.channel is ManagedChannel.STABLE:
            if self.release_version is None:
                raise ValueError("stable target requires a release version")
            if self.source_ref != f"v{self.release_version}":
                raise ValueError("stable target ref must match its release version")
            if self.artifact_digest is None:
                raise ValueError("stable target requires an artifact digest")
            if self.schema_support is None:
                raise ValueError("stable target requires schema compatibility metadata")
        return self


def _expected_root_marker(paths: ManagedInstallPaths) -> ManagedRootMarker:
    """Validate that managed paths form a consistent ManagedRootMarker."""
    try:
        expected = ManagedRootMarker(
            managed_root=str(paths.managed_root),
            application_link=str(paths.application_link),
            versions_root=str(paths.versions_root),
            installation_record=str(paths.installation_record),
            bin_directory=str(paths.bin_directory),
        )
    except ValueError as error:
        raise ManagedInstallError("managed lifecycle paths are invalid") from error
    if paths.lock != paths.managed_root / "update.lock":
        raise ManagedInstallError("managed update lock belongs to a different root")
    return expected


@dataclass(frozen=True)
class ManagedInstallPaths:
    """User-local paths changed by one managed application transaction."""

    managed_root: Path
    application_link: Path
    versions_root: Path
    installation_record: Path
    lock: Path
    bin_directory: Path
    state_root: Path
    configuration_path: Path

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> ManagedInstallPaths:
        values = os.environ if environment is None else environment
        application_override = values.get(INSTALL_ROOT_ENVIRONMENT_VARIABLE)
        if application_override is not None:
            application_link = _absolute_override(
                application_override,
                INSTALL_ROOT_ENVIRONMENT_VARIABLE,
            )
            managed_root = application_link.parent / (
                f".{application_link.name}.sat-managed"
            )
        else:
            data_root = values.get("XDG_DATA_HOME")
            if data_root:
                root = Path(data_root).expanduser()
            else:
                home = values.get("HOME")
                root = (
                    (Path(home).expanduser() if home else Path.home())
                    / ".local"
                    / "share"
                )
            if not root.is_absolute():
                raise ManagedInstallError("managed data root must be absolute")
            managed_root = root / DEFAULT_MANAGED_DIRECTORY_NAME
            application_link = managed_root / "app"
        bin_override = values.get(BIN_DIRECTORY_ENVIRONMENT_VARIABLE)
        if bin_override is not None:
            bin_directory = _absolute_override(
                bin_override,
                BIN_DIRECTORY_ENVIRONMENT_VARIABLE,
            )
        else:
            home = values.get("HOME")
            base = Path(home).expanduser() if home else Path.home()
            bin_directory = base / ".local" / "bin"
        state = user_state_root(values)
        paths = cls(
            managed_root=managed_root,
            application_link=application_link,
            versions_root=managed_root / "versions",
            installation_record=installation_record_path(values),
            lock=managed_root / "update.lock",
            bin_directory=bin_directory,
            state_root=state,
            configuration_path=user_configuration_path(values),
        )
        _expected_root_marker(paths)
        return paths


@dataclass(frozen=True)
class SandboxImageIdentity:
    """One locally inspected image and its deletion-safety facts."""

    image_id: str
    parent_image_id: str | None
    repository_tags: tuple[str, ...]
    owned: bool
    reference_labeled: bool = False


@dataclass(frozen=True)
class SandboxImageTransition:
    """Exact mutable-tag change made while staging one managed candidate."""

    reference: str
    previous: SandboxImageIdentity | None
    previous_lineage: tuple[SandboxImageIdentity, ...]
    candidate: SandboxImageIdentity
    candidate_lineage: tuple[SandboxImageIdentity, ...]
    rollback_reference: str | None = None


@dataclass(frozen=True)
class StagedApplication:
    """Install-verified release that has not yet changed the active link."""

    path: Path
    marker: ManagedApplicationMarker
    schema_support: tuple[SchemaSupport, ...]
    created_candidate: bool
    sandbox_image_transition: SandboxImageTransition | None = None
    docker_engine: DockerEngineIdentity | None = None


class PersistedSandboxImageIdentity(BaseModel):
    """Immutable Docker identity captured before a managed tag transition."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    image_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    parent_image_id: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    repository_tags: tuple[str, ...] = Field(default=(), max_length=100)
    owned: bool
    reference_labeled: bool

    @field_validator("repository_tags")
    @classmethod
    def require_safe_unique_tags(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("sandbox image repository tags must be unique")
        if any(
            not value
            or len(value) > 4_096
            or value != value.strip()
            or any(ord(character) < 32 for character in value)
            for value in values
        ):
            raise ValueError("sandbox image repository tags must be bounded text")
        return values

    @classmethod
    def from_runtime(
        cls,
        identity: SandboxImageIdentity,
    ) -> PersistedSandboxImageIdentity:
        return cls(
            image_id=identity.image_id,
            parent_image_id=identity.parent_image_id,
            repository_tags=identity.repository_tags,
            owned=identity.owned,
            reference_labeled=identity.reference_labeled,
        )

    def to_runtime(self) -> SandboxImageIdentity:
        return SandboxImageIdentity(
            image_id=self.image_id,
            parent_image_id=self.parent_image_id,
            repository_tags=self.repository_tags,
            owned=self.owned,
            reference_labeled=self.reference_labeled,
        )


def _require_persisted_lineage(
    root: PersistedSandboxImageIdentity | None,
    lineage: tuple[PersistedSandboxImageIdentity, ...],
    label: str,
) -> None:
    """Validate that a persistent sandbox image lineage forms an unbroken chain."""
    if root is None:
        if lineage:
            raise ValueError(f"{label} sandbox lineage has no root")
        return
    if not lineage or lineage[0] != root:
        raise ValueError(f"{label} sandbox lineage root is invalid")
    ids = tuple(identity.image_id for identity in lineage)
    if len(ids) != len(set(ids)):
        raise ValueError(f"{label} sandbox lineage contains a cycle")
    for child, parent in pairwise(lineage):
        if child.parent_image_id != parent.image_id:
            raise ValueError(f"{label} sandbox lineage is discontinuous")


class PersistedSandboxImageLineage(BaseModel):
    """One exact legacy root and its locally available parent chain."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    root: PersistedSandboxImageIdentity
    lineage: tuple[PersistedSandboxImageIdentity, ...] = Field(max_length=512)

    @model_validator(mode="after")
    def require_contiguous_lineage(self) -> PersistedSandboxImageLineage:
        _require_persisted_lineage(self.root, self.lineage, "legacy")
        if not self.root.reference_labeled:
            raise ValueError("legacy sandbox root must carry the SAT reference label")
        return self


class PersistedSandboxImageTransition(BaseModel):
    """Target-authored image handoff for cleanup after an older updater exits."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[SANDBOX_IMAGE_TRANSITION_SCHEMA_VERSION] = (
        SANDBOX_IMAGE_TRANSITION_SCHEMA_VERSION
    )
    target_source_revision: str = Field(pattern=r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
    previous_reference: str | None = Field(default=None, min_length=1, max_length=4_096)
    rollback_reference: str | None = None
    reference: str = Field(min_length=1, max_length=4_096)
    previous: PersistedSandboxImageIdentity | None = None
    previous_lineage: tuple[PersistedSandboxImageIdentity, ...] = Field(
        default=(),
        max_length=512,
    )
    candidate: PersistedSandboxImageIdentity | None = None
    candidate_lineage: tuple[PersistedSandboxImageIdentity, ...] = Field(
        default=(),
        max_length=512,
    )
    legacy_orphan_lineages: tuple[PersistedSandboxImageLineage, ...] = Field(
        default=(),
        max_length=64,
    )

    @field_validator("previous_reference", "reference")
    @classmethod
    def require_safe_reference(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if (
            value != value.strip()
            or value.startswith("-")
            or any(character.isspace() or ord(character) < 32 for character in value)
        ):
            raise ValueError("sandbox image reference is invalid")
        return value

    @property
    def resolved_previous_reference(self) -> str:
        """Read legacy handoffs as same-reference transitions."""
        return self.previous_reference or self.reference

    @field_validator("rollback_reference")
    @classmethod
    def require_owned_rollback_reference(cls, value: str | None) -> str | None:
        if (
            value is not None
            and re.fullmatch(
                re.escape(SANDBOX_IMAGE_ROLLBACK_REPOSITORY)
                + rf":u{os.geteuid()}-[0-9a-f]{{32}}",
                value,
            )
            is None
        ):
            raise ValueError("sandbox rollback reference is invalid")
        return value

    @model_validator(mode="after")
    def require_complete_captured_lineages(self) -> PersistedSandboxImageTransition:
        _require_persisted_lineage(self.previous, self.previous_lineage, "previous")
        _require_persisted_lineage(self.candidate, self.candidate_lineage, "candidate")
        if self.candidate is not None and not self.candidate.owned:
            raise ValueError("candidate sandbox image must be attributable to SAT")
        if self.rollback_reference is not None and self.previous is None:
            raise ValueError("sandbox rollback reference requires a previous image")
        roots = tuple(item.root.image_id for item in self.legacy_orphan_lineages)
        if len(roots) != len(set(roots)):
            raise ValueError("legacy sandbox roots must be unique")
        return self
