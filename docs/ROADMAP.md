# Roadmap

Thinkback v1.0 is the first public release. The roadmap is a living document — vote on issues and we'll prioritize accordingly.

## v1.0 (Shipped)

- Three-layer memory model (L1 Round Journal / L2 Session Summary / L3 Long-term Memory)
- Bitemporal validity (`valid_at` / `invalid_at`)
- Business index state machine (`ACTIVE` / `DELETED` / `SUPERSEDED` / `SUPPRESSED`)
- HTTP + gRPC with shared Pydantic schema (26+ endpoints)
- Idempotent writes via `request_id` / `round_id` fingerprints
- Explicit `degraded=True` signal with `degradation_reasons`
- Scoped deletes (`MEMORY` / `SESSION` / `ALL`) with tombstones
- Admin dashboard (React 19, 7 pages, Cmd-K, keyboard nav, URL deep links)
- v1 public integration protocol (API key + Scope + Tenant + rate limit + idempotency)
- mem0 prompt management (real-time editing of 3 core prompts)
- K8s-native liveness/readiness probes, Prometheus metrics, Alembic migrations
- 615+ tests

## v1.1 (In Progress)

- **Webhook event delivery** — `memory.extracted`, `memory.superseded`, `memory.deleted`, `task.completed`
- **Python SDK** (`thinkback-py`) — type-safe client with retry + idempotency

## v1.2 (Planned)

- **TypeScript SDK** (`@thinkback/sdk`) — browser + Node.js client
- **Multi-tenant isolation** — per-tenant quotas, scoped admin views
- **SSO integration** — OAuth2 / OIDC for admin dashboard
- **Memory graph view** — visualize linked memories in the admin dashboard
- **Batch operations** — bulk delete / export / import memories

## Future

- **Memory decay rules** — configurable decay policies per memory type
- **A/B testing harness** — compare extraction prompt variants
- **Audit export** — CSV / JSON export for compliance
- **Grafana dashboard** — pre-built metrics dashboard for ops teams

## Vote

Open an [issue](.github/ISSUE_TEMPLATE/feature_request.md) with the `[Feature]` prefix, and we'll review + prioritize based on community feedback.
