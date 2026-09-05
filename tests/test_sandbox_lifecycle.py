"""Tests for exact run-scoped OpenClaw sandbox cleanup."""

from __future__ import annotations

import json
import subprocess
from collections import deque
from pathlib import Path

import pytest

from software_agent_team.artifacts import AgentRole, ArtifactKind
from software_agent_team.execution import stable_agent_session_key, stable_session_key
from software_agent_team.sandbox_lifecycle import (
    SandboxCleanupError,
    cleanup_run_sandbox_containers,
    inspect_sat_sandbox_resources,
)
from software_agent_team.teams import AgentCapability, AgentSpec, PermissionProfile


class ScriptedRunner:
    """Return deterministic subprocess results while retaining exact argv."""

    def __init__(self, responses: list[subprocess.CompletedProcess[str]]) -> None:
        self.responses = deque(responses)
        self.calls: list[tuple[str, ...]] = []

    def __call__(
        self, argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(tuple(argv))
        return self.responses.popleft()


def completed(
    stdout: str = "", *, returncode: int = 0
) -> subprocess.CompletedProcess[str]:
    """Build one typed fake subprocess result."""

    return subprocess.CompletedProcess(
        args=("docker",),
        returncode=returncode,
        stdout=stdout,
        stderr="",
    )


def sandbox_record(
    *,
    container_id: str,
    session_key: str,
    source: Path,
    running: bool = True,
) -> dict[str, object]:
    """Build the ownership fields consumed from Docker inspect."""

    return {
        "Id": container_id,
        "Name": f"/sandbox-{container_id[:12]}",
        "Config": {
            "Labels": {
                "openclaw.sandbox": "1",
                "openclaw.sessionKey": session_key,
            }
        },
        "Mounts": [{"Source": str(source), "Destination": "/workspace"}],
        "State": {"Running": running},
    }


def test_resource_observation_is_read_only_and_ignores_other_openclaw(
    tmp_path: Path,
) -> None:
    state = (tmp_path / "state").resolve()
    owned_id = "a" * 64
    unrelated_id = "b" * 64
    runner = ScriptedRunner(
        [
            completed(f"{unrelated_id}\n{owned_id}\n"),
            completed(
                json.dumps(
                    [
                        sandbox_record(
                            container_id=unrelated_id,
                            session_key="agent:other:unrelated",
                            source=(tmp_path / "other-openclaw").resolve(),
                        ),
                        sandbox_record(
                            container_id=owned_id,
                            session_key="agent:builder:sat-example-i1-work-result",
                            source=state / "workspaces/example",
                            running=False,
                        ),
                    ]
                )
            ),
        ]
    )

    observation = inspect_sat_sandbox_resources(
        sandbox_binary="docker",
        state_root=state,
        runner=runner,
    )

    assert [item.container_id for item in observation.containers] == [owned_id]
    assert observation.running == ()
    assert observation.stopped == observation.containers
    assert all("rm" not in call for call in runner.calls)


def test_cleanup_with_no_openclaw_containers_is_successful(tmp_path: Path) -> None:
    runner = ScriptedRunner([completed()])

    result = cleanup_run_sandbox_containers(
        sandbox_binary="docker",
        run_id="sat-test-run",
        openclaw_state_dir=(tmp_path / "state").resolve(),
        workspace_dir=(tmp_path / "workspace").resolve(),
        iteration_limit=3,
        roles=(AgentRole.PLANNER,),
        runner=runner,
    )

    assert result.removed == ()
    assert runner.calls == [
        (
            "docker",
            "container",
            "ls",
            "--all",
            "--quiet",
            "--filter",
            "label=openclaw.sandbox=1",
            "--filter",
            "label=openclaw.sessionKey=agent:planner:sat-sat-test-run-i1-implementation-plan",
        )
    ]


def test_cleanup_resolves_sessions_from_run_scoped_agent_specs(tmp_path: Path) -> None:
    agent = AgentSpec(
        id="cli_developer",
        label="CLI Developer",
        responsibility="Implement the assigned CLI tasks.",
        rationale="The task has one cohesive write path.",
        capability=AgentCapability.IMPLEMENTATION,
        permission_profile=PermissionProfile.WORKSPACE_WRITE,
        stage_id="implement",
        expected_output=ArtifactKind.WORK_RESULT,
        model_route_id="default",
        timeout_seconds=600,
        workspace_scope="repository",
    )
    runner = ScriptedRunner([completed(), completed()])

    result = cleanup_run_sandbox_containers(
        sandbox_binary="docker",
        run_id="sat-dynamic-run",
        openclaw_state_dir=(tmp_path / "state").resolve(),
        workspace_dir=(tmp_path / "workspace").resolve(),
        iteration_limit=2,
        agents=(agent,),
        runner=runner,
    )

    assert result.removed == ()
    expected = {
        stable_agent_session_key(
            run_id="sat-dynamic-run",
            agent_id="cli_developer",
            iteration=iteration,
            expected_kind=ArtifactKind.WORK_RESULT,
        )
        for iteration in (1, 2)
    }
    assert {
        call[-1].removeprefix("label=openclaw.sessionKey=") for call in runner.calls
    } == expected


def test_cleanup_removes_only_the_exact_owned_run_container(tmp_path: Path) -> None:
    owned_id = "a" * 64
    unrelated_id = "b" * 64
    run_id = "sat-test-run"
    owned_session = stable_session_key(
        run_id=run_id,
        role=AgentRole.GENERALIST_DEVELOPER,
        iteration=2,
        expected_kind=ArtifactKind.WORK_RESULT,
    )
    unrelated_session = stable_session_key(
        run_id="sat-other-run",
        role=AgentRole.GENERALIST_DEVELOPER,
        iteration=2,
        expected_kind=ArtifactKind.WORK_RESULT,
    )
    state = (tmp_path / "state").resolve()
    workspace = (tmp_path / "workspace").resolve()
    inspection = [
        sandbox_record(
            container_id=owned_id,
            session_key=owned_session,
            source=workspace,
        ),
        sandbox_record(
            container_id=unrelated_id,
            session_key=unrelated_session,
            source=tmp_path / "other-workspace",
        ),
    ]
    runner = ScriptedRunner(
        [
            completed(),
            completed(f"{owned_id}\n{unrelated_id}\n"),
            completed(json.dumps(inspection)),
            completed(owned_id),
        ]
    )

    result = cleanup_run_sandbox_containers(
        sandbox_binary="docker",
        run_id=run_id,
        openclaw_state_dir=state,
        workspace_dir=workspace,
        iteration_limit=2,
        roles=(AgentRole.GENERALIST_DEVELOPER,),
        runner=runner,
    )

    assert [item.container_id for item in result.removed] == [owned_id]
    assert runner.calls[-1] == (
        "docker",
        "container",
        "rm",
        "--force",
        owned_id,
    )


def test_cleanup_refuses_a_matching_session_outside_sat_paths(tmp_path: Path) -> None:
    container_id = "c" * 64
    session = stable_session_key(
        run_id="sat-test-run",
        role=AgentRole.PLANNER,
        iteration=1,
        expected_kind=ArtifactKind.IMPLEMENTATION_PLAN,
    )
    runner = ScriptedRunner(
        [
            completed(f"{container_id}\n"),
            completed(
                json.dumps(
                    [
                        sandbox_record(
                            container_id=container_id,
                            session_key=session,
                            source=Path("/home/another-openclaw/workspace"),
                        )
                    ]
                )
            ),
        ]
    )

    with pytest.raises(SandboxCleanupError, match="outside SAT-owned paths"):
        cleanup_run_sandbox_containers(
            sandbox_binary="docker",
            run_id="sat-test-run",
            openclaw_state_dir=(tmp_path / "state").resolve(),
            workspace_dir=(tmp_path / "workspace").resolve(),
            iteration_limit=3,
            roles=(AgentRole.PLANNER,),
            runner=runner,
        )

    assert all(call[2:4] != ("rm", "--force") for call in runner.calls)


def test_cleanup_reports_a_container_removal_failure(tmp_path: Path) -> None:
    container_id = "d" * 64
    session = stable_session_key(
        run_id="sat-test-run",
        role=AgentRole.TESTER,
        iteration=1,
        expected_kind=ArtifactKind.TEST_REPORT,
    )
    workspace = (tmp_path / "workspace").resolve()
    runner = ScriptedRunner(
        [
            completed(f"{container_id}\n"),
            completed(
                json.dumps(
                    [
                        sandbox_record(
                            container_id=container_id,
                            session_key=session,
                            source=workspace,
                        )
                    ]
                )
            ),
            completed(returncode=1),
        ]
    )

    with pytest.raises(SandboxCleanupError, match="could not remove 1"):
        cleanup_run_sandbox_containers(
            sandbox_binary="docker",
            run_id="sat-test-run",
            openclaw_state_dir=(tmp_path / "state").resolve(),
            workspace_dir=workspace,
            iteration_limit=1,
            roles=(AgentRole.TESTER,),
            runner=runner,
        )
