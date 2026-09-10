"""Tests for staged and rollback-safe managed application activation."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import software_agent_team.managed_install as managed_install_module
import software_agent_team.schema_compatibility as schema_compatibility_module
from software_agent_team.integrity import canonical_model_sha256
from software_agent_team.managed_install import (
    MANAGED_ROOT_MARKER_NAME,
    ManagedApplicationMarker,
    ManagedInstallError,
    ManagedInstallPaths,
    ManagedRootMarker,
    ManagedTarget,
    activate_staged_application,
    install_managed_target,
    load_managed_marker,
    managed_foreground_task_lease,
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
        f"""#!/usr/bin/env bash
set -euo pipefail
[[ "${{SAT_MANAGED_INSTALL:-}}" == "1" ]]
[[ "${{SAT_INSTALL_STAGE_ONLY:-}}" == "1" ]]
mkdir -p .venv/bin
printf '%s\n' '#!/usr/bin/env bash' 'exec {sys.executable} "$@"' > .venv/bin/python
printf '#!%s/.venv/bin/python\n' "$PWD" > .venv/bin/sat
tail -n +3 scripts/fake-sat.py >> .venv/bin/sat
chmod 755 .venv/bin/python .venv/bin/sat
""",
        encoding="utf-8",
    )
    installer.chmod(0o755)
    (repository / "scripts/fake-sat.py").write_text(
        f"""#!{sys.executable}
# Test-only staged application used to exercise the candidate protocol.
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, {str(Path(__file__).parents[1] / "src")!r})

from software_agent_team.schema_compatibility import (
    inspect_candidate_persisted_state,
    supported_schemas,
)


