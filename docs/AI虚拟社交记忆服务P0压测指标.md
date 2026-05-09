# AI虚拟社交记忆服务 P0 压测评测方案

本文档说明 Thinkback 记忆服务 P0 阶段怎么压测、看哪些指标、哪些失败会拦截，以及什么情况下可以说“通过”。

它不是行业通用 SLO，也不是生产容量承诺。它是当前 P0 阶段的工程评测方案，重点拦截三类风险：记错、串记忆、删不掉。

建议阅读顺序：

1. 想先看结论：读 1、2、4。
2. 想理解指标：读 1、5、6、7。
3. 想执行压测：读 9、10、11。
4. 想对齐报告字段：读 [P0 压测报告字段规范](AI虚拟社交记忆服务P0压测报告字段.md)。

## 0. 读前术语

| 术语 | 在本文里的意思 |
| --- | --- |
| append / recall | append 是写入记忆；recall 是根据问题召回相关记忆。 |
| L1 / L2 / L3 | L1 是当前会话缓存；L2 是阶段摘要；L3 是长期记忆。 |
| active / dirty | active 表示当前有效；dirty 表示摘要受删除或修正影响，不能直接拿来回答。 |
| source_refs | 一条长期记忆来自哪些会话和轮次。删除、重建时要靠它避免旧事实复活。 |
| dev gate / release gate | dev gate 是日常开发门禁；release gate 是发布候选前的更完整检查。 |
| top10 / top_k | recall 最多返回的前 N 条 L3 记忆。本文 P0 质量指标默认看前 10 条。 |
| p95 / p99 | 尾延迟。p95 表示 95% 请求不超过该耗时；p99 表示 99% 请求不超过该耗时。 |

## 1. 这些指标是否主流

| 类别 | 判断 | 说明 |
| --- | --- | --- |
| 延迟、错误、吞吐、饱和 | 主流，不过度 | SRE 和压测常见指标。 |
| recall、precision、top1、MRR | 主流，不过度 | 信息检索和排序常见口径。 |
| `item_precision_at_10` | 偏细，先观察 | 防止 top10 混入大量无关记忆。 |
| 冲突、删除、重复 active、dirty/source_refs | 域内必要 | 用来拦截记错、串记忆、删不掉。 |
| smoke、baseline、stress、spike、soak | 主流，不过度 | 压测分型常规做法。 |
| `10/50/100` 并发数字 | 工程探针 | 不是行业标准，也不是线上容量承诺。 |

结论很直接：

- 指标大类是业界主流的：服务稳定性看延迟、错误、吞吐、饱和；检索质量看 recall、precision、top1、MRR。
- 记忆服务额外看删除残留、冲突污染、跨角色泄漏，不是行业通用指标，但不是过度设计；它们正好对应“记错、串记忆、删不掉”。
- 真正作为 P0 硬门禁的只有核心质量、语义失败和 recall 延迟；资源、吞吐、外部依赖耗时大多是生产前观察项。
- 当前没有发现“指标过度严格到不合理”的问题。
- 更大的风险是反过来：如果不跑 soak 和资源观测，短压测很容易把长期稳定性误判成已经达标。

当前仍需补齐的不是更多语义指标，而是三类证据：

| 缺口 | 为什么重要 | 当前状态 |
| --- | --- | --- |
| 6 小时正式 soak | 证明长期稳定、队列不持续积压、外部依赖不周期性失败。 | 10 分钟探针通过；6 小时正式门禁未通过。 |
| 资源饱和观测 | 证明 CPU、内存、连接池、队列和 Qdrant 没有逼近上限。 | L3 队列已有接口；其他资源观测待固化。 |
| 真实流量校准 | 证明 10/50/100 并发和读写比例贴近线上。 | 当前仍是 P0 工程探针，不能当线上容量承诺。 |

## 2. 当前结论

P0 dev gate 只拦核心风险。生产前完整压测还要额外证明容量、稳定性、故障韧性和样本代表性。

截至 2026-05-09 23:00 CST，当前证据快照如下：

