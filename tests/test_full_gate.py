"""Tests for the durable canonical repository-gate supervisor."""

from __future__ import annotations

import io
import json
import signal
import socket
import subprocess
import sys
import threading
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
    release = tmp_path / "release-stage"
    observer_error: list[str] = []

    def release_after_attributable_sample() -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            reports = tuple((tmp_path / "evidence").glob("*/report.json"))
            if reports:
                try:
                    pending = json.loads(reports[0].read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    pass
                else:
                    observation = pending["stages"][0]["resource_observation"]
                    if observation["resource_samples"]:
                        release.touch()
                        return
            time.sleep(0.005)
        observer_error.append("stage sample did not become attributable")
        release.touch()

    observer = threading.Thread(target=release_after_attributable_sample)
    observer.start()
    first_script = (
        "import pathlib, time; print('first output', flush=True); "
        f"release = pathlib.Path({str(release)!r}); "
        "\nwhile not release.exists(): time.sleep(0.005)"
    )
    exit_code, report, output, report_path = _run(
        tmp_path,
        first_script,
        "print('second output', flush=True)",
    )
    observer.join(timeout=1)

    assert exit_code == 0
    assert observer_error == []
    assert not observer.is_alive()
    assert report["schema_version"] == 3
    assert report["status"] == FullGateStatus.COMPLETED.value
    assert report["process_attribution"] == {
        "mode": "subreaper_with_inherited_stage_identity",
        "status": "available",
        "reason": None,
        "restored": True,
    }
    assert [item["status"] for item in report["stages"]] == [
        StageStatus.COMPLETED.value,
        StageStatus.COMPLETED.value,
    ]
    assert report["stages"][0]["argv"] == [
        sys.executable,
        "-c",
        first_script,
    ]
    assert report["cwd"] == str(tmp_path)
    assert report["started_at"] and report["ended_at"]
    assert report["resources"]["resource_observation"]["status"] == "available"
    assert report["resources"]["aggregate_peak_rss_bytes"] > 0
    assert report["resources"]["peak_process_count"] >= 1
    assert report["stages"][0]["resource_observation"]["status"] == "available"
    assert report["post_run_inventory"]["process_leases"]["status"] in {
        "available",
        "unavailable",
    }
    assert b"first output\n" in output
    assert b"second output\n" in output
    assert (report_path.parent / "stage-1.log").read_bytes() == b"first output\n"


def test_unobserved_short_stage_is_typed_unavailable_instead_of_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(full_gate, "_owned_processes", lambda *args, **kwargs: ())

    exit_code, report, _, _ = _run(tmp_path, "pass")

    assert exit_code == 0
    resources = report["resources"]
    observation = resources["resource_observation"]
    assert observation["status"] == "unavailable"
    assert observation["sample_attempts"] >= 1
    assert observation["identity_samples"] == 0
    assert observation["resource_samples"] == 0
    assert observation["reason"] == "process_exited_before_attributable_sample"
    assert resources["aggregate_peak_rss_bytes"] is None
    assert resources["peak_process_count"] is None
    assert resources["peak_thread_count"] is None
    assert resources["peak_process_tree"] is None
    assert report["stages"][0]["resource_observation"]["status"] == "unavailable"


@pytest.mark.parametrize("attempt", range(5))
def test_real_short_stage_never_represents_missing_observation_as_zero(
    tmp_path: Path, attempt: int
) -> None:
    repository = tmp_path / f"attempt-{attempt}"
    repository.mkdir()

    exit_code, report, _, _ = _run(repository, "pass")

    assert exit_code == 0
    resources = report["resources"]
    if resources["resource_observation"]["status"] == "available":
        assert resources["aggregate_peak_rss_bytes"] > 0
        assert resources["peak_process_count"] >= 1
        assert resources["peak_thread_count"] >= 1
        assert resources["peak_process_tree"]
    else:
        assert resources["resource_observation"]["status"] == "unavailable"
        assert resources["aggregate_peak_rss_bytes"] is None
        assert resources["peak_process_count"] is None
        assert resources["peak_thread_count"] is None
        assert resources["peak_process_tree"] is None


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
        )
    )

    exit_code, report, _, _ = _run(tmp_path, code)

    assert exit_code == 1
    stage = report["stages"][0]
    assert stage["status"] == StageStatus.FAILED.value
    actions = stage["process"]["cleanup"]["actions"]
    assert any(action.startswith("sigterm_pid:") for action in actions)
    assert any(action.startswith("reaped_pid:") for action in actions)
    assert stage["process"]["residual_after_cleanup"] == []
    assert stage["process_ownership"]["mechanism"] == "inherited_stage_identity"
    assert len(stage["process_ownership"]["identity_sha256"]) == 64
    assert "SAT_FULL_GATE_STAGE_ID" not in json.dumps(report)


