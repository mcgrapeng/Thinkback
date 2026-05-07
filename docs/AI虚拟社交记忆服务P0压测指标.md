# AI 虚拟社交记忆服务 P0 压测指标

本文档定义 Thinkback 记忆服务 P0 阶段的压测目标、指标口径、场景矩阵、执行方法、报告格式和通过门禁。它只约束首版主链路，不把 P0 扩展成 P1/P2 能力。

P0 压测采用主流工程实践，而不是照搬某个通用行业阈值。Google SRE 提供的是 SLI/SLO 方法：少量关键指标、明确测量条件、关注延迟、流量、错误和饱和度；k6 提供阈值机制和负载类型参考，Locust 提供并发用户建模方式。记忆服务还必须额外验证语义正确性。本文档里的具体阈值是 Thinkback 当前 P0 的产品与工程门禁，不宣称为行业通用标准。

参考资料：

- Google SRE《Service Level Objectives》：https://sre.google/sre-book/service-level-objectives/
- Google SRE《Monitoring Distributed Systems》：https://sre.google/sre-book/monitoring-distributed-systems/
- Grafana k6 thresholds：https://grafana.com/docs/k6/latest/using-k6/thresholds/
- Grafana k6 load test types：https://grafana.com/docs/k6/latest/testing-guides/test-types/
- Locust documentation：https://docs.locust.io/

指标状态说明：

| 状态 | 含义 |
| --- | --- |
| 自动门禁 | 当前脚本已有独立字段并参与失败判定。 |
| 用例门禁 | 当前脚本通过固定 case 或断言判定失败，但独立指标字段还没有完全拆出。 |
| 分段报告 | 当前脚本通过 quality、concurrent_recall、append_probe、post_delete 等分段给出 pass/fail 和请求统计，独立指标字段待补齐。 |
| 自动报告 | 当前脚本已输出结果，但是否作为硬门禁取决于测试级别。 |
| 半自动 | 需要结合报告、日志或数据库记录判读。 |
| 待补齐 | P0 应关注，但当前脚本尚未完整自动化。 |

## 1. P0 边界

P0 压测要回答六个问题：

| 问题 | 判断标准 |
| --- | --- |
| 写入是否可靠 | 完整轮次能写入，重复请求不产生重复记忆，Mem0 瞬时失败可观测。 |
| 召回是否准确 | 核心槽位能召回当前事实，不召回旧事实和无关事实。 |
| 隔离是否严格 | user、character、现实事实、剧情设定之间不串记忆。 |
| 删除和重建是否可靠 | 删除后不再召回，重建后不复活已删除记忆。 |
| 负载下是否稳定 | 小规模并发下不破坏召回、隔离、幂等和删除语义。 |
| 性能是否可接受 | append、recall、delete、rebuild 的延迟有 p50/p95/p99 和失败门禁。 |

P0 压测不验证以下能力：高质量 LLM 阶段摘要、完整安全分类器、线上真实大样本分布、长期语义抑制、写入栅栏和衰减治理。

P0 的核心判断不是“跑了多少请求”，而是“在真实 Mem0/OpenAI/Qdrant/PostgreSQL 链路下，核心记忆语义是否稳定正确”。压测必须同时看正确性和性能，不能只看 HTTP 200。

## 2. P0 门禁分层

P0 结论必须分层表达，避免把一次短压测通过写成生产可上线。

| 层级 | 用途 | 最小测试 | 通过条件 |
| --- | --- | --- | --- |
| P0 dev gate | 每次重要修改后的快速回归。 | 10 并发 recall + 3 并发 append 的真实短压测。 | 核心质量、删除/重建、隔离、非瞬时失败、10 并发 recall 延迟全部通过。 |
| P0 release gate | 判断 P0 主链路是否具备发布候选质量。 | 连续 5 轮 dev gate + 50 并发 recall 代表性压测。 | 5 轮无语义失败；50 并发下召回质量不退化；延迟若未过门禁，必须作为发布风险处理。 |
| 生产前预检 | 上线前稳定性与容量验证。 | baseline、stress、spike、soak、故障注入、代表性样本回放。 | 全部有报告且通过；仍不等同于线上长期稳定。 |

