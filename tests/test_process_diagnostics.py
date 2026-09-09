"""Diagnostic snapshots must not grant progress or inspect process content."""

import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from software_agent_team import process_diagnostics as diagnostics
from software_agent_team.process_lifecycle import read_linux_process_identity


def test_live_owned_child_survives_snapshot():
    child = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        identity = read_linux_process_identity(child.pid)
        assert identity is not None
        result = diagnostics.snapshot_process_wait(identity, expected_uid=os.getuid())
        assert result.status == "observed"
        assert result.identity == identity
        assert child.poll() is None
    finally:
        child.terminate()
        child.wait(timeout=5)


def test_live_snapshot_is_content_free_and_does_not_signal(monkeypatch):
    identity = read_linux_process_identity(os.getpid())
    assert identity is not None
    original = diagnostics._read_proc_field
    reads = []

    def read(path):
        reads.append(path.name)
        return original(path)

    def forbidden(*args):
        pytest.fail("diagnostics must never signal a process")

    monkeypatch.setattr(diagnostics, "_read_proc_field", read)
    monkeypatch.setattr(os, "kill", forbidden)
    snapshot = diagnostics.snapshot_process_wait(identity, expected_uid=os.getuid())
    assert snapshot.status == "observed"
    assert snapshot.expected_uid == os.getuid()
    assert snapshot.user_ticks is not None
    assert snapshot.major_faults is not None
    assert reads == ["stat", "status", "io", "wchan"]
    assert read_linux_process_identity(os.getpid()) == identity


@pytest.mark.parametrize("when", ["before", "after"])
def test_identity_change_discards_all_metrics(monkeypatch, when):
    identity = read_linux_process_identity(os.getpid())
    changed = identity.model_copy(
        update={"start_time_ticks": identity.start_time_ticks + 1}
    )
    identities = iter([changed] if when == "before" else [identity, changed])
    monkeypatch.setattr(
        diagnostics, "read_linux_process_identity", lambda pid: next(identities)
    )
    snapshot = diagnostics.snapshot_process_wait(identity, expected_uid=os.getuid())
    assert snapshot.status == "identity_changed"
    assert snapshot.user_ticks is None
    assert snapshot.wait_channel is None


def test_foreign_uid_never_reads_metrics(monkeypatch):
    identity = read_linux_process_identity(os.getpid())

    def forbidden(path):
        pytest.fail("foreign process metrics were read")

    monkeypatch.setattr(diagnostics, "_read_proc_field", forbidden)
    result = diagnostics.snapshot_process_wait(identity, expected_uid=os.getuid() + 1)
    assert result.status == "identity_changed"


def test_optional_read_failure_is_explicit_not_zero(monkeypatch):
    identity = read_linux_process_identity(os.getpid())
    original = diagnostics._read_proc_field

    def read(path):
        if path.name == "io":
            raise PermissionError("denied")
        return original(path)

    monkeypatch.setattr(diagnostics, "_read_proc_field", read)
    result = diagnostics.snapshot_process_wait(identity, expected_uid=os.getuid())
    assert result.status == "observed"
    assert result.read_bytes is None
    assert {"read_bytes", "write_bytes"} <= set(result.unavailable_fields)


def test_missing_process_has_no_metrics(monkeypatch):
    identity = read_linux_process_identity(os.getpid())
    monkeypatch.setattr(diagnostics, "read_linux_process_identity", lambda pid: None)
    result = diagnostics.snapshot_process_wait(identity, expected_uid=os.getuid())
    assert result.status == "unavailable"
    assert result.rss_kib is None


def test_diagnostic_read_rejects_symlink(tmp_path: Path):
    target = tmp_path / "target"
    target.write_text("not a procfs field")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(OSError):
        diagnostics._read_proc_field(link)


def test_diagnostic_read_is_bounded(tmp_path: Path):
    oversized = tmp_path / "oversized"
    oversized.write_text("x" * 16_385)
    with pytest.raises(ValueError, match="bound"):
        diagnostics._read_proc_field(oversized)


def test_live_tree_observes_owned_runtime_child():
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import subprocess,time,signal\n"
            "p=subprocess.Popen(['sleep','30'])\n"
            "def stop(*args): raise SystemExit(0)\n"
            "signal.signal(signal.SIGTERM,stop)\n"
            "print(p.pid,flush=True)\n"
            "try: time.sleep(30)\n"
            "finally:\n p.terminate()\n p.wait()\n",
        ],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        descendant_pid = int(child.stdout.readline())
        identity = read_linux_process_identity(child.pid)
        result = diagnostics.snapshot_initialization_wait(
            identity,
            expected_uid=os.getuid(),
            reason="suspected",
            elapsed_ms=1,
        )
        assert not result.incomplete
        assert {item.identity.pid for item in result.processes} == {
            child.pid,
            descendant_pid,
        }
        assert all(item.status == "observed" for item in result.processes)
    finally:
        os.killpg(child.pid, signal.SIGTERM)
        child.wait(timeout=5)
        child.stdout.close()


def test_tree_bound_reports_incomplete_instead_of_assuming_no_children(monkeypatch):
    identity = read_linux_process_identity(os.getpid())
    monkeypatch.setattr(diagnostics, "MAX_WAIT_SNAPSHOT_PROCESSES", 0)
    result = diagnostics.snapshot_initialization_wait(
        identity,
        expected_uid=os.getuid(),
        reason="stalled",
        elapsed_ms=1,
    )
    assert result.incomplete
    assert result.processes == ()


def test_tree_identity_unavailable_is_explicit():
    result = diagnostics.snapshot_initialization_wait(
        None,
        expected_uid=os.getuid(),
        reason="suspected",
        elapsed_ms=1,
    )
    assert result.incomplete
    assert not result.processes


@pytest.mark.parametrize("children_present", [True, False])
def test_tree_bound_limits_identity_reads_not_only_saved_records(
    monkeypatch, children_present
):
    root = read_linux_process_identity(os.getpid())
    observed = diagnostics.snapshot_process_wait(root, expected_uid=os.getuid())
    children = [root.pid + offset for offset in range(1, 13)]
    child_reads = []

    def identity(pid):
        if pid != root.pid:
            child_reads.append(pid)
            if not children_present:
                return None
        return root.model_copy(update={"pid": pid})

    monkeypatch.setattr(diagnostics, "MAX_WAIT_SNAPSHOT_PROCESSES", 4)
    monkeypatch.setattr(diagnostics, "read_linux_process_identity", identity)
    monkeypatch.setattr(
        diagnostics,
        "snapshot_process_wait",
        lambda value, **kwargs: observed.model_copy(update={"identity": value}),
    )
    monkeypatch.setattr(
        diagnostics,
        "_read_proc_field",
        lambda path: (
            " ".join(map(str, children)) if path.parts[2] == str(root.pid) else ""
        ),
    )
    result = diagnostics.snapshot_initialization_wait(
        root,
        expected_uid=os.getuid(),
        reason="suspected",
        elapsed_ms=1,
    )
    assert result.incomplete
    assert len(result.processes) == (4 if children_present else 1)
    assert set(child_reads) == set(children[:3])
    # Each admitted child is read once for admission and once as a parent.
    assert len(child_reads) <= 6
