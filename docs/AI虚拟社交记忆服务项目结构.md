# AI 虚拟社交记忆服务项目结构

本文档说明 Thinkback 当前工程目录和核心模块职责。

## 1. 顶层结构

```text
Thinkback/
├── alembic/                 数据库迁移
├── docs/                    架构、部署、迁移、压测指标和项目文档
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
| `dependencies.py` | 注入 `MemoryService`。默认使用 `SqlAlchemyMemoryRepository + Mem0HttpMemoryBackend`。 |
| `health.py` | `/health`、`/health/live`、`/health/ready`。 |
| `memory.py` | `/memory/append`、`/memory/recall`、`/memory/delete`、`/memory/rebuild`、任务查询。 |

`memory.py` 的端点是 async，但服务和 Mem0 adapter 是同步边界，所以通过 `run_in_threadpool` 调用，避免阻塞事件循环。

## 3. src/memory

```text
src/memory/
├── backends.py
├── repositories.py
├── schemas.py
├── service.py
└── tasks.py
```

| 文件 | 职责 |
| --- | --- |
| `schemas.py` | API 请求/响应模型、枚举、三层记忆契约。 |
| `service.py` | 记忆业务工作流：写入、召回、删除、重建、任务查询。 |
| `repositories.py` | 内存仓库和 SQLAlchemy 仓库；保存 L1 派生缓存、L2、可靠轮次、L3 业务索引和任务状态。 |
| `backends.py` | 长期记忆后端协议、Fake 后端、远程 Mem0 REST 后端。 |
| `tasks.py` | Celery 任务扩展位置。 |

关键边界：

1. `service.py` 不直接操作 SQLAlchemy model。
2. `repositories.py` 不调用 Mem0。
3. `backends.py` 不关心业务表，只封装长期记忆后端。
4. `backends.py` 只调用 Mem0 REST，不配置 Mem0 内部的 LLM、Embedding，也不直接操作 Qdrant。

## 4. src/infra

```text
src/infra/
├── cache/redis_client.py
├── database/base.py
├── database/engine.py
├── database/models.py
├── tasks/celery_app.py
├── config.py
├── logging.py
└── readiness.py
```

| 文件 | 职责 |
| --- | --- |
| `config.py` | 读取 `.env` 和环境变量，生成数据库、Redis、Mem0 REST 和共享 Qdrant 诊断配置。 |
| `database/models.py` | 五张 `tb_` 业务表的 SQLAlchemy model。 |
| `database/engine.py` | Async SQLAlchemy engine、session factory、数据库 readiness。 |
| `database/base.py` | Declarative Base，并导入 models 给 Alembic metadata 使用。 |
| `cache/redis_client.py` | Redis readiness 和客户端边界。 |
| `readiness.py` | 聚合依赖检查，包括数据库、Redis、共享 Qdrant 和 Mem0 REST。 |
| `tasks/celery_app.py` | Celery app bootstrap。 |

Thinkback 不保留 `infra/vectorstore` 业务模块。L3 写入和检索只通过远程 Mem0 REST API 进行；`readiness.py` 里的 Qdrant 访问仅用于 `/health/ready` 连通性诊断。

## 5. alembic

```text
alembic/
├── env.py
├── script.py.mako
└── versions/
    └── 20260504_0001_memory_p0_tables.py
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
└── real_mem0_pressure.py
```

真实 5 轮压测脚本，使用真实 Mem0 REST API 和 PostgreSQL。脚本不支持 fake 模式，缺少 Mem0 API 配置或依赖不可达时直接失败。

P0 压测指标、阈值、报告格式和失败处理规则见 `docs/AI虚拟社交记忆服务P0压测指标.md`。

## 7. test

```text
test/unit/
├── test_memory_api.py
├── test_memory_backends.py
├── test_memory_contracts.py
├── test_memory_end_to_end.py
├── test_memory_service.py
└── test_runtime_boundaries.py
```

重点测试覆盖：

| 测试 | 覆盖内容 |
| --- | --- |
| `test_memory_contracts.py` | 请求契约、枚举、完整轮次校验。 |
| `test_memory_backends.py` | Fake 后端、远程 Mem0 REST adapter、Mem0 范围删除安全边界。 |
| `test_memory_service.py` | 写入幂等、失败任务、删除、重建、防复活。 |
| `test_memory_api.py` | API 主流程和业务错误状态码。 |
| `test_memory_end_to_end.py` | 5 轮假后端全链路。 |
| `test_runtime_boundaries.py` | 默认仓库、线程池调用、真实压测脚本边界。 |
