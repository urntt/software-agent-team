"""Handoff envelope creation, quality evidence execution, and dependency results mixin.

This module provides the HandoffMixin which manages deterministic quality boundary
execution, upstream dependency result mapping, and durable handoff envelope
persistence across Agent handoff stages.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from threading import Condition, RLock
from typing import TYPE_CHECKING

from software_agent_team.artifacts import (
    ArtifactKind,
    ArtifactReference,
    CommandEvidence,
    HandoffEnvelope,
    HandoffStatus,
    ReviewReport,
    TaskBrief,
    TestReport,
    WorkResult,
)
from software_agent_team.assembly import (
    assemble_test_report,
    validate_verification_assignment,
)
from software_agent_team.prompting import DynamicUpstreamResult
from software_agent_team.run_control import TerminationReason
from software_agent_team.scheduling import AgentRunOutcome, AgentRunStatus
from software_agent_team.teams import AgentCapability, AgentSpec, TeamPlan

from .types import (
    _UPSTREAM_SUMMARY_LIMIT,
    DynamicAgentRunnerError,
    DynamicQualityGate,
    _bounded_artifact_summary,
    _utc,
)

if TYPE_CHECKING:
    from software_agent_team.artifact_store import ArtifactStore
    from software_agent_team.git_workspace import GitWorkspace, GitWorkspaceManager


class HandoffMixin:
    """Methods for managing durable handoffs, evidence references, and quality gates."""

    artifact_store: ArtifactStore
    workspace: GitWorkspace
    workspace_manager: GitWorkspaceManager
    quality_gate: DynamicQualityGate
    task_brief: TaskBrief
    team_plan: TeamPlan
    iteration: int
    clock: Callable[[], datetime]
    manual_review_criteria: tuple[str, ...]

    _state_lock: RLock
    _latest_commit: str
    _quality_condition: Condition
    _quality_state: str
    _quality_commit: str | None
    _quality_commands: tuple[CommandEvidence, ...]
    _quality_error: DynamicAgentRunnerError | None
    _quality_gate_calls: int
    _controller_test_reference: ArtifactReference | None
    _handoff_sequences: dict[str, int]
    _handoffs: list[ArtifactReference]
    _execution_records: list[ArtifactReference]
    _execution_by_agent: dict[str, list[ArtifactReference]]

    def _ensure_quality_evidence(self) -> tuple[tuple[CommandEvidence, ...], str]:
        """Ensure quality commands execute deterministically once per commit."""
        with self._quality_condition:
            while self._quality_state == "running":
                self._quality_condition.wait()
            if self._quality_state == "ready":
                assert self._quality_commit is not None
                return self._quality_commands, self._quality_commit
            if self._quality_state == "failed":
                assert self._quality_error is not None
                raise self._quality_error
            self._quality_state = "running"
            expected_commit = self._latest_commit
            self._quality_gate_calls += 1

        try:
            self.workspace_manager.verify_workspace(
                self.workspace,
                expected_commit=expected_commit,
                require_clean=True,
            )
            commands = self.quality_gate.run(iteration=self.iteration)
            validate_verification_assignment(
                self.task_brief,
                commands,
                self.manual_review_criteria,
            )
            self.workspace_manager.verify_workspace(
                self.workspace,
                expected_commit=expected_commit,
                require_clean=True,
            )
            controller_test: ArtifactReference | None = None
            if not any(
                agent.capability is AgentCapability.TESTING
                for agent in self.team_plan.agents
            ):
                report = assemble_test_report(
                    None,
                    task_brief=self.task_brief,
                    team_id=self.team_plan.team_id,
                    agent=None,
                    iteration=self.iteration,
                    input_commit=expected_commit,
                    commands=commands,
                    manual_review_criteria=self.manual_review_criteria,
                    created_at=_utc(self.clock),
                )
                controller_test = self.artifact_store.write(
                    report,
                    description="Controller-owned deterministic test evidence.",
                )
        except Exception as error:
            classified = DynamicAgentRunnerError(
                self._error_detail(error),  # type: ignore[attr-defined]
                self._classify_exception(error),  # type: ignore[attr-defined]
            )
            with self._quality_condition:
                self._quality_error = classified
                self._quality_state = "failed"
                self._quality_condition.notify_all()
            raise classified from error

        with self._quality_condition:
            self._quality_commands = commands
            self._quality_commit = expected_commit
            self._controller_test_reference = controller_test
            self._quality_state = "ready"
            self._quality_condition.notify_all()
            return commands, expected_commit

    def _upstream_results(
        self,
        agent: AgentSpec,
        upstream: Mapping[str, AgentRunOutcome],
    ) -> tuple[DynamicUpstreamResult, ...]:
        """Map completed dependency outcomes to structured upstream results."""
        if set(upstream) != set(agent.dependencies):
            raise DynamicAgentRunnerError(
                "scheduler handoffs do not exactly cover Agent dependencies",
                TerminationReason.CONTROLLER_ERROR,
            )
        results: list[DynamicUpstreamResult] = []
        for dependency in agent.dependencies:
            outcome = upstream[dependency]
            if outcome.status is not AgentRunStatus.COMPLETED or outcome.output is None:
                raise DynamicAgentRunnerError(
                    f"dependency {dependency} did not complete",
                    TerminationReason.DEPENDENCY_UNAVAILABLE,
                )
            artifact = self.artifact_store.load(outcome.output)
            if not isinstance(artifact, (WorkResult, TestReport, ReviewReport)):
                raise DynamicAgentRunnerError(
                    f"dependency {dependency} produced an unsupported artifact",
                    TerminationReason.ARTIFACT_INVALID,
                )
            results.append(
                DynamicUpstreamResult(
                    agent_id=dependency,
                    status=HandoffStatus.COMPLETED,
                    summary=_bounded_artifact_summary(
                        artifact.summary,
                        limit=_UPSTREAM_SUMMARY_LIMIT,
                    ),
                    output_commit=(
                        artifact.output_commit
                        if isinstance(artifact, WorkResult)
                        else artifact.input_commit
                    ),
                    completed_task_ids=(
                        artifact.completed_tasks
                        if isinstance(artifact, WorkResult)
                        else ()
                    ),
                )
            )
        return tuple(results)

    def _persist_incoming_handoffs(
        self,
        agent: AgentSpec,
        upstream: Mapping[str, AgentRunOutcome],
    ) -> tuple[ArtifactReference, ...]:
        """Persist inbound handoffs from completed upstream dependencies."""
        self._upstream_results(agent, upstream)
        references: list[ArtifactReference] = []
        for dependency in agent.dependencies:
            outcome = upstream[dependency]
            assert outcome.output is not None
            source = self.team_plan.get_agent(dependency)
            artifact = self.artifact_store.load(outcome.output)
            assert isinstance(artifact, (WorkResult, TestReport, ReviewReport))
            execution_evidence = tuple(
                reference
                for reference in outcome.evidence
                if reference.kind is ArtifactKind.AGENT_EXECUTION_RECORD
            )
            references.append(
                self._write_handoff(
                    source=source,
                    target_agent_id=agent.id,
                    status=HandoffStatus.COMPLETED,
                    input_commit=(
                        artifact.output_commit
                        if isinstance(artifact, WorkResult)
                        else artifact.input_commit
                    ),
                    artifacts=(outcome.output, *execution_evidence),
                    summary=(
                        f"{source.label} supplied controller-verified evidence "
                        f"to {agent.label}."
                    ),
                    blockers=(),
                )
            )
        return tuple(references)

    def _persist_terminal_handoff(
        self,
        agent: AgentSpec,
        *,
        output: ArtifactReference | None,
        executions: tuple[ArtifactReference, ...],
        failed_detail: str | None,
    ) -> tuple[ArtifactReference, ...]:
        """Record outbound terminal handoff for leaf or failed Agents."""
        has_downstream = any(
            agent.id in candidate.dependencies for candidate in self.team_plan.agents
        )
        if failed_detail is None and has_downstream:
            return ()
        try:
            if output is not None:
                artifact = self.artifact_store.load(output)
                assert isinstance(artifact, (WorkResult, TestReport, ReviewReport))
                input_commit = (
                    artifact.output_commit
                    if isinstance(artifact, WorkResult)
                    else artifact.input_commit
                )
                status = HandoffStatus.COMPLETED
                artifacts = (output, *executions)
                summary = f"{agent.label} supplied its terminal run evidence."
                blockers: tuple[str, ...] = ()
            else:
                with self._state_lock:
                    input_commit = self._latest_commit
                status = HandoffStatus.FAILED
                artifacts = executions
                summary = f"{agent.label} reported a terminal execution failure."
                blockers = (failed_detail or "Agent execution failed.",)
            return (
                self._write_handoff(
                    source=agent,
                    target_agent_id=None,
                    status=status,
                    input_commit=input_commit,
                    artifacts=artifacts,
                    summary=summary,
                    blockers=blockers,
                ),
            )
        except Exception:
            if failed_detail is None:
                raise
            return ()

    def _write_handoff(
        self,
        *,
        source: AgentSpec,
        target_agent_id: str | None,
        status: HandoffStatus,
        input_commit: str,
        artifacts: tuple[ArtifactReference, ...],
        summary: str,
        blockers: tuple[str, ...],
    ) -> ArtifactReference:
        """Construct and persist a validated HandoffEnvelope artifact."""
        with self._state_lock:
            sequence = self._handoff_sequences.get(source.stage_id, 0) + 1
            self._handoff_sequences[source.stage_id] = sequence
            envelope = HandoffEnvelope(
                run_id=self.task_brief.run_id,
                team_id=self.team_plan.team_id,
                iteration=self.iteration,
                stage=source.stage_id,
                sequence=sequence,
                source_agent_id=source.id,
                target_agent_id=target_agent_id,
                status=status,
                created_at=_utc(self.clock),
                summary=summary,
                input_commit=input_commit,
                artifacts=list(self._unique_references(artifacts)),
                blockers=list(blockers),
            )
            reference = self.artifact_store.write(
                envelope,
                description=(
                    f"Handoff from {source.id} to {target_agent_id or 'controller'}."
                ),
            )
            self._handoffs.append(reference)
            return reference

    def _record_execution_reference(
        self,
        agent_id: str,
        reference: ArtifactReference,
    ) -> None:
        """Store one AgentExecutionRecord reference in shared state."""
        with self._state_lock:
            self._execution_records.append(reference)
            self._execution_by_agent.setdefault(agent_id, []).append(reference)

    def _execution_references(
        self,
        agent_id: str,
    ) -> tuple[ArtifactReference, ...]:
        """Fetch all historical execution references for an Agent."""
        with self._state_lock:
            return tuple(self._execution_by_agent.get(agent_id, ()))

    def _quality_auxiliary_references(
        self,
        agent: AgentSpec,
    ) -> tuple[ArtifactReference, ...]:
        """Provide controller test evidence references when applicable."""
        if agent.capability not in {AgentCapability.TESTING, AgentCapability.REVIEW}:
            return ()
        with self._state_lock:
            return (
                ()
                if self._controller_test_reference is None
                else (self._controller_test_reference,)
            )

    @staticmethod
    def _unique_references(
        values: tuple[ArtifactReference, ...],
    ) -> tuple[ArtifactReference, ...]:
        """Deduplicate artifact references by relative path preserving order."""
        return tuple({item.path: item for item in values}.values())
