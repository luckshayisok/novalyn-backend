"""Organization, member and invitation endpoints."""

import uuid

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_org_context, get_request_meta, require_permission
from app.core.constants import (
    COMPANY_SIZES,
    DOCUMENT_CATEGORIES,
    INDUSTRIES,
    ROLE_DESCRIPTIONS,
    ROLE_LABELS,
)
from app.core.database import get_db
from app.core.permissions import Permission, Role
from app.core.rate_limit import enforce, rate_limit
from app.models import User
from app.schemas.common import Option
from app.schemas.organizations import (
    DeletionRequest,
    InvitationCreate,
    InvitationOut,
    InvitationPreviewOut,
    MemberOut,
    OptionsOut,
    OrganizationCreate,
    OrganizationOut,
    OrganizationUpdate,
    RoleUpdate,
)
from app.services.context import OrgContext, RequestMeta
from app.services.invitations import InvitationService
from app.services.members import MemberService
from app.services.organizations import OrganizationService

router = APIRouter(prefix="/organizations", tags=["organizations"])
invitation_router = APIRouter(prefix="/invitations", tags=["invitations"])
meta_router = APIRouter(prefix="/meta", tags=["meta"])


# ------------------------------------------------------------------ organizations


@router.post("", response_model=OrganizationOut, status_code=status.HTTP_201_CREATED)
def create_organization(
    payload: OrganizationCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    meta: RequestMeta = Depends(get_request_meta),
) -> OrganizationOut:
    enforce(f"create-organization:{user.id}", 10, 3600)
    organization, membership = OrganizationService(db).create(
        user,
        name=payload.name,
        industry=payload.industry,
        company_size=payload.company_size,
        timezone=payload.timezone,
        meta=meta,
    )
    return OrganizationOut.build(organization, membership)


