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
