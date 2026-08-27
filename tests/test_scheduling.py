"""Tests for deterministic run-scoped DAG scheduling."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from threading import Barrier, Lock
from time import sleep

import pytest

from software_agent_team.artifacts import ArtifactKind, ArtifactReference
from software_agent_team.budgets import AgentBudget
from software_agent_team.scheduling import (
    AgentRunOutcome,
    AgentRunStatus,
    DagScheduler,
    ScheduledAgentState,
    ScheduleEvent,
    ScheduleEventKind,
    ScheduleStatus,
)
from software_agent_team.teams import (
    AgentCapability,
    AgentSpec,
    ModelRoute,
    ModelRoutePlan,
    ModelRoutingMode,
    PermissionProfile,
    PlanApprovalSource,
    TeamPlan,
    TeamPlanOrigin,
)

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


def agent(
    agent_id: str,
    capability: AgentCapability,
    *,
    dependencies: tuple[str, ...] = (),
    scope: str = "repository",
    timeout: int = 300,
) -> AgentSpec:
    """Create one internally coherent dynamic AgentSpec."""

    output = {
        AgentCapability.IMPLEMENTATION: ArtifactKind.WORK_RESULT,
        AgentCapability.INTEGRATION: ArtifactKind.WORK_RESULT,
        AgentCapability.TESTING: ArtifactKind.TEST_REPORT,
        AgentCapability.REVIEW: ArtifactKind.REVIEW_REPORT,
    }[capability]
    permission = (
        PermissionProfile.WORKSPACE_WRITE
        if capability in {AgentCapability.IMPLEMENTATION, AgentCapability.INTEGRATION}
        else PermissionProfile.READ_ONLY
    )
    return AgentSpec(
        id=agent_id,
        label=agent_id.replace("_", " ").title(),
        responsibility=f"Perform {capability.value} work.",
        rationale="The approved task requires this responsibility.",
        capability=capability,
        permission_profile=permission,
        stage_id=(
            "implement"
            if capability
            in {AgentCapability.IMPLEMENTATION, AgentCapability.INTEGRATION}
            else "verify"
        ),
        dependencies=dependencies,
        expected_output=output,
        model_route_id="default",
        timeout_seconds=timeout,
        workspace_scope=scope,
    )


def team_plan(
    agents: tuple[AgentSpec, ...],
    *,
    max_concurrency: int,
) -> TeamPlan:
    """Create an approved adaptive TeamPlan for scheduler tests."""

    return TeamPlan(
        plan_id="scheduler-plan-r1",
        revision=1,
        run_id="scheduler-test",
        task_brief_sha256="a" * 64,
        implementation_plan_sha256="b" * 64,
        team_id="adaptive_team",
        origin=TeamPlanOrigin.ADAPTIVE_PLANNING,
        approval_source=PlanApprovalSource.USER,
        created_at=NOW,
        agents=agents,
        model_routes=ModelRoutePlan(
            mode=ModelRoutingMode.STRICT,
            default_route_id="default",
            routes=(ModelRoute(id="default", model="test/provider-model"),),
        ),
        budget=AgentBudget(
            max_calls=max(4, len(agents)),
            max_input_tokens=100_000,
            max_output_tokens=20_000,
            max_agent_duration_seconds=2_000,
            max_estimated_cost_usd="5",
        ),
        iteration_limit=1,
        max_concurrency=max_concurrency,
        independent_review=True,
        revision_enabled=False,
    )


def successful_outcome(spec: AgentSpec) -> AgentRunOutcome:
    """Return one output bound to the Agent's approved artifact kind."""

    return AgentRunOutcome(
        agent_id=spec.id,
        status=AgentRunStatus.COMPLETED,
        output=ArtifactReference(
            kind=spec.expected_output,
            path=(
                f"iterations/01/agents/{spec.id}/"
                f"{spec.expected_output.value.replace('_', '-')}.json"
            ),
            sha256="c" * 64,
        ),
        summary=f"{spec.label} completed its approved work.",
    )


def test_scheduler_obeys_dependencies_and_passes_exact_upstream_results() -> None:
    plan = team_plan(
        (
            agent(
                "feature_builder",
                AgentCapability.IMPLEMENTATION,
                scope="repository/feature",
                timeout=417,
            ),
            agent(
                "quality_auditor",
                AgentCapability.TESTING,
                dependencies=("feature_builder",),
            ),
        ),
        max_concurrency=2,
    )
    observed: list[tuple[str, tuple[str, ...], int]] = []

    def runner(
        spec: AgentSpec,
        upstream: Mapping[str, AgentRunOutcome],
    ) -> AgentRunOutcome:
        observed.append((spec.id, tuple(upstream), spec.timeout_seconds))
        return successful_outcome(spec)

    result = DagScheduler(clock=lambda: NOW).execute(plan, runner)

    assert result.status is ScheduleStatus.COMPLETED
    assert result.run_id == plan.run_id
    assert result.iteration == 1
    assert observed == [
        ("feature_builder", (), 417),
        ("quality_auditor", ("feature_builder",), 300),
    ]
    assert result.completion_order == ("feature_builder", "quality_auditor")
    assert all(
        record.state is ScheduledAgentState.COMPLETED for record in result.records
    )


