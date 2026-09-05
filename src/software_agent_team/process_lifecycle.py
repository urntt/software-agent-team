"""Durable ownership and recovery for SAT-launched provider subprocesses."""

from __future__ import annotations

import hashlib
import json
import os
import select
import signal
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PROCESS_LEASE_SCHEMA_VERSION = 1
MAX_PROCESS_LEASE_BYTES = 65_536


class ProcessLifecycleError(RuntimeError):
    """Raised when SAT cannot prove process ownership or recovery."""


class ProcessIdentity(BaseModel):
    """PID identity that remains safe when the kernel later reuses the PID."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pid: int = Field(ge=1)
    process_group_id: int = Field(ge=1)
    start_time_ticks: int = Field(ge=1)


class InvocationProcessLease(BaseModel):
    """One durable claim for an exact SAT-launched OpenClaw invocation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[PROCESS_LEASE_SCHEMA_VERSION] = PROCESS_LEASE_SCHEMA_VERSION
    lease_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    run_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    agent_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    session_key: str = Field(min_length=1, max_length=1000)
    owner: ProcessIdentity
    child: ProcessIdentity
    command_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def require_aware_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("process lease time must include a UTC offset")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def require_isolated_child_group(self) -> InvocationProcessLease:
        if self.child.process_group_id != self.child.pid:
            raise ValueError("leased child must own its isolated process group")
        return self


class ProcessLeaseStatus(StrEnum):
    """Observed relationship between one lease and current kernel state."""

    ACTIVE = "active"
    ORPHANED = "orphaned"
    STALE = "stale"


class ObservedInvocationProcess(BaseModel):
    """Current, non-secret observation of one persisted invocation lease."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    lease: InvocationProcessLease
    status: ProcessLeaseStatus


class ProcessResourceObservation(BaseModel):
    """Read-only snapshot of every persisted SAT invocation lease."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    processes: tuple[ObservedInvocationProcess, ...]

    @property
    def active(self) -> tuple[ObservedInvocationProcess, ...]:
        return tuple(
            item for item in self.processes if item.status is ProcessLeaseStatus.ACTIVE
        )

    @property
    def orphaned(self) -> tuple[ObservedInvocationProcess, ...]:
        return tuple(
            item
            for item in self.processes
            if item.status is ProcessLeaseStatus.ORPHANED
        )

    @property
    def stale(self) -> tuple[ObservedInvocationProcess, ...]:
        return tuple(
            item for item in self.processes if item.status is ProcessLeaseStatus.STALE
        )


