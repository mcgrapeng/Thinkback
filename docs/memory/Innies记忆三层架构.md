# Innies记忆三层架构

| 元信息项 | 当前值 |
| --- | --- |
| 文档责任人 | 张朋 |
| 适用系统 | `innies-memory` |
| 文档类型 | 记忆架构说明 |
| 维护范围 | 设计目标、三层分工、记忆作用域、公开 API 口径 |

本文说明 Innies 记忆服务的当前架构口径。
服务面向 Innies 的会话型记忆能力，公开 API 以 `user_id` 和 `session_id` 为核心。

## 0. 术语口径

本文中的“首版”均指当前 P0 主链路版本：公开 API、可靠轮次、L1/L2、L3 业务索引、Mem0 Library 写入召回、删除、最小管理、内部重建和基础运维交付。
V1 是写入、召回、删除闭环基线；V1.1 是在不引入治理台、CAS 或强审计的前提下补齐长期记忆的最小管理能力。

首版**已实现**以下治理能力的简化版本（不承诺强一致 / CAS / 审计语义）：

- 删除墓碑（标记位，不做 CAS、不承诺强一致回放隔离）
- 业务索引状态机（`ACTIVE / DELETED / SUPERSEDED / SUPPRESSED`，其中 `SUPPRESSED` 当前未启用）
- 最小长期记忆管理面（列出、查询、编辑、删除；不含人工审核流或批量治理台）

首版**不实现**以下能力（明确属于未来版本，不在当前主链路中）：

- 写入栅栏（write fence）
- 强一致删除墓碑（带 CAS、版本号或写入冲突检测）
- 人工治理台
- 衰减分片任务
- 线上长期 SLO 承诺

| 术语 | 口径 |
| --- | --- |
| 长期记忆能力 | L3 面向用户长期事实、偏好和事件的跨会话记忆能力 |
| 长期原子记忆 | L3 中可独立召回、纠错和删除的小粒度事实、偏好或事件 |
| L3 业务索引 | PostgreSQL `ins_memory` 中记录 backend memory id、来源、状态和治理字段的业务索引，不是向量库 |
| `memory_scope_id` | 内部兼容字段，**当前同名双义**：`ins_summary_round_journal` / `ins_current_summary` 中等于 `session_id`；`ins_memory` 中等于固定常量 `innies`。属于历史遗留，未来版本计划统一为单义 |
| P0 槽位 / 冲突槽位 | 同一概念的不同表述。指 V1 阶段为降低召回波动而做的关键事实补偿索引；具体词表见 `service.py::_query_conflict_slot`，覆盖通用对话场景的高频事实槽位（昵称、宠物名、地点、生日、工作状态、沟通偏好、饮食偏好等） |

公开 API 使用 `user_id` 和 `session_id`，不暴露 `memory_scope_id`。

## 0.1 服务定位与认证边界

innies-memory 是 Innies 域内的内部记忆服务，部署在 VPC 或可信内网内，不对公网暴露入口。

| 边界 | 约束 |
| --- | --- |
| 调用方身份 | innies-memory 进程内不做调用方鉴权，不校验 `X-Authenticated-User-Id`，不维护 API key 白名单 |
| 调用方约束 | 请求体里的 `user_id` 由上游服务保证可信；innies-memory 按该值做记忆隔离，不再二次比对 |
| 上游接入 | 公网入口、跨业务域鉴权、流量准入由网关、mTLS、服务身份或内网 ACL 在 innies-memory 上游解决，不下沉到本服务 |
| 模型与基础设施凭据 | `OPENAI_API_KEY`、`MEMORY_EMBEDDING_API_KEY`、`MILVUS_USER`、`MILVUS_PASSWORD`、`POSTGRES_PASSWORD`、`REDIS_PASSWORD` 必须在配置中显式注入；本服务调用外部模型、向量库和数据库强制依赖这些凭据 |
| 配置注入方式 | 本地用 `.env`，生产用 Kubernetes ConfigMap + Secret；任何凭据不得进入镜像或仓库 |

凡是涉及"对调用方做认证"的功能（API key 入口、用户 ID header 校验、生产环境强制鉴权检查）不在 innies-memory 范围内。
凡是涉及"访问外部模型、向量库、数据库"的凭据，本服务必须显式持有，不能依赖运行环境默认凭据或匿名访问。
这条边界是稳定约束：未来如需对外暴露 innies-memory，应通过新增网关层实现，不能在本服务内重新引入入口鉴权。

## 0.2 版本路线（业务侧视角）

