# AI虚拟社交记忆服务 P0 压测评测方案

本文档说明 Thinkback 记忆服务 P0 阶段怎么压测、看哪些指标、哪些失败会拦截，以及什么情况下可以说“通过”。

它不是行业通用 SLO，也不是生产容量承诺。它是当前 P0 阶段的工程评测方案，重点拦截三类风险：记错、串记忆、删不掉。

## 0. 先看结论

截至 2026-05-09，本轮真实 P0 压测在当前门禁下通过：baseline、stress、spike、10 分钟 P0 soak、故障注入、代表性样本回放均已通过。

| 结论项 | 判断 | 说明 |
| --- | --- | --- |
| 指标是否业界主流 | 是 | 延迟、错误、吞吐、饱和是 SRE 常规指标；recall、precision、topK、MRR 是检索评测常规指标。 |
| 是否过度严格 | 未发现 | P0 硬门禁只压核心质量、语义失败和 recall 尾延迟；多数资源指标是观察项。 |
| 是否过度设计 | 未发现 | 删除残留、冲突污染、重复 active、跨角色泄漏是记忆服务特有风险，不是额外堆指标。 |
| 是否有遗漏 | 有少量非阻塞缺口 | 资源饱和观测还未固定成完整报告；6 小时以上长稳建议在放量前继续跑。 |
| 当前 P0 生产前压测 | 通过 | 以 10 分钟 soak 为 P0 门禁，详见 `p0-preprod-summary-20260509T175339-bcb19d4c`。 |
| 线上长期稳定 | 不能声明 | 还需要灰度、真实流量、监控告警和持续 SLO。 |

一句话口径：

> Thinkback P0 记忆主链路已通过当前工程门禁。
> 指标设计符合主流压测和检索评测方法；记忆域指标属于必要补充，不是过度设计。
> 它还不是线上长期稳定承诺。

## 1. 读前术语

| 术语 | 人话解释 |
| --- | --- |
| append / recall | append 是写入记忆；recall 是根据问题召回相关记忆。 |
| L1 / L2 / L3 | L1 是当前会话缓存；L2 是阶段摘要；L3 是长期记忆。 |
| active / dirty | active 表示当前有效；dirty 表示摘要受删除或修正影响，不能直接拿来回答。 |
| source_refs | 一条长期记忆来自哪些会话和轮次。删除、重建时要靠它避免旧事实复活。 |
| dev gate / release gate / preprod gate | dev gate 是日常开发门禁；release gate 是发布候选检查；preprod gate 是生产前压测门禁。 |
| top10 / top_k | recall 最多返回的前 N 条 L3 记忆。本文 P0 质量指标默认看前 10 条。 |
| p95 / p99 | 尾延迟。p95 表示 95% 请求不超过该耗时；p99 表示 99% 请求不超过该耗时。 |

## 2. 为什么不是只看 HTTP 200

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

## 3. 指标是否主流、是否过度

| 指标组 | 判断 | 原因 |
| --- | --- | --- |
| 延迟、错误、吞吐、饱和 | 主流 | 对齐 SRE “四个黄金信号”：latency、traffic、errors、saturation。 |
| p95 / p99 | 主流 | 平均值会隐藏尾部慢请求；交互链路更需要看尾延迟。 |
| smoke / baseline / stress / spike / soak | 主流 | 压测常见分型，不同类型暴露不同风险。 |
| recall@10、precision@10、top1、MRR | 主流 | 检索和排序系统常用评测口径。 |
| 冲突污染、删除残留、重复 active、跨角色泄漏 | 域内必要 | 这些指标对应记忆产品的核心失败，不是普通 HTTP 服务能覆盖的内容。 |
| `10/50/100` 并发数字 | 工程探针 | 不是行业标准，也不是线上容量承诺；真实流量稳定后要重新校准。 |

当前没有发现过度严格或过度设计。

真正要避免的是另一种风险：短压测通过后，把长期稳定、资源余量和线上流量适配也一并默认通过。

## 4. 结论边界

