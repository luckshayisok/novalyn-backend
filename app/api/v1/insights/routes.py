"""Compliance calendar and report endpoints."""

import uuid
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.api.deps import require_permission
from app.core.database import get_db
from app.core.permissions import Permission
from app.core.plans import Feature
from app.schemas.insights import CalendarEventOut, CalendarOut, ReportCatalog, ReportInfo, ReportOut
from app.services.calendar import EVENT_TYPES, CalendarService
from app.services.context import OrgContext
from app.services.expiry import organization_today
from app.services.plans import Entitlements
from app.services.reports import ReportService, to_csv, to_pdf

ORG = "/organizations/{organization_id}"
calendar_router = APIRouter(prefix=f"{ORG}/calendar", tags=["calendar"])
reports_router = APIRouter(prefix=f"{ORG}/reports", tags=["reports"])

EventType = Literal["expiry", "renewal_deadline", "approval"]


@calendar_router.get("", response_model=CalendarOut)
def calendar(
    start: date,
    end: date,
    types: list[EventType] = Query(default=list(EVENT_TYPES)),
    category: str | None = Query(default=None, max_length=50),
    owner: uuid.UUID | None = Query(
        default=None, description="Owner of the document or assignee of the renewal"
    ),
    location: uuid.UUID | None = None,
    ctx: OrgContext = Depends(require_permission(Permission.DOCUMENTS_READ)),
    db: Session = Depends(get_db),
) -> CalendarOut:
    """Expirations, renewal deadlines and approvals between two dates (both included, at most 62 days)."""
    today, events = CalendarService(db).events(
        ctx,
        start=start,
        end=end,
        types=tuple(types),
        category=category,
        owner_membership_id=owner,
        location_id=location,
    )
    return CalendarOut(
        start=start, end=end, today=today, events=[CalendarEventOut.build(event) for event in events]
    )


@reports_router.get("", response_model=ReportCatalog)
def report_catalog(
    ctx: OrgContext = Depends(require_permission(Permission.REPORTS_READ)), db: Session = Depends(get_db)
) -> ReportCatalog:
    """Every report, and whether the organization's plan includes it."""
    service = ReportService(db)
    catalog = service.catalog(ctx)
    entitlements = Entitlements(db, ctx.organization_id)
    today = organization_today(ctx.organization.timezone)
    db.commit()
    return ReportCatalog(
        reports=[ReportInfo.build(definition, allowed, today) for definition, allowed in catalog],
        can_export=ctx.has(Permission.REPORTS_EXPORT),
        csv_export=entitlements.has(Feature.CSV_EXPORT),
        pdf_export=entitlements.has(Feature.PDF_EXPORT),
    )


@reports_router.get("/{report_key}", response_model=ReportOut)
def run_report(
    report_key: str,
    start: date | None = None,
    end: date | None = None,
    format: Literal["json", "csv", "pdf"] = "json",
    ctx: OrgContext = Depends(require_permission(Permission.REPORTS_READ)),
    db: Session = Depends(get_db),
):
    """Run a report. `format=csv` or `format=pdf` downloads it, which needs the export permission."""
    if format != "json":
        ctx.require(Permission.REPORTS_EXPORT)
    result = ReportService(db).run(
        ctx, report_key, start=start, end=end, export=None if format == "json" else format
    )
    if format == "json":
        return ReportOut.build(result)

    stamp = result.generated_at.strftime("%Y%m%d")
    filename = f"novalyn-{report_key.replace('_', '-')}-{stamp}.{format}"
    if format == "csv":
        content, media_type = to_csv(result), "text/csv; charset=utf-8"
    else:
        content, media_type = to_pdf(result, ctx.organization.name), "application/pdf"
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
