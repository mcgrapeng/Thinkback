"""槽位词表必须覆盖模型真实产出的中文措辞。

真实栈证据（zhiman38_27b 抽取）：模型把沟通偏好写成「小鹏希望得到简洁直接
的建议，不需要先进行安慰」、把工作状态写成「小鹏已经接受 Moonshot 的
offer，不再评估其它工作机会」。服务端词表只认「沟通偏好…」这类句式，
于是这两条记忆拿不到槽位 —— 既不能被冲突仲裁去重（同一事实存了两份），
也不能在召回时按槽位命中。
"""

from __future__ import annotations

import pytest

from thinkback.domain.slots.canonical import memory_conflict_slot
from thinkback.domain.slots.extractors import (
    extract_communication_preference,
    extract_current_work_status,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("小鹏希望得到简洁直接的建议，不需要先进行安慰", "简洁直接的建议"),
        ("用户希望被直接建议，不要先安慰", "直接建议"),
        ("沟通偏好也更新：我现在更想要简洁直接的建议，不需要先安慰。", "简洁直接的建议"),
    ],
)
def test_chinese_communication_preference_is_recognized(text: str, expected: str) -> None:
    assert extract_communication_preference(text) == expected
    assert memory_conflict_slot(text) == "communication_preference"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("小鹏已经接受 Moonshot 的 offer，不再评估其它工作机会", "accepted Moonshot 的 offer"),
    ],
)
def test_chinese_work_status_is_recognized(text: str, expected: str) -> None:
    assert extract_current_work_status(text) == expected
    assert memory_conflict_slot(text) == "current_work_status"


def test_plain_wish_without_advice_context_is_not_a_preference() -> None:
    """没有建议/沟通语境时不能当成沟通偏好（否则「希望明天开会」也会被当成）。"""
    assert extract_communication_preference("我希望明天早点出发") is None
