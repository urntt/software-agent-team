"""Managed Docker engine identity and ambient-context isolation."""

from __future__ import annotations

import argparse
import json
import os
import socket
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

import software_agent_team.cli as cli
import software_agent_team.docker_engine as docker_engine
import software_agent_team.managed_install as managed_install
from software_agent_team.docker_engine import (
    DockerEngineError,
    DockerEngineIdentity,
    bound_docker_engine,
    discover_docker_engine,
    load_staged_docker_engine,
    save_staged_docker_engine,
    verify_bound_docker_engine,
    verify_docker_engine,
)
from software_agent_team.managed_install import (
    MANAGED_MARKER_NAME,
    ManagedApplicationMarker,
    ManagedInstallPaths,
    promote_legacy_docker_engine_record,
)
from software_agent_team.versioning import (
    ManagedChannel,
    load_installation_record,
    make_installation_record,
    save_installation_record,
)


def identity(endpoint: str, *, daemon_id: str = "daemon-a") -> DockerEngineIdentity:
    return DockerEngineIdentity(
        endpoint=endpoint,
        daemon_id=daemon_id,
        rootless=False,
        owner_uid=os.getuid(),
        socket_uid=os.getuid(),
        cgroup_driver="systemd",
        cgroup_version="2",
    )


def test_discovery_freezes_the_actual_local_context_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    socket_path = tmp_path / "docker.sock"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(socket_path))
    socket_path.chmod(0o660)
    endpoint = f"unix://{socket_path}"
    calls: list[tuple[tuple[str, ...], dict[str, str] | None]] = []

    def fake_docker(*argv: str, environment=None) -> str:
        calls.append((argv, environment))
        if argv[:2] == ("context", "inspect"):
            return json.dumps(endpoint)
        assert argv[:2] == ("--host", endpoint)
        assert environment["DOCKER_HOST"] == endpoint
        assert "DOCKER_CONTEXT" not in environment
        return json.dumps(
            {
                "ID": "daemon-a",
                "SecurityOptions": ["name=seccomp,profile=builtin"],
                "CgroupDriver": "systemd",
                "CgroupVersion": "2",
            }
        )

    monkeypatch.setattr(docker_engine, "_docker_command", fake_docker)
    try:
        selected = discover_docker_engine({"DOCKER_CONTEXT": "selected"})
    finally:
        listener.close()

    assert selected.endpoint == endpoint
    assert selected.daemon_id == "daemon-a"
    assert not selected.rootless
    assert calls[0][1] == {"DOCKER_CONTEXT": "selected"}


def test_binding_overrides_and_restores_ambient_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = identity("unix:///run/user/1001/docker.sock")
    monkeypatch.setenv("DOCKER_HOST", "unix:///run/docker.sock")
    monkeypatch.setenv("DOCKER_CONTEXT", "foreign")
    monkeypatch.setenv("SAT_DOCKER_ENGINE_MODE", "foreign")

    with bound_docker_engine(selected, verify=False):
        assert os.environ["DOCKER_HOST"] == selected.endpoint
        assert "DOCKER_CONTEXT" not in os.environ
        assert os.environ["SAT_DOCKER_ENGINE_MODE"] == "rootful"
        assert docker_engine.current_docker_engine() == selected

    assert os.environ["DOCKER_HOST"] == "unix:///run/docker.sock"
    assert os.environ["DOCKER_CONTEXT"] == "foreign"
    assert os.environ["SAT_DOCKER_ENGINE_MODE"] == "foreign"
    assert docker_engine.current_docker_engine() is None


def test_bound_engine_is_visible_to_worker_threads_and_cannot_be_replaced() -> None:
    selected = identity("unix:///run/user/1001/docker.sock")
    foreign = identity("unix:///var/run/docker.sock", daemon_id="daemon-b")

    with bound_docker_engine(selected, verify=False):
        with ThreadPoolExecutor(max_workers=1) as workers:
            observed = workers.submit(docker_engine.current_docker_engine).result()
            assert observed == selected
        with (
            pytest.raises(DockerEngineError, match="already bound"),
            bound_docker_engine(foreign, verify=False),
        ):
            pass
        assert docker_engine.current_docker_engine() == selected

    assert docker_engine.current_docker_engine() is None


def test_bound_command_refuses_ambient_context_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = identity("unix:///run/user/1001/docker.sock")
    with bound_docker_engine(selected, verify=False):
        monkeypatch.setenv("DOCKER_CONTEXT", "foreign")
        with pytest.raises(DockerEngineError, match="command environment"):
            verify_bound_docker_engine()


