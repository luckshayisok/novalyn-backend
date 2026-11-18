"""Security hardening: leaked passwords, malware scanning, security headers."""

import hashlib
import socket
import struct
import threading

import httpx
import pytest

from app.core.config import Settings, get_settings
from app.integrations.pwned import times_leaked
from tests.helpers import API, PASSWORD, PDF_BYTES, error_code

# The EICAR test string: harmless, and detected by every antivirus as a test "virus".
EICAR = rb"X5O!P%@AP[4\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"


# ------------------------------------------------------------ leaked passwords


def _range_response(password: str, count: int | None):
    """The service's answer for this password's prefix; count None leaves the password out of it."""
    digest = hashlib.sha1(password.encode(), usedforsecurity=False).hexdigest().upper()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/range/{digest[:5]}"  # only the first five characters are sent
        assert password not in str(request.url) and digest not in str(request.url)
        assert request.headers["Add-Padding"] == "true"
        lines = ["0018A45C4D1DEF81644B54AB7F969B88D65:0", "00D4F6E8FA6EECAD2A3AA415EEC418D38EC:2"]
        if count is not None:
            lines.insert(1, f"{digest[5:]}:{count}")
        return httpx.Response(200, text="\r\n".join(lines))

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_times_leaked_uses_only_a_hash_prefix():
    password = "correct horse battery"
    assert times_leaked(password, client=_range_response(password, 1234)) == 1234
    assert times_leaked(password, client=_range_response(password, None)) == 0


def test_an_unreachable_service_does_not_block_sign_up():
    def fail(_request):
        raise httpx.ConnectError("offline")

    assert times_leaked("anything at all", client=httpx.Client(transport=httpx.MockTransport(fail))) is None


@pytest.fixture
def breach_check(monkeypatch):
    monkeypatch.setattr(get_settings(), "password_breach_check", True)
    leaked: set[str] = set()
    monkeypatch.setattr("app.services.auth.times_leaked", lambda password: 42 if password in leaked else 0)
    return leaked


def test_leaked_passwords_are_refused_at_sign_up(client, breach_check):
    breach_check.add("Summer-Holiday-2019")
    response = client.post(
        f"{API}/auth/register",
        json={"email": "new@example.com", "password": "Summer-Holiday-2019", "full_name": "New Person"},
    )
    assert response.status_code == 422
    assert error_code(response) == "weak_password"
    assert "data breach" in response.json()["error"]["message"]

    fine = client.post(
        f"{API}/auth/register",
        json={"email": "new@example.com", "password": PASSWORD, "full_name": "New Person"},
    )
    assert fine.status_code == 201


def test_breach_check_is_off_unless_configured(client, monkeypatch):
    monkeypatch.setattr("app.services.auth.times_leaked", lambda password: pytest.fail("must not be called"))
    response = client.post(
        f"{API}/auth/register", json={"email": "x@example.com", "password": PASSWORD, "full_name": "X"}
    )
    assert response.status_code == 201


# ------------------------------------------------------------------ malware


class FakeClamd:
    """A tiny clamd: INSTREAM chunks in, one verdict out. Reports EICAR as found."""

    def __init__(self) -> None:
        self.server = socket.create_server(("127.0.0.1", 0))
        self.port = self.server.getsockname()[1]
        self.scanned = 0
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _read(self, connection, size):
        data = b""
        while len(data) < size:
            part = connection.recv(size - len(data))
            if not part:
                raise ConnectionError
            data += part
        return data

    def _serve(self) -> None:
        while True:
            try:
                connection, _ = self.server.accept()
            except OSError:
                return
            with connection:
                command = self._read(connection, len(b"zINSTREAM\0"))
                assert command == b"zINSTREAM\0"
                payload = b""
                while (size := struct.unpack("!I", self._read(connection, 4))[0]) > 0:
                    payload += self._read(connection, size)
                self.scanned += 1
                verdict = b"stream: Eicar-Test-Signature FOUND\0" if EICAR in payload else b"stream: OK\0"
                connection.sendall(verdict)

    def close(self) -> None:
        self.server.close()


@pytest.fixture
def clamd(monkeypatch):
    fake = FakeClamd()
    settings = get_settings()
    monkeypatch.setattr(settings, "malware_scanner", "clamav")
    monkeypatch.setattr(settings, "clamav_host", "127.0.0.1")
    monkeypatch.setattr(settings, "clamav_port", fake.port)
    monkeypatch.setattr(settings, "clamav_timeout_seconds", 5.0)
    yield fake
    fake.close()


@pytest.fixture
def owner_org(actor):
    owner = actor("owner@example.com", "Olivia Owner")
    return owner, owner.create_org("Acme")["id"]


def test_clean_uploads_are_scanned_and_stored(owner_org, clamd):
    owner, org = owner_org
    assert owner.upload(org).status_code == 201
    assert clamd.scanned == 1


def test_infected_uploads_are_refused(owner_org, clamd):
    owner, org = owner_org
    infected = PDF_BYTES.replace(b"%%EOF", EICAR + b"\n%%EOF")
    response = owner.upload(org, content=infected)
    assert response.status_code == 422
    assert error_code(response) == "file_infected"
    assert owner.get(f"/organizations/{org}/documents").json()["total"] == 0


def test_uploads_are_refused_when_the_scanner_is_down(owner_org, clamd):
    owner, org = owner_org
    clamd.close()
    response = owner.upload(org)
    assert response.status_code == 503
    assert error_code(response) == "scanner_unavailable"
    assert owner.get(f"/organizations/{org}/documents").json()["total"] == 0


# ------------------------------------------------------------------ headers


def test_api_responses_carry_a_strict_content_security_policy(client):
    response = client.get("/health")
    assert (
        response.headers["Content-Security-Policy"]
        == "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
    )
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "Strict-Transport-Security" not in response.headers  # production only


def test_file_links_keep_their_own_policy_so_previews_can_frame_them(owner_org):
    owner, org = owner_org
    document = owner.uploaded(org)
    version = document["current_version"]["id"]
    link = owner.post(f"/organizations/{org}/documents/{document['id']}/versions/{version}/file-url", json={})
    response = owner.client.get(link.json()["url"])
    assert response.status_code == 200
    assert "frame-ancestors 'self'" in response.headers["Content-Security-Policy"]


def test_production_warns_about_missing_hardening():
    settings = Settings(
        _env_file=None,
        environment="production",
        secret_key="x" * 40,
        database_url="postgresql://app@db/novalyn",
        cookie_secure=True,
        celery_task_always_eager=False,
        redis_url="redis://cache:6379/0",
        email_backend="smtp",
        cors_origins="https://app.example.com",
    )
    assert len(settings.production_warnings) == 2
    hardened = settings.model_copy(update={"malware_scanner": "clamav", "password_breach_check": True})
    assert hardened.production_warnings == []
