# Thinkback 深度分析：与主流 memory 项目的对照、设计漏洞、严重 bug、插件化差距与生产准入

日期：2026-10-29 · 代码基线：`refactor/memory-architecture-rewrite@71d8f1a`（分析时）→ 整改落在同分支 8 个提交（见 §14） · 引擎：mem0ai 2.2.1（当前 PyPI 最新）

方法：源码通读 + 确定性复现脚本 + 现有门禁实测 + 主流项目一手资料。所有"已复现"的结论都附了可重跑的脚本与观察输出。

---

## 0. 结论

**方向没有跑偏，引擎不该换。"插件化"原本只是一份策略配置、不是一个可插拔的记忆系统；本轮整改把它补成了可接入的形态。分析阶段列出的 3 个发布阻断项现已全部关闭，另有 9 个已复现的严重 bug 与 6 条新审计发现一并修复。**

| 维度 | 判断（分析时） | 判断（整改后） |
| --- | --- | --- |
| 与主流设计的偏离 | **无实质偏离**。三层模型（近期原文 / 会话视图 / 派生长期事实）与 Zep-Graphiti 的 episode+derived、LangMem 的类型化记忆同向；"引擎固定为 mem0"是当前 OSS 生态里的合理选择（2.2.1 就是最新发布版，无版本脱节） | 不变 |
| 技术与主流脱节 | **服务层不脱节，接入层脱节**。脱节的是**没有任何标准接入协议**（无 MCP、无生成式 SDK） | **接入层已补上 MCP**（薄适配层，直调 `MemoryService`，不重写业务逻辑）；生成式 SDK 仍未做 |
| 设计思路的严重漏洞 | **4 个**。最重的一个是：冲突/覆盖语义绑死在硬编码的"消费级助手中文槽位"词表上，对所有企业垂类类型**完全失效** | **最重的一个已修**：冲突键改取 per-type 抽取指令产出的属性名（类型 + 属性名）。另 3 个（两套抽取真相源、3 个空配置字段、抽取指令链路空转）分别按"记为已知边界""删掉空旋钮""补上指令文件"处置 |
| 严重 bug | **9 个已复现并已修**（另有 2 个留作决策/下一批）。最重的一个：mem0 引擎历史表结构漂移时，物理擦除**静默跳过却仍报 completed** | 全部关闭；另修 6 条第二轮审计发现（写闸门误杀/漏放、先删后擦留原文、过期记录擦不掉、ALL 范围并发写、mem0 写并发、召回预算兜底） |
| 插件形态 | 骨架已成立，但**3 个配置字段是空配置**、**抽取逻辑不可插拔**、**profile 烤进镜像**、**没有 MCP** | **MCP 已上线、3 个空配置字段已删、per-type 抽取指令已启用**；仍差：profile 运行时写入（要 admin 端点 + 目录监听）、生成式 SDK、Agent Plugins 打包格式 |
| 测试 | 单测 1030 全绿、行覆盖 91%，资产不弱；但**变异测试的预算花错了地方**，**真实栈门禁不在 CI** | 1078 条全绿；**接入契约矩阵已变成可执行测试**（`test_integration_contract.py` + `test_mcp_server.py`）；变异预算错配与真实栈不进 CI 两条仍未处理 |
| git 追踪 | 仓库本已干净；清掉 9 张零引用截图（13.0MB → 8.0MB） | 已落地（`git rm --cached` + `.gitignore`） |

最小整改顺序与最终处置见 §9、§14。**不建议为此换引擎、引入图数据库或做进程内 SDK。**

---

## 1. 与主流 memory 项目的对照

### 1.1 逐项对照

| 维度 | 主流做法 | Thinkback 现状 | 判断 |
| --- | --- | --- | --- |
| 分层记忆 | Zep 保留 episode 与派生事实；LangMem 区分 collection/profile/episodic/procedural | L1 轮次原文 / L2 会话视图 / L3 派生事实 | ✅ 同向 |
| 长期记忆引擎 | mem0 / Zep / cognee 各自承担抽取与检索 | 固定 mem0，Thinkback 只做治理索引 | ✅ 合理 |
| 抽取质量归属 | 主流都交给引擎 + 领域 prompt | mem0 `add(prompt=...)` + Thinkback 拼全局/类型两段 | ✅ 方向对 |
| 冲突与"当前事实" | mem0 OSS 2.2.1 的**写入路径**是 ADD-only（`ADDITIVE_EXTRACTION_PROMPT` 原文："Your sole operation is ADD"）。它另有 `DEFAULT_UPDATE_MEMORY_PROMPT`（ADD/UPDATE/DELETE/NONE）走的是 update 路径，Thinkback 不用那条 | Thinkback 自建 supersede | ✅ **该自建**，但实现绑错了词表，见 §3.1 |
| 时间语义 | Graphiti 区分事实时间/记录时间/参考时间 | `valid_at` / `recorded_at` / `expires_at` 三元组 | ✅ 同向（尚未做 as-of 查询） |
| 删除 | 主流普遍只做逻辑删除，物理残留是普遍痛点 | suppress / erase 四档对外声明 | ✅ 超出主流 |
| 持久执行 | mem0 `Memory.add` 是同步方法，无任务语义；`AsyncMemory` 非持久队列 | operation + lease + attempt + 恢复 + 对账 | ✅ 超出主流 |
| 多租户 | 主流靠 filters + 应用侧约定 | namespace 作业务分组 + 字符集校验 + 单副本边界 | ⚠️ namespace **不提供安全隔离**（`CONTEXT.md` 自己写明），跨企业要一部署一份 |
| 接入协议 | 主流 2026 收敛到 **MCP**（mem0 自己的 OpenMemory/MCPO、Supermemory 都是 MCP server） | 分析时只有 HTTP + gRPC | ❌ 脱节 → **本轮已补 MCP**（`thinkback-mcp`，stdio / streamable-http） |
| SDK | 主流给生成式 SDK / 契约版本 | `docs/SDK.md` 是手写 httpx 示例 | ❌ 缺 |

### 1.2 该自建的（mem0 做不了，Thinkback 必须做）：现状评价

| 能力 | mem0 OSS 2.2.1 现状 | Thinkback 现状 | 评价 |
| --- | --- | --- | --- |
| 事实抽取 | 有（写入路径 ADD-only） | 复用 | ✅ 没重复造 |
| 混合检索 BM25 | 有，但**集合 schema 不带 analyzer_params 时中文 BM25 恒为 0** | Thinkback 预建集合（ADR-0007） | ✅ 补的是引擎的缺口 |
| 实体 NER | 需 `mem0ai[nlp]` extras，检索侧固定 `en_core_web_sm` | 明确不装，词表只做抽取提示 | ✅ 判断正确 |
| as-of 查询 | `timestamp` / `reference_date` 是 Platform-only | 只做失效标记 | ✅ 判断正确 |
| 过期 | `expiration_date` + `show_expired`（**只隐藏不删**） | 保留期三段：不可达 → 宽限 → 物理清理 | ✅ 补的是引擎的缺口 |
| 删除时清历史 | 无（`delete` 只删向量行，history/messages 表留旧值） | 定向删 mem0 SQLite | ✅ 补的是引擎的缺口，见 §4.1 的实现缺陷 |
| 冲突/覆盖 | 写入路径无（ADD-only） | 自建 supersede | ✅ 方向对，实现错（§3.1） |
| 队列/重试/崩溃恢复 | 无 | operation + CAS + 恢复 | ✅ 必须自建 |
| 上下文装配/预算 | 无（`search` 只回 top-k） | 分层预算 + 类型上限 + 质量/强度排序 | ✅ 必须自建 |
| 身份/信任/分类 | 无 | 类型 `source_roles` + 服务端推导 trust + 分类取严 | ✅ 必须自建 |

