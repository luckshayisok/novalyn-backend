"""Calendar and report schemas."""

import uuid
from datetime import date, datetime
from typing import Any

from app.schemas.common import Schema
from app.services.calendar import CalendarEvent
from app.services.reports import ReportDefinition, ReportResult, plain_value


class CalendarEventOut(Schema):
    id: str
    type: str
    date: date
    title: str
    document_id: uuid.UUID
    document_name: str
    category: str
    status: str
    renewal_id: uuid.UUID | None = None
    owner_name: str | None = None
    entity_name: str | None = None

    @classmethod
    def build(cls, event: CalendarEvent) -> "CalendarEventOut":
        return cls.model_validate(event)


class CalendarOut(Schema):
    start: date
    end: date
    today: date
    events: list[CalendarEventOut]


class ReportInfo(Schema):
    key: str
    title: str
    description: str
    feature: str
    allowed: bool
    has_period: bool
    period_label: str
    default_start: date | None
    default_end: date | None

    @classmethod
    def build(cls, definition: ReportDefinition, allowed: bool, today: date) -> "ReportInfo":
        start, end = definition.default_period(today)
        return cls(
            key=definition.key,
            title=definition.title,
            description=definition.description,
            feature=definition.feature.value,
            allowed=allowed,
            has_period=bool(definition.period_label),
            period_label=definition.period_label,
            default_start=start,
            default_end=end,
        )


class ReportCatalog(Schema):
    reports: list[ReportInfo]
    can_export: bool
    csv_export: bool
    pdf_export: bool


class ReportColumnOut(Schema):
    key: str
    label: str


class SummaryItem(Schema):
    label: str
    value: Any


class ReportOut(Schema):
    key: str
    title: str
    description: str
    start: date | None
    end: date | None
    generated_at: datetime
    columns: list[ReportColumnOut]
    rows: list[dict[str, Any]]
    summary: list[SummaryItem]
    truncated: bool

    @classmethod
    def build(cls, result: ReportResult) -> "ReportOut":
        return cls(
            key=result.definition.key,
            title=result.definition.title,
            description=result.definition.description,
            start=result.period.start,
            end=result.period.end,
            generated_at=result.generated_at,
            columns=[ReportColumnOut(key=column.key, label=column.label) for column in result.data.columns],
            rows=[{key: plain_value(value) for key, value in row.items()} for row in result.data.rows],
            summary=[
                SummaryItem(label=label, value=plain_value(value)) for label, value in result.data.summary
            ],
            truncated=result.truncated,
        )
