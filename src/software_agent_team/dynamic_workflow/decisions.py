"""Dynamic workflow decisions, evaluation logic, and terminal report rendering."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import ValidationError

from software_agent_team.artifact_store import (
    ArtifactStoreError,
    FinalReportBundle,
)
from software_agent_team.artifacts import (
    ArtifactReference,
    CheckStatus,
    FinalReport,
    FinalStatus,
    IterationDecision,
    ReviewReport,
    ReviewTerminationReason,
    ReviewVerdict,
    TestReport,
    collect_unresolved_review_findings,
    resolve_acceptance_results,
)
from software_agent_team.budgets import (
    BudgetLedgerRecord,
    budget_ledger_sha256,
)
from software_agent_team.dynamic_runner import DynamicAgentRunnerError
from software_agent_team.dynamic_workflow.types import (
    DynamicWorkflowError,
    DynamicWorkflowOutcome,
    _DynamicWorkflowContext,
    _unique,
    _utc,
)
from software_agent_team.git_workspace import (
    GitWorkspaceError,
    WorkspaceIntegrityError,
)
from software_agent_team.progress import (
    ProgressEvent,
    ProgressEventKind,
)
from software_agent_team.quality_gates import (
    QualityGateBudgetExceeded,
    QualityGateError,
    SandboxUnavailableError,
)
from software_agent_team.run_control import (
    RunControlError,
    RunPhase,
    RunRecord,
    TerminationReason,
)
from software_agent_team.runtime_configuration import RuntimeConfigurationError
from software_agent_team.runtime_controls import RuntimeControlDecision

if TYPE_CHECKING:
    from software_agent_team.dynamic_workflow.coordinator import (
        DynamicWorkflowCoordinator,
    )


def _decide(
    tests: tuple[TestReport, ...],
    reviews: tuple[ReviewReport, ...],
    *,
    iteration: int,
    iteration_limit: int,
) -> IterationDecision:
    """Evaluate test and review artifacts to produce an iteration decision."""
    if any(test.status is CheckStatus.BLOCKED for test in tests) or any(
        review.verdict is ReviewVerdict.FAIL for review in reviews
    ):
        return IterationDecision.FAIL
    if all(test.status is CheckStatus.PASSED for test in tests) and all(
        review.verdict is ReviewVerdict.ACCEPT for review in reviews
    ):
        return IterationDecision.ACCEPT
    if iteration < iteration_limit:
        return IterationDecision.REVISE
    return IterationDecision.FAIL


def _blocking_reasons(tests: tuple[TestReport, ...]) -> tuple[str, ...]:
    """Aggregate unique blockers and diagnostic findings from failed tests."""
    reasons: list[str] = []
    for test in tests:
        if test.status is CheckStatus.PASSED:
            continue
        reasons.extend(test.blockers)
        reasons.extend(
            "Tester analysis (verify against deterministic command evidence): "
            + finding
            for finding in test.findings
        )
        if not test.blockers and not test.findings:
            reasons.append(f"Tester {test.producer} status is {test.status.value}.")
    return _unique(reasons)


def _decision_summary(
    decision: IterationDecision,
    tests: tuple[TestReport, ...],
    reviews: tuple[ReviewReport, ...],
) -> str:
    """Format a human-readable summary of the iteration decision."""
    test_summary = ",".join(f"{test.producer}:{test.status.value}" for test in tests)
    review_summary = (
        ",".join(f"{review.producer}:{review.verdict.value}" for review in reviews)
        or "none"
    )
    return (
        f"Controller decision: {decision.value}; tests={test_summary}; "
        f"reviews={review_summary}."
    )


def _decision_termination_reason(
    tests: tuple[TestReport, ...],
    reviews: tuple[ReviewReport, ...],
    *,
    iteration: int,
    iteration_limit: int,
    repeated: bool,
) -> TerminationReason:
    """Determine the authoritative termination reason for a rejected iteration."""
    if repeated:
        return TerminationReason.REPEATED_BLOCKER
    if any(test.status is CheckStatus.BLOCKED for test in tests):
        return TerminationReason.DEPENDENCY_UNAVAILABLE
    failed_reviews = tuple(
        review for review in reviews if review.verdict is ReviewVerdict.FAIL
    )
    if failed_reviews:
        if any(
            review.termination_reason
            is ReviewTerminationReason.EVIDENCE_INTEGRITY_COMPROMISED
            for review in failed_reviews
        ):
            return TerminationReason.ARTIFACT_INVALID
        return TerminationReason.SAFETY_BOUNDARY_CROSSED
    if iteration >= iteration_limit:
        return TerminationReason.ITERATION_LIMIT_REACHED
    return TerminationReason.EXECUTION_FAILED


def _outcome(
    context: _DynamicWorkflowContext,
    record: RunRecord,
    final_report: ArtifactReference,
    markdown: str,
    *,
    control_stop: RuntimeControlDecision | None = None,
    correction_instruction: str | None = None,
) -> DynamicWorkflowOutcome:
    """Assemble terminal execution state and index all recorded evidence."""
    return DynamicWorkflowOutcome(
        record=context.controller.load(record.run_id),
        final_report=final_report,
        human_report_path=markdown,
        execution_records=tuple(
            sorted(context.execution_records, key=lambda item: item.path)
        ),
        handoffs=tuple(sorted(context.handoffs, key=lambda item: item.path)),
        events=context.event_journal.load(),
        schedules=tuple(context.schedules),
        control_stop=control_stop,
        correction_instruction=correction_instruction,
    )


def _render_report(
    context: _DynamicWorkflowContext,
    record: RunRecord,
    report: FinalReport,
    budget_record: BudgetLedgerRecord,
    budget_digest: str,
) -> str:
    """Render the authoritative markdown report via dynamic workflow facade."""
    from software_agent_team import dynamic_workflow

    return dynamic_workflow.render_run_report(
        artifact_store=context.artifact_store,
        record=record,
        report=report,
        execution_records=tuple(context.execution_records),
        handoffs=tuple(context.handoffs),
        command_evidence=tuple(context.command_evidence),
        budget_ledger=budget_record,
        budget_ledger_sha256=budget_digest,
    )


def _write_terminal_report(
    coordinator: DynamicWorkflowCoordinator | type[DynamicWorkflowCoordinator],
    context: _DynamicWorkflowContext,
    record: RunRecord,
    report: FinalReport,
    *,
    description: str,
    allow_minimal_fallback: bool = False,
) -> FinalReportBundle:
    """Prepare terminal evidence before committing its canonical bundle."""
    budget_record = context.budget_ledger.terminal_record()
    budget_digest = budget_ledger_sha256(budget_record)
    try:
        render_func = getattr(coordinator, "_render_report", _render_report)
        markdown = render_func(
            context,
            record,
            report,
            budget_record,
            budget_digest,
        )
    except Exception as error:
        if not allow_minimal_fallback:
            raise
        from software_agent_team import dynamic_workflow

        markdown = dynamic_workflow.render_minimal_terminal_report(
            report=report,
            budget_ledger=budget_record,
            budget_ledger_sha256=budget_digest,
            rendering_error=error,
        )
    bundle = context.artifact_store.write_final_report_bundle(
        report,
        markdown,
        budget_record,
        description=description,
    )
    return bundle


def _complete(
    coordinator: DynamicWorkflowCoordinator,
    context: _DynamicWorkflowContext,
    record: RunRecord,
    tests: tuple[TestReport, ...],
    reviews: tuple[ReviewReport, ...],
) -> DynamicWorkflowOutcome:
    """Finalize successful run, bind final report bundle, and emit event."""
    acceptance = resolve_acceptance_results(tests[0], reviews)
    limitations = _unique(
        [issue for work in context.last_works for issue in work.unresolved_issues]
    )
    nonblocking = collect_unresolved_review_findings(context.review_history)
    report = FinalReport(
        run_id=record.run_id,
        team_id=record.team_id,
        created_at=_utc(coordinator.clock),
        software_version=coordinator.software_version,
        status=FinalStatus.COMPLETED,
        termination_reason=TerminationReason.SUCCEEDED.value,
        final_commit=record.current_commit,
        iterations=tuple(context.iteration_records),
        acceptance_results=acceptance,
        unresolved_findings=nonblocking,
        known_limitations=limitations,
        summary="The adaptive team passed deterministic gates and review.",
    )
    bundle = coordinator._write_terminal_report(
        context,
        record,
        report,
        description="Authoritative terminal report.",
    )
    record = context.artifact_store.bind_final_report_bundle(
        bundle,
        lambda reference: context.controller.complete(
            record.run_id,
            expected_revision=record.revision,
            detail=report.summary,
            final_report=reference,
        ),
    )
    coordinator._emit(
        context,
        ProgressEvent(
            kind=ProgressEventKind.RUN_COMPLETED,
            message="Build accepted; preparing the verified delivery",
            phase=RunPhase.COMPLETED,
            iteration=record.current_iteration,
        ),
    )
    return coordinator._outcome(
        context,
        record,
        bundle.reference,
        bundle.markdown_path,
    )


def _fail(
    coordinator: DynamicWorkflowCoordinator,
    context: _DynamicWorkflowContext,
    record: RunRecord,
    *,
    reason: TerminationReason,
    detail: str,
) -> DynamicWorkflowOutcome:
    """Finalize failed run with collected unresolved findings and blockers."""
    unresolved = [detail]
    unresolved.extend(collect_unresolved_review_findings(context.review_history))
    for test in context.last_tests:
        unresolved.extend(test.blockers)
        unresolved.extend(test.findings)
    for review in context.last_reviews:
        for finding in review.findings:
            if not finding.blocking:
                continue
            if (
                review.iteration < record.current_iteration
                and review.input_commit != record.current_commit
            ):
                unresolved.append(
                    f"Finding {finding.id} was discovered on commit "
                    f"{review.input_commit[:12]} in iteration "
                    f"{review.iteration}. The implementation changed on commit "
                    f"{record.current_commit[:12]}, but independent "
                    "re-verification did not complete; the finding remains "
                    f"unresolved: {finding.description}"
                )
            else:
                unresolved.append(finding.description)
    limitations = _unique(
        [issue for work in context.last_works for issue in work.unresolved_issues]
    )
    report = FinalReport(
        run_id=record.run_id,
        team_id=record.team_id,
        created_at=_utc(coordinator.clock),
        software_version=coordinator.software_version,
        status=FinalStatus.FAILED,
        termination_reason=reason.value,
        final_commit=record.current_commit,
        iterations=tuple(context.iteration_records),
        acceptance_results=(
            () if not context.last_tests else context.last_tests[0].criteria
        ),
        unresolved_findings=_unique(unresolved),
        known_limitations=limitations,
        summary=f"The adaptive run failed: {detail}",
    )
    bundle = coordinator._write_terminal_report(
        context,
        record,
        report,
        description="Authoritative terminal failure report.",
        allow_minimal_fallback=True,
    )
    record = context.artifact_store.bind_final_report_bundle(
        bundle,
        lambda reference: context.controller.fail(
            record.run_id,
            expected_revision=record.revision,
            reason=reason,
            detail=detail,
            final_report=reference,
        ),
    )
    coordinator._emit(
        context,
        ProgressEvent(
            kind=ProgressEventKind.RUN_FAILED,
            message="Build stopped; see the final report",
            phase=RunPhase.FAILED,
            iteration=record.current_iteration,
        ),
    )
    return coordinator._outcome(
        context,
        record,
        bundle.reference,
        bundle.markdown_path,
    )


def _stop_by_user(
    coordinator: DynamicWorkflowCoordinator,
    context: _DynamicWorkflowContext,
    record: RunRecord,
    *,
    reason: TerminationReason,
    detail: str,
    control_stop: RuntimeControlDecision,
    correction_instruction: str | None = None,
) -> DynamicWorkflowOutcome:
    """Finalize run interrupted or cancelled by external user control."""
    report = FinalReport(
        run_id=record.run_id,
        team_id=record.team_id,
        created_at=_utc(coordinator.clock),
        software_version=coordinator.software_version,
        status=FinalStatus.CANCELLED,
        termination_reason=reason.value,
        final_commit=record.current_commit,
        iterations=tuple(context.iteration_records),
        acceptance_results=(),
        unresolved_findings=collect_unresolved_review_findings(context.review_history),
        known_limitations=(),
        summary=detail,
    )
    bundle = coordinator._write_terminal_report(
        context,
        record,
        report,
        description="Authoritative user-controlled terminal report.",
        allow_minimal_fallback=True,
    )
    record = context.artifact_store.bind_final_report_bundle(
        bundle,
        lambda reference: context.controller.fail(
            record.run_id,
            expected_revision=record.revision,
            reason=reason,
            detail=detail,
            final_report=reference,
        ),
    )
    coordinator._emit(
        context,
        ProgressEvent(
            kind=ProgressEventKind.RUN_CANCELLED,
            message=detail,
            phase=RunPhase.FAILED,
            iteration=record.current_iteration,
        ),
    )
    return coordinator._outcome(
        context,
        record,
        bundle.reference,
        bundle.markdown_path,
        control_stop=control_stop,
        correction_instruction=correction_instruction,
    )


def _termination_reason(error: Exception) -> TerminationReason:
    """Map runtime exceptions to authoritative RunController termination reasons."""
    if isinstance(error, DynamicAgentRunnerError):
        return error.reason
    if isinstance(error, QualityGateBudgetExceeded):
        return TerminationReason.RESOURCE_LIMIT_REACHED
    if isinstance(error, (SandboxUnavailableError, RuntimeConfigurationError)):
        return TerminationReason.DEPENDENCY_UNAVAILABLE
    if isinstance(error, WorkspaceIntegrityError):
        if "no new commit" in str(error) or "no changed files" in str(error):
            return TerminationReason.NO_RELEVANT_CHANGE
        return TerminationReason.SAFETY_BOUNDARY_CROSSED
    if isinstance(error, GitWorkspaceError):
        return TerminationReason.SAFETY_BOUNDARY_CROSSED
    if isinstance(
        error,
        (
            DynamicWorkflowError,
            ArtifactStoreError,
            RunControlError,
            ValidationError,
        ),
    ):
        return TerminationReason.ARTIFACT_INVALID
    if isinstance(error, QualityGateError):
        return TerminationReason.EXECUTION_FAILED
    return TerminationReason.CONTROLLER_ERROR


def _error_detail(error: Exception) -> str:
    """Format a clean exception detail message without excess whitespace."""
    return str(error).strip() or type(error).__name__


__all__ = [
    "_blocking_reasons",
    "_complete",
    "_decide",
    "_decision_summary",
    "_decision_termination_reason",
    "_error_detail",
    "_fail",
    "_outcome",
    "_render_report",
    "_stop_by_user",
    "_termination_reason",
    "_write_terminal_report",
]
