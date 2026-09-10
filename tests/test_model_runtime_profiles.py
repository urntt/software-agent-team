"""Tests for transport-neutral, secret-free provider/model runtime profiles."""

import json
import os
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from software_agent_team.model_routing import (
    ModelProfile,
    ModelRoutingPolicy,
    resolve_model_route_plan,
)
from software_agent_team.model_runtime import (
    ArtifactSubmissionPolicy,
    CredentialSource,
    ModelApi,
    ModelEndpointKind,
    ModelRuntimeProfile,
    ModelRuntimeProfileSource,
    runtime_profile_for_model,
    runtime_profile_from_openclaw_configuration,
)
from software_agent_team.runtime_configuration import (
    materialize_model_check_configuration,
)
from software_agent_team.teams import AgentCapability

DEEPSEEK_FLASH_MODEL = "deepseek/deepseek-flash"
REPOSITORY_ROOT = Path(__file__).parents[1]


def custom_profile(api: ModelApi, *, local: bool = False) -> ModelProfile:
    endpoint_kind = ModelEndpointKind.LOCAL if local else ModelEndpointKind.REMOTE
    base_url = "http://127.0.0.1:11434" if local else "https://models.example/v1"
    return ModelProfile(
        id="default",
        model="custom/example-model",
        capabilities=tuple(AgentCapability),
        runtime_profile=ModelRuntimeProfile(
            source=ModelRuntimeProfileSource.USER_SUPPLIED,
            provider_id="custom",
            native_model_id="example-model",
            api=api,
            endpoint_kind=endpoint_kind,
            base_url=base_url,
            credential_source=(
                CredentialSource.NONE if local else CredentialSource.ENVIRONMENT
            ),
            credential_env=None if local else "CUSTOM_API_KEY",
            input_modalities=("text",),
            context_window_tokens=120_000,
            max_output_tokens=16_384,
            supports_tools=True,
            supports_streaming_usage=True,
        ),
    )


def test_deepseek_flash_is_a_complete_declarative_preset() -> None:
    profile = runtime_profile_for_model(DEEPSEEK_FLASH_MODEL)

    assert profile.source is ModelRuntimeProfileSource.SAT_PRESET
    assert profile.provider_id == "deepseek"
    assert profile.native_model_id == "deepseek-flash"
    assert profile.api is ModelApi.OPENAI_COMPLETIONS
    assert profile.endpoint_kind is ModelEndpointKind.REMOTE
    assert profile.base_url == "https://api.deepseek.com"
    assert profile.credential_source is CredentialSource.ENVIRONMENT
    assert profile.credential_env == "DEEPSEEK_API_KEY"
    assert profile.context_window_tokens == 120_000
    assert profile.supports_tools is True
    assert (
        profile.artifact_submission_policy is ArtifactSubmissionPolicy.OPENAI_REQUIRED
    )


@pytest.mark.parametrize(
    ("api", "local"),
    (
        (ModelApi.OPENAI_COMPLETIONS, False),
        (ModelApi.OPENAI_RESPONSES, False),
        (ModelApi.ANTHROPIC_MESSAGES, False),
        (ModelApi.OLLAMA, True),
    ),
)
def test_transport_profiles_compile_through_one_materializer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    api: ModelApi,
    local: bool,
) -> None:
    monkeypatch.setenv("CUSTOM_API_KEY", "must-not-be-persisted")
    profile = custom_profile(api, local=local)
    destination = tmp_path / f"{api.value}.json"

    materialize_model_check_configuration(destination, profile=profile)

    payload = json.loads(destination.read_text(encoding="utf-8"))
    provider = payload["models"]["providers"]["custom"]
    assert provider["api"] == api.value
    assert provider["baseUrl"] == profile.runtime_profile.base_url
    assert provider["models"][0]["id"] == "example-model"
    if local:
        assert "apiKey" not in provider
    else:
        assert provider["apiKey"] == "${CUSTOM_API_KEY}"
    assert "must-not-be-persisted" not in destination.read_text(encoding="utf-8")


