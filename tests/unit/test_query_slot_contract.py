"""查询侧槽位与上下文词表的契约（domain/slots/query_slots.py 进变异范围后的补齐）。

这个模块一半是逻辑、一半是措辞表：

- 逻辑：槽位优先级、第一人称共现门禁、中文 n-gram 窗口、上下文准入的早退分支
- 措辞表：中英别名表、英文/中文停用词表、各个槽位的 marker 列表

措辞表在变异审计里占了绝大多数变异体（每条表项 2~4 种改写）。与其把每个
变异体登记成「覆盖缺口」，不如把每一条表项用一个例子钉死 —— 措辞被改写、
被删掉、被大小写动过，用例立刻红。

所以下面的用例分两类：表项逐条钉死（表驱动），以及几处逻辑边界。
"""

from __future__ import annotations

import pytest

from thinkback.domain.slots.query_slots import (
    context_terms,
    memory_context_allowed,
    query_conflict_slots,
    query_is_broad_memory_request,
)

# ---------------------------------------------------------------------------
# 别名表：中英同义归一到同一个词
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("marker", "expected"),
    [
        ("猫", {"cat"}),
        ("cat", {"cat"}),
        ("狗", {"dog"}),
        ("dog", {"dog"}),
        ("鸟", {"bird"}),
        ("鹦鹉", {"bird", "鹦鹉"}),
        ("bird", {"bird"}),
        ("parrot", {"bird", "parrot"}),
        ("兔", {"rabbit"}),
        ("兔子", {"rabbit", "兔子"}),
        ("rabbit", {"rabbit"}),
        ("bunny", {"bunny", "rabbit"}),
        ("咖啡", {"coffee", "咖啡"}),
        ("coffee", {"coffee"}),
        ("茶", {"tea"}),
        ("tea", {"tea"}),
        ("饮品", {"drink", "饮品"}),
        ("饮料", {"drink", "饮料"}),
        ("drink", {"drink"}),
        ("食物", {"food", "食物"}),
        ("food", {"food"}),
        ("工作", {"job", "工作"}),
        ("job", {"job"}),
        ("offer", {"job", "offer"}),
        ("生日", {"birthday", "生日"}),
        ("birthday", {"birthday"}),
        ("建议", {"advice", "建议"}),
        ("advice", {"advice"}),
        ("沟通", {"communication", "沟通"}),
        # "communication" 里含着 "cat" —— 别名表按子串匹配，这是个已知的
        # 串扰。写死是为了让改动显式：要修就得连这里一起改。
        ("communication", {"cat", "communication"}),
        ("睡觉", {"sleep", "睡觉"}),
        ("sleep", {"sleep"}),
        ("城市", {"city", "城市"}),
        ("city", {"city"}),
    ],
)
def test_every_alias_maps_to_its_canonical_term(marker: str, expected: set[str]) -> None:
    """别名表逐条钉死：一个别名只能归一到它自己那一个词。

    归一错了，两边就对不上：记忆里写「咖啡」、query 里问「coffee」，词表
    没有把两边并到同一个词 → 共享上下文词判定失败 → 相关记忆被挡在
    上下文包外，助手答不出。
    """
    assert context_terms(marker) == expected


# ---------------------------------------------------------------------------
# 停用词表：高频词不带语义，留下只会制造假交集
# ---------------------------------------------------------------------------

_ENGLISH_STOP_WORDS = [
    "the",
    "and",
    "for",
    "with",
    "that",
    "this",
    "user",
    "their",
    "they",
    "them",
    "what",
    "when",
    "where",
    "which",
    "did",
    "does",
    "has",
    "have",
    "mention",
    "mentioned",
    "recently",
    "current",
    "memory",
]


def test_the_english_stop_words_are_dropped() -> None:
    """这些词在每句话里都有 —— 留着只会让不相干的记忆凑出假交集。"""
    assert context_terms(" ".join(_ENGLISH_STOP_WORDS)) == set()


@pytest.mark.parametrize(
    "stop_word",
    [
        "用户",
        "最近",
        "提到",
        "提过",
        "聊过",
        "什么",
        "是否",
        "喜欢",
        "了吗",
        "哪天",
        "现在",
        "当前",
    ],
)
def test_the_chinese_stop_words_are_stripped_before_ngramming(stop_word: str) -> None:
    """中文停用词在建 n-gram 之前要摘掉。

    不摘的话「用户」会变成一个二元词，于是「用户喜欢喝咖啡」和「他住杭州」
    因为都含「用户」而共享上下文词 → 不相关的记忆混进上下文包。
    """
    assert context_terms(stop_word) == set()