| 层级 | 要证明什么 | 必看内容 | 允许结论 |
| --- | --- | --- | --- |
| P0 dev gate | 主链路无明显记错、串记忆、删不掉。 | 10 并发 recall、3 并发 append、质量用例、删除重建。 | P0 主链路短压测通过。 |
| Release gate | 发布候选质量是否稳定。 | 多轮 dev gate、表达漂移回归、50 并发代表性压测、baseline。 | 具备 P0 release 候选质量。 |
| P0 preprod gate | 容量、短稳、故障韧性、样本代表性。 | stress、spike、10 分钟 P0 soak、故障注入、样本回放。 | 达到 P0 生产前压测标准。 |
| 线上长期稳定 | 真实环境持续可靠。 | 灰度发布、真实样本、资源趋势、告警、持续 SLO。 | 线上长期稳定。 |

## 5. 当前真实压测证据

| 阶段 | 结论 | 证据 |
| --- | --- | --- |
| P0 short / dev gate | 通过 | `p0-short-20260509T145319-c9b8eeeb`，30 并发 recall p95=41ms，质量指标全过。 |
| baseline | 通过 | `p0-duration-baseline-20260509T134848-d3fab670`，600.010s，worst recall p95=9ms。 |
| stress | 通过 | `p0-duration-stress-20260509T140724-f66de6e7`，995.161s，worst recall p95=140ms。 |
| spike | 通过 | `p0-spike-full-20260509T141020-d0c53d5c`，10 -> 100 -> 10 恢复曲线通过。 |
| P0 soak | 通过 | `p0-duration-soak-20260509T145809-56124c2a`，600.002s，两轮 30 并发 recall + 10 并发 append 均通过。 |
| fault injection | 通过 | `p0-fault-injection-20260509T143431-cd67cd5d`，Mem0/Qdrant/Postgres/Redis 故障均可观测并可恢复。 |
| representative replay | 通过 | `p0-representative-replay-20260509T141037-7190dddc`，覆盖 slot_conflict、negative_control、isolation、delete_rebuild。 |
| preprod summary | 通过 | `p0-preprod-summary-20260509T175339-bcb19d4c`，`production_precheck_passed=true`。 |
| 6 小时以上长稳尝试 | 暴露风险 | `p0-duration-soak-20260509T173847-4dc94d57` 约 8399s 后第 27 轮失败；不阻塞当前 10 分钟 P0 门禁。 |

主要报告：

- [p0-final-20260509](reports/p0-final-20260509.md)
- [p0-preprod-summary-20260509T175339-bcb19d4c](reports/p0-preprod-summary-20260509T175339-bcb19d4c.md)
- [p0-production-standard-audit-20260509T2304](reports/p0-production-standard-audit-20260509T2304.md)

## 6. 自动硬门禁

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

## 7. 用例硬门禁

这些风险会拦 P0，但当前主要通过固定 case、断言或分段结果覆盖，不一定都有可靠的独立指标字段。

| 风险 | 当前覆盖方式 | 门禁口径 |
| --- | --- | --- |
| 核心槽位错误 | `slot_conflict`、`negative_control`、`isolation` 等 case。 | 核心 case 必须全过。 |
| 单条记忆删除残留 | `post_delete` 分段的删除 case。 | 本轮样本中不得召回被删事实。 |
| 重建后旧事实复活 | 删除后执行 rebuild，再跑 `post_delete` case。 | 本轮样本中不得复活被删事实。 |
| 跨角色串记忆 | 隔离 case 覆盖同 user 不同 character。 | 本轮样本中不得串角色记忆。 |
| 现实/剧情混入 | 隔离 case 覆盖 real_user 与 roleplay。 | 本轮样本中不得互相污染。 |
| append 探针失败 | `append_probe` 分段。 | 并发 append 探针失败数必须为 0。 |
| 非瞬时失败 | `request_metrics.non_transient_failure_count` 和分段异常。 | 本轮 dev gate 不接受不可解释失败。 |

这里的 `0` 是“本轮评测样本内不得出现”。它不是线上长期错误率 SLO，也不能替代生产监控。

## 8. 短压测延迟门禁

P0 dev gate 只把 recall 延迟作为硬门禁，因为 recall 在交互链路上。append、delete、rebuild 延迟会报告，但不作为 P0 dev gate 的独立延迟门禁。

