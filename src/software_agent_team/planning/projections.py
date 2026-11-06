"""Planning graph projections, dependency compilation, and semantic repairs.

This module owns:
    - Graph projections: Task dependencies, decision provenance relations,
      atomic requirement relations, and unique specialist verifier assignments.
    - Wire payload normalization: Semantic-preserving normalizations before validation.
    - Semantic corrections: Automated repair candidate binding for graph invariants,
      missing specialist roles, and broken references.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Collection, Mapping
from copy import deepcopy

from pydantic import ValidationError

from software_agent_team.artifacts import (
    ProductDefinition,
    ProductDefinitionDimension,
    ProductDefinitionDisposition,
    ProductDefinitionStatement,
)
from software_agent_team.planning.common import (
    MAX_PLANNING_DECISION_SUMMARY_CHARACTERS,
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
    _planning_context_invariant,
    _planning_subjects,
    _planning_user_decision_invariant,
    _PlanningClarificationRecovery,
    _PlanningInvariant,
    _PlanningModelInvariantError,
)
from software_agent_team.planning.models_basic import (
    _DECISION_AUTHORITY,
    _DECISION_PROVENANCE_AUTHORITY,
    _PRODUCT_DIMENSION_DECISION_CATEGORY,
    PlanningDecisionAuthority,
    PlanningDecisionCategory,
    PlanningDecisionProvenance,
    PlanningDecisionProvenanceKind,
    PlanningResponseKind,
)
from software_agent_team.planning.proposals import PlanningProposalBody
from software_agent_team.planning.questions import (
    PLANNING_OPTION_DESCRIPTION_MAX_LENGTH,
    PlanningDecisionRecord,
    PlanningQuestion,
)
from software_agent_team.planning.scopes import (
    _resolve_review_scope_assignments,
    _specialized_review_authority,
)
from software_agent_team.planning_normalization import (
    canonicalize_model_path,
    normalize_response_envelope,
    strip_redundant_stable_id_prefix,
)
from software_agent_team.response_corrections import (
    MAX_CORRECTION_FIELDS,
    ResponseFailureClass,
    ResponseIssueAuthority,
    ResponseIssueSubjectKind,
    ResponseValidationDiagnostic,
    ResponseValidationIssue,
    SemanticCorrectionCandidate,
    SemanticCorrectionCandidateSlot,
    SemanticCorrectionPlan,
    SemanticCorrectionSubmissionError,
    attach_semantic_correction_candidates,
    build_semantic_correction_plan,
    diagnostic_from_invariant,
    diagnostic_from_validation_error,
    require_all_semantic_correction_targets,
)
from software_agent_team.submissions import canonical_json_sha256
from software_agent_team.teams import (
    AcceptanceAuthority,
    AgentCapability,
    AgentSpecialization,
    ModelRoutePlan,
    specialization_contract,
)


def _compile_task_dependency_projection(
    proposal: dict[str, object],
) -> tuple[str, ...]:
    """Compile cross-Agent task edges from the authoritative Agent DAG."""

    raw_agents = proposal.get("agents")
    raw_tasks = proposal.get("tasks")
    if not isinstance(raw_agents, list) or not isinstance(raw_tasks, list):
        return ()
    if not all(isinstance(agent, dict) for agent in raw_agents) or not all(
        isinstance(task, dict) for task in raw_tasks
    ):
        return ()

    agent_ids = [agent.get("id") for agent in raw_agents]
    task_ids = [task.get("id") for task in raw_tasks]
    if (
        not all(isinstance(agent_id, str) for agent_id in agent_ids)
        or len(agent_ids) != len(set(agent_ids))
        or not all(isinstance(task_id, str) for task_id in task_ids)
        or len(task_ids) != len(set(task_ids))
    ):
        return ()
    known_agents = set(agent_ids)
    known_tasks = set(task_ids)

    agent_dependencies: dict[str, tuple[str, ...]] = {}
    for agent_id, agent in zip(agent_ids, raw_agents, strict=True):
        dependencies = agent.get("dependencies", [])
        if not isinstance(dependencies, list) or not all(
            isinstance(item, str) for item in dependencies
        ):
            return ()
        if not set(dependencies).issubset(known_agents):
            return ()
        agent_dependencies[agent_id] = tuple(dependencies)

    task_owners: dict[str, str] = {}
    task_dependencies: dict[str, tuple[str, ...]] = {}
    for task_id, task in zip(task_ids, raw_tasks, strict=True):
        owner = task.get("owner_agent_id")
        dependencies = task.get("dependencies", [])
        if (
            not isinstance(owner, str)
            or owner not in known_agents
            or not isinstance(dependencies, list)
            or not all(isinstance(item, str) for item in dependencies)
            or not set(dependencies).issubset(known_tasks)
        ):
            return ()
        task_owners[task_id] = owner
        task_dependencies[task_id] = tuple(dependencies)

    def transitive_agent_dependencies(agent_id: str) -> set[str]:
        pending = list(agent_dependencies[agent_id])
        seen: set[str] = set()
        while pending:
            dependency = pending.pop()
            if dependency in seen:
                continue
            seen.add(dependency)
            pending.extend(agent_dependencies[dependency])
        return seen

    changed = False
    for task_id, task in zip(task_ids, raw_tasks, strict=True):
        owner = task_owners[task_id]
        direct_upstream_agents = set(agent_dependencies[owner])
        all_upstream_agents = transitive_agent_dependencies(owner)
        existing = task_dependencies[task_id]
        same_owner = [
            dependency for dependency in existing if task_owners[dependency] == owner
        ]
        derived_cross_agent = [
            candidate_id
            for candidate_id in task_ids
            if task_owners[candidate_id] in direct_upstream_agents
        ]
        unauthorized_cross_agent = [
            dependency
            for dependency in existing
            if task_owners[dependency] != owner
            and task_owners[dependency] not in all_upstream_agents
        ]
        canonical = list(
            dict.fromkeys(
                (*same_owner, *derived_cross_agent, *unauthorized_cross_agent)
            )
        )
        if list(existing) == canonical:
            continue
        task["dependencies"] = canonical
        changed = True
    if not changed:
        return ()
    return ("compiled cross-Agent task dependencies from the authoritative Agent DAG",)


def _normalize_planning_decision_relations(
    proposal: dict[str, object],
    product_dimensions: dict[str, object],
    *,
    user_inputs: Collection[str],
    changes: list[str],
) -> None:
    """Compile decision identities and user-grounded relations in one owner."""

    decisions = proposal.get("decisions")
    assumption_decision_ids = proposal.get("assumption_decision_ids")
    if isinstance(decisions, list):
        canonical_id_counts: dict[str, int] = {}
        for decision in decisions:
            decision_id = decision.get("id") if isinstance(decision, dict) else None
            if (
                isinstance(decision_id, str)
                and re.fullmatch(r"DECISION_[A-Za-z0-9_]+", decision_id) is not None
            ):
                canonical_id = decision_id.upper()
                canonical_id_counts[canonical_id] = (
                    canonical_id_counts.get(canonical_id, 0) + 1
                )
        for decision_index, decision in enumerate(decisions):
            if not isinstance(decision, dict):
                continue
            decision_id = decision.get("id")
            if (
                not isinstance(decision_id, str)
                or re.fullmatch(r"DECISION_[A-Za-z0-9_]+", decision_id) is None
            ):
                continue
            canonical_id = decision_id.upper()
            if (
                canonical_id == decision_id
                or canonical_id_counts.get(canonical_id) != 1
            ):
                continue
            decision["id"] = canonical_id
            changes.append(
                f"canonicalized proposal.decisions[{decision_index}].id as "
                f"{canonical_id}"
            )
        if isinstance(assumption_decision_ids, list):
            for reference_index, reference in enumerate(assumption_decision_ids):
                if (
                    not isinstance(reference, str)
                    or re.fullmatch(r"DECISION_[A-Za-z0-9_]+", reference) is None
                ):
                    continue
                canonical_id = reference.upper()
                if (
                    canonical_id == reference
                    or canonical_id_counts.get(canonical_id) != 1
                ):
                    continue
                assumption_decision_ids[reference_index] = canonical_id
                changes.append(
                    "canonicalized "
                    f"proposal.assumption_decision_ids[{reference_index}] as "
                    f"{canonical_id}"
                )

        explicit_sources_by_decision: dict[str, set[str]] = {}
        for dimension in ProductDefinitionDimension:
            item = product_dimensions.get(dimension.value)
            if not isinstance(item, dict):
                continue
            references = item.get("decision_ids")
            if not isinstance(references, list):
                continue
            for reference_index, reference in enumerate(references):
                if (
                    not isinstance(reference, str)
                    or re.fullmatch(r"DECISION_[A-Za-z0-9_]+", reference) is None
                ):
                    continue
                canonical_id = reference.upper()
                if (
                    canonical_id != reference
                    and canonical_id_counts.get(canonical_id) == 1
                ):
                    references[reference_index] = canonical_id
                    changes.append(
                        "canonicalized proposal.product_definition."
                        f"{dimension.value}.decision_ids[{reference_index}] as "
                        f"{canonical_id}"
                    )
                if item.get(
                    "disposition"
                ) == ProductDefinitionDisposition.EXPLICIT_INPUT.value and isinstance(
                    item.get("source"), str
                ):
                    explicit_sources_by_decision.setdefault(canonical_id, set()).add(
                        item["source"]
                    )

        explicit_product_sources = {
            (
                _PRODUCT_DIMENSION_DECISION_CATEGORY[dimension],
                _normalized_evidence_text(item["source"]),
            )
            for dimension in ProductDefinitionDimension
            if isinstance((item := product_dimensions.get(dimension.value)), dict)
            and item.get("disposition")
            == ProductDefinitionDisposition.EXPLICIT_INPUT.value
            and isinstance(item.get("source"), str)
            and item["source"].strip()
        }
        normalized_user_inputs = tuple(
            _normalized_evidence_text(value)
            for value in user_inputs
            if isinstance(value, str) and value.strip()
        )
        redundant_direct_product_decision_ids: set[str] = set()
        for decision_index, decision in enumerate(decisions):
            if not isinstance(decision, dict):
                continue
            category_value = decision.get("category")
            try:
                category = PlanningDecisionCategory(category_value)
            except (TypeError, ValueError):
                continue
            expected_authority = _DECISION_AUTHORITY[category]
            if decision.get("authority") != expected_authority.value:
                decision["authority"] = expected_authority.value
                changes.append(
                    "compiled proposal.decisions"
                    f"[{decision_index}].authority from category {category.value}"
                )

            provenance = decision.get("provenance")
            legacy_question_id = decision.get("question_id")
            if provenance is None:
                compiled_provenance: dict[str, str] | None = None
                if isinstance(legacy_question_id, str) and legacy_question_id:
                    compiled_provenance = {
                        "kind": PlanningDecisionProvenanceKind.RESOLVED_QUESTION.value,
                        "source": legacy_question_id,
                    }
                elif expected_authority is PlanningDecisionAuthority.PLANNER_PROPOSAL:
                    compiled_provenance = {
                        "kind": (
                            PlanningDecisionProvenanceKind.PLANNER_RECOMMENDATION.value
                        ),
                        "source": "planner",
                    }
                elif expected_authority is PlanningDecisionAuthority.AGENT_AUTONOMY:
                    compiled_provenance = {
                        "kind": PlanningDecisionProvenanceKind.AGENT_AUTONOMY.value,
                        "source": "agent",
                    }
                elif expected_authority is PlanningDecisionAuthority.USER:
                    decision_id = decision.get("id")
                    candidates = (
                        explicit_sources_by_decision.get(decision_id, set())
                        if isinstance(decision_id, str)
                        else set()
                    )
                    came_from_product_definition = bool(candidates)
                    summary = decision.get("summary")
                    if (
                        not candidates
                        and isinstance(summary, str)
                        and any(
                            _normalized_evidence_text(summary) in value
                            for value in normalized_user_inputs
                        )
                    ):
                        candidates = {summary}
                    if len(candidates) == 1:
                        compiled_provenance = {
                            "kind": PlanningDecisionProvenanceKind.EXPLICIT_INPUT.value,
                            "source": next(iter(candidates)),
                        }
                        decision_id = decision.get("id")
                        if came_from_product_definition and isinstance(
                            decision_id, str
                        ):
                            redundant_direct_product_decision_ids.add(decision_id)
                if compiled_provenance is not None:
                    decision["provenance"] = compiled_provenance
                    changes.append(
                        "compiled proposal.decisions"
                        f"[{decision_index}].provenance from existing decision source"
                    )
            effective_provenance = decision.get("provenance")
            if (
                isinstance(effective_provenance, dict)
                and effective_provenance.get("kind")
                == PlanningDecisionProvenanceKind.EXPLICIT_INPUT.value
                and isinstance(effective_provenance.get("source"), str)
                and len(effective_provenance["source"])
                <= MAX_PLANNING_DECISION_SUMMARY_CHARACTERS
                and decision.get("summary") != effective_provenance["source"]
            ):
                decision["summary"] = effective_provenance["source"]
                changes.append(
                    "compiled proposal.decisions"
                    f"[{decision_index}].summary from exact direct-input source"
                )
            if (
                isinstance(decision.get("id"), str)
                and isinstance(effective_provenance, dict)
                and effective_provenance.get("kind")
                == PlanningDecisionProvenanceKind.EXPLICIT_INPUT.value
                and isinstance(effective_provenance.get("source"), str)
                and (
                    category,
                    _normalized_evidence_text(effective_provenance["source"]),
                )
                in explicit_product_sources
            ):
                redundant_direct_product_decision_ids.add(decision["id"])
            if (
                isinstance(legacy_question_id, str)
                and isinstance(decision.get("provenance"), dict)
                and decision["provenance"].get("kind")
                == PlanningDecisionProvenanceKind.RESOLVED_QUESTION.value
                and decision["provenance"].get("source") == legacy_question_id
            ):
                del decision["question_id"]
                changes.append(
                    f"retired legacy proposal.decisions[{decision_index}].question_id"
                )

        for dimension in ProductDefinitionDimension:
            item = product_dimensions.get(dimension.value)
            if not isinstance(item, dict):
                continue
            references = item.get("decision_ids")
            if (
                item.get("disposition")
                == ProductDefinitionDisposition.EXPLICIT_INPUT.value
                and isinstance(references, list)
                and references
                and (item.get("requirement_ids") or item.get("criterion_ids"))
            ):
                item["decision_ids"] = []
                changes.append(
                    "removed redundant decision references from "
                    f"proposal.product_definition.{dimension.value}"
                )

        remaining_decision_references = {
            reference
            for dimension in ProductDefinitionDimension
            if isinstance((item := product_dimensions.get(dimension.value)), dict)
            and isinstance(item.get("decision_ids"), list)
            for reference in item["decision_ids"]
            if isinstance(reference, str)
        }
        if isinstance(assumption_decision_ids, list):
            remaining_decision_references.update(
                reference
                for reference in assumption_decision_ids
                if isinstance(reference, str)
            )
        retained_decisions: list[object] = []
        for decision_index, decision in enumerate(decisions):
            decision_id = decision.get("id") if isinstance(decision, dict) else None
            if (
                isinstance(decision_id, str)
                and decision_id in redundant_direct_product_decision_ids
                and decision_id not in remaining_decision_references
            ):
                changes.append(
                    "removed redundant direct-input decision "
                    f"{decision_id} from proposal.decisions[{decision_index}]"
                )
                continue
            retained_decisions.append(decision)
        proposal["decisions"] = retained_decisions


def _compile_atomic_requirement_relations(
    proposal: dict[str, object], changes: list[str]
) -> None:
    """Compile model-authored requirement atoms without changing their authority."""

    requirements = proposal.get("requirements")
    if isinstance(requirements, list) and any(
        isinstance(item, dict) for item in requirements
    ):
        if not all(isinstance(item, dict) for item in requirements):
            raise _planning_context_invariant(
                "planning_requirement_atom_shape",
                (
                    "proposal requirements must be one array of atomic objects; "
                    "string and object entries cannot be mixed"
                ),
                paths=("/proposal/requirements",),
            )
        compiled_requirements: list[ProposedRequirement] = []
        for requirement_index, item in enumerate(requirements):
            try:
                compiled_requirements.append(ProposedRequirement.model_validate(item))
            except ValidationError as error:
                raise _planning_context_invariant(
                    "planning_requirement_atom_schema",
                    (
                        f"proposal requirement {requirement_index} must contain "
                        "exactly one stable REQ_ id and one non-empty description: "
                        f"{_safe_validation_detail(error)}"
                    ),
                    paths=(f"/proposal/requirements/{requirement_index}",),
                ) from error
        compiled_ids = tuple(item.id for item in compiled_requirements)
        compiled_descriptions = tuple(
            item.description for item in compiled_requirements
        )
        if len(compiled_ids) != len(set(compiled_ids)):
            raise _planning_context_invariant(
                "planning_requirement_atom_id_unique",
                "proposal requirement objects must use unique stable REQ_ IDs",
                paths=("/proposal/requirements",),
            )
        if len(compiled_descriptions) != len(set(compiled_descriptions)):
            raise _planning_context_invariant(
                "planning_requirement_atom_description_unique",
                "proposal requirement objects must use unique descriptions",
                paths=("/proposal/requirements",),
            )
        proposal["requirements"] = list(compiled_descriptions)
        proposal["requirement_ids"] = list(compiled_ids)
        requirements = proposal["requirements"]
        changes.append(
            "compiled atomic proposal.requirements into canonical descriptions "
            "and stable IDs"
        )
    requirement_ids = proposal.get("requirement_ids")
    if (
        isinstance(requirements, list)
        and isinstance(requirement_ids, list)
        and len(requirements) == len(requirement_ids)
    ):
        for requirement_index, (description, requirement_id) in enumerate(
            zip(requirements, requirement_ids, strict=True)
        ):
            if (
                not isinstance(description, str)
                or not isinstance(requirement_id, str)
                or re.fullmatch(r"REQ_[A-Z0-9_]+", requirement_id) is None
            ):
                continue
            canonical_description = strip_redundant_stable_id_prefix(
                description,
                requirement_id,
            )
            if canonical_description != description:
                requirements[requirement_index] = canonical_description
                changes.append(
                    "removed redundant stable ID prefix from "
                    f"proposal.requirements[{requirement_index}]"
                )


def _compile_atomic_assumption_relations(
    proposal: dict[str, object], changes: list[str]
) -> None:
    """Compile assumption atoms and their explicit decision references."""

    assumptions = proposal.get("assumptions")
    if isinstance(assumptions, list) and (
        not assumptions or any(isinstance(item, dict) for item in assumptions)
    ):
        if not all(isinstance(item, dict) for item in assumptions):
            raise _planning_context_invariant(
                "planning_assumption_atom_shape",
                (
                    "proposal assumptions must be one array of atomic objects; "
                    "string and object entries cannot be mixed"
                ),
                paths=("/proposal/assumptions",),
            )
        compiled_assumptions: list[ProposedAssumption] = []
        for assumption_index, item in enumerate(assumptions):
            try:
                compiled_assumptions.append(ProposedAssumption.model_validate(item))
            except ValidationError as error:
                raise _planning_context_invariant(
                    "planning_assumption_atom_schema",
                    (
                        f"proposal assumption {assumption_index} must contain "
                        "exactly one statement and one stable autonomous decision "
                        f"reference: {_safe_validation_detail(error)}"
                    ),
                    paths=(f"/proposal/assumptions/{assumption_index}",),
                ) from error
        compiled_statements = tuple(item.statement for item in compiled_assumptions)
        if len(compiled_statements) != len(set(compiled_statements)):
            raise _planning_context_invariant(
                "planning_assumption_atom_statement_unique",
                "proposal assumption objects must use unique statements",
                paths=("/proposal/assumptions",),
            )
        proposal["assumptions"] = list(compiled_statements)
        proposal["assumption_decision_ids"] = [
            item.decision_id for item in compiled_assumptions
        ]
        changes.append(
            "compiled atomic proposal.assumptions into canonical statements "
            "and autonomous decision references"
        )


def _compile_product_dimension_sources(
    proposal: dict[str, object],
    *,
    question_answers: Mapping[str, str] | None,
    question_dimension_values: Mapping[str, Mapping[ProductDefinitionDimension, str]]
    | None,
    changes: list[str],
) -> dict[str, object]:
    """Bind answered dimensions to exact user sources and trim inert links."""

    product_definition = proposal.get("product_definition")
    product_dimensions = (
        product_definition if isinstance(product_definition, dict) else {}
    )
    for dimension in ProductDefinitionDimension:
        if dimension is ProductDefinitionDimension.DELIVERY_MATURITY:
            continue
        item = product_dimensions.get(dimension.value)
        if (
            not isinstance(item, dict)
            or item.get("disposition")
            != ProductDefinitionDisposition.RESOLVED_QUESTION.value
            or not isinstance((question_id := item.get("source")), str)
        ):
            continue
        answer = None if question_answers is None else question_answers.get(question_id)
        dimension_values = (
            {}
            if question_dimension_values is None
            else question_dimension_values.get(question_id, {})
        )
        resolved_answer = dimension_values.get(dimension, answer)
        if (
            resolved_answer is None
            or not resolved_answer.strip()
            or len(resolved_answer.strip()) > 1000
            or item.get("statement") == resolved_answer.strip()
        ):
            continue
        item["statement"] = resolved_answer.strip()
        changes.append(
            "compiled proposal.product_definition."
            f"{dimension.value}.statement from exact question answer"
        )
    for dimension in ProductDefinitionDimension:
        if dimension is ProductDefinitionDimension.PRIMARY_WORKFLOW:
            # Core workflow materiality is never normalized away. Preserve the
            # proposed trace so current validation can request an atomic repair.
            continue
        item = product_dimensions.get(dimension.value)
        if (
            not isinstance(item, dict)
            or item.get("disposition")
            != ProductDefinitionDisposition.NOT_MATERIAL.value
        ):
            continue
        removed_references = False
        for field_name in ("requirement_ids", "criterion_ids", "decision_ids"):
            references = item.get(field_name)
            if isinstance(references, list) and references:
                item[field_name] = []
                removed_references = True
        if removed_references:
            changes.append(
                "removed downstream references from not-material "
                f"proposal.product_definition.{dimension.value}"
            )

    return product_dimensions


def _deconflict_profile_criteria(
    proposal: dict[str, object],
    profile_criterion_ids: Collection[str],
    changes: list[str],
) -> None:
    """Keep Controller-owned criteria distinct from model-authored coverage."""

    acceptance_criteria = proposal.get("acceptance_criteria")
    tasks = proposal.get("tasks")
    controller_owned_ids = set(profile_criterion_ids)
    if isinstance(acceptance_criteria, list) and controller_owned_ids:

        def string_list(value: object) -> tuple[str, ...]:
            return (
                tuple(item for item in value if isinstance(item, str))
                if isinstance(value, list)
                else ()
            )

        criterion_ids = [
            criterion.get("id") if isinstance(criterion, dict) else None
            for criterion in acceptance_criteria
        ]
        criterion_id_counts = {
            criterion_id: criterion_ids.count(criterion_id)
            for criterion_id in criterion_ids
            if isinstance(criterion_id, str)
        }
        declared_requirement_ids = set(string_list(proposal.get("requirement_ids")))
        covered_requirement_ids = {
            requirement_id
            for criterion in acceptance_criteria
            if isinstance(criterion, dict)
            and criterion.get("id") not in controller_owned_ids
            for requirement_id in string_list(criterion.get("requirement_ids"))
        }
        uncovered_requirement_ids = declared_requirement_ids - covered_requirement_ids
        deconfliction_candidates = [
            (
                criterion_index,
                set(string_list(criterion.get("requirement_ids"))),
            )
            for criterion_index, criterion in enumerate(acceptance_criteria)
            if isinstance(criterion, dict)
            and isinstance(criterion.get("id"), str)
            and criterion["id"] in controller_owned_ids
            and criterion_id_counts[criterion["id"]] == 1
        ]
        retained_collision_indexes: set[int] = set()
        while uncovered_requirement_ids:
            eligible = [
                (index, requirements)
                for index, requirements in deconfliction_candidates
                if index not in retained_collision_indexes
                and requirements & uncovered_requirement_ids
            ]
            if not eligible:
                break
            selected_index, selected_requirements = min(
                eligible,
                key=lambda item: (
                    -len(item[1] & uncovered_requirement_ids),
                    item[0],
                ),
            )
            retained_collision_indexes.add(selected_index)
            uncovered_requirement_ids -= selected_requirements

        used_criterion_ids = {
            criterion_id
            for criterion_id in criterion_ids
            if isinstance(criterion_id, str)
        } | controller_owned_ids
        renamed_criterion_ids: dict[str, str] = {}
        retained_criteria: list[object] = []
        for criterion_index, criterion in enumerate(acceptance_criteria):
            criterion_id = criterion.get("id") if isinstance(criterion, dict) else None
            if isinstance(criterion_id, str) and criterion_id in controller_owned_ids:
                if criterion_index in retained_collision_indexes:
                    suffix = (
                        criterion_id.removeprefix("AC_")
                        if criterion_id.startswith("AC_")
                        else criterion_id
                    )
                    base_id = f"AC_TASK_{suffix}"
                    replacement_id = base_id
                    counter = 2
                    while replacement_id in used_criterion_ids:
                        replacement_id = f"{base_id}_{counter}"
                        counter += 1
                    assert isinstance(criterion, dict)
                    criterion["id"] = replacement_id
                    used_criterion_ids.add(replacement_id)
                    renamed_criterion_ids[criterion_id] = replacement_id
                    retained_criteria.append(criterion)
                    changes.append(
                        f"deconflicted model criterion {criterion_id} as "
                        f"{replacement_id} from controller-owned profile ID"
                    )
                    continue
                changes.append(
                    "removed controller-owned profile criterion "
                    f"{criterion_id} from proposal.acceptance_criteria"
                    f"[{criterion_index}]"
                )
                continue
            retained_criteria.append(criterion)
        proposal["acceptance_criteria"] = retained_criteria
        if isinstance(tasks, list) and renamed_criterion_ids:
            for task in tasks:
                if not isinstance(task, dict):
                    continue
                bindings = task.get("acceptance_criteria")
                if not isinstance(bindings, list):
                    continue
                expanded_bindings: list[object] = []
                for binding in bindings:
                    replacement = renamed_criterion_ids.get(binding)
                    if replacement is not None:
                        expanded_bindings.append(replacement)
                    expanded_bindings.append(binding)
                deduplicated_bindings: list[object] = []
                for binding in expanded_bindings:
                    if binding not in deduplicated_bindings:
                        deduplicated_bindings.append(binding)
                task["acceptance_criteria"] = deduplicated_bindings


def _canonicalize_proposed_workspace_paths(
    proposal: dict[str, object], changes: list[str]
) -> None:
    """Normalize only safe task and Agent path presentation variants."""

    tasks = proposal.get("tasks")
    agents = proposal.get("agents")
    if isinstance(tasks, list):
        for task_index, task in enumerate(tasks):
            if not isinstance(task, dict):
                continue
            paths = task.get("expected_paths")
            if not isinstance(paths, list):
                continue
            for path_index, value in enumerate(paths):
                canonical = canonicalize_model_path(value)
                if canonical != value:
                    paths[path_index] = canonical
                    changes.append(
                        "canonicalized "
                        f"proposal.tasks[{task_index}].expected_paths[{path_index}]"
                    )
    if isinstance(agents, list):
        for agent_index, agent in enumerate(agents):
            if not isinstance(agent, dict) or "workspace_scope" not in agent:
                continue
            value = agent["workspace_scope"]
            canonical = canonicalize_model_path(value)
            if canonical != value:
                agent["workspace_scope"] = canonical
                changes.append(
                    f"canonicalized proposal.agents[{agent_index}].workspace_scope"
                )


def _normalize_planning_response_payload(
    payload: dict[str, object],
    *,
    profile_criterion_ids: Collection[str] = (),
    user_inputs: Collection[str] = (),
    question_answers: Mapping[str, str] | None = None,
    question_dimension_values: Mapping[
        str,
        Mapping[ProductDefinitionDimension, str],
    ]
    | None = None,
) -> tuple[dict[str, object], tuple[str, ...]]:
    """Apply bounded semantic-preserving normalization before strict validation."""

    profile_criterion_ids = tuple(profile_criterion_ids)
    normalized: dict[str, object] = json.loads(json.dumps(payload))
    changes: list[str] = []
    normalized, envelope_changes = normalize_response_envelope(
        normalized,
        body_types=(
            (PlanningResponseKind.QUESTION.value, PlanningQuestion),
            (PlanningResponseKind.PROPOSAL.value, PlanningProposalBody),
        ),
    )
    changes.extend(envelope_changes)

    question = normalized.get("question")
    if isinstance(question, dict):
        options = question.get("options")
        if isinstance(options, list):
            for index, option in enumerate(options):
                if not isinstance(option, dict):
                    continue
                description = option.get("description")
                if not isinstance(description, str):
                    continue
                cleaned = description.strip()
                if cleaned != description:
                    option["description"] = cleaned
                    changes.append(
                        f"trimmed question.options[{index}].description whitespace"
                    )
                if len(cleaned) > PLANNING_OPTION_DESCRIPTION_MAX_LENGTH:
                    clipped = cleaned[
                        : PLANNING_OPTION_DESCRIPTION_MAX_LENGTH - 1
                    ].rstrip()
                    option["description"] = clipped + "…"
                    changes.append(
                        f"shortened question.options[{index}].description to "
                        f"{PLANNING_OPTION_DESCRIPTION_MAX_LENGTH} characters"
                    )
        try:
            question_category = PlanningDecisionCategory(
                question.get("decision_category")
            )
        except (TypeError, ValueError):
            question_category = None
        if question_category is not None:
            expected_owner = _DECISION_AUTHORITY[question_category]
            if question.get("decision_owner") != expected_owner.value:
                question["decision_owner"] = expected_owner.value
                changes.append(
                    "compiled question.decision_owner from category "
                    f"{question_category.value}"
                )

    proposal = normalized.get("proposal")
    if not isinstance(proposal, dict):
        return normalized, tuple(changes)
    changes.extend(_compile_task_dependency_projection(proposal))
    _compile_atomic_requirement_relations(proposal, changes)
    _compile_atomic_assumption_relations(proposal, changes)

    product_dimensions = _compile_product_dimension_sources(
        proposal,
        question_answers=question_answers,
        question_dimension_values=question_dimension_values,
        changes=changes,
    )
    _normalize_planning_decision_relations(
        proposal, product_dimensions, user_inputs=user_inputs, changes=changes
    )
    _deconflict_profile_criteria(proposal, profile_criterion_ids, changes)
    _canonicalize_proposed_workspace_paths(proposal, changes)
    changes.extend(_compile_unique_specialist_verifier_projection(proposal))
    changes.extend(
        _compile_review_task_scope_projection(
            proposal,
            profile_criterion_ids=profile_criterion_ids,
        )
    )
    return normalized, tuple(changes)


def _compile_unique_specialist_verifier_projection(
    proposal: dict[str, object],
) -> tuple[str, ...]:
    """Project mandatory Review ownership when one specialist uniquely satisfies it."""

    raw_agents = proposal.get("agents")
    raw_criteria = proposal.get("acceptance_criteria")
    raw_definition = proposal.get("product_definition")
    if (
        not isinstance(raw_agents, list)
        or not isinstance(raw_criteria, list)
        or not isinstance(raw_definition, dict)
        or not all(isinstance(item, dict) for item in raw_agents)
        or not all(isinstance(item, dict) for item in raw_criteria)
    ):
        return ()
    try:
        agents = tuple(ProposedAgent.model_validate(item) for item in raw_agents)
        criteria = tuple(
            ProposedCriterion.model_validate(item) for item in raw_criteria
        )
        definition = ProductDefinition.model_validate(raw_definition)
    except (ValidationError, ValueError):
        return ()

    reviewers = tuple(
        agent for agent in agents if agent.capability is AgentCapability.REVIEW
    )
    agents_by_id = {agent.id: agent for agent in agents}
    reviewer_ids = {agent.id for agent in reviewers}
    changes: list[str] = []
    for criterion_index, (raw_criterion, criterion) in enumerate(
        zip(raw_criteria, criteria, strict=True)
    ):
        current_verifiers = raw_criterion.get("verification_agent_ids")
        if (
            not isinstance(current_verifiers, list)
            or not all(isinstance(item, str) for item in current_verifiers)
            or not set(current_verifiers).issubset(agents_by_id)
        ):
            continue
        try:
            required_authority = _specialized_review_authority(
                criterion,
                definition,
            )
        except ValueError:
            continue
        if required_authority is None:
            continue
        matching = tuple(
            agent
            for agent in reviewers
            if specialization_contract(agent.specialization).acceptance_authority
            is required_authority
        )
        if len(matching) != 1:
            continue
        expected_verifiers = [
            agent_id for agent_id in current_verifiers if agent_id not in reviewer_ids
        ]
        expected_verifiers.append(matching[0].id)
        if current_verifiers == expected_verifiers:
            continue
        raw_criterion["verification_agent_ids"] = expected_verifiers
        changes.append(
            "compiled proposal.acceptance_criteria"
            f"[{criterion_index}].verification_agent_ids from the unique "
            f"{required_authority.value} Review authority"
        )
    return tuple(changes)


def _compile_review_task_scope_projection(
    proposal: dict[str, object],
    *,
    profile_criterion_ids: Collection[str] = (),
) -> tuple[str, ...]:
    """Remove known criterion bindings outside each compiled Review scope."""

    raw_agents = proposal.get("agents")
    raw_criteria = proposal.get("acceptance_criteria")
    raw_definition = proposal.get("product_definition")
    raw_tasks = proposal.get("tasks")
    if (
        not isinstance(raw_agents, list)
        or not isinstance(raw_criteria, list)
        or not isinstance(raw_tasks, list)
        or not all(isinstance(item, dict) for item in raw_agents)
        or not all(isinstance(item, dict) for item in raw_criteria)
        or not all(isinstance(item, dict) for item in raw_tasks)
    ):
        return ()
    try:
        agents = tuple(ProposedAgent.model_validate(item) for item in raw_agents)
        criteria = tuple(
            ProposedCriterion.model_validate(item) for item in raw_criteria
        )
        definition = (
            None
            if raw_definition is None
            else ProductDefinition.model_validate(raw_definition)
        )
        tasks = tuple(ProposedTask.model_validate(item) for item in raw_tasks)
    except (ValidationError, ValueError):
        return ()

    scopes, _ = _resolve_review_scope_assignments(
        criteria=criteria,
        definition=definition,
        agents=agents,
        profile_criterion_ids=profile_criterion_ids,
    )
    known_criteria = {criterion.id for criterion in criteria} | set(
        profile_criterion_ids
    )
    changes: list[str] = []
    for task_index, (raw_task, task) in enumerate(zip(raw_tasks, tasks, strict=True)):
        scope = scopes.get(task.owner_agent_id)
        if scope is None:
            continue
        allowed = set(scope)
        projected = [
            criterion_id
            for criterion_id in task.acceptance_criteria
            if criterion_id not in known_criteria or criterion_id in allowed
        ]
        if not projected or projected == list(task.acceptance_criteria):
            continue
        raw_task["acceptance_criteria"] = projected
        changes.append(
            "compiled proposal.tasks"
            f"[{task_index}].acceptance_criteria from {task.owner_agent_id}'s "
            "Review scope"
        )
    return tuple(changes)


def _preserve_tasks_during_writer_coverage_correction(
    payload: dict[str, object],
    plan: SemanticCorrectionPlan,
) -> tuple[dict[str, object], tuple[str, ...]]:
    """Keep task intent and existing bindings when only writer coverage is open."""

    if "/proposal/tasks" not in plan.evidence.target_paths:
        return payload, ()
    model_issues = tuple(
        issue
        for issue in plan.diagnostic.issues
        if issue.authority is ResponseIssueAuthority.MODEL
    )
    if not model_issues or any(
        issue.invariant_id != "planning_writer_criterion_coverage"
        for issue in model_issues
    ):
        return payload, ()
    base_proposal = plan.base_payload.get("proposal")
    corrected_proposal = payload.get("proposal")
    if not isinstance(base_proposal, dict) or not isinstance(corrected_proposal, dict):
        return payload, ()
    base_tasks = base_proposal.get("tasks")
    corrected_tasks = corrected_proposal.get("tasks")
    base_agents = base_proposal.get("agents")
    if (
        not isinstance(base_tasks, list)
        or not isinstance(base_agents, list)
        or not all(isinstance(item, dict) for item in base_tasks)
    ):
        return payload, ()
    if (
        not isinstance(corrected_tasks, list)
        or not all(isinstance(item, dict) for item in corrected_tasks)
        or len(base_tasks) != len(corrected_tasks)
    ):
        raise SemanticCorrectionSubmissionError(
            "writer coverage correction changed the task collection shape",
            plan=plan,
            paths=("/proposal/tasks",),
        )
    try:
        tasks = tuple(ProposedTask.model_validate(item) for item in base_tasks)
        agents = tuple(ProposedAgent.model_validate(item) for item in base_agents)
    except ValidationError:
        return payload, ()
    writer_ids = {
        agent.id
        for agent in agents
        if agent.capability
        in {AgentCapability.IMPLEMENTATION, AgentCapability.INTEGRATION}
    }
    if any(
        item.get("id") != task.id or item.get("owner_agent_id") != task.owner_agent_id
        for item, task in zip(corrected_tasks, tasks, strict=True)
    ):
        raise SemanticCorrectionSubmissionError(
            "writer coverage correction changed task identity or owner",
            plan=plan,
            paths=("/proposal/tasks",),
        )

    projected_tasks = deepcopy(base_tasks)
    for index, task in enumerate(tasks):
        if task.owner_agent_id not in writer_ids:
            continue
        submitted = corrected_tasks[index].get("acceptance_criteria")
        if not isinstance(submitted, list) or not all(
            isinstance(item, str) for item in submitted
        ):
            raise SemanticCorrectionSubmissionError(
                "writer coverage correction requires criterion ID arrays",
                plan=plan,
                paths=("/proposal/tasks",),
            )
        if len(set(submitted)) != len(submitted):
            raise SemanticCorrectionSubmissionError(
                "writer coverage correction repeated a criterion ID",
                plan=plan,
                paths=("/proposal/tasks",),
            )
        projected_tasks[index]["acceptance_criteria"] = list(
            dict.fromkeys((*task.acceptance_criteria, *submitted))
        )
    if projected_tasks == corrected_tasks:
        return payload, ()
    corrected_proposal["tasks"] = projected_tasks
    return payload, (
        "preserved task intent and existing criterion bindings during writer "
        "coverage correction",
    )


def _preserve_agents_during_missing_specialist_correction(
    payload: dict[str, object],
    plan: SemanticCorrectionPlan,
    *,
    profile_criterion_ids: Collection[str] = (),
) -> tuple[dict[str, object], tuple[str, ...]]:
    """Keep already valid Agents while a correction adds missing specialists."""

    agent_path = "/proposal/agents"
    if agent_path not in plan.evidence.target_paths:
        return payload, ()
    agent_issues = tuple(
        issue
        for issue in plan.diagnostic.issues
        if issue.authority is ResponseIssueAuthority.MODEL
        and (
            issue.path == agent_path
            or issue.path.startswith(f"{agent_path}/")
            or agent_path.startswith(f"{issue.path.rstrip('/')}/")
            # An ambiguous general Review owner is reported at the criterion
            # reference, but may still require changing the Agent collection.
            or issue.invariant_id == "planning_criterion_review_scope_ambiguous"
        )
    )
    if not agent_issues or any(
        issue.invariant_id != "planning_criterion_specialist_required"
        for issue in agent_issues
    ):
        return payload, ()
    required_authorities = {
        AcceptanceAuthority(subject.identifier)
        for issue in agent_issues
        for subject in issue.subjects
        if subject.kind is ResponseIssueSubjectKind.CAPABILITY
        and subject.identifier
        in {
            AcceptanceAuthority.SECURITY.value,
            AcceptanceAuthority.USER_EXPERIENCE.value,
        }
    }
    if not required_authorities:
        return payload, ()

    base_proposal = plan.base_payload.get("proposal")
    corrected_proposal = payload.get("proposal")
    if not isinstance(base_proposal, dict) or not isinstance(corrected_proposal, dict):
        return payload, ()
    raw_base_agents = base_proposal.get("agents")
    raw_corrected_agents = corrected_proposal.get("agents")
    raw_criteria = base_proposal.get("acceptance_criteria")
    raw_corrected_criteria = corrected_proposal.get("acceptance_criteria")
    raw_definition = base_proposal.get("product_definition")
    if (
        not isinstance(raw_base_agents, list)
        or not isinstance(raw_corrected_agents, list)
        or not isinstance(raw_criteria, list)
        or not isinstance(raw_corrected_criteria, list)
        or not all(isinstance(item, dict) for item in raw_base_agents)
        or not all(isinstance(item, dict) for item in raw_corrected_agents)
        or not all(isinstance(item, dict) for item in raw_criteria)
        or not all(isinstance(item, dict) for item in raw_corrected_criteria)
    ):
        return payload, ()
    try:
        base_agents = tuple(
            ProposedAgent.model_validate(item) for item in raw_base_agents
        )
        corrected_agents = tuple(
            ProposedAgent.model_validate(item) for item in raw_corrected_agents
        )
        criteria = tuple(
            ProposedCriterion.model_validate(item) for item in raw_criteria
        )
        corrected_criteria = tuple(
            ProposedCriterion.model_validate(item) for item in raw_corrected_criteria
        )
        definition = (
            None
            if raw_definition is None
            else ProductDefinition.model_validate(raw_definition)
        )
    except (ValidationError, ValueError):
        return payload, ()
    if len({agent.id for agent in base_agents}) != len(base_agents) or len(
        {agent.id for agent in corrected_agents}
    ) != len(corrected_agents):
        return payload, ()
    if any(
        sum(
            specialization_contract(agent.specialization).acceptance_authority
            is authority
            for agent in base_agents
            if agent.capability is AgentCapability.REVIEW
        )
        != 0
        for authority in required_authorities
    ):
        # Resolving duplicate matching specialists may require removing or
        # changing an existing Agent, so this monotonic projection is not safe.
        return payload, ()

    review_scopes, _ = _resolve_review_scope_assignments(
        criteria=criteria,
        definition=definition,
        agents=base_agents,
        profile_criterion_ids=profile_criterion_ids,
    )
    preserved_ids = {
        agent.id
        for agent in base_agents
        if agent.capability is not AgentCapability.REVIEW
        or bool(review_scopes.get(agent.id))
    }
    preserved = [
        deepcopy(raw_agent)
        for raw_agent, agent in zip(raw_base_agents, base_agents, strict=True)
        if agent.id in preserved_ids
    ]
    base_by_id = dict(
        zip((agent.id for agent in base_agents), raw_base_agents, strict=True)
    )
    implementation_ids = tuple(
        agent.id
        for agent in base_agents
        if agent.capability
        in {AgentCapability.IMPLEMENTATION, AgentCapability.INTEGRATION}
    )
    corrected_ids = {agent.id for agent in corrected_agents}
    used_ids = set(preserved_ids)
    additions: list[dict[str, object]] = []
    normalizations: list[str] = []

    def specialist_addition(
        raw_agent: dict[str, object],
        agent: ProposedAgent,
        *,
        specialist_id: str,
        authority: AcceptanceAuthority,
    ) -> dict[str, object]:
        addition = deepcopy(raw_agent)
        addition["id"] = specialist_id
        dependencies = list(
            dict.fromkeys(
                dependency
                for dependency in agent.dependencies
                if dependency in preserved_ids and dependency != specialist_id
            )
        )
        dependencies.extend(
            implementation_id
            for implementation_id in implementation_ids
            if implementation_id != specialist_id
            and implementation_id not in dependencies
        )
        if dependencies != list(agent.dependencies):
            addition["dependencies"] = dependencies
            normalizations.append(
                f"compiled added {authority.value} Review Agent dependencies "
                "from existing implementation paths"
            )
        return addition

    for raw_agent, agent in zip(
        raw_corrected_agents,
        corrected_agents,
        strict=True,
    ):
        authority = specialization_contract(agent.specialization).acceptance_authority
        if agent.id not in preserved_ids:
            if (
                agent.capability is not AgentCapability.REVIEW
                or authority not in required_authorities
            ):
                continue
            additions.append(
                specialist_addition(
                    raw_agent,
                    agent,
                    specialist_id=agent.id,
                    authority=authority,
                )
            )
            used_ids.add(agent.id)
            continue
        if raw_agent == base_by_id[agent.id]:
            continue
        if (
            agent.capability is not AgentCapability.REVIEW
            or authority not in required_authorities
        ):
            continue

        # A full-array correction sometimes expresses an addition by changing a
        # retained Agent in place. Preserve that Agent's accepted work and turn
        # the unambiguous specialist definition into a collision-free addition.
        # Prefer the one new ID already used by the affected criterion relations;
        # otherwise allocate a deterministic opaque ID and let the unique-owner
        # projection bind the criteria below.
        referenced_new_ids_list: list[str] = []
        for criterion in corrected_criteria:
            try:
                criterion_authority = _specialized_review_authority(
                    criterion,
                    definition,
                )
            except ValueError:
                continue
            if criterion_authority is not authority:
                continue
            referenced_new_ids_list.extend(
                verifier_id
                for verifier_id in criterion.verification_agent_ids
                if verifier_id not in corrected_ids and verifier_id not in used_ids
            )
        referenced_new_ids = tuple(dict.fromkeys(referenced_new_ids_list))
        preferred_id = (
            referenced_new_ids[0]
            if len(referenced_new_ids) == 1
            else (
                "security_reviewer"
                if authority is AcceptanceAuthority.SECURITY
                else "experience_reviewer"
            )
        )
        specialist_id = preferred_id
        suffix = 2
        while specialist_id in used_ids:
            specialist_id = f"{preferred_id}_{suffix}"
            suffix += 1
        additions.append(
            specialist_addition(
                raw_agent,
                agent,
                specialist_id=specialist_id,
                authority=authority,
            )
        )
        used_ids.add(specialist_id)
        normalizations.append(
            f"deconflicted added {authority.value} Review authority from Agent "
            f"{agent.id} as {specialist_id}"
        )
    merged = [*preserved, *additions]
    if merged == raw_corrected_agents:
        return payload, ()
    normalized = deepcopy(payload)
    normalized_proposal = normalized.get("proposal")
    assert isinstance(normalized_proposal, dict)
    normalized_proposal["agents"] = merged
    normalizations.append(
        "preserved existing valid Agents while adding missing Review authority"
    )
    return normalized, tuple(normalizations)


def _bind_planning_dependency_correction_candidate(
    plan: SemanticCorrectionPlan | None,
) -> SemanticCorrectionPlan | None:
    """Bind an exact repair for a quality dependency relation."""

    if plan is None:
        return None
    issues = tuple(
        issue
        for issue in plan.diagnostic.issues
        if issue.invariant_id == "planning_quality_dependency_coverage"
        and issue.path in plan.evidence.target_paths
    )
    if not issues:
        return plan
    proposal = plan.base_payload.get("proposal")
    raw_agents = None if not isinstance(proposal, dict) else proposal.get("agents")
    if not isinstance(raw_agents, list):
        return None
    try:
        agents = tuple(ProposedAgent.model_validate(item) for item in raw_agents)
    except ValidationError:
        return None
    agent_ids = tuple(agent.id for agent in agents)
    if len(agent_ids) != len(set(agent_ids)):
        return None
    dependencies = {agent.id: agent.dependencies for agent in agents}
    known_ids = set(agent_ids)
    if any(not set(values).issubset(known_ids) for values in dependencies.values()):
        return None
    implementation_ids = {
        agent.id
        for agent in agents
        if agent.capability
        in {AgentCapability.IMPLEMENTATION, AgentCapability.INTEGRATION}
    }

    def transitively_depends(
        agent_id: str,
        target: str,
        graph: dict[str, tuple[str, ...]] | None = None,
    ) -> bool:
        active_graph = dependencies if graph is None else graph
        pending = list(active_graph[agent_id])
        seen: set[str] = set()
        while pending:
            current = pending.pop()
            if current == target:
                return True
            if current not in seen:
                seen.add(current)
                pending.extend(active_graph[current])
        return False

    slots: list[SemanticCorrectionCandidateSlot] = []
    observed_subjects: list[frozenset[str]] = []
    expected_subjects: list[frozenset[str]] = []
    reversed_edge: tuple[str, str] | None = None
    for issue in sorted(issues, key=lambda item: item.path):
        match = re.fullmatch(r"/proposal/agents/([0-9]+)/dependencies", issue.path)
        if match is None:
            return None
        index = int(match.group(1))
        if index >= len(agents):
            return None
        quality_agent = agents[index]
        if quality_agent.capability not in {
            AgentCapability.TESTING,
            AgentCapability.REVIEW,
        }:
            return None
        missing = tuple(
            sorted(
                implementation_id
                for implementation_id in implementation_ids
                if not transitively_depends(quality_agent.id, implementation_id)
            )
        )
        if any(
            subject.kind is not ResponseIssueSubjectKind.AGENT
            for subject in issue.subjects
        ):
            return None
        issue_agent_ids = frozenset(
            subject.identifier
            for subject in issue.subjects
            if subject.kind is ResponseIssueSubjectKind.AGENT
        )
        if not missing:
            return None
        observed_subjects.append(issue_agent_ids)
        expected_subjects.append(frozenset((quality_agent.id, *missing)))
        for implementation_id in missing:
            if not transitively_depends(implementation_id, quality_agent.id):
                continue
            # A direct reversed edge has one exact, atomic repair: remove it
            # from the writer and add the required quality dependency. Keep
            # indirect or multiple reversals fail-closed.
            if (
                reversed_edge is not None
                or quality_agent.id not in dependencies[implementation_id]
            ):
                return None
            reduced_graph = {
                **dependencies,
                implementation_id: tuple(
                    item
                    for item in dependencies[implementation_id]
                    if item != quality_agent.id
                ),
            }
            if transitively_depends(
                implementation_id,
                quality_agent.id,
                reduced_graph,
            ):
                return None
            reversed_edge = (quality_agent.id, implementation_id)
        replacement = list(dict.fromkeys((*quality_agent.dependencies, *missing)))
        slots.append(
            SemanticCorrectionCandidateSlot(
                target_path=issue.path,
                candidates=(
                    SemanticCorrectionCandidate(
                        handle="candidate_1",
                        replacement_value=replacement,
                        source=(
                            "controller-required additive dependency closure for "
                            f"{quality_agent.id}"
                        ),
                    ),
                ),
            )
        )
    combined_subjects = frozenset().union(*expected_subjects)
    if not (
        observed_subjects == expected_subjects
        or all(subjects == combined_subjects for subjects in observed_subjects)
    ):
        # A multi-path invariant projects its complete subject set to every
        # sibling diagnostic. Accept only that exact union or exact leaf sets.
        return None

    if reversed_edge is None:
        return attach_semantic_correction_candidates(plan, tuple(slots))

    quality_id, implementation_id = reversed_edge
    writer_path = f"/proposal/agents/{agent_ids.index(implementation_id)}/dependencies"
    raw_tasks = proposal.get("tasks")
    if not isinstance(raw_tasks, list):
        return None
    try:
        tasks = tuple(ProposedTask.model_validate(item) for item in raw_tasks)
    except ValidationError:
        return None
    task_ids = tuple(task.id for task in tasks)
    if len(task_ids) != len(set(task_ids)):
        return None
    quality_task_ids = {task.id for task in tasks if task.owner_agent_id == quality_id}
    reversed_task_edges = tuple(
        (index, task)
        for index, task in enumerate(tasks)
        if task.owner_agent_id == implementation_id
        and quality_task_ids.intersection(task.dependencies)
    )
    task_paths = tuple(
        f"/proposal/tasks/{index}/dependencies" for index, _ in reversed_task_edges
    )
    target_paths = tuple(
        sorted({*plan.evidence.target_paths, writer_path, *task_paths})
    )
    if (
        len(target_paths) > MAX_CORRECTION_FIELDS
        or len(plan.diagnostic.issues) + 1 + len(task_paths) > MAX_CORRECTION_FIELDS
    ):
        return None
    relation_issues = [
        ResponseValidationIssue(
            path=writer_path,
            code="planning_quality_dependency_order",
            invariant_id="planning_quality_dependency_order",
            message=(
                "the implementation Agent must stop depending on the "
                "quality Agent before the required reverse edge is added"
            ),
            authority=ResponseIssueAuthority.MODEL,
            subjects=_planning_subjects(
                (ResponseIssueSubjectKind.AGENT, quality_id),
                (ResponseIssueSubjectKind.AGENT, implementation_id),
            ),
        ),
        *(
            ResponseValidationIssue(
                path=f"/proposal/tasks/{index}/dependencies",
                code="planning_quality_task_dependency_order",
                invariant_id="planning_quality_task_dependency_order",
                message=(
                    "the implementation task must stop depending on a "
                    "task owned by its downstream quality Agent"
                ),
                authority=ResponseIssueAuthority.MODEL,
                subjects=_planning_subjects(
                    (ResponseIssueSubjectKind.TASK, task.id),
                    (ResponseIssueSubjectKind.AGENT, quality_id),
                    (ResponseIssueSubjectKind.AGENT, implementation_id),
                ),
            )
            for index, task in reversed_task_edges
        ),
    ]
    diagnostic = ResponseValidationDiagnostic.model_validate(
        {
            **plan.diagnostic.model_dump(mode="json"),
            "issues": [
                *(issue.model_dump(mode="json") for issue in plan.diagnostic.issues),
                *(issue.model_dump(mode="json") for issue in relation_issues),
            ],
            "correction_paths": target_paths,
        }
    )
    expanded = build_semantic_correction_plan(plan.base_payload, diagnostic)
    if expanded is None:
        return None
    writer_dependencies = [
        item for item in dependencies[implementation_id] if item != quality_id
    ]
    slots.append(
        SemanticCorrectionCandidateSlot(
            target_path=writer_path,
            candidates=(
                SemanticCorrectionCandidate(
                    handle="candidate_1",
                    replacement_value=writer_dependencies,
                    source=(
                        "controller-required removal of the reversed quality "
                        f"dependency from {implementation_id}"
                    ),
                ),
            ),
        )
    )
    slots.extend(
        SemanticCorrectionCandidateSlot(
            target_path=f"/proposal/tasks/{index}/dependencies",
            candidates=(
                SemanticCorrectionCandidate(
                    handle="candidate_1",
                    replacement_value=[
                        item
                        for item in task.dependencies
                        if item not in quality_task_ids
                    ],
                    source=(
                        "controller-required removal of the reversed quality "
                        f"task dependency from {task.id}"
                    ),
                ),
            ),
        )
        for index, task in reversed_task_edges
    )
    slots_by_path = {slot.target_path: slot for slot in slots}
    ordered_slots = tuple(slots_by_path[path] for path in target_paths)
    return attach_semantic_correction_candidates(
        require_all_semantic_correction_targets(expanded),
        ordered_slots,
    )


def _planning_product_recommendation_decision_candidates(
    plan: SemanticCorrectionPlan,
    *,
    target_path: str,
) -> tuple[SemanticCorrectionCandidate, ...] | None:
    """Compile exact decision-link repairs for one recommended product dimension."""

    match = re.fullmatch(
        r"/proposal/product_definition/([a-z_]+)",
        target_path,
    )
    if match is None:
        return None
    try:
        dimension = ProductDefinitionDimension(match.group(1))
    except ValueError:
        return None
    issues = tuple(
        issue for issue in plan.diagnostic.issues if issue.path == target_path
    )
    if (
        len(issues) != 1
        or issues[0].authority is not ResponseIssueAuthority.MODEL
        or issues[0].invariant_id
        not in {
            "planning_product_recommendation_authority",
            "planning_product_recommendation_category",
        }
    ):
        return None
    proposal = plan.base_payload.get("proposal")
    definition = (
        None
        if not isinstance(proposal, Mapping)
        else proposal.get("product_definition")
    )
    raw_item = (
        None if not isinstance(definition, Mapping) else definition.get(dimension.value)
    )
    decisions = None if not isinstance(proposal, Mapping) else proposal.get("decisions")
    if not isinstance(raw_item, dict) or not isinstance(decisions, list):
        return None
    try:
        item = ProductDefinitionStatement.model_validate(raw_item)
        parsed_decisions = tuple(
            PlanningDecisionRecord.model_validate(value) for value in decisions
        )
    except ValidationError:
        return None
    if item.disposition is not ProductDefinitionDisposition.PLANNER_RECOMMENDATION:
        return None
    expected_category = _PRODUCT_DIMENSION_DECISION_CATEGORY[dimension]
    eligible_ids = tuple(
        decision.id
        for decision in parsed_decisions
        if decision.authority is PlanningDecisionAuthority.PLANNER_PROPOSAL
        and decision.category is expected_category
    )
    if not eligible_ids:
        return ()
    retained_ids = tuple(
        decision_id for decision_id in item.decision_ids if decision_id in eligible_ids
    )
    candidate_id_sets = (
        (retained_ids,)
        if retained_ids
        else tuple((decision_id,) for decision_id in eligible_ids)
    )
    candidates: list[SemanticCorrectionCandidate] = []
    for decision_ids in candidate_id_sets:
        replacement = deepcopy(raw_item)
        replacement["decision_ids"] = list(decision_ids)
        candidates.append(
            SemanticCorrectionCandidate(
                handle=f"candidate_{len(candidates) + 1}",
                replacement_value=replacement,
                source=(
                    "controller-validated "
                    f"{expected_category.value} Planner decision link"
                ),
            )
        )
    return tuple(candidates)


def _bind_planning_product_recommendation_correction_candidates(
    plan: SemanticCorrectionPlan | None,
) -> SemanticCorrectionPlan | None:
    """Bind category-compatible product recommendation repairs to short handles."""

    if plan is None:
        return None
    slots_by_path = {slot.target_path: slot for slot in plan.candidate_slots}
    for target_path in plan.evidence.target_paths:
        if target_path in slots_by_path:
            continue
        candidates = _planning_product_recommendation_decision_candidates(
            plan,
            target_path=target_path,
        )
        if candidates is None:
            continue
        if not candidates:
            return None
        slots_by_path[target_path] = SemanticCorrectionCandidateSlot(
            target_path=target_path,
            candidates=candidates,
        )
    if len(slots_by_path) == len(plan.candidate_slots):
        return plan
    return attach_semantic_correction_candidates(
        plan,
        tuple(
            slots_by_path[path]
            for path in plan.evidence.target_paths
            if path in slots_by_path
        ),
    )


_CURRENT_DECISION_WIRE_FIELDS = (
    "id",
    "category",
    "provenance",
    "summary",
    "rationale",
)


def _planning_decision_reference_categories(
    proposal: Mapping[str, object],
    *,
    decision_id: str,
) -> tuple[PlanningDecisionCategory, ...] | None:
    """Return categories already constrained by stable decision references."""

    constrained: set[PlanningDecisionCategory] | None = None

    def intersect(values: Collection[PlanningDecisionCategory]) -> None:
        nonlocal constrained
        candidate = set(values)
        constrained = candidate if constrained is None else constrained & candidate

    product_definition = proposal.get("product_definition")
    if isinstance(product_definition, Mapping):
        for dimension in ProductDefinitionDimension:
            item = product_definition.get(dimension.value)
            if not isinstance(item, Mapping):
                continue
            references = item.get("decision_ids")
            if not isinstance(references, list) or decision_id not in references:
                continue
            disposition = item.get("disposition")
            if disposition == ProductDefinitionDisposition.RESOLVED_QUESTION.value:
                intersect((PlanningDecisionCategory.PRODUCT_REQUIREMENT,))
            elif (
                disposition == ProductDefinitionDisposition.PLANNER_RECOMMENDATION.value
            ):
                intersect((_PRODUCT_DIMENSION_DECISION_CATEGORY[dimension],))

    assumption_decision_ids = proposal.get("assumption_decision_ids")
    if (
        isinstance(assumption_decision_ids, list)
        and decision_id in assumption_decision_ids
    ):
        intersect(
            tuple(
                category
                for category, authority in _DECISION_AUTHORITY.items()
                if authority is PlanningDecisionAuthority.AGENT_AUTONOMY
            )
        )

    if constrained is None:
        return None
    return tuple(
        category for category in PlanningDecisionCategory if category in constrained
    )


def _planning_decision_authority_candidates(
    plan: SemanticCorrectionPlan,
    *,
    target_path: str,
) -> tuple[SemanticCorrectionCandidate, ...] | None:
    """Compile valid atomic repairs for one category/provenance mismatch."""

    match = re.fullmatch(r"/proposal/decisions/([0-9]+)", target_path)
    if match is None:
        return None
    proposal = plan.base_payload.get("proposal")
    decisions = None if not isinstance(proposal, Mapping) else proposal.get("decisions")
    index = int(match.group(1))
    if not isinstance(decisions, list) or index >= len(decisions):
        return None
    raw_decision = decisions[index]
    if not isinstance(raw_decision, dict):
        return None
    allowed_fields = {*_CURRENT_DECISION_WIRE_FIELDS, "authority"}
    if set(raw_decision) != allowed_fields:
        return None
    try:
        category = PlanningDecisionCategory(raw_decision["category"])
        provenance = PlanningDecisionProvenance.model_validate(
            raw_decision["provenance"]
        )
    except (KeyError, TypeError, ValueError, ValidationError):
        return None
    category_authority = _DECISION_AUTHORITY[category]
    provenance_authority = _DECISION_PROVENANCE_AUTHORITY[provenance.kind]
    if (
        category_authority is provenance_authority
        or raw_decision.get("authority") != category_authority.value
    ):
        return None
    issues = tuple(
        issue for issue in plan.diagnostic.issues if issue.path == target_path
    )
    if (
        len(issues) != 1
        or issues[0].authority is not ResponseIssueAuthority.MODEL
        or issues[0].code != "value_error"
        or issues[0].invariant_id != "planning_decision_authority_provenance"
    ):
        return None

    constrained = _planning_decision_reference_categories(
        proposal,
        decision_id=str(raw_decision["id"]),
    )
    if constrained is None:
        compatible_with_existing = tuple(
            candidate
            for candidate, authority in _DECISION_AUTHORITY.items()
            if authority is provenance_authority
        )
        candidate_categories = tuple(
            dict.fromkeys((category, *compatible_with_existing))
        )
    else:
        candidate_categories = tuple(
            candidate
            for candidate in (category, *constrained)
            if candidate in constrained
        )
    if not candidate_categories:
        return ()

    existing_provenance = deepcopy(raw_decision["provenance"])
    canonical_provenance = {
        PlanningDecisionAuthority.PLANNER_PROPOSAL: {
            "kind": PlanningDecisionProvenanceKind.PLANNER_RECOMMENDATION.value,
            "source": "planner",
        },
        PlanningDecisionAuthority.AGENT_AUTONOMY: {
            "kind": PlanningDecisionProvenanceKind.AGENT_AUTONOMY.value,
            "source": "agent",
        },
    }
    candidates: list[SemanticCorrectionCandidate] = []
    seen_values: set[str] = set()
    for candidate_category in candidate_categories:
        candidate_authority = _DECISION_AUTHORITY[candidate_category]
        provenance_values: list[object] = []
        if candidate_authority is provenance_authority:
            provenance_values.append(existing_provenance)
        if candidate_authority in canonical_provenance:
            provenance_values.append(canonical_provenance[candidate_authority])
        for provenance_value in provenance_values:
            wire_value = {
                field: deepcopy(raw_decision[field])
                for field in _CURRENT_DECISION_WIRE_FIELDS
            }
            wire_value["category"] = candidate_category.value
            wire_value["provenance"] = deepcopy(provenance_value)
            persisted_value = {
                **wire_value,
                "authority": candidate_authority.value,
            }
            try:
                PlanningDecisionRecord.model_validate(persisted_value)
            except ValidationError:
                continue
            identity = json.dumps(
                wire_value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if identity in seen_values:
                continue
            seen_values.add(identity)
            if (
                candidate_category is category
                and provenance_value != existing_provenance
            ):
                source = (
                    f"preserve {category.value} category with canonical "
                    f"{wire_value['provenance']['kind']} provenance"
                )
            elif provenance_value == existing_provenance:
                source = (
                    f"preserve {provenance.kind.value} provenance under "
                    f"{candidate_category.value} category"
                )
            else:
                source = (
                    f"use reference-bound {candidate_category.value} category "
                    f"with canonical {wire_value['provenance']['kind']} provenance"
                )
            candidates.append(
                SemanticCorrectionCandidate(
                    handle=f"candidate_{len(candidates) + 1}",
                    replacement_value=wire_value,
                    source=source,
                )
            )
    return tuple(candidates)


def _bind_planning_decision_authority_correction_candidates(
    plan: SemanticCorrectionPlan | None,
) -> SemanticCorrectionPlan | None:
    """Bind atomic decision repairs without reopening valid record fields."""

    if plan is None:
        return None
    slots_by_path = {slot.target_path: slot for slot in plan.candidate_slots}
    for target_path in plan.evidence.target_paths:
        if target_path in slots_by_path:
            continue
        candidates = _planning_decision_authority_candidates(
            plan,
            target_path=target_path,
        )
        if candidates is None:
            continue
        if not candidates:
            return None
        slots_by_path[target_path] = SemanticCorrectionCandidateSlot(
            target_path=target_path,
            candidates=candidates,
        )
    if len(slots_by_path) == len(plan.candidate_slots):
        return plan
    ordered_slots = tuple(
        slots_by_path[path]
        for path in plan.evidence.target_paths
        if path in slots_by_path
    )
    return attach_semantic_correction_candidates(plan, ordered_slots)


def _bind_planning_correction_candidates(
    plan: SemanticCorrectionPlan | None,
) -> SemanticCorrectionPlan | None:
    """Attach every safe Controller-owned Planning correction vocabulary."""

    return _bind_planning_decision_authority_correction_candidates(
        _bind_planning_product_recommendation_correction_candidates(
            _bind_planning_dependency_correction_candidate(
                _expand_planning_acceptance_relation_correction(
                    _expand_planning_identity_relation_correction(
                        _collapse_wide_planning_schema_correction(plan)
                    )
                )
            )
        )
    )


def _expand_planning_identity_relation_correction(
    plan: SemanticCorrectionPlan | None,
) -> SemanticCorrectionPlan | None:
    """Keep decision and Agent identities consistent with their live references."""

    if (
        plan is None
        or "/proposal" in plan.evidence.target_paths
        or any(
            issue.authority is not ResponseIssueAuthority.MODEL
            for issue in plan.diagnostic.issues
        )
    ):
        return plan
    proposal = plan.base_payload.get("proposal")
    if not isinstance(proposal, dict):
        return plan

    target_paths = set(plan.evidence.target_paths)
    relation_paths: dict[str, str] = {}
    decisions = proposal.get("decisions")
    duplicate_question_sources: set[str] = set()
    if isinstance(decisions, list):
        question_sources: set[str] = set()
        for decision in decisions:
            provenance = (
                decision.get("provenance") if isinstance(decision, dict) else None
            )
            if (
                not isinstance(provenance, dict)
                or provenance.get("kind") != "resolved_question"
                or not isinstance(provenance.get("source"), str)
            ):
                continue
            source = provenance["source"]
            if source in question_sources:
                duplicate_question_sources.add(source)
            question_sources.add(source)

    decision_collection_targeted = "/proposal/decisions" in target_paths
    if (
        not decision_collection_targeted
        and duplicate_question_sources
        and plan.diagnostic.failure_class is ResponseFailureClass.SEMANTIC_SCHEMA
        and any(
            issue.invariant_id == "planning_decision_authority_provenance"
            for issue in plan.diagnostic.issues
        )
    ):
        target_paths = {
            path for path in target_paths if not path.startswith("/proposal/decisions/")
        }
        target_paths.add("/proposal/decisions")
        relation_paths["/proposal/decisions"] = (
            "one answered question must produce one user-owned decision; "
            "repair duplicate decisions and their references atomically"
        )
        decision_collection_targeted = True

    if decision_collection_targeted and duplicate_question_sources:
        definition = proposal.get("product_definition")
        if isinstance(definition, dict):
            for dimension in ProductDefinitionDimension:
                item = definition.get(dimension.value)
                if isinstance(item, dict) and item.get("decision_ids"):
                    relation_paths[
                        f"/proposal/product_definition/{dimension.value}/decision_ids"
                    ] = "decision references must match the replacement decision set"
        if proposal.get("assumptions"):
            relation_paths["/proposal/assumptions"] = (
                "assumptions must cite decisions in the replacement decision set"
            )

    if "/proposal/agents" in target_paths and any(
        issue.invariant_id
        in {
            "planning_criterion_review_scope_ambiguous",
            "planning_profile_review_scope_ambiguous",
        }
        for issue in plan.diagnostic.issues
    ):
        tasks = proposal.get("tasks")
        if isinstance(tasks, list):
            for index, task in enumerate(tasks):
                if isinstance(task, dict) and task.get("owner_agent_id"):
                    relation_paths[f"/proposal/tasks/{index}/owner_agent_id"] = (
                        "task owners must match the replacement Agent set"
                    )
        criteria = proposal.get("acceptance_criteria")
        if isinstance(criteria, list):
            for index, criterion in enumerate(criteria):
                if isinstance(criterion, dict) and criterion.get(
                    "verification_agent_ids"
                ):
                    relation_paths[
                        f"/proposal/acceptance_criteria/{index}/verification_agent_ids"
                    ] = "Review verifiers must match the replacement Agent set"

    if not relation_paths:
        return plan
    paths = tuple(sorted({*target_paths, *relation_paths}))
    minimal_paths = tuple(
        path
        for path in paths
        if not any(
            path.startswith(f"{other.rstrip('/')}/") for other in paths if other != path
        )
    )
    new_issues = tuple(
        ResponseValidationIssue(
            path=path,
            code="planning_identity_relation_atomicity",
            invariant_id="planning_identity_relation_atomicity",
            message=message,
            authority=ResponseIssueAuthority.MODEL,
        )
        for path, message in relation_paths.items()
        if path in minimal_paths
        and (
            (path == "/proposal/decisions" and bool(duplicate_question_sources))
            or not any(
                path == issue.path
                or path.startswith(f"{issue.path.rstrip('/')}/")
                or issue.path.startswith(f"{path.rstrip('/')}/")
                for issue in plan.diagnostic.issues
                if issue.authority is ResponseIssueAuthority.MODEL
            )
        )
    )
    if (
        len(minimal_paths) > MAX_CORRECTION_FIELDS
        or len(plan.diagnostic.issues) + len(new_issues) > MAX_CORRECTION_FIELDS
    ):
        correction_paths = ("/proposal",)
        issues = plan.diagnostic.issues
    else:
        correction_paths = minimal_paths
        issues = (*plan.diagnostic.issues, *new_issues)
    diagnostic = ResponseValidationDiagnostic.model_validate(
        {
            **plan.diagnostic.model_dump(mode="json"),
            "issues": [issue.model_dump(mode="json") for issue in issues],
            "correction_paths": correction_paths,
        }
    )
    expanded = build_semantic_correction_plan(plan.base_payload, diagnostic)
    return (
        None if expanded is None else require_all_semantic_correction_targets(expanded)
    )


def _minimal_planning_correction_paths(paths: Collection[str]) -> tuple[str, ...]:
    """Keep the narrowest disjoint set that still covers every authorized path."""

    unique = set(paths)
    return tuple(
        sorted(
            path
            for path in unique
            if not any(
                path.startswith(f"{other.rstrip('/')}/")
                for other in unique
                if other != path
            )
        )
    )


def _expand_planning_acceptance_relation_correction(
    plan: SemanticCorrectionPlan | None,
) -> SemanticCorrectionPlan | None:
    """Replace criterion identities and every existing reference atomically.

    A whole ``acceptance_criteria`` replacement may add, split, rename, or remove
    criterion records. Product-definition links and task coverage are then part
    of the same model-owned graph edit: preserving their old IDs creates an
    invalid intermediate graph and can make successive corrections oscillate
    between the collection and its references.
    """

    criterion_path = "/proposal/acceptance_criteria"
    if (
        plan is None
        or plan.diagnostic.failure_class is not ResponseFailureClass.SEMANTIC_CONTEXT
        or criterion_path not in plan.evidence.target_paths
    ):
        return plan
    proposal = plan.base_payload.get("proposal")
    if not isinstance(proposal, dict):
        return plan

    related_paths: list[str] = []
    product_definition = proposal.get("product_definition")
    if isinstance(product_definition, dict):
        for dimension in ProductDefinitionDimension:
            item = product_definition.get(dimension.value)
            criterion_ids = (
                item.get("criterion_ids") if isinstance(item, dict) else None
            )
            if isinstance(criterion_ids, list) and criterion_ids:
                related_paths.append(
                    f"/proposal/product_definition/{dimension.value}/criterion_ids"
                )

    tasks = proposal.get("tasks")
    if isinstance(tasks, list):
        related_paths.extend(
            f"/proposal/tasks/{index}/acceptance_criteria"
            for index, task in enumerate(tasks)
            if isinstance(task, dict)
            and isinstance(task.get("acceptance_criteria"), list)
            and task["acceptance_criteria"]
        )
    if not related_paths:
        return plan

    correction_paths = _minimal_planning_correction_paths(
        (*plan.evidence.target_paths, *related_paths)
    )
    uncovered_paths = tuple(
        path
        for path in related_paths
        if not any(
            path == issue.path
            or path.startswith(f"{issue.path.rstrip('/')}/")
            or issue.path.startswith(f"{path.rstrip('/')}/")
            for issue in plan.diagnostic.issues
            if issue.authority is ResponseIssueAuthority.MODEL
        )
    )
    if (
        len(correction_paths) > MAX_CORRECTION_FIELDS
        or len(plan.diagnostic.issues) + len(uncovered_paths) > MAX_CORRECTION_FIELDS
    ):
        diagnostic = ResponseValidationDiagnostic.model_validate(
            {
                **plan.diagnostic.model_dump(mode="json"),
                "correction_paths": ["/proposal"],
            }
        )
    else:
        relation_issues = tuple(
            ResponseValidationIssue(
                path=path,
                code="planning_acceptance_relation_atomicity",
                invariant_id="planning_acceptance_relation_atomicity",
                message=(
                    "existing criterion references must change atomically with "
                    "the acceptance-criteria collection"
                ),
                authority=ResponseIssueAuthority.MODEL,
            )
            for path in uncovered_paths
        )
        diagnostic = ResponseValidationDiagnostic.model_validate(
            {
                **plan.diagnostic.model_dump(mode="json"),
                "issues": [
                    *(item.model_dump(mode="json") for item in plan.diagnostic.issues),
                    *(item.model_dump(mode="json") for item in relation_issues),
                ],
                "correction_paths": correction_paths,
            }
        )
    expanded = build_semantic_correction_plan(plan.base_payload, diagnostic)
    if expanded is None:
        return None
    return require_all_semantic_correction_targets(expanded)


_MAX_PRECISE_PLANNING_CORRECTION_TARGETS = 8
_PLANNING_IDENTITY_COLLECTIONS = frozenset(
    {"requirements", "acceptance_criteria", "decisions", "tasks", "agents"}
)


def _planning_correction_lost_record_identity(
    plan: SemanticCorrectionPlan,
) -> bool:
    """Return whether an authorized record slot has no identity to preserve.

    A whole-record schema failure means the untrusted value is not a record at
    all. Repairing that value independently cannot safely reconstruct references
    carried by sibling proposal fields, so the proposal graph must be replaced
    as one bounded unit.
    """

    proposal = plan.base_payload.get("proposal")
    if not isinstance(proposal, dict):
        return False
    for target_path in plan.evidence.target_paths:
        match = re.fullmatch(
            r"/proposal/([a-z_]+)/([0-9]+)",
            target_path,
        )
        if match is None or match.group(1) not in _PLANNING_IDENTITY_COLLECTIONS:
            continue
        records = proposal.get(match.group(1))
        index = int(match.group(2))
        if not isinstance(records, list) or index >= len(records):
            return True
        record = records[index]
        if not isinstance(record, dict) or not isinstance(record.get("id"), str):
            return True
    return False


def _collapse_wide_planning_schema_correction(
    plan: SemanticCorrectionPlan | None,
) -> SemanticCorrectionPlan | None:
    """Replace an unusable proposal as one typed unit instead of unsafe slots."""

    if (
        plan is None
        or plan.diagnostic.failure_class is not ResponseFailureClass.SEMANTIC_SCHEMA
        or plan.base_payload.get("kind") != PlanningResponseKind.PROPOSAL.value
        or not isinstance(plan.base_payload.get("proposal"), dict)
        or any(
            issue.authority is not ResponseIssueAuthority.MODEL
            or not (issue.path == "/proposal" or issue.path.startswith("/proposal/"))
            for issue in plan.diagnostic.issues
        )
    ):
        return plan
    if (
        len(plan.evidence.target_paths) <= _MAX_PRECISE_PLANNING_CORRECTION_TARGETS
        and not _planning_correction_lost_record_identity(plan)
    ):
        return plan
    diagnostic = ResponseValidationDiagnostic.model_validate(
        {
            **plan.diagnostic.model_dump(mode="json"),
            "correction_paths": ["/proposal"],
        }
    )
    return build_semantic_correction_plan(plan.base_payload, diagnostic)


def _requires_full_proposal_regeneration(
    plan: SemanticCorrectionPlan,
) -> bool:
    """A whole-proposal schema error cannot benefit from a targeted slot edit."""

    return (
        plan.diagnostic.failure_class is ResponseFailureClass.SEMANTIC_SCHEMA
        and plan.base_payload.get("kind") == PlanningResponseKind.PROPOSAL.value
        and plan.evidence.target_paths == ("/proposal",)
    )


def _digest_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _safe_validation_detail(error: BaseException | str) -> str:
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


def _planning_invariant_diagnostic(
    payload: dict[str, object],
    invariant: _PlanningInvariant,
    *,
    authority: ResponseIssueAuthority | None = None,
    failure_class: ResponseFailureClass | None = None,
) -> ResponseValidationDiagnostic:
    """Compile validator-owned identity without parsing human error text."""

    return diagnostic_from_invariant(
        payload,
        failure_class=(
            invariant.failure_class if failure_class is None else failure_class
        ),
        authority=invariant.authority if authority is None else authority,
        code="planning_context",
        invariant_id=invariant.invariant_id,
        subjects=invariant.subjects,
        message=invariant.message,
        paths=invariant.paths,
    )


def _planning_invariants_diagnostic(
    payload: dict[str, object],
    invariants: tuple[_PlanningInvariant, ...],
) -> ResponseValidationDiagnostic:
    """Compile independent atomic invariants into one correction request."""

    diagnostics = tuple(
        _planning_invariant_diagnostic(payload, invariant) for invariant in invariants
    )
    needs_user_decision = any(
        issue.authority is ResponseIssueAuthority.USER
        for diagnostic in diagnostics
        for issue in diagnostic.issues
    )
    issues = tuple(issue for diagnostic in diagnostics for issue in diagnostic.issues)
    correction_paths = (
        ()
        if needs_user_decision
        else _minimal_planning_correction_paths(
            path for diagnostic in diagnostics for path in diagnostic.correction_paths
        )
    )
    if (
        len(issues) > MAX_CORRECTION_FIELDS
        or len(correction_paths) > MAX_CORRECTION_FIELDS
    ):
        return diagnostic_from_invariant(
            payload,
            failure_class=ResponseFailureClass.SEMANTIC_CONTEXT,
            authority=ResponseIssueAuthority.CONTROLLER,
            code="planning_context_unclassified",
            invariant_id="planning_validation_issue_overflow",
            subjects=(),
            message="Planning has too many independent failures for bounded correction",
            paths=("/",),
        )
    return ResponseValidationDiagnostic(
        failure_class=(
            ResponseFailureClass.MISSING_USER_DECISION
            if needs_user_decision
            else ResponseFailureClass.SEMANTIC_CONTEXT
        ),
        response_sha256=canonical_json_sha256(payload),
        issues=issues,
        correction_paths=correction_paths,
    )


def _planning_correction_reasons(
    diagnostic: ResponseValidationDiagnostic,
) -> tuple[str, ...]:
    """Expose bounded validator-owned reason labels, never model text or IDs."""

    known = {
        "planning_product_explicit_provenance": "product source",
        "planning_product_explicit_statement": "product quote",
        "planning_product_workflow_required": "primary workflow",
        "planning_product_question_source": "answered-question link",
        "planning_product_recommendation_source": "recommendation link",
        "planning_recommendation_provenance": "decision provenance",
        "planning_decision_authority_provenance": "decision authority",
        "planning_criterion_review_boundaries": "Review boundaries",
        "planning_criterion_review_authority_conflict": "Review authority",
        "planning_criterion_specialist_required": "specialist Reviewer",
        "planning_review_task_scope": "Reviewer task scope",
        "planning_writer_criterion_coverage": "writer task coverage",
        "planning_requirement_acceptance_coverage": "requirement coverage",
        "planning_quality_dependency_coverage": "quality Agent dependencies",
    }

    def label(issue: ResponseValidationIssue) -> str:
        if issue.invariant_id in known:
            return known[issue.invariant_id]
        path = issue.path
        for prefix, reason in (
            ("/proposal/product_definition", "product definition"),
            ("/proposal/acceptance_criteria", "acceptance criteria"),
            ("/proposal/requirements", "requirements"),
            ("/proposal/decisions", "decision records"),
            ("/proposal/agents", "Agent assignments"),
            ("/proposal/tasks", "task assignments"),
        ):
            if path == prefix or path.startswith(f"{prefix}/"):
                return reason
        return "response format"

    reasons = tuple(dict.fromkeys(label(issue) for issue in diagnostic.issues))
    if len(reasons) <= 3:
        return reasons
    return (*reasons[:3], f"{len(reasons) - 3} more checks")


def _clarification_recovery_from_diagnostic(
    diagnostic: ResponseValidationDiagnostic,
    *,
    payload: dict[str, object] | None,
) -> _PlanningClarificationRecovery | None:
    """Recover one atomic user question instead of repairing user authority."""

    if diagnostic.failure_class is not ResponseFailureClass.MISSING_USER_DECISION:
        return None
    for dimension in _USER_DECISION_PRODUCT_DIMENSIONS:
        base_path = f"/proposal/product_definition/{dimension.value}"
        matching = tuple(
            issue
            for issue in diagnostic.issues
            if issue.authority is ResponseIssueAuthority.USER
            and (issue.path == base_path or issue.path.startswith(f"{base_path}/"))
        )
        if matching:
            return _PlanningClarificationRecovery(
                decision_category=PlanningDecisionCategory.PRODUCT_REQUIREMENT,
                invariant_ids=tuple(
                    sorted({issue.invariant_id or issue.code for issue in matching})
                ),
                messages=tuple(issue.message for issue in matching),
                dimension=dimension,
            )
    if payload is None:
        return None
    proposal = payload.get("proposal")
    decisions = None if not isinstance(proposal, dict) else proposal.get("decisions")
    if not isinstance(decisions, list):
        return None
    decisions_by_id = {
        item.get("id"): item
        for item in decisions
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    for issue in diagnostic.issues:
        if (
            issue.authority is not ResponseIssueAuthority.USER
            or issue.invariant_id != "planning_question_decision_completeness"
        ):
            continue
        question_ids = tuple(
            subject.identifier
            for subject in issue.subjects
            if subject.kind is ResponseIssueSubjectKind.QUESTION
        )
        decision_ids = tuple(
            subject.identifier
            for subject in issue.subjects
            if subject.kind is ResponseIssueSubjectKind.DECISION
        )
        if len(question_ids) != 1 or len(decision_ids) != 1:
            continue
        decision = decisions_by_id.get(decision_ids[0])
        if not isinstance(decision, dict):
            continue
        try:
            category = PlanningDecisionCategory(decision.get("category"))
        except (TypeError, ValueError):
            continue
        if _DECISION_AUTHORITY[category] is not PlanningDecisionAuthority.USER:
            continue
        summary = decision.get("summary")
        rationale = decision.get("rationale")
        if not isinstance(summary, str) or not isinstance(rationale, str):
            continue
        return _PlanningClarificationRecovery(
            decision_category=category,
            invariant_ids=(issue.invariant_id,),
            messages=(issue.message,),
            question_id=question_ids[0],
            decision_id=decision_ids[0],
            decision_summary=summary,
            decision_rationale=rationale,
        )
    return None


def _planning_wire_user_decision_invariants(
    payload: Mapping[str, object],
) -> tuple[_PlanningInvariant, ...]:
    """Preserve user authority when sibling schema failures mask product intent."""

    proposal = payload.get("proposal")
    definition = (
        None
        if not isinstance(proposal, Mapping)
        else proposal.get("product_definition")
    )
    if not isinstance(definition, Mapping):
        return ()
    invariants: list[_PlanningInvariant] = []
    for dimension in _USER_DECISION_PRODUCT_DIMENSIONS:
        item = definition.get(dimension.value)
        if (
            not isinstance(item, Mapping)
            or item.get("disposition")
            != ProductDefinitionDisposition.PLANNER_RECOMMENDATION.value
        ):
            continue
        invariants.append(_planning_user_decision_invariant(dimension))
    return tuple(invariants)


def _planning_validation_diagnostic(
    error: ValidationError,
    payload: dict[str, object],
) -> ResponseValidationDiagnostic:
    """Recover precise fields from proposal-level relational validators."""

    issues = error.errors(
        include_url=False,
        include_context=True,
        include_input=False,
    )
    typed = tuple(
        context_error.invariant
        for issue in issues
        if isinstance((context := issue.get("ctx")), dict)
        and isinstance(
            (context_error := context.get("error")),
            _PlanningModelInvariantError,
        )
    )
    if len(typed) == 1:
        return _planning_invariant_diagnostic(payload, typed[0])
    if typed:
        return diagnostic_from_invariant(
            payload,
            failure_class=ResponseFailureClass.SEMANTIC_CONTEXT,
            authority=ResponseIssueAuthority.CONTROLLER,
            code="planning_context_unclassified",
            invariant_id="planning_multiple_invariants_unclassified",
            subjects=(),
            message="Planning validation produced multiple relational invariants",
            paths=("/",),
        )
    user_decision_invariants = _planning_wire_user_decision_invariants(payload)
    if user_decision_invariants:
        return _planning_invariants_diagnostic(payload, user_decision_invariants)
    if any(
        tuple(issue["loc"]) == ("proposal",)
        and str(issue["type"]).startswith("value_error")
        for issue in issues
    ):
        return diagnostic_from_invariant(
            payload,
            failure_class=ResponseFailureClass.SEMANTIC_CONTEXT,
            authority=ResponseIssueAuthority.CONTROLLER,
            code="planning_context_unclassified",
            invariant_id="planning_relational_invariant_unclassified",
            subjects=(),
            message="Planning relational validation has no typed correction authority",
            paths=("/",),
        )
    diagnostic = diagnostic_from_validation_error(error, payload)
    proposal = payload.get("proposal")
    decisions = None if not isinstance(proposal, dict) else proposal.get("decisions")
    authority_invariants: dict[str, _PlanningInvariant] = {}
    if isinstance(decisions, list):
        for issue in issues:
            location = tuple(issue["loc"])
            if (
                len(location) != 3
                or location[:2] != ("proposal", "decisions")
                or not isinstance(location[2], int)
                or location[2] >= len(decisions)
                or not str(issue["type"]).startswith("value_error")
            ):
                continue
            decision = decisions[location[2]]
            if not isinstance(decision, dict):
                continue
            try:
                category = PlanningDecisionCategory(decision.get("category"))
                provenance = PlanningDecisionProvenance.model_validate(
                    decision.get("provenance")
                )
            except (TypeError, ValueError, ValidationError):
                continue
            category_authority = _DECISION_AUTHORITY[category]
            provenance_authority = _DECISION_PROVENANCE_AUTHORITY[provenance.kind]
            decision_id = decision.get("id")
            if (
                category_authority is provenance_authority
                or decision.get("authority") != category_authority.value
                or not isinstance(decision_id, str)
            ):
                continue
            path = f"/proposal/decisions/{location[2]}"
            authority_invariants[path] = _PlanningInvariant(
                invariant_id="planning_decision_authority_provenance",
                message=(
                    f"decision {decision_id} category {category.value} belongs to "
                    f"{category_authority.value}, while {provenance.kind.value} "
                    f"provenance belongs to {provenance_authority.value}"
                ),
                paths=(path,),
                subjects=_planning_subjects(
                    (ResponseIssueSubjectKind.DECISION, decision_id)
                ),
                failure_class=ResponseFailureClass.SEMANTIC_SCHEMA,
            )
    if authority_invariants:
        diagnostic = diagnostic.model_copy(
            update={
                "issues": tuple(
                    issue.model_copy(
                        update={
                            "invariant_id": invariant.invariant_id,
                            "message": invariant.message,
                            "subjects": invariant.subjects,
                            "authority": invariant.authority,
                        }
                    )
                    if (invariant := authority_invariants.get(issue.path)) is not None
                    else issue
                    for issue in diagnostic.issues
                )
            }
        )
    if any(
        issue.path == "/question" or issue.path.startswith("/question/")
        for issue in diagnostic.issues
    ):
        correction_paths = tuple(
            sorted(
                {
                    (
                        "/question"
                        if path == "/question" or path.startswith("/question/")
                        else path
                    )
                    for path in diagnostic.correction_paths
                }
            )
        )
        return diagnostic.model_copy(update={"correction_paths": correction_paths})
    return diagnostic


def _compile_team_topology_projection(
    body: PlanningProposalBody,
) -> tuple[ProductDefinition | None, tuple[PlanningDecisionRecord, ...]]:
    """Project team and topology-dependent cost text from the typed Agent graph."""

    specialization_counts: dict[AgentSpecialization, int] = {}
    for agent in body.agents:
        specialization_counts[agent.specialization] = (
            specialization_counts.get(agent.specialization, 0) + 1
        )
    composition = ", ".join(
        f"{specialization.value}={count}"
        for specialization, count in specialization_counts.items()
    )
    summary = (
        f"Controller-derived from the typed Agent graph: {len(body.agents)} runtime "
        f"Agents; specialization composition: {composition}. The typed Agent "
        "identities and dependencies are the sole runtime-team authority."
    )
    rationale = (
        "The Planner proposes the typed Agent graph; the Controller validates its "
        "capabilities, permissions, dependencies, outputs, and acceptance scopes. "
        "Per-Agent rationales below explain the task-specific composition."
    )
    cost_summary = (
        "Controller-derived execution-cost scope from the typed Agent graph: "
        f"{len(body.agents)} runtime Agents across {composition}. Planning and "
        "Agent calls share the approved task-wide model budget, and actual cost is "
        "metered from settled calls. This summary cannot authorize another Agent "
        "or an independent budget."
    )
    product_definition = body.product_definition
    if product_definition is not None:
        product_definition = product_definition.model_copy(
            update={
                "impact": product_definition.impact.model_copy(
                    update={"team": summary, "cost": cost_summary}
                )
            }
        )
    decisions = tuple(
        decision.model_copy(update={"summary": summary, "rationale": rationale})
        if decision.category is PlanningDecisionCategory.TEAM
        else decision
        for decision in body.decisions
    )
    return product_definition, decisions


def _compile_model_route_projection(
    decisions: Collection[PlanningDecisionRecord],
    model_routes: ModelRoutePlan,
) -> tuple[PlanningDecisionRecord, ...]:
    """Project model-route decisions from the resolved Controller plan."""

    primary_route_count = len(
        {assignment.primary_route_id for assignment in model_routes.assignments}
    )
    switch_conditions = (
        ", ".join(
            condition.value for condition in model_routes.authorized_switch_conditions
        )
        or "none"
    )
    summary = (
        "Controller-derived from the approved model route plan: "
        f"runtime Agent assignments: {len(model_routes.assignments)}; authorized "
        f"route count: {len(model_routes.routes)}; primary route count: "
        f"{primary_route_count}; mode: {model_routes.mode.value}; authorized switch "
        f"conditions: {switch_conditions}. Per-Agent assignments shown below are "
        "the sole runtime model-routing authority."
    )
    rationale = (
        "The Controller resolves every Agent from the user-authorized routing "
        "policy, executable capability, and any approved per-Agent override. "
        "Planner prose cannot create, remove, or switch a model route."
    )
    return tuple(
        decision.model_copy(update={"summary": summary, "rationale": rationale})
        if decision.category is PlanningDecisionCategory.MODEL_ROUTE
        else decision
        for decision in decisions
    )