| 结论项 | 当前判断 | 证据 |
| --- | --- | --- |
| P0 主链路短压测 | 已通过 | 已核对 30 并发短探针样本 `p0-short-20260509T145319-c9b8eeeb` 通过，`concurrent_recall recall p95=41ms`。 |
| Release 候选质量 | 主要证据已满足，仍建议扩大样本 | baseline、stress、spike、故障注入、代表性样本回放均已有通过报告。 |
| 生产前完整压测 | 未通过 | 生产前汇总失败在 `soak`；10 分钟探针通过，但未满足 6 小时最低门禁。 |
| 线上长期稳定 | 不能声明 | 还需要灰度、真实样本、监控告警和持续 SLO。 |

主要证据：

- 已核对最终报告：[p0-final-20260509](reports/p0-final-20260509.md)
- 已核对生产前汇总：[p0-preprod-summary-20260509T145827-67b59566](reports/p0-preprod-summary-20260509T145827-67b59566.md)
- 10 分钟 baseline：[p0-duration-baseline-20260509T134848-d3fab670](reports/p0-duration-baseline-20260509T134848-d3fab670.md)
- 15 分钟 stress：[p0-duration-stress-20260509T140724-f66de6e7](reports/p0-duration-stress-20260509T140724-f66de6e7.md)
- 100 并发 spike：[p0-spike-full-20260509T141020-d0c53d5c](reports/p0-spike-full-20260509T141020-d0c53d5c.md)
- 故障注入：[p0-fault-injection-20260509T143431-cd67cd5d](reports/p0-fault-injection-20260509T143431-cd67cd5d.md)
- 代表性样本回放：[p0-representative-replay-20260509T141037-7190dddc](reports/p0-representative-replay-20260509T141037-7190dddc.md)
- 10 分钟 soak 探针：[p0-duration-soak-20260509T145809-56124c2a](reports/p0-duration-soak-20260509T145809-56124c2a.md)
- 6 小时 soak 尝试失败：[p0-duration-soak-20260509T143811-6b5f37c7](reports/p0-duration-soak-20260509T143811-6b5f37c7.md)

10 分钟 soak 探针只能说明当前节奏下两轮短套件通过，不能替代 6 小时正式 soak。生产前结论以 `p0-preprod-summary-*` 为准。

如果新的 6 小时 soak 正在运行，运行中的短压测子报告只作为过程证据；生产前结论以完成后的阶段汇总报告为准。

## 3. 为什么不能只看 HTTP 200

传统接口压测主要看延迟、流量、错误和资源饱和。记忆服务还要判断“回答能不能拿到正确记忆”。

一个请求返回 200，只说明接口有响应，不代表记忆系统正确：

| 风险 | 例子 | 为什么严重 |
| --- | --- | --- |
| 记错 | 用户把猫名从“团子”改成“麻薯”，系统仍召回“团子”。 | 角色会长期使用旧事实。 |
| 串记忆 | A 角色知道了 B 角色里的猫名。 | 破坏角色隔离和用户信任。 |
| 删不掉 | 删除猫名后，重建又把它召回。 | 删除语义失效，后续很难治理。 |

所以 P0 同时看两类指标：

| 类型 | 关注点 | P0 做法 |
| --- | --- | --- |
| 业务正确性 | 记忆是否正确、隔离、可删除。 | dev gate 直接拦截。 |
| 工程稳定性 | 延迟、错误、重试、并发、资源。 | recall 延迟拦 dev gate，其余分层观察或生产前补齐。 |

## 4. 结论边界

不要把一次短压测通过写成“生产可上线”。本文把结论分三层：

| 层级 | 要证明什么 | 必看内容 | 允许结论 |
| --- | --- | --- | --- |
| P0 dev gate | 主链路无明显记错、串记忆、删不掉。 | 10 并发 recall、3 并发 append、质量用例、删除重建。 | P0 主链路短压测通过。 |
| Release gate | 发布候选质量是否稳定。 | 多轮 dev gate、表达漂移回归、50 并发代表性压测、baseline。 | 具备 P0 release 候选质量。 |
| 生产前压测 | 容量、长稳、故障韧性、样本代表性。 | stress、spike、soak、故障注入、样本回放、资源观测。 | 达到生产前压测标准。 |

