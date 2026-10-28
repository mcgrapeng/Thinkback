# Thinkback 生产加固实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修完 2026-10-28 审计报告的 stop-ship 问题，让 Thinkback 达到可发布的生产标准：数据隔离正确、删除与恢复诚实、插件契约贯通、门禁与真实栈证据闭环。

**Architecture:** 保留 Mem0 + Milvus + PostgreSQL + L1/L2/L3 三层，不换引擎。namespace 隔离通过「命名空间限定的会话作用域键」实现，使摘要唯一键、L1 缓存键、journal 查询、删除与擦除自然按 namespace 分区。所有修复先写失败测试再改实现，双仓储实现跑同一组契约用例。

**Tech Stack:** Python 3.12、FastAPI、SQLAlchemy async、PostgreSQL、Mem0 2.2.1、Milvus、gRPC/protobuf、pytest、vitest、Playwright、k6。

**Spec:** `docs/report/2026-10-28-thinkback-current-architecture-production-audit.md`（19 项发现）、`docs/report/2026-10-28-mainstream-memory-landscape.md`（引擎边界）、`docs/specs/2026-10-26-thinkback-memory-plugin-design.md`（插件目标）。

## Global Constraints

- Mem0 固定为 L3 引擎，不引入第二引擎、不引入图数据库、不引入新框架。
- 模型范围固定：`zhiman38_27b`（对话）、`zhiman-embedding`（1024 维）、`bge-reranker-v2-m3`（Xinference 重排）。密钥只作运行时秘密，不进代码与报告。
- 兼容口径：破坏性契约变更不需要兼容期（服务未上线）；但数据库迁移必须可回滚。
- 命名空间字符集 `^[A-Za-z0-9_.-]{1,64}$`（不含 `:`），因此 `namespace::session_id` 拼接无歧义。
- `round_id` 保持全局唯一（现行契约，见 `docs/memory/Thinkback记忆服务数据库迁移指南.md:18`），本轮不改主键结构。
- 完成定义：`make check` 全绿 + `make test-ui` + `make test-e2e` + `make test-fuzz` + Python/npm 供应链扫描 + 真实栈（PG + Milvus + Mem0）验证记录。
- 每个任务结束提交一次，提交信息用中文正文说明「问题 / 做法 / 证据」。

## Review Focus

- 同 user/session、不同 namespace 的写入、召回、删除、擦除互不影响。
- 删除与后台刷新并发时，已删内容不回到 L1/L2/L3。
- 崩溃恢复后的事实保留原类型、TTL 与来源时间。
- 调用方不能把受限类型降级为更宽松的数据分类。
- 自定义记忆类型在写入、列表、详情、更新、擦除全链路不丢失。

---

### Task 1: namespace 全层隔离（L1 / L2 / 删除 / 擦除）

**Files:**
- Modify: `src/thinkback/memory/service.py`（`_session_scope_id`、delete/erase 的 scope 枚举）
- Modify: `src/thinkback/memory/repositories/sqlalchemy.py`、`src/thinkback/memory/repositories/in_memory.py`（按 scope 取轮次）
- Create: `alembic/versions/20261028_0016_namespace_scoped_sessions.py`
- Test: `tests/unit/test_memory_namespace_isolation.py`（新增）、`tests/unit/test_repository_contract.py`（追加契约用例）

**Interfaces:**
- Consumes: `AppendMemoryRequest.namespace`、`JournalEntry.namespace`（已存在，迁移 20261028_0015 加入）。
- Produces: `MemoryService._session_scope_id(request) -> str` 返回 `f"{namespace}::{session_id}"`；仓储 `list_rounds(user_id, memory_scope_id, ...)` 语义不变，但调用方传入的已是命名空间限定作用域。

