"""槽位词表的 golden 用例：每条措辞配一个样例，钉住抽取器的输出。

背景：``domain/slots/_vocabulary.py`` 里的词表是**数据**（措辞），不是判定逻辑。
mutmut 逐条改这些字面量，产出的大多是「等价变异」（大小写 / ``XX`` 包裹）——
那不是缺陷信号，是噪音，所以词表被移出变异范围（``pyproject.toml`` 的
``do_not_mutate``）。移出范围不等于没人管：这里逐条钉死。

每行钉两件事：

1. 该模式**仍然命中**这条样例 —— 模式被改坏、删掉、拼错 → 红；
2. 抽取器在这条样例上的**输出不变** —— 判定口径（闸门、末次匹配、
   取值清洗）被改坏 → 红。

闸门词（marker）另有一组样例：闸门词被删掉，那条样例就抽不出值 → 红。

模式之间本来就有重叠（"preferred name from X to Y" 同时命中 3 条模式），
所以不是每条样例都只命中目标模式 —— 那些样例在数据里
显式记下了同时命中谁，不假装自己是孤例。
"""

from __future__ import annotations

import re

import pytest

from thinkback.domain.slots import _vocabulary
from thinkback.domain.slots.extractors import (
    extract_birthday,
    extract_communication_preference,
    extract_current_location,
    extract_current_nickname,
    extract_current_work_status,
    extract_favorite_consumable,
    extract_pet_name,
    extract_sleep_reminder_preference,
)

PATTERN_TABLES = (
    "BIRTHDAY_PATTERNS",
    "COMMUNICATION_PATTERNS",
    "FAVORITE_DRINK_PATTERNS",
    "FAVORITE_FOOD_PATTERNS",
    "LOCATION_PATTERNS",
    "NICKNAME_PATTERNS",
    "PET_NAME_PATTERNS",
    "SLEEP_REMINDER_PATTERNS",
    "WORK_STATUS_PATTERNS",
)

EXTRACTORS = {
    "BIRTHDAY_PATTERNS": extract_birthday,
    "COMMUNICATION_PATTERNS": extract_communication_preference,
    "FAVORITE_DRINK_PATTERNS": extract_favorite_consumable,
    "FAVORITE_FOOD_PATTERNS": extract_favorite_consumable,
    "LOCATION_PATTERNS": extract_current_location,
    "NICKNAME_PATTERNS": extract_current_nickname,
    "PET_NAME_PATTERNS": extract_pet_name,
    "SLEEP_REMINDER_PATTERNS": extract_sleep_reminder_preference,
    "WORK_STATUS_PATTERNS": extract_current_work_status,
}

EXTRACTORS_BY_FUNC = {
    "extract_birthday": extract_birthday,
    "extract_communication_preference": extract_communication_preference,
    "extract_current_location": extract_current_location,
    "extract_current_nickname": extract_current_nickname,
    "extract_current_work_status": extract_current_work_status,
    "extract_favorite_consumable": extract_favorite_consumable,
    "extract_pet_name": extract_pet_name,
    "extract_sleep_reminder_preference": extract_sleep_reminder_preference,
}

