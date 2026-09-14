"""Bounded supervision for short-lived SAT subprocesses on Linux."""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from software_agent_team.process_lifecycle import ProcessIdentity

_OWNERSHIP_ENVIRONMENT_VARIABLE = "SAT_BOUNDED_PROCESS_ID"
_MAX_PROCESS_ENVIRONMENT_BYTES = 4 * 1024 * 1024
_PR_SET_CHILD_SUBREAPER = 36
_PR_GET_CHILD_SUBREAPER = 37
_POLL_SECONDS = 0.05


class BoundedProcessError(RuntimeError):
    """Raised when a bounded child cannot be launched or safely supervised."""


class BoundedProcessTimeoutError(BoundedProcessError):
    """Raised after a timed-out child and all owned descendants are stopped."""

    def __init__(self, timeout_seconds: float) -> None:
        super().__init__(
            f"bounded subprocess timed out after {timeout_seconds:g} seconds"
        )
        self.timeout_seconds = timeout_seconds


class BoundedProcessResidualError(BoundedProcessError):
    """Raised when an owned process remains after bounded termination."""


@dataclass(frozen=True)
class _SubreaperBoundary:
    previously_active: bool


@dataclass(frozen=True)
class _ProcessObservation:
    identity: ProcessIdentity
    parent_pid: int
    state: str
    uid: int


def _set_child_subreaper() -> _SubreaperBoundary:
    if sys.platform != "linux":
        raise BoundedProcessError("safe bounded subprocess supervision requires Linux")
    try:
        library = ctypes.CDLL(None, use_errno=True)
        current = ctypes.c_int()
        if (
            library.prctl(
                _PR_GET_CHILD_SUBREAPER,
                ctypes.byref(current),
                0,
                0,
                0,
            )
            != 0
        ):
            raise OSError(ctypes.get_errno(), "PR_GET_CHILD_SUBREAPER failed")
        previous = bool(current.value)
        if not previous and library.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "PR_SET_CHILD_SUBREAPER failed")
    except (AttributeError, OSError) as error:
        raise BoundedProcessError(
            "could not establish the Linux child-subreaper boundary"
        ) from error
    return _SubreaperBoundary(previously_active=previous)


def _restore_child_subreaper(boundary: _SubreaperBoundary) -> None:
    if boundary.previously_active:
        return
    try:
        library = ctypes.CDLL(None, use_errno=True)
        if library.prctl(_PR_SET_CHILD_SUBREAPER, 0, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "PR_SET_CHILD_SUBREAPER restore failed")
    except (AttributeError, OSError) as error:
        raise BoundedProcessError(
            "could not restore the Linux child-subreaper boundary"
        ) from error


def _read_process(pid: int) -> _ProcessObservation | None:
    try:
        base = Path(f"/proc/{pid}")
        raw = (base / "stat").read_text(encoding="utf-8")
        closing = raw.rfind(")")
        if closing < 1:
            return None
        fields = raw[closing + 2 :].split()
        before = ProcessIdentity(
            pid=pid,
            process_group_id=int(fields[2]),
            start_time_ticks=int(fields[19]),
        )
        parent_pid = int(fields[1])
        state = fields[0]
        uid = base.stat().st_uid
        verification = (base / "stat").read_text(encoding="utf-8")
        verification_closing = verification.rfind(")")
        verification_fields = verification[verification_closing + 2 :].split()
        after = ProcessIdentity(
            pid=pid,
            process_group_id=int(verification_fields[2]),
            start_time_ticks=int(verification_fields[19]),
        )
        if before != after or parent_pid != int(verification_fields[1]):
            return None
        return _ProcessObservation(
            identity=before,
            parent_pid=parent_pid,
            state=state,
            uid=uid,
        )
    except (
        FileNotFoundError,
        ProcessLookupError,
        PermissionError,
        OSError,
        ValueError,
    ):
        return None


def _process_has_marker(pid: int, marker: bytes) -> bool:
    try:
        descriptor = os.open(
            f"/proc/{pid}/environ",
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
        )
        with os.fdopen(descriptor, "rb") as environment:
            content = environment.read(_MAX_PROCESS_ENVIRONMENT_BYTES + 1)
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
        return False
    if len(content) > _MAX_PROCESS_ENVIRONMENT_BYTES:
        return False
    return marker in content.split(b"\0")


