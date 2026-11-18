"""FastAPI dependencies: authentication, organization context and permissions."""

import uuid

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.errors import AuthenticationError, NotFoundError, PermissionDeniedError
from app.core.permissions import Permission
from app.core.rate_limit import client_ip
from app.core.security import decode_access_token
from app.core.tenancy import set_rls_user, set_tenant
from app.core.time import utcnow
from app.models import User
from app.repositories.organizations import MembershipRepository, OrganizationRepository
from app.repositories.users import RefreshTokenRepository, UserRepository
from app.services.context import OrgContext, RequestMeta

bearer_scheme = HTTPBearer(auto_error=False)


def get_request_meta(request: Request) -> RequestMeta:
    return RequestMeta(ip_address=client_ip(request), user_agent=request.headers.get("user-agent"))


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None:
        raise AuthenticationError()
    claims = decode_access_token(credentials.credentials)
    if claims is None:
        raise AuthenticationError("Your session has expired.", code="invalid_token")

    user = UserRepository(db).get(claims.user_id)
    if user is None or not user.is_active:
        raise AuthenticationError("Your session has expired.", code="invalid_token")

    # Logout and password reset revoke the session family; honour that at once
    # instead of waiting for the access token to expire.
    if not RefreshTokenRepository(db).family_is_active(claims.session_family_id, utcnow()):
        raise AuthenticationError("Your session has ended. Sign in again.", code="session_revoked")

    request.state.session_family_id = claims.session_family_id
    set_rls_user(db, user.id)
    return user


def get_session_family_id(request: Request, _user: User = Depends(get_current_user)) -> uuid.UUID:
    return request.state.session_family_id


def _deleted(organization) -> bool:
    """Past its deletion date: gone for everyone, even before the nightly job erases it."""
    scheduled = organization.deletion_scheduled_for
    return scheduled is not None and scheduled <= utcnow()


def get_org_context(
    organization_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    meta: RequestMeta = Depends(get_request_meta),
) -> OrgContext:
    """Resolve the caller's membership in the organization named in the URL.

    A non-member gets the same 404 as for an organization that does not exist.
    """
    membership = MembershipRepository(db, organization_id).get_by_user(user.id)
    organization = OrganizationRepository(db).get(organization_id) if membership else None
    if membership is None or organization is None or _deleted(organization):
        raise NotFoundError("This organization was not found.")

    set_tenant(db, organization.id)
    return OrgContext(organization=organization, membership=membership, user=user, meta=meta)


def require_permission(permission: Permission):
    def dependency(ctx: OrgContext = Depends(get_org_context)) -> OrgContext:
        ctx.require(permission)
        return ctx

    return dependency


def require_client_header(request: Request) -> None:
    """CSRF guard for endpoints authenticated by the refresh cookie.

    A custom header cannot be sent by a cross-site form and forces a CORS
    preflight for cross-origin scripts.
    """
    if not request.headers.get("x-requested-with"):
        raise PermissionDeniedError("This request is missing a required header.", code="csrf_check_failed")