**结论：引擎优先的原则本身没有被违反，Thinkback 补的绝大多数确实是 mem0 做不了的事。** 真正越界的只有一处，§3.2 的第二套抽取真相源。

---

## 2. 技术栈是否与主流脱节

不脱节，而且有几处比主流更靠前：

- **mem0ai 2.2.1 就是 PyPI 最新发布版**（2.0.4 → 2.2.1），没有落后一个季度以上的版本债。
- **Milvus + BM25 中文分词**这条路线，主流确实在用（mem0 自己的 Milvus provider + BM25），Thinkback 补了 mem0 缺失的 `analyzer_params` 预建。这属于补引擎缺口，不是另造检索管线。
- **PostgreSQL 存 L1/L2 + 治理索引、Milvus 存 L3**：与"向量库做语义、关系库做事实与账本"的主流分工一致。
- **FastAPI + gRPC 双面**：主流 memory 服务多走 gRPC/HTTP，Thinkback 两面都有，gRPC 还同步升了 v2 契约（proto 里 `SourceEvent` / `AppendEvent` 与 HTTP 同形），这点比主流多数项目做得好。
- **OTel / Prometheus / /health/ready / /metrics / 优雅关闭 / 迁移带 advisory lock**：都是生产化的标配，主流 OSS 项目大多没有。

唯一脱节处：**接入协议层**。2026 年"任何企业 AI 助手接入"的事实标准是 MCP，主流记忆项目几乎都提供 MCP server（mem0 hosted 11 个工具、Supermemory 16 个、Graphiti 13 个，Cognee 克制到只 pin 3 个），Thinkback 没有。**这直接命中你说的"可以被任何企业 AI 助手接入"这个目标。**

对照报告（`docs/report/2026-10-29-mainstream-memory-oss-comparison.md`，全部一手来源）里有几条**加强**本报告结论的事实：

| 主流事实 | 对 Thinkback 的含义 |
| --- | --- |
| 7 个项目全部要求每次写入携带至少一个归属维度，没有一家允许"匿名记忆" | `namespace` + `user_id` 已符合 |
| "namespace ≠ 权限"是硬共识（Graphiti 官方明说 `group_id` 只是命名空间） | D10「内网免鉴权」在共识上合规。**但**定位变成"任何企业助手可挂载的插件"之后，网络就不再是信任边界，这是定位变化带来的新义务 |
| "LLM 在写入侧，不在读取侧"是共识 | ADR-0003「引擎融合分 + 服务层显式信号重排」合规 |
| "读取侧必须有 token 预算与装配层"是共识（没有一家引擎直接返回最终 context） | ContextPackage / ContextAssembler 是对的 |
| "异步长任务必须可查状态"是共识，但实现普遍很差 | Thinkback 的三层 `WriteReceipt.layers.{journal,context_view,facts}.state` **比主流 OSS 更强** |
| **幂等是全行业空白**（mem0/Graphiti/Letta/Cognee 都没有，Supermemory 有 open issue 提案） | `event_id` 幂等锚点是差异化点 |
| **删除可核对是全行业空白**（mem0 的 `delete` 不清 history/messages，`AsyncMemory.delete_all` 逐条收集错误只记日志仍返回成功） | 四档擦除 + 逐项复核是最强差异化点，也正是本次 bug 集中的地方 |

### 2.1 正在滑向"重复造轮子"的两处（对照报告 §B 的结论）

对照报告逐项判完 mem0「已做 / 做不了 / 做得很差」之后，结论是：**Thinkback 没有明显重复造轮子，但有两处正在滑过去**。这两处正好落在你「最大化发挥记忆引擎能力」的指令上：

| 滑向的位置 | 现状 | 边界应该划在哪 |
| --- | --- | --- |
| **自造召回融合分** | ADR-0003 说"引擎分数只在同一次查询内可比，服务层叠加显式信号重排"——方向是对的 | 服务层只做**保留/丢弃**（预算裁剪、类型上限），**不要把分数重新归一化成自己的**。mem0 的 `score_and_rank` 已经把 semantic + BM25 + entity 归一化过一次，再乘一次权得到的分数既不可解释，也无法与 mem0 对账 |
| **自建混合检索** | mem0 2.2.1 的 BM25 **只对 semantic 候选重排、不扩召回**（它自己的语义门会把中文低分候选挡掉）。ADR-0004 已经把语义门关掉（threshold=0）由服务侧判分——方向对 | 边界是：**候选集构建归 Thinkback**（因为 mem0 的语义门对中文不适用），**候选集内排序归 mem0**。哪天 Thinkback 自己做了 BM25 召回扩展，那就是重复造轮子了 |

### 2.2 差异化故事该怎么说

对照报告给的措辞可以直接用：**auditable erasure + recall governance + idempotent writes**（可审计的擦除 + 召回治理 + 幂等写入），不是"又一个向量库"。

支撑它的公开 issue（都在对照报告里有链接）：