- [ ] **Step 1: 写失败测试**（`tests/unit/test_memory_namespace_isolation.py`）
  - `test_same_session_two_namespaces_l1_isolated`：同 user/session 写 alpha/beta，alpha 的 L1 只含 alpha 原文，L2 只含 alpha 摘要。
  - `test_delete_all_in_namespace_does_not_touch_other_namespace`：alpha ALL delete 后 beta 的 journal 仍 active、beta L3 仍 ACTIVE、beta 可召回。
  - `test_erase_session_in_namespace_keeps_other_namespace_rounds`：alpha SESSION erase 后 beta 轮次仍在。
- [ ] **Step 2: 跑测试确认失败**：`uv run --group dev pytest tests/unit/test_memory_namespace_isolation.py -v`，预期 3 条失败。
- [ ] **Step 3: 实现**：
  - `_session_scope_id` 返回 `f"{namespace}::{session_id}"`（namespace 取 `request.namespace`，缺省 `DEFAULT_NAMESPACE`）。
  - 删除/擦除枚举会话时按当前 namespace 过滤（`_user_summary_scope_ids` 改为只返回当前 namespace 前缀的作用域）。
  - 新增迁移 `20261028_0016`：把 `ins_summary_round_journal.memory_scope_id` 与 `ins_current_summary.memory_scope_id` 改写为 `namespace || '::' || memory_scope_id`（journal 用自身 namespace 列，summary 用 `'thinkback'`），downgrade 反向。
- [ ] **Step 4: 跑测试确认通过**：同 Step 2 命令，预期 3 条通过；再跑 `uv run --group dev pytest tests/unit/test_repository_contract.py -v` 预期全绿。
- [ ] **Step 5: 提交**：`git commit -m "fix(memory): namespace 隔离覆盖 L1/L2/删除/擦除"`.

### Task 2: L2 刷新删除竞态（TOCTOU）

**Files:**
- Modify: `src/thinkback/memory/l2_refresh.py`（`_run_refresh` 读取顺序与写前复核）
- Test: `tests/unit/test_l2_llm_summary.py`（追加用例）

**Interfaces:**
- Produces: `_run_refresh` 先取 `expected_row_version` 再读 rounds；写入前若排除集合变化则放弃本次写入。

- [ ] **Step 1: 写失败测试**：`test_delete_between_rounds_read_and_version_read_cannot_resurrect_summary`——用受控 composer 阻塞，删除发生在 rounds 读取后、版本读取前，断言摘要最终不是 ACTIVE 或不含已删轮次。
- [ ] **Step 2: 跑测试确认失败**：`uv run --group dev pytest tests/unit/test_l2_llm_summary.py -k resurrect -v`。
- [ ] **Step 3: 实现**：`_run_refresh` 先 `get_summary` 取 `expected_row_version`，再读 rounds；composer 返回后重新取排除集合，与读取时的集合不一致则放弃写入并记日志。
- [ ] **Step 4: 跑测试确认通过**：同 Step 2 + `uv run --group dev pytest tests/unit/test_l2_llm_summary.py -v` 全绿。
- [ ] **Step 5: 提交**：`git commit -m "fix(memory): L2 刷新先取版本再读轮次，杜绝删除后复活"`.

### Task 3: 数据分类不可被请求降级

**Files:**
- Modify: `src/thinkback/domain/classification.py`、`src/thinkback/memory/service.py`（append 的 metadata 合并）
- Test: `tests/unit/test_classification_policy.py`（新增）

**Interfaces:**
- Produces: `resolve_strict_classification(request_classification: str | None, policy_classification: str) -> str`，取两者中更严格者（normal < personal < sensitive < restricted）。

- [ ] **Step 1: 写失败测试**：restricted 类型 + 请求 normal，断言落库与后端分类仍为 restricted，且请求 normal 落库时被拒或被提升。
- [ ] **Step 2: 跑测试确认失败**。
- [ ] **Step 3: 实现**：新增 `resolve_strict_classification`；append 用它合并请求与类型策略的 classification，再走 `assert_storable`。
- [ ] **Step 4: 跑测试确认通过**。
- [ ] **Step 5: 提交**：`git commit -m "fix(memory): 数据分类取请求与类型策略中更严格的一级"`.

