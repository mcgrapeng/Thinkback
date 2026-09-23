"""P0：L2 摘要 LLM 化的单元测试。

覆盖：
- 服务装配：composer=None 不启用（V1 行为不变）；注入后触发后台刷新。
- 刷新链路：全量 active 轮次 → composer → upsert(kind=llm) → 缓存失效回调。
- 降级语义：LLM 失败保留拼接版（kind=concat），不抛异常、无重试风暴。
- 去抖：首 append 即刷新；此后每 N 个 append 触发；在飞时不重复提交。
- repo 持久化：summary_kind 在 upsert/read 往返保留。
- infra 客户端：OpenAICompatibleLLMClient 的包装/空输出/异常路径。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from thinkback.infra.llm import OpenAICompatibleLLMClient
from thinkback.memory.backends import FakeMemoryBackend
from thinkback.memory.l2_refresh import (
    L2BackgroundRefresher,
    build_default_composer_with,
)
from thinkback.memory.repositories import InMemoryMemoryRepository
from thinkback.memory.schemas import (
    AppendMemoryRequest,
    DeleteMemoryRequest,
    DeleteScope,
    RecallMemoryRequest,
)
from thinkback.memory.service import MemoryService


def make_append(round_id: str, content: str, *, index: int = 1) -> AppendMemoryRequest:
    from datetime import UTC, datetime

    return AppendMemoryRequest(
        request_id=f"req-{round_id}",
        user_id="user-1",
        session_id="session-1",
        round_id=round_id,
        round_index=index,
        messages=[
            {
                "message_id": f"{round_id}-u",
                "role": "user",
                "content": content,
                "timestamp": "2026-09-07T10:00:00+00:00",
            },
            {
                "message_id": f"{round_id}-a",
                "role": "assistant",
                "content": "好的。",
                "timestamp": "2026-09-07T10:00:01+00:00",
            },
        ],
        source_timestamp=datetime(2026, 9, 7, 10, 0, 1, tzinfo=UTC),
    )


class RecordingComposer:
    """记录调用并可编程返回/抛错的 fake composer。"""

    def __init__(self, output: str = "【主题】测试摘要") -> None:
        self.output = output
        self.calls: list[tuple[int, str | None]] = []
        self.fail = False

    def __call__(self, rounds: list[Any], previous_summary: str | None) -> str:
        self.calls.append((len(rounds), previous_summary))
        if self.fail:
            raise RuntimeError("simulated llm failure")
        return self.output


def make_service(
    composer: RecordingComposer | None, *, interval: int = 5
) -> tuple[MemoryService, RecordingComposer | None]:
    repository = InMemoryMemoryRepository()
    service = MemoryService(
        repository=repository,
        backend=FakeMemoryBackend(),
        l3_write_mode="sync",
        l2_summary_composer=composer,
        l2_refresh_interval_rounds=interval,
    )
    return service, composer


# ── 装配与行为不变性 ───────────────────────────────────────────────────


def test_l2_llm_disabled_by_default_keeps_v1_behavior() -> None:
    """composer=None（默认/关闭开关）时：append 后 L2 仍为拼接版，无后台刷新。"""
    service, composer = make_service(None)
    service.append(make_append("r1", "我养了一只猫叫麻薯"))

    summary = service.repository.get_summary("user-1", "session-1")
    assert summary is not None
    assert summary.summary_kind == "concat"
    assert "麻薯" in summary.summary_text
    assert composer is None


def test_append_triggers_immediate_llm_refresh_on_first_round() -> None:
    """作用域首次出现即触发刷新（不等满 N 轮）。"""
    composer = RecordingComposer("【主题】猫\n【进行中事项】无")
    service, _ = make_service(composer, interval=5)
    service.append(make_append("r1", "我养了一只猫叫麻薯"))
    service.drain_l3_background_tasks(timeout=5)

    summary = service.repository.get_summary("user-1", "session-1")
    assert summary is not None
    assert summary.summary_kind == "llm"
    assert summary.summary_text.startswith("【主题】")
    assert len(composer.calls) == 1
    round_count, previous = composer.calls[0]
    assert round_count == 1
    assert previous is None  # 首次无上一版


def test_debounce_only_refreshes_every_interval_rounds() -> None:
    """去抖：首次触发后，第 2..N-1 轮不触发，第 N 轮再触发。"""
    composer = RecordingComposer()
    service, _ = make_service(composer, interval=3)
    for index in range(1, 5):
        service.append(make_append(f"r{index}", f"第{index}轮内容", index=index))
    service.drain_l3_background_tasks(timeout=5)

    # r1 触发（首次），r2/r3 计数，r3 达到 interval=3 再触发，r4 计数 → 共 2 次
    assert len(composer.calls) == 2
    # 第二次调用能看到第一次的 llm 摘要作为上一版
    assert composer.calls[1][1] is not None


def test_previous_summary_only_fed_when_kind_is_llm() -> None:
    """上一版只喂 llm 版本；拼接版（如 LLM 失败后的降级）不作为增量输入。"""
    composer = RecordingComposer()
    composer.fail = True
    service, _ = make_service(composer, interval=1)
    service.append(make_append("r1", "第一轮"))
    service.drain_l3_background_tasks(timeout=5)
    assert len(composer.calls) == 1
    summary = service.repository.get_summary("user-1", "session-1")
    assert summary is not None and summary.summary_kind == "concat"

    composer.fail = False
    service.append(make_append("r2", "第二轮", index=2))
    service.drain_l3_background_tasks(timeout=5)
    assert composer.calls[1][1] is None  # concat 版没有被当作上一版


def test_llm_failure_keeps_concat_fallback_and_service_alive() -> None:
    """降级语义：LLM 失败 → 保留拼接版、不抛异常，后续轮次仍可恢复。"""
    composer = RecordingComposer()
    composer.fail = True
    service, _ = make_service(composer, interval=2)
    service.append(make_append("r1", "我住在杭州"))
    service.drain_l3_background_tasks(timeout=5)

    summary = service.repository.get_summary("user-1", "session-1")
    assert summary is not None
    assert summary.summary_kind == "concat"
    assert "杭州" in summary.summary_text

    composer.fail = False
    service.append(make_append("r2", "我在换工作", index=2))
    service.drain_l3_background_tasks(timeout=5)
    summary = service.repository.get_summary("user-1", "session-1")
    assert summary is not None and summary.summary_kind == "llm"


def test_refreshed_summary_visible_via_recall_l2_layer() -> None:
    """刷新后的 llm 摘要进入召回 L2 层（对召回侧透明）。"""
    composer = RecordingComposer("【主题】养猫与搬家\n【近期状态】杭州")
    service, _ = make_service(composer, interval=5)
    service.append(make_append("r1", "我养了一只猫叫麻薯，最近搬到杭州"))
    service.drain_l3_background_tasks(timeout=5)

    recall = service.recall(
        RecallMemoryRequest(
            user_id="user-1",
            session_id="session-1",
            query="用户提过什么重要信息？",
        )
    )
    l2_items = [item for item in recall.items if item.layer == "L2"]
    assert l2_items and "【主题】" in l2_items[0].content


def test_deleted_rounds_excluded_from_llm_refresh() -> None:
    """刷新输入排除已删除来源（与拼接版同口径）。"""
    composer = RecordingComposer()
    service, _ = make_service(composer, interval=99)
    service.append(make_append("r1", "我养了一只猫叫麻薯"))
    service.drain_l3_background_tasks(timeout=5)
    service.delete(
        DeleteMemoryRequest(
            request_id="req-del",
            user_id="user-1",
            scope=DeleteScope.SESSION,
            operation_id="op-del",
            session_id="session-1",
        )
    )
    service.append(make_append("r2", "新的一轮", index=2))  # 删除后新会话轮次（同 scope 重建）
    service.drain_l3_background_tasks(timeout=5)
    # 最后一次刷新（第二次触发：删除会重置 _llm_refreshed_scopes? 不会——进程内集合。
    # 删除后 rounds 只剩 r2 → composer 收到的轮次不含已删来源。
    round_count, _ = composer.calls[-1]
    contents = service.repository.list_rounds("user-1", "session-1")
    assert round_count == len(contents)


def test_pending_scope_not_resubmitted_while_in_flight() -> None:
    """在飞去重：刷新阻塞期间连续 append 不重复提交（阻塞 composer 消除竞态）。"""
    from threading import Event

    release = Event()

    def blocking_composer(rounds: list[Any], previous_summary: str | None) -> str:
        release.wait(timeout=5)
        return "【主题】done"

    repository = InMemoryMemoryRepository()
    repository.save_round(make_append("r1", "内容"))
    refresher = L2BackgroundRefresher(
        repository=repository,
        composer=blocking_composer,
        executor=ThreadPoolExecutor(max_workers=2),
        refresh_interval_rounds=1,
    )
    refresher.maybe_submit("user-1", "session-1")
    refresher.maybe_submit("user-1", "session-1")  # 在飞（composer 阻塞中）
    refresher.maybe_submit("user-1", "session-1")
    # 此刻在飞的只有第一次提交
    assert refresher.pending_count == 1
    release.set()
    refresher.drain(timeout=5)
    assert refresher.pending_count == 0


# ── 仓储持久化 ────────────────────────────────────────────────────────


def test_upsert_summary_kind_roundtrip_in_memory_repo() -> None:
    repository = InMemoryMemoryRepository()
    repository.save_round(make_append("r1", "内容"))
    rounds = repository.list_rounds("user-1", "session-1")

    entry = repository.upsert_summary_from_rounds("user-1", "session-1", rounds)
    assert entry.summary_kind == "concat"
    entry = repository.upsert_summary_from_rounds(
        "user-1", "session-1", rounds, summary_text="【主题】x", summary_kind="llm"
    )
    assert entry.summary_kind == "llm"
    assert repository.get_summary("user-1", "session-1").summary_kind == "llm"


# ── infra 客户端 ──────────────────────────────────────────────────────


class _FakeCompletions:
    def __init__(self, text: str | None, fail: bool = False) -> None:
        self.text = text
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)

        class _Msg:
            content = self.text

        class _Choice:
            message = _Msg()

        class _Resp:
            choices = [_Choice()]

        if self.fail:
            raise ConnectionError("boom")
        return _Resp()


class _FakeOpenAIClient:
    def __init__(self, text: str | None, fail: bool = False) -> None:
        self.chat = type("Chat", (), {"completions": _FakeCompletions(text, fail)})()


def test_llm_client_complete_happy_path() -> None:
    fake = _FakeOpenAIClient(" 摘要文本 ")
    client = OpenAICompatibleLLMClient(
        base_url="http://llm.example/v1", api_key="k", model="m", client=fake
    )
    assert client.complete("sys", "usr") == "摘要文本"
    sent = fake.chat.completions.calls[0]
    assert sent["messages"][0] == {"role": "system", "content": "sys"}
    assert sent["messages"][1] == {"role": "user", "content": "usr"}


def test_llm_client_wraps_errors_and_empty_output() -> None:
    failing = OpenAICompatibleLLMClient(
        base_url="u", api_key="k", model="m", client=_FakeOpenAIClient(None, fail=True)
    )
    with pytest.raises(RuntimeError, match="llm completion failed"):
        failing.complete("s", "u")

    empty = OpenAICompatibleLLMClient(
        base_url="u", api_key="k", model="m", client=_FakeOpenAIClient("  ")
    )
    with pytest.raises(RuntimeError, match="empty"):
        empty.complete("s", "u")


def test_build_default_composer_passes_system_prompt() -> None:
    seen: list[tuple[str, str]] = []

    def fake_complete(system: str, user: str) -> str:
        seen.append((system, user))
        return "【主题】ok"

    composer = build_default_composer_with(fake_complete)
    repository = InMemoryMemoryRepository()
    repository.save_round(make_append("r1", "内容"))
    result = composer(repository.list_rounds("user-1", "session-1"), None)
    assert result == "【主题】ok"
    assert "综合摘要" in seen[0][0]  # system prompt 注入
    assert "内容" in seen[0][1]  # 轮次进入 user prompt
