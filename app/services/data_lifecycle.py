"""Deleting organizations and accounts, and removing records that are no longer needed.

Organization deletion has a grace period: an owner schedules it, owners and
admins are emailed, and any owner can cancel until the date. From that date
the organization is unreachable, and the daily job erases the stored files
first and the database rows second (every tenant-owned row goes with the
organization through ON DELETE CASCADE). If file deletion stops halfway, the
rows stay and the next run finishes the job; files already gone are skipped.

Account deletion erases the person: their memberships, sessions, links and
notifications go with them; documents and history they touched keep the
record, attributed to "Deleted user".
"""

from __future__ import annotations

import logging
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import AuthenticationError, ConflictError, ValidationFailedError
from app.core.permissions import Role
from app.core.security import verify_password
from app.core.storage import StoredFileNotFoundError, get_storage
from app.core.tenancy import all_tenants
from app.core.time import utcnow
from app.integrations.email import templates
from app.models import (
    BillingEvent,
    DocumentVersion,
    Invitation,
    Membership,
    Notification,
    Organization,
    OutboxEvent,
    OutboxStatus,
    RefreshToken,
    SubscriptionStatus,
    User,
    UserToken,
)
from app.services.audit import AuditService
from app.services.context import OrgContext, RequestMeta
from app.services.plans import get_subscription
from app.workers.notification_tasks import enqueue_email

logger = logging.getLogger(__name__)


# --------------------------------------------------------------- organizations


class OrganizationDeletionService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.settings = get_settings()

    def schedule(self, ctx: OrgContext, *, confirm_name: str) -> Organization:
        organization = ctx.organization
        if confirm_name.strip() != organization.name:
            raise ValidationFailedError(
                "Type the organization's name exactly to confirm.",
                code="confirmation_mismatch",
                details=[
                    {"field": "confirm_name", "message": "This does not match the organization's name."}
                ],
            )
        if organization.deletion_scheduled_for is not None:
            raise ConflictError("Deletion is already scheduled.", code="deletion_already_scheduled")
        subscription = get_subscription(self.db, organization.id)
        if (
            subscription.provider in ("stripe", "razorpay")
            and subscription.provider_subscription_id
            and subscription.status in SubscriptionStatus.ENTITLED
            and not subscription.cancel_at_period_end
        ):
            raise ConflictError(
                "Cancel the paid subscription on the billing page first, so you are not charged again.",
                code="cancel_subscription_first",
            )

        now = utcnow()
        organization.deletion_requested_at = now
        organization.deletion_scheduled_for = now + timedelta(
            days=self.settings.organization_deletion_grace_days
        )
        organization.deletion_requested_by_id = ctx.user.id
        AuditService(self.db).record_for(
            ctx,
            action="organization.deletion_scheduled",
            resource_type="organization",
            resource_id=organization.id,
            metadata={"scheduled_for": organization.deletion_scheduled_for.date().isoformat()},
        )
        self.db.commit()
        self._tell_managers(ctx, organization)
        return organization

    def cancel(self, ctx: OrgContext) -> Organization:
        organization = ctx.organization
        if organization.deletion_scheduled_for is None:
            raise ConflictError("No deletion is scheduled.", code="deletion_not_scheduled")
        organization.deletion_requested_at = None
        organization.deletion_scheduled_for = None
        organization.deletion_requested_by_id = None
        AuditService(self.db).record_for(
            ctx,
            action="organization.deletion_canceled",
            resource_type="organization",
            resource_id=organization.id,
        )
        self.db.commit()
        return organization

    def _tell_managers(self, ctx: OrgContext, organization: Organization) -> None:
        managers = self.db.scalars(
            select(Membership).where(
                Membership.organization_id == organization.id,
                Membership.role.in_([Role.OWNER.value, Role.ADMIN.value]),
            )
        ).all()
        url = f"{self.settings.frontend_url.rstrip('/')}/o/{organization.slug}/settings"
        delete_on = f"{organization.deletion_scheduled_for:%d %B %Y}"
        for membership in managers:
            if membership.user.email_verified_at is None:
                continue
            enqueue_email(
                templates.organization_deletion_email(
                    to=membership.user.email,
                    organization_name=organization.name,
                    requested_by=ctx.user.full_name,
                    delete_on=delete_on,
                    url=url,
                )
            )


@dataclass(frozen=True)
class PurgeResult:
    organizations: list[str]
    files_deleted: int


