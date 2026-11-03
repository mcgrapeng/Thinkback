"""service.py 第三批行覆盖缺口的逐行收口。

第一批（``test_service_core_decisions.py``）钉「决定结果」的主干分支，
第二批（``test_service_gap_batch2.py``）补幂等短路与回滚，这第三批补的是
**第三批剩下的边角**：重试预算、墓碑归属、召回回填的准入、冲突仲裁的
自我比较、canonical 回写。

这些行的共同点是**改坏了不会立刻有人发现**：

- 重试任务不涨 retry_count → 客户端可以无限重试，后端一直被抽；
- 墓碑不按 operation_id / session_id 归属 → 别人的擦除水位被当成本次的水位；
- 回填不看上下文词 → 不相关的槽位事实被塞进答案；
- 冲突仲裁把自己跟自己比 → 一条记忆会把自己判成「更旧」而被丢弃；
- canonical 有损折叠也回写后端 → 生日/雇主这类事实被永久截断。

行覆盖率只证明「这行跑过」，证明不了「结论对不对」。这里每个用例钉一个
具体结论：把分支反过来（跳过写成执行、执行写成跳过）时必须红。

所有依赖都是内存实现：``InMemoryMemoryRepository`` + ``FakeMemoryBackend``，
不碰网络、真实 LLM / Milvus / PG。
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime
from typing import Any

import pytest
from loguru import logger

from thinkback.domain.entities import JournalEntry, TaskEntry
from thinkback.infra import config as config_module
from thinkback.memory.backends import FakeMemoryBackend
from thinkback.memory.repositories import InMemoryMemoryRepository
from thinkback.memory.schemas import (
    DeleteMemoryRequest,
    DeleteScope,
    MemoryItem,
    MemoryStatus,
    OperationType,
    RebuildMemoryRequest,
    TaskStatus,
)
from thinkback.memory.service import MemoryService

LONG_TERM_SCOPE = "thinkback"
LOCATION_TEXT = "User lives in Shanghai"


def _service(**kwargs: Any) -> MemoryService:
    return MemoryService(
        repository=InMemoryMemoryRepository(), backend=FakeMemoryBackend(), **kwargs
    )


@pytest.fixture()
def log_sink() -> Generator[list[dict[str, Any]], None, None]:
    """捕获日志记录（含 extra），用来钉住作业打出去的账。"""
    records: list[dict[str, Any]] = []
    handler_id = logger.add(
        lambda message: records.append(
            {"message": str(message.record["message"]), **message.record["extra"]}
        ),
        level="DEBUG",
    )
    try:
        yield records
    finally:
        logger.remove(handler_id)


def _add_memory(
    repository: InMemoryMemoryRepository,
    *,
    backend_memory_id: str,
    memory_text: str = LOCATION_TEXT,
    session_id: str = "s1",
    round_id: str = "r1",
    metadata: dict[str, Any] | None = None,
) -> Any:
    """落一条本地索引行；metadata 里带上 source_refs 才能算出来源游标。"""
    source_refs = [{"session_id": session_id, "round_id": round_id}]
    return repository.add_memory_index(
        backend_memory_id=backend_memory_id,
        user_id="user-1",
        memory_scope_id=LONG_TERM_SCOPE,
        source_refs=source_refs,
        memory_text=memory_text,
        metadata={"source_refs": source_refs, **(metadata or {})},
    )


# ---------------------------------------------------------------------------
# 抽取任务的重试预算
# ---------------------------------------------------------------------------


def _failed_extract_task(
    repository: InMemoryMemoryRepository, *, retry_count: int = 0
) -> TaskEntry:
    task = TaskEntry(
        task_id="memory-extract:round-1",
        request_id="req-1",
        op_type=OperationType.WRITE_ROUND,
        scope={"user_id": "user-1", "memory_scope_id": LONG_TERM_SCOPE},
        status=TaskStatus.FAILED,
        retry_count=retry_count,
        last_error="backend unreachable",
    )
    repository.save_task(task)
    return task


def test_a_failed_extract_task_is_retried_with_a_higher_retry_count() -> None:
    """重试一条失败的抽取任务：retry_count 涨一次，任务复活成 RUNNING。

    不涨 retry_count 的话，客户端可以无限重试同一轮抽取。
    """
    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())
    _failed_extract_task(repository)

    task, should_submit = service._ensure_l3_extract_task(
        request_id="req-2", user_id="user-1", round_id="round-1"
    )

    assert should_submit is True
    assert task.retry_count == 1
    assert task.status is TaskStatus.RUNNING
    assert task.last_error is None
    saved = repository.get_task("memory-extract:round-1")
    assert saved is not None
    assert saved.retry_count == 1


def test_an_extract_task_over_the_retry_budget_goes_to_dead_letter() -> None:
    """重试预算耗尽：任务进 DEAD_LETTER 并抛错，不再提交抽取。"""
    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())
    _failed_extract_task(repository, retry_count=service.MAX_RETRIES)

    with pytest.raises(RuntimeError, match="dead_letter"):
        service._ensure_l3_extract_task(request_id="req-2", user_id="user-1", round_id="round-1")

    saved = repository.get_task("memory-extract:round-1")
    assert saved is not None
    assert saved.status is TaskStatus.DEAD_LETTER
    assert saved.retry_count == service.MAX_RETRIES + 1


# ---------------------------------------------------------------------------
# 删除请求的服务层兜底
# ---------------------------------------------------------------------------


def test_a_memory_delete_without_a_memory_id_is_rejected_by_the_service() -> None:
    """绕开 schema 校验的调用方也不能删错东西。"""
    service = _service()
    request = DeleteMemoryRequest(
        request_id="req-1",
        user_id="user-1",
        scope=DeleteScope.MEMORY,
        operation_id="op-1",
        memory_id="mem-1",
    ).model_copy(update={"memory_id": None})

    with pytest.raises(ValueError, match="memory_id is required"):
        service._delete_one_memory(request)


def test_a_session_delete_without_a_session_id_is_rejected_by_the_service() -> None:
    """会话级删除缺 session_id 必须拒绝：否则会扫掉整个用户范围。"""
    service = _service()
    request = DeleteMemoryRequest(
        request_id="req-1",
        user_id="user-1",
        scope=DeleteScope.SESSION,
        operation_id="op-1",
        session_id="s1",
    ).model_copy(update={"session_id": None})

    with pytest.raises(ValueError, match="session_id is required"):
        service._delete_session_memories(request)


# ---------------------------------------------------------------------------
# 墓碑的归属
# ---------------------------------------------------------------------------


def test_no_source_refs_means_no_source_ref_tombstone() -> None:
    """没有来源引用就没有墓碑：否则会造出一条指向空气的擦除屏障。"""
    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())

    service._add_deleted_source_ref_tombstone(
        user_id="user-1",
        memory_scope_id=LONG_TERM_SCOPE,
        source_refs=[],
        memory_text="被删掉的原文",
    )

    assert repository.list_memories("user-1", LONG_TERM_SCOPE) == []


def test_a_tombstone_of_another_operation_does_not_set_the_delete_cutoff() -> None:
    """墓碑按 operation_id 归属：别人的墓碑不能借来当本次的擦除水位。"""
    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())
    tombstone = repository.add_memory_index(
        backend_memory_id="delete-all:abc",
        user_id="user-1",
        memory_scope_id=LONG_TERM_SCOPE,
        source_refs=[],
        memory_text="delete_all tombstone",
        metadata={
            "delete_all_tombstone": True,
            "deleted_before": "2026-01-01T00:00:00+00:00",
            "operation_id": "op-other",
        },
    )
    repository.mark_memory_deleted(tombstone.memory_id)

    cutoff = service._delete_tombstone_cutoff(
        user_id="user-1",
        memory_scope_id=LONG_TERM_SCOPE,
        tombstone_key="delete_all_tombstone",
        operation_id="op-1",
    )

    assert cutoff is None


def test_another_sessions_delete_tombstone_does_not_set_the_session_cutoff() -> None:
    """会话级擦除的水位只认自己会话的墓碑。"""
    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())
    tombstone = repository.add_memory_index(
        backend_memory_id="delete-session:abc",
        user_id="user-1",
        memory_scope_id=LONG_TERM_SCOPE,
        source_refs=[{"session_id": "other-session", "round_id": "r-other"}],
        memory_text="delete_session tombstone",
        metadata={
            "session_delete_tombstone": True,
            "deleted_session_id": "other-session",
            "deleted_before": "2026-01-01T00:00:00+00:00",
        },
    )
    repository.mark_memory_deleted(tombstone.memory_id)

    assert service._session_delete_cutoff("user-1", LONG_TERM_SCOPE, "s1") is None


# ---------------------------------------------------------------------------
# 召回回填与触达
# ---------------------------------------------------------------------------


class _FailingTouchRepository(InMemoryMemoryRepository):
    """触达写失败：仓储不可用时召回不能跟着挂。"""

    def touch_memory_recalled(self, memory_ids: list[str]) -> int:
        raise RuntimeError("database is down")


def test_a_failing_touch_does_not_break_the_recall(
    log_sink: list[dict[str, Any]],
) -> None:
    """触达是锦上添花：写失败只记日志，召回结果照常返回。"""
    repository = _FailingTouchRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())
    entry = _add_memory(repository, backend_memory_id="backend-1")

    service._touch_recalled_entries([entry])

    warnings = [r for r in log_sink if r["message"] == "memory recall touch failed"]
    assert len(warnings) == 1
    assert warnings[0]["error_type"] == "RuntimeError"
    assert warnings[0]["count"] == 1


def test_a_backfill_without_query_slots_derives_the_slot_from_the_query() -> None:
    """没给 query_slots 时按 query 反推槽位：单槽位提问也能本地精确回填。"""
    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())
    memory = _add_memory(repository, backend_memory_id="backend-1")
    items: list[MemoryItem] = []

    filled = service._backfill_matching_slot_memories(
        items, query="我住在哪里", active_memories=[memory]
    )

    assert [item.memory_id for item in filled] == [memory.memory_id]
    assert items[0].metadata["slot_backfill"] == "current_location"


def test_a_backfill_without_a_derivable_slot_returns_nothing() -> None:
    """query 推不出槽位：回填没有目标就返回空，不猜。"""
    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())
    memory = _add_memory(repository, backend_memory_id="backend-1")
    items: list[MemoryItem] = []

    filled = service._backfill_matching_slot_memories(
        items, query="今天天气怎么样", active_memories=[memory]
    )

    assert filled == []
    assert items == []


def test_a_backfill_skips_a_memory_the_context_does_not_allow() -> None:
    """上下文词不相交的记忆不进回填：别把无关的槽位事实塞进答案。"""
    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())
    memory = _add_memory(
        repository,
        backend_memory_id="backend-1",
        memory_text="User favorite food: 火锅",
    )

    skipped_items: list[MemoryItem] = []
    skipped = service._backfill_matching_slot_memories(
        skipped_items,
        query="推荐一个晚餐餐厅",
        active_memories=[memory],
        query_slots=["favorite:food"],
    )

    assert skipped == []
    assert skipped_items == []

    # 对照：query 命中槽位时，同一条记忆照常回填 —— 排除它的只能是上下文闸门。
    allowed_items: list[MemoryItem] = []
    allowed = service._backfill_matching_slot_memories(
        allowed_items,
        query="我最喜欢吃什么",
        active_memories=[memory],
        query_slots=["favorite:food"],
    )

    assert [item.memory_id for item in allowed] == [memory.memory_id]


# ---------------------------------------------------------------------------
# P0 白名单
# ---------------------------------------------------------------------------


def test_a_p0_backfill_skips_a_slot_the_whitelist_excludes(
    monkeypatch: pytest.MonkeyPatch, log_sink: list[dict[str, Any]]
) -> None:
    """白名单外的槽位不进本地索引：只在日志里记一笔 skipped。"""
    monkeypatch.setattr(config_module.settings, "memory_p0_slots", "current_location")
    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())

    service._backfill_p0_slots_from_round_source(
        source_text="我养了一只猫，名字叫阿福。",
        user_id="user-1",
        memory_scope_id=LONG_TERM_SCOPE,
        source_refs=[{"session_id": "s1", "round_id": "r1"}],
        request_metadata={},
    )

    completed = [r for r in log_sink if r["message"] == "memory p0 slot backfill completed"]
    assert len(completed) == 1
    assert completed[0]["indexed_count"] == 0
    assert completed[0]["skipped_count"] == 1
    assert repository.list_memories("user-1", LONG_TERM_SCOPE) == []


# ---------------------------------------------------------------------------
# L3 事件的 canonical 回写
# ---------------------------------------------------------------------------


def _seed_existing_l3_memory(
    repository: InMemoryMemoryRepository,
    backend: FakeMemoryBackend,
    *,
    backend_memory_id: str,
    memory_text: str,
    backend_text: str,
) -> list[dict[str, str]]:
    source_refs = [{"session_id": "s1", "round_id": "r1"}]
    repository.add_memory_index(
        backend_memory_id=backend_memory_id,
        user_id="user-1",
        memory_scope_id=LONG_TERM_SCOPE,
        source_refs=source_refs,
        memory_text=memory_text,
        metadata={"source_refs": source_refs},
    )
    backend.memories[backend_memory_id] = {
        "id": backend_memory_id,
        "memory": backend_text,
        "event": "UPDATE",
        "user_id": "user-1",
        "agent_id": LONG_TERM_SCOPE,
        "metadata": {},
        "score": 1.0,
    }
    return source_refs


def test_a_lossless_canonical_change_is_written_back_to_the_backend() -> None:
    """canonical 无损时回写后端：mem0 里的噪声文本要跟上规范化口径。"""
    repository = InMemoryMemoryRepository()
    backend = FakeMemoryBackend()
    service = MemoryService(repository=repository, backend=backend)
    source_refs = _seed_existing_l3_memory(
        repository,
        backend,
        backend_memory_id="backend-1",
        memory_text="User has a cat named 麻薯",
        backend_text="  User has a cat named 麻薯  ",
    )

    service._index_l3_event(
        {"id": "backend-1", "event": "UPDATE", "memory": "  User has a cat named 麻薯  "},
        user_id="user-1",
        source_refs=source_refs,
        l3_metadata={
            "source_type": "chat_round",
            "data_classification": "normal",
            "memory_type": "profile",
        },
    )

    assert backend.get("backend-1")["memory"] == "User has a cat named 麻薯"


def test_a_lossy_canonical_fold_is_not_written_back_to_the_backend() -> None:
    """多事实原文的 canonical 是有损折叠：回写会永久截断生日/雇主这类事实。"""
    repository = InMemoryMemoryRepository()
    backend = FakeMemoryBackend()
    service = MemoryService(repository=repository, backend=backend)
    original = "我养了一只猫，名字叫阿福，生日是 3 月 3 日"
    source_refs = _seed_existing_l3_memory(
        repository,
        backend,
        backend_memory_id="backend-1",
        memory_text=original,
        backend_text=original,
    )

    service._index_l3_event(
        {"id": "backend-1", "event": "UPDATE", "memory": original},
        user_id="user-1",
        source_refs=source_refs,
        l3_metadata={
            "source_type": "chat_round",
            "data_classification": "normal",
            "memory_type": "profile",
        },
    )

    assert backend.get("backend-1")["memory"] == original
    indexed = repository.get_active_memory_by_backend_id("user-1", LONG_TERM_SCOPE, "backend-1")
    assert indexed is not None
    assert indexed.memory_text == original


# ---------------------------------------------------------------------------
# 后端写失败的记账
# ---------------------------------------------------------------------------


def test_a_backend_write_failure_without_an_index_log_is_only_logged(
    log_sink: list[dict[str, Any]],
) -> None:
    """回滚写失败时没有 index_log：记顶层日志，不抛（本地索引已经落盘）。"""
    service = _service()

    def _boom() -> None:
        raise RuntimeError("backend down")

    service._run_backend_writes([_boom])

    warnings = [r for r in log_sink if r["message"] == "memory backend write after index failed"]
    assert len(warnings) == 1
    assert warnings[0]["error_type"] == "RuntimeError"


# ---------------------------------------------------------------------------
# 冲突仲裁
# ---------------------------------------------------------------------------


class _NullSupersedeRepository(InMemoryMemoryRepository):
    """标 SUPERSEDED 返回 None：这条行已经不在库里了（并发被别人改掉）。"""

    def mark_memory_superseded(self, memory_id: str) -> Any | None:
        return None


def test_a_memory_is_superseded_when_the_repository_accepts_the_transition() -> None:
    """对照组：仓储接受状态迁移时，同槽位的旧记忆确实被标 SUPERSEDED。"""
    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())
    _add_memory(repository, backend_memory_id="backend-old")

    superseded_ids, _pending = service._supersede_conflicting_memories(
        user_id="user-1",
        memory_scope_id=LONG_TERM_SCOPE,
        backend_memory_id="backend-new",
        memory_text="User lives in Hangzhou",
    )

    assert len(superseded_ids) == 1


def test_a_memory_that_cannot_be_superseded_is_not_counted_as_superseded() -> None:
    """状态迁移失败（返回 None）时不能算作已 supersede：旧记忆还活着。"""
    repository = _NullSupersedeRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())
    _add_memory(repository, backend_memory_id="backend-old")

    superseded_ids, pending = service._supersede_conflicting_memories(
        user_id="user-1",
        memory_scope_id=LONG_TERM_SCOPE,
        backend_memory_id="backend-new",
        memory_text="User lives in Hangzhou",
    )

    assert superseded_ids == []
    assert pending == []


def test_a_memory_is_not_compared_against_its_own_backend_id() -> None:
    """同一条 backend 记忆不跟自己比新旧：否则它会把自己判成「更旧」被丢弃。"""
    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())
    _add_memory(repository, backend_memory_id="backend-1", round_id="round-9")

    is_older = service._is_older_than_active_conflicting_memory(
        user_id="user-1",
        memory_scope_id=LONG_TERM_SCOPE,
        backend_memory_id="backend-1",
        memory_text=LOCATION_TEXT,
        l3_metadata={"source_refs": [{"session_id": "s1", "round_id": "round-1"}]},
    )

    assert is_older is False


def test_a_newer_memory_in_the_same_slot_makes_the_incoming_one_older() -> None:
    """对照组：另一条 backend id 的同槽位更新记忆，确实能判出「更旧」。"""
    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())
    _add_memory(repository, backend_memory_id="backend-old", round_id="round-9")

    is_older = service._is_older_than_active_conflicting_memory(
        user_id="user-1",
        memory_scope_id=LONG_TERM_SCOPE,
        backend_memory_id="backend-new",
        memory_text=LOCATION_TEXT,
        l3_metadata={"source_refs": [{"session_id": "s1", "round_id": "round-1"}]},
    )

    assert is_older is True


def test_only_superseded_memories_in_the_same_slot_are_cleaned_up() -> None:
    """删一条记忆时，同槽位里只有 SUPERSEDED 的旧行跟着下线；活跃行不能被误删。"""
    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())
    active = _add_memory(
        repository,
        backend_memory_id="local-p0:current_location:active",
        memory_text=LOCATION_TEXT,
    )
    superseded = _add_memory(
        repository,
        backend_memory_id="local-p0:current_location:old",
        memory_text="User lives in Hangzhou",
    )
    repository.mark_memory_superseded(superseded.memory_id)
    target = _add_memory(
        repository,
        backend_memory_id="local-p0:current_location:target",
        memory_text="User lives in Beijing",
    )

    service._delete_one_memory(
        DeleteMemoryRequest(
            request_id="req-1",
            user_id="user-1",
            scope=DeleteScope.MEMORY,
            operation_id="op-1",
            memory_id=target.memory_id,
        )
    )

    assert repository.get_memory_index(active.memory_id).memory_status is MemoryStatus.ACTIVE
    assert repository.get_memory_index(superseded.memory_id).memory_status is MemoryStatus.DELETED
    assert repository.get_memory_index(target.memory_id).memory_status is MemoryStatus.DELETED


# ---------------------------------------------------------------------------
# metadata 归一
# ---------------------------------------------------------------------------


def test_a_string_backend_category_becomes_a_single_element_list() -> None:
    """metadata 里的字符串分类要包成列表：下游一律按 list 消费。"""
    service = _service()

    metadata = service._l3_metadata([], {"backend_categories": "food"})

    assert metadata["backend_categories"] == ["food"]


# ---------------------------------------------------------------------------
# 重建
# ---------------------------------------------------------------------------


def test_a_deleted_round_counts_as_already_covered() -> None:
    """这一轮已被删除：重建时算已覆盖，不能再抽一遍（否则会复活被删的事实）。"""
    service = _service()
    entry = JournalEntry(
        journal_id="r1",
        user_id="user-1",
        memory_scope_id="s1",
        session_id="s1",
        round_id="r1",
        messages=[],
        source_timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        round_fingerprint="fp",
    )

    covered = service._rebuild_round_is_l3_covered(
        entry,
        active_memories=[],
        deleted_refs={("s1", "r1")},
        processed_refs=set(),
    )

    assert covered is True


def test_a_rebuild_retry_only_cleans_up_the_requested_session() -> None:
    """重试清理只认本次会话的行：别的会话不能被顺带清掉。"""
    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())
    mine = _add_memory(
        repository,
        backend_memory_id="backend-mine",
        round_id="r1",
        metadata={"rebuild_cleanup_operation_id": "op-1"},
    )
    other = _add_memory(
        repository,
        backend_memory_id="backend-other",
        session_id="s2",
        round_id="r2",
        metadata={"rebuild_cleanup_operation_id": "op-1"},
    )
    repository.mark_memory_superseded(mine.memory_id)
    repository.mark_memory_superseded(other.memory_id)

    candidates = service._rebuild_cleanup_candidates(
        RebuildMemoryRequest(
            request_id="req-1",
            user_id="user-1",
            operation_id="op-1",
            session_id="s1",
        ),
        retry_failed_task=True,
    )

    assert [memory.memory_id for memory in candidates] == [mine.memory_id]
