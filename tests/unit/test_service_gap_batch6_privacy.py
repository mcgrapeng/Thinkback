"""service.py 隐私分级与脱敏缺口的行覆盖收口。

这一批钉的是写入链路的隐私原语：``_l3_metadata`` 的分级/溯源字段、
``_classify_data`` 的缺省分级、write-gate 三个判定包装，以及
``_redact_event`` —— 它直接用于 append 响应的 ``l3_events``
（service.py 876/916/1170/1322 行），改坏就是把记忆原文回显给调用方。

共同点：这些行的分支翻转不会炸任何东西，只会**静默漏数据或静默误杀**——
分级字段丢了没法审计、脱敏键漏剥原文进日志、safe/restricted 判定反了
就是凭据进长期记忆或整轮被静默扔掉。

所有依赖都是内存实现：``InMemoryMemoryRepository`` + ``FakeMemoryBackend``，
不碰网络、真实 LLM / Milvus / PG。
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from thinkback.domain.enums import DataClassification, SourceType
from thinkback.memory.backends import FakeMemoryBackend
from thinkback.memory.repositories import InMemoryMemoryRepository
from thinkback.memory.schemas import MemoryMessage, MessageRole
from thinkback.memory.service import MemoryService


def _service() -> MemoryService:
    return MemoryService(repository=InMemoryMemoryRepository(), backend=FakeMemoryBackend())


def _message(content: str, *, role: MessageRole = MessageRole.USER) -> MemoryMessage:
    return MemoryMessage(
        message_id="m-1",
        role=role,
        content=content,
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
    )


# ---------------------------------------------------------------------------
# _l3_metadata：分级 / 溯源 / 自定义字段
# ---------------------------------------------------------------------------


def test_l3_metadata_keeps_classification_and_provenance_fields() -> None:
    """调用方显式给的分级与溯源字段必须逐键落进 metadata：丢 policy_version 这类键后，策略变更就无从解释既有记忆。"""  # noqa: E501
    metadata = _service()._l3_metadata(
        [{"session_id": "s1", "round_id": "r1"}],
        {
            "data_classification": DataClassification.SENSITIVE.value,
            "backend_categories": ["preference", "routine"],
            "source_timestamp": "2026-01-02T03:04:05Z",
            # datetime 也会被 str() 收敛：到期日落到 metadata 后是字符串。
            "expires_at": datetime(2027, 1, 1, tzinfo=UTC),
            "source_text": "用户原话",
            "policy_version": "pv-1",
            "extraction_prompt_version": "epv-1",
            "extractor": "slot-v3",
            "tenant": "tenant-a",
        },
    )

    assert metadata["source_refs"] == [{"session_id": "s1", "round_id": "r1"}]
    assert metadata["data_classification"] == "sensitive"
    assert metadata["backend_categories"] == ["preference", "routine"]
    assert metadata["source_timestamp"] == "2026-01-02T03:04:05Z"
    assert metadata["expires_at"] == "2027-01-01 00:00:00+00:00"
    assert metadata["source_text"] == "用户原话"
    assert metadata["policy_version"] == "pv-1"
    assert metadata["extraction_prompt_version"] == "epv-1"
    assert metadata["extractor"] == "slot-v3"
    # 非保留键整包进 custom_metadata：企业按自己字段审计记忆的入口。
    assert metadata["custom_metadata"] == {"tenant": "tenant-a"}


def test_l3_metadata_omits_optional_keys_when_absent() -> None:
    """可选键缺省时必须整个键不存在（而不是空值占位）：下游把空串当真值过滤就会筛出垃圾。"""  # noqa: E501
    metadata = _service()._l3_metadata([], {})

    for key in (
        "backend_categories",
        "source_timestamp",
        "expires_at",
        "source_text",
        "policy_version",
        "extraction_prompt_version",
        "extractor",
        "custom_metadata",
    ):
        assert key not in metadata
    # 缺省值：未显式分级按 personal 兜底，来源默认 chat_round，类型默认 profile。
    assert metadata["data_classification"] == DataClassification.PERSONAL.value
    assert metadata["source_type"] == SourceType.CHAT_ROUND.value
    assert metadata["memory_type"] == "profile"
    # 空串 categories 与缺失等价，不能落成空列表占位。
    empty = _service()._l3_metadata([], {"backend_categories": ""})
    assert "backend_categories" not in empty


def test_l3_metadata_wraps_a_bare_string_category_into_a_list() -> None:
    """backend_categories 传单个字符串必须收敛成单元素列表：直接存字符串会让按列表消费的下游崩掉或漏筛。"""  # noqa: E501
    metadata = _service()._l3_metadata([], {"backend_categories": "routine"})

    assert metadata["backend_categories"] == ["routine"]


def test_classify_data_returns_explicit_value_and_defaults_to_personal() -> None:
    """分级兜底语义：显式给什么记什么、没给按 personal，翻转会把全量记忆标错敏感级。"""  # noqa: E501
    assert (
        MemoryService._classify_data({"data_classification": "sensitive"})
        == DataClassification.SENSITIVE.value
    )
    assert MemoryService._classify_data({}) == DataClassification.PERSONAL.value


# ---------------------------------------------------------------------------
# write-gate 判定包装：受限/安全
# ---------------------------------------------------------------------------


def test_restricted_reason_reports_the_matched_pattern_and_none_for_safe() -> None:
    """命中词表必须返回带具体 pattern 的原因（上游要写进 skipped 响应解释原因），安全文本必须返回 None。"""  # noqa: E501
    reason = _service()._restricted_reason([_message("api_key: abc123")])

    assert reason == "restricted_or_unsafe_memory_content:" + r"\bapi[_-]?key\s*[:=]"
    assert _service()._restricted_reason([_message("我养了一只猫，名字叫阿福。")]) is None


def test_restricted_reason_accepts_duck_typed_messages() -> None:
    """鸭子类型消息（纯字符串 role）也必须能判定：回放/重建链路里消息不总是 pydantic 模型。"""  # noqa: E501
    message = SimpleNamespace(role="user", content="sk-abcdef123456")

    assert _service()._restricted_reason([message]) is not None


def test_messages_are_restricted_or_unsafe_delegates_per_content() -> None:
    """整轮判定只看正文不看角色：反转或把角色算进去，要么放行凭据、要么误杀正常轮次。"""  # noqa: E501
    service = _service()

    assert service._messages_are_restricted_or_unsafe(
        [{"role": "user", "content": "api_key: abc123"}]
    )
    assert not service._messages_are_restricted_or_unsafe(
        [{"role": "user", "content": "今天天气不错"}]
    )


def test_text_is_restricted_or_unsafe_delegates_to_the_word_list() -> None:
    """单条文本判定必须原样委托词表：这里翻转一处，凭据直接进长期记忆。"""
    assert MemoryService._text_is_restricted_or_unsafe("password: hunter2")
    assert not MemoryService._text_is_restricted_or_unsafe("我养了一只猫，名字叫阿福。")


# ---------------------------------------------------------------------------
# _redact_event：append 响应 l3_events 的脱敏
# ---------------------------------------------------------------------------


def test_redact_event_drops_memory_and_content_and_renames_id() -> None:
    """l3_events 脱敏必须剥掉 memory/content 两个原文键并把 id 改名：漏剥一个就是记忆原文回显给调用方。"""  # noqa: E501
    event: dict[str, Any] = {
        "id": "backend-1",
        "memory": "记忆原文",
        "content": "消息原文",
        "memory_type": "profile",
    }

    redacted = MemoryService._redact_event(event)

    assert redacted == {"backend_memory_id": "backend-1", "memory_type": "profile"}
    # 脱敏只能产出新字典，不能顺手改掉入库的原始事件。
    assert event == {
        "id": "backend-1",
        "memory": "记忆原文",
        "content": "消息原文",
        "memory_type": "profile",
    }


def test_redact_event_without_id_adds_no_backend_memory_id() -> None:
    """事件本来就没有 id 时不能凭空造出 backend_memory_id：下游会拿它去查一条不存在的记忆。"""  # noqa: E501
    redacted = MemoryService._redact_event({"memory": "记忆原文", "status": "active"})

    assert redacted == {"status": "active"}
    assert "backend_memory_id" not in redacted
