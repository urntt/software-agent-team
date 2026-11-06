"""Docker sandbox image lifecycle management, lineages, and transition handoffs.

Manages verifiable, attributable sandbox images, safe parent lineages,
atomic mutable-tag transitions, rollback preserves, and unreferenced image pruning.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Sequence
from pathlib import Path
from uuid import uuid4

from software_agent_team import managed_install
from software_agent_team.docker_engine import (
    load_staged_docker_engine,
    verify_bound_docker_engine,
)
from software_agent_team.managed_install.constants import (
    _SANDBOX_IMAGE_ID_PATTERN,
    _SOURCE_REVISION_PATTERN,
    MANAGED_MARKER_NAME,
    SANDBOX_IMAGE_COMMAND_TIMEOUT_SECONDS,
    SANDBOX_IMAGE_OWNER_LABEL,
    SANDBOX_IMAGE_REFERENCE_LABEL,
    SANDBOX_IMAGE_ROLLBACK_REPOSITORY,
    SANDBOX_IMAGE_TRANSITION_FILE_PREFIX,
    ManagedInstallError,
    _docker_failure_detail,
)
from software_agent_team.managed_install.models import (
    ManagedInstallPaths,
    ManagedRootMarker,
    PersistedSandboxImageIdentity,
    PersistedSandboxImageLineage,
    PersistedSandboxImageTransition,
    SandboxImageIdentity,
    SandboxImageTransition,
)
from software_agent_team.managed_install.utils import (
    _fsync_directory,
    _lifecycle_file_lock,
    _require_real_directory,
)
from software_agent_team.versioning import (
    InstallationRecord,
    load_installation_record,
    save_installation_record,
)


def promote_legacy_docker_engine_record(
    *,
    project_root: Path,
    paths: ManagedInstallPaths,
    expected_record: InstallationRecord,
) -> InstallationRecord:
    """Complete a legacy updater's activation using its staged engine proof."""
    from software_agent_team import managed_install

    if expected_record.schema_version != 1:
        return expected_record
    identity = load_staged_docker_engine(project_root)
    managed_install.verify_docker_engine(identity)
    with managed_install._exclusive_update_lock(paths):
        if not paths.application_link.is_symlink():
            raise ManagedInstallError(
                "managed application link changed during migration"
            )
        active = paths.application_link.resolve(strict=True)
        if active != project_root.resolve(strict=True):
            raise ManagedInstallError("managed application changed during migration")
        managed_install._require_managed_release_target(active, paths)
        current = load_installation_record(paths.installation_record)
        if current != expected_record:
            raise ManagedInstallError("installation record changed during migration")
        managed_install.verify_docker_engine(identity)
        promoted = current.model_copy(
            update={"schema_version": 2, "docker_engine": identity}
        )
        save_installation_record(promoted, paths.installation_record)
        return promoted