def test_daemon_identity_drift_is_rejected_before_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = identity("unix:///run/docker.sock")
    monkeypatch.setattr(
        docker_engine,
        "_probe_engine",
        lambda _endpoint: identity(selected.endpoint, daemon_id="daemon-b"),
    )
    with pytest.raises(DockerEngineError, match="identity changed"):
        verify_docker_engine(selected)


def test_legacy_updater_record_promotes_from_the_same_candidate_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    paths = ManagedInstallPaths.from_environment({"HOME": str(home)})
    managed_install._ensure_managed_root(paths)
    release = paths.versions_root / "0.4.0-gaaaaaaaaaaaa"
    (release / ".sat").mkdir(parents=True)
    selected = identity("unix:///run/user/1001/docker.sock")
    save_staged_docker_engine(release, selected)
    assert load_staged_docker_engine(release) == selected
    record = make_installation_record(
        channel=ManagedChannel.STABLE,
        release_version="0.4.0",
        source_revision="a" * 40,
        source_ref="v0.4.0",
        repository_url="https://example.invalid/project.git",
        application_path=paths.application_link,
        artifact_digest="sha256:" + "b" * 64,
        installed_at=datetime.now(UTC),
    )
    marker = ManagedApplicationMarker(
        application_link=str(paths.application_link),
        channel=record.channel,
        release_version=record.release_version,
        source_revision=record.source_revision,
        source_ref=record.source_ref,
        repository_url=record.repository_url,
        artifact_digest=record.artifact_digest,
    )
    (release / MANAGED_MARKER_NAME).write_text(marker.model_dump_json() + "\n")
    paths.application_link.symlink_to(release)
    save_installation_record(record, paths.installation_record)
    monkeypatch.setattr(managed_install, "verify_docker_engine", lambda _identity: None)

    promoted = promote_legacy_docker_engine_record(
        project_root=release, paths=paths, expected_record=record
    )

    assert promoted.schema_version == 2
    assert promoted.docker_engine == selected
    assert load_installation_record(paths.installation_record) == promoted


def test_legacy_record_is_unchanged_when_candidate_snapshot_is_missing(
    tmp_path: Path,
) -> None:
    record = make_installation_record(
        channel=ManagedChannel.STABLE,
        release_version="0.4.0",
        source_revision="a" * 40,
        source_ref="v0.4.0",
        repository_url="https://example.invalid/project.git",
        application_path=tmp_path / "app",
        artifact_digest="sha256:" + "b" * 64,
        installed_at=datetime.now(UTC),
    )
    with pytest.raises(DockerEngineError, match="missing or invalid"):
        promote_legacy_docker_engine_record(
            project_root=tmp_path,
            paths=ManagedInstallPaths.from_environment({"HOME": str(tmp_path)}),
            expected_record=record,
        )


def test_local_version_and_staged_checks_do_not_bind_an_inactive_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / managed_install.MANAGED_MARKER_NAME).write_text("{}\n")
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    assert (
        cli._managed_engine_binding(
            argparse.Namespace(command="version", version=False)
        )
        is None
    )
    monkeypatch.setenv("SAT_INSTALL_STAGE_ONLY", "1")
    assert (
        cli._managed_engine_binding(
            argparse.Namespace(command="validate-config", version=False)
        )
        is None
    )


def test_installation_record_v2_requires_and_preserves_the_engine(
    tmp_path: Path,
) -> None:
    selected = identity("unix:///run/docker.sock")
    arguments = {
        "channel": ManagedChannel.STABLE,
        "release_version": "0.4.0",
        "source_revision": "a" * 40,
        "source_ref": "v0.4.0",
        "repository_url": "https://github.com/urntt/software-agent-team.git",
        "application_path": tmp_path / "app",
        "artifact_digest": "sha256:" + "b" * 64,
        "installed_at": datetime.now(UTC),
    }
    path = tmp_path / "installation.json"
    current = make_installation_record(**arguments, docker_engine=selected)
    save_installation_record(current, path)
    assert load_installation_record(path) == current
    assert json.loads(path.read_text())["schema_version"] == 2

    legacy = make_installation_record(**arguments)
    save_installation_record(legacy, path)
    payload = json.loads(path.read_text())
    assert payload["schema_version"] == 1
    assert "docker_engine" not in payload
    assert load_installation_record(path) == legacy


@pytest.mark.parametrize(
    "endpoint",
    (
        "ssh://host",
        "tcp://127.0.0.1:2375",
        "unix://relative.sock",
        "unix:///tmp/../docker.sock",
    ),
)
def test_nonlocal_or_ambiguous_endpoint_is_rejected(endpoint: str) -> None:
    with pytest.raises(ValueError):
        identity(endpoint)
