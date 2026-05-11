.PHONY: help install dev test lint format run debug-api debug-ready quality-real verify-local docker-build docker-up docker-down db-upgrade db-downgrade db-status db-history db-revision clean

-include .env
export

help:
	@echo "Thinkback - memory service"
	@echo "  make install     - install runtime dependencies"
	@echo "  make dev         - install dev dependencies and pre-commit"
	@echo "  make run         - run FastAPI app"
	@echo "  make debug-api   - run local API on 127.0.0.1:18082"
	@echo "  make debug-ready - check local readiness on 127.0.0.1:18082"
	@echo "  make quality-real - run real quality evaluation into docs/report"
	@echo "  make verify-local - run lint, mypy, tests, and compose config"
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
	poetry run ruff check src test script
	poetry run mypy src

format:
	poetry run ruff check --fix src test
	poetry run ruff format src test

run:
	poetry run uvicorn --app-dir src api.app:app --reload --host 0.0.0.0 --port 8000

# 中文注释：debug-api/quality-real 只在命令前临时覆盖本地调试变量；变量含义见 .env.example 和部署指南。
debug-api:
	POSTGRES_HOST=localhost POSTGRES_PORT=5432 POSTGRES_USER=postgres POSTGRES_PASSWORD=$${POSTGRES_PASSWORD:-postgres} POSTGRES_DATABASE=liaoriver_memory REDIS_HOST=localhost REDIS_PORT=6379 REDIS_DB=0 REDIS_PASSWORD=$${REDIS_PASSWORD:-} QDRANT_URL=http://localhost:6333 QDRANT_API_KEY= MEMORY_QDRANT_COLLECTION=memories_qwen_1024 MEMORY_EMBEDDING_DIMS=1024 MEMORY_L3_WRITE_MODE=sync PYTHONPATH=src poetry run uvicorn --app-dir src api.app:app --reload --host 127.0.0.1 --port 18082

debug-ready:
	curl -sS http://127.0.0.1:18082/health/ready

quality-real:
	POSTGRES_HOST=localhost POSTGRES_PORT=5432 POSTGRES_USER=postgres POSTGRES_PASSWORD=$${POSTGRES_PASSWORD:-postgres} POSTGRES_DATABASE=liaoriver_memory REDIS_HOST=localhost REDIS_PORT=6379 REDIS_DB=0 REDIS_PASSWORD=$${REDIS_PASSWORD:-} QDRANT_URL=http://localhost:6333 QDRANT_API_KEY= MEMORY_QDRANT_COLLECTION=memories_qwen_1024 MEMORY_EMBEDDING_DIMS=1024 MEMORY_L3_WRITE_MODE=sync THINKBACK_API_URL=http://127.0.0.1:18082 PYTHONPATH=src poetry run python script/run_real_mem0_quality_evaluation.py --docs-dir docs/report

verify-local:
	poetry run ruff check src test script
	poetry run mypy src
	PYTHONPATH=src poetry run pytest
	docker compose config >/tmp/thinkback-compose.yml

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
