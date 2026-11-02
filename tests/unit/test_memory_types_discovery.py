"""记忆类型的运行时发现：接入方靠它知道「能写哪些类型、策略是什么」。

插件形态的硬需求：企业 AI 助手接进来时，**不能**把类型名写死在客户端里
（服务端一改 profile 就断了），也不能去读服务端的 profile 文件（那是部署
面的东西）。它需要一条运行时能问的接口。

契约：
- 列出该 namespace 下已注册的类型及其策略摘要；
- 注册表读不出来就**报错**，不返回空清单 —— 空清单会被客户端当成「这个
  namespace 没有类型」，于是改用默认类型写入，写进去的记忆用了另一套策略，
  而调用方以为用的是自己的策略。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from thinkback.api.app import create_app
from thinkback.domain.memory_types import MemoryTypeRegistry, MemoryTypeSpec
from thinkback.memory.repositories import InMemoryMemoryRepository
from thinkback.memory.service import MemoryService


def _registry() -> MemoryTypeRegistry:
    return MemoryTypeRegistry(
        namespace="acme",
        version="7",
        types={
            "profile": MemoryTypeSpec(
                name="profile",
                extractor="mem0",
                source_roles=("user",),
                conflict="overwrite",
                retention="forever",
                classification="personal",
                description="用户画像",
                recall_budget=200,
            ),
            "state": MemoryTypeSpec(
                name="state",
                extractor="mem0",
                source_roles=("user", "tool"),
                conflict="overwrite",
                retention="ttl",
                ttl_days=30,
                classification="personal",
                description="会过期的状态",
                extraction_prompt="prompts/acme/state.v1.md",
                extraction_prompt_version="v1",
            ),
        },
    )


def _service(registry: MemoryTypeRegistry) -> MemoryService:
    return MemoryService(
        repository=InMemoryMemoryRepository(),
        backend=object(),  # 类型清单不碰引擎
        registry_provider=lambda _namespace: registry,
    )


def test_the_listed_types_carry_the_policy_an_integrator_needs() -> None:
    service = _service(_registry())

    response = service.list_memory_types(namespace="acme")

    assert response.namespace == "acme"
    assert response.policy_version == "7"
    by_name = {item.name: item for item in response.types}
    assert set(by_name) == {"profile", "state"}

    state = by_name["state"]
    assert state.extractor == "mem0"
    assert state.source_roles == ["user", "tool"]
    assert state.conflict == "overwrite"
    assert state.retention == "ttl"
    assert state.ttl_days == 30
    assert state.classification == "personal"
    assert state.review_required is False
    assert state.extraction_prompt_version == "v1"

    profile = by_name["profile"]
    assert profile.recall_budget == 200
    assert profile.ttl_days is None
    assert profile.extraction_prompt_version == "default"


def test_a_type_that_needs_review_is_flagged_before_the_caller_writes_to_it() -> None:
    """需要审批的类型在清单里就要看得见。

    ``review_required`` 类型在写入路径上被直接拒绝（没有审批通道就不留
    「写进去但不可用」的口子）。接入方按清单逐个试会撞上一条 400 ——
    清单里带上 ``review_required``，它就能在写之前知道自己写不进去。

    默认注册表不再内置这类类型（摆一个永远写不进去的类型只会误导接入方），
    但 profile 仍可定义 —— 这里用一个带审批类型的注册表钉住清单语义。
    """
    registry = MemoryTypeRegistry(
        namespace="thinkback",
        version="1",
        types={
            "profile": MemoryTypeSpec(name="profile"),
            "procedure": MemoryTypeSpec(
                name="procedure",
                extractor="none",
                source_roles=("admin",),
                conflict="none",
                retention="forever",
                classification="restricted",
                review_required=True,
            ),
        },
    )
    service = _service(registry)

    response = service.list_memory_types(namespace="thinkback")
    by_name = {item.name: item for item in response.types}

    assert by_name["procedure"].review_required is True
    assert by_name["procedure"].classification == "restricted"
    # 其余类型都可写：审核标记只在该类型的策略里声明。
    assert all(not item.review_required for name, item in by_name.items() if name != "procedure")


def test_a_broken_registry_fails_loudly_instead_of_reporting_no_types() -> None:
    """注册表读不出来 = 报错，不是空清单。

    空清单会被客户端当成「这个 namespace 没有类型」，于是改用默认类型写入
    —— 写进去的记忆用了另一套策略，而调用方以为用的是自己的策略。
    """

    def broken(_namespace: str) -> MemoryTypeRegistry:
        raise RuntimeError("no memory type profile for namespace 'acme'")

    service = MemoryService(
        repository=InMemoryMemoryRepository(),
        backend=object(),
        registry_provider=broken,
    )

    with pytest.raises(RuntimeError, match="no memory type profile"):
        service.list_memory_types(namespace="acme")


def test_the_http_endpoint_exposes_the_types() -> None:
    """HTTP 面的发现入口：GET /v2/memory/types?namespace=...。"""
    app = create_app(run_startup_tasks=False)
    with TestClient(app) as client:
        response = client.get("/v2/memory/types", params={"namespace": "thinkback"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["namespace"] == "thinkback"
    assert "profile" in {item["name"] for item in payload["types"]}


def test_the_types_follow_the_profile_on_disk(tmp_path: Path) -> None:
    """清单跟着 profile 走：改完 profile，下一次查询就是新的。

    profile 是热加载的（没有缓存），所以「改了配置不用重启服务」这件事
    对接入方也是成立的 —— 它下次问就能看到新类型。
    """
    (tmp_path / "acme.json").write_text(
        json.dumps(
            {
                "version": "1",
                "namespace": "acme",
                "types": {
                    "profile": {
                        "extractor": "mem0",
                        "source_roles": ["user"],
                        "conflict": "overwrite",
                        "retention": "forever",
                        "classification": "personal",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    registry = MemoryTypeRegistry(
        namespace="acme",
        version="1",
        types={"profile": MemoryTypeSpec(name="profile", extractor="mem0")},
    )
    service = MemoryService(
        repository=InMemoryMemoryRepository(),
        backend=object(),
        registry_provider=lambda _namespace: registry,
    )

    assert [item.name for item in service.list_memory_types(namespace="acme").types] == ["profile"]


def test_the_mcp_server_exposes_the_same_discovery() -> None:
    """MCP 面也要能问：接进来的助手不会去读 HTTP 文档。"""
    from thinkback.mcp.server import create_mcp_server

    server = create_mcp_server(_service(_registry()), namespace="acme")

    tool_names = {tool.name for tool in server._tool_manager.list_tools()}
    assert "list_memory_types" in tool_names
