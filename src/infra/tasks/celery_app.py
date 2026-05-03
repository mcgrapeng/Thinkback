"""Celery application for Thinkback workers."""

from __future__ import annotations

import os
import sys

from celery import Celery

from infra.config import settings


def _in_test_mode() -> bool:
    return bool(os.environ.get("PYTEST_CURRENT_TEST")) or "pytest" in sys.modules


celery_app = Celery(
    "thinkback",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["memory.tasks"],
)

celery_app.conf.update(
    task_time_limit=settings.celery_task_time_limit,
    task_soft_time_limit=settings.celery_task_soft_time_limit,
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
)

if _in_test_mode():
    celery_app.conf.broker_url = "memory://"
    celery_app.conf.result_backend = "cache+memory://"

import memory.tasks as _memory_tasks  # noqa: E402,F401
