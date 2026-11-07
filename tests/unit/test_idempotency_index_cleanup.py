"""幂等缓存的**二级索引**清理：过期与淘汰都要连带清掉索引条目。

``IdempotencyCache`` 有两份状态：

- ``_store``：``(scope, key, method, path, body_hash) -> 响应``（有上限，LRU 淘汰）
- ``_index``：``(scope, key, method, path) -> {body_hash, ...}``（**没有上限**）

`_index` 是冲突检测用的二级索引：同一把 key 换了 body 会 409。
它靠 `_store` 的删除来同步清理 —— 索引不跟着清的话，它会无限增长，
而键对应的响应早就没了。

**为什么这类缺口语句覆盖看不见**：现有那条用例
（``test_evicting_the_oldest_entry_also_clears_its_conflict_index``）
淘汰的是**另一把 key**（``k1`` 被淘汰时，索引里
``(scope, k1, POST, path)`` 根本不存在 → ``hashes is None``），
所以它走的是「索引里没有这条」那条分支。

真正的场景是**同一把 key 下有多个 body_hash**（先记 hash-1，再用 hash-2
撞 409 —— 这正是幂等冲突检测要拦的事）：此时索引条目存在，
清掉其中一个 body_hash 时必须**同时从索引集合里移除它**，且
集合清空后要把整个索引条目删掉。

这两条分支（``hashes is not None`` / ``not hashes``）此前从未被走到 ——
语句覆盖率 100% 也一样，因为它们都在函数体内。
"""

from __future__ import annotations

import time

import pytest

from thinkback.api import idempotency as idempotency_module
from thinkback.api.idempotency import CACHE_TTL, IdempotencyCache


def _response(timestamp: float | None = None):  # type: ignore[no-untyped-def]
    from thinkback.api.idempotency import CachedResponse

    return CachedResponse(
        body=b"{}",
        status_code=200,
        content_type="application/json",
        headers={},
        timestamp=time.time() if timestamp is None else timestamp,
    )


def test_pruning_one_body_of_a_shared_key_also_removes_it_from_the_index() -> None:
    """同一把 key 下的某个 body 过期时，索引集合里也要移除它。

    场景：先记 hash-1，再用 hash-2 撞 409（幂等冲突检测）。
    索引里 ``{hash-1, hash-2}`` —— 两条响应都还在缓存里。
    hash-1 过期后：``_store`` 少一条，索引也必须少一个 hash，
    否则「过期后换 body 永远 409」这个 bug 会回来。
    """
    cache = IdempotencyCache()
    now = time.time()
    cache.put("scope", "k1", "POST", "/v2/memory/a", "hash-1", _response(now))
    cache.put("scope", "k1", "POST", "/v2/memory/a", "hash-2", _response(now))
    assert cache._index[("scope", "k1", "POST", "/v2/memory/a")] == {"hash-1", "hash-2"}

    # 只让 hash-1 过期
    stale_key = ("scope", "k1", "POST", "/v2/memory/a", "hash-1")
    cache._store[stale_key] = _response(now - CACHE_TTL - 1)

    assert cache.clear_expired() == 1, "过期的那条没被清掉"

    index = cache._index.get(("scope", "k1", "POST", "/v2/memory/a"))
    assert index == {"hash-2"}, f"索引里还留着已过期的 body_hash：{index}"


def test_pruning_the_last_body_of_a_key_removes_the_whole_index_entry() -> None:
    """一把 key 下最后一条 body 过期后，索引条目整个删掉。

    索引条目留着空集合也没用，但它是内存泄漏 —— 每把 key 留一个空 set，
    缓存上限管住了 ``_store`` 却管不住 ``_index``。
    """
    cache = IdempotencyCache()
    now = time.time()
    cache.put("scope", "k1", "POST", "/v2/memory/a", "hash-1", _response(now))

    stale_key = ("scope", "k1", "POST", "/v2/memory/a", "hash-1")
    cache._store[stale_key] = _response(now - CACHE_TTL - 1)

    assert cache.clear_expired() == 1
    assert ("scope", "k1", "POST", "/v2/memory/a") not in cache._index


def test_evicting_one_body_of_a_shared_key_also_removes_it_from_the_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LRU 淘汰同 key 下的某个 body 时，索引也要跟着清。

    与过期清理是**两条不同的路径**（``_prune_expired`` vs ``put`` 里的
    ``popitem``），各自维护索引。现有那条淘汰用例只覆盖了「淘汰的是
    另一把 key」，所以这条路径的索引清理从未被验证。
    """
    monkeypatch.setattr(idempotency_module, "MAX_CACHE_SIZE", 2)
    cache = IdempotencyCache()
    now = time.time()
    # 同一把 key，两个 body —— 索引里是 {hash-1, hash-2}
    cache.put("scope", "k1", "POST", "/v2/memory/a", "hash-1", _response(now))
    cache.put("scope", "k1", "POST", "/v2/memory/a", "hash-2", _response(now))
    assert cache._index[("scope", "k1", "POST", "/v2/memory/a")] == {"hash-1", "hash-2"}

    # 再放一条别的 key，触发 LRU 淘汰最老的 —— 那正是 hash-1
    cache.put("scope", "k2", "POST", "/v2/memory/a", "hash-3", _response(now))

    index = cache._index.get(("scope", "k1", "POST", "/v2/memory/a"))
    assert index == {"hash-2"}, f"被淘汰的 body_hash 还留在索引里：{index}"
    assert cache.get("scope", "k1", "POST", "/v2/memory/a", "hash-1") is None


def test_the_index_stays_bounded_when_bodies_are_evicted() -> None:
    """索引条目数不超过「还活着的 key 数」—— 不随淘汰无限增长。

    这是那条不变式：``_store`` 有上限，``_index`` 没有，
    所以索引必须**完全**跟着 ``_store`` 走，否则它就是泄漏点。
    """
    monkey = None  # 读起来更清楚：这里不用 monkeypatch
    del monkey
    cache = IdempotencyCache()
    now = time.time()
    for index in range(50):
        cache.put("scope", f"k{index}", "POST", "/v2/memory/a", "hash", _response(now))

    # 50 把 key 都在索引里
    assert len(cache._index) == 50
    # 把它们全部清掉（过期）
    for key in list(cache._store):
        cache._store[key] = _response(now - CACHE_TTL - 1)
    cache.clear_expired()

    assert cache._index == {}, "索引没跟着清空 —— 它会无限增长"