线上长期稳定还需要灰度发布、真实样本回放、监控告警和持续 SLO。它不是本文档能单独证明的结论。

## 5. P0 dev gate：真正拦截什么

本节是日常开发最重要的部分。为了避免“表里写了硬门禁，但脚本没有独立字段”的误解，这里把门禁拆成三类。

### 5.1 自动硬门禁

这些指标当前已有独立字段，并会参与失败判定。

| 指标 | 人话解释 | P0 门禁 | 失败含义 |
| --- | --- | --- | --- |
| `case_pass_rate` | 所有 P0 用例中通过的比例。 | `>= 0.98`，短压测小样本按全过理解。 | 小用例集里有明显失败。 |
| `recall_at_10` | 问一个已知事实，前 10 条 L3 里是否找到了正确答案。 | `>= 0.95`。 | 系统“找不到”。 |
| `precision_at_10` | 前 10 条里既有正确答案，又没有禁用旧事实。 | `>= 0.95`。 | 系统“找不准”。 |
| `conflict_pollution_rate` | 召回里是否混入旧值、纠正前的值或冲突值。 | `<= 0.02`。 | 新旧事实同时出现。 |
| `false_positive_rate` | 不知道的事实是否被系统瞎补。 | `<= 0.02`。 | 系统把无关记忆当答案。 |
| `duplicate_active_rate` | 同一槽位是否同时存在多条 active 记忆。 | `0`。 | 同一事实多个版本并存。 |

样本量小时要按实际 case 数理解比例。比如 20 个 case 下，`case_pass_rate >= 0.98` 实际等价于全部通过。

### 5.2 用例或分段硬门禁

这些风险会拦 P0，但当前主要通过固定 case、断言或分段结果覆盖，不一定都有可靠的独立指标字段。

| 风险 | 当前覆盖方式 | 门禁口径 |
| --- | --- | --- |
| 核心槽位错误 | `slot_conflict`、`negative_control`、`isolation` 等 case。 | 核心 case 必须全过。 |
| 单条记忆删除残留 | `post_delete` 分段的 `deleted-cat-not-recalled` case。 | 本轮样本中不得召回被删事实。 |
| 重建后旧事实复活 | 删除后执行 rebuild，再跑 `post_delete` case。 | 本轮样本中不得复活被删事实。 |
| 跨角色串记忆 | 隔离 case 覆盖同 user 不同 character。 | 本轮样本中不得串角色记忆。 |
| 现实/剧情混入 | 隔离 case 覆盖 real_user 与 roleplay。 | 本轮样本中不得互相污染。 |
| append 探针失败 | `append_probe` 分段。 | 并发 append 探针失败数必须为 0。 |
| 非瞬时失败 | `request_metrics.non_transient_failure_count` 和分段异常。 | 本轮 dev gate 不接受不可解释失败。 |

这里的 `0` 是“本轮评测样本内不得出现”。它不是线上长期错误率 SLO，也不能替代生产监控。

### 5.3 短压测延迟门禁

P0 dev gate 只把 recall 延迟作为硬门禁，因为 recall 在交互链路上。append、delete、rebuild 延迟会报告，但不作为 P0 dev gate 的独立延迟门禁。

| 指标 | P0 门禁 | 为什么要拦 |
| --- | --- | --- |
| 10 并发 `recall p95` | `<= 1500ms`。 | 大多数交互不能明显卡顿。 |
| 10 并发 `recall p99` | `<= 3000ms`。 | 极慢请求不能破坏体验。 |

## 6. 核心指标怎么理解

下面用同一个例子解释几个容易混淆的指标，并说明每个指标的分母是什么。

假设用户先说“我的猫叫团子”，后来纠正为“我的猫叫麻薯”。现在问：“用户的猫叫什么？”

