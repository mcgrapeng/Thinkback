from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient

from innies_memory.api import app as app_module
from innies_memory.api import dependencies as dependencies_module
from innies_memory.api import health as health_module
from innies_memory.api import memory as memory_module
from innies_memory.api.app import create_app
from innies_memory.infra import config as config_module
from innies_memory.infra import logging as logging_module
from innies_memory.infra import readiness as readiness_module
from innies_memory.infra.database import engine as engine_module


@pytest.fixture(autouse=True)
def isolate_runtime_settings(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    for variable in [
        "APP_NAME",
        "APP_VERSION",
        "ENVIRONMENT",
        "DEBUG",
        "LOG_LEVEL",
        "MEMORY_EMBEDDING_BASE_URL",
    ]:
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setitem(config_module.Settings.model_config, "env_file", None)
    config_module.get_settings.cache_clear()
    config_module.settings = config_module.get_settings()
    # disable embedded gRPC server in all HTTP tests to avoid port binding
    monkeypatch.setattr(config_module.settings, "grpc_enabled", False)
    monkeypatch.setattr(app_module, "settings", config_module.settings)
    monkeypatch.setattr(health_module, "settings", config_module.settings)
    monkeypatch.setattr(memory_module, "settings", config_module.settings)
    monkeypatch.setattr(dependencies_module, "settings", config_module.settings)
    monkeypatch.setattr(readiness_module, "settings", config_module.settings)
    monkeypatch.setattr(logging_module, "settings", config_module.settings)
    monkeypatch.setattr(engine_module, "settings", config_module.settings)


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    with TestClient(create_app()) as test_client:
        yield test_client
