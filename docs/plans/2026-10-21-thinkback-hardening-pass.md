# Thinkback 记忆服务 — 审查后加固批次（2026-10-21）

**Spec**: 本文件。来源：2026-10-21 的架构与缺陷审查（`/tmp/architecture-review-20261021-173659.html`）。
**关联计划**: `docs/plans/2026-10-19-thinkback-memory-architecture-rewrite.md`（本文覆盖其 Phase 0 的
Task 0.2 / 0.3 / 0.4 / 0.5 / 0.8 / 0.9 / 0.10 / 0.11 / 0.12，并额外补 8 条审查新增缺陷）。

**Goal**: 把审查发现的 5 条 P0、4 条 P1、8 条 P2 与 4 个 deepening 候选全部落地，
让 `make check` 与 `make test` 全绿，并让「interface 承诺 = implementation 承诺」这条不变量成立。

**Global Constraints**

- 行为变更随附测试：**先写会红的测试**，再改实现。新 API 端点至少覆盖 happy path + 一条错误路径。
- 仓储双实现同行为：改 `SqlAlchemyMemoryRepository` 或 `InMemoryMemoryRepository` 后必须跑
  `tests/unit/test_repository_contract.py`；契约红了就是行为漂移。
- 测试确定性：不引入网络、不连真实 LLM / Milvus / Mem0。
- 完成定义：`make check` 全绿（format-check + lint + typecheck + mutation-audit + eval）+ `make test` 全绿。
- **文件所有权互斥**：同一批次内的任务不得改同一文件（见各任务 Files）。跨批次按顺序执行。
- 每个任务单独提交，commit message 描述**改了什么行为**，不描述文件清单。

---

## T0 — 收尾 Task 0.5：落库文本保留原文（进行中，WIP 违反既有裁定）

**Files**: `src/thinkback/domain/slots/canonical.py` · `src/thinkback/domain/slots/__init__.py` ·
`src/thinkback/memory/service.py` · `tests/unit/test_memory_service.py`

**背景**: 工作区里有一份未完成的 WIP：新增了 `local_index_memory_text` / `count_p0_facts`，
并删掉了三条 `_BackendUpdate` 回写。它让 `service.py:3642` 调用一个当时还不存在的函数 →
NameError，eval 从 100% 掉到 5%。函数已补上，测试已绿。

**但 WIP 违反 ledger 里已写死的裁定**（`.superpowers/sdd/2026-10-19-.../progress.md`）：

> Task 0.5: Ruling: 「多事实记忆在本地索引保留原文」用「canonical 折叠掉事实」判定，
> 而不是「命中多个 P0 槽位」判定 —— 后者抓不到计划里「雇主+年份+团队」那条样例
> （它只命中 location 一个槽位，但 canonical 照样丢掉了 52 个字符的雇主/年份/团队）。
> 实现：canonical 比原文短超过 12 个字符即判定为丢失事实，落库改用原文。

当前 WIP 用 `count_p0_facts(original) > 1` 判定 → 上面那条样例会判成「单事实」→ 落 canonical →
**照样丢事实**，即 Task 0.5 的核心验收项不成立。

**裁定（本批次）**: 保留并组合两条判据，取更保守者 ——
`count_p0_facts(original) > 1` **或** `len(original) - len(canonical) > 12` 即判定丢失事实、落库用原文。
长度判据满足 ledger 原裁定（那条样例 diff=54 > 12 通过），槽位数判据只会在原裁定之外
多保留原文、不会少保留，因此不违反裁定，只会更安全。代价：单槽位改写超过 12 字符时
本地索引存原文而非模板形式（不丢信息，ledger 已接受该代价）。

- [ ] `local_index_memory_text` 同时用两条判据
- [ ] 补回归测试：计划里「雇主+年份+团队」那条样例落库保留原文
- [ ] 补回归测试：单槽位记忆落 canonical 形式
- [ ] 后端不被回写（已有测试保持绿）
- [ ] `make check` + `make test` 全绿

---

## T1 — 鉴权与租户（批次 A，与 T2 并行）

