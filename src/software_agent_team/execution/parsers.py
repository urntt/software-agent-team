"""OpenClaw process stdout/stderr payload and failure parsers."""

from __future__ import annotations

import json
import re

from pydantic import BaseModel, ConfigDict, Field

from software_agent_team.artifacts import AgentToolCallOutcome
from software_agent_team.execution.models import (
    AgentTokenUsage,
)
from software_agent_team.openclaw_session_evidence import (
    CapturedOpenClawToolEvidence,
    OpenClawInvocationTerminalState,
)
from software_agent_team.submissions import (
    AgentSubmissionEvidence,
    AgentSubmissionStatus,
)


class _OpenClawResponse(BaseModel):
    """Normalized reply payloads and metadata from one OpenClaw response."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    visible_texts: tuple[str, ...]
    has_error_payload: bool = False
    historical_tool_warning: tuple[str, int, int] | None = None
    provider_failed: bool = False
    declared_timeout: bool = False
    openclaw_run_id: str | None = None
    session_id: str | None = None
    provider: str | None = None
    model: str | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    usage: AgentTokenUsage | None = None


def _optional_nonblank(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _optional_nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _parse_usage(value: object) -> AgentTokenUsage | None:
    if not isinstance(value, dict):
        return None
    fields = {
        "input_tokens": _optional_nonnegative_int(value.get("input")),
        "output_tokens": _optional_nonnegative_int(value.get("output")),
        "cache_read_tokens": _optional_nonnegative_int(value.get("cacheRead")),
        "cache_write_tokens": _optional_nonnegative_int(value.get("cacheWrite")),
        "reasoning_tokens": _optional_nonnegative_int(value.get("reasoningTokens")),
        "total_tokens": _optional_nonnegative_int(value.get("total")),
    }
    if all(item is None for item in fields.values()):
        return None
    # Pinned OpenClaw's normalized accumulator elides zero-valued buckets.
    # Only absent fields in a split-usage envelope have this meaning; explicit
    # null/malformed values and total-only envelopes are still unknown.
    buckets = (
        ("input", "input_tokens"),
        ("output", "output_tokens"),
        ("cacheRead", "cache_read_tokens"),
        ("cacheWrite", "cache_write_tokens"),
    )
    if any(fields[normalized] is not None for _, normalized in buckets):
        for raw, normalized in buckets:
            if raw not in value:
                fields[normalized] = 0
    return AgentTokenUsage(**fields)


def _canonical_model_reference(
    *,
    provider: str | None,
    model: str | None,
) -> str | None:
    """Normalize OpenClaw's split provider/model metadata for run comparison."""

    if model is None or provider is None or model.startswith(f"{provider}/"):
        return model
    return f"{provider}/{model}"


_OPENCLAW_TIMEOUT_PREFIX = "Request timed out before a response was generated."
_OPENCLAW_PROVIDER_FAILURE_TEXT = "LLM request failed."
_OPENCLAW_DIAGNOSTIC_PREFIXES = ("⚠️ 🛠️ Exec failed:",)
_OPENCLAW_PROVIDER_RESPONSE_PATTERN = re.compile(
    r"^\[provider-transport-fetch\] \[model-fetch\] response "
    r"provider=(?P<provider>[A-Za-z0-9._-]+) "
    r"api=[A-Za-z0-9._-]+ "
    r"model=(?P<model>[A-Za-z0-9._:/-]+) "
    r"status=(?P<status>[1-5][0-9]{2})(?:\s|$)"
)
_OPENCLAW_PROVIDER_ERROR_PATTERN = re.compile(
    r"^\[agent/embedded\] embedded run agent end: "
    r"runId=[A-Za-z0-9-]+ "
    r"isError=true "
    r"model=(?P<model>[A-Za-z0-9._:/-]+) "
    r"provider=(?P<provider>[A-Za-z0-9._-]+)(?:\s|$)"
)
_OPENCLAW_SGR_PATTERN = re.compile(r"\x1b\[[0-9;]*m")
_OPENCLAW_COMPACTION_FAILURE_PATTERN = re.compile(
    r"^\[agents/cli-compaction\] CLI transcript compaction failed for "
    r"(?P<model>[A-Za-z0-9._-]+/[A-Za-z0-9._:/-]+): Compaction timed out$"
)
_OPENCLAW_COMPACTION_ERROR_PATTERN = re.compile(
    r"^Error: CLI transcript compaction failed for "
    r"(?P<model>[A-Za-z0-9._-]+/[A-Za-z0-9._:/-]+): Compaction timed out$"
)
_OPENCLAW_STREAM_TERMINAL_PATTERN = re.compile(
    r"^\[agent/embedded\] embedded run agent end: runId=[A-Za-z0-9-]+ "
    r"isError=true model=(?P<model>[A-Za-z0-9._:/-]+) "
    r"provider=(?P<provider>[A-Za-z0-9._-]+) "
    r"error=LLM request timed out\. rawError=terminated$"
)
_OPENCLAW_STREAM_FALLBACK_PATTERN = re.compile(
    r"^\[model-fallback/decision\] model fallback decision: "
    r"decision=candidate_failed requested=(?P<requested>[A-Za-z0-9._:/-]+) "
    r"candidate=(?P<candidate>[A-Za-z0-9._:/-]+) "
    r"reason=timeout next=none detail=terminated$"
)