按"每版业务闭环 + 不过度设计 + 符合业界主流"原则推进。

| 版本 | 业务范围 | 对外端点 | 闭环验证口径 |
| --- | --- | --- | --- |
| **V1（基线）** | 写入轮次 + L1/L2 + L3（mem0）+ 单条/会话/全部删除 | `POST /memory/{append,recall,delete}`、`GET /memory/tasks/{id}`、`GET /memory/l3/background-status` | 写 → 读 → 删 → 再读已无；跨会话 L3 可召回；`/health/ready` 仅在 `development` 且未配 `OPENAI_API_KEY` 时自动跳过 L3 依赖，生产缺失必须 not_ready |
| **V1.1（当前增补）** | 长期记忆最小管理：列出、查询、编辑；删除继续复用 V1 delete | `GET /memory/items`、`GET /memory/items/{memory_id}`、`POST /memory/update`，并保留 V1 端点 | 列表 → 编辑 → 召回为新值；召回返回业务 `memory_id` 可直接用于编辑或删除；管理响应不暴露 `memory_scope_id`、backend id 或 source refs |
| **V2** | service.py 拆分（写/读/删/任务）；删除 sync 写入模式；删除硬编码槽位词表、自实现中英分词、restricted_or_unsafe 关键词拦截；统一 `memory_scope_id` 同名双义；清理死代码（`RebuildMemory*` schemas、`service.rebuild()`、所有 `_rebuild_*` / tombstone 系列方法） | 不变 | 业务行为不退化；service.py < 800 行；`ins_memory` 状态收敛为 `ACTIVE / DELETED` |
| **V3** | 仅在真实运营痛点出现后推进：外部历史源回放需求 → 加 `POST /memory/rebuild`；按真实流量分布扩展或调整 P0 槽位词表；按真实流量调整并发参数 | 视需求加 `POST /memory/rebuild` | 历史源回放可恢复一致性 |
| **终版** | 强治理：写入栅栏 / 删除墓碑 CAS / 衰减分片 / 人工治理台 / 线上 SLO | 视需求 | 强一致审计 |

每版必须满足：
- 业务闭环：写 → 读 → 删 → 再读已无；跨会话 L3 可召回（V1 起）
- 管理闭环：列表 → 编辑 → 召回为新值 → 删除后不再召回（V1.1 起）
- 测试全过：`make test` 单元测试 100%
- 文档与代码一致：契约表、env 变量、表结构表三处一致
- 不过度设计：不引入未来才用的字段、表、抽象层；不在 V_n 阶段交付 V_(n+1) 才会用的能力

## 1. 设计目标

Innies 记忆服务要解决的是多轮会话中的记忆连续性：

- 当前会话刚聊过的内容要能低延迟接上。
- 当前会话的一段阶段状态要能压缩成摘要。
- 用户长期稳定事实、偏好和事件要能跨会话召回。
- 用户显式删除、会话删除和重建不能让旧事实重新污染召回。

因此服务保留三层记忆：

| 层级 | 作用 | 当前作用域 |
| --- | --- | --- |
| L1 | 当前会话最近完整轮次缓存 | `user_id + session_id` |
| L2 | 当前会话阶段摘要 | `user_id + session_id` |
| L3 | 用户长期原子记忆 | `user_id + long_term_scope_id=innies` |

## 2. 三层分工

### L1：短期上下文

L1 保存当前会话最近若干完整 `user -> assistant` 轮次。
它只服务当前会话即时体验，不是可信归档，也不作为删除和重建的依据。

当前 L1 来源于可靠轮次记录的派生缓存。
缓存丢失只影响短期体验，不影响 L2/L3 的长期正确性。

### L2：阶段摘要

L2 保存当前会话阶段摘要。
它用于压缩近期状态、偏好变化和待跟进事项，避免每次召回都把完整轮次塞进上下文。

L2 有明确状态：

| 状态 | 召回行为 |
| --- | --- |
| `active` | 可进入召回结果 |
| `stale` | 可带降级标记进入召回结果 |
| `dirty` | 必须跳过，等待重建修复 |
| `rebuilding` | 重建中，不作为可信摘要使用 |

删除单条长期记忆或删除会话来源时，相关 L2 要标记为 `dirty`，
避免摘要继续携带已删除事实。

