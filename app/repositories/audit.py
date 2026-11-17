"""Repository for the audit log. Insert and read only."""

from sqlalchemy import func, select

from app.models import AuditLog
from app.repositories.base import TenantRepository


class AuditLogRepository(TenantRepository[AuditLog]):
    model = AuditLog

    def delete(self, instance: AuditLog) -> None:  # pragma: no cover - guard
        raise NotImplementedError("Audit log entries cannot be deleted")

    def list(
        self, *, page: int, page_size: int, action_prefix: str | None = None
    ) -> tuple[list[AuditLog], int]:
        conditions = [AuditLog.organization_id == self.organization_id]
        if action_prefix:
            conditions.append(AuditLog.action.startswith(action_prefix, autoescape=True))

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