| 指标 | 分母 | 怎么看 | 例子 |
| --- | --- | --- | --- |
| `case_pass_rate` | 全部 P0 case。 | 每个 case 是否通过自己的断言。 | 20 个 case 失败 1 个，分数是 19/20。 |
| `recall_at_10` | 当前脚本按全部 case 计算。 | 正样本要找到当前事实；负样本要保持干净。 | 猫名 case top10 有“麻薯”算通过。 |
| `precision_at_10` | 当前脚本按全部 case 计算。 | 找到当前事实，且不夹带旧事实。 | top10 同时有“麻薯”和“团子”算失败。 |
| `conflict_pollution_rate` | 全部 P0 case。 | 有多少 case 召回了旧值或冲突值。 | 纠正后仍召回“团子”就计入污染。 |
| `false_positive_rate` | 负样本 case。 | 问未知事实时是否瞎补。 | 问“鸟叫什么”却返回猫狗记忆就计入误召回。 |
| `duplicate_active_rate` | active 记忆条数。 | 同一槽位是否有多个 active 版本。 | 猫名同时 active “团子”和“麻薯”算重复。 |
| `top1_hit_rate` | 正样本 case。 | 第一条结果是不是正确事实。 | 第 1 条是“猫叫麻薯”算通过。 |
| `mrr` | 正样本 case。 | 正确答案越靠前分数越高。 | 正确答案第 1 条得 1 分，第 5 条得 1/5 分。 |
| `item_precision_at_10` | top10 返回的 L3 条目数。 | top10 每一条有多少是真的相关。 | 10 条里 8 条相关，分数是 0.8。 |

简单记法：

- `recall_at_10` 看“找没找到”。
- `precision_at_10` 看“找到了，但有没有夹带旧错事实”。
- `item_precision_at_10` 看“返回的每一条是不是都相关”。
- `top1_hit_rate` 和 `mrr` 看“正确答案排得靠不靠前”。

## 7. 观察指标：报告但不拦 P0 dev gate

这些指标用于定位问题、观察趋势，或在生产前补齐。它们不是 P0 dev gate 的硬门禁。

### 7.1 召回排序

| 指标 | 人话解释 | 当前处理 |
| --- | --- | --- |
| `item_precision_at_10` | top10 中每一条记忆有多少是真的相关。 | 先记录基线；无关记忆过多时再设硬门禁。 |
| `top1_hit_rate` | 第一条结果是否就是正确记忆。 | 观察排序质量。 |
| `mrr` | 正确答案排得越靠前，分数越高。 | 观察排序质量。 |
| `irrelevant_l3_per_query` | 每次查询平均混入多少无关 L3。 | 观察过滤和 top_k 是否合理。 |

### 7.2 操作可靠性

| 指标 | 人话解释 | 当前处理 |
| --- | --- | --- |
| `append_success_rate` / `recall_success_rate` / `delete_success_rate` / `rebuild_success_rate` | 各操作是否成功。 | 目前由分段结果覆盖。 |
| `transient_retry_rate` | 502、503、504、timeout 等瞬时失败触发重试的比例。 | 自动报告；过高时排查 Mem0/OpenAI/Qdrant。 |
| `http_5xx_rate` / `timeout_rate` | 对外 5xx 和超时比例。 | 长测中观察趋势，线上 SLO 前再定。 |
| `idempotency_failure_rate` | 重复操作是否产生重复副作用。 | release gate 补齐。 |
| `delete_session_residue_rate` / `delete_all_residue_rate` | 会话删除、全部删除是否有残留。 | release gate 补齐。 |
| `dirty_summary_recall_rate` / `source_ref_loss_rate` | dirty L2 是否进入 prompt、L3 来源引用是否丢失。 | release gate 或生产前补齐。 |

### 7.3 资源容量与外部依赖

| 指标 | 人话解释 | 当前处理 |
| --- | --- | --- |
| `throughput_recall_rps` / `throughput_append_rps` | 在质量通过时能承载多少吞吐。 | 生产前记录基线。 |
| `cpu_utilization_pct` / `memory_rss_mb` | CPU 和内存是否接近饱和或持续增长。 | 生产前补齐。 |
| `db_pool_in_use` / `redis_pool_in_use` | 数据库和 Redis 连接池是否耗尽。 | 生产前补齐。 |
| `l3_pending_write_tasks` / `l3_available_capacity` | L3 后台抽取队列是否长期积压。 | 生产前记录趋势；打满时按背压问题处理。 |
| `qdrant_request_latency_ms` / `external_rate_limit_count` | Qdrant 耗时和外部依赖限流。 | 生产前补齐。 |