**V2（2026-09 实施）：L2 已 LLM 化**。写入路径为两级：append 同步写拼接占位（`domain.summarization.summarize_rounds`，最近 10 轮 user 消息），后台 `L2BackgroundRefresher` 按去抖间隔（默认每 5 轮、首轮立即）用 LLM 生成结构化综合摘要（主题/进行中事项/行为偏好/近期状态，`ins_current_summary.summary_kind=llm`），已有 llm 版时拼接占位不覆盖；LLM 失败保留拼接版降级。此前 V1 说明（纯拼接占位）：`repositories.summarize_rounds` 取最近 10 轮 `user` 消息内容用 "；" 拼接，**没有调用 LLM 做语义压缩**。
所以 V1 的 L2 实质是"最近 N 轮 user 消息列表"，能提供基本回放信号，但不能压缩、不能提取意图，召回侧体感与"L1 多带几轮"接近。
按业界主流（OpenAI ChatGPT Memory / Mem0 摘要管线），完整 L2 需要调 LLM 生成结构化摘要，列入架构 §0.2 V2 范围（与拆 service.py 同期推进）。
V1 的 L2 状态机（active/stale/dirty/rebuilding）和召回门禁仍然有效，本说明只澄清"摘要内容"的当前实现深度。

### L3：长期原子记忆

L3 由 Mem0 Library 承担长期记忆后端能力。业务读写和语义检索不得绕过 Mem0。
innies-memory 只允许在 readiness 中做诊断性依赖探测：
对 Milvus 使用 SDK 只读连通性检查；
对 Mem0 Library 只验证配置完整性和适配器构造，不执行 add/delete 写入探针。
readiness 用于暴露 LLM、Embedding、Mem0 或 Milvus 链路不可用；
它不等同于业务读写绕过 Mem0，也不执行业务 L3 语义检索。

Mem0 负责：

- 长期事实抽取。
- 语义检索。
- 过滤、阈值、数量限制和重排。
- 显式更新和删除能力。

Innies 记忆服务负责：

- API 契约和幂等。
- 用户和会话来源边界。
- `ins_memory` 业务索引、来源追踪和状态治理。
- L1/L2 管理。
- 删除、最小管理编辑、重建、任务状态和基础审计。

当前 L3 作用域固定为 `long_term_scope_id=innies`，并始终绑定 `user_id`。
这样长期记忆能跨会话召回，但不会跨用户串扰。

## 3. 写入链路

写入入口只接收完整轮次，不接收孤立单条消息。

```json
{
  "request_id": "req-20260511-append-001",
  "user_id": "user-20260511-001",
  "session_id": "session-20260511-a",
  "round_id": "round-20260511-001",
  "round_index": 1,
  "messages": ["user", "assistant"],
  "source_timestamp": "2026-05-11T10:00:03+00:00"
}
```

主流程：

1. 校验完整 `user -> assistant` 轮次。
2. 用 `round_id` 做幂等锚点，检测重复写入和冲突写入。
3. 写入或更新 `ins_memory_task`，任务 ID 为 `memory-extract:{round_id}`。
4. 将完整轮次写入 `ins_summary_round_journal`。
5. 安全准入通过后，异步模式刷新当前会话 L1/L2 并生成P0 槽位索引。
6. 同步模式先把轮次标为 `pending_append`，等 Mem0 写入成功后再刷新 L1/L2 和P0 槽位索引。
7. 同步模式把候选内容交给 Mem0 写入 L3，并把 backend memory id、来源和状态写入 `ins_memory`。
8. 异步模式先返回 `DEFERRED` 事件，L3 Mem0 抽取和任务完成状态由后台执行器继续推进。

当前 `MEMORY_L3_WRITE_MODE=async` 时，append 会同步完成可靠轮次、L1/L2
和P0 槽位索引，L3 Mem0 抽取进入后台执行器。
因此 append 响应成功不等于 L3 Mem0 抽取已经完成；需要看 `ins_memory_task`、
`l3_events` 和 `/memory/l3/background-status` 判断后台进度。

安全准入在写入早期执行。
系统提示词、内部控制指令、工具回显、密钥凭证、越权诱导和高风险注入内容不能进入 L2/L3，
也不能在重建时回流。
可靠轮次可作为删除来源或审计输入落入 `ins_summary_round_journal`，
但会被标记为删除来源并从 L2/L3 和重建输入中排除。

当前安全准入实现是基于关键词与正则的硬编码列表，词表见 `service.py::_text_is_restricted_or_unsafe`。
命中时**整轮跳过 L1、L2 和 L3** —— 即该轮不会出现在当前会话的 L1 缓存里，也不会沉淀到 L2 摘要或 L3 长期记忆。
这是首版的保守策略：宁可让一轮对话在记忆侧"消失"，也不允许 prompt injection 或凭据泄漏进入长期数据。
词表当前为通用占位，存在 false positive 风险（例如包含 `secret`、`password`、`token`、`忽略...指令`、`系统提示词` 等字面词的正常用户消息也会被命中）。
按真实业务流量分布调整 / 扩展词表是后续任务，需要由产品和数据团队提供 false positive / true positive 样本后再调整。

