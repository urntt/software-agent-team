"""Planning request, questions, options, and decision records."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from software_agent_team.artifacts import (
    ProductDefinitionDimension,
    ReviewBoundaryKind,
)
from software_agent_team.planning.common import (
    MAX_PLANNING_DECISION_SUMMARY_CHARACTERS,
    PLANNING_OPTION_DESCRIPTION_MAX_LENGTH,
    PLANNING_SCHEMA_VERSION,
    _clean_text,
    _clean_unique,
    _utc,
)
from software_agent_team.planning.models_basic import (
    _DECISION_AUTHORITY,
    _DECISION_PROVENANCE_AUTHORITY,
    PlanningDecisionAuthority,
    PlanningDecisionCategory,
    PlanningDecisionProvenance,
    PlanningDecisionProvenanceKind,
    PlanningQuestionOrigin,
)


class PlanningRequest(BaseModel):
    """Direct user input and explicit authorization before any model work."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[
        2,
        3,
        4,
        5,
        6,
        7,
        8,
        9,
        10,
        11,
        12,
        13,
        14,
        15,
        16,
        17,
        18,
        19,
        20,
        21,
        22,
        PLANNING_SCHEMA_VERSION,
    ] = PLANNING_SCHEMA_VERSION
    run_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    project_name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    source_request: str = Field(min_length=1, max_length=2000)
    destination: str = Field(min_length=1, max_length=4096)
    execution_profile: tuple[str, ...] = Field(min_length=1)
    base_constraints: tuple[str, ...] = ()
    model: str = Field(min_length=3)
    authorization: Literal["user_confirmed"]
    authorized_at: datetime

    @field_validator("source_request", "destination", "model")
    @classmethod
    def require_clean_text(cls, value: str) -> str:
        return _clean_text(value, label="Planning request text")

    @field_validator("execution_profile", "base_constraints")
    @classmethod
    def require_clean_unique_text(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _clean_unique(values, label="Planning request")

    @field_validator("model")
    @classmethod
    def require_model_reference(cls, value: str) -> str:
        cleaned = value.strip()
        provider, separator, model = cleaned.partition("/")
        if (
            not separator
            or not provider
            or not model
            or any(character.isspace() for character in cleaned)
        ):
            raise ValueError("Planning requires a canonical provider/model reference")
        return cleaned

    @field_validator("authorized_at")
    @classmethod
    def require_authorized_timestamp(cls, value: datetime) -> datetime:
        return _utc(value)


class PlanningOptionValue(BaseModel):
    """One exact product-definition value offered by a suggested answer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dimension: ProductDefinitionDimension
    value: str = Field(min_length=1, max_length=500)

    @field_validator("value")
    @classmethod
    def require_clean_value(cls, value: str) -> str:
        return _clean_text(value, label="Planning option product value")


PLANNING_OPTION_DESCRIPTION_MAX_LENGTH = 300


class PlanningOption(BaseModel):
    """One suggested answer while preserving a custom-answer path."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    label: str = Field(min_length=1, max_length=80)
    description: str = Field(
        min_length=1, max_length=PLANNING_OPTION_DESCRIPTION_MAX_LENGTH
    )
    product_definition_values: tuple[PlanningOptionValue, ...] = ()

    @field_validator("label", "description")
    @classmethod
    def require_clean_text(cls, value: str) -> str:
        return _clean_text(value, label="Planning option text")

    @model_validator(mode="after")
    def require_unique_product_dimensions(self) -> Self:
        dimensions = [item.dimension for item in self.product_definition_values]
        if len(dimensions) != len(set(dimensions)):
            raise ValueError("Planning option product dimensions must be unique")
        return self


class PlanningQuestion(BaseModel):
    """One high-value clarification selected by the bootstrap Planner."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    text: str = Field(min_length=1, max_length=500)
    why: str = Field(min_length=1, max_length=500)
    decision_category: PlanningDecisionCategory | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    decision_owner: PlanningDecisionAuthority | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    missing_evidence: tuple[str, ...] = Field(
        default=(),
        min_length=1,
        exclude_if=lambda values: not values,
    )
    material_consequences: tuple[str, ...] = Field(
        default=(),
        min_length=1,
        exclude_if=lambda values: not values,
    )
    product_definition_dimensions: tuple[ProductDefinitionDimension, ...] = Field(
        default=(),
        exclude_if=lambda values: not values,
        description=(
            "Every product-depth dimension this answer is intended to resolve; "
            "empty for a material product decision outside the six adequacy "
            "dimensions."
        ),
    )
    options: tuple[PlanningOption, ...] = Field(min_length=2, max_length=3)
    allow_custom: Literal[True] = True

    @field_validator("text", "why")
    @classmethod
    def require_clean_text(cls, value: str) -> str:
        return _clean_text(value, label="Planning question text")

    @field_validator("missing_evidence", "material_consequences")
    @classmethod
    def require_clean_context(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _clean_unique(values, label="Planning question context")

    @model_validator(mode="after")
    def require_unique_options(self) -> Self:
        option_ids = [option.id for option in self.options]
        if len(option_ids) != len(set(option_ids)):
            raise ValueError("Planning question option IDs must be unique")
        if len(self.product_definition_dimensions) != len(
            set(self.product_definition_dimensions)
        ):
            raise ValueError(
                "Planning question product-definition dimensions must be unique"
            )
        return self


class PlanningQuestionAdmission(BaseModel):
    """Controller-owned provenance for presenting one validated question."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    origin: PlanningQuestionOrigin
    product_definition_dimensions: tuple[ProductDefinitionDimension, ...] = ()
    controller_invariant_ids: tuple[str, ...] = ()
    controller_decision_id: str | None = Field(
        default=None,
        pattern=r"^DECISION_[A-Z0-9_]+$",
        exclude_if=lambda value: value is None,
    )

    @field_validator("controller_invariant_ids")
    @classmethod
    def require_clean_invariant_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _clean_unique(values, label="Planning question invariant")

    @model_validator(mode="after")
    def require_origin_evidence(self) -> Self:
        if self.origin is PlanningQuestionOrigin.CONTROLLER_REQUIREMENT:
            if len(self.product_definition_dimensions) > 1:
                raise ValueError(
                    "Controller-required questions cannot span product dimensions"
                )
            if not self.controller_invariant_ids:
                raise ValueError(
                    "Controller-required questions need invariant evidence"
                )
            if (
                not self.product_definition_dimensions
                and self.controller_decision_id is None
            ):
                raise ValueError(
                    "Controller-required questions must bind a product dimension, "
                    "a material decision, or both"
                )
        elif self.controller_invariant_ids or self.controller_decision_id is not None:
            raise ValueError(
                "Planner-suggested questions cannot claim Controller recovery evidence"
            )
        return self


class PresentedPlanningQuestion(PlanningQuestion):
    """Validated question plus non-model presentation authority."""

    admission: PlanningQuestionAdmission


class PlanningQuestionAnswer(BaseModel):
    """One user answer with optional exact values from a displayed suggestion."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(min_length=1, max_length=10_000)
    selected_option_id: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    product_definition_values: tuple[PlanningOptionValue, ...] = ()

    @field_validator("text")
    @classmethod
    def require_clean_answer(cls, value: str) -> str:
        return _clean_text(value, label="Planning question answer")

    @model_validator(mode="after")
    def require_selected_option_for_values(self) -> Self:
        if self.product_definition_values and self.selected_option_id is None:
            raise ValueError("Planning answer product values require a selected option")
        return self


@dataclass(frozen=True)
class _PlanningQuestionContract:
    """Controller-recovered authority and adequacy scope for one question."""

    category: PlanningDecisionCategory
    owner: PlanningDecisionAuthority
    product_definition_dimensions: tuple[ProductDefinitionDimension, ...] = ()
    answer: str | None = None
    answer_dimension_values: tuple[tuple[ProductDefinitionDimension, str], ...] = ()
    approved_dimension_values: tuple[tuple[ProductDefinitionDimension, str], ...] = ()
    required_decision_id: str | None = None


class PlanningDecisionRecord(BaseModel):
    """Attributable resolution or proposal for one material decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^DECISION_[A-Z0-9_]+$")
    category: PlanningDecisionCategory
    authority: PlanningDecisionAuthority
    provenance: PlanningDecisionProvenance | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description=(
            "Current live decisions use an explicit typed source. Absence is kept "
            "only so schema-v2 through schema-v7 evidence remains readable."
        ),
    )
    summary: str = Field(
        min_length=1,
        max_length=MAX_PLANNING_DECISION_SUMMARY_CHARACTERS,
    )
    rationale: str = Field(min_length=1, max_length=500)
    question_id: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]*$",
        exclude_if=lambda value: value is None,
        description="Legacy schema-v2 through schema-v7 question provenance.",
    )

    @field_validator("summary", "rationale")
    @classmethod
    def require_clean_text(cls, value: str) -> str:
        return _clean_text(value, label="Planning decision text")

    @model_validator(mode="after")
    def require_category_authority(self) -> Self:
        expected = _DECISION_AUTHORITY[self.category]
        if self.authority is not expected:
            raise ValueError(
                f"decision category {self.category.value} belongs to {expected.value}"
            )
        if self.provenance is not None:
            expected_provenance_authority = _DECISION_PROVENANCE_AUTHORITY[
                self.provenance.kind
            ]
            if self.authority is not expected_provenance_authority:
                raise ValueError(
                    f"{self.provenance.kind.value} provenance belongs to "
                    f"{expected_provenance_authority.value}"
                )
            if self.question_id is not None and (
                self.provenance.kind
                is not PlanningDecisionProvenanceKind.RESOLVED_QUESTION
                or self.provenance.source != self.question_id
            ):
                raise ValueError(
                    "legacy question_id must match resolved-question provenance"
                )
        if (
            self.authority
            in {
                PlanningDecisionAuthority.AGENT_AUTONOMY,
                PlanningDecisionAuthority.CONTROLLER_POLICY,
            }
            and self.question_id is not None
        ):
            raise ValueError(
                "autonomous or controller-policy decisions cannot claim a user question"
            )
        return self

    @property
    def resolved_question_id(self) -> str | None:
        """Return current or legacy question provenance through one authority."""

        if (
            self.provenance is not None
            and self.provenance.kind is PlanningDecisionProvenanceKind.RESOLVED_QUESTION
        ):
            return self.provenance.source
        return self.question_id


