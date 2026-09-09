from innies_memory.infra.config import Settings

EMPTY_ENV_FILE = "/tmp/innies-memory-missing-test.env"


def test_settings_defaults_are_innies_memory_baseline(monkeypatch) -> None:
    for variable in [
        "OPENAI_API_KEY",
        "POSTGRES_DATABASE",
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "MILVUS_URL",
        "MILVUS_DATABASE",
        "MILVUS_USER",
        "MILVUS_PASSWORD",
        "MEMORY_LLM_BASE_URL",
        "MEMORY_EMBEDDING_BASE_URL",
        "MEMORY_EMBEDDING_API_KEY",
        "MEMORY_MILVUS_COLLECTION",
        "MEMORY_EMBEDDING_DIMS",
        "MEMORY_LLM_MODEL",
        "MEMORY_EMBEDDING_MODEL",
        "MEM0_HISTORY_DB_PATH",
        "MEMORY_BACKEND_MAX_CONCURRENT_CALLS",
        "MEMORY_L3_WRITE_MODE",
        "MEMORY_L3_EXECUTOR_WORKERS",
        "MEMORY_L3_MAX_PENDING_TASKS",
        "MEMORY_L3_QUEUE_WAIT_SECONDS",
        "MEMORY_API_WORKER_LIMIT",
        "MEMORY_API_WORKER_WAIT_SECONDS",
        "READINESS_TIMEOUT_SECONDS",
        "REDIS_HOST",
        "REDIS_PORT",
        "REDIS_DB",
        "REDIS_PASSWORD",
        "DATABASE_URL",
        "ENVIRONMENT",
    ]:
        monkeypatch.delenv(variable, raising=False)
    settings = Settings(_env_file=EMPTY_ENV_FILE)

    assert settings.app_name == "innies-memory"
    assert settings.app_version == "0.1.0"
    assert settings.environment == "development"
    assert settings.postgres_database == "innies-memory"
    assert settings.milvus_url == "http://localhost:19530"
    assert settings.milvus_database == "default"
    assert settings.milvus_user == ""
    assert settings.milvus_password == ""
    assert settings.openai_api_key == ""
    assert settings.memory_llm_base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert settings.memory_embedding_base_url == "http://embedding.example.internal:7345/v1"
    assert settings.memory_embedding_api_key == ""
    assert settings.memory_milvus_collection == "innies_memory"
    assert settings.memory_embedding_dims == 1024
    assert settings.memory_llm_model == "qwen-plus-latest"
    assert settings.memory_embedding_model == "zhiman-embedding"
    assert settings.mem0_history_db_path == ".mem0/history.db"
    assert settings.memory_l3_write_mode == "async"
    assert settings.memory_l3_executor_workers == 16
    assert settings.memory_l3_max_pending_tasks == 256
    assert settings.memory_l3_queue_wait_seconds == 5.0
    assert settings.memory_backend_max_concurrent_calls == 4
    assert settings.memory_api_worker_limit == 8
    assert settings.memory_api_worker_wait_seconds == 5.0
    assert settings.readiness_timeout_seconds == 3.0

    assert not hasattr(settings, "mem0_api_url")
    assert not hasattr(settings, "mem0_api_key")
    assert not hasattr(settings, "mem0_http_timeout_seconds")
    assert not hasattr(settings, "mem0_backend_mode")
    assert not hasattr(settings, "api_host")
    assert not hasattr(settings, "api_port")
    assert not hasattr(settings, "database_backend")
    assert not hasattr(settings, "memory_backend")
    assert not hasattr(settings, "memory_openai_base_url")
    assert not hasattr(settings, "memory_llm_api_key")
    assert not hasattr(settings, "qdrant_url")
    assert not hasattr(settings, "qdrant_api_key")
    assert not hasattr(settings, "memory_qdrant_collection")
    # Redis 已移除：短期记忆只落 PG，不允许再出现 redis / celery 配置面。
    for removed in (
        "redis_host",
        "redis_port",
        "redis_db",
        "redis_password",
        "redis_url",
        "celery_broker_url",
        "celery_result_backend",
        "celery_task_time_limit",
        "celery_task_soft_time_limit",
    ):
        assert not hasattr(settings, removed)


def test_settings_derived_urls(monkeypatch) -> None:
    for variable in [
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_DATABASE",
        "DATABASE_URL",
        "ENVIRONMENT",
    ]:
        monkeypatch.delenv(variable, raising=False)
    settings = Settings(_env_file=EMPTY_ENV_FILE, postgres_password="postgres")

    assert (
        settings.database_url
        == "postgresql+asyncpg://postgres:postgres@localhost:5432/innies-memory"
    )


def test_production_environment_does_not_read_local_dotenv(tmp_path, monkeypatch) -> None:
    (tmp_path / ".env").write_text(
        "APP_NAME=leaked-local-name\n"
        "POSTGRES_DATABASE=leaked-local-db\n"
        "OPENAI_API_KEY=leaked-local-key\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("APP_NAME", raising=False)
    monkeypatch.delenv("POSTGRES_DATABASE", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    settings = Settings()

    assert settings.environment == "production"
    assert settings.app_name == "innies-memory"
    assert settings.postgres_database == "innies-memory"
    assert settings.openai_api_key == ""


def test_settings_does_not_expose_legacy_memory_api_keys_field() -> None:
    """记忆服务是内部服务，不再持有 API key 鉴权字段。"""
    settings = Settings(_env_file=EMPTY_ENV_FILE)

    assert not hasattr(settings, "memory_api_keys")
    assert not hasattr(settings, "memory_api_key_set")


def test_settings_support_remote_milvus_endpoint() -> None:
    settings = Settings(
        milvus_url="http://milvus.example.internal:19530",
        milvus_database="innies",
        milvus_user="milvus-user",
        milvus_password="milvus-secret",
    )

    assert settings.milvus_url == "http://milvus.example.internal:19530"
    assert settings.milvus_database == "innies"
    assert settings.milvus_token == "milvus-user:milvus-secret"


def test_database_url_alias_works_from_env_file_without_os_environ(tmp_path, monkeypatch) -> None:
    """R-0 回归：.env 文件里的 DATABASE_URL 必须直接生效。

    修复前：字段名 raw_database_url 与 .env 键 DATABASE_URL 不匹配，只有
    os.environ（或 pymilvus import 副作用 load_dotenv）才能让它生效 ——
    配置是否被读取取决于 settings 单例创建时机，独立进程（alembic CLI、
    uvicorn 冷启动）会静默回落到错误的 postgres:postgres 默认值。
    """
    monkeypatch.delenv("DATABASE_URL", raising=False)
    env_file = tmp_path / "envalias.env"
    env_file.write_text(
        "DATABASE_URL=postgres://postgres:secret@db.example.internal:5432/innies-memory\n",
        encoding="utf-8",
    )
    settings = Settings(_env_file=str(env_file))

    assert settings.raw_database_url == (
        "postgres://postgres:secret@db.example.internal:5432/innies-memory"
    )
    assert settings.database_url == (
        "postgresql+asyncpg://postgres:secret@db.example.internal:5432/innies-memory"
    )
