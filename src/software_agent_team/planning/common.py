"""Shared primitive utilities and limits for Planning."""

from __future__ import annotations

import unicodedata
from datetime import UTC, datetime
from pathlib import PurePosixPath

PLANNING_SCHEMA_VERSION = 23
MINIMUM_READABLE_PLANNING_SCHEMA_VERSION = 2
PLANNING_OPTION_DESCRIPTION_MAX_LENGTH = 300
MAX_PLANNING_EVIDENCE_CHARACTERS = 1_000_000
MAX_RESPONSE_NORMALIZATIONS = 100
MAX_RESPONSE_NORMALIZATION_CHARACTERS = 200
MAX_PLANNING_DECISION_SUMMARY_CHARACTERS = 500


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Planning timestamps must include a timezone")
    return value.astimezone(UTC)


def _clean_text(value: str, *, label: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{label} must not be blank")
    return cleaned


def _clean_unique(values: tuple[str, ...], *, label: str) -> tuple[str, ...]:
    cleaned = tuple(value.strip() for value in values)
    if any(not value for value in cleaned):
        raise ValueError(f"{label} entries must not be blank")
    if len(cleaned) != len(set(cleaned)):
        raise ValueError(f"{label} entries must be unique")
    return cleaned


def _render_prefixed_text(prefix: str, value: str) -> tuple[str, ...]:
    """Render untrusted text without letting continuations escape their field."""

    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    visible = "".join(
        character
        if character == "\n"
        or unicodedata.category(character) not in {"Cc", "Cf", "Zl", "Zp"}
        else character.encode("unicode_escape").decode("ascii")
        for character in normalized
    )
    fragments = visible.split("\n")
    continuation = " " * len(prefix)
    return (
        prefix + fragments[0],
        *(continuation + fragment for fragment in fragments[1:]),
    )


def _safe_path(value: str) -> str:
    cleaned = value.strip()
    path = PurePosixPath(cleaned)
    if (
        not cleaned
        or "\\" in cleaned
        or path.is_absolute()
        or path == PurePosixPath(".")
        or ".." in path.parts
        or str(path) != cleaned
    ):
        raise ValueError("paths must be canonical safe relative POSIX paths")
    return cleaned
