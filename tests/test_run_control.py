"""Tests for deterministic run lifecycle, persistence, and recovery."""

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

import software_agent_team.run_control as run_control
from software_agent_team.artifacts import (
    ArtifactKind,
    ArtifactReference,
    IterationDecision,
    TaskBrief,
)
from software_agent_team.git_workspace import GitSnapshot, GitWorkspace
from software_agent_team.run_control import (
    InvalidRunTransitionError,
    RunAlreadyExistsError,
    RunConflictError,
    RunControlError,
    RunController,
    RunIntegrityError,
    RunNotFoundError,
    RunPhase,
    RunRecord,
    RunStore,
    RunTransition,
    TerminationReason,
)
from software_agent_team.teams import TeamManifest, load_team_manifest

REPOSITORY_ROOT = Path(__file__).parents[1]
TEAM_CONFIG = REPOSITORY_ROOT / "configs" / "teams.json"
FIXED_TIME = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
BASE_COMMIT = "1" * 40
SHA256 = "a" * 64

PHASES_TO_DECISION = [
    RunPhase.PREPARING_WORKTREE,
    RunPhase.PLANNING,
    RunPhase.IMPLEMENTING,
    RunPhase.SNAPSHOTTING,
    RunPhase.VERIFYING,
    RunPhase.REVIEWING,
    RunPhase.DECIDING,
]
LEGAL_PHASE_TRANSITIONS = {
    (RunPhase.CREATED, RunPhase.PREPARING_WORKTREE),
    (RunPhase.PREPARING_WORKTREE, RunPhase.PLANNING),
    (RunPhase.PLANNING, RunPhase.IMPLEMENTING),
    (RunPhase.IMPLEMENTING, RunPhase.SNAPSHOTTING),
    (RunPhase.SNAPSHOTTING, RunPhase.VERIFYING),
    (RunPhase.VERIFYING, RunPhase.REVIEWING),
    (RunPhase.REVIEWING, RunPhase.DECIDING),
    (RunPhase.DECIDING, RunPhase.IMPLEMENTING),
    (RunPhase.DECIDING, RunPhase.DELIVERING),
    (RunPhase.DELIVERING, RunPhase.COMPLETED),
} | {
    (phase, RunPhase.FAILED)
    for phase in RunPhase
    if phase not in {RunPhase.COMPLETED, RunPhase.FAILED}
}


def confirmed_task_brief(run_id: str = "task-manager-001") -> TaskBrief:
    """Return a confirmed task brief with a selectable run ID."""

    payload = json.loads(
        (REPOSITORY_ROOT / "examples" / "task-brief.json").read_text(encoding="utf-8")
    )
    payload["run_id"] = run_id
    return TaskBrief.model_validate(payload)


def make_controller(
    root: Path,
    *,
    manifest: TeamManifest | None = None,
    clock: Callable[[], datetime] = lambda: FIXED_TIME,
) -> RunController:
    """Build a controller around an isolated run store."""

    return RunController(
        RunStore(root),
        manifest or load_team_manifest(TEAM_CONFIG),
        clock=clock,
    )


def create_run(
    controller: RunController,
    *,
    run_id: str = "task-manager-001",
    iteration_limit: int = 2,
) -> RunRecord:
    """Create one function-specialized run for a test."""

    return controller.create(
        confirmed_task_brief(run_id),
        team_id="function_specialized",
        iteration_limit=iteration_limit,
    )


def workspace(run_id: str) -> GitWorkspace:
    """Return deterministic worktree evidence for state-controller tests."""

    return GitWorkspace(
        run_id=run_id,
        source_repository="/tmp/source-repository",
        worktree_path=f"/tmp/worktrees/{run_id}",
        base_commit=BASE_COMMIT,
        created_at=FIXED_TIME,
    )


def snapshot(record: RunRecord) -> GitSnapshot:
    """Return the next deterministic snapshot in a run's commit chain."""

    assert record.current_commit is not None
    output_commit = str(record.current_iteration + 1) * 40
    return GitSnapshot(
        run_id=record.run_id,
        iteration=record.current_iteration,
        input_commit=record.current_commit,
        output_commit=output_commit,
        commit_count=1,
        changed_files=(f"iteration-{record.current_iteration}.txt",),
        recorded_at=FIXED_TIME,
    )


