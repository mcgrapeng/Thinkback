# innies-memory 生产 Bug 调查报告

| 元信息项 | 值 |
| --- | --- |
| 调查范围 | dev 分支重构后全量代码（service / repositories / backends / api / rpc / domain） |
| 调查方法 | 逐文件路径追踪 + 10 项假设逐一验证/证伪 + 失败场景推演 |
| 结论 | 确认 9 项（4 HIGH / 5 MEDIUM）+ 若干低危边界；**已修复 7 项**（含全部 HIGH 的可安全修复面），2 项列为多副本前置条件；6 项假设被证伪（含"重构引入行为漂移"——未发现任何漂移） |
| 验证 | 每项修复附回归测试或既有 478 测试全绿（pytest + ruff + mypy strict） |

---

## B-1 (HIGH) async 删除任务中途失败后重试，永久卡 `running`

**代码功能**：`delete` 在 async 模式下把 `backend.delete`（mem0 网络 I/O）提交到
L3 后台执行器，用任务结果里的 `pending_cleanup_tasks` 计数跟踪"还有几个清理
future 在飞"；归零后任务转 `COMPLETED`。

**问题是什么**：`_finish_l3_cleanup`（service.py）只在任务处于 `RUNNING` 时
递减计数。删除主体在清理已提交**之后**失败（如下一步 DB 写抛错）时，任务标
`FAILED`，随后完成的后台清理 future 不再递减 → 计数残留。

**为什么失败**：客户端用同一 `operation_id` 重试时，`_register_pending_l3_cleanup`
在残留计数上再 +1。此后分母（计数）永远大于分子（存活 future 数）：每个真实
future 完成只减 1，计数到不了 0，任务停在 `RUNNING`；后续重试全部被
`_existing_delete_response` 以 `running` 短路 —— **删除操作永久不可完成**。

**边缘情况**：
- 单机测试难以复现：`FakeMemoryBackend` 删除极快，future 常在主体失败**前**
  完成（竞态掩盖）。需门控 backend 确定性复现（见回归测试
  `test_delete_retry_converges_after_midflight_failure_with_stale_pending_count`，
  红→绿验证）。
- 反向边界：`FAILED` 状态本身不该被后台完成翻成 `COMPLETED`（重试语义保留）。

**修复后的生产代码**（service.py `_finish_l3_cleanup`）：

```python
elif task is not None:
    # 计数递减不依赖任务当前状态：delete 主体中途失败（task 已 FAILED）时，
    # 已提交的后台清理 future 仍要递减 pending_cleanup_tasks。
    pending = max(0, int(task.result.get("pending_cleanup_tasks", 1)) - 1)
    task.result = {**task.result, "pending_cleanup_tasks": pending}
    if pending == 0 and task.status is TaskStatus.RUNNING:
        task.status = TaskStatus.COMPLETED
    self.repository.save_task(task)
```

不变量恢复为"每次注册(+1)恰有一个 future 完成(−1)"，与任务状态解耦。

---

## B-5 (HIGH) L1 短期记忆只存在进程内，多副本随机丢最近轮次

**代码功能**：recall 的 L1 读最近 N 轮完整对话；轮次本身已持久化在
`ins_summary_round_journal`（PG），进程内 `l1_cache` dict 只是加速。

**问题是什么**：`get_l1` 只读本进程 dict。k8s `deployment-api.yaml` 是
`replicas: 2` 且 Service 无会话亲和 —— append 落 Pod A（写 A 的缓存），
recall 路由到 Pod B → B 缓存为空 → **L1 直接返回空**，用户约一半概率丢失
"刚刚说过的话"；进程重启同样丢 L1。

**为什么失败**：缓存 miss 被当成"没有数据"，而 truth 在 PG。

**边缘情况**：残留窗口 —— 删除操作只失效本副本缓存；若 Pod B 曾缓存过该
会话，会继续返回已删轮次直至淘汰。彻底解决需跨副本失效或读穿透，已记录在
ARCHITECTURE §6.1（水平扩展前置条件）。

**修复后的生产代码**（repositories/sqlalchemy.py）：

