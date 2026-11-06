"""Primary product-flow diagnostics, source preparation, and delivery.

产品工作流诊断、源码准备与交付子包。
提供设备启动前置诊断、宿主机容量与并发限制、绿地项目初始化、
清单命令契约安全解析; 以及原子化、无覆盖风险的代码交付能力。
"""

from __future__ import annotations

import os
import platform
import shutil

from .capacity import (
    _minimum_capacity,
    _nonnegative_integer,
    inspect_host_capacity,
    limit_concurrency_to_host_capacity,
)
from .delivery import (
    _clone_git_result,
    _git_output,
    _read_text,
    _remove_stale_staging_targets,
    _rename_no_replace,
    _run_command,
    deliver_product_workspace,
    generate_product_run_id,
    load_project_commands,
    prepare_product_source,
    validate_project_destination,
)
from .diagnostics import (
    _secure_product_state,
    _state_layout_diagnostic,
    _state_layout_failure,
    ensure_product_state,
    inspect_startup_environment,
    render_startup_diagnostics,
)
from .types import (
    _MAX_PROJECT_MANIFEST_BYTES,
    AT_FDCWD,
    MINIMUM_FREE_BYTES,
    PROJECT_MANIFEST_NAME,
    PROJECT_NAME_PATTERN,
    RENAME_NOREPLACE,
    SUPPORTED_ARCHITECTURES,
    CommandRunner,
    DiagnosticCheck,
    DiagnosticState,
    FileReader,
    HostCapacitySnapshot,
    HostConcurrencyLimit,
    ProductFlowError,
    ProductStatePaths,
    ProjectCommands,
    StartupDiagnostics,
)

__all__ = [
    "AT_FDCWD",
    "MINIMUM_FREE_BYTES",
    "PROJECT_MANIFEST_NAME",
    "PROJECT_NAME_PATTERN",
    "RENAME_NOREPLACE",
    "SUPPORTED_ARCHITECTURES",
    "_MAX_PROJECT_MANIFEST_BYTES",
    "CommandRunner",
    "DiagnosticCheck",
    "DiagnosticState",
    "FileReader",
    "HostCapacitySnapshot",
    "HostConcurrencyLimit",
    "ProductFlowError",
    "ProductStatePaths",
    "ProjectCommands",
    "StartupDiagnostics",
    "_clone_git_result",
    "_git_output",
    "_minimum_capacity",
    "_nonnegative_integer",
    "_read_text",
    "_remove_stale_staging_targets",
    "_rename_no_replace",
    "_run_command",
    "_secure_product_state",
    "_state_layout_diagnostic",
    "_state_layout_failure",
    "deliver_product_workspace",
    "ensure_product_state",
    "generate_product_run_id",
    "inspect_host_capacity",
    "inspect_startup_environment",
    "limit_concurrency_to_host_capacity",
    "load_project_commands",
    "os",
    "platform",
    "prepare_product_source",
    "render_startup_diagnostics",
    "shutil",
    "validate_project_destination",
]
