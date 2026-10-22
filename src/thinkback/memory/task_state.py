"""任务状态机 —— 「能不能重试」「是不是终态」的唯一口径。

这组判定原先散在 service.py 的五处（append / delete / update / rebuild / 终态写）。
散开写就会漏：DEAD_LETTER 曾被当成「不是 FAILED，所以不是重试」→ 状态被覆写成
RUNNING → retry_count 不自增、预算检查不执行 → 实测连打 19 次 retry_count 涨到 11
（MAX_RETRIES=5）。DEAD_LETTER 的语义是「预算已耗尽，需要人工介入」，
复活它等于无视预算。

纯函数、无 IO —— 不依赖 MemoryService 就能测。
"""

from __future__ import annotations

from thinkback.domain.enums import TaskStatus

# 只有 FAILED 可重试。DEAD_LETTER 是终态，不是「另一种失败」。
RETRYABLE_STATUSES: frozenset[TaskStatus] = frozenset({TaskStatus.FAILED})

# 已定局：跨副本 CAS 重放时**不得**改写成别的状态。
# FAILED 也在里面 —— 另一副本已经判定失败并写进 last_error 了，重放不能把它改成
# COMPLETED；DEAD_LETTER 更不能被改写（那会抹掉「预算耗尽、需人工介入」的信号）。
FINALIZED_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.DEAD_LETTER}
)


def is_retryable(status: TaskStatus) -> bool:
    """该状态是否允许重试。DEAD_LETTER 不允许 —— 它的预算已经用完了。"""
    return status in RETRYABLE_STATUSES


def is_finalized(status: TaskStatus) -> bool:
    """该状态是否已定局（跨副本 CAS 重放时不得改写）。"""
    return status in FINALIZED_STATUSES


def is_dead_letter(status: TaskStatus) -> bool:
    """该状态是否为 dead_letter（需要人工介入）。"""
    return status is TaskStatus.DEAD_LETTER