@pytest.mark.skipif(not Path("/proc").exists(), reason="Linux process evidence")
def test_stage_identity_does_not_capture_an_unrelated_process(tmp_path: Path) -> None:
    unrelated = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        exit_code, report, _, _ = _run(tmp_path, "pass")

        assert exit_code == 0
        assert report["status"] == FullGateStatus.COMPLETED.value
        assert unrelated.poll() is None
    finally:
        unrelated.kill()
        unrelated.wait(timeout=5)


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
    ready_socket_path = tmp_path / "stage-ready.sock"
    ready_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    ready_socket.bind(str(ready_socket_path))
    ready_socket.listen(1)
    ready_socket.settimeout(15)
    stage_program = "; ".join(
        (
            "import socket, time",
            "ready = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)",
            f"ready.connect({str(ready_socket_path)!r})",
            "ready.sendall(b'stage-ready\\n')",
            "ready.close()",
            "time.sleep(30)",
        )
    )
    program = "\n".join(
        (
            "from pathlib import Path",
            "import sys",
            "import software_agent_team.full_gate as full_gate",
            "from software_agent_team.full_gate import FullGateSupervisor, GateStage",
            "full_gate.shutil.which = lambda _: None",
            "root, evidence = Path(sys.argv[1]), Path(sys.argv[2])",
            "runner = FullGateSupervisor(repository_root=root, evidence_root=evidence,",
            "    stages=(GateStage('slow', (sys.executable, '-c',",
            f"        {stage_program!r})),), sample_interval_seconds=0.01,",
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
        with ready_socket:
            try:
                connection, _ = ready_socket.accept()
            except TimeoutError:
                pytest.fail(
                    "full-gate stage did not reach its explicit child-ready "
                    f"checkpoint (supervisor_status={process.poll()}, "
                    f"reports={len(tuple(evidence.glob('*/report.json')))})"
                )
            with connection, connection.makefile("rb") as stream:
                assert stream.readline() == b"stage-ready\n"

        reports = tuple(evidence.glob("*/report.json"))
        assert len(reports) == 1
        report_path = reports[0]
        running = json.loads(report_path.read_text(encoding="utf-8"))
        assert running["stages"][0]["status"] == StageStatus.RUNNING.value

        process.send_signal(signal.SIGTERM)
        try:
            exit_code = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            terminal = json.loads(report_path.read_text(encoding="utf-8"))
            pytest.fail(
                "full-gate supervisor did not exit after its signal-ready stage "
                f"(report_status={terminal['status']}, "
                f"stage_status={terminal['stages'][0]['status']})"
            )
        assert exit_code == 128 + signal.SIGTERM
        report = json.loads(report_path.read_text(encoding="utf-8"))

        assert report["status"] == FullGateStatus.INTERRUPTED.value
        assert report["interruption_signal"] == signal.SIGTERM
        assert report["stages"][0]["status"] == StageStatus.INTERRUPTED.value
        assert report["post_run_inventory"] is not None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
