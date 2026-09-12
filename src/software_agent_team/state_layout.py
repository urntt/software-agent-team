"""Authoritative lifecycle manifest for SAT-owned user state."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

STATE_MARKER_NAME = ".sat-state-v1"


class StateLifecycleGroup(StrEnum):
    """The user-visible lifecycle authority for one state category."""

    DATA = "data"
    PROVIDER = "provider"
    EPHEMERAL = "ephemeral"


@dataclass(frozen=True)
class ProductStateCategory:
    """One owned state directory and its export/removal semantics."""

    attribute: str
    directory_name: str
    lifecycle_group: StateLifecycleGroup
    export_name: str | None

    @property
    def exported(self) -> bool:
        return self.export_name is not None


@dataclass(frozen=True)
class StateLayoutProblem:
    """One exact reason an existing SAT state layout is unsafe to consume."""

    path: Path
    detail: str
    remediation: str


@dataclass(frozen=True)
class StateLayoutObservation:
    """Read-only ownership and shape evidence for the complete SAT state root."""

    root: Path
    invoking_uid: int
    initialized: bool
    problems: tuple[StateLayoutProblem, ...]

    @property
    def ready(self) -> bool:
        return not self.problems


PRODUCT_STATE_CATEGORIES = (
    ProductStateCategory(
        attribute="runs",
        directory_name="runs",
        lifecycle_group=StateLifecycleGroup.DATA,
        export_name="runs",
    ),
    ProductStateCategory(
        attribute="workspaces",
        directory_name="workspaces",
        lifecycle_group=StateLifecycleGroup.DATA,
        export_name="workspaces",
    ),
    ProductStateCategory(
        attribute="sources",
        directory_name="sources",
        lifecycle_group=StateLifecycleGroup.DATA,
        export_name="sources",
    ),
    ProductStateCategory(
        attribute="planning",
        directory_name="planning",
        lifecycle_group=StateLifecycleGroup.DATA,
        export_name="planning",
    ),
    ProductStateCategory(
        attribute="self_checks",
        directory_name="self-checks",
        lifecycle_group=StateLifecycleGroup.DATA,
        export_name="self-checks",
    ),
    ProductStateCategory(
        attribute="process_leases",
        directory_name="process-leases",
        lifecycle_group=StateLifecycleGroup.EPHEMERAL,
        export_name=None,
    ),
    ProductStateCategory(
        attribute="openclaw",
        directory_name="openclaw",
        lifecycle_group=StateLifecycleGroup.PROVIDER,
        export_name=None,
    ),
)


def state_category_paths(root: Path) -> dict[str, Path]:
    """Resolve every manifest category beneath one state root."""

    return {
        category.attribute: root / category.directory_name
        for category in PRODUCT_STATE_CATEGORIES
    }


def inspect_state_layout(
    root: Path,
    *,
    invoking_uid: int | None = None,
) -> StateLayoutObservation:
    """Inspect state ownership without changing permissions or following links."""

    uid = os.geteuid() if invoking_uid is None else invoking_uid
    problems: list[StateLayoutProblem] = []
    if not root.is_absolute() or root == Path(root.anchor):
        problems.append(
            StateLayoutProblem(
                path=root,
                detail="SAT state root must be a specific absolute path",
                remediation="Select a specific absolute SAT state root and retry.",
            )
        )
        return StateLayoutObservation(
            root=root,
            invoking_uid=uid,
            initialized=False,
            problems=tuple(problems),
        )
    if not os.path.lexists(root):
        return StateLayoutObservation(
            root=root,
            invoking_uid=uid,
            initialized=False,
            problems=(),
        )

    try:
        root_metadata = root.lstat()
    except OSError:
        problems.append(
            StateLayoutProblem(
                path=root,
                detail=f"SAT state root cannot be inspected: {root}",
                remediation="Restore access to this exact path, then run SAT again.",
            )
        )
        return StateLayoutObservation(
            root=root,
            invoking_uid=uid,
            initialized=True,
            problems=tuple(problems),
        )
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        problems.append(
            StateLayoutProblem(
                path=root,
                detail=f"SAT state root is not a real directory: {root}",
                remediation=(
                    "Select a real SAT state directory; do not redirect it through a "
                    "symbolic link."
                ),
            )
        )
        return StateLayoutObservation(
            root=root,
            invoking_uid=uid,
            initialized=True,
            problems=tuple(problems),
        )
    _append_owner_or_access_problem(
        problems,
        path=root,
        metadata=root_metadata,
        invoking_uid=uid,
        label="SAT state root",
    )

    try:
        resolved_root = root.resolve(strict=True)
    except OSError:
        resolved_root = root
        problems.append(
            StateLayoutProblem(
                path=root,
                detail=f"SAT state root cannot be resolved: {root}",
                remediation="Restore access to this exact path, then run SAT again.",
            )
        )

    marker = root / STATE_MARKER_NAME
    expected_marker = f"software-agent-team-state-v1\nroot={resolved_root}\n"
    try:
        marker_metadata = marker.lstat()
    except FileNotFoundError:
        problems.append(
            StateLayoutProblem(
                path=root,
                detail=f"existing state root is not owned by SAT: {root}",
                remediation=(
                    "Select a new empty SAT_STATE_ROOT, or restore the complete "
                    "matching SAT-owned state root, then retry."
                ),
            )
        )
    except OSError:
        problems.append(
            StateLayoutProblem(
                path=marker,
                detail=f"SAT state ownership marker is unavailable: {marker}",
                remediation=(
                    "Restore the matching SAT state marker, or select a new empty "
                    "SAT_STATE_ROOT, then retry."
                ),
            )
        )
    else:
        marker_valid = (
            stat.S_ISREG(marker_metadata.st_mode)
            and not stat.S_ISLNK(marker_metadata.st_mode)
            and marker_metadata.st_uid == uid
        )
        try:
            marker_content = marker.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            marker_content = None
        if not marker_valid or marker_content != expected_marker:
            problems.append(
                StateLayoutProblem(
                    path=marker,
                    detail=(
                        "SAT state ownership marker is invalid "
                        f"(observed uid={marker_metadata.st_uid}, expected uid={uid}): "
                        f"{marker}"
                    ),
                    remediation=(
                        "Restore the matching invoking-user-owned SAT marker, or "
                        "select a new empty SAT_STATE_ROOT, then retry."
                    ),
                )
            )

    known_names = {category.directory_name for category in PRODUCT_STATE_CATEGORIES}
    try:
        entries = tuple(root.iterdir())
    except OSError:
        entries = ()
        problems.append(
            StateLayoutProblem(
                path=root,
                detail=f"SAT state root cannot be listed: {root}",
                remediation="Restore access to this exact path, then run SAT again.",
            )
        )
    for entry in sorted(entries, key=lambda item: item.name):
        if entry.name in known_names or entry.name == STATE_MARKER_NAME:
            continue
        problems.append(
            StateLayoutProblem(
                path=entry,
                detail=(
                    "SAT state contains an unknown lifecycle category: "
                    f"{entry.name} ({entry})"
                ),
                remediation=(
                    "Stop every SAT task, then move this entry to a new path outside "
                    f"the SAT state root before retrying: {entry}"
                ),
            )
        )

    for category in PRODUCT_STATE_CATEGORIES:
        path = root / category.directory_name
        if not os.path.lexists(path):
            continue
        try:
            metadata = path.lstat()
        except OSError:
            problems.append(
                StateLayoutProblem(
                    path=path,
                    detail=(
                        f"SAT {category.directory_name} state cannot be inspected: "
                        f"{path}"
                    ),
                    remediation=(
                        "Restore access to this exact path, then run SAT again."
                    ),
                )
            )
            continue
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            problems.append(
                StateLayoutProblem(
                    path=path,
                    detail=(
                        f"SAT {category.directory_name} state must be a real "
                        f"directory: {path}"
                    ),
                    remediation=(
                        "Restore this category as a real directory inside the SAT "
                        "state root, then retry."
                    ),
                )
            )
            continue
        _append_owner_or_access_problem(
            problems,
            path=path,
            metadata=metadata,
            invoking_uid=uid,
            label=f"SAT {category.directory_name} state",
        )

    return StateLayoutObservation(
        root=root,
        invoking_uid=uid,
        initialized=True,
        problems=tuple(problems),
    )


def _append_owner_or_access_problem(
    problems: list[StateLayoutProblem],
    *,
    path: Path,
    metadata: os.stat_result,
    invoking_uid: int,
    label: str,
) -> None:
    if metadata.st_uid != invoking_uid:
        problems.append(
            StateLayoutProblem(
                path=path,
                detail=(
                    f"{label} belongs to uid {metadata.st_uid}; invoking uid is "
                    f"{invoking_uid}: {path}"
                ),
                remediation=(
                    "Have the operating-system administrator restore this exact SAT "
                    "path to the invoking user, then retry."
                ),
            )
        )
    elif not os.access(path, os.W_OK | os.X_OK):
        problems.append(
            StateLayoutProblem(
                path=path,
                detail=(
                    f"{label} is not writable and searchable by uid {invoking_uid}: "
                    f"{path}"
                ),
                remediation=(
                    "Restore invoking-user write and search access to this exact SAT "
                    "path, then retry."
                ),
            )
        )
