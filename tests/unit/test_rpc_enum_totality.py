"""领域枚举 → proto 枚举映射的全量性契约。

``rpc/converters.py`` 里有 7 张手写的「领域枚举 → proto 枚举」映射表
（``_MEMORY_STATUS_TO_PB`` 等）。它们是**手工同步**的两份清单：
``domain/enums.py`` 加一个成员，映射表不会自动跟着长。

漏同步的后果（实测）：``_managed_item_to_pb`` / ``task_response_to_pb``
里 ``_X_TO_PB[item.xxx]`` 直接 ``KeyError`` → gRPC 链路 500。
**HTTP 链路完全无感**（它不经过这些表），单元测试也全绿 ——
因为没有任何一条用例断言过「映射表的键集合 == 领域枚举的成员集合」。

这类缺陷只在 gRPC 联调当天暴露，而 gRPC 是对外契约面。so 这份用例的价值
不在于「覆盖了几行」，而在于**新增枚举成员时门禁会红**：

- 领域枚举加了成员、映射表没加 → 键集合不等 → 红
- 映射表加了成员、领域枚举没加 → 键集合不等 → 红
- proto 枚举加了成员、两边都没加 → 值集合不等 → 红
- 映射错位（``SESSION_REBUILD`` 指到 ``CHAT_ROUND``）→ 往返断言不等 → 红

``SESSION_REBUILD`` / ``IMPORT`` 这两个成员在整个测试树里零引用 ——
它们有 proto 映射，但没人钉过映射到哪个值。这里逐个点名断言。
"""

from __future__ import annotations

import pytest

from thinkback.domain.enums import (
    DataClassification,
    MemoryStatus,
    MemoryType,
    OperationType,
    SourceType,
    SummaryState,
    TaskStatus,
)
from thinkback.rpc import converters
from thinkback.rpc import memory_pb2 as pb

#: (映射表名, 领域枚举, proto 枚举名, proto 枚举前缀)
#:
#: proto 枚举的成员名带前缀（``MEMORY_STATUS_ACTIVE``），领域枚举没有
#: （``ACTIVE``）。前缀按 proto 定义写死在这张表里 —— proto 改名前缀，
#: 这条用例会红，那正是要人看一眼的时候。
_ENUM_MAPS = (
    ("_MEMORY_STATUS_TO_PB", MemoryStatus, "MemoryStatus", "MEMORY_STATUS_"),
    ("_SOURCE_TYPE_TO_PB", SourceType, "SourceType", "SOURCE_TYPE_"),
    ("_DATA_CLASS_TO_PB", DataClassification, "DataClassification", "DATA_CLASSIFICATION_"),
    ("_MEMORY_TYPE_TO_PB", MemoryType, "MemoryType", "MEMORY_TYPE_"),
    ("_TASK_STATUS_TO_PB", TaskStatus, "TaskStatus", "TASK_STATUS_"),
    ("_OP_TYPE_TO_PB", OperationType, "OperationType", "OPERATION_TYPE_"),
    ("_SUMMARY_STATE_TO_PB", SummaryState, "SummaryState", "SUMMARY_STATE_"),
)


def _mapping(name: str) -> dict[object, int]:
    mapping = getattr(converters, name)
    assert isinstance(mapping, dict), f"{name} 不是字典"
    return mapping


def _proto_members(enum_name: str, prefix: str) -> set[int]:
    proto_enum = getattr(pb, enum_name).DESCRIPTOR
    return {value.number for value in proto_enum.values if value.name.startswith(prefix)}


@pytest.mark.parametrize(
    ("map_name", "domain_enum", "proto_name", "prefix"),
    _ENUM_MAPS,
    ids=[item[0] for item in _ENUM_MAPS],
)
def test_the_mapping_covers_every_domain_enum_member(
    map_name: str,
    domain_enum: type,
    proto_name: str,
    prefix: str,
) -> None:
    """映射表的键 == 领域枚举的成员集合（不多不少）。

    漏一个 → 那个成员走到 gRPC 转换时 ``KeyError``；
    多一个 → 领域枚举已经删掉它，映射表是死条目。
    """
    mapping = _mapping(map_name)
    assert set(mapping) == set(domain_enum), (
        f"{map_name} 与 {domain_enum.__name__} 的成员不一致："
        f"缺 {sorted(set(domain_enum) - set(mapping))}，"
        f"多 {sorted(set(mapping) - set(domain_enum))}"
    )


