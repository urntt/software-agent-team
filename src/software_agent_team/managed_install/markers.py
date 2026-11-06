"""Ownership marker reading, writing, and lifecycle root verification.

Validates application boundaries, verifies SAT root layout invariants,
and manages atomic persistence of marker files.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from pathlib import Path
from uuid import uuid4

from software_agent_team.managed_install.constants import (
    MANAGED_MARKER_NAME,
    MANAGED_ROOT_MARKER_NAME,
    ManagedInstallError,
)
from software_agent_team.managed_install.models import (
    ManagedApplicationMarker,
    ManagedInstallPaths,
    ManagedRootMarker,
    _expected_root_marker,
)
from software_agent_team.managed_install.utils import (
    _fsync_directory,
    _require_real_directory,
)
from software_agent_team.versioning import load_installation_record


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
    """Write an application marker atomically with exclusive file permissions."""
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
    """Write a root ownership marker atomically with exclusive permissions."""
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


def _ensure_managed_root(paths: ManagedInstallPaths) -> None:
    """Initialize or verify the managed root marker before lifecycle actions."""
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
    """Validate that the managed application root exists and has an expected marker."""
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
    """Check if an unmanaged root qualifies as a migratable legacy SAT install."""
    if paths.application_link.parent != paths.managed_root:
        return False
    allowed = {paths.application_link}
    if paths.installation_record.parent == paths.managed_root:
        allowed.add(paths.installation_record)
    if any(child not in allowed for child in children):
        return False
    return _is_legacy_owned_application(paths.application_link)


def _is_legacy_owned_application(application: Path) -> bool:
    """Detect whether an application was created by a legacy SAT v1 install."""
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
    """Check that destination paths and launchers do not collide with alien software."""
    from software_agent_team import managed_install

    _require_managed_root(paths)
    application = paths.application_link
    if application.is_symlink():
        try:
            active = application.resolve(strict=True)
        except OSError as error:
            raise ManagedInstallError("managed application link is broken") from error
        managed_install._require_managed_release_target(active, paths)
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


def _managed_root_for_release(
    release_marker: ManagedApplicationMarker,
) -> tuple[Path, ManagedRootMarker]:
    """Resolve the one lifecycle root named by a release marker."""
    application_link = Path(release_marker.application_link)
    roots: list[tuple[Path, ManagedRootMarker]] = []
    for candidate in (
        application_link.parent,
        application_link.parent / f".{application_link.name}.sat-managed",
    ):
        marker_path = candidate / MANAGED_ROOT_MARKER_NAME
        if not marker_path.exists() and not marker_path.is_symlink():
            continue
        marker = load_managed_root_marker(marker_path)
        if (
            Path(marker.managed_root) == candidate
            and Path(marker.application_link) == application_link
        ):
            roots.append((candidate, marker))
    if len(roots) != 1:
        raise ManagedInstallError(
            "managed release does not resolve to one owned lifecycle root"
        )
    return roots[0]
