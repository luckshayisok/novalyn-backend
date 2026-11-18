"""Expiry reminders.

The scan runs every hour. For each organization it takes today's date in the
organization's own time zone and looks at every active document with an
expiry date:

* Before expiry, the reminder due is the smallest configured offset that is
  at least the number of days left. With offsets 60/30/7/1/0, a document 45
  days out gets its 60-day reminder, at 29 days its 30-day reminder, and so
  on. A scan that was missed is caught up on the next run, and a document
  added close to its expiry gets one reminder, not a burst.
* In the first week after expiry: one "expired" alert.
* After that: one "overdue" alert per week, for up to twelve weeks.

Each reminder has a dedupe key made of the document, its expiry date and the
offset or week, so it is sent once. A renewal changes the expiry date, which
starts a fresh cycle.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.plans import Feature
from app.models import Document, Organization, Renewal, RenewalStatus
from app.models.organization import DEFAULT_REMINDER_OFFSETS
from app.services.expiry import organization_today
from app.services.notifications import EventType, document_payload, emit, kick_dispatcher
from app.services.plans import Entitlements

logger = logging.getLogger(__name__)

MAX_OVERDUE_WEEKS = 12
MAX_OFFSET_DAYS = 365
MAX_OFFSETS = 10


def normalize_offsets(values: list[int]) -> list[int]:
    """Unique offsets, largest first. Validation of the range happens in the API schema."""
    return sorted({int(value) for value in values}, reverse=True)


def due_reminder(days_left: int, offsets: list[int]) -> int | None:
    """The offset whose reminder is due for a document with this many days left, if any."""
    crossed = [offset for offset in offsets if days_left <= offset]
    return min(crossed) if crossed else None


class ReminderScanner:
    def __init__(self, db: Session) -> None:
        self.db = db

    def run(self) -> int:
        """Emit every reminder that is due. Returns how many events were created."""
        created = 0
        organizations = list(
            self.db.scalars(select(Organization).where(Organization.reminders_enabled.is_(True)))
        )
        for organization in organizations:
            try:
                created += self._scan(organization)
                self.db.commit()
            except Exception:
                self.db.rollback()
                logger.exception("Reminder scan failed", extra={"organization_id": str(organization.id)})
        if created:
            kick_dispatcher()
        return created

    def _scan(self, organization: Organization) -> int:
        today = organization_today(organization.timezone)
        entitlements = Entitlements(self.db, organization.id)
        if not entitlements.has(Feature.AUTOMATED_REMINDERS):
            return 0
        # Without a custom schedule in the plan, the default reminder days apply.
        chosen = organization.reminder_offsets if entitlements.has(Feature.CUSTOM_REMINDERS) else None
        offsets = normalize_offsets(chosen if chosen is not None else DEFAULT_REMINDER_OFFSETS)
        horizon = today + timedelta(days=max(offsets, default=0))
        documents = self.db.scalars(
            select(Document).where(
                Document.organization_id == organization.id,
                Document.archived_at.is_(None),
                Document.expiry_date.is_not(None),
                Document.expiry_date <= horizon,
            )
        )
        open_renewals = {
            renewal.document_id: renewal
            for renewal in self.db.scalars(
                select(Renewal).where(
                    Renewal.organization_id == organization.id, Renewal.status.in_(RenewalStatus.OPEN)
                )
            )
        }

        created = 0
        for document in documents:
            days_left = (document.expiry_date - today).days
            renewal = open_renewals.get(document.id)
            extra = {
                "assignee_membership_id": str(renewal.assignee_membership_id)
                if renewal and renewal.assignee_membership_id
                else None
            }
            key_base = f"{document.id}:{document.expiry_date.isoformat()}"
            if days_left >= 0:
                offset = due_reminder(days_left, offsets)
                if offset is None:
                    continue
                event = emit(
                    self.db,
                    organization.id,
                    EventType.DOCUMENT_EXPIRING,
                    document_payload(document, days=days_left, offset=offset, **extra),
                    dedupe_key=f"expiring:{key_base}:{offset}",
                )
            elif -days_left < 7:
                event = emit(
                    self.db,
                    organization.id,
                    EventType.DOCUMENT_EXPIRED,
                    document_payload(document, **extra),
                    dedupe_key=f"expired:{key_base}",
                )
            else:
                week = -days_left // 7
                if week > MAX_OVERDUE_WEEKS:
                    continue
                event = emit(
                    self.db,
                    organization.id,
                    EventType.DOCUMENT_OVERDUE,
                    document_payload(document, days_overdue=-days_left, **extra),
                    dedupe_key=f"overdue:{key_base}:{week}",
                )
            if event is not None:
                created += 1
        return created