def _all_process_ids() -> tuple[int, ...]:
    try:
        return tuple(
            int(entry.name) for entry in Path("/proc").iterdir() if entry.name.isdigit()
        )
    except OSError:
        return ()


def _direct_children(parent_pid: int) -> set[ProcessIdentity]:
    return {
        observation.identity
        for pid in _all_process_ids()
        if (observation := _read_process(pid)) is not None
        and observation.parent_pid == parent_pid
    }


def _observe_owned_processes(
    *,
    marker: bytes,
    expected_uid: int,
    supervisor_pid: int,
    baseline_children: set[ProcessIdentity],
    tracked: dict[tuple[int, int], ProcessIdentity],
) -> tuple[_ProcessObservation, ...]:
    observed: dict[tuple[int, int], _ProcessObservation] = {}
    for pid in _all_process_ids():
        process = _read_process(pid)
        if process is None or process.uid != expected_uid:
            continue
        identity_key = (
            process.identity.pid,
            process.identity.start_time_ticks,
        )
        inherited_marker = _process_has_marker(pid, marker)
        adopted_child = (
            process.parent_pid == supervisor_pid
            and process.identity not in baseline_children
        )
        previously_tracked = identity_key in tracked
        if not (inherited_marker or adopted_child or previously_tracked):
            continue
        current = _read_process(pid)
        if current is None or current.identity != process.identity:
            continue
        tracked[identity_key] = current.identity
        observed[identity_key] = current
    return tuple(observed[key] for key in sorted(observed))


def _signal_exact_process(
    process: _ProcessObservation,
    signum: signal.Signals,
    *,
    expected_uid: int,
) -> None:
    current = _read_process(process.identity.pid)
    if (
        current is None
        or current.identity != process.identity
        or current.uid != expected_uid
        or current.state == "Z"
    ):
        return
    pidfd_open = getattr(os, "pidfd_open", None)
    pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
    if not callable(pidfd_open) or not callable(pidfd_send_signal):
        raise BoundedProcessError("safe bounded subprocess cleanup requires pidfd")
    try:
        pidfd = pidfd_open(process.identity.pid, 0)
    except ProcessLookupError:
        return
    except OSError as error:
        raise BoundedProcessError("could not pin an owned subprocess") from error
    try:
        current = _read_process(process.identity.pid)
        if (
            current is None
            or current.identity != process.identity
            or current.uid != expected_uid
            or current.state == "Z"
        ):
            return
        try:
            pidfd_send_signal(pidfd, signum)
        except ProcessLookupError:
            return
        except OSError as error:
            raise BoundedProcessError("could not signal an owned subprocess") from error
    finally:
        os.close(pidfd)


def _reap_adopted_zombies(
    processes: Sequence[_ProcessObservation],
    *,
    supervisor_pid: int,
    root_pid: int,
) -> None:
    for process in processes:
        if (
            process.identity.pid == root_pid
            or process.parent_pid != supervisor_pid
            or process.state != "Z"
        ):
            continue
        current = _read_process(process.identity.pid)
        if (
            current is None
            or current.identity != process.identity
            or current.parent_pid != supervisor_pid
            or current.state != "Z"
        ):
            continue
        with suppress(ChildProcessError):
            os.waitpid(process.identity.pid, os.WNOHANG)


def _terminate_owned_processes(
    process: subprocess.Popen[str],
    *,
    marker: bytes,
    expected_uid: int,
    supervisor_pid: int,
    baseline_children: set[ProcessIdentity],
    grace_seconds: float,
) -> tuple[_ProcessObservation, ...]:
    tracked: dict[tuple[int, int], ProcessIdentity] = {}

    def observe() -> tuple[_ProcessObservation, ...]:
        process.poll()
        current = _observe_owned_processes(
            marker=marker,
            expected_uid=expected_uid,
            supervisor_pid=supervisor_pid,
            baseline_children=baseline_children,
            tracked=tracked,
        )
        _reap_adopted_zombies(
            current,
            supervisor_pid=supervisor_pid,
            root_pid=process.pid,
        )
        return _observe_owned_processes(
            marker=marker,
            expected_uid=expected_uid,
            supervisor_pid=supervisor_pid,
            baseline_children=baseline_children,
            tracked=tracked,
        )

    for signum in (signal.SIGTERM, signal.SIGKILL):
        deadline = time.monotonic() + grace_seconds
        signalled: set[tuple[int, int]] = set()
        while True:
            owned = observe()
            if not owned:
                return ()
            for item in owned:
                key = (item.identity.pid, item.identity.start_time_ticks)
                if key in signalled:
                    continue
                _signal_exact_process(item, signum, expected_uid=expected_uid)
                signalled.add(key)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(_POLL_SECONDS, remaining))
    return observe()


