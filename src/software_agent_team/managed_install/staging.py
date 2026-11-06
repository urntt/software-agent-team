"""Managed target resolution, schema compatibility preflights, and candidate staging.

Handles git cloning, depth-1 fetch of pinned references, release version validation,
pre-mutation sandbox preparation, and isolated application installation.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import tomllib
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack
from pathlib import Path

from software_agent_team import managed_install
from software_agent_team.docker_engine import (
    DockerEngineIdentity,
    bound_docker_engine,
    engine_environment,
    load_staged_docker_engine,
)
from software_agent_team.managed_install.constants import (
    _SOURCE_REF_PATTERN,
    _SOURCE_REVISION_PATTERN,
    CANDIDATE_COMPATIBILITY_TIMEOUT_SECONDS,
    MANAGED_MARKER_NAME,
    MAX_CANDIDATE_COMPATIBILITY_OUTPUT_BYTES,
    MAX_RENDERED_STATE_PROBLEMS,
    ManagedInstallError,
    _managed_failure_detail,
)
from software_agent_team.managed_install.models import (
    ManagedApplicationMarker,
    ManagedInstallPaths,
    ManagedTarget,
    PersistedSandboxImageTransition,
    SandboxImageIdentity,
    SandboxImageTransition,
    StagedApplication,
)
from software_agent_team.managed_install.utils import (
    CommandRunner,
    _capture_command,
    _require_real_directory,
    _run_command,
)
from software_agent_team.releases import ResolvedReleaseTarget, git_archive_digest
from software_agent_team.schema_compatibility import (
    CandidateCompatibilityEnvelope,
    PersistedSchemaCompatibilityReport,
    SchemaCompatibilityError,
    SchemaSupport,
    inspect_persisted_schema_compatibility,
)
from software_agent_team.state_layout import StateLayoutOperation, inspect_state_layout
from software_agent_team.versioning import (
    ManagedChannel,
    load_installation_record,
    parse_release_version,
)


def target_from_stable_release(target: ResolvedReleaseTarget) -> ManagedTarget:
    """Project a verified stable manifest into the shared transaction target."""
    manifest = target.manifest
    return ManagedTarget(
        channel=ManagedChannel.STABLE,
        release_version=manifest.release_version,
        source_revision=manifest.source_revision,
        source_ref=manifest.source_ref,
        repository_url=manifest.repository_url,
        artifact_digest=manifest.artifact_digest,
        schema_support=manifest.schema_support,
    )


def resolve_dev_target(
    *,
    repository_url: str,
    source_ref: str = "main",
    command_output: Callable[[Sequence[str]], str] | None = None,
) -> ManagedTarget:
    """Resolve an explicit dev ref to one advertised full revision."""
    if _SOURCE_REF_PATTERN.fullmatch(source_ref) is None or source_ref.startswith("-"):
        raise ManagedInstallError("dev source ref is invalid")
    run = command_output or _capture_command
    revision = source_ref if _SOURCE_REVISION_PATTERN.fullmatch(source_ref) else None
    if revision is None:
        output = run(("git", "ls-remote", "--refs", repository_url, source_ref))
        matches = []
        for line in output.splitlines():
            fields = line.split("\t")
            if len(fields) == 2 and fields[1] in {
                source_ref,
                f"refs/heads/{source_ref}",
                f"refs/tags/{source_ref}",
            }:
                matches.append(fields[0])
        matches = list(dict.fromkeys(matches))
        if len(matches) != 1 or _SOURCE_REVISION_PATTERN.fullmatch(matches[0]) is None:
            raise ManagedInstallError("dev source ref did not resolve unambiguously")
        revision = matches[0]
    return ManagedTarget(
        channel=ManagedChannel.DEV,
        release_version=None,
        source_revision=revision,
        source_ref=source_ref,
        repository_url=repository_url,
        artifact_digest=None,
        schema_support=None,
    )


def stage_managed_target(
    target: ManagedTarget,
    paths: ManagedInstallPaths,
    *,
    environment: Mapping[str, str] | None = None,
    command_runner: CommandRunner | None = None,
) -> StagedApplication:
    """Clone and fully install one target without changing active launchers."""

    managed_install._ensure_managed_root(paths)
    with managed_install._exclusive_update_lock(paths):
        return _stage_managed_target_locked(
            target,
            paths,
            environment=environment,
            command_runner=command_runner,
        )


def _stage_managed_target_locked(
    target: ManagedTarget,
    paths: ManagedInstallPaths,
    *,
    environment: Mapping[str, str] | None = None,
    command_runner: CommandRunner | None = None,
) -> StagedApplication:
    """Prepare one immutable final-path candidate while holding the lock."""

    managed_install._require_managed_root(paths)
    managed_install._validate_managed_destination(paths)
    _preflight_advertised_persisted_state(target, paths)
    managed_install._retire_pending_sandbox_image_transitions(paths.managed_root)
    paths.versions_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    _require_real_directory(paths.versions_root, "managed versions root")
    stage = Path(tempfile.mkdtemp(prefix=".stage-", dir=paths.versions_root)).resolve()
    cleanup_path: Path | None = stage
    runner = command_runner or _run_command
    sandbox_image_reference: str | None = None
    previous_sandbox_image: SandboxImageIdentity | None = None
    previous_sandbox_lineage: tuple[SandboxImageIdentity, ...] = ()
    sandbox_image_transition: SandboxImageTransition | None = None
    sandbox_image_rollback_reference: str | None = None
    sandbox_image_transaction_armed = False
    docker_engine: DockerEngineIdentity | None = None
    engine_scope = ExitStack()
    try:
        runner(("git", "init", "-b", "sat-managed", str(stage)), None, None)
        runner(
            ("git", "-C", str(stage), "remote", "add", "origin", target.repository_url),
            None,
            None,
        )
        runner(
            (
                "git",
                "-C",
                str(stage),
                "fetch",
                "--depth",
                "1",
                "origin",
                target.source_ref,
            ),
            None,
            None,
        )
        runner(
            ("git", "-C", str(stage), "checkout", "--detach", "--force", "FETCH_HEAD"),
            None,
            None,
        )
        actual_revision = _capture_command(
            ("git", "-C", str(stage), "rev-parse", "HEAD")
        )
        if actual_revision != target.source_revision:
            raise ManagedInstallError(
                "fetched source revision does not match the resolved target"
            )
        release_version = _project_release_version(stage)
        if (
            target.release_version is not None
            and release_version != target.release_version
        ):
            raise ManagedInstallError(
                "package release version does not match the resolved target"
            )
        archive_digest = git_archive_digest(stage)
        if (
            target.artifact_digest is not None
            and archive_digest != target.artifact_digest
        ):
            raise ManagedInstallError(
                "staged source archive does not match the release artifact digest"
            )
        marker = ManagedApplicationMarker(
            application_link=str(paths.application_link),
            channel=target.channel,
            release_version=release_version,
            source_revision=actual_revision,
            source_ref=target.source_ref,
            repository_url=target.repository_url,
            artifact_digest=archive_digest,
        )
        managed_install._write_marker(stage / MANAGED_MARKER_NAME, marker)
        final_path = managed_install._final_release_path(paths, marker)
        if final_path.exists() or final_path.is_symlink():
            if final_path.is_symlink() or not final_path.is_dir():
                raise ManagedInstallError(
                    "managed release destination is not a directory"
                )
            existing = managed_install.load_managed_marker(
                final_path / MANAGED_MARKER_NAME
            )
            if existing != marker:
                raise ManagedInstallError(
                    "managed release destination has conflicting provenance"
                )
            _validate_staged_application(final_path, marker)
            schema_support = target.schema_support or _read_staged_schema_support(
                final_path
            )
            shutil.rmtree(stage)
            cleanup_path = None
            return StagedApplication(
                path=final_path,
                marker=marker,
                schema_support=schema_support,
                created_candidate=False,
            )

        os.replace(stage, final_path)
        cleanup_path = final_path
        install_environment = {
            **(os.environ if environment is None else environment),
            "SAT_MANAGED_INSTALL": "1",
            "SAT_INSTALL_STAGE_ONLY": "1",
            "SAT_INSTALL_ROOT": str(paths.application_link),
            "SAT_INSTALL_METADATA_PATH": str(paths.installation_record),
        }
        install_environment.pop("VIRTUAL_ENV", None)
        sandbox_image_reference = managed_install._configured_sandbox_image(final_path)
        if sandbox_image_reference is not None:
            previous_record = load_installation_record(paths.installation_record)
            docker_engine = (
                previous_record.docker_engine
                if previous_record is not None
                and previous_record.docker_engine is not None
                else managed_install.discover_docker_engine(install_environment)
            )
            engine_scope.enter_context(bound_docker_engine(docker_engine))
            install_environment = engine_environment(docker_engine, install_environment)
            previous_sandbox_image = managed_install._inspect_sandbox_image(
                sandbox_image_reference
            )
            if previous_sandbox_image is not None:
                sandbox_image_rollback_reference = (
                    managed_install._preserve_sandbox_image_for_rollback(
                        previous_sandbox_image,
                        expected_reference=sandbox_image_reference,
                    )
                )
                previous_sandbox_lineage = (
                    managed_install._inspect_sandbox_image_lineage(
                        previous_sandbox_image,
                        expected_reference=sandbox_image_reference,
                    )
                )
            sandbox_image_transaction_armed = True
        runner(
            (str(final_path / "scripts" / "install.sh"),),
            final_path,
            install_environment,
        )
        if sandbox_image_reference is not None:
            if load_staged_docker_engine(final_path) != docker_engine:
                raise ManagedInstallError(
                    "staged installer changed the selected Docker engine"
                )
            candidate_image = managed_install._inspect_sandbox_image(
                sandbox_image_reference
            )
            if candidate_image is None or not candidate_image.owned:
                raise ManagedInstallError(
                    "managed installer did not produce an attributable sandbox image"
                )
            sandbox_image_transition = SandboxImageTransition(
                reference=sandbox_image_reference,
                previous=previous_sandbox_image,
                previous_lineage=previous_sandbox_lineage,
                candidate=candidate_image,
                candidate_lineage=managed_install._inspect_sandbox_image_lineage(
                    candidate_image,
                    expected_reference=sandbox_image_reference,
                ),
                rollback_reference=sandbox_image_rollback_reference,
            )
        _validate_staged_application(final_path, marker)
        if sandbox_image_transition is not None:
            handoff_path = managed_install._sandbox_image_transition_path(
                paths.managed_root, marker.source_revision
            )
            handoff = managed_install._load_sandbox_image_transition(handoff_path)
            if handoff is not None:
                if (
                    handoff.target_source_revision != marker.source_revision
                    or handoff.reference != sandbox_image_transition.reference
                    or handoff.candidate is None
                    or handoff.candidate.image_id != candidate_image.image_id
                    or (
                        sandbox_image_rollback_reference is not None
                        and (
                            handoff.previous is None
                            or previous_sandbox_image is None
                            or handoff.previous.image_id
                            != previous_sandbox_image.image_id
                        )
                    )
                ):
                    raise ManagedInstallError("staged sandbox image handoff changed")
                anchor_path = (
                    handoff_path
                    if "rollback_reference" in handoff.model_fields_set
                    else managed_install._retired_sandbox_image_transition_path(
                        paths.managed_root
                    )
                )
                managed_install._write_sandbox_image_transition(
                    anchor_path,
                    PersistedSandboxImageTransition.model_validate(
                        {
                            **handoff.model_dump(),
                            "rollback_reference": sandbox_image_rollback_reference,
                        }
                    ),
                )
        schema_support = target.schema_support or _read_staged_schema_support(
            final_path
        )
        cleanup_path = None
        return StagedApplication(
            path=final_path,
            marker=marker,
            schema_support=schema_support,
            created_candidate=True,
            sandbox_image_transition=sandbox_image_transition,
            docker_engine=docker_engine,
        )
    except BaseException as staging_error:
        rollback_error: ManagedInstallError | None = None
        rollback_reference_cleanup_error: ManagedInstallError | None = None
        if sandbox_image_reference is not None and sandbox_image_transaction_armed:
            try:
                current_image = managed_install._inspect_sandbox_image(
                    sandbox_image_reference
                )
                if sandbox_image_rollback_reference is not None or (
                    current_image is not None
                    and (
                        previous_sandbox_image is None
                        or current_image.image_id != previous_sandbox_image.image_id
                    )
                ):
                    rollback_candidate = current_image or previous_sandbox_image
                    if rollback_candidate is None:
                        raise ManagedInstallError(
                            "managed sandbox image rollback identity is unavailable"
                        )
                    managed_install._restore_sandbox_image_transition(
                        SandboxImageTransition(
                            reference=sandbox_image_reference,
                            previous=previous_sandbox_image,
                            previous_lineage=previous_sandbox_lineage,
                            candidate=rollback_candidate,
                            candidate_lineage=managed_install._inspect_sandbox_image_lineage(
                                rollback_candidate,
                                expected_reference=sandbox_image_reference,
                            ),
                            rollback_reference=sandbox_image_rollback_reference,
                        )
                    )
            except ManagedInstallError as restore_error:
                rollback_error = restore_error
        elif (
            sandbox_image_rollback_reference is not None
            and previous_sandbox_image is not None
        ):
            try:
                assert sandbox_image_reference is not None
                managed_install._release_sandbox_image_rollback_reference(
                    SandboxImageTransition(
                        reference=sandbox_image_reference,
                        previous=previous_sandbox_image,
                        previous_lineage=previous_sandbox_lineage,
                        candidate=previous_sandbox_image,
                        candidate_lineage=previous_sandbox_lineage,
                        rollback_reference=sandbox_image_rollback_reference,
                    )
                )
            except ManagedInstallError as restore_error:
                rollback_reference_cleanup_error = ManagedInstallError(
                    "temporary sandbox image reference cleanup failed "
                    f"({_managed_failure_detail(restore_error)})"
                )
        if cleanup_path is not None:
            shutil.rmtree(cleanup_path, ignore_errors=True)
        if rollback_reference_cleanup_error is not None:
            raise ManagedInstallError(
                "managed staging failed "
                f"({_managed_failure_detail(staging_error)}); "
                f"{_managed_failure_detail(rollback_reference_cleanup_error)}"
            ) from staging_error
        if rollback_error is not None:
            raise ManagedInstallError(
                "managed staging failed "
                f"({_managed_failure_detail(staging_error)}); sandbox image "
                f"rollback also failed ({_managed_failure_detail(rollback_error)})"
            ) from staging_error
        raise
    finally:
        engine_scope.close()


def _project_release_version(repository: Path) -> str:
    """Parse the release version from pyproject.toml."""
    try:
        payload = tomllib.loads(
            (repository / "pyproject.toml").read_text(encoding="utf-8")
        )
        value = payload["project"]["version"]
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as error:
        raise ManagedInstallError("staged package version is unavailable") from error
    if not isinstance(value, str):
        raise ManagedInstallError("staged package version is invalid")
    try:
        parse_release_version(value)
    except ValueError as error:
        raise ManagedInstallError("staged package version is invalid") from error
    return value


def _read_staged_schema_support(stage: Path) -> tuple[SchemaSupport, ...]:
    """Invoke the staged binary to read its schema compatibility support."""
    try:
        completed = subprocess.run(
            [str(stage / ".venv" / "bin" / "sat"), "version", "--json"],
            cwd=stage,
            env={**os.environ, "SAT_INSTALL_STAGE_ONLY": "1"},
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        payload = json.loads(completed.stdout)
        values = payload["schema_support"]
        support = tuple(SchemaSupport.model_validate(value) for value in values)
    except (
        OSError,
        KeyError,
        TypeError,
        json.JSONDecodeError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        ValueError,
    ) as error:
        raise ManagedInstallError(
            "staged application did not report valid schema compatibility"
        ) from error
    if len(support) != len({item.family for item in support}):
        raise ManagedInstallError(
            "staged application reported duplicate schema compatibility families"
        )
    return support


def _validate_staged_application(
    path: Path,
    expected_marker: ManagedApplicationMarker,
) -> None:
    """Verify that staged application files, markers, and executables are intact."""

    _require_real_directory(path, "staged application")
    marker = managed_install.load_managed_marker(path / MANAGED_MARKER_NAME)
    if marker != expected_marker:
        raise ManagedInstallError(
            "staged application marker changed after installation"
        )
    for relative in (".venv/bin/sat", "scripts/uninstall.sh"):
        candidate = path / relative
        if (
            candidate.is_symlink()
            or not candidate.is_file()
            or not os.access(candidate, os.X_OK)
        ):
            raise ManagedInstallError(
                f"staged application is missing executable {relative}"
            )
    _capture_command((str(path / ".venv/bin/sat"), "--help"))


def _inspect_state_with_candidate(
    staged: StagedApplication,
    paths: ManagedInstallPaths,
) -> CandidateCompatibilityEnvelope:
    """Run the verified candidate's read-only compatibility implementation."""
    command = (
        str(staged.path / ".venv/bin/sat"),
        "_managed-state-compatibility",
        "--expected-source-revision",
        staged.marker.source_revision,
        "--configuration-path",
        str(paths.configuration_path),
        "--installation-record-path",
        str(paths.installation_record),
        "--state-root",
        str(paths.state_root),
    )
    environment = {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
    }
    try:
        completed = subprocess.run(
            command,
            cwd=staged.path,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            timeout=CANDIDATE_COMPATIBILITY_TIMEOUT_SECONDS,
        )
        if len(completed.stdout.encode()) > MAX_CANDIDATE_COMPATIBILITY_OUTPUT_BYTES:
            raise ManagedInstallError(
                "candidate compatibility result exceeds the output safety limit"
            )
        return CandidateCompatibilityEnvelope.model_validate_json(completed.stdout)
    except ManagedInstallError:
        raise
    except (
        OSError,
        UnicodeError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        ValueError,
    ) as error:
        raise ManagedInstallError(
            "verified candidate did not produce a valid state compatibility result"
        ) from error


