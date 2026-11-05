"""随包发布的 profile 的类型集合与策略矩阵契约。

``profiles/thinkback.json`` 与 ``profiles/semiconductor.json`` 是**随镜像发布
的部署配置**（Dockerfile 里有 ``COPY profiles/ /app/profiles/``）。企业租户
加载的就是这两份文件 —— 配置写错，线上表现是「按另一套规则在跑」，
而仓库里没有任何东西会红。

此前只有 ``profiles/thinkback.json`` 有一份「文件版 vs ``default_registry()」
逐字段对比」（见 ``test_memory_types.py``），因为 thinkback 命名空间有代码版
的等价物可以对账。**``semiconductor.json`` 没有对账对象** —— 它的 8 个类型
（``equipment_context`` / ``commitment`` / ``procedure`` …）的类型集合与
策略矩阵（conflict / retention / ttl_days / classification / source_roles /
recall_budget）此前**一条断言都没有**。

这份用例把两份随包 profile 的策略矩阵逐字段钉死。它不是「快照比对」——
那会脆到无法维护；它断言的是**每个字段的取值有业务含义**（TTL 类型必须带
正数 ttl_days、追加型与覆盖型分开、准入角色必须是合法角色），任何一处被
改坏都能从失败信息里直接看出是哪条策略、为什么错。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from thinkback.domain.enums import DataClassification, MessageRole
from thinkback.domain.memory_types import MemoryTypeSpec, load_registry

PROFILE_DIR = Path(__file__).resolve().parents[2] / "profiles"

_NAMESPACES = ("thinkback", "semiconductor")

#: 每份随包 profile 的类型集合。类型增减是**有意的部署决策**，改动这份清单
#: 时必须同时说清为什么（新增类型 → 补清单；删除类型 → 从清单里移除）。
_EXPECTED_TYPES: dict[str, tuple[str, ...]] = {
    "thinkback": (
        "commitment",
        "constraint",
        "event",
        "plan",
        "preference",
        "profile",
        "state",
    ),
    "semiconductor": (
        "commitment",
        "equipment_context",
        "event",
        "preference",
        "procedure",
        "process_preference",
        "profile",
        "state",
    ),
}

#: 策略矩阵：类型 → (conflict, retention, ttl_days, classification, source_roles)
#:
#: ttl_days 写 None 表示该类型不设保留期（retention=forever）。
_EXPECTED_POLICY: dict[str, dict[str, tuple[str, str, int | None, str, tuple[str, ...]]]] = {
    "thinkback": {
        # 身份类：覆盖型（同一属性新值覆盖旧值）、永久保留、只认 user 来源。
        "profile": ("overwrite", "forever", None, "personal", ("user",)),
        "preference": ("overwrite", "forever", None, "personal", ("user",)),
        # 约束：同 profile/preference，程序性限制。
        "constraint": ("overwrite", "forever", None, "personal", ("user",)),
        # 计划：覆盖型。
        "plan": ("overwrite", "forever", None, "personal", ("user",)),
        # 事件：追加型 —— 事件是流水，不该被新事件覆盖。
        "event": ("append", "forever", None, "personal", ("user", "tool")),
        # 当前状态：覆盖型 + 90 天 TTL（状态会过期）。
        "state": ("overwrite", "ttl", 90, "personal", ("user", "tool")),
        # 承诺与待办：追加型 + 30 天 TTL。
        "commitment": ("append", "ttl", 30, "personal", ("user", "tool")),
    },
    "semiconductor": {
        "profile": ("overwrite", "forever", None, "personal", ("user",)),
        "preference": ("overwrite", "forever", None, "personal", ("user",)),
        # 工艺偏好：覆盖型 —— 用户改工艺偏好，新值覆盖旧值。
        "process_preference": ("overwrite", "forever", None, "personal", ("user",)),
        # 设备上下文：追加型 + 180 天 TTL —— 设备上下文是累积的设备事实。
        "equipment_context": ("append", "ttl", 180, "personal", ("user", "tool")),
        "event": ("append", "forever", None, "personal", ("user", "tool")),
        "state": ("overwrite", "ttl", 90, "personal", ("user", "tool")),
        "commitment": ("append", "ttl", 30, "personal", ("user", "tool")),
        # 程序性规则：不抽取（extractor=none）、需审批（review_required）、
        # 禁止落库（restricted）、非 user 来源。
        "procedure": ("none", "forever", None, "restricted", ("system",)),
    },
}


def _registry(namespace: str) -> Any:
    return load_registry(PROFILE_DIR, namespace)


@pytest.mark.parametrize("namespace", _NAMESPACES)
def test_the_shipped_profile_loads(namespace: str) -> None:
    """随包 profile 必须能被加载 —— 加载失败 = 整个命名空间退回默认注册表。"""
    registry = _registry(namespace)
    assert registry.namespace == namespace


@pytest.mark.parametrize("namespace", _NAMESPACES)
def test_the_shipped_profile_has_the_expected_type_set(namespace: str) -> None:
    """类型集合逐个点名。

    少一个类型 = 那个类型的记忆写不进去（未注册类型明确报错）；
    多一个类型 = 部署了没测试过的策略。
    """
    registry = _registry(namespace)
    assert tuple(registry.names()) == _EXPECTED_TYPES[namespace]


@pytest.mark.parametrize("namespace", _NAMESPACES)
def test_the_shipped_profile_policy_matrix(namespace: str) -> None:
    """每个类型的策略字段逐条断言。

    失败信息直接指出「哪个命名空间的哪个类型、哪个字段、期望什么、实际什么」——
    配置改坏时不用再靠猜。
    """
    registry = _registry(namespace)
    expected = _EXPECTED_POLICY[namespace]
    for name, (conflict, retention, ttl_days, classification, roles) in expected.items():
        spec: MemoryTypeSpec = registry.get(name)
        assert spec.conflict == conflict, f"{namespace}.{name} 的 conflict 变了"
        assert spec.retention == retention, f"{namespace}.{name} 的 retention 变了"
        assert spec.ttl_days == ttl_days, f"{namespace}.{name} 的 ttl_days 变了"
        assert spec.classification == classification, f"{namespace}.{name} 的 classification 变了"
        assert spec.source_roles == roles, f"{namespace}.{name} 的 source_roles 变了"


@pytest.mark.parametrize("namespace", _NAMESPACES)
def test_every_ttl_type_carries_a_positive_ttl(namespace: str) -> None:
    """TTL 类型必须带正数 ttl_days —— 没有到期日的 TTL 等于永不过期。"""
    registry = _registry(namespace)
    for name in registry.names():
        spec = registry.get(name)
        if spec.retention == "ttl":
            assert isinstance(spec.ttl_days, int) and spec.ttl_days > 0, (
                f"{namespace}.{name} 是 TTL 类型但 ttl_days={spec.ttl_days!r}"
            )
        else:
            assert spec.ttl_days is None, f"{namespace}.{name} 不是 TTL 类型却带了 ttl_days"


@pytest.mark.parametrize("namespace", _NAMESPACES)
def test_every_type_accepts_only_real_message_roles(namespace: str) -> None:
    """准入角色必须是 `MessageRole` 的合法取值。

    角色名写错 → 该类型永远匹配不到任何消息 → 静默地什么都抽不出来。
    ``load_registry`` 现在会当场报错（见
    ``tests/unit/test_source_role_validation_contract.py``），这条是第二道：
    防止将来有人放宽那条校验。
    """
    registry = _registry(namespace)
    known = {role.value for role in MessageRole}
    for name in registry.names():
        spec = registry.get(name)
        unknown = set(spec.source_roles) - known
        assert not unknown, f"{namespace}.{name} 声明了不存在的角色 {sorted(unknown)}"


@pytest.mark.parametrize("namespace", _NAMESPACES)
def test_every_type_uses_a_real_data_classification(namespace: str) -> None:
    """分类必须是四个合法级别之一 —— 它决定「能不能落库」。"""
    registry = _registry(namespace)
    known = {member.value for member in DataClassification}
    for name in registry.names():
        spec = registry.get(name)
        assert spec.classification in known, (
            f"{namespace}.{name} 的 classification={spec.classification!r} 不是合法级别"
        )


@pytest.mark.parametrize("namespace", _NAMESPACES)
def test_no_shipped_type_is_review_required_except_the_procedure(namespace: str) -> None:
    """需要审批的类型只有 `procedure`。

    ``review_required`` 类型在写入路径被直接拒绝（没有审批通道）——
    把它配给任何别的类型，那个类型就**永远写不进去**。而配置加载成功，
    没有任何报错（除了类型清单里写着它）。
    """
    registry = _registry(namespace)
    reviewed = {name for name in registry.names() if registry.get(name).review_required}
    assert reviewed == ({"procedure"} if namespace == "semiconductor" else set())


def test_the_procedure_type_is_not_extractable() -> None:
    """`procedure` 不参与抽取（extractor=none）。

    它是「程序性规则」—— 不该由 LLM 从对话里抽出来。
    """
    registry = _registry("semiconductor")
    assert registry.get("procedure").extractor == "none"
    assert registry.get("procedure").allows_extraction() is False


@pytest.mark.parametrize("role", ["user", "assistant", "tool", "system"])
def test_the_procedure_type_rejects_every_role(role: str) -> None:
    """`procedure` 对**任何**角色都写不进去（没有审批通道）。

    这条钉住「角色改成 system 之后行为没变」：审批闸门在准入判定**之前**
    触发（``service.py`` 的 append 路径先查 ``review_required``），
    所以它的 ``source_roles`` 取什么值都不影响写入结果 —— 角色从
    ``admin`` 改成 ``system``（``admin`` 不在 ``MessageRole`` 里）纯属
    配置合法性修正，不是行为变更。

    这条用例防止将来有人为了「让 procedure 能写进去」而放宽审批闸门：
    那样程序性规则会绕过审批直接落库。
    """
    from datetime import UTC, datetime

    from thinkback.api.dependencies import build_registry_provider
    from thinkback.infra.config import settings
    from thinkback.memory.backends import FakeMemoryBackend
    from thinkback.memory.repositories import InMemoryMemoryRepository
    from thinkback.memory.schemas import AppendMemoryRequest, MessageRole
    from thinkback.memory.service import MemoryService

    service = MemoryService(
        repository=InMemoryMemoryRepository(),
        backend=FakeMemoryBackend(),
        registry_provider=build_registry_provider(settings),
        l3_write_mode="sync",
    )
    request = AppendMemoryRequest(
        request_id=f"req-procedure-{role}",
        user_id="user-1",
        session_id="session-1",
        round_id=f"round-{role}",
        namespace="semiconductor",
        messages=[
            {
                "message_id": f"m-{role}",
                "role": MessageRole(role),
                "content": "以后所有变更必须先过工艺委员会评审。",
                "timestamp": datetime(2026, 11, 5, tzinfo=UTC),
            }
        ],
        source_timestamp=datetime(2026, 11, 5, tzinfo=UTC),
        metadata={"memory_type": "procedure"},
    )
    with pytest.raises(ValueError, match="requires review"):
        service.append(request)


@pytest.mark.parametrize("namespace", _NAMESPACES)
def test_the_overwrite_types_all_carry_an_extraction_prompt(namespace: str) -> None:
    """声明 overwrite 的类型必须配抽取指令。

    冲突键只认「属性名：值」形态的事实；没有指令的类型抽出来是自由文本，
    overwrite 空转 —— 新旧矛盾事实并存且都被召回（ADR-0004 / D2）。
    """
    registry = _registry(namespace)
    overwrite_types = [
        name for name in registry.names() if registry.get(name).conflict == "overwrite"
    ]
    assert overwrite_types, "覆盖型类型不应为空 —— 这条用例失去了对象"
    for name in overwrite_types:
        spec = registry.get(name)
        assert spec.extraction_prompt_text, f"{namespace}.{name} 声明了 overwrite 但没有抽取指令"
        assert spec.extraction_prompt_version, f"{namespace}.{name} 的抽取指令缺版本标识"


@pytest.mark.parametrize("namespace", _NAMESPACES)
def test_every_extraction_prompt_file_actually_exists(namespace: str) -> None:
    """抽取指令引用的文件必须真的在仓库里。

    ``load_registry`` 的 loader 找不到文件会抛错，所以「能加载」已经隐含了
    这一条；这里再显式钉一次，是为了让失败信息指向「哪个命名空间的哪个
    类型缺哪个文件」，而不是一个笼统的加载异常。
    """
    payload = json.loads((PROFILE_DIR / f"{namespace}.json").read_text(encoding="utf-8"))
    for name, spec in payload["types"].items():
        prompt = spec.get("extraction_prompt")
        if not prompt:
            continue
        assert (PROFILE_DIR / prompt).is_file(), (
            f"{namespace}.{name} 的抽取指令文件不存在：{prompt}"
        )


def test_the_shipped_profiles_carry_their_own_namespace() -> None:
    """profile 里的 namespace 字段必须与文件名一致。

    不一致 → 按文件名加载的命名空间与文件里声明的不是同一个，
    而「namespace」会随每条记忆落库并参与隔离判定。
    """
    for namespace in _NAMESPACES:
        payload = json.loads((PROFILE_DIR / f"{namespace}.json").read_text(encoding="utf-8"))
        assert payload["namespace"] == namespace
