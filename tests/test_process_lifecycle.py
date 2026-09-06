"""Tests for PID-reuse-safe SAT provider-process ownership."""

from __future__ import annotations

import ctypes
import os
import signal
import socket
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

import pytest

import software_agent_team.process_lifecycle as process_lifecycle
from software_agent_team.process_lifecycle import (
    ProcessLeaseStatus,
    ProcessLeaseStore,
    ProcessLifecycleError,
    read_linux_process_identity,
)

_PR_SET_CHILD_SUBREAPER = 36
_PR_GET_CHILD_SUBREAPER = 37


@contextmanager
def local_child_subreaper() -> Iterator[None]:
    """Make this fixture the exact reap owner for its simulated orphan."""

    libc = ctypes.CDLL(None, use_errno=True)
    previous = ctypes.c_int()
    if libc.prctl(_PR_GET_CHILD_SUBREAPER, ctypes.byref(previous), 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "cannot read child-subreaper state")
    if libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "cannot set child-subreaper state")
    try:
        yield
    finally:
        if libc.prctl(_PR_SET_CHILD_SUBREAPER, previous.value, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "cannot restore child-subreaper state")


def start_child() -> subprocess.Popen[str]:
    return subprocess.Popen(
        ["sleep", "30"],
        text=True,
        start_new_session=True,
    )


