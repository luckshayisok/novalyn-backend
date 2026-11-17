"""Notification engine: the outbox, recipients, wording, in-app notifications and email.

1. A change writes an `OutboxEvent` in its own transaction (`emit`). If the
   transaction rolls back, there is no event; if it commits, the event exists.
2. After the commit, the dispatcher is woken (`kick_dispatcher`); it also runs
   every minute from Celery Beat, so nothing waits forever if a wake-up is lost.
3. The dispatcher claims each due event, works out who should hear about it,
   creates one `Notification` per person (unique per event and person, so a
   retry never duplicates them) and sends the emails. Failed emails are
   retried with backoff; after five failures an email is marked failed.

Delivery is at least once: if the process stops after an email is sent but
before it is recorded, that one email can be sent twice.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import NotFoundError
from app.core.permissions import Role
from app.core.time import utcnow
from app.integrations.email import templates
from app.integrations.email.senders import get_email_sender
from app.models import (
    Document,
    EmailStatus,
    Membership,
    Notification,
    Organization,
    OutboxEvent,
    OutboxStatus,
    Renewal,
    User,
)
from app.services.context import OrgContext

logger = logging.getLogger(__name__)

MAX_EMAIL_ATTEMPTS = 5
MAX_EVENT_ATTEMPTS = 8
STALE_CLAIM = timedelta(minutes=10)
BATCH_SIZE = 50
MANAGER_ROLES = (Role.OWNER.value, Role.ADMIN.value, Role.COMPLIANCE_MANAGER.value)


class EventType:
    DOCUMENT_EXPIRING = "document.expiring"
    DOCUMENT_EXPIRED = "document.expired"
    DOCUMENT_OVERDUE = "document.overdue"
    DOCUMENT_REJECTED = "document.rejected"
    VERIFICATION_REQUIRED = "document.verification_required"
    RENEWAL_REQUESTED = "renewal.requested"
    RENEWAL_AWAITING_APPROVAL = "renewal.awaiting_approval"
    RENEWAL_COMPLETED = "renewal.completed"
    RENEWAL_REJECTED = "renewal.rejected"


# --------------------------------------------------------------------- emitting


def emit(
    db: Session,
    organization_id: uuid.UUID,
    event_type: str,
    payload: dict[str, Any],
    *,
    dedupe_key: str | None = None,
) -> OutboxEvent | None:
    """Add an event to the caller's transaction. Returns None when the dedupe key was already used."""
    if dedupe_key is not None:
        existing = db.scalar(select(OutboxEvent.id).where(OutboxEvent.dedupe_key == dedupe_key))
        if existing is not None:
            return None
    event = OutboxEvent(
        id=uuid.uuid4(),
        organization_id=organization_id,
        event_type=event_type,
        payload=payload,
        dedupe_key=dedupe_key,
        status=OutboxStatus.PENDING,
        next_attempt_at=utcnow(),
    )
    db.add(event)
    return event


def kick_dispatcher() -> None:
    """Ask the worker to deliver pending events now. Called after a commit that emitted events."""
    from app.workers.notification_tasks import dispatch_outbox_task

    try:
        dispatch_outbox_task.delay()
    except Exception:
        # The scheduled dispatcher picks the events up within a minute.
        logger.exception("Could not wake the notification dispatcher")


# ------------------------------------------------------------------- recipients


def _members(db: Session, organization_id: uuid.UUID) -> list[Membership]:
    return list(
        db.scalars(
            select(Membership)
            .join(User, User.id == Membership.user_id)
            .where(Membership.organization_id == organization_id, User.is_active.is_(True))
        )
    )