### 3.1 append 流程图(async 模式)

```text
client POST /memory/append
  ↓
get_round(round_id) ?
  ├─ existing & fingerprint 不一致 → raise round_id conflict (409)
  ├─ existing & task 在 {PENDING,RUNNING,COMPLETED} → return already_done
  └─ existing FAILED 或 new
        ↓
restricted_or_unsafe(messages) ?
  ├─ 命中 → 整轮跳过 L1/L2/L3,只落 round 并标 deleted_tombstone
  └─ 通过
        ↓
reserve_l3_background_write_slot()  ← 队列满抛背压 503
        ↓
save_task(RUNNING) ─────────── retry_count++ if FAILED 重试
        ↓
save_round(request)         ← ins_summary_round_journal 落库
        ↓
publish_append_round_locally  ← L1 缓存 + L2 摘要 同步刷新
        ↓
submit_l3_write(task) → 线程池后台执行
        ↓
return AppendMemoryResponse(status="completed", task_id, l3_events=DEFERRED)

   …… 后台 ……
_complete_async_l3_write(task_id):
    backend.add(messages)
        ├─ 成功 → save_task(COMPLETED) + index_l3_event(active backend_id)
        └─ 失败 → save_task(FAILED) + release_l3_slot
```

注：sync 模式与 async 的差异仅在第 7-8 步顺序(sync 先 L3 后 L1/L2,且失败时 round 留 `pending_append`),其余等价。

## 4. 召回链路

召回入口：

```text
POST /memory/recall
  user_id
  session_id
  query
  intent
  l3_limit
  l3_score_threshold
  token_budget
```

主流程：

1. 按 `user_id + session_id` 读取 L1。
2. 按 `user_id + session_id` 读取 L2；`dirty` 摘要跳过，`stale` 摘要可带降级原因返回。
3. 对 `chat` 意图查询 L3；`sensitive` 意图 fail-closed。
4. L3 按 `user_id + innies` 读取业务索引并调用 Mem0 检索。
5. 只返回仍处于 `ACTIVE` 状态、且符合当前查询边界的 L3。
6. 对 L1/L2/L3 做去重和预算裁剪。

删除确认、隐私请求和敏感信息查询采用 fail-closed 策略，不用降级结果伪装成可信记忆。

召回结果进入 prompt 时必须作为“记忆资料”使用，不能当作系统指令执行。

### 4.1 recall 流程图

```text
client POST /memory/recall
  ↓
intent ∈ {SENSITIVE} ?     ← 当前 V1 fail-closed 集合,详见 schemas.RecallIntent
  └─ Yes → raise ValueError(403 fail-closed)
  └─ No
        ↓
读 L1 (user_id + session_id) ─── 二次过滤 restricted/unsafe(兜底 append 漏掉的)
        ↓
读 L2 (user_id + session_id)
  ├─ ACTIVE       → 进结果
  ├─ STALE        → 进结果 + degradation=l2_stale
  ├─ DIRTY        → 跳过 + degradation=l2_dirty_skipped
  ├─ REBUILDING   → 跳过 + degradation=l2_rebuilding
  └─ 缺 / 其他    → 静默跳过
        ↓
must_query_l3 = (intent is CHAT) ?
  └─ Yes
        ↓
   active_memories(user_id, innies)         ← L3 业务索引
        ↓
   query_slot = _query_conflict_slot(query)  ← P0 槽位识别
        ↓
   ┌─ 命中槽位 → backfill 匹配 slot 的 active memory (source="business_index")
   └─ 未命中 / backfill 后仍空 → backend.search(query)  ← Mem0 语义检索
        ↓
   过滤:active backend_memory_id 不在业务索引内的丢弃
   过滤:命中槽位时 _memory_conflict_slot 必须匹配
        ↓
dedupe_and_clip(items, token_budget)
        ↓
return RecallMemoryResponse(status="ok", degraded, degradation_reasons, items)
```

## 5. 删除与重建

删除入口支持三个范围：

| 范围 | 输入 | 行为 |
| --- | --- | --- |
| `memory` | `memory_id` | 删除单条 L3，相关 L2 标脏 |
| `session` | `session_id` | 删除指定会话来源，清理该会话 L1，相关 L2 标脏 |
| `all` | `user_id` | 删除该用户 L3，清理该用户所有会话 L1/L2 来源，并写入用户级全量删除墓碑 |