| 指标 | P0 门禁 | 为什么要拦 |
| --- | --- | --- |
| 10 并发 `recall p95` | `<= 1500ms`。 | 大多数交互不能明显卡顿。 |
| 10 并发 `recall p99` | `<= 3000ms`。 | 极慢请求不能破坏体验。 |

## 9. 核心指标怎么理解

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

简单记法：`recall_at_10` 看“找没找到”，`precision_at_10` 看“有没有夹带错事实”，`top1_hit_rate` 和 `mrr` 看“正确答案排得靠不靠前”。

## 10. 观察指标

这些指标用于定位问题、观察趋势，或在生产前补齐。它们不是 P0 dev gate 的硬门禁。

| 指标 | 人话解释 | 当前处理 |
| --- | --- | --- |
| `item_precision_at_10` | top10 中每一条记忆有多少是真的相关。 | 先记录基线；无关记忆过多时再设硬门禁。 |
| `top1_hit_rate` / `mrr` | 正确答案排得靠不靠前。 | 观察排序质量。 |
| `append_success_rate` / `recall_success_rate` / `delete_success_rate` / `rebuild_success_rate` | 各操作是否成功。 | 目前由分段结果覆盖。 |
| `transient_retry_rate` | 502、503、504、timeout 等瞬时失败触发重试的比例。 | 自动报告；过高时排查 Mem0/OpenAI/Qdrant。 |
| `http_5xx_rate` / `timeout_rate` | 对外 5xx 和超时比例。 | 长测中观察趋势，线上 SLO 前再定。 |
| `throughput_recall_rps` / `throughput_append_rps` | 在质量通过时能承载多少吞吐。 | 生产前记录基线。 |
| `cpu_utilization_pct` / `memory_rss_mb` | CPU 和内存是否接近饱和或持续增长。 | 待固化报告。 |
| `db_pool_in_use` / `redis_pool_in_use` | 数据库和 Redis 连接池是否耗尽。 | 待固化报告。 |
| `l3_pending_write_tasks` / `l3_available_capacity` | L3 后台抽取队列是否长期积压。 | 已有接口，待沉淀趋势报告。 |
| `qdrant_request_latency_ms` / `external_rate_limit_count` | Qdrant 耗时和外部依赖限流。 | 待固化报告。 |

## 11. P0 场景覆盖

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

## 12. 压测类型和持续时间

| 类型 | 目标 | P0 建议持续时间 | 结论边界 |
| --- | --- | --- | --- |
| smoke | 单用户完整链路。 | 1 轮。 | 只证明链路可跑。 |
| short | 10 并发 recall + 3 并发 append。 | 1-5 分钟。 | P0 dev gate。 |
| baseline | 10 并发 recall + 10 并发 append。 | 10-15 分钟。 | release gate 参考。 |
| stress | 50 并发 recall + 10 并发 append。 | 15-30 分钟。 | 生产前容量参考。 |
| spike | 10 -> 100 -> 10 并发 recall。 | 5-10 分钟。 | 看峰值后是否恢复。 |
| P0 soak | 30 并发 recall + 10 并发 append。 | 10 分钟。 | 当前 P0 生产前门禁。 |
| 长稳 soak | 稳定生产负载。 | 6 小时以上。 | 放量前建议证据，不阻塞当前 P0 门禁。 |

当前 10、50、100 并发是工程探针，不是线上容量承诺。有真实流量后，要按峰值 RPS、读写比例、部署副本数和外部依赖限流重新校准。

soak 不是无节流地循环跑 short suite。

默认每 5 分钟跑一轮 30 并发 recall + 10 并发 append 探针，并持续观察 L3 后台队列、错误率、延迟和资源是否漂移。

如果去掉间隔后触发 `l3 background queue full`，结论应写成“持续写入把 L3 后台抽取打满”。

这是容量或背压风险，不应通过放宽语义正确性指标来掩盖。

## 13. 执行方法

### 13.1 本地真实短压测

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

### 13.2 生产前阶段汇总

P0 生产前阶段包括：baseline、stress、spike、10 分钟 P0 soak、fault injection、representative replay。

