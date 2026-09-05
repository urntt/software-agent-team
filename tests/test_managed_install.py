"""Tests for staged and rollback-safe managed application activation."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
from pathlib import Path

import pytest

import software_agent_team.managed_install as managed_install_module
from software_agent_team.managed_install import (
    MANAGED_ROOT_MARKER_NAME,
    ManagedInstallError,
    ManagedInstallPaths,
    ManagedRootMarker,
    ManagedTarget,
    activate_staged_application,
    install_managed_target,
    resolve_dev_target,
    stage_managed_target,
)
from software_agent_team.releases import git_archive_digest
from software_agent_team.schema_compatibility import supported_schemas
from software_agent_team.versioning import (
    ManagedChannel,
    inspect_software_version,
    load_installation_record,
)


def git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", repository, *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def prepare_repository(tmp_path: Path, *, version: str = "0.1.0") -> tuple[Path, str]:
    repository = tmp_path / "source"
    (repository / "scripts").mkdir(parents=True)
    (repository / "pyproject.toml").write_text(
        f'[project]\nname = "software-agent-team"\nversion = "{version}"\n',
        encoding="utf-8",
    )
    installer = repository / "scripts/install.sh"
    installer.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
[[ "${SAT_MANAGED_INSTALL:-}" == "1" ]]
[[ "${SAT_INSTALL_STAGE_ONLY:-}" == "1" ]]
mkdir -p .venv/bin
printf '%s\n' '#!/usr/bin/env bash' 'exec /usr/bin/env bash "$@"' > .venv/bin/python
printf '#!%s/.venv/bin/python\nexit 0\n' "$PWD" > .venv/bin/sat
chmod 755 .venv/bin/python .venv/bin/sat
""",
        encoding="utf-8",
    )
    installer.chmod(0o755)
    uninstaller = repository / "scripts/uninstall.sh"
    uninstaller.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    uninstaller.chmod(0o755)
    (repository / ".gitignore").write_text(
        ".sat-managed-install\n.venv/\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-b", "main", repository], check=True)
    git(repository, "config", "user.name", "urntt")
    git(repository, "config", "user.email", "urntts@gmail.com")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "test: initialize managed source")
    return repository, git(repository, "rev-parse", "HEAD")


def paths(tmp_path: Path) -> ManagedInstallPaths:
    home = tmp_path / "home"
    return ManagedInstallPaths.from_environment(
        {
            "HOME": str(home),
            "SAT_INSTALL_METADATA_PATH": str(
                home / ".local/share/software-agent-team/installation.json"
            ),
            "SAT_BIN_DIR": str(home / ".local/bin"),
            "SAT_STATE_ROOT": str(home / ".local/state/software-agent-team"),
        }
    )


def mark_managed_root(install_paths: ManagedInstallPaths) -> None:
    install_paths.managed_root.mkdir(parents=True, exist_ok=True)
    marker = ManagedRootMarker(
        managed_root=str(install_paths.managed_root),
        application_link=str(install_paths.application_link),
        versions_root=str(install_paths.versions_root),
        installation_record=str(install_paths.installation_record),
        bin_directory=str(install_paths.bin_directory),
    )
    (install_paths.managed_root / MANAGED_ROOT_MARKER_NAME).write_text(
        marker.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )


def dev_target(repository: Path, revision: str, *, version: str | None = None):
    return ManagedTarget(
        channel=ManagedChannel.DEV,
        release_version=version,
        source_revision=revision,
        source_ref="main",
        repository_url=str(repository),
        artifact_digest=None,
        schema_support=supported_schemas(),
    )


