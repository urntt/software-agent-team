"""Role-specific and dynamic prompt assembly with explicit context boundaries.

This package provides:
    - types: Errors, data models, template constants, and boundary invariants.
    - context: Context dictionary projection, tail truncation, and safe JSON.
    - schemas: Response schema generation, validation, and review scope binding.
    - builders: Prompt rendering and agent execution request construction.
"""

from __future__ import annotations

from software_agent_team.prompting.builders import (
    build_agent_execution_request,
    build_dynamic_agent_execution_request,
    build_readonly_upstream_continuation_request,
    build_semantic_correction_request,
    build_upstream_continuation_request,
    render_agent_prompt,
    render_dynamic_agent_prompt,
)
from software_agent_team.prompting.context import (
    _artifact_context,
    _command_evidence_context,
    _dynamic_prompt_context,
    _model_json_with_safe_task_ids,
    _prompt_context,
)
from software_agent_team.prompting.schemas import (
    _bind_review_scope_schema,
    _dynamic_response_schema,
    _require_schema_property,
    fixed_response_schema,
)
from software_agent_team.prompting.types import (
    _COMMAND_PROMPT_EXCERPT_BUDGET,
    _COMMAND_PROMPT_STREAM_LIMIT,
    DYNAMIC_CAPABILITY_TEMPLATES,
    ROLE_TEMPLATES,
    TEMPLATE_ROOT,
    AgentPromptError,
    AgentPromptInputs,
    DynamicAgentPromptInputs,
    DynamicRevisionFeedback,
    DynamicUpstreamResult,
    DynamicUserGuidance,
    PromptArtifact,
)

__all__ = [
    "DYNAMIC_CAPABILITY_TEMPLATES",
    "ROLE_TEMPLATES",
    "TEMPLATE_ROOT",
    "_COMMAND_PROMPT_EXCERPT_BUDGET",
    "_COMMAND_PROMPT_STREAM_LIMIT",
    "AgentPromptError",
    "AgentPromptInputs",
    "DynamicAgentPromptInputs",
    "DynamicRevisionFeedback",
    "DynamicUpstreamResult",
    "DynamicUserGuidance",
    "PromptArtifact",
    "_artifact_context",
    "_bind_review_scope_schema",
    "_command_evidence_context",
    "_dynamic_prompt_context",
    "_dynamic_response_schema",
    "_model_json_with_safe_task_ids",
    "_prompt_context",
    "_require_schema_property",
    "build_agent_execution_request",
    "build_dynamic_agent_execution_request",
    "build_readonly_upstream_continuation_request",
    "build_semantic_correction_request",
    "build_upstream_continuation_request",
    "fixed_response_schema",
    "render_agent_prompt",
    "render_dynamic_agent_prompt",
]
