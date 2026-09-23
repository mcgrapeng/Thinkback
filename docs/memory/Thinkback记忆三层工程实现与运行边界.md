# Thinkback记忆三层工程实现与运行边界

本文承接 [Thinkback记忆三层架构](Thinkback记忆三层架构.md)，只记录当前工程实现口径、接口边界和排障要点。

## 1. 当前实现总览

Thinkback 记忆服务由 FastAPI、PostgreSQL、Redis、Mem0 Library、Milvus、OpenAI-compatible LLM endpoint 和独立 Embedding endpoint 组成。

```text
client
  │
  ▼
FastAPI /memory/*
  │
  ▼
MemoryService
  ├─ L1: process-local derived cache
  ├─ L2: ins_current_summary
  ├─ journal: ins_summary_round_journal
  ├─ L3 index: ins_memory
  ├─ task state: ins_memory_task
  └─ Mem0 Library -> Milvus + LLM / Embedding endpoints
```

作用域字段含义只以 [Thinkback记忆三层架构](Thinkback记忆三层架构.md) 第 0 节为准。
本文后续出现 `memory_scope_id` 时，只表示当前数据表或流程中的字段名，不重复解释作用域规则。

## 2. 关键模块

| 文件 | 职责 |
| --- | --- |
| `src/thinkback/api/memory.py` | FastAPI 路由，负责 HTTP 错误码和线程池边界 |
| `src/thinkback/memory/schemas.py` | 请求、响应和枚举契约 |
| `src/thinkback/memory/service.py` | 写入、召回、删除、重建和任务查询工作流 |
| `src/thinkback/memory/repositories.py` | 内存仓库和 SQLAlchemy 仓库 |
| `src/thinkback/memory/backends.py` | Fake 后端和 Mem0 Library 后端 |
| `src/thinkback/infra/readiness.py` | 聚合数据库、Redis、Milvus 和 Mem0 Library 就绪检查 |
| `src/thinkback/infra/database/models.py` | 四张 `ins_` 表的 SQLAlchemy model |

`service.py` 不直接操作 SQLAlchemy model；`repositories.py` 不调用 Mem0；`backends.py` 不关心业务表。

## 3. API 契约

### append

```json
{
  "request_id": "req-20260511-append-001",
  "user_id": "user-20260511-001",
  "session_id": "session-20260511-a",
  "round_id": "round-20260511-001",
  "round_index": 1,
  "messages": [
    {
      "message_id": "msg-20260511-u",
      "role": "user",
      "content": "我喜欢喝茶",
      "timestamp": "2026-05-11T10:00:00+00:00"
    },
    {
      "message_id": "msg-20260511-a",
      "role": "assistant",
      "content": "记住了",
      "timestamp": "2026-05-11T10:00:03+00:00"
    }
  ],
  "source_timestamp": "2026-05-11T10:00:03+00:00",
  "metadata": {}
}
```

要求：

- `messages` 必须是完整 `user -> assistant` 两条消息。
- `round_id` 是写入幂等锚点。
- 额外字段会被拒绝。

### recall

```json
{
  "user_id": "user-20260511-001",
  "session_id": "session-20260511-a",
  "query": "用户喜欢喝什么？",
  "intent": "chat",
  "l3_limit": 5,
  "l3_score_threshold": 0.5,
  "token_budget": 1200
}
```

当前 V1 schema 只暴露 `chat / sensitive` 两类 intent。
`chat` 会触发 L3 查询；`sensitive` 采用 fail-closed 策略。

### delete

```json
{
  "request_id": "req-20260511-delete-001",
  "user_id": "user-20260511-001",
  "scope": "memory",
  "operation_id": "op-20260511-delete-001",
  "memory_id": "mem-20260511-001"
}
```

`scope` 支持 `memory / session / all`。
`memory` 必须带 `memory_id` 且不得带 `session_id`；
`session` 必须带 `session_id` 且不得带 `memory_id`；
`all` 只需要 `user_id`，不得带 `memory_id` 或 `session_id`。

### management list/get

```text
GET /memory/items?user_id=user-20260511-001&include_deleted=false
GET /memory/items/memory-20260511-001?user_id=user-20260511-001
```

管理列表和详情只返回可管理的 L3 业务记忆：