跨会话连锁说明：一条 L3 长期记忆的 `source_refs` 可能跨多个 session（同一事实被多个 session 共同提及形成）。
当 `scope=memory` 删除这条记忆时，**所有相关 session 的 L1 缓存都会被清空、L2 摘要都会被标 `dirty`**。
这是 fail-safe 选择：宁可让多个 session 的短期上下文被刷新，也不允许已删除事实通过 L1 缓存或 L2 摘要继续被召回。
对调用方而言这意味着：用户在 session A 中删除某条记忆后，session B 的下一次召回会暂时失去这条来源的上下文，直到 B 内继续对话或触发重建。

范围删除不得调用 Mem0 原生全量清空接口。
当前安全策略是先从 `ins_memory` 找出当前用户范围内的 active backend id，
再逐条调用 Mem0 `delete()` 或经过验证的安全批量删除能力。

V1.1 补齐最小管理能力：

| 能力 | 输入 | 行为 |
| --- | --- | --- |
| 列表 | `user_id`、可选 `include_deleted` | 返回可管理 L3 业务记忆；默认只返回 `ACTIVE`；不返回系统墓碑 |
| 详情 | `user_id + memory_id` | 返回单条可管理 L3 业务记忆；不暴露内部作用域、backend id 或 source refs |
| 编辑 | `user_id + memory_id + operation_id + content` | 同步调用 Mem0 `update()`（本地 P0 索引不调用 backend 且必须保持同槽位），更新 `ins_memory`，相关 L1 清空、L2 标 `dirty` |

编辑任务 ID 为 `memory-update:{operation_id}`。
同一 `operation_id` 只能复用于同一用户、同一 `memory_id`、同一新正文指纹和同一记忆分类；
不同范围复用必须报冲突。
如果目标记忆已被后续编辑或删除，旧 update 的 `operation_id` 重试会返回冲突，而不是返回后续正文并伪装成旧操作的 `already_done`。
V1.1 不提供 CAS、版本号、人工审核、批量合并或强审计语义。

V1 不对外暴露 `POST /memory/rebuild` HTTP 端点：

- 重建是治理 / 运维能力，不是业务调用方应该触发的常规链路；按"内部服务、最小公开面"的取舍，V1 不把它放进公开 API。
- `MemoryService.rebuild()` 业务方法保留，作为内部能力供运维脚本、真实质量回归脚本和后续版本以 Python 级调用使用。
- 未来如需让调用方直接触发重建（例如对话编排服务在事实纠错失败后想强制刷新摘要），再单独评估是否补回 HTTP 端点。

重建语义（无论 HTTP 是否暴露都成立）：

| 来源 | 行为 |
| --- | --- |
| 外部历史源（`history_version` 提供且 `history_source` 已注入） | 优先使用历史对话源回放 |
| 无外部历史源 | 使用服务已接纳的可靠轮次回放 |
| 已删除来源（source-ref / delete-all / session-delete 三类墓碑命中） | 必须跳过，已删除事实不复活 |

用户级全量删除墓碑会阻止外部历史源把删除时间之前的旧轮次重新写入 L2/L3，避免删除后的事实复活。

### 5.1 delete 流程图

