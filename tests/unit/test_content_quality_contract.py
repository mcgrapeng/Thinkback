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

import pytest

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


def test_novelty_is_zero_when_the_content_normalizes_to_nothing() -> None:
    """去掉标点就没剩什么的内容 → 新颖性记 0.0，不是「这一维没分」。

    ``score_content`` 只对**空字符串**提前返回；「!!!」这种内容会一路走到
    新颖性这一维，归一化后是空的。这条分支以前只有一个杀手：hypothesis 的
    ``texts`` 策略**碰巧**生成过纯标点串 —— 概率性的。于是这个变异体
    （``value=0.0`` 写成 ``None``）在审计里的结论是随机的：同一棵 mutants/
    树、同一条命令，三次复跑里一次判存活、两次判被杀。

    写死一个确定输入，这条分支才有确定的结论。
    """
    score = score_content("!!!")

    assert score.dimension(NOVELTY) == 0.0
    assert score.reasons[NOVELTY] == ("空内容",)


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


def test_self_containedness_adds_up_two_penalties_in_one_dimension() -> None:
    """同一个维度里的多个扣分项要**叠加**，不能互相覆盖。

    「那个东西不见了」同时踩了两个扣分项：句首悬空指代（-0.60）与没有主语
    （-0.15）。写成 ``penalty = 0.15``（覆盖而不是累加）的话，分数会从
    0.25 变成 0.85 —— 一条明显依赖上下文的记忆被当成能独立读懂，
    排序键也跟着错。
    """
    score = score_content("那个东西不见了")

    assert score.dimension(SELF_CONTAINEDNESS) == pytest.approx(1.0 - 0.60 - 0.15)


def test_self_containedness_adds_up_a_conversation_reference_too() -> None:
    """「句首指代」与「引用对话之外的内容」两个扣分项也要叠加。

    「他之前说过的」同时踩了 -0.60 与 -0.25 两项 → 0.15。
    第二项写成覆盖而不是累加的话，分数会从 0.15 变成 0.75 ——
    一条纯靠上下文的话被当成能独立读懂。
    """
    score = score_content("他之前说过的")

    assert score.dimension(SELF_CONTAINEDNESS) == pytest.approx(1.0 - 0.60 - 0.25)


# ---------------------------------------------------------------------------
# 扣分项**叠加**（变异审计暴露的覆盖缺口）
# ---------------------------------------------------------------------------
#
# 每个维度的扣分项写的是 ``penalty += x``。写成 ``penalty = x`` 的话，只有
# 最后一项生效，前面的全被覆盖 —— 一条踩了 4 个坑的正文只扣了最后一个坑的分，
# 质量分虚高，被排进上下文。上面的用例各自只踩一两个坑，看不出这一点。


def test_the_default_reason_appears_when_a_dimension_has_no_penalties() -> None:
    """没有扣分项时补一条默认说明 —— 调用方要看得出「这一项为什么不扣」。

    ``if not reasons: reasons.append(...)`` 写成 ``if reasons`` 的话，
    干净的正文反而没有说明、有扣分的正文反而多一条「干净」的说明 ——
    分值不变，但解释是反的，运维看报告会读反。
    """
    score = score_content("用户每天早上六点去健身房锻炼，坚持了三年")

    assert score.reasons[CLARITY] == ("长度与表达都干净",)
    assert score.reasons[SELF_CONTAINEDNESS] == ("可以脱离对话独立理解",)
    assert score.reasons[VERIFIABILITY] == ("既无客观标记也无模糊限定词，取中性 0.5",)


def test_clarity_penalties_accumulate_rather_than_overwrite() -> None:
    """踩满 4 个坑时每一项都要扣：偏短 + 口头禅 + 连续重复 + 格式噪音。

    期望值照公式算：``1 - (0.25 + 0.12*1 + 0.20 + 0.15) = 0.28``。
    写成 ``penalty = x`` 的话只剩最后一项（0.15），值会是 0.85。
    """
    score = score_content("呃呃呃呃\t")

    assert score.dimension(CLARITY) == pytest.approx(1.0 - 0.25 - 0.12 - 0.20 - 0.15)


def test_self_containedness_penalties_accumulate() -> None:
    """引用对话 + 没有主语 + 过短：三项各扣各的。

    期望值照公式算：``1 - (0.25*1 + 0.15 + 0.30) = 0.30``。
    写成 ``penalty = x`` 的话只剩最后一项（0.30），值会是 0.70。

    正文刻意**不**以指代词开头 —— 那样会再扣 0.60 并被 clamp 到 0，
    累加与覆盖就看不出差别了。
    """
    score = score_content("之前说的")

    assert score.dimension(SELF_CONTAINEDNESS) == pytest.approx(1.0 - 0.25 - 0.15 - 0.30)


def test_the_leading_reference_penalty_also_accumulates() -> None:
    """句首指代（-0.60）与引用对话（-0.25）叠加：``1 - 0.85 = 0.15``。

    只踩「句首指代」一项时看不出覆盖：``penalty += 0.60`` 与
    ``penalty = 0.60`` 同值。这一条钉的是**两项都踩**的场景。
    """
    score = score_content("然后之前说的我")

    assert score.dimension(SELF_CONTAINEDNESS) == pytest.approx(1.0 - 0.60 - 0.25)


# ---------------------------------------------------------------------------
# 相似度工具（内容去重 / 新颖性判定的地基）
# ---------------------------------------------------------------------------


def test_similarity_normalization_folds_case() -> None:
    """归一化必须对大小写免疫 —— 否则同一句话的两种写法判成两条不同事实。

    断言的是**输出是小写**，不只是「两种写法结果相同」：把 ``.lower()`` 换成
    ``.upper()``，两种写法的结果照样相同（都是大写），比较不出差别 ——
    但下游按小写比对的口径就全反了。
    """
    from thinkback.domain.content_quality import _normalize_for_similarity

    assert _normalize_for_similarity("ABC Def") == "abcdef"
    assert _normalize_for_similarity("abc def") == "abcdef"


def test_bigrams_of_empty_and_single_char() -> None:
    """空串 → 空集；单字符 → 它自己。两个边界都要对。

    ``{text} if text else set()`` 的真值判据写反（恒真/恒假），这两个边界
    就会错：空串给出 ``{""}``、单字符给出空集。
    """
    from thinkback.domain.content_quality import _bigrams

    assert _bigrams("") == set()
    assert _bigrams("a") == {"a"}


def test_jaccard_of_two_empty_sets_is_zero_not_one() -> None:
    """两侧皆空记 0（不是 1）：两条空内容不等于「完全相同」。

    写成 1.0 的话，两条空内容会被当成完全重复 —— 新颖性判定会把一条
    真实事实判成「与已有内容一模一样」而降权。
    """
    from thinkback.domain.content_quality import _jaccard

    assert _jaccard(set(), set()) == 0.0
