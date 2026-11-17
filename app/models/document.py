import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.constants import ENTITY_TYPES
from app.core.database import Base
from app.core.tenancy import TenantScoped
from app.models.mixins import CreatedAt, Timestamps, UUIDPrimaryKey
from app.models.organization import Membership
from app.models.types import JSONType, UTCDateTime
from app.models.user import User

_ENTITY_TYPE_VALUES = ", ".join(f"'{value}'" for value in ENTITY_TYPES)


class VerificationStatus:
    PENDING = "pending"
    VERIFIED = "verified"
    REJECTED = "rejected"

    ALL = (PENDING, VERIFIED, REJECTED)


class Entity(UUIDPrimaryKey, Timestamps, TenantScoped, Base):
    """Something documents belong to: an employee, supplier, piece of equipment, location or department."""

    __tablename__ = "entities"
    __table_args__ = (
        CheckConstraint(f"type IN ({_ENTITY_TYPE_VALUES})", name="type_valid"),
        Index("ix_entities_org_type", "organization_id", "type"),
    )

    type: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    code: Mapped[str | None] = mapped_column(String(100))
    email: Mapped[str | None] = mapped_column(String(320))
    phone: Mapped[str | None] = mapped_column(String(50))
    contact_name: Mapped[str | None] = mapped_column(String(200))
    address: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    location_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("entities.id", ondelete="SET NULL")
    )
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("entities.id", ondelete="SET NULL")
    )
    owner_membership_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("memberships.id", ondelete="SET NULL")
    )
    archived_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"))

    location: Mapped["Entity | None"] = relationship(
        foreign_keys=[location_id], remote_side="Entity.id", lazy="selectin"
    )
    department: Mapped["Entity | None"] = relationship(
        foreign_keys=[department_id], remote_side="Entity.id", lazy="selectin"
    )
    owner: Mapped[Membership | None] = relationship(lazy="selectin")


class Document(UUIDPrimaryKey, Timestamps, TenantScoped, Base):
    """A business document and its details. The files themselves are document versions."""

    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint(
            "verification_status IN ('pending', 'verified', 'rejected')", name="verification_status_valid"
        ),
        Index("ix_documents_org_expiry", "organization_id", "expiry_date"),
        Index("ix_documents_org_category", "organization_id", "category"),
        Index("ix_documents_entity_id", "entity_id"),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False)
    document_number: Mapped[str | None] = mapped_column(String(100))
    issuing_authority: Mapped[str | None] = mapped_column(String(200))
    holder_name: Mapped[str | None] = mapped_column(String(200))
    issue_date: Mapped[date | None] = mapped_column(Date)
    expiry_date: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)

    entity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("entities.id", ondelete="SET NULL"))
    owner_membership_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("memberships.id", ondelete="SET NULL")
    )

    verification_status: Mapped[str] = mapped_column(
        String(32), default=VerificationStatus.PENDING, nullable=False
    )
    verified_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    rejection_reason: Mapped[str | None] = mapped_column(String(500))

    archived_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"))

    entity: Mapped[Entity | None] = relationship(lazy="selectin")
    owner: Mapped[Membership | None] = relationship(lazy="selectin")
    verified_by: Mapped[User | None] = relationship(foreign_keys=[verified_by_id], lazy="selectin")
    versions: Mapped[list["DocumentVersion"]] = relationship(
        back_populates="document",
        order_by="DocumentVersion.version_number.desc()",
        lazy="selectin",
    )

    @property
    def current_version(self) -> "DocumentVersion | None":
        return self.versions[0] if self.versions else None


class DocumentVersion(UUIDPrimaryKey, CreatedAt, TenantScoped, Base):
    """One uploaded file. Versions are kept for good; replacing a document adds a version."""

    __tablename__ = "document_versions"
    __table_args__ = (UniqueConstraint("document_id", "version_number"),)

    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(120), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    note: Mapped[str | None] = mapped_column(String(500))
    uploaded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )

    document: Mapped[Document] = relationship(back_populates="versions")
    uploaded_by: Mapped[User | None] = relationship(lazy="joined")
    extraction: Mapped["DocumentExtraction | None"] = relationship(
        back_populates="version", uselist=False, lazy="selectin"
    )


class ExtractionStatus:
    PENDING = "pending"
    PROCESSING = "processing"
    EXTRACTION_COMPLETED = "extraction_completed"
    REVIEW_REQUIRED = "review_required"
    FAILED = "failed"
    VERIFIED = "verified"
    DISMISSED = "dismissed"

    ALL = (PENDING, PROCESSING, EXTRACTION_COMPLETED, REVIEW_REQUIRED, FAILED, VERIFIED, DISMISSED)
    READY = (EXTRACTION_COMPLETED, REVIEW_REQUIRED)
    BUSY = (PENDING, PROCESSING)


_EXTRACTION_STATUSES = ", ".join(f"'{value}'" for value in ExtractionStatus.ALL)


class DocumentExtraction(UUIDPrimaryKey, Timestamps, TenantScoped, Base):
    """What was read automatically from one file version: suggestions waiting for a person.

    Nothing here changes the document until someone reviews it and applies it.
    """

    __tablename__ = "document_extractions"
    __table_args__ = (CheckConstraint(f"status IN ({_EXTRACTION_STATUSES})", name="status_valid"),)

    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    status: Mapped[str] = mapped_column(String(32), default=ExtractionStatus.PENDING, nullable=False)
    provider: Mapped[str | None] = mapped_column(String(50))
    model: Mapped[str | None] = mapped_column(String(100))
    text_source: Mapped[str | None] = mapped_column(String(32))  # pdf_text, docx, ocr, file
    fields: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    warnings: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    error: Mapped[str | None] = mapped_column(String(500))
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    reviewed_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    applied_fields: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)

    version: Mapped[DocumentVersion] = relationship(back_populates="extraction")
    reviewed_by: Mapped[User | None] = relationship(foreign_keys=[reviewed_by_id], lazy="selectin")
