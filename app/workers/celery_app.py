"""Celery application.

With CELERY_TASK_ALWAYS_EAGER=true (development and tests) tasks run inline in
the calling process and no broker is needed.
"""

from celery import Celery
from celery.schedules import crontab

from app.core.config import get_settings

settings = get_settings()

celery_app = Celery(
    "novalyn",
    broker=settings.broker_url or "memory://",
    backend=settings.celery_result_backend,
    include=["app.workers.notification_tasks", "app.workers.document_tasks", "app.workers.maintenance_tasks"],
)

celery_app.conf.update(
    task_always_eager=settings.celery_task_always_eager,
    task_eager_propagates=True,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    timezone="UTC",
    enable_utc=True,
    broker_connection_retry_on_startup=True,
    # Celery Beat: reminders hourly (each organization's day starts at its own midnight),
    # and a sweep of the notification outbox every minute in case a wake-up was lost.
    beat_schedule={
        "scan-expiries": {"task": "lifecycle.scan_expiries", "schedule": crontab(minute=5)},
        "dispatch-outbox": {"task": "notifications.dispatch_outbox", "schedule": 60.0},
        # Erase organizations past their deletion date, then remove expired records.
        "nightly-maintenance": {"task": "maintenance.nightly", "schedule": crontab(hour=3, minute=15)},
    },
)