```python
def get_l1(self, user_id, memory_scope_id, session_id):
    """L1 读取：进程缓存优先，缓存缺失时回源 PG journal 派生。"""
    cached = super().get_l1(user_id, memory_scope_id, session_id)
    if cached:
        return cached
    rounds = self.list_rounds(user_id, memory_scope_id, session_id)[-L1_CACHE_LIMIT:]
    for entry in rounds:
        super().update_l1(entry, limit=L1_CACHE_LIMIT)
    return super().get_l1(user_id, memory_scope_id, session_id)
```

（`list_rounds` 天然过滤 `round_state='active'`，已删轮次不回源。）

---

## B-3 (HIGH，多副本前置) `_task_status_lock` 进程内 + `_save_task` 盲写整行

**代码功能**：任务表读写走 `get_task → 改内存快照 → save_task 全列覆盖`，
用进程内 RLock 串行化读-改-写，防丢失更新。

**问题/为什么失败**：锁只在单进程有效。两副本重试同一任务时双方都读到
`retry_count=2`、各写 3 → 重试预算少计（dead_letter 永远达不到，N-3 保护失效）；
`result`/`l3_events` 相互覆盖；`pending_cleanup_tasks` 增减丢失会喂养 B-1。
`claim_task` 的 IntegrityError 回退只保护**首次插入**，之后所有 save 都是盲写。

**处置**：未在本次修改（需 `ins_memory_task` 加版本列做乐观锁，属 schema
变更）。已作为**多副本水平扩展的硬前置**写入 ARCHITECTURE §6.1/§6.2；
当前按"任务由创建它的副本收尾 + 幂等回放"单写者约束运行。

---

## B-4 (HIGH，产品决策) 白名单外记忆写入 mem0 但永不可召回

**代码功能**：`MEMORY_P0_SLOTS` 控制哪些槽位的 mem0 抽取进入本地
`ins_memory` 索引；recall 只返回本地索引中 ACTIVE 的行。

**问题/为什么失败**：配置注释宣称"未列入白名单的 slot 仍留在 mem0 中供
/recall 语义召回"，但 recall 对无本地行的 backend 命中直接 `continue`——
白名单外记忆写进 Milvus 后既不可召回也不会被删，成为永久存量数据（GDPR
视角即"删不干净的暗数据"）。槽位查询路径甚至完全跳过 `backend.search`。

**处置**：属产品语义决策（改 recall 直通 or 准入处拒绝），本次修正误导性
文档（config.py / .env.example），并在 ARCHITECTURE §6.1 明示该行为。

---

## B-2 (MEDIUM) gRPC 与 HTTP 的错误映射不一致：dead_letter

**问题/为什么失败**：`_check_retry_budget` 抛
`RuntimeError("dead_letter: …")`，HTTP 侧映射 409（停止重试），gRPC 侧无
该分支落入 INTERNAL —— gRPC 客户端无法区分"预算耗尽该放弃"与"服务端 bug"，
重试策略分叉。**修复**：servicer `_handle_error` 增加 `dead_letter →
ABORTED`（语义对齐 409）+ 回归测试。

---

## B-6 (MEDIUM) `delete_all` 单页 1 万上限在向量库留残骸

**问题/为什么失败**：`get_all(limit=10000)` 只取一页；超过 1 万条的用户，
本地行全部标 DELETED 但 Milvus 向量未删 —— "删除我的全部数据"不完整
（原兜底的"后续清理任务"已随 Celery 移除）。**修复**：翻页清理直至取空
（`delete_many` 后下一页自然后移，安全上限 100 页 = 100 万条）。

---

## B-7 (MEDIUM) rebuild 对每个回合做两次全量索引扫描

**问题/为什么失败**：`_round_is_after_delete_all_cutoff` /
`_round_is_after_session_delete_cutoff` 每次调用都 `list_memories` 全量扫描
（无 LIMIT），且在 rebuild 的回合循环里逐轮调用 —— O(rounds × memories)
次查询全部串行挤过仓储单线程事件循环，大用户 rebuild 会把**其他用户**的
DB 操作拖到 30s 超时。**修复**：`_RebuildDeleteCutoffs` 快照 —— delete-all
截止每次 rebuild 算一次，session 截止按 session_id 惰性记忆。

**边缘情况**：快照一致性 —— rebuild 期间的并发删除可能不被本次快照看到，
与原逐轮读取相比语义略弱（原本每次读取也可能读到中间态），可接受。

