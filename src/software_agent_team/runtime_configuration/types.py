"""Data types, models, limits, and error primitives for runtime configuration."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from software_agent_team.model_costs import CachePriceSupport
from software_agent_team.model_runtime import ModelApi, ModelEndpointKind

PREFLIGHT_COMMAND_TIMEOUT_SECONDS = 30
CONFIG_VALIDATION_TIMEOUT_SECONDS = 90
MODEL_INSPECTION_TIMEOUT_SECONDS = 90

_ArtifactSubmissionMode = Literal["single_tool", "tool_loop"]


class RuntimeConfigurationError(ValueError):
    """Raised when a safe run-scoped Agent configuration cannot be created."""


class OpenClawModelInspection(CachePriceSupport):
    """Offline catalog and credential readiness for one exact model ref."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str = Field(min_length=1)
    runtime_profile_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    api: ModelApi | None = None
    endpoint_kind: ModelEndpointKind | None = None
    available: bool
    local: bool | None = None
    provider_request_timeout_seconds: int | None = Field(default=None, ge=1)
    context_window_tokens: int | None = Field(
        default=None,
        ge=1,
    )
    input_modalities: tuple[str, ...] = ()
    input_cost_per_million_usd: Decimal | None = Field(
        default=None,
        ge=0,
    )
    output_cost_per_million_usd: Decimal | None = Field(
        default=None,
        ge=0,
    )
    error: str | None = Field(default=None, max_length=1000)

    @field_validator("input_modalities")
    @classmethod
    def require_clean_modalities(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(value.strip().casefold() for value in values)
        if any(
            not value or any(character.isspace() for character in value)
            for value in cleaned
        ):
            raise ValueError("model input modalities must be clean tokens")
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("model input modalities must be unique")
        return cleaned

    @model_validator(mode="after")
    def require_complete_price_pair(self) -> OpenClawModelInspection:
        if (self.input_cost_per_million_usd is None) != (
            self.output_cost_per_million_usd is None
        ):
            raise ValueError("model inspection prices must be complete or unknown")
        return self


class SandboxImageInspection(BaseModel):
    """Non-secret identity returned for one local Docker image reference."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sandbox_binary: str = Field(min_length=1)
    sandbox_version: str = Field(min_length=1)
    sandbox_image: str = Field(min_length=1)
    sandbox_image_id: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    sandbox_image_present: bool

    @property
    def ready(self) -> bool:
        """Return whether the reference resolves to a valid local image ID."""

        return self.sandbox_image_present and self.sandbox_image_id is not None


class SandboxRuntimeProbe(BaseModel):
    """Result of starting and removing one restricted sandbox container."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sandbox_binary: str = Field(min_length=1)
    sandbox_image_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    sandbox_container_ready: bool
    error: str | None = Field(default=None, max_length=1000)

    @property
    def ready(self) -> bool:
        """Return whether the image stayed alive under sandbox restrictions."""

        return self.sandbox_container_ready and self.error is None


class RuntimePreflight(BaseModel):
    """Non-secret evidence that the local runtime is ready for a live run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    openclaw_binary: str = Field(min_length=1)
    openclaw_version: str = Field(min_length=1)
    openclaw_state_dir: str = Field(min_length=1)
    runtime_config: str = Field(min_length=1)
    command_timeout_seconds: int = Field(
        default=PREFLIGHT_COMMAND_TIMEOUT_SECONDS,
        ge=1,
        le=3600,
    )
    config_validation_timeout_seconds: int = Field(
        default=CONFIG_VALIDATION_TIMEOUT_SECONDS,
        ge=1,
        le=3600,
    )
    model_inspection_timeout_seconds: int = Field(
        default=MODEL_INSPECTION_TIMEOUT_SECONDS,
        ge=1,
        le=3600,
    )
    sandbox_binary: str = Field(min_length=1)
    sandbox_version: str = Field(min_length=1)
    sandbox_image: str = Field(min_length=1)
    sandbox_image_id: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    config_valid: bool
    config_error: str | None = Field(default=None, min_length=1, max_length=1000)
    sandbox_image_present: bool
    sandbox_container_ready: bool
    sandbox_container_error: str | None = Field(default=None, max_length=1000)
    model: str | None = Field(default=None, min_length=1)
    model_available: bool | None = None
    model_error: str | None = Field(default=None, max_length=1000)
    model_local: bool | None = None
    model_request_timeout_seconds: int | None = Field(default=None, ge=1)
    model_inspections: tuple[OpenClawModelInspection, ...] = ()

    @field_validator("model_inspections")
    @classmethod
    def require_unique_model_inspections(
        cls,
        values: tuple[OpenClawModelInspection, ...],
    ) -> tuple[OpenClawModelInspection, ...]:
        models = [inspection.model for inspection in values]
        if len(models) != len(set(models)):
            raise ValueError("runtime preflight model inspections must be unique")
        return values

    @model_validator(mode="after")
    def bind_bootstrap_model_inspection(self) -> RuntimePreflight:
        """Keep the aggregate route evidence consistent with the primary fields."""

        if not self.model_inspections:
            return self
        if self.model is None:
            raise ValueError("model inspections require a bootstrap model")
        primary = next(
            (
                inspection
                for inspection in self.model_inspections
                if inspection.model == self.model
            ),
            None,
        )
        if primary is None:
            raise ValueError(
                "runtime preflight inspections must include the bootstrap model"
            )
        if (
            primary.available is not self.model_available
            or primary.error != self.model_error
            or primary.local is not self.model_local
            or primary.provider_request_timeout_seconds
            != self.model_request_timeout_seconds
        ):
            raise ValueError(
                "bootstrap model inspection differs from primary preflight evidence"
            )
        return self

    @property
    def ready(self) -> bool:
        """Return whether every offline prerequisite is available."""

        return (
            self.config_valid
            and self.sandbox_image_present
            and self.sandbox_image_id is not None
            and self.sandbox_container_ready
            and self.sandbox_container_error is None
            and (self.model is None or self.model_available is True)
            and self.model_error is None
            and all(inspection.available for inspection in self.model_inspections)
        )


__all__ = [
    "CONFIG_VALIDATION_TIMEOUT_SECONDS",
    "MODEL_INSPECTION_TIMEOUT_SECONDS",
    "PREFLIGHT_COMMAND_TIMEOUT_SECONDS",
    "OpenClawModelInspection",
    "RuntimeConfigurationError",
    "RuntimePreflight",
    "SandboxImageInspection",
    "SandboxRuntimeProbe",
    "_ArtifactSubmissionMode",
]
