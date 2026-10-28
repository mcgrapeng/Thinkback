# Thinkback 记忆插件化设计（Mem0 固定为 L3 引擎）

日期：2026-10-26 · 状态：待批准 · 修订：v2

关联文档：

- `docs/plans/2026-10-19-thinkback-memory-architecture-rewrite.md`（记忆模型重构）
- `docs/plans/2026-10-21-thinkback-hardening-pass.md`（加固批次）
- `docs/report/2026-10-26-mainstream-memory-landscape.md`（主流项目对照）
- `.planning/2026-10-26-thinkback-memory-audit/findings.md`（审计证据）

修订记录：

- v3（2026-10-28）：依据真实栈实测（`docs/report/2026-10-28-real-stack-mem0-capability-probe.md`）与一次 grilling 决议修订。混合检索的描述改为实测结论，硬过滤下线改为暂缓，排序与擦除口径补齐，新增 D11–D19。
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
| D8 | legacy `/v2/memory/*` 在生产不可达 | 10-19 Task 3.1「旧路由保持可用」不采纳 |
| D9 | 引擎优先原则：引擎能做的交给引擎，引擎做不好的才补，引擎完全没有的才自己做 | 见 §2.4 能力划分、§2.5 删除清单 |
| D10 | 部署在内网，业务接口不做鉴权；信任边界是网络边界 | 删除 API Key / Scope / X-Tenant-Id 校验；`/admin/api/*` 保留 ADMIN_TOKEN 作为破坏性操作闸门；namespace 降为业务分组标签，靠字符集校验避免碰撞 |
| D11 | 擦除分四档对外声明：不可召回、逻辑删除、物理擦除、备份过期 | 当前只宣称前两档与单副本下的第三档；引擎侧清理加版本护栏，见 ADR-0001 |
| D12 | 数据分类的判定权在服务端，调用方只能更严格 | SENSITIVE/RESTRICTED 连 L1 都不写，见 ADR-0002 |
| D13 | 引擎分数只在同一次查询内可比 | 服务层可叠加显式信号重排，不重写分数量纲，见 ADR-0003 |
| D14 | 硬过滤与固定槽位引擎暂不下线 | 先上中文 BM25，之后按类型逐个下线，见 ADR-0004 |
| D15 | round_id 保持全局唯一 | 约束写进公开 schema 与 SDK，见 ADR-0005 |
| D16 | v2 契约里 gRPC 与 HTTP 同步升版 | 语义不再分叉；召回不进幂等缓存 |
| D17 | procedure 类型暂不进 v2 契约 | 没有审批主体就没有信任等级 |
| D18 | gRPC 业务面不做 TLS，反射保留，并发上限设有限值 | 信任边界是网络；反射用于内网运维 |
| D19 | 引擎投资只做中文分词器 | 不装 nlp extras；reranker 先建无 rerank 基线 |
| D20 | decay 定位为不可达化，物理删除是另一道独立作业 | ADR-0006 |
| D21 | Milvus collection 由 Thinkback 预建，mem0 接管 | 中文分词器与 BM25 Function 由我们定义；ADR-0007 |
| D22 | 一次 append 收一条事件，幂等锚点即事件 ID | role 放开到 user/assistant/tool/system；ADR-0008 |
| D23 | 分层预算 = 层保底份额 + 类型上限 | 未用完的额度可回流；调用方可传总预算覆盖默认 |
| D24 | 类型配置沿用 JSON 就地扩字段并带 version | admin 端点触发重载；策略版本写进记忆记录；ADR-0009 |
| D25 | 真实栈门禁做成一条命令，归档带 commit 号 | 签发清单的一条；不进 CI |
| D26 | 垂类词表降级为抽取提示与 metadata 标签 | 不承担检索期过滤；ADR-0004 |
| D27 | 保留期分三段：不可达、7 天宽限期、物理清理 | 审计只留计数；备份与日志归档不在作业范围；ADR-0010 |
| D28 | v2 契约直接替换现有端点 | 旧路由撤掉，不并存；ADR-0011 |
| D29 | 类型配置严格校验 | 非法配置拒绝生效，未知字段拒绝而不是忽略 |
| D30 | collection 切换期间双写，双读窗口 48 小时 | 旧集合保留 30 天；ADR-0007 |
| D31 | 真实栈门禁以现有真实基线为地板 | 七类条目；样本量扩到 40 条以上再谈提阈值 |
| D32 | 抽取指令随 profile 存放，文件名带版本 | 记忆记录存指令版本；ADR-0012 |
| D33 | 多副本开放有前置清单 | 单副本是当前发布边界，清单见 §6.3 |
| D34 | 上下文视图是分段结构化的 | 降级版按同样段形状拼接；ADR-0013 |
| D35 | 抽取指令在服务侧拼接后传给引擎 | 全局在前类型在后；ADR-0014 |

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
| 混合检索（BM25 + entity matching） | 2.2.1 有 BM25；entity matching 需 `mem0ai[nlp]` | 实测：collection 有 text+sparse+sparse_index，英文与型号词命中（MA6000 的 bm25 分 0.289），中文词命中为 0（text 字段 analyzer_params 为空，默认分析器不切中文）；entity matching 未启用（缺 spaCy，entity_boost 恒 0，无 `_entities` collection） |
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
| `src/thinkback/domain/slots/` 固定槽位引擎 | 1,270 行 + service 约 550 行引用 | 暂缓下线，先补中文 BM25，再按类型逐个下线 | Mem0 抽取 + 类型注册表；ADR-0004 |
| 词典交集硬过滤 `context_terms` | 约 100 行 | 暂缓下线 | 中文 BM25 上线并通过评测后按类型逐个关；ADR-0004 |
| 自造事实排序分（层优先级 + quality + strength） | 约 80 行 | 降为只做预算分配；允许叠加显式信号重排 | 引擎融合分负责同查询内相对序；ADR-0003 |
| `src/thinkback/memory/decay.py` 清扫器 | 155 行 | 待决 | `expiration_date` 只隐藏不删除，是否保留物理保留期清扫需单独定，见第二轮边界 |
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

