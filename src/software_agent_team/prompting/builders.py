"""Prompt and execution request builders for fixed and dynamic workflows.

This module provides prompt template rendering and execution request construction:
    - render_agent_prompt: Renders a fixed-role prompt template with context and schema.
    - build_agent_execution_request: Constructs an AgentExecutionRequest for a role.
    - build_semantic_correction_request: Requests replacements in a fresh session.
    - build_upstream_continuation_request: Resumes partial tool-bearing turns.
    - build_readonly_upstream_continuation_request: Resumes read-only turns.
    - render_dynamic_agent_prompt: Renders prompt with specialization contract.
    - build_dynamic_agent_execution_request: Constructs request for an AgentSpec.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from string import Template

from software_agent_team.execution import AgentExecutionRequest
from software_agent_team.model_runtime import (
    openclaw_invocation_thinking_level,
    runtime_profile_for_model,
)
from software_agent_team.prompting.context import (
    _dynamic_prompt_context,
    _model_json_with_safe_task_ids,
    _prompt_context,
)
from software_agent_team.prompting.schemas import (
    _dynamic_response_schema,
    fixed_response_schema,
)
from software_agent_team.prompting.types import (
    DYNAMIC_CAPABILITY_TEMPLATES,
    ROLE_TEMPLATES,
    TEMPLATE_ROOT,
    AgentPromptError,
    AgentPromptInputs,
    DynamicAgentPromptInputs,
)
from software_agent_team.response_corrections import (
    SemanticCorrectionPlan,
    correction_prompt,
    semantic_correction_schema,
)
from software_agent_team.responses import RESPONSE_BODY_MODELS
from software_agent_team.submissions import (
    ARTIFACT_SUBMISSION_TOOL,
    AgentSubmissionContract,
    AgentSubmissionPurpose,
)
from software_agent_team.teams import AgentCapability, specialization_contract


def render_agent_prompt(
    inputs: AgentPromptInputs,
    *,
    template_root: Path = TEMPLATE_ROOT,
) -> str:
    """Render one role prompt with only its validated contextual inputs."""

    template_name = ROLE_TEMPLATES.get(inputs.role)
    if template_name is None:
        raise AgentPromptError(f"no prompt template exists for {inputs.role.value}")
    template_path = template_root / template_name
    try:
        source = template_path.read_text(encoding="utf-8")
    except OSError as error:
        raise AgentPromptError(
            f"cannot load prompt template: {template_name}"
        ) from error
    response_schema = fixed_response_schema(inputs)
    values = {
        "role": inputs.role.value,
        "expected_kind": inputs.expected_kind.value,
        "context_json": json.dumps(
            _prompt_context(inputs),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        "response_schema_json": json.dumps(
            response_schema,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
    }
    try:
        rendered = Template(source).substitute(values)
    except (KeyError, ValueError) as error:
        raise AgentPromptError(f"invalid prompt template: {template_name}") from error
    if not rendered.strip():
        raise AgentPromptError(f"prompt template rendered empty: {template_name}")
    return rendered


def build_agent_execution_request(
    inputs: AgentPromptInputs,
    *,
    timeout_seconds: int,
    model: str | None = None,
    template_root: Path = TEMPLATE_ROOT,
) -> AgentExecutionRequest:
    """Bind validated prompt inputs to the matching execution identity."""

    return AgentExecutionRequest(
        run_id=inputs.task_brief.run_id,
        team_id=inputs.team_id,
        iteration=inputs.iteration,
        role=inputs.role,
        expected_kind=inputs.expected_kind,
        prompt=render_agent_prompt(inputs, template_root=template_root),
        timeout_seconds=timeout_seconds,
        model=model,
        thinking_level=(
            None
            if model is None
            else openclaw_invocation_thinking_level(runtime_profile_for_model(model))
        ),
    )


def build_semantic_correction_request(
    request: AgentExecutionRequest,
    plan: SemanticCorrectionPlan,
    *,
    session_generation: int,
    response_schema: dict[str, object] | None = None,
) -> AgentExecutionRequest:
    """Request replacements in a fresh session bound to the prior typed result."""

    if session_generation <= request.session_generation:
        raise AgentPromptError(
            "semantic correction requires a newer OpenClaw session generation"
        )

    contract = request.submission_contract
    if contract is None:
        if response_schema is None:
            response_model = RESPONSE_BODY_MODELS.get(request.expected_kind)
            response_schema = (
                None if response_model is None else response_model.model_json_schema()
            )
        return request.model_copy(
            update={
                "prompt": (
                    f"{request.prompt}"
                    f"{correction_prompt(plan, response_schema=response_schema)}"
                ),
                "session_generation": session_generation,
            }
        )
    response_schema = contract.parameters_schema()
    correction_contract = AgentSubmissionContract.from_schema(
        semantic_correction_schema(plan, response_schema=response_schema),
        purpose=AgentSubmissionPurpose.SEMANTIC_CORRECTION,
    )
    correction = correction_prompt(
        plan,
        submission_tool=contract.tool_name,
        response_schema=response_schema,
    )
    return request.model_copy(
        update={
            "prompt": f"{request.prompt}{correction}",
            "submission_contract": correction_contract,
            "session_generation": session_generation,
        }
    )


def build_upstream_continuation_request(
    request: AgentExecutionRequest,
    *,
    workspace_state_sha256: str,
    changed_path_count: int,
) -> AgentExecutionRequest:
    """Continue verified partial work after an incomplete tool-bearing turn."""

    if request.submission_contract is None:
        raise AgentPromptError("controlled continuation requires typed submission")
    if len(workspace_state_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in workspace_state_sha256
    ):
        raise AgentPromptError("controlled continuation requires a valid state digest")
    if changed_path_count < 1:
        raise AgentPromptError("controlled continuation requires workspace progress")
    continuation = f"""

