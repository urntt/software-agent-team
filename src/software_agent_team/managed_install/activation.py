"""Activation, symlink rotation, rollback handling, and task lease management.

Coordinates the atomic transition of application links, launcher script creation,
foreground task leasing locks, and automatic rollback on failure.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Mapping
from contextlib import ExitStack, contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from software_agent_team import managed_install
from software_agent_team.docker_engine import bound_docker_engine
from software_agent_team.integrity import canonical_model_sha256
from software_agent_team.managed_install.constants import (
    _SOURCE_REVISION_PATTERN,
    MANAGED_MARKER_NAME,
    MANAGED_ROOT_MARKER_NAME,
    ManagedInstallError,
    _managed_failure_detail,
)
from software_agent_team.managed_install.models import (
    ManagedApplicationMarker,
    ManagedInstallPaths,
    ManagedRootMarker,
    ManagedTarget,
    StagedApplication,
)
from software_agent_team.managed_install.utils import (
    CommandRunner,
    _capture_command,
    _fsync_directory,
    _lifecycle_file_lock,
    _read_optional_bytes,
    _require_real_directory,
    _restore_optional_bytes,
)
from software_agent_team.releases import git_archive_digest
from software_agent_team.versioning import (
    InstallationRecord,
    ManagedChannel,
    make_installation_record,
    save_installation_record,
)


@contextmanager
def managed_foreground_task_lease(project_root: Path):
    """Keep one active managed release fixed for a complete foreground task."""

    root = project_root.resolve(strict=True)
    release_marker_path = root / MANAGED_MARKER_NAME
    if not release_marker_path.exists() and not release_marker_path.is_symlink():
        yield
        return
    release_marker = managed_install.load_managed_marker(release_marker_path)
    application_link = Path(release_marker.application_link)
    managed_root, root_marker = managed_install._managed_root_for_release(
        release_marker
    )
    versions_root = Path(root_marker.versions_root).resolve(strict=True)
    if root.parent != versions_root:
        raise ManagedInstallError(
            "managed release is not a direct entry in its versions root"
        )
    lock_path = managed_root / "update.lock"
    with _lifecycle_file_lock(lock_path, shared=True):
        current_marker = managed_install.load_managed_marker(release_marker_path)
        if current_marker != release_marker:
            raise ManagedInstallError(
                "managed application identity changed before task admission"
            )
        try:
            active_root = application_link.resolve(strict=True)
        except OSError as error:
            raise ManagedInstallError(
                "managed application changed before task admission; rerun sat"
            ) from error
        if active_root != root:
            raise ManagedInstallError(
                "managed application changed before task admission; rerun sat"
            )
        yield


def activate_staged_application(
    staged: StagedApplication,
    paths: ManagedInstallPaths,
    *,
    installed_at: datetime | None = None,
    fail_after_link_swap: Callable[[], None] | None = None,
) -> InstallationRecord:
    """Atomically switch the stable application link and roll back any failure."""

    engine_scope = ExitStack()
    if staged.docker_engine is not None:
        engine_scope.enter_context(bound_docker_engine(staged.docker_engine))
    try:
        with managed_install._exclusive_update_lock(paths):
            return _activate_staged_application_transaction(
                staged,
                paths,
                installed_at=installed_at,
                fail_after_link_swap=fail_after_link_swap,
            )
    finally:
        engine_scope.close()


def install_managed_target(
    target: ManagedTarget,
    paths: ManagedInstallPaths,
    *,
    environment: Mapping[str, str] | None = None,
    command_runner: CommandRunner | None = None,
) -> InstallationRecord:
    """Run the shared stage-and-activate transaction."""

    managed_install._ensure_managed_root(paths)
    with managed_install._exclusive_update_lock(paths):
        staged = managed_install._stage_managed_target_locked(
            target,
            paths,
            environment=environment,
            command_runner=command_runner,
        )
        with bound_docker_engine(staged.docker_engine):
            try:
                return _activate_staged_application_transaction(staged, paths)
            except BaseException:
                if staged.created_candidate:
                    shutil.rmtree(staged.path, ignore_errors=True)
                raise


def _activate_staged_application_transaction(
    staged: StagedApplication,
    paths: ManagedInstallPaths,
    *,
    installed_at: datetime | None = None,
    fail_after_link_swap: Callable[[], None] | None = None,
) -> InstallationRecord:
    """Own image rollback once, while the caller holds the update lock."""

    try:
        return _activate_staged_application_locked(
            staged,
            paths,
            installed_at=installed_at,
            fail_after_link_swap=fail_after_link_swap,
        )
    except BaseException as activation_error:
        if staged.sandbox_image_transition is not None:
            try:
                managed_install._restore_sandbox_image_transition(
                    staged.sandbox_image_transition
                )
            except ManagedInstallError as restore_error:
                raise ManagedInstallError(
                    "managed activation failed "
                    f"({_managed_failure_detail(activation_error)}); sandbox image "
                    f"rollback also failed ({_managed_failure_detail(restore_error)})"
                ) from activation_error
        raise


def _activate_staged_application_locked(
    staged: StagedApplication,
    paths: ManagedInstallPaths,
    *,
    installed_at: datetime | None = None,
    fail_after_link_swap: Callable[[], None] | None = None,
) -> InstallationRecord:
    """Commit candidate activation while holding the lifecycle update lock."""

    managed_install._require_managed_root(paths)
    managed_install._validate_staged_application(staged.path, staged.marker)
    if Path(staged.marker.application_link) != paths.application_link:
        raise ManagedInstallError("staged application targets a different active link")
    envelope = managed_install._inspect_state_with_candidate(staged, paths)
    if envelope.source_revision != staged.marker.source_revision:
        raise ManagedInstallError(
            "candidate compatibility result has a different source revision"
        )
    if managed_install._schema_support_identity(
        envelope.schema_support
    ) != managed_install._schema_support_identity(staged.schema_support):
        raise ManagedInstallError(
            "candidate compatibility result differs from advertised schema support"
        )
    compatibility = envelope.compatibility
    if not compatibility.compatible:
        raise ManagedInstallError(
            managed_install._render_schema_compatibility_failure(
                compatibility,
                paths=paths,
                heading="candidate cannot read current persisted state",
            )
        )
    if envelope.active_run_ids:
        raise ManagedInstallError(
            "managed application cannot change while a run is active: "
            + ", ".join(envelope.active_run_ids)
        )
    managed_install._validate_staged_application(staged.path, staged.marker)
    previous_record = _read_optional_bytes(paths.installation_record)
    previous_target = _prepare_active_link(paths)
    final_path = _final_release_path(paths, staged.marker)
    if final_path.exists() or final_path.is_symlink():
        if final_path.is_symlink() or not final_path.is_dir():
            raise ManagedInstallError("managed release destination is not a directory")
        existing = managed_install.load_managed_marker(final_path / MANAGED_MARKER_NAME)
        if existing != staged.marker:
            raise ManagedInstallError(
                "managed release destination has conflicting provenance"
            )
        if staged.path != final_path:
            shutil.rmtree(staged.path)
    else:
        os.replace(staged.path, final_path)
    if final_path != previous_target:
        _retire_superseded_applications(
            paths,
            retained=tuple(
                path for path in (final_path, previous_target) if path is not None
            ),
        )
    switched = False
    created_launchers: tuple[Path, ...] = ()
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
            docker_engine=staged.docker_engine,
            installed_at=installed_at or datetime.now(UTC),
        )
        save_installation_record(record, paths.installation_record)
        created_launchers = _activate_launchers(paths)
        _validate_active_application(paths)
        if staged.sandbox_image_transition is not None and (
            managed_install._release_sandbox_image_rollback_reference(
                staged.sandbox_image_transition
            )
        ):
            managed_install._cleanup_superseded_sandbox_image(
                staged.sandbox_image_transition
            )
        return record
    except BaseException:
        for launcher in created_launchers:
            if launcher.is_symlink():
                launcher.unlink()
        if switched:
            _restore_application_link(paths.application_link, previous_target)
        _restore_optional_bytes(paths.installation_record, previous_record)
        raise


def _active_managed_lifecycle(
    project_root: Path,
) -> tuple[Path, ManagedRootMarker, ManagedApplicationMarker] | None:
    """Resolve one active managed release without trusting ambient path overrides."""

    root = project_root.resolve(strict=True)
    release_marker_path = root / MANAGED_MARKER_NAME
    if not release_marker_path.exists() and not release_marker_path.is_symlink():
        return None
    release_marker = managed_install.load_managed_marker(release_marker_path)
    application_link = Path(release_marker.application_link)
    managed_root, root_marker = managed_install._managed_root_for_release(
        release_marker
    )
    versions_root = Path(root_marker.versions_root).resolve(strict=True)
    if root.parent != versions_root:
        raise ManagedInstallError(
            "managed release is not a direct entry in its versions root"
        )
    installation_record = Path(root_marker.installation_record)
    if not application_link.exists() and not application_link.is_symlink():
        if not installation_record.exists() and not installation_record.is_symlink():
            return None
        raise ManagedInstallError("managed application link is missing")
    try:
        active_root = application_link.resolve(strict=True)
    except OSError as error:
        raise ManagedInstallError("managed application link is broken") from error
    if active_root != root:
        return None
    return managed_root, root_marker, release_marker


def is_retired_managed_application(
    project_root: Path,
    retired_root: Path,
) -> bool:
    """Recognize a missing sibling in the active installation's version storage."""

    try:
        lifecycle = managed_install._active_managed_lifecycle(project_root)
        if lifecycle is None:
            return False
        managed_root, marker, _ = lifecycle
        versions = Path(marker.versions_root)
        if retired_root.parent != versions:
            return False
        for path in (
            managed_root,
            versions,
            project_root,
            managed_root / MANAGED_ROOT_MARKER_NAME,
            project_root / MANAGED_MARKER_NAME,
        ):
            if path.is_symlink() or path.stat().st_uid != os.geteuid():
                return False
        if versions.resolve(strict=True) != versions:
            return False
        return not retired_root.exists() and not retired_root.is_symlink()
    except (OSError, ManagedInstallError):
        return False


