"""Event kinds, categories, visibilities, and constants for controller events."""

from __future__ import annotations

import re
from enum import StrEnum

RUN_EVENT_SCHEMA_VERSION = 6
MINIMUM_READABLE_RUN_EVENT_SCHEMA_VERSION = 2
EVENTS_DIRECTORY = "events"
EVENT_FILENAME_PATTERN = re.compile(r"^(?P<sequence>[0-9]{6})\.json$")
DEFAULT_PROGRESS_HEARTBEAT_SECONDS = 10.0
DEFAULT_LIVE_REFRESH_SECONDS = 1.0
MAX_PROGRESS_SUMMARY_CHARACTERS = 500


class ProgressEventKind(StrEnum):
    """Stable event identities emitted by the deterministic controller."""

    RUN_STARTED = "run_started"
    WORKSPACE_READY = "workspace_ready"
    AGENT_QUEUED = "agent_queued"
    AGENT_READY = "agent_ready"
    AGENT_STARTED = "agent_started"
    AGENT_INVOCATION_LAUNCHED = "agent_invocation_launched"
    AGENT_INITIALIZING = "agent_initializing"
    AGENT_INITIALIZATION_PROGRESS = "agent_initialization_progress"
    AGENT_INITIALIZATION_LIVENESS_DEGRADED = "agent_initialization_liveness_degraded"
    AGENT_INITIALIZATION_STALL_SUSPECTED = "agent_initialization_stall_suspected"
    AGENT_INITIALIZATION_STALL_RECOVERED = "agent_initialization_stall_recovered"
    AGENT_INITIALIZATION_STALLED = "agent_initialization_stalled"
    AGENT_WAITING_PROVIDER = "agent_waiting_provider"
    AGENT_PROVIDER_ACTIVITY = "agent_provider_activity"
    AGENT_TOOL_ACTIVE = "agent_tool_active"
    AGENT_TOOL_STARTED = "agent_tool_started"
    AGENT_TOOL_COMPLETED = "agent_tool_completed"
    AGENT_FINALIZING_RESPONSE = "agent_finalizing_response"
    AGENT_FINALIZATION_PROGRESS = "agent_finalization_progress"
    AGENT_FINALIZATION_STALL_SUSPECTED = "agent_finalization_stall_suspected"
    AGENT_FINALIZATION_STALL_RECOVERED = "agent_finalization_stall_recovered"
    AGENT_RESPONSE_FINALIZATION_STALLED = "agent_response_finalization_stalled"
    AGENT_LIVENESS_DEGRADED = "agent_liveness_degraded"
    AGENT_STALL_SUSPECTED = "agent_stall_suspected"
    AGENT_STALL_RECOVERED = "agent_stall_recovered"
    AGENT_PROVIDER_STALLED = "agent_provider_stalled"
    AGENT_STOPPING = "agent_stopping"
    AGENT_COLLECTING_EVIDENCE = "agent_collecting_evidence"
    AGENT_STOPPED = "agent_stopped"
    AGENT_INVOCATION_COMPLETED = "agent_invocation_completed"
    MODEL_ROUTE_SWITCHED = "model_route_switched"
    AGENT_COMPLETED = "agent_completed"
    AGENT_RETRY = "agent_retry"
    AGENT_FAILED = "agent_failed"
    AGENT_SKIPPED = "agent_skipped"
    AGENT_PAUSED = "agent_paused"
    AGENT_RESUMED = "agent_resumed"
    AGENT_INTERRUPTED = "agent_interrupted"
    AGENT_CANCELLED = "agent_cancelled"
    SNAPSHOT_VERIFIED = "snapshot_verified"
    QUALITY_GATES_STARTED = "quality_gates_started"
    QUALITY_GATE_COMPLETED = "quality_gate_completed"
    QUALITY_GATE_PASSED = "quality_gate_passed"
    QUALITY_GATE_FAILED = "quality_gate_failed"
    DECISION_RECORDED = "decision_recorded"
    CONTROL_RECEIVED = "control_received"
    CONTROL_APPLIED = "control_applied"
    CONTROL_REJECTED = "control_rejected"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"
    RUN_CANCELLED = "run_cancelled"