- `memory_id` 是业务记忆 ID，可用于 `/memory/update` 和 `/memory/delete`。
- 默认只返回 `ACTIVE` 记忆；`include_deleted=true` 可返回已删除或已取代的业务记忆。
- 系统墓碑不返回。
- 响应不暴露 `memory_scope_id`、backend memory id 或 source refs。

### management update

```json
{
  "request_id": "req-20260511-update-001",
  "user_id": "user-20260511-001",
  "operation_id": "op-20260511-update-001",
  "memory_id": "memory-20260511-001",
  "content": "User has a cat named 麻薯",
  "memory_type": "profile"
}
```

`content` 必须非空；额外字段会被拒绝。
编辑只处理 `ACTIVE` 业务记忆，找不到时返回 404。
编辑任务可通过 `/memory/tasks/{task_id}` 查询，任务 ID 为 `memory-update:{operation_id}`。

### rebuild

V1 不通过 HTTP 暴露重建端点。`/memory/rebuild` 在 OpenAPI schema 中**不存在**；调用方无法直接触发重建。

`MemoryService.rebuild()` 业务方法作为内部能力保留，仅供运维脚本、真实质量回归脚本和后续版本以 Python 级调用使用。
内部调用契约（不是 HTTP 契约）：

```json
{
  "request_id": "req-20260511-rebuild-001",
  "user_id": "user-20260511-001",
  "operation_id": "op-20260511-rebuild-001",
  "session_id": "session-20260511-a",
  "history_version": null,
  "rebuild_l2": true,
  "rebuild_l3": true
}
```

未传 `session_id` 时按用户下已接纳会话重建 L2，并按用户长期作用域处理 L3。
重建语义和实现细节见第 7 节，HTTP 端点下线取舍见 [Thinkback记忆三层架构](Thinkback记忆三层架构.md) 第 5 节。

## 4. 写入实现

`MemoryService.append()` 的顺序：

1. 计算 `session_scope_id = session_id`、`l3_scope_id = thinkback`。
2. 检查 `round_id` 是否已存在；同一轮次重复提交返回 `already_done`。
3. 保存 `ins_memory_task`，任务 ID 为 `memory-extract:{round_id}`。
4. 保存可靠轮次到 `ins_summary_round_journal`。
5. 命中高风险内容时跳过 L2/L3，并把相关轮次标记为删除来源。
6. 同步模式先把轮次标为 `pending_append`，等 Mem0 写入成功后再激活该轮次。
7. 异步模式立即激活可靠轮次，并刷新 L1、L2 和P0 槽位索引。
8. 按 `MEMORY_L3_WRITE_MODE` 同步写入 Mem0，或把 L3 抽取提交到后台队列。
9. 同步模式在 Mem0 写入完成后刷新 L1、L2、P0 槽位索引、任务状态和脱敏结果。
10. 异步模式先返回 `DEFERRED` 事件，后台完成后再把 `memory-extract:{round_id}` 更新为完成或失败。

当前异步 L3 写入使用 API 进程内线程池，不依赖 Celery worker 消费。队列满时返回明确背压错误。
同步模式下，Mem0 写入失败的轮次保持 `pending_append`，不会进入 L1/L2、P0 槽位补偿、召回或重建输入；调用方复用原 `round_id` 重试，成功后才激活。

## 5. 召回实现

`MemoryService.recall()` 的顺序：

1. 读取当前会话 L1。
2. 读取当前会话 L2；`dirty` 摘要跳过，`stale` 摘要带降级原因返回。
3. 根据 intent 判断是否必须查 L3。
4. 读取当前用户 `thinkback` 长期作用域下的 active 业务索引。
5. 对已知 P0 槽位先做业务索引补偿，降低模型检索波动。
6. 必要时调用 Mem0 search。
7. 丢弃没有 active 业务索引对应的 backend memory。
8. 合并 L1/L2/L3，去重并按 token 预算裁剪。

L3 返回项统一带 `metadata.memory_as_data=true`，提醒下游把记忆当数据而不是指令。
后端返回的内部 metadata 不原样透传；召回响应只保留允许暴露的类型标记，避免泄漏 `source_refs`、内部作用域或原始来源文本。

V1 阶段 L2 摘要由 `repositories.summarize_rounds` 生成，实质为"最近 10 轮 user 消息用 '；' 拼接"，未调用 LLM。
完整 L2 语义压缩（业界主流 LLM 摘要管线）列入架构 §0.2 V2 范围。详细取舍见 [Thinkback记忆三层架构](Thinkback记忆三层架构.md) §2 L2 段。

