# Thinkback 对抗式 Bug 猎捕 — 2026-10-29

- **目标**：在 HEAD `71d8f1a`（含工作区未提交改动）上找**仍然存在**的真实缺陷。
- **方法**：逐条读代码路径 → 用仓库自带 fake（`InMemoryMemoryRepository` / `FakeMemoryBackend` / 自制 mem0 2.2.1 形状 client）写最小确定性复现 → 跑 → 记录。
- **纪律**：只报能复现或能精确到行号+交错的缺陷；风格/命名/重构建议一律不报；每条都对照现有测试查重。
- **复现脚本**：全部在 `/tmp/tbhunt/`，未改动仓库任何文件。运行方式：
  `cd /tmp/tbhunt && uv run --project /Users/zhangpeng/workspace/liaohe/Thinkback --group dev python <script>.py`

## 严重度汇总

| 编号 | 严重度 | 一句话 |
|---|---|---|
| BUG-1 | high | `erase(scope=ALL)` 把「本地无索引行」的引擎记录原文留在 mem0 history.db，审计却写 `mem0_history_purged: complete` |
| BUG-2 | high | 删/抑制**单条**记忆会把**整轮**原始对话从 L1 召回里抹掉（同轮其它事实仍 ACTIVE） |
| BUG-3 | medium-high | 幂等缓存按「对端 IP」分域 → 同一把 Idempotency-Key 让**另一个租户**的写请求 409 |
| BUG-4 | medium-high | 召回可以返回**空包**且整份 token 预算一分未用 |
| BUG-5 | medium | 自定义记忆类型的 update 幂等重放被误判成 `operation_id conflict`（应为 `already_done`） |
| BUG-6 | medium | LLM 摘要失败后**每个 append 都重发一次**刷新（文档承诺「无重试风暴」） |
| BUG-7 | medium | 后端记录 ≥ 4096 条时，`erase(scope=MEMORY/SESSION)` **永久失败**，且报错信息归因错误 |
| BUG-8 | low | 引擎侧原始消息按**去空格后的**正文删，mem0 存的是原文 → 带首尾空白的消息删不掉 |
| BUG-9 | low | 重放（重试 / 崩溃恢复 / rebuild）把 TTL 记忆的到期时间从「写入时间+TTL」推成「重放时间+TTL」 |

---

### BUG-1: `erase(scope=ALL)` 把无本地索引行的后端记录原文留在 mem0 history.db，审计却报告「已清干净」

- **Severity**: high
- **Location**: `src/thinkback/memory/service.py:2456-2481`（`_run_erase_stages` 的 `history` 阶段）、`src/thinkback/memory/backends/mem0_library.py:651-745`（`purge_history`）
- **Reproduction**: `/tmp/tbhunt/p7b_mem0_purge.py`（脚本内自建 mem0 2.2.1 同构 SQLite schema：`history(id, memory_id, old_memory, new_memory, event, …)` + `messages(...)`；client 的 `delete` 复刻 mem0 `Memory._delete_memory` 的行为：删向量后写一行 `old_memory=<原文>, event='DELETE', is_deleted=1`）

  ```
  $ cd /tmp/tbhunt && uv run --project /Users/zhangpeng/workspace/liaohe/Thinkback --group dev python p7b_mem0_purge.py
  append resp: status='completed' task_id='memory-extract:r1' l3_events=[{'event': 'ADD', 'backend_memory_id': 'm-1'}]
  index rows: [('m-1', 'ACTIVE', '今天会议讨论了排产计划与交付节奏')]
  erase: status='completed' task_id='memory-erase:opALL' affected_memories=1
  history after erase:
       ('dark-1', 'ADD', None, '暗数据里的秘密事实')
       ('dark-1', 'DELETE', '暗数据里的秘密事实', None)
  audit: {'affected_memories': 1, 'backend_records_deleted': 1,
          'mem0_history_purged': 'complete',
          'mem0_history_rows_deleted': 2, 'mem0_message_rows_deleted': 0, …}
  ```
  期望：`history` 表里一条原文都不剩。实际：`dark-1` 的 **ADD 与 DELETE 两行都还带着原文**。
