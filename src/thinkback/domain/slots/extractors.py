"""记忆/源文本侧的槽位抽取器。

8 个 P0 槽位的正则抽取器，输入是记忆文本或对话源文本，输出槽位值。
全部为纯函数；模式表与 `Thinkback记忆三层架构` §0 的槽位词表对应。
"""

from __future__ import annotations

import re

from thinkback.domain.slots._vocabulary import (
    BIRTHDAY_PATTERNS,
    COMMUNICATION_MARKERS,
    COMMUNICATION_PATTERNS,
    FAVORITE_DRINK_MARKERS,
    FAVORITE_DRINK_MARKERS_CN,
    FAVORITE_DRINK_PATTERNS,
    FAVORITE_FOOD_MARKERS,
    FAVORITE_FOOD_MARKERS_CN,
    FAVORITE_FOOD_PATTERNS,
    LOCATION_MARKERS,
    LOCATION_PATTERNS,
    LOCATION_STOPWORDS,
    NICKNAME_MARKERS,
    NICKNAME_PATTERNS,
    PET_NAME_PATTERNS,
    SLEEP_REMINDER_MARKERS,
    SLEEP_REMINDER_PATTERNS,
    SLEEP_REMINDER_STOPWORDS,
    WORK_STATUS_MARKERS,
    WORK_STATUS_PATTERNS,
)


def extract_current_nickname(memory_text: str) -> str | None:
    patterns = NICKNAME_PATTERNS
    normalized = memory_text.strip()
    lowered = normalized.lower()
    if re.search(r"\b(?:cat|dog)(?:'s)?\s+name\s+is\b", normalized, flags=re.IGNORECASE):
        return None
    if re.search(
        r"\b(?:book|movie|film|song|album|project|meeting|event|article|column|restaurant|place)\s+called\b",
        lowered,
    ):
        return None
    slot_markers = NICKNAME_MARKERS
    if not any(marker in lowered for marker in slot_markers):
        return None
    for pattern in patterns:
        matches = re.findall(pattern, normalized, flags=re.IGNORECASE)
        if matches:
            value = str(matches[-1]).strip("。,.， ")
            value = re.split(r"\s*\(corrected\s+from\b", value, maxsplit=1, flags=re.IGNORECASE)[
                0
            ].strip("。,.， ")
            # R-3：中文口语"叫我小朋吧"会把语气词"吧"一并吞进昵称。
            value = value.rstrip("吧啊呢哦呀嘛嘿哪了的")
            if len(value) < 1:
                continue
            return value
    return None


def extract_pet_name(memory_text: str) -> tuple[str, str] | None:
    normalized = memory_text.strip()
    lowered = normalized.lower()
    kind: str | None = None
    if "cat" in lowered or "猫" in normalized:
        kind = "cat"
    elif "dog" in lowered or "狗" in normalized:
        kind = "dog"
    elif "bird" in lowered or "parrot" in lowered or "鸟" in normalized or "鹦鹉" in normalized:
        kind = "bird"
    elif "rabbit" in lowered or "bunny" in lowered or "兔" in normalized:
        kind = "rabbit"
    if kind is None:
        return None

    patterns = PET_NAME_PATTERNS
    for pattern in patterns:
        matches = re.findall(pattern, normalized, flags=re.IGNORECASE)
        if matches:
            return kind, str(matches[-1]).strip("。,.， ")
    return None


def extract_current_location(memory_text: str) -> str | None:
    normalized = memory_text.strip()
    lowered = normalized.lower()
    if not any(marker in lowered for marker in LOCATION_MARKERS):
        return None
    patterns = LOCATION_PATTERNS
    for pattern in patterns:
        matches = re.findall(pattern, normalized, flags=re.IGNORECASE)
        if matches:
            value = str(matches[-1]).strip("。,.， ")
            value = re.split(r"\s*\(corrected\s+from\b", value, maxsplit=1, flags=re.IGNORECASE)[
                0
            ].strip("。,.， ")
            # R-3：中文来源常见"搬到杭州工作了"式后缀，贪婪捕获会把
            # "工作了"一并吞进地名。按中文停词截断；截断后过短则尝试下一个匹配。
            value = re.split(
                r"(?:工作|上班|生活|居住|定居|了|，|。|,|\.|；|;|在|跟|和)",
                value,
                maxsplit=1,
            )[0].strip("。,.， 的")
            if len(value) < 2:
                continue
            if value.lower() in LOCATION_STOPWORDS:
                continue
            return value
    return None


