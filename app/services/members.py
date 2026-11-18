"""Team membership: listing, role changes, removal and leaving."""

import uuid

from sqlalchemy.orm import Session

from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError
from app.core.permissions import Role, assignable_roles, can_manage_member
from app.core.plans import Feature
from app.models import Membership
from app.repositories.organizations import MembershipRepository
from app.services.audit import AuditAction, AuditService
from app.services.context import OrgContext
from app.services.plans import Entitlements

_LAST_OWNER_MESSAGE = "An organization needs at least one owner. Make someone else an owner first."


class MemberService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.audit = AuditService(db)

    def list(self, ctx: OrgContext) -> list[Membership]:
        return MembershipRepository(self.db, ctx.organization_id).list()

    def change_role(self, ctx: OrgContext, membership_id: uuid.UUID, new_role: Role) -> Membership:
        memberships = MembershipRepository(self.db, ctx.organization_id)
        target = memberships.get(membership_id)
        if target is None:
            raise NotFoundError("This member was not found.")
        if target.user_id == ctx.user.id:
            raise PermissionDeniedError("You cannot change your own role.", code="cannot_change_own_role")

        current_role = Role(target.role)
        if not can_manage_member(ctx.role, current_role) or new_role not in assignable_roles(ctx.role):
            raise PermissionDeniedError("Your role does not allow this role change.")
        if new_role in (Role.COMPLIANCE_MANAGER, Role.VIEWER) and new_role is not current_role:
            Entitlements(self.db, ctx.organization_id).require(Feature.ADVANCED_PERMISSIONS)
        if current_role is Role.OWNER and new_role is not Role.OWNER and memberships.count_owners() <= 1:
            raise ConflictError(_LAST_OWNER_MESSAGE, code="last_owner")

        if current_role is not new_role:
            target.role = new_role.value
            self.audit.record_for(
                ctx,
                action=AuditAction.MEMBER_ROLE_CHANGED,
                resource_type="membership",
                resource_id=target.id,
                metadata={
                    "user_id": str(target.user_id),
                    "email": target.user.email,
                    "from": current_role.value,
                    "to": new_role.value,
                },
            )
            self.db.commit()
        return target

    def remove(self, ctx: OrgContext, membership_id: uuid.UUID) -> None:
        memberships = MembershipRepository(self.db, ctx.organization_id)
        target = memberships.get(membership_id)
        if target is None:
            raise NotFoundError("This member was not found.")
        if target.user_id == ctx.user.id:
            raise ConflictError("To remove yourself, leave the organization instead.", code="use_leave")
        if not can_manage_member(ctx.role, target.role):
            raise PermissionDeniedError("Your role does not allow removing this member.")
        if Role(target.role) is Role.OWNER and memberships.count_owners() <= 1:
            raise ConflictError(_LAST_OWNER_MESSAGE, code="last_owner")

        self.audit.record_for(
            ctx,
            action=AuditAction.MEMBER_REMOVED,
            resource_type="membership",
            resource_id=target.id,
            metadata={"user_id": str(target.user_id), "email": target.user.email, "role": target.role},
        )
        memberships.delete(target)
        self.db.commit()

    def leave(self, ctx: OrgContext) -> None:
        memberships = MembershipRepository(self.db, ctx.organization_id)
        if ctx.role is Role.OWNER and memberships.count_owners() <= 1:
            raise ConflictError(_LAST_OWNER_MESSAGE, code="last_owner")

        self.audit.record_for(
            ctx,
            action=AuditAction.MEMBER_LEFT,
            resource_type="membership",
            resource_id=ctx.membership.id,
            metadata={"user_id": str(ctx.user.id), "email": ctx.user.email, "role": ctx.membership.role},
        )
        memberships.delete(ctx.membership)
        self.db.commit()
