"""Authentication endpoints."""

import uuid

from fastapi import APIRouter, Cookie, Depends, Response, status
from sqlalchemy.orm import Session

from app.api.deps import (
    get_current_user,
    get_request_meta,
    get_session_family_id,
    require_client_header,
)
from app.core.config import get_settings
from app.core.database import get_db
from app.core.rate_limit import enforce, rate_limit
from app.core.time import utcnow
from app.models import User
from app.schemas.auth import (
    ChangePasswordRequest,
    DeleteAccountRequest,
    ForgotPasswordRequest,
    LoginRequest,
    RegisterRequest,
    ResetPasswordRequest,
    TokenResponse,
    UpdateProfileRequest,
    UserOut,
    VerifyEmailRequest,
)
from app.schemas.common import Message
from app.schemas.organizations import MeResponse, OrganizationOut
from app.services.auth import AuthService, AuthSession
from app.services.context import RequestMeta
from app.services.organizations import OrganizationService

router = APIRouter(prefix="/auth", tags=["auth"])

REFRESH_COOKIE = "novalyn_refresh"


def _cookie_path() -> str:
    return f"{get_settings().api_v1_prefix}/auth"


def _set_refresh_cookie(response: Response, session: AuthSession) -> None:
    settings = get_settings()
    response.set_cookie(
        REFRESH_COOKIE,
        session.refresh_token,
        max_age=max(0, int((session.refresh_expires_at - utcnow()).total_seconds())),
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        domain=settings.cookie_domain,
        path=_cookie_path(),
    )


def _clear_refresh_cookie(response: Response) -> None:
    settings = get_settings()
    response.delete_cookie(
        REFRESH_COOKIE,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        domain=settings.cookie_domain,
        path=_cookie_path(),
    )


def _token_response(response: Response, session: AuthSession) -> TokenResponse:
    _set_refresh_cookie(response, session)
    return TokenResponse(
        access_token=session.access_token,
        expires_in=session.expires_in,
        user=UserOut.model_validate(session.user),
    )


@router.post(
    "/register",
    response_model=TokenResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limit("register", 10, 3600))],
)
def register(
    payload: RegisterRequest,
    response: Response,
    db: Session = Depends(get_db),
    meta: RequestMeta = Depends(get_request_meta),
) -> TokenResponse:
    session = AuthService(db).register(
        email=payload.email, password=payload.password, full_name=payload.full_name, meta=meta
    )
    return _token_response(response, session)


@router.post(
    "/login",
    response_model=TokenResponse,
    dependencies=[Depends(rate_limit("login", 20, 60))],
)
def login(
    payload: LoginRequest,
    response: Response,
    db: Session = Depends(get_db),
    meta: RequestMeta = Depends(get_request_meta),
) -> TokenResponse:
    session = AuthService(db).login(email=payload.email, password=payload.password, meta=meta)
    return _token_response(response, session)


@router.post(
    "/refresh",
    response_model=TokenResponse,
    dependencies=[Depends(require_client_header), Depends(rate_limit("refresh", 120, 60))],
)
def refresh(
    response: Response,
    refresh_token: str | None = Cookie(default=None, alias=REFRESH_COOKIE),
    db: Session = Depends(get_db),
    meta: RequestMeta = Depends(get_request_meta),
) -> TokenResponse:
    session = AuthService(db).refresh(refresh_token, meta)
    return _token_response(response, session)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_client_header)],
)
def logout(
    refresh_token: str | None = Cookie(default=None, alias=REFRESH_COOKIE),
    db: Session = Depends(get_db),
) -> Response:
    AuthService(db).logout(refresh_token)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    _clear_refresh_cookie(response)
    return response


@router.post("/logout-all", status_code=status.HTTP_204_NO_CONTENT)
def logout_all(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> Response:
    """End every session of the current user, on every device."""
    AuthService(db).logout_all(user)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    _clear_refresh_cookie(response)
    return response


@router.get("/me", response_model=MeResponse)
def me(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> MeResponse:
    organizations = OrganizationService(db).list_for_user(user)
    return MeResponse(
        user=UserOut.model_validate(user),
        organizations=[OrganizationOut.build(org, membership) for org, membership in organizations],
    )


@router.patch("/me", response_model=UserOut)
def update_me(
    payload: UpdateProfileRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UserOut:
    return UserOut.model_validate(AuthService(db).update_profile(user, full_name=payload.full_name))


@router.post(
    "/verify-email",
    response_model=UserOut,
    dependencies=[Depends(rate_limit("verify-email", 30, 3600))],
)
def verify_email(payload: VerifyEmailRequest, db: Session = Depends(get_db)) -> UserOut:
    return UserOut.model_validate(AuthService(db).verify_email(payload.token))


@router.post("/resend-verification", response_model=Message, status_code=status.HTTP_202_ACCEPTED)
def resend_verification(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> Message:
    enforce(f"resend-verification:{user.id}", 3, 3600)
    AuthService(db).resend_verification(user)
    return Message(message="A new confirmation email is on its way.")


@router.post(
    "/forgot-password",
    response_model=Message,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(rate_limit("forgot-password", 5, 3600))],
)
def forgot_password(payload: ForgotPasswordRequest, db: Session = Depends(get_db)) -> Message:
    AuthService(db).request_password_reset(payload.email)
    # The same answer whether or not the address has an account.
    return Message(message="If an account exists for this address, a reset link has been sent.")


@router.post(
    "/reset-password",
    response_model=Message,
    dependencies=[Depends(rate_limit("reset-password", 10, 3600))],
)
def reset_password(payload: ResetPasswordRequest, db: Session = Depends(get_db)) -> Message:
    AuthService(db).reset_password(raw_token=payload.token, new_password=payload.password)
    return Message(message="Your password has been changed. Sign in with the new password.")


@router.post("/change-password", response_model=Message)
def change_password(
    payload: ChangePasswordRequest,
    user: User = Depends(get_current_user),
    family_id: uuid.UUID = Depends(get_session_family_id),
    db: Session = Depends(get_db),
) -> Message:
    enforce(f"change-password:{user.id}", 10, 3600)
    AuthService(db).change_password(
        user,
        current_password=payload.current_password,
        new_password=payload.new_password,
        current_family_id=family_id,
    )
    return Message(message="Your password has been changed. Other devices have been signed out.")


@router.delete("/me", status_code=status.HTTP_204_NO_CONTENT)
def delete_account(
    payload: DeleteAccountRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    meta: RequestMeta = Depends(get_request_meta),
) -> Response:
    """Delete the signed-in person's account for good. Needs the password."""
    from app.services.data_lifecycle import AccountDeletionService

    enforce(f"delete-account:{user.id}", 5, 900)
    AccountDeletionService(db).delete(user, password=payload.password, meta=meta)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    _clear_refresh_cookie(response)
    return response
