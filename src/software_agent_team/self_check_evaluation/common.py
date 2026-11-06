"""Shared utilities and result constructors for self-check evaluations.

自检评估模块共享工具集: 提供 UTC 时间获取、标准检查结果结构构造以及默认路径配置。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from software_agent_team.self_check import (
    SelfCheckCategory,
    SelfCheckCheckpoint,
    SelfCheckEvidence,
    SelfCheckOwner,
    SelfCheckResult,
    SelfCheckSeverity,
    SelfCheckStatus,
    observation_sha256,
)

# 默认 prompt 模版根目录 (位于 software_agent_team/prompt_templates)
PROMPT_TEMPLATE_ROOT = Path(__file__).resolve().parent.parent / "prompt_templates"

EvidenceKind = Literal[
    "controller_contract",
    "local_observation",
    "persisted_record",
    "remote_observation",
    "user_authorization",
]


def _utc_now() -> datetime:
    """Return the current datetime in UTC timezone.

    获取当前 UTC 时间。
    """
    return datetime.now(UTC)


def _result(
    *,
    check_id: str,
    checkpoint: SelfCheckCheckpoint,
    category: SelfCheckCategory,
    owner: SelfCheckOwner,
    observed_fact: str,
    evidence_kind: EvidenceKind,
    evidence_reference: str,
    input_value: object,
    checked_at: datetime,
    dependencies: tuple[str, ...] = (),
    status: SelfCheckStatus = SelfCheckStatus.PASS,
    severity: SelfCheckSeverity = SelfCheckSeverity.INFO,
    consequence: str | None = None,
    remediation: str | None = None,
    rerun_rule: str,
) -> SelfCheckResult:
    """Construct an immutable SelfCheckResult with computed input SHA256.

    构造不可变的 SelfCheckResult, 自动计算输入值的 SHA256 校验和并封装证据链。
    """
    return SelfCheckResult(
        id=check_id,
        checkpoint=checkpoint,
        category=category,
        owner=owner,
        dependencies=dependencies,
        input_sha256=observation_sha256(input_value),
        checked_at=checked_at,
        severity=severity,
        status=status,
        observed_fact=observed_fact,
        evidence=(
            SelfCheckEvidence(
                kind=evidence_kind,
                reference=evidence_reference,
            ),
        ),
        consequence=consequence,
        remediation=remediation,
        rerun_rule=rerun_rule,
    )


__all__ = [
    "PROMPT_TEMPLATE_ROOT",
    "EvidenceKind",
    "_result",
    "_utc_now",
]
