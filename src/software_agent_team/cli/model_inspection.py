"""CLI model discovery, provider smoke probing, and OpenClaw configuration."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import json5
from pydantic import ValidationError

from software_agent_team import cli
from software_agent_team.bounded_process import (
    BoundedProcessError,
    BoundedProcessTimeoutError,
)
from software_agent_team.cli.constants import DEFAULT_OPENCLAW_BINARY
from software_agent_team.model_routing import (
    ModelMetadataSource,
    ModelProfile,
)
from software_agent_team.model_runtime import (
    CredentialSource,
    ModelEndpointKind,
    ModelRuntimeProfileSource,
    openclaw_invocation_thinking_level,
    runtime_profile_from_openclaw_configuration,
)
from software_agent_team.openclaw_runtime import isolated_openclaw_environment
from software_agent_team.runtime_configuration import (
    MODEL_INSPECTION_TIMEOUT_SECONDS,
    OpenClawModelInspection,
    RuntimeConfigurationError,
    materialize_model_check_configuration,
)
from software_agent_team.teams import AgentCapability
from software_agent_team.user_configuration import UserConfiguration


@contextmanager
def _effective_model_configuration(
    *,
    profile: ModelProfile,
    configured_path: Path,
) -> Iterator[Path]:
    """Yield one profile-compiled config without copying private secrets."""

    _validate_optional_openclaw_configuration(configured_path)
    with tempfile.TemporaryDirectory(prefix="sat-model-check-") as temporary:
        compatibility_path = Path(temporary) / "openclaw.model.json"
        materialize_model_check_configuration(
            compatibility_path,
            profile=profile,
        )
        yield compatibility_path


def _inspect_selected_model(
    openclaw_binary: Path,
    model: str | ModelProfile,
    *,
    state_dir: Path,
    config_path: Path,
) -> OpenClawModelInspection:
    """Inspect the exact effective model without making a provider request."""

    profile = (
        model
        if isinstance(model, ModelProfile)
        else ModelProfile(
            id="default",
            model=model,
            capabilities=tuple(AgentCapability),
        )
    )
    with _effective_model_configuration(
        profile=profile,
        configured_path=config_path,
    ) as effective_config:
        return cli.inspect_openclaw_model(
            openclaw_binary=openclaw_binary,
            openclaw_state_dir=state_dir,
            config_path=effective_config,
            model=profile.model,
            runtime_profile=profile.runtime_profile,
        )


def _render_model_inspection_start(profile_count: int) -> None:
    """Explain a bounded local model check before its first subprocess wait."""

    if profile_count < 1:
        raise ValueError("model inspection requires at least one profile")
    route_label = "route" if profile_count == 1 else "routes"
    print("\nChecking SAT's isolated model configuration...")
    print(
        f"  Verifying {profile_count} local catalog/auth {route_label} without "
        "generating content."
    )
    print(
        "  Each cold local model check may take up to "
        f"{MODEL_INSPECTION_TIMEOUT_SECONDS} seconds."
    )


_PROVIDER_SMOKE_DETAIL_MAX_CHARACTERS = 600
_PROVIDER_SMOKE_PARSE_MAX_CHARACTERS = 16_384
_ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


def _first_json_object(value: str) -> dict[str, object] | None:
    decoder = json.JSONDecoder()
    for index, character in enumerate(value):
        if character != "{":
            continue
        try:
            payload, _ = decoder.raw_decode(value[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _provider_error_fields(
    payload: dict[str, object],
    *,
    depth: int = 0,
) -> tuple[object | None, object | None, str | None] | None:
    if depth > 2:
        return None
    error = payload.get("error")
    if not isinstance(error, dict):
        return None
    code = error.get("code")
    status = error.get("status")
    message = error.get("message")
    if isinstance(message, str):
        nested = _first_json_object(message)
        if nested is not None:
            nested_fields = _provider_error_fields(nested, depth=depth + 1)
            if nested_fields is not None:
                return nested_fields
    return code, status, message if isinstance(message, str) else None


def _sanitize_provider_smoke_detail(
    value: str,
    *,
    credential_value: str | None,
) -> str:
    if credential_value:
        value = value.replace(credential_value, "[credential]")
    value = _ANSI_ESCAPE.sub("", value)
    value = "".join(
        character if ord(character) >= 32 and ord(character) != 127 else " "
        for character in value
    )
    if credential_value:
        value = value.replace(credential_value, "[credential]")
    value = " ".join(value.split())
    return value[:_PROVIDER_SMOKE_DETAIL_MAX_CHARACTERS].rstrip()


def _provider_smoke_failure_detail(
    result: subprocess.CompletedProcess[str],
    *,
    profile: ModelProfile,
) -> str | None:
    """Extract one bounded provider failure without reflecting credentials."""

    credential_env = profile.runtime_profile.credential_env
    credential_value = os.environ.get(credential_env) if credential_env else None
    combined = "\n".join(
        value for value in (result.stderr, result.stdout) if isinstance(value, str)
    )
    if credential_value:
        combined = combined.replace(credential_value, "[credential]")
    combined = combined[-_PROVIDER_SMOKE_PARSE_MAX_CHARACTERS:]
    payload = _first_json_object(combined)
    fields = _provider_error_fields(payload) if payload is not None else None
    if fields is not None:
        code, status, message = fields
        labels: list[str] = []
        if isinstance(code, int) and 100 <= code <= 599:
            labels.append(f"HTTP {code}")
        if isinstance(status, str) and re.fullmatch(r"[A-Z][A-Z0-9_ -]{0,63}", status):
            labels.append(status)
        structured = " ".join(labels)
        can_redact_provider_message = (
            profile.runtime_profile.credential_source is CredentialSource.NONE
            or bool(credential_value)
        )
        if message and can_redact_provider_message:
            structured = f"{structured}: {message}" if structured else message
        detail = _sanitize_provider_smoke_detail(
            structured,
            credential_value=credential_value,
        )
        if detail:
            return detail
    return None


def _run_provider_smoke(
    openclaw_binary: Path,
    model: str | ModelProfile,
    *,
    state_dir: Path,
    config_path: Path,
) -> None:
    """Run one explicitly authorized minimal provider request."""

    profile = (
        model
        if isinstance(model, ModelProfile)
        else ModelProfile(
            id="default",
            model=model,
            capabilities=tuple(AgentCapability),
        )
    )
    command = [
        str(openclaw_binary),
        "infer",
        "model",
        "run",
        "--local",
        "--model",
        profile.model,
    ]
    thinking_level = openclaw_invocation_thinking_level(profile.runtime_profile)
    if thinking_level is not None:
        command.extend(["--thinking", thinking_level])
    command.extend(
        [
            "--prompt",
            'Reply with exactly: {"status":"ok"}',
            "--json",
        ]
    )
    with _effective_model_configuration(
        profile=profile,
        configured_path=config_path,
    ) as effective_config:
        try:
            result = cli.run_bounded_process(
                command,
                timeout_seconds=180,
                termination_grace_seconds=5,
                environment={
                    **os.environ,
                    **isolated_openclaw_environment(
                        state_dir=state_dir,
                        config_path=effective_config,
                    ),
                },
            )
        except BoundedProcessTimeoutError as error:
            raise RuntimeConfigurationError(
                "the provider smoke check timed out after "
                f"{error.timeout_seconds:g} seconds; SAT stopped its provider "
                "processes. Check provider availability and network access, then "
                "retry with `sat configure`"
            ) from error
        except BoundedProcessError as error:
            raise RuntimeConfigurationError(
                "the provider smoke check could not establish a clean process "
                "boundary; run `sat cleanup`, then retry with `sat configure`"
            ) from error
        except (OSError, subprocess.SubprocessError) as error:
            raise RuntimeConfigurationError(
                "the provider smoke check could not run"
            ) from error
    if result.returncode != 0:
        message = f"the provider smoke check failed for {profile.model}"
        detail = _provider_smoke_failure_detail(result, profile=profile)
        if detail is not None:
            message = f"{message}: {detail}"
        raise RuntimeConfigurationError(message)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeConfigurationError(
            "the provider smoke check returned invalid JSON"
        ) from error
    outputs = payload.get("outputs") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or payload.get("ok") is not True
        or not isinstance(outputs, list)
        or not outputs
    ):
        raise RuntimeConfigurationError(
            "the provider smoke check did not return a successful response"
        )
    if (
        payload.get("provider") != profile.runtime_profile.provider_id
        or payload.get("model") != profile.runtime_profile.native_model_id
    ):
        raise RuntimeConfigurationError(
            "the provider smoke check used a route other than the frozen profile"
        )


def _discover_openclaw_default_model(
    openclaw_binary: Path,
    *,
    state_dir: Path,
    config_path: Path,
) -> str | None:
    """Read OpenClaw's local default model without probing the provider."""

    try:
        result = subprocess.run(
            [str(openclaw_binary), "models", "status", "--json"],
            check=False,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=30,
            env={
                **os.environ,
                **isolated_openclaw_environment(
                    state_dir=state_dir,
                    config_path=config_path,
                ),
            },
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    for key in ("resolvedDefault", "defaultModel"):
        model = payload.get(key)
        if isinstance(model, str):
            try:
                return UserConfiguration(model=model).model
            except ValidationError:
                continue
    return None


def _validate_optional_openclaw_configuration(config_path: Path) -> None:
    """Reject unsafe persistent provider configuration without requiring it."""

    if config_path.is_symlink():
        raise RuntimeConfigurationError(
            "SAT OpenClaw configuration must not be a symbolic link"
        )
    if config_path.exists() and not config_path.is_file():
        raise RuntimeConfigurationError(
            "SAT OpenClaw configuration must be a regular file when present"
        )


def _read_private_openclaw_configuration(config_path: Path) -> dict[str, object]:
    """Read one private OpenClaw config without exposing its secret values."""

    _validate_optional_openclaw_configuration(config_path)
    if not config_path.is_file():
        return {}
    try:
        payload = json5.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise RuntimeConfigurationError(
            "SAT's private OpenClaw configuration is not valid JSON5"
        ) from error
    if not isinstance(payload, dict):
        raise RuntimeConfigurationError(
            "SAT's private OpenClaw configuration must be an object"
        )
    return payload


def _bind_private_runtime_profiles(
    configuration: UserConfiguration,
    *,
    config_path: Path,
    refresh_private_configuration: bool = False,
) -> UserConfiguration:
    """Promote sanitized custom-provider facts into the shared profile owner."""

    payload = _read_private_openclaw_configuration(config_path)
    profiles: list[ModelProfile] = []
    for profile in configuration.model_profiles:
        source = profile.runtime_profile.source
        may_bind = source is ModelRuntimeProfileSource.OPENCLAW_NATIVE or (
            refresh_private_configuration
            and source is ModelRuntimeProfileSource.PRIVATE_OPENCLAW_CONFIG
        )
        if not may_bind:
            profiles.append(profile)
            continue
        try:
            runtime_profile = runtime_profile_from_openclaw_configuration(
                profile.model,
                payload,
            )
        except (ValueError, ValidationError) as error:
            raise RuntimeConfigurationError(
                f"OpenClaw provider metadata is invalid for {profile.model}"
            ) from error
        if runtime_profile is None:
            if source is ModelRuntimeProfileSource.PRIVATE_OPENCLAW_CONFIG:
                raise RuntimeConfigurationError(
                    f"OpenClaw provider metadata is missing for {profile.model}"
                )
            profiles.append(profile)
            continue
        profiles.append(
            ModelProfile.model_validate(
                {
                    **profile.model_dump(mode="json"),
                    "runtime_profile": runtime_profile.model_dump(mode="json"),
                }
            )
        )
    return UserConfiguration.model_validate(
        {
            **configuration.model_dump(mode="json"),
            "model_profiles": tuple(profiles),
        }
    )


def _inspect_model_configuration(
    configuration: UserConfiguration,
    *,
    state_dir: Path,
    config_path: Path,
) -> tuple[OpenClawModelInspection, ...]:
    """Validate every saved route through the same compiled profile boundary."""

    for profile in configuration.model_profiles:
        runtime = profile.runtime_profile
        if (
            runtime.credential_source is CredentialSource.ENVIRONMENT
            and runtime.credential_env is not None
            and not os.environ.get(runtime.credential_env, "").strip()
        ):
            raise RuntimeConfigurationError(
                f"credential environment variable is missing for {profile.model}: "
                f"{runtime.credential_env}"
            )
        if (
            runtime.endpoint_kind is not ModelEndpointKind.OPENCLAW_NATIVE
            and runtime.supports_tools is not True
        ):
            raise RuntimeConfigurationError(
                f"model profile does not declare required tool support: {profile.model}"
            )
    _render_model_inspection_start(len(configuration.model_profiles))
    inspections = tuple(
        cli._inspect_selected_model(
            DEFAULT_OPENCLAW_BINARY,
            profile,
            state_dir=state_dir,
            config_path=config_path,
        )
        for profile in configuration.model_profiles
    )
    unavailable = tuple(item for item in inspections if not item.available)
    if unavailable:
        raise RuntimeConfigurationError(
            "selected model configuration is not locally ready: "
            + "; ".join(
                f"{inspection.model}: {inspection.error}" for inspection in unavailable
            )
        )
    return inspections


def _merge_discovered_model_metadata(
    configuration: UserConfiguration,
    inspections: Sequence[OpenClawModelInspection],
    *,
    observed_at: datetime | None = None,
) -> UserConfiguration:
    """Merge attributable catalog facts without prompting or inventing values."""

    when = (observed_at or datetime.now(UTC)).astimezone(UTC)
    by_model = {item.model: item for item in inspections}
    if len(by_model) != len(inspections) or set(by_model) != {
        profile.model for profile in configuration.model_profiles
    }:
        raise RuntimeConfigurationError(
            "model metadata inspections do not cover the configured profiles"
        )
    profiles: list[ModelProfile] = []
    for profile in configuration.model_profiles:
        inspection = by_model[profile.model]
        updates: dict[str, object] = {}
        if (
            inspection.context_window_tokens is not None
            and profile.context_source
            not in {
                ModelMetadataSource.USER_SUPPLIED,
                ModelMetadataSource.PROVIDER_CATALOG,
            }
        ):
            updates.update(
                context_window_tokens=inspection.context_window_tokens,
                context_source=ModelMetadataSource.RUNTIME_CATALOG,
                context_observed_at=when,
            )
        if (
            inspection.input_cost_per_million_usd is not None
            and inspection.output_cost_per_million_usd is not None
            and profile.pricing_source
            not in {
                ModelMetadataSource.USER_SUPPLIED,
                ModelMetadataSource.CONFIRMED_ZERO,
            }
        ):
            updates.update(
                input_cost_per_million_usd=(inspection.input_cost_per_million_usd),
                output_cost_per_million_usd=(inspection.output_cost_per_million_usd),
                pricing_source=ModelMetadataSource.RUNTIME_CATALOG,
                pricing_observed_at=when,
            )
        if inspection.cache_pricing is not None and (
            profile.cache_pricing is None
            or profile.cache_pricing.source
            not in {
                ModelMetadataSource.USER_SUPPLIED,
                ModelMetadataSource.CONFIRMED_ZERO,
            }
        ):
            updates["cache_pricing"] = inspection.cache_pricing
        profiles.append(profile.model_copy(update=updates))
    return configuration.model_copy(update={"model_profiles": tuple(profiles)})