最近一次 Thinkback 最终报告显示：P0 dev gate 已通过；50 并发代表性压测召回准确率通过，但 `recall p95=1808ms` 超过 1500ms 门禁，因此不能声明 P0 release gate 完全通过。

这些门禁不是“行业统一数值”。行业实践提供的是方法：定义少量关键 SLI、写清测量条件、区分负载类型、把阈值接入自动失败；具体数值要由 Thinkback 的产品体验、依赖链路和历史基线决定。

## 3. 核心质量指标

| 指标 | 口径 | P0 目标 | 适用层级 | 状态 |
| --- | --- | --- | --- | --- |
| `critical_slot_pass_rate` | 昵称、宠物、地点、生日、饮品、食物、睡眠提醒、剧情隔离等 P0 核心槽位 case 的通过率。 | 当前口径是“核心槽位 case 必须全过”；独立聚合字段待补齐。 | dev gate / release gate | 用例门禁。 |
| `case_pass_rate` | 所有 P0 case 中通过的比例。 | `>= 0.98`，短压测推荐 `1.0`。 | dev gate / release gate | 自动门禁。 |
| `recall_at_10` | 正样本 case 的期望事实是否出现在 top10 L3 召回中。 | `>= 0.95`，短压测推荐 `1.0`。 | dev gate / release gate | 自动门禁。 |
| `precision_at_10` | case 级精确率：命中期望事实且不包含禁用事实的比例。 | `>= 0.95`，短压测推荐 `1.0`。 | dev gate / release gate | 自动门禁。 |
| `item_precision_at_10` | item 级精确率：top10 L3 item 中相关 item 的比例。 | 先记录基线；若 top10 长期混入无关 L3，release 前再设硬门禁。 | release gate | 自动报告。 |
| `top1_hit_rate` | 正样本第一个 L3 结果是否命中目标事实。 | 先记录基线，用于观察排序质量，不作为 P0 dev gate 硬门禁。 | release gate | 自动报告。 |
| `mrr` | Mean Reciprocal Rank，目标事实首次出现排名的倒数均值。 | 先记录基线，用于观察排序质量，不作为 P0 dev gate 硬门禁。 | release gate | 自动报告。 |
| `conflict_pollution_rate` | 召回结果包含旧值、被纠正值或同槽位冲突值的 case 比例。 | `<= 0.02`。 | dev gate / release gate | 自动门禁。 |
| `false_positive_rate` | 负样本仍召回 L3 或命中禁用词的比例。 | `<= 0.02`。 | dev gate / release gate | 自动门禁。 |
| `irrelevant_l3_per_query` | 每个查询平均无关 L3 item 数。 | 先记录基线；短压测出现明显无关 L3 必须排查过滤和 top_k。 | release gate | 自动报告。 |
| `known_drift_regression_pass_rate` | 已发现的 Mem0 英文、同义、转写表达是否仍能被归一化和召回。 | 已知漂移样本要求全部通过；真实压测发现的新漂移必须进入固定 regression case。 | release gate | 半自动，单测已覆盖部分样本，压测报告需记录真实漂移故障。 |

`precision_at_10` 是 case 级指标，不是严格 IR item 级 precision。报告必须同时输出 `item_precision_at_10`，避免“case 过了但 top10 混入大量无关记忆”的误判。

P0 核心槽位不应只被总通过率掩盖。只要核心槽位出现漏召回、旧值污染、角色串记忆或跨角色泄漏，即使总通过率仍高，也应判定为 P0 质量失败。

## 4. 可靠性指标

