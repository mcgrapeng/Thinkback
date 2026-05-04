# Thinkback

Thinkback is a memory service scaffold. It provides the runtime foundation for a
future mem0-backed memory implementation without shipping memory business
workflows in the initial scaffold.

## Scope

Included:

- FastAPI service shell
- Health, liveness, and readiness probes
- PostgreSQL configuration and Alembic scaffold
- Redis configuration
- Qdrant configuration
- Celery worker bootstrap
- P0 memory workflows aligned with the three-layer memory architecture
- Remote Mem0 REST API long-term memory adapter, plus local SDK mode for development
- Docker Compose local stack
- Kubernetes API and worker manifests

Not included in P0:

- P2 write fences, semantic suppression, tombstones, dead-letter governance, and decay jobs
- Relationship scoring
- RAG, document parsing, object storage, safety, and evaluation features

## Requirements

- Python `>=3.11,<3.14`
- Poetry
- Docker and Docker Compose for local infrastructure

## Quick Start

```bash
poetry install
cp .env.example .env
make test
make run
```

Start local dependencies:

```bash
make docker-up
```

Production should use a remote Mem0 service:

```bash
MEM0_BACKEND_MODE=http_api
MEM0_API_URL=https://mem0.example.internal
MEM0_API_KEY=<secret>
MEM0_HTTP_TIMEOUT_SECONDS=120
```

Run a worker:

```bash
make worker
```

Run deterministic tests:

```bash
PYTHONPATH=src .venv/bin/pytest
```

Run the real Mem0/OpenAI/Qdrant 5-round pressure scenario:

```bash
make docker-up
PYTHONPATH=src .venv/bin/python script/real_mem0_pressure.py
```

The real pressure script requires `MEMORY_LLM_API_KEY`, `MEMORY_EMBEDDING_API_KEY`, and a reachable `QDRANT_URL`. It fails fast when real configuration is missing; it does not fall back to fake memory.

For a local isolated Mem0 REST stack, use the local URL instead:

```bash
MEM0_BACKEND_MODE=http_api
MEM0_API_URL=http://localhost:8889
MEM0_API_KEY=<local-key>
MEM0_HTTP_TIMEOUT_SECONDS=120
```

## Health

```bash
curl http://localhost:8000/health
curl http://localhost:8000/health/live
curl http://localhost:8000/health/ready
```

## Project Layout

```text
src/api      HTTP application and routes
src/infra    runtime integrations
src/memory   memory-service boundaries
test         scaffold tests
k8s          Kubernetes deployment assets
```
