"""Lifecycle automation: the notification outbox, in-app notifications and renewals."""

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, foreign, mapped_column, relationship

from app.core.database import Base
from app.core.tenancy import TenantScoped
from app.models.document import Document, DocumentVersion
from app.models.mixins import CreatedAt, Timestamps, UUIDPrimaryKey
from app.models.organization import Membership
from app.models.types import JSONType, UTCDateTime
from app.models.user import User


class OutboxStatus:
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"


class OutboxEvent(UUIDPrimaryKey, CreatedAt, TenantScoped, Base):
    """Something that people should be told about.

    Written in the same transaction as the change that caused it, so an event
    can never be lost between the change and the notification. A dispatcher
    turns events into in-app notifications and emails.
    """

    __tablename__ = "outbox_events"
    __table_args__ = (
        Index("ix_outbox_events_status_next", "status", "next_attempt_at"),
        CheckConstraint("status IN ('pending', 'processing', 'done', 'failed')", name="status_valid"),
    )

    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    # Same key, same event: reminders use it so that each one is sent once.
    dedupe_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    status: Mapped[str] = mapped_column(String(16), default=OutboxStatus.PENDING, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    claimed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    processed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_error: Mapped[str | None] = mapped_column(String(500))


class EmailStatus:
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"


class Notification(UUIDPrimaryKey, CreatedAt, TenantScoped, Base):
    """One person's copy of an event: shown in the app, and sent by email when the event calls for it."""

    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint("event_id", "user_id"),
        Index("ix_notifications_user_org_created", "user_id", "organization_id", "created_at"),
    )

    event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("outbox_events.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(String(1000), nullable=False)
    # A path inside the organization, for example "documents/<id>".
    link: Mapped[str | None] = mapped_column(String(300))
    read_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    email_status: Mapped[str] = mapped_column(String(16), default=EmailStatus.PENDING, nullable=False)
    email_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    email_sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    user: Mapped[User] = relationship(lazy="joined")


class RenewalStatus:
    NOT_STARTED = "not_started"
    REQUESTED = "requested"
    IN_PROGRESS = "in_progress"
    SUBMITTED = "submitted"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    REJECTED = "rejected"

    ALL = (NOT_STARTED, REQUESTED, IN_PROGRESS, SUBMITTED, AWAITING_APPROVAL, COMPLETED, REJECTED)
    CLOSED = (COMPLETED, REJECTED)
    OPEN = (NOT_STARTED, REQUESTED, IN_PROGRESS, SUBMITTED, AWAITING_APPROVAL)


_RENEWAL_STATUSES = ", ".join(f"'{value}'" for value in RenewalStatus.ALL)
_OPEN_RENEWAL = "status NOT IN ('completed', 'rejected')"


class Renewal(UUIDPrimaryKey, Timestamps, TenantScoped, Base):
    """Getting a document renewed: who does it, by when, the new file, and the approval."""

    __tablename__ = "renewals"
    __table_args__ = (
        CheckConstraint(f"status IN ({_RENEWAL_STATUSES})", name="status_valid"),
        # One open renewal per document.
        Index(
            "uq_renewals_open_document",
            "document_id",
            unique=True,
            postgresql_where=text(_OPEN_RENEWAL),
            sqlite_where=text(_OPEN_RENEWAL),
        ),
        Index("ix_renewals_org_status", "organization_id", "status"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), default=RenewalStatus.NOT_STARTED, nullable=False)
    assignee_membership_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("memberships.id", ondelete="SET NULL")
    )
    deadline: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)
    previous_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("document_versions.id", ondelete="SET NULL")
    )
    new_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("document_versions.id", ondelete="SET NULL")
    )
    # The dates of the renewed document, applied when the renewal is approved.
    proposed_issue_date: Mapped[date | None] = mapped_column(Date)
    proposed_expiry_date: Mapped[date | None] = mapped_column(Date)
    previous_expiry_date: Mapped[date | None] = mapped_column(Date)
    rejection_reason: Mapped[str | None] = mapped_column(String(500))
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"))
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    completed_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )

    document: Mapped[Document] = relationship(lazy="joined")
    assignee: Mapped[Membership | None] = relationship(lazy="selectin")
    created_by: Mapped[User | None] = relationship(foreign_keys=[created_by_id], lazy="selectin")
    completed_by: Mapped[User | None] = relationship(foreign_keys=[completed_by_id], lazy="selectin")
    new_version: Mapped[DocumentVersion | None] = relationship(foreign_keys=[new_version_id], lazy="selectin")
    events: Mapped[list["RenewalEvent"]] = relationship(
        back_populates="renewal", order_by="RenewalEvent.created_at", lazy="selectin"
    )

    @property
    def is_open(self) -> bool:
        return self.status in RenewalStatus.OPEN


class RenewalEvent(UUIDPrimaryKey, CreatedAt, TenantScoped, Base):
    """One step in a renewal's history. Append-only."""

    __tablename__ = "renewal_events"

    renewal_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("renewals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)
    note: Mapped[str | None] = mapped_column(String(1000))

    renewal: Mapped[Renewal] = relationship(back_populates="events")
    actor: Mapped[User | None] = relationship(lazy="selectin")


# The open renewal of a document, if there is one, loaded with the document.
Document.open_renewal = relationship(
    Renewal,
    primaryjoin=lambda: (
        (Document.id == foreign(Renewal.document_id)) & Renewal.status.in_(RenewalStatus.OPEN)
    ),
    viewonly=True,
    uselist=False,
    lazy="selectin",
)
