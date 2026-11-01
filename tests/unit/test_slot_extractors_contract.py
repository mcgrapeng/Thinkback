"""槽位抽取器的**精确值**契约。

``domain/slots/extractors.py`` 是 P0 冲突判定的地基：``canonical.py`` 与
``query_slots.py`` 都在审计范围里，它们用的槽位值就是这 8 个抽取器抽出来的。
抽取器抽错 → 槽位值错 → 冲突判定错 → 一条该被 supersede 的旧记忆继续留在
画像里被召回（这正是 R-3 回归的成因）。

所以这里的用例断言**具体抽出的值**与**该拒绝时确实拒绝**，而不只是「能抽到东西」。
"""

from __future__ import annotations

import pytest

from thinkback.domain.slots.extractors import (
    extract_current_location,
    extract_current_nickname,
    extract_current_work_status,
)

# ---------------------------------------------------------------------------
# 昵称：闸门与清洗
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "The cat's name is Tom",
        "My dog's name is Rex",
    ],
)
def test_a_pet_name_is_not_a_nickname(text: str) -> None:
    """「猫叫 Tom」里的 Tom 是宠物名，不是用户的昵称。

    认成昵称的话，用户画像里会多出一个叫 Tom 的「昵称」槽位 —— 而它跟
    「用户喜欢别人叫他 X」是同一类噪声：旧昵称不会被 supersede。
    """
    assert extract_current_nickname(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "the book called Dune",
        "the movie called Heat",
        "a project called Apollo",
    ],
)
def test_a_titled_work_is_not_a_nickname(text: str) -> None:
    """「书叫 Dune」里的 Dune 是作品名，不是昵称。

    这批词（book / movie / film / song / album / project / meeting / event /
    article / column / restaurant / place）后面的 "called" 与
    「希望被叫作 X」的 "called" 长得一样 —— 不挡掉就会把作品名当昵称。
    """
    assert extract_current_nickname(text) is None


def test_the_from_to_pattern_needs_the_preferred_name_marker() -> None:
    """「from A to B」这个模式必须配上「preferred name」这个标记才算昵称。

    少了这个闸门，任何一句 "from Alice to Bob" 都会变成「昵称从 Alice 改成
    Bob」—— 那是槽位**变更**的语义，凭空多一条记录。
    """
    assert extract_current_nickname("from Alice to Bob") is None
    assert extract_current_nickname("User's preferred name from Alice to Bob") == "Bob"


def test_the_last_matching_name_wins() -> None:
    """同一句里出现多次时取**最后一个** —— 最新的那个才是当前昵称。"""
    assert extract_current_nickname("name is Tom and name is Jerry") == "Jerry"


def test_the_corrected_from_suffix_is_dropped() -> None:
    """「Bob (corrected from Robert)」抽出来是 Bob。

    括号里那段是**旧值**（来源文本里的对照说明），留在槽位值里就成了
    「Bob (corrected from Robert)」这种没人会用的昵称。
    """
    assert extract_current_nickname("User's name is Bob (corrected from Robert)") == "Bob"


# ---------------------------------------------------------------------------
# 地点：停词与长度
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "User lives in this",
        "User lives in that",
        "User lives in the moment",
    ],
)
def test_a_placeholder_is_not_a_location(text: str) -> None:
    """this / that / the moment 不是地名。

    它们是 1–6 个字母的词，正好能过「至少 2 个字符」那关 —— 不挡掉的话
    「I live in this」会给用户安一个叫 this 的城市。
    """
    assert extract_current_location(text) is None


def test_a_one_character_location_is_not_a_location() -> None:
    """只抽到 1 个字符 → 不是地名（截断噪声）。"""
    assert extract_current_location("搬到A工作了") is None
    assert extract_current_location("搬到上海工作了") == "上海"


# ---------------------------------------------------------------------------
# 大小写与切分
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("USER PREFERS TO BE CALLED XIAOPENG", "XIAOPENG"),
        ("User Prefers To Be Called Xiaopeng", "Xiaopeng"),
    ],
)
def test_the_english_patterns_ignore_case(text: str, expected: str) -> None:
    """英文模式必须大小写不敏感。

    抽取器的模式表是给英文来源文本用的，而英文来源里大小写并不统一
    （"USER LIVES IN ..."、"User Lives In ..."）。去掉 IGNORECASE 的话，
    大写写法整条匹配不上 —— 槽位值凭空消失，而画像里那条旧记忆还在。
    """
    assert extract_current_nickname(text) == expected


def test_the_uppercase_location_is_recognised() -> None:
    """地点的英文模式同样大小写不敏感。"""
    assert extract_current_location("USER LIVES IN HANGZHOU") == "HANGZHOU"


def test_the_work_status_stops_at_the_no_longer_clause() -> None:
    """工作状态在「不再评估其它工作机会」处截断。

    「已经接受 X 的 offer，不再评估其它工作机会」整句塞进槽位值的话，
    槽位里就带着一句跟状态无关的话 —— 与源规范化出的那条同槽位记忆
    对不上，两条变成并存的事实。
    """
    assert (
        extract_current_work_status("用户已接受腾讯的 offer，不再评估其它工作机会")
        == "accepted 腾讯的 offer"
    )
