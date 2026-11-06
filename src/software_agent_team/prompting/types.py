"""Role prompt error definitions, input models, and boundary invariants.

This module provides core data structures and boundary constraints:
    - AgentPromptError: Raised on boundary or schema violations.
    - DynamicUpstreamResult, DynamicRevisionFeedback, DynamicUserGuidance:
      Typed upstream handoff and controller feedback records.
    - DynamicAgentPromptInputs, AgentPromptInputs:
      Validated runtime context inputs for prompt rendering.
    - TEMPLATE_ROOT, ROLE_TEMPLATES, DYNAMIC_CAPABILITY_TEMPLATES:
      Template filesystem paths and role-to-template mappings.
"""

from __future__ import annotations

from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from software_agent_team.artifacts import (
    COMMIT_PATTERN,
    IMPLEMENTATION_ROLES,
    AgentRole,
    ArtifactKind,
    CommandEvidence,
    HandoffStatus,
    ImplementationPlan,
    IterationRecord,
    ReviewFinding,
    ReviewReport,
    TaskBrief,
    TestReport,
    WorkResult,
    validate_artifact_context,
)
from software_agent_team.execution import validate_role_artifact_kind
from software_agent_team.integrity import canonical_model_sha256
from software_agent_team.planning import (
    AdaptiveImplementationPlan,
    ProposedTask,
    validate_task_agent_bindings,
    validate_task_criterion_references,
)
from software_agent_team.teams import (
    AgentCapability,
    AgentSpec,
    TeamPlan,
    capability_for_legacy_role,
)

# Prompt template root directory resolving to prompt_templates next to prompting package
TEMPLATE_ROOT = Path(__file__).resolve().parent.parent / "prompt_templates"

ROLE_TEMPLATES: dict[AgentRole, str] = {
    AgentRole.SINGLE_AGENT: "developer.md",
    AgentRole.PLANNER: "planner.md",
    AgentRole.GENERALIST_DEVELOPER: "developer.md",
    AgentRole.FRONTEND_DEVELOPER: "developer.md",
    AgentRole.BACKEND_DEVELOPER: "developer.md",
    AgentRole.INTEGRATOR: "developer.md",
    AgentRole.TESTER: "tester.md",
    AgentRole.REVIEWER: "reviewer.md",
}

DYNAMIC_CAPABILITY_TEMPLATES: dict[AgentCapability, str] = {
    AgentCapability.IMPLEMENTATION: "adaptive_developer.md",
    AgentCapability.INTEGRATION: "adaptive_developer.md",
    AgentCapability.TESTING: "adaptive_tester.md",
    AgentCapability.REVIEW: "adaptive_reviewer.md",
}

_COMMAND_PROMPT_EXCERPT_BUDGET = 12_000
_COMMAND_PROMPT_STREAM_LIMIT = 2_000


class AgentPromptError(ValueError):
    """Raised when a role prompt would cross its declared context boundary."""


