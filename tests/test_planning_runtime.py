"""Planning invocation settings tested through the production coordinator."""

from pathlib import Path

import pytest
from planning_factories import AdvancingClock, policy, proposal_response, request

from software_agent_team.execution import ScriptedAgentExecutor, ScriptedAgentResponse
from software_agent_team.planning import AdaptivePlanningCoordinator, PlanningStore


def test_planning_compiles_thinking_from_the_runtime_profile(tmp_path: Path) -> None:
    executor = ScriptedAgentExecutor(
        [
            ScriptedAgentResponse(
                text="ignored",
                submission_payload=proposal_response().model_dump(mode="json"),
            )
        ]
    )
    coordinator = AdaptivePlanningCoordinator(
        executor=executor,
        store=PlanningStore(tmp_path / "planning"),
        policy=policy(),
        clock=AdvancingClock(),
    )
    gemini_request = request().model_copy(update={"model": "google/gemini-3.8-flash"})

    coordinator.start(
        gemini_request,
        answer_question=lambda _question: pytest.fail("unexpected question"),
    )

    assert executor.requests[0].thinking_level == "medium"
