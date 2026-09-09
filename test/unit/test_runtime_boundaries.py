from pathlib import Path
from types import SimpleNamespace

from innies_memory.memory.schemas import MemoryStatus


def test_lifespan_shuts_down_l3_executor(monkeypatch) -> None:
    """`_lifespan` 退出路径必须显式 shutdown L3 executor。

    仅 drain 而不 shutdown 会导致：
    - 生产 SIGTERM 时进程延迟数十秒
    - 测试套件偶发挂起
    - ThreadPoolExecutor worker 仍 idle 并继续接收任务
    """
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    from innies_memory.api import app as app_module
    from innies_memory.memory.backends import FakeMemoryBackend
    from innies_memory.memory.repositories import InMemoryMemoryRepository
    from innies_memory.memory.service import MemoryService

    captured: dict[str, ThreadPoolExecutor] = {}

    class _TrackingService(MemoryService):
        def __init__(self) -> None:
            super().__init__(
                repository=InMemoryMemoryRepository(),
                backend=FakeMemoryBackend(),
            )
            captured["executor"] = self._l3_executor
            # Spy on shutdown to detect the call without affecting threadpool state.
            self._shutdown_calls = 0
            original_shutdown = self._l3_executor.shutdown

            def _spy_shutdown(*args, **kwargs):  # type: ignore[no-untyped-def]
                self._shutdown_calls += 1
                return original_shutdown(*args, **kwargs)

            self._l3_executor.shutdown = _spy_shutdown  # type: ignore[method-assign]

    tracking_service = _TrackingService()
    monkeypatch.setattr(app_module, "_memory_service", tracking_service, raising=False)

    # Patch dependencies._memory_service lookup the lifespan uses.
    from innies_memory.api import dependencies

    monkeypatch.setattr(dependencies, "_memory_service", tracking_service, raising=False)

    # Construct a FastAPI app that uses the production `_lifespan` and drive it.
    from fastapi import FastAPI

    test_app = FastAPI(lifespan=app_module._lifespan)

    async def _drive() -> None:
        async with test_app.router.lifespan_context(test_app):
            pass

    asyncio.run(_drive())

    assert tracking_service._shutdown_calls >= 1, (
        "lifespan 退出必须调用 _l3_executor.shutdown(wait=...) — 否则 SIGTERM 会延迟数十秒"
    )
    # executor 应该已 shutdown（_threads 清空或 _shutdown 标志为 True）
    executor = captured["executor"]
    assert getattr(executor, "_shutdown", False) is True


def test_drain_l3_background_tasks_shuts_down_owned_executor() -> None:
    """drain 之后显式 shutdown_l3_executor 关闭自有 executor。

    生产 lifespan 退出路径 = drain + shutdown_l3_executor(wait=False)。
    drain 本身不关闭 executor（让单元测试能继续使用 service），shutdown
    由显式调用触发。
    """
    from innies_memory.memory.backends import FakeMemoryBackend
    from innies_memory.memory.repositories import InMemoryMemoryRepository
    from innies_memory.memory.service import MemoryService

    service = MemoryService(
        repository=InMemoryMemoryRepository(),
        backend=FakeMemoryBackend(),
    )
    assert service._owns_l3_executor is True

    service.drain_l3_background_tasks(timeout=0.1)
    # drain 不应关闭 executor
    assert getattr(service._l3_executor, "_shutdown", False) is False

    # 显式 shutdown 关闭 executor
    service.shutdown_l3_executor(wait=False)
    assert getattr(service._l3_executor, "_shutdown", False) is True
    assert service._l3_executor_shutdown is True

    # 幂等：再次 shutdown 不抛异常
    service.shutdown_l3_executor(wait=False)


