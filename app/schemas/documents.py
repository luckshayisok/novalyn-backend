"""Document, entity, dashboard and search schemas."""

import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import EmailStr, Field, field_validator

from app.core.constants import DOCUMENT_CATEGORIES, ENTITY_TYPES
from app.core.files import INLINE_CONTENT_TYPES
from app.models import Document, DocumentExtraction, DocumentVersion, Entity, Membership
from app.repositories.documents import EntityDocumentCounts
from app.schemas.common import Schema
from app.services.dashboard import Dashboard
from app.services.expiry import DocumentStatus, compute_status, days_until_expiry

EntityType = Literal["employee", "supplier", "equipment", "location", "department"]
Verification = Literal["pending", "verified", "rejected"]


def _blank_to_none(value: Any) -> Any:
    return None if isinstance(value, str) and not value.strip() else value


class _BlankAsNone(Schema):
    """Empty strings from forms mean "not provided"."""

    @field_validator("*", mode="before")
    @classmethod
    def _blank(cls, value: Any) -> Any:
        return _blank_to_none(value)


# ---------------------------------------------------------------------- shared


class OwnerOut(Schema):
    membership_id: uuid.UUID
    full_name: str
    email: str

    @classmethod
    def build(cls, membership: Membership | None) -> "OwnerOut | None":
        if membership is None:
            return None
        return cls(
            membership_id=membership.id, full_name=membership.user.full_name, email=membership.user.email
        )


class EntityRef(Schema):
    id: uuid.UUID
    name: str
    type: EntityType

    @classmethod
    def build(cls, entity: Entity | None) -> "EntityRef | None":
        return cls(id=entity.id, name=entity.name, type=entity.type) if entity else None


# -------------------------------------------------------------------- entities


class _EntityFields(_BlankAsNone):
    code: str | None = Field(default=None, max_length=100)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=50)
    contact_name: str | None = Field(default=None, max_length=200)
    address: str | None = Field(default=None, max_length=1000)
    notes: str | None = Field(default=None, max_length=5000)
    location_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None
    owner_membership_id: uuid.UUID | None = None


class EntityCreate(_EntityFields):
    type: EntityType
    name: str = Field(min_length=1, max_length=200)


class EntityUpdate(_EntityFields):
    name: str | None = Field(default=None, max_length=200)

    def changes(self) -> dict[str, Any]:
        return self.model_dump(exclude_unset=True)


class EntityOut(Schema):
    id: uuid.UUID
    type: EntityType
    name: str
    code: str | None
    email: str | None
    phone: str | None
    contact_name: str | None
    address: str | None
    notes: str | None
    location: EntityRef | None
    department: EntityRef | None
    owner: OwnerOut | None
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime
    document_count: int
    expiring_soon_count: int
    expired_count: int

    @classmethod
    def build(cls, entity: Entity, counts: EntityDocumentCounts | None = None) -> "EntityOut":
        counts = counts or EntityDocumentCounts()
        return cls(
            id=entity.id,
            type=entity.type,
            name=entity.name,
            code=entity.code,
            email=entity.email,
            phone=entity.phone,
            contact_name=entity.contact_name,
            address=entity.address,
            notes=entity.notes,
            location=EntityRef.build(entity.location),
            department=EntityRef.build(entity.department),
            owner=OwnerOut.build(entity.owner),
            archived_at=entity.archived_at,
            created_at=entity.created_at,
            updated_at=entity.updated_at,
            document_count=counts.total,
            expiring_soon_count=counts.expiring_soon,
            expired_count=counts.expired,
        )


class EntityTypeCount(Schema):
    type: EntityType
    label: str
    count: int


def entity_type_counts(counts: dict[str, int]) -> list[EntityTypeCount]:
    return [
        EntityTypeCount(type=type_, label=label, count=counts.get(type_, 0))
        for type_, label in ENTITY_TYPES.items()
    ]


# ------------------------------------------------------------------- documents


class _DocumentFields(_BlankAsNone):
    document_number: str | None = Field(default=None, max_length=100)
    issuing_authority: str | None = Field(default=None, max_length=200)
    holder_name: str | None = Field(default=None, max_length=200)
    issue_date: date | None = None
    expiry_date: date | None = None
    notes: str | None = Field(default=None, max_length=5000)
    entity_id: uuid.UUID | None = None
    owner_membership_id: uuid.UUID | None = None

    @field_validator("category", check_fields=False)
    @classmethod
    def _category(cls, value: str | None) -> str | None:
        if value is not None and value not in DOCUMENT_CATEGORIES:
            raise ValueError("Choose a document type")
        return value


