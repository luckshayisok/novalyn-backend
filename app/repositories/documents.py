"""Repositories for documents, document versions and entities. All tenant-owned."""

# Methods named `list` shadow the builtin inside these classes; postponed
# evaluation keeps `list[...]` in later annotations meaning the builtin.
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import ColumnElement, Select, case, exists, func, or_, select

from app.models import Document, DocumentExtraction, DocumentVersion, Entity, ExtractionStatus
from app.repositories.base import TenantRepository
from app.services.expiry import DocumentStatus, expiring_window_days, status_condition


def _contains(column, text: str) -> ColumnElement[bool]:
    """Case-insensitive substring match with LIKE wildcards in the input escaped."""
    return func.lower(column).contains(text.lower(), autoescape=True)


def suggestions_waiting() -> ColumnElement[bool]:
    """The document's current file has suggested details that nobody has reviewed yet."""
    current_number = (
        select(func.max(DocumentVersion.version_number))
        .where(DocumentVersion.document_id == Document.id, DocumentVersion.state == "active")
        .correlate(Document)
        .scalar_subquery()
    )
    return exists(
        select(DocumentExtraction.id)
        .join(DocumentVersion, DocumentVersion.id == DocumentExtraction.version_id)
        .where(
            DocumentVersion.document_id == Document.id,
            DocumentVersion.version_number == current_number,
            DocumentExtraction.status.in_(ExtractionStatus.READY),
        )
        .correlate(Document)
    )


@dataclass(frozen=True)
class DocumentFilters:
    q: str | None = None
    category: str | None = None
    status: DocumentStatus | None = None
    verification: str | None = None
    entity_id: uuid.UUID | None = None
    owner_membership_id: uuid.UUID | None = None
    archived: bool = False
    suggestions: bool = False


DOCUMENT_SORTS = {
    "name": (func.lower(Document.name), False),
    "expiry_date": (Document.expiry_date, True),
    "created_at": (Document.created_at, False),
    "updated_at": (Document.updated_at, False),
}


@dataclass(frozen=True)
class EntityDocumentCounts:
    total: int = 0
    expiring_soon: int = 0
    expired: int = 0


class DocumentRepository(TenantRepository[Document]):
    model = Document

    def _filtered(self, filters: DocumentFilters, today: date) -> list[ColumnElement[bool]]:
        conditions: list[ColumnElement[bool]] = [Document.organization_id == self.organization_id]
        conditions.append(
            Document.archived_at.is_not(None) if filters.archived else Document.archived_at.is_(None)
        )
        if filters.q:
            conditions.append(
                or_(
                    _contains(Document.name, filters.q),
                    _contains(Document.document_number, filters.q),
                    _contains(Document.issuing_authority, filters.q),
                    _contains(Document.holder_name, filters.q),
                )
            )
        if filters.category:
            conditions.append(Document.category == filters.category)
        if filters.status and filters.status is not DocumentStatus.ARCHIVED:
            conditions.append(status_condition(filters.status, today))
        if filters.verification:
            conditions.append(Document.verification_status == filters.verification)
        if filters.entity_id:
            conditions.append(Document.entity_id == filters.entity_id)
        if filters.suggestions:
            conditions.append(suggestions_waiting())
        if filters.owner_membership_id:
            conditions.append(Document.owner_membership_id == filters.owner_membership_id)
        return conditions

    def list(
        self, filters: DocumentFilters, *, sort: str, page: int, page_size: int, today: date
    ) -> tuple[list[Document], int]:
        conditions = self._filtered(filters, today)
        total = self.db.scalar(select(func.count()).select_from(Document).where(*conditions)) or 0

        descending = sort.startswith("-")
        column, nulls_last = DOCUMENT_SORTS[sort.lstrip("-")]
        ordering = [column.desc() if descending else column.asc(), Document.id]
        if nulls_last:
            ordering.insert(0, column.is_(None))  # documents without a date always come last

        items = list(
            self.db.scalars(
                select(Document)
                .where(*conditions)
                .order_by(*ordering)
                .limit(page_size)
                .offset((page - 1) * page_size)
            )
        )
        return items, total

    def active(self) -> Select:
        return self._select().where(Document.archived_at.is_(None))

    def status_counts(self, today: date) -> dict[str, int]:
        limit = today + timedelta(days=expiring_window_days())
        expired = case((Document.expiry_date < today, 1), else_=0)
        expiring = case(((Document.expiry_date >= today) & (Document.expiry_date <= limit), 1), else_=0)
        pending = case((Document.verification_status == "pending", 1), else_=0)
        row = self.db.execute(
            select(
                func.count(Document.id),
                func.coalesce(func.sum(expired), 0),
                func.coalesce(func.sum(expiring), 0),
                func.coalesce(func.sum(pending), 0),
            ).where(Document.organization_id == self.organization_id, Document.archived_at.is_(None))
        ).one()
        total, expired_count, expiring_count, pending_count = (int(value) for value in row)
        return {
            "total": total,
            "expired": expired_count,
            "expiring_soon": expiring_count,
            "valid": total - expired_count - expiring_count,
            "pending_verification": pending_count,
        }

    def counts_by_category(self) -> list[tuple[str, int]]:
        rows = self.db.execute(
            select(Document.category, func.count(Document.id))
            .where(Document.organization_id == self.organization_id, Document.archived_at.is_(None))
            .group_by(Document.category)
        ).all()
        return [(category, int(count)) for category, count in rows]

    def counts_by_entity(self) -> dict[uuid.UUID, int]:
        rows = self.db.execute(
            select(Document.entity_id, func.count(Document.id))
            .where(
                Document.organization_id == self.organization_id,
                Document.archived_at.is_(None),
                Document.entity_id.is_not(None),
            )
            .group_by(Document.entity_id)
        ).all()
        return {entity_id: int(count) for entity_id, count in rows}

    def expiry_dates_between(self, start: date, end: date) -> list[date]:
        return list(
            self.db.scalars(
                select(Document.expiry_date).where(
                    Document.organization_id == self.organization_id,
                    Document.archived_at.is_(None),
                    Document.expiry_date >= start,
                    Document.expiry_date <= end,
                )
            )
        )

    def archived_count(self) -> int:
        return (
            self.db.scalar(
                select(func.count())
                .select_from(Document)
                .where(Document.organization_id == self.organization_id, Document.archived_at.is_not(None))
            )
            or 0
        )


