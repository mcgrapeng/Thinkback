"""记忆/源文本侧的槽位抽取器。

8 个 P0 槽位的正则抽取器，输入是记忆文本或对话源文本，输出槽位值。
全部为纯函数；模式表与 `Thinkback记忆三层架构` §0 的槽位词表对应。
"""

from __future__ import annotations

import re


def extract_current_nickname(memory_text: str) -> str | None:
    patterns = (
        r"\bpreferred\s+name\s+from\s+.+?\s+to\s+'?([一-鿿A-Za-z0-9_-]{2,24})",
        r"\bpreferred\s+name\s+from\s+'?[一-鿿A-Za-z0-9_-]{2,24}'?\s+to\s+'?([一-鿿A-Za-z0-9_-]{2,24})'?",
        r"\bfrom\s+[一-鿿A-Za-z0-9_-]{2,24}\s+to\s+([一-鿿A-Za-z0-9_-]{2,24})",
        r"\bname\s+to\s+([一-鿿A-Za-z0-9_-]{2,24})",
        r"\bname\s+should\s+be\s+([一-鿿A-Za-z0-9_-]{2,24})",
        r"\bname\s+is\s+([一-鿿A-Za-z0-9_-]{2,24})",
        r"\bprefers\s+to\s+be\s+(?:called|addressed(?:\s+as)?)\s+'?([一-鿿A-Za-z0-9_-]{2,24})'?",
        r"\bshould\s+be\s+called\s+'?([一-鿿A-Za-z0-9_-]{2,24})'?",
        r"\baddressed\s+as\s+'?([一-鿿A-Za-z0-9_-]{2,24})'?",
        r"(?:改成|改为|更正为|纠正为)\s*([一-鿿A-Za-z0-9_-]{2,24})",
        r"(?:叫我|称呼我)\s*([一-鿿A-Za-z0-9_-]{2,24})",
        r"\b(?:user|they|them|me|my|their|i)\s+(?:should\s+)?(?:be\s+)?called\s+'?([一-鿿A-Za-z0-9_-]{2,24})'?",
        r"\b(?:call|called)\s+(?:me|the user|them)\s+'?([一-鿿A-Za-z0-9_-]{2,24})'?",
    )
    normalized = memory_text.strip()
    lowered = normalized.lower()
    if re.search(r"\b(?:cat|dog)(?:'s)?\s+name\s+is\b", normalized, flags=re.IGNORECASE):
        return None
    if re.search(
        r"\b(?:book|movie|film|song|album|project|meeting|event|article|column|restaurant|place)\s+called\b",
        lowered,
    ):
        return None
    slot_markers = (
        "preferred name",
        "prefers to be called",
        "prefers to be addressed as",
        "name should be",
        "name is",
        "called",
        "addressed as",
        "叫我",
        "称呼",
    )
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

    patterns = (
        r"\bfrom\s+[一-鿿A-Za-z0-9_-]{1,24}\s+to\s+([一-鿿A-Za-z0-9_-]{1,24})",
        r"\b(?:cat|dog)\s+was\s+initially\s+named\s+[一-鿿A-Za-z0-9_-]{1,24}\s+but\s+corrected\s+to\s+([一-鿿A-Za-z0-9_-]{1,24})",
        r"\b(?:cat|dog|bird|parrot|rabbit|bunny)\s+character\s+named\s+([一-鿿A-Za-z0-9_-]{1,24})",
        r"\b(?:cat|dog|bird|parrot|rabbit|bunny)(?:'s)?\s+name\s+is\s+([一-鿿A-Za-z0-9_-]{1,24})",
        r"\b(?:cat|dog|bird|parrot|rabbit|bunny)\s+(?:is\s+)?named\s+([一-鿿A-Za-z0-9_-]{1,24})",
        r"(?:猫|狗|鸟|鹦鹉|兔|兔子)不叫[一-鿿A-Za-z0-9_-]{1,24}[，,]?\s*(?:现在)?(?:叫|名叫|名字叫)\s*([一-鿿A-Za-z0-9_-]{1,24})",
        r"(?:猫|狗|鸟|鹦鹉|兔|兔子)(?:的)?(?:名字)?(?:叫|名叫)\s*([一-鿿A-Za-z0-9_-]{1,24})",
        r"(?:养了|有).*?(?:猫|狗|鸟|鹦鹉|兔|兔子).*?(?:叫|名叫|名字叫)\s*([一-鿿A-Za-z0-9_-]{1,24})",
    )
    for pattern in patterns:
        matches = re.findall(pattern, normalized, flags=re.IGNORECASE)
        if matches:
            return kind, str(matches[-1]).strip("。,.， ")
    return None


