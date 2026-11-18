"""Documents: upload, details, replacement, verification, archiving and secure download."""

# Methods named `list` shadow the builtin inside these classes; postponed
# evaluation keeps `list[...]` in later annotations meaning the builtin.
from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.constants import DOCUMENT_CATEGORIES
from app.core.errors import ConflictError, NotFoundError
from app.core.files import INLINE_CONTENT_TYPES, ValidatedUpload
from app.core.permissions import Permission
from app.core.plans import Feature
from app.core.security import create_file_token, decode_file_token
from app.core.storage import StoredFileNotFoundError, document_key, get_storage
from app.core.tenancy import set_tenant
from app.core.time import utcnow
from app.models import Document, DocumentVersion, Entity, VerificationStatus
from app.repositories.documents import (
    DocumentFilters,
    DocumentRepository,
    DocumentVersionRepository,
    EntityRepository,
)
from app.services.audit import AuditAction, AuditService
from app.services.context import OrgContext
from app.services.entities import field_error, resolve_owner
from app.services.expiry import organization_today
from app.services.notifications import EventType, document_payload, emit, kick_dispatcher
from app.services.plans import Entitlements

logger = logging.getLogger(__name__)

_TEXT_FIELDS = ("name", "document_number", "issuing_authority", "holder_name", "notes")
# Changing any of these means a verified document has to be looked at again.
VERIFIED_FACTS = frozenset(
    {"category", "document_number", "issuing_authority", "holder_name", "issue_date", "expiry_date"}
)


@dataclass(frozen=True)
class FileDownload:
    filename: str
    content_type: str
    size_bytes: int
    inline: bool
    chunks: Iterator[bytes]


