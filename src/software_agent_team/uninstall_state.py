"""Preservation-first export and removal of SAT-owned user state."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from software_agent_team.process_lifecycle import (
    ProcessLeaseStore,
    ProcessLifecycleError,
)
from software_agent_team.product import STATE_MARKER_NAME, ProductStatePaths
from software_agent_team.state_layout import (
    PRODUCT_STATE_CATEGORIES,
    StateLifecycleGroup,
)


class UninstallStateError(RuntimeError):
    """Raised when state cannot be exported or removed within its authority."""


class UninstallPolicy(StrEnum):
    """One explicit user decision for a persisted state group."""

    KEEP = "keep"
    PURGE = "purge"


@dataclass(frozen=True)
class UninstallStateRequest:
    """The complete state choices for one uninstall transaction."""

    state_root: Path
    config_path: Path
    config_policy: UninstallPolicy
    data_policy: UninstallPolicy
    provider_policy: UninstallPolicy
    export_to: Path | None = None


@dataclass(frozen=True)
class UninstallStateResult:
    """User-visible facts from one applied state transaction."""

    messages: tuple[str, ...]
    state_root_removed: bool


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _require_specific_absolute(path: Path, *, label: str) -> None:
    if not path.is_absolute() or path == Path(path.anchor):
        raise UninstallStateError(f"{label} must be a specific absolute path")
    if any(ord(character) < 32 for character in str(path)):
        raise UninstallStateError(f"{label} contains control text")
    if Path(os.path.normpath(str(path))) != path:
        raise UninstallStateError(f"{label} must be normalized")


def _require_owned_real_directory(path: Path, *, label: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise UninstallStateError(f"{label} cannot be inspected: {path}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise UninstallStateError(f"{label} must be a real directory: {path}")
    if metadata.st_uid != os.geteuid():
        raise UninstallStateError(f"{label} must belong to the invoking user: {path}")
    return metadata


def _validate_configuration(path: Path) -> None:
    _require_specific_absolute(path, label="configuration path")
    if not _lexists(path):
        return
    try:
        metadata = path.lstat()
    except OSError as error:
        raise UninstallStateError("configuration cannot be inspected") from error
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
    ):
        raise UninstallStateError(
            "configuration must be an invoking-user-owned regular file"
        )


def _validate_export_destination(request: UninstallStateRequest) -> None:
    destination = request.export_to
    if destination is None:
        return
    _require_specific_absolute(destination, label="export destination")
    if _lexists(destination):
        raise UninstallStateError(f"export destination already exists: {destination}")
    parent = destination.parent
    _require_owned_real_directory(parent, label="export destination parent")
    if parent.resolve(strict=True) != parent:
        raise UninstallStateError("export destination parent must be canonical")
    resolved_state = request.state_root.resolve(strict=False)
    if destination == resolved_state or destination.is_relative_to(resolved_state):
        raise UninstallStateError("export destination must be outside SAT state")


def _validate_run_liveness(paths: ProductStatePaths) -> None:
    if not paths.runs.exists():
        return
    try:
        run_entries = sorted(paths.runs.iterdir(), key=lambda item: item.name)
    except OSError as error:
        raise UninstallStateError("SAT run state cannot be listed") from error
    for run_entry in run_entries:
        try:
            run_metadata = run_entry.lstat()
        except OSError as error:
            raise UninstallStateError(
                f"run state entry cannot be inspected: {run_entry}"
            ) from error
        if stat.S_ISLNK(run_metadata.st_mode):
            raise UninstallStateError(
                f"run state entry must not be a symbolic link: {run_entry}"
            )
        if not stat.S_ISDIR(run_metadata.st_mode):
            continue
        state_path = run_entry / "run.json"
        if not _lexists(state_path):
            continue
        try:
            metadata = state_path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise UninstallStateError(
                    f"run state must be a regular file: {state_path}"
                )
            payload = json.loads(state_path.read_text(encoding="utf-8"))
            phase = payload["phase"]
        except UninstallStateError:
            raise
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
        ) as error:
            raise UninstallStateError(
                f"run state cannot be verified: {state_path}"
            ) from error
        if phase not in {"completed", "failed"}:
            raise UninstallStateError(
                f"active SAT run blocks uninstall: {run_entry.name}"
            )


def _validate_process_liveness(paths: ProductStatePaths) -> None:
    try:
        observation = ProcessLeaseStore(paths.process_leases).inspect()
    except ProcessLifecycleError as error:
        raise UninstallStateError(
            "SAT process lease state cannot be verified"
        ) from error
    if observation.active:
        run_ids = sorted({item.lease.run_id for item in observation.active})
        raise UninstallStateError(
            "active SAT provider process blocks uninstall: " + ", ".join(run_ids)
        )
    if observation.orphaned:
        run_ids = sorted({item.lease.run_id for item in observation.orphaned})
        raise UninstallStateError(
            "orphaned SAT provider process requires `sat cleanup --orphans`: "
            + ", ".join(run_ids)
        )


def preflight_uninstall_state(request: UninstallStateRequest) -> ProductStatePaths:
    """Validate every state and liveness boundary without changing it."""

    _require_specific_absolute(request.state_root, label="SAT state root")
    _validate_configuration(request.config_path)
    _validate_export_destination(request)
    paths = ProductStatePaths.below(request.state_root)
    if not _lexists(paths.root):
        return paths
    _require_owned_real_directory(paths.root, label="SAT state root")
    resolved_root = paths.root.resolve(strict=True)
    if resolved_root != paths.root:
        raise UninstallStateError("SAT state root must be canonical")

    marker = paths.root / STATE_MARKER_NAME
    try:
        marker_metadata = marker.lstat()
        expected = f"software-agent-team-state-v1\nroot={resolved_root}\n"
        content = marker.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise UninstallStateError(
            "SAT state ownership marker is unavailable"
        ) from error
    if (
        stat.S_ISLNK(marker_metadata.st_mode)
        or not stat.S_ISREG(marker_metadata.st_mode)
        or marker_metadata.st_uid != os.geteuid()
        or content != expected
    ):
        raise UninstallStateError("SAT state ownership marker is invalid")

    known_names = {category.directory_name for category in PRODUCT_STATE_CATEGORIES}
    try:
        actual_names = {entry.name for entry in paths.root.iterdir()}
    except OSError as error:
        raise UninstallStateError("SAT state root cannot be listed") from error
    unknown = actual_names - known_names - {STATE_MARKER_NAME}
    if unknown:
        raise UninstallStateError(
            "SAT state contains an unknown lifecycle category: "
            + ", ".join(sorted(unknown))
        )
    for category in PRODUCT_STATE_CATEGORIES:
        path = getattr(paths, category.attribute)
        if _lexists(path):
            _require_owned_real_directory(
                path,
                label=f"SAT {category.directory_name} state",
            )
    _validate_run_liveness(paths)
    _validate_process_liveness(paths)
    return paths


def _copy_export(
    request: UninstallStateRequest,
    paths: ProductStatePaths,
) -> None:
    destination = request.export_to
    if destination is None:
        return
    staging = destination.parent / f".{destination.name}.sat-export-{uuid4().hex}"
    try:
        staging.mkdir(mode=0o700)
        exported: dict[str, bool] = {}
        if request.config_path.is_file():
            configuration = staging / "configuration"
            configuration.mkdir(mode=0o700)
            shutil.copy2(request.config_path, configuration / "config.json")
            exported["configuration"] = True
        else:
            exported["configuration"] = False
        data = staging / "data"
        for category in PRODUCT_STATE_CATEGORIES:
            if not category.exported:
                continue
            source = getattr(paths, category.attribute)
            present = source.is_dir()
            exported[category.attribute] = present
            if present:
                data.mkdir(mode=0o700, exist_ok=True)
                assert category.export_name is not None
                shutil.copytree(
                    source,
                    data / category.export_name,
                    symlinks=True,
                )
        lines = [
            "Software Agent Team uninstall export",
            f"created_utc={datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}",
            *(
                f"{name}={'yes' if present else 'no'}"
                for name, present in exported.items()
            ),
            "process_leases=excluded",
            "provider_credentials=excluded",
            "custom_run_roots=excluded",
        ]
        manifest = staging / "EXPORT.txt"
        manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
        manifest.chmod(0o600)
        if _lexists(destination):
            raise UninstallStateError(
                f"export destination appeared during export: {destination}"
            )
        staging.rename(destination)
    except UninstallStateError:
        raise
    except OSError as error:
        raise UninstallStateError("SAT state export could not complete") from error
    finally:
        if _lexists(staging):
            shutil.rmtree(staging)


def apply_uninstall_state(request: UninstallStateRequest) -> UninstallStateResult:
    """Export, then apply exact user choices after a complete fresh preflight."""

    paths = preflight_uninstall_state(request)
    _copy_export(request, paths)
    messages: list[str] = []
    if request.export_to is not None:
        messages.append(f"uninstall: exported preserved state to {request.export_to}")

    if request.config_policy is UninstallPolicy.PURGE:
        try:
            request.config_path.unlink(missing_ok=True)
        except OSError as error:
            raise UninstallStateError(
                "SAT configuration could not be deleted"
            ) from error
        messages.append(f"uninstall: deleted SAT configuration {request.config_path}")
    else:
        messages.append(f"uninstall: preserved SAT configuration {request.config_path}")

    data_categories = tuple(
        category
        for category in PRODUCT_STATE_CATEGORIES
        if category.lifecycle_group is StateLifecycleGroup.DATA
    )
    if request.data_policy is UninstallPolicy.PURGE:
        for category in data_categories:
            path = getattr(paths, category.attribute)
            if _lexists(path):
                try:
                    shutil.rmtree(path)
                except OSError as error:
                    raise UninstallStateError(
                        f"SAT {category.directory_name} state could not be deleted"
                    ) from error
        messages.append(
            "uninstall: deleted runs, workspaces, sources, Planning, and "
            "self-check evidence"
        )
    else:
        messages.append(
            "uninstall: preserved runs, workspaces, sources, Planning, and "
            "self-check evidence"
        )

    if _lexists(paths.process_leases):
        try:
            shutil.rmtree(paths.process_leases)
        except OSError as error:
            raise UninstallStateError(
                "inactive SAT process lease state could not be deleted"
            ) from error
    if request.provider_policy is UninstallPolicy.PURGE:
        if _lexists(paths.openclaw):
            try:
                shutil.rmtree(paths.openclaw)
            except OSError as error:
                raise UninstallStateError(
                    "SAT isolated provider state could not be deleted"
                ) from error
        messages.append("uninstall: deleted SAT's isolated OpenClaw provider state")
    else:
        messages.append("uninstall: preserved SAT's isolated OpenClaw provider state")

    state_root_removed = False
    if paths.root.exists():
        try:
            remaining = {entry.name for entry in paths.root.iterdir()}
        except OSError as error:
            raise UninstallStateError("SAT state root cannot be finalized") from error
        if remaining == {STATE_MARKER_NAME}:
            try:
                (paths.root / STATE_MARKER_NAME).unlink()
                paths.root.rmdir()
            except OSError as error:
                raise UninstallStateError(
                    "empty SAT state root could not be deleted"
                ) from error
            state_root_removed = True
    return UninstallStateResult(
        messages=tuple(messages),
        state_root_removed=state_root_removed,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("action", choices=("preflight", "apply"))
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--config-path", type=Path, required=True)
    parser.add_argument(
        "--config-policy", choices=tuple(UninstallPolicy), required=True
    )
    parser.add_argument("--data-policy", choices=tuple(UninstallPolicy), required=True)
    parser.add_argument(
        "--provider-policy",
        choices=tuple(UninstallPolicy),
        required=True,
    )
    parser.add_argument("--export-to", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the private uninstall state boundary."""

    try:
        arguments = _parser().parse_args(argv)
        request = UninstallStateRequest(
            state_root=arguments.state_root,
            config_path=arguments.config_path,
            config_policy=UninstallPolicy(arguments.config_policy),
            data_policy=UninstallPolicy(arguments.data_policy),
            provider_policy=UninstallPolicy(arguments.provider_policy),
            export_to=arguments.export_to,
        )
        if arguments.action == "preflight":
            preflight_uninstall_state(request)
        else:
            result = apply_uninstall_state(request)
            for message in result.messages:
                print(message)
    except (UninstallStateError, ValueError) as error:
        print(f"uninstall state: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
