# Thinkback 审计修复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务执行。每个任务必须先写失败测试（RED），再改实现（GREEN），再提交。

**Goal:** 修复 2026-10-27 审计报告里的 18 项问题，让 Thinkback 达到可生产发布的内网记忆服务标准。

**Architecture:** 不换记忆引擎，不加新依赖，不重写 MemoryService。改动集中在四个面：API 边界（幂等缓存、错误投影）、擦除语义（屏障、续跑、覆盖范围）、执行所有权（append 单次执行、任务租约、崩溃恢复）、记忆生命周期（TTL、namespace 幂等、重建时间仲裁、类型策略）。数据库改动合并为一次 Alembic 迁移。

**Tech Stack:** Python 3.12、FastAPI、SQLAlchemy 2 async + asyncpg、Alembic、Mem0 2.2.1、Milvus、pytest、ruff、mypy strict。

**Spec:** 本次审计结论（会话内）+ `.planning/2026-10-26-thinkback-memory-audit/findings.md`（历史线索，须以当前源码复核）。

## Global Constraints

- 完成定义：`make check` 全绿（format-check + lint + typecheck + mutation-audit + eval）且 `make test` 全绿。
- 行为变更必须先有失败测试；禁止测试后补。
- 仓储双实现同行为：改 `InMemoryMemoryRepository` 或 `SqlAlchemyMemoryRepository` 后必须跑 `tests/unit/test_repository_contract.py`。
- 测试确定性：无网络、无真实 LLM/Milvus/PostgreSQL，用 `FakeMemoryBackend` 与内存仓储。
- 不新增运行时依赖；不引入 Celery/Redis/图数据库/reranker。
- 不改三层记忆模型与 Mem0 引擎选型。
- 提交只包含本任务拥有的文件，禁止 `git add .`；用户已有的 `tests/script/run_self_contained_evaluation.py` 改动不进任何提交。
- 兼容既有公开契约的部分：HTTP 状态码语义变化（async append 的 `completed`→`accepted`）需同步更新 schemas 文档与测试。

## Review Focus

- 擦除后同一 Idempotency-Key 的 recall 不得再返回旧内容。
- 一轮只产生一条 L3 事实：并发首写、重试、崩溃重启三条路径都要覆盖。
- 过期事实不得从任何路径回到上下文：语义召回、精确槽位回填、`/items`、治理台列表。
- 同一 round 在不同 namespace 必须显式冲突，不得静默丢写。
- 审计记录不得声称完成了实际未完成的擦除。

---

## Task 1: recall 不进入幂等响应缓存

**Files:**
- Modify: `src/thinkback/api/idempotency.py`
- Test: `tests/unit/test_idempotency.py`

- [ ] 写失败测试：`test_recall_response_is_not_replayed_after_erase`（recall + Idempotency-Key → erase → 同 key 再 recall 不得返回旧内容）。
- [ ] 运行该测试，确认 RED（当前返回缓存旧 body）。
- [ ] 实现：幂等中间件只对写操作（append/delete/suppress/erase/update/rebuild）缓存响应；`/memory/recall` 走无缓存路径。
- [ ] 运行 `pytest tests/unit/test_idempotency.py -q` 通过。
- [ ] 提交：`fix(api): recall 不再进入幂等响应缓存`。

## Task 2: 校验错误不回显正文与秘密

**Files:**
- Modify: `src/thinkback/api/errors_standard.py`
- Test: `tests/unit/test_api.py`

- [ ] 写失败测试：`test_validation_error_does_not_echo_input`（带 secret 的 body 触发 422，响应体与日志都不含 secret 原文）。
- [ ] 运行确认 RED。
- [ ] 实现：把 `exc.errors()` 投影为 `loc/type/msg`（去掉 `input`/`ctx`），日志与响应共用同一投影函数。
- [ ] 运行 `pytest tests/unit/test_api.py -q` 通过。
- [ ] 提交：`fix(api): 校验错误不回显输入内容与上下文`。

## Task 3: 擦除覆盖全部状态与仅有 L1/L2 的会话

**Files:**
- Modify: `src/thinkback/memory/service.py`
- Test: `tests/unit/test_memory_service.py`

