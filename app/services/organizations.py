"""Organizations: creation, settings and onboarding."""

import re
import secrets
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import ConflictError
from app.core.permissions import Role
from app.core.plans import Feature
from app.core.tenancy import set_tenant
from app.core.time import utcnow
from app.models import Membership, Organization, Subscription, SubscriptionStatus, User
from app.repositories.organizations import MembershipRepository, OrganizationRepository
from app.services.audit import AuditAction, AuditService
from app.services.context import OrgContext, RequestMeta
from app.services.plans import Entitlements

MAX_ORGANIZATIONS_PER_USER = 25
_SLUG_INVALID = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    slug = _SLUG_INVALID.sub("-", name.lower()).strip("-")[:60].strip("-")
    return slug or "organization"


class OrganizationService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.organizations = OrganizationRepository(db)
        self.audit = AuditService(db)

    def list_for_user(self, user: User) -> list[tuple[Organization, Membership]]:
        return self.organizations.list_for_user(user.id)

    def create(
        self,
        user: User,
        *,
        name: str,
        industry: str | None,
        company_size: str | None,
        timezone: str,
        meta: RequestMeta,
    ) -> tuple[Organization, Membership]:
        if len(self.organizations.list_for_user(user.id)) >= MAX_ORGANIZATIONS_PER_USER:
            raise ConflictError(
                "You have reached the number of organizations one account can belong to.",
                code="organization_limit_reached",
            )

        organization = Organization(
            name=name.strip(),
            slug=self._unique_slug(name),
            industry=industry,
            company_size=company_size,
            timezone=timezone,
            document_categories=[],
            created_by_id=user.id,
        )
        self.organizations.add(organization)
        self.db.flush()
        # Everything below belongs to the new organization.
        set_tenant(self.db, organization.id)

        membership = Membership(organization_id=organization.id, user_id=user.id, role=Role.OWNER.value)
        self.db.add(
            Subscription(
                organization_id=organization.id,
                plan=get_settings().default_plan,
                status=SubscriptionStatus.ACTIVE,
                custom_limits={},
            )
        )
        MembershipRepository(self.db, organization.id).add(membership)
        self.audit.record(
            organization_id=organization.id,
            actor_user_id=user.id,
            action=AuditAction.ORGANIZATION_CREATED,
            resource_type="organization",
            resource_id=organization.id,
            metadata={"name": organization.name},
            ip_address=meta.ip_address,
        )
        self.db.commit()
        return organization, membership

    def update(self, ctx: OrgContext, changes: dict[str, Any]) -> Organization:
        organization = ctx.organization
        offsets = changes.get("reminder_offsets")
        if offsets is not None and sorted(offsets) != sorted(organization.reminder_offsets or []):
            Entitlements(self.db, ctx.organization_id).require(Feature.CUSTOM_REMINDERS)
        changed: dict[str, dict[str, Any]] = {}
        for field, value in changes.items():
            if isinstance(value, str):
                value = value.strip()
            current = getattr(organization, field)
            if current != value:
                changed[field] = {"from": current, "to": value}
                setattr(organization, field, value)

        if changed:
            self.audit.record_for(
                ctx,
                action=AuditAction.ORGANIZATION_SETTINGS_CHANGED,
                resource_type="organization",
                resource_id=organization.id,
                metadata={"changes": changed},
            )
            self.db.commit()
        return organization

    def complete_onboarding(self, ctx: OrgContext) -> Organization:
        organization = ctx.organization
        if organization.onboarding_completed_at is None:
            organization.onboarding_completed_at = utcnow()
            self.audit.record_for(
                ctx,
                action=AuditAction.ORGANIZATION_ONBOARDING_COMPLETED,
                resource_type="organization",
                resource_id=organization.id,
            )
            self.db.commit()
        return organization

    def _unique_slug(self, name: str) -> str:
        base = slugify(name)
        candidate = base
        for _ in range(20):
            if not self.organizations.slug_exists(candidate):
                return candidate
            candidate = f"{base}-{secrets.token_hex(2)}"
        return f"{base}-{secrets.token_hex(6)}"
