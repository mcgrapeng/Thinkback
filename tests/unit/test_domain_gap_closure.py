"""domain 层此前无测试触达的行：逐条钉住可观察行为。

覆盖目标（行号对应源码）：
- ``keys.scope_key``：导出但零调用的公共组合键（隔离维度拼接口径）。
- ``prompts.build_extraction_prompt``：全局默认 + 类型指令同时存在时的拼接顺序
  （ADR-0014：类型指令只能增加约束，不能吃掉全局硬约束）。
- ``content_quality._clamp``：负数夹取（自足性维度最大扣分 1.55 会算出 -0.55）。
- ``content_quality._score_clarity``：空内容记 0 分并给出「空内容」理由。
- ``memory_types.registry_from_dict``：profile 没有 ``types`` 键时按空注册表处理。
- ``memory_types.load_registry``：profile 引用的抽取指令文件缺失时报错。
- ``schemas``：metadata / provenance 为 None 或非 dict 时校验器只透传、
  由 pydantic 自身拒绝（错误类型是 dict_type，不是校验器自造的报错）。
- ``slots.extractors.extract_current_nickname``：昵称只剩语气词时丢弃该候选。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from thinkback.domain import content_quality, keys, prompts
from thinkback.domain.enums import MessageRole
from thinkback.domain.memory_types import load_registry, registry_from_dict
from thinkback.domain.slots.extractors import extract_current_nickname
from thinkback.memory.schemas import (
    AppendEventRequest,
    AppendMemoryRequest,
    MemoryMessage,
    SourceEvent,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _message() -> MemoryMessage:
    return MemoryMessage(message_id="m1", role=MessageRole.USER, content="你好", timestamp=NOW)


def _append(**kwargs: object) -> AppendMemoryRequest:
    payload: dict[str, object] = {
        "request_id": "req-1",
        "user_id": "user-1",
        "session_id": "s1",
        "round_id": "r1",
        "messages": [_message()],
        "source_timestamp": NOW,
    }
    payload.update(kwargs)
    return AppendMemoryRequest(**payload)  # type: ignore[arg-type]


def _event(**kwargs: object) -> SourceEvent:
    payload: dict[str, object] = {
        "event_id": "e1",
        "role": MessageRole.USER,
        "payload": "你好",
        "occurred_at": NOW,
    }
    payload.update(kwargs)
    return SourceEvent(**payload)  # type: ignore[arg-type]


# ── keys.scope_key ────────────────────────────────────────────────────


def test_scope_key_joins_user_id_and_scope_id() -> None:
    """隔离维度是 (user, scope) 的组合键：同 scope 换 user 必须换键。"""
    assert keys.scope_key("user-1", "session-1") == "user-1:session-1"
    assert keys.scope_key("user-1", "session-1") != keys.scope_key("user-2", "session-1")


# ── prompts.build_extraction_prompt ───────────────────────────────────


def test_build_extraction_prompt_keeps_global_default_before_type_instructions() -> None:
    """全局在前、类型在后：类型指令只能追加，不能替换全局硬约束。"""
    merged = prompts.build_extraction_prompt("BASE-RULES", "TYPE-RULES")

    assert merged == "BASE-RULES\n\nTYPE-RULES"
    assert merged.startswith("BASE-RULES")


def test_build_extraction_prompt_returns_type_instructions_when_there_is_no_default() -> None:
    """没有全局默认时原样返回类型指令（不凭空造一段默认约束）。"""
    assert prompts.build_extraction_prompt("", "TYPE-RULES") == "TYPE-RULES"


# ── content_quality ───────────────────────────────────────────────────


def test_self_containedness_is_clamped_to_zero_when_penalties_exceed_one() -> None:
    """扣分超过 1.0 时分数夹到 0.0，不得为负（负分会让排序把最差内容排前面）。"""
    # 「这上次说」同时命中：句首指代 0.60 + 引用过往 0.25 + 无主语 0.15 + 过短 0.30
    score = content_quality.score_content("这上次说")

    assert score.dimension(content_quality.QualityDimension.SELF_CONTAINEDNESS) == 0.0
    assert score.dimension(content_quality.QualityDimension.SELF_CONTAINEDNESS) >= 0.0


def test_empty_content_scores_zero_on_every_dimension() -> None:
    """空内容不参与任何维度：全维度 0 分，理由是「空内容」。

    （``_score_clarity`` 里的空内容分支因此不可达——``score_content`` 对纯空白
    内容先短路返回，属于防御性死分支，本轮按噪音登记，不强行造用例。）
    """
    score = content_quality.score_content("   ")

    assert all(value == 0.0 for value in score.dimensions.values())
    assert score.reasons[content_quality.QualityDimension.CLARITY] == ("空内容",)


# ── memory_types ──────────────────────────────────────────────────────


def test_a_profile_without_a_types_key_registers_no_types() -> None:
    """profile 省略 ``types`` 时注册表为空（显式优于隐式兜底：不给默认类型）。"""
    registry = registry_from_dict({}, namespace="thinkback")

    assert registry.names() == []
    assert registry.has("profile") is False


def test_load_registry_reports_a_missing_extraction_prompt_file(tmp_path: Path) -> None:
    """profile 引用的抽取指令文件缺失时报错（静默空指令会让抽取质量塌方）。"""
    (tmp_path / "thinkback.json").write_text(
        json.dumps(
            {
                "types": {
                    "process_preference": {
                        "extractor": "mem0",
                        "source_roles": ["user"],
                        "classification": "normal",
                        "conflict": "overwrite",
                        "retention": "forever",
                        "extraction_prompt": "prompts/missing.md",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="extraction prompt file not found"):
        load_registry(tmp_path, "thinkback")


# ── schemas：metadata / provenance 的校验边界 ─────────────────────────


def test_append_request_rejects_a_none_metadata_at_the_model_level() -> None:
    """metadata 为 None 时体积校验器只透传，由 pydantic 自己拒（错误类型是 dict_type）。"""
    with pytest.raises(ValidationError) as excinfo:
        _append(metadata=None)

    assert excinfo.value.errors()[0]["type"] == "dict_type"


def test_append_request_rejects_a_non_dict_metadata_at_the_model_level() -> None:
    """metadata 非 dict 时体积校验器只透传，由 pydantic 自己拒。"""
    with pytest.raises(ValidationError) as excinfo:
        _append(metadata="not-a-dict")

    assert excinfo.value.errors()[0]["type"] == "dict_type"


def test_append_event_request_rejects_a_none_metadata_at_the_model_level() -> None:
    """事件请求的 metadata 为 None 时由模型自身拒绝。"""
    with pytest.raises(ValidationError) as excinfo:
        AppendEventRequest(
            request_id="req-1",
            user_id="user-1",
            session_id="s1",
            event=_event(),
            metadata=None,  # type: ignore[arg-type]
        )

    assert excinfo.value.errors()[0]["type"] == "dict_type"


def test_append_event_request_rejects_a_non_dict_metadata_at_the_model_level() -> None:
    """事件请求的 metadata 非 dict 时由模型自身拒绝。"""
    with pytest.raises(ValidationError) as excinfo:
        AppendEventRequest(
            request_id="req-1",
            user_id="user-1",
            session_id="s1",
            event=_event(),
            metadata="not-a-dict",  # type: ignore[arg-type]
        )

    assert excinfo.value.errors()[0]["type"] == "dict_type"


def test_source_event_rejects_a_none_provenance_at_the_model_level() -> None:
    """provenance 为 None 时体积校验器只透传，由 pydantic 自己拒。"""
    with pytest.raises(ValidationError) as excinfo:
        _event(provenance=None)

    assert excinfo.value.errors()[0]["type"] == "dict_type"


# ── slots.extractors.extract_current_nickname ─────────────────────────


def test_a_nickname_made_only_of_particles_is_discarded() -> None:
    """昵称只剩语气词（「叫我啊啊」）时丢弃该候选，不产出空昵称。"""
    assert extract_current_nickname("叫我啊啊") is None
