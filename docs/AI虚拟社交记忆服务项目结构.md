# AI 虚拟社交记忆服务项目结构

本文档说明 Thinkback 当前工程目录和核心模块职责。

## 1. 顶层结构

```text
Thinkback/
├── alembic/                 数据库迁移
├── docs/                    架构、部署、迁移、评测方案和项目文档
├── k8s/                     Kubernetes 部署清单
├── script/                  运维和真实链路脚本
├── src/                     应用源码
├── test/                    单元测试和端到端假后端测试
├── docker-compose.yml       本地依赖栈
├── Dockerfile               应用镜像
├── Makefile                 常用命令
├── pyproject.toml           Python 依赖和工具配置
└── poetry.lock              锁定依赖版本
```

## 2. src/api

```text
src/api/
├── app.py
├── dependencies.py
├── health.py
└── memory.py
```

| 文件 | 职责 |
| --- | --- |
| `app.py` | FastAPI app factory、路由注册、trace/access log 中间件。 |
| `dependencies.py` | 注入 `MemoryService`。默认使用 `SqlAlchemyMemoryRepository + Mem0LibraryMemoryBackend`。 |
| `health.py` | `/health`、`/health/live`、`/health/ready`。 |
| `memory.py` | `/memory/append`、`/memory/recall`、`/memory/delete`、`/memory/rebuild`、任务查询。 |

`memory.py` 的端点是 async，但服务和 Mem0 adapter 是同步边界，所以通过 `run_in_threadpool` 调用，避免阻塞事件循环。

## 3. src/memory

```text
src/memory/
├── backends.py
├── repositories.py
├── schemas.py
└── service.py
```

| 文件 | 职责 |
| --- | --- |
| `schemas.py` | API 请求/响应模型、枚举、三层记忆契约。 |
| `service.py` | 记忆业务工作流：写入、召回、删除、重建、任务查询。 |
| `repositories.py` | 内存仓库和 SQLAlchemy 仓库；保存 L1 派生缓存、L2、可靠轮次、L3 业务索引和任务状态。 |
| `backends.py` | 长期记忆后端协议、Fake 后端、Mem0 Library 后端。 |

关键边界：

1. `service.py` 不直接操作 SQLAlchemy model。
2. `repositories.py` 不调用 Mem0。
3. `backends.py` 不关心业务表，只封装长期记忆后端。
4. `backends.py` 只调用 Mem0 Library，不直接操作 Qdrant。

## 4. src/infra

```text
src/infra/
├── cache/redis_client.py
├── database/base.py
├── database/engine.py
├── database/models.py
├── config.py
├── logging.py
└── readiness.py
```

| 文件 | 职责 |
| --- | --- |
| `config.py` | 读取 `.env` 和环境变量，生成数据库、Redis、OpenAI、Mem0 Library 和共享 Qdrant 配置。 |
| `database/models.py` | 五张 `tb_` 业务表的 SQLAlchemy model。 |
| `database/engine.py` | Async SQLAlchemy engine、session factory、数据库 readiness。 |
| `database/base.py` | Declarative Base，并导入 models 给 Alembic metadata 使用。 |
| `cache/redis_client.py` | Redis readiness 和客户端边界。 |
| `readiness.py` | 聚合依赖检查，包括数据库、Redis、共享 Qdrant 和 Mem0 Library。 |

Thinkback 不保留 `infra/vectorstore` 业务模块。L3 写入和检索只通过 Mem0 Library 进行；业务代码不得绕过 Mem0 直接写 Qdrant。

## 5. alembic

```text
alembic/
├── env.py
├── script.py.mako
└── versions/
    └── <首版记忆服务表结构迁移>
```

首版迁移创建：

```text
tb_current_summary
tb_summary_round_journal
tb_memory
tb_user_character_state
tb_memory_task
```

## 6. script

```text
script/
├── db_migrate.py
├── real_mem0_pressure.py
├── real_mem0_quality_regression.py
├── real_mem0_p0_short_pressure.py
├── real_mem0_p0_preprod_pressure.py
├── real_mem0_p0_fault_injection.py
├── real_mem0_p0_representative_replay.py
├── run_real_mem0_quality_evaluation.py
└── build_p0_pressure_final_report.py
```

脚本按用途分为四类：

| 脚本 | 用途 |
| --- | --- |
| `db_migrate.py` | 数据库迁移辅助入口。 |
| `real_mem0_pressure.py` | 真实 5 轮主链路验证，使用真实 Mem0 Library、OpenAI-compatible endpoint、Qdrant 和 PostgreSQL。 |
| `real_mem0_quality_regression.py` | 首版主链路记忆质量回归评测。 |
| `real_mem0_p0_short_pressure.py` | P0 短压测套件。 |
| `real_mem0_p0_preprod_pressure.py` | P0 生产前 baseline、stress、spike、soak 阶段报告生成。 |
| `real_mem0_p0_fault_injection.py` | P0 依赖故障注入报告。 |
| `real_mem0_p0_representative_replay.py` | P0 代表性样本回放报告。 |
| `run_real_mem0_quality_evaluation.py` | 质量评测报告汇总和对比入口。 |
| `build_p0_pressure_final_report.py` | P0 压测最终报告生成。 |

真实链路类脚本不支持 fake 模式，缺少 OpenAI/Qdrant 配置或依赖不可达时直接失败。

首版记忆质量评测、报告字段、报告模板和失败处理规则见
`docs/AI虚拟社交记忆服务首版主链路质量评测方案.md`、
`docs/AI虚拟社交记忆服务首版主链路质量评测报告字段.md` 和
`docs/AI虚拟社交记忆服务首版主链路质量评测报告模板.md`。
性能与稳定性测试见 `docs/AI虚拟社交记忆服务性能与稳定性测试方案.md`，但当前仓库不再保留旧的稳定性兼容入口脚本。

## 7. test

```text
test/unit/
├── test_api.py
├── test_api_docs.py
├── test_config.py
├── test_memory_api.py
├── test_memory_backends.py
├── test_memory_contracts.py
├── test_memory_end_to_end.py
├── test_memory_service.py
├── test_readiness.py
├── test_real_quality_evaluation.py
├── test_runtime_boundaries.py
└── test_scaffold_files.py
```

重点测试覆盖：

| 测试 | 覆盖内容 |
| --- | --- |
| `test_memory_contracts.py` | 请求契约、枚举、完整轮次校验。 |
| `test_memory_backends.py` | Fake 后端、Mem0 Library adapter、Mem0 范围删除安全边界。 |
| `test_memory_service.py` | 写入幂等、失败任务、删除、重建、防复活。 |
| `test_memory_api.py` | API 主流程和业务错误状态码。 |
| `test_memory_end_to_end.py` | 5 轮假后端全链路。 |
| `test_api.py` / `test_api_docs.py` | 健康检查、OpenAPI 和文档入口。 |
| `test_config.py` | 运行配置默认值和环境变量解析。 |
| `test_readiness.py` | readiness 聚合检查和依赖状态。 |
| `test_real_quality_evaluation.py` | 真实质量评测报告、指标、P0 压测报告生成逻辑。 |
| `test_runtime_boundaries.py` | 默认仓库、线程池调用、真实验证脚本边界。 |
| `test_scaffold_files.py` | Docker、Kubernetes、项目脚手架文件存在性和关键配置。 |