- **Root cause**:
  - `history` 阶段的清理入参是 `memory_ids=backend_ids`，而 `backend_ids` 只来自**本地索引行**（`service.py:2296-2301` 的 plan）。
  - 但 `backend` 阶段对 `scope=ALL` 走的是 `self.backend.delete_all(user_id, memory_scope_id=namespace)`（`service.py:2443`），删的是**后端该作用域的全部记录**，包含本地没有索引行的「暗数据」（代码自己在 `service.py:2441-2443` 的注释里承认这类暗数据存在：`ALL 必须覆盖本地没有索引行的后端记录（抽取后索引失败等暗数据）`）。
  - mem0 的 `Memory._delete_memory`（`.venv/.../mem0/memory/main.py:2126`）在每次删除时都会 `db.add_history(memory_id, prev_value, None, "DELETE", is_deleted=1)`，`prev_value` 就是**事实原文**。
  - 于是 `delete_all` 删掉的暗数据，其 ADD 行与 DELETE 行都留在 `history.db`，而 `purge_history` 只按 `backend_ids` 删 → 永远删不到。
  - 审计侧 `_record_erase_audit`（`service.py:2637-2680`）对 `scope=ALL` 硬编码 `purge_state = "complete"`，**不看实际删了几行**（`mem0_history_rows_deleted` 只统计本地索引那批）。所以「物理擦除已完成」是对外做出的**假承诺**。
- **Covered by existing test?** **no**。`tests/unit/test_memory_contracts.py` / `test_erase_operation_ownership.py` 都用 `FakeMemoryBackend`（其 `purge_history` 永远返回 `supported: False`），从不构造真实 `history.db`，因此这条路径**零覆盖**。

---

### BUG-2: 删/抑制单条记忆会把**整轮**原始对话从 L1 召回里抹掉

- **Severity**: high
- **Location**: `src/thinkback/memory/service.py:1775-1790`（recall 的排除集过滤）、`src/thinkback/memory/repositories/{in_memory,sqlalchemy}.py` 的 `excluded_source_refs_for_user`
- **Reproduction**: `/tmp/tbhunt/p3_delete_one.py`

  ```
  $ cd /tmp/tbhunt && uv run --project /Users/zhangpeng/workspace/liaohe/Thinkback --group dev python p3_delete_one.py
  facts:
     memory-5371a2a9-… ACTIVE 我叫张三，住在上海，我喜欢喝美式咖啡 [{'session_id':'s1','round_id':'r1'}]
     memory-2723852c-… ACTIVE User favorite drink: 美式咖啡         [{'session_id':'s1','round_id':'r1'}]
  recall before delete: [('L3','我叫张三，住在上海，我喜欢喝美式咖啡'), ('L1','user: 我叫张三，住在上海，我喜欢喝美式咖啡\nassistant: 好的')]
  delete resp status='completed' affected_memories=1
  facts after:
     memory-5371a2a9-… DELETED 我叫张三，住在上海，我喜欢喝美式咖啡
     memory-2723852c-… ACTIVE  User favorite drink: 美式咖啡     ← 同轮的兄弟事实还活着
  journal rounds still present: [('r1', 'active')]              ← 轮次原文仍在库里
  recall AFTER delete of ONE fact: []                            ← 整轮对话消失
  excluded refs: {('s1', 'r1')}
  ```
  期望：用户只要求删掉一条事实，**同轮的另一条事实仍然 ACTIVE**，这一轮的原始对话应当留在 L1。实际：recall 返回**空包**。
- **Root cause**:
  - 排除集是**轮次粒度**的：`excluded_source_refs_for_user` 把 DELETED/SUPERSEDED/SUPPRESSED 记忆的 `source_refs`（`(session_id, round_id)`）整条塞进排除集；recall 再用 `(entry.session_id, entry.round_id) in excluded_refs` 把**整个轮次**从 L1 里剔掉。
  - 只要该轮产生过 **≥2 条事实**，删任意一条 → 整轮 L1 原文消失，另一条事实变成「有事实、没出处」的孤儿（journal 行仍是 `active`，只是召回时被排除）。
  - 这与代码自己的注释直接矛盾（`service.py:1776`）：`delete 只 clear_l1 不墓碑 journal（多事实共享轮次时保留原文）` —— 注释说保留原文，代码用排除集把原文挡在召回之外。
  - 同一条排除集也喂给 `upsert_summary_from_journal` / `_rebuild_rounds`，所以 L2 摘要同样丢掉这一轮。
