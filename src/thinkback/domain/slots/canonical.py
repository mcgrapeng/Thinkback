"""槽位归类与规范化记忆文本。

- ``memory_conflict_slot``: 记忆文本 → P0 槽位（冲突检测的共用口径）
- ``canonical_memory_text*``: 记忆文本 → 规范化模板文本（冲突比较前的归一）
"""

from __future__ import annotations

import re
from typing import Any

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


def memory_conflict_slot(memory_text: str) -> str | None:
    """记忆文本命中的 P0 槽位；与查询侧 ``query_conflict_slot`` 共用槽位词表。"""
    normalized = canonical_memory_text(memory_text).lower()
    if extract_current_nickname(memory_text):
        return "preferred_nickname"
    nickname_markers = (
        "叫我",
        "称呼",
        "preferred name",
        "prefers to be called",
        "prefers to be addressed as",
        "addressed as",
        "changed preferred name",
    )
    if any(marker in normalized for marker in nickname_markers):
        return "preferred_nickname"
    pet_kind = extract_pet_name(memory_text)
    if pet_kind:
        return f"pet_name:{pet_kind[0]}"
    if extract_current_location(memory_text):
        return "current_location"
    if extract_current_work_status(memory_text):
        return "current_work_status"
    if any(
        marker in normalized
        for marker in (
            "communication preference",
            "reassurance",
            "direct advice",
            "before advice",
            "instead of reassurance",
            "沟通偏好",
            "直接建议",
            "先安慰",
            "说教",
        )
    ):
        return "communication_preference"
    if extract_birthday(memory_text):
        return "birthday"
    favorite = extract_favorite_consumable(memory_text)
    if favorite:
        return f"favorite:{favorite[0]}"
    if extract_sleep_reminder_preference(memory_text):
        return "sleep_reminder_preference"
    return None


def p0_canonical_memories_from_source(source_text: str) -> list[str]:
    """把对话源文本解析成一组规范化 P0 记忆文本（用于源交叉校验）。"""
    canonical: list[str] = []
    nickname = extract_current_nickname(source_text)
    if nickname:
        canonical.append(f"User prefers to be called {nickname}")
    pet_name = extract_pet_name(source_text)
    if pet_name:
        pet_kind, name = pet_name
        canonical.append(f"User has a {pet_kind} named {name}")
    current_location = extract_current_location(source_text)
    if current_location:
        canonical.append(f"User lives in {current_location}")
    work_status = extract_current_work_status(source_text)
    if work_status:
        canonical.append(f"User current work status: {work_status}")
    communication_preference = extract_communication_preference(source_text)
    if communication_preference:
        canonical.append(f"User communication preference: {communication_preference}")
    birthday = extract_birthday(source_text)
    if birthday:
        canonical.append(f"User birthday: {birthday}")
    favorite = extract_favorite_consumable(source_text)
    if favorite:
        kind, value = favorite
        canonical.append(f"User favorite {kind}: {value}")
    sleep_reminder_preference = extract_sleep_reminder_preference(source_text)
    if sleep_reminder_preference:
        canonical.append(f"User sleep reminder preference: {sleep_reminder_preference}")
    return canonical


def canonical_memory_text(memory_text: str) -> str:
    """把 P0 槽位记忆归一成模板文本；非 P0 记忆原样返回。"""
    canonical_nickname = extract_current_nickname(memory_text)
    if canonical_nickname:
        return f"User prefers to be called {canonical_nickname}"
    pet_name = extract_pet_name(memory_text)
    if pet_name:
        pet_kind, name = pet_name
        return f"User has a {pet_kind} named {name}"
    current_location = extract_current_location(memory_text)
    if current_location:
        return f"User lives in {current_location}"
    work_status = extract_current_work_status(memory_text)
    if work_status:
        return f"User current work status: {work_status}"
    communication_preference = extract_communication_preference(memory_text)
    if communication_preference:
        return f"User communication preference: {communication_preference}"
    birthday = extract_birthday(memory_text)
    if birthday:
        return f"User birthday: {birthday}"
    favorite = extract_favorite_consumable(memory_text)
    if favorite:
        kind, value = favorite
        return f"User favorite {kind}: {value}"
    sleep_reminder_preference = extract_sleep_reminder_preference(memory_text)
    if sleep_reminder_preference:
        return f"User sleep reminder preference: {sleep_reminder_preference}"
    return memory_text


def canonical_memory_text_from_source(memory_text: str, l3_metadata: dict[str, Any]) -> str:
    """优先采用源文本侧的规范化文本，避免 mem0 抽取噪声进入比较。"""
    source_canonical = source_canonical_memory_text(memory_text, l3_metadata)
    if source_canonical is not None:
        return source_canonical
    source_text = str(l3_metadata.get("source_text") or "")
    source_pet_name = extract_pet_name(source_text)
    memory_pet_name = extract_pet_name(memory_text)
    if source_pet_name and memory_pet_name and source_pet_name[0] == memory_pet_name[0]:
        pet_kind, name = source_pet_name
        return f"User has a {pet_kind} named {name}"
    return canonical_memory_text(memory_text)