---

## B-8 (MEDIUM) 关机顺序：L3 残留任务白等 30s 才报误导性超时

**问题/为什么失败**：lifespan 先 `drain(30s)` 再 `repository.close()`，但
`shutdown_l3_executor(wait=False)` 不取消排队任务；超出 drain 窗口的工作
继续执行，其仓储调用进入**已停止**的事件循环 —— `run_coroutine_threadsafe`
永不解析，线程白等满 30s 后报"repository operation timed out"（误导排障）。
**修复**：`_run` 入口 fast-fail（`loop.is_closed() or 线程已亡 →
RuntimeError("event loop is closed")`）。

---

## 低危 / 边界（记录未改）

| 项 | 说明 | 处置 |
| --- | --- | --- |
| sync 模式 `_supersede_conflicting_memories` 在 `_index_mutation_lock` 内直接 `backend.delete` | 违反 H-3 不变量，慢 mem0 调用串行化全部用户索引写 | 待改走 `_BackendDeleteSync` 收集（触碰临界区，单独 PR） |
| `expires_at` 列无人读写 | 原计划的过期清理任务随 Celery 移除；TTL 语义实际不存在 | 需要时补 sweeper 或删列 |
| mem0 `delete` 把任何 `IndexError` 当"ID 不存在" | mem0 内部其他 IndexError 会被静默吞掉 → 向量孤儿 | 依赖 mem0 版本行为，已注释风险 |
| delete 未知 `memory_id` 返回 `completed/affected=0` | 与 update 的 404 契约不一致（幂等视角又说得通） | 客户端文档口径 |
| append 热路径每轮全量 `list_rounds` 重建 L2 + 多处绕过读缓存直查 | DB 放大（性能非正确性） | 列入性能 backlog |

## 已证伪假设（重构无行为漂移的独立验证）

1. `BoundedTTLCache` 与原实现语义一致（含 `<=` 边界、容量淘汰只对新键）；
2. API 信号量无泄漏（submit 失败显式释放；超时不取消 concurrent future，
   done callback 仍会释放）；
3. L3 写槽 reserve/release 严格配对（sync 不预留；async 路径三种失败均释放一次）；
4. `_run` 超时/取消路径无连接泄漏（shutdown 场景除外，见 B-8）；
5. slots 引擎抽取与原 service.py 逐字等价（`[一-鿿]` ≡ `[\u4e00-\u9fff]`（同一码位区间）；
   settings gate 时机一致）；
6. `SUPPRESSED` 未被排除无实际影响（无任何赋值点，状态不存在于数据中）。

## 验证汇总

```text
pytest: 478 passed（含 2 个新增回归测试）
ruff check: passed   mypy --strict: passed（48 files）
新回归测试均以"先在旧代码上红、修复后绿"验证
```

---

# 第二篇：全链路真实测试调查（R 系列）

| 元信息项 | 值 |
| --- | --- |
| 测试形态 | 真实服务进程（uvicorn，async 模式）+ 真实 PG / Milvus / ollama qwen2.5:7b / vLLM embedding |
| 链路覆盖 | HTTP 全链路 26/26、gRPC 全链路 17/17、仓库自带压测 `real_mem0_pressure` EXIT=0 |
| 结论 | 确认 8 项真实缺陷（R-0~R-7），全部修复；单测 492 passed / ruff / mypy strict 全绿 |
| 复跑方式 | `script/realchain/fullchain_http.py` / `fullchain_grpc.py`（服务需以 `no_proxy='*'` 启动） |

## R-0 (HIGH) `.env` 里的 DATABASE_URL 静默失效

**功能**：`Settings` 从环境变量/.env 加载配置；`.env.example` 把 `DATABASE_URL` 列为首选。
**问题/为什么失败**：字段名 `raw_database_url` 与键 `DATABASE_URL` 不匹配（extra=ignore 静默丢弃），唯一生效通道是 os.environ 特判。而 `import pymilvus` 会执行 `load_dotenv()` 把 .env 灌入 os.environ —— 配置是否生效取决于 **settings 单例创建是否晚于 pymilvus 导入**。单测因 conftest 重建 settings 侥幸通过；alembic CLI / uvicorn 冷启动独立进程静默回落 `postgres:postgres` → 认证失败。
**边缘情况**：k8s 注入真实环境变量时不受影响（未暴露的原因）。
**修复**：`AliasChoices("raw_database_url", "DATABASE_URL")` validation_alias，双通道显式生效；删除脆弱特判；回归测试覆盖纯 .env 通道。

