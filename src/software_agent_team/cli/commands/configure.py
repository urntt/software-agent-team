"""CLI interactive configuration command (`sat configure`) and profile management."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import warnings
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from software_agent_team import cli
from software_agent_team.cli.constants import (
    DEFAULT_PRODUCT_POLICY,
    PROJECT_ROOT,
)
from software_agent_team.cli.model_inspection import (
    _bind_private_runtime_profiles,
    _merge_discovered_model_metadata,
    _validate_optional_openclaw_configuration,
)
from software_agent_team.managed_install import is_retired_managed_application
from software_agent_team.model_costs import CachePricing
from software_agent_team.model_metadata import ModelMetadataSource
from software_agent_team.model_routing import (
    ModelProfile,
    ModelRoutingMode,
)
from software_agent_team.model_runtime import (
    ArtifactSubmissionPolicy,
    CredentialSource,
    ModelApi,
    ModelEndpointKind,
    ModelRuntimeProfile,
    ModelRuntimeProfileSource,
)
from software_agent_team.openclaw_runtime import (
    is_owned_openclaw_runtime,
    isolated_openclaw_environment,
)
from software_agent_team.paths import user_state_root
from software_agent_team.product import ProductStatePaths, ensure_product_state
from software_agent_team.runtime_configuration import (
    OpenClawModelInspection,
    RuntimeConfigurationError,
)
from software_agent_team.sdk_state_migration import (
    StateMigrationCleanupError,
    preflight_provider_database_copy,
)
from software_agent_team.teams import (
    AgentCapability,
    ModelSwitchCondition,
)
from software_agent_team.terminal_presentation import display_text
from software_agent_team.user_configuration import (
    UserConfiguration,
    load_user_configuration,
    user_configuration_path,
)
from software_agent_team.workspace_mounts import (
    WorkspaceMountError,
    repair_legacy_openclaw_skill_workspaces,
)


def _print_configuration(configuration: UserConfiguration, path: Path) -> None:
    print(f"configuration: {path}")
    print(f"model routing: {configuration.routing_mode.value}")
    print(f"default model profile: {configuration.default_model_profile_id}")
    print("model profiles:")
    print(
        "Input rates apply to uncached input; "
        "cache reads and writes are priced separately."
    )
    for profile in configuration.model_profiles:
        capabilities = ", ".join(item.value for item in profile.capabilities)
        pricing = (
            "pricing not configured"
            if profile.input_cost_per_million_usd is None
            else (
                f"${profile.input_cost_per_million_usd} input / "
                f"${profile.output_cost_per_million_usd} output per million tokens "
                f"({profile.pricing_source.value})"
            )
        )
        context = (
            "context length unknown"
            if profile.context_window_tokens is None
            else (
                f"{profile.context_window_tokens} context tokens "
                f"({profile.context_source.value})"
            )
        )
        cache = profile.cache_pricing
        pricing += (
            "; cache pricing unknown"
            if cache is None
            else f"; cache ${cache.read_cost_per_million_usd} read / "
            f"${cache.write_cost_per_million_usd} write per million "
            f"({cache.source.value})"
        )
        default = (
            " [default]" if profile.id == configuration.default_model_profile_id else ""
        )
        print(
            f"  - {profile.id}{default}: {profile.model}; priority "
            f"{profile.priority}; {capabilities}; {pricing}; {context}"
        )
        runtime = profile.runtime_profile
        transport = "OpenClaw native" if runtime.api is None else runtime.api.value
        endpoint = runtime.base_url or "OpenClaw-managed"
        print(
            f"    runtime: {transport}; {runtime.endpoint_kind.value}; "
            f"{endpoint}; profile {profile.runtime_profile_sha256[:12]}"
        )
        if runtime.credential_env is not None:
            print(f"    credential reference: env:{runtime.credential_env}")
    if configuration.capability_profile_overrides:
        print("capability routes:")
        for capability, profile_id in sorted(
            configuration.capability_profile_overrides.items(),
            key=lambda item: item[0].value,
        ):
            print(f"  - {capability.value}: {profile_id}")
    if configuration.stage_profile_overrides:
        print("stage routes:")
        for stage_id, profile_id in sorted(
            configuration.stage_profile_overrides.items()
        ):
            print(f"  - {stage_id}: {profile_id}")
    switches = ", ".join(
        condition.value for condition in configuration.authorized_switch_conditions
    )
    print(f"authorized model switches: {switches or 'none'}")
    print(f"maximum concurrent Agents: {configuration.max_concurrency}")
    print(f"progress visibility: {configuration.progress_visibility}")
    print(f"progress display: {configuration.progress_display}")
    print(f"progress color: {configuration.progress_color}")


def _load_user_configuration(path: Path | None = None) -> UserConfiguration | None:
    """Load defaults while making one-way configuration migration visible."""

    return load_user_configuration(
        path,
        on_migration=lambda message: print(f"configuration migration: {message}"),
    )


def _timeout_flag(args: argparse.Namespace) -> tuple[bool, int | None]:
    """Resolve new and deprecated timeout flags without parallel semantics."""

    if args.use_role_timeouts:
        return True, None
    if args.stage_timeout_seconds is not None:
        return True, args.stage_timeout_seconds
    if args.deprecated_agent_timeout_seconds is not None:
        print(
            "warning: --agent-timeout-seconds is deprecated; it now means one "
            "invocation timeout applied independently to a bounded response "
            "correction. Use "
            "--stage-timeout-seconds instead."
        )
        return True, args.deprecated_agent_timeout_seconds
    return False, None


def _complete_model_metadata(
    configuration: UserConfiguration,
    inspections: Sequence[OpenClawModelInspection],
    *,
    offer_price_change: bool,
    observed_at: datetime | None = None,
) -> UserConfiguration:
    """Discover model metadata first, then ask only for unknowns or overrides."""

    when = (observed_at or datetime.now(UTC)).astimezone(UTC)
    by_model = {inspection.model: inspection for inspection in inspections}
    if len(by_model) != len(inspections):
        raise RuntimeConfigurationError("model metadata inspections are not unique")
    expected = {profile.model for profile in configuration.model_profiles}
    if set(by_model) != expected:
        raise RuntimeConfigurationError(
            "model metadata inspections do not cover the configured profiles"
        )

    profiles: list[ModelProfile] = []
    for profile in configuration.model_profiles:
        inspection = by_model[profile.model]
        if not inspection.available:
            profiles.append(profile)
            continue
        updates: dict[str, object] = {}
        print(f"\nModel metadata: {profile.id} ({profile.model})")

        if inspection.context_window_tokens is not None:
            context = inspection.context_window_tokens
            updates.update(
                context_window_tokens=context,
                context_source=ModelMetadataSource.RUNTIME_CATALOG,
                context_observed_at=when,
            )
            print(f"  Context: {context} tokens (runtime catalog)")
        elif profile.context_window_tokens is not None:
            print(
                f"  Context: {profile.context_window_tokens} tokens "
                f"({profile.context_source.value})"
            )
        else:
            print("  Context: not available from the runtime catalog")
            context = cli._prompt_context_window(profile.model)
            updates.update(
                context_window_tokens=context,
                context_source=ModelMetadataSource.USER_SUPPLIED,
                context_observed_at=when,
            )

        discovered_prices = (
            inspection.input_cost_per_million_usd,
            inspection.output_cost_per_million_usd,
        )
        saved_prices = (
            profile.input_cost_per_million_usd,
            profile.output_cost_per_million_usd,
        )
        use_discovered = all(
            value is not None for value in discovered_prices
        ) and profile.pricing_source not in {
            ModelMetadataSource.USER_SUPPLIED,
            ModelMetadataSource.CONFIRMED_ZERO,
        }
        if use_discovered:
            input_price, output_price = discovered_prices
            assert input_price is not None and output_price is not None
            print(
                "  Discovered price: "
                f"${input_price} input / ${output_price} output per million tokens"
            )
            change_prices = offer_price_change and not cli._prompt_yes_no(
                "Use the runtime-catalog prices for this model?",
                default=True,
            )
            if not change_prices:
                updates.update(
                    input_cost_per_million_usd=input_price,
                    output_cost_per_million_usd=output_price,
                    pricing_source=ModelMetadataSource.RUNTIME_CATALOG,
                    pricing_observed_at=when,
                )
                profiles.append(
                    ModelProfile.model_validate(
                        {**profile.model_dump(mode="json"), **updates}
                    )
                )
                continue
        elif all(value is not None for value in saved_prices):
            input_price, output_price = saved_prices
            assert input_price is not None and output_price is not None
            print(
                "  Saved price: "
                f"${input_price} input / ${output_price} output per million tokens "
                f"({profile.pricing_source.value})"
            )
            change_prices = offer_price_change and cli._prompt_yes_no(
                "Change these saved prices?",
                default=False,
            )
            if not change_prices:
                profiles.append(
                    ModelProfile.model_validate(
                        {**profile.model_dump(mode="json"), **updates}
                    )
                )
                continue
        else:
            print("  Price: not available from the runtime catalog")

        input_price = cli._prompt_nonnegative_decimal(
            "Input price per million tokens (USD)"
        )
        output_price = cli._prompt_nonnegative_decimal(
            "Output price per million tokens (USD)"
        )
        source = (
            ModelMetadataSource.CONFIRMED_ZERO
            if input_price == 0 and output_price == 0
            else ModelMetadataSource.USER_SUPPLIED
        )
        if source is ModelMetadataSource.CONFIRMED_ZERO and not cli._prompt_yes_no(
            "Confirm that this model route costs $0 for both input and output?",
            default=False,
        ):
            raise RuntimeConfigurationError("zero-price confirmation was declined")
        updates.update(
            input_cost_per_million_usd=input_price,
            output_cost_per_million_usd=output_price,
            pricing_source=source,
            pricing_observed_at=when,
        )
        profiles.append(
            ModelProfile.model_validate({**profile.model_dump(mode="json"), **updates})
        )

    complete_profiles = []
    for profile in profiles:
        discovered = by_model[profile.model].cache_pricing
        cache_prices = (
            profile.cache_pricing
            if profile.cache_pricing is not None
            and profile.cache_pricing.source
            in {
                ModelMetadataSource.USER_SUPPLIED,
                ModelMetadataSource.CONFIRMED_ZERO,
            }
            else discovered or profile.cache_pricing
        )
        if cache_prices is not None:
            print(
                f"  {profile.model} cache price: "
                f"${cache_prices.read_cost_per_million_usd} read / "
                f"${cache_prices.write_cost_per_million_usd} write per million tokens "
                f"({cache_prices.source.value})"
            )
            change = offer_price_change and cli._prompt_yes_no(
                "Change these cache prices?",
                default=False,
            )
        else:
            print(
                f"  {profile.model}: cache prices are unknown; "
                "they are not assumed free."
            )
            change = True
        if change:
            read_price = cli._prompt_nonnegative_decimal(
                "Cache read input price per million tokens (USD)"
            )
            write_price = cli._prompt_nonnegative_decimal(
                "Cache write input price per million tokens "
                "(USD; 0 if not separately billed)"
            )
            source = ModelMetadataSource.USER_SUPPLIED
            if read_price == 0 and write_price == 0:
                if not cli._prompt_yes_no(
                    "Confirm that both cache input buckets cost $0?", default=False
                ):
                    raise RuntimeConfigurationError(
                        "zero-cache-price confirmation was declined"
                    )
                source = ModelMetadataSource.CONFIRMED_ZERO
            cache_prices = CachePricing(
                read_cost_per_million_usd=read_price,
                write_cost_per_million_usd=write_price,
                source=source,
                observed_at=when,
            )
        complete_profiles.append(
            profile.model_copy(update={"cache_pricing": cache_prices})
        )

    return UserConfiguration.model_validate(
        {
            **configuration.model_dump(mode="json"),
            "model_profiles": tuple(complete_profiles),
        }
    )


def _split_configuration_assignment(value: str, *, label: str) -> tuple[str, str]:
    left, separator, right = value.partition("=")
    if (
        not separator
        or not left
        or not right
        or left != left.strip()
        or right != right.strip()
    ):
        raise ValueError(f"{label} must use NAME=VALUE")
    return left, right


def _replace_profile(
    profiles: list[ModelProfile],
    profile_id: str,
    **updates: object,
) -> None:
    for index, profile in enumerate(profiles):
        if profile.id == profile_id:
            profiles[index] = ModelProfile.model_validate(
                {**profile.model_dump(mode="json"), **updates}
            )
            return
    raise ValueError(f"unknown model profile: {profile_id}")


def _configuration_assignment_map(
    values: Sequence[str],
    *,
    label: str,
) -> dict[str, str]:
    """Parse repeatable profile assignments without last-write ambiguity."""

    assignments: dict[str, str] = {}
    for value in values:
        profile_id, assigned = _split_configuration_assignment(value, label=label)
        if profile_id in assignments:
            raise ValueError(f"{label} repeats profile {profile_id}")
        assignments[profile_id] = assigned
    return assignments


def _configuration_bool(value: str, *, label: str) -> bool:
    normalized = value.casefold()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise ValueError(f"{label} requires true or false")


def _apply_runtime_profile_arguments(
    profiles: list[ModelProfile],
    *,
    args: argparse.Namespace,
) -> None:
    """Apply transport-neutral provider fields to existing model profiles."""

    raw_fields = {
        "api": (args.profile_api, "--profile-api"),
        "endpoint_kind": (
            args.profile_endpoint_kind,
            "--profile-endpoint-kind",
        ),
        "base_url": (args.profile_base_url, "--profile-base-url"),
        "native_model_id": (
            args.profile_native_model_id,
            "--profile-native-model-id",
        ),
        "credential_env": (
            args.profile_credential_env,
            "--profile-credential-env",
        ),
        "context_window_tokens": (
            args.profile_context_window_tokens,
            "--profile-context-window-tokens",
        ),
        "max_output_tokens": (
            args.profile_max_output_tokens,
            "--profile-max-output-tokens",
        ),
        "input_modalities": (
            args.profile_input_modalities,
            "--profile-input-modalities",
        ),
        "supports_tools": (
            args.profile_supports_tools,
            "--profile-supports-tools",
        ),
        "provider_request_timeout_seconds": (
            args.profile_request_timeout_seconds,
            "--profile-request-timeout-seconds",
        ),
    }
    parsed = {
        field: _configuration_assignment_map(values, label=label)
        for field, (values, label) in raw_fields.items()
    }
    touched = {profile_id for values in parsed.values() for profile_id in values}
    known = {profile.id for profile in profiles}
    if unknown := touched - known:
        raise ValueError(
            "runtime profile fields reference unknown profiles: "
            + ", ".join(sorted(unknown))
        )

    for profile_id in sorted(touched):
        profile = next(item for item in profiles if item.id == profile_id)
        current = profile.runtime_profile
        values: dict[str, object] = current.model_dump(mode="json")
        if "api" in parsed and profile_id in parsed["api"]:
            values["api"] = ModelApi(parsed["api"][profile_id]).value
        if profile_id in parsed["endpoint_kind"]:
            values["endpoint_kind"] = ModelEndpointKind(
                parsed["endpoint_kind"][profile_id]
            ).value
        if profile_id in parsed["base_url"]:
            values["base_url"] = parsed["base_url"][profile_id]
        if profile_id in parsed["native_model_id"]:
            values["native_model_id"] = parsed["native_model_id"][profile_id]
        if profile_id in parsed["context_window_tokens"]:
            raw_context = parsed["context_window_tokens"][profile_id]
            if not raw_context.isdecimal() or int(raw_context) < 1:
                raise ValueError(
                    "--profile-context-window-tokens requires a positive integer"
                )
            values["context_window_tokens"] = int(raw_context)
        if profile_id in parsed["max_output_tokens"]:
            raw_maximum = parsed["max_output_tokens"][profile_id]
            if not raw_maximum.isdecimal() or int(raw_maximum) < 1:
                raise ValueError(
                    "--profile-max-output-tokens requires a positive integer"
                )
            values["max_output_tokens"] = int(raw_maximum)
        if profile_id in parsed["provider_request_timeout_seconds"]:
            raw_timeout = parsed["provider_request_timeout_seconds"][profile_id]
            if not raw_timeout.isdecimal() or int(raw_timeout) < 1:
                raise ValueError(
                    "--profile-request-timeout-seconds requires a positive integer"
                )
            values["provider_request_timeout_seconds"] = int(raw_timeout)
        if profile_id in parsed["input_modalities"]:
            modalities = tuple(
                item.strip()
                for item in parsed["input_modalities"][profile_id].split(",")
                if item.strip()
            )
            if not modalities:
                raise ValueError(
                    "--profile-input-modalities requires at least one modality"
                )
            values["input_modalities"] = modalities
        if profile_id in parsed["supports_tools"]:
            values["supports_tools"] = _configuration_bool(
                parsed["supports_tools"][profile_id],
                label="--profile-supports-tools",
            )

        endpoint_kind = ModelEndpointKind(values["endpoint_kind"])
        if endpoint_kind is ModelEndpointKind.OPENCLAW_NATIVE:
            raise ValueError(
                "runtime profile flags configure remote or local endpoints; "
                "omit them for an OpenClaw-native route"
            )
        values["source"] = ModelRuntimeProfileSource.USER_SUPPLIED.value
        if profile_id in parsed["credential_env"]:
            values["credential_source"] = CredentialSource.ENVIRONMENT.value
            values["credential_env"] = parsed["credential_env"][profile_id]
        elif endpoint_kind is ModelEndpointKind.LOCAL:
            values["credential_source"] = CredentialSource.NONE.value
            values["credential_env"] = None
        elif values.get("credential_source") != CredentialSource.ENVIRONMENT.value:
            raise ValueError(f"profile {profile_id} requires --profile-credential-env")
        if values.get("supports_tools") is not True:
            raise ValueError(
                f"profile {profile_id} must explicitly declare tool support"
            )
        if values.get("api") is None:
            raise ValueError(f"profile {profile_id} requires --profile-api")
        api = ModelApi(values["api"])
        values["artifact_submission_policy"] = (
            ArtifactSubmissionPolicy.OPENAI_REQUIRED.value
            if api
            in {
                ModelApi.OPENAI_COMPLETIONS,
                ModelApi.OPENAI_RESPONSES,
                ModelApi.AZURE_OPENAI_RESPONSES,
            }
            else ArtifactSubmissionPolicy.RUNTIME_DEFAULT.value
        )
        runtime_profile = ModelRuntimeProfile.model_validate(values)
        profile_updates: dict[str, object] = {
            "runtime_profile": runtime_profile.model_dump(mode="json")
        }
        if profile_id in parsed["context_window_tokens"]:
            profile_updates.update(
                context_window_tokens=runtime_profile.context_window_tokens,
                context_source=ModelMetadataSource.USER_SUPPLIED,
                context_observed_at=datetime.now(UTC),
            )
        _replace_profile(profiles, profile_id, **profile_updates)


def _configured_model_fields(
    *,
    current: UserConfiguration | None,
    model: str,
    input_cost: Decimal | None,
    output_cost: Decimal | None,
    args: argparse.Namespace,
) -> dict[str, object]:
    """Apply advanced profile flags without retaining duplicate scalar models."""

    preserve = current is not None and model == current.model
    if preserve:
        profiles = list(current.model_profiles)
        default_profile_id = current.default_model_profile_id
        routing_mode = current.routing_mode
        capability_overrides = dict(current.capability_profile_overrides)
        stage_overrides = dict(current.stage_profile_overrides)
        switch_conditions = current.authorized_switch_conditions
        _replace_profile(
            profiles,
            default_profile_id,
            input_cost_per_million_usd=input_cost,
            output_cost_per_million_usd=output_cost,
            pricing_source=(
                None
                if input_cost is None
                else ModelMetadataSource.CONFIRMED_ZERO
                if input_cost == 0 and output_cost == 0
                else ModelMetadataSource.USER_SUPPLIED
            ),
            pricing_observed_at=(None if input_cost is None else datetime.now(UTC)),
        )
    else:
        profiles = [
            ModelProfile(
                id="default",
                model=model,
                capabilities=tuple(AgentCapability),
                input_cost_per_million_usd=input_cost,
                output_cost_per_million_usd=output_cost,
                pricing_source=(
                    None
                    if input_cost is None
                    else ModelMetadataSource.CONFIRMED_ZERO
                    if input_cost == 0 and output_cost == 0
                    else ModelMetadataSource.USER_SUPPLIED
                ),
                pricing_observed_at=(None if input_cost is None else datetime.now(UTC)),
            )
        ]
        default_profile_id = "default"
        routing_mode = ModelRoutingMode.STRICT
        capability_overrides = {}
        stage_overrides = {}
        switch_conditions = ()

    if args.clear_model_routing:
        default = next(
            profile for profile in profiles if profile.id == default_profile_id
        )
        profiles = [
            ModelProfile.model_validate(
                {
                    **default.model_dump(mode="json"),
                    "capabilities": tuple(AgentCapability),
                }
            )
        ]
        capability_overrides = {}
        stage_overrides = {}
        switch_conditions = ()
        routing_mode = ModelRoutingMode.STRICT

    existing_ids = {profile.id for profile in profiles}
    for value in args.add_model_profile:
        profile_id, profile_model = _split_configuration_assignment(
            value,
            label="--add-model-profile",
        )
        if profile_id in existing_ids:
            raise ValueError(f"model profile already exists: {profile_id}")
        profiles.append(
            ModelProfile(
                id=profile_id,
                model=profile_model,
                capabilities=tuple(AgentCapability),
            )
        )
        existing_ids.add(profile_id)

    for profile_id in args.remove_model_profile:
        if profile_id == default_profile_id:
            raise ValueError("cannot remove the default model profile")
        if profile_id not in existing_ids:
            raise ValueError(f"unknown model profile: {profile_id}")
        profiles = [profile for profile in profiles if profile.id != profile_id]
        existing_ids.remove(profile_id)
        capability_overrides = {
            capability: selected
            for capability, selected in capability_overrides.items()
            if selected != profile_id
        }
        stage_overrides = {
            stage: selected
            for stage, selected in stage_overrides.items()
            if selected != profile_id
        }

    _apply_runtime_profile_arguments(profiles, args=args)

    for value in args.profile_capabilities:
        profile_id, raw_capabilities = _split_configuration_assignment(
            value,
            label="--profile-capabilities",
        )
        try:
            capabilities = tuple(
                AgentCapability(item.strip())
                for item in raw_capabilities.split(",")
                if item.strip()
            )
        except ValueError as error:
            raise ValueError(
                f"unknown Agent capability in --profile-capabilities: {value}"
            ) from error
        if not capabilities:
            raise ValueError("--profile-capabilities requires at least one capability")
        _replace_profile(profiles, profile_id, capabilities=capabilities)

    for value in args.profile_priority:
        profile_id, raw_priority = _split_configuration_assignment(
            value,
            label="--profile-priority",
        )
        if not raw_priority.isdigit():
            raise ValueError("--profile-priority requires a positive integer")
        _replace_profile(profiles, profile_id, priority=int(raw_priority))

    for value in args.profile_pricing:
        profile_id, raw_prices = _split_configuration_assignment(
            value,
            label="--profile-pricing",
        )
        parts = [part.strip() for part in raw_prices.split(",")]
        if len(parts) != 2:
            raise ValueError("--profile-pricing requires INPUT,OUTPUT")
        try:
            profile_input, profile_output = (Decimal(part) for part in parts)
        except Exception as error:
            raise ValueError("--profile-pricing requires decimal prices") from error
        _replace_profile(
            profiles,
            profile_id,
            input_cost_per_million_usd=profile_input,
            output_cost_per_million_usd=profile_output,
            pricing_source=(
                ModelMetadataSource.CONFIRMED_ZERO
                if profile_input == 0 and profile_output == 0
                else ModelMetadataSource.USER_SUPPLIED
            ),
            pricing_observed_at=datetime.now(UTC),
        )

    for value in args.profile_cache_pricing:
        profile_id, raw_prices = _split_configuration_assignment(
            value,
            label="--profile-cache-pricing",
        )
        parts = raw_prices.split(",")
        if len(parts) != 2:
            raise ValueError("--profile-cache-pricing requires READ,WRITE")
        try:
            read_price, write_price = (Decimal(part.strip()) for part in parts)
            cache = CachePricing(
                read_cost_per_million_usd=read_price,
                write_cost_per_million_usd=write_price,
                source=(
                    ModelMetadataSource.CONFIRMED_ZERO
                    if read_price == write_price == 0
                    else ModelMetadataSource.USER_SUPPLIED
                ),
                observed_at=datetime.now(UTC),
            )
        except (ValueError, ArithmeticError) as error:
            raise ValueError(
                "--profile-cache-pricing requires finite non-negative rates"
            ) from error
        _replace_profile(profiles, profile_id, cache_pricing=cache)

    if args.default_model_profile is not None:
        default_profile_id = args.default_model_profile
    if args.routing_mode is not None:
        routing_mode = ModelRoutingMode(args.routing_mode)

    for capability_name in args.clear_capability_route:
        capability_overrides.pop(AgentCapability(capability_name), None)
    for value in args.route_capability:
        capability_name, profile_id = _split_configuration_assignment(
            value,
            label="--route-capability",
        )
        capability_overrides[AgentCapability(capability_name)] = profile_id
    for stage_id in args.clear_stage_route:
        stage_overrides.pop(stage_id, None)
    for value in args.route_stage:
        stage_id, profile_id = _split_configuration_assignment(
            value,
            label="--route-stage",
        )
        stage_overrides[stage_id] = profile_id

    if args.allow_provider_switch:
        switch_conditions = (ModelSwitchCondition.PROVIDER_FAILURE,)
    elif args.disable_provider_switch:
        switch_conditions = ()

    return {
        "model_profiles": tuple(profiles),
        "default_model_profile_id": default_profile_id,
        "routing_mode": routing_mode,
        "capability_profile_overrides": capability_overrides,
        "stage_profile_overrides": stage_overrides,
        "authorized_switch_conditions": switch_conditions,
    }


def _configure(args: argparse.Namespace) -> int:
    """Create or replace non-secret user run defaults."""

    path = user_configuration_path()
    current = _load_user_configuration(path)
    supplied = (
        any(
            value is not None
            for value in (
                args.model,
                args.input_cost_per_million_usd,
                args.output_cost_per_million_usd,
                args.max_concurrency,
                args.progress_visibility,
                args.progress_display,
                args.progress_color,
            )
        )
        or any(
            (
                args.add_model_profile,
                args.remove_model_profile,
                args.profile_capabilities,
                args.profile_priority,
                args.profile_pricing,
                args.profile_cache_pricing,
                args.profile_api,
                args.profile_endpoint_kind,
                args.profile_base_url,
                args.profile_native_model_id,
                args.profile_credential_env,
                args.profile_context_window_tokens,
                args.profile_max_output_tokens,
                args.profile_input_modalities,
                args.profile_supports_tools,
                args.profile_request_timeout_seconds,
                args.route_capability,
                args.clear_capability_route,
                args.route_stage,
                args.clear_stage_route,
            )
        )
        or any(
            value is not None
            for value in (
                args.default_model_profile,
                args.routing_mode,
            )
        )
        or args.allow_provider_switch
        or args.disable_provider_switch
        or args.clear_model_routing
    )
    if args.show:
        if supplied:
            raise ValueError("--show cannot be combined with configuration values")
        if current is None:
            print(f"configuration: not configured ({path})")
            print("next: run sat in an interactive terminal")
        else:
            _print_configuration(current, path)
            print("next: enter a project parent directory and run sat")
        return 0

    interactive = not args.non_interactive and sys.stdin.isatty() and not supplied
    if not interactive and not supplied:
        raise ValueError(
            "interactive configuration requires a terminal; supply configuration "
            "flags or use --show"
        )
    state_paths = ProductStatePaths.below(user_state_root())
    ensure_product_state(state_paths)
    openclaw_config = state_paths.openclaw / "openclaw.json"
    _validate_optional_openclaw_configuration(openclaw_config)
    state_context = (
        _staged_openclaw_state(state_paths.openclaw)
        if interactive
        else nullcontext((state_paths.openclaw, openclaw_config))
    )
    provider_setup_changed = False
    with state_context as (candidate_state, candidate_config):
        if interactive:
            print("SAT uses its own isolated OpenClaw runtime and provider state.")
            print("Existing OpenClaw installations and configuration are never used.")
            provider_setup_changed = cli._prompt_yes_no(
                "Open SAT's isolated OpenClaw provider setup now?",
                default=not openclaw_config.is_file(),
            )
            if provider_setup_changed:
                cli._run_openclaw_configuration(
                    cli.DEFAULT_OPENCLAW_BINARY,
                    state_dir=candidate_state,
                    config_path=candidate_config,
                )
            discovered_model = cli._discover_openclaw_default_model(
                cli.DEFAULT_OPENCLAW_BINARY,
                state_dir=candidate_state,
                config_path=candidate_config,
            )
            if discovered_model is not None:
                print(f"OpenClaw default model detected: {discovered_model}")
            print("Press Enter to keep a value shown in brackets.")
            default_model = current.model if current is not None else discovered_model
            model = cli._prompt_value("OpenClaw model reference", default_model)
            same_model = current is not None and model == current.model
            input_cost = current.input_cost_per_million_usd if same_model else None
            output_cost = current.output_cost_per_million_usd if same_model else None
            concurrency = current.max_concurrency if current is not None else 2
            progress_visibility = (
                current.progress_visibility if current is not None else "standard"
            )
            progress_display = (
                current.progress_display if current is not None else "auto"
            )
            progress_color = current.progress_color if current is not None else "auto"
        else:
            model = (
                args.model
                if args.model is not None
                else current.model
                if current
                else None
            )
            price_flags = (
                args.input_cost_per_million_usd is not None,
                args.output_cost_per_million_usd is not None,
            )
            if price_flags[0] != price_flags[1]:
                raise ValueError(
                    "input and output price flags must be supplied together"
                )
            model_changed = (
                current is not None
                and args.model is not None
                and model != current.model
            )
            if all(price_flags):
                input_cost = args.input_cost_per_million_usd
                output_cost = args.output_cost_per_million_usd
            elif current is not None and not model_changed:
                input_cost = current.input_cost_per_million_usd
                output_cost = current.output_cost_per_million_usd
            else:
                input_cost = None
                output_cost = None
            concurrency = (
                args.max_concurrency
                if args.max_concurrency is not None
                else current.max_concurrency
                if current
                else 2
            )
            progress_visibility = (
                args.progress_visibility
                if args.progress_visibility is not None
                else current.progress_visibility
                if current
                else "standard"
            )
            progress_display = (
                args.progress_display
                if args.progress_display is not None
                else current.progress_display
                if current
                else "auto"
            )
            progress_color = (
                args.progress_color
                if args.progress_color is not None
                else current.progress_color
                if current
                else "auto"
            )
            if model is None:
                raise ValueError(
                    "first-time non-interactive configuration requires --model"
                )

        model_fields = _configured_model_fields(
            current=current,
            model=model,
            input_cost=input_cost,
            output_cost=output_cost,
            args=args,
        )
        configuration = UserConfiguration(
            **model_fields,
            max_concurrency=concurrency,
            progress_visibility=progress_visibility,
            progress_display=progress_display,
            progress_color=progress_color,
        )
        configuration = _bind_private_runtime_profiles(
            configuration,
            config_path=candidate_config,
            refresh_private_configuration=provider_setup_changed,
        )
        inspections = cli._inspect_model_configuration(
            configuration,
            state_dir=candidate_state,
            config_path=candidate_config,
        )
        configuration = (
            _complete_model_metadata(
                configuration,
                inspections,
                offer_price_change=True,
            )
            if interactive
            else _merge_discovered_model_metadata(configuration, inspections)
        )
        _commit_configuration_transaction(
            configuration,
            user_path=path,
            live_openclaw_state=state_paths.openclaw,
            staged_openclaw_state=(candidate_state if provider_setup_changed else None),
        )
    print("configuration saved: local routes validated; live check not run")
    _print_configuration(configuration, path)
    print("provider credentials: not stored by SAT")
    print("next: enter a project parent directory and run sat")
    return 0


def _run_openclaw_configuration(
    openclaw_binary: Path,
    *,
    state_dir: Path,
    config_path: Path,
) -> None:
    """Delegate credential entry to OpenClaw's trusted interactive boundary."""

    try:
        result = subprocess.run(
            [str(openclaw_binary), "configure", "--section", "model"],
            check=False,
            env={
                **os.environ,
                **isolated_openclaw_environment(
                    state_dir=state_dir,
                    config_path=config_path,
                ),
            },
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeConfigurationError(
            "OpenClaw provider configuration could not start"
        ) from error
    if result.returncode != 0:
        raise RuntimeConfigurationError(
            "OpenClaw provider configuration did not complete successfully"
        )


@contextmanager
def _staged_openclaw_state(state_dir: Path) -> Iterator[tuple[Path, Path]]:
    """Clone SAT's private OpenClaw state for a reversible configuration edit."""

    state_dir.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if state_dir.is_symlink() or (state_dir.exists() and not state_dir.is_dir()):
        raise RuntimeConfigurationError("SAT OpenClaw state must be a real directory")
    try:
        repair_legacy_openclaw_skill_workspaces(
            openclaw_state=state_dir,
            policy_path=DEFAULT_PRODUCT_POLICY,
        )
    except WorkspaceMountError as error:
        raise RuntimeConfigurationError(str(error)) from error
    links: dict[Path, Path] = {}
    if state_dir.exists():
        for entry in state_dir.rglob("*"):
            if entry.is_symlink():
                links[entry.relative_to(state_dir)] = _staged_npm_link_target(
                    entry, state_dir=state_dir
                )
            elif not entry.is_dir() and not entry.is_file():
                raise RuntimeConfigurationError(
                    "SAT OpenClaw state contains an unsupported filesystem entry: "
                    f"{display_text(str(entry))}"
                )
    preflight_provider_database_copy(state_dir)
    candidate = Path(
        tempfile.mkdtemp(
            prefix=f".{state_dir.name}.candidate-",
            dir=state_dir.parent,
        )
    )
    os.chmod(candidate, 0o700)
    preserve_candidate = False
    try:
        if state_dir.exists():
            shutil.copytree(state_dir, candidate, dirs_exist_ok=True, symlinks=True)
            for relative, target in links.items():
                staged_link = candidate / relative
                staged_link.unlink()
                relocated = (
                    candidate / target.relative_to(state_dir)
                    if target.is_relative_to(state_dir)
                    else target
                )
                staged_link.symlink_to(
                    os.path.relpath(relocated, staged_link.parent)
                    if target.is_relative_to(state_dir)
                    else relocated
                )
        cli.migrate_copied_provider_databases(
            candidate,
            runtime=PROJECT_ROOT / ".sat/openclaw",
            policy_path=DEFAULT_PRODUCT_POLICY,
            source_state=state_dir,
        )
        yield candidate, candidate / "openclaw.json"
    except StateMigrationCleanupError:
        preserve_candidate = True
        raise
    finally:
        if candidate.exists() and not preserve_candidate:
            shutil.rmtree(candidate)


def _staged_npm_link_target(entry: Path, *, state_dir: Path) -> Path:
    """Relocate internal npm links and bind host peers to the current private SDK.

    Copying links never copies their external targets. Authoritative config,
    credentials and sessions still reject links. A legacy SDK peer is accepted
    only inside npm node_modules, from a marked SAT runtime or a retired sibling
    of the active managed application. The old target is never followed.
    """

    relative = entry.relative_to(state_dir)
    raw_target = Path(os.readlink(entry))
    target = Path(
        os.path.normpath(
            raw_target if raw_target.is_absolute() else entry.parent / raw_target
        )
    )
    npm = state_dir / "npm"
    if relative.parts[0] == "npm" and "node_modules" in relative.parts:
        if target.is_relative_to(npm):
            return target
        if relative.parts[-2:] == ("node_modules", "openclaw"):
            old_runtime = next(
                (
                    p
                    for p in target.parents
                    if p.name == "openclaw" and p.parent.name == ".sat"
                ),
                None,
            )
            current_runtime = cli.DEFAULT_OPENCLAW_BINARY.parent.parent
            if (
                old_runtime is not None
                and _is_openclaw_host_peer(target, runtime=old_runtime)
                and is_owned_openclaw_runtime(current_runtime)
                and (
                    is_owned_openclaw_runtime(old_runtime)
                    or is_retired_managed_application(
                        current_runtime.parent.parent, old_runtime.parent.parent
                    )
                )
            ):
                host = current_runtime / "runtime/node_modules/openclaw"
                if host.is_dir() and not host.is_symlink() and host.resolve() == host:
                    return host
    raise RuntimeConfigurationError(
        "unsupported symbolic link in SAT OpenClaw state: "
        f"{display_text(str(entry))}. Configuration and credentials were not changed. "
        "Only npm-internal links and recognized SAT SDK host peers can be staged. "
        "Preserve this state and report the affected path before retrying setup."
    )


def _is_openclaw_host_peer(target: Path, *, runtime: Path) -> bool:
    parts = target.relative_to(runtime).parts
    return parts == ("runtime", "node_modules", "openclaw") or (
        len(parts) == 5
        and parts[0] == "tools"
        and re.fullmatch(r"node-v[0-9]+\.[0-9]+\.[0-9]+", parts[1]) is not None
        and parts[2:] == ("lib", "node_modules", "openclaw")
    )


def _replace_exact_private_file(
    path: Path,
    content: bytes,
    *,
    mode: int,
) -> None:
    """Atomically replace one private file with caller-supplied exact bytes."""

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, raw_temporary = tempfile.mkstemp(
        prefix=f".{path.name}.restore-",
        dir=path.parent,
    )
    temporary = Path(raw_temporary)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _commit_configuration_transaction(
    configuration: UserConfiguration,
    *,
    user_path: Path,
    live_openclaw_state: Path,
    staged_openclaw_state: Path | None,
) -> None:
    """Commit validated SAT and optional OpenClaw state as one rollback unit."""

    previous_exists = user_path.is_file()
    previous_content = user_path.read_bytes() if previous_exists else None
    previous_mode = user_path.stat().st_mode & 0o777 if previous_exists else None
    backup_root: Path | None = None
    backup: Path | None = None
    user_save_started = False
    try:
        if staged_openclaw_state is not None:
            # Reserve recovery storage before changing either authority. Keep the
            # original directory at a distinct child so an empty reservation can
            # never be mistaken for the previous provider state during rollback.
            backup_root = Path(
                tempfile.mkdtemp(
                    prefix=f".{live_openclaw_state.name}.rollback-",
                    dir=live_openclaw_state.parent,
                )
            )
            backup = backup_root / "original"
        user_save_started = True
        cli.save_user_configuration(configuration, user_path)
        if staged_openclaw_state is not None:
            assert backup is not None
            if live_openclaw_state.exists():
                os.replace(live_openclaw_state, backup)
            os.replace(staged_openclaw_state, live_openclaw_state)
    except BaseException as commit_error:
        rollback_errors: list[BaseException] = []
        if staged_openclaw_state is not None and backup is not None:
            try:
                # A signal may arrive after rename has taken effect but before
                # Python updates a flag. The owned paths establish which moves
                # completed, including activation of a first-time provider state.
                if not staged_openclaw_state.exists() and live_openclaw_state.exists():
                    os.replace(live_openclaw_state, staged_openclaw_state)
                if backup.exists():
                    os.replace(backup, live_openclaw_state)
            except BaseException as error:  # pragma: no cover - recovery failure
                rollback_errors.append(error)
        if user_save_started:
            try:
                _restore_user_configuration(
                    user_path,
                    existed=previous_exists,
                    content=previous_content,
                    mode=previous_mode,
                )
            except BaseException as error:  # pragma: no cover - recovery failure
                rollback_errors.append(error)
        if rollback_errors:
            recovery_detail = (
                f"; recovery storage was preserved at {backup_root}"
                if backup_root is not None
                else ""
            )
            raise RuntimeConfigurationError(
                "configuration commit failed and rollback was incomplete"
                + recovery_detail
            ) from rollback_errors[0]
        if backup_root is not None:
            try:
                backup_root.rmdir()
            except OSError as error:  # recovered state remains authoritative
                commit_error.add_note(f"empty recovery directory remains: {error}")
        raise
    else:
        if backup_root is not None:
            try:
                shutil.rmtree(backup_root)
            except OSError as error:  # committed state remains authoritative
                warnings.warn(
                    f"validated configuration committed, but its private rollback "
                    f"copy could not be removed: {error}",
                    RuntimeWarning,
                    stacklevel=2,
                )


def _restore_user_configuration(
    path: Path,
    *,
    existed: bool,
    content: bytes | None,
    mode: int | None,
) -> None:
    """Restore the exact pre-transaction user configuration."""

    if existed:
        assert content is not None and mode is not None
        _replace_exact_private_file(path, content, mode=mode)
    else:
        path.unlink(missing_ok=True)