- **Covered by existing test?** **no**。现有测试只断言「被删记忆不再可召回」（`tests/unit/test_memory_service.py:5430` 附近测的是 SUPERSEDED 的语义），**没有**任何用例断言「同轮兄弟事实仍在时，L1 原文应当保留」。

---

### BUG-3: 幂等缓存按「对端 IP」分域 → 别的租户用同一把 Idempotency-Key 会被 409 拒写

- **Severity**: medium-high
- **Location**: `src/thinkback/api/idempotency.py:236-246`（`_resolve_scope`）、`src/thinkback/api/idempotency.py:167-180`（冲突判定）
- **Reproduction**: `/tmp/tbhunt/p12_idem.py`

  ```
  $ cd /tmp/tbhunt && uv run --project /Users/zhangpeng/workspace/liaohe/Thinkback --group dev python p12_idem.py
  tenant A: 200 {'got': {'user_id': 'tenant-A', 'round_id': 'r1'}}
  tenant B: 409 {'error': {'code': 'TB-1005', 'message': 'Idempotency key reused with different request body'}}
  cache store keys: [('testclient', 'k-1', 'POST', '/v2/memory/events', '4ea6f175…')]
  ```
- **Root cause**:
  - `IdempotencyCache` 的域维度本意是**租户**（类 docstring：「租户这一维是**必需**的 … 否则租户 B 用和租户 A 相同的 Idempotency-Key + 相同 body，就能拿到 A 的缓存响应」）。
  - 但内网免鉴权后 `_resolve_scope` 退化成 `request.client.host`（**直连对端** IP）。在 k8s / ingress / sidecar 部署下，`request.client.host` 对所有外部调用方都是**同一个**（网关或 sidecar 的 IP），租户维度被压平成一个全局域。
  - 后果不是「读到别人的响应」（body 里有 `user_id`，hash 不同），而是**更糟的可用性事故**：域内任意一个客户端用过 key `k` 之后，其它任何客户端再用 `k`（body 不同）都会拿到 409 `TB-1005`，写请求被直接拒绝。一把 key 就能把别人的写路径堵死。
  - 同一把 key 在同一路径下被不同租户使用是**正常**用法（幂等键的约定作用域是调用方，不是服务实例）。
- **Covered by existing test?** **no**。`tests/unit/test_idempotency.py` 只测「同 key 同端点回放」「同 key 不同端点不串」「读路径不进缓存」「过期条目不冲突」，**没有**任何用例覆盖「不同客户端（不同 scope）之间互不影响」。

---

### BUG-4: 召回可以返回**空包**，且整份 token 预算一分未用

- **Severity**: medium-high
- **Location**: `src/thinkback/domain/recall_policy.py:230-254`（`_allocate_by_layer`）与 `:256-345`（`_clip_by_layer`）
- **Reproduction**: `/tmp/tbhunt/p6b_budget.py`

  ```
  $ cd /tmp/tbhunt && uv run --project /Users/zhangpeng/workspace/liaohe/Thinkback --group dev python p6b_budget.py
  costs: 60 60
  budget=100: kept layers=[] count=0        ← 空包，100 token 预算全部闲置
  budget=120: kept layers=['L3', 'L1'] count=2
  ```
  调用形态：`dedupe_and_clip([MemoryItem(layer="L1", content=<60 token>), MemoryItem(layer="L3", content=<60 token>)], 100)`。