## 6. 删除实现

删除任务 ID 为 `memory-delete:{operation_id}`。
只有同一 `operation_id`、同一用户和同一删除范围参数完全一致时，重复完成的删除请求才返回 `already_done`。
如果同一删除任务仍在运行，重复请求返回 `running` 和同一 `task_id`，不得再次提交 backend 删除。
同一 `operation_id` 被不同用户、不同 scope、不同 memory 或不同 session 复用时必须报冲突。

| 范围 | 行为 |
| --- | --- |
| `memory` | 校验 memory 属于当前用户长期作用域；删除 backend；标记 `DELETED`；相关 L2 标脏 |
| `session` | 对命中该会话来源的 L3 移除 source ref，并留下已删除 source-ref tombstone；无剩余来源时删除 backend 并标记 `DELETED`；清理该会话 L1 和轮次来源 |
| `all` | 删除当前用户长期作用域下所有 active L3；写入用户级全量删除墓碑；清理该用户所有已知会话 L1/L2 来源 |

范围删除只基于 `ins_memory` 的 active 索引列出候选 backend id，不调用 Mem0 原生 `delete_all()`。

### 6.1 管理查询与编辑实现

管理查询读取 `ins_memory` 业务索引，并投影为安全响应字段：
`memory_id / content / status / source_type / data_classification / memory_type / backend_categories`。
响应不返回内部 `memory_scope_id`、backend memory id、source refs 或原始 source text。

编辑任务 ID 为 `memory-update:{operation_id}`。
只有同一 `operation_id`、同一用户、同一 `memory_id`、同一新正文指纹和同一记忆分类完全一致，且目标记忆仍是该次编辑结果时，重复完成的编辑请求才返回 `already_done`。
如果目标记忆已被后续编辑或删除，旧 `operation_id` 重试返回冲突，避免把后续正文误报为旧操作结果。
同一 `operation_id` 被不同编辑范围复用时必须报冲突。

`MemoryService.update_memory()` 的顺序：

1. 校验目标 memory 属于当前用户长期作用域且状态为 `ACTIVE`。
2. 校验新正文不命中 restricted/unsafe 规则。
3. 对本地 P0 索引校验新正文仍属于同一 P0 冲突槽位；否则拒绝，避免编辑后只能在管理列表中看到却无法召回。
4. 对 Mem0 管理的 backend id 调用 `backend.update()`；本地 P0 索引不调用 backend。
   如果后续本地索引写入失败，会 best-effort 把 backend 恢复为旧正文。
5. 更新 `ins_memory.memory_text`、`source_type=manual_fix`、`memory_type` 和管理 metadata。
6. 对同槽位冲突 active 记忆先标记 `SUPERSEDED`，再 best-effort 清理旧 backend memory；清理失败不阻断新事实在业务索引中生效，召回仍以 active 业务索引为准。
7. 清空相关 source refs 涉及的 L1，并将对应 L2 标为 `dirty`。
8. 保存任务为 `completed`，任务结果只记录 `memory_id`、正文指纹、memory_type 和摘要状态，不保存正文。

V1.1 的管理编辑不提供 CAS、版本号、人工审核流、批量合并、强审计或治理台。

## 7. 重建实现

重建任务 ID 为 `memory-rebuild:{operation_id}`。
只有同一 `operation_id`、同一用户、同一 session、同一 `history_version` 和同一重建开关完全一致时，重复完成的重建请求才返回 `already_done`。
如果同一重建任务仍在运行，重复请求返回 `running` 和同一 `task_id`，不得再次执行重建编排。
同一 `operation_id` 被不同重建范围复用时必须报冲突。

`MemoryService.rebuild()` 的顺序：

1. 可选检查 `history_version`。
2. 读取外部历史源；无历史源时读取服务已接纳的可靠轮次。
   用户级外部历史源重建使用内部 `__all_sessions__` 作用域枚举该用户历史轮次，再按轮次实际作用域重算 L2。字段说明见 [Thinkback记忆三层架构](Thinkback记忆三层架构.md) 第 0 节。
3. 对 L2：按会话摘要作用域重算，跳过已删除来源和用户级全量删除墓碑覆盖的旧轮次。
4. 对 L3：找出完全来自已删除来源的旧索引，删除 backend 并标记 `SUPERSEDED`。
5. 对未覆盖、未被全量删除墓碑覆盖的有效轮次重新写入 L3。
6. 保存任务结果并清理缓存。