- [ ] 写失败测试：`test_erase_session_without_l3_removes_round_and_summary`（只有 L1/L2、0 条 L3 → erase SESSION 后 round 与 summary 文本消失）。
- [ ] 写失败测试：`test_erase_after_suppress_removes_retained_plaintext`（先 suppress 再 erase → DELETED 索引行、journal、summary 都被清）。
- [ ] 写失败测试：`test_erase_all_deletes_backend_only_memories`（后端有本地无索引的记录 → erase ALL 后消失，调用 backend.delete_all）。
- [ ] 运行确认三条 RED。
- [ ] 实现：擦除目标来源改为「该 user+scope 下所有非墓碑记忆（含 DELETED/SUPERSEDED/SUPPRESSED）」；SESSION/ALL 目标为空时也要执行 L1/L2 清理；ALL 额外调用 `backend.delete_all(user_id, scope)`。
- [ ] 运行 `pytest tests/unit/test_memory_service.py -q` 通过。
- [ ] 提交：`fix(memory): erase 覆盖全部状态、仅有 L1/L2 的会话与后端暗数据`。

## Task 4: 擦除可续跑并有真实任务

**Files:**
- Modify: `src/thinkback/memory/service.py`
- Test: `tests/unit/test_memory_service.py`

- [ ] 写失败测试：`test_erase_retry_after_purge_failure_finishes_cleanup`（首次 purge 抛错 → 重试同 operation_id 必须继续清完 L1/L2，返回 completed 且 journal 已空）。
- [ ] 写失败测试：`test_erase_task_id_is_queryable`（返回的 task_id 能在 `get_task` 查到）。
- [ ] 运行确认 RED。
- [ ] 实现：擦除走 `claim_task` 落一条 `memory-erase:{operation_id}` 任务，目标 ID 与阶段进度写 `task.result`；本地索引删除放到所有外部副作用成功之后；重试按 `task.result` 续跑。
- [ ] 运行 `pytest tests/unit/test_memory_service.py -q` 通过。
- [ ] 提交：`fix(memory): erase 任务化并支持失败续跑`。

## Task 5: 擦除屏障与 L2 摘要版本

**Files:**
- Modify: `src/thinkback/memory/service.py`, `src/thinkback/memory/l2_refresh.py`
- Modify: `src/thinkback/memory/repositories/in_memory.py`, `src/thinkback/memory/repositories/sqlalchemy.py`
- Test: `tests/unit/test_memory_service.py`, `tests/unit/test_l2_llm_summary.py`, `tests/unit/test_repository_contract.py`

- [ ] 写失败测试：`test_erase_blocks_inflight_l3_append`（backend.add 阻塞期间 erase → 释放后不得出现 ACTIVE 记忆与复活 journal）。
- [ ] 写失败测试：`test_erase_blocks_rebuild_from_history`（外部 HistorySource 仍含该轮 → rebuild 不得重新抽出已擦除事实）。
- [ ] 写失败测试：`test_erase_invalidates_inflight_l2_refresh`（composer 阻塞期间 erase → 释放后摘要不得写回 ACTIVE）。
- [ ] 写失败测试（契约）：`clear_summary` 后摘要 `summary_state=DIRTY` 且 `row_version` 递增（内存与 SQL 两实现一致）。
- [ ] 运行确认 RED。
- [ ] 实现：新增内容无关的 erase 屏障（用户+scope+round 的墓碑），append 落库与 L3 索引、rebuild 过滤都检查它；`clear_summary` 在两种仓储里置 DIRTY 并递增 `row_version`。
- [ ] 运行 `pytest tests/unit/test_memory_service.py tests/unit/test_l2_llm_summary.py tests/unit/test_repository_contract.py -q` 通过。
- [ ] 提交：`fix(memory): 擦除建立写入屏障并让 L2 摘要失效`。

## Task 6: Mem0 消息清理范围与审计三态

**Files:**
- Modify: `src/thinkback/memory/service.py`, `src/thinkback/memory/backends/mem0_library.py`
- Test: `tests/unit/test_memory_service.py`, `tests/unit/test_memory_backends.py`

- [ ] 写失败测试：`test_erase_memory_scope_keeps_other_session_messages`（同 user+namespace 两个会话，擦除一个会话的记录后另一会话的 Mem0 原始消息仍在）。
- [ ] 写失败测试：`test_erase_audit_reports_partial_when_nothing_purged`（后端未配置历史库时审计不得写 purged=true）。
- [ ] 运行确认 RED。
- [ ] 实现：MEMORY/SESSION 擦除只按 memory_id 清理 history 行，不做 namespace 级 messages 删除；ALL 才做 scope 级清理并标注；审计 detail 改为 `mem0_history_purged` ∈ {complete, partial, unsupported}。
- [ ] 运行 `pytest tests/unit/test_memory_service.py tests/unit/test_memory_backends.py -q` 通过。
- [ ] 提交：`fix(memory): 擦除只清理目标消息并如实标注审计状态`。

