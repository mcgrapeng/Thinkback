"""槽位抽取与源文本校验的**词表**（数据，不是逻辑）。

为什么单独一个文件：
词表里的每一条都是「措辞」—— 改一个字不影响判定口径，但会让某个说法
不被识别。变异审计（mutmut）逐条改这些字面量，产出的绝大多数是
「等价变异」（大小写 / ``XX`` 包裹），那不是缺陷信号，只是噪音；
真正要盯的是**判定分支**，那些留在 ``extractors.py`` / ``support.py`` 里。

词表内容本身由 golden 用例逐条钉死（``tests/unit/test_slot_vocabulary_golden.py``）：
每条措辞配一个只命中它的样例，断言抽取器的输出。措辞被改坏、删掉、
拼错，golden 用例会红。

这个文件在 mutmut 的变异范围外（``pyproject.toml`` 的 ``do_not_mutate``）——
词表是数据，用 golden 用例验；不在范围里的那些变异体是噪音，登记成存活
只会稀释门禁的信噪比。
"""

from __future__ import annotations

# NICKNAME_PATTERNS
NICKNAME_PATTERNS: tuple[str, ...] = (
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
    # 第三人称偏好措辞：模型抽出的中文往往是「用户喜欢别人叫他X」
    # （而不是「叫我X」）。这类文本不识别成昵称槽位，纠正后旧昵称就
    # 不会被 supersede，旧值继续留在画像里被召回。
    r"(?:喜欢|希望|习惯|想要)?别人(?:喜欢|希望)?(?:叫我|叫他|叫她|称呼)\s*([一-鿿A-Za-z0-9_-]{2,24})",
    r"\b(?:user|they|them|me|my|their|i)\s+(?:should\s+)?(?:be\s+)?called\s+'?([一-鿿A-Za-z0-9_-]{2,24})'?",
    r"\b(?:call|called)\s+(?:me|the user|them)\s+'?([一-鿿A-Za-z0-9_-]{2,24})'?",
)

# NICKNAME_MARKERS
NICKNAME_MARKERS: tuple[str, ...] = (
    "preferred name",
    "prefers to be called",
    "prefers to be addressed as",
    "name should be",
    "name is",
    "called",
    "addressed as",
    "叫我",
    "称呼",
    # 第三人称偏好措辞（见 patterns 里的同款说明）。
    "别人叫",
)

# PET_NAME_PATTERNS
PET_NAME_PATTERNS: tuple[str, ...] = (
    r"\bfrom\s+[一-鿿A-Za-z0-9_-]{1,24}\s+to\s+([一-鿿A-Za-z0-9_-]{1,24})",
    r"\b(?:cat|dog)\s+was\s+initially\s+named\s+[一-鿿A-Za-z0-9_-]{1,24}\s+but\s+corrected\s+to\s+([一-鿿A-Za-z0-9_-]{1,24})",
    r"\b(?:cat|dog|bird|parrot|rabbit|bunny)\s+character\s+named\s+([一-鿿A-Za-z0-9_-]{1,24})",
    r"\b(?:cat|dog|bird|parrot|rabbit|bunny)(?:'s)?\s+name\s+is\s+([一-鿿A-Za-z0-9_-]{1,24})",
    r"\b(?:cat|dog|bird|parrot|rabbit|bunny)\s+(?:is\s+)?named\s+([一-鿿A-Za-z0-9_-]{1,24})",
    r"(?:猫|狗|鸟|鹦鹉|兔|兔子)不叫[一-鿿A-Za-z0-9_-]{1,24}[，,]?\s*(?:现在)?(?:叫|名叫|名字叫)\s*([一-鿿A-Za-z0-9_-]{1,24})",
    r"(?:猫|狗|鸟|鹦鹉|兔|兔子)(?:的)?(?:名字)?(?:叫|名叫)\s*([一-鿿A-Za-z0-9_-]{1,24})",
    r"(?:养了|有).*?(?:猫|狗|鸟|鹦鹉|兔|兔子).*?(?:叫|名叫|名字叫)\s*([一-鿿A-Za-z0-9_-]{1,24})",
)

# LOCATION_MARKERS
LOCATION_MARKERS: tuple[str, ...] = ("live", "resided", "moved", "relocated", "住", "搬")

