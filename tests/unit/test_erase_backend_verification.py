"""擦除的后端复核：宁可不报完成，也不能谎报完成。

擦除对外承诺的是「原文从每一个存储位置物理清掉」。后端 delete 对「读不到的
记忆」按幂等跳过处理，所以**光看 delete 的返回值无法证明记录真的没了**。
复核这一步就是来证明的：删完再列一次，还在 = 擦除没完成 = 任务失败重试。

判错的代价是双向的：

- 谎报完成 → 用户以为数据没了，实际原文还在引擎侧（合规事故）；
- 永远失败 → 用户永远擦不掉，重试一次丢一批新记忆（可用性事故）。

这三条分支（列表失败 / 页满 / 点查失败）在历史覆盖里一次都没被执行过。
"""

from __future__ import annotations

from typing import Any

import pytest

from thinkback.domain.enums import DeleteScope, TaskStatus
from thinkback.domain.ports import MEMORY_LIST_PAGE_LIMIT
from thinkback.memory.backends import FakeMemoryBackend
from thinkback.memory.repositories import InMemoryMemoryRepository
from thinkback.memory.schemas import AppendMemoryRequest, DeleteMemoryRequest
from thinkback.memory.service import MemoryService


class _LyingDeleteBackend(FakeMemoryBackend):
    """delete 谎报成功：实际不删记录（模拟 mem0 删不掉 / 删错作用域）。"""

    def delete(self, memory_id: str) -> None:
        return None


class _UnlistableBackend(FakeMemoryBackend):
    """列不出来：后端不可达。"""

    def get_all(
        self, *, user_id: str, memory_scope_id: str, limit: int = 200
    ) -> list[dict[str, Any]]:
        raise RuntimeError("backend unreachable")


class _UnqueryableBackend(FakeMemoryBackend):
    """点查失败：后端不可达。"""

    def get(self, memory_id: str) -> dict[str, Any] | None:
        raise RuntimeError("backend unreachable")


def _request(round_id: str) -> AppendMemoryRequest:
    return AppendMemoryRequest(
        request_id=f"req-{round_id}",
        user_id="user-1",
        session_id="session-1",
        round_id=round_id,
        source_timestamp="2026-10-29T10:00:00Z",
        messages=[
            {
                "role": "user",
                "content": "我的工号是 E-7788",
                "message_id": f"m-{round_id}",
                "timestamp": "2026-10-29T10:00:00Z",
            },
            {
                "role": "assistant",
                "content": "记住了",
                "message_id": f"ma-{round_id}",
                "timestamp": "2026-10-29T10:00:05Z",
            },
        ],
    )


def _seeded_service(
    backend: FakeMemoryBackend,
) -> tuple[InMemoryMemoryRepository, MemoryService, str]:
    """造一条已落库的记忆（本地索引 + 后端各一份），返回仓储、服务与业务记忆 ID。

    注意 delete 请求用的是**业务记忆 ID**（``memory_id``），不是后端记录 ID。
    """
    repository = InMemoryMemoryRepository()
    request = _request("round-erase-verify")
    round_entry = repository.save_round(request)
    repository.update_l1(round_entry)
    memory = repository.add_memory_index(
        backend_memory_id="erase-verify-memory",
        user_id="user-1",
        memory_scope_id="thinkback",
        source_refs=[{"session_id": "session-1", "round_id": "round-erase-verify"}],
        memory_text="工号是 E-7788",
    )
    backend.memories[memory.backend_memory_id] = {
        "id": memory.backend_memory_id,
        "memory": memory.memory_text,
        "event": "ADD",
        "user_id": "user-1",
        "agent_id": "thinkback",
        "metadata": {},
        "score": 1.0,
    }
    return repository, MemoryService(repository=repository, backend=backend), memory.memory_id


# ---------------------------------------------------------------------------
# 端到端：删不掉就报失败，不得报完成
# ---------------------------------------------------------------------------


def test_erase_fails_when_the_backend_still_holds_the_record() -> None:
    """后端记录没真删掉 → 擦除失败重试，不得谎报 completed。"""
    repository, service, memory_id = _seeded_service(_LyingDeleteBackend())

    with pytest.raises(RuntimeError, match="erase backend stage incomplete"):
        service.erase(
            DeleteMemoryRequest(
                request_id="req-erase-verify",
                user_id="user-1",
                scope=DeleteScope.MEMORY,
                operation_id="op-erase-verify",
                memory_id=memory_id,
            )
        )

    task = repository.get_task("memory-erase:op-erase-verify")
    assert task is not None
    assert task.status is not TaskStatus.COMPLETED, "后端还留着原文就报了完成"


