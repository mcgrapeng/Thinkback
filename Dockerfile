FROM python:3.12-slim AS builder

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

COPY pyproject.toml uv.lock ./

RUN uv sync --frozen --no-dev --no-install-project

FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app/src \
    MEM0_HISTORY_DB_PATH=/tmp/thinkback/mem0/history.db

RUN useradd --create-home --uid 1000 app

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY src/ /app/src/
COPY alembic/ /app/alembic/
COPY alembic.ini /app/alembic.ini
# 记忆类型 profile 必须进镜像：容器里没有 profiles/ 时，namespace 的类型
# 注册表会静默回退到默认注册表，行业类型直接变成「未注册」。
COPY profiles/ /app/profiles/

RUN mkdir -p /tmp/thinkback/mem0 \
    && chown -R app:app /app /tmp/thinkback

USER app

EXPOSE 8000

CMD sh -c 'exec uvicorn --app-dir src thinkback.api.app:app --host 0.0.0.0 --port ${PORT:-8000}'
