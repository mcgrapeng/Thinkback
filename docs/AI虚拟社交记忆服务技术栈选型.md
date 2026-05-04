# AI 虚拟社交记忆服务技术栈选型

本文档记录 Thinkback 当前工程采用的技术栈，以及每个组件在三层记忆架构中的边界。

## 1. 选型结论

| 能力 | 当前选型 | 工程边界 |
| --- | --- | --- |
| HTTP 服务 | FastAPI | 承载 `/memory/*`、健康检查和就绪检查。 |
| 业务数据 | PostgreSQL + SQLAlchemy async | 保存 L2 摘要、可靠轮次、L3 业务索引、任务状态和用户角色状态。 |
| 数据迁移 | Alembic | 管理 `tb_` 业务表结构。当前首版迁移为 `20260504_0001_memory_p0_tables.py`。 |
| 长期记忆引擎 | Mem0 | 负责 L3 的抽取、去重、分类、语义检索、显式更新和删除。 |
| 向量数据库 | Qdrant | 作为 Mem0 的 vector store，不由业务服务直接写入 L3 向量。 |
| LLM / Embedding | OpenAI 兼容接口 | 由 Mem0 调用；配置项为 `MEMORY_LLM_*` 和 `MEMORY_EMBEDDING_*`。 |
| 缓存和异步基础设施 | Redis + Celery | Redis 用于运行时基础设施；Celery 已有 worker bootstrap，复杂异步沉淀可继续扩展。 |
| 测试 | pytest + ruff + mypy | 覆盖契约、后端适配、服务工作流、API 和端到端假后端流程。 |

## 2. Mem0 和 Qdrant 的关系

Mem0 内部依赖向量数据库。当前配置在 `src/memory/mem0_client.py` 中把 `vector_store.provider` 设置为 `qdrant`，集合名来自 `MEMORY_QDRANT_COLLECTION`。

业务服务不要直接通过 `src/infra/vectorstore` 写入 L3 记忆。原因很简单：L3 的抽取、去重、更新、删除和语义检索都属于 Mem0 的职责；业务服务绕过 Mem0 直接写 Qdrant，会破坏 Mem0 的一致性和返回语义。

`src/infra/vectorstore/qdrant_client.py` 只用于 Qdrant 就绪检查、运维诊断和未来可能的连接复用，不是 L3 业务写入路径。

## 3. 为什么还需要 PostgreSQL

Mem0 解决的是长期记忆内容和语义检索，不解决产品侧所有治理问题。Thinkback 仍需要 PostgreSQL 保存以下业务事实：

| 表 | 作用 |
| --- | --- |
| `tb_summary_round_journal` | 可靠轮次存储，是 L2/L3 正常沉淀的输入来源。 |
| `tb_current_summary` | 当前 L2 阶段摘要和摘要游标。 |
| `tb_memory` | Mem0 长期记忆的业务索引，记录作用域、来源、状态和治理字段。 |
| `tb_memory_task` | 写入、删除、重建任务状态和错误原因。 |
| `tb_user_character_state` | 用户和角色关系状态。 |

API 默认使用 `SqlAlchemyMemoryRepository`，不再使用内存仓库作为运行时存储。内存仓库只用于单元测试和确定性假后端验证。

当前服务工作流是同步边界，API 通过线程池调用它；SQLAlchemy engine 因此使用 `NullPool`，避免 asyncpg 连接跨事件循环复用。后续如果把仓库和服务整体改成 async，再重新评估连接池配置。

## 4. L3 删除边界

当前工程禁止业务代码直接调用 Mem0 的 `delete_all()` 做范围删除。已验证的本地 Mem0 版本中，`delete_all()` 会触发 vector store `reset()`，对 Qdrant 集合有高危副作用。

Thinkback 的范围删除策略是：

1. 先从业务索引表找到当前 `user_id × character_id` 下的 active L3 backend id。
2. 对每个 backend id 调用 Mem0 `delete()`。
3. 成功后把业务索引标记为 `DELETED` 或 `SUPERSEDED`。

其中 `DELETED` 表示用户显式删除，重建时必须跳过对应来源；`SUPERSEDED` 表示重建替换旧索引，不等同于用户要求遗忘。

## 5. 当前不做的事

首版没有实现强治理阶段的写入栅栏、语义抑制、连续轮次账本、删除墓碑 CAS 和衰减任务。这些能力适合在真实并发、隐私治理和恢复场景明确后再做，不能塞进首版主路径。

当前首版的目标是：可靠输入、范围隔离、任务可追踪、显式删除不直接复活、Mem0 边界清晰。
