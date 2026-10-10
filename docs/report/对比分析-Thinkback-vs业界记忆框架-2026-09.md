# Thinkback vs 业界 Agent 记忆框架 —— 对比分析与改进路线图

| 元信息 | 值 |
| --- | --- |
| 数据时点 | 2026-09-23/24 全量调研 + 2026-09-24 补盲轮（中文生态/KV-参数记忆/基准专项/多模态/企业级 MaaS，见 §6）；GitHub star/活跃度经 API 实拉 |
| 用途 | 内部改进路线图（继续自研，不做迁移评估） |
| 对标口径 | 主流大头 + 新锐/学术，共 10 项 |
| 评估维度 | 时间与时序记忆、遗忘/衰减/冲突、评测体系为主；记忆架构为辅 |
| 判定标尺 | 可落地 ROI 优先；机制级（直接抄）与范式级（追/不追）分开判定 |
| 约束 | 确定性骨架不动（LLM 只做后台/离线增强）；季度规划可含 schema 改动 |
| 关联 | 取代 `docs/RESEARCH.md`（2026-09-07）的框架矩阵部分（其 mem0 版本差距结论已随升级解决，检索语义差距本文 G3 承接）；工程短板见 `docs/巡检报告-生产级与主流对齐-2026-09.md` |

---

## 0. 结论速览

1. **方向被验证**：Thinkback 的核心设计（双时态失效、supersede 不删历史、三层分层、生产化任务治理）与 2025-2026 业界收敛方向同频甚至领先；**不需要架构级返工**。
2. **差异化资产**：遗忘/衰减（MemoryDecaySweeper）在业界开源框架中是普遍空白，属 Thinkback 独有深度 —— 但默认关闭，等于把差异化资产锁在抽屉里。
3. **真实差距是机制级补课**，不是范式级追赶：召回排序没用在已落库的强化信号上、写入无节流（全量抽取）、L2 非画像化、评测判分过弱（substring）、缺空闲期整理。第一批 4 项全部小成本高 ROI。
4. **范式级决策**：temporal KG 维持不跟（已有设计覆盖其核心语义）；MCP/工具协议化挂牌观察；多 agent 共享记忆挂牌 + `memory_scope_id` 标注为预留演进点。

---

## 1. 业界框架总览（2026-09）

### 1.1 主流大头

| 项目 | Stars / 活跃 | 核心记忆模型 | 时间感知 | 评测主打 | 形态 |
|---|---|---|---|---|---|
| **mem0** | 65.7k，极活跃 | 事实级条目 + 实体链接；v3 单遍 **ADD-only 抽取**（只增不改），冲突靠检索期时间感知排序选版本 | 时间感知检索 | LoCoMo 92.5 / LongMemEval 94.4 / BEAM-1M 64.1（官方注明高分来自托管平台，OSS 结果方向一致但数值不同） | 库 + 自托管 + 云 |
| **graphiti/Zep** | 30.9k，活跃（社区版服务已弃维转云） | **双时态知识图谱**：episode(溯源)→entity→fact(带 valid 窗口)，增量构建不整图重算 | bi-temporal 核心卖点 | DMR 94.8% vs MemGPT 93.4%，延迟 -90% | 引擎库 + 云 |
| **cognee** | 30.2k，活跃 | KG + 向量混合（ECL 管线）；session 快缓存后台同步入图；1.0 全部跑在单 Postgres | 部分（图时序） | BEAM 100K 0.79 / 10M 0.67 | 库 + 云 + MCP |
| **letta (MemGPT)** | 24.3k，活跃 | 分层：**core memory blocks（常驻上下文，agent 自编辑）** + archival/recall；sleep-time agent 空闲期重写记忆 | 弱 | 常作 baseline | Agent 平台 |
| **supermemory** | 29k，活跃 | **InfoBio 单文档传记画像** + 混合检索，读时零成本 | 弱 | 无公开基准（主打 <400ms） | TS 全栈 API |

### 1.2 新锐 / 学术

| 项目 | Stars / 活跃 | 核心记忆模型 | 关键机制 |
|---|---|---|---|
| **MemOS** | 11.5k，增速最猛 | MemCube 统一**明文/激活(KV-cache)/参数**三态记忆；L1 trace/L2 policy/L3 world model | MemScheduler 异步调度；自然语言反馈纠错；LoCoMo 88.83 / LongMemEval 89.20 |
| **MemoBase** | 2.5k，放缓 | schema 化用户画像 + 事件时间线双轨 | 攒批 flush（token 阈值 OR 闲置超时 OR 会话结束）；固定 3 次 LLM 调用 |
| **LangMem** | 1.4k，低活跃 | 最系统类型学：semantic(episodic)/procedural；collection vs profile 双表征 | hot-path vs background 两种成形时机；**strength = recency×frequency 排序** |
| **A-MEM** | 1.1k，论文原型 | Zettelkasten 卡片盒：笔记+结构属性+自动链接 | **记忆演化**：新记忆入库反向更新邻接旧记忆元数据 |
| **LightMem** | ICLR'26，被引 141+ | Atkinson-Shiffrin 三阶段：感觉过滤→短时压缩→长时检索优化 | **写入前廉价过滤层**；~9% Mem0 token 开销达到更高精度 |

