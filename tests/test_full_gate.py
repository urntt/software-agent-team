"""Tests for the durable canonical repository-gate supervisor."""

from __future__ import annotations

import io
import json
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import software_agent_team.full_gate as full_gate
import software_agent_team.full_gate_pytest_plugin as pytest_plugin
from software_agent_team.full_gate import (
    FullGateStatus,
    FullGateSupervisor,
    GateStage,
    StageStatus,
    canonical_stages,
)


def _run(
    tmp_path: Path,
    *scripts: str,
    timeout: float = 5,
) -> tuple[int, dict[str, object], bytes, Path]:
    output = io.BytesIO()
    stages = tuple(
        GateStage(f"stage-{index}", (sys.executable, "-c", script))
        for index, script in enumerate(scripts, start=1)
    )
    supervisor = FullGateSupervisor(
        repository_root=tmp_path,
        evidence_root=tmp_path / "evidence",
        stages=stages,
        stage_timeout_seconds=timeout,
        termination_grace_seconds=0.2,
        sample_interval_seconds=0.01,
        output=output,
    )

    exit_code, report_path = supervisor.run()

    return (
        exit_code,
        json.loads(report_path.read_text(encoding="utf-8")),
        output.getvalue(),
        report_path,
    )


def test_success_records_exact_commands_resources_and_terminal_inventory(
    tmp_path: Path,
) -> None:
    exit_code, report, output, report_path = _run(
        tmp_path,
        "import time; print('first output', flush=True); time.sleep(0.05)",
        "import time; print('second output', flush=True); time.sleep(0.05)",
    )

    assert exit_code == 0
    assert report["status"] == FullGateStatus.COMPLETED.value
    assert [item["status"] for item in report["stages"]] == [
        StageStatus.COMPLETED.value,
        StageStatus.COMPLETED.value,
    ]
    assert report["stages"][0]["argv"] == [
        sys.executable,
        "-c",
        "import time; print('first output', flush=True); time.sleep(0.05)",
    ]
    assert report["cwd"] == str(tmp_path)
    assert report["started_at"] and report["ended_at"]
    assert report["resources"]["aggregate_peak_rss_bytes"] > 0
    assert report["resources"]["peak_process_count"] >= 1
    assert report["post_run_inventory"]["process_leases"]["status"] in {
        "available",
        "unavailable",
    }
    assert b"first output\n" in output
    assert b"second output\n" in output
    assert (report_path.parent / "stage-1.log").read_bytes() == b"first output\n"


def test_nonzero_stage_keeps_real_exit_and_marks_later_stages_skipped(
    tmp_path: Path,
) -> None:
    exit_code, report, _, _ = _run(
        tmp_path,
        "raise SystemExit(7)",
        "raise AssertionError('must not run')",
    )

    assert exit_code == 7
    assert report["status"] == FullGateStatus.FAILED.value
    assert report["stages"][0]["status"] == StageStatus.FAILED.value
    assert report["stages"][0]["exit_code"] == 7
    assert report["stages"][1]["status"] == StageStatus.SKIPPED.value


def test_signal_exit_is_recorded_without_becoming_a_normal_exit(tmp_path: Path) -> None:
    exit_code, report, _, _ = _run(
        tmp_path,
        "import os, signal; os.kill(os.getpid(), signal.SIGUSR1)",
    )

    assert exit_code == 1
    assert report["status"] == FullGateStatus.FAILED.value
    assert report["stages"][0]["exit_code"] is None
    assert report["stages"][0]["signal"] == signal.SIGUSR1


def test_hung_stage_is_bounded_and_records_cleanup(tmp_path: Path) -> None:
    exit_code, report, _, _ = _run(
        tmp_path,
        "import time; time.sleep(30)",
        timeout=0.05,
    )

    assert exit_code == 124
    assert report["status"] == FullGateStatus.FAILED.value
    stage = report["stages"][0]
    assert stage["status"] == StageStatus.TIMED_OUT.value
    assert "sigterm_process_group" in stage["process"]["cleanup"]["actions"]
    assert stage["process"]["residual_after_cleanup"] == []


def test_cleanup_sleep_never_passes_a_crossed_deadline_to_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(full_gate.time, "monotonic", lambda: 10.2)
    monkeypatch.setattr(full_gate.time, "sleep", sleeps.append)

    slept = full_gate._sleep_before_deadline(10.1)

    assert not slept
    assert sleeps == []


def test_recovery_marks_abandoned_started_record_incomplete(tmp_path: Path) -> None:
    evidence_root = tmp_path / "evidence"
    abandoned = evidence_root / "abandoned"
    abandoned.mkdir(parents=True)
    (abandoned / "report.json").write_text(
        json.dumps(
            {
                "status": FullGateStatus.RUNNING.value,
                "supervisor_process": {
                    "pid": 999_999_999,
                    "start_time_ticks": 1,
                },
                "ended_at": None,
            }
        ),
        encoding="utf-8",
    )
    supervisor = FullGateSupervisor(
        repository_root=tmp_path,
        evidence_root=evidence_root,
        stages=(GateStage("success", (sys.executable, "-c", "pass")),),
        sample_interval_seconds=0.01,
        output=io.BytesIO(),
    )

    exit_code, _ = supervisor.run()

    recovered = json.loads((abandoned / "report.json").read_text(encoding="utf-8"))
    assert exit_code == 0
    assert recovered["status"] == (FullGateStatus.INCOMPLETE_OBSERVED_ON_RECOVERY.value)
    assert recovered["ended_at"] is None
    assert recovered["recovered_at"]
    assert recovered["incomplete_reason"] == (
        "supervisor_disappeared_without_terminal_record"
    )


