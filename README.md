# Thinkback

Thinkback is the memory service for the AI virtual social stack. It owns the
three-layer memory business workflow and uses Mem0 Library for L3 long-term
memory.

## Scope

Included:

- FastAPI service shell
- Health, liveness, and readiness probes
- PostgreSQL configuration and Alembic scaffold
- Redis configuration
- Celery worker bootstrap
- P0 memory workflows aligned with the three-layer memory architecture
- Mem0 Library long-term memory adapter
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

Thinkback embeds Mem0 Library. Mem0 uses OpenAI for extraction/embeddings and
an independently deployed Qdrant for L3 vector storage:

```bash
OPENAI_API_KEY=<secret>
QDRANT_URL=https://qdrant.example.internal
QDRANT_API_KEY=<secret>
MEMORY_QDRANT_COLLECTION=memories_qwen_1024
MEMORY_LLM_MODEL=qwen3.5-flash
MEMORY_EMBEDDING_MODEL=text-embedding-v4
MEM0_HISTORY_DB_PATH=.mem0/history.db
MEMORY_L3_WRITE_MODE=async
MEMORY_L3_EXECUTOR_WORKERS=16
MEMORY_L3_MAX_PENDING_TASKS=256
MEMORY_L3_QUEUE_WAIT_SECONDS=5
READINESS_TIMEOUT_SECONDS=30
```

Thinkback and Qdrant can run on different hosts. The service must not bypass
Mem0 Library and write Qdrant directly.

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

The real pressure script requires `OPENAI_API_KEY` and `QDRANT_URL`. Mem0
Library owns LLM extraction, embedding, L3 vector writes, semantic search,
update, and delete.

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
