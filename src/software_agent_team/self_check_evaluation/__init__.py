"""Concrete evaluators for SAT's two task-readiness checkpoints.

自检评估子包: 提供 SAT 两个关键自检阶段的完整评估能力:
1. 任务准入检查点 (Task Admission Checkpoint)
2. 计划执行检查点 (Plan Execution Checkpoint)
"""

from __future__ import annotations

from .admission import (
    _STARTUP_CHECK_IDS,
    _STARTUP_DEPENDENCIES,
    _evaluate_authorization_check,
    _evaluate_destination_check,
    _evaluate_model_profiles,
    _evaluate_planning_route_check,
    _evaluate_request_check,
    _evaluate_schema_check,
    _evaluate_startup_diagnostics,
    _evaluate_update_check,
    _evaluate_version_check,
    build_task_admission_report,
)
from .common import (
    PROMPT_TEMPLATE_ROOT,
    EvidenceKind,
    _result,
    _utc_now,
)
from .plan_execution import (
    _evaluate_agents,
    _evaluate_delivery_boundary,
    _evaluate_model_routes,
    _evaluate_plan_approval,
    _evaluate_runtime_plan,
    _evaluate_specialization_catalog,
    _evaluate_workspace_source,
    _get_prompt_template_root,
    build_plan_execution_report,
)

__all__ = [
    "PROMPT_TEMPLATE_ROOT",
    "_STARTUP_CHECK_IDS",
    "_STARTUP_DEPENDENCIES",
    "EvidenceKind",
    "_evaluate_agents",
    "_evaluate_authorization_check",
    "_evaluate_delivery_boundary",
    "_evaluate_destination_check",
    "_evaluate_model_profiles",
    "_evaluate_model_routes",
    "_evaluate_plan_approval",
    "_evaluate_planning_route_check",
    "_evaluate_request_check",
    "_evaluate_runtime_plan",
    "_evaluate_schema_check",
    "_evaluate_specialization_catalog",
    "_evaluate_startup_diagnostics",
    "_evaluate_update_check",
    "_evaluate_version_check",
    "_evaluate_workspace_source",
    "_get_prompt_template_root",
    "_result",
    "_utc_now",
    "build_plan_execution_report",
    "build_task_admission_report",
]
