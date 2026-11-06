"""Planning evidence filesystem storage with immutable turns and hash chains.

Layout per run directory:
    <root>/<run_id>/
    ├── request.json       # Immutable initial PlanningRequest snapshot
    ├── session.json       # Session state index with turn_head_sha256 digest
    ├── turns/
    │   ├── 001.json       # Append-only hash-chained PlanningTurn records
    │   └── 002.json
    ├── proposals/
    │   └── 001.json       # Numbered proposal revisions
    └── approvals/
        └── 001.json       # User approval binding to a specific proposal revision

Durability guarantees:
    - O_CREAT | O_EXCL prevents accidental record overwriting.
    - Temporary file write + fsync + atomic rename for session.json.
    - Directory fsync ensures directory entry persistence on POSIX filesystems.
    - Symlink detection prevents filesystem escape or redirection attacks.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel

from software_agent_team.artifacts import RuntimeRejectionEvidence
from software_agent_team.budgets import (
    AgentBudgetUsage,
    ModelCallCostRecord,
)
from software_agent_team.execution import (
    AgentExecutionResult,
    AgentExecutionStatus,
)
from software_agent_team.integrity import canonical_model_sha256
from software_agent_team.model_metadata import ModelMetadataSource
from software_agent_team.planning.common import (
    PLANNING_SCHEMA_VERSION,
    _digest_text,
    _utc,
)
from software_agent_team.planning.invariants import (
    PlanningError,
    PlanningIntegrityError,
)
from software_agent_team.planning.models_basic import (
    PlanningProposalSource,
    PlanningRecoveryAction,
    PlanningResponseKind,
    PlanningSessionStatus,
)
from software_agent_team.planning.proposals import PlanningModelResponse
from software_agent_team.planning.questions import (
    PlanningQuestionAdmission,
    PlanningRequest,
)
from software_agent_team.planning.session import (
    PlanningApproval,
    PlanningExecutionEvidence,
    PlanningProposal,
    PlanningSession,
    PlanningTurn,
)
from software_agent_team.response_corrections import (
    ResponseValidationDiagnostic,
    SemanticCorrectionOutcome,
    SemanticCorrectionRequestEvidence,
)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class PlanningStore:
    """Write-once turns/proposals with an atomic, integrity-checked session index."""

    def __init__(self, root: Path) -> None:
        if not root.is_absolute():
            raise PlanningError("Planning store root must be absolute")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if root.is_symlink() or not root.is_dir():
            raise PlanningError("Planning store root must be a real directory")
        root.chmod(0o700)
        self.root = root

    def _directory(self, run_id: str) -> Path:
        if re.fullmatch(r"[a-z0-9][a-z0-9_-]*", run_id) is None:
            raise PlanningError("Planning run ID is invalid")
        return self.root / run_id

    @staticmethod
    def _indexed_files(directory: Path) -> set[int]:
        if not directory.exists():
            return set()
        if directory.is_symlink() or not directory.is_dir():
            raise PlanningIntegrityError(
                f"Planning evidence path is not a real directory: {directory}"
            )
        indexes: set[int] = set()
        for path in directory.iterdir():
            if path.is_symlink() or not path.is_file():
                raise PlanningIntegrityError(
                    f"Planning evidence entry is not a regular file: {path}"
                )
            match = re.fullmatch(r"([0-9]{3,})\.json", path.name)
            if match is None:
                raise PlanningIntegrityError(
                    f"unexpected Planning evidence file: {path.name}"
                )
            indexes.add(int(match.group(1)))
        return indexes

    def _write_once(self, destination: Path, model: BaseModel) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if destination.exists() or destination.is_symlink():
            raise PlanningIntegrityError(
                f"Planning evidence already exists: {destination}"
            )
        content = (json.dumps(model.model_dump(mode="json"), indent=2) + "\n").encode()
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        _fsync_directory(destination.parent)

    def _write_session(self, session: PlanningSession) -> None:
        directory = self._directory(session.run_id)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = directory / "session.json"
        if destination.is_symlink():
            raise PlanningIntegrityError("Planning session index cannot be a symlink")
        content = (
            json.dumps(session.model_dump(mode="json"), indent=2) + "\n"
        ).encode()
        temporary = directory / f".session.{uuid4().hex}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, destination)
            _fsync_directory(directory)
        finally:
            if temporary.exists():
                temporary.unlink()

    def create(self, request: PlanningRequest) -> PlanningSession:
        directory = self._directory(request.run_id)
        if directory.exists() or directory.is_symlink():
            raise PlanningIntegrityError(
                f"Planning session already exists: {request.run_id}"
            )
        directory.mkdir(parents=True, mode=0o700)
        self._write_once(directory / "request.json", request)
        session = PlanningSession(
            run_id=request.run_id,
            request_sha256=canonical_model_sha256(request),
            status=PlanningSessionStatus.AUTHORIZED,
            created_at=request.authorized_at,
            updated_at=request.authorized_at,
        )
        self._write_session(session)
        return session

    def load_request(self, run_id: str) -> PlanningRequest:
        path = self._directory(run_id) / "request.json"
        if path.is_symlink() or not path.is_file():
            raise PlanningIntegrityError("Planning request evidence is missing")
        request = PlanningRequest.model_validate_json(path.read_text(encoding="utf-8"))
        if request.run_id != run_id:
            raise PlanningIntegrityError("Planning request belongs to a different run")
        return request

    def load_session(self, run_id: str, *, verify: bool = True) -> PlanningSession:
        path = self._directory(run_id) / "session.json"
        if path.is_symlink() or not path.is_file():
            raise PlanningIntegrityError("Planning session index is missing")
        session = PlanningSession.model_validate_json(path.read_text(encoding="utf-8"))
        if session.run_id != run_id:
            raise PlanningIntegrityError("Planning session belongs to a different run")
        if canonical_model_sha256(self.load_request(run_id)) != session.request_sha256:
            raise PlanningIntegrityError("Planning request digest changed")
        if verify:
            turn_indexes = self._indexed_files(self._directory(run_id) / "turns")
            if turn_indexes != set(range(1, session.turn_count + 1)):
                raise PlanningIntegrityError(
                    "Planning turn files differ from the session anchor"
                )
            previous: str | None = None
            for sequence in range(1, session.turn_count + 1):
                turn = self.load_turn(run_id, sequence)
                if turn.previous_sha256 != previous:
                    raise PlanningIntegrityError("Planning turn hash chain is broken")
                previous = canonical_model_sha256(turn)
            if previous != session.turn_head_sha256:
                raise PlanningIntegrityError("Planning turn head digest changed")
            proposal_count = session.latest_proposal_revision or 0
            proposal_indexes = self._indexed_files(
                self._directory(run_id) / "proposals"
            )
            if proposal_indexes != set(range(1, proposal_count + 1)):
                raise PlanningIntegrityError(
                    "Planning proposal files differ from the session anchor"
                )
            for revision in range(1, proposal_count + 1):
                self.load_proposal(run_id, revision)
            approval_indexes = self._indexed_files(
                self._directory(run_id) / "approvals"
            )
            if session.approved_revision is not None:
                if approval_indexes != {session.approved_revision}:
                    raise PlanningIntegrityError(
                        "Planning approval files differ from the session anchor"
                    )
                self.load_approval(run_id, session.approved_revision)
            elif approval_indexes:
                raise PlanningIntegrityError(
                    "unapproved Planning session contains approval evidence"
                )
        return session

    def load_turn(self, run_id: str, sequence: int) -> PlanningTurn:
        path = self._directory(run_id) / "turns" / f"{sequence:03d}.json"
        if path.is_symlink() or not path.is_file():
            raise PlanningIntegrityError(f"Planning turn {sequence} is missing")
        turn = PlanningTurn.model_validate_json(path.read_text(encoding="utf-8"))
        if turn.run_id != run_id or turn.sequence != sequence:
            raise PlanningIntegrityError(
                "Planning turn context does not match its path"
            )
        return turn

    def load_user_revision_inputs(
        self, request: PlanningRequest, *, through_revision: int | None = None
    ) -> tuple[str, ...]:
        """Recover accepted user revisions from the verified, run-local history."""

        session = self.load_session(request.run_id)
        if canonical_model_sha256(request) != session.request_sha256:
            raise PlanningIntegrityError("Planning request differs from its session")
        inputs: list[str] = []
        previous_source_turn = 0
        latest = session.latest_proposal_revision or 0
        if through_revision is not None:
            latest = min(latest, through_revision)
        for revision in range(1, latest + 1):
            proposal = self.load_proposal(request.run_id, revision)
            if proposal.source is not PlanningProposalSource.MODEL:
                continue
            assert proposal.source_turn_sequence is not None
            source_sequence = proposal.source_turn_sequence
            if not previous_source_turn < source_sequence <= session.turn_count:
                raise PlanningIntegrityError("Planning proposal source order changed")
            self._validate_proposal_source(proposal)
            change_request = proposal.change_request
            if change_request is not None:
                if not any(
                    self.load_turn(request.run_id, sequence).user_message
                    == change_request
                    for sequence in range(previous_source_turn + 1, source_sequence + 1)
                ):
                    raise PlanningIntegrityError(
                        "Planning revision source does not match persisted user input"
                    )
                inputs.append(change_request)
            previous_source_turn = source_sequence
        return tuple(inputs)

    def append_turn(
        self,
        *,
        run_id: str,
        user_message: str,
        prompt: str,
        result: AgentExecutionResult,
        parsed_response: PlanningModelResponse | None,
        question_admission: PlanningQuestionAdmission | None,
        response_normalizations: tuple[str, ...],
        validation_error: str | None,
        now: datetime,
        response_validation: ResponseValidationDiagnostic | None = None,
        semantic_correction_request: SemanticCorrectionRequestEvidence | None = None,
        semantic_correction_outcome: SemanticCorrectionOutcome | None = None,
        estimated_cost_usd: Decimal | None = None,
        pricing_source: ModelMetadataSource | None = None,
        budget_usage: AgentBudgetUsage | None = None,
        budget_error: str | None = None,
        cost_record: ModelCallCostRecord | None = None,
        recovery_action: PlanningRecoveryAction | None = None,
    ) -> PlanningTurn:
        session = self.load_session(run_id)
        if session.status in {
            PlanningSessionStatus.APPROVED,
            PlanningSessionStatus.CANCELLED,
            PlanningSessionStatus.FAILED,
        }:
            raise PlanningError("terminal Planning session cannot accept another turn")
        sequence = session.turn_count + 1
        response_text = result.response_text
        usage = result.telemetry.usage
        turn = PlanningTurn(
            run_id=run_id,
            sequence=sequence,
            previous_sha256=session.turn_head_sha256,
            user_message=user_message,
            prompt=prompt,
            prompt_sha256=_digest_text(prompt),
            response_text=response_text,
            response_sha256=(
                None if response_text is None else _digest_text(response_text)
            ),
            submission_payload=(
                None
                if result.semantic_submission is None
                else result.semantic_submission.payload
            ),
            submission_evidence=result.submission_evidence,
            parsed_response=parsed_response,
            question_admission=question_admission,
            response_normalizations=response_normalizations,
            validation_error=validation_error,
            response_validation=response_validation,
            semantic_correction_request=semantic_correction_request,
            semantic_correction_outcome=semantic_correction_outcome,
            recovery_action=recovery_action,
            execution=PlanningExecutionEvidence(
                status=result.status,
                session_key=result.telemetry.session_key,
                started_at=result.telemetry.started_at,
                finished_at=result.telemetry.finished_at,
                duration_ms=result.telemetry.duration_ms,
                provider=result.telemetry.provider,
                model=result.telemetry.model,
                input_tokens=None if usage is None else usage.input_tokens,
                output_tokens=None if usage is None else usage.output_tokens,
                estimated_cost_usd=estimated_cost_usd,
                pricing_source=pricing_source,
                budget_usage=budget_usage,
                budget_error=budget_error,
                cost_record=cost_record,
                provider_liveness=result.telemetry.provider_liveness,
                tool_evidence_status=result.telemetry.tool_evidence_status,
                session_transcript_sha256=(result.telemetry.session_transcript_sha256),
                session_record_count=result.telemetry.session_record_count,
                tool_evidence_error=result.telemetry.tool_evidence_error,
                invocation_lifecycle=result.telemetry.invocation_lifecycle,
                runtime_rejection_evidence=(
                    RuntimeRejectionEvidence(
                        transcript_sha256=result.telemetry.session_transcript_sha256,
                        record_count=result.telemetry.session_record_count,
                        rejections=result.telemetry.runtime_rejections,
                    )
                    if result.telemetry.runtime_rejections
                    else None
                ),
                error=result.error,
            ),
        )
        self._write_once(
            self._directory(run_id) / "turns" / f"{sequence:03d}.json",
            turn,
        )
        self._write_session(
            session.model_copy(
                update={
                    "schema_version": PLANNING_SCHEMA_VERSION,
                    "status": (
                        PlanningSessionStatus.CANCELLED
                        if result.status is AgentExecutionStatus.INTERRUPTED
                        else PlanningSessionStatus.CLARIFYING
                        if result.status is AgentExecutionStatus.COMPLETED
                        # A failed turn stays open only when the Controller has
                        # already selected one bounded, persisted recovery action.
                        or recovery_action is not None
                        else PlanningSessionStatus.FAILED
                    ),
                    "updated_at": _utc(now),
                    "turn_count": sequence,
                    "turn_head_sha256": canonical_model_sha256(turn),
                }
            )
        )
        return turn

    def _validate_proposal_source(self, proposal: PlanningProposal) -> None:
        if proposal.source_turn_sequence is not None:
            turn = self.load_turn(proposal.run_id, proposal.source_turn_sequence)
            if (
                turn.parsed_response is None
                or turn.parsed_response.kind is not PlanningResponseKind.PROPOSAL
                or turn.parsed_response.proposal != proposal.body
            ):
                raise PlanningIntegrityError("proposal does not match its model turn")

    def append_proposal(self, proposal: PlanningProposal, *, now: datetime) -> None:
        session = self.load_session(proposal.run_id)
        expected = (session.latest_proposal_revision or 0) + 1
        if proposal.revision != expected:
            raise PlanningIntegrityError(
                f"proposal revision must be {expected}, got {proposal.revision}"
            )
        self._validate_proposal_source(proposal)
        self._write_once(
            self._directory(proposal.run_id)
            / "proposals"
            / f"{proposal.revision:03d}.json",
            proposal,
        )
        self._write_session(
            session.model_copy(
                update={
                    "status": PlanningSessionStatus.PROPOSED,
                    "updated_at": _utc(now),
                    "latest_proposal_revision": proposal.revision,
                }
            )
        )

    def load_proposal(self, run_id: str, revision: int) -> PlanningProposal:
        path = self._directory(run_id) / "proposals" / f"{revision:03d}.json"
        if path.is_symlink() or not path.is_file():
            raise PlanningIntegrityError(f"Planning proposal {revision} is missing")
        proposal = PlanningProposal.model_validate_json(
            path.read_text(encoding="utf-8")
        )
        if proposal.run_id != run_id or proposal.revision != revision:
            raise PlanningIntegrityError(
                "Planning proposal context does not match its path"
            )
        return proposal

    def approve(self, approval: PlanningApproval, *, now: datetime) -> None:
        session = self.load_session(approval.run_id)
        if session.status is not PlanningSessionStatus.PROPOSED:
            raise PlanningError("only a proposed Planning session can be approved")
        if approval.revision != session.latest_proposal_revision:
            raise PlanningIntegrityError("approval must bind the latest proposal")
        proposal = self.load_proposal(approval.run_id, approval.revision)
        if canonical_model_sha256(proposal) != approval.proposal_sha256:
            raise PlanningIntegrityError("approval proposal digest does not match")
        self._write_once(
            self._directory(approval.run_id)
            / "approvals"
            / f"{approval.revision:03d}.json",
            approval,
        )
        self._write_session(
            session.model_copy(
                update={
                    "status": PlanningSessionStatus.APPROVED,
                    "updated_at": _utc(now),
                    "approved_revision": approval.revision,
                }
            )
        )

    def load_approval(self, run_id: str, revision: int) -> PlanningApproval:
        path = self._directory(run_id) / "approvals" / f"{revision:03d}.json"
        if path.is_symlink() or not path.is_file():
            raise PlanningIntegrityError(f"Planning approval {revision} is missing")
        approval = PlanningApproval.model_validate_json(
            path.read_text(encoding="utf-8")
        )
        if approval.run_id != run_id or approval.revision != revision:
            raise PlanningIntegrityError(
                "Planning approval context does not match its path"
            )
        return approval

    def cancel(self, run_id: str, *, now: datetime) -> None:
        session = self.load_session(run_id)
        if session.status is PlanningSessionStatus.APPROVED:
            raise PlanningError("approved Planning session cannot be cancelled")
        self._write_session(
            session.model_copy(
                update={
                    "status": PlanningSessionStatus.CANCELLED,
                    "updated_at": _utc(now),
                }
            )
        )

    def fail(self, run_id: str, *, now: datetime) -> None:
        """Persist a terminal coordinator failure without rewriting its turns."""

        session = self.load_session(run_id)
        if session.status is PlanningSessionStatus.FAILED:
            return
        if session.status in {
            PlanningSessionStatus.APPROVED,
            PlanningSessionStatus.CANCELLED,
        }:
            raise PlanningError("terminal Planning session cannot be failed")
        self._write_session(
            session.model_copy(
                update={
                    "schema_version": PLANNING_SCHEMA_VERSION,
                    "status": PlanningSessionStatus.FAILED,
                    "updated_at": _utc(now),
                }
            )
        )

    def restore_proposed_revision(
        self,
        run_id: str,
        revision: int,
        *,
        now: datetime,
    ) -> None:
        """Restore the last valid proposal after a completed invalid revision."""

        session = self.load_session(run_id)
        if (
            session.status is PlanningSessionStatus.PROPOSED
            and session.latest_proposal_revision == revision
        ):
            return
        if session.status is not PlanningSessionStatus.CLARIFYING:
            raise PlanningError(
                "only a clarifying Planning session can restore a proposal"
            )
        if session.latest_proposal_revision != revision:
            raise PlanningIntegrityError(
                "Planning revision recovery does not match the latest proposal"
            )
        self.load_proposal(run_id, revision)
        self._write_session(
            session.model_copy(
                update={
                    "schema_version": PLANNING_SCHEMA_VERSION,
                    "status": PlanningSessionStatus.PROPOSED,
                    "updated_at": _utc(now),
                }
            )
        )
