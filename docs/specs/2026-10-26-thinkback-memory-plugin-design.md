# Thinkback 记忆插件化设计（Mem0 固定为 L3 引擎）

日期：2026-10-26 · 状态：待批准 · 修订：v2

关联文档：

- `docs/plans/2026-10-19-thinkback-memory-architecture-rewrite.md`（记忆模型重构）
- `docs/plans/2026-10-21-thinkback-hardening-pass.md`（加固批次）
- `docs/report/2026-10-26-mainstream-memory-landscape.md`（主流项目对照）
- `.planning/2026-10-26-thinkback-memory-audit/findings.md`（审计证据）

修订记录：

- v2 修正：既有计划多项已提交，原文误标为待做；L1 缓存 TTL 已完成；补全文件路径；补齐审计项的阶段归属；把 10-19 计划与“三层保留”冲突的三处显式列出；补入独立 reviewer 提出的 10 个待裁决问题。

---

## 0. 决策

| 编号 | 决策 | 影响 |
| --- | --- | --- |
| D1 | 引擎固定为 Mem0（mem0ai 2.1.0，Milvus 为其存储） | 不评估 Zep/Graphiti/Letta 替换任何一层 |
| D2 | 三层保留：L1 轮次原文、L2 会话上下文视图、L3 长期事实 | 三层语义由 Thinkback 定义并对外承诺 |
| D3 | 插件形态是 sidecar/service，外部走版本化 API | 不做进程内嵌入式 SDK（理由见 §13） |
| D4 | 不引入图存储、不引入 Redis/Celery | 进程内状态迁到 PostgreSQL，单副本起步 |
| D5 | 服务从未上线，可做破坏性契约变更 | 直接设计 v2 契约，不需要兼容期 |
| D6 | Mem0 + Milvus 默认开启，是 L3 的必需路径 | 10-19 Task 4.1「Milvus 默认关」不采纳；“纯 PG 跑通全部评测”不是目标 |
| D7 | L2 保留为层，重写实现而不是删层 | 10-19 Task 3.5「删 L2」不采纳，改为“有界 + 版本化 + 可失效”的上下文视图 |
| D8 | legacy `/memory/*` 在生产不可达 | 10-19 Task 3.1「旧路由保持可用」不采纳 |
| D9 | 引擎优先原则：引擎能做的交给引擎，引擎做不好的才补，引擎完全没有的才自己做 | 见 §2.4 能力划分、§2.5 删除清单 |
| D10 | 部署在内网，业务接口不做鉴权；信任边界是网络边界 | 删除 API Key / Scope / X-Tenant-Id 校验；`/admin/api/*` 保留 ADMIN_TOKEN 作为破坏性操作闸门；namespace 降为业务分组标签，靠字符集校验避免碰撞 |

D1+D6 的推论：10-19 的目标模型（Profile + 叙事 Collection + state TTL + procedural + 承诺，Milvus 可选）不是三层模型。本设计保留它的**类型化思路**，但把类型放回三层之内。

## 1. 目标与非目标

### 目标

1. 任意企业 AI 助手可接入：HTTP 为主，gRPC 同步升版，SDK 由契约生成，MCP 作为后续传输适配。
2. 通过配置决定“写哪些记忆、怎么写、谁能读、留多久”，而不是改代码。
3. 每层行为可验证：写入完成状态、删除结果、召回来源都能被调用方和审计方检查。
4. 三层不互相污染：L1 是唯一原始证据，L2 是可失效的派生视图，L3 是可重建的派生事实。

### 非目标

1. 不做进程内嵌入式库（见 §13）。
2. 不做知识图谱、多跳关系推理。
3. 不替换 Mem0，也不承诺 Mem0 之外的抽取质量。
4. 不在本次范围内重写治理台前端。

## 2. 与既有计划的关系

### 2.1 已提交，不再重复

`git log` 确认以下已在当前分支落地（计划文件未回填勾选，容易误判）：

| 提交 | 内容 |
| --- | --- |
| `3f23ff6` | API Key 真校验、租户命名空间贯穿存储层、管理面 fail-closed |
| `8ce69c3` | L2 摘要原子写、并发插入不 500、L1 缓存 TTL、召回索引 |
| `3c13396` | L3 槽位不泄漏、DEAD_LETTER 终态、CAS 耗尽不谎报成功 |
| `d8010b8` | 排除集按用户跨 scope 聚合、召回计数只在真正注入后 |
| `76c76d9` | Prometheus counter 单调、迁移 advisory lock |
| `36dd4af` | gRPC 加 API Key 鉴权、l3_limit 与阈值对齐 HTTP |
| `e4c26d4` | 探活只读、token 估算按字符集分档、预算裁剪取排名前缀 |
| `10429b7` | 删 mem0_config 死链与三处死代码 |

