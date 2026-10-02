"""Historic SDK databases cross the real configuration/SDK/sandbox boundary."""

import hashlib
import json
import os
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
from pinned_runtime import pinned_openclaw_binary, pinned_openclaw_package

from software_agent_team import cli
from software_agent_team import sdk_state_migration as migration
from software_agent_team.openclaw_runtime import isolated_openclaw_environment
from software_agent_team.product import ProductStatePaths, ensure_product_state
from software_agent_team.runtime_configuration import RuntimeConfigurationError
from software_agent_team.sdk_state_migration import (
    StateMigrationCleanupError,
    migrate_copied_provider_databases,
)
from software_agent_team.user_configuration import UserConfiguration

ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "tests/fixtures/openclaw-2026.7.1-2"
MODEL = "deepseek/deepseek-v4-flash"
OPAQUE = json.dumps(
    {
        "version": 1,
        "profiles": {
            "fixture": {
                "type": "api_key",
                "provider": "deepseek",
                "key": "non-secret-fixture",
            }
        },
    }
)


def historic_state(tmp_path, monkeypatch):
    paths = ProductStatePaths.below(tmp_path / "product-state")
    ensure_product_state(paths)
    monkeypatch.setenv("SAT_STATE_ROOT", str(paths.root))
    monkeypatch.setenv("SAT_CONFIG_PATH", str(tmp_path / "config.json"))
    agent = paths.openclaw / "agents/main/agent/openclaw-agent.sqlite"
    shared = paths.openclaw / "state/openclaw.sqlite"
    for kind, database, owner in (("agent", agent, "main"), ("state", shared, None)):
        database.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(database)) as db:
            db.executescript((FIXTURES / f"{kind}-schema-v1.sql").read_text())
            db.execute("PRAGMA user_version=1")
            db.execute(
                "INSERT INTO schema_meta VALUES(?,?,?,?,?,?,?)",
                (
                    "primary",
                    "agent" if owner else "global",
                    1,
                    owner,
                    None,
                    1,
                    1,
                ),
            )
            if owner:
                db.execute(
                    "INSERT INTO auth_profile_store VALUES(?,?,?)",
                    ("profiles", OPAQUE, 1),
                )
            else:
                db.execute(
                    "INSERT INTO agent_databases VALUES(?,?,?,?,?)",
                    (
                        "main",
                        str(agent),
                        1,
                        1,
                        agent.stat().st_size,
                    ),
                )
            db.commit()
    (agent.parent.parent / "sessions").mkdir()
    (paths.openclaw / "openclaw.json").write_text(
        json.dumps(
            {
                "agents": {"defaults": {"model": {"primary": MODEL}}},
            }
        )
    )
    credential = paths.openclaw / "credentials/opaque.txt"
    credential.parent.mkdir(exist_ok=True)
    credential.write_text("non-secret preserved bytes")
    return paths, agent, credential


def snapshot(root):
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file()
    }


def require_runtime_image():
    pinned_openclaw_package(ROOT)
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", "sat-python-quality:phase1-v10"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        pytest.skip(
            "local pinned sandbox image is unavailable; run the runtime installation"
        )
    if result.returncode:
        pytest.skip(
            "local pinned sandbox image is unavailable; run the runtime installation"
        )