def _preflight_advertised_persisted_state(
    target: ManagedTarget,
    paths: ManagedInstallPaths,
) -> None:
    """Reject manifest-known state problems before cloning or application setup."""
    if target.channel is not ManagedChannel.STABLE or target.schema_support is None:
        return
    layout = inspect_state_layout(paths.state_root)
    if layout.ready:
        try:
            compatibility = inspect_persisted_schema_compatibility(
                configuration_path=paths.configuration_path,
                installation_record_path=paths.installation_record,
                state_root=paths.state_root,
                candidate_support=target.schema_support,
            )
        except SchemaCompatibilityError as error:
            compatibility = PersistedSchemaCompatibilityReport(
                compatible=False,
                observations=(),
                problems=(str(error),),
            )
    else:
        compatibility = PersistedSchemaCompatibilityReport(
            compatible=True,
            observations=(),
            problems=(),
        )
    if layout.ready and compatibility.compatible:
        return

    problems = [problem.detail for problem in layout.problems]
    problems.extend(compatibility.problems)
    remediations: list[str] = []
    if layout.problems:
        remediations.append(
            layout.problems[0].remediation(StateLayoutOperation.FIRST_RUN)
        )
    if not compatibility.compatible:
        legacy_example = paths.state_root.with_name(f"{paths.state_root.name}-legacy")
        remediations.append(
            "Stop every SAT task. To preserve incompatible state, rename the complete "
            f"state root {paths.state_root} to a new sibling path outside that root "
            f"(for example {legacy_example}), then rerun the installer. Do not create "
            "backup directories inside the SAT state root. Alternatively, use a "
            "compatible SAT release or an explicit supported migration."
        )
    raise ManagedInstallError(
        "advertised target cannot consume current persisted state before application "
        f"setup:\n{_render_bounded_state_problems(problems)}\n"
        f"remediation: {' '.join(remediations)}"
    )


