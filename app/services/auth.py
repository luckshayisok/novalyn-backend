"""Authentication: accounts, sessions, email verification and password reset."""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import (
    AppError,
    AuthenticationError,
    ConflictError,
    RateLimitedError,
    ValidationFailedError,
)
from app.core.rate_limit import get_rate_limiter
from app.core.security import (
    create_access_token,
    generate_token,
    hash_password,
    hash_token,
    password_needs_rehash,
    verify_password,
)
from app.core.time import utcnow
from app.integrations.email import templates
from app.models import RefreshToken, User, UserToken, UserTokenPurpose
from app.repositories.users import RefreshTokenRepository, UserRepository, UserTokenRepository
from app.services.context import RequestMeta
from app.workers.notification_tasks import enqueue_email

logger = logging.getLogger(__name__)

PASSWORD_MAX_LENGTH = 128
LOGIN_MAX_FAILURES = 5
LOGIN_FAILURE_WINDOW_SECONDS = 15 * 60


@dataclass(frozen=True)
class AuthSession:
    user: User
    access_token: str
    expires_in: int
    refresh_token: str
    refresh_expires_at: datetime


class InvalidTokenError(AppError):
    code = "invalid_token"
    message = "This link is invalid or has expired."


def normalize_email(email: str) -> str:
    return email.strip().lower()


def validate_password(password: str, *, email: str) -> None:
    settings = get_settings()
    problem: str | None = None
    if len(password) < settings.password_min_length:
        problem = f"Use at least {settings.password_min_length} characters."
    elif len(password) > PASSWORD_MAX_LENGTH:
        problem = f"Use at most {PASSWORD_MAX_LENGTH} characters."
    elif password.lower() == email.lower() or password.lower() == email.split("@")[0].lower():
        problem = "The password must not be your email address."
    elif len(set(password)) < 4:
        problem = "The password is too simple."
    if problem:
        raise ValidationFailedError(
            problem, code="weak_password", details=[{"field": "password", "message": problem}]
        )


