# AI虚拟社交记忆服务首版主链路记忆质量评测报告模板

本文档用于记录一次具体的首版记忆质量评测结果。
评测方法、指标定义和门禁见
[首版记忆质量评测方案](AI虚拟社交记忆服务首版主链路质量评测方案.md)。

填写规则：

| 规则 | 说明 |
| --- | --- |
| 一次执行一份报告 | 不把多轮结果混写到同一份报告。 |
| 数据只写在报告里 | 方案文档不记录本轮评测数据。 |
| 未输出字段写 `null` | 不能用 `0.0` 伪装成已经自动化。 |
| 失败必须留证据 | 至少记录 query、召回文本、期望事实、禁用事实。 |

## 1. 运行信息

| 字段 | 值 |
| --- | --- |
| `run_id` | `<real-quality-yyyymmdd-hhmmss>` |
| 执行时间 | `<yyyy-mm-dd hh:mm:ss>` |
| 执行环境 | `<local / staging / preprod>` |
| Thinkback API | `<url>` |
| Mem0 模式 | `<library>` |
| Qdrant | `<url-or-cluster>` |
| PostgreSQL | `<db-or-cluster>` |
| 原始 JSON | `<path-or-link>` |
| 评测脚本 | `script/real_mem0_quality_regression.py` |

## 2. 结论摘要

| 字段 | 值 |
| --- | --- |
| 结论 | `<通过 / 未通过 / 需复核>` |
| 失败 case 数 | `<count>` |
| 失败指标数 | `<count>` |
| 是否满足首版记忆质量门禁 | `<是 / 否>` |
| 结论边界 | 本报告只评价首版记忆质量，不评价性能、容量和长期稳定性。 |

## 3. 指标摘要

| 指标 | 本轮结果 | 建议值 / 门禁 | 结论 |
| --- | ---: | --- | --- |
| `case_pass_rate` | `<value>` | 核心样本全部通过 | `<通过 / 未通过 / 需复核>` |
| `recall_at_10` | `<value>` | 核心样本无漏召回；扩展样本 `>= 0.95` | `<通过 / 未通过 / 需复核>` |
| `precision_at_10` | `<value>` | 核心样本无召回污染；扩展样本 `>= 0.95` | `<通过 / 未通过 / 需复核>` |
| `conflict_pollution_rate` | `<value>` | 核心样本为 `0`；扩展样本 `<= 0.02` | `<通过 / 未通过 / 需复核>` |
| `false_positive_rate` | `<value>` | 负样本为 `0`；扩展样本 `<= 0.02` | `<通过 / 未通过 / 需复核>` |
| `duplicate_active_rate` | `<value>` | `0` | `<通过 / 未通过 / 需复核>` |
| `delete_memory_residue_rate` | `<value-or-null>` | 本轮样本内为 `0` | `<通过 / 未通过 / 需复核>` |
| `rebuild_resurrection_rate` | `<value-or-null>` | 本轮样本内为 `0` | `<通过 / 未通过 / 需复核>` |
| `cross_user_leak_rate` | `<value-or-null>` | 本轮样本内为 `0` | `<通过 / 未通过 / 需复核>` |
| `cross_character_leak_rate` | `<value-or-null>` | 本轮样本内为 `0` | `<通过 / 未通过 / 需复核>` |
| `roleplay_real_mix_rate` | `<value-or-null>` | 本轮样本内为 `0` | `<通过 / 未通过 / 需复核>` |

指标口径说明：

| 口径 | 说明 |
| --- | --- |
| 核心样本 | 用于判断首版主链路是否可用，出现明确失败 case 即未通过。 |
| 扩展样本 | 样本量扩大后用于观察整体趋势，比例阈值才有稳定统计意义。 |
| 生命周期与隔离 | 删除、重建、跨作用域泄漏按不变量解释，不用均值稀释。 |

## 4. 观察指标

| 指标 | 本轮结果 | 建议值 | 处理方式 |
| --- | ---: | --- | --- |
| `top1_hit_rate` | `<value>` | 不设硬门禁 | 低于本地基线时复核排序质量。 |
| `mrr` | `<value>` | 不设硬门禁 | 低于本地基线时复核排序质量。 |
| `item_precision_at_10` | `<value>` | 不设硬门禁 | 辅助解释 top10 召回噪声。 |
| `irrelevant_l3_per_query` | `<value>` | 不设硬门禁，越低越好 | 异常升高时排查。 |
| `known_drift_regression_pass_rate` | `<value-or-null>` | 首版可为 `null` | 有样本后再设门禁。 |

## 5. 样本与状态摘要

| 字段 | 本轮结果 | 解释 |
| --- | ---: | --- |
| `positive_case_count` | `<count>` | 正样本 case 数。 |
| `negative_case_count` | `<count>` | 负样本 case 数。 |
| `active_memory_count` | `<count>` | 删除和 rebuild 前的 active 记忆数。 |
| `active_after_rebuild` | `<count>` | rebuild 后的 active 记忆数。 |
| `request_metrics.request_count` | `<count>` | 本轮请求数。 |
| `request_metrics.transient_failure_count` | `<count>` | 瞬时失败数。 |
| `request_metrics.non_transient_failure_count` | `<count>` | 非瞬时失败数。 |

## 6. 场景覆盖