### 2.2 仍待做

| 既有计划条目 | 状态 | 本设计的处理 |
| --- | --- | --- |
| 10-19 Task 0.4 legacy 路由鉴权 | 未做 | 升级为 P0，改为生产不可达（D8） |
| 10-19 Task 0.11 后半（score 进排序键） | 未做 | 并入 §6 召回改造 |
| 10-19 Task 1.1-1.3 评测先行 | 未做 | 保留，但语料需按企业场景重写，见 §10 |
| 10-19 Task 2.1-2.5 Profile 通道 | 未做 | Profile 降级为 L3 的一个类型，见 §4.3 |
| 10-19 Task 3.2 删 domain/slots | 未做 | 随类型注册表落地后删除，见 §4.3 |
| 10-19 Task 3.3 物理删除 | 未做 | 拆成 suppress / erase，见 §7 |
| 10-19 Task 3.4 删 rebuild | 未做 | 保留 reconcile 幂等命令，删除在线 rebuild |
| 10-19 Task 3.5 删 L2 | **与 D7 冲突** | 不删层，重写实现，见 §3.2、§12 问题 1 |
| 10-19 Task 3.6 decay → state TTL | 未做 | 并入类型化保留策略 |
| 10-19 Task 3.7 procedural 独立 | 未做 | 作为 L3 的一个类型，读路径是提示词装配，见 §4 |
| 10-19 Task 3.8 承诺通道 | 未做 | 作为 L3 的一个类型，读路径是独立到期通道 |
| 10-19 Task 4.1 Milvus 默认关 | **与 D6 冲突** | 不采纳，见 §12 问题 2 |
| 10-19 Task 4.2 单副本 | 已做（`replicas: 1`） | 沿用；进程内状态迁到 PG 后再考虑多副本 |
| 10-21 T6 计划回填 | 未做 | P0 收尾时补 |

### 2.3 审计新增项与阶段归属

| # | 问题 | 证据 | 阶段 |
| --- | --- | --- | --- |
| N1 | tenant 与 user 用 `:` 拼接可碰撞 | `src/thinkback/domain/tenant.py:18-43` | P1（namespace 字符集校验；免鉴权后不是越权，是数据混用） |
| N2 | v1 任务查询无归属包装，可读他人任务 | `src/thinkback/api/v1_memory.py:219-227` | P0（任务按 user 过滤；内网也属信息泄露） |
| N3 | gRPC 只认证不校验 Scope，只读 key 可 Delete | `src/thinkback/rpc/servicer.py:96-119,164-179` | P1（Scope 随鉴权删除，路径一并消失；保留 per-user 过滤） |
| N4 | 删除与在飞写入竞争，已删内容复活 | `src/thinkback/memory/service.py:3336-3355,4191-4198` | P1 |
| N5 | L2 旧刷新覆盖删除结果，摘要回到 active | `src/thinkback/memory/l2_refresh.py:131-159` | P1 |
| N6 | backend.update 失败被吞，响应仍 completed | `src/thinkback/memory/service.py:3810-3870` | P1 |
| N7 | 幂等缓存无路由维度；指纹 `|` 拼接可碰撞 | `src/thinkback/api/idempotency.py:56-75`、`src/thinkback/domain/keys.py:24-34` | P1 |
| N8 | assistant 内容可作为长期事实来源 | `src/thinkback/memory/service.py:3321-3335` | P1（与来源信任一并处理） |
| R | data_classification 无任何行为，sensitive 仍落库并外发 | `src/thinkback/domain/enums.py:47-53` | P1（分类准入） |
| S | 自定义 metadata 被丢弃，memory_type 静默改写 | `src/thinkback/memory/service.py:4058-4092` | P2（类型注册表） |
| T | 白名单 + `force_extract` 产生不可召回暗数据 | `src/thinkback/domain/slots/support.py:143-205` | P2（抽取前统一准入） |
| B | 预算裁剪使 L1/L2 完全不返回，甚至返回空 | `src/thinkback/domain/recall_policy.py:191-212` | P2（分层预算） |
| M | 语义召回后词典交集硬过滤，误杀同义记忆 | `src/thinkback/domain/slots/query_slots.py:131-146` | P2（召回装配重写） |