## R-2 (MEDIUM) 未分类异常零观测

**问题**：`_run_memory_call_in_pool` 只映射 ValueError/RuntimeError 子串；其它异常静默上抛 → 线上一次性 500 **没有任何服务端 traceback**（本次调查因此一度断线）。
**修复**：未分类 RuntimeError 与未知异常先 `logger.opt(exception=…).error` 再上抛（行为不变）。

## R-4 (HIGH) readiness 与业务共享连接池 → 跨事件循环错绑 → 随机 500

**功能**：readiness `check_database` 执行 SELECT 1；业务仓储用私有事件循环线程跑全部 DB 操作；二者共用全局 engine 连接池。
**问题/为什么失败**：asyncpg 连接绑定创建它的 loop。readiness 在 uvicorn 主循环 checkout 连接并归还（连接绑定主循环）；业务私有循环 checkout 到它时 `pool_pre_ping` 抛 `RuntimeError: Future attached to a different loop` → 500。表现为"首个/偶发请求失败、重试成功"（pre_ping 失败后池作废旧连接重建）。
**边缘情况**：k8s readinessProbe **周期性**探测会持续制造错绑连接 —— 生产环境必现为随机 500；任何"单测混合真实 readiness+真实业务"缺失的流水线都无法发现。
**修复**：readiness 独立 `NullPool` 引擎（每次探测短命连接）；删除无人使用且同类隐患的 `get_session`；lifespan 双引擎 dispose。全链路脚本把"readiness 先于业务"固化为第一步。

## R-3 (MEDIUM) 槽位引擎中文贪婪捕获（真实 LLM 链路的实际错误值）

items 中出现 `User lives in 杭州工作了` / `User prefers to be called 小朋吧` / `User favorite drink: 的美式咖啡`：
- 地点：`搬到杭州工作了` 贪婪吞后缀 → 按中文停词（工作/上班/生活/了/标点…）截断；
- 昵称：吞语气词 → `rstrip("吧啊呢哦呀嘛嘿哪了的")`；
- 饮品：吞结构助词"的" → `lstrip("的")`；
- 另补 `prefers to be called/addressed as` 模式（词表 gate 有、模式缺，英文昵称返回 None）。
回归：`test_slot_extraction_real.py`。

## R-6 (HIGH) 自定义 embedder 不兼容 mem0 批量调用协议

**功能**：`OpenAICompatibleEmbeddingNoDimensions.embed` 编码文本。
**问题/为什么失败**：签名只收 `str`；mem0 的 UPDATE 事件路径传 `list[str]` 并期望 `list[list[float]]`（与 mem0 官方 OpenAI embedder 同构）→ `'list' object has no attribute 'replace'` → 整轮 L3 写入失败。
**修复**：`str | list[str]` 双形态 + 批量协议回归测试。

## R-7 (HIGH) mem0 事实抽取零结构校验 + 弱模型嵌套 JSON → 整轮失败

**问题/为什么失败**：mem0 默认 prompt 只要求 "json format"，把 LLM 返回的 `facts` 元素直接当 dict key —— qwen2.5:7b 偶发 `{"facts": [["x"], "y"]}` → `TypeError: unhashable type: 'list'`。
**边缘情况**：prompt 约束是概率性防御；届时异常包装为 RuntimeError → L3 任务 FAILED 可重试，async 主链路（L1/L2）不受影响；与 R-6 构成两级防御。
**修复**：`custom_fact_extraction_prompt` = mem0 默认 prompt + 扁平字符串数组硬约束（正反例）。

## R-1 / R-5（环境与脚本）

- R-1：macOS 系统代理（Clash）被 httpx `trust_env` 读取，劫持 loopback LLM 端点且 `NO_PROXY` 无法豁免（SOCKS）——服务须以 `no_proxy='*'` 或无代理环境启动；本机 gRPC 客户端必须 `grpc.enable_http_proxy=0`。
- R-5：`script/real_mem0_pressure.py` 引用不存在的 `RecallIntent.PREFERENCE/MEMORY_QUERY`（枚举只有 CHAT/SENSITIVE）→ 脚本腐烂从未可运行，已修为 CHAT。