class _OpenClawProviderFailure(BaseModel):
    """Bounded provider diagnostic projected from pinned OpenClaw stderr."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(pattern=r"^[A-Za-z0-9._-]+$")
    model: str = Field(pattern=r"^[A-Za-z0-9._-]+/[A-Za-z0-9._:/-]+$")
    http_status: int = Field(ge=400, le=599)

    @property
    def error(self) -> str:
        """Return a content-free diagnostic safe for execution records."""

        return (
            "OpenClaw reported an attributable upstream model-provider failure "
            f"(HTTP {self.http_status})"
        )

    def response(self) -> _OpenClawResponse:
        """Project only the route identity needed by normal telemetry."""

        return _OpenClawResponse(
            visible_texts=(),
            provider_failed=True,
            provider=self.provider,
            model=self.model,
        )


class _OpenClawStreamFailure(BaseModel):
    """Exact terminal stream failure for one requested pinned-runtime route."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(pattern=r"^[A-Za-z0-9._-]+$")
    model: str = Field(pattern=r"^[A-Za-z0-9._-]+/[A-Za-z0-9._:/-]+$")

    @property
    def error(self) -> str:
        return "OpenClaw reported that the approved model stream terminated"

    def response(self) -> _OpenClawResponse:
        return _OpenClawResponse(
            visible_texts=(),
            provider_failed=True,
            provider=self.provider,
            model=self.model,
        )


def _parse_openclaw_stream_failure(
    stderr: str,
    *,
    requested_model: str | None,
) -> _OpenClawStreamFailure | None:
    """Require paired terminal and exhausted-route diagnostics, without raw text."""

    if requested_model is None or "/" not in requested_model:
        return None
    provider, model = requested_model.split("/", 1)
    terminal = False
    exhausted = False
    for line in stderr.splitlines():
        diagnostic = _OPENCLAW_SGR_PATTERN.sub("", line).strip()
        match = _OPENCLAW_STREAM_TERMINAL_PATTERN.fullmatch(diagnostic)
        if match is not None and (match.group("provider"), match.group("model")) == (
            provider,
            model,
        ):
            terminal = True
        match = _OPENCLAW_STREAM_FALLBACK_PATTERN.fullmatch(diagnostic)
        if match is not None and (
            match.group("requested") == requested_model
            and match.group("candidate") == requested_model
        ):
            exhausted = True
    return (
        _OpenClawStreamFailure(provider=provider, model=requested_model)
        if terminal and exhausted
        else None
    )


