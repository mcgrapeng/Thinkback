# Innies记忆服务开发手册

本文面向参与 `innies-memory` 开发、联调、验证和本地启动的工程师。当前工程提供 Innies 记忆服务首版主链路：写入完整 user -> assistant 轮次，维护 L1/L2 会话上下文，通过 Mem0 Library、LLM、Embedding 和 Milvus 沉淀 L3 长期记忆，并通过 FastAPI 对外提供记忆写入、召回、删除、重建和健康检查接口。

## 1. 开发环境准备

工程使用 Python 3.11+ 与 Poetry 管理依赖。首次拉取代码后执行：

```bash
poetry install
```

常用 Makefile 入口：

```bash
make run
make worker
make test
make lint
make real-test
```

如果只安装运行时依赖，可执行 `make install`；日常开发推荐执行 `make dev`，它会安装开发依赖并注册 pre-commit hook。

## 2. 本地配置

本地调试可以复制 `.env.example` 为 `.env`，或直接在启动命令前注入环境变量。当前本机约定所有需要 PostgreSQL 的工程优先使用共享 `liaohe-postgresql`，因此 `innies-memory` 本地数据库配置为：

```bash
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_USER=postgres
POSTGRES_PASSWORD=<local-postgres-password>
POSTGRES_DATABASE=innies-memory
```

本地 Redis、Milvus 和模型链路按当前约定使用：

```bash
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_DB=0
REDIS_PASSWORD=<local-redis-password>
MILVUS_URL=http://localhost:19530
MILVUS_DATABASE=default
MEMORY_MILVUS_COLLECTION=innies_memory
MEMORY_LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
MEMORY_LLM_MODEL=qwen-plus-latest
MEMORY_EMBEDDING_BASE_URL=http://<embedding-host>:7345/v1
MEMORY_EMBEDDING_MODEL=zhiman-embedding
MEMORY_EMBEDDING_DIMS=1024
```

`ENVIRONMENT=development` 且 `OPENAI_API_KEY` 为空时，服务可以启动，Swagger UI 和基础健康检查也可以访问。
此时 `/health/ready` 把 `mem0` 和 `milvus` 标记为 `ready: skipped`，整体仍返回 200，便于本地最小依赖（只起 `postgres + redis`）验证 V1 业务路径（写入轮次、L1/L2 召回、单条删除）。
`ENVIRONMENT=production` 时不允许跳过 L3 凭据，缺少 `OPENAI_API_KEY` 会让 `/health/ready` 返回 503。
完整 L3 链路（跨会话长期记忆）必须提供真实 `OPENAI_API_KEY` 和可达的 Milvus、Embedding endpoint。

`MEMORY_BACKEND_MAX_CONCURRENT_CALLS` 是单进程内 Mem0 Library 后端调用并发上限。当前本地默认值为 `4`，它是保守限流，不代表最终生产容量。是否调大应以目标主机、LLM、Embedding、Milvus 和 PostgreSQL 的压测结果为准。
当前仓库默认 `MILVUS_DATABASE=default`、`MEMORY_MILVUS_COLLECTION=innies_memory`；如果本地共享实例使用其他库名或 collection，启动前要在 `.env` 里显式覆盖。

## 3. 本地依赖与共享 PostgreSQL

当前本机共享 PostgreSQL 容器名为 `liaohe-postgresql`，监听 `localhost:5432`。确认容器状态：

```bash
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Ports}}\t{{.Status}}'
```

查看 PostgreSQL 日志：

```bash
docker logs --tail 120 liaohe-postgresql
```

确认当前工程会连接到哪个数据库：

```bash
.venv/bin/python - <<'PY'
from innies_memory.infra.config import Settings

s = Settings()
print(s.postgres_host, s.postgres_port, s.postgres_database)
print(s.database_url.replace(s.postgres_password, "***"))
PY
```

如果重建过 `liaohe-postgresql`，需要重新创建 `innies-memory` 数据库，再执行 Alembic 迁移。只创建数据库时可以连接 admin 库 `postgres`：