class DocumentUploadForm(_DocumentFields):
    """Fields sent alongside the file when a document is uploaded."""

    name: str | None = Field(default=None, max_length=255)
    category: str

    def data(self) -> dict[str, Any]:
        return self.model_dump()


class DocumentUpdate(_DocumentFields):
    name: str | None = Field(default=None, max_length=255)
    category: str | None = None

    def changes(self) -> dict[str, Any]:
        data = self.model_dump(exclude_unset=True)
        for required in ("name", "category"):
            if required in data and data[required] is None:
                del data[required]
        return data


class VersionUploadForm(_BlankAsNone):
    """Fields sent alongside the file when a document is replaced."""

    issue_date: date | None = None
    expiry_date: date | None = None
    note: str | None = Field(default=None, max_length=500)

    def dates(self) -> dict[str, date | None]:
        return {
            key: value
            for key, value in self.model_dump(include={"issue_date", "expiry_date"}).items()
            if value
        }


class RejectRequest(Schema):
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Give a reason")
        return value.strip()


class FileUrlRequest(Schema):
    disposition: Literal["inline", "attachment"] = "attachment"


class FileUrlOut(Schema):
    url: str
    expires_in: int


class VersionOut(Schema):
    id: uuid.UUID
    version_number: int
    original_filename: str
    content_type: str
    size_bytes: int
    sha256: str
    note: str | None
    uploaded_by_name: str | None
    created_at: datetime
    previewable: bool

    @classmethod
    def build(cls, version: DocumentVersion) -> "VersionOut":
        return cls(
            id=version.id,
            version_number=version.version_number,
            original_filename=version.original_filename,
            content_type=version.content_type,
            size_bytes=version.size_bytes,
            sha256=version.sha256,
            note=version.note,
            uploaded_by_name=version.uploaded_by.full_name if version.uploaded_by else None,
            created_at=version.created_at,
            previewable=version.content_type in INLINE_CONTENT_TYPES,
        )


class DocumentOut(Schema):
    id: uuid.UUID
    name: str
    category: str
    document_number: str | None
    issuing_authority: str | None
    holder_name: str | None
    issue_date: date | None
    expiry_date: date | None
    notes: str | None
    status: DocumentStatus
    days_until_expiry: int | None
    verification_status: Verification
    verified_at: datetime | None
    verified_by_name: str | None
    rejection_reason: str | None
    entity: EntityRef | None
    owner: OwnerOut | None
    current_version: VersionOut | None
    version_count: int
    extraction_status: str | None
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def values_for(cls, document: Document, today: date) -> dict[str, Any]:
        current = document.current_version
        return {
            "id": document.id,
            "name": document.name,
            "category": document.category,
            "document_number": document.document_number,
            "issuing_authority": document.issuing_authority,
            "holder_name": document.holder_name,
            "issue_date": document.issue_date,
            "expiry_date": document.expiry_date,
            "notes": document.notes,
            "status": compute_status(document.expiry_date, today, archived=document.archived_at is not None),
            "days_until_expiry": days_until_expiry(document.expiry_date, today),
            "verification_status": document.verification_status,
            "verified_at": document.verified_at,
            "verified_by_name": document.verified_by.full_name if document.verified_by else None,
            "rejection_reason": document.rejection_reason,
            "entity": EntityRef.build(document.entity),
            "owner": OwnerOut.build(document.owner),
            "current_version": VersionOut.build(current) if current else None,
            "version_count": len(document.versions),
            "extraction_status": current.extraction.status if current and current.extraction else None,
            "archived_at": document.archived_at,
            "created_at": document.created_at,
            "updated_at": document.updated_at,
        }

    @classmethod
    def build(cls, document: Document, today: date) -> "DocumentOut":
        return cls(**cls.values_for(document, today))


class DocumentDetail(DocumentOut):
    versions: list[VersionOut]

    @classmethod
    def build(cls, document: Document, today: date) -> "DocumentDetail":
        return cls(
            **DocumentOut.values_for(document, today),
            versions=[VersionOut.build(version) for version in document.versions],
        )


# ------------------------------------------------------------------- dashboard


class DashboardMetrics(Schema):
    total: int
    valid: int
    expiring_soon: int
    expired: int
    pending_verification: int
    archived: int
    suggestions_to_review: int = 0


class CategoryCount(Schema):
    category: str
    label: str
    count: int


