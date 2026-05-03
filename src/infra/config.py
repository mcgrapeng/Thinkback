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