## 3. 目标架构

### 2.4 引擎优先原则下的能力划分

原则（D9）：引擎能做的交给引擎；引擎做不好的才自己补；引擎完全没有的必须自己做。

Mem0 OSS 2.1.0（当前安装版本）原生能力与缺口：

| 能力 | 引擎现状 | 证据 |
| --- | --- | --- |
| 事实抽取 | 有，ADD-only 模型 | `mem0/memory/main.py:978-981` 解析 LLM 输出的 `memory` 键 |
| 每次写入自定义抽取指令 | 有，`add(prompt=...)` | `main.py:769-771` |
| 语义检索 | 有 | `main.py:1388-1432` |
| 混合检索（BM25 + entity matching） | **没有**，2.2.x 才引入 | 安装包内无 bm25 / keyword_search / entity matching 实现；官方迁移文档说明该能力随新算法发布 |
| 过期与 TTL | 有，`expiration_date` + `show_expired` | `main.py:788-789,1260-1271` |
| 记忆分类 | **仅 `procedural`**，其它值抛 `Mem0ValidationError` | `main.py:831-835` |
| 事实时间锚点 / as-of 查询 | **没有**，OSS 直接抛错 | `main.py:787,817-818`（`timestamp`）、`main.py:1421-1433`（`reference_date`）均为 Platform-only |
| 线程 / 会话 | 没有 | Mem0 只有 user / agent / run 三种 scope |
| 租户 / 鉴权 / 限流 | 没有 | Mem0 只有 filters |
| 队列 / 重试 / 崩溃恢复 | 没有，同步库调用 | `Memory.add` 是同步方法，无任务语义 |
| 删除时清理历史 | 没有 | `delete` 只删向量行，history 表保留旧值 |
| token 预算 / 上下文装配 | 没有 | `search` 只返回 top-k 事实 |

结论：**L3 的语义归 Mem0**。L1 事件账本、L2 会话上下文、租户与身份、可靠执行、上下文装配、擦除与审计、协议与 SDK 归 Thinkback，因为这些 Mem0 一个都不做。

### 2.5 从 Thinkback 删除（重复造轮子）

| 现有实现 | 规模 | 处理 | 替代 |
| --- | --- | --- | --- |
| `src/thinkback/domain/slots/` 固定槽位引擎 | 1,270 行 + service 约 550 行引用 | 删 | Mem0 抽取 + 类型注册表 |
| 词典交集硬过滤 `context_terms` | 约 100 行 | 删 | Mem0 2.2.x 的 BM25 + entity matching |
| 自造事实排序分（层优先级 + quality + strength） | 约 80 行 | 降为只做预算分配 | Mem0 融合分负责事实排序 |
| `src/thinkback/memory/decay.py` 清扫器 | 155 行 | 删 | Mem0 `expiration_date` + `show_expired` |
| 本地 `valid_at/invalid_at` 生命周期 | 约 60 行 | 保留但收窄 | 事实时间 OSS 不支持，只能自建；先只做失效标记，不做 as-of 查询 |
| 10-19 Task 4.1「Milvus 默认关」 | - | 不采纳 | 引擎既然固定，语义层不能用自造实现替代 |
| 治理台 mem0 提示词编辑 | 已删 | 改为按类型传 `add(prompt=...)` | Mem0 原生每次写入可带抽取指令 |

## 3. 目标架构

### 3.1 外部深模块

调用方只认识 5 个操作，不认识 Mem0、Milvus、任务 CAS 或类型注册表。

```text
append(events, idempotency_key) -> WriteReceipt
recall(query, options)           -> ContextPackage
get_operation(operation_id)      -> OperationStatus
suppress(scope) / erase(scope)   -> OperationReceipt
get_source(source_ref)           -> SourceEvent
```

`WriteReceipt` 逐层报告状态：

```json
{
  "operation_id": "op_...",
  "status": "accepted",
  "layers": {
    "journal": {"state": "stored", "version": 12},
    "context_view": {"state": "pending"},
    "facts": {"state": "pending", "engine": "mem0", "engine_version": "2.1.0"}
  }
}
```

### 3.2 三层职责

