"""Safe ownership boundaries for OpenClaw's nested workspace mounts."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

SANDBOX_SKILL_MOUNT = Path(".openclaw/sandbox-skills/skills")
_SANDBOX_SKILL_PARTS = (".openclaw", "sandbox-skills", "skills")
_CONTAINER_WORKSPACE = PurePosixPath("/sat-workspace")
_IMAGE_ID = re.compile(r"^sha256:[a-f0-9]{64}$")


class WorkspaceMountError(RuntimeError):
    """Raised when a sandbox mountpoint cannot be prepared or repaired safely."""


@dataclass(frozen=True)
class LegacyWorkspaceMountRepair:
    """One exact legacy mountpoint tree repaired during uninstall."""

    workspace: Path
    removed_paths: tuple[Path, ...]


ProcessRunner = Callable[..., subprocess.CompletedProcess[str]]


def _lstat_directory(path: Path, *, label: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise WorkspaceMountError(f"{label} cannot be inspected: {path}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise WorkspaceMountError(f"{label} must be a real directory: {path}")
    return metadata


def prepare_sandbox_skill_mountpoint(workspace: Path) -> Path:
    """Create OpenClaw's nested bind target under the invoking user's authority.

    Docker otherwise creates a missing nested bind destination as root inside the
    already bind-mounted workspace.  The resulting empty directory cannot be
    removed later by the ordinary user who owns the SAT installation.
    """

    if not workspace.is_absolute():
        raise WorkspaceMountError("sandbox workspace must be absolute")
    resolved = workspace.resolve(strict=True)
    if resolved != workspace or workspace.is_symlink():
        raise WorkspaceMountError("sandbox workspace must be a canonical real path")
    workspace_metadata = _lstat_directory(workspace, label="sandbox workspace")
    effective_uid = os.geteuid()
    if workspace_metadata.st_uid != effective_uid:
        raise WorkspaceMountError("sandbox workspace must belong to the invoking user")

    current = workspace
    for part in _SANDBOX_SKILL_PARTS:
        current /= part
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            pass
        except OSError as error:
            raise WorkspaceMountError(
                f"sandbox skill mountpoint cannot be created: {current}"
            ) from error
        metadata = _lstat_directory(current, label="sandbox skill mountpoint")
        if metadata.st_uid != effective_uid:
            raise WorkspaceMountError(
                f"sandbox skill mountpoint must belong to the invoking user: {current}"
            )
        if not metadata.st_mode & stat.S_IWUSR or not metadata.st_mode & stat.S_IXUSR:
            raise WorkspaceMountError(
                "sandbox skill mountpoint must be writable and searchable by "
                "its owner: "
                f"{current}"
            )
    return workspace / SANDBOX_SKILL_MOUNT


@dataclass(frozen=True)
class _LegacyRepairCandidate:
    workspace: Path
    paths: tuple[Path, ...]


def _directory_entries(path: Path) -> frozenset[str]:
    try:
        return frozenset(entry.name for entry in os.scandir(path))
    except OSError as error:
        raise WorkspaceMountError(
            f"legacy sandbox mountpoint cannot be inspected: {path}"
        ) from error


def _legacy_repair_candidates(
    workspaces_root: Path,
) -> tuple[_LegacyRepairCandidate, ...]:
    if not workspaces_root.is_absolute():
        raise WorkspaceMountError("workspace state root must be absolute")
    if not os.path.lexists(workspaces_root):
        return ()
    resolved_root = workspaces_root.resolve(strict=True)
    if resolved_root != workspaces_root or workspaces_root.is_symlink():
        raise WorkspaceMountError("workspace state root must be a canonical real path")
    _lstat_directory(workspaces_root, label="workspace state root")

    effective_uid = os.geteuid()
    candidates: list[_LegacyRepairCandidate] = []
    try:
        workspaces = sorted(workspaces_root.iterdir(), key=lambda item: item.name)
    except OSError as error:
        raise WorkspaceMountError("workspace state root cannot be listed") from error
    for workspace in workspaces:
        try:
            workspace_metadata = workspace.lstat()
        except OSError as error:
            raise WorkspaceMountError(
                f"workspace state entry cannot be inspected: {workspace}"
            ) from error
        if stat.S_ISLNK(workspace_metadata.st_mode) or not stat.S_ISDIR(
            workspace_metadata.st_mode
        ):
            continue
        if "," in str(workspace) or any(
            ord(character) < 32 for character in str(workspace)
        ):
            raise WorkspaceMountError(
                f"workspace path is unsafe for a Docker bind mount: {workspace}"
            )

        chain = tuple(
            workspace.joinpath(*_SANDBOX_SKILL_PARTS[: index + 1])
            for index in range(len(_SANDBOX_SKILL_PARTS))
        )
        if not os.path.lexists(chain[-1]):
            continue
        metadata: list[os.stat_result] = []
        unsafe_chain = False
        for path in chain:
            try:
                item = path.lstat()
            except FileNotFoundError:
                unsafe_chain = True
                break
            except OSError as error:
                raise WorkspaceMountError(
                    f"legacy sandbox mountpoint cannot be inspected: {path}"
                ) from error
            if stat.S_ISLNK(item.st_mode) or not stat.S_ISDIR(item.st_mode):
                unsafe_chain = True
                break
            metadata.append(item)
        if unsafe_chain:
            continue
        if all(item.st_uid == effective_uid for item in metadata):
            continue

        openclaw_entries = _directory_entries(chain[0])
        sandbox_entries = _directory_entries(chain[1])
        skill_entries = _directory_entries(chain[2])
        if skill_entries:
            raise WorkspaceMountError(
                "refusing to repair a non-empty legacy sandbox skill mountpoint: "
                f"{chain[2]}"
            )

        highest_foreign = min(
            index for index, item in enumerate(metadata) if item.st_uid != effective_uid
        )
        if highest_foreign <= 1 and sandbox_entries != {"skills"}:
            raise WorkspaceMountError(
                "refusing to repair a legacy sandbox directory with unexpected "
                "content: "
                f"{chain[1]}"
            )
        if highest_foreign == 0 and openclaw_entries != {"sandbox-skills"}:
            raise WorkspaceMountError(
                "refusing to repair a legacy OpenClaw directory with unexpected "
                "content: "
                f"{chain[0]}"
            )
        candidates.append(
            _LegacyRepairCandidate(
                workspace=workspace,
                paths=tuple(reversed(chain[highest_foreign:])),
            )
        )
    return tuple(candidates)


def _run_command(
    argv: list[str],
    *,
    runner: ProcessRunner,
    timeout_seconds: int,
) -> subprocess.CompletedProcess[str]:
    try:
        return runner(
            argv,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            shell=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise WorkspaceMountError(
            f"legacy workspace repair command could not run: {argv[0]}"
        ) from error


def _sandbox_image(policy_path: Path) -> str:
    try:
        payload = json.loads(policy_path.read_text(encoding="utf-8"))
        sandbox = payload["sandbox"]
        image = sandbox["image"]
        backend = sandbox["backend"]
        pull = sandbox["pull"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise WorkspaceMountError(
            "sandbox policy cannot be read for uninstall"
        ) from error
    if (
        not isinstance(sandbox, dict)
        or backend != "docker"
        or pull != "never"
        or not isinstance(image, str)
        or not image.strip()
    ):
        raise WorkspaceMountError(
            "sandbox policy is unsafe for legacy workspace repair"
        )
    return image


def repair_legacy_sandbox_skill_mountpoints(
    *,
    workspaces_root: Path,
    policy_path: Path,
    sandbox_binary: str = "docker",
    timeout_seconds: int = 30,
    runner: ProcessRunner = subprocess.run,
) -> tuple[LegacyWorkspaceMountRepair, ...]:
    """Remove only empty root-owned mount targets left by earlier SAT releases."""

    if not sandbox_binary.strip():
        raise WorkspaceMountError("sandbox binary must not be blank")
    if timeout_seconds < 1:
        raise WorkspaceMountError("legacy workspace repair timeout must be positive")
    candidates = _legacy_repair_candidates(workspaces_root)
    if not candidates:
        return ()

    image = _sandbox_image(policy_path)
    inspected = _run_command(
        [sandbox_binary, "image", "inspect", "--format", "{{.Id}}", image],
        runner=runner,
        timeout_seconds=timeout_seconds,
    )
    image_id = inspected.stdout.strip()
    if inspected.returncode != 0 or _IMAGE_ID.fullmatch(image_id) is None:
        raise WorkspaceMountError(
            "the configured local sandbox image is unavailable for legacy "
            "workspace repair"
        )

    repaired: list[LegacyWorkspaceMountRepair] = []
    for candidate in candidates:
        container_paths = tuple(
            _CONTAINER_WORKSPACE / path.relative_to(candidate.workspace)
            for path in candidate.paths
        )
        completed = _run_command(
            [
                sandbox_binary,
                "run",
                "--rm",
                "--pull",
                "never",
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--cap-add",
                "DAC_OVERRIDE",
                "--security-opt",
                "no-new-privileges",
                "--user",
                "0:0",
                "--memory",
                "64m",
                "--cpus",
                "0.25",
                "--pids-limit",
                "32",
                "--label",
                "software-agent-team.cleanup=legacy-workspace-mount-v1",
                "--mount",
                f"type=bind,source={candidate.workspace},target={_CONTAINER_WORKSPACE}",
                "--entrypoint",
                "/usr/bin/rmdir",
                image_id,
                "--",
                *(str(path) for path in container_paths),
            ],
            runner=runner,
            timeout_seconds=timeout_seconds,
        )
        if completed.returncode != 0:
            raise WorkspaceMountError(
                "the constrained sandbox could not remove an empty legacy mountpoint "
                f"from {candidate.workspace}"
            )
        if any(os.path.lexists(path) for path in candidate.paths):
            raise WorkspaceMountError(
                "legacy workspace repair returned success without removing its "
                "exact targets"
            )
        repaired.append(
            LegacyWorkspaceMountRepair(
                workspace=candidate.workspace,
                removed_paths=candidate.paths,
            )
        )
    return tuple(repaired)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--workspaces-root", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--sandbox-binary", default="docker")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the private uninstall compatibility boundary."""

    try:
        arguments = _parser().parse_args(argv)
        repaired = repair_legacy_sandbox_skill_mountpoints(
            workspaces_root=arguments.workspaces_root,
            policy_path=arguments.policy,
            sandbox_binary=arguments.sandbox_binary,
        )
    except (WorkspaceMountError, SystemExit) as error:
        if isinstance(error, SystemExit) and error.code == 0:
            return 0
        print(f"workspace cleanup: {error}", file=sys.stderr)
        return 1
    if repaired:
        print(
            "workspace cleanup: repaired "
            f"{len(repaired)} legacy sandbox mountpoint tree(s)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
