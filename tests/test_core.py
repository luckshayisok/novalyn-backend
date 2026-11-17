"""Unit tests for core building blocks: permissions, security, configuration, rate limiting."""

import time
import uuid

import fakeredis
import jwt
import pytest
from pydantic import ValidationError

from app.core import security
from app.core.config import DEV_SECRET_KEY, Settings, get_settings
from app.core.permissions import (
    ROLE_PERMISSIONS,
    ROLE_RANK,
    Permission,
    Role,
    assignable_roles,
    can_manage_member,
    has_permission,
    invitable_roles,
)
from app.core.rate_limit import MemoryRateLimiter, RedisRateLimiter

# ------------------------------------------------------------------ permissions


def test_each_role_includes_everything_the_role_below_it_can_do():
    ordered = sorted(Role, key=lambda role: ROLE_RANK[role])

    for lower, higher in zip(ordered, ordered[1:], strict=False):
        assert ROLE_PERMISSIONS[lower] < ROLE_PERMISSIONS[higher], (lower, higher)


def test_viewer_is_read_only():
    assert all(permission.value.endswith(":read") for permission in ROLE_PERMISSIONS[Role.VIEWER])


def test_sensitive_permissions_are_limited_to_the_right_roles():
    assert [r for r in Role if has_permission(r, Permission.BILLING_MANAGE)] == [Role.OWNER]
    assert [r for r in Role if has_permission(r, Permission.ORG_DELETE)] == [Role.OWNER]
    assert {r for r in Role if has_permission(r, Permission.MEMBERS_MANAGE)} == {Role.OWNER, Role.ADMIN}
    assert {r for r in Role if has_permission(r, Permission.DOCUMENTS_VERIFY)} == {
        Role.OWNER,
        Role.ADMIN,
        Role.COMPLIANCE_MANAGER,
    }


def test_role_assignment_rules():
    assert assignable_roles(Role.OWNER) == list(Role)
    assert assignable_roles(Role.ADMIN) == [Role.COMPLIANCE_MANAGER, Role.MEMBER, Role.VIEWER]
    assert assignable_roles(Role.COMPLIANCE_MANAGER) == []
    assert Role.OWNER not in invitable_roles(Role.OWNER)

    assert can_manage_member(Role.OWNER, Role.OWNER)
    assert can_manage_member(Role.ADMIN, Role.MEMBER)
    assert not can_manage_member(Role.ADMIN, Role.ADMIN)
    assert not can_manage_member(Role.ADMIN, Role.OWNER)
    assert not can_manage_member(Role.MEMBER, Role.VIEWER)


# --------------------------------------------------------------------- security


def test_password_hashing_round_trip():
    hashed = security.hash_password("Harbor-Lantern-4821")

    assert hashed.startswith("$argon2id$")
    assert security.verify_password("Harbor-Lantern-4821", hashed)
    assert not security.verify_password("harbor-lantern-4821", hashed)
    assert not security.verify_password("anything", None)
    assert not security.verify_password("anything", "not-a-hash")
    assert security.hash_password("Harbor-Lantern-4821") != hashed  # salted


def test_opaque_tokens_are_random_and_hashed_deterministically():
    first, second = security.generate_token(), security.generate_token()

    assert first != second
    assert len(first) >= 43
    assert security.hash_token(first) == security.hash_token(first)
    assert len(security.hash_token(first)) == 64
    assert security.hash_token(first) != first


def test_access_token_round_trip():
    user_id, family_id = uuid.uuid4(), uuid.uuid4()

    token, expires_in = security.create_access_token(user_id, family_id)
    claims = security.decode_access_token(token)

    assert expires_in == get_settings().access_token_ttl_minutes * 60
    assert claims.user_id == user_id
    assert claims.session_family_id == family_id


