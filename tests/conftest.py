"""Test configuration.

Environment variables are set before the application is imported, because the
settings and the database engine are created at import time.

By default the suite runs on a temporary SQLite file. Set TEST_DATABASE_URL to
run it against PostgreSQL instead.
"""

import os
import tempfile
from pathlib import Path

_TMP_DIR = Path(tempfile.mkdtemp(prefix="novalyn-tests-"))

os.environ.update(
    {
        "ENVIRONMENT": "testing",
        "DATABASE_URL": os.environ.get("TEST_DATABASE_URL")
        or f"sqlite:///{(_TMP_DIR / 'test.db').as_posix()}",
        "SECRET_KEY": "test-only-secret-key-test-only-secret-key-0000",
        "EMAIL_BACKEND": "memory",
        "STORAGE_BACKEND": "local",
        "AI_PROVIDER": "heuristic",
        "OCR_BACKEND": "none",
        "ANTHROPIC_API_KEY": "",
        "STORAGE_LOCAL_DIR": (_TMP_DIR / "storage").as_posix(),
        "CELERY_TASK_ALWAYS_EAGER": "true",
        "RATE_LIMIT_ENABLED": "true",
        "REDIS_URL": "",
        "CELERY_BROKER_URL": "",
        "FRONTEND_URL": "http://localhost:5173",
        "COOKIE_SECURE": "false",
        "LOG_LEVEL": "WARNING",
    }
)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core.database import Base, SessionLocal, engine  # noqa: E402
from app.core.rate_limit import get_rate_limiter  # noqa: E402
from app.integrations.email.senders import get_email_sender  # noqa: E402
from app.main import app  # noqa: E402
from tests.helpers import Actor  # noqa: E402


@pytest.fixture(autouse=True)
def clean_state():
    """Every test starts with an empty database, an empty outbox and no rate-limit counters."""
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    get_rate_limiter().clear()
    get_email_sender().outbox.clear()
    yield


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def new_client():
    """Factory for independent HTTP clients, each with its own cookie jar."""
    clients: list[TestClient] = []

    def factory() -> TestClient:
        client = TestClient(app, headers={"X-Requested-With": "XMLHttpRequest"})
        clients.append(client)
        return client

    yield factory
    for client in clients:
        client.close()


@pytest.fixture
def client(new_client) -> TestClient:
    return new_client()


@pytest.fixture
def actor(new_client):
    """Factory: register a user and return an Actor that makes authenticated calls."""

    def factory(email: str, full_name: str = "Test User", *, verified: bool = True) -> Actor:
        return Actor.register(new_client(), email, full_name, verified=verified)

    return factory