- **Root cause**:
  - `_allocate_by_layer` 先按份额切：L1 ≤ `int(100*0.3)=30`、L3 ≤ `int(100*0.5)=50`；剩余 20 token 按缺口比例回流，最多只能把 L1 拉到 48、L3 拉到 51（实测 `_allocate_by_layer({'L1':60,'L3':60}, 100) == {'L1':48,'L3':51}`）。
  - `_clip_by_layer` 的挑选条件是 `if used + cost > limit: continue` —— 单条成本（60）**大于该层的最终上限**（48/51）→ 两条都被跳过。
  - 结果：层配额被当成硬上限，而条目成本可以超过配额，于是「预算没花完却什么都不剩」。`token_budget` 的 schema 下限是 100（`memory/schemas.py:347-352`），100 正是最坏档；默认 1200 时同样可复现（L1 一条 400 token 的长消息 + L3 两条 600 token 的事实 → L1 被整条丢掉，而 1200 的预算装得下 400+600=1000）。
  - 讽刺的是 `_clip_by_layer` 的注释写着「一条超长内容不该把整层清空（否则上下文直接空包）」——它防住了「层内一条超长清空整层」，却没防住「层配额本身小于单条成本 → 整层被清空」，而这正是空包的唯一来源。
- **Covered by existing test?** **no**。`tests/unit/test_memory_recall_budget.py` 只测「类型上限」「配额回流」「层保底不受类型上限影响」；`tests/unit/test_memory_service.py:8670` 的 `test_recall_keeps_current_turn_when_l3_floods_budget` 用的是 budget=300、L1 成本 ≈59（< 30% 份额 90），**恰好绕开了**「单条成本 > 层配额」这一档。**没有**任何用例断言「预算有余额时不得返回空包」。

---

### BUG-5: 自定义记忆类型的 update 幂等重放被误判成 `operation_id conflict`

- **Severity**: medium
- **Location**: `src/thinkback/memory/service.py:2753-2768`（`update_memory` 的 `finalize_update`）、`src/thinkback/memory/service.py:2946-2976`（`_completed_update_response`）
- **Reproduction**: `/tmp/tbhunt/p5_update_replay.py`

  ```
  $ cd /tmp/tbhunt && uv run --project /Users/zhangpeng/workspace/liaohe/Thinkback --group dev python p5_update_replay.py
  stored memory_type: 'process_preference'
  first update: completed task result: {…, 'memory_type': None, 'summary_state': 'dirty'}
  replay update RAISED: ValueError operation_id conflict: completed update result is no longer current

  builtin stored memory_type: 'profile'
  first update: completed task result: {…, 'memory_type': 'profile', …}
  replay update: already_done
  ```
- **Root cause**:
  - `finalize_update` 记的是 `response.memory.memory_type.value if response.memory.memory_type else None`。
  - `_managed_memory_item`（`service.py:3527-3553`）里 `memory_type = self._enum_or_none(MemoryType, raw_memory_type)` —— **自定义类型名不在内置枚举里 → 返回 None**，自定义名只放在 `memory_type_name`。于是 task 结果里落的是 `None`。
  - 重放时 `_completed_update_response` 比较 `memory.memory_type != expected_memory_type`，即 `'process_preference' != None` → 抛 `operation_id conflict`。
  - 语义上这是**误报**：目标记忆仍然是那次编辑的结果（正文指纹一致），按 `docs/memory/Thinkback记忆三层架构.md:326` 的约定应当返回 `already_done`，而不是「已被后续编辑/删除」的冲突。对外表现为 HTTP 409（`service_error_to_http` 命中 `conflict`），客户端拿到的是「冲突、别重试」的信号，而真相是「早就成功了」。
- **Covered by existing test?** **no**。`tests/unit/test_memory_service.py:8820` 的 `test_update_accepts_registered_custom_memory_type` 只验证**首次**更新把类型改成自定义类型 + 未注册类型被拒，**没有**重放同一 `operation_id` 的用例。

---

### BUG-6: LLM 摘要失败后每个 append 都重发一次刷新（文档承诺「无重试风暴」）

- **Severity**: medium
- **Location**: `src/thinkback/memory/l2_refresh.py:286`（`_run_refresh` 里只有成功路径才调 `_remember_scope`）、`src/thinkback/memory/l2_refresh.py:127-128`（`maybe_submit` 的 `due` 判定）
- **Reproduction**: `/tmp/tbhunt/p9_l2_storm.py`

  ```
  $ cd /tmp/tbhunt && uv run --project /Users/zhangpeng/workspace/liaohe/Thinkback --group dev python p9_l2_storm.py
  composer invocations for 6 appends: 6
  expected (debounce interval 5): 2
  ```
  （composer 恒抛 `RuntimeError("llm down")`，同 session 连打 6 次 append。）
