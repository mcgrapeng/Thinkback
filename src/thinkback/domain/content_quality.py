"""内容质量六维评分（纯领域层，确定性，不依赖 LLM / 网络）。

背景：上下文包（context pack）里每一条待注入的记忆，其"值不值得注入"和
"注入后能撑起多少对话"不是同一件事。一条"他最近不太好"注入进去，模型
既接不上话，也无从核实。本模块把"内容质量"拆成六个可独立解释、可回归
测试的维度，每维 0.0-1.0，加权成总分，供上下文包排序与治理台展示使用。

六个维度（刻意互斥，避免同一信号被重复计分）：

| 维度 | 回答的问题 | 主要信号 |
| --- | --- | --- |
| specificity 具体性 | 有没有可指认的锚点？ | 时间 / 数量 / 专名 / 地点 / 机构 |
| self_containedness 自足性 | 脱离对话能读懂吗？ | 句首悬空代词、指向过往对话的引用 |
| verifiability 可验证性 | 是事实还是主观感受？ | 客观标记 vs 模糊限定词（好像/可能/有点） |
| emotional_salience 情感显著性 | 对陪伴场景有分量吗？ | 情绪词命中 |
| clarity 表达清晰度 | 读起来干净吗？ | 长度甜区、口头禅、重复字符、格式噪音 |
| novelty 新颖性 | 和包里已有条目重复吗？ | 字符二元组 Jaccard 相似度取最大 |

设计约束：
- 纯函数：无 IO、无时钟依赖（时间新鲜度属于 domain/recall_policy.py 的
  strength，不在这里重复计分），同样输入永远同样输出。
- 可解释：每个维度都返回 reasons，说明扣分/加分的具体原因，便于治理台
  展示与人工排查。
- 不代替 LLM 判分：本模块只判"文本自身的信息结构"，不判"这句话对用户
  重不重要"（那属于召回相关性与事实在位性，由召回侧负责）。

权重：具体性与自足性各 0.20（决定这条记忆能不能被模型接住），可验证性、
情感显著性、表达清晰度、新颖性各 0.15。权重之和恒为 1.0（见 DIMENSION_WEIGHTS）。

调用方：
- ``domain/recall_policy.py`` 的召回排序键用 ``intrinsic_quality()``
  （五个内容内在维度，权重重新归一）—— 新颖性是包内相对量，排序时循环依赖。
- 上下文包组装阶段用 ``score_content(content, peers=...)`` 算完整六维。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum


class QualityDimension(StrEnum):
    """内容质量的六个维度。"""

    SPECIFICITY = "specificity"
    SELF_CONTAINEDNESS = "self_containedness"
    VERIFIABILITY = "verifiability"
    EMOTIONAL_SALIENCE = "emotional_salience"
    CLARITY = "clarity"
    NOVELTY = "novelty"


DIMENSION_WEIGHTS: dict[QualityDimension, float] = {
    QualityDimension.SPECIFICITY: 0.20,
    QualityDimension.SELF_CONTAINEDNESS: 0.20,
    QualityDimension.VERIFIABILITY: 0.15,
    QualityDimension.EMOTIONAL_SALIENCE: 0.15,
    QualityDimension.CLARITY: 0.15,
    QualityDimension.NOVELTY: 0.15,
}

INTRINSIC_DIMENSIONS: tuple[QualityDimension, ...] = (
    QualityDimension.SPECIFICITY,
    QualityDimension.SELF_CONTAINEDNESS,
    QualityDimension.VERIFIABILITY,
    QualityDimension.EMOTIONAL_SALIENCE,
    QualityDimension.CLARITY,
)
"""五个"内容自身"维度（不含新颖性）。