class RunEventCategory(StrEnum):
    """User-facing grouping independent from a renderer layout."""

    LIFECYCLE = "lifecycle"
    AGENT = "agent"
    GIT = "git"
    QUALITY_GATE = "quality_gate"
    DECISION = "decision"


class RunEventVisibility(StrEnum):
    """Lowest detail level at which one safe event should be rendered."""

    COMPACT = "compact"
    STANDARD = "standard"
    DETAILED = "detailed"


class TerminalProgressDisplay(StrEnum):
    """Requested terminal projection; unsafe live output always falls back to log."""

    AUTO = "auto"
    LIVE = "live"
    LOG = "log"


class TerminalColorMode(StrEnum):
    """Requested color behavior within a capable interactive terminal."""

    AUTO = "auto"
    ALWAYS = "always"
    NEVER = "never"


class AgentRunState(StrEnum):
    """Controller-observed state for one run-scoped Agent."""

    QUEUED = "queued"
    READY = "ready"
    RUNNING = "running"
    LAUNCHED = "launched"
    INITIALIZING = "initializing"
    WAITING_PROVIDER = "waiting_provider"
    TOOL_ACTIVE = "tool_active"
    FINALIZING_RESPONSE = "finalizing_response"
    STOPPING = "stopping"
    COLLECTING_EVIDENCE = "collecting_evidence"
    STOPPED = "stopped"
    WAITING_DEPENDENCY = "waiting_dependency"
    BLOCKED = "blocked"
    PAUSED = "paused"
    COMPLETED = "completed"
    WAITING_REPAIR = "waiting_repair"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"
    FAILED = "failed"


class RunEventSource(StrEnum):
    """Authority behind a bounded event summary."""

    CONTROLLER = "controller"
    AGENT_SAFE_SUMMARY = "agent_safe_summary"


class RunEventReferenceKind(StrEnum):
    """Typed evidence that a RunEvent may point to without embedding it."""

    ARTIFACT = "artifact"
    HANDOFF = "handoff"
    QUALITY_GATE = "quality_gate"
    GIT = "git"
    BUDGET = "budget"
    MODEL_ROUTE = "model_route"
    CONTROL_COMMAND = "control_command"


