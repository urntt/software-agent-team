"""OpenClaw model inspection, compatibility overlays, and check configuration."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from contextlib import suppress
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from software_agent_team.model_costs import CachePricing
from software_agent_team.model_metadata import ModelMetadataSource
from software_agent_team.model_routing import ModelProfile
from software_agent_team.model_runtime import (
    ModelRuntimeProfile,
    has_runtime_profile_preset,
    openclaw_agent_model_settings,
    openclaw_provider_payload,
    runtime_profile_for_model,
)
from software_agent_team.openclaw_runtime import isolated_openclaw_environment
from software_agent_team.submissions import ARTIFACT_SUBMISSION_TOOL
from software_agent_team.terminal_presentation import display_text

from .preflight import _persist_private_json
from .types import (
    MODEL_INSPECTION_TIMEOUT_SECONDS,
    OpenClawModelInspection,
    RuntimeConfigurationError,
    _ArtifactSubmissionMode,
)


def has_model_compatibility(model: str) -> bool:
    """Return whether SAT carries a catalog supplement for a pinned runtime."""

    return has_runtime_profile_preset(model)


def _apply_model_compatibility(
    payload: dict[str, Any],
    model: str,
    *,
    runtime_profile: ModelRuntimeProfile | None = None,
    artifact_submission_mode: _ArtifactSubmissionMode | None = None,
) -> None:
    profile = runtime_profile or runtime_profile_for_model(model)
    provider_id, separator, _ = model.partition("/")
    if not separator or profile.provider_id != provider_id:
        raise RuntimeConfigurationError(
            "model runtime profile does not match its provider/model reference"
        )

    compiled_agent = openclaw_agent_model_settings(
        profile,
        artifact_submission_mode=artifact_submission_mode,
        artifact_tool_name=ARTIFACT_SUBMISSION_TOOL,
    )
    if compiled_agent is not None:
        agents = payload.setdefault("agents", {})
        defaults = agents.setdefault("defaults", {})
        agent_models = defaults.setdefault("models", {})
        if not isinstance(agent_models, dict):
            raise RuntimeConfigurationError("OpenClaw Agent model settings are invalid")
        configured_agent = agent_models.get(model)
        if configured_agent is None:
            agent_models[model] = compiled_agent
        elif configured_agent != compiled_agent:
            raise RuntimeConfigurationError(
                f"OpenClaw Agent model settings conflict with profile: {model}"
            )

    provider_payload = openclaw_provider_payload(profile)
    if provider_payload is None:
        return

    models = payload.setdefault("models", {})
    if not isinstance(models, dict):
        raise RuntimeConfigurationError("OpenClaw model catalog is invalid")
    models.setdefault("mode", "merge")
    providers = models.setdefault("providers", {})
    if not isinstance(providers, dict):
        raise RuntimeConfigurationError("OpenClaw model providers are invalid")
    configured_provider = providers.get(provider_id)
    if configured_provider is None:
        providers[provider_id] = provider_payload
        return
    if not isinstance(configured_provider, dict):
        raise RuntimeConfigurationError(
            f"OpenClaw provider configuration is invalid: {provider_id}"
        )
    for key, value in provider_payload.items():
        if key == "models":
            continue
        configured_value = configured_provider.get(key)
        if configured_value is not None and configured_value != value:
            raise RuntimeConfigurationError(
                f"OpenClaw provider conflicts with runtime profile: {provider_id}"
            )
        configured_provider[key] = value
    configured_models = configured_provider.setdefault("models", [])
    if not isinstance(configured_models, list):
        raise RuntimeConfigurationError(
            f"OpenClaw provider model catalog is invalid: {provider_id}"
        )
    compatibility_model = provider_payload["models"][0]
    if not any(
        isinstance(item, dict) and item.get("id") == compatibility_model["id"]
        for item in configured_models
    ):
        configured_models.append(compatibility_model)


def materialize_model_check_configuration(
    destination: Path,
    *,
    model: str | None = None,
    profile: ModelProfile | None = None,
) -> Path:
    """Write one profile-derived config for local catalog checks and smoke."""

    if profile is None and model is None:
        raise RuntimeConfigurationError("model check requires one model profile")
    if profile is not None:
        if model is not None and model.strip() != profile.model:
            raise RuntimeConfigurationError(
                "model check profile differs from its model reference"
            )
        normalized = profile.model
        runtime_profile = profile.runtime_profile
    else:
        assert model is not None
        normalized = model.strip()
        runtime_profile = runtime_profile_for_model(normalized)
    provider, separator, model_id = normalized.partition("/")
    if not provider or not separator or not model_id:
        raise RuntimeConfigurationError(
            "OpenClaw model must use a provider/model reference"
        )
    payload: dict[str, Any] = {
        "agents": {
            "defaults": {
                "model": {"primary": normalized, "fallbacks": []},
            }
        }
    }
    _apply_model_compatibility(
        payload,
        normalized,
        runtime_profile=runtime_profile,
    )
    return _persist_private_json(
        payload,
        destination,
        label="model check configuration",
    )


def _inspect_deferred_model_auth(
    *,
    openclaw_binary: Path,
    state_dir: Path,
    config_path: Path,
    configured: dict[str, Any],
    model: str,
    owner: str | None,
    deadline: float,
    timeout_seconds: int,
) -> str | None:
    """Resolve an omitted catalog flag through the SDK's local readiness check.

    A catalog projection can defer auth availability for inherited agent-local
    profiles. The SDK's `--check` owns readiness; `--probe` is never enabled.
    Inspect only the requested route, including an approved unused fallback,
    without changing the runtime roster, saved config, or credential owner.
    """

    payload = json.loads(json.dumps(configured))
    agents = payload.setdefault("agents", {})
    if not isinstance(agents, dict):
        return "OpenClaw local auth inspection has no valid Agent configuration"
    defaults = agents.setdefault("defaults", {})
    if not isinstance(defaults, dict):
        return "OpenClaw local auth inspection has no valid model defaults"
    defaults["model"] = {"primary": model, "fallbacks": []}
    if owner is not None:
        agents["entries"][owner]["model"] = {"primary": model, "fallbacks": []}
    argv = [str(openclaw_binary), "models", "status", "--json", "--check"]
    if owner is not None:
        argv.extend(("--agent", owner))
    try:
        with tempfile.TemporaryDirectory(
            prefix=".model-auth-check-", dir=config_path.parent
        ) as temporary:
            auth_config = _persist_private_json(
                payload,
                Path(temporary) / "openclaw.json",
                label="local auth inspection configuration",
            )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(argv, timeout_seconds)
            result = subprocess.run(
                argv,
                check=False,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=remaining,
                env=os.environ
                | isolated_openclaw_environment(
                    state_dir=state_dir, config_path=auth_config
                ),
                shell=False,
            )
    except subprocess.TimeoutExpired as error:
        raise RuntimeConfigurationError(
            "OpenClaw model inspection timed out after "
            f"{timeout_seconds} seconds; no provider request was made"
        ) from error
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeConfigurationError(
            "OpenClaw local auth inspection failed before completion "
            f"({type(error).__name__}); no provider request was made"
        ) from error
    # Status output contains credential labels and masked fragments. Never put
    # stdout/stderr into a diagnostic, even when the SDK check fails.
    if result.returncode != 0:
        return (
            "OpenClaw local auth/runtime readiness check exited with status "
            f"{result.returncode} for {model}; no provider request was made"
        )
    try:
        status = json.loads(result.stdout)
    except json.JSONDecodeError:
        return "OpenClaw local auth inspection returned invalid JSON"
    if not isinstance(status, dict) or status.get("resolvedDefault") != model:
        return "OpenClaw local auth inspection did not verify the requested model"
    return None


def inspect_openclaw_model(
    *,
    openclaw_binary: Path,
    openclaw_state_dir: Path,
    config_path: Path,
    model: str,
    runtime_profile: ModelRuntimeProfile | None = None,
    timeout_seconds: int = MODEL_INSPECTION_TIMEOUT_SECONDS,
) -> OpenClawModelInspection:
    """Check one exact local model catalog/auth route without generation."""

    normalized = model.strip()
    provider, separator, model_id = normalized.partition("/")
    if not provider or not separator or not model_id:
        raise RuntimeConfigurationError(
            "OpenClaw model must use a provider/model reference"
        )
    if timeout_seconds < 1:
        raise RuntimeConfigurationError("model inspection timeout must be positive")
    if runtime_profile is not None and runtime_profile.provider_id != provider:
        raise RuntimeConfigurationError(
            "model inspection runtime profile differs from its model reference"
        )
    if not openclaw_binary.is_file() or not os.access(openclaw_binary, os.X_OK):
        raise RuntimeConfigurationError("OpenClaw binary is unavailable")
    if not config_path.is_file() or config_path.is_symlink():
        raise RuntimeConfigurationError("model inspection configuration is unavailable")
    if not openclaw_state_dir.is_dir() or openclaw_state_dir.is_symlink():
        raise RuntimeConfigurationError("SAT OpenClaw state directory is unavailable")

    try:
        configured = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        configured = None
    argv = [str(openclaw_binary), "models", "list", "--json"]
    agents = configured.get("agents") if isinstance(configured, dict) else None
    entries = agents.get("entries") if isinstance(agents, dict) else None
    owner = None
    if isinstance(entries, dict) and entries:
        # Catalog/auth inspection needs an explicit existing roster owner.
        # Unassigned approved fallback routes share that owner's local catalog;
        # this read-only choice does not reassign tasks or change any model.
        owner = next(
            (
                agent_id
                for agent_id, agent in entries.items()
                if isinstance(agent, dict)
                and isinstance(agent.get("model"), dict)
                and agent["model"].get("primary") == normalized
            ),
            next(iter(entries)),
        )
        argv.extend(("--agent", owner))
    deadline = time.monotonic() + timeout_seconds
    try:
        result = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout_seconds,
            env={
                **os.environ,
                **isolated_openclaw_environment(
                    state_dir=openclaw_state_dir,
                    config_path=config_path,
                ),
            },
            shell=False,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeConfigurationError(
            "OpenClaw model inspection timed out after "
            f"{timeout_seconds} seconds; no provider request was made"
        ) from error
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeConfigurationError(
            "OpenClaw model inspection failed before completion "
            f"({type(error).__name__}); no provider request was made"
        ) from error
    if result.returncode != 0:
        detail = display_text(result.stderr or result.stdout, limit=600)
        return OpenClawModelInspection(
            model=normalized,
            runtime_profile_sha256=(
                runtime_profile.sha256 if runtime_profile is not None else None
            ),
            api=(runtime_profile.api if runtime_profile is not None else None),
            endpoint_kind=(
                runtime_profile.endpoint_kind if runtime_profile is not None else None
            ),
            available=False,
            error=f"OpenClaw model listing exited with status {result.returncode}"
            + (f": {detail}" if detail else ""),
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return OpenClawModelInspection(
            model=normalized,
            runtime_profile_sha256=(
                runtime_profile.sha256 if runtime_profile is not None else None
            ),
            api=(runtime_profile.api if runtime_profile is not None else None),
            endpoint_kind=(
                runtime_profile.endpoint_kind if runtime_profile is not None else None
            ),
            available=False,
            error="OpenClaw model listing returned invalid JSON",
        )
    entries = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return OpenClawModelInspection(
            model=normalized,
            runtime_profile_sha256=(
                runtime_profile.sha256 if runtime_profile is not None else None
            ),
            api=(runtime_profile.api if runtime_profile is not None else None),
            endpoint_kind=(
                runtime_profile.endpoint_kind if runtime_profile is not None else None
            ),
            available=False,
            error="OpenClaw model listing omitted its model catalog",
        )
    matches = [
        entry
        for entry in entries
        if isinstance(entry, dict) and entry.get("key") == normalized
    ]
    if len(matches) != 1:
        return OpenClawModelInspection(
            model=normalized,
            runtime_profile_sha256=(
                runtime_profile.sha256 if runtime_profile is not None else None
            ),
            api=(runtime_profile.api if runtime_profile is not None else None),
            endpoint_kind=(
                runtime_profile.endpoint_kind if runtime_profile is not None else None
            ),
            available=False,
            error=f"OpenClaw does not recognize the configured model: {normalized}",
        )
    matched = matches[0]
    availability = matched.get("available")
    auth_error = None
    if availability is None and isinstance(configured, dict):
        auth_error = _inspect_deferred_model_auth(
            openclaw_binary=openclaw_binary,
            state_dir=openclaw_state_dir,
            config_path=config_path,
            configured=configured,
            model=normalized,
            owner=owner,
            deadline=deadline,
            timeout_seconds=timeout_seconds,
        )
        availability = auth_error is None
    if availability is not True:
        return OpenClawModelInspection(
            model=normalized,
            runtime_profile_sha256=(
                runtime_profile.sha256 if runtime_profile is not None else None
            ),
            api=(runtime_profile.api if runtime_profile is not None else None),
            endpoint_kind=(
                runtime_profile.endpoint_kind if runtime_profile is not None else None
            ),
            available=False,
            error=(
                auth_error
                or "OpenClaw has no available catalog/auth route for the configured "
                f"model: {normalized}"
            ),
        )
    raw_context = matched.get("contextWindow")
    context_window = (
        raw_context
        if isinstance(raw_context, int)
        and not isinstance(raw_context, bool)
        and raw_context >= 1
        else None
    )
    raw_input = matched.get("input")
    if isinstance(raw_input, str):
        input_modalities = tuple(
            item.strip().casefold()
            for item in raw_input.replace(",", "+").split("+")
            if item.strip()
        )
    elif isinstance(raw_input, list):
        input_modalities = tuple(
            item.strip().casefold()
            for item in raw_input
            if isinstance(item, str) and item.strip()
        )
    else:
        input_modalities = ()
    discovered_prices: tuple[Decimal | None, Decimal | None] = (None, None)
    raw_input_price = matched.get("inputCostPerMillionUsd")
    raw_output_price = matched.get("outputCostPerMillionUsd")
    if raw_input_price is not None and raw_output_price is not None:
        try:
            input_price = Decimal(str(raw_input_price))
            output_price = Decimal(str(raw_output_price))
        except (InvalidOperation, ValueError):
            pass
        else:
            if input_price >= 0 and output_price >= 0:
                discovered_prices = (input_price, output_price)
    provider_timeout: int | None = None
    if isinstance(configured, dict):
        models = configured.get("models")
        providers = models.get("providers") if isinstance(models, dict) else None
        provider_id = normalized.partition("/")[0]
        provider_config = (
            providers.get(provider_id) if isinstance(providers, dict) else None
        )
        raw_timeout = (
            provider_config.get("timeoutSeconds")
            if isinstance(provider_config, dict)
            else None
        )
        if (
            isinstance(raw_timeout, int)
            and not isinstance(raw_timeout, bool)
            and raw_timeout >= 1
        ):
            provider_timeout = raw_timeout
    raw_local = matched.get("local")
    cache_pricing = None
    # Do not infer a cache rate from the ordinary-input rate or a model name.
    # Some catalog surfaces omit prices entirely; admission then asks the user.
    raw_cost = matched.get("cost")
    if isinstance(raw_cost, dict):
        with suppress(ValueError, InvalidOperation):
            cache_pricing = CachePricing(
                read_cost_per_million_usd=raw_cost.get("cacheRead"),
                write_cost_per_million_usd=raw_cost.get("cacheWrite"),
                source=ModelMetadataSource.RUNTIME_CATALOG,
                observed_at=datetime.now(UTC),
            )
    return OpenClawModelInspection(
        model=normalized,
        runtime_profile_sha256=(
            runtime_profile.sha256 if runtime_profile is not None else None
        ),
        api=(runtime_profile.api if runtime_profile is not None else None),
        endpoint_kind=(
            runtime_profile.endpoint_kind if runtime_profile is not None else None
        ),
        available=True,
        local=raw_local if isinstance(raw_local, bool) else None,
        provider_request_timeout_seconds=provider_timeout,
        context_window_tokens=context_window,
        input_modalities=tuple(dict.fromkeys(input_modalities)),
        input_cost_per_million_usd=discovered_prices[0],
        output_cost_per_million_usd=discovered_prices[1],
        cache_pricing=cache_pricing,
    )


__all__ = [
    "OpenClawModelInspection",
    "_apply_model_compatibility",
    "_inspect_deferred_model_auth",
    "has_model_compatibility",
    "inspect_openclaw_model",
    "materialize_model_check_configuration",
]
