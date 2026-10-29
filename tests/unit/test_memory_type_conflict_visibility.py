"""类型声明的冲突策略必须真的生效；生效不了时必须看得见。

引擎（mem0 2.2.1）的写入路径只产 ADD、不裁决冲突，所以类型声明的
``conflict`` 是**唯一**的覆盖机制。Thinkback 自己算冲突键：

- 类型带 per-type 抽取指令 → 键取「属性名：值」里的属性名；
- 类型不带（内置类型全部如此）→ 键回退到 P0 槽位，而 P0 槽位是一份
  消费级中文词表。

于是内置类型 + 企业事实（算不出槽位）→ 键为 ``None`` → 覆盖机制不存在：
旧值不会被覆盖，新旧两条并存。**改口了，旧事实还在。**

本文件钉三件事：

1. 这个行为是真的（两条企业事实都在）—— 不假装它是好的；
2. 它现在**可见**（一条带类型名与策略名的 warning），运维能发现自己的
   冲突策略是空配置；
3. 两条本来就正常的路没被弄坏：槽位事实照常覆盖，append 类型照常并存。
"""

from __future__ import annotations

from loguru import logger

from thinkback.memory.backends import FakeMemoryBackend
from thinkback.memory.repositories import InMemoryMemoryRepository
from thinkback.memory.schemas import AppendMemoryRequest, MessageRole
from thinkback.memory.service import MemoryService


def _append(round_id: str, content: str, *, memory_type: str | None = None) -> AppendMemoryRequest:
    metadata = {"memory_type": memory_type} if memory_type else {}
    return AppendMemoryRequest(
        request_id=f"req-{round_id}",
        user_id="user-1",
        session_id="session-1",
        round_id=round_id,
        messages=[
            {
                "message_id": f"{round_id}-u",
                "role": MessageRole.USER,
                "content": content,
                "timestamp": "2026-05-04T10:00:00Z",
            },
            {
                "message_id": f"{round_id}-a",
                "role": MessageRole.ASSISTANT,
                "content": "好的。",
                "timestamp": "2026-05-04T10:00:03Z",
            },
        ],
        source_timestamp="2026-05-04T10:00:03Z",
        metadata=metadata,
    )


def _service() -> tuple[InMemoryMemoryRepository, MemoryService]:
    repository = InMemoryMemoryRepository()
    return repository, MemoryService(repository=repository, backend=FakeMemoryBackend())


def _capture_warnings() -> tuple[list[str], int]:
    """挂一个只收 WARNING 的 loguru sink。"""
    sink: list[str] = []
    handler_id = logger.add(sink.append, level="WARNING", format="{level} {message} {extra}")
    return sink, handler_id


def test_builtin_overwrite_type_leaves_both_enterprise_facts_active() -> None:
    """内置类型（无 per-type 抽取指令）+ 企业事实 → 算不出冲突键 → 两条都在。

    这是当前真实行为，不是期望行为。把它钉住，是为了让「冲突策略失效」
    这件事在测试里显形，而不是靠人读代码发现。
    """
    repository, service = _service()

    service.append(_append("r-sup-1", "用户的常用供应商是华为", memory_type="preference"))
    service.append(_append("r-sup-2", "用户的常用供应商是阿里", memory_type="preference"))

    active = repository.active_memories("user-1", "thinkback")
    assert len(active) == 2, "算不出冲突键时旧事实不会被覆盖，两条并存（当前真实行为）"
    assert any("华为" in memory.memory_text for memory in active)
    assert any("阿里" in memory.memory_text for memory in active)


def test_unresolvable_conflict_key_is_reported_with_type_and_policy() -> None:
    """失效的冲突策略必须报出来，并说清是哪个类型、声明的策略是什么。"""
    _, service = _service()
    sink, handler_id = _capture_warnings()
    try:
        service.append(_append("r-sup-warn", "用户的常用供应商是华为", memory_type="preference"))
    finally:
        logger.remove(handler_id)

    logs = "\n".join(sink)
    assert "memory conflict key unresolved" in logs
    assert "preference" in logs
    assert "overwrite" in logs


def test_slot_hitting_fact_still_overwrites_and_reports_nothing() -> None:
    """槽位事实算得出冲突键 → 照常覆盖，且不报失效警告（不制造噪音）。"""
    repository, service = _service()
    sink, handler_id = _capture_warnings()
    try:
        service.append(_append("r-slot-1", "我现在住在杭州。"))
        service.append(_append("r-slot-2", "我现在住在上海。"))
    finally:
        logger.remove(handler_id)

    active = repository.active_memories("user-1", "thinkback")
    assert len(active) == 1
    assert "上海" in active[0].memory_text
    assert "memory conflict key unresolved" not in "\n".join(sink)


def test_append_type_is_not_reported_as_a_dead_policy() -> None:
    """append 策略不靠冲突键生效 → 键算不出来也不该报。"""
    _, service = _service()
    sink, handler_id = _capture_warnings()
    try:
        service.append(_append("r-append-1", "用户的常用供应商是华为", memory_type="event"))
    finally:
        logger.remove(handler_id)

    assert "memory conflict key unresolved" not in "\n".join(sink)


def test_unregistered_type_is_rejected_before_reaching_the_conflict_path() -> None:
    """类型没注册 → 直接报错，不静默降级成别的类型。

    所以它根本走不到冲突键那一步：报的是「类型不存在」，不是「策略失效」。
    """
    import pytest

    _, service = _service()
    sink, handler_id = _capture_warnings()
    try:
        with pytest.raises(ValueError, match="not registered"):
            service.append(
                _append("r-unknown-1", "用户的常用供应商是华为", memory_type="not-a-type")
            )
    finally:
        logger.remove(handler_id)

    assert "memory conflict key unresolved" not in "\n".join(sink)
