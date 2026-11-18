"""Organization, membership and invitation schemas."""

import uuid
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import EmailStr, Field, field_validator

from app.core.constants import COMPANY_SIZES, DOCUMENT_CATEGORIES, INDUSTRIES
from app.core.permissions import Role, permissions_for
from app.models import Invitation, Membership, Organization
from app.schemas.auth import UserOut
from app.schemas.common import Option, Schema


def _validate_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("Unknown time zone") from exc
    return value


def _validate_choice(value: str | None, choices: dict[str, str], label: str) -> str | None:
    if value is not None and value not in choices:
        raise ValueError(f"Unknown {label}")
    return value


class _OrganizationFields(Schema):
    @field_validator("name", check_fields=False)
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("Enter a name")
        return value.strip() if value is not None else None

    @field_validator("industry", check_fields=False)
    @classmethod
    def _industry(cls, value: str | None) -> str | None:
        return _validate_choice(value, INDUSTRIES, "industry")

    @field_validator("company_size", check_fields=False)
    @classmethod
    def _company_size(cls, value: str | None) -> str | None:
        return _validate_choice(value, COMPANY_SIZES, "company size")

    @field_validator("timezone", check_fields=False)
    @classmethod
    def _timezone(cls, value: str | None) -> str | None:
        return _validate_timezone(value) if value is not None else None


class OrganizationCreate(_OrganizationFields):
    name: str = Field(min_length=1, max_length=200)
    industry: str | None = None
    company_size: str | None = None
    timezone: str = "UTC"


class OrganizationUpdate(_OrganizationFields):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    industry: str | None = None
    company_size: str | None = None
    timezone: str | None = None
    document_categories: list[str] | None = None
    reminders_enabled: bool | None = None
    reminder_offsets: list[int] | None = None

    @field_validator("reminder_offsets")
    @classmethod
    def _offsets(cls, value: list[int] | None) -> list[int] | None:
        if value is None:
            return None
        if len(value) > 10:
            raise ValueError("Use at most 10 reminder days")
        if any(day < 0 or day > 365 for day in value):
            raise ValueError("Reminder days must be between 0 and 365")
        return sorted(set(value), reverse=True)

    @field_validator("document_categories")
    @classmethod
    def _categories(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        unknown = [item for item in value if item not in DOCUMENT_CATEGORIES]
        if unknown:
            raise ValueError(f"Unknown document categories: {', '.join(unknown)}")
        # Keep the canonical order and drop duplicates.
        return [key for key in DOCUMENT_CATEGORIES if key in value]

    def changes(self) -> dict[str, Any]:
        """Only the fields the client sent. `name` and `timezone` cannot be cleared."""
        data = self.model_dump(exclude_unset=True)
        for required in ("name", "timezone", "document_categories", "reminders_enabled", "reminder_offsets"):
            if required in data and data[required] is None:
                del data[required]
        return data


class OrganizationOut(Schema):
    id: uuid.UUID
    name: str
    slug: str
    industry: str | None
    company_size: str | None
    timezone: str
    document_categories: list[str]
    onboarding_completed_at: datetime | None
    reminders_enabled: bool
    reminder_offsets: list[int]
    created_at: datetime
    deletion_scheduled_for: datetime | None = None
    role: Role
    permissions: list[str]

    @classmethod
    def build(cls, organization: Organization, membership: Membership) -> "OrganizationOut":
        role = Role(membership.role)
        return cls(
            id=organization.id,
            name=organization.name,
            slug=organization.slug,
            industry=organization.industry,
            company_size=organization.company_size,
            timezone=organization.timezone,
            document_categories=list(organization.document_categories or []),
            onboarding_completed_at=organization.onboarding_completed_at,
            reminders_enabled=organization.reminders_enabled,
            reminder_offsets=sorted(organization.reminder_offsets or [], reverse=True),
            created_at=organization.created_at,
            deletion_scheduled_for=organization.deletion_scheduled_for,
            role=role,
            permissions=sorted(permission.value for permission in permissions_for(role)),
        )


class MeResponse(Schema):
    user: UserOut
    organizations: list[OrganizationOut]


class MemberOut(Schema):
    id: uuid.UUID
    role: Role
    user: UserOut
    joined_at: datetime
    is_self: bool

    @classmethod
    def build(cls, membership: Membership, *, current_user_id: uuid.UUID) -> "MemberOut":
        return cls(
            id=membership.id,
            role=Role(membership.role),
            user=UserOut.model_validate(membership.user),
            joined_at=membership.created_at,
            is_self=membership.user_id == current_user_id,
        )


class RoleUpdate(Schema):
    role: Role


class InvitationCreate(Schema):
    email: EmailStr
    role: Role = Role.MEMBER


class InvitationOut(Schema):
    id: uuid.UUID
    email: str
    role: Role
    status: str
    expires_at: datetime
    last_sent_at: datetime
    created_at: datetime
    invited_by_name: str | None

    @classmethod
    def build(cls, invitation: Invitation) -> "InvitationOut":
        return cls(
            id=invitation.id,
            email=invitation.email,
            role=Role(invitation.role),
            status=invitation.status,
            expires_at=invitation.expires_at,
            last_sent_at=invitation.last_sent_at,
            created_at=invitation.created_at,
            invited_by_name=invitation.invited_by.full_name if invitation.invited_by else None,
        )


class InvitationPreviewOut(Schema):
    organization_name: str
    email: str
    role: Role
    status: str
    invited_by_name: str | None


class OptionsOut(Schema):
    industries: list[Option]
    company_sizes: list[Option]
    document_categories: list[Option]
    roles: list[Option]


class DeletionRequest(Schema):
    confirm_name: str = Field(min_length=1, max_length=200)
