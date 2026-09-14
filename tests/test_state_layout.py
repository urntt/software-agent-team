"""Tests for the single SAT state layout owner and its remediation projections."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from software_agent_team.product import ProductStatePaths, ensure_product_state
from software_agent_team.state_layout import (
    STATE_MARKER_NAME,
    StateLayoutOperation,
    StateLayoutProblem,
    StateLayoutProblemKind,
    describe_state_layout_failure,
    inspect_state_layout,
)

FIRST_RUN_ONLY_PHRASES = ("Select a new empty SAT_STATE_ROOT", "then run SAT again")


def owned_state_root(tmp_path: Path) -> ProductStatePaths:
    """Create one complete SAT-owned layout to mutate in each scenario."""

    paths = ProductStatePaths.below((tmp_path / "state").resolve())
    ensure_product_state(paths)
    return paths


def test_every_problem_kind_has_a_remediation_for_every_operation() -> None:
    """A new problem kind cannot ship without both caller projections."""

    for kind in StateLayoutProblemKind:
        problem = StateLayoutProblem(
            path=Path("/example"),
            kind=kind,
            detail="example",
        )
        for operation in StateLayoutOperation:
            remediation = problem.remediation(operation)
            assert remediation
            assert "{path}" not in remediation


@pytest.mark.parametrize(
    "kind",
    [
        StateLayoutProblemKind.MARKER_MISSING,
        StateLayoutProblemKind.MARKER_INVALID,
        StateLayoutProblemKind.UNKNOWN_CATEGORY,
        StateLayoutProblemKind.OWNERSHIP_FOREIGN,
        StateLayoutProblemKind.ACCESS_DENIED,
        StateLayoutProblemKind.CATEGORY_NOT_DIRECTORY,
    ],
)
def test_uninstall_remediation_never_offers_a_first_run_state_root_choice(
    kind: StateLayoutProblemKind,
) -> None:
    """Uninstall must describe removal, never selection of another state root."""

    problem = StateLayoutProblem(path=Path("/example"), kind=kind, detail="example")
    uninstall = problem.remediation(StateLayoutOperation.UNINSTALL)

    assert "SAT_STATE_ROOT" not in uninstall
    assert "run SAT again" not in uninstall
    assert "uninstall again" in uninstall or "yourself" in uninstall


def test_first_run_remediation_keeps_its_existing_state_root_guidance() -> None:
    """The first-run projection must not regress while uninstall diverges."""

    problem = StateLayoutProblem(
        path=Path("/example"),
        kind=StateLayoutProblemKind.MARKER_MISSING,
        detail="example",
    )

    assert problem.remediation(StateLayoutOperation.FIRST_RUN) == (
        "Select a new empty SAT_STATE_ROOT, or restore the complete matching "
        "SAT-owned state root, then retry."
    )


def test_unknown_category_remediation_names_the_exact_entry(tmp_path: Path) -> None:
    """Both projections must point at the exact blocking path."""

    paths = owned_state_root(tmp_path)
    unknown = paths.root / "future-state"
    unknown.mkdir()

    observation = inspect_state_layout(paths.root)
    problem = observation.problems[0]

    assert problem.kind is StateLayoutProblemKind.UNKNOWN_CATEGORY
    for operation in StateLayoutOperation:
        assert str(unknown) in problem.remediation(operation)


def test_missing_marker_is_one_problem_kind_for_both_callers(tmp_path: Path) -> None:
    """Problem detection stays a single owner; only the next step differs."""

    paths = owned_state_root(tmp_path)
    (paths.root / STATE_MARKER_NAME).unlink()

    observation = inspect_state_layout(paths.root)

    assert [problem.kind for problem in observation.problems] == [
        StateLayoutProblemKind.MARKER_MISSING
    ]
    first_run_detail, first_run_step = describe_state_layout_failure(
        observation,
        operation=StateLayoutOperation.FIRST_RUN,
    )
    uninstall_detail, uninstall_step = describe_state_layout_failure(
        observation,
        operation=StateLayoutOperation.UNINSTALL,
    )

    assert first_run_detail == uninstall_detail
    assert first_run_detail == f"existing state root is not owned by SAT: {paths.root}"
    assert first_run_step != uninstall_step
    assert "SAT_STATE_ROOT" in first_run_step
    assert "SAT_STATE_ROOT" not in uninstall_step


def test_additional_problem_count_uses_the_requested_separator(
    tmp_path: Path,
) -> None:
    """Callers keep their own bounded suffix while sharing one detection pass."""

    paths = owned_state_root(tmp_path)
    (paths.root / STATE_MARKER_NAME).unlink()
    (paths.root / "future-state").mkdir()
    (paths.root / "another-state").mkdir()

    observation = inspect_state_layout(paths.root)
    assert len(observation.problems) == 3

    bracketed, _ = describe_state_layout_failure(
        observation,
        operation=StateLayoutOperation.UNINSTALL,
    )
    inline, _ = describe_state_layout_failure(
        observation,
        operation=StateLayoutOperation.FIRST_RUN,
        separator="; {count} additional problem(s)",
    )

    assert bracketed.endswith(" (2 additional problem(s))")
    assert inline.endswith("; 2 additional problem(s)")


def test_foreign_ownership_is_reported_against_the_invoking_uid(
    tmp_path: Path,
) -> None:
    """Ownership evidence stays uid-exact and is shared by both callers."""

    paths = owned_state_root(tmp_path)
    foreign_uid = os.stat(paths.root).st_uid + 1

    observation = inspect_state_layout(paths.root, invoking_uid=foreign_uid)

    kinds = {problem.kind for problem in observation.problems}
    assert StateLayoutProblemKind.OWNERSHIP_FOREIGN in kinds
    assert observation.invoking_uid == foreign_uid
