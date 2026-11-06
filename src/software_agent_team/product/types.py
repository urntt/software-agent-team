"""Product flow domain models, command contracts, and diagnostic states.

产品引导流领域模型、命令契约以及诊断状态。
定义了产品流异常、启动诊断实体、容量快照模型、项目清单命令规范及私有状态路径。
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from software_agent_team.state_layout import state_category_paths

MINIMUM_FREE_BYTES = 1_073_741_824
PROJECT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SUPPORTED_ARCHITECTURES = {"aarch64", "amd64", "arm64", "x86_64"}
AT_FDCWD = -100
RENAME_NOREPLACE = 1
PROJECT_MANIFEST_NAME = "sat-project.json"
_MAX_PROJECT_MANIFEST_BYTES = 65_536

CommandRunner = Callable[[Sequence[str], float], subprocess.CompletedProcess[str]]
FileReader = Callable[[Path], str]


class ProductFlowError(RuntimeError):
    """Raised when the guided product flow cannot continue safely."""


class DiagnosticState(StrEnum):
    """User-facing readiness state for one startup condition."""

    READY = "ready"
    WARNING = "warning"
    ACTION_REQUIRED = "action_required"


@dataclass(frozen=True)
class DiagnosticCheck:
    """One startup condition and its concrete corrective action."""

    id: str
    label: str
    state: DiagnosticState
    detail: str
    action: str | None = None


@dataclass(frozen=True)
class StartupDiagnostics:
    """Ordered local startup checks for the primary product flow."""

    checks: tuple[DiagnosticCheck, ...]
    host_capacity: HostCapacitySnapshot | None = None

    @property
    def ready(self) -> bool:
        return all(
            check.state is not DiagnosticState.ACTION_REQUIRED for check in self.checks
        )


@dataclass(frozen=True)
class HostCapacitySnapshot:
    """Current process memory and PID capacity discovered from Linux controls."""

    available_memory_bytes: int | None
    available_pids: int | None
    pids_unbounded: bool
    memory_sources: tuple[str, ...]
    pid_sources: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.available_memory_bytes is not None and self.available_memory_bytes < 0:
            raise ValueError("available memory cannot be negative")
        if self.available_pids is not None and self.available_pids < 0:
            raise ValueError("available PIDs cannot be negative")
        if self.pids_unbounded and self.available_pids is not None:
            raise ValueError("unbounded PID capacity cannot also be finite")


@dataclass(frozen=True)
class HostConcurrencyLimit:
    """Run-scoped Agent concurrency supported by current host headroom."""

    configured_max_concurrency: int
    effective_max_concurrency: int
    memory_slots: int | None
    pid_slots: int | None

    def __post_init__(self) -> None:
        if self.configured_max_concurrency < 1:
            raise ValueError("configured concurrency must be positive")
        if not 1 <= self.effective_max_concurrency <= self.configured_max_concurrency:
            raise ValueError(
                "effective concurrency must preserve the configured ceiling"
            )
        if self.memory_slots is not None and self.memory_slots < 0:
            raise ValueError("memory slots cannot be negative")
        if self.pid_slots is not None and self.pid_slots < 0:
            raise ValueError("PID slots cannot be negative")

    @property
    def constrained(self) -> bool:
        return self.effective_max_concurrency < self.configured_max_concurrency


class ProjectCommands(BaseModel):
    """Validated non-shell commands delivered with one generated project."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    setup: tuple[str, ...] = Field(min_length=3, max_length=32)
    start: tuple[str, ...] = Field(min_length=3, max_length=32)
    test: tuple[str, ...] = Field(min_length=3, max_length=32)

    @field_validator("setup", "start", "test")
    @classmethod
    def require_safe_argv(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Reject blank, multiline, oversized, or shell-based commands."""

        if any(
            not value
            or len(value) > 1024
            or any(character in value for character in ("\x00", "\r", "\n"))
            for value in values
        ):
            raise ValueError("project commands require non-empty single-line argv")
        if sum(len(value) for value in values) > 4096:
            raise ValueError("project command argv is too large")
        if values[0] != "uv" or (
            values[1] != "run"
            and values
            != (
                "uv",
                "sync",
                "--dev",
            )
        ):
            raise ValueError("project commands must use the versioned uv environment")
        if values[2] in {
            "bash",
            "cmd",
            "cmd.exe",
            "dash",
            "fish",
            "ksh",
            "powershell",
            "pwsh",
            "sh",
            "zsh",
        }:
            raise ValueError("project commands cannot invoke a command shell")
        return values

    @model_validator(mode="after")
    def require_profile_contract(self) -> Self:
        """Keep setup and test reproducible while allowing a project-specific start."""

        if self.setup != ("uv", "sync", "--dev"):
            raise ValueError("setup command must be: uv sync --dev")
        if self.start[:2] != ("uv", "run"):
            raise ValueError("start command must begin with: uv run")
        if "replace-with-project-entrypoint" in self.start:
            raise ValueError("start command still contains the starter placeholder")
        if self.test != ("uv", "run", "pytest"):
            raise ValueError("test command must be: uv run pytest")
        return self


@dataclass(frozen=True)
class ProductStatePaths:
    """Run-scoped roots beneath the user-local SAT state directory."""

    root: Path
    runs: Path
    workspaces: Path
    sources: Path
    planning: Path
    self_checks: Path
    process_leases: Path
    openclaw: Path

    @classmethod
    def below(cls, root: Path) -> ProductStatePaths:
        return cls(root=root, **state_category_paths(root))