### 1.3 值得单独点名的新技术（与技术框架解耦）

1. 双时态事实失效（invalidation-only，历史不可变、当前可查询）
2. ADD-only 抽取 + 检索期时间感知排序（mem0 v3：写入不做冲突仲裁，检索选对的版本）
3. sleep-time compute / 后台离线整理（对话内不写重记忆，空闲期批量反思）
4. strength = recency × frequency 记忆强度排序（langmem）
5. 写入前"感觉过滤"层（LightMem：廉价判定"值得抽取吗"）
6. 画像单文档（InfoBio / profile schema）—— 读路径零检索成本
7. 记忆演化（A-MEM：supersede 时刷新关联记忆簇元数据）
8. 记忆状态机 + 异步调度（MemOS MemCube：生成→激活→固化→淘汰）
9. 评测军备竞赛：LoCoMo → LongMemEval / HaluMem / PersonaMem / BEAM(1M-10M) → OmniMemEval 横评；各家分数口径不可互比
10. 记忆下沉编码 Agent：MCP server / Claude Code 插件 / CLI 本地记忆（2026 新战场）
11. token 效率成为第一设计约束（"检索预算内做时间感知排序"取代 agentic 多轮检索）

---

## 2. 2025-2026 八条趋势 → Thinkback 映射

| 趋势 | 代表 | Thinkback 现状 |
|---|---|---|
| 1. 双时态失效取代覆盖删除 | graphiti、MemOS | ✅ 已有（valid_at/invalid_at + supersede + 墓碑），PG 索引层实现 |
| 2. OSS 核心 + 托管平台分层 | mem0/Zep/cognee | ➖ 不适用（内部服务） |
| 3. sleep-time 后台整理成标配 | letta、langmem、memobase、cognee | ◐ 部分：L2 去抖刷新（每 5 轮）+ decay 淘汰，无合并/整块重写 → **G5** |
| 4. 评测标准化与自研基准军备 | OmniMemEval、memory-benchmarks | ◐ 14 项指标 + 中文回放，但判分是 substring，无 judge → **第一批** |
| 5. 记忆下沉 CLI/MCP | MemOS、cognee、mem0 | ✖ 无 MCP（无上游消费者，挂牌观察） |
| 6. 多 agent 共享记忆 | MemOS、letta、langmem | ✖ 单 scope（挂牌，`memory_scope_id` 预留演进点） |
| 7. token 效率第一约束 | mem0 v3、LightMem、MemOS | ◐ 写入全量抽取无节流 → **G2** |
| 8. 类型学向认知结构靠拢；**遗忘/衰减仍是业界空白** | langmem、LightMem、SCM | ✅ decay sweeper 领先（默认关闭）—— **差异化机会** |

---

## 3. Thinkback 对比定位

### 3.1 领先 / 已同频（不需要动）

| 能力 | 业界对照 |
|---|---|
| 双时态 + supersede 不删历史 + 任意时点可查语义 | graphiti 同款语义，Thinkback 用槽位+PG 索引达成 |
| 墓碑删除屏障 + rebuild 防复活 | **业界无人有**（graphiti 仅 invalidation，无 delete-all 屏障/防漂移 rebuild） |
| 遗忘衰减（decay sweeper + 召回强化 touch） | 业界普遍空白；langmem 仅止于排序建议 |
| 任务治理（operation_id 幂等、row_version CAS、孤儿双层回收） | 超过全部 10 个对标项目 |
| 三层分层 + 槽位冲突 + 源文本交叉校验 | 与学界收敛点同频，槽位引擎比 mem0 抽取更可控 |
| 生产化（readiness/降级语义/双协议/压测故障注入脚本） | 强于多数研究型框架 |

### 3.2 机制级差距（actionable 部分）

