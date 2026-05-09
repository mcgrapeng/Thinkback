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


def test_compose_declares_expected_services_without_object_storage() -> None:
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")

    for service in ["app", "worker", "postgres", "redis"]:
        assert f"  {service}:" in compose
    assert "qdrant:" not in compose
    assert "QDRANT_URL: ${QDRANT_URL:-https://qdrant.example.internal}" in compose
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
        "OPENAI_API_KEY=",
        "MEMORY_OPENAI_BASE_URL=",
        "MEMORY_QDRANT_COLLECTION=",
        "MEMORY_EMBEDDING_DIMS=",
        "MEMORY_LLM_MODEL=",
        "MEMORY_EMBEDDING_MODEL=",
        "MEM0_HISTORY_DB_PATH=",
        "READINESS_TIMEOUT_SECONDS=",
        "MEMORY_L3_EXECUTOR_WORKERS=",
        "MEMORY_L3_MAX_PENDING_TASKS=",
        "MEMORY_L3_QUEUE_WAIT_SECONDS=",
    ]:
        assert variable in env_example
    assert "QDRANT_API_KEY=" in env_example
    assert "MEM0_API_URL=" not in env_example
    assert "MEM0_API_KEY=" not in env_example
    assert "MEM0_HTTP_TIMEOUT_SECONDS=" not in env_example
    assert "MEMORY_LLM_API_KEY=" not in env_example
    assert "QDRANT_URL=https://qdrant.example.internal" in env_example


def test_k8s_manifests_cover_api_worker_and_service() -> None:
    api_deployment = Path("k8s/deployment-api.yaml").read_text(encoding="utf-8")
    worker_deployment = Path("k8s/deployment-worker.yaml").read_text(encoding="utf-8")
    service = Path("k8s/service.yaml").read_text(encoding="utf-8")

    assert "name: thinkback-api" in api_deployment
    assert "path: /health/ready" in api_deployment
    assert "path: /health/live" in api_deployment
    assert "timeoutSeconds: 3" in api_deployment
    assert "name: thinkback-worker" in worker_deployment
    assert "celery" in worker_deployment
    assert "name: thinkback" in service


def test_k8s_config_and_secret_examples_include_runtime_settings() -> None:
    configmap = Path("k8s/configmap.yaml").read_text(encoding="utf-8")
    secret = Path("k8s/secret.example.yaml").read_text(encoding="utf-8")

    assert "QDRANT_URL" in configmap
    assert "QDRANT_API_KEY" in secret
    assert "MEM0_BACKEND_MODE" not in configmap
    assert "MEM0_API_URL" not in configmap
    assert "MEM0_HTTP_TIMEOUT_SECONDS" not in configmap
    assert "OPENAI_API_KEY" in secret
    assert "MEMORY_OPENAI_BASE_URL" in configmap
    assert "MEMORY_QDRANT_COLLECTION" in configmap
    assert "MEMORY_LLM_MODEL" in configmap
    assert "MEMORY_L3_EXECUTOR_WORKERS" in configmap
    assert "MEMORY_L3_MAX_PENDING_TASKS" in configmap
    assert "MEMORY_L3_QUEUE_WAIT_SECONDS" in configmap
    assert "READINESS_TIMEOUT_SECONDS" in configmap
    assert "CELERY_BROKER_URL" in configmap
    assert "POSTGRES_PASSWORD" in secret
    assert "MEM0_API_KEY" not in secret
    assert "MEMORY_LLM_API_KEY" not in secret
