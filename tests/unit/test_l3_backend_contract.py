"""L3 后端契约：fake 与真实适配器跑**同一组**用例。

整套确定性测试（1100+ 条）都跑在 ``FakeMemoryBackend`` 上，生产跑在
``Mem0LibraryMemoryBackend`` 上。两套实现此前**从未跑过同一组用例** ——
所以「fake 是不是比生产宽松」这件事没人能回答：fake 宽松一点，一条针对
生产的用例就会在测试里绿、在生产红。

本文件把端口契约（``domain/ports.py`` 的 ``MemoryBackend``）逐条钉住，
两个实现都必须满足。任何一方偏离 → 红的正是那条偏离。

mem0 那一侧用一个按 **mem0 2.2.1 真实签名**写的替身（``filters`` 而非
顶层 scope、``top_k`` 而非 ``limit``、``get`` 缺失返回 ``None``、
``delete`` 缺失抛 ``ValueError``）。签名照抄 mem0 很重要：适配器靠反射
（``_scope_kwargs`` / ``_limit_kwargs`` / ``_show_expired_kwargs``）决定往
引擎传什么参数，替身签名不对，反射出来的参数就不对，契约就测不到真东西。
"""

from __future__ import annotations

from typing import Any

import pytest

from thinkback.memory.backends import FakeMemoryBackend, Mem0LibraryMemoryBackend


class _Mem0LikeClient:
    """按 mem0 2.2.1 的签名与错误语义工作的内存替身。"""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self._counter = 0

    # --- 签名照抄 mem0 2.2.1（Memory.add / search / get / get_all / update / delete）---

    def add(
        self,
        messages,
        *,
        user_id=None,
        agent_id=None,
        run_id=None,
        metadata=None,
        timestamp=None,
        expiration_date=None,
        infer=True,
        memory_type=None,
        prompt=None,
    ):  # type: ignore[no-untyped-def]
        _ = (run_id, timestamp, expiration_date, infer, memory_type, prompt)
        text = " ".join(
            str(message.get("content", "")).strip()
            for message in messages
            if str(message.get("content", "")).strip()
        )
        if not text:
            return {"results": []}
        self._counter += 1
        memory_id = f"mem0-{self._counter}"
        self.rows[memory_id] = {
            "id": memory_id,
            "memory": text,
            "event": "ADD",
            "user_id": user_id,
            "agent_id": agent_id,
            "metadata": dict(metadata or {}),
            "score": 1.0,
        }
        return {"results": [{"id": memory_id, "memory": text, "event": "ADD", "score": 1.0}]}

    def search(
        self,
        query: str,
        *,
        top_k: int = 20,
        filters=None,
        threshold: float = 0.1,
        rerank: bool = False,
        explain: bool = False,
        reference_date=None,
        show_expired: bool = False,
        **kwargs,
    ):  # type: ignore[no-untyped-def]
        _ = (rerank, explain, reference_date, show_expired, kwargs)
        # mem0 的空查询校验（memory/main.py:246-250）：空/纯空白直接 ValueError。
        if not isinstance(query, str) or not query.strip():
            raise ValueError("Invalid query: cannot be empty or whitespace-only.")
        scope = filters or {}
        results = []
        for row in self.rows.values():
            if row["user_id"] != scope.get("user_id"):
                continue
            if row["agent_id"] != scope.get("agent_id"):
                continue
            if query and query not in row["memory"]:
                continue
            results.append({"id": row["id"], "memory": row["memory"], "score": 1.0})
        return {"results": results[:top_k]}

    def get(self, memory_id):  # type: ignore[no-untyped-def]
        row = self.rows.get(memory_id)
        if not row:
            return None
        return {"id": row["id"], "memory": row["memory"]}

    def get_all(self, *, filters=None, top_k: int = 20, show_expired: bool = False, **kwargs):  # type: ignore[no-untyped-def]
        _ = (show_expired, kwargs)
        scope = filters or {}
        rows = [
            row
            for row in self.rows.values()
            if row["user_id"] == scope.get("user_id") and row["agent_id"] == scope.get("agent_id")
        ]
        return {"results": [{"id": row["id"], "memory": row["memory"]} for row in rows][:top_k]}

    def update(self, memory_id, text=None, metadata=None, expiration_date=None, data=None):  # type: ignore[no-untyped-def]
        _ = (metadata, expiration_date)
        new_text = text if text is not None else data
        row = self.rows.get(memory_id)
        if row is None:
            raise ValueError(f"Memory with id {memory_id} not found")
        row["memory"] = str(new_text)
        return {"message": "Memory updated successfully!"}

    def delete(self, memory_id):  # type: ignore[no-untyped-def]
        row = self.rows.get(memory_id)
        if row is None:
            raise ValueError(f"Memory with id {memory_id} not found")
        del self.rows[memory_id]
        return {"message": "Memory deleted successfully!"}


def _fake_backend() -> Any:
    return FakeMemoryBackend()