输出形态是分段结构化的：一组有名字的段（画像、近期事实、未决事项、约束），可附带自由文本。降级版（LLM 不可用时）按同样的段形状拼接原文，下游不需要为“有没有分段”写两套逻辑。ADR-0013。

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

实现上沿用 JSON（`profiles/<namespace>.json`）并就地扩字段，不引 YAML 依赖：配置带 `version`，类型字段按上表补齐（`layer`、`recall`、`review`、`ttl_days`）。生效方式是启动加载加 admin 端点触发重载，重载前校验，非法配置拒绝生效而不是回落默认值。ADR-0009 要求每条记忆记录产生它的策略版本。

校验规则：必填 `name`、`extractor`、`source_roles`、`classification`、`conflict`、`retention`；`ttl_days` 只在 `retention=ttl` 时必填且大于零；`review=required` 的类型不允许 `source_roles` 含 `user`；`recall.path` 限 fact、instruction、schedule 三选一；`classification` 限四个枚举值；`extractor` 必须在已注册集合内。未知字段拒绝，与请求 schema 的 `extra="forbid"` 一致。

抽取指令放在 `profiles/<namespace>/prompts/` 下，文件名带版本，profile 引用相对路径。ADR-0012。

引擎的每次写入只能带一份指令，且是覆盖配置级默认值。所以全局默认指令与类型指令由 Thinkback 拼接后一次性传入：全局在前、类型在后，类型只能加约束不能删全局硬约束。配置级的默认指令留空。ADR-0014。

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

### 4.4 垂类记忆档案（vertical domain）

垂类对话助手（先半导体，后续可扩展到其他行业）需要领域实体与行业语义，固定的 P0 槽位（昵称/宠物/地点/生日/饮品）覆盖不了。但**领域知识不写成代码**：不在 Thinkback 里加半导体正则、本体库或词典文件，而是用配置描述一个 vertical profile。

