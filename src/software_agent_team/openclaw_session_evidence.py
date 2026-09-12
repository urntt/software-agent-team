"""Bounded extraction of attributable tool evidence from pinned OpenClaw sessions."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import stat
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pydantic import ValidationError

from software_agent_team.artifacts import (
    AgentSubmissionReceiptEvidence,
    AgentToolCallEvidence,
    AgentToolCallOutcome,
    RuntimeToolRejection,
)
from software_agent_team.invocation_lifecycle import InitializationCheckpoint
from software_agent_team.submissions import ARTIFACT_SUBMISSION_TOOL

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


class _OpenClawSessionFileMissing(OpenClawSessionEvidenceError):
    """Preserve an exact open-time missing result during initialization."""


class OpenClawInvocationTerminalState(StrEnum):
    """Last attributable message state for one completed OpenClaw invocation."""

    ASSISTANT_RESPONSE = "assistant_response"
    TOOL_RESULT = "tool_result"
    RUNTIME_REJECTION = "runtime_rejection"


@dataclass(frozen=True)
class CapturedOpenClawToolEvidence:
    """Sanitized current-invocation evidence ready for telemetry persistence."""

    transcript_sha256: str
    record_count: int
    tool_calls: tuple[AgentToolCallEvidence, ...]
    terminal_state: OpenClawInvocationTerminalState
    runtime_rejections: tuple[RuntimeToolRejection, ...] = ()


@dataclass(frozen=True)
class CapturedOpenClawTerminalResponse:
    """Terminal identity and current-invocation usage before wrapper finalization."""

    session_id: str
    provider: str | None
    model: str | None
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    reasoning_tokens: int | None
    total_tokens: int | None
    tool_evidence: CapturedOpenClawToolEvidence


@dataclass(frozen=True)
class OpenClawToolActivity:
    """Content-free tool identity safe for live activity classification."""

    tool_name: str
    executable: str | None


_ACTIVITY_TOOL_NAMES = {
    "apply_patch",
    "edit",
    "exec",
    "read",
    "sat_submit_artifact",
    "write",
}
_ACTIVITY_EXECUTABLES = {
    "cat",
    "eslint",
    "find",
    "git",
    "grep",
    "head",
    "ls",
    "make",
    "mypy",
    "npm",
    "pip",
    "pnpm",
    "pwd",
    "pyright",
    "pytest",
    "read",
    "readlink",
    "rg",
    "ruff",
    "sat-probe-run",
    "sat-probe-write",
    "sed",
    "stat",
    "tail",
    "uv",
    "yarn",
}


def _safe_activity_identity(
    tool_name: str,
    executable: str | None,
    arguments: object = None,
) -> OpenClawToolActivity:
    """Reduce runtime-owned identities to a bounded progress allow-list."""

    safe_name = tool_name if tool_name in _ACTIVITY_TOOL_NAMES else "other"
    safe_executable = None
    if tool_name == "exec" and executable == "cd":
        executable = _directory_wrapped_activity_executable(arguments)
    if executable is not None:
        basename = executable.rsplit("/", maxsplit=1)[-1]
        if basename in _ACTIVITY_EXECUTABLES:
            safe_executable = basename
    return OpenClawToolActivity(
        tool_name=safe_name,
        executable=safe_executable,
    )


def _directory_wrapped_activity_executable(arguments: object) -> str | None:
    """Classify one literal directory wrapper without changing direct evidence."""

    if not isinstance(arguments, dict) or not isinstance(arguments.get("command"), str):
        return None
    lexer = shlex.shlex(
        _shell_command_prefix(arguments["command"]), posix=False, punctuation_chars=True
    )
    # Preserve quoting so an argument spelled '&&' cannot become shell control.
    lexer.whitespace_split = True
    lexer.whitespace = " \t"
    lexer.commenters = ""
    try:
        if next(lexer) != "cd":
            return None
        directory = next(lexer)
        if directory == "--":
            directory = next(lexer)
        # Expansion, redirection and compound shell syntax are not a literal
        # directory wrapper. No path or argument ever leaves this projection.
        if (
            not directory
            or directory.startswith("-")
            or any(char in directory for char in "$`;&|()<>\n~*?[]{}")
        ):
            return None
        if next(lexer) != "&&":
            return None
        executable = next(lexer)
        if any(char in executable for char in "$`;&|()<>\n~*?[]{}"):
            return None
        return executable
    except (StopIteration, ValueError):
        return None


@dataclass(frozen=True)
class OpenClawSessionActivity:
    """Content-free liveness facts for the current OpenClaw invocation."""

    trusted_record_count: int
    tool_started_count: int
    tool_completed_count: int
    active_tool_count: int
    terminal_response_observed: bool
    started_tools: tuple[OpenClawToolActivity, ...] = ()
    completed_tools: tuple[OpenClawToolActivity, ...] = ()


@dataclass(frozen=True)
class OpenClawInitializationObservation:
    """Highest attributable checkpoint reached while OpenClaw starts one turn."""

    checkpoint: InitializationCheckpoint


@dataclass(frozen=True)
class OpenClawInitializationBaseline:
    """Content-free state that existed before one invocation was launched."""

    checkpoint: InitializationCheckpoint | None
    session_id: str | None
    matching_turn_count: int = 0


@dataclass(frozen=True)
class _OpenClawSessionSnapshot:
    observation: OpenClawInitializationObservation
    invocation_records: tuple[dict[str, object], ...] | None = None
    session_id: str | None = None
    matching_turn_count: int = 0
    transcript_sha256: str | None = None
    transcript_complete: bool = False


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_regular_file(path: Path, *, limit: int, label: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as error:
        raise _OpenClawSessionFileMissing(f"{label} is not published yet") from error
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


def _message_matches_prompt(content: object, prompt_sha256: str) -> bool:
    """Match an attributable turn by digest without retaining prompt text."""

    text = _message_text(content)
    return text is not None and _sha256(text.encode("utf-8")) == prompt_sha256


def _current_invocation_records(
    records: tuple[dict[str, object], ...],
    *,
    prompt: str,
) -> tuple[dict[str, object], ...]:
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


def _inspect_openclaw_session_snapshot(
    *,
    state_dir: Path,
    agent_id: str,
    session_key: str,
    prompt: str,
) -> _OpenClawSessionSnapshot | None:
    """Resolve one exact session boundary and its highest safe checkpoint."""

    candidate = state_dir / "agents" / agent_id / "sessions"
    try:
        candidate.lstat()
    except FileNotFoundError:
        return None
    sessions = _require_safe_session_directory(state_dir, agent_id)
    snapshot = _OpenClawSessionSnapshot(
        observation=OpenClawInitializationObservation(
            checkpoint=InitializationCheckpoint.SESSION_DIRECTORY
        )
    )
    try:
        index_payload = _read_regular_file(
            sessions / "sessions.json",
            limit=_MAX_INDEX_BYTES,
            label="OpenClaw session index",
        )
    except _OpenClawSessionFileMissing:
        return snapshot
    index = _load_json_object(index_payload, label="OpenClaw session index")
    snapshot = _OpenClawSessionSnapshot(
        observation=OpenClawInitializationObservation(
            checkpoint=InitializationCheckpoint.SESSION_INDEX
        )
    )
    entry = index.get(session_key)
    if entry is None:
        return snapshot
    if not isinstance(entry, dict):
        raise OpenClawSessionEvidenceError("OpenClaw session index entry is invalid")
    session_id = entry.get("sessionId")
    if (
        not isinstance(session_id, str)
        or not session_id
        or len(session_id) > 128
        or any(
            character
            not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
            for character in session_id
        )
    ):
        raise OpenClawSessionEvidenceError("OpenClaw session ID is unsafe")
    expected_path = sessions / f"{session_id}.jsonl"
    session_file = entry.get("sessionFile")
    if session_file is not None and (
        not isinstance(session_file, str) or Path(session_file) != expected_path
    ):
        raise OpenClawSessionEvidenceError(
            "OpenClaw session index points outside the expected transcript"
        )
    snapshot = _OpenClawSessionSnapshot(
        observation=OpenClawInitializationObservation(
            checkpoint=InitializationCheckpoint.SESSION_BOUND
        ),
        session_id=session_id,
    )
    try:
        transcript = _read_regular_file(
            expected_path,
            limit=_MAX_SESSION_BYTES,
            label="OpenClaw session transcript",
        )
    except _OpenClawSessionFileMissing:
        return snapshot
    if not transcript:
        return snapshot
    complete_lines = transcript.splitlines()
    if not transcript.endswith(b"\n"):
        complete_lines = complete_lines[:-1]
    if not complete_lines:
        return snapshot
    if len(complete_lines) > _MAX_SESSION_RECORDS:
        raise OpenClawSessionEvidenceError(
            "OpenClaw session transcript has an invalid record count"
        )
    records = tuple(
        _load_json_object(line, label="OpenClaw session record")
        for line in complete_lines
    )
    first = records[0]
    if first.get("type") != "session" or first.get("id") != session_id:
        raise OpenClawSessionEvidenceError(
            "OpenClaw transcript identity differs from the invocation session"
        )
    snapshot = _OpenClawSessionSnapshot(
        observation=OpenClawInitializationObservation(
            checkpoint=InitializationCheckpoint.TRANSCRIPT_HEADER
        ),
        session_id=session_id,
    )
    prompt_sha256 = _sha256(prompt.encode("utf-8"))
    matches = [
        index
        for index, record in enumerate(records)
        if record.get("type") == "message"
        and isinstance(record.get("message"), dict)
        and record["message"].get("role") == "user"
        and _message_matches_prompt(record["message"].get("content"), prompt_sha256)
    ]
    if not matches:
        return snapshot
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
    return _OpenClawSessionSnapshot(
        observation=OpenClawInitializationObservation(
            checkpoint=InitializationCheckpoint.CURRENT_TURN
        ),
        invocation_records=records[start:end],
        session_id=session_id,
        matching_turn_count=len(matches),
        transcript_sha256=_sha256(transcript),
        transcript_complete=transcript.endswith(b"\n"),
    )


def capture_openclaw_initialization_baseline(
    *,
    state_dir: Path,
    agent_id: str,
    session_key: str,
    prompt: str,
) -> OpenClawInitializationBaseline:
    """Capture state published before process launch without retaining content."""

    snapshot = _inspect_openclaw_session_snapshot(
        state_dir=state_dir,
        agent_id=agent_id,
        session_key=session_key,
        prompt=prompt,
    )
    if snapshot is None:
        return OpenClawInitializationBaseline(
            checkpoint=None,
            session_id=None,
        )
    return OpenClawInitializationBaseline(
        checkpoint=snapshot.observation.checkpoint,
        session_id=snapshot.session_id,
        matching_turn_count=snapshot.matching_turn_count,
    )


def _snapshot_is_new_for_invocation(
    snapshot: _OpenClawSessionSnapshot,
    baseline: OpenClawInitializationBaseline | None,
) -> bool:
    """Exclude inherited session state from launch-to-turn progress."""

    if baseline is None or baseline.checkpoint is None:
        return True
    checkpoint = snapshot.observation.checkpoint
    if checkpoint is InitializationCheckpoint.CURRENT_TURN:
        return (
            snapshot.session_id != baseline.session_id
            or snapshot.matching_turn_count > baseline.matching_turn_count
        )
    if (
        snapshot.session_id is not None
        and baseline.session_id is not None
        and snapshot.session_id != baseline.session_id
    ):
        return True
    order = list(InitializationCheckpoint)
    return order.index(checkpoint) > order.index(baseline.checkpoint)


def inspect_openclaw_initialization(
    *,
    state_dir: Path,
    agent_id: str,
    session_key: str,
    prompt: str,
    baseline: OpenClawInitializationBaseline | None = None,
) -> OpenClawInitializationObservation | None:
    """Return finite launch-to-turn progress without retaining session content."""

    snapshot = _inspect_openclaw_session_snapshot(
        state_dir=state_dir,
        agent_id=agent_id,
        session_key=session_key,
        prompt=prompt,
    )
    if snapshot is None or not _snapshot_is_new_for_invocation(snapshot, baseline):
        return None
    return snapshot.observation


def inspect_openclaw_session_activity(
    *,
    state_dir: Path,
    agent_id: str,
    session_key: str,
    prompt: str,
    baseline: OpenClawInitializationBaseline | None = None,
) -> OpenClawSessionActivity | None:
    """Inspect current-turn lifecycle records without retaining their content.

    ``None`` means the exact current turn is not ready. Malformed identities or
    records fail closed so callers never guess that a provider is active.
    """

    snapshot = _inspect_openclaw_session_snapshot(
        state_dir=state_dir,
        agent_id=agent_id,
        session_key=session_key,
        prompt=prompt,
    )
    if (
        snapshot is None
        or not _snapshot_is_new_for_invocation(snapshot, baseline)
        or snapshot.observation.checkpoint is not InitializationCheckpoint.CURRENT_TURN
        or snapshot.invocation_records is None
    ):
        return None
    invocation, _ = _classify_runtime_rejections(snapshot.invocation_records)

    started: dict[str, OpenClawToolActivity] = {}
    started_names: dict[str, str] = {}
    completed: set[str] = set()
    completed_tools: list[OpenClawToolActivity] = []
    trusted_records = 0
    last_message_role: object = "user"
    last_assistant_has_tool_call = False
    for record in invocation[1:]:
        if record.get("type") != "message":
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            raise OpenClawSessionEvidenceError("OpenClaw message record is invalid")
        role = message.get("role")
        last_message_role = role
        if role == "assistant":
            trusted_records += 1
            content = message.get("content")
            # Sanitization can remove an unsupported call while retaining the
            # assistant's text. Its pinned stop reason still denotes tool use,
            # not a provider-final response.
            last_assistant_has_tool_call = message.get("stopReason") == "toolUse"
            if isinstance(content, str):
                continue
            if not isinstance(content, list):
                raise OpenClawSessionEvidenceError(
                    "OpenClaw assistant content is invalid"
                )
            for item in content:
                if not isinstance(item, dict) or item.get("type") != "toolCall":
                    continue
                last_assistant_has_tool_call = True
                external_id = item.get("id")
                tool_name = item.get("name")
                if (
                    not isinstance(external_id, str)
                    or not external_id
                    or not isinstance(tool_name, str)
                    or not tool_name
                    or tool_name.strip() != tool_name
                ):
                    raise OpenClawSessionEvidenceError(
                        "OpenClaw tool-call identity is invalid"
                    )
                if external_id in started:
                    raise OpenClawSessionEvidenceError(
                        "OpenClaw session repeats a tool-call identity"
                    )
                executable = _exec_executable(tool_name, item.get("arguments"))
                started_names[external_id] = tool_name
                started[external_id] = _safe_activity_identity(
                    tool_name,
                    executable,
                    item.get("arguments"),
                )
        elif role == "toolResult":
            trusted_records += 1
            external_id = message.get("toolCallId")
            if not isinstance(external_id, str) or external_id not in started:
                raise OpenClawSessionEvidenceError(
                    "OpenClaw tool result has no attributable call identity"
                )
            if external_id in completed:
                raise OpenClawSessionEvidenceError(
                    "OpenClaw session repeats a tool result"
                )
            activity = started[external_id]
            if message.get("toolName") != started_names[external_id]:
                raise OpenClawSessionEvidenceError(
                    "OpenClaw tool result names a different tool"
                )
            completed.add(external_id)
            completed_tools.append(activity)

    # Filtering a diagnostic must not make an earlier assistant message look
    # like the terminal response after a later runtime rejection.
    for raw_record in reversed(snapshot.invocation_records[1:]):
        if raw_record.get("type") == "message":
            raw_message = raw_record.get("message")
            if isinstance(raw_message, dict):
                last_message_role = raw_message.get("role")
            break
    return OpenClawSessionActivity(
        trusted_record_count=trusted_records,
        tool_started_count=len(started),
        tool_completed_count=len(completed),
        active_tool_count=len(set(started) - completed),
        terminal_response_observed=(
            last_message_role == "assistant"
            and not last_assistant_has_tool_call
            and not (set(started) - completed)
        ),
        started_tools=tuple(started.values()),
        completed_tools=tuple(completed_tools),
    )


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


def _shell_command_prefix(command: str) -> str:
    """Skip only complete leading comments, preserving literal hashes in words."""

    prefix = command.lstrip(" \t\n")
    while prefix.startswith("#"):
        prefix = prefix.partition("\n")[2].lstrip(" \t\n")
    return prefix


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
    # Shell comments before the command are not executable identity. Skip only
    # complete leading comment lines, not hashes inside words or quoted tokens;
    # shlex.commenters would incorrectly truncate those literal command names.
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
    """Accept only pinned OpenClaw shapes that identify live async work.

    A ``running`` result says that the tool returned an async process handle; it
    does not say that the underlying command succeeded. Raw handle values remain
    in the private transcript and are not copied into persisted evidence.
    """

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
    safe = output.replace("\x00", "\ufffd")
    if len(safe) <= _OUTPUT_EXCERPT_CHARACTERS:
        return safe
    remaining = _OUTPUT_EXCERPT_CHARACTERS - len(_TRUNCATION_MARKER)
    head = remaining // 2
    tail = remaining - head
    return f"{safe[:head]}{_TRUNCATION_MARKER}{safe[-tail:]}"


def _classify_runtime_rejections(
    records: tuple[dict[str, object], ...],
) -> tuple[tuple[dict[str, object], ...], tuple[RuntimeToolRejection, ...]]:
    """Separate the pinned runtime's orphaned not-found response from work.

    OpenClaw sanitizes unsupported assistant calls before persistence, but still
    persists its canned negative result. No arguments, execution, successful
    claim, or liveness authority can be reconstructed from that record. Keep a
    provenance-bound diagnostic instead; every other orphan remains invalid in
    the normal pairing validator. Both live and terminal readers use this owner.
    """

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
                if (
                    isinstance(identity, str)
                    and identity
                    and identity.strip() == identity
                    and len(identity) <= 512
                    and "\x00" not in identity
                    and identity not in calls
                    and isinstance(name, str)
                    and re.fullmatch(r"[A-Za-z0-9_:.-]{1,64}", name)
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
                            tool_name=name,
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
    execution_records, runtime_rejections = _classify_runtime_rejections(invocation)
    terminal_state = _invocation_terminal_state(invocation)
    last_message_index = max(
        index
        for index, record in enumerate(invocation)
        if record.get("type") == "message"
    )
    if any(item.record_index == last_message_index for item in runtime_rejections):
        terminal_state = OpenClawInvocationTerminalState.RUNTIME_REJECTION
    return CapturedOpenClawToolEvidence(
        transcript_sha256=_sha256(transcript),
        record_count=len(invocation),
        tool_calls=_extract_tool_calls(execution_records),
        terminal_state=terminal_state,
        runtime_rejections=runtime_rejections,
    )


def _optional_usage_identity(value: object, *, label: str) -> str | None:
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
    # Orphan runtime rejections mean the sanitizer removed an assistant call.
    # Its billable usage cannot be reconstructed from the remaining transcript.
    attributable = not runtime_rejections and provider is not None and model is not None
    for record in records:
        if record.get("type") == "compaction":
            # Compaction records lack billable usage and may survive a rotation
            # that removed assistant messages from this invocation's transcript.
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
            # Session messages are not the wrapper's normalized accumulator:
            # missing per-message counters cannot use its zero-elision rule.
            totals[field] = (
                None if value is None or previous is None else previous + value
            )
    return totals if attributable else dict.fromkeys(fields)


def capture_openclaw_terminal_response(
    *,
    state_dir: Path,
    agent_id: str,
    session_key: str,
    prompt: str,
    baseline: OpenClawInitializationBaseline | None,
) -> CapturedOpenClawTerminalResponse:
    """Recover one fresh terminal turn without trusting a wrapper result envelope."""

    snapshot = _inspect_openclaw_session_snapshot(
        state_dir=state_dir,
        agent_id=agent_id,
        session_key=session_key,
        prompt=prompt,
    )
    if (
        snapshot is None
        or not _snapshot_is_new_for_invocation(snapshot, baseline)
        or snapshot.observation.checkpoint is not InitializationCheckpoint.CURRENT_TURN
        or snapshot.invocation_records is None
        or snapshot.session_id is None
        or snapshot.transcript_sha256 is None
    ):
        raise OpenClawSessionEvidenceError(
            "OpenClaw terminal response is not attributable to this invocation"
        )
    if not snapshot.transcript_complete:
        raise OpenClawSessionEvidenceError("OpenClaw terminal transcript is incomplete")
    invocation = snapshot.invocation_records
    execution_records, runtime_rejections = _classify_runtime_rejections(invocation)
    terminal_state = _invocation_terminal_state(invocation)
    if terminal_state is not OpenClawInvocationTerminalState.ASSISTANT_RESPONSE:
        raise OpenClawSessionEvidenceError(
            "OpenClaw invocation has no terminal assistant response"
        )
    last_message_index = max(
        index
        for index, record in enumerate(invocation)
        if record.get("type") == "message"
    )
    if any(item.record_index == last_message_index for item in runtime_rejections):
        raise OpenClawSessionEvidenceError(
            "OpenClaw terminal response is a runtime rejection"
        )
    terminal_record = invocation[last_message_index]
    terminal_message = terminal_record.get("message")
    if not isinstance(terminal_message, dict) or terminal_message.get("role") != (
        "assistant"
    ):
        raise OpenClawSessionEvidenceError(
            "OpenClaw terminal response has an invalid message identity"
        )
    if terminal_message.get("stopReason") != "stop":
        raise OpenClawSessionEvidenceError(
            "OpenClaw terminal response did not finish with stop reason"
        )

    provider = _optional_usage_identity(
        terminal_message.get("provider"), label="provider"
    )
    model = _optional_usage_identity(terminal_message.get("model"), label="model")
    usage_buckets = _invocation_usage(
        invocation,
        provider=provider,
        model=model,
        runtime_rejections=runtime_rejections,
    )

    tool_evidence = CapturedOpenClawToolEvidence(
        transcript_sha256=snapshot.transcript_sha256,
        record_count=len(invocation),
        tool_calls=_extract_tool_calls(execution_records),
        terminal_state=terminal_state,
        runtime_rejections=runtime_rejections,
    )
    return CapturedOpenClawTerminalResponse(
        session_id=snapshot.session_id,
        provider=provider,
        model=model,
        input_tokens=usage_buckets["input"],
        output_tokens=usage_buckets["output"],
        cache_read_tokens=usage_buckets["cacheRead"],
        cache_write_tokens=usage_buckets["cacheWrite"],
        reasoning_tokens=usage_buckets["reasoningTokens"],
        total_tokens=usage_buckets["total"],
        tool_evidence=tool_evidence,
    )
