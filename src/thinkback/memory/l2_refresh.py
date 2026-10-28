"""L2 LLM 综合摘要的后台刷新器（P0：L2 摘要 LLM 化）。

模式参考 Letta sleep-time / ChatGPT Memory Synthesis（RESEARCH.md 矩阵 #1）：

- append 同步路径**永远先写拼接版**（``summary_kind=concat``）——LLM 不可用、
  未启用或后台刷新失败时，L2 始终有降级内容，召回链路不空转。
- 每个作用域累计 N 个 append（或首次出现）时提交一次后台 LLM 刷新：
  去抖计数器 + pending 集合防止重复提交；刷新用全量 active 轮次
  （排除已删除来源）+ 上一版 **llm** 摘要做增量综合。
- LLM 失败：打 warning 后保留拼接版，无重试风暴（best-effort 优化语义）。
- 刷新成功：``summary_kind=llm`` 落库，并通过 ``on_refreshed`` 回调失效
  服务层摘要缓存。

并发模型：复用服务的 L3 后台线程池（都是低频后台 LLM 工作，共享预算）；
futures 单独记账，``drain`` 由 ``MemoryService.drain_l3_background_tasks``
统一等待。
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, wait
from contextvars import copy_context
from threading import Lock
from typing import TYPE_CHECKING

from loguru import logger

from thinkback.domain.keys import DEFAULT_NAMESPACE
from thinkback.domain.summarization import (
    L2_STRUCTURED_SUMMARY_SYSTEM_PROMPT,
    SummaryComposer,
    SummaryComposerResult,
    build_structured_summary_user_prompt,
    sections_to_text,
)

if TYPE_CHECKING:
    from thinkback.domain.entities import JournalEntry
    from thinkback.domain.ports import MemoryRepository

OnSummaryRefreshed = Callable[[str, str], None]

# 刷新作用域的键：(user_id, namespace, session_scope_id)。
# namespace 必须进键：同一 session_id 在不同 namespace 下是两份记忆，
# 去抖计数与在飞去重不能跨 namespace 共享。
ScopeKey = tuple[str, str, str]


class L2BackgroundRefresher:
    """每个 ``MemoryService`` 持有零或一个实例（composer 为 None 时不启用）。"""

    def __init__(
        self,
        *,
        repository: MemoryRepository,
        composer: SummaryComposer,
        executor: ThreadPoolExecutor,
        refresh_interval_rounds: int = 5,
        on_refreshed: OnSummaryRefreshed | None = None,
        reserve_capacity: Callable[..., None] | None = None,
        release_capacity: Callable[[], None] | None = None,
        max_tracked_scopes: int = 4096,
    ) -> None:
        if refresh_interval_rounds < 1:
            raise ValueError("refresh_interval_rounds must be >= 1")
        self._repository = repository
        self._composer = composer
        self._executor = executor
        self.refresh_interval_rounds = refresh_interval_rounds
        self._on_refreshed = on_refreshed
        # 与 L3 共用同一个队列预算：L2 刷新必须排队，否则新会话突发会
        # 绕过 max_pending_tasks 直接压到 LLM 上。
        self._reserve_capacity = reserve_capacity
        self._release_capacity = release_capacity
        self._max_tracked_scopes = max(1, max_tracked_scopes)
        self._state_lock = Lock()
        # 去抖：每个 (user, namespace, scope) 自上次提交以来的 append 计数（LRU 淘汰）
        self._append_counters: OrderedDict[ScopeKey, int] = OrderedDict()
        # 已完成过至少一次 llm 刷新的作用域（进程内视角；重启后首 append 会再触发一次，幂等无害）
        self._llm_refreshed_scopes: OrderedDict[ScopeKey, None] = OrderedDict()
        # 提交去重 + futures 记账
        self._pending_scopes: set[ScopeKey] = set()
        self._futures: set[Future[None]] = set()

    def _remember_scope(self, scope: ScopeKey) -> None:
        """记住一个活跃 scope，并淘汰最久未用的（进程内状态必须有上界）。

        注意：这里**不能**清零 append 计数——计数表示「上次提交之后又来了
        几轮」，在飞刷新结束时要靠它决定要不要补一次 trailing 刷新。清零
        会让在飞期间到达的轮次被吞掉（且只在刷新写入成功时复现，很难被
        常规跑法发现）。
        """
        if scope in self._append_counters:
            self._append_counters.move_to_end(scope)
        self._llm_refreshed_scopes.pop(scope, None)
        self._llm_refreshed_scopes[scope] = None
        self._trim_scopes()

    def _trim_scopes(self) -> None:
        """按 LRU 淘汰超出上界的 scope 状态（调用方持有状态锁）。"""
        while len(self._append_counters) > self._max_tracked_scopes:
            self._append_counters.popitem(last=False)
        while len(self._llm_refreshed_scopes) > self._max_tracked_scopes:
            self._llm_refreshed_scopes.popitem(last=False)

    # ── 对外接口 ────────────────────────────────────────────────────

    def maybe_submit(
        self, user_id: str, session_scope_id: str, *, namespace: str = DEFAULT_NAMESPACE
    ) -> None:
        """append 后调用：达到去抖阈值或该作用域尚无 llm 摘要时提交后台刷新。"""

        scope: ScopeKey = (user_id, namespace, session_scope_id)
        with self._state_lock:
            # 计数先落：在飞期间的 append 也要计数，否则在飞结束后
            # 这几轮永远等不到下一次刷新（trailing 刷新缺失）。
            counter = self._append_counters.get(scope, 0) + 1
            self._append_counters[scope] = counter
            self._append_counters.move_to_end(scope)
            self._trim_scopes()
            if scope in self._pending_scopes:
                # 已有一次在飞的刷新，等它完成后计数自然对齐
                return
            has_llm_summary = scope in self._llm_refreshed_scopes
            due = not has_llm_summary or counter >= self.refresh_interval_rounds
            if not due:
                return

        # 容量闸门：与 L3 共用队列预算，队列满时保留拼接摘要并延后，
        # 不排队（否则新会话突发会绕过 max_pending_tasks 压垮 LLM）。
        # 不等待：这里跑在 append 请求线程里，等 5 秒会把 append 变慢。
        if self._reserve_capacity is not None:
            try:
                self._reserve_capacity(0)
            except Exception as exc:  # noqa: BLE001 - 队列满只是延后
                logger.bind(
                    user_id=user_id,
                    session_scope_id=session_scope_id,
                    error_type=type(exc).__name__,
                ).warning("memory l2 llm refresh deferred: background queue full")
                return

        with self._state_lock:
            if scope in self._pending_scopes:
                # 容量闸门期间另一个 append 也判定该刷新：它先抢到，本作用域
                # 不能再提交一次（两次 LLM 刷新会互相覆盖摘要）。
                if self._release_capacity is not None:
                    self._release_capacity()
                return
            self._append_counters[scope] = 0
            self._pending_scopes.add(scope)

        context = copy_context()
        try:
            future = self._executor.submit(
                context.run, self._run_refresh, user_id, session_scope_id, namespace
            )
        except Exception:
            with self._state_lock:
                self._pending_scopes.discard(scope)
            if self._release_capacity is not None:
                self._release_capacity()
            logger.bind(user_id=user_id, session_scope_id=session_scope_id).warning(
                "memory l2 llm refresh submit failed"
            )
            return
        with self._state_lock:
            self._futures.add(future)
        future.add_done_callback(lambda done_future: context.run(self._finish, done_future, scope))

    def drain(self, timeout: float | None = None) -> None:
        """等待在飞的 L2 刷新完成（失败已被记录，不向上抛）。

        要连 trailing 刷新一起等：在飞刷新结束后可能立刻补一次新的，
        所以循环到既没有 future 也没有 pending scope 为止。
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._state_lock:
                futures = list(self._futures)
                pending = bool(self._pending_scopes)
            if not futures and not pending:
                return
            remaining: float | None = None
            if deadline is not None:
                remaining = max(0.0, deadline - time.monotonic())
                if remaining <= 0:
                    return
            wait(futures, timeout=remaining)
            if not futures and pending:
                # future 已完成但完成回调还没跑（trailing 刷新尚未提交）：
                # 等一小会儿再收一次。
                time.sleep(0.005)

    @property
    def pending_count(self) -> int:
        with self._state_lock:
            return len(self._futures)

    # ── 内部 ────────────────────────────────────────────────────────

    def _finish(self, future: Future[None], scope: ScopeKey) -> None:
        with self._state_lock:
            self._futures.discard(future)
            self._pending_scopes.discard(scope)
            # 在飞期间累计的轮次达到阈值 → 补一次 trailing 刷新，
            # 否则这些轮次会被旧快照吞掉，直到再攒够 N 轮才更新。
            trailing_due = self._append_counters.get(scope, 0) >= self.refresh_interval_rounds
        if self._release_capacity is not None:
            # 归还队列槽位：L2 与 L3 共用同一个预算。
            self._release_capacity()
        if future.done() and not future.cancelled() and future.exception() is not None:
            # _run_refresh 自身已捕获并落日志；这里只做记账清理。
            pass
        if trailing_due:
            user_id, namespace, session_scope_id = scope
            self.maybe_submit(user_id, session_scope_id, namespace=namespace)

    def _run_refresh(
        self, user_id: str, session_scope_id: str, namespace: str = DEFAULT_NAMESPACE
    ) -> None:
        refresh_log = logger.bind(
            user_id=user_id, session_scope_id=session_scope_id, namespace=namespace
        )
        refresh_log.debug("memory l2 llm refresh started")
        try:
            # 乐观锁版本必须在**读轮次之前**取。顺序反了的话，删除恰好落在
            # 「读完轮次」与「读版本」之间时，拿到的是删除之后的新版本号，
            # CAS 反而成功，已删原文被写回 ACTIVE 摘要。
            expected_row_version = self._summary_row_version(
                user_id, session_scope_id, namespace=namespace
            )
            # 排除集按用户聚合：L3 记忆在长期 scope，轮次在会话 scope；
            # 按 scope 过滤会漏掉已删除/已遗忘的事实 → LLM 摘要把原文复活。
            excluded_refs = self._repository.excluded_source_refs_for_user(user_id)
            rounds = [
                entry
                for entry in self._repository.list_rounds(
                    user_id, session_scope_id, namespace=namespace
                )
                if (entry.session_id, entry.round_id) not in excluded_refs
            ]
            previous_summary = self._previous_llm_summary(
                user_id, session_scope_id, namespace=namespace
            )
            if not rounds:
                refresh_log.debug("memory l2 llm refresh skipped: no active rounds")
                return
            summary_text, structured_sections = self._composer(rounds, previous_summary)
            if not summary_text or not summary_text.strip():
                raise RuntimeError("l2 llm composer returned empty summary")
            # 写入前复核排除集：composer 期间发生的删除（删除屏障墓碑化轮次）
            # 必须让本次刷新放弃，否则摘要里会带回已删原文。
            current_excluded = self._repository.excluded_source_refs_for_user(user_id)
            resurrected = [
                entry for entry in rounds if (entry.session_id, entry.round_id) in current_excluded
            ]
            if resurrected:
                refresh_log.bind(
                    resurrected_round_count=len(resurrected),
                ).warning("memory l2 llm refresh skipped: sources deleted during refresh")
                return
            written = self._repository.upsert_summary_from_rounds(
                user_id,
                session_scope_id,
                rounds,
                summary_text=summary_text.strip(),
                summary_kind="llm",
                structured_sections=structured_sections,
                expected_row_version=expected_row_version,
                namespace=namespace,
            )
            if written.summary_text != summary_text.strip():
                # 写不进去：期间有人改过这一行（多半是删除置脏）。
                # 保留对方的结果，并且不把本 scope 记成已刷新 ——
                # 否则下一轮会把别人的内容当「上一版 LLM 摘要」喂进模型。
                refresh_log.bind(
                    current_state=written.summary_state,
                    current_kind=written.summary_kind,
                ).warning("memory l2 llm refresh superseded by concurrent write")
                return
            with self._state_lock:
                self._remember_scope((user_id, namespace, session_scope_id))
            if self._on_refreshed is not None:
                self._on_refreshed(user_id, session_scope_id)
            refresh_log.bind(
                round_count=len(rounds),
                summary_length=len(summary_text),
                had_previous=previous_summary is not None,
            ).info("memory l2 llm refresh completed")
        except Exception as exc:
            # 降级语义：保留拼接版摘要，不重试、不影响主链路。
            refresh_log.bind(error_type=type(exc).__name__).warning(
                "memory l2 llm refresh failed: keeping concat summary"
            )

    def _summary_row_version(
        self, user_id: str, session_scope_id: str, *, namespace: str = DEFAULT_NAMESPACE
    ) -> int | None:
        """当前摘要行版本（乐观锁基准）。行不存在时返回 None。"""
        summary = self._repository.get_summary(user_id, session_scope_id, namespace=namespace)
        return summary.row_version if summary is not None else None

    def _previous_llm_summary(
        self, user_id: str, session_scope_id: str, *, namespace: str = DEFAULT_NAMESPACE
    ) -> str | None:
        """上一版摘要只喂 llm 版本；拼接版是轮次子集，喂入只会引入噪声。"""

        summary = self._repository.get_summary(user_id, session_scope_id, namespace=namespace)
        if summary is None:
            return None
        if getattr(summary, "summary_kind", "concat") != "llm":
            return None
        return str(summary.summary_text or "") or None