| 层 | 真值来源 | 状态机 | 对外承诺 |
| --- | --- | --- | --- |
| L1 轮次原文 | PostgreSQL `ins_summary_round_journal` | `active / pending_append / deleted_tombstone` | 幂等写入；原文可回放；消息序列不限 user/assistant 两条（支持 tool 与中断回合）；不因召回策略丢失 |
| L2 会话上下文视图 | PostgreSQL `ins_current_summary` | `active / stale / dirty / rebuilding` | 有界长度、版本化、可失效；陈旧必须标记；删除后不得被旧刷新复活；不复制原文当摘要 |
| L3 长期事实 | Mem0 + Milvus，PG 侧存治理索引 | `ACTIVE / DELETED / SUPERSEDED / SUPPRESSED` | 语义由 Mem0 负责（抽取、混合检索、去重、过期）；Thinkback 只负责治理索引、来源引用与删除语义 |

L2 的实现重写，但层的契约不变。10-19 对 L2 的批评成立：concat 是原文副本不是摘要，llm 模式每次全量刷是 O(N²)。保留层、重写实现：

- 触发：滚动阈值或闲置时长，二选一由配置决定。
- 内容：有界（按 token 预算而非固定轮数），由 LLM 产出。
- 写入：带版本号的条件更新，删除或新快照都会让旧快照失效。
- 与 L3 重叠的部分（偏好、状态）交由 L3 的类型承担，L2 只做“这一段会话发生了什么”的叙事视图。

### 3.3 规范模型

```text
SourceEvent    event_id, source, role, subject_ref, occurred_at, received_at,
               payload, provenance, trust
MemoryRecord   record_id, type, schema_version, content, status, kind,
               valid_from, valid_to, recorded_at, source_refs, classification,
               review_state, extractor_version, embedder_version, policy_version
MemoryScope    tenant_id, namespace, subject_id, session_id
```

- `tenant_id` 独立成字段，不再拼进 `user_id`（阶段见问题 3）。
- `namespace` 表达层级（企业 / 应用 / 助手），不拼接存储。
- `recorded_at` 与 `valid_from` 分开。现有 `valid_at/invalid_at` 是事实时间，补 `recorded_at` 后才具备 as-of 能力。
- 每条记忆带 extractor/embedder/policy 版本，Mem0 升级或策略变更后可解释、可重建。

### 3.4 扩展点

只保留三处真正会变的接缝。

| 接缝 | 职责 | 今天的实现 | 扩展方式 |
| --- | --- | --- | --- |
| `MemoryTypePolicy` | 某类记忆的抽取、准入、冲突、保留、召回路径、审批 | `src/thinkback/domain/slots/`（1,270 行，固定 8 槽位）+ `src/thinkback/memory/service.py` 中约 550 行引用 | 代码注册 + 配置选择 |
| `MemoryStore` | 记忆记录与索引的持久化 | `src/thinkback/memory/repositories/sqlalchemy.py` | 新表或新后端实现同一契约 |
| `TransportAdapter` | 外部协议 | HTTP v1、gRPC v1 | v2 契约 + MCP |

Mem0 固定为 L3 的默认抽取实现，不作为可替换接缝暴露给调用方。

## 4. 记忆类型与配置

### 4.1 类型注册表

类型由代码注册（抽取逻辑、冲突规则、schema），由配置选择（启用、范围、保留、分类、召回参数、信任角色）。配置不执行任意代码。

```yaml
version: 1
namespace: acme/support
types:
  profile.core:
    layer: facts
    extractor: profile.v1
    source_roles: [user]
    classification: personal
    conflict: overwrite
    retention: forever
    recall: {path: fact, budget: 600}
  preference:
    layer: facts
    extractor: mem0.collection
    source_roles: [user]
    classification: personal
    conflict: overwrite
    retention: forever
    recall: {path: fact, budget: 400}
  procedure:
    layer: facts
    extractor: none
    source_roles: [admin]
    classification: restricted
    review: required
    recall: {path: instruction}
  commitment:
    layer: facts
    extractor: mem0.collection
    source_roles: [user, tool]
    classification: personal
    conflict: append
    retention: ttl
    ttl_days: 30
    recall: {path: schedule}
```

`recall.path` 决定该类型如何进入上下文：`fact` 进事实池，`instruction` 进提示词装配，`schedule` 走独立到期通道，不与事实抢预算。

### 4.2 来源信任

`trust` 与 `review_state` 入库必填，且**由服务端推导**，不接受调用方自报。