def test_access_token_is_rejected_when_tampered_expired_or_of_another_type():
    settings = get_settings()
    now = int(time.time())
    base = {"sub": str(uuid.uuid4()), "sid": str(uuid.uuid4()), "typ": "access", "iat": now, "exp": now + 60}

    def encode(payload, key=settings.secret_key, algorithm="HS256"):
        return jwt.encode(payload, key, algorithm=algorithm)

    assert security.decode_access_token(encode(base)) is not None
    assert security.decode_access_token(encode(base, key="another-key-another-key-another-key-0")) is None
    assert security.decode_access_token(encode({**base, "exp": now - 1})) is None
    assert security.decode_access_token(encode({**base, "typ": "refresh"})) is None
    assert security.decode_access_token(encode({**base, "sub": "not-a-uuid"})) is None
    assert security.decode_access_token(encode({k: v for k, v in base.items() if k != "sid"})) is None
    assert security.decode_access_token("garbage") is None
    unsigned = jwt.encode(base, key=None, algorithm="none")
    assert security.decode_access_token(unsigned) is None


# ---------------------------------------------------------------- configuration


def _production(**overrides) -> Settings:
    values = {
        "environment": "production",
        "secret_key": "p" * 48,
        "database_url": "postgresql://novalyn:pw@db:5432/novalyn",
        "cookie_secure": True,
        "celery_task_always_eager": False,
        "redis_url": "redis://redis:6379/0",
        "email_backend": "smtp",
        "smtp_host": "smtp.example.com",
        "cors_origins": "https://app.example.com",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_production_configuration_is_accepted_when_complete():
    settings = _production()

    assert settings.is_production
    assert settings.database_url.startswith("postgresql+psycopg://")
    assert settings.cors_origin_list == ["https://app.example.com"]


@pytest.mark.parametrize(
    ("override", "fragment"),
    [
        ({"secret_key": DEV_SECRET_KEY}, "SECRET_KEY"),
        ({"secret_key": "too-short"}, "SECRET_KEY"),
        ({"database_url": "sqlite:///./x.db"}, "DATABASE_URL"),
        ({"cookie_secure": False}, "COOKIE_SECURE"),
        ({"celery_task_always_eager": True}, "CELERY_TASK_ALWAYS_EAGER"),
        ({"redis_url": None}, "REDIS_URL"),
        ({"email_backend": "file"}, "EMAIL_BACKEND"),
        ({"cors_origins": "*"}, "CORS_ORIGINS"),
    ],
)
def test_production_refuses_to_start_with_unsafe_settings(override, fragment):
    with pytest.raises(ValidationError, match=fragment):
        _production(**override)


def test_development_defaults_need_no_configuration():
    settings = Settings(_env_file=None, environment="development")

    assert settings.is_sqlite
    assert settings.celery_task_always_eager


# ---------------------------------------------------------------- rate limiting


@pytest.fixture(params=["memory", "redis"])
def limiter(request):
    if request.param == "memory":
        return MemoryRateLimiter()
    return RedisRateLimiter(fakeredis.FakeRedis())


def test_limiter_allows_up_to_the_limit_then_blocks(limiter):
    results = [limiter.hit("key", 3, 60) for _ in range(4)]

    assert [result.allowed for result in results] == [True, True, True, False]
    assert 0 < results[-1].retry_after <= 60


def test_limiter_keys_are_independent_and_can_be_reset(limiter):
    for _ in range(3):
        limiter.hit("a", 2, 60)

    assert not limiter.is_blocked("a", 2).allowed
    assert limiter.is_blocked("b", 2).allowed
    assert limiter.hit("b", 2, 60).allowed

    limiter.reset("a")
    assert limiter.is_blocked("a", 2).allowed
    assert limiter.hit("a", 2, 60).allowed


def test_memory_limiter_window_expires():
    limiter = MemoryRateLimiter()
    assert limiter.hit("key", 1, 1).allowed
    assert not limiter.hit("key", 1, 1).allowed

    time.sleep(1.1)

    assert limiter.hit("key", 1, 1).allowed
