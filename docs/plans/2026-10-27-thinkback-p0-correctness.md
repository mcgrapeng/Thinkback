# Thinkback P0 正确性修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修掉审计已复现的严重正确性缺陷：删除后内容复活、L2 摘要被旧刷新覆盖、update 失败谎报完成、幂等跨端点串答、assistant 断言变长期事实、敏感分类不生效。

**Architecture:** 不改三层模型，不换 Mem0，不动对外契约。本计划只改 `src/thinkback/memory/`、`src/thinkback/domain/`、`src/thinkback/api/` 内的既有实现，全部用 InMemory + Fake 后端做确定性验证。

**Tech Stack:** Python 3.12 · FastAPI · SQLAlchemy(async) + asyncpg · Pydantic v2 · pytest

**Spec:** `docs/specs/2026-10-26-thinkback-memory-plugin-design.md`（D9 引擎优先、D10 内网免鉴权、§2.3 审计项、§10 质量门禁）

**后续计划（不在本文件范围）：** P0 引擎与门禁（mem0 已升级 2.1.0→2.2.1，856 用例全绿；真实 Milvus 契约验证因本机 arm64 容器启动即崩溃而未完成，需换 x86_64 实例或兼容镜像重跑）、P1 内网化收口、P2 插件模型、P3 接入生态。

## Global Constraints

- 行为变更随附测试：先写会红的测试，再改实现。删测试与改行为在同一个 commit。
- 仓储双实现同行为：改 `SqlAlchemyMemoryRepository` 或 `InMemoryMemoryRepository` 后必须跑 `tests/unit/test_repository_contract.py`。
- 测试确定性：不引入网络，不连真实 LLM / Milvus / Mem0。
- 完成定义：`make check` 全绿（format-check + lint + typecheck + mutation-audit + eval），`make test` 全绿。
- Mem0 是固定引擎，不要在 Thinkback 里重实现抽取、检索、去重、过期。
- 免鉴权（D10）：业务接口不引入鉴权代码；`/admin/api/*` 的 ADMIN_TOKEN 不动。
- 门禁在本地跑：真实 Mem0/Milvus 契约套件是本地必跑项，不要求进 CI；CI 保持现状。
- 每个任务单独提交，commit message 描述改了什么行为。

## Review Focus

以下五类输入最容易在真实使用中咬人，各自由对应任务的测试钉住：

1. 删除与写入并发：用户删除会话的同时上一轮抽取还在跑，已删内容不得复活（Task 1）。
2. 后台刷新与删除并发：删完之后旧的 LLM 摘要不得把已删内容写回来（Task 2）。
3. 后端写失败：Mem0 写失败时客户端不得收到 `completed`（Task 3）。
4. 同一把 Idempotency-Key 用在不同端点：append 的缓存响应不得被 recall 领走（Task 4）。
5. 助手的自述被当成用户事实：用户没说过的事不得进入长期记忆（Task 5）。

---

## 完成状态（2026-10-27 回填）

| 任务 | 状态 | 提交 |
| --- | --- | --- |
| Task 1 删除屏障 | 完成 | `505ae4d` |
| Task 1.5 读写等待预算解耦（flaky 用例） | 完成 | `e7f7e0a`（随 Task 2 一并提交） |
| Task 2 L2 摘要乐观锁 | 完成 | `e7f7e0a` |
| Task 3 update 失败不谎报 completed | 完成 | `6dfe72b` |
| Task 4 幂等键区分端点 + 指纹 canonical 化 | 完成 | `60a0920` |
| Task 5 assistant 断言不进长期事实 | 完成 | （与 Task 6 同批） |
| Task 6 敏感分类落库前拦截 | 完成 | `4369d9c` |
| P1 内网化收口（方案二） | 完成 | `04b33e5`（gRPC 去鉴权）、`1ac2a06`（HTTP 去 v1 与鉴权、限流幂等按 IP） |
| mem0 引擎升级 | 完成 | `29a3719`（2.1.0 → 2.2.1） |
| tenant 碰撞止血 | 完成 | `a254517` |

