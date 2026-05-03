# Thinkback Scaffold Design

## Context

Thinkback is a memory service. The current repository contains only a README and
should be initialized as a production-oriented service scaffold. The scaffold may
reuse the engineering style of `liaoriver-knowledge`, but it must stay focused on
Thinkback's memory-service boundary.

The user explicitly wants an industry-mainstream, recommended structure without
over-engineering. Thinkback depends on mem0, has background tasks, will use Qdrant,
Celery, and Kubernetes deployment, but memory business code will be implemented
later from documentation. This scaffold must therefore define clean extension
points and runtime wiring, not implement memory extraction, recall, delete, or
relationship logic.

## Goals

- Initialize a maintainable Python backend scaffold using mainstream patterns.
- Keep the service focused on memory-service concerns.
- Provide a runnable FastAPI app with health and readiness endpoints.
- Provide configuration, logging, database, cache, queue, and vector-store
  integration boundaries.
- Include local and deployment assets for PostgreSQL, Redis, Qdrant, Celery, and
  Kubernetes.
- Add focused tests that prove the scaffold imports, starts, and exposes baseline
  endpoints.
- Avoid copying unrelated RAG, document parsing, MinIO, safety, or evaluation
  code from the reference project.

## Non-Goals

- No implementation of memory extraction, recall, deletion, feedback, rebuild, or
  relationship scoring.
- No mem0 business workflow implementation beyond a future-facing adapter module
  boundary.
- No RAG ingestion, document parsing, reranking, object storage, safety audit, or
  evaluation-admin surfaces.
- No production-grade Kubernetes platform stack such as ingress, HPA, monitoring,
  service mesh, or managed dependency manifests.

## Chosen Approach

Use a focused FastAPI service scaffold with `src/` layout and explicit runtime
layers:

- `api`: HTTP application factory, routes, and dependency wiring.
- `infra`: configuration, logging, database, Redis, Celery, and Qdrant clients.
- `memory`: memory-service module boundary with schemas, ports, and placeholder
  service/task entry points.
- `test`: unit tests for the scaffold contract.
- `alembic`: database migration baseline.
- `k8s`: API and worker deployment manifests.

This follows the useful structure of `liaoriver-knowledge` while removing
unrelated domains. It is more complete than a toy API scaffold, but smaller than
copying the reference project wholesale.

## Project Structure

```text
Thinkback/
  src/
    api/
      __init__.py
      app.py
      dependencies.py
      health.py
    infra/
      __init__.py
      config.py
      logging.py
      readiness.py
      cache/
        __init__.py
        redis_client.py
      database/
        __init__.py
        base.py
        engine.py
      tasks/
        __init__.py
        celery_app.py
      vectorstore/
        __init__.py
        qdrant_client.py
    memory/
      __init__.py
      application/
        __init__.py
      domain/
        __init__.py
      mem0_client.py
      service.py
      tasks.py
  test/
    __init__.py
    conftest.py
    unit/
      __init__.py
      test_api.py
      test_config.py
  alembic/
    env.py
    script.py.mako
    versions/
  k8s/
    README.md
    configmap.yaml
    deployment-api.yaml
    deployment-worker.yaml
    secret.example.yaml
    service.yaml
  script/
    db_migrate.py
  .dockerignore
  .env.example
  .pre-commit-config.yaml
  Dockerfile
  Makefile
  README.md
  alembic.ini
  docker-compose.yml
  pyproject.toml
```

## Runtime Surface

The first scaffold exposes only platform-level endpoints:

- `GET /`: service identity and status.
- `GET /health`: process health.
- `GET /health/live`: liveness probe.
- `GET /health/ready`: dependency readiness for PostgreSQL, Redis, and Qdrant.

Memory HTTP endpoints are intentionally not added yet. The `memory` package
exists so future implementation can land in a stable place without changing the
top-level architecture.

## Configuration

Use `pydantic-settings` with `.env` support. Baseline settings include:

- app metadata and environment.
- PostgreSQL connection fields.
- Redis connection fields and Celery broker/result URLs.
- Qdrant URL, API key, and memory collection name.
- mem0 LLM/embedder configuration placeholders.
- Celery task timeout settings.

Production validation should remain light in the scaffold. It may validate enum
choices and log level, but should not require API keys until memory business code
is implemented.

## Infrastructure Boundaries

Database:

- Provide SQLAlchemy async engine/session helpers.
- Include Alembic wiring and an empty initial migration path.
- Do not define memory tables yet unless the later documentation requires them.

Redis:

- Provide a small client factory and health ping helper.
- Do not implement memory window behavior yet.

Qdrant:

- Provide a client factory and health/readiness helper.
- Do not create collections or define vector schemas yet.

Celery:

- Provide one Celery app under `infra.tasks.celery_app`.
- Include `memory.tasks` as the future task module.
- Register only a no-op diagnostic task if needed for worker bootstrap; avoid
  memory business tasks.

mem0:

- Provide `memory.mem0_client` as an adapter boundary with config construction
  placeholders.
- Avoid calling `Memory.from_config` in import-time code.
- Do not implement add/search/delete workflows yet.

## Local Development

`docker-compose.yml` should include:

- `app`
- `worker`
- `postgres`
- `redis`
- `qdrant`

No MinIO service is included because object storage is not part of the current
memory-service scaffold.

Make targets:

- `make install`
- `make dev`
- `make run`
- `make worker`
- `make test`
- `make lint`
- `make format`
- `make docker-up`
- `make docker-down`
- `make db-upgrade`
- `make db-revision MSG="..."`

## Kubernetes

Provide baseline manifests for future deployment:

- API deployment with readiness and liveness probes.
- Worker deployment using the same image and Celery command.
- ClusterIP service for API.
- ConfigMap for non-secret runtime settings.
- Secret example for passwords and API keys.

Do not add ingress, autoscaling, or dependency-stateful manifests in this scaffold.

## Testing

Initial tests should verify:

- `create_app()` builds a FastAPI app.
- Root and health endpoints return expected payloads.
- Readiness returns a structured payload and can be monkeypatched without real
  infrastructure.
- Settings load defaults and derived URLs correctly.
- Celery app imports without starting workers or connecting to Redis.

These tests validate the scaffold contract without forcing memory business
behavior before it is designed.

## Error Handling

The scaffold should include request trace logging and return ordinary FastAPI
errors. Readiness checks should report dependency status per dependency rather
than raising on the first failure. Business-specific errors are deferred until
memory endpoints are implemented.

## Implementation Constraints

- Keep code comments concise.
- Prefer explicit module boundaries over broad abstraction.
- Avoid copying large reference-project modules that are not part of Thinkback.
- Keep names aligned to Thinkback rather than liaoriver-knowledge.
- Do not implement memory behavior in this scaffold.
