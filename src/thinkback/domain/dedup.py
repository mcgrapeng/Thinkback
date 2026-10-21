"""跨通道去重的领域口径（纯函数）。

一次召回会同时装配三条通道：

- **L1** 原始回合（对话原文拼接）
- **L2** 会话摘要（拼接版或 LLM 综合版）
- **L3** 业务记忆（mem0 抽取 + 槽位归一后的单条事实）

三条通道描述的是同一批事实，措辞却各不相同：大小写、空白、标点、
``user:`` 之类的说话人前缀、句末句号…… 旧口径只按「字符串完全相等」去重，
于是同一条事实会被注入两三次：既浪费 token 预算，又让模型看到互相重复、
甚至互相矛盾的表述。

本模块给出两条**领域口径**的去重键：

- :func:`content_dedup_key`：正文归一化后的键。归一只保留实义字符
  （字母 / 数字 / 汉字），丢掉空白与标点并统一大小写 —— 语义等价的措辞
  落到同一个键上。
- :func:`slot_dedup_key`：P0 槽位键。同一个 P0 槽位（生日 / 现居城市 /
  宠物名 / 沟通偏好……）在一个上下文包里只能有一条事实：旧值与新值
  同时出现，模型会同时看到两个「答案」。这条键用于**槽位冲突消解**，
  与正文去重互补（正文不同、槽位相同的两条记忆必须二选一）。

纯函数：无 IO、无时钟依赖、无全局状态。
"""

from __future__ import annotations

from thinkback.domain.slots.canonical import memory_conflict_slot


def normalize_text(text: str) -> str:
    """正文归一化：只保留实义字符（字母 / 数字 / 汉字），大小写归一。

    - ``"User lives in Shanghai."`` → ``"userlivesinshanghai"``
    - ``"我住在上海。"`` → ``"我住在上海"``
    - ``"user:  我住在上海"`` → ``"user我住在上海"``

    归一只用于**比较**，不用于展示：真正注入模型的内容仍是原文。
    """

    return "".join(char for char in text.casefold() if char.isalnum())


def content_dedup_key(text: str) -> str:
    """正文去重键：归一化后的文本；空正文返回空串（调用方应跳过）。"""

    return normalize_text(text)


def slot_dedup_key(text: str) -> str | None:
    """P0 槽位去重键；未命中任何 P0 槽位返回 ``None``。

    槽位口径与冲突检测完全一致（``domain.slots.canonical.memory_conflict_slot``），
    避免「召回期认为同槽位、写入期却当成不同事实」的两套口径。
    """

    slot = memory_conflict_slot(text)
    if slot is None:
        return None
    return f"slot:{slot}"
