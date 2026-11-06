"""Reviewer scope assignments, invariants, and specialist authority resolution.

This module determines which Review Agent owns each acceptance criterion,
ensuring non-overlapping boundaries, mandatory specialist reviews (security, UX),
and valid Review task scopes.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping

from software_agent_team.artifacts import (
    ProductDefinition,
)
from software_agent_team.planning.entities import (
    ProposedAgent,
    ProposedCriterion,
    ProposedTask,
)
from software_agent_team.planning.invariants import (
    _planning_subjects,
    _PlanningInvariant,
)
from software_agent_team.planning.questions import _ALL_REVIEW_BOUNDARIES
from software_agent_team.response_corrections import ResponseIssueSubjectKind
from software_agent_team.teams import (
    AcceptanceAuthority,
    AgentCapability,
    AgentSpec,
    specialization_contract,
)


def _specialized_review_authority(
    criterion: ProposedCriterion,
    definition: ProductDefinition | None,
) -> AcceptanceAuthority | None:
    """Derive specialist authority from existing typed acceptance relationships."""

    requires_security = set(criterion.review_boundaries) == set(_ALL_REVIEW_BOUNDARIES)
    requires_experience = bool(
        definition is not None
        and criterion.id in definition.primary_workflow.criterion_ids
        and criterion.id in definition.usability_expectations.criterion_ids
    )
    if requires_security and requires_experience:
        raise ValueError(
            f"criterion {criterion.id} combines security and user-experience "
            "acceptance authority; split the claims into independently assigned "
            "criteria"
        )
    if requires_security:
        return AcceptanceAuthority.SECURITY
    if requires_experience:
        return AcceptanceAuthority.USER_EXPERIENCE
    return None


def _resolve_review_scope_assignments(
    *,
    criteria: Collection[ProposedCriterion],
    definition: ProductDefinition | None,
    agents: Collection[ProposedAgent | AgentSpec],
    profile_criterion_ids: Collection[str] = (),
) -> tuple[dict[str, tuple[str, ...]], tuple[_PlanningInvariant, ...]]:
    """Resolve one non-overlapping Reviewer owner for every manual criterion."""

    criterion_items = tuple(criteria)
    fixed_criteria = tuple(dict.fromkeys(profile_criterion_ids))
    agent_items = tuple(agents)
    agents_by_id = {agent.id: agent for agent in agent_items}
    reviewers = tuple(
        agent for agent in agent_items if agent.capability is AgentCapability.REVIEW
    )
    reviewer_ids = {agent.id for agent in reviewers}
    general_reviewers = tuple(
        agent
        for agent in reviewers
        if specialization_contract(agent.specialization).acceptance_authority
        is AcceptanceAuthority.GENERAL_REVIEW
    )
    scopes: dict[str, list[str]] = {agent.id: [] for agent in reviewers}
    invariants: list[_PlanningInvariant] = []

    if (criterion_items or fixed_criteria) and not reviewers:
        return {}, (
            _PlanningInvariant(
                invariant_id="planning_review_scope_reviewer_required",
                message=("manual acceptance requires an independent review Agent"),
                paths=("/proposal/agents",),
                subjects=_planning_subjects(
                    (ResponseIssueSubjectKind.CAPABILITY, "review")
                ),
            ),
        )

    def generic_owner(
        explicit: tuple[ProposedAgent | AgentSpec, ...],
    ) -> ProposedAgent | AgentSpec | None:
        explicit_general = tuple(
            agent
            for agent in explicit
            if specialization_contract(agent.specialization).acceptance_authority
            is AcceptanceAuthority.GENERAL_REVIEW
        )
        if len(explicit_general) == 1:
            return explicit_general[0]
        if len(explicit_general) > 1:
            return None
        if len(explicit) == 1:
            return explicit[0]
        if explicit:
            return None
        if len(general_reviewers) == 1:
            return general_reviewers[0]
        if len(reviewers) == 1:
            return reviewers[0]
        return None

    for criterion_index, criterion in enumerate(criterion_items):
        explicit_reviewers = tuple(
            agents_by_id[agent_id]
            for agent_id in criterion.verification_agent_ids
            if agent_id in reviewer_ids
        )
        try:
            required_authority = _specialized_review_authority(
                criterion,
                definition,
            )
        except ValueError as error:
            invariants.append(
                _PlanningInvariant(
                    invariant_id="planning_criterion_review_authority_conflict",
                    message=str(error),
                    paths=(
                        f"/proposal/acceptance_criteria/{criterion_index}",
                        "/proposal/product_definition/primary_workflow/criterion_ids",
                        "/proposal/product_definition/usability_expectations/criterion_ids",
                    ),
                    subjects=_planning_subjects(
                        (ResponseIssueSubjectKind.CRITERION, criterion.id)
                    ),
                )
            )
            continue

        if required_authority is None:
            owner = generic_owner(explicit_reviewers)
            if owner is None:
                invariants.append(
                    _PlanningInvariant(
                        invariant_id="planning_criterion_review_scope_ambiguous",
                        message=(
                            f"criterion {criterion.id} does not assign an "
                            "unambiguous general Review owner"
                        ),
                        paths=(
                            f"/proposal/acceptance_criteria/{criterion_index}/"
                            "verification_agent_ids",
                        ),
                        subjects=_planning_subjects(
                            (ResponseIssueSubjectKind.CRITERION, criterion.id),
                            (
                                ResponseIssueSubjectKind.CAPABILITY,
                                "general_review",
                            ),
                            *(
                                (ResponseIssueSubjectKind.AGENT, agent.id)
                                for agent in explicit_reviewers
                            ),
                        ),
                    )
                )
                continue
        else:
            matching = tuple(
                agent
                for agent in explicit_reviewers
                if specialization_contract(agent.specialization).acceptance_authority
                is required_authority
            )
            if len(matching) != 1 or len(explicit_reviewers) != 1:
                label = (
                    "security"
                    if required_authority is AcceptanceAuthority.SECURITY
                    else "user-experience"
                )
                available_matching = tuple(
                    agent
                    for agent in reviewers
                    if (
                        specialization_contract(
                            agent.specialization
                        ).acceptance_authority
                        is required_authority
                    )
                )
                correction_paths = (
                    f"/proposal/acceptance_criteria/{criterion_index}/"
                    "verification_agent_ids",
                )
                if len(available_matching) != 1:
                    correction_paths = ("/proposal/agents", *correction_paths)
                invariants.append(
                    _PlanningInvariant(
                        invariant_id="planning_criterion_specialist_required",
                        message=(
                            f"criterion {criterion.id} requires exactly one Review "
                            f"owner with {label} acceptance authority in its "
                            "verification assignment"
                        ),
                        paths=correction_paths,
                        subjects=_planning_subjects(
                            (ResponseIssueSubjectKind.CRITERION, criterion.id),
                            (
                                ResponseIssueSubjectKind.CAPABILITY,
                                required_authority.value,
                            ),
                            *(
                                (ResponseIssueSubjectKind.AGENT, agent.id)
                                for agent in explicit_reviewers
                            ),
                        ),
                    )
                )
                continue
            owner = matching[0]
        scopes[owner.id].append(criterion.id)

    if fixed_criteria:
        profile_owner = generic_owner(())
        if profile_owner is None:
            invariants.append(
                _PlanningInvariant(
                    invariant_id="planning_profile_review_scope_ambiguous",
                    message=(
                        "controller-owned profile criteria require one unambiguous "
                        "general Review owner"
                    ),
                    paths=("/proposal/agents",),
                    subjects=_planning_subjects(
                        (ResponseIssueSubjectKind.CAPABILITY, "general_review")
                    ),
                )
            )
        else:
            scopes[profile_owner.id].extend(fixed_criteria)

    if not invariants:
        unused = tuple(agent for agent in reviewers if not scopes[agent.id])
        if unused:
            invariants.append(
                _PlanningInvariant(
                    invariant_id="planning_review_agent_scope_required",
                    message=(
                        "every Review Agent requires an assigned acceptance scope: "
                        + ", ".join(agent.id for agent in unused)
                    ),
                    paths=("/proposal/agents",),
                    subjects=_planning_subjects(
                        *(
                            (ResponseIssueSubjectKind.AGENT, agent.id)
                            for agent in unused
                        )
                    ),
                )
            )

    return (
        {agent_id: tuple(criterion_ids) for agent_id, criterion_ids in scopes.items()},
        tuple(invariants),
    )


def _review_task_scope_invariants(
    *,
    tasks: Collection[ProposedTask],
    review_scope_by_agent: Mapping[str, Collection[str]],
) -> tuple[_PlanningInvariant, ...]:
    """Reject Review work that claims another Reviewer's acceptance authority."""

    invariants: list[_PlanningInvariant] = []
    normalized_scopes = {
        agent_id: set(criterion_ids)
        for agent_id, criterion_ids in review_scope_by_agent.items()
    }
    for task_index, task in enumerate(tasks):
        scope = normalized_scopes.get(task.owner_agent_id)
        if scope is None:
            continue
        outside_scope = tuple(
            criterion_id
            for criterion_id in task.acceptance_criteria
            if criterion_id not in scope
        )
        if not outside_scope:
            continue
        invariants.append(
            _PlanningInvariant(
                invariant_id="planning_review_task_scope",
                message=(
                    f"Review task {task.id} owned by {task.owner_agent_id} claims "
                    "criteria outside its compiled Review scope: "
                    + ", ".join(outside_scope)
                ),
                paths=(f"/proposal/tasks/{task_index}",),
                subjects=_planning_subjects(
                    (ResponseIssueSubjectKind.TASK, task.id),
                    (ResponseIssueSubjectKind.AGENT, task.owner_agent_id),
                    *(
                        (ResponseIssueSubjectKind.CRITERION, criterion_id)
                        for criterion_id in outside_scope
                    ),
                ),
            )
        )
    return tuple(invariants)
