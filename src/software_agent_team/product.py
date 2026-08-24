"""Primary product-flow diagnostics, request materialization, and delivery."""

from __future__ import annotations

import ctypes
import errno
import json
import os
import platform
import re
import shutil
import subprocess
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from software_agent_team.artifacts import AcceptanceCriterion, TaskBrief
from software_agent_team.benchmark_seed import prepare_seed_repository

MINIMUM_FREE_BYTES = 1_073_741_824
PROJECT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SUPPORTED_ARCHITECTURES = {"aarch64", "amd64", "arm64", "x86_64"}
STATE_MARKER_NAME = ".sat-state-v1"
AT_FDCWD = -100
RENAME_NOREPLACE = 1
PROJECT_MANIFEST_NAME = "sat-project.json"
_MAX_PROJECT_MANIFEST_BYTES = 65_536
_MAX_REQUEST_ITEMS = 10


class ProductFlowError(RuntimeError):
    """Raised when the guided product flow cannot continue safely."""


class DiagnosticState(StrEnum):
    """User-facing readiness state for one startup condition."""

    READY = "ready"
    WARNING = "warning"
    ACTION_REQUIRED = "action_required"


@dataclass(frozen=True)
class DiagnosticCheck:
    """One startup condition and its concrete corrective action."""

    id: str
    label: str
    state: DiagnosticState
    detail: str
    action: str | None = None


@dataclass(frozen=True)
class StartupDiagnostics:
    """Ordered local startup checks for the primary product flow."""

    checks: tuple[DiagnosticCheck, ...]

    @property
    def ready(self) -> bool:
        return all(
            check.state is not DiagnosticState.ACTION_REQUIRED for check in self.checks
        )


class ProjectCommands(BaseModel):
    """Validated non-shell commands delivered with one generated project."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    setup: tuple[str, ...] = Field(min_length=3, max_length=32)
    start: tuple[str, ...] = Field(min_length=3, max_length=32)
    test: tuple[str, ...] = Field(min_length=3, max_length=32)

    @field_validator("setup", "start", "test")
    @classmethod
    def require_safe_argv(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Reject blank, multiline, oversized, or shell-based commands."""

        if any(
            not value
            or len(value) > 1024
            or any(character in value for character in ("\x00", "\r", "\n"))
            for value in values
        ):
            raise ValueError("project commands require non-empty single-line argv")
        if sum(len(value) for value in values) > 4096:
            raise ValueError("project command argv is too large")
        if values[0] != "uv" or (
            values[1] != "run"
            and values
            != (
                "uv",
                "sync",
                "--dev",
            )
        ):
            raise ValueError("project commands must use the versioned uv environment")
        if values[2] in {
            "bash",
            "cmd",
            "cmd.exe",
            "dash",
            "fish",
            "ksh",
            "powershell",
            "pwsh",
            "sh",
            "zsh",
        }:
            raise ValueError("project commands cannot invoke a command shell")
        return values

    @model_validator(mode="after")
    def require_profile_contract(self) -> Self:
        """Keep setup and test reproducible while allowing a project-specific start."""

        if self.setup != ("uv", "sync", "--dev"):
            raise ValueError("setup command must be: uv sync --dev")
        if self.start[:2] != ("uv", "run"):
            raise ValueError("start command must begin with: uv run")
        if "replace-with-project-entrypoint" in self.start:
            raise ValueError("start command still contains the starter placeholder")
        if self.test != ("uv", "run", "pytest"):
            raise ValueError("test command must be: uv run pytest")
        return self


@dataclass(frozen=True)
class ProductStatePaths:
    """Run-scoped roots beneath the user-local SAT state directory."""

    root: Path
    runs: Path
    workspaces: Path
    sources: Path

    @classmethod
    def below(cls, root: Path) -> ProductStatePaths:
        return cls(
            root=root,
            runs=root / "runs",
            workspaces=root / "workspaces",
            sources=root / "sources",
        )


