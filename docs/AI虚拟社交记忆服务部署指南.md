# AI 虚拟社交记忆服务部署指南

本文档说明 Thinkback 记忆服务的本地部署、配置、验证和真实压测方式。

## 1. 运行组件

首版运行至少需要：

| 组件 | 用途 |
| --- | --- |
| FastAPI app | 对外提供 `/memory/*` 和健康检查。 |
| PostgreSQL | 保存 L2、可靠轮次、L3 业务索引和任务状态。 |
| Redis | Celery broker/result backend 和运行时基础设施。 |
| Qdrant | Mem0 的向量存储。 |
| Mem0 | L3 长期记忆引擎。 |
| OpenAI 兼容 LLM/Embedding | Mem0 抽取和检索所需模型。 |

## 2. 环境变量

最小配置见 `.env.example`。生产环境必须显式设置：

```bash
POSTGRES_HOST=
POSTGRES_PORT=
POSTGRES_USER=
POSTGRES_PASSWORD=
POSTGRES_DATABASE=

REDIS_HOST=
REDIS_PORT=
CELERY_BROKER_URL=
CELERY_RESULT_BACKEND=

QDRANT_URL=
QDRANT_API_KEY=
MEMORY_QDRANT_COLLECTION=thinkback_memories

MEMORY_LLM_MODEL=gpt-4o-mini
MEMORY_LLM_BASE_URL=https://api.openai.com/v1
MEMORY_LLM_API_KEY=
MEMORY_EMBEDDING_MODEL=text-embedding-3-small
MEMORY_EMBEDDING_API_KEY=
```

`OPENAI_API_KEY` 不是业务配置的主入口。真实压测脚本会在缺失 `OPENAI_API_KEY` 时用 `MEMORY_LLM_API_KEY` 补齐，以兼容部分 OpenAI SDK 默认读取方式。

## 3. 本地启动

安装依赖：

```bash
poetry install
```

启动依赖：

```bash
docker compose up -d postgres redis qdrant
```

执行数据库迁移：

```bash
PYTHONPATH=src .venv/bin/alembic upgrade head
```

启动 API：

```bash
PYTHONPATH=src .venv/bin/uvicorn api.app:app --app-dir src --host 0.0.0.0 --port 8000
```

启动 worker：

```bash
PYTHONPATH=src .venv/bin/celery -A infra.tasks.celery_app.celery_app worker -l info
```

## 4. 健康检查

```bash
curl http://localhost:8000/health
curl http://localhost:8000/health/live
curl http://localhost:8000/health/ready
```

就绪检查会检查数据库、Redis 和 Qdrant。当前本机如果出现：

```text
password authentication failed for user "postgres"
```

说明应用连到了一个可达但密码不匹配的 PostgreSQL。常见原因是本机已有服务占用 `5432`，而不是 compose 内的 `thinkback-postgres`。

## 5. 真实 5 轮压测

脚本：

```bash
PYTHONPATH=src .venv/bin/python script/real_mem0_pressure.py
```

它会执行：

1. 检查 `MEMORY_LLM_API_KEY`、`MEMORY_EMBEDDING_API_KEY`、`QDRANT_URL`。
2. 检查 Qdrant 可达。
3. 检查 PostgreSQL 可达。
4. 使用真实 `SqlAlchemyMemoryRepository` 和真实 `Mem0MemoryBackend`。
5. 追加 5 轮对话。
6. 召回长期偏好。
7. 删除一条目标记忆。
8. 重建 L2/L3。
9. 再次召回确认链路可用。

脚本不支持 fake 模式。缺少密钥时会失败退出，例如：

```text
RuntimeError: real pressure test requires: MEMORY_LLM_API_KEY, MEMORY_EMBEDDING_API_KEY
```

这不是测试通过，也不是代码失败，而是环境未满足真实压测条件。

## 6. 生产注意事项

1. API 默认使用 PostgreSQL 业务仓库，不允许生产路径退回内存仓库。
2. L3 通过 Mem0 管理，业务服务不得直接写 Qdrant。
3. 范围删除不得调用 Mem0 `delete_all()`；服务按业务索引逐条调用 Mem0 `delete()`。
4. 删除、重建和写入失败时，`tb_memory_task` 必须记录 `failed` 和 `last_error`。
5. 所有 `/memory/*` 路由会把同步 Mem0/数据库工作放入线程池，避免阻塞 FastAPI 事件循环。
6. 当前 SQLAlchemy async engine 使用 `NullPool`，避免同步服务边界在线程池里复用 asyncpg 连接导致跨事件循环问题；生产如要启用连接池，需要把仓库改为全异步边界后再调整。
7. 强治理阶段的语义抑制、写入栅栏和衰减任务尚未启用，不能在隐私强合规场景中把首版当成最终治理方案。
