"""Column types that behave the same on PostgreSQL and SQLite."""

from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator):
    """Timezone-aware UTC datetime.

    SQLite drops the offset on storage, so values are normalised to UTC on the
    way in and tagged as UTC on the way out. Naive datetimes are rejected.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Naive datetime passed to a UTCDateTime column")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


JSONType = JSON().with_variant(JSONB(), "postgresql")