## 观察项（未改）

- mem0 内部 `update` 路径在其 Milvus `get(id)` 上抛 ValueError（"Error getting memory … during update"），适配层已隔离（索引已物化、任务正常完成）；属 mem0×Milvus 版本兼容问题，升级 mem0 时验证。
- `round_id` 全局唯一（跨用户）：多租户客户端必须全局生成，已写入 API 契约说明的候选事项。

## 全链路测试结果（修复后）

```text
HTTP  全链路（readiness→append×3+幂等→L3 抽取→items→recall→update→delete→幂等→删除后不泄漏）: 26/26
gRPC  全链路（Append/GetTask/Status/List/Get/Recall/Update/Delete/幂等/sensitive fail-closed）: 17/17
real_mem0_pressure（5 轮 append→recall 8→delete→rebuild l2+l3→final recall 7）: EXIT=0
单测: 492 passed / ruff / mypy strict 全绿
```

---

# 第三篇：mem0 2.0.20 升级后全链路复测（W 系列）

| 元信息项 | 值 |
| --- | --- |
| 测试形态 | 真实服务进程（uvicorn 127.0.0.1:8000 + 内嵌 gRPC 50052，async 模式）+ 真实 PG / Milvus / ollama qwen2.5:7b / vLLM embedding |
| 触发背景 | P0（L2 LLM 摘要）、P1a（任务乐观锁）、P1b（mem0 0.1.118 → 2.0.20）三个 feature 合入后未做过全链路真实复测 |
| 链路覆盖 | HTTP 26/26 → 修复后 27/27（新增 L2 LLM 摘要检查）、gRPC 17/17、8 线程并发幂等（1 completed + 7 already_done、round 无重复） |
| 结论 | 确认 2 项真实缺陷（W-1/W-2）已修复 + 1 项部署事项（W-3）待决策；单测 522 passed / ruff / mypy strict 全绿 |

## W-1 (HIGH) mem0 2.0.20 紧邻写后读的可见性窗口 → update/delete 窗口性失败 → 数据分叉

**问题/为什么失败**：mem0 v2 的 `update`/`delete` 内部第一步都是
`vector_store.get(vector_id=…)`（Milvus pk 点查）。紧邻 `add` 之后调用时，
刚写入的行落在 growing segment 的读可见性窗口内（同 client 的 filter
查询已可见、pk 点查不可见），点查返回 None → `ValueError: Memory with
id … not found`。真实 Milvus 上直测：insert 后 0.0s pk 点查 MISS / filter
HIT，0.3s 后自愈；`add → sleep(3) → update` 稳定成功。
主链路命中点：`_index_l3_event` 物化本地索引后立刻提交
`_BackendUpdate`（槽位规范化改写 mem0 原文）与 supersede 清理
`_BackendDeleteSync`（同 slot 新记忆落地后删旧记忆）—— 正落在这个
窗口上。放任失败会让本地索引与向量库内容分叉（update）或在 Milvus
残留本该删除的记忆（delete），只能等 rebuild 收敛。

**mem0 行为变化**：v2 对不存在 id 的 delete 抛 `ValueError`（0.1.x 是
`list[0]` 的 `IndexError`）。原 delete 兼容逻辑只识别 IndexError，
v2 窗口性失败被当真实错误上抛。

**修复**（`mem0_library.py`）：
- `update()`：对 `_is_not_found_valueerror`（cause 是 ValueError 且消息
  同时含目标 memory_id 与 "not found"）按 0.5s/1s/2s 退避重试，预算
  耗尽后上抛（update 无幂等跳过语义）。
- `delete()`：同判定先重试；耗尽后与「已被并发删除」不可区分，按
  delete 幂等语义收敛为跳过（info 日志）；IndexError 兼容保留。
- `_call` 失败日志补充 `error_message`（截断 200 字符）—— 本轮排障
  正是因为只记 error_type 差点断线。

