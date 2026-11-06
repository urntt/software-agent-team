"""Domain types, models, and exceptions for deterministic quality gates.

Provides core domain models and policy configuration for quality gates:
- Exceptions: QualityGateError, QualityGateConfigurationError, etc.
- Run policies and limits: RunPolicy, SandboxLimits, DockerSandboxPolicy.
- Quality gate manifests: ReadOnlyInputMount, QualityGateDefinition, etc.
- Execution contracts: ResolvedInputMount, QualityGateConfiguration, etc.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from software_agent_team.artifacts import AgentRole, TaskBrief
from software_agent_team.budgets import AgentBudget

QUALITY_GATE_SCHEMA_VERSION = 1

_SHELL_EXECUTABLES = {
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
}
_INLINE_CODE_FLAGS = {
    "node": {"-e", "--eval"},
    "nodejs": {"-e", "--eval"},
    "python": {"-c"},
    "python3": {"-c"},
}
_CONTAINER_INPUT_ROOT = PurePosixPath("/opt/software-agent-team/inputs")


class QualityGateError(RuntimeError):
    """Base error for quality-gate configuration and execution."""


class QualityGateConfigurationError(QualityGateError):
    """A policy, benchmark manifest, or runtime path is unsafe or incoherent."""


class SandboxUnavailableError(QualityGateError):
    """The configured sandbox runtime cannot execute a gate."""


class QualityGateEvidenceError(QualityGateError):
    """Command evidence cannot be persisted without overwriting prior evidence."""


class QualityGateBudgetExceeded(QualityGateError):
    """The aggregate deterministic-gate time budget is exhausted."""


class RunPolicyAuthority(StrEnum):
    """Whether a policy carries frozen experiment inputs or product guards."""

    CONTROLLED_EVALUATION = "controlled_evaluation"
    PRODUCT = "product"


def _safe_relative_path(value: str, *, allow_dot: bool = False) -> str:
    if not value or "\\" in value or "\x00" in value:
        raise ValueError("paths must be non-empty and use POSIX separators")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("paths must be safe relative paths")
    if path == PurePosixPath(".") and not allow_dot:
        raise ValueError("path must identify a concrete relative entry")
    return value


def _is_within_posix(path: PurePosixPath, root: PurePosixPath) -> bool:
    return path == root or root in path.parents


def _validate_container_path(value: str) -> str:
    if "\\" in value or "\x00" in value:
        raise ValueError("container paths must use POSIX separators")
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts or str(path) != value:
        raise ValueError("container paths must be canonical absolute paths")
    return value


def _validate_argv(values: tuple[str, ...]) -> tuple[str, ...]:
    if not values or len(values) > 64:
        raise ValueError("command argv must contain between 1 and 64 arguments")
    if any(
        not value
        or len(value) > 4096
        or any(character in value for character in ("\x00", "\r", "\n"))
        for value in values
    ):
        raise ValueError("command arguments must be non-empty single-line values")
    if sum(len(value) for value in values) > 16_384:
        raise ValueError("command argv is too large")

    executable = PurePosixPath(values[0]).name.lower()
    if values[0] != executable and values[0].lower() != executable:
        raise ValueError("command executables must be bare image PATH names")
    if re.fullmatch(r"[a-z0-9][a-z0-9._+-]*", executable) is None:
        raise ValueError("command executable contains unsafe characters")
    if executable in _SHELL_EXECUTABLES:
        raise ValueError("quality gates cannot invoke a command shell")
    forbidden_flags = _INLINE_CODE_FLAGS.get(executable, set())
    if forbidden_flags.intersection(values[1:]):
        raise ValueError("quality gates cannot use opaque inline interpreter code")
    return values


class SandboxLimits(BaseModel):
    """Mandatory per-command and aggregate resource ceilings."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    command_timeout_seconds: int = Field(ge=1, le=3600)
    total_timeout_seconds: int = Field(ge=1, le=14_400)
    cpu_cores: float = Field(gt=0, le=8)
    memory_mb: int = Field(ge=64, le=32_768)
    pids: int = Field(ge=8, le=4096)
    open_files: int = Field(ge=32, le=65_536)
    writable_tmpfs_mb: int = Field(ge=16, le=8192)
    stdout_max_bytes: int = Field(ge=1024, le=16_777_216)
    stderr_max_bytes: int = Field(ge=1024, le=16_777_216)

    @model_validator(mode="after")
    def require_coherent_time_limits(self) -> Self:
        """The aggregate budget must permit at least one full command."""

        if self.total_timeout_seconds < self.command_timeout_seconds:
            raise ValueError("total timeout cannot be below the command timeout")
        return self


