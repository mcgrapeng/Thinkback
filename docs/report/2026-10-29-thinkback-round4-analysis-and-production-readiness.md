# Thinkback 第四轮：与主流对照、设计漏洞、接入 Lumen、生产准入

日期：2026-10-29 · 基线：`refactor/memory-architecture-rewrite` @ `e40a0f1`（本轮改动未提交） · 引擎：mem0ai 2.2.1（`pyproject.toml` 锁 `==2.2.1`）

前序轮次：`2026-10-29-thinkback-deep-analysis.md`、`2026-10-29-thinkback-adversarial-bug-hunt.md`、
`2026-10-29-thinkback-integration-and-release-readiness.md`、`2026-10-29-thinkback-ponytail-audit{,-pass2}.md`。
本轮**不复述**前序结论，只写前序没覆盖到、或前序写错了的部分。

---

## 0. 结论

**方向没有跑偏，引擎不该换。** 三层模型（原始事件 / 上下文视图 / 派生事实）与 Zep-Graphiti 的
episode+derived、LangMem 的类型化记忆同向；身份维度（用户 + 命名空间）与 mem0 自己的
`user_id` + `agent_id` 一一对应，接入方不需要做任何引擎层翻译。

**本轮实际做了六件事：**

1. **找到并修复三个真实缺陷 + 一处潜伏不变量**（详见 §3）：
   - 预算装不下任何一条记忆时，召回静默返回空包且 `degraded=false`（high）
   - MCP 的 `append_memory` 声明 `idempotentHint=True` 却不幂等（high）
   - gRPC 未知 proto3 枚举值被报成 `INTERNAL` 而不是 `INVALID_ARGUMENT`（medium）
   - 引擎事件与本地索引的对齐：守住「要么进索引、要么不存在」这条不变量（**low / 潜伏**，
     当前引擎下不可达 —— 原因见 §3.1 的订正）
2. **修掉接入 Lumen 的一个硬阻断**：限流按对端 IP 分桶 → 所有租户共享一份额度，
   一个 pod 约 100 个活跃会话就被打满 429，而且被限流的不只是超载的那个租户
   （详见 §4.6）。已改为按调用方分桶，口径与幂等层一致。
3. **修掉变异门禁的结构性盲区**：补的测试对存活变异体不可见 —— 门禁永远显示旧的存活数，
   于是「补测试有没有真的补上」这件事没人能回答（详见 §5.3）。
4. **真实栈门禁本地跑通 9/9**（真实 mem0 + 真实 Milvus + 真实 PG + 真实 LLM/embedding 端点），
   报告见 `docs/memory/report/real/gate-1a5e2f7-20261029-054610.json`。
5. **给出接入 Lumen 的最小改动清单**（详见 §4），并把「租户鉴权」那条待拍板决策
   收敛成两个可选方案（§4.7）。
6. **订正了我自己这一轮的三处判断**（都写进了报告，不是悄悄改掉）：
   - §3.1 的前提错了：mem0 2.2.1 的写入路径只产 ADD 事件，
     「引擎已删而本地索引仍活跃」在当前引擎下不会发生 —— 严重度从 high 降到 low/潜伏；
   - 「fake 后端只产 ADD 所以引擎路径没被覆盖」这个批评本身是错的：fake 与引擎一致；
     真正成立的是「L3 后端没有契约套件」；
   - 前序报告里的**视觉回归**与**属性测试**两项**不是 PASS 是 PARTIAL**（§5）；
     覆盖率实测 90.5%，低于仓库自己写的 92% 水位。

**本轮结束时 `make check` 全绿（EXIT=0）**：format-check / lint / mypy / **单测全过** /
mutation-audit（存活 84，与基线一致）/ eval（自包含 40/40，垂类基线达标）。

**所有改动都做了「先红后绿」验证**（先回退实现 → 跑测试确认红 → 恢复 → 确认绿），
共 14 条新用例参与，其中 7 条在修复前红。

**还剩 4 个设计漏洞**（§2），其中 **漏洞 A 对企业接入是阻断级**：内置记忆类型声明的冲突策略
在企业事实面前**静默失效**，且没有任何日志或指标能让人发现。前序报告说「冲突键已修复」——
修复只覆盖了**自带抽取指令的类型**，内置类型（默认路径）仍然落在消费级中文槽位词表上。

---

## 1. 与主流的对照：Thinkback 站在哪

| 维度 | Thinkback 现状 | 主流做法 | 判断 |
| --- | --- | --- | --- |
| 分层 | L1 原始事件 / L2 上下文视图 / L3 派生事实 | Graphiti：episode + derived entity；Letta：archival + recall + core memory | **同向** |
| 身份维度 | `user_id` + `namespace` | mem0：`user_id` + `agent_id`；LangMem：namespace | **同向**（`namespace` → mem0 `agent_id`，`mem0_library.py:311`） |
| 事实抽取 | 交给引擎（`infer=True`，`service.py:4749`），Thinkback 只拼指令 | mem0 原生能力 | **未重复造轮子** |
| **冲突裁决** | **Thinkback 自己做**（类型 + 属性名的冲突键 → 覆盖/并存） | **mem0 2.2.1 的写入路径不裁决**：它用 `ADDITIVE_EXTRACTION_PROMPT`（`configs/prompts.py:468`，开宗明义 "sole operation is ADD"），`main.py` 里所有 `"event"` 字面量都是 `"ADD"`；UPDATE/DELETE 的判定机器（`get_update_memory_messages`）在 2.2.1 里**无人调用**（死代码） | **必须自己做** —— 引擎不提供。这把 §2 漏洞 A 从「策略配错」升级为「唯一的覆盖机制失效」 |
| 抽取指令 | 全局 + per-type 合并成一份传给引擎（ADR-0014） | mem0 每次写入可带 `custom_instructions` | **用足引擎能力** |
| 排序分 | 引擎分只作同一次查询内的相对排序键（ADR-0003） | mem0 / Zep 都把分数当相对量 | **未自建第二套评分** |
| 过期 | 引擎 `expiration_date` 隐藏 + 本地到期过滤 + 独立保留期作业（ADR-0010） | mem0 只做检索期隐藏 | **补引擎做不了的**（审计要区分抑制与物理删除） |
| 删除审计 | 四档对外声明 + 引擎侧 history/messages 定向清（ADR-0001） | mem0 公开 CRUD 不清 history | **补引擎做不了的** |
| 幂等 / 完成语义 | 服务层自己做（事件 ID 锚点、写回执分层） | mem0 OSS 不做 | **补引擎做不了的** |
| 上下文装配 | 服务层做（分段 + 预算 + 降级说明） | mem0 只返回检索结果 | **补引擎做不了的** |

