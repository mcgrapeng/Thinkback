# Thinkback Scaffold Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a runnable Thinkback memory-service scaffold with FastAPI, PostgreSQL, Redis, Qdrant, Celery, mem0 boundaries, Docker Compose, Kubernetes manifests, and tests, without implementing memory business behavior.

**Architecture:** Use a focused `src/` Python service layout. `api` owns HTTP app and platform routes, `infra` owns runtime integrations, and `memory` owns only extension boundaries for future memory workflows. The first working surface is health/readiness and worker bootstrap.

**Tech Stack:** Python 3.11, Poetry, FastAPI, Pydantic Settings, SQLAlchemy async, Alembic, Redis, Qdrant client, Celery, mem0ai, pytest, Ruff, mypy, Docker, Kubernetes.

---

## Scope Check

This plan covers one subsystem: the Thinkback memory-service scaffold. It intentionally excludes memory extraction, recall, deletion, feedback, rebuild, relationship scoring, RAG, MinIO, safety, and evaluation features.

## File Structure Map

- `pyproject.toml`: package metadata, dependencies, test/lint/type-check settings.
- `src/api/app.py`: FastAPI application factory, middleware, root route, router registration.
- `src/api/health.py`: health, liveness, and readiness routes.
- `src/api/dependencies.py`: dependency accessors for settings and readiness hooks.
- `src/infra/config.py`: environment-backed settings and derived URLs.
- `src/infra/logging.py`: trace id context helpers and logging setup.
- `src/infra/readiness.py`: dependency readiness aggregation.
- `src/infra/database/base.py`: SQLAlchemy declarative base.
- `src/infra/database/engine.py`: async engine/session factory and database ping.
- `src/infra/cache/redis_client.py`: Redis client factory and ping.
- `src/infra/vectorstore/qdrant_client.py`: Qdrant client factory and ping.
- `src/infra/tasks/celery_app.py`: Celery application configuration and worker bootstrap.
- `src/memory/mem0_client.py`: mem0 configuration boundary and lazy factory.
- `src/memory/service.py`: explicit memory service boundary object with no workflows.
- `src/memory/tasks.py`: diagnostic Celery task used to prove worker startup.
- `test/conftest.py`: FastAPI test client fixture.
- `test/unit/test_api.py`: API and readiness route tests.
- `test/unit/test_config.py`: settings and derived URL tests.
- `test/unit/test_runtime_boundaries.py`: Celery and mem0 boundary tests.
- `test/unit/test_scaffold_files.py`: deployment and migration asset tests.
- `alembic.ini`, `alembic/env.py`, `alembic/script.py.mako`: migration scaffold.
- `Dockerfile`, `docker-compose.yml`, `.dockerignore`, `.env.example`, `Makefile`: local runtime assets.
- `k8s/*`: API and worker deployment manifests.
- `README.md`: service purpose, commands, and current boundaries.

---

## Task 1: Project Metadata and Minimal API

**Files:**
- Create: `pyproject.toml`
- Create: `src/__init__.py`
- Create: `src/api/__init__.py`
- Create: `src/api/app.py`
- Create: `src/api/health.py`
- Create: `test/__init__.py`
- Create: `test/conftest.py`
- Create: `test/unit/__init__.py`
- Create: `test/unit/test_api.py`

- [ ] **Step 1: Write the failing API tests**

Create `test/conftest.py`:

```python
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient

from api.app import create_app


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    with TestClient(create_app()) as test_client:
        yield test_client
```

Create `test/unit/test_api.py`:

```python
from fastapi import FastAPI

from api.app import create_app


def test_create_app_returns_fastapi_app() -> None:
    app = create_app()

    assert isinstance(app, FastAPI)
    assert app.title == "Thinkback"


def test_root_endpoint(client) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert response.json() == {
        "name": "Thinkback",
        "version": "0.1.0",
        "status": "running",
        "scope": "memory-service",
    }


def test_health_endpoint(client) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "healthy",
        "version": "0.1.0",
        "environment": "development",
    }


def test_liveness_endpoint(client) -> None:
    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}
```

Create empty package markers:

```bash
touch test/__init__.py test/unit/__init__.py
```

- [ ] **Step 2: Run an import smoke check and verify it fails**

Run:

```bash
PYTHONPATH=src python - <<'PY'
from api.app import create_app

create_app()
PY
```

Expected: FAIL with `ModuleNotFoundError: No module named 'api'`.

- [ ] **Step 3: Add package metadata and minimal API implementation**

Create `pyproject.toml`:

```toml
[tool.poetry]
name = "thinkback"
version = "0.1.0"
description = "Thinkback memory service"
authors = ["Thinkback Contributors"]
readme = "README.md"
packages = [
    {include = "api", from = "src"},
    {include = "infra", from = "src"},
    {include = "memory", from = "src"}
]

[tool.poetry.dependencies]
python = ">=3.11,<3.14"
fastapi = "^0.135.2"
uvicorn = {extras = ["standard"], version = "^0.43.0"}
pydantic = "^2.12.5"
pydantic-settings = "^2.13.1"
sqlalchemy = {extras = ["asyncio"], version = "^2.0.49"}
asyncpg = "^0.30.0"
alembic = "^1.18.4"
redis = {extras = ["hiredis"], version = "^7.4.0"}
celery = "^5.6.3"
qdrant-client = "^1.17.0"
mem0ai = "^0.1.0"
loguru = "^0.7.2"
python-dotenv = "^1.0.0"

[tool.poetry.group.dev.dependencies]
pytest = "^8.3.0"
pytest-asyncio = "^0.24.0"
pytest-mock = "^3.14.0"
httpx = "^0.28.0"
ruff = "^0.8.0"
black = "^24.10.0"
mypy = "^1.13.0"
pre-commit = "^4.0.0"

[build-system]
requires = ["poetry-core"]
build-backend = "poetry.core.masonry.api"

[tool.ruff]
line-length = 100
target-version = "py311"

[tool.ruff.lint]
select = ["E", "W", "F", "I", "B", "C4", "UP", "ARG", "SIM"]
ignore = ["E501", "B008", "B904"]

[tool.ruff.lint.per-file-ignores]
"__init__.py" = ["F401"]
"test/**/*" = ["ARG", "S101"]

[tool.black]
line-length = 100
target-version = ["py311"]
include = "\\.pyi?$"

[tool.mypy]
python_version = "3.11"
warn_return_any = true
warn_unused_configs = true
disallow_untyped_defs = true
disallow_incomplete_defs = true
check_untyped_defs = true
no_implicit_optional = true
warn_redundant_casts = true
warn_unused_ignores = true
warn_no_return = true
strict_equality = true
ignore_missing_imports = true

[[tool.mypy.overrides]]
module = "test.*"
disallow_untyped_defs = false

[tool.pytest.ini_options]
minversion = "8.0"
addopts = "-ra -q --strict-markers"
testpaths = ["test"]
pythonpath = ["src"]
asyncio_mode = "auto"
```

