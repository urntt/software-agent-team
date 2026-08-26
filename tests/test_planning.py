"""Tests for adaptive Planning dialogue, validation, approval, and evidence."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from software_agent_team.budgets import AgentBudget
from software_agent_team.execution import ScriptedAgentExecutor
from software_agent_team.integrity import canonical_model_sha256
from software_agent_team.planning import (
    AdaptivePlanningCoordinator,
    ApprovedPlanningResult,
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
    apply_structured_edit,
    preview_adaptive_proposal,
    render_planning_overview,
    run_interactive_planning,
)
from software_agent_team.teams import (
    AgentCapability,
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
        "capability_timeout_ceiling_seconds": {
            AgentCapability.IMPLEMENTATION: 900,
            AgentCapability.INTEGRATION: 900,
            AgentCapability.TESTING: 300,
            AgentCapability.REVIEW: 300,
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
                timeout_seconds=600,
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
                timeout_seconds=240,
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
                timeout_seconds=240,
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
        timeout_seconds=300,
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
        timeout_seconds=300,
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
        timeout_seconds=300,
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

    excessive = tuple(
        agent.model_copy(update={"timeout_seconds": 301})
        if agent.id == "acceptance_tester"
        else agent
        for agent in proposal_body().agents
    )
    invalid = proposal(body=proposal_body().model_copy(update={"agents": excessive}))
    with pytest.raises(PlanningError, match="testing policy ceiling of 300s"):
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

    assert concurrency.revision == 2
    assert concurrency.source is PlanningProposalSource.STRUCTURED_EDIT
    assert concurrency.body.max_concurrency == 1
    assert iteration.body.iteration_limit == 1
    assert not iteration.body.revision_enabled


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
    session = store.load_session(request().run_id)
    assert session.status is PlanningSessionStatus.APPROVED
    assert session.turn_count == 3
    assert session.latest_proposal_revision == 3
    assert session.approved_revision == 3
    assert len(executor.requests) == 3
    assert all(call.role.value == "clarifier" for call in executor.requests)
    assert all(call.timeout_seconds == 180 for call in executor.requests)

    tampered = approved.model_dump(mode="json")
    tampered["team_plan"]["agents"][0]["timeout_seconds"] += 1
    with pytest.raises(ValidationError, match="does not bind the supplied TeamPlan"):
        ApprovedPlanningResult.model_validate(tampered)


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

    created = coordinator.start(
        request(),
        answer_question=lambda _question: pytest.fail("unexpected question"),
    )

    assert created is not None
    assert store.load_session(request().run_id).turn_count == 2
    rejected = store.load_turn(request().run_id, 1)
    assert rejected.parsed_response is None
    assert rejected.validation_error is not None
    assert "must remain independent" in rejected.validation_error
    assert "previous_response_rejected" in executor.requests[1].prompt


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
    assert "Custom answer" in rendered
    assert "Planning overview" in rendered
    assert "Runtime Agents" in rendered
    assert "Request changes in your own words" in rendered
    assert "Edit safe limits" in rendered
    assert "controller may now create only the Agents shown above" in rendered
    assert not any("JSON" in line for line in output)
    assert prompts[-1] == "Review choice: "