## 8. P0 场景覆盖

P0 用例不追求“大而全”，只覆盖最容易让记忆服务失信的场景。

| 场景 | 必测内容 |
| --- | --- |
| 核心槽位纠正 | 昵称、宠物名、生日、所在地、工作状态、饮品、食物、睡眠提醒、沟通偏好。 |
| 多槽位并存 | 猫和狗不能互相覆盖，饮品和食物不能互相覆盖。 |
| 旧值污染 | 纠正后不得包含旧值，如阿鹏、团子、杭州、咖啡、汉堡、5月20日。 |
| 负样本 | 未提供鸟、兔等信息时，不得用猫狗记忆填充。 |
| 作用域隔离 | 不同 user、不同 character 不得串记忆。 |
| 剧情隔离 | 现实猫和剧情猫互不污染。 |
| 删除和重建 | 删除后不召回，重建不复活，不重复制造 active 记忆。 |
| 并发 | 并发写入和并发召回不破坏幂等、隔离和冲突收敛。 |
| 长会话 | 50/100 轮后核心槽位仍能召回当前值。 |
| 表达漂移 | Mem0 抽取出英文、转写、同义表达时仍能归一化。 |
| 故障注入 | Mem0、Qdrant、Postgres、Redis 异常均可观测，不产生静默错误。 |

核心槽位示例：

| 槽位 | 当前事实 | 旧值或冲突值 | 负样本关注点 |
| --- | --- | --- | --- |
| 昵称 | 小鹏 | 阿鹏 | 宠物名不得当昵称。 |
| 宠物名 | 猫“麻薯”、狗“豆包” | 猫旧名“团子” | 鸟、兔未知时不得填充猫狗。 |
| 地点 | 上海 | 杭州 | 地点不得被抽成宠物名。 |
| 工作状态 | 接受 Moonshot offer | 仍在评估机会 | 普通情绪不应污染工作状态。 |
| 沟通偏好 | 简洁直接建议 | 先安慰、说教 | 焦虑处理流程不能覆盖沟通偏好。 |
| 生日 | 6月1日 | 5月20日 | 日期不能被其他事件污染。 |
| 饮品/食物 | 茶、寿司 | 咖啡、汉堡 | 饮品和食物不得互相覆盖。 |
| 睡眠提醒 | 接受温和提醒 | 讨厌提醒睡觉 | 工作压力偏好不得覆盖睡眠提醒。 |
| 剧情设定 | 剧情猫“露露” | 现实猫“麻薯” | 现实追问不得召回剧情猫。 |

## 9. 压测类型

| 类型 | 目标 | 建议持续时间 | 结论边界 |
| --- | --- | --- | --- |
| smoke | 单用户完整链路。 | 1 轮。 | 只证明链路可跑。 |
| short | 10 并发 recall + 3 并发 append。 | 1-5 分钟。 | P0 dev gate。 |
| baseline | 10 并发 recall + 10 并发 append。 | 10-15 分钟。 | release gate 参考。 |
| stress | 50 并发 recall + 10 并发 append。 | 15-30 分钟。 | 生产前容量参考。 |
| spike | 10 -> 100 -> 10 并发 recall。 | 5-10 分钟。 | 看峰值后是否恢复。 |
| soak | 10-30 并发混合读写。 | 最少 6 小时，推荐 6-24 小时。 | 看泄漏和长期稳定性。 |

当前 10、50、100 并发是工程探针，不是线上容量承诺。有真实流量后，要按峰值 RPS、读写比例、部署副本数和外部依赖限流重新校准。

soak 不是无节流地循环跑 short suite。它要模拟稳定生产负载。

