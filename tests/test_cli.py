"""Seed command."""

import pytest

from app.cli import DEMO_USERS, SeedError, seed
from app.core.config import get_settings
from tests.helpers import API, PASSWORD


def test_seed_creates_one_user_per_role_and_is_idempotent(db, client):
    created = seed(db, PASSWORD)
    again = seed(db, PASSWORD)

    assert created == [email for email, _, _ in DEMO_USERS]
    assert again == []

    login = client.post(f"{API}/auth/login", json={"email": "viewer@northwind.example", "password": PASSWORD})
    assert login.status_code == 200
    me = client.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {login.json()['access_token']}"})
    assert me.json()["user"]["is_email_verified"] is True
    assert [(org["name"], org["role"]) for org in me.json()["organizations"]] == [
        ("Northwind Facilities", "viewer")
    ]


def test_seed_refuses_production_and_weak_passwords(db, monkeypatch):
    with pytest.raises(Exception, match="at least"):
        seed(db, "short")

    monkeypatch.setattr(get_settings(), "environment", "production")
    with pytest.raises(SeedError):
        seed(db, PASSWORD)
