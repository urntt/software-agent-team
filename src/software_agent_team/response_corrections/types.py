"""Typed response models, enums, and constants for semantic corrections."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

CORRECTION_SCHEMA_VERSION = 2
MAX_CORRECTION_FIELDS = 64
MAX_CORRECTION_PROMPT_CANDIDATES = 256
MAX_SERIALIZED_CORRECTION_VALUE_BYTES = 512 * 1024


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
        from software_agent_team.response_corrections.diagnostics import (
            _decode_pointer,
        )

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
        from software_agent_team.response_corrections.diagnostics import (
            _paths_overlap,
        )

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
        from software_agent_team.response_corrections.diagnostics import (
            _json_sha256,
        )

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


class SemanticCorrectionReplacement(BaseModel):
    """One value bound to a short Controller-issued request-local slot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    slot_handle: str = Field(pattern=r"^slot_[1-9][0-9]?$")
    replacement_value: JsonValue


class SemanticCorrectionSubmission(BaseModel):
    """The only model-owned, identity-bound values accepted during correction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    replacements: tuple[SemanticCorrectionReplacement, ...] = Field(
        min_length=1,
        max_length=MAX_CORRECTION_FIELDS,
    )

    @field_validator("replacements")
    @classmethod
    def require_unique_slot_handles(
        cls,
        values: tuple[SemanticCorrectionReplacement, ...],
    ) -> tuple[SemanticCorrectionReplacement, ...]:
        handles = tuple(item.slot_handle for item in values)
        if len(handles) != len(set(handles)):
            raise ValueError("semantic correction slot handles must be unique")
        return values


class SemanticCorrectionCandidate(BaseModel):
    """One Controller-owned exact value exposed through a short local handle."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    handle: str = Field(pattern=r"^candidate_[1-9][0-9]?$")
    replacement_value: JsonValue
    source: str = Field(min_length=1, max_length=200)

    @field_validator("source")
    @classmethod
    def require_safe_source(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if not cleaned or "\x00" in cleaned:
            raise ValueError("correction candidate source must be bounded text")
        return cleaned


class SemanticCorrectionCandidateSlot(BaseModel):
    """Controller-owned candidate vocabulary for one correction target."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_path: str = Field(min_length=1, max_length=500)
    candidates: tuple[SemanticCorrectionCandidate, ...] = Field(
        min_length=1,
        max_length=MAX_CORRECTION_PROMPT_CANDIDATES,
    )

    @field_validator("target_path")
    @classmethod
    def require_target_pointer(cls, value: str) -> str:
        if value == "/" or not value.startswith("/"):
            raise ValueError("candidate slot requires a targeted JSON pointer")
        from software_agent_team.response_corrections.diagnostics import (
            _decode_pointer,
        )

        _decode_pointer(value)
        return value

    @field_validator("candidates")
    @classmethod
    def require_unique_candidates(
        cls,
        values: tuple[SemanticCorrectionCandidate, ...],
    ) -> tuple[SemanticCorrectionCandidate, ...]:
        handles = tuple(item.handle for item in values)
        from software_agent_team.response_corrections.diagnostics import (
            _json_sha256,
        )

        identities = tuple(_json_sha256(item.replacement_value) for item in values)
        if len(handles) != len(set(handles)):
            raise ValueError("correction candidate handles must be unique")
        if len(identities) != len(set(identities)):
            raise ValueError("correction candidate values must be unique")
        return values


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
    candidate_slots: tuple[SemanticCorrectionCandidateSlot, ...] = ()
    require_all_targets: bool = False


@dataclass(frozen=True)
class SemanticCorrectionApplication:
    """Corrected payload plus auditable controller-owned value bindings."""

    payload: dict[str, object]
    normalizations: tuple[str, ...] = ()


class SemanticCorrectionSubmissionError(ValueError):
    """Rejected model input, distinct from an invalid Controller-owned plan.

    A recovery plan is a private, unpublished staging copy. It exists only when
    exact candidate binding strictly reduced the remaining selection slots.
    """

    def __init__(
        self,
        message: str,
        *,
        plan: SemanticCorrectionPlan,
        paths: tuple[str, ...] | None = None,
        recovery_plan: SemanticCorrectionPlan | None = None,
        normalizations: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        from software_agent_team.response_corrections.diagnostics import (
            diagnostic_from_invariant,
        )

        self.diagnostic = diagnostic_from_invariant(
            plan.base_payload,
            failure_class=ResponseFailureClass.SEMANTIC_SCHEMA,
            authority=ResponseIssueAuthority.MODEL,
            code="correction_submission_invalid",
            invariant_id="correction_submission_authority",
            subjects=(),
            message=message,
            paths=plan.evidence.target_paths if paths is None else paths,
        )
        self.recovery_plan = recovery_plan
        self.normalizations = normalizations


__all__ = [
    "CORRECTION_SCHEMA_VERSION",
    "MAX_CORRECTION_FIELDS",
    "MAX_CORRECTION_PROMPT_CANDIDATES",
    "MAX_SERIALIZED_CORRECTION_VALUE_BYTES",
    "ResponseFailureClass",
    "ResponseIssueAuthority",
    "ResponseIssueSubject",
    "ResponseIssueSubjectKind",
    "ResponseValidationDiagnostic",
    "ResponseValidationIssue",
    "SemanticCorrectionApplication",
    "SemanticCorrectionCandidate",
    "SemanticCorrectionCandidateSlot",
    "SemanticCorrectionOutcome",
    "SemanticCorrectionPlan",
    "SemanticCorrectionReplacement",
    "SemanticCorrectionRequestEvidence",
    "SemanticCorrectionSubmission",
    "SemanticCorrectionSubmissionError",
]
