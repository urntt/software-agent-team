"""Shared human-readable rendering for controller-owned terminal evidence."""

from __future__ import annotations

from collections import Counter
from decimal import Decimal
from typing import cast

from software_agent_team.artifact_store import ArtifactStore
from software_agent_team.artifacts import (
    AgentExecutionRecord,
    ArtifactReference,
    CommandEvidence,
    FinalReport,
)
from software_agent_team.run_control import RunRecord


def render_run_report(
    *,
    artifact_store: ArtifactStore,
    record: RunRecord,
    report: FinalReport,
    execution_records: tuple[ArtifactReference, ...],
    handoffs: tuple[ArtifactReference, ...],
    command_evidence: tuple[CommandEvidence, ...],
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
    estimated_cost_values = tuple(
        item.estimated_cost_usd
        for item in executions
        if item.estimated_cost_usd is not None
    )
    estimated_cost_text = (
        f"${sum(estimated_cost_values, Decimal(0)):.6f}"
        if estimated_cost_values
        else "not configured"
    )
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
