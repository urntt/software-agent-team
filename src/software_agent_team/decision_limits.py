# ruff: noqa: E501
"""Typed ownership metadata for decision-relevant product constants.

The registry stores references to authoritative values rather than copying the
values.  It therefore explains a limit without becoming a second policy file.
"""

from __future__ import annotations

import importlib
import json
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator


class DecisionLimitError(ValueError):
    """Raised when decision-limit ownership metadata is incomplete or stale."""


class DecisionLimitCategory(StrEnum):
    """Mutually exclusive authority classes for limit-like product values."""

    USER_CHOICE = "user_choice"
    DISCOVERED_CAPABILITY = "discovered_capability"
    INFRASTRUCTURE_GUARD = "infrastructure_guard"
    PROTOCOL_SCHEMA_BOUND = "protocol_schema_bound"
    CONTROLLED_EXPERIMENT_VARIABLE = "controlled_experiment_variable"
    PRESENTATION_PARAMETER = "presentation_parameter"


class DecisionValueReferenceKind(StrEnum):
    """How validation locates a value without duplicating it in this registry."""

    JSON_POINTER = "json_pointer"
    MODEL_FIELD = "model_field"
    PYTHON_ATTRIBUTE = "python_attribute"
    RUNTIME_DERIVED = "runtime_derived"


class DecisionLimitVisibility(StrEnum):
    """When an ordinary user needs to see a limit or its effect."""

    ALWAYS = "always"
    ON_TRIGGER = "on_trigger"
    DETAILED = "detailed"
    EVALUATION_ONLY = "evaluation_only"


class DecisionLimitConfigurability(StrEnum):
    """Who, if anyone, can alter the effective value."""

    USER_PER_TASK = "user_per_task"
    USER_CONFIGURATION = "user_configuration"
    AUTO_DISCOVERED = "auto_discovered"
    MAINTAINER_POLICY = "maintainer_policy"
    CONTROLLED_EVALUATION = "controlled_evaluation"
    NOT_CONFIGURABLE = "not_configurable"


class DecisionValueReference(BaseModel):
    """Stable pointer to one authoritative value or runtime fact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: DecisionValueReferenceKind
    reference: str = Field(min_length=1, max_length=4096)

    @field_validator("reference")
    @classmethod
    def require_clean_reference(cls, value: str) -> str:
        if value != value.strip() or any(ord(character) < 32 for character in value):
            raise ValueError("decision value references must be clean text")
        return value


class DecisionLimitDefinition(BaseModel):
    """Ownership, meaning, trigger, and revision contract for one limit."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_.-]*$")
    category: DecisionLimitCategory
    value: DecisionValueReference
    owner: str = Field(min_length=1, max_length=500)
    rationale: str = Field(min_length=1, max_length=2000)
    visibility: DecisionLimitVisibility
    configurability: DecisionLimitConfigurability
    trigger_consequence: str = Field(min_length=1, max_length=2000)
    recovery: str = Field(min_length=1, max_length=2000)
    revision_evidence: tuple[str, ...] = Field(min_length=1, max_length=16)


def _definition(
    identifier: str,
    category: DecisionLimitCategory,
    reference_kind: DecisionValueReferenceKind,
    reference: str,
    *,
    owner: str,
    rationale: str,
    visibility: DecisionLimitVisibility,
    configurability: DecisionLimitConfigurability,
    consequence: str,
    recovery: str,
    evidence: str,
) -> DecisionLimitDefinition:
    return DecisionLimitDefinition(
        id=identifier,
        category=category,
        value=DecisionValueReference(kind=reference_kind, reference=reference),
        owner=owner,
        rationale=rationale,
        visibility=visibility,
        configurability=configurability,
        trigger_consequence=consequence,
        recovery=recovery,
        revision_evidence=(evidence,),
    )


_INFRASTRUCTURE = DecisionLimitCategory.INFRASTRUCTURE_GUARD
_PROTOCOL = DecisionLimitCategory.PROTOCOL_SCHEMA_BOUND