def sdk_status(state):
    return subprocess.run(
        [
            str(pinned_openclaw_binary(ROOT)),
            "models",
            "status",
            "--json",
        ],
        env={
            **os.environ,
            **isolated_openclaw_environment(
                state_dir=state,
                config_path=state / "openclaw.json",
            ),
        },
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.parametrize("commit,wal", [(False, False), (True, False), (True, True)])
def test_schema_one_migrates_in_production_transaction_without_changing_live_state(
    tmp_path,
    monkeypatch,
    commit,
    wal,
):
    require_runtime_image()
    paths, agent, credential = historic_state(tmp_path, monkeypatch)
    if wal:
        with closing(sqlite3.connect(agent)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA wal_autocheckpoint=0")
            db.execute("UPDATE auth_profile_store SET updated_at=42")
            db.commit()
            saved = {
                suffix: Path(str(agent) + suffix).read_bytes()
                for suffix in ("", "-wal", "-shm")
            }
        # Restore a valid stopped-writer file set containing uncheckpointed WAL.
        for suffix, content in saved.items():
            Path(str(agent) + suffix).write_bytes(content)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "sentinel").write_text("must remain private and unchanged")
    # The SDK's old failure crosses its real auth reader, before any model call.
    before_status = sdk_status(paths.openclaw)
    assert before_status.returncode != 0
    assert "run openclaw doctor --fix" in before_status.stderr + before_status.stdout
    before = snapshot(paths.openclaw)
    foreign_before = snapshot(foreign)
    with cli._staged_openclaw_state(paths.openclaw) as (candidate, config):
        assert snapshot(paths.openclaw) == before
        migrated = candidate / agent.relative_to(paths.openclaw)
        with closing(sqlite3.connect(migrated)) as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] > 1
            assert (
                db.execute("SELECT store_json FROM auth_profile_store").fetchone()[0]
                == OPAQUE
            )
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert sdk_status(candidate).returncode == 0
        assert config.read_bytes() == (paths.openclaw / "openclaw.json").read_bytes()
        if commit:
            cli._commit_configuration_transaction(
                UserConfiguration(model=MODEL),
                user_path=tmp_path / "config.json",
                live_openclaw_state=paths.openclaw,
                staged_openclaw_state=candidate,
            )
    assert snapshot(foreign) == foreign_before
    assert credential.read_text() == "non-secret preserved bytes"
    assert not tuple(paths.root.glob(".openclaw.candidate-*"))
    assert not tuple(paths.root.glob(".openclaw.rollback-*"))
    if commit:
        assert sdk_status(paths.openclaw).returncode == 0
        with closing(sqlite3.connect(paths.openclaw / "state/openclaw.sqlite")) as db:
            registered = db.execute("SELECT path FROM agent_databases").fetchall()
        assert registered
        assert all(
            "/sat-state" not in row[0] and ".candidate-" not in row[0]
            for row in registered
        )
    else:
        assert snapshot(paths.openclaw) == before


def test_unknown_registry_locator_refuses_migration(tmp_path, monkeypatch):
    pinned_openclaw_package(ROOT)
    paths, _, _ = historic_state(tmp_path, monkeypatch)
    foreign = tmp_path / "foreign.sqlite"
    foreign.write_bytes(b"external sentinel")
    with closing(sqlite3.connect(paths.openclaw / "state/openclaw.sqlite")) as db:
        db.execute("UPDATE agent_databases SET path=?", (str(foreign),))
        db.commit()
    before = snapshot(paths.openclaw)
    with pytest.raises(RuntimeConfigurationError, match="unsupported target"):
        migrate_copied_provider_databases(
            paths.openclaw,
            runtime=ROOT / ".sat/openclaw",
            policy_path=cli.DEFAULT_PRODUCT_POLICY,
        )
    assert snapshot(paths.openclaw) == before
    assert foreign.read_bytes() == b"external sentinel"


@pytest.mark.parametrize("rootless", [False, True])
def test_create_timeout_uses_exact_cid_receipt_and_redacts_sdk_output(
    tmp_path, monkeypatch, rootless
):
    paths, _, _ = historic_state(tmp_path, monkeypatch)
    runtime = tmp_path / "application/.sat/openclaw"
    runtime.mkdir(parents=True)
    (runtime / ".sat-owned-runtime").write_text(
        f"software-agent-team-openclaw-runtime-v1\nroot={runtime}\n"
    )
    dist = runtime / "runtime/node_modules/openclaw/dist"
    dist.mkdir(parents=True)
    (dist / "openclaw-agent-db-contract-fixture.mjs").write_text(
        "const OPENCLAW_AGENT_SCHEMA_VERSION = 23;"
    )
    node = runtime / "tools/node-v24.19.0/bin/node"
    node.parent.mkdir(parents=True)
    node.touch()
    pins = runtime.parent.parent / "configs/toolchain.sh"
    pins.parent.mkdir()
    pins.write_text('task_node_version="24.19.0"\n')
    monkeypatch.setattr(
        migration, "verified_local_sandbox_image", lambda *args: "sha256:" + "b" * 64
    )
    monkeypatch.setattr(
        migration, "current_docker_engine", lambda: SimpleNamespace(rootless=rootless)
    )
    cid = "a" * 64
    removed = []

    def timed_out(command, **kwargs):
        if command[1] == "create":
            Path(command[command.index("--cidfile") + 1]).write_text(cid + "\n")
            assert command[command.index("--network") + 1] == "none"
            expected_user = "0:0" if rootless else f"{os.getuid()}:{os.getgid()}"
            assert command[command.index("--user") + 1] == expected_user
            assert not any("DEEPSEEK_API_KEY" in arg for arg in command)
            raise subprocess.TimeoutExpired(command, 30, stderr="private-test-value")
        assert command == ["docker", "rm", "--force", cid]
        removed.append(cid)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(migration.subprocess, "run", timed_out)
    with pytest.raises(
        RuntimeConfigurationError, match="original state is preserved"
    ) as failure:
        migrate_copied_provider_databases(
            paths.openclaw,
            runtime=runtime,
            policy_path=cli.DEFAULT_PRODUCT_POLICY,
        )
    assert "private-test-value" not in str(failure.value)
    assert removed == [cid]
    assert not tuple(paths.openclaw.glob(".sdk-migration-cid-*"))


