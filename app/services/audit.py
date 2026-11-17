"""Audit trail.

`record` adds an entry to the caller's open transaction, so the entry commits
or rolls back together with the change it describes.
"""

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.models import AuditLog
from app.repositories.audit import AuditLogRepository
from app.services.context import OrgContext

_FORBIDDEN_KEY_FRAGMENTS = ("password", "token", "secret")


class AuditAction:
    ORGANIZATION_CREATED = "organization.created"
    ORGANIZATION_SETTINGS_CHANGED = "organization.settings_changed"
    ORGANIZATION_ONBOARDING_COMPLETED = "organization.onboarding_completed"
    MEMBER_INVITED = "member.invited"
    MEMBER_INVITATION_RESENT = "member.invitation_resent"
    MEMBER_INVITATION_REVOKED = "member.invitation_revoked"
    MEMBER_JOINED = "member.joined"
    MEMBER_ROLE_CHANGED = "member.role_changed"
    MEMBER_REMOVED = "member.removed"
    MEMBER_LEFT = "member.left"
    DOCUMENT_UPLOADED = "document.uploaded"
    DOCUMENT_UPDATED = "document.updated"
    DOCUMENT_VERSION_ADDED = "document.version_added"
    DOCUMENT_VERIFIED = "document.verified"
    DOCUMENT_REJECTED = "document.rejected"
    DOCUMENT_DOWNLOADED = "document.downloaded"
    DOCUMENT_ARCHIVED = "document.archived"
    DOCUMENT_RESTORED = "document.restored"
    DOCUMENT_EXTRACTION_APPLIED = "document.extraction_applied"
    DOCUMENT_EXTRACTION_DISMISSED = "document.extraction_dismissed"
    DOCUMENT_EXTRACTION_RETRIED = "document.extraction_retried"
    ENTITY_CREATED = "entity.created"
    ENTITY_UPDATED = "entity.updated"
    ENTITY_ARCHIVED = "entity.archived"
    ENTITY_RESTORED = "entity.restored"


def _check_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    for key in metadata:
        if any(fragment in key.lower() for fragment in _FORBIDDEN_KEY_FRAGMENTS):
            raise ValueError(f"Audit metadata must not contain secrets (key: {key!r})")
    return metadata


class AuditService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def record(
        self,
        *,
        organization_id: uuid.UUID,
        actor_user_id: uuid.UUID | None,
        action: str,
        resource_type: str,
        resource_id: uuid.UUID | str | None = None,
        metadata: dict[str, Any] | None = None,
        ip_address: str | None = None,
    ) -> AuditLog:
        entry = AuditLog(
            organization_id=organization_id,
            actor_user_id=actor_user_id,
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id) if resource_id is not None else None,
            event_metadata=_check_metadata(metadata or {}),
            ip_address=ip_address,
        )
        return AuditLogRepository(self.db, organization_id).add(entry)

    def record_for(
        self,
        ctx: OrgContext,
        *,
        action: str,
        resource_type: str,
        resource_id: uuid.UUID | str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AuditLog:
        return self.record(
            organization_id=ctx.organization_id,
            actor_user_id=ctx.user.id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            metadata=metadata,
            ip_address=ctx.meta.ip_address,
        )

    def list(
        self, ctx: OrgContext, *, page: int, page_size: int, action_prefix: str | None = None
    ) -> tuple[list[AuditLog], int]:
        return AuditLogRepository(self.db, ctx.organization_id).list(
            page=page, page_size=page_size, action_prefix=action_prefix
        )
