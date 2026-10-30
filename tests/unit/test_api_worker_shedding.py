"""API 线程池的**限流泄压**路径：饱和时必须 503，不能挂死也不能 500。

记忆服务是同步实现，FastAPI 侧用线程池 + 信号量兜住并发。池子满的时候
唯一正确的答案是「暂时不可用」（503）—— 挂死会让上游把请求堆在这里；
返回 500 会让调用方以为「重试也没用」。

而且槽位必须**归还**：漏还一个，池子就永远少一格，最后整条写路径死锁。
这几条路径以前没被走过 —— 常规跑法永远打不满池子。
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException

from thinkback.api import memory as memory_module
from thinkback.api.memory import _run_memory_call_in_pool
from thinkback.infra.config import settings


def _executor() -> ThreadPoolExecutor:
    return ThreadPoolExecutor(max_workers=1, thread_name_prefix="test-api-worker")


async def test_a_saturated_pool_answers_503_instead_of_hanging() -> None:
    """槽位拿不到 → 503 queue full。"""

    slots = asyncio.BoundedSemaphore(1)
    await slots.acquire()  # 把唯一的槽位占住
    try:
        with pytest.raises(HTTPException) as excinfo:
            await _run_memory_call_in_pool(slots, _executor(), lambda: "never")
    finally:
        slots.release()

    assert excinfo.value.status_code == 503
    assert "queue full" in str(excinfo.value.detail)


async def test_a_slot_is_returned_after_a_successful_call() -> None:
    """成功之后必须归还槽位 —— 漏还一个就是一次永久的容量损失。"""

    slots = asyncio.BoundedSemaphore(1)
    executor = _executor()
    try:
        assert await _run_memory_call_in_pool(slots, executor, lambda: "ok") == "ok"
        assert slots._value == 1
    finally:
        executor.shutdown(wait=False)


async def test_a_slot_is_returned_after_a_failing_call() -> None:
    """抛异常也必须归还 —— 否则一次报错就吃掉一个槽位。"""

    slots = asyncio.BoundedSemaphore(1)
    executor = _executor()

    def _boom() -> None:
        raise RuntimeError("backend down")

    try:
        with pytest.raises(RuntimeError, match="backend down"):
            await _run_memory_call_in_pool(slots, executor, _boom)
        assert slots._value == 1
    finally:
        executor.shutdown(wait=False)


async def test_a_slot_is_returned_when_the_submit_fails() -> None:
    """提交失败（池子已关）时必须归还槽位 —— 否则池子一关就再也起不来。"""

    slots = asyncio.BoundedSemaphore(1)
    executor = _executor()
    executor.shutdown(wait=False)

    with pytest.raises(RuntimeError):
        await _run_memory_call_in_pool(slots, executor, lambda: "never")

    assert slots._value == 1


async def test_a_value_error_is_mapped_to_400() -> None:
    """未分类的 ValueError → 400（不是 500）。"""

    slots = asyncio.BoundedSemaphore(1)
    executor = _executor()

    def _bad() -> None:
        raise ValueError("something is off")

    try:
        with pytest.raises(HTTPException) as excinfo:
            await _run_memory_call_in_pool(slots, executor, _bad)
        assert excinfo.value.status_code == 400
    finally:
        executor.shutdown(wait=False)


async def test_an_unclassified_runtime_error_keeps_its_500() -> None:
    """未分类的 RuntimeError 保持 500 —— 不许兜底成 4xx（那是「重试也没用」）。"""

    slots = asyncio.BoundedSemaphore(1)
    executor = _executor()

    def _boom() -> None:
        raise RuntimeError("something exploded")

    try:
        with pytest.raises(RuntimeError, match="something exploded"):
            await _run_memory_call_in_pool(slots, executor, _boom)
    finally:
        executor.shutdown(wait=False)


async def test_the_wait_budget_comes_from_the_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """等待预算取自配置 —— 配成 0 就是「拿不到槽位立刻 503」。"""

    monkeypatch.setattr(settings, "memory_api_worker_wait_seconds", 0)
    slots = asyncio.BoundedSemaphore(1)
    await slots.acquire()
    try:
        with pytest.raises(HTTPException) as excinfo:
            await _run_memory_call_in_pool(slots, _executor(), lambda: "never")
    finally:
        slots.release()

    assert excinfo.value.status_code == 503


def test_the_read_and_write_pools_have_their_own_slots() -> None:
    """读池与写池各有各的槽位 —— 写洪峰不能把读路径饿死。"""

    assert memory_module._memory_read_call_slots is not memory_module._memory_write_call_slots
    assert memory_module._memory_read_call_executor is not memory_module._memory_write_call_executor
