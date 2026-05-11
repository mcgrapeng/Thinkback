# AI 虚拟社交记忆服务数据库迁移指南

本文档说明 Thinkback 记忆服务的数据库表、迁移命令和本地排障方式。

## 1. 当前迁移

当前首版迁移文件：

```text
alembic/versions/20260504_0001_memory_p0_tables.py
```

它创建五张业务表：

| 表名 | 必需性 | 说明 |
| --- | --- | --- |
| `tb_summary_round_journal` | 首版必需 | 可靠轮次存储。写入以完整 `user -> assistant` 轮次进入，`round_id` 全局唯一。 |
| `tb_current_summary` | 首版必需 | L2 当前阶段摘要，包含摘要游标和 dirty/stale/active 状态。 |
| `tb_memory` | 首版必需 | L3 业务索引，关联 Mem0 backend memory id、来源、作用域和状态。 |
| `tb_memory_task` | 首版必需 | 写入、删除、重建任务状态、重试次数和错误信息。 |
| `tb_user_character_state` | 首版预留 | 用户和角色关系状态，后续关系连续性能力使用。 |

## 2. 迁移前检查

确认 `.env` 或运行环境里数据库变量正确。下面只展示字段形状；如果使用仓库的 Docker Compose 本地 PostgreSQL，默认密码是 `postgres`，如果使用你自己已有的数据库，真实密码按你的 `.env` 为准：

| 变量 | 中文说明 |
| --- | --- |
| `POSTGRES_HOST` | PostgreSQL 主机名；控制台本地调试通常是 `localhost`，K8s 中通常是 Service DNS。 |
| `POSTGRES_PORT` | PostgreSQL 端口；本地默认 `5432`。 |
| `POSTGRES_USER` | PostgreSQL 用户名。 |
| `POSTGRES_PASSWORD` | PostgreSQL 密码；生产必须从 Secret 注入。 |
| `POSTGRES_DATABASE` | PostgreSQL 数据库名；本工程统一使用 `liaoriver_memory`。 |
| `PYTHONPATH` | 本地执行 Alembic 时的 Python 导入路径，设置为 `src`。 |

```bash
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_USER=postgres
POSTGRES_PASSWORD=postgres
POSTGRES_DATABASE=liaoriver_memory
```

本地如果已有其他 PostgreSQL 占用 `5432`，`docker compose up -d postgres` 会失败，或者应用会连到错误实例。处理方式二选一：

1. 停掉本机占用 `5432` 的旧服务。
2. 修改 `docker-compose.yml` 的宿主机端口和 `.env` 的 `POSTGRES_PORT`，保持两边一致。

这里的 `.env` 端口是宿主机或控制台命令视角。Compose 的 `app` 容器访问 `postgres` 服务时使用容器内固定端口 `5432`，由 `docker-compose.yml` 显式覆盖，避免把宿主机调试端口带进容器。

## 3. 执行迁移

启动依赖：

```bash
docker compose up -d postgres redis
```

执行迁移：

```bash
PYTHONPATH=src .venv/bin/alembic upgrade head
```

或使用 Makefile：

```bash
make db-upgrade
```

查看当前版本：

```bash
PYTHONPATH=src .venv/bin/alembic current -v
```

生成静态 SQL：

```bash
PYTHONPATH=src .venv/bin/alembic upgrade head --sql
```

注：静态 SQL 生成只验证迁移脚本可编译，不代表当前数据库已经能连上。
真实连库状态应以 `PYTHONPATH=src .venv/bin/alembic current -v` 的即时输出为准。

常见错误可以按下面区分：

| 错误 | 直白解释 | 处理方式 |
| --- | --- | --- |
| `password authentication failed for user "postgres"` | 已经连到 PostgreSQL，但用户名或密码不匹配。 | 修正 `.env` 里的账号密码，或调整本地数据库密码。 |
| `Connect call failed` / `Connection refused` | 指定的 `POSTGRES_HOST / POSTGRES_PORT` 没有可用实例。 | 检查 `docker compose up -d postgres` 是否成功，以及 `.env` 端口是否和 `docker-compose.yml` 一致。 |

## 4. 表结构要点

### tb_summary_round_journal

保存可靠轮次，不是 L1 缓存。关键字段：

| 字段 | 用途 |
| --- | --- |
| `round_id` | 写入幂等锚点，建唯一索引。 |
| `user_id` / `character_id` / `session_id` | 作用域和会话来源。 |
| `round_fingerprint` | 检测同一 `round_id` 下内容或作用域冲突。 |
| `messages` | 规范化后的轮次消息。 |
| `round_state` | 当前工程使用 `active` 或 `deleted_tombstone`；`deleted_tombstone` 在首版用于删除、修复或硬跳过后的来源排除，不进入摘要和重建输入。 |

### tb_current_summary

保存 L2 阶段摘要。`summary_state=dirty` 时不得进入 prompt；`active` 才可参与召回。

### tb_memory

保存 L3 业务索引，不保存向量。关键状态：

| 状态 | 含义 |
| --- | --- |
| `ACTIVE` | 当前可召回。 |
| `DELETED` | 用户显式删除，重建必须跳过来源。 |
| `SUPERSEDED` | 重建替换产生的旧索引，不代表用户删除。 |
| `SUPPRESSED` | 预留给强治理阶段的语义抑制。 |

### tb_memory_task

所有写入、删除、重建都登记任务。外部后端失败时，任务写为 `failed` 并记录 `last_error`。

## 5. 回滚

首版回滚会删除五张业务表：

```bash
PYTHONPATH=src .venv/bin/alembic downgrade -1
```

生产环境回滚前必须备份数据库，并确认 Mem0 中的长期记忆是否需要同步清理。Alembic 回滚只处理 PostgreSQL 业务表，不会自动删除 Mem0 服务里的长期记忆。

Qdrant 不属于 Alembic 管理范围。Mem0 使用独立 Qdrant 承载 L3 向量数据；Thinkback 数据库迁移只管理 `tb_` 业务表和索引，不创建、不删除 Qdrant collection。需要清理长期记忆时，应先通过 Thinkback 的业务索引和 Mem0 delete/update 能力处理，再按运维流程评估 Qdrant 数据。
