"""Schema generation, validation, and manual review boundary binding.

This module provides response schema generation and constraint enforcement:
    - fixed_response_schema: Binds fixed Review assessments to manual criteria scope.
    - _require_schema_property: Validates and extracts a mutable schema property.
    - _bind_review_scope_schema: Restricts model criterion references to assigned scope.
    - _dynamic_response_schema: Builds the shared response schema for dynamic prompts.
"""

from __future__ import annotations

from software_agent_team.artifacts import AgentRole
from software_agent_team.prompting.types import (
    AgentPromptError,
    AgentPromptInputs,
    DynamicAgentPromptInputs,
)
from software_agent_team.responses import RESPONSE_BODY_MODELS
from software_agent_team.teams import AgentCapability


def _require_schema_property(
    definition: object,
    *,
    definition_name: str,
    property_name: str,
) -> dict[str, object]:
    """Return one mutable definition property or reject schema drift."""

    if not isinstance(definition, dict):
        raise AgentPromptError(
            f"Review response schema lacks {definition_name} definition"
        )
    properties = definition.get("properties")
    value = properties.get(property_name) if isinstance(properties, dict) else None
    if not isinstance(value, dict):
        raise AgentPromptError(
            f"Review response schema lacks {definition_name}.{property_name}"
        )
    return value


def _bind_review_scope_schema(
    response_schema: dict[str, object],
    *,
    reviewed_criterion_ids: tuple[str, ...],
) -> None:
    """Constrain every model-owned criterion reference to its assigned scope."""

    definitions = response_schema.get("$defs")
    if not isinstance(definitions, dict):
        raise AgentPromptError("dynamic Review response schema lacks definitions")
    allowed = list(reviewed_criterion_ids)

    assessment = definitions.get("ReviewCriterionAssessmentResponse")
    assessment_criterion = _require_schema_property(
        assessment,
        definition_name="assessment",
        property_name="criterion_id",
    )
    assessment_criterion["enum"] = allowed

    finding = definitions.get("ReviewFinding")
    finding_criteria = _require_schema_property(
        finding,
        definition_name="finding",
        property_name="criterion_ids",
    )
    finding_items = finding_criteria.get("items")
    if not isinstance(finding_items, dict):
        raise AgentPromptError(
            "dynamic Review response schema lacks finding criterion items"
        )
    finding_items["enum"] = allowed
    finding_criteria["minItems"] = 1
    finding_criteria["uniqueItems"] = True

    for definition_name in (
        "SecuritySurfaceAssessment",
        "ExperienceWorkflowAssessment",
    ):
        specialized = definitions.get(definition_name)
        if specialized is None:
            continue
        specialized_criteria = _require_schema_property(
            specialized,
            definition_name=definition_name,
            property_name="criterion_ids",
        )
        specialized_items = specialized_criteria.get("items")
        if not isinstance(specialized_items, dict):
            raise AgentPromptError(
                "dynamic Review response schema lacks specialized criterion items"
            )
        specialized_items["enum"] = allowed
        specialized_criteria["uniqueItems"] = True


def fixed_response_schema(inputs: AgentPromptInputs) -> dict[str, object]:
    """Bind fixed Review assessments to the controller's manual scope."""

    model = RESPONSE_BODY_MODELS.get(inputs.expected_kind)
    if model is None:
        raise AgentPromptError(
            f"no response model exists for {inputs.expected_kind.value}"
        )
    schema = model.model_json_schema()
    if inputs.role is not AgentRole.REVIEWER:
        return schema
    properties = schema.get("properties")
    assessments = (
        properties.get("criterion_assessments")
        if isinstance(properties, dict)
        else None
    )
    definitions = schema.get("$defs")
    assessment = (
        definitions.get("ReviewCriterionAssessmentResponse")
        if isinstance(definitions, dict)
        else None
    )
    if not isinstance(assessments, dict):
        raise AgentPromptError("fixed Review schema lacks criterion assessments")
    allowed = list(inputs.manual_review_criteria)
    assessments["minItems"] = len(allowed)
    assessments["maxItems"] = len(allowed)
    if allowed:
        assessments.pop("default", None)
        required = schema.setdefault("required", [])
        if not isinstance(required, list):
            raise AgentPromptError("fixed Review schema has invalid required fields")
        if "criterion_assessments" not in required:
            required.append("criterion_assessments")
    criterion_id = _require_schema_property(
        assessment,
        definition_name="fixed review assessment",
        property_name="criterion_id",
    )
    criterion_id["enum"] = allowed
    return schema


def _dynamic_response_schema(inputs: DynamicAgentPromptInputs) -> dict[str, object]:
    """Build the one schema shared by the prompt and submission tool."""

    agent = inputs.agent
    response_model = RESPONSE_BODY_MODELS.get(agent.expected_output)
    if response_model is None:
        raise AgentPromptError(
            f"no response model exists for {agent.expected_output.value}"
        )
    response_schema = response_model.model_json_schema()
    if agent.capability is AgentCapability.REVIEW:
        properties = response_schema.get("properties")
        if not isinstance(properties, dict) or not isinstance(
            properties.get("criterion_assessments"), dict
        ):
            raise AgentPromptError(
                "dynamic Review response schema lacks criterion assessments"
            )
        properties["criterion_assessments"].pop("default", None)
        required = response_schema.setdefault("required", [])
        if not isinstance(required, list):
            raise AgentPromptError("dynamic Review response requirements are invalid")
        if "criterion_assessments" not in required:
            required.append("criterion_assessments")
        definitions = response_schema.get("$defs")
        assessment = (
            definitions.get("ReviewCriterionAssessmentResponse")
            if isinstance(definitions, dict)
            else None
        )
        if not isinstance(assessment, dict):
            raise AgentPromptError(
                "dynamic Review response schema lacks assessment definition"
            )
        assessment_properties = assessment.get("properties")
        if not isinstance(assessment_properties, dict) or not isinstance(
            assessment_properties.get("boundary_checks"), dict
        ):
            raise AgentPromptError(
                "dynamic Review response schema lacks boundary checks"
            )
        assessment_properties["boundary_checks"].pop("default", None)
        assessment_required = assessment.setdefault("required", [])
        if not isinstance(assessment_required, list):
            raise AgentPromptError("dynamic Review assessment requirements are invalid")
        if "boundary_checks" not in assessment_required:
            assessment_required.append("boundary_checks")
        _bind_review_scope_schema(
            response_schema,
            reviewed_criterion_ids=inputs.manual_review_criteria,
        )
    return response_schema


__all__ = [
    "_bind_review_scope_schema",
    "_dynamic_response_schema",
    "_require_schema_property",
    "fixed_response_schema",
]
