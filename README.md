<!-- 顶部对齐排版,GitHub 原生渲染无需额外样式 -->
<div align="center">

<img src="assets/banner.svg" alt="Thinkback — Memory Layer for Personalized AI Conversations" width="100%">

<br>

<img src="assets/logo-mark.svg" alt="Thinkback" width="64" height="64" style="vertical-align: middle;">

# Thinkback

**Self-hosted memory service for AI conversations.**

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

[**English**](README.md) · [**中文**](README.zh-CN.md) · [Docs](docs/) · [Changelog](CHANGELOG.md) · [Roadmap](docs/ROADMAP.md)

[Report Bug](.github/ISSUE_TEMPLATE/bug_report.md) · [Request Feature](.github/ISSUE_TEMPLATE/feature_request.md)

</div>

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

## ✦ Capabilities

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

### Typical uses

- Conversation assistants with session-spanning memory
- Customer support bots recalling past tickets and preferences
- Multi-turn agents holding task context across tool calls and interruptions
- Self-hosted AI infrastructure that must keep data on-prem

## ✦ Admin dashboard

A web UI for inspecting and repairing the memory layer — background task states with retry
counts and last error, per-user memory browsing with source back-links, governance actions,
and an audit trail. Powered by React 19 + TanStack Query/Router, designed in Editorial
Premium style.

### Overview — at-a-glance system health

<div align="center">
  <img src="assets/screenshots/01-overview.png" alt="Thinkback overview — 96px hero KPI with sparkline, system pulse, needs attention, and heatmaps" width="100%">
  <p><em>96px hero KPI with 24h sparkline · System Pulse with 4 sub-KPIs · Needs Attention list · 5min throughput heatmap · Recent governance audit timeline</em></p>
</div>

### Memory browser — search, source back-link, compare

<div align="center">
  <img src="assets/screenshots/02-memories-list.png" alt="Memory browser — editorial card list with status chips and 24h sparkline" width="100%">
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

### Admin (`/admin/api/*`)

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/admin/api/overview` | System pulse: 4 hero KPIs + system pulse + L3 status + recent audit |
| `GET` | `/admin/api/tasks` | Task list with `statuses` filter, pagination |
| `GET` | `/admin/api/memories` | Memory list with `user_id` / `statuses`, source back-link |
| `GET` | `/admin/api/audit` | Audit log with `action` filter (delete / update / rebuild) |
| `POST` | `/admin/api/{delete,update,rebuild}` | Governance actions with `operation_id` idempotency |

gRPC surface mirrors the same operations in `proto/memory.proto` for service-to-service calls.

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

## ✦ Project layout

```text
thinkback/
├── src/thinkback/
│   ├── api/              FastAPI routers (memory, admin, health, integration, mem0-config)
│   │   ├── auth.py           API key 认证 + Scope 授权
│   │   ├── ratelimit.py      Token Bucket 限流中间件
│   │   ├── idempotency.py    Idempotency-Key 中间件
│   │   ├── errors_standard.py 标准化错误码 (TB-1001~TB-2003)
│   │   ├── v1_memory.py      /v1/memory/* 业务系统接入端点
│   │   ├── integration_admin.py API Key 管理端点
│   │   └── mem0_config_admin.py mem0 提示词管理端点
│   ├── domain/           纯业务域(entities / enums / ports / errors)
│   ├── memory/           编排服务
│   │   ├── service.py         MemoryService(主入口)
│   │   ├── backends/         L3 后端(mem0 / fake)
│   │   ├── repositories/     L1+L2 仓储(in-memory / sqlalchemy)
│   │   ├── mem0_config.py   mem0 自定义配置(提示词等)
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

## ✦ Extending

- **Swap L3 backend** — implement the `MemoryBackend` port (`thinkback.domain.ports`) and
  register it in `thinkback.memory.backends`. Mem0 ships as the default; Qdrant-native or
  pgvector-backed adapters are straightforward to add.
- **Add a new admin page** — drop a new file in `web/src/pages/`, register the route in
  `web/src/main.tsx`, add a sidebar entry in `web/src/components/layout.tsx`.
- **Custom extraction rules** — edit mem0 prompts via the admin console
  (`/integration` → mem0 配置). Changes apply on the next write with no restart.

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
```

Code style:

- **Python** — `ruff` (lint + format) + `mypy --strict` (type check)
- **TypeScript** — strict mode, no `any` at boundaries

## ✦ Documentation

- [Architecture deep-dive](docs/ARCHITECTURE.md) — hexagonal ports, dependency inversion
- [API reference](docs/API.md) — full HTTP / gRPC surface
- [Deployment guide](docs/DEPLOYMENT.md) — Kubernetes manifests, Helm chart
- [Operations runbook](docs/RUNBOOK.md) — debugging, recovery, scaling
- [中文文档](README.zh-CN.md) — Chinese translation

## ✦ Contributing

We welcome PRs for bug fixes, new backends, admin dashboard improvements, and docs. For
larger changes please open an issue first to discuss direction.

- [Good first issues](../../issues?q=is%3Aopen+is%3Aissue+label%3A%22good+first+issue%22)
- [How to contribute](.github/CONTRIBUTING.md)
- [Code of conduct](.github/CODE_OF_CONDUCT.md)
- [Security policy](.github/SECURITY.md)

## ✦ License

[Apache License 2.0](LICENSE) — see `LICENSE` for the full text.

Thinkback is open source under the Apache 2.0 license. You can freely use, modify, and
distribute it, including for commercial purposes, as long as you preserve the copyright
notice and disclaimer.

## ✦ Acknowledgments

- [Mem0](https://github.com/mem0ai/mem0) — the long-term memory engine that Thinkback
  orchestrates
- [Milvus](https://milvus.io/) — the vector store under L3
- [FastAPI](https://fastapi.tiangolo.com) — the HTTP framework
- [TanStack Query / Router](https://tanstack.com) — the admin web UI
- The community of contributors who report issues, send PRs, and share their use cases

---

<div align="center">

Built with care for teams who need memory to be a **service**, not a side project.

[**Get started**](#-quickstart) · [**Read the docs**](docs/) · [**Report an issue**](.github/ISSUE_TEMPLATE/bug_report.md)

</div>
