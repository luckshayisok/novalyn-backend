"""Authentication: registration, sessions, email verification, password reset."""

from sqlalchemy import select

from app.core.config import get_settings
from app.models import User
from tests.helpers import (
    API,
    NEW_PASSWORD,
    PASSWORD,
    emails_to,
    error_code,
    last_email,
    link_token,
)

EMAIL = "ada@example.com"
COOKIE = "novalyn_refresh"


def _register(client, email=EMAIL, password=PASSWORD, full_name="Ada Lovelace"):
    return client.post(
        f"{API}/auth/register", json={"email": email, "password": password, "full_name": full_name}
    )


def _login(client, email=EMAIL, password=PASSWORD):
    return client.post(f"{API}/auth/login", json={"email": email, "password": password})


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


# ------------------------------------------------------------------ registration


def test_register_creates_account_session_and_verification_email(client):
    response = _register(client)

    assert response.status_code == 201
    body = response.json()
    assert body["user"]["email"] == EMAIL
    assert body["user"]["is_email_verified"] is False
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == get_settings().access_token_ttl_minutes * 60

    set_cookie = response.headers["set-cookie"]
    assert COOKIE in set_cookie
    assert "HttpOnly" in set_cookie
    assert "Path=/api/v1/auth" in set_cookie
    assert "samesite=lax" in set_cookie.lower()

    messages = emails_to(EMAIL)
    assert len(messages) == 1
    assert "NOVALYN" in messages[0].subject
    assert "/verify-email?token=" in messages[0].text


def test_register_stores_email_lower_case_and_password_hashed(client, db):
    assert _register(client, email="Ada@Example.COM").status_code == 201

    user = db.scalars(select(User)).one()
    assert user.email == "ada@example.com"
    assert user.password_hash != PASSWORD
    assert user.password_hash.startswith("$argon2id$")


def test_register_rejects_duplicate_email_case_insensitively(client, new_client):
    assert _register(client).status_code == 201

    response = _register(new_client(), email="ADA@example.com")

    assert response.status_code == 409
    assert error_code(response) == "email_taken"


def test_register_rejects_weak_password(client):
    response = _register(client, password="short")

    assert response.status_code == 422
    assert error_code(response) == "weak_password"
    assert response.json()["error"]["details"][0]["field"] == "password"


def test_register_rejects_password_equal_to_email(client):
    response = _register(client, password=EMAIL)

    assert response.status_code == 422
    assert error_code(response) == "weak_password"


def test_register_rejects_invalid_email(client):
    response = _register(client, email="not-an-email")

    assert response.status_code == 422
    assert error_code(response) == "validation_error"
    assert response.json()["error"]["details"][0]["field"] == "email"


# ------------------------------------------------------------------------ login


def test_login_returns_session(client, new_client):
    _register(client)

    response = _login(new_client())

    assert response.status_code == 200
    assert response.json()["user"]["email"] == EMAIL
    assert COOKIE in response.headers["set-cookie"]


def test_login_gives_the_same_error_for_wrong_password_and_unknown_account(client, new_client):
    _register(client)

    wrong_password = _login(new_client(), password="Wrong-Password-000")
    unknown_account = _login(new_client(), email="nobody@example.com")

    assert wrong_password.status_code == unknown_account.status_code == 401
    assert error_code(wrong_password) == error_code(unknown_account) == "invalid_credentials"
    assert wrong_password.json()["error"]["message"] == unknown_account.json()["error"]["message"]


def test_login_is_blocked_after_repeated_failures(client, new_client):
    _register(client)
    attacker = new_client()
    for _ in range(5):
        assert _login(attacker, password="Wrong-Password-000").status_code == 401

    response = _login(attacker)  # correct password, but the account is throttled

    assert response.status_code == 429
    assert error_code(response) == "rate_limited"
    assert int(response.headers["retry-after"]) > 0


def test_inactive_user_cannot_sign_in_or_use_an_existing_session(client, new_client, db):
    token = _register(client).json()["access_token"]
    user = db.scalars(select(User)).one()
    user.is_active = False
    db.commit()

    assert client.get(f"{API}/auth/me", headers=_bearer(token)).status_code == 401
    assert _login(new_client()).status_code == 401


# --------------------------------------------------------------------------- me


def test_me_requires_a_valid_access_token(client):
    assert client.get(f"{API}/auth/me").status_code == 401
    assert error_code(client.get(f"{API}/auth/me")) == "not_authenticated"

    response = client.get(f"{API}/auth/me", headers=_bearer("not-a-token"))
    assert response.status_code == 401
    assert error_code(response) == "invalid_token"


