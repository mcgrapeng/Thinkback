"""依赖装配（``api/dependencies.py``）的配置开关契约。

这里的每个开关都对应一条**降级路径**：关掉之后必须走「不调用该外部依赖」
的分支，而不是抛异常或静默按开启处理。

- ``memory_decay_enabled=false`` → 不构造清扫器。没有清扫器不是故障 ——
  「不遗忘」是合法策略（ADR 语义）。反过来若开关读错、清扫器照常跑，
  就会在没配置 LLM 的环境里去调 LLM。
- ``memory_l2_llm_enabled=false`` → L2 用拼接摘要，不构造 LLM 客户端。
  LLM 不可用时 L2 始终要有降级内容，召回链路不能空转。
- 共享后台线程池必须是**单例**：两个清扫器各建一个池 = 线程数翻倍，
  而 L2/L3 共用预算的前提就是同一个池。

这些分支此前只有「开着」的一侧被覆盖（88% 行覆盖，缺的正好是
「关掉」的那一半）。
"""

from __future__ import annotations

from thinkback.api import dependencies
from thinkback.infra.config import Settings


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "environment": "development",
        "memory_decay_enabled": False,
        "memory_l2_llm_enabled": False,
        "memory_l3_executor_workers": 2,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_get_settings_returns_the_process_singleton() -> None:
    assert dependencies.get_settings() is dependencies.get_settings()


def test_decay_sweeper_is_not_built_when_decay_is_disabled() -> None:
    """遗忘是可选策略，关掉就不该构造清扫器。"""
    assert dependencies.build_decay_sweeper(_settings(), repository=object()) is None


def test_decay_sweeper_is_built_when_decay_is_enabled() -> None:
    sweeper = dependencies.build_decay_sweeper(
        _settings(memory_decay_enabled=True),
        repository=object(),
    )
    assert sweeper is not None


def test_l2_composer_is_not_built_when_llm_summary_is_disabled() -> None:
    """L2 走拼接降级，不构造 LLM 客户端。"""
    assert dependencies.get_l2_summary_composer(_settings()) is None


def test_shared_background_executor_is_a_singleton() -> None:
    """L2/L3/清扫共用一个池 —— 两个池意味着后台线程数翻倍。"""
    first = dependencies._build_shared_executor(_settings(memory_l3_executor_workers=3))
    second = dependencies._build_shared_executor(_settings(memory_l3_executor_workers=3))
    assert first is second
    first.shutdown(wait=False)


def test_reset_memory_service_clears_the_singleton() -> None:
    """lifespan 退出时必须清空单例 —— 否则下一轮生命周期复用已关闭的仓储。"""
    dependencies.reset_memory_service()
    assert dependencies._memory_service is None
