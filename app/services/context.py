"""Request context passed from the API layer into services."""

import uuid
from dataclasses import dataclass

from app.core.errors import PermissionDeniedError
from app.core.permissions import Permission, Role, has_permission
from app.models import Membership, Organization, User


@dataclass(frozen=True)
class RequestMeta:
    ip_address: str | None = None
    user_agent: str | None = None


@dataclass(frozen=True)
class OrgContext:
    """The authenticated caller acting inside one organization."""

    organization: Organization
    membership: Membership
    user: User
    meta: RequestMeta

    @property
    def organization_id(self) -> uuid.UUID:
        return self.organization.id

    @property
    def role(self) -> Role:
        return Role(self.membership.role)

    def has(self, permission: Permission) -> bool:
        return has_permission(self.role, permission)

    def require(self, permission: Permission) -> None:
        if not self.has(permission):
            raise PermissionDeniedError()