def test_me_returns_user_and_organizations(actor):
    ada = actor(EMAIL, "Ada Lovelace")
    organization = ada.create_org("Analytical Engines")

    body = ada.get("/auth/me").json()

    assert body["user"]["email"] == EMAIL
    assert body["user"]["is_email_verified"] is True
    assert [org["id"] for org in body["organizations"]] == [organization["id"]]
    assert body["organizations"][0]["role"] == "owner"


def test_update_profile(actor):
    ada = actor(EMAIL, "Ada")

    response = ada.patch("/auth/me", json={"full_name": "  Ada Lovelace  "})

    assert response.status_code == 200
    assert response.json()["full_name"] == "Ada Lovelace"
    assert ada.patch("/auth/me", json={"full_name": "   "}).status_code == 422


# ---------------------------------------------------------------------- refresh


def test_refresh_rotates_the_refresh_token(client):
    first = _register(client)
    first_cookie = first.cookies[COOKIE]

    second = client.post(f"{API}/auth/refresh")

    assert second.status_code == 200
    assert second.cookies[COOKIE] != first_cookie
    assert second.json()["access_token"]
    assert client.get(f"{API}/auth/me", headers=_bearer(second.json()["access_token"])).status_code == 200


def test_reusing_a_rotated_refresh_token_revokes_the_whole_session(client, new_client, monkeypatch):
    monkeypatch.setattr(get_settings(), "refresh_reuse_grace_seconds", -1)
    stolen_cookie = _register(client).cookies[COOKIE]
    rotated = client.post(f"{API}/auth/refresh")
    assert rotated.status_code == 200

    replay = new_client().post(f"{API}/auth/refresh", headers={"Cookie": f"{COOKIE}={stolen_cookie}"})

    assert replay.status_code == 401
    assert error_code(replay) == "invalid_refresh_token"
    # The legitimate client is signed out too: the session family is gone.
    assert client.post(f"{API}/auth/refresh").status_code == 401
    me = client.get(f"{API}/auth/me", headers=_bearer(rotated.json()["access_token"]))
    assert me.status_code == 401
    assert error_code(me) == "session_revoked"


def test_concurrent_refresh_inside_the_grace_window_is_allowed(client, new_client):
    cookie = _register(client).cookies[COOKIE]
    assert client.post(f"{API}/auth/refresh").status_code == 200

    second_tab = new_client().post(f"{API}/auth/refresh", headers={"Cookie": f"{COOKIE}={cookie}"})

    assert second_tab.status_code == 200
    assert client.post(f"{API}/auth/refresh").status_code == 200


def test_refresh_without_cookie_is_rejected(client):
    response = client.post(f"{API}/auth/refresh")

    assert response.status_code == 401
    assert error_code(response) == "invalid_refresh_token"


def test_refresh_requires_the_client_header(client):
    _register(client)

    response = client.post(f"{API}/auth/refresh", headers={"X-Requested-With": ""})

    assert response.status_code == 403
    assert error_code(response) == "csrf_check_failed"


# ----------------------------------------------------------------------- logout


def test_logout_ends_the_session_immediately(client):
    token = _register(client).json()["access_token"]

    response = client.post(f"{API}/auth/logout")

    assert response.status_code == 204
    assert client.post(f"{API}/auth/refresh").status_code == 401
    me = client.get(f"{API}/auth/me", headers=_bearer(token))
    assert me.status_code == 401
    assert error_code(me) == "session_revoked"


def test_logout_all_ends_sessions_on_other_devices(client, new_client):
    token = _register(client).json()["access_token"]
    other_device = new_client()
    other_token = _login(other_device).json()["access_token"]

    assert client.post(f"{API}/auth/logout-all", headers=_bearer(token)).status_code == 204

    assert other_device.post(f"{API}/auth/refresh").status_code == 401
    assert other_device.get(f"{API}/auth/me", headers=_bearer(other_token)).status_code == 401


# ----------------------------------------------------------- email verification


def test_verify_email_confirms_the_address_and_is_idempotent(client):
    token_response = _register(client).json()
    token = link_token(last_email(EMAIL))

    first = client.post(f"{API}/auth/verify-email", json={"token": token})
    again = client.post(f"{API}/auth/verify-email", json={"token": token})

    assert first.status_code == 200
    assert first.json()["is_email_verified"] is True
    assert again.status_code == 200
    me = client.get(f"{API}/auth/me", headers=_bearer(token_response["access_token"])).json()
    assert me["user"]["is_email_verified"] is True


def test_verify_email_rejects_unknown_and_expired_tokens(client, monkeypatch):
    unknown = client.post(f"{API}/auth/verify-email", json={"token": "x" * 43})
    assert unknown.status_code == 400
    assert error_code(unknown) == "invalid_token"

    monkeypatch.setattr(get_settings(), "email_verification_ttl_hours", -1)
    _register(client)
    expired = client.post(f"{API}/auth/verify-email", json={"token": link_token(last_email(EMAIL))})
    assert expired.status_code == 400
    assert error_code(expired) == "invalid_token"