def test_scheduler_runs_ready_read_only_agents_in_parallel() -> None:
    plan = team_plan(
        (
            agent(
                "feature_builder",
                AgentCapability.IMPLEMENTATION,
                scope="repository/feature",
            ),
            agent(
                "test_auditor",
                AgentCapability.TESTING,
                dependencies=("feature_builder",),
            ),
            agent(
                "review_auditor",
                AgentCapability.REVIEW,
                dependencies=("feature_builder",),
            ),
        ),
        max_concurrency=2,
    )
    quality_barrier = Barrier(2)
    active_quality = 0
    max_active_quality = 0
    lock = Lock()

    def runner(
        spec: AgentSpec,
        upstream: Mapping[str, AgentRunOutcome],
    ) -> AgentRunOutcome:
        nonlocal active_quality, max_active_quality
        if spec.permission_profile is PermissionProfile.READ_ONLY:
            with lock:
                active_quality += 1
                max_active_quality = max(max_active_quality, active_quality)
            quality_barrier.wait(timeout=2)
            with lock:
                active_quality -= 1
        return successful_outcome(spec)

    result = DagScheduler(clock=lambda: NOW).execute(plan, runner)

    assert result.status is ScheduleStatus.COMPLETED
    assert max_active_quality == 2
    assert result.max_observed_concurrency == 2


def test_scheduler_enforces_an_approved_concurrency_cap_of_one() -> None:
    plan = team_plan(
        (
            agent(
                "feature_builder",
                AgentCapability.IMPLEMENTATION,
                scope="repository/feature",
            ),
            agent(
                "test_auditor",
                AgentCapability.TESTING,
                dependencies=("feature_builder",),
            ),
            agent(
                "review_auditor",
                AgentCapability.REVIEW,
                dependencies=("feature_builder",),
            ),
        ),
        max_concurrency=1,
    )

    result = DagScheduler(clock=lambda: NOW).execute(
        plan,
        lambda spec, upstream: successful_outcome(spec),
    )

    assert result.status is ScheduleStatus.COMPLETED
    assert result.max_observed_concurrency == 1
    assert result.completion_order == (
        "feature_builder",
        "test_auditor",
        "review_auditor",
    )


def test_scheduler_serializes_all_shared_git_workspace_writers() -> None:
    plan = team_plan(
        (
            agent(
                "frontend_builder",
                AgentCapability.IMPLEMENTATION,
                scope="repository/frontend",
            ),
            agent(
                "backend_builder",
                AgentCapability.IMPLEMENTATION,
                scope="repository/backend",
            ),
            agent(
                "quality_auditor",
                AgentCapability.TESTING,
                dependencies=("frontend_builder", "backend_builder"),
            ),
        ),
        max_concurrency=2,
    )
    active_writers = 0
    max_active_writers = 0
    lock = Lock()

    def runner(
        spec: AgentSpec,
        upstream: Mapping[str, AgentRunOutcome],
    ) -> AgentRunOutcome:
        nonlocal active_writers, max_active_writers
        if spec.permission_profile is PermissionProfile.WORKSPACE_WRITE:
            with lock:
                active_writers += 1
                max_active_writers = max(max_active_writers, active_writers)
            sleep(0.03)
            with lock:
                active_writers -= 1
        return successful_outcome(spec)

    result = DagScheduler(clock=lambda: NOW).execute(plan, runner)

    assert result.status is ScheduleStatus.COMPLETED
    assert max_active_writers == 1
    assert result.completion_order == (
        "frontend_builder",
        "backend_builder",
        "quality_auditor",
    )
    assert result.max_observed_concurrency == 1


def test_scheduler_stops_new_launches_and_skips_pending_agents_after_failure() -> None:
    plan = team_plan(
        (
            agent(
                "failing_builder",
                AgentCapability.IMPLEMENTATION,
                scope="repository/first",
            ),
            agent(
                "unused_builder",
                AgentCapability.IMPLEMENTATION,
                scope="repository/second",
            ),
            agent(
                "quality_auditor",
                AgentCapability.TESTING,
                dependencies=("failing_builder", "unused_builder"),
            ),
        ),
        max_concurrency=3,
    )
    invoked: list[str] = []

    def runner(
        spec: AgentSpec,
        upstream: Mapping[str, AgentRunOutcome],
    ) -> AgentRunOutcome:
        invoked.append(spec.id)
        return AgentRunOutcome(
            agent_id=spec.id,
            status=AgentRunStatus.FAILED,
            summary="The implementation failed.",
            error="reproducible failure",
        )

    result = DagScheduler(clock=lambda: NOW).execute(plan, runner)

    assert result.status is ScheduleStatus.FAILED
    assert result.failed_agent_id == "failing_builder"
    assert invoked == ["failing_builder"]
    assert [record.state for record in result.records] == [
        ScheduledAgentState.FAILED,
        ScheduledAgentState.SKIPPED,
        ScheduledAgentState.SKIPPED,
    ]
    assert [event.kind for event in result.events][-2:] == [
        ScheduleEventKind.AGENT_SKIPPED,
        ScheduleEventKind.AGENT_SKIPPED,
    ]


