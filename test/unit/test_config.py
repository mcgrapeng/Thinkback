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

    assert (
        settings.database_url == "postgresql+asyncpg://postgres:postgres@localhost:5432/thinkback"
    )
    assert settings.redis_url == "redis://localhost:6379/0"