def run_bounded_process(
    command: Sequence[str],
    *,
    environment: Mapping[str, str],
    timeout_seconds: float,
    termination_grace_seconds: float,
) -> subprocess.CompletedProcess[str]:
    """Run one command and prove that every inherited child is gone on return."""

    if not command or any(not isinstance(value, str) or not value for value in command):
        raise ValueError("bounded subprocess command must contain non-empty strings")
    if timeout_seconds <= 0 or termination_grace_seconds <= 0:
        raise ValueError("bounded subprocess time limits must be positive")
    if not callable(getattr(os, "pidfd_open", None)) or not callable(
        getattr(signal, "pidfd_send_signal", None)
    ):
        raise BoundedProcessError("safe bounded subprocess cleanup requires pidfd")
    if threading.active_count() != 1:
        raise BoundedProcessError(
            "safe bounded subprocess supervision requires one controller thread"
        )

    boundary = _set_child_subreaper()
    supervisor_pid = os.getpid()
    baseline_children = _direct_children(supervisor_pid)
    if baseline_children:
        _restore_child_subreaper(boundary)
        raise BoundedProcessError(
            "safe bounded subprocess supervision requires no existing children"
        )
    marker_value = uuid4().hex
    marker = f"{_OWNERSHIP_ENVIRONMENT_VARIABLE}={marker_value}".encode()
    child_environment = dict(environment)
    child_environment[_OWNERSHIP_ENVIRONMENT_VARIABLE] = marker_value
    process: subprocess.Popen[str] | None = None
    try:
        process = subprocess.Popen(
            list(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            env=child_environment,
            shell=False,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as error:
            residual = _terminate_owned_processes(
                process,
                marker=marker,
                expected_uid=os.getuid(),
                supervisor_pid=supervisor_pid,
                baseline_children=baseline_children,
                grace_seconds=termination_grace_seconds,
            )
            if residual:
                raise BoundedProcessResidualError(
                    "owned subprocesses remained after timeout cleanup"
                ) from error
            with suppress(subprocess.TimeoutExpired):
                process.communicate(timeout=termination_grace_seconds)
            raise BoundedProcessTimeoutError(timeout_seconds) from error
        except BaseException as error:
            residual = _terminate_owned_processes(
                process,
                marker=marker,
                expected_uid=os.getuid(),
                supervisor_pid=supervisor_pid,
                baseline_children=baseline_children,
                grace_seconds=termination_grace_seconds,
            )
            if residual:
                raise BoundedProcessResidualError(
                    "owned subprocesses remained after interrupted cleanup"
                ) from error
            raise

        residual = _observe_owned_processes(
            marker=marker,
            expected_uid=os.getuid(),
            supervisor_pid=supervisor_pid,
            baseline_children=baseline_children,
            tracked={},
        )
        _reap_adopted_zombies(
            residual,
            supervisor_pid=supervisor_pid,
            root_pid=process.pid,
        )
        residual = _observe_owned_processes(
            marker=marker,
            expected_uid=os.getuid(),
            supervisor_pid=supervisor_pid,
            baseline_children=baseline_children,
            tracked={},
        )
        if residual:
            remaining = _terminate_owned_processes(
                process,
                marker=marker,
                expected_uid=os.getuid(),
                supervisor_pid=supervisor_pid,
                baseline_children=baseline_children,
                grace_seconds=termination_grace_seconds,
            )
            if remaining:
                raise BoundedProcessResidualError(
                    "owned subprocesses remained after command completion"
                )
            raise BoundedProcessResidualError(
                "the command exited while an owned descendant was still running"
            )
        return subprocess.CompletedProcess(
            args=list(command),
            returncode=process.returncode,
            stdout=stdout,
            stderr=stderr,
        )
    finally:
        _restore_child_subreaper(boundary)