Create `src/__init__.py`:

```python
"""Thinkback service source package."""
```

Create `src/api/__init__.py`:

```python
"""HTTP API package."""
```

Create `src/api/health.py`:

```python
"""Health and probe routes."""

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/health", tags=["health"])


class HealthResponse(BaseModel):
    status: str
    version: str
    environment: str


@router.get("", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    return HealthResponse(
        status="healthy",
        version="0.1.0",
        environment="development",
    )


@router.get("/live")
async def liveness_check() -> dict[str, str]:
    return {"status": "alive"}
```

Create `src/api/app.py`:

```python
"""Thinkback FastAPI application factory."""

from fastapi import FastAPI

from api.health import router as health_router


def create_app() -> FastAPI:
    app = FastAPI(
        title="Thinkback",
        version="0.1.0",
        description="Thinkback memory service",
        debug=False,
    )
    app.include_router(health_router)

    @app.get("/")
    async def root() -> dict[str, str]:
        return {
            "name": "Thinkback",
            "version": "0.1.0",
            "status": "running",
            "scope": "memory-service",
        }

    return app


app = create_app()

__all__ = ["app", "create_app"]
```

- [ ] **Step 4: Install dependencies and run the API tests**

Run:

```bash
poetry install
PYTHONPATH=src poetry run pytest test/unit/test_api.py -q
```

Expected: PASS for 4 tests.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml poetry.lock src test
git commit -m "feat: add minimal FastAPI scaffold"
```

---

## Task 2: Settings and Request Logging

**Files:**
- Create: `src/infra/__init__.py`
- Create: `src/infra/config.py`
- Create: `src/infra/logging.py`
- Modify: `src/api/app.py`
- Modify: `src/api/health.py`
- Create: `test/unit/test_config.py`
- Modify: `test/unit/test_api.py`

- [ ] **Step 1: Write failing settings and trace tests**

Create `test/unit/test_config.py`:

```python
from infra.config import Settings


def test_settings_defaults_are_thinkback_baseline() -> None:
    settings = Settings()

    assert settings.app_name == "Thinkback"
    assert settings.app_version == "0.1.0"
    assert settings.environment == "development"
    assert settings.database_backend == "postgresql"
    assert settings.vectorstore_type == "qdrant"
    assert settings.memory_backend == "redis"


def test_settings_derived_urls() -> None:
    settings = Settings()

    assert settings.database_url == (
        "postgresql+asyncpg://postgres:postgres@localhost:5432/thinkback"
    )
    assert settings.redis_url == "redis://localhost:6379/0"
```

Append this test to `test/unit/test_api.py`:

```python
def test_trace_id_header_is_returned(client) -> None:
    response = client.get("/health", headers={"X-Request-Id": "req-test"})

    assert response.status_code == 200
    assert response.headers["X-Trace-Id"] == "req-test"
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
PYTHONPATH=src poetry run pytest test/unit/test_config.py test/unit/test_api.py::test_trace_id_header_is_returned -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'infra'` or missing `X-Trace-Id`.

- [ ] **Step 3: Add settings and logging modules**

Create `src/infra/__init__.py`:

```python
"""Infrastructure package for runtime integrations."""
```

Create `src/infra/config.py`:

```python
"""Runtime configuration for Thinkback."""

from functools import lru_cache
from typing import Literal
from urllib.parse import quote_plus

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "Thinkback"
    app_version: str = "0.1.0"
    environment: Literal["development", "staging", "production"] = "development"
    debug: bool = False
    log_level: str = "INFO"

    api_host: str = "0.0.0.0"
    api_port: int = 8000

    database_backend: Literal["postgresql"] = "postgresql"
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "postgres"
    postgres_password: str = "postgres"
    postgres_database: str = "thinkback"

    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: str = ""
    memory_backend: Literal["redis"] = "redis"

    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"
    celery_task_time_limit: int = 3600
    celery_task_soft_time_limit: int = 3000

    vectorstore_type: Literal["qdrant"] = "qdrant"
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str = ""
    memory_qdrant_collection: str = "thinkback_memories"

    memory_llm_model: str = "gpt-4o-mini"
    memory_llm_base_url: str = "https://api.openai.com/v1"
    memory_llm_api_key: str = Field(default="", description="Memory LLM API key")
    memory_embedding_model: str = "text-embedding-3-small"
    memory_embedding_api_key: str = Field(default="", description="Memory embedding API key")

    @property
    def database_url(self) -> str:
        encoded_password = quote_plus(self.postgres_password) if self.postgres_password else ""
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{encoded_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_database}"
        )

    @property
    def redis_url(self) -> str:
        auth_part = f":{self.redis_password}@" if self.redis_password else ""
        return f"redis://{auth_part}{self.redis_host}:{self.redis_port}/{self.redis_db}"

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        normalized = value.upper()
        valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if normalized not in valid_levels:
            raise ValueError(f"Invalid log level: {value}")
        return normalized


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
```

Create `src/infra/logging.py`:

```python
"""Request trace id helpers."""

