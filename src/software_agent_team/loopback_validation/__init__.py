"""Validate the pinned OpenClaw transport against a controlled SSE endpoint.

Modularized subpackage architecture:
- types: Constants, models (ScenarioValidation), and coercion helpers.
- container_cleanup: Docker container inspection and deterministic session cleanup.
- server: Mock SSE server (ScenarioServer, ScenarioHandler, running_server).
- matrix: Scenario runner (execute_scenario), oracle verifier (validate_matrix,
  validate_scenario_outcome, validation_exit_code), and CLI entry point (main).
"""

from __future__ import annotations

from software_agent_team.loopback_validation.container_cleanup import (
    remove_sandbox_sessions,
    sandbox_container_ids,
)
from software_agent_team.loopback_validation.matrix import (
    _run_matrix,
    _serialize_invocation,
    _write_result,
    execute_scenario,
    main,
    validate_matrix,
    validate_scenario_outcome,
    validation_exit_code,
)
from software_agent_team.loopback_validation.server import (
    ScenarioHandler,
    ScenarioServer,
    running_server,
)
from software_agent_team.loopback_validation.types import (
    _EXPECTED_RESPONSE,
    _EXPECTED_TERMINAL,
    _TERMINAL_PHASES,
    MODEL,
    OPTIONAL_SCENARIOS,
    SCENARIOS,
    ScenarioValidation,
    _mapping,
    _sequence,
)

__all__ = [
    "MODEL",
    "OPTIONAL_SCENARIOS",
    "SCENARIOS",
    "_EXPECTED_RESPONSE",
    "_EXPECTED_TERMINAL",
    "_TERMINAL_PHASES",
    "ScenarioHandler",
    "ScenarioServer",
    "ScenarioValidation",
    "_mapping",
    "_run_matrix",
    "_sequence",
    "_serialize_invocation",
    "_write_result",
    "execute_scenario",
    "main",
    "remove_sandbox_sessions",
    "running_server",
    "sandbox_container_ids",
    "validate_matrix",
    "validate_scenario_outcome",
    "validation_exit_code",
]
