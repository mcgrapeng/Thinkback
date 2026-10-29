"""FakeMemoryBackend 与生产后端的契约对齐。

fake 是整套确定性测试的地基：1100 多个用例都跑在它上面。fake 比生产宽松，
测试就等于骗人 —— 一条针对「更新不存在的记忆」的用例在 fake 上通过，到生产
就炸。所以 fake 的语义必须**不比生产宽松**，并且要有用例钉住。

本文件盯的是 ``_call`` 这层包装：它承诺与
``Mem0LibraryMemoryBackend._call`` 一样把后端异常包成 RuntimeError，
并且对不存在的操作抛 AttributeError。这层历史上一次都没被执行过。
"""

from __future__ import annotations

import pytest

from thinkback.memory.backends import FakeMemoryBackend


def test_fake_call_forwards_arguments_to_the_operation() -> None:
    """_call 是透明转发：参数与关键字原样交给操作。"""
    backend = FakeMemoryBackend()

    backend.add(
        [{"role": "user", "content": "我养了一只狗"}],
        user_id="user-1",
        memory_scope_id="thinkback",
    )

    backend._call("update", next(iter(backend.memories)), "我养了一只猫")

    assert list(backend.memories.values())[0]["memory"] == "我养了一只猫"


def test_fake_call_wraps_backend_exceptions_into_runtime_error() -> None:
    """后端异常包成 RuntimeError：与生产后端的异常语义一致。

    生产后端把 mem0 的任何异常包成 ``RuntimeError("mem0 library <op> failed")``。
    fake 不包的话，服务层那些 ``except RuntimeError`` 分支在测试里根本不触发。
    """
    backend = FakeMemoryBackend()

    with pytest.raises(RuntimeError, match="fake backend update failed"):
        backend._call("update", "does-not-exist", "新内容")


def test_fake_call_rejects_unknown_operations() -> None:
    """不存在的操作抛 AttributeError：拼错操作名要当场炸，别静默通过。"""
    backend = FakeMemoryBackend()

    with pytest.raises(AttributeError, match="no operation 'explode'"):
        backend._call("explode")


def test_fake_call_does_not_swallow_attribute_errors_from_the_operation() -> None:
    """操作本身抛的 AttributeError 也要包成 RuntimeError（与生产一致）。"""
    backend = FakeMemoryBackend()

    def _explode(*args: object, **kwargs: object) -> None:
        raise AttributeError("内部属性访问失败")

    backend.explode = _explode  # type: ignore[attr-defined]

    with pytest.raises(RuntimeError, match="fake backend explode failed"):
        backend._call("explode")


def test_fake_add_returns_no_events_when_the_content_is_empty() -> None:
    """空正文不产生事件：避免把空记忆写进向量库。"""
    backend = FakeMemoryBackend()

    assert backend.add([{"role": "user", "content": "   "}], user_id="u", memory_scope_id="s") == []
    assert backend.memories == {}


def test_fake_get_all_respects_the_limit() -> None:
    """get_all 必须按 limit 截断：生产后端分页，越界取全量会拖垮一次复核。"""
    backend = FakeMemoryBackend()
    for index in range(5):
        backend.memories[f"m-{index}"] = {
            "id": f"m-{index}",
            "memory": f"记忆 {index}",
            "event": "ADD",
            "user_id": "user-1",
            "agent_id": "thinkback",
            "metadata": {},
            "score": 1.0,
        }

    assert len(backend.get_all(user_id="user-1", memory_scope_id="thinkback", limit=2)) == 2
