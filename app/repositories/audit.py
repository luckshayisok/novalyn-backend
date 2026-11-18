"""Repository for the audit log. Insert and read only."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select

from app.models import AuditLog
from app.repositories.base import TenantRepository


@dataclass(frozen=True)
class AuditFilters:
    action_prefix: str | None = None
    actor_user_id: uuid.UUID | None = None
    resource_type: str | None = None
    since: datetime | None = None  # inclusive
    until: datetime | None = None  # exclusive


class AuditLogRepository(TenantRepository[AuditLog]):
    model = AuditLog

    def delete(self, instance: AuditLog) -> None:  # pragma: no cover - guard
        raise NotImplementedError("Audit log entries cannot be deleted")

    def list(
        self, *, page: int, page_size: int, filters: AuditFilters | None = None
    ) -> tuple[list[AuditLog], int]:
        filters = filters or AuditFilters()
        conditions = [AuditLog.organization_id == self.organization_id]
        if filters.action_prefix:
            conditions.append(AuditLog.action.startswith(filters.action_prefix, autoescape=True))
        if filters.actor_user_id:
            conditions.append(AuditLog.actor_user_id == filters.actor_user_id)
        if filters.resource_type:
            conditions.append(AuditLog.resource_type == filters.resource_type)
        if filters.since:
            conditions.append(AuditLog.created_at >= filters.since)
        if filters.until:
            conditions.append(AuditLog.created_at < filters.until)

        total = self.db.scalar(select(func.count()).select_from(AuditLog).where(*conditions)) or 0
        items = list(
            self.db.scalars(
                select(AuditLog)
                .where(*conditions)
                .order_by(AuditLog.created_at.desc(), AuditLog.id)
                .limit(page_size)
                .offset((page - 1) * page_size)
            )
        )
        return items, total
