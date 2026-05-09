# AI虚拟社交记忆服务 P0 压测报告字段规范

本文档给脚本和报告生成器对齐字段用。评测方案正文见 [P0 压测评测方案](AI虚拟社交记忆服务P0压测指标.md)。

## 稳定 JSON 字段

当前脚本尚未输出的字段必须写 `null`，并在 `metric_automation_status` 中标明状态。

用例门禁可以写实际 case 结果，但必须说明“由 case 覆盖”。不能用 `0.0` 假装已经有独立统计字段。

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
  "http_5xx_rate": null,
  "timeout_rate": null,
  "idempotency_failure_rate": null,
  "transient_retry_rate": 0.0,
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
    "delete_memory_residue_rate": "case_gate_post_delete",
    "delete_session_residue_rate": "pending",
    "delete_all_residue_rate": "pending",
    "rebuild_resurrection_rate": "case_gate_post_delete_and_active_count",
    "dirty_summary_recall_rate": "pending",
    "cross_user_leak_rate": "pending",
    "cross_character_leak_rate": "case_gate_isolation",
    "roleplay_real_mix_rate": "case_gate_independent_field_pending",
    "operation_success_rate": "section_pass_fail_independent_fields_pending",
    "known_drift_regression_pass_rate": "semi_automatic",
    "source_ref_loss_rate": "semi_automatic_independent_field_pending",
    "http_5xx_rate": "semi_automatic",
    "timeout_rate": "semi_automatic",
    "idempotency_failure_rate": "pending",
    "transient_retry_rate": "automatic_report"
  }
}
```

## 历史字段兼容口径

| 历史字段 | 只能映射为 | 不能解释为 |
| --- | --- | --- |
| `delete_residue_rate` | 单条 L3 删除后的 `delete_memory_residue_rate`。 | 会话删除和全部删除都已覆盖。 |
| `cross_scope_leak_rate` | 跨角色和剧情隔离 case。 | 所有跨 user、跨 character、现实/剧情隔离都已覆盖。 |

历史 JSON 中的 `delete_residue_rate` 仅兼容映射为单条 L3 删除后的 `delete_memory_residue_rate`，不代表会话删除和全部删除也已覆盖。

历史 JSON 中的 `cross_scope_leak_rate` 仅兼容映射为跨角色和剧情隔离 case，不代表跨 user 独立指标已覆盖。

报告汇总时必须保留这层兼容说明。
