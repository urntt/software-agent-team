"""Content-free Linux process observations; never a readiness authority."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from software_agent_team.process_lifecycle import (
    ProcessIdentity,
    ProcessLifecycleError,
    read_linux_process_identity,
)


class ProcessWaitSnapshot(BaseModel):
    """One identity-checked observation, not proof of useful work or a cause."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    identity: ProcessIdentity
    status: Literal["observed", "identity_changed", "unavailable"]
    state: str | None = Field(default=None, pattern=r"^[A-Za-z]$")
    user_ticks: int | None = Field(default=None, ge=0)
    system_ticks: int | None = Field(default=None, ge=0)
    minor_faults: int | None = Field(default=None, ge=0)
    major_faults: int | None = Field(default=None, ge=0)
    rss_kib: int | None = Field(default=None, ge=0)
    swap_kib: int | None = Field(default=None, ge=0)
    read_bytes: int | None = Field(default=None, ge=0)
    write_bytes: int | None = Field(default=None, ge=0)
    wait_channel: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_]+$")
    unavailable_fields: tuple[str, ...] = ()

    @model_validator(mode="after")
    def require_attributed_metrics(self) -> Self:
        core = (
            self.state,
            self.user_ticks,
            self.system_ticks,
            self.minor_faults,
            self.major_faults,
        )
        optional = {
            "rss_kib": self.rss_kib,
            "swap_kib": self.swap_kib,
            "read_bytes": self.read_bytes,
            "write_bytes": self.write_bytes,
            "wait_channel": self.wait_channel,
        }
        if self.status != "observed":
            if (
                any(value is not None for value in (*core, *optional.values()))
                or self.unavailable_fields
            ):
                raise ValueError("unattributed processes cannot carry metrics")
        elif any(value is None for value in core) or (
            set(self.unavailable_fields)
            != {name for name, value in optional.items() if value is None}
            or len(self.unavailable_fields) != len(set(self.unavailable_fields))
        ):
            raise ValueError("observed processes require explicit metric availability")
        return self


def _read_proc_field(path: Path) -> str:
    """Read only a bounded procfs field without following a leaf symlink."""

    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
        content = stream.read(16_385)
    if len(content) > 16_384:
        raise ValueError("procfs diagnostic field exceeds its bound")
    return content


def snapshot_process_wait(
    identity: ProcessIdentity, *, expected_uid: int
) -> ProcessWaitSnapshot:
    """Discard all metrics if UID or exact process identity changes mid-read."""

    base = Path(f"/proc/{identity.pid}")
    try:
        before = read_linux_process_identity(identity.pid)
        if before is None:
            return ProcessWaitSnapshot(identity=identity, status="unavailable")
        if before != identity or base.stat().st_uid != expected_uid:
            return ProcessWaitSnapshot(identity=identity, status="identity_changed")
        raw = _read_proc_field(base / "stat")
        fields = raw[raw.rindex(")") + 2 :].split()
        if int(fields[19]) != identity.start_time_ticks:
            return ProcessWaitSnapshot(identity=identity, status="identity_changed")
        metrics: dict[str, object] = {
            "state": fields[0],
            "minor_faults": int(fields[7]),
            "major_faults": int(fields[9]),
            "user_ticks": int(fields[11]),
            "system_ticks": int(fields[12]),
        }
        unavailable: list[str] = []
        for filename, selected in (
            ("status", {"VmRSS": "rss_kib", "VmSwap": "swap_kib"}),
            ("io", {"read_bytes": "read_bytes", "write_bytes": "write_bytes"}),
        ):
            parsed: dict[str, int] = {}
            try:
                for line in _read_proc_field(base / filename).splitlines():
                    key, _, value = line.partition(":")
                    if key in selected:
                        number = int(value.split()[0])
                        if number < 0:
                            raise ValueError("negative procfs metric")
                        parsed[selected[key]] = number
            except (OSError, ValueError, IndexError, UnicodeError):
                parsed = {}
            metrics.update(parsed)
            unavailable.extend(name for name in selected.values() if name not in parsed)
        try:
            wait = _read_proc_field(base / "wchan").strip()
            if wait == "0" or re.fullmatch(r"[A-Za-z0-9_]+", wait) is None:
                raise ValueError("kernel wait channel unavailable")
            metrics["wait_channel"] = wait
        except (OSError, ValueError, UnicodeError):
            unavailable.append("wait_channel")
        if (
            read_linux_process_identity(identity.pid) != identity
            or base.stat().st_uid != expected_uid
        ):
            return ProcessWaitSnapshot(identity=identity, status="identity_changed")
        return ProcessWaitSnapshot(
            identity=identity,
            status="observed",
            unavailable_fields=tuple(unavailable),
            **metrics,
        )
    except (OSError, ValueError, IndexError, UnicodeError, ProcessLifecycleError):
        return ProcessWaitSnapshot(identity=identity, status="unavailable")
