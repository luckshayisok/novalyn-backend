"""Organization invitations: create, resend, revoke, preview and accept."""

import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.constants import ROLE_LABELS
from app.core.errors import AppError, ConflictError, NotFoundError, PermissionDeniedError
from app.core.permissions import Role, invitable_roles
from app.core.plans import Feature
from app.core.rate_limit import enforce
from app.core.security import generate_token, hash_token
from app.core.tenancy import set_tenant
from app.core.time import utcnow
from app.integrations.email import templates
from app.models import Invitation, Membership, Organization, User
from app.repositories.organizations import (
    InvitationRepository,
    MembershipRepository,
    OrganizationRepository,
    get_invitation_by_token_hash,
)
from app.services.audit import AuditAction, AuditService
from app.services.auth import normalize_email
from app.services.context import OrgContext, RequestMeta
from app.services.plans import Entitlements
from app.workers.notification_tasks import enqueue_email

INVITES_PER_ORG_PER_HOUR = 50
# Roles beyond Owner, Admin and Member come with the advanced permissions feature.
ADVANCED_ROLES = (Role.COMPLIANCE_MANAGER, Role.VIEWER)


class InvitationUnavailableError(AppError):
    status_code = 410
    code = "invitation_unavailable"
    message = "This invitation is no longer available."


@dataclass(frozen=True)
class InvitationPreview:
    organization_name: str
    email: str
    role: str
    status: str
    invited_by_name: str | None