**唯一与技术主流脱节的地方是接入层**，前序轮次已补 MCP（5 个工具：`append_memory` /
`recall_memories` / `list_memories` / `delete_memory` / `get_operation`，`mcp/server.py:62-196`）。
gRPC 面（`proto/memory.proto`）与 HTTP 面（`/v2/memory/*`）也都在。

**结论：不存在需要换引擎的理由，也不存在需要自建图数据库 / 自建向量检索的理由。**
Thinkback 现在做的事，恰好是 mem0 OSS 明确不做的那部分。

---

## 2. 设计思路的漏洞

### 漏洞 A（高，企业接入阻断级）：内置类型的冲突策略静默失效

**事实链**（逐行可查）：

- `_conflict_key()`（`service.py:5579`）分两路：有 per-type 抽取指令 → 取属性名；没有 → 回退
  **P0 槽位**。
- `_type_uses_attribute_keys()`（`service.py:5547`）的判据是「有没有 per-type 抽取指令」。
- 8 个内置类型（`domain/memory_types.py:143-225`）**全部 `extraction_prompt=""`**，即全部走回退路。
- P0 槽位是**消费级中文词表**（`domain/slots/__init__.py`：昵称 / 宠物 / 地点 / 工作状态 /
  沟通偏好 / 生日 / 饮食偏好 / 睡眠提醒）。

**后果**：企业助手用内置类型 `preference` 写入「用户的常用供应商是华为」——
`_memory_conflict_slot()` 返回 `None` → 冲突键为 `None` → `_conflicting_active_memories()`
直接返回空（`service.py:3892`）→ **`conflict="overwrite"` 这次声明从未生效**，
旧值不会被覆盖，新旧两条并存。

**为什么这是「静默」的**：`_conflict_key` 全程没有任何日志、指标或审计出口
（`grep _conflict_key` 只有 6 处调用，0 处 `logger`）。运维看到的现象是「记忆越攒越多、
改口了旧事实还在」，但系统不会告诉他「你这个类型的冲突策略是空配置」。

**为什么前序报告说「已修复」是误导**：修复（提交 `90eb060`）把冲突键从硬编码槽位改成
「per-type 抽取指令产出的属性名」，**但只对自带抽取指令的类型生效**。默认注册表里的类型
一条指令都没有 —— 也就是说**新接入方第一天拿到的就是失效路径**。

**而且没有任何一层兜底**：mem0 2.2.1 的写入路径只做 ADD、不裁决冲突（见 §1 与 §3.1 的订正），
所以**类型声明的冲突策略就是唯一的覆盖机制**。它算不出冲突键 = 覆盖机制不存在。

**最小修法（不做大重构）**：让「类型声明了冲突策略、但这条事实算不出冲突键」变成**可见**，
而不是靠读者自己推导。具体是在 `_conflict_key` 返回 `None` 且该类型的 `conflict != "none"`
时记一条 warning + 计数（指标口径复用 `api/metrics.py` 现有的 registry）。
**不建议**现在就给内置类型补一份通用抽取指令 —— 那会改变所有消费级场景的抽取形状，
需要重跑评测，属于另一个决定。**本轮未做这一项**：它会改抽取行为，属于策略决定而非缺陷修复，
留给拍板（见 §7）。

### 漏洞 B（中）：P0 槽位引擎 = 手写正则 NLP，与引擎语义层职责重叠

`domain/slots/` 是 8 个正则抽取器 + 槽位归类 + 来源交叉校验 + 召回回填，
职责是「mem0 抽出的记忆按槽位归类，用于冲突检测、来源交叉校验与召回回填」。
问题不是它存在，而是**它的词表是消费级中文**：对企业垂类事实几乎恒不命中，
于是它对企业的实际贡献接近于零，却仍在热路径上（`_index_l3_event_locked` 每次都调
`_memory_conflict_slot`）。

这正是用户「不要重复造轮子」担心的地方，但需要说清楚边界：
**冲突仲裁本身不是重复造轮子**（mem0 的 ADD/UPDATE/DELETE 判定是「事实级」的，
Thinkback 的冲突键是「属性级 + 类型策略」的，两者语义不同、都必要）。
重复的是**用正则硬编码一个消费级词表去猜属性名**这一层 —— 而这件事引擎的 LLM 已经做得更好。
**本轮未删**：删掉 P0 会改动召回行为与冲突语义，需要配套评测与迁移，不该在没有评测的
情况下动（上一轮的教训就是「回填不是纯增益」）。**记为待评估项**。

### 漏洞 C（中）：`CONTEXT.md` 对 namespace 的描述与实现不符

`CONTEXT.md` 写：「命名空间 (Namespace)：……**它不提供安全隔离，只防误用**」。

实现里 namespace 是**真实隔离维度**：
- L3：`_long_term_scope_id()`（`service.py:376`）直接返回请求里的 namespace，
  它就是 mem0 的 `agent_id`（`mem0_library.py:311`）与 `filters` 的一部分；
- L1/L2：`CurrentSummaryRecord` 的唯一约束是 `(user_id, namespace, memory_scope_id)`
  （`infra/database/models.py:62`），L1 查询按 namespace 过滤；
