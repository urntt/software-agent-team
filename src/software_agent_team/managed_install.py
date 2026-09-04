"""Staged, verified, and rollback-safe managed application activation."""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import subprocess
import tempfile
import tomllib
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from software_agent_team.paths import user_state_root
from software_agent_team.releases import (
    ResolvedReleaseTarget,
    git_archive_digest,
)
from software_agent_team.schema_compatibility import (
    SchemaSupport,
    inspect_persisted_schema_compatibility,
)
from software_agent_team.user_configuration import user_configuration_path
from software_agent_team.versioning import (
    InstallationRecord,
    ManagedChannel,
    installation_record_path,
    load_installation_record,
    make_installation_record,
    parse_release_version,
    save_installation_record,
)

MANAGED_MARKER_NAME = ".sat-managed-install"
MANAGED_MARKER_SCHEMA_VERSION = 2
MANAGED_ROOT_MARKER_NAME = ".sat-managed-root"
MANAGED_ROOT_MARKER_SCHEMA_VERSION = 1
INSTALL_ROOT_ENVIRONMENT_VARIABLE = "SAT_INSTALL_ROOT"
BIN_DIRECTORY_ENVIRONMENT_VARIABLE = "SAT_BIN_DIR"
LATEST_RELEASE_API_ENVIRONMENT_VARIABLE = "SAT_RELEASE_API_URL"
DEFAULT_MANAGED_DIRECTORY_NAME = "software-agent-team"
_SOURCE_REF_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_SOURCE_REVISION_PATTERN = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


class ManagedInstallError(RuntimeError):
    """Raised when a managed install cannot preserve its safety contract."""


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
class StagedApplication:
    """Install-verified release that has not yet changed the active link."""

    path: Path
    marker: ManagedApplicationMarker
    schema_support: tuple[SchemaSupport, ...]


CommandRunner = Callable[[Sequence[str], Path | None, Mapping[str, str] | None], None]


def target_from_stable_release(target: ResolvedReleaseTarget) -> ManagedTarget:
    """Project a verified stable manifest into the shared transaction target."""

    manifest = target.manifest
    return ManagedTarget(
        channel=ManagedChannel.STABLE,
        release_version=manifest.release_version,
        source_revision=manifest.source_revision,
        source_ref=manifest.source_ref,
        repository_url=manifest.repository_url,
        artifact_digest=manifest.artifact_digest,
        schema_support=manifest.schema_support,
    )


def resolve_dev_target(
    *,
    repository_url: str,
    source_ref: str = "main",
    command_output: Callable[[Sequence[str]], str] | None = None,
) -> ManagedTarget:
    """Resolve an explicit dev ref to one advertised full revision."""

    if _SOURCE_REF_PATTERN.fullmatch(source_ref) is None or source_ref.startswith("-"):
        raise ManagedInstallError("dev source ref is invalid")
    run = command_output or _capture_command
    revision = source_ref if _SOURCE_REVISION_PATTERN.fullmatch(source_ref) else None
    if revision is None:
        output = run(("git", "ls-remote", "--refs", repository_url, source_ref))
        matches = []
        for line in output.splitlines():
            fields = line.split("\t")
            if len(fields) == 2 and fields[1] in {
                source_ref,
                f"refs/heads/{source_ref}",
                f"refs/tags/{source_ref}",
            }:
                matches.append(fields[0])
        matches = list(dict.fromkeys(matches))
        if len(matches) != 1 or _SOURCE_REVISION_PATTERN.fullmatch(matches[0]) is None:
            raise ManagedInstallError("dev source ref did not resolve unambiguously")
        revision = matches[0]
    return ManagedTarget(
        channel=ManagedChannel.DEV,
        release_version=None,
        source_revision=revision,
        source_ref=source_ref,
        repository_url=repository_url,
        artifact_digest=None,
        schema_support=None,
    )


