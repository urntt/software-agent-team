"""Safe isolated Git clone preparation and immutable snapshot verification."""

from __future__ import annotations

import os
import re
import stat
import subprocess
from collections.abc import Callable, Collection
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from software_agent_team.artifacts import WorkResult

COMMIT_PATTERN = r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"
RUN_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
ATTRIBUTE_FILTER_PATTERN = re.compile(r"(?:^|\s)[-!]?filter(?:=|\s|$)")
UNSAFE_CONFIG_PATTERN = (
    r"^(core\.hookspath|core\.fsmonitor|"
    r"filter\..*\.(clean|smudge|process))$"
)


class GitWorkspaceError(ValueError):
    """Base error for isolated Git workspace operations."""


class GitCommandError(GitWorkspaceError):
    """Raised when a bounded Git subprocess returns an unexpected result."""


class RepositoryValidationError(GitWorkspaceError):
    """Raised when a source repository cannot provide a controlled snapshot."""


class UnsafeRepositoryError(RepositoryValidationError):
    """Raised when checkout could execute repository-controlled programs."""


class WorkspaceAlreadyExistsError(GitWorkspaceError):
    """Raised when the derived workspace path is already occupied."""


class WorkspaceIntegrityError(GitWorkspaceError):
    """Raised when a workspace or snapshot disagrees with verified Git state."""


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("workspace timestamps must include a timezone")
    return value.astimezone(UTC)


def _require_safe_repository_path(value: str) -> str:
    if "\\" in value:
        raise ValueError("repository paths must use POSIX separators")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path == PurePosixPath("."):
        raise ValueError("repository paths must be safe relative paths")
    return value


