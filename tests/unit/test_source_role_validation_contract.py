"""`source_roles` 的角色名必须是 `MessageRole` 的合法取值。

配置是严格校验的（ADR-0004 / D29）：写错字段名而静默忽略是最难查的一类
配置问题，所以一律 fail-visible。**但角色名此前不在校验范围内** ——
``source_roles`` 只被检查「是非空字符串列表」，拼错的名字照样加载成功。

后果是静默的：准入判定是
``any(message.role in type_spec.source_roles for message in messages)``
（``memory/service.py`` 的 ``_run_l3_write``）。角色名拼错 → 永远匹配不上
任何消息 → **该类型永远抽不出任何东西**，而配置加载成功、没有任何报错。

随包发布的 ``profiles/semiconductor.json`` 里就有这样的类型：
``procedure`` 声明 ``source_roles: ["admin"]``，而 ``MessageRole`` 只有
user / assistant / tool / system —— 没有 admin。那个类型还叠了
``review_required`` 与 ``extractor: none`` 两层拦截，写入路径本就拒绝它，
所以线上没有实际故障；但它是**「角色名写错不会报错」的活证据**。

本文件钉住这条校验：角色名拼错必须**当场报错**，而不是静默地让类型失效。
"""

from __future__ import annotations

from typing import Any

import pytest

from thinkback.domain.enums import MessageRole
from thinkback.domain.memory_types import registry_from_dict

_NAMESPACE = "acme"

_VALID_TYPE: dict[str, Any] = {
    "extractor": "mem0",
    "source_roles": ["user"],
    "conflict": "overwrite",
    "retention": "forever",
    "classification": "personal",
}


def _payload(type_overrides: dict[str, Any] | None = None) -> Any:
    type_def = dict(_VALID_TYPE)
    type_def.update(type_overrides or {})
    return {"namespace": _NAMESPACE, "types": {"profile": type_def}}


def test_every_message_role_is_accepted_as_a_source_role() -> None:
    """四种合法角色都能写进 `source_roles`。"""
    registry = registry_from_dict(
        _payload({"source_roles": [role.value for role in MessageRole]}),
        namespace=_NAMESPACE,
    )
    assert registry.get("profile").source_roles == tuple(role.value for role in MessageRole)


@pytest.mark.parametrize("role", ["usr", "admin", "USER", "User", "user ", "unknown_role"])
def test_a_role_name_outside_message_role_is_rejected(role: str) -> None:
    """角色名不在 `MessageRole` 里 → 当场报错。

    拼错的角色名会让该类型永远匹配不到任何消息、静默地什么都抽不出来。
    """
    with pytest.raises(ValueError, match="source_roles"):
        registry_from_dict(_payload({"source_roles": [role]}), namespace=_NAMESPACE)


def test_the_error_message_names_the_offending_role() -> None:
    """报错要指出是哪个角色名不对 —— 配置错误的修法全靠这条消息。

    只点名**错的**那个：合法的角色名（这里 ``user``）不该出现在「未知角色」
    名单里 —— 它出现在「合法取值」清单里是对的。
    """
    with pytest.raises(ValueError) as excinfo:
        registry_from_dict(_payload({"source_roles": ["user", "usr"]}), namespace=_NAMESPACE)
    message = str(excinfo.value)
    assert "'usr'" in message
    assert "'user'" not in message.split("expected one of")[0]
