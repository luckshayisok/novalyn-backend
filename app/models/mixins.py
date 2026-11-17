"""Shared model mixins."""

import uuid
from datetime import datetime

from sqlalchemy import Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.core.time import utcnow
from app.models.types import UTCDateTime


class UUIDPrimaryKey:
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)


class CreatedAt:
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


class Timestamps(CreatedAt):
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow, nullable=False)