class DockerSandboxPolicy(BaseModel):
    """Fixed Docker isolation settings for generated-code execution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    backend: Literal["docker"] = "docker"
    image: str = Field(min_length=1, max_length=255)
    pull: Literal["never"] = "never"
    network: Literal["none"] = "none"
    read_only_root_filesystem: Literal[True] = True
    workspace_access: Literal["read_only"] = "read_only"
    workspace_target: Literal["/workspace"] = "/workspace"
    tmpfs_target: Literal["/tmp"] = "/tmp"
    user: str = Field(pattern=r"^(?:host|[1-9][0-9]*:[1-9][0-9]*)$")
    environment: dict[str, str] = Field(default_factory=dict)

    @field_validator("image")
    @classmethod
    def require_versioned_image(cls, value: str) -> str:
        """Reject mutable implicit/latest image references and CLI-like values."""

        final_component = value.rsplit("/", 1)[-1]
        if (
            value != value.strip()
            or any(character.isspace() for character in value)
            or value.startswith("-")
            or value.endswith(":latest")
            or (":" not in final_component and "@sha256:" not in value)
        ):
            raise ValueError("Docker image must use an explicit non-latest version")
        return value

    @field_validator("environment")
    @classmethod
    def require_literal_safe_environment(cls, values: dict[str, str]) -> dict[str, str]:
        """Only fixed literal values are passed; host variables are never copied."""

        for name, value in values.items():
            if re.fullmatch(r"[A-Z_][A-Z0-9_]*", name) is None:
                raise ValueError(
                    "sandbox environment names must be uppercase identifiers"
                )
            if not value or any(
                character in value for character in ("\x00", "\r", "\n")
            ):
                raise ValueError(
                    "sandbox environment values must be non-empty literals"
                )
        return dict(sorted(values.items()))


class RunPolicy(BaseModel):
    """Versioned sandbox policy with explicitly separated resource authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[QUALITY_GATE_SCHEMA_VERSION]
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    authority: RunPolicyAuthority
    agent_budget: AgentBudget | None = None
    agent_stage_timeouts_seconds: (
        dict[AgentRole, Annotated[int, Field(ge=1, le=3600)]] | None
    ) = None
    sandbox: DockerSandboxPolicy
    limits: SandboxLimits

    @field_validator("agent_stage_timeouts_seconds")
    @classmethod
    def require_every_role_timeout(
        cls,
        values: dict[AgentRole, int] | None,
    ) -> dict[AgentRole, int] | None:
        """Freeze one explicit invocation timeout for every Agent role."""

        if values is None:
            return None
        missing = set(AgentRole) - set(values)
        if missing:
            names = ", ".join(sorted(role.value for role in missing))
            raise ValueError(f"Agent invocation timeouts are missing roles: {names}")
        return dict(sorted(values.items(), key=lambda item: item[0].value))

    @model_validator(mode="after")
    def separate_product_and_evaluation_limits(self) -> Self:
        """Never let experiment ceilings masquerade as ordinary product policy."""

        has_budget = self.agent_budget is not None
        has_timeouts = self.agent_stage_timeouts_seconds is not None
        if self.authority is RunPolicyAuthority.CONTROLLED_EVALUATION:
            if not has_budget or not has_timeouts:
                raise ValueError(
                    "controlled-evaluation policies require Agent budgets and "
                    "invocation timeouts"
                )
        elif has_budget or has_timeouts:
            raise ValueError(
                "product policies cannot contain Agent call, token, duration, cost, "
                "or invocation-timeout ceilings"
            )
        return self

    @property
    def evaluation_budget(self) -> AgentBudget:
        """Return the frozen evaluation budget or reject product misuse."""

        if self.agent_budget is None:
            raise QualityGateConfigurationError(
                "product policy has no controlled-evaluation Agent budget"
            )
        return self.agent_budget

    @property
    def evaluation_timeouts(self) -> dict[AgentRole, int]:
        """Return frozen evaluation timeouts or reject product misuse."""

        if self.agent_stage_timeouts_seconds is None:
            raise QualityGateConfigurationError(
                "product policy has no controlled-evaluation Agent timeouts"
            )
        return self.agent_stage_timeouts_seconds


