"""Roles, permissions and the rules for managing roles.

This module is the single source of truth for authorization. Routes declare a
permission; nothing else in the codebase compares role names directly.
"""

from enum import StrEnum


class Role(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    COMPLIANCE_MANAGER = "compliance_manager"
    MEMBER = "member"
    VIEWER = "viewer"


ROLE_RANK: dict[Role, int] = {
    Role.OWNER: 50,
    Role.ADMIN: 40,
    Role.COMPLIANCE_MANAGER: 30,
    Role.MEMBER: 20,
    Role.VIEWER: 10,
}


class Permission(StrEnum):
    ORG_READ = "organization:read"
    ORG_UPDATE = "organization:update"
    ORG_DELETE = "organization:delete"

    MEMBERS_READ = "members:read"
    MEMBERS_INVITE = "members:invite"
    MEMBERS_MANAGE = "members:manage"

    AUDIT_READ = "audit:read"

    DOCUMENTS_READ = "documents:read"
    DOCUMENTS_WRITE = "documents:write"
    DOCUMENTS_VERIFY = "documents:verify"
    DOCUMENTS_DELETE = "documents:delete"

    ENTITIES_READ = "entities:read"
    ENTITIES_WRITE = "entities:write"

    RENEWALS_READ = "renewals:read"
    RENEWALS_MANAGE = "renewals:manage"

    REPORTS_READ = "reports:read"
    REPORTS_EXPORT = "reports:export"

    SETTINGS_MANAGE = "settings:manage"
    BILLING_MANAGE = "billing:manage"


_VIEWER = frozenset(
    {
        Permission.ORG_READ,
        Permission.MEMBERS_READ,
        Permission.DOCUMENTS_READ,
        Permission.ENTITIES_READ,
        Permission.RENEWALS_READ,
        Permission.REPORTS_READ,
    }
)
_MEMBER = _VIEWER | {Permission.DOCUMENTS_WRITE, Permission.ENTITIES_WRITE}
_COMPLIANCE_MANAGER = _MEMBER | {
    Permission.DOCUMENTS_VERIFY,
    Permission.DOCUMENTS_DELETE,
    Permission.RENEWALS_MANAGE,
    Permission.REPORTS_EXPORT,
    Permission.AUDIT_READ,
}
_ADMIN = _COMPLIANCE_MANAGER | {
    Permission.ORG_UPDATE,
    Permission.MEMBERS_INVITE,
    Permission.MEMBERS_MANAGE,
    Permission.SETTINGS_MANAGE,
}
_OWNER = _ADMIN | {Permission.ORG_DELETE, Permission.BILLING_MANAGE}

ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.VIEWER: frozenset(_VIEWER),
    Role.MEMBER: frozenset(_MEMBER),
    Role.COMPLIANCE_MANAGER: frozenset(_COMPLIANCE_MANAGER),
    Role.ADMIN: frozenset(_ADMIN),
    Role.OWNER: frozenset(_OWNER),
}


def permissions_for(role: Role | str) -> frozenset[Permission]:
    return ROLE_PERMISSIONS[Role(role)]


def has_permission(role: Role | str, permission: Permission) -> bool:
    return permission in permissions_for(role)


def assignable_roles(actor: Role | str) -> list[Role]:
    """Roles the actor may grant to another member."""
    actor = Role(actor)
    if actor is Role.OWNER:
        return list(Role)
    if actor is Role.ADMIN:
        return [role for role in Role if ROLE_RANK[role] < ROLE_RANK[Role.ADMIN]]
    return []


def invitable_roles(actor: Role | str) -> list[Role]:
    """Roles the actor may invite someone as. Ownership is never granted by invitation."""
    return [role for role in assignable_roles(actor) if role is not Role.OWNER]


def can_manage_member(actor: Role | str, target: Role | str) -> bool:
    """Whether the actor may change the role of, or remove, a member with the target role."""
    actor, target = Role(actor), Role(target)
    if actor is Role.OWNER:
        return True
    if actor is Role.ADMIN:
        return ROLE_RANK[target] < ROLE_RANK[Role.ADMIN]
    return False
