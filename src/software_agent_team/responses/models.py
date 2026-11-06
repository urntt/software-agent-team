"""Pydantic semantic response models and controller-bound response bodies."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, field_validator

from software_agent_team.artifacts import (
    ArtifactKind,
    ExperienceWorkflowAssessment,
    PlanTask,
    ReviewBoundaryKind,
    ReviewCriterionAssessment,
    ReviewCriterionStatus,
    ReviewFinding,
    ReviewTerminationReason,
    ReviewVerdict,
    SecuritySurfaceAssessment,
)


def _clean_unique_text(values: tuple[str, ...]) -> tuple[str, ...]:
    cleaned = tuple(value.strip() for value in values)
    if any(not value for value in cleaned):
        raise ValueError("response text values must not be blank")
    if len(cleaned) != len(set(cleaned)):
        raise ValueError("response text values must be unique")
    return cleaned


class ImplementationPlanResponse(BaseModel):
    """Planner reasoning; the controller supplies the artifact envelope."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    objective: str = Field(min_length=1)
    approach: tuple[str, ...] = Field(min_length=1)
    tasks: tuple[PlanTask, ...] = Field(min_length=1)
    risks: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()

    @field_validator("objective")
    @classmethod
    def require_clean_objective(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("objective must not be blank")
        return cleaned

    @field_validator("approach", "risks", "assumptions")
    @classmethod
    def require_clean_unique_text(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _clean_unique_text(values)


class WorkResultResponse(BaseModel):
    """Developer-authored semantic result; Git facts remain controller-owned."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: str = Field(min_length=1)
    completed_tasks: tuple[str, ...] = Field(min_length=1)
    unresolved_issues: tuple[str, ...] = Field(
        default=(),
        description=(
            "Currently open defects or constraints blocking assigned work or "
            "the product; record resolved checks and nonblocking environment "
            "observations in summary instead."
        ),
    )

    @field_validator("summary")
    @classmethod
    def require_clean_summary(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("summary must not be blank")
        return cleaned

    @field_validator("completed_tasks", "unresolved_issues")
    @classmethod
    def require_clean_unique_text(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _clean_unique_text(values)


class TestReportResponse(BaseModel):
    """Tester analysis; deterministic statuses and evidence are controller-owned."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    findings: tuple[str, ...] = ()
    summary: str = Field(min_length=1)

    @field_validator("findings")
    @classmethod
    def require_clean_unique_findings(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        return _clean_unique_text(values)

    @field_validator("summary")
    @classmethod
    def require_clean_summary(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("summary must not be blank")
        return cleaned


class ReviewToolEvidenceClaim(BaseModel):
    """Model-visible exact fragment used to select current tool results."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    observable: str = Field(min_length=1, max_length=256)

    @field_validator("observable")
    @classmethod
    def require_clean_observable(cls, value: str) -> str:
        """Keep semantic selectors small, exact, and text-safe."""

        cleaned = value.strip()
        if not cleaned or "\x00" in cleaned:
            raise ValueError("tool evidence observables must be nonblank text")
        return cleaned


class ReviewBoundaryCheckResponse(BaseModel):
    """Model-visible evidence selector for one required entry boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    boundary: ReviewBoundaryKind
    adversarial_check: str = Field(min_length=1, max_length=1000)
    tool_evidence: tuple[ReviewToolEvidenceClaim, ...] = Field(
        min_length=1, max_length=1
    )

    @field_validator("adversarial_check")
    @classmethod
    def require_clean_adversarial_check(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("boundary adversarial check must not be blank")
        return cleaned


class ReviewCriterionAssessmentResponse(BaseModel):
    """Reviewer assessment with model-visible result-fragment selectors."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    criterion_id: str = Field(min_length=1, pattern=r"^[A-Z][A-Z0-9_-]*$")
    status: ReviewCriterionStatus
    adversarial_check: str = Field(min_length=1, max_length=2000)
    evidence: str = Field(min_length=1, max_length=2000)
    tool_evidence: tuple[ReviewToolEvidenceClaim, ...] = Field(min_length=1)
    boundary_checks: tuple[ReviewBoundaryCheckResponse, ...] = ()

    @field_validator("adversarial_check", "evidence")
    @classmethod
    def require_clean_assessment_text(cls, value: str) -> str:
        """Reject empty presentation-only assessment text."""

        cleaned = value.strip()
        if not cleaned:
            raise ValueError("criterion assessment text must not be blank")
        return cleaned

    @field_validator("boundary_checks")
    @classmethod
    def require_unique_boundary_checks_and_observables(
        cls,
        values: tuple[ReviewBoundaryCheckResponse, ...],
    ) -> tuple[ReviewBoundaryCheckResponse, ...]:
        boundaries = [value.boundary for value in values]
        if len(boundaries) != len(set(boundaries)):
            raise ValueError("criterion boundary checks must be unique")
        observables = [
            claim.observable for value in values for claim in value.tool_evidence
        ]
        if len(observables) != len(set(observables)):
            raise ValueError(
                "criterion boundary checks require distinct evidence fragments"
            )
        return values


class ReviewReportResponse(BaseModel):
    """Reviewer's semantic verdict; immutable commit and scope are controller-owned."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    verdict: ReviewVerdict = Field(
        description=(
            "Use revise for correctable defects. Use fail only when continuing "
            "would cross a safety or evidence-integrity boundary."
        )
    )
    termination_reason: ReviewTerminationReason | None = None
    criterion_assessments: tuple[ReviewCriterionAssessmentResponse, ...] = ()
    findings: tuple[ReviewFinding, ...] = ()
    summary: str = Field(min_length=1)

    @field_validator("summary")
    @classmethod
    def require_clean_summary(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("summary must not be blank")
        return cleaned


class GroundedReviewReportResponse(BaseModel):
    """Controller-resolved Review semantics ready for artifact assembly."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    verdict: ReviewVerdict
    termination_reason: ReviewTerminationReason | None = None
    criterion_assessments: tuple[ReviewCriterionAssessment, ...] = ()
    findings: tuple[ReviewFinding, ...] = ()
    summary: str = Field(min_length=1)

    @field_validator("summary")
    @classmethod
    def require_clean_summary(cls, value: str) -> str:
        """Reject whitespace-only controller-bound summaries."""

        cleaned = value.strip()
        if not cleaned:
            raise ValueError("summary must not be blank")
        return cleaned


class SecurityAssessmentResponse(ReviewReportResponse):
    """Security-specialist semantics beyond the shared review contract."""

    surfaces: tuple[SecuritySurfaceAssessment, ...] = Field(min_length=1)
    residual_risks: tuple[str, ...] = ()

    @field_validator("residual_risks")
    @classmethod
    def require_clean_residual_risks(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        return _clean_unique_text(values)


class GroundedSecurityAssessmentResponse(GroundedReviewReportResponse):
    """Security semantics after controller evidence binding."""

    surfaces: tuple[SecuritySurfaceAssessment, ...] = Field(min_length=1)
    residual_risks: tuple[str, ...] = ()


class ExperienceAssessmentResponse(ReviewReportResponse):
    """User-experience specialist semantics beyond shared review fields."""

    workflows: tuple[ExperienceWorkflowAssessment, ...] = Field(min_length=1)
    usability_risks: tuple[str, ...] = ()

    @field_validator("usability_risks")
    @classmethod
    def require_clean_usability_risks(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        return _clean_unique_text(values)


class GroundedExperienceAssessmentResponse(GroundedReviewReportResponse):
    """User-experience semantics after controller evidence binding."""

    workflows: tuple[ExperienceWorkflowAssessment, ...] = Field(min_length=1)
    usability_risks: tuple[str, ...] = ()


type AgentResponseBody = (
    ImplementationPlanResponse
    | WorkResultResponse
    | TestReportResponse
    | ReviewReportResponse
    | GroundedReviewReportResponse
    | SecurityAssessmentResponse
    | GroundedSecurityAssessmentResponse
    | ExperienceAssessmentResponse
    | GroundedExperienceAssessmentResponse
)


RESPONSE_BODY_MODELS: dict[ArtifactKind, type[AgentResponseBody]] = {
    ArtifactKind.IMPLEMENTATION_PLAN: ImplementationPlanResponse,
    ArtifactKind.WORK_RESULT: WorkResultResponse,
    ArtifactKind.TEST_REPORT: TestReportResponse,
    ArtifactKind.REVIEW_REPORT: ReviewReportResponse,
    ArtifactKind.SECURITY_ASSESSMENT: SecurityAssessmentResponse,
    ArtifactKind.EXPERIENCE_ASSESSMENT: ExperienceAssessmentResponse,
}

_COMMON_CONTROLLER_FIELDS = {
    "schema_version",
    "kind",
    "run_id",
    "team_id",
    "producer",
    "created_at",
    "iteration",
}
_CONTROLLER_FIELDS: dict[ArtifactKind, frozenset[str]] = {
    ArtifactKind.IMPLEMENTATION_PLAN: frozenset(_COMMON_CONTROLLER_FIELDS),
    ArtifactKind.WORK_RESULT: frozenset(
        _COMMON_CONTROLLER_FIELDS | {"input_commit", "output_commit", "changed_files"}
    ),
    ArtifactKind.TEST_REPORT: frozenset(
        _COMMON_CONTROLLER_FIELDS
        | {
            "input_commit",
            "status",
            "commands",
            "criteria",
            "manual_review_criteria",
            "blockers",
        }
    ),
    ArtifactKind.REVIEW_REPORT: frozenset(
        _COMMON_CONTROLLER_FIELDS | {"input_commit", "reviewed_criteria"}
    ),
    ArtifactKind.SECURITY_ASSESSMENT: frozenset(
        _COMMON_CONTROLLER_FIELDS | {"input_commit", "reviewed_criteria"}
    ),
    ArtifactKind.EXPERIENCE_ASSESSMENT: frozenset(
        _COMMON_CONTROLLER_FIELDS | {"input_commit", "reviewed_criteria"}
    ),
}
_PROTECTED_CONTROLLER_FIELD_NAMES = frozenset(
    {
        *(field for fields in _CONTROLLER_FIELDS.values() for field in fields),
        "command_evidence_ids",
        "execution_attempt",
        "tool_call_id",
    }
)


def controller_fields_for(kind: ArtifactKind) -> tuple[str, ...]:
    """Return fields that the controller binds for one response contract."""

    fields = _CONTROLLER_FIELDS.get(kind)
    if fields is None:
        raise ValueError(f"no Agent response contract exists for {kind.value}")
    return tuple(sorted(fields))


@dataclass(frozen=True)
class ParsedAgentResponse:
    """Validated semantic body plus ignored controller-owned response fields."""

    body: AgentResponseBody
    ignored_controller_fields: tuple[str, ...]
    semantic_payload: dict[str, object]
    response_normalizations: tuple[str, ...] = ()