def test_default_and_custom_install_paths_use_dedicated_managed_roots(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    default = ManagedInstallPaths.from_environment({"HOME": str(home)})
    custom_application = tmp_path / "applications/sat"
    custom = ManagedInstallPaths.from_environment(
        {
            "HOME": str(home),
            "SAT_INSTALL_ROOT": str(custom_application),
        }
    )

    assert default.managed_root == home / ".local/share/software-agent-team"
    assert default.application_link == default.managed_root / "app"
    assert default.versions_root == default.managed_root / "versions"
    assert custom.application_link == custom_application
    assert custom.managed_root == custom_application.parent / ".sat.sat-managed"
    assert custom.versions_root == custom.managed_root / "versions"
    assert custom.versions_root != custom_application.parent / "versions"


def test_install_records_the_complete_managed_root_ownership_boundary(
    tmp_path: Path,
) -> None:
    repository, revision = prepare_repository(tmp_path)
    install_paths = paths(tmp_path)

    install_managed_target(dev_target(repository, revision), install_paths)

    marker = json.loads(
        (install_paths.managed_root / MANAGED_ROOT_MARKER_NAME).read_text(
            encoding="utf-8"
        )
    )
    assert marker == {
        "application_link": str(install_paths.application_link),
        "bin_directory": str(install_paths.bin_directory),
        "installation_record": str(install_paths.installation_record),
        "managed_root": str(install_paths.managed_root),
        "schema_version": 1,
        "versions_root": str(install_paths.versions_root),
    }


def test_install_refuses_unowned_application_before_running_source_commands(
    tmp_path: Path,
) -> None:
    repository, revision = prepare_repository(tmp_path)
    install_paths = paths(tmp_path)
    install_paths.application_link.parent.mkdir(parents=True)
    install_paths.application_link.write_text("user content\n", encoding="utf-8")
    calls: list[tuple[str, ...]] = []

    with pytest.raises(ManagedInstallError, match="not owned by SAT"):
        install_managed_target(
            dev_target(repository, revision),
            install_paths,
            command_runner=lambda command, _cwd, _environment: calls.append(
                tuple(command)
            ),
        )

    assert not calls
    assert install_paths.application_link.read_text(encoding="utf-8") == (
        "user content\n"
    )


def test_install_refuses_to_claim_a_nonempty_unmarked_sidecar_root(
    tmp_path: Path,
) -> None:
    repository, revision = prepare_repository(tmp_path)
    install_paths = paths(tmp_path)
    install_paths.managed_root.mkdir(parents=True)
    user_file = install_paths.managed_root / "user-file.txt"
    user_file.write_text("preserve\n", encoding="utf-8")

    with pytest.raises(ManagedInstallError, match="no valid ownership marker"):
        install_managed_target(dev_target(repository, revision), install_paths)

    assert user_file.read_text(encoding="utf-8") == "preserve\n"
    assert not (install_paths.managed_root / MANAGED_ROOT_MARKER_NAME).exists()


def test_standard_install_holds_the_lifecycle_lock_while_staging(
    tmp_path: Path,
) -> None:
    repository, revision = prepare_repository(tmp_path)
    install_paths = paths(tmp_path)
    observations: list[bool] = []

    def run_locked_command(
        command: tuple[str, ...] | list[str],
        cwd: Path | None,
        environment: dict[str, str] | None,
    ) -> None:
        descriptor = os.open(install_paths.lock, os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            observations.append(True)
        finally:
            os.close(descriptor)
        subprocess.run(
            list(command),
            cwd=cwd,
            env=environment,
            check=True,
            timeout=30,
        )

    install_managed_target(
        dev_target(repository, revision),
        install_paths,
        command_runner=run_locked_command,
    )

    assert observations


def test_staged_install_drops_only_the_callers_active_virtual_environment(
    tmp_path: Path,
) -> None:
    repository, revision = prepare_repository(tmp_path)
    install_paths = paths(tmp_path)
    caller_environment = {
        **os.environ,
        "VIRTUAL_ENV": "/tmp/bootstrap-helper/.venv",
        "SAT_TEST_HANDOFF": "preserved",
    }
    install_environments: list[dict[str, str]] = []

    def capture_install_environment(
        command: tuple[str, ...] | list[str],
        cwd: Path | None,
        environment: dict[str, str] | None,
    ) -> None:
        if environment is not None:
            install_environments.append(dict(environment))
        subprocess.run(
            list(command),
            cwd=cwd,
            env=environment,
            check=True,
            timeout=30,
        )

    stage_managed_target(
        dev_target(repository, revision),
        install_paths,
        environment=caller_environment,
        command_runner=capture_install_environment,
    )

    assert len(install_environments) == 1
    assert "VIRTUAL_ENV" not in install_environments[0]
    assert install_environments[0]["SAT_TEST_HANDOFF"] == "preserved"
    assert caller_environment["VIRTUAL_ENV"] == "/tmp/bootstrap-helper/.venv"


def test_dev_resolution_accepts_one_advertised_ref_and_exact_revision(
    tmp_path: Path,
) -> None:
    repository, revision = prepare_repository(tmp_path)

    resolved = resolve_dev_target(repository_url=str(repository), source_ref="main")
    pinned = resolve_dev_target(repository_url=str(repository), source_ref=revision)

    assert resolved.source_revision == revision
    assert resolved.release_version is None
    assert pinned.source_revision == revision
    with pytest.raises(ManagedInstallError, match="unambiguously"):
        resolve_dev_target(
            repository_url=str(repository),
            source_ref="missing",
        )


def test_initial_install_stages_verifies_and_activates_one_logical_link(
    tmp_path: Path,
) -> None:
    repository, revision = prepare_repository(tmp_path)
    install_paths = paths(tmp_path)

    record = install_managed_target(
        dev_target(repository, revision),
        install_paths,
    )

    assert install_paths.application_link.is_symlink()
    active = install_paths.application_link.resolve(strict=True)
    assert active.parent == install_paths.versions_root
    assert active.name == f"0.1.0-g{revision[:12]}"
    assert record.application_path == str(install_paths.application_link)
    assert load_installation_record(install_paths.installation_record) == record
    assert (install_paths.bin_directory / "sat").readlink() == (
        install_paths.application_link / ".venv/bin/sat"
    )
    sat_target = active / ".venv/bin/sat"
    assert sat_target.read_text(encoding="utf-8").splitlines()[0] == (
        f"#!{active}/.venv/bin/python"
    )
    subprocess.run(
        [install_paths.bin_directory / "sat", "--version"],
        check=True,
        timeout=30,
    )
    report = inspect_software_version(
        project_root=active,
        environment={
            "SAT_INSTALL_METADATA_PATH": str(install_paths.installation_record)
        },
        installed_version="0.1.0",
    )
    assert report.install_mode.value == "managed"
    assert report.channel is ManagedChannel.DEV


def test_stable_stage_rejects_package_or_archive_identity_drift(
    tmp_path: Path,
) -> None:
    repository, revision = prepare_repository(tmp_path)
    install_paths = paths(tmp_path)
    git(repository, "tag", "v0.1.0")
    git(repository, "tag", "v0.2.0")

    with pytest.raises(ManagedInstallError, match="package release version"):
        stage_managed_target(
            ManagedTarget(
                channel=ManagedChannel.STABLE,
                release_version="0.2.0",
                source_revision=revision,
                source_ref="v0.2.0",
                repository_url=str(repository),
                artifact_digest=git_archive_digest(repository),
                schema_support=supported_schemas(),
            ),
            install_paths,
        )
    assert not tuple(install_paths.versions_root.glob(".stage-*"))

    with pytest.raises(ManagedInstallError, match="artifact digest"):
        stage_managed_target(
            ManagedTarget(
                channel=ManagedChannel.STABLE,
                release_version="0.1.0",
                source_revision=revision,
                source_ref="v0.1.0",
                repository_url=str(repository),
                artifact_digest="sha256:" + "0" * 64,
                schema_support=supported_schemas(),
            ),
            install_paths,
        )


def test_activation_failure_restores_previous_link_and_record(tmp_path: Path) -> None:
    repository, first_revision = prepare_repository(tmp_path)
    install_paths = paths(tmp_path)
    first_record = install_managed_target(
        dev_target(repository, first_revision),
        install_paths,
    )
    first_target = install_paths.application_link.resolve(strict=True)

    (repository / "change.txt").write_text("second\n", encoding="utf-8")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "test: add second revision")
    second_revision = git(repository, "rev-parse", "HEAD")
    staged = stage_managed_target(
        dev_target(repository, second_revision),
        install_paths,
    )

    def fail() -> None:
        raise RuntimeError("injected activation failure")

    with pytest.raises(RuntimeError, match="injected"):
        activate_staged_application(
            staged,
            install_paths,
            fail_after_link_swap=fail,
        )

    assert install_paths.application_link.resolve(strict=True) == first_target
    assert load_installation_record(install_paths.installation_record) == first_record


def test_failed_final_launcher_probe_rolls_back_initial_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, revision = prepare_repository(tmp_path)
    install_paths = paths(tmp_path)

    def fail_final_launcher(_paths: ManagedInstallPaths) -> None:
        raise ManagedInstallError("injected final launcher failure")

    monkeypatch.setattr(
        managed_install_module,
        "_validate_active_application",
        fail_final_launcher,
    )

    with pytest.raises(ManagedInstallError, match="injected final launcher"):
        install_managed_target(
            dev_target(repository, revision),
            install_paths,
        )

    assert not install_paths.application_link.exists()
    assert not install_paths.installation_record.exists()
    assert not (install_paths.bin_directory / "sat").exists()
    assert not (install_paths.bin_directory / "sat-uninstall").exists()
    assert not tuple(install_paths.versions_root.glob("0.1.0-g*"))


def test_active_run_blocks_activation_before_the_link_changes(tmp_path: Path) -> None:
    repository, revision = prepare_repository(tmp_path)
    install_paths = paths(tmp_path)
    staged = stage_managed_target(
        dev_target(repository, revision),
        install_paths,
    )
    run_state = install_paths.state_root / "runs/active/run.json"
    run_state.parent.mkdir(parents=True)
    run_state.write_text(json.dumps({"phase": "implementing"}), encoding="utf-8")

    with pytest.raises(ManagedInstallError, match="while a run is active"):
        activate_staged_application(staged, install_paths)

    assert not install_paths.application_link.exists()
    assert staged.path.is_dir()
    assert not install_paths.installation_record.exists()


def test_conflicting_launcher_rolls_back_initial_activation(tmp_path: Path) -> None:
    repository, revision = prepare_repository(tmp_path)
    install_paths = paths(tmp_path)
    install_paths.bin_directory.mkdir(parents=True)
    conflicting = install_paths.bin_directory / "sat"
    conflicting.write_text("user file\n", encoding="utf-8")

    with pytest.raises(ManagedInstallError, match="launcher already exists"):
        install_managed_target(
            dev_target(repository, revision),
            install_paths,
        )

    assert not install_paths.application_link.exists()
    assert not install_paths.installation_record.exists()
    assert conflicting.read_text(encoding="utf-8") == "user file\n"


def test_second_launcher_conflict_does_not_leave_a_partial_first_launcher(
    tmp_path: Path,
) -> None:
    repository, revision = prepare_repository(tmp_path)
    install_paths = paths(tmp_path)
    install_paths.bin_directory.mkdir(parents=True)
    conflicting = install_paths.bin_directory / "sat-uninstall"
    conflicting.write_text("user file\n", encoding="utf-8")

    with pytest.raises(ManagedInstallError, match="launcher already exists"):
        install_managed_target(
            dev_target(repository, revision),
            install_paths,
        )

    assert not (install_paths.bin_directory / "sat").exists()
    assert conflicting.read_text(encoding="utf-8") == "user file\n"


def test_legacy_direct_checkout_migrates_to_retryable_version_link(
    tmp_path: Path,
) -> None:
    repository, revision = prepare_repository(tmp_path)
    install_paths = paths(tmp_path)
    application = install_paths.application_link
    application.parent.mkdir(parents=True)
    subprocess.run(
        ["git", "clone", repository, application],
        check=True,
        capture_output=True,
    )
    (application / ".sat-managed-install").write_text(
        f"software-agent-team-managed-v1\nroot={application}\n",
        encoding="utf-8",
    )
    (application / ".venv/bin").mkdir(parents=True)
    sat = application / ".venv/bin/sat"
    sat.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    sat.chmod(0o755)
    staged = stage_managed_target(
        dev_target(repository, revision),
        install_paths,
    )

    def fail() -> None:
        raise RuntimeError("injected activation failure")

    with pytest.raises(RuntimeError, match="injected"):
        activate_staged_application(
            staged,
            install_paths,
            fail_after_link_swap=fail,
        )

    assert application.is_symlink()
    legacy = application.resolve(strict=True)
    assert legacy.name == f"legacy-g{revision[:12]}"
    marker = json.loads((legacy / ".sat-managed-install").read_text(encoding="utf-8"))
    assert marker["schema_version"] == 2
    assert marker["source_revision"] == revision


def test_symlink_update_lock_is_rejected_without_following_it(tmp_path: Path) -> None:
    repository, revision = prepare_repository(tmp_path)
    install_paths = paths(tmp_path)
    mark_managed_root(install_paths)
    target = tmp_path / "user-lock"
    target.write_text("preserve\n", encoding="utf-8")
    install_paths.lock.symlink_to(target)

    with pytest.raises(ManagedInstallError, match="lock must be a regular file"):
        install_managed_target(
            dev_target(repository, revision),
            install_paths,
        )

    assert target.read_text(encoding="utf-8") == "preserve\n"


def test_path_overrides_must_be_specific_absolute_paths(tmp_path: Path) -> None:
    with pytest.raises(ManagedInstallError, match="SAT_INSTALL_ROOT"):
        ManagedInstallPaths.from_environment(
            {
                "HOME": str(tmp_path),
                "SAT_INSTALL_ROOT": "relative/app",
            }
        )


def test_active_terminal_run_does_not_block_activation(tmp_path: Path) -> None:
    repository, revision = prepare_repository(tmp_path)
    install_paths = paths(tmp_path)
    run_state = install_paths.state_root / "runs/done/run.json"
    run_state.parent.mkdir(parents=True)
    run_state.write_text(
        json.dumps({"schema_version": 6, "phase": "completed"}),
        encoding="utf-8",
    )

    record = install_managed_target(
        dev_target(repository, revision),
        install_paths,
    )

    assert record.source_revision == revision
    assert install_paths.application_link.is_symlink()


def test_unsupported_persisted_schema_blocks_before_activation(tmp_path: Path) -> None:
    repository, revision = prepare_repository(tmp_path)
    install_paths = paths(tmp_path)
    run_state = install_paths.state_root / "runs/newer/run.json"
    run_state.parent.mkdir(parents=True)
    run_state.write_text(
        json.dumps({"schema_version": 7, "phase": "completed"}),
        encoding="utf-8",
    )

    with pytest.raises(ManagedInstallError, match="outside readable range"):
        install_managed_target(
            dev_target(repository, revision),
            install_paths,
        )

    assert not install_paths.application_link.exists()
    assert not install_paths.installation_record.exists()