def _recipients(db: Session, event: OutboxEvent) -> list[User]:
    payload = event.payload
    members = _members(db, event.organization_id)
    by_membership = {member.id: member for member in members}
    by_user = {member.user_id: member for member in members}
    managers = [member for member in members if member.role in MANAGER_ROLES]
    chosen: list[Membership] = []

    def membership(value: Any) -> Membership | None:
        return by_membership.get(uuid.UUID(value)) if value else None

    def user_member(value: Any) -> Membership | None:
        return by_user.get(uuid.UUID(value)) if value else None

    kind = event.event_type
    if kind in (EventType.DOCUMENT_EXPIRING, EventType.DOCUMENT_EXPIRED, EventType.DOCUMENT_OVERDUE):
        chosen = [
            *managers,
            membership(payload.get("owner_membership_id")),
            membership(payload.get("assignee_membership_id")),
        ]
    elif kind == EventType.VERIFICATION_REQUIRED:
        chosen = list(managers)
    elif kind == EventType.DOCUMENT_REJECTED:
        chosen = [user_member(payload.get("uploaded_by_id"))]
    elif kind == EventType.RENEWAL_REQUESTED:
        chosen = [membership(payload.get("assignee_membership_id"))]
    elif kind == EventType.RENEWAL_AWAITING_APPROVAL:
        chosen = list(managers)
    elif kind in (EventType.RENEWAL_COMPLETED, EventType.RENEWAL_REJECTED):
        chosen = [
            membership(payload.get("assignee_membership_id")),
            user_member(payload.get("created_by_id")),
        ]

    actor = payload.get("actor_user_id")
    seen: set[uuid.UUID] = set()
    users: list[User] = []
    for item in chosen:
        if item is None or item.user_id in seen or (actor and str(item.user_id) == actor):
            continue
        seen.add(item.user_id)
        users.append(item.user)
    return users


# ---------------------------------------------------------------------- wording


@dataclass(frozen=True)
class Content:
    title: str
    body: str
    link: str | None
    email: bool
    action_label: str


def _day(value: str | None) -> str:
    if not value:
        return "an unknown date"
    return date.fromisoformat(value).strftime("%d %B %Y").lstrip("0")


def _days(count: int) -> str:
    return f"{count} day{'s' if count != 1 else ''}"


def render(event: OutboxEvent) -> Content:
    data = event.payload
    name = data.get("document_name", "A document")
    document_link = f"documents/{data['document_id']}" if data.get("document_id") else None
    renewal_link = f"renewals/{data['renewal_id']}" if data.get("renewal_id") else document_link

    match event.event_type:
        case EventType.DOCUMENT_EXPIRING:
            days = int(data.get("days", 0))
            when = "today" if days == 0 else f"in {_days(days)}"
            title = f"{name} expires {when}"
            body = (
                f"{name} expires on {_day(data.get('expiry_date'))}. Renew it before then so it stays valid."
            )
            return Content(title, body, document_link, True, "Open the document")
        case EventType.DOCUMENT_EXPIRED:
            title = f"{name} has expired"
            body = f"{name} expired on {_day(data.get('expiry_date'))}. Start a renewal as soon as you can."
            return Content(title, body, document_link, True, "Open the document")
        case EventType.DOCUMENT_OVERDUE:
            days = int(data.get("days_overdue", 0))
            title = f"{name} expired {_days(days)} ago"
            body = f"{name} expired on {_day(data.get('expiry_date'))} and has not been renewed yet."
            return Content(title, body, document_link, True, "Open the document")
        case EventType.VERIFICATION_REQUIRED:
            title = f"{name} is waiting for review"
            body = f"{data.get('actor_name', 'Someone')} uploaded {name}. Check its details and verify it."
            return Content(title, body, document_link, False, "Review the document")
        case EventType.DOCUMENT_REJECTED:
            title = f"{name} was rejected"
            body = f"{data.get('actor_name', 'A reviewer')} rejected {name}: {data.get('reason', '')}"
            return Content(title, body, document_link, True, "Open the document")
        case EventType.RENEWAL_REQUESTED:
            deadline = f" by {_day(data['deadline'])}" if data.get("deadline") else ""
            title = f"Please renew {name}"
            body = f"{data.get('actor_name', 'Someone')} asked you to renew {name}{deadline}."
            return Content(title, body, renewal_link, True, "Open the renewal")
        case EventType.RENEWAL_AWAITING_APPROVAL:
            title = f"Renewal of {name} needs approval"
            body = (
                f"{data.get('actor_name', 'Someone')} uploaded the renewed {name} and sent it for approval."
            )
            return Content(title, body, renewal_link, True, "Review the renewal")
        case EventType.RENEWAL_COMPLETED:
            expiry = f" It is now valid until {_day(data['expiry_date'])}." if data.get("expiry_date") else ""
            title = f"Renewal of {name} approved"
            body = f"{data.get('actor_name', 'Someone')} approved the renewal of {name}.{expiry}"
            return Content(title, body, renewal_link, True, "Open the renewal")
        case EventType.RENEWAL_REJECTED:
            title = f"Renewal of {name} sent back"
            body = (
                f"{data.get('actor_name', 'Someone')} rejected the renewed {name}: {data.get('reason', '')}"
            )
            return Content(title, body, renewal_link, True, "Open the renewal")
    return Content(event.event_type, "", document_link, False, "Open NOVALYN")


