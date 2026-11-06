"""Dynamic workflow DAG scheduling, plan execution, and artifact verification."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from software_agent_team.artifact_store import ArtifactStore
from software_agent_team.artifacts import (
    ArtifactReference,
    CheckStatus,
    IterationDecision,
    IterationRecord,
    ReviewReport,
    ReviewVerdict,
    TestReport,
    WorkResult,
)
from software_agent_team.dynamic_runner import (
    DynamicAgentRunner,
    DynamicQualityGate,
)
from software_agent_team.dynamic_workflow.types import (
    DynamicWorkflowError,
    DynamicWorkflowOutcome,
    _DynamicWorkflowContext,
    _safe_event_summary,
    _unique_references,
    _utc,
)
from software_agent_team.git_workspace import (
    GitSnapshot,
    GitWorkspace,
    GitWorkspaceManager,
)
from software_agent_team.integrity import canonical_model_sha256
from software_agent_team.progress import (
    ProgressEvent,
    ProgressEventKind,
    RunEvent,
    RunEventReference,
    RunEventReferenceKind,
    RunEventSource,
)
from software_agent_team.prompting import DynamicRevisionFeedback
from software_agent_team.run_control import (
    RunPhase,
    RunRecord,
    TerminationReason,
)
from software_agent_team.runtime_controls import RuntimeControlDecision
from software_agent_team.scheduling import (
    DagScheduler,
    DagScheduleResult,
    ScheduleEvent,
    ScheduleEventKind,
    ScheduleStatus,
)
from software_agent_team.teams import AgentCapability, TeamPlan

if TYPE_CHECKING:
    from software_agent_team.dynamic_workflow.coordinator import (
        DynamicWorkflowCoordinator,
    )


def _event_digest(event: RunEvent) -> str:
    """Compute canonical SHA256 digest of a progress run event."""
    return canonical_model_sha256(event)


def _references_for_capabilities(
    team_plan: TeamPlan,
    outputs: Mapping[str, ArtifactReference],
    capabilities: set[AgentCapability],
    *,
    agent_order: tuple[str, ...] | None = None,
    allow_missing: bool = False,
) -> tuple[ArtifactReference, ...]:
    """Extract and validate output artifact references matching given capabilities."""
    selected_ids = {
        agent.id for agent in team_plan.agents if agent.capability in capabilities
    }
    ordered_ids = (
        tuple(agent.id for agent in team_plan.agents)
        if agent_order is None
        else agent_order
    )
    if not allow_missing and not selected_ids.issubset(ordered_ids):
        raise DynamicWorkflowError(
            "completed schedule omitted a required Agent completion"
        )
    selected: list[ArtifactReference] = []
    for agent_id in ordered_ids:
        if agent_id not in selected_ids:
            continue
        reference = outputs.get(agent_id)
        if reference is None:
            if allow_missing:
                continue
            raise DynamicWorkflowError(
                f"completed schedule omitted output from Agent {agent_id}"
            )
        selected.append(reference)
    return tuple(selected)


def _load_artifacts[ArtifactT: WorkResult | TestReport | ReviewReport](
    store: ArtifactStore,
    references: tuple[ArtifactReference, ...],
    model: type[ArtifactT],
) -> tuple[ArtifactT, ...]:
    """Load and type-validate stored artifacts against expected model."""
    loaded: list[ArtifactT] = []
    for reference in references:
        artifact = store.load(reference)
        if not isinstance(artifact, model):
            raise DynamicWorkflowError(
                f"artifact {reference.path} has the wrong runtime type"
            )
        loaded.append(artifact)
    return tuple(loaded)


def _validate_work_chain(
    store: ArtifactStore,
    references: tuple[ArtifactReference, ...],
    snapshot: GitSnapshot,
) -> None:
    """Verify that writer work results form a contiguous git commit chain."""
    works = _load_artifacts(
        store,
        references,
        WorkResult,
    )
    expected = snapshot.input_commit
    for work in works:
        if work.input_commit != expected:
            raise DynamicWorkflowError(
                "dynamic WorkResults do not form the scheduler commit chain"
            )
        expected = work.output_commit
    if expected != snapshot.output_commit:
        raise DynamicWorkflowError(
            "dynamic writer chain differs from the aggregate Git snapshot"
        )


def _capture_runner_outputs(
    context: _DynamicWorkflowContext,
    runner: DynamicAgentRunner,
) -> None:
    """Extract and categorize runner outputs into context state and history."""
    works: list[WorkResult] = []
    tests: list[TestReport] = []
    reviews: list[ReviewReport] = []
    for reference in runner.outputs.values():
        artifact = context.artifact_store.load(reference)
        if isinstance(artifact, WorkResult):
            works.append(artifact)
        elif isinstance(artifact, TestReport):
            tests.append(artifact)
        elif isinstance(artifact, ReviewReport):
            reviews.append(artifact)
    if runner.controller_test_reference is not None:
        artifact = context.artifact_store.load(runner.controller_test_reference)
        if isinstance(artifact, TestReport):
            tests.append(artifact)
    context.last_works = tuple(works) or context.last_works
    context.last_tests = tuple(tests) or context.last_tests
    context.last_reviews = tuple(reviews) or context.last_reviews
    known_reviews = {
        (review.producer, review.iteration, review.input_commit)
        for review in context.review_history
    }
    for review in sorted(
        reviews,
        key=lambda item: (item.iteration, item.producer, item.input_commit),
    ):
        identity = (review.producer, review.iteration, review.input_commit)
        if identity in known_reviews:
            continue
        context.review_history.append(review)
        known_reviews.add(identity)


def _execute_approved_plan(
    coordinator: DynamicWorkflowCoordinator,
    context: _DynamicWorkflowContext,
    record: RunRecord,
    workspace: GitWorkspace,
    workspace_manager: GitWorkspaceManager,
    quality_gate: DynamicQualityGate,
    review_scope_by_agent: Mapping[str, tuple[str, ...]] | None,
) -> DynamicWorkflowOutcome:
    """Execute iterations of the approved adaptive plan until convergence."""
    team_plan = context.approved.team_plan
    plan_digest = team_plan.implementation_plan_sha256
    assert plan_digest is not None
    record = context.controller.advance(
        record.run_id,
        expected_revision=record.revision,
        target=RunPhase.IMPLEMENTING,
        reason="user-approved adaptive implementation plan is ready",
        implementation_plan_sha256=plan_digest,
    )

    revision_feedback: DynamicRevisionFeedback | None = None
    previous_signature: (
        tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]] | None
    ) = None
    previous_blocking_ids: tuple[str, ...] = ()
    while True:
        input_commit = record.current_commit
        if input_commit is None:
            raise DynamicWorkflowError("implementation input commit is missing")
        runner = DynamicAgentRunner(
            task_brief=context.approved.task_brief,
            implementation_plan=context.approved.implementation_plan,
            team_plan=team_plan,
            workspace=workspace,
            workspace_manager=workspace_manager,
            artifact_store=context.artifact_store,
            executor=coordinator.executor,
            quality_gate=quality_gate,
            budget_ledger=context.budget_ledger,
            pricing_by_model=coordinator.pricing_by_model,
            manual_review_criteria=coordinator.manual_review_criteria,
            review_scope_by_agent=review_scope_by_agent,
            iteration=record.current_iteration,
            input_commit=input_commit,
            artifact_repair_limit=coordinator.artifact_repair_limit,
            revision_feedback=revision_feedback,
            revision_command_evidence=(
                context.last_tests[0].commands
                if revision_feedback is not None and context.last_tests
                else ()
            ),
            guidance_provider=context.control_channel.consume_guidance,
            invocation_stop_provider=(context.control_channel.invocation_stop_reason),
            activity_handler=lambda event: coordinator._emit(context, event),
            clock=coordinator.clock,
        )
        snapshots: list[GitSnapshot] = []
        completed_agents: list[str] = []

        def observe(
            event: ScheduleEvent,
            *,
            current_runner: DynamicAgentRunner = runner,
            current_snapshots: list[GitSnapshot] = snapshots,
            current_completed_agents: list[str] = completed_agents,
            current_input_commit: str = input_commit,
        ) -> None:
            nonlocal record
            agent = team_plan.get_agent(event.agent_id)
            if event.kind is ScheduleEventKind.AGENT_COMPLETED:
                current_completed_agents.append(agent.id)
            is_quality = agent.capability in {
                AgentCapability.TESTING,
                AgentCapability.REVIEW,
            }
            if (
                event.kind is ScheduleEventKind.AGENT_STARTED
                and is_quality
                and not current_snapshots
            ):
                work_references = _references_for_capabilities(
                    team_plan,
                    current_runner.outputs,
                    {AgentCapability.IMPLEMENTATION, AgentCapability.INTEGRATION},
                    agent_order=tuple(current_completed_agents),
                )
                snapshot = workspace_manager.verify_snapshot(
                    workspace,
                    iteration=record.current_iteration,
                    input_commit=current_input_commit,
                )
                _validate_work_chain(
                    context.artifact_store,
                    work_references,
                    snapshot,
                )
                record = context.controller.advance(
                    record.run_id,
                    expected_revision=record.revision,
                    target=RunPhase.SNAPSHOTTING,
                    reason="all approved writers produced a verified commit chain",
                    artifacts=work_references,
                )
                record = context.controller.record_snapshot(
                    record.run_id,
                    expected_revision=record.revision,
                    snapshot=snapshot,
                )
                current_snapshots.append(snapshot)
                coordinator._emit(
                    context,
                    ProgressEvent(
                        kind=ProgressEventKind.SNAPSHOT_VERIFIED,
                        message=(
                            "Git snapshot verified: "
                            f"{len(snapshot.changed_files)} files changed"
                        ),
                        phase=RunPhase.VERIFYING,
                        iteration=record.current_iteration,
                        changed_files=snapshot.changed_files,
                    ),
                )
                coordinator._emit(
                    context,
                    ProgressEvent(
                        kind=ProgressEventKind.QUALITY_GATES_STARTED,
                        message="Running deterministic quality gates",
                        phase=RunPhase.VERIFYING,
                        iteration=record.current_iteration,
                    ),
                )
            event_kind = {
                ScheduleEventKind.AGENT_QUEUED: ProgressEventKind.AGENT_QUEUED,
                ScheduleEventKind.AGENT_READY: ProgressEventKind.AGENT_READY,
                ScheduleEventKind.AGENT_STARTED: ProgressEventKind.AGENT_STARTED,
                ScheduleEventKind.AGENT_COMPLETED: (ProgressEventKind.AGENT_COMPLETED),
                ScheduleEventKind.AGENT_FAILED: ProgressEventKind.AGENT_FAILED,
                ScheduleEventKind.AGENT_SKIPPED: ProgressEventKind.AGENT_SKIPPED,
                ScheduleEventKind.AGENT_INTERRUPTED: (
                    ProgressEventKind.AGENT_INTERRUPTED
                ),
                ScheduleEventKind.AGENT_CANCELLED: (ProgressEventKind.AGENT_CANCELLED),
            }[event.kind]
            route = team_plan.model_routes.get_route(agent.model_route_id)
            executed = event.kind in {
                ScheduleEventKind.AGENT_STARTED,
                ScheduleEventKind.AGENT_COMPLETED,
                ScheduleEventKind.AGENT_FAILED,
                ScheduleEventKind.AGENT_INTERRUPTED,
            }
            message = _safe_event_summary(
                event.message,
                fallback=f"{agent.label} changed scheduler state",
            )
            source = (
                RunEventSource.AGENT_SAFE_SUMMARY
                if event.kind
                in {
                    ScheduleEventKind.AGENT_COMPLETED,
                    ScheduleEventKind.AGENT_FAILED,
                }
                else RunEventSource.CONTROLLER
            )
            coordinator._emit(
                context,
                ProgressEvent(
                    kind=event_kind,
                    message=message,
                    agent_id=agent.id,
                    iteration=record.current_iteration,
                    attempt=1 if executed else None,
                    duration_ms=event.duration_ms,
                    capability=agent.capability.value,
                    specialization=agent.specialization.value,
                    stage_id=agent.stage_id,
                    model=route.model,
                    dependency_ids=agent.dependencies,
                    references=(
                        RunEventReference(
                            kind=RunEventReferenceKind.MODEL_ROUTE,
                            id=agent.model_route_id,
                        ),
                    ),
                    source=source,
                ),
            )

        schedule: DagScheduleResult | None = None
        try:
            schedule = DagScheduler(
                clock=coordinator.clock,
                observer=observe,
                control_channel=context.control_channel,
            ).execute(
                team_plan,
                runner,
                iteration=record.current_iteration,
            )
        finally:
            context.execution_records.extend(runner.execution_records)
            context.handoffs.extend(runner.handoffs)
            context.execution_records[:] = list(
                _unique_references(context.execution_records)
            )
            context.handoffs[:] = list(_unique_references(context.handoffs))
            _capture_runner_outputs(context, runner)

        assert schedule is not None
        context.schedules.append(schedule)
        if schedule.status is ScheduleStatus.CANCELLED:
            return coordinator._stop_by_user(
                context,
                record,
                reason=TerminationReason.USER_CANCELLED,
                detail="The user cancelled the run.",
                control_stop=RuntimeControlDecision.CANCEL,
            )
        if schedule.status is ScheduleStatus.CORRECTION_REQUESTED:
            instruction = context.control_channel.correction_instruction
            if instruction is None:
                raise DynamicWorkflowError(
                    "correction stop omitted its replacement instruction"
                )
            return coordinator._stop_by_user(
                context,
                record,
                reason=TerminationReason.USER_CORRECTION_REQUESTED,
                detail="The user requested replacement Planning.",
                control_stop=RuntimeControlDecision.CORRECT,
                correction_instruction=instruction,
            )
        if schedule.status is ScheduleStatus.FAILED:
            if (
                context.last_tests
                and context.last_tests[0].iteration == record.current_iteration
            ):
                context.command_evidence.extend(context.last_tests[0].commands)
            failed_id = schedule.failed_agent_id
            assert failed_id is not None
            failed_record = next(
                item for item in schedule.records if item.agent_id == failed_id
            )
            return coordinator._fail(
                context,
                record,
                reason=runner.termination_reasons.get(
                    failed_id,
                    TerminationReason.EXECUTION_FAILED,
                ),
                detail=failed_record.error or failed_record.summary,
            )
        if schedule.status not in {
            ScheduleStatus.COMPLETED,
            ScheduleStatus.QUALITY_DECISION_REQUIRED,
        }:
            raise DynamicWorkflowError(
                f"unsupported dynamic schedule status: {schedule.status.value}"
            )
        if len(snapshots) != 1 or record.phase is not RunPhase.VERIFYING:
            raise DynamicWorkflowError(
                "completed dynamic schedule did not cross one snapshot boundary"
            )
        snapshot = snapshots[0]
        outputs = runner.outputs
        work_references = _references_for_capabilities(
            team_plan,
            outputs,
            {AgentCapability.IMPLEMENTATION, AgentCapability.INTEGRATION},
            agent_order=schedule.completion_order,
        )
        test_references = _references_for_capabilities(
            team_plan,
            outputs,
            {AgentCapability.TESTING},
            allow_missing=(schedule.status is ScheduleStatus.QUALITY_DECISION_REQUIRED),
        )
        if not test_references:
            controller_test = runner.controller_test_reference
            if controller_test is None:
                raise DynamicWorkflowError(
                    "dynamic quality pass produced no deterministic TestReport"
                )
            test_references = (controller_test,)
        review_references = _references_for_capabilities(
            team_plan,
            outputs,
            {AgentCapability.REVIEW},
            allow_missing=(schedule.status is ScheduleStatus.QUALITY_DECISION_REQUIRED),
        )
        works = _load_artifacts(
            context.artifact_store,
            work_references,
            WorkResult,
        )
        tests = _load_artifacts(
            context.artifact_store,
            test_references,
            TestReport,
        )
        reviews = _load_artifacts(
            context.artifact_store,
            review_references,
            ReviewReport,
        )
        context.last_works = works
        context.last_tests = tests
        context.last_reviews = reviews
        context.command_evidence.extend(tests[0].commands)

        record = context.controller.advance(
            record.run_id,
            expected_revision=record.revision,
            target=RunPhase.REVIEWING,
            reason="deterministic test evidence is recorded",
            artifacts=test_references,
        )
        decision = coordinator._decide(
            tests,
            reviews,
            iteration=record.current_iteration,
            iteration_limit=record.iteration_limit,
        )
        blocking_findings = tuple(
            finding
            for review in reviews
            for finding in review.findings
            if finding.blocking
        )
        blocking_ids = tuple(finding.id for finding in blocking_findings)
        blocking_reasons = coordinator._blocking_reasons(tests)
        signature = (
            tuple(sorted(blocking_ids)),
            tuple(sorted(finding.description for finding in blocking_findings)),
            tuple(sorted(blocking_reasons)),
        )
        correctable_nonacceptance = (
            not any(test.status is CheckStatus.BLOCKED for test in tests)
            and not any(review.verdict is ReviewVerdict.FAIL for review in reviews)
            and not (
                all(test.status is CheckStatus.PASSED for test in tests)
                and all(review.verdict is ReviewVerdict.ACCEPT for review in reviews)
            )
        )
        repeated = (
            correctable_nonacceptance
            and previous_signature is not None
            and signature == previous_signature
        )
        if repeated:
            decision = IterationDecision.FAIL
        resolved_ids = tuple(
            identifier
            for identifier in previous_blocking_ids
            if identifier not in set(blocking_ids)
        )
        summary = coordinator._decision_summary(decision, tests, reviews)
        iteration_record = IterationRecord(
            run_id=record.run_id,
            team_id=record.team_id,
            created_at=_utc(coordinator.clock),
            iteration=record.current_iteration,
            input_commit=snapshot.input_commit,
            output_commit=snapshot.output_commit,
            implementation_plan_sha256=plan_digest,
            work_results=work_references,
            test_reports=test_references,
            review_reports=review_references,
            decision=decision,
            blocking_finding_ids=blocking_ids,
            blocking_reasons=blocking_reasons,
            resolved_finding_ids=resolved_ids,
            summary=summary,
        )
        iteration_reference = context.artifact_store.write(
            iteration_record,
            description="Controller decision for this immutable iteration.",
        )
        context.iteration_records.append(iteration_reference)
        record = context.controller.advance(
            record.run_id,
            expected_revision=record.revision,
            target=RunPhase.DECIDING,
            reason="independent quality and controller decision are recorded",
            artifacts=(*review_references, iteration_reference),
        )
        coordinator._emit(
            context,
            ProgressEvent(
                kind=ProgressEventKind.DECISION_RECORDED,
                message=(
                    f"Iteration {record.current_iteration} decision: {decision.value}"
                ),
                phase=RunPhase.DECIDING,
                iteration=record.current_iteration,
                decision=decision,
            ),
        )

        if decision is IterationDecision.ACCEPT:
            record = context.controller.advance(
                record.run_id,
                expected_revision=record.revision,
                target=RunPhase.DELIVERING,
                reason="all acceptance evidence passed",
                decision=IterationDecision.ACCEPT,
            )
            return coordinator._complete(context, record, tests, reviews)
        if decision is IterationDecision.FAIL:
            return coordinator._fail(
                context,
                record,
                reason=coordinator._decision_termination_reason(
                    tests,
                    reviews,
                    iteration=record.current_iteration,
                    iteration_limit=record.iteration_limit,
                    repeated=repeated,
                ),
                detail=summary,
            )

        record = context.controller.advance(
            record.run_id,
            expected_revision=record.revision,
            target=RunPhase.IMPLEMENTING,
            reason="another bounded evidence-driven revision is required",
            decision=IterationDecision.REVISE,
        )
        revision_feedback = DynamicRevisionFeedback(
            previous_iteration=iteration_record.iteration,
            output_commit=iteration_record.output_commit,
            blocking_findings=blocking_findings,
            blocking_reasons=blocking_reasons,
            summary=summary,
        )
        previous_signature = signature
        previous_blocking_ids = blocking_ids


__all__ = [
    "_capture_runner_outputs",
    "_event_digest",
    "_execute_approved_plan",
    "_load_artifacts",
    "_references_for_capabilities",
    "_validate_work_chain",
]
