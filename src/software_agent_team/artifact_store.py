"""Immutable, integrity-checked persistence for phase artifacts."""

import hashlib
import json
import os
from collections.abc import Callable
from contextlib import suppress
from itertools import pairwise
from pathlib import Path, PurePosixPath
from typing import NamedTuple
from uuid import uuid4

from pydantic import ValidationError

from software_agent_team.artifacts import (
    ARTIFACT_MODELS,
    AgentExecutionRecord,
    ArtifactKind,
    ArtifactReference,
    CheckStatus,
    FinalReport,
    FinalStatus,
    HandoffEnvelope,
    HandoffStatus,
    ImplementationPlan,
    IterationArtifact,
    IterationDecision,
    IterationRecord,
    PersistedArtifact,
    PhaseArtifact,
    ReviewReport,
    ReviewVerdict,
    TaskBrief,
    TestReport,
    WorkResult,
    resolve_acceptance_results,
    validate_artifact_context,
)
from software_agent_team.budgets import (
    BUDGET_LEDGER_FILENAME,
    BudgetLedgerRecord,
    serialize_budget_ledger,
)
from software_agent_team.integrity import canonical_model_sha256
from software_agent_team.teams import AgentCapability, TeamPlan


class ArtifactStoreError(ValueError):
    """Base error for persisted phase artifacts."""


class ArtifactAlreadyExistsError(ArtifactStoreError):
    """Raised when a caller attempts to replace immutable evidence."""


class ArtifactIntegrityError(ArtifactStoreError):
    """Raised when an artifact is missing, altered, or contextually invalid."""


class ExecutionOutputEvidence(NamedTuple):
    """Canonical paths and digests for one captured Agent process."""

    stdout_path: str
    stderr_path: str
    stdout_sha256: str
    stderr_sha256: str


class FinalReportBundle(NamedTuple):
    """Paths and digests committed by one terminal evidence transaction."""

    reference: ArtifactReference
    markdown_path: str
    markdown_sha256: str
    budget_ledger_path: str
    budget_ledger_sha256: str


def _artifact_path(artifact: PersistedArtifact) -> PurePosixPath:
    if isinstance(artifact, HandoffEnvelope):
        target = (
            artifact.target_agent_id
            if artifact.target_agent_id is not None
            else "controller"
        )
        filename = (
            f"{artifact.sequence:02d}-{artifact.source_agent_id}-to-{target}.json"
        )
        return (
            PurePosixPath("iterations")
            / f"{artifact.iteration:02d}"
            / "handoffs"
            / artifact.stage
            / filename
        )
    if isinstance(artifact, AgentExecutionRecord):
        return _execution_directory(artifact) / f"{_execution_stem(artifact)}.json"
    if isinstance(artifact, ImplementationPlan):
        return PurePosixPath("implementation-plan.json")
    if isinstance(artifact, FinalReport):
        return PurePosixPath("final-report.json")
    if not isinstance(artifact, IterationArtifact):
        raise ArtifactStoreError(f"unsupported artifact model: {type(artifact)!r}")
    filename = f"{artifact.kind.value.replace('_', '-')}.json"
    base = PurePosixPath("iterations") / f"{artifact.iteration:02d}"
    if isinstance(artifact, IterationRecord):
        return base / filename
    return base / "agents" / artifact.producer / filename


def _execution_directory(record: AgentExecutionRecord) -> PurePosixPath:
    return (
        PurePosixPath("iterations")
        / f"{record.iteration:02d}"
        / "executions"
        / record.stage
    )


def _execution_stem(record: AgentExecutionRecord) -> str:
    return f"{record.agent_id}-attempt-{record.attempt:02d}"


def _execution_output_paths(
    record: AgentExecutionRecord,
) -> tuple[PurePosixPath, PurePosixPath]:
    return _execution_output_paths_for(
        iteration=record.iteration,
        stage=record.stage,
        agent_id=record.agent_id,
        attempt=record.attempt,
    )


