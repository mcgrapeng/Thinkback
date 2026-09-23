"""查询侧槽位识别与上下文词表。

从用户 query 判断本次召回命中的 P0 槽位，以及记忆是否允许进入
本次上下文。全部为纯文本函数。
"""

from __future__ import annotations

import re


def query_conflict_slot(query: str) -> str | None:
    """识别 query 命中的 P0 冲突槽位；未命中返回 None。

    槽位语义见 `Thinkback记忆三层架构` §0：昵称、宠物名、地点、生日、
    工作状态、沟通偏好、饮食偏好、睡眠提醒等高频事实。
    """
    normalized = query.lower()
    compact = normalized.strip(" ?？。,.，")
    asks_pet_name = any(
        marker in normalized for marker in ("叫什么", "名字", "叫啥", "name", "called", "named")
    )
    if asks_pet_name:
        if any(marker in normalized for marker in ("猫", "cat")):
            return "pet_name:cat"
        if any(marker in normalized for marker in ("狗", "dog")):
            return "pet_name:dog"
        if any(marker in normalized for marker in ("鸟", "鹦鹉", "bird", "parrot")):
            return "pet_name:bird"
        if any(marker in normalized for marker in ("兔", "兔子", "rabbit", "bunny")):
            return "pet_name:rabbit"
    # M-2 修复：要求第一人称自指（"我"/"我的"/"me"/"my"/"i"）共现。
    # "system nickname policy" / "how to address the user" 等元问题不应触发，
    # 因为它们没有自指主语（"user"/"them" 在元场景里是宾语/讨论对象）。
    _first_person = (
        re.search(r"(?:我|我的|叫我|叫用户|用户|我等|\bme\b|\bmy\b|\bmyself\b|\bi\s)", normalized)
        is not None
    )
    asks_nickname = bool(_first_person) and (
        any(
            marker in normalized
            for marker in ("称呼", "nickname", "preferred name", "叫用户", "叫我")
        )
        or re.search(
            r"\b(?:what|how).{0,16}(?:call|called|address).{0,16}(?:user|me|them)\b",
            normalized,
        )
        is not None
    )
    if asks_nickname:
        return "preferred_nickname"
    asks_location = (
        any(marker in normalized for marker in ("住哪", "住在", "住哪里", "搬到", "搬去"))
        or "location" in normalized
        or re.search(r"\bwhere.{0,16}(?:live|living|located|based)\b", normalized) is not None
        or re.search(r"\blive(?:s|d)?\s+in\b|\bliving\s+in\b", normalized) is not None
        or re.search(r"(?:现在|目前|current).{0,8}城市", normalized) is not None
    )
    if asks_location:
        return "current_location"
    asks_work_status = (
        "工作状态" in normalized
        or "work status" in normalized
        or compact in {"换工作", "job status"}
        or re.search(r"(?:换工作|工作).{0,8}(?:状态|进展|情况)", normalized) is not None
        or re.search(
            r"\b(?:current|what|what's|whats|how).{0,20}(?:job|offer|work).{0,12}status\b",
            normalized,
        )
        is not None
    )
    if asks_work_status:
        return "current_work_status"
    # M-3 修复：要求第一人称自指（"我"/"我的"/"me"/"my"/"i"）共现，
    # 避免 "how to communicate" / "怎么给建议" 等元问题误触发。
    _first_person_for_comm = (
        re.search(r"(?:我|我的|我们|\bme\b|\bmy\b|\bmyself\b|\bi\s|\bi\b)", normalized) is not None
    )
    asks_communication_preference = bool(_first_person_for_comm) and (
        "怎么给建议" in normalized
        or "沟通偏好" in normalized
        or re.search(r"(?:怎么|如何).{0,8}(?:沟通|建议)", normalized) is not None
        or "communication preference" in normalized
        or "advice preference" in normalized
        or re.search(
            r"\bhow.{0,20}(?:communicate|give advice|advise|suggestions?)\b",
            normalized,
        )
        is not None
    )
    if asks_communication_preference:
        return "communication_preference"
    asks_birthday = (
        "生日" in normalized
        and any(marker in normalized for marker in ("哪天", "什么时候", "几号", "日期", "是"))
    ) or (
        "birthday party" not in normalized
        and re.search(
            r"\b(?:when|what|which day|date).{0,24}birthday\b|\bbirthday.{0,24}(?:when|what|which day|date)\b",
            normalized,
        )
        is not None
    )
    if asks_birthday:
        return "birthday"
    if any(
        marker in normalized
        for marker in (
            "喜欢喝",
            "饮品",
            "饮料",
            "favorite drink",
            "preferred drink",
            "drink preference",
            "beverage preference",
        )
    ):
        return "favorite:drink"
    if any(
        marker in normalized for marker in ("喜欢吃", "食物", "favorite food", "food preference")
    ):
        return "favorite:food"
    if any(
        marker in normalized
        for marker in ("提醒睡觉", "催睡觉", "睡眠提醒", "睡觉偏好", "sleep reminder")
    ):
        return "sleep_reminder_preference"
    return None


