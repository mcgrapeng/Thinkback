# Changelog

All notable changes to Thinkback will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-10-08

### Added

**Core memory model**
- Three-layer memory architecture (L1 Round Journal / L2 Session Summary / L3 Long-term Memory)
- Bitemporal validity (`valid_at` / `invalid_at`) for fact supersession
- Business index state machine (`ACTIVE` / `DELETED` / `SUPERSEDED` / `SUPPRESSED`)
- Decay sweeper for long-tail memory suppression (opt-in)

**API surface**
- HTTP + gRPC with shared Pydantic schema (26+ endpoints)
- Idempotent writes via `request_id` / `round_id` fingerprints
- Explicit `degraded=True` signal with `degradation_reasons`
- Scoped deletes (`MEMORY` / `SESSION` / `ALL`) with tombstones

**v1 public integration protocol**
- API key + Scope-based auth (`memory:append` / `memory:recall` / `memory:read` / etc.)
- Token Bucket rate limiting (`X-RateLimit-*` headers, `Retry-After`)
- `Idempotency-Key` header for replay-safe writes
- Standardized error codes (`TB-1001` ~ `TB-2003`)
- Tenant isolation (`X-Tenant-Id`)

**Admin dashboard**
- React 19 + Vite + TanStack Query/Router
- 7 pages: Overview / Memory Browser / Tasks / Govern / Audit / Config / Integration
- Overview: 96px hero KPI + sparkline + 5min throughput heatmap + recent audit timeline
- Memory browser: editorial card list + source back-link + ⌘K command palette + j/k keyboard nav
- Tasks: 4 KPI sparklines + retry count chips + expandable detail rows
- Governance: scoped delete with confirmation + update + rebuild + operation ID idempotency
- Audit: timeline view with color-coded action dots
- Config: grouped config with SECRET badges + search aliases
- Integration: API Key management + mem0 prompt editing + protocol reference

**Operability**
- Kubernetes-native liveness/readiness probes
- Prometheus metrics (request latency, queue depth, task states, L3 worker stats)
- Alembic migrations
- 615+ tests (in-memory + fake Mem0 backends)

**mem0 prompt management**
- Real-time editing of `custom_instructions` / `update_memory_prompt` / `memory_answer_prompt`
- 4-stage pipeline explanation for AI engineers
- Tuning tips + examples per prompt

### Dependencies
- Python 3.12+ / FastAPI / SQLAlchemy 2.0 / Mem0 / Milvus / PostgreSQL
- React 19 / Vite 6 / TanStack Query 5 / TanStack Router / Radix UI / Tailwind CSS 4
- gRPC (proto3) / Pydantic v2 / mypy --strict / ruff

---

## [Unreleased]

### Planned
- Webhook event delivery (`memory.extracted`, `memory.superseded`, `memory.deleted`, `task.completed`)
- Python SDK (`thinkback-py`)
- TypeScript SDK (`@thinkback/sdk`)
- Multi-tenant isolation
- SSO integration for admin dashboard