| 配置项 | 含义 | 落点 |
| --- | --- | --- |
| `domain` | 行业标识，如 `semiconductor` | namespace 的第三段，与其它行业隔离 |
| `entity_vocabulary` | 受控实体类别与别名，如工艺节点、设备型号、客户、良率、产能、缺陷模式 | 作为类型 metadata 与检索过滤维度，不写代码 |
| `extraction_prompt` | 按类型给的抽取指令（含领域术语与输出约束） | Mem0 原生 `add(prompt=...)` 每次写入可带 |
| `retention` | 各类型的保留期，如良率趋势 90 天、客户偏好永久 | 类型注册表的 retention 策略 |
| `classification` | 行业敏感级别，如客户名、工艺参数、成本 | 分类准入（§8） |

Mem0 承接点：每次写入的 `prompt` 参数承载领域抽取指令；`metadata` 携带 domain/entity 标签。**词表不承担检索期过滤**：实测 2.2.1 的 entity matching 因缺 spaCy 未启用（`entity_boost` 恒为 0，无 `_entities` collection），BM25 只对英文与型号词命中，中文查询关键词分为零。词表先做抽取提示与 metadata 标签，是否需要检索期过滤交给评测决定。ADR-0004。

半导体示例（配置而非代码）：

```yaml
domain: semiconductor
entity_vocabulary:
  process_node: ["3nm", "5nm", "7nm", "14nm", "28nm"]
  equipment: ["光刻机", "刻蚀机", "薄膜沉积", "CMP", "量测设备"]
  metric: ["良率", "产能", "缺陷密度", "关键尺寸", "套刻精度"]
types:
  process_preference:
    layer: facts
    extractor: mem0.collection
    extraction_prompt: prompts/semiconductor/process_preference.md
    classification: confidential
    retention: forever
  yield_watch:
    layer: facts
    extractor: mem0.collection
    extraction_prompt: prompts/semiconductor/yield_watch.md
    classification: internal
    retention: ttl
    ttl_days: 90
    recall: {path: fact, budget: 400}
  customer_context:
    layer: facts
    extractor: mem0.collection
    extraction_prompt: prompts/semiconductor/customer_context.md
    classification: restricted
    review: required
```

这套结构同时适用于其它垂类（换 `domain`、词表、抽取 prompt 与保留策略即可），不需要为每个行业写一套引擎。

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
3. **排序**：引擎融合分只在同一次查询内可比（分母随本次是否命中 BM25 而变），不当阈值用也不跨查询比较。服务层可叠加有业务含义的信号重排：当前事实优先、信任等级、数据分类、类型预算；不重写引擎分数的量纲，也不自建事实排序分取代引擎。后端需透传 `explain`，否则 BM25 与 entity 的分解在服务侧不可观测。ADR-0003。
4. **分层预算**：L1/L2/L3 与各类型各有配额，避免长期事实吃光当前对话；单条超预算时截断或跳过并记原因，不返回空包。
5. **proactive 标记**：沿用 10-19 Task 2.5，返回条目加 `proactive=false` 与原因，由调用方决定是否注入。
6. **不可信数据边界**：所有层返回内容统一标记；L2 prompt 明确要求把轮次内容当数据。
7. **重排只叠加显式信号**：收到引擎 top-k 后做过滤（本地索引状态、来源、信任、分类）与预算裁剪，可按业务信号重排，但不改写引擎分数的量纲。ADR-0003。

## 7. 删除语义

| 操作 | 效果 | 适用 |
| --- | --- | --- |
| `suppress` | 不可召回，保留记录与墓碑 | 防止复活、逻辑下架 |
| `erase` | 物理删除：PG 原文、Milvus 记录、Mem0 history、缓存、审计中的原文 | 遗忘权与合规 |
| `forget_all` | 某租户或 subject 的全部记忆，按上面规则处理 | 用户注销、数据导出删除 |