| # | 差距 | 业界出处 | Thinkback 现状 | ROI / 成本 / 风险 |
|---|---|---|---|---|
| **G1** | 召回排序采用 strength（recency×frequency） | langmem | `touch_memory_recalled` 已落库 recall_count/last_recalled_at，**dedupe_and_clip 排序完全没用它** | 高 / 小 / 低 —— 数据已在，改排序函数 |
| **G2** | 写入前廉价过滤层 | LightMem | 每轮全量送 mem0 抽取；且白名单外记忆入库却不可召回（已知边界，巡检 §6.1） | 高（省钱+堵边界）/ 小-中 / 低 |
| **G3** | 召回时间感知 rerank + 多信号融合 | mem0 v3 | 检索被 mem0 封装，外层无排序控制 | 中 / 中 / 中 —— 路径见 §4 |
| **G4** | L2 摘要 → schema 化画像单文档 | supermemory InfoBio / memobase profile | L2 prompt 已有主题/进行中/偏好/状态雏形但输出自由文本 | 中-高 / 小 / 低 |
| **G5** | 空闲期离线整理器（L3 邻域合并 + L2 整块 rethink） | letta sleep-time / cognee `improve` | 只有去抖刷新与 decay 淘汰，无合并重写 | 中 / 中 / 中 |

---

## 4. 改进路线图

### 第一批（本季度，全部在确定性骨架内）

1. **G1 strength 排序**：`dedupe_and_clip` 的同层打分从「分高者胜」扩为 `score × f(recency, frequency)`（用本地索引 recall_count / last_recalled_at）；槽位/双时态优先级不变。验证：现有中文回放评测 + 新增 strength 单测。
2. **G2 写入过滤层**：append 进 L3 抽取前加确定性过滤（槽位命中 OR 轮次信息量阈值 OR 显式 metadata 标记），被过滤轮只落 L1/L2；与 `MEMORY_P0_SLOTS` 白名单联动，顺带消灭「入库不可召回」存量边界。验证：抽取调用量断言 + 成本对账。
3. **G4 L2 画像化**：L2 综合摘要 prompt 输出改为固定 schema（基本画像 / 进行中 / 偏好 / 状态四段），落 `ins_current_summary`，读路径不变；拼接降级版保持兼容。验证：LLM-as-judge 顺带评画像质量。
4. **LLM-as-judge 评测**：中文回放评测的 substring 断言升级为 judge 判分（离线跑、不进在线路径，符合 Q6 边界）；LOCOMO 类英文基准一次性校准定位，不进 CI。验证：判分一致性抽样人审。

### 第二批（下季度候选）

5. **G3 召回外层 rerank（路径 A）**：mem0 召回 top-N 候选后，Thinkback 用本地索引的 strength / valid_at 窗口 / 槽位信号做二段确定性排序 —— 不动 mem0 存储契约，不重建检索（路径 B 已否决）。
6. **G5 整理器（扩展 MemoryDecaySweeper）**：在既有调度骨架（1 小时门控、append 触发、槽位/墓碑保护）上加两个 action：同槽位/同源邻域合并、L2 整块 rethink（复用 L2 refresher 的 composer）。对外可暴露为 cognee `improve` 式一等 API。
7. **G6 L1 历史原文参与召回**（2026-09-24 补盲轮新增）：recall 时对 journal 历史原文做语义检索兜底 —— L3 抽取遗漏/被 G2 过滤的事实可从原文找回。与 G2 构成「原文必留、抽取可选」的完整对冲（原文派实证见 §6.1）。

**评测实现路径（第一批第 4 项细化，2026-09-24 确认）**：LLM-as-judge 基于 **mem0ai/memory-benchmarks**（Apache 2.0）复用实现 —— Ingest→Search→Evaluate 三段流水线跑 Thinkback vs mem0 同 judge 对照，不自建 judge 框架（与《轮子审计》复用约束一致）；评测维度加 **contradiction resolution**（业界全员最弱项、Thinkback 槽位 supersede 强项，复用 BEAM 口径）；LongMemEval-V2（长期运行任务，最佳系统仅 74.9%）与 MemoryAgentBench（Hindsight 有参考实现）列为第二批靶标。

### 范式级决策记录（只决策不排期）

| 项 | 决策 | 依据 |
|---|---|---|
| temporal KG | **维持不跟**（沿巡检报告 D1） | 槽位 + 双时态已覆盖 Latest-value 与历史可查语义；图增量收益在多跳关系推理，当前场景弱 |
| MCP / agent 工具协议化 | **挂牌观察** | 趋势热但无上游消费者；HTTP/gRPC 已可用，等真实消费者 |
| 多 agent 共享记忆 | **挂牌** | 取决产品多 agent 化时间表；`memory_scope_id` 在 ARCHITECTURE.md 标注为预留演进点 |
| coding-agent 记忆（hook 采集 + 跨 agent 共享） | **不做**（2026-09-24） | 该子市场规模大（agentmemory 28.8k★ 等）但 Thinkback 服务对话型产品，无真实消费者不建设；规模数据记录于 §6 供产品侧决策 |

---

## 5. 决策记录（16 项，2026-09-24 确认）

