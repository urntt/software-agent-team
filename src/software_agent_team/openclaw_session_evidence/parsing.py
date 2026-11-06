"""Parsing and extraction of messages, tool calls, and usage from session records.

This module provides parsing functions for attributable prompt turns, tool invocations,
tool results (including deferred background processes), runtime rejection diagnostics,
and assistant token usage.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex

from pydantic import ValidationError

from software_agent_team.artifacts import (
    AgentSubmissionReceiptEvidence,
    AgentToolCallEvidence,
    AgentToolCallOutcome,
    RuntimeToolRejection,
)
from software_agent_team.submissions import ARTIFACT_SUBMISSION_TOOL

from .reader import _sha256
from .types import (
    _ACTIVITY_EXECUTABLES,
    _ACTIVITY_TOOL_NAMES,
    _MAX_TOOL_CALLS,
    _MAX_TOOL_OUTPUT_BYTES,
    _OUTPUT_EXCERPT_CHARACTERS,
    _TRUNCATION_MARKER,
    OpenClawInvocationTerminalState,
    OpenClawSessionEvidenceError,
    _shell_command_prefix,
)

_ENVIRONMENT_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _message_text(content: object) -> str | None:
    """Extract plain text segments from message content."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "text":
            continue
        text = item.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts) if parts else None


def _message_matches_prompt(content: object, prompt_sha256: str) -> bool:
    """Match an attributable turn by digest without retaining prompt text."""
    text = _message_text(content)
    return text is not None and _sha256(text.encode("utf-8")) == prompt_sha256


def _current_invocation_records(
    records: tuple[dict[str, object], ...],
    *,
    prompt: str,
) -> tuple[dict[str, object], ...]:
    """Slice transcript records belonging to the latest prompt turn."""
    prompt_sha256 = _sha256(prompt.encode("utf-8"))
    matches: list[int] = []
    for index, record in enumerate(records):
        if record.get("type") != "message":
            continue
        message = record.get("message")
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        if _message_matches_prompt(message.get("content"), prompt_sha256):
            matches.append(index)
    if not matches:
        raise OpenClawSessionEvidenceError(
            "current Agent prompt is absent from the OpenClaw session"
        )
    start = matches[-1]
    end = len(records)
    for index in range(start + 1, len(records)):
        record = records[index]
        message = record.get("message")
        if (
            record.get("type") == "message"
            and isinstance(message, dict)
            and message.get("role") == "user"
        ):
            end = index
            break
    invocation = records[start:end]
    if len(invocation) < 2:
        raise OpenClawSessionEvidenceError(
            "current OpenClaw invocation has no completed response record"
        )
    return invocation


def _invocation_terminal_state(
    records: tuple[dict[str, object], ...],
) -> OpenClawInvocationTerminalState:
    """Classify the final attributable message without retaining its content."""
    for record in reversed(records[1:]):
        if record.get("type") != "message":
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            raise OpenClawSessionEvidenceError("OpenClaw message record is invalid")
        role = message.get("role")
        if role == "toolResult":
            return OpenClawInvocationTerminalState.TOOL_RESULT
        if role == "assistant":
            if message.get("stopReason") == "toolUse":
                raise OpenClawSessionEvidenceError(
                    "OpenClaw invocation ends with an incomplete tool call"
                )
            content = message.get("content")
            if isinstance(content, str):
                return OpenClawInvocationTerminalState.ASSISTANT_RESPONSE
            if not isinstance(content, list):
                raise OpenClawSessionEvidenceError(
                    "OpenClaw assistant content is invalid"
                )
            if any(
                isinstance(item, dict) and item.get("type") == "toolCall"
                for item in content
            ):
                raise OpenClawSessionEvidenceError(
                    "OpenClaw invocation ends with an incomplete tool call"
                )
            return OpenClawInvocationTerminalState.ASSISTANT_RESPONSE
        raise OpenClawSessionEvidenceError(
            "OpenClaw invocation ends with an unsupported message role"
        )
    raise OpenClawSessionEvidenceError(
        "current OpenClaw invocation has no completed response record"
    )


def _process_poll_identity(tool_name: str, arguments: object) -> str | None:
    """Return an opaque identity for one background-process observation."""
    if tool_name != "process" or not isinstance(arguments, dict):
        return None
    action = arguments.get("action")
    session_id = arguments.get("sessionId")
    if action not in {"poll", "log"} or not isinstance(session_id, str):
        return None
    if not session_id or session_id.strip() != session_id:
        return None
    return hashlib.sha256(f"{action}\0{session_id}".encode()).hexdigest()


