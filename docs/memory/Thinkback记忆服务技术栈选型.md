# Thinkback记忆服务技术栈选型

本文档记录 thinkback 当前工程采用的技术栈，以及每个组件在三层记忆架构中的边界。

## 1. 选型结论

| 能力 | 当前选型 |
| --- | --- |
| HTTP 服务 | FastAPI |
| 业务数据 | PostgreSQL + SQLAlchemy async |
| 数据迁移 | Alembic |
| 长期记忆引擎 | Mem0 Library |
| 向量数据库 | 独立 Milvus 服务 |
| LLM / Embedding | OpenAI-compatible endpoint |
| 缓存和异步基础设施 | Redis + Celery |
| 测试 | pytest + ruff + mypy |

工程边界：

- FastAPI 承载 `/memory/*`、健康检查和就绪检查。
- PostgreSQL 保存 L2 摘要、可靠轮次、L3 业务索引和任务状态。
- Alembic 管理 `ins_` 业务表结构。
  首版基线迁移为 `20260504_0001_memory_p0_tables.py`；
  当前 head 还包含 `20260512_0002_unique_active_backend_memory.py`，
  用于补充 active backend memory id 唯一保护。
- Mem0 Library 以 Python Library 方式嵌入 thinkback，
  负责 L3 的抽取、去重、分类、语义检索、显式更新和删除。
- Milvus 是独立向量数据库。
  Mem0 Library 通过 thinkback 配置的 Milvus endpoint 写入和检索 L3 向量。
  readiness 对 Milvus 做只读连通性探测，对 Mem0 Library 只做配置和适配器构造检查。
  本仓库默认 `MILVUS_DATABASE=default`、`MEMORY_MILVUS_COLLECTION=thinkback`；本地或生产如使用其他库名或 collection，必须由运行配置显式覆盖。
- LLM 和 Embedding 是两组独立 endpoint。
  LLM 使用 `OPENAI_API_KEY + MEMORY_LLM_BASE_URL`。
  Embedding 使用独立 base URL 和可选 key。
- Redis 提供 Celery broker/result backend。
  Celery worker 目前只承载诊断任务和后续异步任务扩展入口。
- pytest、ruff 和 mypy 覆盖契约、后端适配、服务工作流、API 和端到端假后端流程。

## 2. Mem0 和向量数据库的关系

Mem0 内部依赖向量数据库。当前工程采用 Mem0 Library，不再部署独立的 Mem0 REST Server。
生产部署模型按 K8s 运行 thinkback，并连接外部 Milvus、外部 LLM endpoint 和外部 Embedding endpoint。
Mem0 Library 运行在 thinkback 进程内，通过 `MILVUS_URL / MILVUS_DATABASE / MILVUS_USER / MILVUS_PASSWORD` 写入和检索 L3 向量。

业务读写和语义检索不得绕过 Mem0。
L3 的抽取、去重、更新、删除和语义检索都属于 Mem0 的职责。
业务服务绕过 Mem0 写向量库，会破坏 Mem0 的一致性和返回语义。
readiness 允许使用 Milvus SDK 做只读连通性探测，也允许构造 Mem0 Library 适配器验证配置完整性。
readiness 不能执行 add/delete 写入探针，也不能直接执行业务 L3 语义检索。

thinkback 当前只保留 Mem0 Library 业务适配器。核心配置项是：

- `OPENAI_API_KEY`
- `MEMORY_LLM_BASE_URL`
- `MEMORY_EMBEDDING_BASE_URL`
- 可选 `MEMORY_EMBEDDING_API_KEY`
- `MILVUS_URL / MILVUS_DATABASE / MILVUS_USER / MILVUS_PASSWORD`
- `MEMORY_MILVUS_COLLECTION`
- `MEMORY_LLM_MODEL`
- `MEMORY_EMBEDDING_MODEL`
- `MEM0_HISTORY_DB_PATH`

工程不再需要 `MEM0_API_URL / MEM0_API_KEY / MEM0_HTTP_TIMEOUT_SECONDS`。

当前 `default-embedding` endpoint 不接受 OpenAI embeddings 请求里的 `dimensions` 参数。
因此 thinkback 在 `src/thinkback/memory/embeddings.py` 提供 `OpenAICompatibleEmbeddingNoDimensions`。
构建 Mem0 Library client 前，服务会把 Mem0 的 `openai` embedder provider 映射到该实现。
这不是绕过 Mem0 写向量库；它只是让 Mem0 的 embedding 调用兼容当前 endpoint，L3 写入和检索仍由 Mem0 Library 驱动。