- 幂等域：`_caller_identity()`（`api/idempotency.py:33`）用 `namespace:user_id` 分域。

原话在**鉴权语义**上没错（Thinkback 不校验「你只能访问自己的 namespace」），
但在**数据分区语义**上会误导接入方 —— 接入方读到这句话，可能以为 namespace 不隔离数据，
于是不敢用它承载租户边界。**这是接入 Lumen 时最容易踩的一坑**（§4）。
**本轮未改**：`CONTEXT.md` 是词汇表，改它要连同 ADR 一起过（属于文档决定）。

### 漏洞 D（中）：业务面无鉴权，租户隔离靠约定

`api/app.py` 的注释写明「内网部署（方案二）：业务面免鉴权」。只有管理面有
`admin_token`（`api/admin.py:52`，fail-closed：token 未配置时**拒绝**，不是放行）。

这与主流一致 —— mem0 OSS 本身也没有租户概念，隔离由宿主应用负责。
但**对「任何企业 AI 助手接入」这个目标**，它要求接入方必须自己保证：
(a) 只有受信调用方能连到业务面；(b) 传入的身份元组不可被终端用户伪造。
§4 给出具体做法。**本轮未在 Thinkback 内建租户子系统** —— 那是过度设计，
mem0 也没有，宿主应用做隔离是行业惯例。

---

## 3. 本轮找到并修复的三个缺陷 + 一处潜伏不变量

### 3.1 引擎事件的本地索引对齐（**本节前提已订正**，见下）

> **订正**：本节最初写成「mem0 的 `DELETE` 事件被丢弃，high，引擎已删而本地索引仍活跃」。
> **那个前提是错的**，是另一路独立审计（提交 `1a5e2f7`）纠正后我复核 mem0 源码才发现的。
> 保留本节是因为**代码改动仍然有价值**，但严重度与理由都已改变。

**订正后的事实**（逐行核实 mem0 2.2.1 源码）：

| 事实 | 证据 |
| --- | --- |
| mem0 有**两套**抽取指令 | `configs/prompts.py`：`ADDITIVE_EXTRACTION_PROMPT`（`:468`）与 `DEFAULT_UPDATE_MEMORY_PROMPT`（`:176`） |
| **写入路径用的是 ADDITIVE**，其开宗明义「Your sole operation is ADD」 | `main.py:944`（同步）/ `:2627`（异步）：`system_prompt = ADDITIVE_EXTRACTION_PROMPT` |
| `main.py` 里**所有** `"event"` 字面量都是 `"ADD"` —— 没有任何一条代码路径产出 `"UPDATE"`/`"DELETE"` 的返回事件 | `grep '"event": "' main.py` → 8 处，全是 ADD |
| UPDATE/DELETE 的判定机器（`get_update_memory_messages`）**在 2.2.1 里是死代码** —— 全包无人调用 | `grep -rn get_update_memory_messages mem0/` → 只有定义处 |
| `_update_memory` / `_delete_memory` 只被 mem0 自己的 `update()` / `delete()` 方法调用，**add 路径不碰** | `main.py:1879/1896/1948`（同步）、`:3558/3575/3629`（异步），都在 update/delete 方法体内 |

**结论**：mem0 2.2.1 的 `add()` **只产 ADD 事件**，也不会在写入时改动已有记录。
所以「引擎已删、本地索引仍 ACTIVE」这个分叉**在当前引擎下不会发生** ——
我原来的复现脚本是**自己造了一个 mem0 不会产生的事件**。

**这条订正反而加重了 §2 漏洞 A**：引擎**不做**冲突裁决，那么同类型事实的覆盖/并存
**完全**由 Thinkback 的冲突键决定。而内置类型的冲突键在企业事实上算不出来
（回退到消费级中文槽位）→ `conflict="overwrite"` 从声明到生效之间，
**中间没有任何一层兜底**。

**仍然保留的代码改动及其正当理由**（与原判断不同）：

- 改动本身：`_index_l3_event` 现在接受 `DELETE` 事件并把对应本地索引行置为
  `DELETED`（保留墓碑可审计）。**没有新状态、没有新表、没有新接口**。
- 理由从「修一个正在发生的生产 bug」改成「**守住一条不变量**」：
  代码里自己写着「要么进索引、要么不存在」（`service.py:5207-5211`），
  而这条不变量原先只对 ADD 事件成立。`MemoryBackend` 是一个端口
  （`domain/ports.py`），将来换后端或引擎把 update 指令接回写入路径时，
  没有这层兜底就会静默分叉。
- **严重度：low（潜伏），不是 high。** 当前引擎下不可达。

**测试**（`tests/unit/test_l3_backend_event_sync.py`，4 条）：用一个会产出非 ADD 事件的
后端替身，把「本地索引与引擎事件保持一致」这条不变量钉死；
**已验证修复前 2 条红、修复后全绿**。

**顺带订正我自己的另一处说法**：我原先写「`FakeMemoryBackend` 永远只产 ADD，
所以引擎的 UPDATE/DELETE 路径没被覆盖过」—— **这个批评本身是错的**：
fake 的 ADD-only 恰好**与引擎一致**，不是保真度缺陷。
真正成立的是另一件事：**L3 后端没有契约套件**（fake 与 mem0 从未跑同一组用例），
见 §5 的第 2 条。

### 3.2 预算装不下任何一条记忆时，召回静默返回空包

**严重度**：high（可用性）

召回有「每层至少留一条」的兜底（`domain/recall_policy.py:305-345`）：
用全局余额给空层补一条。但**余额本身也可能不够** —— 契约允许的最小预算是
`token_budget=100`（`schemas.py:349`），而一条记忆的成本轻易超过它
（`estimate_tokens` 按字符估）。这时物理上装不下任何一条，召回只能返回空包。

