from infra.config import Settings


def test_settings_defaults_are_thinkback_baseline() -> None:
    settings = Settings(_env_file=None)

    assert settings.app_name == "Thinkback"
    assert settings.app_version == "0.1.0"
    assert settings.environment == "development"
    assert settings.database_backend == "postgresql"
    assert settings.postgres_database == "liaoriver_memory"
    assert settings.memory_backend == "redis"
    assert settings.qdrant_url == "https://qdrant.example.internal"
    assert settings.qdrant_api_key == ""
    assert settings.openai_api_key == ""
    assert settings.memory_openai_base_url == ""
    assert settings.memory_qdrant_collection == "memories_qwen_1024"
    assert settings.memory_embedding_dims == 1024
    assert settings.memory_llm_model == "qwen3.5-flash"
    assert settings.memory_embedding_model == "text-embedding-v4"
    assert settings.mem0_history_db_path == ".mem0/history.db"
    assert settings.memory_l3_write_mode == "async"
    assert settings.memory_l3_executor_workers == 16
    assert settings.memory_l3_max_pending_tasks == 256
    assert settings.memory_l3_queue_wait_seconds == 5.0
    assert settings.memory_api_worker_limit == 8
    assert settings.readiness_timeout_seconds == 30.0

    assert not hasattr(settings, "mem0_api_url")
    assert not hasattr(settings, "mem0_api_key")
    assert not hasattr(settings, "mem0_http_timeout_seconds")
    assert not hasattr(settings, "mem0_backend_mode")
    assert not hasattr(settings, "memory_llm_api_key")
    assert not hasattr(settings, "memory_embedding_api_key")
    assert not hasattr(settings, "celery_broker_url")
    assert not hasattr(settings, "celery_result_backend")


def test_settings_derived_urls() -> None:
    settings = Settings(_env_file=None)

    assert settings.database_url == (
        "postgresql+asyncpg://postgres:postgres@localhost:5432/liaoriver_memory"
    )
    assert settings.redis_url == "redis://localhost:6379/0"


def test_redis_url_encodes_password() -> None:
    settings = Settings(_env_file=None, redis_password="p@ss/word")

    assert settings.redis_url == "redis://:p%40ss%2Fword@localhost:6379/0"


def test_settings_support_remote_qdrant_endpoint() -> None:
    settings = Settings(
        qdrant_url="https://qdrant.example.internal",
        qdrant_api_key="qdrant-secret",
    )

    assert settings.qdrant_url == "https://qdrant.example.internal"
    assert settings.qdrant_api_key == "qdrant-secret"