当前 `mem0ai` Library 的 Milvus provider 使用 `pymilvus` 连接。
生产环境如果启用 Milvus 鉴权，应显式配置 `MILVUS_USER` 和 `MILVUS_PASSWORD`。
本地无鉴权可只配置 `MILVUS_URL=http://localhost:19530`。

## 3. 为什么还需要 PostgreSQL

Mem0 解决的是长期记忆内容和语义检索，不解决产品侧所有治理问题。thinkback 仍需要 PostgreSQL 保存以下业务事实：

| 表 | 作用 |
| --- | --- |
| `ins_summary_round_journal` | 可靠轮次存储，是 L2/L3 正常沉淀的输入来源 |
| `ins_current_summary` | 当前 L2 阶段摘要和摘要游标 |
| `ins_memory` | Mem0 长期记忆的业务索引，记录作用域、来源、状态和治理字段 |
| `ins_memory_task` | 写入、删除、重建任务状态和错误原因 |

API 默认使用 `SqlAlchemyMemoryRepository`，不再使用内存仓库作为运行时存储。内存仓库只用于单元测试和确定性假后端验证。

当前服务工作流是同步边界，API 通过线程池调用它。
`SqlAlchemyMemoryRepository` 在固定后台 event loop 上运行 async SQLAlchemy 调用，engine 使用默认连接池，避免每次调用新建 event loop 和 asyncpg 连接。
后续如果把仓库和服务整体改成 async，可以去掉这层同步适配。

Mem0 Library 后端调用并发上限由 `MEMORY_BACKEND_MAX_CONCURRENT_CALLS` 控制，当前默认 `4`。
这个闸门限制单进程同时进入 LLM、Embedding 和 Milvus 的 Mem0 调用数，不等同于 L3 后台队列容量。
Mem0 telemetry 在 thinkback 适配层保持关闭，因为已知 `mem0.memory.telemetry` 会为 Posthog client 创建后台线程，不能放在主链路热路径里。运行时由 `disable_mem0_telemetry()` 在创建 Mem0 Library 后端前处理，这里只保留选型原因。

## 4. Redis、Celery 和 L3 后台写入边界

Redis 和 Celery 是当前工程保留的异步基础设施。
Celery worker 已有 bootstrap，并注册 `memory.diagnostics.ping` 诊断任务，方便部署侧验证 worker、broker 和 result backend。

当前 L3 `MEMORY_L3_WRITE_MODE=async` 不依赖 Celery worker 消费。
append 请求由 API 进程同步完成可靠轮次存储、L1/L2 更新和 P0 槽位索引；
Mem0 L3 抽取在 API 进程内线程池中执行。
对应队列状态通过 `/memory/l3/background-status` 暴露。

因此生产部署 Celery worker 的主要价值是保留 worker Deployment 形态。
Celery worker 目前只承载诊断任务和后续异步任务扩展入口。
不要把 Celery worker 状态误解为当前 L3 后台抽取是否在执行；
判断 L3 后台抽取应看 API 日志、`ins_memory_task` 和 `/memory/l3/background-status`。

## 5. L3 删除边界

当前工程禁止业务代码直接调用 Mem0 的 `delete_all()` 做范围删除。
已验证的本地 Mem0 版本中，该接口会触发 vector store `reset()`，对 Milvus 集合有高危副作用。

thinkback 的范围删除策略是：

1. 先从业务索引表找到当前 `user_id + long_term_scope_id=thinkback` 下的 active L3 backend id。
2. 对每个 backend id 调用 Mem0 `delete()`。
3. 成功后把业务索引标记为 `DELETED` 或 `SUPERSEDED`。

其中 `DELETED` 表示用户显式删除，重建时必须跳过对应来源；`SUPERSEDED` 表示重建替换旧索引，不等同于用户要求遗忘。

## 6. 当前不做的事

首版没有实现强治理阶段的写入栅栏、语义抑制、连续性强校验、删除墓碑 CAS 和衰减任务。
这些能力适合在真实并发、隐私治理和恢复场景明确后再做，不能塞进首版主路径。

当前首版的目标是：可靠输入、范围隔离、任务可追踪、显式删除不直接复活、Mem0 边界清晰。
