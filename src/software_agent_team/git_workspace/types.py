"""Safe isolated Git clone types, models, errors, and validation primitives.

Git 工作区类型系统、数据模型、异常体系与基础校验原语。
定义了工作区标识契约 (GitWorkspace)、控制器验证快照 (GitSnapshot)、
增量状态跟踪 (GitWorkspaceProgress) 以及底层路径与时区校验工具。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

COMMIT_PATTERN = r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"
RUN_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
ATTRIBUTE_FILTER_PATTERN = re.compile(r"(?:^|\s)[-!]?filter(?:=|\s|$)")


class GitWorkspaceError(ValueError):
    """Base error for isolated Git workspace operations.

    Git 隔离工作区操作的基础异常类。
    """


class GitCommandError(GitWorkspaceError):
    """Raised when a bounded Git subprocess returns an unexpected result.

    当受限 Git 子进程执行失败或返回非预期状态码时抛出。
    """


class RepositoryValidationError(GitWorkspaceError):
    """Raised when a source repository cannot provide a controlled snapshot.

    当源仓库无法提供受控快照或未满足前置条件时抛出。
    """


class UnsafeRepositoryError(RepositoryValidationError):
    """Raised when checkout could execute repository-controlled programs.

    当检出操作可能执行仓库受控程序 (如钩子、过滤器) 时抛出。
    """


class WorkspaceAlreadyExistsError(GitWorkspaceError):
    """Raised when the derived workspace path is already occupied.

    当派生的工作区路径已存在或被占用时抛出。
    """


class WorkspaceIntegrityError(GitWorkspaceError):
    """Raised when a workspace or snapshot disagrees with verified Git state.

    当工作区或快照与经验证的 Git 状态不一致时抛出。
    """


def _require_utc(value: datetime) -> datetime:
    """Normalize workspace timestamps to UTC.

    验证时间戳必须包含时区信息并统一转换为 UTC。
    """
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("workspace timestamps must include a timezone")
    return value.astimezone(UTC)


def _require_safe_repository_path(value: str) -> str:
    """Validate relative repository path safety against traversal attacks.

    验证仓库相对路径的安全性; 防止路径穿越攻击与非法分隔符。
    """
    if "\\" in value:
        raise ValueError("repository paths must use POSIX separators")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path == PurePosixPath("."):
        raise ValueError("repository paths must be safe relative paths")
    return value


Clock = Callable[[], datetime]


def _system_clock() -> datetime:
    """Default system clock returning current UTC time.

    默认系统时钟生成器: 返回当前 UTC 时间。
    """
    return datetime.now(UTC)


class GitWorkspace(BaseModel):
    """Immutable identity of one detached, self-contained run clone.

    单次运行周期内独立分离、自包含克隆工作区的不可变标识契约。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    source_repository: str = Field(min_length=1)
    workspace_path: str = Field(min_length=1)
    base_commit: str = Field(pattern=COMMIT_PATTERN)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        """Normalize workspace timestamps to UTC."""
        return _require_utc(value)

    @field_validator("source_repository", "workspace_path")
    @classmethod
    def require_absolute_path(cls, value: str) -> str:
        """Persist explicit machine-local paths for recovery."""
        if not Path(value).is_absolute():
            raise ValueError("workspace paths must be absolute")
        return value

    @model_validator(mode="after")
    def validate_distinct_paths(self) -> Self:
        """Never treat the source checkout as its own isolated workspace."""
        if self.source_repository == self.workspace_path:
            raise ValueError("source repository and workspace paths must differ")
        return self


class GitSnapshot(BaseModel):
    """Controller-verified immutable commit boundary for one iteration.

    单次迭代周期内经控制器严格验证的不可变提交边界与变更摘要。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    iteration: int = Field(ge=1)
    input_commit: str = Field(pattern=COMMIT_PATTERN)
    output_commit: str = Field(pattern=COMMIT_PATTERN)
    commit_count: int = Field(ge=1)
    changed_files: tuple[str, ...] = Field(min_length=1)
    recorded_at: datetime

    @field_validator("recorded_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        """Normalize snapshot timestamps to UTC."""
        return _require_utc(value)

    @field_validator("changed_files")
    @classmethod
    def require_safe_changed_files(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Keep verified paths unique, relative, and portable."""
        if len(values) != len(set(values)):
            raise ValueError("snapshot changed files must be unique")
        if any(not value for value in values):
            raise ValueError("snapshot changed files must not be blank")
        return tuple(_require_safe_repository_path(value) for value in values)

    @model_validator(mode="after")
    def validate_commits(self) -> Self:
        """Require a new output commit."""
        if self.input_commit == self.output_commit:
            raise ValueError("snapshot output commit must differ from input")
        return self


class GitWorkspaceProgress(BaseModel):
    """Content-digested, non-deliverable state used for safe continuation.

    内容摘要化的非交付阶段状态; 用于安全恢复与增量推进判定。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    input_commit: str = Field(pattern=COMMIT_PATTERN)
    current_commit: str = Field(pattern=COMMIT_PATTERN)
    commits_ahead: int = Field(ge=0)
    changed_files: tuple[str, ...] = ()
    has_uncommitted_changes: bool
    state_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("changed_files")
    @classmethod
    def require_safe_changed_files(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Keep progress paths ordered, unique, and repository-relative."""
        if tuple(sorted(values)) != values or len(values) != len(set(values)):
            raise ValueError("workspace progress paths must be sorted and unique")
        return tuple(_require_safe_repository_path(value) for value in values)

    @model_validator(mode="after")
    def bind_progress_to_git_state(self) -> Self:
        """Keep commit, dirty-state, and path evidence coherent."""
        if (self.current_commit == self.input_commit) != (self.commits_ahead == 0):
            raise ValueError("workspace progress commit evidence is inconsistent")
        if (self.commits_ahead > 0 or self.has_uncommitted_changes) and not (
            self.changed_files
        ):
            raise ValueError("workspace progress requires changed paths")
        if self.changed_files and not (
            self.commits_ahead > 0 or self.has_uncommitted_changes
        ):
            raise ValueError("unchanged workspace cannot report changed paths")
        return self

    @property
    def made_progress(self) -> bool:
        """Return whether this state differs materially from the input commit."""
        return bool(self.changed_files)


__all__ = [
    "ATTRIBUTE_FILTER_PATTERN",
    "COMMIT_PATTERN",
    "RUN_ID_PATTERN",
    "Clock",
    "GitCommandError",
    "GitSnapshot",
    "GitWorkspace",
    "GitWorkspaceError",
    "GitWorkspaceProgress",
    "RepositoryValidationError",
    "UnsafeRepositoryError",
    "WorkspaceAlreadyExistsError",
    "WorkspaceIntegrityError",
    "_require_safe_repository_path",
    "_require_utc",
    "_system_clock",
]
