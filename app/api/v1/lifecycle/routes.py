"""Notification inbox and renewal endpoints."""

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile, status
from sqlalchemy.orm import Session

from app.api.deps import get_org_context, require_permission
from app.core.config import get_settings
from app.core.database import get_db
from app.core.files import validate_upload
from app.core.permissions import Permission
from app.models import RenewalStatus
from app.schemas.common import Page
from app.schemas.lifecycle import (
    NotificationOut,
    RenewalCreate,
    RenewalDetail,
    RenewalNote,
    RenewalReject,
    RenewalSummary,
    RenewalUpdate,
    RenewalUploadForm,
    UnreadCount,
)
from app.services.context import OrgContext
from app.services.notifications import NotificationService
from app.services.renewals import RenewalService

ORG = "/organizations/{organization_id}"
notifications_router = APIRouter(prefix=f"{ORG}/notifications", tags=["notifications"])
renewals_router = APIRouter(prefix=f"{ORG}/renewals", tags=["renewals"])

CanRead = Depends(require_permission(Permission.RENEWALS_READ))

# ------------------------------------------------------------------ notifications


@notifications_router.get("", response_model=Page[NotificationOut])
def list_notifications(
    unread: bool = False,
    page: int = Query(default=1, ge=1, le=10_000),
    page_size: int = Query(default=20, ge=1, le=100),
    ctx: OrgContext = Depends(get_org_context),
    db: Session = Depends(get_db),
) -> Page[NotificationOut]:
    """The signed-in user's notifications in this organization, newest first."""
    items, total = NotificationService(db).list(ctx, unread_only=unread, page=page, page_size=page_size)
    return Page[NotificationOut](
        items=[NotificationOut.build(item) for item in items], total=total, page=page, page_size=page_size
    )


@notifications_router.get("/unread-count", response_model=UnreadCount)
def unread_count(ctx: OrgContext = Depends(get_org_context), db: Session = Depends(get_db)) -> UnreadCount:
    return UnreadCount(unread=NotificationService(db).unread_count(ctx))


@notifications_router.post("/{notification_id}/read", response_model=NotificationOut)
def mark_read(
    notification_id: uuid.UUID, ctx: OrgContext = Depends(get_org_context), db: Session = Depends(get_db)
) -> NotificationOut:
    return NotificationOut.build(NotificationService(db).mark_read(ctx, notification_id))


@notifications_router.post("/read-all", status_code=status.HTTP_204_NO_CONTENT)
def mark_all_read(ctx: OrgContext = Depends(get_org_context), db: Session = Depends(get_db)) -> Response:
    NotificationService(db).mark_all_read(ctx)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ----------------------------------------------------------------------- renewals

StatusFilter = Literal["open", "overdue", "closed", *RenewalStatus.ALL]


