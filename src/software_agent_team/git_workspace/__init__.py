"""Safe isolated Git clone preparation and immutable snapshot verification.

安全隔离 Git 工作区子包。
提供独立的 Git 克隆环境、受限子进程执行沙箱、不可变提交快照验证、
增量进度检查以及产物一致性校验门面。
"""

from __future__ import annotations

from .manager import GitWorkspaceManager
from .types import (
    ATTRIBUTE_FILTER_PATTERN,
    COMMIT_PATTERN,
    RUN_ID_PATTERN,
    Clock,
    GitCommandError,
    GitSnapshot,
    GitWorkspace,
    GitWorkspaceError,
    GitWorkspaceProgress,
    RepositoryValidationError,
    UnsafeRepositoryError,
    WorkspaceAlreadyExistsError,
    WorkspaceIntegrityError,
    _require_safe_repository_path,
    _require_utc,
    _system_clock,
)
from .validation import validate_work_result_snapshot

__all__ = [
    "ATTRIBUTE_FILTER_PATTERN",
    "COMMIT_PATTERN",
    "RUN_ID_PATTERN",
    "Clock",
    "GitCommandError",
    "GitSnapshot",
    "GitWorkspace",
    "GitWorkspaceError",
    "GitWorkspaceManager",
    "GitWorkspaceProgress",
    "RepositoryValidationError",
    "UnsafeRepositoryError",
    "WorkspaceAlreadyExistsError",
    "WorkspaceIntegrityError",
    "_require_safe_repository_path",
    "_require_utc",
    "_system_clock",
    "validate_work_result_snapshot",
]
