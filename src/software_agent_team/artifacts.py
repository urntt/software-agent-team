"""Validated contracts for requests, phase artifacts, and Agent handoffs."""

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ARTIFACT_SCHEMA_VERSION = 1
COMMIT_PATTERN = r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"


class AgentRole(StrEnum):
    """Agent responsibilities available to versioned team configurations."""

    CLARIFIER = "clarifier"
    SINGLE_AGENT = "single_agent"
    PLANNER = "planner"
    GENERALIST_DEVELOPER = "generalist_developer"
    FRONTEND_DEVELOPER = "frontend_developer"
    BACKEND_DEVELOPER = "backend_developer"
    INTEGRATOR = "integrator"
    TESTER = "tester"
    REVIEWER = "reviewer"


class HandoffStatus(StrEnum):
    """Terminal status for one Agent execution."""

    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"


class ArtifactKind(StrEnum):
    """Persisted artifact types used by the workflow."""

    CLARIFICATION_RECORD = "clarification_record"
    TASK_BRIEF = "task_brief"
    IMPLEMENTATION_PLAN = "implementation_plan"
    WORK_RESULT = "work_result"
    TEST_REPORT = "test_report"
    REVIEW_REPORT = "review_report"
    ITERATION_RECORD = "iteration_record"
    FINAL_REPORT = "final_report"


class CheckStatus(StrEnum):
    """Outcome of a deterministic check or acceptance criterion."""

    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"


class ReviewSeverity(StrEnum):
    """Impact category for an independent review finding."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ReviewVerdict(StrEnum):
    """Reviewer's semantic recommendation to the controller."""

    ACCEPT = "accept"
    REVISE = "revise"
    FAIL = "fail"


class IterationDecision(StrEnum):
    """Controller decision after test and review evidence is available."""

    ACCEPT = "accept"
    REVISE = "revise"
    FAIL = "fail"


class FinalStatus(StrEnum):
    """Human-readable final report outcome."""

    COMPLETED = "completed"
    FAILED = "failed"


type ArtifactProducer = AgentRole | Literal["controller"]


IMPLEMENTATION_ROLES = {
    AgentRole.SINGLE_AGENT,
    AgentRole.GENERALIST_DEVELOPER,
    AgentRole.FRONTEND_DEVELOPER,
    AgentRole.BACKEND_DEVELOPER,
    AgentRole.INTEGRATOR,
}


