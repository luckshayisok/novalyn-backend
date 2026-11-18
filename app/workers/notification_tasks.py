"""Notification tasks. Phase 1 has transactional email only."""

import logging

from app.integrations.email.base import EmailMessage
from app.integrations.email.senders import get_email_sender
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

MAX_RETRIES = 5


@celery_app.task(name="notifications.send_email", bind=True, max_retries=MAX_RETRIES)
def send_email_task(self, payload: dict) -> None:
    message = EmailMessage.from_dict(payload)
    try:
        get_email_sender().send(message)
    except Exception as exc:
        if celery_app.conf.task_always_eager:
            raise
        # 30s, 60s, 120s, ... capped at one hour.
        raise self.retry(exc=exc, countdown=min(30 * 2**self.request.retries, 3600)) from exc


def enqueue_email(message: EmailMessage) -> bool:
    """Queue an email after the transaction that caused it has committed.

    Returns False when the message could not be queued. The failure is logged,
    not raised: the user's request has already succeeded, and every email sent
    here can be requested again from the UI. Phase 4 replaces this with an
    outbox table so that queueing is part of the transaction.
    """
    try:
        send_email_task.delay(message.to_dict())
        return True
    except Exception:
        logger.exception("Could not queue email", extra={"email_subject": message.subject})
        return False


@celery_app.task(name="notifications.dispatch_outbox", soft_time_limit=240, time_limit=300)
def dispatch_outbox_task() -> int:
    """Deliver pending notification events. Runs every minute, and right after changes that emit events."""
    from app.core.database import system_session
    from app.services.notifications import Dispatcher

    with system_session() as db:
        return Dispatcher(db).run()


@celery_app.task(name="lifecycle.scan_expiries", soft_time_limit=600, time_limit=900)
def scan_expiries_task() -> int:
    """Emit the expiry reminders that are due. Runs every hour."""
    from app.core.database import system_session
    from app.services.reminders import ReminderScanner

    with system_session() as db:
        return ReminderScanner(db).run()