**Files**: `src/thinkback/api/auth.py` · `src/thinkback/api/admin.py` ·
`src/thinkback/api/integration_admin.py` · `src/thinkback/api/idempotency.py` ·
`src/thinkback/api/ratelimit.py` · `src/thinkback/api/v1_memory.py` ·
`src/thinkback/api/dependencies.py` · `src/thinkback/infra/config.py` ·
`.env.example` · `k8s/configmap.yaml` ·
`tests/unit/test_v1_integration.py` · `tests/unit/test_integration_admin.py` · `tests/unit/test_admin_api.py`

### T1.1 API Key secret 真校验（审查 B2）

`api/auth.py:149` 的 `_verify_secret` 两侧是同一个表达式 → 恒 True。
根因不是笔误：`ApiKey` 没有 secret 字段，明文只在签发时返回一次、从不落库，校验在结构上不可能实现。

- `ApiKey` 增加 `secret_hash: str`（存 `sha256(f"{key_id}:{secret}")`，不存明文）
- `generate_api_key`：`key_id` 从 `token_hex(4)`（4 字节，可枚举）加长到 ≥128 bit；
  `secret` 加长到 ≥256 bit；把 `secret_hash` 写进记录
- `_verify_secret`：`secrets.compare_digest(record.secret_hash, sha256(f"{key_id}:{secret}"))`
- `revoke_api_key` 复制记录时保留 `secret_hash`（当前会丢字段）
- **日志不再打印 key_id**（`generate_api_key` / `revoke_api_key` 两条 INFO 日志）
- `register_api_key` 的调用方同步更新（它现在必须能带 secret_hash）

**必须新增的回归测试**（现在「无效 key」用例在 key_id 不存在那一步就 401 了，根本没走到校验）：
合法 key_id + 错误 secret → 401 · 空 secret → 401 · 已吊销 key → 401 ·
跨租户 header → 403 · key_id 长度 ≥ 32 hex · 日志不含 key_id。

### T1.2 管理端点鉴权 + 生产 fail-fast（审查 B4 / B5）

- `api/integration_admin.py` 三个路由（list / create / revoke）**没有任何** `Depends(require_admin_token)`
  → router 加 `dependencies=[Depends(require_admin_token)]`
  → `tests/unit/test_integration_admin.py:19` 断言未认证返回 200，**这条测试要把不安全行为锁进测试了**，
  一并改成断言 401
- `api/admin.py:47` `require_admin_token`：token 为空时直接 `return`（fail-open）→ 改为 fail-closed；
  token 比较用 `secrets.compare_digest`（现在是明文 `!=`）
- `admin_health_detail()`（admin.py:390）没挂鉴权，会泄露 alembic 版本、连接池、gRPC 地址、功能开关 → 补鉴权
- `infra/config.py:149` `admin_token` 默认空串；`.env.example` 与 `k8s/configmap.yaml` 都没有这一项
  → `ENVIRONMENT=production` 且缺 `ADMIN_TOKEN` 时**启动失败**（不是运行时才失败）
- 限流 `api/ratelimit.py:63` `_get_bucket` 只校验 key 存在、**不校验 `is_active`**（已吊销的 key 仍占桶）
  → 补 `is_active` 校验；`_get_bucket` 在 `bucket.burst != burst` 时会**重置桶**（tokens=burst）→ 不得重置已有桶

### T1.3 租户贯穿（审查 B3 / 候选 2）

现状：`api/v1_memory.py:10` 的模块 docstring 写着「强制 Tenant ID 头(X-Tenant-Id)」，
实现是 `if x_tenant_id and x_tenant_id != record.tenant_id` —— 客户端不传就不校验；
且 `grep -rn tenant src/thinkback/memory/ src/thinkback/domain/` **返回空**：tenant 从未进入存储层。
所有查询以请求体里的 `user_id` 为准 → 跨租户读写已实测复现。
幂等缓存键（`idempotency.py:42`）也不含 tenant → 跨租户拿别人缓存响应已实测复现。

**裁定**: 用 **user_id 命名空间** 把 tenant 带进存储层，不做 schema 迁移、不动 44 方法的仓储协议 ——
服务从未上线，没有存量数据迁移负担。