# 表名 -> [(序号, 样例文本, 期望输出, 同时命中的其它模式序号)]
GOLDEN: dict[str, list[tuple[int, str, object, tuple[int, ...]]]] = {
    "BIRTHDAY_PATTERNS": [
        (0, "birthday from 示 to 示.", "示", ()),
        (1, "birthday corrected birthday to 示(", "示", ()),
        (2, "birthday birthday falls on 示,", "示", ()),
        (3, "birthday birthday has been updated to 示,", "示", ()),
        (4, "birthday birthdayis示.", "示", ()),
        (5, "birthday 生日不是示是示", "示", (6, 7)),
        (6, "birthday 生日改成示", "示", (7,)),
        (7, "birthday 生日示", "示", ()),
    ],
    "COMMUNICATION_PATTERNS": [
        (0, "建议 updated communication preference to want 示 without", "示", ()),
        (1, "建议 updated their communication preference to want 示 without", "示", ()),
        (2, "建议 updated communication preference  to prefer 示 without", "示", ()),
        (3, "建议 communication preference updated to want 示 without", "示", ()),
        (4, "建议 communication preference updated to prefer 示 without", "示", ()),
        (5, "建议 communication preference updated:wants 示 without", "示", ()),
        (6, "建议 communication preference is for 示 without", "示", ()),
        (7, "建议 communication preference is to receive 示 without", "示", ()),
        (8, "建议 communication preference has been updated to want 示 without", "示", ()),
        (9, "建议 communication preference has been updated to prefer 示 without", "示", ()),
        (10, "建议 communication preference has been updated: they now want 示 without", "示", ()),
        (11, "建议 prefers that when 示 before giving", "that when 示", (16,)),
        (12, "建议 prefers 示advice without", "示advice", ()),
        (13, "建议 prefers 示suggestions without", "示suggestions", ()),
        (14, "建议 now wants 示 without", "示", ()),
        (15, "建议 now wants 示 instead of", "示", (14,)),
        (16, "建议 prefers 示 before", "示", ()),
        (17, "建议 communication preference:示.", "示", ()),
        (18, "建议 沟通偏好想要示不需要", "示", (19, 20)),
        (19, "建议 沟通偏好示", "示", ()),
        (20, "建议 希望示不需要", "示", ()),
    ],
    "FAVORITE_DRINK_PATTERNS": [
        (0, "preferred drink now prefers 示 instead of favorite drink", ("drink", "示"), ()),
        (
            1,
            "preferred drink beverage preference changed from 示 to 示 and no longer",
            ("drink", "示"),
            (),
        ),
        (2, "preferred drink beverage preference from 示 to 示 and no longer", ("drink", "示"), ()),
        (3, "preferred drink changed drink preference from 示 to 示.", ("drink", "示"), ()),
        (4, "preferred drink changed their drink preference from 示 to 示.", ("drink", "示"), ()),
        (5, "preferred drink drink preference changed to 示.", ("drink", "示"), ()),
        (6, "preferred drink drink preference updated to 示 only", ("drink", "示"), ()),
        (7, "preferred drink switched drink preference from 示 to 示,", ("drink", "示"), ()),
        (8, "preferred drink switched their drink preference from 示 to 示,", ("drink", "示"), ()),
        (9, "preferred drink prefers 示 as their favorite drink", ("drink", "示"), ()),
        (
            10,
            "preferred drink switched from drinking 示 to 示 as their preferred beverage",
            ("drink", "示"),
            (13,),
        ),
        (11, "preferred drink switched from drinking 示 to preferring 示.", ("drink", "示"), ()),
        (
            12,
            "preferred drink switched from regularly drinking 示 to preferring 示 as their daily beverage",
            ("drink", "示"),
            (),
        ),
        (
            13,
            "preferred drink switched from 示 to 示 as their preferred beverage",
            ("drink", "示"),
            (),
        ),
        (
            14,
            "preferred drink switched from 示 to 示 as their preferred drink",
            ("drink", "示"),
            (),
        ),
        (15, "preferred drink prefers 示 over 示 and no longer drink", ("drink", "示"), ()),
        (16, "preferred drink no longer drink 示, only 示.", ("drink", "示"), ()),
        (17, "preferred drink no longer drinking 示, only 示.", ("drink", "示"), ()),
        (18, "preferred drink favorite drinkis示.", ("drink", "示"), ()),
        (19, "preferred drink drink preferenceis示.", ("drink", "示"), ()),
        (20, "preferred drink beverage preferenceis示.", ("drink", "示"), ()),
        (21, "preferred drink 饮品改成示", ("drink", "示"), (22,)),
        (22, "preferred drink 最喜欢喝示", ("drink", "示"), ()),
    ],
    "FAVORITE_FOOD_PATTERNS": [
        (0, "preferred food now prefers 示 instead of favorite food", ("food", "示"), (2,)),
        (1, "preferred food prefers 示 over food", ("food", "示"), ()),
        (2, "preferred food prefers 示 instead of food", ("food", "示"), ()),
        (3, "preferred food food preference changed from 示 to 示.", ("food", "示"), ()),
        (
            4,
            "preferred food switched from eating 示 to 示 as their preferred food",
            ("food", "示"),
            (),
        ),
        (
            5,
            "preferred food switched from eating 示 to 示 as their favorite food",
            ("food", "示"),
            (),
        ),
        (6, "preferred food changed food preference from 示 to 示.", ("food", "示"), ()),
        (7, "preferred food switched food preference from 示 to 示.", ("food", "示"), ()),
        (8, "preferred food switched their food preference from 示 to 示.", ("food", "示"), ()),
        (9, "preferred food food preference changed to 示.", ("food", "示"), ()),
        (10, "preferred food prefers 示 over 示 and no longer eat", ("food", "示"), ()),
        (11, "preferred food no longer eat 示, only 示.", ("food", "示"), ()),
        (12, "preferred food no longer eating 示, only 示.", ("food", "示"), ()),
        (13, "preferred food favorite foodis示.", ("food", "示"), ()),
        (14, "preferred food food preferenceis示.", ("food", "示"), ()),
        (15, "preferred food 食物改成示", ("food", "示"), (16,)),
        (16, "preferred food 最喜欢吃示", ("food", "示"), ()),
    ],
    "LOCATION_PATTERNS": [
        (0, "live from 阿阿 to 阿阿", "阿阿", ()),
        (1, "live moved to 阿阿", "阿阿", ()),
        (2, "live before moving to 阿阿", "阿阿", ()),
        (3, "live before relocating to 阿阿", "阿阿", ()),
        (4, "live previously resided in 阿阿 before relocating to 阿阿", "阿阿", (3,)),
        (5, "live relocated to 阿阿", "阿阿", ()),
        (6, "live live in 阿阿", "阿阿", ()),
        (7, "live 搬到阿阿", "阿阿", ()),
    ],
    "NICKNAME_PATTERNS": [
        (0, "called preferred name from 示 to 阿阿", "阿阿", ()),
        (1, "called preferred name from 阿阿 to 阿阿", "阿阿", (0, 2)),
        (2, "called from 阿阿 to 阿阿", "阿阿", ()),
        (3, "called name to 阿阿", "阿阿", ()),
        (4, "called name should be 阿阿", "阿阿", ()),
        (5, "called name is 阿阿", "阿阿", ()),
        (6, "called prefers to be called 阿阿", "阿阿", ()),
        (7, "called should be called 阿阿", "阿阿", ()),
        (8, "called addressed as 阿阿", "阿阿", ()),
        (9, "called 改成阿阿", "阿阿", ()),
        (10, "called 叫我阿阿", "阿阿", ()),
        (11, "called 别人叫我阿阿", "阿阿", (10,)),
        (12, "called user called 阿阿", "阿阿", ()),
        (13, "called call me 阿阿", "阿阿", ()),
    ],
    "PET_NAME_PATTERNS": [
        (0, "The cat from 阿 to 阿", ("cat", "阿"), ()),
        (1, "cat was initially named 阿 but corrected to 阿", ("cat", "阿"), ()),
        (2, "cat character named 阿", ("cat", "阿"), ()),
        (3, "cat name is 阿", ("cat", "阿"), ()),
        (4, "cat named 阿", ("cat", "阿"), ()),
        (5, "猫不叫阿叫阿", ("cat", "阿"), ()),
        (6, "猫叫阿", ("cat", "阿"), ()),
        (7, "养了猫叫阿", ("cat", "阿"), (6,)),
    ],
    "SLEEP_REMINDER_PATTERNS": [
        (
            0,
            "sleep reminders now okay with 示sleep reminder instead",
            "okay with 示sleep reminder",
            (),
        ),
        (
            1,
            "sleep reminders now accepts gentle reminders to go to sleep,",
            "accepts gentle reminders to go to sleep",
            (),
        ),
        (
            2,
            "sleep reminders now accepts gentle reminders to sleep,",
            "accepts gentle reminders to sleep",
            (),
        ),
        (
            3,
            "sleep reminders now accepts gentle reminders about going to sleep,",
            "accepts gentle reminders about going to sleep",
            (),
        ),
        (
            4,
            "sleep reminders accepts gentle reminders about sleep and",
            "accepts gentle reminders about sleep",
            (),
        ),
        (
            5,
            "sleep reminders nighttime routine preference to now accept gentle sleep reminders,",
            "now accept gentle sleep reminders",
            (),
        ),
        (
            6,
            "sleep reminders can now accept gentle reminders to go to sleep,",
            "can now accept gentle reminders to go to sleep",
            (),
        ),
        (
            7,
            "sleep reminders can now accept gentle reminders about going to sleep.",
            "can now accept gentle reminders about going to sleep",
            (),
        ),
        (8, "sleep reminders sleep reminder preference:示.", "示", ()),
        (9, "sleep reminders dislikes 示.", "示", ()),
        (10, "sleep reminders 可以温和提醒睡觉", "okay with gentle sleep reminders", ()),
        (11, "sleep reminders 不喜欢催睡觉", "dislikes sleep reminders", ()),
    ],
    "WORK_STATUS_PATTERNS": [
        (0, "current work accepted 示offer示 and is no longer", "accepted 示offer示", (4,)),
        (1, "current work work status from to 示.", "示", ()),
        (2, "current work current work status:示.", "示", ()),
        (3, "current work stopped evaluating 示 after accepting 示offer,", "accepted 示offer", ()),
        (4, "current work accepted 示offer示.", "accepted 示offer示", (0,)),
        (5, "current work is 示job opportunities.", "示job opportunities", ()),
        (6, "current work 工作状态示", "示", ()),
        (7, "current work 接受示offer", "accepted 示offer", ()),
        (8, "current work 不再评估机会", "", ()),
    ],
}

