"""Verify telemetry source, visible text selection, redaction, and bounds."""

from __future__ import annotations

import json

from software_agent_team.budgets import AgentBudget, AgentBudgetLedger, BudgetAuthority
from software_agent_team.terminal_metrics import TerminalRunMetrics
from software_agent_team.terminal_presentation import (
    display_text,
    invocation_preview,
    stream_preview,
    tool_preview,
)


def test_context_uses_latest_attributed_request_and_compaction_invalidates_it() -> None:
    def message(tokens):
        return {
            "type": "message",
            "message": {
                "role": "assistant",
                "provider": "provider",
                "model": "model",
                "usage": {"input": tokens, "cacheRead": 20, "cacheWrite": 3},
                "content": [
                    {"type": "thinking", "thinking": "PRIVATE REASONING"},
                    {"type": "text", "text": "Visible output"},
                ],
            },
        }

    records = [message(100), message(40)]
    result = invocation_preview(records, session_id="current", model="provider/model")
    assert result.context_input_tokens == 63
    assert result.model_text == "Visible output"
    assert "PRIVATE" not in result.model_dump_json()
    compacted = invocation_preview(
        [*records, {"type": "compaction"}], session_id="current", model="provider/model"
    )
    assert compacted.context_input_tokens is None
    mismatch = invocation_preview(records, session_id="current", model="other/model")
    assert mismatch.context_input_tokens is None and not mismatch.model_text


def test_stream_only_projects_current_session_visible_events_and_redacts(
    monkeypatch,
) -> None:
    key = "fixture-private-credential-123"
    monkeypatch.setenv("EXAMPLE_API_KEY", key)
    records = [
        {
            "sessionId": "other",
            "event": "assistant_text_stream",
            "evtType": "text_delta",
            "content": "OTHER SESSION",
        },
        {
            "sessionId": "current",
            "event": "assistant_thinking_stream",
            "evtType": "thinking_delta",
            "content": "PRIVATE THINKING",
        },
        {
            "sessionId": "current",
            "event": "assistant_text_stream",
            "evtType": "text_delta",
            "content": "Working " + key + "\x1b[2J",
        },
    ]
    raw = "\n".join(json.dumps(record) for record in records).encode()
    text = display_text(stream_preview(raw, session_id="current"))
    assert "Working" in text and "[redacted]" in text
    assert key not in text and "OTHER SESSION" not in text and "PRIVATE" not in text
    assert "\x1b" not in text
    assert len(display_text("a" * 100000, limit=1000)) == 1000
    assert "hidden" not in display_text("hello <think>hidden</think> world")
    tool = tool_preview(
        "exec",
        {"command": "python app.py --api-key=sk-examplekey"},
        result="password=private",
    )
    assert (
        "sk-examplekey" not in tool.model_dump_json()
        and "password=[redacted]" in tool.result
    )
    submission = tool_preview(
        "sat_submit_artifact", {"artifact": "OPAQUE CONTENT"}, result="OPAQUE RESULT"
    )
    assert "OPAQUE" not in submission.model_dump_json()


def test_global_clock_updates_without_events_and_context_is_not_ledger_total() -> None:
    clock = [10.0]
    ledger = AgentBudgetLedger(
        AgentBudget(authority=BudgetAuthority.USER_TASK, max_estimated_cost_usd="1")
    )
    metrics = TerminalRunMetrics(
        ledger=ledger,
        model_windows={"provider/model": 8192},
        monotonic=lambda: clock[0],
    )
    metrics.observe(
        "writer",
        "provider/model",
        invocation_preview([], session_id="one", model="provider/model"),
    )
    clock[0] = 17.0
    lines = "\n".join(metrics())
    assert "00:00:07 elapsed" in lines
    assert "unavailable / 8192" in lines
    assert "Git · unavailable" in lines
