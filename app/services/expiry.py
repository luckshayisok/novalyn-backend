"""Expiry calculation: the one place that decides whether a document is valid, expiring or expired.

Status is derived from the expiry date every time it is needed, never stored,
so it cannot go stale. "Today" is the date in the organization's own time zone.
"""

from datetime import date, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import ColumnElement, and_, or_

from app.core.config import get_settings
from app.core.time import utcnow
from app.models import Document


class DocumentStatus(StrEnum):
    VALID = "valid"
    EXPIRING_SOON = "expiring_soon"
    EXPIRED = "expired"
    ARCHIVED = "archived"


def expiring_window_days() -> int:
    return get_settings().expiring_soon_days


def organization_zone(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def organization_today(timezone_name: str) -> date:
    return utcnow().astimezone(organization_zone(timezone_name)).date()


def compute_status(
    expiry_date: date | None, today: date, *, archived: bool = False, window_days: int | None = None
) -> DocumentStatus:
    """A document with no expiry date is valid. One that expires today is expiring, not expired."""
    if archived:
        return DocumentStatus.ARCHIVED
    if expiry_date is None:
        return DocumentStatus.VALID
    if expiry_date < today:
        return DocumentStatus.EXPIRED
    window = expiring_window_days() if window_days is None else window_days
    if expiry_date <= today + timedelta(days=window):
        return DocumentStatus.EXPIRING_SOON
    return DocumentStatus.VALID


def days_until_expiry(expiry_date: date | None, today: date) -> int | None:
    return (expiry_date - today).days if expiry_date is not None else None


def status_condition(status: DocumentStatus, today: date) -> ColumnElement[bool]:
    """The SQL condition equivalent to `compute_status(...) == status` for non-archived documents."""
    limit = today + timedelta(days=expiring_window_days())
    match status:
        case DocumentStatus.EXPIRED:
            return Document.expiry_date < today
        case DocumentStatus.EXPIRING_SOON:
            return and_(Document.expiry_date >= today, Document.expiry_date <= limit)
        case DocumentStatus.VALID:
            return or_(Document.expiry_date.is_(None), Document.expiry_date > limit)
        case _:
            return Document.archived_at.is_not(None)
