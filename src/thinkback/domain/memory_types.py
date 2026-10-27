"""记忆类型注册表：配置驱动「写什么、谁能写、留多久、怎么召回」。

为什么需要它：固定槽位（昵称/宠物/地点/生日…）把「记忆类型」写死在代码里，
换个垂类（如半导体：工艺节点、良率、设备、客户）就要改 Python。注册表把类型
定义移到配置，代码只保留通用机制。

边界（引擎优先原则）：
- 抽取、检索、去重、过期都由 Mem0 负责，注册表不重复实现；
- 注册表只决定「准入与策略」，不自己写行业正则或词表匹配逻辑；
- 行业实体词表（entity_vocabulary）作为配置数据，供抽取 prompt 与检索过滤使用。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

from thinkback.domain.enums import DataClassification

Extractor = Literal["mem0", "profile", "none"]
Conflict = Literal["overwrite", "append", "none"]
Retention = Literal["forever", "ttl"]


@dataclass(frozen=True)
class MemoryTypeSpec:
    """一种记忆类型的策略定义。"""

    name: str
    layer: Literal["L1", "L2", "L3"] = "L3"
    extractor: Extractor = "mem0"
    # 允许作为该类型来源的角色；assistant 默认不在其中（助手的话不是用户事实）。
    source_roles: tuple[str, ...] = ("user",)
    conflict: Conflict = "overwrite"
    retention: Retention = "forever"
    ttl_days: int | None = None
    # 只能是 DataClassification 的四个取值（normal/personal/sensitive/restricted）——
    # 分类语义由 domain/classification.py 统一执行，配置不能自造级别。
    classification: str = "personal"
    review_required: bool = False
    # 该类型在召回预算里的份额；None 表示用层默认配额。
    recall_budget: int | None = None
    # 受控实体词表（行业术语、别名），只作为数据，不参与匹配逻辑。
    entity_vocabulary: tuple[str, ...] = ()
    # 供抽取 prompt 引用的领域说明
    description: str = ""

    def allows_role(self, role: str) -> bool:
        return role in self.source_roles

    def allows_extraction(self) -> bool:
        return self.extractor != "none"


@dataclass(frozen=True)
class MemoryTypeRegistry:
    """按 namespace 生效的类型集合（不可变）。"""

    namespace: str = "thinkback"
    types: Mapping[str, MemoryTypeSpec] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "types", MappingProxyType(dict(self.types)))

    def get(self, name: str) -> MemoryTypeSpec:
        """取类型；未注册抛 KeyError（调用方要显式处理，不能静默换类型）。"""
        try:
            return self.types[name]
        except KeyError as exc:
            raise KeyError(
                f"memory type {name!r} is not registered in namespace {self.namespace!r}"
            ) from exc

    def has(self, name: str) -> bool:
        return name in self.types

    def names(self) -> list[str]:
        return sorted(self.types)

    def default_type(self) -> MemoryTypeSpec:
        """未指定类型时的兜底：profile 类型（当前行为）。"""
        return self.types["profile"]


def default_registry() -> MemoryTypeRegistry:
    """内置默认注册表：不配置时行为与现在一致。"""
    return MemoryTypeRegistry(
        namespace="thinkback",
        types={
            "profile": MemoryTypeSpec(
                name="profile",
                extractor="profile",
                source_roles=("user",),
                conflict="overwrite",
                retention="forever",
                classification="personal",
                description="用户画像：称呼、偏好、稳定的长期事实",
            ),
            "preference": MemoryTypeSpec(
                name="preference",
                extractor="mem0",
                source_roles=("user",),
                conflict="overwrite",
                retention="forever",
                classification="personal",
                description="偏好与习惯",
            ),
            "event": MemoryTypeSpec(
                name="event",
                extractor="mem0",
                source_roles=("user", "tool"),
                conflict="append",
                retention="forever",
                classification="personal",
                description="已发生的事件与叙事",
            ),
            "state": MemoryTypeSpec(
                name="state",
                extractor="mem0",
                source_roles=("user", "tool"),
                conflict="overwrite",
                retention="ttl",
                ttl_days=90,
                classification="personal",
                description="会过期的状态（过期由 Mem0 expiration_date 承担）",
            ),
            "commitment": MemoryTypeSpec(
                name="commitment",
                extractor="mem0",
                source_roles=("user", "tool"),
                conflict="append",
                retention="ttl",
                ttl_days=30,
                classification="personal",
                description="承诺与待办，走独立到期通道",
            ),
            "plan": MemoryTypeSpec(
                name="plan",
                extractor="mem0",
                source_roles=("user",),
                conflict="overwrite",
                retention="forever",
                classification="personal",
                description="计划与目标",
            ),
            "constraint": MemoryTypeSpec(
                name="constraint",
                extractor="mem0",
                source_roles=("user",),
                conflict="overwrite",
                retention="forever",
                classification="personal",
                description="约束与雷区（属用户事实，不是程序性规则）",
            ),
            "procedure": MemoryTypeSpec(
                name="procedure",
                extractor="none",
                source_roles=("admin",),
                conflict="none",
                retention="forever",
                classification="restricted",
                review_required=True,
                description="程序性规则：只允许管理员写入且需审批，召回时按提示词装配",
            ),
        },
    )


def registry_from_dict(payload: dict[str, Any], *, namespace: str) -> MemoryTypeRegistry:
    """从配置字典构造注册表（未列出的类型不注册 —— 显式优于隐式兜底）。"""
    valid_classifications = {member.value for member in DataClassification}
    types: dict[str, MemoryTypeSpec] = {}
    for name, raw in (payload.get("types") or {}).items():
        classification = str(raw.get("classification", "personal"))
        if classification not in valid_classifications:
            raise ValueError(
                f"memory type {name!r} has unknown classification {classification!r}; "
                f"expected one of {sorted(valid_classifications)}"
            )
        types[name] = MemoryTypeSpec(
            name=name,
            layer=raw.get("layer", "L3"),
            extractor=raw.get("extractor", "mem0"),
            source_roles=tuple(raw.get("source_roles", ("user",))),
            conflict=raw.get("conflict", "overwrite"),
            retention=raw.get("retention", "forever"),
            ttl_days=raw.get("ttl_days"),
            classification=classification,
            review_required=bool(raw.get("review_required", False)),
            recall_budget=raw.get("recall_budget"),
            entity_vocabulary=tuple(raw.get("entity_vocabulary", ())),
            description=raw.get("description", ""),
        )
    return MemoryTypeRegistry(namespace=namespace, types=types)


def load_registry(profile_dir: Path, namespace: str) -> MemoryTypeRegistry:
    """按 namespace 加载 profile；没有对应文件时回落到默认注册表。

    文件约定：``<profile_dir>/<namespace>.json``。
    """
    path = profile_dir / f"{namespace}.json"
    if not path.is_file():
        return default_registry()
    payload = json.loads(path.read_text(encoding="utf-8"))
    return registry_from_dict(payload, namespace=namespace)
