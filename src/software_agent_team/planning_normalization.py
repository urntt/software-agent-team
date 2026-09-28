"""Pure, pre-schema presentation normalization for Planning responses."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from pydantic import BaseModel


def strip_redundant_stable_id_prefix(value: str, stable_id: str) -> str:
    """Remove only repeated copies of a requirement's stable-ID presentation."""

    return re.sub(rf"^(?:{re.escape(stable_id)}\s*:\s*)+", "", value)


def canonicalize_model_path(value: object) -> object:
    """Normalize only unambiguous safe relative-path presentation variants."""

    if not isinstance(value, str):
        return value
    cleaned = value.strip()
    path = PurePosixPath(cleaned)
    if (
        not cleaned
        or "\\" in cleaned
        or path.is_absolute()
        or path == PurePosixPath(".")
        or ".." in path.parts
    ):
        return value
    return str(path)


def normalize_response_envelope(
    payload: dict[str, object],
    *,
    body_types: tuple[tuple[str, type[BaseModel]], ...],
) -> tuple[dict[str, object], tuple[str, ...]]:
    """Frame a uniquely recognizable body and infer an unambiguous kind."""

    changes: list[str] = []
    if not {"kind", *(kind for kind, _ in body_types)}.intersection(payload):
        payload_fields = set(payload)
        bare_candidates: list[str] = []
        for response_kind, body_type in body_types:
            allowed_fields = set(body_type.model_fields)
            required_fields = {
                name
                for name, field in body_type.model_fields.items()
                if field.is_required()
            }
            if required_fields.issubset(payload_fields) and payload_fields.issubset(
                allowed_fields
            ):
                bare_candidates.append(response_kind)
        if len(bare_candidates) == 1:
            response_kind = bare_candidates[0]
            payload = {"kind": response_kind, response_kind: payload}
            changes.append(
                f"framed bare {response_kind} body as {response_kind} response"
            )
    if "kind" not in payload:
        candidates = tuple(
            kind for kind, _ in body_types if payload.get(kind) is not None
        )
        if len(candidates) == 1:
            payload["kind"] = candidates[0]
            changes.append(f"inferred response kind as {candidates[0]}")
    return payload, tuple(changes)