def memory_context_allowed(memory: object, query: str) -> bool:
    """非槽位查询时，要求记忆与 query 共享至少一个上下文词。"""
    if query_conflict_slot(query) is not None:
        return True
    if query_is_broad_memory_request(query):
        return True

    query_terms = context_terms(query)
    if not query_terms:
        return True

    memory_text = str(getattr(memory, "memory_text", "") or memory)
    memory_terms = context_terms(memory_text)
    if not memory_terms:
        return True
    return bool(query_terms & memory_terms)


def query_is_broad_memory_request(query: str) -> bool:
    """query 是否是"用户提过什么"类的宽泛记忆请求。"""
    lowered = query.lower()
    return any(
        marker in lowered
        for marker in (
            "提过什么",
            "提到什么",
            "聊过什么",
            "记得什么",
            "什么重要信息",
            "重要信息",
            "what did the user mention",
            "what has the user mentioned",
            "anything important",
            "important information",
            "recent important",
        )
    )


def context_terms(text: str) -> set[str]:
    """抽取中英混合的上下文词表（含双语别名归一与中文 n-gram）。"""
    lowered = text.lower()
    terms = {
        word
        for word in re.findall(r"[a-z][a-z0-9_-]{2,}", lowered)
        if word
        not in {
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
        }
    }
    aliases = {
        "猫": "cat",
        "cat": "cat",
        "狗": "dog",
        "dog": "dog",
        "鸟": "bird",
        "鹦鹉": "bird",
        "bird": "bird",
        "parrot": "bird",
        "兔": "rabbit",
        "兔子": "rabbit",
        "rabbit": "rabbit",
        "bunny": "rabbit",
        "咖啡": "coffee",
        "coffee": "coffee",
        "茶": "tea",
        "tea": "tea",
        "饮品": "drink",
        "饮料": "drink",
        "drink": "drink",
        "食物": "food",
        "food": "food",
        "工作": "job",
        "job": "job",
        "offer": "job",
        "生日": "birthday",
        "birthday": "birthday",
        "建议": "advice",
        "advice": "advice",
        "沟通": "communication",
        "communication": "communication",
        "睡觉": "sleep",
        "sleep": "sleep",
        "城市": "city",
        "city": "city",
    }
    for marker, alias in aliases.items():
        if marker in lowered:
            terms.add(alias)

    for run in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        stripped = run
        for stop in frozenset(
            (
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
            )
        ):
            stripped = stripped.replace(stop, " ")
        for segment in re.findall(r"[\u4e00-\u9fff]{2,}", stripped):
            for size in range(2, min(4, len(segment)) + 1):
                for index in range(0, len(segment) - size + 1):
                    terms.add(segment[index : index + size])
    return terms
