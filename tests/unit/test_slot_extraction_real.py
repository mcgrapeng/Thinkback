"""R-3 回归：真实全链路跑出的槽位抽取质量问题。

来源：HTTP 全链路真实测试（ollama qwen2.5:7b 抽取 + 本地槽位引擎）中
items 出现 "User lives in 杭州工作了" / "User prefers to be called 小朋吧"。
"""

import pytest

from thinkback.domain.slots.extractors import (
    extract_current_location,
    extract_current_nickname,
    extract_favorite_consumable,
    extract_pet_name,
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


# ---------------------------------------------------------------------------
# 退化成空值的分支（此前 0 覆盖）
# ---------------------------------------------------------------------------


def test_a_nickname_that_is_only_a_particle_is_not_a_nickname() -> None:
    """模式匹配上了但抽出来的是语气词 → 换下一个模式，全都不行才返回 None。

    「叫我吧」里的「吧」被剥掉后是空的：此时必须返回 None，而不是把空串
    当成一个昵称（「用户的昵称是」这种记录进了索引就是脏数据）。
    """
    assert extract_current_nickname("叫我吧") is None
    assert extract_current_nickname("称呼我") is None


def test_a_location_of_one_character_is_not_a_location() -> None:
    """「搬到A工作了」抽出来的地名只有 1 个字符 → 不是地名。

    太短的地名多半是截断噪声（"搬到A工作"里的 A）。放它进索引 = 每次
    提到"搬到A"就凭空冒出一个叫 A 的城市。
    """
    assert extract_current_location("搬到A工作了") is None
    assert extract_current_location("搬到上海工作了") == "上海"


def test_a_favorite_consumable_that_is_only_a_particle_is_not_one() -> None:
    """「我最喜欢喝的」抽出来的是「的」→ 剥掉后是空 → 不是饮品名。

    结构助词「的」被当成了饮品名，索引里就会多出一条「用户喜欢喝的」。
    """
    assert extract_favorite_consumable("我最喜欢喝的") is None
    assert extract_favorite_consumable("我最喜欢喝美式咖啡") == ("drink", "美式咖啡")


# ---------------------------------------------------------------------------
# 宠物种类：鸟与兔此前一次都没被测过
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("User has a bird named Kiwi", ("bird", "Kiwi")),
        ("User has a parrot named Polly", ("bird", "Polly")),
        ("我家养了只鹦鹉叫Polly", ("bird", "Polly")),
        ("User has a rabbit named Snowball", ("rabbit", "Snowball")),
        ("User has a bunny named Cloud", ("rabbit", "Cloud")),
        ("我家养了只兔子叫雪球", ("rabbit", "雪球")),
    ],
)
def test_every_pet_kind_is_recognised(text: str, expected: tuple[str, str]) -> None:
    """四种宠物种类都要认得出来。

    猫狗之外的两种（鸟、兔）在测试里一次都没出现过 —— 而它们的防捏造
    匹配表是各自独立的（鸟认 bird/parrot/鸟/鹦鹉，兔认 rabbit/bunny/兔/
    兔子）。表错了就是一类记忆整体漏判或整体误判。
    """
    assert extract_pet_name(text) == expected


def test_a_noun_that_merely_contains_the_pet_word_is_not_a_pet() -> None:
    """「我在卖兔粮」里的「兔」不是宠物 —— 只是名词。

    种类判定只看有没有那个字，于是「兔粮」「鸟摆件」都会被当成宠物名，
    接着用错误的种类去做源文本校验，整条链路的依据就错了。
    """
    assert extract_pet_name("我在卖兔粮") is None
