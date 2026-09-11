"""Validate the pinned OpenClaw transport against a controlled SSE endpoint."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from software_agent_team.artifacts import AgentRole, ArtifactKind
from software_agent_team.execution import (
    AgentExecutionActivityKind,
    AgentExecutionRequest,
    AgentExecutionResult,
    OpenClawSubprocessExecutor,
    ProviderLivenessPolicy,
    ResponseFinalizationPolicy,
)
from software_agent_team.openclaw_runtime import isolated_openclaw_environment
from software_agent_team.quality_gates import load_quality_gate_configuration
from software_agent_team.runtime_configuration import materialize_run_configuration
from software_agent_team.teams import AgentCapability, load_team_manifest

MODEL = "deepseek/deepseek-flash"
SCENARIOS = ("stream", "recovery", "disconnect", "hang")
OPTIONAL_SCENARIOS = ("tool-rejection", "continuation", "correction")
_EXPECTED_TERMINAL = {
    "stream": ("completed", "completed"),
    "recovery": ("completed", "completed"),
    "disconnect": ("process_failed", "process_failure"),
    "hang": ("provider_stalled", "provider_stall"),
    "tool-rejection": ("completed", "completed"),
    "continuation": ("completed", "completed"),
    "correction": ("completed", "completed"),
}
_TERMINAL_PHASES = ("stopping", "collecting_evidence", "stopped")
_EXPECTED_RESPONSE = '{"status":"ok"}'


@dataclass(frozen=True)
class ScenarioValidation:
    """Machine-readable verdict for one controlled transport scenario."""

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
    return value if isinstance(value, Mapping) else {}


def _sequence(value: object) -> Sequence[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return value
    return ()


def validate_scenario_outcome(outcome: Mapping[str, object]) -> ScenarioValidation:
    """Compare one result with the complete oracle for its declared scenario."""

    scenario_value = outcome.get("scenario")
    scenario = scenario_value if isinstance(scenario_value, str) else "<missing>"
    mismatches: list[str] = []
    expected = _EXPECTED_TERMINAL.get(scenario)
    if expected is None:
        return ScenarioValidation(
            scenario=scenario,
            passed=False,
            mismatches=(f"unknown scenario: {scenario}",),
        )

    expected_status, expected_reason = expected
    if outcome.get("status") != expected_status:
        mismatches.append(
            f"status expected {expected_status!r}, got {outcome.get('status')!r}"
        )
    if outcome.get("stop_reason") != expected_reason:
        mismatches.append(
            "stop_reason expected "
            f"{expected_reason!r}, got {outcome.get('stop_reason')!r}"
        )

    exit_code = outcome.get("exit_code")
    if expected_status == "completed":
        if exit_code != 0:
            mismatches.append(f"exit_code expected 0, got {exit_code!r}")
        if outcome.get("response_text") != _EXPECTED_RESPONSE:
            mismatches.append("completed response_text did not match the fixture")
        if outcome.get("provider") != MODEL.split("/", 1)[0]:
            mismatches.append("completed provider identity did not match the fixture")
        if outcome.get("model") != MODEL:
            mismatches.append("completed model identity did not match the fixture")
    elif not isinstance(exit_code, int) or exit_code == 0:
        mismatches.append(f"failure exit_code must be non-zero, got {exit_code!r}")

    phases = tuple(str(item) for item in _sequence(outcome.get("phases")))
    if phases[-len(_TERMINAL_PHASES) :] != _TERMINAL_PHASES:
        mismatches.append(
            "terminal phases must end with stopping, collecting_evidence, stopped"
        )
    if outcome.get("cleanup_completed") is not True:
        mismatches.append("lifecycle cleanup_completed must be true")
    if outcome.get("sandbox_containers_before_launch") != []:
        mismatches.append(
            "scenario did not start with an empty exact sandbox inventory"
        )
    sandbox_cleanup = _mapping(outcome.get("sandbox_cleanup"))
    if sandbox_cleanup.get("completed") is not True:
        mismatches.append("exact sandbox cleanup must complete")
    if sandbox_cleanup.get("container_ids_after_cleanup") != []:
        mismatches.append("exact sandbox inventory must be empty after cleanup")
    requests_seen = outcome.get("requests_seen")
    if not isinstance(requests_seen, int) or requests_seen < 1:
        mismatches.append("loopback endpoint must observe at least one request")

    liveness = _mapping(outcome.get("liveness"))
    if liveness.get("lease_started") is not True:
        mismatches.append("provider liveness lease did not start")
    if liveness.get("raw_stream_observed") is not True:
        mismatches.append("private provider stream was not observed")

    suspected = liveness.get("stall_suspected_count")
    recovered = liveness.get("stall_recovered_count")
    if scenario == "stream":
        if liveness.get("stalled") is not False:
            mismatches.append("productive stream was marked stalled")
        if suspected != 0 or recovered != 0:
            mismatches.append("productive stream unexpectedly entered stall grace")
        if liveness.get("terminal_response_observed") is not True:
            mismatches.append("productive stream did not observe a terminal response")
    elif scenario == "recovery":
        if liveness.get("stalled") is not False:
            mismatches.append("recovery scenario remained stalled")
        if not isinstance(suspected, int) or suspected < 2:
            mismatches.append("recovery scenario must enter stall grace at least twice")
        if recovered != suspected:
            mismatches.append("every recovery suspicion must have a matching recovery")
        if liveness.get("terminal_response_observed") is not True:
            mismatches.append("recovery scenario did not observe a terminal response")
        event_kinds = [
            _mapping(item).get("kind")
            for item in _sequence(outcome.get("server_events"))
        ]
        if event_kinds.count("controller_stall_signal") < 2:
            mismatches.append(
                "recovery activity was not released by two Controller observations"
            )
        if "recovery_signal_timeout" in event_kinds:
            mismatches.append("recovery endpoint timed out waiting for the Controller")
    elif scenario == "disconnect":
        if liveness.get("stalled") is not False:
            mismatches.append("network disconnect was misclassified as provider stall")
    elif scenario == "hang":
        if liveness.get("stalled") is not True:
            mismatches.append("permanent silence did not produce provider stall")
        if not isinstance(suspected, int) or suspected < 1:
            mismatches.append("permanent silence did not enter stall grace")
        if recovered != 0:
            mismatches.append("permanent silence recorded a false recovery")
    elif scenario == "tool-rejection":
        if liveness.get("degradation_reason") is not None:
            mismatches.append("runtime rejection degraded session attribution")
        if outcome.get("tool_evidence_status") != "captured":
            mismatches.append("paired tool evidence was not captured")
        rejections = _sequence(outcome.get("runtime_rejections"))
        calls = _sequence(outcome.get("tool_calls"))
        if (
            len(rejections) != 1
            or _mapping(rejections[0]).get("reason") != "unknown_tool"
        ):
            mismatches.append("expected one unknown-tool runtime diagnostic")
        if len(calls) != 1 or (
            _mapping(calls[0]).get("tool_name") != "read"
            or _mapping(calls[0]).get("outcome") != "succeeded"
            or "LOOPBACK_READ_OK" not in str(_mapping(calls[0]).get("output_excerpt"))
        ):
            mismatches.append("expected independent successful fixture read evidence")
        if requests_seen != 3:
            mismatches.append("expected rejection, read, and final response requests")
    elif scenario == "continuation":
        invocations = _sequence(outcome.get("invocations"))
        if len(invocations) != 2:
            mismatches.append("continuation must record exactly two invocations")
        else:
            for index, invocation in enumerate(invocations, start=1):
                item = _mapping(invocation)
                if item.get("status") != "completed" or item.get("exit_code") != 0:
                    mismatches.append(
                        f"continuation invocation {index} did not complete"
                    )
                initialization = _mapping(item.get("initialization"))
                if initialization.get("mode") != "enforced":
                    mismatches.append(
                        "continuation invocation "
                        f"{index} lacked enforced initialization"
                    )
                checkpoints = _sequence(initialization.get("checkpoints"))
                if index == 1 and not {
                    "current_turn",
                    "provider_stream",
                }.intersection(checkpoints):
                    mismatches.append(
                        "continuation invocation 1 lacked attributable readiness"
                    )
                if index == 2 and "current_turn" not in checkpoints:
                    mismatches.append(
                        "continuation invocation 2 lacked a new current turn"
                    )
            second_initialization = _mapping(
                _mapping(invocations[1]).get("initialization")
            )
            if second_initialization.get("baseline_checkpoint") != "current_turn":
                mismatches.append(
                    "continuation did not start from the existing current turn"
                )
            matching_turn_count = second_initialization.get(
                "baseline_matching_turn_count"
            )
            if not isinstance(matching_turn_count, int) or matching_turn_count < 1:
                mismatches.append(
                    "continuation did not preserve the matching-turn baseline"
                )
        if requests_seen != 2:
            mismatches.append("continuation endpoint must observe exactly two requests")
    elif scenario == "correction":
        invocations = _sequence(outcome.get("invocations"))
        if len(invocations) != 2:
            mismatches.append("correction must record exactly two invocations")
        else:
            sessions = tuple(
                _mapping(invocation).get("session_key") for invocation in invocations
            )
            if (
                not all(isinstance(session, str) for session in sessions)
                or sessions[0] == sessions[1]
                or not str(sessions[1]).endswith("-g2")
            ):
                mismatches.append(
                    "correction must use a distinct second session generation"
                )
            for index, invocation in enumerate(invocations, start=1):
                item = _mapping(invocation)
                if item.get("status") != "completed" or item.get("exit_code") != 0:
                    mismatches.append(f"correction invocation {index} did not complete")
                initialization = _mapping(item.get("initialization"))
                if initialization.get("mode") != "enforced":
                    mismatches.append(
                        f"correction invocation {index} lacked enforced initialization"
                    )
                if not {"current_turn", "provider_stream"}.intersection(
                    _sequence(initialization.get("checkpoints"))
                ):
                    mismatches.append(
                        f"correction invocation {index} lacked attributable readiness"
                    )
        if requests_seen != 2:
            mismatches.append("correction endpoint must observe exactly two requests")

    return ScenarioValidation(
        scenario=scenario,
        passed=not mismatches,
        mismatches=tuple(mismatches),
    )


def validate_matrix(
    outcomes: Sequence[Mapping[str, object]],
    *,
    expected_scenarios: Sequence[str] = SCENARIOS,
) -> dict[str, object]:
    """Validate presence, uniqueness, and the typed oracle for every scenario."""

    expected = tuple(expected_scenarios)
    observed_names = [outcome.get("scenario") for outcome in outcomes]
    matrix_mismatches: list[str] = []
    for scenario in expected:
        count = observed_names.count(scenario)
        if count != 1:
            matrix_mismatches.append(
                f"scenario {scenario!r} expected exactly once, observed {count}"
            )
    unexpected = sorted(
        name
        for name in observed_names
        if isinstance(name, str) and name not in expected
    )
    if unexpected:
        matrix_mismatches.append(f"unexpected scenarios: {', '.join(unexpected)}")

    scenarios = [validate_scenario_outcome(outcome) for outcome in outcomes]
    passed = not matrix_mismatches and all(item.passed for item in scenarios)
    return {
        "schema_version": 1,
        "passed": passed,
        "matrix_mismatches": matrix_mismatches,
        "scenarios": [item.as_dict() for item in scenarios],
    }


def validation_exit_code(validation: Mapping[str, object]) -> int:
    """Return a shell gate status from a matrix verdict."""

    return 0 if validation.get("passed") is True else 2


def sandbox_container_ids(docker_binary: str, session_key: str) -> list[str]:
    """Return full IDs for only the exact disposable validation session."""

    completed = subprocess.run(
        [
            docker_binary,
            "ps",
            "--all",
            "--quiet",
            "--no-trunc",
            "--filter",
            f"label=openclaw.sessionKey={session_key}",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "could not inventory the exact loopback sandbox: "
            f"{completed.stderr[-1000:]}"
        )
    return sorted({line for line in completed.stdout.splitlines() if line})


def remove_sandbox_containers(
    docker_binary: str, session_key: str
) -> dict[str, object]:
    """Remove and verify only containers carrying the exact session label."""

    before = sandbox_container_ids(docker_binary, session_key)
    actions = []
    for container_id in before:
        completed = subprocess.run(
            [docker_binary, "rm", "--force", container_id],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        actions.append(
            {
                "container_id": container_id,
                "exit_code": completed.returncode,
                "stdout": completed.stdout.strip(),
                "stderr": completed.stderr.strip(),
            }
        )
    after = sandbox_container_ids(docker_binary, session_key)
    return {
        "session_key": session_key,
        "container_ids_before_cleanup": before,
        "container_created": bool(before),
        "actions": actions,
        "container_ids_after_cleanup": after,
        "completed": not after and all(action["exit_code"] == 0 for action in actions),
    }


def remove_sandbox_sessions(
    docker_binary: str,
    session_keys: Sequence[str],
) -> dict[str, object]:
    """Remove an exact set of disposable validation sessions."""

    cleanups = [
        remove_sandbox_containers(docker_binary, session_key)
        for session_key in dict.fromkeys(session_keys)
    ]
    return {
        "session_keys": list(dict.fromkeys(session_keys)),
        "sessions": cleanups,
        "container_ids_after_cleanup": sorted(
            {
                container_id
                for cleanup in cleanups
                for container_id in cleanup["container_ids_after_cleanup"]
            }
        ),
        "completed": all(cleanup["completed"] is True for cleanup in cleanups),
    }


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
        return

    def do_POST(self) -> None:
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


def _serialize_invocation(
    result: AgentExecutionResult,
    activities: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Serialize one invocation without losing initialization evidence."""

    lifecycle = result.telemetry.invocation_lifecycle
    provider_liveness = result.telemetry.provider_liveness
    response_finalization = (
        None if lifecycle is None else lifecycle.response_finalization
    )
    return {
        "status": result.status.value,
        "stop_reason": (None if lifecycle is None else lifecycle.shutdown.reason.value),
        "response_text": result.response_text,
        "exit_code": result.telemetry.exit_code,
        "duration_ms": result.telemetry.duration_ms,
        "provider": result.telemetry.provider,
        "model": result.telemetry.model,
        "session_key": result.telemetry.session_key,
        "usage": (
            None
            if result.telemetry.usage is None
            else result.telemetry.usage.model_dump(mode="json")
        ),
        "tool_evidence_status": result.telemetry.tool_evidence_status.value,
        "tool_calls": [
            item.model_dump(mode="json") for item in result.telemetry.tool_calls
        ],
        "runtime_rejections": [
            item.model_dump(mode="json") for item in result.telemetry.runtime_rejections
        ],
        "initialization": (
            None
            if lifecycle is None
            else lifecycle.initialization.model_dump(mode="json")
        ),
        "liveness": (
            None
            if provider_liveness is None
            else provider_liveness.model_dump(mode="json")
        ),
        "response_finalization": (
            None
            if response_finalization is None
            else response_finalization.model_dump(mode="json")
        ),
        "phases": (
            []
            if lifecycle is None
            else [item.phase.value for item in lifecycle.transitions]
        ),
        "activities": list(activities),
        "cleanup_completed": (
            None if lifecycle is None else lifecycle.shutdown.cleanup_completed
        ),
        "stdout_tail": result.telemetry.stdout[-4000:],
        "stderr_tail": result.telemetry.stderr[-4000:],
    }