class ProcessRecoveryResult(BaseModel):
    """Exact invocation leases reclaimed by one explicit recovery action."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reclaimed: tuple[InvocationProcessLease, ...]
    active: tuple[InvocationProcessLease, ...]


ProcessIdentityReader = Callable[[int], ProcessIdentity | None]
Clock = Callable[[], datetime]


def read_linux_process_identity(pid: int) -> ProcessIdentity | None:
    """Read a Linux process identity from procfs without trusting PID alone."""

    if isinstance(pid, bool) or pid < 1:
        raise ValueError("process identity requires a positive PID")
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except (FileNotFoundError, ProcessLookupError):
        return None
    except OSError as error:
        raise ProcessLifecycleError(f"could not inspect process {pid}") from error
    closing = raw.rfind(")")
    if closing < 1:
        raise ProcessLifecycleError(f"process {pid} has invalid procfs identity")
    try:
        recorded_pid = int(raw[: raw.find(" ")])
        fields = raw[closing + 2 :].split()
        process_group_id = int(fields[2])
        start_time_ticks = int(fields[19])
    except (IndexError, ValueError) as error:
        raise ProcessLifecycleError(
            f"process {pid} has invalid procfs identity"
        ) from error
    if recorded_pid != pid:
        raise ProcessLifecycleError(f"process {pid} procfs identity disagrees")
    return ProcessIdentity(
        pid=pid,
        process_group_id=process_group_id,
        start_time_ticks=start_time_ticks,
    )


def _command_sha256(command: Sequence[str]) -> str:
    encoded = json.dumps(
        list(command),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class ProcessLeaseStore:
    """Private write-once invocation leases with PID-reuse-safe observation."""

    def __init__(
        self,
        root: Path,
        *,
        identity_reader: ProcessIdentityReader = read_linux_process_identity,
        clock: Clock = lambda: datetime.now(UTC),
    ) -> None:
        if not root.is_absolute() or root == Path(root.anchor):
            raise ProcessLifecycleError(
                "process lease root must be a specific absolute path"
            )
        self.root = root
        self.identity_reader = identity_reader
        self.clock = clock

    def acquire(
        self,
        *,
        run_id: str,
        agent_id: str,
        session_key: str,
        child_pid: int,
        command: Sequence[str],
    ) -> InvocationProcessLease:
        """Persist ownership immediately after an isolated child is spawned."""

        owner = self.identity_reader(os.getpid())
        child = self.identity_reader(child_pid)
        if owner is None:
            raise ProcessLifecycleError("SAT controller process identity disappeared")
        if child is None:
            raise ProcessLifecycleError("OpenClaw child exited before ownership")
        lease = InvocationProcessLease(
            lease_id=uuid4().hex,
            run_id=run_id,
            agent_id=agent_id,
            session_key=session_key,
            owner=owner,
            child=child,
            command_sha256=_command_sha256(command),
            created_at=self.clock(),
        )
        self._ensure_root()
        destination = self._path(lease.lease_id)
        content = (
            json.dumps(lease.model_dump(mode="json"), ensure_ascii=False, indent=2)
            + "\n"
        ).encode()
        temporary = self.root / f".{lease.lease_id}.{uuid4().hex}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, destination)
            _fsync_directory(self.root)
        finally:
            temporary.unlink(missing_ok=True)
        return lease

    def release(self, lease: InvocationProcessLease) -> None:
        """Remove exactly the lease acquired for a now-terminal child."""

        path = self._path(lease.lease_id)
        persisted = self._load(path)
        if persisted != lease:
            raise ProcessLifecycleError("process lease changed before release")
        path.unlink()
        _fsync_directory(self.root)

    def inspect(self) -> ProcessResourceObservation:
        """Classify leases without changing processes or evidence."""

        if not self.root.exists():
            return ProcessResourceObservation(processes=())
        self._validate_root()
        observed: list[ObservedInvocationProcess] = []
        for path in sorted(self.root.glob("*.json")):
            lease = self._load(path)
            child = self.identity_reader(lease.child.pid)
            owner = self.identity_reader(lease.owner.pid)
            status = (
                ProcessLeaseStatus.ACTIVE
                if owner == lease.owner
                else ProcessLeaseStatus.ORPHANED
                if child == lease.child
                else ProcessLeaseStatus.STALE
            )
            observed.append(ObservedInvocationProcess(lease=lease, status=status))
        return ProcessResourceObservation(processes=tuple(observed))

    def reclaim_orphans(
        self,
        *,
        lease_ids: Sequence[str] | None = None,
        grace_seconds: float = 5.0,
        release_leases: bool = True,
    ) -> ProcessRecoveryResult:
        """Terminate only proven orphans and discard only proven stale leases."""

        if grace_seconds <= 0:
            raise ValueError("process recovery grace must be positive")
        observation = self.inspect()
        requested = (
            tuple(item.lease.lease_id for item in observation.processes)
            if lease_ids is None
            else tuple(lease_ids)
        )
        if not requested and lease_ids is None:
            return ProcessRecoveryResult(reclaimed=(), active=())
        selected = set(requested)
        if not requested or len(selected) != len(requested):
            raise ProcessLifecycleError(
                "process recovery requires unique persisted lease IDs"
            )
        known = {item.lease.lease_id for item in observation.processes}
        if unknown := selected - known:
            raise ProcessLifecycleError(
                "process recovery lease disappeared or was replaced: "
                + ", ".join(sorted(unknown))
            )
        reclaimed: list[InvocationProcessLease] = []
        active = tuple(
            item.lease for item in observation.active if item.lease.lease_id in selected
        )
        for item in observation.stale:
            if item.lease.lease_id not in selected:
                continue
            if release_leases:
                self.release(item.lease)
            reclaimed.append(item.lease)
        for item in observation.orphaned:
            if item.lease.lease_id not in selected:
                continue
            lease = item.lease
            if self.identity_reader(lease.child.pid) != lease.child:
                if release_leases:
                    self.release(lease)
                reclaimed.append(lease)
                continue
            pidfd_open = getattr(os, "pidfd_open", None)
            if not callable(pidfd_open):
                raise ProcessLifecycleError(
                    "safe orphan recovery requires Linux pidfd support"
                )
            try:
                pidfd = pidfd_open(lease.child.pid, 0)
            except OSError as error:
                raise ProcessLifecycleError(
                    f"could not pin orphaned process {lease.child.pid}"
                ) from error
            try:
                if self.identity_reader(lease.child.pid) != lease.child:
                    raise ProcessLifecycleError(
                        "orphaned process identity changed before recovery"
                    )
                try:
                    os.killpg(lease.child.process_group_id, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                except OSError as error:
                    raise ProcessLifecycleError(
                        "could not stop orphaned process group "
                        f"{lease.child.process_group_id}"
                    ) from error
                exited = bool(select.select((pidfd,), (), (), grace_seconds)[0])
                if not exited:
                    try:
                        os.killpg(lease.child.process_group_id, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    except OSError as error:
                        raise ProcessLifecycleError(
                            "could not kill orphaned process group "
                            f"{lease.child.process_group_id}"
                        ) from error
                    exited = bool(select.select((pidfd,), (), (), grace_seconds)[0])
                if not exited:
                    raise ProcessLifecycleError(
                        f"orphaned process {lease.child.pid} remained alive"
                    )
            finally:
                os.close(pidfd)
            if release_leases:
                self.release(lease)
            reclaimed.append(lease)
        return ProcessRecoveryResult(reclaimed=tuple(reclaimed), active=active)

    def _ensure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._validate_root()
        self.root.chmod(0o700)

    def _validate_root(self) -> None:
        if self.root.is_symlink() or not self.root.is_dir():
            raise ProcessLifecycleError("process lease root must be a real directory")

    def _path(self, lease_id: str) -> Path:
        if len(lease_id) != 32 or any(
            character not in "0123456789abcdef" for character in lease_id
        ):
            raise ProcessLifecycleError("process lease ID is invalid")
        return self.root / f"{lease_id}.json"

    def _load(self, path: Path) -> InvocationProcessLease:
        if path.is_symlink() or not path.is_file():
            raise ProcessLifecycleError(f"process lease is not a regular file: {path}")
        try:
            raw = path.read_bytes()
            if len(raw) > MAX_PROCESS_LEASE_BYTES:
                raise ProcessLifecycleError("process lease exceeds the read limit")
            lease = InvocationProcessLease.model_validate_json(raw)
        except (OSError, ValueError) as error:
            raise ProcessLifecycleError(f"process lease is invalid: {path}") from error
        if path != self._path(lease.lease_id):
            raise ProcessLifecycleError("process lease path and identity disagree")
        return lease