CASES = [
    (table, index, sample, expected, also)
    for table, entries in GOLDEN.items()
    for index, sample, expected, also in entries
]

# 抽取器 -> [(闸门词, 样例文本, 期望输出)]
MARKER_GOLDEN: dict[str, list[tuple[str, str, object]]] = {
    "extract_communication_preference": [
        ("reassurance", "prefers reassurance advice without", "reassurance advice"),
        ("direct advice", "prefers direct advice without", "direct advice"),
        ("anxious", "now wants anxious support without", "anxious support"),
        ("安慰", "希望得到安慰", "安慰"),
        ("说教", "希望得到说教", "说教"),
    ],
    "extract_current_location": [
        ("住", "现在住在杭州", "杭州"),
    ],
    "extract_current_nickname": [
        ("prefers to be addressed as", "prefers to be addressed as 阿阿", "阿阿"),
        ("称呼", "称呼我阿阿", "阿阿"),
    ],
    "extract_current_work_status": [
        (
            "evaluating offers",
            "stopped evaluating offers after accepting 工程师 offer.",
            "accepted 工程师 offer",
        ),
        ("accepted an offer", "accepted an offer.", "accepted an offer"),
        ("换工作", "换工作状态是工程师", "工程师"),
    ],
    "extract_favorite_consumable": [
        ("no longer drinks", "no longer drinks coffee, only tea.", ("drink", "tea")),
        ("饮料", "饮料是茶", ("drink", "茶")),
        ("no longer eats", "no longer eats pizza, only sushi.", ("food", "sushi")),
    ],
    "extract_sleep_reminder_preference": [
        ("reminded to sleep", "reminded to sleep, dislikes being reminded.", "being reminded"),
        ("睡眠提醒", "不喜欢睡眠提醒", "dislikes sleep reminders"),
    ],
}