def ensure_product_state(paths: ProductStatePaths) -> None:
    """Create private real directories for product-owned run state."""

    root_existed = paths.root.exists() or paths.root.is_symlink()
    paths.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if paths.root.is_symlink() or not paths.root.is_dir():
        raise ProductFlowError(f"SAT state root must be a real directory: {paths.root}")
    resolved_root = paths.root.resolve(strict=True)
    marker = paths.root / STATE_MARKER_NAME
    expected_marker = f"software-agent-team-state-v1\nroot={resolved_root}\n".encode()
    if marker.exists() or marker.is_symlink():
        if marker.is_symlink() or not marker.is_file():
            raise ProductFlowError("SAT state ownership marker must be a regular file")
        if marker.read_bytes() != expected_marker:
            raise ProductFlowError("SAT state ownership marker is invalid")
    else:
        if root_existed:
            raise ProductFlowError(
                f"existing state root is not owned by SAT: {paths.root}"
            )
        descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(expected_marker)
            output.flush()
            os.fsync(output.fileno())
        marker.chmod(0o600)

    paths.root.chmod(0o700)
    for path in (paths.runs, paths.workspaces, paths.sources):
        path.mkdir(exist_ok=True, mode=0o700)
        if path.is_symlink() or not path.is_dir():
            raise ProductFlowError(f"SAT state path must be a real directory: {path}")
        path.chmod(0o700)
        try:
            path.resolve(strict=True).relative_to(resolved_root)
        except ValueError as error:
            raise ProductFlowError(
                f"SAT state path escapes its root: {path}"
            ) from error


CommandRunner = Callable[[Sequence[str], float], subprocess.CompletedProcess[str]]


def _run_command(
    argv: Sequence[str], timeout_seconds: float
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        check=False,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=timeout_seconds,
        shell=False,
    )


def inspect_startup_environment(
    *,
    working_directory: Path,
    openclaw_binary: Path,
    sandbox_image: str,
    sandbox_binary: str = "docker",
    command_finder: Callable[[str], str | None] = shutil.which,
    command_runner: CommandRunner = _run_command,
    environment: Mapping[str, str] | None = None,
) -> StartupDiagnostics:
    """Inspect local prerequisites without changing them or calling a provider."""

    values = os.environ if environment is None else environment
    checks: list[DiagnosticCheck] = []
    system = platform.system()
    checks.append(
        DiagnosticCheck(
            id="platform",
            label="Linux or WSL",
            state=(
                DiagnosticState.READY
                if system == "Linux"
                else DiagnosticState.ACTION_REQUIRED
            ),
            detail=f"detected {system or 'unknown'}",
            action=(
                None
                if system == "Linux"
                else "Run SAT on Linux or inside a WSL distribution."
            ),
        )
    )
    architecture = platform.machine().casefold()
    checks.append(
        DiagnosticCheck(
            id="architecture",
            label="Supported architecture",
            state=(
                DiagnosticState.READY
                if architecture in SUPPORTED_ARCHITECTURES
                else DiagnosticState.ACTION_REQUIRED
            ),
            detail=f"detected {architecture or 'unknown'}",
            action=(
                None
                if architecture in SUPPORTED_ARCHITECTURES
                else "Use an x86-64 or ARM64 Linux environment."
            ),
        )
    )
    uid = os.getuid()
    gid = os.getgid()
    unprivileged = uid != 0 and gid != 0
    checks.append(
        DiagnosticCheck(
            id="identity",
            label="Unprivileged user",
            state=(
                DiagnosticState.READY
                if unprivileged
                else DiagnosticState.ACTION_REQUIRED
            ),
            detail=f"uid={uid} gid={gid}",
            action=(None if unprivileged else "Run SAT as a normal user, not root."),
        )
    )

    try:
        resolved_working_directory = working_directory.resolve(strict=True)
        directory_ready = (
            resolved_working_directory.is_dir()
            and not working_directory.is_symlink()
            and os.access(resolved_working_directory, os.W_OK | os.X_OK)
        )
    except OSError:
        resolved_working_directory = working_directory
        directory_ready = False
    checks.append(
        DiagnosticCheck(
            id="working_directory",
            label="Writable project parent",
            state=(
                DiagnosticState.READY
                if directory_ready
                else DiagnosticState.ACTION_REQUIRED
            ),
            detail=str(resolved_working_directory),
            action=(
                None
                if directory_ready
                else "Enter a writable real directory and run sat again."
            ),
        )
    )

    for command in ("git", sandbox_binary):
        found = command_finder(command)
        checks.append(
            DiagnosticCheck(
                id=f"command_{command}",
                label=f"{command} command",
                state=(
                    DiagnosticState.READY
                    if found is not None
                    else DiagnosticState.ACTION_REQUIRED
                ),
                detail=found or "not found",
                action=(
                    None
                    if found is not None
                    else f"Install {command} and run sat again."
                ),
            )
        )

    openclaw_ready = openclaw_binary.is_file() and os.access(openclaw_binary, os.X_OK)
    checks.append(
        DiagnosticCheck(
            id="openclaw",
            label="OpenClaw runtime",
            state=(
                DiagnosticState.READY
                if openclaw_ready
                else DiagnosticState.ACTION_REQUIRED
            ),
            detail=str(openclaw_binary),
            action=(
                None
                if openclaw_ready
                else "Re-run the SAT installer to restore the pinned runtime."
            ),
        )
    )

    docker_found = command_finder(sandbox_binary) is not None
    docker_ready = False
    image_ready = False
    if docker_found:
        try:
            docker_ready = command_runner((sandbox_binary, "info"), 15).returncode == 0
        except (OSError, subprocess.SubprocessError):
            docker_ready = False
        if docker_ready:
            try:
                image_ready = (
                    command_runner(
                        (sandbox_binary, "image", "inspect", sandbox_image),
                        15,
                    ).returncode
                    == 0
                )
            except (OSError, subprocess.SubprocessError):
                image_ready = False
    checks.append(
        DiagnosticCheck(
            id="docker_daemon",
            label="Docker daemon",
            state=(
                DiagnosticState.READY
                if docker_ready
                else DiagnosticState.ACTION_REQUIRED
            ),
            detail=(
                "available to the current user"
                if docker_ready
                else "unavailable to the current user"
            ),
            action=(
                None
                if docker_ready
                else "Start Docker in Linux-container mode and grant this user access."
            ),
        )
    )
    checks.append(
        DiagnosticCheck(
            id="sandbox_image",
            label="Pinned sandbox image",
            state=(
                DiagnosticState.READY
                if image_ready
                else DiagnosticState.ACTION_REQUIRED
            ),
            detail=sandbox_image,
            action=(
                None
                if image_ready
                else "Re-run the SAT installer to build the pinned sandbox image."
            ),
        )
    )

    try:
        free_bytes = shutil.disk_usage(resolved_working_directory).free
    except OSError:
        free_bytes = 0
    enough_storage = free_bytes >= MINIMUM_FREE_BYTES
    checks.append(
        DiagnosticCheck(
            id="storage",
            label="Available storage",
            state=(
                DiagnosticState.READY if enough_storage else DiagnosticState.WARNING
            ),
            detail=f"{free_bytes // (1024 * 1024)} MiB free",
            action=(
                None if enough_storage else "Free at least 1 GiB before a long build."
            ),
        )
    )

    path_entries = values.get("PATH", "").split(os.pathsep)
    launcher_visible = any(
        entry and (Path(entry).expanduser() / "sat").exists() for entry in path_entries
    )
    checks.append(
        DiagnosticCheck(
            id="launcher",
            label="sat launcher",
            state=(
                DiagnosticState.READY if launcher_visible else DiagnosticState.WARNING
            ),
            detail="available on PATH" if launcher_visible else "current process only",
            action=(
                None
                if launcher_visible
                else "Add the installer-reported SAT bin directory to PATH."
            ),
        )
    )
    return StartupDiagnostics(checks=tuple(checks))


