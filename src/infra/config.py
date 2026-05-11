"""Runtime configuration for Thinkback."""

from functools import lru_cache
from typing import Literal
from urllib.parse import quote_plus

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # 中文注释：运行配置只读取 .env 一个模板来源；K8s 生产环境通过 ConfigMap/Secret 覆盖同名变量。
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
    # 中文注释：数据库名是业务口径，默认值必须与 Docker、K8s、CI 和文档保持一致。
    postgres_database: str = "liaoriver_memory"

    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: str = ""
    memory_backend: Literal["redis"] = "redis"
    qdrant_url: str = "https://qdrant.example.internal"
    qdrant_api_key: str = Field(default="", description="Shared Qdrant API key")
    openai_api_key: str = Field(default="", description="OpenAI API key for Mem0 Library")
    memory_openai_base_url: str = ""
    memory_qdrant_collection: str = "memories_qwen_1024"
    memory_embedding_dims: int = 1024
    memory_llm_model: str = "qwen3.5-flash"
    memory_embedding_model: str = "text-embedding-v4"
    mem0_history_db_path: str = ".mem0/history.db"
    memory_l3_write_mode: Literal["sync", "async"] = "async"
    # 中文注释：下面三项控制 API 进程内 L3 后台写入容量，不代表最终线上容量承诺。
    memory_l3_executor_workers: int = 16
    memory_l3_max_pending_tasks: int = 256
    memory_l3_queue_wait_seconds: float = 5.0
    memory_api_worker_limit: int = 8
    readiness_timeout_seconds: float = 30.0

    @field_validator("memory_l3_executor_workers")
    @classmethod
    def validate_memory_l3_executor_workers(cls, value: int) -> int:
        if value < 1:
            raise ValueError("memory_l3_executor_workers must be >= 1")
        return value

    @field_validator("memory_api_worker_limit")
    @classmethod
    def validate_memory_api_worker_limit(cls, value: int) -> int:
        if value < 1:
            raise ValueError("memory_api_worker_limit must be >= 1")
        return value

    @property
    def database_url(self) -> str:
        encoded_password = quote_plus(self.postgres_password) if self.postgres_password else ""
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{encoded_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_database}"
        )

    @property
    def redis_url(self) -> str:
        encoded_password = quote_plus(self.redis_password) if self.redis_password else ""
        auth_part = f":{encoded_password}@" if encoded_password else ""
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
