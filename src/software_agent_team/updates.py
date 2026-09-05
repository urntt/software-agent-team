"""Managed update planning shared by explicit checks, updates, and switches."""

from __future__ import annotations

import os
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from software_agent_team.managed_install import (
    LATEST_RELEASE_API_ENVIRONMENT_VARIABLE,
    MANAGED_ROOT_MARKER_NAME,
    ManagedApplicationMarker,
    ManagedInstallError,
    ManagedInstallPaths,
    ManagedTarget,
    load_managed_marker,
    load_managed_root_marker,
    resolve_dev_target,
    target_from_stable_release,
)
from software_agent_team.releases import (
    DEFAULT_LATEST_RELEASE_API_URL,
    ReleaseResolutionError,
    UpdateAvailability,
    compare_stable_target,
    git_archive_digest,
    resolve_latest_stable_release,
)
from software_agent_team.versioning import (
    InstallationRecord,
    InstallMode,
    ManagedChannel,
    SoftwareVersionReport,
    inspect_software_version,
    load_installation_record,
)


class ManagedChangeStatus(StrEnum):
    """Result of comparing one installed and requested managed target."""

    CURRENT = "current"
    UPDATE_AVAILABLE = "update_available"
    CHANNEL_SWITCH = "channel_switch"
    LOCAL_NEWER = "local_newer"
    INCONSISTENT = "inconsistent"


class ForegroundUpdateStatus(StrEnum):
    """Task-admission update states with explicit user-facing semantics."""

    NOT_APPLICABLE = "not_applicable"
    CURRENT = "current"
    UPDATE_AVAILABLE = "update_available"
    PROVENANCE_CHANGED = "provenance_changed"
    UNAVAILABLE = "unavailable"
    INCONSISTENT = "inconsistent"


class ForegroundUpdateObservation(BaseModel):
    """One fresh, foreground-only update observation for a bare ``sat`` run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: ForegroundUpdateStatus
    current_channel: ManagedChannel | None
    current_version: str
    target_version: str | None = None
    current_revision: str | None = None
    target_revision: str | None = None
    network_attempted: bool
    detail: str = Field(min_length=1, max_length=2000)

    @property
    def blocks_task(self) -> bool:
        """Return whether local installation provenance is unsafe to consume."""

        return self.status is ForegroundUpdateStatus.INCONSISTENT

    @property
    def prompts_update(self) -> bool:
        """Return whether a numeric stable release update should be shown."""

        return self.status is ForegroundUpdateStatus.UPDATE_AVAILABLE


class ManagedChangePlan(BaseModel):
    """User-visible preview before any staged installation or activation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: ManagedChangeStatus
    current_channel: ManagedChannel
    target_channel: ManagedChannel
    current_version: str
    target_version: str | None
    current_revision: str
    target_revision: str
    target_ref: str
    detail: str

    @property
    def requires_activation(self) -> bool:
        return self.status in {
            ManagedChangeStatus.UPDATE_AVAILABLE,
            ManagedChangeStatus.CHANNEL_SWITCH,
        }