| 场景 | 是否覆盖 | 结果 | 证据 |
| --- | --- | --- | --- |
| 核心槽位纠错 | `<是 / 否>` | `<通过 / 未通过 / 需复核>` | `<case list>` |
| 多槽位并存 | `<是 / 否>` | `<通过 / 未通过 / 需复核>` | `<case list>` |
| 旧值污染 | `<是 / 否>` | `<通过 / 未通过 / 需复核>` | `<case list>` |
| 负样本 | `<是 / 否>` | `<通过 / 未通过 / 需复核>` | `<case list>` |
| 跨用户隔离 | `<是 / 否>` | `<通过 / 未通过 / 需复核>` | `<case list>` |
| 跨角色隔离 | `<是 / 否>` | `<通过 / 未通过 / 需复核>` | `<case list>` |
| 现实/剧情隔离 | `<是 / 否>` | `<通过 / 未通过 / 需复核>` | `<case list>` |
| 删除与重建 | `<是 / 否>` | `<通过 / 未通过 / 需复核>` | `<case list>` |
| 表达漂移 | `<是 / 否>` | `<通过 / 未通过 / 需复核>` | `<case list>` |

## 7. 失败用例

| case | 场景 | query | 期望事实 | 禁用事实 | 召回摘要 | 失败原因 |
| --- | --- | --- | --- | --- | --- | --- |
| `<case_name>` | `<scenario>` | `<query>` | `<expected>` | `<forbidden>` | `<recall_summary>` | `<reason>` |

## 8. 删除与重建证据

| 字段 | 值 |
| --- | --- |
| 删除目标 memory_id | `<memory_id>` |
| 删除前 active 数 | `<count>` |
| 删除后复查结果 | `<summary>` |
| rebuild 后 active 数 | `<count>` |
| rebuild 后复查结果 | `<summary>` |
| 是否出现旧事实复活 | `<是 / 否>` |

## 9. 作用域隔离证据

| 隔离类型 | 写入事实 | 召回作用域 | 禁用事实 | 结果 |
| --- | --- | --- | --- | --- |
| 跨 user | `<other_user_fact>` | `<current_user_scope>` | `<forbidden>` | `<通过 / 未通过>` |
| 跨 character | `<other_character_fact>` | `<current_character_scope>` | `<forbidden>` | `<通过 / 未通过>` |
| 现实/剧情 | `<roleplay_or_real_fact>` | `<opposite_context>` | `<forbidden>` | `<通过 / 未通过>` |

## 10. 自动化状态

| 字段 | 状态 | 说明 |
| --- | --- | --- |
| `case_pass_rate` | `<automatic>` | `<note>` |
| `recall_at_10` | `<automatic>` | `<note>` |
| `precision_at_10` | `<automatic>` | `<note>` |
| `item_precision_at_10` | `<automatic>` | `<note>` |
| `top1_hit_rate` | `<automatic>` | `<note>` |
| `mrr` | `<automatic>` | `<note>` |
| `irrelevant_l3_per_query` | `<automatic>` | `<note>` |
| `conflict_pollution_rate` | `<automatic>` | `<note>` |
| `false_positive_rate` | `<automatic>` | `<note>` |
| `duplicate_active_rate` | `<automatic>` | `<note>` |
| `delete_memory_residue_rate` | `<case_gate / automatic / null>` | `<note>` |
| `rebuild_resurrection_rate` | `<case_gate / automatic / null>` | `<note>` |
| `cross_user_leak_rate` | `<case_gate / automatic / null>` | `<note>` |
| `cross_character_leak_rate` | `<case_gate / automatic / null>` | `<note>` |
| `roleplay_real_mix_rate` | `<case_gate / automatic / null>` | `<note>` |
| `known_drift_regression_pass_rate` | `<manual_review / null>` | `<note>` |

## 11. 结论与处理建议

| 字段 | 值 |
| --- | --- |
| 最终结论 | `<通过 / 未通过 / 需复核>` |
| 阻断问题 | `<blocking issues>` |
| 建议处理 | `<action items>` |
| 下次复测条件 | `<conditions>` |

## 12. 稳定 JSON 摘要

```json
{
  "run_id": "<real-quality-...>",
  "started_at": "<yyyy-mm-ddThh:mm:ss+08:00>",
  "environment": "<local|staging|preprod>",
  "passed": "<true_or_false>",
  "case_count": "<count>",
  "positive_case_count": "<count>",
  "negative_case_count": "<count>",
  "case_pass_rate": "<value>",
  "recall_at_10": "<value>",
  "precision_at_10": "<value>",
  "item_precision_at_10": "<value>",
  "top1_hit_rate": "<value>",
  "mrr": "<value>",
  "irrelevant_l3_per_query": "<value>",
  "conflict_pollution_rate": "<value>",
  "false_positive_rate": "<value>",
  "duplicate_active_rate": "<value>",
  "delete_memory_residue_rate": "<value_or_null>",
  "rebuild_resurrection_rate": "<value_or_null>",
  "cross_user_leak_rate": "<value_or_null>",
  "cross_character_leak_rate": "<value_or_null>",
  "roleplay_real_mix_rate": "<value_or_null>",
  "known_drift_regression_pass_rate": "<value_or_null>",
  "active_memory_count": "<count>",
  "active_after_rebuild": "<count>",
  "request_metrics": {
    "request_count": "<count>",
    "retry_count": "<count>",
    "transient_failure_count": "<count>",
    "non_transient_failure_count": "<count>"
  },
  "case_results": [],
  "category_metrics": {},
  "latency_ms": {},
  "failed_cases": [],
  "failed_metrics": [],
  "metric_automation_status": {}
}
```
