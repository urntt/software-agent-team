"""Authoritative SAT release, source, and installation identity."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tomllib
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as distribution_version
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from software_agent_team.schema_compatibility import SchemaSupport

DISTRIBUTION_NAME = "software-agent-team"
INSTALLATION_RECORD_SCHEMA_VERSION = 1
INSTALLATION_RECORD_ENVIRONMENT_VARIABLE = "SAT_INSTALL_METADATA_PATH"
_RELEASE_VERSION_PATTERN = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
)
_SOURCE_REVISION_PATTERN = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_ARTIFACT_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


class VersionIdentityError(ValueError):
    """Raised when SAT version or installation identity is unsafe or invalid."""


class ManagedChannel(StrEnum):
    """Managed application channels supported by the product."""

    STABLE = "stable"
    DEV = "dev"


class InstallMode(StrEnum):
    """How the current SAT executable is controlled."""

    MANAGED = "managed"
    SOURCE = "source"
    PACKAGE = "package"


class IdentityStatus(StrEnum):
    """Whether release and provenance facts form one reproducible identity."""

    VERIFIED = "verified"
    PARTIAL = "partial"
    INCONSISTENT = "inconsistent"


def parse_release_version(value: str) -> tuple[int, int, int]:
    """Parse the stable SemVer subset used for SAT release ordering."""

    match = _RELEASE_VERSION_PATTERN.fullmatch(value)
    if match is None:
        raise VersionIdentityError(
            "release version must be MAJOR.MINOR.PATCH without prerelease or "
            "build metadata"
        )
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def compare_release_versions(left: str, right: str) -> int:
    """Return the ordering of two stable SAT release versions."""

    left_parts = parse_release_version(left)
    right_parts = parse_release_version(right)
    return (left_parts > right_parts) - (left_parts < right_parts)


class InstallationRecord(BaseModel):
    """Write-once identity selected by one successful managed activation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[INSTALLATION_RECORD_SCHEMA_VERSION] = (
        INSTALLATION_RECORD_SCHEMA_VERSION
    )
    install_mode: Literal[InstallMode.MANAGED] = InstallMode.MANAGED
    channel: ManagedChannel
    release_version: str
    source_revision: str
    source_ref: str = Field(min_length=1, max_length=200)
    repository_url: str = Field(min_length=1, max_length=2_000)
    application_path: str = Field(min_length=1, max_length=4_096)
    artifact_digest: str | None = None
    installed_at: datetime

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
    def require_clean_text(cls, value: str) -> str:
        if value != value.strip() or any(character.isspace() for character in value):
            raise ValueError("installation source values must not contain whitespace")
        return value

    @field_validator("artifact_digest")
    @classmethod
    def validate_artifact_digest(cls, value: str | None) -> str | None:
        if value is not None and _ARTIFACT_DIGEST_PATTERN.fullmatch(value) is None:
            raise ValueError("artifact digest must be a sha256 digest")
        return value

    @field_validator("application_path")
    @classmethod
    def require_absolute_application_path(cls, value: str) -> str:
        path = Path(value)
        if not path.is_absolute() or path == Path(path.anchor):
            raise ValueError("application path must be a specific absolute path")
        return value

    @field_validator("installed_at")
    @classmethod
    def require_aware_install_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("installed_at must include a UTC offset")
        return value

    @model_validator(mode="after")
    def validate_channel_ref(self) -> InstallationRecord:
        if self.channel is ManagedChannel.STABLE:
            expected = f"v{self.release_version}"
            if self.source_ref != expected:
                raise ValueError(f"stable source_ref must be {expected}")
        return self