def _require_clean_unique_items(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    cleaned = tuple(value.strip() for value in values)
    if any(not value for value in cleaned):
        raise ValueError("list entries must not be blank")
    if len(cleaned) != len(set(cleaned)):
        raise ValueError("list entries must be unique")
    return cleaned


def _require_safe_relative_path(value: str) -> str:
    if "\\" in value:
        raise ValueError("paths must use POSIX separators")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path == PurePosixPath("."):
        raise ValueError("paths must be safe relative paths")
    return value


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("artifact timestamps must include a timezone")
    return value.astimezone(UTC)


class AcceptanceCriterion(BaseModel):
    """One independently verifiable condition for accepting a result."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, pattern=r"^[A-Z][A-Z0-9_-]*$")
    description: str = Field(min_length=1)
    verification: str = Field(min_length=1)


class TaskBrief(BaseModel):
    """Confirmed requirements passed unchanged to comparable team runs."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    title: str = Field(min_length=1)
    source_request: str = Field(min_length=1)
    requirements: list[str] = Field(min_length=1)
    acceptance_criteria: list[AcceptanceCriterion] = Field(min_length=1)
    constraints: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    confirmed: bool = False

    @field_validator("requirements", "constraints", "assumptions", "open_questions")
    @classmethod
    def require_clean_unique_items(cls, values: list[str]) -> list[str]:
        """Reject blank or duplicate list entries."""

        cleaned = [value.strip() for value in values]
        if any(not value for value in cleaned):
            raise ValueError("task brief list entries must not be blank")
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("task brief list entries must be unique")
        return cleaned

    @model_validator(mode="after")
    def validate_confirmation(self) -> Self:
        """A confirmed brief cannot retain unresolved questions."""

        criterion_ids = [criterion.id for criterion in self.acceptance_criteria]
        if len(criterion_ids) != len(set(criterion_ids)):
            raise ValueError("acceptance criterion IDs must be unique")
        if self.confirmed and self.open_questions:
            raise ValueError("a confirmed task brief cannot contain open questions")
        return self


class ArtifactReference(BaseModel):
    """Run-relative reference to a persisted workflow artifact."""

    model_config = ConfigDict(extra="forbid")

    kind: ArtifactKind
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    description: str = ""

    @field_validator("path")
    @classmethod
    def require_safe_relative_path(cls, value: str) -> str:
        """Reject paths that can escape a run artifact directory."""

        if "\\" in value:
            raise ValueError("artifact paths must use POSIX separators")
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or path == PurePosixPath("."):
            raise ValueError("artifact paths must be safe run-relative paths")
        return value


class HandoffEnvelope(BaseModel):
    """Common metadata exchanged between Agent responsibilities."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    team_id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    iteration: int = Field(ge=1, le=3)
    source_role: AgentRole
    target_role: AgentRole | None = None
    status: HandoffStatus
    summary: str = Field(min_length=1)
    input_commit: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{7,64}$",
    )
    artifacts: list[ArtifactReference] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)

    @field_validator("blockers")
    @classmethod
    def require_clean_unique_blockers(cls, values: list[str]) -> list[str]:
        """Keep failure evidence concise and unambiguous."""

        cleaned = [value.strip() for value in values]
        if any(not value for value in cleaned):
            raise ValueError("blockers must not be blank")
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("blockers must be unique")
        return cleaned

    @model_validator(mode="after")
    def validate_handoff(self) -> Self:
        """Enforce evidence for failures and meaningful role boundaries."""

        if (
            self.status in {HandoffStatus.BLOCKED, HandoffStatus.FAILED}
            and not self.blockers
        ):
            raise ValueError("blocked or failed handoffs must identify a blocker")
        if self.target_role is not None and self.target_role == self.source_role:
            raise ValueError("source and target roles must differ")
        return self


class PhaseArtifact(BaseModel):
    """Metadata shared by every durable workflow artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[ARTIFACT_SCHEMA_VERSION] = ARTIFACT_SCHEMA_VERSION
    kind: ArtifactKind
    run_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    team_id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    producer: ArtifactProducer
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        """Normalize persisted timestamps to UTC."""

        return _require_utc(value)


class IterationArtifact(PhaseArtifact):
    """Metadata shared by artifacts produced within one implementation pass."""

    iteration: int = Field(ge=1, le=3)


class PlanTask(BaseModel):
    """One attributable implementation task in a Planner artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, pattern=r"^TASK_[A-Z0-9_]+$")
    owner: AgentRole
    description: str = Field(min_length=1)
    dependencies: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = Field(min_length=1)
    expected_paths: tuple[str, ...] = ()

    @field_validator("dependencies", "acceptance_criteria")
    @classmethod
    def require_clean_unique_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Reject ambiguous task and criterion references."""

        return _require_clean_unique_items(values)

    @field_validator("expected_paths")
    @classmethod
    def require_safe_expected_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Keep expected repository paths relative and portable."""

        cleaned = _require_clean_unique_items(values)
        return tuple(_require_safe_relative_path(value) for value in cleaned)


class ImplementationPlan(IterationArtifact):
    """Planner-owned implementation plan for a confirmed task brief."""

    kind: Literal[ArtifactKind.IMPLEMENTATION_PLAN] = ArtifactKind.IMPLEMENTATION_PLAN
    producer: Literal[AgentRole.PLANNER] = AgentRole.PLANNER
    iteration: Literal[1] = 1
    objective: str = Field(min_length=1)
    approach: tuple[str, ...] = Field(min_length=1)
    tasks: tuple[PlanTask, ...] = Field(min_length=1)
    risks: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()

    @field_validator("approach", "risks", "assumptions")
    @classmethod
    def require_clean_unique_text(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Reject blank and duplicate planning statements."""

        return _require_clean_unique_items(values)

    @model_validator(mode="after")
    def validate_tasks(self) -> Self:
        """Require unique, resolvable, acyclic task dependencies."""

        task_ids = [task.id for task in self.tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("implementation plan task IDs must be unique")
        known = set(task_ids)
        dependencies = {task.id: set(task.dependencies) for task in self.tasks}
        for task_id, task_dependencies in dependencies.items():
            unknown = task_dependencies - known
            if unknown:
                raise ValueError(
                    f"task {task_id} references unknown dependencies: "
                    f"{', '.join(sorted(unknown))}"
                )
            if task_id in task_dependencies:
                raise ValueError("a plan task cannot depend on itself")

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(task_id: str) -> None:
            if task_id in visiting:
                raise ValueError("implementation plan dependencies must be acyclic")
            if task_id in visited:
                return
            visiting.add(task_id)
            for dependency in dependencies[task_id]:
                visit(dependency)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in task_ids:
            visit(task_id)
        return self


class WorkResult(IterationArtifact):
    """Verified source result attributed to one implementation Agent."""

    kind: Literal[ArtifactKind.WORK_RESULT] = ArtifactKind.WORK_RESULT
    producer: AgentRole
    input_commit: str = Field(pattern=COMMIT_PATTERN)
    output_commit: str = Field(pattern=COMMIT_PATTERN)
    summary: str = Field(min_length=1)
    completed_tasks: tuple[str, ...] = Field(min_length=1)
    changed_files: tuple[str, ...] = Field(min_length=1)
    unresolved_issues: tuple[str, ...] = ()

    @field_validator("completed_tasks", "unresolved_issues")
    @classmethod
    def require_clean_unique_text(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Reject blank and duplicate implementation statements."""

        return _require_clean_unique_items(values)

    @field_validator("changed_files")
    @classmethod
    def require_safe_changed_files(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Require concrete repository-relative changed paths."""

        cleaned = _require_clean_unique_items(values)
        return tuple(_require_safe_relative_path(value) for value in cleaned)

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        """Require an implementation role and a new immutable commit."""

        if self.producer not in IMPLEMENTATION_ROLES:
            raise ValueError("work results require an implementation role")
        if self.input_commit == self.output_commit:
            raise ValueError("work result output commit must differ from input")
        return self


class CommandEvidence(BaseModel):
    """Controller-recorded evidence for one deterministic command."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, pattern=r"^CHECK_[A-Z0-9_]+$")
    argv: tuple[str, ...] = Field(min_length=1)
    exit_code: int | None = None
    timed_out: bool = False
    duration_ms: int = Field(ge=0)
    stdout_path: str = Field(min_length=1)
    stderr_path: str = Field(min_length=1)
    summary: str = ""

    @field_validator("argv")
    @classmethod
    def require_clean_argv(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Persist an unambiguous argv rather than a shell command string."""

        if any(not value for value in values):
            raise ValueError("command arguments must not be empty")
        return values

    @field_validator("stdout_path", "stderr_path")
    @classmethod
    def require_safe_output_path(cls, value: str) -> str:
        """Keep command output inside the run directory."""

        return _require_safe_relative_path(value)

    @model_validator(mode="after")
    def validate_exit(self) -> Self:
        """Distinguish process timeout from a normal process exit."""

        if self.timed_out and self.exit_code is not None:
            raise ValueError("timed-out commands cannot report an exit code")
        if not self.timed_out and self.exit_code is None:
            raise ValueError("completed commands require an exit code")
        return self


class CriterionResult(BaseModel):
    """Evidence-backed result for one confirmed acceptance criterion."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    criterion_id: str = Field(min_length=1, pattern=r"^[A-Z][A-Z0-9_-]*$")
    status: CheckStatus
    command_ids: tuple[str, ...] = ()
    detail: str = Field(min_length=1)

    @field_validator("command_ids")
    @classmethod
    def require_clean_command_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Reject ambiguous command references."""

        return _require_clean_unique_items(values)


class TestReport(IterationArtifact):
    """Tester analysis grounded in deterministic command evidence."""

    kind: Literal[ArtifactKind.TEST_REPORT] = ArtifactKind.TEST_REPORT
    producer: Literal[AgentRole.TESTER] = AgentRole.TESTER
    input_commit: str = Field(pattern=COMMIT_PATTERN)
    status: CheckStatus
    commands: tuple[CommandEvidence, ...] = Field(min_length=1)
    criteria: tuple[CriterionResult, ...] = Field(min_length=1)
    findings: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    summary: str = Field(min_length=1)

    @field_validator("findings", "blockers")
    @classmethod
    def require_clean_unique_text(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Reject blank and duplicate verification findings."""

        return _require_clean_unique_items(values)

    @model_validator(mode="after")
    def validate_evidence(self) -> Self:
        """Keep overall test status consistent with recorded evidence."""

        command_ids = [command.id for command in self.commands]
        if len(command_ids) != len(set(command_ids)):
            raise ValueError("test command IDs must be unique")
        criterion_ids = [criterion.criterion_id for criterion in self.criteria]
        if len(criterion_ids) != len(set(criterion_ids)):
            raise ValueError("test criterion IDs must be unique")
        known_commands = set(command_ids)
        for criterion in self.criteria:
            unknown = set(criterion.command_ids) - known_commands
            if unknown:
                raise ValueError(
                    f"criterion {criterion.criterion_id} references unknown commands"
                )

        commands_passed = all(
            not command.timed_out and command.exit_code == 0
            for command in self.commands
        )
        criteria_passed = all(
            criterion.status is CheckStatus.PASSED for criterion in self.criteria
        )
        if self.status is CheckStatus.PASSED:
            if not commands_passed or not criteria_passed or self.blockers:
                raise ValueError("passed test reports require all evidence to pass")
        elif self.status is CheckStatus.BLOCKED:
            if not self.blockers:
                raise ValueError("blocked test reports must identify a blocker")
        elif commands_passed and criteria_passed:
            raise ValueError("failed test reports require failing evidence")
        return self


class ReviewFinding(BaseModel):
    """One attributable independent-review finding."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, pattern=r"^FINDING_[A-Z0-9_]+$")
    severity: ReviewSeverity
    blocking: bool
    category: str = Field(min_length=1)
    description: str = Field(min_length=1)
    recommendation: str = Field(min_length=1)
    path: str | None = None
    line: int | None = Field(default=None, ge=1)
    criterion_ids: tuple[str, ...] = ()

    @field_validator("path")
    @classmethod
    def require_safe_finding_path(cls, value: str | None) -> str | None:
        """Keep optional source locations repository-relative."""

        return None if value is None else _require_safe_relative_path(value)

    @field_validator("criterion_ids")
    @classmethod
    def require_clean_criterion_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Reject duplicate criterion references."""

        return _require_clean_unique_items(values)

    @model_validator(mode="after")
    def validate_finding(self) -> Self:
        """Keep locations and critical severity semantically coherent."""

        if self.line is not None and self.path is None:
            raise ValueError("a finding line requires a source path")
        if self.severity is ReviewSeverity.CRITICAL and not self.blocking:
            raise ValueError("critical review findings must be blocking")
        return self


class ReviewReport(IterationArtifact):
    """Independent semantic review of one immutable implementation commit."""

    kind: Literal[ArtifactKind.REVIEW_REPORT] = ArtifactKind.REVIEW_REPORT
    producer: Literal[AgentRole.REVIEWER] = AgentRole.REVIEWER
    input_commit: str = Field(pattern=COMMIT_PATTERN)
    verdict: ReviewVerdict
    findings: tuple[ReviewFinding, ...] = ()
    summary: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_verdict(self) -> Self:
        """Tie accept, revise, and fail recommendations to findings."""

        finding_ids = [finding.id for finding in self.findings]
        if len(finding_ids) != len(set(finding_ids)):
            raise ValueError("review finding IDs must be unique")
        blocking = [finding for finding in self.findings if finding.blocking]
        critical = [
            finding
            for finding in self.findings
            if finding.severity is ReviewSeverity.CRITICAL
        ]
        if self.verdict is ReviewVerdict.ACCEPT and blocking:
            raise ValueError("accepted reviews cannot contain blocking findings")
        if self.verdict is ReviewVerdict.REVISE and not blocking:
            raise ValueError("revision requires a blocking review finding")
        if self.verdict is ReviewVerdict.FAIL and not critical:
            raise ValueError("failed reviews require a critical finding")
        return self


class IterationRecord(IterationArtifact):
    """Controller-owned decision record for one implementation iteration."""

    kind: Literal[ArtifactKind.ITERATION_RECORD] = ArtifactKind.ITERATION_RECORD
    producer: Literal["controller"] = "controller"
    input_commit: str = Field(pattern=COMMIT_PATTERN)
    output_commit: str = Field(pattern=COMMIT_PATTERN)
    implementation_plan: ArtifactReference
    work_result: ArtifactReference
    test_report: ArtifactReference
    review_report: ArtifactReference
    decision: IterationDecision
    blocking_finding_ids: tuple[str, ...] = ()
    resolved_finding_ids: tuple[str, ...] = ()
    summary: str = Field(min_length=1)

    @field_validator("blocking_finding_ids", "resolved_finding_ids")
    @classmethod
    def require_clean_finding_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Reject duplicate finding references."""

        return _require_clean_unique_items(values)

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        """Require exact artifact kinds and evidence for revision or failure."""

        expected_kinds = {
            "implementation_plan": ArtifactKind.IMPLEMENTATION_PLAN,
            "work_result": ArtifactKind.WORK_RESULT,
            "test_report": ArtifactKind.TEST_REPORT,
            "review_report": ArtifactKind.REVIEW_REPORT,
        }
        for field, expected in expected_kinds.items():
            if getattr(self, field).kind is not expected:
                raise ValueError(f"{field} must reference {expected.value}")
        if self.input_commit == self.output_commit:
            raise ValueError("iteration output commit must differ from input")
        if self.decision is IterationDecision.ACCEPT and self.blocking_finding_ids:
            raise ValueError("accepted iterations cannot retain blocking findings")
        if self.decision is not IterationDecision.ACCEPT and not (
            self.blocking_finding_ids
        ):
            raise ValueError("revision and failure require blocking findings")
        overlap = set(self.blocking_finding_ids) & set(self.resolved_finding_ids)
        if overlap:
            raise ValueError("a finding cannot be both blocking and resolved")
        return self


class FinalReport(PhaseArtifact):
    """Controller-owned final delivery or failure report."""

    kind: Literal[ArtifactKind.FINAL_REPORT] = ArtifactKind.FINAL_REPORT
    producer: Literal["controller"] = "controller"
    status: FinalStatus
    termination_reason: str = Field(min_length=1)
    final_commit: str | None = Field(default=None, pattern=COMMIT_PATTERN)
    iterations: tuple[ArtifactReference, ...] = ()
    acceptance_results: tuple[CriterionResult, ...] = ()
    unresolved_findings: tuple[str, ...] = ()
    known_limitations: tuple[str, ...] = ()
    summary: str = Field(min_length=1)

    @field_validator("unresolved_findings", "known_limitations")
    @classmethod
    def require_clean_unique_text(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Reject blank and duplicate final-report statements."""

        return _require_clean_unique_items(values)

    @model_validator(mode="after")
    def validate_final_report(self) -> Self:
        """Tie a completed report to commit and acceptance evidence."""

        if any(
            reference.kind is not ArtifactKind.ITERATION_RECORD
            for reference in self.iterations
        ):
            raise ValueError("final report iterations must reference iteration records")
        paths = [reference.path for reference in self.iterations]
        if len(paths) != len(set(paths)):
            raise ValueError("final report iteration references must be unique")

        criterion_ids = [result.criterion_id for result in self.acceptance_results]
        if len(criterion_ids) != len(set(criterion_ids)):
            raise ValueError("final acceptance criterion IDs must be unique")
        if self.status is FinalStatus.COMPLETED:
            if self.final_commit is None:
                raise ValueError("completed final reports require a commit")
            if not self.iterations or not self.acceptance_results:
                raise ValueError(
                    "completed final reports require iteration and acceptance evidence"
                )
            if any(
                result.status is not CheckStatus.PASSED
                for result in self.acceptance_results
            ):
                raise ValueError("completed final reports require passed acceptance")
        return self


type PersistedArtifact = (
    ImplementationPlan
    | WorkResult
    | TestReport
    | ReviewReport
    | IterationRecord
    | FinalReport
)


ARTIFACT_MODELS: dict[ArtifactKind, type[PersistedArtifact]] = {
    ArtifactKind.IMPLEMENTATION_PLAN: ImplementationPlan,
    ArtifactKind.WORK_RESULT: WorkResult,
    ArtifactKind.TEST_REPORT: TestReport,
    ArtifactKind.REVIEW_REPORT: ReviewReport,
    ArtifactKind.ITERATION_RECORD: IterationRecord,
    ArtifactKind.FINAL_REPORT: FinalReport,
}


def parse_phase_artifact(payload: object) -> PersistedArtifact:
    """Dispatch an untrusted JSON payload to its one declared artifact model."""

    if not isinstance(payload, dict):
        raise ValueError("phase artifact must be a JSON object")
    try:
        kind = ArtifactKind(payload["kind"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("phase artifact requires a supported kind") from error
    model = ARTIFACT_MODELS.get(kind)
    if model is None:
        raise ValueError(f"unsupported phase artifact kind: {kind.value}")
    return model.model_validate(payload)


def validate_artifact_context(
    artifact: PersistedArtifact,
    *,
    task_brief: TaskBrief,
    team_id: str,
    team_roles: set[AgentRole],
    iteration_limit: int,
) -> None:
    """Validate one phase artifact against its frozen run inputs."""

    if artifact.run_id != task_brief.run_id:
        raise ValueError("artifact run ID does not match the task brief")
    if artifact.team_id != team_id:
        raise ValueError("artifact team ID does not match the selected team")
    if isinstance(artifact, IterationArtifact) and artifact.iteration > iteration_limit:
        raise ValueError("artifact iteration exceeds the run iteration limit")
    if isinstance(artifact.producer, AgentRole) and artifact.producer not in team_roles:
        raise ValueError("artifact producer is not part of the selected team")

    expected_criteria = {criterion.id for criterion in task_brief.acceptance_criteria}
    referenced_criteria: set[str] = set()
    if isinstance(artifact, ImplementationPlan):
        for task in artifact.tasks:
            if task.owner not in team_roles or task.owner not in IMPLEMENTATION_ROLES:
                raise ValueError("plan task owner is not an implementation team role")
            referenced_criteria.update(task.acceptance_criteria)
        if referenced_criteria != expected_criteria:
            raise ValueError(
                "implementation plan must cover every acceptance criterion exactly"
            )
    elif isinstance(artifact, TestReport):
        referenced_criteria = {
            criterion.criterion_id for criterion in artifact.criteria
        }
        if referenced_criteria != expected_criteria:
            raise ValueError(
                "test report must cover every confirmed acceptance criterion"
            )
    elif isinstance(artifact, ReviewReport):
        referenced_criteria = {
            criterion_id
            for finding in artifact.findings
            for criterion_id in finding.criterion_ids
        }
        if not referenced_criteria.issubset(expected_criteria):
            raise ValueError("review report references an unknown criterion")
    elif isinstance(artifact, FinalReport):
        referenced_criteria = {
            result.criterion_id for result in artifact.acceptance_results
        }
        if artifact.status is FinalStatus.COMPLETED:
            if referenced_criteria != expected_criteria:
                raise ValueError(
                    "completed final report must cover every acceptance criterion"
                )
        elif not referenced_criteria.issubset(expected_criteria):
            raise ValueError("final report references an unknown criterion")