class DynamicUpstreamResult(BaseModel):
    """Bounded typed handoff context from one completed dependency."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    status: HandoffStatus
    summary: str = Field(min_length=1, max_length=1000)
    output_commit: str | None = Field(default=None, pattern=COMMIT_PATTERN)
    completed_task_ids: tuple[str, ...] = ()

    @field_validator("summary")
    @classmethod
    def require_clean_summary(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("upstream result summary must not be blank")
        return cleaned

    @field_validator("completed_task_ids")
    @classmethod
    def require_unique_tasks(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(value.strip() for value in values)
        if any(not value for value in cleaned) or len(cleaned) != len(set(cleaned)):
            raise ValueError("completed task IDs must be non-empty and unique")
        return cleaned


class DynamicRevisionFeedback(BaseModel):
    """Controller-derived actionable evidence for one bounded revision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    previous_iteration: int = Field(ge=1)
    output_commit: str = Field(pattern=COMMIT_PATTERN)
    blocking_findings: tuple[ReviewFinding, ...] = ()
    blocking_reasons: tuple[str, ...] = ()
    summary: str = Field(min_length=1, max_length=2000)

    @field_validator("blocking_reasons")
    @classmethod
    def require_unique_reasons(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(value.strip() for value in values)
        if any(not value for value in cleaned) or len(cleaned) != len(set(cleaned)):
            raise ValueError("revision blocking reasons must be non-empty and unique")
        return cleaned

    @field_validator("summary")
    @classmethod
    def require_clean_summary(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("revision feedback summary must not be blank")
        return cleaned

    @model_validator(mode="after")
    def require_blocking_evidence(self) -> Self:
        finding_ids = [finding.id for finding in self.blocking_findings]
        if len(finding_ids) != len(set(finding_ids)):
            raise ValueError("revision feedback finding IDs must be unique")
        if any(not finding.blocking for finding in self.blocking_findings):
            raise ValueError("revision feedback may include only blocking findings")
        if not self.blocking_findings and not self.blocking_reasons:
            raise ValueError("revision feedback requires blocking evidence")
        return self


class DynamicUserGuidance(BaseModel):
    """One persisted user instruction authorized for prospective Agent work."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    command_id: str = Field(pattern=r"^ctl-[a-z0-9][a-z0-9-]*$")
    instruction: str = Field(min_length=1, max_length=2_000)

    @field_validator("instruction")
    @classmethod
    def require_clean_instruction(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned or "\x00" in cleaned:
            raise ValueError("dynamic user guidance must contain safe text")
        return cleaned


class DynamicAgentPromptInputs(BaseModel):
    """Validated minimum context for one approved run-scoped AgentSpec."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_brief: TaskBrief
    implementation_plan: AdaptiveImplementationPlan
    team_plan: TeamPlan
    agent_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    active_model_route_id: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    iteration: int = Field(ge=1)
    iteration_input_commit: str = Field(pattern=COMMIT_PATTERN)
    input_commit: str = Field(pattern=COMMIT_PATTERN)
    upstream_results: tuple[DynamicUpstreamResult, ...] = ()
    command_evidence: tuple[CommandEvidence, ...] = ()
    manual_review_criteria: tuple[str, ...] = ()
    revision_feedback: DynamicRevisionFeedback | None = None
    user_guidance: tuple[DynamicUserGuidance, ...] = ()

    @field_validator("upstream_results")
    @classmethod
    def require_unique_upstream_agents(
        cls,
        values: tuple[DynamicUpstreamResult, ...],
    ) -> tuple[DynamicUpstreamResult, ...]:
        ids = [item.agent_id for item in values]
        if len(ids) != len(set(ids)):
            raise ValueError("dynamic prompt cannot repeat an upstream Agent")
        return values

    @field_validator("command_evidence")
    @classmethod
    def require_unique_command_ids(
        cls,
        values: tuple[CommandEvidence, ...],
    ) -> tuple[CommandEvidence, ...]:
        ids = [item.id for item in values]
        if len(ids) != len(set(ids)):
            raise ValueError("dynamic prompt command IDs must be unique")
        return values

    @field_validator("manual_review_criteria")
    @classmethod
    def require_unique_manual_criteria(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(value.strip() for value in values)
        if any(not value for value in cleaned) or len(cleaned) != len(set(cleaned)):
            raise ValueError("manual criterion IDs must be non-empty and unique")
        return cleaned

    @field_validator("user_guidance")
    @classmethod
    def require_unique_guidance(
        cls,
        values: tuple[DynamicUserGuidance, ...],
    ) -> tuple[DynamicUserGuidance, ...]:
        command_ids = [value.command_id for value in values]
        if len(command_ids) != len(set(command_ids)):
            raise ValueError("dynamic prompt cannot repeat user guidance")
        return values

    @model_validator(mode="after")
    def validate_dynamic_context(self) -> Self:
        if not self.task_brief.confirmed:
            raise ValueError("dynamic Agent prompts require a confirmed TaskBrief")
        if self.team_plan.run_id != self.task_brief.run_id:
            raise ValueError("TeamPlan and TaskBrief run IDs differ")
        if self.implementation_plan.run_id != self.task_brief.run_id:
            raise ValueError("implementation plan and TaskBrief run IDs differ")
        if self.implementation_plan.team_id != self.team_plan.team_id:
            raise ValueError("implementation plan and TeamPlan team IDs differ")
        if canonical_model_sha256(self.task_brief) != self.team_plan.task_brief_sha256:
            raise ValueError("TeamPlan does not bind the supplied TaskBrief")
        if canonical_model_sha256(self.implementation_plan) != (
            self.team_plan.implementation_plan_sha256
        ):
            raise ValueError("TeamPlan does not bind the implementation plan")
        try:
            validate_task_criterion_references(
                self.implementation_plan.tasks,
                {criterion.id for criterion in self.task_brief.acceptance_criteria},
            )
            validate_task_agent_bindings(
                self.implementation_plan.tasks,
                {
                    plan_agent.id: plan_agent.dependencies
                    for plan_agent in self.team_plan.agents
                },
                {
                    plan_agent.id
                    for plan_agent in self.team_plan.agents
                    if plan_agent.capability
                    in {
                        AgentCapability.IMPLEMENTATION,
                        AgentCapability.INTEGRATION,
                    }
                },
                require_canonical_cross_agent_projection=(
                    self.implementation_plan.schema_version >= 16
                ),
            )
        except ValueError as error:
            raise ValueError(f"dynamic implementation plan {error}") from error
        if self.iteration > self.team_plan.iteration_limit:
            raise ValueError("dynamic prompt iteration exceeds the TeamPlan")
        if self.iteration == 1:
            if self.revision_feedback is not None:
                raise ValueError("initial dynamic iteration cannot receive feedback")
        elif self.revision_feedback is None:
            raise ValueError("dynamic revision requires prior blocking feedback")
        elif (
            self.revision_feedback.previous_iteration != self.iteration - 1
            or self.revision_feedback.output_commit != self.iteration_input_commit
        ):
            raise ValueError(
                "dynamic revision feedback does not match the iteration input commit"
            )
        agent = self.team_plan.get_agent(self.agent_id)
        route_id = self.active_model_route_id or agent.model_route_id
        if route_id not in self.team_plan.model_routes.authorized_route_ids(agent.id):
            raise ValueError("dynamic prompt model route is not authorized for Agent")
        if agent.capability not in DYNAMIC_CAPABILITY_TEMPLATES:
            raise ValueError("Agent capability has no dynamic prompt contract")
        upstream_ids = {item.agent_id for item in self.upstream_results}
        if upstream_ids != set(agent.dependencies):
            raise ValueError("dynamic prompt handoffs must exactly cover dependencies")
        if any(
            item.status is not HandoffStatus.COMPLETED for item in self.upstream_results
        ):
            raise ValueError("dynamic prompt dependencies must be completed")
        assigned = self.assigned_tasks
        if agent.capability in {
            AgentCapability.IMPLEMENTATION,
            AgentCapability.INTEGRATION,
        }:
            if not assigned:
                raise ValueError(
                    "implementation Agent requires at least one assigned task"
                )
            if self.manual_review_criteria:
                raise ValueError(
                    "implementation Agent cannot receive manual-review scope"
                )
            if self.command_evidence and self.revision_feedback is None:
                raise ValueError(
                    "initial implementation cannot receive command evidence"
                )
        else:
            if not self.command_evidence:
                raise ValueError(
                    "quality Agent requires deterministic command evidence"
                )
            criterion_ids = {
                criterion.id for criterion in self.task_brief.acceptance_criteria
            }
            if not set(self.manual_review_criteria).issubset(criterion_ids):
                raise ValueError(
                    "dynamic prompt references an unknown manual criterion"
                )
        return self

    @property
    def agent(self) -> AgentSpec:
        """Return the exact approved AgentSpec."""

        return self.team_plan.get_agent(self.agent_id)

    @property
    def assigned_tasks(self) -> tuple[ProposedTask, ...]:
        """Return approved work-intent tasks owned by this Agent."""

        return tuple(
            task
            for task in self.implementation_plan.tasks
            if task.owner_agent_id == self.agent_id
        )

    @property
    def model_route_id(self) -> str:
        """Return the primary or explicitly activated approved route."""

        return self.active_model_route_id or self.agent.model_route_id


type PromptArtifact = (
    ImplementationPlan | WorkResult | TestReport | ReviewReport | IterationRecord
)


class AgentPromptInputs(BaseModel):
    """Validated information from which one minimal role prompt is rendered."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_brief: TaskBrief
    team_id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    team_roles: frozenset[AgentRole] = Field(min_length=1)
    iteration: int = Field(ge=1)
    iteration_limit: int = Field(ge=1)
    role: AgentRole
    expected_kind: ArtifactKind
    input_commit: str | None = Field(default=None, pattern=COMMIT_PATTERN)
    upstream_artifacts: tuple[PromptArtifact, ...] = ()
    command_evidence: tuple[CommandEvidence, ...] = ()
    manual_review_criteria: tuple[str, ...] = ()

    @field_validator("upstream_artifacts")
    @classmethod
    def require_unique_artifact_kinds(
        cls,
        values: tuple[PromptArtifact, ...],
    ) -> tuple[PromptArtifact, ...]:
        """Keep every contextual artifact attributable by its unique kind."""

        kinds = [artifact.kind for artifact in values]
        if len(kinds) != len(set(kinds)):
            raise ValueError("prompt context cannot repeat an artifact kind")
        return values

    @field_validator("command_evidence")
    @classmethod
    def require_unique_command_ids(
        cls,
        values: tuple[CommandEvidence, ...],
    ) -> tuple[CommandEvidence, ...]:
        """Reject ambiguous deterministic command evidence."""

        identifiers = [command.id for command in values]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("prompt command evidence IDs must be unique")
        return values

    @field_validator("manual_review_criteria")
    @classmethod
    def require_unique_manual_review_criteria(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        """Reject ambiguous manual-review scope."""

        cleaned = tuple(value.strip() for value in values)
        if any(not value for value in cleaned) or len(cleaned) != len(set(cleaned)):
            raise ValueError("manual-review criterion IDs must be non-empty and unique")
        return cleaned

    @model_validator(mode="after")
    def validate_context_boundary(self) -> Self:
        """Permit only the upstream evidence required by the invoked role."""

        validate_role_artifact_kind(self.role, self.expected_kind)
        if not self.task_brief.confirmed:
            raise ValueError("Agent prompts require a confirmed task brief")
        if self.role not in self.team_roles:
            raise ValueError("prompt role is not part of the selected team")
        if self.iteration > self.iteration_limit:
            raise ValueError("prompt iteration exceeds the run iteration limit")
        if self.role is AgentRole.PLANNER and self.iteration != 1:
            raise ValueError("the Planner runs only for the first implementation plan")

        for artifact in self.upstream_artifacts:
            try:
                validate_artifact_context(
                    artifact,
                    task_brief=self.task_brief,
                    team_id=self.team_id,
                    team_agents={
                        role.value: capability_for_legacy_role(role).value
                        for role in self.team_roles
                    },
                    iteration_limit=self.iteration_limit,
                )
            except ValueError as error:
                raise ValueError(
                    f"invalid {artifact.kind.value} prompt context"
                ) from error

        if self.role is AgentRole.PLANNER:
            self._validate_planner_context()
        elif self.role in IMPLEMENTATION_ROLES:
            self._validate_implementation_context()
        elif self.role is AgentRole.TESTER:
            self._validate_tester_context()
        elif self.role is AgentRole.REVIEWER:
            self._validate_reviewer_context()
        else:
            raise ValueError(f"no prompt boundary is implemented for {self.role.value}")
        return self

    def _validate_planner_context(self) -> None:
        if (
            self.upstream_artifacts
            or self.command_evidence
            or self.manual_review_criteria
        ):
            raise ValueError("Planner receives only the confirmed task brief")

    def _validate_implementation_context(self) -> None:
        if self.command_evidence or self.manual_review_criteria:
            raise ValueError("implementation roles do not receive command evidence")
        if self.input_commit is None:
            raise ValueError("implementation prompt requires an input commit")
        allowed = (ImplementationPlan, TestReport, ReviewReport, IterationRecord)
        if any(
            not isinstance(artifact, allowed) for artifact in self.upstream_artifacts
        ):
            raise ValueError("implementation prompt contains unrelated artifacts")

        plans = self._artifacts(ImplementationPlan)
        if self.role is not AgentRole.SINGLE_AGENT and len(plans) != 1:
            raise ValueError(
                "specialized implementation requires one implementation plan"
            )
        if self.role is AgentRole.SINGLE_AGENT and plans:
            raise ValueError(
                "single-Agent baseline does not receive a Planner artifact"
            )

        feedback = tuple(
            artifact
            for artifact in self.upstream_artifacts
            if isinstance(artifact, (TestReport, ReviewReport, IterationRecord))
        )
        if self.iteration == 1:
            if feedback:
                raise ValueError(
                    "initial implementation cannot receive revision feedback"
                )
            return
        tests = self._artifacts(TestReport)
        reviews = self._artifacts(ReviewReport)
        records = self._artifacts(IterationRecord)
        if len(tests) != 1 or len(reviews) != 1 or len(records) > 1:
            raise ValueError(
                "revision requires one prior test report and one prior review report"
            )
        for artifact in (*tests, *reviews, *records):
            if artifact.iteration != self.iteration - 1:
                raise ValueError("revision feedback must come from the prior iteration")

    def _validate_tester_context(self) -> None:
        if self.input_commit is None:
            raise ValueError("Tester prompt requires the verified input commit")
        work = self._artifacts(WorkResult)
        if len(self.upstream_artifacts) != 1 or len(work) != 1:
            raise ValueError("Tester receives exactly one current work result")
        if work[0].iteration != self.iteration:
            raise ValueError("Tester work result must match the current iteration")
        if work[0].output_commit != self.input_commit:
            raise ValueError("Tester input commit must match the work result")
        if not self.command_evidence:
            raise ValueError("Tester requires deterministic command evidence")
        self._validate_verification_scope()

    def _validate_reviewer_context(self) -> None:
        if self.input_commit is None:
            raise ValueError("Reviewer prompt requires the verified input commit")
        work = self._artifacts(WorkResult)
        if len(self.upstream_artifacts) != 1 or len(work) != 1:
            raise ValueError("Reviewer receives exactly one current work result")
        if work[0].iteration != self.iteration:
            raise ValueError("Reviewer work result must match the current iteration")
        if work[0].output_commit != self.input_commit:
            raise ValueError("Reviewer input commit must match the work result")
        if not self.command_evidence:
            raise ValueError("Reviewer requires deterministic command evidence")
        self._validate_verification_scope()

    def _validate_verification_scope(self) -> None:
        expected = {criterion.id for criterion in self.task_brief.acceptance_criteria}
        manual = set(self.manual_review_criteria)
        if not manual.issubset(expected):
            raise ValueError("manual-review scope references an unknown criterion")
        if any(not command.criterion_ids for command in self.command_evidence):
            raise ValueError("verification commands require criterion coverage")
        deterministic = {
            criterion_id
            for command in self.command_evidence
            for criterion_id in command.criterion_ids
        }
        if not deterministic.issubset(expected):
            raise ValueError("command evidence references an unknown criterion")
        if deterministic | manual != expected:
            raise ValueError(
                "verification scope must cover every confirmed acceptance criterion"
            )

    def _artifacts[ArtifactT: PromptArtifact](
        self,
        model: type[ArtifactT],
    ) -> tuple[ArtifactT, ...]:
        return tuple(
            artifact
            for artifact in self.upstream_artifacts
            if isinstance(artifact, model)
        )


__all__ = [
    "DYNAMIC_CAPABILITY_TEMPLATES",
    "ROLE_TEMPLATES",
    "TEMPLATE_ROOT",
    "_COMMAND_PROMPT_EXCERPT_BUDGET",
    "_COMMAND_PROMPT_STREAM_LIMIT",
    "AgentPromptError",
    "AgentPromptInputs",
    "DynamicAgentPromptInputs",
    "DynamicRevisionFeedback",
    "DynamicUpstreamResult",
    "DynamicUserGuidance",
    "PromptArtifact",
]
