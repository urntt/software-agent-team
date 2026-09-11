"""Transport-neutral, secret-free provider/model runtime profiles."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MODEL_RUNTIME_PROFILE_SCHEMA_VERSION = 1


class ModelApi(StrEnum):
    """Provider transports accepted by the pinned OpenClaw configuration schema."""

    OPENAI_COMPLETIONS = "openai-completions"
    OPENAI_RESPONSES = "openai-responses"
    OPENAI_CHATGPT_RESPONSES = "openai-chatgpt-responses"
    ANTHROPIC_MESSAGES = "anthropic-messages"
    GOOGLE_GENERATIVE_AI = "google-generative-ai"
    GOOGLE_VERTEX = "google-vertex"
    GITHUB_COPILOT = "github-copilot"
    BEDROCK_CONVERSE_STREAM = "bedrock-converse-stream"
    OLLAMA = "ollama"
    AZURE_OPENAI_RESPONSES = "azure-openai-responses"


class ModelEndpointKind(StrEnum):
    """Network ownership class for one provider route."""

    OPENCLAW_NATIVE = "openclaw_native"
    REMOTE = "remote"
    LOCAL = "local"


class ModelRuntimeProfileSource(StrEnum):
    """Attributable origin for one runtime profile."""

    OPENCLAW_NATIVE = "openclaw_native"
    SAT_PRESET = "sat_preset"
    PRIVATE_OPENCLAW_CONFIG = "private_openclaw_config"
    USER_SUPPLIED = "user_supplied"


class CredentialSource(StrEnum):
    """Secret-free description of where OpenClaw resolves provider auth."""

    NONE = "none"
    OPENCLAW_AUTH = "openclaw_auth"
    ENVIRONMENT = "environment"


class ArtifactSubmissionPolicy(StrEnum):
    """Transport capability used to require an invocation-bound submission."""

    RUNTIME_DEFAULT = "runtime_default"
    OPENAI_REQUIRED = "openai_required"


class ModelRuntimeProfile(BaseModel):
    """One immutable provider transport contract without credential values."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[MODEL_RUNTIME_PROFILE_SCHEMA_VERSION] = (
        MODEL_RUNTIME_PROFILE_SCHEMA_VERSION
    )
    source: ModelRuntimeProfileSource
    provider_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]*$")
    native_model_id: str = Field(min_length=1, max_length=500)
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    api: ModelApi | None = None
    endpoint_kind: ModelEndpointKind
    base_url: str | None = Field(default=None, min_length=1, max_length=2000)
    credential_source: CredentialSource
    credential_env: str | None = Field(
        default=None,
        pattern=r"^[A-Z_][A-Z0-9_]*$",
    )
    provider_request_timeout_seconds: int | None = Field(default=None, ge=1)
    input_modalities: tuple[str, ...] = ()
    context_window_tokens: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    invocation_max_tokens: int | None = Field(default=None, ge=1)
    reasoning: bool | None = None
    disable_thinking: bool = False
    supports_tools: bool | None = None
    supports_streaming_usage: bool | None = None
    supports_reasoning_effort: bool | None = None
    artifact_submission_policy: ArtifactSubmissionPolicy = (
        ArtifactSubmissionPolicy.RUNTIME_DEFAULT
    )

    @field_validator("native_model_id", "display_name")
    @classmethod
    def require_clean_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned or any(
            character in cleaned for character in ("\x00", "\r", "\n")
        ):
            raise ValueError("model runtime text must be clean")
        return cleaned

    @field_validator("input_modalities")
    @classmethod
    def require_known_unique_modalities(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        cleaned = tuple(value.strip().casefold() for value in values)
        allowed = {"text", "image", "video", "audio"}
        if any(value not in allowed for value in cleaned):
            raise ValueError("model input modalities must use supported values")
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("model input modalities must be unique")
        return cleaned

    @model_validator(mode="after")
    def validate_transport_and_endpoint(self) -> Self:
        if self.endpoint_kind is ModelEndpointKind.OPENCLAW_NATIVE:
            if self.api is not None or self.base_url is not None:
                raise ValueError("native OpenClaw routes cannot override transport")
            if self.source is not ModelRuntimeProfileSource.OPENCLAW_NATIVE:
                raise ValueError("native OpenClaw routes require native source")
        else:
            if self.api is None or self.base_url is None:
                raise ValueError("configured model endpoints require API and base URL")
            parsed = urlsplit(self.base_url)
            if (
                not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("model endpoint URL is unsafe")
            if self.endpoint_kind is ModelEndpointKind.REMOTE:
                if parsed.scheme != "https":
                    raise ValueError("remote model endpoints require HTTPS")
                if _is_local_host(parsed.hostname):
                    raise ValueError("remote model endpoints cannot use a private host")
                if self.credential_source is CredentialSource.NONE:
                    raise ValueError(
                        "remote model endpoints require a credential source"
                    )
            else:
                if parsed.scheme not in {"http", "https"} or not _is_local_host(
                    parsed.hostname
                ):
                    raise ValueError(
                        "local model endpoints require an HTTP(S) local URL"
                    )
        if self.credential_source is CredentialSource.ENVIRONMENT:
            if self.credential_env is None:
                raise ValueError("environment credentials require an environment name")
        elif self.credential_env is not None:
            raise ValueError("credential environment belongs to environment auth")
        if (
            self.endpoint_kind is ModelEndpointKind.LOCAL
            and self.credential_source is CredentialSource.OPENCLAW_AUTH
        ):
            raise ValueError("local model endpoints need explicit or no credentials")
        if self.artifact_submission_policy is ArtifactSubmissionPolicy.OPENAI_REQUIRED:
            if self.api not in {
                ModelApi.OPENAI_COMPLETIONS,
                ModelApi.OPENAI_RESPONSES,
                ModelApi.AZURE_OPENAI_RESPONSES,
            }:
                raise ValueError(
                    "OpenAI required-tool policy needs an OpenAI-family transport"
                )
            if self.supports_tools is not True:
                raise ValueError("required artifact submission needs tool support")
        return self

    @property
    def sha256(self) -> str:
        """Return the canonical non-secret runtime-profile identity."""

        canonical = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(canonical).hexdigest()


def _is_local_host(host: str) -> bool:
    normalized = host.rstrip(".").casefold()
    if (
        normalized == "localhost"
        or normalized.endswith(".localhost")
        or normalized.endswith(".local")
        or normalized.endswith(".internal")
    ):
        return True
    try:
        address = ipaddress.ip_address(normalized)
        return (
            address.is_loopback
            or address.is_private
            or address.is_link_local
            or address.is_reserved
        )
    except ValueError:
        return False


def _native_runtime_profile(model: str) -> ModelRuntimeProfile:
    provider_id, separator, native_model_id = model.strip().partition("/")
    if not provider_id or not separator or not native_model_id:
        raise ValueError("model runtime profiles require a provider/model reference")
    return ModelRuntimeProfile(
        source=ModelRuntimeProfileSource.OPENCLAW_NATIVE,
        provider_id=provider_id,
        native_model_id=native_model_id,
        endpoint_kind=ModelEndpointKind.OPENCLAW_NATIVE,
        credential_source=CredentialSource.OPENCLAW_AUTH,
    )


_MODEL_RUNTIME_PRESETS: Mapping[str, ModelRuntimeProfile] = MappingProxyType(
    {
        "deepseek/deepseek-v4-flash-vision-exp": ModelRuntimeProfile(
            source=ModelRuntimeProfileSource.SAT_PRESET,
            provider_id="deepseek",
            native_model_id="deepseek-v4-flash-vision-exp",
            display_name="DeepSeek V4 Flash Vision Exp",
            api=ModelApi.OPENAI_COMPLETIONS,
            endpoint_kind=ModelEndpointKind.REMOTE,
            base_url="https://api.deepseek.com",
            credential_source=CredentialSource.ENVIRONMENT,
            credential_env="DEEPSEEK_API_KEY",
            input_modalities=("text", "image"),
            context_window_tokens=1_000_000,
            max_output_tokens=384_000,
            invocation_max_tokens=16_384,
            reasoning=True,
            disable_thinking=True,
            supports_tools=True,
            supports_streaming_usage=True,
            supports_reasoning_effort=True,
            artifact_submission_policy=ArtifactSubmissionPolicy.OPENAI_REQUIRED,
        ),
        "deepseek/deepseek-flash": ModelRuntimeProfile(
            source=ModelRuntimeProfileSource.SAT_PRESET,
            provider_id="deepseek",
            native_model_id="deepseek-flash",
            display_name="DeepSeek V4.1 Flash",
            api=ModelApi.OPENAI_COMPLETIONS,
            endpoint_kind=ModelEndpointKind.REMOTE,
            base_url="https://api.deepseek.com",
            credential_source=CredentialSource.ENVIRONMENT,
            credential_env="DEEPSEEK_API_KEY",
            input_modalities=("text",),
            context_window_tokens=120_000,
            max_output_tokens=16_384,
            invocation_max_tokens=16_384,
            reasoning=True,
            disable_thinking=True,
            supports_tools=True,
            supports_streaming_usage=True,
            supports_reasoning_effort=True,
            artifact_submission_policy=ArtifactSubmissionPolicy.OPENAI_REQUIRED,
        ),
    }
)


def has_runtime_profile_preset(model: str) -> bool:
    """Return whether SAT carries a reviewed supplement for one exact model."""

    return model.strip() in _MODEL_RUNTIME_PRESETS


def runtime_profile_for_model(model: str) -> ModelRuntimeProfile:
    """Resolve a reviewed preset or an explicit OpenClaw-native profile."""

    normalized = model.strip()
    preset = _MODEL_RUNTIME_PRESETS.get(normalized)
    return preset if preset is not None else _native_runtime_profile(normalized)


def runtime_profile_from_openclaw_configuration(
    model: str,
    payload: Mapping[str, Any],
) -> ModelRuntimeProfile | None:
    """Extract one sanitized configured provider without retaining secrets."""

    normalized = model.strip()
    provider_id, separator, visible_model_id = normalized.partition("/")
    if not provider_id or not separator or not visible_model_id:
        raise ValueError("model runtime profiles require a provider/model reference")
    models = payload.get("models")
    providers = models.get("providers") if isinstance(models, Mapping) else None
    provider = providers.get(provider_id) if isinstance(providers, Mapping) else None
    if not isinstance(provider, Mapping):
        return None
    configured_models = provider.get("models")
    entries = configured_models if isinstance(configured_models, list) else []
    matches = [
        entry
        for entry in entries
        if isinstance(entry, Mapping) and entry.get("id") == visible_model_id
    ]
    if len(matches) > 1:
        raise ValueError("private OpenClaw provider contains duplicate model IDs")
    model_entry: Mapping[str, Any] = matches[0] if matches else {}
    raw_base_url = model_entry.get("baseUrl", provider.get("baseUrl"))
    if not isinstance(raw_base_url, str) or not raw_base_url.strip():
        return None
    raw_api = model_entry.get("api", provider.get("api"))
    if raw_api is None:
        raw_api = "ollama" if provider_id == "ollama" else "openai-completions"
    api = ModelApi(raw_api)
    parsed = urlsplit(raw_base_url)
    endpoint_kind = (
        ModelEndpointKind.LOCAL
        if parsed.hostname is not None and _is_local_host(parsed.hostname)
        else ModelEndpointKind.REMOTE
    )
    raw_credential = provider.get("apiKey")
    credential_source = (
        CredentialSource.NONE
        if endpoint_kind is ModelEndpointKind.LOCAL and raw_credential is None
        else CredentialSource.OPENCLAW_AUTH
    )
    credential_env = _credential_environment_name(raw_credential)
    if credential_env is not None:
        credential_source = CredentialSource.ENVIRONMENT
    compat = model_entry.get("compat")
    compat = compat if isinstance(compat, Mapping) else {}
    raw_input = model_entry.get("input")
    input_modalities = (
        tuple(item for item in raw_input if isinstance(item, str))
        if isinstance(raw_input, list)
        else ()
    )
    return ModelRuntimeProfile(
        source=ModelRuntimeProfileSource.PRIVATE_OPENCLAW_CONFIG,
        provider_id=provider_id,
        native_model_id=(
            str(model_entry.get("id")) if model_entry.get("id") else visible_model_id
        ),
        display_name=(
            str(model_entry.get("name")) if model_entry.get("name") else None
        ),
        api=api,
        endpoint_kind=endpoint_kind,
        base_url=raw_base_url.strip(),
        credential_source=credential_source,
        credential_env=credential_env,
        provider_request_timeout_seconds=_positive_int(provider.get("timeoutSeconds")),
        input_modalities=input_modalities,
        context_window_tokens=_positive_int(model_entry.get("contextWindow")),
        max_output_tokens=_positive_int(model_entry.get("maxTokens")),
        reasoning=(
            model_entry.get("reasoning")
            if isinstance(model_entry.get("reasoning"), bool)
            else None
        ),
        supports_tools=_optional_bool(compat.get("supportsTools")),
        supports_streaming_usage=_optional_bool(compat.get("supportsUsageInStreaming")),
        supports_reasoning_effort=_optional_bool(compat.get("supportsReasoningEffort")),
    )


def _positive_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _optional_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _credential_environment_name(value: object) -> str | None:
    if isinstance(value, Mapping):
        if value.get("source") == "env" and isinstance(value.get("id"), str):
            candidate = value["id"]
            if re.fullmatch(r"[A-Z_][A-Z0-9_]*", candidate):
                return candidate
            return None
        return None
    if isinstance(value, str):
        match = re.fullmatch(r"\$\{([A-Z_][A-Z0-9_]*)\}", value.strip())
        return match.group(1) if match is not None else None
    return None


def openclaw_provider_payload(
    profile: ModelRuntimeProfile,
) -> dict[str, Any] | None:
    """Compile one configured provider entry for pinned OpenClaw."""

    if profile.endpoint_kind is ModelEndpointKind.OPENCLAW_NATIVE:
        return None
    assert profile.api is not None and profile.base_url is not None
    provider: dict[str, Any] = {
        "baseUrl": profile.base_url,
        "api": profile.api.value,
    }
    if profile.provider_request_timeout_seconds is not None:
        provider["timeoutSeconds"] = profile.provider_request_timeout_seconds
    if (
        profile.credential_source is CredentialSource.ENVIRONMENT
        and profile.credential_env is not None
    ):
        provider["apiKey"] = f"${{{profile.credential_env}}}"
    model: dict[str, Any] = {
        "id": profile.native_model_id,
        "name": profile.display_name or profile.native_model_id,
    }
    if profile.reasoning is not None:
        model["reasoning"] = profile.reasoning
    if profile.input_modalities:
        model["input"] = list(profile.input_modalities)
    if profile.context_window_tokens is not None:
        model["contextWindow"] = profile.context_window_tokens
    if profile.max_output_tokens is not None:
        model["maxTokens"] = profile.max_output_tokens
    compat: dict[str, bool] = {}
    for key, value in (
        ("supportsTools", profile.supports_tools),
        ("supportsUsageInStreaming", profile.supports_streaming_usage),
        ("supportsReasoningEffort", profile.supports_reasoning_effort),
    ):
        if value is not None:
            compat[key] = value
    if compat:
        model["compat"] = compat
    provider["models"] = [model]
    return provider


def openclaw_agent_model_settings(
    profile: ModelRuntimeProfile,
    *,
    artifact_submission_mode: Literal["single_tool", "tool_loop"] | None,
    artifact_tool_name: str,
) -> dict[str, Any] | None:
    """Compile model request settings from explicit transport capabilities."""

    params: dict[str, Any] = {}
    if profile.invocation_max_tokens is not None:
        params["maxTokens"] = profile.invocation_max_tokens
    extra_body: dict[str, Any] = {}
    if profile.disable_thinking:
        extra_body["thinking"] = {"type": "disabled"}
    if (
        artifact_submission_mode == "single_tool"
        and profile.artifact_submission_policy
        is ArtifactSubmissionPolicy.OPENAI_REQUIRED
    ):
        # A bootstrap turn has no work tool to choose: its only semantic action
        # is the bound submission, so forcing that exact function is safe.
        # Dynamic Agents need a real completion choice after optional work or
        # evidence tools. Forcing *some* tool on every turn can erase that
        # completion signal and sustain an unproductive tool loop. Their prompt
        # requests the terminal submission and the Controller still rejects a
        # missing, malformed, duplicate, or post-submission call.
        extra_body["tool_choice"] = {
            "type": "function",
            "function": {"name": artifact_tool_name},
        }
    if extra_body:
        params["extra_body"] = extra_body
    return {"params": params} if params else None
