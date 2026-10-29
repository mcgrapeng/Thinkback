# Thinkback 接入面与发布就绪度（第三轮，2026-10-29）

- 基线：分支 `refactor/memory-architecture-rewrite`，HEAD `71d8f1a` + 上一轮未提交改动
- 前两轮报告：`2026-10-29-thinkback-deep-analysis.md`（设计漏洞与生产差距）、
  `2026-10-29-thinkback-adversarial-bug-hunt.md`（9 条可复现缺陷）、
  `2026-10-29-mainstream-memory-oss-comparison.md`（主流对照）、
  `2026-10-29-thinkback-ponytail-audit.md`（老手审计清单）
- 本轮定位：把「能接」从口号变成可执行的契约与可跑的测试；同时补齐上一轮留下的两个未决缺陷、
  拆掉三个不生效的配置旋钮。

## 0. 结论

1. 引擎不换。mem0 2.2.1 就是当前最新发布版，没有版本债；mem0 OSS 明确不做持久执行、幂等、
   namespace、信任分类、删除审计、上下文装配、预算，这些是 Thinkback 的存在理由。本轮把
   `mem0ai` 从 `>=2.2.1,<3` 锁死 `==2.2.1`：物理擦除要直接操作 mem0 的 SQLite `history` /
   `messages` 表，版本漂移该由人显式决定，不能由 pip 顺手升上去。
2. 上一轮 9 条缺陷闭环 8 条。剩下的 BUG-2（删一条事实是否连整轮原文一起抹掉）是产品口径问题，
   本轮按更安全的一侧保持现状，并补了回归测试把这个口径钉死。
3. 并发会话的 pass2 审计又找出 7 条 must-fix，全部复现、全部处置（§2.1）。其中 3 条是**合规级**
   的洞：擦除说「已完成」而原文还在引擎侧、过期记忆永远擦不掉、「删除全部」对活跃用户永远失败
   而且每次重试都销毁新数据。
4. 「任何企业助手接入」缺的是接入协议，不是引擎能力。本轮补上 MCP（官方 SDK，5 个工具），
   接入契约从人工清单变成可执行的测试矩阵。
5. 离生产还差三块，都不是代码问题：变异预算花错地方、真实栈门禁不在 CI、编排层缺并发与故障
   注入测试（详见 §5）。

## 1. 与主流的对照：Thinkback 站在哪

结论与上一轮一致（详见对照报告 §4），本轮只补状态：

| 主流共识 | Thinkback 状态 |
| --- | --- |
| 语义抽取交给引擎 | ✅ 只让 mem0 抽取；Thinkback 不自建抽取器，只提供 per-type 指令 |
| 有版本化的普通 API，MCP 是它的投影 | ✅ **本轮补齐**：HTTP `/v2/memory/*` + gRPC v2 + MCP `thinkback-mcp` |
| 工具面克制（读 2–3 / 写 1–2 / 删 1 / 状态 1） | ✅ MCP 固定 5 个工具，全部带 annotations 与结构化返回 |
| 幂等锚点由调用方给 | ✅ `event_id`（行业里几乎没人做，supermemory 至今仍是提案） |
| 删除语义要能表达「部分完成」 | ✅ 擦除回执按资源逐项给（`purged` + `done_stages`） |
| namespace 不是权限边界 | ⚠️ **仍然成立**：业务面免鉴权，信任边界是内网。跨企业共用一套部署前必须补租户鉴权 |
| 打包格式用 Agent Plugins v1.1.0 | ❌ 未做：只有服务端本体，没有客户端分发包 |
| 真实栈质量要在 CI 里报警 | ❌ 未做：`make gate-real` 仍只跑本地 |

明确不做（对照报告 §5.2 的清单仍然有效）：不自建图存储、不自建 BM25、不自建向量融合打分、
不做 agent 自主改记忆、不做多 provider 适配层、不自建评测排行榜、不自己实现 MCP 协议栈
（用官方 `mcp` SDK）。

## 2. 上一轮 9 条缺陷的处置