_ALL_REVIEW_BOUNDARIES = tuple(ReviewBoundaryKind)
_ABSOLUTE_GUARANTEE_PATTERN = re.compile(
    r"(?:\bnever\b|\bno\b|\b(?:must|shall|may|can|does?|is|are)\s+not\b|"
    r"\bcannot\b|"
    r"\bunder\s+no\s+circumstances\b|\bat\s+any\s+(?:depth|level|time)\b|"
    r"不得|禁止|永不|绝不|任何(?:层级|深度|情况下)?.{0,8}(?:不|无))",
    flags=re.IGNORECASE,
)
_TRUST_BOUNDARY_PATTERN = re.compile(
    r"\b(?:symlinks?|symbolic links?|path traversal|root escape|"
    r"unauthori[sz]ed|untrusted|access|private|personal data|"
    r"credentials?|secrets?|passwords?|"
    r"permissions?|sandbox|exfiltrat\w*|leak\w*|disclos\w*|"
    r"delet\w*|overwrit\w*|execut\w*|"
    r"outside (?:of )?(?:the )?(?:project|root|workspace|sandbox|directory)|"
    r"network|external (?:service|host|process|command))\b|"
    r"越权|越界|沙箱|符号链接|凭据|密钥|口令|密码|敏感|隐私|"
    r"网络|外部服务|删除|覆盖|泄露|泄漏|执行",
    flags=re.IGNORECASE,
)


def _requires_full_review_boundaries(text: str) -> bool:
    """Distinguish trust-boundary guarantees from ordinary negative behavior."""

    for clause in re.split(r"[.!?;\n。\uFF1B\uFF01\uFF1F]+", text):
        if _ABSOLUTE_GUARANTEE_PATTERN.search(clause) and (
            _TRUST_BOUNDARY_PATTERN.search(clause)
        ):
            return True
    return False