### Task 4: 崩溃恢复保留策略并分页

**Files:**
- Modify: `src/thinkback/memory/service.py`（`recover_pending_writes`）
- Test: `tests/unit/test_memory_service.py`（追加用例）

**Interfaces:**
- Consumes: 已有 `MemoryService._replay_metadata(entry)`。
- Produces: `recover_pending_writes` 用 `request_metadata=self._replay_metadata(entry)`，并按 `list_tasks(offset=...)` 分页直到取完。

- [ ] **Step 1: 写失败测试**：`test_recovery_preserves_custom_type_and_ttl`（state 类型恢复后仍 state 且 TTL 保留）；`test_recovery_processes_more_than_200_pending_tasks`（造 201 个 pending 任务，断言全部恢复）。
- [ ] **Step 2: 跑测试确认失败**。
- [ ] **Step 3: 实现**：分页循环 + `_replay_metadata`。
- [ ] **Step 4: 跑测试确认通过**。
- [ ] **Step 5: 提交**：`git commit -m "fix(memory): 崩溃恢复保留类型与 TTL，并分页处理全部待恢复任务"`.

### Task 5: 暗数据归零（抽取前后同一准入）

**Files:**
- Modify: `src/thinkback/memory/service.py`（`_run_l3_write` 准入判定与后置失败回滚）
- Test: `tests/unit/test_write_pre_filter.py`（追加用例）

**Interfaces:**
- Produces: 抽取前用类型准入决定「是否抽取」；后置索引不通过时调用 `backend.delete_many(event_ids)` 回滚并返回 `SKIP(reason=...)`。

- [ ] **Step 1: 写失败测试**：白名单仅含 current_location + `force_extract=true` + 非 P0 内容，断言 backend 无残留、响应不报 completed。
- [ ] **Step 2: 跑测试确认失败**。
- [ ] **Step 3: 实现**：抽取前按类型与白名单判定；后置失败回滚后端记录。
- [ ] **Step 4: 跑测试确认通过**。
- [ ] **Step 5: 提交**：`git commit -m "fix(memory): 抽取前后使用同一准入，消灭不可召回暗数据"`.

### Task 6: 擦除语义诚实化（Mem0 消息与暂不可见）

**Files:**
- Modify: `src/thinkback/memory/service.py`（erase 阶段）、`src/thinkback/memory/backends/mem0_library.py`（`purge_history` / `delete`）
- Test: `tests/unit/test_memory_service.py`（追加用例）

**Interfaces:**
- Produces: `Mem0LibraryMemoryBackend.purge_history(user_id, memory_ids, scopes)` 在给定 scope 时删除该 scope 的 Mem0 原始消息；erase 的 `scopes` 参数对 SESSION 范围传该会话的 session scope。

- [ ] **Step 1: 写失败测试**：`test_erase_session_purges_engine_messages_for_that_session`（断言 `purge_history` 收到非空 scopes 且只含该会话）；`test_erase_reports_residual_when_backend_cannot_confirm_delete`。
- [ ] **Step 2: 跑测试确认失败**。
- [ ] **Step 3: 实现**：会话范围传 session scope；无法确认删除时响应体/审计标注 residual。
- [ ] **Step 4: 跑测试确认通过**。
- [ ] **Step 5: 提交**：`git commit -m "fix(memory): 擦除按会话清理引擎消息并如实标注残留"`.

### Task 7: 契约诚实化（PERSONAL 脱敏、抽取提示词语言）

**Files:**
- Modify: `src/thinkback/domain/enums.py`、`src/thinkback/memory/schemas.py`、`src/thinkback/memory/backends/mem0_library.py`
- Test: `tests/unit/test_memory_contracts.py`（追加用例）

