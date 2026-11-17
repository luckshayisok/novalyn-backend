"""Renewal workflow.

    not_started -> requested -> in_progress -> submitted -> awaiting_approval -> completed
                                                    \\                    \\
                                                     `-> rejected <------ '   (reopen -> in_progress)

* Managers (owners, admins, compliance managers) create renewals, assign
  them, approve and reject them.
* The assignee starts the work, uploads the renewed file and sends it for
  approval. Managers can do these steps too.
* The renewed file is stored as a *pending* version. It becomes the current
  file only when the renewal is approved; a rejected file stays in the
  history marked as rejected. Nothing is deleted.
* Approval also sets the document's new dates and verifies it.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationFailedError
from app.core.files import ValidatedUpload
from app.core.permissions import Permission
from app.core.time import utcnow
from app.models import Document, DocumentVersion, Renewal, RenewalEvent, RenewalStatus, VersionState
from app.repositories.base import TenantRepository
from app.repositories.documents import DocumentVersionRepository
from app.services.audit import AuditService
from app.services.context import OrgContext
from app.services.documents import DocumentService
from app.services.entities import field_error, resolve_owner
from app.services.expiry import organization_today
from app.services.notifications import EventType, emit, kick_dispatcher, renewal_payload

S = RenewalStatus


class RenewalAction:
    CREATED = "created"
    REQUESTED = "requested"
    STARTED = "started"
    FILE_UPLOADED = "file_uploaded"
    SUBMITTED = "submitted"
    APPROVED = "approved"
    REJECTED = "rejected"
    REOPENED = "reopened"
    UPDATED = "updated"


AUDIT_PREFIX = "renewal."


class RenewalRepository(TenantRepository[Renewal]):
    model = Renewal

    def open_for_document(self, document_id: uuid.UUID) -> Renewal | None:
        return self.db.scalars(
            self._select().where(Renewal.document_id == document_id, Renewal.status.in_(S.OPEN))
        ).first()

    def list(
        self,
        *,
        statuses: tuple[str, ...] | None,
        assignee_id: uuid.UUID | None,
        page: int,
        page_size: int,
        deadline_before: date | None = None,
    ) -> tuple[list[Renewal], int]:
        conditions = [Renewal.organization_id == self.organization_id]
        if deadline_before:
            conditions.append(Renewal.deadline < deadline_before)
        if statuses:
            conditions.append(Renewal.status.in_(statuses))
        if assignee_id:
            conditions.append(Renewal.assignee_membership_id == assignee_id)
        total = self.db.scalar(select(func.count()).select_from(Renewal).where(*conditions)) or 0
        items = list(
            self.db.scalars(
                select(Renewal)
                .where(*conditions)
                .order_by(Renewal.deadline.is_(None), Renewal.deadline, Renewal.created_at.desc())
                .limit(page_size)
                .offset((page - 1) * page_size)
            )
        )
        return items, total

    def counts(self, today: date) -> dict[str, int]:
        open_count = (
            self.db.scalar(
                select(func.count())
                .select_from(Renewal)
                .where(Renewal.organization_id == self.organization_id, Renewal.status.in_(S.OPEN))
            )
            or 0
        )
        overdue = (
            self.db.scalar(
                select(func.count())
                .select_from(Renewal)
                .where(
                    Renewal.organization_id == self.organization_id,
                    Renewal.status.in_(S.OPEN),
                    Renewal.deadline < today,
                )
            )
            or 0
        )
        awaiting = (
            self.db.scalar(
                select(func.count())
                .select_from(Renewal)
                .where(Renewal.organization_id == self.organization_id, Renewal.status == S.AWAITING_APPROVAL)
            )
            or 0
        )
        return {"open": open_count, "overdue": overdue, "awaiting_approval": awaiting}


def renewal_permissions(ctx: OrgContext, renewal: Renewal) -> dict[str, bool]:
    """What the caller may do with this renewal right now. The service enforces the same rules."""
    manager = ctx.has(Permission.RENEWALS_MANAGE)
    worker = manager or (
        ctx.has(Permission.DOCUMENTS_WRITE) and renewal.assignee_membership_id == ctx.membership.id
    )
    status = renewal.status
    archived = renewal.document.archived_at is not None
    return {
        "edit": manager and renewal.is_open,
        "request": manager and status in (S.NOT_STARTED, S.IN_PROGRESS),
        "start": worker and status in (S.NOT_STARTED, S.REQUESTED),
        "upload": worker
        and not archived
        and status in (S.NOT_STARTED, S.REQUESTED, S.IN_PROGRESS, S.SUBMITTED),
        "submit": worker and status == S.SUBMITTED,
        "approve": manager
        and ctx.has(Permission.DOCUMENTS_VERIFY)
        and not archived
        and status in (S.SUBMITTED, S.AWAITING_APPROVAL),
        "reject": manager and status in (S.SUBMITTED, S.AWAITING_APPROVAL),
        "reopen": manager and status == S.REJECTED,
    }


class RenewalService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.audit = AuditService(db)
        self.documents = DocumentService(db)

    # ------------------------------------------------------------------- read

    def repo(self, ctx: OrgContext) -> RenewalRepository:
        return RenewalRepository(self.db, ctx.organization_id)

    def get(self, ctx: OrgContext, renewal_id: uuid.UUID) -> Renewal:
        renewal = self.repo(ctx).get(renewal_id)
        if renewal is None:
            raise NotFoundError("This renewal was not found.")
        return renewal

    def today(self, ctx: OrgContext) -> date:
        return organization_today(ctx.organization.timezone)

    # ------------------------------------------------------------------ create

    def create(
        self,
        ctx: OrgContext,
        *,
        document_id: uuid.UUID,
        assignee_membership_id: uuid.UUID | None,
        deadline: date | None,
        notes: str | None,
        request: bool,
    ) -> Renewal:
        ctx.require(Permission.RENEWALS_MANAGE)
        document = self.documents.get(ctx, document_id)
        if document.archived_at is not None:
            raise ConflictError("This document is archived. Restore it first.", code="document_archived")
        if self.repo(ctx).open_for_document(document.id) is not None:
            raise ConflictError("This document already has a renewal in progress.", code="renewal_exists")
        assignee = self._check_assignee(ctx, assignee_membership_id) if assignee_membership_id else None
        if request and assignee is None:
            raise field_error("assignee_membership_id", "Choose who should renew this document.")

        renewal = Renewal(
            id=uuid.uuid4(),
            organization_id=ctx.organization_id,
            document_id=document.id,
            status=S.NOT_STARTED,
            assignee_membership_id=assignee.id if assignee else None,
            deadline=deadline or document.expiry_date or self.today(ctx) + timedelta(days=30),
            notes=(notes or "").strip() or None,
            previous_version_id=document.current_version.id if document.current_version else None,
            previous_expiry_date=document.expiry_date,
            created_by_id=ctx.user.id,
        )
        self.repo(ctx).add(renewal)
        renewal.document = document
        renewal.assignee = assignee
        self._event(ctx, renewal, RenewalAction.CREATED, None, S.NOT_STARTED, renewal.notes)
        if request:
            self._move(ctx, renewal, S.REQUESTED, RenewalAction.REQUESTED, None)
            emit(
                self.db,
                ctx.organization_id,
                EventType.RENEWAL_REQUESTED,
                renewal_payload(renewal, actor=ctx.user),
            )
        self._audit(ctx, renewal, "renewal.created", {"assignee": assignee.user.email if assignee else None})
        try:
            self.db.commit()
        except IntegrityError as error:  # two people started a renewal at the same moment
            self.db.rollback()
            raise ConflictError(
                "This document already has a renewal in progress.", code="renewal_exists"
            ) from error
        if request:
            kick_dispatcher()
        return renewal

    def update(self, ctx: OrgContext, renewal_id: uuid.UUID, changes: dict[str, Any]) -> Renewal:
        renewal = self.get(ctx, renewal_id)
        self._allow(ctx, renewal, "edit")
        changed: list[str] = []
        notify = False
        if "assignee_membership_id" in changes:
            value = changes["assignee_membership_id"]
            assignee = self._check_assignee(ctx, value) if value else None
            new_id = assignee.id if assignee else None
            if renewal.assignee_membership_id != new_id:
                if new_id is None and renewal.status == S.REQUESTED:
                    raise field_error("assignee_membership_id", "A requested renewal needs someone assigned.")
                renewal.assignee = assignee
                changed.append("assignee")
                notify = new_id is not None and renewal.status in (S.REQUESTED, S.IN_PROGRESS)
        if "deadline" in changes and changes["deadline"] and changes["deadline"] != renewal.deadline:
            renewal.deadline = changes["deadline"]
            changed.append("deadline")
        if "notes" in changes:
            notes = (changes["notes"] or "").strip() or None
            if notes != renewal.notes:
                renewal.notes = notes
                changed.append("notes")
        if not changed:
            return renewal
        self._event(ctx, renewal, RenewalAction.UPDATED, renewal.status, renewal.status, ", ".join(changed))
        if notify:
            emit(
                self.db,
                ctx.organization_id,
                EventType.RENEWAL_REQUESTED,
                renewal_payload(renewal, actor=ctx.user),
            )
        self._audit(ctx, renewal, "renewal.updated", {"fields": changed})
        self.db.commit()
        if notify:
            kick_dispatcher()
        return renewal

    # ------------------------------------------------------------- transitions

    def request(self, ctx: OrgContext, renewal_id: uuid.UUID, note: str | None) -> Renewal:
        renewal = self.get(ctx, renewal_id)
        self._allow(ctx, renewal, "request")
        if renewal.assignee_membership_id is None:
            raise ConflictError(
                "Assign someone to the renewal before requesting it.", code="renewal_unassigned"
            )
        self._move(ctx, renewal, S.REQUESTED, RenewalAction.REQUESTED, note)
        emit(
            self.db,
            ctx.organization_id,
            EventType.RENEWAL_REQUESTED,
            renewal_payload(renewal, actor=ctx.user),
        )
        self._audit(ctx, renewal, "renewal.requested", {})
        self.db.commit()
        kick_dispatcher()
        return renewal

    def start(self, ctx: OrgContext, renewal_id: uuid.UUID) -> Renewal:
        renewal = self.get(ctx, renewal_id)
        self._allow(ctx, renewal, "start")
        self._move(ctx, renewal, S.IN_PROGRESS, RenewalAction.STARTED, None)
        self.db.commit()
        return renewal

    def upload(
        self,
        ctx: OrgContext,
        renewal_id: uuid.UUID,
        upload: ValidatedUpload,
        *,
        issue_date: date | None,
        expiry_date: date | None,
        note: str | None,
    ) -> Renewal:
        """Attach the renewed file. It stays pending until the renewal is approved."""
        renewal = self.get(ctx, renewal_id)
        self._allow(ctx, renewal, "upload")
        if issue_date and expiry_date and expiry_date < issue_date:
            raise field_error("expiry_date", "The expiry date cannot be before the issue date.")
        if expiry_date and renewal.previous_expiry_date and expiry_date <= renewal.previous_expiry_date:
            raise field_error("expiry_date", "The new expiry date should be later than the current one.")

        document = renewal.document
        if renewal.new_version is not None and renewal.new_version.state == VersionState.PENDING:
            # A replaced upload within the same renewal is kept, marked as not used.
            renewal.new_version.state = VersionState.REJECTED
        number = DocumentVersionRepository(self.db, ctx.organization_id).next_version_number(document.id)
        version = self.documents.store_version(
            ctx, document, upload, version_number=number, note=note, state=VersionState.PENDING
        )
        renewal.new_version = version
        if issue_date:
            renewal.proposed_issue_date = issue_date
        if expiry_date:
            renewal.proposed_expiry_date = expiry_date
        renewal.rejection_reason = None
        self._move(
            ctx, renewal, S.SUBMITTED, RenewalAction.FILE_UPLOADED, version.original_filename, force=True
        )
        extraction_id = self.documents.queue_extraction(ctx, document, version)
        self._audit(
            ctx, renewal, "renewal.file_uploaded", {"filename": version.original_filename, "version": number}
        )
        self.documents.commit_or_remove_file(version.storage_key)
        self.documents.start_extraction(extraction_id)
        return self.get(ctx, renewal_id)

    def submit(self, ctx: OrgContext, renewal_id: uuid.UUID, note: str | None) -> Renewal:
        renewal = self.get(ctx, renewal_id)
        self._allow(ctx, renewal, "submit")
        if renewal.new_version is None:
            raise ConflictError("Upload the renewed document first.", code="renewal_no_file")
        if renewal.previous_expiry_date and not renewal.proposed_expiry_date:
            raise ValidationFailedError(
                "Enter the new expiry date before sending the renewal for approval.",
                code="renewal_no_expiry",
                details=[{"field": "expiry_date", "message": "Enter the new expiry date."}],
            )
        self._move(ctx, renewal, S.AWAITING_APPROVAL, RenewalAction.SUBMITTED, note)
        emit(
            self.db,
            ctx.organization_id,
            EventType.RENEWAL_AWAITING_APPROVAL,
            renewal_payload(renewal, actor=ctx.user),
        )
        self._audit(ctx, renewal, "renewal.submitted", {})
        self.db.commit()
        kick_dispatcher()
        return renewal

    def approve(self, ctx: OrgContext, renewal_id: uuid.UUID, note: str | None) -> Renewal:
        renewal = self.get(ctx, renewal_id)
        self._allow(ctx, renewal, "approve")
        if renewal.new_version is None:
            raise ConflictError("There is no renewed file to approve.", code="renewal_no_file")
        if renewal.previous_expiry_date and not renewal.proposed_expiry_date:
            raise ValidationFailedError(
                "Enter the new expiry date before approving.", code="renewal_no_expiry"
            )

        document: Document = renewal.document
        version: DocumentVersion = renewal.new_version
        version.state = VersionState.ACTIVE  # the renewed file becomes the current one
        now = utcnow()
        if renewal.proposed_issue_date:
            document.issue_date = renewal.proposed_issue_date
        if renewal.proposed_expiry_date:
            document.expiry_date = renewal.proposed_expiry_date
        document.verification_status = "verified"
        document.verified_by = ctx.user
        document.verified_at = now
        document.rejection_reason = None
        document.updated_at = now
        renewal.completed_at = now
        renewal.completed_by = ctx.user
        self._move(ctx, renewal, S.COMPLETED, RenewalAction.APPROVED, note)
        emit(
            self.db,
            ctx.organization_id,
            EventType.RENEWAL_COMPLETED,
            renewal_payload(renewal, actor=ctx.user),
        )
        self._audit(
            ctx,
            renewal,
            "renewal.completed",
            {
                "version": version.version_number,
                "previous_expiry": renewal.previous_expiry_date.isoformat()
                if renewal.previous_expiry_date
                else None,
                "new_expiry": document.expiry_date.isoformat() if document.expiry_date else None,
            },
        )
        self.db.commit()
        kick_dispatcher()
        self.db.refresh(document)
        return renewal

    def reject(self, ctx: OrgContext, renewal_id: uuid.UUID, reason: str) -> Renewal:
        renewal = self.get(ctx, renewal_id)
        self._allow(ctx, renewal, "reject")
        if renewal.new_version is not None and renewal.new_version.state == VersionState.PENDING:
            renewal.new_version.state = VersionState.REJECTED
        renewal.rejection_reason = reason.strip()[:500]
        self._move(ctx, renewal, S.REJECTED, RenewalAction.REJECTED, renewal.rejection_reason)
        emit(
            self.db,
            ctx.organization_id,
            EventType.RENEWAL_REJECTED,
            renewal_payload(renewal, actor=ctx.user, reason=renewal.rejection_reason),
        )
        self._audit(ctx, renewal, "renewal.rejected", {"reason": renewal.rejection_reason})
        self.db.commit()
        kick_dispatcher()
        return renewal

    def reopen(self, ctx: OrgContext, renewal_id: uuid.UUID) -> Renewal:
        renewal = self.get(ctx, renewal_id)
        self._allow(ctx, renewal, "reopen")
        if self.repo(ctx).open_for_document(renewal.document_id) is not None:
            raise ConflictError(
                "This document already has another renewal in progress.", code="renewal_exists"
            )
        renewal.new_version = None
        renewal.completed_at = None
        self._move(ctx, renewal, S.IN_PROGRESS, RenewalAction.REOPENED, None)
        self._audit(ctx, renewal, "renewal.reopened", {})
        self.db.commit()
        return renewal

    # ----------------------------------------------------------------- helpers

    def _check_assignee(self, ctx: OrgContext, membership_id: uuid.UUID):
        from app.core.permissions import has_permission

        assignee = resolve_owner(self.db, ctx, membership_id)
        if assignee is not None and not has_permission(assignee.role, Permission.DOCUMENTS_WRITE):
            raise field_error("assignee_membership_id", "Viewers cannot be assigned renewals.")
        return assignee

    def _allow(self, ctx: OrgContext, renewal: Renewal, action: str) -> None:
        if renewal_permissions(ctx, renewal).get(action):
            return
        manager = ctx.has(Permission.RENEWALS_MANAGE)
        assignee = renewal.assignee_membership_id == ctx.membership.id
        if not manager and not (assignee and action in ("start", "upload", "submit")):
            raise PermissionDeniedError("Your role does not allow this.")
        raise ConflictError(
            f"This renewal is {renewal.status.replace('_', ' ')}, so that step is not available.",
            code="renewal_wrong_status",
        )

    def _move(
        self,
        ctx: OrgContext,
        renewal: Renewal,
        to: str,
        action: str,
        note: str | None,
        *,
        force: bool = False,
    ) -> None:
        before = renewal.status
        if before != to or force:
            renewal.status = to
            self._event(ctx, renewal, action, before, to, note)

    def _event(
        self, ctx: OrgContext, renewal: Renewal, action: str, before: str | None, after: str, note: str | None
    ) -> None:
        event = RenewalEvent(
            organization_id=ctx.organization_id,
            renewal_id=renewal.id,
            actor_user_id=ctx.user.id,
            action=action,
            from_status=before,
            to_status=after,
            note=(note or "").strip()[:1000] or None,
        )
        renewal.events.append(event)

    def _audit(self, ctx: OrgContext, renewal: Renewal, action: str, metadata: dict[str, Any]) -> None:
        self.audit.record_for(
            ctx,
            action=action,
            resource_type="renewal",
            resource_id=renewal.id,
            metadata={"document": renewal.document.name, "status": renewal.status, **metadata},
        )