def _retire_superseded_applications(
    paths: ManagedInstallPaths,
    *,
    retained: tuple[Path, ...],
) -> None:
    """Keep only the active candidate and one direct predecessor release."""
    root = paths.versions_root.resolve(strict=True)
    keep = {path.resolve(strict=True) for path in retained}
    retire: list[Path] = []
    for candidate in sorted(root.iterdir(), key=lambda path: path.name):
        if candidate.is_symlink():
            raise ManagedInstallError(
                "managed versions root contains an unsafe release entry"
            )
        if candidate.resolve(strict=True) in keep:
            continue
        if not candidate.is_dir():
            raise ManagedInstallError(
                "managed versions root contains an unsafe release entry"
            )
        _require_managed_release_target(candidate, paths)
        retire.append(candidate)
    for candidate in retire:
        shutil.rmtree(candidate)


def _final_release_path(
    paths: ManagedInstallPaths,
    marker: ManagedApplicationMarker,
) -> Path:
    """Compute the immutable release target directory path."""

    legacy = paths.versions_root / (
        f"{marker.release_version}-g{marker.source_revision[:12]}"
    )
    if legacy.exists() or legacy.is_symlink():
        if legacy.is_symlink() or not legacy.is_dir():
            raise ManagedInstallError("managed release destination is not a directory")
        if managed_install.load_managed_marker(legacy / MANAGED_MARKER_NAME) == marker:
            return legacy
    return paths.versions_root / (
        f"{marker.release_version}-p{canonical_model_sha256(marker)}"
    )