| 编号 | 严重度 | 状态 | 处置 |
| --- | --- | --- | --- |
| BUG-1 | high | ✅ 已修 | `delete_all` 返回**实际删掉的 ID 列表**（协议 / mem0 适配器 / fake 同步改），擦除按这些 ID 清引擎侧原文；SQLite `IN (...)` 按 500 分块 |
| BUG-2 | high | ⚠️ 取产品口径 | 保持现状：**整轮粒度**抹除。删一条事实 → 整轮进排除集 → 该轮原文不再进上下文。理由：L1 是唯一原始证据层，「被删事实的原文还在」比「兄弟事实少了出处」更难向合规方解释。本轮补了回归测试把这个口径钉死 |
| BUG-3 | medium-high | ✅ 已修 | 幂等缓存按**调用方身份**（请求体 `namespace` + `user_id`）分域，取不到才退回对端 IP |
| BUG-4 | medium-high | ✅ 已修 | 层配额装不下任何一条时，用全局余额给空层补一条（保底顺序 L1→L2→L3） |
| BUG-5 | medium | ✅ 已修 | 任务结果记**原始类型名**，重放比对也用原始类型名，自定义类型不再误报 409 |
| BUG-6 | medium | ✅ 已修 | LLM 摘要失败也算「已尝试」，重试节奏回到去抖间隔，不形成调用风暴 |
| BUG-7 | medium | ✅ 已修 | 擦除复核改成**逐个点查目标 ID**（后端协议新增 `get`），不再把「页取满」当成残留 |
| BUG-8 | low | ✅ 已修 | 引擎侧原始消息按**原文**匹配（mem0 存的是原样 content） |
| BUG-9 | low | ✅ **本轮补完** | 重试沿用**首次写入时**定的到期日，不按「重试时刻 + TTL」重算。回归测试用可控时钟验证：首次写入 2026-05-04（到期 2026-08-02），一个月后重试仍是 2026-08-02 |

## 2.1 pass2 审计的 7 条 must-fix：复现与处置

工作区里同时有另一场审计（`docs/report/2026-10-29-thinkback-ponytail-audit-pass2.md`）。
它的 7 条我逐条跑了它自带的复现脚本（`/tmp/mos/repro_*.py`），结论如下。第 1、2、4 条由并发
会话在同一工作区修掉，第 3、5、6、7 条由本会话接手。

| # | 问题 | 复现 | 处置 |
| --- | --- | --- | --- |
| 1 | `erase` 排在 `delete` 之后时，引擎侧消息表里的原文没被清，接口仍报 completed | ✅ 复现（`message_texts: []`） | ✅ 已修（并发会话）：擦除计划从墓碑里取回被删轮次的引用，不再依赖「当前还活着的轮次」 |
| 2 | 词表的 `system:` / `tool:` 任意位置匹配 →「我们的操作系统：Ubuntu」整轮被扔 | ✅ 复现 | ✅ 已修（并发会话）：只在一行开头才算命中 |
| 3 | 词表 docstring 说「保留 L1 原文」，代码却把该轮标成墓碑（不进召回） | ✅ 复现 | ✅ 已修（本会话）：docstring 改成与代码一致 —— 原文留在库里、治理台可见，但**隔离出召回** |
| 4 | 注入语料加两个空格（`ignore  all  previous  instructions`）就绕过词表 | ✅ 复现 | ✅ 已修（并发会话）：词表用 `\s+` 而不是单个空格 |
| 5 | ALL 范围擦除把「擦除期间新写的记忆」当成残留 → 擦除永远失败，且每次重试都删掉新数据 | ✅ 复现 | ✅ 已修（本会话）：ALL 只复核**这次真删掉的那批 ID**，新写的记忆不算残留；目标没删掉时仍然失败（不放宽成「永远成功」） |
| 6 | mem0 的 `get_all` 默认隐藏过期行 → 过期记忆列不出来、删不掉，擦除却报「已完成」 | ✅ 复现 | ✅ 已修（本会话）：删除与复核显式传 `show_expired=True`（引擎不支持该参数时自动省略） |
| 7 | mem0 客户端不是线程安全的：entity_store 是实例级懒加载属性，实体链接是读-改-写 → 同一用户的并发写会丢记忆 id 且不报错 | 读码核对 mem0 2.2.1 源码（`_upsert_entity` / `_link_entities_for_memory`）+ mem0ai/mem0#4892 | ✅ 已修（本会话）：写操作按 `(user, scope)` 分片串行（64 片固定锁表，不随用户数增长）；不同用户之间不互相阻塞 |

