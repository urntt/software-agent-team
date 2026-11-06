"""Controlled mock SSE HTTP server for transport loopback validation.

Architecture & Intent / 架构与设计意图:
Simulates SSE endpoints responding with streaming chunks, simulated stall/recovery
handshakes, abrupt TCP disconnects, or hanging responses according to scenario rules.
- ScenarioServer: Threading HTTP server tracking events and controller stall handshakes.
- ScenarioHandler: HTTP handler that deterministically emulates upstream provider.
- running_server: Context manager managing background daemon thread lifecycle.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from software_agent_team.loopback_validation.types import (
    MODEL,
    _mapping,
    _sequence,
)


class ScenarioServer(ThreadingHTTPServer):
    """Loopback endpoint plus an observation-driven recovery handshake."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], scenario: str) -> None:
        super().__init__(address, ScenarioHandler)
        self.scenario = scenario
        self.requests_seen = 0
        self.started = time.monotonic()
        self.events: list[dict[str, object]] = []
        self._event_lock = threading.Lock()
        self._recovery_condition = threading.Condition()
        self._controller_stall_signals = 0
        self._stopping = False

    def record(self, kind: str, **details: object) -> None:
        """Record a content-free endpoint event."""
        event = {
            "elapsed_ms": round((time.monotonic() - self.started) * 1000),
            "kind": kind,
            **details,
        }
        with self._event_lock:
            self.events.append(event)

    def signal_controller_stall(self) -> None:
        """Release one recovery fragment after a real Controller observation."""
        with self._recovery_condition:
            self._controller_stall_signals += 1
            sequence = self._controller_stall_signals
            self._recovery_condition.notify_all()
        self.record("controller_stall_signal", sequence=sequence)

    def wait_for_controller_stall(self, sequence: int, timeout: float) -> bool:
        """Wait until the Controller has observed the requested suspicion."""
        with self._recovery_condition:
            return (
                self._recovery_condition.wait_for(
                    lambda: (
                        self._controller_stall_signals >= sequence or self._stopping
                    ),
                    timeout=timeout,
                )
                and self._controller_stall_signals >= sequence
            )

    def wait_until_stopping(self, timeout: float) -> None:
        """Keep a hang response open until validation teardown begins."""
        with self._recovery_condition:
            self._recovery_condition.wait_for(lambda: self._stopping, timeout=timeout)

    def begin_teardown(self) -> None:
        """Release endpoint workers without extending process teardown."""
        with self._recovery_condition:
            self._stopping = True
            self._recovery_condition.notify_all()


class ScenarioHandler(BaseHTTPRequestHandler):
    """Serve deterministic streaming, recovery, disconnect, and hang responses."""

    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        """Suppress default stderr HTTP request logging in test harnesses."""
        return

    def do_POST(self) -> None:
        """Handle incoming model inference POST requests under scenario rules."""
        server = self.server
        assert isinstance(server, ScenarioServer)
        server.requests_seen += 1
        server.record("request", path=self.path)
        length = int(self.headers.get("content-length", "0"))
        request = json.loads(self.rfile.read(length))
        if request.get("model") != MODEL.split("/", 1)[1]:
            server.record("unexpected_model")
            self.send_error(400)
            return
        if request.get("stream") is not True:
            server.record("non_stream_request")
            self.send_error(400)
            return

        if (
            server.scenario in {"submission-contract", "tool-warning-submission"}
            and server.requests_seen == 1
        ):
            names = [
                _mapping(item.get("function")).get("name")
                for item in _sequence(request.get("tools"))
                if isinstance(item, dict)
            ]
            choice = _mapping(_mapping(request.get("tool_choice")).get("function"))
            valid = (
                "sat_submit_artifact" in names
                and choice.get("name") == "sat_submit_artifact"
            )
            server.record("submission_contract", valid=valid, tool_names=names)
            if not valid:
                self.send_error(400, "bound submission tool was not declared")
                return

        if server.scenario == "disconnect":
            server.record("disconnect")
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return

        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.flush()
        server.record("headers_flushed")

        if server.scenario == "tool-rejection" and server.requests_seen <= 2:
            name, arguments = (
                ("sat_nonexistent_fixture_tool", {})
                if server.requests_seen == 1
                # The static read-only profile mounts the source at /agent;
                # /workspace is the separate sandbox working directory.
                else ("read", {"path": "/agent/fixture.txt"})
            )
            self._send_tool_call(name, arguments)
            return

        if server.scenario == "tool-warning-submission":
            if server.requests_seen == 1:
                self._send_tool_call("ls", {"path": "/agent/absent-warning-fixture"})
            else:
                self._send_tool_call(
                    "sat_submit_artifact", {"artifact": {"status": "ok"}}
                )
            return

        if server.scenario == "submission-contract" and server.requests_seen == 1:
            self._send_tool_call("sat_submit_artifact", {"artifact": {"status": "ok"}})
            return

        if server.scenario == "hang":
            self._send_chunk('{"status":')
            server.record("hang_started")
            server.wait_until_stopping(timeout=30)
            return

        fragments = ('{"status"', ':"ok"', "}")
        if server.scenario == "recovery":
            self._send_chunk(fragments[0])
            server.record("text_delta_flushed", sequence=1)
            for sequence, fragment in enumerate(fragments[1:], start=1):
                if not server.wait_for_controller_stall(sequence, timeout=30):
                    server.record("recovery_signal_timeout", sequence=sequence)
                    return
                self._send_chunk(fragment)
                server.record("text_delta_flushed", sequence=sequence + 1)
        else:
            for sequence, fragment in enumerate(fragments, start=1):
                self._send_chunk(fragment)
                server.record("text_delta_flushed", sequence=sequence)
                time.sleep(0.45)
        self._send_terminal()
        server.record("terminal_flushed")

    def _send_chunk(self, content: str) -> None:
        """Send a single assistant SSE text chunk."""
        payload = {
            "id": "loopback-validation",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": MODEL.split("/", 1)[1],
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": content},
                    "finish_reason": None,
                }
            ],
        }
        self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
        self.wfile.flush()

    def _send_tool_call(self, name: str, arguments: dict[str, object]) -> None:
        """Exercise real runtime rejection/pairing with fixed local-only inputs."""
        payload = {
            "id": "loopback-tool-validation",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": MODEL.split("/", 1)[1],
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": f"fixture-{name}",
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": json.dumps(arguments),
                                },
                            }
                        ],
                    },
                    "finish_reason": None,
                }
            ],
        }
        self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
        payload["choices"] = [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]
        payload["usage"] = {
            "prompt_tokens": 12,
            "completion_tokens": 5,
            "total_tokens": 17,
        }
        self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _send_terminal(self) -> None:
        """Send terminal stop chunk followed by the SSE DONE marker."""
        terminal = {
            "id": "loopback-validation",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": MODEL.split("/", 1)[1],
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 12,
                "completion_tokens": 5,
                "total_tokens": 17,
            },
        }
        self.wfile.write(f"data: {json.dumps(terminal)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


@contextmanager
def running_server(scenario: str) -> Iterator[ScenarioServer]:
    """Run and deterministically stop one loopback scenario endpoint."""
    server = ScenarioServer(("127.0.0.1", 0), scenario)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server
    finally:
        server.begin_teardown()
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