验收结果：`mypy` 无错误；全套件 856 passed；`make eval` 20/20、baseline 达标；`make mutation-audit` 已跑。
`ruff format/check` 仅剩用户 WIP 文件 `tests/unit/test_api_key_auth.py` 的 SIM300 与 B017，未改动该文件。

**方案二落地说明**：业务面只保留 `/memory/*`，v1 与 API Key/Scope/Tenant 机制已删除；`/admin/api/*` 仍要求 ADMIN_TOKEN；限流与幂等按客户端 IP；`/v1/memory/append` 实测 404、`/memory/append` 正常进入校验。真实 Milvus 契约验证仍未通过（arm64 容器崩溃），门禁尚未满足。

---

### Task 1: 删除屏障，让在飞写入无法复活已删内容

**Files:**
- Modify: `src/thinkback/memory/service.py`（`_delete_session_memories`、`_delete_all_memories`、`_add_session_delete_tombstone`、`_add_delete_all_tombstone`、`append` 的 `mark_round_active` 调用点）
- Modify: `src/thinkback/domain/ports.py`、`src/thinkback/memory/repositories/sqlalchemy.py`、`src/thinkback/memory/repositories/in_memory.py`（`list_rounds` 增加 `include_pending`）
- Test: `tests/unit/test_memory_service.py`、`tests/unit/test_repository_contract.py`

**Interfaces:**
- Consumes: `MemoryRepository.mark_rounds_deleted(user_id, memory_scope_id, session_id=None) -> int`、`MemoryRepository.list_rounds(user_id, memory_scope_id, session_id=None, *, limit=None, descending=False) -> list[JournalEntry]`、`MemoryRepository.list_user_rounds(user_id) -> list[JournalEntry]`、`MemoryRepository.excluded_source_refs(user_id, memory_scope_id) -> set[tuple[str | None, str | None]]`
- Produces: `MemoryRepository.list_rounds(user_id, memory_scope_id, session_id=None, *, limit=None, descending=False, include_pending: bool = False) -> list[JournalEntry]`（`include_pending=True` 时同时返回 `round_state == "pending_append"` 的轮次）；`MemoryService._add_session_delete_tombstone(..., source_refs: list[dict[str, str]]) -> datetime` 与 `MemoryService._add_delete_all_tombstone(..., source_refs: list[dict[str, str]]) -> datetime`（新增关键字参数，墓碑行把这些 refs 存进 `ins_memory.source_refs`）

- [x] **Step 1: 写会红的测试**

在 `tests/unit/test_memory_service.py` 新增 `test_delete_session_blocks_inflight_l3_write`：

用一个 backend 替身，`add()` 先 `started.set()` 再 `release.wait(timeout=10)`，返回一条非 P0 事实 `{"id": "inflight-1", "memory": "I meditate daily", "event": "ADD"}`。主线程 append 一轮后等 `started`，然后执行 `DeleteMemoryRequest(scope=DeleteScope.SESSION, session_id="session-1", operation_id="op-inflight")`，再 `release.set()` 并 join。

断言三件事：

```python
assert service.repository.active_memories("user-1", "thinkback") == []
assert service.repository.get_round("r-inflight").round_state == "deleted_tombstone"
assert "inflight-1" not in backend.memories
```

- [x] **Step 2: 运行测试确认失败**

Run: `uv run --group dev pytest tests/unit/test_memory_service.py::test_delete_session_blocks_inflight_l3_write -v`
Expected: FAIL，表现为 `active_memories` 非空（当前 session 墓碑 `source_refs=[]`，排除集里没有该轮次）

- [x] **Step 3: 修墓碑不携带来源**

在 `src/thinkback/memory/service.py`：