@pytest.mark.parametrize(
    ("map_name", "domain_enum", "proto_name", "prefix"),
    _ENUM_MAPS,
    ids=[item[0] for item in _ENUM_MAPS],
)
def test_the_mapping_covers_every_proto_enum_member(
    map_name: str,
    domain_enum: type,
    proto_name: str,
    prefix: str,
) -> None:
    """映射表的值集合 == proto 枚举的成员集合。

    proto 加了成员、映射表没加 → 那个值永远发不出去（proto3 的 0 号值
    还会与「未设置」混淆）。这条把 proto 侧的漂移也钉住。
    """
    mapping = _mapping(map_name)
    assert set(mapping.values()) == _proto_members(proto_name, prefix), (
        f"{map_name} 的值集合与 proto {proto_name} 不一致："
        f"缺 {sorted(_proto_members(proto_name, prefix) - set(mapping.values()))}，"
        f"多 {sorted(set(mapping.values()) - _proto_members(proto_name, prefix))}"
    )


@pytest.mark.parametrize(
    ("map_name", "domain_enum", "proto_name", "prefix"),
    _ENUM_MAPS,
    ids=[item[0] for item in _ENUM_MAPS],
)
def test_every_mapping_entry_is_distinct(
    map_name: str,
    domain_enum: type,
    proto_name: str,
    prefix: str,
) -> None:
    """两个领域成员不能映到同一个 proto 值。

    映重了 = 客户端按 proto 值反推时拿到的是另一个语义，且不报错。
    """
    values = list(_mapping(map_name).values())
    assert len(values) == len(set(values)), f"{map_name} 有成员映到了重复的 proto 值"


@pytest.mark.parametrize(
    ("map_name", "domain_enum", "proto_name", "prefix"),
    _ENUM_MAPS,
    ids=[item[0] for item in _ENUM_MAPS],
)
def test_the_mapping_round_trips(
    map_name: str,
    domain_enum: type,
    proto_name: str,
    prefix: str,
) -> None:
    """domain → pb → domain 往返不丢成员。

    proto 侧按前缀反查回领域成员：映错位（``SESSION_REBUILD`` 指到
    ``CHAT_ROUND``）在这里现形 —— 键集合相等但值指错，上一条断言抓不到。
    """
    mapping = _mapping(map_name)
    proto_enum = getattr(pb, proto_name).DESCRIPTOR
    by_number = {value.number: value.name for value in proto_enum.values}
    for member in domain_enum:
        number = mapping[member]
        name = by_number[number]
        assert name == f"{prefix}{member.name}", (
            f"{map_name}[{member!r}] 映到了 {name}，按命名约定应是 {prefix}{member.name}"
        )


def test_the_rarely_used_source_types_are_pinned_to_their_proto_values() -> None:
    """`SESSION_REBUILD` / `IMPORT` 在整个测试树里零引用 —— 这里点名钉住。

    这两个是「重建补缺」与「外部导入」的来源类型：产出点少，但一旦产出
    就要经 gRPC 发给客户端。映射错了没有任何别的地方会发现。
    """
    mapping = _mapping("_SOURCE_TYPE_TO_PB")
    assert mapping[SourceType.SESSION_REBUILD] == pb.SOURCE_TYPE_SESSION_REBUILD
    assert mapping[SourceType.IMPORT] == pb.SOURCE_TYPE_IMPORT
    assert mapping[SourceType.CHAT_ROUND] == pb.SOURCE_TYPE_CHAT_ROUND
    assert mapping[SourceType.MANUAL_FIX] == pb.SOURCE_TYPE_MANUAL_FIX
    assert mapping[SourceType.SYSTEM_MIGRATION] == pb.SOURCE_TYPE_SYSTEM_MIGRATION


def test_the_session_delete_operation_type_is_pinned() -> None:
    """`DELETE_SESSION` 同样零引用：会话级删除的任务类型。

    它与 `DELETE_MEMORY` / `DELETE_ALL` 是三个不同的对外语义，
    映错位会让客户端把会话级删除当成单条删除。
    """
    mapping = _mapping("_OP_TYPE_TO_PB")
    assert mapping[OperationType.DELETE_SESSION] == pb.OPERATION_TYPE_DELETE_SESSION
    assert mapping[OperationType.DELETE_MEMORY] == pb.OPERATION_TYPE_DELETE_MEMORY
    assert mapping[OperationType.DELETE_ALL] == pb.OPERATION_TYPE_DELETE_ALL
    assert mapping[OperationType.ERASE] == pb.OPERATION_TYPE_ERASE
    assert mapping[OperationType.WRITE_ROUND] == pb.OPERATION_TYPE_WRITE_ROUND
    assert mapping[OperationType.UPDATE_MEMORY] == pb.OPERATION_TYPE_UPDATE_MEMORY
    assert mapping[OperationType.REBUILD_L2] == pb.OPERATION_TYPE_REBUILD_L2
    assert mapping[OperationType.REBUILD_L3] == pb.OPERATION_TYPE_REBUILD_L3
