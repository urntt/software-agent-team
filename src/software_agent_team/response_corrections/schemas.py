"""JSON schema inspection and resolution for response corrections."""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import JsonValue

from software_agent_team.response_corrections.diagnostics import _decode_pointer
from software_agent_team.response_corrections.types import (
    SemanticCorrectionPlan,
    SemanticCorrectionSubmission,
)


def _local_schema_ref(
    root: Mapping[str, JsonValue],
    reference: str,
) -> Mapping[str, JsonValue] | None:
    """Resolve one local JSON-Schema reference without external I/O."""

    if not reference.startswith("#/"):
        return None
    current: object = root
    try:
        parts = _decode_pointer(reference[1:])
    except ValueError:
        return None
    for part in parts:
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current if isinstance(current, Mapping) else None


def _schema_container_for_part(
    schema: Mapping[str, JsonValue],
    *,
    part: str,
    root: Mapping[str, JsonValue],
) -> Mapping[str, JsonValue] | None:
    """Select a local schema branch with one unambiguous path-segment contract."""

    current = schema
    visited: set[str] = set()
    while isinstance(reference := current.get("$ref"), str):
        if reference in visited:
            return None
        visited.add(reference)
        resolved = _local_schema_ref(root, reference)
        if resolved is None:
            return None
        current = resolved
    for keyword in ("anyOf", "oneOf", "allOf"):
        branches = current.get(keyword)
        if not isinstance(branches, list):
            continue
        matching: list[Mapping[str, JsonValue]] = []
        for branch in branches:
            if not isinstance(branch, Mapping):
                continue
            candidate = _schema_container_for_part(branch, part=part, root=root)
            if candidate is None:
                continue
            properties = candidate.get("properties")
            if (isinstance(properties, Mapping) and part in properties) or (
                part.isdecimal() and "items" in candidate
            ):
                matching.append(candidate)
        if len(matching) == 1:
            return matching[0]
        if len(matching) > 1:
            child_schemas: list[Mapping[str, JsonValue]] = []
            for candidate in matching:
                properties = candidate.get("properties")
                child = (
                    properties.get(part)
                    if isinstance(properties, Mapping) and part in properties
                    else candidate.get("items")
                )
                if not isinstance(child, Mapping):
                    return None
                child_schemas.append(child)
            expanded = [
                _expand_local_schema_refs(dict(child), root=root)
                for child in child_schemas
            ]
            if all(child == expanded[0] for child in expanded[1:]):
                return matching[0]
            return None
    return current


def _expand_local_schema_refs(
    value: JsonValue,
    *,
    root: Mapping[str, JsonValue],
    active_refs: frozenset[str] = frozenset(),
) -> JsonValue:
    """Make a bounded target schema self-contained for model-visible guidance."""

    if isinstance(value, list):
        return [
            _expand_local_schema_refs(item, root=root, active_refs=active_refs)
            for item in value
        ]
    if not isinstance(value, Mapping):
        return value
    reference = value.get("$ref")
    if isinstance(reference, str) and reference not in active_refs:
        resolved = _local_schema_ref(root, reference)
        if resolved is not None:
            expanded = _expand_local_schema_refs(
                dict(resolved),
                root=root,
                active_refs=active_refs | {reference},
            )
            siblings = {key: item for key, item in value.items() if key != "$ref"}
            if not siblings:
                return expanded
            return {
                "allOf": [
                    expanded,
                    _expand_local_schema_refs(
                        siblings,
                        root=root,
                        active_refs=active_refs,
                    ),
                ]
            }
    return {
        str(key): _expand_local_schema_refs(
            item,
            root=root,
            active_refs=active_refs,
        )
        for key, item in value.items()
    }


def _make_closed_tuple_items_model_readable(value: JsonValue) -> JsonValue:
    """Remove an unreachable closed-tuple tail marker from model-facing schema.

    In JSON Schema 2020-12, ``items: false`` beside ``prefixItems`` rejects only
    elements after the positional prefix. When ``maxItems`` already prevents
    such a tail, omitting that boolean is validation-equivalent. ``prefixItems``
    already carries every usable item shape, so duplicating those schemas under
    ``items`` only enlarges the provider prompt.
    """

    if isinstance(value, list):
        return [_make_closed_tuple_items_model_readable(item) for item in value]
    if not isinstance(value, Mapping):
        return value
    projected: dict[str, JsonValue] = {
        str(key): _make_closed_tuple_items_model_readable(item)
        for key, item in value.items()
    }
    prefix_items = projected.get("prefixItems")
    max_items = projected.get("maxItems")
    if (
        projected.get("items") is False
        and isinstance(prefix_items, list)
        and prefix_items
        and isinstance(max_items, int)
        and not isinstance(max_items, bool)
        and max_items <= len(prefix_items)
    ):
        projected.pop("items")
    return projected