@renewals_router.get("", response_model=Page[RenewalSummary])
def list_renewals(
    status_filter: StatusFilter | None = Query(default="open", alias="status"),
    mine: bool = False,
    page: int = Query(default=1, ge=1, le=10_000),
    page_size: int = Query(default=25, ge=1, le=100),
    ctx: OrgContext = CanRead,
    db: Session = Depends(get_db),
) -> Page[RenewalSummary]:
    service = RenewalService(db)
    statuses: tuple[str, ...] | None
    if status_filter in ("open", "overdue"):
        statuses = RenewalStatus.OPEN
    elif status_filter == "closed":
        statuses = RenewalStatus.CLOSED
    elif status_filter:
        statuses = (status_filter,)
    else:
        statuses = None
    today = service.today(ctx)
    items, total = service.repo(ctx).list(
        statuses=statuses,
        assignee_id=ctx.membership.id if mine else None,
        page=page,
        page_size=page_size,
        deadline_before=today if status_filter == "overdue" else None,
    )
    return Page[RenewalSummary](
        items=[RenewalSummary.build(item, today) for item in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@renewals_router.post("", response_model=RenewalDetail, status_code=status.HTTP_201_CREATED)
def create_renewal(
    payload: RenewalCreate,
    ctx: OrgContext = Depends(require_permission(Permission.RENEWALS_MANAGE)),
    db: Session = Depends(get_db),
) -> RenewalDetail:
    service = RenewalService(db)
    renewal = service.create(
        ctx,
        document_id=payload.document_id,
        assignee_membership_id=payload.assignee_membership_id,
        deadline=payload.deadline,
        notes=payload.notes,
        request=payload.request,
    )
    return RenewalDetail.build_for(renewal, service.today(ctx), ctx)


@renewals_router.get("/{renewal_id}", response_model=RenewalDetail)
def get_renewal(
    renewal_id: uuid.UUID, ctx: OrgContext = CanRead, db: Session = Depends(get_db)
) -> RenewalDetail:
    service = RenewalService(db)
    return RenewalDetail.build_for(service.get(ctx, renewal_id), service.today(ctx), ctx)


@renewals_router.patch("/{renewal_id}", response_model=RenewalDetail)
def update_renewal(
    renewal_id: uuid.UUID, payload: RenewalUpdate, ctx: OrgContext = CanRead, db: Session = Depends(get_db)
) -> RenewalDetail:
    service = RenewalService(db)
    return RenewalDetail.build_for(
        service.update(ctx, renewal_id, payload.changes()), service.today(ctx), ctx
    )


def _step(name: str):
    def endpoint(
        renewal_id: uuid.UUID,
        payload: RenewalNote | None = None,
        ctx: OrgContext = CanRead,
        db: Session = Depends(get_db),
    ) -> RenewalDetail:
        service = RenewalService(db)
        note = payload.note if payload else None
        match name:
            case "request":
                renewal = service.request(ctx, renewal_id, note)
            case "start":
                renewal = service.start(ctx, renewal_id)
            case "submit":
                renewal = service.submit(ctx, renewal_id, note)
            case "approve":
                renewal = service.approve(ctx, renewal_id, note)
            case _:
                renewal = service.reopen(ctx, renewal_id)
        return RenewalDetail.build_for(renewal, service.today(ctx), ctx)

    endpoint.__name__ = f"{name}_renewal"
    return endpoint


for _name, _summary in [
    ("request", "Ask the assignee to renew the document"),
    ("start", "The assignee starts working on the renewal"),
    ("submit", "Send the renewed document for approval"),
    ("approve", "Approve: the renewed file becomes current and the new dates apply"),
    ("reopen", "Reopen a rejected renewal"),
]:
    renewals_router.add_api_route(
        f"/{{renewal_id}}/{_name}",
        _step(_name),
        methods=["POST"],
        response_model=RenewalDetail,
        summary=_summary,
    )


@renewals_router.post("/{renewal_id}/reject", response_model=RenewalDetail)
def reject_renewal(
    renewal_id: uuid.UUID, payload: RenewalReject, ctx: OrgContext = CanRead, db: Session = Depends(get_db)
) -> RenewalDetail:
    service = RenewalService(db)
    return RenewalDetail.build_for(service.reject(ctx, renewal_id, payload.reason), service.today(ctx), ctx)


@renewals_router.post("/{renewal_id}/file", response_model=RenewalDetail)
def upload_renewed_file(
    renewal_id: uuid.UUID,
    form: Annotated[RenewalUploadForm, Form()],
    file: UploadFile = File(...),
    ctx: OrgContext = CanRead,
    db: Session = Depends(get_db),
) -> RenewalDetail:
    """Upload the renewed document. It stays pending until the renewal is approved."""
    service = RenewalService(db)
    upload = validate_upload(file.file, file.filename, max_bytes=get_settings().max_upload_size_bytes)
    try:
        renewal = service.upload(
            ctx, renewal_id, upload, issue_date=form.issue_date, expiry_date=form.expiry_date, note=form.note
        )
    finally:
        upload.close()
    return RenewalDetail.build_for(renewal, service.today(ctx), ctx)