- mem0 `delete_all()` 把作用域内的原始消息留在磁盘上并**再喂给 LLM**（[mem0ai/mem0#7452](https://github.com/mem0ai/mem0/issues/7452)）
- LangMem 写入失败仍报成功（[#196](https://github.com/langchain-ai/langmem/issues/196)）、删除会泄漏（[#202](https://github.com/langchain-ai/langmem/issues/202)）
- Supermemory 完全没有幂等键（[#1627](https://github.com/supermemoryai/supermemory/issues/1627)）
- Graphiti 的 Neo4j 路径在按 UUID 寻址的读/删上忽略 `group_id`（[#2020](https://github.com/getzep/graphiti/issues/2020)）
- Supermemory 有一个**正好在要 Thinkback 已经建好的那层治理**（PII 脱敏、投毒防御、检索审计链）的 open request（[#1348](https://github.com/supermemoryai/supermemory/issues/1348)）

---

## 3. 设计思路的严重漏洞

### 3.1 【最重】冲突/覆盖语义绑死在"消费级助手中文槽位"词表上，对企业垂类完全失效

`CONTEXT.md` 把"冲突策略"写成"同类事实出现矛盾时谁作数：覆盖、追加、并存"，类型注册表也让每个类型声明 `conflict: overwrite|append|none`。但运行时的冲突键来自 `domain/slots/canonical.py::memory_conflict_slot()`，它只认 9 个硬编码的中文消费级槽位：昵称 / 宠物名 / 现居地 / 工作状态 / 沟通偏好 / 生日 / 喜欢的饮品 / 喜欢的食物 / 睡眠提醒。

**企业垂类事实一律落在这些槽位之外 → `slot is None` → `_conflicting_active_memories()` 直接返回空 → supersede 永远不发生。**

复现（`overwrite` 语义下用户把工艺节点从 3nm 改成 5nm）：

```
A) overwrite 垂直类型 —— 活跃事实条数: 2 ['用户负责 3nm 工艺整合。', '用户负责 5nm 工艺整合。']
B) overwrite P0 槽位类型 —— 活跃事实条数: 3 [..., 'User lives in 上海']   # 旧的杭州已被覆盖
```

同一个 `conflict: overwrite` 声明，在 P0 词表上生效，在垂直类型上是空配置。

**影响**：垂类场景下"新的覆盖旧的"这条基本承诺不成立。旧值与新值同时 ACTIVE、同时被召回，模型会同时看到"3nm"和"5nm"，这比重复更严重（旧值污染回答）。召回期的兜底 `_resolve_slot_conflicts()` 用的是同一套 P0 词表，所以召回期也救不了。

**现有测试为什么没抓到**：`tests/unit/test_memory_type_conflict_policy.py` 的用例全部用 `"我现在住在杭州"` / `"我现在住在上海"`，都是 P0 槽位词汇。它测的是"类型声明的 conflict 有没有被读"，没测"conflict 对这个类型有没有意义"。

**这为什么重要**：插件模型的核心承诺就是"配了类型就有对应语义"。现在配出来的语义对真正的企业类型是空的。

**修法（最小，不引入新抽象）**：把冲突键从"代码里的 P0 词表"换成"**由类型自己的抽取指令产出的属性名**"。三处现成的抓手都已经有了：

1. `MemoryTypeSpec` 已有 `extraction_prompt`（按类型分文件的抽取指令，已实现、已版本化、已随写入传给 mem0 的 `add(prompt=)`），只要让它要求每条事实写成 `<属性名>：<值>`（例：`工艺节点：3nm`）；
2. 冲突键从事实文本取第一个冒号之前的部分（一个全局约定，不需要给每个类型配正则；类型不遵守约定时退化为"整条文本"，即当前行为，向后兼容）；
3. `_conflicting_active_memories()` 的比较从 `memory_conflict_slot(text)` 换成 `同 memory_type 且同属性名`。

这样"领域知识不写成代码"（设计文档 §4.4 的原话）就真正贯彻了：`domain/slots/` 那 1476 行可以随之删除。如果某个类型的属性名形态复杂，再给类型配置加一个 `conflict_key` 正则作为可选覆盖。**先不加，等真的遇到再加**。

有一个反直觉的点：不能把冲突键换成"向量语义最近"。**纠正是语义不相似的**（"负责 3nm" 与 "负责 5nm" 向量距离很远）。键表达的是"同一个属性"，值才是变量。所以键必须来自类型配置或模型输出，不能来自相似度。


### 3.2 【次重】两套抽取真相源，且 Thinkback 会改写引擎输出

一次 append 里同时发生三件事：

1. mem0 用 LLM 抽取事实（`add(prompt=...)`）；
2. Thinkback 用正则回填 P0 槽位事实（`_backfill_p0_slots_from_round_source`，**每次 append 无条件跑**）；
3. Thinkback 用 canonical 化结果**回写 mem0 的记忆文本**（`_BackendUpdate`）。

第 3 条的实际效果是：P0 事实会被 Thinkback 的模板改写成英文，`"我现在住在杭州"` → `"User lives in 杭州"`。而 mem0 的抽取指令明确要求"跟随对话语言（中文对话 → 中文记忆）"。**两套规范互相打架。** 垂类事实因为不在 P0 词表里反而躲过了改写，所以垂类评测（40 条语料 40/40）根本照不到这个矛盾。

第 2 条对垂类是空转（正则不命中），但它对所有 append 都跑一遍。

**影响**：不会立刻出线上事故，但这是"引擎优先"原则被侵蚀的入口。引擎产出被服务层的第二套规则覆盖，而第二套规则只对一种垂直场景（消费级助手）有效。

**修法**：删掉 `_backfill_p0_slots_from_round_source` 的无条件调用（保留为 `profile` 类型的内置实现），删掉 P0 canonical 的回写路径（canonical 只用于本地索引展示，不回写引擎）。

### 3.3 类型注册表里 3 个字段是"空配置"

设计文档自己在 `MemoryTypeSpec` 的注释里写过一条原则："没有行为消费者的配置项只会给出错误预期（以为配了词表就会影响抽取），所以一律不留。" 但现在有 3 个字段违反了它：

| 字段 | 声明的语义 | 运行时消费者 |
| --- | --- | --- |
| `recall.path` | `fact` 进事实池 / `instruction` 进提示词装配 / `schedule` 走独立到期通道 | **零**（全仓库只有 `memory_types.py` 自己引用） |
| `layer` | L1 / L2 / L3 | **零**（类型级 `spec.layer` 无消费者；`item.layer` 是另一回事） |
| `extractor` | `mem0` / `profile` / `none` | `none` 有消费者（不抽取）；**`mem0` 与 `profile` 行为完全相同**（docstring 自己承认） |

`profiles/semiconductor.json` 里 6 个类型全部写 `extractor: mem0`，`recall.path` 全部写 `fact`，写不写都一样。

### 3.4 已经建好但没人用的"每类型抽取指令"

`extraction_prompt` 字段、profile 相对路径加载、`build_extraction_prompt()` 拼接、版本号写进记忆记录，**整条链路是通的**（`service.py:1630` → `mem0_library.py` 的 `prompt=`）。

但：`profiles/` 目录下**只有 `semiconductor.json`，没有 `prompts/` 子目录**，profile 里也没有一个类型引用 `extraction_prompt`。**垂类抽取质量目前完全靠那份全局默认指令撑着。**

这是本次分析里**性价比最高的一条**：垂类质量提升的主要杠杆（每个类型一份领域抽取指令）已经铺好路，缺的只是配置。而它恰好也是 §3.1 冲突键改造需要的载体。

---

## 4. 严重 bug（已复现，第一轮）

本节是本次分析直接发现的 3 个；第二轮对抗式猎捕又找出 9 个，见 §10。

### 4.1 【高】mem0 引擎历史表结构漂移 → 物理擦除静默跳过，却仍报 completed

**证据**（`/tmp/repro_purge.py`，可直接重跑）：

```
purge_history 返回: {'history': 0, 'messages': 1, 'supported': True}
残留 history 行: 1
```

把 mem0 的 `history` 表改名（模拟引擎升级漂移）后：

- 旧实现：`if memory_ids and "history" in tables:` → 表不在就**跳过**，`counts["history"] = 0`，`supported` 仍是 `True`；
- 服务层 `service.py:2456` 把这个计数原样存进 task result，**不校验**，然后 `mark_done("history")` → 任务标 `completed`；
- 结果：**旧事实原文还躺在 mem0 的 history SQLite 里，操作却报"已擦除"**。设计文档 §7 明写的契约是"表结构或列名不匹配时 fail-visible 并让操作失败，不允许静默跳过"，实现与契约相反。

**放大条件**：`pyproject.toml` 写的是 `mem0ai>=2.2.1,<3`，是**区间**不是精确锁定。任何 2.x 小版本都可能改表名。而擦除是"物理擦除"这一档对外承诺的唯一实现路径。

**已修**：`mem0_library.py::purge_history` 现在在表缺失 / 列漂移时抛 `RuntimeError`，服务层的 erase 路径已有 `except → mark_failed → raise`，所以任务会失败重试而不是谎报完成。补了两条回归测试（表改名、列改名）。

**仍然没修的部分**：`purge_history` 直接操作 mem0 的**私有** SQLite 表（`history` / `messages` 的表名与列名），并且 `_mem0_session_scope()` 复刻了 mem0 私有的 `_build_session_scope`。这是全仓库对第三方内部实现耦合最深的一处。建议把 `mem0ai` 从区间改成精确锁定 `==2.2.1`（设计文档 D11 本来就说"固定 mem0 2.2.1"，是 `pyproject.toml` 没跟上）。

### 4.2 【高】L1 缓存的**键**空间无上界 → 长跑服务内存泄漏

`memory/repositories/_l1_cache.py` 的值有上界（每键 10 条轮次），**键没有**：每个 `(user, namespace, scope, session)` 组合一个键，会话只增不减。TTL 是惰性的，只在 `get_l1` 命中时判断，不再访问的键永远不过期。

设计文档 §5.3 已经把它列为"待补：补 LRU 上界（键无界）"，一直没做。

**已修**：加了 `L1_CACHE_MAX_KEYS = 1000` 的 FIFO 淘汰（`dict` 保序，够用，不引 OrderedDict）。补了两条回归测试（键数上界、时间戳簿记同步淘汰）。

### 4.3 【中】mem0 适配器对引擎内部状态的全局 monkey-patch（不改：不是 bug，是长期要付的税）

`mem0_library.py::_mem0_inline_threadpool()` 在 `_call` 期间**全局替换** `mem0.memory.main.concurrent`。这是进程级状态：

- 深度计数保证嵌套时不互相覆盖，但只要**任何**一个线程在 `_call` 里，全局的 mem0 并发执行器就是内联版，包括那些没走 `_call`、直接调 mem0 的路径；
- 多副本部署时每个 Pod 各改各的，无冲突，但同 Pod 内任何绕过 `_call` 的第三方调用会被意外串行化。

同时 `_scope_kwargs` / `_limit_kwargs` 用 `inspect.signature` 反射适配 mem0 的参数形状（`limit` vs `top_k`、`user_id`+`agent_id` vs `filters`）。**mem0 一次签名调整，这两处会静默走错分支**，而且没有版本断言。

这是"不切换引擎"这条路线上必须长期付的税。它不是 bug，但发布说明里要写明"引擎版本是硬依赖"。

---

## 5. 插件形态：骨架成立，还差什么

### 5.1 已经成立的（实测验证）

用真实的 v2 HTTP 契约 + in-memory 仓储跑通了完整链路：

```
append  -> 200 {'status': 'completed', 'task_id': 'memory-extract:ev-1', ...}
recall  -> 200 {'items': [{'layer': 'L3', 'content': '我负责 3nm 工艺整合…',
                           'metadata': {'memory_type': 'process_preference', …}}, {'layer': 'L1', …}]}
items   -> 200 [{'memory_type': null, 'memory_type_name': 'process_preference', …}]
```

- **v2 事件契约**：`SourceEvent`（`event_id` 既是幂等锚点也是唯一 ID，`role` 放开到 user/assistant/tool/system，`occurred_at` / `provenance` / `subject_ref` 齐备），一次 append 一条事件；
- **11 个 v2 端点**：events / recall / items / items/{id} / update / delete / suppress / erase / tasks/{id} / l3-status，另有 13 个 admin 与 health 端点；
- **OpenAPI 3.1** 可直接生成 SDK；**gRPC 同步升 v2**，proto 与 HTTP 同形；
- **类型注册表**：严格校验（未知字段拒绝）、按 namespace 装载、准入/TTL/分类/审批/类型级预算都真的有运行时消费者；
- **namespace 隔离**：本次实测 `alpha` / `beta` 同 user 同 session，L1/L2/L3 读写互不可见（上一轮审计的 #1 必修项已闭环）；
- **幂等**：`(scope, key, method, path, body_hash)` 五元组 + 24h TTL + LRU 上界 + 读路径不进缓存。

### 5.2 还差的（按对"任何企业助手接入"的影响排序）

| # | 缺口 | 现状 | 最小补法 |
| --- | --- | --- | --- |
| 1 | **没有 MCP** | ✅ **已补**（`src/thinkback/mcp/`）：tools = `append_memory` / `recall_memories` / `list_memories` / `delete_memory` / `get_operation`，每个带 MCP `annotations`；**不重写业务逻辑**，直调 `MemoryService`；namespace 由服务端固定，不作为工具参数暴露。stdio 与 streamable-http 两种传输，http 可选 Bearer token。详见 [docs/MCP.md](../MCP.md) |
| 2 | **抽取逻辑不可插拔** | `extractor` 只有 3 个值，`mem0` 与 `profile` 行为相同 | 新类型目前只能用现有 LLM 抽取。若要"不同的类型用不同的抽取法"（正则/规则/第三方 NER），需要真正的 extractor 注册表。**先别做**，等 §3.1 的 per-type prompt 落地并证明不够用再说 |
| 3 | **profile 烤进镜像** | `Dockerfile` 里 `COPY profiles/`，`k8s/configmap.yaml` 没有 profile；新增一个 namespace 要重建镜像 + rollout | 加一个 admin 端点写 profile + 一个目录监听重载（`load_registry` 已经是按 namespace 懒加载的，只差"能写"和"能重载"）。**这是"配置即可接入"与"改代码/改镜像才能接入"的分界线** |
| 3b | **插件打包格式没有标准** | 只有服务本体 | 打包层已有开放标准：Agent Plugins v1.1.0（`plugin.json` + `mcp.json` + `skills/<name>/SKILL.md`）。mem0 官方已经实现了一份可抄的：`integrations/agent-plugin-core/`（6 个 skill + 一致性验证框架）。Thinkback 要"做成插件形态"，照这个形状做即可，不必自创 |
| 4 | **没有生成式 SDK** | `docs/SDK.md` 是手写 httpx 示例 | 从已有 OpenAPI 生成 Python/TS 客户端，带版本、重试、兼容策略 |
| 5 | **namespace 不是安全边界** | `CONTEXT.md` 明说"不提供安全隔离"；业务接口免鉴权 | 跨企业共用一套部署前必须补租户鉴权；或者明确"一企业一部署"并写进发布说明 |
| 6 | **没有公开的来源取证端点** | 设计文档 §9 列了 `GET /v2/memory/sources/{ref}`，实际只有 admin 的 `/admin/api/memories/{id}/source` | 业务面补一个读来源的端点（企业审计场景会要） |
| 7 | **`recall.path` / `layer` 是空配置** | ✅ **已删**：三个没人读的旋钮（`layer` / `recall.path` / `extractor` 的 `profile` 值）从类型 schema 里移除。**留着一个不生效的旋钮比没有更危险** |

---

## 6. 测试现状与生产标准的差距

### 6.1 现状（本次实测）

| 项 | 结果 |
| --- | --- |
| 单元测试 | **1030 全绿**（本次运行 exit 0） |
| 行覆盖率 | **91%**（7681 语句 / 701 未覆盖）；`service.py` 90%、`mem0_library.py` 87% |
| 变异审计 | **98 存活 / 0 无测试**，全部已验收；本次新增代码的变异体被新增的 5 条用例杀掉 |
| 自包含评测 | 20/20 + 垂类 16/16 |
| 真实栈门禁 | `make gate-real` 九类检查（**本地必跑，不进 CI**） |
| CI | ruff / mypy / pytest / fuzz / pip-audit / mutation+eval / web(build+unit+e2e+npm audit) / docker build |
| 负载 | k6 冒烟，`checks: rate==1.0` 已进阈值 |

比上一轮审计（2026-10-28）进步明显：**CI 口径已经和 `make check` 对齐**，namespace 隔离、分类不可降级、gRPC ERASE、Dockerfile 打包 profile、kustomization、k6 正确性阈值都补上了。

**发布边界仍须写进说明**：`infra/replica_guard.py` 会在"宣称多副本但前置没齐"时启动期硬失败，因为幂等缓存与限流桶仍在进程内（`src/thinkback/api/idempotency.py:128`、`api/ratelimit.py`），所以**当前只支持单副本**。这不是 bug，是被显式守住的边界，但对外承诺 HA 之前必须先把幂等与限流迁到 PG。

### 6.2 差距一：变异测试的预算花错了地方

`pyproject.toml [tool.mutmut]` 只变异 **67 个源文件里的 4 个**：

```toml
only_mutate = [
    "src/thinkback/domain/keys.py",          # 90 行
    "src/thinkback/domain/summarization.py",  # 101 行
    "src/thinkback/memory/decay.py",           # 155 行
    "src/thinkback/memory/repositories/_l1_cache.py",
]
```

- **注释与事实不符**：`pyproject.toml:107-108` 写着"service.py（1500+ 行）全量变异留给 CI 夜间任务"，但 CI 夜间跑的是 `make check` → `mutation-audit` → 同一个窄范围。**`service.py`（5798 行，所有并发/擦除/幂等/预算逻辑都在里面）从来没有被变异过。**
- **98 个存活变异体里绝大多数是低价值项**：`decay.py` 里 90%+ 是"日志文案未被断言""日志绑定参数未被断言""只断言抛 ValueError，未断言异常文案"。这些不是行为风险。换句话说，变异门禁在保护一个 155 行文件里的日志字符串，同时放过整个编排核心。

**最小修法（不要扩到全量 service.py，那是几小时的变异跑批）**：

1. 先把注释改成事实；
2. 把 `decay.py` 里纯日志文案类的变异体归为"刻意不钉死"整类登记（现在是一条条登记，噪音大）；
3. **把变异预算挪到 `service.py` 的决策点上**。做法是抽出 3~5 个纯决策函数（冲突裁决、预算分配、分类取严、擦除阶段判定）单独变异，不做全文件变异。这几个函数抽出来本身就是降复杂度；
4. 或者换个更划算的门禁：`service.py` 上做**属性测试 / 状态机测试**（并发交错、失败注入），比变异更适合编排层。

### 6.3 差距二：最高风险面没有 CI 证据

真实栈（mem0 + Milvus + PG）承载的是：**抽取质量、BM25 是否真生效、擦除后各资源是否真的不可见、幂等重放一致**。这些**只在本地 `make gate-real` 跑**，CI 里没有。

而 D9（引擎优先）之下，"语义由 Mem0 承担，Mem0 的契约与质量就是系统质量"，那么**mem0 的版本升级就是一次生产风险**，而今天没有任何自动化能在 PR 上告诉你是"mem0 2.x 变了，BM25 中文命中从 1 掉到 0"。

**最小修法**：CI 夜间用 `docker compose` 起 Milvus + PG + 一个假 LLM/embedding endpoint，把 `make gate-real` 的**结构与断言**接进去（真实 LLM 那部分仍留本地，因为需要凭证与成本）。至少做到：**集合 schema、BM25 中英文命中、filters 生效、erase 后不可见**这四类在每次 mem0 版本变化时能自动报警。

### 6.4 差距三：插件契约缺矩阵测试

上一轮审计的"协议契约矩阵"（自定义类型与 metadata 不丢失、HTTP/gRPC 五操作对跑、namespace 读写删矩阵）目前是**人工清单**，不是测试。要让"任何企业助手接入"这句话成立，需要一份**可执行的接入契约测试**：

| 矩阵 | 断言 |
| --- | --- |
| 自定义类型 | append → items → get → update → recall → erase 全链路，`memory_type_name` 不丢、不是 `null` |
| 事件模型 | user / assistant / tool / system 四种角色都能写；assistant 不成为用户事实 |
| metadata | 自定义业务属性进后端 metadata 与索引，不被丢 |
| namespace | 同 user/session、不同 namespace 的读写删全矩阵，互不可见 |
| HTTP ↔ gRPC | 同一组操作两面对跑，语义与字段一致 |
| 幂等 | 同 anchor 重放一致；同 key 不同 body → 409 |
| 擦除 | PG / Milvus / mem0 history / mem0 messages / L1 缓存 / 审计正文 六处扫描 |

---

## 7. git 追踪清理

**仓库本已干净**（逐项核实）：`coverage.xml` / `.coverage*` / `.env` / `node_modules/` / `mutants/` / `.testmondata*` / `.idea/` / `.claude/` 等全部已在 `.gitignore` 且未被跟踪；`git ls-files -i -c --exclude-standard` 为空（没有"被跟踪但被忽略"的文件）；`.env` 未跟踪，只有 `.env.example` 入库。

**本次做的唯一改动**：9 张**全仓库零引用**、且由 `web/take-screenshots*.mjs` 可再生的截图，取消跟踪并加进 `.gitignore`：

```
assets/screenshots/01-overview-full.png        assets/screenshots/05-audit-full.png
assets/screenshots/02-memories-full.png        assets/screenshots/06-config-full.png
assets/screenshots/03-tasks-full.png           assets/screenshots/07-integration-keys.png
assets/screenshots/04-govern-full.png          assets/screenshots/07-integration-prompts.png
                                                assets/screenshots/07-integration-prompts-detail.png
```

跟踪体积 **13032 KB → 7972 KB（-39%）**。README 引用的 10 张变体全部保留。

**建议但未做**（需要你拍板）：`docs/report/` 下 21 份一次性审计报告（含 2026-09 的中文命名旧报告）目前全部入库。它们记录了决策过程，价值在于"为什么"，但作为历史快照更适合归档而非留在主干。

---

## 8. 与你几条明确要求的对照

| 你的要求 | 结论 |
| --- | --- |
| 不切换记忆引擎，引擎保持 mem0 | ✅ **同意，且有依据**：mem0ai 2.2.1 就是 PyPI 最新发布版，没有版本债；mem0 OSS 明确不做持久执行/幂等/namespace/信任分类/删除审计/上下文装配/预算，这些正是 Thinkback 的价值所在。**不建议换。** 唯一要做的是把 `mem0ai>=2.2.1,<3` 改成精确锁定 `==2.2.1`，因为擦除路径依赖 mem0 私有表结构 |
| 记忆引擎能做的不要重复造轮子 | ⚠️ **大部分守住了，但有 3 处越界**：① P0 槽位正则回填 + canonical 回写引擎输出（§3.2）；② 冲突键绑死 P0 词表导致垂类空配置（§3.1）；③ `domain/slots/` 1476 行（占 src 的 6.7%）本该在中文 BM25 上线后按 D14 下线。**前提条件（中文 BM25 生效）已经满足，但下线没做** |
| 做记忆引擎做不了的事 | ✅ 这正是当前主体结构。做得到、且该保留 |
| 插件形态，可被任何企业 AI 助手接入 | ⚠️ 骨架成立，**接入协议缺 MCP** 是最大缺口；profile 不能运行时新增是第二个缺口 |
| 通过配置写入需要记忆的各类信息 | ⚠️ 类型注册表能配"准入/TTL/分类/冲突声明/预算"，但**冲突语义对垂类无效**、**抽取指令链路建好却没人用**、3 个字段是空配置 |
| 灵活性、扩展性 | ⚠️ 扩"类型"可以（配置）；扩"抽取逻辑"不行（`extractor` 无差异）；扩"传输"要改代码（MCP 未做） |
| 全面彻底的测试，达到生产标准 | ⚠️ 单测与覆盖率扎实；**变异预算错配**、**真实栈门禁不在 CI**、**接入契约矩阵是人工清单不是测试** |
| 脱离不需要的 git 追踪 | ✅ 仓库本已干净；本次清掉 9 张零引用截图，体积 -39% |
| 不要过度设计 | ✅ 本文的整改清单刻意保持最小：3 个发布阻断项 + 5 个插件缺口 + 3 个测试缺口，没有引入新框架、新存储、新队列 |

---

## 9. 最小整改顺序

### 本次已改动的文件（按主题拆成 8 个提交，见 §14）

```
M  .gitignore                                        +5   截图产物不再跟踪
D  assets/screenshots/{9 张零引用截图}                     跟踪体积 -39%
M  src/thinkback/memory/backends/mem0_library.py     +161  擦除 fail-visible + 分块删 + delete_all 返回 ID + get + 过期可见 + 写串行
M  src/thinkback/memory/repositories/_l1_cache.py     +26  键空间上界 + FIFO 淘汰
M  src/thinkback/memory/service.py                   +304  擦除覆盖暗数据与先删后擦 + update 重放比对 + 原文匹配 + 冲突键 + TTL
M  src/thinkback/domain/recall_policy.py             +42  空层补一条（不返回空包）
M  src/thinkback/memory/l2_refresh.py                +15  失败也算已尝试（无重试风暴）
M  src/thinkback/api/idempotency.py                  +49  幂等按调用方分域
M  src/thinkback/domain/ports.py                     +19  delete_all 返回 ID 契约 + get
M  src/thinkback/memory/backends/fake.py             +11  同步上面两条契约
M  src/thinkback/domain/memory_types.py              +61  属性名冲突键 + 删 3 个空配置字段
M  src/thinkback/domain/safety.py                    +33  写闸门收窄误杀 + 空格变体不再漏放
M  src/thinkback/mcp/**                             新增  MCP 接入层（5 个工具）
M  profiles/semiconductor.json + profiles/prompts/  +89  per-type 抽取指令
M  tests/unit/…                           +700 -2  回归测试（含接入契约矩阵）
```

门禁实测（整改后）：`make test` EXIT=0（1078 条）· `make mutation-audit` EXIT=0（98 存活全部已验收，无新增）· `make eval` EXIT=0（20/20 + 40/40）· `ruff format --check` 绿 · `ruff check` 绿 · `mypy src` 绿（70 文件）。

### P0：发布阻断（3 项，已全部关闭）

1. **✅ mem0 历史擦除 fail-visible**：表缺失/列漂移 → `RuntimeError` → 擦除任务失败重试，不再谎报 completed。附两条回归测试。
   *配套*：`mem0ai>=2.2.1,<3` → `==2.2.1`。
2. **✅ L1 缓存键空间上界**：`L1_CACHE_MAX_KEYS = 1000` FIFO 淘汰 + 时间戳簿记同步清理。附三条回归测试。
3. **✅ 冲突键从 P0 词表搬到"类型自己的抽取指令"**（§3.1）：per-type 抽取指令规定事实写成 `<属性名>：<值>`，冲突键 = 类型 + 属性名；`_conflicting_active_memories()` / `_supersede_conflicting_memories()` / `_is_older_than_active_conflicting_memory()` 统一按新口径比较。**企业垂类的 `conflict` 现在真的有意义。**（`domain/slots/` 仍在，见 §14 备注）

### P1：插件形态（配置优先，代码最少）

4. **✅ 启用已建好的 per-type 抽取指令**：`profiles/prompts/semiconductor/*.v1.md`，profile 里用 `extraction_prompt` 引用。零代码。
5. **⬜ profile 运行时管理**（未做）：admin 端点写 profile + 目录监听重载（`load_registry` 已按 namespace 懒加载，只差写与重载）。做完这一步，"新企业接入 = 一份 JSON"。
6. **✅ MCP server**：`src/thinkback/mcp/`，tools = append / recall / list / delete / get_operation，namespace 服务端固定。详见 [docs/MCP.md](../MCP.md)。
7. **✅ 删掉 3 个空配置字段**（`recall.path` / `layer` / `extractor` 的 `profile` 值）。

### P1'：第二轮修掉的（对抗式 bug 猎捕，详见 §10）

11. **擦除覆盖暗数据**：`delete_all` 返回实际删掉的 ID 列表，引擎历史按这些 ID 清（含本地无索引行的暗数据）；SQLite `IN (...)` 分块。
12. **幂等按调用方分域**：域取请求体的 `namespace` + `user_id`，不再用直连 IP。
13. **召回不返回空包**：层配额装不下任何一条时用全局余额补一条。
14. **update 幂等重放**：按原始类型名比对，自定义类型不再误报 409。
15. **L2 失败不刷风暴**：失败也算"已尝试"，重试节奏回到去抖间隔。
16. **擦除复核逐个点查**：后端记录超过一页时不再永久失败（后端协议新增 `get`）。

### P2：测试（生产准入的真正短板）

17. **⬜ 变异范围修正**（未做）：注释改成事实；日志文案类整类登记；把预算挪到 `service.py` 抽出的 3~5 个纯决策函数上，或改用属性/状态机测试覆盖编排层。
18. **⬜ 真实栈门禁进 CI 夜间**（未做）：compose 起 Milvus + PG + 假模型端点，至少自动守住"集合 schema / BM25 中英文命中 / filters 生效 / erase 后不可见"四类。真实 LLM 那部分仍留本地。
19. **✅ 接入契约矩阵变成可执行测试**：`tests/unit/test_integration_contract.py`（自定义类型全链路、事件模型、metadata、namespace 矩阵、HTTP↔gRPC 同形、幂等、擦除回执）+ `tests/unit/test_mcp_server.py`。

---

## 10. 第二轮：对抗式 bug 猎捕的结果与处置

`docs/report/2026-10-29-thinkback-adversarial-bug-hunt.md` 用仓库自带 fake 与确定性复现脚本又找出 9 个缺陷（每条都有可重跑的脚本，落在 `/tmp/tbhunt/`）。我逐条重跑了复现脚本并核对根因，**7 条当场修掉，2 条留作设计决策**。

| 编号 | 严重度 | 问题 | 处置 |
| --- | --- | --- | --- |
| BUG-1 | high | `erase(scope=ALL)` 只按本地索引清引擎历史，暗数据（后端有、本地无索引行）的原文留在 mem0 history.db，审计却写 `mem0_history_purged: complete` | ✅ 已修：`delete_all` 改为返回**实际删掉的 ID 列表**（协议、mem0 适配器、fake 同步改），擦除把这些 ID 一并交给 `purge_history`；SQLite 的 `IN (...)` 按 500 一块分批（一条语句拼不下几千个占位符） |
| BUG-2 | high | 删/抑制单条记忆会把**整轮**原始对话从 L1 召回里抹掉（同轮兄弟事实仍 ACTIVE） | ⚠️ **不自动改**：L1 是轮次粒度的，删一条事实没法只抹掉轮次里的一部分文本。这条要么"整轮不召回"（当前），要么"轮次保留但被删事实的措辞可能仍在文本里"，是隐私与上下文完整性的取舍。见 §11 |
| BUG-3 | medium-high | 幂等缓存按"直连对端 IP"分域 → k8s/ingress 部署下所有调用方同域，一个客户端用过 key `k` 后别的客户端用 `k` 直接 409，一把 key 堵死别人的写路径 | ✅ 已修：域改成**调用方身份**（请求体里的 `namespace` + `user_id`），取不到才退回对端 IP |
| BUG-4 | medium-high | 层配额可能小于该层任何一条的成本 → 整层被清空，召回返回**空包**且预算一分未用 | ✅ 已修：空层用全局余额补一条（保底顺序 L1→L2→L3）。契约本来就写着"不返回空包" |
| BUG-5 | medium | 自定义类型的 update 幂等重放被误判成 `operation_id conflict`（应为 `already_done`），对外 409 | ✅ 已修：任务结果记**原始类型名**（`memory_type_name`），重放比对也用原始类型名 |
| BUG-6 | medium | LLM 摘要失败后每个 append 都重发一次刷新（docstring 承诺"无重试风暴"） | ✅ 已修：失败也算"已尝试"，重试节奏回到去抖间隔 |
| BUG-7 | medium | 后端记录 ≥ 4096 条时 `erase(scope=MEMORY/SESSION)` **永久失败**（复核只看一页，列表取满就判"无法证明已删"，重试撞同一条件） | ✅ 已修：复核改成**逐个点查目标 ID**（后端协议新增 `get`），不再把"页取满"当成残留 |
| BUG-8 | low | 引擎侧原始消息按去空格后的正文删，mem0 存的是原文 → 带首尾空白的消息删不掉 | ✅ 已修（防御性）：按原文匹配。注：公开 API 路径上 `MemoryMessage` 已经 strip 过内容，所以这条走不到，属于"实现与意图对齐" |
| BUG-9 | low | 重放（重试/崩溃恢复/rebuild）把 TTL 记忆的到期时刻从"写入时间+TTL"推成"重放时间+TTL" | ✅ 已修：到期日随轮次落进 journal 的请求元数据，重放时复用同一个值（`_index_lifecycle` 先读持久化的 `expires_at`，读不到才按类型 TTL 重算） |

这批缺陷集中在**擦除**与**召回装配**两条线上，和上一轮审计指向同一处：Thinkback 的差异化（可核对的删除、逐层预算）也正是最容易说谎的地方。

## 11. 留给你的两个决策

**决策一（BUG-2）：删一条事实，要不要连整轮原始对话一起从召回里抹掉？**

- 现状：抹掉。排除集是轮次粒度的，删任何一条事实 → 整轮 `(session_id, round_id)` 进排除集 → 同轮的兄弟事实还 ACTIVE，但它们的原文从 L1 消失，召回可能返回空包。
- 选项 A（现状）：保持轮次粒度。**被删事实的措辞一定不进上下文**（隐私安全），代价是兄弟事实失去原文出处。
- 选项 B：改成事实粒度（只有当一轮里**再没有 ACTIVE 事实**时才排除整轮）。兄弟事实保住原文出处，代价是被删事实的措辞可能仍留在那一轮的 L1 文本与 L2 摘要里 —— 与 API 文档"`suppress`：把记忆移出召回、保留文本"其实是一致的，但与"删了就别再出现"的直觉相反。
- 选 B 的话还要顺带把 L2 摘要的输入过滤与 L1 分开：L2 是派生视图，被删事实不该出现在摘要里，而 L1 是原始证据，可以留。

我的倾向是 **A**，理由是 L1 是唯一原始证据层，"被删事实的原文还在"比"兄弟事实少了出处"更难向合规方解释。但这是产品口径，你定。

**决策二：BUG-9（重放延长 TTL）现在修还是下一批？**

修法是把原始到期时刻写进 journal 的请求元数据、重放时复用。改动面不大但碰契约（journal 元数据多一个字段），建议和"崩溃恢复保留类型/TTL"那一类一起做，别单开。

### 决策结果（用户已拍板）

| 决策 | 结论 | 落地 |
| --- | --- | --- |
| 冲突键粒度 | **按类型自己的抽取指令产出的属性名** | 冲突键 = 类型 + 属性名；`profiles/prompts/semiconductor/*.v1.md` 要求事实写成 `<属性名>：<值>` |
| BUG-2 删事实的粒度 | **保持现状：整轮不召回** | 未改代码，语义写进注释与本报告 |
| BUG-9（TTL 重放） | **这批一起修**，与「崩溃恢复保留类型/TTL」打包 | 到期日随轮次落库，重放复用；附 `test_ttl_expiry_survives_retry` 与 `test_rebuild_replay_does_not_extend_ttl` 两条回归 |
| 提交方式 | 分成几个提交落在当前分支 | 8 个主题提交，见 §14 |

---

## 12. 本次实测记录

| 检查 | 结果 |
| --- | --- |
| `pytest -q`（全量） | **全绿**，exit 0（整改后 1078 条；本轮新增回归 30+ 条） |
| `pytest --cov=thinkback` | 行覆盖 **91%**；`service.py` 90%、`mem0_library.py` 87%、`infra/milvus.py` 45%、`infra/database/migration.py` 32% |
| `ruff format --check .` / `ruff check .` / `mypy src` | 全绿（整改后：274 文件已格式化、70 源文件无类型问题） |
| `make mutation-audit` | **EXIT=0**；98 存活全部已验收，无新增存活 |
| `make eval` | **EXIT=0**；自包含 20/20，垂类 40/40 |
| namespace 隔离复现 | alpha/beta 读写互不可见 ✅（上一轮审计的必修项已闭环） |
| 分类降级 / gRPC ERASE / Dockerfile profiles / kustomization / k6 checks | 上一轮审计的必修项**均已修复** ✅ |
| v2 端到端（events → items → recall） | 200，类型与来源元数据完整 ✅ |
| `conflict: overwrite` 对垂类 | **空配置**（复现输出见 §3.1）❌ → **已修**：属性名即冲突键，垂类覆盖生效 ✅ |
| mem0 history 表改名 → 擦除 | **静默跳过、报 completed** ❌→✅ |
| L1 缓存键空间 | **无上界** ❌→✅ |
| mem0ai 最新版本 | PyPI **2.2.1**（无版本债） |
| 变异范围 | 67 个源文件里 **4 个**（6%）—— 仍未改（§9 第 17 项） |

## 13. 未证明的范围

- 未在本轮启动真实 Milvus / PG / LLM 集群重跑 `make gate-real`、真实擦除扫描、SIGKILL 恢复、多 Pod 与负载；真实栈证据仍是上一轮的。
- **本机 embedding 端点不可达**（`embedding.example.internal:7345`），垂类 `--real` 评测跑不了，只能跑 self-contained 口径（40/40）。
- 未验证当前 Milvus 集合在**本机**是否真的启用了 BM25 与 entity matching（只有设计文档记录的实测结论）。
- 未做其它记忆项目的同条件性能排名；主流对照只用于职责与设计校准，不用于性能结论。
- 未验证备份、日志归档、Milvus compaction 之后的物理字节擦除。

---

## 14. 整改落地清单

分支 `refactor/memory-architecture-rewrite`，8 个主题提交：

| # | 提交 | 内容 |
| --- | --- | --- |
| 1 | `chore(deps): mem0ai 锁死 2.2.1，引入 MCP SDK` | 擦除依赖 mem0 私有表结构，版本必须锁死 |
| 2 | `feat(mcp): 记忆能力投影成 MCP 工具，接入方零改造` | 5 个工具 + annotations；namespace 服务端固定 |
| 3 | `fix(safety): 写入闸门只拦真凭据与注入` | `system:`/`tool:` 收窄到行首；注入词表吃空格变体；补 `secret_key` |
| 4 | `feat(profile): 垂类类型带抽取指令，属性名即冲突键` | 4 份 per-type 指令；删 `recall.path` 空字段 |
| 5 | `fix(memory): 后端擦除契约 —— 返回被删 ID、点查单条、列出含过期记录` | `delete_all` 返回 ID；新增 `get`；`show_expired`；写操作按分片串行 |
| 6 | `fix(memory): 冲突键、TTL、擦除计划、召回预算与幂等分域` | 见 §9 的 P0/P1' 全部条目 |
| 7 | `chore: 截图产物脱离 git 追踪，文档同步` | 9 张零引用截图 `git rm --cached`；k8s 接入文档 |
| 8 | `docs: 深度分析报告` | 本文件与四份审计报告 |

### 第二轮审计（`docs/report/2026-10-29-thinkback-ponytail-audit-pass2.md`）处置

| 编号 | 严重度 | 问题 | 处置 |
| --- | --- | --- | --- |
| 1 | must fix | 先 suppress 再 erase：轮次已墓碑 → 擦除计划取不到轮次 → 引擎侧原始消息留着，接口报 completed | ✅ 已修：软删除墓碑里记着当时的轮次集合，擦除取回来按原文正文精确清 |
| 2 | must fix | `"our operating system: Ubuntu"` 整轮被拦（`system:\s` 任意位置匹配） | ✅ 已修：收窄成行首匹配 |
| 3 | must fix | 闸门文档说"保留 L1"，代码却把轮次做成软墓碑（不进召回） | ✅ 已修：文档改成「原文留在库里，但该轮不进召回」，与代码一致 |
| 4 | must fix | `"ignore␣␣all␣␣previous␣␣instructions"` 加两个空格就通过 | ✅ 已修：词表改 `\s+` |
| 5 | must fix | ALL 范围擦除把"擦除期间新写的记忆"当残留 → 永远失败，重试还把新写的删掉 | ✅ 已修：复核只看这次真删掉的那批 ID |
| 6 | must fix | 过了 TTL 的记忆 `get_all` 看不见 → 列不出、删不掉，擦除报"已完成" | ✅ 已修：列出时带 `show_expired` |
| 7 | must fix | mem0 的 `Memory` 不是线程安全的，并发写会丢 entity link | ✅ 已修：写/更新/删除按 (user, scope) 分片加锁 |
| 8 | should fix | fake 与真后端的删除语义不一致（`update` 对缺失 ID 静默 no-op、`delete_many` 返回删除数而非尝试数） | ✅ 已修：fake 的 `update` 对缺失 ID 抛异常，`delete_many` 返回尝试条数；契约统一（`delete_all` 返回 ID、`get` 点查） |
| 10 | should fix | 审计永远写"只清了一部分" | ✅ 已修：审计按实际清掉的行数判定 —— 清掉原始消息写 `complete`，一条没清写 `partial`，两者不再长得一样 |
| 11 | should fix | 擦除时我们开第二条 SQLite 连接删 `history.db`，与 mem0 自己的连接争锁 → 报 `database is locked`，擦除在负载下间歇失败 | ✅ 已修：锁冲突做有限重试（写事务通常只持锁几毫秒，busy_timeout 对"升级为排他锁"这种瞬时冲突不起作用）。更彻底的解法是给文件开 WAL，但那会改引擎存储格式，留给多副本那一批 |
| 12 | should fix | 衰减清扫与并发写交错，被压制的记忆可能复活 | ⬜ 未做（需要真实栈验证收益） |
| 15 | nice | 重试对账看不到已过期记忆 | ✅ 已修：对账走的是同一个 `get_all`，带上 `show_expired` 后过期记录也能被认出来 |
| 16 | nice | 并发限流把实体查找变成串行循环 | ⬜ 未做 |

### 备注

- `domain/slots/`（1476 行）**仍在**：本轮把冲突键换成了属性名口径，但旧槽位仍被 P0 回填与本地索引路径使用。彻底下线要先确认中文 BM25 上的召回效果，属于独立一批。
- §3.2 的"两套抽取真相源"（Thinkback 改写引擎输出）**未改**：属于已知边界，改动面涉及召回语义，本轮不动。

### 仍未修的（按影响排序，都不是本轮引入的）

| 来源 | 问题 | 影响 | 为什么这批不做 |
| --- | --- | --- | --- |
| ponytail-audit #8 | 每次 append/recall 都要读该用户**全部**已删除/被取代/被遗忘记忆的 `source_refs`，无上限、无窗口 | 召回热路径的耗时随服务运行月数单调增长，且不报错 | 正确修法是独立小表 + 索引（schema 变更）或"轮次过了保留窗口就清 source_refs"。前者是设计变更，后者要先确认被取代事实的复活窗口 —— 都不该顺手塞进这批 |
| ponytail-audit #9 | 后端调用卡死时，API 线程池的写槽位要等线程结束才归还；8 次卡死 → 写路径全 503 | 一次后端挂起能打掉整个写入面 | 需要给后端客户端加超时（跨 milvus / mem0 / embedding 三处），是独立一批 |
| ponytail-audit #12 | `MemoryService` 是 5798 行的单类，承担 8 类职责 | 可读性与测试隔离度 | 拆分类是纯重构、收益不体现在行为上；**测试才是**这里的抓手，已按主题拆出独立测试文件 |
| ponytail-audit #14/#15、pass2 #13/#16 | 无效校验器、无人调用的公开方法、幂等缓存每次全量扫描、并发限流把实体查找变串行 | 低 | 收益小，且部分（实体查找）因没装 spaCy extras 实际不触发 |
| §9 第 17 项 | 变异预算仍集中在 67 个源文件里的 4 个，`service.py` 从未被变异 | 变异门禁在保护日志字符串，放过编排核心 | 变异范围调整会让门禁跑批时间翻倍，需要单独排期 |
| §9 第 18 项 | 真实栈（mem0 + Milvus + PG）门禁只在本地 `make gate-real`，不在 CI | mem0 版本升级没有自动报警 | 需要 CI 夜间起 compose + 假模型端点，属于 CI 基建 |
| §9 第 5 项 | profile 不能运行时写入（要 admin 端点 + 目录监听） | "新企业接入 = 一份 JSON" 还没闭环 | 需要写接口与文件监听，是独立一批 |

**本轮没有引入任何新框架、新存储、新队列。**
