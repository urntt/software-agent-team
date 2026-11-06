"""Controller-owned runtime adapter for one approved dynamic Agent DAG.

This package orchestrates the execution of individual AgentSpecs scheduled within an
adaptive TeamPlan, enforcing workspace isolation, deterministic quality gates,
telemetry capture, durable handoffs, and semantic response verification.
"""

from __future__ import annotations

from .runner import DynamicAgentRunner
from .types import (
    _SCHEDULER_SUMMARY_LIMIT,
    _UPSTREAM_SUMMARY_LIMIT,
    DynamicAgentRunnerError,
    DynamicQualityGate,
    GuidanceProvider,
    InvocationStopProvider,
    _bounded_artifact_summary,
    _CompletedAttemptDisposition,
    _IncompleteAttemptDisposition,
    _ReadOnlyContinuation,
    _system_clock,
    _UpstreamContinuation,
    _utc,
)

__all__ = [
    "_SCHEDULER_SUMMARY_LIMIT",
    "_UPSTREAM_SUMMARY_LIMIT",
    "DynamicAgentRunner",
    "DynamicAgentRunnerError",
    "DynamicQualityGate",
    "GuidanceProvider",
    "InvocationStopProvider",
    "_CompletedAttemptDisposition",
    "_IncompleteAttemptDisposition",
    "_ReadOnlyContinuation",
    "_UpstreamContinuation",
    "_bounded_artifact_summary",
    "_system_clock",
    "_utc",
]
