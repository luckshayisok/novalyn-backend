"""Application shell: health checks, error shape, headers, CORS, email adapters, migrations."""

import json
from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect

from alembic import command
from app.core.database import Base
from app.integrations.email.base import EmailMessage
from app.integrations.email.senders import FileEmailSender
from app.main import app
from app.workers import notification_tasks
from tests.helpers import API

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def test_health_endpoints(client):
    assert client.get("/health").json() == {"status": "ok"}

    ready = client.get("/health/ready")
    assert ready.status_code == 200
    assert ready.json() == {"status": "ok", "database": "ok"}


def test_errors_share_one_shape_and_carry_the_request_id(client):
    response = client.get(f"{API}/auth/me", headers={"X-Request-ID": "trace-12345678"})

    assert response.status_code == 401
    error = response.json()["error"]
    assert set(error) == {"code", "message", "details", "request_id"}
    assert error["request_id"] == "trace-12345678"
    assert response.headers["x-request-id"] == "trace-12345678"


def test_unknown_route_and_wrong_method_use_the_error_shape(client):
    missing = client.get(f"{API}/does-not-exist")
    wrong_method = client.delete(f"{API}/auth/login")

    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"
    assert wrong_method.status_code == 405
    assert wrong_method.json()["error"]["code"] == "method_not_allowed"


def test_unhandled_errors_do_not_leak_details():
    @app.get("/__boom")
    def boom():
        raise RuntimeError("database password is hunter2")

    try:
        with TestClient(app, raise_server_exceptions=False) as failing_client:
            response = failing_client.get("/__boom")
    finally:
        app.router.routes[:] = [
            route for route in app.router.routes if getattr(route, "path", "") != "/__boom"
        ]

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "hunter2" not in response.text
    assert response.headers["x-request-id"]


def test_security_headers_are_set(client):
    response = client.get("/health")

    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"
    assert len(response.headers["x-request-id"]) == 32


def test_cors_allows_the_frontend_origin_only(client):
    def preflight(origin):
        return client.options(
            f"{API}/auth/login",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type,x-requested-with",
            },
        )

    allowed = preflight("http://localhost:5173")
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert allowed.headers["access-control-allow-credentials"] == "true"

    denied = preflight("https://evil.example")
    assert "access-control-allow-origin" not in denied.headers


# ------------------------------------------------------------------------ email


def test_file_email_sender_writes_one_file_per_message(tmp_path):
    sender = FileEmailSender(str(tmp_path / "mail"))

    sender.send(EmailMessage(to="a@example.com", subject="Hello", text="plain", html="<p>html</p>"))

    files = list((tmp_path / "mail").glob("*.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_text(encoding="utf-8"))["to"] == "a@example.com"


def test_failed_email_delivery_is_reported_not_raised(monkeypatch, caplog):
    class BrokenSender:
        def send(self, message):
            raise ConnectionError("smtp down")

    monkeypatch.setattr(notification_tasks, "get_email_sender", lambda: BrokenSender())

    queued = notification_tasks.enqueue_email(
        EmailMessage(to="a@example.com", subject="Hello", text="plain", html="<p>html</p>")
    )

    assert queued is False
    assert "Could not queue email" in caplog.text


# ------------------------------------------------------------------- migrations


def test_migrations_build_the_schema_the_models_describe(tmp_path):
    url = f"sqlite:///{(tmp_path / 'migrated.db').as_posix()}"
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    config.attributes["configure_logging"] = False

    command.upgrade(config, "head")

    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection, opts={"compare_type": True})
            differences = compare_metadata(context, Base.metadata)
        assert differences == []

        command.downgrade(config, "base")
        assert inspect(engine).get_table_names() == ["alembic_version"]
    finally:
        engine.dispose()