def execute_scenario(
    repository: Path,
    root: Path,
    openclaw: Path,
    docker_binary: str,
    sandbox_image: str,
    sandbox_user: str,
    scenario: str,
) -> dict[str, object]:
    """Execute one real OpenClaw transport scenario through the SAT adapter."""

    scenario_root = root / scenario
    state = scenario_root / "state"
    workspace = scenario_root / "workspace"
    state.mkdir(parents=True)
    workspace.mkdir()
    if scenario == "tool-rejection":
        (workspace / "fixture.txt").write_text("LOOPBACK_READ_OK\n", encoding="utf-8")
    config = scenario_root / "openclaw.json"
    materialize_run_configuration(
        repository / "configs/openclaw.example.json5",
        config,
        manifest=load_team_manifest(repository / "configs/teams.json"),
        workspace=workspace,
        sandbox_image=sandbox_image,
        sandbox_user=sandbox_user,
        model=MODEL,
        bootstrap_capability=(
            None if scenario == "tool-rejection" else AgentCapability.PLANNING
        ),
    )

    with running_server(scenario) as server:
        payload = json.loads(config.read_text(encoding="utf-8"))
        provider_name = MODEL.split("/", 1)[0]
        provider = payload["models"]["providers"][provider_name]
        provider["baseUrl"] = f"http://127.0.0.1:{server.server_port}/v1"
        provider["apiKey"] = "${DEEPSEEK_API_KEY}"
        config.write_text(json.dumps(payload), encoding="utf-8")
        config.chmod(0o600)
        environment = isolated_openclaw_environment(
            state_dir=state,
            config_path=config,
            ambient_environment=os.environ,
        )
        environment["DEEPSEEK_API_KEY"] = "loopback-not-a-secret"
        validation_identity = hashlib.sha256(
            str(scenario_root.resolve()).encode()
        ).hexdigest()[:12]
        request = AgentExecutionRequest(
            run_id=f"loopback-{scenario}-{validation_identity}",
            team_id="loopback_validation",
            iteration=1,
            role=AgentRole.PLANNER,
            expected_kind=ArtifactKind.IMPLEMENTATION_PLAN,
            prompt='Reply with exactly: {"status":"ok"}',
            timeout_seconds=0,
            model=MODEL,
        )
        requests = (
            (request, request.model_copy(update={"session_generation": 2}))
            if scenario == "correction"
            else ((request, request) if scenario == "continuation" else (request,))
        )
        session_keys = tuple(item.session_key for item in requests)
        containers_before_launch = sorted(
            {
                container_id
                for session_key in dict.fromkeys(session_keys)
                for container_id in sandbox_container_ids(docker_binary, session_key)
            }
        )
        if containers_before_launch:
            raise RuntimeError(
                "fresh loopback session key already owns containers: "
                f"{containers_before_launch}"
            )
        liveness = ProviderLivenessPolicy(
            model=MODEL,
            silence_seconds=4.0,
            stall_grace_seconds=2.0,
            source="controlled loopback validation",
        )
        executor = OpenClawSubprocessExecutor(
            openclaw_binary=openclaw,
            environment=environment,
            local=True,
            process_grace_seconds=2,
            liveness_poll_seconds=0.05,
            liveness_policies={MODEL: liveness},
            response_finalization_policy=ResponseFinalizationPolicy(
                no_progress_seconds=60.0,
                stall_grace_seconds=10.0,
                source="controlled loopback response-finalization validation",
            ),
        )
        invocations: list[dict[str, object]] = []
        current_activities: list[dict[str, object]] = []
        try:
            for invocation_request in requests:
                current_activities = []
                activity_started = time.monotonic()

                def record_activity(
                    activity: object,
                    activity_records: list[dict[str, object]] = current_activities,
                    started: float = activity_started,
                ) -> None:
                    dumped = activity.model_dump(mode="json")
                    activity_records.append(
                        {
                            "elapsed_ms": round((time.monotonic() - started) * 1000),
                            "activity": dumped,
                        }
                    )
                    if (
                        scenario == "recovery"
                        and dumped.get("kind")
                        == AgentExecutionActivityKind.STALL_SUSPECTED.value
                    ):
                        server.signal_controller_stall()

                result = executor.execute(
                    invocation_request,
                    activity_handler=record_activity,
                )
                invocations.append(_serialize_invocation(result, current_activities))
                if result.status.value != "completed":
                    break
        except Exception as error:  # pragma: no cover - exercised by the live tool
            sandbox_cleanup = remove_sandbox_sessions(docker_binary, session_keys)
            return {
                "scenario": scenario,
                "status": "validation_exception",
                "stop_reason": None,
                "response_text": None,
                "exit_code": None,
                "error_type": type(error).__name__,
                "error": str(error),
                "phases": [],
                "activities": current_activities,
                "invocations": invocations,
                "server_events": list(server.events),
                "requests_seen": server.requests_seen,
                "cleanup_completed": False,
                "sandbox_containers_before_launch": containers_before_launch,
                "sandbox_cleanup": sandbox_cleanup,
            }
        sandbox_cleanup = remove_sandbox_sessions(docker_binary, session_keys)

    final_invocation = invocations[-1]
    return {
        "scenario": scenario,
        **final_invocation,
        "invocations": invocations,
        "server_events": list(server.events),
        "requests_seen": server.requests_seen,
        "sandbox_containers_before_launch": containers_before_launch,
        "sandbox_cleanup": sandbox_cleanup,
    }