问题不在于返回空包，而在于 `degraded=false`：**调用方无法区分「用户没有相关记忆」
与「我的预算太小」**。前者是正常结果，后者是配置错误 —— 两种反应（不再调召回 /
以为记忆没生效）都会让记忆白记。

**修法**（最小）：召回侧在裁剪后判断「有候选但一条都没装下」→ 记一个降级原因。

```python
# service.py —— 裁剪之后
if items and not clipped_items:
    degradation_reasons.append("token_budget_too_small")
```

**验证**：`tests/unit/test_recall_budget_degradation.py`（2 条）。
第一条**已验证修复前红、修复后绿**；第二条是护栏（真的没有记忆时不该标降级 ——
否则「没记过」和「装不下」又混成一件事）。

### 3.3 MCP 的 `append_memory` 声明幂等，实际不幂等

**严重度**：high（契约说谎）

`mcp/server.py` 的 `append_memory` 对外声明 `idempotentHint=True` ——
助手侧会据此决定「重试这个调用是安全的」。但幂等锚点 `event_id` 之前是
**可选**的，不传就现场造一个 UUID（`event_id or f"mcp-{uuid.uuid4()}"`），
于是每次重试都写成一条新记忆。

两种坏后果：助手按「可安全重试」用 → 同一条事实记好几遍；
助手按「不安全」用 → 自己加去重逻辑，绕过了服务本该提供的保证。

**修法**：`event_id` 改为**必填**。这与 HTTP 面一致（ADR-0005 / ADR-0008：
锚点由调用方生成、全局唯一，由 SDK 负责生成 UUID），也与该字段**自己的描述**
一致 —— 描述早就写着「必须全局唯一」，是默认值在违背它。

**这是对外契约变更**：省略 `event_id` 的 MCP 调用现在会报错而不是静默写入。
服务从未上线、没有真实接入方（ADR-0011），现在改的成本最低。

**验证**：`tests/unit/test_mcp_idempotency_contract.py`（2 条）——
schema 必须把 `event_id` 标成 required；省略它必须被拒。**两条均已验证修复前红、修复后绿**。

### 3.4 gRPC：未知的 proto3 枚举值被报成 INTERNAL

**严重度**：medium（协议语义）

proto3 的 enum 允许未知值（客户端版本比服务端新时会遇到）。
`rpc/converters.py` 直接查映射字典（`_ROLE[event.role]`、`_DELETE_SCOPE[req.scope]`），
查不到抛 `KeyError` —— 它既不是 ValueError 也不是 RuntimeError，
一路落到 `servicer.py` 的「未知异常 → INTERNAL」。

INTERNAL 对客户端的含义是「服务端坏了，重试」；实际是它自己发错了枚举值，
于是客户端会拿着同一个坏请求一直重试。正确的状态码是 **INVALID_ARGUMENT**。

**修法**：查不到时抛 `ValueError`（`_handle_error` 已把 ValueError 映射成
INVALID_ARGUMENT），不改状态码映射表。

**验证**：`tests/unit/test_grpc_enum_contract.py`（2 条，走真实 gRPC 通道）。
**两条均已验证修复前红、修复后绿**。

### 3.5 独立猎捕报来、本轮**未**独立复核的三条

另一路独立审计（不同上下文、不同方法）报了三条，本轮**没有**自己复现，
只记录不处置，理由是它们的影响面比上面四条小、且需要更多上下文判断：

| 编号 | 严重度 | 摘要 | 位置 |
| --- | --- | --- | --- |
| M-1 | medium | `Idempotency-Key` 的 409/重放可被 `namespace` 字段的默认值绕过：省略 namespace 与显式传默认值是两个分域，同一把 key 因此既不冲突也不重放 | `api/idempotency.py:33-60` |
| L-1 | low | `L2BackgroundRefresher.drain()` 可能在一次尾部刷新还在途时就返回 | `memory/l2_refresh.py:218-233` |
| L-2 | low | 幂等缓存每次写请求都做一次 O(n) 全表清理（10k 条时约 1.21ms × 3） | `api/idempotency.py:87-104` |

M-1 值得下一轮看一眼：它的根因与 §4.6 修的限流是同一处（调用方身份取自请求体），
修限流时我**刻意没有**动幂等层的分域口径，避免把「显式 vs 省略」两种写法合并 ——
那会改变既有契约，需要单独评估。

---

## 4. 接入 Lumen：最小改动清单

Lumen 是 TypeScript 单仓（Fastify + Drizzle + Postgres），多租户：
每行都带 `tenantId`，鉴权解析出 `UserIdentity`（`service/src/middleware/auth.ts`）。
它的记忆现状是 `service/src/user-preferences/` —— 一张按
`(tenant_id, user_id, type)` 唯一的 JSONB 键值表，**没有长期语义记忆**。

### 4.1 身份映射表

| Lumen | Thinkback | 说明 |
| --- | --- | --- |
| `tenantId` | **并入 `namespace`** | namespace 就是 mem0 的 `agent_id`，L1/L2/L3 三层都按它分区 |
| `userId` | `user_id` | 记忆归属人 |
| `conversationId` | `session_id` | Lumen 没有 sessionId；会话上下文范围等价 |
| `agentId` | 并入 `namespace`（如 `t42:lumen:agent-7`） | Lumen 的智能体维度 Thinkback 没有独立字段 |
| — | `memory_type` | Thinkback 独有：事实类型（画像/偏好/约束…） |

**为什么 tenant 走 namespace 而不是 user_id**：namespace 在三层都是真实分区键
（L3 = mem0 `agent_id`，L1/L2 有唯一约束与过滤，幂等域也用它分域），
而 `user_id` 在 mem0 侧只对应 `user_id` 一个维度，把租户塞进去会让
「同一租户同一用户跨智能体」的记忆被切开。**两者都可以，但 namespace 是语义正确的那个。**