class ReadOnlyInputMount(BaseModel):
    """Trusted quality input mounted outside the generated workspace."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    source: str = Field(min_length=1)
    target: str = Field(min_length=1)
    read_only: Literal[True] = True

    @field_validator("source")
    @classmethod
    def require_safe_source(cls, value: str) -> str:
        return _safe_relative_path(value)

    @field_validator("target")
    @classmethod
    def require_isolated_target(cls, value: str) -> str:
        value = _validate_container_path(value)
        target = PurePosixPath(value)
        if target == _CONTAINER_INPUT_ROOT or not _is_within_posix(
            target, _CONTAINER_INPUT_ROOT
        ):
            raise ValueError(
                f"input mount targets must be below {_CONTAINER_INPUT_ROOT}"
            )
        return value


class QualityGateDefinition(BaseModel):
    """One fixed, non-shell deterministic command."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^CHECK_[A-Z0-9_]+$")
    argv: tuple[str, ...] = Field(min_length=1, max_length=64)
    working_directory: str = "."
    timeout_seconds: int | None = Field(default=None, ge=1, le=3600)
    criterion_ids: tuple[str, ...] = Field(min_length=1)

    @field_validator("argv")
    @classmethod
    def require_safe_non_shell_argv(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_argv(values)

    @field_validator("working_directory")
    @classmethod
    def require_safe_working_directory(cls, value: str) -> str:
        return _safe_relative_path(value, allow_dot=True)

    @field_validator("criterion_ids")
    @classmethod
    def require_unique_criteria(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)) or any(
            re.fullmatch(r"[A-Z][A-Z0-9_-]*", value) is None for value in values
        ):
            raise ValueError("gate criterion IDs must be valid and unique")
        return values


class QualityGateManifest(BaseModel):
    """Versioned authority for deterministic acceptance gates and trusted inputs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[QUALITY_GATE_SCHEMA_VERSION]
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    policy_id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    task_brief: str = Field(min_length=1)
    input_mounts: tuple[ReadOnlyInputMount, ...] = Field(min_length=1)
    gates: tuple[QualityGateDefinition, ...] = Field(min_length=1)
    manual_review_criteria: tuple[str, ...] = ()

    @field_validator("task_brief")
    @classmethod
    def require_safe_task_brief_path(cls, value: str) -> str:
        return _safe_relative_path(value)

    @field_validator("manual_review_criteria")
    @classmethod
    def require_unique_manual_criteria(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)) or any(
            re.fullmatch(r"[A-Z][A-Z0-9_-]*", value) is None for value in values
        ):
            raise ValueError("manual-review criterion IDs must be valid and unique")
        return values

    @model_validator(mode="after")
    def require_unique_definitions(self) -> Self:
        """Reject ambiguous IDs and overlapping container mount targets."""

        gate_ids = [gate.id for gate in self.gates]
        mount_ids = [mount.id for mount in self.input_mounts]
        if len(gate_ids) != len(set(gate_ids)):
            raise ValueError("quality-gate IDs must be unique")
        if len(mount_ids) != len(set(mount_ids)):
            raise ValueError("input-mount IDs must be unique")

        targets = [PurePosixPath(mount.target) for mount in self.input_mounts]
        for index, target in enumerate(targets):
            for other in targets[index + 1 :]:
                if _is_within_posix(target, other) or _is_within_posix(other, target):
                    raise ValueError("input-mount targets cannot overlap")
        return self


# Compatibility export for callers that explicitly validate benchmark manifests.
BenchmarkManifest = QualityGateManifest


@dataclass(frozen=True)
class ResolvedInputMount:
    """Validated host source and container target for a trusted input."""

    id: str
    source: Path
    target: PurePosixPath


@dataclass(frozen=True)
class QualityGateConfiguration:
    """Loaded, cross-validated manifests plus their reproducibility hashes."""

    policy: RunPolicy
    manifest: QualityGateManifest
    task_brief: TaskBrief
    policy_path: Path
    manifest_path: Path
    task_brief_path: Path
    policy_sha256: str
    manifest_sha256: str
    task_brief_sha256: str
    input_mounts: tuple[ResolvedInputMount, ...]

    @property
    def benchmark(self) -> QualityGateManifest:
        """Return the manifest under its compatibility name for evaluation callers."""

        return self.manifest

    @property
    def benchmark_path(self) -> Path:
        """Return the manifest path under the legacy evaluation name."""

        return self.manifest_path

    @property
    def benchmark_sha256(self) -> str:
        """Return the manifest digest under the legacy evaluation name."""

        return self.manifest_sha256


@dataclass(frozen=True)
class SandboxInvocation:
    """One validated command invocation independent of backend transport."""

    gate_id: str
    argv: tuple[str, ...]
    working_directory: PurePosixPath
    workspace: Path
    input_mounts: tuple[ResolvedInputMount, ...]
    environment: tuple[tuple[str, str], ...]
    sandbox: DockerSandboxPolicy
    limits: SandboxLimits
    timeout_seconds: float


@dataclass(frozen=True)
class SandboxExecution:
    """Raw backend result before it is converted to persisted evidence."""

    exit_code: int | None
    timed_out: bool
    duration_ms: int
    stdout: bytes = b""
    stderr: bytes = b""
    output_limit_exceeded: bool = False

    def __post_init__(self) -> None:
        if self.duration_ms < 0:
            raise ValueError("sandbox duration cannot be negative")
        if self.timed_out and self.exit_code is not None:
            raise ValueError("timed-out sandbox execution cannot have an exit code")
        if not self.timed_out and self.exit_code is None:
            raise ValueError("completed sandbox execution requires an exit code")
