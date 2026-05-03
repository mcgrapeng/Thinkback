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
- mem0 configuration boundary
- Docker Compose local stack
- Kubernetes API and worker manifests

Excluded from the scaffold:

- Memory extraction
- Memory recall
- Memory deletion
- Feedback and rebuild workflows
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

Run a worker:

```bash
make worker
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