**注意**：这条映射把租户隔离建立在「调用方不伪造 namespace」之上 —— Thinkback 不校验
namespace 的归属（漏洞 D）。接入初期务必让 namespace 由**服务端从 `tenantId` 拼出来**，
不要从请求体透传。

### 4.2 插入点

Lumen 的上下文装配在 `service/src/chat/context-builder/`：
`resolveLlmContext()`（`resolve.ts:266`）→ `buildPreContext()`（`plan.ts:399`）→
`buildModelMessages()`（`messages.ts:448`）。

Thinkback 召回包的落点是 **`buildPreContext` 产出的 pre-context**：
它已经是「在真正建消息之前先拼一段上下文」的结构，与 Thinkback 的
`Context Package`（分层内容 + 来源 + 预算 + 降级说明）是同一形状。
`buildModelMessages` 之后再插就要动消息序列，破坏 compaction 锚点与
`resolve.ts` 的历史选择逻辑。

**最小改法**：在 `resolveLlmContext` 里、构造 pre-context 之前，并发调一次
`recall_memories`（MCP 或 HTTP），把返回的 L1/L2/L3 条目转成 pre-context 的一段。
**不动 `buildModelMessages`、不动 compaction 逻辑。**

### 4.3 传输选择：MCP

Lumen 已有 MCP 客户端代码（`service/src/mcp-servers/`、`service/src/mcp-server/`），
Thinkback 已有官方 MCP server（`src/thinkback/mcp/`，5 个工具）。
**MCP 是现成的、跨语言、双方都实现了的接缝**，不需要 lumen 引入 Python SDK、
不需要维护 gRPC 描述符，也不需要把 Thinkback 的 REST 契约复制成 TypeScript 类型。

gRPC 面留给「lumen 要高吞吐直连」的后续；HTTP 面留给「lumen 已有 HTTP 客户端、
不想引入 MCP 依赖」的场景。**三者语义一致**（`tests/unit/test_integration_contract.py`
已把 HTTP ↔ gRPC 的同形钉住）。

### 4.4 与 Lumen 现有能力重叠的部分

| Lumen 现状 | 接入 Thinkback 后 | 判断 |
| --- | --- | --- |
| `user-preferences/`（JSONB KV） | **保留** | 它存的是「用户在某个智能体下选了哪些选项」这类结构化偏好，读路径要求同步、强一致；Thinkback 是异步抽取的语义记忆，读路径要容忍延迟与召回不到。两者不是一回事。 |
| `context-builder/summary.ts`（会话摘要） | **与 Thinkback L2 重复** | Thinkback 的 L2 就是会话上下文视图。接入后二选一：要么让 Thinkback 出摘要、lumen 不再自己摘要（省一次 LLM 调用，但引入跨服务延迟）；要么保留 lumen 的摘要、Thinkback 只用 L1+L3。**建议先保留 lumen 的，L2 留到有对比评测再切** —— 这是产品口径决定，不是技术决定。 |
| `chat/storage/`（会话原文） | **保留** | 那是 Lumen 自己的会话账本，与 Thinkback 的 L1 是不同生命周期（Thinkback 的 L1 服务记忆，lumen 的会话原文服务 UI）。 |

### 4.5 需要做的一方 / 另一方

**Lumen 侧**：一个 MCP 客户端 + 一个 `recall_memories` → pre-context 的转换器
+ namespace 由服务端从 `tenantId` 拼装。

**Thinkback 侧**：**需要改一处，已经改完**（限流分桶口径，见 §4.6）；
**还需要拍板一处**（租户鉴权，见 §4.7）。

其余部分不需要改：namespace 已经是够用的租户维度，MCP 已经暴露了需要的操作。
**唯一待定的是漏洞 A**（内置类型冲突策略失效）—— 如果 Lumen 用内置类型，
企业事实会缺覆盖语义；**最小处置是给 Lumen 配一个带 per-type 抽取指令的 profile**，
而不是改 Thinkback 的代码。

### 4.6 限流分桶：按 IP 分桶会让多租户调用方互相挤掉（本轮已修）

**这是接入 Lumen 的硬阻断，而且比租户鉴权更早发作。**

`api/ratelimit.py:97` 原来按对端 IP 分桶，600 请求/分钟、突发 50
（`infra/config.py:156-157`）。服务化接入时**所有租户都从同一个出口 IP 出来**
（k8s / ingress / sidecar），一个 IP 一个桶 = 全服务共享一份额度。
算术：一个 pod 服务 N 个租户，每个租户每轮 1 次召回 + 1 次写入、每 20 秒一轮
→ 约 `6N` 请求/分钟。N=100 就是 600/分钟 —— **整个 pod 被限流打满**，
而且被限流的不是超载的那个租户，是**全部租户**。

**修法**（本轮已做）：分桶单位从「网络位置」改成「调用方」——
请求体里的 `namespace` + `user_id`，与幂等层（`api/idempotency.py` 的
`_caller_identity`）**用同一个口径**，两个中间件对「谁」的理解不能各说各话；
取不到标识时退回对端 IP，行为与原来一致。

```python
# api/ratelimit.py —— 读 body 安全：Starlette 的 _CachedRequest 会缓存并向下游重放
identity = _caller_identity(await request.body())
if identity:
    return identity
return request.client.host if request.client and request.client.host else "unknown"
```

**语义变化（须知）**：限额单位从「一个网络位置」变成「一个调用方」。
对当前内网单租户部署，额度实际上变宽了（多用户 = 多桶）；
仍然想要一个服务级总闸的部署，用现成的 `MEMORY_RATE_LIMIT_PER_MINUTE` 调低即可，
不需要新配置项。

**验证**：新增 `tests/unit/test_rate_limit_caller_scoping.py`（4 条）。
其中「同 IP 不同租户不共享额度」「同租户不同用户不共享额度」两条
**已验证修复前红、修复后绿**；另两条是护栏（取不到标识时退回 IP、
限流读过 body 后下游仍能拿到完整请求体）。