def _execution_output_paths_for(
    *,
    iteration: int,
    stage: str,
    agent_id: str,
    attempt: int,
) -> tuple[PurePosixPath, PurePosixPath]:
    directory = PurePosixPath("iterations") / f"{iteration:02d}" / "executions" / stage
    stem = f"{agent_id}-attempt-{attempt:02d}"
    return directory / f"{stem}.stdout.txt", directory / f"{stem}.stderr.txt"


def _serialize(artifact: PersistedArtifact) -> bytes:
    payload = json.dumps(
        artifact.model_dump(mode="json"),
        ensure_ascii=False,
        indent=2,
    )
    return f"{payload}\n".encode()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class ArtifactStore:
    """Write-once store rooted in one persisted run directory."""

    def __init__(
        self,
        root: Path,
        *,
        task_brief: TaskBrief,
        team_plan: TeamPlan,
    ) -> None:
        if root.is_symlink() or not root.is_dir():
            raise ArtifactStoreError(
                "artifact store requires an existing run directory"
            )
        if not task_brief.confirmed:
            raise ArtifactStoreError("artifact store requires a confirmed task brief")
        if task_brief.run_id != team_plan.run_id:
            raise ArtifactStoreError("TeamPlan belongs to a different run")
        if canonical_model_sha256(task_brief) != team_plan.task_brief_sha256:
            raise ArtifactStoreError("TeamPlan binds a different task brief")
        self.root = root
        self.task_brief = task_brief
        self.team_plan = team_plan
        self.iteration_limit = team_plan.iteration_limit

    def write(
        self,
        artifact: PersistedArtifact,
        *,
        description: str = "",
    ) -> ArtifactReference:
        """Atomically create one canonical artifact and return its digest reference."""

        try:
            self._validate_context(artifact)
        except ValueError as error:
            raise ArtifactStoreError("artifact context is invalid") from error
        self._validate_references(artifact)
        relative_path = _artifact_path(artifact)
        destination = self.root.joinpath(*relative_path.parts)
        self._prepare_parent(relative_path.parent)

        content = _serialize(artifact)
        digest = hashlib.sha256(content).hexdigest()
        temporary = destination.parent / f".{destination.name}.{uuid4().hex}.tmp"
        try:
            with temporary.open("xb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            try:
                os.link(temporary, destination)
            except FileExistsError as error:
                raise ArtifactAlreadyExistsError(
                    f"artifact already exists: {relative_path.as_posix()}"
                ) from error
            _fsync_directory(destination.parent)
        finally:
            temporary.unlink(missing_ok=True)

        return ArtifactReference(
            kind=artifact.kind,
            path=relative_path.as_posix(),
            sha256=digest,
            description=description,
        )

    def write_execution_outputs(
        self,
        *,
        iteration: int,
        stage: str,
        agent_id: str,
        attempt: int,
        stdout: str,
        stderr: str,
    ) -> ExecutionOutputEvidence:
        """Persist one write-once stdout/stderr pair before its telemetry record."""

        if not 1 <= iteration <= self.iteration_limit:
            raise ArtifactStoreError("execution iteration is outside the run limit")
        if not 1 <= attempt <= 99:
            raise ArtifactStoreError("execution attempt must be between 1 and 99")
        stage_agents = self.team_plan.stage_agents.get(stage)
        if stage_agents is None or agent_id not in stage_agents:
            raise ArtifactStoreError("execution Agent is outside the declared stage")

        stdout_relative, stderr_relative = _execution_output_paths_for(
            iteration=iteration,
            stage=stage,
            agent_id=agent_id,
            attempt=attempt,
        )
        self._prepare_parent(stdout_relative.parent)
        stdout_content = stdout.encode("utf-8")
        stderr_content = stderr.encode("utf-8")
        stdout_destination = self.root.joinpath(*stdout_relative.parts)
        stderr_destination = self.root.joinpath(*stderr_relative.parts)
        stdout_created = False
        try:
            self._write_immutable_file(stdout_destination, stdout_content)
            stdout_created = True
            self._write_immutable_file(stderr_destination, stderr_content)
        except Exception:
            if stdout_created:
                stdout_destination.unlink(missing_ok=True)
                _fsync_directory(stdout_destination.parent)
            raise

        return ExecutionOutputEvidence(
            stdout_path=stdout_relative.as_posix(),
            stderr_path=stderr_relative.as_posix(),
            stdout_sha256=hashlib.sha256(stdout_content).hexdigest(),
            stderr_sha256=hashlib.sha256(stderr_content).hexdigest(),
        )

    def write_final_report_markdown(
        self,
        final_report: ArtifactReference,
        content: str,
    ) -> str:
        """Write a derived human report only after its JSON authority exists."""

        if final_report.kind is not ArtifactKind.FINAL_REPORT:
            raise ArtifactStoreError("human report requires a final-report reference")
        loaded = self.load(final_report)
        if not isinstance(loaded, FinalReport):
            raise ArtifactStoreError("human report authority has the wrong type")
        if not content.strip() or "\x00" in content:
            raise ArtifactStoreError("human report content is invalid")
        relative_path = PurePosixPath("final-report.md")
        destination = self.root / relative_path.as_posix()
        self._write_immutable_file(destination, content.encode("utf-8"))
        return relative_path.as_posix()

    def write_final_report_bundle(
        self,
        report: FinalReport,
        markdown: str,
        budget_ledger: BudgetLedgerRecord,
        *,
        description: str,
    ) -> FinalReportBundle:
        """Commit terminal JSON, Markdown, and budget evidence as one bundle."""

        try:
            self._validate_context(report)
        except ValueError as error:
            raise ArtifactStoreError("artifact context is invalid") from error
        self._validate_references(report)
        if not markdown.strip() or "\x00" in markdown:
            raise ArtifactStoreError("human report content is invalid")

        report_path = PurePosixPath("final-report.json")
        markdown_path = PurePosixPath("final-report.md")
        budget_path = PurePosixPath(BUDGET_LEDGER_FILENAME)
        entries = (
            (report_path, _serialize(report)),
            (markdown_path, markdown.encode("utf-8")),
            (budget_path, serialize_budget_ledger(budget_ledger)),
        )
        destinations = tuple(
            (relative, self.root.joinpath(*relative.parts), content)
            for relative, content in entries
        )
        for relative, destination, _content in destinations:
            self._require_inside_root(destination, must_exist=False)
            if destination.exists() or destination.is_symlink():
                raise ArtifactAlreadyExistsError(
                    f"artifact already exists: {relative.as_posix()}"
                )

        staged: list[tuple[Path, Path]] = []
        created: list[Path] = []
        try:
            for relative, destination, content in destinations:
                temporary = self.root / f".{relative.name}.{uuid4().hex}.tmp"
                with temporary.open("xb") as output:
                    output.write(content)
                    output.flush()
                    os.fsync(output.fileno())
                staged.append((temporary, destination))
            for temporary, destination in staged:
                os.link(temporary, destination)
                created.append(destination)
            _fsync_directory(self.root)
        except Exception:
            for destination in reversed(created):
                destination.unlink(missing_ok=True)
            if created:
                _fsync_directory(self.root)
            raise
        finally:
            for temporary, _destination in staged:
                temporary.unlink(missing_ok=True)

        report_content = entries[0][1]
        budget_content = entries[2][1]
        return FinalReportBundle(
            reference=ArtifactReference(
                kind=ArtifactKind.FINAL_REPORT,
                path=report_path.as_posix(),
                sha256=hashlib.sha256(report_content).hexdigest(),
                description=description,
            ),
            markdown_path=markdown_path.as_posix(),
            markdown_sha256=hashlib.sha256(entries[1][1]).hexdigest(),
            budget_ledger_path=budget_path.as_posix(),
            budget_ledger_sha256=hashlib.sha256(budget_content).hexdigest(),
        )

    def rollback_final_report_bundle(self, bundle: FinalReportBundle) -> None:
        """Remove an uncommitted terminal bundle only when every digest matches."""

        expected = (
            (bundle.reference.path, bundle.reference.sha256),
            (bundle.markdown_path, bundle.markdown_sha256),
            (bundle.budget_ledger_path, bundle.budget_ledger_sha256),
        )
        destinations: list[Path] = []
        for relative, digest in expected:
            destination = self.root.joinpath(*PurePosixPath(relative).parts)
            self._require_inside_root(destination, must_exist=True)
            if destination.is_symlink() or not destination.is_file():
                raise ArtifactIntegrityError(
                    "terminal rollback target is not a regular file"
                )
            if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
                raise ArtifactIntegrityError(
                    "terminal rollback target differs from the prepared bundle"
                )
            destinations.append(destination)
        for destination in reversed(destinations):
            destination.unlink()
        _fsync_directory(self.root)

    def bind_final_report_bundle[ResultT](
        self,
        bundle: FinalReportBundle,
        transition: Callable[[ArtifactReference], ResultT],
    ) -> ResultT:
        """Bind a prepared bundle to controller state or roll it back exactly."""

        try:
            return transition(bundle.reference)
        except BaseException as error:
            try:
                self.rollback_final_report_bundle(bundle)
            except Exception as rollback_error:
                error.add_note(
                    f"terminal bundle rollback also failed: {rollback_error}"
                )
            raise

    def load(self, reference: ArtifactReference) -> PersistedArtifact:
        """Load one referenced artifact after path, digest, type, and context checks."""

        model = ARTIFACT_MODELS.get(reference.kind)
        if model is None:
            raise ArtifactIntegrityError(
                f"unsupported persisted artifact kind: {reference.kind.value}"
            )

        relative_path = PurePosixPath(reference.path)
        destination = self.root.joinpath(*relative_path.parts)
        self._require_inside_root(destination, must_exist=True)
        if destination.is_symlink() or not destination.is_file():
            raise ArtifactIntegrityError("artifact reference is not a regular file")
        try:
            content = destination.read_bytes()
        except OSError as error:
            raise ArtifactIntegrityError(
                f"cannot read artifact: {reference.path}"
            ) from error
        if hashlib.sha256(content).hexdigest() != reference.sha256:
            raise ArtifactIntegrityError(
                f"artifact digest does not match: {reference.path}"
            )
        try:
            artifact = model.model_validate_json(content)
        except ValidationError as error:
            raise ArtifactIntegrityError(
                f"artifact content is invalid: {reference.path}"
            ) from error
        if _artifact_path(artifact) != relative_path:
            raise ArtifactIntegrityError(
                f"artifact is not stored at its canonical path: {reference.path}"
            )
        try:
            self._validate_context(artifact)
            self._validate_references(artifact)
        except ArtifactIntegrityError:
            raise
        except ValueError as error:
            raise ArtifactIntegrityError(
                f"artifact context is invalid: {reference.path}"
            ) from error
        return artifact

    def _validate_context(self, artifact: PersistedArtifact) -> None:
        validate_artifact_context(
            artifact,
            task_brief=self.task_brief,
            team_id=self.team_plan.team_id,
            team_agents=self.team_plan.agent_capabilities,
            iteration_limit=self.iteration_limit,
            team_stages=self.team_plan.stage_agents,
        )

    def _validate_references(self, artifact: PersistedArtifact) -> None:
        if isinstance(artifact, HandoffEnvelope):
            if artifact.status is HandoffStatus.COMPLETED and not artifact.artifacts:
                raise ArtifactStoreError(
                    "completed handoffs require a persisted source artifact"
                )
            loaded_artifacts = [
                self.load(reference) for reference in artifact.artifacts
            ]
            for loaded in loaded_artifacts:
                if (
                    isinstance(loaded, IterationArtifact)
                    and not isinstance(loaded, ImplementationPlan)
                    and loaded.iteration != artifact.iteration
                ):
                    raise ArtifactStoreError(
                        "handoff references must use the envelope's iteration"
                    )
            if artifact.status is HandoffStatus.COMPLETED and not any(
                isinstance(loaded, PhaseArtifact)
                and loaded.producer == artifact.source_agent_id
                for loaded in loaded_artifacts
            ):
                raise ArtifactStoreError(
                    "completed handoffs require evidence produced by the source Agent"
                )

        elif isinstance(artifact, AgentExecutionRecord):
            expected_stdout, expected_stderr = _execution_output_paths(artifact)
            if PurePosixPath(artifact.stdout_path) != expected_stdout:
                raise ArtifactStoreError(
                    "execution stdout is not stored at its canonical path"
                )
            if PurePosixPath(artifact.stderr_path) != expected_stderr:
                raise ArtifactStoreError(
                    "execution stderr is not stored at its canonical path"
                )
            self._validate_file_digest(
                artifact.stdout_path,
                artifact.stdout_sha256,
                label="execution stdout",
            )
            self._validate_file_digest(
                artifact.stderr_path,
                artifact.stderr_sha256,
                label="execution stderr",
            )
            if artifact.response_artifact is not None:
                response = self.load(artifact.response_artifact)
                if not isinstance(response, PhaseArtifact):
                    raise ArtifactStoreError(
                        "execution response must reference a phase artifact"
                    )
                if response.producer != artifact.agent_id:
                    raise ArtifactStoreError(
                        "execution response producer does not match the Agent"
                    )
                if isinstance(response, IterationArtifact) and (
                    response.iteration != artifact.iteration
                ):
                    raise ArtifactStoreError(
                        "execution response must use the execution iteration"
                    )

        elif isinstance(artifact, IterationRecord):
            if artifact.implementation_plan is not None:
                plan = self.load(artifact.implementation_plan)
                if not isinstance(plan, ImplementationPlan):
                    raise ArtifactStoreError(
                        "iteration plan reference has the wrong type"
                    )
            elif artifact.implementation_plan_sha256 != (
                self.team_plan.implementation_plan_sha256
            ):
                raise ArtifactStoreError(
                    "iteration plan digest differs from the approved TeamPlan"
                )

            works = [self.load(reference) for reference in artifact.work_results]
            tests = [self.load(reference) for reference in artifact.test_reports]
            reviews = [self.load(reference) for reference in artifact.review_reports]
            if not all(isinstance(item, WorkResult) for item in works):
                raise ArtifactStoreError("iteration work reference has the wrong type")
            if not all(isinstance(item, TestReport) for item in tests):
                raise ArtifactStoreError("iteration test reference has the wrong type")
            if not all(isinstance(item, ReviewReport) for item in reviews):
                raise ArtifactStoreError(
                    "iteration review reference has the wrong type"
                )
            typed_works = [item for item in works if isinstance(item, WorkResult)]
            typed_tests = [item for item in tests if isinstance(item, TestReport)]
            typed_reviews = [item for item in reviews if isinstance(item, ReviewReport)]
            expected_work_producers = {
                agent.id
                for agent in self.team_plan.agents
                if agent.capability
                in {AgentCapability.IMPLEMENTATION, AgentCapability.INTEGRATION}
            }
            expected_test_producers = {
                agent.id
                for agent in self.team_plan.agents
                if agent.capability is AgentCapability.TESTING
            } or {"controller"}
            expected_review_producers = {
                agent.id
                for agent in self.team_plan.agents
                if agent.capability is AgentCapability.REVIEW
            }
            actual_work_producers = {item.producer for item in typed_works}
            actual_test_producers = {item.producer for item in typed_tests}
            actual_review_producers = {item.producer for item in typed_reviews}
            if actual_work_producers != expected_work_producers:
                raise ArtifactStoreError(
                    "iteration work evidence must cover every approved writer"
                )
            if actual_test_producers != expected_test_producers:
                raise ArtifactStoreError(
                    "iteration test evidence must cover every approved tester"
                )
            if actual_review_producers != expected_review_producers:
                raise ArtifactStoreError(
                    "iteration review evidence must cover every approved reviewer"
                )
            if any(
                item.iteration != artifact.iteration
                for item in (*typed_works, *typed_tests, *typed_reviews)
            ):
                raise ArtifactStoreError(
                    "iteration references must use the record's iteration"
                )

            expected_input = artifact.input_commit
            for work in typed_works:
                if work.input_commit != expected_input:
                    raise ArtifactStoreError(
                        "iteration work results must form one commit chain"
                    )
                expected_input = work.output_commit
            if expected_input != artifact.output_commit or any(
                item.input_commit != artifact.output_commit
                for item in (*typed_tests, *typed_reviews)
            ):
                raise ArtifactStoreError(
                    "iteration commits do not match referenced evidence"
                )

            first_test = typed_tests[0]
            deterministic_evidence = (
                first_test.status,
                first_test.commands,
                first_test.criteria,
                first_test.manual_review_criteria,
                first_test.blockers,
            )
            if any(
                (
                    test.status,
                    test.commands,
                    test.criteria,
                    test.manual_review_criteria,
                    test.blockers,
                )
                != deterministic_evidence
                for test in typed_tests[1:]
            ):
                raise ArtifactStoreError(
                    "iteration test reports disagree on controller-owned evidence"
                )

            reviewed_criteria = {
                criterion_id
                for review in typed_reviews
                for criterion_id in review.reviewed_criteria
            }
            if reviewed_criteria != set(first_test.manual_review_criteria):
                raise ArtifactStoreError(
                    "iteration reviews must exactly cover manual criteria"
                )
            finding_ids = [
                finding.id for review in typed_reviews for finding in review.findings
            ]
            if len(finding_ids) != len(set(finding_ids)):
                raise ArtifactStoreError(
                    "review finding IDs must be unique across the iteration"
                )
            blocking_ids = {
                finding.id
                for review in typed_reviews
                for finding in review.findings
                if finding.blocking
            }
            if set(artifact.blocking_finding_ids) != blocking_ids:
                raise ArtifactStoreError(
                    "iteration blocking findings must match all review reports"
                )
            if artifact.decision is IterationDecision.ACCEPT:
                if first_test.status is not CheckStatus.PASSED or any(
                    review.verdict is not ReviewVerdict.ACCEPT
                    for review in typed_reviews
                ):
                    raise ArtifactStoreError(
                        "accepted iteration requires all quality evidence to pass"
                    )
                try:
                    resolve_acceptance_results(first_test, tuple(typed_reviews))
                except ValueError as error:
                    raise ArtifactStoreError(
                        "accepted iteration has unresolved manual criteria"
                    ) from error
            elif artifact.decision is IterationDecision.REVISE:
                if any(
                    review.verdict is ReviewVerdict.FAIL for review in typed_reviews
                ) or (
                    first_test.status is CheckStatus.PASSED
                    and all(
                        review.verdict is ReviewVerdict.ACCEPT
                        for review in typed_reviews
                    )
                ):
                    raise ArtifactStoreError(
                        "revision decision does not match test and review evidence"
                    )
            elif first_test.status is CheckStatus.PASSED and all(
                review.verdict is ReviewVerdict.ACCEPT for review in typed_reviews
            ):
                raise ArtifactStoreError(
                    "failure decision cannot replace an acceptance decision"
                )

        elif isinstance(artifact, FinalReport) and artifact.iterations:
            records = [self.load(reference) for reference in artifact.iterations]
            if not all(isinstance(record, IterationRecord) for record in records):
                raise ArtifactStoreError(
                    "final report must reference iteration records"
                )
            iteration_records = [
                record for record in records if isinstance(record, IterationRecord)
            ]
            if [record.iteration for record in iteration_records] != list(
                range(1, len(iteration_records) + 1)
            ):
                raise ArtifactStoreError(
                    "final report iterations must be contiguous and ordered"
                )
            for previous, current in pairwise(iteration_records):
                if current.input_commit != previous.output_commit:
                    raise ArtifactStoreError(
                        "final report iteration commits must form one chain"
                    )

            last_record = iteration_records[-1]
            last_tests = [
                self.load(reference) for reference in last_record.test_reports
            ]
            last_reviews = [
                self.load(reference) for reference in last_record.review_reports
            ]
            if not all(isinstance(item, TestReport) for item in last_tests):
                raise ArtifactStoreError("final iteration test reference is invalid")
            if not all(isinstance(item, ReviewReport) for item in last_reviews):
                raise ArtifactStoreError("final iteration review reference is invalid")
            typed_last_tests = [
                item for item in last_tests if isinstance(item, TestReport)
            ]
            typed_last_reviews = [
                item for item in last_reviews if isinstance(item, ReviewReport)
            ]
            if artifact.status is FinalStatus.COMPLETED:
                try:
                    expected_results = resolve_acceptance_results(
                        typed_last_tests[0],
                        tuple(typed_last_reviews),
                    )
                except ValueError as error:
                    raise ArtifactStoreError(
                        "completed final report has unresolved manual criteria"
                    ) from error
                if (
                    last_record.decision is not IterationDecision.ACCEPT
                    or artifact.final_commit != last_record.output_commit
                    or artifact.acceptance_results != expected_results
                ):
                    raise ArtifactStoreError(
                        "completed final report does not match final iteration evidence"
                    )

    def _validate_file_digest(self, path: str, digest: str, *, label: str) -> None:
        relative_path = PurePosixPath(path)
        destination = self.root.joinpath(*relative_path.parts)
        self._require_inside_root(destination, must_exist=True)
        if destination.is_symlink() or not destination.is_file():
            raise ArtifactIntegrityError(f"{label} is not a regular file")
        try:
            content = destination.read_bytes()
        except OSError as error:
            raise ArtifactIntegrityError(f"cannot read {label}") from error
        if hashlib.sha256(content).hexdigest() != digest:
            raise ArtifactIntegrityError(f"{label} digest does not match")

    def _require_inside_root(self, path: Path, *, must_exist: bool) -> None:
        try:
            resolved_root = self.root.resolve(strict=True)
            resolved_path = path.resolve(strict=must_exist)
        except OSError as error:
            raise ArtifactIntegrityError("artifact path does not exist") from error
        if not resolved_path.is_relative_to(resolved_root):
            raise ArtifactIntegrityError("artifact path escapes the run directory")

    def _prepare_parent(self, relative_parent: PurePosixPath) -> None:
        current = self.root
        for part in relative_parent.parts:
            current = current / part
            with suppress(FileExistsError):
                current.mkdir()
            if current.is_symlink() or not current.is_dir():
                raise ArtifactIntegrityError(
                    "artifact parent must be a real directory inside the run"
                )
            self._require_inside_root(current, must_exist=True)

    @staticmethod
    def _write_immutable_file(destination: Path, content: bytes) -> None:
        temporary = destination.parent / f".{destination.name}.{uuid4().hex}.tmp"
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "wb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            try:
                os.link(temporary, destination)
            except FileExistsError as error:
                raise ArtifactAlreadyExistsError(
                    f"artifact already exists: {destination.name}"
                ) from error
            _fsync_directory(destination.parent)
        finally:
            temporary.unlink(missing_ok=True)
