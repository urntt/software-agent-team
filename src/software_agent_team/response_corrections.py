"""Typed, field-targeted correction of model-owned semantic JSON."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    ValidationError,
    field_validator,
    model_validator,
)

CORRECTION_SCHEMA_VERSION = 2
MAX_CORRECTION_FIELDS = 64


class ResponseFailureClass(StrEnum):
    """Root-cause class for one rejected model response."""

    TRANSPORT = "transport"
    SEMANTIC_SCHEMA = "semantic_schema"
    SEMANTIC_CONTEXT = "semantic_context"
    EVIDENCE_GROUNDING = "evidence_grounding"
    MISSING_USER_DECISION = "missing_user_decision"


class ResponseIssueAuthority(StrEnum):
    """Authority that can resolve one response-validation issue."""

    TRANSPORT = "transport"
    MODEL = "model"
    USER = "user"
    EVIDENCE = "evidence"
    CONTROLLER = "controller"


class ResponseIssueSubjectKind(StrEnum):
    """Stable entity type affected by one response invariant."""

    AGENT = "agent"
    CAPABILITY = "capability"
    CRITERION = "criterion"
    DECISION = "decision"
    QUESTION = "question"
    REQUIREMENT = "requirement"
    TASK = "task"


class ResponseIssueSubject(BaseModel):
    """Content-free identity of an entity involved in a response defect."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ResponseIssueSubjectKind
    identifier: str = Field(min_length=1, max_length=200)

    @field_validator("identifier")
    @classmethod
    def require_safe_identifier(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if not cleaned or any(
            character in cleaned for character in ("\x00", "\r", "\n")
        ):
            raise ValueError("response issue subject must be bounded text")
        return cleaned


class SemanticCorrectionOutcome(StrEnum):
    """Controller conclusion after applying one correction submission."""

    ACCEPTED = "accepted"
    IMPROVED = "improved"
    NO_IMPROVEMENT = "no_improvement"
    INVALID_SUBMISSION = "invalid_submission"
    NOT_EVALUATED = "not_evaluated"


class ResponseValidationIssue(BaseModel):
    """One content-free, typed error located in model-owned JSON."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1, max_length=500)
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{1,99}$")
    invariant_id: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]{1,99}$",
        exclude_if=lambda value: value is None,
    )
    subjects: tuple[ResponseIssueSubject, ...] = Field(
        default=(),
        exclude_if=lambda values: not values,
    )
    message: str = Field(min_length=1, max_length=500)
    authority: ResponseIssueAuthority

    @field_validator("path")
    @classmethod
    def require_json_pointer(cls, value: str) -> str:
        if value == "/":
            return value
        if not value.startswith("/") or any(
            character in value for character in ("\x00", "\r", "\n")
        ):
            raise ValueError("response issue path must be a JSON pointer")
        _decode_pointer(value)
        return value

    @field_validator("message")
    @classmethod
    def require_safe_message(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if not cleaned:
            raise ValueError("response issue message must not be blank")
        return cleaned

    @field_validator("subjects")
    @classmethod
    def require_canonical_subjects(
        cls,
        values: tuple[ResponseIssueSubject, ...],
    ) -> tuple[ResponseIssueSubject, ...]:
        identities = tuple((item.kind.value, item.identifier) for item in values)
        if len(identities) != len(set(identities)) or identities != tuple(
            sorted(identities)
        ):
            raise ValueError("response issue subjects must be unique and sorted")
        return values

    @property
    def identity(
        self,
    ) -> tuple[
        str,
        tuple[tuple[str, str], ...],
        str | None,
        ResponseIssueAuthority,
    ]:
        """Return the stable root-cause identity used for convergence."""

        subjects = tuple((item.kind.value, item.identifier) for item in self.subjects)
        return (
            self.invariant_id or self.code,
            subjects,
            None if subjects else self.path,
            self.authority,
        )


class ResponseValidationDiagnostic(BaseModel):
    """Stable failure set used to decide whether correction is possible."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1, CORRECTION_SCHEMA_VERSION] = CORRECTION_SCHEMA_VERSION
    failure_class: ResponseFailureClass
    response_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    issues: tuple[ResponseValidationIssue, ...] = Field(
        min_length=1,
        max_length=MAX_CORRECTION_FIELDS,
    )
    correction_paths: tuple[str, ...] = Field(max_length=MAX_CORRECTION_FIELDS)

    @field_validator("correction_paths")
    @classmethod
    def require_unique_correction_paths(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        if len(values) != len(set(values)) or tuple(sorted(values)) != values:
            raise ValueError("response correction paths must be unique and sorted")
        for value in values:
            if value == "/":
                raise ValueError("whole-response replacement is not targeted")
            ResponseValidationIssue(
                path=value,
                code="repair_path",
                invariant_id="repair_path",
                message="validated repair path",
                authority=ResponseIssueAuthority.MODEL,
            )
        return values

    @model_validator(mode="after")
    def bind_correction_paths_to_model_issues(self) -> Self:
        if self.schema_version == CORRECTION_SCHEMA_VERSION and any(
            issue.invariant_id is None for issue in self.issues
        ):
            raise ValueError("current response issues require a stable invariant ID")
        model_paths = {
            issue.path
            for issue in self.issues
            if issue.authority is ResponseIssueAuthority.MODEL
        }
        non_model_paths = {
            issue.path
            for issue in self.issues
            if issue.authority is not ResponseIssueAuthority.MODEL
        }
        if any(
            not any(_paths_overlap(path, issue_path) for issue_path in model_paths)
            for path in self.correction_paths
        ):
            raise ValueError("correction paths must resolve model-owned issues")
        if any(
            any(_paths_overlap(path, issue_path) for issue_path in non_model_paths)
            for path in self.correction_paths
        ):
            raise ValueError("correction paths cannot overlap non-model authority")
        return self

    @property
    def fingerprint(self) -> str:
        """Return a content-free identity for non-convergence detection."""

        if self.schema_version == 1:
            return _json_sha256(
                {
                    "failure_class": self.failure_class.value,
                    "issues": [
                        {
                            "path": issue.path,
                            "code": issue.code,
                            "authority": issue.authority.value,
                        }
                        for issue in self.issues
                    ],
                    "correction_paths": list(self.correction_paths),
                }
            )
        identities = sorted(set(issue.identity for issue in self.issues))
        payload = {
            "failure_class": self.failure_class.value,
            "issues": [
                {
                    "invariant_id": invariant_id,
                    "subjects": [
                        {"kind": kind, "identifier": identifier}
                        for kind, identifier in subjects
                    ],
                    "path": path,
                    "authority": authority.value,
                }
                for invariant_id, subjects, path, authority in identities
            ],
        }
        return _json_sha256(payload)


class SemanticCorrectionEnvelope(BaseModel):
    """The only model submission accepted during targeted correction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["semantic_correction_v2"] = "semantic_correction_v2"
    base_response_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    replacement_values: tuple[JsonValue, ...] = Field(
        min_length=1,
        max_length=MAX_CORRECTION_FIELDS,
    )


class SemanticCorrectionRequestEvidence(BaseModel):
    """Content-free trace of one controller-authorized correction request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    base_response_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    issue_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_paths: tuple[str, ...] = Field(
        min_length=1,
        max_length=MAX_CORRECTION_FIELDS,
    )
    preserved_top_level_paths: tuple[str, ...] = Field(
        max_length=MAX_CORRECTION_FIELDS,
    )


@dataclass(frozen=True)
class SemanticCorrectionPlan:
    """In-memory base content plus its persistable correction authority."""

    base_payload: dict[str, JsonValue]
    diagnostic: ResponseValidationDiagnostic
    evidence: SemanticCorrectionRequestEvidence


def _json_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def semantic_payload_sha256(payload: dict[str, object]) -> str:
    """Return the canonical identity of one untrusted semantic payload."""

    return _json_sha256(payload)


def _encode_pointer(parts: tuple[str | int, ...]) -> str:
    if not parts:
        return "/"
    return "/" + "/".join(
        str(part).replace("~", "~0").replace("/", "~1") for part in parts
    )


def _decode_pointer(pointer: str) -> tuple[str, ...]:
    if pointer == "/":
        return ()
    parts: list[str] = []
    for raw in pointer[1:].split("/"):
        index = 0
        while index < len(raw):
            if raw[index] == "~":
                if index + 1 >= len(raw) or raw[index + 1] not in {"0", "1"}:
                    raise ValueError("JSON pointer contains an invalid escape")
                index += 2
            else:
                index += 1
        parts.append(raw.replace("~1", "/").replace("~0", "~"))
    return tuple(parts)


def _paths_overlap(left: str, right: str) -> bool:
    left_parts = _decode_pointer(left)
    right_parts = _decode_pointer(right)
    limit = min(len(left_parts), len(right_parts))
    return left_parts[:limit] == right_parts[:limit]


def _minimal_correction_paths(
    issues: tuple[ResponseValidationIssue, ...],
) -> tuple[str, ...]:
    non_model_paths = {
        issue.path
        for issue in issues
        if issue.authority is not ResponseIssueAuthority.MODEL
    }
    candidates = sorted(
        {
            issue.path
            for issue in issues
            if (
                issue.authority is ResponseIssueAuthority.MODEL
                and issue.path != "/"
                and not any(
                    _paths_overlap(issue.path, non_model_path)
                    for non_model_path in non_model_paths
                )
            )
        }
    )
    # Prefer a precise descendant over a cascading container error. If only a
    # container-level validator is available, that container remains the target.
    return tuple(
        path
        for path in candidates
        if not any(
            other != path
            and len(_decode_pointer(other)) > len(_decode_pointer(path))
            and _paths_overlap(path, other)
            for other in candidates
        )
    )


def diagnostic_from_validation_error(
    error: ValidationError,
    payload: dict[str, object],
    *,
    failure_class: ResponseFailureClass = ResponseFailureClass.SEMANTIC_SCHEMA,
    authority: ResponseIssueAuthority = ResponseIssueAuthority.MODEL,
    protected_field_names: frozenset[str] = frozenset(),
) -> ResponseValidationDiagnostic:
    """Convert Pydantic failures to a bounded content-free field set."""

    raw_issues = error.errors(
        include_url=False,
        include_context=False,
        include_input=False,
    )
    if len(raw_issues) > MAX_CORRECTION_FIELDS:
        issues = (
            ResponseValidationIssue(
                path="/",
                code="validation_issue_overflow",
                invariant_id="validation_issue_overflow",
                message=(
                    "Response has too many independent validation failures for "
                    "safe field-targeted correction"
                ),
                authority=ResponseIssueAuthority.CONTROLLER,
            ),
        )
        return ResponseValidationDiagnostic(
            failure_class=failure_class,
            response_sha256=semantic_payload_sha256(payload),
            issues=issues,
            correction_paths=(),
        )
    issues = tuple(
        ResponseValidationIssue(
            path=_encode_pointer(tuple(item for item in issue["loc"])),
            code=str(issue["type"]).replace(".", "_")[:100],
            invariant_id=str(issue["type"]).replace(".", "_")[:100],
            message=str(issue["msg"])[:500],
            authority=(
                ResponseIssueAuthority.CONTROLLER
                if (
                    issue["type"] == "extra_forbidden"
                    and issue["loc"]
                    and str(issue["loc"][-1]) in protected_field_names
                )
                else authority
            ),
        )
        for issue in raw_issues
    )
    return ResponseValidationDiagnostic(
        failure_class=failure_class,
        response_sha256=semantic_payload_sha256(payload),
        issues=issues,
        correction_paths=_minimal_correction_paths(issues),
    )


def diagnostic_from_message(
    payload: dict[str, object],
    *,
    failure_class: ResponseFailureClass,
    authority: ResponseIssueAuthority,
    code: str,
    message: str,
    paths: tuple[str, ...],
) -> ResponseValidationDiagnostic:
    """Create a typed diagnostic for a controller context validator."""

    issues = tuple(
        ResponseValidationIssue(
            path=path,
            code=code,
            invariant_id=code,
            message=message[:500],
            authority=authority,
        )
        for path in paths
    )
    return ResponseValidationDiagnostic(
        failure_class=failure_class,
        response_sha256=semantic_payload_sha256(payload),
        issues=issues,
        correction_paths=_minimal_correction_paths(issues),
    )


def diagnostic_from_invariant(
    payload: dict[str, object],
    *,
    failure_class: ResponseFailureClass,
    authority: ResponseIssueAuthority,
    code: str,
    invariant_id: str,
    subjects: tuple[ResponseIssueSubject, ...],
    message: str,
    paths: tuple[str, ...],
) -> ResponseValidationDiagnostic:
    """Create a diagnostic from a validator-owned invariant and entities."""

    issues = tuple(
        ResponseValidationIssue(
            path=path,
            code=code,
            invariant_id=invariant_id,
            subjects=subjects,
            message=message[:500],
            authority=authority,
        )
        for path in paths
    )
    return ResponseValidationDiagnostic(
        failure_class=failure_class,
        response_sha256=semantic_payload_sha256(payload),
        issues=issues,
        correction_paths=_minimal_correction_paths(issues),
    )


def diagnostic_from_transport(
    value: str,
    *,
    code: str,
    message: str,
) -> ResponseValidationDiagnostic:
    """Record an unparseable response without pretending a field can be patched."""

    issue = ResponseValidationIssue(
        path="/",
        code=code,
        invariant_id=code,
        message=message[:500],
        authority=ResponseIssueAuthority.TRANSPORT,
    )
    return ResponseValidationDiagnostic(
        failure_class=ResponseFailureClass.TRANSPORT,
        response_sha256=hashlib.sha256(value.encode()).hexdigest(),
        issues=(issue,),
        correction_paths=(),
    )


def deterministically_remove_forbidden_fields(
    payload: dict[str, object],
    error: ValidationError,
    *,
    protected_field_names: frozenset[str] = frozenset(),
) -> tuple[dict[str, object], tuple[str, ...]]:
    """Discard only non-authoritative fields the schema explicitly forbids."""

    paths = sorted(
        {
            _encode_pointer(tuple(item for item in issue["loc"]))
            for issue in error.errors(
                include_url=False,
                include_context=False,
                include_input=False,
            )
            if (
                issue["type"] == "extra_forbidden"
                and issue["loc"]
                and str(issue["loc"][-1]) not in protected_field_names
            )
        },
        key=lambda value: (-len(_decode_pointer(value)), value),
    )
    if len(paths) > MAX_CORRECTION_FIELDS:
        return deepcopy(payload), ()
    normalized: object = deepcopy(payload)
    removed: list[str] = []
    for path in paths:
        parts = _decode_pointer(path)
        parent = normalized
        for part in parts[:-1]:
            if isinstance(parent, dict) and part in parent:
                parent = parent[part]
            elif (
                isinstance(parent, list)
                and part.isdecimal()
                and int(part) < len(parent)
            ):
                parent = parent[int(part)]
            else:
                parent = None
                break
        final = parts[-1]
        if isinstance(parent, dict) and final in parent:
            del parent[final]
            removed.append(path)
    assert isinstance(normalized, dict)
    return normalized, tuple(sorted(removed))


def build_semantic_correction_plan(
    payload: dict[str, object],
    diagnostic: ResponseValidationDiagnostic,
) -> SemanticCorrectionPlan | None:
    """Authorize only field-level model correction, never full regeneration."""

    if (
        diagnostic.response_sha256 != semantic_payload_sha256(payload)
        or not diagnostic.correction_paths
    ):
        return None
    targeted_top_level = {
        _decode_pointer(path)[0] for path in diagnostic.correction_paths
    }
    preserved = tuple(
        sorted(
            _encode_pointer((key,)) for key in payload if key not in targeted_top_level
        )
    )
    evidence = SemanticCorrectionRequestEvidence(
        base_response_sha256=diagnostic.response_sha256,
        issue_fingerprint=diagnostic.fingerprint,
        target_paths=diagnostic.correction_paths,
        preserved_top_level_paths=preserved,
    )
    return SemanticCorrectionPlan(
        base_payload=deepcopy(payload),
        diagnostic=diagnostic,
        evidence=evidence,
    )


def semantic_correction_schema(
    plan: SemanticCorrectionPlan,
) -> dict[str, JsonValue]:
    """Return the exact bounded schema for one controller-authorized correction."""

    schema = SemanticCorrectionEnvelope.model_json_schema()
    value_schema = schema["properties"]["replacement_values"]
    value_schema["minItems"] = len(plan.evidence.target_paths)
    value_schema["maxItems"] = len(plan.evidence.target_paths)
    return schema


def correction_prompt(
    plan: SemanticCorrectionPlan,
    *,
    submission_tool: str | None = None,
) -> str:
    """Render the small correction envelope contract without echoing content."""

    target_slots = [
        {
            "slot": index,
            "target_path": path,
            "errors": [
                {
                    "code": issue.code,
                    "invariant_id": issue.invariant_id,
                    "subjects": [
                        item.model_dump(mode="json") for item in issue.subjects
                    ],
                    "message": issue.message,
                }
                for issue in plan.diagnostic.issues
                if issue.path == path
            ],
        }
        for index, path in enumerate(plan.evidence.target_paths)
    ]
    schema = semantic_correction_schema(plan)
    transport_instruction = (
        "Return exactly one JSON object and no prose or Markdown fence."
        if submission_tool is None
        else (
            f"Call `{submission_tool}` exactly once with the correction envelope as "
            "its arguments. Do not serialize the envelope in assistant text. The "
            "successful submission ends this invocation."
        )
    )
    return (
        "\n\nTARGETED_SEMANTIC_CORRECTION_V2\n"
        "This correction contract supersedes the earlier FINAL_RESPONSE_CONTRACT "
        "for this invocation. "
        "The prior semantic JSON object was parsed and retained by the controller. "
        "Do not regenerate or repeat that object. Return only a correction envelope "
        "matching CORRECTION_SCHEMA_JSON. Provide one semantic value for each slot, "
        "in exact slot order. Do not repeat or choose target paths; the controller "
        "owns those bindings. All other fields are immutable and will be preserved "
        "by the controller.\n"
        f"BASE_RESPONSE_SHA256\n{plan.evidence.base_response_sha256}\n"
        "TARGET_SLOTS_AND_ERRORS\n"
        f"{json.dumps(target_slots, ensure_ascii=False, indent=2)}\n"
        "CORRECTION_SCHEMA_JSON\n"
        f"{json.dumps(schema, ensure_ascii=False, indent=2)}\n"
        f"{transport_instruction}"
    )


def apply_semantic_correction(
    envelope_payload: dict[str, object],
    plan: SemanticCorrectionPlan,
) -> dict[str, object]:
    """Apply exactly the authorized replacements to a copied base payload."""

    envelope = SemanticCorrectionEnvelope.model_validate(envelope_payload)
    if envelope.base_response_sha256 != plan.evidence.base_response_sha256:
        raise ValueError("semantic correction base digest does not match")
    expected_count = len(plan.evidence.target_paths)
    if len(envelope.replacement_values) != expected_count:
        raise ValueError(
            "semantic correction value count differs: "
            f"expected {expected_count}, received {len(envelope.replacement_values)}"
        )

    corrected: object = deepcopy(plan.base_payload)
    for path, replacement_value in zip(
        plan.evidence.target_paths,
        envelope.replacement_values,
        strict=True,
    ):
        parts = _decode_pointer(path)
        parent = corrected
        for part in parts[:-1]:
            if isinstance(parent, dict):
                if part not in parent:
                    raise ValueError(f"semantic correction parent is missing: {path}")
                parent = parent[part]
            elif isinstance(parent, list):
                if not part.isdecimal() or int(part) >= len(parent):
                    raise ValueError(f"semantic correction index is invalid: {path}")
                parent = parent[int(part)]
            else:
                raise ValueError(
                    f"semantic correction parent is not a container: {path}"
                )
        final = parts[-1]
        if isinstance(parent, dict):
            parent[final] = deepcopy(replacement_value)
        elif isinstance(parent, list):
            if not final.isdecimal() or int(final) >= len(parent):
                raise ValueError(f"semantic correction index is invalid: {path}")
            parent[int(final)] = deepcopy(replacement_value)
        else:
            raise ValueError(f"semantic correction target is not a container: {path}")
    assert isinstance(corrected, dict)
    return corrected


def correction_outcome(
    plan: SemanticCorrectionPlan,
    diagnostic: ResponseValidationDiagnostic | None,
    *,
    seen_fingerprints: frozenset[str],
) -> SemanticCorrectionOutcome:
    """Continue only after every targeted prior typed issue was removed."""

    if diagnostic is None:
        return SemanticCorrectionOutcome.ACCEPTED
    if diagnostic.fingerprint in seen_fingerprints:
        return SemanticCorrectionOutcome.NO_IMPROVEMENT
    prior_issues = {
        issue.identity
        for issue in plan.diagnostic.issues
        if any(
            _paths_overlap(issue.path, target) for target in plan.evidence.target_paths
        )
    }
    current_issues = {issue.identity for issue in diagnostic.issues}
    if prior_issues & current_issues:
        return SemanticCorrectionOutcome.NO_IMPROVEMENT
    return SemanticCorrectionOutcome.IMPROVED
