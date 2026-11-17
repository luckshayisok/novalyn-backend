"""Time helpers. All timestamps in the application are timezone-aware UTC."""

from datetime import UTC, datetime


def utcnow() -> datetime:
    return datetime.now(UTC)
