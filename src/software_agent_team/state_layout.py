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


class StateLayoutOperation(StrEnum):
    """The caller operation one layout problem must be remediated for.

    Layout inspection has exactly one owner, but its callers want opposite
    outcomes: a first run needs a usable state root, an uninstall needs the
    existing one removed. Remediation is therefore projected per operation
    instead of being stored once with the problem.
    """

    FIRST_RUN = "first_run"
    UNINSTALL = "uninstall"


class StateLayoutProblemKind(StrEnum):
    """Machine-decidable reason one SAT state layout is unsafe to consume."""

    ROOT_NOT_SPECIFIC = "root_not_specific"
    ROOT_UNREADABLE = "root_unreadable"
    ROOT_NOT_DIRECTORY = "root_not_directory"
    ROOT_UNRESOLVABLE = "root_unresolvable"
    ROOT_UNLISTABLE = "root_unlistable"
    OWNERSHIP_FOREIGN = "ownership_foreign"
    ACCESS_DENIED = "access_denied"
    MARKER_MISSING = "marker_missing"
    MARKER_UNAVAILABLE = "marker_unavailable"
    MARKER_INVALID = "marker_invalid"
    UNKNOWN_CATEGORY = "unknown_category"
    CATEGORY_UNREADABLE = "category_unreadable"
    CATEGORY_NOT_DIRECTORY = "category_not_directory"


_FIRST_RUN_REMEDIATIONS: dict[StateLayoutProblemKind, str] = {
    StateLayoutProblemKind.ROOT_NOT_SPECIFIC: (
        "Select a specific absolute SAT state root and retry."
    ),
    StateLayoutProblemKind.ROOT_UNREADABLE: (
        "Restore access to this exact path, then run SAT again."
    ),
    StateLayoutProblemKind.ROOT_NOT_DIRECTORY: (
        "Select a real SAT state directory; do not redirect it through a symbolic link."
    ),
    StateLayoutProblemKind.ROOT_UNRESOLVABLE: (
        "Restore access to this exact path, then run SAT again."
    ),
    StateLayoutProblemKind.ROOT_UNLISTABLE: (
        "Restore access to this exact path, then run SAT again."
    ),
    StateLayoutProblemKind.OWNERSHIP_FOREIGN: (
        "Have the operating-system administrator restore this exact SAT path to "
        "the invoking user, then retry."
    ),
    StateLayoutProblemKind.ACCESS_DENIED: (
        "Restore invoking-user write and search access to this exact SAT path, "
        "then retry."
    ),
    StateLayoutProblemKind.MARKER_MISSING: (
        "Select a new empty SAT_STATE_ROOT, or restore the complete matching "
        "SAT-owned state root, then retry."
    ),
    StateLayoutProblemKind.MARKER_UNAVAILABLE: (
        "Restore the matching SAT state marker, or select a new empty "
        "SAT_STATE_ROOT, then retry."
    ),
    StateLayoutProblemKind.MARKER_INVALID: (
        "Restore the matching invoking-user-owned SAT marker, or select a new "
        "empty SAT_STATE_ROOT, then retry."
    ),
    StateLayoutProblemKind.UNKNOWN_CATEGORY: (
        "Stop every SAT task, then move this entry to a new path outside the "
        "SAT state root before retrying: {path}"
    ),
    StateLayoutProblemKind.CATEGORY_UNREADABLE: (
        "Restore access to this exact path, then run SAT again."
    ),
    StateLayoutProblemKind.CATEGORY_NOT_DIRECTORY: (
        "Restore this category as a real directory inside the SAT state root, "
        "then retry."
    ),
}

_UNINSTALL_REMEDIATIONS: dict[StateLayoutProblemKind, str] = {
    StateLayoutProblemKind.ROOT_NOT_SPECIFIC: (
        "Point SAT_STATE_ROOT at the specific absolute state root to remove, "
        "then run uninstall again."
    ),
    StateLayoutProblemKind.ROOT_UNREADABLE: (
        "Restore access to this exact path, then run uninstall again."
    ),
    StateLayoutProblemKind.ROOT_NOT_DIRECTORY: (
        "SAT removes only a real state directory; remove or relocate this path "
        "yourself."
    ),
    StateLayoutProblemKind.ROOT_UNRESOLVABLE: (
        "Restore access to this exact path, then run uninstall again."
    ),
    StateLayoutProblemKind.ROOT_UNLISTABLE: (
        "Restore access to this exact path, then run uninstall again."
    ),
    StateLayoutProblemKind.OWNERSHIP_FOREIGN: (
        "Have the operating-system administrator restore this exact SAT path to "
        "the invoking user, or remove it as its owner, then run uninstall again."
    ),
    StateLayoutProblemKind.ACCESS_DENIED: (
        "Restore invoking-user write and search access to this exact SAT path, "
        "then run uninstall again."
    ),
    StateLayoutProblemKind.MARKER_MISSING: (
        "SAT does not remove a state root it cannot prove it owns. Remove this "
        "directory yourself if it is not SAT state, or restore its SAT marker, "
        "then run uninstall again."
    ),
    StateLayoutProblemKind.MARKER_UNAVAILABLE: (
        "Restore the matching SAT state marker, or remove this directory "
        "yourself, then run uninstall again."
    ),
    StateLayoutProblemKind.MARKER_INVALID: (
        "Restore the matching invoking-user-owned SAT marker, or remove this "
        "directory yourself, then run uninstall again."
    ),
    StateLayoutProblemKind.UNKNOWN_CATEGORY: (
        "Stop every SAT task, then move this entry to a new path outside the "
        "SAT state root before running uninstall again: {path}"
    ),
    StateLayoutProblemKind.CATEGORY_UNREADABLE: (
        "Restore access to this exact path, then run uninstall again."
    ),
    StateLayoutProblemKind.CATEGORY_NOT_DIRECTORY: (
        "Restore this category as a real directory inside the SAT state root, "
        "or remove it yourself, then run uninstall again."
    ),
}

