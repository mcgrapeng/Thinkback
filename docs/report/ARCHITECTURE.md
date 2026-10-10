# thinkback 架构说明

| 元信息项 | 值 |
| --- | --- |
| 适用系统 | `thinkback` |
| 文档类型 | 干净架构重构后的权威架构描述 |
| 覆盖范围 | 分层架构 / 组件结构 / 数据流 / API 设计 / 数据库模式 / 扩展路径 |
| 关联文档 | [Thinkback记忆三层架构](../memory/Thinkback记忆三层架构.md)（领域口径）、[GRPC](GRPC.md) |

本文描述 2026-09 重构后的代码架构：领域策略下沉 `domain/`，
编排收敛 `memory/`，基础设施隔离 `infra/`，传输层薄化 `api/` + `rpc/`。
**短期记忆（L1/L2）只落 PostgreSQL，服务无 Redis / Celery 依赖。**

包布局：代码包为 `src/thinkback`（uv + PEP 621 + Python 3.12，
hatchling 构建）；部署标识（镜像名 / k8s 配置 / 数据库名 /
Milvus collection / `LONG_TERM_SCOPE_ID`）沿用 `thinkback`，
两者独立演进，重命名部署标识需配套 gitops 与数据迁移。

---

## 1. 架构总览

### 1.1 分层模型（依赖只允许向内）

```text
┌─────────────────────────────────────────────────────────────────┐
│  传输层 entrypoints                                              │
│  api/   FastAPI HTTP 路由（线程池桥接同步服务）                    │
│  rpc/   gRPC servicer（与 HTTP 共享同一 MemoryService 单例）      │
├─────────────────────────────────────────────────────────────────┤
│  应用层 memory/                                                  │
│  service.py     MemoryService 编排器（append/recall/delete/…）   │
│  schemas.py     API DTO（Pydantic，含校验器）                     │
│  caches.py      BoundedTTLCache（进程内读加速，非持久化）          │
│  repositories/  MemoryRepository 端口实现（in_memory/sqlalchemy） │
│  backends/      MemoryBackend 端口实现（fake/mem0_library）       │
├─────────────────────────────────────────────────────────────────┤
│  基础设施层 infra/                                               │
│  config.py  pydantic-settings 配置（生产禁 .env 兜底）            │
│  database/  async engine + ORM models + alembic migration        │
│  readiness.py  database/milvus/mem0 三探针聚合                    │
│  logging / structured_logging  loguru + trace_id + 指标           │
├─────────────────────────────────────────────────────────────────┤
│  领域层 domain/（零外部依赖，仅标准库）                            │
│  enums / entities   领域词汇与持久化无关实体                       │
│  keys / summarization  幂等指纹、L2 摘要策略                      │
│  ports              MemoryRepository / MemoryBackend / HistorySource │
│  safety             内容安全准入词表（write-gate）                 │
│  recall_policy      召回去重与 token 预算裁剪                      │
│  slots/             P0 槽位引擎（query_slots/extractors/canonical/support）│
└─────────────────────────────────────────────────────────────────┘
```

**依赖规则**：`api/rpc → memory → infra → domain`；`domain` 不 import
任何其他层；`api` 与 `rpc` 互相独立、只经 `MemoryService` 公开方法进入业务。

**端口与适配器**：业务只依赖 `domain/ports.py` 的三个 Protocol；
生产实现在 `memory/repositories`、`memory/backends`，测试注入
`InMemoryMemoryRepository` / `FakeMemoryBackend`。mem0 / Milvus SDK
只允许出现在 `memory/backends/mem0_library.py`（readiness 只读探针除外）。

### 1.2 运行拓扑（最小生产版本）

```text
                    ┌──────────────────────────────┐
  会话网关 ──HTTP──▶ │ thinkback API (FastAPI)  │──▶ PostgreSQL (L1/L2/索引/任务)
  (上游鉴权/限流)    │  + 内嵌 gRPC server           │──▶ Mem0 Library ─▶ Milvus (L3 向量)
                    │  + L3 后台线程池(async 模式)   │──▶ LLM endpoint（mem0 抽取）
                    └──────────────────────────────┘         Embedding endpoint
```

