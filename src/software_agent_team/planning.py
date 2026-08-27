"""Adaptive Planning dialogue, proposal validation, approval, and evidence."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from string import Template
from typing import Literal, Self
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from software_agent_team.artifacts import (
    AcceptanceCriterion,
    AgentRole,
    ArtifactKind,
    TaskBrief,
)
from software_agent_team.budgets import AgentBudget
from software_agent_team.execution import (
    AgentExecutionRequest,
    AgentExecutionResult,
    AgentExecutionStatus,
    AgentExecutor,
)
from software_agent_team.integrity import canonical_model_sha256
from software_agent_team.model_routing import (
    ModelProfile,
    ModelRoutingError,
    ModelRoutingPolicy,
    resolve_model_route_plan,
)
from software_agent_team.responses import (
    AgentArtifactResponseError,
    parse_json_object_response,
)
from software_agent_team.teams import (
    AgentCapability,
    AgentSpec,
    ModelRoute,
    ModelRoutingMode,
    PlanApprovalSource,
    TeamPlan,
    TeamPlanOrigin,
    expected_output_for_capability,
    permission_for_capability,
)

PLANNING_SCHEMA_VERSION = 2
PLANNING_TEMPLATE = Path(__file__).with_name("prompt_templates") / "adaptive_planner.md"
_MAX_EVIDENCE_TEXT = 1_000_000
_MAX_RESPONSE_NORMALIZATIONS = 100
_MAX_RESPONSE_NORMALIZATION_LENGTH = 200


class PlanningError(RuntimeError):
    """Raised when an adaptive Planning session cannot continue safely."""


class PlanningIntegrityError(PlanningError):
    """Raised when persisted Planning evidence is incomplete or changed."""


class PlanningSessionStatus(StrEnum):
    """Controller-owned state of one pre-execution Planning session."""

    AUTHORIZED = "authorized"
    CLARIFYING = "clarifying"
    PROPOSED = "proposed"
    APPROVED = "approved"
    CANCELLED = "cancelled"


class PlanningResponseKind(StrEnum):
    """Allowed semantic outcomes of one bootstrap Planner turn."""

    QUESTION = "question"
    PROPOSAL = "proposal"


class PlanningProposalSource(StrEnum):
    """Attributable origin of one immutable proposal revision."""

    MODEL = "model"
    STRUCTURED_EDIT = "structured_edit"


class StructuredEditKind(StrEnum):
    """Fields that the controller can edit without accepting internal JSON."""

    MAX_CONCURRENCY = "max_concurrency"
    ITERATION_LIMIT = "iteration_limit"
    AGENT_TIMEOUT = "agent_timeout"
    AGENT_MODEL = "agent_model"


class AgentWorkload(StrEnum):
    """Planner-owned qualitative workload estimate for one runtime Agent."""

    ROUTINE = "routine"
    SUBSTANTIAL = "substantial"
    COMPLEX = "complex"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Planning timestamps must include a timezone")
    return value.astimezone(UTC)


def _clean_text(value: str, *, label: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{label} must not be blank")
    return cleaned


def _clean_unique(values: tuple[str, ...], *, label: str) -> tuple[str, ...]:
    cleaned = tuple(value.strip() for value in values)
    if any(not value for value in cleaned):
        raise ValueError(f"{label} entries must not be blank")
    if len(cleaned) != len(set(cleaned)):
        raise ValueError(f"{label} entries must be unique")
    return cleaned


def _safe_path(value: str) -> str:
    cleaned = value.strip()
    path = PurePosixPath(cleaned)
    if (
        not cleaned
        or "\\" in cleaned
        or path.is_absolute()
        or path == PurePosixPath(".")
        or ".." in path.parts
        or str(path) != cleaned
    ):
        raise ValueError("paths must be canonical safe relative POSIX paths")
    return cleaned


def _canonicalize_model_path(value: object) -> object:
    """Normalize only unambiguous safe relative-path presentation variants."""

    if not isinstance(value, str):
        return value
    cleaned = value.strip()
    path = PurePosixPath(cleaned)
    if (
        not cleaned
        or "\\" in cleaned
        or path.is_absolute()
        or path == PurePosixPath(".")
        or ".." in path.parts
    ):
        return value
    return str(path)


def _normalize_planning_response_payload(
    payload: dict[str, object],
) -> tuple[dict[str, object], tuple[str, ...]]:
    """Apply bounded semantic-preserving normalization before strict validation."""

    normalized: dict[str, object] = json.loads(json.dumps(payload))
    changes: list[str] = []
    if "kind" not in normalized:
        candidates = tuple(
            name
            for name in ("question", "proposal")
            if normalized.get(name) is not None
        )
        if len(candidates) == 1:
            normalized["kind"] = candidates[0]
            changes.append(f"inferred response kind as {candidates[0]}")

    proposal = normalized.get("proposal")
    if not isinstance(proposal, dict):
        return normalized, tuple(changes)
    tasks = proposal.get("tasks")
    if isinstance(tasks, list):
        for task_index, task in enumerate(tasks):
            if not isinstance(task, dict):
                continue
            paths = task.get("expected_paths")
            if not isinstance(paths, list):
                continue
            for path_index, value in enumerate(paths):
                canonical = _canonicalize_model_path(value)
                if canonical != value:
                    paths[path_index] = canonical
                    changes.append(
                        "canonicalized "
                        f"proposal.tasks[{task_index}].expected_paths[{path_index}]"
                    )
    agents = proposal.get("agents")
    if isinstance(agents, list):
        for agent_index, agent in enumerate(agents):
            if not isinstance(agent, dict) or "workspace_scope" not in agent:
                continue
            value = agent["workspace_scope"]
            canonical = _canonicalize_model_path(value)
            if canonical != value:
                agent["workspace_scope"] = canonical
                changes.append(
                    f"canonicalized proposal.agents[{agent_index}].workspace_scope"
                )
    return normalized, tuple(changes)


def _digest_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _safe_validation_detail(error: ValueError) -> str:
    if isinstance(error, ValidationError):
        details = []
        for issue in error.errors(
            include_url=False,
            include_context=False,
            include_input=False,
        ):
            location = ".".join(str(item) for item in issue["loc"]) or "response"
            details.append(f"{location}: {issue['msg']}")
        return "; ".join(details)[:1500]
    return str(error)[:1500]


class PlanningRequest(BaseModel):
    """Direct user input and explicit authorization before any model work."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[PLANNING_SCHEMA_VERSION] = PLANNING_SCHEMA_VERSION
    run_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    project_name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    source_request: str = Field(min_length=1, max_length=2000)
    destination: str = Field(min_length=1, max_length=4096)
    execution_profile: tuple[str, ...] = Field(min_length=1, max_length=20)
    base_constraints: tuple[str, ...] = Field(default=(), max_length=20)
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


class PlanningOption(BaseModel):
    """One suggested answer while preserving a custom-answer path."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    label: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=300)

    @field_validator("label", "description")
    @classmethod
    def require_clean_text(cls, value: str) -> str:
        return _clean_text(value, label="Planning option text")


class PlanningQuestion(BaseModel):
    """One high-value clarification selected by the bootstrap Planner."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    text: str = Field(min_length=1, max_length=500)
    why: str = Field(min_length=1, max_length=500)
    options: tuple[PlanningOption, ...] = Field(min_length=2, max_length=3)
    allow_custom: Literal[True] = True

    @field_validator("text", "why")
    @classmethod
    def require_clean_text(cls, value: str) -> str:
        return _clean_text(value, label="Planning question text")

    @model_validator(mode="after")
    def require_unique_options(self) -> Self:
        option_ids = [option.id for option in self.options]
        if len(option_ids) != len(set(option_ids)):
            raise ValueError("Planning question option IDs must be unique")
        return self