def build_default_composer_with(
    complete: Callable[[str, str], str],
) -> SummaryComposer:
    """把「system+user → 文本」的底层调用适配为 ``SummaryComposer``。

    供 infra 层注入真实 LLM 客户端、测试注入 fake 使用。
    返回 ``(拼接视图, 结构化 4 段画像)``；G4 落地后下游可机器消费各段。
    """

    def compose(rounds: list[JournalEntry], previous_summary: str | None) -> SummaryComposerResult:
        user_prompt = build_structured_summary_user_prompt(rounds, previous_summary)
        raw = complete(L2_STRUCTURED_SUMMARY_SYSTEM_PROMPT, user_prompt)
        sections = parse_summary_sections(raw)
        text_view = sections_to_text(sections) if sections else raw.strip()
        return text_view, sections

    return compose


def parse_summary_sections(raw: str) -> dict[str, str]:
    """解析 LLM 返回的结构化摘要。

    优先尝试 JSON 解析（强 schema 路径）；失败时回退到【】标记的弱解析，
    容错处理弱模型返回的自由文本（与之前 V2 行为兼容）。

    4 段 schema 固定：主题 / 进行中事项 / 行为偏好 / 近期状态。LLM 返回的
    其他 key 一律丢弃（防止 prompt 漂移引入噪声 key）；非字符串 value
    （json 数字/null）转为 "" 或丢弃，保证下游机器消费安全。
    """
    import json
    import re

    known_keys = ("主题", "进行中事项", "行为偏好", "近期状态")
    candidate = raw.strip()
    if candidate.startswith("{"):
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                sections: dict[str, str] = {}
                for key in known_keys:
                    if key in parsed and isinstance(parsed[key], str):
                        sections[key] = parsed[key]
                if sections:
                    return sections
                # JSON 解析成功但 known_keys 全空（LLM 返回了其他字段），回退
                # 到【】解析以保留旧模型行为；不回退会让 LLM 输出彻底丢失。
        except json.JSONDecodeError:
            pass
    sections = {}
    for key in known_keys:
        match = re.search(rf"【{key}】\s*([^\n【]+)", raw)
        if match:
            sections[key] = match.group(1).strip()
    return sections
