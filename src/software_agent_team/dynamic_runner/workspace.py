"""Workspace validation, scope checks, and writer commit transition mixin.

This module provides the WorkspaceValidationMixin which manages clean workspace
pre-conditions, scope boundaries, git snapshot verification, and verified
unchanged integration result authorization.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from software_agent_team.artifacts import (
    AgentSubmissionStatus,
    AgentToolCallOutcome,
    AgentToolEvidenceStatus,
    WorkResult,
)
from software_agent_team.execution import AgentExecutionResult
from software_agent_team.git_workspace import GitSnapshot
from software_agent_team.responses import WorkResultResponse
from software_agent_team.run_control import TerminationReason
from software_agent_team.scheduling import AgentRunOutcome
from software_agent_team.teams import (
    AgentCapability,
    AgentSpec,
    PermissionProfile,
)

from .types import DynamicAgentRunnerError

if TYPE_CHECKING:
    from software_agent_team.artifact_store import ArtifactStore
    from software_agent_team.git_workspace import GitWorkspace, GitWorkspaceManager


class WorkspaceValidationMixin:
    """Methods for validating workspace state, writer permissions, and scopes."""

    artifact_store: ArtifactStore
    workspace: GitWorkspace
    workspace_manager: GitWorkspaceManager
    iteration: int
    _latest_commit: str

    def _verified_unchanged_integration(
        self,
        agent: AgentSpec,
        *,
        body: WorkResultResponse,
        input_commit: str,
        result: AgentExecutionResult,
        upstream: Mapping[str, AgentRunOutcome],
    ) -> bool:
        """Authorize an evidence-backed no-op only after a completed writer."""
        liveness = result.telemetry.provider_liveness
        if (
            agent.capability is not AgentCapability.INTEGRATION
            or body.unresolved_issues
            or result.telemetry.tool_evidence_status
            is not AgentToolEvidenceStatus.CAPTURED
            or result.telemetry.runtime_rejections
            or result.submission_evidence is None
            or result.submission_evidence.status is not AgentSubmissionStatus.ACCEPTED
            or liveness is None
            or liveness.stalled
            or liveness.tool_started_count != len(result.telemetry.tool_calls)
            or liveness.tool_completed_count != len(result.telemetry.tool_calls)
        ):
            return False
        submission_tool = result.submission_evidence.tool_name
        has_successful_verification = any(
            call.tool_name != submission_tool
            and call.outcome is AgentToolCallOutcome.SUCCEEDED
            and not call.is_error
            for call in result.telemetry.tool_calls
        )
        if not has_successful_verification:
            return False
        for dependency_id in agent.dependencies:
            outcome = upstream.get(dependency_id)
            if outcome is None or outcome.output is None:
                continue
            artifact = self.artifact_store.load(outcome.output)
            if (
                isinstance(artifact, WorkResult)
                and artifact.output_commit == input_commit
            ):
                return True
        return False

    def _begin_writer(self, agent: AgentSpec) -> str:
        """Verify preconditions before an implementation Agent modifies workspace."""
        if agent.capability not in {
            AgentCapability.IMPLEMENTATION,
            AgentCapability.INTEGRATION,
        }:
            raise DynamicAgentRunnerError(
                "write permission was assigned to a non-implementation Agent",
                TerminationReason.SAFETY_BOUNDARY_CROSSED,
            )
        with self._state_lock:  # type: ignore[attr-defined]
            expected = self._latest_commit
        return self.workspace_manager.verify_workspace(
            self.workspace,
            expected_commit=expected,
            require_clean=True,
        )

    def _finish_writer(
        self,
        agent: AgentSpec,
        input_commit: str,
        output_commit: str,
    ) -> None:
        """Commit workspace state transition for a finished writer Agent."""
        with self._state_lock:  # type: ignore[attr-defined]
            if self._latest_commit != input_commit:
                raise DynamicAgentRunnerError(
                    f"writer {agent.id} did not start from the controller commit",
                    TerminationReason.SAFETY_BOUNDARY_CROSSED,
                )
            self._latest_commit = output_commit

    def _verify_workspace_after_call(
        self,
        agent: AgentSpec,
        input_commit: str,
    ) -> GitSnapshot | None:
        """Verify cleanliness and capture any authorized git diff snapshot."""
        if agent.permission_profile is PermissionProfile.READ_ONLY:
            self.workspace_manager.verify_workspace(
                self.workspace,
                expected_commit=input_commit,
                require_clean=True,
            )
            return None
        head = self.workspace_manager.verify_workspace(
            self.workspace,
            require_clean=True,
        )
        if head == input_commit:
            return None
        snapshot = self.workspace_manager.verify_snapshot(
            self.workspace,
            iteration=self.iteration,
            input_commit=input_commit,
        )
        self._validate_workspace_scope(agent, snapshot.changed_files)
        return snapshot

    @staticmethod
    def _validate_workspace_scope(
        agent: AgentSpec,
        changed_files: tuple[str, ...],
    ) -> None:
        """Enforce that modified files lie within the Agent's approved scope."""
        scope = PurePosixPath(agent.workspace_scope)
        if scope == PurePosixPath("repository"):
            return
        if scope.parts and scope.parts[0] == "repository":
            scope = PurePosixPath(*scope.parts[1:])
        if scope == PurePosixPath("."):
            return
        outside = [
            path
            for path in changed_files
            if PurePosixPath(path) != scope and scope not in PurePosixPath(path).parents
        ]
        if outside:
            raise DynamicAgentRunnerError(
                f"Agent {agent.id} changed paths outside {agent.workspace_scope}: "
                f"{', '.join(outside)}",
                TerminationReason.SAFETY_BOUNDARY_CROSSED,
            )