## Task 7: append 单次执行与 add 结果对账

**Files:**
- Modify: `src/thinkback/memory/service.py`
- Test: `tests/unit/test_memory_service.py`

- [ ] 写失败测试：`test_concurrent_first_append_executes_backend_add_once`（两个服务实例共享仓储并发首写同一 round → backend.add 只发生一次）。
- [ ] 写失败测试：`test_append_retry_after_index_failure_reuses_backend_result`（backend.add 成功、本地索引失败 → 重试不再新增后端记录）。
- [ ] 运行确认 RED。
- [ ] 实现：append 在任何 L3 工作前用 `claim_task` 选唯一执行者，loser 返回既有任务状态；add 成功后把 backend ID 写进 `task.result`，重试先查该结果再决定是否重新抽取。任务 ID 统一为 `memory-extract:{namespace}:{round_id}`（含 namespace），相关测试同步更新。
- [ ] 运行 `pytest tests/unit/test_memory_service.py -q` 通过。
- [ ] 提交：`fix(memory): append 只允许一个执行者并对账后端结果`。

## Task 8: 任务租约、异步状态与崩溃恢复

**Files:**
- Modify: `src/thinkback/memory/service.py`, `src/thinkback/memory/repositories/in_memory.py`, `src/thinkback/memory/repositories/sqlalchemy.py`
- Modify: `src/thinkback/infra/database/models.py`, `alembic/versions/<new>.py`
- Modify: `src/thinkback/memory/schemas.py`
- Test: `tests/unit/test_memory_service.py`, `tests/unit/test_task_state.py`, `tests/unit/test_repository_contract.py`

- [ ] 写失败测试：`test_healthy_long_task_is_not_reclaimed`（任务运行超过 orphan 阈值仍不得被标 FAILED）。
- [ ] 写失败测试：`test_async_append_returns_accepted_until_task_finishes`（async 模式响应状态为 accepted，不返回 completed）。
- [ ] 写失败测试：`test_startup_replays_accepted_write_tasks`（进程重启后，未完成的 L3 写入按 journal 重放，不丢事实）。
- [ ] 运行确认 RED。
- [ ] 实现：任务表加 `attempt`/`lease_expires_at`/`heartbeat_at`；回收只回收过期租约；旧 attempt 不得写终态；async append 返回 `accepted`；启动时把 `WRITE_ROUND` 且未完成的任务按 journal 重放（journal 需存 namespace 与请求 metadata）。迁移 M1：本任务新增 `ins_memory_task.attempt/lease_expires_at/heartbeat_at`、`ins_summary_round_journal.namespace`、`ins_summary_round_journal.request_metadata`。
- [ ] 运行相关测试通过 + `pytest tests/unit/test_repository_contract.py -q` 通过。
- [ ] 提交：`fix(memory): 任务引入租约与心跳，async 状态诚实且崩溃可恢复`。

## Task 9: L2 有界提交与 scope 状态淘汰

**Files:**
- Modify: `src/thinkback/memory/l2_refresh.py`
- Test: `tests/unit/test_l2_llm_summary.py`

- [ ] 写失败测试：`test_l2_respects_pending_task_limit`（pending 上限 1 时最多排 1 个 L2 刷新，多余的退回 concat 摘要）。
- [ ] 运行确认 RED。
- [ ] 实现：L2 提交走 L3 executor 的容量信号量，满则跳过本轮并保留 concat；`_append_counters`/`_llm_refreshed_scopes` 改为有界 LRU。
- [ ] 运行 `pytest tests/unit/test_l2_llm_summary.py -q` 通过。
- [ ] 提交：`fix(memory): L2 刷新有界提交并淘汰历史 scope 状态`。

## Task 10: TTL 在本地生命周期生效

**Files:**
- Modify: `src/thinkback/memory/service.py`, `src/thinkback/domain/entities.py`
- Modify: `src/thinkback/memory/repositories/in_memory.py`, `src/thinkback/memory/repositories/sqlalchemy.py`
- Modify: `src/thinkback/infra/database/models.py`, `alembic/versions/<new>.py`（迁移 M2）
- Test: `tests/unit/test_memory_service.py`, `tests/unit/test_repository_contract.py`