0. 先给 `list_rounds` 加 `include_pending: bool = False`（ports 协议 + 两个仓储实现）：SQL 的 `list_rounds` 现在只取 `round_state == "active"`，而 sync 模式下在飞轮次是 `pending_append`，不加这个参数就收集不到，删除屏障对它无效。`include_pending=True` 时条件改为 `round_state IN ("active", "pending_append")`。
1. `_delete_session_memories` 在调用 `mark_rounds_deleted` 之前，先取 `self.repository.list_rounds(request.user_id, session_scope_id, request.session_id, include_pending=True)`，把每个 `JournalEntry` 转成 `{"session_id": entry.session_id, "round_id": entry.round_id}`，作为 `source_refs` 传给 `_add_session_delete_tombstone`。
2. `_delete_all_memories` 同理，用 `self.repository.list_user_rounds(request.user_id)` 收集全部轮次的 refs，传给 `_add_delete_all_tombstone`。
3. 两个 `_add_*_tombstone` 新增 `source_refs: list[dict[str, str]]` 参数，`add_memory_index(..., source_refs=source_refs, ...)`，替换原来的 `source_refs=[]`。

- [x] **Step 4: 修 append 不把轮次改回 active**

在 `MemoryService.append` 的 sync 分支，`self.repository.mark_round_active(request.round_id)` 之前重新读一次轮次：`current = self.repository.get_round(request.round_id)`；只有 `current is not None and current.round_state == "pending_append"` 时才调用 `mark_round_active`。

- [x] **Step 5: 运行测试确认通过**

Run: `uv run --group dev pytest tests/unit/test_memory_service.py::test_delete_session_blocks_inflight_l3_write -v`
Expected: PASS

- [x] **Step 6: 加仓储契约测试**

在 `tests/unit/test_repository_contract.py` 新增 `test_excluded_source_refs_include_tombstoned_rounds`：对两个仓储实现都跑。构造一条 `ins_memory` 墓碑行（`memory_status="DELETED"`，`source_refs=[{"session_id": "s1", "round_id": "r1"}]`），断言 `excluded_source_refs("user-1", "thinkback")` 含有 `("s1", "r1")`。同文件再加 `test_list_rounds_can_include_pending`：写入一条 `round_state="pending_append"` 的轮次，断言 `list_rounds(..., include_pending=True)` 能看到它，默认看不到。

- [ ] **Step 7: 跑相关套件**

Run: `uv run --group dev pytest tests/unit/test_memory_service.py tests/unit/test_repository_contract.py tests/unit/test_memory_repositories.py -q`
Expected: 全部通过

- [x] **Step 8: 提交**

```bash
git add src/thinkback/memory/service.py tests/unit/test_memory_service.py tests/unit/test_repository_contract.py
git commit -m "fix(memory): 删除墓碑携带来源轮次，在飞写入不再复活已删内容"
```

---

### Task 1.5: 读写路径的等待预算解耦（修掉时序敏感用例）

**Files:**
- Modify: `tests/unit/test_memory_api.py`（`test_recall_uses_dedicated_worker_pool_when_write_pool_is_full`）

**Interfaces:**
- Consumes: `thinkback.api.memory._run_memory_call_in_pool` 读取的 `settings.memory_api_worker_wait_seconds`
- Produces: 无新接口。

- [x] **Step 1: 定位 flaky 根因**

读写各有独立的信号量与线程池，但共用 `memory_api_worker_wait_seconds` 这一个旋钮。用例为了逼写路径 503 把旋钮压到 0.001s，recall 的线程交接也被压进 1ms，机器一忙就误报 503。A/B 实测：撤掉本计划全部改动后该用例同样失败，与业务改动无关。

- [x] **Step 2: 只让写预算影响写路径**

用例在 append 之后、recall 之前把 `memory_api_worker_wait_seconds` 恢复为默认值。

- [x] **Step 3: 跑全套件**

Run: `uv run --group dev pytest`
Expected: 846 passed，0 failed（连续两次复跑稳定）

---

### Task 2: L2 摘要版本化，旧刷新不得覆盖新摘要或复活已删内容

**Files:**
- Modify: `src/thinkback/domain/entities.py`（`SummaryEntry` 增加 `row_version: int = 0`）
- Modify: `src/thinkback/domain/ports.py`（协议签名同步）
- Modify: `src/thinkback/memory/repositories/sqlalchemy.py`、`src/thinkback/memory/repositories/in_memory.py`（`upsert_summary_from_rounds` 增加 `expected_row_version` 参数，写入用版本条件）
- Modify: `src/thinkback/memory/l2_refresh.py`（读版本、写版本、在飞期间也累计 append 次数）
- Modify: `alembic/versions/`（新增迁移，给 `ins_current_summary` 加 `row_version` 列）
- Test: `tests/unit/test_l2_llm_summary.py`、`tests/unit/test_repository_contract.py`

