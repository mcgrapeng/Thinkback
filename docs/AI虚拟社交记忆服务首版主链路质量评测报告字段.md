# AI虚拟社交记忆服务首版主链路记忆质量评测报告字段规范

本文档定义首版记忆质量评测报告的稳定字段。
评测方案见 [首版记忆质量评测方案](AI虚拟社交记忆服务首版主链路质量评测方案.md)。
报告模板见 [首版记忆质量评测报告模板](AI虚拟社交记忆服务首版主链路质量评测报告模板.md)。

## 1. 字段填写规则

| 规则 | 说明 |
| --- | --- |
| 字段名保持稳定 | 报告生成器、脚本和汇总工具应使用本文字段。 |
| 暂未输出写 `null` | 不能用 `0.0` 表示未自动化字段。 |
| case 门禁要说明状态 | 由 case 覆盖的字段必须在 `metric_automation_status` 中说明。 |
| 性能结论不写入本报告 | 延迟可作为排障记录；并发、容量、资源趋势放到性能与稳定性报告。 |

## 2. 核心字段

| 字段 | 类型 | 直白解释 | 备注 |
| --- | --- | --- | --- |
| `run_id` | string | 本次评测唯一标识。 | 每次执行必须唯一。 |
| `case_count` | number | 本次评测用例数。 | 不含手工补充说明。 |
| `case_pass_rate` | number | 用例通过比例。 | 核心样本按全部通过解释。 |
| `recall_at_10` | number | 正确事实进入 top10 的比例。 | 首版硬门禁。 |
| `precision_at_10` | number | case 级 top10 干净率。 | 首版硬门禁。 |
| `item_precision_at_10` | number | top10 条目级相关比例。 | 观察指标。 |
| `top1_hit_rate` | number | 正确事实排第一的比例。 | 观察指标。 |
| `mrr` | number | 正确事实排序靠前程度。 | 观察指标。 |
| `irrelevant_l3_per_query` | number | 平均每次 query 返回的无关 L3 条目数。 | 观察指标。 |
| `conflict_pollution_rate` | number | 旧值或冲突值污染比例。 | 首版硬门禁。 |
| `false_positive_rate` | number | 未知事实误召回比例。 | 首版硬门禁。 |
| `duplicate_active_rate` | number | 同槽位重复 active 比例。 | 首版硬门禁。 |
| `delete_memory_residue_rate` | number/null | 删除后残留比例。 | 可先由 case 门禁覆盖。 |
| `rebuild_resurrection_rate` | number/null | rebuild 后旧事实复活比例。 | 可先由 case 门禁覆盖。 |
| `cross_user_leak_rate` | number/null | 跨 user 泄漏比例。 | 可先由 case 门禁覆盖。 |
| `cross_character_leak_rate` | number/null | 跨 character 泄漏比例。 | 可先由 case 门禁覆盖。 |
| `roleplay_real_mix_rate` | number/null | 现实/剧情混用比例。 | 可先由 case 门禁覆盖。 |
| `known_drift_regression_pass_rate` | number/null | 表达漂移样本通过率。 | 首版可为 `null`。 |

`precision_at_10` 与 `item_precision_at_10` 的区别：

| 指标 | 分母 | 含义 |
| --- | --- | --- |
| `precision_at_10` | case 数 | 一个 case 的 top10 是否既命中当前事实，又没有禁用事实。 |
| `item_precision_at_10` | top10 条目数 | top10 里有多少条目与当前问题相关。 |

建议值解释：

| 指标 | 建议值 | 报告解释 |
| --- | --- | --- |
| `case_pass_rate` | `1.0` | 出现失败 case 时即为未通过。 |
| `recall_at_10` | 核心无漏召回；扩展 `>= 0.95`。 | 低于建议值即未通过。 |
| `precision_at_10` | 核心无污染；扩展 `>= 0.95`。 | 低于建议值即未通过。 |
| `conflict_pollution_rate` | 核心为 `0`；扩展 `<= 0.02`。 | 高于建议值即未通过。 |
| `false_positive_rate` | 负样本为 `0`；扩展 `<= 0.02`。 | 高于建议值即未通过。 |
| 删除、重建、隔离类字段 | 本轮样本内为 `0`。 | 按生命周期和作用域不变量解释。 |
| `top1_hit_rate` | 不设硬门禁。 | 观察排序质量，低于本地基线时复核。 |
| `mrr` | 不设硬门禁。 | 观察整体排序位置，低于本地基线时复核。 |
| `item_precision_at_10` | 不设硬门禁。 | 辅助解释 top10 噪声。 |
| `irrelevant_l3_per_query` | 不设硬门禁，越低越好。 | 异常升高时排查召回噪声。 |
| `known_drift_regression_pass_rate` | 首版可为 `null`。 | 有样本后再设门禁。 |