def stage_managed_target(
    target: ManagedTarget,
    paths: ManagedInstallPaths,
    *,
    environment: Mapping[str, str] | None = None,
    command_runner: CommandRunner | None = None,
) -> StagedApplication:
    """Clone and fully install one target without changing active launchers."""

    _ensure_managed_root(paths)
    _validate_managed_destination(paths)
    paths.versions_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    _require_real_directory(paths.versions_root, "managed versions root")
    stage = Path(tempfile.mkdtemp(prefix=".stage-", dir=paths.versions_root)).resolve()
    runner = command_runner or _run_command
    try:
        runner(("git", "init", "-b", "sat-managed", str(stage)), None, None)
        runner(
            ("git", "-C", str(stage), "remote", "add", "origin", target.repository_url),
            None,
            None,
        )
        runner(
            (
                "git",
                "-C",
                str(stage),
                "fetch",
                "--depth",
                "1",
                "origin",
                target.source_ref,
            ),
            None,
            None,
        )
        runner(
            ("git", "-C", str(stage), "checkout", "--detach", "--force", "FETCH_HEAD"),
            None,
            None,
        )
        actual_revision = _capture_command(
            ("git", "-C", str(stage), "rev-parse", "HEAD")
        )
        if actual_revision != target.source_revision:
            raise ManagedInstallError(
                "fetched source revision does not match the resolved target"
            )
        release_version = _project_release_version(stage)
        if (
            target.release_version is not None
            and release_version != target.release_version
        ):
            raise ManagedInstallError(
                "package release version does not match the resolved target"
            )
        archive_digest = git_archive_digest(stage)
        if (
            target.artifact_digest is not None
            and archive_digest != target.artifact_digest
        ):
            raise ManagedInstallError(
                "staged source archive does not match the release artifact digest"
            )
        marker = ManagedApplicationMarker(
            application_link=str(paths.application_link),
            channel=target.channel,
            release_version=release_version,
            source_revision=actual_revision,
            source_ref=target.source_ref,
            repository_url=target.repository_url,
            artifact_digest=archive_digest,
        )
        _write_marker(stage / MANAGED_MARKER_NAME, marker)
        install_environment = {
            **(os.environ if environment is None else environment),
            "SAT_MANAGED_INSTALL": "1",
            "SAT_INSTALL_STAGE_ONLY": "1",
            "SAT_INSTALL_ROOT": str(paths.application_link),
            "SAT_INSTALL_METADATA_PATH": str(paths.installation_record),
        }
        runner((str(stage / "scripts" / "install.sh"),), stage, install_environment)
        _validate_staged_application(stage, marker)
        schema_support = target.schema_support or _read_staged_schema_support(stage)
        return StagedApplication(
            path=stage,
            marker=marker,
            schema_support=schema_support,
        )
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def activate_staged_application(
    staged: StagedApplication,
    paths: ManagedInstallPaths,
    *,
    installed_at: datetime | None = None,
    fail_after_link_swap: Callable[[], None] | None = None,
) -> InstallationRecord:
    """Atomically switch the stable application link and roll back any failure."""

    _require_managed_root(paths)
    _validate_staged_application(staged.path, staged.marker)
    if Path(staged.marker.application_link) != paths.application_link:
        raise ManagedInstallError("staged application targets a different active link")
    with _exclusive_update_lock(paths):
        return _activate_staged_application_locked(
            staged,
            paths,
            installed_at=installed_at,
            fail_after_link_swap=fail_after_link_swap,
        )


def install_managed_target(
    target: ManagedTarget,
    paths: ManagedInstallPaths,
    *,
    environment: Mapping[str, str] | None = None,
    command_runner: CommandRunner | None = None,
) -> InstallationRecord:
    """Run the shared stage-and-activate transaction."""

    _ensure_managed_root(paths)
    with _exclusive_update_lock(paths):
        staged = stage_managed_target(
            target,
            paths,
            environment=environment,
            command_runner=command_runner,
        )
        try:
            return _activate_staged_application_locked(staged, paths)
        except BaseException:
            shutil.rmtree(staged.path, ignore_errors=True)
            raise


