"""核心链路残余缺口的收口（第十四轮收尾）。

这一轮把全套件的行覆盖缺口从 131 条压到 9 条，逐条分完类，结论如下。

**可达且值得钉（本文件 + SQL 分支文件）**

- ``service.py`` 心跳失败路径：一次 DB 抖动不能把心跳线程带走。
- ``service.py`` 召回相关性闸：语义分与综合分都不达标的引擎结果不进上下文包。

**可达但已有假覆盖（这里把被 stub 掉的行为真写一遍）**

``tests/unit/test_service_gap_batch1.py`` 里两条用例把**被测行为本身**
换成了 lambda，声称覆盖的分支其实一行没跑：

- ``test_a_failing_heartbeat_beat_does_not_stop_the_heart`` 换掉了
  ``_mutate_task_with_retry`` —— 心跳的失败路径根本没执行；
- ``test_recall_skips_backend_items_that_are_not_relevant`` 换掉了
  ``_l3_item_relevant`` —— 相关性闸门根本没执行。

那两条用例保留（它们断言的是对外形状），但**不能算数**：本文件用真实
仓储与真实后端重写这两条语义。

**死分支（登记，不造用例）**

登记一律引用**符号**而不是行号：行号会随任何一次提交漂移（本轮就被
并行工作线的两次提交挪动过），符号不会。

| 位置（符号） | 为什么到不了 |
| --- | --- |
| ``service.MemoryService.append`` 里的作用域补齐分支 | append 的两处入口都先过 ``_assert_round_matches_request``，作用域不等会先被 409 拒掉 |
| ``service.MemoryService._canonical_memory_text_from_source`` | 全仓零调用者（真实路径走 ``_canonical_memory_text_with_source``；只有 ``_index_l3_event`` 的流程图 docstring 提到过它）—— 死委托，建议删 |
| ``content_quality._score_clarity`` 的 ``length == 0`` 分支 | ``score_content`` / ``intrinsic_quality`` 都先对空白内容短路返回，它收不到空串 |
| ``content_quality._jaccard`` 的 ``if not union`` 分支 | 两侧皆空已在上一行返回，走到这里的 union 必非空 |
| ``replica_guard.assert_multi_replica_ready`` 的「前置齐全」分支 | 第一条前置「幂等与限流入 PG」的 ``satisfied`` 恒为 ``False``，永远凑不齐 |
| ``l2_refresh.L2BackgroundRefresher._finish`` 的 ``pass`` 分支 | ``_run_refresh`` 兜底捕获 ``Exception``，future 不会带异常出来 |
| ``schemas._validate_metadata_size`` 的 ``value is None`` 分支 | 三个调用点都在调用前先 ``if value is None: return value`` |
| ``mcp/__main__.py`` 的 ``sys.exit(main())`` | ``if __name__ == "__main__"`` 守卫，进程内导入不可达 |
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from thinkback.domain.entities import TaskEntry
from thinkback.domain.enums import OperationType, TaskStatus
from thinkback.memory.backends import FakeMemoryBackend
from thinkback.memory.repositories import InMemoryMemoryRepository
from thinkback.memory.schemas import RecallMemoryRequest
from thinkback.memory.service import MemoryService


def _service(**kwargs: Any) -> MemoryService:
    return MemoryService(
        repository=InMemoryMemoryRepository(),
        backend=FakeMemoryBackend(),
        **kwargs,
    )


# ── 心跳失败路径（service.py 1448-1454） ──────────────────────────────


def test_a_heartbeat_beat_that_fails_to_write_does_not_stop_the_heart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """心跳写库失败只记警告，下一拍继续：心跳停了，健康的长任务会被当孤儿回收。

    真写一遍：让仓储的 ``save_task`` 抛错（DB 抖动），走真实的
    ``_mutate_task_with_retry``，而不是把它换掉。
    """
    service = _service(task_heartbeat_seconds=0.01)
    task = TaskEntry(
        task_id="t-1",
        request_id="req-1",
        op_type=OperationType.WRITE_ROUND,
        scope={"user_id": "user-1"},
        status=TaskStatus.RUNNING,
        result={},
    )
    service.repository.save_task(task)

    attempts: list[str] = []

    def _boom(entry: TaskEntry) -> TaskEntry:
        attempts.append(entry.task_id)
        raise RuntimeError("db jitter")

    monkeypatch.setattr(service.repository, "save_task", _boom)

    def _slow_action() -> str:
        # 睡够几拍心跳间隔：失败之后必须还有下一拍。
        time.sleep(0.15)
        return "done"

    result = service._run_with_task_heartbeat(task.task_id, _slow_action)  # noqa: SLF001

    assert result == "done"
    assert len(attempts) >= 2, "心跳写失败后必须继续打下一拍，而不是就此停下"


# ── 召回相关性闸（service.py 1903） ─────────────────────────────────


def test_a_backend_item_below_the_relevance_threshold_is_not_delivered() -> None:
    """引擎结果分数不达标就不进上下文包：预算留给相关事实。

    真写一遍：不替换 ``_l3_item_relevant``，而是给引擎结果一个真实分数
    （语义分/综合分都没有 → 只看综合分）。
    """
    backend = FakeMemoryBackend()
    backend.memories["m-low"] = {
        "id": "m-low",
        "memory": "用户住在上海",
        "event": "ADD",
        "user_id": "user-1",
        "agent_id": "thinkback",
        "metadata": {},
        "score": 0.05,
    }
    backend.memories["m-high"] = {
        "id": "m-high",
        "memory": "用户住在上海",
        "event": "ADD",
        "user_id": "user-1",
        "agent_id": "thinkback",
        "metadata": {},
        "score": 0.9,
    }
    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=backend)
    repository.add_memory_index(
        backend_memory_id="m-high",
        user_id="user-1",
        source_refs=[{"session_id": "session-1", "round_id": "r-1"}],
        memory_text="居住城市：上海",
    )

    response = service.recall(
        RecallMemoryRequest(user_id="user-1", session_id="session-1", query="上海")
    )

    delivered = [item for item in response.items if item.layer == "L3"]
    # 正控制：达标的那条确实进了包（否则本用例会被「什么都没召回」骗过）。
    assert [item.content for item in delivered] == ["居住城市：上海"]
    assert all((item.score or 0.0) >= 0.5 for item in delivered)
