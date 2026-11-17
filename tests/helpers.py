"""Helpers shared by the tests."""

import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from fastapi.testclient import TestClient

from app.integrations.email.base import EmailMessage
from app.integrations.email.senders import get_email_sender

API = "/api/v1"
PASSWORD = "Harbor-Lantern-4821"  # test value, used only against the test database
NEW_PASSWORD = "Granite-Meadow-9073"

_QUERY_TOKEN = re.compile(r"[?&]token=([A-Za-z0-9_\-]+)")
_INVITE_TOKEN = re.compile(r"/invite/([A-Za-z0-9_\-]+)")


PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 64


def docx_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<w:document/>")
    return buffer.getvalue()


def plain_zip_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("readme.txt", "not a word document")
    return buffer.getvalue()


def in_days(days: int) -> str:
    """An ISO date relative to today (UTC, the default organization time zone)."""
    from app.services.expiry import organization_today

    return (organization_today("UTC") + timedelta(days=days)).isoformat()


def today() -> date:
    from app.services.expiry import organization_today

    return organization_today("UTC")


def outbox() -> list[EmailMessage]:
    return get_email_sender().outbox


def emails_to(address: str) -> list[EmailMessage]:
    return [message for message in outbox() if message.to == address]


def last_email(address: str, containing: str | None = None) -> EmailMessage:
    """The most recent email to the address, optionally the most recent whose text contains a marker."""
    messages = [m for m in emails_to(address) if containing is None or containing in m.text]
    assert messages, f"No matching email was sent to {address}"
    return messages[-1]


def link_token(message: EmailMessage) -> str:
    match = _QUERY_TOKEN.search(message.text)
    assert match, f"No token link in email: {message.subject}"
    return match.group(1)


def invite_token(message: EmailMessage) -> str:
    match = _INVITE_TOKEN.search(message.text)
    assert match, f"No invitation link in email: {message.subject}"
    return match.group(1)


def error_code(response) -> str:
    return response.json()["error"]["code"]


@dataclass
class Actor:
    """A registered user with an authenticated HTTP client."""

    client: TestClient
    email: str
    user: dict[str, Any]
    access_token: str
    organizations: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def register(cls, client: TestClient, email: str, full_name: str, *, verified: bool) -> "Actor":
        response = client.post(
            f"{API}/auth/register",
            json={"email": email, "password": PASSWORD, "full_name": full_name},
        )
        assert response.status_code == 201, response.text
        body = response.json()
        actor = cls(client=client, email=email, user=body["user"], access_token=body["access_token"])
        if verified:
            actor.verify_email()
        return actor

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}

    def request(self, method: str, path: str, **kwargs):
        headers = {**self.headers, **kwargs.pop("headers", {})}
        return self.client.request(method, f"{API}{path}", headers=headers, **kwargs)

    def get(self, path: str, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs):
        return self.request("POST", path, **kwargs)

    def patch(self, path: str, **kwargs):
        return self.request("PATCH", path, **kwargs)

    def delete(self, path: str, **kwargs):
        return self.request("DELETE", path, **kwargs)

    def verify_email(self) -> None:
        token = link_token(last_email(self.email, "/verify-email"))
        response = self.client.post(f"{API}/auth/verify-email", json={"token": token})
        assert response.status_code == 200, response.text

    def create_org(self, name: str = "Acme Facilities", **fields) -> dict[str, Any]:
        response = self.post("/organizations", json={"name": name, **fields})
        assert response.status_code == 201, response.text
        organization = response.json()
        self.organizations[organization["id"]] = organization
        return organization

    def invite(self, organization_id: str, email: str, role: str = "member"):
        return self.post(f"/organizations/{organization_id}/invitations", json={"email": email, "role": role})

    def accept_invitation_for(self, email: str | None = None):
        token = invite_token(last_email(email or self.email, "/invite/"))
        return self.post(f"/invitations/{token}/accept")

    def upload(
        self,
        organization_id: str,
        filename: str = "certificate.pdf",
        content: bytes = PDF_BYTES,
        **fields,
    ):
        """Upload a document. Fields default to a permit with no dates."""
        data = {"category": "permit", **{key: value for key, value in fields.items() if value is not None}}
        return self.client.post(
            f"{API}/organizations/{organization_id}/documents",
            headers=self.headers,
            data=data,
            files={"file": (filename, content, "application/octet-stream")},
        )

    def uploaded(self, organization_id: str, **fields) -> dict[str, Any]:
        response = self.upload(organization_id, **fields)
        assert response.status_code == 201, response.text
        return response.json()

    def create_entity(
        self, organization_id: str, type_: str = "supplier", name: str = "Globex", **fields
    ) -> dict:
        response = self.post(
            f"/organizations/{organization_id}/entities", json={"type": type_, "name": name, **fields}
        )
        assert response.status_code == 201, response.text
        return response.json()

    def membership_id(self, organization_id: str, email: str) -> str:
        """The membership ID of `email` in the organization, as seen by this actor."""
        response = self.get(f"/organizations/{organization_id}/members")
        assert response.status_code == 200, response.text
        for member in response.json():
            if member["user"]["email"] == email:
                return member["id"]
        raise AssertionError(f"{email} is not a member of {organization_id}")


def add_member(owner: Actor, organization_id: str, member: Actor, role: str = "member") -> str:
    """Invite `member` into the organization and accept. Returns the membership ID."""
    response = owner.invite(organization_id, member.email, role)
    assert response.status_code == 201, response.text
    accepted = member.accept_invitation_for()
    assert accepted.status_code == 200, accepted.text
    return owner.membership_id(organization_id, member.email)
