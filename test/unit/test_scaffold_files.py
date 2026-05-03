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