# ------------------------------------------------------------------- dispatching


class Dispatcher:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.settings = get_settings()

    def run(self, limit: int = BATCH_SIZE) -> int:
        """Deliver due events. Returns how many were processed."""
        now = utcnow()
        candidates = list(
            self.db.scalars(
                select(OutboxEvent.id)
                .where(
                    or_(
                        (OutboxEvent.status == OutboxStatus.PENDING)
                        & (or_(OutboxEvent.next_attempt_at.is_(None), OutboxEvent.next_attempt_at <= now)),
                        (OutboxEvent.status == OutboxStatus.PROCESSING)
                        & (OutboxEvent.claimed_at < now - STALE_CLAIM),
                    )
                )
                .order_by(OutboxEvent.created_at)
                .limit(limit)
            )
        )
        processed = 0
        for event_id in candidates:
            if self._claim(event_id):
                self._process(event_id)
                processed += 1
        return processed

    def _claim(self, event_id: uuid.UUID) -> bool:
        now = utcnow()
        claimed = self.db.execute(
            update(OutboxEvent)
            .where(
                OutboxEvent.id == event_id,
                or_(
                    OutboxEvent.status == OutboxStatus.PENDING,
                    (OutboxEvent.status == OutboxStatus.PROCESSING)
                    & (OutboxEvent.claimed_at < now - STALE_CLAIM),
                ),
            )
            .values(status=OutboxStatus.PROCESSING, claimed_at=now, attempts=OutboxEvent.attempts + 1)
        ).rowcount
        self.db.commit()
        return bool(claimed)

    def _process(self, event_id: uuid.UUID) -> None:
        event = self.db.get(OutboxEvent, event_id)
        try:
            content = render(event)
            for user in _recipients(self.db, event):
                exists = self.db.scalar(
                    select(Notification.id).where(
                        Notification.event_id == event.id, Notification.user_id == user.id
                    )
                )
                if exists is None:
                    self.db.add(
                        Notification(
                            organization_id=event.organization_id,
                            event_id=event.id,
                            user_id=user.id,
                            type=event.event_type,
                            title=content.title[:200],
                            body=content.body[:1000],
                            link=content.link,
                            email_status=EmailStatus.PENDING if content.email else EmailStatus.SKIPPED,
                        )
                    )
            self.db.commit()
            pending = self._send_emails(event, content)
        except Exception as error:
            self.db.rollback()
            logger.exception("Could not deliver notification event", extra={"event_id": str(event_id)})
            event = self.db.get(OutboxEvent, event_id)
            self._retry_later(event, str(error)[:500])
            return

        if pending:
            self._retry_later(event, "Some emails could not be sent yet.")
        else:
            event.status = OutboxStatus.DONE
            event.processed_at = utcnow()
            event.last_error = None
            self.db.commit()

    def _send_emails(self, event: OutboxEvent, content: Content) -> int:
        """Send what is still pending. Returns how many emails remain to retry."""
        organization = self.db.get(Organization, event.organization_id)
        notifications = list(
            self.db.scalars(
                select(Notification).where(
                    Notification.event_id == event.id, Notification.email_status == EmailStatus.PENDING
                )
            )
        )
        remaining = 0
        sender = get_email_sender()
        for notification in notifications:
            user = notification.user
            if not user.is_active or not user.is_email_verified:
                notification.email_status = EmailStatus.SKIPPED
                self.db.commit()
                continue
            url = f"{self.settings.frontend_url}/o/{organization.slug}"
            if content.link:
                url = f"{url}/{content.link}"
            message = templates.notification_email(
                to=user.email,
                full_name=user.full_name,
                subject=f"{content.title} · {organization.name}",
                heading=content.title,
                lines=[content.body],
                action_label=content.action_label,
                url=url,
                organization_name=organization.name,
            )
            try:
                sender.send(message)
            except Exception:
                logger.exception("Email delivery failed", extra={"notification_id": str(notification.id)})
                notification.email_attempts += 1
                if notification.email_attempts >= MAX_EMAIL_ATTEMPTS:
                    notification.email_status = EmailStatus.FAILED
                else:
                    remaining += 1
            else:
                notification.email_status = EmailStatus.SENT
                notification.email_sent_at = utcnow()
            self.db.commit()
        return remaining

    def _retry_later(self, event: OutboxEvent, error: str) -> None:
        event.last_error = error
        if event.attempts >= MAX_EVENT_ATTEMPTS:
            event.status = OutboxStatus.FAILED
        else:
            event.status = OutboxStatus.PENDING
            # 1, 2, 4, 8 ... minutes, at most an hour.
            event.next_attempt_at = utcnow() + timedelta(minutes=min(2 ** (event.attempts - 1), 60))
        self.db.commit()


