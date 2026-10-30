"""记忆类型注册表：配置驱动「写什么、谁能写、留多久、怎么召回」。

为什么需要它：固定槽位（昵称/宠物名/地点/生日…）把「记忆类型」写死在代码里，
换个垂类（如半导体：工艺节点、良率、设备、客户）就要改 Python。注册表把类型
定义移到配置，代码只保留通用机制。

边界（引擎优先原则）：
- 抽取、检索、去重、过期都由 Mem0 负责，注册表不重复实现；
- 注册表只决定「准入与策略」，不自己写行业正则或词表匹配逻辑。

配置是**严格校验**的：未知字段、缺失必填项、非法取值一律拒绝生效，
不静默回落默认值（ADR-0004 / D29）。写错字段名而静默忽略是最难查的一类
配置问题，所以这里选择 fail-visible。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from thinkback.domain.enums import DataClassification

Extractor = Literal["mem0", "none"]
Conflict = Literal["overwrite", "append", "none"]
Retention = Literal["forever", "ttl"]

#: 类型定义里允许出现的字段。出现其它字段一律拒绝（不静默忽略）。
_TYPE_FIELDS = frozenset(
    {
        "extractor",
        "source_roles",
        "conflict",
        "retention",
        "ttl_days",
        "classification",
        "review_required",
        "description",
        "recall",
        "extraction_prompt",
    }
)
#: 类型定义里必须出现的字段。缺任何一个都拒绝生效。
_REQUIRED_TYPE_FIELDS = frozenset(
    {"extractor", "source_roles", "classification", "conflict", "retention"}
)
#: profile 文件顶层允许出现的字段。
_PROFILE_FIELDS = frozenset({"version", "namespace", "types"})
_RECALL_FIELDS = frozenset({"budget"})
_EXTRACTORS = frozenset({"mem0", "none"})
_CONFLICTS = frozenset({"overwrite", "append", "none"})
_RETENTIONS = frozenset({"forever", "ttl"})

_PROMPT_VERSION_PATTERN = re.compile(r"\.v(?P<version>\d+)$")


@dataclass(frozen=True)
class MemoryTypeSpec:
    """一种记忆类型的策略定义。

    只保留**真正被运行时消费**的字段：没有行为消费者的配置项只会给出
    错误预期（以为配了词表就会影响抽取），所以一律不留。

    字段：
      name: 类型名
      extractor: 抽取方式。``none`` = 不抽取（策略性条目）；
        ``mem0`` = 交给 Mem0 抽取（唯一真正实现的一种）。
      source_roles: 允许作为该类型来源的角色（assistant 默认不在其中：
        助手的话不是用户事实）。这同时是「这条角色的事件能不能成为
        该类型的事实」的判定依据。
      conflict: 同槽位新事实是否覆盖旧事实（overwrite / append / none）。
      retention: 保留策略（forever / ttl）。
      ttl_days: TTL 天数（retention=ttl 时生效，由 Mem0 expiration_date 承担）。
      classification: 该类型的敏感级别（语义由 domain/classification.py 统一执行）。
      review_required: 是否需要审批后才能写入。
      recall_budget: 该类型的 token 预算上限（None = 不单独限制）。
      extraction_prompt: 抽取指令文件路径（相对 profile 目录）。
      extraction_prompt_text: 指令正文（加载后填入）。
      extraction_prompt_version: 指令版本（从文件名解析，写进记忆记录）。
      description: **仅供人阅读**的类型说明，不参与任何行为。
    """

    name: str
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
    recall_budget: int | None = None
    extraction_prompt: str = ""
    extraction_prompt_text: str = ""
    extraction_prompt_version: str = ""
    # 仅供人阅读的类型说明；不参与抽取、检索或预算计算。
    description: str = ""

    def allows_role(self, role: str) -> bool:
        return role in self.source_roles

    def allows_extraction(self) -> bool:
        return self.extractor != "none"


@dataclass(frozen=True)
class MemoryTypeRegistry:
    """按 namespace 生效的类型集合（不可变）。"""

    namespace: str = "thinkback"
    version: str = "1"
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


def default_registry() -> MemoryTypeRegistry:
    """内置默认注册表：不配置时行为与现在一致。"""
    return MemoryTypeRegistry(
        namespace="thinkback",
        version="1",
        types={
            "profile": MemoryTypeSpec(
                name="profile",
                extractor="mem0",
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


def _validate_prompt_path(value: str, *, where: str) -> str:
    """抽取指令路径必须是 profile 目录下的相对路径。

    配置是部署面输入，路径穿越会让服务读到 profile 目录之外的文件。
    """
    if not value.strip():
        raise ValueError(f"{where}: 'extraction_prompt' must not be empty")
    if value.startswith("/") or ".." in Path(value).parts:
        raise ValueError(
            f"{where}: 'extraction_prompt' must be a relative path inside the profile dir"
        )
    return value


def prompt_version_from_filename(path: str) -> str:
    """从指令文件名解析版本：``process_preference.v3.md`` → ``v3``。

    没有版本后缀时用文件主干名，保证记忆记录里总有可追溯的版本标识。
    """
    stem = Path(path).stem
    match = _PROMPT_VERSION_PATTERN.search(stem)
    if match:
        return f"v{match.group('version')}"
    return stem


def registry_from_dict(
    payload: object,
    *,
    namespace: str,
    prompt_loader: Callable[[str], str] | None = None,
) -> MemoryTypeRegistry:
    """从配置字典构造注册表（未列出的类型不注册 —— 显式优于隐式兜底）。

    严格校验：未知字段、缺失必填项、非法取值一律抛 ``ValueError``，
    不静默忽略也不回落默认类型。
    """
    if not isinstance(payload, dict):
        raise ValueError(f"memory type profile for namespace {namespace!r} must be an object")
    unknown_top = sorted(set(payload) - _PROFILE_FIELDS)
    if unknown_top:
        raise ValueError(
            f"memory type profile for namespace {namespace!r} has unknown fields: {unknown_top}"
        )
    valid_classifications = {member.value for member in DataClassification}
    raw_types = payload.get("types")
    if raw_types is None:
        raw_types = {}
    if not isinstance(raw_types, dict):
        raise ValueError(
            f"memory type profile for namespace {namespace!r}: 'types' must be an object"
        )

    types: dict[str, MemoryTypeSpec] = {}
    for name, raw in raw_types.items():
        where = f"memory type {name!r}"
        if not isinstance(raw, dict):
            raise ValueError(f"{where}: definition must be an object")
        unknown = sorted(set(raw) - _TYPE_FIELDS)
        if unknown:
            raise ValueError(f"{where}: unknown fields: {unknown}")
        missing = sorted(_REQUIRED_TYPE_FIELDS - set(raw))
        if missing:
            raise ValueError(f"{where}: missing required fields: {missing}")

        classification = str(raw["classification"])
        if classification not in valid_classifications:
            raise ValueError(
                f"{where} has unknown classification {classification!r}; "
                f"expected one of {sorted(valid_classifications)}"
            )
        extractor = str(raw["extractor"])
        if extractor not in _EXTRACTORS:
            raise ValueError(
                f"{where} has unknown extractor {extractor!r}; "
                f"expected one of {sorted(_EXTRACTORS)}"
            )
        conflict = str(raw["conflict"])
        if conflict not in _CONFLICTS:
            raise ValueError(
                f"{where} has unknown conflict {conflict!r}; expected one of {sorted(_CONFLICTS)}"
            )
        retention = str(raw["retention"])
        if retention not in _RETENTIONS:
            raise ValueError(
                f"{where} has unknown retention {retention!r}; expected one of {sorted(_RETENTIONS)}"
            )

        source_roles = raw["source_roles"]
        if (
            not isinstance(source_roles, (list, tuple))
            or not source_roles
            or not all(isinstance(role, str) and role.strip() for role in source_roles)
        ):
            raise ValueError(f"{where}: 'source_roles' must be a non-empty list of role names")

        review_required = bool(raw.get("review_required", False))
        if review_required and "user" in source_roles:
            raise ValueError(
                f"{where}: 'review_required' types must not accept the 'user' role "
                "(a reviewed type needs a non-user source)"
            )

        ttl_days = raw.get("ttl_days")
        if retention == "ttl":
            if not isinstance(ttl_days, int) or isinstance(ttl_days, bool) or ttl_days <= 0:
                raise ValueError(
                    f"{where}: 'ttl_days' must be a positive integer when retention='ttl'"
                )
        elif ttl_days is not None:
            raise ValueError(f"{where}: 'ttl_days' is only allowed when retention='ttl'")

        recall = raw.get("recall")
        recall_budget: int | None = None
        if recall is not None:
            if not isinstance(recall, dict):
                raise ValueError(f"{where}: 'recall' must be an object")
            unknown_recall = sorted(set(recall) - _RECALL_FIELDS)
            if unknown_recall:
                raise ValueError(f"{where}: 'recall' has unknown fields: {unknown_recall}")
            raw_budget = recall.get("budget")
            if raw_budget is not None:
                if (
                    not isinstance(raw_budget, int)
                    or isinstance(raw_budget, bool)
                    or raw_budget <= 0
                ):
                    raise ValueError(f"{where}: 'recall.budget' must be a positive integer")
                recall_budget = raw_budget

        prompt_path = str(raw.get("extraction_prompt", ""))
        prompt_text = ""
        prompt_version = ""
        if prompt_path:
            _validate_prompt_path(prompt_path, where=where)
            if prompt_loader is None:
                raise ValueError(f"{where}: 'extraction_prompt' requires a prompt loader")
            prompt_text = prompt_loader(prompt_path)
            prompt_version = prompt_version_from_filename(prompt_path)

        types[name] = MemoryTypeSpec(
            name=name,
            extractor=extractor,  # type: ignore[arg-type]
            source_roles=tuple(str(role) for role in source_roles),
            conflict=conflict,  # type: ignore[arg-type]
            retention=retention,  # type: ignore[arg-type]
            ttl_days=ttl_days,
            classification=classification,
            review_required=review_required,
            recall_budget=recall_budget,
            extraction_prompt=prompt_path,
            extraction_prompt_text=prompt_text,
            extraction_prompt_version=prompt_version,
            description=str(raw.get("description", "")),
        )
    return MemoryTypeRegistry(
        namespace=namespace,
        version=str(payload.get("version", "1")),
        types=types,
    )


#: 属性名（冲突键）的长度上限。冲突键表达的是「这条事实讲的是哪个属性」，
#: 太长就说明这段文字不是「属性名：值」的结构。
ATTRIBUTE_NAME_MAX_LEN = 16


def extract_attribute_name(text: str) -> str | None:
    """从「属性名：值」格式的事实文本里取属性名。

    per-type 抽取指令要求每条事实写成 ``<属性名>：<值>``（例 ``工艺节点：3nm``）：
    属性名是「这条事实讲哪个属性」，值才是变量。

    为什么冲突键不能来自相似度：纠正是**语义不相似**的 ——「用户负责 3nm」与
    「用户负责 5nm」的向量距离很远，检索侧根本找不出后者是前者的纠正。冲突键
    必须是显式的属性名。

    取不到（没有冒号、冒号前不是短词、冒号后为空）时返回 ``None``，调用方
    回退到旧口径。
    """
    for separator in ("：", ":"):
        head, found, tail = text.partition(separator)
        if not found or not tail.strip():
            continue
        name = head.strip()
        if name and len(name) <= ATTRIBUTE_NAME_MAX_LEN:
            return name
    return None


def load_registry(profile_dir: Path, namespace: str) -> MemoryTypeRegistry:
    """按 namespace 加载 profile。

    文件约定：``<profile_dir>/<namespace>.json``。文件不存在时回落到默认
    注册表 —— 默认 namespace 就是内置类型集，缺文件是正常的。

    指令文件约定：``<profile_dir>/<extraction_prompt>``（相对路径），
    例如 ``prompts/semiconductor/process_preference.v1.md``。
    """
    path = profile_dir / f"{namespace}.json"
    if not path.is_file():
        return default_registry()
    payload = json.loads(path.read_text(encoding="utf-8"))

    def load_prompt(relative: str) -> str:
        prompt_path = profile_dir / relative
        if not prompt_path.is_file():
            raise ValueError(f"extraction prompt file not found: {prompt_path}")
        return prompt_path.read_text(encoding="utf-8")

    return registry_from_dict(payload, namespace=namespace, prompt_loader=load_prompt)


def has_profile(profile_dir: Path, namespace: str) -> bool:
    """该 namespace 是否有自己的 profile 文件。"""
    return (profile_dir / f"{namespace}.json").is_file()
