"""Shared human-readable rendering for controller-owned terminal evidence."""

from __future__ import annotations

from collections import Counter
from typing import cast

from software_agent_team.artifact_store import ArtifactStore
from software_agent_team.artifacts import (
    AgentExecutionRecord,
    ArtifactReference,
    CommandEvidence,
    FinalReport,
)
from software_agent_team.budgets import (
    BUDGET_LEDGER_FILENAME,
    BudgetLedgerRecord,
    ModelCallCostRecord,
)
from software_agent_team.run_control import RunRecord


def _pricing_source(call: ModelCallCostRecord) -> str:
    return "unknown" if call.pricing_source is None else call.pricing_source.value


def render_run_report(
    *,
    artifact_store: ArtifactStore,
    record: RunRecord,
    report: FinalReport,
    execution_records: tuple[ArtifactReference, ...],
    handoffs: tuple[ArtifactReference, ...],
    command_evidence: tuple[CommandEvidence, ...],
    budget_ledger: BudgetLedgerRecord,
    budget_ledger_sha256: str,
) -> str:
    """Derive one stable Markdown view from immutable run evidence."""

    executions = [
        cast(AgentExecutionRecord, artifact_store.load(reference))
        for reference in sorted(execution_records, key=lambda item: item.path)
    ]
    calls = len(executions)
    failures = sum(item.error is not None for item in executions)
    identities = Counter(
        (item.iteration, item.stage, item.agent_id) for item in executions
    )
    retries = sum(max(0, count - 1) for count in identities.values())
    duration_ms = sum(item.duration_ms for item in executions)
    input_tokens = [
        item.input_tokens for item in executions if item.input_tokens is not None
    ]
    output_tokens = [
        item.output_tokens for item in executions if item.output_tokens is not None
    ]
    gate_duration_ms = sum(command.duration_ms for command in command_evidence)
    if not budget_ledger.usage.unpriced_calls:
        estimated_cost_text = f"${budget_ledger.usage.known_estimated_cost_usd:.6f}"
    elif budget_ledger.usage.unpriced_calls == budget_ledger.usage.calls_completed:
        estimated_cost_text = "not configured"
    else:
        estimated_cost_text = "partially unknown"
    token_text = (
        f"{sum(input_tokens)} input / {sum(output_tokens)} output"
        if input_tokens or output_tokens
        else "not reported"
    )
    lines = [
        f"# Run report: {report.run_id}",
        "",
        f"- Status: `{report.status.value}`",
        f"- Team: `{report.team_id}`",
        f"- Termination reason: `{report.termination_reason}`",
        f"- Final commit: `{report.final_commit or 'not available'}`",
        f"- Iterations recorded: {len(report.iterations)}",
        "",
        "## Summary",
        "",
        report.summary,
        "",
        "## Acceptance results",
        "",
        "| Criterion | Status | Detail |",
        "| --- | --- | --- |",
    ]
    if report.acceptance_results:
        lines.extend(
            f"| {item.criterion_id} | {item.status.value} | "
            f"{item.detail.replace('|', '\\|')} |"
            for item in report.acceptance_results
        )
    else:
        lines.append("| _none recorded_ | blocked | No test report was available. |")
    lines.extend(
        [
            "",
            "## Execution metrics",
            "",
            f"- Agent calls: {calls}",
            f"- Controlled response repairs: {retries}",
            f"- Failed Agent attempts: {failures}",
            f"- Agent duration: {duration_ms} ms",
            f"- Deterministic-gate duration: {gate_duration_ms} ms",
            f"- Reported tokens: {token_text}",
            f"- Estimated model cost: {estimated_cost_text}",
            f"- Complete-journey model calls: {budget_ledger.usage.calls_completed}",
            "",
            "## Model-spend authorization",
            "",
            f"- Authority: `{budget_ledger.budget.authority.value}`",
            "- Authorized estimated spend ceiling: "
            f"${budget_ledger.budget.max_estimated_cost_usd}",
            "- Recorded estimated spend: "
            f"${budget_ledger.usage.known_estimated_cost_usd:.6f}",
            "- Recorded remaining authorization: "
            f"${budget_ledger.usage.remaining_estimated_cost_usd(budget_ledger.budget):.6f}",
            f"- Calls with unknown cost: {budget_ledger.usage.unpriced_calls}",
            "- Cost values marked `estimated` use frozen per-token prices and "
            "provider-reported token usage; `provider_reported` is reserved for "
            "a provider-supplied USD amount; `unknown` is never treated as zero.",
            "- An absolute billing cap requires a provider-side spending or quota "
            "limit because final token usage arrives after a call.",
            "",
            "### Cost by call, Agent, phase, and route",
            "",
            (
                "| Call | Run | Phase | Agent | Attempt | Route / model | "
                "Price source | Tokens | Cost basis | Cost |"
            ),
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
            *(
                (
                    f"| {call.sequence} | `{call.run_id or 'not attributed'}` | "
                    f"`{call.stage or 'not attributed'}` | `{call.agent_id}` | "
                    f"{call.attempt} | `{call.route_id or 'not attributed'} / "
                    f"{call.model or 'not attributed'}` | "
                    f"`{_pricing_source(call)}` | "
                    + (
                        f"{call.input_tokens} input / {call.output_tokens} output"
                        if call.input_tokens is not None
                        and call.output_tokens is not None
                        else "not reported"
                    )
                    + f" | `{call.cost_source.value}` | "
                    + (
                        f"${call.cost_usd:.6f}"
                        if call.cost_usd is not None
                        else "unknown"
                    )
                    + " |"
                )
                for call in budget_ledger.calls
            ),
            "",
            "## Evidence index",
            "",
            "### Iteration decisions",
            "",
        ]
    )
    lines.extend(
        f"- `{reference.path}` (`{reference.sha256}`)"
        for reference in report.iterations
    )
    if not report.iterations:
        lines.append("- No complete iteration decision was recorded.")
    lines.extend(["", "### Agent executions", ""])
    lines.extend(
        f"- `{reference.path}` (`{reference.sha256}`)"
        for reference in sorted(execution_records, key=lambda item: item.path)
    )
    if not execution_records:
        lines.append("- No Agent execution completed far enough to record telemetry.")
    lines.extend(
        [
            "",
            "### Model-spend ledger",
            "",
            f"- `{BUDGET_LEDGER_FILENAME}` (`{budget_ledger_sha256}`)",
        ]
    )
    lines.extend(["", "### Handoffs", ""])
    lines.extend(
        f"- `{reference.path}` (`{reference.sha256}`)"
        for reference in sorted(handoffs, key=lambda item: item.path)
    )
    if not handoffs:
        lines.append("- No cross-Agent handoff was recorded.")
    if report.known_limitations:
        lines.extend(["", "## Known limitations", ""])
        lines.extend(f"- {item}" for item in report.known_limitations)
    if report.unresolved_findings:
        lines.extend(["", "## Unresolved findings", ""])
        lines.extend(f"- {item}" for item in report.unresolved_findings)
    lines.extend(
        [
            "",
            "## Workspace",
            "",
            (
                f"The isolated result remains at `{record.workspace.workspace_path}`."
                if record.workspace is not None
                else "No isolated workspace was attached."
            ),
            "",
            "This report is derived from the immutable JSON artifacts in this run.",
            "",
        ]
    )
    return "\n".join(lines)


def render_minimal_terminal_report(
    *,
    report: FinalReport,
    budget_ledger: BudgetLedgerRecord,
    budget_ledger_sha256: str,
    rendering_error: Exception,
) -> str:
    """Render a dependency-free failure view when rich rendering cannot finish."""

    detail = " ".join(str(rendering_error).split()) or type(rendering_error).__name__
    return "\n".join(
        (
            f"# Run report: {report.run_id}",
            "",
            f"- Status: `{report.status.value}`",
            f"- Team: `{report.team_id}`",
            f"- Termination reason: `{report.termination_reason}`",
            f"- Final commit: `{report.final_commit or 'not available'}`",
            "",
            "## Summary",
            "",
            report.summary,
            "",
            "## Report finalization diagnostic",
            "",
            "The detailed evidence view could not be rendered. The machine-readable "
            "final report, model-spend ledger, and earlier execution evidence remain "
            "authoritative.",
            f"- Rendering error: `{type(rendering_error).__name__}: {detail[:1000]}`",
            "- Recorded estimated model spend: "
            f"${budget_ledger.usage.known_estimated_cost_usd:.6f}",
            f"- `{BUDGET_LEDGER_FILENAME}` (`{budget_ledger_sha256}`)",
            "",
        )
    )