def test_service_with_injected_executor_does_not_double_shutdown() -> None:
    """caller 注入自定义 executor 时，drain/shutdown 不关闭外部 executor。

    避免 double-shutdown：caller 拥有生命周期管理权，服务层在 caller-inject
    场景下既不主动 drain 也不主动 shutdown，而是把控制权留给 caller。
    """
    from concurrent.futures import ThreadPoolExecutor

    from innies_memory.memory.backends import FakeMemoryBackend
    from innies_memory.memory.repositories import InMemoryMemoryRepository
    from innies_memory.memory.service import MemoryService

    external_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="caller-injected")
    try:
        service = MemoryService(
            repository=InMemoryMemoryRepository(),
            backend=FakeMemoryBackend(),
            l3_executor=external_executor,
        )
        # 标记 caller-inject：服务不拥有 executor 生命周期
        assert service._owns_l3_executor is False

        # drain 不应该关闭外部 executor
        service.drain_l3_background_tasks(timeout=0.1)
        assert getattr(external_executor, "_shutdown", False) is False

        # 显式 shutdown_l3_executor() 在 caller-inject 场景下也是 no-op，
        # 否则 double-shutdown 会抛 RuntimeError
        service.shutdown_l3_executor()
        assert getattr(external_executor, "_shutdown", False) is False
        # 服务内部 shutdown 状态不应被错误地标为 True
        assert service._l3_executor_shutdown is False
    finally:
        # 收尾：测试结束后由 caller 关闭外部 executor
        if not getattr(external_executor, "_shutdown", False):
            external_executor.shutdown(wait=True)


def test_runtime_sources_do_not_depend_on_redis_or_celery() -> None:
    """Redis/Celery 已整体移除：源码不允许再出现 redis 客户端或 celery 集成。"""

    for path in Path("src/innies_memory").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "redis" not in source.lower(), str(path)
        assert "celery" not in source.lower(), str(path)


def test_mem0_config_builder_targets_milvus_memory_collection() -> None:
    assert not Path("src/memory/mem0_client.py").exists()


def test_mem0_boundary_uses_library_adapter_without_direct_vector_client() -> None:
    source = Path("src/innies_memory/memory/backends/mem0_library.py").read_text(encoding="utf-8")

    assert "Memory.from_config" in source
    assert "build_mem0_library_config" in source
    assert "QdrantClient" not in source
    assert "MilvusClient" not in source


def test_runtime_sources_do_not_use_qdrant_configuration() -> None:
    for path in Path("src/innies_memory").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "qdrant" not in source.lower(), str(path)


def test_runtime_sources_do_not_use_character_id() -> None:
    from pathlib import Path

    for path in Path("src/innies_memory").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "character_id" not in source, str(path)


def test_runtime_sources_do_not_import_legacy_top_level_packages() -> None:
    import re

    forbidden_patterns = [
        r"^from api\b",
        r"^import api\b",
        r"^from infra\b",
        r"^import infra\b",
        r"^from memory\b",
        r"^import memory\b",
    ]
    for path in Path("src/innies_memory").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        for pattern in forbidden_patterns:
            assert not re.search(pattern, source, re.MULTILINE), f"{path} still matches '{pattern}'"


def test_memory_service_exposes_public_workflows() -> None:
    from innies_memory.memory.backends import FakeMemoryBackend
    from innies_memory.memory.repositories import InMemoryMemoryRepository
    from innies_memory.memory.service import MemoryService

    service = MemoryService(repository=InMemoryMemoryRepository(), backend=FakeMemoryBackend())

    assert service.name == "innies-memory"
    assert callable(service.append)
    assert callable(service.recall)
    assert callable(service.delete)
    assert callable(service.rebuild)