from __future__ import annotations

from contextvars import ContextVar
from uuid import uuid4

_trace_id: ContextVar[str | None] = ContextVar("trace_id", default=None)


def set_trace_id(incoming_trace_id: str | None = None) -> str:
    trace_id = incoming_trace_id or uuid4().hex
    _trace_id.set(trace_id)
    return trace_id


def get_trace_id() -> str | None:
    return _trace_id.get()


def clear_trace_id() -> None:
    _trace_id.set(None)
```

- [ ] **Step 4: Wire settings and trace middleware into API**

Replace `src/api/health.py` with:

```python
"""Health and probe routes."""

from fastapi import APIRouter
from pydantic import BaseModel

from infra.config import settings

router = APIRouter(prefix="/health", tags=["health"])


class HealthResponse(BaseModel):
    status: str
    version: str
    environment: str


@router.get("", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    return HealthResponse(
        status="healthy",
        version=settings.app_version,
        environment=settings.environment,
    )


@router.get("/live")
async def liveness_check() -> dict[str, str]:
    return {"status": "alive"}
```

Replace `src/api/app.py` with:

```python
"""Thinkback FastAPI application factory."""

from time import perf_counter

from fastapi import FastAPI, Request, Response
from loguru import logger

from api.health import router as health_router
from infra.config import settings
from infra.logging import clear_trace_id, set_trace_id


def _register_request_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def trace_and_access_log(request: Request, call_next) -> Response:
        incoming_trace_id = request.headers.get("X-Trace-Id") or request.headers.get(
            "X-Request-Id"
        )
        trace_id = set_trace_id(incoming_trace_id.strip() if incoming_trace_id else None)
        started_at = perf_counter()
        response: Response | None = None
        try:
            response = await call_next(request)
            response.headers["X-Trace-Id"] = trace_id
            return response
        finally:
            duration_ms = (perf_counter() - started_at) * 1000
            logger.info(
                "HTTP request completed",
                method=request.method,
                path=request.url.path,
                status_code=response.status_code if response else 500,
                duration_ms=round(duration_ms, 2),
            )
            clear_trace_id()


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description="Thinkback memory service",
        debug=settings.debug,
    )
    _register_request_middleware(app)
    app.include_router(health_router)

    @app.get("/")
    async def root() -> dict[str, str]:
        return {
            "name": settings.app_name,
            "version": settings.app_version,
            "status": "running",
            "scope": "memory-service",
        }

    return app


app = create_app()

__all__ = ["app", "create_app"]
```

- [ ] **Step 5: Run tests**

Run:

```bash
PYTHONPATH=src poetry run pytest test/unit/test_config.py test/unit/test_api.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/infra src/api test/unit/test_api.py test/unit/test_config.py
git commit -m "feat: add runtime settings and request tracing"
```

---

## Task 3: Readiness Boundaries for PostgreSQL, Redis, and Qdrant

**Files:**
- Create: `src/infra/database/__init__.py`
- Create: `src/infra/database/base.py`
- Create: `src/infra/database/engine.py`
- Create: `src/infra/cache/__init__.py`
- Create: `src/infra/cache/redis_client.py`
- Create: `src/infra/vectorstore/__init__.py`
- Create: `src/infra/vectorstore/qdrant_client.py`
- Create: `src/infra/readiness.py`
- Create: `src/api/dependencies.py`
- Modify: `src/api/health.py`
- Modify: `test/unit/test_api.py`

- [ ] **Step 1: Write failing readiness tests**

Append these tests to `test/unit/test_api.py`:

```python
def test_readiness_endpoint_returns_ready(client, monkeypatch) -> None:
    async def fake_collect_readiness():
        return {
            "status": "ready",
            "dependencies": {
                "database": {"status": "ready", "detail": "ok"},
                "redis": {"status": "ready", "detail": "ok"},
                "qdrant": {"status": "ready", "detail": "ok"},
            },
        }

    monkeypatch.setattr("api.health.collect_readiness", fake_collect_readiness)

    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"