- **Root cause**:
  - `maybe_submit` 的判定是 `due = not has_llm_summary or counter >= self.refresh_interval_rounds`，其中 `has_llm_summary = scope in self._llm_refreshed_scopes`。
  - `_llm_refreshed_scopes` **只在刷新成功时**由 `_remember_scope` 写入（`l2_refresh.py:283-285`，在 `upsert_summary_from_rounds` 成功之后）。刷新失败走 `except` 分支（`l2_refresh.py:298-304`），**不记账**。
  - 于是失败后 `has_llm_summary` 恒为 False → `due` 恒为 True → **每一次 append 都提交一次后台 LLM 刷新**。去抖计数（`refresh_interval_rounds`）只在提交成功时清零，对「提交后失败」无效。
  - 与模块 docstring 的承诺直接冲突：「LLM 失败：打 warning 后保留拼接版，**无重试风暴**（best-effort 优化语义）」。LLM 挂掉时，这条路径把故障放大成「每个 append 一次 LLM 调用」，正好在最需要减压的时候加压。
- **Covered by existing test?** **no**。`tests/unit/test_l2_llm_summary.py:164` 的 `test_llm_failure_keeps_concat_fallback_and_service_alive` 只断言「失败后保留 concat、之后能恢复」，**没有**统计失败期间的 composer 调用次数。

---

### BUG-7: 后端记录 ≥ 4096 条时 `erase(scope=MEMORY/SESSION)` 永久失败，且报错归因错误

- **Severity**: medium
- **Location**: `src/thinkback/memory/service.py:2523-2562`（`_residual_backend_ids`）、`src/thinkback/domain/ports.py:32`（`MEMORY_LIST_PAGE_LIMIT = 4096`）
- **Reproduction**: `/tmp/tbhunt/p11_erase_big.py`

  ```
  $ cd /tmp/tbhunt && uv run --project /Users/zhangpeng/workspace/liaohe/Thinkback --group dev python p11_erase_big.py
  index rows: [('fake-a3a51619-…', 'ACTIVE')]
  backend record count: 4097
  erase RAISED: RuntimeError erase backend stage incomplete: 1 record(s) still present in backend
  task status: failed last_error: erase backend stage incomplete: 1 record(s) still present in backend
  backend record count after: 4096          ← 目标记录其实已经删掉了
  index rows after: [('fake-a3a51619-…', 'ACTIVE'), ('erase-tombstone:12c1425207373d24a821e6004bb2c849', 'DELETED')]
  ```
  注意：**目标记录已从后端删除**（4097 → 4096），但擦除任务被判失败。
- **Root cause**:
  - `_residual_backend_ids` 用 `backend.get_all(..., limit=MEMORY_LIST_PAGE_LIMIT)` 复核残留。当用户在该 namespace 的后端记录 ≥ 4096 时，`get_all` 返回**满页 4096 条**（即便目标已被删）。
  - 代码随即走 `if len(remaining) >= MEMORY_LIST_PAGE_LIMIT: return sorted(target_ids)` —— 判定为「无法证明已删」→ 抛 `RuntimeError("erase backend stage incomplete: N record(s) still present in backend")`。
  - 该分支**不区分「目标还在」和「页面取满」**。前者是真残留，后者只是复核手段不够（应该翻页或按 id 点查）。所以对一个重用户，**每一次** 单条/会话擦除都必然失败，且用同一个 `operation_id` 重试会再次撞上同一条件 → **永久失败**（只有 `scope=ALL` 走 `delete_all` 那条分支能过）。
  - 错误信息本身也是错的：`1 record(s) still present` 指的是 `target_ids` 的长度，不是真正残留的记录数，运维按这条信息去查会查错方向。
- **Covered by existing test?** **no**。现有 erase 用例的 fake 后端记录数都是个位数，`len(remaining) >= 4096` 分支**从未被执行**。

---

### BUG-8: 引擎侧原始消息按「去空格后的正文」删，mem0 存的是原文 → 带首尾空白的消息删不掉