- [ ] 写失败测试：`test_expired_memory_is_not_returned_by_slot_backfill`（后端已隐藏过期记录时，精确槽位查询不得从本地回填过期事实）。
- [ ] 写失败测试：`test_expired_memory_is_not_listed`（`/items` 不返回过期记忆）。
- [ ] 运行确认 RED。
- [ ] 实现：`MemoryIndexEntry` 与 `ins_memory` 加 `expires_at`；索引时按类型 TTL 写入；recall 语义结果、槽位回填、列表统一过滤过期。迁移 M2：`ins_memory.expires_at`。
- [ ] 运行相关测试通过。
- [ ] 提交：`fix(memory): TTL 落到本地索引并在召回与列表统一过滤`。

## Task 11: namespace 进入 append 幂等身份

**Files:**
- Modify: `src/thinkback/memory/service.py`, `src/thinkback/domain/keys.py`
- Test: `tests/unit/test_memory_service.py`

- [ ] 写失败测试：`test_same_round_in_other_namespace_conflicts`（同一 round 换 namespace → 显式 409 冲突，不得 already_done 丢写）。
- [ ] 运行确认 RED。
- [ ] 实现：journal 记录 namespace；请求指纹含 namespace；round 冲突检查比较 namespace，不一致时抛冲突。
- [ ] 运行 `pytest tests/unit/test_memory_service.py -q` 通过。
- [ ] 提交：`fix(memory): namespace 进入 append 幂等身份，跨 namespace 同 round 显式冲突`。

## Task 12: rebuild 用来源时间与来源类型策略

**Files:**
- Modify: `src/thinkback/memory/service.py`, `src/thinkback/domain/slots/support.py`
- Modify: `src/thinkback/domain/entities.py`, `src/thinkback/infra/database/models.py`, `alembic/versions/<new>.py`（迁移 M3）
- Test: `tests/unit/test_memory_service.py`

- [ ] 写失败测试：`test_rebuild_uses_source_timestamp_for_conflict_order`（外部 history 中新上海/旧杭州 → 旧杭州不得被 supersede）。
- [ ] 写失败测试：`test_rebuild_preserves_memory_type_policy`（rebuild 后索引行仍带原 memory_type 与 TTL）。
- [ ] 运行确认 RED。
- [ ] 实现：journal 存请求 metadata；重建按 source_timestamp 排序；valid_at 取来源时间，另存 recorded_at=now；重放使用来源事件的有效类型策略。迁移 M3：`ins_memory.recorded_at`。
- [ ] 运行 `pytest tests/unit/test_memory_service.py -q` 通过。
- [ ] 提交：`fix(memory): rebuild 用来源时间仲裁并保留类型策略`。

## Task 13: Milvus 一致性设置指向正确库并可重试

**Files:**
- Modify: `src/thinkback/infra/milvus.py`, `src/thinkback/memory/backends/mem0_library.py`
- Test: `tests/unit/test_milvus_consistency.py`, `tests/unit/test_memory_backends.py`

- [ ] 写失败测试：`test_consistency_helper_uses_configured_database`（构造 MilvusClient 时带 db_name）。
- [ ] 写失败测试：`test_consistency_is_retried_until_strong`（首次 skipped → 后续写入仍会再次尝试）。
- [ ] 运行确认 RED。
- [ ] 实现：helper 传 `db_name`，client 构造纳入异常处理；只在真正返回 Strong 后置 ensured 标志。
- [ ] 运行相关测试通过。
- [ ] 提交：`fix(memory): Milvus Strong 一致性指向正确库并可重试`。

## Task 14: 部署边界与发布策略

**Files:**
- Create: `k8s/networkpolicy.yaml`
- Modify: `k8s/deployment-api.yaml`, `k8s/README.md`, `docs/DEPLOYMENT.md`

- [ ] 写失败检查（无自动化测试则记录替代验证）：`kubectl apply --dry-run=client` 解析新清单通过；清单含 default-deny NetworkPolicy。
- [ ] 实现：默认拒绝入站，仅放行网关/指定 namespace 到 8000/50051；滚动策略改 Recreate；文档写明多副本仍需共享状态。
- [ ] 验证：清单解析通过，文档与清单一致。
- [ ] 提交：`feat(k8s): 补默认拒绝网络策略并改为 Recreate 发布`。

## Task 15: 类型注册表只保留有效语义