def test_resend_verification_replaces_the_previous_link(client):
    access_token = _register(client).json()["access_token"]
    old_token = link_token(last_email(EMAIL))

    response = client.post(f"{API}/auth/resend-verification", headers=_bearer(access_token))

    assert response.status_code == 202
    new_token = link_token(last_email(EMAIL))
    assert new_token != old_token
    assert client.post(f"{API}/auth/verify-email", json={"token": old_token}).status_code == 400
    assert client.post(f"{API}/auth/verify-email", json={"token": new_token}).status_code == 200

    already = client.post(f"{API}/auth/resend-verification", headers=_bearer(access_token))
    assert already.status_code == 409
    assert error_code(already) == "already_verified"


# --------------------------------------------------------------- password reset


def test_forgot_password_answers_the_same_for_known_and_unknown_addresses(client):
    _register(client)

    known = client.post(f"{API}/auth/forgot-password", json={"email": EMAIL})
    unknown = client.post(f"{API}/auth/forgot-password", json={"email": "nobody@example.com"})

    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json()
    assert "/reset-password?token=" in last_email(EMAIL).text
    assert emails_to("nobody@example.com") == []


def test_reset_password_changes_the_password_and_ends_all_sessions(client, new_client):
    access_token = _register(client).json()["access_token"]
    client.post(f"{API}/auth/forgot-password", json={"email": EMAIL})
    token = link_token(last_email(EMAIL, "/reset-password"))

    response = new_client().post(
        f"{API}/auth/reset-password", json={"token": token, "password": NEW_PASSWORD}
    )

    assert response.status_code == 200
    assert _login(new_client(), password=PASSWORD).status_code == 401
    assert _login(new_client(), password=NEW_PASSWORD).status_code == 200
    # Existing sessions are gone.
    assert client.get(f"{API}/auth/me", headers=_bearer(access_token)).status_code == 401
    assert client.post(f"{API}/auth/refresh").status_code == 401
    # The link works once.
    reuse = new_client().post(f"{API}/auth/reset-password", json={"token": token, "password": PASSWORD})
    assert reuse.status_code == 400
    assert error_code(reuse) == "invalid_token"


def test_reset_password_with_a_weak_password_keeps_the_link_usable(client):
    _register(client)
    client.post(f"{API}/auth/forgot-password", json={"email": EMAIL})
    token = link_token(last_email(EMAIL, "/reset-password"))

    weak = client.post(f"{API}/auth/reset-password", json={"token": token, "password": "short"})
    strong = client.post(f"{API}/auth/reset-password", json={"token": token, "password": NEW_PASSWORD})

    assert weak.status_code == 422
    assert strong.status_code == 200


def test_requesting_a_new_reset_link_invalidates_the_previous_one(client):
    _register(client)
    client.post(f"{API}/auth/forgot-password", json={"email": EMAIL})
    first = link_token(last_email(EMAIL, "/reset-password"))
    client.post(f"{API}/auth/forgot-password", json={"email": EMAIL})

    response = client.post(f"{API}/auth/reset-password", json={"token": first, "password": NEW_PASSWORD})

    assert response.status_code == 400


def test_forgot_password_is_rate_limited(client):
    for _ in range(5):
        assert client.post(f"{API}/auth/forgot-password", json={"email": EMAIL}).status_code == 202

    response = client.post(f"{API}/auth/forgot-password", json={"email": EMAIL})

    assert response.status_code == 429
    assert "retry-after" in response.headers


# -------------------------------------------------------------- change password


def test_change_password_requires_the_current_password(client):
    access_token = _register(client).json()["access_token"]

    response = client.post(
        f"{API}/auth/change-password",
        headers=_bearer(access_token),
        json={"current_password": "Wrong-Password-000", "new_password": NEW_PASSWORD},
    )

    assert response.status_code == 422
    assert error_code(response) == "incorrect_password"


def test_change_password_keeps_this_session_and_ends_the_others(client, new_client):
    access_token = _register(client).json()["access_token"]
    other_device = new_client()
    assert _login(other_device).status_code == 200

    response = client.post(
        f"{API}/auth/change-password",
        headers=_bearer(access_token),
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
    )

    assert response.status_code == 200
    assert client.get(f"{API}/auth/me", headers=_bearer(access_token)).status_code == 200
    assert client.post(f"{API}/auth/refresh").status_code == 200
    assert other_device.post(f"{API}/auth/refresh").status_code == 401
    assert _login(new_client(), password=NEW_PASSWORD).status_code == 200
