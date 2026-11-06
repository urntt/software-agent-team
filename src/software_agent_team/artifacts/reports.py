"""Controller-owned iteration records, final reports, and validation dispatch.

This module provides data models for iteration decision records, final delivery
and failure reports, top-level phase artifact parsing, and frozen run-context
verification against approved TeamPlans.
"""

from __future__ import annotations

import re
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from software_agent_team.versioning import SoftwareVersionReport

from .envelope import (
    ArtifactReference,
    HandoffEnvelope,
    IterationArtifact,
    PhaseArtifact,
)
from .evidence import AgentExecutionRecord
from .implementation import (
    CriterionResult,
    ImplementationPlan,
    TestReport,
    WorkResult,
)
from .reviews import (
    ExperienceAssessment,
    ReviewReport,
    SecurityAssessment,
)
from .task_brief import TaskBrief
from .types import (
    COMMIT_PATTERN,
    REVIEW_ARTIFACT_KINDS,
    ArtifactKind,
    CheckStatus,
    FinalStatus,
    IterationDecision,
    _require_clean_unique_items,
)


class IterationRecord(IterationArtifact):
    """Controller-owned decision record for one implementation iteration."""

    kind: Literal[ArtifactKind.ITERATION_RECORD] = ArtifactKind.ITERATION_RECORD
    producer: Literal["controller"] = "controller"
    input_commit: str = Field(pattern=COMMIT_PATTERN)
    output_commit: str = Field(pattern=COMMIT_PATTERN)
    implementation_plan: ArtifactReference | None = None
    implementation_plan_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    work_results: tuple[ArtifactReference, ...] = Field(min_length=1)
    test_reports: tuple[ArtifactReference, ...] = Field(min_length=1)
    review_reports: tuple[ArtifactReference, ...] = ()
    decision: IterationDecision
    blocking_finding_ids: tuple[str, ...] = ()
    blocking_reasons: tuple[str, ...] = ()
    resolved_finding_ids: tuple[str, ...] = ()
    summary: str = Field(min_length=1)

    @field_validator(
        "blocking_finding_ids",
        "blocking_reasons",
        "resolved_finding_ids",
    )
    @classmethod
    def require_clean_finding_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Reject duplicate finding references."""
        return _require_clean_unique_items(values)

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        """Require exact artifact kinds and evidence for revision or failure."""
        if (
            self.implementation_plan is not None
            and self.implementation_plan_sha256 is not None
        ):
            raise ValueError(
                "iteration record cannot bind both a plan artifact and digest"
            )
        if (
            self.implementation_plan is not None
            and self.implementation_plan.kind is not ArtifactKind.IMPLEMENTATION_PLAN
        ):
            raise ValueError("implementation_plan must reference implementation_plan")
        expected_collections = {
            "work_results": ArtifactKind.WORK_RESULT,
            "test_reports": ArtifactKind.TEST_REPORT,
        }
        for field, expected in expected_collections.items():
            references = getattr(self, field)
            if any(reference.kind is not expected for reference in references):
                raise ValueError(f"{field} must reference only {expected.value}")
            paths = [reference.path for reference in references]
            if len(paths) != len(set(paths)):
                raise ValueError(f"{field} references must be unique")
        if any(
            reference.kind not in REVIEW_ARTIFACT_KINDS
            for reference in self.review_reports
        ):
            raise ValueError("review_reports must reference only quality assessments")
        review_paths = [reference.path for reference in self.review_reports]
        if len(review_paths) != len(set(review_paths)):
            raise ValueError("review_reports references must be unique")
        if self.input_commit == self.output_commit:
            raise ValueError("iteration output commit must differ from input")
        has_blocker = bool(self.blocking_finding_ids or self.blocking_reasons)
        if self.decision is IterationDecision.ACCEPT and has_blocker:
            raise ValueError("accepted iterations cannot retain blockers")
        if self.decision is not IterationDecision.ACCEPT and not has_blocker:
            raise ValueError("revision and failure require blocking evidence")
        overlap = set(self.blocking_finding_ids) & set(self.resolved_finding_ids)
        if overlap:
            raise ValueError("a finding cannot be both blocking and resolved")
        return self


class FinalReport(PhaseArtifact):
    """Controller-owned final delivery or failure report."""

    kind: Literal[ArtifactKind.FINAL_REPORT] = ArtifactKind.FINAL_REPORT
    producer: Literal["controller"] = "controller"
    software_version: SoftwareVersionReport | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    status: FinalStatus
    termination_reason: str = Field(min_length=1)
    final_commit: str | None = Field(default=None, pattern=COMMIT_PATTERN)
    iterations: tuple[ArtifactReference, ...] = ()
    acceptance_results: tuple[CriterionResult, ...] = ()
    unresolved_findings: tuple[str, ...] = ()
    known_limitations: tuple[str, ...] = ()
    summary: str = Field(min_length=1)

    @field_validator("unresolved_findings", "known_limitations")
    @classmethod
    def require_clean_unique_text(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Reject blank and duplicate final-report statements."""
        return _require_clean_unique_items(values)

    @model_validator(mode="after")
    def validate_final_report(self) -> Self:
        """Tie a completed report to commit and acceptance evidence."""
        if any(
            reference.kind is not ArtifactKind.ITERATION_RECORD
            for reference in self.iterations
        ):
            raise ValueError("final report iterations must reference iteration records")
        paths = [reference.path for reference in self.iterations]
        if len(paths) != len(set(paths)):
            raise ValueError("final report iteration references must be unique")

        criterion_ids = [result.criterion_id for result in self.acceptance_results]
        if len(criterion_ids) != len(set(criterion_ids)):
            raise ValueError("final acceptance criterion IDs must be unique")
        if self.status is FinalStatus.COMPLETED:
            if self.final_commit is None:
                raise ValueError("completed final reports require a commit")
            if not self.iterations or not self.acceptance_results:
                raise ValueError(
                    "completed final reports require iteration and acceptance evidence"
                )
            if any(
                result.status is not CheckStatus.PASSED
                for result in self.acceptance_results
            ):
                raise ValueError("completed final reports require passed acceptance")
        return self