第 1、5、6 条是**对外承诺与实际不符**那一类：接口说「已物理擦除」，原文还在。Thinkback 的
差异化（可核对的删除）正是最容易说谎的地方，所以这三条的回归测试落在
`tests/unit/test_erase_concurrent_write.py` 与 `tests/unit/test_memory_backends.py`。

## 3. 本轮的改动清单

### 3.1 MCP 接入层（新增）

`src/thinkback/mcp/`（`server.py` + `__main__.py`），用官方 `mcp` SDK（`FastMCP`），薄适配：
工具直接调用 `MemoryService`，不重写业务逻辑，语义与 `/v2/memory/*` 完全一致。

| 工具 | 作用 | annotations |
| --- | --- | --- |
| `append_memory` | 写入一条原始事件 | `readOnlyHint=false` `destructiveHint=false` `idempotentHint=true` |
| `recall_memories` | 召回 L1/L2/L3 | `readOnlyHint=true` |
| `list_memories` | 列出长期记忆 | `readOnlyHint=true` |
| `delete_memory` | 逻辑删除一条记忆 | `readOnlyHint=false` `destructiveHint=true` `idempotentHint=true` |
| `get_operation` | 查后台任务终态 | `readOnlyHint=true` |

三条设计决定：

- namespace 由服务端固定，不作为工具参数暴露。namespace 是租户/业务分组边界，让调用方自己
  传等于任何助手都能换租户。
- 写工具返回任务句柄，终态要问 `get_operation`。写成功 ≠ 全链路成功。
- http 传输可配 Bearer token，没配就是不鉴权（只适合受信内网）；stdio 传输不需要 token，
  进程边界本身就是信任边界。

启动：`thinkback-mcp --namespace semiconductor`（stdio）或 `--transport http --token ...`。

### 3.2 拆掉三个不生效的旋钮

`recall.path` / `layer` / `extractor=profile` 在全仓库零消费者（`grep` 确认），留着只会让接入方
以为配了就生效。现在一律拒绝：

- `layer` 字段删除（只落地 L3，从来没有第二层语义）；
- `recall` 对象只剩 `budget`（`path` 的三种取值里 `fact` 是唯一实现的）；
- `extractor` 收敛为 `mem0 | none`（`profile` 与 `mem0` 行为完全相同）。

配套：`profiles/semiconductor.json` 同步清理；新增用例钉住「这三个旋钮必须被拒绝」。

### 3.3 引擎版本锁死

`mem0ai==2.2.1`。理由见 §0。

### 3.4 接入新企业不用重建镜像

`load_registry` 每次调用重读盘，没有缓存，profile 因此是运行时加载的。新企业只要把
自己的 `<namespace>.json` 与 `prompts/<namespace>/*.vN.md` 以 ConfigMap 挂进容器即可，
不用改代码、不用重建镜像。挂载写法与边界写在 `k8s/README.md`「接入新企业」一节；
`MEMORY_TYPE_PROFILE_DIR` 可以把整个目录指到别处。新增用例钉住「profile 是调用时加载的」。

### 3.5 引擎侧的三处硬伤（并发会话 pass2 审计 + 本会话接手）

- **过期记忆删不掉**：mem0 的 `get_all` 默认隐藏过期行，删除与擦除复核因此看不见它们，
  列不出来就删不掉，擦除却报「已完成」。删除路径显式传 `show_expired=True`（引擎不支持该
  参数时自动省略）。
- **「删除全部」对活跃用户永远失败**：ALL 复核把作用域里的任何记录都当成残留，擦除期间用户
  新写的记忆也算。改成只复核**这次真删掉的那批 ID**；目标没删掉时仍然失败。