def test_observers_fail_typed_without_masking_stage_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SAT_STATE_ROOT", "relative")
    monkeypatch.setattr(
        full_gate,
        "_read_cgroup_memory",
        lambda: {"status": "unavailable", "reason": "test_observer"},
    )
    monkeypatch.setattr(
        full_gate,
        "_kernel_oom_evidence",
        lambda: {"status": "unavailable", "reason": "test_observer"},
    )

    exit_code, report, _, _ = _run(tmp_path, "print('ok')")

    assert exit_code == 0
    assert report["status"] == FullGateStatus.COMPLETED.value
    assert report["post_run_inventory"]["process_leases"] == {
        "status": "unavailable",
        "reason": "UserPathError",
    }
    assert report["resources"]["cgroup"]["status"] == "unavailable"
    assert report["resources"]["kernel_oom_delta"]["status"] == "unavailable"


@pytest.mark.skipif(not Path("/proc").exists(), reason="Linux process evidence")
def test_detached_descendant_is_reported_and_exactly_cleaned(tmp_path: Path) -> None:
    code = "\n".join(
        (
            "import subprocess, sys, time",
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],",
            "                 start_new_session=True, stdout=subprocess.DEVNULL,",
            "                 stderr=subprocess.DEVNULL)",
            "time.sleep(0.2)",
        )
    )

    exit_code, report, _, _ = _run(tmp_path, code)

    assert exit_code == 1
    stage = report["stages"][0]
    assert stage["status"] == StageStatus.FAILED.value
    assert any(
        action.startswith("sigkill_pid:")
        for action in stage["process"]["cleanup"]["actions"]
    )
    assert stage["process"]["residual_after_cleanup"] == []


def test_canonical_stage_order_uses_shell_free_commands(tmp_path: Path) -> None:
    uv = tmp_path / "uv"
    stages = canonical_stages(tmp_path, uv)

    assert [stage.name for stage in stages] == [
        "doctor",
        "format-check",
        "lint",
        "test",
    ]
    assert stages[0].argv == (str(tmp_path / "scripts/doctor.sh"),)
    assert stages[-1].argv[-2:] == (
        "-p",
        "software_agent_team.full_gate_pytest_plugin",
    )
    assert all(isinstance(stage.argv, tuple) for stage in stages)


def test_pytest_plugin_persists_current_last_and_terminal_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "pytest-state.json"
    monkeypatch.setenv("SAT_FULL_GATE_PYTEST_STATE", str(state))

    pytest_plugin.pytest_sessionstart(None)
    pytest_plugin.pytest_runtest_logstart("tests/test_one.py::test_case", None)
    running = json.loads(state.read_text(encoding="utf-8"))
    pytest_plugin.pytest_runtest_logreport(
        SimpleNamespace(when="teardown", nodeid="tests/test_one.py::test_case")
    )
    pytest_plugin.pytest_sessionfinish(None, 0)
    terminal = json.loads(state.read_text(encoding="utf-8"))

    assert running["current_node_id"] == "tests/test_one.py::test_case"
    assert terminal == {
        "current_node_id": None,
        "exit_status": 0,
        "last_completed_node_id": "tests/test_one.py::test_case",
        "status": "completed",
    }


def test_supervisor_interrupt_is_forwarded_and_durably_terminal(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "evidence"
    program = "\n".join(
        (
            "from pathlib import Path",
            "import sys",
            "from software_agent_team.full_gate import FullGateSupervisor, GateStage",
            "root, evidence = Path(sys.argv[1]), Path(sys.argv[2])",
            "runner = FullGateSupervisor(repository_root=root, evidence_root=evidence,",
            "    stages=(GateStage('slow', (sys.executable, '-c',",
            "        'import time; time.sleep(30)')),), sample_interval_seconds=0.01,",
            "    termination_grace_seconds=0.2)",
            "raise SystemExit(runner.run()[0])",
        )
    )
    process = subprocess.Popen(
        [sys.executable, "-c", program, str(tmp_path), str(evidence)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        report_path = None
        for _ in range(500):
            reports = tuple(evidence.glob("*/report.json"))
            if reports:
                report_path = reports[0]
                payload = json.loads(report_path.read_text(encoding="utf-8"))
                if payload["stages"][0]["status"] == StageStatus.RUNNING.value:
                    break
            if process.poll() is not None:
                raise AssertionError(process.stderr.read())
            time.sleep(0.01)
        assert report_path is not None

        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=5) == 128 + signal.SIGTERM
        report = json.loads(report_path.read_text(encoding="utf-8"))

        assert report["status"] == FullGateStatus.INTERRUPTED.value
        assert report["interruption_signal"] == signal.SIGTERM
        assert report["stages"][0]["status"] == StageStatus.INTERRUPTED.value
        assert report["post_run_inventory"] is not None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
