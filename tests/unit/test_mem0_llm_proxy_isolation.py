"""mem0 自建 LLM 客户端的代理隔离。

机器上配了 SOCKS 代理又没装 socksio 时，httpx 会在**构造客户端**的阶段就抛
ImportError（``Using SOCKS proxy, but the 'socksio' package is not installed``）。
mem0 的 OpenAI LLM 没有 http_client 注入口 → 它建的客户端会踩这个坑：预热只记
一条 warning，用户看到的现象是「抽取一直为空、召回 0 条」，日志里没有任何指向
代理的线索。

内部端点本就不该走代理（仓库自己的两个客户端已经是 trust_env=False），这里钉住
mem0 那一侧也被换掉。

契约：不管 mem0 交给我们什么客户端，注入给它的都必须是不读环境代理的
（``trust_env=False``），且端点、凭据、重试语义原样保留。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from openai import OpenAI

from thinkback.memory.backends.mem0_library import _disable_env_proxy_for_mem0_llm


class _Mem0StyleClient:
    """mem0 建出来的客户端形态：只有 OpenAI SDK 会读的那几个属性。

    不用真客户端做替身：在有 SOCKS 代理的机器上，httpx 构造带代理的客户端
    本身就抛 ImportError——那正是被修的现象，不该出现在替身里。
    """

    api_key = "test-key"
    base_url = "http://internal-llm.example/v1"
    timeout = 30.0
    max_retries = 2


def test_the_mem0_llm_client_is_rebuilt_to_ignore_env_proxies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SOCKS 代理环境下也要能重建出客户端（构造阶段不炸），且不读环境代理。"""
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:9/")
    monkeypatch.setenv("HTTPS_PROXY", "socks5://127.0.0.1:9/")

    memory = SimpleNamespace(llm=SimpleNamespace(client=_Mem0StyleClient()))
    _disable_env_proxy_for_mem0_llm(memory)

    rebuilt = memory.llm.client
    assert isinstance(rebuilt, OpenAI)
    assert rebuilt._client.trust_env is False


def test_the_rebuilt_client_keeps_the_endpoint_key_and_retries() -> None:
    """换传输层不能换掉端点、凭据与重试语义。"""
    memory = SimpleNamespace(llm=SimpleNamespace(client=_Mem0StyleClient()))

    _disable_env_proxy_for_mem0_llm(memory)

    rebuilt = memory.llm.client
    assert str(rebuilt.base_url).rstrip("/") == _Mem0StyleClient.base_url
    assert rebuilt.api_key == _Mem0StyleClient.api_key
    assert rebuilt.max_retries == _Mem0StyleClient.max_retries


def test_the_rebuilt_client_uses_a_plain_http_transport() -> None:
    """换出来的客户端是普通 HTTP 传输层，不是代理传输层。"""
    memory = SimpleNamespace(llm=SimpleNamespace(client=_Mem0StyleClient()))

    _disable_env_proxy_for_mem0_llm(memory)

    transport: Any = memory.llm.client._client._transport
    assert isinstance(transport, httpx.HTTPTransport)


def test_a_client_without_endpoint_details_is_left_untouched() -> None:
    """不是 OpenAI 形态的客户端（非 OpenAI provider）不硬换：换不动就沿用。"""
    sentinel = SimpleNamespace(marker="original")
    memory = SimpleNamespace(llm=SimpleNamespace(client=sentinel))

    _disable_env_proxy_for_mem0_llm(memory)

    assert memory.llm.client is sentinel


def test_a_memory_without_an_llm_is_left_untouched() -> None:
    """没有 llm 字段的对象（mem0 版本差异）直接跳过，不抛异常。"""
    memory = SimpleNamespace()

    _disable_env_proxy_for_mem0_llm(memory)

    assert not hasattr(memory, "llm")