class InvitationService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.settings = get_settings()
        self.audit = AuditService(db)

    # ----------------------------------------------------------- inside the org

    def list_open(self, ctx: OrgContext) -> list[Invitation]:
        return InvitationRepository(self.db, ctx.organization_id).list_open()

    def create(self, ctx: OrgContext, *, email: str, role: Role) -> Invitation:
        if not ctx.user.is_email_verified:
            raise PermissionDeniedError(
                "Confirm your own email address before inviting other people.",
                code="email_not_verified",
            )
        if role not in invitable_roles(ctx.role):
            raise PermissionDeniedError("Your role does not allow inviting someone with this role.")
        entitlements = Entitlements(self.db, ctx.organization_id)
        if role in ADVANCED_ROLES:
            entitlements.require(Feature.ADVANCED_PERMISSIONS)

        email = normalize_email(email)
        if MembershipRepository(self.db, ctx.organization_id).get_by_email(email) is not None:
            raise ConflictError("This person is already a member.", code="already_member")

        invitations = InvitationRepository(self.db, ctx.organization_id)
        now = utcnow()
        existing = invitations.get_open_by_email(email)
        if existing is not None:
            if existing.expires_at > now:
                raise ConflictError(
                    "This person already has a pending invitation. You can resend it.",
                    code="already_invited",
                )
            existing.revoked_at = now  # expired: replace it
            self.db.flush()

        enforce(f"invite:{ctx.organization_id}", INVITES_PER_ORG_PER_HOUR, 3600)
        entitlements.check_members(adding=1)

        raw_token = generate_token()
        invitation = Invitation(
            organization_id=ctx.organization_id,
            email=email,
            role=role.value,
            token_hash=hash_token(raw_token),
            invited_by_id=ctx.user.id,
            expires_at=now + timedelta(days=self.settings.invitation_ttl_days),
            last_sent_at=now,
        )
        invitations.add(invitation)
        try:
            self.db.flush()
        except IntegrityError as exc:
            # Two people invited the same address at the same moment.
            self.db.rollback()
            raise ConflictError(
                "This person already has a pending invitation. You can resend it.",
                code="already_invited",
            ) from exc
        self.audit.record_for(
            ctx,
            action=AuditAction.MEMBER_INVITED,
            resource_type="invitation",
            resource_id=invitation.id,
            metadata={"email": email, "role": role.value},
        )
        self.db.commit()

        self._send(ctx.organization, ctx.user, invitation, raw_token)
        return invitation

    def resend(self, ctx: OrgContext, invitation_id: uuid.UUID) -> Invitation:
        invitation = self._get_open(ctx, invitation_id)
        enforce(f"invite:{ctx.organization_id}", INVITES_PER_ORG_PER_HOUR, 3600)

        # A new token is issued; the link in the earlier email stops working.
        raw_token = generate_token()
        now = utcnow()
        invitation.token_hash = hash_token(raw_token)
        invitation.expires_at = now + timedelta(days=self.settings.invitation_ttl_days)
        invitation.last_sent_at = now
        self.audit.record_for(
            ctx,
            action=AuditAction.MEMBER_INVITATION_RESENT,
            resource_type="invitation",
            resource_id=invitation.id,
            metadata={"email": invitation.email},
        )
        self.db.commit()

        self._send(ctx.organization, ctx.user, invitation, raw_token)
        return invitation

    def revoke(self, ctx: OrgContext, invitation_id: uuid.UUID) -> None:
        invitation = self._get_open(ctx, invitation_id)
        invitation.revoked_at = utcnow()
        self.audit.record_for(
            ctx,
            action=AuditAction.MEMBER_INVITATION_REVOKED,
            resource_type="invitation",
            resource_id=invitation.id,
            metadata={"email": invitation.email},
        )
        self.db.commit()

    # ------------------------------------------------------------- by token

    def preview(self, raw_token: str) -> InvitationPreview:
        invitation = self._get_by_token(raw_token)
        organization = OrganizationRepository(self.db).get(invitation.organization_id)
        if organization is None:
            raise NotFoundError("This invitation was not found.")
        return InvitationPreview(
            organization_name=organization.name,
            email=invitation.email,
            role=invitation.role,
            status=invitation.status,
            invited_by_name=invitation.invited_by.full_name if invitation.invited_by else None,
        )

    def accept(self, user: User, raw_token: str, meta: RequestMeta) -> tuple[Organization, Membership]:
        invitation = self._get_by_token(raw_token)
        if invitation.status != "pending":
            raise InvitationUnavailableError(code=f"invitation_{invitation.status}")
        if invitation.email != user.email:
            raise PermissionDeniedError(
                "This invitation was sent to a different email address. "
                "Sign in with that address to accept it.",
                code="invitation_email_mismatch",
            )

        organization = OrganizationRepository(self.db).get(invitation.organization_id)
        if organization is None:
            raise NotFoundError("This invitation was not found.")

        set_tenant(self.db, organization.id)
        memberships = MembershipRepository(self.db, organization.id)
        if memberships.get_by_user(user.id) is not None:
            raise ConflictError("You are already a member of this organization.", code="already_member")
        # The open invitation was counted when it was sent; check again in case the plan changed since.
        Entitlements(self.db, organization.id).check_members(adding=1, count_pending=False)

        now = utcnow()
        membership = Membership(organization_id=organization.id, user_id=user.id, role=invitation.role)
        memberships.add(membership)
        invitation.accepted_at = now
        invitation.accepted_by_id = user.id
        if user.email_verified_at is None:
            # The invitation link was delivered to this address.
            user.email_verified_at = now
        try:
            self.db.flush()
        except IntegrityError as exc:
            self.db.rollback()
            raise ConflictError(
                "You are already a member of this organization.", code="already_member"
            ) from exc
        self.audit.record(
            organization_id=organization.id,
            actor_user_id=user.id,
            action=AuditAction.MEMBER_JOINED,
            resource_type="membership",
            resource_id=membership.id,
            metadata={"email": user.email, "role": invitation.role},
            ip_address=meta.ip_address,
        )
        self.db.commit()
        return organization, membership

    # ------------------------------------------------------------------ helpers

    def _get_open(self, ctx: OrgContext, invitation_id: uuid.UUID) -> Invitation:
        invitation = InvitationRepository(self.db, ctx.organization_id).get(invitation_id)
        if invitation is None:
            raise NotFoundError("This invitation was not found.")
        if invitation.accepted_at is not None or invitation.revoked_at is not None:
            raise ConflictError(
                "This invitation has already been accepted or revoked.", code="invitation_closed"
            )
        return invitation

    def _get_by_token(self, raw_token: str) -> Invitation:
        invitation = get_invitation_by_token_hash(self.db, hash_token(raw_token))
        if invitation is None:
            raise NotFoundError("This invitation was not found.")
        return invitation

    def _send(
        self, organization: Organization, inviter: User, invitation: Invitation, raw_token: str
    ) -> None:
        enqueue_email(
            templates.invitation_email(
                to=invitation.email,
                organization_name=organization.name,
                inviter_name=inviter.full_name,
                role_label=ROLE_LABELS[Role(invitation.role)],
                url=f"{self.settings.frontend_url}/invite/{raw_token}",
                expires_days=self.settings.invitation_ttl_days,
            )
        )