- 单进程承担 HTTP + gRPC + L3 后台抽取；无独立 worker（Celery 已移除）。
- 服务部署在 Thinkback VPC 内，进程内不做调用方鉴权（边界见三层架构文档 §0.1）。

---

## 2. 组件结构

```text
src/thinkback/
├── domain/                    # 纯领域层（仅标准库依赖）
│   ├── enums.py               # 10 个 StrEnum 状态机/词汇表
│   ├── entities.py            # JournalEntry/SummaryEntry/MemoryIndexEntry/TaskEntry
│   ├── keys.py                # scope_key/fingerprint/request_fingerprint/source_ref_key
│   ├── summarization.py       # L2 摘要：拼接降级 + LLM 结构化综合 prompt
│   ├── ports.py               # MemoryRepository/MemoryBackend/HistorySource 协议
│   ├── safety.py              # 受限内容正则词表（RESTRICTED_PATTERNS）
│   ├── recall_policy.py       # dedupe_and_clip（层优先 L3>L2>L1，len//2 token 估价）
│   └── slots/                 # P0 槽位引擎
│       ├── query_slots.py     # query_conflict_slot / context_terms / 宽泛请求识别
│       ├── extractors.py      # 8 个槽位抽取器（昵称/宠物/地点/工作/沟通/生日/饮食/睡眠）
│       ├── canonical.py       # memory_conflict_slot / canonical_memory_text*
│       └── support.py         # 源文本交叉校验 / round_order 游标 / 本地索引 ID 识别
├── memory/                    # 应用层
│   ├── service.py             # MemoryService（用例编排器：append/recall/delete/rebuild 状态机、锁与缓存边界）
│   ├── schemas.py             # API DTO + 校验器（re-export domain.enums）
│   ├── caches.py              # BoundedTTLCache（cachetools.TTLCache 适配层：TTL + 容量淘汰）
│   ├── l3.py                  # L3WriteExecutor（后台写基础设施：线程池生命周期/容量背压/future 记账）
│   ├── l2_refresh.py          # L2BackgroundRefresher（LLM 综合摘要后台去抖刷新）
│   ├── decay.py               # MemoryDecaySweeper（遗忘 decay 清扫，默认关）
│   ├── repositories/
│   │   ├── _l1_cache.py       # L1 进程内缓存 mixin（N-5 锁保护）
│   │   ├── in_memory.py       # 测试/开发实现
│   │   └── sqlalchemy.py      # 生产实现（独立事件循环线程桥接 async PG）
│   └── backends/
│       ├── fake.py            # 测试后端（对齐 _call 异常包装语义）
│       └── mem0_library.py    # 生产后端（唯一接触 mem0/Milvus 的模块）
├── infra/
│   ├── config.py              # Settings（分组校验；生产禁 .env）
│   ├── database/{engine,models,base,migration}.py
│   ├── readiness.py           # database/milvus/mem0 并发探针（单探针超时收敛为 not_ready）
│   ├── milvus.py              # 启动期 lazy CREATE DATABASE
│   ├── logging.py / structured_logging.py
├── api/                       # HTTP：app(工厂+lifespan) / health / memory / dependencies
└── rpc/                       # gRPC：server / servicer / converters / pb2 生成物
```

### 2.1 关键组件职责

| 组件 | 职责 | 不负责 |
| --- | --- | --- |
| `MemoryService` | 用例编排：幂等、任务状态机、锁边界、L3 写工作流、L2 后台刷新触发、墓碑与重建 | 文本策略（已下沉 domain）、SQL、mem0 调用细节 |
| `L3WriteExecutor` | L3 后台写基础设施：executor 生命周期（自建/注入）、容量背压（信号量+槽位计数）、write/cleanup future 记账与 drain | 业务语义（任务状态流转、L3 写流程回调在 service） |
| `L2BackgroundRefresher` | L2 LLM 综合摘要后台刷新：去抖计数、在飞去重、失败降级保留拼接版 | LLM 调用细节（composer 注入） |
| `MemoryDecaySweeper` | 遗忘 decay（P2#7，默认关）：老+久未召回的长尾记忆置 SUPPRESSED；槽位/墓碑保护、召回强化、进程级时间门控 | 记忆价值判断（无 LLM，确定性政策） |
| `SqlAlchemyMemoryRepository` | L1/L2/索引/任务的 PG 持久化；同步外观 + 内部事件循环线程 | 业务判断（指纹冲突语义由 service 决定） |
| `Mem0LibraryMemoryBackend` | mem0 唯一入口：懒构造、并发信号量、异常统一包装、内联线程池补丁 | 本地索引与治理（service 负责） |
| `domain/slots` | 槽位识别/抽取/归一/源校验（纯函数） | 任何 I/O |
| `BoundedTTLCache` | 读路径 TTL 缓存（丢失不影响正确性） | 持久化 |
| `api/memory.py` | 线程池 + 信号量桥接同步服务；异常→HTTP 状态码映射 | 业务逻辑 |

