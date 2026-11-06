"""Deterministic run lifecycle, persistence, and recovery subpackage.

运行生命周期状态机、持久化与故障恢复子包。
对外导出完整的生命周期枚举、数据模式、异常体系、存储器及控制器门面。
"""

from __future__ import annotations

import os

from .controller import RunController
from .store import (
    RunStore,
    _exclusive_lock,
    _fsync_directory,
    _system_clock,
    _write_new_file,
)
from .types import (
    _LEGAL_NEXT_PHASES,
    RUN_ID_PATTERN,
    RUN_SCHEMA_VERSION,
    RUN_STATE_FILENAME,
    TASK_BRIEF_FILENAME,
    TEAM_PLAN_FILENAME,
    Clock,
    InvalidRunTransitionError,
    RunAlreadyExistsError,
    RunConflictError,
    RunControlError,
    RunIntegrityError,
    RunNotFoundError,
    RunPhase,
    RunRecord,
    RunTransition,
    TerminationReason,
    _is_legal_transition,
    _require_utc,
    _serialized_model,
)

__all__ = [
    "RUN_ID_PATTERN",
    "RUN_SCHEMA_VERSION",
    "RUN_STATE_FILENAME",
    "TASK_BRIEF_FILENAME",
    "TEAM_PLAN_FILENAME",
    "_LEGAL_NEXT_PHASES",
    "Clock",
    "InvalidRunTransitionError",
    "RunAlreadyExistsError",
    "RunConflictError",
    "RunControlError",
    "RunController",
    "RunIntegrityError",
    "RunNotFoundError",
    "RunPhase",
    "RunRecord",
    "RunStore",
    "RunTransition",
    "TerminationReason",
    "_exclusive_lock",
    "_fsync_directory",
    "_is_legal_transition",
    "_require_utc",
    "_serialized_model",
    "_system_clock",
    "_write_new_file",
    "os",
]
