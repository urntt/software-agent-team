"""Tests for adaptive Planning dialogue, validation, approval, and evidence."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

import software_agent_team.planning as planning
from software_agent_team.artifacts import AcceptanceCriterion
from software_agent_team.budgets import AgentBudget
from software_agent_team.execution import ScriptedAgentExecutor
from software_agent_team.integrity import canonical_model_sha256
from software_agent_team.model_routing import ModelProfile, ModelRoutingPolicy
from software_agent_team.planning import (
    AdaptivePlanningCoordinator,
    AgentWorkload,
    ApprovedPlanningResult,
    CapabilityTimeoutPolicy,
    PlanningActivity,
    PlanningActivityKind,
    PlanningError,
    PlanningIntegrityError,
    PlanningModelResponse,
    PlanningOption,
    PlanningPolicy,
    PlanningProposal,
    PlanningProposalBody,
    PlanningProposalSource,
    PlanningQuestion,
    PlanningRequest,
    PlanningResponseKind,
    PlanningSessionStatus,
    PlanningStore,
    ProposedAgent,
    ProposedCriterion,
    ProposedTask,
    StructuredEditKind,
    StructuredPlanEdit,
    TerminalPlanningProgress,
    apply_structured_edit,
    preview_adaptive_proposal,
    render_planning_overview,
    run_interactive_planning,
)
from software_agent_team.teams import (
    AgentCapability,
    ModelRouteSelectionSource,
    ModelRoutingMode,
    ModelSwitchCondition,
    PermissionProfile,
    TeamPlanOrigin,
)

FIXED_TIME = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


class AdvancingClock:
    """Return deterministic increasing timestamps for persisted evidence."""

    def __init__(self) -> None:
        self.current = FIXED_TIME

    def __call__(self) -> datetime:
        value = self.current
        self.current += timedelta(seconds=1)
        return value


def request() -> PlanningRequest:
    """Return direct input with explicit pre-model authorization."""

    return PlanningRequest(
        run_id="sat-adaptive-001",
        project_name="link-checker",
        source_request="Build a CLI that checks Markdown links.",
        destination="/tmp/link-checker",
        execution_profile=(
            "Small greenfield Python 3.12 project",
            "No network access during deterministic verification",
        ),
        base_constraints=("Use the versioned uv environment",),
        model="provider/model",
        authorization="user_confirmed",
        authorized_at=FIXED_TIME,
    )


def policy(**updates: object) -> PlanningPolicy:
    """Return the controller authority used by adaptive plan tests."""

    values: dict[str, object] = {
        "budget": AgentBudget(
            max_calls=14,
            max_input_tokens=1_000_000,
            max_output_tokens=200_000,
            max_agent_duration_seconds=7_200,
            max_estimated_cost_usd="25",
        ),
        "capability_timeouts": {
            AgentCapability.IMPLEMENTATION: CapabilityTimeoutPolicy(
                default_seconds=600,
                ceiling_seconds=900,
            ),
            AgentCapability.INTEGRATION: CapabilityTimeoutPolicy(
                default_seconds=600,
                ceiling_seconds=900,
            ),
            AgentCapability.TESTING: CapabilityTimeoutPolicy(
                default_seconds=240,
                ceiling_seconds=300,
            ),
            AgentCapability.REVIEW: CapabilityTimeoutPolicy(
                default_seconds=240,
                ceiling_seconds=300,
            ),
        },
    }
    values.update(updates)
    return PlanningPolicy.model_validate(values)


def proposal_body(*, title: str = "Markdown Link Checker") -> PlanningProposalBody:
    """Return one complete task-defined team proposal."""

    return PlanningProposalBody(
        title=title,
        requirements=(
            "Scan Markdown files below a selected path.",
            "Report broken local links with source locations.",
        ),
        acceptance_criteria=(
            ProposedCriterion(
                id="AC_SCAN",
                description="Broken local links produce a non-zero exit status.",
                verification="Run the CLI against valid and broken fixtures.",
            ),
            ProposedCriterion(
                id="AC_REPORT",
                description="Every failure includes the file and line number.",
                verification="Assert structured output for a broken fixture.",
            ),
        ),
        constraints=("Use only the standard library at runtime.",),
        assumptions=("Only local file and fragment links are in scope.",),
        objective="Deliver a documented, tested Markdown link-checking CLI.",
        approach=(
            "Parse Markdown link targets without fetching network resources.",
            "Separate scanning, resolution, and CLI presentation.",
        ),
        tasks=(
            ProposedTask(
                id="TASK_IMPLEMENT",
                owner_agent_id="cli_developer",
                description="Implement scanning, resolution, and CLI output.",
                acceptance_criteria=("AC_SCAN", "AC_REPORT"),
                expected_paths=("src", "tests"),
            ),
        ),
        risks=("Markdown edge cases may require explicit documented limits.",),
        agents=(
            ProposedAgent(
                id="cli_developer",
                label="CLI Developer",
                responsibility="Implement the complete CLI and its focused tests.",
                rationale="The small cohesive codebase does not need split writers.",
                capability=AgentCapability.IMPLEMENTATION,
                stage_id="implement",
                workspace_scope="repository",
                workload=AgentWorkload.ROUTINE,
            ),
            ProposedAgent(
                id="acceptance_tester",
                label="Acceptance Tester",
                responsibility="Verify deterministic behavior against every criterion.",
                rationale="Testing remains independent from implementation.",
                capability=AgentCapability.TESTING,
                stage_id="verify",
                dependencies=("cli_developer",),
                workspace_scope="repository",
                workload=AgentWorkload.ROUTINE,
            ),
            ProposedAgent(
                id="quality_reviewer",
                label="Quality Reviewer",
                responsibility="Review correctness, maintainability, and evidence.",
                rationale="A separate reviewer prevents self-approval.",
                capability=AgentCapability.REVIEW,
                stage_id="verify",
                dependencies=("cli_developer",),
                workspace_scope="repository",
                workload=AgentWorkload.ROUTINE,
            ),
        ),
        iteration_limit=2,
        max_concurrency=2,
        revision_enabled=True,
    )


def proposal(
    *,
    revision: int = 1,
    body: PlanningProposalBody | None = None,
) -> PlanningProposal:
    return PlanningProposal(
        run_id=request().run_id,
        revision=revision,
        created_at=FIXED_TIME,
        source=PlanningProposalSource.MODEL,
        source_turn_sequence=revision,
        body=body or proposal_body(),
    )


def response(value: PlanningModelResponse) -> str:
    return value.model_dump_json()


def question_response() -> PlanningModelResponse:
    return PlanningModelResponse(
        kind=PlanningResponseKind.QUESTION,
        question=PlanningQuestion(
            id="link_scope",
            text="Should the first version check web links too?",
            why="Network access changes reliability and test architecture.",
            options=(
                PlanningOption(
                    id="local_only",
                    label="Local only",
                    description="Check files and fragments deterministically.",
                ),
                PlanningOption(
                    id="include_web",
                    label="Include web",
                    description="Add network requests and retry behavior.",
                ),
            ),
        ),
    )


def proposal_response(
    body: PlanningProposalBody | None = None,
) -> PlanningModelResponse:
    return PlanningModelResponse(
        kind=PlanningResponseKind.PROPOSAL,
        proposal=body or proposal_body(),
    )


def test_planning_request_requires_explicit_pre_model_authorization() -> None:
    payload = request().model_dump(mode="json")
    payload["authorization"] = "assumed"

    with pytest.raises(ValidationError, match="user_confirmed"):
        PlanningRequest.model_validate(payload)


def test_question_requires_suggestions_and_preserves_custom_answers() -> None:
    question = question_response().question

    assert question is not None
    assert len(question.options) == 2
    assert question.allow_custom


def test_response_normalizer_canonicalizes_only_safe_presentation_variants() -> None:
    payload = proposal_response().model_dump(mode="json")
    payload.pop("kind")
    payload["proposal"]["tasks"][0]["expected_paths"] = [
        " tests/ ",
        "./src//link_checker.py",
    ]
    payload["proposal"]["agents"][0]["workspace_scope"] = "repository/"
    original = json.loads(json.dumps(payload))

    normalized, changes = planning._normalize_planning_response_payload(payload)
    parsed = PlanningModelResponse.model_validate(normalized)

    assert payload == original
    assert parsed.kind is PlanningResponseKind.PROPOSAL
    assert parsed.proposal is not None
    assert parsed.proposal.tasks[0].expected_paths == (
        "tests",
        "src/link_checker.py",
    )
    assert parsed.proposal.agents[0].workspace_scope == "repository"
    assert changes == (
        "inferred response kind as proposal",
        "canonicalized proposal.tasks[0].expected_paths[0]",
        "canonicalized proposal.tasks[0].expected_paths[1]",
        "canonicalized proposal.agents[0].workspace_scope",
    )


def test_response_normalizer_leaves_unsafe_paths_for_strict_rejection() -> None:
    payload = proposal_response().model_dump(mode="json")
    payload["proposal"]["tasks"][0]["expected_paths"] = ["../secret/"]

    normalized, changes = planning._normalize_planning_response_payload(payload)

    assert normalized["proposal"]["tasks"][0]["expected_paths"] == ["../secret/"]
    assert changes == ()
    with pytest.raises(ValidationError, match="canonical safe relative POSIX paths"):
        PlanningModelResponse.model_validate(normalized)


def test_proposed_workspace_scope_cannot_repeat_the_destination_directory() -> None:
    payload = proposal_response().model_dump(mode="json")
    payload["proposal"]["agents"][0]["workspace_scope"] = "link-checker"

    with pytest.raises(ValidationError, match="must start at repository"):
        PlanningModelResponse.model_validate(payload)


def test_proposal_compiles_to_complete_controller_owned_authority() -> None:
    preview = preview_adaptive_proposal(
        request(),
        proposal(),
        policy(),
        created_at=FIXED_TIME,
    )

    assert preview.task_brief.confirmed
    assert preview.team_plan.origin is TeamPlanOrigin.ADAPTIVE_PLANNING
    assert preview.team_plan.execution_waves() == (
        ("cli_developer",),
        ("acceptance_tester", "quality_reviewer"),
    )
    assert (
        preview.team_plan.get_agent("cli_developer").permission_profile
        is PermissionProfile.WORKSPACE_WRITE
    )
    assert (
        preview.team_plan.get_agent("quality_reviewer").permission_profile
        is PermissionProfile.READ_ONLY
    )
    assert preview.team_plan.model_routes.routes[0].model == "provider/model"
    assert preview.team_plan.task_brief_sha256 == canonical_model_sha256(
        preview.task_brief
    )

    overview = render_planning_overview(preview)
    assert "Destination: /tmp/link-checker" in overview
    assert "Small greenfield Python 3.12 project" in overview
    assert "Runtime Agents" in overview
    assert (
        "execution order: cli_developer -> acceptance_tester + quality_reviewer"
        in overview
    )
    assert "permission: workspace_write" in overview
    assert "timeout: 600 seconds" in overview
    assert "model: provider/model" in overview
    assert "model calls: 14" in overview
    assert "cumulative Agent time: 7200 seconds" in overview
    assert "estimated cost ceiling: $25" in overview


def test_controller_resolves_visible_per_agent_model_routes_before_approval() -> None:
    default = ModelProfile(
        id="default",
        model="provider/model",
        capabilities=(
            AgentCapability.CLARIFICATION,
            AgentCapability.PLANNING,
            AgentCapability.IMPLEMENTATION,
            AgentCapability.TESTING,
            AgentCapability.REVIEW,
        ),
        input_cost_per_million_usd="0.50",
        output_cost_per_million_usd="1.50",
    )
    quality = ModelProfile(
        id="quality",
        model="provider/quality",
        capabilities=(AgentCapability.TESTING, AgentCapability.REVIEW),
        priority=10,
    )
    routing = ModelRoutingPolicy(
        mode=ModelRoutingMode.POLICY,
        profiles=(default, quality),
        default_profile_id="default",
        capability_profile_overrides={AgentCapability.TESTING: "quality"},
        authorized_switch_conditions=(ModelSwitchCondition.PROVIDER_FAILURE,),
        max_switches_per_agent=1,
    )

    preview = preview_adaptive_proposal(
        request(),
        proposal(),
        policy(model_routing=routing),
        created_at=FIXED_TIME,
    )

    assignments = {
        assignment.agent_id: assignment
        for assignment in preview.team_plan.model_routes.assignments
    }
    assert assignments["cli_developer"].primary_route_id == "default"
    assert assignments["acceptance_tester"].primary_route_id == "quality"
    assert (
        assignments["acceptance_tester"].selection_source
        is ModelRouteSelectionSource.CAPABILITY_OVERRIDE
    )
    assert assignments["quality_reviewer"].primary_route_id == "default"
    overview = render_planning_overview(preview)
    assert "provider/quality (profile quality; capability_override)" in overview
    assert "model reason:" in overview
    assert "model pricing: not configured" in overview
    assert "$0.50 input / $1.50 output per million tokens" in overview
    assert (
        "authorized fallback profiles: default: provider/model "
        "(pricing: $0.50 input / $1.50 output per million tokens)"
    ) in overview
    assert "model routing: policy" in overview


def test_user_can_override_one_agent_model_without_editing_plan_json() -> None:
    routing = ModelRoutingPolicy(
        mode=ModelRoutingMode.POLICY,
        profiles=(
            ModelProfile(
                id="default",
                model="provider/model",
                capabilities=tuple(AgentCapability),
            ),
            ModelProfile(
                id="quality",
                model="provider/quality",
                capabilities=(AgentCapability.TESTING, AgentCapability.REVIEW),
            ),
        ),
        default_profile_id="default",
    )
    edited = apply_structured_edit(
        proposal(),
        StructuredPlanEdit(
            kind=StructuredEditKind.AGENT_MODEL,
            agent_id="quality_reviewer",
            value="quality",
        ),
        created_at=FIXED_TIME + timedelta(seconds=1),
    )

    preview = preview_adaptive_proposal(
        request(),
        edited,
        policy(model_routing=routing),
        created_at=edited.created_at,
    )

    assignment = preview.team_plan.model_routes.get_assignment("quality_reviewer")
    assert assignment.primary_route_id == "quality"
    assert assignment.selection_source is ModelRouteSelectionSource.AGENT_OVERRIDE
    assert edited.model_profile_overrides == {"quality_reviewer": "quality"}


def test_single_profile_plan_editor_does_not_offer_a_hidden_model_option() -> None:
    strict_routing = ModelRoutingPolicy(
        mode=ModelRoutingMode.STRICT,
        profiles=(
            ModelProfile(
                id="default",
                model="provider/model",
                capabilities=tuple(AgentCapability),
            ),
        ),
        default_profile_id="default",
    )
    answers = iter(("4", "x"))
    output: list[str] = []

    edit = planning._read_structured_edit(
        proposal(),
        model_routing=strict_routing,
        read=lambda _prompt: next(answers),
        write=output.append,
    )

    assert edit is None
    assert "  4. One Agent model profile" not in output
    assert "Choose 1, 2, 3, or x." in output


def test_planning_rejects_missing_model_capability_and_fallback_overbudget() -> None:
    missing_testing = ModelRoutingPolicy(
        mode=ModelRoutingMode.POLICY,
        profiles=(
            ModelProfile(
                id="default",
                model="provider/model",
                capabilities=(
                    AgentCapability.CLARIFICATION,
                    AgentCapability.PLANNING,
                    AgentCapability.IMPLEMENTATION,
                    AgentCapability.REVIEW,
                ),
            ),
        ),
        default_profile_id="default",
    )
    with pytest.raises(PlanningError, match="no authorized model profile"):
        preview_adaptive_proposal(
            request(),
            proposal(),
            policy(model_routing=missing_testing),
            created_at=FIXED_TIME,
        )

    switching = ModelRoutingPolicy(
        mode=ModelRoutingMode.POLICY,
        profiles=(
            ModelProfile(
                id="default",
                model="provider/model",
                capabilities=tuple(AgentCapability),
                priority=1,
            ),
            ModelProfile(
                id="fallback",
                model="provider/fallback",
                capabilities=tuple(AgentCapability),
                priority=2,
            ),
        ),
        default_profile_id="default",
        authorized_switch_conditions=(ModelSwitchCondition.PROVIDER_FAILURE,),
        max_switches_per_agent=1,
    )
    constrained_budget = AgentBudget(
        max_calls=10,
        max_input_tokens=1_000_000,
        max_output_tokens=200_000,
        max_agent_duration_seconds=7_200,
        max_estimated_cost_usd="25",
    )
    with pytest.raises(ValueError, match="fallback invocations exceed"):
        preview_adaptive_proposal(
            request(),
            proposal(),
            policy(model_routing=switching, budget=constrained_budget),
            created_at=FIXED_TIME,
        )


def test_controller_resolves_workload_classes_without_model_timeout_authority() -> None:
    body = proposal_body()
    workloads = {
        "cli_developer": AgentWorkload.COMPLEX,
        "acceptance_tester": AgentWorkload.SUBSTANTIAL,
        "quality_reviewer": AgentWorkload.ROUTINE,
    }
    agents = tuple(
        agent.model_copy(update={"workload": workloads[agent.id]})
        for agent in body.agents
    )

    preview = preview_adaptive_proposal(
        request(),
        proposal(body=body.model_copy(update={"agents": agents})),
        policy(),
        created_at=FIXED_TIME,
    )

    assert {agent.id: agent.timeout_seconds for agent in preview.team_plan.agents} == {
        "cli_developer": 900,
        "acceptance_tester": 270,
        "quality_reviewer": 240,
    }
    assert all(
        resolution.source == "policy_workload"
        for resolution in preview.timeout_resolutions
    )
    assert "timeout_seconds" not in proposal_response().model_dump_json()


def test_controller_raises_reviewer_timeout_floor_from_exact_scope() -> None:
    profile_criteria = tuple(
        AcceptanceCriterion(
            id=f"AC_PROFILE_{index}",
            description=f"The project satisfies profile condition {index}.",
            verification=f"Verify profile condition {index} independently.",
        )
        for index in range(1, 5)
    )
    configured = policy(profile_acceptance_criteria=profile_criteria)

    preview = preview_adaptive_proposal(
        request(),
        proposal(),
        configured,
        created_at=FIXED_TIME,
    )

    reviewer = preview.team_plan.get_agent("quality_reviewer")
    resolution = next(
        item
        for item in preview.timeout_resolutions
        if item.agent_id == "quality_reviewer"
    )
    assert len(preview.task_brief.acceptance_criteria) == 6
    assert reviewer.timeout_seconds == 270
    assert resolution.source == "policy_scope_floor"
    assert resolution.workload is AgentWorkload.ROUTINE
    assert resolution.minimum_seconds == 270
    assert resolution.scope_criterion_count == 6
    assert "controller review-scope floor for 6 criteria" in (
        render_planning_overview(preview)
    )
    assert "allowed 270..300" in render_planning_overview(preview)

    too_short = proposal().model_copy(
        update={"timeout_overrides_seconds": {"quality_reviewer": 250}}
    )
    with pytest.raises(PlanningError, match=r"policy envelope of 270\.\.300s"):
        preview_adaptive_proposal(
            request(),
            too_short,
            configured,
            created_at=FIXED_TIME,
        )


def test_review_scope_timeout_thresholds_are_deterministic_and_ordered() -> None:
    configured = policy()

    assert configured.review_scope_workload(5) is AgentWorkload.ROUTINE
    assert configured.review_scope_workload(6) is AgentWorkload.SUBSTANTIAL
    assert configured.review_scope_workload(12) is AgentWorkload.SUBSTANTIAL
    assert configured.review_scope_workload(13) is AgentWorkload.COMPLEX
    with pytest.raises(ValidationError, match="thresholds must be ordered"):
        policy(
            review_substantial_criterion_threshold=13,
            review_complex_criterion_threshold=13,
        )


def test_controller_adds_profile_criteria_without_model_echo() -> None:
    profile_criterion = AcceptanceCriterion(
        id="AC_PROFILE",
        description="The project satisfies the fixed runtime contract.",
        verification="Run the profile contract gate.",
    )
    configured = policy(
        profile_acceptance_criteria=(profile_criterion,),
        require_review_agent=True,
    )

    preview = preview_adaptive_proposal(
        request(),
        proposal(),
        configured,
        created_at=FIXED_TIME,
    )

    assert [criterion.id for criterion in preview.task_brief.acceptance_criteria] == [
        "AC_SCAN",
        "AC_REPORT",
        "AC_PROFILE",
    ]
    assert "AC_PROFILE" not in {
        criterion.id for criterion in proposal().body.acceptance_criteria
    }


def test_controller_rejects_profile_criterion_echo_and_missing_reviewer() -> None:
    profile_criterion = AcceptanceCriterion(
        id="AC_PROFILE",
        description="The project satisfies the fixed runtime contract.",
        verification="Run the profile contract gate.",
    )
    configured = policy(
        profile_acceptance_criteria=(profile_criterion,),
        require_review_agent=True,
    )
    body = proposal_body()
    echoed = ProposedCriterion(
        id="AC_PROFILE",
        description=profile_criterion.description,
        verification=profile_criterion.verification,
    )
    echoed_tasks = (
        body.tasks[0].model_copy(
            update={
                "acceptance_criteria": (
                    *body.tasks[0].acceptance_criteria,
                    "AC_PROFILE",
                )
            }
        ),
    )
    echoed_body = PlanningProposalBody.model_validate(
        body.model_copy(
            update={
                "acceptance_criteria": (*body.acceptance_criteria, echoed),
                "tasks": echoed_tasks,
            }
        )
    )
    with pytest.raises(PlanningError, match="controller-owned profile criteria"):
        preview_adaptive_proposal(
            request(),
            proposal(body=echoed_body),
            configured,
            created_at=FIXED_TIME,
        )

    without_reviewer = PlanningProposalBody.model_validate(
        body.model_copy(
            update={
                "agents": tuple(
                    agent
                    for agent in body.agents
                    if agent.capability is not AgentCapability.REVIEW
                ),
                "max_concurrency": 1,
            }
        )
    )
    with pytest.raises(PlanningError, match="requires an independent review Agent"):
        preview_adaptive_proposal(
            request(),
            proposal(body=without_reviewer),
            configured,
            created_at=FIXED_TIME,
        )


def test_small_task_may_use_one_independent_quality_agent() -> None:
    body = proposal_body()
    agents = tuple(agent for agent in body.agents if agent.id != "acceptance_tester")
    smaller = body.model_copy(update={"agents": agents, "max_concurrency": 1})

    preview = preview_adaptive_proposal(
        request(),
        proposal(body=PlanningProposalBody.model_validate(smaller)),
        policy(),
        created_at=FIXED_TIME,
    )

    assert tuple(agent.id for agent in preview.team_plan.agents) == (
        "cli_developer",
        "quality_reviewer",
    )
    assert preview.team_plan.execution_waves() == (
        ("cli_developer",),
        ("quality_reviewer",),
    )


def test_proposal_rejects_an_implementation_agent_without_tasks() -> None:
    body = proposal_body()
    extra = ProposedAgent(
        id="docs_developer",
        label="Documentation Developer",
        responsibility="Write task documentation.",
        rationale="Documentation has a separate write scope.",
        capability=AgentCapability.IMPLEMENTATION,
        stage_id="implement",
        workspace_scope="repository/docs",
        workload=AgentWorkload.ROUTINE,
    )
    agents = tuple(
        agent.model_copy(
            update={"dependencies": (*agent.dependencies, extra.id)}
            if agent.capability in {AgentCapability.TESTING, AgentCapability.REVIEW}
            else {}
        )
        for agent in (*body.agents, extra)
    )

    with pytest.raises(ValidationError, match="must own at least one task"):
        PlanningProposalBody.model_validate(body.model_copy(update={"agents": agents}))


def test_cross_agent_task_dependencies_require_matching_agent_dependencies() -> None:
    body = proposal_body()
    fixture_agent = ProposedAgent(
        id="fixture_developer",
        label="Fixture Developer",
        responsibility="Build deterministic test fixtures.",
        rationale="Fixture work has an isolated write scope.",
        capability=AgentCapability.IMPLEMENTATION,
        stage_id="implement",
        workspace_scope="repository/tests",
        workload=AgentWorkload.ROUTINE,
    )
    agents = tuple(
        agent.model_copy(
            update={"dependencies": (*agent.dependencies, fixture_agent.id)}
            if agent.capability in {AgentCapability.TESTING, AgentCapability.REVIEW}
            else {}
        )
        for agent in (*body.agents, fixture_agent)
    )
    tasks = (
        ProposedTask(
            id="TASK_FIXTURES",
            owner_agent_id=fixture_agent.id,
            description="Create deterministic valid and broken link fixtures.",
            acceptance_criteria=("AC_SCAN",),
            expected_paths=("tests/fixtures",),
        ),
        body.tasks[0].model_copy(update={"dependencies": ("TASK_FIXTURES",)}),
    )

    with pytest.raises(ValidationError, match="does not depend on fixture_developer"):
        PlanningProposalBody.model_validate(
            body.model_copy(update={"agents": agents, "tasks": tasks})
        )


def test_proposal_cannot_split_final_commit_coverage_across_quality_agents() -> None:
    body = proposal_body()
    fixture_agent = ProposedAgent(
        id="fixture_developer",
        label="Fixture Developer",
        responsibility="Build deterministic test fixtures.",
        rationale="Fixture work has an isolated write scope.",
        capability=AgentCapability.IMPLEMENTATION,
        stage_id="implement",
        workspace_scope="repository/tests",
        workload=AgentWorkload.ROUTINE,
    )
    agents = tuple(
        agent.model_copy(
            update={"dependencies": ("cli_developer",)}
            if agent.id == "acceptance_tester"
            else {"dependencies": ("fixture_developer",)}
            if agent.id == "quality_reviewer"
            else {}
        )
        for agent in (*body.agents, fixture_agent)
    )
    tasks = (
        *body.tasks,
        ProposedTask(
            id="TASK_FIXTURES",
            owner_agent_id=fixture_agent.id,
            description="Create deterministic link-checker fixtures.",
            acceptance_criteria=("AC_SCAN",),
            expected_paths=("tests/fixtures",),
        ),
    )

    with pytest.raises(ValidationError, match="every implementation path"):
        PlanningProposalBody.model_validate(
            body.model_copy(update={"agents": agents, "tasks": tasks})
        )


def test_controller_rejects_quality_dependency_and_timeout_policy_violations() -> None:
    invalid_agents = tuple(
        agent.model_copy(
            update={"dependencies": ("acceptance_tester",)}
            if agent.id == "quality_reviewer"
            else {}
        )
        for agent in proposal_body().agents
    )
    invalid = proposal(
        body=proposal_body().model_copy(update={"agents": invalid_agents})
    )

    with pytest.raises(ValueError, match="must remain independent"):
        preview_adaptive_proposal(
            request(),
            invalid,
            policy(),
            created_at=FIXED_TIME,
        )

    invalid = apply_structured_edit(
        proposal(),
        StructuredPlanEdit(
            kind=StructuredEditKind.AGENT_TIMEOUT,
            agent_id="acceptance_tester",
            value=301,
        ),
        created_at=FIXED_TIME + timedelta(seconds=1),
    )
    with pytest.raises(PlanningError, match=r"policy envelope of 240\.\.300s"):
        preview_adaptive_proposal(
            request(),
            invalid,
            policy(),
            created_at=FIXED_TIME,
        )

    excessive_concurrency = proposal(
        body=proposal_body().model_copy(update={"max_concurrency": 3})
    )
    with pytest.raises(PlanningError, match="policy ceiling of 2"):
        preview_adaptive_proposal(
            request(),
            excessive_concurrency,
            policy(max_concurrency=2),
            created_at=FIXED_TIME,
        )


def test_structured_edits_create_valid_revisions_without_internal_json() -> None:
    first = proposal()

    concurrency = apply_structured_edit(
        first,
        StructuredPlanEdit(kind=StructuredEditKind.MAX_CONCURRENCY, value=1),
        created_at=FIXED_TIME + timedelta(seconds=1),
    )
    iteration = apply_structured_edit(
        concurrency,
        StructuredPlanEdit(kind=StructuredEditKind.ITERATION_LIMIT, value=1),
        created_at=FIXED_TIME + timedelta(seconds=2),
    )
    timeout = apply_structured_edit(
        iteration,
        StructuredPlanEdit(
            kind=StructuredEditKind.AGENT_TIMEOUT,
            agent_id="cli_developer",
            value=750,
        ),
        created_at=FIXED_TIME + timedelta(seconds=3),
    )

    assert concurrency.revision == 2
    assert concurrency.source is PlanningProposalSource.STRUCTURED_EDIT
    assert concurrency.body.max_concurrency == 1
    assert iteration.body.iteration_limit == 1
    assert not iteration.body.revision_enabled
    assert timeout.timeout_overrides_seconds == {"cli_developer": 750}
    assert (
        preview_adaptive_proposal(
            request(),
            timeout,
            policy(),
            created_at=FIXED_TIME,
        )
        .team_plan.get_agent("cli_developer")
        .timeout_seconds
        == 750
    )


def test_store_detects_changed_append_only_turn_evidence(tmp_path: Path) -> None:
    store = PlanningStore(tmp_path / "planning")
    executor = ScriptedAgentExecutor([response(proposal_response())])
    coordinator = AdaptivePlanningCoordinator(
        executor=executor,
        store=store,
        policy=policy(),
        clock=AdvancingClock(),
    )

    created = coordinator.start(
        request(),
        answer_question=lambda _question: pytest.fail("unexpected question"),
    )

    assert created is not None
    turn_path = tmp_path / "planning" / request().run_id / "turns" / "001.json"
    payload = json.loads(turn_path.read_text(encoding="utf-8"))
    assert "response_normalizations" not in payload
    payload["user_message"] = "changed after persistence"
    turn_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(PlanningIntegrityError, match="head digest changed"):
        store.load_session(request().run_id)


def test_store_rejects_unanchored_planning_evidence(tmp_path: Path) -> None:
    store = PlanningStore(tmp_path / "planning")
    coordinator = AdaptivePlanningCoordinator(
        executor=ScriptedAgentExecutor([response(proposal_response())]),
        store=store,
        policy=policy(),
        clock=AdvancingClock(),
    )
    assert (
        coordinator.start(
            request(),
            answer_question=lambda _question: pytest.fail("unexpected question"),
        )
        is not None
    )
    turns = tmp_path / "planning" / request().run_id / "turns"
    (turns / "002.json").write_bytes((turns / "001.json").read_bytes())

    with pytest.raises(PlanningIntegrityError, match="differ from the session anchor"):
        store.load_session(request().run_id)


def test_dialogue_revision_structured_edit_and_approval_are_recoverable(
    tmp_path: Path,
) -> None:
    revised_body = proposal_body(title="Local Markdown Link Checker")
    executor = ScriptedAgentExecutor(
        [
            response(question_response()),
            response(proposal_response()),
            response(proposal_response(revised_body)),
        ]
    )
    store = PlanningStore(tmp_path / "planning")
    coordinator = AdaptivePlanningCoordinator(
        executor=executor,
        store=store,
        policy=policy(),
        clock=AdvancingClock(),
    )
    questions: list[str] = []

    def answer(question: PlanningQuestion) -> str:
        questions.append(question.text)
        return "Only local file and fragment links."

    first = coordinator.start(request(), answer_question=answer)
    assert first is not None
    second = coordinator.revise(
        request(),
        first,
        "Make the local-only scope explicit in the title.",
        answer_question=answer,
    )
    assert second is not None
    third = coordinator.structured_edit(
        request(),
        second,
        StructuredPlanEdit(kind=StructuredEditKind.MAX_CONCURRENCY, value=1),
    )
    approved = coordinator.approve(request(), third)

    assert questions == ["Should the first version check web links too?"]
    assert approved.task_brief.title == "Local Markdown Link Checker"
    assert approved.team_plan.max_concurrency == 1
    assert approved.approval.revision == 3
    assert approved.approval.team_plan_sha256 == canonical_model_sha256(
        approved.team_plan
    )
    assert {
        resolution.agent_id: resolution.resolved_seconds
        for resolution in approved.approval.timeout_resolutions
    } == {agent.id: agent.timeout_seconds for agent in approved.team_plan.agents}
    session = store.load_session(request().run_id)
    assert session.status is PlanningSessionStatus.APPROVED
    assert session.turn_count == 3
    assert session.latest_proposal_revision == 3
    assert session.approved_revision == 3
    assert len(executor.requests) == 3
    assert all(call.role.value == "clarifier" for call in executor.requests)
    assert all(call.timeout_seconds == 180 for call in executor.requests)
    assert "unqualified prohibition" in executor.requests[0].prompt
    assert "top-level input" in executor.requests[0].prompt

    tampered = approved.model_dump(mode="json")
    tampered["team_plan"]["agents"][0]["timeout_seconds"] += 1
    with pytest.raises(ValidationError, match="does not bind the supplied TeamPlan"):
        ApprovedPlanningResult.model_validate(tampered)

    tampered_resolution = approved.model_dump(mode="json")
    resolution = tampered_resolution["approval"]["timeout_resolutions"][0]
    resolution["source"] = "user_override"
    resolution["resolved_seconds"] = (
        resolution["default_seconds"]
        if resolution["resolved_seconds"] != resolution["default_seconds"]
        else resolution["ceiling_seconds"]
    )
    with pytest.raises(ValidationError, match="do not match the TeamPlan"):
        ApprovedPlanningResult.model_validate(tampered_resolution)


def test_store_loads_approval_written_before_scope_timeout_evidence(
    tmp_path: Path,
) -> None:
    store = PlanningStore(tmp_path / "planning")
    coordinator = AdaptivePlanningCoordinator(
        executor=ScriptedAgentExecutor([response(proposal_response())]),
        store=store,
        policy=policy(),
        clock=AdvancingClock(),
    )
    created = coordinator.start(
        request(),
        answer_question=lambda _question: pytest.fail("unexpected question"),
    )
    assert created is not None
    coordinator.approve(request(), created)

    approval_path = tmp_path / "planning" / request().run_id / "approvals" / "001.json"
    payload = json.loads(approval_path.read_text(encoding="utf-8"))
    for resolution in payload["timeout_resolutions"]:
        resolution.pop("minimum_seconds")
        resolution.pop("scope_criterion_count")
    approval_path.write_text(json.dumps(payload), encoding="utf-8")

    session = store.load_session(request().run_id)
    loaded = store.load_approval(request().run_id, 1)

    assert session.status is PlanningSessionStatus.APPROVED
    assert all(item.minimum_seconds is None for item in loaded.timeout_resolutions)
    assert all(
        item.scope_criterion_count is None for item in loaded.timeout_resolutions
    )


def test_invalid_complete_proposal_is_repaired_before_it_is_shown(
    tmp_path: Path,
) -> None:
    invalid_agents = tuple(
        agent.model_copy(
            update={"dependencies": ("acceptance_tester",)}
            if agent.id == "quality_reviewer"
            else {}
        )
        for agent in proposal_body().agents
    )
    invalid_body = proposal_body().model_copy(update={"agents": invalid_agents})
    executor = ScriptedAgentExecutor(
        [
            response(proposal_response(invalid_body)),
            response(proposal_response()),
        ]
    )
    store = PlanningStore(tmp_path / "planning")
    coordinator = AdaptivePlanningCoordinator(
        executor=executor,
        store=store,
        policy=policy(response_repair_limit=1),
        clock=AdvancingClock(),
    )
    activities: list[PlanningActivity] = []

    created = coordinator.start(
        request(),
        answer_question=lambda _question: pytest.fail("unexpected question"),
        activity_handler=activities.append,
    )

    assert created is not None
    assert store.load_session(request().run_id).turn_count == 2
    rejected = store.load_turn(request().run_id, 1)
    assert rejected.parsed_response is None
    assert rejected.validation_error is not None
    assert "must remain independent" in rejected.validation_error
    assert "previous_response_rejected" in executor.requests[1].prompt
    assert [activity.kind for activity in activities] == [
        PlanningActivityKind.WAITING_MODEL,
        PlanningActivityKind.RESPONSE_RECEIVED,
        PlanningActivityKind.REPAIR_SCHEDULED,
        PlanningActivityKind.WAITING_MODEL,
        PlanningActivityKind.RESPONSE_RECEIVED,
        PlanningActivityKind.RESPONSE_VALIDATED,
    ]
    assert [
        (activity.attempt, activity.maximum_attempts) for activity in activities
    ] == [(1, 2), (1, 2), (1, 2), (2, 2), (2, 2), (2, 2)]


def test_terminal_planning_progress_shows_heartbeat_and_stops_cleanly() -> None:
    output: list[str] = []
    progress = TerminalPlanningProgress(
        write=output.append,
        heartbeat_seconds=0.01,
    )
    progress(
        PlanningActivity(
            kind=PlanningActivityKind.WAITING_MODEL,
            attempt=1,
            maximum_attempts=2,
            model="provider/model",
        )
    )
    time.sleep(0.025)
    progress(
        PlanningActivity(
            kind=PlanningActivityKind.RESPONSE_RECEIVED,
            attempt=1,
            maximum_attempts=2,
            model="provider/model",
            duration_ms=25,
            execution_status=planning.AgentExecutionStatus.COMPLETED,
        )
    )
    rendered_at_completion = tuple(output)
    time.sleep(0.025)
    progress.close()

    assert any("Planning is waiting for provider/model" in line for line in output)
    assert any("still waiting for the model" in line for line in output)
    assert any("response received in 0.0s (completed)" in line for line in output)
    assert tuple(output) == rendered_at_completion


def test_safe_response_variants_do_not_consume_a_model_repair_call(
    tmp_path: Path,
) -> None:
    payload = proposal_response().model_dump(mode="json")
    payload.pop("kind")
    payload["proposal"]["tasks"][0]["expected_paths"] = ["tests/"]
    payload["proposal"]["agents"][0]["workspace_scope"] = "repository/"
    raw_response = json.dumps(payload)
    executor = ScriptedAgentExecutor([raw_response])
    store = PlanningStore(tmp_path / "planning")
    coordinator = AdaptivePlanningCoordinator(
        executor=executor,
        store=store,
        policy=policy(response_repair_limit=0),
        clock=AdvancingClock(),
    )

    created = coordinator.start(
        request(),
        answer_question=lambda _question: pytest.fail("unexpected question"),
    )

    assert created is not None
    assert len(executor.requests) == 1
    turn = store.load_turn(request().run_id, 1)
    assert turn.response_text == raw_response
    assert turn.parsed_response is not None
    assert turn.parsed_response.proposal is not None
    assert turn.parsed_response.proposal.tasks[0].expected_paths == ("tests",)
    assert turn.parsed_response.proposal.agents[0].workspace_scope == "repository"
    assert turn.response_normalizations == (
        "inferred response kind as proposal",
        "canonicalized proposal.tasks[0].expected_paths[0]",
        "canonicalized proposal.agents[0].workspace_scope",
    )


def test_cancellation_stops_before_a_proposal_or_approval(tmp_path: Path) -> None:
    store = PlanningStore(tmp_path / "planning")
    coordinator = AdaptivePlanningCoordinator(
        executor=ScriptedAgentExecutor([response(question_response())]),
        store=store,
        policy=policy(),
        clock=AdvancingClock(),
    )

    created = coordinator.start(request(), answer_question=lambda _question: None)

    assert created is None
    session = store.load_session(request().run_id)
    assert session.status is PlanningSessionStatus.CANCELLED
    assert session.latest_proposal_revision is None


def test_ordinary_user_can_answer_revise_edit_and_approve_without_json(
    tmp_path: Path,
) -> None:
    executor = ScriptedAgentExecutor(
        [
            response(question_response()),
            response(proposal_response()),
            response(
                proposal_response(proposal_body(title="Local Markdown Link Checker"))
            ),
        ]
    )
    coordinator = AdaptivePlanningCoordinator(
        executor=executor,
        store=PlanningStore(tmp_path / "planning"),
        policy=policy(),
        clock=AdvancingClock(),
    )
    answers = iter(
        (
            "1",
            "r",
            "Make local-only scope explicit in the title.",
            "e",
            "1",
            "1",
            "a",
        )
    )
    prompts: list[str] = []
    output: list[str] = []

    def read(prompt: str) -> str:
        prompts.append(prompt)
        return next(answers)

    approved = run_interactive_planning(
        coordinator,
        request(),
        read=read,
        write=output.append,
    )

    assert approved is not None
    assert approved.task_brief.title == "Local Markdown Link Checker"
    assert approved.team_plan.max_concurrency == 1
    rendered = "\n".join(output)
    assert "Planning question" in rendered
    assert "Planning is waiting for provider/model" in rendered
    assert "Planning response validated" in rendered
    assert "Custom answer" in rendered
    assert "Planning overview" in rendered
    assert "Runtime Agents" in rendered
    assert "Request changes in your own words" in rendered
    assert "Edit safe limits" in rendered
    assert "controller may now create only the Agents shown above" in rendered
    assert not any("JSON" in line for line in output)
    assert prompts[-1] == "Review choice: "
