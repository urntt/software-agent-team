"""Configuration loading, cross-validation, and mounting for quality gates.

Handles loading of policy files (run-policy.json) and quality gate manifests
(benchmark.json), strict JSON validation with duplicate-key rejection, trusted
relative path resolution, command argv boundary validation, and binding approved
task briefs as read-only container mounts.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path, PurePosixPath

from software_agent_team.artifacts import TaskBrief
from software_agent_team.integrity import canonical_model_sha256
from software_agent_team.quality_gates.types import (
    QualityGateConfiguration,
    QualityGateConfigurationError,
    QualityGateManifest,
    ResolvedInputMount,
    RunPolicy,
    _is_within_posix,
)

_MAX_MANIFEST_BYTES = 1_048_576


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise QualityGateConfigurationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _read_json(path: Path) -> tuple[object, bytes]:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise QualityGateConfigurationError(
            f"cannot read manifest {path}: {error}"
        ) from error
    if len(raw) > _MAX_MANIFEST_BYTES:
        raise QualityGateConfigurationError(
            f"manifest exceeds {_MAX_MANIFEST_BYTES} bytes"
        )
    try:
        payload = json.loads(raw, object_pairs_hook=_reject_duplicate_json_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise QualityGateConfigurationError(
            f"invalid JSON in {path}: {error}"
        ) from error
    return payload, raw


def _resolve_trusted_relative(root: Path, relative: str, *, label: str) -> Path:
    candidate = root / relative
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise QualityGateConfigurationError(
            f"{label} does not exist: {relative}"
        ) from error
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise QualityGateConfigurationError(
            f"{label} escapes the quality-manifest directory"
        ) from error
    if "," in str(resolved):
        raise QualityGateConfigurationError(f"{label} path cannot contain a comma")
    return resolved


def _validate_manifest_commands(
    manifest: QualityGateManifest, policy: RunPolicy
) -> None:
    allowed_roots = {
        PurePosixPath(policy.sandbox.workspace_target),
        PurePosixPath(policy.sandbox.tmpfs_target),
        *(PurePosixPath(mount.target) for mount in manifest.input_mounts),
    }
    maximum_gate_seconds = 0
    for gate in manifest.gates:
        if (
            gate.timeout_seconds is not None
            and gate.timeout_seconds > policy.limits.command_timeout_seconds
        ):
            raise QualityGateConfigurationError(
                f"gate {gate.id} timeout exceeds the run policy"
            )
        maximum_gate_seconds += (
            gate.timeout_seconds or policy.limits.command_timeout_seconds
        )
        for argument in gate.argv[1:]:
            candidate = argument.split("=", 1)[-1] if "=" in argument else argument
            if not candidate.startswith("/"):
                continue
            path = PurePosixPath(candidate)
            if ".." in path.parts or not any(
                _is_within_posix(path, root) for root in allowed_roots
            ):
                raise QualityGateConfigurationError(
                    f"gate {gate.id} references an unmounted absolute path"
                )
    if maximum_gate_seconds > policy.limits.total_timeout_seconds:
        raise QualityGateConfigurationError(
            "sum of gate timeouts exceeds the total run-policy timeout"
        )

    for name, value in policy.sandbox.environment.items():
        if value.startswith("/"):
            path = PurePosixPath(value)
            if ".." in path.parts or not any(
                _is_within_posix(path, root) for root in allowed_roots
            ):
                raise QualityGateConfigurationError(
                    f"sandbox environment {name} references an unmounted path"
                )


def load_quality_gate_configuration(
    policy_path: Path | str, manifest_path: Path | str
) -> QualityGateConfiguration:
    """Load and cross-validate one authoritative policy and quality manifest."""

    policy_path = Path(policy_path).resolve(strict=True)
    manifest_path = Path(manifest_path).resolve(strict=True)
    policy_payload, policy_raw = _read_json(policy_path)
    manifest_payload, manifest_raw = _read_json(manifest_path)
    try:
        policy = RunPolicy.model_validate(policy_payload)
        manifest = QualityGateManifest.model_validate(manifest_payload)
    except ValueError as error:
        raise QualityGateConfigurationError(str(error)) from error
    if manifest.policy_id != policy.id:
        raise QualityGateConfigurationError("quality manifest policy ID does not match")

    manifest_root = manifest_path.parent.resolve(strict=True)
    task_brief_path = _resolve_trusted_relative(
        manifest_root, manifest.task_brief, label="task brief"
    )
    if not task_brief_path.is_file():
        raise QualityGateConfigurationError("task brief must be a regular file")
    task_payload, task_raw = _read_json(task_brief_path)
    try:
        task_brief = TaskBrief.model_validate(task_payload)
    except ValueError as error:
        raise QualityGateConfigurationError(str(error)) from error
    if not task_brief.confirmed:
        raise QualityGateConfigurationError(
            "quality-manifest task brief must be confirmed"
        )

    resolved_mounts: list[ResolvedInputMount] = []
    for mount in manifest.input_mounts:
        source = _resolve_trusted_relative(
            manifest_root, mount.source, label=f"input mount {mount.id}"
        )
        resolved_mounts.append(
            ResolvedInputMount(
                id=mount.id,
                source=source,
                target=PurePosixPath(mount.target),
            )
        )

    known_criteria = {criterion.id for criterion in task_brief.acceptance_criteria}
    gate_criteria = {
        criterion_id for gate in manifest.gates for criterion_id in gate.criterion_ids
    }
    declared_criteria = gate_criteria | set(manifest.manual_review_criteria)
    unknown = declared_criteria - known_criteria
    missing = known_criteria - declared_criteria
    if unknown:
        raise QualityGateConfigurationError(
            "quality manifest references unknown criteria: "
            + ", ".join(sorted(unknown))
        )
    if missing:
        raise QualityGateConfigurationError(
            f"quality manifest leaves criteria unassigned: {', '.join(sorted(missing))}"
        )
    _validate_manifest_commands(manifest, policy)

    def digest(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    return QualityGateConfiguration(
        policy=policy,
        manifest=manifest,
        task_brief=task_brief,
        policy_path=policy_path,
        manifest_path=manifest_path,
        task_brief_path=task_brief_path,
        policy_sha256=digest(policy_raw),
        manifest_sha256=digest(manifest_raw),
        task_brief_sha256=digest(task_raw),
        input_mounts=tuple(resolved_mounts),
    )


def bind_run_task_brief_mount(
    configuration: QualityGateConfiguration,
    *,
    run_directory: Path,
    expected_run_id: str,
    expected_sha256: str,
) -> QualityGateConfiguration:
    """Bind the verified, approved brief as a read-only quality-gate input."""

    mounts = tuple(
        mount
        for mount in configuration.input_mounts
        if mount.id == "approved_task_brief"
    )
    if len(mounts) != 1:
        raise QualityGateConfigurationError(
            "quality profile must declare one approved_task_brief input mount"
        )
    source = run_directory / "task-brief.json"
    if source.is_symlink() or not source.is_file():
        raise QualityGateConfigurationError("approved run task brief is unavailable")
    try:
        raw = source.read_bytes()
        if len(raw) > _MAX_MANIFEST_BYTES:
            raise ValueError("approved run task brief is too large")
        brief = TaskBrief.model_validate_json(raw)
    except (OSError, ValueError) as error:
        raise QualityGateConfigurationError(
            "approved run task brief is invalid"
        ) from error
    if (
        brief.run_id != expected_run_id
        or canonical_model_sha256(brief) != expected_sha256
    ):
        raise QualityGateConfigurationError(
            "approved run task brief does not match the frozen team plan"
        )
    return replace(
        configuration,
        input_mounts=tuple(
            replace(mount, source=source.resolve(strict=True))
            if mount.id == "approved_task_brief"
            else mount
            for mount in configuration.input_mounts
        ),
    )