**Interfaces:**
- Consumes: `SummaryEntry(user_id, memory_scope_id, summary_text, summary_kind, summary_state, summary_cursor_round, latest_source_round_id, latest_source_timestamp, summary_id, structured_sections, row_version)`
- Produces: `MemoryRepository.upsert_summary_from_rounds(user_id, memory_scope_id, rounds, *, summary_text=None, summary_kind="concat", structured_sections=None, preserve_llm=False, expected_row_version: int | None = None) -> SummaryEntry`；`expected_row_version` 为 None 表示不检查（现有调用方不变），非 None 时版本不匹配则不写入并返回当前行。

- [x] **Step 1: 写会红的测试（删除后旧刷新复活）**

在 `tests/unit/test_l2_llm_summary.py` 新增 `test_stale_refresh_cannot_resurrect_deleted_summary`：composer 用 `Event` 阻塞；append 一轮后等 composer 开始，执行 `DeleteMemoryRequest(scope=DeleteScope.SESSION, session_id="session-1", operation_id="op-del")`，释放 composer，`drain_l3_background_tasks(timeout=10)`。

断言：

```python
summary = repository.get_summary("user-1", "session-1")
assert summary.summary_state is SummaryState.DIRTY
assert "SECRET-MARKER" not in summary.summary_text
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run --group dev pytest tests/unit/test_l2_llm_summary.py::test_stale_refresh_cannot_resurrect_deleted_summary -v`
Expected: FAIL，当前 `summary_state` 为 `active` 且文本含已删内容

- [x] **Step 3: 仓储加版本**

1. `SummaryEntry` 增加 `row_version: int = 0`。
2. SQL 实现：`_upsert_summary` 的 UPDATE 加 `WHERE row_version = :expected` 条件（`expected_row_version is not None` 时），命中 0 行时重新 SELECT 返回当前行；INSERT 与 UPDATE 成功时 `row_version = row_version + 1`。
3. InMemory 实现：`self.summaries[key].row_version` 同样按 `expected_row_version` 条件判断，失败返回现有 `SummaryEntry`。
4. 新增 alembic 迁移：`ins_current_summary` 加 `row_version INTEGER NOT NULL DEFAULT 0`。

- [x] **Step 4: 刷新器带版本写入**

在 `memory/l2_refresh.py` 的 `_run_refresh`：读 `summary = self._repository.get_summary(user_id, session_scope_id)`，把 `expected_row_version = summary.row_version if summary else None` 传给 `upsert_summary_from_rounds`。写后比对返回的 `summary_text` 与自己写入的文本：不相等说明版本冲突，本次没写成功，不要把该 scope 记入 `_llm_refreshed_scopes`（否则下次刷新会把旧摘要当上一版喂给模型）。

- [x] **Step 5: 写会红的测试（在飞期间的新轮次不丢）**

在 `tests/unit/test_l2_llm_summary.py` 新增 `test_refresh_in_flight_triggers_trailing_refresh`：`refresh_interval_rounds=5`；第一次 composer 阻塞时再 append 4 轮（共 5 轮累计），释放后 `drain`，断言 `len(composer.calls) >= 2` 且最后一次调用的轮次数包含新轮次。

- [x] **Step 6: 修在飞计数**

在 `L2BackgroundRefresher.maybe_submit`：pending 时也把 `self._append_counters[scope]` 加一并 return；`_finish` 里若 `self._append_counters.get(scope, 0) >= self.refresh_interval_rounds`，重新调用 `maybe_submit(user_id, session_scope_id)` 触发一次 trailing 刷新。

- [x] **Step 7: 加契约测试**

在 `tests/unit/test_repository_contract.py` 新增 `test_upsert_summary_rejects_stale_row_version`：两个实现都跑，先 upsert 一次拿到 `row_version`，再用旧的 `expected_row_version` 写一次，断言文本没变、`row_version` 递增。

