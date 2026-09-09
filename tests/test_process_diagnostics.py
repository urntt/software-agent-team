"""Diagnostic snapshots must not grant progress or inspect process content."""

import os
import subprocess
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