# LOCATION_PATTERNS
LOCATION_PATTERNS: tuple[str, ...] = (
    r"\bfrom\s+[一-鿿A-Za-z0-9_-]{2,40}\s+to\s+([一-鿿A-Za-z0-9_-]{2,40})",
    r"\bmoved\s+to\s+([一-鿿A-Za-z0-9_-]{2,40})",
    r"\bbefore\s+moving\s+to\s+([一-鿿A-Za-z0-9_-]{2,40})",
    r"\bbefore\s+relocating\s+to\s+([一-鿿A-Za-z0-9_-]{2,40})",
    r"\bpreviously\s+resided\s+in\s+[一-鿿A-Za-z0-9_-]{2,40}\s+before\s+relocating\s+to\s+([一-鿿A-Za-z0-9_-]{2,40})",
    r"\brelocated\s+to\s+([一-鿿A-Za-z0-9_-]{2,40})",
    r"\blives?\s+in\s+([一-鿿A-Za-z0-9_-]{2,40})",
    r"(?:搬到|搬去|住在|现在住在)\s*([一-鿿A-Za-z0-9_-]{2,40})",
)

# LOCATION_STOPWORDS
LOCATION_STOPWORDS: frozenset[str] = frozenset({"the", "this", "that", "moment"})

# WORK_STATUS_MARKERS
WORK_STATUS_MARKERS: tuple[str, ...] = (
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

# WORK_STATUS_PATTERNS
WORK_STATUS_PATTERNS: tuple[str, ...] = (
    r"\b(user\s+)?accepted\s+(.+?offer.+?)(?:\s+and\s+is\s+no\s+longer\b|[.;。]|$)",
    r"(?:work status|job status|current work status).*?\bfrom\b.+?\bto\b\s+(.+?)(?:[.;。]|$)",
    r"\bcurrent\s+work\s+status:\s*(.+?)(?:[.;。]|$)",
    r"\bstopped\s+evaluating\s+.+?\s+after\s+accepting\s+(.+?offer)(?:,|[.;。]|$)",
    r"\baccepted\s+(.+?offer.+?)(?:[.;。]|$)",
    r"\bis\s+(.+?job opportunities)(?:[.;。]|$)",
    r"(?:工作状态|现在)\s*(?:是|为)?\s*([^。,.，]+)",
    # 中文工作状态写法：模型抽出来的是「已经接受 X 的 offer，不再评估其它
    # 工作机会」。不认这类措辞，这条记忆就拿不到 current_work_status 槽位，
    # 与源规范化出的那条同槽位记忆变成两条并存的事实。
    r"(?:已经|已|正式)?接受(?:了)?\s*([^。,.，]+?offer[^。,.，]*)",
    r"(?:不再|停止|不用)评估(?:其它|其他)?(?:工作)?机会",
)

# COMMUNICATION_MARKERS
COMMUNICATION_MARKERS: tuple[str, ...] = (
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

# COMMUNICATION_PATTERNS
COMMUNICATION_PATTERNS: tuple[str, ...] = (
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
    # 中文偏好写法：模型抽出来的是「希望得到简洁直接的建议，不需要先安慰」
    # 这类句式。只认「沟通偏好」开头的写法时，这条记忆拿不到
    # communication_preference 槽位，与源规范化出的那条同槽位记忆并存。
    r"(?:希望|想要|更想要|更喜欢|希望被)(?:得到|被)?\s*([^。,.，]+?)(?:，?不需要|，?不要|，?不想|[。,.，]|$)",
)

# BIRTHDAY_PATTERNS
BIRTHDAY_PATTERNS: tuple[str, ...] = (
    r"\bfrom\s+.+?\s+to\s+(.+?)(?:[.;。]|$)",
    r"\bcorrected\s+(?:their\s+)?birthday\s+to\s+(.+?)(?:\s*\(|,|\s+not\b|[.;。]|$)",
    r"\bbirthday\s+falls\s+on\s+(.+?)(?:,|\s+correcting\b|[.;。]|$)",
    r"\bbirthday\s+has\s+been\s+updated\s+to\s+(.+?)(?:,|\s+not\b|[.;。]|$)",
    r"\bbirthday\s*(?:is|:)\s*(.+?)(?:[.;。]|$)",
    r"(?:生日|出生日期).*?不是[^。,.，]+[，,]?\s*(?:是|改成|改为|更正为|纠正为)\s*([^。,.，]+)",
    r"(?:生日|出生日期).*?(?:改成|改为|更正为|纠正为|(?<!不)是)\s*([^。,.，]+)",
    r"(?:生日|出生日期)(?:是|为|:|：)?\s*([^。,.，]+)",
)

# FAVORITE_DRINK_MARKERS
FAVORITE_DRINK_MARKERS: frozenset[str] = frozenset(
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

# FAVORITE_DRINK_MARKERS_CN
FAVORITE_DRINK_MARKERS_CN: tuple[str, ...] = ("喜欢喝", "饮品", "饮料")

# FAVORITE_FOOD_MARKERS
FAVORITE_FOOD_MARKERS: frozenset[str] = frozenset(
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

# FAVORITE_FOOD_MARKERS_CN
FAVORITE_FOOD_MARKERS_CN: tuple[str, ...] = ("喜欢吃", "食物")

# FAVORITE_DRINK_PATTERNS
FAVORITE_DRINK_PATTERNS: tuple[str, ...] = (
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

# FAVORITE_FOOD_PATTERNS
FAVORITE_FOOD_PATTERNS: tuple[str, ...] = (
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

# SLEEP_REMINDER_MARKERS
SLEEP_REMINDER_MARKERS: tuple[str, ...] = (
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

# SLEEP_REMINDER_PATTERNS
SLEEP_REMINDER_PATTERNS: tuple[str, ...] = (
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

# SLEEP_REMINDER_STOPWORDS
SLEEP_REMINDER_STOPWORDS: frozenset[str] = frozenset({"催睡觉", "提醒睡觉", "睡眠提醒"})

# PET_ASCII_MARKERS
PET_ASCII_MARKERS: dict[str, tuple[str, ...]] = {
    "cat": ("cat",),
    "dog": ("dog",),
    "bird": ("bird", "parrot"),
    "rabbit": ("rabbit", "bunny"),
}

# PET_CJK_MARKERS
PET_CJK_MARKERS: dict[str, tuple[str, ...]] = {
    "cat": ("猫",),
    "dog": ("狗",),
    "bird": ("鸟", "鹦鹉"),
    "rabbit": ("兔", "兔子"),
}

# NICKNAME_SOURCE_MARKERS
NICKNAME_SOURCE_MARKERS: tuple[str, ...] = (
    "叫我",
    "称呼我",
    "叫用户",
    "称呼",
    "preferred name",
    "name should be",
    "name is",
    "called",
    "addressed as",
)

# BIRTHDAY_SOURCE_MARKERS
BIRTHDAY_SOURCE_MARKERS: tuple[str, ...] = ("生日", "birthday")

# LOCATION_SOURCE_MARKERS
LOCATION_SOURCE_MARKERS: tuple[str, ...] = (
    "住",
    "搬",
    "城市",
    "location",
    "live",
    "lived",
    "moving",
    "moved",
    "relocated",
)

# WORK_STATUS_SOURCE_MARKERS
WORK_STATUS_SOURCE_MARKERS: tuple[str, ...] = ("工作", "offer", "job", "moonshot", "换工作")

# ANXIETY_MEMORY_MARKERS
ANXIETY_MEMORY_MARKERS: tuple[str, ...] = (
    "anxiety",
    "anxious",
    "break down",
    "breaking down",
    "焦虑",
    "拆解",
)

# ANXIETY_SOURCE_MARKERS
ANXIETY_SOURCE_MARKERS: tuple[str, ...] = (
    "焦虑",
    "拆解",
    "anxiety",
    "anxious",
    "break down",
    "breaking down",
)

# CONCISE_MEMORY_MARKERS
CONCISE_MEMORY_MARKERS: tuple[str, ...] = ("concise", "direct", "简洁", "直接")

# CONCISE_SOURCE_MARKERS
CONCISE_SOURCE_MARKERS: tuple[str, ...] = ("简洁", "直接", "concise", "direct")

# REASSURANCE_SOURCE_MARKERS
REASSURANCE_SOURCE_MARKERS: tuple[str, ...] = ("安慰", "reassurance", "comfort")

# COMMUNICATION_SOURCE_MARKERS
COMMUNICATION_SOURCE_MARKERS: tuple[str, ...] = (
    "沟通",
    "建议",
    "安慰",
    "说教",
    "焦虑",
    "communication",
    "advice",
    "suggestions",
    "reassurance",
    "anxious",
)

# DRINK_SOURCE_MARKERS
DRINK_SOURCE_MARKERS: tuple[str, ...] = (
    "饮品",
    "饮料",
    "喝",
    "咖啡",
    "茶",
    "drink",
    "beverage",
    "coffee",
    "tea",
)

# FOOD_SOURCE_MARKERS
FOOD_SOURCE_MARKERS: tuple[str, ...] = ("食物", "吃", "food")

# SLEEP_SOURCE_MARKERS
SLEEP_SOURCE_MARKERS: tuple[str, ...] = ("睡", "提醒", "sleep", "reminder")