def advance(
    controller: RunController,
    record: RunRecord,
    target: RunPhase,
) -> RunRecord:
    """Advance with the current optimistic-concurrency revision."""

    if record.phase is RunPhase.PREPARING_WORKTREE and target is RunPhase.PLANNING:
        return controller.attach_workspace(
            record.run_id,
            expected_revision=record.revision,
            workspace=workspace(record.run_id),
        )
    if record.phase is RunPhase.SNAPSHOTTING and target is RunPhase.VERIFYING:
        return controller.record_snapshot(
            record.run_id,
            expected_revision=record.revision,
            snapshot=snapshot(record),
        )
    evidence: tuple[ArtifactReference, ...] = ()
    required = {
        (RunPhase.PLANNING, RunPhase.IMPLEMENTING): (
            (ArtifactKind.IMPLEMENTATION_PLAN, "implementation-plan.json"),
        ),
        (RunPhase.IMPLEMENTING, RunPhase.SNAPSHOTTING): (
            (
                ArtifactKind.WORK_RESULT,
                f"iterations/{record.current_iteration:02d}/work-result.json",
            ),
        ),
        (RunPhase.VERIFYING, RunPhase.REVIEWING): (
            (
                ArtifactKind.TEST_REPORT,
                f"iterations/{record.current_iteration:02d}/test-report.json",
            ),
        ),
        (RunPhase.REVIEWING, RunPhase.DECIDING): (
            (
                ArtifactKind.REVIEW_REPORT,
                f"iterations/{record.current_iteration:02d}/review-report.json",
            ),
            (
                ArtifactKind.ITERATION_RECORD,
                f"iterations/{record.current_iteration:02d}/iteration-record.json",
            ),
        ),
    }.get((record.phase, target), ())
    evidence = tuple(
        ArtifactReference(kind=kind, path=path, sha256=SHA256)
        for kind, path in required
    )
    decision = None
    if record.phase is RunPhase.DECIDING:
        decision = (
            IterationDecision.REVISE
            if target is RunPhase.IMPLEMENTING
            else IterationDecision.ACCEPT
        )
    return controller.advance(
        record.run_id,
        expected_revision=record.revision,
        target=target,
        reason=f"enter {target.value}",
        artifacts=evidence,
        decision=decision,
    )


def final_report_reference() -> ArtifactReference:
    """Return a deterministic terminal-report reference."""

    return ArtifactReference(
        kind=ArtifactKind.FINAL_REPORT,
        path="final-report.json",
        sha256=SHA256,
    )


def advance_through(
    controller: RunController,
    record: RunRecord,
    phases: list[RunPhase],
) -> RunRecord:
    """Apply an ordered phase sequence."""

    for phase in phases:
        record = advance(controller, record, phase)
    return record


