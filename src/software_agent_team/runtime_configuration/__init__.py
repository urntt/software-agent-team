"""Run-scoped OpenClaw configuration materialization and offline preflight."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time

from software_agent_team.docker_engine import (
    current_docker_engine,
    verify_docker_engine,
)
from software_agent_team.openclaw_runtime import isolated_openclaw_environment

from .model_inspection import (
    _apply_model_compatibility,
    _inspect_deferred_model_auth,
    has_model_compatibility,
    inspect_openclaw_model,
    materialize_model_check_configuration,
)
from .preflight import (
    _config_validation_error,
    _fsync_directory,
    _persist_private_json,
    _run_openclaw_preflight_command,
    inspect_runtime_preflight,
    persist_runtime_preflight,
)
from .sandbox import (
    _CAPABILITY_TEMPLATE_ROLES,
    _ROOTLESS_CONTRACT_SCRIPT,
    _probe_rootless_sandbox_contract,
    inspect_sandbox_image,
    materialize_run_configuration,
    probe_sandbox_runtime,
    require,
)
from .types import (
    CONFIG_VALIDATION_TIMEOUT_SECONDS,
    MODEL_INSPECTION_TIMEOUT_SECONDS,
    PREFLIGHT_COMMAND_TIMEOUT_SECONDS,
    OpenClawModelInspection,
    RuntimeConfigurationError,
    RuntimePreflight,
    SandboxImageInspection,
    SandboxRuntimeProbe,
    _ArtifactSubmissionMode,
)

__all__ = [
    "CONFIG_VALIDATION_TIMEOUT_SECONDS",
    "MODEL_INSPECTION_TIMEOUT_SECONDS",
    "PREFLIGHT_COMMAND_TIMEOUT_SECONDS",
    "_CAPABILITY_TEMPLATE_ROLES",
    "_ROOTLESS_CONTRACT_SCRIPT",
    "OpenClawModelInspection",
    "RuntimeConfigurationError",
    "RuntimePreflight",
    "SandboxImageInspection",
    "SandboxRuntimeProbe",
    "_ArtifactSubmissionMode",
    "_apply_model_compatibility",
    "_config_validation_error",
    "_fsync_directory",
    "_inspect_deferred_model_auth",
    "_persist_private_json",
    "_probe_rootless_sandbox_contract",
    "_run_openclaw_preflight_command",
    "current_docker_engine",
    "has_model_compatibility",
    "inspect_openclaw_model",
    "inspect_runtime_preflight",
    "inspect_sandbox_image",
    "isolated_openclaw_environment",
    "json",
    "materialize_model_check_configuration",
    "materialize_run_configuration",
    "os",
    "persist_runtime_preflight",
    "probe_sandbox_runtime",
    "require",
    "shutil",
    "subprocess",
    "tempfile",
    "time",
    "verify_docker_engine",
]