默认每 5 分钟跑一轮 30 并发 recall + 10 并发 append 探针，并持续观察 L3 后台队列、错误率、延迟和资源是否漂移。

如果去掉间隔后触发 `l3 background queue full`，结论应写成“持续写入把 L3 后台抽取打满”。

这是容量或背压风险，不应通过放宽语义正确性指标来掩盖。

## 10. 执行方法

### 10.1 本地真实短压测

```bash
env OPENAI_API_KEY=${OPENAI_API_KEY:?set OPENAI_API_KEY} \
POSTGRES_PORT=55432 \
POSTGRES_DATABASE=thinkback_real \
REDIS_PORT=56379 \
REDIS_PASSWORD= \
QDRANT_URL=${QDRANT_URL:?set QDRANT_URL} \
THINKBACK_API_URL=${THINKBACK_API_URL:?set THINKBACK_API_URL} \
PYTHONPATH=src .venv/bin/python script/real_mem0_p0_short_pressure.py \
  --recall-concurrency 10 \
  --recall-requests 20 \
  --append-concurrency 3 \
  --report-dir docs/reports
```

短压测至少生成：

```text
docs/reports/<suite_id>.json
docs/reports/<suite_id>.md
```

### 10.2 多轮重复压测

重复执行 10.1 的短压测命令 5 轮。5 轮中任一轮失败，都不能把 P0 质量判定为稳定。

### 10.3 生产前补齐项

- baseline：10-15 分钟。
- stress：50 并发，15-30 分钟。
- spike：10 -> 100 -> 10 并发。
- soak：最少 6 小时，推荐 6-24 小时，默认每轮间隔 300 秒。
- fault injection：覆盖 Mem0、Qdrant、Postgres、Redis。
- representative replay：覆盖当前工程构造样本，并逐步接入线上代表性样本。

如果使用 k6/Locust，只用它们产生负载；业务质量指标仍要由 Thinkback 的语义断言负责。

soak 执行示例：

```bash
env THINKBACK_API_URL=${THINKBACK_API_URL:?set THINKBACK_API_URL} \
PYTHONPATH=src .venv/bin/python script/real_mem0_p0_preprod_pressure.py \
  --duration-phase soak \
  --target-duration-seconds 21600 \
  --iteration-interval-seconds 300 \
  --report-dir docs/reports \
  --output-dir docs/reports
```

队列状态可以通过接口确认：

```bash
curl ${THINKBACK_API_URL}/memory/l3/background-status
```

## 11. 报告内容要求

短压测 Markdown 报告至少包含：

| 部分 | 内容 |
| --- | --- |
| 结论 | 通过/未通过、suite_id、执行时间、失败分段。 |
| 自动硬门禁 | case、recall、precision、冲突污染、误召回、重复 active。 |
| 分段结果 | quality、concurrent_recall、append_probe、post_delete。 |
| 延迟 | append/recall/delete/rebuild 的 p95。 |
| 限制 | 样本覆盖、soak、spike、故障注入、代表性回放的执行状态说明。 |

生产前报告还应补充：

| 部分 | 内容 |
| --- | --- |
| 资源与环境 | 测试环境、服务副本数、数据库和缓存配置、CPU、内存、连接池、队列等待、外部依赖限流。 |
| 阶段门禁 | baseline、stress、spike、soak、fault_injection、representative_replay 的通过状态。 |
| 未完成项 | 未执行、部分执行、未满足持续时间或未自动化的字段。 |

用例门禁可以写实际 case 结果，但要说明“由 case 覆盖”。

尚未自动化的字段写 `null`，并在 `metric_automation_status` 中标明状态。

稳定 JSON 字段见 [P0 压测报告字段规范](AI虚拟社交记忆服务P0压测报告字段.md)。

## 12. 失败处置

