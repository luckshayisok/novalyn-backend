"""Audit log schemas."""

import uuid
from datetime import datetime
from typing import Any

from app.models import AuditLog
from app.schemas.common import Schema


class AuditActorOut(Schema):
    id: uuid.UUID
    full_name: str
    email: str


class AuditLogOut(Schema):
    id: uuid.UUID
    action: str
    resource_type: str
    resource_id: str | None
    metadata: dict[str, Any]
    actor: AuditActorOut | None
    created_at: datetime

    @classmethod
    def build(cls, entry: AuditLog) -> "AuditLogOut":
        return cls(
            id=entry.id,
            action=entry.action,
            resource_type=entry.resource_type,
            resource_id=entry.resource_id,
            metadata=entry.event_metadata or {},
            actor=AuditActorOut.model_validate(entry.actor) if entry.actor else None,
            created_at=entry.created_at,
        )