DECISION_LIMIT_REGISTRY: tuple[DecisionLimitDefinition, ...] = (
    _definition(
        "user.task-cost-usd",
        DecisionLimitCategory.USER_CHOICE,
        DecisionValueReferenceKind.MODEL_FIELD,
        "software_agent_team.self_check:TaskResourceAuthorization.maximum_estimated_cost_usd",
        owner="the user, recorded by TaskResourceAuthorization",
        rationale="This is the only aggregate model-usage budget for an ordinary task.",
        visibility=DecisionLimitVisibility.ALWAYS,
        configurability=DecisionLimitConfigurability.USER_PER_TASK,
        consequence="No new paid call starts after recorded estimated spend exhausts the authorization; an absolute provider bill cap remains provider-owned.",
        recovery="Increase the task authorization, approve a less costly route or plan, or configure a provider-side quota for an absolute cap.",
        evidence="VISION.md resource-authority decision",
    ),
    _definition(
        "user.optional-run-deadline",
        DecisionLimitCategory.USER_CHOICE,
        DecisionValueReferenceKind.MODEL_FIELD,
        "software_agent_team.self_check:TaskResourceAuthorization.run_deadline_seconds",
        owner="the user, recorded by TaskResourceAuthorization",
        rationale="Only the user can define whether completion after a real deadline has no value.",
        visibility=DecisionLimitVisibility.ALWAYS,
        configurability=DecisionLimitConfigurability.USER_PER_TASK,
        consequence="No new call starts after the deadline; an active call is interrupted at it.",
        recovery="Start a new task with a later deadline or no deadline.",
        evidence="VISION.md provider-liveness decision",
    ),
    _definition(
        "user.maximum-concurrency",
        DecisionLimitCategory.USER_CHOICE,
        DecisionValueReferenceKind.MODEL_FIELD,
        "software_agent_team.user_configuration:UserConfiguration.max_concurrency",
        owner="the user, constrained by plan dependencies and host readiness",
        rationale="Concurrency trades elapsed time against local and provider capacity without changing scope.",
        visibility=DecisionLimitVisibility.ALWAYS,
        configurability=DecisionLimitConfigurability.USER_CONFIGURATION,
        consequence="The scheduler starts no more than the approved number of ready Agents together.",
        recovery="Choose another concurrency value and re-approve the plan.",
        evidence="VISION.md controller-owned scheduling decision",
    ),
    _definition(
        "model.context-window",
        DecisionLimitCategory.DISCOVERED_CAPABILITY,
        DecisionValueReferenceKind.MODEL_FIELD,
        "software_agent_team.model_routing:ModelProfile.context_window_tokens",
        owner="provider/runtime metadata, with explicit user input only when unknown",
        rationale="The context window is a model capability, not a user resource budget.",
        visibility=DecisionLimitVisibility.ALWAYS,
        configurability=DecisionLimitConfigurability.AUTO_DISCOVERED,
        consequence="A route with unknown context cannot pass task admission without user input.",
        recovery="Refresh provider metadata or enter the documented model context length.",
        evidence="VISION.md resource-authority decision",
    ),
    _definition(
        "provider.stream-inactivity",
        _INFRASTRUCTURE,
        DecisionValueReferenceKind.PYTHON_ATTRIBUTE,
        "software_agent_team.execution:resolve_provider_liveness_policy",
        owner="SAT provider-liveness resolver, bounded by inspected OpenClaw provider/model metadata",
        rationale="Provider chunks and attributable tool lifecycle renew liveness while total productive work time remains unbounded.",
        visibility=DecisionLimitVisibility.ON_TRIGGER,
        configurability=DecisionLimitConfigurability.MAINTAINER_POLICY,
        consequence="Sustained provider silence becomes a typed stalled invocation rather than a work-time cutoff.",
        recovery="Probe provider health, preserve evidence, and retry only under the approved task budget.",
        evidence="tests/test_execution.py renewable liveness coverage",
    ),
    _definition(
        "infrastructure.provider-stall-grace",
        _INFRASTRUCTURE,
        DecisionValueReferenceKind.PYTHON_ATTRIBUTE,
        "software_agent_team.execution:DEFAULT_PROVIDER_STALL_GRACE_SECONDS",
        owner="OpenClawSubprocessExecutor",
        rationale="A visible probe window separates suspected silence from interruption and is shortened for providers with smaller explicit request bounds.",
        visibility=DecisionLimitVisibility.ON_TRIGGER,
        configurability=DecisionLimitConfigurability.MAINTAINER_POLICY,
        consequence="Trusted activity recovers the invocation; continued silence terminates only the exact SAT-owned process.",
        recovery="Inspect content-free liveness evidence and provider behavior before revising the grace cap.",
        evidence="tests/test_execution.py stall recovery and cleanup coverage",
    ),
    _definition(
        "infrastructure.provider-liveness-poll",
        _INFRASTRUCTURE,
        DecisionValueReferenceKind.PYTHON_ATTRIBUTE,
        "software_agent_team.execution:DEFAULT_LIVENESS_POLL_SECONDS",
        owner="OpenClawSubprocessExecutor",
        rationale="The controller must observe raw-stream and tool lifecycle changes promptly without busy-spinning.",
        visibility=DecisionLimitVisibility.DETAILED,
        configurability=DecisionLimitConfigurability.MAINTAINER_POLICY,
        consequence="Only observation latency and termination overshoot change; the silence lease does not.",
        recovery="Tune with process and provider activity measurements, then rerun liveness timing tests.",
        evidence="tests/test_execution.py liveness timing coverage",
    ),
    _definition(
        "infrastructure.process-shutdown-grace",
        _INFRASTRUCTURE,
        DecisionValueReferenceKind.PYTHON_ATTRIBUTE,
        "software_agent_team.execution:DEFAULT_PROCESS_SHUTDOWN_GRACE_SECONDS",
        owner="OpenClawSubprocessExecutor",
        rationale="A terminated child receives bounded time to exit before forceful SAT-owned cleanup.",
        visibility=DecisionLimitVisibility.ON_TRIGGER,
        configurability=DecisionLimitConfigurability.MAINTAINER_POLICY,
        consequence="SAT escalates from termination to killing only the exact owned process group.",
        recovery="Inspect retained stderr/session evidence before changing the grace value.",
        evidence="tests/test_execution.py process cleanup coverage",
    ),
    _definition(
        "infrastructure.preflight-command-timeout",
        _INFRASTRUCTURE,
        DecisionValueReferenceKind.PYTHON_ATTRIBUTE,
        "software_agent_team.runtime_configuration:PREFLIGHT_COMMAND_TIMEOUT_SECONDS",
        owner="runtime preflight",
        rationale="Local diagnostic subprocesses must not hang task admission indefinitely.",
        visibility=DecisionLimitVisibility.ON_TRIGGER,
        configurability=DecisionLimitConfigurability.MAINTAINER_POLICY,
        consequence="The specific readiness probe becomes unavailable or blocked; model work does not start.",
        recovery="Repair the named local tool and rerun only the stale check.",
        evidence="tests/test_runtime_configuration.py preflight timeout coverage",
    ),
    _definition(
        "infrastructure.model-catalog-timeout",
        _INFRASTRUCTURE,
        DecisionValueReferenceKind.PYTHON_ATTRIBUTE,
        "software_agent_team.runtime_configuration:MODEL_INSPECTION_TIMEOUT_SECONDS",
        owner="OpenClaw model inspection",
        rationale="Cold local catalog inspection is separate from model-generation work and must terminate.",
        visibility=DecisionLimitVisibility.ON_TRIGGER,
        configurability=DecisionLimitConfigurability.MAINTAINER_POLICY,
        consequence="The route remains unverified and task admission requests remediation.",
        recovery="Repair model catalog/auth access and rerun route inspection.",
        evidence="tests/test_runtime_configuration.py cold catalog coverage",
    ),
    _definition(
        "infrastructure.minimum-free-disk",
        _INFRASTRUCTURE,
        DecisionValueReferenceKind.PYTHON_ATTRIBUTE,
        "software_agent_team.product:MINIMUM_FREE_BYTES",
        owner="product startup diagnostics",
        rationale="A new workspace and immutable evidence must not predictably fill the host filesystem.",
        visibility=DecisionLimitVisibility.ON_TRIGGER,
        configurability=DecisionLimitConfigurability.MAINTAINER_POLICY,
        consequence="Task admission blocks before creating a workspace.",
        recovery="Free disk space or select a supported state location with sufficient capacity.",
        evidence="tests/test_product.py disk readiness coverage",
    ),
    *tuple(
        _definition(
            f"infrastructure.quality-sandbox-{field.replace('_', '-')}",
            _INFRASTRUCTURE,
            DecisionValueReferenceKind.JSON_POINTER,
            f"configs/product-policy.json#/limits/{field}",
            owner="product deterministic quality sandbox policy",
            rationale=(
                "This bounds a non-model generated-code verification resource inside the isolated sandbox; memory and PID capacity are also checked against current host headroom before task work."
            ),
            visibility=DecisionLimitVisibility.ON_TRIGGER,
            configurability=DecisionLimitConfigurability.MAINTAINER_POLICY,
            consequence=(
                "Host headroom below the configured ceiling is shown as a warning because a ceiling is not a minimum; the executable sandbox probe or affected quality gate decides readiness and retains evidence."
                if field in {"memory_mb", "pids"}
                else "The affected quality gate fails with retained command evidence."
            ),
            recovery=(
                "Free or increase host/WSL/Docker capacity, then rerun the executable sandbox probe; revise the sandbox policy only with measurements."
                if field in {"memory_mb", "pids"}
                else "Inspect the gate evidence, reduce pathological output/work, or revise the policy with measurements."
            ),
            evidence=(
                "tests/test_product.py host-capacity admission coverage"
                if field in {"memory_mb", "pids"}
                else "tests/test_quality_gates.py sandbox-limit coverage"
            ),
        )
        for field in (
            "command_timeout_seconds",
            "total_timeout_seconds",
            "cpu_cores",
            "memory_mb",
            "pids",
            "open_files",
            "writable_tmpfs_mb",
            "stdout_max_bytes",
            "stderr_max_bytes",
        )
    ),
    _definition(
        "protocol.persisted-schema-file-bytes",
        _PROTOCOL,
        DecisionValueReferenceKind.PYTHON_ATTRIBUTE,
        "software_agent_team.schema_compatibility:MAX_SCHEMA_FILE_BYTES",
        owner="schema compatibility scanner",
        rationale="An untrusted persisted file cannot consume unbounded memory during compatibility admission.",
        visibility=DecisionLimitVisibility.ON_TRIGGER,
        configurability=DecisionLimitConfigurability.NOT_CONFIGURABLE,
        consequence="The oversized state file is rejected without mutation.",
        recovery="Export and inspect the file; use a supported migration rather than truncating evidence.",
        evidence="tests/test_schema_compatibility.py adversarial file coverage",
    ),
    _definition(
        "protocol.persisted-schema-file-count",
        _PROTOCOL,
        DecisionValueReferenceKind.PYTHON_ATTRIBUTE,
        "software_agent_team.schema_compatibility:MAX_SCHEMA_FILES",
        owner="schema compatibility scanner",
        rationale="A hostile or corrupted state tree cannot cause unbounded traversal.",
        visibility=DecisionLimitVisibility.ON_TRIGGER,
        configurability=DecisionLimitConfigurability.NOT_CONFIGURABLE,
        consequence="Compatibility admission stops before changing state.",
        recovery="Export and inspect the state tree, then use a supported recovery path.",
        evidence="tests/test_schema_compatibility.py traversal coverage",
    ),
    _definition(
        "protocol.self-check-report-bytes",
        _PROTOCOL,
        DecisionValueReferenceKind.PYTHON_ATTRIBUTE,
        "software_agent_team.self_check:MAX_SELF_CHECK_REPORT_BYTES",
        owner="TaskSelfCheckStore",
        rationale="A readiness report must remain bounded and safely readable as one immutable record.",
        visibility=DecisionLimitVisibility.ON_TRIGGER,
        configurability=DecisionLimitConfigurability.NOT_CONFIGURABLE,
        consequence="The report is refused before a partial record can be published.",
        recovery="Reduce duplicated evidence references; never discard the underlying evidence.",
        evidence="tests/test_self_check.py persistence coverage",
    ),
    _definition(
        "protocol.planning-evidence-characters",
        _PROTOCOL,
        DecisionValueReferenceKind.PYTHON_ATTRIBUTE,
        "software_agent_team.planning:MAX_PLANNING_EVIDENCE_CHARACTERS",
        owner="PlanningStore",
        rationale="One model response cannot create an unbounded in-memory or persisted evidence object.",
        visibility=DecisionLimitVisibility.ON_TRIGGER,
        configurability=DecisionLimitConfigurability.NOT_CONFIGURABLE,
        consequence="The response is rejected as an invalid protocol payload.",
        recovery="Use smaller targeted semantic fields or split evidence through controller-owned references.",
        evidence="tests/test_planning.py Planning evidence coverage",
    ),
    _definition(
        "protocol.planning-normalization-count",
        _PROTOCOL,
        DecisionValueReferenceKind.PYTHON_ATTRIBUTE,
        "software_agent_team.planning:MAX_RESPONSE_NORMALIZATIONS",
        owner="Planning response normalizer",
        rationale="Attributable deterministic corrections cannot grow without bound in one response.",
        visibility=DecisionLimitVisibility.DETAILED,
        configurability=DecisionLimitConfigurability.NOT_CONFIGURABLE,
        consequence="The response is rejected rather than silently dropping correction evidence.",
        recovery="Correct the response contract or reduce redundant model-authored fields.",
        evidence="tests/test_planning.py normalization coverage",
    ),
    _definition(
        "protocol.planning-normalization-characters",
        _PROTOCOL,
        DecisionValueReferenceKind.PYTHON_ATTRIBUTE,
        "software_agent_team.planning:MAX_RESPONSE_NORMALIZATION_CHARACTERS",
        owner="Planning response normalizer",
        rationale="One correction description cannot become an unbounded payload channel.",
        visibility=DecisionLimitVisibility.DETAILED,
        configurability=DecisionLimitConfigurability.NOT_CONFIGURABLE,
        consequence="The response is rejected with its validation error preserved.",
        recovery="Emit typed concise correction facts and retain detailed evidence by reference.",
        evidence="tests/test_planning.py normalization coverage",
    ),
    _definition(
        "experiment.agent-budget",
        DecisionLimitCategory.CONTROLLED_EXPERIMENT_VARIABLE,
        DecisionValueReferenceKind.JSON_POINTER,
        "configs/run-policy.json#/agent_budget",
        owner="controlled evaluation run policy",
        rationale="Frozen call, token, duration, and cost ceilings make benchmark comparisons reproducible.",
        visibility=DecisionLimitVisibility.EVALUATION_ONLY,
        configurability=DecisionLimitConfigurability.CONTROLLED_EVALUATION,
        consequence="The evaluation stops at its preregistered boundary and records the crossing.",
        recovery="Start a new explicitly versioned evaluation; do not mutate completed evidence.",
        evidence="configs/run-policy.json controlled_evaluation authority",
    ),
    _definition(
        "experiment.agent-invocation-timeouts",
        DecisionLimitCategory.CONTROLLED_EXPERIMENT_VARIABLE,
        DecisionValueReferenceKind.JSON_POINTER,
        "configs/run-policy.json#/agent_stage_timeouts_seconds",
        owner="controlled evaluation run policy",
        rationale="Frozen per-role wall-clock limits are experiment inputs, not ordinary product semantics.",
        visibility=DecisionLimitVisibility.EVALUATION_ONLY,
        configurability=DecisionLimitConfigurability.CONTROLLED_EVALUATION,
        consequence="The exact evaluation invocation is terminated and recorded as timed out.",
        recovery="Run a separately identified evaluation with another preregistered policy.",
        evidence="configs/run-policy.json controlled_evaluation authority",
    ),
    _definition(
        "experiment.iteration-limit",
        DecisionLimitCategory.CONTROLLED_EXPERIMENT_VARIABLE,
        DecisionValueReferenceKind.PYTHON_ATTRIBUTE,
        "software_agent_team.cli:EVALUATION_ITERATION_LIMIT",
        owner="fixed-workflow evaluation launcher",
        rationale="The Phase 1 comparison freezes the same revision opportunity for every team fixture.",
        visibility=DecisionLimitVisibility.EVALUATION_ONLY,
        configurability=DecisionLimitConfigurability.CONTROLLED_EVALUATION,
        consequence="The compatibility workflow terminates at the declared evaluation boundary.",
        recovery="Create a new evaluation definition rather than changing a completed run.",
        evidence="tests/test_workflow.py iteration-limit coverage",
    ),
    _definition(
        "presentation.progress-heartbeat-seconds",
        DecisionLimitCategory.PRESENTATION_PARAMETER,
        DecisionValueReferenceKind.PYTHON_ATTRIBUTE,
        "software_agent_team.progress:DEFAULT_PROGRESS_HEARTBEAT_SECONDS",
        owner="TerminalProgressRenderer",
        rationale="A periodic elapsed notice reassures the user without claiming provider activity.",
        visibility=DecisionLimitVisibility.DETAILED,
        configurability=DecisionLimitConfigurability.MAINTAINER_POLICY,
        consequence="Only terminal refresh frequency changes; execution and liveness do not.",
        recovery="Change visibility or revise the display interval with usability evidence.",
        evidence="tests/test_progress.py heartbeat isolation coverage",
    ),
    _definition(
        "presentation.progress-summary-characters",
        DecisionLimitCategory.PRESENTATION_PARAMETER,
        DecisionValueReferenceKind.PYTHON_ATTRIBUTE,
        "software_agent_team.progress:MAX_PROGRESS_SUMMARY_CHARACTERS",
        owner="RunEvent terminal summary schema",
        rationale="Terminal summaries stay scannable while full artifacts remain immutable by reference.",
        visibility=DecisionLimitVisibility.DETAILED,
        configurability=DecisionLimitConfigurability.MAINTAINER_POLICY,
        consequence="A producer must mark truncation and link the complete evidence.",
        recovery="Open the referenced artifact or increase visibility; do not infer missing text.",
        evidence="tests/test_progress.py truncation coverage",
    ),
)


