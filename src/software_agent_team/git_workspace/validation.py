"""Validation between Agent work results and verified Git snapshots.

智能体产物与受控 Git 提交快照一致性核验模块。
提供严格的一致性比对函数; 确保 Agent 提交的工作结果与控制器实测的 Git 状态完全相符。
"""

from __future__ import annotations

from software_agent_team.artifacts import WorkResult

from .types import GitSnapshot, WorkspaceIntegrityError


def validate_work_result_snapshot(
    work_result: WorkResult,
    snapshot: GitSnapshot,
) -> None:
    """Require an Agent work result to match controller-verified Git evidence.

    核验智能体产物与受控 Git 快照的一致性。

    要求 run_id、iteration、输入/输出 commit 以及变更文件列表完全吻合;
    若存在任何歧义或不匹配则抛出 WorkspaceIntegrityError。
    """
    if work_result.run_id != snapshot.run_id:
        raise WorkspaceIntegrityError("work result and snapshot run IDs differ")
    if work_result.iteration != snapshot.iteration:
        raise WorkspaceIntegrityError("work result and snapshot iterations differ")
    if (
        work_result.input_commit != snapshot.input_commit
        or work_result.output_commit != snapshot.output_commit
    ):
        raise WorkspaceIntegrityError("work result and snapshot commits differ")
    if set(work_result.changed_files) != set(snapshot.changed_files):
        raise WorkspaceIntegrityError("work result and snapshot changed files differ")


__all__ = [
    "validate_work_result_snapshot",
]