class MonthCount(Schema):
    month: str  # YYYY-MM
    count: int


class GroupCountOut(Schema):
    entity_id: uuid.UUID
    name: str
    type: EntityType
    count: int


class DashboardOut(Schema):
    today: date
    expiring_window_days: int
    metrics: DashboardMetrics
    by_category: list[CategoryCount]
    upcoming_expirations: list[MonthCount]
    by_group: list[GroupCountOut]
    ungrouped_count: int
    expiring_documents: list[DocumentOut]
    expired_documents: list[DocumentOut]
    recent_documents: list[DocumentOut]

    @classmethod
    def build(cls, dashboard: Dashboard) -> "DashboardOut":
        today = dashboard.today
        return cls(
            today=today,
            expiring_window_days=dashboard.expiring_window_days,
            metrics=DashboardMetrics(**dashboard.metrics),
            by_category=[
                CategoryCount(
                    category=category, label=DOCUMENT_CATEGORIES.get(category, category), count=count
                )
                for category, count in dashboard.by_category
            ],
            upcoming_expirations=[
                MonthCount(month=month, count=count) for month, count in dashboard.upcoming_expirations
            ],
            by_group=[
                GroupCountOut(
                    entity_id=uuid.UUID(group.entity_id), name=group.name, type=group.type, count=group.count
                )
                for group in dashboard.by_group
            ],
            ungrouped_count=dashboard.ungrouped_count,
            expiring_documents=[DocumentOut.build(item, today) for item in dashboard.expiring_documents],
            expired_documents=[DocumentOut.build(item, today) for item in dashboard.expired_documents],
            recent_documents=[DocumentOut.build(item, today) for item in dashboard.recent_documents],
        )


class SearchOut(Schema):
    documents: list[DocumentOut]
    entities: list[EntityRef]


# ------------------------------------------------------------------ extraction

PROVIDER_LABELS = {"heuristic": "Rule-based reader (development)", "anthropic": "Claude"}
SOURCE_LABELS = {
    "pdf_text": "Text layer of the PDF",
    "docx": "Text of the Word document",
    "ocr": "Text recognised from the scan (OCR)",
    "file": "The file itself, read by the AI provider",
}


class ExtractedFieldOut(Schema):
    value: str | None
    confidence: float
    evidence: str | None = None
    note: str | None = None
    raw: str | None = None


class ExtractionOut(Schema):
    id: uuid.UUID
    version_id: uuid.UUID
    status: Literal[
        "pending", "processing", "extraction_completed", "review_required", "failed", "verified", "dismissed"
    ]
    provider: str | None
    provider_label: str | None
    model: str | None
    text_source: str | None
    text_source_label: str | None
    fields: dict[str, ExtractedFieldOut]
    warnings: list[str]
    error: str | None
    attempts: int
    started_at: datetime | None
    completed_at: datetime | None
    reviewed_by_name: str | None
    reviewed_at: datetime | None
    applied_fields: list[str]

    @classmethod
    def build(cls, extraction: DocumentExtraction) -> "ExtractionOut":
        return cls(
            id=extraction.id,
            version_id=extraction.version_id,
            status=extraction.status,
            provider=extraction.provider,
            provider_label=PROVIDER_LABELS.get(extraction.provider or "", extraction.provider),
            model=extraction.model,
            text_source=extraction.text_source,
            text_source_label=SOURCE_LABELS.get(extraction.text_source or ""),
            fields={key: ExtractedFieldOut(**value) for key, value in (extraction.fields or {}).items()},
            warnings=list(extraction.warnings or []),
            error=extraction.error,
            attempts=extraction.attempts,
            started_at=extraction.started_at,
            completed_at=extraction.completed_at,
            reviewed_by_name=extraction.reviewed_by.full_name if extraction.reviewed_by else None,
            reviewed_at=extraction.reviewed_at,
            applied_fields=list(extraction.applied_fields or []),
        )


class ExtractionState(Schema):
    """Whether automatic reading is on, and the current file's extraction if there is one."""

    enabled: bool
    extraction: ExtractionOut | None


class ExtractionApply(_DocumentFields):
    """The details a person confirmed. Only fields sent are changed."""

    name: str | None = Field(default=None, max_length=255)
    category: str | None = None
    verify: bool = False

    def changes(self) -> dict[str, Any]:
        data = self.model_dump(exclude_unset=True, exclude={"verify", "notes", "owner_membership_id"})
        for required in ("name", "category"):
            if required in data and data[required] is None:
                del data[required]
        return data