def _running_process_poll_outcome(message: dict[str, object]) -> str | None:
    """Hash progress-bearing fields for a running process without retaining output."""
    details = message.get("details")
    if not isinstance(details, dict) or details.get("status") != "running":
        return None
    progress_fields = {
        key: details[key]
        for key in (
            "aggregated",
            "tail",
            "totalLines",
            "totalChars",
            "truncated",
        )
        if key in details
    }
    try:
        canonical = json.dumps(
            {
                "content": message.get("content"),
                "status": details["status"],
                "progress": progress_fields,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as error:
        raise OpenClawSessionEvidenceError(
            "OpenClaw process poll progress is not canonical JSON"
        ) from error
    return hashlib.sha256(canonical).hexdigest()


def _canonical_arguments(arguments: object) -> bytes:
    """Produce deterministic UTF-8 JSON bytes for tool arguments."""
    if not isinstance(arguments, dict):
        raise OpenClawSessionEvidenceError("OpenClaw tool arguments must be an object")
    try:
        value = json.dumps(
            arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, RecursionError) as error:
        raise OpenClawSessionEvidenceError(
            "OpenClaw tool arguments are not canonical JSON"
        ) from error
    return value.encode("utf-8")


def _exec_executable(tool_name: str, arguments: object) -> str | None:
    """Extract only a safe executable token, never the complete command argv."""
    if tool_name != "exec":
        return None
    if not isinstance(arguments, dict):
        raise OpenClawSessionEvidenceError("OpenClaw exec arguments must be an object")
    command = arguments.get("command")
    if not isinstance(command, str) or not command.strip() or "\x00" in command:
        raise OpenClawSessionEvidenceError(
            "OpenClaw exec command is unavailable for attribution"
        )
    prefix = _shell_command_prefix(command)
    lexer = shlex.shlex(prefix, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        executable = next(
            token for token in lexer if _ENVIRONMENT_ASSIGNMENT.match(token) is None
        )
    except (StopIteration, ValueError) as error:
        raise OpenClawSessionEvidenceError(
            "OpenClaw exec command cannot be attributed safely"
        ) from error
    if executable.startswith("#") or len(executable) > 512 or "\x00" in executable:
        raise OpenClawSessionEvidenceError(
            "OpenClaw exec command cannot be attributed safely"
        )
    return executable


def _tool_result_output(message: dict[str, object]) -> str:
    """Extract raw aggregated or text content from a tool result message."""
    details = message.get("details")
    if details is not None and not isinstance(details, dict):
        raise OpenClawSessionEvidenceError("OpenClaw tool result details are invalid")
    if isinstance(details, dict):
        aggregated = details.get("aggregated")
        if isinstance(aggregated, str):
            return aggregated
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "text":
            continue
        text = item.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def _optional_result_integer(value: object, *, label: str) -> int | None:
    """Validate and return an optional integer value."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise OpenClawSessionEvidenceError(f"OpenClaw {label} is invalid")
    if label == "tool duration" and value < 0:
        raise OpenClawSessionEvidenceError(f"OpenClaw {label} is invalid")
    return value


def _require_clean_async_text(
    value: object,
    *,
    label: str,
    maximum_length: int,
) -> str:
    """Validate one bounded OpenClaw async-handle text field."""
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or "\x00" in value
        or len(value) > maximum_length
    ):
        raise OpenClawSessionEvidenceError(f"OpenClaw {label} is invalid")
    return value


def _validate_deferred_process_result(
    *,
    tool_name: str,
    details: dict[str, object],
    is_error: bool,
    exit_code: int | None,
) -> None:
    """Accept only pinned OpenClaw shapes that identify live async work."""
    if tool_name not in {"exec", "process"} or is_error or exit_code is not None:
        raise OpenClawSessionEvidenceError(
            "OpenClaw deferred tool result is not a valid async process handle"
        )
    _require_clean_async_text(
        details.get("sessionId"),
        label="async session identity",
        maximum_length=512,
    )
    if any(
        key in details
        for key in (
            "exitCode",
            "exitSignal",
            "exitReason",
            "timedOut",
            "noOutputTimedOut",
        )
    ):
        raise OpenClawSessionEvidenceError(
            "OpenClaw deferred tool result contains terminal process state"
        )
    if tool_name == "exec":
        started_at = _optional_result_integer(
            details.get("startedAt"),
            label="async process start time",
        )
        if started_at is None or started_at <= 0:
            raise OpenClawSessionEvidenceError(
                "OpenClaw async process start time is invalid"
            )
        pid = _optional_result_integer(
            details.get("pid"),
            label="async process PID",
        )
        if pid is not None and pid <= 0:
            raise OpenClawSessionEvidenceError("OpenClaw async process PID is invalid")
        _require_clean_async_text(
            details.get("cwd"),
            label="async process working directory",
            maximum_length=4096,
        )
        tail = details.get("tail")
        if not isinstance(tail, str) or "\x00" in tail:
            raise OpenClawSessionEvidenceError(
                "OpenClaw async process output tail is invalid"
            )
    else:
        _require_clean_async_text(
            details.get("name"),
            label="async process name",
            maximum_length=512,
        )


def _output_excerpt(output: str) -> str:
    """Generate a bounded excerpt from output replacing NUL characters."""
    safe = output.replace("\x00", "\ufffd")
    if len(safe) <= _OUTPUT_EXCERPT_CHARACTERS:
        return safe
    remaining = _OUTPUT_EXCERPT_CHARACTERS - len(_TRUNCATION_MARKER)
    head = remaining // 2
    tail = remaining - head
    return f"{safe[:head]}{_TRUNCATION_MARKER}{safe[-tail:]}"


def _runtime_rejection_tool_name(value: object) -> str | None:
    """Project one exact rejected runtime name without retaining its arguments."""
    if not isinstance(value, str):
        return None
    if re.fullmatch(r"[A-Za-z0-9_:.-]{1,64}", value):
        return value
    if (
        not value
        or value.strip() != value
        or len(value) > 512
        or any(ord(character) < 32 or ord(character) > 126 for character in value)
    ):
        return None
    command, separator, remainder = value.partition(" ")
    if (
        separator != " "
        or not remainder.strip()
        or command not in _ACTIVITY_TOOL_NAMES | _ACTIVITY_EXECUTABLES
    ):
        return None
    return command


def _classify_runtime_rejections(
    records: tuple[dict[str, object], ...],
) -> tuple[tuple[dict[str, object], ...], tuple[RuntimeToolRejection, ...]]:
    """Separate the pinned runtime's orphaned not-found response from work."""
    calls: set[str] = set()
    rejected: set[str] = set()
    terminal_submission_seen = False
    filtered: list[dict[str, object]] = []
    diagnostics: list[RuntimeToolRejection] = []
    for index, record in enumerate(records):
        message = record.get("message") if record.get("type") == "message" else None
        if isinstance(message, dict):
            if message.get("role") == "assistant":
                content = message.get("content")
                for item in content if isinstance(content, list) else []:
                    if not isinstance(item, dict) or item.get("type") != "toolCall":
                        continue
                    identity = item.get("id")
                    if isinstance(identity, str):
                        if identity in rejected:
                            raise OpenClawSessionEvidenceError(
                                "OpenClaw call reuses a runtime rejection identity"
                            )
                        calls.add(identity)
                        if item.get("name") == "sat_submit_artifact":
                            terminal_submission_seen = True
            elif message.get("role") == "toolResult":
                identity = message.get("toolCallId")
                if isinstance(identity, str) and identity in rejected:
                    raise OpenClawSessionEvidenceError(
                        "OpenClaw session repeats a runtime rejection identity"
                    )
                name = message.get("toolName")
                projected_name = _runtime_rejection_tool_name(name)
                if (
                    isinstance(identity, str)
                    and identity
                    and identity.strip() == identity
                    and len(identity) <= 512
                    and "\x00" not in identity
                    and identity not in calls
                    and isinstance(name, str)
                    and projected_name is not None
                    and message.get("isError") is True
                    and message.get("details") == {}
                    and message.get("content")
                    == [{"type": "text", "text": f"Tool {name} not found"}]
                ):
                    if terminal_submission_seen:
                        raise OpenClawSessionEvidenceError(
                            "OpenClaw runtime rejection follows terminal submission"
                        )
                    rejected.add(identity)
                    diagnostics.append(
                        RuntimeToolRejection(
                            record_index=index,
                            tool_name=projected_name,
                            external_call_sha256=_sha256(identity.encode("utf-8")),
                            record_sha256=_sha256(_canonical_arguments(record)),
                        )
                    )
                    continue
        filtered.append(record)
    return tuple(filtered), tuple(diagnostics)


def _extract_tool_calls(
    records: tuple[dict[str, object], ...],
) -> tuple[AgentToolCallEvidence, ...]:
    """Pair attributable assistant tool calls with toolResult records into evidence."""
    calls: list[tuple[str, str, str | None, str]] = []
    seen_calls: set[str] = set()
    results: dict[str, dict[str, object]] = {}
    for record in records:
        if record.get("type") != "message":
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            raise OpenClawSessionEvidenceError("OpenClaw message record is invalid")
        role = message.get("role")
        if role == "assistant":
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for item in content:
                if not isinstance(item, dict) or item.get("type") != "toolCall":
                    continue
                external_id = item.get("id")
                tool_name = item.get("name")
                if (
                    not isinstance(external_id, str)
                    or not external_id
                    or external_id.strip() != external_id
                    or len(external_id) > 512
                    or not isinstance(tool_name, str)
                    or not tool_name
                    or tool_name.strip() != tool_name
                ):
                    raise OpenClawSessionEvidenceError(
                        "OpenClaw tool-call identity is invalid"
                    )
                if external_id in seen_calls:
                    raise OpenClawSessionEvidenceError(
                        "OpenClaw session repeats a tool-call identity"
                    )
                if len(calls) >= _MAX_TOOL_CALLS:
                    raise OpenClawSessionEvidenceError(
                        "OpenClaw invocation exceeds the tool-call evidence limit"
                    )
                seen_calls.add(external_id)
                raw_arguments = item.get("arguments")
                arguments = _canonical_arguments(raw_arguments)
                calls.append(
                    (
                        external_id,
                        tool_name,
                        _exec_executable(tool_name, raw_arguments),
                        _sha256(arguments),
                    )
                )
        elif role == "toolResult":
            external_id = message.get("toolCallId")
            if not isinstance(external_id, str) or not external_id:
                raise OpenClawSessionEvidenceError(
                    "OpenClaw tool result has no call identity"
                )
            if external_id not in seen_calls:
                raise OpenClawSessionEvidenceError(
                    "OpenClaw tool result appears before its tool call"
                )
            if external_id in results:
                raise OpenClawSessionEvidenceError(
                    "OpenClaw session repeats a tool result"
                )
            results[external_id] = message

    call_ids = {item[0] for item in calls}
    if set(results) != call_ids:
        raise OpenClawSessionEvidenceError(
            "OpenClaw tool calls and results do not pair exactly"
        )

    evidence: list[AgentToolCallEvidence] = []
    for index, (
        external_id,
        tool_name,
        executable,
        arguments_sha256,
    ) in enumerate(calls, start=1):
        result = results[external_id]
        if result.get("toolName") != tool_name:
            raise OpenClawSessionEvidenceError(
                "OpenClaw tool result names a different tool"
            )
        is_error = result.get("isError")
        if not isinstance(is_error, bool):
            raise OpenClawSessionEvidenceError(
                "OpenClaw tool result omits its error state"
            )
        details = result.get("details")
        if details is None:
            details = {}
        if not isinstance(details, dict):
            raise OpenClawSessionEvidenceError(
                "OpenClaw tool result details are invalid"
            )
        exit_code = _optional_result_integer(
            details.get("exitCode"),
            label="tool exit code",
        )
        duration_ms = _optional_result_integer(
            details.get("durationMs"),
            label="tool duration",
        )
        reported_status = details.get("status")
        if reported_status is not None and not isinstance(reported_status, str):
            raise OpenClawSessionEvidenceError("OpenClaw tool status is invalid")
        successful_statuses = {"completed", "ok", "success"}
        failed_statuses = {
            "cancelled",
            "error",
            "failed",
            "timed_out",
            "timeout",
        }
        deferred_statuses = {"running"}
        if reported_status is not None and reported_status not in (
            successful_statuses | failed_statuses | deferred_statuses
        ):
            raise OpenClawSessionEvidenceError("OpenClaw tool status is unknown")
        deferred = reported_status in deferred_statuses
        if deferred:
            _validate_deferred_process_result(
                tool_name=tool_name,
                details=details,
                is_error=is_error,
                exit_code=exit_code,
            )
        failed = (
            is_error or exit_code not in {None, 0} or reported_status in failed_statuses
        )
        output = _tool_result_output(result)
        output_bytes = output.encode("utf-8", errors="replace")
        if len(output_bytes) > _MAX_TOOL_OUTPUT_BYTES:
            raise OpenClawSessionEvidenceError(
                "OpenClaw tool result exceeds its evidence limit"
            )
        receipt_keys = {
            "submission_status",
            "protocol",
            "schema_sha256",
            "binding_sha256",
        }
        receipt_binding_keys = {
            "protocol",
            "schema_sha256",
            "binding_sha256",
        }
        receipt_fields_present = receipt_keys.intersection(details)
        has_bound_receipt = bool(receipt_binding_keys.intersection(details))
        if has_bound_receipt and tool_name != ARTIFACT_SUBMISSION_TOOL:
            raise OpenClawSessionEvidenceError(
                "non-submission tool result claims a submission receipt"
            )
        submission_receipt = None
        try:
            if has_bound_receipt:
                if receipt_fields_present != receipt_keys:
                    raise ValueError("submission receipt is incomplete")
                submission_receipt = AgentSubmissionReceiptEvidence(
                    submission_status=details.get("submission_status"),
                    protocol=details.get("protocol"),
                    schema_sha256=details.get("schema_sha256"),
                    binding_sha256=details.get("binding_sha256"),
                    external_call_sha256=_sha256(external_id.encode("utf-8")),
                )
            item = AgentToolCallEvidence(
                id=f"tool-{index:03d}",
                tool_name=tool_name,
                executable=executable,
                external_call_sha256=_sha256(external_id.encode("utf-8")),
                arguments_sha256=arguments_sha256,
                outcome=(
                    AgentToolCallOutcome.FAILED
                    if failed
                    else (
                        AgentToolCallOutcome.DEFERRED
                        if deferred
                        else AgentToolCallOutcome.SUCCEEDED
                    )
                ),
                is_error=is_error,
                reported_status=reported_status,
                exit_code=exit_code,
                duration_ms=duration_ms,
                output_sha256=_sha256(output_bytes),
                output_bytes=len(output_bytes),
                output_excerpt=_output_excerpt(
                    output_bytes.decode("utf-8", errors="strict")
                ),
                submission_receipt=submission_receipt,
            )
        except (ValueError, ValidationError) as error:
            raise OpenClawSessionEvidenceError(
                "OpenClaw tool evidence violates the pinned schema"
            ) from error
        evidence.append(item)
    return tuple(evidence)


def _optional_usage_identity(value: object, *, label: str) -> str | None:
    """Validate and clean optional provider/model identity."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 500:
        raise OpenClawSessionEvidenceError(f"OpenClaw usage {label} is invalid")
    return value.strip()


def _invocation_usage(
    records: tuple[dict[str, object], ...],
    *,
    provider: str | None,
    model: str | None,
    runtime_rejections: tuple[RuntimeToolRejection, ...],
) -> dict[str, int | None]:
    """Sum disjoint assistant counters only when their coverage is attributable."""
    fields = {
        "input": ("input",),
        "output": ("output",),
        "cacheRead": ("cacheRead",),
        "cacheWrite": ("cacheWrite",),
        "reasoningTokens": ("reasoningTokens",),
        "total": ("total", "totalTokens"),
    }
    totals: dict[str, int | None] = dict.fromkeys(fields, 0)
    attributable = not runtime_rejections and provider is not None and model is not None
    for record in records:
        if record.get("type") == "compaction":
            attributable = False
        message = record.get("message") if record.get("type") == "message" else None
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        reported_provider = _optional_usage_identity(
            message.get("provider"), label="provider"
        )
        reported_model = _optional_usage_identity(message.get("model"), label="model")
        if reported_provider is None or reported_model is None:
            attributable = False
        elif (
            provider is not None
            and model is not None
            and (
                reported_provider != provider
                or reported_model.removeprefix(f"{provider}/")
                != model.removeprefix(f"{provider}/")
            )
        ):
            raise OpenClawSessionEvidenceError(
                "OpenClaw invocation usage attribution differs between assistants"
            )
        raw_usage = message.get("usage")
        if raw_usage is not None and not isinstance(raw_usage, dict):
            raise OpenClawSessionEvidenceError("OpenClaw assistant usage is invalid")
        for field, aliases in fields.items():
            value = None
            if raw_usage is not None:
                for alias in aliases:
                    if alias in raw_usage:
                        value = raw_usage[alias]
                        break
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise OpenClawSessionEvidenceError(
                    f"OpenClaw assistant usage field {field} is invalid"
                )
            previous = totals[field]
            totals[field] = (
                None if value is None or previous is None else previous + value
            )
    return totals if attributable else dict.fromkeys(fields)
