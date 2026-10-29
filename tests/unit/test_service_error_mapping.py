"""领域异常 → HTTP 状态码的映射契约。

``api/errors.py`` 的 ``service_error_to_http`` 是服务层与 HTTP 之间的**唯一
翻译层**：状态码写错，调用方看到的语义就变了。

- ``dead_letter`` / ``stale write`` 必须是 409：调用方据此知道「重试也没用，
  得人工介入」，映射成 500 会让上游无限重试。
- ``optimistic lock contention exhausted`` 必须是 409：终态写没落库，调用方
  必须看到失败并重试。映射成 200 的话，调用方以为成功了，task 行却停在
  RUNNING —— 之后每次重试都命中 already_done，不一致被永久固化。
- ``l3 background queue full`` / ``repository operation timed out`` 是 503：
  可重试的暂时性故障。
- ``mem0 library`` / ``memory backend`` 是 502：上游依赖挂了。
- 不认识的 RuntimeError 返回 ``None``：由调用方记日志后原样上抛（500），
  **不能**默认映射成 4xx —— 那是把服务端故障说成客户端错误。

此前这层只有业务路由间接覆盖到几个分支，``400`` 兜底、``409`` 终态、
``None`` 透传三条都没被钉住。
"""

from __future__ import annotations

from fastapi import HTTPException

from thinkback.api.errors import service_error_to_http
from thinkback.domain.errors import TaskLockExhaustedError, TaskStaleWriteError


def _status(exc: Exception) -> int | None:
    mapped = service_error_to_http(exc)
    assert mapped is not None, f"{exc!r} 应当被映射"
    assert isinstance(mapped, HTTPException)
    return mapped.status_code


# ---------------------------------------------------------------------------
# ValueError → 4xx
# ---------------------------------------------------------------------------


def test_fail_closed_value_error_maps_to_403() -> None:
    assert _status(ValueError("policy fail-closed: restricted")) == 403


def test_conflict_value_error_maps_to_409() -> None:
    assert _status(ValueError("memory type conflict detected")) == 409


def test_not_found_value_error_maps_to_404() -> None:
    assert _status(ValueError("memory not found")) == 404


def test_other_value_error_maps_to_400() -> None:
    """没命中任何关键词的 ValueError 是 400（调用方请求有问题）。"""
    assert _status(ValueError("content must not be empty")) == 400


def test_value_error_keeps_the_original_detail() -> None:
    mapped = service_error_to_http(ValueError("content must not be empty"))
    assert mapped is not None
    assert mapped.detail == "content must not be empty"


# ---------------------------------------------------------------------------
# RuntimeError → 409 / 503 / 502 / 透传
# ---------------------------------------------------------------------------


def test_dead_letter_maps_to_409() -> None:
    assert _status(RuntimeError("task dead_letter: budget exhausted")) == 409


def test_stale_write_maps_to_409() -> None:
    assert _status(RuntimeError("task stale write: t1 row_version 3 outdated")) == 409


def test_optimistic_lock_contention_exhausted_maps_to_409() -> None:
    """终态写没落库 —— 调用方必须看到失败，不能当成功。"""
    assert (
        _status(RuntimeError("task optimistic lock contention exhausted after 5 attempts: t1"))
        == 409
    )


def test_background_queue_full_maps_to_503() -> None:
    assert _status(RuntimeError("l3 background queue full")) == 503


def test_repository_timeout_maps_to_503() -> None:
    assert _status(RuntimeError("repository operation timed out")) == 503


def test_mem0_library_failure_maps_to_502() -> None:
    assert _status(RuntimeError("mem0 library not available")) == 502


def test_memory_backend_failure_maps_to_502() -> None:
    assert _status(RuntimeError("memory backend unreachable")) == 502


def test_unrecognized_runtime_error_is_not_mapped() -> None:
    """不认识的 RuntimeError 透传（调用方记日志后原样上抛 → 500）。

    兜底成 4xx 会把服务端故障说成客户端错误，调用方就不会重试。
    """
    assert service_error_to_http(RuntimeError("something nobody anticipated")) is None


def test_non_service_exception_is_not_mapped() -> None:
    assert service_error_to_http(KeyError("missing")) is None
    assert service_error_to_http(TypeError("bad type")) is None


# ---------------------------------------------------------------------------
# 领域异常实例本身
# ---------------------------------------------------------------------------


def test_task_stale_write_error_carries_the_context() -> None:
    error = TaskStaleWriteError("task-1", 7)
    assert error.task_id == "task-1"
    assert error.expected_version == 7
    assert "stale write" in str(error)


def test_task_lock_exhausted_error_carries_the_attempt_count() -> None:
    error = TaskLockExhaustedError("task-1", 5)
    assert error.task_id == "task-1"
    assert error.attempts == 5
    assert "optimistic lock contention exhausted" in str(error)


def test_domain_errors_map_to_the_expected_http_status() -> None:
    """领域异常实例直接喂给映射层，得到与文案约定一致的状态码。"""
    assert _status(TaskStaleWriteError("task-1", 7)) == 409
    assert _status(TaskLockExhaustedError("task-1", 5)) == 409