- **mem0 写入不是线程安全的**：entity_store 是实例级懒加载属性，实体链接是 `linked_memory_ids`
  上的读-改-写，同一用户的并发写会丢记忆 id 且不报错（mem0 自己有同类的异步写入 bug：
  mem0ai/mem0#4892）。写操作按 `(user, scope)` 分片串行，64 片固定锁表，不随用户数增长。

### 3.6 文档

- 新增 `docs/MCP.md`：工具面、启动参数、接 Lumen 的两条路径、已知边界、契约验收入口。
- `docs/API.md`、`README.md` 补上 MCP 这张面孔。
- `k8s/README.md` 补「接入新企业」一节：profile 挂 ConfigMap 即可，不用重建镜像。

## 4. 接入面现状：Lumen 怎么接

Lumen（`/Users/zhangpeng/workspace/zhiman/lumen`）是 TypeScript 服务，两条路都通：

| 方式 | 适用 | 映射 |
| --- | --- | --- |
| HTTP 客户端 | Lumen 后端调用 | `tenantId` → namespace，`userId` → `user_id`，`sessionId` → `session_id` |
| MCP 客户端 | Lumen 已有 MCP client 能力（`service/src/mcp-server/client.ts`） | 把 Thinkback 当远程 MCP server，工具直接暴露给 Lumen 的 agent |

Lumen 侧现有的 `user-preferences` 是偏好设置（聊天/侧边栏），不是长期记忆，两者不要混：
Thinkback 存的是「用户说过的事实 + 可核对的来源」，Lumen 的 preferences 是 UI 状态。

接入前必须知道的边界：

- namespace 是分组标签，不是安全边界。业务面免鉴权，跨企业共用一套部署前要补租户鉴权；
  或者坚持一企业一部署（推荐，也是当前部署模型）。
- 只支持单副本。幂等缓存与限流桶在进程内，`infra/replica_guard.py` 会在「宣称多副本但前置
  没齐」时启动期硬失败。要承诺 HA，先把幂等与限流迁到 PG。
- MCP 与 HTTP 在同一进程共享同一个 `MemoryService`（同一份 L1 缓存与任务账本）。

## 5. 测试体系：现状与还差什么

### 5.1 现状（本轮实测）

| 项 | 结果 |
| --- | --- |
| `make check` | EXIT=0（format-check / lint / mypy / pytest / mutation-audit / eval 六段全过） |
| 单元测试 | 1075 全绿（上一轮 1030，本轮 +45） |
| 垂类评测 | 自包含 40/40 通过率 100%，基线达标 |
| 变异审计 | 通过，无新增未登记存活 |

### 5.2 本轮把「人工清单」变成「可执行测试」

上一轮报告指出，接入契约矩阵是人工清单，不是测试。本轮落成
`tests/unit/test_integration_contract.py`（11 条用例），逐行对应契约矩阵：

| 契约行 | 用例 |
| --- | --- |
| 自定义类型全链路不丢类型名 | append → items → get → update → recall，`memory_type_name` 全程为自定义名 |
| 四种事件角色都能写 | user / assistant / tool / system 各一条 |
| assistant 不成为用户事实 | assistant 事件在 `source_roles=(user)` 的类型下不产生记忆 |
| metadata 不被吞 | 自定义业务属性保留在 `custom_metadata` 子字典 |
| namespace 读写删矩阵 | 同 user/session 跨 namespace：写、读、擦除三面互不可见 |
| HTTP ↔ gRPC 同形 | 两面同语义；同一事件跨面重放是同一件事（幂等锚点跨传输面也认） |
| 幂等 | 同 `event_id` 重放只落一条 |
| 擦除回执 | `done_stages` 覆盖六个阶段，`purged` 逐资源给数；各层确实不可见 |

MCP 自身的契约在 `tests/unit/test_mcp_server.py`（6 条，走真实 MCP 协议路径：内存传输加客户端
session，工具清单、参数 schema、结构化返回都过一遍客户端）。

