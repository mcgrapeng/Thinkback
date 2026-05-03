"""API dependency accessors."""

from infra.config import Settings, settings


def get_settings() -> Settings:
    return settings
