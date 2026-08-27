"""Bounded extraction of attributable tool evidence from pinned OpenClaw sessions."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import stat
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from software_agent_team.artifacts import (
    AgentToolCallEvidence,
    AgentToolCallOutcome,
)

_MAX_INDEX_BYTES = 4 * 1024 * 1024
_MAX_SESSION_BYTES = 16 * 1024 * 1024
_MAX_SESSION_RECORDS = 4096
_MAX_RECORD_BYTES = 1024 * 1024
_MAX_TOOL_OUTPUT_BYTES = 1024 * 1024
_MAX_TOOL_CALLS = 999
_OUTPUT_EXCERPT_CHARACTERS = 4096
_TRUNCATION_MARKER = "\n... controller excerpt truncated ...\n"


class OpenClawSessionEvidenceError(ValueError):
    """Raised when session provenance or tool-call pairing is not trustworthy."""


@dataclass(frozen=True)
class CapturedOpenClawToolEvidence:
    """Sanitized current-invocation evidence ready for telemetry persistence."""

    transcript_sha256: str
    record_count: int
    tool_calls: tuple[AgentToolCallEvidence, ...]


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_regular_file(path: Path, *, limit: int, label: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise OpenClawSessionEvidenceError(f"cannot open {label} safely") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OpenClawSessionEvidenceError(f"{label} is not a regular file")
        if metadata.st_size > limit:
            raise OpenClawSessionEvidenceError(f"{label} exceeds its size limit")
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > limit:
            raise OpenClawSessionEvidenceError(f"{label} exceeds its size limit")
        return payload
    finally:
        os.close(descriptor)


def _require_safe_session_directory(state_dir: Path, agent_id: str) -> Path:
    if not state_dir.is_absolute():
        raise OpenClawSessionEvidenceError("OpenClaw state directory must be absolute")
    current = state_dir
    for part in ("agents", agent_id, "sessions"):
        try:
            metadata = current.lstat()
        except OSError as error:
            raise OpenClawSessionEvidenceError(
                "OpenClaw session directory is unavailable"
            ) from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise OpenClawSessionEvidenceError(
                "OpenClaw session directory is not a direct directory boundary"
            )
        current /= part
    try:
        metadata = current.lstat()
    except OSError as error:
        raise OpenClawSessionEvidenceError(
            "OpenClaw session directory is unavailable"
        ) from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise OpenClawSessionEvidenceError(
            "OpenClaw session directory is not a direct directory boundary"
        )
    return current


def _load_json_object(payload: bytes, *, label: str) -> dict[str, object]:
    try:
        decoded = payload.decode("utf-8", errors="strict")
        value = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise OpenClawSessionEvidenceError(
            f"{label} is not valid UTF-8 JSON"
        ) from error
    if not isinstance(value, dict):
        raise OpenClawSessionEvidenceError(f"{label} must be a JSON object")
    return value


def _load_session_records(payload: bytes) -> tuple[dict[str, object], ...]:
    if not payload or not payload.endswith(b"\n"):
        raise OpenClawSessionEvidenceError(
            "OpenClaw session transcript is empty or incomplete"
        )
    lines = payload.splitlines()
    if not lines or len(lines) > _MAX_SESSION_RECORDS:
        raise OpenClawSessionEvidenceError(
            "OpenClaw session transcript has an invalid record count"
        )
    records: list[dict[str, object]] = []
    for line in lines:
        if not line or len(line) > _MAX_RECORD_BYTES:
            raise OpenClawSessionEvidenceError(
                "OpenClaw session transcript contains an invalid record"
            )
        records.append(_load_json_object(line, label="OpenClaw session record"))
    return tuple(records)


def _message_text(content: object) -> str | None:
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


def _current_invocation_records(
    records: tuple[dict[str, object], ...],
    *,
    prompt: str,
) -> tuple[dict[str, object], ...]:
    matches: list[int] = []
    for index, record in enumerate(records):
        if record.get("type") != "message":
            continue
        message = record.get("message")
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        if _message_text(message.get("content")) == prompt:
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


def _canonical_arguments(arguments: object) -> bytes:
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


_ENVIRONMENT_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


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
    lexer = shlex.shlex(command, posix=True)
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
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise OpenClawSessionEvidenceError(f"OpenClaw {label} is invalid")
    if label == "tool duration" and value < 0:
        raise OpenClawSessionEvidenceError(f"OpenClaw {label} is invalid")
    return value


def _output_excerpt(output: str) -> str:
    safe = output.replace("\x00", "\ufffd")
    if len(safe) <= _OUTPUT_EXCERPT_CHARACTERS:
        return safe
    remaining = _OUTPUT_EXCERPT_CHARACTERS - len(_TRUNCATION_MARKER)
    head = remaining // 2
    tail = remaining - head
    return f"{safe[:head]}{_TRUNCATION_MARKER}{safe[-tail:]}"


def _extract_tool_calls(
    records: tuple[dict[str, object], ...],
) -> tuple[AgentToolCallEvidence, ...]:
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
        if reported_status is not None and reported_status not in (
            successful_statuses | failed_statuses
        ):
            raise OpenClawSessionEvidenceError("OpenClaw tool status is unknown")
        failed = (
            is_error or exit_code not in {None, 0} or reported_status in failed_statuses
        )
        output = _tool_result_output(result)
        output_bytes = output.encode("utf-8", errors="replace")
        if len(output_bytes) > _MAX_TOOL_OUTPUT_BYTES:
            raise OpenClawSessionEvidenceError(
                "OpenClaw tool result exceeds its evidence limit"
            )
        try:
            item = AgentToolCallEvidence(
                id=f"tool-{index:03d}",
                tool_name=tool_name,
                executable=executable,
                external_call_sha256=_sha256(external_id.encode("utf-8")),
                arguments_sha256=arguments_sha256,
                outcome=(
                    AgentToolCallOutcome.FAILED
                    if failed
                    else AgentToolCallOutcome.SUCCEEDED
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
            )
        except ValidationError as error:
            raise OpenClawSessionEvidenceError(
                "OpenClaw tool evidence violates the pinned schema"
            ) from error
        evidence.append(item)
    return tuple(evidence)


def capture_openclaw_tool_evidence(
    *,
    state_dir: Path,
    agent_id: str,
    session_key: str,
    session_id: str,
    prompt: str,
) -> CapturedOpenClawToolEvidence:
    """Capture only the exact current invocation from SAT's pinned session state."""

    if (
        not session_id
        or len(session_id) > 128
        or any(
            character
            not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
            for character in session_id
        )
    ):
        raise OpenClawSessionEvidenceError("OpenClaw session ID is unsafe")
    sessions = _require_safe_session_directory(state_dir, agent_id)
    index_payload = _read_regular_file(
        sessions / "sessions.json",
        limit=_MAX_INDEX_BYTES,
        label="OpenClaw session index",
    )
    index = _load_json_object(index_payload, label="OpenClaw session index")
    entry = index.get(session_key)
    if not isinstance(entry, dict) or entry.get("sessionId") != session_id:
        raise OpenClawSessionEvidenceError(
            "OpenClaw session index does not bind the invocation session"
        )
    expected_path = sessions / f"{session_id}.jsonl"
    session_file = entry.get("sessionFile")
    if session_file is not None and (
        not isinstance(session_file, str) or Path(session_file) != expected_path
    ):
        raise OpenClawSessionEvidenceError(
            "OpenClaw session index points outside the expected transcript"
        )
    transcript = _read_regular_file(
        expected_path,
        limit=_MAX_SESSION_BYTES,
        label="OpenClaw session transcript",
    )
    records = _load_session_records(transcript)
    first = records[0]
    if first.get("type") != "session" or first.get("id") != session_id:
        raise OpenClawSessionEvidenceError(
            "OpenClaw transcript identity differs from the invocation session"
        )
    invocation = _current_invocation_records(records, prompt=prompt)
    return CapturedOpenClawToolEvidence(
        transcript_sha256=_sha256(transcript),
        record_count=len(invocation),
        tool_calls=_extract_tool_calls(invocation),
    )
