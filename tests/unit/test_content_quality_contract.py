"""质量打分的**精确值**契约（domain/content_quality.py 进变异范围后的补齐）。

`test_content_quality.py` 钉的是方向（「具体的比含糊的高」「短的扣分」）。
方向对了，阈值写错也照样红 —— 比如把「锚点出现次数上限 4」写成 3、把
「引用引用最多扣两次」写成 3 次、把长度甜区的 200 字写成 180 字，
方向都没变，但选出来的记忆变了。

所以这里的用例把**公式的每个常数钉死**：期望值写成与实现同形的表达式
（``0.3 * 1 + 0.1 * min(5, 4)``），常数被改成任何别的值，断言立刻不成立。

公式出处是各维度 docstring 里写明的那一条；期望值不是抄实现输出，
是照公式算出来的 —— 实现与公式不符时，这里会红。
"""

from __future__ import annotations

from thinkback.domain.content_quality import QualityDimension, score_content

SPECIFICITY = QualityDimension.SPECIFICITY
SELF_CONTAINEDNESS = QualityDimension.SELF_CONTAINEDNESS
VERIFIABILITY = QualityDimension.VERIFIABILITY
EMOTIONAL_SALIENCE = QualityDimension.EMOTIONAL_SALIENCE
CLARITY = QualityDimension.CLARITY
NOVELTY = QualityDimension.NOVELTY


