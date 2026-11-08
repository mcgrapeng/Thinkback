"""擦除（erase）的**阶段断点续跑**：失败重试必须跳过已完成的阶段。

擦除是分阶段做的（``_run_erase_stages``）：

```
barrier  →  backend  →  history  →  l1  →  audit  →  index
```

每个阶段做完就把阶段名记进任务的 ``done_stages``。中途失败重试时，
从已完成阶段**之后**继续 —— 已做过的阶段不能重做（重复副作用、可能重复计数），
没做过的阶段**不能跳过**（留下没擦干净的原文）。

每一步都是隐私相关的：

- ``barrier`` —— 先落墓碑，防止被擦的轮次被写回来
- ``backend`` —— 删引擎里的事实
- ``history`` —— 删引擎侧历史与**原始消息表**（原文就在那儿）
- ``l1`` —— 删本地回合流水与 L1 缓存
- ``index`` —— 最后删本地索引行

**这些「从断点继续」的分支此前从未被走到**：单测都是一次跑完，
``done`` 永远是空集，于是每个 ``if "xxx" not in done`` 的**假分支**
（跳过已完成的阶段）从未被验证过。

也就是说：**如果某阶段失败后重试会重复执行已完成的阶段，现有测试一个都发现不了** ——
而这正是「擦除残留 0 条」这条产品承诺最脆弱的地方。

本文件用**真实计划**做续跑：先跑一次真擦除拿到服务自己算出的 plan，
把它留在任务里，再改 ``done_stages`` 制造「断点」，重跑同一个 operation_id。
计划是服务算的，不是手搓的 —— 手搓的键格式与服务算出的不一致，
那种测试会「跑过了但什么都没验证」。
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import pytest

from thinkback.domain.enums import DeleteScope, TaskStatus
from thinkback.memory.backends import FakeMemoryBackend
from thinkback.memory.repositories import InMemoryMemoryRepository
from thinkback.memory.schemas import AppendMemoryRequest, DeleteMemoryRequest, MessageRole
from thinkback.memory.service import MemoryService

#: 擦除的阶段顺序（``_run_erase_stages`` 里的实际顺序）。
_STAGES = ("barrier", "backend", "history", "l1", "audit", "index")

_TS = datetime(2026, 11, 5, tzinfo=UTC)


class _RecordingBackend(FakeMemoryBackend):
    """记录每个副作用被调用了几次 —— 用来断言「已完成的阶段没重做」。"""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    def add(self, messages: list[dict[str, str]], **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append("add")
        return super().add(messages, **kwargs)

    def search(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append("search")
        return super().search(query, **kwargs)

    def delete(self, memory_id: str, **kwargs: Any) -> Any:
        self.calls.append(f"delete:{memory_id}")
        return super().delete(memory_id, **kwargs)

    def purge_history(self, **kwargs: Any) -> dict[str, int]:
        self.calls.append("purge_history")
        return super().purge_history(**kwargs)


def _service() -> tuple[MemoryService, _RecordingBackend, InMemoryMemoryRepository]:
    backend = _RecordingBackend()
    repository = InMemoryMemoryRepository()
    service = MemoryService(
        repository=repository,
        backend=backend,
        l3_write_mode="sync",
    )
    return service, backend, repository


def _append(service: MemoryService, round_id: str, content: str = "我叫麻薯") -> None:
    service.append(
        AppendMemoryRequest(
            request_id=f"req-{round_id}",
            user_id="u1",
            session_id="s1",
            round_id=round_id,
            messages=[
                {
                    "message_id": f"m-{round_id}",
                    "role": MessageRole.USER,
                    "content": content,
                    "timestamp": _TS,
                }
            ],
            source_timestamp=_TS,
            metadata={},
        )
    )


def _erase_request(operation_id: str) -> DeleteMemoryRequest:
    return DeleteMemoryRequest(
        request_id=f"req-erase-{operation_id}",
        user_id="u1",
        session_id=None,
        scope=DeleteScope.ALL,
        operation_id=operation_id,
        namespace="thinkback",
    )


def _run_erase_and_keep_plan(
    service: MemoryService,
    backend: _RecordingBackend,
    repository: InMemoryMemoryRepository,
    operation_id: str,
) -> dict[str, Any]:
    """跑一次真擦除，把服务算出的 plan 留下来。

    计划是服务按当前状态算的（哪些记忆、哪些会话、哪些轮次）。
    手搓计划会与真实键格式不符 —— 那样「跑过了」也什么都没验证。
    """
    _append(service, "r1")
    service.erase(_erase_request(operation_id))
    time.sleep(0.2)

    task = repository.get_task(f"memory-erase:{operation_id}")
    assert task is not None, "擦除任务行没建起来"
    plan = dict(task.result.get("plan") or {})
    assert plan, "擦除计划没落库"
    return plan


def _reseed_with_done_stages(
    service: MemoryService,
    repository: InMemoryMemoryRepository,
    operation_id: str,
    done_stages: list[str],
) -> None:
    """把任务重置成「已完成这些阶段」的断点状态。

    状态必须是 **FAILED**：``erase()`` 看到已有任务时按状态分流 ——
    ``COMPLETED`` 直接返回旧结果、``RUNNING``/``DEAD_LETTER`` 回报「进行中」，
    只有 ``FAILED``/``PENDING`` 才走「按计划续跑」那条路（那正是断点续跑）。

    计划要**重算**：还原现场时写入的是新的记忆（新的 memory_id），
    沿用旧计划的话，「继续删」删的是已经不存在的旧 id ——
    断言会恒真，而且掩盖了「续跑删错对象」这种真缺陷。
    """
    # 先读任务行（``get_task`` 返回深拷贝），再还原现场 ——
    # 顺序反了的话，``_append`` 之后任务行可能已经不是原来那个快照。
    task = repository.get_task(f"memory-erase:{operation_id}")
    assert task is not None, "擦除任务行不见了"

    # 还原现场：把被擦掉的内容重新写回去，让「继续删」真的有对象可删。
    # 必须用**新的 round_id** —— 擦除会落墓碑，同一个 round_id 再写会被
    # 「source round was erased」挡住（那是防「擦完又写回来」的正确行为）。
    _append(service, "r1-restored")

    request = _erase_request(operation_id)
    plan = service._plan_erase(  # noqa: SLF001
        request,
        service._long_term_scope_id(request),  # noqa: SLF001
        service._session_scope_id(request),  # noqa: SLF001
    )
    task.result = {**task.result, "plan": plan, "done_stages": sorted(done_stages)}
    task.status = TaskStatus.FAILED
    repository.save_task(task)


def test_an_erase_resumes_after_the_completed_stages() -> None:
    """已完成 ``barrier`` 的擦除任务重试时，**不再**重复落屏障。

    走的是 ``if "barrier" not in done`` 的**假分支**。
    """
    service, backend, repository = _service()
    operation_id = "op-barrier"

    _run_erase_and_keep_plan(service, backend, repository, operation_id)
    _reseed_with_done_stages(service, repository, operation_id, ["barrier"])
    backend.calls.clear()
    service.erase(_erase_request(operation_id))
    time.sleep(0.2)

    # 屏障相关的作用力（墓碑写入）不该重复发生
    assert "add" not in backend.calls, f"已完成 barrier 阶段却又执行了一遍：{backend.calls}"


def test_an_erase_with_all_stages_done_does_not_redo_them() -> None:
    """所有阶段都已完成的任务重试 → 一个副作用都不该重复发生。

    这是「擦除残留 0 条」承诺的兜底：全做完了还重做，
    轻则重复计数，重则把不该删的东西删了。
    """
    service, backend, repository = _service()
    operation_id = "op-all-done"

    _run_erase_and_keep_plan(service, backend, repository, operation_id)
    _reseed_with_done_stages(service, repository, operation_id, list(_STAGES))
    backend.calls.clear()
    service.erase(_erase_request(operation_id))
    time.sleep(0.2)

    assert "purge_history" not in backend.calls, (
        f"history 阶段已完成却又执行了一遍：{backend.calls}"
    )
    assert not [call for call in backend.calls if call.startswith("delete:")], (
        f"backend 阶段已完成却又删了一遍：{backend.calls}"
    )


def test_an_erase_resumes_history_after_the_backend_stage() -> None:
    """``backend`` 已完成、``history`` 未完成 → 继续清 history。

    漏掉这一步 = 引擎侧 history 表里的**原文**没删 ——
    「擦除残留 0 条」直接不成立。
    """
    service, backend, repository = _service()
    operation_id = "op-history"

    _run_erase_and_keep_plan(service, backend, repository, operation_id)
    _reseed_with_done_stages(service, repository, operation_id, ["barrier", "backend"])
    backend.calls.clear()
    service.erase(_erase_request(operation_id))
    time.sleep(0.2)

    assert "purge_history" in backend.calls, f"history 阶段没被执行（原文残留）：{backend.calls}"


def test_an_erase_resumes_l1_after_the_history_stage() -> None:
    """``history`` 已完成、``l1`` 未完成 → 继续清本地回合流水。

    L1 留着 = 被擦的轮次原文还在本地库里。
    """
    service, backend, repository = _service()
    operation_id = "op-l1"

    _run_erase_and_keep_plan(service, backend, repository, operation_id)
    _reseed_with_done_stages(service, repository, operation_id, ["barrier", "backend", "history"])
    # 断点现场：这一轮是**第一次擦除之后**才写进去的，只有续跑时 L1 阶段真跑了
    # 才会被删掉。（早先这里断言的是 "r1" —— 它在第一次擦除时就已被删，
    # 那个断言恒真，续跑漏掉 L1 阶段它也照样过。）
    assert repository.get_round("r1-restored") is not None, "断点现场没还原成功"
    service.erase(_erase_request(operation_id))
    time.sleep(0.2)

    assert repository.get_round("r1-restored") is None, "L1 阶段没跑，被擦的轮次原文还在本地"


def test_an_erase_resumes_the_index_after_the_audit_stage() -> None:
    """``audit`` 已完成、``index`` 未完成 → 继续删本地索引行。

    索引行留着 = 被擦的记忆在 ``/items`` 里还看得见。
    """
    service, backend, repository = _service()
    operation_id = "op-index"

    _run_erase_and_keep_plan(service, backend, repository, operation_id)
    _reseed_with_done_stages(
        service, repository, operation_id, ["barrier", "backend", "history", "l1", "audit"]
    )
    service.erase(_erase_request(operation_id))
    time.sleep(0.2)

    remaining = [
        memory
        for memory in repository.active_memories("u1", "thinkback")
        if not memory.metadata.get("delete_all_tombstone")
    ]
    assert remaining == [], f"index 阶段没跑，本地索引里还留着 {len(remaining)} 条"


@pytest.mark.parametrize(
    "done",
    [
        ["barrier"],
        ["barrier", "backend"],
        ["barrier", "backend", "history"],
        ["barrier", "backend", "history", "l1"],
        ["barrier", "backend", "history", "l1", "audit"],
    ],
)
def test_every_partial_progress_resumes_to_completion(done: list[str]) -> None:
    """每一个「断点位置」都能续跑到完成。

    逐个断点位置验证：重试后任务落到 COMPLETED。
    """
    service, backend, repository = _service()
    operation_id = f"op-{'-'.join(done)}"

    _run_erase_and_keep_plan(service, backend, repository, operation_id)
    _reseed_with_done_stages(service, repository, operation_id, list(done))
    service.erase(_erase_request(operation_id))
    time.sleep(0.2)

    task = repository.get_task(f"memory-erase:{operation_id}")
    assert task is not None
    assert task.status is TaskStatus.COMPLETED, (
        f"断点 {done} 续跑后任务状态是 {task.status}，不是 COMPLETED"
    )