def test_erase_fails_when_the_backend_cannot_be_verified() -> None:
    """复核做不了 → 按「未能证明已删」处理：失败，不谎报完成。"""
    repository, service, memory_id = _seeded_service(_UnlistableBackend())

    with pytest.raises(RuntimeError, match="erase backend stage incomplete"):
        service.erase(
            DeleteMemoryRequest(
                request_id="req-erase-unlistable",
                user_id="user-1",
                scope=DeleteScope.MEMORY,
                operation_id="op-erase-unlistable",
                memory_id=memory_id,
            )
        )

    task = repository.get_task("memory-erase:op-erase-unlistable")
    assert task is not None
    assert task.status is not TaskStatus.COMPLETED


# ---------------------------------------------------------------------------
# 复核逻辑本身
# ---------------------------------------------------------------------------


def test_residual_check_reports_targets_when_the_backend_cannot_be_listed() -> None:
    """列不出来 → 目标全部算残留：宁可失败也不谎报已删。"""
    _, service, _ = _seeded_service(_UnlistableBackend())

    residual = service._residual_backend_ids(
        user_id="user-1",
        memory_scope_id="thinkback",
        target_ids=["m-1", "m-2"],
        scope_all=False,
    )

    assert residual == ["m-1", "m-2"]


def test_residual_check_ignores_records_written_after_the_erase_plan() -> None:
    """ALL 范围：擦除期间新写的记忆不是这次的目标，不算残留。

    算成残留会让擦除永远失败，而每次重试都会把新记忆删掉 —— 用户越重试丢得越多。
    """
    backend = FakeMemoryBackend()
    _, service, _ = _seeded_service(backend)
    # 目标已删掉；擦除期间用户又新写了一条。
    backend.memories.pop("erase-verify-memory")
    backend.memories["written-after-plan"] = {
        "id": "written-after-plan",
        "memory": "用户后来又写的一条",
        "event": "ADD",
        "user_id": "user-1",
        "agent_id": "thinkback",
        "metadata": {},
        "score": 1.0,
    }

    residual = service._residual_backend_ids(
        user_id="user-1",
        memory_scope_id="thinkback",
        target_ids=["erase-verify-memory"],
        scope_all=True,
    )

    assert residual == []


def test_residual_check_point_queries_targets_when_the_page_limit_is_hit() -> None:
    """一页取满时光看列表判断不了目标还在不在 → 逐个点查目标 ID。

    不点查就直接判失败，会让后端记录超过一页的用户**每次**擦除都必然失败。
    """
    backend = FakeMemoryBackend()
    _, service, _ = _seeded_service(backend)
    for index in range(MEMORY_LIST_PAGE_LIMIT):
        backend.memories[f"filler-{index}"] = {
            "id": f"filler-{index}",
            "memory": f"filler {index}",
            "event": "ADD",
            "user_id": "user-1",
            "agent_id": "thinkback",
            "metadata": {},
            "score": 1.0,
        }

    residual = service._residual_backend_ids(
        user_id="user-1",
        memory_scope_id="thinkback",
        target_ids=["erase-verify-memory"],
        scope_all=False,
    )

    assert residual == ["erase-verify-memory"]


def test_point_lookup_failure_counts_as_still_present() -> None:
    """点查失败按「还在」处理：宁可让擦除失败重试，也不谎报「已擦除」。"""
    _, service, _ = _seeded_service(_UnqueryableBackend())

    assert service._present_backend_ids(["m-1", "", "m-2"]) == ["m-1", "m-2"]


def test_erased_message_texts_returns_the_exact_original_text() -> None:
    """引擎侧历史必须用**原文**精确删：抹掉空白就匹配不上，原文会留在引擎侧。"""
    repository = InMemoryMemoryRepository()
    request = _request("round-texts")
    repository.save_round(request)
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())

    texts = service._erased_message_texts(
        [{"session_id": "session-1", "round_id": "round-texts"}],
        "thinkback",
    )

    assert texts == ["我的工号是 E-7788", "记住了"]


def test_erased_message_texts_skips_rounds_from_another_namespace() -> None:
    """别的 namespace 的轮次不归这次擦除：原文不能被跨 namespace 清掉。"""
    repository = InMemoryMemoryRepository()
    request = _request("round-other-ns")
    repository.save_round(request)
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())

    texts = service._erased_message_texts(
        [{"session_id": "session-1", "round_id": "round-other-ns"}],
        "other-namespace",
    )

    assert texts == []