type PersistedArtifact = (
    ImplementationPlan
    | WorkResult
    | TestReport
    | ReviewReport
    | SecurityAssessment
    | ExperienceAssessment
    | IterationRecord
    | FinalReport
    | HandoffEnvelope
    | AgentExecutionRecord
)


ARTIFACT_MODELS: dict[ArtifactKind, type[PersistedArtifact]] = {
    ArtifactKind.IMPLEMENTATION_PLAN: ImplementationPlan,
    ArtifactKind.WORK_RESULT: WorkResult,
    ArtifactKind.TEST_REPORT: TestReport,
    ArtifactKind.REVIEW_REPORT: ReviewReport,
    ArtifactKind.SECURITY_ASSESSMENT: SecurityAssessment,
    ArtifactKind.EXPERIENCE_ASSESSMENT: ExperienceAssessment,
    ArtifactKind.ITERATION_RECORD: IterationRecord,
    ArtifactKind.FINAL_REPORT: FinalReport,
    ArtifactKind.HANDOFF_ENVELOPE: HandoffEnvelope,
    ArtifactKind.AGENT_EXECUTION_RECORD: AgentExecutionRecord,
}


def parse_phase_artifact(payload: object) -> PersistedArtifact:
    """Dispatch an untrusted JSON payload to its one declared artifact model."""
    if not isinstance(payload, dict):
        raise ValueError("phase artifact must be a JSON object")
    try:
        kind = ArtifactKind(payload["kind"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("phase artifact requires a supported kind") from error
    model = ARTIFACT_MODELS.get(kind)
    if model is None:
        raise ValueError(f"unsupported phase artifact kind: {kind.value}")
    return model.model_validate(payload)


def validate_artifact_context(
    artifact: PersistedArtifact,
    *,
    task_brief: TaskBrief,
    team_id: str,
    team_agents: dict[str, str],
    team_specializations: dict[str, str] | None = None,
    iteration_limit: int,
    team_stages: dict[str, set[str]] | None = None,
) -> None:
    """Validate one phase artifact against its frozen run inputs."""
    if artifact.run_id != task_brief.run_id:
        raise ValueError("artifact run ID does not match the task brief")
    if artifact.team_id != team_id:
        raise ValueError("artifact team ID does not match the selected team")
    if (
        isinstance(artifact, (IterationArtifact, HandoffEnvelope, AgentExecutionRecord))
        and artifact.iteration > iteration_limit
    ):
        raise ValueError("artifact iteration exceeds the run iteration limit")
    producer = getattr(artifact, "producer", None)
    if (
        producer is not None
        and producer != "controller"
        and producer not in team_agents
    ):
        raise ValueError("artifact producer is not part of the approved TeamPlan")

    if isinstance(artifact, (HandoffEnvelope, AgentExecutionRecord)):
        if team_stages is None:
            raise ValueError("Agent runtime artifacts require team stage context")
        stage_agents = team_stages.get(artifact.stage)
        if stage_agents is None:
            raise ValueError("artifact stage is not part of the selected team")
        agent_id = (
            artifact.source_agent_id
            if isinstance(artifact, HandoffEnvelope)
            else artifact.agent_id
        )
        if agent_id not in team_agents:
            raise ValueError("artifact Agent is not part of the approved TeamPlan")
        if agent_id not in stage_agents:
            raise ValueError("artifact Agent is not assigned to its declared stage")
        if (
            isinstance(artifact, AgentExecutionRecord)
            and artifact.capability != team_agents[agent_id]
        ):
            raise ValueError("execution capability differs from its approved AgentSpec")
        if (
            isinstance(artifact, AgentExecutionRecord)
            and team_specializations is not None
        ):
            expected_specialization = team_specializations.get(agent_id)
            if artifact.schema_version >= 13 and artifact.specialization is None:
                raise ValueError("current execution record requires specialization")
            if (
                artifact.specialization is not None
                and artifact.specialization != expected_specialization
            ):
                raise ValueError(
                    "execution specialization differs from its approved AgentSpec"
                )

    if isinstance(artifact, HandoffEnvelope):
        if artifact.created_at is None:
            raise ValueError("persisted handoffs require a creation timestamp")
        if (
            artifact.target_agent_id is not None
            and artifact.target_agent_id not in team_agents
        ):
            raise ValueError("handoff target is not part of the approved TeamPlan")
        if artifact.input_commit is None:
            raise ValueError("persisted handoffs require an input commit")
        if re.fullmatch(COMMIT_PATTERN, artifact.input_commit) is None:
            raise ValueError("persisted handoffs require a full commit ID")

    expected_criteria = {criterion.id for criterion in task_brief.acceptance_criteria}
    referenced_criteria: set[str] = set()
    if isinstance(artifact, ImplementationPlan):
        for task in artifact.tasks:
            owner = task.owner.value
            if team_agents.get(owner) not in {"implementation", "integration"}:
                raise ValueError(
                    "plan task owner is not an approved implementation Agent"
                )
            referenced_criteria.update(task.acceptance_criteria)
        if referenced_criteria != expected_criteria:
            missing = sorted(expected_criteria - referenced_criteria)
            unknown = sorted(referenced_criteria - expected_criteria)
            details = []
            if missing:
                details.append(f"missing: {', '.join(missing)}")
            if unknown:
                details.append(f"unknown: {', '.join(unknown)}")
            raise ValueError(
                "implementation plan acceptance coverage differs from the task "
                f"brief ({'; '.join(details)})"
            )
    elif isinstance(artifact, WorkResult):
        if team_agents.get(artifact.producer) not in {
            "implementation",
            "integration",
        }:
            raise ValueError("work-result producer lacks implementation capability")
    elif isinstance(artifact, TestReport):
        if artifact.producer != "controller" and (
            team_agents.get(artifact.producer) != "testing"
        ):
            raise ValueError("test-report producer lacks testing capability")
        referenced_criteria = {
            criterion.criterion_id for criterion in artifact.criteria
        }
        if referenced_criteria != expected_criteria:
            raise ValueError(
                "test report must cover every confirmed acceptance criterion"
            )
        if not set(artifact.manual_review_criteria).issubset(expected_criteria):
            raise ValueError("test report references an unknown manual criterion")
    elif isinstance(artifact, ReviewReport):
        if team_agents.get(artifact.producer) != "review":
            raise ValueError("review-report producer lacks review capability")
        referenced_criteria = {
            criterion_id
            for finding in artifact.findings
            for criterion_id in finding.criterion_ids
        }
        referenced_criteria.update(artifact.reviewed_criteria)
        if isinstance(artifact, SecurityAssessment):
            referenced_criteria.update(
                criterion_id
                for surface in artifact.surfaces
                for criterion_id in surface.criterion_ids
            )
        elif isinstance(artifact, ExperienceAssessment):
            referenced_criteria.update(
                criterion_id
                for workflow in artifact.workflows
                for criterion_id in workflow.criterion_ids
            )
        if not referenced_criteria.issubset(expected_criteria):
            raise ValueError("review report references an unknown criterion")
    elif isinstance(artifact, FinalReport):
        referenced_criteria = {
            result.criterion_id for result in artifact.acceptance_results
        }
        if artifact.status is FinalStatus.COMPLETED:
            if referenced_criteria != expected_criteria:
                raise ValueError(
                    "completed final report must cover every acceptance criterion"
                )
        elif not referenced_criteria.issubset(expected_criteria):
            raise ValueError("final report references an unknown criterion")