def test_readiness_endpoint_returns_503_when_dependency_is_not_ready(client, monkeypatch) -> None:
    async def fake_collect_readiness():
        return {
            "status": "not_ready",
            "dependencies": {
                "database": {"status": "ready", "detail": "ok"},
                "redis": {"status": "not_ready", "detail": "connection refused"},
                "qdrant": {"status": "ready", "detail": "ok"},
            },
        }

    monkeypatch.setattr("api.health.collect_readiness", fake_collect_readiness)

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["dependencies"]["redis"]["status"] == "not_ready"
```

- [ ] **Step 2: Run readiness tests and verify they fail**

Run:

```bash
PYTHONPATH=src poetry run pytest test/unit/test_api.py::test_readiness_endpoint_returns_ready test/unit/test_api.py::test_readiness_endpoint_returns_503_when_dependency_is_not_ready -q
```

Expected: FAIL with 404 for `/health/ready` or missing `collect_readiness`.

- [ ] **Step 3: Add infrastructure client boundaries**

Create `src/infra/database/__init__.py`:

```python
"""Database infrastructure."""
```

Create `src/infra/database/base.py`:

```python
"""SQLAlchemy declarative base."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
```

Create `src/infra/database/engine.py`:

```python
"""Async database engine and readiness helpers."""

from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from infra.config import settings

engine = create_async_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session


async def check_database() -> dict[str, str]:
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as exc:
        return {"status": "not_ready", "detail": str(exc)}
    return {"status": "ready", "detail": "ok"}
```

Create `src/infra/cache/__init__.py`:

```python
"""Cache infrastructure."""
```

Create `src/infra/cache/redis_client.py`:

```python
"""Redis client and readiness helpers."""

from redis.asyncio import Redis

from infra.config import settings

_redis_client: Redis | None = None


def get_redis_client() -> Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = Redis.from_url(settings.redis_url, decode_responses=True)
    return _redis_client


async def check_redis() -> dict[str, str]:
    try:
        await get_redis_client().ping()
    except Exception as exc:
        return {"status": "not_ready", "detail": str(exc)}
    return {"status": "ready", "detail": "ok"}
```

Create `src/infra/vectorstore/__init__.py`:

```python
"""Vector store infrastructure."""
```

Create `src/infra/vectorstore/qdrant_client.py`:

```python
"""Qdrant client and readiness helpers."""

from __future__ import annotations

import asyncio

from qdrant_client import QdrantClient

from infra.config import settings

_qdrant_client: QdrantClient | None = None


def get_qdrant_client() -> QdrantClient:
    global _qdrant_client
    if _qdrant_client is None:
        kwargs: dict[str, str] = {"url": settings.qdrant_url}
        if settings.qdrant_api_key:
            kwargs["api_key"] = settings.qdrant_api_key
        _qdrant_client = QdrantClient(**kwargs)
    return _qdrant_client


async def check_qdrant() -> dict[str, str]:
    try:
        await asyncio.to_thread(get_qdrant_client().get_collections)
    except Exception as exc:
        return {"status": "not_ready", "detail": str(exc)}
    return {"status": "ready", "detail": "ok"}
```

Create `src/infra/readiness.py`:

```python
"""Readiness aggregation for runtime dependencies."""

from infra.cache.redis_client import check_redis
from infra.database.engine import check_database
from infra.vectorstore.qdrant_client import check_qdrant


async def collect_readiness() -> dict[str, object]:
    dependencies = {
        "database": await check_database(),
        "redis": await check_redis(),
        "qdrant": await check_qdrant(),
    }
    status = (
        "ready"
        if all(item["status"] == "ready" for item in dependencies.values())
        else "not_ready"
    )
    return {"status": status, "dependencies": dependencies}
```

Create `src/api/dependencies.py`:

```python
"""API dependency accessors."""

from infra.config import Settings, settings


def get_settings() -> Settings:
    return settings
```

- [ ] **Step 4: Add readiness route**

Replace `src/api/health.py` with:

```python
"""Health and probe routes."""

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from infra.config import settings
from infra.readiness import collect_readiness

router = APIRouter(prefix="/health", tags=["health"])


class HealthResponse(BaseModel):
    status: str
    version: str
    environment: str


@router.get("", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    return HealthResponse(
        status="healthy",
        version=settings.app_version,
        environment=settings.environment,
    )


@router.get("/ready")
async def readiness_check() -> JSONResponse:
    payload = await collect_readiness()
    status_code = 200 if payload["status"] == "ready" else 503
    return JSONResponse(status_code=status_code, content=payload)


@router.get("/live")
async def liveness_check() -> dict[str, str]:
    return {"status": "alive"}
```

- [ ] **Step 5: Run readiness tests**

Run:

```bash
PYTHONPATH=src poetry run pytest test/unit/test_api.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/api src/infra test/unit/test_api.py
git commit -m "feat: add infrastructure readiness checks"
```

---

## Task 4: Celery and Memory Boundaries

**Files:**
- Create: `src/infra/tasks/__init__.py`
- Create: `src/infra/tasks/celery_app.py`
- Create: `src/memory/__init__.py`
- Create: `src/memory/application/__init__.py`
- Create: `src/memory/domain/__init__.py`
- Create: `src/memory/mem0_client.py`
- Create: `src/memory/service.py`
- Create: `src/memory/tasks.py`
- Create: `test/unit/test_runtime_boundaries.py`

- [ ] **Step 1: Write failing boundary tests**

Create `test/unit/test_runtime_boundaries.py`:

```python
from infra.config import Settings


def test_celery_app_registers_diagnostic_memory_task() -> None:
    from infra.tasks.celery_app import celery_app

    assert celery_app.main == "thinkback"
    assert "memory.diagnostics.ping" in celery_app.tasks


def test_mem0_config_builder_targets_qdrant_memory_collection() -> None:
    from memory.mem0_client import build_mem0_config

    settings = Settings()
    config = build_mem0_config(settings=settings)

    assert config["vector_store"]["provider"] == "qdrant"
    assert config["vector_store"]["config"]["collection_name"] == "thinkback_memories"
    assert config["llm"]["provider"] == "openai"
    assert config["embedder"]["provider"] == "openai"


def test_memory_service_boundary_has_no_public_workflows() -> None:
    from memory.service import MemoryService

    service = MemoryService()

    assert service.name == "thinkback-memory"
```

- [ ] **Step 2: Run boundary tests and verify they fail**

Run:

```bash
PYTHONPATH=src poetry run pytest test/unit/test_runtime_boundaries.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'infra.tasks'` or `No module named 'memory'`.

- [ ] **Step 3: Add Celery app and memory package boundaries**

Create `src/infra/tasks/__init__.py`:

```python
"""Task queue infrastructure."""
```

Create `src/infra/tasks/celery_app.py`:

```python
"""Celery application for Thinkback workers."""

from __future__ import annotations

import os
import sys

from celery import Celery

from infra.config import settings


def _in_test_mode() -> bool:
    return bool(os.environ.get("PYTEST_CURRENT_TEST")) or "pytest" in sys.modules


celery_app = Celery(
    "thinkback",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["memory.tasks"],
)

celery_app.conf.update(
    task_time_limit=settings.celery_task_time_limit,
    task_soft_time_limit=settings.celery_task_soft_time_limit,
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
)

if _in_test_mode():
    celery_app.conf.broker_url = "memory://"
    celery_app.conf.result_backend = "cache+memory://"

import memory.tasks as _memory_tasks  # noqa: E402,F401
```

Create `src/memory/__init__.py`:

```python
"""Memory service package."""
```

Create `src/memory/application/__init__.py`:

```python
"""Memory application boundary."""
```

Create `src/memory/domain/__init__.py`:

```python
"""Memory domain boundary."""
```

Create `src/memory/mem0_client.py`:

```python
"""mem0 configuration boundary.