Q1 内部路线图 / Q2 大头+新锐 / Q3 时间·遗忘·评测主+架构辅 / Q4 报告+优先级路线图 / Q5 ROI 优先分机制·范式级 / Q6 确定性骨架（LLM 仅后台） / Q7 中文评测+judge 主、LOCOMO 校准 / Q8 季度规划 / Q9 第一批 G1+G2+G4+judge / Q10 G3 走外层 rerank / Q11 KG 不跟·MCP 观察·多 agent 挂牌 / Q12 G5 扩展 decay sweeper
Q13（补盲轮）G6 历史原文召回入第二批 / Q14 矛盾消解显式化（评测维度 + 差异化宣称）/ Q15 coding-agent 不做（记录市场供产品侧）/ Q16 评测复用 mem0ai/memory-benchmarks 路径确认

---

## 6. 补盲调研补充（2026-09-24 第二轮）

### 6.1 「全量原文派」登顶 —— 对抽取派路线的实证挑战

**MemPalace**（2026-04 发布，5 天 36K★，现 ~50K+）公开主张「不摘要、不抽取、不信任 LLM 决定该记什么」：全量 verbatim 存 ChromaDB + 语义检索，其 96.6% LongMemEval（raw 模式）被社区独立复现；**agentmemory**（28.8K★）同路线（hook 全量捕获 + working/episodic/semantic/procedural 四层整理 + 遗忘曲线 + 矛盾检测）。

**对 Thinkback 的启示**：L1 journal 本就是全量原文（架构天然对冲，rebuild 可重放），真正缺口是**历史原文不参与召回**（L1 只出最近 10 轮）→ 已立 G6（第二批）。抽取派 vs 原文派之争未定，Thinkback「原文必留 + 抽取增强」的双轨恰是两派之间的稳健位置。

**另一实证**：contradiction_resolution 是所有系统的最弱项（BEAM-1M 最强者仅 0.357），Thinkback 槽位 supersede 是该能力强项 → 已立 Q14 显式化度量。

### 6.2 补盲新入表项目（6 个万星级）

| 项目 | Stars/时点 | 核心机制 | 借鉴点 |
|---|---|---|---|
| MemPalace | ~50K+（2026-04 起） | 全量原文 + 语义检索（原文派旗舰） | §6.1；G6 |
| agentmemory | 28.8K | hook 静默全量捕获 + 四层整理 + 遗忘曲线 + 矛盾检测 | coding-agent 子市场形态（决策：不做） |
| vectorize-io/hindsight | 26.4K | 企业级记忆基础设施：60+ 框架集成、四语言 client、Helm/Grafana、system-evals | 集成矩阵完整度标尺；MemoryAgentBench 参考实现 |
| 字节 OpenViking | ~21.9K | context-as-filesystem，L0/L1/L2 分层按需加载降 token | 分层加载策略（观察） |
| 腾讯 TencentDB-Agent-Memory | ~22K（2026-08 v2.0） | 团队级记忆中枢；**换 base-URL 零代码接入**（LLM 代理拦截） | 代理式接入形态（观察） |
| EverOS | 13.2K | Markdown-native 本地优先自演化记忆，用户可控可迁移 | 观察 |

中文生态小项：memU（14.4K，1.0 已转三层架构，LoCoMo 92.09%，最直接同构对标）、SimpleMem（3.8K，全模态+语义无损压缩，多模态唯一规模化开源）。

### 6.3 矩阵修正（增量核对）

- **MemoBase 降级**：停更 ~8 个月（最后 push 2026-01-11），从对标矩阵移出主力、降为历史参照；其「攒批 flush + 画像增量合并」借鉴价值不变
- **LightMem 路径修正**：实际仓库 **zjunlp/LightMem**（1,176★，ICLR'26 已接收，持续更新）；衍生 LightMem-Ego（个人日常记忆版，93★）
- 其余 8 项（mem0/letta/graphiti/cognee/supermemory/MemOS/LangMem/A-MEM）无架构级变化；mem0 新增 memory-benchmarks 评测仓库（→ 已采纳为 Q16 复用路径）

### 6.4 其他类别结论

- **KV-cache/参数记忆层**：论文多工程少（MemoryLLM/M+ 学术标准实现、Titans 无官方代码、MemArt 论文），除 MemOS 外无生产级开源 —— 维持不跟进
- **多模态记忆**：SimpleMem 一家独大 + 论文层，工程化刚起步 —— 观察
- **企业级 MaaS**：Redis 官方 agent-memory-server（工作/长期记忆两级）、MongoDB+Memori、Oracle 原生记忆引擎 —— 厂商方案频出验证赛道，架构上「工作记忆+长期记忆」分离与 Thinkback L1/L2/L3 同构