```text
client POST /memory/delete  {scope, operation_id, user_id, [memory_id], [session_id]}
  ↓
validate_delete_required_identifiers
  ├─ scope=memory & 无 memory_id  → 400 memory_id is required
  ├─ scope=session & 无 session_id → 400 session_id is required
  └─ scope=all & 带 memory_id/session_id → 422
        ↓
get_task(memory-delete:{operation_id}) ?
  ├─ existing & scope 不一致 → raise operation_id conflict
  ├─ existing PENDING/RUNNING → return running
  ├─ existing COMPLETED       → return already_done
  └─ existing FAILED 或 new
        ↓
save_task(RUNNING) ── retry_count++ if FAILED 重试
        ↓
分支(by scope):
┌──────────────────────────────────────────────────────────────┐
│ scope=memory                                                  │
│   1. 取 active(或重试时 list)中 memory_id 对应的一条          │
│   2. backend.delete(backend_memory_id)  ← P0-5 IndexError 兜底│
│   3. mark_memory_deleted_for_operation(memory)                │
│   4. mark_superseded_slot_sources_deleted (清同槽位旧 super)  │
│   5. 对该 memory.source_refs 涉及的所有 session:              │
│       clear_l1 + mark_summary(DIRTY)   ← D1 跨 session 连锁   │
├──────────────────────────────────────────────────────────────┤
│ scope=session                                                 │
│   1. 拉 active 候选;每条命中 session_id 的 memory:            │
│       - 还有其他 refs → update_source_refs(去掉该 session)    │
│                       + add_deleted_source_ref_tombstone      │
│       - 仅来自该 session → backend.delete + 标 DELETED       │
│   2. clear_l1 + mark_rounds_deleted + mark_summary(DIRTY)     │
│   3. add_session_delete_tombstone (时间墓碑)                  │
│   4. add_deleted_source_ref_tombstones_for_history (若有源)   │
├──────────────────────────────────────────────────────────────┤
│ scope=all                                                     │
│   1. 全部 active L3:逐条 backend.delete                      │
│   2. 逐条 mark_memory_deleted_for_operation                  │
│   3. add_delete_all_tombstone (用户级时间墓碑)                │
│   4. 该用户所有 session:clear_l1 + mark_rounds_deleted        │
│      + mark_summary(DIRTY)                                    │
│   5. add_deleted_source_ref_tombstones_for_history            │
└──────────────────────────────────────────────────────────────┘
        ↓
save_task(COMPLETED 或 RUNNING(若 cleanup 仍 pending))
        ↓
return DeleteMemoryResponse(status, task_id, affected_memories, summary_state=DIRTY)
```

### 5.2 task 状态机

```text
                          ┌─────────────┐
                          │   PENDING   │ ← 极少经过(当前直接构造为 RUNNING)
                          └──────┬──────┘
                                 │
                                 ↓
       new / FAILED 重试 → ┌─────────────┐
                          │   RUNNING   │
                          └──────┬──────┘
                                 │
                ┌────────────────┼────────────────┐
                ↓                ↓                ↓
         ┌───────────┐     ┌───────────┐    [进程被强杀]
         │ COMPLETED │     │  FAILED   │           │
         └─────┬─────┘     └─────┬─────┘           ↓
               │                 │            RUNNING 卡死
               │                 │            (V1 手工 SOP:
               │                 │             见部署文档 §2.10.6)
               │                 │            (V2 自动 reaper)
        终态(同 round_id    可重试 → 进入新一轮
        / operation_id      RUNNING(retry_count++)
        命中 already_done)
```

幂等保证:
- append: `round_id` 作为业务键;同一 round_id 重复命中除 FAILED 外的状态都 return already_done。
- update / delete / rebuild: `operation_id` 作为业务键;额外校验 task.scope 与请求 scope 一致(不允许同 operation_id 用于不同 user/scope/memory/session/内容指纹)。
- update: 完成后的重试只有在目标记忆仍是该次编辑结果时返回 already_done;目标已被后续编辑或删除时返回冲突。

## 6. 数据落点

当前首版迁移创建四张业务表：

| 表 | 职责 |
| --- | --- |
| `ins_summary_round_journal` | 可靠轮次账本，保存已接纳完整轮次 |
| `ins_current_summary` | L2 当前摘要和摘要状态 |
| `ins_memory` | L3 业务索引，关联 Mem0 backend memory id、来源和治理状态 |
| `ins_memory_task` | 写入、删除、重建任务状态和错误原因 |

表职责边界：

- `ins_summary_round_journal` 不是 L1 缓存。
- `ins_current_summary` 不是历史归档。
- `ins_memory` 不是向量库，也不替代 Mem0。
- `ins_memory_task` 不保存记忆正文。

## 7. 首版能力边界

首版必须做到：

- 公开 API 只要求 `user_id` 和 `session_id`。
- 写入以 `round_id` 幂等。
- L1/L2 按会话隔离。
- L3 按用户隔离并可跨会话召回。
- 召回返回的 L3 `memory_id` 是业务记忆 ID，可直接用于管理编辑或删除。
- 长期记忆支持最小管理查询和单条编辑。
- 删除和重建有任务状态。
- 被删除事实不继续召回。
- 遵守第 2 节 L3 边界：业务路径走 Mem0，readiness 只做诊断性依赖探测。

暂不把以下能力作为首版主路径前置条件：

- 写入栅栏。
- 删除墓碑 CAS。
- 语义抑制。
- 衰减分片。
- 人工治理台。
- 全链路强一致审计系统。

这些属于稳定性增强或强治理阶段，应该在真实并发、合规和恢复需求明确后再启用。

## 8. Innies需求适配性分析