def extract_current_location(memory_text: str) -> str | None:
    normalized = memory_text.strip()
    lowered = normalized.lower()
    if not any(
        marker in lowered for marker in ("live", "resided", "moved", "relocated", "住", "搬")
    ):
        return None
    patterns = (
        r"\bfrom\s+[一-鿿A-Za-z0-9_-]{2,40}\s+to\s+([一-鿿A-Za-z0-9_-]{2,40})",
        r"\bmoved\s+to\s+([一-鿿A-Za-z0-9_-]{2,40})",
        r"\bbefore\s+moving\s+to\s+([一-鿿A-Za-z0-9_-]{2,40})",
        r"\bbefore\s+relocating\s+to\s+([一-鿿A-Za-z0-9_-]{2,40})",
        r"\bpreviously\s+resided\s+in\s+[一-鿿A-Za-z0-9_-]{2,40}\s+before\s+relocating\s+to\s+([一-鿿A-Za-z0-9_-]{2,40})",
        r"\brelocated\s+to\s+([一-鿿A-Za-z0-9_-]{2,40})",
        r"\blives?\s+in\s+([一-鿿A-Za-z0-9_-]{2,40})",
        r"(?:搬到|搬去|住在|现在住在)\s*([一-鿿A-Za-z0-9_-]{2,40})",
    )
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
            if value.lower() in {"the", "this", "that", "moment"}:
                continue
            return value
    return None