- `trust` 由认证主体、事件来源与配置的 `source_roles` 决定：调用方传什么不影响判定。
- 只有 `role=user` 或受信工具事件能成为用户事实。
- `role=assistant` 产生的内容不进入用户事实，作为候选事件留在 L1，不再被当长期事实。
- 程序性记忆必须 `review_state=approved` 才能召回，来源限管理员或受信系统。
- 所有召回内容统一标记为不可信数据，调用方在自己的提示层做指令与数据隔离。

### 4.3 现有 P0 槽位的去向

10-19 判定 1,270 行正则槽位引擎应删除，这部分沿用。差别是删除后不留缺口，而是变成注册表里的 `profile.core` 一个类型：抽取、冲突、证据校验都在类型内部，`MemoryService` 不再认识任何具体槽位。

新增“过敏史”“用药记录”这类类型时，写一个类型实现加一段配置，不动编排器。

## 5. 写入与执行

### 5.1 状态语义

`accepted` 表示 L1 已落库并已登记后台工作。`completed` 只在三层都到终态时使用，通过 `get_operation` 查询。

### 5.2 持久执行

- 原始事件与 outbox 行在同一 PostgreSQL 事务写入。
- worker 领取任务时记录 `owner`、`attempt`、`lease_until`，执行中续租。
- 每个抽取事件有稳定 ID（`operation_id + fact_hash`），重试只补缺失事件，不整批重放。
- 后端写失败进入持久重试与对账，不用“以后 rebuild 会修”这种 best-effort。
- 孤儿回收改为重新领取并从 journal 重放，而不是只把状态改成 failed。

### 5.3 进程内状态清单

| 现状 | 位置 | 目标 |
| --- | --- | --- |
| API Key 表 | `src/thinkback/api/auth.py:86` | `ins_api_key`（只存 secret hash） |
| 幂等缓存 | `src/thinkback/api/idempotency.py:106` | `ins_idempotency`（唯一键含 tenant/method/path/key） |
| 限流桶 | `src/thinkback/api/ratelimit.py` | 先保持进程内，补 LRU 上限；多副本需求出现时再评估 |
| L1 缓存 | `src/thinkback/memory/repositories/_l1_cache.py` | 已有 30s TTL；补 LRU 上界（键无界） |
| L2 去抖计数 | `src/thinkback/memory/l2_refresh.py:63-70` | L2 重写时一并收敛为有界结构 |

## 6. 召回

1. **角色与证据保留**：L1 按角色分段，不拼成无标签文本；每条记忆带 source_refs。
2. **复合查询**：不再因为命中一个槽位就跳过语义检索。精确槽位只用于单一事实 lookup。
3. **排序**：L3 内部由 Mem0 的融合分负责（semantic + BM25 + entity matching），Thinkback 不再自造事实排序分。Thinkback 只决定“哪些层、哪些类型进入上下文”。
4. **分层预算**：L1/L2/L3 与各类型各有配额，避免长期事实吃光当前对话；单条超预算时截断或跳过并记原因，不返回空包。
5. **proactive 标记**：沿用 10-19 Task 2.5，返回条目加 `proactive=false` 与原因，由调用方决定是否注入。
6. **不可信数据边界**：所有层返回内容统一标记；L2 prompt 明确要求把轮次内容当数据。
7. **不重排引擎结果**：Thinkback 收到 Mem0 的 top-k 后，只做过滤（按本地索引状态、来源、信任、分类）与预算裁剪，不改写引擎给出的顺序权重。

## 7. 删除语义

| 操作 | 效果 | 适用 |
| --- | --- | --- |
| `suppress` | 不可召回，保留记录与墓碑 | 防止复活、逻辑下架 |
| `erase` | 物理删除：PG 原文、Milvus 记录、Mem0 history、缓存、审计中的原文 | 遗忘权与合规 |
| `forget_all` | 某租户或 subject 的全部记忆，按上面规则处理 | 用户注销、数据导出删除 |

`erase` 的级联清单必须逐项验证：journal messages、context view 文本、ins_memory 行、Milvus collection、Mem0 SQLite history、进程内缓存、审计日志原文。审计只留范围、数量、时间与操作人。

Mem0 2.1.0 没有 history 清理接口，history 表结构由 Mem0 内部定义且文件按 Pod 共享，物理擦除机制需要单独决策（见问题 4）。

## 8. 安全

