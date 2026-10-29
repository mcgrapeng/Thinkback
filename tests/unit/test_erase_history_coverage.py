"""物理擦除必须覆盖「本地没有索引行」的引擎记录（BUG-1）。

擦除 ALL 范围时，后端删掉的是该作用域的**全部**记录，包括抽取后索引失败
留下的「暗数据」。mem0 的 delete 会把事实原文写进 history 表，所以暗数据的
原文也要按它��记忆 ID 清掉 —— 只清本地索引里有的那批，暗数据的原文就永久留在
引擎侧，而审计却写「物理擦除已完成」。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from thinkback.memory.backends import Mem0LibraryMemoryBackend
from thinkback.memory.repositories import InMemoryMemoryRepository
from thinkback.memory.schemas import (
    AppendMemoryRequest,
    DeleteMemoryRequest,
    DeleteScope,
)
from thinkback.memory.service import MemoryService


class _HistoryMem0Client:
    """仿 mem0 2.2.1 的最小 client：delete 会把原文写进 history 表。"""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.rows: dict[str, dict[str, Any]] = {}
        self._counter = 0

    def _write_history(self, memory_id: str, old: str | None, new: str | None, event: str) -> None:
        connection = sqlite3.connect(self.db_path)
        connection.execute(
            "INSERT INTO history (id, memory_id, old_memory, new_memory, event)"
            " VALUES (?, ?, ?, ?, ?)",
            (f"h-{memory_id}-{event}-{self._counter}", memory_id, old, new, event),
        )
        self._counter += 1
        connection.commit()
        connection.close()

    def add(self, messages: list[dict[str, str]], **kwargs: Any) -> dict[str, Any]:
        _ = kwargs
        memory_id = f"m-{len(self.rows) + 1}"
        text = messages[0]["content"]
        self.rows[memory_id] = {"id": memory_id, "memory": text, "metadata": {}}
        self._write_history(memory_id, None, text, "ADD")
        return {"results": [{"id": memory_id, "memory": text, "event": "ADD"}]}

    def search(self, query: str, **kwargs: Any) -> dict[str, Any]:
        _ = (query, kwargs)
        return {"results": list(self.rows.values())}

    def get_all(self, **kwargs: Any) -> dict[str, Any]:
        _ = kwargs
        return {"results": list(self.rows.values())}

    def update(self, memory_id: str, text: str | None = None, **kwargs: Any) -> None:
        _ = (text, kwargs)
        self.rows[memory_id]["memory"] = text or self.rows[memory_id]["memory"]

    def delete(self, memory_id: str) -> None:
        row = self.rows.pop(memory_id, None)
        if row is None:
            raise ValueError(f"Memory with id {memory_id} not found")
        self._write_history(memory_id, row["memory"], None, "DELETE")


def _append(round_id: str, text: str) -> AppendMemoryRequest:
    now = datetime(2026, 10, 29, 10, 0, 0, tzinfo=UTC)
    return AppendMemoryRequest(
        request_id=f"req-{round_id}",
        user_id="user-1",
        session_id="session-1",
        round_id=round_id,
        messages=[
            {
                "message_id": f"{round_id}-u",
                "role": "user",
                "content": text,
                "timestamp": now.isoformat(),
            }
        ],
        source_timestamp=now,
        metadata={"force_extract": True},
    )


def test_erase_all_purges_history_rows_of_records_without_local_index(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "history.db"
    connection = sqlite3.connect(db_path)
    connection.execute(
        "CREATE TABLE history (id TEXT PRIMARY KEY, memory_id TEXT, old_memory TEXT,"
        " new_memory TEXT, event TEXT)"
    )
    connection.execute(
        "CREATE TABLE messages (id TEXT PRIMARY KEY, session_scope TEXT, role TEXT,"
        " content TEXT, name TEXT, created_at DATETIME)"
    )
    connection.commit()
    connection.close()

    client = _HistoryMem0Client(db_path)
    backend = Mem0LibraryMemoryBackend(
        config={"history_db_path": str(db_path)}, memory_client=client
    )
    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=backend, l3_write_mode="sync")

    service.append(_append("r1", "今天会议讨论了排产计划"))
    # 暗数据：后端有、本地没有索引行（抽取后索引失败）
    client.rows["dark-1"] = {"id": "dark-1", "memory": "暗数据里的秘密事实", "metadata": {}}
    client._write_history("dark-1", None, "暗数据里的秘密事实", "ADD")

    service.erase(
        DeleteMemoryRequest(
            request_id="d-1",
            user_id="user-1",
            scope=DeleteScope.ALL,
            operation_id="op-all",
        )
    )

    remaining = sqlite3.connect(db_path).execute("SELECT memory_id, event FROM history").fetchall()
    assert remaining == [], f"引擎侧历史仍有原文：{remaining}"


def test_erase_all_audit_reports_the_actual_history_row_count(tmp_path: Path) -> None:
    """审计里的「清了几行」要是真数，不能是本地索引那批的数。"""
    db_path = tmp_path / "history.db"
    connection = sqlite3.connect(db_path)
    connection.execute(
        "CREATE TABLE history (id TEXT PRIMARY KEY, memory_id TEXT, old_memory TEXT,"
        " new_memory TEXT, event TEXT)"
    )
    connection.execute(
        "CREATE TABLE messages (id TEXT PRIMARY KEY, session_scope TEXT, role TEXT,"
        " content TEXT, name TEXT, created_at DATETIME)"
    )
    connection.commit()
    connection.close()

    client = _HistoryMem0Client(db_path)
    backend = Mem0LibraryMemoryBackend(
        config={"history_db_path": str(db_path)}, memory_client=client
    )
    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=backend, l3_write_mode="sync")

    service.append(_append("r1", "今天会议讨论了排产计划"))
    client.rows["dark-1"] = {"id": "dark-1", "memory": "暗数据里的秘密事实", "metadata": {}}
    client._write_history("dark-1", None, "暗数据里的秘密事实", "ADD")

    service.erase(
        DeleteMemoryRequest(
            request_id="d-1",
            user_id="user-1",
            scope=DeleteScope.ALL,
            operation_id="op-all",
        )
    )

    audit = repository.admin_audit[-1]
    assert audit.detail["mem0_history_rows_deleted"] == 4, audit.detail


def test_erase_after_suppress_still_purges_the_engine_side_raw_message(
    tmp_path: Path,
) -> None:
    """先 suppress（逻辑下架）再 erase（物理擦除），引擎侧的原始消息必须被清掉。

    suppress 把轮次标成 deleted_tombstone，之后 list_rounds 就看不到它们 ——
    擦除计划按「活着的轮次」取原文，取到空列表，``purge_history`` 拿着
    ``message_texts=[]`` 去删，mem0 消息表里的对话原文留着，而接口回
    ``status="completed"``。对外承诺的「物理擦除」在这一档是假的。
    """
    db_path = tmp_path / "history.db"
    connection = sqlite3.connect(db_path)
    connection.execute(
        "CREATE TABLE history (id TEXT PRIMARY KEY, memory_id TEXT, old_memory TEXT,"
        " new_memory TEXT, event TEXT)"
    )
    connection.execute(
        "CREATE TABLE messages (id TEXT PRIMARY KEY, session_scope TEXT, role TEXT,"
        " content TEXT, name TEXT, created_at DATETIME)"
    )
    connection.commit()
    connection.close()

    client = _HistoryMem0Client(db_path)
    backend = Mem0LibraryMemoryBackend(
        config={"history_db_path": str(db_path)}, memory_client=client
    )
    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=backend, l3_write_mode="sync")

    service.append(_append("r1", "客户要求下季度良率提升到 95%"))

    # mem0 的 messages 表：原始对话正文（引擎侧保存最近 10 条）
    scope_key = Mem0LibraryMemoryBackend._mem0_session_scope("user-1", "thinkback")
    connection = sqlite3.connect(db_path)
    connection.execute(
        "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?)",
        ("msg-1", scope_key, "user", "客户要求下季度良率提升到 95%", None, "2026-10-29"),
    )
    connection.commit()
    connection.close()

    service.delete(
        DeleteMemoryRequest(
            request_id="d-1",
            user_id="user-1",
            scope=DeleteScope.SESSION,
            session_id="session-1",
            operation_id="op-suppress",
        )
    )
    service.erase(
        DeleteMemoryRequest(
            request_id="e-1",
            user_id="user-1",
            scope=DeleteScope.SESSION,
            session_id="session-1",
            operation_id="op-erase",
        )
    )

    remaining = sqlite3.connect(db_path).execute("SELECT content FROM messages").fetchall()
    assert remaining == [], f"引擎侧原始消息仍在：{remaining}"


def test_erase_succeeds_when_backend_has_more_records_than_one_page() -> None:
    """后端记录超过一页时，单条擦除不能被判成「无法证明已删」而永久失败。

    复核原来只看一页：列表取满就判定「目标还在」→ 擦除任务失败，重试又撞
    同一条件 → 重用户永远删不掉。改成逐个点查目标 ID。
    """
    from thinkback.domain.ports import MEMORY_LIST_PAGE_LIMIT
    from thinkback.memory.backends import FakeMemoryBackend

    backend = FakeMemoryBackend()
    # 先塞满一页：被删的那条排在列表之外（第一页里看不到它）
    for index in range(MEMORY_LIST_PAGE_LIMIT):
        memory_id = f"fake-{index:06d}"
        backend.memories[memory_id] = {
            "id": memory_id,
            "memory": f"第 {index} 条记录",
            "event": "ADD",
            "user_id": "user-1",
            "agent_id": "thinkback",
            "metadata": {},
            "score": 1.0,
        }

    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=backend, l3_write_mode="sync")

    service.append(_append("r1", "今天会议讨论了排产计划"))
    indexed = repository.list_memories("user-1", "thinkback")
    assert indexed, "append 后应当有一条本地索引行"
    memory_id = indexed[0].memory_id
    target_backend_id = indexed[0].backend_memory_id

    response = service.delete(
        DeleteMemoryRequest(
            request_id="d-1",
            user_id="user-1",
            scope=DeleteScope.MEMORY,
            memory_id=memory_id,
            operation_id="op-one",
        )
    )

    assert response.status == "completed", response
    assert target_backend_id not in backend.memories
