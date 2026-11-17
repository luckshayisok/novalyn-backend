import uuid
from typing import Any

from sqlalchemy import ForeignKey, Index, String, Uuid, event
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.tenancy import TenantScoped
from app.models.mixins import CreatedAt, UUIDPrimaryKey
from app.models.types import JSONType
from app.models.user import User


class AuditLog(UUIDPrimaryKey, CreatedAt, TenantScoped, Base):
    """An append-only record of something that happened in an organization."""

    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_logs_org_created", "organization_id", "created_at"),)

    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(50), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(64))
    event_metadata: Mapped[dict[str, Any]] = mapped_column("metadata", JSONType, default=dict, nullable=False)
    ip_address: Mapped[str | None] = mapped_column(String(64))

    actor: Mapped[User | None] = relationship(lazy="joined")


class AuditLogImmutableError(RuntimeError):
    pass


@event.listens_for(AuditLog, "before_update")
def _reject_update(_mapper, _connection, _target) -> None:
    raise AuditLogImmutableError("Audit log entries cannot be modified")


@event.listens_for(AuditLog, "before_delete")
def _reject_delete(_mapper, _connection, _target) -> None:
    raise AuditLogImmutableError("Audit log entries cannot be deleted")