当前结论：首版主链路符合 Innies 对会话型记忆服务的 P0 需求。
这里的 P0 需求指用户在不同会话中持续使用 Innies 时，系统能保存当前会话上下文、
沉淀长期稳定事实、处理事实修正、支持显式删除，并且不发生跨用户串扰。

### 8.1 需求判断口径

本节按三类结论判断当前服务是否符合 Innies 记忆需求：

| 判断 | 含义 | 是否可进入首版主链路 |
| --- | --- | --- |
| 适配 | 已有代码路径、数据落点和自动化测试或真实质量评测覆盖 | 可以 |
| 部分适配 | 主链路可用，但高并发、治理、合规或长期 SLO 仍需运维和后续版本补强 | 可以，但要带边界 |
| 不适配 | 超出当前记忆服务职责，或会把记忆服务误用成其他系统 | 不进入首版 |

当前方案不再以角色 ID 作为记忆分区。
原因是 Innies 记忆服务面向用户会话和用户长期偏好，
而不是面向某个可替换角色的独立记忆。
公开 API 只要求 `user_id` 和 `session_id`，L1/L2 以会话隔离，
L3 以用户长期作用域 `innies` 隔离。
这能避免把用户长期偏好错误绑定到某个角色，同时保留跨会话召回能力。

### 8.2 需求到实现证据链

本节只回答“当前实现是否覆盖 Innies P0 记忆需求”。

不在本节展开的内容：

- 接口契约见 [工程实现与运行边界](Innies记忆三层工程实现与运行边界.md)。
- 部署参数见 [部署与运维交付文档](Innies记忆服务部署与运维交付文档.md)。
- 本地验证和排障步骤见 [开发手册](Innies记忆服务开发手册.md)。

| 证据分组 | 阅读口径 |
| --- | --- |
| 主路径需求 | 用户体验必须成立的能力 |
| 交付边界 | 运维、治理和非目标 |

代码入口和测试证据见第 8.3 节。
完整能力边界见第 8.4 节，避免把 P0 主链路说成完整平台能力。

#### 主路径需求

| 需求 | 判断 | 证据入口 | 当前边界 |
| --- | --- | --- | --- |
| 会话连续性 | 适配 | L1 最近轮次，L2 阶段摘要 | 同一会话继续聊天和上下文压缩 |
| 跨会话长期事实 | 适配 | L3 固定为 `user_id + long_term_scope_id=innies` | 用户稳定事实、偏好和事件 |
| 事实纠错 | 适配 | `ACTIVE` 过滤和冲突槽位 `SUPERSEDED` | P0 槽位按新事实覆盖旧事实 |
| 删除与重建 | 适配 | 删除更新业务索引，相关 L2 标为 `dirty` | 重建跳过已删除来源 |
| 用户隔离 | 适配 | L1/L2/L3 查询都绑定 `user_id` | Innies 多用户基础隔离 |
| 隐私和安全边界 | 适配 | 隐私、敏感和删除确认类召回 fail-closed | 高风险内容不进入 L2/L3 |
| 异步写入体验 | 部分适配 | async 模式先落可靠轮次、L1/L2 和 P0 槽位 | 容量、失败重试和长期 SLO 依赖生产压测 |

当前 P0 槽位词表覆盖通用对话场景的高频事实（昵称、宠物名、地点、生日、工作状态、沟通偏好、饮食偏好等），用于跑通主链路。
后续如果需要按真实业务流量分布扩展或调整词表，由产品和数据团队提供样本后再迭代。

#### 交付边界

| 需求 | 判断 | 实现证据 | 边界 |
| --- | --- | --- | --- |
| 工程可运维 | 部分适配 | PostgreSQL 记录可靠轮次、L2、L3 业务索引和任务状态 | 监控、告警、容量和备份策略仍由运维落地 |
| 质量门禁 | 适配 | 质量回归脚本和单元测试覆盖召回、污染、删除、重建和隔离 | 可作为首版上线前验收基线 |
| 人工治理 | 部分适配 | 当前有状态、来源和任务记录 | 没有治理台、人工审核流和批量纠偏界面 |
| 知识库/RAG | 不适配 | L3 只保存用户长期原子记忆 | 不承载文档切片、业务知识和全局检索 |

### 8.3 代码与测试证据

核心代码证据：

- `AppendMemoryRequest` 强制写入完整 `user -> assistant` 轮次，并拒绝内部 `memory_scope_id` 字段。
- `RecallMemoryRequest` 只暴露 `user_id`、`session_id`、`query`、`intent`、阈值和预算，
  不暴露长期作用域内部字段。
