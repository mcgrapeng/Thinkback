"""Mem0 适配器的**分页批量删除**与若干未覆盖的降级路径。

``delete_all`` 不是调 mem0 的 ``delete_all()``（那会连带删掉别的作用域），
而是**分页取 id、逐个删**：

```
while 还有页:
    get_all(limit=page_limit) → 一批 id
    delete_many(那一批)
    如果这批不满页 → 说明取完了，退出
```

**分页循环的两侧此前从未被验证**：

- ``if len(memory_ids) < page_limit: break`` —— 最后一页不满时退出。
  少了这条，删除会多跑一轮空查询（慢），而且 ``max_pages`` 上限会被
  默默消耗掉。
- 循环自然结束（``for _ in range(max_pages)`` 走完）—— 满页时继续取下一页。

还有一条更要紧的：**``max_pages=100`` 的安全上限**。
数据量超过 100 万条时循环退出，**剩下的没删干净** —— 这是「删除用户全部数据」
的残留风险，而现有用例只删了一页。

本文件用假客户端覆盖这几条路径。
"""

from __future__ import annotations

from typing import Any

import pytest

from thinkback.domain.ports import MEMORY_LIST_PAGE_LIMIT
from thinkback.memory.backends import Mem0LibraryMemoryBackend


class _PagingClient:
    """按页返回 id 的假 mem0 客户端。"""

    def __init__(self, *, pages: list[list[str]]) -> None:
        self.pages = pages
        self.calls: list[tuple[str, tuple, dict]] = []
        self.deleted: list[str] = []

    def get_all(self, *, filters: dict, limit: int, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get_all", (), {"filters": filters, "limit": limit}))
        index = sum(1 for call in self.calls if call[0] == "get_all") - 1
        if index < len(self.pages):
            return {"results": [{"id": memory_id} for memory_id in self.pages[index]]}
        return {"results": []}

    def delete(self, memory_id: str) -> dict[str, Any]:
        self.calls.append(("delete", (memory_id,), {}))
        self.deleted.append(memory_id)
        return {"message": "deleted"}

    def delete_all(self, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("library backend must not call mem0 delete_all() directly")


def _backend(client: _PagingClient) -> Mem0LibraryMemoryBackend:
    return Mem0LibraryMemoryBackend(
        config={"history_db_path": ".mem0/history.db"},
        memory_client=client,  # type: ignore[arg-type]
    )


def test_a_single_short_page_ends_the_paging_loop() -> None:
    """一批不满页 → 删完就停（不再多跑一轮空查询）。"""
    client = _PagingClient(pages=[["m1", "m2"]])
    backend = _backend(client)

    deleted = backend.delete_all(user_id="user-1", memory_scope_id="thinkback")

    assert deleted == ["m1", "m2"]
    get_all_calls = [call for call in client.calls if call[0] == "get_all"]
    assert len(get_all_calls) == 1, f"不满页却多跑了查询：{len(get_all_calls)} 次"


def test_a_full_page_continues_to_the_next_page() -> None:
    """一批正好满页 → 继续取下一页（数据比一页多的情况）。"""
    full_page = [f"m{index}" for index in range(MEMORY_LIST_PAGE_LIMIT)]
    client = _PagingClient(pages=[full_page, ["m-last"]])
    backend = _backend(client)

    deleted = backend.delete_all(user_id="user-1", memory_scope_id="thinkback")

    assert len(deleted) == MEMORY_LIST_PAGE_LIMIT + 1
    assert deleted[-1] == "m-last"
    get_all_calls = [call for call in client.calls if call[0] == "get_all"]
    assert len(get_all_calls) == 2, f"满页后没有继续取下一页：{len(get_all_calls)} 次"


def test_multiple_full_pages_are_all_deleted() -> None:
    """连续多页满页 → 全部删掉（不只是第一页）。"""
    page1 = [f"a{index}" for index in range(MEMORY_LIST_PAGE_LIMIT)]
    page2 = [f"b{index}" for index in range(MEMORY_LIST_PAGE_LIMIT)]
    client = _PagingClient(pages=[page1, page2, ["c-tail"]])
    backend = _backend(client)

    deleted = backend.delete_all(user_id="user-1", memory_scope_id="thinkback")

    assert len(deleted) == MEMORY_LIST_PAGE_LIMIT * 2 + 1
    assert len(set(deleted)) == len(deleted), "同一条被删了两次"
    assert deleted[-1] == "c-tail"


def test_an_empty_first_page_deletes_nothing() -> None:
    """第一页就空 → 什么都没删，也不该报错。"""
    client = _PagingClient(pages=[[]])
    backend = _backend(client)

    assert backend.delete_all(user_id="user-1", memory_scope_id="thinkback") == []


def test_the_paging_loop_is_bounded_by_the_max_pages_safety_limit() -> None:
    """数据量超过安全上限时，循环**停下**（而不是无限取页）。

    ``max_pages=100`` × 每页 1000 条 = 最多 10 万条。超过这个量，
    剩下的**不会被删** —— 这是「删除用户全部数据」的已知残留边界
    （``delete_all`` 的 docstring 里写了）。

    这条用例把这个边界钉住：上限是 100 页，不是无限。
    """
    full_page = [f"m{index}" for index in range(MEMORY_LIST_PAGE_LIMIT)]
    # 每一页都满 → 永远取不满，循环会跑到 max_pages 上限
    client = _PagingClient(pages=[full_page] * 200)
    backend = _backend(client)

    deleted = backend.delete_all(user_id="user-1", memory_scope_id="thinkback")

    get_all_calls = [call for call in client.calls if call[0] == "get_all"]
    assert len(get_all_calls) == 100, f"分页循环没有停在安全上限（跑了 {len(get_all_calls)} 页）"
    # 上限之外的数据确实没删 —— 这是已知边界，不是 bug
    assert len(deleted) == MEMORY_LIST_PAGE_LIMIT * 100


def test_the_get_all_limit_is_capped_at_the_page_limit() -> None:
    """``get_all`` 的 limit 不能超过一页的上限（防一次拉爆内存）。

    ``get_all`` 是擦除复核与 ALL 范围删除的取数口；它可能被调用方传一个
    很大的 limit，而引擎一次拉那么多会把内存打满。
    """
    client = _PagingClient(pages=[[]])
    backend = _backend(client)
    seen: list[int] = []

    def _get_all(*, filters: dict, limit: int, **kw: Any) -> dict:
        seen.append(limit)
        return {"results": []}

    client.get_all = _get_all  # type: ignore[method-assign]

    backend.get_all(user_id="user-1", memory_scope_id="thinkback", limit=10_000)

    assert seen, "get_all 没被调用"
    assert seen[0] == MEMORY_LIST_PAGE_LIMIT, (
        f"get_all 的 limit 没被压到一页上限：{seen[0]} != {MEMORY_LIST_PAGE_LIMIT}"
    )


@pytest.mark.parametrize("page_size", [0, 1, MEMORY_LIST_PAGE_LIMIT])
def test_any_page_size_is_deleted_completely(page_size: int) -> None:
    """各种页大小都要删干净（分页边界不能漏）。"""
    ids = [f"m{index}" for index in range(page_size)]
    client = _PagingClient(pages=[ids] if ids else [[]])
    backend = _backend(client)

    deleted = backend.delete_all(user_id="user-1", memory_scope_id="thinkback")

    assert deleted == ids