def correction_value_schema(
    response_schema: Mapping[str, JsonValue],
    target_path: str,
) -> dict[str, JsonValue] | None:
    """Project the exact response-schema contract for one correction path."""

    current: Mapping[str, JsonValue] = response_schema
    for part in _decode_pointer(target_path):
        container = _schema_container_for_part(current, part=part, root=response_schema)
        if container is None:
            return None
        properties = container.get("properties")
        if isinstance(properties, Mapping) and part in properties:
            candidate = properties[part]
        elif part.isdecimal():
            index = int(part)
            prefix_items = container.get("prefixItems")
            candidate = (
                prefix_items[index]
                if isinstance(prefix_items, list) and index < len(prefix_items)
                else container.get("items")
            )
        else:
            return None
        if not isinstance(candidate, Mapping):
            return None
        current = candidate
    expanded = _expand_local_schema_refs(dict(current), root=response_schema)
    projected = _make_closed_tuple_items_model_readable(expanded)
    return dict(projected) if isinstance(projected, Mapping) else None


def semantic_correction_schema(
    plan: SemanticCorrectionPlan,
    *,
    response_schema: Mapping[str, JsonValue] | None = None,
) -> dict[str, JsonValue]:
    """Return the exact bounded schema for one controller-authorized correction."""

    schema = SemanticCorrectionSubmission.model_json_schema()
    candidate_slots = {slot.target_path: slot for slot in plan.candidate_slots}
    replacement_variants: list[JsonValue] = []
    from software_agent_team.response_corrections.plans import (
        _semantic_correction_slot_bindings,
    )

    slot_bindings = _semantic_correction_slot_bindings(plan)
    for handle, path in slot_bindings.items():
        candidate_slot = candidate_slots.get(path)
        if candidate_slot is not None:
            projected: JsonValue = {
                "type": "string",
                "enum": [item.handle for item in candidate_slot.candidates],
            }
        else:
            projected = (
                None
                if response_schema is None
                else correction_value_schema(response_schema, path)
            )
            if projected is None:
                projected = {"$ref": "#/$defs/JsonValue"}
        replacement_variants.append(
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "slot_handle": {
                        "type": "string",
                        "const": handle,
                    },
                    "replacement_value": projected,
                },
                "required": ["slot_handle", "replacement_value"],
            }
        )
    schema["properties"]["replacements"] = {
        "type": "array",
        "title": "Replacements",
        "items": {"oneOf": replacement_variants},
        "minItems": (
            len(plan.evidence.target_paths) if plan.require_all_targets else 1
        ),
        "maxItems": len(plan.evidence.target_paths),
    }
    return schema


def _structured_schema_root_types(
    schema: Mapping[str, JsonValue],
) -> frozenset[str]:
    """Return structured root types only when a schema excludes scalar values."""

    declared = schema.get("type")
    if isinstance(declared, str):
        return (
            frozenset((declared,)) if declared in {"array", "object"} else frozenset()
        )
    if isinstance(declared, list):
        values = {value for value in declared if isinstance(value, str)}
        if values and values.issubset({"array", "object"}):
            return frozenset(values)
        return frozenset()
    for keyword in ("oneOf", "anyOf"):
        branches = schema.get(keyword)
        if not isinstance(branches, list) or not branches:
            continue
        branch_types: set[str] = set()
        for branch in branches:
            if not isinstance(branch, Mapping):
                return frozenset()
            current = _structured_schema_root_types(branch)
            if not current:
                return frozenset()
            branch_types.update(current)
        return frozenset(branch_types)
    return frozenset()


__all__ = [
    "_expand_local_schema_refs",
    "_local_schema_ref",
    "_make_closed_tuple_items_model_readable",
    "_schema_container_for_part",
    "_structured_schema_root_types",
    "correction_value_schema",
    "semantic_correction_schema",
]
