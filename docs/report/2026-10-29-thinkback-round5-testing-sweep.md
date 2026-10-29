# 第五轮：记忆系统测试体系排查与整改（2026-10-29）

> 上线前把「核心逻辑真的被测过」这件事从「相信」变成「可核对」。
> 手段是变异审计：逐条改坏源码，看现有测试会不会红。
> 第四轮已经证明示例测试钉不住「方向写反」这类缺陷——离阈值几十天的样本，
> 判定方向翻转也不红。

## 0. 结论

| 维度 | 本轮开始 | 本轮结束 |
| --- | --- | --- |
| 确定性用例 | 1,239 | 1,628 |
| 变异审计范围 | 4 个文件 / 361 个变异体 | 11 个文件 / 1,054 个变异体 |
| 变异体被杀率 | 77% | 90% |
| 已验收存活变异体 | 84 | 106 |
| 行覆盖率 | 93%（8,022 行，未覆盖 567） | 94%（8,022 行，未覆盖 482） |
| `make check` | 绿 | 绿 |

本轮最重要的产出是三个门禁缺陷。它们让「变异审计已覆盖核心逻辑」这句话在
某些情况下不成立，而且静默：门禁照绿，实际什么都没审。

## 1. 排查方法

按 `~/.agents/skills/ai-testing-system/SKILL.md` 的成熟度清单 10 条逐条对照
Thinkback。状态没变的条目不重复写。

| # | 条目 | 本轮结论 |
| --- | --- | --- |
| 1 | 确定性单测/集成 | 已有，本轮补齐缺口 |
| 2 | 契约测试 | 已有（L3 后端 / 仓储双实现） |
| 3 | 覆盖率门禁 | 仓库**有意**做成非阻断，理由写在 Makefile 注释里。本轮不推翻该决定，只把核心模块的覆盖率拉上去 |
| 4 | 属性测试 | 已有，但只覆盖 `keys` / `summarization` / `task_state` / `retention`。本轮补上 `recall_policy` |
| 5 | 变异审计 | 范围只有 4 个文件，且范围一变就静默失效。本轮主要战场 |
| 6 | e2e 关键旅程 | 已有 |
| 7 | 视觉 + a11y | a11y 已有；视觉基线仍缺（第四轮已列，本轮未做） |
| 8 | API 模糊测试 | 已有（schemathesis） |
| 9 | 供应链 CVE | 已有（pip-audit） |
| 10 | 防回血基线 | 已有（上一轮刚加），本轮发现它自身有一个盲区（§2.2） |
| AI | LLM 质量评测 | 已有（自包含评测 + 基线） |

对照下来只有第 5 条有结构性缺口。「核心逻辑真的被测过」这句话，全靠它撑着。

## 2. 三个门禁缺陷

### 2.1 变异范围一变，新增文件一个变异体都不生成

第四轮把「`recall_policy` 没有变异覆盖」列成了缺口。本轮把它加进
`pyproject.toml` 的 `only_mutate`，跑变异审计，结果：**变异体总数还是 361，
一个都没多**。

根因在 mutmut 的缓存判断里。`mutants/` 目录里已经有一份 `recall_policy.py`
的**普通副本**——它此前在变异范围之外，被当作「忽略文件」拷进去的。mutmut
判断「这份文件已有变异体」的依据是**副本的 mtime 比源文件新**，满足就直接
复用那个**空的变异体集合**。

于是：新文件进范围 → 变异体数为 0 → 门禁照样绿 → 实际什么都没审计。
mutmut 不报错，`mutmut results` 也不显示异常。

修法在 `tests/eval/refresh_mutation_stats.py`：对「决定存在哪些变异体」的
配置字段（`source_paths` / `only_mutate` / `do_not_mutate`）取指纹，指纹变了
就**精确清掉**会遮蔽变异生成的残留——进范围但没有 `.meta` 的旧副本、出范围
但还留着 `.meta` 的旧记录。指纹不含测试选择列表，它不影响变异体集合，只影响
映射缓存（映射缓存本来就会被丢弃）。