- **Severity**: low
- **Location**: `src/thinkback/memory/backends/mem0_library.py:727-736`（`purge_history` 的 `content IN (...)`）、`src/thinkback/memory/service.py:2564-2582`（`_erased_message_texts` 做 `.strip()`）
- **Reproduction**: `/tmp/tbhunt/p10_msg_purge.py`

  ```
  $ cd /tmp/tbhunt && uv run --project /Users/zhangpeng/workspace/liaohe/Thinkback --group dev python p10_msg_purge.py
  counts: {'history': 0, 'messages': 0, 'supported': True}
  messages left in mem0 history.db: [('m1', '  我的密码是 hunter2  ')]
  EXPECTED: [] (row deleted)
  ```
- **Root cause**:
  - mem0 的 `SQLiteManager.save_messages`（`.venv/.../mem0/memory/storage.py:267-280`）存的是 `message.get("content")` **原样**。
  - Thinkback 传给后端的是 `trusted_messages`（原始 dict，未 strip），所以 `messages` 表里就是带空白的原文。
  - `_erased_message_texts` 却对每条正文做 `.strip()` 后再作为 `content IN (...)` 的匹配值 → **大小写/空白不一致就匹配不上**，DELETE 影响 0 行，而 `counts["messages"]` 记 0，**调用方无法区分「没有可清的」和「没清掉」**。
  - 影响面有限：`scope=ALL` 走 `purge_all_messages=True`（按 session_scope 删，不受空白影响）；只有 `scope=MEMORY/SESSION` 的按正文删受影响，而审计对这两个范围本就标 `partial`。但代码的意图是「精确删掉自己那几条」，实测没删掉。
- **Covered by existing test?** **no**。`FakeMemoryBackend.purge_history` 是空实现，真实 SQLite 路径零覆盖。

---

### BUG-9: 重放把 TTL 记忆的到期时间从「写入时间+TTL」推成「重放时间+TTL」

- **Severity**: low
- **Location**: `src/thinkback/memory/service.py:4838-4864`（`_index_lifecycle`：`expires_at = now + ttl_days`）、`src/thinkback/memory/service.py:2977-2989`（`_replay_metadata` 不带原始到期时间）、`src/thinkback/memory/service.py:5028/5075`（`update_memory_index(..., expires_at=expires_at)` 覆盖已有行）
- **Reproduction**: `/tmp/tbhunt/p13_ttl_reindex.py`（把 `service` 模块的 `datetime.now` 拨快 80 天后重放同一轮）

  ```
  $ cd /tmp/tbhunt && uv run --project /Users/zhangpeng/workspace/liaohe/Thinkback --group dev python p13_ttl_reindex.py
  write  expires_at: 2027-01-26 23:59:59.999999+00:00
     row: memory-a0f52d54-… ACTIVE 2027-01-26 23:59:59.999999+00:00
     row: memory-ad552162-… ACTIVE 2027-04-16 23:59:59.999999+00:00
  replay expires_at: 2027-04-16 23:59:59.999999+00:00
  extended by: 80 days, 0:00:00
  policy: state ttl_days=90 -> expiry must stay at write_time+90d
  ```
  类型 `state` 的 TTL 是 90 天（`src/thinkback/domain/memory_types.py:186-192`）。
- **Root cause**:
  - 到期时刻是**索引时**用 `datetime.now(UTC)` 现算的（`_index_lifecycle`），原始到期时刻**从不写进 journal 的 `request_metadata`**（`_replay_metadata` 只带 `memory_type` / `source_timestamp`）。
  - 于是任何重放都会用「重放时刻」重算：`recover_pending_writes`（崩溃恢复）、append 失败重试（`_prefetch_backend_events` 复用后端记录 → `_index_l3_event_locked` 的 `existing` 分支 `update_memory_index(..., expires_at=<now+ttl>)`）、`rebuild` 回放。
  - 结果：一条本该在写后 90 天过期的事实，在第 80 天被重放后**又活了 90 天**（多活 80 天）。TTL 策略被系统自己的恢复机制绕过。