| 指标 | 口径 | P0 目标 | 适用层级 | 状态 |
| --- | --- | --- | --- | --- |
| `append_success_rate` | append 分段请求成功比例。 | dev gate 要求覆盖到的 append 探针全过；独立 success rate 字段待补齐。 | dev gate / release gate | 分段报告。 |
| `recall_success_rate` | recall 分段请求成功比例。 | dev gate 要求覆盖到的 recall 探针全过；独立 success rate 字段待补齐。 | dev gate / release gate | 分段报告。 |
| `delete_success_rate` | delete 分段请求成功比例。 | dev gate 要求覆盖到的删除探针全过；独立 success rate 字段待补齐。 | dev gate / release gate | 分段报告。 |
| `rebuild_success_rate` | rebuild 分段请求成功比例。 | dev gate 要求覆盖到的重建探针全过；独立 success rate 字段待补齐。 | dev gate / release gate | 分段报告。 |
| `idempotency_failure_rate` | 重复 `round_id` / `operation_id` 导致重复写入、重复删除或状态错误的比例。 | `0`。 | release gate | 待补齐。 |
| `duplicate_active_rate` | 同一 user×character×context 下同槽位重复 active 记忆比例。 | `0`。 | dev gate / release gate | 自动门禁。 |
| `transient_retry_rate` | 502、503、504、timeout 等瞬时失败触发重试的比例。 | 先观测，过高必须排查 Mem0/OpenAI/Qdrant。 | dev gate / release gate | 自动报告。 |
| `non_transient_failure_count` | 非瞬时失败数量。 | dev gate 要求 `0`。 | dev gate / release gate | 自动门禁。 |
| `http_5xx_rate` | Thinkback 对外接口 5xx 比例。 | dev gate 要求分段无 5xx；独立比率在生产前长测中记录趋势，线上 SLO 上线前再定。 | dev gate / 生产前预检 | 半自动。 |
| `timeout_rate` | 客户端超时或 Mem0 超时比例。 | dev gate 要求分段无 timeout；独立比率在生产前长测中记录趋势，线上 SLO 上线前再定。 | dev gate / 生产前预检 | 半自动。 |

可靠性指标要按操作拆分：append、recall、delete、rebuild 不应混成一个总成功率。append 受 LLM/embedding 影响较大，可以允许延迟高，但不能允许不可观测失败。

## 5. 删除、重建和隔离指标

| 指标 | 口径 | P0 目标 | 适用层级 | 状态 |
| --- | --- | --- | --- | --- |
| `delete_memory_residue_rate` | 删除单条 L3 后，被删事实仍出现在召回结果的比例。 | `0`。 | dev gate / release gate | 用例门禁，当前由 post_delete case 覆盖。 |
| `delete_session_residue_rate` | 删除会话来源后，该会话独有事实仍被召回的比例。 | `0`。 | release gate | 待补齐。 |
| `delete_all_residue_rate` | 删除 user×character 全部记忆后仍召回任何旧 L1/L2/L3 的比例。 | `0`。 | release gate | 待补齐。 |
| `rebuild_resurrection_rate` | 重建后，已删除 L3 或其旧事实重新出现的比例。 | `0`。 | dev gate / release gate | 用例门禁，当前由 post_delete case 覆盖。 |
| `dirty_summary_recall_rate` | dirty L2 仍进入 prompt 的比例。 | `0`。 | release gate | 待补齐。 |
| `cross_user_leak_rate` | 跨 user 召回污染比例。 | `0`。 | release gate | 待补齐。 |
| `cross_character_leak_rate` | 同 user 跨 character 召回污染比例。 | `0`。 | dev gate / release gate | 用例门禁，当前由隔离 case 覆盖。 |
| `roleplay_real_mix_rate` | 现实事实与剧情设定互相混入召回的比例。 | `0`。 | dev gate / release gate | 用例门禁，当前由剧情隔离 case 覆盖。 |
| `source_ref_loss_rate` | L3 业务索引丢失来源引用的比例。 | `0`。 | release gate | 半自动抽查，独立统计待补齐。 |

