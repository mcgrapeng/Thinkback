# Thinkback

Thinkback is the memory service for the AI virtual social stack. It owns the
three-layer memory business workflow and calls an independently deployed Mem0
REST service for L3 long-term memory.

## Scope

Included:

- FastAPI service shell
- Health, liveness, and readiness probes
- PostgreSQL configuration and Alembic scaffold
- Redis configuration
- Celery worker bootstrap
- P0 memory workflows aligned with the three-layer memory architecture
- Remote Mem0 REST API long-term memory adapter
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

Thinkback talks to an independently deployed Mem0 REST service:

```bash
MEM0_API_URL=https://mem0.example.internal
MEM0_API_KEY=<secret>
MEM0_HTTP_TIMEOUT_SECONDS=120
```

Thinkback, Mem0, and Qdrant are configured as three independent services. Both
Thinkback and Mem0 should point to the same Qdrant endpoint, but only Mem0 owns
L3 vector writes and semantic search. Thinkback uses `QDRANT_URL` and
`QDRANT_API_KEY` for readiness diagnostics only:

```bash
QDRANT_URL=https://qdrant.example.internal
QDRANT_API_KEY=<secret>
```

Run a worker:

```bash
make worker
```

Run deterministic tests:

```bash
PYTHONPATH=src .venv/bin/pytest
```

Run the real Mem0 5-round pressure scenario:

```bash
make docker-up
PYTHONPATH=src .venv/bin/python script/real_mem0_pressure.py
```

The real pressure script requires `MEM0_API_URL` and `MEM0_API_KEY`. Mem0 owns
LLM, embedding, L3 vector writes, semantic search, update, and delete. Thinkback
only probes Qdrant availability; it does not bypass Mem0 for L3 operations.

For a local isolated Mem0 REST stack, use the local URL instead:

```bash
MEM0_API_URL=http://localhost:8888
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