def _render_schema_compatibility_failure(
    compatibility: PersistedSchemaCompatibilityReport,
    *,
    paths: ManagedInstallPaths,
    heading: str,
) -> str:
    """Format compatibility error message with remediation guidance."""
    legacy_example = paths.state_root.with_name(f"{paths.state_root.name}-legacy")
    return (
        f"{heading}:\n"
        f"{_render_bounded_state_problems(list(compatibility.problems))}\n"
        "remediation: stop every SAT task; preserve incompatible state only outside "
        f"{paths.state_root} (for example by renaming the complete root to the new "
        f"sibling {legacy_example}), then use a compatible release, an explicit "
        "supported migration, or rerun the installer with a new empty state root"
    )


def _render_bounded_state_problems(problems: list[str]) -> str:
    """Format problem list with truncation for high counts."""
    visible = problems[:MAX_RENDERED_STATE_PROBLEMS]
    lines = [f"- {problem}" for problem in visible]
    hidden = len(problems) - len(visible)
    if hidden:
        lines.append(f"- {hidden} additional problem(s) omitted")
    return "\n".join(lines)


def _schema_support_identity(
    support: Sequence[SchemaSupport],
) -> tuple[tuple[str, int, int, int], ...]:
    """Generate canonical tuple identity for schema support tuples."""
    return tuple(
        sorted(
            (
                item.family.value,
                item.current,
                item.minimum_readable,
                item.maximum_readable,
            )
            for item in support
        )
    )
