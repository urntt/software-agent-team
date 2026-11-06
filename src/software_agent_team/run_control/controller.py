"""Lifecycle controller and state transition coordinator.

运行生命周期控制器 - 负责管理任务初始化、状态跃迁、工作区挂载、快照归档及终止判定。
作为外部世界操作 RunRecord 的唯一合法入口; 严格保证状态机单向流转与不可篡改性。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime

from software_agent_team.artifacts import (
    ArtifactKind,
    ArtifactReference,
    IterationDecision,
    TaskBrief,
)
from software_agent_team.git_workspace import GitSnapshot, GitWorkspace
from software_agent_team.integrity import canonical_model_sha256
from software_agent_team.teams import (
    TeamManifest,
    TeamPlan,
    TeamPlanOrigin,
    validate_fixed_team_plan,
)

from .store import RunStore, _system_clock
from .types import (
    Clock,
    InvalidRunTransitionError,
    RunConflictError,
    RunControlError,
    RunIntegrityError,
    RunPhase,
    RunRecord,
    RunTransition,
    TerminationReason,
    _is_legal_transition,
    _require_utc,
)


class RunController:
    """Only authority allowed to create and advance persisted run state."""

    def __init__(
        self,
        store: RunStore,
        manifest: TeamManifest | None,
        *,
        clock: Clock = _system_clock,
    ) -> None:
        self.store = store
        self.manifest = manifest
        self.clock = clock

    def create(
        self,
        task_brief: TaskBrief,
        *,
        team_plan: TeamPlan,
    ) -> RunRecord:
        """Create the initial recoverable record for a confirmed task brief."""

        if not task_brief.confirmed:
            raise RunControlError("a run requires a confirmed task brief")
        if team_plan.run_id != task_brief.run_id:
            raise RunControlError("TeamPlan run ID must match the confirmed task")
        task_brief_sha256 = canonical_model_sha256(task_brief)
        if team_plan.task_brief_sha256 != task_brief_sha256:
            raise RunControlError("TeamPlan must bind the confirmed task brief")
        self._validate_team_plan_boundary(team_plan)

        now = _require_utc(self.clock())
        record = RunRecord(
            run_id=task_brief.run_id,
            team_id=team_plan.team_id,
            team_plan_revision=team_plan.revision,
            team_plan_sha256=canonical_model_sha256(team_plan),
            agent_timeouts_seconds={
                agent.id: agent.timeout_seconds for agent in team_plan.agents
            },
            iteration_limit=team_plan.iteration_limit,
            task_brief_sha256=task_brief_sha256,
            created_at=now,
            updated_at=now,
        )
        return self.store.create(task_brief, team_plan, record)

    def load(self, run_id: str) -> RunRecord:
        """Recover the latest complete state without inferring new work."""

        _, team_plan, record = self.store.load(run_id)
        self._validate_team_plan_boundary(team_plan, record=record)
        return record

    def advance(
        self,
        run_id: str,
        *,
        expected_revision: int,
        target: RunPhase,
        reason: str,
        artifacts: tuple[ArtifactReference, ...] = (),
        implementation_plan_sha256: str | None = None,
        decision: IterationDecision | None = None,
    ) -> RunRecord:
        """Apply one non-terminal lifecycle transition."""

        if target.is_terminal:
            raise InvalidRunTransitionError(
                "use complete() or fail() for terminal transitions"
            )
        team_plan, current = self._load_expected_with_plan(
            run_id,
            expected_revision,
        )
        if (
            current.phase is RunPhase.PREPARING_WORKSPACE
            and target is RunPhase.PLANNING
        ):
            raise InvalidRunTransitionError("use attach_workspace() to enter planning")
        if current.phase is RunPhase.SNAPSHOTTING and target is RunPhase.VERIFYING:
            raise InvalidRunTransitionError(
                "use record_snapshot() to enter verification"
            )
        if not _is_legal_transition(current.phase, target):
            raise InvalidRunTransitionError(
                f"illegal run transition: {current.phase.value} -> {target.value}"
            )
        if current.phase is RunPhase.PLANNING and target is RunPhase.IMPLEMENTING:
            if team_plan.origin is TeamPlanOrigin.ADAPTIVE_PLANNING:
                if implementation_plan_sha256 != (team_plan.implementation_plan_sha256):
                    raise RunIntegrityError(
                        "implementation-plan digest differs from the approved TeamPlan"
                    )
            elif implementation_plan_sha256 is not None:
                raise RunIntegrityError(
                    "fixed TeamPlan cannot start from an adaptive plan digest"
                )
            if team_plan.team_id == "single_agent":
                if artifacts:
                    raise RunIntegrityError("single-agent baseline cannot claim a plan")
            elif (
                not any(
                    item.kind is ArtifactKind.IMPLEMENTATION_PLAN for item in artifacts
                )
                and team_plan.origin is TeamPlanOrigin.FIXED_MANIFEST
            ):
                raise RunIntegrityError("fixed team requires a planning artifact")
        elif implementation_plan_sha256 is not None:
            raise InvalidRunTransitionError(
                "implementation-plan digest is valid only when planning starts"
            )
        iteration_after = current.current_iteration
        if current.phase is RunPhase.DECIDING and target is RunPhase.IMPLEMENTING:
            if current.current_iteration >= current.iteration_limit:
                raise InvalidRunTransitionError(
                    "run iteration limit prevents another revision"
                )
            iteration_after += 1

        return self._transition(
            current,
            target=target,
            iteration_after=iteration_after,
            reason=reason,
            artifacts=artifacts,
            implementation_plan_sha256=implementation_plan_sha256,
            decision=decision,
        )

    def record_event_head(
        self,
        run_id: str,
        *,
        event_sequence: int,
        event_sha256: str,
        occurred_at: datetime,
    ) -> RunRecord:
        """Anchor the next append-only event without advancing lifecycle state."""

        if re.fullmatch(r"[0-9a-f]{64}", event_sha256) is None:
            raise RunIntegrityError("RunEvent head digest is invalid")
        current = self.load(run_id)
        if event_sequence != current.event_count + 1:
            raise RunConflictError(
                "RunEvent sequence conflict: "
                f"expected {current.event_count + 1}, found {event_sequence}"
            )
        event_time = _require_utc(occurred_at)
        if event_time < current.updated_at:
            raise RunControlError("RunEvent timestamp predates run state")
        updated = current.model_copy(
            update={
                "updated_at": event_time,
                "event_count": event_sequence,
                "event_head_sha256": event_sha256,
            }
        )
        updated = RunRecord.model_validate(updated.model_dump())
        return self.store.replace_event_head(current, updated)

    def attach_workspace(
        self,
        run_id: str,
        *,
        expected_revision: int,
        workspace: GitWorkspace,
        reason: str = "isolated workspace prepared",
    ) -> RunRecord:
        """Attach verified workspace evidence and enter planning."""

        current = self._load_expected(run_id, expected_revision)
        if current.phase is not RunPhase.PREPARING_WORKSPACE:
            raise InvalidRunTransitionError(
                f"cannot attach a workspace from {current.phase.value}"
            )
        if workspace.run_id != current.run_id:
            raise RunIntegrityError("workspace belongs to a different run")
        return self._transition(
            current,
            target=RunPhase.PLANNING,
            iteration_after=current.current_iteration,
            reason=reason,
            record_updates={"workspace": workspace},
        )

    def record_snapshot(
        self,
        run_id: str,
        *,
        expected_revision: int,
        snapshot: GitSnapshot,
        reason: str = "iteration commit snapshot verified",
    ) -> RunRecord:
        """Append verified commit evidence and enter deterministic verification."""

        current = self._load_expected(run_id, expected_revision)
        if current.phase is not RunPhase.SNAPSHOTTING:
            raise InvalidRunTransitionError(
                f"cannot record a snapshot from {current.phase.value}"
            )
        if snapshot.run_id != current.run_id:
            raise RunIntegrityError("snapshot belongs to a different run")
        if snapshot.iteration != current.current_iteration:
            raise RunIntegrityError("snapshot iteration does not match the run")
        if snapshot.input_commit != current.current_commit:
            raise RunIntegrityError("snapshot input does not match the current commit")
        return self._transition(
            current,
            target=RunPhase.VERIFYING,
            iteration_after=current.current_iteration,
            reason=reason,
            record_updates={"snapshots": (*current.snapshots, snapshot)},
        )

    def complete(
        self,
        run_id: str,
        *,
        expected_revision: int,
        detail: str,
        final_report: ArtifactReference,
    ) -> RunRecord:
        """Complete a delivering run with an explicit success record."""

        current = self._load_expected(run_id, expected_revision)
        if current.phase is not RunPhase.DELIVERING:
            raise InvalidRunTransitionError(
                f"cannot complete a run from {current.phase.value}"
            )
        return self._transition(
            current,
            target=RunPhase.COMPLETED,
            iteration_after=current.current_iteration,
            reason="delivery completed",
            artifacts=(final_report,),
            termination_reason=TerminationReason.SUCCEEDED,
            termination_detail=detail,
        )

    def fail(
        self,
        run_id: str,
        *,
        expected_revision: int,
        reason: TerminationReason,
        detail: str,
        final_report: ArtifactReference,
    ) -> RunRecord:
        """Persist an explicit failure from any non-terminal run phase."""

        if reason is TerminationReason.SUCCEEDED:
            raise InvalidRunTransitionError("failure cannot use the succeeded reason")
        current = self._load_expected(run_id, expected_revision)
        if current.phase.is_terminal:
            raise InvalidRunTransitionError("terminal runs cannot transition again")
        return self._transition(
            current,
            target=RunPhase.FAILED,
            iteration_after=current.current_iteration,
            reason=detail,
            artifacts=(final_report,),
            termination_reason=reason,
            termination_detail=detail,
        )

    def _load_expected(self, run_id: str, expected_revision: int) -> RunRecord:
        _, current = self._load_expected_with_plan(run_id, expected_revision)
        return current

    def _load_expected_with_plan(
        self,
        run_id: str,
        expected_revision: int,
    ) -> tuple[TeamPlan, RunRecord]:
        _, team_plan, current = self.store.load(run_id)
        self._validate_team_plan_boundary(team_plan, record=current)
        if current.revision != expected_revision:
            raise RunConflictError(
                f"run revision conflict: expected {expected_revision}, "
                f"found {current.revision}"
            )
        if current.phase.is_terminal:
            raise InvalidRunTransitionError("terminal runs cannot transition again")
        return team_plan, current

    def _transition(
        self,
        current: RunRecord,
        *,
        target: RunPhase,
        iteration_after: int,
        reason: str,
        artifacts: tuple[ArtifactReference, ...] = (),
        implementation_plan_sha256: str | None = None,
        decision: IterationDecision | None = None,
        termination_reason: TerminationReason | None = None,
        termination_detail: str | None = None,
        record_updates: Mapping[str, object] | None = None,
    ) -> RunRecord:
        now = _require_utc(self.clock())
        if now < current.updated_at:
            raise RunControlError("controller clock moved backwards")
        transition = RunTransition(
            sequence=current.revision + 1,
            source=current.phase,
            target=target,
            iteration_before=current.current_iteration,
            iteration_after=iteration_after,
            occurred_at=now,
            reason=reason,
            artifacts=artifacts,
            implementation_plan_sha256=implementation_plan_sha256,
            decision=decision,
        )
        updates: dict[str, object] = {
            "phase": target,
            "current_iteration": iteration_after,
            "revision": current.revision + 1,
            "updated_at": now,
            "termination_reason": termination_reason,
            "termination_detail": termination_detail,
            "transitions": (*current.transitions, transition),
        }
        if record_updates:
            reserved = set(updates) & set(record_updates)
            if reserved:
                raise RunIntegrityError(
                    f"record update cannot replace controller fields: "
                    f"{', '.join(sorted(reserved))}"
                )
            updates.update(record_updates)
        updated = current.model_copy(update=updates)
        updated = RunRecord.model_validate(updated.model_dump())
        return self.store.replace(current, updated)

    def _validate_team_plan_boundary(
        self,
        team_plan: TeamPlan,
        *,
        record: RunRecord | None = None,
    ) -> None:
        try:
            if team_plan.origin is TeamPlanOrigin.FIXED_MANIFEST:
                if self.manifest is None:
                    raise ValueError("fixed TeamPlan requires its source manifest")
                validate_fixed_team_plan(team_plan, self.manifest)
        except ValueError as error:
            raise RunIntegrityError(
                f"TeamPlan provenance is invalid: {error}"
            ) from error

        if record is None:
            return
        if record.team_id != team_plan.team_id:
            raise RunIntegrityError("persisted run uses a different TeamPlan team")
        if record.team_plan_revision != team_plan.revision:
            raise RunIntegrityError("persisted run uses a different TeamPlan revision")
        if record.team_plan_sha256 != canonical_model_sha256(team_plan):
            raise RunIntegrityError("persisted run uses a different TeamPlan digest")
        if record.iteration_limit != team_plan.iteration_limit:
            raise RunIntegrityError("persisted iteration limit differs from TeamPlan")
        timeouts = {agent.id: agent.timeout_seconds for agent in team_plan.agents}
        if record.agent_timeouts_seconds != timeouts:
            raise RunIntegrityError("persisted Agent timeouts differ from TeamPlan")


__all__ = [
    "RunController",
]
