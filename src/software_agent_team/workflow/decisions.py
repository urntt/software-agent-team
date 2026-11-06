"""Gate decisions, final reporting, and outcome rendering helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from software_agent_team.artifact_store import FinalReportBundle
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
from software_agent_team.progress import (
    ProgressEvent,
    ProgressEventKind,
)
from software_agent_team.run_control import (
    RunPhase,
    RunRecord,
    TerminationReason,
)
from software_agent_team.workflow.types import (
    WorkflowOutcome,
    _unique,
    _utc,
    _WorkflowContext,
)

if TYPE_CHECKING:
    from software_agent_team.workflow.coordinator import WorkflowCoordinator


def _decide(
    test: TestReport,
    review: ReviewReport,
    *,
    iteration: int,
    iteration_limit: int,
) -> IterationDecision:
    if test.status is CheckStatus.BLOCKED or review.verdict is ReviewVerdict.FAIL:
        return IterationDecision.FAIL
    if test.status is CheckStatus.PASSED and review.verdict is ReviewVerdict.ACCEPT:
        return IterationDecision.ACCEPT
    if iteration < iteration_limit:
        return IterationDecision.REVISE
    return IterationDecision.FAIL


def _blocking_reasons(test: TestReport) -> tuple[str, ...]:
    if test.status is CheckStatus.PASSED:
        return ()
    reasons = [*test.blockers, *test.findings]
    if not reasons:
        reasons.append(f"Tester status is {test.status.value}.")
    return _unique(reasons)


def _decision_summary(
    decision: IterationDecision,
    test: TestReport,
    review: ReviewReport,
) -> str:
    return (
        f"Controller decision: {decision.value}; "
        f"test={test.status.value}; review={review.verdict.value}."
    )


def _decision_termination_reason(
    test: TestReport,
    review: ReviewReport,
    *,
    iteration: int,
    iteration_limit: int,
) -> TerminationReason:
    if test.status is CheckStatus.BLOCKED:
        return TerminationReason.DEPENDENCY_UNAVAILABLE
    if review.verdict is ReviewVerdict.FAIL:
        if (
            review.termination_reason
            is ReviewTerminationReason.EVIDENCE_INTEGRITY_COMPROMISED
        ):
            return TerminationReason.ARTIFACT_INVALID
        return TerminationReason.SAFETY_BOUNDARY_CROSSED
    if iteration >= iteration_limit:
        return TerminationReason.ITERATION_LIMIT_REACHED
    return TerminationReason.EXECUTION_FAILED


def _outcome(
    context: _WorkflowContext,
    record: RunRecord,
    final_reference: ArtifactReference,
    markdown_path: str,
) -> WorkflowOutcome:
    record = context.controller.load(record.run_id)
    return WorkflowOutcome(
        record=record,
        final_report=final_reference,
        human_report_path=markdown_path,
        execution_records=tuple(
            sorted(context.execution_records, key=lambda item: item.path)
        ),
        handoffs=tuple(sorted(context.handoffs, key=lambda item: item.path)),
        events=context.event_journal.load(),
    )


def _render_report(
    coordinator: WorkflowCoordinator,
    context: _WorkflowContext,
    record: RunRecord,
    report: FinalReport,
    budget_record: BudgetLedgerRecord,
    budget_digest: str,
) -> str:
    from software_agent_team import workflow

    return workflow.render_run_report(
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
    coordinator: WorkflowCoordinator,
    context: _WorkflowContext,
    record: RunRecord,
    report: FinalReport,
    *,
    description: str,
    allow_minimal_fallback: bool = False,
) -> FinalReportBundle:
    """Prepare terminal evidence before committing its canonical bundle."""
    assert context.budget_ledger is not None
    budget_record = context.budget_ledger.terminal_record()
    budget_digest = budget_ledger_sha256(budget_record)
    try:
        markdown = _render_report(
            coordinator,
            context,
            record,
            report,
            budget_record,
            budget_digest,
        )
    except Exception as error:
        if not allow_minimal_fallback:
            raise
        from software_agent_team import workflow

        markdown = workflow.render_minimal_terminal_report(
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
    coordinator: WorkflowCoordinator,
    context: _WorkflowContext,
    record: RunRecord,
    test: TestReport,
    review: ReviewReport | None,
) -> WorkflowOutcome:
    final_report = FinalReport(
        run_id=record.run_id,
        team_id=record.team_id,
        created_at=_utc(coordinator.clock),
        software_version=coordinator.software_version,
        status=FinalStatus.COMPLETED,
        termination_reason=TerminationReason.SUCCEEDED.value,
        final_commit=record.current_commit,
        iterations=tuple(context.iteration_records),
        acceptance_results=resolve_acceptance_results(
            test, () if review is None else review
        ),
        unresolved_findings=collect_unresolved_review_findings(context.review_history),
        summary=(
            "The implementation passed deterministic gates; no independent "
            "Agent review was part of this baseline."
            if review is None
            else "The implementation passed deterministic gates and review."
        ),
    )
    bundle = _write_terminal_report(
        coordinator,
        context,
        record,
        final_report,
        description="Authoritative terminal report.",
    )
    record = context.artifact_store.bind_final_report_bundle(
        bundle,
        lambda reference: context.controller.complete(
            record.run_id,
            expected_revision=record.revision,
            detail=final_report.summary,
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
    return _outcome(
        context,
        record,
        bundle.reference,
        bundle.markdown_path,
    )


def _fail(
    coordinator: WorkflowCoordinator,
    context: _WorkflowContext,
    record: RunRecord,
    *,
    reason: TerminationReason,
    detail: str,
) -> WorkflowOutcome:
    unresolved = [detail]
    unresolved.extend(collect_unresolved_review_findings(context.review_history))
    if context.last_test is not None:
        unresolved.extend(context.last_test.blockers)
        unresolved.extend(context.last_test.findings)
    if context.last_review is not None:
        unresolved.extend(
            finding.description
            for finding in context.last_review.findings
            if finding.blocking
        )
    final_report = FinalReport(
        run_id=record.run_id,
        team_id=record.team_id,
        created_at=_utc(coordinator.clock),
        software_version=coordinator.software_version,
        status=FinalStatus.FAILED,
        termination_reason=reason.value,
        final_commit=record.current_commit,
        iterations=tuple(context.iteration_records),
        acceptance_results=(
            () if context.last_test is None else context.last_test.criteria
        ),
        unresolved_findings=_unique(unresolved),
        summary=f"The run failed: {detail}",
    )
    bundle = _write_terminal_report(
        coordinator,
        context,
        record,
        final_report,
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
            message=(
                "Build stopped during "
                f"{record.transitions[-1].source.value}; see the final report"
            ),
            phase=RunPhase.FAILED,
            iteration=record.current_iteration,
        ),
    )
    return _outcome(
        context,
        record,
        bundle.reference,
        bundle.markdown_path,
    )
