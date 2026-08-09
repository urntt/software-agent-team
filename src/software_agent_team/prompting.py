"""Role-specific prompt rendering with an explicit minimum context boundary."""

from __future__ import annotations

import json
from pathlib import Path
from string import Template
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from software_agent_team.artifacts import (
    ARTIFACT_MODELS,
    COMMIT_PATTERN,
    IMPLEMENTATION_ROLES,
    AgentRole,
    ArtifactKind,
    CommandEvidence,
    ImplementationPlan,
    IterationRecord,
    ReviewReport,
    TaskBrief,
    TestReport,
    WorkResult,
    validate_artifact_context,
)
from software_agent_team.execution import (
    AgentExecutionRequest,
    validate_role_artifact_kind,
)

TEMPLATE_ROOT = Path(__file__).with_name("prompt_templates")

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


class AgentPromptError(ValueError):
    """Raised when a role prompt would cross its declared context boundary."""


type PromptArtifact = (
    ImplementationPlan | WorkResult | TestReport | ReviewReport | IterationRecord
)


class AgentPromptInputs(BaseModel):
    """Validated information from which one minimal role prompt is rendered."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_brief: TaskBrief
    team_id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    team_roles: frozenset[AgentRole] = Field(min_length=1)
    iteration: int = Field(ge=1, le=3)
    iteration_limit: int = Field(ge=1, le=3)
    role: AgentRole
    expected_kind: ArtifactKind
    input_commit: str | None = Field(default=None, pattern=COMMIT_PATTERN)
    upstream_artifacts: tuple[PromptArtifact, ...] = ()
    command_evidence: tuple[CommandEvidence, ...] = ()

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
                    team_roles=set(self.team_roles),
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
        if self.upstream_artifacts or self.command_evidence:
            raise ValueError("Planner receives only the confirmed task brief")

    def _validate_implementation_context(self) -> None:
        if self.command_evidence:
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

    def _artifacts[ArtifactT: PromptArtifact](
        self,
        model: type[ArtifactT],
    ) -> tuple[ArtifactT, ...]:
        return tuple(
            artifact
            for artifact in self.upstream_artifacts
            if isinstance(artifact, model)
        )


def _artifact_context(inputs: AgentPromptInputs) -> dict[str, object]:
    artifacts: dict[str, object] = {}
    for artifact in inputs.upstream_artifacts:
        artifacts[artifact.kind.value] = artifact.model_dump(mode="json")
    return artifacts


def _prompt_context(inputs: AgentPromptInputs) -> dict[str, object]:
    context: dict[str, object] = {
        "run": {
            "run_id": inputs.task_brief.run_id,
            "team_id": inputs.team_id,
            "iteration": inputs.iteration,
            "iteration_limit": inputs.iteration_limit,
            "role": inputs.role.value,
            "expected_artifact_kind": inputs.expected_kind.value,
            "implementation_roles": sorted(
                role.value for role in inputs.team_roles if role in IMPLEMENTATION_ROLES
            ),
        },
        "task_brief": inputs.task_brief.model_dump(mode="json"),
    }
    if inputs.input_commit is not None:
        run = context["run"]
        if isinstance(run, dict):
            run["input_commit"] = inputs.input_commit
    artifacts = _artifact_context(inputs)
    if artifacts:
        context["upstream_artifacts"] = artifacts
    if inputs.command_evidence:
        context["deterministic_command_evidence"] = [
            command.model_dump(mode="json") for command in inputs.command_evidence
        ]
    return context


def render_agent_prompt(
    inputs: AgentPromptInputs,
    *,
    template_root: Path = TEMPLATE_ROOT,
) -> str:
    """Render one role prompt with only its validated contextual inputs."""

    template_name = ROLE_TEMPLATES.get(inputs.role)
    if template_name is None:
        raise AgentPromptError(f"no prompt template exists for {inputs.role.value}")
    template_path = template_root / template_name
    try:
        source = template_path.read_text(encoding="utf-8")
    except OSError as error:
        raise AgentPromptError(
            f"cannot load prompt template: {template_name}"
        ) from error
    model = ARTIFACT_MODELS.get(inputs.expected_kind)
    if model is None:
        raise AgentPromptError(
            f"no response model exists for {inputs.expected_kind.value}"
        )
    values = {
        "role": inputs.role.value,
        "expected_kind": inputs.expected_kind.value,
        "context_json": json.dumps(
            _prompt_context(inputs),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        "response_schema_json": json.dumps(
            model.model_json_schema(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
    }
    try:
        rendered = Template(source).substitute(values)
    except (KeyError, ValueError) as error:
        raise AgentPromptError(f"invalid prompt template: {template_name}") from error
    if not rendered.strip():
        raise AgentPromptError(f"prompt template rendered empty: {template_name}")
    return rendered


def build_agent_execution_request(
    inputs: AgentPromptInputs,
    *,
    timeout_seconds: int = 600,
    model: str | None = None,
    template_root: Path = TEMPLATE_ROOT,
) -> AgentExecutionRequest:
    """Bind validated prompt inputs to the matching execution identity."""

    return AgentExecutionRequest(
        run_id=inputs.task_brief.run_id,
        team_id=inputs.team_id,
        iteration=inputs.iteration,
        role=inputs.role,
        expected_kind=inputs.expected_kind,
        prompt=render_agent_prompt(inputs, template_root=template_root),
        timeout_seconds=timeout_seconds,
        model=model,
    )
