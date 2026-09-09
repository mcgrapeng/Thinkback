FROM python:3.12-slim AS builder

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    POETRY_VERSION=2.3.1 \
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

FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    MEM0_HISTORY_DB_PATH=/tmp/innies-memory/mem0/history.db

RUN useradd --create-home --uid 1000 app

WORKDIR /app

COPY --from=builder /usr/local /usr/local
COPY src/ /app/src/
COPY alembic/ /app/alembic/
COPY alembic.ini /app/alembic.ini

RUN mkdir -p /tmp/innies-memory/mem0 \
    && chown -R app:app /app /tmp/innies-memory

USER app

EXPOSE 8000

CMD sh -c 'exec uvicorn --app-dir src innies_memory.api.app:app --host 0.0.0.0 --port ${PORT:-8000}'
