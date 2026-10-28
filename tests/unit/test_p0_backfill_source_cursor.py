"""P0 补齐记忆的来源游标必须带时间戳。

真实栈证据：用户纠正沟通偏好后，新值（「简洁直接的建议」）进不了索引 ——
冲突仲裁按「来源游标」比新旧，而游标的第一档是「时间戳从哪来」：
调用方给的 4 档、库里读出的 3 档、round_id 数字 1 档。档位优先于时间，
于是「库里读出的时间戳」在任何 mem0 抽取记忆面前都算更旧，P0 补齐的
源规范化记忆被直接跳过（indexed_count=0），用户的最新偏好丢失。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from thinkback.memory.backends import FakeMemoryBackend
from thinkback.memory.repositories import InMemoryMemoryRepository
from thinkback.memory.schemas import (
    AppendMemoryRequest,
    MemoryMessage,
    MessageRole,
)
from thinkback.memory.service import MemoryService


def _append(round_id: str, content: str, timestamp: str) -> AppendMemoryRequest:
    return AppendMemoryRequest(
        request_id=f"req-{round_id}",
        user_id="user-1",
        session_id="session-1",
        round_id=round_id,
        source_timestamp=datetime.fromisoformat(timestamp),
        messages=[
            MemoryMessage(
                message_id=f"{round_id}-u",
                role=MessageRole.USER,
                content=content,
                timestamp=timestamp,
            ),
            MemoryMessage(
                message_id=f"{round_id}-a",
                role=MessageRole.ASSISTANT,
                content="好的。",
                timestamp=timestamp,
            ),
        ],
    )


def test_p0_backfill_memory_carries_source_timestamp() -> None:
    """P0 补齐的记忆要带来源时间戳，游标才和 mem0 抽取的记忆可比。"""
    repository = InMemoryMemoryRepository()
    service = MemoryService(
        repository=repository,
        backend=FakeMemoryBackend(),
        l3_write_mode="sync",
    )

    service.append(
        _append(
            "round-cp",
            "沟通偏好更新：我现在更想要简洁直接的建议，不需要先安慰。",
            "2026-05-04T10:00:03Z",
        )
    )

    indexed = repository.active_memories("user-1", "thinkback")
    preferences = [
        memory
        for memory in indexed
        if memory.memory_text.startswith("User communication preference")
    ]
    assert preferences, "P0 补齐应写入沟通偏好记忆"
    assert preferences[0].metadata.get("source_timestamp"), (
        "P0 补齐的记忆必须带来源时间戳：冲突仲裁的游标按档位比较，"
        "缺时间戳会退到 round_id 数字档，在 mem0 抽取记忆面前永远算更旧"
    )


class _ExtractingBackend(FakeMemoryBackend):
    """模拟 mem0 真的抽出一条事实（metadata 里带自己的 source_timestamp）。"""

    def add(
        self,
        messages: list[dict[str, str]],
        *,
        user_id: str,
        memory_scope_id: str,
        metadata: dict[str, Any] | None = None,
        infer: bool = True,
        expiration_date: str | None = None,
        prompt: str | None = None,
    ) -> list[dict[str, Any]]:
        _ = (infer, expiration_date, prompt)
        text = " ".join(
            message["content"].strip() for message in messages if message.get("content", "").strip()
        )
        # 只在第一轮产出抽取结果：纠正轮模型没抽出同槽位事实（真实栈就是
        # 这样 —— 纠正轮抽出的是另一条事实），此时「源规范化」是唯一能表达
        # 新偏好的路径，它被跳过就等于用户的新偏好彻底丢失。
        if "不喜欢被说教" not in text:
            return []
        memory_text = "用户不喜欢被说教"
        memory_id = f"fake-{uuid4()}"
        self.memories[memory_id] = {
            "id": memory_id,
            "memory": memory_text,
            "event": "ADD",
            "user_id": user_id,
            "agent_id": memory_scope_id,
            "metadata": metadata or {},
            "score": 1.0,
        }
        return [self.memories[memory_id]]


def test_newer_preference_supersedes_older_extracted_one() -> None:
    """后端抽出的旧偏好存在时，新偏好（源规范化）必须能顶掉它。

    这条复现真实栈故障：mem0 抽出的记忆 metadata 带 source_timestamp
    （游标 4 档），P0 补齐的记忆此前拿不到时间戳只能退到 round_id 数字
    （1 档），档位优先于时间 → 新偏好被判「更旧」直接跳过，用户最新的
    沟通偏好进不了索引，召回只能召回旧值。
    """
    repository = InMemoryMemoryRepository()
    service = MemoryService(
        repository=repository,
        backend=_ExtractingBackend(),
        l3_write_mode="sync",
    )

    service.append(
        _append(
            "round-cp-old",
            "沟通偏好更新：我不喜欢被说教。",
            "2026-05-04T10:00:03Z",
        )
    )
    service.append(
        _append(
            "round-cp-new",
            "沟通偏好也更新：我现在更想要简洁直接的建议，不需要先安慰。",
            "2026-05-04T10:05:03Z",
        )
    )

    active = repository.active_memories("user-1", "thinkback")
    active_texts = [memory.memory_text for memory in active]
    assert any("简洁直接" in text for text in active_texts), (
        f"新偏好应为 ACTIVE，实际：{active_texts}"
    )
    assert not any("不喜欢被说教" in text for text in active_texts), (
        f"旧偏好应被 supersede，实际：{active_texts}"
    )
