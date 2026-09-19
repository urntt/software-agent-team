"""Bind every managed Docker operation to one local daemon identity."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

_DAEMON_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,199}$")
_COMMAND_TIMEOUT_SECONDS = 20
_STAGED_ENGINE_NAME = ".sat/docker-engine.json"


class DockerEngineError(RuntimeError):
    """A Docker endpoint is unavailable or cannot preserve SAT's boundaries."""


def _validate_endpoint(value: str) -> str:
    if not value.startswith("unix:///"):
        raise ValueError("Docker engine endpoint must be a local Unix socket")
    path_text = value.removeprefix("unix://")
    path = Path(path_text)
    if (
        not path.is_absolute()
        or path == Path(path.anchor)
        or os.path.normpath(path_text) != path_text
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        raise ValueError("Docker engine socket path is unsafe")
    return value


class DockerEngineIdentity(BaseModel):
    """The local daemon selected before managed image mutation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    endpoint: str = Field(min_length=9, max_length=4096)
    daemon_id: str = Field(min_length=1, max_length=200)
    rootless: bool
    owner_uid: int = Field(ge=0)
    socket_uid: int = Field(ge=0)
    cgroup_driver: str = Field(min_length=1, max_length=50)
    cgroup_version: str = Field(min_length=1, max_length=20)

    @field_validator("endpoint")
    @classmethod
    def require_local_socket(cls, value: str) -> str:
        return _validate_endpoint(value)

    @field_validator("daemon_id")
    @classmethod
    def require_daemon_id(cls, value: str) -> str:
        if _DAEMON_ID.fullmatch(value) is None:
            raise ValueError("Docker daemon ID is invalid")
        return value


_BOUND_ENGINE: ContextVar[DockerEngineIdentity | None] = ContextVar(
    "sat_bound_docker_engine", default=None
)
_PROCESS_ENGINE_LOCK = threading.Lock()
_PROCESS_ENGINE: DockerEngineIdentity | None = None
_PROCESS_ENGINE_USERS = 0
_PROCESS_PREVIOUS_ENVIRONMENT: tuple[str | None, str | None, str | None] | None = None


def _local_endpoint(value: str) -> tuple[str, int]:
    """Resolve a user-selected endpoint without accepting remote transports."""

    try:
        _validate_endpoint(value)
        path = Path(value.removeprefix("unix://")).resolve(strict=True)
        metadata = path.stat()
    except (OSError, ValueError) as error:
        raise DockerEngineError(
            "Docker endpoint is not an accessible local socket"
        ) from error
    if not stat.S_ISSOCK(metadata.st_mode):
        raise DockerEngineError("Docker endpoint is not a Unix socket")
    if stat.S_IMODE(metadata.st_mode) & 0o007:
        raise DockerEngineError("Docker socket must not allow world access")
    canonical = f"unix://{path}"
    _validate_endpoint(canonical)
    return canonical, metadata.st_uid


def _docker_command(
    *arguments: str, environment: Mapping[str, str] | None = None
) -> str:
    try:
        completed = subprocess.run(
            ("docker", *arguments),
            check=False,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=_COMMAND_TIMEOUT_SECONDS,
            env=None if environment is None else dict(environment),
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise DockerEngineError("Docker engine inspection could not run") from error
    if completed.returncode != 0:
        raise DockerEngineError(
            "Docker engine is unavailable to this user; check the selected "
            "Docker socket or context"
        )
    return completed.stdout.strip()


def _probe_engine(endpoint: str) -> DockerEngineIdentity:
    endpoint, socket_uid = _local_endpoint(endpoint)
    environment = dict(os.environ)
    environment.pop("DOCKER_CONTEXT", None)
    environment["DOCKER_HOST"] = endpoint
    try:
        payload = json.loads(
            _docker_command(
                "--host",
                endpoint,
                "info",
                "--format",
                "{{json .}}",
                environment=environment,
            )
        )
        daemon_id = payload["ID"]
        options = payload["SecurityOptions"]
        cgroup_driver = payload["CgroupDriver"]
        cgroup_version = payload["CgroupVersion"]
        if (
            not isinstance(payload, dict)
            or not isinstance(options, list)
            or not all(isinstance(option, str) for option in options)
            or not isinstance(cgroup_driver, str)
            or not isinstance(cgroup_version, str)
        ):
            raise TypeError
        rootless = "name=rootless" in options
        if rootless and os.getuid() == 0:
            raise DockerEngineError(
                "rootless Docker must be used from an unprivileged host account"
            )
        if rootless and socket_uid != os.getuid():
            raise DockerEngineError(
                "rootless Docker socket must belong to the current user"
            )
        if rootless and (cgroup_version != "2" or cgroup_driver != "systemd"):
            raise DockerEngineError(
                "rootless Docker requires cgroup v2 and a systemd cgroup driver "
                "to enforce memory, PID, and CPU limits"
            )
        return DockerEngineIdentity(
            endpoint=endpoint,
            daemon_id=daemon_id,
            rootless=rootless,
            owner_uid=os.getuid(),
            socket_uid=socket_uid,
            cgroup_driver=cgroup_driver,
            cgroup_version=cgroup_version,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise DockerEngineError("Docker returned an invalid engine identity") from error


def discover_docker_engine(
    environment: Mapping[str, str] | None = None,
) -> DockerEngineIdentity:
    """Freeze the current local Docker selection before any image mutation."""

    selected = os.environ if environment is None else environment
    endpoint = selected.get("DOCKER_HOST")
    if not endpoint:
        endpoint = _docker_command(
            "context",
            "inspect",
            "--format",
            "{{json .Endpoints.docker.Host}}",
            environment=selected,
        )
        try:
            endpoint = json.loads(endpoint)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise DockerEngineError(
                "Docker context has no readable local endpoint"
            ) from error
    if not isinstance(endpoint, str):
        raise DockerEngineError("Docker context has no local endpoint")
    return _probe_engine(endpoint)


def verify_docker_engine(identity: DockerEngineIdentity) -> None:
    """Reject daemon, mode, or enforcement drift before using its resources."""

    if os.getuid() != identity.owner_uid:
        raise DockerEngineError("Docker engine binding belongs to another user")
    current = _probe_engine(identity.endpoint)
    if current != identity:
        raise DockerEngineError(
            "Docker engine identity changed; SAT refused to use another daemon"
        )


def engine_environment(
    identity: DockerEngineIdentity, environment: Mapping[str, str]
) -> dict[str, str]:
    """Override ambient context selection with the immutable Unix endpoint."""

    result = dict(environment)
    result.pop("DOCKER_CONTEXT", None)
    result["DOCKER_HOST"] = identity.endpoint
    result["SAT_DOCKER_ENGINE_MODE"] = "rootless" if identity.rootless else "rootful"
    return result


@contextmanager
def bound_docker_engine(
    identity: DockerEngineIdentity | None, *, verify: bool = True
) -> Iterator[None]:
    """Bind Docker subprocesses and descendants for one CLI lifecycle."""

    if identity is None:
        yield
        return
    if verify:
        verify_docker_engine(identity)
    global _PROCESS_ENGINE, _PROCESS_ENGINE_USERS, _PROCESS_PREVIOUS_ENVIRONMENT
    with _PROCESS_ENGINE_LOCK:
        if _PROCESS_ENGINE is not None and identity != _PROCESS_ENGINE:
            raise DockerEngineError(
                "another Docker engine is already bound in this SAT process"
            )
        if _PROCESS_ENGINE is None:
            _PROCESS_PREVIOUS_ENVIRONMENT = (
                os.environ.get("DOCKER_HOST"),
                os.environ.get("DOCKER_CONTEXT"),
                os.environ.get("SAT_DOCKER_ENGINE_MODE"),
            )
            os.environ["DOCKER_HOST"] = identity.endpoint
            os.environ.pop("DOCKER_CONTEXT", None)
            os.environ["SAT_DOCKER_ENGINE_MODE"] = (
                "rootless" if identity.rootless else "rootful"
            )
            _PROCESS_ENGINE = identity
        _PROCESS_ENGINE_USERS += 1
    token = _BOUND_ENGINE.set(identity)
    try:
        yield
    finally:
        _BOUND_ENGINE.reset(token)
        with _PROCESS_ENGINE_LOCK:
            _PROCESS_ENGINE_USERS -= 1
            if _PROCESS_ENGINE_USERS == 0:
                assert _PROCESS_PREVIOUS_ENVIRONMENT is not None
                for name, previous in zip(
                    ("DOCKER_HOST", "DOCKER_CONTEXT", "SAT_DOCKER_ENGINE_MODE"),
                    _PROCESS_PREVIOUS_ENVIRONMENT,
                    strict=True,
                ):
                    if previous is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = previous
                _PROCESS_ENGINE = None
                _PROCESS_PREVIOUS_ENVIRONMENT = None


def current_docker_engine() -> DockerEngineIdentity | None:
    """Return the current process-owned binding, if one exists."""

    local = _BOUND_ENGINE.get()
    process = _PROCESS_ENGINE
    if local is not None and process is not None and local != process:
        raise DockerEngineError("Docker engine bindings disagree across threads")
    return local or process


def verify_bound_docker_engine() -> None:
    """Verify the bound daemon before an external Docker command."""

    identity = current_docker_engine()
    if identity is not None:
        if (
            os.environ.get("DOCKER_HOST") != identity.endpoint
            or "DOCKER_CONTEXT" in os.environ
        ):
            raise DockerEngineError(
                "Docker command environment no longer matches the recorded engine"
            )
        verify_docker_engine(identity)


def save_staged_docker_engine(root: Path, identity: DockerEngineIdentity) -> Path:
    """Persist the candidate's verified engine for legacy-updater migration."""

    destination = root / _STAGED_ENGINE_NAME
    if destination.parent.is_symlink() or not destination.parent.is_dir():
        raise DockerEngineError("staged Docker engine directory is invalid")
    if destination.is_symlink() or (destination.exists() and not destination.is_file()):
        raise DockerEngineError("staged Docker engine record is invalid")
    content = (identity.model_dump_json(indent=2) + "\n").encode()
    temporary = destination.parent / f".{destination.name}.{uuid4().hex}.tmp"
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
        directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def load_staged_docker_engine(root: Path) -> DockerEngineIdentity:
    """Read an owned candidate snapshot without following a record symlink."""

    path = root / _STAGED_ENGINE_NAME
    if path.is_symlink() or not path.is_file():
        raise DockerEngineError("staged Docker engine record is missing or invalid")
    metadata = path.stat()
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise DockerEngineError("staged Docker engine record is not private")
    try:
        if metadata.st_size > 8192:
            raise ValueError("oversized engine record")
        return DockerEngineIdentity.model_validate_json(path.read_bytes())
    except (OSError, ValueError) as error:
        raise DockerEngineError("staged Docker engine record is invalid") from error