## 3. 执行与失败字段

| 字段 | 类型 | 直白解释 |
| --- | --- | --- |
| `passed` | boolean | 本轮是否通过首版质量门禁。 |
| `failed_cases` | array | 失败 case 名称列表。 |
| `failed_metrics` | array | 未达到门禁的指标列表。 |
| `failed_sections` | array | 失败分段列表，适合汇总报告使用。 |
| `case_results` | array | 每个 case 的命中、污染、状态和失败原因。 |
| `category_metrics` | object | 每类场景的 case 数、通过数和通过率。 |
| `request_metrics` | object | 请求数、瞬时失败数、非瞬时失败数。 |
| `metric_automation_status` | object | 字段自动化状态说明。 |

## 4. 辅助字段

这些字段可以出现在脚本 JSON 中，用于排障或兼容汇总工具。
它们不是首版记忆质量门禁。

| 字段 | 类型 | 直白解释 |
| --- | --- | --- |
| `positive_case_count` | number | 正样本 case 数。 |
| `negative_case_count` | number | 负样本 case 数。 |
| `active_memory_count` | number | 删除和 rebuild 前的 active 记忆数。 |
| `active_after_rebuild` | number | rebuild 后的 active 记忆数。 |
| `transient_retry_rate` | number | 瞬时失败占请求数的比例。 |
| `delete_session_residue_rate` | number/null | 会话级删除残留，首版可为 `null`。 |
| `delete_all_residue_rate` | number/null | 全量删除残留，首版可为 `null`。 |
| `latency_ms` | object | 执行过程延迟记录，只用于排障。 |

`http_5xx_rate`、`timeout_rate` 等性能字段如在历史 JSON 中出现，
只作为兼容字段保留，不参与首版质量门禁。

## 5. 自动化状态取值

| 状态值 | 含义 |
| --- | --- |
| `automatic` | 已由脚本独立计算并参与报告输出。 |
| `case_gate` | 当前由明确 case 覆盖，还没有独立统计字段。 |
| `case_gate_independent_field_pending` | case 已覆盖，但独立字段仍为 `null`。 |
| `pending` | 暂未覆盖，报告中必须写 `null`。 |
| `manual_review` | 需要人工或 judge 复核。 |

## 6. 稳定 JSON 示例

```json
{
  "run_id": "real-quality-...",
  "passed": false,
  "case_count": 0,
  "positive_case_count": 0,
  "negative_case_count": 0,
  "case_pass_rate": 0.0,
  "recall_at_10": 0.0,
  "precision_at_10": 0.0,
  "item_precision_at_10": 0.0,
  "top1_hit_rate": 0.0,
  "mrr": 0.0,
  "irrelevant_l3_per_query": 0.0,
  "conflict_pollution_rate": 0.0,
  "false_positive_rate": 0.0,
  "duplicate_active_rate": 0.0,
  "delete_memory_residue_rate": 0.0,
  "delete_session_residue_rate": null,
  "delete_all_residue_rate": null,
  "rebuild_resurrection_rate": 0.0,
  "cross_user_leak_rate": null,
  "cross_character_leak_rate": 0.0,
  "roleplay_real_mix_rate": 0.0,
  "known_drift_regression_pass_rate": null,
  "active_memory_count": 0,
  "active_after_rebuild": 0,
  "transient_retry_rate": 0.0,
  "request_metrics": {
    "request_count": 0,
    "retry_count": 0,
    "transient_failure_count": 0,
    "non_transient_failure_count": 0
  },
  "case_results": [],
  "category_metrics": {},
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
    "case_pass_rate": "automatic",
    "recall_at_10": "automatic",
    "precision_at_10": "automatic",
    "item_precision_at_10": "automatic",
    "top1_hit_rate": "automatic",
    "mrr": "automatic",
    "irrelevant_l3_per_query": "automatic",
    "conflict_pollution_rate": "automatic",
    "false_positive_rate": "automatic",
    "duplicate_active_rate": "automatic",
    "delete_memory_residue_rate": "case_gate",
    "delete_session_residue_rate": "pending",
    "delete_all_residue_rate": "pending",
    "rebuild_resurrection_rate": "case_gate",
    "cross_user_leak_rate": "case_gate_independent_field_pending",
    "cross_character_leak_rate": "case_gate",
    "roleplay_real_mix_rate": "case_gate",
    "known_drift_regression_pass_rate": "manual_review"
  }
}
```

## 7. 不属于本报告结论的字段

| 字段类型 | 放置位置 |
| --- | --- |
| 延迟 p95 / p99 | 性能与稳定性测试报告。 |
| 并发、吞吐、容量 | 性能与稳定性测试报告。 |
| CPU、内存、连接池、队列趋势 | 性能与稳定性测试报告。 |
| fault injection 结果 | 性能与稳定性测试报告。 |