class DocumentVersionRepository(TenantRepository[DocumentVersion]):
    model = DocumentVersion

    def get_for_document(self, document_id: uuid.UUID, version_id: uuid.UUID) -> DocumentVersion | None:
        return self.db.scalars(
            self._select().where(DocumentVersion.id == version_id, DocumentVersion.document_id == document_id)
        ).first()

    def next_version_number(self, document_id: uuid.UUID) -> int:
        current = self.db.scalar(
            select(func.max(DocumentVersion.version_number)).where(
                DocumentVersion.organization_id == self.organization_id,
                DocumentVersion.document_id == document_id,
            )
        )
        return (current or 0) + 1


class EntityRepository(TenantRepository[Entity]):
    model = Entity

    def _conditions(
        self, *, type_: str | None, q: str | None, archived: bool | None
    ) -> list[ColumnElement[bool]]:
        conditions: list[ColumnElement[bool]] = [Entity.organization_id == self.organization_id]
        if archived is not None:
            conditions.append(Entity.archived_at.is_not(None) if archived else Entity.archived_at.is_(None))
        if type_:
            conditions.append(Entity.type == type_)
        if q:
            conditions.append(
                or_(_contains(Entity.name, q), _contains(Entity.code, q), _contains(Entity.email, q))
            )
        return conditions

    def list(
        self, *, type_: str | None, q: str | None, archived: bool, page: int, page_size: int
    ) -> tuple[list[Entity], int]:
        conditions = self._conditions(type_=type_, q=q, archived=archived)
        total = self.db.scalar(select(func.count()).select_from(Entity).where(*conditions)) or 0
        items = list(
            self.db.scalars(
                select(Entity)
                .where(*conditions)
                .order_by(func.lower(Entity.name), Entity.id)
                .limit(page_size)
                .offset((page - 1) * page_size)
            )
        )
        return items, total

    def lookup(self, *, type_: str | None, q: str | None, limit: int) -> list[Entity]:
        conditions = self._conditions(type_=type_, q=q, archived=False)
        return list(
            self.db.scalars(select(Entity).where(*conditions).order_by(func.lower(Entity.name)).limit(limit))
        )

    def get_many(self, entity_ids: list[uuid.UUID]) -> list[Entity]:
        if not entity_ids:
            return []
        return list(self.db.scalars(self._select().where(Entity.id.in_(entity_ids))))

    def count_by_type(self) -> dict[str, int]:
        rows = self.db.execute(
            select(Entity.type, func.count(Entity.id))
            .where(Entity.organization_id == self.organization_id, Entity.archived_at.is_(None))
            .group_by(Entity.type)
        ).all()
        return {type_: int(count) for type_, count in rows}

    def document_counts(
        self, entity_ids: list[uuid.UUID], today: date
    ) -> dict[uuid.UUID, EntityDocumentCounts]:
        """Active document totals per entity, with how many are expiring soon or expired."""
        if not entity_ids:
            return {}
        limit = today + timedelta(days=expiring_window_days())
        expired = case((Document.expiry_date < today, 1), else_=0)
        expiring = case(((Document.expiry_date >= today) & (Document.expiry_date <= limit), 1), else_=0)
        rows = self.db.execute(
            select(
                Document.entity_id,
                func.count(Document.id),
                func.coalesce(func.sum(expiring), 0),
                func.coalesce(func.sum(expired), 0),
            )
            .where(
                Document.organization_id == self.organization_id,
                Document.archived_at.is_(None),
                Document.entity_id.in_(entity_ids),
            )
            .group_by(Document.entity_id)
        ).all()
        return {
            entity_id: EntityDocumentCounts(int(total), int(expiring_count), int(expired_count))
            for entity_id, total, expiring_count, expired_count in rows
        }