# ---------------------------------------------------------------------------
# 槽位 marker：每条 marker 都要能把提问送进它那一档槽位
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "expected_slot"),
    [
        # 宠物名的提问门禁（叫什么 / 名字 / 叫啥 / name / called）
        ("我的猫叫什么", "pet_name:cat"),
        ("我的猫的名字", "pet_name:cat"),
        ("我的猫叫啥", "pet_name:cat"),
        ("my cat name", "pet_name:cat"),
        ("what is my cat called", "pet_name:cat"),
        # 宠物种类
        ("我的猫叫什么", "pet_name:cat"),
        ("my cat name", "pet_name:cat"),
        ("我的狗叫什么", "pet_name:dog"),
        ("my dog name", "pet_name:dog"),
        ("我的鸟叫什么", "pet_name:bird"),
        ("我的鹦鹉叫什么", "pet_name:bird"),
        ("my bird name", "pet_name:bird"),
        ("my parrot name", "pet_name:bird"),
        ("我的兔叫什么", "pet_name:rabbit"),
        ("我的兔子叫什么", "pet_name:rabbit"),
        ("my rabbit name", "pet_name:rabbit"),
        ("my bunny name", "pet_name:rabbit"),
        # 昵称
        ("我该怎么称呼", "preferred_nickname"),
        ("what is my nickname", "preferred_nickname"),
        ("what is my preferred name", "preferred_nickname"),
        ("怎么叫用户", "preferred_nickname"),
        ("以后叫我小明", "preferred_nickname"),
        ("how do you address me", "preferred_nickname"),
        # 位置
        ("我住哪", "current_location"),
        ("我住在杭州", "current_location"),
        ("我搬到上海", "current_location"),
        ("我搬去上海", "current_location"),
        ("what is my location", "current_location"),
        ("where do i live", "current_location"),
        ("i live in hangzhou", "current_location"),
        ("current 城市", "current_location"),
        # 工作状态
        ("我的工作状态如何", "current_work_status"),
        ("work status", "current_work_status"),
        ("换工作", "current_work_status"),
        ("job status", "current_work_status"),
        ("换工作进展如何", "current_work_status"),
        ("what is my job status", "current_work_status"),
        # 沟通偏好
        ("用户的沟通偏好是什么", "communication_preference"),
        ("what is my communication preference", "communication_preference"),
        ("what is my advice preference", "communication_preference"),
        ("我该怎么沟通比较好", "communication_preference"),
        ("how should you advise me", "communication_preference"),
        # 生日
        ("他的生日是", "birthday"),
        ("我的生日哪天", "birthday"),
        ("我的生日是什么时候", "birthday"),
        ("我的生日几号", "birthday"),
        ("我的生日日期", "birthday"),
        ("what is the birthday", "birthday"),
        # 饮品偏好
        ("我喜欢喝什么", "favorite:drink"),
        ("推荐饮品", "favorite:drink"),
        ("推荐饮料", "favorite:drink"),
        ("what is my favorite drink", "favorite:drink"),
        ("what is my preferred drink", "favorite:drink"),
        ("my drink preference", "favorite:drink"),
        ("my beverage preference", "favorite:drink"),
        # 食物偏好
        ("我喜欢吃什么", "favorite:food"),
        ("我最喜欢食物", "favorite:food"),
        ("what is my favorite food", "favorite:food"),
        ("my food preference", "favorite:food"),
        # 睡眠提醒偏好
        ("你提醒睡觉吗", "sleep_reminder_preference"),
        ("你会催睡觉吗", "sleep_reminder_preference"),
        ("睡眠提醒偏好", "sleep_reminder_preference"),
        ("睡觉偏好", "sleep_reminder_preference"),
        ("sleep reminder preference", "sleep_reminder_preference"),
    ],
)
def test_every_marker_sends_the_question_into_its_slot(query: str, expected_slot: str) -> None:
    """每条 marker 都要能把提问送进它那一档槽位。

    少认一条 → 用户问「我最喜欢喝什么」不落槽位 → 召回不做槽位回填 →
    用户明明说过喜欢喝什么，助手却看不到那条事实。
    """
    assert expected_slot in query_conflict_slots(query)


