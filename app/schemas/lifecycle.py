"""Schemas for notifications and renewals."""

import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import Field, field_validator

from app.models import Notification, Renewal, RenewalEvent
from app.schemas.common import Schema
from app.schemas.documents import EntityRef, OwnerOut, VersionOut
from app.services.context import OrgContext
from app.services.expiry import DocumentStatus, compute_status
from app.services.renewals import renewal_permissions

RenewalStatusValue = Literal[
    "not_started", "requested", "in_progress", "submitted", "awaiting_approval", "completed", "rejected"
]


class NotificationOut(Schema):
    id: uuid.UUID
    type: str
    title: str
    body: str
    link: str | None
    read_at: datetime | None
    created_at: datetime

    @classmethod
    def build(cls, notification: Notification) -> "NotificationOut":
        return cls.model_validate(notification)


class UnreadCount(Schema):
    unread: int


# -------------------------------------------------------------------- renewals


class RenewalDocumentOut(Schema):
    id: uuid.UUID
    name: str
    category: str
    expiry_date: date | None
    status: DocumentStatus
    entity: EntityRef | None


class RenewalEventOut(Schema):
    id: uuid.UUID
    action: str
    from_status: str | None
    to_status: str
    note: str | None
    actor_name: str | None
    created_at: datetime

    @classmethod
    def build(cls, event: RenewalEvent) -> "RenewalEventOut":
        return cls(
            id=event.id,
            action=event.action,
            from_status=event.from_status,
            to_status=event.to_status,
            note=event.note,
            actor_name=event.actor.full_name if event.actor else None,
            created_at=event.created_at,
        )


class RenewalSummary(Schema):
    id: uuid.UUID
    status: RenewalStatusValue
    deadline: date | None
    days_to_deadline: int | None
    overdue: bool
    assignee: OwnerOut | None
    document: RenewalDocumentOut
    created_at: datetime
    updated_at: datetime

    @classmethod
    def values_for(cls, renewal: Renewal, today: date) -> dict[str, Any]:
        document = renewal.document
        days = (renewal.deadline - today).days if renewal.deadline else None
        return {
            "id": renewal.id,
            "status": renewal.status,
            "deadline": renewal.deadline,
            "days_to_deadline": days,
            "overdue": bool(renewal.is_open and days is not None and days < 0),
            "assignee": OwnerOut.build(renewal.assignee),
            "document": RenewalDocumentOut(
                id=document.id,
                name=document.name,
                category=document.category,
                expiry_date=document.expiry_date,
                status=compute_status(document.expiry_date, today, archived=document.archived_at is not None),
                entity=EntityRef.build(document.entity),
            ),
            "created_at": renewal.created_at,
            "updated_at": renewal.updated_at,
        }

    @classmethod
    def build(cls, renewal: Renewal, today: date) -> "RenewalSummary":
        return cls(**cls.values_for(renewal, today))


class RenewalDetail(RenewalSummary):
    notes: str | None
    previous_expiry_date: date | None
    proposed_issue_date: date | None
    proposed_expiry_date: date | None
    # Read automatically from the renewed file, offered as a hint for the new expiry date.
    suggested_expiry_date: date | None
    new_version: VersionOut | None
    rejection_reason: str | None
    created_by_name: str | None
    completed_at: datetime | None
    completed_by_name: str | None
    events: list[RenewalEventOut]
    can: dict[str, bool]

    @classmethod
    def build_for(cls, renewal: Renewal, today: date, ctx: OrgContext) -> "RenewalDetail":
        suggested = None
        extraction = renewal.new_version.extraction if renewal.new_version else None
        if extraction and extraction.status in ("extraction_completed", "review_required"):
            value = (extraction.fields or {}).get("expiry_date", {}).get("value")
            try:
                suggested = date.fromisoformat(value) if value else None
            except ValueError:
                suggested = None
        return cls(
            **RenewalSummary.values_for(renewal, today),
            notes=renewal.notes,
            previous_expiry_date=renewal.previous_expiry_date,
            proposed_issue_date=renewal.proposed_issue_date,
            proposed_expiry_date=renewal.proposed_expiry_date,
            suggested_expiry_date=suggested,
            new_version=VersionOut.build(renewal.new_version) if renewal.new_version else None,
            rejection_reason=renewal.rejection_reason,
            created_by_name=renewal.created_by.full_name if renewal.created_by else None,
            completed_at=renewal.completed_at,
            completed_by_name=renewal.completed_by.full_name if renewal.completed_by else None,
            events=[RenewalEventOut.build(event) for event in renewal.events],
            can=renewal_permissions(ctx, renewal),
        )


class RenewalCreate(Schema):
    document_id: uuid.UUID
    assignee_membership_id: uuid.UUID | None = None
    deadline: date | None = None
    notes: str | None = Field(default=None, max_length=5000)
    request: bool = False


class RenewalUpdate(Schema):
    assignee_membership_id: uuid.UUID | None = None
    deadline: date | None = None
    notes: str | None = Field(default=None, max_length=5000)

    def changes(self) -> dict[str, Any]:
        return self.model_dump(exclude_unset=True)


class RenewalNote(Schema):
    note: str | None = Field(default=None, max_length=1000)


class RenewalReject(Schema):
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Give a reason")
        return value.strip()


class RenewalUploadForm(Schema):
    issue_date: date | None = None
    expiry_date: date | None = None
    note: str | None = Field(default=None, max_length=500)

    @field_validator("*", mode="before")
    @classmethod
    def _blank(cls, value: Any) -> Any:
        return None if isinstance(value, str) and not value.strip() else value