- [x] **Step 8: 跑相关套件**

Run: `uv run --group dev pytest tests/unit/test_l2_llm_summary.py tests/unit/test_repository_contract.py tests/unit/test_memory_repositories.py -q`
Expected: 全部通过

- [x] **Step 9: 提交**

```bash
git add src/thinkback/domain/entities.py src/thinkback/domain/ports.py src/thinkback/infra/database/models.py src/thinkback/memory/repositories src/thinkback/memory/l2_refresh.py alembic/versions tests/unit/test_l2_llm_summary.py tests/unit/test_repository_contract.py
git commit -m "fix(memory): 摘要写入加版本条件，旧刷新不能覆盖新摘要或复活已删内容"
```

---

### Task 3: update 失败不再谎报 completed

**Files:**
- Modify: `src/thinkback/memory/service.py`（`_update_memory_locked`、`update_memory`）
- Modify: `src/thinkback/api/errors.py`（`TaskLockExhaustedError` → 409 映射）
- Test: `tests/unit/test_memory_service.py`

**Interfaces:**
- Consumes: 现有 `UpdateMemoryResponse(status, task_id, memory, summary_state)`、`TaskStatus`
- Produces: `update_memory` 在后端写失败时返回 `status="failed"`；任务状态为 `TaskStatus.FAILED` 且 `last_error` 非空。

- [ ] **Step 1: 写会红的测试**

在 `tests/unit/test_memory_service.py` 新增 `test_update_backend_failure_does_not_report_completed`：用 `FakeMemoryBackend` 的子类让 `update()` 抛 `RuntimeError("simulated backend update outage")`；先 append 一轮造出记忆，拿到 `memory_id`，再调 `update_memory`。

断言：

```python
assert response.status == "failed"
assert service.repository.get_task(f"memory-update:{operation_id}").status is TaskStatus.FAILED
```

同时断言本地索引文本仍是新值（本地已写，允许），后端 `backend.memories[backend_id]["memory"]` 仍是旧值。

- [ ] **Step 2: 运行确认失败**

Run: `uv run --group dev pytest tests/unit/test_memory_service.py::test_update_backend_failure_does_not_report_completed -v`
Expected: FAIL，当前 `response.status == "completed"`

- [ ] **Step 3: 实现**

在 `MemoryService.update_memory`：`_update_memory_locked` 返回的 pending 回调改为在锁外逐个执行并收集异常；任一异常时用 `_mutate_task_with_retry` 把任务标 `FAILED` 并写 `last_error`，响应 `status="failed"`、`task_id` 保持不变。`finalize_update` 的 `_mutate_task_with_retry` 改用 `strict=True`；`TaskLockExhaustedError` 目前没有 HTTP 映射，在 `src/thinkback/api/errors.py` 补成 409（可重试冲突），不返回 completed。

- [ ] **Step 4: 修正既有测试的期望**

`tests/unit/test_memory_service.py` 中断言“后端更新失败仍 completed”的用例改为断言 `status == "failed"`；同时保留“本地索引不被破坏”的断言。

- [ ] **Step 5: 跑相关套件**

Run: `uv run --group dev pytest tests/unit/test_memory_service.py -q`
Expected: 全部通过

- [ ] **Step 6: 提交**

```bash
git add src/thinkback/memory/service.py tests/unit/test_memory_service.py
git commit -m "fix(memory): 后端更新失败时 update 返回 failed，不再谎报完成"
```

---

### Task 4: 幂等键区分端点，指纹不再结构化碰撞

**Files:**
- Modify: `src/thinkback/api/idempotency.py`（`IdempotencyCache` 的键与过期清理）
- Modify: `src/thinkback/domain/keys.py`（`fingerprint`、`request_fingerprint`）
- Test: `tests/unit/test_v1_integration.py`、`tests/unit/test_domain_pure_functions.py`