class OrganizationPurger:
    """Erase organizations whose grace period has ended. Needs a system session."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def run(self, now: datetime | None = None) -> PurgeResult:
        now = now or utcnow()
        storage = get_storage()
        due = self.db.scalars(
            select(Organization).where(
                Organization.deletion_scheduled_for.is_not(None), Organization.deletion_scheduled_for <= now
            )
        ).all()
        erased: list[str] = []
        files = 0
        for organization in due:
            keys = self.db.scalars(
                select(DocumentVersion.storage_key).where(DocumentVersion.organization_id == organization.id)
            ).all()
            try:
                for key in keys:
                    with suppress(StoredFileNotFoundError):  # already gone: nothing to do
                        storage.delete(key)
                    files += 1
            except Exception:
                logger.exception("Could not delete the files of organization %s; will retry", organization.id)
                continue
            name, organization_id = organization.name, organization.id
            self.db.execute(delete(Organization).where(Organization.id == organization_id))
            self.db.commit()
            logger.info(
                "Organization erased", extra={"organization_id": str(organization_id), "files": len(keys)}
            )
            erased.append(name)
        return PurgeResult(erased, files)


# -------------------------------------------------------------------- accounts


class AccountDeletionService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def delete(self, user: User, *, password: str, meta: RequestMeta) -> None:
        if not verify_password(password, user.password_hash):
            raise AuthenticationError("The password is not correct.", code="wrong_password")

        with all_tenants(self.db):
            memberships = self.db.scalars(select(Membership).where(Membership.user_id == user.id)).all()
            blocking = []
            for membership in memberships:
                if membership.role != Role.OWNER.value:
                    continue
                organization = self.db.get(Organization, membership.organization_id)
                other_owners = self.db.scalars(
                    select(Membership.id).where(
                        Membership.organization_id == membership.organization_id,
                        Membership.role == Role.OWNER.value,
                        Membership.user_id != user.id,
                    )
                ).first()
                if organization and other_owners is None and organization.deletion_scheduled_for is None:
                    blocking.append(organization.name)
            if blocking:
                raise ConflictError(
                    "You are the only owner of "
                    + ", ".join(sorted(blocking))
                    + ". Make someone else an owner, or delete the organization, first.",
                    code="sole_owner",
                    details={"organizations": sorted(blocking)},
                )

            audit = AuditService(self.db)
            for membership in memberships:
                # No name or email: the record says that a member left, not who they were.
                audit.record(
                    organization_id=membership.organization_id,
                    actor_user_id=None,
                    action="member.account_deleted",
                    resource_type="membership",
                    resource_id=membership.id,
                    metadata={"role": membership.role},
                    ip_address=None,
                )
            self.db.delete(user)
            self.db.commit()
        logger.info("Account deleted", extra={"user_id": str(user.id)})


# ------------------------------------------------------------------- retention


class RetentionService:
    """Remove records that have served their purpose. Needs a system session."""

    def __init__(self, db: Session) -> None:
        self.db = db
        self.settings = get_settings()

    def run(self, now: datetime | None = None) -> dict[str, int]:
        now = now or utcnow()
        s = self.settings

        def before(days: int) -> datetime:
            return now - timedelta(days=days)

        statements = {
            "sessions": delete(RefreshToken).where(
                or_(
                    RefreshToken.expires_at < before(s.retention_sessions_days),
                    RefreshToken.revoked_at < before(s.retention_sessions_days),
                )
            ),
            "email_links": delete(UserToken).where(
                or_(
                    UserToken.expires_at < before(s.retention_sessions_days),
                    UserToken.used_at < before(s.retention_sessions_days),
                )
            ),
            "notification_events": delete(OutboxEvent).where(
                OutboxEvent.status.in_([OutboxStatus.DONE, OutboxStatus.FAILED]),
                OutboxEvent.created_at < before(s.retention_outbox_days),
            ),
            "read_notifications": delete(Notification).where(
                Notification.read_at < before(s.retention_read_notifications_days)
            ),
            "closed_invitations": delete(Invitation).where(
                or_(
                    Invitation.accepted_at < before(s.retention_closed_invitations_days),
                    Invitation.revoked_at < before(s.retention_closed_invitations_days),
                    Invitation.expires_at < before(s.retention_closed_invitations_days),
                )
            ),
            "billing_events": delete(BillingEvent).where(
                BillingEvent.processed_at < before(s.retention_billing_events_days)
            ),
        }
        removed = {name: self.db.execute(statement).rowcount or 0 for name, statement in statements.items()}
        self.db.commit()
        return removed