CONTROLLED_UPSTREAM_CONTINUATION_V1
The preceding tool-bearing invocation in this same Agent session ended before the
required terminal `{request.submission_contract.tool_name}` call. The Controller
did not accept its assistant text or commit partial work on your behalf. It verified
the workspace identity, ancestry, permission scope, and {changed_path_count} changed
path(s); the exact non-semantic state fingerprint is
`{workspace_state_sha256}`.

Continue the same approved task from the existing workspace and session. Inspect
the current Git diff and status before acting; preserve correct work and do not
blindly restart, revert, or repeat completed side effects. Finish the implementation,
run the required checks, create the required commit, and then call
`{request.submission_contract.tool_name}` exactly once with the requested artifact.
The original task, permission, model route, deadline, and aggregate USD budget are
unchanged. If completion is unsafe or blocked, submit that truthful terminal result
instead of fabricating success.
"""
    return request.model_copy(update={"prompt": f"{request.prompt}{continuation}"})


def build_readonly_upstream_continuation_request(
    request: AgentExecutionRequest,
    *,
    verified_commit: str,
    completed_tool_operations: int,
) -> AgentExecutionRequest:
    """Finish an attributable read-only turn without accepting its prose."""

    if request.submission_contract is None:
        raise AgentPromptError("controlled continuation requires typed submission")
    if not re.fullmatch(r"[0-9a-f]{40}", verified_commit):
        raise AgentPromptError("read-only continuation requires a verified commit")
    if completed_tool_operations < 1:
        raise AgentPromptError("read-only continuation requires attributable tools")
    review_command_note = (
        "For a documented start command, use the Controller's clean-copy command "
        "evidence when this Review sandbox alone prevents execution. A Review-only "
        "noexec mount is not a product failure when that exact clean-copy "
        "gate passed.\n"
        if request.capability is AgentCapability.REVIEW
        else ""
    )
    continuation = f"""

CONTROLLED_READONLY_UPSTREAM_CONTINUATION_V1
Your preceding tool-bearing invocation ended before the required terminal
`{request.submission_contract.tool_name}` call. The Controller did not accept
your assistant prose as a result. It verified that the read-only project is
still clean at commit `{verified_commit}` and captured {completed_tool_operations}
attributable tool operation(s) from that invocation.