### 2.2 并发模型（单进程内三套预算）

| 池/信号量 | 配置 | 保护对象 |
| --- | --- | --- |
| API 读/写线程池 | `MEMORY_API_WORKER_LIMIT`(8) × 2，等待 `MEMORY_API_WORKER_WAIT_SECONDS`(5s) | 同步 MemoryService 调用不阻塞事件循环 |
| L3 后台执行器（`memory/l3.py` `L3WriteExecutor`） | `MEMORY_L3_EXECUTOR_WORKERS`(16) + `MEMORY_L3_MAX_PENDING_TASKS`(256) 信号量背压 | async 模式的 mem0 抽取 |
| mem0 调用信号量 | `MEMORY_BACKEND_MAX_CONCURRENT_CALLS`(4) | LLM/Embedding/Milvus 压力 |
| 仓储事件循环 | 单线程 loop，单操作超时 30s | DB 连接（pool 5+5） |

---

## 3. 数据流

### 3.1 写入 append（async 模式主链路）

```text
POST /memory/append
  → schemas 校验（完整 user→assistant 轮次、id≤128、metadata≤64KB）
  → get_round(round_id) 幂等锚点
      ├─ 存在且指纹不同 → 409 conflict
      └─ 存在且任务 PENDING/RUNNING/COMPLETED → already_done
  → domain.safety 门禁（命中 → 整轮跳过 L1/L2/L3，只落 round+tombstone）
  → reserve L3 slot（队列满 → 503 背压）
  → save_task(RUNNING) + save_round（ins_summary_round_journal）
  → 同步刷新 L1 缓存 + L2 拼接占位摘要（已有 LLM 综合版时仓储层跳过覆盖）
  → L2 去抖触发后台 LLM 综合摘要刷新（首轮即刷，其后每 N 轮；失败保留拼接版）
  → submit L3 后台任务 → 返回 completed + l3_events=DEFERRED
  …… 后台 ……
  → backend.add（mem0：LLM 抽取 → embedding → Milvus 写入）
  → 逐事件：domain.slots.memory_supported_by_source 准入校验
      → 过：写 ins_memory 业务索引（ACTIVE），同槽位冲突记忆置 SUPERSEDED
      → 不过（白名单外/源校验失败）：mask 回滚 delete_many
  → P0 槽位回填（local-p0: 合成 ID，service _backfill_p0_slots_from_round_source）
  → save_task(COMPLETED)；失败则 FAILED 并释放 slot
```

### 3.2 召回 recall

```text
POST /memory/recall
  → intent=SENSITIVE → fail-closed（ValueError → 403）
  → L1：进程内缓存（miss 时读 journal 派生）
  → L2：get_summary（kind=llm 综合版优先，concat 为降级）；DIRTY/REBUILDING 跳过，STALE 带 degradation_reasons
  → L3：chat 意图 → backend.search（异常降级为只返回 L1/L2，服务可用性优先）
       + 本地索引 ACTIVE 过滤 + 槽位回填 + memory_context_allowed 上下文门控
  → domain.recall_policy.dedupe_and_clip（去重 + token 预算裁剪）
```

### 3.3 删除 delete（MEMORY/SESSION/ALL 三种作用域）

```text
POST /memory/delete
  → claim_task（operation_id 幂等）→ 已存在直接回放既有响应
  → 本地索引置 DELETED + backend.delete（H-3：锁内收集、锁外执行）
  → 轮次置 deleted_tombstone + 合成 delete-all:/delete-session: 墓碑行
  → L1 清缓存 + L2 置 DIRTY（防摘要携带已删事实）
  → rebuild 输入通过 excluded_source_refs 排除 DELETED/SUPERSEDED 来源
```