def render_startup_diagnostics(diagnostics: StartupDiagnostics) -> None:
    """Print one concise startup report with actionable failures."""

    print("Checking this device...")
    for check in diagnostics.checks:
        symbol = {
            DiagnosticState.READY: "✓",
            DiagnosticState.WARNING: "!",
            DiagnosticState.ACTION_REQUIRED: "✗",
        }[check.state]
        print(f"{symbol} {check.label}: {check.detail}")
        if check.action is not None:
            print(f"  Action: {check.action}")


def generate_product_run_id(
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    random_suffix: Callable[[], str] = lambda: uuid.uuid4().hex[:8],
) -> str:
    """Generate a sortable collision-resistant internal run identifier."""

    now = clock().astimezone(UTC)
    suffix = random_suffix().casefold()
    if re.fullmatch(r"[0-9a-f]{8}", suffix) is None:
        raise ProductFlowError("run ID suffix must contain eight hexadecimal digits")
    return f"sat-{now:%Y%m%d-%H%M%S}-{suffix}"


def validate_project_destination(parent: Path, project_name: str) -> Path:
    """Resolve one new direct child without accepting an overwrite target."""

    cleaned = project_name.strip()
    if PROJECT_NAME_PATTERN.fullmatch(cleaned) is None or cleaned in {".", ".."}:
        raise ProductFlowError(
            "project directory must use 1-64 letters, numbers, dots, dashes, or "
            "underscores and cannot contain a path separator"
        )
    try:
        resolved_parent = parent.resolve(strict=True)
    except OSError as error:
        raise ProductFlowError("project parent directory does not exist") from error
    if not resolved_parent.is_dir() or parent.is_symlink():
        raise ProductFlowError("project parent must be a real directory")
    destination = resolved_parent / cleaned
    if destination.exists() or destination.is_symlink():
        raise ProductFlowError(
            f"project destination already exists and will not be overwritten: "
            f"{destination}"
        )
    return destination


