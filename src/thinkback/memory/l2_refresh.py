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

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, wait
from contextvars import copy_context
from threading import Lock
from typing import TYPE_CHECKING

from loguru import logger

from thinkback.domain.summarization import (
    L2_STRUCTURED_SUMMARY_SYSTEM_PROMPT,
    SummaryComposer,
    build_structured_summary_user_prompt,
)

if TYPE_CHECKING:
    from thinkback.domain.entities import JournalEntry
    from thinkback.domain.ports import MemoryRepository

OnSummaryRefreshed = Callable[[str, str], None]


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
    ) -> None:
        if refresh_interval_rounds < 1:
            raise ValueError("refresh_interval_rounds must be >= 1")
        self._repository = repository
        self._composer = composer
        self._executor = executor
        self.refresh_interval_rounds = refresh_interval_rounds
        self._on_refreshed = on_refreshed
        self._state_lock = Lock()
        # 去抖：每个 (user, scope) 自上次提交以来的 append 计数
        self._append_counters: dict[tuple[str, str], int] = {}
        # 已完成过至少一次 llm 刷新的作用域（进程内视角；重启后首 append 会再触发一次，幂等无害）
        self._llm_refreshed_scopes: set[tuple[str, str]] = set()
        # 提交去重 + futures 记账
        self._pending_scopes: set[tuple[str, str]] = set()
        self._futures: set[Future[None]] = set()

    # ── 对外接口 ────────────────────────────────────────────────────

    def maybe_submit(self, user_id: str, session_scope_id: str) -> None:
        """append 后调用：达到去抖阈值或该作用域尚无 llm 摘要时提交后台刷新。"""

        scope = (user_id, session_scope_id)
        with self._state_lock:
            if scope in self._pending_scopes:
                # 已有一次在飞的刷新，等它完成后计数自然对齐
                return
            has_llm_summary = scope in self._llm_refreshed_scopes
            counter = self._append_counters.get(scope, 0) + 1
            due = not has_llm_summary or counter >= self.refresh_interval_rounds
            if not due:
                self._append_counters[scope] = counter
                return
            self._append_counters[scope] = 0
            self._pending_scopes.add(scope)

        context = copy_context()
        try:
            future = self._executor.submit(
                context.run, self._run_refresh, user_id, session_scope_id
            )
        except Exception:
            with self._state_lock:
                self._pending_scopes.discard(scope)
            logger.bind(user_id=user_id, session_scope_id=session_scope_id).warning(
                "memory l2 llm refresh submit failed"
            )
            return
        with self._state_lock:
            self._futures.add(future)
        future.add_done_callback(lambda done_future: context.run(self._finish, done_future, scope))

    def drain(self, timeout: float | None = None) -> None:
        """等待在飞的 L2 刷新完成（失败已被记录，不向上抛）。"""

        with self._state_lock:
            futures = list(self._futures)
        if not futures:
            return
        wait(futures, timeout=timeout)

    @property
    def pending_count(self) -> int:
        with self._state_lock:
            return len(self._futures)

    # ── 内部 ────────────────────────────────────────────────────────

    def _finish(self, future: Future[None], scope: tuple[str, str]) -> None:
        with self._state_lock:
            self._futures.discard(future)
            self._pending_scopes.discard(scope)
        if future.done() and not future.cancelled() and future.exception() is not None:
            # _run_refresh 自身已捕获并落日志；这里只做记账清理。
            pass

    def _run_refresh(self, user_id: str, session_scope_id: str) -> None:
        refresh_log = logger.bind(user_id=user_id, session_scope_id=session_scope_id)
        refresh_log.debug("memory l2 llm refresh started")
        try:
            excluded_refs = self._repository.excluded_source_refs(user_id, session_scope_id)
            rounds = [
                entry
                for entry in self._repository.list_rounds(user_id, session_scope_id)
                if (entry.session_id, entry.round_id) not in excluded_refs
            ]
            previous_summary = self._previous_llm_summary(user_id, session_scope_id)
            if not rounds:
                refresh_log.debug("memory l2 llm refresh skipped: no active rounds")
                return
            summary_text = self._composer(rounds, previous_summary)
            if not summary_text or not summary_text.strip():
                raise RuntimeError("l2 llm composer returned empty summary")
            self._repository.upsert_summary_from_rounds(
                user_id,
                session_scope_id,
                rounds,
                summary_text=summary_text.strip(),
                summary_kind="llm",
            )
            with self._state_lock:
                self._llm_refreshed_scopes.add((user_id, session_scope_id))
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

    def _previous_llm_summary(self, user_id: str, session_scope_id: str) -> str | None:
        """上一版摘要只喂 llm 版本；拼接版是轮次子集，喂入只会引入噪声。"""

        summary = self._repository.get_summary(user_id, session_scope_id)
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
    """

    def compose(rounds: list[JournalEntry], previous_summary: str | None) -> str:
        user_prompt = build_structured_summary_user_prompt(rounds, previous_summary)
        return complete(L2_STRUCTURED_SUMMARY_SYSTEM_PROMPT, user_prompt)

    return compose
