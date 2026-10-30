"""L2 摘要解析器的契约（memory/l2_refresh.parse_summary_sections）。

这个解析器决定「LLM 回来的摘要里，哪几段能进记忆」。它有两条路：

- **JSON 路径（强 schema）**：只认 4 段固定 key，其余 key 一律丢弃；
- **【】路径（弱模型容错）**：按标记逐段抓取值。

判据写歪的后果各不相同：

- **该丢的没丢** → prompt 漂移引入的噪声字段进了下游，治理台看到
  「用户偏好：null」这种垃圾；
- **该留的没留** → 用户真说过的内容从摘要里消失，助手回头答不出来；
- **该回退的不回退** → 弱模型返回的自由文本整段丢失，摘要变成空。

已有用例只覆盖了 3 条路径（过滤噪声 key / 丢非字符串 value /
JSON 全是噪声 key 时回退），边界一个都没钉：解析失败、标记的取值边界
（换行、下一段标记、重复标记、空白）、以及「JSON 段为空串」这种
「看起来没内容但其实段存在」的输入。
"""

from __future__ import annotations

import pytest

from thinkback.memory.l2_refresh import parse_summary_sections

#: 4 段固定 schema —— 少一段就少一段摘要，多一段就是噪声。
KNOWN_SECTIONS = ("主题", "进行中事项", "行为偏好", "近期状态")


@pytest.mark.parametrize("section", KNOWN_SECTIONS)
def test_the_json_path_keeps_every_known_section(section: str) -> None:
    """4 段每一段都要能从 JSON 里取到。

    少认一段 → 这一段的内容从摘要里消失 → 助手回忆时看不到用户说过的事。
    """
    raw = "{" + ", ".join(f'"{key}": "{key}的值"' for key in KNOWN_SECTIONS) + "}"

    assert parse_summary_sections(raw) == {key: f"{key}的值" for key in KNOWN_SECTIONS}


def test_the_json_path_drops_keys_that_are_not_part_of_the_schema() -> None:
    """schema 之外的 key 一律丢弃 —— prompt 漂移引入的字段不能进下游。"""
    parsed = parse_summary_sections('{"主题": "x", "noise_key": "drop", "备注": "drop"}')

    assert parsed == {"主题": "x"}


def test_the_json_path_drops_non_string_values() -> None:
    """数字 / null 不是摘要内容 —— 硬转成字符串只会给下游灌噪声。"""
    parsed = parse_summary_sections('{"主题": 123, "进行中事项": null, "行为偏好": "keep"}')

    assert parsed == {"行为偏好": "keep"}


def test_an_empty_string_value_is_still_a_section() -> None:
    """空串是「这一段存在但没内容」，不是「这一段不存在」。

    两者在下游的处置不同：段不存在会被补默认值，段存在但为空则保留
    「用户确实没提」这个事实。
    """
    assert parse_summary_sections('{"主题": ""}') == {"主题": ""}


def test_the_json_path_ignores_whitespace_around_the_payload() -> None:
    """LLM 常常在 JSON 前后带空白 —— 归一化后再判「是不是 JSON」。"""
    assert parse_summary_sections('  {"主题": "x"}  ') == {"主题": "x"}


def test_a_broken_json_falls_back_to_the_marker_path() -> None:
    """截断的 JSON 要回退到【】解析，而不是把整段丢掉。"""
    parsed = parse_summary_sections('{"主题": "x"\n【行为偏好】保留我')

    assert parsed == {"行为偏好": "保留我"}


def test_a_json_followed_by_other_text_falls_back_to_the_marker_path() -> None:
    """JSON 后面还跟着别的内容时，它就不是合法 JSON —— 按弱解析走。

    这条容易被忽略：``json.loads`` 对「对象 + 尾随内容」直接报错，
    于是模型返回「JSON + 补充说明」时，整段 JSON 里的信息会全丢。
    """
    parsed = parse_summary_sections('{"主题": "x"}\n【行为偏好】保留我')

    assert parsed == {"行为偏好": "保留我"}


def test_a_json_with_only_unknown_keys_falls_back_to_the_marker_path() -> None:
    """JSON 解析成功但一段都不认识时，回退到【】而不是返回空。"""
    parsed = parse_summary_sections('{"unknown": "x"}\n【主题】hello\n【行为偏好】foo')

    assert parsed == {"主题": "hello", "行为偏好": "foo"}


def test_the_marker_path_reads_every_section() -> None:
    """弱模型路径要能读全 4 段。"""
    raw = "\n".join(f"【{key}】{key}的内容" for key in KNOWN_SECTIONS)

    assert parse_summary_sections(raw) == {key: f"{key}的内容" for key in KNOWN_SECTIONS}


def test_a_marker_value_stops_at_the_next_marker() -> None:
    """一段的取值不能越过下一段标记 —— 否则两段被拼成一段。"""
    parsed = parse_summary_sections("【主题】主题内容\n【行为偏好】偏好内容")

    assert parsed == {"主题": "主题内容", "行为偏好": "偏好内容"}


def test_a_marker_value_stops_at_the_line_break() -> None:
    """取值只取到行尾 —— 换行之后是下一行的事。"""
    assert parse_summary_sections("【主题】第一行\n第二行") == {"主题": "第一行"}


def test_a_marker_value_may_span_the_line_after_the_marker() -> None:
    """标记独占一行、值在下一行时也要读得到（弱模型常这么排版）。"""
    assert parse_summary_sections("【主题】\n换行后的值") == {"主题": "换行后的值"}


def test_a_marker_value_is_stripped() -> None:
    """取值两端的空白要摘掉 —— 摘要里留着缩进，肉眼看着就是脏数据。"""
    assert parse_summary_sections("【主题】  空格值  ") == {"主题": "空格值"}


def test_a_marker_value_may_contain_spaces_inside() -> None:
    """值内部的空格要保留 —— 那是内容，不是缩进。"""
    assert parse_summary_sections("【主题】值 里 有 空格") == {"主题": "值 里 有 空格"}


def test_a_marker_value_stops_at_a_nested_marker() -> None:
    """值里出现【就截断 —— 【是段落标记的起始符。"""
    assert parse_summary_sections("【主题】a【b") == {"主题": "a"}


def test_an_empty_marker_value_yields_no_section() -> None:
    """空段不产出条目 —— 结果里只有真正有内容的段。"""
    parsed = parse_summary_sections("【主题】\n【行为偏好】有内容")

    assert parsed == {"行为偏好": "有内容"}


def test_the_first_occurrence_of_a_marker_wins() -> None:
    """同一个标记出现两次时取第一个 —— 后面那段是模型自己重复的话。"""
    assert parse_summary_sections("【主题】first\n【主题】second") == {"主题": "first"}


def test_a_section_that_is_absent_is_not_in_the_result() -> None:
    """没出现的段不在结果里 —— 下游据此判断「这一段用户没提」。"""
    parsed = parse_summary_sections("【主题】只有主题")

    assert parsed == {"主题": "只有主题"}
    assert "近期状态" not in parsed


def test_an_empty_payload_yields_no_sections() -> None:
    """空输入 → 空结果，不能是 {段: ""}。"""
    assert parse_summary_sections("") == {}
