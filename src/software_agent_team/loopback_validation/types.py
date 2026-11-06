"""Data structures, models, and constants for loopback validation.

Architecture & Intent / 架构与设计意图:
Defines the core data models, scenario constants, and safe coercion helpers
used across the controlled transport oracle.
- Scenario constants identify deterministic behaviors (stream, recovery, etc.).
- ScenarioValidation encapsulates verdict outcome and mismatch diagnostics.
- Helper functions provide defensive parsing for mappings and sequences.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# Pinned provider and model used across all loopback scenarios.
MODEL = "deepseek/deepseek-flash"

# Primary deterministic scenarios required for complete matrix validation.
SCENARIOS = ("stream", "recovery", "disconnect", "hang")

# Optional or specialized edge scenarios supported by the oracle.
OPTIONAL_SCENARIOS = (
    "tool-rejection",
    "continuation",
    "correction",
    "submission-contract",
    "tool-warning-submission",
)

# Oracle expected terminal execution status and stop reason per scenario:
# scenario_name -> (expected_status, expected_stop_reason)
_EXPECTED_TERMINAL = {
    "stream": ("completed", "completed"),
    "recovery": ("completed", "completed"),
    "disconnect": ("process_failed", "process_failure"),
    "hang": ("provider_stalled", "provider_stall"),
    "tool-rejection": ("completed", "completed"),
    "continuation": ("completed", "completed"),
    "correction": ("completed", "completed"),
    "submission-contract": ("completed", "completed"),
    "tool-warning-submission": ("completed", "completed"),
}

# The mandatory tail sequence of lifecycle phases for every clean exit.
_TERMINAL_PHASES = ("stopping", "collecting_evidence", "stopped")

# Standard fixed JSON response returned by the mock server on completed stream.
_EXPECTED_RESPONSE = '{"status":"ok"}'


@dataclass(frozen=True)
class ScenarioValidation:
    """Machine-readable verdict for one controlled transport scenario.

    Captures validation verdict for a single scenario including pass/fail status
    and any mismatch diagnostic messages.
    """

    scenario: str
    passed: bool
    mismatches: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        """Render the verdict without exposing transport content."""
        return {
            "scenario": self.scenario,
            "passed": self.passed,
            "mismatches": list(self.mismatches),
        }


def _mapping(value: object) -> Mapping[str, Any]:
    """Safely coerce any object into a string-keyed mapping."""
    return value if isinstance(value, Mapping) else {}


def _sequence(value: object) -> Sequence[Any]:
    """Safely coerce any object into a non-string sequence."""
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return value
    return ()
