"""Adaptive planning proposal schemas, body models, and validation rules.

This module owns:
    - PlanningProposalBody: Complete Pydantic model for an adaptive proposal,
      including requirements, assumptions, criteria, agents, tasks.
    - PlanningModelResponse: Wire-level model response envelope.
    - Validation functions: DAG cycle detection, agent bindings, criterion references,
      writer ownership, decision provenance, and semantic clarity checks.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Mapping
from copy import deepcopy
from pathlib import PurePosixPath
from typing import Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from software_agent_team.artifacts import (
    DeliveryMaturity,
    ProductDefinition,
    ProductDefinitionBasis,
    ProductDefinitionDimension,
    ProductDefinitionDisposition,
)
from software_agent_team.planning.common import (
    MAX_PLANNING_DECISION_SUMMARY_CHARACTERS,
    _clean_text,
    _clean_unique,
    _normalized_evidence_text,
)
from software_agent_team.planning.entities import (
    ProposedAgent,
    ProposedAssumption,
    ProposedCriterion,
    ProposedRequirement,
    ProposedTask,
)
from software_agent_team.planning.invariants import (
    _USER_DECISION_PRODUCT_DIMENSIONS,
    PlanningError,
    _planning_context_invariant,
    _planning_model_invariant,
    _planning_subjects,
    _planning_user_decision_invariant,
    _PlanningClarificationRecovery,
    _PlanningContextInvariantError,
    _PlanningContextInvariantsError,
    _PlanningInvariant,
)
from software_agent_team.planning.models_basic import (
    _DECISION_AUTHORITY,
    PlanningDecisionAuthority,
    PlanningDecisionCategory,
    PlanningDecisionProvenanceKind,
    PlanningResponseKind,
)
from software_agent_team.planning.questions import (
    _ALL_REVIEW_BOUNDARIES,
    PlanningDecisionRecord,
    PlanningQuestion,
    PlanningRequest,
    _PlanningQuestionContract,
    _requires_full_review_boundaries,
)
from software_agent_team.planning.scopes import (
    _resolve_review_scope_assignments,
    _review_task_scope_invariants,
)
from software_agent_team.response_corrections import (
    ResponseFailureClass,
    ResponseIssueAuthority,
    ResponseIssueSubject,
    ResponseIssueSubjectKind,
    SemanticCorrectionPlan,
)
from software_agent_team.teams import (
    AgentCapability,
    AgentSpec,
    workspace_scopes_overlap,
)


def validate_writer_task_scope_forecasts(
    tasks: tuple[ProposedTask, ...],
    agents: Collection[ProposedAgent | AgentSpec],
) -> None:
    """Reject writer plans whose own path forecasts exceed their write scope."""

    agents_by_id = {agent.id: (index, agent) for index, agent in enumerate(agents)}
    conflicts: list[
        tuple[int, ProposedTask, int, ProposedAgent | AgentSpec, tuple[str, ...]]
    ] = []
    for task_index, task in enumerate(tasks):
        owner_record = agents_by_id.get(task.owner_agent_id)
        if owner_record is None:
            continue  # The task-owner invariant reports this separately.
        agent_index, owner = owner_record
        if owner.capability not in {
            AgentCapability.IMPLEMENTATION,
            AgentCapability.INTEGRATION,
        }:
            continue
        scope_parts = PurePosixPath(owner.workspace_scope).parts[1:]
        outside = tuple(
            path
            for path in task.expected_paths
            if PurePosixPath(path).parts[: len(scope_parts)] != scope_parts
        )
        if not outside:
            continue
        conflicts.append((task_index, task, agent_index, owner, outside))
    if conflicts:
        details = "; ".join(
            f"{task.id} -> {owner.id} ({owner.workspace_scope}): " + ", ".join(outside)
            for _, task, _, owner, outside in conflicts[:4]
        )
        if len(conflicts) > 4:
            details += f"; plus {len(conflicts) - 4} more writer task(s)"
        raise _planning_model_invariant(
            "planning_writer_task_workspace_consistency",
            "writer tasks forecast paths outside owner workspaces: "
            + details
            + "; align task intent and owner scopes before approval",
            paths=tuple(
                path
                for task_index, _, agent_index, _, _ in conflicts
                for path in (
                    f"/proposal/tasks/{task_index}",
                    f"/proposal/agents/{agent_index}/workspace_scope",
                )
            ),
            subjects=_planning_subjects(
                *(
                    subject
                    for _, task, _, owner, _ in conflicts
                    for subject in (
                        (ResponseIssueSubjectKind.TASK, task.id),
                        (ResponseIssueSubjectKind.AGENT, owner.id),
                    )
                )
            ),
        )


def _validate_dag(
    nodes: tuple[str, ...],
    dependencies: Mapping[str, tuple[str, ...]],
    *,
    label: str,
    path_prefix: str,
    subject_kind: ResponseIssueSubjectKind,
) -> None:
    known = set(nodes)
    indexes = {node: index for index, node in enumerate(nodes)}
    for node, required in dependencies.items():
        unknown = set(required) - known
        if unknown:
            message = (
                f"{label} {node} references unknown dependencies: "
                f"{', '.join(sorted(unknown))}"
            )
            raise _planning_model_invariant(
                f"planning_{subject_kind.value}_dependency_reference",
                message,
                paths=(f"{path_prefix}/{indexes[node]}/dependencies",),
                subjects=_planning_subjects(
                    (subject_kind, node),
                    *((subject_kind, item) for item in sorted(unknown)),
                ),
            )
    visiting: list[str] = []
    active: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in active:
            cycle = tuple(visiting[visiting.index(node) :])
            raise _planning_model_invariant(
                f"planning_{subject_kind.value}_dependency_cycle",
                f"{label} dependencies must be acyclic",
                paths=tuple(
                    f"{path_prefix}/{indexes[item]}/dependencies" for item in cycle
                ),
                subjects=_planning_subjects(*((subject_kind, item) for item in cycle)),
            )
        if node in visited:
            return
        visiting.append(node)
        active.add(node)
        for dependency in dependencies[node]:
            visit(dependency)
        visiting.pop()
        active.remove(node)
        visited.add(node)

    for node in nodes:
        visit(node)


def validate_task_agent_bindings(
    tasks: tuple[ProposedTask, ...],
    agent_dependencies: Mapping[str, tuple[str, ...]],
    writer_agent_ids: Collection[str],
    *,
    require_canonical_cross_agent_projection: bool = False,
) -> None:
    """Validate task ownership and ordering against one approved Agent DAG."""

    task_ids = tuple(task.id for task in tasks)
    if len(task_ids) != len(set(task_ids)):
        duplicates = tuple(
            sorted({task_id for task_id in task_ids if task_ids.count(task_id) > 1})
        )
        raise _planning_model_invariant(
            "planning_task_id_unique",
            "proposed task IDs must be unique",
            paths=("/proposal/tasks",),
            subjects=_planning_subjects(
                *((ResponseIssueSubjectKind.TASK, item) for item in duplicates)
            ),
        )
    _validate_dag(
        task_ids,
        {task.id: task.dependencies for task in tasks},
        label="task",
        path_prefix="/proposal/tasks",
        subject_kind=ResponseIssueSubjectKind.TASK,
    )

    known_agent_ids = set(agent_dependencies)
    unknown_task_owners = {task.owner_agent_id for task in tasks} - known_agent_ids
    if unknown_task_owners:
        message = "tasks reference unknown Agent owners: " + ", ".join(
            sorted(unknown_task_owners)
        )
        raise _planning_model_invariant(
            "planning_task_owner_reference",
            message,
            paths=tuple(
                f"/proposal/tasks/{index}/owner_agent_id"
                for index, task in enumerate(tasks)
                if task.owner_agent_id in unknown_task_owners
            ),
            subjects=_planning_subjects(
                *(
                    (ResponseIssueSubjectKind.AGENT, item)
                    for item in unknown_task_owners
                ),
                *(
                    (ResponseIssueSubjectKind.TASK, task.id)
                    for task in tasks
                    if task.owner_agent_id in unknown_task_owners
                ),
            ),
        )

    writers = set(writer_agent_ids)
    task_owners = {task.owner_agent_id for task in tasks}
    unassigned_writers = writers - task_owners
    if unassigned_writers:
        message = "every implementation Agent must own at least one task: " + ", ".join(
            sorted(unassigned_writers)
        )
        raise _planning_model_invariant(
            "planning_writer_task_required",
            message,
            paths=("/proposal/tasks",),
            subjects=_planning_subjects(
                *((ResponseIssueSubjectKind.AGENT, item) for item in unassigned_writers)
            ),
        )

    def transitively_depends(agent_id: str, target: str) -> bool:
        pending = list(agent_dependencies[agent_id])
        seen: set[str] = set()
        while pending:
            current = pending.pop()
            if current == target:
                return True
            if current not in seen:
                seen.add(current)
                pending.extend(agent_dependencies[current])
        return False

    task_owner_by_id = {task.id: task.owner_agent_id for task in tasks}
    owner_dependency_paths: list[str] = []
    owner_dependency_subjects: list[tuple[ResponseIssueSubjectKind, str]] = []
    owner_dependency_details: list[str] = []
    for task_index, task in enumerate(tasks):
        for dependency_id in task.dependencies:
            dependency_owner = task_owner_by_id[dependency_id]
            if dependency_owner != task.owner_agent_id and not transitively_depends(
                task.owner_agent_id,
                dependency_owner,
            ):
                owner_dependency_paths.append(
                    f"/proposal/tasks/{task_index}/dependencies"
                )
                owner_dependency_details.append(
                    f"task {task.id} depends on {dependency_id}, but Agent "
                    f"{task.owner_agent_id} does not depend on {dependency_owner}"
                )
                owner_dependency_subjects.extend(
                    (
                        (ResponseIssueSubjectKind.AGENT, dependency_owner),
                        (ResponseIssueSubjectKind.AGENT, task.owner_agent_id),
                        (ResponseIssueSubjectKind.TASK, dependency_id),
                        (ResponseIssueSubjectKind.TASK, task.id),
                    )
                )
    if owner_dependency_paths:
        raise _planning_model_invariant(
            "planning_task_owner_dependency",
            "; ".join(owner_dependency_details)[:1500],
            paths=tuple(dict.fromkeys(owner_dependency_paths)),
            subjects=_planning_subjects(*owner_dependency_subjects),
        )

    if require_canonical_cross_agent_projection:
        projection_paths: list[str] = []
        projection_subjects: list[tuple[ResponseIssueSubjectKind, str]] = []
        for task_index, task in enumerate(tasks):
            same_owner = tuple(
                dependency_id
                for dependency_id in task.dependencies
                if task_owner_by_id[dependency_id] == task.owner_agent_id
            )
            direct_upstream = set(agent_dependencies[task.owner_agent_id])
            derived_cross_agent = tuple(
                candidate.id
                for candidate in tasks
                if task_owner_by_id[candidate.id] in direct_upstream
            )
            canonical = tuple(dict.fromkeys((*same_owner, *derived_cross_agent)))
            if task.dependencies == canonical:
                continue
            projection_paths.append(f"/proposal/tasks/{task_index}/dependencies")
            projection_subjects.extend(
                (
                    (ResponseIssueSubjectKind.AGENT, task.owner_agent_id),
                    (ResponseIssueSubjectKind.TASK, task.id),
                    *(
                        (ResponseIssueSubjectKind.TASK, dependency_id)
                        for dependency_id in canonical
                    ),
                )
            )
        if projection_paths:
            raise _planning_model_invariant(
                "planning_task_dependency_projection",
                "task dependencies must match the Controller projection of the "
                "Agent DAG",
                paths=tuple(projection_paths),
                subjects=_planning_subjects(*projection_subjects),
            )


def validate_task_criterion_references(
    tasks: tuple[ProposedTask, ...],
    known_criterion_ids: Collection[str],
) -> None:
    """Reject task bindings outside a controller-known criterion contract."""

    covered = {criterion for task in tasks for criterion in task.acceptance_criteria}
    unknown = covered - set(known_criterion_ids)
    if unknown:
        message = "tasks reference unknown acceptance criteria: " + ", ".join(
            sorted(unknown)
        )
        raise _planning_model_invariant(
            "planning_task_criterion_reference",
            message,
            paths=tuple(
                f"/proposal/tasks/{index}/acceptance_criteria"
                for index, task in enumerate(tasks)
                if set(task.acceptance_criteria) & unknown
            ),
            subjects=_planning_subjects(
                *((ResponseIssueSubjectKind.CRITERION, item) for item in unknown),
                *(
                    (ResponseIssueSubjectKind.TASK, task.id)
                    for task in tasks
                    if set(task.acceptance_criteria) & unknown
                ),
            ),
        )


def validate_test_suite_writer_ownership(
    criteria: tuple[ProposedCriterion, ...],
    tasks: tuple[ProposedTask, ...],
    agents: tuple[ProposedAgent, ...],
) -> None:
    """Require a writable test author when a proposal promises a pytest suite."""

    agents_by_id = {agent.id: agent for agent in agents}
    for criterion in criteria:
        evidence = f"{criterion.description} {criterion.verification}"
        test_identity = "test" in criterion.id.lower() or any(
            "test" in requirement_id.lower()
            for requirement_id in criterion.requirement_ids
        )
        explicitly_promises_suite = re.search(
            r"\b(?:pytest|test) suite\b", criterion.description, re.IGNORECASE
        )
        if not explicitly_promises_suite and not (
            test_identity and re.search(r"\bpytest\b", evidence, re.IGNORECASE)
        ):
            continue
        candidates = [
            (index, task, agents_by_id[task.owner_agent_id])
            for index, task in enumerate(tasks)
            if criterion.id in task.acceptance_criteria
            and task.owner_agent_id in agents_by_id
            and agents_by_id[task.owner_agent_id].capability
            in {AgentCapability.IMPLEMENTATION, AgentCapability.INTEGRATION}
        ]
        if any(
            owner.workspace_scope in {"repository", "repository/tests"}
            and re.search(
                r"\b(?:write|create|author|add|implement|develop|extend|"
                r"maintain|update|build)\b.{0,250}\b(?:tests|test suite|test cases)\b",
                task.description,
                re.IGNORECASE,
            )
            for _, task, owner in candidates
        ):
            continue
        raise _planning_model_invariant(
            "planning_test_suite_writer_ownership",
            (
                f"criterion {criterion.id} requires a pytest test suite but no "
                "implementation or integration task explicitly authors tests "
                "within repository/tests; assign that work to a writer before "
                "a read-only Testing Agent verifies it"
            ),
            paths=tuple(
                f"/proposal/tasks/{index}/description" for index, _, _ in candidates
            )
            or ("/proposal/tasks",),
            subjects=_planning_subjects(
                (ResponseIssueSubjectKind.CRITERION, criterion.id),
                *(
                    (ResponseIssueSubjectKind.AGENT, owner.id)
                    for _, _, owner in candidates
                ),
            ),
        )


class PlanningProposalBody(BaseModel):
    """Complete semantic proposal returned before user approval."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str = Field(min_length=1, max_length=120)
    product_definition: ProductDefinition | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    requirements: tuple[str, ...] = Field(min_length=1)
    requirement_ids: tuple[str, ...] = Field(
        default=(),
        min_length=1,
        exclude_if=lambda values: not values,
    )
    non_goals: tuple[str, ...] = Field(
        default=(),
        min_length=1,
        exclude_if=lambda values: not values,
    )
    acceptance_criteria: tuple[ProposedCriterion, ...] = Field(
        min_length=1,
    )
    constraints: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    assumption_decision_ids: tuple[str, ...] = Field(
        default=(),
        exclude_if=lambda values: not values,
    )
    decisions: tuple[PlanningDecisionRecord, ...] = Field(
        default=(),
        min_length=1,
        exclude_if=lambda values: not values,
    )
    objective: str = Field(min_length=1, max_length=1000)
    approach: tuple[str, ...] = Field(min_length=1)
    tasks: tuple[ProposedTask, ...] = Field(
        min_length=1,
        description=(
            "Work intent for proposed runtime Agents. Tasks describe approved "
            "focus but do not create Agents or grant permissions."
        ),
    )
    risks: tuple[str, ...] = ()
    agents: tuple[ProposedAgent, ...] = Field(min_length=2)
    iteration_limit: int = Field(ge=1)
    max_concurrency: int = Field(ge=1)
    revision_enabled: bool

    @field_validator("title", "objective")
    @classmethod
    def require_clean_text(cls, value: str) -> str:
        return _clean_text(value, label="proposal text")

    @field_validator(
        "requirements",
        "non_goals",
        "constraints",
        "assumptions",
        "approach",
        "risks",
    )
    @classmethod
    def require_clean_unique_text(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _clean_unique(values, label="proposal")

    @field_validator("requirement_ids")
    @classmethod
    def require_stable_requirement_ids(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        cleaned = _clean_unique(values, label="proposal requirement ID")
        if any(re.fullmatch(r"REQ_[A-Z0-9_]+", value) is None for value in cleaned):
            raise ValueError("proposal requirements must use stable REQ_ IDs")
        return cleaned

    @field_validator("assumption_decision_ids")
    @classmethod
    def require_assumption_decision_ids(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        if any(re.fullmatch(r"DECISION_[A-Z0-9_]+", value) is None for value in values):
            raise ValueError("assumptions must reference stable DECISION_ IDs")
        return values

    @model_validator(mode="after")
    def validate_complete_proposal(self) -> Self:
        criterion_ids = tuple(item.id for item in self.acceptance_criteria)
        if len(criterion_ids) != len(set(criterion_ids)):
            duplicates = tuple(
                sorted(
                    {
                        criterion_id
                        for criterion_id in criterion_ids
                        if criterion_ids.count(criterion_id) > 1
                    }
                )
            )
            raise _planning_model_invariant(
                "planning_criterion_id_unique",
                "proposal acceptance criterion IDs must be unique",
                paths=("/proposal/acceptance_criteria",),
                subjects=_planning_subjects(
                    *((ResponseIssueSubjectKind.CRITERION, item) for item in duplicates)
                ),
            )
        agent_ids = tuple(agent.id for agent in self.agents)
        if len(agent_ids) != len(set(agent_ids)):
            duplicates = tuple(
                sorted(
                    {
                        agent_id
                        for agent_id in agent_ids
                        if agent_ids.count(agent_id) > 1
                    }
                )
            )
            raise _planning_model_invariant(
                "planning_agent_id_unique",
                "proposed Agent IDs must be unique",
                paths=("/proposal/agents",),
                subjects=_planning_subjects(
                    *((ResponseIssueSubjectKind.AGENT, item) for item in duplicates)
                ),
            )
        decision_ids = tuple(item.id for item in self.decisions)
        if len(decision_ids) != len(set(decision_ids)):
            duplicates = tuple(
                sorted(
                    {
                        decision_id
                        for decision_id in decision_ids
                        if decision_ids.count(decision_id) > 1
                    }
                )
            )
            raise _planning_model_invariant(
                "planning_decision_id_unique",
                "proposal decision IDs must be unique",
                paths=("/proposal/decisions",),
                subjects=_planning_subjects(
                    *((ResponseIssueSubjectKind.DECISION, item) for item in duplicates)
                ),
            )
        _validate_dag(
            agent_ids,
            {agent.id: agent.dependencies for agent in self.agents},
            label="Agent",
            path_prefix="/proposal/agents",
            subject_kind=ResponseIssueSubjectKind.AGENT,
        )
        implementation_agents = {
            agent.id
            for agent in self.agents
            if agent.capability
            in {AgentCapability.IMPLEMENTATION, AgentCapability.INTEGRATION}
        }
        if not implementation_agents:
            raise _planning_model_invariant(
                "planning_implementation_agent_required",
                "proposal requires an implementation Agent",
                paths=("/proposal/agents",),
                subjects=_planning_subjects(
                    (ResponseIssueSubjectKind.CAPABILITY, "implementation")
                ),
            )
        quality_agents = {
            agent.id
            for agent in self.agents
            if agent.capability in {AgentCapability.TESTING, AgentCapability.REVIEW}
        }
        if not quality_agents:
            raise _planning_model_invariant(
                "planning_quality_agent_required",
                "proposal requires an independent quality Agent",
                paths=("/proposal/agents",),
                subjects=_planning_subjects(
                    (ResponseIssueSubjectKind.CAPABILITY, "read_only_quality")
                ),
            )

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

        quality_dependency_paths: list[str] = []
        quality_dependency_subjects: list[tuple[ResponseIssueSubjectKind, str]] = []
        for quality_agent in sorted(quality_agents):
            missing_dependencies = tuple(
                sorted(
                    implementation_agent
                    for implementation_agent in implementation_agents
                    if not transitively_depends(quality_agent, implementation_agent)
                )
            )
            if missing_dependencies:
                quality_dependency_paths.append(
                    f"/proposal/agents/{agent_ids.index(quality_agent)}/dependencies",
                )
                quality_dependency_subjects.extend(
                    (
                        (ResponseIssueSubjectKind.AGENT, quality_agent),
                        *(
                            (ResponseIssueSubjectKind.AGENT, item)
                            for item in missing_dependencies
                        ),
                    )
                )
        if quality_dependency_paths:
            raise _planning_model_invariant(
                "planning_quality_dependency_coverage",
                "every quality Agent must depend on every implementation path",
                paths=tuple(quality_dependency_paths),
                subjects=_planning_subjects(*quality_dependency_subjects),
            )

        validate_task_agent_bindings(
            self.tasks,
            dependencies,
            implementation_agents,
        )
        implementation_by_id = {
            agent.id: agent
            for agent in self.agents
            if agent.id in implementation_agents
        }
        unordered_writer_pairs: list[tuple[str, str]] = []
        ordered_writer_ids = tuple(
            agent.id for agent in self.agents if agent.id in implementation_agents
        )
        for index, agent_id in enumerate(ordered_writer_ids):
            agent = implementation_by_id[agent_id]
            for other_id in ordered_writer_ids[index + 1 :]:
                other = implementation_by_id[other_id]
                if not workspace_scopes_overlap(
                    agent.workspace_scope,
                    other.workspace_scope,
                ):
                    continue
                if not (
                    transitively_depends(agent_id, other_id)
                    or transitively_depends(other_id, agent_id)
                ):
                    unordered_writer_pairs.append((agent_id, other_id))
        if unordered_writer_pairs:
            involved_writer_ids = tuple(
                sorted(
                    {agent_id for pair in unordered_writer_pairs for agent_id in pair}
                )
            )
            conflicts = ", ".join(
                f"{first} <> {second}" for first, second in unordered_writer_pairs
            )
            raise _planning_model_invariant(
                "planning_writer_workspace_dependency_order",
                (
                    "overlapping writer workspace scopes must be dependency "
                    f"ordered: {conflicts}"
                ),
                paths=tuple(
                    f"/proposal/agents/{agent_ids.index(agent_id)}/dependencies"
                    for agent_id in involved_writer_ids
                ),
                subjects=_planning_subjects(
                    *(
                        (ResponseIssueSubjectKind.AGENT, agent_id)
                        for agent_id in involved_writer_ids
                    )
                ),
            )
        covered = {
            criterion
            for task in self.tasks
            if task.owner_agent_id in implementation_agents
            for criterion in task.acceptance_criteria
        }
        expected = set(criterion_ids)
        missing = expected - covered
        if missing:
            writer_task_indexes = tuple(
                index
                for index, task in enumerate(self.tasks)
                if task.owner_agent_id in implementation_agents
            )
            message = (
                "writer tasks do not cover proposal acceptance criteria: "
                + ", ".join(sorted(missing))
            )
            raise _planning_model_invariant(
                "planning_writer_criterion_coverage",
                message,
                paths=(
                    (f"/proposal/tasks/{writer_task_indexes[0]}/acceptance_criteria")
                    if len(writer_task_indexes) == 1
                    else "/proposal/tasks",
                ),
                subjects=_planning_subjects(
                    *((ResponseIssueSubjectKind.CRITERION, item) for item in missing)
                ),
            )
        if self.max_concurrency > len(self.agents):
            raise _planning_model_invariant(
                "planning_concurrency_agent_bound",
                "proposal concurrency cannot exceed its Agent count",
                paths=("/proposal/max_concurrency",),
            )
        if self.revision_enabled != (self.iteration_limit > 1):
            raise _planning_model_invariant(
                "planning_revision_iteration_consistency",
                "revision_enabled must equal whether iteration_limit exceeds one",
                paths=(
                    "/proposal/iteration_limit",
                    "/proposal/revision_enabled",
                ),
            )
        return self


def validate_question_admission(
    question: PlanningQuestion,
    *,
    previous_question_ids: Collection[str] = (),
) -> None:
    """Reject questions outside the deterministic responsibility matrix."""

    if question.id in set(previous_question_ids):
        raise _planning_context_invariant(
            "planning_question_id_reused",
            f"Planning question ID was already used: {question.id}",
            paths=("/question",),
            subjects=_planning_subjects(
                (ResponseIssueSubjectKind.QUESTION, question.id)
            ),
        )
    if question.decision_category is None or question.decision_owner is None:
        raise _planning_context_invariant(
            "planning_question_contract_required",
            "Planning question is missing decision category or owner",
            paths=("/question",),
            subjects=_planning_subjects(
                (ResponseIssueSubjectKind.QUESTION, question.id)
            ),
        )
    expected = _DECISION_AUTHORITY[question.decision_category]
    if question.decision_owner is not expected:
        message = (
            f"Planning question category {question.decision_category.value} belongs "
            f"to {expected.value}, not {question.decision_owner.value}"
        )
        raise _planning_context_invariant(
            "planning_question_authority",
            message,
            paths=("/question",),
            subjects=_planning_subjects(
                (ResponseIssueSubjectKind.QUESTION, question.id)
            ),
        )
    if expected in {
        PlanningDecisionAuthority.AGENT_AUTONOMY,
        PlanningDecisionAuthority.CONTROLLER_POLICY,
    }:
        raise _planning_context_invariant(
            "planning_question_user_authority",
            (
                "Planning cannot ask the user to decide "
                f"{question.decision_category.value}"
            ),
            paths=("/question",),
            subjects=_planning_subjects(
                (ResponseIssueSubjectKind.QUESTION, question.id)
            ),
        )
    if (
        question.product_definition_dimensions
        and question.decision_category
        is not PlanningDecisionCategory.PRODUCT_REQUIREMENT
    ):
        raise _planning_context_invariant(
            "planning_product_question_authority",
            "product-definition clarification belongs to user product requirements",
            paths=("/question",),
            subjects=_planning_subjects(
                (ResponseIssueSubjectKind.QUESTION, question.id)
            ),
        )
    if not question.missing_evidence:
        raise _planning_context_invariant(
            "planning_question_missing_evidence",
            "Planning question must name the missing evidence",
            paths=("/question",),
            subjects=_planning_subjects(
                (ResponseIssueSubjectKind.QUESTION, question.id)
            ),
        )
    if not question.material_consequences:
        raise _planning_context_invariant(
            "planning_question_material_consequence",
            "Planning question must name a material consequence",
            paths=("/question",),
            subjects=_planning_subjects(
                (ResponseIssueSubjectKind.QUESTION, question.id)
            ),
        )
    expected_dimensions = question.product_definition_dimensions
    invalid_options = tuple(
        option.id
        for option in question.options
        if tuple(value.dimension for value in option.product_definition_values)
        != expected_dimensions
    )
    if invalid_options:
        raise _planning_context_invariant(
            "planning_question_option_scope",
            (
                "Planning question option values must exactly match the declared "
                "product-definition dimension order; invalid options: "
                + ", ".join(invalid_options)
            ),
            paths=("/question",),
            subjects=_planning_subjects(
                (ResponseIssueSubjectKind.QUESTION, question.id)
            ),
        )
    maturity_values = tuple(
        value.value
        for option in question.options
        for value in option.product_definition_values
        if value.dimension is ProductDefinitionDimension.DELIVERY_MATURITY
    )
    allowed_maturity = {item.value for item in DeliveryMaturity}
    if any(value not in allowed_maturity for value in maturity_values):
        raise _planning_context_invariant(
            "planning_question_maturity_option",
            "Planning maturity options must use canonical delivery-maturity values",
            paths=("/question",),
            subjects=_planning_subjects(
                (ResponseIssueSubjectKind.QUESTION, question.id)
            ),
        )


# (_normalized_evidence_text imported from .common)


_MATURITY_EVIDENCE_ALIASES = {
    DeliveryMaturity.THROWAWAY_PROTOTYPE: (
        "throwaway prototype",
        "personal local one time helper",
    ),
    DeliveryMaturity.USABLE_LOCAL_PRODUCT: (
        "usable local product",
        "personal reusable local tool",
    ),
    DeliveryMaturity.RELEASABLE_SMALL_PRODUCT: (
        "releasable small product",
        "reusable for others",
    ),
}


def _maturity_levels_in_answer(value: str) -> frozenset[DeliveryMaturity]:
    """Recognize only the canonical levels and labels exposed by Planning."""

    normalized = re.sub(r"[_-]+", " ", _normalized_evidence_text(value))
    normalized = " ".join(re.findall(r"[\w]+", normalized))
    return frozenset(
        level
        for level, aliases in _MATURITY_EVIDENCE_ALIASES.items()
        if any(alias in normalized for alias in aliases)
    )


def _validate_decision_provenance(
    body: PlanningProposalBody,
    *,
    normalized_inputs: tuple[str, ...],
    require_current_provenance: bool,
) -> None:
    """Separate model-authored quote defects from genuinely missing user input."""

    quote_invariants: list[_PlanningInvariant] = []
    for decision_index, decision in enumerate(body.decisions):
        provenance = decision.provenance
        if provenance is None:
            if not require_current_provenance:
                if (
                    decision.authority is PlanningDecisionAuthority.USER
                    and decision.question_id is None
                ):
                    raise _planning_context_invariant(
                        "planning_legacy_user_decision_source",
                        (
                            f"decision {decision.id} has neither direct-input nor "
                            "question provenance"
                        ),
                        paths=(f"/proposal/decisions/{decision_index}",),
                        subjects=_planning_subjects(
                            (ResponseIssueSubjectKind.DECISION, decision.id)
                        ),
                        failure_class=ResponseFailureClass.MISSING_USER_DECISION,
                        authority=ResponseIssueAuthority.USER,
                    )
                continue
            authority = (
                ResponseIssueAuthority.USER
                if decision.authority is PlanningDecisionAuthority.USER
                else ResponseIssueAuthority.MODEL
            )
            failure_class = (
                ResponseFailureClass.MISSING_USER_DECISION
                if authority is ResponseIssueAuthority.USER
                else ResponseFailureClass.SEMANTIC_CONTEXT
            )
            raise _planning_context_invariant(
                "planning_decision_provenance_required",
                f"current decision {decision.id} requires typed provenance",
                paths=(f"/proposal/decisions/{decision_index}/provenance",),
                subjects=_planning_subjects(
                    (ResponseIssueSubjectKind.DECISION, decision.id)
                ),
                failure_class=failure_class,
                authority=authority,
            )
        if provenance.kind is PlanningDecisionProvenanceKind.EXPLICIT_INPUT and not any(
            _normalized_evidence_text(provenance.source) in value
            for value in normalized_inputs
        ):
            invariant = _PlanningInvariant(
                invariant_id="planning_decision_explicit_source",
                message=(
                    f"decision {decision.id} claims explicit user input that is "
                    "not present in the Planning request or a user revision; "
                    "source must quote one contiguous verbatim substring of "
                    "the existing user input, not a label or invented permission"
                ),
                paths=(f"/proposal/decisions/{decision_index}/provenance/source",),
                subjects=_planning_subjects(
                    (ResponseIssueSubjectKind.DECISION, decision.id)
                ),
            )
            if not normalized_inputs:
                raise _PlanningContextInvariantError(
                    invariant,
                    failure_class=ResponseFailureClass.MISSING_USER_DECISION,
                    authority=ResponseIssueAuthority.USER,
                )
            quote_invariants.append(invariant)
            continue
        if (
            provenance.kind is PlanningDecisionProvenanceKind.EXPLICIT_INPUT
            and _normalized_evidence_text(decision.summary)
            not in _normalized_evidence_text(provenance.source)
        ):
            raise _planning_context_invariant(
                "planning_decision_explicit_summary",
                (
                    f"decision {decision.id} summary must be one contiguous "
                    "direct-input excerpt of at most "
                    f"{MAX_PLANNING_DECISION_SUMMARY_CHARACTERS} characters"
                ),
                paths=(f"/proposal/decisions/{decision_index}/summary",),
                subjects=_planning_subjects(
                    (ResponseIssueSubjectKind.DECISION, decision.id)
                ),
            )

    if quote_invariants:
        raise _PlanningContextInvariantsError(tuple(quote_invariants))


def _product_definition_dimension_invariant(
    *,
    dimension: ProductDefinitionDimension,
    item: ProductDefinitionBasis,
    definition: ProductDefinition,
    requirement_ids: set[str],
    criterion_ids: set[str],
    decision_ids: set[str],
    decisions: Mapping[str, PlanningDecisionRecord],
    normalized_inputs: tuple[str, ...],
    question_contracts: Mapping[str, _PlanningQuestionContract] | None,
    allow_legacy_decision_links: bool,
    allow_legacy_workflow_exemption: bool,
) -> _PlanningInvariant | None:
    """Return one atomic, coherently repairable issue for a product dimension."""

    path = f"/proposal/product_definition/{dimension.value}"

    def issue(
        invariant_id: str,
        message: str,
        *,
        subjects: tuple[ResponseIssueSubject, ...] = (),
        failure_class: ResponseFailureClass = ResponseFailureClass.SEMANTIC_CONTEXT,
        authority: ResponseIssueAuthority = ResponseIssueAuthority.MODEL,
        related_paths: tuple[str, ...] = (),
    ) -> _PlanningInvariant:
        return _PlanningInvariant(
            invariant_id=invariant_id,
            message=message,
            paths=tuple(sorted({path, *related_paths})),
            subjects=subjects,
            failure_class=failure_class,
            authority=authority,
        )

    if (
        dimension is ProductDefinitionDimension.PRIMARY_WORKFLOW
        and item.disposition is ProductDefinitionDisposition.NOT_MATERIAL
        and not allow_legacy_workflow_exemption
    ):
        return issue(
            "planning_product_workflow_required",
            "primary workflow is always material, including a throwaway prototype; "
            "preserve its explicit user input or ask a focused question, and link "
            "the workflow to the requirements it drives",
        )

    if (
        item.disposition is ProductDefinitionDisposition.NOT_MATERIAL
        and not allow_legacy_decision_links
        and (item.requirement_ids or item.criterion_ids or item.decision_ids)
    ):
        return issue(
            "planning_product_not_material_trace",
            (
                f"{dimension.value} is not material and cannot claim a "
                "downstream requirement, criterion, or decision effect"
            ),
        )

    unknown_requirements = set(item.requirement_ids) - requirement_ids
    unknown_criteria = set(item.criterion_ids) - criterion_ids
    unknown_decisions = set(item.decision_ids) - decision_ids
    if unknown_requirements or unknown_criteria or unknown_decisions:
        details = []
        reference_paths = []
        if unknown_requirements:
            details.append("requirements " + ", ".join(sorted(unknown_requirements)))
            reference_paths.append(f"{path}/requirement_ids")
        if unknown_criteria:
            details.append("criteria " + ", ".join(sorted(unknown_criteria)))
            reference_paths.append(f"{path}/criterion_ids")
        if unknown_decisions:
            details.append("decisions " + ", ".join(sorted(unknown_decisions)))
            reference_paths.append(f"{path}/decision_ids")
        return _PlanningInvariant(
            invariant_id="planning_product_definition_reference",
            message=(
                f"{dimension.value} references unknown downstream " + "; ".join(details)
            ),
            paths=tuple(reference_paths),
        )

    if item.disposition is ProductDefinitionDisposition.EXPLICIT_INPUT:
        source_is_exact = bool(normalized_inputs) and any(
            _normalized_evidence_text(item.source) in value
            for value in normalized_inputs
        )
        statement = getattr(item, "statement", None)
        source_text = _normalized_evidence_text(item.source)
        statement_text = (
            None if statement is None else _normalized_evidence_text(statement)
        )
        statement_preserves_source = (
            statement_text is None
            or statement_text == source_text
            or (not source_text.endswith(".") and statement_text == f"{source_text}.")
        )
        if not source_is_exact:
            return issue(
                "planning_product_explicit_provenance",
                (
                    f"{dimension.value} explicit_input requires source to be one "
                    "contiguous verbatim substring of a user input, with no label, "
                    "quote delimiters, stitched excerpts, ellipsis, or commentary. "
                    "If the dimension is not explicit, choose the truthful "
                    "disposition and its source."
                ),
            )
        if not statement_preserves_source:
            return issue(
                "planning_product_explicit_statement",
                (
                    f"{dimension.value} source already quotes user input correctly; "
                    "keep that source and set statement to the same wording. "
                    "Only case, whitespace, and one final sentence period may "
                    "differ: do not omit leading words or paraphrase."
                ),
            )
        if item.decision_ids and not allow_legacy_decision_links:
            return issue(
                "planning_product_explicit_decision_trace",
                (
                    f"{dimension.value} explicit_input is already attributable to "
                    "its exact user source and cannot cite a separate decision record"
                ),
            )
        return None

    if item.disposition is ProductDefinitionDisposition.RESOLVED_QUESTION:
        linked = tuple(
            decision
            for decision in decisions.values()
            if decision.resolved_question_id == item.source
        )
        if (
            len(linked) != 1
            or linked[0].authority is not PlanningDecisionAuthority.USER
            or linked[0].category is not PlanningDecisionCategory.PRODUCT_REQUIREMENT
        ):
            return issue(
                "planning_product_question_source",
                (
                    f"{dimension.value} must reference one user-owned product "
                    "question decision and cite its ID. If no admitted user "
                    "question exists, do not invent one: change the dimension to "
                    "explicit_input with identical verbatim source and statement "
                    "when the user supplied it, or to planner_recommendation with "
                    "source 'planner' and a corresponding Planner decision."
                ),
                related_paths=("/proposal/decisions",),
            )
        if (
            linked[0].id not in item.decision_ids
            if allow_legacy_decision_links
            else set(item.decision_ids) != {linked[0].id}
        ):
            return issue(
                "planning_product_question_decision_trace",
                (f"{dimension.value} must cite only its resolved question decision"),
            )
        if question_contracts is None:
            return None
        contract = question_contracts.get(item.source)
        subjects = _planning_subjects((ResponseIssueSubjectKind.QUESTION, item.source))
        if (
            contract is None
            or contract.category is not PlanningDecisionCategory.PRODUCT_REQUIREMENT
            or contract.owner is not PlanningDecisionAuthority.USER
        ):
            return issue(
                "planning_product_question_dimension",
                f"{dimension.value} was not resolved by its declared question",
                subjects=subjects,
            )
        if dimension not in contract.product_definition_dimensions:
            if dimension not in _USER_DECISION_PRODUCT_DIMENSIONS:
                return issue(
                    "planning_product_question_dimension",
                    (
                        f"{dimension.value} was not resolved by its declared "
                        "question; use an attributable Planner recommendation "
                        "or an exact explicit user input instead"
                    ),
                    subjects=subjects,
                )
            return issue(
                "planning_product_question_dimension",
                f"{dimension.value} was not resolved by its declared question",
                subjects=subjects,
                failure_class=ResponseFailureClass.MISSING_USER_DECISION,
                authority=ResponseIssueAuthority.USER,
            )
        resolved_value = (
            definition.delivery_maturity.level.value.replace("_", " ")
            if dimension is ProductDefinitionDimension.DELIVERY_MATURITY
            else getattr(item, "statement", "")
        )
        if contract.answer is not None:
            exact_dimension_value = dict(contract.answer_dimension_values).get(
                dimension
            )
            maturity_levels = (
                _maturity_levels_in_answer(contract.answer)
                if dimension is ProductDefinitionDimension.DELIVERY_MATURITY
                and exact_dimension_value is None
                else frozenset()
            )
            answer_preserves_value = (
                _normalized_evidence_text(resolved_value)
                == _normalized_evidence_text(exact_dimension_value.replace("_", " "))
                if exact_dimension_value is not None
                else maturity_levels == {definition.delivery_maturity.level}
                if dimension is ProductDefinitionDimension.DELIVERY_MATURITY
                else _normalized_evidence_text(resolved_value)
                in _normalized_evidence_text(contract.answer)
            )
            if not answer_preserves_value:
                missing_maturity_answer = (
                    dimension is ProductDefinitionDimension.DELIVERY_MATURITY
                    and exact_dimension_value is None
                    and not maturity_levels
                )
                return issue(
                    "planning_product_question_answer",
                    f"{dimension.value} is not preserved in its user answer",
                    subjects=subjects,
                    failure_class=(
                        ResponseFailureClass.MISSING_USER_DECISION
                        if missing_maturity_answer
                        else ResponseFailureClass.SEMANTIC_CONTEXT
                    ),
                    authority=(
                        ResponseIssueAuthority.USER
                        if missing_maturity_answer
                        else ResponseIssueAuthority.MODEL
                    ),
                )
        else:
            approved_values = dict(contract.approved_dimension_values)
            previous_value = approved_values.get(dimension)
            if previous_value is None or _normalized_evidence_text(
                resolved_value
            ) != _normalized_evidence_text(previous_value):
                return issue(
                    "planning_product_question_revision",
                    f"{dimension.value} changed without a new user answer",
                    subjects=subjects,
                )
        return None

    if item.disposition is ProductDefinitionDisposition.PLANNER_RECOMMENDATION:
        if dimension in _USER_DECISION_PRODUCT_DIMENSIONS:
            return _planning_user_decision_invariant(dimension)
        expected_category = {
            ProductDefinitionDimension.USABILITY_EXPECTATIONS: (
                PlanningDecisionCategory.ACCEPTANCE_SCOPE
            ),
            ProductDefinitionDimension.OPERATIONAL_EXPECTATIONS: (
                PlanningDecisionCategory.ACCEPTANCE_SCOPE
            ),
            ProductDefinitionDimension.DELIVERY_EXPECTATIONS: (
                PlanningDecisionCategory.DELIVERY
            ),
        }[dimension]
        decision_related_paths = (
            ()
            if any(
                decision.authority is PlanningDecisionAuthority.PLANNER_PROPOSAL
                and decision.category is expected_category
                for decision in decisions.values()
            )
            else ("/proposal/decisions",)
        )
        if item.source != "planner" or not item.decision_ids:
            return issue(
                "planning_product_recommendation_source",
                f"{dimension.value} Planner recommendation needs decision provenance",
                related_paths=decision_related_paths,
            )
        if any(
            decisions[decision_id].authority
            is not PlanningDecisionAuthority.PLANNER_PROPOSAL
            for decision_id in item.decision_ids
        ):
            return issue(
                "planning_product_recommendation_authority",
                f"{dimension.value} references a non-Planner decision",
                related_paths=decision_related_paths,
            )
        category_matches = tuple(
            decisions[decision_id].category is expected_category
            for decision_id in item.decision_ids
        )
        if not (
            any(category_matches)
            if allow_legacy_decision_links
            else all(category_matches)
        ):
            return issue(
                "planning_product_recommendation_category",
                (
                    f"{dimension.value} may cite only its corresponding "
                    "Planner decision category"
                ),
                related_paths=decision_related_paths,
            )
        return None

    if dimension is ProductDefinitionDimension.DELIVERY_MATURITY:
        return issue(
            "planning_product_maturity_required",
            "delivery maturity is always material to the approved product",
            failure_class=ResponseFailureClass.MISSING_USER_DECISION,
            authority=ResponseIssueAuthority.USER,
        )
    if (
        dimension
        in {
            ProductDefinitionDimension.TARGET_USERS,
            ProductDefinitionDimension.PRIMARY_WORKFLOW,
        }
        and definition.delivery_maturity.level
        is not DeliveryMaturity.THROWAWAY_PROTOTYPE
    ):
        return issue(
            "planning_product_user_decision_required",
            f"{dimension.value} may be not_material only for a throwaway prototype",
            failure_class=ResponseFailureClass.MISSING_USER_DECISION,
            authority=ResponseIssueAuthority.USER,
        )
    if item.source != "planner":
        return issue(
            "planning_product_not_material_source",
            (
                f"{dimension.value} not_material disposition must be "
                "attributable to Planning"
            ),
        )
    return None


def _validate_product_definition(
    body: PlanningProposalBody,
    *,
    decisions: Mapping[str, PlanningDecisionRecord],
    user_inputs: Collection[str],
    question_contracts: Mapping[str, _PlanningQuestionContract] | None,
    allowed_criterion_ids: Collection[str] = (),
    allow_legacy_decision_links: bool = False,
    allow_legacy_workflow_exemption: bool = False,
) -> None:
    """Require attributable product depth with real downstream plan effects."""

    definition = body.product_definition
    if definition is None:
        raise _planning_context_invariant(
            "planning_product_definition_required",
            "current proposals require an approved product definition",
            paths=("/proposal/product_definition",),
        )

    requirement_ids = set(body.requirement_ids)
    criterion_ids = {
        *(criterion.id for criterion in body.acceptance_criteria),
        *allowed_criterion_ids,
    }
    decision_ids = set(decisions)
    normalized_inputs = tuple(_normalized_evidence_text(value) for value in user_inputs)
    dimension_invariants = tuple(
        invariant
        for dimension, item in definition.dimensions()
        if (
            invariant := _product_definition_dimension_invariant(
                dimension=dimension,
                item=item,
                definition=definition,
                requirement_ids=requirement_ids,
                criterion_ids=criterion_ids,
                decision_ids=decision_ids,
                decisions=decisions,
                normalized_inputs=normalized_inputs,
                question_contracts=question_contracts,
                allow_legacy_decision_links=allow_legacy_decision_links,
                allow_legacy_workflow_exemption=allow_legacy_workflow_exemption,
            )
        )
        is not None
    )
    if dimension_invariants:
        raise _PlanningContextInvariantsError(dimension_invariants)

    if (
        definition.target_users.disposition
        is not ProductDefinitionDisposition.NOT_MATERIAL
        and not definition.target_users.requirement_ids
    ):
        raise _planning_context_invariant(
            "planning_product_audience_effect",
            "target users must affect at least one requirement",
            paths=("/proposal/product_definition/target_users/requirement_ids",),
        )
    if (
        definition.primary_workflow.disposition
        is not ProductDefinitionDisposition.NOT_MATERIAL
        and not definition.primary_workflow.requirement_ids
    ):
        raise _planning_context_invariant(
            "planning_product_workflow_effect",
            "primary workflow must affect at least one requirement",
            paths=("/proposal/product_definition/primary_workflow/requirement_ids",),
        )
    for dimension, item in (
        (
            ProductDefinitionDimension.USABILITY_EXPECTATIONS,
            definition.usability_expectations,
        ),
        (
            ProductDefinitionDimension.OPERATIONAL_EXPECTATIONS,
            definition.operational_expectations,
        ),
    ):
        if (
            item.disposition is not ProductDefinitionDisposition.NOT_MATERIAL
            and not item.criterion_ids
        ):
            raise _planning_context_invariant(
                "planning_product_quality_effect",
                f"{dimension.value} must affect at least one acceptance criterion",
                paths=(
                    f"/proposal/product_definition/{dimension.value}/criterion_ids",
                ),
            )


def validate_planning_clarity(
    body: PlanningProposalBody,
    *,
    source_request: str | None = None,
    additional_user_inputs: Collection[str] = (),
    question_contracts: Mapping[str, _PlanningQuestionContract] | None = None,
    allowed_criterion_ids: Collection[str] = (),
    require_current_decision_provenance: bool = True,
    allow_legacy_product_decision_links: bool = False,
    allow_legacy_workflow_exemption: bool = False,
    enforce_specialized_review_authority: bool = False,
    enforce_test_suite_writer_ownership: bool = False,
) -> None:
    """Enforce the current decision and requirement-to-evidence contract."""

    allowed_criterion_ids = tuple(allowed_criterion_ids)
    if enforce_test_suite_writer_ownership:
        validate_test_suite_writer_ownership(
            body.acceptance_criteria, body.tasks, body.agents
        )

    if len(body.requirement_ids) != len(body.requirements):
        raise _planning_context_invariant(
            "planning_requirement_id_cardinality",
            (
                "legacy proposal requirement fields do not contain one stable ID "
                "for every description; replace the complete requirements relation "
                "with atomic id/description objects"
            ),
            paths=("/proposal/requirements",),
        )
    if not body.non_goals:
        raise _planning_context_invariant(
            "planning_non_goal_required",
            "current proposals must state at least one non-goal",
            paths=("/proposal/non_goals",),
        )
    if not body.decisions:
        raise _planning_context_invariant(
            "planning_decision_provenance_required",
            "current proposals must record decision provenance",
            paths=("/proposal/decisions",),
        )

    decisions = {decision.id: decision for decision in body.decisions}
    if len(decisions) != len(body.decisions):
        decision_ids = tuple(decision.id for decision in body.decisions)
        duplicates = tuple(
            sorted(
                {
                    decision_id
                    for decision_id in decision_ids
                    if decision_ids.count(decision_id) > 1
                }
            )
        )
        raise _planning_context_invariant(
            "planning_decision_id_unique",
            "proposal decision IDs must be unique",
            paths=("/proposal/decisions",),
            subjects=_planning_subjects(
                *((ResponseIssueSubjectKind.DECISION, item) for item in duplicates)
            ),
        )
    if any(
        decision.authority is PlanningDecisionAuthority.CONTROLLER_POLICY
        for decision in body.decisions
    ):
        invalid_decisions = tuple(
            (index, decision)
            for index, decision in enumerate(body.decisions)
            if decision.authority is PlanningDecisionAuthority.CONTROLLER_POLICY
        )
        raise _planning_context_invariant(
            "planning_controller_authority_claim",
            "Planner output cannot claim controller-policy decision authority",
            paths=tuple(
                f"/proposal/decisions/{index}/category"
                for index, _decision in invalid_decisions
            ),
            subjects=_planning_subjects(
                *(
                    (ResponseIssueSubjectKind.DECISION, decision.id)
                    for _index, decision in invalid_decisions
                )
            ),
            authority=ResponseIssueAuthority.CONTROLLER,
        )

    normalized_inputs = tuple(
        _normalized_evidence_text(value)
        for value in (source_request, *additional_user_inputs)
        if value is not None
    )
    _validate_decision_provenance(
        body,
        normalized_inputs=normalized_inputs,
        require_current_provenance=require_current_decision_provenance,
    )

    independent_invariants: list[_PlanningInvariant] = []
    try:
        _validate_product_definition(
            body,
            decisions=decisions,
            user_inputs=tuple(
                value
                for value in (source_request, *additional_user_inputs)
                if value is not None
            ),
            question_contracts=question_contracts,
            allowed_criterion_ids=allowed_criterion_ids,
            allow_legacy_decision_links=allow_legacy_product_decision_links,
            allow_legacy_workflow_exemption=allow_legacy_workflow_exemption,
        )
    except _PlanningContextInvariantError as error:
        if (
            error.authority is not ResponseIssueAuthority.MODEL
            or error.failure_class is not ResponseFailureClass.SEMANTIC_CONTEXT
        ):
            raise
        independent_invariants.append(error.invariant)
    except _PlanningContextInvariantsError as error:
        if any(
            invariant.authority is not ResponseIssueAuthority.MODEL
            or invariant.failure_class is not ResponseFailureClass.SEMANTIC_CONTEXT
            for invariant in error.invariants
        ):
            raise
        independent_invariants.extend(error.invariants)

    required_recommendations = {
        PlanningDecisionCategory.ACCEPTANCE_SCOPE,
        PlanningDecisionCategory.DELIVERY,
        PlanningDecisionCategory.TEAM,
        PlanningDecisionCategory.MODEL_ROUTE,
    }
    recorded_recommendations = {
        decision.category
        for decision in body.decisions
        if decision.authority is PlanningDecisionAuthority.PLANNER_PROPOSAL
    }
    missing_recommendations = required_recommendations - recorded_recommendations
    if missing_recommendations:
        message = "proposal omits Planner recommendation provenance for: " + ", ".join(
            sorted(item.value for item in missing_recommendations)
        )
        independent_invariants.append(
            _PlanningInvariant(
                invariant_id="planning_recommendation_provenance",
                message=message,
                paths=("/proposal/decisions",),
                subjects=_planning_subjects(
                    *(
                        (ResponseIssueSubjectKind.DECISION, item.value)
                        for item in missing_recommendations
                    )
                ),
            )
        )

    if len(body.assumption_decision_ids) != len(body.assumptions):
        raise _planning_context_invariant(
            "planning_assumption_decision_cardinality",
            "every assumption must identify its autonomous decision record",
            paths=("/proposal/assumptions",),
        )
    for decision_id in body.assumption_decision_ids:
        decision = decisions.get(decision_id)
        if decision is None:
            raise _planning_context_invariant(
                "planning_assumption_decision_reference",
                f"assumption references an unknown decision: {decision_id}",
                paths=("/proposal/assumptions",),
                subjects=_planning_subjects(
                    (ResponseIssueSubjectKind.DECISION, decision_id)
                ),
            )
        if decision.authority is not PlanningDecisionAuthority.AGENT_AUTONOMY:
            raise _planning_context_invariant(
                "planning_assumption_decision_authority",
                f"assumption {decision_id} is not an autonomous implementation choice",
                paths=("/proposal/assumptions",),
                subjects=_planning_subjects(
                    (ResponseIssueSubjectKind.DECISION, decision_id)
                ),
            )

    requirement_ids = set(body.requirement_ids)
    covered_requirements: set[str] = set()
    agents = {agent.id: agent for agent in body.agents}
    tasks_by_criterion: dict[str, list[ProposedTask]] = {
        criterion.id: [] for criterion in body.acceptance_criteria
    }
    for task in body.tasks:
        for criterion_id in task.acceptance_criteria:
            if criterion_id in tasks_by_criterion:
                tasks_by_criterion[criterion_id].append(task)

    dependencies = {agent.id: agent.dependencies for agent in body.agents}

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

    agent_indexes = {agent.id: index for index, agent in enumerate(body.agents)}

    def validate_criterion(criterion_index: int, criterion: ProposedCriterion) -> None:
        # Keep prerequisite order within a criterion, but do not hide errors in
        # independent siblings behind another model round trip.
        unknown_requirements = set(criterion.requirement_ids) - requirement_ids
        if unknown_requirements:
            message = (
                f"criterion {criterion.id} references unknown requirements: "
                + ", ".join(sorted(unknown_requirements))
            )
            raise _planning_context_invariant(
                "planning_criterion_requirement_reference",
                message,
                paths=(
                    f"/proposal/acceptance_criteria/{criterion_index}/requirement_ids",
                ),
                subjects=_planning_subjects(
                    (ResponseIssueSubjectKind.CRITERION, criterion.id),
                    *(
                        (ResponseIssueSubjectKind.REQUIREMENT, item)
                        for item in unknown_requirements
                    ),
                ),
            )
        if not criterion.requirement_ids:
            raise _planning_context_invariant(
                "planning_criterion_requirement_required",
                f"criterion {criterion.id} must reference at least one requirement",
                paths=(
                    f"/proposal/acceptance_criteria/{criterion_index}/requirement_ids",
                ),
                subjects=_planning_subjects(
                    (ResponseIssueSubjectKind.CRITERION, criterion.id)
                ),
            )
        covered_requirements.update(criterion.requirement_ids)
        writers = {
            task.owner_agent_id
            for task in tasks_by_criterion[criterion.id]
            if agents[task.owner_agent_id].capability
            in {AgentCapability.IMPLEMENTATION, AgentCapability.INTEGRATION}
        }
        if not writers:
            raise _planning_context_invariant(
                "planning_criterion_writer_required",
                f"criterion {criterion.id} has no responsible writer task",
                paths=("/proposal/tasks",),
                subjects=_planning_subjects(
                    (ResponseIssueSubjectKind.CRITERION, criterion.id)
                ),
            )
        if not criterion.verification_agent_ids:
            raise _planning_context_invariant(
                "planning_criterion_verifier_required",
                f"criterion {criterion.id} must name an independent verifier",
                paths=(
                    f"/proposal/acceptance_criteria/{criterion_index}/verification_agent_ids",
                ),
                subjects=_planning_subjects(
                    (ResponseIssueSubjectKind.CRITERION, criterion.id)
                ),
            )
        for verifier_id in criterion.verification_agent_ids:
            verifier = agents.get(verifier_id)
            if verifier is None:
                message = (
                    f"criterion {criterion.id} references unknown verifier "
                    f"{verifier_id}"
                )
                raise _planning_context_invariant(
                    "planning_criterion_verifier_reference",
                    message,
                    paths=(
                        f"/proposal/acceptance_criteria/{criterion_index}/verification_agent_ids",
                    ),
                    subjects=_planning_subjects(
                        (ResponseIssueSubjectKind.AGENT, verifier_id),
                        (ResponseIssueSubjectKind.CRITERION, criterion.id),
                    ),
                )
            if verifier.capability not in {
                AgentCapability.TESTING,
                AgentCapability.REVIEW,
            }:
                message = (
                    f"criterion {criterion.id} verifier {verifier_id} is not "
                    "read-only quality"
                )
                raise _planning_context_invariant(
                    "planning_criterion_verifier_capability",
                    message,
                    paths=(
                        f"/proposal/acceptance_criteria/{criterion_index}/verification_agent_ids",
                    ),
                    subjects=_planning_subjects(
                        (ResponseIssueSubjectKind.AGENT, verifier_id),
                        (ResponseIssueSubjectKind.CRITERION, criterion.id),
                    ),
                )
            missing_writers = tuple(
                sorted(
                    writer
                    for writer in writers
                    if not transitively_depends(verifier_id, writer)
                )
            )
            if missing_writers:
                message = (
                    f"criterion {criterion.id} verifier {verifier_id} is not "
                    "downstream of every responsible writer"
                )
                raise _planning_context_invariant(
                    "planning_criterion_verifier_dependency",
                    message,
                    paths=(
                        f"/proposal/agents/{agent_indexes[verifier_id]}/dependencies",
                    ),
                    subjects=_planning_subjects(
                        (ResponseIssueSubjectKind.AGENT, verifier_id),
                        (ResponseIssueSubjectKind.CRITERION, criterion.id),
                        *(
                            (ResponseIssueSubjectKind.AGENT, item)
                            for item in missing_writers
                        ),
                    ),
                )
        if _requires_full_review_boundaries(criterion.description) and set(
            criterion.review_boundaries
        ) != set(_ALL_REVIEW_BOUNDARIES):
            raise _planning_context_invariant(
                "planning_criterion_review_boundaries",
                (
                    f"criterion {criterion.id} contains an unqualified trust-"
                    "boundary prohibition or safety guarantee and must "
                    "require top-level, "
                    "nested, alias-or-indirection, and failure-path Review "
                    "boundaries"
                ),
                paths=(
                    f"/proposal/acceptance_criteria/{criterion_index}/review_boundaries",
                ),
                subjects=_planning_subjects(
                    (ResponseIssueSubjectKind.CRITERION, criterion.id)
                ),
            )

    criterion_invariants = []
    for criterion_index, criterion in enumerate(body.acceptance_criteria):
        try:
            validate_criterion(criterion_index, criterion)
        except _PlanningContextInvariantError as error:
            criterion_invariants.append(error.invariant)
    independent_invariants.extend(criterion_invariants)

    if enforce_specialized_review_authority:
        review_scope_by_agent, review_scope_invariants = (
            _resolve_review_scope_assignments(
                criteria=body.acceptance_criteria,
                definition=body.product_definition,
                agents=body.agents,
                profile_criterion_ids=allowed_criterion_ids,
            )
        )
        independent_invariants.extend(review_scope_invariants)
        if not review_scope_invariants:
            independent_invariants.extend(
                _review_task_scope_invariants(
                    tasks=body.tasks,
                    review_scope_by_agent=review_scope_by_agent,
                )
            )

    if (
        source_request is not None
        and _requires_full_review_boundaries(source_request)
        and not any(
            set(criterion.review_boundaries) == set(_ALL_REVIEW_BOUNDARIES)
            for criterion in body.acceptance_criteria
        )
    ):
        independent_invariants.append(
            _PlanningInvariant(
                invariant_id="planning_request_review_boundaries",
                message=(
                    "the user request contains an unqualified trust-boundary "
                    "prohibition or safety guarantee, but no proposed acceptance "
                    "criterion preserves all "
                    "four Review boundaries"
                ),
                paths=("/proposal/acceptance_criteria",),
            )
        )

    missing_requirement_coverage = requirement_ids - covered_requirements
    if missing_requirement_coverage:
        message = "requirements lack observable acceptance coverage: " + ", ".join(
            sorted(missing_requirement_coverage)
        )
        independent_invariants.append(
            _PlanningInvariant(
                invariant_id="planning_requirement_acceptance_coverage",
                message=message,
                paths=("/proposal/acceptance_criteria",),
                subjects=_planning_subjects(
                    *(
                        (ResponseIssueSubjectKind.REQUIREMENT, item)
                        for item in missing_requirement_coverage
                    )
                ),
            )
        )

    if independent_invariants:
        raise _PlanningContextInvariantsError(tuple(independent_invariants))

    if question_contracts is None:
        return
    linked = {
        decision.resolved_question_id: decision
        for decision in body.decisions
        if decision.resolved_question_id is not None
    }
    if len(linked) != sum(
        decision.resolved_question_id is not None for decision in body.decisions
    ):
        duplicate_questions = tuple(
            sorted(
                {
                    decision.resolved_question_id
                    for decision in body.decisions
                    if decision.resolved_question_id is not None
                    and sum(
                        item.resolved_question_id == decision.resolved_question_id
                        for item in body.decisions
                    )
                    > 1
                }
            )
        )
        raise _planning_context_invariant(
            "planning_question_decision_unique",
            "a Planning question can resolve only one decision record",
            paths=("/proposal/decisions",),
            subjects=_planning_subjects(
                *(
                    (ResponseIssueSubjectKind.QUESTION, item)
                    for item in duplicate_questions
                )
            ),
        )
    if set(linked) != set(question_contracts):
        missing = set(question_contracts) - set(linked)
        invented = set(linked) - set(question_contracts)
        invariants: list[_PlanningInvariant] = []
        if missing:
            invariants.append(
                _PlanningInvariant(
                    invariant_id="planning_question_decision_completeness",
                    message=(
                        "proposal question-decision provenance is incomplete: missing "
                        + ", ".join(sorted(missing))
                    ),
                    paths=("/proposal/decisions",),
                    subjects=_planning_subjects(
                        *((ResponseIssueSubjectKind.QUESTION, item) for item in missing)
                    ),
                )
            )
        decision_ids = tuple(item.id for item in body.decisions)
        for question_id in sorted(invented):
            decision = linked[question_id]
            decision_index = decision_ids.index(decision.id)
            invariants.append(
                _PlanningInvariant(
                    invariant_id="planning_question_decision_completeness",
                    message=(
                        "proposal question-decision provenance is incomplete: "
                        f"unknown {question_id} for user-owned decision {decision.id}"
                    ),
                    paths=(f"/proposal/decisions/{decision_index}/provenance",),
                    subjects=_planning_subjects(
                        (ResponseIssueSubjectKind.DECISION, decision.id),
                        (ResponseIssueSubjectKind.QUESTION, question_id),
                    ),
                    failure_class=ResponseFailureClass.MISSING_USER_DECISION,
                    authority=ResponseIssueAuthority.USER,
                )
            )
        raise _PlanningContextInvariantsError(tuple(invariants))
    for question_id, contract in question_contracts.items():
        decision = linked[question_id]
        if (
            decision.category is not contract.category
            or decision.authority is not contract.owner
        ):
            decision_index = tuple(item.id for item in body.decisions).index(
                decision.id
            )
            raise _planning_context_invariant(
                "planning_question_decision_contract",
                f"decision for question {question_id} changed its category",
                paths=(f"/proposal/decisions/{decision_index}/category",),
                subjects=_planning_subjects(
                    (ResponseIssueSubjectKind.DECISION, decision.id),
                    (ResponseIssueSubjectKind.QUESTION, question_id),
                ),
            )
        if (
            contract.required_decision_id is not None
            and decision.id != contract.required_decision_id
        ):
            decision_index = tuple(item.id for item in body.decisions).index(
                decision.id
            )
            raise _planning_context_invariant(
                "planning_question_decision_identity",
                (
                    f"decision for question {question_id} must preserve Controller-"
                    f"bound ID {contract.required_decision_id}"
                ),
                paths=(f"/proposal/decisions/{decision_index}/id",),
                subjects=_planning_subjects(
                    (ResponseIssueSubjectKind.DECISION, decision.id),
                    (ResponseIssueSubjectKind.QUESTION, question_id),
                ),
            )


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
        if self.proposal is not None:
            validate_writer_task_scope_forecasts(
                self.proposal.tasks, self.proposal.agents
            )
        return self


def _planning_response_schema() -> dict[str, object]:
    """Require every field in the current live Planning contract."""

    schema = PlanningModelResponse.model_json_schema()
    definitions = schema.get("$defs")
    if not isinstance(definitions, dict):
        raise PlanningError("Planning response schema has no definitions")
    required_by_definition = {
        "PlanningQuestion": (
            "decision_category",
            "missing_evidence",
            "material_consequences",
            "product_definition_dimensions",
        ),
        "PlanningOption": ("product_definition_values",),
        "ProposedCriterion": (
            "requirement_ids",
            "verification_agent_ids",
            "review_boundaries",
        ),
        "PlanningDecisionRecord": ("provenance",),
        "PlanningProposalBody": (
            "product_definition",
            "non_goals",
            "decisions",
        ),
    }
    for definition_name, field_names in required_by_definition.items():
        definition = definitions.get(definition_name)
        if not isinstance(definition, dict):
            raise PlanningError(
                f"Planning response schema has no {definition_name} definition"
            )
        properties = definition.get("properties")
        required = definition.setdefault("required", [])
        if not isinstance(properties, dict) or not isinstance(required, list):
            raise PlanningError(
                f"Planning response schema has invalid {definition_name} fields"
            )
        for field_name in field_names:
            field_schema = properties.get(field_name)
            if not isinstance(field_schema, dict):
                raise PlanningError(
                    f"Planning response schema has no {definition_name}.{field_name}"
                )
            field_schema.pop("default", None)
            if field_name not in required:
                required.append(field_name)
    question_properties = definitions["PlanningQuestion"]["properties"]
    dimension_schema = question_properties["product_definition_dimensions"]
    dimension_schema["maxItems"] = len(ProductDefinitionDimension)
    option_properties = definitions["PlanningOption"]["properties"]
    option_values_schema = option_properties["product_definition_values"]
    option_values_schema["maxItems"] = len(ProductDefinitionDimension)
    for field_name in ("decision_category",):
        field_schema = question_properties[field_name]
        options = field_schema.get("anyOf")
        if not isinstance(options, list):
            raise PlanningError(
                f"Planning response schema has no nullable {field_name} union"
            )
        non_null = [option for option in options if option.get("type") != "null"]
        if len(non_null) != 1:
            raise PlanningError(
                f"Planning response schema has an invalid {field_name} union"
            )
        question_properties[field_name] = non_null[0]
    question_required = definitions["PlanningQuestion"]["required"]
    question_properties.pop("decision_owner", None)
    while "decision_owner" in question_required:
        question_required.remove("decision_owner")
    decision_definition = definitions["PlanningDecisionRecord"]
    decision_properties = decision_definition["properties"]
    provenance_schema = decision_properties["provenance"]
    provenance_options = provenance_schema.get("anyOf")
    if not isinstance(provenance_options, list):
        raise PlanningError(
            "Planning response schema has no nullable decision provenance union"
        )
    if (
        len([option for option in provenance_options if option.get("type") != "null"])
        != 1
    ):
        raise PlanningError("Planning response schema has invalid decision provenance")
    common_properties = {
        field: json.loads(json.dumps(decision_properties[field]))
        for field in ("id", "summary", "rationale")
    }

    def decision_branch(
        *,
        categories: tuple[PlanningDecisionCategory, ...],
        provenance_kind: PlanningDecisionProvenanceKind,
        source_schema: dict[str, object],
    ) -> dict[str, object]:
        return {
            "additionalProperties": False,
            "properties": {
                **json.loads(json.dumps(common_properties)),
                "category": {
                    "enum": [item.value for item in categories],
                    "type": "string",
                },
                "provenance": {
                    "additionalProperties": False,
                    "properties": {
                        "kind": {
                            "const": provenance_kind.value,
                            "type": "string",
                        },
                        "source": source_schema,
                    },
                    "required": ["kind", "source"],
                    "type": "object",
                },
            },
            "required": ["id", "category", "provenance", "summary", "rationale"],
            "type": "object",
        }

    user_categories = tuple(
        category
        for category, authority in _DECISION_AUTHORITY.items()
        if authority is PlanningDecisionAuthority.USER
    )
    planner_categories = tuple(
        category
        for category, authority in _DECISION_AUTHORITY.items()
        if authority is PlanningDecisionAuthority.PLANNER_PROPOSAL
    )
    agent_categories = tuple(
        category
        for category, authority in _DECISION_AUTHORITY.items()
        if authority is PlanningDecisionAuthority.AGENT_AUTONOMY
    )
    definitions["PlanningDecisionRecord"] = {
        "description": (
            "One decision whose category and typed provenance form an authorized "
            "atomic combination."
        ),
        "oneOf": [
            decision_branch(
                categories=user_categories,
                provenance_kind=PlanningDecisionProvenanceKind.EXPLICIT_INPUT,
                source_schema={"minLength": 1, "maxLength": 2000, "type": "string"},
            ),
            decision_branch(
                categories=user_categories,
                provenance_kind=PlanningDecisionProvenanceKind.RESOLVED_QUESTION,
                source_schema={
                    "maxLength": 2000,
                    "minLength": 1,
                    "pattern": "^[a-z][a-z0-9_]*$",
                    "type": "string",
                },
            ),
            decision_branch(
                categories=planner_categories,
                provenance_kind=PlanningDecisionProvenanceKind.PLANNER_RECOMMENDATION,
                source_schema={"const": "planner", "type": "string"},
            ),
            decision_branch(
                categories=agent_categories,
                provenance_kind=PlanningDecisionProvenanceKind.AGENT_AUTONOMY,
                source_schema={"const": "agent", "type": "string"},
            ),
        ],
        "title": "PlanningDecisionRecord",
    }
    proposal_properties = definitions["PlanningProposalBody"]["properties"]
    requirement_definition = ProposedRequirement.model_json_schema()
    nested_definitions = requirement_definition.pop("$defs", None)
    if nested_definitions:
        raise PlanningError("requirement response schema unexpectedly has definitions")
    definitions["ProposedRequirement"] = requirement_definition
    proposal_properties["requirements"] = {
        "description": (
            "Atomic requirement records. The controller compiles their IDs and "
            "descriptions into the backward-compatible internal representation."
        ),
        "items": {"$ref": "#/$defs/ProposedRequirement"},
        "minItems": 1,
        "title": "Requirements",
        "type": "array",
    }
    proposal_properties.pop("requirement_ids", None)
    proposal_required = definitions["PlanningProposalBody"]["required"]
    while "requirement_ids" in proposal_required:
        proposal_required.remove("requirement_ids")
    assumption_definition = ProposedAssumption.model_json_schema()
    nested_definitions = assumption_definition.pop("$defs", None)
    if nested_definitions:
        raise PlanningError("assumption response schema unexpectedly has definitions")
    definitions["ProposedAssumption"] = assumption_definition
    proposal_properties["assumptions"] = {
        "description": (
            "Atomic assumption records. The controller compiles each statement "
            "and its autonomy decision reference into the backward-compatible "
            "internal representation."
        ),
        "items": {"$ref": "#/$defs/ProposedAssumption"},
        "title": "Assumptions",
        "type": "array",
    }
    proposal_properties.pop("assumption_decision_ids", None)
    while "assumption_decision_ids" in proposal_required:
        proposal_required.remove("assumption_decision_ids")
    product_definition_schema = proposal_properties["product_definition"]
    product_options = product_definition_schema.get("anyOf")
    if not isinstance(product_options, list):
        raise PlanningError(
            "Planning response schema has no nullable product_definition union"
        )
    product_non_null = [
        option for option in product_options if option.get("type") != "null"
    ]
    if len(product_non_null) != 1:
        raise PlanningError(
            "Planning response schema has an invalid product_definition union"
        )
    proposal_properties["product_definition"] = product_non_null[0]
    workflow_schema = deepcopy(definitions["ProductDefinitionStatement"])
    workflow_schema["title"] = "PrimaryWorkflowStatement"
    workflow_schema["description"] = (
        "The product's core user workflow is always material, even for a "
        "one-time prototype. Reuse explicit input without unnecessary questions; "
        "ask only when the workflow is missing. Link it to its requirements."
    )
    workflow_properties = workflow_schema["properties"]
    workflow_properties["disposition"] = {
        "type": "string",
        "enum": [
            ProductDefinitionDisposition.EXPLICIT_INPUT.value,
            ProductDefinitionDisposition.RESOLVED_QUESTION.value,
        ],
    }
    workflow_properties["requirement_ids"].pop("default", None)
    workflow_properties["requirement_ids"]["minItems"] = 1
    workflow_schema["required"].append("requirement_ids")
    definitions["PrimaryWorkflowStatement"] = workflow_schema
    definitions["ProductDefinition"]["properties"]["primary_workflow"] = {
        "$ref": "#/$defs/PrimaryWorkflowStatement"
    }
    return schema


def _planning_regeneration_transport_schema(
    response_schema: dict[str, object],
) -> dict[str, object]:
    """Expose only JSON container and primitive types during proposal regeneration.

    The semantic schema remains the Controller's authority. This projection lets
    the model-facing tool reject scalar placeholders for proposal records without
    making upstream tool validation decide required fields, IDs, or policy.
    """

    def project(node: dict[str, object]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key in ("type", "$ref"):
            if key in node:
                result[key] = node[key]
        for key in ("properties", "$defs"):
            values = node.get(key)
            if isinstance(values, dict):
                result[key] = {
                    name: project(value)
                    for name, value in values.items()
                    if isinstance(name, str) and isinstance(value, dict)
                }
        items = node.get("items")
        if isinstance(items, dict):
            result["items"] = project(items)
        for key in ("anyOf", "oneOf", "allOf"):
            branches = node.get(key)
            if isinstance(branches, list):
                # Stripping discriminator constraints can make former oneOf
                # branches overlap; anyOf still preserves their type shapes.
                projected_key = "anyOf" if key == "oneOf" else key
                result[projected_key] = [
                    project(branch) for branch in branches if isinstance(branch, dict)
                ]
        return result

    projected = project(response_schema)
    if projected.get("type") != "object":
        raise PlanningError("Planning regeneration transport schema is not an object")
    return projected


def _validate_current_planning_response_wire(
    payload: dict[str, object],
    *,
    response_schema: dict[str, object],
) -> None:
    """Enforce current wire keys without breaking persisted legacy records."""

    definitions = response_schema.get("$defs")
    if not isinstance(definitions, dict):
        raise PlanningError("Planning response schema has no definitions")
    if payload.get("kind") == PlanningResponseKind.PROPOSAL.value:
        proposal = payload.get("proposal")
        agents = None if not isinstance(proposal, dict) else proposal.get("agents")
        missing = (
            ()
            if not isinstance(agents, list)
            else tuple(
                index
                for index, agent in enumerate(agents)
                if isinstance(agent, dict) and "specialization" not in agent
            )
        )
        if missing:
            raise _planning_model_invariant(
                "planning_current_specialization_wire_contract",
                "current Planning proposal Agents require specialization IDs",
                paths=tuple(
                    f"/proposal/agents/{index}/specialization" for index in missing
                ),
                subjects=_planning_subjects(
                    *(
                        (ResponseIssueSubjectKind.AGENT, str(agents[index].get("id")))
                        for index in missing
                        if isinstance(agents[index].get("id"), str)
                    )
                ),
            )
        return
    if payload.get("kind") != PlanningResponseKind.QUESTION.value:
        return
    question = payload.get("question")
    if not isinstance(question, dict):
        return
    definition = definitions.get("PlanningQuestion")
    if not isinstance(definition, dict):
        raise PlanningError("Planning response schema has no question definition")
    properties = definition.get("properties")
    required = definition.get("required")
    if not isinstance(properties, dict) or not isinstance(required, list):
        raise PlanningError("Planning response schema has invalid question fields")
    missing = sorted(set(required) - set(question))
    # decision_owner is compiled by the Controller after transport capture and is
    # therefore valid in the normalized payload even though models cannot submit it.
    unknown = sorted(set(question) - set(properties) - {"decision_owner"})
    options = question.get("options")
    missing_option_fields = (
        []
        if not isinstance(options, list)
        else [
            f"options[{index}].product_definition_values"
            for index, option in enumerate(options)
            if isinstance(option, dict) and "product_definition_values" not in option
        ]
    )
    if not missing and not unknown and not missing_option_fields:
        return
    details: list[str] = []
    if missing:
        details.append("missing current fields: " + ", ".join(missing))
    if unknown:
        details.append("unknown current fields: " + ", ".join(unknown))
    if missing_option_fields:
        details.append("missing current fields: " + ", ".join(missing_option_fields))
    question_id = question.get("id")
    subjects = (
        ()
        if not isinstance(question_id, str) or not question_id
        else _planning_subjects((ResponseIssueSubjectKind.QUESTION, question_id))
    )
    raise _planning_model_invariant(
        "planning_current_question_wire_contract",
        "Planning question violates its current wire contract ("
        + "; ".join(details)
        + ")",
        paths=("/question",),
        subjects=subjects,
    )


def _planning_proposal_body_for_model(
    body: PlanningProposalBody,
) -> dict[str, object]:
    """Project canonical persisted fields into the current model-facing contract."""

    payload = body.model_dump(mode="json")
    if len(body.requirements) == len(body.requirement_ids):
        payload["requirements"] = [
            {"id": requirement_id, "description": description}
            for requirement_id, description in zip(
                body.requirement_ids,
                body.requirements,
                strict=True,
            )
        ]
        payload.pop("requirement_ids", None)
    if len(body.assumptions) == len(body.assumption_decision_ids):
        payload["assumptions"] = [
            {"statement": statement, "decision_id": decision_id}
            for statement, decision_id in zip(
                body.assumptions,
                body.assumption_decision_ids,
                strict=True,
            )
        ]
        payload.pop("assumption_decision_ids", None)
    return payload


def _planning_question_response_schema(
    recovery: _PlanningClarificationRecovery,
) -> dict[str, object]:
    """Constrain recovery to one atomic Controller-identified user decision."""

    schema = _planning_response_schema()
    properties = schema.get("properties")
    definitions = schema.get("$defs")
    required = schema.get("required")
    if (
        not isinstance(properties, dict)
        or not isinstance(definitions, dict)
        or not isinstance(required, list)
    ):
        raise PlanningError("Planning response schema has an invalid root")
    question_schema = properties.get("question")
    question_options = (
        None if not isinstance(question_schema, dict) else question_schema.get("anyOf")
    )
    if not isinstance(question_options, list):
        raise PlanningError("Planning response schema has no nullable question union")
    question_non_null = [
        option for option in question_options if option.get("type") != "null"
    ]
    if len(question_non_null) != 1:
        raise PlanningError("Planning response schema has an invalid question union")
    properties["kind"] = {
        "const": PlanningResponseKind.QUESTION.value,
        "type": "string",
    }
    properties["question"] = question_non_null[0]
    properties.pop("proposal", None)
    required[:] = ["kind", "question"]

    definition = definitions.get("PlanningQuestion")
    question_properties = (
        None if not isinstance(definition, dict) else definition.get("properties")
    )
    if not isinstance(question_properties, dict):
        raise PlanningError("Planning response schema has no question definition")
    question_properties["decision_category"] = {
        "const": recovery.decision_category.value,
        "type": "string",
    }
    if recovery.question_id is not None:
        question_properties["id"] = {
            "const": recovery.question_id,
            "type": "string",
        }
    decision_may_bind_product_dimension = (
        recovery.dimension is None
        and recovery.decision_id is not None
        and recovery.decision_category is PlanningDecisionCategory.PRODUCT_REQUIREMENT
    )
    if decision_may_bind_product_dimension:
        question_properties["product_definition_dimensions"] = {
            "items": {
                "enum": [item.value for item in ProductDefinitionDimension],
                "type": "string",
            },
            "maxItems": 1,
            "type": "array",
            "uniqueItems": True,
        }
    elif recovery.dimension is None:
        question_properties["product_definition_dimensions"] = {
            "maxItems": 0,
            "type": "array",
        }
    else:
        question_properties["product_definition_dimensions"] = {
            "items": {"const": recovery.dimension.value, "type": "string"},
            "maxItems": 1,
            "minItems": 1,
            "type": "array",
        }
    option_definition = definitions.get("PlanningOption")
    option_properties = (
        None
        if not isinstance(option_definition, dict)
        else option_definition.get("properties")
    )
    option_value_definition = definitions.get("PlanningOptionValue")
    option_value_properties = (
        None
        if not isinstance(option_value_definition, dict)
        else option_value_definition.get("properties")
    )
    if not isinstance(option_properties, dict) or not isinstance(
        option_value_properties, dict
    ):
        raise PlanningError("Planning response schema has no option value definition")
    option_values = option_properties["product_definition_values"]
    if decision_may_bind_product_dimension:
        option_values.pop("minItems", None)
        option_values["maxItems"] = 1
    elif recovery.dimension is None:
        option_values.pop("minItems", None)
        option_values["maxItems"] = 0
    else:
        option_values.update({"maxItems": 1, "minItems": 1})
        option_value_properties["dimension"] = {
            "const": recovery.dimension.value,
            "type": "string",
        }
    return schema


def _planning_response_schema_for_correction(
    plan: SemanticCorrectionPlan,
    *,
    base_schema: dict[str, object] | None = None,
    profile_criterion_ids: Collection[str] = (),
) -> dict[str, object]:
    """Bind stable correction references when their identity owners are immutable."""

    schema = deepcopy(
        _planning_response_schema() if base_schema is None else base_schema
    )
    if "/question" in plan.evidence.target_paths:
        question_schema = schema["properties"]["question"]
        question_options = question_schema.get("anyOf")
        if isinstance(question_options, list):
            question_non_null = [
                option for option in question_options if option.get("type") != "null"
            ]
            if len(question_non_null) != 1:
                raise PlanningError(
                    "Planning response schema has an invalid question union"
                )
            schema["properties"]["question"] = question_non_null[0]
    if "/proposal" in plan.evidence.target_paths:
        proposal_schema = schema["properties"]["proposal"]
        proposal_options = proposal_schema.get("anyOf")
        if isinstance(proposal_options, list):
            proposal_non_null = [
                option for option in proposal_options if option.get("type") != "null"
            ]
            if len(proposal_non_null) != 1:
                raise PlanningError(
                    "Planning response schema has an invalid proposal union"
                )
            schema["properties"]["proposal"] = proposal_non_null[0]
    proposal = plan.base_payload.get("proposal")
    if not isinstance(proposal, dict):
        return schema

    target_paths = plan.evidence.target_paths

    def identity_owner_is_mutable(collection_path: str) -> bool:
        prefix = f"{collection_path}/"
        for target_path in target_paths:
            if target_path == "/" or target_path == collection_path:
                return True
            if collection_path.startswith(f"{target_path.rstrip('/')}/"):
                return True
            if not target_path.startswith(prefix):
                continue
            suffix = target_path[len(prefix) :].split("/")
            if len(suffix) > 1 and suffix[1] == "id":
                return True
        return False

    def record_ids(collection_name: str) -> list[str]:
        values = proposal.get(collection_name)
        if not isinstance(values, list):
            return []
        return list(
            dict.fromkeys(
                identifier
                for value in values
                if isinstance(value, dict)
                and isinstance((identifier := value.get("id")), str)
            )
        )

    requirement_ids = proposal.get("requirement_ids")
    stable_requirement_ids = (
        list(
            dict.fromkeys(value for value in requirement_ids if isinstance(value, str))
        )
        if isinstance(requirement_ids, list)
        else record_ids("requirements")
    )
    proposal_criterion_ids = record_ids("acceptance_criteria")
    stable_criterion_ids = list(
        dict.fromkeys((*proposal_criterion_ids, *profile_criterion_ids))
    )
    stable_decision_ids = record_ids("decisions")
    stable_agent_ids = record_ids("agents")
    stable_task_ids = record_ids("tasks")
    autonomous_ids = [
        decision["id"]
        for decision in proposal.get("decisions", [])
        if isinstance(decision, dict)
        and isinstance(decision.get("id"), str)
        and decision.get("authority") == PlanningDecisionAuthority.AGENT_AUTONOMY.value
    ]

    definitions = schema.get("$defs")
    if not isinstance(definitions, dict):
        raise PlanningError("Planning response schema has no definitions")

    def definition_properties(definition_name: str) -> dict[str, object]:
        definition = definitions.get(definition_name)
        properties = (
            definition.get("properties") if isinstance(definition, dict) else None
        )
        if not isinstance(properties, dict):
            raise PlanningError(
                f"Planning response schema has no {definition_name} fields"
            )
        return properties

    def bind_array_items(
        definition_name: str,
        field_name: str,
        allowed_ids: list[str],
    ) -> None:
        field_schema = definition_properties(definition_name).get(field_name)
        items = field_schema.get("items") if isinstance(field_schema, dict) else None
        if not isinstance(items, dict):
            raise PlanningError(
                "Planning response schema has no "
                f"{definition_name}.{field_name} item contract"
            )
        items["enum"] = allowed_ids

    def bind_scalar(
        definition_name: str,
        field_name: str,
        allowed_ids: list[str],
    ) -> None:
        field_schema = definition_properties(definition_name).get(field_name)
        if not isinstance(field_schema, dict):
            raise PlanningError(
                f"Planning response schema has no {definition_name}.{field_name}"
            )
        field_schema["enum"] = allowed_ids

    def pin_record_identity(
        item: dict[str, object],
        identifier: str,
    ) -> dict[str, object]:
        """Bind one record slot to its stable ID without dropping its contract.

        Record identity has exactly one owner. Scope projections that rebuild a
        collection's ``prefixItems`` re-apply this binding instead of replacing
        it, so a later writer cannot silently release an untargeted record.
        """

        properties = item.get("properties")
        if not isinstance(properties, dict):
            properties = {}
            item["properties"] = properties
        properties["id"] = {"const": identifier, "type": "string"}
        required = item.get("required")
        if isinstance(required, list):
            if "id" not in required:
                required.append("id")
        else:
            item["required"] = ["id"]
        item.setdefault("type", "object")
        return item

    def pin_collection_identities(
        items: list[dict[str, object]],
        stable_ids: list[str],
    ) -> list[dict[str, object]]:
        """Re-apply stable identity across one rebuilt record collection."""

        if len(items) != len(stable_ids):
            raise PlanningError(
                "Planning response schema record projection lost its identity alignment"
            )
        return [
            pin_record_identity(item, identifier)
            for item, identifier in zip(items, stable_ids, strict=True)
        ]

    def bind_record_ids(
        collection_name: str,
        definition_name: str,
        stable_ids: list[str],
    ) -> None:
        collection_schema = definition_properties("PlanningProposalBody").get(
            collection_name
        )
        definition = definitions.get(definition_name)
        if not isinstance(collection_schema, dict) or not isinstance(definition, dict):
            raise PlanningError(
                "Planning response schema has no stable identity contract for "
                f"{collection_name}"
            )
        collection_schema["prefixItems"] = [
            pin_record_identity({"$ref": f"#/$defs/{definition_name}"}, identifier)
            for identifier in stable_ids
        ]

    if not identity_owner_is_mutable("/proposal/requirements"):
        for definition_name in (
            "ProductDefinitionStatement",
            "PrimaryWorkflowStatement",
            "ProductMaturityDefinition",
            "ProposedCriterion",
        ):
            bind_array_items(
                definition_name,
                "requirement_ids",
                stable_requirement_ids,
            )
    if not identity_owner_is_mutable("/proposal/acceptance_criteria"):
        for definition_name in (
            "ProductDefinitionStatement",
            "PrimaryWorkflowStatement",
            "ProductMaturityDefinition",
        ):
            bind_array_items(
                definition_name,
                "criterion_ids",
                stable_criterion_ids,
            )
        bind_array_items(
            "ProposedTask",
            "acceptance_criteria",
            stable_criterion_ids,
        )
    if not identity_owner_is_mutable("/proposal/decisions"):
        for definition_name in (
            "ProductDefinitionStatement",
            "PrimaryWorkflowStatement",
            "ProductMaturityDefinition",
        ):
            bind_array_items(
                definition_name,
                "decision_ids",
                stable_decision_ids,
            )
        bind_scalar("ProposedAssumption", "decision_id", autonomous_ids)
    if not identity_owner_is_mutable("/proposal/agents"):
        bind_array_items(
            "ProposedCriterion",
            "verification_agent_ids",
            stable_agent_ids,
        )
        bind_array_items("ProposedAgent", "dependencies", stable_agent_ids)
        bind_scalar("ProposedTask", "owner_agent_id", stable_agent_ids)
    if not identity_owner_is_mutable("/proposal/tasks"):
        bind_array_items("ProposedTask", "dependencies", stable_task_ids)

    stable_identity_collections = (
        (
            "requirements",
            "ProposedRequirement",
            stable_requirement_ids,
        ),
        (
            "acceptance_criteria",
            "ProposedCriterion",
            proposal_criterion_ids,
        ),
        ("decisions", "PlanningDecisionRecord", stable_decision_ids),
        ("agents", "ProposedAgent", stable_agent_ids),
        ("tasks", "ProposedTask", stable_task_ids),
    )
    for collection_name, definition_name, stable_ids in stable_identity_collections:
        if not identity_owner_is_mutable(f"/proposal/{collection_name}"):
            bind_record_ids(collection_name, definition_name, stable_ids)

    review_task_paths = {
        issue.path
        for issue in plan.diagnostic.issues
        if issue.invariant_id == "planning_review_task_scope"
        and issue.path in target_paths
        and re.fullmatch(r"/proposal/tasks/[0-9]+", issue.path) is not None
    }
    if review_task_paths:
        mutable_scope_inputs = tuple(
            path
            for path in (
                "/proposal/acceptance_criteria",
                "/proposal/agents",
                "/proposal/product_definition",
            )
            if identity_owner_is_mutable(path)
        )
        if mutable_scope_inputs:
            raise PlanningError(
                "Review task correction scope cannot be compiled while its "
                "acceptance authority inputs are mutable: "
                + ", ".join(mutable_scope_inputs)
            )

        raw_criteria = proposal.get("acceptance_criteria")
        raw_agents = proposal.get("agents")
        raw_definition = proposal.get("product_definition")
        raw_tasks = proposal.get("tasks")
        if not isinstance(raw_criteria, list) or not isinstance(raw_agents, list):
            raise PlanningError(
                "Review task correction requires typed criteria and Agents"
            )
        if not isinstance(raw_tasks, list):
            raise PlanningError("Review task correction requires typed tasks")
        try:
            criteria = tuple(
                ProposedCriterion.model_validate(item) for item in raw_criteria
            )
            agents = tuple(ProposedAgent.model_validate(item) for item in raw_agents)
            definition = (
                None
                if raw_definition is None
                else ProductDefinition.model_validate(raw_definition)
            )
            tasks = tuple(ProposedTask.model_validate(item) for item in raw_tasks)
        except ValidationError as error:
            raise PlanningError(
                "Review task correction authority inputs are not structurally valid"
            ) from error

        review_scopes, scope_invariants = _resolve_review_scope_assignments(
            criteria=criteria,
            definition=definition,
            agents=agents,
            profile_criterion_ids=profile_criterion_ids,
        )
        if scope_invariants:
            raise PlanningError(
                "Review task correction scope cannot be compiled from unresolved "
                "Review authority"
            )

        task_definition = definitions.get("ProposedTask")
        if not isinstance(task_definition, dict):
            raise PlanningError("Planning response schema has no task definition")
        task_items: list[dict[str, object]] = [
            {"$ref": "#/$defs/ProposedTask"} for _ in tasks
        ]
        for path in review_task_paths:
            task_index = int(path.rsplit("/", 1)[1])
            if task_index >= len(tasks):
                raise PlanningError("Review task correction index is out of range")
            task = tasks[task_index]
            scope = review_scopes.get(task.owner_agent_id)
            if scope is None:
                raise PlanningError(
                    "Review task correction owner has no compiled Review scope"
                )
            scoped_task = deepcopy(task_definition)
            scoped_properties = scoped_task.get("properties")
            if not isinstance(scoped_properties, dict):
                raise PlanningError(
                    "Planning response schema has no task field contract"
                )
            scoped_properties["id"] = {
                "const": task.id,
                "type": "string",
            }
            scoped_properties["owner_agent_id"] = {
                "const": task.owner_agent_id,
                "type": "string",
            }
            acceptance_schema = scoped_properties.get("acceptance_criteria")
            acceptance_items = (
                acceptance_schema.get("items")
                if isinstance(acceptance_schema, dict)
                else None
            )
            if not isinstance(acceptance_items, dict):
                raise PlanningError(
                    "Planning response schema has no task acceptance contract"
                )
            acceptance_items["enum"] = list(scope)
            acceptance_schema["uniqueItems"] = True
            task_items[task_index] = scoped_task

        tasks_schema = definition_properties("PlanningProposalBody").get("tasks")
        if not isinstance(tasks_schema, dict):
            raise PlanningError("Planning response schema has no task collection")
        if not identity_owner_is_mutable("/proposal/tasks"):
            task_items = pin_collection_identities(
                task_items,
                [task.id for task in tasks],
            )
        tasks_schema["prefixItems"] = task_items

    writer_coverage_issues = tuple(
        issue
        for issue in plan.diagnostic.issues
        if issue.invariant_id == "planning_writer_criterion_coverage"
        and issue.path in target_paths
        and (
            issue.path == "/proposal/tasks"
            or re.fullmatch(
                r"/proposal/tasks/[0-9]+/acceptance_criteria",
                issue.path,
            )
            is not None
        )
    )
    if writer_coverage_issues:
        coverage_only_task_rewrite = any(
            issue.path == "/proposal/tasks" for issue in writer_coverage_issues
        ) and all(
            issue.invariant_id == "planning_writer_criterion_coverage"
            for issue in plan.diagnostic.issues
            if issue.authority is ResponseIssueAuthority.MODEL
        )
        mutable_coverage_inputs = tuple(
            path
            for path in (
                "/proposal/acceptance_criteria",
                "/proposal/agents",
            )
            if identity_owner_is_mutable(path)
        )
        if mutable_coverage_inputs:
            raise PlanningError(
                "Writer coverage correction cannot be compiled while its "
                "criterion or Agent authority is mutable: "
                + ", ".join(mutable_coverage_inputs)
            )

        raw_agents = proposal.get("agents")
        raw_tasks = proposal.get("tasks")
        if not isinstance(raw_agents, list) or not isinstance(raw_tasks, list):
            raise PlanningError(
                "Writer coverage correction requires typed Agents and tasks"
            )
        try:
            agents = tuple(ProposedAgent.model_validate(item) for item in raw_agents)
            tasks = tuple(ProposedTask.model_validate(item) for item in raw_tasks)
        except ValidationError as error:
            raise PlanningError(
                "Writer coverage correction authority inputs are not structurally valid"
            ) from error

        missing_criterion_ids = tuple(
            dict.fromkeys(
                subject.identifier
                for issue in writer_coverage_issues
                for subject in issue.subjects
                if subject.kind is ResponseIssueSubjectKind.CRITERION
            )
        )
        if not missing_criterion_ids or any(
            criterion_id not in stable_criterion_ids
            for criterion_id in missing_criterion_ids
        ):
            raise PlanningError(
                "Writer coverage correction has no exact known criterion subjects"
            )
        writer_ids = tuple(
            agent.id
            for agent in agents
            if agent.capability
            in {AgentCapability.IMPLEMENTATION, AgentCapability.INTEGRATION}
        )
        if not writer_ids:
            raise PlanningError("Writer coverage correction has no writer Agents")

        task_definition = definitions.get("ProposedTask")
        if not isinstance(task_definition, dict):
            raise PlanningError("Planning response schema has no task definition")
        exact_task_indexes = {
            int(issue.path.split("/")[3])
            for issue in writer_coverage_issues
            if issue.path != "/proposal/tasks"
        }
        if any(index >= len(tasks) for index in exact_task_indexes):
            raise PlanningError("Writer coverage correction index is out of range")
        task_items: list[dict[str, object]] = []
        for task_index, task in enumerate(tasks):
            scoped_task = deepcopy(task_definition)
            scoped_properties = scoped_task.get("properties")
            if not isinstance(scoped_properties, dict):
                raise PlanningError(
                    "Planning response schema has no task field contract"
                )
            scoped_properties["id"] = {
                "const": task.id,
                "type": "string",
            }
            scoped_properties["owner_agent_id"] = {
                "const": task.owner_agent_id,
                "type": "string",
            }
            acceptance_schema = scoped_properties.get("acceptance_criteria")
            acceptance_items = (
                acceptance_schema.get("items")
                if isinstance(acceptance_schema, dict)
                else None
            )
            dependencies_schema = scoped_properties.get("dependencies")
            dependency_items = (
                dependencies_schema.get("items")
                if isinstance(dependencies_schema, dict)
                else None
            )
            if not isinstance(acceptance_items, dict) or not isinstance(
                dependency_items, dict
            ):
                raise PlanningError(
                    "Planning response schema has no task relation contract"
                )
            acceptance_items["enum"] = stable_criterion_ids
            acceptance_schema["uniqueItems"] = True
            if coverage_only_task_rewrite:
                scoped_properties["description"] = {
                    "const": task.description,
                    "type": "string",
                }
                scoped_properties["dependencies"] = {
                    "const": list(task.dependencies),
                    "type": "array",
                }
                scoped_properties["expected_paths"] = {
                    "const": list(task.expected_paths),
                    "type": "array",
                }
                if task.owner_agent_id in writer_ids:
                    acceptance_schema["allOf"] = [
                        {"contains": {"const": criterion_id}}
                        for criterion_id in task.acceptance_criteria
                    ]
                else:
                    scoped_properties["acceptance_criteria"] = {
                        "const": list(task.acceptance_criteria),
                        "type": "array",
                    }
            if task_index in exact_task_indexes:
                acceptance_schema["allOf"] = [
                    {"contains": {"const": criterion_id}}
                    for criterion_id in missing_criterion_ids
                ]
            dependency_items["enum"] = stable_task_ids
            dependencies_schema["uniqueItems"] = True
            task_items.append(scoped_task)

        tasks_schema = definition_properties("PlanningProposalBody").get("tasks")
        if not isinstance(tasks_schema, dict):
            raise PlanningError("Planning response schema has no task collection")
        if not identity_owner_is_mutable("/proposal/tasks"):
            task_items = pin_collection_identities(
                task_items,
                [task.id for task in tasks],
            )
        tasks_schema["prefixItems"] = task_items
        if any(issue.path == "/proposal/tasks" for issue in writer_coverage_issues):
            tasks_schema.pop("items", None)
            tasks_schema.update(
                {
                    "minItems": len(tasks),
                    "maxItems": len(tasks),
                    "allOf": [
                        {
                            "contains": {
                                "type": "object",
                                "properties": {
                                    "owner_agent_id": {
                                        "type": "string",
                                        "enum": list(writer_ids),
                                    },
                                    "acceptance_criteria": {
                                        "type": "array",
                                        "contains": {"const": criterion_id},
                                    },
                                },
                                "required": [
                                    "owner_agent_id",
                                    "acceptance_criteria",
                                ],
                            },
                            "minContains": 1,
                        }
                        for criterion_id in missing_criterion_ids
                    ],
                }
            )
    return schema


def _planning_request_prompt_context(request: PlanningRequest) -> dict[str, object]:
    """Project only model-relevant request facts into the Planning prompt."""

    return {
        "project_name": request.project_name,
        "source_request": request.source_request,
        "execution_profile": list(request.execution_profile),
        "base_constraints": list(request.base_constraints),
    }
