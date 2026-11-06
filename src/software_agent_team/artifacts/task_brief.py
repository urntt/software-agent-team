"""Task brief schemas and confirmed requirements models.

Defines AcceptanceCriterion, ProductDefinition, and TaskBrief contracts that
govern task requirements across planning and execution phases.
"""

from __future__ import annotations

import re
from typing import Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

from software_agent_team.artifacts.types import (
    DeliveryMaturity,
    ProductDefinitionDimension,
    ProductDefinitionDisposition,
    ReviewBoundaryKind,
)


class AcceptanceCriterion(BaseModel):
    """One independently verifiable condition for accepting a result."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, pattern=r"^[A-Z][A-Z0-9_-]*$")
    description: str = Field(min_length=1)
    verification: str = Field(min_length=1)
    review_boundaries: tuple[ReviewBoundaryKind, ...] = ()

    @field_validator("review_boundaries")
    @classmethod
    def require_unique_review_boundaries(
        cls,
        values: tuple[ReviewBoundaryKind, ...],
    ) -> tuple[ReviewBoundaryKind, ...]:
        """Keep explicit Review obligations ordered and unique."""
        if len(values) != len(set(values)):
            raise ValueError("acceptance review boundaries must be unique")
        return values

    @model_serializer(mode="wrap")
    def omit_empty_review_boundaries(
        self,
        handler: SerializerFunctionWrapHandler,
    ) -> object:
        """Preserve existing TaskBrief bytes when no boundary is required."""
        serialized = handler(self)
        if isinstance(serialized, dict) and not self.review_boundaries:
            serialized.pop("review_boundaries", None)
        return serialized


class ProductDefinitionBasis(BaseModel):
    """Provenance and downstream trace for one product-depth statement."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    disposition: ProductDefinitionDisposition
    source: str = Field(
        min_length=1,
        max_length=500,
        description=(
            "Provenance selector. For explicit_input, use exactly one contiguous "
            "verbatim substring from the user's input with no label, quote "
            "delimiter, stitched excerpt, ellipsis, or commentary. For "
            "resolved_question, use the stable question ID. For "
            "planner_recommendation or not_material, use exactly 'planner'."
        ),
    )
    rationale: str = Field(min_length=1, max_length=500)
    requirement_ids: tuple[str, ...] = ()
    criterion_ids: tuple[str, ...] = ()
    decision_ids: tuple[str, ...] = ()

    @field_validator("source", "rationale")
    @classmethod
    def require_clean_text(cls, value: str) -> str:
        cleaned = value.strip()
        if cleaned != value or not cleaned:
            raise ValueError("product-definition text must be stripped and non-empty")
        return cleaned

    @field_validator("requirement_ids", "criterion_ids", "decision_ids")
    @classmethod
    def require_unique_references(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("product-definition references must be unique")
        return values

    @model_validator(mode="after")
    def require_downstream_trace(self) -> Self:
        if self.disposition is not ProductDefinitionDisposition.NOT_MATERIAL and not (
            self.requirement_ids or self.criterion_ids or self.decision_ids
        ):
            raise ValueError(
                "each material product-definition dimension requires a downstream "
                "reference"
            )
        if any(
            re.fullmatch(r"REQ_[A-Z0-9_]+", value) is None
            for value in self.requirement_ids
        ):
            raise ValueError("product-definition requirements must use REQ_ IDs")
        if any(
            re.fullmatch(r"[A-Z][A-Z0-9_-]*", value) is None
            for value in self.criterion_ids
        ):
            raise ValueError(
                "product-definition criteria must use stable uppercase IDs"
            )
        if any(
            re.fullmatch(r"DECISION_[A-Z0-9_]+", value) is None
            for value in self.decision_ids
        ):
            raise ValueError("product-definition decisions must use DECISION_ IDs")
        return self


class ProductDefinitionStatement(ProductDefinitionBasis):
    """One approved natural-language product-definition dimension."""

    statement: str = Field(min_length=1, max_length=1000)

    @field_validator("statement")
    @classmethod
    def require_clean_statement(cls, value: str) -> str:
        cleaned = value.strip()
        if cleaned != value or not cleaned:
            raise ValueError(
                "product-definition statements must be stripped and non-empty"
            )
        return cleaned


class ProductMaturityDefinition(ProductDefinitionBasis):
    """Approved delivery maturity with the same provenance contract."""

    level: DeliveryMaturity


class ProductDefinitionImpact(BaseModel):
    """User-visible consequences of the approved product definition."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    architecture: str = Field(min_length=1, max_length=1000)
    team: str = Field(min_length=1, max_length=1000)
    cost: str = Field(min_length=1, max_length=1000)
    delivery: str = Field(min_length=1, max_length=1000)

    @field_validator("architecture", "team", "cost", "delivery")
    @classmethod
    def require_clean_impact(cls, value: str) -> str:
        cleaned = value.strip()
        if cleaned != value or not cleaned:
            raise ValueError("product-definition impact must be stripped and non-empty")
        return cleaned


class ProductDefinition(BaseModel):
    """Approved audience, workflow, maturity, quality, and delivery contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_users: ProductDefinitionStatement
    primary_workflow: ProductDefinitionStatement
    delivery_maturity: ProductMaturityDefinition
    usability_expectations: ProductDefinitionStatement
    operational_expectations: ProductDefinitionStatement
    delivery_expectations: ProductDefinitionStatement
    impact: ProductDefinitionImpact

    def dimensions(
        self,
    ) -> tuple[
        tuple[ProductDefinitionDimension, ProductDefinitionBasis],
        ...,
    ]:
        """Return every dimension with its stable identifier."""
        return (
            (ProductDefinitionDimension.TARGET_USERS, self.target_users),
            (ProductDefinitionDimension.PRIMARY_WORKFLOW, self.primary_workflow),
            (ProductDefinitionDimension.DELIVERY_MATURITY, self.delivery_maturity),
            (
                ProductDefinitionDimension.USABILITY_EXPECTATIONS,
                self.usability_expectations,
            ),
            (
                ProductDefinitionDimension.OPERATIONAL_EXPECTATIONS,
                self.operational_expectations,
            ),
            (
                ProductDefinitionDimension.DELIVERY_EXPECTATIONS,
                self.delivery_expectations,
            ),
        )


class TaskBrief(BaseModel):
    """Confirmed requirements passed unchanged to comparable team runs."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    title: str = Field(min_length=1)
    source_request: str = Field(min_length=1)
    requirements: list[str] = Field(min_length=1)
    acceptance_criteria: list[AcceptanceCriterion] = Field(min_length=1)
    product_definition: ProductDefinition | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    constraints: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    confirmed: bool = False

    @field_validator("requirements", "constraints", "assumptions", "open_questions")
    @classmethod
    def require_clean_unique_items(cls, values: list[str]) -> list[str]:
        """Reject blank or duplicate list entries."""
        cleaned = [value.strip() for value in values]
        if any(not value for value in cleaned):
            raise ValueError("task brief list entries must not be blank")
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("task brief list entries must be unique")
        return cleaned

    @model_validator(mode="after")
    def validate_confirmation(self) -> Self:
        """A confirmed brief cannot retain unresolved questions."""
        criterion_ids = [criterion.id for criterion in self.acceptance_criteria]
        if len(criterion_ids) != len(set(criterion_ids)):
            raise ValueError("acceptance criterion IDs must be unique")
        if self.confirmed and self.open_questions:
            raise ValueError("a confirmed task brief cannot contain open questions")
        return self
