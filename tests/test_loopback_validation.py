"""Tests for the controlled OpenClaw transport validation oracle."""

from __future__ import annotations

from copy import deepcopy

from software_agent_team.loopback_validation import (
    MODEL,
    validate_matrix,
    validation_exit_code,
)


def outcome(scenario: str) -> dict[str, object]:
    status, stop_reason = {
        "stream": ("completed", "completed"),
        "recovery": ("completed", "completed"),
        "disconnect": ("process_failed", "process_failure"),
        "hang": ("provider_stalled", "provider_stall"),
    }[scenario]
    liveness = {
        "lease_started": True,
        "raw_stream_observed": True,
        "stalled": scenario == "hang",
        "stall_suspected_count": 1 if scenario == "hang" else 0,
        "stall_recovered_count": 0,
        "terminal_response_observed": scenario in {"stream", "recovery"},
    }
    server_events: list[dict[str, object]] = [{"kind": "request"}]
    if scenario == "recovery":
        liveness.update(
            {
                "stall_suspected_count": 2,
                "stall_recovered_count": 2,
            }
        )
        server_events.extend(
            [
                {"kind": "controller_stall_signal", "sequence": 1},
                {"kind": "controller_stall_signal", "sequence": 2},
            ]
        )
    return {
        "scenario": scenario,
        "status": status,
        "stop_reason": stop_reason,
        "response_text": '{"status":"ok"}' if status == "completed" else None,
        "exit_code": 0 if status == "completed" else 1,
        "provider": MODEL.split("/", 1)[0] if status == "completed" else None,
        "model": MODEL if status == "completed" else None,
        "liveness": liveness,
        "phases": ["provider_wait", "stopping", "collecting_evidence", "stopped"],
        "server_events": server_events,
        "requests_seen": 1,
        "cleanup_completed": True,
        "sandbox_containers_before_launch": [],
        "sandbox_cleanup": {
            "completed": True,
            "container_ids_after_cleanup": [],
        },
    }


def valid_outcomes() -> list[dict[str, object]]:
    return [outcome(name) for name in ("stream", "recovery", "disconnect", "hang")]


def test_complete_loopback_oracle_accepts_every_expected_scenario() -> None:
    validation = validate_matrix(valid_outcomes())

    assert validation["passed"] is True
    assert validation_exit_code(validation) == 0
    assert all(item["passed"] is True for item in validation["scenarios"])


def test_tool_rejection_oracle_requires_diagnostic_and_independent_work() -> None:
    sample = outcome("stream")
    sample.update(
        {
            "scenario": "tool-rejection",
            "requests_seen": 3,
            "tool_evidence_status": "captured",
            "runtime_rejections": [{"reason": "unknown_tool"}],
            "tool_calls": [
                {
                    "tool_name": "read",
                    "outcome": "succeeded",
                    "output_excerpt": "LOOPBACK_READ_OK",
                }
            ],
        }
    )
    assert validate_matrix([sample], expected_scenarios=("tool-rejection",))["passed"]
    for field, bad_value in (
        ("runtime_rejections", []),
        ("tool_calls", []),
        ("tool_evidence_status", "invalid"),
        ("requests_seen", 1),
    ):
        invalid = {**sample, field: bad_value}
        result = validate_matrix([invalid], expected_scenarios=("tool-rejection",))
        assert not result["passed"]
        assert validation_exit_code(result) == 2


def test_cleanup_success_cannot_hide_a_failed_recovery_outcome() -> None:
    outcomes = valid_outcomes()
    recovery = outcomes[1]
    recovery.update(
        {
            "status": "provider_stalled",
            "stop_reason": "provider_stall",
            "response_text": None,
            "exit_code": -9,
            "provider": None,
            "model": None,
        }
    )
    recovery["liveness"] = {
        **recovery["liveness"],
        "stalled": True,
        "stall_recovered_count": 1,
        "terminal_response_observed": False,
    }

    validation = validate_matrix(outcomes)

    assert validation["passed"] is False
    assert validation_exit_code(validation) == 2
    recovery_verdict = validation["scenarios"][1]
    assert recovery_verdict["passed"] is False
    assert any(
        "status expected 'completed'" in item for item in recovery_verdict["mismatches"]
    )
    assert any("matching recovery" in item for item in recovery_verdict["mismatches"])


def test_recovery_requires_controller_observation_driven_release() -> None:
    outcomes = valid_outcomes()
    recovery = outcomes[1]
    recovery["server_events"] = [{"kind": "request"}]

    validation = validate_matrix(outcomes)

    assert validation["passed"] is False
    assert any(
        "Controller observations" in mismatch
        for mismatch in validation["scenarios"][1]["mismatches"]
    )


def test_matrix_rejects_missing_duplicate_and_unexpected_scenarios() -> None:
    outcomes = valid_outcomes()
    outcomes.pop()
    outcomes.append(deepcopy(outcomes[0]))
    extra = deepcopy(outcomes[0])
    extra["scenario"] = "unexpected"
    outcomes.append(extra)

    validation = validate_matrix(outcomes)

    assert validation["passed"] is False
    assert validation["matrix_mismatches"] == [
        "scenario 'stream' expected exactly once, observed 2",
        "scenario 'hang' expected exactly once, observed 0",
        "unexpected scenarios: unexpected",
    ]