- **Covered by existing test?** **no**。`tests/unit/test_memory_expiry_namespace_rebuild.py:152` 只测「append 时 TTL 类型在索引行上落下到期时间」，**没有**测「重放/恢复不得延长到期时间」。

---

## 我检查过、确认是**正确**的部分

- **mem0 2.2.1 的签名适配**（`_scope_kwargs` / `_limit_kwargs`）：实测 `mem0.Memory.search` 的签名是 `(query, *, top_k=20, filters=None, threshold=0.1, rerank=False, explain=False, reference_date=None, show_expired=False, **kwargs)`，**没有** `user_id`/`agent_id` 顶层参数；`get_all` 同理。适配器因此走 `{"filters": {...}}` + `{"top_k": N}` 分支，与 mem0 2.2.1 的 `_reject_top_level_entity_params` 兼容。`get_all` 的 `top_k` 校验只要求非负 int，4096 合法。**当前版本是对的**（若 mem0 未来给 `search` 加回顶层 `user_id`，`_scope_kwargs` 会切到 `filters=None` 形态并被 mem0 拒掉——这是未来的脆弱点，不是当前缺陷）。
- **`_mem0_inline_threadpool` 的全局 monkey-patch**：深度计数在 `RLock` 内维护，最后一个使用者退出时才恢复原 `concurrent`；嵌套调用不会互相覆盖。mem0 2.2.1 对 `concurrent.futures` 的用法只有 `ThreadPoolExecutor(...)` 的 `with` + `pool.submit(...)` + `concurrent.futures.as_completed(...)`，`_InlineThreadPoolExecutor` 提供的 `__enter__/__exit__/submit` + `_Mem0FuturesProxy.__getattr__` 转发足以覆盖。多线程下不会有「恢复过早」的问题（深度计数保证）。**未发现缺陷**。
- **mem0 `delete_all` 的翻页循环**（`mem0_library.py:591-620`）：`get_all(top_k=4096)` → 逐条删 → `len(memory_ids) < page_limit` 才 break，否则继续翻页，最多 100 页。**翻页逻辑本身是对的**（BUG-7 的问题出在擦除的*复核*环节，不在 `delete_all`）。
- **mem0 `history` 表的 `DELETE ... WHERE memory_id IN (...)`**：与 mem0 2.2.1 的真实 schema（`memory_id, old_memory, new_memory, event, is_deleted, …`）一致；表缺失 / 列漂移时 fail-visible 抛错（符合契约）。
- **mem0 的 session scope 拼法**：`Mem0LibraryMemoryBackend._mem0_session_scope` 复刻的 `agent_id=…&user_id=…` 与 mem0 的 `_build_session_scope`（键按 `agent_id, run_id, user_id` 字母序、同样转义 `% & =`）一致，`messages` 表按该键删除是对的。
- **并发幂等**：5 个线程并发 append 同一 `round_id` → 结果 `1 completed + 4 already_done`，journal 1 条、索引 1 行、后端 1 条、task `completed/retry_count=0`。**没有重复抽取。**
- **delete 与 append 竞争**：删除先跑时，随后到达的 append 不会被误吞（`source_timestamp` 晚于删除屏障截止时间），轮次保持 `active`。**行为正确。**
- **namespace 隔离**：L1（journal.namespace）、L2（`ins_current_summary.namespace` + `uq_ins_current_summary_scope`）、L3（`memory_scope_id` 即 namespace）在读写删三条路径上都带了 namespace 过滤；`tests/unit/test_memory_namespace_isolation.py` 覆盖了 L1/L2 隔离、ALL 删除不跨 namespace、会话擦除不删别的 namespace。**未发现遗漏。**（`excluded_source_refs_for_user` 不按 namespace 聚合，但因为 `round_id` 在 journal 表上全局唯一、`ins_summary_round_journal.round_id` 有 unique 约束，来源引用天然不跨 namespace，实际安全。）
- **召回计数一致性**：只有真正进入上下文的 L3 条目才 `touch_memory_recalled`（`service.py:1893-1902` 按 `delivered_memory_ids` 过滤），被去重/裁掉的条目不计数；`tests/unit/test_recall_count_after_clip.py` 覆盖。**正确。**
- **任务状态机**：`DEAD_LETTER` 被正确排除出可重试集合（`task_state.py`），`append` 的 dead-letter 短路存在；`_mutate_task_with_retry` 的终态写用 `strict=True`，CAS 耗尽会抛 `TaskLockExhaustedError` 而不是谎报 completed。**正确。**
- **L1 缓存键空间上界**：工作区未提交的改动给 `L1CacheMixin` 加了 `L1_CACHE_MAX_KEYS = 1000` + `_evict_oldest_keys()`（`src/thinkback/memory/repositories/_l1_cache.py:33, 102-114`），键空间不再无界增长。**已修复**（HEAD 上尚未包含）。
- **限流器**：`TokenBucket.consume` 的 refill 逻辑、限额变更时不重置余额（`api/ratelimit.py:70-82`）都正确；`_RATE_LIMIT_BURST`/`_RATE_LIMIT_PER_MINUTE` 的默认值合理。`self.buckets` 按对端 IP 增长理论上无界，但在「内网免鉴权 + 按 IP 分桶」的定位下可接受，**未作为缺陷上报**。