```bash
.venv/bin/python - <<'PY'
import asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

async def main() -> None:
    engine = create_async_engine(
        "postgresql+asyncpg://postgres:<local-postgres-password>@localhost:5432/postgres",
        isolation_level="AUTOCOMMIT",
    )
    async with engine.connect() as conn:
        exists = (
            await conn.execute(
                text("select 1 from pg_database where datname = 'innies-memory'")
            )
        ).scalar()
        if not exists:
            await conn.execute(text('CREATE DATABASE "innies-memory"'))
            print("created innies-memory")
        else:
            print("exists innies-memory")
    await engine.dispose()

asyncio.run(main())
PY
```

如果 PostgreSQL 数据目录缺文件，例如日志出现 `could not open file "base/...": No such file or directory`、`checkpoint request failed` 或数据库变为 invalid，说明不是 Alembic 脚本问题，而是共享 PostgreSQL 数据目录损坏。当前数据目录挂载点是：

```text
/Users/zhangpeng/workspace/postgre
```

处理原则：

- 不直接删除旧目录，先停容器并改名备份为 `postgre.corrupt-backup-<timestamp>`。
- 重建同名 `liaohe-postgresql` 后，再重新创建各项目数据库并分别执行各自迁移。
- 当前工程只依赖 `innies-memory` 数据库；其他项目数据库可以先创建空库恢复连接入口，但表结构仍应由各项目自己的迁移脚本负责。

## 4. 数据库迁移

执行迁移：

```bash
.venv/bin/alembic upgrade head
```

查看当前迁移版本：

```bash
.venv/bin/alembic current
```

当前正常结果应为：

```text
20260512_0002 (head)
```

查看业务表：

```bash
.venv/bin/python - <<'PY'
import asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from innies_memory.infra.config import Settings

async def main() -> None:
    s = Settings()
    engine = create_async_engine(s.database_url)
    async with engine.connect() as conn:
        version = (await conn.execute(text("select version_num from alembic_version"))).scalar()
        tables = (
            await conn.execute(
                text("select tablename from pg_tables where schemaname='public' order by tablename")
            )
        ).scalars().all()
        print("alembic_version=", version)
        print("tables=", ",".join(tables))
    await engine.dispose()

asyncio.run(main())
PY
```

正常表包括：

```text
alembic_version
ins_current_summary
ins_memory
ins_memory_task
ins_summary_round_journal
```

## 5. 启动 FastAPI 与 Swagger UI

本地推荐使用 `127.0.0.1` 绑定，避免误暴露开发服务：

```bash
.venv/bin/uvicorn innies_memory.api.app:app --host 127.0.0.1 --port 8000
```

启动后访问：

```text
http://127.0.0.1:8000/docs
http://127.0.0.1:8000/redoc
http://127.0.0.1:8000/openapi.json
```

健康检查：

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/health/live
curl http://127.0.0.1:8000/health/ready
```

`GET /health/live` 只证明 API 进程存活。`GET /health/ready` 会检查 PostgreSQL、Redis、Milvus 和 Mem0 Library 配置。Mem0 Library 检查不执行 add/delete 写入探针，避免 readiness 消耗 LLM、Embedding 或向量库写入资源。

如果 Mem0 Library 配置无法构造，`/health/ready` 可能返回类似：

```text
mem0 library readiness check failed
```

这说明数据库、Redis、Milvus 可能已经 ready，但 Mem0 Library 的本地配置或依赖包不满足。只验证 Swagger、OpenAPI 和基础路由时可以接受这个状态；验证完整 L3 写入和召回时不能接受。

如果要调试单次请求链路，可以显式带上 `X-Request-Id` 或 `X-Trace-Id`：

```bash
curl -H "X-Request-Id: trace-debug-001" http://127.0.0.1:8000/health/ready
```

服务会把入站 trace 回写到 `X-Trace-Id` 响应头，日志也会带 `trace_id` 字段，便于串联同一请求的 API、线程池后台任务和排障日志。

Makefile 中的默认启动命令是：

```bash
make run
```

## 6. 验证工程

提交前至少运行以下质量门禁：

```bash
.venv/bin/python -m pytest test/unit -q
.venv/bin/python -m compileall -q src script alembic test
```

如果只改 API 文档、Swagger 注释或手册，至少运行：

```bash
.venv/bin/python -m pytest test/unit/test_api_docs.py test/unit/test_scaffold_files.py -q
```

如果改动数据库模型或 Alembic 迁移，额外运行：

```bash
.venv/bin/alembic upgrade head
.venv/bin/alembic current
```

如果改动运行配置、健康检查或依赖探测，额外启动 API 并访问：

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/openapi.json
curl http://127.0.0.1:8000/health/ready
```

