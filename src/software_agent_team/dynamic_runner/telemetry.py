"""Agent execution activity observation and progress event emission mixin.

This module provides the AgentTelemetryMixin which translates fine-grained low-level
execution events (initialization progress, provider streaming, tool starts/finishes,
liveness degradation, stall warnings/recoveries) into high-level ProgressEvents.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from software_agent_team.budgets import AgentBudgetUsage
from software_agent_team.execution import (
    AgentExecutionActivity,
    AgentExecutionActivityKind,
)
from software_agent_team.invocation_lifecycle import InvocationPhase
from software_agent_team.progress import (
    ProgressCheckpointSnapshot,
    ProgressEvent,
    ProgressEventKind,
    RunEventReference,
)
from software_agent_team.teams import AgentCapability, AgentSpec
from software_agent_team.terminal_presentation import InvocationPresentation

if TYPE_CHECKING:
    pass


class AgentTelemetryMixin:
    """Methods for translating low-level activity into progress events."""

    activity_handler: Callable[[ProgressEvent], None] | None
    iteration: int

    def _emit_activity(
        self,
        agent: AgentSpec,
        *,
        kind: ProgressEventKind,
        message: str,
        attempt: int,
        model: str | None,
        duration_ms: int | None = None,
        budget_usage: AgentBudgetUsage | None = None,
        references: tuple[RunEventReference, ...] = (),
        checkpoint: ProgressCheckpointSnapshot | None = None,
        presentation: InvocationPresentation | None = None,
    ) -> None:
        """Project one safe invocation checkpoint without changing runtime state."""
        if self.activity_handler is None:
            return
        self.activity_handler(
            ProgressEvent(
                kind=kind,
                message=(" ".join(message.split()) or "Agent activity changed")[:500],
                agent_id=agent.id,
                iteration=self.iteration,
                attempt=attempt,
                duration_ms=duration_ms,
                capability=agent.capability.value,
                specialization=agent.specialization.value,
                stage_id=agent.stage_id,
                model=model,
                dependency_ids=agent.dependencies,
                budget_usage=budget_usage,
                checkpoint=checkpoint,
                presentation=presentation,
                references=references,
            )
        )

    def _observe_execution_activity(
        self,
        agent: AgentSpec,
        *,
        attempt: int,
        activity: AgentExecutionActivity,
    ) -> None:
        """Map fine-grained agent execution activity into structured progress events."""
        kind = {
            AgentExecutionActivityKind.INVOCATION_LAUNCHED: (
                ProgressEventKind.AGENT_INVOCATION_LAUNCHED
            ),
            AgentExecutionActivityKind.INVOCATION_INITIALIZING: (
                ProgressEventKind.AGENT_INITIALIZING
            ),
            AgentExecutionActivityKind.INITIALIZATION_PROGRESS: (
                ProgressEventKind.AGENT_INITIALIZATION_PROGRESS
            ),
            AgentExecutionActivityKind.INITIALIZATION_LIVENESS_DEGRADED: (
                ProgressEventKind.AGENT_INITIALIZATION_LIVENESS_DEGRADED
            ),
            AgentExecutionActivityKind.INITIALIZATION_STALL_SUSPECTED: (
                ProgressEventKind.AGENT_INITIALIZATION_STALL_SUSPECTED
            ),
            AgentExecutionActivityKind.INITIALIZATION_STALL_RECOVERED: (
                ProgressEventKind.AGENT_INITIALIZATION_STALL_RECOVERED
            ),
            AgentExecutionActivityKind.INITIALIZATION_STALLED: (
                ProgressEventKind.AGENT_INITIALIZATION_STALLED
            ),
            AgentExecutionActivityKind.INVOCATION_PROVIDER_WAIT: (
                ProgressEventKind.AGENT_WAITING_PROVIDER
            ),
            AgentExecutionActivityKind.INVOCATION_TOOL_ACTIVE: (
                ProgressEventKind.AGENT_TOOL_ACTIVE
            ),
            AgentExecutionActivityKind.INVOCATION_FINALIZING_RESPONSE: (
                ProgressEventKind.AGENT_FINALIZING_RESPONSE
            ),
            AgentExecutionActivityKind.INVOCATION_STOPPING: (
                ProgressEventKind.AGENT_STOPPING
            ),
            AgentExecutionActivityKind.INVOCATION_COLLECTING_EVIDENCE: (
                ProgressEventKind.AGENT_COLLECTING_EVIDENCE
            ),
            AgentExecutionActivityKind.INVOCATION_STOPPED: (
                ProgressEventKind.AGENT_STOPPED
            ),
            AgentExecutionActivityKind.PROVIDER_STREAM: (
                ProgressEventKind.AGENT_PROVIDER_ACTIVITY
            ),
            AgentExecutionActivityKind.TOOL_STARTED: (
                ProgressEventKind.AGENT_TOOL_STARTED
            ),
            AgentExecutionActivityKind.TOOL_COMPLETED: (
                ProgressEventKind.AGENT_TOOL_COMPLETED
            ),
            AgentExecutionActivityKind.LIVENESS_DEGRADED: (
                ProgressEventKind.AGENT_LIVENESS_DEGRADED
            ),
            AgentExecutionActivityKind.STALL_SUSPECTED: (
                ProgressEventKind.AGENT_STALL_SUSPECTED
            ),
            AgentExecutionActivityKind.STALL_RECOVERED: (
                ProgressEventKind.AGENT_STALL_RECOVERED
            ),
            AgentExecutionActivityKind.PROVIDER_STALLED: (
                ProgressEventKind.AGENT_PROVIDER_STALLED
            ),
            AgentExecutionActivityKind.FINALIZATION_PROGRESS: (
                ProgressEventKind.AGENT_FINALIZATION_PROGRESS
            ),
            AgentExecutionActivityKind.FINALIZATION_STALL_SUSPECTED: (
                ProgressEventKind.AGENT_FINALIZATION_STALL_SUSPECTED
            ),
            AgentExecutionActivityKind.FINALIZATION_STALL_RECOVERED: (
                ProgressEventKind.AGENT_FINALIZATION_STALL_RECOVERED
            ),
            AgentExecutionActivityKind.RESPONSE_FINALIZATION_STALLED: (
                ProgressEventKind.AGENT_RESPONSE_FINALIZATION_STALLED
            ),
        }[activity.kind]
        initialization_checkpoint = (
            "unknown"
            if activity.initialization_checkpoint is None
            else activity.initialization_checkpoint.value
        )
        stop_reason = (
            "unknown" if activity.stop_reason is None else activity.stop_reason.value
        )
        inactivity_seconds = (activity.inactivity_ms or 0) / 1000
        stall_grace_seconds = activity.stall_grace_seconds or 0
        silence_seconds = activity.silence_seconds or 0
        shutdown_grace_seconds = activity.shutdown_grace_seconds or 0
        tool_action = (
            "using"
            if activity.tool_action_class is None
            else activity.tool_action_class.value
        )
        tool_target = (
            "a sandboxed tool operation"
            if activity.tool_target_class is None
            else activity.tool_target_class.value.replace("_", " ")
        )
        tool_detail = (
            "" if activity.tool_detail is None else f" ({activity.tool_detail})"
        )
        message = {
            AgentExecutionActivityKind.INVOCATION_LAUNCHED: (
                f"{agent.label} invocation {attempt} entered the execution adapter"
            ),
            AgentExecutionActivityKind.INVOCATION_INITIALIZING: (
                f"{agent.label} is initializing an attributable OpenClaw turn"
            ),
            AgentExecutionActivityKind.INITIALIZATION_PROGRESS: (
                f"{agent.label} initialization verified {initialization_checkpoint}"
            ),
            AgentExecutionActivityKind.INITIALIZATION_LIVENESS_DEGRADED: (
                f"{agent.label} initialization observer is unavailable: "
                f"{activity.degradation_reason}"
            ),
            AgentExecutionActivityKind.INITIALIZATION_STALL_SUSPECTED: (
                f"{agent.label} initialization made no attributable progress for "
                f"{inactivity_seconds:.1f}s; waiting the final "
                f"{stall_grace_seconds:g}s diagnostic window "
                f"({activity.policy_source})"
            ),
            AgentExecutionActivityKind.INITIALIZATION_STALL_RECOVERED: (
                f"{agent.label} initialization advanced to "
                f"{initialization_checkpoint} during the grace window"
            ),
            AgentExecutionActivityKind.INITIALIZATION_STALLED: (
                f"{agent.label} initialization made no attributable progress for "
                f"{silence_seconds:g}s; a typed initialization stop follows"
            ),
            AgentExecutionActivityKind.INVOCATION_PROVIDER_WAIT: (
                f"{agent.label} current turn is attributable and is waiting for "
                f"the approved {activity.model} model"
            ),
            AgentExecutionActivityKind.INVOCATION_TOOL_ACTIVE: (
                f"{agent.label} has {activity.active_tool_count} attributable "
                "tool operation(s) active"
            ),
            AgentExecutionActivityKind.INVOCATION_FINALIZING_RESPONSE: (
                f"{agent.label} received a terminal model response and OpenClaw "
                "is finalizing the result"
            ),
            AgentExecutionActivityKind.INVOCATION_STOPPING: (
                f"{agent.label} is stopping ({stop_reason}); "
                f"{activity.action}; shutdown ceiling "
                f"{shutdown_grace_seconds:g}s"
            ),
            AgentExecutionActivityKind.INVOCATION_COLLECTING_EVIDENCE: (
                f"{agent.label} process has stopped; collecting invocation evidence "
                f"({stop_reason})"
            ),
            AgentExecutionActivityKind.INVOCATION_STOPPED: (
                f"{agent.label} invocation is stopped; process outcome, evidence, "
                f"and cleanup are known ({stop_reason})"
            ),
            AgentExecutionActivityKind.PROVIDER_STREAM: (
                f"{agent.label} received provider stream activity"
            ),
            AgentExecutionActivityKind.TOOL_STARTED: (
                f"{agent.label} started {tool_action} {tool_target}{tool_detail}"
            ),
            AgentExecutionActivityKind.TOOL_COMPLETED: (
                f"{agent.label} completed {tool_action} {tool_target}{tool_detail}"
            ),
            AgentExecutionActivityKind.LIVENESS_DEGRADED: (
                f"{agent.label} provider liveness is degraded: "
                f"{activity.degradation_reason}; SAT will preserve the call "
                "instead of guessing that silence means a stall"
            ),
            AgentExecutionActivityKind.STALL_SUSPECTED: (
                f"{agent.label} has produced no trusted activity for "
                f"{inactivity_seconds:.1f}s; SAT is checking its "
                "private stream and attributable tool state for another "
                f"{stall_grace_seconds:g}s before interruption "
                f"({activity.policy_source})"
            ),
            AgentExecutionActivityKind.STALL_RECOVERED: (
                f"{agent.label} provider activity recovered during the "
                f"{stall_grace_seconds:g}s grace period"
            ),
            AgentExecutionActivityKind.PROVIDER_STALLED: (
                f"{agent.label} provider remained silent for "
                f"{silence_seconds:g}s; stall is confirmed and the "
                "separate stopping transition follows"
            ),
            AgentExecutionActivityKind.FINALIZATION_PROGRESS: (
                f"{agent.label} OpenClaw result finalization made observable progress"
            ),
            AgentExecutionActivityKind.FINALIZATION_STALL_SUSPECTED: (
                f"{agent.label} OpenClaw result finalization made no observable "
                f"progress for {inactivity_seconds:.1f}s; waiting the final "
                f"{stall_grace_seconds:g}s diagnostic window "
                f"({activity.policy_source})"
            ),
            AgentExecutionActivityKind.FINALIZATION_STALL_RECOVERED: (
                f"{agent.label} OpenClaw result finalization resumed during the "
                f"{stall_grace_seconds:g}s grace period"
            ),
            AgentExecutionActivityKind.RESPONSE_FINALIZATION_STALLED: (
                f"{agent.label} OpenClaw received the terminal model response but "
                f"result finalization made no progress for {silence_seconds:g}s; "
                "the exact invocation will be stopped"
            ),
        }[activity.kind]
        phase = (
            activity.invocation_phase
            or {
                AgentExecutionActivityKind.INITIALIZATION_PROGRESS: (
                    InvocationPhase.INITIALIZING
                ),
                AgentExecutionActivityKind.INITIALIZATION_LIVENESS_DEGRADED: (
                    InvocationPhase.INITIALIZING
                ),
                AgentExecutionActivityKind.INITIALIZATION_STALL_SUSPECTED: (
                    InvocationPhase.INITIALIZING
                ),
                AgentExecutionActivityKind.INITIALIZATION_STALL_RECOVERED: (
                    InvocationPhase.INITIALIZING
                ),
                AgentExecutionActivityKind.INITIALIZATION_STALLED: (
                    InvocationPhase.INITIALIZING
                ),
                AgentExecutionActivityKind.PROVIDER_STREAM: (
                    InvocationPhase.TOOL_ACTIVE
                    if activity.active_tool_count
                    else InvocationPhase.PROVIDER_WAIT
                ),
                AgentExecutionActivityKind.TOOL_STARTED: (
                    InvocationPhase.TOOL_ACTIVE
                    if activity.active_tool_count
                    else InvocationPhase.PROVIDER_WAIT
                ),
                AgentExecutionActivityKind.TOOL_COMPLETED: (
                    InvocationPhase.TOOL_ACTIVE
                    if activity.active_tool_count
                    else InvocationPhase.PROVIDER_WAIT
                ),
                AgentExecutionActivityKind.LIVENESS_DEGRADED: (
                    InvocationPhase.PROVIDER_WAIT
                ),
                AgentExecutionActivityKind.STALL_SUSPECTED: (
                    InvocationPhase.PROVIDER_WAIT
                ),
                AgentExecutionActivityKind.STALL_RECOVERED: (
                    InvocationPhase.PROVIDER_WAIT
                ),
                AgentExecutionActivityKind.PROVIDER_STALLED: (
                    InvocationPhase.PROVIDER_WAIT
                ),
                AgentExecutionActivityKind.FINALIZATION_PROGRESS: (
                    InvocationPhase.FINALIZING_RESPONSE
                ),
                AgentExecutionActivityKind.FINALIZATION_STALL_SUSPECTED: (
                    InvocationPhase.FINALIZING_RESPONSE
                ),
                AgentExecutionActivityKind.FINALIZATION_STALL_RECOVERED: (
                    InvocationPhase.FINALIZING_RESPONSE
                ),
                AgentExecutionActivityKind.RESPONSE_FINALIZATION_STALLED: (
                    InvocationPhase.FINALIZING_RESPONSE
                ),
            }[activity.kind]
        )
        last_checkpoint: str | None = None
        if activity.initialization_checkpoint is not None:
            last_checkpoint = (
                "Verified OpenClaw initialization checkpoint "
                f"{activity.initialization_checkpoint.value}"
            )
        elif activity.kind is AgentExecutionActivityKind.PROVIDER_STREAM:
            last_checkpoint = "Observed attributable provider stream activity"
        elif activity.kind is AgentExecutionActivityKind.TOOL_COMPLETED:
            last_checkpoint = (
                "Observed attributable tool completion; criterion coverage remains "
                "unverified until typed assessment"
                if agent.capability is AgentCapability.REVIEW
                else (
                    f"Verified tool completion: {tool_action} "
                    f"{tool_target}{tool_detail}"
                )
            )
        elif activity.kind in {
            AgentExecutionActivityKind.INVOCATION_FINALIZING_RESPONSE,
            AgentExecutionActivityKind.FINALIZATION_PROGRESS,
            AgentExecutionActivityKind.FINALIZATION_STALL_RECOVERED,
        }:
            last_checkpoint = "Observed the terminal provider response"
        elif phase is InvocationPhase.STOPPING:
            last_checkpoint = f"Accepted typed stop reason {stop_reason}"
        elif phase is InvocationPhase.COLLECTING_EVIDENCE:
            last_checkpoint = "Observed the exact invocation process stop"
        elif phase is InvocationPhase.STOPPED:
            last_checkpoint = (
                "Collected process, session, submission, and cleanup facts"
            )
        next_checkpoint = {
            InvocationPhase.LAUNCHED: "Launch the exact OpenClaw process",
            InvocationPhase.INITIALIZING: (
                "Reach an attributable current turn or private provider stream"
            ),
            InvocationPhase.PROVIDER_WAIT: (
                "Observe provider response activity or an attributable tool operation"
            ),
            InvocationPhase.TOOL_ACTIVE: (
                "Observe completion of the active attributable tool operation"
            ),
            InvocationPhase.FINALIZING_RESPONSE: (
                "Observe OpenClaw result finalization or stop a confirmed no-progress "
                "stall"
            ),
            InvocationPhase.STOPPING: (
                "Confirm process exit or escalate at the shutdown ceiling"
            ),
            InvocationPhase.COLLECTING_EVIDENCE: (
                "Persist terminal invocation evidence and budget usage"
            ),
            InvocationPhase.STOPPED: (
                "Let the Controller validate output and choose the next approved action"
            ),
        }[phase]
        if agent.capability is AgentCapability.REVIEW and phase in {
            InvocationPhase.PROVIDER_WAIT,
            InvocationPhase.TOOL_ACTIVE,
        }:
            next_checkpoint = (
                "Submit a grounded typed assessment when coverage is sufficient; "
                "equivalent tool activity alone adds no criterion authority"
            )
        self._emit_activity(
            agent,
            kind=kind,
            message=message,
            attempt=attempt,
            model=activity.model,
            duration_ms=activity.elapsed_ms,
            presentation=activity.presentation,
            checkpoint=self._checkpoint_snapshot(  # type: ignore[attr-defined]
                agent,
                phase=phase,
                last_verified_checkpoint=last_checkpoint,
                next_controller_checkpoint=next_checkpoint,
                completed_tool_operations=activity.completed_tool_count,
            ),
        )
