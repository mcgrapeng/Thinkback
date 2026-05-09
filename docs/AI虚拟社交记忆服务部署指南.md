# AI 虚拟社交记忆服务部署指南

本文档说明 Thinkback 记忆服务的本地部署、配置、验证和真实压测方式。

## 1. 运行组件

首版运行至少需要：

| 组件 | 用途 |
| --- | --- |
| FastAPI app | 对外提供 `/memory/*` 和健康检查。 |
| PostgreSQL | 保存 L2、可靠轮次、L3 业务索引和任务状态。 |
| Redis | Celery broker/result backend 和运行时基础设施。 |
| Mem0 Library | 嵌入 Thinkback 进程内的 L3 长期记忆引擎。 |
| Qdrant | 独立向量数据库。Mem0 Library 用它承载 L3 向量，Thinkback 也会做就绪探测。 |
| OpenAI | Mem0 Library 使用 OpenAI 做记忆抽取和向量化。 |

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

QDRANT_URL=https://qdrant.example.internal
QDRANT_API_KEY=

OPENAI_API_KEY=
MEMORY_QDRANT_COLLECTION=memories_qwen_1024
MEMORY_EMBEDDING_DIMS=1024
MEMORY_LLM_MODEL=qwen3.5-flash
MEMORY_EMBEDDING_MODEL=text-embedding-v4
MEM0_HISTORY_DB_PATH=.mem0/history.db
MEMORY_L3_WRITE_MODE=async
```

Mem0 不再作为独立 REST Server 部署。Thinkback 通过 `mem0ai` Library 直接调用 Mem0 能力，Mem0 Library 再访问 OpenAI 和 Qdrant。生产不要把 `QDRANT_URL` 写死为 `localhost`，应使用内网域名、服务发现地址或受控 HTTPS 地址，例如 `https://qdrant.example.internal`。

Qdrant 按独立基础设施配置，未来可以与 Thinkback 部署在不同主机上。Mem0 Library 负责 L3 向量写入和语义检索；Thinkback 的 readiness 也会访问 Qdrant `/collections` 做连通性诊断。

当前 `mem0ai` Library 在 `url` 模式下要求同时传入 Qdrant `api_key`。因此生产推荐配置是 `QDRANT_URL=https://...` + `QDRANT_API_KEY=...`。本地无鉴权压测可以使用 `QDRANT_URL=http://localhost:6333` 且不填 `QDRANT_API_KEY`，Thinkback 会按 Mem0 支持的 `host/port` 方式连接。

`MEM0_HISTORY_DB_PATH` 是 Mem0 Library 的本地历史数据库路径。容器部署时建议挂载可写目录，避免应用进程没有写权限。

`MEMORY_L3_WRITE_MODE=async` 是首版推荐配置。append 请求同步完成可靠轮次存储、L1/L2 更新和 P0 槽位索引，Mem0 L3 抽取在后台任务中沉淀，避免外部 LLM/Embedding 抖动把写入接口拖到超时。

## 3. 本地启动

安装依赖：

```bash
poetry install
```

启动依赖：

```bash
docker compose up -d postgres redis
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

就绪检查会检查数据库、Redis、共享 Qdrant 和 Mem0 Library。当前本机如果出现：

```text
password authentication failed for user "postgres"
```

说明应用连到了一个可达但密码不匹配的 PostgreSQL。常见原因是本机已有服务占用 `5432`，而不是 compose 内的 `thinkback-postgres`。

## 5. 真实 5 轮压测

压测指标、门禁阈值和失败处理规则见 `docs/AI虚拟社交记忆服务P0压测指标.md`。真实压测结论必须同时看召回质量、删除重建、隔离、幂等、失败重试和延迟指标，不能只看接口是否返回 200。

脚本：

```bash
PYTHONPATH=src .venv/bin/python script/real_mem0_pressure.py
```

它会执行：

1. 检查 `OPENAI_API_KEY`、`QDRANT_URL`。
2. 检查 Mem0 Library 能否通过 OpenAI 和 Qdrant 完成基础调用。
3. 检查 PostgreSQL 可达。
4. 使用真实 `SqlAlchemyMemoryRepository` 和真实 Mem0 Library 后端。
5. 追加 5 轮对话。
6. 召回长期偏好。
7. 删除一条目标记忆。
8. 重建 L2/L3。
9. 再次召回确认链路可用。

脚本不支持 fake 模式。缺少密钥时会失败退出，例如：

```text
RuntimeError: real pressure test requires: OPENAI_API_KEY
```

这不是测试通过，也不是代码失败，而是环境未满足真实压测条件。

## 6. 生产注意事项

1. API 默认使用 PostgreSQL 业务仓库，不允许生产路径退回内存仓库。
2. Mem0 以 Library 方式嵌入 Thinkback；Qdrant 独立部署，不能假设和 Thinkback 同主机。
3. Thinkback 只通过 Mem0 Library 使用 Qdrant，不得绕过 Mem0 做 L3 语义检索。
4. 范围删除不得调用 Mem0 `delete_all()`；服务按业务索引逐条调用 Mem0 `delete()`。
5. 删除、重建和写入失败时，`tb_memory_task` 必须记录 `failed` 和 `last_error`。
6. 所有 `/memory/*` 路由会把同步 Mem0/数据库工作放入线程池，避免阻塞 FastAPI 事件循环。
7. 当前 SQLAlchemy async engine 使用 `NullPool`，避免同步服务边界在线程池里复用 asyncpg 连接导致跨事件循环问题；生产如要启用连接池，需要把仓库改为全异步边界后再调整。
8. 强治理阶段的语义抑制、写入栅栏和衰减任务尚未启用，不能在隐私强合规场景中把首版当成最终治理方案。