- 命名空间规则：`scoped_user_id(tenant_id, user_id) -> f"{tenant_id}:{user_id}"`，
  放在 `domain` 里（纯函数，可测），供 api 层调用
- `/v1/*` 的每一个端点（append / recall / list / get / update / delete / tasks / l3-status）
  都必须把请求里的 user_id 命名空间化后才能进 service 层；**漏一个就是跨租户漏洞**
- 内部 `/memory/*` 与 `/admin/api/*` 路由（trusted admin，无 API Key）**不命名空间**，
  直接用原始 user_id → 治理台仍能按 user_id 看到全部数据（v1 来源的数据会以 `tenant:user` 形式出现，
  这是预期行为，文档里写明）
- `X-Tenant-Id` 头变**强制**：缺失 → 403 TB-1003；与 key 的 tenant 不符 → 403 TB-1003
- 幂等缓存键必须含 tenant → 跨租户不可达
- gRPC 入口（`rpc/`）没有 API Key 概念 → 视为 trusted admin 命名空间（与内部路由一致）

**必须新增的回归测试**: tenant_A 的 key 读 tenant_B 的 user_id → 看不到任何数据（不是 403，是空）；
同租户正常读写；缺 `X-Tenant-Id` → 403；header 与 key 不符 → 403；
跨租户用同一个 `Idempotency-Key` + 同一个 body → 不得命中对方缓存。

---

## T2 — 仓储原子性与缓存（批次 A，与 T1 并行）

**Files**: `src/thinkback/domain/ports.py` · `src/thinkback/memory/repositories/sqlalchemy.py` ·
`src/thinkback/memory/repositories/in_memory.py` · `src/thinkback/memory/repositories/_l1_cache.py` ·
`src/thinkback/memory/l2_refresh.py` · `src/thinkback/infra/database/engine.py` ·
`tests/unit/test_repository_contract.py` · `tests/unit/test_memory_repositories.py` ·
`tests/unit/test_l2_llm_summary.py`

### T2.1 L2 摘要原子写（审查 B6，已实测复现）

`repositories/sqlalchemy.py:329-381` `_upsert_summary` 先 SELECT 判 `summary_kind == "llm"`
（preserve_llm 保护），再写文本 —— 两步之间没有串行化。后台 LLM 刷新（`l2_refresh.py:148`）
与每次 append 的同步拼接写（`service.py:4111`）天然重叠 → 复现结果
`FINAL kind = llm | text = 'concat-fallback'`：行上仍标 llm，内容却是拼接兜底，
且 `_previous_llm_summary` 会把它当「上一版 LLM 摘要」喂进下一轮刷新 → 污染自我强化。

- 改成**单条原子语句**：`UPDATE ... SET summary_text=..., summary_kind=... WHERE user_id=? AND
  memory_scope_id=? AND (summary_kind <> 'llm' OR :new_kind = 'llm')`，
  0 行受影响时再走 INSERT 分支
- in-memory 实现必须**同行为**（契约测试会抓）
- 接口要能表达「这条写不许覆盖 LLM 摘要」这个条件 —— 不要把并发语义藏在 implementation 里
  （这是审查里 Top recommendation 的核心：**原子性属于 interface**）
- 回归测试：两个写入者交错（LLM 刷新 vs preserve_llm 拼接），最终 `summary_kind` 与
  `summary_text` 必须一致，不能出现 kind=llm 而内容是拼接文本

### T2.2 并发插入不 500（审查 B8，已实测复现）

- `save_task` 插入路径（`sqlalchemy.py:1006-1064`）没有 catch `IntegrityError`；
  同文件的 `_claim_task` 与 `_save_round` 都有 —— 同文件内不一致
- `ins_current_summary` 首次插入（`sqlalchemy.py:340-380`）同样会撞 UNIQUE
- 统一用 `INSERT ... ON CONFLICT DO NOTHING RETURNING`（PostgreSQL）/ 等价语义（SQLite 契约测试用的）
- 回归测试：两个仓储实例共享一个 DB，同时插入同一 task_id → 不得抛 IntegrityError，
  且两个实现行为一致

