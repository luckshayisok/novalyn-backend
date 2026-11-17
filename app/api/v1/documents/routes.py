"""Document, entity, dashboard, search and file endpoints."""

import uuid
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.deps import require_permission
from app.core.config import get_settings
from app.core.database import get_db
from app.core.files import validate_upload
from app.core.permissions import Permission
from app.core.rate_limit import rate_limit
from app.repositories.documents import DOCUMENT_SORTS, DocumentFilters
from app.schemas.common import Page
from app.schemas.documents import (
    DashboardOut,
    DocumentDetail,
    DocumentOut,
    DocumentUpdate,
    DocumentUploadForm,
    EntityCreate,
    EntityOut,
    EntityRef,
    EntityType,
    EntityTypeCount,
    EntityUpdate,
    ExtractionApply,
    ExtractionOut,
    ExtractionState,
    FileUrlOut,
    FileUrlRequest,
    RejectRequest,
    SearchOut,
    Verification,
    VersionUploadForm,
    entity_type_counts,
)
from app.services.context import OrgContext
from app.services.dashboard import DashboardService
from app.services.documents import DocumentService
from app.services.entities import EntityService
from app.services.expiry import DocumentStatus
from app.services.extraction import ExtractionService, extraction_enabled

ORG = "/organizations/{organization_id}"

documents_router = APIRouter(prefix=f"{ORG}/documents", tags=["documents"])
entities_router = APIRouter(prefix=f"{ORG}/entities", tags=["entities"])
insights_router = APIRouter(prefix=ORG, tags=["dashboard"])
files_router = APIRouter(prefix="/files", tags=["files"])

SortOption = Literal[
    "name", "-name", "expiry_date", "-expiry_date", "created_at", "-created_at", "updated_at", "-updated_at"
]
assert all(option.lstrip("-") in DOCUMENT_SORTS for option in SortOption.__args__)  # noqa: S101

StatusFilter = Literal["valid", "expiring_soon", "expired"]
CanRead = Depends(require_permission(Permission.DOCUMENTS_READ))
CanWrite = Depends(require_permission(Permission.DOCUMENTS_WRITE))
CanVerify = Depends(require_permission(Permission.DOCUMENTS_VERIFY))
CanArchive = Depends(require_permission(Permission.DOCUMENTS_DELETE))


def _validated(file: UploadFile):
    return validate_upload(file.file, file.filename, max_bytes=get_settings().max_upload_size_bytes)


# ------------------------------------------------------------------- documents


@documents_router.post("", response_model=DocumentDetail, status_code=status.HTTP_201_CREATED)
def upload_document(
    form: Annotated[DocumentUploadForm, Form()],
    file: UploadFile = File(...),
    ctx: OrgContext = CanWrite,
    db: Session = Depends(get_db),
) -> DocumentDetail:
    """Upload a file and create a document from it."""
    service = DocumentService(db)
    upload = _validated(file)
    try:
        document = service.upload(ctx, upload, form.data())
    finally:
        upload.close()
    return DocumentDetail.build(document, service.today(ctx))


@documents_router.get("", response_model=Page[DocumentOut])
def list_documents(
    q: str | None = Query(default=None, max_length=200),
    category: str | None = Query(default=None, max_length=50),
    status_filter: StatusFilter | None = Query(default=None, alias="status"),
    verification: Verification | None = None,
    entity_id: uuid.UUID | None = None,
    owner_membership_id: uuid.UUID | None = None,
    suggestions: bool = Query(default=False, description="Only documents with suggestions to review"),
    archived: bool = False,
    sort: SortOption = "expiry_date",
    page: int = Query(default=1, ge=1, le=10_000),
    page_size: int = Query(default=25, ge=1, le=100),
    ctx: OrgContext = CanRead,
    db: Session = Depends(get_db),
) -> Page[DocumentOut]:
    service = DocumentService(db)
    filters = DocumentFilters(
        q=q.strip() if q and q.strip() else None,
        category=category,
        status=DocumentStatus(status_filter) if status_filter else None,
        verification=verification,
        entity_id=entity_id,
        owner_membership_id=owner_membership_id,
        suggestions=suggestions,
        archived=archived,
    )
    items, total = service.list(ctx, filters, sort=sort, page=page, page_size=page_size)
    today = service.today(ctx)
    return Page[DocumentOut](
        items=[DocumentOut.build(item, today) for item in items], total=total, page=page, page_size=page_size
    )