def validate_current_managed_install(
    *,
    project_root: Path,
    paths: ManagedInstallPaths,
) -> tuple[InstallationRecord, ManagedApplicationMarker]:
    """Prove that this process is executing the recorded active application."""

    expected_root_marker = {
        "managed_root": str(paths.managed_root),
        "application_link": str(paths.application_link),
        "versions_root": str(paths.versions_root),
        "installation_record": str(paths.installation_record),
        "bin_directory": str(paths.bin_directory),
    }
    root_marker = load_managed_root_marker(
        paths.managed_root / MANAGED_ROOT_MARKER_NAME
    )
    if root_marker.model_dump(exclude={"schema_version"}) != expected_root_marker:
        raise ManagedInstallError(
            "managed application root conflicts with the active path configuration"
        )
    record = load_installation_record(paths.installation_record)
    if record is None:
        raise ManagedInstallError(
            "this SAT installation has no managed identity; source checkouts and "
            "legacy installs cannot be changed by `sat update`"
        )
    if Path(record.application_path) != paths.application_link:
        raise ManagedInstallError(
            "installation record belongs to a different managed application"
        )
    if not paths.application_link.is_symlink():
        raise ManagedInstallError(
            "managed application link is missing or not a symlink"
        )
    try:
        active = paths.application_link.resolve(strict=True)
    except OSError as error:
        raise ManagedInstallError("managed application link is broken") from error
    if active != project_root.resolve():
        raise ManagedInstallError(
            "this SAT process is not running from the recorded active application"
        )
    try:
        active.relative_to(paths.versions_root.resolve(strict=True))
    except (OSError, ValueError) as error:
        raise ManagedInstallError(
            "active managed application escapes the versions root"
        ) from error
    marker = load_managed_marker(active / ".sat-managed-install")
    expected = {
        "application_link": record.application_path,
        "channel": record.channel,
        "release_version": record.release_version,
        "source_revision": record.source_revision,
        "source_ref": record.source_ref,
        "repository_url": record.repository_url,
        "artifact_digest": record.artifact_digest,
    }
    actual = marker.model_dump(exclude={"schema_version"})
    if actual != expected:
        raise ManagedInstallError(
            "active application marker conflicts with the installation record"
        )
    git_directory = active / ".git"
    if git_directory.exists():
        revision = _git_output(active, "rev-parse", "HEAD")
        if revision != record.source_revision:
            raise ManagedInstallError(
                "active Git revision conflicts with the installation record"
            )
        if _git_output(active, "status", "--porcelain", "--untracked-files=all"):
            raise ManagedInstallError(
                "active managed application contains source drift"
            )
        if record.artifact_digest != git_archive_digest(active):
            raise ManagedInstallError(
                "active source archive conflicts with the installation record"
            )
    return record, marker


def resolve_requested_target(
    *,
    record: InstallationRecord,
    channel: ManagedChannel,
    dev_ref: str | None = None,
    environment: Mapping[str, str] | None = None,
) -> ManagedTarget:
    """Resolve stable or dev through its one authoritative resolver."""

    values = os.environ if environment is None else environment
    if channel is ManagedChannel.STABLE:
        if dev_ref is not None:
            raise ManagedInstallError("a dev ref cannot be supplied for stable")
        api_url = values.get(
            LATEST_RELEASE_API_ENVIRONMENT_VARIABLE,
            DEFAULT_LATEST_RELEASE_API_URL,
        )
        target = resolve_latest_stable_release(
            release_api_url=api_url,
            expected_repository_url=record.repository_url,
        )
        return target_from_stable_release(target)
    source_ref = dev_ref or (record.source_ref if record.channel is channel else "main")
    return resolve_dev_target(
        repository_url=record.repository_url,
        source_ref=source_ref,
    )


def plan_managed_change(
    record: InstallationRecord,
    target: ManagedTarget,
) -> ManagedChangePlan:
    """Compare exact identities without mutating installation state."""

    if record.channel is not target.channel:
        return ManagedChangePlan(
            status=ManagedChangeStatus.CHANNEL_SWITCH,
            current_channel=record.channel,
            target_channel=target.channel,
            current_version=record.release_version,
            target_version=target.release_version,
            current_revision=record.source_revision,
            target_revision=target.source_revision,
            target_ref=target.source_ref,
            detail=(
                f"switch channel from {record.channel.value} to {target.channel.value}"
            ),
        )
    if target.channel is ManagedChannel.STABLE:
        if target.release_version is None or target.artifact_digest is None:
            raise ManagedInstallError("stable target has incomplete release identity")
        comparison = compare_stable_target(
            record,
            release_version=target.release_version,
            source_revision=target.source_revision,
            source_ref=target.source_ref,
            artifact_digest=target.artifact_digest,
        )
        status = {
            UpdateAvailability.CURRENT: ManagedChangeStatus.CURRENT,
            UpdateAvailability.AVAILABLE: ManagedChangeStatus.UPDATE_AVAILABLE,
            UpdateAvailability.LOCAL_NEWER: ManagedChangeStatus.LOCAL_NEWER,
            UpdateAvailability.INCONSISTENT: ManagedChangeStatus.INCONSISTENT,
        }[comparison.status]
        return ManagedChangePlan(
            status=status,
            current_channel=record.channel,
            target_channel=target.channel,
            current_version=record.release_version,
            target_version=target.release_version,
            current_revision=record.source_revision,
            target_revision=target.source_revision,
            target_ref=target.source_ref,
            detail=comparison.detail,
        )
    if record.source_revision == target.source_revision:
        status = ManagedChangeStatus.CURRENT
        detail = "the installed dev target is current"
    else:
        status = ManagedChangeStatus.UPDATE_AVAILABLE
        detail = (
            f"dev target {target.source_ref} changed from "
            f"{record.source_revision[:12]} to {target.source_revision[:12]}"
        )
    return ManagedChangePlan(
        status=status,
        current_channel=record.channel,
        target_channel=target.channel,
        current_version=record.release_version,
        target_version=target.release_version,
        current_revision=record.source_revision,
        target_revision=target.source_revision,
        target_ref=target.source_ref,
        detail=detail,
    )