**真实验证**：服务日志出现 `delete target not visible yet; retrying` →
重试后 `call completed`；复测全量 append 链路 0 次
`write after index failed`。回归测试 5 个（update 重试收敛 / 不重试无关
错误 / 有界放弃；delete 窗口重试 / 幂等跳过 / 不重试无关错误）。

## W-2 (HIGH) 进程硬杀遗留孤儿 running 任务，重启后永远 running

**问题**：进程被 SIGKILL / OOM / 节点驱逐时，in-flight 任务停留在
`running` 且无人推进（实测遗留 3 条 row_version=0 的
`memory-extract:*`）。重启后 `GetTask` 对客户端永远返回 running →
无限轮询；无任何回收机制。
**边缘情况**：多副本滚动重启时不能误回收健康副本正在执行的任务。
**修复**：仓储新增 `reclaim_stale_running_tasks(max_age_seconds)`：
原子 `UPDATE … WHERE status='running' AND updated_at < cutoff` 集合级
条件回收（多副本并发执行天然幂等；健康任务随每次 `save_task` 刷新
`updated_at`，阈值 `task_orphan_running_seconds=1800` 显著大于 L3 抽取
分钟级时长，validator 强制 >= 60）。`MemoryService.reclaim_orphan_running_tasks`
吞错不阻塞启动；lifespan 在 Milvus 就绪后调用。
**真实验证**：重启后 3 条孤儿任务全部转为 `failed`，last_error 带
`reclaimed: orphaned running task …` 说明。回归测试 3 个（超龄回收 /
幂等 / 仓储失败不阻塞启动）。

## W-3（部署事项，未改代码）旧 collection 无 `text`/`sparse` 字段，BM25 混合检索未启用

P1b 升级的「混合检索」依赖 mem0 v3 collection schema（`text` 字段 +
BM25 sparse 向量）。现有 `agent_semantic_memory_v1` 是旧 schema（实测
`describe_collection` 仅 `id`/`vectors`/`metadata`），mem0 打 warning：
`BM25 keyword scoring will be disabled`。语义检索不受影响（全链路
recall 命中全部通过）。**启用需要新 collection**（mem0 提示 use a
fresh collection）：建议部署流程改为切换
`MEMORY_MILVUS_COLLECTION=agent_semantic_memory_v2` + 存量数据迁移
（mem0 add 重放或 Milvus dump/restore），列入发布计划决策。

## 已证伪 / 已覆盖项

1. **mem0 v2 `get_all` API 变更**（top-level `user_id` 被拒，须
   `filters=`）：P1b 的 `_scope_kwargs` 签名探测已兼容，真实链路
   delete_all 正常。
2. **mem0 v2 update 对「真不存在 id」的行为**：同 not found 消息，
   与窗口不可区分 —— update 侧选择重试后上抛（由 rebuild 兜底），
   delete 侧选择重试后幂等跳过（删除幂等语义）。
3. **历史 failed 任务**（`unhashable type: 'list'` 等 2 条）：R-6/R-7
   修复前的存量数据，非本轮回归。
4. **并发幂等**：8 线程同 request_id 真实并发 append → 恰好 1
   completed + 7 already_done，round 表单行，无 500（P1a 乐观锁 +
   幂等门真实生效）。

## 验证汇总

```text
单测: 522 passed（含本轮新增 8 个回归测试）/ ruff check+format / mypy strict 51 files 全绿
HTTP  全链路（含 L2 LLM 摘要新检查项）: 27/27（修复前脚本为 26 项，新增检查项后仍全过）
gRPC  全链路: 17/17
并发幂等: 8 并发 → 1 completed + 7 already_done，round 无重复
孤儿任务回收: 重启即回收（3/3）
update/delete 可见性窗口: 修复前每轮全链路 1~2 次 write after index failed；
  修复后最终复跑 0 次残余失败，踩窗口→退避重试→成功（日志可见
  "update/delete target not visible yet; retrying"）
real_mem0_pressure（5 轮 append→recall→delete→rebuild→final recall 7）: EXIT=0
```

**修复的实现要点**：除 backend 层重试外，`_BackendUpdate.__call__` 从
直接调 `backend._call("update", …)` 改为 `backend.update(…)` —— 前者会
绕过重试逻辑（第一版修复因此在真实链路上无效，被最终复跑抓回），
N-6 契约测试同步修订为「update 必须经 backend.update → _call」。
