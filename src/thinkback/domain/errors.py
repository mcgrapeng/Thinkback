"""类型化领域错误。

以既有异常基类（ValueError/RuntimeError）为根，保持 HTTP/gRPC 的
消息子串映射兼容；类型化让仓储/服务层可以精确捕获，不再依赖字符串。
"""

from __future__ import annotations


class TaskStaleWriteError(RuntimeError):
    """任务乐观锁冲突：快照 ``row_version`` 落后于库中当前值。

    多副本部署下，另一副本已基于更新的快照提交了写入；本方的
    get→mutate→save 序列基于过期状态，必须重读重放或放弃，
    而不是静默覆盖（否则丢失更新：retry_count 少计、
    pending_cleanup_tasks 计数失真、状态回跳）。
    """

    def __init__(self, task_id: str, expected_version: int) -> None:
        self.task_id = task_id
        self.expected_version = expected_version
        super().__init__(f"task stale write: {task_id} row_version {expected_version} outdated")


class TaskLockExhaustedError(RuntimeError):
    """乐观锁重试耗尽：终态写没有落库。

    与 :class:`TaskStaleWriteError` 区分开：后者是「快照过期，重读重放」
    （可恢复）；本异常是「重读重放也一直冲突」，状态**没有**被写下去。
    终态写（append 的 COMPLETED）遇到它必须让调用方看到失败 ——
    否则调用方会照常返回 completed，而 task 行还停在 RUNNING、result 为空，
    之后每次重试都命中 already_done，不一致被永久固化。
    """

    def __init__(self, task_id: str, attempts: int) -> None:
        self.task_id = task_id
        self.attempts = attempts
        super().__init__(
            f"task optimistic lock contention exhausted after {attempts} attempts: {task_id}"
        )