`erase` 的级联清单必须逐项验证：journal messages、context view 文本、ins_memory 行、Milvus collection、Mem0 SQLite history、进程内缓存、审计日志原文。审计只留范围、数量、时间与操作人。

Mem0 的公开 CRUD 不清理 history 与 messages 表，`delete` 还会追加保留旧值的历史行；history 表结构由 Mem0 内部定义且文件按 Pod 共享。Thinkback 保留对该 SQLite 的定向删除，固定 mem0 2.2.1，表结构或列名不匹配时 fail-visible 并让操作失败，不允许静默跳过。

对外承诺分四档：不可召回、逻辑删除、物理擦除、备份过期。当前只宣称前两档与单副本下的第三档，第四档没有机制。单副本是当前发布边界；多副本开放前，引擎侧历史必须先外置。ADR-0001。

保留期到期走另一条路径：先不可达（隐藏加本地到期过滤加 decay 抑制），7 天宽限期满后由保留期清理作业物理删除治理行、向量与引擎侧历史。审计只留计数与操作元数据不删；备份与日志归档不在作业范围内，对外声明由运维策略负责。ADR-0010。

## 8. 安全

| 项 | 目标状态 |
| --- | --- |
| legacy `/v2/memory/*` | 删除或并入 v2 单一业务面；免鉴权后两套路由没有安全差别，重复面只剩维护成本 |
| namespace | 业务分组标签，靠字符集校验（禁 `:`、限长）避免与 user_id 拼接碰撞；`ins_memory.memory_scope_id` 列已存在，直接承载 namespace，不做 schema 迁移 |
| 业务鉴权 | 不做。内网信任边界 = 网络边界，需要在部署文档写明「服务不暴露到内网之外」，并在 k8s 补 NetworkPolicy 或等效约束 |
| admin 闸门 | `/admin/api/*` 保留 ADMIN_TOKEN：它能删任意用户全部记忆、吊销密钥、改抽取提示词，是不可逆操作 |
| 分类准入 | 在 journal 写入、LLM 调用、embedding 之前执行；SENSITIVE/RESTRICTED 不落库、不外发（判定方见问题 6） |
| 限流 | 免鉴权后按来源 IP 限速，防失控重试与脚本打爆；不再按 key 分桶 |
| 日志 | 只留字段路径与错误类型，不留正文、metadata、密钥与后端地址（免鉴权不代表可以泄露内容） |
| 注入防护 | 记忆内容一律视为不可信数据，见 §6.6 |

### 8.1 多副本开放的前置清单

单副本是当前的发布边界，不禁止使用者按自己的部署需要开多副本，但要先做完这五件事：

| 前置 | 现在的问题 |
| --- | --- |
| 幂等与限流入 PG | 幂等缓存与限流桶都在进程内，多副本各算各的 |
| L1 缓存跨副本失效或改读穿透 | 删除只失效本副本缓存，其他副本会继续返回已删轮次 |
| 引擎侧历史外置 | `history.db` 是 pod 本地文件，一次擦除只清当前副本 |
| 后台任务改 DB 队列抢占 | 现在是进程内 executor 加启动回收，跨副本会重复执行 |
| 删除屏障跨副本 | 删除屏障靠进程内锁，跨副本的在飞写入挡不住 |

在这五件事落地之前，多副本不能作为 HA 能力对外承诺。

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

- 幂等：写入的锚点是请求体里的事件 ID（全局唯一，D15/D22）；范围类写操作（suppress/erase）用 `Idempotency-Key`，DB 唯一键为 `(namespace, method, path, key)`。召回这类读形态的 POST 不进幂等缓存。
- 免鉴权：业务端点不要求 Authorization 或 X-Tenant-Id（D10）。namespace 由请求体显式给出，按字符集校验。`/admin/api/*` 仍要求 ADMIN_TOKEN。
- 错误码沿用 TB-XXXX，新增“操作未完成”“需要审批”两类。
- SDK：契约稳定后由 OpenAPI 与 proto 生成，不手写。
- MCP：作为 TransportAdapter 在 P3 加入，tools 用于写搜删查，resources 用于记录与证据，状态查询回退普通 API。
- 迁移：v2 端点直接替换现有 `/v2/memory/*`，旧路由撤掉，不留兼容版本期（服务从未上线，无真实接入方）。ADR-0011。

