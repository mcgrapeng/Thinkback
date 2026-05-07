from infra.config import Settings


def test_settings_defaults_are_thinkback_baseline() -> None:
    settings = Settings(_env_file=None)

    assert settings.app_name == "Thinkback"
    assert settings.app_version == "0.1.0"
    assert settings.environment == "development"
    assert settings.database_backend == "postgresql"
    assert settings.memory_backend == "redis"
    assert settings.qdrant_url == "https://qdrant.example.internal"
    assert settings.qdrant_api_key == ""
    assert settings.mem0_api_url == "https://mem0.example.internal"
    assert settings.mem0_http_timeout_seconds == 120.0

    assert not hasattr(settings, "mem0_backend_mode")
    assert not hasattr(settings, "memory_llm_api_key")
    assert not hasattr(settings, "memory_embedding_api_key")


def test_settings_derived_urls() -> None:
    settings = Settings()

    assert (
        settings.database_url == "postgresql+asyncpg://postgres:postgres@localhost:5432/thinkback"
    )
    assert settings.redis_url == "redis://localhost:6379/0"


def test_redis_url_encodes_password() -> None:
    settings = Settings(redis_password="p@ss/word")

    assert settings.redis_url == "redis://:p%40ss%2Fword@localhost:6379/0"


def test_settings_support_remote_qdrant_endpoint() -> None:
    settings = Settings(
        qdrant_url="https://qdrant.example.internal",
        qdrant_api_key="qdrant-secret",
    )

    assert settings.qdrant_url == "https://qdrant.example.internal"
    assert settings.qdrant_api_key == "qdrant-secret"
