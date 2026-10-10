"""schemathesis API 模糊测试：按 OpenAPI 契约 fuzz 全部端点。

审计层（``make test-fuzz``），不进 make check：hypothesis 驱动，时长随示例数浮动。
进程内 ASGI 调用（无网络）；仓储/后端注入 in-memory fake，请求绝不触真实
PG / Milvus / LLM。判定口径：响应状态码与结构符合 OpenAPI schema、无未声明 5xx。
"""

from __future__ import annotations

import schemathesis
from hypothesis import HealthCheck, settings
from schemathesis.openapi.checks import AllowHeaderMismatch, RejectedPositiveData

from thinkback.api import dependencies as dependencies_module
from thinkback.api.app import create_app
from thinkback.memory.backends import FakeMemoryBackend
from thinkback.memory.repositories import InMemoryMemoryRepository
from thinkback.memory.service import MemoryService

# schema 加载会经 TestClient 走一遍 lifespan（结束时 reset 单例）；
# 加载完成后注入假服务，之后 fuzz 请求全部落在 in-memory 实现上。
schema = schemathesis.openapi.from_asgi("/openapi.json", app=create_app())

dependencies_module._memory_service = MemoryService(
    repository=InMemoryMemoryRepository(),
    backend=FakeMemoryBackend(),
    l3_write_mode="sync",
)


@schema.parametrize()
@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
def test_openapi_conformance(case) -> None:  # type: ignore[no-untyped-def]
    response = case.call()
    case.validate_response(
        response,
        excluded_checks=[
            # 框架行为：FastAPI 按 method 拆分路由，405 的 Allow 只含命中的那条
            # 路由的方法集，Starlette 的固有语义，非业务缺陷。
            AllowHeaderMismatch,
            # 跨字段业务规则无法用 OpenAPI 表达，由单测钉死：append 需完整
            # user→assistant 轮；delete scope=all 禁带 memory_id；content 去
            # 空白后非空。新拒绝规则先试 schema 表达（minLength 等），确实不可
            # 表达才加进本排除。
            RejectedPositiveData,
        ],
    )
