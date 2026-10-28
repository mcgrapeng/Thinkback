# Thinkback 审计修复验证记录

日期：2026-10-28
审计输入：2026-10-27 生产审计的 18 条问题（`/memory/recall` 幂等回放、擦除覆盖面/续跑/屏障/审计三态、append 单次执行、TTL、任务心跳与崩溃恢复、L2 容量、namespace 幂等、rebuild 时间与类型策略、类型注册表无效配置、降级多槽位、Milvus 一致性、校验错误泄密、内网边界、CI/依赖/评测/文档）。
验证快照：分支 `refactor/memory-architecture-rewrite`，HEAD `e15556b`。

## 一条审计问题一行证据

| # | 审计问题 | 修复提交 | 回归测试 / 证据 |
| --- | --- | --- | --- |
| 1 | recall 响应进幂等缓存，擦除后仍能回放旧内容 | `b834f5d` | `test_read_path_is_not_cached_or_replayed` |
| 2 | 422 把正文与密钥写进日志和响应 | `16a578e` | `test_validation_error_does_not_echo_input` |
| 3 | 擦除漏掉 DELETED/SUPPRESSED、只有 L1/L2 的会话、后端暗数据 | `6f26246` | `test_erase_session_without_l3_removes_round_and_summary`、`test_erase_after_suppress_removes_retained_plaintext`、`test_erase_all_deletes_backend_only_memories` |
| 4 | 擦除中途失败后重试报 completed，原文残留 | `3c9ac4e` | `test_erase_retry_after_purge_failure_finishes_cleanup`、`test_erase_task_id_is_queryable` |
| 5 | 擦除挡不住在飞写入与外部重建复活 | `8bc1a53` | `test_erase_blocks_inflight_l3_append`、`test_erase_leaves_source_tombstone_so_external_history_rebuild_cannot_restore_it` |
| 6 | 擦除清掉同 namespace 别的会话的原始消息，审计无条件写 true | `f4dd9ea` | `test_erase_scoped_does_not_purge_namespace_messages`、`test_erase_audit_reports_purge_state_instead_of_unconditional_true` |
| 7 | 同一 round 并发首写执行两次后端副作用 | `8be79f9` | `test_concurrent_first_append_executes_backend_add_once` |
| 8 | 后端写成功、本地索引失败后重试重复抽取 | `8be79f9` | `test_append_retry_after_index_failure_reuses_backend_result` |
| 9 | 健康长任务被回收成 failed，状态翻转 RUNNING→FAILED→COMPLETED | `bed9de4` | `test_healthy_long_task_is_not_reclaimed_while_worker_alive` |
| 10 | async append 谎报 completed；崩溃后已接受的工作永久丢失 | `bed9de4` | `test_async_append_returns_accepted_until_task_finishes`、`test_startup_replays_accepted_write_tasks` |
| 11 | L2 刷新绕过队列上限，scope 状态无限增长 | `741d0e7` | `test_l2_respects_pending_task_limit`、`test_l2_scope_state_is_bounded` |
| 12 | L2 在飞刷新结束后 trailing 刷新可能丢失（清零计数） | `8ebb64f`、`e15556b` | `test_refresh_in_flight_triggers_trailing_refresh`（改成确定性） |
| 13 | TTL 只给 Mem0，本地索引无到期时间，过期事实从本地回流 | `88c9e10` | `test_expired_memory_is_not_returned_by_slot_backfill`、`test_expired_memory_is_not_returned_by_semantic_recall`、`test_expired_memory_is_not_listed_by_items` |
| 14 | 同一 round 换 namespace 静默丢写 | `88c9e10` | `test_same_round_under_other_namespace_raises_conflict`、`test_same_round_same_namespace_stays_idempotent` |
| 15 | rebuild 按 round_id 字典序判新旧、丢掉原记忆类型 | `88c9e10` | `test_rebuild_prefers_newer_source_timestamp_over_round_id_order`、`test_rebuild_preserves_custom_memory_type_policy` |
| 16 | conflict=append 不生效；recall_budget/entity_vocabulary 等无效配置 | `8768e5c` | `test_event_type_append_keeps_both_facts`、`test_profile_type_overwrite_still_supersedes`、`test_registry_drops_config_without_behavior` |
| 17 | 降级模式下复合查询只回填第一个槽位 | `8768e5c` | `test_degraded_recall_backfills_all_requested_slots` |
| 18 | Milvus Strong 一致性改错库、失败后不重试 | `bed9de4` | `test_passes_configured_database_to_client`、`test_mem0_library_backend_retries_collection_consistency_until_succeeded` |

工程与交付面的问题（不在 18 条计数内，单独记）：

