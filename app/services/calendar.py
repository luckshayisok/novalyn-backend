"""Compliance calendar: expirations, renewal deadlines and approvals on one timeline."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, aliased

from app.core.errors import ValidationFailedError
from app.models import Document, Entity, Renewal, RenewalStatus
from app.services.context import OrgContext
from app.services.expiry import compute_status, organization_today

MAX_RANGE_DAYS = 62
EVENT_TYPES = ("expiry", "renewal_deadline", "approval")


@dataclass(frozen=True)
class CalendarEvent:
    id: str
    type: str  # expiry, expired, renewal_deadline, approval
    date: date
    title: str
    document_id: uuid.UUID
    document_name: str
    category: str
    status: str
    renewal_id: uuid.UUID | None = None
    owner_name: str | None = None
    entity_name: str | None = None


class CalendarService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def events(
        self,
        ctx: OrgContext,
        *,
        start: date,
        end: date,
        types: tuple[str, ...] = EVENT_TYPES,
        category: str | None = None,
        owner_membership_id: uuid.UUID | None = None,
        location_id: uuid.UUID | None = None,
    ) -> tuple[date, list[CalendarEvent]]:
        if end < start:
            raise ValidationFailedError("The end of the range is before its start.", code="invalid_range")
        if (end - start).days > MAX_RANGE_DAYS:
            raise ValidationFailedError(
                f"Ask for at most {MAX_RANGE_DAYS} days at a time.", code="range_too_long"
            )
        today = organization_today(ctx.organization.timezone)
        location = aliased(Entity)

        def document_filters(query):
            query = query.where(
                Document.organization_id == ctx.organization_id, Document.archived_at.is_(None)
            )
            if category:
                query = query.where(Document.category == category)
            if location_id:
                query = query.outerjoin(location, location.id == Document.entity_id).where(
                    or_(Document.entity_id == location_id, location.location_id == location_id)
                )
            return query

        events: list[CalendarEvent] = []
        if "expiry" in types:
            query = document_filters(
                select(Document).where(Document.expiry_date >= start, Document.expiry_date <= end)
            )
            if owner_membership_id:
                query = query.where(Document.owner_membership_id == owner_membership_id)
            for document in self.db.scalars(query):
                status = compute_status(document.expiry_date, today)
                expired = document.expiry_date < today
                events.append(
                    CalendarEvent(
                        id=f"expiry:{document.id}",
                        type="expired" if expired else "expiry",
                        date=document.expiry_date,
                        title=f"{document.name} {'expired' if expired else 'expires'}",
                        document_id=document.id,
                        document_name=document.name,
                        category=document.category,
                        status=status.value,
                        owner_name=document.owner.user.full_name if document.owner else None,
                        entity_name=document.entity.name if document.entity else None,
                    )
                )

        wanted = [kind for kind in ("renewal_deadline", "approval") if kind in types]
        if wanted:
            query = document_filters(
                select(Renewal)
                .join(Document, Document.id == Renewal.document_id)
                .where(
                    Renewal.organization_id == ctx.organization_id,
                    Renewal.status.in_(RenewalStatus.OPEN),
                    Renewal.deadline >= start,
                    Renewal.deadline <= end,
                )
            )
            if owner_membership_id:
                query = query.where(Renewal.assignee_membership_id == owner_membership_id)
            for renewal in self.db.scalars(query):
                kind = "approval" if renewal.status == RenewalStatus.AWAITING_APPROVAL else "renewal_deadline"
                if kind not in wanted:
                    continue
                document = renewal.document
                title = (
                    f"Approve the renewal of {document.name}"
                    if kind == "approval"
                    else f"Renewal of {document.name} due"
                )
                events.append(
                    CalendarEvent(
                        id=f"renewal:{renewal.id}",
                        type=kind,
                        date=renewal.deadline,
                        title=title,
                        document_id=document.id,
                        document_name=document.name,
                        category=document.category,
                        status=renewal.status,
                        renewal_id=renewal.id,
                        owner_name=renewal.assignee.user.full_name if renewal.assignee else None,
                        entity_name=document.entity.name if document.entity else None,
                    )
                )

        order = {"expired": 0, "approval": 1, "renewal_deadline": 2, "expiry": 3}
        events.sort(key=lambda event: (event.date, order[event.type], event.title.lower()))
        return today, events
