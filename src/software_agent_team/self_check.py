"""Versioned task-readiness checks shared by product lifecycle checkpoints."""

from __future__ import annotations

import json
import os
import re
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from software_agent_team.integrity import canonical_model_sha256
from software_agent_team.model_metadata import ModelMetadataSource

SELF_CHECK_SCHEMA_VERSION = 1
_CHECK_ID_PATTERN = r"^[a-z][a-z0-9_.:-]{2,127}$"
_RUN_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]*$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
MAX_SELF_CHECK_REPORT_BYTES = 4 * 1024 * 1024


class SelfCheckError(RuntimeError):
    """Raised when task-readiness evidence is invalid or cannot be persisted."""


class SelfCheckCheckpoint(StrEnum):
    """Mandatory lifecycle boundaries that consume readiness evidence."""

    TASK_ADMISSION = "task_admission"
    PLAN_EXECUTION = "plan_execution"


class SelfCheckCategory(StrEnum):
    """Stable user-facing groups for readiness results."""

    APPLICATION = "application"
    SYSTEM = "system"
    ENVIRONMENT = "environment"
    TOOL = "tool"
    RUNTIME = "runtime"
    MODEL = "model"
    TASK = "task"
    BUDGET = "budget"
    ROUTE = "route"
    AGENT = "agent"
    WORKSPACE = "workspace"
    DELIVERY = "delivery"


class SelfCheckOwner(StrEnum):
    """Authority responsible for the checked fact or its remediation."""

    SAT = "sat"
    USER = "user"
    HOST = "host"
    PROVIDER = "provider"
    APPROVED_PLAN = "approved_plan"


class SelfCheckSeverity(StrEnum):
    """Consequence if a check is not satisfied."""

    INFO = "info"
    WARNING = "warning"
    REQUIRED = "required"


class SelfCheckStatus(StrEnum):
    """Current semantic result of one readiness check."""

    PASS = "pass"
    WARNING = "warning"
    NEEDS_INPUT = "needs_input"
    BLOCKED = "blocked"
    STALE = "stale"


class SelfCheckFreshness(StrEnum):
    """Whether the result still describes its declared inputs."""

    FRESH = "fresh"
    STALE = "stale"


class SelfCheckEvidence(BaseModel):
    """One non-secret pointer to the fact behind a readiness conclusion."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal[
        "controller_contract",
        "local_observation",
        "persisted_record",
        "remote_observation",
        "user_authorization",
    ]
    reference: str = Field(min_length=1, max_length=4096)
    sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)

    @field_validator("reference")
    @classmethod
    def require_safe_reference(cls, value: str) -> str:
        if value != value.strip() or any(ord(character) < 32 for character in value):
            raise ValueError("self-check evidence reference must be clean text")
        return value


class TaskModelMetadata(BaseModel):
    """Run-scoped price and context facts frozen before the first model call."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    profile_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    model: str = Field(min_length=3, max_length=500)
    input_cost_per_million_usd: Decimal = Field(ge=0)
    output_cost_per_million_usd: Decimal = Field(ge=0)
    pricing_source: ModelMetadataSource
    context_window_tokens: int = Field(ge=1)
    context_source: ModelMetadataSource
    observed_at: datetime

    @field_validator("model")
    @classmethod
    def require_model_reference(cls, value: str) -> str:
        cleaned = value.strip()
        provider, separator, model = cleaned.partition("/")
        if (
            not provider
            or not separator
            or not model
            or any(character.isspace() for character in cleaned)
        ):
            raise ValueError("task model metadata requires provider/model")
        return cleaned

    @field_validator("observed_at")
    @classmethod
    def require_aware_observation_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("task model metadata time must include a UTC offset")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_zero_price_source(self) -> Self:
        if self.pricing_source is ModelMetadataSource.CONFIRMED_ZERO and (
            self.input_cost_per_million_usd != 0
            or self.output_cost_per_million_usd != 0
        ):
            raise ValueError("confirmed-zero task pricing requires two zero prices")
        return self