_REMEDIATIONS: dict[StateLayoutOperation, dict[StateLayoutProblemKind, str]] = {
    StateLayoutOperation.FIRST_RUN: _FIRST_RUN_REMEDIATIONS,
    StateLayoutOperation.UNINSTALL: _UNINSTALL_REMEDIATIONS,
}


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
    kind: StateLayoutProblemKind
    detail: str

    def remediation(self, operation: StateLayoutOperation) -> str:
        """Project this problem's next step for one caller operation."""

        return _REMEDIATIONS[operation][self.kind].format(path=self.path)


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


def describe_state_layout_failure(
    observation: StateLayoutObservation,
    *,
    operation: StateLayoutOperation,
    separator: str = " ({count} additional problem(s))",
) -> tuple[str, str]:
    """Return one bounded failure detail and its operation-specific next step."""

    first = observation.problems[0]
    additional = len(observation.problems) - 1
    suffix = separator.format(count=additional) if additional else ""
    return f"{first.detail}{suffix}", first.remediation(operation)


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
                kind=StateLayoutProblemKind.ROOT_NOT_SPECIFIC,
                detail="SAT state root must be a specific absolute path",
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
                kind=StateLayoutProblemKind.ROOT_UNREADABLE,
                detail=f"SAT state root cannot be inspected: {root}",
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
                kind=StateLayoutProblemKind.ROOT_NOT_DIRECTORY,
                detail=f"SAT state root is not a real directory: {root}",
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
                kind=StateLayoutProblemKind.ROOT_UNRESOLVABLE,
                detail=f"SAT state root cannot be resolved: {root}",
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
                kind=StateLayoutProblemKind.MARKER_MISSING,
                detail=f"existing state root is not owned by SAT: {root}",
            )
        )
    except OSError:
        problems.append(
            StateLayoutProblem(
                path=marker,
                kind=StateLayoutProblemKind.MARKER_UNAVAILABLE,
                detail=f"SAT state ownership marker is unavailable: {marker}",
            )
        )
    else:
        marker_valid = (
            stat.S_ISREG(marker_metadata.st_mode) and marker_metadata.st_uid == uid
        )
        try:
            marker_content = marker.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            marker_content = None
        if not marker_valid or marker_content != expected_marker:
            problems.append(
                StateLayoutProblem(
                    path=marker,
                    kind=StateLayoutProblemKind.MARKER_INVALID,
                    detail=(
                        "SAT state ownership marker is invalid "
                        f"(observed uid={marker_metadata.st_uid}, expected uid={uid}): "
                        f"{marker}"
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
                kind=StateLayoutProblemKind.ROOT_UNLISTABLE,
                detail=f"SAT state root cannot be listed: {root}",
            )
        )
    for entry in sorted(entries, key=lambda item: item.name):
        if entry.name in known_names or entry.name == STATE_MARKER_NAME:
            continue
        problems.append(
            StateLayoutProblem(
                path=entry,
                kind=StateLayoutProblemKind.UNKNOWN_CATEGORY,
                detail=(
                    "SAT state contains an unknown lifecycle category: "
                    f"{entry.name} ({entry})"
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
                    kind=StateLayoutProblemKind.CATEGORY_UNREADABLE,
                    detail=(
                        f"SAT {category.directory_name} state cannot be inspected: "
                        f"{path}"
                    ),
                )
            )
            continue
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            problems.append(
                StateLayoutProblem(
                    path=path,
                    kind=StateLayoutProblemKind.CATEGORY_NOT_DIRECTORY,
                    detail=(
                        f"SAT {category.directory_name} state must be a real "
                        f"directory: {path}"
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
                kind=StateLayoutProblemKind.OWNERSHIP_FOREIGN,
                detail=(
                    f"{label} belongs to uid {metadata.st_uid}; invoking uid is "
                    f"{invoking_uid}: {path}"
                ),
            )
        )
    elif not os.access(path, os.W_OK | os.X_OK):
        problems.append(
            StateLayoutProblem(
                path=path,
                kind=StateLayoutProblemKind.ACCESS_DENIED,
                detail=(
                    f"{label} is not writable and searchable by uid {invoking_uid}: "
                    f"{path}"
                ),
            )
        )