**Interfaces:**
- Consumes: `IdempotencyCache.get(scope, idem_key, body_hash)` / `.get_conflict(scope, idem_key, body_hash)` / `.put(scope, idem_key, body_hash, response)`
- Produces: `IdempotencyCache.get(scope, idem_key, method, path, body_hash)` / `.get_conflict(scope, idem_key, method, path, body_hash)` / `.put(scope, idem_key, method, path, body_hash, response)`，键为 `(scope, idem_key, method, path, body_hash)`。

- [ ] **Step 1: 写会红的测试（跨端点串答）**

在 `tests/unit/test_v1_integration.py` 新增 `test_idempotency_key_does_not_cross_endpoints`：同一把 key 先 `POST /v1/memory/append`，再 `POST /v1/memory/recall`，断言 recall 的响应体是 recall 的结构（`items` 存在），不是 append 的响应。

- [ ] **Step 2: 运行确认失败**

Run: `uv run --group dev pytest tests/unit/test_v1_integration.py::test_idempotency_key_does_not_cross_endpoints -v`
Expected: FAIL，recall 直接返回 append 的缓存响应

- [ ] **Step 3: 缓存键加 method 与 path**

`IdempotencyCache` 的三个公开方法都加 `method`、`path` 参数，`_make_key` 返回 `(scope, idem_key, method, path, body_hash)`；二级索引改为 `(scope, idem_key, method, path) -> set[body_hash]`。`IdempotencyMiddleware.dispatch` 传 `request.method` 与 `request.url.path`。

- [ ] **Step 4: 过期条目不再造成永久冲突**

`get_conflict` 先调用内部 `_prune_expired()`，把过期条目从 `_store` 与 `_index` 一并清掉；`get` 与 `put` 的淘汰路径同步维护 `_index`。

- [ ] **Step 5: 写会红的测试（指纹碰撞）**

在 `tests/unit/test_domain_pure_functions.py` 新增 `test_request_fingerprint_distinguishes_embedded_separators`：两轮消息分别是 `[("user","a|assistant:b"),("assistant","c")]` 与 `[("user","a"),("assistant","b|assistant:c")]`，断言 `fingerprint` 两者不相等。

- [ ] **Step 6: 指纹改 canonical JSON**

`domain/keys.py`：

```python
def fingerprint(messages: list[dict[str, Any]]) -> str:
    payload = json.dumps(
        [{"role": m["role"], "content": m["content"]} for m in messages],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
```

`request_fingerprint(request)` 在消息之外加入影响行为的 metadata 键：`("force_extract", "memory_type", "data_classification", "source_type", "backend_categories")`，以同样方式序列化。旧指纹与新算法不兼容，服务未上线，在 CHANGELOG 记一条。

- [ ] **Step 7: 跑相关套件**

Run: `uv run --group dev pytest tests/unit/test_v1_integration.py tests/unit/test_domain_pure_functions.py tests/unit/test_memory_service.py -q`
Expected: 全部通过

- [ ] **Step 8: 提交**

```bash
git add src/thinkback/api/idempotency.py src/thinkback/domain/keys.py tests/unit/test_v1_integration.py tests/unit/test_domain_pure_functions.py
git commit -m "fix(api): 幂等键区分端点与路径，指纹改用 canonical JSON"
```

---

### Task 5: assistant 断言不再成为长期事实

**Files:**
- Modify: `src/thinkback/memory/service.py`（`_add_l3_from_messages` 的消息筛选）
- Test: `tests/unit/test_memory_service.py`

**Interfaces:**
- Consumes: `MemoryMessage.role`（`MessageRole.USER / ASSISTANT / SYSTEM`）
- Produces: `_add_l3_from_messages` 只把 `role == user` 的消息传给 `backend.add` 并只用于 `source_text`；assistant 内容仍完整留在 L1。

- [ ] **Step 1: 写会红的测试**

在 `tests/unit/test_memory_service.py` 新增 `test_assistant_assertion_does_not_become_user_fact`：用一个 backend 替身，`add()` 固定返回 `{"id": "m-1", "memory": "User lives in Shanghai", "event": "ADD"}`；用户消息是 `Where should I live?`，assistant 消息是 `You live in Shanghai.`。append 后断言 `service.repository.active_memories("user-1", "thinkback")` 为空，且 backend 收到的 messages 里只有 user 那一条。