删除压测必须至少覆盖三类作用域：单条记忆删除、会话来源删除、全部记忆删除。P0 阶段重点看“显式删除不再直接召回”和“重建不复活”。

## 6. 性能指标

| 指标 | 口径 | P0 目标 | 适用层级 | 状态 |
| --- | --- | --- | --- | --- |
| `p50_append_latency_ms` | append 中位延迟。 | 记录基线，用于观察版本间退化。 | release gate | 自动报告。 |
| `p95_append_latency_ms` | append p95 延迟。 | 当前观察线，连续压测形成基线后再调整；真实 LLM 抖动下先作为软门禁。 | release gate | 自动报告。 |
| `p99_append_latency_ms` | append p99 延迟。 | 当前观察线，超过必须排查 Mem0/OpenAI/Qdrant；不宣称为通用 SLA。 | release gate | 自动报告。 |
| `p50_recall_latency_ms` | recall 中位延迟。 | 记录基线，用于观察版本间退化，不作为 P0 硬门禁。 | release gate | 自动报告。 |
| `p95_recall_latency_ms` | recall p95 延迟。 | dev gate `<= 1500ms`。 | dev gate / release gate | 自动门禁。 |
| `p99_recall_latency_ms` | recall p99 延迟。 | dev gate `<= 3000ms`。 | dev gate / release gate | 自动门禁。 |
| `p95_delete_latency_ms` | delete p95 延迟。 | 当前观察线，连续压测形成基线后再调整。 | release gate | 自动报告。 |
| `p95_rebuild_latency_ms` | rebuild p95 延迟。 | 当前观察线；真实回放量变大后必须重设。 | release gate | 自动报告。 |
| `throughput_recall_rps` | 在通过质量门禁时的 recall 吞吐。 | 记录基线，后续版本不得明显退化。 | 生产前预检 | 待补齐。 |
| `throughput_append_rps` | 在通过质量门禁时的 append 吞吐。 | 记录基线，受 Mem0/LLM 限流影响。 | 生产前预检 | 待补齐。 |
| `db_memory_index_latency_ms` | recall 读取业务索引的耗时。 | 先观测，用于解释高并发尾延迟。 | 生产前预检 | 待补齐。 |
| `mem0_search_latency_ms` | 需要语义检索时 Mem0 search 的耗时。 | 先观测。 | 生产前预检 | 待补齐。 |
| `server_queue_or_threadpool_wait_ms` | API 线程池排队或服务端队列等待。 | 先观测。 | 生产前预检 | 待补齐。 |

由于 Mem0 的 `/memories` 可能同步调用 LLM 和 embedding，append 延迟受外部服务影响较大。压测报告必须同时输出请求数、重试数和非瞬时失败数，避免把外部抖动误判为召回算法问题。

性能门禁分两类：

| 类型 | 说明 |
| --- | --- |
| 短压测硬门禁 | 核心质量、recall p95/p99、5xx、timeout、删除复活、隔离泄漏、非瞬时失败。 |
| 生产前软门禁 | append p95/p99、吞吐、soak 稳定性、依赖抖动下的重试比例。软门禁失败不代表 P0 功能不可用，但必须记录风险和排查项。 |

样本规模要写进报告。20 个 recall 请求只能做快速回归，p99 更接近最大值探针；100+ 请求适合代表性 stress；长测或容量评估再看更稳定的 p95/p99。不能用一次短压测的 p99 直接推断线上长期尾延迟。

## 7. P0 场景矩阵