**Files:**
- Modify: `src/thinkback/domain/memory_types.py`, `src/thinkback/memory/service.py`
- Modify: `profiles/semiconductor.json`
- Test: `tests/unit/test_memory_types.py`, `tests/unit/test_memory_service.py`

- [ ] 写失败测试：`test_event_type_append_keeps_both_events`（event 类型连续写两个地点事实 → 两条都在，旧的不被 supersede）。
- [ ] 运行确认 RED。
- [ ] 实现：`conflict=append` 的类型跳过 P0 slot supersede；删除没有行为消费者的 `recall_budget`、`entity_vocabulary`、`layer` 字段与 profile 中的对应配置；`description` 若未接入 prompt 则一并删除。
- [ ] 运行 `pytest tests/unit/test_memory_types.py tests/unit/test_memory_service.py -q` 通过。
- [ ] 提交：`fix(memory): 类型注册表只保留有效语义，append 类型不再被覆盖`。

## Task 16: 降级模式下复合查询回填全部槽位

**Files:**
- Modify: `src/thinkback/memory/service.py`
- Test: `tests/unit/test_memory_service.py`

- [ ] 写失败测试：`test_degraded_recall_backfills_all_requested_slots`（后端不可用时 location+food 查询回填两条本地事实）。
- [ ] 运行确认 RED。
- [ ] 实现：backfill 接收完整 slot 列表，逐槽位取最新合格项。
- [ ] 运行 `pytest tests/unit/test_memory_service.py -q` 通过。
- [ ] 提交：`fix(memory): 降级召回回填全部请求槽位`。

## Task 17: 门禁与依赖

**Files:**
- Modify: `.github/workflows/ci.yml`, `Makefile`, `pyproject.toml`, `uv.lock`
- Modify: `tests/load/smoke.k6.js`, `tests/fuzz/test_openapi_contract.py`
- Modify: `src/thinkback/api/app.py`（OpenAPI 响应声明）
- Test: `tests/fuzz/test_openapi_contract.py`

- [ ] 写失败检查：`make test-fuzz` 当前失败（未声明 429/503），修复后必须绿。
- [ ] 写失败检查：`pip-audit` 当前报 3 个包 9 条记录，升级后必须绿。
- [ ] 实现：CI 去掉两个 ignore；`make check` 依赖 `make test`；mutmut 新 survivor 阻断；OpenAPI 声明 429/503 且 fuzz 隔离限流状态；k6 断言 task 终态、degraded 与召回事实；升级 certifi/filelock/setuptools。
- [ ] 验证：`make test-fuzz` 绿、`make audit` 绿、`make check` 绿。
- [ ] 提交：`ci: 收紧门禁并升级已知漏洞依赖`。

## Task 18: 评测与文档对齐

**Files:**
- Modify: `tests/script/eval/run_semiconductor_evaluation.py`, `tests/eval/baseline_semiconductor.json`
- Modify: `README.md`, `README.zh-CN.md`, `docs/API.md`, `docs/SDK.md`, `docs/DEPLOYMENT.md`, `docs/ROADMAP.md`
- Delete: `tests/script/run_real_smoke_test.py`
- Modify: `tests/script/realchain/fullchain_http.py`, `tests/script/real_mem0_quality_regression.py`, `tests/script/eval/zh_replay_eval.py`, `tests/script/run_real_smoke_test.py` 相关脚本

- [ ] 写失败检查：自包含评测报告标注为契约套件；真实评测报告把失败分为「抽取为空 / 抽取漏事实 / 检索漏事实」三类。
- [ ] 实现：评测报告加失败分类与真实模式阈值文件；删除 522 行不可运行 smoke script；其余真实脚本去掉已删除的 `intent` 字段；文档按当前 OpenAPI/proto 修正 v1、API key、prompt editing、check 口径、端口与扩容描述。
- [ ] 验证：脚本 import 全部成功；文档不再出现已删除接口。
- [ ] 提交：`docs: 评测分型并修正接入与部署文档`。

## Task 19: 总验证与复审

- [ ] `make check` 全绿。
- [ ] `make test` 全绿（含新增回归测试）。
- [ ] `make test-fuzz`、`make audit`、`make test-ui` 全绿。
- [ ] 逐条重跑审计里的 18 个反例，确认全部转为通过或被明确降级记录。
- [ ] 对最终 diff 做一次独立代码审查（Standards + Spec 两条轴），修掉 Critical/High。
- [ ] 写 `docs/report/2026-10-27-thinkback-audit-fixes-verification.md` 记录每个反例的修复证据与剩余边界。