class SoftwareVersionReport(BaseModel):
    """Machine-readable installed software identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    release_version: str
    display_version: str
    source_revision: str | None
    dirty: bool | None
    install_mode: InstallMode
    channel: ManagedChannel | None
    source_ref: str | None
    repository_url: str | None
    application_path: str
    artifact_digest: str | None
    installed_at: datetime | None
    identity_status: IdentityStatus
    provenance_source: Literal["installation_record", "git", "unavailable"]
    schema_support: tuple[SchemaSupport, ...]
    problems: tuple[str, ...] = ()

    @field_validator("release_version")
    @classmethod
    def validate_release_version(cls, value: str) -> str:
        parse_release_version(value)
        return value


def installation_record_path(
    environment: Mapping[str, str] | None = None,
) -> Path:
    """Resolve the user-local managed installation record path."""

    values = os.environ if environment is None else environment
    override = values.get(INSTALLATION_RECORD_ENVIRONMENT_VARIABLE)
    if override is not None:
        if not override.strip() or override != override.strip():
            raise VersionIdentityError(
                f"{INSTALLATION_RECORD_ENVIRONMENT_VARIABLE} must be a clean path"
            )
        path = Path(override).expanduser()
        if not path.is_absolute():
            raise VersionIdentityError(
                f"{INSTALLATION_RECORD_ENVIRONMENT_VARIABLE} must be absolute"
            )
        return path

    data_root = values.get("XDG_DATA_HOME")
    if data_root:
        root = Path(data_root).expanduser()
    else:
        home = values.get("HOME")
        root = (Path(home).expanduser() if home else Path.home()) / ".local" / "share"
    if not root.is_absolute():
        raise VersionIdentityError("the installation data root must be absolute")
    return root / "software-agent-team" / "installation.json"


def load_installation_record(path: Path | None = None) -> InstallationRecord | None:
    """Load a managed installation record without following a record symlink."""

    source = installation_record_path() if path is None else path
    if source.is_symlink():
        raise VersionIdentityError(
            f"installation record must not be a symbolic link: {source}"
        )
    if not source.exists():
        return None
    if not source.is_file():
        raise VersionIdentityError(
            f"installation record must be a regular file: {source}"
        )
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise VersionIdentityError(
            f"cannot read installation record: {source}"
        ) from error
    return InstallationRecord.model_validate(payload)


def save_installation_record(
    record: InstallationRecord,
    path: Path | None = None,
) -> Path:
    """Atomically persist one user-local managed installation identity."""

    destination = installation_record_path() if path is None else path
    if not destination.is_absolute():
        raise VersionIdentityError("installation record path must be absolute")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if destination.is_symlink():
        raise VersionIdentityError(
            f"installation record must not be a symbolic link: {destination}"
        )
    if destination.exists() and not destination.is_file():
        raise VersionIdentityError(
            f"installation record must be a regular file: {destination}"
        )
    content = (
        json.dumps(record.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n"
    ).encode()
    temporary = destination.parent / f".{destination.name}.{uuid4().hex}.tmp"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
        destination.chmod(0o600)
        _fsync_directory(destination.parent)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def inspect_software_version(
    *,
    project_root: Path,
    environment: Mapping[str, str] | None = None,
    installed_version: str | None = None,
) -> SoftwareVersionReport:
    """Inspect package identity, managed provenance, or a source checkout."""

    from software_agent_team.schema_compatibility import supported_schemas

    release_version = installed_version or _installed_release_version(project_root)
    parse_release_version(release_version)
    root = project_root.resolve()
    record = load_installation_record(
        installation_record_path(environment) if environment is not None else None
    )
    problems: list[str] = []
    if record is not None and Path(record.application_path).resolve() == root:
        if record.release_version != release_version:
            problems.append(
                "installation record release version does not match package metadata"
            )
        status = IdentityStatus.INCONSISTENT if problems else IdentityStatus.VERIFIED
        return SoftwareVersionReport(
            release_version=release_version,
            display_version=_display_version(release_version, record.source_revision),
            source_revision=record.source_revision,
            dirty=False,
            install_mode=InstallMode.MANAGED,
            channel=record.channel,
            source_ref=record.source_ref,
            repository_url=record.repository_url,
            application_path=str(root),
            artifact_digest=record.artifact_digest,
            installed_at=record.installed_at,
            identity_status=status,
            provenance_source="installation_record",
            schema_support=supported_schemas(),
            problems=tuple(problems),
        )

    git_identity = _inspect_git_identity(root)
    if git_identity is not None:
        revision, dirty = git_identity
        return SoftwareVersionReport(
            release_version=release_version,
            display_version=_display_version(release_version, revision),
            source_revision=revision,
            dirty=dirty,
            install_mode=InstallMode.SOURCE,
            channel=None,
            source_ref=None,
            repository_url=None,
            application_path=str(root),
            artifact_digest=None,
            installed_at=None,
            identity_status=(
                IdentityStatus.PARTIAL if dirty else IdentityStatus.VERIFIED
            ),
            provenance_source="git",
            schema_support=supported_schemas(),
            problems=("source checkout has uncommitted changes",) if dirty else (),
        )

    return SoftwareVersionReport(
        release_version=release_version,
        display_version=release_version,
        source_revision=None,
        dirty=None,
        install_mode=InstallMode.PACKAGE,
        channel=None,
        source_ref=None,
        repository_url=None,
        application_path=str(root),
        artifact_digest=None,
        installed_at=None,
        identity_status=IdentityStatus.PARTIAL,
        provenance_source="unavailable",
        schema_support=supported_schemas(),
        problems=("source provenance is unavailable",),
    )


def render_short_version(report: SoftwareVersionReport) -> str:
    """Render the non-networked one-line ``sat --version`` response."""

    suffix = " (dirty)" if report.dirty else ""
    channel = f" [{report.channel.value}]" if report.channel is not None else ""
    return f"sat {report.display_version}{suffix}{channel}"


def render_version_report(report: SoftwareVersionReport) -> str:
    """Render a readable detailed version report."""

    lines = [
        f"release: {report.release_version}",
        f"display: {report.display_version}",
        f"revision: {report.source_revision or 'unavailable'}",
        f"dirty: {_format_optional_bool(report.dirty)}",
        f"install mode: {report.install_mode.value}",
        f"channel: {report.channel.value if report.channel is not None else 'none'}",
        f"source ref: {report.source_ref or 'none'}",
        f"artifact: {report.artifact_digest or 'unavailable'}",
        f"identity: {report.identity_status.value}",
        f"provenance: {report.provenance_source}",
    ]
    lines.extend(
        "schema: "
        f"{support.family.value} current={support.current} "
        f"readable={support.minimum_readable}..{support.maximum_readable}"
        for support in report.schema_support
    )
    lines.extend(f"problem: {problem}" for problem in report.problems)
    return "\n".join(lines)


def make_installation_record(
    *,
    channel: ManagedChannel,
    release_version: str,
    source_revision: str,
    source_ref: str,
    repository_url: str,
    application_path: Path,
    artifact_digest: str | None,
    installed_at: datetime | None = None,
) -> InstallationRecord:
    """Construct an activation record with one injectable timestamp."""

    return InstallationRecord(
        channel=channel,
        release_version=release_version,
        source_revision=source_revision,
        source_ref=source_ref,
        repository_url=repository_url,
        application_path=str(application_path.resolve()),
        artifact_digest=artifact_digest,
        installed_at=installed_at or datetime.now(UTC),
    )


def _installed_release_version(project_root: Path) -> str:
    try:
        return distribution_version(DISTRIBUTION_NAME)
    except PackageNotFoundError:
        pyproject = project_root / "pyproject.toml"
        try:
            payload = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            value = payload["project"]["version"]
        except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as error:
            raise VersionIdentityError(
                "SAT release version is unavailable from package metadata"
            ) from error
        if not isinstance(value, str):
            raise VersionIdentityError(
                "project release version must be a string"
            ) from None
        return value


def _inspect_git_identity(project_root: Path) -> tuple[str, bool] | None:
    try:
        top_level = _run_git(project_root, "rev-parse", "--show-toplevel")
        if Path(top_level).resolve() != project_root:
            return None
        revision = _run_git(project_root, "rev-parse", "HEAD")
        if _SOURCE_REVISION_PATTERN.fullmatch(revision) is None:
            raise VersionIdentityError("Git returned an invalid source revision")
        dirty = bool(
            _run_git(project_root, "status", "--porcelain", "--untracked-files=all")
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return revision, dirty


def _run_git(project_root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(project_root), *arguments],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return completed.stdout.strip()


def _display_version(release: str, revision: str) -> str:
    return f"{release}+g{revision[:12]}"


def _format_optional_bool(value: bool | None) -> str:
    if value is None:
        return "unknown"
    return "yes" if value else "no"


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def highest_release_version(values: Sequence[str]) -> str:
    """Select the highest stable release from a non-empty sequence."""

    if not values:
        raise VersionIdentityError("at least one release version is required")
    for value in values:
        parse_release_version(value)
    return max(values, key=parse_release_version)