def test_default_memory_service_uses_sql_repository(monkeypatch) -> None:
    import innies_memory.api.dependencies as dependencies
    from innies_memory.memory.backends import FakeMemoryBackend
    from innies_memory.memory.repositories import SqlAlchemyMemoryRepository

    class FakeHttpBackend(FakeMemoryBackend):
        def __init__(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
            super().__init__()

    monkeypatch.setattr(dependencies, "_memory_service", None)
    monkeypatch.setattr(dependencies, "Mem0LibraryMemoryBackend", FakeHttpBackend)
    service = dependencies.get_memory_service()

    assert isinstance(service.repository, SqlAlchemyMemoryRepository)


def test_memory_service_can_use_mem0_library_backend_from_settings(monkeypatch) -> None:
    import innies_memory.api.dependencies as dependencies
    from innies_memory.infra.config import Settings
    from innies_memory.memory.backends import Mem0LibraryMemoryBackend

    monkeypatch.setattr(dependencies, "_memory_service", None)
    for variable in [
        "MEMORY_L3_WRITE_MODE",
        "MEMORY_L3_EXECUTOR_WORKERS",
        "MEMORY_L3_MAX_PENDING_TASKS",
        "MEMORY_L3_QUEUE_WAIT_SECONDS",
    ]:
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setattr(
        dependencies,
        "settings",
        Settings(
            _env_file="/tmp/innies-memory-missing-test.env",
            openai_api_key="openai-secret",
            memory_llm_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            memory_embedding_base_url="http://embedding.example.internal:7345/v1",
            memory_embedding_api_key="",
            memory_llm_model="qwen-plus-latest",
            memory_embedding_model="zhiman-embedding",
            milvus_url="http://milvus.example.internal:19530",
            milvus_user="milvus-user",
            milvus_password="milvus-secret",
            milvus_database="innies",
            memory_milvus_collection="innies_memories_test",
            memory_backend_max_concurrent_calls=3,
        ),
    )
    service = dependencies.get_memory_service()

    assert isinstance(service.backend, Mem0LibraryMemoryBackend)
    assert service.backend.config["llm"]["config"]["api_key"] == "openai-secret"
    assert (
        service.backend.config["llm"]["config"]["openai_base_url"]
        == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    )
    assert service.backend.config["embedder"]["config"]["api_key"] == ""
    assert (
        service.backend.config["embedder"]["config"]["openai_base_url"]
        == "http://embedding.example.internal:7345/v1"
    )
    assert service.backend.config["llm"]["config"]["model"] == "qwen-plus-latest"
    assert service.backend.config["embedder"]["config"]["model"] == "zhiman-embedding"
    assert service.backend.config["vector_store"]["provider"] == "milvus"
    assert (
        service.backend.config["vector_store"]["config"]["url"]
        == "http://milvus.example.internal:19530"
    )
    assert service.backend.config["vector_store"]["config"]["token"] == "milvus-user:milvus-secret"
    assert service.backend.config["vector_store"]["config"]["db_name"] == "innies"
    assert (
        service.backend.config["vector_store"]["config"]["collection_name"]
        == "innies_memories_test"
    )
    assert service.l3_write_mode == "async"
    assert service.l3_executor_workers == 16
    assert service.l3_max_pending_tasks == 256
    assert service.l3_queue_wait_seconds == 5.0
    assert service.backend.max_concurrent_calls == 3


def test_memory_routes_call_sync_service_in_threadpool() -> None:
    source = Path("src/innies_memory/api/memory.py").read_text(encoding="utf-8")

    assert "partial(method, *args, **kwargs)" in source
    assert "ThreadPoolExecutor(" in source
    assert "asyncio.BoundedSemaphore(settings.memory_api_worker_limit)" in source
    assert "_memory_read_call_slots" in source
    assert "_memory_write_call_slots" in source
    assert "asyncio.wait_for(slots.acquire()" in source
    assert "loop.call_soon_threadsafe(slots.release)" in source
    assert "deadline = monotonic() + wait_seconds" in source
    assert "asyncio.wait_for(" in source
    assert "_run_write_memory_call(service.append, request)" in source
    assert "_run_read_memory_call(service.recall, request)" in source
    assert "_run_write_memory_call(service.delete, request)" in source


def test_real_validation_script_uses_sql_repository_not_in_memory() -> None:
    source = Path("script/real_mem0_pressure.py").read_text(encoding="utf-8")

    assert "SqlAlchemyMemoryRepository" in source
    assert "InMemoryMemoryRepository" not in source


def test_real_validation_script_reuses_configurable_mem0_backend() -> None:
    source = Path("script/real_mem0_pressure.py").read_text(encoding="utf-8")

    assert "get_memory_backend(settings)" in source
    assert "Mem0HttpMemoryBackend" not in source
    assert "QdrantClient" not in source
    assert "repository.memories.values()" not in source


def test_real_validation_preflight_reports_missing_config_and_dependency_statuses() -> None:
    from innies_memory.infra.config import Settings
    from script import real_mem0_pressure

    def ready() -> dict[str, str]:
        return {"status": "ready", "detail": "ok"}

    real_mem0_pressure.check_database = ready
    real_mem0_pressure.check_milvus = ready

    report = real_mem0_pressure.build_preflight_report(
        Settings(openai_api_key="", milvus_url="http://localhost:19530")
    )

    assert report["ready"] is False
    assert report["missing_config"] == ["OPENAI_API_KEY"]
    assert report["dependencies"]["database"]["status"] == "ready"
    assert report["dependencies"]["milvus"]["status"] == "ready"


def test_real_validation_preflight_error_includes_actionable_statuses() -> None:
    from innies_memory.infra.config import Settings
    from script import real_mem0_pressure

    def ready() -> dict[str, str]:
        return {"status": "ready", "detail": "ok"}

    def unavailable() -> dict[str, str]:
        return {"status": "not_ready", "detail": "authentication failed"}

    real_mem0_pressure.check_database = unavailable
    real_mem0_pressure.check_milvus = ready

    try:
        real_mem0_pressure.assert_preflight_ready(
            Settings(openai_api_key="", milvus_url="http://localhost:19530")
        )
    except RuntimeError as exc:
        message = str(exc)
        assert "missing_config=OPENAI_API_KEY" in message
        assert "database=not_ready(authentication failed)" in message
        assert "milvus=ready" in message
    else:
        raise AssertionError("expected missing OPENAI_API_KEY to fail preflight")


def test_real_validation_script_does_not_keep_legacy_preflight_error_text() -> None:
    source = Path("script/real_mem0_pressure.py").read_text(encoding="utf-8")

    assert "real validation requires:" not in source


def test_real_validation_cli_reports_preflight_failure_without_traceback(
    capsys, monkeypatch
) -> None:
    from script import real_mem0_pressure

    def fail_preflight() -> None:
        raise real_mem0_pressure.PreflightError(
            "real validation preflight failed: missing_config=OPENAI_API_KEY"
        )

    monkeypatch.setattr(real_mem0_pressure, "main", fail_preflight)

    exit_code = real_mem0_pressure.run_cli()

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "missing_config=OPENAI_API_KEY" in captured.err
    assert "Traceback" not in captured.err


def test_real_validation_script_generates_unique_scope_and_ids() -> None:
    from script.real_mem0_pressure import _new_run_scope

    first = _new_run_scope("20260504T010203")
    second = _new_run_scope("20260504T010204")

    assert first.user_id != second.user_id
    assert first.session_id != second.session_id
    assert first.request_id(1) != second.request_id(1)
    assert first.round_id(1) != second.round_id(1)
    assert first.operation_id("delete") != second.operation_id("delete")


def test_real_validation_delete_target_uses_any_active_memory_not_text_keywords() -> None:
    from script.real_mem0_pressure import _select_delete_target

    target = SimpleNamespace(
        memory_id="memory-1",
        backend_memory_id="backend-1",
        memory_status=MemoryStatus.ACTIVE,
        memory_text="用户更喜欢被称呼为阿鹏",
    )
    repository = SimpleNamespace(active_memories=lambda user_id, memory_scope_id: [target])

    assert _select_delete_target(repository, "user-1", "innies") is target


def test_real_validation_delete_target_fails_when_no_active_l3_memory() -> None:
    from script.real_mem0_pressure import _select_delete_target

    repository = SimpleNamespace(active_memories=lambda user_id, memory_scope_id: [])

    try:
        _select_delete_target(repository, "user-1", "innies")
    except RuntimeError as exc:
        assert "no active L3 memory" in str(exc)
    else:
        raise AssertionError("expected missing active memory to fail validation")


def test_real_validation_l3_generation_failure_points_to_mem0_provider() -> None:
    from script.real_mem0_pressure import _assert_l3_generated

    repository = SimpleNamespace(active_memories=lambda user_id, memory_scope_id: [])

    try:
        _assert_l3_generated(repository, "user-1", "innies")
    except RuntimeError as exc:
        assert "produced no active L3 memories" in str(exc)
        assert "Mem0 LLM/embedding provider" in str(exc)
    else:
        raise AssertionError("expected missing generated L3 memory to fail validation")
