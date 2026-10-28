"""限流额度必须可配置。

内网免鉴权 → 业务面按客户端 IP 分桶限流（默认 600/分钟、突发 50）。这个
默认值对生产是对的，但压测要测的是服务容量、不是限流器，所以额度必须能
按部署调高 —— 否则单 IP 压测会把 429 当成服务故障。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from thinkback.api.ratelimit import RateLimitMiddleware


def _app(rate_limit_per_minute: int, burst: int) -> FastAPI:
    app = FastAPI()
    app.add_middleware(
        RateLimitMiddleware,
        rate_limit_per_minute=rate_limit_per_minute,
        burst=burst,
    )

    @app.get("/v2/memory/ping")
    async def ping() -> dict[str, bool]:
        return {"ok": True}

    return app


def test_burst_exhausted_requests_are_rejected() -> None:
    """突发额度用完后必须 429（而不是无限放行）。

    速率取 1/分钟：突发额度要比补充速度快才测得到突发。原来的 600/分钟
    是 10 token/秒，三次请求之间就补回 0.5 个 token，第三个请求又被放行 ——
    结果取决于机器有多快，是条会随机红的用例。
    """
    with TestClient(_app(rate_limit_per_minute=1, burst=2)) as client:
        assert client.get("/v2/memory/ping").status_code == 200
        assert client.get("/v2/memory/ping").status_code == 200

        rejected = client.get("/v2/memory/ping")
        assert rejected.status_code == 429
        assert rejected.json()["error"]["code"] == "TB-1007"
        assert rejected.headers["X-RateLimit-Limit"] == "1"


def test_configured_limit_is_applied() -> None:
    """配置的额度生效：调高后不再被限流挡住。"""
    with TestClient(_app(rate_limit_per_minute=600_000, burst=10_000)) as client:
        for _ in range(20):
            assert client.get("/v2/memory/ping").status_code == 200


def test_settings_default_keeps_production_limits() -> None:
    """生产默认值不变：600/分钟、突发 50。"""
    from thinkback.infra.config import Settings

    settings = Settings()
    assert settings.memory_rate_limit_per_minute == 600
    assert settings.memory_rate_limit_burst == 50


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("memory_rate_limit_per_minute", 0),
        ("memory_rate_limit_burst", 0),
    ],
)
def test_invalid_limits_are_rejected(field: str, value: int) -> None:
    """非法额度必须报错：写 0 等于悄悄关掉限流。"""
    from pydantic import ValidationError

    from thinkback.infra.config import Settings

    with pytest.raises(ValidationError):
        Settings(**{field: value})
