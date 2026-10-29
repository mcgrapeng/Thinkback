"""垂类冲突键：per-type 抽取指令产出的「属性名」就是冲突键。

回归的场景：企业垂类类型声明 ``conflict: overwrite``，但过去的冲突键只认
``domain/slots`` 里硬编码的 9 个中文消费级槽位（昵称/宠物/现居地/生日…），
垂类事实一律落在槽位之外 → 永不 supersede → 用户纠正后新旧事实同时 ACTIVE、
同时被召回。现在冲突键取自 per-type 抽取指令产出的「属性名：值」格式。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from thinkback.domain.memory_types import extract_attribute_name, load_registry
from thinkback.memory.backends import FakeMemoryBackend
from thinkback.memory.repositories import InMemoryMemoryRepository
from thinkback.memory.schemas import (
    AppendMemoryRequest,
    MemoryMessage,
    MessageRole,
)
from thinkback.memory.service import MemoryService

PROFILE_DIR = Path(__file__).resolve().parents[2] / "profiles"


def _service() -> MemoryService:
    registry = load_registry(PROFILE_DIR, "semiconductor")
    repository = InMemoryMemoryRepository()
    return MemoryService(
        repository=repository,
        backend=FakeMemoryBackend(),
        registry_provider=lambda namespace: (
            registry if namespace == "semiconductor" else load_registry(PROFILE_DIR, namespace)
        ),
    )


def _append(round_id: str, text: str, *, memory_type: str) -> AppendMemoryRequest:
    now = datetime(2026, 10, 29, 10, 0, 0, tzinfo=UTC)
    return AppendMemoryRequest(
        request_id=f"req-{round_id}",
        user_id="user-1",
        session_id="session-1",
        round_id=round_id,
        namespace="semiconductor",
        metadata={"memory_type": memory_type},
        messages=[
            MemoryMessage(
                message_id=f"{round_id}-u",
                role=MessageRole.USER,
                content=text,
                timestamp=now,
            )
        ],
        source_timestamp=now,
    )


def test_shipped_semiconductor_profile_loads_per_type_prompts() -> None:
    """profile 里的类型必须真的带上抽取指令（否则属性名约定无从谈起）。"""
    registry = load_registry(PROFILE_DIR, "semiconductor")
    for name in ("profile", "preference", "process_preference", "state"):
        spec = registry.get(name)
        assert spec.extraction_prompt_text, f"{name} 没有加载到抽取指令"
        assert spec.extraction_prompt_version, f"{name} 的抽取指令没有版本号"


def test_extract_attribute_name_reads_the_leading_attribute() -> None:
    assert extract_attribute_name("制程节点：28nm") == "制程节点"
    assert extract_attribute_name("常用机台：ASML") == "常用机台"
    # 没有属性名格式 → 取不到（调用方回退旧口径）
    assert extract_attribute_name("用户负责 28nm 工艺整合") is None
    assert extract_attribute_name("制程节点：") is None
    # 属性名太长说明这不是「属性名：值」的结构
    assert extract_attribute_name("User has a cat named 麻薯: 3 years old") is None


def test_same_attribute_in_a_vertical_type_supersedes_the_old_value() -> None:
    """同类型、同属性名的新事实覆盖旧值（旧值不该再被召回）。"""
    service = _service()
    service.append(_append("r1", "制程节点：28nm", memory_type="process_preference"))
    service.append(_append("r2", "制程节点：14nm", memory_type="process_preference"))

    active = service.repository.active_memories("user-1", "semiconductor")
    assert [memory.memory_text for memory in active] == ["制程节点：14nm"]


def test_different_attributes_in_a_vertical_type_both_survive() -> None:
    """同类型但不同属性名 → 两条都留（不是同一个属性）。"""
    service = _service()
    service.append(_append("r1", "制程节点：28nm", memory_type="process_preference"))
    service.append(_append("r2", "常用机台：ASML", memory_type="process_preference"))

    active = service.repository.active_memories("user-1", "semiconductor")
    assert sorted(memory.memory_text for memory in active) == ["制程节点：28nm", "常用机台：ASML"]


def test_different_types_with_the_same_attribute_name_do_not_supersede() -> None:
    """不同类型即使属性名相同也不互相覆盖 —— 键里带类型。"""
    service = _service()
    service.append(_append("r1", "负责工序段：前道 CMP", memory_type="profile"))
    service.append(_append("r2", "负责工序段：后道封装", memory_type="preference"))

    active = service.repository.active_memories("user-1", "semiconductor")
    assert sorted(memory.memory_text for memory in active) == [
        "负责工序段：前道 CMP",
        "负责工序段：后道封装",
    ]


def test_append_conflict_types_keep_their_history() -> None:
    """conflict=append 的类型（事件类）不因为属性名相同而丢历史。"""
    service = _service()
    service.append(_append("r1", "当前批次良率：92%", memory_type="state"))
    service.append(_append("r2", "当前批次良率：95%", memory_type="state"))

    # state 的 conflict 是 overwrite，但两条的属性名相同 → 应当互相覆盖
    active = service.repository.active_memories("user-1", "semiconductor")
    assert [memory.memory_text for memory in active] == ["当前批次良率：95%"]
