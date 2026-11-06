"""Validator-owned invariants, exceptions, and recovery definitions for Planning."""

from __future__ import annotations

from dataclasses import dataclass

from software_agent_team.artifacts import ProductDefinitionDimension
from software_agent_team.planning.models_basic import PlanningDecisionCategory
from software_agent_team.responses import (
    ResponseFailureClass,
    ResponseIssueAuthority,
    ResponseIssueSubject,
    ResponseIssueSubjectKind,
)


class PlanningError(RuntimeError):
    """Raised when an adaptive Planning session cannot continue safely."""


class PlanningIntegrityError(PlanningError):
    """Raised when persisted Planning evidence is incomplete or changed."""


@dataclass(frozen=True)
class _PlanningInvariant:
    """Validator-owned identity and correction authority for one defect."""

    invariant_id: str
    message: str
    paths: tuple[str, ...]
    subjects: tuple[ResponseIssueSubject, ...] = ()
    failure_class: ResponseFailureClass = ResponseFailureClass.SEMANTIC_CONTEXT
    authority: ResponseIssueAuthority = ResponseIssueAuthority.MODEL


_USER_DECISION_PRODUCT_DIMENSIONS = (
    ProductDefinitionDimension.TARGET_USERS,
    ProductDefinitionDimension.PRIMARY_WORKFLOW,
    ProductDefinitionDimension.DELIVERY_MATURITY,
)


def _planning_user_decision_invariant(
    dimension: ProductDefinitionDimension,
) -> _PlanningInvariant:
    """Own the shared diagnostic for a Planner-selected user decision."""

    return _PlanningInvariant(
        invariant_id="planning_product_user_decision_required",
        message=f"{dimension.value} cannot be silently chosen by Planning",
        paths=(f"/proposal/product_definition/{dimension.value}",),
        failure_class=ResponseFailureClass.MISSING_USER_DECISION,
        authority=ResponseIssueAuthority.USER,
    )


@dataclass(frozen=True)
class _PlanningClarificationRecovery:
    """One user-owned decision that an invalid proposal must ask about."""

    decision_category: PlanningDecisionCategory
    invariant_ids: tuple[str, ...]
    messages: tuple[str, ...]
    question_id: str | None = None
    decision_id: str | None = None
    decision_summary: str | None = None
    decision_rationale: str | None = None
    dimension: ProductDefinitionDimension | None = None


class _PlanningModelInvariantError(ValueError):
    """Carry a typed proposal invariant through Pydantic validation."""

    def __init__(self, invariant: _PlanningInvariant) -> None:
        self.invariant = invariant
        super().__init__(invariant.message)


class _PlanningContextInvariantError(PlanningError):
    """Carry a typed post-schema Planning invariant to response compilation."""

    def __init__(
        self,
        invariant: _PlanningInvariant,
        *,
        failure_class: ResponseFailureClass | None = None,
        authority: ResponseIssueAuthority | None = None,
    ) -> None:
        self.invariant = invariant
        self.failure_class = (
            invariant.failure_class if failure_class is None else failure_class
        )
        self.authority = invariant.authority if authority is None else authority
        super().__init__(invariant.message)


class _PlanningContextInvariantsError(PlanningError):
    """Carry independent typed Planning invariants from one validation pass."""

    def __init__(self, invariants: tuple[_PlanningInvariant, ...]) -> None:
        if not invariants:
            raise ValueError("Planning invariant collection cannot be empty")
        self.invariants = invariants
        super().__init__("; ".join(item.message for item in invariants)[:1500])


def _planning_subjects(
    *items: tuple[ResponseIssueSubjectKind, str],
) -> tuple[ResponseIssueSubject, ...]:
    """Build one canonical structured subject set."""

    return tuple(
        ResponseIssueSubject(kind=kind, identifier=identifier)
        for kind, identifier in sorted(
            set(items),
            key=lambda item: (item[0].value, item[1]),
        )
    )


def _planning_model_invariant(
    invariant_id: str,
    message: str,
    *,
    paths: tuple[str, ...],
    subjects: tuple[ResponseIssueSubject, ...] = (),
) -> _PlanningModelInvariantError:
    return _PlanningModelInvariantError(
        _PlanningInvariant(
            invariant_id=invariant_id,
            message=message,
            paths=tuple(sorted(set(paths))),
            subjects=subjects,
        )
    )


def _planning_context_invariant(
    invariant_id: str,
    message: str,
    *,
    paths: tuple[str, ...],
    subjects: tuple[ResponseIssueSubject, ...] = (),
    failure_class: ResponseFailureClass = ResponseFailureClass.SEMANTIC_CONTEXT,
    authority: ResponseIssueAuthority = ResponseIssueAuthority.MODEL,
) -> _PlanningContextInvariantError:
    return _PlanningContextInvariantError(
        _PlanningInvariant(
            invariant_id=invariant_id,
            message=message,
            paths=tuple(sorted(set(paths))),
            subjects=subjects,
            failure_class=failure_class,
            authority=authority,
        ),
    )
