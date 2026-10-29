"""Milvus 启动期引导：建库与集合一致性升级。

这两个函数是 mem0 能不能写进向量库的前置条件，历史上一次都没被执行过：

- db 不存在 → mem0 首次写抛 ``code=800 (database not found)``，append 拖成 502；
- 集合一致性停在 Bounded → 刚写完的向量立刻查不到，召回命中率掉下去。

两者的失败契约都是「只 warning，不抛异常」：启动期不能因为向量库连不上就
起不来 —— 业务侧在 append 时会看到更明确的 800/802。

MilvusClient 在函数内部 import，所以这里用假模块顶掉 ``pymilvus``：
既不碰网络，又能逐条脚本化故障。
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from thinkback.infra.milvus import (
    ensure_milvus_collection_consistency,
    ensure_milvus_database,
)


class _FakeMilvusClient:
    """脚本化的 MilvusClient。"""

    def __init__(self, harness: _Harness, **kwargs: Any) -> None:
        self._harness = harness
        self.init_kwargs = kwargs
        self.closed = False
        self.created: list[str] = []
        self.altered: list[tuple[str, dict[str, Any]]] = []

    def list_databases(self) -> list[str]:
        if self._harness.list_error is not None:
            raise self._harness.list_error
        return list(self._harness.databases)

    def create_database(self, name: str) -> None:
        if self._harness.create_error is not None:
            raise self._harness.create_error
        self.created.append(name)

    def has_collection(self, name: str) -> bool:
        if self._harness.has_collection_error is not None:
            raise self._harness.has_collection_error
        return name in self._harness.collections

    def describe_collection(self, name: str) -> dict[str, Any]:
        if self._harness.describe_error is not None:
            raise self._harness.describe_error
        return {"consistency_level_name": self._harness.levels.get(name, "Bounded")}

    def alter_collection_properties(self, name: str, props: dict[str, Any]) -> None:
        if self._harness.alter_error is not None:
            raise self._harness.alter_error
        self.altered.append((name, props))

    def close(self) -> None:
        self.closed = True


class _Harness:
    def __init__(self) -> None:
        self.clients: list[_FakeMilvusClient] = []
        self.databases: list[str] = ["default"]
        self.collections: set[str] = {"thinkback"}
        self.levels: dict[str, str] = {"thinkback": "Bounded"}
        self.connect_error: Exception | None = None
        self.list_error: Exception | None = None
        self.create_error: Exception | None = None
        self.has_collection_error: Exception | None = None
        self.describe_error: Exception | None = None
        self.alter_error: Exception | None = None

    def make_client(self, **kwargs: Any) -> _FakeMilvusClient:
        if self.connect_error is not None:
            raise self.connect_error
        client = _FakeMilvusClient(self, **kwargs)
        self.clients.append(client)
        return client

    @property
    def client(self) -> _FakeMilvusClient:
        assert self.clients, "没有创建过 Milvus 客户端"
        return self.clients[-1]


@pytest.fixture()
def milvus(monkeypatch: pytest.MonkeyPatch) -> _Harness:
    state = _Harness()
    module = types.ModuleType("pymilvus")
    module.MilvusClient = state.make_client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pymilvus", module)
    return state


# ---------------------------------------------------------------------------
# ensure_milvus_database
# ---------------------------------------------------------------------------


def test_ensure_milvus_database_creates_the_database_when_it_is_missing(milvus: _Harness) -> None:
    """db 不存在就建：mem0 不会自己建，第一次写就会撞 code=800。"""
    milvus.databases = ["default"]

    ensure_milvus_database(milvus_url="http://milvus:19530", milvus_database="thinkback")

    assert milvus.client.created == ["thinkback"]


def test_ensure_milvus_database_skips_creation_when_it_already_exists(milvus: _Harness) -> None:
    """db 已存在就不要重复建。"""
    milvus.databases = ["default", "thinkback"]

    ensure_milvus_database(milvus_url="http://milvus:19530", milvus_database="thinkback")

    assert milvus.client.created == []


def test_ensure_milvus_database_does_not_connect_when_database_name_is_empty(
    milvus: _Harness,
) -> None:
    """没配 db 名就不连：没有 db 名时连上去也改不了什么。"""
    ensure_milvus_database(milvus_url="http://milvus:19530", milvus_database="")

    assert milvus.clients == []


def test_ensure_milvus_database_survives_connection_failure(milvus: _Harness) -> None:
    """连不上不能挡启动：只 warning，服务照常 boot。"""
    milvus.connect_error = RuntimeError("connection refused")

    ensure_milvus_database(milvus_url="http://milvus:19530", milvus_database="thinkback")


def test_ensure_milvus_database_survives_list_failure(milvus: _Harness) -> None:
    """列库失败不能挡启动，但客户端必须关掉（否则连接泄漏）。"""
    milvus.list_error = RuntimeError("list failed")

    ensure_milvus_database(milvus_url="http://milvus:19530", milvus_database="thinkback")

    assert milvus.client.closed is True


def test_ensure_milvus_database_survives_create_failure(milvus: _Harness) -> None:
    """建库失败不能挡启动：业务侧会在 append 时看到明确的错误码。"""
    milvus.create_error = RuntimeError("db name illegal")

    ensure_milvus_database(milvus_url="http://milvus:19530", milvus_database="thinkback")

    assert milvus.client.closed is True


def test_ensure_milvus_database_closes_the_client_on_the_success_path(milvus: _Harness) -> None:
    """成功建库后也要关客户端。"""
    ensure_milvus_database(milvus_url="http://milvus:19530", milvus_database="thinkback")

    assert milvus.client.closed is True


# ---------------------------------------------------------------------------
# ensure_milvus_collection_consistency
# ---------------------------------------------------------------------------


def test_consistency_raises_the_collection_to_strong(milvus: _Harness) -> None:
    """Bounded 读不到刚写的向量 → 提到 Strong。"""
    milvus.levels["thinkback"] = "Bounded"

    level = ensure_milvus_collection_consistency(
        milvus_url="http://milvus:19530",
        milvus_database="thinkback",
        collection_name="thinkback",
    )

    assert level == "Strong"
    assert milvus.client.altered == [("thinkback", {"consistency_level": "Strong"})]


def test_consistency_returns_current_level_when_already_strong(milvus: _Harness) -> None:
    """已经是目标级别就别改：多余的 DDL 只会给 Milvus 加负担。"""
    milvus.levels["thinkback"] = "Strong"

    level = ensure_milvus_collection_consistency(
        milvus_url="http://milvus:19530",
        milvus_database="thinkback",
        collection_name="thinkback",
    )

    assert level == "Strong"
    assert milvus.client.altered == []


def test_consistency_skips_when_the_collection_does_not_exist_yet(milvus: _Harness) -> None:
    """集合还没建（mem0 首次写入才建）→ 跳过，调用方写完再调一次。"""
    milvus.collections = set()

    level = ensure_milvus_collection_consistency(
        milvus_url="http://milvus:19530",
        milvus_database="thinkback",
        collection_name="thinkback",
    )

    assert level == "skipped"
    assert milvus.client.altered == []


def test_consistency_connects_to_the_configured_database(milvus: _Harness) -> None:
    """必须显式传 db_name：否则改的是 default 库里的同名集合，不是 mem0 真正写的那个。"""
    ensure_milvus_collection_consistency(
        milvus_url="http://milvus:19530",
        milvus_database="thinkback",
        collection_name="thinkback",
    )

    assert milvus.client.init_kwargs["db_name"] == "thinkback"


def test_consistency_reports_failed_when_connection_fails(milvus: _Harness) -> None:
    """连不上不能打断已经成功的记忆写入。"""
    milvus.connect_error = RuntimeError("connection refused")

    level = ensure_milvus_collection_consistency(
        milvus_url="http://milvus:19530",
        milvus_database="thinkback",
        collection_name="thinkback",
    )

    assert level == "failed"


def test_consistency_reports_failed_when_the_update_fails(milvus: _Harness) -> None:
    """改属性失败不能把异常抛给写入路径。"""
    milvus.alter_error = RuntimeError("alter failed")

    level = ensure_milvus_collection_consistency(
        milvus_url="http://milvus:19530",
        milvus_database="thinkback",
        collection_name="thinkback",
    )

    assert level == "failed"
    assert milvus.client.closed is True


def test_consistency_reports_failed_when_describing_the_collection_fails(
    milvus: _Harness,
) -> None:
    """读不到当前级别 → 判失败，不猜。"""
    milvus.describe_error = RuntimeError("describe failed")

    level = ensure_milvus_collection_consistency(
        milvus_url="http://milvus:19530",
        milvus_database="thinkback",
        collection_name="thinkback",
    )

    assert level == "failed"
    assert milvus.client.closed is True


def test_consistency_closes_the_client_on_the_success_path(milvus: _Harness) -> None:
    ensure_milvus_collection_consistency(
        milvus_url="http://milvus:19530",
        milvus_database="thinkback",
        collection_name="thinkback",
    )

    assert milvus.client.closed is True
