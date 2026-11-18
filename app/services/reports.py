"""Reports: the same data shown on screen, exported as CSV, or laid out as a PDF.

Each report is a function that returns columns, rows and summary figures for
one organization and period. Everything is computed from live records at the
moment the report is run.
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.constants import DOCUMENT_CATEGORIES
from app.core.errors import NotFoundError, ValidationFailedError
from app.core.plans import Feature
from app.core.time import utcnow
from app.models import AuditLog, Document, Entity, Renewal, RenewalStatus
from app.services.audit import AuditService
from app.services.context import OrgContext
from app.services.expiry import DocumentStatus, compute_status, organization_today, organization_zone
from app.services.plans import Entitlements

MAX_EXPORT_ROWS = 5000
PREVIEW_ROWS = 500
MAX_PERIOD_DAYS = 3 * 366


@dataclass(frozen=True)
class Column:
    key: str
    label: str
    width: float = 1.0  # relative width in the PDF


@dataclass
class ReportData:
    columns: list[Column]
    rows: list[dict[str, Any]]
    summary: list[tuple[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class ReportPeriod:
    start: date | None
    end: date | None
    today: date


@dataclass(frozen=True)
class ReportDefinition:
    key: str
    title: str
    description: str
    feature: Feature
    period_label: str
    default_period: Callable[[date], tuple[date | None, date | None]]
    build: Callable[[Session, OrgContext, ReportPeriod], ReportData]


# ------------------------------------------------------------------ helpers


def _category(value: str) -> str:
    return DOCUMENT_CATEGORIES.get(value, value)


def _active_documents(db: Session, ctx: OrgContext):
    return select(Document).where(
        Document.organization_id == ctx.organization_id, Document.archived_at.is_(None)
    )


def _owner(document: Document) -> str:
    return document.owner.user.full_name if document.owner else ""


def _belongs(document: Document) -> str:
    return document.entity.name if document.entity else ""


_STATUS_LABELS = {
    DocumentStatus.VALID: "Valid",
    DocumentStatus.EXPIRING_SOON: "Expiring soon",
    DocumentStatus.EXPIRED: "Expired",
    DocumentStatus.ARCHIVED: "Archived",
}
_REVIEW_LABELS = {"pending": "Pending review", "verified": "Verified", "rejected": "Rejected"}
_RENEWAL_LABELS = {
    "not_started": "Not started",
    "requested": "Requested",
    "in_progress": "In progress",
    "submitted": "File uploaded",
    "awaiting_approval": "Awaiting approval",
    "completed": "Completed",
    "rejected": "Rejected",
}


# ----------------------------------------------------------------- reports


def expiring_report(db: Session, ctx: OrgContext, period: ReportPeriod) -> ReportData:
    start = period.start or period.today
    end = period.end or period.today + timedelta(days=90)
    documents = db.scalars(
        _active_documents(db, ctx)
        .where(Document.expiry_date >= start, Document.expiry_date <= end)
        .order_by(Document.expiry_date, Document.name)
    ).all()
    rows = [
        {
            "name": document.name,
            "category": _category(document.category),
            "number": document.document_number or "",
            "belongs_to": _belongs(document),
            "owner": _owner(document),
            "expiry_date": document.expiry_date,
            "days_left": (document.expiry_date - period.today).days,
            "renewal": _RENEWAL_LABELS.get(document.open_renewal.status, "")
            if document.open_renewal
            else "None",
        }
        for document in documents
    ]
    no_renewal = sum(1 for row in rows if row["renewal"] == "None")
    return ReportData(
        columns=[
            Column("name", "Document", 2),
            Column("category", "Type", 1.4),
            Column("number", "Number", 1),
            Column("belongs_to", "Belongs to", 1.3),
            Column("owner", "Owner", 1.2),
            Column("expiry_date", "Expires", 0.9),
            Column("days_left", "Days left", 0.7),
            Column("renewal", "Renewal", 1),
        ],
        rows=rows,
        summary=[("Documents expiring", len(rows)), ("Without a renewal started", no_renewal)],
    )


def expired_report(db: Session, ctx: OrgContext, period: ReportPeriod) -> ReportData:
    query = _active_documents(db, ctx).where(Document.expiry_date < period.today)
    if period.start:
        query = query.where(Document.expiry_date >= period.start)
    if period.end:
        query = query.where(Document.expiry_date <= period.end)
    documents = db.scalars(query.order_by(Document.expiry_date, Document.name)).all()
    rows = [
        {
            "name": document.name,
            "category": _category(document.category),
            "belongs_to": _belongs(document),
            "owner": _owner(document),
            "expiry_date": document.expiry_date,
            "days_overdue": (period.today - document.expiry_date).days,
            "renewal": _RENEWAL_LABELS.get(document.open_renewal.status, "")
            if document.open_renewal
            else "None",
        }
        for document in documents
    ]
    return ReportData(
        columns=[
            Column("name", "Document", 2),
            Column("category", "Type", 1.4),
            Column("belongs_to", "Belongs to", 1.3),
            Column("owner", "Owner", 1.2),
            Column("expiry_date", "Expired on", 0.9),
            Column("days_overdue", "Days overdue", 0.8),
            Column("renewal", "Renewal", 1),
        ],
        rows=rows,
        summary=[
            ("Expired documents", len(rows)),
            ("Renewal in progress", sum(1 for row in rows if row["renewal"] != "None")),
        ],
    )


def _status_counts(documents: list[Document], today: date) -> dict[str, int]:
    counts = {"total": 0, "valid": 0, "expiring_soon": 0, "expired": 0}
    for document in documents:
        status = compute_status(document.expiry_date, today)
        counts["total"] += 1
        counts[status.value] += 1
    return counts


def category_report(db: Session, ctx: OrgContext, period: ReportPeriod) -> ReportData:
    groups: dict[str, list[Document]] = defaultdict(list)
    for document in db.scalars(_active_documents(db, ctx)):
        groups[document.category].append(document)
    rows = []
    for category, documents in sorted(groups.items(), key=lambda item: (-len(item[1]), _category(item[0]))):
        counts = _status_counts(documents, period.today)
        rows.append({"category": _category(category), **counts})
    totals = _status_counts([doc for docs in groups.values() for doc in docs], period.today)
    return ReportData(
        columns=[
            Column("category", "Document type", 2.4),
            Column("total", "Documents", 1),
            Column("valid", "Valid", 1),
            Column("expiring_soon", "Expiring soon", 1),
            Column("expired", "Expired", 1),
        ],
        rows=rows,
        summary=[
            ("Documents", totals["total"]),
            ("Expiring soon", totals["expiring_soon"]),
            ("Expired", totals["expired"]),
        ],
    )


def _entity_report(entity_type: str, label: str):
    def build(db: Session, ctx: OrgContext, period: ReportPeriod) -> ReportData:
        entities = db.scalars(
            select(Entity)
            .where(
                Entity.organization_id == ctx.organization_id,
                Entity.type == entity_type,
                Entity.archived_at.is_(None),
            )
            .order_by(Entity.name)
        ).all()
        documents: dict[Any, list[Document]] = defaultdict(list)
        for document in db.scalars(
            _active_documents(db, ctx).where(Document.entity_id.in_([e.id for e in entities]))
        ):
            documents[document.entity_id].append(document)
        rows = []
        for entity in entities:
            own = documents.get(entity.id, [])
            counts = _status_counts(own, period.today)
            upcoming = sorted(d.expiry_date for d in own if d.expiry_date and d.expiry_date >= period.today)
            rows.append(
                {
                    "name": entity.name,
                    "code": entity.code or "",
                    "responsible": entity.owner.user.full_name if entity.owner else "",
                    **counts,
                    "next_expiry": upcoming[0] if upcoming else "",
                }
            )
        attention = sum(1 for row in rows if row["expired"] or row["expiring_soon"])
        return ReportData(
            columns=[
                Column("name", label, 2),
                Column("code", "Code", 1),
                Column("responsible", "Responsible", 1.3),
                Column("total", "Documents", 0.8),
                Column("valid", "Valid", 0.7),
                Column("expiring_soon", "Expiring", 0.7),
                Column("expired", "Expired", 0.7),
                Column("next_expiry", "Next expiry", 1),
            ],
            rows=rows,
            summary=[(f"{label}s", len(rows)), ("Need attention", attention)],
        )

    return build


def pending_verification_report(db: Session, ctx: OrgContext, period: ReportPeriod) -> ReportData:
    documents = db.scalars(
        _active_documents(db, ctx)
        .where(Document.verification_status.in_(("pending", "rejected")))
        .order_by(Document.created_at)
    ).all()
    rows = []
    for document in documents:
        current = document.current_version
        rows.append(
            {
                "name": document.name,
                "category": _category(document.category),
                "review": _REVIEW_LABELS[document.verification_status],
                "reason": document.rejection_reason or "",
                "uploaded_by": current.uploaded_by.full_name if current and current.uploaded_by else "",
                "uploaded": document.created_at.astimezone(
                    organization_zone(ctx.organization.timezone)
                ).date(),
                "waiting_days": (period.today - document.created_at.date()).days,
            }
        )
    return ReportData(
        columns=[
            Column("name", "Document", 2),
            Column("category", "Type", 1.3),
            Column("review", "Review", 1),
            Column("reason", "Reason", 1.6),
            Column("uploaded_by", "Uploaded by", 1.2),
            Column("uploaded", "Uploaded", 0.9),
            Column("waiting_days", "Days waiting", 0.8),
        ],
        rows=rows,
        summary=[
            ("Waiting for review", sum(1 for row in rows if row["review"] == "Pending review")),
            ("Rejected", sum(1 for row in rows if row["review"] == "Rejected")),
        ],
    )


def _day_bounds(ctx: OrgContext, start: date, end: date) -> tuple[datetime, datetime]:
    zone = organization_zone(ctx.organization.timezone)
    return datetime.combine(start, time.min, zone), datetime.combine(end + timedelta(days=1), time.min, zone)


def renewal_performance_report(db: Session, ctx: OrgContext, period: ReportPeriod) -> ReportData:
    start = period.start or period.today - timedelta(days=90)
    end = period.end or period.today
    since, until = _day_bounds(ctx, start, end)
    zone = organization_zone(ctx.organization.timezone)
    renewals = db.scalars(
        select(Renewal)
        .where(
            Renewal.organization_id == ctx.organization_id,
            Renewal.status == RenewalStatus.COMPLETED,
            Renewal.completed_at >= since,
            Renewal.completed_at < until,
        )
        .order_by(Renewal.completed_at)
    ).all()
    rows = []
    for renewal in renewals:
        completed = renewal.completed_at.astimezone(zone).date()
        started = renewal.created_at.astimezone(zone).date()
        on_time = renewal.deadline is None or completed <= renewal.deadline
        rows.append(
            {
                "document": renewal.document.name,
                "assignee": renewal.assignee.user.full_name if renewal.assignee else "",
                "started": started,
                "deadline": renewal.deadline or "",
                "completed": completed,
                "days_taken": (completed - started).days,
                "on_time": "Yes" if on_time else "No",
            }
        )
    open_overdue = len(
        db.scalars(
            select(Renewal.id).where(
                Renewal.organization_id == ctx.organization_id,
                Renewal.status.in_(RenewalStatus.OPEN),
                Renewal.deadline < period.today,
            )
        ).all()
    )
    on_time = sum(1 for row in rows if row["on_time"] == "Yes")
    average = round(sum(row["days_taken"] for row in rows) / len(rows), 1) if rows else 0
    return ReportData(
        columns=[
            Column("document", "Document", 2),
            Column("assignee", "Assigned to", 1.3),
            Column("started", "Started", 0.9),
            Column("deadline", "Deadline", 0.9),
            Column("completed", "Completed", 0.9),
            Column("days_taken", "Days taken", 0.7),
            Column("on_time", "On time", 0.6),
        ],
        rows=rows,
        summary=[
            ("Renewals completed", len(rows)),
            ("On time", f"{round(100 * on_time / len(rows))}%" if rows else "–"),
            ("Average days taken", average if rows else "–"),
            ("Open and overdue now", open_overdue),
        ],
    )


def _describe(entry: AuditLog) -> str:
    data = entry.event_metadata or {}
    subject = data.get("name") or data.get("document") or data.get("email") or ""
    return f"{entry.action.replace('.', ': ').replace('_', ' ')}{(' - ' + str(subject)) if subject else ''}"


def activity_report(db: Session, ctx: OrgContext, period: ReportPeriod) -> ReportData:
    start = period.start or period.today - timedelta(days=30)
    end = period.end or period.today
    since, until = _day_bounds(ctx, start, end)
    zone = organization_zone(ctx.organization.timezone)
    entries = db.scalars(
        select(AuditLog)
        .where(
            AuditLog.organization_id == ctx.organization_id,
            AuditLog.created_at >= since,
            AuditLog.created_at < until,
        )
        .order_by(AuditLog.created_at.desc())
        .limit(MAX_EXPORT_ROWS + 1)
    ).all()
    rows = [
        {
            "when": entry.created_at.astimezone(zone).strftime("%Y-%m-%d %H:%M"),
            "who": entry.actor.full_name if entry.actor else "System or former member",
            "action": _describe(entry),
            "resource": entry.resource_type,
        }
        for entry in entries
    ]
    people = len({row["who"] for row in rows})
    return ReportData(
        columns=[
            Column("when", "When", 1.1),
            Column("who", "Who", 1.3),
            Column("action", "What happened", 3),
            Column("resource", "Area", 0.8),
        ],
        rows=rows,
        summary=[("Events", len(rows)), ("People", people)],
    )


# ----------------------------------------------------------------- registry


def _next_days(days: int):
    return lambda today: (today, today + timedelta(days=days))


def _past_days(days: int):
    return lambda today: (today - timedelta(days=days), today)


def _no_period(today: date) -> tuple[None, None]:
    return None, None


BASIC, ADVANCED = Feature.BASIC_REPORTS, Feature.ADVANCED_REPORTS
REPORTS: dict[str, ReportDefinition] = {
    report.key: report
    for report in [
        ReportDefinition(
            "expiring", "Expiring documents", "Documents that expire within the period, soonest first.",
            BASIC, "Expiry date", _next_days(90), expiring_report),
        ReportDefinition(
            "expired", "Expired documents", "Active documents whose expiry date has passed.",
            BASIC, "Expiry date", _no_period, expired_report),
        ReportDefinition(
            "by_category", "Documents by type",
            "How many documents of each type are valid, expiring or expired.",
            BASIC, "", _no_period, category_report),
        ReportDefinition(
            "pending_verification", "Pending verification",
            "Documents waiting for review, and rejected ones.",
            BASIC, "", _no_period, pending_verification_report),
        ReportDefinition(
            "by_supplier", "Documents by supplier", "Each supplier's documents and what needs attention.",
            ADVANCED, "", _no_period, _entity_report("supplier", "Supplier")),
        ReportDefinition(
            "by_employee", "Documents by employee",
            "Each employee's certifications and what needs attention.",
            ADVANCED, "", _no_period, _entity_report("employee", "Employee")),
        ReportDefinition(
            "renewal_performance", "Renewal performance",
            "Renewals completed in the period, and whether they were on time.",
            ADVANCED, "Completed", _past_days(90), renewal_performance_report),
        ReportDefinition(
            "activity", "Activity history", "Everything recorded in the audit log during the period.",
            ADVANCED, "Date", _past_days(30), activity_report),
    ]
}  # fmt: skip


@dataclass
class ReportResult:
    definition: ReportDefinition
    period: ReportPeriod
    data: ReportData
    generated_at: datetime
    truncated: bool = False


class ReportService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def catalog(self, ctx: OrgContext) -> list[tuple[ReportDefinition, bool]]:
        entitlements = Entitlements(self.db, ctx.organization_id)
        return [(report, entitlements.has(report.feature)) for report in REPORTS.values()]

    def run(
        self, ctx: OrgContext, key: str, *, start: date | None, end: date | None, export: str | None = None
    ) -> ReportResult:
        definition = REPORTS.get(key)
        if definition is None:
            raise NotFoundError("This report does not exist.")
        entitlements = Entitlements(self.db, ctx.organization_id)
        entitlements.require(definition.feature)
        if export == "csv":
            entitlements.require(Feature.CSV_EXPORT)
        elif export == "pdf":
            entitlements.require(Feature.PDF_EXPORT)
        if start and end and end < start:
            raise ValidationFailedError("The end of the period is before its start.", code="invalid_range")
        if start and end and (end - start).days > MAX_PERIOD_DAYS:
            raise ValidationFailedError("Choose a period of at most three years.", code="range_too_long")

        today = organization_today(ctx.organization.timezone)
        default_start, default_end = definition.default_period(today)
        period = ReportPeriod(start or default_start, end or default_end, today)
        data = definition.build(self.db, ctx, period)
        limit = MAX_EXPORT_ROWS if export else PREVIEW_ROWS
        truncated = len(data.rows) > limit
        data.rows = data.rows[:limit]
        result = ReportResult(definition, period, data, utcnow(), truncated)
        if export:
            AuditService(self.db).record_for(
                ctx,
                action="report.exported",
                resource_type="report",
                resource_id=key,
                metadata={
                    "name": definition.title,
                    "format": export,
                    "rows": len(data.rows),
                    "start": period.start.isoformat() if period.start else None,
                    "end": period.end.isoformat() if period.end else None,
                },
            )
        self.db.commit()
        return result


# ------------------------------------------------------------------- output


def plain_value(value: Any) -> Any:
    if isinstance(value, date):
        return value.isoformat()
    return value


_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value: Any) -> Any:
    """Stop spreadsheets from running a cell as a formula (CSV injection)."""
    value = plain_value(value)
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def to_csv(result: ReportResult) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([column.label for column in result.data.columns])
    for row in result.data.rows:
        writer.writerow([_csv_safe(row.get(column.key, "")) for column in result.data.columns])
    # UTF-8 with a byte-order mark so that Excel shows accented names correctly.
    return buffer.getvalue().encode("utf-8-sig")


_ASCII_FALLBACKS = str.maketrans({"–": "-", "—": " - ", "‘": "'", "’": "'", "“": '"', "”": '"', "…": "..."})


def _latin(text: Any) -> str:
    """The PDF uses the built-in Helvetica font, which covers Latin-1 only."""
    value = str(plain_value(text) if text is not None else "").translate(_ASCII_FALLBACKS)
    return value.encode("latin-1", "replace").decode("latin-1")


def to_pdf(result: ReportResult, organization_name: str) -> bytes:
    from fpdf import FPDF

    navy, muted, line = (20, 39, 78), (92, 103, 120), (226, 230, 236)
    report = result.definition

    class ReportPDF(FPDF):
        def footer(self) -> None:
            self.set_y(-12)
            self.set_font("Helvetica", size=8)
            self.set_text_color(*muted)
            self.cell(0, 6, _latin(f"NOVALYN | {organization_name}"), align="L")
            self.cell(0, 6, f"Page {self.page_no()} of {{nb}}", align="R")

    pdf = ReportPDF(orientation="L", unit="mm", format="A4")
    pdf.alias_nb_pages()
    pdf.set_auto_page_break(auto=True, margin=16)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_text_color(*navy)
    pdf.cell(0, 6, "NOVALYN", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, _latin(report.title), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", size=9)
    pdf.set_text_color(*muted)
    period = ""
    if result.period.start or result.period.end:
        start, end = result.period.start or "the start", result.period.end or "today"
        period = f"  |  {report.period_label or 'Period'}: {_latin(start)} to {_latin(end)}"
    generated = result.generated_at.strftime("%Y-%m-%d %H:%M UTC")
    pdf.cell(
        0, 5, _latin(f"{organization_name}{period}  |  Generated {generated}"), new_x="LMARGIN", new_y="NEXT"
    )
    pdf.ln(3)

    if result.data.summary:
        pdf.set_text_color(*navy)
        for label, _value in result.data.summary:
            pdf.set_font("Helvetica", size=8)
            pdf.cell(45, 5, _latin(label))
        pdf.ln(5)
        for _, value in result.data.summary:
            pdf.set_font("Helvetica", "B", 13)
            pdf.cell(45, 8, _latin(value))
        pdf.ln(11)

    columns = result.data.columns
    total_width = pdf.w - pdf.l_margin - pdf.r_margin
    weight = sum(column.width for column in columns)
    widths = [total_width * column.width / weight for column in columns]

    def header() -> None:
        pdf.set_fill_color(241, 243, 247)
        pdf.set_text_color(*muted)
        pdf.set_font("Helvetica", "B", 8)
        for column, width in zip(columns, widths, strict=True):
            pdf.cell(width, 7, _latin(column.label), border="B", fill=True)
        pdf.ln(7)

    header()
    pdf.set_draw_color(*line)
    for row in result.data.rows:
        if pdf.get_y() > pdf.h - 24:
            pdf.add_page()
            header()
        pdf.set_font("Helvetica", size=8)
        pdf.set_text_color(15, 23, 42)
        for column, width in zip(columns, widths, strict=True):
            text = _latin(row.get(column.key, ""))
            # Fit the text in the cell; long values are shortened rather than overflowing.
            while text and pdf.get_string_width(text) > width - 2:
                text = text[:-4] + "..." if len(text) > 4 else ""
                text = _latin(text)
            pdf.cell(width, 6.5, text, border="B")
        pdf.ln(6.5)
    if not result.data.rows:
        pdf.set_font("Helvetica", "I", 9)
        pdf.cell(0, 8, "Nothing to report for this period.")
    if result.truncated:
        pdf.ln(4)
        pdf.set_font("Helvetica", "I", 8)
        pdf.cell(0, 5, f"Only the first {MAX_EXPORT_ROWS} rows are included.")
    return bytes(pdf.output())
