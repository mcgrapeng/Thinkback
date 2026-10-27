"""数据分类的准入判定（domain 纯函数，无 IO）。

背景：`DataClassification` 的注释承诺了「PERSONAL 脱敏存储、SENSITIVE/RESTRICTED
不存储」，但原先没有任何执行点：分类只是被写进 metadata，数据照样落库、照样
发给 LLM/embedding/Mem0。这里把「能不能存」做成一个可独立测试的判定，
并在写入路径最前面调用，保证敏感级别不会漏进任何一层存储。
"""

from __future__ import annotations

from typing import Any

from thinkback.domain.enums import DataClassification

# 未声明分类时沿用现有行为：按个人数据对待，但允许存储。
_DEFAULT_CLASSIFICATION = DataClassification.PERSONAL.value


def resolve_classification(metadata: dict[str, Any] | None) -> str:
    """从请求 metadata 解析数据分类。

    未提供时返回 PERSONAL（历史默认）。显式给出未知值直接报错：
    静默降级成 normal/personal 会让调用方以为「已按敏感级处理」，
    实际却按更低级别存了下去。
    """
    if not metadata:
        return _DEFAULT_CLASSIFICATION
    raw = metadata.get("data_classification")
    if raw is None or raw == "":
        return _DEFAULT_CLASSIFICATION
    value = str(raw)
    valid = {member.value for member in DataClassification}
    if value not in valid:
        raise ValueError(f"unknown data_classification: {value}")
    return value


def blocks_storage(classification: str) -> bool:
    """SENSITIVE / RESTRICTED 禁止落库与外发（含 L1、LLM、embedding、Mem0）。"""
    return classification in {
        DataClassification.SENSITIVE.value,
        DataClassification.RESTRICTED.value,
    }


def assert_storable(classification: str) -> None:
    """禁止级别直接拒绝，消息里带 sensitive_or_restricted 供客户端识别。"""
    if blocks_storage(classification):
        raise ValueError(
            f"sensitive_or_restricted data cannot be stored or sent to LLM: {classification}"
        )
