"""Migrate copied provider databases through the pinned SDK in a closed sandbox."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import subprocess
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from software_agent_team.docker_engine import (
    DockerEngineIdentity,
    bound_docker_engine,
    container_user,
    current_docker_engine,
    discover_docker_engine,
)
from software_agent_team.openclaw_runtime import is_owned_openclaw_runtime
from software_agent_team.runtime_configuration import RuntimeConfigurationError
from software_agent_team.workspace_mounts import (
    require_idle_openclaw_state,
    verified_local_sandbox_image,
)

_RESULT_PREFIX = "SAT_SDK_STATE_MIGRATION_V1 "
_CONTAINER_ID = re.compile(r"[a-f0-9]{64}")


class StateMigrationCleanupError(RuntimeConfigurationError):
    """A migration mount must be preserved until its exact container is removed."""


def provider_databases(state: Path) -> tuple[Path, ...]:
    """Find canonical agent stores, never configured external database targets."""

    return tuple(sorted(state.glob("agents/*/agent/openclaw-agent.sqlite")))


def preflight_provider_database_copy(state: Path) -> None:
    """Require the shared product liveness boundary before copying databases."""

    databases = provider_databases(state)
    shared = state / "state/openclaw.sqlite"
    if os.path.lexists(shared):
        databases = (*databases, shared)
    if databases:
        for database in databases:
            metadata = database.lstat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
                raise RuntimeConfigurationError(
                    "SAT provider database must be a real file owned by this account"
                )
        require_idle_openclaw_state(state, "docker", subprocess.run, 30)


def _supported_schema(runtime: Path) -> int:
    if not is_owned_openclaw_runtime(runtime):
        raise RuntimeConfigurationError("SAT database migration requires its owned SDK")
    host = runtime / "runtime/node_modules/openclaw"
    if host.is_symlink() or host.resolve(strict=True) != host:
        raise RuntimeConfigurationError(
            "SAT SDK database package must be a real owned path"
        )
    contracts = tuple((host / "dist").glob("openclaw-agent-db-contract-*.mjs"))
    if len(contracts) != 1 or contracts[0].is_symlink():
        raise RuntimeConfigurationError(
            "SAT SDK database schema contract is unavailable"
        )
    match = re.search(
        r"const OPENCLAW_AGENT_SCHEMA_VERSION = ([1-9][0-9]*);",
        contracts[0].read_text(encoding="utf-8"),
    )
    if match is None:
        raise RuntimeConfigurationError(
            "SAT SDK database schema contract is unsupported"
        )
    return int(match[1])


def _schema(path: Path) -> int:
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            return db.execute("PRAGMA user_version").fetchone()[0]
    except sqlite3.Error as error:
        raise RuntimeConfigurationError(
            "SAT could not inspect its copied provider database; original state "
            "is preserved. Retry `sat configure` after resolving the database error."
        ) from error


def _validate_registry(shared: Path, source: Path) -> None:
    """Do not reinterpret an invisible external locator as a missing database."""

    if not shared.exists():
        return
    try:
        with closing(sqlite3.connect(shared.as_uri() + "?mode=ro", uri=True)) as db:
            if not db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='agent_databases'"
            ).fetchone():
                return
            for (locator,) in db.execute("SELECT path FROM agent_databases"):
                target = Path(locator)
                if target.is_absolute():
                    if not target.is_relative_to(source):
                        raise ValueError("external database locator")
                    target = target.relative_to(source)
                if not re.fullmatch(
                    r"agents/[a-z0-9_-]+/agent/openclaw-agent\.sqlite",
                    target.as_posix(),
                ):
                    raise ValueError("unknown database locator")
    except (sqlite3.Error, TypeError, ValueError) as error:
        raise RuntimeConfigurationError(
            "SAT's saved database registry has an unsupported target; original state "
            "is preserved. Do not repair it with a different OpenClaw installation."
        ) from error


def migrate_copied_provider_databases(
    candidate: Path,
    *,
    runtime: Path,
    policy_path: Path,
    source_state: Path | None = None,
) -> None:
    """Apply SDK transformations only to a disposable configuration candidate.

    The helper cannot see the live state, external config targets, user HOME,
    other runtimes, Docker socket, or network. SDK registration uses relative
    database locators, so candidate activation does not retain sandbox paths.
    """

    databases = provider_databases(candidate)
    shared = candidate / "state/openclaw.sqlite"
    if not databases and not shared.exists():
        return
    supported = _supported_schema(runtime)
    pending = []
    for database in databases:
        relative = database.relative_to(candidate)
        if (
            not re.fullmatch(
                r"agents/[a-z0-9_-]+/agent/openclaw-agent\.sqlite", relative.as_posix()
            )
            or database.is_symlink()
            or not database.is_file()
            or database.stat().st_uid != os.geteuid()
        ):
            raise RuntimeConfigurationError(
                "SAT provider database has an unsafe owner/path"
            )
        version = _schema(database)
        if version > supported:
            raise RuntimeConfigurationError(
                f"SAT provider database schema {version} exceeds SDK "
                f"schema {supported}; "
                "original state is preserved. Update SAT before retrying setup."
            )
        if 0 < version < supported:
            pending.append(relative.as_posix())
    if not pending and not shared.exists():
        return
    _validate_registry(shared, source_state or candidate)
    print("Preparing saved provider databases for the private SDK…")
    engine = current_docker_engine()
    if engine is not None:
        _migrate_in_sandbox(candidate, runtime, policy_path, pending, supported, engine)
    else:
        engine = discover_docker_engine()
        with bound_docker_engine(engine):
            _migrate_in_sandbox(
                candidate, runtime, policy_path, pending, supported, engine
            )


def _migrate_in_sandbox(
    candidate: Path,
    runtime: Path,
    policy_path: Path,
    pending: list[str],
    supported: int,
    engine: DockerEngineIdentity,
) -> None:
    image = verified_local_sandbox_image(policy_path, "docker", subprocess.run, 30)
    helper = Path(__file__).with_suffix(".mjs")
    pins = (runtime.parent.parent / "configs/toolchain.sh").read_text(encoding="utf-8")
    pin = re.search(
        r'^task_node_version="([0-9]+\.[0-9]+\.[0-9]+)"$', pins, re.MULTILINE
    )
    node = runtime / "tools" / f"node-v{pin[1] if pin else 'invalid'}/bin/node"
    if not node.is_file() or node.is_symlink():
        raise RuntimeConfigurationError(
            "SAT SDK database migration Node is unavailable"
        )
    for mount in (runtime, candidate, helper):
        if "," in str(mount) or any(ord(char) < 32 for char in str(mount)):
            raise RuntimeConfigurationError(
                "SAT SDK database migration mount is unsafe"
            )
    entrypoint = "/opt/sat-sdk/" + node.relative_to(runtime).as_posix()
    token = uuid4().hex
    container_name = f"sat-provider-migration-{token}"
    cidfile = candidate / f".sdk-migration-cid-{token}"
    ownership = candidate / f".sdk-migration-owner-{token}.json"
    with ownership.open("x", encoding="utf-8") as output:
        json.dump(
            {
                "schema_version": 1,
                "container_name": container_name,
                "image_id": image,
                "state_mount": str(candidate),
                "sdk_mount": str(runtime),
                "owner_uid": os.geteuid(),
            },
            output,
        )
    ownership.chmod(0o600)
    command = [
        "docker",
        "create",
        "--name",
        container_name,
        "--cidfile",
        str(cidfile),
        "--pull",
        "never",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        container_user(engine),
        "--memory",
        "512m",
        "--pids-limit",
        "128",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,noexec,size=64m",
        "--label",
        "software-agent-team.maintenance=provider-schema-v1",
        "--label",
        f"software-agent-team.maintenance-token={token}",
        "--mount",
        f"type=bind,source={runtime},target=/opt/sat-sdk,readonly",
        "--mount",
        f"type=bind,source={candidate},target=/sat-state",
        "--mount",
        f"type=bind,source={helper},target=/opt/sat-migrate.mjs,readonly",
    ]
    for name, value in {
        "HOME": "/sat-state",
        "OPENCLAW_HOME": "/sat-state",
        "OPENCLAW_STATE_DIR": "/sat-state",
        "OPENCLAW_CONFIG_PATH": "/sat-state/.sat-migration-config.json",
        "OPENCLAW_AUTH_PROFILE_SECRET_DIR": "/sat-state/credentials",
        "OPENCLAW_OAUTH_DIR": "/sat-state/credentials",
        "OPENCLAW_WORKSPACE_DIR": "/sat-state/workspace",
        "NODE_DISABLE_COMPILE_CACHE": "1",
    }.items():
        command.extend(("--env", f"{name}={value}"))
    command.extend(
        ("--entrypoint", entrypoint, image, "/opt/sat-migrate.mjs", json.dumps(pending))
    )
    container = None
    try:
        created = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            stdin=subprocess.DEVNULL,
        )
        if (
            created.returncode
            or _CONTAINER_ID.fullmatch(created.stdout.strip()) is None
        ):
            raise RuntimeConfigurationError(
                "SAT database migration container could not start"
            )
        container = created.stdout.strip()
        result = subprocess.run(
            ["docker", "start", "--attach", container],
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
        )
        receipts = [
            line[len(_RESULT_PREFIX) :]
            for line in result.stdout.splitlines()
            if line.startswith(_RESULT_PREFIX)
        ]
        try:
            receipt = json.loads(receipts[0]) if len(receipts) == 1 else None
        except json.JSONDecodeError:
            receipt = None
        if (
            result.returncode
            or not isinstance(receipt, dict)
            or receipt.get("ok") is not True
            or receipt.get("versions")
            != [{"database": target, "version": supported} for target in pending]
        ):
            raise RuntimeConfigurationError(
                "SAT's private SDK could not verify the saved database migration; "
                "original state and credentials are preserved. Retry `sat configure`; "
                "do not use a global OpenClaw installation to repair SAT state."
            )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeConfigurationError(
            "SAT database migration failed; original state is preserved"
        ) from error
    finally:
        if container is None and os.path.lexists(cidfile):
            try:
                metadata = cidfile.lstat()
                if (
                    stat.S_ISREG(metadata.st_mode)
                    and metadata.st_uid == os.geteuid()
                    and metadata.st_nlink == 1
                    and metadata.st_size <= 65
                ):
                    recorded = cidfile.read_text(encoding="ascii").strip()
                    if _CONTAINER_ID.fullmatch(recorded):
                        container = recorded
            except (OSError, UnicodeError):
                pass
            if container is None:
                raise StateMigrationCleanupError(
                    f"SAT migration ownership is unresolved; candidate and receipt "
                    f"are preserved at {candidate} and {cidfile}. "
                    "Original state is unchanged."
                )
        if container is None:
            raise StateMigrationCleanupError(
                f"SAT could not verify creation of {container_name}; the candidate "
                f"and ownership receipt are preserved at {candidate}. "
                "Original state is unchanged."
            )
        if container is not None:
            try:
                removed = subprocess.run(
                    ["docker", "rm", "--force", container],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=30,
                    stdin=subprocess.DEVNULL,
                )
                if removed.returncode:
                    raise OSError("migration container removal failed")
            except (OSError, subprocess.SubprocessError, KeyboardInterrupt) as error:
                raise StateMigrationCleanupError(
                    f"SAT migration container {container} could not be removed; "
                    f"the uncommitted candidate is preserved at {candidate}. "
                    "Original state is unchanged."
                ) from error
        cidfile.unlink(missing_ok=True)
        ownership.unlink(missing_ok=True)
    print("✓ Saved provider databases are ready; no provider call was made.")