## 7. 真实主链路验证

真实主链路验证会访问 PostgreSQL、Redis、Milvus、LLM、Embedding 和 Mem0 Library，不支持 fake 模式。运行前需要确认：

- `OPENAI_API_KEY` 已配置。
- `POSTGRES_DATABASE=innies-memory` 已迁移到 head。
- `REDIS_HOST / REDIS_PORT / REDIS_PASSWORD` 与本地 Redis 一致。
- `MILVUS_URL=http://localhost:19530` 可访问。
- `MILVUS_DATABASE=default` 可用。
- `MEMORY_EMBEDDING_DIMS` 与 embedding 服务输出一致。

运行：

```bash
make real-test
```

脚本会追加 5 轮对话、触发 L3 生成、召回长期偏好、删除目标记忆、重建 L2/L3 并再次召回确认链路可用。

缺少密钥时会失败退出，例如：

```text
real validation preflight failed: missing_config=OPENAI_API_KEY database=ready redis=ready milvus=ready
```

这不是测试通过，也不是代码失败，而是环境未满足真实验证条件。

## 8. 常见问题

### Swagger 能打开，但 `/health/ready` 返回 503

先看 ready 响应里的 `dependencies`。如果 `database`、`redis`、`milvus` 是 ready，但 `mem0` 是 not_ready 且 detail 包含 `mem0 library readiness check failed`，说明 Mem0 Library 本地配置、依赖包或适配器构造失败。
若本地 `ENVIRONMENT=development` 且未配置 `OPENAI_API_KEY`，`mem0` 和 `milvus` 应自动标记 `ready: skipped`，不会让整体 ready 返回 503；此时 503 通常意味着 `database` 或 `redis` 不可达。
生产环境缺少 `OPENAI_API_KEY` 时，`mem0` 和 `milvus` 必须返回 `not_ready`。

### 迁移时报 `could not open file "base/..."`

这是 PostgreSQL 数据目录缺文件，不是当前迁移 SQL 写错。先看：

```bash
docker logs --tail 120 liaohe-postgresql
```

如果同时看到 checkpoint 失败、数据库 invalid 或 `DROP DATABASE` 也失败，需要整体重建共享 `liaohe-postgresql` 数据目录。旧目录应先改名备份，例如：

```text
/Users/zhangpeng/workspace/postgre.corrupt-backup-20260514-1507
```

重建后再创建 `innies-memory` 数据库并执行：

```bash
.venv/bin/alembic upgrade head
```

### 迁移时报 `password authentication failed`

说明已经连到了 PostgreSQL，但账号或密码不匹配。检查 `.env` 中的 `POSTGRES_PASSWORD` 是否和当前 `liaohe-postgresql` 一致。

### 迁移时报 `Connection refused`

说明 `POSTGRES_HOST / POSTGRES_PORT` 没有可用 PostgreSQL。检查：

```bash
docker ps --format 'table {{.Names}}\t{{.Ports}}\t{{.Status}}'
lsof -iTCP:5432 -sTCP:LISTEN
```

### 端口被占用

将 `--port 8000` 改成未占用端口，或查询占用进程：

```bash
lsof -iTCP:8000 -sTCP:LISTEN
```

### L3 pending 队列持续增长

先观察 `/memory/l3/background-status`。如果 pending 线性增长但没有错误，通常表示外部 LLM、Embedding、Milvus 或本机资源处理速度低于写入速度。当前本地默认 `MEMORY_BACKEND_MAX_CONCURRENT_CALLS=4` 偏保守；是否调高应基于目标主机压测，不应只根据本机硬件做最终判断。

## 9. 提交前检查清单

- 已确认 `.env` 不会进入 Git，也不会进入镜像。
- 已确认本地连接的是 `liaohe-postgresql` 或目标验证环境 PostgreSQL。
- 已完成本次变更对应的质量门禁。
- 已启动 FastAPI 并打开 Swagger UI。
- 已完成相关健康检查，并理解 ready 失败是否由缺少 `OPENAI_API_KEY` 导致。
- 如涉及真实 L3 链路，已按第 7 章完成真实主链路验证并保存关键输出用于 review。