@documents_router.get("/{document_id}", response_model=DocumentDetail)
def get_document(
    document_id: uuid.UUID, ctx: OrgContext = CanRead, db: Session = Depends(get_db)
) -> DocumentDetail:
    service = DocumentService(db)
    return DocumentDetail.build(service.get(ctx, document_id), service.today(ctx))


@documents_router.patch("/{document_id}", response_model=DocumentDetail)
def update_document(
    document_id: uuid.UUID,
    payload: DocumentUpdate,
    ctx: OrgContext = CanWrite,
    db: Session = Depends(get_db),
) -> DocumentDetail:
    service = DocumentService(db)
    return DocumentDetail.build(service.update(ctx, document_id, payload.changes()), service.today(ctx))


@documents_router.post(
    "/{document_id}/versions", response_model=DocumentDetail, status_code=status.HTTP_201_CREATED
)
def replace_document_file(
    document_id: uuid.UUID,
    form: Annotated[VersionUploadForm, Form()],
    file: UploadFile = File(...),
    ctx: OrgContext = CanWrite,
    db: Session = Depends(get_db),
) -> DocumentDetail:
    """Upload a new file for an existing document. The earlier file stays in the version history."""
    service = DocumentService(db)
    upload = _validated(file)
    try:
        document = service.add_version(ctx, document_id, upload, dates=form.dates(), note=form.note)
    finally:
        upload.close()
    return DocumentDetail.build(document, service.today(ctx))


@documents_router.post("/{document_id}/versions/{version_id}/file-url", response_model=FileUrlOut)
def create_file_url(
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    payload: FileUrlRequest,
    ctx: OrgContext = CanRead,
    db: Session = Depends(get_db),
) -> FileUrlOut:
    """A short-lived link to view (inline) or download (attachment) one file version."""
    url, expires_in = DocumentService(db).create_file_url(
        ctx, document_id, version_id, inline=payload.disposition == "inline"
    )
    return FileUrlOut(url=url, expires_in=expires_in)


@documents_router.post("/{document_id}/verify", response_model=DocumentDetail)
def verify_document(
    document_id: uuid.UUID, ctx: OrgContext = CanVerify, db: Session = Depends(get_db)
) -> DocumentDetail:
    service = DocumentService(db)
    return DocumentDetail.build(service.verify(ctx, document_id), service.today(ctx))


@documents_router.post("/{document_id}/reject", response_model=DocumentDetail)
def reject_document(
    document_id: uuid.UUID,
    payload: RejectRequest,
    ctx: OrgContext = CanVerify,
    db: Session = Depends(get_db),
) -> DocumentDetail:
    service = DocumentService(db)
    return DocumentDetail.build(service.reject(ctx, document_id, payload.reason), service.today(ctx))


@documents_router.post("/{document_id}/archive", response_model=DocumentDetail)
def archive_document(
    document_id: uuid.UUID, ctx: OrgContext = CanArchive, db: Session = Depends(get_db)
) -> DocumentDetail:
    service = DocumentService(db)
    return DocumentDetail.build(service.set_archived(ctx, document_id, True), service.today(ctx))


@documents_router.post("/{document_id}/restore", response_model=DocumentDetail)
def restore_document(
    document_id: uuid.UUID, ctx: OrgContext = CanArchive, db: Session = Depends(get_db)
) -> DocumentDetail:
    service = DocumentService(db)
    return DocumentDetail.build(service.set_archived(ctx, document_id, False), service.today(ctx))