_EVENT_METADATA: dict[
    ProgressEventKind,
    tuple[RunEventCategory, RunEventVisibility, AgentRunState | None],
] = {
    ProgressEventKind.RUN_STARTED: (
        RunEventCategory.LIFECYCLE,
        RunEventVisibility.COMPACT,
        None,
    ),
    ProgressEventKind.WORKSPACE_READY: (
        RunEventCategory.LIFECYCLE,
        RunEventVisibility.STANDARD,
        None,
    ),
    ProgressEventKind.AGENT_QUEUED: (
        RunEventCategory.AGENT,
        RunEventVisibility.STANDARD,
        AgentRunState.QUEUED,
    ),
    ProgressEventKind.AGENT_READY: (
        RunEventCategory.AGENT,
        RunEventVisibility.STANDARD,
        AgentRunState.READY,
    ),
    ProgressEventKind.AGENT_STARTED: (
        RunEventCategory.AGENT,
        RunEventVisibility.STANDARD,
        AgentRunState.RUNNING,
    ),
    ProgressEventKind.AGENT_INVOCATION_LAUNCHED: (
        RunEventCategory.AGENT,
        RunEventVisibility.STANDARD,
        AgentRunState.LAUNCHED,
    ),
    ProgressEventKind.AGENT_INITIALIZING: (
        RunEventCategory.AGENT,
        RunEventVisibility.STANDARD,
        AgentRunState.INITIALIZING,
    ),
    ProgressEventKind.AGENT_INITIALIZATION_PROGRESS: (
        RunEventCategory.AGENT,
        RunEventVisibility.DETAILED,
        AgentRunState.INITIALIZING,
    ),
    ProgressEventKind.AGENT_INITIALIZATION_LIVENESS_DEGRADED: (
        RunEventCategory.AGENT,
        RunEventVisibility.COMPACT,
        AgentRunState.INITIALIZING,
    ),
    ProgressEventKind.AGENT_INITIALIZATION_STALL_SUSPECTED: (
        RunEventCategory.AGENT,
        RunEventVisibility.COMPACT,
        AgentRunState.INITIALIZING,
    ),
    ProgressEventKind.AGENT_INITIALIZATION_STALL_RECOVERED: (
        RunEventCategory.AGENT,
        RunEventVisibility.STANDARD,
        AgentRunState.INITIALIZING,
    ),
    ProgressEventKind.AGENT_INITIALIZATION_STALLED: (
        RunEventCategory.AGENT,
        RunEventVisibility.COMPACT,
        AgentRunState.INITIALIZING,
    ),
    ProgressEventKind.AGENT_WAITING_PROVIDER: (
        RunEventCategory.AGENT,
        RunEventVisibility.STANDARD,
        AgentRunState.WAITING_PROVIDER,
    ),
    ProgressEventKind.AGENT_PROVIDER_ACTIVITY: (
        RunEventCategory.AGENT,
        RunEventVisibility.DETAILED,
        AgentRunState.WAITING_PROVIDER,
    ),
    ProgressEventKind.AGENT_TOOL_ACTIVE: (
        RunEventCategory.AGENT,
        RunEventVisibility.STANDARD,
        AgentRunState.TOOL_ACTIVE,
    ),
    ProgressEventKind.AGENT_TOOL_STARTED: (
        RunEventCategory.AGENT,
        RunEventVisibility.STANDARD,
        AgentRunState.RUNNING,
    ),
    ProgressEventKind.AGENT_TOOL_COMPLETED: (
        RunEventCategory.AGENT,
        RunEventVisibility.STANDARD,
        AgentRunState.RUNNING,
    ),
    ProgressEventKind.AGENT_FINALIZING_RESPONSE: (
        RunEventCategory.AGENT,
        RunEventVisibility.STANDARD,
        AgentRunState.FINALIZING_RESPONSE,
    ),
    ProgressEventKind.AGENT_FINALIZATION_PROGRESS: (
        RunEventCategory.AGENT,
        RunEventVisibility.DETAILED,
        AgentRunState.FINALIZING_RESPONSE,
    ),
    ProgressEventKind.AGENT_FINALIZATION_STALL_SUSPECTED: (
        RunEventCategory.AGENT,
        RunEventVisibility.COMPACT,
        AgentRunState.FINALIZING_RESPONSE,
    ),
    ProgressEventKind.AGENT_FINALIZATION_STALL_RECOVERED: (
        RunEventCategory.AGENT,
        RunEventVisibility.STANDARD,
        AgentRunState.FINALIZING_RESPONSE,
    ),
    ProgressEventKind.AGENT_RESPONSE_FINALIZATION_STALLED: (
        RunEventCategory.AGENT,
        RunEventVisibility.COMPACT,
        AgentRunState.FINALIZING_RESPONSE,
    ),
    ProgressEventKind.AGENT_LIVENESS_DEGRADED: (
        RunEventCategory.AGENT,
        RunEventVisibility.COMPACT,
        AgentRunState.WAITING_PROVIDER,
    ),
    ProgressEventKind.AGENT_STALL_SUSPECTED: (
        RunEventCategory.AGENT,
        RunEventVisibility.COMPACT,
        AgentRunState.WAITING_PROVIDER,
    ),
    ProgressEventKind.AGENT_STALL_RECOVERED: (
        RunEventCategory.AGENT,
        RunEventVisibility.STANDARD,
        AgentRunState.WAITING_PROVIDER,
    ),
    ProgressEventKind.AGENT_PROVIDER_STALLED: (
        RunEventCategory.AGENT,
        RunEventVisibility.COMPACT,
        AgentRunState.WAITING_PROVIDER,
    ),
    ProgressEventKind.AGENT_STOPPING: (
        RunEventCategory.AGENT,
        RunEventVisibility.COMPACT,
        AgentRunState.STOPPING,
    ),
    ProgressEventKind.AGENT_COLLECTING_EVIDENCE: (
        RunEventCategory.AGENT,
        RunEventVisibility.COMPACT,
        AgentRunState.COLLECTING_EVIDENCE,
    ),
    ProgressEventKind.AGENT_STOPPED: (
        RunEventCategory.AGENT,
        RunEventVisibility.COMPACT,
        AgentRunState.STOPPED,
    ),
    ProgressEventKind.AGENT_INVOCATION_COMPLETED: (
        RunEventCategory.AGENT,
        RunEventVisibility.STANDARD,
        AgentRunState.RUNNING,
    ),
    ProgressEventKind.MODEL_ROUTE_SWITCHED: (
        RunEventCategory.AGENT,
        RunEventVisibility.COMPACT,
        AgentRunState.WAITING_PROVIDER,
    ),
    ProgressEventKind.AGENT_COMPLETED: (
        RunEventCategory.AGENT,
        RunEventVisibility.STANDARD,
        AgentRunState.COMPLETED,
    ),
    ProgressEventKind.AGENT_RETRY: (
        RunEventCategory.AGENT,
        RunEventVisibility.COMPACT,
        AgentRunState.WAITING_REPAIR,
    ),
    ProgressEventKind.AGENT_FAILED: (
        RunEventCategory.AGENT,
        RunEventVisibility.COMPACT,
        AgentRunState.FAILED,
    ),
    ProgressEventKind.AGENT_SKIPPED: (
        RunEventCategory.AGENT,
        RunEventVisibility.STANDARD,
        AgentRunState.BLOCKED,
    ),
    ProgressEventKind.AGENT_PAUSED: (
        RunEventCategory.AGENT,
        RunEventVisibility.STANDARD,
        AgentRunState.PAUSED,
    ),
    ProgressEventKind.AGENT_RESUMED: (
        RunEventCategory.AGENT,
        RunEventVisibility.STANDARD,
        AgentRunState.WAITING_DEPENDENCY,
    ),
    ProgressEventKind.AGENT_INTERRUPTED: (
        RunEventCategory.AGENT,
        RunEventVisibility.COMPACT,
        AgentRunState.INTERRUPTED,
    ),
    ProgressEventKind.AGENT_CANCELLED: (
        RunEventCategory.AGENT,
        RunEventVisibility.STANDARD,
        AgentRunState.CANCELLED,
    ),
    ProgressEventKind.SNAPSHOT_VERIFIED: (
        RunEventCategory.GIT,
        RunEventVisibility.STANDARD,
        None,
    ),
    ProgressEventKind.QUALITY_GATES_STARTED: (
        RunEventCategory.QUALITY_GATE,
        RunEventVisibility.STANDARD,
        None,
    ),
    ProgressEventKind.QUALITY_GATE_COMPLETED: (
        RunEventCategory.QUALITY_GATE,
        RunEventVisibility.STANDARD,
        None,
    ),
    ProgressEventKind.QUALITY_GATE_PASSED: (
        RunEventCategory.QUALITY_GATE,
        RunEventVisibility.STANDARD,
        None,
    ),
    ProgressEventKind.QUALITY_GATE_FAILED: (
        RunEventCategory.QUALITY_GATE,
        RunEventVisibility.STANDARD,
        None,
    ),
    ProgressEventKind.DECISION_RECORDED: (
        RunEventCategory.DECISION,
        RunEventVisibility.COMPACT,
        None,
    ),
    ProgressEventKind.CONTROL_RECEIVED: (
        RunEventCategory.DECISION,
        RunEventVisibility.COMPACT,
        None,
    ),
    ProgressEventKind.CONTROL_APPLIED: (
        RunEventCategory.DECISION,
        RunEventVisibility.COMPACT,
        None,
    ),
    ProgressEventKind.CONTROL_REJECTED: (
        RunEventCategory.DECISION,
        RunEventVisibility.COMPACT,
        None,
    ),
    ProgressEventKind.RUN_COMPLETED: (
        RunEventCategory.LIFECYCLE,
        RunEventVisibility.COMPACT,
        None,
    ),
    ProgressEventKind.RUN_FAILED: (
        RunEventCategory.LIFECYCLE,
        RunEventVisibility.COMPACT,
        None,
    ),
    ProgressEventKind.RUN_CANCELLED: (
        RunEventCategory.LIFECYCLE,
        RunEventVisibility.COMPACT,
        None,
    ),
}

