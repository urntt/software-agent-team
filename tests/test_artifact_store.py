"""Tests for concrete phase artifacts and immutable artifact persistence."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

import software_agent_team.artifact_store as artifact_store_module
from software_agent_team.artifact_store import (
    ArtifactAlreadyExistsError,
    ArtifactIntegrityError,
    ArtifactStore,
    ArtifactStoreError,
)
from software_agent_team.artifacts import (
    AgentRole,
    ArtifactKind,
    ArtifactReference,
    CheckStatus,
    CommandEvidence,
    CriterionResult,
    FinalReport,
    FinalStatus,
    ImplementationPlan,
    IterationDecision,
    IterationRecord,
    PlanTask,
    ReviewReport,
    ReviewVerdict,
    TaskBrief,
    WorkResult,
    parse_phase_artifact,
)
from software_agent_team.artifacts import TestReport as PhaseTestReport
from software_agent_team.teams import TeamDefinition, load_team_manifest

REPOSITORY_ROOT = Path(__file__).parents[1]
TEAM_CONFIG = REPOSITORY_ROOT / "configs" / "teams.json"
CREATED_AT = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
INPUT_COMMIT = "a" * 40
OUTPUT_COMMIT = "b" * 40
SHA256 = "c" * 64


def task_brief() -> TaskBrief:
    """Load the checked-in confirmed benchmark brief."""

    return TaskBrief.model_validate_json(
        (REPOSITORY_ROOT / "examples" / "task-brief.json").read_text(encoding="utf-8")
    )


def function_team() -> TeamDefinition:
    """Load the first vertical-slice team definition."""

    return load_team_manifest(TEAM_CONFIG).get_team("function_specialized")


def reference(kind: ArtifactKind, path: str) -> ArtifactReference:
    """Create a structurally valid reference for model tests."""

    return ArtifactReference(kind=kind, path=path, sha256=SHA256)


def implementation_plan() -> ImplementationPlan:
    """Return a plan covering every checked-in acceptance criterion."""

    return ImplementationPlan(
        run_id="task-manager-001",
        team_id="function_specialized",
        created_at=CREATED_AT,
        objective="Build the confirmed task manager.",
        approach=("Implement the vertical slice.", "Add acceptance evidence."),
        tasks=(
            PlanTask(
                id="TASK_APPLICATION",
                owner=AgentRole.GENERALIST_DEVELOPER,
                description="Implement the application.",
                acceptance_criteria=("AC_CREATE", "AC_PERSIST"),
                expected_paths=("app/", "templates/"),
            ),
            PlanTask(
                id="TASK_QUALITY",
                owner=AgentRole.GENERALIST_DEVELOPER,
                description="Add tests and documentation.",
                dependencies=("TASK_APPLICATION",),
                acceptance_criteria=("AC_QUALITY",),
                expected_paths=("tests/", "README.md"),
            ),
        ),
        risks=("Validation behavior requires focused tests.",),
    )


def work_result(*, iteration: int = 1) -> WorkResult:
    """Return a committed implementation result."""

    return WorkResult(
        run_id="task-manager-001",
        team_id="function_specialized",
        producer=AgentRole.GENERALIST_DEVELOPER,
        created_at=CREATED_AT,
        iteration=iteration,
        input_commit=INPUT_COMMIT,
        output_commit=OUTPUT_COMMIT,
        summary="Implemented the requested application.",
        completed_tasks=("TASK_APPLICATION", "TASK_QUALITY"),
        changed_files=("app/main.py", "tests/test_app.py", "README.md"),
    )


def make_test_report() -> PhaseTestReport:
    """Return passing deterministic and acceptance evidence."""

    command = CommandEvidence(
        id="CHECK_PYTEST",
        argv=("pytest", "-q"),
        exit_code=0,
        duration_ms=250,
        stdout_path="iterations/01/commands/pytest.stdout.txt",
        stderr_path="iterations/01/commands/pytest.stderr.txt",
        summary="All tests passed.",
    )
    criteria = tuple(
        CriterionResult(
            criterion_id=criterion.id,
            status=CheckStatus.PASSED,
            command_ids=(command.id,),
            detail="Covered by the acceptance suite.",
        )
        for criterion in task_brief().acceptance_criteria
    )
    return PhaseTestReport(
        run_id="task-manager-001",
        team_id="function_specialized",
        created_at=CREATED_AT,
        iteration=1,
        input_commit=OUTPUT_COMMIT,
        status=CheckStatus.PASSED,
        commands=(command,),
        criteria=criteria,
        summary="All deterministic checks and acceptance criteria passed.",
    )


def review_report() -> ReviewReport:
    """Return an accepting independent review."""

    return ReviewReport(
        run_id="task-manager-001",
        team_id="function_specialized",
        created_at=CREATED_AT,
        iteration=1,
        input_commit=OUTPUT_COMMIT,
        verdict=ReviewVerdict.ACCEPT,
        summary="No blocking review findings remain.",
    )


def make_store(tmp_path: Path) -> ArtifactStore:
    """Create a context-bound store in an isolated run directory."""

    run_directory = tmp_path / "runs" / "task-manager-001"
    run_directory.mkdir(parents=True)
    return ArtifactStore(
        run_directory,
        task_brief=task_brief(),
        team=function_team(),
        iteration_limit=2,
    )


def write_iteration_artifacts(
    store: ArtifactStore,
) -> tuple[ArtifactReference, ArtifactReference, ArtifactReference, ArtifactReference]:
    """Persist the four inputs required by an iteration record."""

    plan_reference = store.write(implementation_plan())
    work_reference = store.write(work_result())
    test_reference = store.write(make_test_report())
    review_reference = store.write(review_report())
    return plan_reference, work_reference, test_reference, review_reference


def test_store_round_trips_all_six_phase_artifacts(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    plan_ref, work_ref, test_ref, review_ref = write_iteration_artifacts(store)
    iteration = IterationRecord(
        run_id="task-manager-001",
        team_id="function_specialized",
        created_at=CREATED_AT,
        iteration=1,
        input_commit=INPUT_COMMIT,
        output_commit=OUTPUT_COMMIT,
        implementation_plan=plan_ref,
        work_result=work_ref,
        test_report=test_ref,
        review_report=review_ref,
        decision=IterationDecision.ACCEPT,
        summary="The implementation is ready for delivery.",
    )
    iteration_ref = store.write(iteration)
    final = FinalReport(
        run_id="task-manager-001",
        team_id="function_specialized",
        created_at=CREATED_AT,
        status=FinalStatus.COMPLETED,
        termination_reason="succeeded",
        final_commit=OUTPUT_COMMIT,
        iterations=(iteration_ref,),
        acceptance_results=make_test_report().criteria,
        summary="The runnable product passed every acceptance criterion.",
    )
    final_ref = store.write(final)

    expected_paths = [
        (plan_ref, "implementation-plan.json"),
        (work_ref, "iterations/01/work-result.json"),
        (test_ref, "iterations/01/test-report.json"),
        (review_ref, "iterations/01/review-report.json"),
        (iteration_ref, "iterations/01/iteration-record.json"),
        (final_ref, "final-report.json"),
    ]
    for artifact_reference, expected_path in expected_paths:
        assert artifact_reference.path == expected_path
        assert len(artifact_reference.sha256) == 64
        loaded = store.load(artifact_reference)
        assert loaded.kind is artifact_reference.kind


def test_store_rejects_overwriting_an_artifact(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    artifact = implementation_plan()
    original = store.write(artifact)

    with pytest.raises(ArtifactAlreadyExistsError, match="already exists"):
        store.write(artifact)

    assert store.load(original) == artifact


def test_store_detects_artifact_tampering(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    artifact_reference = store.write(implementation_plan())
    artifact_path = store.root / artifact_reference.path
    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    payload["objective"] = "A modified objective"
    artifact_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ArtifactIntegrityError, match="digest"):
        store.load(artifact_reference)


def test_iteration_record_must_match_referenced_commits(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    plan_ref, work_ref, test_ref, review_ref = write_iteration_artifacts(store)
    iteration = IterationRecord(
        run_id="task-manager-001",
        team_id="function_specialized",
        created_at=CREATED_AT,
        iteration=1,
        input_commit="d" * 40,
        output_commit=OUTPUT_COMMIT,
        implementation_plan=plan_ref,
        work_result=work_ref,
        test_report=test_ref,
        review_report=review_ref,
        decision=IterationDecision.ACCEPT,
        summary="This record contains a mismatched input commit.",
    )

    with pytest.raises(ArtifactStoreError, match="commits"):
        store.write(iteration)


def test_final_report_must_match_final_iteration_evidence(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    plan_ref, work_ref, test_ref, review_ref = write_iteration_artifacts(store)
    iteration_ref = store.write(
        IterationRecord(
            run_id="task-manager-001",
            team_id="function_specialized",
            created_at=CREATED_AT,
            iteration=1,
            input_commit=INPUT_COMMIT,
            output_commit=OUTPUT_COMMIT,
            implementation_plan=plan_ref,
            work_result=work_ref,
            test_report=test_ref,
            review_report=review_ref,
            decision=IterationDecision.ACCEPT,
            summary="The iteration is ready for delivery.",
        )
    )
    criteria = list(make_test_report().criteria)
    criteria[0] = criteria[0].model_copy(
        update={"detail": "A mismatched final detail."}
    )
    final = FinalReport(
        run_id="task-manager-001",
        team_id="function_specialized",
        created_at=CREATED_AT,
        status=FinalStatus.COMPLETED,
        termination_reason="succeeded",
        final_commit=OUTPUT_COMMIT,
        iterations=(iteration_ref,),
        acceptance_results=tuple(criteria),
        summary="The final evidence was altered.",
    )

    with pytest.raises(ArtifactStoreError, match="final iteration evidence"):
        store.write(final)


@pytest.mark.parametrize(
    "artifact",
    [
        implementation_plan().model_copy(update={"run_id": "another-run"}),
        work_result().model_copy(update={"producer": AgentRole.FRONTEND_DEVELOPER}),
        work_result(iteration=3),
    ],
)
def test_store_rejects_artifacts_outside_the_run_context(
    tmp_path: Path,
    artifact: ImplementationPlan | WorkResult,
) -> None:
    store = make_store(tmp_path)

    with pytest.raises(ArtifactStoreError, match="context"):
        store.write(artifact)


def test_store_rejects_incomplete_acceptance_coverage(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    plan = implementation_plan().model_copy(
        update={"tasks": implementation_plan().tasks[:1]}
    )

    with pytest.raises(ArtifactStoreError, match="context"):
        store.write(plan)


def test_store_rejects_a_symlinked_artifact_parent(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (store.root / "iterations").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ArtifactIntegrityError, match="real directory"):
        store.write(work_result())

    assert not list(outside.iterdir())


def test_failed_atomic_link_leaves_no_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = make_store(tmp_path)

    def fail_link(source: Path, destination: Path) -> None:
        raise OSError(f"cannot link {source} to {destination}")

    monkeypatch.setattr(artifact_store_module.os, "link", fail_link)
    with pytest.raises(OSError, match="cannot link"):
        store.write(implementation_plan())

    assert not (store.root / "implementation-plan.json").exists()
    assert not list(store.root.glob(".implementation-plan.json.*.tmp"))


def test_reference_rejects_an_invalid_digest() -> None:
    with pytest.raises(ValidationError):
        ArtifactReference(
            kind=ArtifactKind.WORK_RESULT,
            path="iterations/01/work-result.json",
            sha256="short",
        )


def test_parser_rejects_unknown_and_unimplemented_kinds() -> None:
    with pytest.raises(ValueError, match="supported kind"):
        parse_phase_artifact({"kind": "unknown"})
    with pytest.raises(ValueError, match="unsupported"):
        parse_phase_artifact({"kind": "task_brief"})


def test_plan_rejects_cyclic_dependencies() -> None:
    payload = implementation_plan().model_dump(mode="json")
    payload["tasks"][0]["dependencies"] = ["TASK_QUALITY"]

    with pytest.raises(ValidationError, match="acyclic"):
        ImplementationPlan.model_validate(payload)


def test_work_result_requires_a_new_commit() -> None:
    payload = work_result().model_dump(mode="json")
    payload["output_commit"] = payload["input_commit"]

    with pytest.raises(ValidationError, match="must differ"):
        WorkResult.model_validate(payload)


def test_passed_test_report_requires_passing_commands() -> None:
    payload = make_test_report().model_dump(mode="json")
    payload["commands"][0]["exit_code"] = 1

    with pytest.raises(ValidationError, match="all evidence"):
        PhaseTestReport.model_validate(payload)


def test_structural_parser_accepts_checked_in_plan() -> None:
    payload = json.loads(
        (REPOSITORY_ROOT / "examples" / "implementation-plan.json").read_text(
            encoding="utf-8"
        )
    )

    artifact = parse_phase_artifact(payload)

    assert isinstance(artifact, ImplementationPlan)
