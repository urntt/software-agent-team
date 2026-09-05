"""Diagnostic supervisor for the canonical repository quality gate."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import selectors
import shutil
import signal
import subprocess
import sys
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, BinaryIO
from uuid import uuid4

from software_agent_team.paths import user_state_root
from software_agent_team.process_lifecycle import (
    ProcessLeaseStore,
    ProcessLifecycleError,
    read_linux_process_identity,
)

FULL_GATE_SCHEMA_VERSION = 1
DEFAULT_STAGE_TIMEOUT_SECONDS = 1_800.0
DEFAULT_TERMINATION_GRACE_SECONDS = 5.0
DEFAULT_SAMPLE_INTERVAL_SECONDS = 0.10
REPORT_FILENAME = "report.json"
PYTEST_STATE_FILENAME = "pytest-state.json"


class FullGateStatus(StrEnum):
    """Terminal and non-terminal supervisor outcomes."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    INCOMPLETE_OBSERVED_ON_RECOVERY = "incomplete_observed_on_recovery"


class StageStatus(StrEnum):
    """Observable state of one canonical gate stage."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    INTERRUPTED = "interrupted"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class GateStage:
    """One shell-free command in the canonical repository gate."""

    name: str
    argv: tuple[str, ...]
    environment: dict[str, str] | None = None


@dataclass(frozen=True)
class ProcessSnapshot:
    """One non-secret Linux process observation."""

    pid: int
    parent_pid: int
    process_group_id: int
    start_time_ticks: int
    command_name: str
    rss_bytes: int
    thread_count: int

    def as_json(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "parent_pid": self.parent_pid,
            "process_group_id": self.process_group_id,
            "start_time_ticks": self.start_time_ticks,
            "command_name": self.command_name,
            "rss_bytes": self.rss_bytes,
            "thread_count": self.thread_count,
        }


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8") as output:
        json.dump(payload, output, ensure_ascii=False, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _git_fact(repository_root: Path) -> dict[str, Any]:
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain=v1"],
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError) as error:
        return {"status": "unavailable", "reason": type(error).__name__}
    return {
        "status": "available",
        "revision": revision,
        "dirty": bool(status),
        "status_sha256": hashlib.sha256(status.encode()).hexdigest(),
    }


def _read_proc_snapshot(pid: int) -> ProcessSnapshot | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        closing = raw.rfind(")")
        opening = raw.find("(")
        if opening < 1 or closing <= opening:
            return None
        fields = raw[closing + 2 :].split()
        statm = Path(f"/proc/{pid}/statm").read_text(encoding="utf-8").split()
        thread_count = sum(1 for _ in Path(f"/proc/{pid}/task").iterdir())
        return ProcessSnapshot(
            pid=pid,
            parent_pid=int(fields[1]),
            process_group_id=int(fields[2]),
            start_time_ticks=int(fields[19]),
            command_name=raw[opening + 1 : closing],
            rss_bytes=int(statm[1]) * os.sysconf("SC_PAGE_SIZE"),
            thread_count=thread_count,
        )
    except (
        FileNotFoundError,
        ProcessLookupError,
        PermissionError,
        OSError,
        ValueError,
    ):
        return None


def _all_processes() -> dict[int, ProcessSnapshot]:
    observed: dict[int, ProcessSnapshot] = {}
    try:
        entries = tuple(Path("/proc").iterdir())
    except OSError:
        return observed
    for entry in entries:
        if not entry.name.isdigit():
            continue
        snapshot = _read_proc_snapshot(int(entry.name))
        if snapshot is not None:
            observed[snapshot.pid] = snapshot
    return observed


def _owned_processes(
    root_pid: int,
    process_group_id: int,
    *,
    tracked: dict[tuple[int, int], ProcessSnapshot],
) -> tuple[ProcessSnapshot, ...]:
    processes = _all_processes()
    selected = {
        pid
        for pid, item in processes.items()
        if item.process_group_id == process_group_id
    }
    selected.add(root_pid)
    changed = True
    while changed:
        changed = False
        for pid, item in processes.items():
            if item.parent_pid in selected and pid not in selected:
                selected.add(pid)
                changed = True
    for identity in tuple(tracked):
        pid, start_time_ticks = identity
        item = processes.get(pid)
        if item is not None and item.start_time_ticks == start_time_ticks:
            selected.add(pid)
    result = tuple(processes[pid] for pid in sorted(selected) if pid in processes)
    for item in result:
        tracked[(item.pid, item.start_time_ticks)] = item
    return result


def _read_cgroup_memory() -> dict[str, Any]:
    try:
        membership = Path("/proc/self/cgroup").read_text(encoding="utf-8")
        unified = next(
            line.split(":", 2)[2]
            for line in membership.splitlines()
            if line.startswith("0::")
        )
        root = Path("/sys/fs/cgroup") / unified.lstrip("/")
        events = {
            key: int(value)
            for key, value in (
                line.split(maxsplit=1)
                for line in (root / "memory.events")
                .read_text(encoding="utf-8")
                .splitlines()
            )
        }
        current = int((root / "memory.current").read_text(encoding="utf-8"))
        peak_path = root / "memory.peak"
        peak = (
            int(peak_path.read_text(encoding="utf-8")) if peak_path.exists() else None
        )
    except (OSError, StopIteration, ValueError) as error:
        return {"status": "unavailable", "reason": type(error).__name__}
    return {
        "status": "available",
        "path": str(root),
        "memory_current_bytes": current,
        "memory_peak_bytes": peak,
        "events": events,
    }


def _cgroup_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    if before.get("status") != "available" or after.get("status") != "available":
        return {
            "status": "unavailable",
            "before": before,
            "after": after,
        }
    before_events = before.get("events", {})
    after_events = after.get("events", {})
    return {
        "status": "available",
        "path": after["path"],
        "memory_current_bytes_before": before["memory_current_bytes"],
        "memory_current_bytes_after": after["memory_current_bytes"],
        "memory_peak_bytes": after["memory_peak_bytes"],
        "event_delta": {
            key: int(after_events.get(key, 0)) - int(before_events.get(key, 0))
            for key in sorted(set(before_events) | set(after_events))
        },
    }


def _kernel_oom_evidence() -> dict[str, Any]:
    dmesg = shutil.which("dmesg")
    if dmesg is None:
        return {"status": "unavailable", "reason": "dmesg_missing"}
    try:
        completed = subprocess.run(
            [dmesg, "--color=never"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return {"status": "unavailable", "reason": type(error).__name__}
    if completed.returncode != 0:
        return {
            "status": "unavailable",
            "reason": "permission_or_command_failure",
            "exit_code": completed.returncode,
        }
    matches = tuple(
        line.strip()
        for line in completed.stdout.splitlines()
        if any(
            marker in line.casefold()
            for marker in ("out of memory", "oom-kill", "killed process")
        )
    )
    encoded = "\n".join(matches).encode()
    return {
        "status": "available",
        "matching_line_count": len(matches),
        "matching_lines_sha256": hashlib.sha256(encoded).hexdigest(),
        "matching_lines_tail": list(matches[-20:]),
    }


def _kernel_oom_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    if before.get("status") != "available" or after.get("status") != "available":
        return {"status": "unavailable", "before": before, "after": after}
    return {
        "status": "available",
        "changed": before.get("matching_lines_sha256")
        != after.get("matching_lines_sha256"),
        "matching_line_count_delta": int(after.get("matching_line_count", 0))
        - int(before.get("matching_line_count", 0)),
    }


def _process_lease_inventory() -> dict[str, Any]:
    try:
        root = user_state_root() / "process-leases"
        observation = ProcessLeaseStore(root).inspect()
    except (OSError, ValueError, ProcessLifecycleError) as error:
        return {"status": "unavailable", "reason": type(error).__name__}
    return {
        "status": "available",
        "root": str(root),
        "leases": [
            {
                "lease_id": item.lease.lease_id,
                "run_id": item.lease.run_id,
                "agent_id": item.lease.agent_id,
                "pid": item.lease.child.pid,
                "process_group_id": item.lease.child.process_group_id,
                "state": item.status.value,
            }
            for item in observation.processes
        ],
    }


def _docker_inventory(repository_root: Path) -> dict[str, Any]:
    docker = shutil.which("docker")
    if docker is None:
        return {"status": "unavailable", "reason": "docker_missing"}
    try:
        listed = subprocess.run(
            [
                docker,
                "container",
                "ls",
                "--all",
                "--quiet",
                "--filter",
                "label=openclaw.sandbox=1",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if listed.returncode != 0:
            return {
                "status": "unavailable",
                "reason": "docker_list_failed",
                "exit_code": listed.returncode,
            }
        container_ids = tuple(line for line in listed.stdout.splitlines() if line)
        if not container_ids:
            return {"status": "available", "containers": [], "volumes": []}
        inspected = subprocess.run(
            [docker, "container", "inspect", *container_ids],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if inspected.returncode != 0:
            return {
                "status": "unavailable",
                "reason": "docker_inspect_failed",
                "exit_code": inspected.returncode,
            }
        payload = json.loads(inspected.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        return {"status": "unavailable", "reason": type(error).__name__}

    try:
        state = user_state_root().resolve(strict=False)
    except ValueError as error:
        return {"status": "unavailable", "reason": type(error).__name__}
    repository = repository_root.resolve(strict=False)
    containers: list[dict[str, Any]] = []
    volumes: set[str] = set()
    for item in payload if isinstance(payload, list) else ():
        if not isinstance(item, dict):
            continue
        mounts = item.get("Mounts")
        if not isinstance(mounts, list):
            continue
        owned_mount = False
        item_volumes: set[str] = set()
        for mount in mounts:
            if not isinstance(mount, dict):
                continue
            source = mount.get("Source")
            if isinstance(source, str):
                resolved = Path(source).resolve(strict=False)
                if resolved == state or state in resolved.parents:
                    owned_mount = True
                if resolved == repository or repository in resolved.parents:
                    owned_mount = True
            if mount.get("Type") == "volume" and isinstance(mount.get("Name"), str):
                item_volumes.add(mount["Name"])
        if not owned_mount:
            continue
        volumes.update(item_volumes)
        config = item.get("Config") if isinstance(item.get("Config"), dict) else {}
        labels = config.get("Labels") if isinstance(config.get("Labels"), dict) else {}
        state_payload = item.get("State") if isinstance(item.get("State"), dict) else {}
        containers.append(
            {
                "id": item.get("Id"),
                "name": str(item.get("Name", "")).removeprefix("/"),
                "session_key": labels.get("openclaw.sessionKey"),
                "running": bool(state_payload.get("Running")),
            }
        )
    return {
        "status": "available",
        "containers": sorted(containers, key=lambda item: str(item["id"])),
        "volumes": sorted(volumes),
    }


def _pytest_state(path: Path) -> dict[str, Any]:
    return _read_json_object(path) or {
        "status": "unavailable",
        "current_node_id": None,
        "last_completed_node_id": None,
    }


def _sleep_before_deadline(deadline: float) -> bool:
    """Sleep only when one fresh clock reading leaves positive time."""

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return False
    time.sleep(min(DEFAULT_SAMPLE_INTERVAL_SECONDS, remaining))
    return True


def _terminate_owned_processes(
    process: subprocess.Popen[bytes],
    tracked: dict[tuple[int, int], ProcessSnapshot],
    *,
    grace_seconds: float,
) -> dict[str, Any]:
    actions: list[str] = []
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            actions.append("sigterm_process_group")
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        alive = _owned_processes(process.pid, process.pid, tracked=tracked)
        if not alive:
            break
        if not _sleep_before_deadline(deadline):
            break
    alive = _owned_processes(process.pid, process.pid, tracked=tracked)
    if alive:
        try:
            os.killpg(process.pid, signal.SIGKILL)
            actions.append("sigkill_process_group")
        except ProcessLookupError:
            pass
        for item in alive:
            current = _read_proc_snapshot(item.pid)
            if current is None or current.start_time_ticks != item.start_time_ticks:
                continue
            try:
                os.kill(item.pid, signal.SIGKILL)
                actions.append(f"sigkill_pid:{item.pid}")
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + grace_seconds
        while time.monotonic() < deadline:
            if not _owned_processes(process.pid, process.pid, tracked=tracked):
                break
            if not _sleep_before_deadline(deadline):
                break
    return {"actions": actions}


class FullGateSupervisor:
    """Run each stage while durably recording diagnostics and exact outcomes."""

    def __init__(
        self,
        *,
        repository_root: Path,
        evidence_root: Path,
        stages: tuple[GateStage, ...],
        stage_timeout_seconds: float = DEFAULT_STAGE_TIMEOUT_SECONDS,
        termination_grace_seconds: float = DEFAULT_TERMINATION_GRACE_SECONDS,
        sample_interval_seconds: float = DEFAULT_SAMPLE_INTERVAL_SECONDS,
        output: BinaryIO | None = None,
    ) -> None:
        if not repository_root.is_absolute() or not repository_root.is_dir():
            raise ValueError("repository root must be an existing absolute directory")
        if not evidence_root.is_absolute():
            raise ValueError("evidence root must be absolute")
        if not stages or len({stage.name for stage in stages}) != len(stages):
            raise ValueError("gate stages must be non-empty and uniquely named")
        if stage_timeout_seconds <= 0 or termination_grace_seconds <= 0:
            raise ValueError("gate timeouts must be positive")
        if sample_interval_seconds <= 0:
            raise ValueError("sample interval must be positive")
        self.repository_root = repository_root
        self.evidence_root = evidence_root
        self.stages = stages
        self.stage_timeout_seconds = stage_timeout_seconds
        self.termination_grace_seconds = termination_grace_seconds
        self.sample_interval_seconds = sample_interval_seconds
        self.output = output if output is not None else sys.stdout.buffer
        self._requested_signal: int | None = None
        self._active_process: subprocess.Popen[bytes] | None = None

    def run(self) -> tuple[int, Path]:
        self.evidence_root.mkdir(parents=True, exist_ok=True)
        self._recover_incomplete_reports()
        report_directory = self._new_report_directory()
        report_path = report_directory / REPORT_FILENAME
        cgroup_before = _read_cgroup_memory()
        kernel_before = _kernel_oom_evidence()
        owner = read_linux_process_identity(os.getpid())
        report: dict[str, Any] = {
            "schema_version": FULL_GATE_SCHEMA_VERSION,
            "report_id": report_directory.name,
            "status": FullGateStatus.RUNNING.value,
            "canonical_entrypoint": ["make", "check"],
            "supervisor_argv": list(sys.argv),
            "cwd": str(self.repository_root),
            "git": _git_fact(self.repository_root),
            "started_at": _utc_now(),
            "ended_at": None,
            "supervisor_process": (
                None
                if owner is None
                else {
                    "pid": owner.pid,
                    "process_group_id": owner.process_group_id,
                    "start_time_ticks": owner.start_time_ticks,
                }
            ),
            "stage_timeout_seconds": self.stage_timeout_seconds,
            "termination_grace_seconds": self.termination_grace_seconds,
            "stages": [
                {
                    "name": stage.name,
                    "argv": list(stage.argv),
                    "status": StageStatus.PENDING.value,
                    "started_at": None,
                    "ended_at": None,
                    "exit_code": None,
                    "signal": None,
                    "log_path": f"{stage.name}.log",
                }
                for stage in self.stages
            ],
            "pytest": _pytest_state(report_directory / PYTEST_STATE_FILENAME),
            "resources": {
                "aggregate_peak_rss_bytes": 0,
                "peak_process_count": 0,
                "peak_thread_count": 0,
                "peak_process_tree": [],
                "cgroup_before": cgroup_before,
                "kernel_oom_before": kernel_before,
            },
            "post_run_inventory": None,
        }
        _atomic_write_json(report_path, report)
        previous_handlers = self._install_signal_handlers()
        exit_code = 0
        try:
            for index, stage in enumerate(self.stages):
                if exit_code != 0 or self._requested_signal is not None:
                    report["stages"][index]["status"] = StageStatus.SKIPPED.value
                    continue
                exit_code = self._run_stage(
                    stage,
                    report["stages"][index],
                    report,
                    report_path,
                    report_directory,
                )
            if self._requested_signal is not None:
                report["status"] = FullGateStatus.INTERRUPTED.value
                report["interruption_signal"] = self._requested_signal
                exit_code = 128 + self._requested_signal
            elif exit_code == 0:
                report["status"] = FullGateStatus.COMPLETED.value
            else:
                report["status"] = FullGateStatus.FAILED.value
        except BaseException as error:
            report["status"] = FullGateStatus.INTERRUPTED.value
            report["supervisor_error"] = type(error).__name__
            exit_code = exit_code or 1
            raise
        finally:
            self._restore_signal_handlers(previous_handlers)
            report["ended_at"] = _utc_now()
            report["pytest"] = _pytest_state(report_directory / PYTEST_STATE_FILENAME)
            cgroup_after = _read_cgroup_memory()
            report["resources"]["cgroup"] = _cgroup_delta(cgroup_before, cgroup_after)
            kernel_after = _kernel_oom_evidence()
            report["resources"]["kernel_oom_after"] = kernel_after
            report["resources"]["kernel_oom_delta"] = _kernel_oom_delta(
                kernel_before, kernel_after
            )
            report["post_run_inventory"] = self._post_run_inventory(report["stages"])
            _atomic_write_json(report_path, report)
            _atomic_write_json(
                self.evidence_root / "latest.json",
                {
                    "schema_version": FULL_GATE_SCHEMA_VERSION,
                    "report_id": report["report_id"],
                    "status": report["status"],
                    "report_path": str(report_path.relative_to(self.evidence_root)),
                },
            )
        self.output.write(
            f"full-gate: {report['status']} evidence={report_path}\n".encode()
        )
        self.output.flush()
        return exit_code, report_path

    def _new_report_directory(self) -> Path:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        directory = self.evidence_root / f"{stamp}-{uuid4().hex[:12]}"
        directory.mkdir(mode=0o700)
        return directory

    def _recover_incomplete_reports(self) -> None:
        for path in sorted(self.evidence_root.glob(f"*/{REPORT_FILENAME}")):
            report = _read_json_object(path)
            if report is None or report.get("status") != FullGateStatus.RUNNING.value:
                continue
            process = report.get("supervisor_process")
            if not isinstance(process, dict):
                alive = False
            else:
                try:
                    current = read_linux_process_identity(int(process["pid"]))
                    alive = current is not None and current.start_time_ticks == int(
                        process["start_time_ticks"]
                    )
                except (KeyError, TypeError, ValueError):
                    alive = False
            if alive:
                continue
            report["status"] = FullGateStatus.INCOMPLETE_OBSERVED_ON_RECOVERY.value
            report["ended_at"] = None
            report["recovered_at"] = _utc_now()
            report["incomplete_reason"] = (
                "supervisor_disappeared_without_terminal_record"
            )
            _atomic_write_json(path, report)

    def _install_signal_handlers(self) -> dict[int, Any]:
        handlers: dict[int, Any] = {}

        def request_stop(signum: int, frame: Any) -> None:
            del frame
            if self._requested_signal is None:
                self._requested_signal = signum
                process = self._active_process
                if process is not None and process.poll() is None:
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGTERM)

        for signum in (signal.SIGINT, signal.SIGTERM):
            handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, request_stop)
        return handlers

    @staticmethod
    def _restore_signal_handlers(handlers: dict[int, Any]) -> None:
        for signum, handler in handlers.items():
            signal.signal(signum, handler)

    def _run_stage(
        self,
        stage: GateStage,
        stage_record: dict[str, Any],
        report: dict[str, Any],
        report_path: Path,
        report_directory: Path,
    ) -> int:
        stage_record["status"] = StageStatus.RUNNING.value
        stage_record["started_at"] = _utc_now()
        _atomic_write_json(report_path, report)
        self.output.write(f"full-gate: stage={stage.name}\n".encode())
        self.output.flush()
        environment = os.environ.copy()
        if stage.environment:
            environment.update(stage.environment)
        if stage.name == "test":
            environment["SAT_FULL_GATE_PYTEST_STATE"] = str(
                report_directory / PYTEST_STATE_FILENAME
            )
        tracked: dict[tuple[int, int], ProcessSnapshot] = {}
        log_path = report_directory / f"{stage.name}.log"
        with log_path.open("wb") as log:
            try:
                process = subprocess.Popen(
                    stage.argv,
                    cwd=self.repository_root,
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            except OSError as error:
                stage_record["status"] = StageStatus.FAILED.value
                stage_record["ended_at"] = _utc_now()
                stage_record["launch_error"] = type(error).__name__
                _atomic_write_json(report_path, report)
                return 1
            self._active_process = process
            assert process.stdout is not None
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ)
            started = time.monotonic()
            termination: dict[str, Any] | None = None
            while True:
                for key, _ in selector.select(self.sample_interval_seconds):
                    chunk = os.read(key.fileobj.fileno(), 65_536)
                    if chunk:
                        self.output.write(chunk)
                        self.output.flush()
                        log.write(chunk)
                        log.flush()
                    else:
                        selector.unregister(key.fileobj)
                tree = _owned_processes(process.pid, process.pid, tracked=tracked)
                self._update_resource_peak(report, tree)
                if stage.name == "test":
                    report["pytest"] = _pytest_state(
                        report_directory / PYTEST_STATE_FILENAME
                    )
                if self._requested_signal is not None and termination is None:
                    termination = _terminate_owned_processes(
                        process,
                        tracked,
                        grace_seconds=self.termination_grace_seconds,
                    )
                if (
                    time.monotonic() - started >= self.stage_timeout_seconds
                    and termination is None
                ):
                    stage_record["status"] = StageStatus.TIMED_OUT.value
                    termination = _terminate_owned_processes(
                        process,
                        tracked,
                        grace_seconds=self.termination_grace_seconds,
                    )
                if process.poll() is not None and not selector.get_map():
                    break
                _atomic_write_json(report_path, report)
            selector.close()
            return_code = process.wait()
            self._active_process = None
        residual_before = _owned_processes(process.pid, process.pid, tracked=tracked)
        residual_cleanup: dict[str, Any] = termination or {"actions": []}
        if residual_before:
            later_cleanup = _terminate_owned_processes(
                process,
                tracked,
                grace_seconds=self.termination_grace_seconds,
            )
            residual_cleanup["actions"].extend(later_cleanup["actions"])
        residual_after = _owned_processes(process.pid, process.pid, tracked=tracked)
        stage_record["ended_at"] = _utc_now()
        stage_record["exit_code"] = return_code if return_code >= 0 else None
        stage_record["signal"] = -return_code if return_code < 0 else None
        stage_record["process"] = {
            "root_pid": process.pid,
            "tracked_identity_count": len(tracked),
            "residual_before_cleanup": [item.as_json() for item in residual_before],
            "cleanup": residual_cleanup,
            "residual_after_cleanup": [item.as_json() for item in residual_after],
        }
        if self._requested_signal is not None:
            stage_record["status"] = StageStatus.INTERRUPTED.value
        elif stage_record["status"] == StageStatus.TIMED_OUT.value:
            pass
        elif return_code == 0 and not residual_before:
            stage_record["status"] = StageStatus.COMPLETED.value
        else:
            stage_record["status"] = StageStatus.FAILED.value
        _atomic_write_json(report_path, report)
        if stage_record["status"] == StageStatus.COMPLETED.value:
            return 0
        if self._requested_signal is not None:
            return 128 + self._requested_signal
        if stage_record["status"] == StageStatus.TIMED_OUT.value:
            return 124
        return return_code if return_code > 0 else 1

    @staticmethod
    def _update_resource_peak(
        report: dict[str, Any], tree: tuple[ProcessSnapshot, ...]
    ) -> None:
        resources = report["resources"]
        rss = sum(item.rss_bytes for item in tree)
        threads = sum(item.thread_count for item in tree)
        if rss > resources["aggregate_peak_rss_bytes"]:
            resources["aggregate_peak_rss_bytes"] = rss
            resources["peak_process_tree"] = [item.as_json() for item in tree]
        elif tree and not resources["peak_process_tree"]:
            resources["peak_process_tree"] = [item.as_json() for item in tree]
        resources["peak_process_count"] = max(
            resources["peak_process_count"], len(tree)
        )
        resources["peak_thread_count"] = max(resources["peak_thread_count"], threads)

    def _post_run_inventory(self, stages: list[dict[str, Any]]) -> dict[str, Any]:
        own_threads = []
        with suppress(OSError, ValueError):
            own_threads = sorted(
                int(path.name) for path in Path("/proc/self/task").iterdir()
            )
        return {
            "process_leases": _process_lease_inventory(),
            "supervisor_threads": {
                "status": "available" if own_threads else "unavailable",
                "thread_ids": own_threads,
            },
            "stage_processes": [
                {
                    "stage": item["name"],
                    "residual_after_cleanup": item.get("process", {}).get(
                        "residual_after_cleanup", []
                    ),
                }
                for item in stages
            ],
            "docker": _docker_inventory(self.repository_root),
        }


def canonical_stages(repository_root: Path, uv_binary: Path) -> tuple[GateStage, ...]:
    """Return the single authoritative ordered repository gate."""

    return (
        GateStage("doctor", (str(repository_root / "scripts/doctor.sh"),)),
        GateStage(
            "format-check",
            (str(uv_binary), "run", "--frozen", "ruff", "format", "--check", "."),
        ),
        GateStage(
            "lint",
            (str(uv_binary), "run", "--frozen", "ruff", "check", "."),
        ),
        GateStage(
            "test",
            (
                str(uv_binary),
                "run",
                "--frozen",
                "pytest",
                "-vv",
                "-p",
                "software_agent_team.full_gate_pytest_plugin",
            ),
        ),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the canonical repository gate with durable diagnostics."
    )
    parser.add_argument(
        "--evidence-root",
        type=Path,
        help="ignored output root (defaults to artifacts/generated/full-gate)",
    )
    parser.add_argument(
        "--stage-timeout-seconds",
        type=float,
        default=DEFAULT_STAGE_TIMEOUT_SECONDS,
        help="developer-gate infrastructure ceiling for one stage",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the canonical full gate and return its real terminal status."""

    arguments = _parser().parse_args(argv)
    repository_root = Path.cwd().resolve()
    evidence_root = (
        arguments.evidence_root.resolve()
        if arguments.evidence_root is not None
        else repository_root / "artifacts/generated/full-gate"
    )
    uv_binary = Path(os.environ.get("SAT_UV_BIN", Path.home() / ".local/bin/uv"))
    supervisor = FullGateSupervisor(
        repository_root=repository_root,
        evidence_root=evidence_root,
        stages=canonical_stages(repository_root, uv_binary),
        stage_timeout_seconds=arguments.stage_timeout_seconds,
    )
    exit_code, _ = supervisor.run()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
