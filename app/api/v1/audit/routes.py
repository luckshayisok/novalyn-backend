"""Audit log endpoints. Read only."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import require_permission
from app.core.database import get_db
from app.core.permissions import Permission
from app.schemas.audit import AuditLogOut
from app.schemas.common import Page
from app.services.audit import AuditService
from app.services.context import OrgContext

router = APIRouter(prefix="/organizations/{organization_id}/audit-logs", tags=["audit"])


@router.get("", response_model=Page[AuditLogOut])
def list_audit_logs(
    page: int = Query(default=1, ge=1, le=10_000),
    page_size: int = Query(default=25, ge=1, le=100),
    action: str | None = Query(
        default=None,
        max_length=100,
        pattern=r"^[a-z_.]+$",
        description="Filter by action or action prefix, for example `member.`",
    ),
    ctx: OrgContext = Depends(require_permission(Permission.AUDIT_READ)),
    db: Session = Depends(get_db),
) -> Page[AuditLogOut]:
    items, total = AuditService(db).list(ctx, page=page, page_size=page_size, action_prefix=action)
    return Page[AuditLogOut](
        items=[AuditLogOut.build(item) for item in items],
        total=total,
        page=page,
        page_size=page_size,
    )