# ------------------------------------------------------------------ user inbox


class NotificationService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def _mine(self, ctx: OrgContext):
        return select(Notification).where(
            Notification.organization_id == ctx.organization_id, Notification.user_id == ctx.user.id
        )

    def list(
        self, ctx: OrgContext, *, unread_only: bool, page: int, page_size: int
    ) -> tuple[list[Notification], int]:
        query = self._mine(ctx)
        if unread_only:
            query = query.where(Notification.read_at.is_(None))
        total = self.db.scalar(select(func.count()).select_from(query.subquery())) or 0
        items = list(
            self.db.scalars(
                query.order_by(Notification.created_at.desc(), Notification.id)
                .limit(page_size)
                .offset((page - 1) * page_size)
            )
        )
        return items, total

    def unread_count(self, ctx: OrgContext) -> int:
        query = self._mine(ctx).where(Notification.read_at.is_(None))
        return self.db.scalar(select(func.count()).select_from(query.subquery())) or 0

    def mark_read(self, ctx: OrgContext, notification_id: uuid.UUID) -> Notification:
        notification = self.db.scalars(self._mine(ctx).where(Notification.id == notification_id)).first()
        if notification is None:
            raise NotFoundError("This notification was not found.")
        if notification.read_at is None:
            notification.read_at = utcnow()
            self.db.commit()
        return notification

    def mark_all_read(self, ctx: OrgContext) -> int:
        count = self.db.execute(
            update(Notification)
            .where(
                Notification.organization_id == ctx.organization_id,
                Notification.user_id == ctx.user.id,
                Notification.read_at.is_(None),
            )
            .values(read_at=utcnow())
        ).rowcount
        self.db.commit()
        return count


# ------------------------------------------------------------- event payloads


def document_payload(document: Document, *, actor: User | None = None, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "document_id": str(document.id),
        "document_name": document.name,
        "expiry_date": document.expiry_date.isoformat() if document.expiry_date else None,
        "owner_membership_id": str(document.owner_membership_id) if document.owner_membership_id else None,
    }
    if actor is not None:
        payload["actor_user_id"] = str(actor.id)
        payload["actor_name"] = actor.full_name
    payload.update(extra)
    return payload


def renewal_payload(renewal: Renewal, *, actor: User, **extra: Any) -> dict[str, Any]:
    return document_payload(
        renewal.document,
        actor=actor,
        renewal_id=str(renewal.id),
        # Read through the relationship: a reassignment in this transaction is not flushed yet.
        assignee_membership_id=str(renewal.assignee.id) if renewal.assignee else None,
        created_by_id=str(renewal.created_by_id) if renewal.created_by_id else None,
        deadline=renewal.deadline.isoformat() if renewal.deadline else None,
        **extra,
    )