def test_transport_matrix_validates_with_the_pinned_openclaw(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CUSTOM_API_KEY", "test-only")
    state = tmp_path / "state"
    state.mkdir()
    openclaw = REPOSITORY_ROOT / ".sat/openclaw/bin/openclaw"
    for api, local in (
        (ModelApi.OPENAI_COMPLETIONS, False),
        (ModelApi.OPENAI_RESPONSES, False),
        (ModelApi.ANTHROPIC_MESSAGES, False),
        (ModelApi.OLLAMA, True),
    ):
        destination = tmp_path / f"{api.value}.json"
        materialize_model_check_configuration(
            destination,
            profile=custom_profile(api, local=local),
        )
        result = subprocess.run(
            [str(openclaw), "config", "validate", "--json"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            env={
                **os.environ,
                "HOME": str(tmp_path),
                "OPENCLAW_STATE_DIR": str(state),
                "OPENCLAW_CONFIG_PATH": str(destination),
                "OPENCLAW_AGENT_DIR": "",
                "OPENCLAW_OAUTH_DIR": str(state / "credentials"),
            },
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["valid"] is True


def test_endpoint_policy_distinguishes_remote_and_local_services() -> None:
    with pytest.raises(ValidationError, match="remote model endpoints require HTTPS"):
        ModelRuntimeProfile(
            source=ModelRuntimeProfileSource.USER_SUPPLIED,
            provider_id="custom",
            native_model_id="model",
            api=ModelApi.OPENAI_RESPONSES,
            endpoint_kind=ModelEndpointKind.REMOTE,
            base_url="http://models.example/v1",
            credential_source=CredentialSource.OPENCLAW_AUTH,
        )

    with pytest.raises(ValidationError, match="remote model endpoints cannot"):
        ModelRuntimeProfile(
            source=ModelRuntimeProfileSource.USER_SUPPLIED,
            provider_id="ollama",
            native_model_id="model",
            api=ModelApi.OLLAMA,
            endpoint_kind=ModelEndpointKind.REMOTE,
            base_url="https://192.168.1.4:11434",
            credential_source=CredentialSource.ENVIRONMENT,
            credential_env="OLLAMA_API_KEY",
        )

    local = ModelRuntimeProfile(
        source=ModelRuntimeProfileSource.USER_SUPPLIED,
        provider_id="ollama",
        native_model_id="model",
        api=ModelApi.OLLAMA,
        endpoint_kind=ModelEndpointKind.LOCAL,
        base_url="http://192.168.1.4:11434",
        credential_source=CredentialSource.NONE,
    )
    assert local.endpoint_kind is ModelEndpointKind.LOCAL


def test_private_openclaw_provider_is_sanitized_into_one_runtime_profile() -> None:
    payload = {
        "models": {
            "providers": {
                "urntt": {
                    "baseUrl": "https://api.urntt.com/v1",
                    "api": "openai-responses",
                    "apiKey": "literal-secret-must-not-survive",
                    "timeoutSeconds": 75,
                    "models": [
                        {
                            "id": "qwen-runtime-id",
                            "name": "Qwen",
                            "reasoning": True,
                            "input": ["text"],
                            "contextWindow": 120_000,
                            "maxTokens": 16_384,
                            "compat": {
                                "supportsTools": True,
                                "supportsUsageInStreaming": True,
                            },
                        }
                    ],
                }
            }
        }
    }

    profile = runtime_profile_from_openclaw_configuration(
        "urntt/qwen-runtime-id",
        payload,
    )

    assert profile is not None
    assert profile.source is ModelRuntimeProfileSource.PRIVATE_OPENCLAW_CONFIG
    assert profile.api is ModelApi.OPENAI_RESPONSES
    assert profile.credential_source is CredentialSource.OPENCLAW_AUTH
    assert profile.credential_env is None
    assert profile.context_window_tokens == 120_000
    assert "literal-secret" not in profile.model_dump_json()


def test_route_resolution_freezes_the_same_runtime_profile_digest() -> None:
    profile = custom_profile(ModelApi.OPENAI_RESPONSES)
    policy = {
        "mode": "strict",
        "profiles": (profile,),
        "default_profile_id": "default",
    }
    agent = type(
        "Agent",
        (),
        {
            "id": "developer",
            "stage_id": "implement",
            "capability": AgentCapability.IMPLEMENTATION,
        },
    )()

    plan = resolve_model_route_plan(
        ModelRoutingPolicy.model_validate(policy),
        (agent,),
    )
    route = plan.get_route("default")

    assert route.runtime_profile == profile.runtime_profile
    assert route.runtime_profile_sha256 == profile.runtime_profile_sha256