- `DeleteMemoryRequest` 支持 `memory`、`session`、`all` 三类删除范围，
  并拒绝当前范围无关的 `memory_id` 或 `session_id`。
- `RebuildMemoryRequest` 支持按用户或按会话重建，并可带 `history_version` 做历史源版本校验。
- `MemoryService.LONG_TERM_SCOPE_ID = "innies"` 固定 L3 长期作用域，
  避免角色分区污染用户长期偏好。
- `MemoryService.append()` 以 `round_id` 幂等写入可靠轮次、L1、L2 和 L3 业务索引。
- `MemoryService.recall()` 先读当前会话 L1/L2，再按 `user_id + innies` 查询 L3，
  只返回 active 业务索引记录。
- `MemoryService.delete()` 不直接使用 Mem0 全量清空接口，
  而是按当前用户和范围定位 active backend id 后删除。
- `MemoryService.list_memory_items()` / `get_memory_item()` 只返回可管理业务记忆，不暴露内部作用域、backend id 或 source refs。
- `MemoryService.update_memory()` 以 `operation_id` 幂等编辑单条 active L3，写穿 Mem0 backend 和 `ins_memory`，并把相关 L2 标 `dirty`。
- `MemoryService.rebuild()` 使用可靠轮次或外部历史源重放，并跳过已删除来源。

自动化测试证据：

- `test_l3_memory_is_available_across_new_sessions_for_same_user` 覆盖同一用户跨会话长期召回。
- `test_l2_summary_is_scoped_to_current_session` 覆盖 L2 不跨会话串用。
- `test_privacy_and_sensitive_recall_fail_closed` 覆盖隐私和敏感召回 fail-closed。
- `test_async_l3_write_queue_is_bounded_before_append_mutates_state` 覆盖异步队列满时不提前污染状态。
- `test_deleting_current_slot_prevents_rebuild_from_restoring_superseded_old_slot_memory`
  覆盖删除当前事实后重建不复活旧事实。
- `test_recall_l3_item_exposes_business_memory_id_for_management_delete`
  覆盖召回结果可直接用于管理删除。
- `test_update_memory_updates_backend_index_and_marks_source_summaries_dirty`
  覆盖编辑写穿 backend、本地索引和 L2 脏标记。
- `test_update_memory_supersedes_conflict_even_when_backend_cleanup_fails`
  覆盖编辑纠错时旧冲突记忆先从 active 业务索引下线，backend 清理失败不阻断新事实生效。
- `test_list_memory_items_hides_deleted_memories_by_default_without_internal_fields`
  覆盖管理列表默认隐藏删除项且不暴露内部字段。
- `test_rebuild_does_not_restore_memory_from_deleted_source_round` 覆盖删除来源在重建时被跳过。
- `test_memory_requests_reject_internal_scope_fields` 覆盖公开 API 不暴露内部作用域字段。
- 真实质量回归脚本和相关单元测试覆盖召回率、准确率、旧值污染、误召回、重复 active、
  删除残留、重建复活和跨用户泄漏。

这些证据说明当前服务不是只在文档上“声称适配”，而是已有代码路径和测试门禁支撑。

### 8.4 不完全适配与边界

首版不承诺完整覆盖下列能力；这些能力属于不完全适配或明确不适配的范围：

- 不是知识库记忆：不保存文档切片、全局业务知识、工具说明或产品资料。
- 不是强一致审计系统：不承诺写入栅栏、删除墓碑 CAS、全链路强一致审计和长期不可抵赖证明。
- 不是人工可治理记忆平台：没有运营审核台、人工纠错流、批量合并、衰减分片和语义抑制界面。
- 不承诺长期线上 SLO、容量上限和稳定性结论；这些需要生产容量模型、压测和监控数据。
- 不承诺把 L3 作为唯一上下文来源；业务调用方仍要把召回结果作为“记忆资料”，
  不能当系统指令执行。
- 不承诺自动理解所有用户事实类型；首版覆盖通用对话场景的高频槽位（详见 8.2 节）。

这些边界不阻塞 P0 主链路，但必须进入产品、研发和运维的共同认知。

### 8.5 最终判断

当前服务可以作为 Innies 首版记忆底座进入联调。
理由是它已经覆盖会话连续、跨会话长期事实、事实纠错、显式删除、删除后重建、
用户隔离、基础安全和工程交付。

它不适合被包装成完整的知识库、强一致审计系统或人工治理平台。
在高并发、强隐私合规、人工治理和长期稳定性要求明确后，
再推进写入栅栏、删除墓碑 CAS、治理后台、衰减/抑制策略和线上 SLO。
