"""Memory worker tasks."""

from infra.tasks.celery_app import celery_app


@celery_app.task(name="memory.diagnostics.ping")
def diagnostics_ping() -> dict[str, str]:
    return {"status": "ok", "service": "thinkback-memory"}