异步模式下，第 5 步会提交一个或多个 `memory-extract:{round_id}` 子任务。
此时 `memory-rebuild:{operation_id}` 表示重建编排已完成；任务结果中的 `l3_replay_status=deferred` 和 `l3_extract_task_ids` 才是判断 L3 回放是否仍在后台执行的依据。

重建不使用 L1 作为可信来源。
即使外部历史源仍返回已删除会话的轮次，已删除 source-ref tombstone 也会让 L2/L3 重建跳过该来源。
如果用户执行过 `scope=all` 删除，用户级全量删除墓碑会让外部历史源中删除时间之前的轮次继续被跳过；删除之后的新轮次仍可在后续重建中沉淀。

## 8. 数据表

下表只列字段名；作用域字段统一按 [Thinkback记忆三层架构](Thinkback记忆三层架构.md) 第 0 节。

| 表 | 关键字段 | 说明 |
| --- | --- | --- |
| `ins_summary_round_journal` | `user_id / memory_scope_id / session_id / round_id / messages / round_state` | 可靠轮次账本 |
| `ins_current_summary` | `user_id / memory_scope_id / summary_text / summary_state` | 当前 L2 摘要 |
| `ins_memory` | `backend_memory_id / user_id / memory_scope_id / source_refs / memory_status / metadata` | L3 业务索引 |
| `ins_memory_task` | `task_id / request_id / op_type / scope / status / last_error / result` | 写入、编辑、删除、重建任务状态 |

## 9. 运行时边界

- API 路由是 async，服务和 Mem0 adapter 是同步边界，因此通过显式读/写 `ThreadPoolExecutor` 和 bounded semaphore 执行。
- `SqlAlchemyMemoryRepository` 在固定后台 event loop 上运行 async SQLAlchemy 调用，engine 使用默认连接池，避免每次仓库调用新建 event loop 和 asyncpg 连接。
- 生产路径默认使用 `SqlAlchemyMemoryRepository + Mem0LibraryMemoryBackend`。
- `InMemoryMemoryRepository` 和 fake backend 只用于测试。
- 业务读写和语义检索不得绕过 Mem0。
- `MEMORY_BACKEND_MAX_CONCURRENT_CALLS` 是 Mem0 Library 后端调用并发上限，默认 `4`，用于限制 LLM、Embedding 和 Milvus 压力。
- readiness 允许做诊断性依赖探测：Milvus 通过 SDK `list_collections()` 做只读连通性检查，Mem0 Library 只做配置和适配器构造检查，不在探针中执行 add/delete 写入。

## 10. 可观测性和排障

必须关注：

- `/health/ready` 的数据库、Redis、Milvus、Mem0 Library 配置状态；Mem0 Library 检查不执行 add/delete 写入探针。
- `/memory/l3/background-status` 的 L3 后台队列容量。
- `ins_memory_task.status` 和 `last_error`。
- 真实验证脚本或发布归档中的 `request_metrics`、瞬时失败和非瞬时失败。
- 质量回归输出中的召回、污染、删除残留、重建复活和跨用户泄漏指标。

Prometheus label 不应直接使用 `user_id / session_id / round_id / memory_id / task_id / request_id` 这类高基数字段。
需要按这些字段排障时走日志、任务查询或审计记录。

### 10.1 请求追踪

HTTP 入口接受 `X-Trace-Id` 和 `X-Request-Id`，优先回收入站 trace 作为当前请求链路的标识。
如果请求头都没有提供，服务会生成新的 trace id，并回写到响应头 `X-Trace-Id`。

日志格式会显式输出 `trace_id`，便于在日志平台中串联 API 请求、后台线程池写入和依赖探测日志。
`trace_id` 默认值为 `-`，表示当前日志记录不在任何具体请求链路内。

`copy_context()` 负责把当前请求上下文带入线程池执行的同步工作。
这保证 `/memory/*` 写入、后台 L3 提交和相关回调继续携带同一个 `trace_id`，方便定位跨线程问题。

## 11. 当前不做

当前 P0 不实现：

- Mem0 外的独立向量检索路径。
- 强一致写入栅栏。
- 删除墓碑 CAS。
- 语义抑制。
- 衰减分片任务。
- 人工治理台。

这些能力应在真实风险出现后按阶段加入，不能反向阻塞当前主链路。