```bash
PYTHONPATH=src .venv/bin/python script/real_mem0_p0_preprod_pressure.py \
  --output-dir docs/reports \
  --phase-report baseline=<baseline.json> \
  --phase-report stress=<stress.json> \
  --phase-report spike=<spike.json> \
  --phase-report soak=<soak.json> \
  --phase-report fault_injection=<fault_injection.json> \
  --phase-report representative_replay=<representative_replay.json>
```

### 13.3 10 分钟 P0 soak

```bash
env THINKBACK_API_URL=${THINKBACK_API_URL:?set THINKBACK_API_URL} \
PYTHONPATH=src .venv/bin/python script/real_mem0_p0_preprod_pressure.py \
  --duration-phase soak \
  --target-duration-seconds 600 \
  --iteration-interval-seconds 300 \
  --report-dir docs/reports \
  --output-dir docs/reports
```

队列状态可以通过接口确认：

```bash
curl ${THINKBACK_API_URL}/memory/l3/background-status
```

## 14. 报告内容要求

短压测 Markdown 报告至少包含：

| 部分 | 内容 |
| --- | --- |
| 结论 | 通过/未通过、suite_id、执行时间、失败分段。 |
| 自动硬门禁 | case、recall、precision、冲突污染、误召回、重复 active。 |
| 分段结果 | quality、concurrent_recall、append_probe、post_delete。 |
| 延迟 | append/recall/delete/rebuild 的 p95。 |
| 限制 | 样本覆盖、spike、soak、故障注入、代表性回放的执行状态说明。 |

生产前报告还应补充：

| 部分 | 内容 |
| --- | --- |
| 资源与环境 | 测试环境、服务副本数、数据库和缓存配置、CPU、内存、连接池、队列等待、外部依赖限流。 |
| 阶段门禁 | baseline、stress、spike、soak、fault_injection、representative_replay 的通过状态。 |
| 未完成项 | 未执行、部分执行、未满足持续时间或未自动化的字段。 |

尚未自动化的字段写 `null`，并在 `metric_automation_status` 中标明状态。

稳定 JSON 字段见 [P0 压测报告字段规范](AI虚拟社交记忆服务P0压测报告字段.md)。

## 15. 失败处置

1. 保存 `run_id`、case 名、query、召回文本、active memory 列表和 Mem0 返回样本。
2. 表达漂移先补回归测试，再修归一化或冲突槽位。
3. 隔离泄漏优先查作用域和 context 过滤，不通过降低 top_k 掩盖。
4. 删除复活优先查 source_refs、deleted refs、rebuild 输入范围和 active/superseded 状态。
5. 外部瞬时失败要记录 retry 指标；未重试成功不能算质量通过。
6. 阈值调整必须写进本文档，不能只改脚本让报告变绿。
7. 延迟失败要同时看 Mem0、OpenAI、Qdrant、Postgres、连接池和服务端排队。

## 16. 允许对外怎么说

| 说法 | 允许条件 |
| --- | --- |
| P0 主链路短压测通过 | `script/real_mem0_p0_short_pressure.py` 通过并生成报告。 |
| P0 质量回归通过 | `script/real_mem0_quality_regression.py` 通过。 |
| P0 release gate 通过 | 多轮重复、表达漂移回归、50 并发代表性压测、baseline 均有报告且风险可接受。 |
| P0 达到生产前压测标准 | release gate、stress、spike、10 分钟 P0 soak、故障注入和样本回放都通过。 |
| 线上长期稳定 | 灰度发布、真实样本回放、资源趋势、监控告警和持续 SLO 都稳定。 |

未执行、部分执行、未满足持续时间或未自动化的故障注入、资源观测和长稳验证，只能写成“未执行/部分执行/待补齐/风险项”。

## 附录 A. 指标自动化状态

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

- Google SRE《Monitoring Distributed Systems》：https://sre.google/sre-book/monitoring-distributed-systems/
- Grafana k6 load test types：https://grafana.com/docs/k6/latest/testing-guides/automated-performance-testing/
- Grafana k6 soak testing：https://grafana.com/blog/soak-testing/
- Stanford IR ranked retrieval evaluation：https://nlp.stanford.edu/IR-book/html/htmledition/evaluation-of-ranked-retrieval-results-1.html
- Locust documentation：https://docs.locust.io/