- [ ] **Step 2: 运行确认失败**

Run: `uv run --group dev pytest tests/unit/test_memory_service.py::test_assistant_assertion_does_not_become_user_fact -v`
Expected: FAIL，当前会把 assistant 内容混进 `source_text` 并索引出 `User lives in Shanghai`

- [ ] **Step 3: 实现**

在 `_add_l3_from_messages`：

```python
trusted_messages = [m for m in messages if m.get("role") == MessageRole.USER.value]
if not trusted_messages:
    return []
```

`source_text` 只用 `trusted_messages` 拼接；`backend.add(trusted_messages, ...)`。

- [ ] **Step 4: 跑相关套件**

Run: `uv run --group dev pytest tests/unit/test_memory_service.py tests/unit/test_write_pre_filter.py -q`
Expected: 全部通过

- [ ] **Step 5: 提交**

```bash
git add src/thinkback/memory/service.py tests/unit/test_memory_service.py
git commit -m "fix(memory): 只有用户消息能成为长期事实来源"
```

---

### Task 6: 敏感分类在落库与外发前生效

**Files:**
- Create: `src/thinkback/domain/classification.py`
- Modify: `src/thinkback/memory/service.py`（`append` 在写 journal 之前判定）
- Test: `tests/unit/test_memory_service.py`、`tests/unit/test_domain_pure_functions.py`

**Interfaces:**
- Consumes: `DataClassification`（`domain/enums.py`）
- Produces: `resolve_classification(metadata: dict[str, Any] | None) -> str`（返回 `DataClassification` 的值，缺省 `normal`）；`blocks_storage(classification: str) -> bool`（`sensitive` / `restricted` 为 True）；`assert_storable(classification: str, messages: list[dict[str, str]]) -> None`（不满足时抛 `ValueError`，消息含 `classification` 与 `sensitive_or_restricted`）。

- [ ] **Step 1: 写会红的测试**

在 `tests/unit/test_memory_service.py` 新增 `test_sensitive_classification_is_rejected_before_persistence`：append 一轮，`metadata={"data_classification": "sensitive"}`，内容是普通文本。断言抛 `ValueError`（消息含 `sensitive_or_restricted`），并且 `repository.get_round(round_id) is None`、`backend.memories == {}`。

- [ ] **Step 2: 运行确认失败**

Run: `uv run --group dev pytest tests/unit/test_memory_service.py::test_sensitive_classification_is_rejected_before_persistence -v`
Expected: FAIL，当前照常写 journal 并调用 backend

- [ ] **Step 3: 实现**

新建 `src/thinkback/domain/classification.py`，按 Interfaces 写三个纯函数。`MemoryService.append` 在 `self.repository.save_round(request)` 之前调用 `assert_storable(resolve_classification(request.metadata), [...])`，异常向上抛；API 层现有映射会把 `ValueError` 转成 400，消息里带 `sensitive_or_restricted` 与实际分类。

- [ ] **Step 4: 补纯函数测试**

在 `tests/unit/test_domain_pure_functions.py` 新增 `test_resolve_classification_defaults_and_blocks`：缺省返回 `normal`；`sensitive` / `restricted` 的 `blocks_storage` 为 True；`personal` / `normal` 为 False。

- [ ] **Step 5: 跑相关套件**

Run: `uv run --group dev pytest tests/unit/test_memory_service.py tests/unit/test_domain_pure_functions.py -q`
Expected: 全部通过

- [ ] **Step 6: 提交**

```bash
git add src/thinkback/domain/classification.py src/thinkback/memory/service.py tests/unit/test_memory_service.py tests/unit/test_domain_pure_functions.py
git commit -m "feat(memory): 敏感与受限分类在落库与外发前被拦截"
```

---

## 完成定义

- `make check` 全绿（format-check + lint + typecheck + mutation-audit + eval）。
- `make test` 全绿。
- Review Focus 的五类输入各有一条会红的测试在先、绿在后。
- 审计报告 `.planning/2026-10-26-thinkback-memory-audit/findings.md` 中 N4、N5、N6、N7、N8、R 标注为已修。