class _OpenClawCompactionFailure(BaseModel):
    """Bounded diagnosis emitted by pinned OpenClaw compaction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(pattern=r"^[A-Za-z0-9._-]+$")
    model: str = Field(pattern=r"^[A-Za-z0-9._-]+/[A-Za-z0-9._:/-]+$")

    @property
    def error(self) -> str:
        return "OpenClaw transcript compaction timed out"

    def response(self) -> _OpenClawResponse:
        return _OpenClawResponse(
            visible_texts=(),
            provider=self.provider,
            model=self.model,
        )


def _parse_openclaw_compaction_failure(
    stderr: str,
    *,
    requested_model: str | None,
) -> _OpenClawCompactionFailure | None:
    """Accept only the paired, exact timeout terminal for the requested route."""

    if requested_model is None:
        return None
    diagnostic = False
    terminal = False
    for line in stderr.splitlines():
        cleaned = line.strip()
        match = _OPENCLAW_COMPACTION_FAILURE_PATTERN.fullmatch(cleaned)
        if match is not None and match.group("model") == requested_model:
            diagnostic = True
            continue
        match = _OPENCLAW_COMPACTION_ERROR_PATTERN.fullmatch(cleaned)
        if match is not None and match.group("model") == requested_model:
            terminal = True
    if not diagnostic or not terminal:
        return None
    provider, _, _ = requested_model.partition("/")
    return _OpenClawCompactionFailure(provider=provider, model=requested_model)


def _parse_openclaw_provider_failure(
    stderr: str,
    *,
    requested_model: str | None,
) -> _OpenClawProviderFailure | None:
    """Classify terminal 402/429/5xx only when pinned diagnostics bind the route."""

    if requested_model is None:
        return None
    requested_provider, separator, requested_leaf = requested_model.partition("/")
    if not separator or not requested_provider or not requested_leaf:
        return None

    terminal_error_observed = False
    last_response_status: int | None = None
    for line in stderr.splitlines():
        # The pinned wrapper colors diagnostic prefixes on an interactive TTY.
        # Remove only SGR styling before matching the same strict line grammar.
        diagnostic = _OPENCLAW_SGR_PATTERN.sub("", line).strip()
        response_match = _OPENCLAW_PROVIDER_RESPONSE_PATTERN.match(diagnostic)
        if response_match is not None:
            provider = response_match.group("provider")
            model = response_match.group("model")
            if provider == requested_provider and model == requested_leaf:
                last_response_status = int(response_match.group("status"))
                terminal_error_observed = False
            continue
        error_match = _OPENCLAW_PROVIDER_ERROR_PATTERN.match(diagnostic)
        if error_match is not None:
            provider = error_match.group("provider")
            model = error_match.group("model")
            terminal_error_observed = terminal_error_observed or (
                last_response_status is not None
                and provider == requested_provider
                and model == requested_leaf
            )

    if not terminal_error_observed or last_response_status is None:
        return None
    status = last_response_status
    if status not in {402, 429} and status < 500:
        return None
    return _OpenClawProviderFailure(
        provider=requested_provider,
        model=requested_model,
        http_status=status,
    )


def _historical_tool_warning_summary(
    meta: dict[str, object], payloads: list[object]
) -> tuple[str, int, int] | None:
    """Recognize the pinned runner's warning, never a terminal error by text alone."""

    if meta.get("aborted") is not False or meta.get("error") is not None:
        return None
    if meta.get("stopReason") != "toolUse":
        return None
    completion = meta.get("completion")
    if not isinstance(completion, dict) or completion != {
        "stopReason": "toolUse",
        "finishReason": "toolUse",
    }:
        return None
    trace = meta.get("executionTrace")
    if not isinstance(trace, dict) or trace.get("fallbackUsed") is not False:
        return None
    attempts = trace.get("attempts")
    if not isinstance(attempts, list) or len(attempts) != 1:
        return None
    if not isinstance(attempts[0], dict) or (
        attempts[0].get("result") != "success"
        or attempts[0].get("stage") != "assistant"
    ):
        return None
    summary = meta.get("toolSummary")
    if not isinstance(summary, dict):
        return None
    unresolved = summary.get("unresolvedError")
    if not isinstance(unresolved, dict):
        return None
    name = unresolved.get("toolName")
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", name):
        return None
    # The pinned runner's defaultTitle capitalizes each underscore-separated
    # word. Do not restrict historical failures to a partial tool allowlist:
    # read-only tools such as ls/find/grep have the same warning semantics.
    # Non-default SDK display labels remain unrecognized and fail closed.
    label = " ".join(word[:1].upper() + word[1:] for word in name.split("_") if word)
    calls = _optional_nonnegative_int(summary.get("calls"))
    failures = _optional_nonnegative_int(summary.get("failures"))
    if calls is None or failures is None or not failures:
        return None
    # The pinned payload builder emits this warning only in the absence of a
    # user-facing reply. Additional or unrecognized error payloads fail closed.
    if len(payloads) != 1 or not isinstance(payloads[0], dict):
        return None
    warning = payloads[0]
    if warning.get("isError") is not True:
        return None
    text = warning.get("text")
    if not isinstance(text, str) or not re.match(
        rf"^⚠️ (?:{re.escape(label)}|\*\*{re.escape(label)}\*\*) "
        r"(?:failed|blocked|timed out)(?:[ (:.;]|$)",
        text,
    ):
        return None
    return name, calls, failures


