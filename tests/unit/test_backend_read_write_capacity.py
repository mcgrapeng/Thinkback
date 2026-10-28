"""后端读写并发预算必须分开。

写路径要打 LLM 抽取（慢），读路径只要 embedding + 向量检索（快）。共用
一把信号量时，写积压会把读饿死：实测 4 路并发写入时 mem0 add 耗时 76s，
排在后面的 search 等了 69s，召回直接 503 —— 召回在助手的关键路径上，
不能被后台写入队列拖死。
"""

from __future__ import annotations

import threading
from typing import Any

from thinkback.memory.backends import Mem0LibraryMemoryBackend


class _BlockingAddClient:
    """add 会一直阻塞，模拟慢速 LLM 抽取占满写预算。"""

    def __init__(self) -> None:
        self.add_started = threading.Event()
        self.release_add = threading.Event()
        self.search_calls: list[str] = []

    def add(self, messages: list[dict[str, str]], **kwargs: Any) -> dict[str, Any]:
        _ = (messages, kwargs)
        self.add_started.set()
        self.release_add.wait(timeout=10)
        return {"results": []}

    def search(self, query: str, **kwargs: Any) -> dict[str, Any]:
        _ = (query, kwargs)
        self.search_calls.append("search")
        return {"results": []}


def test_read_is_not_blocked_by_slow_writes() -> None:
    """写被写满时，search 仍要能立刻完成。"""
    client = _BlockingAddClient()
    backend = Mem0LibraryMemoryBackend(
        config={"history_db_path": ".mem0/history.db"},
        memory_client=client,
        max_concurrent_calls=1,
    )

    writer = threading.Thread(
        target=lambda: backend.add(
            [{"role": "user", "content": "hi"}],
            user_id="user-1",
            memory_scope_id="thinkback",
        ),
        daemon=True,
    )
    writer.start()
    assert client.add_started.wait(timeout=5), "add 未启动"

    # 写预算（1）已被占满；读必须用自己的额度。
    search_done = threading.Event()

    def _search() -> None:
        backend.search("猫", user_id="user-1", memory_scope_id="thinkback", limit=5)
        search_done.set()

    reader = threading.Thread(target=_search, daemon=True)
    reader.start()

    assert search_done.wait(timeout=5), "search 被写路径堵住了：读应有自己的并发额度"

    client.release_add.set()
    writer.join(timeout=5)
    reader.join(timeout=5)
    assert client.search_calls == ["search"]
