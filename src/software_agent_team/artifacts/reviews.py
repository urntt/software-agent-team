"""Independent review reports, boundary checks, and specialized security/UX assessments.

This module provides data models and validation functions for code reviews,
specialized threat-surface and usability assessments, finding collections,
and acceptance resolution.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

from .envelope import IterationArtifact
from .evidence import ReviewToolEvidenceReference
from .implementation import CriterionResult, TestReport
from .types import (
    AGENT_ID_PATTERN,
    COMMIT_PATTERN,
    ArtifactKind,
    CheckStatus,
    ReviewBoundaryKind,
    ReviewCriterionStatus,
    ReviewSeverity,
    ReviewTerminationReason,
    ReviewVerdict,
    _require_clean_unique_items,
    _require_safe_relative_path,
)


class ReviewFinding(BaseModel):
    """One attributable independent-review finding."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, pattern=r"^FINDING_[A-Z0-9_]+$")
    severity: ReviewSeverity = Field(
        description=(
            "Product impact. Critical severity alone does not make a finding "
            "terminal when a Developer revision can correct it."
        )
    )
    blocking: bool
    category: str = Field(min_length=1)
    description: str = Field(min_length=1)
    recommendation: str = Field(min_length=1)
    path: str | None = None
    line: int | None = Field(default=None, ge=1)
    criterion_ids: tuple[str, ...] = ()

    @field_validator("path")
    @classmethod
    def require_safe_finding_path(cls, value: str | None) -> str | None:
        """Keep optional source locations repository-relative."""
        return None if value is None else _require_safe_relative_path(value)

    @field_validator("criterion_ids")
    @classmethod
    def require_clean_criterion_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Reject duplicate criterion references."""
        return _require_clean_unique_items(values)

    @model_validator(mode="after")
    def validate_finding(self) -> Self:
        """Keep locations and critical severity semantically coherent."""
        if self.line is not None and self.path is None:
            raise ValueError("a finding line requires a source path")
        if self.severity is ReviewSeverity.CRITICAL and not self.blocking:
            raise ValueError("critical review findings must be blocking")
        return self


class ReviewBoundaryCheck(BaseModel):
    """Controller-grounded evidence for one required Review entry boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    boundary: ReviewBoundaryKind
    adversarial_check: str = Field(min_length=1, max_length=1000)
    command_evidence_ids: tuple[str, ...] = ()
    tool_evidence: tuple[ReviewToolEvidenceReference, ...] = ()

    @field_validator("adversarial_check")
    @classmethod
    def require_clean_adversarial_check(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("boundary adversarial check must not be blank")
        return cleaned

    @field_validator("command_evidence_ids")
    @classmethod
    def require_unique_command_evidence(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        if len(values) != len(set(values)) or any(
            re.fullmatch(r"CHECK_[A-Z0-9_]+", value) is None for value in values
        ):
            raise ValueError("boundary command-evidence references must be valid")
        return values

    @field_validator("tool_evidence")
    @classmethod
    def require_unique_tool_evidence(
        cls,
        values: tuple[ReviewToolEvidenceReference, ...],
    ) -> tuple[ReviewToolEvidenceReference, ...]:
        identifiers = [
            (value.execution_attempt, value.tool_call_id) for value in values
        ]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("boundary tool-evidence references must be unique")
        return values

    @model_validator(mode="after")
    def require_grounded_evidence(self) -> Self:
        """Allow command-only boundaries while rejecting an ungrounded check."""
        if not self.command_evidence_ids and not self.tool_evidence:
            raise ValueError(
                "boundary checks require command or tool evidence references"
            )
        return self

    @model_serializer(mode="wrap")
    def omit_empty_evidence(
        self,
        handler: SerializerFunctionWrapHandler,
    ) -> object:
        serialized = handler(self)
        if isinstance(serialized, dict):
            if not self.command_evidence_ids:
                serialized.pop("command_evidence_ids", None)
            if not self.tool_evidence:
                serialized.pop("tool_evidence", None)
        return serialized


class ReviewCriterionAssessment(BaseModel):
    """Attributable evidence and adversarial reasoning for one criterion."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    criterion_id: str = Field(min_length=1, pattern=r"^[A-Z][A-Z0-9_-]*$")
    status: ReviewCriterionStatus
    adversarial_check: str = Field(min_length=1, max_length=2000)
    evidence: str = Field(min_length=1, max_length=2000)
    command_evidence_ids: tuple[str, ...] = ()
    tool_evidence: tuple[ReviewToolEvidenceReference, ...] = ()
    boundary_checks: tuple[ReviewBoundaryCheck, ...] = ()

    @field_validator("adversarial_check", "evidence")
    @classmethod
    def require_clean_assessment_text(cls, value: str) -> str:
        """Reject empty presentation-only evidence."""
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("criterion assessment text must not be blank")
        return cleaned

    @field_validator("command_evidence_ids")
    @classmethod
    def require_unique_command_evidence(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        """Keep controller command references explicit and unambiguous."""
        if len(values) != len(set(values)) or any(
            re.fullmatch(r"CHECK_[A-Z0-9_]+", value) is None for value in values
        ):
            raise ValueError(
                "criterion command-evidence references must be valid and unique"
            )
        return values

    @field_validator("tool_evidence")
    @classmethod
    def require_unique_tool_evidence(
        cls,
        values: tuple[ReviewToolEvidenceReference, ...],
    ) -> tuple[ReviewToolEvidenceReference, ...]:
        """Keep controller tool references unambiguous within one criterion."""
        identifiers = [
            (value.execution_attempt, value.tool_call_id) for value in values
        ]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("criterion tool-evidence references must be unique")
        return values

    @field_validator("boundary_checks")
    @classmethod
    def require_unique_boundary_checks(
        cls,
        values: tuple[ReviewBoundaryCheck, ...],
    ) -> tuple[ReviewBoundaryCheck, ...]:
        boundaries = [value.boundary for value in values]
        if len(boundaries) != len(set(boundaries)):
            raise ValueError("criterion boundary checks must be unique")
        return values

    @model_serializer(mode="wrap")
    def omit_empty_command_evidence(
        self,
        handler: SerializerFunctionWrapHandler,
    ) -> object:
        """Keep pre-v3 Review artifacts byte-stable when no command is bound."""
        serialized = handler(self)
        if isinstance(serialized, dict) and not self.command_evidence_ids:
            serialized.pop("command_evidence_ids", None)
        if isinstance(serialized, dict) and not self.boundary_checks:
            serialized.pop("boundary_checks", None)
        return serialized


class ReviewReport(IterationArtifact):
    """Independent semantic review of one immutable implementation commit."""

    kind: Literal[ArtifactKind.REVIEW_REPORT] = ArtifactKind.REVIEW_REPORT
    producer: str = Field(default="reviewer", pattern=AGENT_ID_PATTERN)
    input_commit: str = Field(pattern=COMMIT_PATTERN)
    verdict: ReviewVerdict = Field(
        description=(
            "Use revise for every correctable implementation defect, including "
            "failed acceptance gates; fail is reserved for a terminal safety or "
            "evidence-integrity boundary."
        )
    )
    termination_reason: ReviewTerminationReason | None = Field(
        default=None,
        description=(
            "Required only with fail. It records the terminal boundary that makes "
            "another implementation revision unsafe."
        ),
    )
    reviewed_criteria: tuple[str, ...] = ()
    criterion_assessments: tuple[ReviewCriterionAssessment, ...] = ()
    findings: tuple[ReviewFinding, ...] = ()
    summary: str = Field(min_length=1)

    @field_validator("reviewed_criteria")
    @classmethod
    def require_clean_reviewed_criteria(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        """Keep the independent review scope explicit and attributable."""
        return _require_clean_unique_items(values)

    @model_validator(mode="after")
    def validate_verdict(self) -> Self:
        """Tie accept, revise, and fail recommendations to findings."""
        finding_ids = [finding.id for finding in self.findings]
        if len(finding_ids) != len(set(finding_ids)):
            raise ValueError("review finding IDs must be unique")
        assessment_ids = [
            assessment.criterion_id for assessment in self.criterion_assessments
        ]
        if len(assessment_ids) != len(set(assessment_ids)):
            raise ValueError("review criterion assessments must be unique")
        if self.criterion_assessments and set(assessment_ids) != set(
            self.reviewed_criteria
        ):
            raise ValueError(
                "review criterion assessments must exactly cover review scope"
            )
        blocking = [finding for finding in self.findings if finding.blocking]
        blocked_criteria = {
            assessment.criterion_id
            for assessment in self.criterion_assessments
            if assessment.status is ReviewCriterionStatus.BLOCKED
        }
        finding_blocked_criteria = {
            criterion_id
            for finding in blocking
            for criterion_id in finding.criterion_ids
        }
        if self.criterion_assessments and blocked_criteria != finding_blocked_criteria:
            raise ValueError(
                "blocked criterion assessments must exactly match blocking findings"
            )
        critical = [
            finding
            for finding in self.findings
            if finding.severity is ReviewSeverity.CRITICAL
        ]
        if (
            self.verdict is ReviewVerdict.ACCEPT
            and self.criterion_assessments
            and blocked_criteria
        ):
            raise ValueError("accepted reviews require every criterion to be satisfied")
        if (
            self.verdict in {ReviewVerdict.REVISE, ReviewVerdict.FAIL}
            and self.criterion_assessments
            and not blocked_criteria
        ):
            raise ValueError("non-accepted reviews require a blocked criterion")
        if self.verdict is ReviewVerdict.ACCEPT and blocking:
            raise ValueError("accepted reviews cannot contain blocking findings")
        if self.verdict is ReviewVerdict.REVISE and not blocking:
            raise ValueError("revision requires a blocking review finding")
        if self.verdict is ReviewVerdict.FAIL and not critical:
            raise ValueError("failed reviews require a critical finding")
        if (
            self.verdict is not ReviewVerdict.FAIL
            and self.termination_reason is not None
        ):
            raise ValueError("only failed reviews may declare a terminal reason")
        return self


class SecuritySurfaceAssessment(BaseModel):
    """One criterion-bound security surface evaluated by a specialist."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^SECURITY_[A-Z0-9_]+$")
    surface: str = Field(min_length=1, max_length=1000)
    threat: str = Field(min_length=1, max_length=1000)
    control: str = Field(min_length=1, max_length=1000)
    status: ReviewCriterionStatus
    evidence: str = Field(min_length=1, max_length=2000)
    criterion_ids: tuple[str, ...] = Field(min_length=1)

    @field_validator("surface", "threat", "control", "evidence")
    @classmethod
    def require_clean_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("security assessment text must not be blank")
        return cleaned

    @field_validator("criterion_ids")
    @classmethod
    def require_clean_criteria(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _require_clean_unique_items(values)


class ExperienceWorkflowAssessment(BaseModel):
    """One criterion-bound end-user workflow evaluated by a specialist."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^EXPERIENCE_[A-Z0-9_]+$")
    actor: str = Field(min_length=1, max_length=500)
    workflow: str = Field(min_length=1, max_length=1000)
    outcome: str = Field(min_length=1, max_length=1000)
    friction: str = Field(min_length=1, max_length=1000)
    recovery: str = Field(min_length=1, max_length=1000)
    status: ReviewCriterionStatus
    evidence: str = Field(min_length=1, max_length=2000)
    criterion_ids: tuple[str, ...] = Field(min_length=1)

    @field_validator(
        "actor",
        "workflow",
        "outcome",
        "friction",
        "recovery",
        "evidence",
    )
    @classmethod
    def require_clean_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("experience assessment text must not be blank")
        return cleaned

    @field_validator("criterion_ids")
    @classmethod
    def require_clean_criteria(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _require_clean_unique_items(values)


def _validate_specialized_assessment_scope(
    *,
    item_ids: tuple[str, ...],
    item_statuses: tuple[ReviewCriterionStatus, ...],
    item_criterion_ids: tuple[tuple[str, ...], ...],
    reviewed_criteria: tuple[str, ...],
    verdict: ReviewVerdict,
    label: str,
) -> None:
    """Keep one specialized assessment attributable to its approved scope."""
    if len(item_ids) != len(set(item_ids)):
        raise ValueError(f"{label} IDs must be unique")
    referenced = {
        criterion_id
        for criterion_ids in item_criterion_ids
        for criterion_id in criterion_ids
    }
    if not referenced.issubset(set(reviewed_criteria)):
        raise ValueError(f"{label} references criteria outside its review scope")
    blocked = any(status is ReviewCriterionStatus.BLOCKED for status in item_statuses)
    if verdict is ReviewVerdict.ACCEPT and blocked:
        raise ValueError(f"accepted {label} requires every specialist check to pass")
    if verdict is not ReviewVerdict.ACCEPT and not blocked:
        raise ValueError(f"non-accepted {label} requires a blocked specialist check")


class SecurityAssessment(ReviewReport):
    """Independent security assessment with explicit threat-surface semantics."""

    kind: Literal[ArtifactKind.SECURITY_ASSESSMENT] = ArtifactKind.SECURITY_ASSESSMENT
    surfaces: tuple[SecuritySurfaceAssessment, ...] = Field(min_length=1)
    residual_risks: tuple[str, ...] = ()

    @field_validator("residual_risks")
    @classmethod
    def require_clean_residual_risks(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        return _require_clean_unique_items(values)

    @model_validator(mode="after")
    def validate_specialized_scope(self) -> Self:
        _validate_specialized_assessment_scope(
            item_ids=tuple(item.id for item in self.surfaces),
            item_statuses=tuple(item.status for item in self.surfaces),
            item_criterion_ids=tuple(item.criterion_ids for item in self.surfaces),
            reviewed_criteria=self.reviewed_criteria,
            verdict=self.verdict,
            label="security assessment",
        )
        return self


class ExperienceAssessment(ReviewReport):
    """Independent assessment of an approved end-user workflow."""

    kind: Literal[ArtifactKind.EXPERIENCE_ASSESSMENT] = (
        ArtifactKind.EXPERIENCE_ASSESSMENT
    )
    workflows: tuple[ExperienceWorkflowAssessment, ...] = Field(min_length=1)
    usability_risks: tuple[str, ...] = ()

    @field_validator("usability_risks")
    @classmethod
    def require_clean_usability_risks(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        return _require_clean_unique_items(values)

    @model_validator(mode="after")
    def validate_specialized_scope(self) -> Self:
        _validate_specialized_assessment_scope(
            item_ids=tuple(item.id for item in self.workflows),
            item_statuses=tuple(item.status for item in self.workflows),
            item_criterion_ids=tuple(item.criterion_ids for item in self.workflows),
            reviewed_criteria=self.reviewed_criteria,
            verdict=self.verdict,
            label="experience assessment",
        )
        return self


def collect_unresolved_review_findings(
    reviews: Iterable[ReviewReport],
) -> tuple[str, ...]:
    """Carry every non-blocking Review finding into terminal evidence.

    Review artifacts have no explicit resolution signal for non-blocking
    findings. A later Review omitting one therefore cannot prove that it was
    fixed. Preserve the first observed description across completed iterations
    and collapse only exact repeats.
    """
    return tuple(
        dict.fromkeys(
            finding.description.strip()
            for review in reviews
            for finding in review.findings
            if not finding.blocking and finding.description.strip()
        )
    )


def resolve_acceptance_results(
    test: TestReport,
    reviews: ReviewReport | tuple[ReviewReport, ...],
) -> tuple[CriterionResult, ...]:
    """Resolve manual criteria when accepted independent reviews cover them."""
    normalized_reviews = (reviews,) if isinstance(reviews, ReviewReport) else reviews
    manual = set(test.manual_review_criteria)
    reviewed = {
        criterion_id
        for review in normalized_reviews
        for criterion_id in review.reviewed_criteria
    }
    if reviewed != manual:
        raise ValueError("independent reviews must exactly cover manual criteria")
    if any(review.verdict is not ReviewVerdict.ACCEPT for review in normalized_reviews):
        raise ValueError("manual acceptance requires every review to be accepted")
    results: list[CriterionResult] = []
    for criterion in test.criteria:
        if criterion.criterion_id not in manual:
            results.append(criterion)
            continue
        if criterion.status is not CheckStatus.PENDING_REVIEW:
            raise ValueError("manual criterion is not pending independent review")
        results.append(
            criterion.model_copy(
                update={
                    "status": CheckStatus.PASSED,
                    "detail": (
                        f"{criterion.detail} Independent review accepted the "
                        "assigned manual criterion on the same immutable commit."
                    ),
                }
            )
        )
    return tuple(results)