def source_canonical_memory_text(memory_text: str, l3_metadata: dict[str, Any]) -> str | None:
    """源文本侧能给出同槽位规范化文本时返回它，否则返回 None。

    与 :func:`canonical_memory_text_from_source` 的区别是「是否来自源文本」这件事
    本身要能被调用方看到 —— 落库文本的选择依赖它（源文本是权威，mem0 抽取文本是噪声）。
    """
    source_text = str(l3_metadata.get("source_text") or "")
    source_canonical = p0_canonical_memory_for_source_slot(memory_text, source_text)
    if source_canonical is not None:
        return source_canonical
    return None


def p0_canonical_memory_for_source_slot(memory_text: str, source_text: str) -> str | None:
    """若源文本能产出与记忆同槽位的规范化文本，返回该文本。"""
    if not source_text:
        return None
    memory_slot = memory_conflict_slot(memory_text)
    if memory_slot is None:
        return None
    for source_memory_text in p0_canonical_memories_from_source(source_text):
        if memory_conflict_slot(source_memory_text) == memory_slot:
            return source_memory_text
    return None


def count_p0_facts(text: str) -> int:
    """统计文本命中的 P0 槽位事实数量。"""
    facts = 0
    if extract_current_nickname(text):
        facts += 1
    if extract_pet_name(text):
        facts += 1
    if extract_current_location(text):
        facts += 1
    if extract_current_work_status(text):
        facts += 1
    if extract_communication_preference(text):
        facts += 1
    if extract_birthday(text):
        facts += 1
    if extract_favorite_consumable(text):
        facts += 1
    if extract_sleep_reminder_preference(text):
        facts += 1
    return facts


# canonical 折叠掉多少字符算「丢了事实」——经验阈值。
# 单槽位改写（canonical 模板化）通常只差几个字；多事实截断会差几十个字符。
# 阈值来自 2026-10-19 计划的裁定：canonical 比原文短超过 12 个字符即判定丢失事实。
_CANONICAL_LOSS_THRESHOLD = 12


# 「更正/替换」措辞：mem0 抽出的这类文本同时带着**新值和被取代的旧值**，
# canonical 化丢掉的那部分不是「额外事实」而是「陈旧值」，必须丢掉 ——
# 否则召回会把旧值带回来（User moved from Hangzhou to Shanghai → 存原文会让
# 「Hangzhou」继续躺在画像里）。
_CORRECTION_MARKERS = (
    r"\bcorrected\b",
    r"\bcorrects?\b",
    r"\bchanged\b",
    r"\bswitched\b",
    r"\bpreviously\b",
    r"\bformerly\b",
    r"\bno longer\b",
    r"\binstead of\b",
    r"\bused to\b",
    r"\bfrom\b[^.]{1,40}?\bto\b",
    r"改成|改为|更正为|纠正为|不再是|而不是",
)


def is_correction_text(text: str) -> bool:
    """文本是否表达「更正/替换」（同时带着新值与被取代的旧值）。

    与槽位抽取器已有的措辞口径一致：抽取器用 ``matches[-1]`` 取最后一个匹配、
    并专门处理 ``from X to Y`` / ``corrected`` / ``previously`` 这些写法，
    说明这些措辞在 mem0 抽取结果里表示「新值替换旧值」。
    """
    if not text:
        return False
    return any(re.search(marker, text, flags=re.IGNORECASE) for marker in _CORRECTION_MARKERS)


def local_index_memory_text(
    original: str, canonical: str, *, canonical_from_source: bool = False
) -> str:
    """L3 落库文本：canonical 只服务槽位比较，多事实原文必须保留。

    canonical 是单槽位模板，"生日 + 姐姐 Lily 住波士顿" 被折叠成
    "User lives in 波士顿" 时，生日和 Lily 会在 PG 与 mem0 两边同时消失。

    判据按优先级：
    - 原文命中多个 P0 槽位 → 用原文（明确是多事实）
    - 更正事件 → 用 canonical（丢的是陈旧值，留着会污染画像）
    - canonical 来自源文本 → 用 canonical（源文本是权威，mem0 抽取文本是噪声）
    - canonical 比原文短超过阈值 → 用原文（canonical 折叠掉了内容）

    长度判据不能省："搬到杭州 + 2024 年去阿里巴巴上班 + 离开老团队" 只命中
    location 一个槽位，但 canonical 会丢掉 52 个字符的雇主/年份/团队。

    多槽位判据必须排在更正判据**前面**：一条文本可能同时是多事实与更正措辞
    （"生日是 3 月 3 日，她从杭州搬到上海，2024 年去了阿里巴巴"）。
    更正判据在前会把生日、雇主、年份一起丢掉 —— 不可逆。
    陈旧值不会因此复活：冲突仲裁走的是 canonical 的槽位
    （``_supersede_conflicting_memories`` 用 canonical 算 slot），与落库文本无关。
    """
    if not canonical:
        return original
    if not original:
        return canonical
    if count_p0_facts(original) > 1:
        return original
    if is_correction_text(original):
        return canonical
    if canonical_from_source:
        return canonical
    if len(original) - len(canonical) > _CANONICAL_LOSS_THRESHOLD:
        return original
    return canonical
