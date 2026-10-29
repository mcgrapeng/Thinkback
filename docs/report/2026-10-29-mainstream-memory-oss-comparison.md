# 主流记忆开源系统对照（面向 Thinkback 插件化改造）

日期：2026-10-29 · 所有在线资料访问日期：2026-10-29

---

## 1. 方法与来源

### 1.1 方法

只采信一手来源：GitHub 仓库代码与工作流文件、仓库内 `docs/*.mdx`、官方文档站、GitHub issue 原文。凡是无法从一手来源核实的，下文显式标 **unverified**。

代码引用采用 `仓库@commit:文件:行` 或 `仓库/路径` 形式；`main` 分支的行号在 2026-10-29 有效，未来可能漂移。

### 1.2 对象与版本基线

| 对象 | 仓库 | 版本 / commit 基线 | License | Stars（2026-10-29） | 一手来源 |
| --- | --- | --- | --- | --- | --- |
| Mem0 | `mem0ai/mem0` | `mem0ai` 2.2.1（`pyproject.toml:7`） | Apache-2.0 | 67,996 | [repo](https://github.com/mem0ai/mem0)、[pyproject](https://github.com/mem0ai/mem0/blob/main/pyproject.toml) |
| Graphiti | `getzep/graphiti` | `main` | Apache-2.0 | 32,306 | [repo](https://github.com/getzep/graphiti) |
| Cognee | `topoteretes/cognee` | `main` | Apache-2.0 | 32,860 | [repo](https://github.com/topoteretes/cognee) |
| Supermemory | `supermemoryai/supermemory` | `main` | MIT | 31,444 | [repo](https://github.com/supermemoryai/supermemory) |
| Letta | `letta-ai/letta` | `archive` 分支（V1 API server） | Apache-2.0 | 25,273 | [repo](https://github.com/letta-ai/letta)、[archive](https://github.com/letta-ai/letta/tree/archive) |
| LangMem | `langchain-ai/langmem` | `main` | MIT | 1,727 | [repo](https://github.com/langchain-ai/langmem) |
| Agent Plugins 规范 | `agentplugins/agent-plugins-spec` | Spec v1.1.0（Working Draft） | Apache-2.0 + CC-BY-4.0 | 1,413 | [spec](https://github.com/agentplugins/agent-plugins-spec/blob/main/spec/1.1.0.md)、[站点](https://agent-plugins.org/) |
| MCP 规范 | `modelcontextprotocol/modelcontextprotocol` | `2026-07-28`（最新 release） | — | — | [release](https://github.com/modelcontextprotocol/modelcontextprotocol/releases/tag/2026-07-28) |
| Mem0 评测套件 | `mem0ai/memory-benchmarks` | `main` | Apache-2.0 | 132 | [repo](https://github.com/mem0ai/memory-benchmarks) |

**排序说明（按真实采用度）**：mem0（68k）> cognee（32.9k）≈ graphiti（32.3k）≈ supermemory（31.4k）> letta（25.3k）> langmem（1.7k）。langmem 星数低但它是 LangChain 官方 SDK，生态内嵌深，不能按星数低估。Letta 当前主线已迁到 `letta-ai/letta-code`，`letta-ai/letta` 的 `README.md` 自述为 "retired Letta V1 API server"，当前活跃代码在 `letta-ai/letta-code`（[README](https://github.com/letta-ai/letta/blob/main/README.md)）。

### 1.3 与本仓库既有报告的关系

本仓库已有两份相关报告，本报告**不重复**它们的引擎内部核查，而是补上"插件契约 / 横向共识 / 生产化清单 / 已知缺陷"四个维度：

- `docs/report/2026-10-28-mainstream-memory-landscape.md` — mem0 2.2.1 逐函数源码核查（add/search/delete/history/messages 残留等）。本报告引用其结论时不重复贴行号，只在需要新的判断时补一手引用。
- `docs/report/2026-10-28-real-stack-mem0-capability-probe.md` — 真实栈（Milvus + 中文 BM25）实测。

本报告新增的一手材料：**Agent Plugins v1.1.0 规范**、**mem0 官方 MCP 工具表**、**mem0 官方 agent-plugin-core（含 conformance 框架）**、**Supermemory MCP 源码级工具面**、**Cognee MCP 的 tool-mode 分层**、**Graphiti MCP 工具面**、**7 个仓库的 CI 工作流清单**、**10 个带 issue 原文的已知缺陷**。

---

## 2. 各项目档案

### 2.1 mem0（`mem0ai/mem0`）

| 维度 | 事实 | 来源 |
| --- | --- | --- |
| 架构 | 单进程库 + 自带 REST server。`Memory` 类持有 vector_store / graph_store / history_db 三件套；server 复用同一 `Memory` 实例（`server/server_state.py` 的 `_memory_instance`） | [main.py](https://github.com/mem0ai/mem0/blob/main/mem0/memory/main.py)、[server_state.py](https://github.com/mem0ai/mem0/blob/main/server/server_state.py) |
| 数据模型 | 一条记忆 = 一行向量记录 + `id / memory / hash / user_id / agent_id / run_id / metadata / created_at / updated_at / categories`；另有 SQLite `history` 表（ADD/UPDATE/DELETE 行）与 `messages` 表（每 scope 保留最近 10 条原文） | [storage.py](https://github.com/mem0ai/mem0/blob/main/mem0/memory/storage.py) |
| 写入生命周期 | `add(messages, user_id/agent_id/run_id, metadata, infer, memory_type, prompt, expiration_date)`；`infer=True` 走单遍 ADD-only 抽取（一次 LLM 调用，无 UPDATE/DELETE 判定）；`prompt` 是**每次写入自定义抽取指令**的入口 | [main.py:760](https://github.com/mem0ai/mem0/blob/main/mem0/memory/main.py#L760) |
| 检索生命周期 | `search(query, top_k=20, filters, threshold=0.1, rerank, explain, reference_date, show_expired)`；多信号融合 `combined = (semantic + bm25 + entity_boost) / max_possible`，**threshold 只卡语义分**，候选低于语义阈值的即使 BM25/entity 高也被排除 | [main.py:1393](https://github.com/mem0ai/mem0/blob/main/mem0/memory/main.py#L1393)、[scoring.py:60-115](https://github.com/mem0ai/mem0/blob/main/mem0/utils/scoring.py#L60) |
| 作用域 | OSS 仅 `user_id` / `agent_id` / `run_id`（`app_id` 是 Platform-only）；`filters` 必须至少含其一 | [main.py:1393](https://github.com/mem0ai/mem0/blob/main/mem0/memory/main.py#L1393) |
| 图记忆 | **已从 OSS 移除**：`enable_graph` + `graph_store` 配置块被删，graph memory 变成 Platform 的内置常开特性 | [oss-v2-to-v3 迁移指南](https://github.com/mem0ai/mem0/blob/main/docs/migration/oss-v2-to-v3.mdx) |
| 集成面 | ① Python/TS SDK；② 自带 REST server（`POST /memories`、`GET /memories`、`POST /search`、`PUT /memories/{id}`、`GET /memories/{id}/history`、`DELETE /memories[/{id}]`、`POST /reset`）；③ **hosted MCP** `https://mcp.mem0.ai/mcp`（11 个工具，见 §3.1）；④ **Agent Plugins 生态**（自建 manifest + skills + 6 个 host adapter）；⑤ 大量框架集成目录（n8n、zapier、strands、vercel-ai、openclaw…） | [server/main.py](https://github.com/mem0ai/mem0/blob/main/server/main.py)、[mem0-mcp.mdx](https://github.com/mem0ai/mem0/blob/main/docs/platform/mem0-mcp.mdx)、[integrations/](https://github.com/mem0ai/mem0/tree/main/integrations) |
| 治理 | OSS server：JWT + per-user `X-API-Key`、`ADMIN_API_KEY` 兼容、`/api-keys` 签发与吊销（只存 hash 与 prefix）、`/requests` 审计日志（method/path/status/latency/auth_type）。**无** RBAC、无 per-tenant 物理隔离、无 retention policy | [api_keys.py](https://github.com/mem0ai/mem0/blob/main/server/routers/api_keys.py)、[requests.py](https://github.com/mem0ai/mem0/blob/main/server/routers/requests.py)、[auth.py](https://github.com/mem0ai/mem0/blob/main/server/auth.py) |
| 评测 | 独立开源评测套件 `mem0ai/memory-benchmarks`，含 LOCOMO / LongMemEval / BEAM，**可对 OSS 与 Cloud 两种后端跑**（OSS 路径起本地 docker compose + Qdrant） | [memory-benchmarks README](https://github.com/mem0ai/memory-benchmarks/blob/main/README.md) |
| 生产就绪信号 | 34 个 workflow（`ci-gate.yml`、`pr-gate.yml`、`release.yml`、每集成独立的 `*-checks.yml` / `*-cd.yml`）；`docs-llms-txt-check.yml` 保证 `llms.txt` 与 docs 同步；`vouch-check-pr.yml` 做 PR 门禁 | [.github/workflows](https://github.com/mem0ai/mem0/tree/main/.github/workflows) |
| 弱点 | 见 §4（E）：删除残留、并发写损坏索引、过滤语义、静默降级 |

### 2.2 Zep / Graphiti（`getzep/graphiti`）

| 维度 | 事实 | 来源 |
| --- | --- | --- |
| 定位 | README 明确切分：**"Graphiti is a framework for building graphs, and Zep unifies, governs, and serves an enterprise's context."** Graphiti = 图构建框架；Zep = 托管治理层（Konig 图数据库 + Enterprise ABAC + 保留策略） | [README](https://github.com/getzep/graphiti/blob/main/README.md) |
| 数据模型 | 四类节点：Entity（人/物/概念，摘要随时间演化）、Community（社区摘要）、Episode（原始数据，逐字保存）、自定义类型（Pydantic ontology）；边为 `EntityEdge`，字段含 `created_at / valid_at / invalid_at / expired_at / reference_time / episodes` | [edges.py:263-286](https://github.com/getzep/graphiti/blob/main/graphiti_core/edges.py#L263) |
| 双时态 | 事实何时为真（`valid_at`/`invalid_at`）与系统何时得知（`created_at`）分开；`reference_time` 是"事件发生时间"锚点 | [edges.py](https://github.com/getzep/graphiti/blob/main/graphiti_core/edges.py)、[MCP add_memory 文档串](https://github.com/getzep/graphiti/blob/main/mcp_server/src/graphiti_mcp_server.py) |
| 检索 | 混合：语义 embedding + BM25 关键词 + 图遍历；`search_memory_facts` 支持 `valid_at_after/before`、`invalid_at_after/before` 时间过滤 | [graphiti_mcp.py](https://github.com/getzep/graphiti/blob/main/mcp_server/src/graphiti_mcp_server.py) |
| 作用域 | `group_id` 是命名空间，**不是权限**。MCP README 自己写明："This server is a reference implementation. Do not use the server as a production deployment. The server has no authentication, no authorization, and no prompt-injection protection." | [mcp_server/README.md](https://github.com/getzep/graphiti/blob/main/mcp_server/README.md) |
| 集成面 | Python 库 + 官方 MCP server（13 个工具：`add_memory` / `search_nodes` / `search_memory_facts` / `delete_entity_edge` / `delete_episode` / `get_entity_edge` / `get_episodes` / `get_episode_entities` / `summarize_saga` / `build_communities` / `add_triplet` / `clear_graph` / `get_status`）+ `mcp_server/` Docker 镜像 | [graphiti_mcp_server.py](https://github.com/getzep/graphiti/blob/main/mcp_server/src/graphiti_mcp_server.py) |
| 生产就绪信号 | 15 个 workflow，含 `mcp-server-tests.yml`、`server-tests.yml`、`unit_tests.yml`、`typecheck.yml`、`lint.yml`、`codeql.yml` | [.github/workflows](https://github.com/getzep/graphiti/tree/main/.github/workflows) |
| 弱点 | 见 §4（E）：Neo4j 下 `group_id` 不生效的跨组读删（#2020）、FalkorDB 共享 driver 串组（#1795/#2033）、MMR 丢最相关候选（#1978） |

### 2.3 Letta / MemGPT（`letta-ai/letta`，V1 archive）

| 维度 | 事实 | 来源 |
| --- | --- | --- |
| 三类记忆 | **core memory**（`persona` / `human` 两个 block，常驻 system prompt）、**archival memory**（语义可检索的长期条目）、**recall memory**（对话历史） | [function_sets/base.py:10](https://github.com/letta-ai/letta/blob/archive/letta/functions/function_sets/base.py#L10) |
| Agent 自主管理 | Agent **自己调用工具**改记忆：`core_memory_append` / `core_memory_replace`（精确串替换，old_content 不在则 `raise ValueError`）/ `rethink_memory`（整块重写）/ `archival_memory_insert`（带 tags）/ `archival_memory_search`（query + tags + `tag_match_mode` any/all + `start_datetime`/`end_datetime`） | [base.py:164-300](https://github.com/letta-ai/letta/blob/archive/letta/functions/function_sets/base.py#L164) |
| 写入语义 | 与 mem0 相反：**append / replace / rethink 是三种显式工具**，不是 LLM 自动 ADD/UPDATE 决策。核心 block 是"当前真值"（replace 覆盖），archival 是追加历史 | 同上 |
| 作用域 | agent / conversation / org 三层；共享 memory 通过 shared block / shared repository | [base.py](https://github.com/letta-ai/letta/blob/archive/letta/functions/function_sets/base.py) |
| 集成面 | REST/SDK 为主；V1 支持 `ToolCreate.from_mcp`（把 MCP 工具挂给 agent） | [schemas/tool.py:146](https://github.com/letta-ai/letta/blob/archive/letta/schemas/tool.py#L146) |
| 生产就绪信号 | V1 `letta-ai/letta` 只有 `issue-guard.yml` 一个 workflow（代码已归档）；当前主线在 `letta-ai/letta-code`，16 个 workflow 含 `ci.yml`、`release.yml`、`nightly-update-smoke.yml`、`container.yml` | [letta workflows](https://github.com/letta-ai/letta/tree/main/.github/workflows)、[letta-code workflows](https://github.com/letta-ai/letta-code/tree/main/.github/workflows) |
| 弱点 | 见 §4（E）：core memory 投毒跨会话泄漏（#3388）、duplicate-label 无守卫（#3397） |

### 2.4 Cognee（`topoteretes/cognee`）

| 维度 | 事实 | 来源 |
| --- | --- | --- |
| 定位 | 图 + 向量双管线：文本 → 实体/关系/可检索 chunk；代码 → 符号/依赖图；`cognify` 是把原始数据变成图的后台索引动作 | [README](https://github.com/topoteretes/cognee/blob/main/README.md)、[cognee-mcp/README.md](https://github.com/topoteretes/cognee/blob/main/cognee-mcp/README.md) |
| 四个主操作 | `remember`（写）/ `recall`（读，含 `only_context` 只装配上下文不生成答案）/ `improve`（ enrich + session→graph 桥接）/ `forget`（删）。CLI: `cognee-cli remember/recall` | [README](https://github.com/topoteretes/cognee/blob/main/README.md) |
| 本地可跑 | "Runs locally for free — no API key required"，用 GLiNER 本地抽取 + 本地 embedding；LLM 只在需要生成答案/媒体处理时才要 | [README](https://github.com/topoteretes/cognee/blob/main/README.md) |
| 作用域 | dataset = 隔离单位；多用户模式按 dataset 做权限与物理路由；tenant 是权限主体，不天然等于 DB 边界 | [cognee-mcp README](https://github.com/topoteretes/cognee/blob/main/cognee-mcp/README.md) |
| 集成面 | Python / TS SDK / Rust SDK（cognee-rs）/ REST API / **官方 MCP** / Claude Code plugin / Codex plugin / OpenClaw plugin | [README](https://github.com/topoteretes/cognee/blob/main/README.md) |
| MCP 工具面（关键设计） | **默认只 pin 三个工具**（`remember` / `recall` / `forget`）+ `cognify_status`；其余工具通过 `search_tools` 检索发现，`COGNEE_MCP_TOOL_MODE=default\|minimal\|all` 切换。理由写在 README："Advertising every tool up front costs agent context and hurts tool-selection accuracy" | [cognee-mcp/README.md](https://github.com/topoteretes/cognee/blob/main/cognee-mcp/README.md)、[tool_registry.py](https://github.com/topoteretes/cognee/blob/main/cognee-mcp/src/tool_registry.py) |
| recall 状态语义 | 返回体 `_meta["cognee/memory"]["state"]` 有四值：`found` / `indexing` / `build_failed` / `none`——**明确区分"没存"和"还在索引"** | [cognee-mcp/README.md](https://github.com/topoteretes/cognee/blob/main/cognee-mcp/README.md) |
| 治理 | ontology 可上传并按 `ontology_key` 选择（永久写才生效）；`forget` 分 graph/vector/relational/raw files/session cache/search history 多资源 | [cognee-mcp/README.md](https://github.com/topoteretes/cognee/blob/main/cognee-mcp/README.md) |
| 生产就绪信号 | **60+ 个 workflow**，覆盖多 DB（`vector_db_tests` / `graph_db_tests` / `search_db_tests` / `relational_db_migration_tests`）、`helm-ci`、`scorecard.yml`、`cve_alert.yml`、`test_mcp.yml`、`dockerhub-mcp.yml`、nightly/e2e/load/perf 报告 | [.github/workflows](https://github.com/topoteretes/cognee/tree/main/.github/workflows) |
| 弱点 | 见 §4（E）：cognify 滞后被误读为空召回（#3553）、用户 SQL 连接串 SSRF（#4415）、Neo4j 全图摘要 OOM（#4832） |

### 2.5 LangMem SDK（`langchain-ai/langmem`）

| 维度 | 事实 | 来源 |
| --- | --- | --- |
| 定位 | LangChain 官方 SDK。两种范式：**hot path**（agent 主动调工具）与 **background**（`create_memory_store_manager` 后台抽取 + 整合） | [README](https://github.com/langchain-ai/langmem/blob/main/README.md)、[background_quickstart.md](https://github.com/langchain-ai/langmem/blob/main/docs/docs/background_quickstart.md) |
| 内存类型 | semantic collection（逐条累积、检索）、semantic profile（持续更新的当前状态）、episodic、procedural（优化系统行为 prompt） | [conceptual_guide](https://langchain-ai.github.io/langmem/concepts/conceptual_guide/) |
| 记忆工具 | `create_manage_memory_tool(namespace, instructions, schema, actions_permitted=("create","update","delete"))` → 工具签名 `manage_memory(content, id, action)`；`create_search_memory_tool(namespace)` | [knowledge/tools.py:25](https://github.com/langchain-ai/langmem/blob/main/src/langmem/knowledge/tools.py#L25) |
| 配置驱动 | `instructions`（何时该调用）、`schema`（结构化 Pydantic 模型）、`namespace`（层级，运行时用 `{langgraph_user_id}` 模板填）、`actions_permitted`（允许的动作集）——**这四个旋钮就是"配置声明要记什么"的最小形状** | 同上 |
| 存储 | **不带存储**。持久化交给 LangGraph `BaseStore`（`InMemoryStore` / `AsyncPostgresStore`）。Core API 可无存储运行 | [README](https://github.com/langchain-ai/langmem/blob/main/README.md) |
| 后台执行 | `ReflectionExecutor` 本地是 `queue.PriorityQueue` + thread 的 debounce，**serverless 本地线程会随进程终止**；生产需 LangGraph Platform remote executor | [reflection.py](https://github.com/langchain-ai/langmem/blob/main/src/langmem/reflection.py)、[delayed_processing](https://langchain-ai.github.io/langmem/guides/delayed_processing/) |
| 生产就绪信号 | 只有 `deploy_docs.yml` + `release.yml` 两个 workflow，**没有 CI workflow、没有 test workflow** | [.github/workflows](https://github.com/langchain-ai/langmem/tree/main/.github/workflows) |
| 弱点 | 见 §4（E）：写失败报成功（#196）、删除后残留（#202）、本地 worker CANCELLED（#70/#11） |

### 2.6 Supermemory（`supermemoryai/supermemory`）

| 维度 | 事实 | 来源 |
| --- | --- | --- |
| 定位 | 商业研究团队做的"memory + context engine"，自托管形态是**单二进制、嵌入式图引擎、内置本地 embedding**，无 Docker、无数据库 | [self-hosting/overview](https://supermemory.ai/docs/self-hosting/overview) |
| 数据模型 | 单一 memory 结构 + ontology；profile 分 `static`（长驻事实）与 `dynamic`（近期上下文）两段 | [get-profile.ts:32-36](https://github.com/supermemoryai/supermemory/blob/main/apps/mcp/src/server/tools/get-profile.ts#L32) |
| MCP 工具面（源码级） | 16 个工具：`add_memory` / `forget_memory` / `search_memory` / `get_profile` / `list_memories` / `list_spaces` / `list_documents` / `get_document` / `memory_graph` / `fetch_graph_data` / `who_am_i` / `select_space` / `set_active_tag` / `guided-save` / `save-memory` / `prepare-file-upload` / `upload-file` | [apps/mcp/src/server/tools/](https://github.com/supermemoryai/supermemory/tree/main/apps/mcp/src/server/tools) |
| MCP 工程细节 | ① **tool annotations 分类**：读工具 `readOnlyHint:true/destructiveHint:false/idempotentHint:true`；写工具 `destructiveHint:true`；`save-memory` 用 additive 注解；② 每个工具同时给 `outputSchema` + `structuredContent`（机器可读）；③ 工具名 snake_case 改名后**保留旧 camelCase 别名**（`get_document`/`getDocument`）并标注 deprecated；④ resources 暴露 `supermemory://spaces`、`supermemory://profile` | [annotations.ts](https://github.com/supermemoryai/supermemory/blob/main/apps/mcp/src/server/tools/annotations.ts)、[tools/index.ts](https://github.com/supermemoryai/supermemory/blob/main/apps/mcp/src/server/tools/index.ts)、[resources/namespaces.ts](https://github.com/supermemoryai/supermemory/blob/main/apps/mcp/src/server/resources/namespaces.ts) |
| 作用域 | namespace（space）= 一等公民；`select_space` 工具切换活跃 space，`namespace` 参数可覆盖 | [add-memory.ts](https://github.com/supermemoryai/supermemory/blob/main/apps/mcp/src/server/tools/add-memory.ts) |
| 删除语义 | `forget_memory` 分两条路径：给 `ids`（1–100 个）直接删；给 `content` 则先搜 5 个候选再取**完全相等**的那条才删，否则不删——**拒绝模糊删** | [forget-memory.ts](https://github.com/supermemoryai/supermemory/blob/main/apps/mcp/src/server/tools/forget-memory.ts) |
| 评测 | 自称 LongMemEval / LoCoMo / ConvoMem 第一（README 声明） | [README](https://github.com/supermemoryai/supermemory/blob/main/README.md) |
| 生产就绪信号 | 11 个 workflow（`ci.yml`、`ci-python.yml`、`claude-auto-fix-ci.yml`、多个 publish）；**无** DCO/scorecard 类供应链门禁 | [.github/workflows](https://github.com/supermemoryai/supermemory/tree/main/.github/workflows) |
| 弱点 | 见 §4（E）：无幂等键（#1627）、无治理层（#1348）、OOM（#1826）、启动竞态吞错（#1208） |

### 2.7 Mem0 OpenMemory（MCPO）—— **已归档**

| 事实 | 来源 |
| --- | --- |
| OpenMemory 原本是 mem0 仓库下的自托管 MCP server + dashboard，现已移出并**明确归档**。归档 README 首行："**OpenMemory** (the self-hosted MCP server and dashboard that lived in `mem0ai/mem0` under `openmemory/`) is no longer actively maintained. This directory is a read-only snapshot kept for reference." | [openmemory-archive/README.md](https://github.com/mem0ai/openmemory/blob/main/openmemory-archive/README.md) |
| 归档版 MCP 工具面只有 5 个，且**无任何作用域参数**：`add_memories(text, infer)` / `search_memory(query)` / `list_memories()` / `delete_memories(memory_ids)` / `delete_all_memories()`。全部按"the user's memory"隐式单用户 | [mcp_server.py](https://github.com/mem0ai/openmemory/blob/main/openmemory-archive/api/app/mcp_server.py) |
| 官方建议替代路径："Looking for local self-hosted memory with a dashboard? Use the Mem0 self-hosted server" | 同上 |
| `mem0ai/openmemory` 仓库本身已被复用为**跨 harness 会话搬运 CLI/TUI**（把 Claude Code / Codex / OpenCode 的会话互导），与 MCP 记忆服务无关 | [openmemory README](https://github.com/mem0ai/openmemory/blob/main/README.md) |

> 结论：**"Mem0 OpenMemory (MCPO)" 这个 2024–2025 年的主流 MCP 记忆形态今天已经不存在**。现存形态是 hosted MCP（`mcp.mem0.ai`，Platform-only）+ Agent Plugins 包（自建，OSS 代码）。

### 2.8 其他值得知道的

| 项目 | 状态 | 来源 |
| --- | --- | --- |
| **Agent Plugins v1.1.0** | 开放厂商中立标准，定义**两种可移植组件**：Agent Skills（`skills/<name>/SKILL.md`）与 MCP servers（`mcp.json`）。TSC 初始成员含 Amazon、Cursor、Microsoft、OpenAI、Vercel | [agent-plugins.org](https://agent-plugins.org/)、[spec](https://github.com/agentplugins/agent-plugins-spec/blob/main/spec/1.1.0.md) |
| Mem0 官方 agent 插件 | mem0 自己实现了 Agent Plugins 包（`plugin.json` v0.3.3 + `mcp.json` + 6 个 skills），且带**跨 host 一致性 conformance 框架**（`conformance/run.py`，8 个 group，产出机器可读 JSON 报告） | [agent-plugin-core/README.md](https://github.com/mem0ai/mem0/blob/main/integrations/agent-plugin-core/README.md)、[conformance/run.py](https://github.com/mem0ai/mem0/blob/main/integrations/agent-plugin-core/conformance/run.py) |
| Mem0 官方 `search_memories` MCP 工具 | 单一工具，参数 `query` / `top_k`(1–20) / `category`(枚举) / `scope`(`repo`/`dir`/`mine`) / `run_id`；**schema 用 `additionalProperties: false` 且自己做严格校验**（未知参数报错） | [mcp_server.py:26-113](https://github.com/mem0ai/mem0/blob/main/integrations/agent-plugin-core/python/mcp_server.py#L26) |
| Mem0 官方 coding 记忆分类 | 5 类：`project_knowledge` / `decisions_and_constraints` / `workflows` / `problems_and_fixes` / `results`，每类一段自然语言定义 + 每类一条抽取 prompt。**这就是"配置声明记什么"的现成范式** | [memory_core.py:91-126](https://github.com/mem0ai/mem0/blob/main/integrations/agent-plugin-core/python/memory_core.py#L91) |
| Zep Platform | 托管层含 Context Block、任务化摄入、ABAC、保留策略、Agent Skills（不可变版本 + 证据 + 审批） | [Zep docs](https://help.getzep.com/v4/zep-vs-graphiti)（引自本仓库 `2026-10-28` 报告） |
| A-MEM / 论文系 | 影响 OSS 设计但无独立主流部署形态 | **unverified**（本次未做论文溯源） |

---

## 3. 横向对照

### 3.1 MCP 记忆协议在实践中的形状（题 C 的直接答案）

**主流 MCP 记忆服务器暴露的工具集（一手源码/文档）**

| 服务器 | 工具数 | 工具名（原文） | 作用域参数 | 来源 |
| --- | --- | --- | --- | --- |
| mem0（hosted, Platform） | 11 | `add_memory` `search_memories` `get_memories` `get_memory` `update_memory` `delete_memory` `delete_all_memories` `delete_entities` `list_entities` `list_events` `get_event_status` | 隐式（client 身份决定） | [mem0-mcp.mdx](https://github.com/mem0ai/mem0/blob/main/docs/platform/mem0-mcp.mdx) |
| mem0 agent-plugin（本地 stdio） | 1 | `search_memories` | `scope`(`repo`/`dir`/`mine`) + `run_id` + `category` | [mcp_server.py](https://github.com/mem0ai/mem0/blob/main/integrations/agent-plugin-core/python/mcp_server.py) |
| Supermemory | 16 | `add_memory` `forget_memory` `search_memory` `get_profile` `list_memories` `list_spaces` `list_documents` `get_document` `memory_graph` `fetch_graph_data` `who_am_i` `select_space` `set_active_tag` `guided-save` `save-memory` `prepare-file-upload` `upload-file` | `namespace`（space） | [tools/](https://github.com/supermemoryai/supermemory/tree/main/apps/mcp/src/server/tools) |
| Cognee | 3(+1) 默认 pin | `remember` `recall` `forget` + `cognify_status`（另有 `code_search`、`search_tools`、`call_tool`） | `dataset_name` / `session_id` / `ontology_key` | [cognee-mcp/README.md](https://github.com/topoteretes/cognee/blob/main/cognee-mcp/README.md) |
| Graphiti | 13 | `add_memory` `search_nodes` `search_memory_facts` `delete_entity_edge` `delete_episode` `get_entity_edge` `get_episodes` `get_episode_entities` `summarize_saga` `build_communities` `add_triplet` `clear_graph` `get_status` | `group_id` / `group_ids` | [graphiti_mcp_server.py](https://github.com/getzep/graphiti/blob/main/mcp_server/src/graphiti_mcp_server.py) |
| OpenMemory（归档） | 5 | `add_memories` `search_memory` `list_memories` `delete_memories` `delete_all_memories` | **无** | [mcp_server.py](https://github.com/mem0ai/openmemory/blob/main/openmemory-archive/api/app/mcp_server.py) |

**从这张表能读出的行业事实（不是我的设计偏好，是源码事实）**

| 事实 | 证据 |
| --- | --- |
| 工具名一律 snake_case（`add_memory` / `search_memories`），但 Supermemory 迁移期同时保留 camelCase 别名 | [tools/index.ts](https://github.com/supermemoryai/supermemory/blob/main/apps/mcp/src/server/tools/index.ts) 的 `LEGACY_TOOL_NAMES` |
| **写与读分工具，删除独立成工具**（不是 `add(action="delete")`）——只有归档的 OpenMemory 用过 `action` 复用写法 | Supermemory / Cognee / Graphiti / mem0 均为独立 `forget` / `delete_*` |
| 工具声明用 MCP `annotations` 区分读写与破坏性（`readOnlyHint` / `destructiveHint` / `idempotentHint`） | [annotations.ts](https://github.com/supermemoryai/supermemory/blob/main/apps/mcp/src/server/tools/annotations.ts) |
| 工具**同时**给 `outputSchema` + `structuredContent`，让机器读结构、人读 text | Supermemory 每个工具都有 `outputSchema` |
| 长任务返回 handle + 单独的状态查询工具，而不是阻塞 | mem0 MCP 有 `list_events` + `get_event_status`；Cognee 有 `cognify_status` |
| "召回为空"必须能区分"没存"和"还在写" | Cognee 的 `state ∈ {found, indexing, build_failed, none}` |
| 工具面要**克制**——Cognee 明确 pin 三个，其余走 `search_tools` | [cognee-mcp/README.md](https://github.com/topoteretes/cognee/blob/main/cognee-mcp/README.md) |
| MCP 规范本身只规定工具名/参数/结果形状，**不规定记忆语义**；规范还要求工具名 1–128 字符、建议 `[A-Za-z0-9_.-]` | [MCP tools spec](https://modelcontextprotocol.io/specification/2026-07-28/server/tools) |
| MCP 协议版本是"每个请求携带版本 + capabilities"，server 必须实现 `server/discover` | [MCP versioning](https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning) |

### 3.2 跨项目共识表

| 维度 | mem0 | Graphiti | Letta | Cognee | LangMem | Supermemory | 共识? |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **向量存储归谁** | 引擎内置抽象（OSS 可换 Qdrant/Milvus/pgvector/…） | 引擎不接管，**自带图数据库**（Neo4j/FalkorDB/Neptune/Kuzu） | 引擎自带（Postgres/pgvector 路线） | 引擎自带，可换 | **不接管**，交给 LangGraph `BaseStore` | 引擎内置（自托管为嵌入式图引擎） | ⚠️ 分歧。共识只在"**存储可插拔**"，不在"引擎拥有存储" |
| **图/关系建模** | OSS 已移除，Platform 常开；OSS 只做 entity boost | 一等公民，双时态边 | 无（档案库是语义检索） | 一等公民（实体+关系+社区摘要） | 有 `graph_rag.py` 但非核心 | 内置图引擎 | ⚠️ 分歧。Graphiti/Cognee 认为是核心，mem0/Letta 认为是可选 |
| **抽取由谁触发** | 服务端自动（`add` 内 LLM） | 服务端自动（episode 抽取） | **agent 主动调工具** | 服务端自动（`cognify`） | 两种都有（hot path 工具 / background manager） | 服务端自动 | ⚠️ 分歧：agent 自主 vs 服务端自动 |
| **冲突处理** | ADD-only（**不自动合并**） | 自动失效旧边，保留历史 | agent 自己 replace/rethink | session→graph 桥接 + improve | `create_memory_store_manager` 会 update 已有项 | 有 contradictions 处理（README 声明） | ⚠️ 分歧大。只有 Graphiti 的"失效但不删"是明确的双时态答案 |
| **作用域模型** | user/agent/run（+app 限 Platform） | group_id（纯命名空间） | agent/conversation/org | dataset（+session） | namespace（LangGraph 层级） | namespace/space | ✅ 共识：**至少要有一个"归属"维度**，但维度集合不统一。**没有一家把 namespace 当权限** |
| **每条写入带 scope id** | ✅ user_id/agent_id/run_id | ✅ group_id | ✅ agent_id | ✅ dataset | ✅ namespace | ✅ namespace | ✅ **强共识** |
| **幂等** | 无（SDK 无 idempotency key） | 无 | 无 | 无 | 无 | **明确缺失**（issue #1627 提案中） | ✅ 共识：**这层没人做好**，是 Thinkback 的机会 |
| **删除可核对** | 弱（delete 不清 history/messages） | 有 CRUD，但派生残留 | Git 历史语义 | `forget` 分资源，但磁盘字节可能残留 | 删除后残留（issue #202 实测） | 拒绝模糊删（好） | ⚠️ **删除可核对是全行业空白** |
| **LLM 角色** | 抽取 + 冲突决策（OSS 现在只抽取） | 抽取 + 摘要 | 无（agent 自己写） | 抽取 + 摘要 + 生成答案 | 抽取 + 反思 | 抽取 + profile | ⚠️ 分歧，但共识是 **LLM 在写入侧，不在读取侧** |
| **读取侧排序** | 引擎融合分（semantic+bm25+entity），**无 recency** | 图遍历 + 语义 + BM25 | agent 自己选 | 自动路由 | 应用层（importance + similarity + strength） | 相似度 + profile | ⚠️ 分歧。**只有 mem0 Platform 有 decay（软排序偏置）** |
| **暴露面** | SDK + REST + hosted MCP + Agent Plugins + 20+ 集成 | SDK + MCP server | REST + SDK + MCP-client 接入 | SDK(4 语言) + REST + MCP + plugin | Python 函数（嵌在 LangGraph） | SDK + MCP + plugin | ✅ 共识：**MCP + SDK 双通道**，REST 普遍 |
| **异步长任务有状态** | Platform: `list_events`/`get_event_status`；OSS: 无 | 返回即入队，另有 `get_status` | agent run 有状态 | `cognify_status` | 后台 future（本地不持久） | 文档处理异步 | ✅ 共识：**状态查询是必需的**，实现质量普遍差 |
| **评测** | 自带开源套件（LOCOMO/LongMemEval/BEAM，可跑 OSS） | 有 benchmark 引用 | 有 | 有 | ❌ 无 | 自称第一 | ⚠️ 只有 mem0 的**评测套件可对 OSS 跑**，是可直接借用的 |
| **CI 门禁** | 34 workflows，含 `pr-gate.yml` | 15，含 `codeql.yml` | letta-code 16 | **60+**，含 scorecard/CVE | **仅 2（无 CI）** | 11，无 scorecard | ⚠️ 差异极大 |

---

## 4. A–E 问题的回答

### A. 主流共识在哪里？偏离即设计气味

**A1. 「归属维度」是硬共识。** 全部 7 个项目都要求每次写入携带至少一个 scope 标识（`user_id`/`group_id`/`dataset`/`namespace`/`agent`）。没有一家允许"匿名记忆"。Thinkback 的 `namespace` + `user_id` 已符合。

**A2. 「namespace ≠ 权限」是硬共识。** Graphiti 官方明确 `group_id` 只是命名空间、要求应用自行授权；Cognee 明确 tenant 是权限主体但不等于 DB 边界；LangMem 的 namespace 完全没有 ACL 语义。**Thinkback 的 D10（内网免鉴权、网络即信任边界）在这条共识上是合规的**，但要注意：**一旦 Thinkback 变成"任何企业 AI 助手都能挂载的插件"，网络就不再是信任边界**——这是 Thinkback 自己的定位变化带来的新义务，不是 mem0/Zep 的错。

**A3. 「LLM 在写入侧，不在读取侧」是共识。** 抽取、摘要、合并都是写入侧 LLM；读取侧一律是确定性检索（向量 + BM25 + 图遍历）。**Thinkback 若在召回路径上引入 LLM 做重写或判断，是偏离共识**——LLM 重排会让延迟、成本和可复现性都不可控。Thinkback 现有的"引擎融合分 + 服务层显式信号重排"（ADR-0003）符合共识。

**A4. 「读取侧必须有 token 预算与装配层」是共识。** 没有一家引擎返回"直接喂给 LLM 的最终 context"——Cognee 有 `recall(only_context=True)`（文档原话："Returns what the LLM would have received instead of its answer. The final LLM answer is not generated."，[recall 文档](https://docs.cognee.ai/core-concepts/main-operations/recall)）、Zep 有 Context Block、LangMem 要应用自己装。Thinkback 的 ContextPackage / ContextAssembler 是对的。

**A5. 「异步长任务必须可查状态」是共识，但实现普遍很差。** 唯一实现正确的是 Cognee（`state ∈ {found, indexing, build_failed, none}`）和 mem0 Platform（`list_events` + `get_event_status`）。**Thinkback 的 `WriteReceipt.layers.{journal,context_view,facts}.state` 三层独立状态机，正是这条共识的正确形态**——这一点 Thinkback 比主流 OSS 更强。

**A6. 「工具面要克制」是共识。** Cognee 明确 pin 3 个工具、其余走 `search_tools`；MCP 规范本身也警告聚合多服务器时工具名会冲突、建议加前缀。Thinkback 若做 MCP，应 pin 一个最小集。

**偏离共识的地方（Thinkback 需注意）**

| 偏离 | 判定 |
| --- | --- |
| Thinkback 自建 Milvus collection schema（ADR-0007/D21） | **不算偏离**，但要知道代价：mem0 官方 Milvus 实现硬编码 `text` 字段的分析器参数、不带中文分词器（[milvus.py](https://github.com/mem0ai/mem0/blob/main/mem0/vector_stores/milvus.py)），且 mem0 升级时建表逻辑变化不会自动生效。Thinkback 已用双写 + 48h 双读 + 兼容测试兜住，这是正确做法 |
| Thinkback 自建时态有效性列（`valid_from/valid_to`） | **不算偏离**，因为 mem0 OSS 明确不支持：`timestamp` 参数 docstring 写 "Platform-only temporal parameter. Not supported in OSS"，`reference_date` 同理（[main.py:769](https://github.com/mem0ai/mem0/blob/main/mem0/memory/main.py#L769)）。但要注意 Graphiti 的教训：**只有 valid_at/invalid_at 不足以回答"系统何时知道"**，Thinkback 的 `recorded_at` 是对的 |
| Thinkback 自建 L1 缓存 | **不算偏离**——没有任何一家引擎提供跨请求的会话级原文缓存，LangMem 明确要求应用自备 store，Letta 的 recall memory 是 agent 侧的 |
| Thinkback 自建抽取 prompt（版本化文件） | **不算偏离**——mem0 原生支持每次写入带 `prompt` / `custom_instructions`（[base.py:54](https://github.com/mem0ai/mem0/blob/main/mem0/configs/base.py#L54)），Thinkback 只是把 prompt 管理做成带版本的文件。这是正确的"薄封装" |
| Thinkback 自建摘要（L2） | **不属于记忆引擎的活**。mem0/Letta/Cognee 都不做"会话上下文视图"。Thinkback 保留 L2 是对的 |

### B. Thinkback 在哪里重复造轮子？

按"mem0 已经做了 / mem0 做不了 / mem0 做得很差"三分法，逐项判定 Thinkback 的自有实现：

| Thinkback 自有实现 | mem0 已做？ | mem0 做不了（→ 保留理由成立） | mem0 做得很差（→ 补的理由成立） | 判定 |
| --- | --- | --- | --- | --- |
| **Milvus collection schema + 中文 BM25 + sparse 字段** | 部分：mem0 有 Milvus 向量库适配、有 BM25 打分，但建表时 `text` 字段不带中文分析器 | — | 是：BM25 中文命中为 0（已实测） | **保留，但收窄**：schema 归 Thinkback，**打分归 mem0**。不要自造融合分——mem0 的 `score_and_rank` 已做 semantic+bm25+entity 归一化（[scoring.py](https://github.com/mem0ai/mem0/blob/main/mem0/utils/scoring.py)） |
| **抽取 prompt（版本化文件）** | 是：`add(prompt=...)` + `custom_instructions` | — | — | **保留但要薄**：只做"按类型选哪个 prompt + 记录版本号"，不要自造抽取管线 |
| **L1 缓存** | 否 | ✅ mem0 无会话级原文缓存 | — | **保留**（引擎完全没有） |
| **L2 上下文视图 / 摘要** | 否 | ✅ mem0 无此概念 | — | **保留**（引擎完全没有） |
| **时态有效性 `valid_from/valid_to`** | 否（OSS 不支持 timestamp/reference_date） | ✅ | — | **保留**（引擎做不了） |
| **policy_version / extractor_version 打在每条记忆上** | 否 | ✅ mem0 无 | — | **保留** |
| **四档擦除 + 审计** | 部分：mem0 `delete` 只删向量行，history/messages 残留 | ✅ 删除残留是 mem0 已知缺陷（[issue #7452](https://github.com/mem0ai/mem0/issues/7452)） | mem0 删除**吞错**：`AsyncMemory.delete_all` 逐条收集错误只记日志，仍返回成功 | **保留，且这是 Thinkback 最强的差异化点** |
| **幂等键（event_id）** | 否 | ✅ mem0 无 idempotency key | — | **保留**（全行业空白，见 [supermemory #1627](https://github.com/supermemoryai/supermemory/issues/1627) 的提案） |
| **配置驱动的类型注册表（声明记什么）** | 部分：mem0 有 `memory_type`（**仅 procedural**，其他值抛 `Mem0ValidationError`——[main.py:831](https://github.com/mem0ai/mem0/blob/main/mem0/memory/main.py#L831)）、Platform 有 `custom_categories`；mem0 官方插件有 5 类 coding 分类 | 部分：mem0 OSS 的类型只有 procedural 一个值 | mem0 Platform 的 `custom_categories` 是**分类打标**，不是"抽取什么"的规则；不是同一件事 | **保留**，但形状应抄 mem0 官方插件那 5 类的写法（定义 + 每类一条抽取指令），而不是自造一套 |
| **召回装配 / token 预算** | 否（`search` 只返回 top-k 事实） | ✅ | — | **保留**（引擎完全没有） |
| **Policy version / A-B 抽取 prompt** | 否 | ✅ | — | **保留** |
| （Thinkback **没有**的）图存储 / 关系建模 | mem0 OSS 已移除 graph；Graphiti/Cognee 有一等公民实现 | — | — | **不要建**。Thinkback D4 明确不引入图存储，与 mem0 OSS 现状一致 |

**结论**：Thinkback **没有**明显重复造轮子，但有**两处正在滑向重复**：

1. **自造召回融合分**。ADR-0003 说"引擎分数只在同一次查询内可比，服务层可叠加显式信号重排"——这是对的。但要守住边界：**服务层只做"保留/丢弃"的显式信号（如预算裁剪、类型硬过滤已下线后残留的收窄），不要重新归一化成自己的分数**。mem0 的 `score_and_rank` 已经把 semantic/bm25/entity 归一化过一次；Thinkback 再乘一次权，得到的分数既不可解释也不可与 mem0 对账。

2. **自造混合检索（如果 L3 仍走 mem0 的 search）**。mem0 2.2.1 的 BM25 **只对 semantic 候选重排，不扩召回**（[scoring.py:75](https://github.com/mem0ai/mem0/blob/main/mem0/utils/scoring.py#L75) 的注释："Threshold gates the semantic score BEFORE combining -- candidates below the threshold are excluded even if BM25/entity would boost them"）。ADR-0004 已经把 mem0 的语义门关掉（threshold=0）自己判分——这是**对的方向**（因为中文场景语义分低但词命中），但它意味着 Thinkback 已经接管了"候选集构建"。这条边界必须写清楚：**候选集构建归 Thinkback（因为 mem0 的语义门对中文不适用），候选集内排序归 mem0**。如果哪天 Thinkback 自己做了 BM25 召回扩展，那就是重复造轮子了。

### C. 行业标准的插件/集成契约是什么？

**C1. MCP 是事实上的记忆接入协议，但只是"接入"，不是"契约"。** MCP 规范只定义工具/资源/提示词的形状，**不定义记忆语义、租户、时态、删除、抽取**（[MCP tools](https://modelcontextprotocol.io/specification/2026-07-28/server/tools)、[resources](https://modelcontextprotocol.io/specification/2026-07-28/server/resources)、[prompts](https://modelcontextprotocol.io/specification/2026-07-28/server/prompts)）。所有主流厂商的做法都一样：**先有版本化的普通 API，再把同一应用服务映射成 MCP**。

**C2. 打包格式已有开放标准：Agent Plugins v1.1.0。** 这是本次调研最重要的发现之一，因为它正好回答了"plugin-shaped service"的形状问题：

- 包 = 一个目录，根下必须有 `plugin.json`；可选 `skills/<name>/SKILL.md`（Agent Skills 格式）与 `mcp.json`（MCP 服务器配置）。
- `plugin.json` 的 `$schema` 指向 `https://agent-plugins.org/schemas/1.1.0/plugin.schema.json`；必填字段只有 `$schema` 与 `name`（name 约束：`^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$`，长度 1–64）；可选 `version` / `description` / `author` / `homepage` / `repository` / `license` / `keywords` / `extensions`（反向域名命名空间，给客户端放私有行为）。
- `mcp.json` 声明 `mcpServers`，三种传输：`stdio` / `streamable-http` / `sse`（后者标为 deprecated）。客户端**必须**至少支持 stdio 或 streamable-http 之一。
- **安全规则**：包内所有路径必须留在 plugin root 内（`./` 前缀，`../` 非法）；**header 里不得嵌凭证**（"Header values are visible package data, not a portable secret mechanism. Plugins MUST NOT embed credentials or other secrets in `headers`."）；**规范不定义 OAuth 配置或可移植的凭证引用字段**——授权完全由客户端管理。
- 加载规则是**容错**的：`mcp.json` 无效 / 版本不支持 / 服务器起不来 → 只禁用该部分，插件其余组件照常加载。
  来源：[spec 1.1.0 §4/§5/§7.2](https://github.com/agentplugins/agent-plugins-spec/blob/main/spec/1.1.0.md)、[mcp.schema.json](https://github.com/agentplugins/agent-plugins-spec/blob/main/schemas/1.1.0/mcp.schema.json)

**C3. mem0 官方已经把这件事做完了，可以直接抄形状。** `integrations/agent-plugin-core/` 是一个**跨 6 个 host 共享的插件内核**，包含：

- `plugin.json`（v0.3.3，schema 指向 agent-plugins.org）
- `mcp.json`（stdio 本地 MCP server，只暴露一个 `search_memories` 工具）
- `skills/`（6 个 skill 模板：remember / search / forget / pause / resume / status）
- `build/build.py` + `build/validate.py`（schema 校验 + 生成）
- `conformance/run.py`（**一致性验证框架**：8 个 group，产出一份机器可读 JSON 报告；含离线组与需要一次性 disposable key 的 live 组）
- 契约测试（`tests/test_conformance.py`、`test_spool_delivery.py`、`test_validate.py` 等）
  来源：[agent-plugin-core/README.md](https://github.com/mem0ai/mem0/blob/main/integrations/agent-plugin-core/README.md)

**C4. 「配置声明要记什么」的现成范式有三个，可以挑：**

| 范式 | 出处 | 形状 | 适配 Thinkback 的程度 |
| --- | --- | --- | --- |
| **A. 每类一段定义 + 一条抽取指令** | mem0 官方插件 5 类 coding 分类（`project_knowledge` / `decisions_and_constraints` / `workflows` / `problems_and_fixes` / `results`），每类配一段自然语言定义，服务端按类拼抽取 prompt | `MemoryTypePolicy` 的 `extract_prompt` 是类型注册表的一个字段 | **最高**。Thinkback 已有类型注册表 + `profiles/<ns>/prompts/*.vN.md`（ADR-0012/0014），只差把"类型 → prompt 文件"这一步固定成契约 |
| **B. 结构化 schema（Pydantic）** | LangMem `create_manage_memory_tool(schema=..., instructions=..., actions_permitted=...)`；Cognee 的 OWL `ontology_key` | 类型 = Pydantic 模型；`instructions` 说何时调；`actions_permitted` 限制动作集 | 中。Thinkback 记忆类型有"抽取/准入/冲突/保留/召回路径"多维，比纯 schema 复杂；可借鉴 `actions_permitted` 那种"能力集开关"的显式化 |
| **C. 分类打标** | mem0 Platform `custom_categories`（15 个默认类，可按 project 或按 add 覆盖） | 只是标签，不是抽取规则 | 低。**不要用这个做"声明记什么"**——它是检索后打标，不是写入前准入 |

**C5. 最小意外形状（least-surprise）的建议契约。** 综合以上一手事实，一个企业可挂载的记忆插件，其外部面最少需要：

1. **一份普通版本化 API**（REST 或 gRPC），带显式协议版本（Thinkback 已有 `/v2/memory/*` + gRPC v2）；MCP 是它的投影，不是唯一入口。
2. **MCP 工具面克制**：读 2–3 个（`recall` / `search_memories` / `get_profile` 之类），写 1–2 个（`append_memory` / `add_memory`），删 1 个（`delete_memory`），状态 1 个（`get_operation`）。Cognee 是最好的参照。
3. **每个工具带 MCP annotations**（`readOnlyHint` / `destructiveHint` / `idempotentHint`）+ `outputSchema` + `structuredContent`——Supermemory 是最好的参照。
4. **写工具必须能声明"这次写什么"**：要么透传配置里的类型名（Thinkback 类型注册表），要么像 mem0 那样让调用方只给原始消息、服务端按配置决定抽什么。**后者更符合"配置驱动的插件"定位**。
5. **作用域靠调用方身份解析，不靠调用方传的字符串**：Thinkback 当前 D10 是"内网免鉴权、namespace 是业务分组标签"。一旦要挂载到外部助手，namespace 必须由**受信服务端状态**推导（Zep 的 ABAC、Graphiti 的"namespace 不是权限"都指向这一点），不能接受调用方任意传 `namespace=` 就换租户。
6. **幂等键是必答题**：写入工具必须接受调用方提供的幂等锚点（Thinkback 已有 `event_id`；mem0/Graphiti/Letta/Cognee 都没有）。
7. **删除语义要能在响应里表达"部分完成"**：至少要区分"已不可召回 / 已物理擦除 / 备份未过期"（Thinkback 的四档），并且给出逐资源状态。

**C6. 打包层**：如果要分发客户端插件，采用 Agent Plugins v1.1.0 格式（`plugin.json` + `mcp.json` + `skills/`）几乎无成本——mem0 的 conformance 框架甚至可以直接用作参考实现。**但注意**：Agent Plugins 规范明确**不定义授权/凭证字段**，所以 Thinkback 的鉴权必须在服务端（API key 由客户端配置注入，不是包内 header）。

### D. 生产级记忆服务的必备清单（从一手来源推出）

| # | 必备项 | 谁做到了（证据） | Thinkback 状态 |
| --- | --- | --- | --- |
| 1 | **写入幂等键** | 无主流项目做到；[supermemory #1627](https://github.com/supermemoryai/supermemory/issues/1627) 明确提案为缺失 | ✅ 已有（`event_id`） |
| 2 | **可核对的完成状态**（写成功 ≠ 全链路成功） | mem0 Platform `list_events`/`get_event_status`；Cognee `cognify_status` + 四态 `state` | ✅ 已有三层 `WriteReceipt` |
| 3 | **删除后残留可枚举**（哪些副本还留着原文） | Zep 有保留策略；Cognee `forget` 分资源列；mem0 有已知残留缺陷 | ⚠️ 需确认 erase 是否覆盖 Mem0 SQLite `messages` 表 |
| 4 | **不吞错**（引擎返回 success 时服务层仍对账） | mem0 `AsyncMemory.delete_all` 是反面教材；LangMem #196 是反面教材 | ✅ Thinkback 有对账测试 |
| 5 | **并发写安全** | mem0 #4892：并发 `AsyncMemory` 写损坏 Qdrant HNSW 索引 | ⚠️ 需确认 Thinkback 写路径的并发模型 |
| 6 | **可运行的评测套件（对自己的 OSS 栈）** | 只有 mem0 的 `memory-benchmarks` 支持对自托管 OSS 跑 | ✅ Thinkback 有真实栈门禁（D25/D31） |
| 7 | **schema / 索引版本化 + 可回滚** | Graphiti 有 group/索引管理；mem0 OSS server 有 Alembic | ✅ Thinkback 有 ADR-0007 双写双读 |
| 8 | **配置变更版本化，且版本落在每条记录上** | mem0 Platform 有 policy/config 版本概念 | ✅ Thinkback 有 `policy_version`（ADR-0009） |
| 9 | **按 scope 的 API key 签发/吊销 + 请求审计** | mem0 OSS server：`/api-keys`（存 hash+prefix）、`/requests` 审计日志 | ⚠️ Thinkback 业务面无鉴权（D10）。若要外接，必须补 |
| 10 | **供应链与安全门禁 CI** | cognee：`scorecard.yml` + `cve_alert.yml`；graphiti：`codeql.yml` | ❓ 未查 Thinkback CI 是否含 scorecard/CVE 扫描 |
| 11 | **MCP tool annotations + 结构化输出** | Supermemory 全量实现 | ❌ Thinkback 尚无 MCP |
| 12 | **召回状态的"没存/还在写/写失败"三分** | Cognee `state` | ✅ Thinkback `WriteReceipt` 等价 |
| 13 | **测试覆盖多后端** | cognee 60+ workflow，含 vector/graph/search/relational DB 测试矩阵 | ❓ Thinkback 只对 Milvus 真实栈 |
| 14 | **记忆读取的治理闸门（PII / 投毒 / 读审计）** | **无人做到**：supermemory #1348 就是提这个需求的 | ✅ Thinkback 有分类准入（ADR-0002）+ recall tracking——**这是 Thinkback 领先全行业的地方，应作为差异化卖点** |
| 15 | **把记忆当数据不当指令** | Zep 官方 memory security 要求 | ✅ Thinkback 有 `text_is_restricted_or_unsafe` + "第三方内容视为不可信数据" |

### E. 已知失败模式 —— Thinkback 不要继承的

每条附 issue 原文摘要与链接。

| # | 项目 | 缺陷 | 机制 | Thinkback 的对策 |
| --- | --- | --- | --- | --- |
| E1 | mem0 | `delete_all()` 不删该 scope 的 `messages` 表，下一次 `add()` 又把最近 10 条原文发给 LLM，可能重新抽取成新记忆 | issue 原文：*"There's also no way to clear one scope's messages at all: the only thing that deletes from that table is `reset()`, which drops it for every user."*（[#7452](https://github.com/mem0ai/mem0/issues/7452)，2026-10-19，open） | Thinkback 的 erase 必须把 **Mem0 SQLite `messages`** 列为待清对象，否则"已擦除"不成立 |
| E2 | mem0 | 并发 `AsyncMemory` 写损坏 Qdrant HNSW 索引（`IndexError: index N is out of bounds`），之后读返回错结果 | issue 原文：*"`AsyncMemory`'s write methods dispatch the synchronous `vector_store.insert/update/delete` calls via `asyncio.to_thread()`... Qdrant's `upsert` is not re-entrant"*（[#4892](https://github.com/mem0ai/mem0/issues/4892)，P1，accepted） | Thinkback 的 L3 写入要串行化或自带重试+重建兜底；不能假设引擎并发安全 |
| E3 | mem0 | Qdrant 向量库**静默丢弃** `'*'` 通配符 filter（与 OpenSearch/Pinecone 行为不一致） | [#7113](https://github.com/mem0ai/mem0/issues/7113)（2026-09-24，open） | 任何依赖 filter 通配符的召回路径必须做**行为差异测试**，不能只看文档 |
| E4 | mem0 | `Memory.delete_all` 在 `vector_store.list()` 返回空 list 时抛 `IndexError`；`get_all`/`delete_all` 在非 Chroma 的 LangChain 向量库上 `list()` 返回 None 直接 `TypeError` | [#7759](https://github.com/mem0ai/mem0/issues/7759)、[#7461](https://github.com/mem0ai/mem0/issues/7461) | Thinkback 的删除路径必须有**空集与异构后端的行为测试** |
| E5 | mem0 | `delete_all` 只删第一页（超出向量库 limit 时） | [#4869](https://github.com/mem0ai/mem0/issues/4869)（P1，closed） | 批量删除必须分页 + 复查残留 |
| E6 | LangMem | `MemoryStoreManager.invoke()` 在 store 写/删失败时**仍返回成功**；`ReflectionExecutor` 也看不到这个错误 | issue 原文：*"For updates, the old memory remains; for failed deletions, the memory remains retrievable while the invocation returns an empty list normally. Callers cannot detect those failures from the result."*（[#196](https://github.com/langchain-ai/langmem/issues/196)，2026-10-16，open） | **反面对照教材**。Thinkback 的对账层必须存在，且必须有故障注入测试覆盖它 |
| E7 | LangMem | 删除后残留：实测 60 次删除请求中 2 次秘密仍存于 store；`enable_deletes=False` 时 56 次里 8 次残留；手动 `store.delete()` 才降到 0 | [#202](https://github.com/langchain-ai/langmem/issues/202)（2026-10-21，open，第三方实测） | 用户的"忘掉某条"请求**必须落到一条确定的 delete 路径**，不能依赖默认配置 |
| E8 | LangMem | 本地 `ReflectionExecutor` 的 future 被取消（`CANCELLED`），记忆更新延迟或丢失 | [#11](https://github.com/langchain-ai/langmem/issues/11)、[#70](https://github.com/langchain-ai/langmem/issues/70) | Thinkback 的后台作业必须在 PG 上有持久状态，不能靠进程内线程 |
| E9 | Graphiti | Neo4j 下 `group_id` 在 UUID 寻址操作上**不生效**——跨组读与跨组删，尽管文档说"Accepts an optional `group_id`" | issue 原文：*"On **Neo4j** that routing is structurally a no-op and the model-layer lookups carry **no group predicate**"*（[#2020](https://github.com/getzep/graphiti/issues/2020)，2026-10-21，open） | **Thinkback 的 namespace 隔离必须有"按 id 直查也不跨 namespace"的测试**——这是最容易被漏掉的一类 bug |
| E10 | Graphiti | FalkorDB 写入改共享 driver（`self.driver.clone(database=group_id)`），之后读**答的是最后写入的那个组**，其它组静默返回空 | [#1795](https://github.com/getzep/graphiti/issues/1795)、[#2033](https://github.com/getzep/graphiti/issues/2033) | 隔离必须能被**交叉读写测试**证明，不能只看正常路径 |
| E11 | Graphiti | MMR 可能因为一个未选中的重复项而丢掉**最相关的候选** | [#1978](https://github.com/getzep/graphiti/issues/1978) | 召回去重策略要显式定义（保留哪条、依据什么） |
| E12 | Cognee | cognify 索引滞后时 `recall` 返回空，agent 静默退回 transcript grep，用户无法区分"没存"和"还没可查" | [#3553](https://github.com/topoteretes/cognee/issues/3553)（"Surface cognify lag instead of silent empty recall"） | Thinkback 已有 `WriteReceipt` 状态机，**确保 recall 响应里带上"还在写"的信号**，不要让空结果变成静默失败 |
| E13 | Cognee | 用户提供的 SQL 连接串可触发任意本地文件与内网访问 | [#4415](https://github.com/topoteretes/cognee/issues/4415)（2026-09-21，open，安全） | Thinkback 任何接受外部连接串/URL 的配置项都要有 SSRF 边界 |
| E14 | Cognee | `GET /datasets/graph-summary` 物化整个 Neo4j 图 → OOM | [#4832](https://github.com/topoteretes/cognee/issues/4832) | 管理面查询必须有分页/上限 |
| E15 | Supermemory | SDK 写内存**无幂等键**；网络重试或调用方重试会产生重复记忆、重复 embedding、重复写入 | issue 原文：*"there is no `Idempotency-Key` header and no client helper to keep the same key across retries. Offline sync and batching also need a stable per-content key."*（[#1627](https://github.com/supermemoryai/supermemory/issues/1627)，2026-10-16，open，提案） | **Thinkback 已经领先**——`event_id` 就是幂等锚点（ADR-0008）。不要在未来的 SDK 里丢掉它 |
| E16 | Supermemory | 记忆与检索之间**没有治理层**：PII 直接进 LLM context；连接器同步的外部文档可投毒；无读取审计 | issue 原文三点：*"1. **PII in memories**... 2. **Context poisoning**... 3. **No audit evidence for memory reads**"*（[#1348](https://github.com/supermemoryai/supermemory/issues/1348)，2026-10-24，open，提案） | **Thinkback 的分类准入 + 审计 + 召回追踪正是这块的答案**——应作为对外差异化能力主动宣传 |
| E17 | Supermemory | 自托管本地版无界内存增长 → 每几小时 OOM；重启后卡在 "cannot drop active portal" | [#1826](https://github.com/supermemoryai/supermemory/issues/1826)（2026-10-25，open） | 自托管形态必须有内存/磁盘上限与回收策略 |
| E18 | Supermemory | 启动时 LLM-provider 初始化竞态失败，错误被本地日志吞掉，重试 cron 对可恢复文档终态失败 | [#1208](https://github.com/supermemoryai/supermemory/issues/1208) | 启动期依赖初始化要有可见的失败状态，不能吞错 |
| E19 | Letta | core memory 投毒导致**跨会话状态泄漏**（sandbox 隔离失效） | [#3388](https://github.com/letta-ai/letta/issues/3388) | 记忆写入要有来源信任分级（Thinkback 已有 `provenance` / `trust`） |
| E20 | Letta | `Memory.blocks` 无 duplicate-label 守卫：过期的 core-memory 内容**静默留在编译后的 prompt 里** | [#3397](https://github.com/letta-ai/letta/issues/3397) | 记忆块/类型注册表必须有唯一性约束 + 删除后清 prompt 缓存 |
| E21 | mem0 | 文档示例用顶层 `user_id` 调 `search()`/`get_all()`，在当前版直接 `ValueError` | [#7202](https://github.com/mem0ai/mem0/issues/7202) | Thinkback 的 SDK 文档与实现版本必须一起升级；签名漂移是常见坑 |

**跨项目共同失败模式（三家以上都有）**

1. **删除不彻底**：mem0（E1）、LangMem（E7）、Cognee（多资源残留）三家都有。→ Thinkback 的四档擦除（ADR-0001）是对的，但必须逐副本列表化并做残留测试。
2. **写失败报成功**：mem0（async `delete_all` 吞错）、LangMem（E6）。→ 永远把引擎返回当回执，不当结论。
3. **空结果语义混淆**：Cognee（E12）、mem0（ADD-only 下召回为空其实是正常态）。→ 召回响应必须带状态。
4. **作用域只在正常路径生效**：Graphiti（E9/E10）。→ 隔离必须用交叉路径测试证明。
5. **并发/幂等缺失**：mem0（E2）、Supermemory（E15）。→ Thinkback 的 `event_id` + 串行写 + 对账三层都要有。

---

## 5. Thinkback 应该借什么 / 不该建什么

### 5.1 应该借（照抄形状，不抄实现）

| # | 借什么 | 从谁 | 具体怎么借 |
| --- | --- | --- | --- |
| 1 | **Agent Plugins v1.1.0 包格式** | `agentplugins/agent-plugins-spec` | 若要做客户端分发：`plugin.json`（schema 指 agent-plugins.org/1.1.0）+ `mcp.json`（stdio 或 streamable-http）+ `skills/<name>/SKILL.md`。凭证**不进包**（规范明令 header 不得含 secret） |
| 2 | **跨 host 一致性 conformance 框架** | mem0 `integrations/agent-plugin-core/conformance/` | Thinkback 若出多个 host 适配器，抄这个"一个命令产出一份机器可读报告"的做法，而不是每个 host 各写一套 e2e |
| 3 | **MCP 工具面克制 + 分层发现** | Cognee `COGNEE_MCP_TOOL_MODE` / `search_tools` | pin 一个最小工具集（读 2–3 / 写 1–2 / 删 1 / 状态 1），其余走检索发现 |
| 4 | **MCP annotations + outputSchema + structuredContent** | Supermemory `apps/mcp/src/server/tools/annotations.ts` | 每个工具标 `readOnlyHint` / `destructiveHint` / `idempotentHint`；响应同时给 `structuredContent` 与人读 text |
| 5 | **"召回为空"的四态语义** | Cognee `state ∈ {found, indexing, build_failed, none}` | Thinkback 把它接进 `ContextPackage`，让下游能区分"没记忆"和"记忆还没写进去" |
| 6 | **类型 → 抽取指令 的定义写法** | mem0 官方插件 5 类 coding 分类 | 每类一段自然语言定义 + 一条抽取指令；Thinkback 已有版本化 prompt 文件（ADR-0012），只差把这层契约固定下来并暴露成"配置声明记什么"的对外形状 |
| 7 | **"精确匹配才删"** | Supermemory `forget_memory`（给 content 时只删**完全相等**的候选） | Thinkback 的 delete 语义应同样保守：模糊请求走"列出候选 → 显式确认 → 删 id"，不做语义删除 |
| 8 | **OSS 可跑的评测套件** | mem0 `memory-benchmarks`（LOCOMO / LongMemEval / BEAM，可对自托管 OSS 跑） | Thinkback 的真实栈门禁（D25/D31）可以补一套跨项目可比的基线，避免只跟自己的历史基线比 |
| 9 | **供给侧读取审计 / 治理闸门** | （反例：Supermemory #1348 提议但未实现） | Thinkback 已有分类准入 + 召回追踪 + policy_version，**这是全行业空白**，应主动产品化（每次召回了哪些记忆、经过了什么准入检查、返回给 LLM 的是什么） |
| 10 | **删除的逐资源状态回执** | Zep 的对象级保留与 task 状态 | Thinkback 的 erase 回执按副本逐项列（PG L1/L2 / Milvus 主集合 / entity 集合 / Mem0 SQLite history / Mem0 SQLite messages / 缓存 / 备份），每项一个状态 |

### 5.2 不该建（明确不做的清单）

| 不该建 | 理由（有一手依据） |
| --- | --- |
| **图存储 / 关系图引擎** | mem0 OSS 已移除 `enable_graph`/`graph_store`，graph memory 变成 Platform 特性（[迁移指南](https://github.com/mem0ai/mem0/blob/main/docs/migration/oss-v2-to-v3.mdx)）；Graphiti/Cognee 的一等公民图模型是它们各自的定位，不是通用必需。Thinkback D4 已定，**守住** |
| **自己的向量融合打分** | mem0 的 `score_and_rank` 已做 semantic+bm25+entity 归一化（[scoring.py](https://github.com/mem0ai/mem0/blob/main/mem0/utils/scoring.py)）。Thinkback 只做**候选集构建**（因为 mem0 语义门对中文不适用，ADR-0004）与**预算裁剪**，不再叠一层归一化权重 |
| **自己的 BM25 召回实现** | mem0 2.2.1 已接 BM25 打分（`text` + `sparse` 字段 + sparse index）；Thinkback 只负责**建表时把中文分析器配对**（ADR-0007）。一旦自己实现 BM25 召回，就是重复造轮子且要维护中文分词、稀疏向量、索引一致性三摊事 |
| **agent 自主改记忆的工具**（`core_memory_replace` 式） | Letta 的路线要求 agent 具备 replace/rethink 的精确编辑能力与 prompt 重建，Thinkback 是服务端策略驱动，不走这条路 |
| **多语言/多 provider 适配层** | mem0 已有 25+ 向量库配置、多个 LLM/embedder provider 配置；Thinkback 固定 Mem0 + Milvus，不扩 |
| **本地反思 worker（ReflectionExecutor 式）** | LangMem 的本地线程模式被其官方标注为 serverless 会终止（[delayed_processing](https://langchain-ai.github.io/langmem/guides/delayed_processing/)），LangMem #11/#70 是其代价。Thinkback 的后台作业在 PG 上有持久状态 |
| **Webhooks（若无明确外部需求）** | mem0 Platform 有（[webhooks.mdx](https://github.com/mem0ai/mem0/blob/main/docs/platform/features/webhooks.mdx)），但那依赖 Platform 的事件总线。Thinkback 已有自己的事件账本（L1 journal）+ 轮询 `GET /v2/memory/tasks/{task_id}`，加 webhook 是新运维面 |
| **自己实现 MCP server 协议栈** | 用 FastMCP / 官方 SDK；Cognee 已迁到 `fastmcp.FastMCP` 独立实例（[cognee-mcp README](https://github.com/topoteretes/cognee/blob/main/cognee-mcp/README.md) 明确说 `mcp` CLI 不认这个形态）。Thinkback 用同样的成熟栈 |
| **自建 "memory benchmark" 排行榜** | 主流的评测套件（LoCoMo / LongMemEval / BEAM）已经是社区共识，借用即可，不要另立门户 |

---

## 6. 本次未能核实的事项（unverified）

| 项 | 为什么没核实 | 影响 |
| --- | --- | --- |
| mem0 Platform 的 `list_events` / `get_event_status` 语义细节（事件类型、投递保证、重试策略） | 公开页面只有工具名，没有完整 payload/投递契约；且 Platform 无源码 | 影响"Thinkback 若要做 webhook/事件推送该对齐什么"的判断深度 |
| Zep Cloud 的 ABAC / 保留策略 / Context Block 的完整实现 | 托管产品，无源码，本次只引文档与本仓库 10-28 报告的结论 | 影响 Thinkback 若做"企业治理层"时的功能对标完整度 |
| A-MEM 及其他论文对 OSS 设计的具体影响 | 本次未做论文溯源 | 影响"论文系设计主张"的完整性，不影响上面五问的结论 |
| 各项目的实际性能/延迟 | 没有统一运行环境；官方 benchmark 多为托管服务结果（mem0 README 自述"Scores reflect Mem0's managed platform"） | 本报告不做性能排名 |
| mem0 `memory_type` 在最新版的完整取值集合 | 只确认了 `PROCEDURAL` 被显式处理、其它值抛 `Mem0ValidationError`（本仓库 10-28 报告），本次未重新核对 | 影响 Thinkback 类型注册表"能映射到引擎多少类型"的精度 |
| Letta 当前主线（`letta-ai/letta-code`）的 memory 工具完整清单 | 本次只读了 `letta-ai/letta` 的 `archive` 分支（V1 server 的 core/archival/recall 三类记忆），`letta-code` 的 MemFS/Agent SDK 只从本仓库 10-28 报告引用 | 影响 Letta 档案中"当前主线"一节的精度 |
| Agent Plugins v1.1.0 的**客户端实际实现覆盖面** | 规范站列了兼容客户端，但本次未逐个验证各家客户端的落地程度；TSC 成员含 Amazon/Cursor/Microsoft/OpenAI/Vercel 是 agent-plugins.org 站点的自述 | 影响"Agent Plugins 是否已是事实标准"的判断强度 |
| Supermemory 完整的数据模型（文档/块/自定义 schema 的字段定义） | 只确认了 MCP 层的 memory/profile/namespace 形状，未读其内部存储模型 | 影响对"超能力型商业产品"的架构对标深度 |
| Cognee 的 `improve` / session→graph 桥接的完整语义 | 只读到工具名与 README 段落 | 影响对"会话经验如何沉淀"的机制理解 |

---

## 附：核心一手来源清单

- mem0 源码：[main.py](https://github.com/mem0ai/mem0/blob/main/mem0/memory/main.py)、[scoring.py](https://github.com/mem0ai/mem0/blob/main/mem0/utils/scoring.py)、[storage.py](https://github.com/mem0ai/mem0/blob/main/mem0/memory/storage.py)、[base config](https://github.com/mem0ai/mem0/blob/main/mem0/configs/base.py)
- mem0 server：[main.py](https://github.com/mem0ai/mem0/blob/main/server/main.py)、[api_keys.py](https://github.com/mem0ai/mem0/blob/main/server/routers/api_keys.py)、[requests.py](https://github.com/mem0ai/mem0/blob/main/server/routers/requests.py)
- mem0 插件内核：[agent-plugin-core/README.md](https://github.com/mem0ai/mem0/blob/main/integrations/agent-plugin-core/README.md)、[mcp_server.py](https://github.com/mem0ai/mem0/blob/main/integrations/agent-plugin-core/python/mcp_server.py)、[memory_core.py](https://github.com/mem0ai/mem0/blob/main/integrations/agent-plugin-core/python/memory_core.py)、[conformance/run.py](https://github.com/mem0ai/mem0/blob/main/integrations/agent-plugin-core/conformance/run.py)
- mem0 文档：[mem0-mcp.mdx](https://github.com/mem0ai/mem0/blob/main/docs/platform/mem0-mcp.mdx)、[oss-v2-to-v3](https://github.com/mem0ai/mem0/blob/main/docs/migration/oss-v2-to-v3.mdx)、[custom-categories](https://github.com/mem0ai/mem0/blob/main/docs/platform/features/custom-categories.mdx)、[entity-scoped-memory](https://github.com/mem0ai/mem0/blob/main/docs/platform/features/entity-scoped-memory.mdx)、[memory-decay](https://github.com/mem0ai/mem0/blob/main/docs/platform/features/memory-decay.mdx)、[graph-memory](https://github.com/mem0ai/mem0/blob/main/docs/platform/features/graph-memory.mdx)
- Agent Plugins：[spec 1.1.0](https://github.com/agentplugins/agent-plugins-spec/blob/main/spec/1.1.0.md)、[plugin.schema.json](https://github.com/agentplugins/agent-plugins-spec/blob/main/schemas/1.1.0/plugin.schema.json)、[mcp.schema.json](https://github.com/agentplugins/agent-plugins-spec/blob/main/schemas/1.1.0/mcp.schema.json)、[agent-plugins.org](https://agent-plugins.org/)
- MCP 规范：[2026-07-28 tools](https://modelcontextprotocol.io/specification/2026-07-28/server/tools)、[resources](https://modelcontextprotocol.io/specification/2026-07-28/server/resources)、[prompts](https://modelcontextprotocol.io/specification/2026-07-28/server/prompts)、[versioning](https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning)、[tasks ext](https://modelcontextprotocol.io/extensions/tasks/overview)
- Graphiti：[README](https://github.com/getzep/graphiti/blob/main/README.md)、[edges.py](https://github.com/getzep/graphiti/blob/main/graphiti_core/edges.py)、[mcp_server/README.md](https://github.com/getzep/graphiti/blob/main/mcp_server/README.md)、[graphiti_mcp_server.py](https://github.com/getzep/graphiti/blob/main/mcp_server/src/graphiti_mcp_server.py)
- Cognee：[README](https://github.com/topoteretes/cognee/blob/main/README.md)、[cognee-mcp/README.md](https://github.com/topoteretes/cognee/blob/main/cognee-mcp/README.md)、[tool_registry.py](https://github.com/topoteretes/cognee/blob/main/cognee-mcp/src/tool_registry.py)、[server.py](https://github.com/topoteretes/cognee/blob/main/cognee-mcp/src/server.py)
- LangMem：[README](https://github.com/langchain-ai/langmem/blob/main/README.md)、[knowledge/tools.py](https://github.com/langchain-ai/langmem/blob/main/src/langmem/knowledge/tools.py)、[knowledge/extraction.py](https://github.com/langchain-ai/langmem/blob/main/src/langmem/knowledge/extraction.py)、[reflection.py](https://github.com/langchain-ai/langmem/blob/main/src/langmem/reflection.py)、[background_quickstart.md](https://github.com/langchain-ai/langmem/blob/main/docs/docs/background_quickstart.md)
- Letta：[archive/function_sets/base.py](https://github.com/letta-ai/letta/blob/archive/letta/functions/function_sets/base.py)、[archive/schemas/tool.py](https://github.com/letta-ai/letta/blob/archive/letta/schemas/tool.py)
- Supermemory：[README](https://github.com/supermemoryai/supermemory/blob/main/README.md)、[apps/mcp/src/server/tools/](https://github.com/supermemoryai/supermemory/tree/main/apps/mcp/src/server/tools)、[annotations.ts](https://github.com/supermemoryai/supermemory/blob/main/apps/mcp/src/server/tools/annotations.ts)、[self-hosting docs](https://supermemory.ai/docs/self-hosting/overview)
- OpenMemory（归档）：[openmemory-archive/README.md](https://github.com/mem0ai/openmemory/blob/main/openmemory-archive/README.md)、[api/app/mcp_server.py](https://github.com/mem0ai/openmemory/blob/main/openmemory-archive/api/app/mcp_server.py)
- 评测：[mem0ai/memory-benchmarks](https://github.com/mem0ai/memory-benchmarks)
