"""Shared data types and activity data models for OpenClaw session evidence.

This module provides data models representing captured tool evidence, terminal
responses, initialization baselines, tool activity allow-lists, and transcript
bounds.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from enum import StrEnum

from software_agent_team.artifacts import (
    AgentToolCallEvidence,
    RuntimeToolRejection,
)
from software_agent_team.invocation_lifecycle import InitializationCheckpoint
from software_agent_team.terminal_presentation import (
    InvocationPresentation,
    TerminalToolDetail,
)

_MAX_INDEX_BYTES = 4 * 1024 * 1024
_MAX_SESSION_BYTES = 16 * 1024 * 1024
_MAX_SESSION_RECORDS = 4096
_MAX_SESSION_ROTATIONS = 8
_MAX_CHAIN_BYTES = 64 * 1024 * 1024
_MAX_CHAIN_RECORDS = 16384
_MAX_RECORD_BYTES = 1024 * 1024
_MAX_TOOL_OUTPUT_BYTES = 1024 * 1024
_MAX_TOOL_CALLS = 999
_MAX_SQLITE_BYTES = 2 * 1024 * 1024 * 1024
_MAX_ZSTD_WINDOW_BYTES = 8 * _MAX_RECORD_BYTES
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
class CapturedOpenClawTerminalDiagnostic:
    """Attributable identity and usage retained when terminal tools are invalid."""

    session_id: str
    provider: str | None
    model: str | None
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    reasoning_tokens: int | None
    total_tokens: int | None
    transcript_sha256: str
    record_count: int
    submission_tool_call_observed: bool


@dataclass(frozen=True)
class CapturedOpenClawInvocationDiagnostic:
    """Safe provenance for a fresh invocation without a terminal assistant."""

    session_id: str
    provider: str | None
    model: str | None
    transcript_sha256: str
    record_count: int
    submission_tool_call_observed: bool


@dataclass(frozen=True)
class _OpenClawTerminalContext:
    """Fresh terminal session records before success-only evidence validation."""

    session_id: str
    transcript_sha256: str
    invocation: tuple[dict[str, object], ...]
    execution_records: tuple[dict[str, object], ...]
    runtime_rejections: tuple[RuntimeToolRejection, ...]
    terminal_state: OpenClawInvocationTerminalState
    terminal_message: dict[str, object]


@dataclass(frozen=True)
class OpenClawToolActivity:
    """Content-free tool identity safe for live activity classification."""

    tool_name: str
    executable: str | None
    presentation: TerminalToolDetail | None = None


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


def _shell_command_prefix(command: str) -> str:
    """Skip only complete leading comments, preserving literal hashes in words."""
    prefix = command.lstrip(" \t\n")
    while prefix.startswith("#"):
        prefix = prefix.partition("\n")[2].lstrip(" \t\n")
    return prefix


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
    progress_record_count: int
    tool_started_count: int
    tool_completed_count: int
    active_tool_count: int
    repeating_no_progress_poll: bool
    terminal_response_observed: bool
    started_tools: tuple[OpenClawToolActivity, ...] = ()
    completed_tools: tuple[OpenClawToolActivity, ...] = ()
    presentation: InvocationPresentation | None = None


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


@dataclass(frozen=True)
class _BoundOpenClawTranscript:
    records: tuple[dict[str, object], ...]
    sha256: str
    complete: bool