| 项 | 目标状态 |
| --- | --- |
| legacy `/memory/*` | 删除或并入 v2 单一业务面；免鉴权后两套路由没有安全差别，重复面只剩维护成本 |
| namespace | 业务分组标签，靠字符集校验（禁 `:`、限长）避免与 user_id 拼接碰撞；`ins_memory.memory_scope_id` 列已存在，直接承载 namespace，不做 schema 迁移 |
| 业务鉴权 | 不做。内网信任边界 = 网络边界，需要在部署文档写明「服务不暴露到内网之外」，并在 k8s 补 NetworkPolicy 或等效约束 |
| admin 闸门 | `/admin/api/*` 保留 ADMIN_TOKEN：它能删任意用户全部记忆、吊销密钥、改抽取提示词，是不可逆操作 |
| 分类准入 | 在 journal 写入、LLM 调用、embedding 之前执行；SENSITIVE/RESTRICTED 不落库、不外发（判定方见问题 6） |
| 限流 | 免鉴权后按来源 IP 限速，防失控重试与脚本打爆；不再按 key 分桶 |
| 日志 | 只留字段路径与错误类型，不留正文、metadata、密钥与后端地址（免鉴权不代表可以泄露内容） |
| 注入防护 | 记忆内容一律视为不可信数据，见 §6.6 |

## 9. 接入契约

v2 端点（破坏性变更，不兼容 v1）：

```text
POST /v2/memory/events        写事件，返回 WriteReceipt（202）
POST /v2/memory/recall        召回，返回 ContextPackage
POST /v2/memory/operations/{id}   查询操作状态
POST /v2/memory/suppress      逻辑下架
POST /v2/memory/erase         物理删除
GET  /v2/memory/records       列出记录
GET  /v2/memory/sources/{ref} 取原始证据
```

- 幂等：所有写操作要求 `Idempotency-Key`，DB 唯一键为 `(namespace, method, path, key)`。召回这类读形态的 POST 默认不进幂等缓存（见问题 7）。
- 免鉴权：业务端点不要求 Authorization 或 X-Tenant-Id（D10）。namespace 由请求体显式给出，按字符集校验。`/admin/api/*` 仍要求 ADMIN_TOKEN。
- 错误码沿用 TB-XXXX，新增“操作未完成”“需要审批”两类。
- SDK：契约稳定后由 OpenAPI 与 proto 生成，不手写。
- MCP：作为 TransportAdapter 在 P3 加入，tools 用于写搜删查，resources 用于记录与证据，状态查询回退普通 API。

## 10. 质量门禁

现有 834 个测试与 20 条 eval 全部通过，但审计的 13 项反例都不在门禁内。补入：

1. **交错门禁**：delete×在飞 append、refresh×delete、refresh×append、首次 append×首次 append，各一组确定性测试。
2. **失败注入门禁**：在 journal 写入、backend.add、索引写入、任务终态写、backend.update、backend.delete 每个点注入失败，断言不出现假 completed，重试不产生重复记录。
3. **数据隔离门禁**：免鉴权后越权不再是问题，但数据混用仍是正确性问题。每个端点 × 每个传输 × 匿名 × 跨 user/namespace 读取必须为空；任务按 user 过滤，不允许按 task_id 读到别人的任务。
4. **删除物理校验**：erase 后直接查 PG、Milvus、Mem0 history、缓存，确认原文不存在（依赖问题 4 的机制）。
5. **召回质量**：复合查询、同义表达、当前轮预算、assistant 幻觉、用户纠正五类负向用例。
6. **评测语料**：10-19 Task 1.1 的陪伴语料面向陪伴场景，**不能直接用于企业助手**。需要按目标场景重写客服/销售/内部助手语料，或明确本期只服务陪伴场景。
7. **真实引擎门禁升级为主门禁**：D9 之下，语义由 Mem0 承担，Mem0 的契约与质量就是系统质量。现存 834 个测试与 20 条 eval 全部用 `FakeMemoryBackend`，`tests/script/real_mem0_*.py` 是手工脚本；`.github/workflows/ci.yml` 只有 ruff、mypy、pytest、fuzz、pip-audit、docker build，**既没有 `make check`，也没有任何真实 Mem0/Milvus 步骤**。需要：docker-compose 起 Mem0+Milvus+LLM 的契约套件进 CI，且 `make check` 本身进 CI。

## 11. 实施顺序