def _mem0_backend(monkeypatch: pytest.MonkeyPatch) -> Any:
    backend = Mem0LibraryMemoryBackend(config={}, memory_client=_Mem0LikeClient())
    # 缺失 ID 的退避重试在生产要等 3.5 秒（Milvus growing segment 的可见性窗口）。
    # 契约测的是**结果**不是耗时，所以把退避压到 0；真实耗时由
    # test_memory_backends.py 的重试用例单独钉。
    monkeypatch.setattr(backend, "_NOT_FOUND_RETRY_DELAYS", (0.0,))
    return backend


@pytest.fixture(params=["fake", "mem0"])
def backend(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Any:
    if request.param == "fake":
        return _fake_backend()
    return _mem0_backend(monkeypatch)


@pytest.fixture()
def fake_backend() -> Any:
    return _fake_backend()


@pytest.fixture()
def mem0_backend(monkeypatch: pytest.MonkeyPatch) -> Any:
    return _mem0_backend(monkeypatch)


def _add(backend: Any, content: str, *, user_id: str = "user-1", scope: str = "thinkback") -> str:
    events = backend.add(
        [{"role": "user", "content": content}],
        user_id=user_id,
        memory_scope_id=scope,
    )
    return str(events[0]["id"])


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------


def test_add_returns_items_carrying_id_and_memory(backend: Any) -> None:
    """写入必须回带 id 与正文：调用方靠 id 做后续更新/删除。"""
    events = backend.add(
        [{"role": "user", "content": "用户喜欢猫"}],
        user_id="user-1",
        memory_scope_id="thinkback",
    )

    assert isinstance(events, list)
    assert len(events) == 1
    assert events[0]["id"]
    assert "猫" in str(events[0]["memory"])


def test_add_with_empty_content_produces_no_events(backend: Any) -> None:
    """空正文不产生事件：避免把空记忆写进向量库。"""
    events = backend.add(
        [{"role": "user", "content": "   "}],
        user_id="user-1",
        memory_scope_id="thinkback",
    )

    assert events == []


# ---------------------------------------------------------------------------
# 检索
# ---------------------------------------------------------------------------


def test_search_finds_what_add_wrote(backend: Any) -> None:
    """写入的内容能被检索回来。"""
    _add(backend, "用户喜欢猫")

    results = backend.search("猫", user_id="user-1", memory_scope_id="thinkback", limit=10)

    assert len(results) == 1
    assert "猫" in str(results[0]["memory"])


def test_search_does_not_leak_another_users_memories(backend: Any) -> None:
    """检索必须按 user_id 隔离：串户就是数据泄露。"""
    _add(backend, "用户喜欢猫", user_id="user-1")
    _add(backend, "用户喜欢狗", user_id="user-2")

    results = backend.search("喜欢", user_id="user-1", memory_scope_id="thinkback", limit=10)

    assert [str(item["memory"]) for item in results] == ["用户喜欢猫"]


def test_search_does_not_leak_another_scope(backend: Any) -> None:
    """检索必须按 memory_scope_id 隔离：namespace 是真实分区维度。"""
    _add(backend, "用户喜欢猫", scope="thinkback")
    _add(backend, "用户喜欢狗", scope="other-namespace")

    results = backend.search("喜欢", user_id="user-1", memory_scope_id="thinkback", limit=10)

    assert [str(item["memory"]) for item in results] == ["用户喜欢猫"]


def test_search_respects_the_limit(backend: Any) -> None:
    """limit 必须真的限制条数。"""
    for index in range(5):
        _add(backend, f"用户喜欢猫{index}")

    results = backend.search("猫", user_id="user-1", memory_scope_id="thinkback", limit=2)

    assert len(results) == 2


# ---------------------------------------------------------------------------
# 更新
# ---------------------------------------------------------------------------


def test_update_changes_the_stored_text(backend: Any) -> None:
    """更新后检索得到新正文。"""
    memory_id = _add(backend, "用户喜欢猫")

    backend.update(memory_id, "用户喜欢狗")

    results = backend.search("狗", user_id="user-1", memory_scope_id="thinkback", limit=10)
    assert [str(item["memory"]) for item in results] == ["用户喜欢狗"]


def test_update_of_a_missing_id_raises(backend: Any) -> None:
    """更新不存在的记忆必须报错：静默 no-op 会让本地索引与向量库分叉。

    （fake 原来就是静默 no-op，等于骗测试。）
    """
    with pytest.raises(RuntimeError):
        backend.update("does-not-exist", "新内容")


# ---------------------------------------------------------------------------
# 删除
# ---------------------------------------------------------------------------


def test_delete_of_a_missing_id_is_idempotent(backend: Any) -> None:
    """删除不存在的记忆不得抛错：重复擦除必须能安全重试。"""
    backend.delete("does-not-exist")


def test_delete_removes_the_record(backend: Any) -> None:
    """删除后检索不到、点查也取不到。"""
    memory_id = _add(backend, "用户喜欢猫")

    backend.delete(memory_id)

    assert backend.search("猫", user_id="user-1", memory_scope_id="thinkback", limit=10) == []
    assert backend.get(memory_id) is None


def test_delete_many_returns_the_attempted_count_and_skips_empty_ids(backend: Any) -> None:
    """返回**尝试删除**的条数（不含空 ID）：调用方据此判断擦除覆盖范围。"""
    first = _add(backend, "用户喜欢猫")
    second = _add(backend, "用户喜欢狗")

    deleted = backend.delete_many([first, "", second])

    assert deleted == 2


# ---------------------------------------------------------------------------
# 列出与点查
# ---------------------------------------------------------------------------


def test_get_all_is_scoped_to_user_and_scope(backend: Any) -> None:
    """列出必须按 user+scope 隔离。"""
    _add(backend, "用户喜欢猫", user_id="user-1", scope="thinkback")
    _add(backend, "用户喜欢狗", user_id="user-2", scope="thinkback")
    _add(backend, "用户喜欢鼠", user_id="user-1", scope="other-namespace")

    items = backend.get_all(user_id="user-1", memory_scope_id="thinkback")

    assert [str(item["memory"]) for item in items] == ["用户喜欢猫"]


def test_get_all_respects_the_limit(backend: Any) -> None:
    """limit 必须真的限制条数：擦除复核按页取。"""
    for index in range(5):
        _add(backend, f"用户喜欢猫{index}")

    items = backend.get_all(user_id="user-1", memory_scope_id="thinkback", limit=2)

    assert len(items) == 2


def test_get_returns_none_for_a_missing_id(backend: Any) -> None:
    """点查缺失 ID 返回 None：擦除复核靠它判断「目标还在不在」。"""
    assert backend.get("does-not-exist") is None


def test_get_returns_the_record_for_an_existing_id(backend: Any) -> None:
    memory_id = _add(backend, "用户喜欢猫")

    record = backend.get(memory_id)

    assert record is not None
    assert "猫" in str(record["memory"])


# ---------------------------------------------------------------------------
# 范围删除
# ---------------------------------------------------------------------------


def test_delete_all_returns_the_deleted_ids_and_empties_the_scope(backend: Any) -> None:
    """范围删除必须回带被删的 ID：调用方要按这些 ID 清引擎侧原文。"""
    _add(backend, "用户喜欢猫")
    _add(backend, "用户喜欢狗")
    _add(backend, "别人的记忆", user_id="user-2")

    deleted = backend.delete_all(user_id="user-1", memory_scope_id="thinkback")

    assert len(deleted) == 2
    assert backend.get_all(user_id="user-1", memory_scope_id="thinkback") == []
    assert len(backend.get_all(user_id="user-2", memory_scope_id="thinkback")) == 1


# ---------------------------------------------------------------------------
# 已知差异：fake 与 mem0 在这些地方**不**等价
#
# 这些差异是 fake 做不到语义检索的必然结果，不是缺陷。但它们有后果：
# 拿 fake 断言这些行为，测的是 fake 而不是生产。逐条钉住 —— 谁想改
# fake 的行为（或让两边收敛），会先撞到这里，届时该决定是「改 fake」
# 还是「承认这是刻意的差异」。
# ---------------------------------------------------------------------------


def test_empty_query_divergence_is_pinned(fake_backend: Any, mem0_backend: Any) -> None:
    """空查询：fake 返回作用域内全部记录，mem0 直接报错。

    mem0 的空查询校验在 ``memory/main.py:246-250``（ValueError），经适配器的
    ``_call`` 包成 RuntimeError。

    后果：拿 fake 断言「空查询的召回」会看到一堆 L3 结果，而生产里空查询是
    降级的（L3 空 + ``l3_backend_unreachable``）。请求模型对 ``query`` 没有
    ``min_length``，空查询是合法输入 —— 所以这条差异是可达的。
    """
    _add(fake_backend, "用户喜欢猫")
    _add(mem0_backend, "用户喜欢猫")

    fake_items = fake_backend.search("", user_id="user-1", memory_scope_id="thinkback", limit=10)
    with pytest.raises(RuntimeError):
        mem0_backend.search("", user_id="user-1", memory_scope_id="thinkback", limit=10)

    assert len(fake_items) == 1


def test_threshold_divergence_is_pinned(fake_backend: Any, mem0_backend: Any) -> None:
    """阈值：fake 忽略它，mem0 用它当语义分的门。

    服务层另有一道自己的闸（``_l3_item_relevant``，用
    ``l3_score_threshold``），所以最终召回结果不受这条差异影响；但**直接**
    断言后端行为的用例会看到差别。
    """
    _add(fake_backend, "用户喜欢猫")
    _add(mem0_backend, "用户喜欢猫")

    fake_items = fake_backend.search(
        "猫", user_id="user-1", memory_scope_id="thinkback", limit=10, threshold=0.99
    )
    mem0_items = mem0_backend.search(
        "猫", user_id="user-1", memory_scope_id="thinkback", limit=10, threshold=0.99
    )

    assert len(fake_items) == 1
    assert len(mem0_items) == 1, "本替身给满分的记录，阈值 0.99 仍应命中"