# ------------------------------------------------------------------ extraction


def _state(extraction) -> ExtractionState:
    return ExtractionState(
        enabled=extraction_enabled(), extraction=ExtractionOut.build(extraction) if extraction else None
    )


@documents_router.get("/{document_id}/extraction", response_model=ExtractionState)
def get_extraction(
    document_id: uuid.UUID, ctx: OrgContext = CanRead, db: Session = Depends(get_db)
) -> ExtractionState:
    """Suggestions read from the current file, if any."""
    _, extraction = ExtractionService(db).current(ctx, document_id)
    return _state(extraction)


@documents_router.post("/{document_id}/extraction/retry", response_model=ExtractionState)
def retry_extraction(
    document_id: uuid.UUID, ctx: OrgContext = CanWrite, db: Session = Depends(get_db)
) -> ExtractionState:
    """Read the current file again."""
    return _state(ExtractionService(db).retry(ctx, document_id))


@documents_router.post("/{document_id}/extraction/apply", response_model=DocumentDetail)
def apply_extraction(
    document_id: uuid.UUID,
    payload: ExtractionApply,
    ctx: OrgContext = CanWrite,
    db: Session = Depends(get_db),
) -> DocumentDetail:
    """Save the details a person confirmed, optionally verifying the document in the same step."""
    document = ExtractionService(db).apply(ctx, document_id, payload.changes(), verify=payload.verify)
    return DocumentDetail.build(document, DocumentService(db).today(ctx))


@documents_router.post("/{document_id}/extraction/dismiss", response_model=ExtractionState)
def dismiss_extraction(
    document_id: uuid.UUID, ctx: OrgContext = CanWrite, db: Session = Depends(get_db)
) -> ExtractionState:
    """Close the suggestions without changing the document."""
    return _state(ExtractionService(db).dismiss(ctx, document_id))


# ----------------------------------------------------------------------- files


@files_router.get("/{token}", dependencies=[Depends(rate_limit("files", 300, 60))])
def get_file(token: str, db: Session = Depends(get_db)) -> StreamingResponse:
    """Serve a file for a link issued by `file-url`. The link is the credential and expires quickly."""
    download = DocumentService(db).open_file(token)
    disposition = "inline" if download.inline else "attachment"
    ascii_name = download.filename.encode("ascii", "replace").decode().replace('"', "'").replace("\\", "_")
    encoded_name = quote(download.filename)
    content_disposition = f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded_name}"
    headers = {
        "Content-Disposition": content_disposition,
        "Content-Length": str(download.size_bytes),
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, no-store",
        # Only this site may show the file in a frame (the document preview).
        "X-Frame-Options": "SAMEORIGIN",
        "Content-Security-Policy": "default-src 'none'; frame-ancestors 'self'",
    }
    return StreamingResponse(download.chunks, media_type=download.content_type, headers=headers)


# -------------------------------------------------------------------- entities