def _activate_staged_application_locked(
    staged: StagedApplication,
    paths: ManagedInstallPaths,
    *,
    installed_at: datetime | None = None,
    fail_after_link_swap: Callable[[], None] | None = None,
) -> InstallationRecord:
    _require_managed_root(paths)
    _validate_staged_application(staged.path, staged.marker)
    if Path(staged.marker.application_link) != paths.application_link:
        raise ManagedInstallError("staged application targets a different active link")
    _assert_no_active_runs(paths.state_root)
    compatibility = inspect_persisted_schema_compatibility(
        configuration_path=paths.configuration_path,
        installation_record_path=paths.installation_record,
        state_root=paths.state_root,
        candidate_support=staged.schema_support,
    )
    if not compatibility.compatible:
        raise ManagedInstallError(
            "candidate cannot read current persisted state: "
            + "; ".join(compatibility.problems)
        )
    previous_record = _read_optional_bytes(paths.installation_record)
    previous_target = _prepare_active_link(paths)
    final_path = _final_release_path(paths, staged.marker)
    if final_path.exists() or final_path.is_symlink():
        if final_path.is_symlink() or not final_path.is_dir():
            raise ManagedInstallError("managed release destination is not a directory")
        existing = load_managed_marker(final_path / MANAGED_MARKER_NAME)
        if existing != staged.marker:
            raise ManagedInstallError(
                "managed release destination has conflicting provenance"
            )
        shutil.rmtree(staged.path)
    else:
        os.replace(staged.path, final_path)
    switched = False
    try:
        _replace_application_link(paths.application_link, final_path)
        switched = True
        if fail_after_link_swap is not None:
            fail_after_link_swap()
        record = make_installation_record(
            channel=staged.marker.channel,
            release_version=staged.marker.release_version,
            source_revision=staged.marker.source_revision,
            source_ref=staged.marker.source_ref,
            repository_url=staged.marker.repository_url,
            application_path=paths.application_link,
            artifact_digest=staged.marker.artifact_digest,
            installed_at=installed_at or datetime.now(UTC),
        )
        save_installation_record(record, paths.installation_record)
        _activate_launchers(paths)
        return record
    except BaseException:
        if switched:
            _restore_application_link(paths.application_link, previous_target)
        _restore_optional_bytes(paths.installation_record, previous_record)
        raise


def load_managed_marker(path: Path) -> ManagedApplicationMarker:
    """Read a current release marker without following a marker symlink."""

    if path.is_symlink() or not path.is_file():
        raise ManagedInstallError(f"managed application marker is invalid: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return ManagedApplicationMarker.model_validate(payload)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        raise ManagedInstallError(
            f"managed application marker is invalid: {path}"
        ) from error


