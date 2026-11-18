"""Repositories for organizations, memberships and invitations."""

import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.permissions import Role
from app.core.time import utcnow
from app.models import Invitation, Membership, Organization, User
from app.repositories.base import TenantRepository


class OrganizationRepository:
    """Organizations are the tenants themselves, so lookups here are by ID or by user."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def get(self, organization_id: uuid.UUID) -> Organization | None:
        return self.db.get(Organization, organization_id)

    def add(self, organization: Organization) -> Organization:
        self.db.add(organization)
        return organization

    def slug_exists(self, slug: str) -> bool:
        return self.db.scalar(select(Organization.id).where(Organization.slug == slug)) is not None

    def list_for_user(self, user_id: uuid.UUID) -> list[tuple[Organization, Membership]]:
        rows = self.db.execute(
            select(Organization, Membership)
            .join(Membership, Membership.organization_id == Organization.id)
            .where(
                Membership.user_id == user_id,
                # Organizations past their deletion date are gone, even before the nightly job erases them.
                or_(
                    Organization.deletion_scheduled_for.is_(None),
                    Organization.deletion_scheduled_for > utcnow(),
                ),
            )
            .order_by(Organization.name)
        ).all()
        return [(row[0], row[1]) for row in rows]


class MembershipRepository(TenantRepository[Membership]):
    model = Membership

    def get_by_user(self, user_id: uuid.UUID) -> Membership | None:
        return self.db.scalars(self._select().where(Membership.user_id == user_id)).first()

    def get_by_email(self, email: str) -> Membership | None:
        return self.db.scalars(
            self._select().join(User, User.id == Membership.user_id).where(User.email == email)
        ).first()

    def list(self) -> list[Membership]:
        return list(
            self.db.scalars(
                self._select().join(User, User.id == Membership.user_id).order_by(User.full_name, User.email)
            )
        )

    def count(self) -> int:
        return (
            self.db.scalar(
                select(func.count())
                .select_from(Membership)
                .where(Membership.organization_id == self.organization_id)
            )
            or 0
        )

    def count_owners(self) -> int:
        return (
            self.db.scalar(
                select(func.count())
                .select_from(Membership)
                .where(
                    Membership.organization_id == self.organization_id,
                    Membership.role == Role.OWNER.value,
                )
            )
            or 0
        )


class InvitationRepository(TenantRepository[Invitation]):
    model = Invitation

    def list_open(self) -> list[Invitation]:
        """Invitations that are neither accepted nor revoked, including expired ones."""
        return list(
            self.db.scalars(
                self._select()
                .where(Invitation.accepted_at.is_(None), Invitation.revoked_at.is_(None))
                .order_by(Invitation.created_at.desc())
            )
        )

    def get_open_by_email(self, email: str) -> Invitation | None:
        return self.db.scalars(
            self._select().where(
                Invitation.email == email,
                Invitation.accepted_at.is_(None),
                Invitation.revoked_at.is_(None),
            )
        ).first()


def get_invitation_by_token_hash(db: Session, token_hash: str) -> Invitation | None:
    """Look an invitation up by its secret token.

    This is the one deliberate cross-tenant read: the token itself is the proof
    of access, and the caller does not yet belong to the organization.
    """
    return db.scalars(select(Invitation).where(Invitation.token_hash == token_hash)).first()