def _configured_sandbox_image(application: Path) -> str | None:
    """Return the application-owned mutable image reference when one exists."""
    policy = application / "configs" / "product-policy.json"
    if not policy.exists() and not policy.is_symlink():
        return None
    if policy.is_symlink() or not policy.is_file():
        raise ManagedInstallError("managed sandbox policy must be a regular file")
    try:
        payload = json.loads(policy.read_text(encoding="utf-8"))
        image = payload["sandbox"]["image"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise ManagedInstallError(
            "managed sandbox policy does not declare a valid image"
        ) from error
    if (
        not isinstance(image, str)
        or not image
        or image != image.strip()
        or image.startswith("-")
        or any(character.isspace() or ord(character) < 32 for character in image)
    ):
        raise ManagedInstallError(
            "managed sandbox policy does not declare a valid image"
        )
    return image


def _active_predecessor_sandbox_image_reference(
    *,
    candidate_root: Path,
    managed_root: Path,
    root_marker: ManagedRootMarker,
    candidate_reference: str,
) -> str:
    """Resolve the active release's image without trusting the staged policy."""
    from software_agent_team import managed_install

    application_link = Path(root_marker.application_link)
    if not application_link.exists() and not application_link.is_symlink():
        return candidate_reference
    try:
        active_root = application_link.resolve(strict=True)
    except OSError as error:
        raise ManagedInstallError("managed application link is broken") from error
    if active_root == candidate_root:
        return candidate_reference
    active_lifecycle = managed_install._active_managed_lifecycle(active_root)
    if active_lifecycle is None or active_lifecycle[0] != managed_root:
        raise ManagedInstallError(
            "staged sandbox image transition has no attributable predecessor"
        )
    active_reference = _configured_sandbox_image(active_root)
    return candidate_reference if active_reference is None else active_reference


def prepare_staged_sandbox_image_transition(
    *,
    application_root: Path,
    installation_record_path: Path,
) -> None:
    """Persist the exact pre-build image lineage for an older active updater."""
    from software_agent_team import managed_install

    root = application_root.resolve(strict=True)
    marker = managed_install.load_managed_marker(root / MANAGED_MARKER_NAME)
    managed_root, root_marker = managed_install._managed_root_for_release(marker)
    if Path(root_marker.installation_record) != _specific_absolute_path(
        installation_record_path,
        label="installation record",
    ):
        raise ManagedInstallError(
            "managed sandbox image transition targets another installation record"
        )
    reference = _configured_sandbox_image(root)
    if reference is None:
        return
    previous_reference = _active_predecessor_sandbox_image_reference(
        candidate_root=root,
        managed_root=managed_root,
        root_marker=root_marker,
        candidate_reference=reference,
    )
    previous = managed_install._inspect_sandbox_image(previous_reference)
    previous_lineage = (
        ()
        if previous is None
        else managed_install._inspect_sandbox_image_lineage(
            previous,
            expected_reference=previous_reference,
        )
    )
    legacy_orphan_lineages = _inspect_legacy_sandbox_image_lineages(
        expected_reference=previous_reference,
        excluded_image_ids=frozenset(
            identity.image_id for identity in previous_lineage
        ),
    )
    _retire_pending_sandbox_image_transitions(managed_root)
    record = PersistedSandboxImageTransition(
        target_source_revision=marker.source_revision,
        previous_reference=previous_reference,
        reference=reference,
        previous=(
            None
            if previous is None
            else PersistedSandboxImageIdentity.from_runtime(previous)
        ),
        previous_lineage=tuple(
            PersistedSandboxImageIdentity.from_runtime(identity)
            for identity in previous_lineage
        ),
        legacy_orphan_lineages=tuple(
            PersistedSandboxImageLineage(
                root=PersistedSandboxImageIdentity.from_runtime(lineage[0]),
                lineage=tuple(
                    PersistedSandboxImageIdentity.from_runtime(identity)
                    for identity in lineage
                ),
            )
            for lineage in legacy_orphan_lineages
        ),
    )
    _write_sandbox_image_transition(
        _sandbox_image_transition_path(
            managed_root,
            marker.source_revision,
        ),
        record,
    )


def finalize_staged_sandbox_image_transition(
    *,
    application_root: Path,
    installation_record_path: Path,
) -> None:
    """Bind a prepared handoff to the image produced by the staged target."""
    from software_agent_team import managed_install

    root = application_root.resolve(strict=True)
    marker = managed_install.load_managed_marker(root / MANAGED_MARKER_NAME)
    managed_root, root_marker = managed_install._managed_root_for_release(marker)
    if Path(root_marker.installation_record) != _specific_absolute_path(
        installation_record_path,
        label="installation record",
    ):
        raise ManagedInstallError(
            "managed sandbox image transition targets another installation record"
        )
    path = _sandbox_image_transition_path(
        managed_root,
        marker.source_revision,
    )
    prepared = _load_sandbox_image_transition(path)
    if prepared is None:
        raise ManagedInstallError("managed sandbox image transition was not prepared")
    if prepared.target_source_revision != marker.source_revision:
        raise ManagedInstallError("managed sandbox image transition target changed")
    reference = _configured_sandbox_image(root)
    if reference is None or prepared.reference != reference:
        raise ManagedInstallError("managed sandbox image transition reference changed")
    candidate = managed_install._inspect_sandbox_image(reference)
    if candidate is None or not candidate.owned:
        raise ManagedInstallError(
            "managed installer did not produce an attributable sandbox image"
        )
    candidate_lineage = managed_install._inspect_sandbox_image_lineage(
        candidate,
        expected_reference=reference,
    )
    previous = prepared.previous
    if (
        previous is None
        or not previous.owned
        or previous.image_id == candidate.image_id
    ) and not prepared.legacy_orphan_lineages:
        _remove_sandbox_image_transition(path)
        return
    completed = PersistedSandboxImageTransition.model_validate(
        {
            **prepared.model_dump(mode="python"),
            "candidate": PersistedSandboxImageIdentity.from_runtime(candidate),
            "candidate_lineage": tuple(
                PersistedSandboxImageIdentity.from_runtime(identity)
                for identity in candidate_lineage
            ),
        }
    )
    _write_sandbox_image_transition(path, completed)


def reconcile_pending_sandbox_image_transition(project_root: Path) -> bool:
    """Reconcile proven predecessor image resources after target activation."""
    from software_agent_team import managed_install

    lifecycle = managed_install._active_managed_lifecycle(project_root)
    if lifecycle is None:
        return True
    managed_root, _root_marker, release_marker = lifecycle
    try:
        with _lifecycle_file_lock(managed_root / "update.lock", shared=False):
            current_lifecycle = managed_install._active_managed_lifecycle(project_root)
            if current_lifecycle != lifecycle:
                return False
            retired_complete = _reconcile_retired_sandbox_image_transitions(
                managed_root,
                current_reference=_configured_sandbox_image(project_root),
            )
            path = _sandbox_image_transition_path(
                managed_root,
                release_marker.source_revision,
            )
            transition = _load_sandbox_image_transition(path)
            if transition is None:
                return retired_complete
            if transition.target_source_revision != release_marker.source_revision:
                raise ManagedInstallError(
                    "managed sandbox image transition belongs to another release"
                )
            if transition.candidate is None:
                return False
            candidate = managed_install._inspect_sandbox_image(transition.reference)
            if (
                candidate is None
                or not candidate.owned
                or candidate.image_id != transition.candidate.image_id
            ):
                raise ManagedInstallError(
                    "active sandbox image differs from its pending transition"
                )
            candidate_lineage = managed_install._inspect_sandbox_image_lineage(
                candidate,
                expected_reference=transition.reference,
            )
            if _lineage_structure(candidate_lineage) != _lineage_structure(
                tuple(item.to_runtime() for item in transition.candidate_lineage)
            ):
                raise ManagedInstallError(
                    "active sandbox image lineage differs from its pending transition"
                )
            previous = transition.previous
            retained_lineage = tuple(
                item.to_runtime() for item in transition.candidate_lineage
            )
            previous_reference = transition.resolved_previous_reference
            complete = retired_complete
            if transition.rollback_reference is not None:
                released = _release_sandbox_image_rollback_reference(
                    SandboxImageTransition(
                        reference=previous_reference,
                        previous=(None if previous is None else previous.to_runtime()),
                        previous_lineage=(),
                        candidate=candidate,
                        candidate_lineage=(),
                        rollback_reference=transition.rollback_reference,
                    )
                )
                if not released:
                    return False
            if previous is not None and previous.owned:
                current_previous = managed_install._inspect_sandbox_image(
                    previous.image_id,
                    expected_reference=previous_reference,
                )
                if current_previous is not None and (
                    not current_previous.owned
                    or current_previous.parent_image_id != previous.parent_image_id
                ):
                    raise ManagedInstallError(
                        "retired sandbox image differs from its pending transition"
                    )
                if previous_reference == transition.reference:
                    previous_complete = (
                        managed_install._remove_attributable_unreferenced_lineage(
                            previous.to_runtime(),
                            expected_reference=previous_reference,
                            retired_lineage=tuple(
                                item.to_runtime()
                                for item in transition.previous_lineage
                            ),
                            retained_lineage=retained_lineage,
                        )
                    )
                    complete = previous_complete and complete
                elif current_previous is None:
                    raise ManagedInstallError(
                        "rollback sandbox image disappeared after activation"
                    )
                else:
                    if previous_reference not in current_previous.repository_tags:
                        raise ManagedInstallError(
                            "rollback sandbox image reference disappeared after "
                            "activation"
                        )
                    retained_lineage = tuple(
                        dict.fromkeys(
                            (
                                *retained_lineage,
                                *(
                                    item.to_runtime()
                                    for item in transition.previous_lineage
                                ),
                            )
                        )
                    )
            for legacy in transition.legacy_orphan_lineages:
                legacy_complete = (
                    managed_install._remove_attributable_unreferenced_lineage(
                        legacy.root.to_runtime(),
                        expected_reference=previous_reference,
                        retired_lineage=tuple(
                            item.to_runtime() for item in legacy.lineage
                        ),
                        retained_lineage=retained_lineage,
                        allow_reference_labeled_root=True,
                    )
                )
                complete = legacy_complete and complete
            current_candidate = managed_install._inspect_sandbox_image(
                transition.reference
            )
            if (
                current_candidate is None
                or not current_candidate.owned
                or current_candidate.image_id != transition.candidate.image_id
            ):
                raise ManagedInstallError(
                    "active sandbox image changed during predecessor cleanup"
                )
            if complete:
                _remove_sandbox_image_transition(path)
            return complete
    except ManagedInstallError as error:
        if str(error) == "a SAT task, managed install, or update is active":
            return False
        raise


def _retire_pending_sandbox_image_transitions(managed_root: Path) -> None:
    """Move prior handoffs before a new installer can overwrite their source slot."""
    pending = tuple(
        sorted(managed_root.glob(f"{SANDBOX_IMAGE_TRANSITION_FILE_PREFIX}*.json"))
    )
    retired = tuple(managed_root.glob(".sat-sandbox-image-retired-*.json"))
    if len(pending) + len(retired) > 64:
        raise ManagedInstallError("too many pending sandbox image transitions")
    for path in pending:
        transition = _load_sandbox_image_transition(path)
        if transition is None:
            continue
        if path != _sandbox_image_transition_path(
            managed_root, transition.target_source_revision
        ):
            raise ManagedInstallError("pending sandbox image transition path changed")
        destination = _retired_sandbox_image_transition_path(managed_root)
        if destination.exists() or destination.is_symlink():
            raise ManagedInstallError("retired sandbox image transition already exists")
        os.replace(path, destination)


def _retired_sandbox_image_transition_path(managed_root: Path) -> Path:
    """Allocate one transaction identity independent of release or channel."""
    return managed_root / f".sat-sandbox-image-retired-{uuid4().hex}.json"


def _reconcile_retired_sandbox_image_transitions(
    managed_root: Path,
    *,
    current_reference: str | None,
) -> bool:
    """Reclaim recorded predecessor resources without requiring an old active app."""
    from software_agent_team import managed_install

    paths = tuple(sorted(managed_root.glob(".sat-sandbox-image-retired-*.json")))
    if len(paths) > 64:
        raise ManagedInstallError("too many retired sandbox image transitions")
    if not paths:
        return True
    if current_reference is None:
        return False
    candidate = managed_install._inspect_sandbox_image(current_reference)
    if candidate is None or not candidate.owned:
        raise ManagedInstallError(
            "active sandbox image is unavailable for retired cleanup"
        )
    retained_lineage = managed_install._inspect_sandbox_image_lineage(
        candidate, expected_reference=current_reference
    )
    complete = True
    for path in paths:
        if (
            re.fullmatch(r"\.sat-sandbox-image-retired-[0-9a-f]{32}\.json", path.name)
            is None
        ):
            raise ManagedInstallError(
                "retired sandbox image transition path is invalid"
            )
        record = _load_sandbox_image_transition(path)
        if record is None:
            continue
        previous = record.previous
        reference = record.resolved_previous_reference
        released = _release_sandbox_image_rollback_reference(
            SandboxImageTransition(
                reference=reference,
                previous=None if previous is None else previous.to_runtime(),
                previous_lineage=(),
                candidate=candidate,
                candidate_lineage=retained_lineage,
                rollback_reference=record.rollback_reference,
            )
        )
        if not released:
            complete = False
            continue
        reclaimed = True
        if previous is not None:
            reclaimed = managed_install._remove_attributable_unreferenced_lineage(
                previous.to_runtime(),
                expected_reference=reference,
                retired_lineage=tuple(
                    item.to_runtime() for item in record.previous_lineage
                ),
                retained_lineage=retained_lineage,
            )
        for legacy in record.legacy_orphan_lineages:
            reclaimed = (
                managed_install._remove_attributable_unreferenced_lineage(
                    legacy.root.to_runtime(),
                    expected_reference=reference,
                    retired_lineage=tuple(item.to_runtime() for item in legacy.lineage),
                    retained_lineage=retained_lineage,
                    allow_reference_labeled_root=True,
                )
                and reclaimed
            )
        if reclaimed:
            _remove_sandbox_image_transition(path)
        complete = reclaimed and complete
    return complete


def _sandbox_image_transition_path(
    managed_root: Path,
    source_revision: str,
) -> Path:
    """Compute the path for a given revision's transition file."""
    if _SOURCE_REVISION_PATTERN.fullmatch(source_revision) is None:
        raise ManagedInstallError("sandbox image transition revision is invalid")
    root = _specific_absolute_path(managed_root, label="managed root")
    return root / (f"{SANDBOX_IMAGE_TRANSITION_FILE_PREFIX}{source_revision}.json")


def _specific_absolute_path(path: Path, *, label: str) -> Path:
    """Ensure path is absolute, normalized, and not a root directory."""
    expanded = path.expanduser()
    if (
        not expanded.is_absolute()
        or expanded == Path(expanded.anchor)
        or Path(os.path.normpath(expanded)) != expanded
    ):
        raise ManagedInstallError(f"{label} path must be specific and absolute")
    return expanded


def _write_sandbox_image_transition(
    path: Path,
    transition: PersistedSandboxImageTransition,
) -> None:
    """Write an image transition payload atomically."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _require_real_directory(path.parent, "installation record directory")
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ManagedInstallError("sandbox image transition must be a regular file")
    content = (
        json.dumps(transition.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    ).encode()
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _load_sandbox_image_transition(
    path: Path,
) -> PersistedSandboxImageTransition | None:
    """Read and validate a persisted sandbox image transition."""
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file():
        raise ManagedInstallError("sandbox image transition is invalid")
    try:
        return PersistedSandboxImageTransition.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as error:
        raise ManagedInstallError("sandbox image transition is invalid") from error


def _remove_sandbox_image_transition(path: Path) -> None:
    """Unlink a transition file and sync directory."""
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ManagedInstallError("sandbox image transition is invalid")
    path.unlink(missing_ok=True)
    _fsync_directory(path.parent)


def _lineage_structure(
    lineage: Sequence[SandboxImageIdentity],
) -> tuple[tuple[str, str | None], ...]:
    """Extract (image_id, parent_image_id) pairs from a sequence of image identities."""
    return tuple((identity.image_id, identity.parent_image_id) for identity in lineage)


def _docker_command(
    arguments: Sequence[str],
    *,
    check: bool,
) -> subprocess.CompletedProcess[str]:
    """Run one bounded Docker lifecycle command without a shell."""
    verify_bound_docker_engine()
    try:
        completed = subprocess.run(
            ("docker", *arguments),
            check=False,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=SANDBOX_IMAGE_COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise ManagedInstallError(
            "Docker command timed out after "
            f"{SANDBOX_IMAGE_COMMAND_TIMEOUT_SECONDS} seconds"
        ) from error
    except FileNotFoundError as error:
        raise ManagedInstallError("Docker CLI is unavailable") from error
    except (OSError, subprocess.SubprocessError) as error:
        raise ManagedInstallError("Docker command could not be completed") from error
    if check and completed.returncode != 0:
        raise ManagedInstallError(
            _docker_failure_detail(completed, operation="command")
        )
    return completed


def _inspect_sandbox_image(
    selector: str,
    *,
    expected_reference: str | None = None,
) -> SandboxImageIdentity | None:
    """Inspect one exact image without treating a missing image as owned."""
    from software_agent_team import managed_install

    completed = managed_install._docker_command(
        ("image", "inspect", selector),
        check=False,
    )
    if completed.returncode != 0:
        diagnostic = completed.stderr.lower()
        if "no such image" in diagnostic or "no such object" in diagnostic:
            return None
        raise ManagedInstallError(
            _docker_failure_detail(completed, operation="image inspection")
        )
    try:
        raw_payload = json.loads(completed.stdout)
        if (
            not isinstance(raw_payload, list)
            or len(raw_payload) != 1
            or not isinstance(raw_payload[0], dict)
        ):
            raise TypeError
        payload = raw_payload[0]
        image_id = payload["Id"]
        config = payload["Config"]
        labels = config.get("Labels") or {}
        raw_tags = payload.get("RepoTags") or []
        raw_parent = payload.get("Parent") or None
    except (AttributeError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise ManagedInstallError(
            "Docker returned malformed managed sandbox image metadata"
        ) from error
    if (
        not isinstance(image_id, str)
        or _SANDBOX_IMAGE_ID_PATTERN.fullmatch(image_id) is None
        or not isinstance(labels, dict)
        or not isinstance(raw_tags, list)
        or any(not isinstance(tag, str) for tag in raw_tags)
        or (
            raw_parent is not None
            and (
                not isinstance(raw_parent, str)
                or _SANDBOX_IMAGE_ID_PATTERN.fullmatch(raw_parent) is None
            )
        )
    ):
        raise ManagedInstallError(
            "Docker returned malformed managed sandbox image metadata"
        )
    reference = selector if expected_reference is None else expected_reference
    reference_labeled = labels.get(SANDBOX_IMAGE_REFERENCE_LABEL) == reference
    owned = labels.get(SANDBOX_IMAGE_OWNER_LABEL) == "true" and reference_labeled
    return SandboxImageIdentity(
        image_id=image_id,
        parent_image_id=raw_parent,
        repository_tags=tuple(raw_tags),
        owned=owned,
        reference_labeled=reference_labeled,
    )


def _inspect_sandbox_image_lineage(
    image: SandboxImageIdentity,
    *,
    expected_reference: str,
) -> tuple[SandboxImageIdentity, ...]:
    """Capture the locally available immutable parent chain rooted at one image."""
    from software_agent_team import managed_install

    lineage = [image]
    seen = {image.image_id}
    parent_id = image.parent_image_id
    while parent_id is not None:
        if parent_id in seen:
            raise ManagedInstallError("managed sandbox image lineage contains a cycle")
        parent = managed_install._inspect_sandbox_image(
            parent_id,
            expected_reference=expected_reference,
        )
        if parent is None:
            # Older SAT releases removed images without Docker's --no-prune
            # boundary. The daemon can therefore retain a runnable child whose
            # legacy Parent metadata names an image record that no longer
            # exists. Treat that absent record as the end of attributable
            # history; never invent or delete beyond it.
            break
        lineage.append(parent)
        seen.add(parent.image_id)
        parent_id = parent.parent_image_id
    return tuple(lineage)


def _sandbox_image_inventory(
    *,
    expected_reference: str,
    required_image_ids: Sequence[str] = (),
) -> dict[str, SandboxImageIdentity]:
    """Inspect all local image records needed to prove child relationships."""
    from software_agent_team import managed_install

    completed = managed_install._docker_command(
        ("image", "ls", "--all", "--no-trunc", "--quiet"),
        check=True,
    )
    listed_ids = tuple(line for line in completed.stdout.splitlines() if line)
    invalid_ids = tuple(
        image_id
        for image_id in listed_ids
        if _SANDBOX_IMAGE_ID_PATTERN.fullmatch(image_id) is None
    )
    if invalid_ids:
        raise ManagedInstallError("Docker returned an invalid sandbox image inventory")
    selectors = tuple(dict.fromkeys((*listed_ids, *required_image_ids)))
    inventory: dict[str, SandboxImageIdentity] = {}
    for image_id in selectors:
        identity = managed_install._inspect_sandbox_image(
            image_id,
            expected_reference=expected_reference,
        )
        if identity is not None:
            inventory[identity.image_id] = identity
    return inventory


def _inspect_legacy_sandbox_image_lineages(
    *,
    expected_reference: str,
    excluded_image_ids: frozenset[str],
) -> tuple[tuple[SandboxImageIdentity, ...], ...]:
    """Capture detached legacy roots bearing SAT's exact image-reference label."""
    from software_agent_team import managed_install

    inventory = _sandbox_image_inventory(expected_reference=expected_reference)
    children: dict[str, set[str]] = {}
    for identity in inventory.values():
        if identity.parent_image_id is not None:
            children.setdefault(identity.parent_image_id, set()).add(identity.image_id)
    roots = tuple(
        identity
        for identity in sorted(inventory.values(), key=lambda item: item.image_id)
        if identity.image_id not in excluded_image_ids
        and identity.reference_labeled
        and not identity.repository_tags
        and not children.get(identity.image_id)
        and not _image_has_container_references(identity.image_id)
    )
    if len(roots) > 64:
        raise ManagedInstallError(
            "too many legacy sandbox image roots require explicit recovery"
        )
    return tuple(
        managed_install._inspect_sandbox_image_lineage(
            root,
            expected_reference=expected_reference,
        )
        for root in roots
    )


def _image_has_container_references(image_id: str) -> bool:
    """Check if any container (running or stopped) is derived from this image."""
    from software_agent_team import managed_install

    completed = managed_install._docker_command(
        (
            "container",
            "ls",
            "--all",
            "--quiet",
            "--filter",
            f"ancestor={image_id}",
        ),
        check=True,
    )
    return bool(completed.stdout.strip())


def _remove_attributable_unreferenced_lineage(
    retired: SandboxImageIdentity,
    *,
    expected_reference: str,
    retired_lineage: tuple[SandboxImageIdentity, ...],
    retained_lineage: tuple[SandboxImageIdentity, ...],
    allow_reference_labeled_root: bool = False,
) -> bool:
    """Remove the exact unshared prefix of one attributable image lineage."""
    from software_agent_team import managed_install

    if not retired.owned and not (
        allow_reference_labeled_root and retired.reference_labeled
    ):
        return True
    if not retired_lineage:
        retired_lineage = managed_install._inspect_sandbox_image_lineage(
            retired,
            expected_reference=expected_reference,
        )
    if retired_lineage[0].image_id != retired.image_id:
        raise ManagedInstallError("managed sandbox image lineage root is invalid")
    for index, captured in enumerate(retired_lineage[:-1]):
        expected_parent = retired_lineage[index + 1].image_id
        if captured.parent_image_id != expected_parent:
            raise ManagedInstallError("managed sandbox image lineage is invalid")

    retained_ids = {identity.image_id for identity in retained_lineage}
    required_ids = tuple(
        identity.image_id for identity in (*retired_lineage, *retained_lineage)
    )
    inventory = _sandbox_image_inventory(
        expected_reference=expected_reference,
        required_image_ids=required_ids,
    )
    children: dict[str, set[str]] = {}
    for identity in inventory.values():
        if identity.parent_image_id is not None:
            children.setdefault(identity.parent_image_id, set()).add(identity.image_id)

    removal_ids: list[str] = []
    scheduled_descendants: set[str] = set()
    complete = True
    for captured in retired_lineage:
        current = inventory.get(captured.image_id)
        if current is None:
            scheduled_descendants.add(captured.image_id)
            continue
        if current.parent_image_id != captured.parent_image_id:
            raise ManagedInstallError("managed sandbox image lineage changed")
        if (
            current.image_id == retired.image_id
            and not current.owned
            and not (allow_reference_labeled_root and current.reference_labeled)
        ):
            break
        if current.image_id in retained_ids or current.repository_tags:
            break
        if managed_install._image_has_container_references(current.image_id):
            complete = False
            break
        external_children = (
            children.get(current.image_id, set()) - scheduled_descendants
        )
        if external_children:
            break
        removal_ids.append(current.image_id)
        scheduled_descendants.add(current.image_id)

    for image_id in removal_ids:
        # Reclamation happens after the candidate has become authoritative. A
        # no-force deletion can still lose a race to a new Docker reference;
        # stop at that boundary instead of turning a safe activation into an
        # impossible rollback after an earlier lineage record was removed.
        completed = managed_install._docker_command(
            ("image", "rm", "--no-prune", image_id),
            check=False,
        )
        if completed.returncode != 0:
            return False
    return complete


def _cleanup_superseded_sandbox_image(
    transition: SandboxImageTransition,
) -> None:
    """Delete only the unreferenced, attributable image replaced by activation."""
    previous = transition.previous
    if (
        previous is None
        or previous.image_id == transition.candidate.image_id
        or not previous.owned
    ):
        return
    managed_install._remove_attributable_unreferenced_lineage(
        previous,
        expected_reference=transition.reference,
        retired_lineage=transition.previous_lineage,
        retained_lineage=transition.candidate_lineage,
    )


def _preserve_sandbox_image_for_rollback(
    image: SandboxImageIdentity,
    *,
    expected_reference: str,
) -> str:
    """Keep the pre-stage image addressable while Docker moves its main tag."""
    from software_agent_team import managed_install

    rollback_reference = (
        f"{SANDBOX_IMAGE_ROLLBACK_REPOSITORY}:u{os.geteuid()}-{uuid4().hex}"
    )
    managed_install._docker_command(
        ("image", "tag", image.image_id, rollback_reference),
        check=False,
    )
    preserved = managed_install._inspect_sandbox_image(
        rollback_reference,
        expected_reference=expected_reference,
    )
    if preserved is None or preserved.image_id != image.image_id:
        managed_install._docker_command(
            ("image", "rm", "--no-prune", rollback_reference),
            check=False,
        )
        raise ManagedInstallError(
            "previous managed sandbox image could not be preserved for rollback"
        )
    return rollback_reference


def _release_sandbox_image_rollback_reference(
    transition: SandboxImageTransition,
) -> bool:
    """Remove only the temporary tag created by this image transaction."""
    from software_agent_team import managed_install

    rollback_reference = transition.rollback_reference
    if rollback_reference is None:
        return True
    preserved = managed_install._inspect_sandbox_image(
        rollback_reference,
        expected_reference=transition.reference,
    )
    if preserved is None:
        return True
    previous = transition.previous
    if previous is None or preserved.image_id != previous.image_id:
        raise ManagedInstallError(
            "managed sandbox image rollback reference changed unexpectedly"
        )
    managed_install._docker_command(
        ("image", "rm", "--no-prune", rollback_reference),
        check=False,
    )
    remaining = managed_install._inspect_sandbox_image(
        rollback_reference, expected_reference=transition.reference
    )
    if remaining is not None:
        if remaining.image_id != previous.image_id:
            raise ManagedInstallError(
                "managed sandbox image rollback reference changed unexpectedly"
            )
        if managed_install._image_has_container_references(previous.image_id):
            return False
        raise ManagedInstallError(
            "managed sandbox image rollback reference could not be removed"
        )
    return True


def _restore_sandbox_image_transition(
    transition: SandboxImageTransition,
) -> None:
    """Restore the pre-stage tag and retire an unused candidate after failure."""
    from software_agent_team import managed_install

    current = managed_install._inspect_sandbox_image(transition.reference)
    previous = transition.previous
    if previous is not None:
        preserved = (
            current
            if current is not None and current.image_id == previous.image_id
            else managed_install._inspect_sandbox_image(
                transition.rollback_reference or previous.image_id,
                expected_reference=transition.reference,
            )
        )
        if preserved is None or preserved.image_id != previous.image_id:
            raise ManagedInstallError(
                "previous managed sandbox image is unavailable for rollback"
            )
        if current is None or current.image_id != previous.image_id:
            managed_install._docker_command(
                ("image", "tag", preserved.image_id, transition.reference),
                check=True,
            )
    elif current is not None and current.image_id == transition.candidate.image_id:
        managed_install._docker_command(
            ("image", "rm", "--no-prune", transition.reference),
            check=True,
        )
    if transition.candidate.owned and (
        previous is None or transition.candidate.image_id != previous.image_id
    ):
        managed_install._remove_attributable_unreferenced_lineage(
            transition.candidate,
            expected_reference=transition.reference,
            retired_lineage=transition.candidate_lineage,
            retained_lineage=transition.previous_lineage,
        )
    _release_sandbox_image_rollback_reference(transition)
