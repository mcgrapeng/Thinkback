"""schemathesis API 模糊测试：按 OpenAPI 契约 fuzz 全部端点。

审计层（``make test-fuzz``），不进 make check：hypothesis 驱动，时长随示例数浮动。
进程内 ASGI 调用（无网络）；仓储/后端注入 in-memory fake，请求绝不触真实
PG / Milvus / LLM。判定口径：响应状态码与结构符合 OpenAPI schema、无未声明 5xx。
"""

from __future__ import annotations

import schemathesis
from hypothesis import HealthCheck, settings
from schemathesis.config import (
    ChecksConfig,
    PositiveDataAcceptanceConfig,
    ProjectConfig,
    ProjectsConfig,
    SchemathesisConfig,
    SimpleCheckConfig,
)
from schemathesis.config._checks import (  # noqa: PLC2701 — 未公开导出的检查配置类
    NegativeDataRejectionConfig,
)

from thinkback.api import dependencies as dependencies_module
from thinkback.api.app import create_app
from thinkback.memory.backends import FakeMemoryBackend
from thinkback.memory.repositories import InMemoryMemoryRepository
from thinkback.memory.service import MemoryService

# 三类已知豁免（弱区留痕，见 docs/report/testing-toolchain-audit.md）：
# - allow_header_conformance：FastAPI 按 method 拆分路由，405 的 Allow 只含
#   命中路由的方法集，Starlette 固有语义。
# - positive_data_acceptance 放行 422：跨字段业务规则无法用 OpenAPI 表达
#   （append 需完整 user→assistant 轮；delete scope=all 禁带 memory_id；
#   content 去空白后非空），由单测钉死。新拒绝规则先试 schema 表达
#   （minLength 等），确实不可表达再放宽。
# - negative_data_rejection 关闭：pydantic 默认宽松 coercion（字符串数字/
#   布尔被接受）是全模型约定，收紧为 strict 属 API 策略变更，列为 backlog。
fuzz_config = SchemathesisConfig(
    projects=ProjectsConfig(
        default=ProjectConfig(
            checks=ChecksConfig(
                allow_header_conformance=SimpleCheckConfig(enabled=False),
                negative_data_rejection=NegativeDataRejectionConfig(enabled=False),
                positive_data_acceptance=PositiveDataAcceptanceConfig(
                    expected_statuses=["2xx", "3xx", 401, 403, 404, 409, 422, 429, "5xx"]
                ),
            ),
        )
    ),
)

# schema 加载会经 TestClient 走一遍 lifespan（结束时 reset 单例）；
# 加载完成后注入假服务，之后 fuzz 请求全部落在 in-memory 实现上。
schema = schemathesis.openapi.from_asgi("/openapi.json", app=create_app(), config=fuzz_config)

dependencies_module._memory_service = MemoryService(
    repository=InMemoryMemoryRepository(),
    backend=FakeMemoryBackend(),
    l3_write_mode="sync",
)
# schemathesis 的 ASGI transport 每个用例都走 lifespan，shutdown 会 reset
# 单例并让后续请求重建真实 SqlAlchemy/Mem0 后端。摘除 reset，假服务常驻。
dependencies_module.reset_memory_service = lambda: None  # type: ignore[assignment]


@schema.parametrize()
@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
def test_openapi_conformance(case) -> None:  # type: ignore[no-untyped-def]
    response = case.call()
    case.validate_response(response)