| 阶段 | 范围 | 验收 |
| --- | --- | --- |
| P0 正确性 | 删除与在飞写入的删除屏障、L2 版本化写入、backend.update 失败不再谎报 completed、幂等键加 method/path、指纹 canonical 化、来源信任（assistant 不进用户事实）、分类准入 | 删除后无复活；删除后摘要不回退；失败注入门禁全绿；assistant 断言不进用户事实 |
| P0 引擎与门禁 | 升级 mem0 2.1.0 → 2.2.x 拿混合检索与 entity matching；真实 Mem0 契约套件与 `make check` 进 CI | 真实 Mem0 契约套件全绿；`make check` 全绿 |
| P1 内网化收口 | 免鉴权：删 API Key / Scope / X-Tenant-Id 校验；业务面收敛为 v2 单一契约；namespace 字符集校验；admin 保留 ADMIN_TOKEN；限流改按 IP | 免鉴权后无鉴权死代码；namespace 无碰撞；admin 缺 token 时拒绝 |
| P2 插件模型 | 类型注册表、抽取前统一准入、召回装配重写、删除固定 L3 scope 常量、suppress/erase | 新增类型只改配置与注册项；erase 全链路物理校验通过；召回负向用例全绿 |
| P3 接入生态 | SDK 生成、MCP、审计导出、文档 | SDK 覆盖 Python/TS 样例；MCP 客户端矩阵通过 |

P0 与 P1 完成前不发布新接入方。P2 完成才算“插件形态”成立。

## 12. 决策状态

### 已决（按你的指示执行）

| # | 问题 | 结论 |
| --- | --- | --- |
| 1 | L2 保留还是删 | 保留为层，重写实现（D7） |
| 2 | Milvus/Mem0 是否默认开启 | 默认开启（D6） |
| 3 | tenant 用独立列还是命名空间 | 不做安全隔离；namespace 作为业务分组标签，字符集校验防碰撞，`ins_memory.memory_scope_id` 列直接承载，不做 schema 迁移 |
| 9 | 10-19 Phase 1/2/3/4 范围 | 保留 Phase 1 评测（语料重写），Phase 2 降级为类型注册表，Phase 3 删除按 D6/D7 调整，Phase 4 不默认关 Milvus |
| 11 | 接受 mem0 2.1.0 → 2.2.x 行为变化 | 接受，threshold 与 rerank 重测 |
| 12 | 真实引擎门禁进不进 CI | 进，折中方案：nightly 真实套件，PR 跑 mock 套件加契约签名校验 |
| 13 | 鉴权 | 业务接口免鉴权，仅内网可达（D10）；`/admin/api/*` 保留 ADMIN_TOKEN |

### 仍待你给信息

| # | 问题 | 阻塞什么 | 我的建议 |
| --- | --- | --- | --- |
| 4 | Mem0 history 的物理擦除机制 | erase 的物理校验门禁 | 接受残留并明确文档化，或关闭 Mem0 history 持久化；不做脆弱的 SQL 手术 |
| 5 | gRPC TLS 在哪终止 | 传输加固 | 内网免鉴权可暂不做 TLS，但反射与无限并发要收 |
| 6 | 分类由谁判定，“不落库”是否含 L1 | 分类准入实现 | 类型注册表静态 classification 加服务端检测器组合；SENSITIVE/RESTRICTED 连 L1 都不落 |
| 7 | v2 的 gRPC 是否同步升版；召回是否进幂等缓存 | 契约形态 | gRPC 同步升 v2；召回不进幂等缓存 |
| 8 | procedure 是否进 v2 契约，审批路径谁提供 | 类型范围 | 先不暴露 procedure 类型，避免无审批路径 |
| 10 | 评测语料按什么场景重写 | 质量门禁定义 | 需要你给出目标客户场景（客服/销售/内部助手/陪伴） |

## 13. 明确不做与理由

| 不做 | 理由 |
| --- | --- |
| 进程内嵌入式 Python SDK | Mem0 适配会改 Mem0 全局状态（`src/thinkback/memory/backends/mem0_library.py:562-579,764-807`），同进程多租户与依赖冲突不可控 |
| 图存储 | 需求是事实与画像，不是多跳关系 |
| 引入 Redis/Celery | 单副本起步；需要多副本时先把状态迁到 PG |
| 替换 Mem0 | D1 |
| 治理台前端大改 | 后端契约稳定后再动 |
| 完整多语言提示词体系 | 当前默认抽取提示词自相矛盾（要求跟随对话语言同时要求纯中文），先修默认配置 |