- [ ] **Step 1: 写失败测试**：`test_default_instructions_do_not_force_chinese_only`（断言提示词不含「纯中文」硬约束且保留「跟随对话语言」）；`test_personal_classification_contract_states_no_automatic_redaction`。
- [ ] **Step 2: 跑测试确认失败**。
- [ ] **Step 3: 实现**：改提示词第 6 条为「跟随来源语言，实体逐字保留」；把 PERSONAL 的注释与 schema 描述改为「允许原文存储，不自动脱敏，由调用方负责前置处理」。
- [ ] **Step 4: 跑测试确认通过**。
- [ ] **Step 5: 提交**：`git commit -m "fix(memory): 抽取提示词不再强制纯中文，PERSONAL 契约改为不做自动脱敏"`.

### Task 8: gRPC 生命周期契约补齐

**Files:**
- Modify: `proto/memory.proto`、`src/thinkback/rpc/converters.py`
- Test: `tests/unit/test_memory_grpc.py`（追加用例）

**Interfaces:**
- Produces: proto 新增 `OPERATION_TYPE_ERASE = 6`（不复用既有编号）；`converters._OP_TYPE_TO_PB` 含 ERASE；`ManagedMemoryItem.memory_type` 在 proto 侧保持枚举兼容，另加 `string memory_type_name` 承载自定义类型。

- [ ] **Step 1: 写失败测试**：`test_task_response_for_erase_operation_converts`（当前 `KeyError`）。
- [ ] **Step 2: 跑测试确认失败**。
- [ ] **Step 3: 实现**：改 proto + 转换表 + 重新生成 pb2（`make proto-gen`）。
- [ ] **Step 4: 跑测试确认通过**。
- [ ] **Step 5: 提交**：`git commit -m "fix(grpc): 补齐 erase 操作类型，任务查询不再抛 KeyError"`.

### Task 9: 自定义类型与自定义属性在协议层不丢失

**Files:**
- Modify: `src/thinkback/memory/schemas.py`、`src/thinkback/memory/service.py`、`proto/memory.proto`、`src/thinkback/rpc/converters.py`
- Test: `tests/unit/test_memory_service.py`、`tests/unit/test_memory_grpc.py`

**Interfaces:**
- Produces: `ManagedMemoryItem.memory_type: str | None`（保留任意注册类型名）；`UpdateMemoryRequest.memory_type: str | None`；`MemoryItem.metadata` 保留 `custom_metadata` 命名空间化字典。

- [ ] **Step 1: 写失败测试**：`test_custom_memory_type_visible_in_managed_item`（state/process_preference 不再变 None）；`test_custom_metadata_survives_to_backend`。
- [ ] **Step 2: 跑测试确认失败**。
- [ ] **Step 3: 实现**：`_managed_memory_item` 直接透传字符串类型名；`_l3_metadata` 保留 `custom_metadata` 子字典并传给后端。
- [ ] **Step 4: 跑测试确认通过**。
- [ ] **Step 5: 提交**：`git commit -m "feat(memory): 自定义记忆类型与自定义属性在协议层保留"`.

### Task 10: profile 进入镜像并 fail-visible

**Files:**
- Modify: `Dockerfile`、`src/thinkback/api/dependencies.py`、`src/thinkback/infra/config.py`
- Test: `tests/unit/test_config.py`（追加用例）

**Interfaces:**
- Produces: `Settings.memory_type_profile_dir` 默认 `profiles`；生产环境（`ENVIRONMENT=production`）加载非默认 namespace 的 profile 失败时抛错而非回落默认注册表。

- [ ] **Step 1: 写失败测试**：`test_production_profile_load_failure_is_not_silently_downgraded`。
- [ ] **Step 2: 跑测试确认失败**。
- [ ] **Step 3: 实现**：Dockerfile `COPY profiles/ /app/profiles/`；`build_registry_provider` 在 production 且 namespace 非默认时失败即抛。
- [ ] **Step 4: 跑测试确认通过**。
- [ ] **Step 5: 提交**：`git commit -m "fix(config): profile 进入镜像，生产加载失败不再静默回落"`.

