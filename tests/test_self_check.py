"""Tests for the versioned dependency-aware task self-check contract."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from software_agent_team.self_check import (
    SelfCheckCategory,
    SelfCheckCheckpoint,
    SelfCheckError,
    SelfCheckEvidence,
    SelfCheckFreshness,
    SelfCheckOwner,
    SelfCheckResult,
    SelfCheckSeverity,
    SelfCheckStatus,
    TaskSelfCheckReport,
    TaskSelfCheckStore,
    invalidate_self_check_results,
    observation_sha256,
    reconcile_self_check_report,
    refresh_stale_self_checks,
    render_self_check_report,
)

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def result(
    check_id: str,
    *,
    dependencies: tuple[str, ...] = (),
    status: SelfCheckStatus = SelfCheckStatus.PASS,
    checkpoint: SelfCheckCheckpoint = SelfCheckCheckpoint.TASK_ADMISSION,
    checked_at: datetime = NOW,
) -> SelfCheckResult:
    non_passing = status is not SelfCheckStatus.PASS
    return SelfCheckResult(
        id=check_id,
        checkpoint=checkpoint,
        category=SelfCheckCategory.RUNTIME,
        owner=SelfCheckOwner.SAT,
        dependencies=dependencies,
        input_sha256=observation_sha256({"id": check_id, "time": str(checked_at)}),
        checked_at=checked_at,
        freshness=(
            SelfCheckFreshness.STALE
            if status is SelfCheckStatus.STALE
            else SelfCheckFreshness.FRESH
        ),
        severity=(
            SelfCheckSeverity.REQUIRED
            if status is SelfCheckStatus.BLOCKED
            else SelfCheckSeverity.WARNING
            if non_passing
            else SelfCheckSeverity.INFO
        ),
        status=status,
        observed_fact=f"observed {check_id}",
        evidence=(
            SelfCheckEvidence(
                kind="local_observation",
                reference=f"test:{check_id}",
            ),
        ),
        consequence="further work cannot rely on this fact" if non_passing else None,
        remediation="re-run the affected check" if non_passing else None,
        rerun_rule="Re-run when the observed test input changes.",
    )


def report(
    *checks: SelfCheckResult,
    revision: int = 1,
    previous: str | None = None,
    checkpoint: SelfCheckCheckpoint = SelfCheckCheckpoint.TASK_ADMISSION,
) -> TaskSelfCheckReport:
    return TaskSelfCheckReport(
        run_id="sat-20260904-test",
        checkpoint=checkpoint,
        revision=revision,
        previous_report_sha256=previous,
        created_at=NOW + timedelta(minutes=revision - 1),
        checks=checks,
    )


def test_report_requires_one_acyclic_known_dependency_graph() -> None:
    healthy = report(
        result("runtime.docker"),
        result("runtime.sandbox", dependencies=("runtime.docker",)),
    )

    assert healthy.ready

    with pytest.raises(ValidationError, match="unknown dependencies"):
        report(result("runtime.sandbox", dependencies=("runtime.missing",)))

    with pytest.raises(ValidationError, match="contains a cycle"):
        report(
            result("runtime.docker", dependencies=("runtime.sandbox",)),
            result("runtime.sandbox", dependencies=("runtime.docker",)),
        )


def test_result_requires_actionable_non_pass_state_and_consistent_freshness() -> None:
    payload = result("model.context", status=SelfCheckStatus.NEEDS_INPUT).model_dump()
    payload["remediation"] = None
    with pytest.raises(ValidationError, match="require consequence and remediation"):
        SelfCheckResult.model_validate(payload)

    payload = result("model.context").model_dump()
    payload["freshness"] = SelfCheckFreshness.STALE
    with pytest.raises(ValidationError, match="stale status and freshness must agree"):
        SelfCheckResult.model_validate(payload)


def test_changed_fact_invalidates_only_itself_and_transitive_dependents() -> None:
    checks = (
        result("tool.docker"),
        result("runtime.sandbox", dependencies=("tool.docker",)),
        result("route.default", dependencies=("runtime.sandbox",)),
        result("task.destination"),
    )

    invalidated = invalidate_self_check_results(
        checks,
        {"tool.docker"},
        reason="the Docker daemon identity changed",
    )

    by_id = {check.id: check for check in invalidated}
    assert by_id["tool.docker"].status is SelfCheckStatus.STALE
    assert by_id["runtime.sandbox"].status is SelfCheckStatus.STALE
    assert by_id["route.default"].status is SelfCheckStatus.STALE
    assert by_id["task.destination"].status is SelfCheckStatus.PASS


def test_refresh_requires_exact_stale_set_and_preserves_stable_definitions() -> None:
    original = (
        result("tool.docker"),
        result("runtime.sandbox", dependencies=("tool.docker",)),
    )
    stale = invalidate_self_check_results(
        original,
        {"tool.docker"},
        reason="the Docker daemon identity changed",
    )
    replacements = {
        item.id: result(
            item.id,
            dependencies=item.dependencies,
            checked_at=NOW + timedelta(minutes=1),
        )
        for item in stale
    }

    refreshed = refresh_stale_self_checks(stale, replacements)

    assert all(item.status is SelfCheckStatus.PASS for item in refreshed)
    with pytest.raises(SelfCheckError, match=r"missing runtime\.sandbox"):
        refresh_stale_self_checks(stale, {"tool.docker": replacements["tool.docker"]})
    changed_definition = replacements["runtime.sandbox"].model_copy(
        update={"owner": SelfCheckOwner.HOST}
    )
    with pytest.raises(SelfCheckError, match="changed its stable definition"):
        refresh_stale_self_checks(
            stale,
            {**replacements, "runtime.sandbox": changed_definition},
        )


def test_recheck_appends_only_changed_facts_and_transitive_dependents() -> None:
    previous = report(
        result("tool.docker"),
        result("runtime.sandbox", dependencies=("tool.docker",)),
        result("task.destination"),
    )
    observed = report(
        result("tool.docker", checked_at=NOW + timedelta(minutes=1)),
        result(
            "runtime.sandbox",
            dependencies=("tool.docker",),
            checked_at=NOW + timedelta(minutes=1),
        ),
        result("task.destination"),
    )

    reconciled = reconcile_self_check_report(
        previous,
        observed,
        reason="the Docker observation changed",
    )

    assert reconciled is not None
    assert reconciled.revision == 2
    assert reconciled.previous_report_sha256 == previous.sha256
    by_id = {check.id: check for check in reconciled.checks}
    assert by_id["tool.docker"].checked_at == NOW + timedelta(minutes=1)
    assert by_id["runtime.sandbox"].checked_at == NOW + timedelta(minutes=1)
    assert by_id["task.destination"] == previous.checks[2]
    assert by_id["task.destination"].checked_at == NOW
    assert (
        reconcile_self_check_report(
            reconciled,
            reconciled.model_copy(
                update={
                    "revision": 1,
                    "previous_report_sha256": None,
                }
            ),
            reason="nothing changed",
        )
        is None
    )


def test_store_persists_and_verifies_one_write_once_digest_chain(
    tmp_path: Path,
) -> None:
    store = TaskSelfCheckStore(tmp_path / "self-checks")
    first = report(result("task.request"))

    first_path = store.persist(first)
    second = report(
        result("task.request"),
        result(
            "plan.routes",
            dependencies=("task.request",),
            checkpoint=SelfCheckCheckpoint.PLAN_EXECUTION,
        ),
        revision=2,
        previous=first.sha256,
        checkpoint=SelfCheckCheckpoint.PLAN_EXECUTION,
    )
    second_path = store.persist(second)

    assert first_path.name == "0001-task_admission.json"
    assert second_path.name == "0002-plan_execution.json"
    assert store.load_latest(first.run_id) == second
    assert first_path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(SelfCheckError, match="does not extend"):
        store.persist(second.model_copy(update={"revision": 3}))

    payload = json.loads(first_path.read_text(encoding="utf-8"))
    payload["checks"][0]["observed_fact"] = "tampered"
    first_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SelfCheckError, match="digest chain is broken"):
        store.load_latest(first.run_id)


def test_store_rejects_a_symlinked_root(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    root = tmp_path / "self-checks"
    root.symlink_to(target, target_is_directory=True)

    with pytest.raises(SelfCheckError, match="real directory"):
        TaskSelfCheckStore(root).persist(report(result("task.request")))


def test_renderer_keeps_compact_output_actionable_and_detailed_output_complete() -> (
    None
):
    checked = report(
        result("task.request"),
        result("model.context", status=SelfCheckStatus.NEEDS_INPUT),
    )

    compact = render_self_check_report(checked, visibility="compact")
    detailed = render_self_check_report(checked, visibility="detailed")

    assert "task.request" not in compact
    assert "model.context" in compact
    assert "Consequence:" in compact
    assert "Action:" in compact
    assert "task.request" in detailed
    assert "Re-run:" in detailed
    assert not checked.ready
    assert tuple(item.id for item in checked.needs_input) == ("model.context",)
