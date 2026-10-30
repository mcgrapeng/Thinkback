import os
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck
from hypothesis import settings as hypothesis_settings

from thinkback.api import app as app_module
from thinkback.api import dependencies as dependencies_module
from thinkback.api import health as health_module
from thinkback.api import memory as memory_module
from thinkback.api.app import create_app
from thinkback.infra import config as config_module
from thinkback.infra import logging as logging_module
from thinkback.infra import readiness as readiness_module
from thinkback.infra.database import engine as engine_module

# hypothesis 的样例数与 deadline 在这里统一设，用例文件里不再各写一份。
#
# - ``deadline=None``：这些属性测试跑的是纯函数，但里面夹着槽位抽取的正则，
#   单次执行偶尔会超过 hypothesis 默认的 200ms 上限 —— 那不是被测代码变慢，
#   是 deadline 太紧，会变成随机 flaky。
# - 变异审计下样例数压到 20：每个变异体都要把选中的测试整套重跑一遍，
#   1000 个变异体 × 200 个样例 = 门禁跑几小时。20 个样例足以杀掉真变异体
#   —— 杀不掉的那些是「样例再多也杀不掉」的等价变异，本来就该登记为存活。
# - 变异审计下关掉 ``differing_executors`` 健康检查：mutmut 用多个 worker
#   并行跑同一批用例，同一个属性测试天然会在不同 worker 进程里各跑一遍。
#   这个检查防的是「同一个测试被不同执行器跑 → 回放不稳定」，指的是 flaky
#   测试；这里跑的是纯函数属性测试（round_sort_key 之类），没有共享状态，
#   并行执行不会让它变得不稳定。
hypothesis_settings.register_profile("thinkback", max_examples=100, deadline=None)
hypothesis_settings.register_profile(
    "thinkback-mutation",
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.differing_executors],
)
hypothesis_settings.load_profile(
    "thinkback-mutation" if os.environ.get("MUTANT_UNDER_TEST") else "thinkback"
)


@pytest.fixture(autouse=True)
def isolate_runtime_settings(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    for variable in [
        "APP_NAME",
        "APP_VERSION",
        "ENVIRONMENT",
        "DEBUG",
        "LOG_LEVEL",
        "MEMORY_EMBEDDING_BASE_URL",
    ]:
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setitem(config_module.Settings.model_config, "env_file", None)
    config_module.get_settings.cache_clear()
    config_module.settings = config_module.get_settings()
    # disable embedded gRPC server in all HTTP tests to avoid port binding
    monkeypatch.setattr(config_module.settings, "grpc_enabled", False)
    monkeypatch.setattr(app_module, "settings", config_module.settings)
    monkeypatch.setattr(health_module, "settings", config_module.settings)
    monkeypatch.setattr(memory_module, "settings", config_module.settings)
    monkeypatch.setattr(dependencies_module, "settings", config_module.settings)
    monkeypatch.setattr(readiness_module, "settings", config_module.settings)
    monkeypatch.setattr(logging_module, "settings", config_module.settings)
    monkeypatch.setattr(engine_module, "settings", config_module.settings)


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    # run_startup_tasks=False：单测不跑迁移、不建 Milvus 库、不做启动恢复，
    # 因此不依赖真实 PG / Milvus / LLM —— 同一份代码在任何机器上结果一致。
    with TestClient(create_app(run_startup_tasks=False)) as test_client:
        yield test_client