This module prepares configuration for the future memory implementation. It does
not perform add, search, delete, or other memory workflows.
"""

from __future__ import annotations

from typing import Any

from infra.config import Settings, settings as default_settings


def build_mem0_config(*, settings: Settings = default_settings) -> dict[str, Any]:
    qdrant_config: dict[str, Any] = {
        "url": settings.qdrant_url,
        "collection_name": settings.memory_qdrant_collection,
    }
    if settings.qdrant_api_key:
        qdrant_config["api_key"] = settings.qdrant_api_key

    return {
        "llm": {
            "provider": "openai",
            "config": {
                "model": settings.memory_llm_model,
                "api_key": settings.memory_llm_api_key,
                "openai_base_url": settings.memory_llm_base_url,
            },
        },
        "vector_store": {
            "provider": "qdrant",
            "config": qdrant_config,
        },
        "embedder": {
            "provider": "openai",
            "config": {
                "model": settings.memory_embedding_model,
                "api_key": settings.memory_embedding_api_key,
            },
        },
        "version": "v1.1",
    }


class Mem0Factory:
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._config = config or build_mem0_config()

    @property
    def config(self) -> dict[str, Any]:
        return self._config

    def create_memory(self) -> Any:
        from mem0 import Memory

        return Memory.from_config(self._config)
```

Create `src/memory/service.py`:

```python
"""Memory service boundary.