def build_product_task_brief(
    *,
    run_id: str,
    project_name: str,
    source_request: str,
    success_conditions: Sequence[str] = (),
    user_constraints: Sequence[str] = (),
) -> TaskBrief:
    """Build a confirmed brief directly from the user's product request."""

    request = " ".join(source_request.split())
    if not request:
        raise ProductFlowError("the software request must not be blank")
    if len(request) > 2000:
        raise ProductFlowError("the software request must be at most 2000 characters")

    def clean_items(values: Sequence[str], *, label: str) -> tuple[str, ...]:
        cleaned = tuple(" ".join(value.split()) for value in values if value.strip())
        if len(cleaned) > _MAX_REQUEST_ITEMS:
            raise ProductFlowError(
                f"{label} must contain at most {_MAX_REQUEST_ITEMS} items"
            )
        if any(len(value) > 500 for value in cleaned):
            raise ProductFlowError(f"each {label} item must be at most 500 characters")
        if len(cleaned) != len(set(cleaned)):
            raise ProductFlowError(f"{label} items must be unique")
        return cleaned

    conditions = clean_items(success_conditions, label="success condition")
    constraints = clean_items(user_constraints, label="constraint")
    confirmed_outcomes = conditions or (request,)
    outcome_text = "; ".join(confirmed_outcomes)
    requirements = [
        f"Implement the confirmed software request exactly as stated: {request}",
        *(f"Satisfy this confirmed success condition: {item}" for item in conditions),
        (
            "Provide automated pytest coverage for the implemented behavior and "
            "the project-specific entry point."
        ),
        "Provide a README with setup, start, test, and known-limitation guidance.",
        (
            "Provide sat-project.json with schema_version 1 and argv arrays named "
            "setup, start, and test. Use ['uv', 'sync', '--dev'] for setup, a "
            "project-specific non-shell ['uv', 'run', ...] command for start, and "
            "['uv', 'run', 'pytest'] for test."
        ),
    ]
    return TaskBrief(
        run_id=run_id,
        title=project_name.replace("-", " ").replace("_", " ").strip().title(),
        source_request=request,
        requirements=requirements,
        acceptance_criteria=[
            AcceptanceCriterion(
                id="AC_REQUEST",
                description=(
                    "The delivered project satisfies the confirmed user outcomes: "
                    f"{outcome_text}"
                ),
                verification=(
                    "Inspect the immutable implementation and its automated tests "
                    "during independent review."
                ),
            ),
            AcceptanceCriterion(
                id="AC_RUNNABLE",
                description=(
                    "The project supplies a validated, project-specific non-shell "
                    "setup, start, and test command contract."
                ),
                verification="Run CHECK_PROJECT_CONTRACT.",
            ),
            AcceptanceCriterion(
                id="AC_TESTS",
                description="The project's automated pytest suite passes.",
                verification="Run CHECK_PROJECT_TESTS.",
            ),
            AcceptanceCriterion(
                id="AC_QUALITY",
                description="The Python source compiles and passes the pinned linter.",
                verification="Run CHECK_COMPILE and CHECK_LINT.",
            ),
            AcceptanceCriterion(
                id="AC_DOCUMENTATION",
                description=(
                    "The README explains setup, start, tests, and known limitations."
                ),
                verification="Run CHECK_PROJECT_CONTRACT and independent review.",
            ),
        ],
        constraints=[
            "Use the supplied local Python 3.12 execution profile.",
            (
                "Do not require credentials, hosted services, an external database, "
                "or network access after dependency installation."
            ),
            (
                "Keep all source, tests, configuration, and documentation in the "
                "repository."
            ),
            *constraints,
        ],
        assumptions=[
            "This is a new greenfield project delivered for local execution.",
            "The supplied profile dependencies are available in the quality sandbox.",
        ],
        open_questions=[],
        confirmed=True,
    )


def prepare_product_source(
    *,
    seed: Path,
    state_paths: ProductStatePaths,
    run_id: str,
) -> Path:
    """Create the trusted greenfield source baseline for one confirmed run."""

    destination = state_paths.sources / run_id
    prepare_seed_repository(
        seed,
        destination,
        commit_message="chore: initialize software project",
    )
    return destination