def stop_child(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.terminate()
    process.wait(timeout=5)


def test_linux_identity_binds_pid_group_and_start_ticks() -> None:
    identity = read_linux_process_identity(1)

    assert identity is not None
    assert identity.pid == 1
    assert identity.process_group_id >= 1
    assert identity.start_time_ticks >= 1


def test_process_lease_is_active_only_while_exact_owner_identity_exists(
    tmp_path: Path,
) -> None:
    process = start_child()
    store = ProcessLeaseStore(tmp_path / "leases")
    try:
        lease = store.acquire(
            run_id="sat-process-active",
            agent_id="builder",
            session_key="agent:builder:sat-process-active-i1-work-result",
            child_pid=process.pid,
            command=("sleep", "30"),
        )

        restarted_store = ProcessLeaseStore(tmp_path / "leases")
        observation = restarted_store.inspect()

        assert len(observation.active) == 1
        assert observation.active[0].lease == lease
        assert not observation.orphaned
        assert not observation.stale
        store.release(lease)
        assert not tuple((tmp_path / "leases").glob("*.json"))
    finally:
        stop_child(process)


def test_restarted_controller_reclaims_exact_orphan_but_not_active_owner(
    tmp_path: Path,
) -> None:
    process = start_child()
    owner_alive = True

    def identity_reader(pid: int):  # type: ignore[no-untyped-def]
        if pid == process.pid and process.poll() is not None:
            return None
        if pid != process.pid and not owner_alive:
            return None
        return read_linux_process_identity(pid)

    store = ProcessLeaseStore(
        tmp_path / "leases",
        identity_reader=identity_reader,
    )
    lease = store.acquire(
        run_id="sat-process-orphan",
        agent_id="tester",
        session_key="agent:tester:sat-process-orphan-i1-test-report",
        child_pid=process.pid,
        command=("sleep", "30"),
    )
    owner_alive = False

    observation = ProcessLeaseStore(
        tmp_path / "leases",
        identity_reader=identity_reader,
    ).inspect()
    assert observation.processes[0].status is ProcessLeaseStatus.ORPHANED

    recovery = store.reclaim_orphans(grace_seconds=1)

    assert recovery.reclaimed == (lease,)
    assert not recovery.active
    assert process.wait(timeout=5) < 0
    assert not tuple((tmp_path / "leases").glob("*.json"))


def test_pid_reuse_or_missing_child_is_stale_and_never_signalled(
    tmp_path: Path,
) -> None:
    process = start_child()
    owner_alive = True
    child_is_original = True

    def identity_reader(pid: int):  # type: ignore[no-untyped-def]
        nonlocal child_is_original
        if pid == process.pid and process.poll() is not None:
            return None
        identity = read_linux_process_identity(pid)
        if pid == process.pid and not child_is_original and identity is not None:
            return identity.model_copy(
                update={"start_time_ticks": identity.start_time_ticks + 1}
            )
        if pid != process.pid and not owner_alive:
            return None
        return identity

    store = ProcessLeaseStore(
        tmp_path / "leases",
        identity_reader=identity_reader,
    )
    store.acquire(
        run_id="sat-process-reused",
        agent_id="reviewer",
        session_key="agent:reviewer:sat-process-reused-i1-review-report",
        child_pid=process.pid,
        command=("sleep", "30"),
    )
    owner_alive = False
    child_is_original = False

    observation = store.inspect()
    assert observation.processes[0].status is ProcessLeaseStatus.STALE
    recovery = store.reclaim_orphans(grace_seconds=1)

    assert len(recovery.reclaimed) == 1
    assert process.poll() is None
    assert not tuple((tmp_path / "leases").glob("*.json"))
    stop_child(process)


def test_orphan_recovery_fails_closed_without_pidfd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = start_child()
    owner_alive = True

    def identity_reader(pid: int):  # type: ignore[no-untyped-def]
        if pid == process.pid and process.poll() is not None:
            return None
        if pid != process.pid and not owner_alive:
            return None
        return read_linux_process_identity(pid)

    store = ProcessLeaseStore(
        tmp_path / "leases",
        identity_reader=identity_reader,
    )
    lease = store.acquire(
        run_id="sat-process-no-pidfd",
        agent_id="builder",
        session_key="agent:builder:sat-process-no-pidfd-i1-work-result",
        child_pid=process.pid,
        command=("sleep", "30"),
    )
    owner_alive = False
    monkeypatch.delattr(process_lifecycle.os, "pidfd_open")

    with pytest.raises(ProcessLifecycleError, match="requires Linux pidfd"):
        store.reclaim_orphans(grace_seconds=1)

    assert process.poll() is None
    store.release(lease)
    stop_child(process)


@pytest.mark.skipif(not Path("/proc").exists(), reason="Linux process evidence")
def test_new_process_recovers_lease_after_controller_is_killed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "leases"
    ready_socket_path = tmp_path / "ready.sock"
    ready_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    ready_socket.bind(str(ready_socket_path))
    ready_socket.listen(1)
    ready_socket.settimeout(15)
    controller_code = "\n".join(
        (
            "import socket, subprocess, sys, time",
            "from pathlib import Path",
            "from software_agent_team.process_lifecycle import ProcessLeaseStore",
            "root, ready_path = Path(sys.argv[1]), sys.argv[2]",
            "child = subprocess.Popen(['sleep', '30'], start_new_session=True)",
            "store = ProcessLeaseStore(root)",
            "store.acquire(run_id='sat-crash', agent_id='builder', "
            "session_key='agent:builder:sat-crash-i1-work-result', "
            "child_pid=child.pid, command=('sleep', '30'))",
            "ready = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)",
            "ready.connect(ready_path)",
            "ready.sendall((str(child.pid) + '\\n').encode('ascii'))",
            "ready.close()",
            "time.sleep(30)",
        )
    )
    with local_child_subreaper():
        controller = subprocess.Popen(
            [sys.executable, "-c", controller_code, str(root), str(ready_socket_path)],
            text=True,
        )
        child_pid: int | None = None
        try:
            with ready_socket:
                try:
                    connection, _ = ready_socket.accept()
                except TimeoutError:
                    pytest.fail(
                        "controller did not publish its post-acquire ready frame "
                        f"(controller_status={controller.poll()}, "
                        f"lease_files={len(tuple(root.glob('*.json')))})"
                    )
                with connection, connection.makefile("rb") as stream:
                    child_pid = int(stream.readline().decode("ascii"))

            controller.kill()
            controller.wait(timeout=5)
            restarted = ProcessLeaseStore(root)
            observation = restarted.inspect()

            assert len(observation.orphaned) == 1
            assert observation.orphaned[0].lease.child.pid == child_pid
            recovery = restarted.reclaim_orphans(grace_seconds=2)
            assert len(recovery.reclaimed) == 1
            waited_pid, _ = os.waitpid(child_pid, 0)
            assert waited_pid == child_pid
            assert read_linux_process_identity(child_pid) is None
            assert restarted.inspect().processes == ()
        finally:
            if controller.poll() is None:
                controller.kill()
                controller.wait(timeout=5)
            if (
                child_pid is not None
                and read_linux_process_identity(child_pid) is not None
            ):
                with suppress(ProcessLookupError):
                    os.killpg(child_pid, signal.SIGKILL)
                with suppress(ChildProcessError):
                    os.waitpid(child_pid, 0)