_ATTEMPT_EVENT_KINDS = {
    ProgressEventKind.AGENT_STARTED,
    ProgressEventKind.AGENT_INVOCATION_LAUNCHED,
    ProgressEventKind.AGENT_INITIALIZING,
    ProgressEventKind.AGENT_INITIALIZATION_PROGRESS,
    ProgressEventKind.AGENT_INITIALIZATION_LIVENESS_DEGRADED,
    ProgressEventKind.AGENT_INITIALIZATION_STALL_SUSPECTED,
    ProgressEventKind.AGENT_INITIALIZATION_STALL_RECOVERED,
    ProgressEventKind.AGENT_INITIALIZATION_STALLED,
    ProgressEventKind.AGENT_WAITING_PROVIDER,
    ProgressEventKind.AGENT_PROVIDER_ACTIVITY,
    ProgressEventKind.AGENT_TOOL_ACTIVE,
    ProgressEventKind.AGENT_TOOL_STARTED,
    ProgressEventKind.AGENT_TOOL_COMPLETED,
    ProgressEventKind.AGENT_FINALIZING_RESPONSE,
    ProgressEventKind.AGENT_FINALIZATION_PROGRESS,
    ProgressEventKind.AGENT_FINALIZATION_STALL_SUSPECTED,
    ProgressEventKind.AGENT_FINALIZATION_STALL_RECOVERED,
    ProgressEventKind.AGENT_RESPONSE_FINALIZATION_STALLED,
    ProgressEventKind.AGENT_LIVENESS_DEGRADED,
    ProgressEventKind.AGENT_STALL_SUSPECTED,
    ProgressEventKind.AGENT_STALL_RECOVERED,
    ProgressEventKind.AGENT_PROVIDER_STALLED,
    ProgressEventKind.AGENT_STOPPING,
    ProgressEventKind.AGENT_COLLECTING_EVIDENCE,
    ProgressEventKind.AGENT_STOPPED,
    ProgressEventKind.AGENT_INVOCATION_COMPLETED,
    ProgressEventKind.MODEL_ROUTE_SWITCHED,
    ProgressEventKind.AGENT_COMPLETED,
    ProgressEventKind.AGENT_RETRY,
    ProgressEventKind.AGENT_FAILED,
    ProgressEventKind.AGENT_INTERRUPTED,
}

_RUN_EVENT_SCHEMA_FOUR_KINDS = {
    ProgressEventKind.AGENT_FINALIZING_RESPONSE,
    ProgressEventKind.AGENT_FINALIZATION_PROGRESS,
    ProgressEventKind.AGENT_FINALIZATION_STALL_SUSPECTED,
    ProgressEventKind.AGENT_FINALIZATION_STALL_RECOVERED,
    ProgressEventKind.AGENT_RESPONSE_FINALIZATION_STALLED,
}

_VISIBILITY_RANK = {
    RunEventVisibility.COMPACT: 0,
    RunEventVisibility.STANDARD: 1,
    RunEventVisibility.DETAILED: 2,
}
