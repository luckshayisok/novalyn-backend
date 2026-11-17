import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, UniqueConstraint, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.permissions import Role
from app.core.tenancy import TenantScoped
from app.core.time import utcnow
from app.models.mixins import CreatedAt, Timestamps, UUIDPrimaryKey
from app.models.types import JSONType, UTCDateTime
from app.models.user import User

_ROLE_VALUES = ", ".join(f"'{role.value}'" for role in Role)


class Organization(UUIDPrimaryKey, Timestamps, Base):
    """A tenant. Every tenant-owned row points here through organization_id."""

    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    industry: Mapped[str | None] = mapped_column(String(80))
    company_size: Mapped[str | None] = mapped_column(String(32))
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", nullable=False)
    document_categories: Mapped[list[str]] = mapped_column(JSONType, default=list, nullable=False)
    onboarding_completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"))


class Membership(UUIDPrimaryKey, Timestamps, TenantScoped, Base):
    """A user's role in one organization."""

    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("organization_id", "user_id"),
        CheckConstraint(f"role IN ({_ROLE_VALUES})", name="role_valid"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)

    user: Mapped[User] = relationship(lazy="joined")


class Invitation(UUIDPrimaryKey, CreatedAt, TenantScoped, Base):
    """An invitation for an email address to join an organization with a role."""

    __tablename__ = "invitations"
    __table_args__ = (
        CheckConstraint(f"role IN ({_ROLE_VALUES})", name="role_valid"),
        # One open invitation per address per organization.
        Index(
            "uq_invitations_open_email",
            "organization_id",
            "email",
            unique=True,
            postgresql_where=text("accepted_at IS NULL AND revoked_at IS NULL"),
            sqlite_where=text("accepted_at IS NULL AND revoked_at IS NULL"),
        ),
    )

    email: Mapped[str] = mapped_column(String(320), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    invited_by_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"))
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    last_sent_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    accepted_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    invited_by: Mapped[User | None] = relationship(foreign_keys=[invited_by_id], lazy="joined")

    @property
    def status(self) -> str:
        if self.accepted_at is not None:
            return "accepted"
        if self.revoked_at is not None:
            return "revoked"
        if self.expires_at <= utcnow():
            return "expired"
        return "pending"