def test_create_freezes_input_and_recovers_the_same_record(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    controller = make_controller(runs)

    record = create_run(controller)

    assert record.phase is RunPhase.CREATED
    assert record.current_iteration == 1
    assert record.iteration_limit == 2
    assert record.revision == 0
    assert len(record.team_definition_sha256) == 64
    assert len(record.task_brief_sha256) == 64
    assert (runs / record.run_id / "task-brief.json").is_file()
    assert (runs / record.run_id / "run.json").is_file()

    recovered = make_controller(runs).load(record.run_id)
    assert recovered == record


def test_create_requires_a_confirmed_task_brief(tmp_path: Path) -> None:
    controller = make_controller(tmp_path / "runs")
    draft = confirmed_task_brief().model_copy(update={"confirmed": False})

    with pytest.raises(RunControlError, match="confirmed"):
        controller.create(
            draft,
            team_id="function_specialized",
            iteration_limit=2,
        )


@pytest.mark.parametrize(
    ("team_id", "iteration_limit"),
    [("single_agent", 2), ("function_specialized", 4)],
)
def test_iteration_limit_cannot_exceed_team_configuration(
    tmp_path: Path,
    team_id: str,
    iteration_limit: int,
) -> None:
    controller = make_controller(tmp_path / "runs")

    with pytest.raises(RunControlError, match="iteration limit"):
        controller.create(
            confirmed_task_brief(),
            team_id=team_id,
            iteration_limit=iteration_limit,
        )


def test_successful_lifecycle_persists_auditable_history(tmp_path: Path) -> None:
    controller = make_controller(tmp_path / "runs")
    record = create_run(controller)
    record = advance_through(controller, record, PHASES_TO_DECISION)
    record = advance(controller, record, RunPhase.DELIVERING)

    record = controller.complete(
        record.run_id,
        expected_revision=record.revision,
        detail="Runnable product and evidence are ready.",
        final_report=final_report_reference(),
    )

    assert record.phase is RunPhase.COMPLETED
    assert record.termination_reason is TerminationReason.SUCCEEDED
    assert record.workspace == workspace(record.run_id)
    assert len(record.snapshots) == 1
    assert record.current_commit == "2" * 40
    assert record.revision == len(record.transitions) == 9
    assert [item.sequence for item in record.transitions] == list(range(1, 10))
    assert controller.load(record.run_id) == record


def test_revision_increments_iteration_once(tmp_path: Path) -> None:
    controller = make_controller(tmp_path / "runs")
    record = advance_through(controller, create_run(controller), PHASES_TO_DECISION)

    revised = advance(controller, record, RunPhase.IMPLEMENTING)

    assert revised.current_iteration == 2
    assert revised.transitions[-1].iteration_before == 1
    assert revised.transitions[-1].iteration_after == 2

    revised = advance_through(
        controller,
        revised,
        [
            RunPhase.SNAPSHOTTING,
            RunPhase.VERIFYING,
            RunPhase.REVIEWING,
            RunPhase.DECIDING,
            RunPhase.DELIVERING,
        ],
    )
    completed = controller.complete(
        revised.run_id,
        expected_revision=revised.revision,
        detail="The bounded revision resolved the blocking findings.",
        final_report=final_report_reference(),
    )
    assert completed.current_iteration == 2
    assert [item.output_commit for item in completed.snapshots] == [
        "2" * 40,
        "3" * 40,
    ]
    assert completed.phase is RunPhase.COMPLETED


def test_iteration_limit_rejects_another_revision_without_mutation(
    tmp_path: Path,
) -> None:
    controller = make_controller(tmp_path / "runs")
    record = create_run(controller, iteration_limit=1)
    record = advance_through(controller, record, PHASES_TO_DECISION)
    state_path = tmp_path / "runs" / record.run_id / "run.json"
    before = state_path.read_bytes()

    with pytest.raises(InvalidRunTransitionError, match="iteration limit"):
        advance(controller, record, RunPhase.IMPLEMENTING)

    assert state_path.read_bytes() == before
    assert controller.load(record.run_id) == record


def test_illegal_transition_does_not_modify_persisted_state(tmp_path: Path) -> None:
    controller = make_controller(tmp_path / "runs")
    record = create_run(controller)
    state_path = tmp_path / "runs" / record.run_id / "run.json"
    before = state_path.read_bytes()

    with pytest.raises(InvalidRunTransitionError, match="created -> planning"):
        advance(controller, record, RunPhase.PLANNING)

    assert state_path.read_bytes() == before
    assert controller.load(record.run_id) == record


def test_evidence_transitions_cannot_bypass_specialized_methods(
    tmp_path: Path,
) -> None:
    controller = make_controller(tmp_path / "runs")
    record = advance(controller, create_run(controller), RunPhase.PREPARING_WORKTREE)

    with pytest.raises(InvalidRunTransitionError, match="attach_workspace"):
        controller.advance(
            record.run_id,
            expected_revision=record.revision,
            target=RunPhase.PLANNING,
            reason="skip workspace evidence",
        )

    record = advance(controller, record, RunPhase.PLANNING)
    record = advance(controller, record, RunPhase.IMPLEMENTING)
    record = advance(controller, record, RunPhase.SNAPSHOTTING)
    with pytest.raises(InvalidRunTransitionError, match="record_snapshot"):
        controller.advance(
            record.run_id,
            expected_revision=record.revision,
            target=RunPhase.VERIFYING,
            reason="skip snapshot evidence",
        )


def test_workspace_and_snapshot_must_match_the_run(tmp_path: Path) -> None:
    controller = make_controller(tmp_path / "runs")
    record = advance(controller, create_run(controller), RunPhase.PREPARING_WORKTREE)
    wrong_workspace = workspace("different-run")

    with pytest.raises(RunIntegrityError, match="different run"):
        controller.attach_workspace(
            record.run_id,
            expected_revision=record.revision,
            workspace=wrong_workspace,
        )

    record = advance(controller, record, RunPhase.PLANNING)
    record = advance(controller, record, RunPhase.IMPLEMENTING)
    record = advance(controller, record, RunPhase.SNAPSHOTTING)
    wrong_snapshot = snapshot(record).model_copy(update={"input_commit": "f" * 40})
    with pytest.raises(RunIntegrityError, match="current commit"):
        controller.record_snapshot(
            record.run_id,
            expected_revision=record.revision,
            snapshot=wrong_snapshot,
        )


def test_transition_contract_rejects_every_illegal_phase_pair() -> None:
    for source in RunPhase:
        for target in RunPhase:
            if (source, target) in LEGAL_PHASE_TRANSITIONS:
                continue
            with pytest.raises(ValidationError, match="illegal run transition"):
                RunTransition(
                    sequence=1,
                    source=source,
                    target=target,
                    iteration_before=1,
                    iteration_after=1,
                    occurred_at=FIXED_TIME,
                    reason="invalid transition",
                )


@pytest.mark.parametrize("phase_count", range(len(PHASES_TO_DECISION) + 2))
def test_failure_is_recorded_from_every_non_terminal_phase(
    tmp_path: Path,
    phase_count: int,
) -> None:
    controller = make_controller(tmp_path / "runs")
    record = create_run(controller, run_id=f"failure-{phase_count}")
    path = [*PHASES_TO_DECISION, RunPhase.DELIVERING]
    record = advance_through(controller, record, path[:phase_count])

    failed = controller.fail(
        record.run_id,
        expected_revision=record.revision,
        reason=TerminationReason.EXECUTION_FAILED,
        detail="The assigned execution failed.",
        final_report=final_report_reference(),
    )

    assert failed.phase is RunPhase.FAILED
    assert failed.termination_reason is TerminationReason.EXECUTION_FAILED
    assert controller.load(failed.run_id) == failed


def test_terminal_run_cannot_transition_again(tmp_path: Path) -> None:
    controller = make_controller(tmp_path / "runs")
    record = controller.fail(
        create_run(controller).run_id,
        expected_revision=0,
        reason=TerminationReason.DEPENDENCY_UNAVAILABLE,
        detail="The required runtime is unavailable.",
        final_report=final_report_reference(),
    )

    with pytest.raises(InvalidRunTransitionError, match="terminal"):
        controller.fail(
            record.run_id,
            expected_revision=record.revision,
            reason=TerminationReason.CONTROLLER_ERROR,
            detail="A second terminal result must not be written.",
            final_report=final_report_reference(),
        )


def test_duplicate_run_does_not_overwrite_existing_state(tmp_path: Path) -> None:
    controller = make_controller(tmp_path / "runs")
    original = create_run(controller)

    with pytest.raises(RunAlreadyExistsError, match="already exists"):
        create_run(controller)

    assert controller.load(original.run_id) == original


def test_stale_revision_is_rejected(tmp_path: Path) -> None:
    controller = make_controller(tmp_path / "runs")
    original = create_run(controller)
    updated = advance(controller, original, RunPhase.PREPARING_WORKTREE)

    with pytest.raises(RunConflictError, match="expected 0, found 1"):
        controller.advance(
            original.run_id,
            expected_revision=original.revision,
            target=RunPhase.PLANNING,
            reason="use an obsolete revision",
        )

    assert controller.load(updated.run_id) == updated


def test_task_brief_tampering_is_detected(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    controller = make_controller(runs)
    record = create_run(controller)
    task_path = runs / record.run_id / "task-brief.json"
    payload = json.loads(task_path.read_text(encoding="utf-8"))
    payload["source_request"] = "A modified request"
    task_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RunIntegrityError, match="digest"):
        controller.load(record.run_id)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.update(schema_version=1),
        lambda payload: payload.update(schema_version=99),
        lambda payload: payload.update(run_id="different-run"),
        lambda payload: payload.update(phase="planning"),
    ],
)
def test_invalid_persisted_run_record_is_rejected(
    tmp_path: Path,
    mutation: Callable[[dict[str, object]], None],
) -> None:
    runs = tmp_path / "runs"
    controller = make_controller(runs)
    record = create_run(controller)
    state_path = runs / record.run_id / "run.json"
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    mutation(payload)
    state_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RunIntegrityError, match="persisted run"):
        controller.load(record.run_id)


