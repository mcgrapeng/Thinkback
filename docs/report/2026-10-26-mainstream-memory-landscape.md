# Thinkback 与主流开源记忆项目对照

日期：2026-10-26

## 范围与方法

本报告覆盖五类设计：事实型记忆 API、时间知识图谱、agent 自有记忆、框架内记忆库、可定制记忆管线。MCP 只作为接入协议讨论，不当作记忆实现。比较依据是官方文档、固定 commit 的源码和正式规范，不使用 star 数或二手榜单，也没有做跨项目性能实测。

Zep 托管服务与 Graphiti OSS、Mem0 Platform 与 Mem0 OSS 分开讨论。不同产品的同名功能不直接等价。

## 本次核查基线

| 对象 | 版本或快照 | 说明 |
| --- | --- | --- |
| Mem0 | Python SDK 2.2.1，2026-09-25，commit [94c3fe9](https://github.com/mem0ai/mem0/commit/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd) | 同时核查当前 OSS、Platform 与迁移文档 |
| OpenMemory | 归档快照 commit [2df78cc](https://github.com/mem0ai/openmemory/commit/2df78cc48e5f688a771fee5b39899325accc2450) | 旧 MCP+仪表盘方案，不作为当前推荐架构 |
| Graphiti | 0.31.2，2026-10-20，commit [d72407d](https://github.com/getzep/graphiti/commit/d72407dcedac911e1be9fc15b55e8c05a3efcc36) | Zep 在线文档使用 v4，托管引擎与 OSS 分开 |
| Letta | 0.16.8，2026-05-14，commit [1131535](https://github.com/letta-ai/letta/commit/1131535716e8a31c9a437f8695e25ac98f203a24) | 同时核查 legacy memory blocks 与当前 MemFS |
| LangMem | pyproject 0.0.30，主分支 commit [48e3c11](https://github.com/langchain-ai/langmem/commit/48e3c11f5bb527282c7d5339c6a87a0b35abccfc) | 没有 Release 或 Tag，不称为稳定 release |
| Cognee | 稳定版 1.6.5，2026-10-16，commit [37f788b](https://github.com/topoteretes/cognee/commit/37f788bd287750e10d7610173e5332b3843f6ebd) | 未采用 1.6.6rc1 预 release |
| MCP | 稳定规范 2026-07-28，commit [5f5440b](https://github.com/modelcontextprotocol/modelcontextprotocol/commit/5f5440bb26a62e2cf3440b92da5a667efa03b267) | 另需考虑旧客户端兼容 |

在线文档访问日期均为 2026-10-26。没有独立发布时间的页面按访问日期记录。

## 各项目的一手事实

### Mem0

当前 OSS 迁移文档说明，新算法使用单遍 ADD-only 抽取，检索融合 semantic、BM25 keyword 和 entity matching。OSS 已移除外部 graph store，graph memory 转向 Platform。`search()` / `get_all()` 的实体 ID 改为放入 `filters`，默认 `top_k`、threshold 和 rerank 也有变化。

来源：[Mem0 OSS migration](https://docs.mem0.ai/migration/oss-v2-to-v3)。

对 Thinkback 的意义：

- 当前锁定和安装的是 Mem0 2.1.0，本次研究基线是 2.2.1。一个小版本差本身不构成技术脱节。
- Thinkback 的 `_scope_kwargs()` 已按实际方法签名选择 `filters` 或旧式顶层参数，这是有用的兼容处理。
- 上游 ADD-only 与 Thinkback 本地 supersede 可以同时成立，但物理后端会保留更多历史数据，删除和对账必须覆盖这些记录。
- 上游转向多信号融合后，Thinkback 不应再用单一词典交集硬过滤抹掉语义召回。升级还需要重测 threshold、真实 embedding、prompt 输出形状和 monkey-patch 的内部 API。
- OpenMemory 已归档，不能拿它的旧 MCP 结构当新插件方案。

### Zep / Graphiti

固定版本的 `EntityEdge` 同时包含 `created_at`、`expired_at`、`valid_at`、`invalid_at` 和 `reference_time`，并保留来源 episodes。字段语义区分事实何时为真、何时被系统记录，以及事实来自哪个参考时刻。

来源：[Graphiti edges.py，固定 commit](https://github.com/getzep/graphiti/blob/d72407dcedac911e1be9fc15b55e8c05a3efcc36/graphiti_core/edges.py)。

Graphiti 支持用 Pydantic ontology 定义自定义实体和边类型。Zep 当前安全文档明确要求把检索上下文当作不可信数据，记忆不能充当指令、策略、凭证或行动授权；来源 metadata 还应包含 review_state，行动权限在模型之外执行。

来源：[自定义类型](https://help.getzep.com/graphiti/core-concepts/custom-entity-and-edge-types)、[memory security](https://help.getzep.com/memory-security)。

对 Thinkback 的意义：

- `valid_at` / `invalid_at` 是值得保留的方向，但只有两个字段不等于完整双时态。还需要记录系统时间、来源参考时间、迟到写入规则、as-of 查询和删除语义。
- 图数据库只在多跳关系、动态企业数据和关系问答有需求时增加。对话事实记忆不必强行图化。
- 企业写入程序性规则或系统行为时，需要来源证明、审批状态、写权限和检索降权。普通偏好事实不应与可执行规则共用信任等级。

### Letta

Letta 的当前文档已将 memory blocks/shared blocks 标为 legacy，方向转向 MemFS 和 shared repositories。Thinkback 可以借鉴 agent 自有记忆与共享记忆的边界，但不应照抄旧 block 设计。

来源：[Letta shared memory](https://docs.letta.com/concepts/shared-memory)。

对 Thinkback 的意义：固定 scope `thinkback` 无法表达“企业共享画像、助手私有记忆、团队共享知识”三种命名空间。插件模型需要显式 namespace 和读写 ACL，而不是给所有 assistant 共用一个 agent_id。

### LangGraph / LangMem

LangMem 区分 semantic、episodic、procedural。Semantic memory 又分 collection 与 profile：collection 适合逐条积累并检索，profile 适合有固定 schema、持续更新的当前状态。其 namespace 可按 organization、user、application 分层，内容可以是结构化数据。

来源：[LangMem conceptual guide](https://langchain-ai.github.io/langmem/concepts/conceptual_guide/)。

对 Thinkback 的意义：

- L1/L2/L3 是物理层，不是业务类型系统。
- 企业需要配置的是 profile、preference、episode、procedure、rule、event 等类型。每类有独立 schema、抽取指令、触发器、作用域、保留策略、读路径、敏感级别和版本。
- 召回相关度不能只看向量相似度，也可结合 importance 与使用强度。Thinkback 已有 strength 信号，可以保留，但它应与 query score 一起参与排序。

### Cognee

Cognee 当前 `remember()` 支持 permanent 与 session 两种路径。session 先写缓存并快速返回，之后可通过后台 improve 桥接到永久图；permanent 路径生成关系记录、原文、chunks、图结构、embeddings 和 summaries。结果对象区分 running、completed、errored、session_stored。文档明确说明索引期间可能出现部分召回，并提供数据集和 pipeline run 状态查询。

来源：[Cognee remember](https://docs.cognee.ai/core-concepts/main-operations/remember)、[vector stores](https://docs.cognee.ai/setup-configuration/vector-stores)。

对 Thinkback 的意义：

- 写入完成状态应分层表达。Thinkback 可以返回 source_event_id、write_id、L1/L2/L3 各层状态与 ready_at，并提供 read-your-writes 选项。
- 派生记忆需要记录 extractor prompt/version、LLM、embedder、chunker、源事件和 schema version。只存最终文本和向量，升级后难以解释或重建。
- Cognee 的 forget 文档也说明逻辑删除与磁盘物理擦除不是一回事。Thinkback 的 invalidation、不可召回、硬删除和备份过期需要分别定义。

来源：[Cognee forget](https://docs.cognee.ai/core-concepts/main-operations/forget)。

### MCP

MCP 2026-07-28 使用每请求携带版本与 capabilities 的模型，不再只依赖 initialize 握手。服务器必须实现 `server/discover`。旧版客户端仍使用 initialize，因此通用接入需要 dual-era 或明确的版本支持矩阵。

来源：[MCP versioning](https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning)。

MCP tools、resources、prompts 的控制主体不同：写、搜、删、查适合 tools；原文、证据和历史适合 application-driven resources；人工审批工作流才适合 prompts。Tasks 是可选扩展，客户端支持不一，仍需普通状态查询或同步回退。

来源：[tools](https://modelcontextprotocol.io/specification/2026-07-28/server/tools)、[resources](https://modelcontextprotocol.io/specification/2026-07-28/server/resources)、[prompts](https://modelcontextprotocol.io/specification/2026-07-28/server/prompts)、[Tasks](https://modelcontextprotocol.io/extensions/tasks/overview)。

MCP authorization 处理 OAuth、Protected Resource Metadata、PKCE、resource audience 和 scope step-up，但不定义 tenant、user、dataset 或 memory scope。Thinkback 必须从已认证主体和策略推导这些值，不能信任模型传入的 tenant_id 或 clientInfo。

来源：[MCP authorization](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization)。

## 按维度对照 Thinkback

| 维度 | 主流资料给出的参考 | 对 Thinkback 当前设计的判断 |
| --- | --- | --- |
| 分层 | 原始证据、近期上下文、派生长期状态在多个项目中都存在 | L1/L2/L3 合理，不需要为追新而重写 |
| 业务类型 | LangMem 类型划分、Graphiti ontology、Cognee 自定义 graph model | 固定五类枚举和 P0 槽位不足以配置企业记忆 |
| 命名空间 | LangMem 多级 namespace、Zep user/graph、Cognee dataset | 固定 L3 scope 无法区分共享与私有记忆 |
| 时间 | Graphiti 同时保存事实时间与系统记录信息 | 当前更像 valid-time 生命周期，严格双时态尚未成立 |
| 检索 | Mem0 多信号融合，LangMem similarity + importance + strength | 单槽位跳搜和词典硬过滤会损害召回，属于明显偏离 |
| 写入 | Cognee 分层结果、Graphiti 顺序 episode、Mem0 分层事件状态 | 幂等和任务状态方向合理，completed 语义和崩溃恢复需修正 |
| 删除 | 失效、过期、硬删除、物理擦除分开定义 | 当前主要保证不可召回，物理残留与跨层对账不足 |
| 安全 | Zep 以来源、review_state、作用域和模型外授权控制记忆 | assistant 可自证事实、分类无行为、legacy 绕过和 tenant 碰撞需先修 |
| 接入 | MCP 版本协商、普通 API/SDK、任务回退 | HTTP/gRPC 是起点，现无正式 SDK、MCP、Webhook，协议文档也有漂移 |
| 可移植性 | 多存储适配、派生产物版本化重建 | MemoryBackend 可替换整个 L3，但策略与 Mem0 内部补丁耦合较紧 |

## 对插件化目标的借鉴顺序

1. 先修外部契约依赖的安全与一致性：关闭或保护 legacy，租户独立存储，统一 Scope，持久化 API key，修正更新/删除完成语义。
2. 定义身份与作用域：tenant、subject、application、assistant、dataset、session 各自独立，明确共享和私有 namespace 的 ACL。
3. 建立记忆类型注册表。现有 P0 槽位可以作为一个内置 semantic profile 插件，不再承担全部记忆类型。
4. 统一写入状态与删除状态。每层分别报告 accepted、processing、ready、failed，删除区分不可召回与物理擦除，并有持久 outbox 和对账。
5. 重做 context assembler：保留 L1/L2/L3 来源和强度，恢复 query score 的作用，支持复合查询、分层预算和不可信数据标记。
6. 在普通版本化 API 之上增加 MCP 适配器，支持 dual-era、普通状态查询回退和资源证据链。不要先做一个只有工具名单的 `/mcp` 外壳。
7. 最后再按真实查询需求评估图存储、复杂 reranker 和更多存储后端。

## 不应照抄的旧方案

- [OpenMemory 已归档](https://github.com/mem0ai/openmemory/blob/2df78cc48e5f688a771fee5b39899325accc2450/openmemory-archive/README.md)。
- [Letta memory blocks/shared blocks 已属 legacy](https://docs.letta.com/concepts/shared-memory)。
- [Cognee memify 已 deprecated](https://docs.cognee.ai/core-concepts/main-operations/legacy-operations/memify)。
- [Graphiti 的 MCP/REST 是参考实现，不能默认当成有认证的企业生产服务](https://github.com/getzep/graphiti/blob/d72407dcedac911e1be9fc15b55e8c05a3efcc36/README.md)。

## 研究限制

本报告没有做各项目性能排名，也没有验证其官方 benchmark。MCP 与上游项目变化较快，实施前需按目标客户端和实际依赖版本重验兼容矩阵。Thinkback 的缺陷评级以同仓库的源码审计和行为复现为准，本文件只提供对照依据。
