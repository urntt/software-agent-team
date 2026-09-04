"""Controller-owned runtime adapter for one approved dynamic Agent DAG."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import PurePosixPath
from threading import Condition, RLock
from typing import Protocol, cast

from pydantic import ValidationError

from software_agent_team.artifact_store import ArtifactStore, ArtifactStoreError
from software_agent_team.artifacts import (
    AgentToolEvidenceStatus,
    ArtifactKind,
    ArtifactReference,
    CommandEvidence,
    HandoffEnvelope,
    HandoffStatus,
    ReviewReport,
    TaskBrief,
    TestReport,
    WorkResult,
)
from software_agent_team.assembly import (
    ArtifactAssemblyError,
    assemble_review_report,
    assemble_test_report,
    assemble_work_result,
    validate_verification_assignment,
)
from software_agent_team.budgets import (
    AgentBudgetExceeded,
    AgentBudgetLedger,
    AgentBudgetUsage,
    ModelPricing,
)
from software_agent_team.execution import (
    AgentExecutionActivity,
    AgentExecutionActivityKind,
    AgentExecutionRequest,
    AgentExecutionResult,
    AgentExecutionStatus,
    AgentExecutionTelemetry,
    AgentExecutor,
)
from software_agent_team.git_workspace import (
    GitSnapshot,
    GitWorkspace,
    GitWorkspaceError,
    GitWorkspaceManager,
    WorkspaceIntegrityError,
)
from software_agent_team.integrity import canonical_model_sha256
from software_agent_team.invocation import persist_agent_invocation
from software_agent_team.planning import (
    AdaptiveImplementationPlan,
    validate_task_agent_bindings,
)
from software_agent_team.progress import (
    ProgressDraftHandler,
    ProgressEvent,
    ProgressEventKind,
    RunEventReference,
    RunEventReferenceKind,
)
from software_agent_team.prompting import (
    AgentPromptError,
    DynamicAgentPromptInputs,
    DynamicRevisionFeedback,
    DynamicUpstreamResult,
    DynamicUserGuidance,
    build_dynamic_agent_execution_request,
    build_semantic_repair_request,
)
from software_agent_team.quality_gates import (
    QualityGateBudgetExceeded,
    QualityGateError,
    SandboxUnavailableError,
)
from software_agent_team.responses import (
    AgentArtifactResponseError,
    GroundedReviewReportResponse,
    ReviewToolEvidenceAttempt,
    TestReportResponse,
    WorkResultResponse,
    controller_fields_for,
    parse_dynamic_agent_response,
)
from software_agent_team.run_control import TerminationReason
from software_agent_team.scheduling import (
    AgentRunOutcome,
    AgentRunStatus,
)
from software_agent_team.teams import (
    AgentCapability,
    AgentSpec,
    ModelSwitchCondition,
    PermissionProfile,
    TeamPlan,
    TeamPlanOrigin,
)


class DynamicQualityGate(Protocol):
    """Deterministic quality boundary shared by all quality Agents."""

    def run(self, *, iteration: int) -> tuple[CommandEvidence, ...]:
        """Execute the approved commands for one immutable commit."""


class DynamicAgentRunnerError(RuntimeError):
    """A classified failure returned through the deterministic scheduler."""

    def __init__(self, detail: str, reason: TerminationReason) -> None:
        super().__init__(detail)
        self.reason = reason


_SCHEDULER_SUMMARY_LIMIT = 2_000
_UPSTREAM_SUMMARY_LIMIT = 1_000


def _bounded_artifact_summary(value: str, *, limit: int) -> str:
    """Project complete artifact text into an attributable bounded context."""

    cleaned = value.strip()
    if len(cleaned) <= limit:
        return cleaned
    digest = hashlib.sha256(cleaned.encode()).hexdigest()
    marker = (
        "\n[Controller projection: source summary truncated from "
        f"{len(cleaned)} characters; source sha256={digest}; full text remains "
        "in immutable artifact evidence.]"
    )
    if len(marker) >= limit:
        raise ValueError("artifact summary projection limit is too small")
    prefix = cleaned[: limit - len(marker)].rstrip()
    return f"{prefix}{marker}"


type GuidanceProvider = Callable[[str], tuple[DynamicUserGuidance, ...]]


def _system_clock() -> datetime:
    return datetime.now(UTC)


def _utc(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if value.tzinfo is None or value.utcoffset() is None:
        raise DynamicAgentRunnerError(
            "dynamic runner clock must include a timezone",
            TerminationReason.CONTROLLER_ERROR,
        )
    return value.astimezone(UTC)


class DynamicAgentRunner:
    """Invoke, verify, assemble, account for, and hand off approved Agents.

    ``DagScheduler`` remains the sole scheduling authority. This adapter owns
    the bounded lifecycle of one ready ``AgentSpec`` and never creates another
    Agent, changes the approved DAG, or extends an approved timeout.
    """

    def __init__(
        self,
        *,
        task_brief: TaskBrief,
        implementation_plan: AdaptiveImplementationPlan,
        team_plan: TeamPlan,
        workspace: GitWorkspace,
        workspace_manager: GitWorkspaceManager,
        artifact_store: ArtifactStore,
        executor: AgentExecutor,
        quality_gate: DynamicQualityGate,
        budget_ledger: AgentBudgetLedger,
        pricing_by_model: Mapping[str, ModelPricing],
        manual_review_criteria: tuple[str, ...] = (),
        review_scope_by_agent: Mapping[str, tuple[str, ...]] | None = None,
        iteration: int = 1,
        input_commit: str | None = None,
        artifact_repair_limit: int = 1,
        revision_feedback: DynamicRevisionFeedback | None = None,
        guidance_provider: GuidanceProvider | None = None,
        activity_handler: ProgressDraftHandler | None = None,
        clock: Callable[[], datetime] = _system_clock,
    ) -> None:
        if team_plan.origin is not TeamPlanOrigin.ADAPTIVE_PLANNING:
            raise ValueError("dynamic runner requires an adaptive TeamPlan")
        if not task_brief.confirmed:
            raise ValueError("dynamic runner requires a confirmed TaskBrief")
        if task_brief.run_id != implementation_plan.run_id:
            raise ValueError("implementation plan belongs to a different run")
        if (
            task_brief.run_id != team_plan.run_id
            or workspace.run_id != team_plan.run_id
        ):
            raise ValueError("dynamic runtime inputs use different run IDs")
        if implementation_plan.team_id != team_plan.team_id:
            raise ValueError("implementation plan belongs to a different team")
        if implementation_plan.revision != team_plan.revision:
            raise ValueError("implementation plan and TeamPlan revisions differ")
        if canonical_model_sha256(task_brief) != team_plan.task_brief_sha256:
            raise ValueError("TeamPlan does not bind the supplied TaskBrief")
        if canonical_model_sha256(implementation_plan) != (
            team_plan.implementation_plan_sha256
        ):
            raise ValueError("TeamPlan does not bind the implementation plan")
        if (
            artifact_store.task_brief != task_brief
            or artifact_store.team_plan != team_plan
        ):
            raise ValueError("artifact store binds different dynamic inputs")
        if not 1 <= iteration <= team_plan.iteration_limit:
            raise ValueError("dynamic iteration exceeds the approved TeamPlan")
        if artifact_repair_limit not in {0, 1}:
            raise ValueError("dynamic execution permits zero or one semantic repair")

        writer_ids = {
            agent.id
            for agent in team_plan.agents
            if agent.capability
            in {AgentCapability.IMPLEMENTATION, AgentCapability.INTEGRATION}
        }
        try:
            validate_task_agent_bindings(
                implementation_plan.tasks,
                {agent.id: agent.dependencies for agent in team_plan.agents},
                writer_ids,
            )
        except ValueError as error:
            raise ValueError(f"dynamic implementation plan {error}") from error

        criteria = tuple(item.strip() for item in manual_review_criteria)
        if any(not item for item in criteria) or len(criteria) != len(set(criteria)):
            raise ValueError("manual-review criterion IDs must be non-empty and unique")
        known_criteria = {item.id for item in task_brief.acceptance_criteria}
        if not set(criteria).issubset(known_criteria):
            raise ValueError("manual-review scope references an unknown criterion")
        reviewer_ids = {
            agent.id
            for agent in team_plan.agents
            if agent.capability is AgentCapability.REVIEW
        }
        scopes = self._resolve_review_scopes(
            reviewer_ids,
            criteria,
            review_scope_by_agent,
        )

        route_models = {route.model for route in team_plan.model_routes.routes}
        if set(pricing_by_model) != route_models:
            raise ValueError("pricing evidence must exactly cover authorized models")
        prices = dict(pricing_by_model)
        if any(model != pricing.model for model, pricing in prices.items()):
            raise ValueError("pricing keys and model identities differ")

        starting_commit = input_commit or workspace.base_commit
        verified_commit = workspace_manager.verify_workspace(
            workspace,
            expected_commit=starting_commit,
            require_clean=True,
        )

        self.task_brief = task_brief
        self.implementation_plan = implementation_plan
        self.team_plan = team_plan
        self.workspace = workspace
        self.workspace_manager = workspace_manager
        self.artifact_store = artifact_store
        self.executor = executor
        self.quality_gate = quality_gate
        self.budget_ledger = budget_ledger
        self.pricing_by_model = prices
        self.manual_review_criteria = criteria
        self.review_scope_by_agent = scopes
        self.iteration = iteration
        self.input_commit = verified_commit
        self.artifact_repair_limit = artifact_repair_limit
        self.revision_feedback = revision_feedback
        self.guidance_provider = guidance_provider
        self.activity_handler = activity_handler
        self.clock = clock

        self._state_lock = RLock()
        self._quality_condition = Condition(self._state_lock)
        self._latest_commit = verified_commit
        self._outputs: dict[str, ArtifactReference] = {}
        self._execution_records: list[ArtifactReference] = []
        self._execution_by_agent: dict[str, list[ArtifactReference]] = {}
        self._handoffs: list[ArtifactReference] = []
        self._handoff_sequences: dict[str, int] = {}
        self._termination_reasons: dict[str, TerminationReason] = {}
        self._quality_state = "pending"
        self._quality_error: DynamicAgentRunnerError | None = None
        self._quality_commands: tuple[CommandEvidence, ...] = ()
        self._quality_commit: str | None = None
        self._quality_gate_calls = 0
        self._controller_test_reference: ArtifactReference | None = None

    @staticmethod
    def _resolve_review_scopes(
        reviewer_ids: set[str],
        manual_criteria: tuple[str, ...],
        supplied: Mapping[str, tuple[str, ...]] | None,
    ) -> dict[str, tuple[str, ...]]:
        if manual_criteria and not reviewer_ids:
            raise ValueError("manual-review criteria require an approved Reviewer")
        if supplied is None:
            if len(reviewer_ids) == 1:
                return {next(iter(reviewer_ids)): manual_criteria}
            if manual_criteria:
                raise ValueError(
                    "multiple Reviewers require an explicit non-overlapping scope"
                )
            return {agent_id: () for agent_id in reviewer_ids}
        if set(supplied) != reviewer_ids:
            raise ValueError("review-scope keys must exactly cover approved Reviewers")
        resolved: dict[str, tuple[str, ...]] = {}
        flattened: list[str] = []
        for agent_id in sorted(reviewer_ids):
            scope = tuple(item.strip() for item in supplied[agent_id])
            if any(not item for item in scope) or len(scope) != len(set(scope)):
                raise ValueError("Reviewer scopes must contain unique criterion IDs")
            resolved[agent_id] = scope
            flattened.extend(scope)
        if len(flattened) != len(set(flattened)):
            raise ValueError("Reviewer scopes cannot overlap")
        if set(flattened) != set(manual_criteria):
            raise ValueError("Reviewer scopes must exactly cover manual criteria")
        return resolved

    @property
    def execution_records(self) -> tuple[ArtifactReference, ...]:
        """Return every recorded invocation in actual persistence order."""

        with self._state_lock:
            return tuple(self._execution_records)

    @property
    def handoffs(self) -> tuple[ArtifactReference, ...]:
        """Return every completed or failed durable handoff."""

        with self._state_lock:
            return tuple(self._handoffs)

    @property
    def outputs(self) -> dict[str, ArtifactReference]:
        """Return successful phase output references keyed by Agent ID."""

        with self._state_lock:
            return dict(self._outputs)

    @property
    def termination_reasons(self) -> dict[str, TerminationReason]:
        """Return controller classifications for failed Agents."""

        with self._state_lock:
            return dict(self._termination_reasons)

    @property
    def quality_gate_calls(self) -> int:
        """Expose deterministic-gate execution count for evidence and tests."""

        with self._state_lock:
            return self._quality_gate_calls

    @property
    def controller_test_reference(self) -> ArtifactReference | None:
        """Return controller-owned deterministic evidence when no Tester exists."""

        with self._state_lock:
            return self._controller_test_reference

    def __call__(
        self,
        agent: AgentSpec,
        upstream: Mapping[str, AgentRunOutcome],
    ) -> AgentRunOutcome:
        """Run one scheduler-approved Agent without changing scheduling policy."""

        if self.team_plan.get_agent(agent.id) != agent:
            return self._failed_outcome(
                agent,
                "scheduler supplied an AgentSpec outside the approved TeamPlan",
                TerminationReason.SAFETY_BOUNDARY_CROSSED,
                (),
            )
        incoming: tuple[ArtifactReference, ...] = ()
        try:
            incoming = self._persist_incoming_handoffs(agent, upstream)
            output = self._invoke_agent(agent, upstream)
            with self._state_lock:
                self._outputs[agent.id] = output
            executions = self._execution_references(agent.id)
            auxiliary = self._quality_auxiliary_references(agent)
            terminal = self._persist_terminal_handoff(
                agent,
                output=output,
                executions=executions,
                failed_detail=None,
            )
            evidence = self._unique_references(
                (*incoming, *executions, *auxiliary, *terminal)
            )
            artifact = self.artifact_store.load(output)
            summary = cast(WorkResult | TestReport | ReviewReport, artifact).summary
            return AgentRunOutcome(
                agent_id=agent.id,
                status=AgentRunStatus.COMPLETED,
                output=output,
                evidence=evidence,
                summary=_bounded_artifact_summary(
                    summary,
                    limit=_SCHEDULER_SUMMARY_LIMIT,
                ),
            )
        except Exception as error:
            reason = self._classify_exception(error)
            detail = self._error_detail(error)
            with self._state_lock:
                self._termination_reasons[agent.id] = reason
            executions = self._execution_references(agent.id)
            auxiliary = self._quality_auxiliary_references(agent)
            terminal = self._persist_terminal_handoff(
                agent,
                output=None,
                executions=executions,
                failed_detail=detail,
            )
            evidence = self._unique_references(
                (*incoming, *executions, *auxiliary, *terminal)
            )
            return self._failed_outcome(agent, detail, reason, evidence)

    def _failed_outcome(
        self,
        agent: AgentSpec,
        detail: str,
        reason: TerminationReason,
        evidence: tuple[ArtifactReference, ...],
    ) -> AgentRunOutcome:
        safe_detail = detail[:2000]
        return AgentRunOutcome(
            agent_id=agent.id,
            status=(
                AgentRunStatus.INTERRUPTED
                if reason is TerminationReason.USER_INTERRUPTED
                else AgentRunStatus.FAILED
            ),
            evidence=evidence,
            summary=f"{agent.label} failed ({reason.value})."[:2000],
            error=safe_detail,
        )

    def _invoke_agent(
        self,
        agent: AgentSpec,
        upstream: Mapping[str, AgentRunOutcome],
    ) -> ArtifactReference:
        if agent.permission_profile is PermissionProfile.WORKSPACE_WRITE:
            input_commit = self._begin_writer(agent)
            commands: tuple[CommandEvidence, ...] = ()
            manual_scope: tuple[str, ...] = ()
        else:
            commands, input_commit = self._ensure_quality_evidence()
            manual_scope = (
                self.review_scope_by_agent[agent.id]
                if agent.capability is AgentCapability.REVIEW
                else self.manual_review_criteria
            )
        upstream_results = self._upstream_results(agent, upstream)
        assigned_task_ids = tuple(
            task.id
            for task in self.implementation_plan.tasks
            if task.owner_agent_id == agent.id
        )
        guidance_by_id: dict[str, DynamicUserGuidance] = {}
        previous_error = "Agent did not return a semantic response"
        repair_detail: str | None = None
        semantic_repairs = 0
        attempt = 1
        review_evidence_attempts: list[ReviewToolEvidenceAttempt] = []
        route_ids = self.team_plan.model_routes.authorized_route_ids(agent.id)
        route_index = 0
        provider_switching = ModelSwitchCondition.PROVIDER_FAILURE in (
            self.team_plan.model_routes.authorized_switch_conditions
        )

        while True:
            route_id = route_ids[route_index]
            if self.guidance_provider is not None:
                for guidance in self.guidance_provider(agent.id):
                    guidance_by_id[guidance.command_id] = guidance
            prompt_inputs = DynamicAgentPromptInputs(
                task_brief=self.task_brief,
                implementation_plan=self.implementation_plan,
                team_plan=self.team_plan,
                agent_id=agent.id,
                active_model_route_id=route_id,
                iteration=self.iteration,
                iteration_input_commit=self.input_commit,
                input_commit=input_commit,
                upstream_results=upstream_results,
                command_evidence=commands,
                manual_review_criteria=manual_scope,
                revision_feedback=self.revision_feedback,
                user_guidance=tuple(guidance_by_id.values()),
            )
            base_request = build_dynamic_agent_execution_request(prompt_inputs)
            request = (
                base_request
                if repair_detail is None
                else build_semantic_repair_request(base_request, repair_detail)
            )
            pricing = self.pricing_by_model[cast(str, request.model)]
            reservation = self.budget_ledger.reserve_call(
                agent.id,
                run_id=self.task_brief.run_id,
                stage=agent.stage_id,
                attempt=attempt,
                route_id=route_id,
                pricing=pricing,
            )
            self._emit_activity(
                agent,
                kind=ProgressEventKind.AGENT_WAITING_PROVIDER,
                message=(
                    f"{agent.label} is waiting for the approved {request.model} "
                    f"model (attempt {attempt})"
                ),
                attempt=attempt,
                model=request.model,
                references=tuple(
                    RunEventReference(
                        kind=RunEventReferenceKind.CONTROL_COMMAND,
                        id=guidance.command_id,
                    )
                    for guidance in guidance_by_id.values()
                ),
            )
            result = self._execute(
                request,
                activity_handler=lambda activity, current_attempt=attempt: (
                    self._observe_execution_activity(
                        agent,
                        attempt=current_attempt,
                        activity=activity,
                    )
                ),
            )
            response_reference: ArtifactReference | None = None
            ignored_fields: tuple[str, ...] = ()
            record_error: str | None = None
            repairable = False
            failure: Exception | None = None
            current_review_evidence: ReviewToolEvidenceAttempt | None = None
            try:
                self._validate_execution_result(result, request)
                if (
                    agent.capability is AgentCapability.REVIEW
                    and result.status is AgentExecutionStatus.COMPLETED
                ):
                    current_review_evidence = ReviewToolEvidenceAttempt(
                        execution_attempt=attempt,
                        tool_calls=result.telemetry.tool_calls,
                    )
                snapshot = (
                    None
                    if result.status is AgentExecutionStatus.INTERRUPTED
                    else self._verify_workspace_after_call(agent, input_commit)
                )
                if result.status is not AgentExecutionStatus.COMPLETED:
                    record_error = result.error or (
                        f"Agent execution ended as {result.status.value}"
                    )
                    repairable = result.status is AgentExecutionStatus.INVALID_RESPONSE
                    failure = DynamicAgentRunnerError(
                        record_error,
                        self._execution_termination_reason(result.status),
                    )
                else:
                    try:
                        parsed = parse_dynamic_agent_response(
                            result,
                            request,
                            task_brief=self.task_brief,
                            team_plan=self.team_plan,
                            assigned_task_ids=assigned_task_ids,
                            reviewed_criterion_ids=manual_scope,
                            review_tool_evidence_attempts=(
                                *review_evidence_attempts,
                                *(
                                    ()
                                    if current_review_evidence is None
                                    else (current_review_evidence,)
                                ),
                            ),
                            review_command_evidence=commands,
                        )
                    except AgentArtifactResponseError as error:
                        record_error = self._error_detail(error)
                        repairable = True
                        failure = error
                    else:
                        ignored_fields = parsed.ignored_controller_fields
                        artifact = self._assemble_response(
                            agent,
                            parsed.body,
                            input_commit=input_commit,
                            commands=commands,
                            manual_scope=manual_scope,
                            snapshot=snapshot,
                        )
                        response_reference = self.artifact_store.write(
                            artifact,
                            description=(
                                f"Controller-assembled response from {agent.id}."
                            ),
                        )
            except Exception as error:
                if failure is None:
                    failure = error
                    record_error = self._error_detail(error)

            persisted = persist_agent_invocation(
                artifact_store=self.artifact_store,
                budget_ledger=self.budget_ledger,
                reservation=reservation,
                request=request,
                result=result,
                stage=agent.stage_id,
                attempt=attempt,
                response_reference=response_reference,
                error=record_error,
                controller_supplied_fields=controller_fields_for(request.expected_kind),
                ignored_controller_fields=ignored_fields,
                pricing=pricing,
            )
            self._record_execution_reference(agent.id, persisted.reference)
            if current_review_evidence is not None:
                review_evidence_attempts.append(current_review_evidence)
            budget_usage = self.budget_ledger.snapshot()
            budget_remaining = budget_usage.remaining_estimated_cost_usd(
                self.budget_ledger.budget
            )
            pricing_source = (
                "unknown"
                if pricing.pricing_source is None
                else pricing.pricing_source.value
            )
            self._emit_activity(
                agent,
                kind=ProgressEventKind.AGENT_INVOCATION_COMPLETED,
                message=(
                    f"{agent.label} invocation {attempt} returned "
                    f"{result.status.value}; task model spend "
                    f"${budget_usage.known_estimated_cost_usd:.6f} estimated / "
                    f"${self.budget_ledger.budget.max_estimated_cost_usd} "
                    f"authorized, ${budget_remaining:.6f} recorded remaining "
                    f"(price source {pricing_source})"
                ),
                attempt=attempt,
                model=request.model,
                duration_ms=result.telemetry.duration_ms,
                budget_usage=budget_usage,
                references=(
                    RunEventReference(
                        kind=RunEventReferenceKind.ARTIFACT,
                        id=f"{agent.id}-invocation-{attempt}",
                        path=persisted.reference.path,
                        sha256=persisted.reference.sha256,
                    ),
                    RunEventReference(
                        kind=RunEventReferenceKind.MODEL_ROUTE,
                        id=route_id,
                    ),
                ),
            )
            if persisted.budget_error is not None:
                raise DynamicAgentRunnerError(
                    persisted.budget_error,
                    TerminationReason.RESOURCE_LIMIT_REACHED,
                )
            if response_reference is not None:
                if agent.permission_profile is PermissionProfile.WORKSPACE_WRITE:
                    work = cast(
                        WorkResult, self.artifact_store.load(response_reference)
                    )
                    self._finish_writer(agent, input_commit, work.output_commit)
                return response_reference
            if (
                result.status
                in {
                    AgentExecutionStatus.PROVIDER_FAILED,
                    AgentExecutionStatus.PROVIDER_STALLED,
                }
                and provider_switching
                and route_index + 1 < len(route_ids)
                and isinstance(failure, DynamicAgentRunnerError)
                and failure.reason is TerminationReason.DEPENDENCY_UNAVAILABLE
            ):
                previous_route = route_id
                route_index += 1
                route_id = route_ids[route_index]
                next_route = self.team_plan.model_routes.get_route(route_id)
                attempt += 1
                self._emit_activity(
                    agent,
                    kind=ProgressEventKind.MODEL_ROUTE_SWITCHED,
                    message=(
                        f"{agent.label} provider route {previous_route} failed; "
                        f"switching to the explicitly approved {route_id} profile. "
                        "The failed call remains recorded and may be billable."
                    ),
                    attempt=attempt,
                    model=next_route.model,
                    references=(
                        RunEventReference(
                            kind=RunEventReferenceKind.MODEL_ROUTE,
                            id=previous_route,
                        ),
                        RunEventReference(
                            kind=RunEventReferenceKind.MODEL_ROUTE,
                            id=route_id,
                        ),
                    ),
                )
                continue
            previous_error = record_error or previous_error
            if repairable and semantic_repairs < self.artifact_repair_limit:
                semantic_repairs += 1
                repair_detail = previous_error
                self._emit_activity(
                    agent,
                    kind=ProgressEventKind.AGENT_RETRY,
                    message=(
                        f"{agent.label} response failed validation; "
                        "starting one bounded repair"
                    ),
                    attempt=attempt,
                    model=request.model,
                )
                attempt += 1
                continue
            if failure is not None:
                if repairable:
                    raise DynamicAgentRunnerError(
                        previous_error,
                        TerminationReason.ARTIFACT_INVALID,
                    ) from failure
                raise failure
            raise DynamicAgentRunnerError(
                previous_error,
                TerminationReason.EXECUTION_FAILED,
            )

    def _emit_activity(
        self,
        agent: AgentSpec,
        *,
        kind: ProgressEventKind,
        message: str,
        attempt: int,
        model: str | None,
        duration_ms: int | None = None,
        budget_usage: AgentBudgetUsage | None = None,
        references: tuple[RunEventReference, ...] = (),
    ) -> None:
        """Project one safe invocation checkpoint without changing runtime state."""

        if self.activity_handler is None:
            return
        self.activity_handler(
            ProgressEvent(
                kind=kind,
                message=(" ".join(message.split()) or "Agent activity changed")[:500],
                agent_id=agent.id,
                iteration=self.iteration,
                attempt=attempt,
                duration_ms=duration_ms,
                capability=agent.capability.value,
                stage_id=agent.stage_id,
                model=model,
                dependency_ids=agent.dependencies,
                budget_usage=budget_usage,
                references=references,
            )
        )

    def _observe_execution_activity(
        self,
        agent: AgentSpec,
        *,
        attempt: int,
        activity: AgentExecutionActivity,
    ) -> None:
        kind = {
            AgentExecutionActivityKind.PROVIDER_STREAM: (
                ProgressEventKind.AGENT_PROVIDER_ACTIVITY
            ),
            AgentExecutionActivityKind.TOOL_STARTED: (
                ProgressEventKind.AGENT_TOOL_STARTED
            ),
            AgentExecutionActivityKind.TOOL_COMPLETED: (
                ProgressEventKind.AGENT_TOOL_COMPLETED
            ),
            AgentExecutionActivityKind.LIVENESS_DEGRADED: (
                ProgressEventKind.AGENT_LIVENESS_DEGRADED
            ),
            AgentExecutionActivityKind.STALL_SUSPECTED: (
                ProgressEventKind.AGENT_STALL_SUSPECTED
            ),
            AgentExecutionActivityKind.STALL_RECOVERED: (
                ProgressEventKind.AGENT_STALL_RECOVERED
            ),
            AgentExecutionActivityKind.PROVIDER_STALLED: (
                ProgressEventKind.AGENT_PROVIDER_STALLED
            ),
        }[activity.kind]
        message = {
            AgentExecutionActivityKind.PROVIDER_STREAM: (
                f"{agent.label} received provider stream activity"
            ),
            AgentExecutionActivityKind.TOOL_STARTED: (
                f"{agent.label} started a sandboxed tool operation"
            ),
            AgentExecutionActivityKind.TOOL_COMPLETED: (
                f"{agent.label} completed a sandboxed tool operation"
            ),
            AgentExecutionActivityKind.LIVENESS_DEGRADED: (
                f"{agent.label} provider liveness is degraded: "
                f"{activity.degradation_reason}; SAT will preserve the call "
                "instead of guessing that silence means a stall"
            ),
            AgentExecutionActivityKind.STALL_SUSPECTED: (
                f"{agent.label} has produced no trusted activity for "
                f"{activity.inactivity_ms / 1000:.1f}s; SAT is checking its "
                "private stream and attributable tool state for another "
                f"{activity.stall_grace_seconds:g}s before interruption "
                f"({activity.policy_source})"
            ),
            AgentExecutionActivityKind.STALL_RECOVERED: (
                f"{agent.label} provider activity recovered during the "
                f"{activity.stall_grace_seconds:g}s grace period"
            ),
            AgentExecutionActivityKind.PROVIDER_STALLED: (
                f"{agent.label} provider remained silent for "
                f"{activity.silence_seconds:g}s; SAT is interrupting only this "
                "invocation and preserving its evidence"
            ),
        }[activity.kind]
        self._emit_activity(
            agent,
            kind=kind,
            message=message,
            attempt=attempt,
            model=activity.model,
            duration_ms=activity.elapsed_ms,
        )

    def _execute(
        self,
        request: AgentExecutionRequest,
        *,
        activity_handler: Callable[[AgentExecutionActivity], None],
    ) -> AgentExecutionResult:
        started_at = _utc(self.clock)
        try:
            return self.executor.execute(
                request,
                activity_handler=activity_handler,
            )
        except Exception as error:
            finished_at = _utc(self.clock)
            duration_ms = max(
                0,
                round((finished_at - started_at).total_seconds() * 1000),
            )
            detail = self._error_detail(error)
            return AgentExecutionResult(
                status=AgentExecutionStatus.LAUNCH_FAILED,
                error=f"Agent executor raised {type(error).__name__}: {detail}",
                telemetry=AgentExecutionTelemetry(
                    role=None,
                    agent_id=request.agent_id,
                    capability=request.capability,
                    session_key=request.session_key,
                    command=("agent-executor",),
                    started_at=started_at,
                    finished_at=finished_at,
                    duration_ms=duration_ms,
                    exit_code=None,
                ),
            )

    def _validate_execution_result(
        self,
        result: AgentExecutionResult,
        request: AgentExecutionRequest,
    ) -> None:
        telemetry = result.telemetry
        if (
            telemetry.role is not None
            or telemetry.agent_id != request.agent_id
            or telemetry.capability is not request.capability
            or telemetry.session_key != request.session_key
        ):
            raise DynamicAgentRunnerError(
                "execution telemetry identity differs from the approved AgentSpec",
                TerminationReason.SAFETY_BOUNDARY_CROSSED,
            )
        if result.status is not AgentExecutionStatus.COMPLETED:
            return
        if telemetry.model is None:
            raise DynamicAgentRunnerError(
                "successful execution omitted model metadata",
                TerminationReason.DEPENDENCY_UNAVAILABLE,
            )
        if telemetry.model != request.model:
            raise DynamicAgentRunnerError(
                "execution model differs from the approved model route",
                TerminationReason.SAFETY_BOUNDARY_CROSSED,
            )
        if telemetry.provider is None:
            raise DynamicAgentRunnerError(
                "successful execution omitted provider metadata",
                TerminationReason.DEPENDENCY_UNAVAILABLE,
            )
        if (
            telemetry.usage is None
            or telemetry.usage.input_tokens is None
            or telemetry.usage.output_tokens is None
        ):
            raise DynamicAgentRunnerError(
                "successful execution omitted token usage",
                TerminationReason.DEPENDENCY_UNAVAILABLE,
            )
        if sandbox_error := self._sandbox_runtime_error(result):
            raise DynamicAgentRunnerError(
                sandbox_error,
                TerminationReason.DEPENDENCY_UNAVAILABLE,
            )
        if request.capability is AgentCapability.REVIEW:
            if telemetry.tool_evidence_status is AgentToolEvidenceStatus.INVALID:
                raise DynamicAgentRunnerError(
                    "Reviewer session evidence failed integrity validation: "
                    f"{telemetry.tool_evidence_error or 'unknown session error'}",
                    TerminationReason.SAFETY_BOUNDARY_CROSSED,
                )
            if telemetry.tool_evidence_status is not AgentToolEvidenceStatus.CAPTURED:
                raise DynamicAgentRunnerError(
                    "Reviewer execution omitted attributable session tool evidence",
                    TerminationReason.DEPENDENCY_UNAVAILABLE,
                )

    def _assemble_response(
        self,
        agent: AgentSpec,
        body: object,
        *,
        input_commit: str,
        commands: tuple[CommandEvidence, ...],
        manual_scope: tuple[str, ...],
        snapshot: GitSnapshot | None,
    ) -> WorkResult | TestReport | ReviewReport:
        created_at = _utc(self.clock)
        if agent.capability in {
            AgentCapability.IMPLEMENTATION,
            AgentCapability.INTEGRATION,
        }:
            if not isinstance(body, WorkResultResponse):
                raise ArtifactAssemblyError(
                    "implementation Agent returned the wrong semantic body"
                )
            if snapshot is None:
                raise DynamicAgentRunnerError(
                    "implementation Agent produced no committed change",
                    TerminationReason.NO_RELEVANT_CHANGE,
                )
            return assemble_work_result(
                body,
                task_brief=self.task_brief,
                team_id=self.team_plan.team_id,
                agent=agent,
                snapshot=snapshot,
                created_at=created_at,
            )
        if agent.capability is AgentCapability.TESTING:
            if not isinstance(body, TestReportResponse):
                raise ArtifactAssemblyError("Tester returned the wrong semantic body")
            return assemble_test_report(
                body,
                task_brief=self.task_brief,
                team_id=self.team_plan.team_id,
                agent=agent,
                iteration=self.iteration,
                input_commit=input_commit,
                commands=commands,
                manual_review_criteria=self.manual_review_criteria,
                created_at=created_at,
            )
        if agent.capability is AgentCapability.REVIEW:
            if not isinstance(body, GroundedReviewReportResponse):
                raise ArtifactAssemblyError("Reviewer returned the wrong semantic body")
            return assemble_review_report(
                body,
                task_brief=self.task_brief,
                team_id=self.team_plan.team_id,
                agent=agent,
                iteration=self.iteration,
                input_commit=input_commit,
                reviewed_criteria=manual_scope,
                created_at=created_at,
            )
        raise ArtifactAssemblyError(
            f"unsupported dynamic capability: {agent.capability.value}"
        )

    def _begin_writer(self, agent: AgentSpec) -> str:
        if agent.capability not in {
            AgentCapability.IMPLEMENTATION,
            AgentCapability.INTEGRATION,
        }:
            raise DynamicAgentRunnerError(
                "write permission was assigned to a non-implementation Agent",
                TerminationReason.SAFETY_BOUNDARY_CROSSED,
            )
        with self._state_lock:
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
        with self._state_lock:
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

    def _ensure_quality_evidence(self) -> tuple[tuple[CommandEvidence, ...], str]:
        with self._quality_condition:
            while self._quality_state == "running":
                self._quality_condition.wait()
            if self._quality_state == "ready":
                assert self._quality_commit is not None
                return self._quality_commands, self._quality_commit
            if self._quality_state == "failed":
                assert self._quality_error is not None
                raise self._quality_error
            self._quality_state = "running"
            expected_commit = self._latest_commit
            self._quality_gate_calls += 1

        try:
            self.workspace_manager.verify_workspace(
                self.workspace,
                expected_commit=expected_commit,
                require_clean=True,
            )
            commands = self.quality_gate.run(iteration=self.iteration)
            validate_verification_assignment(
                self.task_brief,
                commands,
                self.manual_review_criteria,
            )
            self.workspace_manager.verify_workspace(
                self.workspace,
                expected_commit=expected_commit,
                require_clean=True,
            )
            controller_test: ArtifactReference | None = None
            if not any(
                agent.capability is AgentCapability.TESTING
                for agent in self.team_plan.agents
            ):
                report = assemble_test_report(
                    None,
                    task_brief=self.task_brief,
                    team_id=self.team_plan.team_id,
                    agent=None,
                    iteration=self.iteration,
                    input_commit=expected_commit,
                    commands=commands,
                    manual_review_criteria=self.manual_review_criteria,
                    created_at=_utc(self.clock),
                )
                controller_test = self.artifact_store.write(
                    report,
                    description="Controller-owned deterministic test evidence.",
                )
        except Exception as error:
            classified = DynamicAgentRunnerError(
                self._error_detail(error),
                self._classify_exception(error),
            )
            with self._quality_condition:
                self._quality_error = classified
                self._quality_state = "failed"
                self._quality_condition.notify_all()
            raise classified from error

        with self._quality_condition:
            self._quality_commands = commands
            self._quality_commit = expected_commit
            self._controller_test_reference = controller_test
            self._quality_state = "ready"
            self._quality_condition.notify_all()
            return commands, expected_commit

    def _upstream_results(
        self,
        agent: AgentSpec,
        upstream: Mapping[str, AgentRunOutcome],
    ) -> tuple[DynamicUpstreamResult, ...]:
        if set(upstream) != set(agent.dependencies):
            raise DynamicAgentRunnerError(
                "scheduler handoffs do not exactly cover Agent dependencies",
                TerminationReason.CONTROLLER_ERROR,
            )
        results: list[DynamicUpstreamResult] = []
        for dependency in agent.dependencies:
            outcome = upstream[dependency]
            if outcome.status is not AgentRunStatus.COMPLETED or outcome.output is None:
                raise DynamicAgentRunnerError(
                    f"dependency {dependency} did not complete",
                    TerminationReason.DEPENDENCY_UNAVAILABLE,
                )
            artifact = self.artifact_store.load(outcome.output)
            if not isinstance(artifact, (WorkResult, TestReport, ReviewReport)):
                raise DynamicAgentRunnerError(
                    f"dependency {dependency} produced an unsupported artifact",
                    TerminationReason.ARTIFACT_INVALID,
                )
            results.append(
                DynamicUpstreamResult(
                    agent_id=dependency,
                    status=HandoffStatus.COMPLETED,
                    summary=_bounded_artifact_summary(
                        artifact.summary,
                        limit=_UPSTREAM_SUMMARY_LIMIT,
                    ),
                    output_commit=(
                        artifact.output_commit
                        if isinstance(artifact, WorkResult)
                        else artifact.input_commit
                    ),
                    completed_task_ids=(
                        artifact.completed_tasks
                        if isinstance(artifact, WorkResult)
                        else ()
                    ),
                )
            )
        return tuple(results)

    def _persist_incoming_handoffs(
        self,
        agent: AgentSpec,
        upstream: Mapping[str, AgentRunOutcome],
    ) -> tuple[ArtifactReference, ...]:
        self._upstream_results(agent, upstream)
        references: list[ArtifactReference] = []
        for dependency in agent.dependencies:
            outcome = upstream[dependency]
            assert outcome.output is not None
            source = self.team_plan.get_agent(dependency)
            artifact = self.artifact_store.load(outcome.output)
            assert isinstance(artifact, (WorkResult, TestReport, ReviewReport))
            execution_evidence = tuple(
                reference
                for reference in outcome.evidence
                if reference.kind is ArtifactKind.AGENT_EXECUTION_RECORD
            )
            references.append(
                self._write_handoff(
                    source=source,
                    target_agent_id=agent.id,
                    status=HandoffStatus.COMPLETED,
                    input_commit=(
                        artifact.output_commit
                        if isinstance(artifact, WorkResult)
                        else artifact.input_commit
                    ),
                    artifacts=(outcome.output, *execution_evidence),
                    summary=(
                        f"{source.label} supplied controller-verified evidence "
                        f"to {agent.label}."
                    ),
                    blockers=(),
                )
            )
        return tuple(references)

    def _persist_terminal_handoff(
        self,
        agent: AgentSpec,
        *,
        output: ArtifactReference | None,
        executions: tuple[ArtifactReference, ...],
        failed_detail: str | None,
    ) -> tuple[ArtifactReference, ...]:
        has_downstream = any(
            agent.id in candidate.dependencies for candidate in self.team_plan.agents
        )
        if failed_detail is None and has_downstream:
            return ()
        try:
            if output is not None:
                artifact = self.artifact_store.load(output)
                assert isinstance(artifact, (WorkResult, TestReport, ReviewReport))
                input_commit = (
                    artifact.output_commit
                    if isinstance(artifact, WorkResult)
                    else artifact.input_commit
                )
                status = HandoffStatus.COMPLETED
                artifacts = (output, *executions)
                summary = f"{agent.label} supplied its terminal run evidence."
                blockers: tuple[str, ...] = ()
            else:
                with self._state_lock:
                    input_commit = self._latest_commit
                status = HandoffStatus.FAILED
                artifacts = executions
                summary = f"{agent.label} reported a terminal execution failure."
                blockers = (failed_detail or "Agent execution failed.",)
            return (
                self._write_handoff(
                    source=agent,
                    target_agent_id=None,
                    status=status,
                    input_commit=input_commit,
                    artifacts=artifacts,
                    summary=summary,
                    blockers=blockers,
                ),
            )
        except Exception:
            if failed_detail is None:
                raise
            return ()

    def _write_handoff(
        self,
        *,
        source: AgentSpec,
        target_agent_id: str | None,
        status: HandoffStatus,
        input_commit: str,
        artifacts: tuple[ArtifactReference, ...],
        summary: str,
        blockers: tuple[str, ...],
    ) -> ArtifactReference:
        with self._state_lock:
            sequence = self._handoff_sequences.get(source.stage_id, 0) + 1
            self._handoff_sequences[source.stage_id] = sequence
            envelope = HandoffEnvelope(
                run_id=self.task_brief.run_id,
                team_id=self.team_plan.team_id,
                iteration=self.iteration,
                stage=source.stage_id,
                sequence=sequence,
                source_agent_id=source.id,
                target_agent_id=target_agent_id,
                status=status,
                created_at=_utc(self.clock),
                summary=summary,
                input_commit=input_commit,
                artifacts=list(self._unique_references(artifacts)),
                blockers=list(blockers),
            )
            reference = self.artifact_store.write(
                envelope,
                description=(
                    f"Handoff from {source.id} to {target_agent_id or 'controller'}."
                ),
            )
            self._handoffs.append(reference)
            return reference

    def _record_execution_reference(
        self,
        agent_id: str,
        reference: ArtifactReference,
    ) -> None:
        with self._state_lock:
            self._execution_records.append(reference)
            self._execution_by_agent.setdefault(agent_id, []).append(reference)

    def _execution_references(
        self,
        agent_id: str,
    ) -> tuple[ArtifactReference, ...]:
        with self._state_lock:
            return tuple(self._execution_by_agent.get(agent_id, ()))

    def _quality_auxiliary_references(
        self,
        agent: AgentSpec,
    ) -> tuple[ArtifactReference, ...]:
        if agent.capability not in {AgentCapability.TESTING, AgentCapability.REVIEW}:
            return ()
        with self._state_lock:
            return (
                ()
                if self._controller_test_reference is None
                else (self._controller_test_reference,)
            )

    @staticmethod
    def _unique_references(
        values: tuple[ArtifactReference, ...],
    ) -> tuple[ArtifactReference, ...]:
        return tuple({item.path: item for item in values}.values())

    @staticmethod
    def _execution_termination_reason(
        status: AgentExecutionStatus,
    ) -> TerminationReason:
        if status is AgentExecutionStatus.TIMED_OUT:
            return TerminationReason.RESOURCE_LIMIT_REACHED
        if status is AgentExecutionStatus.INTERRUPTED:
            return TerminationReason.USER_INTERRUPTED
        if status in {
            AgentExecutionStatus.LAUNCH_FAILED,
            AgentExecutionStatus.PROVIDER_FAILED,
            AgentExecutionStatus.PROVIDER_STALLED,
        }:
            return TerminationReason.DEPENDENCY_UNAVAILABLE
        if status is AgentExecutionStatus.INVALID_RESPONSE:
            return TerminationReason.ARTIFACT_INVALID
        return TerminationReason.EXECUTION_FAILED

    @staticmethod
    def _classify_exception(error: Exception) -> TerminationReason:
        if isinstance(error, DynamicAgentRunnerError):
            return error.reason
        if isinstance(error, AgentBudgetExceeded):
            return TerminationReason.RESOURCE_LIMIT_REACHED
        if isinstance(error, QualityGateBudgetExceeded):
            return TerminationReason.RESOURCE_LIMIT_REACHED
        if isinstance(error, SandboxUnavailableError):
            return TerminationReason.DEPENDENCY_UNAVAILABLE
        if isinstance(error, WorkspaceIntegrityError):
            if "no new commit" in str(error) or "no changed files" in str(error):
                return TerminationReason.NO_RELEVANT_CHANGE
            return TerminationReason.SAFETY_BOUNDARY_CROSSED
        if isinstance(error, GitWorkspaceError):
            return TerminationReason.SAFETY_BOUNDARY_CROSSED
        if isinstance(
            error,
            (
                AgentArtifactResponseError,
                AgentPromptError,
                ArtifactAssemblyError,
                ArtifactStoreError,
                ValidationError,
            ),
        ):
            return TerminationReason.ARTIFACT_INVALID
        if isinstance(error, QualityGateError):
            return TerminationReason.EXECUTION_FAILED
        return TerminationReason.CONTROLLER_ERROR

    @staticmethod
    def _sandbox_runtime_error(result: AgentExecutionResult) -> str | None:
        for line in result.telemetry.stderr.splitlines():
            if "[tools]" not in line or " failed:" not in line:
                continue
            if (
                "Error response from daemon: container " in line
                and " is not running" in line
            ) or "Cannot connect to the Docker daemon" in line:
                return "OpenClaw sandbox became unavailable during Agent tool use"
        return None

    @staticmethod
    def _error_detail(error: Exception) -> str:
        return str(error).strip() or type(error).__name__
