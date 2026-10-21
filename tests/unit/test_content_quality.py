"""内容质量六维评分的契约测试。

六维评分是纯函数，行为完全确定，测试直接钉住每个维度的扣分/加分规则，
并用 hypothesis 钉住不变量（取值范围、确定性、权重和）。
"""

from __future__ import annotations

import json

from hypothesis import given
from hypothesis import strategies as st

from thinkback.domain.content_quality import (
    DIMENSION_WEIGHTS,
    QualityDimension,
    format_quality_report,
    rank_by_quality,
    score_content,
    score_contents,
)

DIMENSIONS = list(QualityDimension)


def test_every_dimension_is_scored_and_weighted() -> None:
    """六维齐全、权重和为 1，总分 = 加权和。"""
    score = score_content("2024 年 3 月我入职了杭州阿里巴巴云智能团队。")

    assert set(score.dimensions) == set(DIMENSIONS)
    assert set(score.reasons) == set(DIMENSIONS)
    assert sum(DIMENSION_WEIGHTS.values()) == 1.0
    expected = sum(
        DIMENSION_WEIGHTS[dimension] * score.dimensions[dimension] for dimension in DIMENSIONS
    )
    assert score.total == expected
    assert 0.0 <= score.total <= 1.0


def test_specificity_rewards_anchors_not_vagueness() -> None:
    """具体性：时间/数量/专名/地点/机构是锚点，'最近'这种虚指不是。"""
    vague = score_content("他最近不太好")
    concrete = score_content("2024 年 3 月入职杭州阿里巴巴")

    assert vague.dimension(QualityDimension.SPECIFICITY) == 0.0
    assert concrete.dimension(QualityDimension.SPECIFICITY) > vague.dimension(
        QualityDimension.SPECIFICITY
    )
    assert concrete.dimension(QualityDimension.SPECIFICITY) <= 1.0


def test_specificity_caps_at_one() -> None:
    """锚点再多也不超过 1.0。"""
    content = (
        "2024 年 3 月 5 日，我在杭州余杭区的阿里巴巴公司入职，"
        "月薪 30000 元，负责 ToB 业务，团队 20 人。"
    )
    score = score_content(content)

    assert score.dimension(QualityDimension.SPECIFICITY) == 1.0


def test_self_containedness_penalizes_dangling_reference() -> None:
    """自足性：句首悬空代词扣 0.6，指向过往对话的引用另扣。"""
    standalone = score_content("我养了一只英短猫，名字叫麻薯。")
    dangling = score_content("他最近不太好")
    referencing = score_content("之前说过的那个项目，我还在等回复。")

    assert standalone.dimension(QualityDimension.SELF_CONTAINEDNESS) == 1.0
    assert dangling.dimension(QualityDimension.SELF_CONTAINEDNESS) == 0.4
    assert referencing.dimension(QualityDimension.SELF_CONTAINEDNESS) < 1.0
    assert any(
        "引用" in reason for reason in referencing.reasons[QualityDimension.SELF_CONTAINEDNESS]
    )


def test_verifiability_favors_facts_over_hedges() -> None:
    """可验证性：客观标记加分，模糊限定词扣分。"""
    factual = score_content("2024 年 3 月入职阿里巴巴")
    hedged = score_content("我好像有点不太舒服，可能是最近压力太大了")

    assert factual.dimension(QualityDimension.VERIFIABILITY) > hedged.dimension(
        QualityDimension.VERIFIABILITY
    )
    assert hedged.dimension(QualityDimension.VERIFIABILITY) < 0.5


def test_emotional_salience_counts_distinct_words_once() -> None:
    """情感显著性：每种情绪只计一次。"""
    once = score_content("我今天很开心")
    twice = score_content("我今天很开心很开心")
    both = score_content("我今天很开心，但是有点难过")

    assert once.dimension(QualityDimension.EMOTIONAL_SALIENCE) == 0.5
    assert twice.dimension(QualityDimension.EMOTIONAL_SALIENCE) == 0.5
    assert both.dimension(QualityDimension.EMOTIONAL_SALIENCE) == 1.0
    assert score_content("我搬到杭州工作").dimension(QualityDimension.EMOTIONAL_SALIENCE) == 0.0


def test_clarity_penalizes_fillers_repetition_and_noise() -> None:
    """表达清晰度：口头禅、重复字符、格式噪音各扣分。"""
    clean = score_content("我养了一只英短猫，名字叫麻薯，特别黏人。")
    filler = score_content("嗯，那个，我养了一只英短猫，名字叫麻薯。")
    repeated = score_content("我养了猫猫猫猫，名字叫麻薯。")
    noisy = score_content("我养了一只猫\t名字叫麻薯")

    assert clean.dimension(QualityDimension.CLARITY) == 1.0
    assert filler.dimension(QualityDimension.CLARITY) < 1.0
    assert repeated.dimension(QualityDimension.CLARITY) < 1.0
    assert noisy.dimension(QualityDimension.CLARITY) < 1.0