def extract_current_work_status(memory_text: str) -> str | None:
    normalized = memory_text.strip()
    lowered = normalized.lower()
    markers = (
        "work status",
        "job opportunities",
        "evaluating offers",
        "accepted an offer",
        "current work",
        "stopped evaluating",
        "换工作",
        "工作状态",
        "offer",
    )
    if not any(marker in lowered for marker in markers):
        return None
    patterns = (
        r"\b(user\s+)?accepted\s+(.+?offer.+?)(?:\s+and\s+is\s+no\s+longer\b|[.;。]|$)",
        r"(?:work status|job status|current work status).*?\bfrom\b.+?\bto\b\s+(.+?)(?:[.;。]|$)",
        r"\bcurrent\s+work\s+status:\s*(.+?)(?:[.;。]|$)",
        r"\bstopped\s+evaluating\s+.+?\s+after\s+accepting\s+(.+?offer)(?:,|[.;。]|$)",
        r"\baccepted\s+(.+?offer.+?)(?:[.;。]|$)",
        r"\bis\s+(.+?job opportunities)(?:[.;。]|$)",
        r"(?:工作状态|现在)\s*(?:是|为)?\s*([^。,.，]+)",
    )
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
    markers = (
        "communication preference",
        "reassurance",
        "direct advice",
        "advice",
        "suggestions",
        "anxious",
        "沟通偏好",
        "建议",
        "安慰",
        "说教",
    )
    if not any(marker in lowered for marker in markers):
        return None
    patterns = (
        r"\bupdated\s+communication\s+preference\s+to\s+want\s+(.+?)(?:\s+without\b|\s+instead\s+of\b|[.;。]|$)",
        r"\bupdated\s+their\s+communication\s+preference\s+to\s+want\s+(.+?)(?:\s+without\b|\s+instead\s+of\b|[.;。]|$)",
        r"\bupdated\s+communication\s+preference\s+.+?\bto\s+prefer\s+(.+?)(?:\s+without\b|\s+instead\s+of\b|[.;。]|$)",
        r"\bcommunication\s+preference\s+updated\s+to\s+want\s+(.+?)(?:\s+without\b|\s+instead\s+of\b|[.;。]|$)",
        r"\bcommunication\s+preference\s+updated\s+to\s+prefer\s+(.+?)(?:\s+without\b|\s+instead\s+of\b|[.;。]|$)",
        r"\bcommunication\s+preference\s+updated:\s*wants\s+(.+?)(?:\s+without\b|\s+instead\s+of\b|[.;。]|$)",
        r"\bcommunication\s+preference\s+is\s+for\s+(.+?)(?:\s+without\b|\s+instead\s+of\b|[.;。]|$)",
        r"\bcommunication\s+preference\s+is\s+to\s+receive\s+(.+?)(?:\s+without\b|\s+instead\s+of\b|[.;。]|$)",
        r"\bcommunication\s+preference\s+has\s+been\s+updated\s+to\s+want\s+(.+?)(?:\s+without\b|\s+instead\s+of\b|[.;。]|$)",
        r"\b(?:user's\s+)?communication\s+preference\s+has\s+been\s+updated\s+to\s+prefer\s+(.+?)(?:\s+without\b|\s+instead\s+of\b|[.;。]|$)",
        r"\bcommunication\s+preference\s+has\s+been\s+updated:\s+they\s+now\s+want\s+(.+?)(?:\s+without\b|\s+instead\s+of\b|[.;。]|$)",
        r"\bprefers\s+(that\s+when\s+.+?)(?:\s+before\s+giving\b|[.;。]|$)",
        r"\bprefers\s+(.+?advice)(?:\s+without\b|\s+instead\s+of\b|[.;。]|$)",
        r"\bprefers\s+(.+?suggestions)(?:\s+without\b|\s+instead\s+of\b|[.;。]|$)",
        r"\bnow\s+wants\s+(.+?)(?:\s+without\b|\s+instead\s+of\b|[.;。]|$)",
        r"\bnow\s+wants\s+(.+?)\s+instead\s+of\b",
        r"\bprefers\s+(.+?)\s+before\b",
        r"\bcommunication preference:\s*(.+?)(?:[.;。]|$)",
        r"(?:沟通偏好).*?(?:现在)?(?:更)?(?:想要|希望|要|偏好|喜欢)\s*([^。,.，]+?)(?:，?不需要|，?不要|，?不想|[。,.，]|$)",
        r"(?:沟通偏好|希望你|想要你)\s*([^。,.，]+)",
    )
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
    patterns = (
        r"\bfrom\s+.+?\s+to\s+(.+?)(?:[.;。]|$)",
        r"\bcorrected\s+(?:their\s+)?birthday\s+to\s+(.+?)(?:\s*\(|,|\s+not\b|[.;。]|$)",
        r"\bbirthday\s+falls\s+on\s+(.+?)(?:,|\s+correcting\b|[.;。]|$)",
        r"\bbirthday\s+has\s+been\s+updated\s+to\s+(.+?)(?:,|\s+not\b|[.;。]|$)",
        r"\bbirthday\s*(?:is|:)\s*(.+?)(?:[.;。]|$)",
        r"(?:生日|出生日期).*?不是[^。,.，]+[，,]?\s*(?:是|改成|改为|更正为|纠正为)\s*([^。,.，]+)",
        r"(?:生日|出生日期).*?(?:改成|改为|更正为|纠正为|(?<!不)是)\s*([^。,.，]+)",
        r"(?:生日|出生日期)(?:是|为|:|：)?\s*([^。,.，]+)",
    )
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
    if any(
        marker in lowered
        for marker in frozenset(
            (
                "favorite drink",
                "preferred drink",
                "drink preference",
                "beverage preference",
                "preferred beverage",
                "beverage preference changed",
                "switched from drinking",
                "changed drink preference",
                "changed their drink preference",
                "drink preference changed",
                "drink preference updated",
                "switched drink preference",
                "switched their drink preference",
                "preferring",
                "no longer drinks",
                "no longer drinking",
            )
        )
    ) or any(marker in normalized for marker in ("喜欢喝", "饮品", "饮料")):
        kind = "drink"
    elif any(
        marker in lowered
        for marker in frozenset(
            (
                "favorite food",
                "preferred food",
                "food preference",
                "changed food preference",
                "food preference changed",
                "switched food preference",
                "switched their food preference",
                "prefers",
                "no longer eats",
                "no longer eating",
            )
        )
    ) or any(marker in normalized for marker in ("喜欢吃", "食物")):
        kind = "food"
    if kind is None:
        return None

    patterns: tuple[str, ...]
    if kind == "drink":
        patterns = (
            r"\bnow\s+prefers\s+(.+?)\s+instead\s+of\b.+?\bfavorite\s+drink\b",
            r"\bbeverage\s+preference\s+changed\s+from\s+.+?\s+to\s+(.+?)(?:\s+and\s+no\s+longer\b|[.;。]|$)",
            r"\bbeverage\s+preference\s+from\s+.+?\s+to\s+(.+?)(?:\s+and\s+no\s+longer\b|[.;。]|$)",
            r"\bchanged\s+drink\s+preference\s+from\s+.+?\s+to\s+(.+?)(?:[.;。]|$)",
            r"\bchanged\s+their\s+drink\s+preference\s+from\s+.+?\s+to\s+(.+?)(?:[.;。]|$)",
            r"\bdrink\s+preference\s+changed\s+to\s+(.+?)(?:[.;。]|$)",
            r"\bdrink\s+preference\s+updated\s+to\s+(.+?)(?:\s+only\b|,|\s+and\s+no\s+longer\b|[.;。]|$)",
            r"\bswitched\s+drink\s+preference\s+from\s+.+?\s+to\s+(.+?)(?:,|\s+and\s+no\s+longer\b|[.;。]|$)",
            r"\bswitched\s+their\s+drink\s+preference\s+from\s+.+?\s+to\s+(.+?)(?:,|\s+and\s+no\s+longer\b|[.;。]|$)",
            r"\bprefers\s+(.+?)\s+as\s+their\s+favorite\s+drink\b",
            r"\bswitched\s+from\s+drinking\s+.+?\s+to\s+(.+?)\s+as\s+their\s+preferred\s+beverage\b",
            r"\bswitched\s+from\s+drinking\s+.+?\s+to\s+preferring\s+(.+?)(?:[.;。]|$)",
            r"\bswitched\s+from\s+regularly\s+drinking\s+.+?\s+to\s+preferring\s+(.+?)\s+as\s+their\s+daily\s+beverage\b",
            r"\bswitched\s+from\s+.+?\s+to\s+(.+?)\s+as\s+their\s+preferred\s+beverage\b",
            r"\bswitched\s+from\s+.+?\s+to\s+(.+?)\s+as\s+their\s+preferred\s+drink\b",
            r"\bprefers\s+(.+?)\s+over\s+.+?\s+and\s+no\s+longer\s+drinks?\b",
            r"\bno\s+longer\s+drinks?\s+.+?,\s+only\s+(.+?)(?:[.;。]|$)",
            r"\bno\s+longer\s+drinking\s+.+?,\s+only\s+(.+?)(?:[.;。]|$)",
            r"\bfavorite\s+drink\s*(?:is|:)\s*(.+?)(?:[.;。]|$)",
            r"\bdrink\s+preference\s*(?:is|:|to)\s*(.+?)(?:[.;。]|$)",
            r"\bbeverage\s+preference\s*(?:is|:|to)\s*(.+?)(?:[.;。]|$)",
            r"(?:饮品|饮料).*?(?:偏好)?(?:改成|改为|更正为|纠正为|是|为|:|：)\s*([^。,.，]+)",
            r"(?:最喜欢喝|喜欢喝|饮品|饮料)(?:是|为|:|：)?\s*([^。,.，]+)",
        )
    else:
        patterns = (
            r"\bnow\s+prefers\s+(.+?)\s+instead\s+of\b.+?\bfavorite\s+food\b",
            r"\bprefers\s+(.+?)\s+over\b.+?\bfood\b",
            r"\bprefers\s+(.+?)\s+instead\s+of\b.+?\bfood\b",
            r"\bfood\s+preference\s+changed\s+from\s+.+?\s+to\s+(.+?)(?:[.;。]|$)",
            r"\bswitched\s+from\s+eating\s+.+?\s+to\s+(.+?)\s+as\s+their\s+preferred\s+food\b",
            r"\bswitched\s+from\s+eating\s+.+?\s+to\s+(.+?)\s+as\s+their\s+favorite\s+food\b",
            r"\bchanged\s+food\s+preference\s+from\s+.+?\s+to\s+(.+?)(?:[.;。]|$)",
            r"\bswitched\s+food\s+preference\s+from\s+.+?\s+to\s+(.+?)(?:[.;。]|$)",
            r"\bswitched\s+their\s+food\s+preference\s+from\s+.+?\s+to\s+(.+?)(?:[.;。]|$)",
            r"\bfood\s+preference\s+changed\s+to\s+(.+?)(?:[.;。]|$)",
            r"\bprefers\s+(.+?)\s+over\s+.+?\s+and\s+no\s+longer\s+eats?\b",
            r"\bno\s+longer\s+eats?\s+.+?,\s+only\s+(.+?)(?:[.;。]|$)",
            r"\bno\s+longer\s+eating\s+.+?,\s+only\s+(.+?)(?:[.;。]|$)",
            r"\bfavorite\s+food\s*(?:is|:)\s*(.+?)(?:[.;。]|$)",
            r"\bfood\s+preference\s*(?:is|:|to)\s*(.+?)(?:[.;。]|$)",
            r"(?:食物).*?(?:偏好)?(?:改成|改为|更正为|纠正为|是|为|:|：)\s*([^。,.，]+)",
            r"(?:最喜欢吃|喜欢吃|食物)(?:是|为|:|：)?\s*([^。,.，]+)",
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
    markers = (
        "sleep reminder preference",
        "gentle reminders about sleep",
        "sleep reminders",
        "reminded to sleep",
        "reminders to sleep",
        "reminders to go to sleep",
        "gentle sleep reminders",
        "going to sleep",
        "nighttime routine preference",
        "催睡觉",
        "提醒睡觉",
        "睡眠提醒",
    )
    if not any(marker in lowered for marker in markers):
        return None
    patterns = (
        r"\bnow\s+(?:is\s+)?((?:okay|ok|fine)\s+with\s+.+?sleep reminders?)(?:\s+instead\b|[.;。]|$)",
        r"\bnow\s+(accepts\s+gentle\s+reminders\s+to\s+go\s+to\s+sleep)(?:,|\s+representing\b|[.;。]|$)",
        r"\bnow\s+(accepts\s+gentle\s+reminders\s+to\s+sleep)(?:,|\s+representing\b|[.;。]|$)",
        r"\bnow\s+(accepts\s+gentle\s+reminders\s+about\s+going\s+to\s+sleep)(?:,|\s+representing\b|[.;。]|$)",
        r"\b(accepts\s+gentle\s+reminders\s+about\s+sleep)(?:\s+and\b|,|[.;。]|$)",
        r"\bnighttime\s+routine\s+preference\s+to\s+(now\s+accept\s+gentle\s+sleep\s+reminders)(?:,|[.;。]|$)",
        r"\b(can\s+now\s+accept\s+gentle\s+reminders\s+to\s+go\s+to\s+sleep)(?:,|\s+updating\b|[.;。]|$)",
        r"\b(can\s+now\s+accept\s+gentle\s+reminders\s+about\s+going\s+to\s+sleep)(?:[.;。]|$)",
        r"\bsleep reminder preference:\s*(.+?)(?:[.;。]|$)",
        r"\bdislikes\s+(.+?)(?:[.;。]|$)",
        r"(?:可以|接受|愿意).*?(温和.*?(?:提醒睡觉|睡眠提醒))",
        r"(?:不喜欢|讨厌|不要|不需要).*?(催睡觉|提醒睡觉|睡眠提醒)",
    )
    for pattern in patterns:
        matches = re.findall(pattern, normalized, flags=re.IGNORECASE)
        if matches:
            value = str(matches[-1]).strip("。,.， ")
            if value in {"催睡觉", "提醒睡觉", "睡眠提醒"}:
                return "dislikes sleep reminders"
            if "温和" in value:
                return "okay with gentle sleep reminders"
            return value
    return None
