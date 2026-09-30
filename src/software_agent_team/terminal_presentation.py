"""Bounded, non-authoritative projections of attributable execution content."""

from __future__ import annotations

import os
import re
import unicodedata
from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict, Field

_SECRET_ASSIGNMENT = re.compile(
    r"(?i)((?:api[_-]?key|password|secret|access[_-]?token|authorization)"
    r'\s*["\']?\s*[:=]\s*["\']?)([^\s"\',;]+)'
)
_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]+|(?i:Bearer\s+)[A-Za-z0-9._-]+")
_REASONING = re.compile(
    r"<(think|thinking|analysis|reasoning)\b[^>]*>.*?(?:</\1\s*>|$)",
    re.IGNORECASE | re.DOTALL,
)


def display_text(value: str, *, limit: int = 600, tail: bool = False) -> str:
    """Redact credential-shaped text, remove reasoning tags, and bound output."""

    # Redact before truncation so a clipped credential cannot become a prefix leak.
    text = value[:65536]
    for name, secret in os.environ.items():
        if (
            any(word in name.upper() for word in ("KEY", "TOKEN", "PASSWORD", "SECRET"))
            and len(secret) >= 8
        ):
            text = text.replace(secret, "[redacted]")
    text = _SECRET_ASSIGNMENT.sub(r"\1[redacted]", text)
    text = _KEY.sub("[redacted]", text)
    text = _REASONING.sub("", text)
    text = "".join(
        character
        if character in "\n\t" or not unicodedata.category(character).startswith("C")
        else "?"
        for character in text
    ).strip()
    if len(text) <= limit:
        return text
    return "…" + text[-(limit - 1) :] if tail else text[: limit - 1] + "…"


class TerminalToolDetail(BaseModel):
    """A preview, never evidence that a command succeeded or a file was created."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    label: str = Field(max_length=180)
    arguments: str = Field(default="", max_length=600)
    result: str = Field(default="", max_length=600)
    completed: bool = False


class InvocationPresentation(BaseModel):
    """Ephemeral previews excluded from persisted lifecycle and event schemas."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    session_id: str | None = None
    context_input_tokens: int | None = Field(default=None, ge=0)
    model_text: str = Field(default="", max_length=1000)
    streaming: bool = False
    tool: TerminalToolDetail | None = None


def tool_preview(
    name: str, arguments: object, *, result: str = ""
) -> TerminalToolDetail:
    """Show useful targets without dumping submission payloads or file contents."""

    args = arguments if isinstance(arguments, dict) else {}
    text = ""
    label = (
        name
        if name
        in {
            "read",
            "write",
            "edit",
            "apply_patch",
            "exec",
            "process",
            "sat_submit_artifact",
        }
        else "tool"
    )
    if name == "exec" and isinstance(args.get("command"), str):
        text = args["command"]
        label = "command: " + " ".join(text.split())
    elif name in {"read", "write", "edit"}:
        path = args.get("path", args.get("file_path"))
        if isinstance(path, str):
            text = path
            label += " " + path
    elif name == "apply_patch" and isinstance(args.get("input"), str):
        targets = re.findall(
            r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", args["input"], re.MULTILINE
        )
        text = ", ".join(targets[:5])
        label += " " + text
    return TerminalToolDetail(
        label=display_text(label, limit=180),
        arguments=display_text(text),
        result=display_text(result)
        if name != "sat_submit_artifact"
        else "typed submission received",
    )


def visible_message(message: dict[str, object]) -> str:
    """Select only explicit text blocks, never thinking, arguments, or metadata."""

    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        item["text"]
        for item in content
        if isinstance(item, dict)
        and item.get("type") == "text"
        and isinstance(item.get("text"), str)
    )


def invocation_preview(
    records: Iterable[dict[str, object]], *, session_id: str | None, model: str | None
) -> InvocationPresentation:
    """Project the latest reported request size, never cumulative token usage."""

    tokens: int | None = None
    text = ""
    for record in records:
        if record.get("type") == "compaction":
            tokens = None
        message = record.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        reported = f"{message.get('provider')}/{message.get('model')}"
        if model is not None and reported != model:
            continue
        visible = visible_message(message)
        if visible:
            text = display_text(visible, limit=1000)
        usage = message.get("usage")
        if isinstance(usage, dict):
            values = [usage.get(key, 0) for key in ("input", "cacheRead", "cacheWrite")]
            tokens = (
                sum(values)
                if all(type(value) is int and value >= 0 for value in values)
                and "input" in usage
                else None
            )
    return InvocationPresentation(
        session_id=session_id, context_input_tokens=tokens, model_text=text
    )


def stream_preview(batch: bytes, *, session_id: str, previous: str = "") -> str:
    """Read only visible text events for this exact attributable runtime session."""

    import json

    text = previous
    for line in batch.splitlines():
        try:
            record = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if (
            not isinstance(record, dict)
            or record.get("sessionId") != session_id
            or record.get("event") != "assistant_text_stream"
        ):
            continue
        if record.get("evtType") not in {
            "text_start",
            "text_delta",
            "text_end",
            "commentary_update",
        }:
            continue
        content = record.get("content")
        delta = record.get("delta")
        if isinstance(content, str) and content:
            text = content[:65536]
        elif isinstance(delta, str):
            text = (text + delta)[:65536]
    return text
