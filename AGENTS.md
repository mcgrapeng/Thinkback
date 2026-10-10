# Thinkback

Python 3.12 记忆服务（FastAPI + gRPC + SQLAlchemy）与 React 管理台。工程命令统一走 `Makefile`。

## 测试纪律

完成定义：`make check` 全绿（format-check + ruff + mypy --strict src + 覆盖率 ≥92）。

- 行为变更随附测试：先写失败的测试再改实现；新 API 端点至少覆盖 happy path 与一条错误路径。
- 仓储双实现同行为：改动 `InMemoryMemoryRepository` 或 `SqlAlchemyMemoryRepository` 后跑
  `pytest tests/unit/test_repository_contract.py`——契约测试红了就是行为漂移，以 SQL 实现为生产真值。
- 测试确定性：无网络、无真实 LLM / Milvus / Mem0，用 `FakeMemoryBackend` 与内存仓储。
- 覆盖率只升不降：`make coverage` 有 `COVERAGE_FLOOR`（92）门禁；`memory_pb2*.py` 为生成代码，不计入。
- 纯函数（排序键/指纹/摘要窗口/衰减判定）用 hypothesis 属性测试钉不变量，模式见
  `tests/unit/test_domain_properties.py`。
- 找测试模式：契约用例看 `tests/unit/test_repository_contract.py`，服务级用例看 `tests/unit/test_memory_service.py`。

## 技能触发

写测试前按分支读对应 skill（都在 `~/.agents/skills`，读完再动手）：

| 分支 | Skill |
| --- | --- |
| e2e（tests/e2e、Playwright） | `playwright-best-practices` |
| 前端组件测试（web/、vitest） | `vitest` |
| pytest 用例/fixture/mocking | `python-testing-patterns` |
| 补覆盖率缺口、--cov-fail-under | `pytest-coverage` |

## 常用命令

| 命令 | 用途 |
| --- | --- |
| `make test-fast` | 本地快环：testmon 只跑受改动影响的用例 |
| `make test` | 确定性单元/集成套件 |
| `make coverage` | 套件 + 覆盖率门禁（提交前必跑） |
| `make check` | 全部门禁，等价 CI |
| `make test-ui` / `make test-e2e` | 前端组件 / 端到端冒烟（e2e 含 axe a11y 扫描） |
| `make test-fuzz` | API 契约模糊测试（schemathesis，审计层） |
| `make audit` | 供应链 CVE 扫描（需网络；CI 门禁） |
| `make eval` | 自包含质量评测 + 基线门禁（确定性；真实链路评测见 tests/script/） |