修完之后变异体从 361 变成 1,054。

### 2.2 `timeout` 状态从门禁视野里消失

`check_mutation_survivors.py` 只认 `survived` 和 `no tests` 两种状态。
mutmut 的 `timeout`（测试没跑完）**直接被门禁忽略**，一类「跑不完」的变异体
无人看守。

超时既不是测试通过也不是测试失败：可能是变异体让代码死循环，也可能是墙钟
估算偏了（见 2.3）。两种都不该静默。基线现在增加 `accepted_timeouts`，超时跟
存活一样必须登记理由。

### 2.3 并行跑出假超时

12 个 `domain/keys.py` 的变异体在本轮开始时是「存活」，加了新测试后变成
「超时」。单独跑一个，它 11.9 秒跑完并正确判为存活。

mutmut 的超时公式是 `(估算用时 + 1) × timeout_multiplier`。估算用时来自空载
机器上单次测试的耗时，实际是 8 个 worker 并行跑，墙钟时间膨胀 3~5 倍；
映射不到测试的变异体估算值是 0，超时上限只有 15 秒，并行一慢就误判。

`pyproject.toml` 的 `timeout_multiplier` 从默认 15 提到 **60**，给并行留够
余量。改完之后超时数归零。

## 3. 变异审计范围：4 个文件 → 11 个文件

新增进范围的 7 个文件按「**纯函数 + 有判定分支**」收窄。这类模块的变异体才
代表真实缺陷（比较方向翻转、阈值差一、份额写反），胶水代码的变异体只会产出
「日志文案变了」这种噪音，登记成存活只会稀释门禁的信噪比。

| 文件 | 为什么在范围内 |
| --- | --- |
| `domain/recall_policy.py` | 记忆系统里逻辑最密的一块（去重 / 槽位仲裁 / 预算分配 / 逐层裁剪），此前**零覆盖** |
| `domain/classification.py` | 「敏感数据不许落库」的唯一执行点 |
| `domain/safety.py` | 写入路径最早期的内容安全闸门 |
| `domain/dedup.py` | 正文与槽位去重键 |
| `memory/task_state.py` | 「能不能重试 / 是不是终态」的唯一口径 |
| `memory/retention.py` | 物理清理作业——参数传错就是删错东西 |
| `infra/structured_logging.py` | 生产日志的词汇表，告警规则按它匹配 |

**测试选择列表**也一起扩了。原来只选 5 个文件，导致「某个分支只有别的测试
文件覆盖，但那个文件没被选中」→ 变异体白白存活。最典型的是 `type_caps`
（类型级 token 上限）：它由 `test_memory_recall_budget.py` 断言，而那个文件
不在选择列表里，于是 `recall_policy` 里 metadata/memory_type 那条分支的 13 个
变异体全部存活。

## 4. 变异审计挖出来的真实缺口

这才是本轮真正的产出。以下每一条都是**改坏源码后现有测试不会红**的行为。
每条末尾给出该模块在审计中浮出的存活变异体数，以及补完测试后的余量。

### 4.1 物理清理作业的作用域参数从未被断言

`memory/retention.py` 的 `RetentionCleaner` 是 ADR-0010 第三段：把过了宽限期
的事实真的删掉——向量、引擎侧历史、治理行一起删。这个作业的参数一旦传错，
代价不是「召回不准」，是**删错东西**：

| 变异体 | 改坏后的行为 | 后果 |
| --- | --- | --- |
| `self.user_id = user_id or None` → `= None` | 用户过滤被丢掉 | 清掉**别的用户**的记忆 |
| `purge_cutoff(now=now, grace_days=self.grace_days)` → 去掉 grace_days | 用默认 7 天 | 配置里写的 30 天形同虚设，记忆早 23 天被删 |
| `limit=self.batch_size` → `None` | 批次上限消失 | 一次清空整张表 |