### T2.3 L1 缓存跨副本失效（审查 B7，已实测复现）

`sqlalchemy.py:198-219` 的 DB 回落只在缓存**为空**时触发；缓存一旦热了就再也不读库，
而 `clear_l1` 只在 delete/rebuild 路径调用。`k8s/deployment-api.yaml:9` 是 `replicas: 2` →
同一用户换个 Pod 就换一份记忆。

**裁定**: 回到**单副本**（`replicas: 1`），与 `docs/ARCHITECTURE.md` 的「单进程」表述对齐；
同时给 L1 缓存加**最长存活时间**，即使将来扩容也不会无限期返回陈旧数据。
不引入 Redis（计划 Global Constraints 明写「单进程单副本起步，不引入 Redis / Celery」）。

- `k8s/deployment-api.yaml` → `replicas: 1`，并注释说明原因
- L1 缓存条目加 `expires_at`，读路径过期即回落 DB
- 回归测试：两个仓储实例共享一个 DB，podA 缓存热 → podB 追加 → podA 在 TTL 内能读到新数据

### T2.4 索引与读取路径（审查候选 4 的一部分）

- `ins_memory_task` 缺 `updated_at` 索引（两处回收路径按它过滤、`list_tasks` 按它排序）；
  `ins_memory` 缺 `(user_id, memory_scope_id, created_at)` 索引 → 补 alembic migration
- `_excluded_source_refs`（`sqlalchemy.py:924-949`）`select(MemoryRecord)` 拉全部 18 列，
  而召回热路径只需要 `source_refs` → 只选需要的列
- `list_rounds` 加 `limit` / `descending` 参数（SQL 实现已支持，**协议没有**）→
  加进 `MemoryRepository` 协议，两个实现同步（in-memory 也要支持）
- `mark_rounds_deleted`（`sqlalchemy.py:505-522`）先全量 select 再逐行改 → 单条 UPDATE

---

## T3 — 记忆核心状态机（批次 B，T1/T2 之后）

**Files**: `src/thinkback/memory/service.py` · `src/thinkback/memory/task_state.py`（新建）·
`src/thinkback/domain/safety.py` · `src/thinkback/domain/recall_policy.py` ·
`tests/unit/test_memory_service.py` · `tests/unit/test_l2_llm_summary.py`

### T3.1 L3 槽位泄漏（审查 B1，已实测复现）

`service.py:454-470`：`append()` 在 `try` 块**之外**预留 L3 队列槽位，而
`get_task` / `save_task` / `_check_retry_budget` 也在 try 之外 —— 这些调用一旦抛异常，
`release_write_slot()` 永不执行，槽位只进不出。生产 `l3_max_pending_tasks=256`：
一次 30 秒 DB 抖动就能让该 Pod 的写入能力**永久归零**，直到重启。

- 把 `reserve_write_slot()` 之后的全部代码纳入 `try/finally`，保证释放
- 回归测试：连续 4 次 append 抛异常（`l3_max_pending_tasks=4`）→ 槽位必须全部归还，
  DB 恢复后 append 必须成功

### T3.2 DEAD_LETTER 是终态（审查 P2，已实测）

`service.py:472` 等 **5 处**同款：`was_retrying_failed_task = previous_task is not None and
previous_task.status is TaskStatus.FAILED` —— 只认 FAILED，`DEAD_LETTER` 被无条件覆写成 `RUNNING`
→ retry_count 不自增、预算检查不执行。实测连打 19 次 retry_count 涨到 11（MAX_RETRIES=5）。

- 5 处统一改为只把 FAILED 当作可重试；`DEAD_LETTER` 是终态，直接短路
- **把这条不变量抽到一个独立 module**（`memory/task_state.py`）：5 处散落的判定收敛成一处，
  这正是审查里「候选 3：拆 service.py」的核心收益 —— locality
- 回归测试：连打 20 次 → 稳定停在 dead_letter，retry_count 不超过 MAX_RETRIES

### T3.3 CAS 耗尽不谎报成功（审查 P2）