### 4.7 租户鉴权：不是 Thinkback 的问题，但要按 Lumen 的规矩来

`docs/API.md:9-13` 写得很清楚：「业务面免鉴权……信任边界是网络，不是 token。
没有 API key、没有 scope、没有租户头。」而 v1 的公开接入协议
（`Authorization: Bearer tbk_live_*` + `X-Tenant-Id`）是**被刻意移除的**
（`docs/specs/2026-10-08-thinkback-integration-protocol-v1.md`）。

这与主流一致（mem0 OSS 本身也没有租户概念），但 **Lumen 的工程规矩比 Thinkback 严**：

> `service/AGENTS.md:88`：多租户：业务表启用 RLS……租户相关的 DB 读写必须通过
> `withTenant(tenantId, fn)` 执行……**数据库必须用非超级用户角色连接（超级用户绕过 RLS）**。

也就是说：把记忆外包给一个「不鉴权、且信任调用方自报 namespace」的旁路服务，
**恰好破掉 Lumen 用 RLS 守的那条线**（RLS 保护不到旁路服务里的数据）。

**最小处置（不建租户子系统）**：

| 方案 | 改动 | 什么时候选它 |
| --- | --- | --- |
| **A. 一企业一部署** | 零改动。Thinkback 只服务一个企业，namespace 由企业自己的服务拼 | 绝大多数情况。Lumen 本来就是多租户 SaaS，所以对 Lumen 而言**不适用** |
| **B. 共享部署 + 服务凭据** | 业务面加一个可选的 `Authorization: Bearer`（对齐 Lumen 的 `Bearer sk-<服务 key>` + `x-service-name` 约定），并在请求里带 `tenant_id`，**由服务端派生 namespace**（客户端传的 namespace 不再参与隔离） | Lumen 复用同一套 Thinkback 部署服务多个租户时 |

**B 方案的改动是纯增量的**：`tenant_id` 作为可选字段加在 5 个请求模型
（`memory/schemas.py`）、proto 的可选字段、MCP 工具的可选参数；
`tenant_id is None` 时行为与今天**逐字节相同**。

**本轮未做 B**：它改的是对外契约与信任模型，属于「租户鉴权/部署模型」那条
**待你拍板**的决策（见 §7）。先做完不依赖该决策的部分（§4.6 限流、
§4.5 的 Lumen 侧适配、漏洞 A 的 profile 绕法），拍板后再动契约。

---

## 5. 测试体系：按 `ai-testing-system` 的 11 条打分

| # | 条目 | 判定 | 证据 |
| --- | --- | --- | --- |
| 1 | 确定性单测/集成 | **PASS** | `make test` 全绿，无网络 / 无真实 LLM·DB，用 `FakeMemoryBackend` + 内存仓储 |
| 2 | 契约测试 | **PARTIAL** | 仓储契约有（`test_repository_contract.py` 同一组用例跑内存/SQL 双实现）、接入契约有（`test_integration_contract.py`）。**但 L3 后端没有契约套件** —— `fake.py` 与 `mem0_library.py` 从未跑同一组用例。
（注意：**不是**因为 fake 只产 ADD —— 那与引擎一致；缺口在于两套实现没有任何共享用例，
将来改适配器时行为漂移不会被发现） |
| 3 | 覆盖率门禁 | **FAIL（非阻断，但要认账）** | 仓库**刻意**不把覆盖率当门禁（`Makefile:44` 的 `COVERAGE_FLOOR ?= 92` 只打印不阻断），取舍有道理（`_verify_secret` 恒真函数在 95.9% 覆盖率下活着）。**但本轮实测行覆盖率 90.5%（7995 行 / 缺 761 行），低于仓库自己写的 92% 观测水位** —— 水位线既然写了，就该对得上：要么把水位调到 90，要么补上缺的 1.5% |
| 4 | 属性测试 | **PARTIAL** | hypothesis 只覆盖 `domain/keys.py` / `summarization.py` / `content_quality.py` 三个模块；**`recall_policy` / `decay` / `retention` / `task_state` / `l2_refresh` 一条属性测试都没有** —— 而这几个正是纯函数、正是该用属性测试的地方 |
| 5 | 变异审计 | **PARTIAL → 本轮补强** | 见 §5.2、§5.3 |
| 6 | e2e 关键旅程 | **PASS** | `tests/e2e/` + 真实栈 `gate_real.py` 的「写入→召回→擦除→不可见」链路（本轮本地实测 9/9） |
| 7 | 视觉 + a11y 回归 | **PARTIAL** | a11y 有（`tests/e2e/test_a11y_smoke.py`，axe 严重级为 0）；**视觉基线完全没有** —— 全仓 `toHaveScreenshot` / `--snapshot` 零命中，管理台静默破版无人拦 |
| 8 | API 模糊测试 | **PASS** | `tests/fuzz/`（schemathesis），**且已进 CI**（`ci.yml` 的 `api-fuzz` job） |
| 9 | 供应链 CVE 门禁 | **PASS** | `make audit`（pip-audit）**已进 CI**（`supply-chain-audit` job） |
| 10 | 防回血基线 | **PARTIAL** | 评测基线有（`tests/eval/baseline*.json` + `check_baseline.py` 会抓删用例/改阈值）；**但 pytest 套件本身没有基线** —— 删掉一条单测，门禁看不见 |
| 11 | LLM 质量评测 | **PASS** | `run_self_contained_evaluation.py` + `run_semiconductor_evaluation.py` + 真实栈 `gate_real.py` |
| — | 真实栈门禁在 CI | **FAIL（本轮已定位，未修）** | 见 §5.4 |