### 5.3 还差三块（诚实缺口）

1. 变异预算花错了地方。`pyproject.toml` 只变异 4 个文件（`domain/keys.py`、
   `domain/summarization.py`、`memory/decay.py`、`memory/repositories/_l1_cache.py`），而
   `memory/service.py`（5800+ 行，所有并发、擦除、幂等、预算逻辑都在里面）从未被变异过；
   98 个存活变异体里绝大多数是「日志文案未被断言」这类低价值项。
   最小修法：把纯日志文案类整类登记（而不是一条条登记），再把预算挪到 `service.py` 里抽出的
   3 到 5 个纯决策函数上；或者对编排层改用属性 / 状态机测试。
2. 真实栈门禁不在 CI。抽取质量、BM25 是否真生效、擦除后各资源是否真不可见、幂等重放一致，
   这些只在本地 `make gate-real` 跑。D9 之下「语义由 mem0 承担」意味着 mem0 版本升级就是一次
   生产风险，而今天没有任何自动化能在 PR 上告诉你「mem0 2.x 变了，中文 BM25 命中从 1 掉到 0」。
   最小修法：CI 夜间用 compose 起 Milvus + PG + 假模型端点，把「集合 schema / BM25 中英文命中 /
   filters 生效 / erase 后不可见」四类接进 CI。
3. 编排层缺并发与故障注入测试。上一轮对抗式猎捕能找出 9 条缺陷，靠的是读码加单进程 in-memory
   仓储；多副本下的 append/erase 交错、同一 `round_id` 两副本并发、跨副本删除屏障，都需要两个
   真进程加共享 PG 才能证伪。这一块目前是空白。

## 6. git 追踪

仓库本已干净（`git ls-files -i -c --exclude-standard` 为空：没有被跟踪却被忽略的文件）。
本轮维持上一轮的处置：9 张全仓库零引用、可由 `web/take-screenshots*.mjs` 再生的截图取消跟踪并进
`.gitignore`，跟踪体积 13032 KB → 7972 KB（-39%）；README 引用的 10 张变体全部保留。

`docs/report/` 下的审计报告继续入库：它们是「为什么这么决定」的证据链，不是构建产物。
本报告同样入库。

## 7. 下一步（按投产阻塞程度排序）

| # | 事项 | 性质 | 建议 |
| --- | --- | --- | --- |
| 1 | 租户鉴权（跨企业共用部署的前提） | 架构 | 先确认部署模型：**一企业一部署**（零改动）还是**共享部署**（要加 token→namespace 派生） |
| 2 | 真实栈门禁进 CI 夜间 | 测试 | mem0 版本升级是生产风险，这一条不做，版本升级只能靠人肉 |
| 3 | 变异预算挪到 `service.py` 决策点 | 测试 | 现有门禁在保护日志字符串，放过整个编排核心 |
| 4 | 编排层并发 / 故障注入测试 | 测试 | 上一轮 9 条缺陷的发现方式在多副本下会失效 |
| 5 | Agent Plugins v1.1.0 打包（客户端分发） | 生态 | 只在真的要跨多个 host 分发时才做 |
| 6 | `service.py` 拆分（5800 行八件事） | 可维护性 | 上一轮老手审计的第 12 条；**不要在投产前动**，动就是新的回归面 |

## 8. 本轮没做的（以及为什么）

- 没做租户鉴权：它是唯一的架构级决策，且取决于部署模型（见 §7 第 1 条）。本轮用「namespace 由
  服务端固定 + 一企业一部署 + 文档写明边界」把这个洞显式化，没有偷偷扩大信任面。
- 没做真实栈 CI、变异预算重排、并发测试：都是测试工程量的大头，需要独立一轮；本轮先把接入
  契约从人工清单变成可执行测试，并在报告里写清剩余缺口。
- 没拆 `service.py`：5800 行的类在投产前拆是给自己制造回归面，列为投产后的第一件事。
- 没改 `namespace` 的语义：`namespace` 仍不是安全边界，这是既有设计决策（D10），改动会牵动所有
  调用方。