MARKER_CASES = [
    (extractor, marker, sample, expected)
    for extractor, entries in MARKER_GOLDEN.items()
    for marker, sample, expected in entries
]


@pytest.mark.parametrize(
    ("table", "index", "sample", "expected", "also_matches"),
    CASES,
    ids=[f"{table}[{index}]" for table, index, *_ in CASES],
)
def test_the_pattern_still_matches_its_sample_and_extracts_the_same_value(
    table: str,
    index: int,
    sample: str,
    expected: object,
    also_matches: tuple[int, ...],
) -> None:
    """词表里这一条没被改坏，且抽取器对它的输出没变。"""
    patterns = getattr(_vocabulary, table)
    pattern = patterns[index]
    assert re.search(pattern, sample, flags=re.IGNORECASE), (
        f"{table}[{index}] 不再命中它的 golden 样例：{pattern!r}"
    )
    assert EXTRACTORS[table](sample) == expected


@pytest.mark.parametrize(
    ("extractor", "marker", "sample", "expected"),
    MARKER_CASES,
    ids=[f"{marker}" for _extractor, marker, *_ in MARKER_CASES],
)
def test_the_gate_marker_still_lets_the_slot_through(
    extractor: str,
    marker: str,
    sample: str,
    expected: object,
) -> None:
    """闸门词还在表里，且带着它的样例仍能抽出槽位值。"""
    assert marker.lower() in sample.lower()
    assert EXTRACTORS_BY_FUNC[extractor](sample) == expected


def test_every_pattern_has_a_golden_sample() -> None:
    """词表加了模式却没配样例 → 红（否则那条措辞没人钉）。"""
    covered = {(table, index) for table, index, *_ in CASES}
    for table in PATTERN_TABLES:
        patterns = getattr(_vocabulary, table)
        for index in range(len(patterns)):
            assert (table, index) in covered, f"{table}[{index}] 没有 golden 样例"


def test_every_gate_marker_is_covered_by_a_sample() -> None:
    """闸门词加了却没有任何样例带它 → 红（那条闸门就没人钉）。"""
    samples = [sample for _table, _index, sample, _expected, _also in CASES]
    samples += [sample for _extractor, _marker, sample, _expected in MARKER_CASES]
    haystack = [sample.lower() for sample in samples]
    marker_tables = (
        "COMMUNICATION_MARKERS",
        "FAVORITE_DRINK_MARKERS",
        "FAVORITE_DRINK_MARKERS_CN",
        "FAVORITE_FOOD_MARKERS",
        "FAVORITE_FOOD_MARKERS_CN",
        "LOCATION_MARKERS",
        "NICKNAME_MARKERS",
        "SLEEP_REMINDER_MARKERS",
        "WORK_STATUS_MARKERS",
    )
    for table in marker_tables:
        for marker in getattr(_vocabulary, table):
            assert any(marker.lower() in text for text in haystack), (
                f"{table} 里的闸门词 {marker!r} 没有任何 golden 样例带它"
            )