前序报告说「真实栈门禁不在 CI」「编排层缺并发与故障注入测试」——
**第二条现在不准确了**：仓库已有 `test_erase_concurrent_write.py`、
`test_cas_retry.py`、`test_task_optimistic_lock.py`（含双副本 CAS）、
`test_concurrent_l3_cleanup_completions_do_not_lose_pending_count`，
`test_memory_service.py` 里有 `Barrier`/`Thread` 并发用例；
故障注入也有（`FailingAddBackend` / `FailingDeleteBackend` / `FailsOnceDeleteBackend`）。

### 5.1 本轮真实栈门禁实测（本地）

用本地真 Milvus + 真 PG + 真 LLM/embedding 端点跑 `tests/script/gate_real.py`：

```
PASS extraction_non_empty        PASS idempotent_replay_consistent
PASS expected_fact_coverage      PASS collection_schema_owned
PASS expected_fact_recall        PASS bm25_ascii_hit
PASS forbidden_term_not_leaked   PASS bm25_chinese_hit
PASS erase_no_residue
全部通过（9 项）
```

报告：`docs/memory/report/real/gate-1a5e2f7-20261029-054610.json`。

**踩到的环境坑**：本地 `.env` 的 `MILVUS_URL` 写的是容器端口 `19530`，
而 compose 把容器 19530 映射到宿主机 `19540` —— 仓库自己的 `gate_real.py` 文档里
写的就是 `MILVUS_URL=http://localhost:19540`。`.env` 需按实际映射端口改。
（`.env` 不进版本库，这是本地配置问题，不是代码缺陷。）

### 5.2 变异预算的真实分布

`pyproject.toml` 的 `only_mutate` 只覆盖 4 个文件（`domain/keys.py`、
`domain/summarization.py`、`memory/decay.py`、`memory/repositories/_l1_cache.py`）。
361 个变异体里 **71 个（72%）集中在 `memory/decay.py`** 一个文件，
而 `memory/service.py`（6000+ 行，幂等 / 擦除 / 冲突 / 预算 / 并发都在里面）**一个都没变异**。

前序报告说「98 个存活变异体里绝大多数是日志文案未被断言」——
**实测不准确**：98 个里 30 个是日志/文案，16 个是等价变异，**44 个是真正的覆盖缺口**
（校验边界没断言、异常文案没断言）。这不是装饰性的。

### 5.3 本轮修的门禁盲区（结构性缺陷）

**问题**：mutmut 不会重跑已经跑过的变异体（源码没变就沿用上次结论）。
于是「昨天写的测试今天才杀掉某个存活变异体」这件事**永远看不见**：
`mutmut results` 里的存活数与基线里的 98 条**在补测试之后依然是 98**。
门禁因此**无法回答「我补的测试到底有没有补上」** —— 这正是「变异预算花错了地方」
这句话在代码里的真实形态。

**修法**（`tests/eval/refresh_mutation_stats.py`）：审计前置时**把存活的那批也点名重跑**。
mutmut 对「被点名的变异体」会强制重跑，所以新增覆盖能立刻反映出来。
实测代价：98 个存活变异体重跑 **21 秒**，可接受。

**效果**：本轮给 `memory/decay.py` 补了 6 条边界用例后，
**98 → 84**（14 个存活变异体被新测试杀死），基线同步从 98 条剪到 84 条。
新补的用例钉死的是**两个真实的 off-by-one 风险**：

- 年龄阈值判定是 `valid_at > min_valid_at → 跳过`，即**恰好到龄就是候选**；
  原有用例只有「明显老」和「明显新」两个样本，离阈值几十天，判定方向翻转也不红。
- `last_recalled_at <= max_last_recalled` 同理。

这类错误不会让任何既有用例变红，要等生产上「早一天衰减」或「晚一天衰减」才暴露。

### 5.4 CI 的真实状态与一处名不副实

`.github/workflows/ci.yml` 的 PR 门禁其实相当完整：
`lint-and-typecheck` → `unit-tests` → `supply-chain-audit`（pip-audit）→
`api-fuzz`（schemathesis）→ `mutation-and-eval`（变异 + 评测基线）→
`web-gates`（npm audit + vitest + build + Playwright e2e）→ `docker-build-verify`。

**前序报告说「真实栈门禁不在 CI」是对的**，而且还有一处名不副实：

```yaml
nightly-full:
  name: nightly (full local gates + real-stack scripts)   # ← 承诺了 real-stack
  steps:
    - run: make check        # 全是 fake / 确定性
    - run: pytest tests/fuzz
                                          # ← 没有任何 real-stack 步骤
```

job 名字承诺「real-stack scripts」，步骤里一条都没有。**本轮未修**：
补这个 job 需要在 CI runner 上起 Milvus standalone + PG + 配 LLM/embedding secret，
属于 CI 基础设施改动，且**本地无法验证**（GitHub runner 的 5432 端口与
根 `docker-compose.yml` 的固定端口映射冲突，需要额外 override）。
**建议单独一个 PR 做**，并先用 `workflow_dispatch` 手动跑通一次再合。

---

## 6. git 追踪

仓库当前**工作区干净、零未跟踪文件**（`git status --porcelain` 无 `??`）。
运行产物已在 `.gitignore`：`mutants/`、`.testmondata*`、`coverage.xml`、`.coverage.*`、
`.hypothesis/`、`web/node_modules/`、`docs/memory/report/`、`.claude/`、`.serena/` 等。
截图副产物的脱离追踪在提交 `3573781` 完成（`assets/screenshots/*-full.png`、
`07-integration-*.png`）。**本轮无需再动。**

本轮新增/修改的文件（全部应当进版本库）：