Continue the same approved task in the same Agent session. Use trustworthy
observations already gathered rather than repeating probes without reason.
{review_command_note}Submit one grounded typed artifact with
`{request.submission_contract.tool_name}` now. If evidence is insufficient,
submit the truthful blocked or failing assessment through that tool. The
approved model route, permission scope, deadline, and aggregate USD budget
are unchanged.
"""
    return request.model_copy(update={"prompt": f"{request.prompt}{continuation}"})


def render_dynamic_agent_prompt(
    inputs: DynamicAgentPromptInputs,
    *,
    template_root: Path = TEMPLATE_ROOT,
) -> str:
    """Render a capability prompt from one approved run-scoped AgentSpec."""

    agent = inputs.agent
    specialization = specialization_contract(agent.specialization)
    template_name = DYNAMIC_CAPABILITY_TEMPLATES.get(agent.capability)
    if template_name is None:
        raise AgentPromptError(f"no dynamic prompt exists for {agent.capability.value}")
    try:
        source = (template_root / template_name).read_text(encoding="utf-8")
    except OSError as error:
        raise AgentPromptError(
            f"cannot load dynamic prompt template: {template_name}"
        ) from error
    if specialization.prompt_module is None:
        raise AgentPromptError(
            f"specialization {agent.specialization.value} has no runtime prompt module"
        )
    try:
        specialization_source = (
            template_root / specialization.prompt_module
        ).read_text(encoding="utf-8")
    except OSError as error:
        raise AgentPromptError(
            f"cannot load specialization prompt module: {specialization.prompt_module}"
        ) from error
    response_schema = _dynamic_response_schema(inputs)
    task_ids = tuple(task.id for task in inputs.implementation_plan.tasks)
    values = {
        "agent_id": agent.id,
        "agent_label": agent.label,
        "capability": agent.capability.value,
        "workspace_scope": agent.workspace_scope,
        "specialization": agent.specialization.value,
        "specialization_contract": specialization_source.strip(),
        "expected_kind": agent.expected_output.value,
        "submission_tool": ARTIFACT_SUBMISSION_TOOL,
        "context_json": _model_json_with_safe_task_ids(
            _dynamic_prompt_context(inputs),
            task_ids=task_ids,
        ),
        "response_schema_json": _model_json_with_safe_task_ids(
            response_schema,
            task_ids=task_ids,
        ),
    }
    try:
        rendered = Template(source).substitute(values)
    except (KeyError, ValueError) as error:
        raise AgentPromptError(
            f"invalid dynamic prompt template: {template_name}"
        ) from error
    rendered = (
        f"{rendered.rstrip()}\n\n"
        "SPECIALIZATION_CONTRACT_V1\n"
        f"specialization: {agent.specialization.value}\n"
        f"acceptance_authority: {specialization.acceptance_authority.value}\n"
        f"handoff_boundary: {specialization.handoff_boundary}\n"
        f"{specialization_source.strip()}\n"
    )
    if not rendered.strip():
        raise AgentPromptError(
            f"dynamic prompt template rendered empty: {template_name}"
        )
    return rendered


def build_dynamic_agent_execution_request(
    inputs: DynamicAgentPromptInputs,
    *,
    template_root: Path = TEMPLATE_ROOT,
) -> AgentExecutionRequest:
    """Bind one approved AgentSpec, route, timeout, and prompt to execution."""

    agent = inputs.agent
    route = inputs.team_plan.model_routes.get_route(inputs.model_route_id)
    return AgentExecutionRequest(
        run_id=inputs.task_brief.run_id,
        team_id=inputs.team_plan.team_id,
        iteration=inputs.iteration,
        agent_id=agent.id,
        capability=agent.capability,
        specialization=agent.specialization,
        expected_kind=agent.expected_output,
        prompt=render_dynamic_agent_prompt(inputs, template_root=template_root),
        timeout_seconds=agent.timeout_seconds,
        model=route.model,
        thinking_level=openclaw_invocation_thinking_level(route.runtime_profile),
        submission_contract=AgentSubmissionContract.from_schema(
            _dynamic_response_schema(inputs),
            purpose=AgentSubmissionPurpose.ARTIFACT,
        ),
    )


__all__ = [
    "build_agent_execution_request",
    "build_dynamic_agent_execution_request",
    "build_readonly_upstream_continuation_request",
    "build_semantic_correction_request",
    "build_upstream_continuation_request",
    "render_agent_prompt",
    "render_dynamic_agent_prompt",
]