class GitWorkspace(BaseModel):
    """Immutable identity of one detached, self-contained run clone."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    source_repository: str = Field(min_length=1)
    workspace_path: str = Field(min_length=1)
    base_commit: str = Field(pattern=COMMIT_PATTERN)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        """Normalize workspace timestamps to UTC."""

        return _require_utc(value)

    @field_validator("source_repository", "workspace_path")
    @classmethod
    def require_absolute_path(cls, value: str) -> str:
        """Persist explicit machine-local paths for recovery."""

        if not Path(value).is_absolute():
            raise ValueError("workspace paths must be absolute")
        return value

    @model_validator(mode="after")
    def validate_distinct_paths(self) -> Self:
        """Never treat the source checkout as its own isolated workspace."""

        if self.source_repository == self.workspace_path:
            raise ValueError("source repository and workspace paths must differ")
        return self


class GitSnapshot(BaseModel):
    """Controller-verified immutable commit boundary for one iteration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    iteration: int = Field(ge=1)
    input_commit: str = Field(pattern=COMMIT_PATTERN)
    output_commit: str = Field(pattern=COMMIT_PATTERN)
    commit_count: int = Field(ge=1)
    changed_files: tuple[str, ...] = Field(min_length=1)
    recorded_at: datetime

    @field_validator("recorded_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        """Normalize snapshot timestamps to UTC."""

        return _require_utc(value)

    @field_validator("changed_files")
    @classmethod
    def require_safe_changed_files(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Keep verified paths unique, relative, and portable."""

        if len(values) != len(set(values)):
            raise ValueError("snapshot changed files must be unique")
        if any(not value for value in values):
            raise ValueError("snapshot changed files must not be blank")
        return tuple(_require_safe_repository_path(value) for value in values)

    @model_validator(mode="after")
    def validate_commits(self) -> Self:
        """Require a new output commit."""

        if self.input_commit == self.output_commit:
            raise ValueError("snapshot output commit must differ from input")
        return self


def validate_work_result_snapshot(
    work_result: WorkResult,
    snapshot: GitSnapshot,
) -> None:
    """Require an Agent work result to match controller-verified Git evidence."""

    if work_result.run_id != snapshot.run_id:
        raise WorkspaceIntegrityError("work result and snapshot run IDs differ")
    if work_result.iteration != snapshot.iteration:
        raise WorkspaceIntegrityError("work result and snapshot iterations differ")
    if (
        work_result.input_commit != snapshot.input_commit
        or work_result.output_commit != snapshot.output_commit
    ):
        raise WorkspaceIntegrityError("work result and snapshot commits differ")
    if set(work_result.changed_files) != set(snapshot.changed_files):
        raise WorkspaceIntegrityError("work result and snapshot changed files differ")


Clock = Callable[[], datetime]


def _system_clock() -> datetime:
    return datetime.now(UTC)


class GitWorkspaceManager:
    """Prepare detached standalone clones and verify Agent-created commits."""

    def __init__(
        self,
        root: Path,
        *,
        clock: Clock = _system_clock,
        git_binary: str = "git",
        timeout_seconds: int = 30,
    ) -> None:
        if timeout_seconds < 1:
            raise GitWorkspaceError("Git command timeout must be positive")
        self.root = root
        self.clock = clock
        self.git_binary = git_binary
        self.timeout_seconds = timeout_seconds

    def prepare(
        self,
        run_id: str,
        *,
        source_repository: Path,
        base_ref: str = "HEAD",
    ) -> GitWorkspace:
        """Create one detached standalone clone from a validated repository."""

        if not RUN_ID_PATTERN.fullmatch(run_id):
            raise GitWorkspaceError(f"invalid run ID: {run_id}")
        source, base_commit, author_name, author_email = (
            self._validated_source_repository(source_repository, base_ref=base_ref)
        )

        candidate_root = self.root.resolve(strict=False)
        if candidate_root == source or candidate_root.is_relative_to(source):
            raise GitWorkspaceError(
                "workspace root must be outside the source repository"
            )
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink() or not self.root.is_dir():
            raise GitWorkspaceError("workspace root must be a real directory")
        root = self.root.resolve(strict=True)
        destination = root / run_id
        if destination.exists() or destination.is_symlink():
            raise WorkspaceAlreadyExistsError(
                f"workspace path already exists: {destination}"
            )

        self._git(
            source,
            [
                "clone",
                "--no-local",
                "--no-checkout",
                "--",
                str(source),
                str(destination),
            ],
        )
        self._git(destination, ["remote", "remove", "origin"])
        self._git(destination, ["config", "--local", "user.name", author_name])
        self._git(destination, ["config", "--local", "user.email", author_email])
        self._git(destination, ["checkout", "--detach", base_commit])
        workspace = GitWorkspace(
            run_id=run_id,
            source_repository=str(source),
            workspace_path=str(destination),
            base_commit=base_commit,
            created_at=_require_utc(self.clock()),
        )
        self.verify_workspace(
            workspace, expected_commit=base_commit, require_clean=True
        )
        return workspace

    def validate_source_repository(
        self,
        source_repository: Path,
        *,
        base_ref: str = "HEAD",
    ) -> str:
        """Validate every source precondition without creating a workspace."""

        _, base_commit, _, _ = self._validated_source_repository(
            source_repository,
            base_ref=base_ref,
        )
        return base_commit

    def verify_workspace(
        self,
        workspace: GitWorkspace,
        *,
        expected_commit: str | None = None,
        require_clean: bool = False,
    ) -> str:
        """Verify standalone clone identity, state, commit, and cleanliness."""

        try:
            source = self._validate_source(Path(workspace.source_repository))
        except GitWorkspaceError as error:
            raise WorkspaceIntegrityError("source repository is missing") from error
        run_workspace = Path(workspace.workspace_path)
        if not run_workspace.is_dir() or run_workspace.is_symlink():
            raise WorkspaceIntegrityError("isolated workspace is missing")

        top_level = Path(
            self._git_text(run_workspace, ["rev-parse", "--show-toplevel"])
        ).resolve(strict=True)
        resolved_workspace = run_workspace.resolve(strict=True)
        if top_level != resolved_workspace:
            raise WorkspaceIntegrityError("workspace top level does not match metadata")

        git_directory = run_workspace / ".git"
        if not git_directory.is_dir() or git_directory.is_symlink():
            raise WorkspaceIntegrityError(
                "workspace Git metadata must be a self-contained directory"
            )
        resolved_git_directory = git_directory.resolve(strict=True)
        reported_git_directory = self._resolved_git_path(
            run_workspace,
            self._git_text(run_workspace, ["rev-parse", "--absolute-git-dir"]),
        )
        common_directory = self._resolved_git_path(
            run_workspace,
            self._git_text(run_workspace, ["rev-parse", "--git-common-dir"]),
        )
        if (
            reported_git_directory != resolved_git_directory
            or common_directory != resolved_git_directory
        ):
            raise WorkspaceIntegrityError(
                "workspace Git metadata is not self-contained"
            )
        if (git_directory / "objects" / "info" / "alternates").exists():
            raise WorkspaceIntegrityError(
                "workspace object database cannot use an external alternate"
            )
        if self._git_text(run_workspace, ["remote"]):
            raise WorkspaceIntegrityError("isolated workspace cannot retain a remote")
        try:
            source_base = self._resolve_commit(source, workspace.base_commit)
        except GitWorkspaceError as error:
            raise WorkspaceIntegrityError(
                "workspace base commit is absent from the recorded source"
            ) from error
        if source_base != workspace.base_commit:
            raise WorkspaceIntegrityError(
                "workspace belongs to a different source history"
            )

        symbolic = self._git(
            run_workspace,
            ["symbolic-ref", "-q", "HEAD"],
            allowed_returncodes={0, 1},
        )
        if symbolic.returncode == 0:
            raise WorkspaceIntegrityError("run workspace must remain detached")

        head = self._resolve_commit(run_workspace, "HEAD")
        if expected_commit is not None and head != expected_commit:
            raise WorkspaceIntegrityError(
                f"workspace HEAD does not match expected commit {expected_commit}"
            )
        self._validate_checkout_safety(run_workspace, head)
        self._validate_working_tree_attributes(run_workspace)
        if require_clean and self._status(run_workspace):
            raise WorkspaceIntegrityError("workspace contains uncommitted changes")
        return head

    def recover_prepared(
        self,
        run_id: str,
        *,
        source_repository: Path,
        base_ref: str = "HEAD",
    ) -> GitWorkspace:
        """Explicitly recover a matching clone created before state attachment."""

        if not RUN_ID_PATTERN.fullmatch(run_id):
            raise GitWorkspaceError(f"invalid run ID: {run_id}")
        source, base_commit, _, _ = self._validated_source_repository(
            source_repository,
            base_ref=base_ref,
        )

        if self.root.is_symlink() or not self.root.is_dir():
            raise WorkspaceIntegrityError("workspace root is unavailable for recovery")
        root = self.root.resolve(strict=True)
        if root == source or root.is_relative_to(source):
            raise WorkspaceIntegrityError(
                "workspace root must be outside the source repository"
            )
        workspace = GitWorkspace(
            run_id=run_id,
            source_repository=str(source),
            workspace_path=str(root / run_id),
            base_commit=base_commit,
            created_at=_require_utc(self.clock()),
        )
        try:
            self.verify_workspace(
                workspace,
                expected_commit=base_commit,
                require_clean=True,
            )
        except GitWorkspaceError as error:
            raise WorkspaceIntegrityError(
                "existing workspace cannot be recovered safely"
            ) from error
        return workspace

    def _validated_source_repository(
        self,
        source_repository: Path,
        *,
        base_ref: str,
    ) -> tuple[Path, str, str, str]:
        """Return one source snapshot after all preparation checks pass."""

        source = self._validate_source(source_repository)
        if not base_ref:
            raise RepositoryValidationError("base ref must not be empty")
        base_commit = self._resolve_commit(source, base_ref)
        self._validate_checkout_safety(source, base_commit)
        self._validate_working_tree_attributes(source)
        if self._status(source):
            raise RepositoryValidationError("source repository must be clean")
        author_name, author_email = self._source_commit_identity(source)
        return source, base_commit, author_name, author_email

    def verify_snapshot(
        self,
        workspace: GitWorkspace,
        *,
        iteration: int,
        input_commit: str,
    ) -> GitSnapshot:
        """Verify and describe one clean descendant commit range."""

        if not 1 <= iteration <= 3:
            raise WorkspaceIntegrityError("snapshot iteration must be between 1 and 3")
        if not re.fullmatch(COMMIT_PATTERN, input_commit):
            raise WorkspaceIntegrityError("snapshot input commit is invalid")
        run_workspace = Path(workspace.workspace_path)
        output_commit = self.verify_workspace(workspace, require_clean=True)
        if output_commit == input_commit:
            raise WorkspaceIntegrityError("snapshot contains no new commit")
        try:
            resolved_input = self._resolve_commit(run_workspace, input_commit)
        except (GitCommandError, RepositoryValidationError) as error:
            raise WorkspaceIntegrityError(
                "snapshot input commit is not available in the workspace"
            ) from error
        if resolved_input != input_commit:
            raise WorkspaceIntegrityError("snapshot input commit is ambiguous")

        ancestor = self._git(
            run_workspace,
            ["merge-base", "--is-ancestor", input_commit, output_commit],
            allowed_returncodes={0, 1},
        )
        if ancestor.returncode != 0:
            raise WorkspaceIntegrityError(
                "snapshot output commit is not a descendant of its input"
            )
        commit_count = int(
            self._git_text(
                run_workspace,
                ["rev-list", "--count", f"{input_commit}..{output_commit}"],
            )
        )
        changed_output = self._git(
            run_workspace,
            ["diff", "--name-only", "-z", input_commit, output_commit, "--"],
        ).stdout
        try:
            changed_files = tuple(
                item.decode("utf-8", errors="strict")
                for item in changed_output.split(b"\0")
                if item
            )
        except UnicodeDecodeError as error:
            raise WorkspaceIntegrityError(
                "snapshot contains a non-UTF-8 repository path"
            ) from error
        if not changed_files:
            raise WorkspaceIntegrityError("snapshot contains no changed files")
        return GitSnapshot(
            run_id=workspace.run_id,
            iteration=iteration,
            input_commit=input_commit,
            output_commit=output_commit,
            commit_count=commit_count,
            changed_files=changed_files,
            recorded_at=_require_utc(self.clock()),
        )

    def _source_commit_identity(self, source: Path) -> tuple[str, str]:
        """Read the explicit local identity copied into the isolated clone."""

        try:
            name = self._git_text(source, ["config", "--local", "--get", "user.name"])
            email = self._git_text(
                source,
                ["config", "--local", "--get", "user.email"],
            )
        except GitCommandError as error:
            raise RepositoryValidationError(
                "source repository requires local user.name and user.email"
            ) from error
        if not name or not email:
            raise RepositoryValidationError(
                "source repository requires local user.name and user.email"
            )
        return name, email

    def _validate_source(self, source_repository: Path) -> Path:
        try:
            source = source_repository.resolve(strict=True)
        except OSError as error:
            raise RepositoryValidationError(
                "source repository does not exist"
            ) from error
        if not source.is_dir():
            raise RepositoryValidationError("source repository must be a directory")
        try:
            top_level = Path(
                self._git_text(source, ["rev-parse", "--show-toplevel"])
            ).resolve(strict=True)
        except GitCommandError as error:
            raise RepositoryValidationError(
                "source path is not a Git working tree"
            ) from error
        if top_level != source:
            raise RepositoryValidationError(
                "source path must be the repository top level"
            )
        if self._git_text(source, ["rev-parse", "--is-bare-repository"]) != "false":
            raise RepositoryValidationError("bare repositories are not supported")
        return source

    def _validate_checkout_safety(self, source: Path, commit: str) -> None:
        unsafe_config = self._git(
            source,
            [
                "config",
                "--includes",
                "--get-regexp",
                UNSAFE_CONFIG_PATTERN,
            ],
            allowed_returncodes={0, 1},
            controller_config=False,
        )
        if unsafe_config.returncode == 0 and unsafe_config.stdout.strip():
            raise UnsafeRepositoryError(
                "repository config contains hooks, filters, or fsmonitor"
            )

        hooks_path = (
            self._resolved_git_path(
                source,
                self._git_text(source, ["rev-parse", "--git-common-dir"]),
            )
            / "hooks"
        )
        if hooks_path.is_dir() and any(
            path.is_file()
            and not path.name.endswith(".sample")
            and os.access(path, os.X_OK)
            for path in hooks_path.iterdir()
        ):
            raise UnsafeRepositoryError("repository contains an executable Git hook")

        info_attributes = self._resolved_git_path(
            source,
            self._git_text(source, ["rev-parse", "--git-path", "info/attributes"]),
        )
        if info_attributes.is_file() and ATTRIBUTE_FILTER_PATTERN.search(
            info_attributes.read_text(encoding="utf-8", errors="replace")
        ):
            raise UnsafeRepositoryError("repository info attributes select a filter")

        tree = self._git(source, ["ls-tree", "-r", "-z", commit]).stdout
        entries = [entry for entry in tree.split(b"\0") if entry]
        if any(entry.startswith(b"160000 ") for entry in entries):
            raise UnsafeRepositoryError("repositories with submodules are unsupported")
        attribute_paths = []
        for entry in entries:
            _, separator, raw_path = entry.partition(b"\t")
            if not separator:
                raise RepositoryValidationError("cannot parse repository tree")
            path = raw_path.decode("utf-8", errors="strict")
            if PurePosixPath(path).name == ".gitattributes":
                attribute_paths.append(path)
        for path in attribute_paths:
            size = int(self._git_text(source, ["cat-file", "-s", f"{commit}:{path}"]))
            if size > 1024 * 1024:
                raise UnsafeRepositoryError(
                    "tracked attributes exceed the safety size limit"
                )
            content = self._git(source, ["show", f"{commit}:{path}"]).stdout.decode(
                "utf-8", errors="replace"
            )
            if ATTRIBUTE_FILTER_PATTERN.search(content):
                raise UnsafeRepositoryError(
                    "tracked attributes select a checkout filter"
                )

    def _validate_working_tree_attributes(self, repository: Path) -> None:
        for root, directories, filenames in os.walk(repository, followlinks=False):
            directories[:] = [name for name in directories if name != ".git"]
            if ".gitattributes" not in filenames:
                continue
            path = Path(root) / ".gitattributes"
            if path.is_symlink():
                raise UnsafeRepositoryError(
                    "working-tree attributes cannot be a symbolic link"
                )
            try:
                attributes_stat = path.stat(follow_symlinks=False)
                if not stat.S_ISREG(attributes_stat.st_mode):
                    raise UnsafeRepositoryError(
                        "working-tree attributes must be a regular file"
                    )
                if attributes_stat.st_size > 1024 * 1024:
                    raise UnsafeRepositoryError(
                        "working-tree attributes exceed the safety size limit"
                    )
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError as error:
                raise UnsafeRepositoryError(
                    "cannot inspect working-tree attributes"
                ) from error
            if ATTRIBUTE_FILTER_PATTERN.search(content):
                raise UnsafeRepositoryError(
                    "working-tree attributes select a checkout filter"
                )

    def _status(self, repository: Path) -> bytes:
        return self._git(
            repository,
            ["status", "--porcelain=v1", "--untracked-files=all"],
        ).stdout.strip()

    def _resolve_commit(self, repository: Path, ref: str) -> str:
        result = self._git_text(
            repository,
            ["rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"],
        )
        if not re.fullmatch(COMMIT_PATTERN, result):
            raise RepositoryValidationError("Git returned an invalid commit ID")
        return result

    @staticmethod
    def _resolved_git_path(repository: Path, value: str) -> Path:
        path = Path(value)
        if not path.is_absolute():
            path = repository / path
        return path.resolve(strict=False)

    def _git_text(self, repository: Path, args: list[str]) -> str:
        return (
            self._git(repository, args).stdout.decode("utf-8", errors="strict").strip()
        )

    def _git(
        self,
        repository: Path,
        args: list[str],
        *,
        allowed_returncodes: Collection[int] = (0,),
        controller_config: bool = True,
    ) -> subprocess.CompletedProcess[bytes]:
        environment = {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "HOME": str(self.root.resolve(strict=False)),
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": os.environ.get("PATH", ""),
        }
        command = [self.git_binary]
        if controller_config:
            command.extend(
                [
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-c",
                    "core.fsmonitor=false",
                    "-c",
                    "credential.helper=",
                ]
            )
        command.extend(["-C", str(repository), *args])
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                timeout=self.timeout_seconds,
                env=environment,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise GitCommandError(f"cannot execute Git operation: {args[0]}") from error
        if result.returncode not in allowed_returncodes:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            if not detail:
                detail = "no diagnostic output"
            raise GitCommandError(
                f"Git operation {args[0]} failed with {result.returncode}: {detail}"
            )
        return result