| 场景 | 必测内容 |
| --- | --- |
| 核心槽位纠正 | 昵称、宠物名、生日、所在地、工作状态、饮品、食物、睡眠提醒、沟通偏好。 |
| 多槽位并存 | 猫和狗不能互相覆盖，饮品和食物不能互相覆盖。 |
| 旧值污染 | 纠正后的召回不得包含旧值，如阿鹏、团子、杭州、咖啡、汉堡、5月20日。 |
| 负样本 | 未提供鸟、兔等信息时，不得用猫狗记忆填充。 |
| 作用域隔离 | 不同 user、不同 character 不得串记忆。 |
| 剧情隔离 | 现实猫和剧情猫互不污染；问剧情只召回剧情设定，问现实只召回现实事实。 |
| 删除验证 | 删除单条 L3 后，L3 不召回，相关 dirty L2 不进 prompt。 |
| 重建验证 | 重建不复活已删除记忆，不重复制造 active 记忆。 |
| 幂等 | 重复 append、delete、rebuild 不产生重复副作用。 |
| 长会话 | 50/100 轮后核心槽位仍能召回当前值。 |
| 并发 | 并发写入和并发召回不破坏幂等、隔离和冲突收敛。 |
| 代表性样本 | release gate 不只看当前少量核心 case，还要覆盖主要槽位、冲突、负样本、隔离、删除和高频表达；样本量按覆盖清单决定，不把固定数量写成行业硬标准。 |
| 表达漂移 | Mem0 抽取结果出现英文、转写、同义表达时不丢业务索引；已发现漂移必须进入回归集。 |
| 故障注入 | Mem0 超时、Qdrant 短暂不可用、Postgres 慢查询时有明确失败或降级行为。 |

### 7.1 P0 核心槽位

P0 必测槽位如下：

| 槽位 | 正样本 | 冲突样本 | 负样本 |
| --- | --- | --- | --- |
| 昵称 | 当前称呼“小鹏”。 | 旧称呼“阿鹏”。 | 宠物名不得被当昵称。 |
| 宠物名 | 猫“麻薯”、狗“豆包”。 | 猫旧名“团子”。 | 鸟、兔未知时不得填充猫狗。 |
| 地点 | 当前上海。 | 旧地点杭州。 | 地点不得被抽成宠物名。 |
| 工作状态 | 接受 Moonshot offer。 | 仍在评估机会。 | 普通情绪不应污染工作状态。 |
| 沟通偏好 | 简洁直接建议。 | 先安慰、说教。 | 焦虑处理流程不能覆盖沟通偏好。 |
| 生日 | 6月1日。 | 5月20日。 | 日期不能被其他事件污染。 |
| 饮品/食物 | 茶、寿司。 | 咖啡、汉堡。 | 饮品和食物不得互相覆盖。 |
| 睡眠提醒 | 接受温和提醒。 | 讨厌提醒睡觉。 | 工作压力偏好不得覆盖睡眠提醒。 |
| 剧情设定 | 剧情猫“露露”。 | 现实猫“麻薯”。 | 现实追问不得召回剧情猫。 |

### 7.2 并发矩阵

| 级别 | 目标 | 建议持续时间 | P0 门禁 |
| --- | --- | --- | --- |
| smoke | 单用户完整链路。 | 1 轮。 | 必须 100% 通过。 |
| short | 10 并发 recall + 3 并发 append。 | 1-5 分钟。 | 质量门禁必须通过。 |
| baseline | 10 并发 recall + 10 并发 append。 | 10-15 分钟。 | 质量不退化，记录吞吐和 p95/p99。 |
| stress | 50 并发 recall + 10 并发 append。 | 15-30 分钟。 | 不允许语义、隔离、删除错误；延迟超门禁必须记录发布风险。 |
| spike | 10 -> 100 -> 10 并发 recall。 | 5-10 分钟。 | spike 后召回质量恢复正常。 |
| soak | 10-30 并发混合读写。 | 6-24 小时。 | 无内存泄漏、连接泄漏、错误率持续上升。 |

短压测用于开发阶段快速判断；baseline/stress/spike/soak 用于生产前容量和稳定性验收，不用于替代语义质量断言。

## 8. 推荐执行方式

### 8.1 本地真实短压测

用于每次重要修改后的快速验收：