1. 保存 `run_id`、case 名、query、召回文本、active memory 列表和 Mem0 返回样本。
2. 表达漂移先补回归测试，再修归一化或冲突槽位。
3. 隔离泄漏优先查作用域和 context 过滤，不通过降低 top_k 掩盖。
4. 删除复活优先查 source_refs、deleted refs、rebuild 输入范围和 active/superseded 状态。
5. 外部瞬时失败要记录 retry 指标；未重试成功不能算质量通过。
6. 阈值调整必须写进本文档，不能只改脚本让报告变绿。
7. 延迟失败要同时看 Mem0、OpenAI、Qdrant、Postgres、连接池和服务端排队。

## 13. 结论表述口径

| 说法 | 允许条件 |
| --- | --- |
| P0 主链路短压测通过 | `script/real_mem0_p0_short_pressure.py` 通过并生成报告。 |
| P0 质量回归通过 | `script/real_mem0_quality_regression.py` 通过。 |
| P0 release gate 通过 | 多轮重复、表达漂移回归、50 并发代表性压测、baseline 均有报告且风险可接受。 |
| P0 达到生产前压测标准 | release gate、stress、spike、soak、故障注入、样本回放和资源观测都通过。 |
| 线上长期稳定 | 灰度发布、真实样本回放、监控告警和持续 SLO 都稳定。 |

未执行、部分执行、未满足持续时间或未自动化的长测、故障注入和资源观测，只能写成“未执行/部分执行/待补齐/风险项”。

## 附录 A. 指标自动化状态

| 状态 | 含义 |
| --- | --- |
| 自动门禁 | 当前脚本已有独立字段并参与失败判定。 |
| 用例门禁 | 当前脚本通过固定 case 或断言判定失败，但独立指标字段还没有完全拆出。 |
| 分段报告 | 当前脚本通过 quality、concurrent_recall、append_probe、post_delete 给出 pass/fail 和统计，独立字段待补。 |
| 自动报告 | 当前脚本已输出结果，但是否作为硬门禁取决于测试级别。 |
| 半自动 | 需要结合报告、日志或数据库记录判读。 |
| 待补齐 | P0 应关注，但当前脚本尚未完整自动化。 |

| 指标 | 当前状态 | 说明 |
| --- | --- | --- |
| `case_pass_rate` | 自动门禁 | 独立字段参与失败判定。 |
| `recall_at_10` | 自动门禁 | 独立字段参与失败判定。 |
| `precision_at_10` | 自动门禁 | 独立字段参与失败判定。 |
| `conflict_pollution_rate` | 自动门禁 | 独立字段参与失败判定。 |
| `false_positive_rate` | 自动门禁 | 独立字段参与失败判定。 |
| `duplicate_active_rate` | 自动门禁 | 独立字段参与失败判定。 |
| `critical_slot_pass_rate` | 用例门禁 | 当前由核心 case 覆盖，独立字段仍为 `null`。 |
| `delete_memory_residue_rate` | 用例门禁 | 当前由 `post_delete` 分段覆盖。 |
| `rebuild_resurrection_rate` | 用例门禁 | 当前由删除后 rebuild 和 active count 断言覆盖。 |
| `cross_character_leak_rate` | 用例门禁 | 当前由隔离 case 覆盖。 |
| `roleplay_real_mix_rate` | 用例门禁 | 当前由现实/剧情隔离 case 覆盖。 |
| `operation_success_rate` | 分段报告 | 当前通过分段 pass/fail 表达。 |
| `transient_retry_rate` | 自动报告 | 当前自动输出，用于排查瞬时失败。 |
| `http_5xx_rate` / `timeout_rate` | 半自动 | 需要结合报告或日志判读。 |
| `dirty_summary_recall_rate` / `source_ref_loss_rate` | 待补齐 | 需要独立字段或固定回归。 |
| `idempotency_failure_rate` | 待补齐 | release gate 补齐。 |

## 参考资料

- Google SRE《Service Level Objectives》：https://sre.google/sre-book/service-level-objectives/
- Google SRE《Monitoring Distributed Systems》：https://sre.google/sre-book/monitoring-distributed-systems/
- Grafana k6 thresholds：https://grafana.com/docs/k6/latest/using-k6/thresholds/
- Grafana k6 load test types：https://grafana.com/docs/k6/latest/testing-guides/test-types/
- Locust documentation：https://docs.locust.io/