### 9.1 契约字段形态

```text
SourceEvent
  event_id       全局唯一，幂等锚点（ADR-0005 / ADR-0008）
  source         事件来源标识，如 chat_round / tool_result / import
  role           user | assistant | tool | system
  subject_ref    事件指向的主体，默认即 user_id
  occurred_at    事件在源头发生的时间
  received_at    服务收到的时间（可由服务端填）
  payload        事件正文
  provenance     来源链路信息（上游系统、工具名、原始引用）
  trust          信任等级，服务端推导，不接受调用方自报

WriteReceipt
  operation_id   操作标识
  status         accepted | completed | failed
  layers         journal / context_view / facts 各自的状态与版本

ContextPackage
  items          分层内容，每条带来源引用、类型、信任等级、是否主动召回
  degraded       是否降级
  reasons        降级原因
  token_estimate 估算占用
  budget         预算使用情况
```

`role=assistant` 的内容不作为用户事实来源，只留在 L1；`role=tool` 是否可作为事实来源由类型的 `source_roles` 决定。信任等级由服务端按类型策略推导，调用方传什么都不影响判定。

## 10. 质量门禁

现有 834 个测试与 20 条 eval 全部通过，但审计的 13 项反例都不在门禁内。补入：

1. **交错门禁**：delete×在飞 append、refresh×delete、refresh×append、首次 append×首次 append，各一组确定性测试。
2. **失败注入门禁**：在 journal 写入、backend.add、索引写入、任务终态写、backend.update、backend.delete 每个点注入失败，断言不出现假 completed，重试不产生重复记录。
3. **数据隔离门禁**：免鉴权后越权不再是问题，但数据混用仍是正确性问题。每个端点 × 每个传输 × 匿名 × 跨 user/namespace 读取必须为空；任务按 user 过滤，不允许按 task_id 读到别人的任务。
4. **删除物理校验**：erase 后直接查 PG、Milvus、Mem0 history、缓存，确认原文不存在（依赖问题 4 的机制）。
5. **召回质量**：复合查询、同义表达、当前轮预算、assistant 幻觉、用户纠正五类负向用例。
6. **评测语料按垂类对话助手编写，先半导体，可扩展到其它行业**：覆盖工艺咨询、设备选型、良率分析、客户支持四类对话；每条标注期望召回 / 不该召回 / 不该主动提 / 保留期。10-19 Task 1.1 的陪伴语料不作为本期主门禁，仅作回归 fixture。
7. **真实引擎门禁是本地门禁，不要求进 CI**：D9 之下，语义由 Mem0 承担，Mem0 的契约与质量就是系统质量。现存 834 个测试与 20 条 eval 全部用 `FakeMemoryBackend`，`tests/script/real_mem0_*.py` 是手工脚本。真实 Mem0/Milvus 契约套件保留为**本地必跑**的门禁（发布前在开发机执行并记录结果），CI 维持现状（ruff、mypy、pytest、fuzz、pip-audit、docker build），不引入服务化依赖。

   门禁做成一条 `make gate-real`，覆盖七类条目：抽取非空率、期望事实覆盖率、期望事实召回率、禁存词不泄漏、erase 后各资源不可见、幂等重放一致、BM25 中英文命中与 filter 操作符正负向。阈值以现有真实基线为地板（总通过率不低于 0.6875，分型下限不低于工艺咨询 0.66、设备选型 1.0、良率分析 0.33、客户支持 0.33、垂类机制 1.0），只许升不许降。中文 BM25 条目在 jieba 上线前标为待启用，上线后转为阻断。真实语料当前 16 条，扩到 40 条以上再谈提高阈值。

