# AI虚拟社交记忆服务性能与稳定性测试方案

本文档定义 Thinkback 记忆服务的性能与稳定性测试方案。
覆盖范围包括并发、容量、短周期稳定性、故障注入和资源观测。

本文不定义记忆质量门禁。
记忆质量评测见
[AI虚拟社交记忆服务首版主链路记忆质量评测方案](AI虚拟社交记忆服务首版主链路质量评测方案.md)。

## 1. 测试定位

性能与稳定性测试回答的是“服务能否稳定承载目标负载”。
记忆质量评测回答的是“记忆事实是否可信”。
两者不能互相替代。

| 文档 | 回答的问题 | 结论边界 |
| --- | --- | --- |
| 首版记忆质量评测方案 | 记忆是否可信。 | 判断首版记忆质量是否可用。 |
| 性能与稳定性测试方案 | 服务能否承载目标负载。 | 判断当前环境的承载能力和稳定性风险。 |

性能与稳定性测试必须建立在记忆质量评测通过的基础上。
若质量门禁未通过，性能结果只能用于定位瓶颈，不能证明服务可用。

## 2. 测试类型

| 类型 | 目标 | 建议持续时间 | 结论边界 |
| --- | --- | --- | --- |
| smoke | 单用户完整链路是否可执行。 | 1 轮。 | 验证最小链路可执行。 |
| baseline | 低并发下建立延迟、错误率和吞吐基线。 | 10-15 分钟。 | 性能基线参考。 |
| stress | 提升并发，观察错误、尾延迟和资源饱和。 | 15-30 分钟。 | 容量参考。 |
| spike | 快速升高并发后回落，观察恢复能力。 | 5-10 分钟。 | 峰值恢复参考。 |
| soak | 稳定负载持续运行，观察队列、资源和错误率漂移。 | 10 分钟。 | 短周期稳定性证据。 |
| fault injection | Mem0、Qdrant、Postgres、Redis 等依赖异常。 | 按故障场景执行。 | 故障可观测性与恢复参考。 |

当前 10、50、100 并发是工程探针，不是行业标准，也不是线上容量承诺。
有真实流量后，应按峰值 RPS、读写比例、部署副本数和外部依赖限流重新校准。

## 3. 性能与稳定性指标

| 指标组 | 指标 | 说明 |
| --- | --- | --- |
| 延迟 | recall p95、recall p99、append p95、delete p95、rebuild p95。 | 观察交互链路和后台操作的尾延迟。 |
| 错误 | HTTP 5xx、timeout、non_transient_failure_count。 | 区分瞬时错误和不可恢复失败。 |
| 重试 | transient_retry_rate、retry_success_rate。 | 判断外部依赖抖动是否被正确吸收。 |
| 吞吐 | throughput_recall_rps、throughput_append_rps。 | 在质量通过前提下观察处理能力。 |
| 资源 | CPU、memory RSS、连接池占用、Redis、Postgres、Qdrant 耗时。 | 判断是否接近饱和或持续增长。 |
| 队列 | l3_pending_write_tasks、l3_available_capacity。 | 判断 L3 后台抽取是否积压。 |

不纳入本文的指标：

| 指标类型 | 放置位置 |
| --- | --- |
| `recall_at_10`、`precision_at_10`、`conflict_pollution_rate` | 首版记忆质量评测报告。 |
| 删除残留、重建复活、作用域隔离 | 首版记忆质量评测报告。 |
| 失败 case、召回文本、active memory 证据 | 首版记忆质量评测报告。 |

## 4. 执行方法

### 4.1 生产前阶段汇总

```bash
PYTHONPATH=src .venv/bin/python script/real_mem0_stability_preprod.py \
  --output-dir docs/reports \
  --phase-report baseline=<baseline.json> \
  --phase-report stress=<stress.json> \
  --phase-report spike=<spike.json> \
  --phase-report soak=<soak.json> \
  --phase-report fault_injection=<fault_injection.json> \
  --phase-report representative_replay=<representative_replay.json>
```

### 4.2 10 分钟 soak

```bash
env THINKBACK_API_URL=${THINKBACK_API_URL:?set THINKBACK_API_URL} \
PYTHONPATH=src .venv/bin/python script/real_mem0_stability_preprod.py \
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

## 5. 报告内容要求

| 部分 | 内容 |
| --- | --- |
| 测试环境 | 服务副本数、数据库和缓存配置、外部依赖配置、数据样本说明。 |
| 阶段结果 | baseline、stress、spike、soak、fault_injection、representative_replay 的执行状态。 |
| 延迟与错误 | 各操作 p95/p99、HTTP 5xx、timeout、非瞬时失败、重试结果。 |
| 资源趋势 | CPU、内存、连接池、队列积压、Qdrant 耗时、外部限流。 |
| 结论边界 | 当前结果能证明什么、不能证明什么、哪些缺口需要后续补齐。 |

尚未自动化的字段写 `null`，并在 `metric_automation_status` 中标明状态。

报告中可以引用质量评测报告的结论，但不复制质量评测数据。

## 6. 结果解释

- 性能与稳定性测试不替代记忆质量评测。质量失败时，不能用延迟或吞吐结果证明服务可用。
- soak 不等同于无间隔循环执行 short suite。
  本文 soak 仅定义为 10 分钟稳定负载观察。
  默认每 5 分钟执行一轮 30 并发 recall + 10 并发 append 探针，
  并持续观察 L3 后台队列、错误率、延迟和资源是否漂移。
- 如果去掉间隔后触发 `l3 background queue full`，结论应记录为“持续写入使 L3 后台抽取达到容量上限”。
- 容量或背压风险不应通过放宽语义正确性指标来掩盖。

## 7. 参考资料

- Google SRE《Monitoring Distributed Systems》：https://sre.google/sre-book/monitoring-distributed-systems/
- Grafana k6 load test types：https://grafana.com/docs/k6/latest/testing-guides/automated-performance-testing/
- Grafana k6 soak testing：https://grafana.com/blog/soak-testing/
- Locust documentation：https://docs.locust.io/