def main() -> int:
    if sys.argv[1:] == ["version", "--json"]:
        print(json.dumps({{
            "schema_support": [
                item.model_dump(mode="json") for item in supported_schemas()
            ]
        }}))
        return 0
    if sys.argv[1:2] != ["_managed-state-compatibility"]:
        return 0
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-source-revision", required=True)
    parser.add_argument("--configuration-path", required=True, type=Path)
    parser.add_argument("--installation-record-path", required=True, type=Path)
    parser.add_argument("--state-root", required=True, type=Path)
    args = parser.parse_args(sys.argv[2:])
    envelope = inspect_candidate_persisted_state(
        source_revision=args.expected_source_revision,
        configuration_path=args.configuration_path,
        installation_record_path=args.installation_record_path,
        state_root=args.state_root,
    )
    print(envelope.model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
""",
        encoding="utf-8",
    )
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


@pytest.mark.parametrize("first_channel", ["dev", "stable", "dev-ref"])
def test_same_source_provenance_switch_preserves_and_reuses_exact_targets(
    tmp_path: Path, first_channel: str
) -> None:
    repository, revision = prepare_repository(tmp_path)
    git(repository, "tag", "v0.1.0")
    install_paths = paths(tmp_path)
    dev = dev_target(repository, revision)
    stable = ManagedTarget(
        channel=ManagedChannel.STABLE,
        release_version="0.1.0",
        source_revision=revision,
        source_ref="v0.1.0",
        repository_url=str(repository),
        artifact_digest=git_archive_digest(repository),
        schema_support=supported_schemas(),
    )
    if first_channel == "dev-ref":
        first, second = dev, dev.model_copy(update={"source_ref": revision})
    elif first_channel == "dev":
        first, second = dev, stable
    else:
        first, second = stable, dev
    install_managed_target(first, install_paths)
    original = install_paths.application_link.resolve(strict=True)
    original_marker = (original / ".sat-managed-install").read_bytes()
    install_paths.state_root.mkdir(parents=True, exist_ok=True)
    sentinel = install_paths.state_root / "preserve.txt"
    sentinel.write_text("user state must survive\n", encoding="utf-8")

    changed = install_managed_target(second, install_paths)
    second_path = install_paths.application_link.resolve(strict=True)
    assert second_path != original
    assert changed.channel == second.channel
    assert changed.source_ref == second.source_ref
    assert changed.source_revision == revision
    assert (original / ".sat-managed-install").read_bytes() == original_marker
    assert sentinel.read_text(encoding="utf-8") == "user state must survive\n"
    staged_original = stage_managed_target(first, install_paths)
    assert staged_original.path == original
    assert staged_original.created_candidate is False
    activate_staged_application(staged_original, install_paths)
    assert install_paths.application_link.resolve(strict=True) == original
    assert (original / ".sat-managed-install").read_bytes() == original_marker
    assert len(tuple(install_paths.versions_root.iterdir())) == 2


def test_legacy_source_only_path_is_reused_without_relocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, revision = prepare_repository(tmp_path)
    install_paths = paths(tmp_path)
    original_target = dev_target(repository, revision)
    legacy = install_paths.versions_root / f"0.1.0-g{revision[:12]}"
    with monkeypatch.context() as old_layout:
        old_layout.setattr(
            managed_install_module,
            "_final_release_path",
            lambda _paths, _marker: legacy,
        )
        install_managed_target(original_target, install_paths)
    original_marker = (legacy / ".sat-managed-install").read_bytes()
    original_launcher = (legacy / ".venv/bin/sat").read_bytes()
    staged = stage_managed_target(original_target, install_paths)
    assert staged.path == legacy
    assert staged.created_candidate is False
    activate_staged_application(staged, install_paths)
    with managed_foreground_task_lease(legacy):
        pass
    install_managed_target(
        original_target.model_copy(update={"source_ref": revision}), install_paths
    )
    assert install_paths.application_link.resolve(strict=True) != legacy
    assert (legacy / ".sat-managed-install").read_bytes() == original_marker
    assert (legacy / ".venv/bin/sat").read_bytes() == original_launcher
    activate_staged_application(
        stage_managed_target(original_target, install_paths), install_paths
    )
    assert install_paths.application_link.resolve(strict=True) == legacy


@pytest.mark.parametrize("legacy_kind", ["symlink", "file", "missing-marker"])
def test_unsafe_legacy_release_is_not_followed_or_overwritten(
    tmp_path: Path, legacy_kind: str
) -> None:
    install_paths = paths(tmp_path)
    marker = ManagedApplicationMarker(
        application_link=str(install_paths.application_link),
        channel=ManagedChannel.DEV,
        release_version="0.1.0",
        source_revision="a" * 40,
        source_ref="main",
        repository_url="https://example.invalid/sat.git",
        artifact_digest="sha256:" + "b" * 64,
    )
    legacy = install_paths.versions_root / "0.1.0-gaaaaaaaaaaaa"
    legacy.parent.mkdir(parents=True)
    if legacy_kind == "symlink":
        foreign = tmp_path / "foreign"
        foreign.mkdir()
        legacy.symlink_to(foreign, target_is_directory=True)
    elif legacy_kind == "file":
        legacy.write_text("not owned", encoding="utf-8")
    else:
        legacy.mkdir()
    with pytest.raises(ManagedInstallError):
        managed_install_module._final_release_path(install_paths, marker)
    assert legacy.exists()
    if legacy_kind == "symlink":
        assert legacy.is_symlink()
        assert tuple(foreign.iterdir()) == ()
    elif legacy_kind == "file":
        assert legacy.read_text(encoding="utf-8") == "not owned"
    else:
        assert tuple(legacy.iterdir()) == ()


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
    marker = load_managed_marker(active / ".sat-managed-install")
    assert active.name == f"0.1.0-p{canonical_model_sha256(marker)}"
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


def test_consecutive_upgrades_retain_only_active_and_direct_predecessor(
    tmp_path: Path,
) -> None:
    repository, first_revision = prepare_repository(tmp_path)
    install_paths = paths(tmp_path)
    install_managed_target(dev_target(repository, first_revision), install_paths)
    first_release = install_paths.application_link.resolve(strict=True)

    (repository / "second.txt").write_text("second\n", encoding="utf-8")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "test: add second revision")
    second_revision = git(repository, "rev-parse", "HEAD")
    install_managed_target(dev_target(repository, second_revision), install_paths)
    second_release = install_paths.application_link.resolve(strict=True)

    (repository / "third.txt").write_text("third\n", encoding="utf-8")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "test: add third revision")
    third_revision = git(repository, "rev-parse", "HEAD")
    install_managed_target(dev_target(repository, third_revision), install_paths)
    third_release = install_paths.application_link.resolve(strict=True)

    assert not first_release.exists()
    assert second_release.is_dir()
    assert third_release.is_dir()
    assert set(install_paths.versions_root.iterdir()) == {
        second_release,
        third_release,
    }

    activate_staged_application(
        stage_managed_target(
            dev_target(repository, third_revision),
            install_paths,
        ),
        install_paths,
    )
    assert set(install_paths.versions_root.iterdir()) == {
        second_release,
        third_release,
    }


def test_upgrade_refuses_to_delete_an_unattributed_version_entry(
    tmp_path: Path,
) -> None:
    repository, first_revision = prepare_repository(tmp_path)
    install_paths = paths(tmp_path)
    first_record = install_managed_target(
        dev_target(repository, first_revision),
        install_paths,
    )
    first_release = install_paths.application_link.resolve(strict=True)
    foreign = install_paths.versions_root / "foreign"
    foreign.mkdir()
    (foreign / "preserve.txt").write_text("user data\n", encoding="utf-8")
    (repository / "second.txt").write_text("second\n", encoding="utf-8")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "test: add second revision")
    second_revision = git(repository, "rev-parse", "HEAD")

    with pytest.raises(
        ManagedInstallError, match="managed application marker is invalid"
    ):
        install_managed_target(
            dev_target(repository, second_revision),
            install_paths,
        )

    assert install_paths.application_link.resolve(strict=True) == first_release
    assert load_installation_record(install_paths.installation_record) == first_record
    assert (foreign / "preserve.txt").read_text(encoding="utf-8") == "user data\n"


def test_successful_activation_removes_only_attributable_dangling_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference = "sat-python-quality:phase1-v6"
    previous_id = "sha256:" + "a" * 64
    candidate_id = "sha256:" + "b" * 64
    commands: list[tuple[str, ...]] = []

    def fake_docker(
        arguments: tuple[str, ...],
        *,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        commands.append(arguments)
        if arguments[:2] == ("image", "inspect"):
            assert arguments[-1] == previous_id
            payload = {
                "Id": previous_id,
                "RepoTags": None,
                "Config": {
                    "Labels": {
                        managed_install_module.SANDBOX_IMAGE_OWNER_LABEL: "true",
                        managed_install_module.SANDBOX_IMAGE_REFERENCE_LABEL: reference,
                    }
                },
            }
            return subprocess.CompletedProcess(arguments, 0, json.dumps(payload), "")
        if arguments[:2] == ("container", "ls"):
            return subprocess.CompletedProcess(arguments, 0, "", "")
        if arguments[:2] == ("image", "rm"):
            assert check is True
            return subprocess.CompletedProcess(arguments, 0, previous_id + "\n", "")
        raise AssertionError(f"unexpected Docker command: {arguments}")

    monkeypatch.setattr(managed_install_module, "_docker_command", fake_docker)
    transition = managed_install_module.SandboxImageTransition(
        reference=reference,
        previous=managed_install_module.SandboxImageIdentity(
            image_id=previous_id,
            repository_tags=(reference,),
            owned=True,
        ),
        candidate=managed_install_module.SandboxImageIdentity(
            image_id=candidate_id,
            repository_tags=(reference,),
            owned=True,
        ),
    )

    managed_install_module._cleanup_superseded_sandbox_image(transition)

    assert ("image", "rm", previous_id) in commands


def test_successful_activation_preserves_unattributed_previous_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_docker(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("an unattributed image must not be touched")

    monkeypatch.setattr(managed_install_module, "_docker_command", reject_docker)
    transition = managed_install_module.SandboxImageTransition(
        reference="sat-python-quality:phase1-v6",
        previous=managed_install_module.SandboxImageIdentity(
            image_id="sha256:" + "a" * 64,
            repository_tags=(),
            owned=False,
        ),
        candidate=managed_install_module.SandboxImageIdentity(
            image_id="sha256:" + "b" * 64,
            repository_tags=("sat-python-quality:phase1-v6",),
            owned=True,
        ),
    )

    managed_install_module._cleanup_superseded_sandbox_image(transition)


def test_failed_activation_restores_previous_image_tag_and_removes_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference = "sat-python-quality:phase1-v6"
    previous_id = "sha256:" + "a" * 64
    candidate_id = "sha256:" + "b" * 64
    current_tag = {"image_id": candidate_id}
    removed: set[str] = set()

    def identity_payload(image_id: str) -> str:
        tags = [reference] if current_tag["image_id"] == image_id else None
        return json.dumps(
            {
                "Id": image_id,
                "RepoTags": tags,
                "Config": {
                    "Labels": {
                        managed_install_module.SANDBOX_IMAGE_OWNER_LABEL: "true",
                        managed_install_module.SANDBOX_IMAGE_REFERENCE_LABEL: reference,
                    }
                },
            }
        )

    def fake_docker(
        arguments: tuple[str, ...],
        *,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        if arguments[:2] == ("image", "inspect"):
            selector = arguments[-1]
            image_id = current_tag["image_id"] if selector == reference else selector
            if image_id in removed:
                return subprocess.CompletedProcess(
                    arguments,
                    1,
                    "",
                    "Error response from daemon: No such image",
                )
            return subprocess.CompletedProcess(
                arguments,
                0,
                identity_payload(image_id),
                "",
            )
        if arguments[:2] == ("image", "tag"):
            assert check is True
            assert arguments[2:] == (previous_id, reference)
            current_tag["image_id"] = previous_id
            return subprocess.CompletedProcess(arguments, 0, "", "")
        if arguments[:2] == ("container", "ls"):
            return subprocess.CompletedProcess(arguments, 0, "", "")
        if arguments[:2] == ("image", "rm"):
            assert check is True
            removed.add(arguments[2])
            return subprocess.CompletedProcess(arguments, 0, "", "")
        raise AssertionError(f"unexpected Docker command: {arguments}")

    monkeypatch.setattr(managed_install_module, "_docker_command", fake_docker)
    transition = managed_install_module.SandboxImageTransition(
        reference=reference,
        previous=managed_install_module.SandboxImageIdentity(
            image_id=previous_id,
            repository_tags=(reference,),
            owned=True,
        ),
        candidate=managed_install_module.SandboxImageIdentity(
            image_id=candidate_id,
            repository_tags=(reference,),
            owned=True,
        ),
    )

    managed_install_module._restore_sandbox_image_transition(transition)

    assert current_tag["image_id"] == previous_id
    assert candidate_id in removed


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
    assert not tuple(install_paths.versions_root.iterdir())


def test_active_run_blocks_activation_before_the_link_changes(tmp_path: Path) -> None:
    repository, revision = prepare_repository(tmp_path)
    install_paths = paths(tmp_path)
    staged = stage_managed_target(
        dev_target(repository, revision),
        install_paths,
    )
    run_state = install_paths.state_root / "runs/active/run.json"
    run_state.parent.mkdir(parents=True)
    run_state.write_text(
        json.dumps({"schema_version": 6, "phase": "implementing"}),
        encoding="utf-8",
    )

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
    run_lock = install_paths.state_root / "runs/.lock"
    run_lock.touch(mode=0o644)

    record = install_managed_target(
        dev_target(repository, revision),
        install_paths,
    )

    assert record.source_revision == revision
    assert install_paths.application_link.is_symlink()
    assert run_lock.is_file()


def test_candidate_not_current_process_owns_persisted_schema_interpretation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, revision = prepare_repository(tmp_path)
    install_paths = paths(tmp_path)
    run_state = install_paths.state_root / "runs/done/run.json"
    run_state.parent.mkdir(parents=True)
    run_state.write_text(
        json.dumps({"schema_version": 6, "phase": "completed"}),
        encoding="utf-8",
    )

    def reject_from_current_process(**_kwargs: object) -> None:
        raise AssertionError("the current process must not interpret candidate state")

    monkeypatch.setattr(
        schema_compatibility_module,
        "inspect_persisted_schema_compatibility",
        reject_from_current_process,
    )

    record = install_managed_target(
        dev_target(repository, revision),
        install_paths,
    )

    assert record.source_revision == revision


def test_invalid_candidate_compatibility_result_preserves_active_release(
    tmp_path: Path,
) -> None:
    repository, first_revision = prepare_repository(tmp_path)
    install_paths = paths(tmp_path)
    first_record = install_managed_target(
        dev_target(repository, first_revision),
        install_paths,
    )
    first_release = install_paths.application_link.resolve(strict=True)
    candidate_helper = repository / "scripts/fake-sat.py"
    content = candidate_helper.read_text(encoding="utf-8")
    candidate_helper.write_text(
        content.replace(
            "print(envelope.model_dump_json())",
            'print("not-json")',
        ),
        encoding="utf-8",
    )
    git(repository, "add", ".")
    git(repository, "commit", "-m", "test: corrupt candidate response")
    bad_revision = git(repository, "rev-parse", "HEAD")

    with pytest.raises(ManagedInstallError, match="valid state compatibility"):
        install_managed_target(
            dev_target(repository, bad_revision),
            install_paths,
        )

    assert install_paths.application_link.resolve(strict=True) == first_release
    assert load_installation_record(install_paths.installation_record) == first_record
    assert tuple(install_paths.versions_root.iterdir()) == (first_release,)


def test_foreground_task_lease_blocks_activation_until_task_exit(
    tmp_path: Path,
) -> None:
    repository, first_revision = prepare_repository(tmp_path)
    install_paths = paths(tmp_path)
    first_record = install_managed_target(
        dev_target(repository, first_revision),
        install_paths,
    )
    active = install_paths.application_link.resolve(strict=True)
    (repository / "change.txt").write_text("second\n", encoding="utf-8")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "test: add second managed revision")
    second_revision = git(repository, "rev-parse", "HEAD")

    with managed_foreground_task_lease(active):
        with pytest.raises(ManagedInstallError, match="SAT task"):
            install_managed_target(
                dev_target(repository, second_revision),
                install_paths,
            )
        assert (
            load_installation_record(install_paths.installation_record) == first_record
        )

    second_record = install_managed_target(
        dev_target(repository, second_revision),
        install_paths,
    )
    assert second_record.source_revision == second_revision


def test_stale_loaded_release_refuses_new_task_after_activation(
    tmp_path: Path,
) -> None:
    repository, first_revision = prepare_repository(tmp_path)
    install_paths = paths(tmp_path)
    install_managed_target(dev_target(repository, first_revision), install_paths)
    first_release = install_paths.application_link.resolve(strict=True)
    (repository / "change.txt").write_text("second\n", encoding="utf-8")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "test: add replacement revision")
    second_revision = git(repository, "rev-parse", "HEAD")
    install_managed_target(dev_target(repository, second_revision), install_paths)

    with (
        pytest.raises(ManagedInstallError, match="changed before task admission"),
        managed_foreground_task_lease(first_release),
    ):
        pytest.fail("a stale loaded release must not enter the product flow")


def test_foreground_task_lease_is_released_by_kernel_after_process_crash(
    tmp_path: Path,
) -> None:
    repository, revision = prepare_repository(tmp_path)
    install_paths = paths(tmp_path)
    install_managed_target(dev_target(repository, revision), install_paths)
    active = install_paths.application_link.resolve(strict=True)
    program = """
import os
import sys
from pathlib import Path
from software_agent_team.managed_install import managed_foreground_task_lease

with managed_foreground_task_lease(Path(sys.argv[1])):
    print("lease-acquired", flush=True)
    os._exit(23)
"""

    completed = subprocess.run(
        [sys.executable, "-c", program, str(active)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 23
    assert completed.stdout == "lease-acquired\n"
    descriptor = os.open(install_paths.lock, os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


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