def _attributable_historical_tool_warning(
    payload: _OpenClawResponse,
    evidence: CapturedOpenClawToolEvidence | None,
    submission: AgentSubmissionEvidence,
) -> bool:
    """Bind the SDK warning to failed tools strictly before the accepted receipt."""

    if payload.historical_tool_warning is None or evidence is None:
        return False
    if evidence.terminal_state is not OpenClawInvocationTerminalState.TOOL_RESULT:
        return False
    name, count, failures = payload.historical_tool_warning
    calls = evidence.tool_calls
    failed = tuple(call for call in calls if call.is_error)
    return (
        len(calls) == count
        and len(failed) == failures
        and failed[-1].tool_name == name
        and calls[-1].id == submission.tool_call_id
        and calls[-1].outcome is AgentToolCallOutcome.SUCCEEDED
        and calls[-1].submission_receipt is not None
        and submission.status is AgentSubmissionStatus.ACCEPTED
    )


def _parse_openclaw_payload(stdout: str) -> _OpenClawResponse:
    """Parse local or Gateway JSON emitted by ``openclaw agent --json``."""

    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise ValueError("OpenClaw stdout is not one JSON object") from error
    if not isinstance(envelope, dict):
        raise ValueError("OpenClaw response must be a JSON object")

    openclaw_run_id: str | None = None
    if "payloads" in envelope:
        result = envelope
    else:
        status = envelope.get("status")
        if status != "ok":
            raise ValueError(
                f"OpenClaw response did not complete successfully: {status!r}"
            )
        result = envelope.get("result")
        if not isinstance(result, dict):
            raise ValueError("OpenClaw response is missing result metadata")
        openclaw_run_id = _optional_nonblank(envelope.get("runId"))

    payloads = result.get("payloads")
    if not isinstance(payloads, list):
        raise ValueError("OpenClaw response is missing reply payloads")

    visible: list[str] = []
    has_error_payload = False
    saw_provider_failure = False
    for payload in payloads:
        if not isinstance(payload, dict):
            raise ValueError("OpenClaw reply payload must be an object")
        if payload.get("isError") is True:
            has_error_payload = True
        if payload.get("isReasoning") is True or payload.get("isCommentary") is True:
            continue
        text = payload.get("text")
        if isinstance(text, str) and text.strip():
            cleaned = text.strip()
            if cleaned == _OPENCLAW_PROVIDER_FAILURE_TEXT:
                saw_provider_failure = True
                continue
            if cleaned.startswith(_OPENCLAW_DIAGNOSTIC_PREFIXES):
                continue
            visible.append(cleaned)

    meta = result.get("meta")
    if meta is None:
        meta = {}
    if not isinstance(meta, dict):
        raise ValueError("OpenClaw result metadata must be an object")
    agent_meta = meta.get("agentMeta")
    if agent_meta is None:
        agent_meta = {}
    if not isinstance(agent_meta, dict):
        raise ValueError("OpenClaw Agent metadata must be an object")

    provider = _optional_nonblank(agent_meta.get("provider"))
    model = _optional_nonblank(agent_meta.get("model"))

    return _OpenClawResponse(
        visible_texts=tuple(visible),
        has_error_payload=has_error_payload,
        historical_tool_warning=_historical_tool_warning_summary(meta, payloads),
        provider_failed=saw_provider_failure and not visible,
        declared_timeout=any(
            text.startswith(_OPENCLAW_TIMEOUT_PREFIX) for text in visible
        ),
        openclaw_run_id=openclaw_run_id,
        session_id=_optional_nonblank(agent_meta.get("sessionId")),
        provider=provider,
        model=_canonical_model_reference(provider=provider, model=model),
        duration_ms=_optional_nonnegative_int(meta.get("durationMs")),
        usage=_parse_usage(agent_meta.get("usage")),
    )