@router.get("", response_model=list[OrganizationOut])
def list_organizations(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> list[OrganizationOut]:
    return [
        OrganizationOut.build(organization, membership)
        for organization, membership in OrganizationService(db).list_for_user(user)
    ]


@router.get("/{organization_id}", response_model=OrganizationOut)
def get_organization(
    ctx: OrgContext = Depends(require_permission(Permission.ORG_READ)),
) -> OrganizationOut:
    return OrganizationOut.build(ctx.organization, ctx.membership)


@router.patch("/{organization_id}", response_model=OrganizationOut)
def update_organization(
    payload: OrganizationUpdate,
    ctx: OrgContext = Depends(require_permission(Permission.ORG_UPDATE)),
    db: Session = Depends(get_db),
) -> OrganizationOut:
    organization = OrganizationService(db).update(ctx, payload.changes())
    return OrganizationOut.build(organization, ctx.membership)


@router.post("/{organization_id}/onboarding/complete", response_model=OrganizationOut)
def complete_onboarding(
    ctx: OrgContext = Depends(require_permission(Permission.ORG_UPDATE)),
    db: Session = Depends(get_db),
) -> OrganizationOut:
    organization = OrganizationService(db).complete_onboarding(ctx)
    return OrganizationOut.build(organization, ctx.membership)


# ------------------------------------------------------------------------ members


@router.get("/{organization_id}/members", response_model=list[MemberOut])
def list_members(
    ctx: OrgContext = Depends(require_permission(Permission.MEMBERS_READ)),
    db: Session = Depends(get_db),
) -> list[MemberOut]:
    return [
        MemberOut.build(membership, current_user_id=ctx.user.id) for membership in MemberService(db).list(ctx)
    ]


@router.patch("/{organization_id}/members/{membership_id}", response_model=MemberOut)
def change_member_role(
    membership_id: uuid.UUID,
    payload: RoleUpdate,
    ctx: OrgContext = Depends(require_permission(Permission.MEMBERS_MANAGE)),
    db: Session = Depends(get_db),
) -> MemberOut:
    membership = MemberService(db).change_role(ctx, membership_id, payload.role)
    return MemberOut.build(membership, current_user_id=ctx.user.id)


@router.delete("/{organization_id}/members/{membership_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_member(
    membership_id: uuid.UUID,
    ctx: OrgContext = Depends(require_permission(Permission.MEMBERS_MANAGE)),
    db: Session = Depends(get_db),
) -> Response:
    MemberService(db).remove(ctx, membership_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{organization_id}/leave", status_code=status.HTTP_204_NO_CONTENT)
def leave_organization(ctx: OrgContext = Depends(get_org_context), db: Session = Depends(get_db)) -> Response:
    MemberService(db).leave(ctx)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# -------------------------------------------------------------------- invitations


@router.get("/{organization_id}/invitations", response_model=list[InvitationOut])
def list_invitations(
    ctx: OrgContext = Depends(require_permission(Permission.MEMBERS_INVITE)),
    db: Session = Depends(get_db),
) -> list[InvitationOut]:
    return [InvitationOut.build(invitation) for invitation in InvitationService(db).list_open(ctx)]


@router.post(
    "/{organization_id}/invitations",
    response_model=InvitationOut,
    status_code=status.HTTP_201_CREATED,
)
def create_invitation(
    payload: InvitationCreate,
    ctx: OrgContext = Depends(require_permission(Permission.MEMBERS_INVITE)),
    db: Session = Depends(get_db),
) -> InvitationOut:
    invitation = InvitationService(db).create(ctx, email=payload.email, role=payload.role)
    return InvitationOut.build(invitation)


@router.post("/{organization_id}/invitations/{invitation_id}/resend", response_model=InvitationOut)
def resend_invitation(
    invitation_id: uuid.UUID,
    ctx: OrgContext = Depends(require_permission(Permission.MEMBERS_INVITE)),
    db: Session = Depends(get_db),
) -> InvitationOut:
    return InvitationOut.build(InvitationService(db).resend(ctx, invitation_id))


@router.delete("/{organization_id}/invitations/{invitation_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_invitation(
    invitation_id: uuid.UUID,
    ctx: OrgContext = Depends(require_permission(Permission.MEMBERS_INVITE)),
    db: Session = Depends(get_db),
) -> Response:
    InvitationService(db).revoke(ctx, invitation_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ------------------------------------------------------- invitations, by token


@invitation_router.get(
    "/{token}",
    response_model=InvitationPreviewOut,
    dependencies=[Depends(rate_limit("invitation-preview", 60, 60))],
)
def preview_invitation(token: str, db: Session = Depends(get_db)) -> InvitationPreviewOut:
    """Public: what the holder of an invitation link is being invited to."""
    return InvitationPreviewOut.model_validate(InvitationService(db).preview(token))


@invitation_router.post("/{token}/accept", response_model=OrganizationOut)
def accept_invitation(
    token: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    meta: RequestMeta = Depends(get_request_meta),
) -> OrganizationOut:
    organization, membership = InvitationService(db).accept(user, token, meta)
    return OrganizationOut.build(organization, membership)


# --------------------------------------------------------------------------- meta


def _options(choices: dict[str, str]) -> list[Option]:
    return [Option(value=value, label=label) for value, label in choices.items()]


@meta_router.get("/options", response_model=OptionsOut)
def get_options() -> OptionsOut:
    """Reference lists used by forms. Public: contains no tenant data."""
    return OptionsOut(
        industries=_options(INDUSTRIES),
        company_sizes=_options(COMPANY_SIZES),
        document_categories=_options(DOCUMENT_CATEGORIES),
        roles=[
            Option(value=role.value, label=ROLE_LABELS[role], description=ROLE_DESCRIPTIONS[role])
            for role in Role
        ],
    )


# ------------------------------------------------------------------ deletion


@router.post("/{organization_id}/deletion", response_model=OrganizationOut)
def schedule_deletion(
    payload: DeletionRequest,
    ctx: OrgContext = Depends(require_permission(Permission.ORG_DELETE)),
    db: Session = Depends(get_db),
) -> OrganizationOut:
    """Erase the organization after the grace period. Owners and admins are emailed; an owner can cancel."""
    from app.services.data_lifecycle import OrganizationDeletionService

    organization = OrganizationDeletionService(db).schedule(ctx, confirm_name=payload.confirm_name)
    return OrganizationOut.build(organization, ctx.membership)


@router.delete("/{organization_id}/deletion", response_model=OrganizationOut)
def cancel_deletion(
    ctx: OrgContext = Depends(require_permission(Permission.ORG_DELETE)), db: Session = Depends(get_db)
) -> OrganizationOut:
    from app.services.data_lifecycle import OrganizationDeletionService

    organization = OrganizationDeletionService(db).cancel(ctx)
    return OrganizationOut.build(organization, ctx.membership)