def _run_matrix(args: argparse.Namespace) -> dict[str, object]:
    repository = args.repository.resolve(strict=True)
    openclaw = args.openclaw.resolve(strict=True)
    configuration = load_quality_gate_configuration(
        repository / "configs/run-policy.json",
        repository / "benchmarks/task_manager/benchmark.json",
    )
    scenarios = tuple(args.scenario or SCENARIOS)
    if args.work_root is not None:
        root = args.work_root.resolve()
        root.mkdir(parents=True, exist_ok=False)
        outcomes = [
            execute_scenario(
                repository,
                root,
                openclaw,
                args.docker,
                configuration.policy.sandbox.image,
                args.sandbox_user,
                scenario,
            )
            for scenario in scenarios
        ]
    else:
        with TemporaryDirectory(prefix="sat-loopback-") as temporary:
            outcomes = [
                execute_scenario(
                    repository,
                    Path(temporary),
                    openclaw,
                    args.docker,
                    configuration.policy.sandbox.image,
                    args.sandbox_user,
                    scenario,
                )
                for scenario in scenarios
            ]
    return {
        "schema_version": 4,
        "model": MODEL,
        "external_provider_calls": 0,
        "outcomes": outcomes,
        "validation": validate_matrix(outcomes, expected_scenarios=scenarios),
    }


def _write_result(payload: Mapping[str, object], output: Path | None) -> None:
    rendered = json.dumps(payload, indent=2) + "\n"
    if output is None:
        print(rendered, end="")
        return
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        output,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        os.write(descriptor, rendered.encode())
    finally:
        os.close(descriptor)
    print(f"loopback validation: {output}")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the controlled matrix and fail closed on any oracle mismatch."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--openclaw", type=Path, required=True)
    parser.add_argument("--docker", default="docker")
    parser.add_argument("--sandbox-user", default="1000:1000")
    parser.add_argument(
        "--scenario", choices=(*SCENARIOS, *OPTIONAL_SCENARIOS), action="append"
    )
    parser.add_argument("--work-root", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    payload = _run_matrix(args)
    _write_result(payload, args.output)
    return validation_exit_code(_mapping(payload.get("validation")))


if __name__ == "__main__":
    raise SystemExit(main())
