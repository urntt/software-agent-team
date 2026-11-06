"""Host capacity discovery and sandbox concurrency headroom limits.

宿主机容量探测与沙箱并发上限约束。
基于 Linux /proc/meminfo 与 cgroup v2 控制组评估内存和 PID 余量;
确定性约束 Agent 运行并发上限并避免资源耗尽。
"""

from __future__ import annotations

import re
from pathlib import Path

from .delivery import _read_text
from .types import (
    FileReader,
    HostCapacitySnapshot,
    HostConcurrencyLimit,
)


def _nonnegative_integer(value: str) -> int | None:
    cleaned = value.strip()
    if not cleaned.isascii() or not cleaned.isdigit():
        return None
    return int(cleaned)


def _minimum_capacity(
    candidates: list[tuple[int, str]],
) -> tuple[int | None, tuple[str, ...]]:
    if not candidates:
        return None, ()
    minimum = min(value for value, _source in candidates)
    sources = tuple(source for value, source in candidates if value == minimum)
    return minimum, sources


def inspect_host_capacity(
    *,
    file_reader: FileReader = _read_text,
) -> HostCapacitySnapshot:
    """Discover effective memory and PID headroom without changing the host."""

    memory_candidates: list[tuple[int, str]] = []
    pid_candidates: list[tuple[int, str]] = []
    pids_unbounded = False

    try:
        meminfo = file_reader(Path("/proc/meminfo"))
    except OSError:
        meminfo = ""
    match = re.search(r"(?m)^MemAvailable:\s+([0-9]+)\s+kB\s*$", meminfo)
    if match is not None:
        memory_candidates.append((int(match.group(1)) * 1024, "/proc/meminfo"))

    try:
        cgroup = file_reader(Path("/proc/self/cgroup"))
    except OSError:
        cgroup = ""
    relative: Path | None = None
    for line in cgroup.splitlines():
        parts = line.split(":", 2)
        if len(parts) == 3 and parts[0] == "0" and parts[1] == "":
            candidate = Path(parts[2].lstrip("/"))
            if ".." not in candidate.parts:
                relative = candidate
            break

    if relative is not None:
        cgroup_root = Path("/sys/fs/cgroup")
        current = cgroup_root / relative
        paths: list[Path] = []
        while current == cgroup_root or current.is_relative_to(cgroup_root):
            paths.append(current)
            if current == cgroup_root:
                break
            current = current.parent
        saw_unbounded_pid_limit = False
        saw_pid_limit = False
        for path in paths:
            try:
                memory_max = _nonnegative_integer(file_reader(path / "memory.max"))
                memory_current = _nonnegative_integer(
                    file_reader(path / "memory.current")
                )
            except OSError:
                memory_max = None
                memory_current = None
            if memory_max is not None and memory_current is not None:
                memory_candidates.append(
                    (max(0, memory_max - memory_current), str(path / "memory.max"))
                )

            try:
                raw_pids_max = file_reader(path / "pids.max").strip()
                pids_current = _nonnegative_integer(file_reader(path / "pids.current"))
            except OSError:
                raw_pids_max = ""
                pids_current = None
            if raw_pids_max == "max" and pids_current is not None:
                saw_unbounded_pid_limit = True
            else:
                pids_max = _nonnegative_integer(raw_pids_max)
                if pids_max is not None and pids_current is not None:
                    saw_pid_limit = True
                    pid_candidates.append(
                        (max(0, pids_max - pids_current), str(path / "pids.max"))
                    )
        pids_unbounded = saw_unbounded_pid_limit and not saw_pid_limit

    memory_value, memory_sources = _minimum_capacity(memory_candidates)
    pid_value, pid_sources = _minimum_capacity(pid_candidates)
    return HostCapacitySnapshot(
        available_memory_bytes=memory_value,
        available_pids=pid_value,
        pids_unbounded=pids_unbounded,
        memory_sources=memory_sources,
        pid_sources=pid_sources,
    )


def limit_concurrency_to_host_capacity(
    *,
    configured_max_concurrency: int,
    capacity: HostCapacitySnapshot,
    sandbox_memory_mb: int,
    sandbox_pids: int,
) -> HostConcurrencyLimit:
    """Lower one run's concurrency when current headroom cannot fund each sandbox."""

    if (
        isinstance(configured_max_concurrency, bool)
        or configured_max_concurrency < 1
        or isinstance(sandbox_memory_mb, bool)
        or sandbox_memory_mb < 1
        or isinstance(sandbox_pids, bool)
        or sandbox_pids < 1
    ):
        raise ValueError("concurrency and sandbox resource ceilings must be positive")

    memory_slots = (
        None
        if capacity.available_memory_bytes is None
        else capacity.available_memory_bytes // (sandbox_memory_mb * 1024 * 1024)
    )
    pid_slots = (
        None
        if capacity.pids_unbounded or capacity.available_pids is None
        else capacity.available_pids // sandbox_pids
    )
    supported = configured_max_concurrency
    for slots in (memory_slots, pid_slots):
        if slots is not None:
            supported = min(supported, max(1, slots))
    return HostConcurrencyLimit(
        configured_max_concurrency=configured_max_concurrency,
        effective_max_concurrency=supported,
        memory_slots=memory_slots,
        pid_slots=pid_slots,
    )
