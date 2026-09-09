# Innies记忆服务数据库迁移指南

本文档说明 innies-memory 记忆服务的数据库表、迁移命令和本地排障方式。

## 1. 当前迁移

当前迁移文件：

```text
alembic/versions/20260504_0001_memory_p0_tables.py
alembic/versions/20260512_0002_unique_active_backend_memory.py
```

`20260504_0001` 创建四张业务表：

| 表名 | 必需性 | 说明 |
| --- | --- | --- |
| `ins_summary_round_journal` | 首版必需 | 可靠轮次存储，写入以完整 `user -> assistant` 轮次进入，`round_id` 全局唯一 |
| `ins_current_summary` | 首版必需 | L2 当前阶段摘要，包含摘要游标和 dirty/stale/active 状态 |
| `ins_memory` | 首版必需 | L3 业务索引，关联 Mem0 backend memory id、来源、作用域和状态 |
| `ins_memory_task` | 首版必需 | 写入、删除、重建任务状态、重试次数和错误信息 |

`20260512_0002` 先把历史重复 active `backend_memory_id` 标为 `SUPERSEDED`，
再为 `ins_memory(user_id, memory_scope_id, backend_memory_id)` 创建 active-only 唯一索引。
字段含义见 [Innies记忆三层架构](Innies记忆三层架构.md) 第 0 节，本段只记录该索引的迁移影响。
这个约束用于防止多 API 进程或多 Pod 并发写入同一 Mem0 backend id 时产生重复 active 业务索引。

## 2. 迁移前检查

确认 `.env` 或运行环境里数据库变量正确：

```bash
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_USER=postgres
POSTGRES_PASSWORD=<local-postgres-password>
POSTGRES_DATABASE=innies-memory
```

本地约定优先使用共享 `liaohe-postgresql`，不要通过本仓库的 `docker compose up -d postgres` 自建一套新的本地 PostgreSQL。
如果要重建数据库，请参考 [Innies记忆服务开发手册](Innies记忆服务开发手册.md) 第 3 节的共享 PostgreSQL 口径。
compose 自带 PostgreSQL 仅用于隔离验证或 CI 类临时环境；使用它时必须显式确认 `.env` 指向 compose 暴露端口，而不是共享 `liaohe-postgresql`。

## 3. 执行迁移

启动依赖请按开发手册中的共享 PostgreSQL / Redis 口径准备。

执行迁移：

```bash
.venv/bin/alembic upgrade head
```

或使用 Makefile：

```bash
make db-upgrade
```

查看当前版本：

```bash
.venv/bin/alembic current -v
```

生成静态 SQL：

```bash
.venv/bin/alembic upgrade head --sql
```

注：静态 SQL 生成只验证迁移脚本可编译，不代表当前数据库已经能连上。
真实连库状态应以 `.venv/bin/alembic current -v` 的即时输出为准。

常见错误入口详见 [Innies记忆服务开发手册](Innies记忆服务开发手册.md) 第 8 章。

## 4. 表结构要点

### ins_summary_round_journal

保存可靠轮次，不是 L1 缓存。关键字段：

| 字段 | 用途 |
| --- | --- |
| `round_id` | 写入幂等锚点，建唯一索引 |
| `user_id` / `memory_scope_id` / `session_id` | 用户、作用域字段和会话来源；字段含义见架构第 0 节，本表不重复定义 |
| `round_fingerprint` | 检测同一 `round_id` 下内容或作用域冲突 |
| `messages` | 规范化后的轮次消息 |
| `round_state` | 当前使用 `pending_append`、`active` 或 `deleted_tombstone`；只有 `active` 进入摘要、召回和重建输入 |

### ins_current_summary

保存 L2 阶段摘要。
`active` 正常参与召回，`stale` 可带降级标记参与召回，`dirty` 不得进入 prompt。

### ins_memory

保存 L3 业务索引，不保存向量。
字段含义见 [Innies记忆三层架构](Innies记忆三层架构.md) 第 0 节，本节只记录迁移影响。
关键状态：

| 状态 | 含义 |
| --- | --- |
| `ACTIVE` | 当前可召回 |
| `DELETED` | 用户显式删除，重建必须跳过来源；`scope=all` 也会用该状态保存用户级全量删除墓碑 |
| `SUPERSEDED` | 重建替换产生的旧索引，不代表用户删除 |
| `SUPPRESSED` | 预留给强治理阶段的语义抑制 |

数据库通过 `uq_ins_memory_active_backend_scope` 保证同一 `user_id + memory_scope_id + backend_memory_id` 只能有一条 `ACTIVE` 记录。
迁移会在创建约束前把历史重复 active 记录标为 `SUPERSEDED`，避免升级时因脏数据失败。

### ins_memory_task

所有写入、删除、重建都登记任务。外部后端失败时，任务写为 `failed` 并记录 `last_error`。

## 5. 回滚

首版回滚会删除四张业务表：

```bash
.venv/bin/alembic downgrade -1
```

生产环境回滚前必须备份数据库，并确认 Mem0 中的长期记忆是否需要同步清理。
Alembic 回滚只处理 PostgreSQL 业务表，不会自动删除 Mem0 服务里的长期记忆。

Milvus 不属于 Alembic 管理范围。
Mem0 使用独立 Milvus 承载 L3 向量数据；innies-memory 数据库迁移只管理 `ins_` 业务表和索引，不创建、不删除 Milvus collection。
需要清理长期记忆时，应先通过 innies-memory 的业务索引和 Mem0 delete/update 能力处理，再按运维流程评估 Milvus 数据。
