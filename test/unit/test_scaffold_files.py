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

    for service in ["app", "worker", "postgres", "redis", "qdrant"]:
        assert f"  {service}:" in compose
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
        "MEMORY_QDRANT_COLLECTION=",
    ]:
        assert variable in env_example