def load_project_commands(project: Path) -> ProjectCommands:
    """Load the accepted project's bounded command contract."""

    manifest = project / PROJECT_MANIFEST_NAME
    if manifest.is_symlink() or not manifest.is_file():
        raise ProductFlowError(
            f"accepted project is missing a regular {PROJECT_MANIFEST_NAME}"
        )
    raw = manifest.read_bytes()
    if len(raw) > _MAX_PROJECT_MANIFEST_BYTES:
        raise ProductFlowError(f"{PROJECT_MANIFEST_NAME} is too large")

    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        payload = json.loads(raw, object_pairs_hook=reject_duplicate_keys)
        return ProjectCommands.model_validate(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ProductFlowError(
            f"accepted project has an invalid {PROJECT_MANIFEST_NAME}: {error}"
        ) from error


def _git_output(repository: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-C",
                str(repository),
                *arguments,
            ],
            check=False,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=30,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ProductFlowError("could not verify the delivered Git result") from error
    if result.returncode != 0:
        raise ProductFlowError("could not verify the delivered Git result")
    return result.stdout.strip()


def _clone_git_result(source: Path, destination: Path) -> None:
    try:
        result = subprocess.run(
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "clone",
                "--no-local",
                "--no-checkout",
                "--",
                str(source),
                str(destination),
            ],
            check=False,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=30,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ProductFlowError("could not clone the accepted Git result") from error
    if result.returncode != 0:
        raise ProductFlowError("could not clone the accepted Git result")


def _rename_no_replace(source: Path, destination: Path) -> None:
    """Publish one directory atomically without replacing a late destination."""

    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise ProductFlowError(
            "this Linux runtime cannot publish the result without overwrite risk"
        )
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    result = renameat2(
        AT_FDCWD,
        os.fsencode(source),
        AT_FDCWD,
        os.fsencode(destination),
        RENAME_NOREPLACE,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise ProductFlowError(
            f"delivery destination appeared during the run: {destination}"
        )
    if error_number in {errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP}:
        raise ProductFlowError(
            "the destination filesystem cannot publish the result atomically "
            "without overwrite risk"
        )
    raise ProductFlowError(
        f"could not publish the accepted result: {os.strerror(error_number)}"
    )


def deliver_product_workspace(
    source: Path,
    destination: Path,
    *,
    expected_commit: str,
) -> Path:
    """Copy one accepted clean workspace into a new project directory atomically."""

    try:
        resolved_source = source.resolve(strict=True)
        resolved_parent = destination.parent.resolve(strict=True)
    except OSError as error:
        raise ProductFlowError(
            "delivery source or destination parent is unavailable"
        ) from error
    if not resolved_source.is_dir() or source.is_symlink():
        raise ProductFlowError("accepted workspace must be a real directory")
    if not resolved_parent.is_dir() or destination.parent.is_symlink():
        raise ProductFlowError("delivery parent must be a real directory")
    if destination.exists() or destination.is_symlink():
        raise ProductFlowError(
            f"delivery destination appeared during the run: {destination}"
        )
    if resolved_parent == resolved_source or resolved_parent.is_relative_to(
        resolved_source
    ):
        raise ProductFlowError("delivery destination cannot be inside the workspace")
    if (
        _git_output(resolved_source, "rev-parse", f"{expected_commit}^{{commit}}")
        != expected_commit
    ):
        raise ProductFlowError("accepted commit is unavailable before delivery")
    if _git_output(resolved_source, "remote"):
        raise ProductFlowError("accepted workspace unexpectedly contains a remote")
    tree = _git_output(
        resolved_source,
        "ls-tree",
        "-r",
        "--full-tree",
        expected_commit,
    )
    if any(line.startswith("120000 ") for line in tree.splitlines()):
        raise ProductFlowError("accepted commit contains a symbolic link")

    staging = resolved_parent / f".{destination.name}.sat-{uuid.uuid4().hex}.tmp"
    try:
        _clone_git_result(resolved_source, staging)
        _git_output(staging, "remote", "remove", "origin")
        _git_output(staging, "switch", "-C", "main", expected_commit)
        if _git_output(staging, "rev-parse", "HEAD") != expected_commit:
            raise ProductFlowError("staged delivery changed the accepted commit")
        if _git_output(staging, "status", "--porcelain=v1", "--untracked-files=all"):
            raise ProductFlowError("staged delivery is not clean")
        _rename_no_replace(staging, destination)
        descriptor = os.open(resolved_parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return destination
