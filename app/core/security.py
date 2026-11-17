"""Security primitives: password hashing, access tokens and opaque tokens."""

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.core.config import get_settings
from app.core.time import utcnow

JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_TYPE = "access"
FILE_TOKEN_TYPE = "file"

_password_hasher = PasswordHasher()

# Verified against when the account does not exist, so that a login attempt takes
# the same time whether or not the email is registered.
_DUMMY_HASH = _password_hasher.hash(secrets.token_urlsafe(16))


def hash_password(password: str) -> str:
    return _password_hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    try:
        return _password_hasher.verify(password_hash or _DUMMY_HASH, password) and bool(password_hash)
    except (VerificationError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    return _password_hasher.check_needs_rehash(password_hash)


def generate_token() -> str:
    """A 256-bit random token, safe to put in a URL."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """Tokens are high-entropy, so a plain SHA-256 is enough for storage."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AccessTokenClaims:
    user_id: uuid.UUID
    session_family_id: uuid.UUID
    expires_at: datetime


def create_access_token(user_id: uuid.UUID, session_family_id: uuid.UUID) -> tuple[str, int]:
    """Returns the encoded token and its lifetime in seconds."""
    settings = get_settings()
    now = utcnow()
    ttl = timedelta(minutes=settings.access_token_ttl_minutes)
    payload = {
        "sub": str(user_id),
        "sid": str(session_family_id),
        "typ": ACCESS_TOKEN_TYPE,
        "iat": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
    }
    token = jwt.encode(payload, settings.secret_key, algorithm=JWT_ALGORITHM)
    return token, int(ttl.total_seconds())


def decode_access_token(token: str) -> AccessTokenClaims | None:
    """Returns the claims, or None when the token is invalid or expired."""
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.secret_key,
            algorithms=[JWT_ALGORITHM],
            options={"require": ["sub", "sid", "exp", "typ"]},
        )
        if payload.get("typ") != ACCESS_TOKEN_TYPE:
            return None
        return AccessTokenClaims(
            user_id=uuid.UUID(payload["sub"]),
            session_family_id=uuid.UUID(payload["sid"]),
            expires_at=datetime.fromtimestamp(payload["exp"], tz=UTC),
        )
    except (jwt.PyJWTError, ValueError, KeyError):
        return None


@dataclass(frozen=True)
class FileTokenClaims:
    organization_id: uuid.UUID
    version_id: uuid.UUID
    inline: bool


def create_file_token(organization_id: uuid.UUID, version_id: uuid.UUID, *, inline: bool) -> tuple[str, int]:
    """A short-lived signed token that lets its holder fetch one file version.

    It is issued only after a permission check and is the whole credential for
    the download, so that a browser can open the file in a tab or an <iframe>.
    """
    settings = get_settings()
    ttl = settings.file_url_ttl_seconds
    payload = {
        "org": str(organization_id),
        "ver": str(version_id),
        "inl": inline,
        "typ": FILE_TOKEN_TYPE,
        "exp": int(utcnow().timestamp()) + ttl,
    }
    return jwt.encode(payload, settings.secret_key, algorithm=JWT_ALGORITHM), ttl


def decode_file_token(token: str) -> FileTokenClaims | None:
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.secret_key,
            algorithms=[JWT_ALGORITHM],
            options={"require": ["org", "ver", "exp", "typ"]},
        )
        if payload.get("typ") != FILE_TOKEN_TYPE:
            return None
        return FileTokenClaims(
            organization_id=uuid.UUID(payload["org"]),
            version_id=uuid.UUID(payload["ver"]),
            inline=bool(payload.get("inl", False)),
        )
    except (jwt.PyJWTError, ValueError, KeyError):
        return None