def inspect_task_admission_update(
    *,
    project_root: Path,
    environment: Mapping[str, str] | None = None,
    version_report: SoftwareVersionReport | None = None,
) -> ForegroundUpdateObservation:
    """Check the active managed channel once in the foreground task lifecycle.

    Source checkouts and unmanaged packages are intentionally local-only.  A
    remote resolution failure is observable but never makes an otherwise safe
    task unavailable.  Local managed-install inconsistency remains blocking.
    """

    version = version_report or inspect_software_version(
        project_root=project_root,
        environment=environment,
    )
    if version.install_mode is not InstallMode.MANAGED:
        return ForegroundUpdateObservation(
            status=ForegroundUpdateStatus.NOT_APPLICABLE,
            current_channel=version.channel,
            current_version=version.release_version,
            current_revision=version.source_revision,
            network_attempted=False,
            detail=(
                f"{version.install_mode.value} installations are not changed by "
                "the managed updater"
            ),
        )

    try:
        paths = ManagedInstallPaths.from_environment(environment)
        record, _marker = validate_current_managed_install(
            project_root=project_root,
            paths=paths,
        )
    except ManagedInstallError as error:
        return ForegroundUpdateObservation(
            status=ForegroundUpdateStatus.INCONSISTENT,
            current_channel=version.channel,
            current_version=version.release_version,
            current_revision=version.source_revision,
            network_attempted=False,
            detail=f"managed installation identity is inconsistent: {error}",
        )

    try:
        target = resolve_requested_target(
            record=record,
            channel=record.channel,
            environment=environment,
        )
    except (ManagedInstallError, ReleaseResolutionError) as error:
        return ForegroundUpdateObservation(
            status=ForegroundUpdateStatus.UNAVAILABLE,
            current_channel=record.channel,
            current_version=record.release_version,
            current_revision=record.source_revision,
            network_attempted=True,
            detail=f"update metadata is unavailable: {error}",
        )

    plan = plan_managed_change(record, target)
    if plan.status is ManagedChangeStatus.INCONSISTENT:
        status = ForegroundUpdateStatus.INCONSISTENT
    elif (
        plan.status is ManagedChangeStatus.UPDATE_AVAILABLE
        and record.channel is ManagedChannel.STABLE
    ):
        status = ForegroundUpdateStatus.UPDATE_AVAILABLE
    elif plan.status is ManagedChangeStatus.UPDATE_AVAILABLE:
        status = ForegroundUpdateStatus.PROVENANCE_CHANGED
    else:
        status = ForegroundUpdateStatus.CURRENT
    return ForegroundUpdateObservation(
        status=status,
        current_channel=record.channel,
        current_version=record.release_version,
        target_version=plan.target_version,
        current_revision=record.source_revision,
        target_revision=plan.target_revision,
        network_attempted=True,
        detail=plan.detail,
    )


def _git_output(repository: Path, *arguments: str) -> str:
    import subprocess

    try:
        completed = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise ManagedInstallError(
            "active managed Git identity cannot be verified"
        ) from error
    return completed.stdout.strip()
