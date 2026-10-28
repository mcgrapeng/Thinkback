# Thinkback 与主流记忆系统对照复审

日期：2026-10-28

## 0. 范围、结论状态与版本基线

本文复核 `2026-10-26` 报告和插件设计中的上游主张，目标不是重新选引擎。Thinkback 固定使用 Mem0 OSS，其他项目只用于校准服务层职责。

证据分为三类：

- **源码证实**：固定 release/tag 或 commit 的实现可直接支持该说法。
- **文档证实**：官方在线文档支持该说法，但本次没有运行验证。
- **未知**：文档矛盾、实现依部署而变，或本次没有做真实引擎实验。

除版本发布时间外，本文所有在线资料访问日期均为 **2026-10-28**。没有使用营销二手文章，也没有把各项目的官方 benchmark 当作 Thinkback 的实测结果。

| 对象 | 本次基线 | 一手版本来源 |
| --- | --- | --- |
| Mem0 OSS | Python `mem0ai 2.2.1`，2026-09-25，commit [`94c3fe9`](https://github.com/mem0ai/mem0/commit/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd)；TypeScript `3.3.1` | [Python release](https://github.com/mem0ai/mem0/releases/tag/v2.2.1)、[PyPI JSON](https://pypi.org/pypi/mem0ai/json)、[npm latest](https://registry.npmjs.org/mem0ai/latest) |
| Mem0 Platform | 当前 v3 在线文档，与 OSS 分开记录 | [Platform vs OSS](https://docs.mem0.ai/platform/platform-vs-oss) |
| Graphiti OSS | `0.31.2`，2026-10-20，commit [`d72407d`](https://github.com/getzep/graphiti/commit/d72407dcedac911e1be9fc15b55e8c05a3efcc36) | [release](https://github.com/getzep/graphiti/releases/tag/v0.31.2)、[PyPI JSON](https://pypi.org/pypi/graphiti-core/json) |
| Zep Cloud | 当前 v4 在线文档；托管能力不能归到 Graphiti OSS | [Zep vs Graphiti](https://help.getzep.com/v4/zep-vs-graphiti) |
| LangMem | 包版本 `0.0.30`，PyPI 上传于 2025-10-27；主分支 commit [`48e3c11`](https://github.com/langchain-ai/langmem/commit/48e3c11f5bb527282c7d5339c6a87a0b35abccfc) 仍是 0.0.30，无 GitHub release | [固定 pyproject](https://github.com/langchain-ai/langmem/blob/48e3c11f5bb527282c7d5339c6a87a0b35abccfc/pyproject.toml)、[PyPI JSON](https://pypi.org/pypi/langmem/json) |
| Cognee | 最新稳定 PyPI 版 `1.6.5`，2026-10-16，commit [`37f788b`](https://github.com/topoteretes/cognee/commit/37f788bd287750e10d7610173e5332b3843f6ebd)；另有 `1.6.6rc1`，2026-10-23，commit [`277a314`](https://github.com/topoteretes/cognee/commit/277a314f57509257ae4f67662c46c66730889ed1) | [1.6.5 release](https://github.com/topoteretes/cognee/releases/tag/v1.6.5)、[1.6.6rc1 release](https://github.com/topoteretes/cognee/releases/tag/v1.6.6rc1)、[PyPI JSON](https://pypi.org/pypi/cognee/json) |
| Letta | 当前 Letta Harness / Letta Code `0.34.26`，commit [`03f96f8`](https://github.com/letta-ai/letta-code/commit/03f96f8af697e21d4312b33a50f6a765e6a13266)；旧 `letta-ai/letta` server 最新 release 仍是 `0.16.8`，不是当前 Harness 的版本基线 | [npm latest](https://registry.npmjs.org/@letta-ai/letta-code/latest)、[当前文档索引](https://docs.letta.com/llms.txt)、[旧 server release](https://github.com/letta-ai/letta/releases/tag/0.16.8) |
| MCP | 稳定规范 `2026-07-28`，commit [`5f5440b`](https://github.com/modelcontextprotocol/modelcontextprotocol/commit/5f5440bb26a62e2cf3440b92da5a667efa03b267) | [规范 release](https://github.com/modelcontextprotocol/modelcontextprotocol/releases/tag/2026-07-28)、[versioning](https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning) |

Cognee 在线文档没有逐稳定版归档，部分内容已经描述 `1.6.6rc1` 的行为。本文引用时会标明“当前在线文档”，不把这些行为全部倒算到 1.6.5。

## 1. 本次刷新改变或加强的判断

1. **Mem0 最新 Python release 仍是 2.2.1，但“升到 2.2.x 就有混合检索”不成立。** Milvus 老 collection 缺少 `text` 和 `sparse` 字段时会退回语义检索，官方实现要求新 collection；spaCy、实体 collection 和存储支持也都有前置条件。[Milvus 2.2.1 源码，80-90、237-270 行](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/vector_stores/milvus.py#L80-L90)
2. **ADD-only 不等于自动解决冲突。** OSS 自动写入只返回 ADD，精确文本 hash 去重；显式 `update()` 仍存在。当前融合排序没有 recency 或 supersede 信号，不能据此保证新事实排在旧事实前。[add 源码](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/memory/main.py#L918-L955)、[hash 去重源码](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/memory/main.py#L1007-L1045)、[scoring 源码](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/utils/scoring.py)
3. **Thinkback 的行业 `entity_vocabulary` 不会自动成为 Mem0 检索词表。** OSS 实体抽取固定加载 `en_core_web_sm`，规则写在源码中，和 `add(prompt=...)` 的事实抽取 prompt 是两条路径。中文和半导体术语效果未知，必须实测。[spaCy loader](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/utils/spacy_models.py)、[entity extraction](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/utils/entity_extraction.py)
4. **删除范围比 10-26 设计列出的更大。** Mem0 `add()` 除向量和 history 外，还把原始 messages 写入自己的 SQLite，保留每个 session scope 最近 10 条供后续抽取使用。`delete()` 不会清掉这些 messages，也不会清掉 history 旧值。[messages 写入](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/memory/main.py#L988-L991)、[SQLite messages 表与清理](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/memory/storage.py#L257-L295)、[delete 实现](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/memory/main.py#L2114-L2140)
5. **Mem0 `AsyncMemory` 是进程内 async 接口，不是持久任务系统。** 2.2.1 的 async `delete_all()` 还会逐条收集错误、只记日志，最后仍返回成功消息，Thinkback 必须对账，不能把响应直接映射成 `completed`。[async delete_all](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/memory/main.py#L3583-L3654)
6. **Zep v4 已把托管服务与 Graphiti OSS 的边界写得更清楚。** episode、双时态事实、Context Block、任务、ABAC、审计和保留策略可作为服务层参照；其中大部分治理功能不属于 Graphiti OSS。[官方对照](https://help.getzep.com/v4/zep-vs-graphiti)
7. **Letta 的当前主线是 Git 版本化 MemFS 和 Agent SDK。** 10-26 报告仅以旧 server 0.16.8 为版本基线已经不够；当前 MemFS 默认没有向量索引，搜索靠文件工具或可选 mod。[MemFS](https://docs.letta.com/concepts/memfs/index.md)
8. **Cognee 的“版本化”不是完整历史或 as-of。** 当前文档明确说旧属性值、旧文档内容和 point-in-time query 都不保存；可选 provenance ledger 也有覆盖范围和保留限制。[Memory Versioning and History](https://docs.cognee.ai/core-concepts/further-concepts/memory-versioning)

## 2. Mem0 OSS 2.2.1 能力核查

### 2.1 写入、读取、修改、删除与历史

| 项目 | 证实行为 | 对 Thinkback 的边界 | 来源（2.2.1，访问于 2026-10-28） |
| --- | --- | --- | --- |
| `add()` | `infer=True` 走单遍 ADD-only 抽取；已有记忆只作为抽取上下文。精确文本 MD5 在候选和本批次内去重。`infer=False` 逐条原文写入并跳过 system 消息，重复内容可直接落库。 | 可委托事实抽取与基本去重。来源角色准入、幂等键、语义冲突、纠正流程和跨批次精确去重仍需服务层处理。 | [add/additive pipeline](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/memory/main.py#L760-L879)、[raw add](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/memory/main.py#L881-L916)、[ADD-only migration](https://docs.mem0.ai/migration/oss-v2-to-v3) |
| 部分写入 | 批量 insert 失败会逐条重试，只有实际插入的记录返回 ADD；全部失败才抛 `VectorStoreError`。history 写入失败会降级并记录日志。 | `add()` 返回成功不代表整个输入或所有派生产物原子完成。Thinkback 要按 memory id 对账，并区分事实、entity link、history、原始 messages 的状态。 | [批量持久化与降级](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/memory/main.py#L1047-L1098)、[2.2.1 release note](https://github.com/mem0ai/mem0/releases/tag/v2.2.1) |
| `search()` / `get_all()` | 两者都要求 `filters` 至少含 `user_id`、`agent_id`、`run_id` 之一；顶层实体参数会报错。`top_k=20`，`threshold=0.1`，`rerank=False`。 | 服务层可以统一封装新签名，不再为当前版保留旧式顶层 user/agent/run 分支。阈值必须用真实数据重测。 | [search 签名与校验](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/memory/main.py#L1393-L1519)、[get_all](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/memory/main.py#L1269-L1338)、[migration](https://docs.mem0.ai/migration/oss-v2-to-v3) |
| `update()` | 按 id 原地更新文本和/或 metadata，重新 embedding 并追加 UPDATE history；实体 ID 和 actor 在创建后不可通过 metadata 改。可设置或清除 `expiration_date`。 | 这是纠正和人工编辑的底层能力，不等于 `add()` 会自动调用它。服务层需要自己的 conflict/version/source refs，不能只靠更新后的当前文本重建历史。 | [update](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/memory/main.py#L1829-L1881)、[内部更新与 history](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/memory/main.py#L2052-L2112) |
| `delete()` | 先删向量记录，再向 history 追加 DELETE 行，并尽力清 entity link。不会删除 history 旧值，也没有清理已保存 messages 的步骤。 | 可用于“当前索引不可召回”，不能直接宣称物理擦除完成。 | [delete 与内部实现](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/memory/main.py#L1883-L1902)、[内部删除](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/memory/main.py#L2114-L2140) |
| `delete_all()` | 至少要求一个 user/agent/run，分批删除。sync 和 async 都没有返回逐项删除清单；async 版本对部分错误只记日志后返回成功。`reset()` 是全局重置，不是按 subject 擦除。 | Thinkback 应保存目标 id 集合并复查存储；按用户擦除不能使用全局 `reset()`。删除操作要有自己的终态和重试。 | [sync delete_all/reset](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/memory/main.py#L1904-L1958)、[async delete_all](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/memory/main.py#L3583-L3654)、[reset](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/memory/main.py#L2144-L2165) |
| `history()` | 返回指定 memory id 的 ADD/UPDATE/DELETE 历史。删除时旧值仍写进 history；add 失败时的 history 写入也可能是 best-effort。 | 可做调试线索，不是完整 canonical event log，也不是合规擦除后的空存储证明。 | [history API](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/memory/main.py#L1960-L1973)、[history/messages schema](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/memory/storage.py) |
| 隐式原文副本 | 每次推断式 add 都调用 `save_messages()`，SQLite 每 scope 只保留最近 10 条，用于下一次抽取的上下文。 | “L1 是唯一原始证据”需要改成“Thinkback 的权威原文账本”，并把所有 Mem0 内部副本纳入派生副本清单和 erase 验证。 | [调用点](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/memory/main.py#L1206-L1207)、[save_messages](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/memory/storage.py#L257-L295) |

### 2.2 检索、过滤、TTL、类型与作用域

| 项目 | 证实行为 | 服务层应补什么 | 来源（2.2.1，访问于 2026-10-28） |
| --- | --- | --- | --- |
| 混合检索 | 候选集只来自 semantic results；BM25 和 entity 只给这些候选加分，不扩大召回。threshold 先过滤 semantic score，再计算融合分。 | 不能再加词典交集硬过滤，但也不能把 BM25 当召回扩展。领域专名若 semantic 候选没召回，仍会丢失。 | [检索流程](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/memory/main.py#L1642-L1701)、[scoring](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/utils/scoring.py)、[migration](https://docs.mem0.ai/migration/oss-v2-to-v3) |
| Milvus BM25 | 新 collection 使用原生 BM25 sparse 字段；旧 collection 只告警并返回 `None`，官方注释要求用 fresh collection 开启。keyword 查询异常也退回 `None`。 | 升级计划必须包含 collection 迁移/重建、真实检索验证和回退监控，不能只改依赖版本。 | [Milvus schema 与 keyword_search](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/vector_stores/milvus.py#L80-L124) |
| Entity matching | 需要 `[nlp]` 和模型，另建 `{collection}_entities`。抽取固定 `en_core_web_sm`，代码涵盖 NER、proper noun、quoted text、noun compound 和 identifier。失败时信号降级。 | 行业词表、中文 NER、别名归一和实体权限属于应用问题。Thinkback 可用 metadata 做确定过滤，但需实测 Milvus 操作符，不要宣称配置词表已驱动 Mem0 实体抽取。 | [entity extraction](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/utils/entity_extraction.py)、[model loader](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/utils/spacy_models.py)、[entity store migration](https://docs.mem0.ai/migration/oss-v2-to-v3) |
| `rerank` | 默认关闭，需配置 reranker 且每次 search 开启；在混合检索之后执行。失败时返回原排序并告警。支持 hosted、本地 cross-encoder 和 LLM reranker。 | 先测融合检索，再按 query 集决定是否开启。Thinkback 无需另造同用途 reranker，但要保留时延、成本与失败回退指标。 | [search rerank 调用](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/memory/main.py#L1513-L1519)、[OSS reranker 文档](https://docs.mem0.ai/open-source/features/reranker-search) |
| `filters` | OSS 支持任意 metadata key，以及 `eq/ne/gt/gte/lt/lte/in/nin/contains/icontains` 和 AND/OR/NOT；实际支持依 vector store。官方特别警告不支持的操作符常被静默忽略。`get_all()` 的逻辑树兼容性也不一致。 | Thinkback 应只暴露经 Milvus 契约测试覆盖的最小子集。权限和删除不能依赖一个未验证的 metadata filter。 | [search filter grammar](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/memory/main.py#L1416-L1431)、[OSS metadata filtering](https://docs.mem0.ai/open-source/features/metadata-filtering) |
| TTL / expiration | `expiration_date=YYYY-MM-DD` 按 UTC 过期，只在 search/get_all 隐藏；get by id 仍返回，`show_expired=True` 可恢复显示。它不做删除。 | 可把“停止召回”交给 Mem0，把保留期物理清理、跨层删除、备份过期和审计留给 Thinkback。`decay.py` 若承担物理保留任务，不能仅因 expiration 存在就删除。 | [Memory Expiration](https://docs.mem0.ai/platform/features/memory-expiration)、[过期判断](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/memory/main.py#L427-L449) |
| `procedural_memory` | 只有 `memory_type="procedural_memory"` 且提供 `agent_id` 才进入专门生成路径；其他 memory_type 抛 validation error。源码 TODO 明说未来 breaking release 要移除该支持。 | 不宜把长期类型系统建立在这个特殊分支上。procedure 需要独立 schema、版本、证据、审批和召回权限；当前暂缓该类型是合理选择。 | [类型校验与 procedural 分支](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/memory/main.py#L831-L856)、[生成实现](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/memory/main.py#L2007-L2048) |
| user / agent / run | OSS 只有这三种实体维度，没有 app/org/project/tenant。`add()` 和 `delete_all()` 用顶层参数，search/get_all 用 filters。OSS 源码会把调用时提供的多个 ID 都写入 metadata。 | tenant、subject、application、assistant、dataset、session 的语义映射和读写策略由 Thinkback 定义。run_id 只是调用方给的隔离键，不会自动形成完整会话模型。 | [scope 构造](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/memory/main.py#L314-L404)、[Platform vs OSS](https://docs.mem0.ai/platform/platform-vs-oss) |
| `AsyncMemory` | 提供 add/search/get/get_all/update/delete/delete_all/history 的 await 形式，很多存储和模型调用通过 `asyncio.to_thread` 执行。没有持久队列、lease、崩溃恢复或任务查询。 | FastAPI 可用它避免阻塞 event loop；可靠执行仍由 Thinkback 的 PostgreSQL operation/outbox/worker 负责。 | [AsyncMemory 文档](https://docs.mem0.ai/open-source/features/async-memory)、[async search/rerank](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/memory/main.py#L3074-L3224) |
| 自定义 prompt | 配置项是 `custom_instructions`，每次 `add(prompt=...)` 可覆盖；prompt 进入事实抽取 user prompt。 | 适合按记忆类型或垂直领域指导抽取。prompt 版本、输出验证、来源准入和评测仍由 Thinkback 管理。 | [add prompt 选择](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/memory/main.py#L942-L955)、[OSS custom instructions](https://docs.mem0.ai/open-source/features/custom-instructions) |

**文档矛盾：** OSS custom-instructions 页面仍写 `facts` 数组，2.2.1 实现实际读取 `memory` 数组，并逐项读取 `text`。因此自定义 prompt 不应被当作任意结构化 schema API，Thinkback 要以固定版本源码和契约测试为准。[文档](https://docs.mem0.ai/open-source/features/custom-instructions)、[实际解析](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/memory/main.py#L973-L995)

### 2.3 Platform 与 OSS 不能混用的结论

| 能力 | 当前官方划分 | Thinkback 固定 OSS 后的处理 | 来源（访问于 2026-10-28） |
| --- | --- | --- | --- |
| Graph | OSS 已移除外置 graph store 和 `relations`；Platform 原生 graph 始终开启，graph 信号参与排序。 | 不把 entity matching 叫知识图谱，也不期待多跳关系。 | [migration](https://docs.mem0.ai/migration/oss-v2-to-v3)、[Platform vs OSS](https://docs.mem0.ai/platform/platform-vs-oss) |
| 冲突整理 | Platform Dream 有 Supersede、Merge，Synthesis 可选；Superseded 默认仍返回，`latest_only=true` 才排除。OSS 没有 Dream。 | OSS 的旧新事实治理要由 Thinkback 做。即使使用 Platform，也要显式决定历史与当前事实读法。 | [Dream](https://docs.mem0.ai/platform/features/dream)、[Platform vs OSS](https://docs.mem0.ai/platform/platform-vs-oss) |
| 时间与衰减 | Temporal Reasoning 和 Memory Decay 为 Platform 功能；OSS 的 timestamp/reference_date 直接报错。 | 自有 valid/recorded time、as-of、迟到事件和当前状态过滤；expiration 只负责隐藏。 | [Temporal Reasoning](https://docs.mem0.ai/platform/features/temporal-reasoning)、[OSS timestamp/reference_date 校验](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/memory/main.py#L817-L818) |
| 租户与组织 | Platform 另有 app_id、org/project 和成员角色；OSS 只有 user/agent/run。 | 自有 scope 模型。免鉴权内网部署也仍需防止业务数据混用。 | [Entity-Scoped Memory](https://docs.mem0.ai/platform/features/entity-scoped-memory)、[Platform vs OSS](https://docs.mem0.ai/platform/platform-vs-oss) |
| 事件与审计 | Platform 有项目级 event feed；OSS 只有单条 memory history。 | L1 canonical events、操作审计和检索证据快照由 Thinkback 保存。 | [Platform vs OSS](https://docs.mem0.ai/platform/platform-vs-oss) |
| Webhook | Platform 有项目级 add/update/delete/categorize 和 ingest job callbacks；OSS 没有。 | Thinkback 从自己的持久 outbox 发事件，不能依赖 Mem0 OSS 回调。 | [Webhooks](https://docs.mem0.ai/platform/features/webhooks)、[Platform vs OSS](https://docs.mem0.ai/platform/platform-vs-oss) |
| SDK / add 完成语义 | Python/TS 同时提供 Platform client 与 OSS 类，但 import 和运行边界不同。Platform add 当前默认异步，返回 PENDING/event id；OSS AsyncMemory 仍是本地调用并返回结果。 | Thinkback 的外部 operation 状态不能照抄任一 SDK 的返回消息，应统一映射到自己的分层状态机。 | [Add Memory](https://docs.mem0.ai/core-concepts/memory-operations/add)、[AsyncMemory](https://docs.mem0.ai/open-source/features/async-memory) |
| 其他管理面 | 自定义 categories、memory export、batch update/delete、feedback、project summary 等也属于 Platform 差异项。 | 类型注册表、导出、批量擦除、反馈和摘要各自评估，不假定 OSS 已有。 | [Platform vs OSS](https://docs.mem0.ai/platform/platform-vs-oss) |

Platform 文档关于 webhook 的页面没有给出足够的签名、重试、幂等和投递保证细节，这些属于**未知**。若 Thinkback 自己提供 webhook，应单独定义并测试这些契约。

另一个容易误判的差异是：Platform 默认抽取路径会把 user/agent 按说话者归属拆分，而 OSS 2.2.1 源码会保留调用时传入的多个实体 ID。跨产品复用 AND/OR filter 前必须重新验证。[Platform scope 文档](https://docs.mem0.ai/platform/features/entity-scoped-memory)、[OSS scope 源码](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/memory/main.py#L376-L389)

## 3. Thinkback 服务层的职责划分

### 3.1 可以直接交给 Mem0 的部分

- 对话到事实文本的 LLM 抽取，按类型传 `prompt`，保存 prompt/extractor 版本。
- 基本向量索引、语义候选、可用的 BM25/entity 加分，以及可选 reranker。
- 单条事实的 get/update/delete，用户/助手/run 三维过滤，expiration 的停止召回。
- `AsyncMemory` 的进程内非阻塞调用。

这些委托都以真实 Mem0 + Milvus 契约套件通过为前提。Fake backend 无法证明 BM25 是否启用、entity collection 是否建立、filter 是否被静默丢弃或删除是否完整。

### 3.2 Mem0 有入口但做不好，Thinkback 必须补强的部分

| 能力 | 原因 | Thinkback 最小职责 |
| --- | --- | --- |
| 当前事实与冲突 | ADD-only 只精确去重，OSS 排序没有 supersede/recency 信号。 | 保存 stable record id、supersedes/source refs、recorded_at；纠正时显式 update/delete/suppress，并决定当前事实读法。 |
| 领域抽取与检索 | prompt 可指导事实抽取，但不能配置检索侧实体抽取；英文 spaCy 和 semantic-only 候选限制会影响中文术语。 | 维护领域 prompt 与确定性的 metadata 标签；建立中文半导体召回评测，必要时补领域召回，不把配置词表当成已生效的 NER。 |
| 写入完成 | 向量、history、entity、messages 不是一个事务；async delete_all 可部分失败后返回成功。 | 按 operation 和 memory id 记录分层状态、失败原因、重试和对账结果，不能只看成功 message。 |
| 删除与保留 | expiration 只隐藏；delete 留 history/messages，entity 清理也可 best-effort。 | suppress、不可召回、物理 erase、备份/日志/缓存过期分开；保留期清扫和对账仍需存在。 |
| 历史与版本 | history 只有单条变更文本，且可能残留；没有完整事件溯源和 as-of。 | 自有 canonical SourceEvent、版本链、recorded/valid time、提取器版本和删除证据。 |
| 类型化记忆 | procedural 是特殊且计划移除的分支，其他业务类型没有 schema 注册。 | 自有类型注册表、准入、schema、冲突策略、读路径和审批；Mem0 只承载部分 extractor。 |

### 3.3 Mem0 没有，Thinkback 应完整拥有的部分

1. **权威事件与分层模型**：L1 SourceEvent journal、L2 有界会话视图、L3 派生事实的 source_refs。Mem0 SQLite messages 是需要治理的内部副本，不是 Thinkback 的权威 L1。
2. **业务身份与策略**：tenant/subject/application/assistant/dataset/session 的映射、共享与私有 namespace、角色来源信任、分类和审批。内网免鉴权只改变认证边界，不会消除这些不变量。
3. **持久执行**：PG transaction + outbox、idempotency、worker lease/attempt、崩溃重领、删除屏障、分层 receipt 和 read-your-writes 选项。
4. **上下文装配**：L1/L2/L3 的查询规划、类型和信任过滤、token 预算、来源证据、不可信数据标记。Mem0 search 只返回事实候选，不负责整个提示包。
5. **合规删除与审计**：跨 PG、Milvus 主 collection、entity collection、Mem0 SQLite history/messages、缓存、派生摘要、日志和备份的删除矩阵。审计正文与操作元数据分别定义保留期。
6. **外部契约与生态**：版本化 HTTP/gRPC、生成 SDK、审计导出、webhook outbox，以及之后的 MCP adapter。Mem0 OSS 没有这些面向 Thinkback 租户语义的接口。

## 4. 其他项目提供的设计参照

### 4.1 Zep Cloud 与 Graphiti OSS

| 维度 | 一手事实 | 对 Thinkback 的借鉴 | 来源（访问于 2026-10-28） |
| --- | --- | --- | --- |
| Canonical event | Zep episode 是逐字保存的 message/text/JSON 原始工件，派生 facts/entities/summaries 保留 episode 关联。官方也明确：episode 记录输入，不证明输入为真。 | L1 保存原始事件，L3 保存派生事实和 source_refs；来源可追溯与事实可信分开。 | [Zep episodes](https://help.getzep.com/v4/episodes)、[source traceability](https://help.getzep.com/v4/source-traceability)、[Graphiti adding episodes](https://help.getzep.com/v4/graphiti/core-concepts/adding-episodes) |
| 时间与失效 | Graphiti 0.31.2 `EntityEdge` 有 created_at、valid_at、invalid_at、expired_at、reference_time 和 episodes。Zep v4 文档用四时间字段区分事实何时为真与系统何时得知。 | `valid_from/to`、`recorded_at`、来源参考时间和迟到写入规则都值得保留；只有 valid_at/invalid_at 不足以回答系统 as-of。 | [Graphiti edges，263-286 行](https://github.com/getzep/graphiti/blob/d72407dcedac911e1be9fc15b55e8c05a3efcc36/graphiti_core/edges.py#L263-L286)、[Zep facts](https://help.getzep.com/v4/facts) |
| 写入顺序与完成 | Graphiti 要求 add_episode 顺序 await，并建议放后台队列。Zep Cloud 按提交顺序处理同一路径 episode，提供 episode/task/batch 状态；最后一条 processed 也不代表此前没有失败项。 | 顺序约束和逐条失败状态应进入 Thinkback operation 合约，不能用一个 completed 掩盖批次内失败。 | [Graphiti add_episode，1404-1413 行](https://github.com/getzep/graphiti/blob/d72407dcedac911e1be9fc15b55e8c05a3efcc36/graphiti_core/graphiti.py#L1404-L1413)、[Zep ingestion status](https://help.getzep.com/v4/check-data-ingestion-status) |
| 上下文装配 | Zep Context Block 用最近四条消息自动查询，组合多类上下文，默认偏 recall；也支持过滤、limits、reranker 和自定义模板。官方要求保留最近 4-6 条原文，因为抽取可能延迟。 | Thinkback 保留 L1 近期原文和 L2/L3 分工是合理的；装配器要允许复合查询、延迟状态和预算，而非只拿 L3 top-k。 | [Retrieving context](https://help.getzep.com/v4/retrieving-context) |
| 权限与租户 | Graphiti `group_id` 只是命名空间，官方要求应用授权。Zep Cloud 另有项目隔离和 Enterprise ABAC，按 action、graph 和 episode metadata 控制访问。 | namespace 不是权限。即便 Thinkback 内网免鉴权，也要从受信服务端状态生成 scope 和来源标签。 | [Graphiti namespacing](https://help.getzep.com/v4/graphiti/core-concepts/graph-namespacing)、[Zep ABAC](https://help.getzep.com/v4/policy-based-access-control) |
| 删除与保留 | Zep 删除是异步 task。删 episode 不保证摘要中的信息消失，备份、graph write log、旧 snapshot、API/Audit Logs 各有保留期。对象级保留可清 episode 及只由它支持的派生物。 | erase 必须定义派生残留和每种副本的保留期，不能把 API 不可见当作所有副本已擦除。 | [Deleting data](https://help.getzep.com/v4/deleting-data-from-the-graph)、[Context retention](https://help.getzep.com/v4/context-retention) |
| 安全 | 官方要求 memory 只作为证据，不作为指令、策略、凭证或行动授权；Context Block 不应进入 privileged channel。 | Thinkback 的 procedure 若未来进入 instruction path，需要独立审批和权限，普通事实统一按不可信数据处理。 | [Memory security](https://help.getzep.com/v4/memory-security) |
| Procedural 参考 | Zep Cloud Agent Skills 有不可变版本、证据、候选 admission 和恢复为新 candidate；恢复仍需重新通过准入。 | 可借鉴“版本 + 证据 + 审批后发布”，不是直接照搬 agent 自改提示词。 | [Inspect and restore Skills](https://help.getzep.com/v4/agent-skills/manage-skills) |

Graphiti OSS 与 Zep Cloud 的删除行为也不能互相推定。例如 Graphiti 0.31.2 `remove_episode()` 会尝试重建存活节点摘要，而 Zep v4 文档明确说某些共享摘要不会重建。[Graphiti remove_episode](https://github.com/getzep/graphiti/blob/d72407dcedac911e1be9fc15b55e8c05a3efcc36/graphiti_core/graphiti.py#L2174-L2252)

### 4.2 LangMem

- **类型与读写路径**：semantic collection、semantic profile、episodic、procedural 的划分仍成立。Profile 是持续更新的当前状态，collection 是逐条积累并检索；procedural 主要优化系统行为 prompt。[Conceptual guide，0.0.30 对应文档，访问于 2026-10-28](https://langchain-ai.github.io/langmem/concepts/conceptual_guide/)
- **上下文与排序**：官方建议相关性结合 similarity、importance 和使用 strength。Thinkback 可保留 strength 作为跨层装配信号，但 L3 内部不要另造一套与 Mem0 冲突的分数。[同一来源](https://langchain-ai.github.io/langmem/concepts/conceptual_guide/)
- **Namespace 与存储**：多级 namespace 和 structured content 是 LangGraph BaseStore 的组织方式，不是 ACL、canonical event 或时态模型。Core API 可以无存储运行，应用仍负责数据库、权限和装配。[同一来源](https://langchain-ai.github.io/langmem/concepts/conceptual_guide/)
- **后台执行**：本地 `ReflectionExecutor` 用 `queue.PriorityQueue`、pending dict 和 thread 做 debounce；官方明确 serverless 本地线程会终止，应换 LangGraph Platform remote executor。它不是 Thinkback 可直接借用的持久 worker。[Delayed processing](https://langchain-ai.github.io/langmem/guides/delayed_processing/)、[固定 reflection.py](https://github.com/langchain-ai/langmem/blob/48e3c11f5bb527282c7d5339c6a87a0b35abccfc/src/langmem/reflection.py)

对 Thinkback 最有用的是“类型决定读写路径”，不是引入 LangMem 作为另一引擎。LangMem 主分支仍无新 release，实施时不应把主分支示例当成稳定 API 承诺。

### 4.3 Cognee

| 维度 | 当前一手事实 | 对 Thinkback 的借鉴 | 来源（访问于 2026-10-28） |
| --- | --- | --- | --- |
| 写入状态 | `remember()` 区分 permanent 和 session 路径，RememberResult 有 running/completed/errored/session_stored、dataset/pipeline run 等字段；索引中召回可能不完整。 | 分层 receipt 和 ready 状态比单一 completed 更准确。 | [Remember](https://docs.cognee.ai/core-concepts/main-operations/remember) |
| 持久执行边界 | 当前文档有 per-dataset lock、失败 rollback 和 API server 启动时 abandoned-run recovery；锁是进程内的，默认嵌入存储不能多进程共享，library-only 不触发启动恢复。 | 可借鉴按 scope 串行和恢复状态，但 Thinkback 应用 PG lease/outbox 实现跨重启所有权，不能照搬进程锁。 | [Pipelines](https://docs.cognee.ai/core-concepts/building-blocks/pipelines) |
| 版本与审计 | 节点 version 不会自动保存旧值；provenance ledger 是可选且有覆盖限制，旧文档内容不保留，recall 没有 as-of。 | 版本号不能替代历史正文；SourceEvent 与必要快照要在覆盖写前保存。 | [Memory Versioning and History](https://docs.cognee.ai/core-concepts/further-concepts/memory-versioning) |
| 删除 | `forget()` 区分 graph/vector/relational、raw files、session cache 和 search history；raw file 可保留，磁盘字节还可能留在 append-only fragments。部分 cache 清理是 best-effort。 | 删除契约要逐资源列出保证，success 也要能表达未完成的派生清理。 | [Forget](https://docs.cognee.ai/core-concepts/main-operations/forget) |
| 权限与数据集 | multi-user 模式按 dataset 做权限与物理存储路由；tenant 是权限主体，不天然等于数据库边界。 | Thinkback 可让 namespace 表示业务边界，但存储分区和权限策略要分别定义。 | [Data Isolation and Multi-Tenancy](https://docs.cognee.ai/core-concepts/multi-user-mode/data-isolation-and-multi-tenancy) |
| 上下文 | `recall(only_context=True)` 可返回将送给 LLM 的上下文而不生成答案，并区分 graph/session 等来源。 | ContextPackage 应可读、可审计、可不触发额外模型调用；记录每层来源。 | [Recall](https://docs.cognee.ai/core-concepts/main-operations/recall) |

`1.6.6rc1` 的 release note 明确把 `clean_history=True`、数据集删除时清 history、history purge 失败即失败和移除 `run_info` 原始数据列为新变化。复审旧设计时不能把这些全部当成 1.6.5 已有能力。[1.6.6rc1 release](https://github.com/topoteretes/cognee/releases/tag/v1.6.6rc1)

### 4.4 Letta 当前 Harness

- **MemFS**：每个 agent 的记忆是 Git repository，文件提交后形成版本历史；根目录文件每轮进入 system prompt，带子目录 `MEMORY.md` 的内容按需读取。这个“常驻核心 + 按需展开”的装配思路可参考，但 Letta 的文件模型不适合替代 Thinkback 的事实库。[MemFS，0.34.x 当前文档](https://docs.letta.com/concepts/memfs/index.md)
- **检索**：MemFS 默认没有 semantic/vector index，普通文件搜索是主路径；keyword/semantic/hybrid 需要可选 MemFS Search mod。不能沿用“Letta 当前就是向量记忆”的笼统说法。[同一来源](https://docs.letta.com/concepts/memfs/index.md)
- **共享与权限**：私有 MemFS 与组织共享 repository 分开，shared repositories 主要是 Cloud 功能；旧 shared blocks 是 legacy。组织角色、agent 共享和 conversation 可见性分别有规则。[Shared memory](https://docs.letta.com/concepts/shared-memory/index.md)、[Team permissions](https://docs.letta.com/teams/permissions/index.md)
- **持久会话的真相**：agent 与 conversation 持久，SDK session 是连接；断线后不重放事件，要 resume 后用 listMessages/bootstrapState 对账，send 成功后也不能盲目重试。这个边界适合 Thinkback 外部 SDK 契约借鉴。[Sessions, turns, and durability](https://docs.letta.com/agent-sdk/sessions/index.md)
- **记忆整理**：dreaming 用后台 subagent 整理记忆，MemFS 用 git worktree 支持并发修改。它证明后台反思与版本化可以组合，但没有替 Thinkback 提供持久 outbox 或合规删除。[Memory & dreaming](https://docs.letta.com/configuration/memory/index.md)、[Agent SDK memory](https://docs.letta.com/agent-sdk/memory/index.md)

### 4.5 MCP 只作为接入协议

MCP 不提供记忆类型、租户、时态、删除或抽取语义。Thinkback 应先稳定普通版本化 API，再把同一应用服务映射到 MCP：

- `2026-07-28` 每个请求携带版本和 capabilities，server 必须实现 `server/discover`；兼容旧客户端需要 dual-era 或明确的支持矩阵。[Versioning](https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning)
- tools 是 model-controlled，适合写、搜、删、查；resources 是 application-driven，适合原文、证据和历史；prompts 是 user-controlled，不应拿来隐藏自动执行的记忆写入。[Tools](https://modelcontextprotocol.io/specification/2026-07-28/server/tools)、[Resources](https://modelcontextprotocol.io/specification/2026-07-28/server/resources)、[Prompts](https://modelcontextprotocol.io/specification/2026-07-28/server/prompts)
- Tasks 是可选扩展，任务 handle 必须在响应前持久创建，客户端支持不一。Thinkback 仍需普通 operation 查询和同步回退。[Tasks](https://modelcontextprotocol.io/extensions/tasks/overview)
- Authorization 处理 OAuth、resource metadata、audience 和 scope，不定义 Thinkback 的 tenant/user/dataset/memory scope。每次按 handle 操作都要重新做应用授权或业务归属校验。[Authorization](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization)、[Stateful Tools](https://modelcontextprotocol.io/specification/2026-07-28/server/tools#stateful-tools)

## 5. 横向对照：不要把不同层的能力混为一谈

| 维度 | Mem0 OSS | Zep / Graphiti | LangMem | Cognee | Letta |
| --- | --- | --- | --- | --- | --- |
| Canonical event | SQLite 最近 messages + 单条 history，非完整事件账本 | episode + source refs；Zep 托管最完整 | 应用/LangGraph 负责 | 原始 data/document + pipeline/provenance | conversation + Git MemFS |
| 时态/版本 | created/updated + expiration；无 OSS temporal/as-of | Graphiti 双时态字段，Zep 文档完整 | 由应用 schema 决定 | 有 version/ledger，但无完整旧值和 as-of | Git 历史，非双时态事实模型 |
| 上下文装配 | search/get_all，应用组装 | Graphiti 返回图记录；Zep 有 Context Block | 应用或 LangGraph agent 组装 | recall/only_context 与来源类型 | 常驻文件 + 按需目录/文件读取 |
| 权限/租户 | user/agent/run 标签，无 org/ACL | group_id 需应用授权；Zep Cloud 有治理 | namespace 标签，权限在外层 | dataset 权限与 tenant 角色 | agent/conversation/org 与 shared repo |
| 删除/审计 | 当前索引删除，history/messages 可残留 | 有 CRUD；Zep 另有 task、保留和日志规则 | 由 store 与应用决定 | 多资源 forget，磁盘字节仍可能残留 | Git 历史与文件删除语义 |
| 持久执行 | AsyncMemory 非持久任务 | Graphiti 建议外部队列；Zep 有 task/batch 状态 | 本地 ReflectionExecutor 非持久，远程靠 LangGraph | 有 run 状态与恢复，但进程锁和部署限制明显 | agent/conversation 持久，连接事件不自动重放 |
| 插件/SDK | Python/TS、providers、OSS REST；无 OSS webhook | Python Graphiti；Zep Python/TS/Go | Python 函数和 LangGraph store | Python/TS、tasks/pipelines/retrievers | Agent SDK、MemFS、mods |

## 6. 对当前插件设计的具体修订建议

按对正确性的影响排序：

1. **把升级验收从“mem0ai 升到 2.2.x”改为可证明的能力验收。** 固定 2.2.1，确认新 Milvus collection schema、`_entities` collection、`[nlp]` 模型、实际 `score_details` 中的 BM25/entity 信号、filter 操作符和中文语料。老 collection 不重建时，应明确记录 semantic-only，而不是宣称混合检索。
2. **保留并明确冲突治理。** 10-26 设计中“Mem0 负责去重、ADD-only 与本地 supersede 并存”的方向需要提升为必做项；OSS 不会自动完成当前事实合并。纠正、用户撤销和人工编辑应走可审计的显式流程。
3. **修改行业词表假设。** `entity_vocabulary` 可作为 Thinkback metadata/filter 和 prompt 配置，但不能写成“升级后自然成为 Mem0 entity matching 词表”。中文与半导体实体的收益目前未知，先建评测再决定是否补充领域召回。
4. **把 erase 范围扩到 Mem0 messages。** 目标清单至少包含 PG L1/L2/治理索引、Milvus 主 collection 与 entity collection、Mem0 SQLite history、Mem0 SQLite messages、缓存、审计正文和备份。2.2.1 未找到按 memory/user 清 history/messages 的公开 API；问题 4 仍未解决，不能用直接 SQL 手术冒充稳定接口。
5. **保留物理保留期清扫。** `expiration_date` 可以替代“召回时隐藏过期事实”的本地逻辑，但不能替代硬删除、备份过期和跨层对账。`decay.py` 只能删除其中确实重复的部分。
6. **完成状态按资源对账。** add 的部分成功、history/entity 的 best-effort、async delete_all 的吞错都要进入失败注入测试。Mem0 返回 success 只作为引擎回执，不是 Thinkback `completed` 的充分条件。
7. **类型注册表不要依赖 Mem0 procedural 特例。** procedure 若以后开放，采用独立版本、证据、审批、回滚和 instruction-path 权限；普通 preference/profile/commitment 与 procedure 不共用信任等级。
8. **保持 Thinkback 自有服务层，不再评估换引擎。** L1/L2、可靠执行、scope、上下文装配、删除审计、API/SDK/webhook 都是 Mem0 OSS 的明确空白或弱项。图数据库、LangMem、Cognee、Letta 只提供设计参照。

## 7. 未知与实施前必须实测的项目

| 未知项 | 当前能确认的事实 | 需要的验证 |
| --- | --- | --- |
| 中文半导体文本的 add/search 质量 | prompt 可定制，检索实体模型固定为英文，BM25 只重排 semantic 候选 | 用真实 Mem0 2.2.1 + Milvus 跑中文、别名、工艺节点、设备型号、冲突纠正和空结果集，记录 score_details |
| 老 Milvus 数据如何迁移到混合检索 | 老 collection 不会自动获得 BM25 | 新建 collection 的回放、双读、回滚、维度一致性、entity collection 权限与删除对账 |
| Mem0 history/messages 的合规擦除 | 公开 CRUD 不做按 subject 物理清理，reset 是全局操作 | 选择受支持机制或明确残留；验证 SQLite、备份和共享 Pod 文件的实际部署边界 |
| Filter 在 Thinkback Milvus 配置下的完整行为 | 官方说明操作符和 wildcard 依 store，且可能静默降级 | 对计划开放的每个 operator 做正向、负向和缺字段测试 |
| Mem0 ADD-only 对“当前事实”的实际排序 | 文档说检索让当前信息浮前，OSS scoring 源码没有 recency/supersede 项 | 构造互相矛盾且新旧时间不同的事实，确认召回顺序；服务层仍要显式 supersede |
| Cognee 稳定版与在线文档差异 | 当前文档已含 1.6.6rc1 行为 | 若借鉴具体 API，按实际安装版本复核，不在 Thinkback 中直接依赖 |
| Platform webhook 投递保证 | 有事件类型和 payload，公开页面未完整说明签名/重试/幂等 | 不作为 Thinkback OSS 设计依据；自己的 outbox 契约另定 |
| 各系统性能与官方 benchmark 的可比性 | 本次只核查接口和实现，没有统一运行条件 | 不做性能排名；Thinkback 用自己的语料、模型、数据量和 SLO 测量 |
