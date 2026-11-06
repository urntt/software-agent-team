"""Artifact packaging and durable handoff envelope models.

This module provides data models for artifact references, handoff envelopes,
and phase/iteration artifact base classes that enforce strict validation,
tamper-evident paths, and UTC timestamps.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import PurePosixPath
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .types import (
    AGENT_ID_PATTERN,
    ARTIFACT_SCHEMA_VERSION,
    ArtifactKind,
    HandoffStatus,
    _require_utc,
)


class ArtifactReference(BaseModel):
    """Run-relative reference to a persisted workflow artifact."""

    model_config = ConfigDict(extra="forbid")

    kind: ArtifactKind
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    description: str = ""

    @field_validator("path")
    @classmethod
    def require_safe_relative_path(cls, value: str) -> str:
        """Reject paths that can escape a run artifact directory."""
        if "\\" in value:
            raise ValueError("artifact paths must use POSIX separators")
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or path == PurePosixPath("."):
            raise ValueError("artifact paths must be safe run-relative paths")
        return value


class HandoffEnvelope(BaseModel):
    """Durable metadata exchanged between Agent responsibilities.

    ``stage``, ``sequence``, and ``created_at`` have compatibility defaults so
    older envelopes remain structurally readable. The context-bound artifact
    store requires explicit, valid values before persisting an envelope.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[
        2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, ARTIFACT_SCHEMA_VERSION
    ] = ARTIFACT_SCHEMA_VERSION
    kind: Literal[ArtifactKind.HANDOFF_ENVELOPE] = ArtifactKind.HANDOFF_ENVELOPE
    run_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    team_id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    iteration: int = Field(ge=1)
    stage: str = Field(default="handoff", pattern=r"^[a-z][a-z0-9_]*$")
    sequence: int = Field(default=1, ge=1)
    source_agent_id: str = Field(pattern=AGENT_ID_PATTERN)
    target_agent_id: str | None = Field(default=None, pattern=AGENT_ID_PATTERN)
    status: HandoffStatus
    created_at: datetime | None = None
    summary: str = Field(min_length=1)
    input_commit: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{7,64}$",
    )
    artifacts: list[ArtifactReference] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)

    @field_validator("created_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime | None) -> datetime | None:
        """Normalize explicit durable timestamps while accepting legacy input."""
        return None if value is None else _require_utc(value)

    @field_validator("blockers")
    @classmethod
    def require_clean_unique_blockers(cls, values: list[str]) -> list[str]:
        """Keep failure evidence concise and unambiguous."""
        cleaned = [value.strip() for value in values]
        if any(not value for value in cleaned):
            raise ValueError("blockers must not be blank")
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("blockers must be unique")
        return cleaned

    @model_validator(mode="after")
    def validate_handoff(self) -> Self:
        """Enforce evidence for failures and meaningful role boundaries."""
        if (
            self.status in {HandoffStatus.BLOCKED, HandoffStatus.FAILED}
            and not self.blockers
        ):
            raise ValueError("blocked or failed handoffs must identify a blocker")
        if (
            self.target_agent_id is not None
            and self.target_agent_id == self.source_agent_id
        ):
            raise ValueError("source and target Agents must differ")
        artifact_paths = [artifact.path for artifact in self.artifacts]
        if len(artifact_paths) != len(set(artifact_paths)):
            raise ValueError("handoff artifact references must be unique")
        return self


class PhaseArtifact(BaseModel):
    """Metadata shared by every durable workflow artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[
        2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, ARTIFACT_SCHEMA_VERSION
    ] = ARTIFACT_SCHEMA_VERSION
    kind: ArtifactKind
    run_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    team_id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    producer: str = Field(pattern=AGENT_ID_PATTERN)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        """Normalize persisted timestamps to UTC."""
        return _require_utc(value)


class IterationArtifact(PhaseArtifact):
    """Metadata shared by artifacts produced within one implementation pass."""

    iteration: int = Field(ge=1)