### Task 11: 单测隔离与启动时序确定

**Files:**
- Modify: `tests/unit/test_repository_shutdown.py`、`tests/conftest.py`（新增）、`src/thinkback/api/app.py`（可选：启动时同步完成廉价初始化）
- Test: 同上

- [ ] **Step 1: 写失败测试**：`test_lifespan_startup_does_not_require_real_dependencies`（断言单测不连真实 PG/Milvus）。
- [ ] **Step 2: 跑测试确认失败**。
- [ ] **Step 3: 实现**：测试侧注入 fake 服务并禁用迁移/Milvus 探针；lifespan 启动时同步构造服务单例（后台线程只做恢复与回收）。
- [ ] **Step 4: 跑测试确认通过**，并连续跑 3 次 `pytest tests/unit/test_repository_shutdown.py -q` 稳定。
- [ ] **Step 5: 提交**：`git commit -m "test(unit): 启动时序确定化，单测不再依赖真实 PG/Milvus"`.

### Task 12: 前端门禁、供应链与 E2E 修复

**Files:**
- Modify: `pyproject.toml`（dev 依赖加 playwright）、`web/package.json`（依赖升级）、`tests/e2e/test_admin_smoke.py`（断言改为当前契约）、`.github/workflows/ci.yml`
- Test: `tests/e2e/*`

- [ ] **Step 1: 写失败测试**：把 e2e 的「API Keys」断言改为当前接入页真实文案（内网免鉴权契约）。
- [ ] **Step 2: 跑测试确认失败**：`uv run --group dev pytest tests/e2e -v`。
- [ ] **Step 3: 实现**：加 playwright dev 依赖并 `npm audit fix`；e2e 断言更新；CI 增加 `make check`、web build/test、e2e、npm audit 门禁。
- [ ] **Step 4: 跑测试确认通过**：`make test-e2e`、`make test-ui`、`npm audit --audit-level=high`。
- [ ] **Step 5: 提交**：`git commit -m "ci: 前端与 e2e 进入门禁，修复过期断言与依赖漏洞"`.

### Task 13: 部署入口与负载门禁

**Files:**
- Create: `k8s/kustomization.yaml`
- Modify: `tests/load/smoke.k6.js`（thresholds 加 `checks: ["rate==1"]`）
- Test: `tests/load/smoke.k6.js`

- [ ] **Step 1: 写失败测试**：k6 脚本断言 checks 参与退出码（静态检查 thresholds 含 `checks`）。
- [ ] **Step 2: 跑测试确认失败**。
- [ ] **Step 3: 实现**：新增 kustomization 聚合现有清单；k6 thresholds 增 `checks: ["rate==1"]`；`kubectl kustomize k8s` 可渲染。
- [ ] **Step 4: 跑测试确认通过**：`kubectl kustomize k8s` 退出 0。
- [ ] **Step 5: 提交**：`git commit -m "fix(deploy): 补 kustomization 入口，负载正确性纳入退出码"`.

### Task 14: 真实栈证据与发布清单

**Files:**
- Modify: `tests/script/real_mem0_erase_verification.py`（补 namespace 与 messages 断言）
- Create: `docs/report/2026-10-28-thinkback-production-readiness.md`

- [ ] **Step 1: 写失败测试**：真实擦除脚本新增「同 session 不同 namespace 互不影响」「Mem0 messages 无残留」断言。
- [ ] **Step 2: 跑测试确认失败**（在真实栈上）。
- [ ] **Step 3: 实现**：脚本补断言；跑真实栈 erase + 真实评测 + 负载冒烟 + SIGKILL 恢复。
- [ ] **Step 4: 跑测试确认通过**：归档报告与基线。
- [ ] **Step 5: 提交**：`git commit -m "docs: 记录发布 SHA 的真实栈与故障注入证据"`.