| 问题 | 修复提交 | 证据 |
| --- | --- | --- |
| 内网边界没有落地：任意集群内 Pod 可调业务 API | `656f151` | `k8s/networkpolicy.yaml`（default-deny + 放行网关 namespace）+ 部署文档写明要改的那一行 |
| 单副本滚动发布期双 Pod 进程内状态分裂 | `656f151` | 改用 `strategy: Recreate` |
| CI 跳过 97 个确定性用例；`make check` 不含单测 | `c35e8fd` | `.github/workflows/ci.yml` 去掉两个 `--ignore`；`check: ... test ...` |
| 变异门禁不阻断（mutmut 有存活也退出 0） | `c35e8fd` | `tests/eval/check_mutation_survivors.py` + `tests/eval/mutation_survivors.json`（逐条写明验收理由） |
| 契约 fuzz 把限流额度耗尽误报成契约不符；429/503 未声明 | `c35e8fd` | OpenAPI 声明 429/503 + fuzz 隔离限流中间件；`tests/fuzz` 全绿 |
| 压测只看 2xx 与延迟，写进去却召不回来也算绿 | `c35e8fd` | k6 冒烟改为轮询任务终态 + 要求 `degraded=false` + 要求召回本轮事实 |
| 9 条依赖漏洞 | `c35e8fd` | `make audit`：No known vulnerabilities found（certifi 2026.10.23 / filelock 4.2.4 / setuptools 84.2.1） |
| 文档宣传已删除的 v1、API key/tenant、prompt 编辑、旧 check 口径 | `8c01468` | README/API/SDK/ROADMAP/协议 spec 按当前实现重写 |
| 真实评测不分型，75% 的真实水位被 100% 的 fake 结果掩盖 | `8c01468` | 评测报告分桶（extraction_empty / extraction_missing_fact / retrieval_missing_fact / forbidden_term_leaked）+ 新增真实基线 `tests/eval/baseline_semiconductor_real.json` |
| 旧真实脚本仍在用已删除的 HTTP `intent` 字段；522 行 smoke script import 就会失败 | `8c01468` | 删除 `tests/script/run_real_smoke_test.py`；其余脚本去掉 `intent` |
| 管理台集成页渲染已删除的 v1 API Key 界面（按钮全 404） | `dc2099d` | 页面改为只读契约参考；删除 API Key 客户端与查询钩子；web 22 用例 + tsc + build 通过 |
| 示例配置 gRPC 端口与代码默认值不一致 | `a4a5168` | `.env.example` 改 50051 |

## 数据迁移

`alembic/versions/20261028_0015_memory_lifecycle_columns.py`（20261028_0015，revises 20261027_0014）：

- `ins_memory.expires_at`、`ins_memory.recorded_at`：TTL 与「事实何时为真 / 我们何时记下」；
- `ins_summary_round_journal.namespace`：append 幂等身份的一部分；
- `ins_summary_round_journal.request_metadata`：rebuild 回放按原类型与 TTL 策略重放。

已在本地 PostgreSQL 上 `alembic upgrade head` 实跑通过，downgrade 也在同一文件内。

## 本轮验证命令

终验在 HEAD `5c1f9c0`（`make check` 全链跑通）：

| 命令 | 结果 |
| --- | --- |
| `make format-check` | 293 files already formatted |
| `make lint` | All checks passed! |
| `make typecheck` | Success: no issues found in 63 source files |
| `make test`（隔离本机 OPENAI_API_KEY 与代理） | **885 passed**, 1 warning |
| `make mutation-audit` | 当前存活 112 / 无测试 2，与已验收基线一致，无新增 |
| `make eval` | 通用基线达标 + 半导体自包含基线达标 |
| `make test-fuzz` | 通过 |
| `make audit` | No known vulnerabilities found |
| `make test-ui` | 22 个用例通过；`tsc --noEmit` 与 `vite build` 通过 |
| `alembic upgrade head`（本地 PostgreSQL） | 迁移 20261028_0015 应用成功 |

过程中门禁自己抓出并修掉的两个真实回归（都是本轮引入的）：
1. L2 记录 scope 状态时清零 append 计数 → 在飞刷新写入成功时 trailing 刷新丢失（`8ebb64f`）；
2. 行为 metadata 取值未进指纹断言 → 变异审计新增 2 个存活变异体（`e15556b`）。

## 未在本轮验证的边界

- 真实栈（Milvus + Mem0 + 内网 LLM）的 erase 脚本与垂类 real 评测未由本轮重跑：需要起 `deploy/milvus-local` 与可达的内网网关。仓库里最新的 real 报告（`docs/memory/report/semiconductor/semi-eval-20261027-100922-3524bf.md`）是 75%，模型保真度问题仍按报告归因。
- `make load-smoke`（k6）需要真实运行中的服务与凭据，本轮未跑；脚本本身已改为断言任务终态、降级状态与召回内容。
- 多 Pod / SIGKILL / 滚动发布重叠只在清单与代码层面收口，未做集群级实验。
- 真实 PostgreSQL 只跑了迁移；跨副本 CAS 与隔离级别的行为仍以 SQLite 契约测试与代码推理为准。
