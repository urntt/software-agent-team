"""Versioned deterministic quality gates executed through a sandbox boundary.

The checked-in run policy owns resource and isolation limits. A quality
manifest owns fixed command argv, working directories, acceptance-criterion
coverage, and trusted read-only inputs. Generated repositories cannot change
either manifest. Product profiles and controlled benchmarks use the same
validated boundary without sharing requirements or source seeds.

Modularized subpackage architecture:
- types: Exceptions, policies, limits, and domain models.
- config: Manifest loading, cross-validation, and mounting.
- backends: Docker, HostTest, and Fake execution backends.
- runner: Sequential execution supervisor and evidence collection.
"""

from __future__ import annotations

from software_agent_team.quality_gates.backends import (
    _EVIDENCE_TAIL_BYTES,
    _OUTPUT_LIMIT_MARKER,
    DockerSandboxBackend,
    FakeSandboxBackend,
    HostTestBackend,
    SandboxBackend,
    _append_bounded,
    _evidence_tail,
    _map_container_argument,
    _map_container_path,
    _ProcessResult,
    _run_bounded_process,
    _terminate_process,
)
from software_agent_team.quality_gates.config import (
    _MAX_MANIFEST_BYTES,
    _read_json,
    _reject_duplicate_json_keys,
    _resolve_trusted_relative,
    _validate_manifest_commands,
    bind_run_task_brief_mount,
    load_quality_gate_configuration,
)
from software_agent_team.quality_gates.runner import QualityGateRunner
from software_agent_team.quality_gates.types import (
    _CONTAINER_INPUT_ROOT,
    _INLINE_CODE_FLAGS,
    _SHELL_EXECUTABLES,
    QUALITY_GATE_SCHEMA_VERSION,
    BenchmarkManifest,
    DockerSandboxPolicy,
    QualityGateBudgetExceeded,
    QualityGateConfiguration,
    QualityGateConfigurationError,
    QualityGateDefinition,
    QualityGateError,
    QualityGateEvidenceError,
    QualityGateManifest,
    ReadOnlyInputMount,
    ResolvedInputMount,
    RunPolicy,
    RunPolicyAuthority,
    SandboxExecution,
    SandboxInvocation,
    SandboxLimits,
    SandboxUnavailableError,
    _is_within_posix,
    _safe_relative_path,
    _validate_argv,
    _validate_container_path,
)

__all__ = [
    "QUALITY_GATE_SCHEMA_VERSION",
    "_CONTAINER_INPUT_ROOT",
    "_EVIDENCE_TAIL_BYTES",
    "_INLINE_CODE_FLAGS",
    "_MAX_MANIFEST_BYTES",
    "_OUTPUT_LIMIT_MARKER",
    "_SHELL_EXECUTABLES",
    "BenchmarkManifest",
    "DockerSandboxBackend",
    "DockerSandboxPolicy",
    "FakeSandboxBackend",
    "HostTestBackend",
    "QualityGateBudgetExceeded",
    "QualityGateConfiguration",
    "QualityGateConfigurationError",
    "QualityGateDefinition",
    "QualityGateError",
    "QualityGateEvidenceError",
    "QualityGateManifest",
    "QualityGateRunner",
    "ReadOnlyInputMount",
    "ResolvedInputMount",
    "RunPolicy",
    "RunPolicyAuthority",
    "SandboxBackend",
    "SandboxExecution",
    "SandboxInvocation",
    "SandboxLimits",
    "SandboxUnavailableError",
    "_ProcessResult",
    "_append_bounded",
    "_evidence_tail",
    "_is_within_posix",
    "_map_container_argument",
    "_map_container_path",
    "_read_json",
    "_reject_duplicate_json_keys",
    "_resolve_trusted_relative",
    "_run_bounded_process",
    "_safe_relative_path",
    "_terminate_process",
    "_validate_argv",
    "_validate_container_path",
    "_validate_manifest_commands",
    "bind_run_task_brief_mount",
    "load_quality_gate_configuration",
]