class DocumentService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.settings = get_settings()
        self.audit = AuditService(db)

    # --------------------------------------------------------------------- read

    def today(self, ctx: OrgContext) -> date:
        return organization_today(ctx.organization.timezone)

    def list(
        self, ctx: OrgContext, filters: DocumentFilters, *, sort: str, page: int, page_size: int
    ) -> tuple[list[Document], int]:
        return DocumentRepository(self.db, ctx.organization_id).list(
            filters, sort=sort, page=page, page_size=page_size, today=self.today(ctx)
        )

    def get(self, ctx: OrgContext, document_id: uuid.UUID) -> Document:
        document = DocumentRepository(self.db, ctx.organization_id).get(document_id)
        if document is None:
            raise NotFoundError("This document was not found.")
        return document

    # ------------------------------------------------------------------- upload

    def upload(self, ctx: OrgContext, upload: ValidatedUpload, data: dict[str, Any]) -> Document:
        """Create a document from an uploaded file. The file becomes version 1."""
        Entitlements(self.db, ctx.organization_id).check_documents(adding=1)
        name = (data.get("name") or "").strip() or PurePosixPath(upload.filename).stem
        document = Document(
            organization_id=ctx.organization_id,
            name=name[:255],
            category=data["category"],
            created_by_id=ctx.user.id,
        )
        self._apply(ctx, document, {key: value for key, value in data.items() if key != "name"})
        DocumentRepository(self.db, ctx.organization_id).add(document)
        self.db.flush()

        version = self.store_version(ctx, document, upload, version_number=1, note=None)
        self.audit.record_for(
            ctx,
            action=AuditAction.DOCUMENT_UPLOADED,
            resource_type="document",
            resource_id=document.id,
            metadata={
                "name": document.name,
                "category": document.category,
                "filename": version.original_filename,
            },
        )
        self._ask_for_review(ctx, document)
        extraction_id = self.queue_extraction(ctx, document, version)
        self.commit_or_remove_file(version.storage_key)
        self.start_extraction(extraction_id)
        kick_dispatcher()
        self.db.refresh(document)
        return document

    def add_version(
        self,
        ctx: OrgContext,
        document_id: uuid.UUID,
        upload: ValidatedUpload,
        *,
        dates: dict[str, date | None],
        note: str | None,
    ) -> Document:
        """Replace the document's file. Earlier versions stay in the history."""
        document = self._get_editable(ctx, document_id)
        from app.services.renewals import RenewalRepository

        if RenewalRepository(self.db, ctx.organization_id).open_for_document(document.id) is not None:
            raise ConflictError(
                "A renewal is in progress for this document. Upload the renewed file to the renewal instead.",
                code="renewal_in_progress",
            )
        self._apply(ctx, document, dates)
        number = DocumentVersionRepository(self.db, ctx.organization_id).next_version_number(document.id)
        version = self.store_version(ctx, document, upload, version_number=number, note=note)

        # A new file has not been checked by anyone yet.
        self._reset_verification(document)
        document.updated_at = utcnow()
        self.audit.record_for(
            ctx,
            action=AuditAction.DOCUMENT_VERSION_ADDED,
            resource_type="document",
            resource_id=document.id,
            metadata={"name": document.name, "version": number, "filename": version.original_filename},
        )
        extraction_id = self.queue_extraction(ctx, document, version)
        self.commit_or_remove_file(version.storage_key)
        self.start_extraction(extraction_id)
        self.db.refresh(document)
        return document

    def _ask_for_review(self, ctx: OrgContext, document: Document) -> None:
        """Tell reviewers, in the app only, that a new document is waiting to be verified."""
        if not ctx.has(Permission.DOCUMENTS_VERIFY):
            emit(
                self.db,
                ctx.organization_id,
                EventType.VERIFICATION_REQUIRED,
                document_payload(document, actor=ctx.user),
            )

    def queue_extraction(
        self, ctx: OrgContext, document: Document, version: DocumentVersion
    ) -> uuid.UUID | None:
        """Create a pending extraction in the same transaction as the new file, if reading is on."""
        from app.services.extraction import ExtractionRepository, extraction_enabled, new_extraction

        if not extraction_enabled():
            return None
        extraction = new_extraction(ctx.organization_id, document.id, version.id)
        ExtractionRepository(self.db, ctx.organization_id).add(extraction)
        return extraction.id

    def start_extraction(self, extraction_id: uuid.UUID | None) -> None:
        """Queue the extraction after commit. In development without a worker it runs here and now."""
        if extraction_id is None:
            return
        from app.services.extraction import enqueue_extraction

        enqueue_extraction(extraction_id)
        self.db.expire_all()

    # ------------------------------------------------------------------- change

    def update(self, ctx: OrgContext, document_id: uuid.UUID, changes: dict[str, Any]) -> Document:
        document = self._get_editable(ctx, document_id)
        changed = self._apply(ctx, document, changes)
        if not changed:
            return document
        if VERIFIED_FACTS.intersection(changed):
            self._reset_verification(document)
        self.audit.record_for(
            ctx,
            action=AuditAction.DOCUMENT_UPDATED,
            resource_type="document",
            resource_id=document.id,
            metadata={"name": document.name, "fields": changed},
        )
        self.db.commit()
        return document

    def verify(self, ctx: OrgContext, document_id: uuid.UUID) -> Document:
        document = self._get_editable(ctx, document_id)
        if document.verification_status == VerificationStatus.VERIFIED:
            return document
        document.verification_status = VerificationStatus.VERIFIED
        document.verified_by = ctx.user
        document.verified_at = utcnow()
        document.rejection_reason = None
        self.audit.record_for(
            ctx,
            action=AuditAction.DOCUMENT_VERIFIED,
            resource_type="document",
            resource_id=document.id,
            metadata={"name": document.name},
        )
        self.db.commit()
        return document

    def reject(self, ctx: OrgContext, document_id: uuid.UUID, reason: str) -> Document:
        document = self._get_editable(ctx, document_id)
        document.verification_status = VerificationStatus.REJECTED
        document.verified_by = ctx.user
        document.verified_at = utcnow()
        document.rejection_reason = reason.strip()
        self.audit.record_for(
            ctx,
            action=AuditAction.DOCUMENT_REJECTED,
            resource_type="document",
            resource_id=document.id,
            metadata={"name": document.name, "reason": document.rejection_reason},
        )
        current = document.current_version
        uploader = current.uploaded_by_id if current and current.uploaded_by_id else document.created_by_id
        emit(
            self.db,
            ctx.organization_id,
            EventType.DOCUMENT_REJECTED,
            document_payload(
                document,
                actor=ctx.user,
                reason=document.rejection_reason,
                uploaded_by_id=str(uploader) if uploader else None,
            ),
        )
        self.db.commit()
        kick_dispatcher()
        return document

    def set_archived(self, ctx: OrgContext, document_id: uuid.UUID, archived: bool) -> Document:
        document = self.get(ctx, document_id)
        if (document.archived_at is not None) == archived:
            return document
        if not archived:
            Entitlements(self.db, ctx.organization_id).check_documents(adding=1)
        document.archived_at = utcnow() if archived else None
        self.audit.record_for(
            ctx,
            action=AuditAction.DOCUMENT_ARCHIVED if archived else AuditAction.DOCUMENT_RESTORED,
            resource_type="document",
            resource_id=document.id,
            metadata={"name": document.name},
        )
        self.db.commit()
        return document

    # ----------------------------------------------------------------- download

    def create_file_url(
        self, ctx: OrgContext, document_id: uuid.UUID, version_id: uuid.UUID, *, inline: bool
    ) -> tuple[str, int]:
        """A short-lived link to one file version. Issued only after the permission check on the route."""
        document = self.get(ctx, document_id)
        version = DocumentVersionRepository(self.db, ctx.organization_id).get_for_document(
            document.id, version_id
        )
        if version is None:
            raise NotFoundError("This file was not found.")
        current = document.current_version
        if version.state != "pending" and (current is None or version.id != current.id):
            # Earlier files, and files of rejected renewals, are the document's history.
            Entitlements(self.db, ctx.organization_id).require(Feature.DOCUMENT_HISTORY)

        inline = inline and version.content_type in INLINE_CONTENT_TYPES
        token, ttl = create_file_token(ctx.organization_id, version.id, inline=inline)
        if not inline:
            self.audit.record_for(
                ctx,
                action=AuditAction.DOCUMENT_DOWNLOADED,
                resource_type="document",
                resource_id=document.id,
                metadata={"name": document.name, "version": version.version_number},
            )
            self.db.commit()
        return f"{self.settings.api_v1_prefix}/files/{token}", ttl

    def open_file(self, token: str) -> FileDownload:
        """Resolve a file link. The token is the credential; it names one version in one organization."""
        expired = NotFoundError("This link has expired. Open the document again.", code="file_link_invalid")
        claims = decode_file_token(token)
        if claims is None:
            raise expired

        set_tenant(self.db, claims.organization_id)
        version = DocumentVersionRepository(self.db, claims.organization_id).get(claims.version_id)
        if version is None:
            raise expired
        try:
            chunks = get_storage().open(version.storage_key)
        except StoredFileNotFoundError as error:
            logger.error("Stored file is missing", extra={"version_id": str(version.id)})
            raise NotFoundError("This file is not available.", code="file_missing") from error
        return FileDownload(
            filename=version.original_filename,
            content_type=version.content_type,
            size_bytes=version.size_bytes,
            inline=claims.inline and version.content_type in INLINE_CONTENT_TYPES,
            chunks=chunks,
        )

    # ------------------------------------------------------------------ helpers

    def _get_editable(self, ctx: OrgContext, document_id: uuid.UUID) -> Document:
        document = self.get(ctx, document_id)
        if document.archived_at is not None:
            raise ConflictError(
                "This document is archived. Restore it to make changes.", code="document_archived"
            )
        return document

    def _reset_verification(self, document: Document) -> None:
        if document.verification_status != VerificationStatus.PENDING:
            document.verification_status = VerificationStatus.PENDING
            document.verified_by = None
            document.verified_at = None
            document.rejection_reason = None

    def _resolve_entity(self, ctx: OrgContext, entity_id: uuid.UUID | None) -> Entity | None:
        if entity_id is None:
            return None
        entity = EntityRepository(self.db, ctx.organization_id).get(entity_id)
        if entity is None or entity.archived_at is not None:
            raise field_error("entity_id", "Choose an existing record.")
        return entity

    def _apply(self, ctx: OrgContext, document: Document, changes: dict[str, Any]) -> list[str]:
        """Validate and set fields. Returns the names of the fields that changed."""
        changed: list[str] = []

        def assign(field: str, value: Any) -> None:
            if getattr(document, field) != value:
                setattr(document, field, value)
                changed.append(field)

        for field in _TEXT_FIELDS:
            if field in changes:
                value = changes[field]
                value = value.strip() or None if isinstance(value, str) else None
                if field == "name" and not value:
                    raise field_error("name", "Enter a name.")
                assign(field, value)

        if "category" in changes:
            if changes["category"] not in DOCUMENT_CATEGORIES:
                raise field_error("category", "Choose a document type.")
            assign("category", changes["category"])

        for field in ("issue_date", "expiry_date"):
            if field in changes:
                assign(field, changes[field])
        if document.issue_date and document.expiry_date and document.expiry_date < document.issue_date:
            raise field_error("expiry_date", "The expiry date cannot be before the issue date.")

        # Links are set through the relationship so the loaded object and the foreign key stay in step.
        if "entity_id" in changes:
            entity = self._resolve_entity(ctx, changes["entity_id"])
            if document.entity_id != (entity.id if entity else None):
                document.entity = entity
                changed.append("entity_id")
        if "owner_membership_id" in changes:
            owner = resolve_owner(self.db, ctx, changes["owner_membership_id"])
            if document.owner_membership_id != (owner.id if owner else None):
                document.owner = owner
                changed.append("owner_membership_id")
        return changed

    def store_version(
        self,
        ctx: OrgContext,
        document: Document,
        upload: ValidatedUpload,
        *,
        version_number: int,
        note: str | None,
        state: str = "active",
    ) -> DocumentVersion:
        version_id = uuid.uuid4()
        key = document_key(ctx.organization_id, document.id, version_id, upload.extension)
        get_storage().save(key, upload.file, upload.content_type)
        version = DocumentVersion(
            id=version_id,
            organization_id=ctx.organization_id,
            document_id=document.id,
            version_number=version_number,
            state=state,
            storage_key=key,
            original_filename=upload.filename,
            content_type=upload.content_type,
            size_bytes=upload.size_bytes,
            sha256=upload.sha256,
            note=(note or "").strip()[:500] or None,
            uploaded_by_id=ctx.user.id,
        )
        DocumentVersionRepository(self.db, ctx.organization_id).add(version)
        return version

    def commit_or_remove_file(self, storage_key: str) -> None:
        """Commit; if the database rejects it, remove the file that was just stored so nothing is orphaned."""
        try:
            self.db.commit()
        except Exception:
            self.db.rollback()
            try:
                get_storage().delete(storage_key)
            except Exception:
                logger.exception(
                    "Could not remove stored file after a failed commit", extra={"key": storage_key}
                )
            raise