```bash
env MEM0_API_KEY=$(cat /private/tmp/thinkback-mem0-api-key) \
MEM0_API_URL=http://localhost:8888 \
POSTGRES_PORT=55432 \
POSTGRES_DATABASE=thinkback_real \
REDIS_PORT=56379 \
REDIS_PASSWORD= \
QDRANT_URL=http://localhost:6333 \
THINKBACK_API_URL=http://127.0.0.1:18082 \
PYTHONPATH=src .venv/bin/python script/real_mem0_p0_short_pressure.py \
  --recall-concurrency 10 \
  --recall-requests 20 \
  --append-concurrency 3 \
  --report-dir docs/reports
```

短压测至少生成两个文件：

```text
docs/reports/<suite_id>.json
docs/reports/<suite_id>.md
```

### 8.2 多轮重复压测

用于排除 Mem0/LLM 抽取随机性：

```bash
for i in 1 2 3 4 5; do
  env MEM0_API_KEY=$(cat /private/tmp/thinkback-mem0-api-key) \
  MEM0_API_URL=http://localhost:8888 \
  POSTGRES_PORT=55432 \
  POSTGRES_DATABASE=thinkback_real \
  REDIS_PORT=56379 \
  REDIS_PASSWORD= \
  QDRANT_URL=http://localhost:6333 \
  THINKBACK_API_URL=http://127.0.0.1:18082 \
  PYTHONPATH=src .venv/bin/python script/real_mem0_p0_short_pressure.py \
    --recall-concurrency 10 \
    --recall-requests 20 \
    --append-concurrency 3 \
    --report-dir docs/reports
done
```

5 轮中任一轮失败，都不能把 P0 质量判定为稳定。

### 8.3 生产前长测

生产前至少补齐：

```bash
# baseline：10-15 分钟
# stress：50 并发，15-30 分钟
# spike：10 -> 100 -> 10 并发
# soak：6-24 小时
```

如果使用 k6/Locust，应把 Thinkback 的业务质量指标也纳入阈值，而不是只看 HTTP 延迟。HTTP 压测工具适合产生负载；记忆服务必须额外跑语义断言。

### 8.4 故障注入

P0 生产前至少演练：

| 故障 | 注入方式 | 期望 |
| --- | --- | --- |
| Mem0 502/503/504 | 代理或 mock transport 注入。 | 触发重试，失败可观测。 |
| Mem0 超时 | 拉高 Mem0 响应时间。 | 不产生业务索引和后端存储不一致的静默成功。 |
| Qdrant 不可达 | 停止 Qdrant 或阻断端口。 | append/recall 明确失败或降级，报告记录原因。 |
| Postgres 慢查询 | 限速或锁表演练。 | API 不挂死，错误可观测。 |
| Redis 短暂不可用 | 停止 Redis。 | readiness 失败，主链路按设计处理。 |

故障注入必须在隔离环境执行，不能在共享开发库里直接破坏服务。

## 9. 报告格式

每次真实压测至少输出稳定字段。下面的 JSON 是目标稳定结构；当前脚本尚未输出的字段必须写 `null`，并在 `metric_automation_status` 中标明状态。用例门禁可以写实际 case 结果，但必须说明“由 case 覆盖”，不能用 `0.0` 假装已经有独立统计字段。

当前脚本仍有少量历史字段名。`delete_residue_rate` 暂时等价于单条 L3 删除后的 `delete_memory_residue_rate`，不代表会话删除和全部删除也已覆盖；`cross_scope_leak_rate` 暂时主要由跨角色和剧情隔离 case 覆盖，不代表跨 user 独立指标已覆盖。报告汇总时必须保留这层兼容说明。