def test_missing_run_file_is_rejected(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    controller = make_controller(runs)
    record = create_run(controller)
    (runs / record.run_id / "run.json").unlink()

    with pytest.raises(RunIntegrityError, match="persisted run"):
        controller.load(record.run_id)


def test_malformed_run_json_is_rejected(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    controller = make_controller(runs)
    record = create_run(controller)
    (runs / record.run_id / "run.json").write_text("{", encoding="utf-8")

    with pytest.raises(RunIntegrityError, match="persisted run"):
        controller.load(record.run_id)


def test_unknown_and_unsafe_run_ids_are_rejected(tmp_path: Path) -> None:
    controller = make_controller(tmp_path / "runs")

    with pytest.raises(RunNotFoundError, match="not found"):
        controller.load("missing-run")
    with pytest.raises(RunControlError, match="invalid run ID"):
        controller.load("../escaped")


def test_manifest_version_mismatch_blocks_recovery(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    manifest = load_team_manifest(TEAM_CONFIG)
    controller = make_controller(runs, manifest=manifest)
    record = create_run(controller)
    changed_manifest = TeamManifest.model_validate(
        {**manifest.model_dump(mode="json"), "schema_version": 2}
    )

    with pytest.raises(RunIntegrityError, match="manifest version"):
        make_controller(runs, manifest=changed_manifest).load(record.run_id)


def test_team_definition_change_blocks_recovery(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    manifest = load_team_manifest(TEAM_CONFIG)
    controller = make_controller(runs, manifest=manifest)
    record = create_run(controller)
    payload = manifest.model_dump(mode="json")
    for team in payload["teams"]:
        if team["id"] == record.team_id:
            team["description"] = "A changed team definition."
    changed_manifest = TeamManifest.model_validate(payload)

    with pytest.raises(RunIntegrityError, match="team definition"):
        make_controller(runs, manifest=changed_manifest).load(record.run_id)


def test_failed_atomic_replace_preserves_previous_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs = tmp_path / "runs"
    controller = make_controller(runs)
    record = create_run(controller)
    state_path = runs / record.run_id / "run.json"
    before = state_path.read_bytes()

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError(f"cannot replace {source} with {destination}")

    monkeypatch.setattr(run_control.os, "replace", fail_replace)
    with pytest.raises(OSError, match="cannot replace"):
        advance(controller, record, RunPhase.PREPARING_WORKTREE)

    assert state_path.read_bytes() == before
    assert controller.load(record.run_id) == record
    assert not list(state_path.parent.glob(".run.json.*.tmp"))


def test_failed_atomic_initialization_leaves_no_partial_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs = tmp_path / "runs"
    controller = make_controller(runs)

    def fail_rename(source: Path, destination: Path) -> None:
        raise OSError(f"cannot rename {source} to {destination}")

    monkeypatch.setattr(run_control.os, "rename", fail_rename)
    with pytest.raises(OSError, match="cannot rename"):
        create_run(controller)

    assert not (runs / "task-manager-001").exists()
    assert not list(runs.glob(".*.tmp"))


def test_controller_rejects_a_clock_that_moves_backwards(tmp_path: Path) -> None:
    times = iter([FIXED_TIME, FIXED_TIME - timedelta(seconds=1)])
    controller = make_controller(tmp_path / "runs", clock=lambda: next(times))
    record = create_run(controller)

    with pytest.raises(RunControlError, match="clock moved backwards"):
        advance(controller, record, RunPhase.PREPARING_WORKTREE)