class TaskResourceAuthorization(BaseModel):
    """User authority for total task cost and an optional whole-run deadline."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    maximum_estimated_cost_usd: Decimal = Field(ge=0)
    run_deadline_seconds: int | None = Field(default=None, ge=1)
    model_metadata: tuple[TaskModelMetadata, ...] = Field(min_length=1)
    confirmation: Literal["user_confirmed"] = "user_confirmed"
    authorized_at: datetime

    @field_validator("model_metadata")
    @classmethod
    def require_unique_models(
        cls,
        values: tuple[TaskModelMetadata, ...],
    ) -> tuple[TaskModelMetadata, ...]:
        profile_ids = [item.profile_id for item in values]
        models = [item.model for item in values]
        if len(profile_ids) != len(set(profile_ids)):
            raise ValueError("task model profile IDs must be unique")
        if len(models) != len(set(models)):
            raise ValueError("task model identities must be unique")
        return values

    @field_validator("authorized_at")
    @classmethod
    def require_aware_authorization_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("task authorization time must include a UTC offset")
        return value.astimezone(UTC)

    @property
    def deadline_at(self) -> datetime | None:
        """Return the exact whole-run deadline derived from user authority."""

        if self.run_deadline_seconds is None:
            return None
        return self.authorized_at + timedelta(seconds=self.run_deadline_seconds)


class SelfCheckResult(BaseModel):
    """One dependency-aware, actionable readiness result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=_CHECK_ID_PATTERN)
    checkpoint: SelfCheckCheckpoint
    category: SelfCheckCategory
    owner: SelfCheckOwner
    dependencies: tuple[str, ...] = ()
    input_sha256: str = Field(pattern=_SHA256_PATTERN)
    checked_at: datetime
    freshness: SelfCheckFreshness = SelfCheckFreshness.FRESH
    severity: SelfCheckSeverity
    status: SelfCheckStatus
    observed_fact: str = Field(min_length=1, max_length=4000)
    evidence: tuple[SelfCheckEvidence, ...] = Field(min_length=1, max_length=32)
    consequence: str | None = Field(default=None, min_length=1, max_length=2000)
    remediation: str | None = Field(default=None, min_length=1, max_length=2000)
    rerun_rule: str = Field(min_length=1, max_length=2000)

    @field_validator("dependencies")
    @classmethod
    def require_unique_dependencies(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("self-check dependencies must be unique")
        if any(re.fullmatch(_CHECK_ID_PATTERN, value) is None for value in values):
            raise ValueError("self-check dependency ID is invalid")
        return values

    @field_validator("checked_at")
    @classmethod
    def require_aware_check_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("self-check timestamp must include a UTC offset")
        return value.astimezone(UTC)

    @field_validator("observed_fact", "consequence", "remediation", "rerun_rule")
    @classmethod
    def require_safe_text(cls, value: str | None) -> str | None:
        if value is not None and any(
            character in value for character in ("\x00", "\r")
        ):
            raise ValueError("self-check text contains control characters")
        return value

    @model_validator(mode="after")
    def require_consistent_actionability(self) -> Self:
        if self.id in self.dependencies:
            raise ValueError("a self-check cannot depend on itself")
        is_stale = self.status is SelfCheckStatus.STALE
        if is_stale != (self.freshness is SelfCheckFreshness.STALE):
            raise ValueError("stale status and freshness must agree")
        if self.status is SelfCheckStatus.PASS:
            if self.consequence is not None or self.remediation is not None:
                raise ValueError("passing self-checks do not require remediation")
        elif self.consequence is None or self.remediation is None:
            raise ValueError(
                "non-passing self-checks require consequence and remediation"
            )
        if (
            self.status is SelfCheckStatus.BLOCKED
            and self.severity is not SelfCheckSeverity.REQUIRED
        ):
            raise ValueError("blocking self-checks must have required severity")
        return self


class TaskSelfCheckReport(BaseModel):
    """Immutable readiness snapshot at one task lifecycle checkpoint."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[SELF_CHECK_SCHEMA_VERSION] = SELF_CHECK_SCHEMA_VERSION
    run_id: str = Field(pattern=_RUN_ID_PATTERN)
    checkpoint: SelfCheckCheckpoint
    revision: int = Field(ge=1)
    previous_report_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    created_at: datetime
    resource_authorization: TaskResourceAuthorization | None = None
    checks: tuple[SelfCheckResult, ...] = Field(min_length=1, max_length=2048)

    @field_validator("created_at")
    @classmethod
    def require_aware_creation_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("self-check report time must include a UTC offset")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_dependency_graph(self) -> Self:
        ids = [check.id for check in self.checks]
        if len(ids) != len(set(ids)):
            raise ValueError("self-check result IDs must be unique")
        known = set(ids)
        for check in self.checks:
            if unknown := set(check.dependencies) - known:
                raise ValueError(
                    f"self-check {check.id} has unknown dependencies: "
                    + ", ".join(sorted(unknown))
                )
            if (
                self.checkpoint is SelfCheckCheckpoint.TASK_ADMISSION
                and check.checkpoint is not SelfCheckCheckpoint.TASK_ADMISSION
            ):
                raise ValueError("task-admission report contains a future checkpoint")
        _topological_check_order(self.checks)
        if self.revision == 1 and self.previous_report_sha256 is not None:
            raise ValueError("first self-check report cannot have a predecessor")
        if self.revision > 1 and self.previous_report_sha256 is None:
            raise ValueError("later self-check reports require a predecessor digest")
        return self

    @property
    def ready(self) -> bool:
        """Return whether no result requires input, blocks, or is stale."""

        return all(
            check.status
            not in {
                SelfCheckStatus.NEEDS_INPUT,
                SelfCheckStatus.BLOCKED,
                SelfCheckStatus.STALE,
            }
            for check in self.checks
        )

    @property
    def needs_input(self) -> tuple[SelfCheckResult, ...]:
        """Return the exact unresolved user-input requirements."""

        return tuple(
            check
            for check in self.checks
            if check.status is SelfCheckStatus.NEEDS_INPUT
        )

    @property
    def sha256(self) -> str:
        """Return the canonical digest used by the immutable report chain."""

        return canonical_model_sha256(self)


def observation_sha256(value: object) -> str:
    """Hash non-secret evaluator input for freshness comparison."""

    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    import hashlib

    return hashlib.sha256(canonical).hexdigest()


def invalidate_self_check_results(
    checks: Sequence[SelfCheckResult],
    changed_check_ids: Iterable[str],
    *,
    reason: str,
) -> tuple[SelfCheckResult, ...]:
    """Mark changed checks and every transitive dependent stale."""

    by_id = {check.id: check for check in checks}
    if len(by_id) != len(checks):
        raise SelfCheckError("cannot invalidate duplicate self-check IDs")
    changed = set(changed_check_ids)
    if unknown := changed - set(by_id):
        raise SelfCheckError(
            "cannot invalidate unknown self-checks: " + ", ".join(sorted(unknown))
        )
    dependents: dict[str, set[str]] = {check_id: set() for check_id in by_id}
    for check in checks:
        for dependency in check.dependencies:
            if dependency not in by_id:
                raise SelfCheckError(
                    f"self-check {check.id} has unknown dependency {dependency}"
                )
            dependents[dependency].add(check.id)
    stale = set(changed)
    queue = deque(changed)
    while queue:
        for dependent in dependents[queue.popleft()]:
            if dependent not in stale:
                stale.add(dependent)
                queue.append(dependent)
    return tuple(
        check.model_copy(
            update={
                "status": SelfCheckStatus.STALE,
                "freshness": SelfCheckFreshness.STALE,
                "consequence": (
                    "This readiness conclusion cannot authorize further work because "
                    f"{reason}."
                ),
                "remediation": "Re-run this check after its dependencies are fresh.",
            }
        )
        if check.id in stale
        else check
        for check in checks
    )


def refresh_stale_self_checks(
    checks: Sequence[SelfCheckResult],
    replacements: Mapping[str, SelfCheckResult],
) -> tuple[SelfCheckResult, ...]:
    """Replace exactly stale results after dependencies were re-evaluated."""

    stale_ids = {check.id for check in checks if check.status is SelfCheckStatus.STALE}
    if set(replacements) != stale_ids:
        missing = stale_ids - set(replacements)
        extra = set(replacements) - stale_ids
        detail = []
        if missing:
            detail.append("missing " + ", ".join(sorted(missing)))
        if extra:
            detail.append("not stale " + ", ".join(sorted(extra)))
        raise SelfCheckError("invalid self-check refresh: " + "; ".join(detail))
    refreshed: list[SelfCheckResult] = []
    for previous in checks:
        replacement = replacements.get(previous.id)
        if replacement is None:
            refreshed.append(previous)
            continue
        if (
            replacement.id != previous.id
            or replacement.checkpoint is not previous.checkpoint
            or replacement.category is not previous.category
            or replacement.owner is not previous.owner
            or replacement.dependencies != previous.dependencies
            or replacement.freshness is not SelfCheckFreshness.FRESH
            or replacement.status is SelfCheckStatus.STALE
        ):
            raise SelfCheckError(
                f"refreshed self-check changed its stable definition: {previous.id}"
            )
        refreshed.append(replacement)
    _topological_check_order(refreshed)
    return tuple(refreshed)


def reconcile_self_check_report(
    previous: TaskSelfCheckReport,
    observed: TaskSelfCheckReport,
) -> TaskSelfCheckReport | None:
    """Append only changed facts and their dependents to one report chain.

    ``observed`` is a fresh evaluator snapshot. Its revision metadata is ignored;
    unchanged checks retain their original evidence and timestamp. Dynamic
    plan revisions may add, remove, or redefine checks; those graph changes and
    their transitive dependents are refreshed from the new observation. Returning
    ``None`` means the attempted recheck observed the same checkpoint, graph,
    inputs, and resource authority and therefore must not create a redundant
    evidence revision.
    """

    if previous.run_id != observed.run_id:
        raise SelfCheckError("self-check recheck changed the task identity")
    previous_by_id = {check.id: check for check in previous.checks}
    observed_by_id = {check.id: check for check in observed.checks}
    previous_ids = set(previous_by_id)
    observed_ids = set(observed_by_id)

    def semantic_result(check: SelfCheckResult) -> dict[str, object]:
        return check.model_dump(
            mode="json",
            exclude={"checked_at", "input_sha256"},
        )

    changed = previous_ids.symmetric_difference(observed_ids)
    changed.update(
        check_id
        for check_id in previous_ids & observed_ids
        if semantic_result(previous_by_id[check_id])
        != semantic_result(observed_by_id[check_id])
        or previous_by_id[check_id].input_sha256
        != observed_by_id[check_id].input_sha256
    )
    authority_changed = (
        previous.resource_authorization != observed.resource_authorization
    )
    checkpoint_changed = previous.checkpoint is not observed.checkpoint
    if not changed and not authority_changed and not checkpoint_changed:
        return None

    # A removed or redefined dependency can affect checks in either graph.
    # Traverse both so a plan edit cannot retain evidence that depended on a
    # superseded route, Agent, or authority edge.
    affected = set(changed)
    for graph in (previous.checks, observed.checks):
        dependents: dict[str, set[str]] = {}
        for check in graph:
            for dependency in check.dependencies:
                dependents.setdefault(dependency, set()).add(check.id)
        queue = deque(affected)
        while queue:
            for dependent in dependents.get(queue.popleft(), ()):
                if dependent not in affected:
                    affected.add(dependent)
                    queue.append(dependent)

    refreshed = tuple(
        (
            prior
            if check.id not in affected
            and (prior := previous_by_id.get(check.id)) is not None
            else check
        )
        for check in observed.checks
    )
    return TaskSelfCheckReport(
        run_id=previous.run_id,
        checkpoint=observed.checkpoint,
        revision=previous.revision + 1,
        previous_report_sha256=previous.sha256,
        created_at=observed.created_at,
        resource_authorization=observed.resource_authorization,
        checks=refreshed,
    )


class TaskSelfCheckStore:
    """Write-once report chain beneath one SAT-owned state root."""

    def __init__(self, root: Path) -> None:
        if not root.is_absolute() or root == Path(root.anchor):
            raise SelfCheckError("self-check root must be a specific absolute path")
        self.root = root

    def persist(self, report: TaskSelfCheckReport) -> Path:
        """Append one report only when its revision and digest chain are valid."""

        run_directory = self._run_directory(report.run_id, create=True)
        latest = self.load_latest(report.run_id)
        if latest is None:
            if report.revision != 1 or report.previous_report_sha256 is not None:
                raise SelfCheckError("first persisted self-check must be revision 1")
        elif (
            report.revision != latest.revision + 1
            or report.previous_report_sha256 != latest.sha256
        ):
            raise SelfCheckError(
                "self-check report does not extend the latest revision"
            )
        destination = run_directory / self._filename(report)
        if destination.exists() or destination.is_symlink():
            raise SelfCheckError(f"self-check report already exists: {destination}")
        content = (
            json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2)
            + "\n"
        ).encode()
        if len(content) > MAX_SELF_CHECK_REPORT_BYTES:
            raise SelfCheckError("self-check report exceeds the persistence limit")
        temporary = run_directory / f".{destination.name}.{uuid4().hex}.tmp"
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            try:
                os.link(temporary, destination)
            except FileExistsError as error:
                raise SelfCheckError(
                    f"self-check report already exists: {destination}"
                ) from error
            _fsync_directory(run_directory)
        finally:
            temporary.unlink(missing_ok=True)
        return destination

    def load_latest(self, run_id: str) -> TaskSelfCheckReport | None:
        """Load and verify the complete immutable chain for one task."""

        run_directory = self._run_directory(run_id, create=False)
        if run_directory is None:
            return None
        paths = sorted(run_directory.glob("*.json"))
        if not paths:
            return None
        previous: TaskSelfCheckReport | None = None
        for expected_revision, path in enumerate(paths, start=1):
            if path.is_symlink() or not path.is_file():
                raise SelfCheckError(f"self-check entry is not a regular file: {path}")
            try:
                raw = path.read_bytes()
                if len(raw) > MAX_SELF_CHECK_REPORT_BYTES:
                    raise SelfCheckError("self-check report exceeds the read limit")
                report = TaskSelfCheckReport.model_validate_json(raw)
            except (OSError, ValueError) as error:
                raise SelfCheckError(f"self-check report is invalid: {path}") from error
            if report.run_id != run_id or report.revision != expected_revision:
                raise SelfCheckError("self-check report path and revision disagree")
            if path.name != self._filename(report):
                raise SelfCheckError("self-check report filename is not canonical")
            if previous is None:
                if report.previous_report_sha256 is not None:
                    raise SelfCheckError("first self-check report has a predecessor")
            elif report.previous_report_sha256 != previous.sha256:
                raise SelfCheckError("self-check report digest chain is broken")
            previous = report
        return previous

    def record_observation(
        self,
        observed: TaskSelfCheckReport,
    ) -> tuple[TaskSelfCheckReport, Path | None]:
        """Reconcile one fresh observation against durable task history.

        The caller does not carry a previous report in memory. This method
        reloads and verifies the immutable chain, making remediation retries and
        checkpoint changes safe across CLI process restarts. ``None`` as the
        returned path means the observation was identical and no new revision
        was written.
        """

        latest = self.load_latest(observed.run_id)
        if latest is None:
            first = observed.model_copy(
                update={
                    "revision": 1,
                    "previous_report_sha256": None,
                }
            )
            return first, self.persist(first)
        reconciled = reconcile_self_check_report(latest, observed)
        if reconciled is None:
            return latest, None
        return reconciled, self.persist(reconciled)

    def _run_directory(self, run_id: str, *, create: bool) -> Path | None:
        if re.fullmatch(_RUN_ID_PATTERN, run_id) is None:
            raise SelfCheckError("self-check run ID is invalid")
        if create:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self.root.is_symlink() or not self.root.is_dir():
                raise SelfCheckError("self-check root must be a real directory")
            self.root.chmod(0o700)
        elif not self.root.exists():
            return None
        elif self.root.is_symlink() or not self.root.is_dir():
            raise SelfCheckError("self-check root must be a real directory")
        run_directory = self.root / run_id
        if create:
            run_directory.mkdir(exist_ok=True, mode=0o700)
            if run_directory.is_symlink() or not run_directory.is_dir():
                raise SelfCheckError("self-check task path must be a real directory")
            run_directory.chmod(0o700)
            return run_directory
        if not run_directory.exists():
            return None
        if run_directory.is_symlink() or not run_directory.is_dir():
            raise SelfCheckError("self-check task path must be a real directory")
        return run_directory

    @staticmethod
    def _filename(report: TaskSelfCheckReport) -> str:
        return f"{report.revision:04d}-{report.checkpoint.value}.json"


def render_self_check_report(
    report: TaskSelfCheckReport,
    *,
    visibility: Literal["compact", "standard", "detailed"] = "standard",
) -> str:
    """Render the same report at three non-secret visibility levels."""

    counts = {
        status: sum(check.status is status for check in report.checks)
        for status in SelfCheckStatus
    }
    lines = [
        f"Task self-check: {report.checkpoint.value.replace('_', ' ')}",
        (
            f"  {len(report.checks)} checks: {counts[SelfCheckStatus.PASS]} pass, "
            f"{counts[SelfCheckStatus.WARNING]} warning, "
            f"{counts[SelfCheckStatus.NEEDS_INPUT]} need input, "
            f"{counts[SelfCheckStatus.BLOCKED]} blocked, "
            f"{counts[SelfCheckStatus.STALE]} stale"
        ),
    ]
    visible = (
        report.checks
        if visibility == "detailed"
        else tuple(
            check
            for check in report.checks
            if visibility == "standard" or check.status is not SelfCheckStatus.PASS
        )
    )
    symbols = {
        SelfCheckStatus.PASS: "✓",
        SelfCheckStatus.WARNING: "!",
        SelfCheckStatus.NEEDS_INPUT: "?",
        SelfCheckStatus.BLOCKED: "✗",
        SelfCheckStatus.STALE: "~",
    }
    for check in visible:
        lines.append(f"{symbols[check.status]} {check.id}: {check.observed_fact}")
        if check.status is not SelfCheckStatus.PASS:
            lines.append(f"  Consequence: {check.consequence}")
            lines.append(
                f"  Evidence: {', '.join(item.reference for item in check.evidence)}"
            )
            lines.append(f"  Action: {check.remediation}")
        elif visibility == "detailed":
            lines.append(
                f"  Evidence: {', '.join(item.reference for item in check.evidence)}"
            )
            lines.append(f"  Re-run: {check.rerun_rule}")
    lines.append("  Result: ready" if report.ready else "  Result: not ready")
    return "\n".join(lines)


def _topological_check_order(checks: Sequence[SelfCheckResult]) -> tuple[str, ...]:
    by_id = {check.id: check for check in checks}
    if len(by_id) != len(checks):
        raise ValueError("self-check result IDs must be unique")
    indegree = {check_id: 0 for check_id in by_id}
    dependents = {check_id: set() for check_id in by_id}
    for check in checks:
        for dependency in check.dependencies:
            if dependency not in by_id:
                raise ValueError(
                    f"self-check {check.id} has unknown dependency {dependency}"
                )
            indegree[check.id] += 1
            dependents[dependency].add(check.id)
    ready = deque(
        sorted(check_id for check_id, count in indegree.items() if count == 0)
    )
    ordered: list[str] = []
    while ready:
        check_id = ready.popleft()
        ordered.append(check_id)
        for dependent in sorted(dependents[check_id]):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
    if len(ordered) != len(checks):
        raise ValueError("self-check dependency graph contains a cycle")
    return tuple(ordered)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