def load_managed_root_marker(path: Path) -> ManagedRootMarker:
    """Read the lifecycle ownership marker without following a symlink."""

    if path.is_symlink() or not path.is_file():
        raise ManagedInstallError(f"managed root marker is invalid: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return ManagedRootMarker.model_validate(payload)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        raise ManagedInstallError(f"managed root marker is invalid: {path}") from error


def _write_marker(path: Path, marker: ManagedApplicationMarker) -> None:
    if path.is_symlink():
        raise ManagedInstallError("managed application marker cannot be a symlink")
    content = (
        json.dumps(marker.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    ).encode()
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _write_root_marker(path: Path, marker: ManagedRootMarker) -> None:
    if path.is_symlink():
        raise ManagedInstallError("managed root marker cannot be a symlink")
    content = (
        json.dumps(marker.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    ).encode()
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _expected_root_marker(paths: ManagedInstallPaths) -> ManagedRootMarker:
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


def _ensure_managed_root(paths: ManagedInstallPaths) -> None:
    expected = _expected_root_marker(paths)
    root = paths.managed_root
    root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _require_real_directory(root.parent, "managed application parent")
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise ManagedInstallError("managed application root must be a real directory")
    if not root.exists():
        root.mkdir(mode=0o700)
    marker_path = root / MANAGED_ROOT_MARKER_NAME
    if marker_path.exists() or marker_path.is_symlink():
        actual = load_managed_root_marker(marker_path)
        if actual != expected:
            raise ManagedInstallError(
                "managed application root belongs to different lifecycle paths"
            )
        return
    unknown = tuple(root.iterdir())
    if (
        paths.application_link.parent == root
        and paths.application_link in unknown
        and not _is_legacy_owned_application(paths.application_link)
    ):
        raise ManagedInstallError("existing application path is not owned by SAT")
    if unknown and not _is_migratable_legacy_root(paths, unknown):
        raise ManagedInstallError(
            "existing managed application root has no valid ownership marker"
        )
    _write_root_marker(marker_path, expected)


def _require_managed_root(paths: ManagedInstallPaths) -> ManagedRootMarker:
    _require_real_directory(paths.managed_root, "managed application root")
    actual = load_managed_root_marker(paths.managed_root / MANAGED_ROOT_MARKER_NAME)
    expected = _expected_root_marker(paths)
    if actual != expected:
        raise ManagedInstallError(
            "managed application root belongs to different lifecycle paths"
        )
    return actual


def _is_migratable_legacy_root(
    paths: ManagedInstallPaths,
    children: Sequence[Path],
) -> bool:
    if paths.application_link.parent != paths.managed_root:
        return False
    allowed = {paths.application_link}
    if paths.installation_record.parent == paths.managed_root:
        allowed.add(paths.installation_record)
    if any(child not in allowed for child in children):
        return False
    return _is_legacy_owned_application(paths.application_link)


def _is_legacy_owned_application(application: Path) -> bool:
    if application.is_symlink() or not application.is_dir():
        return False
    marker = application / MANAGED_MARKER_NAME
    if marker.is_symlink() or not marker.is_file():
        return False
    try:
        lines = marker.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    return lines[:2] == [
        "software-agent-team-managed-v1",
        f"root={application}",
    ]


def _validate_managed_destination(paths: ManagedInstallPaths) -> None:
    _require_managed_root(paths)
    application = paths.application_link
    if application.is_symlink():
        try:
            active = application.resolve(strict=True)
        except OSError as error:
            raise ManagedInstallError("managed application link is broken") from error
        _require_managed_release_target(active, paths)
    elif application.exists() and not _is_legacy_owned_application(application):
        raise ManagedInstallError("existing application path is not owned by SAT")

    record = load_installation_record(paths.installation_record)
    if record is not None and Path(record.application_path) != application:
        raise ManagedInstallError(
            "installation record belongs to a different managed application"
        )

    expected_launchers = {
        paths.bin_directory / "sat": application / ".venv" / "bin" / "sat",
        paths.bin_directory / "sat-uninstall": application / "scripts" / "uninstall.sh",
    }
    for launcher, target in expected_launchers.items():
        if launcher.is_symlink():
            if Path(os.readlink(launcher)) != target:
                raise ManagedInstallError(
                    f"launcher points to a different installation: {launcher}"
                )
        elif launcher.exists():
            raise ManagedInstallError(
                f"launcher already exists and is not managed: {launcher}"
            )


def _project_release_version(repository: Path) -> str:
    try:
        payload = tomllib.loads(
            (repository / "pyproject.toml").read_text(encoding="utf-8")
        )
        value = payload["project"]["version"]
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as error:
        raise ManagedInstallError("staged package version is unavailable") from error
    if not isinstance(value, str):
        raise ManagedInstallError("staged package version is invalid")
    try:
        parse_release_version(value)
    except ValueError as error:
        raise ManagedInstallError("staged package version is invalid") from error
    return value


def _read_staged_schema_support(stage: Path) -> tuple[SchemaSupport, ...]:
    try:
        completed = subprocess.run(
            [str(stage / ".venv" / "bin" / "sat"), "version", "--json"],
            cwd=stage,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        payload = json.loads(completed.stdout)
        values = payload["schema_support"]
        support = tuple(SchemaSupport.model_validate(value) for value in values)
    except (
        OSError,
        KeyError,
        TypeError,
        json.JSONDecodeError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        ValueError,
    ) as error:
        raise ManagedInstallError(
            "staged application did not report valid schema compatibility"
        ) from error
    if len(support) != len({item.family for item in support}):
        raise ManagedInstallError(
            "staged application reported duplicate schema compatibility families"
        )
    return support


def _validate_staged_application(
    path: Path,
    expected_marker: ManagedApplicationMarker,
) -> None:
    _require_real_directory(path, "staged application")
    marker = load_managed_marker(path / MANAGED_MARKER_NAME)
    if marker != expected_marker:
        raise ManagedInstallError(
            "staged application marker changed after installation"
        )
    for relative in (".venv/bin/sat", "scripts/uninstall.sh"):
        candidate = path / relative
        if (
            candidate.is_symlink()
            or not candidate.is_file()
            or not os.access(candidate, os.X_OK)
        ):
            raise ManagedInstallError(
                f"staged application is missing executable {relative}"
            )


def _final_release_path(
    paths: ManagedInstallPaths,
    marker: ManagedApplicationMarker,
) -> Path:
    label = f"{marker.release_version}-g{marker.source_revision[:12]}"
    return paths.versions_root / label


def _prepare_active_link(paths: ManagedInstallPaths) -> Path | None:
    link = paths.application_link
    if link.is_symlink():
        target = link.resolve(strict=True)
        _require_managed_release_target(target, paths)
        return target
    if not link.exists():
        return None
    if not link.is_dir():
        raise ManagedInstallError(
            "existing managed application path is not a directory"
        )
    if not _is_legacy_owned_application(link):
        raise ManagedInstallError("existing application path is not owned by SAT")
    legacy_marker = link / MANAGED_MARKER_NAME
    revision = _capture_command(("git", "-C", str(link), "rev-parse", "HEAD"))
    if _SOURCE_REVISION_PATTERN.fullmatch(revision) is None:
        raise ManagedInstallError("legacy application Git identity is invalid")
    legacy_target = paths.versions_root / f"legacy-g{revision[:12]}"
    if legacy_target.exists() or legacy_target.is_symlink():
        raise ManagedInstallError("legacy migration destination already exists")
    release_version = _project_release_version(link)
    repository_url = _capture_command(
        ("git", "-C", str(link), "remote", "get-url", "origin")
    )
    legacy_content = legacy_marker.read_bytes()
    legacy_identity = ManagedApplicationMarker(
        application_link=str(link),
        channel=ManagedChannel.DEV,
        release_version=release_version,
        source_revision=revision,
        source_ref="legacy",
        repository_url=repository_url,
        artifact_digest=git_archive_digest(link),
    )
    os.replace(link, legacy_target)
    try:
        _write_marker(legacy_target / MANAGED_MARKER_NAME, legacy_identity)
        _replace_application_link(link, legacy_target)
    except BaseException:
        with suppress(OSError):
            (legacy_target / MANAGED_MARKER_NAME).write_bytes(legacy_content)
        os.replace(legacy_target, link)
        raise
    return legacy_target


def _require_managed_release_target(target: Path, paths: ManagedInstallPaths) -> None:
    root = paths.versions_root.resolve(strict=True)
    try:
        target.relative_to(root)
    except ValueError as error:
        raise ManagedInstallError(
            "active application link escapes the versions root"
        ) from error
    if target.parent != root:
        raise ManagedInstallError("managed release must be a direct version entry")
    marker = load_managed_marker(target / MANAGED_MARKER_NAME)
    if Path(marker.application_link) != paths.application_link:
        raise ManagedInstallError(
            "managed release marker belongs to a different application link"
        )


def _replace_application_link(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _require_real_directory(link.parent, "managed application parent")
    relative_target = os.path.relpath(target, start=link.parent)
    temporary = link.parent / f".{link.name}.{uuid4().hex}.tmp"
    try:
        temporary.symlink_to(relative_target, target_is_directory=True)
        os.replace(temporary, link)
        _fsync_directory(link.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _restore_application_link(link: Path, target: Path | None) -> None:
    if target is None:
        if link.is_symlink():
            link.unlink()
            _fsync_directory(link.parent)
        return
    _replace_application_link(link, target)


def _activate_launchers(paths: ManagedInstallPaths) -> None:
    paths.bin_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    _require_real_directory(paths.bin_directory, "launcher directory")
    targets = {
        paths.bin_directory / "sat": paths.application_link / ".venv" / "bin" / "sat",
        paths.bin_directory / "sat-uninstall": paths.application_link
        / "scripts"
        / "uninstall.sh",
    }
    missing: list[tuple[Path, Path]] = []
    for link, target in targets.items():
        if link.is_symlink():
            existing = Path(os.readlink(link))
            if existing != target:
                raise ManagedInstallError(
                    f"launcher points to a different installation: {link}"
                )
            continue
        if link.exists():
            raise ManagedInstallError(
                f"launcher already exists and is not managed: {link}"
            )
        missing.append((link, target))
    created: list[Path] = []
    try:
        for link, target in missing:
            link.symlink_to(target)
            created.append(link)
    except BaseException:
        for link in created:
            link.unlink(missing_ok=True)
        raise


def _assert_no_active_runs(state_root: Path) -> None:
    runs = state_root / "runs"
    if not runs.exists():
        return
    if runs.is_symlink() or not runs.is_dir():
        raise ManagedInstallError("run state root is not a real directory")
    active: list[Path] = []
    for path in sorted(runs.glob("*/run.json")):
        if path.is_symlink() or not path.is_file():
            raise ManagedInstallError(f"run state is not a regular file: {path}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            phase = payload["phase"]
        except (OSError, KeyError, json.JSONDecodeError, TypeError) as error:
            raise ManagedInstallError(
                f"run state cannot be verified: {path}"
            ) from error
        if phase not in {"completed", "failed"}:
            active.append(path)
    if active:
        raise ManagedInstallError(
            "managed application cannot change while a run is active: "
            + ", ".join(str(path.parent.name) for path in active)
        )


@contextmanager
def _exclusive_update_lock(paths: ManagedInstallPaths):
    _require_managed_root(paths)
    paths.lock.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _require_real_directory(paths.lock.parent, "managed application root")
    if paths.lock.is_symlink() or (paths.lock.exists() and not paths.lock.is_file()):
        raise ManagedInstallError("managed update lock must be a regular file")
    descriptor = os.open(
        paths.lock,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ManagedInstallError(
                "another managed install or update is active"
            ) from error
        yield
    finally:
        os.close(descriptor)


def _run_command(
    arguments: Sequence[str],
    cwd: Path | None,
    environment: Mapping[str, str] | None,
) -> None:
    try:
        subprocess.run(
            list(arguments),
            cwd=cwd,
            env=None if environment is None else dict(environment),
            check=True,
            timeout=1_800,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise ManagedInstallError(f"managed command failed: {arguments[0]}") from error


def _capture_command(arguments: Sequence[str]) -> str:
    try:
        completed = subprocess.run(
            list(arguments),
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise ManagedInstallError(f"managed command failed: {arguments[0]}") from error
    return completed.stdout.strip()


def _absolute_override(value: str, variable: str) -> Path:
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


def _require_real_directory(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_dir():
        raise ManagedInstallError(f"{label} must be a real directory: {path}")


def _read_optional_bytes(path: Path) -> bytes | None:
    if path.is_symlink():
        raise ManagedInstallError("installation record must not be a symbolic link")
    if not path.exists():
        return None
    if not path.is_file():
        raise ManagedInstallError("installation record must be a regular file")
    return path.read_bytes()


def _restore_optional_bytes(path: Path, content: bytes | None) -> None:
    if content is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.parent / f".{path.name}.{uuid4().hex}.rollback"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _is_sha256_digest(value: str) -> bool:
    prefix = "sha256:"
    digest = value.removeprefix(prefix)
    return (
        value.startswith(prefix)
        and len(digest) == 64
        and all(character in "0123456789abcdef" for character in digest)
    )


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True