def _plain(length: int) -> str:
    """长度精确、且不触发口头禅 / 连续重复 / 格式噪音的正文。"""
    base = "我养了一只英短猫，名字叫麻薯，"
    return (base * (length // len(base) + 1))[:length]


# --- 具体性：0.3 × 类别数 + 0.1 × min(出现次数, 4) ----------------------


def test_specificity_scores_categories_and_occurrences() -> None:
    """1 类 3 处 → 0.3×1 + 0.1×3。类别项与次数项各自独立。"""
    score = score_content("2024 年 3 月我在杭州的阿里巴巴公司")

    assert score.dimension(SPECIFICITY) == 0.3 * 2 + 0.1 * 3


def test_specificity_counts_at_most_four_occurrences() -> None:
    """锚点出现次数上限 4：第 5 处开始不再加分。"""
    four = score_content("3 个 5 人 7 天 9 元 2 万")
    five = score_content("3 个 5 人 7 天 9 元 2 万 6 岁")

    assert four.dimension(SPECIFICITY) == 0.3 * 1 + 0.1 * min(4, 4)
    assert five.dimension(SPECIFICITY) == 0.3 * 1 + 0.1 * min(5, 4)
    assert five.dimension(SPECIFICITY) == four.dimension(SPECIFICITY)


def test_specificity_is_zero_without_any_anchor() -> None:
    score = score_content("他最近不太好")

    assert score.dimension(SPECIFICITY) == 0.0


# --- 自足性：扣 0.60 / 0.25×min(引用数, 2) / 0.15 / 0.30 ----------------


def test_self_containedness_penalizes_a_leading_reference() -> None:
    """句首悬空指代扣 0.60。"""
    score = score_content("他最近不太好")

    assert score.dimension(SELF_CONTAINEDNESS) == 1.0 - 0.60


def test_self_containedness_counts_at_most_two_conversation_references() -> None:
    """指向过往对话的引用每次扣 0.25，最多扣两次（0.50）。"""
    two = score_content("之前说的项目和上次提到的报价单")
    three = score_content("之前说的项目和上次提到的报价单还有刚才聊的方案")

    assert two.dimension(SELF_CONTAINEDNESS) == 1.0 - 0.25 * min(2, 2) - 0.15
    assert three.dimension(SELF_CONTAINEDNESS) == 1.0 - 0.25 * min(3, 2) - 0.15


def test_self_containedness_penalizes_a_missing_subject() -> None:
    """没有明确主语扣 0.15。"""
    score = score_content("2024 年 3 月入职杭州阿里巴巴")

    assert score.dimension(SELF_CONTAINEDNESS) == 1.0 - 0.15


def test_self_containedness_penalizes_content_shorter_than_five_characters() -> None:
    """过短（< 5 字）扣 0.30；与「没有主语」叠加。"""
    score = score_content("猫")

    assert score.dimension(SELF_CONTAINEDNESS) == 1.0 - 0.15 - 0.30


# --- 可验证性：0.5 + 0.1×min(客观标记, 3) − 0.15×min(模糊词, 3) ---------


def test_verifiability_counts_at_most_three_objective_markers() -> None:
    """客观标记每个加 0.1，最多加三次（0.30）。"""
    score = score_content("2024 年 3 月入职杭州阿里巴巴")

    assert score.dimension(VERIFIABILITY) == 0.5 + 0.1 * min(6, 3)


def test_verifiability_counts_at_most_three_hedges() -> None:
    """模糊限定词每个扣 0.15，最多扣三次（0.45）。"""
    three = score_content("我好像有点不太舒服，可能是最近压力太大了")
    four = score_content("我好像有点不太舒服，可能是最近压力太大了，大概吧，或者说不清楚")

    assert three.dimension(VERIFIABILITY) == 0.5 - 0.15 * min(3, 3)
    assert four.dimension(VERIFIABILITY) == 0.5 - 0.15 * min(4, 3)


def test_verifiability_is_neutral_without_markers() -> None:
    score = score_content("他最近不太好")

    assert score.dimension(VERIFIABILITY) == 0.5


# --- 情感显著性：min(1.0, 0.5 × 不同情绪词数) ---------------------------


def test_emotional_salience_scores_each_distinct_word_once() -> None:
    """同一种情绪重复出现只计一次。"""
    once = score_content("我今天很开心")
    twice = score_content("我今天很开心很开心")

    assert once.dimension(EMOTIONAL_SALIENCE) == 0.5 * 1
    assert twice.dimension(EMOTIONAL_SALIENCE) == 0.5 * 1


def test_emotional_salience_caps_at_one() -> None:
    """两种情绪即封顶（0.5×2 = 1.0），更多不再加分。"""
    two = score_content("我今天很开心，但是有点难过")

    assert two.dimension(EMOTIONAL_SALIENCE) == 0.5 * 2


def test_emotional_salience_is_zero_without_emotion_words() -> None:
    score = score_content("我搬到杭州工作")

    assert score.dimension(EMOTIONAL_SALIENCE) == 0.0


# --- 清晰度：长度甜区 + 口头禅 + 重复字符 + 格式噪音 ---------------------


def test_clarity_penalizes_content_shorter_than_four_characters() -> None:
    score = score_content(_plain(3))

    assert score.dimension(CLARITY) == 1.0 - 0.60


def test_clarity_penalizes_content_between_four_and_eight_characters() -> None:
    """4 字（不含）到 8 字（不含）扣 0.25。"""
    assert score_content(_plain(4)).dimension(CLARITY) == 1.0 - 0.25
    assert score_content(_plain(7)).dimension(CLARITY) == 1.0 - 0.25


def test_clarity_does_not_penalize_eight_characters() -> None:
    assert score_content(_plain(8)).dimension(CLARITY) == 1.0


def test_clarity_penalizes_content_longer_than_two_hundred_characters() -> None:
    """200 字整不扣，201 字起扣 0.25。"""
    assert score_content(_plain(200)).dimension(CLARITY) == 1.0
    assert score_content(_plain(201)).dimension(CLARITY) == 1.0 - 0.25


def test_clarity_penalizes_content_longer_than_four_hundred_characters() -> None:
    """400 字整仍是 0.25 档，401 字起扣 0.45（取更严的一档，不叠加）。"""
    assert score_content(_plain(400)).dimension(CLARITY) == 1.0 - 0.25
    assert score_content(_plain(401)).dimension(CLARITY) == 1.0 - 0.45


def test_clarity_counts_at_most_three_fillers() -> None:
    """口头禅每个扣 0.12，最多扣三次（0.36）。"""
    three = score_content("嗯那个呃额我养了一只猫")
    five = score_content("嗯那个呃额就是说反正一只猫")

    assert three.dimension(CLARITY) == 1.0 - 0.12 * min(3, 3)
    assert five.dimension(CLARITY) == 1.0 - 0.12 * min(5, 3)


def test_clarity_penalizes_repeated_characters() -> None:
    score = score_content("我养了猫猫猫猫，名字叫麻薯。")

    assert score.dimension(CLARITY) == 1.0 - 0.20


def test_clarity_penalizes_format_noise() -> None:
    score = score_content("我养了一只猫\t名字叫麻薯")

    assert score.dimension(CLARITY) == 1.0 - 0.15


# --- 新颖性：1.0 − 与 peers 的最大 Jaccard 相似度 ------------------------


def test_novelty_is_zero_for_an_identical_peer() -> None:
    content = "我养了一只英短猫"

    assert score_content(content, peers=(content,)).dimension(NOVELTY) == 1.0 - 1.0


def test_novelty_is_one_minus_the_maximum_jaccard_similarity() -> None:
    """与最近似 peer 的二元组 Jaccard 取反：6/7 交集 → 1 − 6/7。"""
    content = "我养了一只英短猫"
    near = "我养了一只英短"

    assert score_content(content, peers=(near,)).dimension(NOVELTY) == 1.0 - 6 / 7


def test_novelty_uses_the_maximum_similarity_across_peers() -> None:
    """取**最大**相似度（最像的那条），不是平均、也不是最小。"""
    content = "我养了一只英短猫"
    near = "我养了一只英短"
    far = "我上周去了一趟北京。"

    assert score_content(content, peers=(far, near)).dimension(NOVELTY) == 1.0 - 6 / 7
    assert score_content(content, peers=(far,)).dimension(NOVELTY) > 1.0 - 6 / 7


def test_novelty_is_one_without_peers() -> None:
    assert score_content("我养了一只英短猫").dimension(NOVELTY) == 1.0


# --- 边界：这些差一个字符就红 --------------------------------------------


def test_self_containedness_treats_five_characters_as_long_enough() -> None:
    """过短阈值是「< 5 字」：正好 5 字不扣分（写成 <= 5 就多扣 0.30）。"""
    score = score_content("我养了猫咪")

    assert len("我养了猫咪") == 5
    assert score.dimension(SELF_CONTAINEDNESS) == 1.0


def test_novelty_skips_peers_that_normalize_to_nothing() -> None:
    """归一化后为空的 peer 不参与比对，但**不能**因此中断后面的比对。"""
    content = "我养了一只猫"

    score = score_content(content, peers=("!!!", content))

    assert score.dimension(NOVELTY) == 0.0


def test_novelty_strips_punctuation_before_comparing() -> None:
    """归一化会去掉标点：带句号的正文与不带句号的正文应当视为同一条。"""
    score = score_content("我养了一只猫。", peers=("我养了一只猫",))

    assert score.dimension(NOVELTY) == 0.0