def test_scheduler_rejects_runner_identity_and_output_contract_mismatches() -> None:
    plan = team_plan(
        (
            agent(
                "feature_builder",
                AgentCapability.IMPLEMENTATION,
                scope="repository/feature",
            ),
            agent(
                "quality_auditor",
                AgentCapability.TESTING,
                dependencies=("feature_builder",),
            ),
        ),
        max_concurrency=1,
    )

    def runner(
        spec: AgentSpec,
        upstream: Mapping[str, AgentRunOutcome],
    ) -> AgentRunOutcome:
        outcome = successful_outcome(spec)
        return outcome.model_copy(update={"agent_id": "another_agent"})

    result = DagScheduler(clock=lambda: NOW).execute(plan, runner)

    assert result.status is ScheduleStatus.FAILED
    assert result.records[0].state is ScheduledAgentState.FAILED
    assert "different Agent ID" in (result.records[0].error or "")
    assert result.records[1].state is ScheduledAgentState.SKIPPED

    def wrong_output_runner(
        spec: AgentSpec,
        upstream: Mapping[str, AgentRunOutcome],
    ) -> AgentRunOutcome:
        outcome = successful_outcome(spec)
        assert outcome.output is not None
        return outcome.model_copy(
            update={
                "output": outcome.output.model_copy(
                    update={"kind": ArtifactKind.REVIEW_REPORT}
                )
            }
        )

    wrong_output = DagScheduler(clock=lambda: NOW).execute(plan, wrong_output_runner)

    assert wrong_output.status is ScheduleStatus.FAILED
    assert "output kind" in (wrong_output.records[0].error or "")


def test_scheduler_emits_ordered_observer_events() -> None:
    plan = team_plan(
        (
            agent(
                "feature_builder",
                AgentCapability.IMPLEMENTATION,
                scope="repository/feature",
            ),
            agent(
                "quality_auditor",
                AgentCapability.TESTING,
                dependencies=("feature_builder",),
            ),
        ),
        max_concurrency=1,
    )
    observed: list[ScheduleEvent] = []

    result = DagScheduler(
        clock=lambda: NOW,
        observer=observed.append,
    ).execute(plan, lambda spec, upstream: successful_outcome(spec))

    assert observed == list(result.events)
    assert [event.sequence for event in observed] == list(range(1, 9))
    assert [event.kind for event in observed] == [
        ScheduleEventKind.AGENT_QUEUED,
        ScheduleEventKind.AGENT_QUEUED,
        ScheduleEventKind.AGENT_READY,
        ScheduleEventKind.AGENT_STARTED,
        ScheduleEventKind.AGENT_COMPLETED,
        ScheduleEventKind.AGENT_READY,
        ScheduleEventKind.AGENT_STARTED,
        ScheduleEventKind.AGENT_COMPLETED,
    ]
    assert [event.active_count for event in observed] == [0, 0, 0, 1, 0, 0, 1, 0]
    assert [event.duration_ms is not None for event in observed] == [
        False,
        False,
        False,
        False,
        True,
        False,
        False,
        True,
    ]


def test_scheduler_rejects_an_iteration_outside_the_approved_plan() -> None:
    plan = team_plan(
        (
            agent(
                "feature_builder",
                AgentCapability.IMPLEMENTATION,
                scope="repository/feature",
            ),
            agent(
                "quality_auditor",
                AgentCapability.TESTING,
                dependencies=("feature_builder",),
            ),
        ),
        max_concurrency=1,
    )

    with pytest.raises(ValueError, match="iteration"):
        DagScheduler(clock=lambda: NOW).execute(
            plan,
            lambda spec, upstream: successful_outcome(spec),
            iteration=2,
        )


def test_scheduler_bounds_multiline_agent_summaries_before_observer_delivery() -> None:
    plan = team_plan(
        (
            agent(
                "feature_builder",
                AgentCapability.IMPLEMENTATION,
                scope="repository/feature",
            ),
            agent(
                "quality_auditor",
                AgentCapability.TESTING,
                dependencies=("feature_builder",),
            ),
        ),
        max_concurrency=1,
    )

    def runner(
        spec: AgentSpec,
        upstream: Mapping[str, AgentRunOutcome],
    ) -> AgentRunOutcome:
        del upstream
        return successful_outcome(spec).model_copy(
            update={"summary": "first line\n" + "x" * 1000}
        )

    result = DagScheduler(clock=lambda: NOW).execute(plan, runner)
    completed = next(
        event
        for event in result.events
        if event.kind is ScheduleEventKind.AGENT_COMPLETED
    )

    assert len(completed.message) == 500
    assert "\n" not in completed.message