新颖性是相对于同包其它条目的**相对量**，在排序时无法定义（要排就得先知道
谁排在前面，循环依赖）。所以排序键用这五维并把权重重新归一；新颖性留给
上下文包组装阶段（那时已经知道包里已选了谁）。
"""

_INTRINSIC_WEIGHT_SUM = sum(DIMENSION_WEIGHTS[dimension] for dimension in INTRINSIC_DIMENSIONS)


@dataclass(frozen=True)
class ContentQualityScore:
    """单条内容的质量评分结果。

    属性：
      dimensions: 每个维度的得分（0.0-1.0）
      reasons: 每个维度的扣分/加分原因（人可读）
      total: 加权总分（0.0-1.0）
    """

    dimensions: dict[QualityDimension, float]
    reasons: dict[QualityDimension, tuple[str, ...]]
    total: float

    def dimension(self, name: QualityDimension) -> float:
        """取单维得分。"""

        return self.dimensions[name]

    def as_dict(self) -> dict[str, object]:
        """转成可 JSON 序列化的普通字典（维度名为 str）。"""

        return {
            "total": self.total,
            "dimensions": {str(key): value for key, value in self.dimensions.items()},
            "reasons": {str(key): list(value) for key, value in self.reasons.items()},
        }


@dataclass(frozen=True)
class _DimensionResult:
    value: float
    reasons: tuple[str, ...] = field(default=())


def _clamp(value: float) -> float:
    """把分数夹到 [0.0, 1.0]（浮点误差也在此收敛）。"""

    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


# --------------------------------------------------------------------------- #
# 词表与正则
# --------------------------------------------------------------------------- #

# 具体性：五类锚点。每类只记"有没有"（类别数），同时记总出现次数。
_TIME_RE = re.compile(
    r"昨天|今天|明天|前天|后天|上周|这周|本周|上个月|下个月|今年|去年|明年|"
    r"周末|工作日|早上|中午|下午|晚上|凌晨|刚才"
)
_QUANTITY_RE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:岁|年|月|日|号|点|分|元|万|次|天|周|个月|人|公里|米|公斤|斤|块|%)"
)
_CJK_QUANTITY_RE = re.compile(
    r"[二三四五六七八九十百千万两]+(?:岁|年|月|日|号|点|元|万|次|公里|公斤|楼)"
)
_PROPER_NAME_RE = re.compile(r"[A-Z][A-Za-z]{2,}")
_DECLARED_NAME_RE = re.compile(r"(?:名字叫|叫|昵称是|名字是)[一-鿿A-Za-z]{1,10}")
_PLACE_RE = re.compile(r"[一-鿿]{2,8}(?:省|市|区|县|镇|村|街|路|城|公寓|小区|地铁站)")
_ORG_RE = re.compile(r"[一-鿿A-Za-z]{2,10}(?:公司|集团|大学|学院|医院|银行|学校|部门|团队|工作室)")

# 自足性：句首悬空指代 / 指向过往对话的引用。
_LEADING_REFERENCE_RE = re.compile(
    r"^\s*(?:他|她|它|他们|她们|这|那|这个|那个|这件事|那件事|上次|昨天|刚才|然后|所以|另外|而且)"
)
_CONVERSATION_REFERENCE_RE = re.compile(
    r"之前说(?:的|过)|上次(?:说|聊|提)|刚才(?:说|聊|提)|前面说|上面提|之前聊|之前提|咱们之前|我们之前"
)
_SUBJECT_RE = re.compile(r"我|你|他|她|它|咱|我们|你们|他们|用户|user|User")

# 可验证性：客观标记 vs 模糊限定词。
_OBJECTIVE_RE = re.compile(
    r"\d|[A-Z][A-Za-z]{2,}|确诊|入职|毕业|搬到|搬去|买了|去了|开始|结束|完成|签了|提交|"
    r"考试|面试|发布|上线|生日|婚礼|去世"
)
_HEDGE_RE = re.compile(
    r"好像|似乎|大概|可能|也许|应该|我觉得|我感觉|貌似|估计|有点|比较|稍微|还行|一般般"
)

# 情感显著性：陪伴场景里有分量的情绪词。
_EMOTION_WORDS: tuple[str, ...] = (
    "开心",
    "高兴",
    "快乐",
    "兴奋",
    "难过",
    "伤心",
    "失落",
    "沮丧",
    "焦虑",
    "紧张",
    "害怕",
    "担心",
    "孤独",
    "寂寞",
    "生气",
    "愤怒",
    "委屈",
    "累",
    "疲惫",
    "压力",
    "崩溃",
    "失望",
    "期待",
    "喜欢",
    "讨厌",
    "想念",
    "感动",
    "平静",
    "放松",
    "满足",
    "感恩",
    "低落",
    "失眠",
    "感谢",
    "舍不得",
    "后悔",
    "尴尬",
    "温暖",
    "治愈",
)

# 表达清晰度：口头禅、重复字符、格式噪音。
_FILLER_WORDS: tuple[str, ...] = (
    "嗯",
    "呃",
    "额",
    "就是说",
    "怎么说呢",
    "反正",
    "然后呢",
    "你知道吗",
    "其实吧",
)
_REPEATED_CHAR_RE = re.compile(r"(.)\1{3,}")
_FORMAT_NOISE_RE = re.compile(r"[\t\r]|https?://|^\s*(?:[-*]\s|\d+[.、]\s)")

_PUNCTUATION_CHARS = " \t\r\n，。！？、；：“”‘’\"'（）()《》「」…—-·,.!?;:[]{}<>~@#$%^&*+=_|/\\"
_PUNCTUATION_RE = re.compile("[" + re.escape(_PUNCTUATION_CHARS) + "]+")


def _normalize_for_similarity(text: str) -> str:
    """相似度比较用的归一化：去掉空白与标点，只留实义字符。"""

    return _PUNCTUATION_RE.sub("", text.lower())


def _bigrams(text: str) -> set[str]:
    """字符二元组集合（中文友好，不需要分词）。"""

    if len(text) < 2:
        return {text} if text else set()
    return {text[index : index + 2] for index in range(len(text) - 1)}


def _jaccard(left: set[str], right: set[str]) -> float:
    """Jaccard 相似度；两侧皆空记 0（不是 1）。"""

    if not left and not right:
        return 0.0
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


# --------------------------------------------------------------------------- #
# 单维度打分
# --------------------------------------------------------------------------- #


def _score_specificity(content: str) -> _DimensionResult:
    """具体性：时间 / 数量 / 专名 / 地点 / 机构 五类锚点。

    计分公式（刻意非线性，先看"有没有"再看"有几个"）：

    score = min(1.0, 0.3 * 命中类别数 + 0.1 * min(锚点出现次数, 4))
    """

    occurrences = 0
    kinds: list[str] = []

    for name, pattern in (
        ("时间", _TIME_RE),
        ("数量", _QUANTITY_RE),
        ("数量", _CJK_QUANTITY_RE),
        ("专名", _PROPER_NAME_RE),
        ("专名", _DECLARED_NAME_RE),
        ("地点", _PLACE_RE),
        ("机构", _ORG_RE),
    ):
        matches = pattern.findall(content)
        if not matches:
            continue
        occurrences += len(matches)
        if name not in kinds:
            kinds.append(name)

    categories = len(kinds)
    value = _clamp(0.3 * categories + 0.1 * min(occurrences, 4))
    reasons: tuple[str, ...]
    if categories == 0:
        reasons = ("没有可指认的时间/数量/专名/地点/机构锚点",)
    else:
        reasons = (f"锚点类别：{'、'.join(kinds)}（{categories} 类，共 {occurrences} 处）",)
    return _DimensionResult(value=value, reasons=reasons)


def _score_self_containedness(content: str) -> _DimensionResult:
    """自足性：脱离对话上下文也能读懂。

    扣分项：
    - 句首悬空指代（"他最近…"）：-0.60
    - 指向过往对话的引用（"之前说过的…"）：每次 -0.25，最多 -0.50
    - 完全没有主语：-0.15
    - 过短（< 5 字）：-0.30
    """

    penalty = 0.0
    reasons: list[str] = []

    if _LEADING_REFERENCE_RE.match(content):
        penalty += 0.60
        reasons.append("句首是指代/承接词，脱离对话读不懂")

    references = _CONVERSATION_REFERENCE_RE.findall(content)
    if references:
        penalty += 0.25 * min(len(references), 2)
        reasons.append(f"引用了对话之外的内容（{len(references)} 处）")

    if not _SUBJECT_RE.search(content):
        penalty += 0.15
        reasons.append("没有明确主语")

    stripped = content.strip()
    if len(stripped) < 5:
        penalty += 0.30
        reasons.append("过短，撑不起独立语义")

    value = _clamp(1.0 - penalty)
    if not reasons:
        reasons.append("可以脱离对话独立理解")
    return _DimensionResult(value=value, reasons=tuple(reasons))


def _score_verifiability(content: str) -> _DimensionResult:
    """可验证性：客观标记越多越好，模糊限定词越多越差。

    score = clamp(0.5 + 0.1 * min(客观标记数, 3) - 0.15 * min(模糊限定词数, 3))
    """

    objective = len(_OBJECTIVE_RE.findall(content))
    hedges = len(_HEDGE_RE.findall(content))
    value = _clamp(0.5 + 0.1 * min(objective, 3) - 0.15 * min(hedges, 3))

    reasons: list[str] = []
    if objective:
        reasons.append(f"含 {objective} 处可核对的客观标记")
    if hedges:
        reasons.append(f"含 {hedges} 处模糊限定词")
    if not reasons:
        reasons.append("既无客观标记也无模糊限定词，取中性 0.5")
    return _DimensionResult(value=value, reasons=tuple(reasons))


def _score_emotional_salience(content: str) -> _DimensionResult:
    """情感显著性：陪伴场景下情绪词的出现密度。

    score = min(1.0, 0.5 * 命中的不同情绪词个数)（每种情绪只计一次）
    """

    hits = [word for word in _EMOTION_WORDS if word in content]
    value = _clamp(0.5 * len(hits))
    reasons = (f"情绪词：{'、'.join(hits)}",) if hits else ("没有情绪信息",)
    return _DimensionResult(value=value, reasons=reasons)


def _score_clarity(content: str) -> _DimensionResult:
    """表达清晰度：长度甜区 + 口头禅 + 重复字符 + 格式噪音。

    扣分项：
    - < 4 字：-0.60；4-7 字：-0.25
    - > 200 字：-0.25；> 400 字：-0.45（取更严的一档，不叠加）
    - 口头禅：每次 -0.12，最多 -0.36
    - 连续重复字符（4 个及以上）：-0.20
    - 制表符 / URL / 列表符号等格式噪音：-0.15
    """

    stripped = content.strip()
    length = len(stripped)
    penalty = 0.0
    reasons: list[str] = []

    if length == 0:
        return _DimensionResult(value=0.0, reasons=("空内容",))

    if length < 4:
        penalty += 0.60
        reasons.append("过短，撑不起一句完整表达")
    elif length < 8:
        penalty += 0.25
        reasons.append("偏短，信息密度不足")

    if length > 400:
        penalty += 0.45
        reasons.append("过长，注入成本高")
    elif length > 200:
        penalty += 0.25
        reasons.append("偏长")

    fillers = [word for word in _FILLER_WORDS if word in content]
    if fillers:
        penalty += 0.12 * min(len(fillers), 3)
        reasons.append(f"口头禅：{'、'.join(fillers)}")

    if _REPEATED_CHAR_RE.search(content):
        penalty += 0.20
        reasons.append("存在连续重复字符")

    if _FORMAT_NOISE_RE.search(content):
        penalty += 0.15
        reasons.append("含制表符/链接/列表等格式噪音")

    value = _clamp(1.0 - penalty)
    if not reasons:
        reasons.append("长度与表达都干净")
    return _DimensionResult(value=value, reasons=tuple(reasons))


def _score_novelty(content: str, peers: Sequence[str]) -> _DimensionResult:
    """新颖性：与 peers（已选入上下文包的条目）的最大相似度取反。

    相似度 = 字符二元组 Jaccard。peers 为空时取 1.0（没有可比对象，
    不构成扣分理由）。
    """

    normalized = _normalize_for_similarity(content)
    if not normalized:
        return _DimensionResult(value=0.0, reasons=("空内容",))
    if not peers:
        return _DimensionResult(value=1.0, reasons=("没有可比对的已有条目",))

    base = _bigrams(normalized)
    best_peer = ""
    best_similarity = 0.0
    for peer in peers:
        peer_normalized = _normalize_for_similarity(peer)
        if not peer_normalized:
            continue
        similarity = _jaccard(base, _bigrams(peer_normalized))
        if similarity > best_similarity:
            best_similarity = similarity
            best_peer = peer

    value = _clamp(1.0 - best_similarity)
    reasons = (f"与已有条目最高相似度 {best_similarity:.2f}：{best_peer[:30]}",)
    return _DimensionResult(value=value, reasons=reasons)


# --------------------------------------------------------------------------- #
# 对外 API
# --------------------------------------------------------------------------- #


def score_content(content: str, *, peers: Sequence[str] = ()) -> ContentQualityScore:
    """给单条内容打六维质量分。

    peers 是上下文包里已经选定的条目，用于算新颖性；为空时新颖度记 1.0。
    """

    if not content.strip():
        # 空内容不参与任何维度：中性分（verifiability 0.5、自足性 0.55）
        # 会让"什么都没有"排在"内容很糙"前面，语义上是错的。
        return ContentQualityScore(
            dimensions=dict.fromkeys(QualityDimension, 0.0),
            reasons=dict.fromkeys(QualityDimension, ("空内容",)),
            total=0.0,
        )

    results: dict[QualityDimension, _DimensionResult] = _intrinsic_results(content)
    results[QualityDimension.NOVELTY] = _score_novelty(content, peers)
    dimensions = {dimension: result.value for dimension, result in results.items()}
    reasons = {dimension: result.reasons for dimension, result in results.items()}
    total = sum(DIMENSION_WEIGHTS[dimension] * value for dimension, value in dimensions.items())
    return ContentQualityScore(dimensions=dimensions, reasons=reasons, total=_clamp(total))


def _intrinsic_results(content: str) -> dict[QualityDimension, _DimensionResult]:
    """五个内容自身维度的打分结果（不含新颖性）。"""

    return {
        QualityDimension.SPECIFICITY: _score_specificity(content),
        QualityDimension.SELF_CONTAINEDNESS: _score_self_containedness(content),
        QualityDimension.VERIFIABILITY: _score_verifiability(content),
        QualityDimension.EMOTIONAL_SALIENCE: _score_emotional_salience(content),
        QualityDimension.CLARITY: _score_clarity(content),
    }


def intrinsic_quality(content: str) -> float:
    """内容内在质量（0.0-1.0，五维权重重新归一），用作召回排序键。

    为什么不含新颖性：新颖性取决于"包里已有哪些条目"，而排序本身决定谁先进包，
    循环依赖。近似重复的过滤由 dedupe 负责，这里只判内容自身能不能被模型接住。
    """

    if not content.strip():
        return 0.0
    results = _intrinsic_results(content)
    weighted = sum(
        DIMENSION_WEIGHTS[dimension] * result.value for dimension, result in results.items()
    )
    return _clamp(weighted / _INTRINSIC_WEIGHT_SUM)


def score_contents(contents: Iterable[str]) -> list[ContentQualityScore]:
    """给一组内容打质量分，新颖性相对前面已出现的条目计算。

    顺序敏感：后面的条目会把前面已入选的内容当作 peers，因此调用方应把
    contents 按期望的注入顺序传入。
    """

    scores: list[ContentQualityScore] = []
    seen: list[str] = []
    for content in contents:
        scores.append(score_content(content, peers=tuple(seen)))
        seen.append(content)
    return scores


def rank_by_quality(contents: Sequence[str]) -> list[int]:
    """按质量分给内容排序，返回下标列表（高分在前）。

    稳定排序：同分保持输入顺序。新颖性按输入顺序累计计算（见 score_contents）。
    """

    scores = score_contents(contents)
    return sorted(range(len(contents)), key=lambda index: -scores[index].total)


def format_quality_report(score: ContentQualityScore) -> str:
    """人可读的一行摘要（治理台 / 日志用）。"""

    parts = [f"{dimension}={score.dimensions[dimension]:.2f}" for dimension in QualityDimension]
    return f"total={score.total:.2f} (" + ", ".join(parts) + ")"