### 3.4 重建 rebuild

外部历史源（HistorySource 端口）版本一致性校验 → 清理来源已删的 L3
（置 SUPERSEDED → 删除）→ 对覆盖不足的回合重放 L3 抽取 → 重建 L2。

---

## 4. API 设计

### 4.1 HTTP（`/memory` 前缀，均不走进程内鉴权）

| 方法 | 路径 | 请求 | 成功 | 主要错误 |
| --- | --- | --- | --- | --- |
| POST | `/memory/append` | `AppendMemoryRequest` | 200 `AppendMemoryResponse` | 400 校验 / 409 round_id 冲突 / 403 安全门禁 / 503 队列满或超时 |
| POST | `/memory/recall` | `RecallMemoryRequest` | 200 `RecallMemoryResponse`（含 degraded/degradation_reasons） | 403 sensitive fail-closed / 400 |
| POST | `/memory/delete` | `DeleteMemoryRequest` | 200 `DeleteMemoryResponse` | 400 / 404 / 409 dead_letter |
| POST | `/memory/update` | `UpdateMemoryRequest` | 200 `UpdateMemoryResponse` | 400 / 403 受限内容 / 404 / 409 |
| GET | `/memory/items?user_id=&include_deleted=` | — | 200 `ListMemoriesResponse` | — |
| GET | `/memory/items/{memory_id}?user_id=` | — | 200 `GetMemoryResponse` | 404 |
| GET | `/memory/tasks/{task_id}` | — | 200 `TaskResponse` | 404 |
| GET | `/memory/l3/background-status` | — | 200 `L3BackgroundStatusResponse` | — |

健康：`GET /health`、`/health/live`、`/health/ready`（503 摘流）。

**异常→状态码映射**（api/memory.py 统一映射，rpc/servicer.py 保持同义 gRPC 码）：

| 服务层异常（消息子串） | HTTP | gRPC |
| --- | --- | --- |
| `ValueError("fail-closed …")` | 403 | PERMISSION_DENIED |
| `ValueError("… conflict …")` | 409 | ABORTED |
| `ValueError("… not found …")` | 404 | NOT_FOUND |
| `ValueError`（其他） | 400 | INVALID_ARGUMENT |
| `RuntimeError("dead_letter …")` | 409 | INTERNAL |
| `RuntimeError("l3 background queue full"/"timed out")` | 503 | UNAVAILABLE |
| `RuntimeError("mem0 library …"/"memory backend …")` | 502 | INTERNAL |

### 4.2 gRPC（`proto/memory.proto`，默认 50052）

`Append / Recall / Delete / ListMemories / GetMemory / UpdateMemory /
GetTask / GetL3BackgroundStatus`，server reflection 已启用；
与 HTTP 共用同一 `MemoryService` 单例（并发预算独立，见 §2.2）。

### 4.3 幂等契约

- `append`：`round_id` 为幂等锚点（指纹不同 = 冲突 409，相同 = already_done）。
- `delete` / `update` / `rebuild`：`operation_id` 幂等，重复提交回放任务结果。
- 503 后重试必须复用原 `round_id` / `operation_id`。

---

## 5. 数据库模式（PostgreSQL，Alembic 管理）

| 表 | 用途 | 关键列 | 约束/索引 |
| --- | --- | --- | --- |
| `ins_summary_round_journal` | L1 可靠轮次流水（append 唯一可信源） | round_id, messages(JSON), round_fingerprint, round_state, source_timestamp, round_index | **round_id 唯一索引**；(user_id, memory_scope_id, session_id) 索引 |
| `ins_current_summary` | L2 每作用域当前摘要 | summary_text, summary_state, summary_cursor_round, latest_source_* | **(user_id, memory_scope_id) 唯一** |
| `ins_memory` | L3 业务索引（非向量库） | backend_memory_id, source_refs(JSON), memory_status, memory_metadata(JSON), **valid_at/invalid_at**（P2#6 双时态：失效时刻随 supersede/delete 幂等落盘，召回端同槽位新事实优先）, expires_at(保留未启用) | **部分唯一索引** (user, scope, backend_id) WHERE status='ACTIVE'；backend_memory_id 索引 |
| `ins_memory_task` | 后台任务/幂等/重试 | task_id PK, operation_id, status, retry_count, result(JSON) | operation_id 索引（非唯一）；status 索引 |