`service.py:811-844`：三次 CAS 全冲突时只 `logger.warning` + `return None`，
而 append 的终态 COMPLETED 写入走的正是这条路径（`service.py:604`），
**调用方丢弃返回值**照常返回 completed → task 行留在 RUNNING、result 为空，
之后每次重试都命中 `already_done`，不一致被永久固化。

- CAS 耗尽时抛出可识别的异常（不要用 RuntimeError 字符串协议）→ append 返回 5xx 而不是假成功
- 回归测试：CAS 连续冲突 → append 不返回 completed

### T3.4 write-gate 不销毁 L1（审查 P2 / 计划 Task 0.8）

`service.py:500-504` + `domain/safety.py`：命中词表 → **整轮 `mark_round_deleted`**
（连 L1 原文一起销毁）→ 返回 `status="completed"`。实测误杀率极高
（`set api_key:` / `the tool:` / `System:` / `token:` 都会命中），
而对抗性注入只需加两个空格（`ignore␣␣all␣␣previous␣␣instructions`）就全部放行。
**两件事一件没做到，只制造静默数据丢失。**

- 命中词表 → 返回 `skipped` + 明确 reason，**保留 L1 原文**
- 删除是独立、需显式授权的操作，不在写入路径上自动发生
- 删掉 `role in {system, tool}` 的判定（把工具遥测与 prompt injection 混为一谈）
- 词表降级为「写入前的可选过滤开关」，默认关
- 回归测试：命中词表的轮次 → L1 原文仍在、状态是 skipped 而不是 completed

### T3.5 召回计数与预算裁剪的顺序（审查 P2 / 计划 Task 0.11）

`service.py:1340`：`_touch_recalled_entries` 在 `clipped_items = self._dedupe_and_clip(...)`
**之前**执行 → 被预算裁掉、根本没返回给模型的记忆，一样 `recall_count += 1`
（曝光偏差闭环：召回 → 计数涨 → strength 涨 → 排名提前 → 更容易召回）。
`domain/recall_policy.py:93` 的排序键 (-层优先级, -strength) 完全不含 score。

- touch 移到裁剪之后，只对真正返回的记忆计数
- score 进入排序键（作为主项或与 strength 组合，理由写清楚）
- 回归测试：被预算裁掉的记忆 recall_count 不变

---

## T4 — 运维面、可观测性与 gRPC（批次 B，与 T3 并行）

**Files**: `src/thinkback/api/app.py` · `src/thinkback/api/metrics.py` ·
`src/thinkback/api/errors.py` · `src/thinkback/api/errors_standard.py` ·
`src/thinkback/rpc/server.py` · `src/thinkback/rpc/converters.py` · `src/thinkback/rpc/servicer.py` ·
`src/thinkback/memory/backends/mem0_library.py` · `src/thinkback/domain/recall_policy.py`（token 估算）·
`k8s/deployment-api.yaml`（replicas 由 T2 改，这里不再动）· `k8s/job-migrate.yaml` ·
`tests/unit/test_metrics_prometheus.py` · `tests/unit/test_rpc_auth.py`（新建）·
`tests/unit/test_ops_boundaries.py`（新建）

### T4.1 X-Trace-Id 在 /v1 写请求上丢失

`api/idempotency.py` 在 2xx 分支把响应体读出来、**重建**了一个新的 `Response`，丢掉全部响应头
→ trace 中间件设的 `X-Trace-Id` 被外层幂等中间件抹掉，恰好在需要排查的写链路上失去关联。
修法：重建 Response 时透传 `response.headers`。

### T4.2 Prometheus counter 非单调

`api/metrics.py:71-100`：`thinkback_request_total` 声明 TYPE=counter、HELP 写「since process start」，
但值来自 300 秒滑动窗口，每 5 分钟归零 → Prometheus 的 `rate()`/`increase()` 会把它当成进程重启，
错误率与 SLO 计算全错。修法：真 counter（只增不减）+ 独立 gauge 做 5 分钟速率；或改 TYPE=gauge 并改名。

### T4.3 三个进程并发跑 alembic

