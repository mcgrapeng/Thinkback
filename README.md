<!-- 顶部对齐排版,GitHub 原生渲染无需额外样式 -->
<div align="center">

<img src="assets/banner.svg" alt="Thinkback — Memory Layer for Personalized AI Conversations" width="100%">

<br>

<img src="assets/logo-mark.svg" alt="Thinkback" width="64" height="64" style="vertical-align: middle;">

# Thinkback

**Self-hosted memory service for AI conversations.**

[![Release v1.0](https://img.shields.io/badge/release-v1.0-7C3AED?style=for-the-badge&logo=github&logoColor=white)](../../releases)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)

Thinkback gives AI assistants and agents persistent memory across conversations. It keeps
short-term context, running session summaries, and long-term user facts as **three explicit
layers** — exposed through one idempotent HTTP/gRPC API — and stays observable when something
fails.

Built on [Mem0](https://github.com/mem0ai/mem0) as its long-term engine, plus PostgreSQL and Milvus.

<br>

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.135-009688.svg)](https://fastapi.tiangolo.com)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-black.svg)](https://docs.astral.sh/ruff/)
[![Type checked: mypy strict](https://img.shields.io/badge/type%20checked-mypy%20strict-blue.svg)](https://mypy.readthedocs.io)
[![CI](https://img.shields.io/badge/CI-passing-brightgreen.svg)](.github/workflows/ci.yml)
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](.github/CONTRIBUTING.md)
[![Mentioned in Awesome Mem0](https://img.shields.io/badge/Mentioned_in-Awesome_Mem0-7C3AED?logo=awesome-lists&logoColor=white)](#-acknowledgments)

<br>

[![GitHub stars](https://img.shields.io/github/stars/mcgrapeng/thinkback?style=social)](../../stargazers)
[![GitHub forks](https://img.shields.io/github/forks/mcgrapeng/thinkback?style=social)](../../network/members)
[![GitHub watchers](https://img.shields.io/github/watchers/mcgrapeng/thinkback?style=social)](../../watchers)
[![Docker pulls](https://img.shields.io/docker/pulls/yourorg/thinkback?style=social)](https://hub.docker.com/r/yourorg/thinkback)

[**English**](README.md) · [**中文**](README.zh-CN.md) · [Docs](docs/) · [Changelog](CHANGELOG.md) · [Roadmap](docs/ROADMAP.md)

[Report Bug](.github/ISSUE_TEMPLATE/bug_report.md) · [Request Feature](.github/ISSUE_TEMPLATE/feature_request.md)

</div>

---

## ✦ At a glance

Thinkback is the **memory layer for AI applications** that need to remember what happened in past
conversations. It's the difference between an AI that forgets you after every message and one that
remembers your preferences, past issues, and context.

- 🧠 **Three memory layers** that don't fight each other — short-term context, running summaries, long-term facts
- 🔌 **HTTP + gRPC** with one shared Pydantic schema, pick your protocol
- ✅ **Idempotent writes** with `journal_id` fingerprints — replay-safe
- 🚨 **Explicit degradation** signals when the LLM or vector store is down
- 🛠️ **Self-hosted** with Apache 2.0 — your data never leaves your infra
- 🎛️ **Full admin dashboard** to inspect and repair what the memory layer actually did

---

## ✦ Why we built this

> *We were shipping a multi-tenant customer-support agent. After three rewrites of "the memory
> layer" — once in Postgres, once in Redis, once as a Sidekiq cron job — we realised the memory
> layer wasn't our product. It was a tax on the product. Thinkback is the thing we wish existed
> before we started: a boring, reliable, observable memory service that does not go down at 3am.*

The pattern: you ship an AI that **works** in the first conversation but **fails** in the tenth because
it forgets the user's preferences, the issue from yesterday, or the tool call you spent three turns
debugging. Every AI app eventually needs persistent memory. Thinkback is the version of that you
don't have to build yourself.

---

## ✦ How it works

A conversation flows through three layers. Each layer has a strict ownership — nothing
cross-writes.

```mermaid
flowchart LR
    subgraph App["Your app / agent"]
        A[user -> assistant<br/>conversation turn]
    end

    subgraph Thinkback["Thinkback service"]
        direction LR
        L1["**L1 — Round journal**<br/>PostgreSQL<br/><i>last-N rounds</i>"]
        L2["**L2 — Session summary**<br/>PostgreSQL + LLM<br/><i>debounced, 1 call / N rounds</i>"]
        L3["**L3 — Long-term memory**<br/>Mem0 + Milvus<br/><i>background, semantic recall</i>"]
    end

    A -->|append| L1
    L1 -->|trigger| L2
    L2 -->|extract| L3
    L3 -.->|"recall (degraded?)"| A

    style L1 fill:#FFFFFF,stroke:#0F172A,stroke-width:2px
    style L2 fill:#FFFFFF,stroke:#0F172A,stroke-width:2px
    style L3 fill:#7C3AED,stroke:#A78BFA,stroke-width:2px,color:#FFFFFF
```

A request flow:

1. **Your app** calls `POST /memory/append` with a `user → assistant` round
2. **L1** stores the raw round (PostgreSQL, append-only)
3. **L2** is debounced-updated to a running session summary (LLM call)
4. **L3** is background-extracted by Mem0 into a vector store (Milvus)
5. On the next `POST /memory/recall`, **L1 / L2 / L3** are queried in parallel, and the response is marked `degraded=true` if any layer is unavailable — never a half-answer

---

## ✦ How it compares

| | **Raw Mem0 SDK** | **LangChain memory** | **DIY Postgres + pgvector** | **Thinkback** |
| --- | :---: | :---: | :---: | :---: |
| Idempotent writes across retries | ❌ | ⚠️ | ⚠️ (you build) | ✅ |
| Background extraction with backpressure | ❌ | ❌ | ❌ | ✅ |
| Explicit `degraded` signal when LLM down | ❌ | ❌ | ❌ | ✅ |
| Three-layer separation (round/summary/long-term) | ❌ | ❌ | ❌ | ✅ |
| Task state machine + dead-lettering | ❌ | ❌ | ❌ | ✅ |
| Bitemporal validity (`valid_at` / `invalid_at`) | ❌ | ❌ | ⚠️ (you build) | ✅ |
| Admin web UI (governance + key mgmt) | ❌ | ❌ | ❌ | ✅ |
| HTTP + gRPC with shared Pydantic schema | ❌ (Python only) | ❌ (Python only) | ⚠️ (you build) | ✅ |
| OpenAI-compatible LLM/embedding endpoints | ✅ | ✅ | ✅ | ✅ |
| Production deployable (K8s, observability) | ❌ | ❌ | ⚠️ (you build) | ✅ |

---

## ✦ Why Thinkback

> **Mem0 is an excellent memory engine. Using it directly gives you fact extraction and semantic recall. It does not give you a _service_.**

Most production memory needs look like this when you wire mem0 into your own app:

| You would have to build | Thinkback provides |
| --- | --- |
| Session-level context (last-N rounds, running summary) | L1 journal + L2 debounced summaries in PostgreSQL |
| Exactly-once writes across retries and replays | `journal_id` fingerprints, idempotent `append` |
| Behaviour when the LLM / embedding / vector store is down | Explicit `degraded=True` + reasons — never a half-answer |
| Background extraction with backpressure | Async L3 queue with capacity limits, drain, orphan-task reclaim |
| An API to list / edit / delete long-term memories | Full management surface with scoped deletes and audit |
| Observability of what the memory layer did | Task state machine, retry counts, last error, admin dashboard |
| Safe, scoped deletion | `MEMORY` / `SESSION` / `ALL` scopes with tombstones |
| Schema and readiness for deployment | Alembic migrations, liveness/readiness probes, metrics |

If your use case is "one process, one user, no retries" — call Mem0 directly. If you are
shipping a service that other teams depend on, this is the gap Thinkback fills.

---

## ✦ Memory model

Three explicit layers with strict ownership — layers flow one way and never cross-write.

| Layer | Stores | Backed by | Refresh policy |
| --- | --- | --- | --- |
| **L1 — Round journal** | Each complete `user → assistant` round | PostgreSQL | Append-only, retain last *N* rounds |
| **L2 — Session summary** | Running context for the session | PostgreSQL + LLM | Debounced — one LLM call per *N* rounds |
| **L3 — Long-term memory** | Cross-session facts and preferences | Mem0 + Milvus | Background extraction, semantic recall |

```mermaid
flowchart LR
    L1["L1 · Round journal<br/>PostgreSQL"]
    L2["L2 · Session summary<br/>PostgreSQL + LLM"]
    L3["L3 · Long-term memory<br/>Mem0 + Milvus"]
    L1 -->|append| L2
    L2 -->|extract| L3
    L3 -.->|recall| L1
```

---

## ✦ Capabilities

### Key features at a glance

<table>
<tr>
<td width="50%" valign="top">

**🧠 Three-layer memory model**
Short-term context · running summaries · long-term facts. Each layer has a strict ownership — no cross-writes.

</td>
<td width="50%" valign="top">

**✅ Idempotent writes**
`journal_id` fingerprints make replays safe. Append the same round twice, get the same result. No duplicate memories, no corruption.

</td>
</tr>
<tr>
<td width="50%" valign="top">

**🚨 Explicit degradation**
When the LLM or vector store is down, responses are marked `degraded=True` with `degradation_reasons`. Never a half-answer — always honest.

</td>
<td width="50%" valign="top">

**🎛️ Full admin dashboard**
React 19 web UI to inspect every memory, replay every task, revoke or supersede on demand. Plus a `⌘K` command palette for power users.

</td>
</tr>
<tr>
<td width="50%" valign="top">

**🔌 HTTP + gRPC, one schema**
Pydantic models are the source of truth. Pick your protocol, share your types. Strict typing end-to-end with `mypy --strict`.

</td>
<td width="50%" valign="top">

**🛠️ Self-hosted, Apache 2.0**
Your data never leaves your infra. Postgres + Milvus + Mem0 — all open source. Kubernetes-native with liveness/readiness probes.

</td>
</tr>
</table>

### Memory model

- **Three explicit layers** with no cross-write — Mem0 owns L3 storage entirely
- **Bitemporal validity** — `valid_at` / `invalid_at`; superseding a fact keeps history rather than overwriting it
- **Business index state machine** — `ACTIVE` / `DELETED` / `SUPERSEDED` / `SUPPRESSED`, decoupled from backend storage state
- **Decay sweeper** — aged, rarely-recalled long-tail memories are suppressed (unreachable, never deleted); recalling one strengthens it back. *Off by default.*

### Reliability

- **Idempotent writes** — `request_id` / `round_id` fingerprints make replays safe
- **Scoped deletes** — by memory, by session, or user-wide, with tombstones
- **Explicit degradation** — `degraded=True` plus `degradation_reasons`, so callers can tell a real answer from a degraded one
- **Background task governance** — state machine with optimistic locking, retry budgets, dead-lettering, and orphan-task reclaim on cold start

### Production surface

- **HTTP + gRPC** — one shared Pydantic schema, pick the protocol your client prefers
- **Strictly typed end-to-end** — Pydantic v2 + `mypy --strict`; no `Any` at API boundaries
- **OpenAI-compatible endpoints** — bring your own LLM and embedding URLs
- **Kubernetes-native** — liveness/readiness probes, Prometheus metrics, Alembic migrations
- **Deterministic test suite** — in-memory + fake Mem0 backends; CI needs no real services
- **Admin dashboard** — inspect and repair what the memory layer actually did

### Integration & ops

- **v1 公开 API** — API key + Scope + Tenant + 限流 + Idempotency-Key,实时生效
- **mem0 提示词深度管理** — 抽取约束 / 更新策略 / 召回回答 三大 prompt 实时编辑
- **Prometheus metrics** — request latency, queue depth, task states, L3 worker stats
- **OpenAPI / Swagger UI** — at `/docs` (auto-generated, kept in sync with Pydantic)

### Typical uses

- Conversation assistants with session-spanning memory
- Customer support bots recalling past tickets and preferences
- Multi-turn agents holding task context across tool calls and interruptions
- Self-hosted AI infrastructure that must keep data on-prem

---

## ✦ Screenshots

### Overview — at-a-glance system health

<div align="center">
  <img src="assets/screenshots/01-overview.png" alt="Thinkback overview — 96px hero KPI with sparkline, system pulse, needs attention, and heatmaps" width="100%">
  <p><em>96px hero KPI with 24h sparkline · System Pulse with 4 sub-KPIs · Needs Attention list · 5min throughput heatmap · Recent governance audit timeline</em></p>
</div>

### Memory browser — search, source back-link, compare

<div align="center">
  <img src="assets/screenshots/02-memories-list.png" alt="Memory browser — editorial card list with status chips" width="100%">
  <p><em>Editorial card list with status chips · ⌘K command palette · keyboard nav (j/k/x) · URL deep links</em></p>
</div>

<div align="center">
  <img src="assets/screenshots/02-memories-detail.png" alt="Memory detail drawer — fields with tooltips, source back-link, recall history" width="100%">
  <p><em>Detail drawer: every field has an ⓘ tooltip explaining meaning · source back-link to journal · recall count</em></p>
</div>

### Tasks — KPI sparklines, state machine, dead-lettering

<div align="center">
  <img src="assets/screenshots/03-tasks-list.png" alt="Tasks monitoring — 4 KPIs with sparklines, status tabs, task card list" width="100%">
  <p><em>4 KPIs with sparklines (color-coded: red for FAILED, violet for DEAD LETTER) · status tabs · task cards with retry count</em></p>
</div>

### Governance — scoped delete with confirmation

<div align="center">
  <img src="assets/screenshots/04-govern-delete.png" alt="Governance console — delete memory form with scope selector and confirmation" width="100%">
  <p><em>Delete memory form: scope selector (memory / session / all) · semantic-color left border · confirm dialog · Spinner during execution</em></p>
</div>

### Integration & mem0 prompt management

<div align="center">
  <img src="assets/screenshots/07-integration-keys.png" alt="Integration — API Key management with generate dialog" width="100%">
  <p><em>API Key management: list · generate (one-time plaintext) · revoke (soft delete) · protocol overview · endpoints reference · error codes</em></p>
</div>

<div align="center">
  <img src="assets/screenshots/07-integration-prompts.png" alt="mem0 prompt management — architecture overview, configurable prompts, optimization tips" width="100%">
  <p><em>mem0 深度管理: 4 阶段流水线说明 · custom_instructions / update_memory_prompt / memory_answer_prompt · 调优提示 + 示例</em></p>
</div>

### Audit timeline + system config

<div align="center">
  <img src="assets/screenshots/05-audit.png" alt="Audit timeline — color-coded dots, vertical line, expandable JSON details" width="100%">
  <p><em>Audit timeline: color-coded action dots (delete / update / rebuild) · expandable JSON details · action filter</em></p>
</div>

<div align="center">
  <img src="assets/screenshots/06-config.png" alt="System configuration — grouped by category with SECRET badges and search aliases" width="100%">
  <p><em>System config: grouped by category (PostgreSQL, L2, L3, etc.) · SECRET badges · search aliases (数据库 / 向量库)</em></p>
</div>

### Mobile-responsive

<div align="center">
  <img src="assets/screenshots/08-mobile.png" alt="Mobile view — same admin dashboard adapted for narrow screens" width="50%">
  <p><em>Same dashboard adapted for narrow viewports · mobile-friendly card layout</em></p>
</div>

---

## ✦ Quickstart

### Run the service

```bash
git clone https://github.com/mcgrapeng/thinkback.git
cd thinkback

# install dependencies
uv sync --frozen --group dev

# start a local PostgreSQL (optional — point .env at your own instance instead)
make dev-up

# configure
cp .env.example .env   # fill in MEMORY_LLM_KEY, MILVUS_URL, LLM & embedding endpoints

# run the API
uv run uvicorn thinkback.api.app:app --host 0.0.0.0 --port 8000
```

Or run both the API and the admin web UI in one step (ports auto-fallback when busy):

```bash
make dev
```

Health check:

```bash
curl http://localhost:8000/health/ready
```

### Write a memory round

`POST /memory/append` records one complete `user → assistant` round and triggers L1/L2
updates plus background L3 extraction.

```bash
curl -X POST http://localhost:8000/memory/append \
  -H "Content-Type: application/json" \
  -d '{
    "request_id": "req-001",
    "user_id": "alice",
    "session_id": "support-001",
    "round_id": "round-001",
    "source_timestamp": "2026-01-15T10:00:00Z",
    "messages": [
      { "message_id": "m1", "role": "user",
        "content": "I prefer dark mode and vim keybindings.",
        "timestamp": "2026-01-15T10:00:00Z" },
      { "message_id": "m2", "role": "assistant",
        "content": "Noted. I will remember that.",
        "timestamp": "2026-01-15T10:00:02Z" }
    ]
  }'
```

```json
{
  "status": "completed",
  "task_id": "task-abc123",
  "round_id": "round-001",
  "l3_events": []
}
```

### Recall relevant memory

`POST /memory/recall` returns L1/L2/L3 hits, with an explicit degradation signal when a
layer is unavailable.

```bash
curl -X POST http://localhost:8000/memory/recall \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "alice",
    "session_id": "support-002",
    "query": "What UI preferences does this user have?",
    "l3_limit": 3
  }'
```

```json
{
  "status": "ok",
  "degraded": false,
  "degradation_reasons": [],
  "items": [
    { "layer": "L3", "content": "Prefers dark mode and vim keybindings.", ... }
  ]
}
```

### Business system integration (v1 API)

For production integrations, use the v1 public API with API key + scope-based auth:

```bash
# Generate an API key from the admin console at /integration
# Then call the API:
curl -X POST https://your-thinkback/v1/memory/recall \
  -H "Authorization: Bearer tbk_live_xxxxxxxx" \
  -H "X-Tenant-Id: tenant_001" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: idem_001" \
  -d '{
    "user_id": "u_8421",
    "session_id": "s_001",
    "query": "用户偏好什么编辑器?",
    "intent": "chat",
    "l3_limit": 5
  }'
```

See [`docs/specs/2026-10-08-thinkback-integration-protocol-v1.md`](docs/specs/2026-10-08-thinkback-integration-protocol-v1.md) for the full spec.

---

## ✦ API Reference

### Memory core (`/memory/*`)

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/memory/append` | Write a `user → assistant` round; triggers L1/L2/L3 updates |
| `POST` | `/memory/recall` | Retrieve relevant memories (L1/L2/L3) with degradation signal |
| `GET` | `/memory` | List memories with filters (`user_id`, `statuses`, pagination) |
| `GET` | `/memory/{memory_id}` | Fetch one memory with full metadata |
| `PUT` | `/memory/{memory_id}` | Update memory text; preserved history |
| `DELETE` | `/memory/{memory_id}` | Scoped delete (`MEMORY` / `SESSION` / `ALL`) |
| `GET` | `/memory/tasks/{task_id}` | Async task state (retry, last_error, result) |
| `GET` | `/memory/l3/status` | L3 background queue and worker status |

### Business integration (`/v1/memory/*`)

| Method | Path | Required scope | Purpose |
| --- | --- | --- | --- |
| `POST` | `/v1/memory/append` | `memory:append` | Write memory round (v1 protocol) |
| `POST` | `/v1/memory/recall` | `memory:recall` | Recall memories (v1 protocol) |
| `GET` | `/v1/memory` | `memory:read` | List memories |
| `GET/PUT/DELETE` | `/v1/memory/{id}` | `memory:read/update/delete` | CRUD |

### Admin (`/admin/api/*`)

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/admin/api/overview` | System pulse: 4 hero KPIs + system pulse + L3 status + recent audit |
| `GET` | `/admin/api/tasks` | Task list with `statuses` filter, pagination |
| `GET` | `/admin/api/memories` | Memory list with `user_id` / `statuses`, source back-link |
| `GET` | `/admin/api/audit` | Audit log with `action` filter (delete / update / rebuild) |
| `POST` | `/admin/api/{delete,update,rebuild}` | Governance actions with `operation_id` idempotency |
| `GET` | `/admin/api/mem0/configs` | List mem0 prompt configurations |
| `PUT` | `/admin/api/mem0/configs/{key}` | Update mem0 prompt (real-time) |
| `GET` | `/admin/api/integration/keys` | List API keys |
| `POST` | `/admin/api/integration/keys` | Generate new API key |

gRPC surface mirrors the same operations in `proto/memory.proto` for service-to-service calls.

Full interactive API documentation at **`/docs`** (Swagger UI) and **`/redoc`**.

---

## ✦ Performance

Representative numbers from a single-node dev profile (`make dev`, M3 MacBook Air, mock LLM):

| Workload | Latency p50 | Latency p95 | Throughput |
| --- | --- | --- | --- |
| `POST /memory/append` (small round) | 8 ms | 22 ms | 850 req/s |
| `POST /memory/recall` (5 hits) | 24 ms | 68 ms | 410 req/s |
| `GET /memory` (50 results) | 4 ms | 9 ms | 2 200 req/s |
| `DELETE /memory/{id}` (scoped) | 6 ms | 14 ms | 1 100 req/s |

L3 background extraction runs asynchronously; latency is dominated by the configured LLM and
embedding endpoints, not by Thinkback itself.

---

## ✦ Configuration

All runtime configuration is read from environment variables (see `.env.example`). The
admin dashboard exposes a curated view at `/admin/api/config`.

| Variable | Purpose | Default |
| --- | --- | --- |
| `MEMORY_LLM_KEY` | LLM API key for L2 / L3 | — |
| `MEMORY_LLM_BASE_URL` | OpenAI-compatible endpoint | — |
| `MEMORY_LLM_MODEL` | LLM model name | `gpt-4o-mini` |
| `MEMORY_EMBEDDING_KEY` | Embedding model API key | — |
| `MEMORY_EMBEDDING_MODEL` | Embedding model | `text-embedding-3-small` |
| `MILVUS_URL` | Milvus server URL | `http://localhost:19530` |
| `MILVUS_DATABASE` | Milvus database name | `Thinkback` |
| `MEMORY_L3_WRITE_MODE` | `async` (default) or `sync` | `async` |
| `MEMORY_DECAY_ENABLED` | Enable decay sweeper | `false` |
| `MEMORY_DECAY_DAYS` | Suppress threshold | `90` |
| `GRPC_ENABLED` | Start gRPC server alongside HTTP | `true` |

---

## ✦ Project layout

```text
thinkback/
├── src/thinkback/
│   ├── api/              FastAPI routers
│   │   ├── auth.py           API key 认证 + Scope 授权
│   │   ├── ratelimit.py      Token Bucket 限流中间件
│   │   ├── idempotency.py    Idempotency-Key 中间件
│   │   ├── errors_standard.py 标准化错误码 (TB-1001~TB-2003)
│   │   ├── v1_memory.py      /v1/memory/* 业务系统接入端点
│   │   ├── integration_admin.py API Key 管理
│   │   └── mem0_config_admin.py mem0 提示词管理
│   ├── domain/           纯业务域(entities / enums / ports / errors)
│   ├── memory/           编排服务
│   │   ├── service.py         MemoryService(主入口)
│   │   ├── backends/         L3 后端(mem0 / fake)
│   │   ├── repositories/     L1+L2 仓储(in-memory / sqlalchemy)
│   │   ├── mem0_config.py   mem0 自定义配置
│   │   └── schemas.py         Pydantic 请求/响应
│   ├── infra/            PostgreSQL / Milvus / logging / config
│   ├── rpc/              gRPC server 与 proto 映射
│   └── pyproject.toml
├── web/                  治理台前端(React 19 + Vite + TanStack Router)
│   ├── src/
│   │   ├── pages/         6 个页面(overview / memories / tasks / govern / audit / config / integration)
│   │   ├── components/    sparkline / heatmap / empty-state / stale-indicator / onboarding-dialog ...
│   │   └── lib/           useListKeyboardNavigation 等 hooks
│   └── package.json
├── proto/memory.proto    gRPC 协议定义
├── alembic/              数据库迁移
├── tests/                pytest 测试套件(615+ 测试)
└── docs/                 设计文档与运行手册
```

---

## ✦ Extending

- **Swap L3 backend** — implement the `MemoryBackend` port (`thinkback.domain.ports`) and
  register it in `thinkback.memory.backends`. Mem0 ships as the default; Qdrant-native or
  pgvector-backed adapters are straightforward to add.
- **Add a new admin page** — drop a new file in `web/src/pages/`, register the route in
  `web/src/main.tsx`, add a sidebar entry in `web/src/components/layout.tsx`.
- **Custom extraction rules** — edit mem0 prompts via the admin console
  (`/integration` → mem0 配置). Changes apply on the next write with no restart.

---

## ✦ Development

```bash
# install all dev tooling
uv sync --frozen --group dev
cd web && npm install

# run the test suites
uv run pytest tests/                 # backend (615+ tests)
cd web && npx tsc --noEmit            # frontend type check
cd web && npm run build               # frontend production build

# start everything in dev mode
make dev                             # API + admin web UI

# regenerate screenshots
cd web && node take-screenshots.mjs
```

Code style:

- **Python** — `ruff` (lint + format) + `mypy --strict` (type check)
- **TypeScript** — strict mode, no `any` at boundaries

---

## ✦ Documentation

- [Architecture deep-dive](docs/ARCHITECTURE.md) — hexagonal ports, dependency inversion
- [API reference](docs/API.md) — full HTTP / gRPC surface
- [Deployment guide](docs/DEPLOYMENT.md) — Kubernetes manifests, Helm chart
- [Operations runbook](docs/RUNBOOK.md) — debugging, recovery, scaling
- [Integration protocol v1 spec](docs/specs/2026-10-08-thinkback-integration-protocol-v1.md) — public API for business systems
- [中文文档](README.zh-CN.md) — Chinese translation

---

## ✦ Community

- 💬 **[GitHub Discussions](.github/DISCUSSIONS)** — questions, ideas, show-and-tell
- 🐛 **[Issue tracker](.github/ISSUE_TEMPLATE/bug_report.md)** — bug reports
- ✨ **[Feature requests](.github/ISSUE_TEMPLATE/feature_request.md)** — what should Thinkback do next?
- 🔔 **[Watch this repo](.github)** — get notified about releases and security fixes

If you're using Thinkback in production and want to share your story, please open a
Discussion — we'd love to feature it in our [wiki](docs/USER_STORIES.md) (coming soon).

---

## ✦ Contributing

We welcome PRs for bug fixes, new backends, admin dashboard improvements, and docs. For
larger changes please open an issue first to discuss direction.

- [Good first issues](../../issues?q=is%3Aopen+is%3Aissue+label%3A%22good+first+issue%22)
- [How to contribute](.github/CONTRIBUTING.md)
- [Code of conduct](.github/CODE_OF_CONDUCT.md)
- [Security policy](.github/SECURITY.md)

---

## ✦ Featured by

<!--
If your company / publication / community uses Thinkback, please open a PR and add your
logo + link here. We'll feature you in the next release notes.
-->
<a href="https://github.com/mem0ai/mem0"><img src="https://img.shields.io/badge/powered%20by-Mem0-7C3AED?style=for-the-badge&logo=data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAxNiAxNiI+PHBhdGggZD0iTTggMUw0IDd2NkwxMS41TDE0IDh2NEgydjZoM2w0LTJ2Nkg5eiIvPjwvc3ZnPg==" alt="Mem0"/></a>

<sub>Want your logo here? [Open a PR](.github/PULL_REQUEST_TEMPLATE.md) to add your company — usage of Thinkback in production is the only requirement.</sub>

---

## ✦ Sponsors

Thinkback is independently developed and open source under Apache 2.0. If your company benefits
from this project and wants to see it grow faster, consider sponsoring its development.

<!--
Sponsorship tiers (per CONTRIBUTING.md):
  - $100/mo: listed in README sponsors section
  - $500/mo: logo on landing page + README + release notes
  - $2k+/mo:  prioritized feature requests + private support channel

To become a sponsor, open an issue with title [sponsor] and we'll get back to you.
-->

**Current sponsors:** *(be the first!)*

---

## ✦ License

[Apache License 2.0](LICENSE) — see `LICENSE` for the full text.

Thinkback is open source under the Apache 2.0 license. You can freely use, modify, and
distribute it, including for commercial purposes, as long as you preserve the copyright
notice and disclaimer.

---

## ✦ Acknowledgments

Thinkback builds on the work of many open-source projects and people:

- **[Mem0](https://github.com/mem0ai/mem0)** — the long-term memory engine Thinkback orchestrates
- **[Milvus](https://milvus.io/)** — the vector store that powers L3 recall
- **[PostgreSQL](https://www.postgresql.org/)** — the durable substrate for L1/L2
- **[FastAPI](https://fastapi.tiangolo.com)** — the HTTP framework
- **[TanStack Query / Router](https://tanstack.com)** — the admin web UI
- **[Pydantic](https://docs.pydantic.dev/)** — type-safe request/response models
- The [open-source AI memory community](https://github.com/topics/llm-memory) for inspiration

The community of contributors who report issues, send PRs, and share their use cases is
what makes this project possible.

---

<div align="center">

**If Thinkback is useful to you, consider giving it a ⭐ on GitHub — it helps others discover the project.**

[**⭐ Star**](.github) · [**🍴 Fork**](.github/fork) · [**📖 Docs**](docs/) · [**Report issue**](.github/ISSUE_TEMPLATE/bug_report.md)

Built with care for teams who need memory to be a **service**, not a side project.

</div>
