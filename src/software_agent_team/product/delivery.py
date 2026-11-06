"""Greenfield project initialization, workspace staging, and atomic delivery.

全新项目初始化、工作区暂存与原子化交付。
提供运行标识生成、目标路径安全校验、源码基线克隆、清单加载以及无覆盖风险的原子交付。
"""

from __future__ import annotations

import ctypes
import errno
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from software_agent_team.benchmark_seed import prepare_seed_repository
from software_agent_team.git_policy import (
    git_command,
    git_environment,
    repository_has_callbacks,
)

from .types import (
    _MAX_PROJECT_MANIFEST_BYTES,
    AT_FDCWD,
    PROJECT_MANIFEST_NAME,
    PROJECT_NAME_PATTERN,
    RENAME_NOREPLACE,
    ProductFlowError,
    ProductStatePaths,
    ProjectCommands,
)


def _run_command(
    argv: Sequence[str], timeout_seconds: float
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        check=False,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=timeout_seconds,
        shell=False,
    )


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _remove_stale_staging_targets(parent: Path, destination_name: str) -> None:
    """Remove any abandoned staging temporary directories from interrupted runs."""

    pattern = f".{destination_name}.sat-*.tmp"
    try:
        for candidate in parent.glob(pattern):
            if candidate.is_dir() and not candidate.is_symlink():
                shutil.rmtree(candidate, ignore_errors=True)
    except OSError:
        pass


def generate_product_run_id(
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    random_suffix: Callable[[], str] = lambda: uuid.uuid4().hex[:8],
) -> str:
    """Generate a sortable collision-resistant internal run identifier."""

    now = clock().astimezone(UTC)
    suffix = random_suffix().casefold()
    if re.fullmatch(r"[0-9a-f]{8}", suffix) is None:
        raise ProductFlowError("run ID suffix must contain eight hexadecimal digits")
    return f"sat-{now:%Y%m%d-%H%M%S}-{suffix}"


def validate_project_destination(parent: Path, project_name: str) -> Path:
    """Resolve one new direct child without accepting an overwrite target."""

    cleaned = project_name.strip()
    if PROJECT_NAME_PATTERN.fullmatch(cleaned) is None or cleaned in {".", ".."}:
        raise ProductFlowError(
            "project directory must use 1-64 letters, numbers, dots, dashes, or "
            "underscores and cannot contain a path separator"
        )
    try:
        resolved_parent = parent.resolve(strict=True)
    except OSError as error:
        raise ProductFlowError("project parent directory does not exist") from error
    if not resolved_parent.is_dir() or parent.is_symlink():
        raise ProductFlowError("project parent must be a real directory")
    destination = resolved_parent / cleaned
    if destination.exists() or destination.is_symlink():
        raise ProductFlowError(
            f"project destination already exists and will not be overwritten: "
            f"{destination}"
        )
    return destination


def prepare_product_source(
    *,
    seed: Path,
    state_paths: ProductStatePaths,
    run_id: str,
) -> Path:
    """Create the trusted greenfield source baseline for one confirmed run."""

    destination = state_paths.sources / run_id
    prepare_seed_repository(
        seed,
        destination,
        commit_message="chore: initialize software project",
    )
    return destination