| 文件 | 性质 |
| --- | --- |
| `src/thinkback/memory/service.py` | 缺陷修复（DELETE 事件下线、预算过小记降级） |
| `src/thinkback/api/ratelimit.py` | 缺陷修复（限流按调用方分桶） |
| `src/thinkback/mcp/server.py` | 契约修复（`event_id` 必填） |
| `src/thinkback/rpc/converters.py` | 协议修复（未知枚举 → INVALID_ARGUMENT） |
| `tests/unit/test_l3_backend_event_sync.py` | 新增回归测试（4 条） |
| `tests/unit/test_rate_limit_caller_scoping.py` | 新增回归测试（4 条） |
| `tests/unit/test_recall_budget_degradation.py` | 新增回归测试（2 条） |
| `tests/unit/test_mcp_idempotency_contract.py` | 新增回归测试（2 条） |
| `tests/unit/test_grpc_enum_contract.py` | 新增回归测试（2 条） |
| `tests/unit/test_memory_decay.py` | 新增边界用例（6 条） |
| `tests/eval/refresh_mutation_stats.py` | 门禁修复（存活变异体重跑） |
| `tests/eval/mutation_survivors.json` | 基线随实测剪枝（98 → 84） |
| `docs/report/2026-10-29-thinkback-round4-…md` | 本报告 |

---

## 7. 未做与待拍板

| 项 | 为什么没做 | 建议 |
| --- | --- | --- |
| 漏洞 A 的 fail-visible 改造 | 会改可观测行为（新增告警/指标），且属于「策略决定」而非缺陷修复 | **建议做**：只加 warning + 计数，不改语义，风险低、收益直接 |
| 给内置类型补通用抽取指令 | 会改变所有消费级场景的抽取形状，必须重跑评测 | 暂不做；接入 Lumen 时**用带 per-type 指令的 profile** 绕开 |
| 删 P0 槽位引擎 | 改召回与冲突语义，需要配套评测与迁移 | 暂不做；先评估「企业场景下 P0 的净贡献是否为零」 |
| `CONTEXT.md` 改写 namespace 描述 | 词汇表改动要连 ADR 一起过 | **建议做**：接 Lumen 之前必须改，否则接入方会被误导 |
| 真实栈门禁进 CI | 需要 CI runner 起 Milvus + PG + secret，且本地无法验证 | 单独一个 PR，先 `workflow_dispatch` 手动跑通再合 |
| 租户鉴权（§4.7 方案 B） | 改的是对外契约与信任模型，属于**待你拍板**的那条决策 | **需要你拍板**：Lumen 是多租户 SaaS，若要与别的企业共用一套 Thinkback 部署，就得走 B；否则各企业各自部署（对 Lumen 不适用） |
| L3 后端契约套件（fake ↔ mem0 跑同一组用例） | 要给 `FakeMemoryBackend` 补 `UPDATE`/`DELETE` 语义，会改所有既有用例的语义 | **建议做**：§3 那个 bug 正是从这条缝里漏过去的；本轮用测试内替身覆盖真实形状，契约化留到下一轮 |
| 属性测试扩到 `recall_policy`/`decay`/`retention`/`task_state`/`l2_refresh` | 这几个模块还没被变异预算覆盖，扩属性测试前应先让它们进变异范围 | 建议按「先变异、再补属性测试」的顺序做 |
| 视觉基线（管理台截图） | 需要先有稳定的截图基线文件与 CI 产物留存策略 | 建议做，但不急 —— a11y 已有 |
| pytest 套件本身的防回血基线 | 需要记录用例清单并做增删比对 | 建议做，成本低（一条脚本 + 一个清单文件） |
| M-1（幂等域被 namespace 默认值绕过）、L-1（L2 drain 竞态）、L-2（幂等缓存 O(n) 清理） | 另一路独立审计报来，本轮**未**独立复现（§3.5） | 下一轮各自复核后再处置；M-1 与本轮限流改动同源，改它要先定「显式 vs 省略 namespace 算不算同一次写」 |

---

## 附：本轮改动清单（可复核）

```
src/thinkback/memory/service.py                 +25 −1   引擎事件对齐 + 预算过小记降级
src/thinkback/api/ratelimit.py                  +37 −4   限流分桶：调用方 → 不再按对端 IP
src/thinkback/mcp/server.py                     +16 −5   append_memory 的 event_id 必填
src/thinkback/rpc/converters.py                 +17 −2   未知枚举 → INVALID_ARGUMENT
tests/unit/test_l3_backend_event_sync.py        新增     4 条（修复前 2 条红）
tests/unit/test_rate_limit_caller_scoping.py    新增     4 条（修复前 2 条红）
tests/unit/test_recall_budget_degradation.py    新增     2 条（修复前 1 条红）
tests/unit/test_mcp_idempotency_contract.py     新增     2 条（修复前 2 条红）
tests/unit/test_grpc_enum_contract.py           新增     2 条（修复前 2 条红）
tests/unit/test_memory_decay.py                 +~130    6 条边界用例
tests/eval/refresh_mutation_stats.py            +~25    存活变异体纳入重跑
tests/eval/mutation_survivors.json              98 → 84  基线剪枝
```

**本轮的「先红后绿」验证**（防止写了空测试）：

| 缺陷 | 修复前红了的用例 | 修复后 |
| --- | --- | --- |
| 引擎事件与本地索引的一致性（潜伏，当前引擎不可达，见 §3.1 订正） | `test_backend_delete_event_deactivates_local_index_row`<br>`test_backend_delete_event_not_listed_as_live_memory` | 全绿 |
| 限流按 IP 分桶 | `test_tenants_sharing_one_ip_do_not_share_a_rate_limit_bucket`<br>`test_same_tenant_different_users_have_separate_buckets` | 全绿 |
| 预算过小静默空包 | `test_budget_smaller_than_one_memory_reports_degradation` | 全绿 |
| MCP 声明幂等却不幂等 | `test_append_memory_schema_requires_event_id`<br>`test_append_memory_without_event_id_is_rejected` | 全绿 |
| gRPC 未知枚举报 INTERNAL | `test_unknown_role_enum_returns_invalid_argument`<br>`test_unknown_delete_scope_enum_returns_invalid_argument` | 全绿 |

五处都用「先回退实现 → 跑测试确认红 → 恢复 → 跑测试确认绿」的方式验证过，
不是「看着像对的」。