状态机：round `active|pending_append|deleted_tombstone`；summary
`active|stale|dirty|rebuilding`；memory `ACTIVE|DELETED|SUPERSEDED|SUPPRESSED(未启用)`；
task `pending|running|completed|failed|dead_letter`（重试上限 5 → dead_letter）。

L3 向量数据在 Milvus（collection=thinkback，COSINE，1024 维），
由 mem0 独占读写；PG 只存治理索引，召回过滤不得绕过 mem0 直查向量库。

---

## 6. 可扩展性设计

### 6.1 当前最小生产版本的边界（明确不做）

- 无强一致写栅栏 / CAS 墓碑 / 人工治理台 / 衰减任务（三层架构文档 §0）。
- ~~`ins_memory_task` 乐观并发未做版本列~~ **已落地（P1a，2026-09）**：
  `row_version` CAS 写入（`UPDATE ... WHERE row_version=?`），冲突抛
  `TaskStaleWriteError`（HTTP 409 / gRPC ABORTED）；后台计数类变更经
  `_mutate_task_with_retry` 重读重放收敛（进程内锁 + 跨副本 CAS 双保险）。
  残留：前台用例路径（append 预算/delete/update/rebuild 终态写）遇冲突
  以 409 上抛、由客户端幂等重试收敛，未做自动重放。
- 孤儿 RUNNING 任务双层回收（2026-09 关机竞态堵死）：关机竞态 / SIGKILL
  会让 L3 后台线程的终态写丢失，任务停留 RUNNING。①启动回收
  （`reclaim_orphan_running_tasks`，set 级条件更新）；②读路径自愈
  （`get_task` 读到超龄 RUNNING 时单任务原子回收，健康任务 no-op）——
  幽灵任务的存活期从「依赖下一次启动」收敛为「≤ task_orphan_running_seconds
  （默认 1800s）且被读取即自愈」，两层共用同一阈值与幂等语义。
  已知边界：终态写丢失本身仍会发生（进程死亡时无从写库），仅保证
  客户端可见状态收敛；跨重启续跑未完成的工作属 DB 任务队列范畴，未做。
- L1 读路径为 "进程缓存优先，miss 回源 PG journal"（`get_l1` 回源最近
  10 个 active 轮次）：跨副本 append/recall 与进程重启场景已正确。
  残留边界：**删除操作只失效本副本缓存**，其他副本若曾缓存过该会话，
  会继续返回已删轮次直至缓存被淘汰 —— 需要跨副本失效或读穿透时再收紧。
- L2 为拼接式摘要（V1 占位），LLM 语义压缩在 V2 范围（替换
  `domain/summarization.py` 即可，接口不变）。
- `MEMORY_P0_SLOTS` 白名单外的记忆会写入 mem0 但**不进入 /recall 结果**
  （recall 只返回本地索引 ACTIVE 行），属向量库中的不可召回存量数据；
  彻底不放行应在准入校验处拒绝（详见配置注释）。

### 6.2 扩展路径（按优先级）

1. **多副本水平扩展**：`ins_memory_task` 加 `version` 列做乐观锁；
   L3 后台任务改为 DB 队列抢占（claim 语义已有雏形）替代进程内 executor。
2. **读扩展**：recall 的 L1/L2 读可走 PG 只读副本；`BoundedTTLCache`
   已隔离缓存语义，切换数据源不动业务层。
3. **L2 语义化**：`domain/summarization.summarize_rounds` 换 LLM 实现，
   召回门禁（DIRTY 跳过）不变。
4. **槽位引擎演进**：`domain/slots` 为纯函数，可整体替换为模型抽取，
   冲突/校验口径（`memory_conflict_slot`）保持不变。
5. **分区**：journal 按月分区（source_timestamp）+ 任务表归档，
   当前数据量级（P0）无需提前做。

### 6.3 失效语义（生产 SLO 口径）

- PG 不可用：readiness 503 摘流；在途写操作 30s 超时收敛为 503。
- mem0/Milvus 不可用：async 模式 append 仍成功（L1/L2 完整），
  L3 任务 FAILED 可重试；recall 降级返回 L1/L2。
- LLM 凭据缺失：生产 readiness 直接 not_ready（fail-visible）。
