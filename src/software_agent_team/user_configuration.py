"""User-local, secret-free defaults for live harness runs."""

from __future__ import annotations

import json
import os
import warnings
from collections.abc import Callable, Mapping
from decimal import Decimal
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from software_agent_team.model_routing import ModelProfile, ModelRoutingPolicy
from software_agent_team.teams import (
    AgentCapability,
    ModelRoutingMode,
    ModelSwitchCondition,
)

USER_CONFIGURATION_SCHEMA_VERSION = 7
USER_CONFIGURATION_ENVIRONMENT_VARIABLE = "SAT_CONFIG_PATH"


class UserConfigurationError(ValueError):
    """Raised when user-local configuration cannot be loaded safely."""


class _UserConfigurationV5(BaseModel):
    """Validated product defaults before multiple model profiles."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[5] = 5
    model: str = Field(min_length=1)
    input_cost_per_million_usd: Decimal | None = Field(
        default=None,
        ge=0,
        le=10_000,
    )
    output_cost_per_million_usd: Decimal | None = Field(
        default=None,
        ge=0,
        le=10_000,
    )
    max_concurrency: int = Field(default=2, ge=1, le=16)
    stage_timeout_seconds: int | None = Field(default=None, ge=30, le=3600)
    progress_visibility: Literal["compact", "standard", "detailed"] = "standard"

    @field_validator("model")
    @classmethod
    def require_clean_model(cls, value: str) -> str:
        """Store the exact non-blank OpenClaw model reference."""

        cleaned = value.strip()
        if not cleaned:
            raise ValueError("model must not be blank")
        if any(character.isspace() for character in cleaned):
            raise ValueError("model must be one reference without whitespace")
        return cleaned

    @model_validator(mode="after")
    def require_complete_price_pair(self) -> _UserConfigurationV5:
        if (self.input_cost_per_million_usd is None) != (
            self.output_cost_per_million_usd is None
        ):
            raise ValueError("input and output prices must be configured together")
        return self


class UserConfiguration(BaseModel):
    """Non-secret model profiles and product defaults for live SAT runs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[USER_CONFIGURATION_SCHEMA_VERSION] = (
        USER_CONFIGURATION_SCHEMA_VERSION
    )
    model_profiles: tuple[ModelProfile, ...] = Field(min_length=1, max_length=16)
    default_model_profile_id: str = Field(
        default="default",
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    routing_mode: ModelRoutingMode = ModelRoutingMode.STRICT
    capability_profile_overrides: dict[AgentCapability, str] = Field(
        default_factory=dict
    )
    stage_profile_overrides: dict[str, str] = Field(default_factory=dict)
    authorized_switch_conditions: tuple[ModelSwitchCondition, ...] = ()
    max_model_switches_per_agent: int = Field(default=0, ge=0, le=3)
    max_concurrency: int = Field(default=2, ge=1, le=16)
    stage_timeout_seconds: int | None = Field(default=None, ge=30, le=3600)
    progress_visibility: Literal["compact", "standard", "detailed"] = "standard"

    @model_validator(mode="before")
    @classmethod
    def expand_single_model_input(cls, value: object) -> object:
        """Accept the former constructor shape without persisting duplicate fields."""

        if not isinstance(value, dict) or "model_profiles" in value:
            return value
        if "model" not in value:
            return value
        payload = dict(value)
        model = payload.pop("model")
        input_cost = payload.pop("input_cost_per_million_usd", None)
        output_cost = payload.pop("output_cost_per_million_usd", None)
        payload["model_profiles"] = (
            {
                "id": "default",
                "model": model,
                "capabilities": tuple(
                    capability.value for capability in AgentCapability
                ),
                "input_cost_per_million_usd": input_cost,
                "output_cost_per_million_usd": output_cost,
            },
        )
        payload.setdefault("default_model_profile_id", "default")
        payload.setdefault("routing_mode", ModelRoutingMode.STRICT.value)
        return payload

    @model_validator(mode="after")
    def validate_model_routing(self) -> UserConfiguration:
        self.model_routing_policy()
        return self

    @property
    def default_model_profile(self) -> ModelProfile:
        """Return the authoritative profile used for bootstrap Planning."""

        for profile in self.model_profiles:
            if profile.id == self.default_model_profile_id:
                return profile
        raise ValueError("default model profile is not configured")

    @property
    def model(self) -> str:
        """Return the bootstrap model derived from the default profile."""

        return self.default_model_profile.model

    @property
    def input_cost_per_million_usd(self) -> Decimal | None:
        """Return the default profile's optional input price."""

        return self.default_model_profile.input_cost_per_million_usd

    @property
    def output_cost_per_million_usd(self) -> Decimal | None:
        """Return the default profile's optional output price."""

        return self.default_model_profile.output_cost_per_million_usd

    def model_routing_policy(self) -> ModelRoutingPolicy:
        """Compile the saved fields into the controller's routing contract."""

        return ModelRoutingPolicy(
            mode=self.routing_mode,
            profiles=self.model_profiles,
            default_profile_id=self.default_model_profile_id,
            capability_profile_overrides=self.capability_profile_overrides,
            stage_profile_overrides=self.stage_profile_overrides,
            authorized_switch_conditions=self.authorized_switch_conditions,
            max_switches_per_agent=self.max_model_switches_per_agent,
        )


class _UserConfigurationV4(BaseModel):
    """Validated product defaults before persisted progress visibility."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[4]
    model: str = Field(min_length=1)
    input_cost_per_million_usd: Decimal | None = Field(
        default=None,
        ge=0,
        le=10_000,
    )
    output_cost_per_million_usd: Decimal | None = Field(
        default=None,
        ge=0,
        le=10_000,
    )
    max_concurrency: int = Field(default=2, ge=1, le=16)
    stage_timeout_seconds: int | None = Field(default=None, ge=30, le=3600)

    @field_validator("model")
    @classmethod
    def require_clean_model(cls, value: str) -> str:
        return _UserConfigurationV5.require_clean_model(value)

    @model_validator(mode="after")
    def require_complete_price_pair(self) -> _UserConfigurationV4:
        if (self.input_cost_per_million_usd is None) != (
            self.output_cost_per_million_usd is None
        ):
            raise ValueError("input and output prices must be configured together")
        return self


class _UserConfigurationV3(BaseModel):
    """Validated product defaults before adaptive concurrency was separated."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[3]
    model: str = Field(min_length=1)
    input_cost_per_million_usd: Decimal | None = Field(
        default=None,
        ge=0,
        le=10_000,
    )
    output_cost_per_million_usd: Decimal | None = Field(
        default=None,
        ge=0,
        le=10_000,
    )
    verification_concurrency: Literal[1, 2] = 1
    stage_timeout_seconds: int | None = Field(default=None, ge=1, le=3600)

    @field_validator("model")
    @classmethod
    def require_clean_model(cls, value: str) -> str:
        return _UserConfigurationV5.require_clean_model(value)

    @model_validator(mode="after")
    def require_complete_price_pair(self) -> _UserConfigurationV3:
        if (self.input_cost_per_million_usd is None) != (
            self.output_cost_per_million_usd is None
        ):
            raise ValueError("input and output prices must be configured together")
        return self


class _UserConfigurationV2(BaseModel):
    """Validated evaluation-oriented defaults from the previous schema."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[2]
    model: str = Field(min_length=1)
    input_cost_per_million_usd: Decimal = Field(ge=0, le=10_000)
    output_cost_per_million_usd: Decimal = Field(ge=0, le=10_000)
    verification_concurrency: Literal[1, 2] = 2
    stage_timeout_seconds: int | None = Field(default=None, ge=1, le=3600)

    @field_validator("model")
    @classmethod
    def require_clean_model(cls, value: str) -> str:
        return _UserConfigurationV5.require_clean_model(value)


class _UserConfigurationV1(BaseModel):
    """Validated legacy shape used only for an explicit one-way migration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    model: str = Field(min_length=1)
    input_cost_per_million_usd: Decimal = Field(ge=0, le=10_000)
    output_cost_per_million_usd: Decimal = Field(ge=0, le=10_000)
    verification_concurrency: Literal[1, 2] = 2
    agent_timeout_seconds: int = Field(ge=1, le=86_400)

    @field_validator("model")
    @classmethod
    def require_clean_model(cls, value: str) -> str:
        return _UserConfigurationV5.require_clean_model(value)


def user_configuration_path(
    environment: Mapping[str, str] | None = None,
) -> Path:
    """Resolve the XDG user configuration path, with a testable override."""

    values = os.environ if environment is None else environment
    override = values.get(USER_CONFIGURATION_ENVIRONMENT_VARIABLE)
    if override is not None:
        if not override.strip() or override != override.strip():
            raise UserConfigurationError(
                f"{USER_CONFIGURATION_ENVIRONMENT_VARIABLE} must be a clean path"
            )
        path = Path(override).expanduser()
        if not path.is_absolute():
            raise UserConfigurationError(
                f"{USER_CONFIGURATION_ENVIRONMENT_VARIABLE} must be absolute"
            )
        return path

    xdg_root = values.get("XDG_CONFIG_HOME")
    if xdg_root:
        root = Path(xdg_root).expanduser()
    else:
        home = values.get("HOME")
        root = (Path(home).expanduser() if home else Path.home()) / ".config"
    if not root.is_absolute():
        raise UserConfigurationError("the user configuration root must be absolute")
    return root / "software-agent-team" / "config.json"


def load_user_configuration(
    path: Path | None = None,
    *,
    on_migration: Callable[[str], None] | None = None,
) -> UserConfiguration | None:
    """Load saved defaults, returning ``None`` before first-time setup."""

    destination = user_configuration_path() if path is None else path
    if destination.is_symlink():
        raise UserConfigurationError(
            f"user configuration must not be a symbolic link: {destination}"
        )
    if not destination.exists():
        return None
    if not destination.is_file():
        raise UserConfigurationError(
            f"user configuration must be a regular file: {destination}"
        )
    try:
        payload = json.loads(destination.read_text(encoding="utf-8"))
    except OSError as error:
        raise UserConfigurationError(
            f"cannot read user configuration: {destination}"
        ) from error
    if not isinstance(payload, dict):
        raise UserConfigurationError("user configuration must be a JSON object")
    if payload.get("schema_version") == 1:
        legacy = _UserConfigurationV1.model_validate(payload)
        notice = (
            "configuration schema v1 was loaded without its legacy "
            "agent_timeout_seconds value; runs now use measured per-role "
            "invocation timeouts unless a new global override is configured"
        )
        if on_migration is None:
            warnings.warn(notice, UserWarning, stacklevel=2)
        else:
            on_migration(notice)
        return UserConfiguration(
            model=legacy.model,
            input_cost_per_million_usd=legacy.input_cost_per_million_usd,
            output_cost_per_million_usd=legacy.output_cost_per_million_usd,
            max_concurrency=legacy.verification_concurrency,
            stage_timeout_seconds=None,
        )
    if payload.get("schema_version") == 2:
        legacy = _UserConfigurationV2.model_validate(payload)
        notice = (
            "configuration schema v2 was loaded with its existing evaluation "
            "prices and runtime overrides; current configuration also permits setup "
            "without a local price estimate"
        )
        if on_migration is None:
            warnings.warn(notice, UserWarning, stacklevel=2)
        else:
            on_migration(notice)
        return UserConfiguration(
            model=legacy.model,
            input_cost_per_million_usd=legacy.input_cost_per_million_usd,
            output_cost_per_million_usd=legacy.output_cost_per_million_usd,
            max_concurrency=legacy.verification_concurrency,
            stage_timeout_seconds=legacy.stage_timeout_seconds,
        )
    if payload.get("schema_version") == 3:
        legacy = _UserConfigurationV3.model_validate(payload)
        notice = (
            "configuration schema v3 verification_concurrency was migrated to "
            "the adaptive max_concurrency setting"
        )
        if on_migration is None:
            warnings.warn(notice, UserWarning, stacklevel=2)
        else:
            on_migration(notice)
        return UserConfiguration(
            model=legacy.model,
            input_cost_per_million_usd=legacy.input_cost_per_million_usd,
            output_cost_per_million_usd=legacy.output_cost_per_million_usd,
            max_concurrency=legacy.verification_concurrency,
            stage_timeout_seconds=legacy.stage_timeout_seconds,
        )
    if payload.get("schema_version") == 4:
        legacy = _UserConfigurationV4.model_validate(payload)
        notice = (
            "configuration schema v4 was loaded with standard progress visibility; "
            "save configuration to persist another visibility level"
        )
        if on_migration is None:
            warnings.warn(notice, UserWarning, stacklevel=2)
        else:
            on_migration(notice)
        return UserConfiguration(
            model=legacy.model,
            input_cost_per_million_usd=legacy.input_cost_per_million_usd,
            output_cost_per_million_usd=legacy.output_cost_per_million_usd,
            max_concurrency=legacy.max_concurrency,
            stage_timeout_seconds=legacy.stage_timeout_seconds,
            progress_visibility="standard",
        )
    if payload.get("schema_version") == 5:
        legacy = _UserConfigurationV5.model_validate(payload)
        notice = (
            "configuration schema v5 was migrated to one strict default model "
            "profile; add profiles explicitly to enable adaptive routing"
        )
        if on_migration is None:
            warnings.warn(notice, UserWarning, stacklevel=2)
        else:
            on_migration(notice)
        return UserConfiguration(
            model=legacy.model,
            input_cost_per_million_usd=legacy.input_cost_per_million_usd,
            output_cost_per_million_usd=legacy.output_cost_per_million_usd,
            max_concurrency=legacy.max_concurrency,
            stage_timeout_seconds=legacy.stage_timeout_seconds,
            progress_visibility=legacy.progress_visibility,
        )
    if payload.get("schema_version") == 6:
        notice = (
            "configuration schema v6 was migrated with unknown model context and "
            "attributable legacy user-supplied prices; task self-check will discover "
            "or request missing model metadata before use"
        )
        if on_migration is None:
            warnings.warn(notice, UserWarning, stacklevel=2)
        else:
            on_migration(notice)
        return UserConfiguration.model_validate(
            {**payload, "schema_version": USER_CONFIGURATION_SCHEMA_VERSION}
        )
    return UserConfiguration.model_validate(payload)


def save_user_configuration(
    configuration: UserConfiguration,
    path: Path | None = None,
) -> Path:
    """Atomically persist configuration with user-only file permissions."""

    destination = user_configuration_path() if path is None else path
    if not destination.is_absolute():
        raise UserConfigurationError("user configuration path must be absolute")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if destination.is_symlink():
        raise UserConfigurationError(
            f"user configuration must not be a symbolic link: {destination}"
        )
    if destination.exists() and not destination.is_file():
        raise UserConfigurationError(
            f"user configuration must be a regular file: {destination}"
        )

    content = (
        json.dumps(
            configuration.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    ).encode()
    temporary = destination.parent / f".{destination.name}.{uuid4().hex}.tmp"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
        destination.chmod(0o600)
        _fsync_directory(destination.parent)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
