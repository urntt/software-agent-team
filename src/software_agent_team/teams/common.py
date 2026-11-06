"""Shared primitive validation helpers and utilities for team definitions.

团队定义与计划模型通用的底层校验工具与辅助函数。
"""

from __future__ import annotations

from datetime import UTC, datetime


def _clean_unique_text(values: tuple[str, ...], *, label: str) -> tuple[str, ...]:
    """Validate and clean a tuple of non-blank unique strings.

    清洗并校验字符串元组: 要求非空且不可重复。
    """
    cleaned = tuple(value.strip() for value in values)
    if any(not value for value in cleaned):
        raise ValueError(f"{label} entries must not be blank")
    if len(cleaned) != len(set(cleaned)):
        raise ValueError(f"{label} entries must be unique")
    return cleaned


def _require_utc(value: datetime) -> datetime:
    """Ensure a datetime object includes timezone info and convert to UTC.

    校验时间戳必须包含时区信息并归一化为 UTC 时区。
    """
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("team-plan timestamps must include a timezone")
    return value.astimezone(UTC)
