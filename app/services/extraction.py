"""Document intelligence: read an uploaded file and offer its details as suggestions.

Pipeline for one file version:

    pending -> processing -> extraction_completed | review_required | failed

then, after a person looks at the suggestions:

    -> verified (some or all applied) | dismissed (none applied)

The suggestions never change a document by themselves. Applying them is an
explicit action by a person with write permission, recorded in the audit log.
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.constants import DOCUMENT_CATEGORIES
from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError
from app.core.permissions import Permission
from app.core.rate_limit import enforce
from app.core.storage import StoredFileNotFoundError, get_storage
from app.core.tenancy import set_tenant
from app.core.time import utcnow
from app.integrations.ai import get_ai_provider
from app.integrations.ai.base import ProviderError, ProviderInput
from app.integrations.ocr import get_ocr
from app.models import Document, DocumentExtraction, DocumentVersion, Entity, ExtractionStatus, Organization
from app.repositories.base import TenantRepository
from app.services.audit import AuditService
from app.services.context import OrgContext
from app.services.documents import VERIFIED_FACTS, DocumentService
from app.services.expiry import organization_today
from app.services.extraction_rules import CheckedField, check_fields
from app.services.text_extraction import TextExtractionError, extract_text

logger = logging.getLogger(__name__)

STALE_AFTER = timedelta(minutes=10)
APPLICABLE_FIELDS = (
    "name",
    "category",
    "document_number",
    "issuing_authority",
    "holder_name",
    "issue_date",
    "expiry_date",
    "entity_id",
)


class ExtractionAction:
    APPLIED = "document.extraction_applied"
    DISMISSED = "document.extraction_dismissed"
    RETRIED = "document.extraction_retried"


class ExtractionRepository(TenantRepository[DocumentExtraction]):
    model = DocumentExtraction

    def for_version(self, version_id: uuid.UUID) -> DocumentExtraction | None:
        return self.db.scalars(self._select().where(DocumentExtraction.version_id == version_id)).first()

    def count_ready(self) -> int:
        """Active documents whose current file has suggestions waiting for a person."""
        from app.repositories.documents import suggestions_waiting

        return (
            self.db.scalar(
                select(func.count())
                .select_from(Document)
                .where(
                    Document.organization_id == self.organization_id,
                    Document.archived_at.is_(None),
                    suggestions_waiting(),
                )
            )
            or 0
        )


def extraction_enabled() -> bool:
    return get_ai_provider() is not None


def new_extraction(
    organization_id: uuid.UUID, document_id: uuid.UUID, version_id: uuid.UUID
) -> DocumentExtraction:
    return DocumentExtraction(
        id=uuid.uuid4(),  # known before flush, so it can be queued after commit
        organization_id=organization_id,
        document_id=document_id,
        version_id=version_id,
        status=ExtractionStatus.PENDING,
        fields={},
        warnings=[],
        applied_fields=[],
    )


def enqueue_extraction(extraction_id: uuid.UUID) -> None:
    """Hand an extraction to the worker after the transaction that created it has committed.

    A failure to queue is logged and the extraction stays pending; it can be
    started again from the document page.
    """
    from app.workers.document_tasks import extract_document_task

    try:
        extract_document_task.delay(str(extraction_id))
    except Exception:
        logger.exception("Could not queue document extraction", extra={"extraction_id": str(extraction_id)})


class ExtractionService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.settings = get_settings()
        self.audit = AuditService(db)

    # ------------------------------------------------------------- the pipeline

    def run(self, extraction_id: uuid.UUID) -> None:
        """Read the file and store suggestions. Safe to call twice: only a pending extraction is processed."""
        claimed = self.db.execute(
            update(DocumentExtraction)
            .where(
                DocumentExtraction.id == extraction_id, DocumentExtraction.status == ExtractionStatus.PENDING
            )
            .values(
                status=ExtractionStatus.PROCESSING,
                started_at=utcnow(),
                attempts=DocumentExtraction.attempts + 1,
                error=None,
            )
        ).rowcount
        self.db.commit()
        if not claimed:
            return

        extraction = self.db.get(DocumentExtraction, extraction_id)
        if extraction is None:  # pragma: no cover - deleted between claim and load
            return
        set_tenant(self.db, extraction.organization_id)
        try:
            self._process(extraction)
        except (ProviderError, TextExtractionError) as error:
            self._fail(extraction, str(getattr(error, "message", error)))
        except StoredFileNotFoundError:
            logger.error("Stored file missing for extraction", extra={"extraction_id": str(extraction_id)})
            self._fail(extraction, "The file could not be found in storage.")
        except Exception:
            logger.exception("Document extraction failed", extra={"extraction_id": str(extraction_id)})
            self._fail(extraction, "Something went wrong while reading this file. Try again.")

    def _process(self, extraction: DocumentExtraction) -> None:
        provider = get_ai_provider()
        if provider is None:
            raise ProviderError("Automatic reading is switched off.", retryable=False)

        version = self.db.get(DocumentVersion, extraction.version_id)
        organization = self.db.get(Organization, extraction.organization_id)
        data = b"".join(get_storage().open(version.storage_key))

        file_for_provider: bytes | None = None
        extracted = extract_text(data, version.content_type, ocr=None)
        if extracted is None:
            # A scan or a photo: let the provider read the file itself if it can, else try OCR.
            if provider.can_read_file(version.content_type, len(data)):
                file_for_provider, source, text = data, "file", None
            elif (ocr := get_ocr()) is not None:
                extracted = extract_text(data, version.content_type, ocr=ocr)
                source, text = extracted.source, extracted.text
            else:
                raise TextExtractionError(
                    "This looks like a scan or a photo, and no text could be read from it. "
                    "Enter the details by hand."
                )
        else:
            source, text = extracted.source, extracted.text

        if file_for_provider is None and not (text or "").strip():
            raise TextExtractionError("No text could be read from this file. Enter the details by hand.")

        categories = dict(DOCUMENT_CATEGORIES)
        result = provider.extract(
            ProviderInput(
                text=text,
                file=file_for_provider,
                content_type=version.content_type,
                filename=version.original_filename,
                categories=categories,
            )
        )
        outcome = check_fields(
            result.fields,
            categories=categories,
            today=organization_today(organization.timezone),
            threshold=self.settings.extraction_confidence_threshold,
        )
        fields = {key: item.to_dict() for key, item in outcome.fields.items()}
        if match := self._match_entity(extraction.organization_id, outcome.fields):
            fields["entity_id"] = match

        extraction.provider = result.provider
        extraction.model = result.model
        extraction.text_source = source
        extraction.fields = fields
        extraction.warnings = outcome.warnings
        extraction.status = (
            ExtractionStatus.REVIEW_REQUIRED
            if outcome.needs_review
            else ExtractionStatus.EXTRACTION_COMPLETED
        )
        extraction.completed_at = utcnow()
        self.db.commit()

    def _match_entity(
        self, organization_id: uuid.UUID, fields: dict[str, CheckedField]
    ) -> dict[str, Any] | None:
        """Suggest the record a document belongs to when a name on it matches a record exactly."""
        for key in ("organization_mentioned", "holder_name"):
            item = fields.get(key)
            if not item or not item.value:
                continue
            entity = self.db.scalars(
                select(Entity).where(
                    Entity.organization_id == organization_id,
                    Entity.archived_at.is_(None),
                    func.lower(Entity.name) == item.value.strip().lower(),
                )
            ).first()
            if entity:
                return {
                    "value": str(entity.id),
                    "confidence": item.confidence,
                    "evidence": item.evidence,
                    "note": f'The name "{item.value}" matches the record {entity.name}.',
                    "raw": entity.name,
                }
        return None

    def _fail(self, extraction: DocumentExtraction, message: str) -> None:
        self.db.rollback()
        extraction = self.db.get(DocumentExtraction, extraction.id)
        extraction.status = ExtractionStatus.FAILED
        extraction.error = message[:500]
        extraction.completed_at = utcnow()
        self.db.commit()

    # ----------------------------------------------------------- user actions

    def current(self, ctx: OrgContext, document_id: uuid.UUID) -> tuple[Document, DocumentExtraction | None]:
        document = DocumentService(self.db).get(ctx, document_id)
        version = document.current_version
        extraction = (
            ExtractionRepository(self.db, ctx.organization_id).for_version(version.id) if version else None
        )
        return document, extraction

    def retry(self, ctx: OrgContext, document_id: uuid.UUID) -> DocumentExtraction:
        if not extraction_enabled():
            raise ConflictError("Automatic reading is switched off.", code="extraction_disabled")
        document, extraction = self.current(ctx, document_id)
        if document.archived_at is not None:
            raise ConflictError("This document is archived. Restore it first.", code="document_archived")
        if document.current_version is None:
            raise ConflictError("This document has no file to read.", code="no_file")
        enforce(f"extraction-retry:{ctx.organization_id}", 30, 3600)

        now = utcnow()
        if extraction is None:
            extraction = new_extraction(ctx.organization_id, document.id, document.current_version.id)
            ExtractionRepository(self.db, ctx.organization_id).add(extraction)
        elif extraction.status in ExtractionStatus.BUSY and (
            (extraction.started_at or extraction.updated_at) > now - STALE_AFTER
        ):
            raise ConflictError("This file is being read now.", code="extraction_in_progress")
        else:
            extraction.status = ExtractionStatus.PENDING
            extraction.fields = {}
            extraction.warnings = []
            extraction.error = None
            extraction.started_at = None
            extraction.completed_at = None
            extraction.reviewed_by_id = None
            extraction.reviewed_at = None
            extraction.applied_fields = []
        self.db.flush()
        self.audit.record_for(
            ctx,
            action=ExtractionAction.RETRIED,
            resource_type="document",
            resource_id=document.id,
            metadata={"name": document.name},
        )
        self.db.commit()
        enqueue_extraction(extraction.id)
        self.db.expire_all()
        return ExtractionRepository(self.db, ctx.organization_id).get(extraction.id)

    def _ready(self, ctx: OrgContext, document_id: uuid.UUID) -> tuple[Document, DocumentExtraction]:
        document, extraction = self.current(ctx, document_id)
        if document.archived_at is not None:
            raise ConflictError("This document is archived. Restore it first.", code="document_archived")
        if extraction is None:
            raise NotFoundError("There are no suggestions for this document.")
        if extraction.status not in ExtractionStatus.READY:
            raise ConflictError("These suggestions are not waiting for review.", code="extraction_not_ready")
        return document, extraction

    def apply(
        self, ctx: OrgContext, document_id: uuid.UUID, values: dict[str, Any], *, verify: bool
    ) -> Document:
        """Save the details a person confirmed. Only fields sent are changed."""
        if verify and not ctx.has(Permission.DOCUMENTS_VERIFY):
            raise PermissionDeniedError("Your role does not allow verifying documents.")
        document, extraction = self._ready(ctx, document_id)
        documents = DocumentService(self.db)

        changes = {key: value for key, value in values.items() if key in APPLICABLE_FIELDS}
        changed = documents._apply(ctx, document, changes)
        now = utcnow()
        if verify:
            document.verification_status = "verified"
            document.verified_by = ctx.user
            document.verified_at = now
            document.rejection_reason = None
        elif VERIFIED_FACTS.intersection(changed):
            documents._reset_verification(document)

        extraction.status = ExtractionStatus.VERIFIED
        extraction.reviewed_by = ctx.user
        extraction.reviewed_at = now
        extraction.applied_fields = changed
        self.audit.record_for(
            ctx,
            action=ExtractionAction.APPLIED,
            resource_type="document",
            resource_id=document.id,
            metadata={"name": document.name, "fields": changed, "verified": verify},
        )
        if verify:
            self.audit.record_for(
                ctx,
                action="document.verified",
                resource_type="document",
                resource_id=document.id,
                metadata={"name": document.name},
            )
        self.db.commit()
        self.db.refresh(document)
        return document

    def dismiss(self, ctx: OrgContext, document_id: uuid.UUID) -> DocumentExtraction:
        document, extraction = self._ready(ctx, document_id)
        extraction.status = ExtractionStatus.DISMISSED
        extraction.reviewed_by = ctx.user
        extraction.reviewed_at = utcnow()
        extraction.applied_fields = []
        self.audit.record_for(
            ctx,
            action=ExtractionAction.DISMISSED,
            resource_type="document",
            resource_id=document.id,
            metadata={"name": document.name},
        )
        self.db.commit()
        return extraction

    def count_ready(self, ctx: OrgContext) -> int:
        return ExtractionRepository(self.db, ctx.organization_id).count_ready()