def test_clarity_penalizes_too_short_and_too_long() -> None:
    """清晰度：过短/过长都扣分，且都有下限。"""
    short = score_content("猫")
    long = score_content("我" + "喜欢猫" * 200)

    assert short.dimension(QualityDimension.CLARITY) < 0.5
    assert long.dimension(QualityDimension.CLARITY) < 1.0
    assert long.dimension(QualityDimension.CLARITY) > 0.0


def test_novelty_is_relative_to_peers() -> None:
    """新颖性：与已有条目越像越低。"""
    content = "我养了一只英短猫，名字叫麻薯。"
    identical = score_content(content, peers=(content,))
    different = score_content(content, peers=("我上周去了一趟北京。",))
    alone = score_content(content)

    assert identical.dimension(QualityDimension.NOVELTY) == 0.0
    assert different.dimension(QualityDimension.NOVELTY) > 0.8
    assert alone.dimension(QualityDimension.NOVELTY) == 1.0


def test_empty_content_scores_zero() -> None:
    """空内容全部维度 0 分，总分 0。"""
    score = score_content("")

    assert score.total == 0.0
    assert all(value == 0.0 for value in score.dimensions.values())


def test_scoring_is_deterministic() -> None:
    """同输入必同输出（无时钟、无随机）。"""
    first = score_content("我最近有点焦虑，晚上总是失眠。")
    second = score_content("我最近有点焦虑，晚上总是失眠。")

    assert first.dimensions == second.dimensions
    assert first.total == second.total
    assert first.reasons == second.reasons


def test_as_dict_is_json_serializable() -> None:
    """as_dict 输出可 JSON 序列化（治理台 / API 用）。"""
    payload = score_content("我养了一只英短猫，名字叫麻薯。").as_dict()

    assert json.loads(json.dumps(payload, ensure_ascii=False))["total"] >= 0.0
    assert set(payload["dimensions"]) == {str(dimension) for dimension in DIMENSIONS}


def test_rank_by_quality_puts_informative_content_first() -> None:
    """排序：信息密度高、自足性好的内容排在前面。"""
    contents = [
        "他最近不太好",
        "2024 年 3 月入职杭州阿里巴巴云智能团队，负责 ToB 业务方向",
    ]

    order = rank_by_quality(contents)

    assert order[0] == 1


def test_score_contents_is_order_dependent_on_novelty() -> None:
    """批量打分：新颖性相对前面已出现的条目累计。"""
    scores = score_contents(["我养了一只英短猫，名字叫麻薯。"] * 2)

    assert scores[0].dimension(QualityDimension.NOVELTY) == 1.0
    assert scores[1].dimension(QualityDimension.NOVELTY) == 0.0


def test_format_quality_report_mentions_all_dimensions() -> None:
    """可读摘要包含总分与六个维度。"""
    report = format_quality_report(score_content("我养了一只英短猫，名字叫麻薯。"))

    assert report.startswith("total=")
    for dimension in DIMENSIONS:
        assert str(dimension) in report


texts = st.text(max_size=200)


@given(text=texts)
def test_dimensions_stay_in_unit_range(text: str) -> None:
    """不变量：任何输入下每个维度与总分都在 [0, 1]。"""
    score = score_content(text)

    assert all(0.0 <= value <= 1.0 for value in score.dimensions.values())
    assert 0.0 <= score.total <= 1.0


@given(text=texts)
def test_scoring_is_pure(text: str) -> None:
    """不变量：打分是纯函数。"""
    assert score_content(text).dimensions == score_content(text).dimensions


@given(text=texts, peers=st.lists(texts, max_size=3))
def test_novelty_never_negative(text: str, peers: list[str]) -> None:
    """不变量：新颖性恒在 [0, 1]，与 peers 无关地不越界。"""
    score = score_content(text, peers=tuple(peers))

    assert 0.0 <= score.dimension(QualityDimension.NOVELTY) <= 1.0


def test_intrinsic_quality_excludes_novelty_and_is_renormalized() -> None:
    """排序用的内在质量：不含新颖性，权重重新归一到 [0, 1]。"""
    from thinkback.domain.content_quality import INTRINSIC_DIMENSIONS, intrinsic_quality

    content = "2024 年 3 月我入职了杭州阿里巴巴云智能团队"
    score = score_content(content)
    expected = sum(
        DIMENSION_WEIGHTS[dimension] * score.dimensions[dimension]
        for dimension in INTRINSIC_DIMENSIONS
    ) / sum(DIMENSION_WEIGHTS[dimension] for dimension in INTRINSIC_DIMENSIONS)

    assert QualityDimension.NOVELTY not in INTRINSIC_DIMENSIONS
    assert intrinsic_quality(content) == expected
    assert 0.0 <= intrinsic_quality(content) <= 1.0


def test_intrinsic_quality_of_empty_content_is_zero() -> None:
    """空内容内在质量为 0（不占排序优势）。"""
    from thinkback.domain.content_quality import intrinsic_quality

    assert intrinsic_quality("") == 0.0
    assert intrinsic_quality("   ") == 0.0
