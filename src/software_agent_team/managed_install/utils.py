"""Low-level filesystem operations, flock lifecycle locks, and command execution.

Provides transactional file manipulation with fsync, cross-platform flock primitives,
and bounded subprocess runners with Git policy enforcement.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from software_agent_team.git_policy import git_command, git_environment
from software_agent_team.managed_install.constants import ManagedInstallError
from software_agent_team.managed_install.models import ManagedInstallPaths

CommandRunner = Callable[[Sequence[str], Path | None, Mapping[str, str] | None], None]


def _fsync_directory(path: Path) -> None:
    """Flush directory entry metadata to disk."""
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _require_real_directory(path: Path, label: str) -> None:
    """Assert that a path is a true non-symlinked directory."""
    if path.is_symlink() or not path.is_dir():
        raise ManagedInstallError(f"{label} must be a real directory: {path}")


def _read_optional_bytes(path: Path) -> bytes | None:
    """Read regular file contents safely, rejecting symbolic links."""
    if path.is_symlink():
        raise ManagedInstallError("installation record must not be a symbolic link")
    if not path.exists():
        return None
    if not path.is_file():
        raise ManagedInstallError("installation record must be a regular file")
    return path.read_bytes()


def _restore_optional_bytes(path: Path, content: bytes | None) -> None:
    """Safely replace or delete a file with atomic rollback semantics and fsync."""
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


@contextmanager
def _lifecycle_file_lock(path: Path, *, shared: bool):
    """Acquire a non-blocking advisory flock on a regular lock file."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _require_real_directory(path.parent, "managed application root")
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ManagedInstallError("managed lifecycle lock must be a regular file")
    descriptor = os.open(
        path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        try:
            operation = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
            fcntl.flock(descriptor, operation | fcntl.LOCK_NB)
        except BlockingIOError as error:
            message = (
                "a managed install or update is active; rerun sat when it finishes"
                if shared
                else "a SAT task, managed install, or update is active"
            )
            raise ManagedInstallError(message) from error
        yield
    finally:
        os.close(descriptor)


@contextmanager
def _exclusive_update_lock(paths: ManagedInstallPaths):
    """Enforce exclusive ownership of the managed lifecycle root while updating."""
    from software_agent_team import managed_install

    managed_install._require_managed_root(paths)
    with _lifecycle_file_lock(paths.lock, shared=False):
        yield


def _run_command(
    arguments: Sequence[str],
    cwd: Path | None,
    environment: Mapping[str, str] | None,
) -> None:
    """Execute a subprocess command, enforcing git policy for git invocations."""
    is_git = arguments[0] == "git"
    command = (
        git_command(list(arguments[1:]), allow_https=True)
        if is_git
        else list(arguments)
    )
    command_environment = (
        git_environment(cwd or Path.cwd())
        if is_git
        else None
        if environment is None
        else dict(environment)
    )
    try:
        subprocess.run(
            command,
            cwd=cwd,
            env=command_environment,
            check=True,
            timeout=1_800,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise ManagedInstallError(f"managed command failed: {arguments[0]}") from error


def _capture_command(arguments: Sequence[str]) -> str:
    """Execute a subprocess command and return stripped stdout output."""
    is_git = arguments[0] == "git"
    command = (
        git_command(list(arguments[1:]), allow_https=True)
        if is_git
        else list(arguments)
    )
    try:
        completed = subprocess.run(
            command,
            env=git_environment(Path.cwd()) if is_git else None,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise ManagedInstallError(f"managed command failed: {arguments[0]}") from error
    return completed.stdout.strip()