`api/app.py:72` lifespan 每次启动都跑 `run_migrations_async()`，`k8s/job-migrate.yaml` 还有一个迁移 Job，
`replicas: 2` → 最多 3 个进程并发 DDL → 撞「already exists」或死锁。
修法：lifespan 只做 `alembic current` 校验（schema 不匹配则启动失败并提示跑迁移 Job），
迁移本身交给 Job；或 lifespan 里用 PG advisory lock 串行化。

### T4.4 gRPC 无鉴权 + 与 HTTP 语义漂移

- `rpc/server.py` 用 `add_insecure_port`，无 interceptor 认证 → 加 API Key 鉴权 interceptor
  （与 HTTP 同口径：Authorization + tenant）
- `rpc/converters.py:143`：`l3_score_threshold` HTTP 默认 0.5、gRPC 默认 **0.0**（proto3 无 presence，
  默认 0 = 不过滤）→ 两边对齐；`l3_limit` HTTP ≤50、gRPC `req.l3_limit or 5` → 显式传 0 也变 5 且无上限 → 对齐

### T4.5 health_check 写数据 + token 估算

- `memory/backends/mem0_library.py` 的 `health_check` 真的调 `add` 写一条探针记忆再 `delete`。
  k8s 探针默认 10s 一次 = 一次 LLM + 一次 embedding + 一次 Milvus 写 + 一次删 → 改用只读查询
- `domain/recall_policy.py:102` `cost = max(1, len(normalized) // 2)`：注释写「中文一字约等于一个 token」，
  代码对中文**低估约 2 倍**、对英文**高估约 2.4 倍** → 按字符集分档估算
- 同处 `cost` 超预算时用的是 `continue` 不是 `break` → 改 `break`

---

## T5 — 死代码与死链（批次 C）

**Files**: `src/thinkback/api/mem0_config_admin.py` · `src/thinkback/memory/mem0_config.py` ·
`src/thinkback/memory/backends/mem0_library.py` · `src/thinkback/memory/schemas.py` ·
`src/thinkback/domain/slots/canonical.py` · `src/thinkback/memory/service.py` ·
`src/thinkback/api/app.py` · `alembic/versions/`（新增删表迁移）· `web/src/`（治理台调用方）·
`docs/ARCHITECTURE.md` · `tests/unit/`（对应测试）

### T5.1 mem0_config 整条链是死的（计划 Task 0.6）

`memory/backends/mem0_library.py:45` 的 `_build_custom_instructions()` 不接任何参数，
直接 `return THINKBACK_CUSTOM_INSTRUCTIONS` 常量 —— **治理台改的抽取提示词从不生效**。
删除 `memory/mem0_config.py`（266 行）、`api/mem0_config_admin.py`（128 行）、`ins_mem0_config` 表
（alembic migration）、app.py 的 router 注册、以及治理台（`web/src/components/mem0-config.tsx`
与 `web/src/pages/` 里相关调用方）。

### T5.2 死代码（计划 Task 0.7）

- `RecallIntent.SENSITIVE` —— 枚举只有两个值，SENSITIVE 在 `service.py:1237` 无条件 raise ValueError，
  连本地安全的 L1/L2 都不返回（fail-useless 不是 fail-closed）→ 删掉整个 intent 轴
- `conflict_partition` / `conflict_partition_from_metadata`（`canonical.py:23,27`）都 `return ()`，
  被 4 层包装转发 → 删
- `service.py` 里 14 个 `_extract_*` 纯转发方法 → 删
- `docs/ARCHITECTURE.md` 引用了不存在的 `memory/caches.py`（BoundedTTLCache）→ 删文档段落

---

## T6 — 收尾与复盘（批次 C 之后）

- `make check` 全绿 + `make test` 全绿 + `make coverage` 记录观测水位
- 回填 `docs/plans/2026-10-19-thinkback-memory-architecture-rewrite.md` 的 Phase 0 勾选框
- 回填本文件各任务的勾选框
- 复盘：写进 `.superpowers/sdd/2026-10-21-thinkback-hardening-pass/progress.md`
- 若产生新的领域词汇或不可逆决策 → 写 `CONTEXT.md` / `docs/adr/`

