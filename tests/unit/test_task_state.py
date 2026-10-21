"""任务状态机的口径（纯函数，不依赖 MemoryService）。

这组判定原先散在 service.py 的五处。散开写就会漏：DEAD_LETTER 曾被当成
「不是 FAILED，所以不是重试」→ 任务被覆写成 RUNNING 复活 → retry_count 不自增、
预算检查不执行 → 实测连打 19 次 retry_count 涨到 11（MAX_RETRIES=5）。
"""

from __future__ import annotations

import pytest

from thinkback.domain.enums import TaskStatus
from thinkback.memory.task_state import (
    FINALIZED_STATUSES,
    RETRYABLE_STATUSES,
    is_dead_letter,
    is_finalized,
    is_retryable,
)


def test_only_failed_is_retryable() -> None:
    assert is_retryable(TaskStatus.FAILED) is True
    assert is_retryable(TaskStatus.DEAD_LETTER) is False, "DEAD_LETTER 是终态，预算已耗尽"
    assert is_retryable(TaskStatus.COMPLETED) is False
    assert is_retryable(TaskStatus.RUNNING) is False
    assert is_retryable(TaskStatus.PENDING) is False
    assert RETRYABLE_STATUSES == frozenset({TaskStatus.FAILED})


def test_finalized_covers_failed_completed_and_dead_letter() -> None:
    """已定局的三种状态都不得被跨副本 CAS 重放改写。

    FAILED 也在里面：另一副本已经判定失败并写进 last_error，重放不能改成 COMPLETED。
    """
    assert is_finalized(TaskStatus.FAILED) is True
    assert is_finalized(TaskStatus.COMPLETED) is True
    assert is_finalized(TaskStatus.DEAD_LETTER) is True
    assert is_finalized(TaskStatus.RUNNING) is False
    assert is_finalized(TaskStatus.PENDING) is False
    assert FINALIZED_STATUSES == frozenset(
        {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.DEAD_LETTER}
    )


def test_is_dead_letter() -> None:
    assert is_dead_letter(TaskStatus.DEAD_LETTER) is True
    assert is_dead_letter(TaskStatus.FAILED) is False


@pytest.mark.parametrize("status", list(TaskStatus))
def test_dead_letter_is_never_retryable(status: TaskStatus) -> None:
    """DEAD_LETTER 永远不可重试 —— 它的预算已经用完了。

    FAILED 同时「可重试」且「已定局」并不矛盾：这是两个正交的轴 ——
    前者说「调用方可以再试一次」，后者说「跨副本重放不得改写它」。
    """
    if status is TaskStatus.DEAD_LETTER:
        assert is_retryable(status) is False
    assert is_finalized(TaskStatus.DEAD_LETTER) is True