@entities_router.get("", response_model=Page[EntityOut])
def list_entities(
    type_: EntityType | None = Query(default=None, alias="type"),
    q: str | None = Query(default=None, max_length=200),
    archived: bool = False,
    page: int = Query(default=1, ge=1, le=10_000),
    page_size: int = Query(default=25, ge=1, le=100),
    ctx: OrgContext = Depends(require_permission(Permission.ENTITIES_READ)),
    db: Session = Depends(get_db),
) -> Page[EntityOut]:
    items, total, counts = EntityService(db).list(
        ctx,
        type_=type_,
        q=q.strip() if q and q.strip() else None,
        archived=archived,
        page=page,
        page_size=page_size,
    )
    return Page[EntityOut](
        items=[EntityOut.build(item, counts.get(item.id)) for item in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@entities_router.get("/lookup", response_model=list[EntityRef])
def lookup_entities(
    type_: EntityType | None = Query(default=None, alias="type"),
    q: str | None = Query(default=None, max_length=200),
    ctx: OrgContext = Depends(require_permission(Permission.ENTITIES_READ)),
    db: Session = Depends(get_db),
) -> list[EntityRef]:
    """Names for pickers: up to 50 active records, optionally of one type."""
    return [EntityRef.build(item) for item in EntityService(db).lookup(ctx, type_=type_, q=q or None)]


@entities_router.get("/counts", response_model=list[EntityTypeCount])
def count_entities(
    ctx: OrgContext = Depends(require_permission(Permission.ENTITIES_READ)),
    db: Session = Depends(get_db),
) -> list[EntityTypeCount]:
    return entity_type_counts(EntityService(db).count_by_type(ctx))


@entities_router.post("", response_model=EntityOut, status_code=status.HTTP_201_CREATED)
def create_entity(
    payload: EntityCreate,
    ctx: OrgContext = Depends(require_permission(Permission.ENTITIES_WRITE)),
    db: Session = Depends(get_db),
) -> EntityOut:
    return EntityOut.build(EntityService(db).create(ctx, payload.model_dump(exclude_unset=True)))


@entities_router.get("/{entity_id}", response_model=EntityOut)
def get_entity(
    entity_id: uuid.UUID,
    ctx: OrgContext = Depends(require_permission(Permission.ENTITIES_READ)),
    db: Session = Depends(get_db),
) -> EntityOut:
    entity, counts = EntityService(db).get(ctx, entity_id)
    return EntityOut.build(entity, counts)


@entities_router.patch("/{entity_id}", response_model=EntityOut)
def update_entity(
    entity_id: uuid.UUID,
    payload: EntityUpdate,
    ctx: OrgContext = Depends(require_permission(Permission.ENTITIES_WRITE)),
    db: Session = Depends(get_db),
) -> EntityOut:
    service = EntityService(db)
    service.update(ctx, entity_id, payload.changes())
    entity, counts = service.get(ctx, entity_id)
    return EntityOut.build(entity, counts)


@entities_router.post("/{entity_id}/archive", response_model=EntityOut)
def archive_entity(
    entity_id: uuid.UUID,
    ctx: OrgContext = Depends(require_permission(Permission.ENTITIES_WRITE)),
    db: Session = Depends(get_db),
) -> EntityOut:
    service = EntityService(db)
    service.set_archived(ctx, entity_id, True)
    entity, counts = service.get(ctx, entity_id)
    return EntityOut.build(entity, counts)


@entities_router.post("/{entity_id}/restore", response_model=EntityOut)
def restore_entity(
    entity_id: uuid.UUID,
    ctx: OrgContext = Depends(require_permission(Permission.ENTITIES_WRITE)),
    db: Session = Depends(get_db),
) -> EntityOut:
    service = EntityService(db)
    service.set_archived(ctx, entity_id, False)
    entity, counts = service.get(ctx, entity_id)
    return EntityOut.build(entity, counts)


# -------------------------------------------------------- dashboard and search


@insights_router.get("/dashboard", response_model=DashboardOut)
def get_dashboard(ctx: OrgContext = CanRead, db: Session = Depends(get_db)) -> DashboardOut:
    return DashboardOut.build(DashboardService(db).build(ctx))


@insights_router.get("/search", response_model=SearchOut)
def search(
    q: str = Query(min_length=2, max_length=100),
    ctx: OrgContext = CanRead,
    db: Session = Depends(get_db),
) -> SearchOut:
    """Documents and records whose name, number or other key text contains the query."""
    service = DashboardService(db)
    documents, entities = service.search(ctx, q.strip())
    today = DocumentService(db).today(ctx)
    return SearchOut(
        documents=[DocumentOut.build(item, today) for item in documents],
        entities=[EntityRef.build(item) for item in entities] if ctx.has(Permission.ENTITIES_READ) else [],
    )