`memory/retention.py` 进范围时浮出 82 个存活变异体。补完测试后它的存活数是 0。

### 4.2 数据分类的解析几乎没被断言

`domain/classification.py` 是「敏感数据不许落库」这条承诺的唯一执行点。
`raw = metadata.get("data_classification")` → `None`，分类就回落成 `personal`，
**受限数据照常落库、照常发给 LLM 和 embedding**。`domain/classification.py`
进范围时浮出 18 个存活变异体，全部集中在这一个函数上；补完后存活数是 0。

### 4.3 召回兜底分支的边界

`recall_policy._clip_by_layer` 有一段「每层至少留一条」的兜底，用全局余额给
空层补一条。`used_total = 0` 写成 `1`，余额就差一个 token，预算刚好装满时
兜底会返回**空包**——而契约要求「不返回空包」。属性测试够不着（要随机命中
精确相等），示例测试也没覆盖。

同一段兜底里的类型级上限判定还浮出 20 个变异体：兜底补进来的那条同样要受
类型上限约束，否则某一类可以借兜底把整份预算吃掉。这两类补完后，
`domain/recall_policy.py` 的存活数从 157 降到 17，剩下 17 个全部是等价变异
（详见基线文件 `tests/eval/mutation_survivors.json` 的逐条理由）。

### 4.4 缺失 score 的默认口径

去重仲裁里缺失的 `score` 当 0.0 还是 1.0，决定「没分」的记忆能不能压过
「有分」的。`candidate.score or 0.0` → `or 1.0` 这类变异体浮出 4 个，
补完测试后归零。

### 4.5 安全闸门的大小写

`content.lower()` → `content.upper()` 会让整个词表失效。词表用 `re.IGNORECASE`
编译，所以这条恰好是等价变异——但它同时说明：**词表的匹配语义从未被用例钉住**，
哪天有人去掉 IGNORECASE 就没有任何东西会红。这一类浮出 4 个存活变异体，
补完测试后剩 4 个，全部是等价变异或刻意不钉死的文案。

### 4.6 日志动词的默认参数

`StructuredLogger.decision(reason="")` 的默认值被改掉，告警规则按精确文本匹配
就会失效，而功能用例照样全绿。这要求断言用**精确相等**而不是子串包含。
这一类浮出 6 个存活变异体，补完后剩 1 个等价变异。

## 5. 新增测试

本轮新增 18 个测试文件。其中 13 个是本轮写的，另外 5 个由同一工作区里并行的
另一路写入（覆盖同样的模块，内容重叠，详见 §7）。

| 文件 | 条数 | 钉住什么 |
| --- | --- | --- |
| `test_recall_policy_properties.py` | 22 | 预算不被突破、输出恒为 L3→L2→L1、去重幂等、同槽位至多留一条、装得下就至少给一条 |
| `test_recall_policy_edge_cases.py` | 30 | 仲裁链上的具体边界：缺失 score 的默认口径、同分时的仲裁顺序、`valid_at` 的时区混用、预算刚好装满、兜底分支的类型上限与层顺序、CJK 区间端点 |
| `test_recall_policy_contract.py` | 81 | 并行写入 |
| `test_retention_cleanup_contract.py` | 14 | 物理清理的作用域参数与审计字段契约 |
| `test_retention_contract.py` | 37 | 并行写入 |
| `test_classification_policy.py` | 14 | 分类解析、存储闸门、取最严格的一级 |
| `test_classification_contract.py` | 22 | 并行写入 |
| `test_safety_gate_policy.py` | 15 | 凭据与注入的拦截，以及误杀防护 |
| `test_safety_gate_contract.py` | 27 | 并行写入 |
| `test_memory_type_profile_validation.py` | 27 | profile 的校验矩阵：未知字段、非法取值、路径穿越、`review_required` 不得吃 user 角色 |
| `test_p0_source_support_gate.py` | 22 | P0 记忆的源文本交叉校验，含 `cat` 不命中 `catastrophe`、`猫` 不命中 `招财猫` 的回归 |
| `test_service_error_mapping.py` | 17 | 领域异常 → HTTP 状态码；不认识的后端异常必须透传，不能兜底成 4xx |
| `test_structured_logging.py` | 19 | 日志动词的级别与格式，断言用精确相等 |
| `test_structured_logging_contract.py` | 6 | 并行写入 |
| `test_query_slot_matching.py` | 16 | 查询侧槽位识别与上下文准入 |
| `test_api_dependency_wiring.py` | 6 | 配置开关的降级路径：关掉之后必须走不调用该外部依赖的分支 |
| `test_database_readiness_probe.py` | 4 | 探测失败返回 `not_ready` 而不是抛异常；连接用完必须关闭 |
| `test_mutation_gate_refresh.py` | 8 | §2 三个缺陷的自检——门禁本身必须可验证 |