def _prepare_active_link(paths: ManagedInstallPaths) -> Path | None:
    """Resolve current active target or migrate legacy single-directory SAT install."""

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
    if not managed_install._is_legacy_owned_application(link):
        raise ManagedInstallError("existing application path is not owned by SAT")
    legacy_marker = link / MANAGED_MARKER_NAME
    revision = _capture_command(("git", "-C", str(link), "rev-parse", "HEAD"))
    if _SOURCE_REVISION_PATTERN.fullmatch(revision) is None:
        raise ManagedInstallError("legacy application Git identity is invalid")
    legacy_target = paths.versions_root / f"legacy-g{revision[:12]}"
    if legacy_target.exists() or legacy_target.is_symlink():
        raise ManagedInstallError("legacy migration destination already exists")
    release_version = managed_install._project_release_version(link)
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
        managed_install._write_marker(
            legacy_target / MANAGED_MARKER_NAME, legacy_identity
        )
        _replace_application_link(link, legacy_target)
    except BaseException:
        with suppress(OSError):
            (legacy_target / MANAGED_MARKER_NAME).write_bytes(legacy_content)
        os.replace(legacy_target, link)
        raise
    return legacy_target


def _require_managed_release_target(target: Path, paths: ManagedInstallPaths) -> None:
    """Validate that target lives directly inside paths.versions_root."""

    root = paths.versions_root.resolve(strict=True)
    try:
        target.relative_to(root)
    except ValueError as error:
        raise ManagedInstallError(
            "active application link escapes the versions root"
        ) from error
    if target.parent != root:
        raise ManagedInstallError("managed release must be a direct version entry")
    marker = managed_install.load_managed_marker(target / MANAGED_MARKER_NAME)
    if Path(marker.application_link) != paths.application_link:
        raise ManagedInstallError(
            "managed release marker belongs to a different application link"
        )


def _replace_application_link(link: Path, target: Path) -> None:
    """Atomically swap the application symlink using a temporary sibling link."""
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
    """Restore the previous application symlink on failure."""
    if target is None:
        if link.is_symlink():
            link.unlink()
            _fsync_directory(link.parent)
        return
    _replace_application_link(link, target)


def _activate_launchers(paths: ManagedInstallPaths) -> tuple[Path, ...]:
    """Create executable symlinks in bin_directory (e.g. sat, sat-uninstall)."""
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
    return tuple(created)


def _validate_active_application(paths: ManagedInstallPaths) -> None:
    """Prove the final user-facing launcher after link and record activation."""
    _require_managed_release_target(
        paths.application_link.resolve(strict=True),
        paths,
    )
    launcher = paths.bin_directory / "sat"
    if not launcher.is_symlink():
        raise ManagedInstallError("managed sat launcher is not a symbolic link")
    _capture_command((str(launcher), "--version"))
