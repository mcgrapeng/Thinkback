"""R-3 回归：真实全链路跑出的槽位抽取质量问题。

来源：HTTP 全链路真实测试（ollama qwen2.5:7b 抽取 + 本地槽位引擎）中
items 出现 "User lives in 杭州工作了" / "User prefers to be called 小朋吧"。
"""

import pytest

from thinkback.domain.slots.extractors import (
    extract_current_location,
    extract_current_nickname,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("我最近搬到杭州工作了，在滨江上班。", "杭州"),
        ("搬到北京了", "北京"),
        ("我现在住在上海。", "上海"),
        ("住在杭州滨江", "杭州滨江"),
        ("User lives in Hangzhou", "Hangzhou"),
        ("User moved to Shenzhen", "Shenzhen"),
    ],
)
def test_extract_current_location_stops_at_chinese_suffixes(text: str, expected: str) -> None:
    """中文地名后接"工作了/上班/了/标点"时必须截断，不得吞进地名。"""
    assert extract_current_location(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("以后请叫我小朋吧，这是我的昵称。", "小朋"),
        ("称呼我阿花呀", "阿花"),
        ("叫我小明", "小明"),
        ("User prefers to be called Xiaopeng", "Xiaopeng"),
    ],
)
def test_extract_current_nickname_strips_trailing_particles(text: str, expected: str) -> None:
    """中文口语语气词（吧/呀/呢/哦…）不得成为昵称的一部分。"""
    assert extract_current_nickname(text) == expected
