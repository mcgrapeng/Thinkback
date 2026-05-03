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