class AuthService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.settings = get_settings()
        self.users = UserRepository(db)
        self.refresh_tokens = RefreshTokenRepository(db)
        self.user_tokens = UserTokenRepository(db)

    # ------------------------------------------------------------------ accounts

    def register(self, *, email: str, password: str, full_name: str, meta: RequestMeta) -> AuthSession:
        email = normalize_email(email)
        validate_password(password, email=email)
        if self.users.get_by_email(email) is not None:
            raise ConflictError("An account with this email already exists.", code="email_taken")

        now = utcnow()
        user = User(
            email=email,
            password_hash=hash_password(password),
            full_name=full_name.strip(),
            last_login_at=now,
        )
        self.users.add(user)
        try:
            self.db.flush()
        except IntegrityError as exc:
            self.db.rollback()
            raise ConflictError("An account with this email already exists.", code="email_taken") from exc

        verification_token = self._create_user_token(
            user,
            UserTokenPurpose.EMAIL_VERIFICATION,
            timedelta(hours=self.settings.email_verification_ttl_hours),
        )
        session = self._start_session(user, meta)
        self.db.commit()

        self._send_verification_email(user, verification_token)
        return session

    def update_profile(self, user: User, *, full_name: str) -> User:
        user.full_name = full_name.strip()
        self.db.commit()
        return user

    # ------------------------------------------------------------------ sessions

    def login(self, *, email: str, password: str, meta: RequestMeta) -> AuthSession:
        email = normalize_email(email)
        limiter = get_rate_limiter()
        throttle_key = f"login-fail:{hash_token(email)[:32]}"

        if self.settings.rate_limit_enabled:
            state = limiter.is_blocked(throttle_key, LOGIN_MAX_FAILURES)
            if not state.allowed:
                raise RateLimitedError(
                    "Too many failed sign-in attempts. Try again later or reset your password.",
                    headers={"Retry-After": str(state.retry_after)},
                )

        user = self.users.get_by_email(email)
        password_ok = verify_password(password, user.password_hash if user else None)
        if user is None or not password_ok or not user.is_active:
            if self.settings.rate_limit_enabled:
                limiter.hit(throttle_key, LOGIN_MAX_FAILURES, LOGIN_FAILURE_WINDOW_SECONDS)
            raise AuthenticationError("Incorrect email or password.", code="invalid_credentials")

        limiter.reset(throttle_key)
        if password_needs_rehash(user.password_hash):
            user.password_hash = hash_password(password)
        user.last_login_at = utcnow()
        session = self._start_session(user, meta)
        self.db.commit()
        return session

    def refresh(self, raw_refresh_token: str | None, meta: RequestMeta) -> AuthSession:
        invalid = AuthenticationError(
            "Your session has expired. Sign in again.", code="invalid_refresh_token"
        )
        if not raw_refresh_token:
            raise invalid

        token = self.refresh_tokens.get_by_hash(hash_token(raw_refresh_token))
        now = utcnow()
        if token is None or token.revoked_at is not None or token.expires_at <= now:
            raise invalid

        if token.rotated_at is not None:
            grace = timedelta(seconds=self.settings.refresh_reuse_grace_seconds)
            if now - token.rotated_at > grace:
                # A token that was already exchanged is being presented again:
                # treat the session as stolen and end it everywhere.
                self.refresh_tokens.revoke_family(token.family_id, now)
                self.db.commit()
                logger.warning(
                    "Refresh token reuse detected; session revoked",
                    extra={"user_id": str(token.user_id), "family_id": str(token.family_id)},
                )
                raise invalid
            # Inside the grace window this is a concurrent refresh (two tabs).
        else:
            token.rotated_at = now

        user = self.users.get(token.user_id)
        if user is None or not user.is_active:
            raise invalid

        refresh_token, expires_at = self._issue_refresh_token(user, token.family_id, meta)
        access_token, expires_in = create_access_token(user.id, token.family_id)
        self.db.commit()
        return AuthSession(user, access_token, expires_in, refresh_token, expires_at)

    def logout(self, raw_refresh_token: str | None) -> None:
        if not raw_refresh_token:
            return
        token = self.refresh_tokens.get_by_hash(hash_token(raw_refresh_token))
        if token is None:
            return
        self.refresh_tokens.revoke_family(token.family_id, utcnow())
        self.db.commit()

    def logout_all(self, user: User) -> None:
        self.refresh_tokens.revoke_all_for_user(user.id, utcnow())
        self.db.commit()

    # -------------------------------------------------------- email verification

    def verify_email(self, raw_token: str) -> User:
        token = self.user_tokens.get_by_hash(hash_token(raw_token), UserTokenPurpose.EMAIL_VERIFICATION)
        if token is None:
            raise InvalidTokenError()
        user = self.users.get(token.user_id)
        if user is None:
            raise InvalidTokenError()
        if token.used_at is not None:
            # Opening the link twice is fine once the address is confirmed.
            if user.is_email_verified:
                return user
            raise InvalidTokenError()
        now = utcnow()
        if token.expires_at <= now:
            raise InvalidTokenError()

        token.used_at = now
        if user.email_verified_at is None:
            user.email_verified_at = now
        self.db.commit()
        return user

    def resend_verification(self, user: User) -> None:
        if user.is_email_verified:
            raise ConflictError("This email address is already confirmed.", code="already_verified")
        self.user_tokens.invalidate_unused(user.id, UserTokenPurpose.EMAIL_VERIFICATION, utcnow())
        raw_token = self._create_user_token(
            user,
            UserTokenPurpose.EMAIL_VERIFICATION,
            timedelta(hours=self.settings.email_verification_ttl_hours),
        )
        self.db.commit()
        self._send_verification_email(user, raw_token)

    # ------------------------------------------------------------ password reset

    def request_password_reset(self, email: str) -> None:
        """Always succeeds from the caller's point of view, whether or not the account exists."""
        user = self.users.get_by_email(normalize_email(email))
        if user is None or not user.is_active:
            return
        self.user_tokens.invalidate_unused(user.id, UserTokenPurpose.PASSWORD_RESET, utcnow())
        raw_token = self._create_user_token(
            user,
            UserTokenPurpose.PASSWORD_RESET,
            timedelta(minutes=self.settings.password_reset_ttl_minutes),
        )
        self.db.commit()
        enqueue_email(
            templates.password_reset_email(
                to=user.email,
                full_name=user.full_name,
                url=f"{self.settings.frontend_url}/reset-password?token={raw_token}",
                expires_minutes=self.settings.password_reset_ttl_minutes,
            )
        )

    def reset_password(self, *, raw_token: str, new_password: str) -> None:
        token = self.user_tokens.get_by_hash(hash_token(raw_token), UserTokenPurpose.PASSWORD_RESET)
        now = utcnow()
        if token is None or token.used_at is not None or token.expires_at <= now:
            raise InvalidTokenError()
        user = self.users.get(token.user_id)
        if user is None or not user.is_active:
            raise InvalidTokenError()

        # Validate before consuming the token, so a rejected password can be retried.
        validate_password(new_password, email=user.email)

        token.used_at = now
        user.password_hash = hash_password(new_password)
        if user.email_verified_at is None:
            # The link was delivered to this address.
            user.email_verified_at = now
        self.refresh_tokens.revoke_all_for_user(user.id, now)
        self.db.commit()

    def change_password(
        self,
        user: User,
        *,
        current_password: str,
        new_password: str,
        current_family_id: uuid.UUID | None,
    ) -> None:
        if not verify_password(current_password, user.password_hash):
            raise ValidationFailedError(
                "The current password is not correct.",
                code="incorrect_password",
                details=[{"field": "current_password", "message": "The current password is not correct."}],
            )
        validate_password(new_password, email=user.email)
        user.password_hash = hash_password(new_password)
        self.refresh_tokens.revoke_all_for_user(user.id, utcnow(), except_family_id=current_family_id)
        self.db.commit()

    # ------------------------------------------------------------------- helpers

    def _start_session(self, user: User, meta: RequestMeta) -> AuthSession:
        family_id = uuid.uuid4()
        refresh_token, expires_at = self._issue_refresh_token(user, family_id, meta)
        access_token, expires_in = create_access_token(user.id, family_id)
        return AuthSession(user, access_token, expires_in, refresh_token, expires_at)

    def _issue_refresh_token(
        self, user: User, family_id: uuid.UUID, meta: RequestMeta
    ) -> tuple[str, datetime]:
        raw_token = generate_token()
        expires_at = utcnow() + timedelta(days=self.settings.refresh_token_ttl_days)
        self.refresh_tokens.add(
            RefreshToken(
                user_id=user.id,
                family_id=family_id,
                token_hash=hash_token(raw_token),
                expires_at=expires_at,
                user_agent=(meta.user_agent or "")[:400] or None,
                ip_address=meta.ip_address,
            )
        )
        return raw_token, expires_at

    def _create_user_token(self, user: User, purpose: str, ttl: timedelta) -> str:
        raw_token = generate_token()
        self.user_tokens.add(
            UserToken(
                user_id=user.id,
                purpose=purpose,
                token_hash=hash_token(raw_token),
                expires_at=utcnow() + ttl,
            )
        )
        return raw_token

    def _send_verification_email(self, user: User, raw_token: str) -> None:
        enqueue_email(
            templates.verification_email(
                to=user.email,
                full_name=user.full_name,
                url=f"{self.settings.frontend_url}/verify-email?token={raw_token}",
                expires_hours=self.settings.email_verification_ttl_hours,
            )
        )