@pytest.mark.parametrize(
    "query",
    [
        "用户提过什么",
        "用户提到什么",
        "用户聊过什么",
        "用户记得什么",
        "重要信息",
        "what did the user mention",
        "what has the user mentioned",
        "anything important",
        "important information",
        "recent important",
    ],
)
def test_every_broad_request_marker_is_recognized(query: str) -> None:
    """宽泛请求的 marker 逐条钉死。

    漏认一条 → 「用户提过什么」这类提问只召回一条记忆，答非所问。
    """
    assert query_is_broad_memory_request(query) is True


# ---------------------------------------------------------------------------
# 逻辑边界
# ---------------------------------------------------------------------------


def test_the_chinese_ngram_window_is_two_to_four() -> None:
    """中文 n-gram 取 2~4 字。

    窗口开大了词表会膨胀（召回变慢、假交集变多），开小了长词就切不出来
    （「芯片整合」会被切成「芯片」+「整合」，与「整合方案」判成两件事）。
    """
    assert context_terms("芯片整合方案") == {
        "合方",
        "合方案",
        "整合",
        "整合方",
        "整合方案",
        "方案",
        "片整",
        "片整合",
        "片整合方",
        "芯片",
        "芯片整",
        "芯片整合",
    }


def test_an_uppercase_question_still_finds_its_slot() -> None:
    """提问是大写也要认 —— 归一化在比较之前做，不能因为大小写丢槽位。"""
    assert "current_location" in query_conflict_slots("WHERE DO I LIVE")


def test_a_memory_object_is_read_through_its_memory_text() -> None:
    """记忆是对象时，取它的 memory_text 字段，不是把对象本身转成字符串。

    生产路径传进来的就是记忆对象；只按字符串测的话，这条分支从没被走到过。
    """

    class _Memory:
        memory_text = "用户喜欢咖啡"

    assert memory_context_allowed(_Memory(), "他的咖啡口味怎么样") is True


def test_a_trailing_punctuation_does_not_break_the_compact_match() -> None:
    """紧凑匹配靠的是去掉首尾标点 —— 「换工作？」也要算「换工作」。

    去掉的必须是标点，不是空白：空白本来就该被 strip 掉，标点才需要显式列。
    """
    assert "current_work_status" in query_conflict_slots("换工作？")


def test_a_trailing_letter_does_not_make_it_a_compact_work_status() -> None:
    """紧凑匹配的判定是精确的「换工作」两个字 —— 后面多一个字就不是了。"""
    assert "current_work_status" not in query_conflict_slots("换工作X")


def test_a_time_question_without_a_birthday_is_not_a_birthday_slot() -> None:
    """「什么时候有空」不是问生日。

    生日槽位要「生日」和时间问法**同时**出现：只判其一的话，「什么时候有空」
    这种日常问句也会把用户的生日塞进召回。
    """
    assert "birthday" not in query_conflict_slots("什么时候有空")


def test_the_birthday_slot_needs_only_the_time_marker() -> None:
    """「生日」加上时间问法就要落槽位 —— 五个时间问法各自都得认。"""
    assert "birthday" in query_conflict_slots("他生日什么时候")


def test_a_birthday_party_question_is_not_a_birthday_slot() -> None:
    """「生日派对」是别人的派对，不是用户的生日。

    命中生日槽位就会把用户的生日那条事实塞进召回 —— 答非所问。
    """
    assert "birthday" not in query_conflict_slots("when is the birthday party")


def test_every_pet_name_marker_present_still_asks_for_the_pet_name() -> None:
    """提问里同时出现所有宠物名问法时，仍要判成「在问宠物名」。

    这条钉的是「任一 marker 命中即可」的逻辑 —— 写成「所有 marker 都命中」
    就反了。
    """
    assert "pet_name:cat" in query_conflict_slots("我的猫叫什么名字叫啥 called named")


def test_every_birthday_marker_present_still_asks_for_the_birthday() -> None:
    """生日的五个问法同时出现时，仍要判成「在问生日」。"""
    assert "birthday" in query_conflict_slots("我的生日哪天什么时候几号日期是")
