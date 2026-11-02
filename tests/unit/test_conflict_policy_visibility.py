"""冲突策略的可见性：声明了 overwrite 却算不出冲突键的类型要被点名。

引擎（mem0 2.2.1）的写入路径只产 ADD、不裁决矛盾，所以类型声明的
``conflict`` 是**唯一**的覆盖机制。冲突键取自事实文本的「属性名：值」形状，
而文本形状由 per-type 抽取指令规定 —— 没有指令的类型回退到 9 个固定 P0
槽位（中文消费级：昵称 / 宠物 / 住址 / 生日 …）。

于是这类配置在纸面上完全正常：``conflict: overwrite`` 写得规规矩矩，校验
全过，但企业垂类事实落在 P0 槽位之外，覆盖机制根本不存在 —— 旧值不会被
覆盖，新旧两条并存，两条都进召回。运行期有告警（service 里的
``_report_unresolvable_conflict_key``），但那是**逐条事实**的告警；运维看到
的是「这条事实算不出键」，不是「你这个类型的配置是空配置」。

所以补一条**配置期**的告警：加载注册表时点名这些类型。

这里用一份自造的 profile 钉住三件事（不依赖仓库里真实的 profile，免得那边
改了这里跟着漂）：
- 声明 overwrite 且没有抽取指令的类型被点名；
- 配了抽取指令的类型不被点名（它本来就能算出属性名）；
- 同一个 (namespace, 类型) 只报一次（注册表按请求读，不去重会变日志洪水）。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from loguru import logger

from thinkback.api.dependencies import build_registry_provider
from thinkback.infra.config import Settings

WITHOUT_PROMPT = {
    "namespace": "acme",
    "version": "1",
    "types": {
        "profile": {
            "extractor": "mem0",
            "source_roles": ["user"],
            "conflict": "overwrite",
            "retention": "forever",
            "classification": "personal",
        },
        "event": {
            "extractor": "mem0",
            "source_roles": ["user", "tool"],
            "conflict": "append",
            "retention": "forever",
            "classification": "personal",
        },
    },
}

WITH_PROMPT = {
    "namespace": "acme",
    "version": "1",
    "types": {
        "profile": {
            "extractor": "mem0",
            "source_roles": ["user"],
            "conflict": "overwrite",
            "retention": "forever",
            "classification": "personal",
            "extraction_prompt": "prompts/acme/profile.v1.md",
        },
    },
}


@pytest.fixture()
def warnings() -> Iterator[list[str]]:
    """收集 WARNING 级别的日志文案。"""

    sink: list[str] = []
    # 类型名在 loguru 的 extra 里，不在 message 里 —— 要连 extra 一起收。
    handler_id = logger.add(sink.append, level="WARNING", format="{message} {extra}")
    try:
        yield sink
    finally:
        logger.remove(handler_id)


def _write_profile(directory: Path, payload: dict, *, with_prompt: bool) -> None:
    (directory / "acme.json").write_text(json.dumps(payload), encoding="utf-8")
    if with_prompt:
        prompts = directory / "prompts" / "acme"
        prompts.mkdir(parents=True, exist_ok=True)
        (prompts / "profile.v1.md").write_text("属性名：值", encoding="utf-8")


def _provider_for(directory: Path):
    return build_registry_provider(
        Settings(memory_type_profile_dir=str(directory), environment="development")
    )


def test_a_type_declaring_overwrite_without_a_prompt_is_named(
    tmp_path: Path, warnings: list[str]
) -> None:
    """没有抽取指令的 overwrite 类型被点名 —— 它的覆盖机制根本不存在。"""

    _write_profile(tmp_path, WITHOUT_PROMPT, with_prompt=False)

    _provider_for(tmp_path)("acme")

    assert [w for w in warnings if "conflict='overwrite'" in w]
    assert any("profile" in w for w in warnings)


def test_a_type_with_a_per_type_prompt_is_not_named(tmp_path: Path, warnings: list[str]) -> None:
    """配了抽取指令的类型能算出属性名 → 覆盖机制存在 → 不该被点名。"""

    _write_profile(tmp_path, WITH_PROMPT, with_prompt=True)

    _provider_for(tmp_path)("acme")

    assert not [w for w in warnings if "conflict='overwrite'" in w]


def test_append_and_none_policies_are_never_named(tmp_path: Path, warnings: list[str]) -> None:
    """append / none 本来就不靠冲突键生效 —— 不该被点名。"""

    _write_profile(tmp_path, WITHOUT_PROMPT, with_prompt=False)

    _provider_for(tmp_path)("acme")

    assert not any("event" in w for w in warnings)


def test_the_same_type_is_named_only_once(tmp_path: Path, warnings: list[str]) -> None:
    """注册表按请求读；不去重会把启动提示变成日志洪水。"""

    _write_profile(tmp_path, WITHOUT_PROMPT, with_prompt=False)
    provider = _provider_for(tmp_path)

    provider("acme")
    provider("acme")
    provider("acme")

    hits = [w for w in warnings if "conflict='overwrite'" in w]
    assert len(hits) == 1


def test_the_built_in_registry_types_are_named_when_no_profile_exists(
    tmp_path: Path, warnings: list[str]
) -> None:
    """没有 profile 的 namespace 落回内置注册表 —— 内置的 overwrite 类型同样要点名。

    内置注册表里的类型没有抽取指令（ADR-0012：指令随 profile 存放），所以
    它们声明的 overwrite 只有命中 P0 槽位的事实才生效。点名它比让运维在
    运行日志里猜要快。
    """

    provider = build_registry_provider(
        Settings(memory_type_profile_dir=str(tmp_path), environment="development")
    )

    provider("no-such-namespace")

    hits = [w for w in warnings if "conflict='overwrite'" in w]
    assert len(hits) == len(
        [
            name
            for name in ("profile", "preference", "state", "plan", "constraint")
            if any(name in w for w in hits)
        ]
    )
    assert hits