Business workflows are intentionally absent from the scaffold.
"""


class MemoryService:
    name = "thinkback-memory"
```

Create `src/memory/tasks.py`:

```python
"""Memory worker tasks."""

from infra.tasks.celery_app import celery_app


@celery_app.task(name="memory.diagnostics.ping")
def diagnostics_ping() -> dict[str, str]:
    return {"status": "ok", "service": "thinkback-memory"}
```

- [ ] **Step 4: Run boundary tests**

Run:

```bash
PYTHONPATH=src poetry run pytest test/unit/test_runtime_boundaries.py -q
```

Expected: PASS.

- [ ] **Step 5: Run API tests to check imports still work**

Run:

```bash
PYTHONPATH=src poetry run pytest test/unit/test_api.py test/unit/test_config.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/infra/tasks src/memory test/unit/test_runtime_boundaries.py
git commit -m "feat: add celery and mem0 boundaries"
```

---

## Task 5: Alembic Migration Scaffold

**Files:**
- Create: `alembic.ini`
- Create: `alembic/env.py`
- Create: `alembic/script.py.mako`
- Create: `alembic/versions/.gitkeep`
- Create: `script/db_migrate.py`
- Modify: `test/unit/test_scaffold_files.py`

- [ ] **Step 1: Write failing migration scaffold tests**

Create `test/unit/test_scaffold_files.py`:

```python
from pathlib import Path


def test_alembic_scaffold_wires_sqlalchemy_metadata() -> None:
    env_py = Path("alembic/env.py")
    alembic_ini = Path("alembic.ini")

    assert env_py.exists()
    assert alembic_ini.exists()
    content = env_py.read_text(encoding="utf-8")
    assert "from infra.database.base import Base" in content
    assert "target_metadata = Base.metadata" in content


def test_db_migration_script_exists() -> None:
    script = Path("script/db_migrate.py")

    assert script.exists()
    assert "alembic" in script.read_text(encoding="utf-8")
```

- [ ] **Step 2: Run migration scaffold tests and verify they fail**

Run:

```bash
PYTHONPATH=src poetry run pytest test/unit/test_scaffold_files.py -q
```

Expected: FAIL because `alembic/env.py` and `script/db_migrate.py` do not exist.

- [ ] **Step 3: Add Alembic files**

Create `alembic.ini`:

```ini
[alembic]
script_location = alembic
prepend_sys_path = src
sqlalchemy.url = postgresql+asyncpg://postgres:postgres@localhost:5432/thinkback

[post_write_hooks]

[loggers]
keys = root,sqlalchemy,alembic

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARN
handlers = console
qualname =

[logger_sqlalchemy]
level = WARN
handlers =
qualname = sqlalchemy.engine

[logger_alembic]
level = INFO
handlers =
qualname = alembic

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
datefmt = %H:%M:%S
```

Create `alembic/env.py`:

```python
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from infra.config import settings
from infra.database.base import Base

config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    import asyncio

    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
```

Create `alembic/script.py.mako`:

```mako
"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

${imports if imports else ""}

revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
```

Create `alembic/versions/.gitkeep`:

```text
```

- [ ] **Step 4: Add database migration helper script**

Create `script/db_migrate.py`:

```python
"""Small Alembic command wrapper for local development."""

from __future__ import annotations

import argparse
import subprocess
import sys


def run_alembic(args: list[str]) -> int:
    return subprocess.call(["alembic", *args])


def main() -> int:
    parser = argparse.ArgumentParser(description="Thinkback database migration helper")
    parser.add_argument("command", choices=["upgrade", "downgrade", "current", "history"])
    parser.add_argument("revision", nargs="?", default="head")
    parsed = parser.parse_args()

    if parsed.command == "upgrade":
        return run_alembic(["upgrade", parsed.revision])
    if parsed.command == "downgrade":
        return run_alembic(["downgrade", parsed.revision])
    if parsed.command == "current":
        return run_alembic(["current", "-v"])
    return run_alembic(["history", "--verbose"])


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Run migration scaffold tests and Alembic history**

Run:

```bash
PYTHONPATH=src poetry run pytest test/unit/test_scaffold_files.py -q
PYTHONPATH=src poetry run alembic history
```

Expected: pytest PASS. `alembic history` exits 0 and prints no revisions.

- [ ] **Step 6: Commit**

```bash
git add alembic alembic.ini script test/unit/test_scaffold_files.py
git commit -m "feat: add alembic migration scaffold"
```

---

## Task 6: Local Runtime Assets

**Files:**
- Create: `.dockerignore`
- Create: `.env.example`
- Create: `.pre-commit-config.yaml`
- Create: `Dockerfile`
- Create: `Makefile`
- Create: `docker-compose.yml`
- Modify: `README.md`
- Modify: `test/unit/test_scaffold_files.py`

- [ ] **Step 1: Add failing tests for local runtime assets**

Append these tests to `test/unit/test_scaffold_files.py`:

```python
def test_compose_declares_expected_services_without_object_storage() -> None:
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")

    for service in ["app", "worker", "postgres", "redis", "qdrant"]:
        assert f"  {service}:" in compose
    assert "minio" not in compose.lower()


def test_makefile_declares_core_commands() -> None:
    makefile = Path("Makefile").read_text(encoding="utf-8")

    for target in ["install:", "dev:", "run:", "worker:", "test:", "lint:", "format:"]:
        assert target in makefile


def test_env_example_declares_runtime_settings() -> None:
    env_example = Path(".env.example").read_text(encoding="utf-8")

    for variable in [
        "POSTGRES_HOST=",
        "REDIS_HOST=",
        "QDRANT_URL=",
        "CELERY_BROKER_URL=",
        "MEMORY_QDRANT_COLLECTION=",
    ]:
        assert variable in env_example
```

- [ ] **Step 2: Run asset tests and verify they fail**

Run:

```bash
PYTHONPATH=src poetry run pytest test/unit/test_scaffold_files.py -q
```

Expected: FAIL because Docker Compose, Makefile, and `.env.example` are missing.

- [ ] **Step 3: Add local runtime files**

Create `.dockerignore`:

```text
.git
.idea
.venv
.pytest_cache
.ruff_cache
.mypy_cache
__pycache__
*.pyc
htmlcov
.coverage
.env
dist
build
```

Create `.env.example`:

```dotenv
APP_NAME=Thinkback
APP_VERSION=0.1.0
ENVIRONMENT=development
DEBUG=false
LOG_LEVEL=INFO

POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_USER=postgres
POSTGRES_PASSWORD=postgres
POSTGRES_DATABASE=thinkback

REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_DB=0
REDIS_PASSWORD=

CELERY_BROKER_URL=redis://localhost:6379/0
CELERY_RESULT_BACKEND=redis://localhost:6379/1

QDRANT_URL=http://localhost:6333
QDRANT_API_KEY=
MEMORY_QDRANT_COLLECTION=thinkback_memories

MEMORY_LLM_MODEL=gpt-4o-mini
MEMORY_LLM_BASE_URL=https://api.openai.com/v1
MEMORY_LLM_API_KEY=
MEMORY_EMBEDDING_MODEL=text-embedding-3-small
MEMORY_EMBEDDING_API_KEY=
```

Create `.pre-commit-config.yaml`:

```yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.8.0
    hooks:
      - id: ruff
        args: [--fix]
      - id: ruff-format
```

Create `Dockerfile`:

```dockerfile
FROM python:3.11-slim AS builder

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    POETRY_VERSION=1.8.0 \
    POETRY_NO_INTERACTION=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install "poetry==${POETRY_VERSION}"

WORKDIR /app

COPY pyproject.toml poetry.lock ./

RUN poetry config virtualenvs.create false \
    && poetry install --without dev --no-root

FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src

RUN useradd --create-home --uid 1000 app

WORKDIR /app

COPY --from=builder /usr/local /usr/local
COPY src/ /app/src/
COPY alembic/ /app/alembic/
COPY alembic.ini /app/alembic.ini

RUN chown -R app:app /app

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=5).read()"

CMD ["uvicorn", "--app-dir", "src", "api.app:app", "--host", "0.0.0.0", "--port", "8000"]
```

Create `docker-compose.yml`:

```yaml
services:
  app:
    build:
      context: .
      dockerfile: Dockerfile
    container_name: thinkback-app
    ports:
      - "8000:8000"
    environment:
      PYTHONPATH: /app/src
      ENVIRONMENT: development
      POSTGRES_HOST: postgres
      REDIS_HOST: redis
      QDRANT_URL: http://qdrant:6333
      CELERY_BROKER_URL: redis://redis:6379/0
      CELERY_RESULT_BACKEND: redis://redis:6379/1
    env_file:
      - .env
    depends_on:
      - postgres
      - redis
      - qdrant
    networks:
      - thinkback-network

  worker:
    build:
      context: .
      dockerfile: Dockerfile
    container_name: thinkback-worker
    command: ["celery", "-A", "infra.tasks.celery_app.celery_app", "worker", "-l", "info"]
    environment:
      PYTHONPATH: /app/src
      ENVIRONMENT: development
      POSTGRES_HOST: postgres
      REDIS_HOST: redis
      QDRANT_URL: http://qdrant:6333
      CELERY_BROKER_URL: redis://redis:6379/0
      CELERY_RESULT_BACKEND: redis://redis:6379/1
    env_file:
      - .env
    depends_on:
      - postgres
      - redis
      - qdrant
    networks:
      - thinkback-network

  postgres:
    image: postgres:18.3
    container_name: thinkback-postgres
    ports:
      - "5432:5432"
    environment:
      POSTGRES_USER: ${POSTGRES_USER:-postgres}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-postgres}
      POSTGRES_DB: ${POSTGRES_DATABASE:-thinkback}
    volumes:
      - postgres-data:/var/lib/postgresql/data
    networks:
      - thinkback-network

  redis:
    image: redis:7.4.8-alpine
    container_name: thinkback-redis
    ports:
      - "6379:6379"
    command: redis-server --appendonly yes
    volumes:
      - redis-data:/data
    networks:
      - thinkback-network

  qdrant:
    image: qdrant/qdrant:v1.17.1
    container_name: thinkback-qdrant
    ports:
      - "6333:6333"
      - "6334:6334"
    volumes:
      - qdrant-data:/qdrant/storage
    networks:
      - thinkback-network

volumes:
  postgres-data:
  redis-data:
  qdrant-data:

networks:
  thinkback-network:
    driver: bridge
```

Create `Makefile`:

```makefile
.PHONY: help install dev test lint format run worker docker-build docker-up docker-down db-upgrade db-downgrade db-status db-history db-revision clean

help:
	@echo "Thinkback - memory service"
	@echo "  make install     - install runtime dependencies"
	@echo "  make dev         - install dev dependencies and pre-commit"
	@echo "  make run         - run FastAPI app"
	@echo "  make worker      - run Celery worker"
	@echo "  make test        - run tests"
	@echo "  make lint        - run Ruff and mypy"
	@echo "  make format      - format Python code"
	@echo "  make docker-up   - start local runtime stack"
	@echo "  make docker-down - stop local runtime stack"

install:
	poetry install --without dev

dev:
	poetry install
	poetry run pre-commit install

test:
	PYTHONPATH=src poetry run pytest

lint:
	poetry run ruff check src test
	poetry run mypy src

format:
	poetry run ruff check --fix src test
	poetry run ruff format src test

run:
	poetry run uvicorn --app-dir src api.app:app --reload --host 0.0.0.0 --port 8000

worker:
	PYTHONPATH=src poetry run celery -A infra.tasks.celery_app.celery_app worker -l info

docker-build:
	docker build -t thinkback:latest -f Dockerfile .

docker-up:
	docker compose up -d

docker-down:
	docker compose down

db-upgrade:
	PYTHONPATH=src poetry run alembic upgrade head

db-downgrade:
	PYTHONPATH=src poetry run alembic downgrade -1

db-status:
	PYTHONPATH=src poetry run alembic current -v

db-history:
	PYTHONPATH=src poetry run alembic history --verbose

db-revision:
ifndef MSG
	@echo "Usage: make db-revision MSG='create baseline'"
	@exit 1
endif
	PYTHONPATH=src poetry run alembic revision --autogenerate -m "$(MSG)"

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".ruff_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".mypy_cache" -exec rm -rf {} + 2>/dev/null || true
	rm -rf dist build htmlcov .coverage
```

- [ ] **Step 4: Update README**

Replace `README.md` with:

```markdown
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
```

- [ ] **Step 5: Run asset tests and Compose config validation**

Run:

```bash
PYTHONPATH=src poetry run pytest test/unit/test_scaffold_files.py -q
docker compose config >/tmp/thinkback-compose.yml
```

Expected: pytest PASS. `docker compose config` exits 0.

- [ ] **Step 6: Commit**

```bash
git add .dockerignore .env.example .pre-commit-config.yaml Dockerfile Makefile docker-compose.yml README.md test/unit/test_scaffold_files.py
git commit -m "feat: add local runtime assets"
```

---

## Task 7: Kubernetes Deployment Assets

**Files:**
- Create: `k8s/README.md`
- Create: `k8s/configmap.yaml`
- Create: `k8s/deployment-api.yaml`
- Create: `k8s/deployment-worker.yaml`
- Create: `k8s/secret.example.yaml`
- Create: `k8s/service.yaml`
- Modify: `test/unit/test_scaffold_files.py`

- [ ] **Step 1: Add failing Kubernetes asset tests**

Append these tests to `test/unit/test_scaffold_files.py`:

```python
def test_k8s_manifests_cover_api_worker_and_service() -> None:
    api_deployment = Path("k8s/deployment-api.yaml").read_text(encoding="utf-8")
    worker_deployment = Path("k8s/deployment-worker.yaml").read_text(encoding="utf-8")
    service = Path("k8s/service.yaml").read_text(encoding="utf-8")

    assert "name: thinkback-api" in api_deployment
    assert "path: /health/ready" in api_deployment
    assert "path: /health/live" in api_deployment
    assert "name: thinkback-worker" in worker_deployment
    assert "celery" in worker_deployment
    assert "name: thinkback" in service


def test_k8s_config_and_secret_examples_include_runtime_settings() -> None:
    configmap = Path("k8s/configmap.yaml").read_text(encoding="utf-8")
    secret = Path("k8s/secret.example.yaml").read_text(encoding="utf-8")

    assert "QDRANT_URL" in configmap
    assert "CELERY_BROKER_URL" in configmap
    assert "POSTGRES_PASSWORD" in secret
    assert "MEMORY_LLM_API_KEY" in secret
```

- [ ] **Step 2: Run Kubernetes asset tests and verify they fail**

Run:

```bash
PYTHONPATH=src poetry run pytest test/unit/test_scaffold_files.py::test_k8s_manifests_cover_api_worker_and_service test/unit/test_scaffold_files.py::test_k8s_config_and_secret_examples_include_runtime_settings -q
```

Expected: FAIL because `k8s` manifests do not exist.

- [ ] **Step 3: Add Kubernetes manifests**

Create `k8s/configmap.yaml`:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: thinkback-config
data:
  APP_NAME: Thinkback
  APP_VERSION: "0.1.0"
  ENVIRONMENT: production
  LOG_LEVEL: INFO
  POSTGRES_HOST: postgres
  POSTGRES_PORT: "5432"
  POSTGRES_USER: postgres
  POSTGRES_DATABASE: thinkback
  REDIS_HOST: redis
  REDIS_PORT: "6379"
  REDIS_DB: "0"
  QDRANT_URL: http://qdrant:6333
  MEMORY_QDRANT_COLLECTION: thinkback_memories
  CELERY_BROKER_URL: redis://redis:6379/0
  CELERY_RESULT_BACKEND: redis://redis:6379/1
```

Create `k8s/secret.example.yaml`:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: thinkback-secret
type: Opaque
stringData:
  POSTGRES_PASSWORD: postgres
  REDIS_PASSWORD: ""
  QDRANT_API_KEY: ""
  MEMORY_LLM_API_KEY: ""
  MEMORY_EMBEDDING_API_KEY: ""
```

Create `k8s/deployment-api.yaml`:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: thinkback-api
  labels:
    app: thinkback
    component: api
spec:
  replicas: 2
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxUnavailable: 0
      maxSurge: 1
  selector:
    matchLabels:
      app: thinkback
      component: api
  template:
    metadata:
      labels:
        app: thinkback
        component: api
    spec:
      securityContext:
        runAsNonRoot: true
      containers:
        - name: thinkback-api
          image: thinkback:0.1.0
          imagePullPolicy: IfNotPresent
          ports:
            - containerPort: 8000
          env:
            - name: PYTHONPATH
              value: /app/src
          envFrom:
            - configMapRef:
                name: thinkback-config
            - secretRef:
                name: thinkback-secret
          readinessProbe:
            httpGet:
              path: /health/ready
              port: 8000
            initialDelaySeconds: 5
            periodSeconds: 10
          livenessProbe:
            httpGet:
              path: /health/live
              port: 8000
            initialDelaySeconds: 15
            periodSeconds: 20
```

Create `k8s/deployment-worker.yaml`:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: thinkback-worker
  labels:
    app: thinkback
    component: worker
spec:
  replicas: 1
  selector:
    matchLabels:
      app: thinkback
      component: worker
  template:
    metadata:
      labels:
        app: thinkback
        component: worker
    spec:
      securityContext:
        runAsNonRoot: true
      containers:
        - name: thinkback-worker
          image: thinkback:0.1.0
          imagePullPolicy: IfNotPresent
          command:
            - celery
            - -A
            - infra.tasks.celery_app.celery_app
            - worker
            - -l
            - info
          env:
            - name: PYTHONPATH
              value: /app/src
          envFrom:
            - configMapRef:
                name: thinkback-config
            - secretRef:
                name: thinkback-secret
```

Create `k8s/service.yaml`:

```yaml
apiVersion: v1
kind: Service
metadata:
  name: thinkback
  labels:
    app: thinkback
spec:
  type: ClusterIP
  selector:
    app: thinkback
    component: api
  ports:
    - name: http
      port: 8000
      targetPort: 8000
```

Create `k8s/README.md`:

```markdown
# Thinkback Kubernetes Assets

These manifests provide a baseline API and worker deployment for Thinkback.
They assume PostgreSQL, Redis, and Qdrant are available in the cluster or through
managed services addressed by the ConfigMap.

## Build Image

```bash
docker build -t thinkback:0.1.0 .
```

## Apply Runtime Settings

```bash
kubectl apply -f k8s/configmap.yaml
kubectl apply -f k8s/secret.example.yaml
```

Replace values in `secret.example.yaml` before using it outside a local cluster.

## Deploy

```bash
kubectl apply -f k8s/service.yaml
kubectl apply -f k8s/deployment-api.yaml
kubectl apply -f k8s/deployment-worker.yaml
```

## Verify

```bash
kubectl rollout status deployment/thinkback-api
kubectl rollout status deployment/thinkback-worker
kubectl port-forward service/thinkback 8000:8000
curl http://127.0.0.1:8000/health/live
curl http://127.0.0.1:8000/health/ready
```
```

- [ ] **Step 4: Run Kubernetes asset tests**

Run:

```bash
PYTHONPATH=src poetry run pytest test/unit/test_scaffold_files.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add k8s test/unit/test_scaffold_files.py
git commit -m "feat: add kubernetes deployment assets"
```

---

## Task 8: Final Verification and Cleanup

**Files:**
- Modify only files needed to fix verification failures.

- [ ] **Step 1: Run formatting**

Run:

```bash
poetry run ruff format src test
```

Expected: command exits 0.

- [ ] **Step 2: Run lint**

Run:

```bash
poetry run ruff check src test
```

Expected: command exits 0.

- [ ] **Step 3: Run type checks**

Run:

```bash
poetry run mypy src
```

Expected: command exits 0.

- [ ] **Step 4: Run full test suite**

Run:

```bash
PYTHONPATH=src poetry run pytest
```

Expected: all tests pass.

- [ ] **Step 5: Validate deployment configuration syntax**

Run:

```bash
docker compose config >/tmp/thinkback-compose.yml
```

Expected: command exits 0.

- [ ] **Step 6: Review git status**

Run:

```bash
git status --short
```

Expected: only intended scaffold files are modified or untracked. Existing `.idea/` remains untracked and is not committed.

- [ ] **Step 7: Commit verification fixes if any were needed**

If formatting or verification changed files, run:

```bash
git add src test pyproject.toml poetry.lock alembic alembic.ini script Dockerfile Makefile docker-compose.yml .dockerignore .env.example .pre-commit-config.yaml README.md k8s
git commit -m "chore: verify scaffold baseline"
```

Expected: commit is created only if there are staged changes.

---

## Self-Review Notes

- Spec coverage: API shell, settings, PostgreSQL, Redis, Qdrant, Celery, mem0 boundary, Alembic, Docker Compose, Kubernetes, and tests are covered.
- Scope control: memory workflows are absent; only boundary modules and diagnostic worker bootstrap exist.
- Placeholder scan: this plan contains explicit file contents and commands, with no unresolved implementation markers.
- Type consistency: settings names match Docker Compose, Kubernetes ConfigMap, and tests.