def extract_current_work_status(memory_text: str) -> str | None:
    normalized = memory_text.strip()
    lowered = normalized.lower()
    markers = WORK_STATUS_MARKERS
    if not any(marker in lowered for marker in markers):
        return None
    patterns = WORK_STATUS_PATTERNS
    for pattern in patterns:
        matches = re.findall(pattern, normalized, flags=re.IGNORECASE)
        if matches:
            match = matches[-1]
            if isinstance(match, tuple):
                match = next((part for part in reversed(match) if part), "")
            value = str(match).strip("。,.， ")
            value = re.split(
                r"\s+(?:and\s+)?(?:is\s+)?no\s+longer\b|\s+and\s+stopped\s+evaluating\b|，?不再|，?不需要",
                value,
                maxsplit=1,
                flags=re.IGNORECASE,
            )[0].strip("。,.， ")
            if (
                value.startswith("a job offer")
                or value.startswith("an offer")
                or ("offer" in value.lower() and not value.lower().startswith("accepted"))
            ):
                value = f"accepted {value}"
            return value
    return None


def extract_communication_preference(memory_text: str) -> str | None:
    normalized = memory_text.strip()
    lowered = normalized.lower()
    markers = COMMUNICATION_MARKERS
    if not any(marker in lowered for marker in markers):
        return None
    patterns = COMMUNICATION_PATTERNS
    for pattern in patterns:
        matches = re.findall(pattern, normalized, flags=re.IGNORECASE)
        if matches:
            value = str(matches[-1]).strip("。,.， ")
            value = re.split(
                r"\s+without\b|\s+instead\s+of\b|，?不需要|，?不要",
                value,
                maxsplit=1,
                flags=re.IGNORECASE,
            )[0].strip("。,.， ")
            return value
    return None


def extract_birthday(memory_text: str) -> str | None:
    normalized = memory_text.strip()
    lowered = normalized.lower()
    if "birthday" not in lowered and "生日" not in normalized:
        return None
    patterns = BIRTHDAY_PATTERNS
    for pattern in patterns:
        matches = re.findall(pattern, normalized, flags=re.IGNORECASE)
        if matches:
            value = str(matches[-1]).strip("。,.， ")
            value = re.split(
                r"\s*\(corrected\s+from\b|\s*\(not\b|\s*\(previously\s+thought\s+to\s+be\b|\s*\(previously\s+stated\s+as\b|,\s*previously\b|,\s*not\b|,\s*correcting\s+a\s+previous\b|,\s*corrected\s+from\b",
                value,
                maxsplit=1,
                flags=re.IGNORECASE,
            )[0].strip("。,.， ")
            return value
    return None


def extract_favorite_consumable(memory_text: str) -> tuple[str, str] | None:
    normalized = memory_text.strip()
    lowered = normalized.lower()
    kind: str | None = None
    if any(marker in lowered for marker in FAVORITE_DRINK_MARKERS) or any(
        marker in normalized for marker in FAVORITE_DRINK_MARKERS_CN
    ):
        kind = "drink"
    elif any(marker in lowered for marker in FAVORITE_FOOD_MARKERS) or any(
        marker in normalized for marker in FAVORITE_FOOD_MARKERS_CN
    ):
        kind = "food"
    if kind is None:
        return None

    patterns: tuple[str, ...] = (
        FAVORITE_DRINK_PATTERNS if kind == "drink" else FAVORITE_FOOD_PATTERNS
    )
    for pattern in patterns:
        matches = re.findall(pattern, normalized, flags=re.IGNORECASE)
        if matches:
            value = str(matches[-1]).strip("。,.， ")
            value = re.split(
                r",?\s+no\s+longer\s+drinking\b|,?\s+no\s+longer\s+drinks\b|,?\s+no\s+longer\s+eating\b|,?\s+no\s+longer\s+eats\b|\s+and\s+no\s+longer\s+drinks\b|\s+and\s+no\s+longer\s+eats\b|\s+and\s+stopped\s+drinking\b|\s+and\s+stopped\s+eating\b|，?不再",
                value,
                maxsplit=1,
                flags=re.IGNORECASE,
            )[0].strip("。,.， ")
            # R-6：中文来源"最喜欢喝的美式咖啡"会把结构助词"的"吞进饮品名。
            value = value.lstrip("的")
            if not value:
                continue
            return kind, value
    return None


def extract_sleep_reminder_preference(memory_text: str) -> str | None:
    normalized = memory_text.strip()
    lowered = normalized.lower()
    markers = SLEEP_REMINDER_MARKERS
    if not any(marker in lowered for marker in markers):
        return None
    patterns = SLEEP_REMINDER_PATTERNS
    for pattern in patterns:
        matches = re.findall(pattern, normalized, flags=re.IGNORECASE)
        if matches:
            value = str(matches[-1]).strip("。,.， ")
            if value in SLEEP_REMINDER_STOPWORDS:
                return "dislikes sleep reminders"
            if "温和" in value:
                return "okay with gentle sleep reminders"
            return value
    return None
