"""对外契约与抽取提示词的一致性。

两处此前名不副实：
- 抽取提示词同时要求「跟随对话语言」与「只能纯中文」，英文对话与英文
  实体无法同时满足，模型只能随机挑一边。
- 数据分类把 PERSONAL 描述为「脱敏存储」、召回内容描述为「脱敏后正文」，
  但实现里没有任何脱敏步骤。调用方据此判断合规边界会误判。
"""

from __future__ import annotations

from thinkback.domain.enums import DataClassification
from thinkback.memory.backends.mem0_library import THINKBACK_CUSTOM_INSTRUCTIONS
from thinkback.memory.schemas import MemoryItem


def test_extraction_instructions_do_not_force_chinese() -> None:
    """提示词不得同时要求「跟随对话语言」和「只能纯中文」。"""
    instructions = THINKBACK_CUSTOM_INSTRUCTIONS.lower()

    assert "same language as the conversation" in instructions
    # 「纯中文 / pure chinese characters」这类硬约束必须去掉。
    assert "pure chinese" not in instructions
    assert "只能输出中文" not in instructions
    # 实体逐字保留的要求要留着。
    assert "verbatim" in instructions


def test_personal_classification_does_not_promise_redaction() -> None:
    """PERSONAL 不得宣称「脱敏存储」：实现里没有脱敏步骤。"""
    import inspect

    doc = inspect.getdoc(DataClassification) or ""
    assert "脱敏存储" not in doc
    assert "自动脱敏" in doc
    # 枚举自身的成员注释也不能承诺脱敏。
    assert "脱敏存储" not in (DataClassification.PERSONAL.__doc__ or "")


def test_recall_item_does_not_claim_redacted_content() -> None:
    """召回正文不是「脱敏后正文」。"""
    import inspect

    doc = inspect.getdoc(MemoryItem) or ""
    assert "脱敏后正文" not in doc
    assert "不做自动脱敏" in doc
