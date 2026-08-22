"""User-local, secret-free defaults for live harness runs."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

USER_CONFIGURATION_SCHEMA_VERSION = 1
USER_CONFIGURATION_ENVIRONMENT_VARIABLE = "SAT_CONFIG_PATH"


class UserConfigurationError(ValueError):
    """Raised when user-local configuration cannot be loaded safely."""


class UserConfiguration(BaseModel):
    """Non-secret defaults applied when equivalent CLI flags are omitted."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[USER_CONFIGURATION_SCHEMA_VERSION] = (
        USER_CONFIGURATION_SCHEMA_VERSION
    )
    model: str = Field(min_length=1)
    input_cost_per_million_usd: Decimal = Field(ge=0, le=10_000)
    output_cost_per_million_usd: Decimal = Field(ge=0, le=10_000)
    verification_concurrency: Literal[1, 2] = 2
    agent_timeout_seconds: int = Field(default=600, ge=1, le=86_400)

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