def validate_decision_limit_registry(project_root: Path) -> None:
    """Resolve every static reference and reject duplicate or incomplete metadata."""

    ids = [definition.id for definition in DECISION_LIMIT_REGISTRY]
    if len(ids) != len(set(ids)):
        raise DecisionLimitError("decision-limit IDs must be unique")
    references = [definition.value.reference for definition in DECISION_LIMIT_REGISTRY]
    if len(references) != len(set(references)):
        raise DecisionLimitError("decision-limit value references must be unique")
    if set(DecisionLimitCategory) != {
        definition.category for definition in DECISION_LIMIT_REGISTRY
    }:
        raise DecisionLimitError("decision-limit registry must cover every category")
    for definition in DECISION_LIMIT_REGISTRY:
        _resolve_reference(definition.value, project_root=project_root)


def _resolve_reference(
    reference: DecisionValueReference, *, project_root: Path
) -> object:
    if reference.kind is DecisionValueReferenceKind.RUNTIME_DERIVED:
        return reference.reference
    if reference.kind is DecisionValueReferenceKind.JSON_POINTER:
        relative, marker, pointer = reference.reference.partition("#")
        if marker != "#" or not pointer.startswith("/"):
            raise DecisionLimitError(
                f"invalid decision-limit JSON pointer: {reference.reference}"
            )
        path = (project_root / relative).resolve(strict=True)
        try:
            path.relative_to(project_root.resolve(strict=True))
        except ValueError as error:
            raise DecisionLimitError(
                "decision-limit JSON pointer escapes project"
            ) from error
        value: object = json.loads(path.read_text(encoding="utf-8"))
        for encoded_part in pointer.removeprefix("/").split("/"):
            part = encoded_part.replace("~1", "/").replace("~0", "~")
            if not isinstance(value, dict) or part not in value:
                raise DecisionLimitError(
                    f"decision-limit JSON pointer does not resolve: {reference.reference}"
                )
            value = value[part]
        return value
    module_name, separator, target = reference.reference.partition(":")
    if not separator or not module_name or not target:
        raise DecisionLimitError(
            f"invalid decision-limit Python reference: {reference.reference}"
        )
    module = importlib.import_module(module_name)
    if reference.kind is DecisionValueReferenceKind.PYTHON_ATTRIBUTE:
        if not hasattr(module, target):
            raise DecisionLimitError(
                f"decision-limit Python attribute does not exist: {reference.reference}"
            )
        return getattr(module, target)
    class_name, dot, field_name = target.partition(".")
    model = getattr(module, class_name, None)
    fields = getattr(model, "model_fields", None)
    if not dot or not isinstance(fields, dict) or field_name not in fields:
        raise DecisionLimitError(
            f"decision-limit model field does not exist: {reference.reference}"
        )
    return fields[field_name]
