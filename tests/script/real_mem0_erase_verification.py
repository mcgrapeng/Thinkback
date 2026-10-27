"""真实栈上的 erase 契约验证：Milvus + Mem0 本地历史 + PostgreSQL。

为什么需要它：erase 的断言如果只在 fake 后端上跑，就没验证到「引擎侧残留」这
一层 —— Milvus 的向量行、Mem0 本地 SQLite 的事实抽取历史（old_memory /
new_memory 存事实原文）与原始消息（messages 表存对话原文）都可能留残骸。
fake 后端没有这些面，测不到。

跑法（需要真实 Milvus + 可达的 LLM/embedding endpoint + 可写的 PG）：

    docker compose -f deploy/milvus-local/docker-compose.yml up -d
    MILVUS_URL=http://localhost:19540 \\
    MEM0_HISTORY_DB_PATH=.mem0/verify-history.db \\
      uv run --group dev python tests/script/real_mem0_erase_verification.py

退出码 0 = 全部通过；非 0 = 有断言失败（逐条打印 PASS/FAIL）。
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv

from thinkback.api.dependencies import get_memory_backend
from thinkback.infra.config import Settings
from thinkback.infra.milvus import ensure_milvus_database
from thinkback.memory.repositories import SqlAlchemyMemoryRepository
from thinkback.memory.schemas import (
    AppendMemoryRequest,
    DeleteMemoryRequest,
    DeleteScope,
    MemoryMessage,
    MessageRole,
)
from thinkback.memory.service import MemoryService

PASSED: list[str] = []
FAILED: list[str] = []

# L3 作用域 = 请求里的 namespace（不是 Milvus collection 名）
NAMESPACE = "thinkback"


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  PASS {name}")
    else:
        FAILED.append(f"{name}: {detail}")
        print(f"  FAIL {name} -> {detail}")


def history_rows(history_db_path: str, table: str) -> list[tuple]:
    """直接读 Mem0 本地 SQLite（引擎侧残留的真值）。"""
    path = Path(history_db_path)
    if not path.is_file():
        return []
    connection = sqlite3.connect(path)
    try:
        return connection.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608
    except sqlite3.OperationalError:
        return []
    finally:
        connection.close()


def milvus_rows(settings: Settings, user_id: str) -> list[dict]:
    """直接查 Milvus（向量侧残留的真值），不走 mem0 的过滤。

    mem0 的 Milvus schema 把业务字段放在 JSON 字段 ``metadata`` 里
    （id / vectors / metadata / text / sparse），所以过滤要写
    ``metadata["user_id"] == "..."``。查询用强一致，避免刚写完的向量在
    bounded 一致性窗口内查不到（会把「写入后有向量行」误判成失败）。
    """
    from pymilvus import MilvusClient

    client = MilvusClient(
        uri=settings.milvus_url,
        token=settings.milvus_token or "",
        db_name=settings.milvus_database,
    )
    try:
        client.load_collection(settings.memory_milvus_collection)
        return list(
            client.query(
                collection_name=settings.memory_milvus_collection,
                filter=f'metadata["user_id"] == "{user_id}"',
                output_fields=["id", "metadata", "text"],
                limit=1000,
                consistency_level="Strong",
            )
        )
    finally:
        client.close()


def main() -> int:
    load_dotenv()
    # 本机装了个人 SOCKS 代理（HTTP_PROXY / ALL_PROXY）：会把对内网网关的请求
    # 劫持走，SOCKS 还会因为缺 socksio 直接构造失败。生产 pod 没有这些变量。
    for proxy_var in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        os.environ.pop(proxy_var, None)
    settings = Settings()
    history_db_path = settings.mem0_history_db_path
    run_id = uuid4().hex[:10]
    user_id = f"erase-verify-{run_id}"
    session_id = f"erase-verify-session-{run_id}"
    round_id = f"erase-verify-round-{run_id}"
    secret = f"夜莺计划-{run_id}"

    print(f"milvus={settings.milvus_url} db={settings.milvus_database}")
    print(f"collection={settings.memory_milvus_collection} history_db={history_db_path}")
    print(f"user={user_id}")

    repository = SqlAlchemyMemoryRepository()
    # 服务启动时也会做这一步（lazy-create）：这里显式调用，让脚本能对着
    # 全新 Milvus 实例直接跑。
    ensure_milvus_database(
        milvus_url=settings.milvus_url,
        milvus_database=settings.milvus_database,
        milvus_token=settings.milvus_token or None,
    )
    backend = get_memory_backend(settings)
    service = MemoryService(
        repository=repository,
        backend=backend,
        mem0_infer_facts=settings.memory_infer_facts,
        l3_write_mode="sync",
    )

    try:
        # ── 写入 ──────────────────────────────────────────────────────────
        service.append(
            AppendMemoryRequest(
                request_id=f"req-{round_id}",
                user_id=user_id,
                session_id=session_id,
                round_id=round_id,
                messages=[
                    MemoryMessage(
                        message_id=f"{round_id}-u",
                        role=MessageRole.USER,
                        content=f"我负责的项目代号是{secret}，交付时间是下个季度。",
                        timestamp="2026-10-27T10:00:00Z",
                    ),
                    MemoryMessage(
                        message_id=f"{round_id}-a",
                        role=MessageRole.ASSISTANT,
                        content="好的，我记下了。",
                        timestamp="2026-10-27T10:00:01Z",
                    ),
                ],
                source_timestamp="2026-10-27T10:00:01Z",
            )
        )

        memories = [
            memory
            for memory in repository.list_memories(user_id, NAMESPACE)
            if memory.memory_status.value == "ACTIVE"
        ]
        check("写入后 PG 有索引行", bool(memories), "ins_memory 无记录")
        if not memories:
            return 1
        backend_ids = [memory.backend_memory_id for memory in memories]
        check("索引行带后端记忆 ID", all(backend_ids), f"backend_memory_id 为空：{backend_ids}")

        before_milvus = milvus_rows(settings, user_id)
        check(
            "写入后 Milvus 有向量行",
            any(secret in str(row.get("text", "")) for row in before_milvus),
            f"未找到含密文的向量行：{[row.get('id') for row in before_milvus]}",
        )

        before_history = history_rows(history_db_path, "history")
        check(
            "写入后 Mem0 有事实抽取历史",
            any(memory_id in str(row) for row in before_history for memory_id in backend_ids),
            f"history 无对应 memory_id 的行：{len(before_history)} 行",
        )
        before_messages = history_rows(history_db_path, "messages")
        check(
            "写入后 Mem0 有原始消息",
            any(secret in str(row) for row in before_messages),
            f"messages 无含密文的行：{len(before_messages)} 行",
        )

        # ── 擦除（整个会话：一轮可能抽出多条事实）─────────────────────────
        service.erase(
            DeleteMemoryRequest(
                request_id=f"req-erase-{round_id}",
                user_id=user_id,
                session_id=session_id,
                scope=DeleteScope.SESSION,
                operation_id=f"op-erase-{round_id}",
            )
        )

        # ── 擦除后的真值 ─────────────────────────────────────────────────
        after_memories = [
            memory
            for memory in repository.list_memories(user_id, NAMESPACE)
            if memory.memory_status.value == "ACTIVE"
        ]
        check("擦除后 PG 索引行消失", not after_memories, f"仍有 {after_memories}")
        check("擦除后 PG 轮次原文消失", repository.get_round(round_id) is None, "journal 仍有行")
        summary = repository.get_summary(user_id, session_id)
        check(
            "擦除后 PG 摘要正文清空",
            summary is None or not summary.summary_text,
            f"摘要仍有内容：{summary.summary_text if summary else None}",
        )

        after_milvus = milvus_rows(settings, user_id)
        check(
            "擦除后 Milvus 向量不可见",
            not any(secret in str(row.get("text", "")) for row in after_milvus),
            f"仍能查到含密文的向量行：{[row.get('id') for row in after_milvus]}",
        )
        check(
            "擦除后 Milvus 目标 ID 已删除",
            not any(row.get("id") in backend_ids for row in after_milvus),
            f"仍能查到被删的向量 ID：{backend_ids}",
        )

        after_history = history_rows(history_db_path, "history")
        check(
            "擦除后 Mem0 事实抽取历史清空",
            not any(memory_id in str(row) for row in after_history for memory_id in backend_ids),
            f"history 仍有对应 memory_id 的行：{after_history}",
        )
        check(
            "擦除后 Mem0 历史里查不到密文",
            not any(secret in str(row) for row in after_history),
            f"history 仍含密文：{after_history}",
        )

        after_messages = history_rows(history_db_path, "messages")
        check(
            "擦除后 Mem0 原始消息清空",
            not any(secret in str(row) for row in after_messages),
            f"messages 仍含密文：{after_messages}",
        )

        audits = repository.list_admin_audit()
        erase_audits = [audit for audit in audits if audit.action == "memory.erase"]
        check("擦除留下审计记录", bool(erase_audits), "无 memory.erase 审计")
        if erase_audits:
            detail = erase_audits[-1].detail
            check(
                "审计声明已清理引擎历史",
                bool(detail.get("mem0_history_purged")),
                f"审计 detail={detail}",
            )
            check(
                "审计不含原文",
                secret not in str(detail),
                f"审计里出现密文：{detail}",
            )
    finally:
        repository.close()

    print(f"\n通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    for failure in FAILED:
        print(f"  - {failure}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