### 10.1 发布签发清单

| 条目 | 内容 | 签发人 |
| --- | --- | --- |
| 确定性门禁 | format、lint、typecheck、单测、变异审计、评测、前端门禁全绿 | 工程负责人 |
| 真实栈门禁 | 同一发布 SHA 上 `make gate-real` 全绿，结果带 commit 号归档 | 工程负责人 |
| 协议契约矩阵 | HTTP 与 gRPC 各走一遍五操作；自定义类型与 metadata 不丢失 | 工程负责人 |
| 镜像与部署 | profile 装载正确、kustomize 渲染、迁移可升可降 | 工程负责人 |
| 负载与故障 | p95/p99、任务完成时延、恢复时间 | 工程负责人 |
| 质量与承诺 | 召回质量达标、擦除四档口径成立 | 业务方 |

业务方签最后一条：召回质量与擦除口径是产品承诺，不是工程判断。

## 11. 实施顺序

| 阶段 | 范围 | 验收 |
| --- | --- | --- |
| P0 正确性 | 删除与在飞写入的删除屏障、L2 版本化写入、backend.update 失败不再谎报 completed、幂等键加 method/path、指纹 canonical 化、来源信任（assistant 不进用户事实）、分类准入 | 删除后无复活；删除后摘要不回退；失败注入门禁全绿；assistant 断言不进用户事实 |
| P0 引擎与门禁 | 升级 mem0 2.1.0 → 2.2.x 拿混合检索与 entity matching；真实 Mem0 契约套件作为本地必跑门禁（不进 CI） | 真实 Mem0 契约套件本地全绿；`make check` 本地全绿 |
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
| 12 | 真实引擎门禁进不进 CI | 不进 CI。真实 Mem0/Milvus 契约套件作为本地必跑门禁，CI 维持现状（ruff、mypy、pytest、fuzz、pip-audit、docker build） |
| 13 | 鉴权 | 业务接口免鉴权，仅内网可达（D10）；`/admin/api/*` 保留 ADMIN_TOKEN |
| 14 | `make check` 是否进 CI | 不进。本地跑完 format-check、lint、typecheck、mutation-audit、eval 即可；CI 保持现状 |
| 15 | 评测语料场景 | 已决：垂类对话助手，先半导体（工艺咨询、设备选型、良率分析、客户支持），结构上可扩展到其它行业 |

### 已决（2026-10-28 grilling）

| # | 问题 | 结论 | 对应 |
| --- | --- | --- | --- |
| 4 | Mem0 history 的物理擦除机制 | 保留对 Mem0 SQLite 的定向删除，固定 mem0 2.2.1 并加表结构护栏；对外按四档声明，只宣称前两档与单副本下的第三档 | D11 / ADR-0001 |
| 5 | gRPC TLS 在哪终止 | 业务面不做 TLS，信任边界是网络；反射保留；并发上限从无限改为有限值 | D18 |
| 6 | 分类由谁判定，“不落库”是否含 L1 | 判定权在服务端，调用方只能更严格；SENSITIVE/RESTRICTED 连 L1 都不写 | D12 / ADR-0002 |
| 7 | v2 的 gRPC 是否同步升版；召回是否进幂等缓存 | gRPC 与 HTTP 同步升 v2，语义不分叉；召回不进幂等缓存，超时可安全重试 | D16 |
| 8 | procedure 是否进 v2 契约，审批路径谁提供 | 暂不暴露 procedure 类型，模型里保留 `review_state` | D17 |
| 16 | 引擎结果能不能被服务层重排 | 能，但只叠加显式信号，不重写引擎分数量纲 | D13 / ADR-0003 |
| 17 | 硬过滤与固定槽位引擎何时下线 | 暂缓；先上中文 BM25，再按类型逐个下线 | D14 / ADR-0004 |
| 18 | 幂等锚点是全局 round_id 还是复合键 | 保持全局唯一，约束写进 schema 与 SDK | D15 / ADR-0005 |
| 19 | 要不要为引擎能力投资 | 只做中文分词器；不装 nlp extras；reranker 先建无 rerank 基线 | D19 |