class ProposedCriterion(BaseModel):
    """User-facing acceptance condition proposed during Planning."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[A-Z][A-Z0-9_-]*$")
    description: str = Field(min_length=1, max_length=500)
    verification: str = Field(min_length=1, max_length=500)

    @field_validator("description", "verification")
    @classmethod
    def require_clean_text(cls, value: str) -> str:
        return _clean_text(value, label="acceptance criterion text")


class ProposedAgent(BaseModel):
    """Planner recommendation before the controller assigns runtime authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    label: str = Field(min_length=1, max_length=80)
    responsibility: str = Field(min_length=1, max_length=500)
    rationale: str = Field(min_length=1, max_length=500)
    capability: AgentCapability
    stage_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    dependencies: tuple[str, ...] = ()
    workspace_scope: str = Field(
        min_length=1,
        max_length=200,
        description=(
            "Use repository for whole-project access or repository/path for a "
            "narrower canonical scope; never repeat the destination directory."
        ),
    )
    workload: AgentWorkload

    @field_validator("label", "responsibility", "rationale")
    @classmethod
    def require_clean_text(cls, value: str) -> str:
        return _clean_text(value, label="proposed Agent text")

    @field_validator("dependencies")
    @classmethod
    def require_unique_dependencies(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _clean_unique(values, label="Agent dependency")

    @field_validator("workspace_scope")
    @classmethod
    def require_safe_scope(cls, value: str) -> str:
        cleaned = _safe_path(value)
        if PurePosixPath(cleaned).parts[0] != "repository":
            raise ValueError("workspace scopes must start at repository or repository/")
        return cleaned

    @model_validator(mode="after")
    def reject_bootstrap_capability(self) -> Self:
        if self.capability in {
            AgentCapability.CLARIFICATION,
            AgentCapability.PLANNING,
        }:
            raise ValueError(
                "bootstrap Planning and Clarification capabilities are outside "
                "the runtime team"
            )
        if self.id in self.dependencies:
            raise ValueError("a proposed Agent cannot depend on itself")
        return self


class ProposedTask(BaseModel):
    """Implementation intent assigned to one proposed runtime Agent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^TASK_[A-Z0-9_]+$")
    owner_agent_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    description: str = Field(min_length=1, max_length=500)
    dependencies: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = Field(min_length=1)
    expected_paths: tuple[str, ...] = Field(
        default=(),
        description=(
            "Canonical paths relative to the repository root; directory paths "
            "must not end with a slash."
        ),
    )

    @field_validator("description")
    @classmethod
    def require_clean_description(cls, value: str) -> str:
        return _clean_text(value, label="task description")

    @field_validator("dependencies", "acceptance_criteria")
    @classmethod
    def require_clean_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _clean_unique(values, label="task reference")

    @field_validator("expected_paths")
    @classmethod
    def require_safe_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = _clean_unique(values, label="expected path")
        return tuple(_safe_path(value) for value in cleaned)


def _validate_dag(
    nodes: tuple[str, ...],
    dependencies: Mapping[str, tuple[str, ...]],
    *,
    label: str,
) -> None:
    known = set(nodes)
    for node, required in dependencies.items():
        unknown = set(required) - known
        if unknown:
            raise ValueError(
                f"{label} {node} references unknown dependencies: "
                f"{', '.join(sorted(unknown))}"
            )
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            raise ValueError(f"{label} dependencies must be acyclic")
        if node in visited:
            return
        visiting.add(node)
        for dependency in dependencies[node]:
            visit(dependency)
        visiting.remove(node)
        visited.add(node)

    for node in nodes:
        visit(node)


class PlanningProposalBody(BaseModel):
    """Complete semantic proposal returned before user approval."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str = Field(min_length=1, max_length=120)
    requirements: tuple[str, ...] = Field(min_length=1, max_length=30)
    acceptance_criteria: tuple[ProposedCriterion, ...] = Field(
        min_length=1,
        max_length=30,
    )
    constraints: tuple[str, ...] = Field(default=(), max_length=30)
    assumptions: tuple[str, ...] = Field(default=(), max_length=30)
    objective: str = Field(min_length=1, max_length=1000)
    approach: tuple[str, ...] = Field(min_length=1, max_length=30)
    tasks: tuple[ProposedTask, ...] = Field(min_length=1, max_length=30)
    risks: tuple[str, ...] = Field(default=(), max_length=30)
    agents: tuple[ProposedAgent, ...] = Field(min_length=2, max_length=16)
    iteration_limit: int = Field(ge=1, le=3)
    max_concurrency: int = Field(ge=1, le=16)
    revision_enabled: bool

    @field_validator("title", "objective")
    @classmethod
    def require_clean_text(cls, value: str) -> str:
        return _clean_text(value, label="proposal text")

    @field_validator(
        "requirements",
        "constraints",
        "assumptions",
        "approach",
        "risks",
    )
    @classmethod
    def require_clean_unique_text(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _clean_unique(values, label="proposal")

    @model_validator(mode="after")
    def validate_complete_proposal(self) -> Self:
        criterion_ids = tuple(item.id for item in self.acceptance_criteria)
        if len(criterion_ids) != len(set(criterion_ids)):
            raise ValueError("proposal acceptance criterion IDs must be unique")

        agent_ids = tuple(agent.id for agent in self.agents)
        if len(agent_ids) != len(set(agent_ids)):
            raise ValueError("proposed Agent IDs must be unique")
        _validate_dag(
            agent_ids,
            {agent.id: agent.dependencies for agent in self.agents},
            label="Agent",
        )
        implementation_agents = {
            agent.id
            for agent in self.agents
            if agent.capability
            in {AgentCapability.IMPLEMENTATION, AgentCapability.INTEGRATION}
        }
        if not implementation_agents:
            raise ValueError("proposal requires an implementation Agent")
        quality_agents = {
            agent.id
            for agent in self.agents
            if agent.capability in {AgentCapability.TESTING, AgentCapability.REVIEW}
        }
        if not quality_agents:
            raise ValueError("proposal requires an independent quality Agent")

        dependencies = {agent.id: agent.dependencies for agent in self.agents}

        def transitively_depends(agent_id: str, target: str) -> bool:
            pending = list(dependencies[agent_id])
            seen: set[str] = set()
            while pending:
                current = pending.pop()
                if current == target:
                    return True
                if current not in seen:
                    seen.add(current)
                    pending.extend(dependencies[current])
            return False

        for quality_agent in quality_agents:
            if any(
                not transitively_depends(quality_agent, implementation_agent)
                for implementation_agent in implementation_agents
            ):
                raise ValueError(
                    "every quality Agent must depend on every implementation path"
                )

        task_ids = tuple(task.id for task in self.tasks)
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("proposed task IDs must be unique")
        _validate_dag(
            task_ids,
            {task.id: task.dependencies for task in self.tasks},
            label="task",
        )
        if any(task.owner_agent_id not in implementation_agents for task in self.tasks):
            raise ValueError("every task owner must be an implementation Agent")
        task_owners = {task.owner_agent_id for task in self.tasks}
        unassigned_agents = implementation_agents - task_owners
        if unassigned_agents:
            raise ValueError(
                "every implementation Agent must own at least one task: "
                f"{', '.join(sorted(unassigned_agents))}"
            )
        task_owner_by_id = {task.id: task.owner_agent_id for task in self.tasks}
        for task in self.tasks:
            for dependency_id in task.dependencies:
                dependency_owner = task_owner_by_id[dependency_id]
                if dependency_owner != task.owner_agent_id and not transitively_depends(
                    task.owner_agent_id,
                    dependency_owner,
                ):
                    raise ValueError(
                        f"task {task.id} depends on {dependency_id}, but Agent "
                        f"{task.owner_agent_id} does not depend on "
                        f"{dependency_owner}"
                    )
        covered = {
            criterion for task in self.tasks for criterion in task.acceptance_criteria
        }
        expected = set(criterion_ids)
        if covered != expected:
            missing = ", ".join(sorted(expected - covered)) or "none"
            unknown = ", ".join(sorted(covered - expected)) or "none"
            raise ValueError(
                "task acceptance coverage differs from the proposal "
                f"(missing: {missing}; unknown: {unknown})"
            )
        if self.max_concurrency > len(self.agents):
            raise ValueError("proposal concurrency cannot exceed its Agent count")
        if self.revision_enabled != (self.iteration_limit > 1):
            raise ValueError(
                "revision_enabled must equal whether iteration_limit exceeds one"
            )
        return self


class PlanningModelResponse(BaseModel):
    """Strict either-question-or-proposal response from bootstrap Planning."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: PlanningResponseKind
    question: PlanningQuestion | None = None
    proposal: PlanningProposalBody | None = None

    @model_validator(mode="after")
    def require_exact_payload(self) -> Self:
        if self.kind is PlanningResponseKind.QUESTION:
            if self.question is None or self.proposal is not None:
                raise ValueError("question responses require only question")
        elif self.proposal is None or self.question is not None:
            raise ValueError("proposal responses require only proposal")
        return self


class AdaptiveImplementationPlan(BaseModel):
    """Approved task-to-Agent intent bound by an adaptive TeamPlan."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[PLANNING_SCHEMA_VERSION] = PLANNING_SCHEMA_VERSION
    run_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    team_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    revision: int = Field(ge=1, le=99)
    created_at: datetime
    objective: str
    approach: tuple[str, ...]
    tasks: tuple[ProposedTask, ...]
    risks: tuple[str, ...]
    assumptions: tuple[str, ...]

    @field_validator("created_at")
    @classmethod
    def require_timestamp(cls, value: datetime) -> datetime:
        return _utc(value)


class PlanningExecutionEvidence(BaseModel):
    """Bounded provider and usage evidence copied from one execution result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: AgentExecutionStatus
    session_key: str
    started_at: datetime
    finished_at: datetime
    duration_ms: int = Field(ge=0)
    provider: str | None = None
    model: str | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    error: str | None = None

    @field_validator("started_at", "finished_at")
    @classmethod
    def require_timestamp(cls, value: datetime) -> datetime:
        return _utc(value)


class PlanningTurn(BaseModel):
    """One append-only model invocation, including invalid response evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[PLANNING_SCHEMA_VERSION] = PLANNING_SCHEMA_VERSION
    run_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    sequence: int = Field(ge=1, le=999)
    previous_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    user_message: str = Field(min_length=1, max_length=10_000)
    prompt: str = Field(min_length=1, max_length=_MAX_EVIDENCE_TEXT)
    prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    response_text: str | None = Field(default=None, max_length=_MAX_EVIDENCE_TEXT)
    response_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    parsed_response: PlanningModelResponse | None = None
    response_normalizations: tuple[str, ...] = Field(
        default=(),
        exclude_if=lambda values: not values,
    )
    validation_error: str | None = Field(default=None, min_length=1, max_length=2000)
    execution: PlanningExecutionEvidence

    @field_validator("response_normalizations")
    @classmethod
    def require_unique_normalizations(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) > _MAX_RESPONSE_NORMALIZATIONS:
            raise ValueError("too many Planning response normalizations")
        if any(len(value) > _MAX_RESPONSE_NORMALIZATION_LENGTH for value in values):
            raise ValueError("Planning response normalization entries are too long")
        return _clean_unique(values, label="Planning response normalization")

    @model_validator(mode="after")
    def validate_evidence(self) -> Self:
        if _digest_text(self.prompt) != self.prompt_sha256:
            raise ValueError("Planning prompt digest does not match its content")
        if self.response_text is None:
            if (
                self.response_sha256 is not None
                or self.parsed_response is not None
                or self.response_normalizations
            ):
                raise ValueError("missing response text cannot have parsed evidence")
        elif _digest_text(self.response_text) != self.response_sha256:
            raise ValueError("Planning response digest does not match its content")
        if self.parsed_response is not None and self.validation_error is not None:
            raise ValueError("valid Planning turns cannot contain a validation error")
        if self.execution.status is AgentExecutionStatus.COMPLETED:
            if self.response_text is None:
                raise ValueError("completed Planning execution requires response text")
            if self.parsed_response is None and self.validation_error is None:
                raise ValueError(
                    "completed Planning response requires validation state"
                )
        elif self.validation_error is None:
            raise ValueError("failed Planning execution requires a validation error")
        return self


class PlanningProposal(BaseModel):
    """Immutable, validated proposal revision shown to the user."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[PLANNING_SCHEMA_VERSION] = PLANNING_SCHEMA_VERSION
    run_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    revision: int = Field(ge=1, le=99)
    created_at: datetime
    source: PlanningProposalSource
    source_turn_sequence: int | None = Field(default=None, ge=1, le=999)
    change_request: str | None = Field(default=None, min_length=1, max_length=2000)
    body: PlanningProposalBody
    timeout_overrides_seconds: dict[str, int] = Field(default_factory=dict)
    model_profile_overrides: dict[str, str] = Field(default_factory=dict)

    @field_validator("created_at")
    @classmethod
    def require_timestamp(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def require_source_evidence(self) -> Self:
        if self.source is PlanningProposalSource.MODEL:
            if self.source_turn_sequence is None:
                raise ValueError("model proposal requires its source turn")
            if self.timeout_overrides_seconds:
                raise ValueError("model proposals cannot authorize timeout overrides")
            if self.model_profile_overrides:
                raise ValueError("model proposals cannot authorize model overrides")
        elif self.source_turn_sequence is not None:
            raise ValueError("structured edit cannot claim a model turn")
        known_agents = {agent.id for agent in self.body.agents}
        unknown_agents = set(self.timeout_overrides_seconds) - known_agents
        if unknown_agents:
            raise ValueError(
                "timeout overrides reference unknown Agents: "
                + ", ".join(sorted(unknown_agents))
            )
        unknown_model_agents = set(self.model_profile_overrides) - known_agents
        if unknown_model_agents:
            raise ValueError(
                "model overrides reference unknown Agents: "
                + ", ".join(sorted(unknown_model_agents))
            )
        if any(
            re.fullmatch(r"[a-z][a-z0-9_]*", profile_id) is None
            for profile_id in self.model_profile_overrides.values()
        ):
            raise ValueError("model overrides require safe profile IDs")
        if any(
            not 30 <= seconds <= 3600
            for seconds in self.timeout_overrides_seconds.values()
        ):
            raise ValueError("timeout overrides must be within 30..3600s")
        return self


class PlanningSession(BaseModel):
    """Atomic index anchoring all write-once Planning evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[PLANNING_SCHEMA_VERSION] = PLANNING_SCHEMA_VERSION
    run_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: PlanningSessionStatus
    created_at: datetime
    updated_at: datetime
    turn_count: int = Field(default=0, ge=0, le=999)
    turn_head_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    latest_proposal_revision: int | None = Field(default=None, ge=1, le=99)
    approved_revision: int | None = Field(default=None, ge=1, le=99)

    @field_validator("created_at", "updated_at")
    @classmethod
    def require_timestamp(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        if self.updated_at < self.created_at:
            raise ValueError("Planning session cannot update before creation")
        if (self.turn_count == 0) != (self.turn_head_sha256 is None):
            raise ValueError("Planning turn count and head digest are inconsistent")
        if (
            self.status is PlanningSessionStatus.PROPOSED
            and self.latest_proposal_revision is None
        ):
            raise ValueError("proposed Planning session requires a proposal")
        if self.status is PlanningSessionStatus.APPROVED:
            if self.approved_revision != self.latest_proposal_revision:
                raise ValueError("approved Planning session must bind latest proposal")
        elif self.approved_revision is not None:
            raise ValueError("only approved Planning sessions have approval evidence")
        return self


class StructuredPlanEdit(BaseModel):
    """One safe controller-owned edit selected through the product UI."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: StructuredEditKind
    value: int | str
    agent_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]*$")

    @model_validator(mode="after")
    def require_target(self) -> Self:
        if self.kind in {
            StructuredEditKind.AGENT_TIMEOUT,
            StructuredEditKind.AGENT_MODEL,
        }:
            if self.agent_id is None:
                raise ValueError("Agent-specific edits require an Agent target")
        elif self.agent_id is not None:
            raise ValueError("only Agent-specific edits accept an Agent target")
        if self.kind is StructuredEditKind.AGENT_MODEL:
            if (
                not isinstance(self.value, str)
                or re.fullmatch(r"[a-z][a-z0-9_]*", self.value) is None
            ):
                raise ValueError("Agent model edits require a safe profile ID")
            return self
        if not isinstance(self.value, int):
            raise ValueError("numeric plan edits require an integer")
        if not 1 <= self.value <= 3600:
            raise ValueError("numeric plan edits must be within 1..3600")
        if self.kind is StructuredEditKind.AGENT_TIMEOUT and self.value < 30:
            raise ValueError("Agent timeout edit requires an Agent and at least 30s")
        if self.kind is StructuredEditKind.ITERATION_LIMIT and self.value > 3:
            raise ValueError("iteration limit cannot exceed three")
        if self.kind is StructuredEditKind.MAX_CONCURRENCY and self.value > 16:
            raise ValueError("concurrency cannot exceed sixteen")
        return self


class CapabilityTimeoutPolicy(BaseModel):
    """Controller-owned timeout envelope for one adaptive capability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    default_seconds: int = Field(ge=30, le=3600)
    ceiling_seconds: int = Field(ge=30, le=3600)

    @model_validator(mode="after")
    def require_ordered_envelope(self) -> Self:
        if self.default_seconds > self.ceiling_seconds:
            raise ValueError("timeout default cannot exceed its ceiling")
        return self

    def resolve(self, workload: AgentWorkload) -> int:
        """Map a qualitative estimate to a deterministic policy value."""

        if workload is AgentWorkload.ROUTINE:
            return self.default_seconds
        if workload is AgentWorkload.COMPLEX:
            return self.ceiling_seconds
        return self.default_seconds + (self.ceiling_seconds - self.default_seconds) // 2


class AgentTimeoutResolution(BaseModel):
    """Explain how the controller resolved one approved invocation timeout."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    workload: AgentWorkload
    default_seconds: int = Field(ge=30, le=3600)
    ceiling_seconds: int = Field(ge=30, le=3600)
    resolved_seconds: int = Field(ge=30, le=3600)
    source: Literal["policy_workload", "user_override"]

    @model_validator(mode="after")
    def require_valid_resolution(self) -> Self:
        policy = CapabilityTimeoutPolicy(
            default_seconds=self.default_seconds,
            ceiling_seconds=self.ceiling_seconds,
        )
        if not self.default_seconds <= self.resolved_seconds <= self.ceiling_seconds:
            raise ValueError("resolved timeout must remain inside its policy envelope")
        if self.source == "policy_workload" and self.resolved_seconds != policy.resolve(
            self.workload
        ):
            raise ValueError("policy timeout does not match the workload mapping")
        return self


class PlanningApproval(BaseModel):
    """Explicit user authorization for one exact proposal and compiled plan."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[PLANNING_SCHEMA_VERSION] = PLANNING_SCHEMA_VERSION
    run_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    revision: int = Field(ge=1, le=99)
    approved_at: datetime
    confirmation: Literal["user_approved"]
    proposal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    task_brief_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    implementation_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    team_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    timeout_resolutions: tuple[AgentTimeoutResolution, ...] = Field(
        min_length=2,
        max_length=16,
    )

    @field_validator("approved_at")
    @classmethod
    def require_timestamp(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("timeout_resolutions")
    @classmethod
    def require_unique_timeout_agents(
        cls,
        values: tuple[AgentTimeoutResolution, ...],
    ) -> tuple[AgentTimeoutResolution, ...]:
        agent_ids = [resolution.agent_id for resolution in values]
        if len(agent_ids) != len(set(agent_ids)):
            raise ValueError("timeout resolutions must identify unique Agents")
        return values


class PlanningPolicy(BaseModel):
    """Controller limits around dialogue and adaptive proposal compilation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_clarification_rounds: int = Field(default=3, ge=0, le=5)
    max_proposal_revisions: int = Field(default=3, ge=1, le=5)
    response_repair_limit: int = Field(default=1, ge=0, le=2)
    planning_timeout_seconds: int = Field(default=180, ge=1, le=3600)
    max_agents: int = Field(default=8, ge=2, le=16)
    max_concurrency: int = Field(default=4, ge=1, le=16)
    max_review_agents: int = Field(default=16, ge=1, le=16)
    budget: AgentBudget
    capability_timeouts: dict[AgentCapability, CapabilityTimeoutPolicy]
    model_routing: ModelRoutingPolicy | None = None
    profile_acceptance_criteria: tuple[AcceptanceCriterion, ...] = ()
    require_review_agent: bool = False

    @field_validator("capability_timeouts")
    @classmethod
    def require_runtime_capabilities(
        cls,
        values: dict[AgentCapability, CapabilityTimeoutPolicy],
    ) -> dict[AgentCapability, CapabilityTimeoutPolicy]:
        required = {
            AgentCapability.IMPLEMENTATION,
            AgentCapability.INTEGRATION,
            AgentCapability.TESTING,
            AgentCapability.REVIEW,
        }
        if set(values) != required:
            raise ValueError(
                "Planning policy requires every runtime capability timeout"
            )
        return values

    @field_validator("profile_acceptance_criteria")
    @classmethod
    def require_unique_profile_criteria(
        cls,
        values: tuple[AcceptanceCriterion, ...],
    ) -> tuple[AcceptanceCriterion, ...]:
        identifiers = [criterion.id for criterion in values]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("profile acceptance criterion IDs must be unique")
        return values


class PlanningPreview(BaseModel):
    """Validated controller interpretation shown before user approval."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    destination: str
    execution_profile: tuple[str, ...]
    task_brief: TaskBrief
    implementation_plan: AdaptiveImplementationPlan
    team_plan: TeamPlan
    timeout_resolutions: tuple[AgentTimeoutResolution, ...]


class ApprovedPlanningResult(BaseModel):
    """Approved inputs ready for the dynamic controller runtime."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_brief: TaskBrief
    implementation_plan: AdaptiveImplementationPlan
    team_plan: TeamPlan
    approval: PlanningApproval

    @model_validator(mode="after")
    def validate_approval_boundary(self) -> Self:
        """Bind execution inputs to the exact proposal revision the user approved."""

        run_ids = {
            self.task_brief.run_id,
            self.implementation_plan.run_id,
            self.team_plan.run_id,
            self.approval.run_id,
        }
        if len(run_ids) != 1:
            raise ValueError("approved Planning inputs use different run IDs")
        if (
            self.implementation_plan.revision != self.approval.revision
            or self.team_plan.revision != self.approval.revision
        ):
            raise ValueError("approved Planning inputs use different revisions")
        if self.implementation_plan.team_id != self.team_plan.team_id:
            raise ValueError(
                "approved implementation and team plans use different teams"
            )
        if self.team_plan.task_brief_sha256 != canonical_model_sha256(self.task_brief):
            raise ValueError("approved TeamPlan does not bind the supplied TaskBrief")
        if self.team_plan.implementation_plan_sha256 != canonical_model_sha256(
            self.implementation_plan
        ):
            raise ValueError(
                "approved TeamPlan does not bind the supplied implementation plan"
            )
        expected_digests = {
            "task brief": (
                self.approval.task_brief_sha256,
                canonical_model_sha256(self.task_brief),
            ),
            "implementation plan": (
                self.approval.implementation_plan_sha256,
                canonical_model_sha256(self.implementation_plan),
            ),
            "TeamPlan": (
                self.approval.team_plan_sha256,
                canonical_model_sha256(self.team_plan),
            ),
        }
        mismatched = [
            label
            for label, (approved, actual) in expected_digests.items()
            if approved != actual
        ]
        if mismatched:
            raise ValueError(
                "Planning approval does not bind the supplied " + ", ".join(mismatched)
            )
        agents_by_id = {agent.id: agent for agent in self.team_plan.agents}
        resolutions_by_id = {
            resolution.agent_id: resolution
            for resolution in self.approval.timeout_resolutions
        }
        if set(resolutions_by_id) != set(agents_by_id):
            raise ValueError(
                "Planning approval timeout resolutions do not cover the TeamPlan Agents"
            )
        mismatched_timeouts = [
            agent_id
            for agent_id, resolution in resolutions_by_id.items()
            if resolution.resolved_seconds != agents_by_id[agent_id].timeout_seconds
        ]
        if mismatched_timeouts:
            raise ValueError(
                "Planning approval timeout resolutions do not match the TeamPlan for "
                + ", ".join(sorted(mismatched_timeouts))
            )
        return self


Clock = Callable[[], datetime]
QuestionAnswerer = Callable[[PlanningQuestion], str | None]
InputReader = Callable[[str], str]
OutputWriter = Callable[[str], None]


def _system_clock() -> datetime:
    return datetime.now(UTC)


def preview_adaptive_proposal(
    request: PlanningRequest,
    proposal: PlanningProposal,
    policy: PlanningPolicy,
    *,
    created_at: datetime,
) -> PlanningPreview:
    """Compile and validate the exact authority that approval would create."""

    if proposal.run_id != request.run_id:
        raise PlanningError("proposal belongs to a different Planning request")
    body = proposal.body
    if len(body.agents) > policy.max_agents:
        raise PlanningError(
            f"proposal has {len(body.agents)} Agents; policy permits "
            f"{policy.max_agents}"
        )
    if body.max_concurrency > policy.max_concurrency:
        raise PlanningError(
            f"proposal concurrency {body.max_concurrency} exceeds the policy "
            f"ceiling of {policy.max_concurrency}"
        )
    if len(body.agents) > policy.budget.max_calls:
        raise PlanningError("proposal Agent count exceeds the approved call budget")
    planned_calls = len(body.agents) * body.iteration_limit
    if planned_calls > policy.budget.max_calls:
        raise PlanningError(
            f"proposal requires up to {planned_calls} planned Agent calls, but the "
            f"approved budget permits {policy.budget.max_calls}"
        )
    profile_criterion_ids = {
        criterion.id for criterion in policy.profile_acceptance_criteria
    }
    proposed_criterion_ids = {criterion.id for criterion in body.acceptance_criteria}
    collisions = profile_criterion_ids & proposed_criterion_ids
    if collisions:
        raise PlanningError(
            "proposal repeats controller-owned profile criteria: "
            + ", ".join(sorted(collisions))
        )
    if policy.require_review_agent and not any(
        agent.capability is AgentCapability.REVIEW for agent in body.agents
    ):
        raise PlanningError(
            "this execution profile requires an independent review Agent"
        )
    review_count = sum(
        agent.capability is AgentCapability.REVIEW for agent in body.agents
    )
    if review_count > policy.max_review_agents:
        raise PlanningError(
            f"proposal has {review_count} review Agents; this profile permits "
            f"{policy.max_review_agents}"
        )

    constraints = tuple(dict.fromkeys((*request.base_constraints, *body.constraints)))
    task_brief = TaskBrief(
        run_id=request.run_id,
        title=body.title,
        source_request=request.source_request,
        requirements=list(body.requirements),
        acceptance_criteria=[
            AcceptanceCriterion.model_validate(item.model_dump(mode="json"))
            for item in body.acceptance_criteria
        ]
        + list(policy.profile_acceptance_criteria),
        constraints=list(constraints),
        assumptions=list(body.assumptions),
        open_questions=[],
        confirmed=True,
    )
    implementation_plan = AdaptiveImplementationPlan(
        run_id=request.run_id,
        team_id="adaptive_team",
        revision=proposal.revision,
        created_at=created_at,
        objective=body.objective,
        approach=body.approach,
        tasks=body.tasks,
        risks=body.risks,
        assumptions=body.assumptions,
    )

    routing_policy = policy.model_routing or ModelRoutingPolicy(
        mode=ModelRoutingMode.STRICT,
        profiles=(
            ModelProfile(
                id="default",
                model=request.model,
                capabilities=tuple(AgentCapability),
            ),
        ),
        default_profile_id="default",
    )
    if routing_policy.get_profile(routing_policy.default_profile_id).model != (
        request.model
    ):
        raise PlanningError(
            "Planning request model differs from the routing policy bootstrap model"
        )
    try:
        model_routes = resolve_model_route_plan(
            routing_policy,
            body.agents,
            agent_profile_overrides=proposal.model_profile_overrides,
        )
    except ModelRoutingError as error:
        raise PlanningError(str(error)) from error
    assignments = {
        assignment.agent_id: assignment for assignment in model_routes.assignments
    }

    agents = []
    timeout_resolutions = []
    for proposed in body.agents:
        timeout_policy = policy.capability_timeouts[proposed.capability]
        override = proposal.timeout_overrides_seconds.get(proposed.id)
        if override is not None and not (
            timeout_policy.default_seconds <= override <= timeout_policy.ceiling_seconds
        ):
            raise PlanningError(
                f"Agent {proposed.id} timeout override {override}s is outside the "
                f"{proposed.capability.value} policy envelope of "
                f"{timeout_policy.default_seconds}.."
                f"{timeout_policy.ceiling_seconds}s"
            )
        resolved_timeout = (
            timeout_policy.resolve(proposed.workload) if override is None else override
        )
        timeout_resolutions.append(
            AgentTimeoutResolution(
                agent_id=proposed.id,
                workload=proposed.workload,
                default_seconds=timeout_policy.default_seconds,
                ceiling_seconds=timeout_policy.ceiling_seconds,
                resolved_seconds=resolved_timeout,
                source=("policy_workload" if override is None else "user_override"),
            )
        )
        agents.append(
            AgentSpec(
                id=proposed.id,
                label=proposed.label,
                responsibility=proposed.responsibility,
                rationale=proposed.rationale,
                capability=proposed.capability,
                permission_profile=permission_for_capability(proposed.capability),
                stage_id=proposed.stage_id,
                dependencies=proposed.dependencies,
                expected_output=expected_output_for_capability(proposed.capability),
                model_route_id=assignments[proposed.id].primary_route_id,
                timeout_seconds=resolved_timeout,
                workspace_scope=proposed.workspace_scope,
            )
        )
    team_plan = TeamPlan(
        plan_id=f"{request.run_id}-team-r{proposal.revision}",
        revision=proposal.revision,
        run_id=request.run_id,
        task_brief_sha256=canonical_model_sha256(task_brief),
        implementation_plan_sha256=canonical_model_sha256(implementation_plan),
        team_id="adaptive_team",
        origin=TeamPlanOrigin.ADAPTIVE_PLANNING,
        approval_source=PlanApprovalSource.USER,
        created_at=created_at,
        agents=tuple(agents),
        model_routes=model_routes,
        budget=policy.budget,
        iteration_limit=body.iteration_limit,
        max_concurrency=body.max_concurrency,
        independent_review=True,
        revision_enabled=body.revision_enabled,
    )
    return PlanningPreview(
        destination=request.destination,
        execution_profile=request.execution_profile,
        task_brief=task_brief,
        implementation_plan=implementation_plan,
        team_plan=team_plan,
        timeout_resolutions=tuple(timeout_resolutions),
    )


def apply_structured_edit(
    proposal: PlanningProposal,
    edit: StructuredPlanEdit,
    *,
    created_at: datetime,
) -> PlanningProposal:
    """Create one new proposal revision through a bounded safe edit."""

    body = proposal.body
    timeout_overrides = dict(proposal.timeout_overrides_seconds)
    model_overrides = dict(proposal.model_profile_overrides)
    if edit.kind is StructuredEditKind.MAX_CONCURRENCY:
        assert isinstance(edit.value, int)
        body = body.model_copy(update={"max_concurrency": edit.value})
        description = f"Set maximum concurrency to {edit.value}."
    elif edit.kind is StructuredEditKind.ITERATION_LIMIT:
        assert isinstance(edit.value, int)
        body = body.model_copy(
            update={
                "iteration_limit": edit.value,
                "revision_enabled": edit.value > 1,
            }
        )
        description = f"Set iteration limit to {edit.value}."
    elif edit.kind is StructuredEditKind.AGENT_TIMEOUT:
        if edit.agent_id not in {agent.id for agent in body.agents}:
            raise PlanningError(f"unknown Agent for timeout edit: {edit.agent_id}")
        assert edit.agent_id is not None
        assert isinstance(edit.value, int)
        timeout_overrides[edit.agent_id] = edit.value
        description = f"Set {edit.agent_id} timeout to {edit.value} seconds."
    else:
        if edit.agent_id not in {agent.id for agent in body.agents}:
            raise PlanningError(f"unknown Agent for model edit: {edit.agent_id}")
        assert edit.agent_id is not None
        assert isinstance(edit.value, str)
        model_overrides[edit.agent_id] = edit.value
        description = f"Set {edit.agent_id} model profile to {edit.value}."
    body = PlanningProposalBody.model_validate(body.model_dump(mode="json"))
    return PlanningProposal(
        run_id=proposal.run_id,
        revision=proposal.revision + 1,
        created_at=created_at,
        source=PlanningProposalSource.STRUCTURED_EDIT,
        change_request=description,
        body=body,
        timeout_overrides_seconds=timeout_overrides,
        model_profile_overrides=model_overrides,
    )


def _render_model_pricing(route: ModelRoute) -> str:
    """Render one secret-free route price without inventing a zero estimate."""

    if route.input_cost_per_million_usd is None:
        return "not configured"
    return (
        f"${route.input_cost_per_million_usd} input / "
        f"${route.output_cost_per_million_usd} output per million tokens"
    )


def render_planning_overview(preview: PlanningPreview) -> str:
    """Render every material decision a user approves before execution."""

    brief = preview.task_brief
    implementation = preview.implementation_plan
    plan = preview.team_plan
    lines = [
        "Planning overview",
        f"  Product: {brief.title}",
        f"  Request: {brief.source_request}",
        f"  Destination: {preview.destination}",
        "  Execution profile:",
        *(f"    - {item}" for item in preview.execution_profile),
        "  Requirements:",
        *(f"    - {item}" for item in brief.requirements),
        "  Acceptance criteria:",
        *(
            f"    - {item.id}: {item.description} (verify: {item.verification})"
            for item in brief.acceptance_criteria
        ),
        "  Implementation approach:",
        *(f"    - {item}" for item in implementation.approach),
        "  Tasks:",
        *(
            f"    - {task.id} -> {task.owner_agent_id}: {task.description}"
            for task in implementation.tasks
        ),
        "  Runtime Agents:",
    ]
    resolutions = {
        resolution.agent_id: resolution for resolution in preview.timeout_resolutions
    }
    for agent in plan.agents:
        dependencies = ", ".join(agent.dependencies) or "none"
        route = plan.model_routes.get_route(agent.model_route_id)
        assignment = plan.model_routes.get_assignment(agent.id)
        fallback_routes = tuple(
            plan.model_routes.get_route(route_id)
            for route_id in assignment.fallback_route_ids
        )
        timeout = resolutions[agent.id]
        timeout_source = (
            f"controller policy from {timeout.workload.value} workload"
            if timeout.source == "policy_workload"
            else "user override"
        )
        lines.extend(
            (
                f"    - {agent.id} ({agent.label})",
                f"      responsibility: {agent.responsibility}",
                f"      why: {agent.rationale}",
                f"      capability: {agent.capability.value}",
                f"      dependencies: {dependencies}",
                f"      permission: {agent.permission_profile.value}",
                f"      workspace: {agent.workspace_scope}",
                f"      model: {route.model} (profile {route.id}; "
                f"{assignment.selection_source.value})",
                f"      model reason: {assignment.reason}",
                "      authorized fallback profiles: "
                + (
                    ", ".join(
                        f"{fallback.id}: {fallback.model} "
                        f"(pricing: {_render_model_pricing(fallback)})"
                        for fallback in fallback_routes
                    )
                    if fallback_routes
                    else "none"
                ),
                f"      model pricing: {_render_model_pricing(route)}",
                f"      workload: {timeout.workload.value}",
                f"      timeout: {agent.timeout_seconds} seconds ({timeout_source}; "
                f"allowed {timeout.default_seconds}..{timeout.ceiling_seconds})",
            )
        )
    waves = " -> ".join(" + ".join(wave) for wave in plan.execution_waves())
    lines.extend(
        (
            "  Controller limits:",
            f"    - execution order: {waves}",
            f"    - maximum parallel Agents: {plan.max_concurrency}",
            f"    - implementation iterations: {plan.iteration_limit}",
            f"    - model routing: {plan.model_routes.mode.value}",
            "    - authorized model switches: "
            + (
                ", ".join(
                    condition.value
                    for condition in plan.model_routes.authorized_switch_conditions
                )
                if plan.model_routes.authorized_switch_conditions
                else "none"
            ),
            f"    - model calls: {plan.budget.max_calls}",
            f"    - input tokens: {plan.budget.max_input_tokens}",
            f"    - output tokens: {plan.budget.max_output_tokens}",
            "    - cumulative Agent time: "
            f"{plan.budget.max_agent_duration_seconds} seconds",
            f"    - estimated cost ceiling: ${plan.budget.max_estimated_cost_usd}",
            "    - independent testing and review: required",
        )
    )
    if brief.constraints:
        lines.append("  Constraints:")
        lines.extend(f"    - {item}" for item in brief.constraints)
    if implementation.risks:
        lines.append("  Risks:")
        lines.extend(f"    - {item}" for item in implementation.risks)
    return "\n".join(lines)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class PlanningStore:
    """Write-once turns/proposals with an atomic, integrity-checked session index."""

    def __init__(self, root: Path) -> None:
        if not root.is_absolute():
            raise PlanningError("Planning store root must be absolute")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if root.is_symlink() or not root.is_dir():
            raise PlanningError("Planning store root must be a real directory")
        root.chmod(0o700)
        self.root = root

    def _directory(self, run_id: str) -> Path:
        if re.fullmatch(r"[a-z0-9][a-z0-9_-]*", run_id) is None:
            raise PlanningError("Planning run ID is invalid")
        return self.root / run_id

    @staticmethod
    def _indexed_files(directory: Path) -> set[int]:
        if not directory.exists():
            return set()
        if directory.is_symlink() or not directory.is_dir():
            raise PlanningIntegrityError(
                f"Planning evidence path is not a real directory: {directory}"
            )
        indexes: set[int] = set()
        for path in directory.iterdir():
            if path.is_symlink() or not path.is_file():
                raise PlanningIntegrityError(
                    f"Planning evidence entry is not a regular file: {path}"
                )
            match = re.fullmatch(r"([0-9]{3})\.json", path.name)
            if match is None:
                raise PlanningIntegrityError(
                    f"unexpected Planning evidence file: {path.name}"
                )
            indexes.add(int(match.group(1)))
        return indexes

    def _write_once(self, destination: Path, model: BaseModel) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if destination.exists() or destination.is_symlink():
            raise PlanningIntegrityError(
                f"Planning evidence already exists: {destination}"
            )
        content = (json.dumps(model.model_dump(mode="json"), indent=2) + "\n").encode()
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        _fsync_directory(destination.parent)

    def _write_session(self, session: PlanningSession) -> None:
        directory = self._directory(session.run_id)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = directory / "session.json"
        if destination.is_symlink():
            raise PlanningIntegrityError("Planning session index cannot be a symlink")
        content = (
            json.dumps(session.model_dump(mode="json"), indent=2) + "\n"
        ).encode()
        temporary = directory / f".session.{uuid4().hex}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, destination)
            _fsync_directory(directory)
        finally:
            if temporary.exists():
                temporary.unlink()

    def create(self, request: PlanningRequest) -> PlanningSession:
        directory = self._directory(request.run_id)
        if directory.exists() or directory.is_symlink():
            raise PlanningIntegrityError(
                f"Planning session already exists: {request.run_id}"
            )
        directory.mkdir(parents=True, mode=0o700)
        self._write_once(directory / "request.json", request)
        session = PlanningSession(
            run_id=request.run_id,
            request_sha256=canonical_model_sha256(request),
            status=PlanningSessionStatus.AUTHORIZED,
            created_at=request.authorized_at,
            updated_at=request.authorized_at,
        )
        self._write_session(session)
        return session

    def load_request(self, run_id: str) -> PlanningRequest:
        path = self._directory(run_id) / "request.json"
        if path.is_symlink() or not path.is_file():
            raise PlanningIntegrityError("Planning request evidence is missing")
        request = PlanningRequest.model_validate_json(path.read_text(encoding="utf-8"))
        if request.run_id != run_id:
            raise PlanningIntegrityError("Planning request belongs to a different run")
        return request

    def load_session(self, run_id: str, *, verify: bool = True) -> PlanningSession:
        path = self._directory(run_id) / "session.json"
        if path.is_symlink() or not path.is_file():
            raise PlanningIntegrityError("Planning session index is missing")
        session = PlanningSession.model_validate_json(path.read_text(encoding="utf-8"))
        if session.run_id != run_id:
            raise PlanningIntegrityError("Planning session belongs to a different run")
        if canonical_model_sha256(self.load_request(run_id)) != session.request_sha256:
            raise PlanningIntegrityError("Planning request digest changed")
        if verify:
            turn_indexes = self._indexed_files(self._directory(run_id) / "turns")
            if turn_indexes != set(range(1, session.turn_count + 1)):
                raise PlanningIntegrityError(
                    "Planning turn files differ from the session anchor"
                )
            previous: str | None = None
            for sequence in range(1, session.turn_count + 1):
                turn = self.load_turn(run_id, sequence)
                if turn.previous_sha256 != previous:
                    raise PlanningIntegrityError("Planning turn hash chain is broken")
                previous = canonical_model_sha256(turn)
            if previous != session.turn_head_sha256:
                raise PlanningIntegrityError("Planning turn head digest changed")
            proposal_count = session.latest_proposal_revision or 0
            proposal_indexes = self._indexed_files(
                self._directory(run_id) / "proposals"
            )
            if proposal_indexes != set(range(1, proposal_count + 1)):
                raise PlanningIntegrityError(
                    "Planning proposal files differ from the session anchor"
                )
            for revision in range(1, proposal_count + 1):
                self.load_proposal(run_id, revision)
            approval_indexes = self._indexed_files(
                self._directory(run_id) / "approvals"
            )
            if session.approved_revision is not None:
                if approval_indexes != {session.approved_revision}:
                    raise PlanningIntegrityError(
                        "Planning approval files differ from the session anchor"
                    )
                self.load_approval(run_id, session.approved_revision)
            elif approval_indexes:
                raise PlanningIntegrityError(
                    "unapproved Planning session contains approval evidence"
                )
        return session

    def load_turn(self, run_id: str, sequence: int) -> PlanningTurn:
        path = self._directory(run_id) / "turns" / f"{sequence:03d}.json"
        if path.is_symlink() or not path.is_file():
            raise PlanningIntegrityError(f"Planning turn {sequence} is missing")
        turn = PlanningTurn.model_validate_json(path.read_text(encoding="utf-8"))
        if turn.run_id != run_id or turn.sequence != sequence:
            raise PlanningIntegrityError(
                "Planning turn context does not match its path"
            )
        return turn

    def append_turn(
        self,
        *,
        run_id: str,
        user_message: str,
        prompt: str,
        result: AgentExecutionResult,
        parsed_response: PlanningModelResponse | None,
        response_normalizations: tuple[str, ...],
        validation_error: str | None,
        now: datetime,
    ) -> PlanningTurn:
        session = self.load_session(run_id)
        if session.status in {
            PlanningSessionStatus.APPROVED,
            PlanningSessionStatus.CANCELLED,
        }:
            raise PlanningError("terminal Planning session cannot accept another turn")
        sequence = session.turn_count + 1
        response_text = result.response_text
        usage = result.telemetry.usage
        turn = PlanningTurn(
            run_id=run_id,
            sequence=sequence,
            previous_sha256=session.turn_head_sha256,
            user_message=user_message,
            prompt=prompt,
            prompt_sha256=_digest_text(prompt),
            response_text=response_text,
            response_sha256=(
                None if response_text is None else _digest_text(response_text)
            ),
            parsed_response=parsed_response,
            response_normalizations=response_normalizations,
            validation_error=validation_error,
            execution=PlanningExecutionEvidence(
                status=result.status,
                session_key=result.telemetry.session_key,
                started_at=result.telemetry.started_at,
                finished_at=result.telemetry.finished_at,
                duration_ms=result.telemetry.duration_ms,
                provider=result.telemetry.provider,
                model=result.telemetry.model,
                input_tokens=None if usage is None else usage.input_tokens,
                output_tokens=None if usage is None else usage.output_tokens,
                error=result.error,
            ),
        )
        self._write_once(
            self._directory(run_id) / "turns" / f"{sequence:03d}.json",
            turn,
        )
        self._write_session(
            session.model_copy(
                update={
                    "status": PlanningSessionStatus.CLARIFYING,
                    "updated_at": _utc(now),
                    "turn_count": sequence,
                    "turn_head_sha256": canonical_model_sha256(turn),
                }
            )
        )
        return turn

    def append_proposal(self, proposal: PlanningProposal, *, now: datetime) -> None:
        session = self.load_session(proposal.run_id)
        expected = (session.latest_proposal_revision or 0) + 1
        if proposal.revision != expected:
            raise PlanningIntegrityError(
                f"proposal revision must be {expected}, got {proposal.revision}"
            )
        if proposal.source_turn_sequence is not None:
            turn = self.load_turn(proposal.run_id, proposal.source_turn_sequence)
            if (
                turn.parsed_response is None
                or turn.parsed_response.kind is not PlanningResponseKind.PROPOSAL
                or turn.parsed_response.proposal != proposal.body
            ):
                raise PlanningIntegrityError("proposal does not match its model turn")
        self._write_once(
            self._directory(proposal.run_id)
            / "proposals"
            / f"{proposal.revision:03d}.json",
            proposal,
        )
        self._write_session(
            session.model_copy(
                update={
                    "status": PlanningSessionStatus.PROPOSED,
                    "updated_at": _utc(now),
                    "latest_proposal_revision": proposal.revision,
                }
            )
        )

    def load_proposal(self, run_id: str, revision: int) -> PlanningProposal:
        path = self._directory(run_id) / "proposals" / f"{revision:03d}.json"
        if path.is_symlink() or not path.is_file():
            raise PlanningIntegrityError(f"Planning proposal {revision} is missing")
        proposal = PlanningProposal.model_validate_json(
            path.read_text(encoding="utf-8")
        )
        if proposal.run_id != run_id or proposal.revision != revision:
            raise PlanningIntegrityError(
                "Planning proposal context does not match its path"
            )
        return proposal

    def approve(self, approval: PlanningApproval, *, now: datetime) -> None:
        session = self.load_session(approval.run_id)
        if session.status is not PlanningSessionStatus.PROPOSED:
            raise PlanningError("only a proposed Planning session can be approved")
        if approval.revision != session.latest_proposal_revision:
            raise PlanningIntegrityError("approval must bind the latest proposal")
        proposal = self.load_proposal(approval.run_id, approval.revision)
        if canonical_model_sha256(proposal) != approval.proposal_sha256:
            raise PlanningIntegrityError("approval proposal digest does not match")
        self._write_once(
            self._directory(approval.run_id)
            / "approvals"
            / f"{approval.revision:03d}.json",
            approval,
        )
        self._write_session(
            session.model_copy(
                update={
                    "status": PlanningSessionStatus.APPROVED,
                    "updated_at": _utc(now),
                    "approved_revision": approval.revision,
                }
            )
        )

    def load_approval(self, run_id: str, revision: int) -> PlanningApproval:
        path = self._directory(run_id) / "approvals" / f"{revision:03d}.json"
        if path.is_symlink() or not path.is_file():
            raise PlanningIntegrityError(f"Planning approval {revision} is missing")
        approval = PlanningApproval.model_validate_json(
            path.read_text(encoding="utf-8")
        )
        if approval.run_id != run_id or approval.revision != revision:
            raise PlanningIntegrityError(
                "Planning approval context does not match its path"
            )
        return approval

    def cancel(self, run_id: str, *, now: datetime) -> None:
        session = self.load_session(run_id)
        if session.status is PlanningSessionStatus.APPROVED:
            raise PlanningError("approved Planning session cannot be cancelled")
        self._write_session(
            session.model_copy(
                update={
                    "status": PlanningSessionStatus.CANCELLED,
                    "updated_at": _utc(now),
                }
            )
        )


@dataclass(frozen=True)
class _Invocation:
    response: PlanningModelResponse
    turn: PlanningTurn


class AdaptivePlanningCoordinator:
    """Run bounded dialogue while retaining approval and lifecycle authority."""

    def __init__(
        self,
        *,
        executor: AgentExecutor,
        store: PlanningStore,
        policy: PlanningPolicy,
        clock: Clock = _system_clock,
    ) -> None:
        self.executor = executor
        self.store = store
        self.policy = policy
        self.clock = clock

    def start(
        self,
        request: PlanningRequest,
        *,
        answer_question: QuestionAnswerer,
    ) -> PlanningProposal | None:
        """Ask only high-value questions, then persist one validated proposal."""

        self.store.create(request)
        transcript: list[dict[str, object]] = []
        user_message = request.source_request
        clarification_rounds = 0
        while True:
            invocation = self._invoke(
                request,
                user_message=user_message,
                transcript=transcript,
                current_proposal=None,
            )
            response = invocation.response
            if response.kind is PlanningResponseKind.PROPOSAL:
                assert response.proposal is not None
                proposal = PlanningProposal(
                    run_id=request.run_id,
                    revision=1,
                    created_at=_utc(self.clock()),
                    source=PlanningProposalSource.MODEL,
                    source_turn_sequence=invocation.turn.sequence,
                    body=response.proposal,
                )
                self._validate_preview(request, proposal)
                self.store.append_proposal(proposal, now=self.clock())
                return proposal
            assert response.question is not None
            if clarification_rounds >= self.policy.max_clarification_rounds:
                raise PlanningError("Planning exceeded its clarification-round limit")
            answer = answer_question(response.question)
            if answer is None:
                self.store.cancel(request.run_id, now=self.clock())
                return None
            answer = _clean_text(answer, label="clarification answer")
            transcript.append(
                {
                    "question": response.question.model_dump(mode="json"),
                    "answer": answer,
                }
            )
            user_message = answer
            clarification_rounds += 1

    def revise(
        self,
        request: PlanningRequest,
        proposal: PlanningProposal,
        change_request: str,
        *,
        answer_question: QuestionAnswerer,
    ) -> PlanningProposal | None:
        """Use natural language to produce a complete replacement revision."""

        if proposal.revision >= self.policy.max_proposal_revisions:
            raise PlanningError("Planning reached its proposal-revision limit")
        change_request = _clean_text(change_request, label="proposal change request")
        transcript: list[dict[str, object]] = []
        user_message = change_request
        clarification_rounds = 0
        while True:
            invocation = self._invoke(
                request,
                user_message=user_message,
                transcript=transcript,
                current_proposal=proposal,
                change_request=change_request,
            )
            response = invocation.response
            if response.kind is PlanningResponseKind.PROPOSAL:
                assert response.proposal is not None
                revision = PlanningProposal(
                    run_id=request.run_id,
                    revision=proposal.revision + 1,
                    created_at=_utc(self.clock()),
                    source=PlanningProposalSource.MODEL,
                    source_turn_sequence=invocation.turn.sequence,
                    change_request=change_request,
                    body=response.proposal,
                )
                self._validate_preview(request, revision)
                self.store.append_proposal(revision, now=self.clock())
                return revision
            assert response.question is not None
            if clarification_rounds >= self.policy.max_clarification_rounds:
                raise PlanningError("Planning revision exceeded its question limit")
            answer = answer_question(response.question)
            if answer is None:
                self.store.cancel(request.run_id, now=self.clock())
                return None
            answer = _clean_text(answer, label="clarification answer")
            transcript.append(
                {
                    "question": response.question.model_dump(mode="json"),
                    "answer": answer,
                }
            )
            user_message = answer
            clarification_rounds += 1

    def structured_edit(
        self,
        request: PlanningRequest,
        proposal: PlanningProposal,
        edit: StructuredPlanEdit,
    ) -> PlanningProposal:
        """Validate and persist one safe non-model proposal revision."""

        if proposal.revision >= self.policy.max_proposal_revisions:
            raise PlanningError("Planning reached its proposal-revision limit")
        revision = apply_structured_edit(proposal, edit, created_at=self.clock())
        self._validate_preview(request, revision)
        self.store.append_proposal(revision, now=self.clock())
        return revision

    def approve(
        self,
        request: PlanningRequest,
        proposal: PlanningProposal,
    ) -> ApprovedPlanningResult:
        """Freeze the exact validated TeamPlan only after explicit user approval."""

        approved_at = _utc(self.clock())
        preview = preview_adaptive_proposal(
            request,
            proposal,
            self.policy,
            created_at=approved_at,
        )
        approval = PlanningApproval(
            run_id=request.run_id,
            revision=proposal.revision,
            approved_at=approved_at,
            confirmation="user_approved",
            proposal_sha256=canonical_model_sha256(proposal),
            task_brief_sha256=canonical_model_sha256(preview.task_brief),
            implementation_plan_sha256=canonical_model_sha256(
                preview.implementation_plan
            ),
            team_plan_sha256=canonical_model_sha256(preview.team_plan),
            timeout_resolutions=preview.timeout_resolutions,
        )
        self.store.approve(approval, now=approved_at)
        return ApprovedPlanningResult(
            task_brief=preview.task_brief,
            implementation_plan=preview.implementation_plan,
            team_plan=preview.team_plan,
            approval=approval,
        )

    def _validate_preview(
        self,
        request: PlanningRequest,
        proposal: PlanningProposal,
    ) -> PlanningPreview:
        try:
            return preview_adaptive_proposal(
                request,
                proposal,
                self.policy,
                created_at=proposal.created_at,
            )
        except (ValueError, PlanningError) as error:
            raise PlanningError(f"proposed TeamPlan is invalid: {error}") from error

    def _invoke(
        self,
        request: PlanningRequest,
        *,
        user_message: str,
        transcript: list[dict[str, object]],
        current_proposal: PlanningProposal | None,
        change_request: str | None = None,
    ) -> _Invocation:
        previous_error: str | None = None
        for attempt in range(1, self.policy.response_repair_limit + 2):
            prompt = self._prompt(
                request,
                transcript=transcript,
                current_proposal=current_proposal,
                change_request=change_request,
                previous_error=previous_error,
            )
            execution_request = AgentExecutionRequest(
                run_id=request.run_id,
                team_id="adaptive_planning",
                iteration=1,
                role=AgentRole.CLARIFIER,
                expected_kind=ArtifactKind.CLARIFICATION_RECORD,
                prompt=prompt,
                timeout_seconds=self.policy.planning_timeout_seconds,
                model=request.model,
            )
            result = self.executor.execute(execution_request)
            parsed: PlanningModelResponse | None = None
            response_normalizations: tuple[str, ...] = ()
            validation_error: str | None = None
            if result.status is not AgentExecutionStatus.COMPLETED:
                validation_error = (
                    result.error or f"Planning execution ended as {result.status.value}"
                )
            else:
                try:
                    payload = parse_json_object_response(result.response_text)
                    payload, response_normalizations = (
                        _normalize_planning_response_payload(payload)
                    )
                    parsed = PlanningModelResponse.model_validate(payload)
                    if parsed.kind is PlanningResponseKind.PROPOSAL:
                        assert parsed.proposal is not None
                        candidate = PlanningProposal(
                            run_id=request.run_id,
                            revision=(
                                1
                                if current_proposal is None
                                else current_proposal.revision + 1
                            ),
                            created_at=_utc(self.clock()),
                            source=PlanningProposalSource.MODEL,
                            source_turn_sequence=1,
                            change_request=change_request,
                            body=parsed.proposal,
                        )
                        self._validate_preview(request, candidate)
                except (
                    AgentArtifactResponseError,
                    PlanningError,
                    ValidationError,
                    ValueError,
                ) as error:
                    validation_error = _safe_validation_detail(error)
                    parsed = None
            turn = self.store.append_turn(
                run_id=request.run_id,
                user_message=user_message,
                prompt=prompt,
                result=result,
                parsed_response=parsed,
                response_normalizations=response_normalizations,
                validation_error=validation_error,
                now=self.clock(),
            )
            if parsed is not None:
                return _Invocation(response=parsed, turn=turn)
            previous_error = validation_error
            if attempt > self.policy.response_repair_limit:
                raise PlanningError(
                    f"Planning response remained invalid: {validation_error}"
                )
        raise AssertionError("unreachable Planning repair state")

    def _prompt(
        self,
        request: PlanningRequest,
        *,
        transcript: list[dict[str, object]],
        current_proposal: PlanningProposal | None,
        change_request: str | None,
        previous_error: str | None,
    ) -> str:
        template = Template(PLANNING_TEMPLATE.read_text(encoding="utf-8"))
        context = {
            "request": request.model_dump(mode="json"),
            "dialogue": transcript,
            "current_proposal": (
                None
                if current_proposal is None
                else current_proposal.body.model_dump(mode="json")
            ),
            "change_request": change_request,
            "controller_policy": {
                "maximum_agents": self.policy.max_agents,
                "maximum_concurrency": self.policy.max_concurrency,
                "maximum_iterations": 3,
                "maximum_agent_calls": self.policy.budget.max_calls,
                "maximum_review_agents": self.policy.max_review_agents,
                "profile_acceptance_criteria": [
                    criterion.model_dump(mode="json")
                    for criterion in self.policy.profile_acceptance_criteria
                ],
                "requires_independent_review_agent": (self.policy.require_review_agent),
                "capability_timeout_profiles": {
                    capability.value: {
                        "routine_seconds": timeout.default_seconds,
                        "substantial_seconds": timeout.resolve(
                            AgentWorkload.SUBSTANTIAL
                        ),
                        "complex_seconds": timeout.ceiling_seconds,
                    }
                    for capability, timeout in self.policy.capability_timeouts.items()
                },
                "runtime_capabilities": [
                    AgentCapability.IMPLEMENTATION.value,
                    AgentCapability.INTEGRATION.value,
                    AgentCapability.TESTING.value,
                    AgentCapability.REVIEW.value,
                ],
                "model_routing": (
                    {
                        "mode": self.policy.model_routing.mode.value,
                        "profiles": [
                            {
                                "id": profile.id,
                                "model": profile.model,
                                "eligible_agent_capabilities": [
                                    capability.value
                                    for capability in profile.capabilities
                                ],
                            }
                            for profile in self.policy.model_routing.profiles
                        ],
                        "instruction": (
                            "Describe capability and workload needs only. The "
                            "controller resolves model profiles; do not add model "
                            "fields to the proposal."
                        ),
                    }
                    if self.policy.model_routing is not None
                    else {
                        "mode": ModelRoutingMode.STRICT.value,
                        "instruction": "All runtime Agents use the pinned model.",
                    }
                ),
            },
        }
        repair = (
            None
            if previous_error is None
            else {
                "previous_response_rejected": previous_error,
                "instruction": (
                    "Revalidate the complete response against the schema and policy. "
                    "Return one corrected object, not a patch."
                ),
            }
        )
        return template.substitute(
            planning_context_json=json.dumps(context, ensure_ascii=False, indent=2),
            response_schema_json=json.dumps(
                PlanningModelResponse.model_json_schema(),
                ensure_ascii=False,
                indent=2,
            ),
            repair_context_json=json.dumps(repair, ensure_ascii=False, indent=2),
        )


def _interactive_question_answerer(
    *,
    read: InputReader,
    write: OutputWriter,
) -> QuestionAnswerer:
    def answer(question: PlanningQuestion) -> str | None:
        write("")
        write(f"Planning question: {question.text}")
        write(f"Why this matters: {question.why}")
        for index, option in enumerate(question.options, start=1):
            write(f"  {index}. {option.label} — {option.description}")
        write("  c. Custom answer")
        write("  x. Cancel")
        while True:
            choice = read("Choose an option or enter a custom answer: ").strip()
            if choice.casefold() in {"x", "cancel"}:
                return None
            if choice.casefold() in {"c", "custom"}:
                custom = read("Your answer: ").strip()
                if custom:
                    return custom
                write("Please enter a non-empty answer.")
                continue
            if choice.isdigit() and 1 <= int(choice) <= len(question.options):
                selected = question.options[int(choice) - 1]
                return f"{selected.label}: {selected.description}"
            if choice:
                return choice
            write("Please choose a suggestion, enter a custom answer, or cancel.")

    return answer


def _read_positive_integer(
    prompt: str,
    *,
    read: InputReader,
    write: OutputWriter,
) -> int | None:
    while True:
        value = read(prompt).strip()
        if value.casefold() in {"x", "cancel"}:
            return None
        if value.isdigit() and int(value) > 0:
            return int(value)
        write("Enter a positive integer, or x to return to the overview.")


def _read_structured_edit(
    proposal: PlanningProposal,
    *,
    model_routing: ModelRoutingPolicy | None,
    read: InputReader,
    write: OutputWriter,
) -> StructuredPlanEdit | None:
    allow_model_edit = model_routing is not None and len(model_routing.profiles) > 1
    write("")
    write("Safe plan edits")
    write("  1. Maximum parallel Agents")
    write("  2. Implementation iteration limit")
    write("  3. One Agent timeout")
    if allow_model_edit:
        write("  4. One Agent model profile")
    write("  x. Return to overview")
    while True:
        choice = read("Edit: ").strip().casefold()
        if choice in {"x", "cancel"}:
            return None
        if choice == "1":
            value = _read_positive_integer(
                "Maximum parallel Agents: ", read=read, write=write
            )
            return (
                None
                if value is None
                else StructuredPlanEdit(
                    kind=StructuredEditKind.MAX_CONCURRENCY,
                    value=value,
                )
            )
        if choice == "2":
            value = _read_positive_integer(
                "Implementation iterations (1-3): ", read=read, write=write
            )
            return (
                None
                if value is None
                else StructuredPlanEdit(
                    kind=StructuredEditKind.ITERATION_LIMIT,
                    value=value,
                )
            )
        if choice == "3":
            write("Runtime Agents:")
            for agent in proposal.body.agents:
                write(f"  - {agent.id}: {agent.workload.value} workload")
            agent_id = read("Agent ID: ").strip()
            if not agent_id:
                write("Agent ID must not be blank.")
                continue
            value = _read_positive_integer(
                "Timeout in seconds (at least 30): ", read=read, write=write
            )
            return (
                None
                if value is None
                else StructuredPlanEdit(
                    kind=StructuredEditKind.AGENT_TIMEOUT,
                    agent_id=agent_id,
                    value=value,
                )
            )
        if choice == "4" and allow_model_edit:
            assert model_routing is not None
            write("Runtime Agents:")
            for agent in proposal.body.agents:
                write(f"  - {agent.id}: {agent.capability.value}")
            agent_id = read("Agent ID: ").strip()
            if not agent_id:
                write("Agent ID must not be blank.")
                continue
            write("Configured model profiles:")
            for profile in model_routing.profiles:
                capabilities = ", ".join(
                    capability.value for capability in profile.capabilities
                )
                write(f"  - {profile.id}: {profile.model} ({capabilities})")
            profile_id = read("Model profile ID: ").strip()
            if not profile_id:
                write("Model profile ID must not be blank.")
                continue
            return StructuredPlanEdit(
                kind=StructuredEditKind.AGENT_MODEL,
                agent_id=agent_id,
                value=profile_id,
            )
        choices = "1, 2, 3, 4, or x" if allow_model_edit else "1, 2, 3, or x"
        write(f"Choose {choices}.")


def run_interactive_planning(
    coordinator: AdaptivePlanningCoordinator,
    request: PlanningRequest,
    *,
    read: InputReader = input,
    write: OutputWriter = print,
) -> ApprovedPlanningResult | None:
    """Run the user-facing clarification, overview, revision, and approval loop."""

    answer_question = _interactive_question_answerer(read=read, write=write)
    write("")
    write("Planning started. No runtime Agent has been created yet.")
    proposal = coordinator.start(request, answer_question=answer_question)
    if proposal is None:
        write("Planning cancelled; no runtime Agent was created.")
        return None

    while True:
        preview = preview_adaptive_proposal(
            request,
            proposal,
            coordinator.policy,
            created_at=proposal.created_at,
        )
        write("")
        write(render_planning_overview(preview))
        write("")
        write("  a. Approve and allow the controller to create this team")
        write("  r. Request changes in your own words")
        write("  e. Edit safe limits")
        write("  c. Cancel")
        choice = read("Review choice: ").strip().casefold()
        if choice in {"a", "approve"}:
            approved = coordinator.approve(request, proposal)
            write(
                f"Plan revision {approved.approval.revision} approved. "
                "The controller may now create only the Agents shown above."
            )
            return approved
        if choice in {"c", "cancel"}:
            coordinator.store.cancel(request.run_id, now=coordinator.clock())
            write("Planning cancelled; no runtime Agent was created.")
            return None
        if choice in {"r", "revise"}:
            change = read("Describe the changes you want: ").strip()
            if not change:
                write("Change request must not be blank.")
                continue
            try:
                revised = coordinator.revise(
                    request,
                    proposal,
                    change,
                    answer_question=answer_question,
                )
            except PlanningError as error:
                write(f"Plan was not changed: {error}")
                continue
            if revised is None:
                write("Planning cancelled; no runtime Agent was created.")
                return None
            proposal = revised
            continue
        if choice in {"e", "edit"}:
            try:
                edit = _read_structured_edit(
                    proposal,
                    model_routing=coordinator.policy.model_routing,
                    read=read,
                    write=write,
                )
                if edit is not None:
                    proposal = coordinator.structured_edit(request, proposal, edit)
            except (PlanningError, ValidationError, ValueError) as error:
                write(f"Plan was not changed: {_safe_validation_detail(error)}")
            continue
        write("Choose a, r, e, or c.")