```json
{
  "run_id": "real-quality-...",
  "case_count": 0,
  "case_pass_rate": 0.0,
  "recall_at_10": 0.0,
  "precision_at_10": 0.0,
  "item_precision_at_10": 0.0,
  "top1_hit_rate": 0.0,
  "mrr": 0.0,
  "conflict_pollution_rate": 0.0,
  "false_positive_rate": 0.0,
  "delete_memory_residue_rate": 0.0,
  "delete_session_residue_rate": null,
  "delete_all_residue_rate": null,
  "rebuild_resurrection_rate": 0.0,
  "cross_user_leak_rate": null,
  "cross_character_leak_rate": 0.0,
  "roleplay_real_mix_rate": 0.0,
  "dirty_summary_recall_rate": null,
  "source_ref_loss_rate": null,
  "duplicate_active_rate": 0.0,
  "critical_slot_pass_rate": null,
  "known_drift_regression_pass_rate": null,
  "operation_success_rate": {
    "append": null,
    "recall": null,
    "delete": null,
    "rebuild": null
  },
  "request_metrics": {
    "request_count": 0,
    "retry_count": 0,
    "transient_failure_count": 0,
    "non_transient_failure_count": 0
  },
  "latency_ms": {
    "append": {"p50": 0, "p95": 0, "p99": 0},
    "recall": {"p50": 0, "p95": 0, "p99": 0},
    "delete": {"p50": 0, "p95": 0, "p99": 0},
    "rebuild": {"p50": 0, "p95": 0, "p99": 0}
  },
  "failed_cases": [],
  "failed_metrics": [],
  "failed_sections": [],
  "metric_automation_status": {
    "critical_slot_pass_rate": "case_gate_independent_field_pending",
    "delete_memory_residue_rate": "case_gate_legacy_delete_residue_rate",
    "delete_session_residue_rate": "pending",
    "delete_all_residue_rate": "pending",
    "rebuild_resurrection_rate": "case_gate_post_delete_and_active_count",
    "dirty_summary_recall_rate": "pending",
    "cross_user_leak_rate": "pending",
    "cross_character_leak_rate": "case_gate_legacy_cross_scope_leak_rate",
    "roleplay_real_mix_rate": "case_gate_independent_field_pending",
    "operation_success_rate": "section_pass_fail_independent_fields_pending",
    "known_drift_regression_pass_rate": "semi_automatic",
    "source_ref_loss_rate": "semi_automatic_independent_field_pending"
  }
}
```

脚本可以分阶段补齐指标，但报告字段名应尽量稳定，方便后续接入 CI 或压测平台。对于当前报告仍使用旧聚合字段的情况，例如 `delete_residue_rate` 或 `cross_scope_leak_rate`，应在下一轮脚本改造时映射到上面的细分字段；在改造前，最终报告必须说明字段口径，不能把旧聚合字段解释成所有删除或所有隔离指标已经通过。

Markdown 报告必须包含：

| 部分 | 内容 |
| --- | --- |
| 结论 | 通过/未通过、suite_id、执行时间、失败分段。 |
| 核心门禁 | 核心槽位、recall、precision、冲突污染、误召回、重复 active。 |
| 分段结果 | quality、concurrent_recall、append_probe、post_delete。 |
| 延迟 | append/recall/delete/rebuild 的 p95。 |
| 限制 | 样本覆盖、未执行的 soak、100 并发、故障注入、代表性回放说明。 |

## 10. 失败处理规则

1. 任何召回失败都要保存 `run_id`、case 名、query、召回文本、active memory 列表和 Mem0 返回样本。
2. 如果是 Mem0 表达漂移，先把真实表达补成回归测试，再修归一化或冲突槽位。
3. 如果是隔离泄漏，优先修作用域或 context 过滤，不能通过降低 top_k 掩盖。
4. 如果是删除复活，优先查 source_refs、deleted refs、rebuild 输入范围和 active/superseded 状态。
5. 如果是外部瞬时失败，记录 retry 指标；不能把未重试成功的失败算作质量通过。
6. 任何阈值调整都必须写进本文档，不能只改脚本让报告变绿。
7. 如果是并发下失败，必须先区分语义失败、HTTP 失败、超时失败和报告聚合误差。
8. 如果是延迟失败，必须同时看 Mem0、OpenAI、Qdrant、Postgres、数据库连接和服务端排队的分段耗时。
9. 如果指标尚未自动化，报告必须标明“未自动化/半自动”，不能把未测指标写成已通过。