def load_project_commands(project: Path) -> ProjectCommands:
    """Load the accepted project's bounded command contract."""

    manifest = project / PROJECT_MANIFEST_NAME
    if manifest.is_symlink() or not manifest.is_file():
        raise ProductFlowError(
            f"accepted project is missing a regular {PROJECT_MANIFEST_NAME}"
        )
    raw = manifest.read_bytes()
    if len(raw) > _MAX_PROJECT_MANIFEST_BYTES:
        raise ProductFlowError(f"{PROJECT_MANIFEST_NAME} is too large")

    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        payload = json.loads(raw, object_pairs_hook=reject_duplicate_keys)
        return ProjectCommands.model_validate(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ProductFlowError(
            f"accepted project has an invalid {PROJECT_MANIFEST_NAME}: {error}"
        ) from error


def _git_output(repository: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            git_command(["-C", str(repository), *arguments]),
            env=git_environment(repository),
            check=False,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=30,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ProductFlowError("could not verify the delivered Git result") from error
    if result.returncode != 0:
        raise ProductFlowError("could not verify the delivered Git result")
    return result.stdout.strip()


def _clone_git_result(source: Path, destination: Path) -> None:
    try:
        result = subprocess.run(
            git_command(
                [
                    "clone",
                    "--no-local",
                    "--no-checkout",
                    "--",
                    str(source),
                    str(destination),
                ]
            ),
            env=git_environment(source),
            check=False,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=30,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ProductFlowError("could not clone the accepted Git result") from error
    if result.returncode != 0:
        raise ProductFlowError("could not clone the accepted Git result")


def _rename_no_replace(source: Path, destination: Path) -> None:
    """Publish one directory atomically without replacing a late destination."""

    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise ProductFlowError(
            "this Linux runtime cannot publish the result without overwrite risk"
        )
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    result = renameat2(
        AT_FDCWD,
        os.fsencode(source),
        AT_FDCWD,
        os.fsencode(destination),
        RENAME_NOREPLACE,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise ProductFlowError(
            f"delivery destination appeared during the run: {destination}"
        )
    if error_number in {errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP}:
        raise ProductFlowError(
            "the destination filesystem cannot publish the result atomically "
            "without overwrite risk"
        )
    raise ProductFlowError(
        f"could not publish the accepted result: {os.strerror(error_number)}"
    )


_default_rename_no_replace = _rename_no_replace


def deliver_product_workspace(
    source: Path,
    destination: Path,
    *,
    expected_commit: str,
) -> Path:
    """Copy one accepted clean workspace into a new project directory atomically."""

    try:
        resolved_source = source.resolve(strict=True)
        resolved_parent = destination.parent.resolve(strict=True)
    except OSError as error:
        raise ProductFlowError(
            "delivery source or destination parent is unavailable"
        ) from error
    if not resolved_source.is_dir() or source.is_symlink():
        raise ProductFlowError("accepted workspace must be a real directory")
    if not resolved_parent.is_dir() or destination.parent.is_symlink():
        raise ProductFlowError("delivery parent must be a real directory")
    if destination.exists() or destination.is_symlink():
        raise ProductFlowError(
            f"delivery destination appeared during the run: {destination}"
        )
    if resolved_parent == resolved_source or resolved_parent.is_relative_to(
        resolved_source
    ):
        raise ProductFlowError("delivery destination cannot be inside the workspace")
    try:
        callbacks = repository_has_callbacks(resolved_source)
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise ProductFlowError(
            "could not verify accepted workspace Git policy"
        ) from error
    if callbacks:
        raise ProductFlowError("accepted workspace contains unsafe Git callbacks")
    if (
        _git_output(resolved_source, "rev-parse", f"{expected_commit}^{{commit}}")
        != expected_commit
    ):
        raise ProductFlowError("accepted commit is unavailable before delivery")
    if _git_output(resolved_source, "remote"):
        raise ProductFlowError("accepted workspace unexpectedly contains a remote")
    tree = _git_output(
        resolved_source,
        "ls-tree",
        "-r",
        "--full-tree",
        expected_commit,
    )
    if any(line.startswith("120000 ") for line in tree.splitlines()):
        raise ProductFlowError("accepted commit contains a symbolic link")

    _remove_stale_staging_targets(resolved_parent, destination.name)
    staging = resolved_parent / f".{destination.name}.sat-{uuid.uuid4().hex}.tmp"
    try:
        _clone_git_result(resolved_source, staging)
        _git_output(staging, "remote", "remove", "origin")
        _git_output(staging, "switch", "-C", "main", expected_commit)
        if _git_output(staging, "rev-parse", "HEAD") != expected_commit:
            raise ProductFlowError("staged delivery changed the accepted commit")
        if _git_output(staging, "status", "--porcelain=v1", "--untracked-files=all"):
            raise ProductFlowError("staged delivery is not clean")

        product_mod = sys.modules.get("software_agent_team.product")
        product_publish = (
            getattr(product_mod, "_rename_no_replace", None) if product_mod else None
        )
        current_delivery_publish = globals().get("_rename_no_replace")
        if (
            product_publish is not None
            and product_publish is not _default_rename_no_replace
        ):
            publish_fn = product_publish
        elif current_delivery_publish is not None:
            publish_fn = current_delivery_publish
        else:
            publish_fn = _rename_no_replace
        publish_fn(staging, destination)

        descriptor = os.open(resolved_parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return destination
