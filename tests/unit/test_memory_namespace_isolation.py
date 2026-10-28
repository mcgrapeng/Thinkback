"""namespace 隔离的读、删、擦除矩阵。

同一 user、同一 session 下，不同 namespace 的 L1 原文、L2 摘要、L3 事实
必须互不可见；一个 namespace 的删除/擦除不得影响另一个 namespace。
"""

from __future__ import annotations

from datetime import UTC, datetime

from thinkback.memory.backends import FakeMemoryBackend
from thinkback.memory.repositories import InMemoryMemoryRepository
from thinkback.memory.schemas import (
    AppendMemoryRequest,
    DeleteMemoryRequest,
    DeleteScope,
    RecallMemoryRequest,
)
from thinkback.memory.service import MemoryService

USER = "user-1"
SESSION = "session-1"


def _append(
    round_id: str,
    content: str,
    *,
    namespace: str,
    user_id: str = USER,
    session_id: str = SESSION,
) -> AppendMemoryRequest:
    ts = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    return AppendMemoryRequest(
        request_id=f"req-{namespace}-{round_id}",
        user_id=user_id,
        session_id=session_id,
        round_id=round_id,
        namespace=namespace,
        source_timestamp=ts,
        messages=[
            {
                "message_id": f"{round_id}-u",
                "role": "user",
                "content": content,
                "timestamp": ts,
            },
            {
                "message_id": f"{round_id}-a",
                "role": "assistant",
                "content": "好的。",
                "timestamp": ts,
            },
        ],
    )


def _service() -> MemoryService:
    return MemoryService(
        repository=InMemoryMemoryRepository(),
        backend=FakeMemoryBackend(),
        l3_write_mode="sync",
    )


def _l1_and_l2_text(service: MemoryService, namespace: str) -> tuple[list[str], list[str]]:
    response = service.recall(
        RecallMemoryRequest(
            user_id=USER,
            session_id=SESSION,
            namespace=namespace,
            query="记忆",
        )
    )
    l1 = [item.content for item in response.items if item.layer == "L1"]
    l2 = [item.content for item in response.items if item.layer == "L2"]
    return l1, l2


def test_same_session_two_namespaces_l1_isolated() -> None:
    """alpha 的召回不得包含 beta 的 L1 原文与 L2 摘要。"""
    service = _service()
    service.append(_append("r-alpha", "alpha private fact", namespace="alpha"))
    service.append(_append("r-beta", "beta private fact", namespace="beta"))

    l1, l2 = _l1_and_l2_text(service, "alpha")

    assert l1, "alpha 的 L1 应该有内容"
    assert all("beta private fact" not in text for text in l1)
    assert all("beta private fact" not in text for text in l2)

    beta_l1, beta_l2 = _l1_and_l2_text(service, "beta")
    assert all("alpha private fact" not in text for text in beta_l1)
    assert all("alpha private fact" not in text for text in beta_l2)


def test_delete_all_in_namespace_keeps_other_namespace_intact() -> None:
    """alpha 的 ALL 删除不得墓碑化 beta 的轮次，也不得让 beta 的 L3 失效。"""
    repository = InMemoryMemoryRepository()
    backend = FakeMemoryBackend()
    service = MemoryService(repository=repository, backend=backend, l3_write_mode="sync")
    service.append(_append("r-alpha", "alpha private fact", namespace="alpha"))
    service.append(_append("r-beta", "beta private fact", namespace="beta"))
    beta_memories_before = len(repository.list_memories(USER, "beta"))

    service.delete(
        DeleteMemoryRequest(
            request_id="req-delete",
            user_id=USER,
            scope=DeleteScope.ALL,
            operation_id="op-delete-alpha",
            namespace="alpha",
        )
    )

    beta_round = repository.get_round("r-beta")
    assert beta_round is not None
    assert beta_round.round_state == "active"
    assert len(repository.list_memories(USER, "beta")) == beta_memories_before
    beta_l1, _ = _l1_and_l2_text(service, "beta")
    assert any("beta private fact" in text for text in beta_l1)


def test_erase_session_in_namespace_keeps_other_namespace_rounds() -> None:
    """alpha 的会话擦除不得删掉 beta 的轮次原文。"""
    repository = InMemoryMemoryRepository()
    service = MemoryService(
        repository=repository, backend=FakeMemoryBackend(), l3_write_mode="sync"
    )
    service.append(_append("r-alpha", "alpha private fact", namespace="alpha"))
    service.append(_append("r-beta", "beta private fact", namespace="beta"))

    service.erase(
        DeleteMemoryRequest(
            request_id="req-erase",
            user_id=USER,
            scope=DeleteScope.SESSION,
            operation_id="op-erase-alpha",
            session_id=SESSION,
            namespace="alpha",
        )
    )

    beta_round = repository.get_round("r-beta")
    assert beta_round is not None
    assert beta_round.round_state == "active"
    beta_l1, _ = _l1_and_l2_text(service, "beta")
    assert any("beta private fact" in text for text in beta_l1)