## 11. 当前 P0 门禁

P0 dev gate 必须满足：

| 指标 | 门禁 | 状态 |
| --- | --- | --- |
| `critical_slot_pass_rate` | 核心槽位 case 全过 | 用例门禁，独立字段待补齐。 |
| `case_pass_rate` | `>= 0.98` | 自动门禁。 |
| `recall_at_10` | `>= 0.95` | 自动门禁。 |
| `precision_at_10` | `>= 0.95` | 自动门禁。 |
| `item_precision_at_10` | 当前先报告基线；release gate 前如发现 top10 无关 L3 过多，再设硬门禁。 | 自动报告。 |
| `conflict_pollution_rate` | `<= 0.02` | 自动门禁。 |
| `false_positive_rate` | `<= 0.02` | 自动门禁。 |
| `delete_memory_residue_rate` | `0` | 用例门禁，当前由 post_delete case 覆盖。 |
| `rebuild_resurrection_rate` | `0` | 用例门禁，当前由 post_delete case 覆盖。 |
| `cross_character_leak_rate` | `0` | 用例门禁，当前由隔离 case 覆盖。 |
| `roleplay_real_mix_rate` | `0` | 用例门禁，当前由剧情隔离 case 覆盖。 |
| `duplicate_active_rate` | `0` | 自动门禁。 |
| `non_transient_failure_count` | `0` | 自动门禁。 |
| 10 并发 `recall p95` | `<= 1500ms` | 自动门禁。 |
| 10 并发 `recall p99` | `<= 3000ms` | 自动门禁。 |

如果真实 Mem0/OpenAI/Qdrant 出现瞬时抖动，可以接受重试后通过，但必须在报告中保留 `transient_failure_count` 和 `retry_count`。

P0 release gate 必须另外满足：

| 类别 | 门禁 |
| --- | --- |
| 多轮重复 | 连续 5 轮 P0 短压测全部通过。 |
| 表达漂移 | 已发现真实漂移样本全部进入回归测试，并在修复后通过。 |
| 样本扩展 | 固定 P0 核心 case 必须全过；release 前应补齐主要槽位、冲突、负样本、隔离、删除和高频表达覆盖，未补齐时必须在报告中写成限制。 |
| 50 并发代表性压测 | 召回质量不退化；延迟若超过门禁，必须列为发布风险并给出修复计划。 |
| baseline | 10 并发混合读写 10-15 分钟，无质量退化。 |

生产前预检再补齐：

| 类别 | 门禁 |
| --- | --- |
| 代表性样本回放 | 工程构造 case 覆盖主要槽位、冲突、负样本、隔离和删除；有真实或仿真数据后按覆盖率扩展，不把固定样本数写成行业标准。 |
| stress | 50 并发 recall + 10 并发 append 15-30 分钟，无隔离、删除、冲突错误。 |
| spike | 峰值 100 并发 recall 后，质量指标恢复到 P0 门禁。 |
| soak | 6-24 小时无错误率持续上升、连接泄漏、内存泄漏。 |
| 故障注入 | Mem0/Qdrant/Postgres/Redis 异常均可观测，不产生静默错误。 |

## 12. 当前状态口径

报告结论要避免混淆：

| 说法 | 允许条件 |
| --- | --- |
| P0 主链路短压测通过 | `script/real_mem0_p0_short_pressure.py` 通过并生成报告。 |
| P0 质量回归通过 | `script/real_mem0_quality_regression.py` 通过。 |
| P0 release gate 通过 | 多轮重复、表达漂移回归、50 并发代表性压测、baseline 均有报告且风险可接受。 |
| P0 达到生产前压测标准 | release gate、stress、spike、soak、故障注入、代表性样本回放均有报告且通过。 |
| 线上长期稳定 | 还需要灰度发布、真实样本回放、监控告警和持续 SLO。 |

未执行的长测不能写成“已通过”，只能写成“未执行/待补齐/风险项”。