## 我没能验证的部分（诚实缺口）

- **PostgreSQL 路径全部未实测**。本机没有跑起来的 PG/Milvus，所有 SQL 结论（`sqlalchemy.py` 的 CAS、行版本、`ins_memory` 的 partial unique index、`_upsert_summary` 的保护条件）都是**读码论证**，没有真库复现。BUG-7 的页上限判定在 SQL 后端同样成立（`get_all` 走 mem0，与仓储无关），但其余 SQL 分支建议在有 PG 的环境再跑一遍。
- **mem0 2.2.1 真实链路未实测**。BUG-1 / BUG-8 用的是「与 mem0 2.2.1 同构」的 SQLite + 同签名 client 复现，不是真 mem0 + 真 Milvus。mem0 侧的行为（`add_history` 写入 `old_memory`/`new_memory`、`save_messages` 存原样 content、`_delete_memory` 写 DELETE 行）都是**照 mem0 2.2.1 源码逐行核对过的**（`.venv/lib/python3.12/site-packages/mem0/memory/{main,storage}.py`），但没有端到端跑过真引擎。
- **多副本场景**未测。`replica_guard` 的五项前置、多副本下的任务抢占 / CAS 冲突 / 删除屏障跨副本生效，都只在单进程 in-memory 仓储下推演过。跨副本的「append 与 erase 交错」「同一 round_id 两副本并发」这类问题需要两个真进程 + 共享 PG 才能证伪。
- **`MemoryService.rebuild()` 的全量路径**（3007-3259 行）只做了静态阅读，没有构造出触发缺陷的复现；特别是 `_uncovered_rebuild_rounds` 与 `_rebuild_slot_is_covered_by_active_memory` 的覆盖判定，边界情形多但我没找到确定的反例。
- **`admin.py` 的治理面**（508 行）只粗读了审计落库与列表过滤，没有系统排查；`api/health.py`、`api/metrics.py`、`api/readiness.py` 基本没看。
- **`decay` 清扫器**只做了静态阅读：`MemoryDecaySweeper._decay_scope` 把记忆置 `SUPPRESSED` 后，同轮的 L1 原文会因 BUG-2 的排除集机制一并消失。这条我判断是 BUG-2 的衍生影响而非独立缺陷（decay 默认关闭、且只作用于 90 天前的长尾），**未单列**。
- **性能/资源类**问题（幂等缓存每次请求 O(n) 扫全表剪枝、限流桶按 IP 无界、SQL 仓储 `_prune_expired` 同理）按要求未作为缺陷上报。

---

## 建议的修复顺序（仅供参考，本次未改任何代码）

1. **BUG-1 / BUG-2** 属于「对外承诺与实际不符」类，直接影响合规与数据可见性，优先。
2. **BUG-3 / BUG-4** 是线上可感知的可用性事故（409 拒写 / 空上下文包），次之。
3. **BUG-5 / BUG-6 / BUG-7** 是正确性与幂等语义问题。
4. **BUG-8 / BUG-9** 影响面小，可与其他改动搭车。