### 已决（2026-10-28 第二轮 grilling）

| # | 问题 | 结论 | 对应 |
| --- | --- | --- | --- |
| 20 | `decay.py` 物理保留期清扫删还是留 | decay 定位为不可达化，物理删除是另一道独立作业 | D20 / ADR-0006 |
| 21 | collection 重建的回放与回滚方案 | Thinkback 预建 collection 并接管 schema；存量行复制式回填，不重新抽取；切换靠配置改名，旧集合留作回滚，双读兜底 | D21 / ADR-0007 |
| 22 | 分层预算怎么切 | 层保底份额加类型上限，未用额度可回流；调用方可传总预算覆盖默认 | D23 |
| 23 | v2 契约的字段级形态 | 采用 SourceEvent 规范模型并放开 role；一次 append 一条事件，锚点即事件 ID；WriteReceipt 与 ContextPackage 形状见 §9.1 | D22 / ADR-0008 |
| 24 | 类型注册表配置格式与生效方式 | 沿用 JSON 扩字段加 version；admin 端点触发重载，非法配置拒绝生效 | D24 / ADR-0009 |
| 25 | 真实栈门禁清单与归档位置 | 一条命令跑完整清单，结果带 commit 号归档，不进 CI | D25 |

### 已决（2026-10-28 第三轮 grilling）

| # | 问题 | 结论 | 对应 |
| --- | --- | --- | --- |
| 26 | 保留策略与物理删除作业的形状 | 三段：不可达、7 天宽限期、物理清理；审计只留计数；备份不在作业范围 | D27 / ADR-0010 |
| 27 | v2 契约的迁移方式 | 直接替换现有端点，旧路由撤掉不并存 | D28 / ADR-0011 |
| 28 | 类型注册表的字段清单与校验规则 | 严格校验 + 未知字段拒绝，非法配置拒绝生效 | D29 |
| 29 | collection 重建的回滚细节 | 切换期间双写，双读 48 小时，旧集合留 30 天 | D30 / ADR-0007 |
| 30 | 真实栈门禁的条目与阈值 | 七类条目，阈值以现有真实基线为地板只升不降 | D31 |
| 31 | 发布签发清单的最终形态 | 六条清单，质量与承诺由业务方签 | 见 §10.1 |
| 32 | 抽取指令的存放与版本化 | 随 profile 存放，文件名带版本，记忆记录存版本号 | D32 / ADR-0012 |

### 已决（2026-10-28 第四轮 grilling）

| # | 问题 | 结论 | 对应 |
| --- | --- | --- | --- |
| 33 | 多副本开放的门槛 | 五项前置清单，清单未做完不承诺 HA | D33 / §8.1 |
| 34 | 上下文视图的输出形态 | 分段结构化，降级版同形状 | D34 / ADR-0013 |
| 35 | 默认指令与类型指令的合并规则 | 服务侧拼接，全局在前类型在后 | D35 / ADR-0014 |

设计树的边界已空。剩下的都是实现细节：字段怎么落、脚本怎么写、票怎么拆。

## 13. 明确不做与理由

| 不做 | 理由 |
| --- | --- |
| 进程内嵌入式 Python SDK | Mem0 适配会改 Mem0 全局状态（`src/thinkback/memory/backends/mem0_library.py:562-579,764-807`），同进程多租户与依赖冲突不可控 |
| 图存储 | 需求是事实与画像，不是多跳关系 |
| 引入 Redis/Celery | 单副本起步；需要多副本时先把状态迁到 PG |
| 替换 Mem0 | D1 |
| 治理台前端大改 | 后端契约稳定后再动 |
| 完整多语言提示词体系 | 默认抽取指令已修（`a314900`：改为跟随对话语言、实体逐字保留），但指令仍偏长且有重复条目，v2 上线前做一次瘦身 |