属性测试还需要一处配套改动：`tests/conftest.py` 注册了 hypothesis 的 profile，
把样例数统一收到一处（正常 100 个，变异审计下 20 个）。变异审计下压到 20 是
必须的：每个变异体都要把选中的测试整套重跑一遍，1,054 个变异体 × 200 个样例
= 门禁跑几小时。20 个样例足以杀掉真变异体——杀不掉的那些是「样例再多也杀不掉」
的等价变异，本来就该登记为存活。

## 6. 覆盖率

`make coverage` 仍然是**非阻断**的观测水位，这是仓库自己的决定，理由写在
Makefile 注释里（行覆盖率衡量「这行被执行过」，不衡量「结果对不对」）。本轮
不动这个决定，只把核心模块的缺口补上。

核心模块现在都是满的：`domain/recall_policy.py` 95% → 100%、
`domain/classification.py` 92% → 100%、`domain/safety.py` 100%、
`domain/errors.py` 73% → 100%、`domain/slots/query_slots.py` 81% → 100%、
`api/errors.py` 81% → 100%、`infra/database/engine.py` 50% → 100%、
`infra/structured_logging.py` 63% → 100%、`memory/retention.py` 100%、
`memory/task_state.py` 100%。

## 7. 未做与待拍板

| 项 | 为什么没做 | 建议 |
| --- | --- | --- |
| `memory/service.py` 的 210 行未覆盖 | 6,000+ 行里多为编排胶水；按纯函数标准收窄后，剩下的是「这行跑过没」的覆盖，不是「结果对不对」的覆盖 | 变异审计扩到 service.py 的关键方法需要 mutmut 支持函数级定向（本版只支持文件级 glob），属工具能力问题 |
| `memory/backends/mem0_library.py` 71 行未覆盖 | 需要真实 mem0 库；fake 后端与真实后端的契约差异已由 L3 契约套件覆盖 | 真栈门禁进 CI（第四轮已列，需要 CI 基础设施改动） |
| 同一工作区里并行写入的 5 个测试文件与本轮的 13 个内容重叠 | 这 5 个文件不是本轮写的，覆盖同样的模块与行为；合并要逐条比对，而且另一路可能仍在改 | **上线前合并**：重复用例让套件变慢、维护面翻倍。建议按模块各留一份 |
| 视觉基线（管理台截图） | 需要先有稳定的截图基线文件与 CI 产物留存策略 | 建议做，不急——a11y 已有 |
| 租户鉴权 | 改的是对外契约与信任模型 | **仍待拍板**：多租户共用一套部署 vs 各企业各自部署 |

## 8. 复核方式

```bash
make check            # 格式 / lint / 类型 / 单测 / 用例基线 / 变异审计 / 评测
make coverage         # 覆盖率观测（非阻断）
uv run --group dev python tests/eval/check_mutation_survivors.py   # 变异基线
```
