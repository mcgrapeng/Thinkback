"""抽取指令：全局默认约束 + 按类型追加的领域约束。

引擎（Mem0）每次写入只能带一份指令，且是**覆盖**配置级默认值
（``custom_instr = prompt or self.custom_instructions``）。所以全局默认与
类型指令必须由 Thinkback 自己拼接后一次性传入，否则类型指令会把全局硬
约束整段丢掉。ADR-0014。

全局默认放在这里而不是后端适配器里：它是领域策略，后端只负责传参。
"""

from __future__ import annotations

DEFAULT_EXTRACTION_INSTRUCTIONS = """Additional requirements:
1. Language: write each memory text in the SAME language as the conversation (Chinese conversations → Chinese memories). Never translate Chinese names, nicknames, places, or brands into English.
2. Output shape: return exactly {"memory": [{"id": "<sequential string>", "text": "<one self-contained memory>"}]}. Every "text" MUST be a plain string; never nest arrays or objects inside it.
3. Preserve entity values verbatim in their original script (宠物名/昵称/地点/品牌逐字保留). Do NOT transliterate names and do NOT append a parenthetical romanization (e.g. "Mashu (麻薯)").
4. Corrections write only the new value, never re-state the old one. When the user says "不叫 X，叫 Y" or "改成 Y", record only the new fact Y. Do NOT include X in the same memory text. The superseded memory is handled by a separate update path; your job is to surface the current truth, not the audit trail.
5. One fact per memory. If a turn contains multiple distinct facts, emit one memory per fact. Never combine "X is Y" + "X is Z" into a single memory.
6. The "New Messages" section may contain a "user", a "tool" or a "system" message; extract the facts from whichever message is present. NEVER return an empty list merely because there is no assistant turn to extract from.
7. Facts include the user's own work and business context: responsibilities, process / equipment / tool facts, targets, requirements, constraints, deadlines, customer information, complaints, and numeric specifics (percentages, process nodes, tolerances). If the user's message contains ANY of these, extract at least one memory about it - returning an empty list for such a message is WRONG. Return an empty list ONLY for messages that truly contain no personal, work or business fact (pure greetings or acknowledgements)."""


def build_extraction_prompt(
    default_instructions: str = DEFAULT_EXTRACTION_INSTRUCTIONS,
    type_instructions: str | None = None,
) -> str:
    """拼接全局默认与类型指令：全局在前，类型在后。

    类型指令只能**增加**约束，不能删掉全局硬约束，所以顺序固定为
    全局 → 类型。类型没有指令时原样返回全局默认。
    """
    type_text = (type_instructions or "").strip()
    base = (default_instructions or "").strip()
    if not type_text:
        return base
    if not base:
        return type_text
    return f"{base}\n\n{type_text}"
