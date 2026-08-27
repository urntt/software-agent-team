"""Behavior tests for controller-applied dynamic-runtime controls."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from software_agent_team.artifacts import ArtifactKind
from software_agent_team.budgets import AgentBudget
from software_agent_team.control_console import (
    ControlConsoleError,
    submit_control_line,
)
from software_agent_team.controls import (
    ControlApplicationBoundary,
    ControlCommandStatus,
    ControlCommandStore,
    ControlCommandType,
    ControlTarget,
    ControlTargetKind,
)
from software_agent_team.progress import ProgressEvent, ProgressEventKind
from software_agent_team.run_control import RunPhase
from software_agent_team.runtime_controls import (
    RuntimeControlChannel,
    RuntimeControlDecision,
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

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)


def _agent(
    agent_id: str,
    capability: AgentCapability,
    *,
    dependencies: tuple[str, ...] = (),
) -> AgentSpec:
    output = {
        AgentCapability.IMPLEMENTATION: ArtifactKind.WORK_RESULT,
        AgentCapability.TESTING: ArtifactKind.TEST_REPORT,
    }[capability]
    return AgentSpec(
        id=agent_id,
        label=agent_id.replace("_", " ").title(),
        responsibility=f"Perform {capability.value} work.",
        rationale="The approved plan requires this responsibility.",
        capability=capability,
        permission_profile=(
            PermissionProfile.WORKSPACE_WRITE
            if capability is AgentCapability.IMPLEMENTATION
            else PermissionProfile.READ_ONLY
        ),
        stage_id=(
            "implement" if capability is AgentCapability.IMPLEMENTATION else "verify"
        ),
        dependencies=dependencies,
        expected_output=output,
        model_route_id="default",
        timeout_seconds=300,
        workspace_scope="repository",
    )


def _plan() -> TeamPlan:
    return TeamPlan(
        plan_id="control-plan-r1",
        revision=1,
        run_id="control-run",
        task_brief_sha256="a" * 64,
        implementation_plan_sha256="b" * 64,
        team_id="adaptive_team",
        origin=TeamPlanOrigin.ADAPTIVE_PLANNING,
        approval_source=PlanApprovalSource.USER,
        created_at=NOW,
        agents=(
            _agent("feature_builder", AgentCapability.IMPLEMENTATION),
            _agent(
                "quality_auditor",
                AgentCapability.TESTING,
                dependencies=("feature_builder",),
            ),
        ),
        model_routes=ModelRoutePlan(
            mode=ModelRoutingMode.STRICT,
            default_route_id="default",
            routes=(ModelRoute(id="default", model="test/model"),),
        ),
        budget=AgentBudget(
            max_calls=4,
            max_input_tokens=100_000,
            max_output_tokens=20_000,
            max_agent_duration_seconds=2_000,
            max_estimated_cost_usd="5",
        ),
        iteration_limit=1,
        max_concurrency=2,
        independent_review=True,
        revision_enabled=False,
    )


def _channel(
    tmp_path: Path,
    *,
    interrupt_agent=lambda agent_id: 0,
    interrupt_all=lambda: 0,
) -> tuple[ControlCommandStore, RuntimeControlChannel, list[ProgressEvent]]:
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    store = ControlCommandStore(run_directory, run_id="control-run", clock=lambda: NOW)
    events: list[ProgressEvent] = []
    channel = RuntimeControlChannel(
        store=store,
        team_plan=_plan(),
        interrupt_agent=interrupt_agent,
        interrupt_all=interrupt_all,
        phase_reader=lambda: RunPhase.IMPLEMENTING,
        event_handler=events.append,
    )
    return store, channel, events


def test_guidance_is_applied_prospectively_and_consumed_once(tmp_path: Path) -> None:
    store, channel, events = _channel(tmp_path)
    requested = store.request(
        command=ControlCommandType.GUIDE,
        target=ControlTarget(kind=ControlTargetKind.FUTURE_WORK),
        application_boundary=ControlApplicationBoundary.BEFORE_NEXT_INVOCATION,
        instruction="Keep the public interface small.",
        command_id="ctl-guide-runtime",
    )

    decision = channel.poll(
        active_agent_ids=(),
        pending_agent_ids=("feature_builder", "quality_auditor"),
    )

    assert decision is RuntimeControlDecision.CONTINUE
    assert store.load(requested.command_id)[-1].status is ControlCommandStatus.APPLIED
    builder_guidance = channel.consume_guidance("feature_builder")
    auditor_guidance = channel.consume_guidance("quality_auditor")
    assert builder_guidance[0].instruction == "Keep the public interface small."
    assert auditor_guidance == builder_guidance
    assert channel.consume_guidance("feature_builder") == ()
    assert [event.kind for event in events] == [
        ProgressEventKind.CONTROL_RECEIVED,
        ProgressEventKind.CONTROL_APPLIED,
    ]
    assert events[0].references[0].path == "controls/ctl-guide-runtime/000001.json"
    assert events[1].references[0].path == "controls/ctl-guide-runtime/000002.json"


def test_pause_waits_for_a_safe_checkpoint_and_resume_can_withdraw_it(
    tmp_path: Path,
) -> None:
    store, channel, _ = _channel(tmp_path)
    pause = store.request(
        command=ControlCommandType.PAUSE,
        target=ControlTarget(kind=ControlTargetKind.RUN),
        application_boundary=ControlApplicationBoundary.NEXT_SAFE_CHECKPOINT,
        command_id="ctl-pause-runtime",
    )

    assert (
        channel.poll(
            active_agent_ids=("feature_builder",),
            pending_agent_ids=("quality_auditor",),
        )
        is RuntimeControlDecision.HOLD
    )
    assert store.load(pause.command_id)[-1].status is ControlCommandStatus.QUEUED

    resume = store.request(
        command=ControlCommandType.RESUME,
        target=ControlTarget(kind=ControlTargetKind.RUN),
        application_boundary=ControlApplicationBoundary.NEXT_SAFE_CHECKPOINT,
        command_id="ctl-resume-runtime",
    )
    assert (
        channel.poll(
            active_agent_ids=("feature_builder",),
            pending_agent_ids=("quality_auditor",),
        )
        is RuntimeControlDecision.CONTINUE
    )
    assert store.load(pause.command_id)[-1].status is (ControlCommandStatus.SUPERSEDED)
    assert store.load(resume.command_id)[-1].status is ControlCommandStatus.APPLIED


def test_pause_applies_only_after_active_work_finishes(tmp_path: Path) -> None:
    store, channel, events = _channel(tmp_path)
    pause = store.request(
        command=ControlCommandType.PAUSE,
        target=ControlTarget(kind=ControlTargetKind.RUN),
        application_boundary=ControlApplicationBoundary.NEXT_SAFE_CHECKPOINT,
        command_id="ctl-pause-safe",
    )

    assert (
        channel.poll(
            active_agent_ids=("feature_builder",),
            pending_agent_ids=("quality_auditor",),
        )
        is RuntimeControlDecision.HOLD
    )
    assert (
        channel.poll(
            active_agent_ids=(),
            pending_agent_ids=("quality_auditor",),
        )
        is RuntimeControlDecision.HOLD
    )
    assert channel.paused
    assert store.load(pause.command_id)[-1].status is ControlCommandStatus.APPLIED
    assert [
        event.agent_id
        for event in events
        if event.kind is ProgressEventKind.AGENT_PAUSED
    ] == ["quality_auditor"]

    store.request(
        command=ControlCommandType.RESUME,
        target=ControlTarget(kind=ControlTargetKind.RUN),
        application_boundary=ControlApplicationBoundary.NEXT_SAFE_CHECKPOINT,
        command_id="ctl-resume-safe",
    )
    assert (
        channel.poll(
            active_agent_ids=(),
            pending_agent_ids=("quality_auditor",),
        )
        is RuntimeControlDecision.CONTINUE
    )
    assert not channel.paused
    assert [
        event.agent_id
        for event in events
        if event.kind is ProgressEventKind.AGENT_RESUMED
    ] == ["quality_auditor"]


def test_interrupt_retries_registration_and_records_provider_cost_caveat(
    tmp_path: Path,
) -> None:
    attempts = iter((0, 1))
    store, channel, _ = _channel(
        tmp_path,
        interrupt_agent=lambda agent_id: next(attempts),
    )
    interrupt = store.request(
        command=ControlCommandType.INTERRUPT,
        target=ControlTarget(
            kind=ControlTargetKind.AGENT,
            agent_id="feature_builder",
        ),
        application_boundary=ControlApplicationBoundary.IMMEDIATE,
        command_id="ctl-interrupt-runtime",
    )

    for _ in range(2):
        assert (
            channel.poll(
                active_agent_ids=("feature_builder",),
                pending_agent_ids=("quality_auditor",),
            )
            is RuntimeControlDecision.CONTINUE
        )

    resolved = store.load(interrupt.command_id)[-1]
    assert resolved.status is ControlCommandStatus.APPLIED
    assert resolved.provider_cost_caveat is not None


def test_correction_stops_launches_then_resolves_at_a_safe_checkpoint(
    tmp_path: Path,
) -> None:
    store, channel, _ = _channel(tmp_path)
    correction = store.request(
        command=ControlCommandType.CORRECT,
        target=ControlTarget(kind=ControlTargetKind.RUN),
        application_boundary=ControlApplicationBoundary.PLANNING_REVISION,
        instruction="Use a CLI instead of a Web interface.",
        command_id="ctl-correct-runtime",
    )

    assert (
        channel.poll(
            active_agent_ids=("feature_builder",),
            pending_agent_ids=("quality_auditor",),
        )
        is RuntimeControlDecision.CORRECT
    )
    assert store.load(correction.command_id)[-1].status is ControlCommandStatus.QUEUED
    assert (
        channel.poll(
            active_agent_ids=(),
            pending_agent_ids=("quality_auditor",),
        )
        is RuntimeControlDecision.CORRECT
    )
    assert store.load(correction.command_id)[-1].status is ControlCommandStatus.APPLIED
    assert channel.correction_instruction == "Use a CLI instead of a Web interface."


def test_cancel_is_terminal_and_interrupts_only_active_owned_calls(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    store, channel, _ = _channel(
        tmp_path,
        interrupt_all=lambda: calls.append("interrupt_all") or 1,
    )
    cancel = store.request(
        command=ControlCommandType.CANCEL,
        target=ControlTarget(kind=ControlTargetKind.RUN),
        application_boundary=ControlApplicationBoundary.IMMEDIATE,
        command_id="ctl-cancel-runtime",
    )

    assert (
        channel.poll(
            active_agent_ids=("feature_builder",),
            pending_agent_ids=("quality_auditor",),
        )
        is RuntimeControlDecision.CANCEL
    )
    resolved = store.load(cancel.command_id)[-1]
    assert resolved.status is ControlCommandStatus.APPLIED
    assert resolved.provider_cost_caveat is not None
    assert calls


def test_plain_language_console_queues_typed_controls_without_internal_files(
    tmp_path: Path,
) -> None:
    store, _, _ = _channel(tmp_path)
    plan = _plan()

    guide_message = submit_control_line(
        "/guide feature_builder Prefer standard-library dependencies.",
        store=store,
        team_plan=plan,
    )
    correct_message = submit_control_line(
        "/correct The output must also support JSON.",
        store=store,
        team_plan=plan,
    )

    latest = store.list_latest()
    by_command = {item.command: item for item in latest}
    assert set(by_command) == {
        ControlCommandType.GUIDE,
        ControlCommandType.CORRECT,
    }
    assert "Queued guide" in guide_message
    assert "Queued correct" in correct_message
    assert by_command[ControlCommandType.GUIDE].target.agent_id == "feature_builder"
    assert (
        by_command[ControlCommandType.CORRECT].instruction
        == "The output must also support JSON."
    )


def test_console_requires_confirmation_for_terminal_cancellation(
    tmp_path: Path,
) -> None:
    store, _, _ = _channel(tmp_path)
    plan = _plan()

    warning = submit_control_line(
        "/cancel",
        store=store,
        team_plan=plan,
    )
    assert "terminal" in warning
    assert store.list_latest() == ()

    queued = submit_control_line(
        "/cancel confirm",
        store=store,
        team_plan=plan,
    )
    assert "Queued cancel" in queued
    assert store.list_latest()[0].command is ControlCommandType.CANCEL


def test_console_changes_visibility_without_changing_execution(tmp_path: Path) -> None:
    store, _, _ = _channel(tmp_path)
    selected: list[str] = []

    message = submit_control_line(
        "/visibility detailed",
        store=store,
        team_plan=_plan(),
        visibility_handler=selected.append,
    )

    assert selected == ["detailed"]
    assert "Execution was not changed" in message
    assert store.list_latest() == ()


def test_console_rejects_unknown_agents_and_non_control_input(tmp_path: Path) -> None:
    store, _, _ = _channel(tmp_path)
    plan = _plan()

    with pytest.raises(ControlConsoleError, match="Unknown Agent"):
        submit_control_line(
            "/interrupt missing_agent",
            store=store,
            team_plan=plan,
        )
    with pytest.raises(ControlConsoleError, match="begin with"):
        submit_control_line(
            "pause",
            store=store,
            team_plan=plan,
        )