def test_invalid_database_refuses_setup_preserving_original_bytes(
    tmp_path, monkeypatch
):
    require_runtime_image()
    paths, agent, _ = historic_state(tmp_path, monkeypatch)
    agent.write_bytes(b"not a SQLite database")
    before = snapshot(paths.openclaw)
    with (
        pytest.raises(RuntimeConfigurationError, match="copied provider database"),
        cli._staged_openclaw_state(paths.openclaw),
    ):
        pytest.fail("corrupt database reached provider setup")
    assert snapshot(paths.openclaw) == before
    assert not tuple(paths.root.glob(".openclaw.candidate-*"))


def test_sdk_refused_owner_preserves_original_state(tmp_path, monkeypatch):
    require_runtime_image()
    paths, agent, _ = historic_state(tmp_path, monkeypatch)
    with closing(sqlite3.connect(agent)) as db:
        db.execute("UPDATE schema_meta SET agent_id='foreign'")
        db.commit()
    before = snapshot(paths.openclaw)
    with (
        pytest.raises(RuntimeConfigurationError, match="could not verify"),
        cli._staged_openclaw_state(paths.openclaw),
    ):
        pytest.fail("invalid SDK database owner reached provider setup")
    assert snapshot(paths.openclaw) == before
    assert not tuple(paths.root.glob(".openclaw.candidate-*"))


def test_shared_schema_one_migrates_without_an_agent_database(tmp_path, monkeypatch):
    require_runtime_image()
    paths, agent, _ = historic_state(tmp_path, monkeypatch)
    agent.unlink()
    before = snapshot(paths.openclaw)
    with cli._staged_openclaw_state(paths.openclaw) as (candidate, _):
        assert sdk_status(candidate).returncode == 0
        assert snapshot(paths.openclaw) == before
    assert snapshot(paths.openclaw) == before


def test_newer_schema_refuses_downgrade(tmp_path, monkeypatch):
    pinned_openclaw_package(ROOT)
    paths, agent, _ = historic_state(tmp_path, monkeypatch)
    with closing(sqlite3.connect(agent)) as db:
        db.execute("PRAGMA user_version=2147483647")
    before = snapshot(paths.openclaw)
    with pytest.raises(RuntimeConfigurationError, match="exceeds SDK schema"):
        migrate_copied_provider_databases(
            paths.openclaw,
            runtime=ROOT / ".sat/openclaw",
            policy_path=cli.DEFAULT_PRODUCT_POLICY,
        )
    assert snapshot(paths.openclaw) == before


def test_unremoved_migration_container_keeps_candidate(tmp_path, monkeypatch):
    state = tmp_path / "openclaw"
    state.mkdir()
    (state / "sentinel").write_text("original")

    def blocked(candidate, **kwargs):
        raise StateMigrationCleanupError("exact container remains")

    monkeypatch.setattr(cli, "migrate_copied_provider_databases", blocked)
    with (
        pytest.raises(StateMigrationCleanupError, match="exact container remains"),
        cli._staged_openclaw_state(state),
    ):
        pytest.fail("unremoved container reached provider setup")
    candidates = tuple(tmp_path.glob(".openclaw.candidate-*"))
    assert len(candidates) == 1
    assert (candidates[0] / "sentinel").read_text() == "original"
    assert (state / "sentinel").read_text() == "original"
