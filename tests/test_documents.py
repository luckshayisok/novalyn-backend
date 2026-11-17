"""Documents: upload and validation, library, updates, versions, verification, archive, download."""

import uuid
from pathlib import Path

import pytest

from app.core.config import get_settings
from app.core.security import create_access_token
from tests.helpers import (
    API,
    JPEG_BYTES,
    PDF_BYTES,
    PNG_BYTES,
    add_member,
    docx_bytes,
    error_code,
    in_days,
    plain_zip_bytes,
)


@pytest.fixture
def org(actor):
    owner = actor("owner@example.com", "Olivia Owner")
    return owner, owner.create_org("Acme")["id"]


def _docs(actor_, org_id, **params):
    response = actor_.get(f"/organizations/{org_id}/documents", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _names(actor_, org_id, **params):
    return [item["name"] for item in _docs(actor_, org_id, **params)["items"]]


def _file_url(actor_, org_id, document, disposition="attachment", version=None):
    version_id = (version or document["current_version"])["id"]
    return actor_.post(
        f"/organizations/{org_id}/documents/{document['id']}/versions/{version_id}/file-url",
        json={"disposition": disposition},
    )


# --------------------------------------------------------------------- upload


def test_upload_creates_a_document_with_its_first_version(org):
    owner, org_id = org

    response = owner.upload(
        org_id,
        filename="Fire Safety Certificate.pdf",
        category="safety_certificate",
        document_number="FS-2291",
        issuing_authority="City Fire Department",
        issue_date=in_days(-300),
        expiry_date=in_days(65),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Fire Safety Certificate"  # defaults to the file name without its extension
    assert body["category"] == "safety_certificate"
    assert body["document_number"] == "FS-2291"
    assert body["status"] == "valid"
    assert body["days_until_expiry"] == 65
    assert body["verification_status"] == "pending"
    assert body["version_count"] == 1
    version = body["current_version"]
    assert version["version_number"] == 1
    assert version["original_filename"] == "Fire Safety Certificate.pdf"
    assert version["content_type"] == "application/pdf"
    assert version["size_bytes"] == len(PDF_BYTES)
    assert version["previewable"] is True
    assert version["uploaded_by_name"] == "Olivia Owner"
    assert len(version["sha256"]) == 64


def test_uploaded_file_is_stored_under_the_organization_and_not_in_the_database(org, db):
    from sqlalchemy import select

    from app.models import DocumentVersion

    owner, org_id = org
    owner.uploaded(org_id)

    version = db.scalars(select(DocumentVersion)).one()
    assert version.storage_key.startswith(f"organizations/{org_id}/documents/")
    stored = Path(get_settings().storage_local_dir) / version.storage_key
    assert stored.read_bytes() == PDF_BYTES


@pytest.mark.parametrize(
    ("filename", "content", "content_type", "previewable"),
    [
        ("a.pdf", PDF_BYTES, "application/pdf", True),
        ("a.PNG", PNG_BYTES, "image/png", True),
        ("a.jpg", JPEG_BYTES, "image/jpeg", True),
        ("a.jpeg", JPEG_BYTES, "image/jpeg", True),
        (
            "a.docx",
            docx_bytes(),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            False,
        ),
    ],
)
def test_supported_file_types_are_accepted(org, filename, content, content_type, previewable):
    owner, org_id = org

    body = owner.uploaded(org_id, filename=filename, content=content)

    assert body["current_version"]["content_type"] == content_type
    assert body["current_version"]["previewable"] is previewable


@pytest.mark.parametrize(
    ("filename", "content", "code", "status"),
    [
        ("malware.exe", b"MZ\x90\x00", "unsupported_file_type", 422),
        ("page.html", b"<script>alert(1)</script>", "unsupported_file_type", 422),
        ("noextension", PDF_BYTES, "unsupported_file_type", 422),
        ("disguised.pdf", PNG_BYTES, "file_content_mismatch", 422),
        ("script.png", b"<script>alert(1)</script>", "file_content_mismatch", 422),
        ("archive.docx", plain_zip_bytes(), "file_content_mismatch", 422),
        ("broken.docx", b"PK\x03\x04 not really a zip", "file_content_mismatch", 422),
        ("empty.pdf", b"", "empty_file", 422),
    ],
)
def test_unsafe_or_mismatched_files_are_rejected(org, filename, content, code, status):
    owner, org_id = org

    response = owner.upload(org_id, filename=filename, content=content)

    assert response.status_code == status
    assert error_code(response) == code
    assert _docs(owner, org_id)["total"] == 0


def test_oversized_files_are_rejected(org, monkeypatch):
    owner, org_id = org
    monkeypatch.setattr(get_settings(), "max_upload_size_mb", 0)

    response = owner.upload(org_id)

    assert response.status_code == 413
    assert error_code(response) == "file_too_large"


def test_declared_oversized_body_is_refused_before_it_is_read(org):
    owner, org_id = org
    too_large = get_settings().max_upload_size_bytes + 2 * 1024 * 1024

    response = owner.client.post(
        f"{API}/organizations/{org_id}/documents",
        headers={
            **owner.headers,
            "Content-Length": str(too_large),
            "Content-Type": "application/octet-stream",
        },
        content=b"x",
    )

    assert response.status_code == 413


def test_directory_parts_in_a_filename_are_dropped(org, db):
    from sqlalchemy import select

    from app.models import DocumentVersion

    owner, org_id = org

    body = owner.uploaded(org_id, filename="../../etc/passwd.pdf")

    assert body["current_version"]["original_filename"] == "passwd.pdf"
    assert ".." not in db.scalars(select(DocumentVersion)).one().storage_key


def test_upload_validates_the_details(org, actor):
    owner, org_id = org
    stranger = actor("stranger@example.com")
    other_org = stranger.create_org("Other")["id"]
    foreign_entity = stranger.create_entity(other_org)["id"]
    foreign_membership = stranger.membership_id(other_org, stranger.email)

    assert owner.upload(org_id, category="tax_return").status_code == 422
    assert owner.upload(org_id, expiry_date="not-a-date").status_code == 422
    backwards = owner.upload(org_id, issue_date=in_days(10), expiry_date=in_days(5))
    assert backwards.status_code == 422
    assert backwards.json()["error"]["details"][0]["field"] == "expiry_date"
    # Records and members of another organization cannot be attached.
    assert owner.upload(org_id, entity_id=foreign_entity).status_code == 422
    assert owner.upload(org_id, owner_membership_id=foreign_membership).status_code == 422
    assert owner.upload(org_id, entity_id=str(uuid.uuid4())).status_code == 422
    assert _docs(owner, org_id)["total"] == 0


def test_upload_requires_write_permission(org, actor):
    owner, org_id = org
    viewer, member = actor("viewer@example.com"), actor("member@example.com")
    add_member(owner, org_id, viewer, "viewer")
    add_member(owner, org_id, member, "member")

    assert viewer.upload(org_id).status_code == 403
    assert member.upload(org_id).status_code == 201
    assert _docs(viewer, org_id)["total"] == 1  # viewers can read


# -------------------------------------------------------------------- library


@pytest.mark.parametrize(
    ("days", "status"),
    [(-1, "expired"), (0, "expiring_soon"), (30, "expiring_soon"), (31, "valid"), (None, "valid")],
)
def test_status_follows_the_expiry_date(org, days, status):
    owner, org_id = org

    body = owner.uploaded(org_id, expiry_date=in_days(days) if days is not None else None)

    assert body["status"] == status
    assert _names(owner, org_id, status=status) == ["certificate"]
    others = {"expired", "expiring_soon", "valid"} - {status}
    assert all(_names(owner, org_id, status=other) == [] for other in others)


@pytest.fixture
def library(org):
    owner, org_id = org
    supplier = owner.create_entity(org_id, "supplier", "Globex Supplies")
    owner.uploaded(org_id, name="Trade licence", category="business_license", expiry_date=in_days(200))
    owner.uploaded(
        org_id,
        name="Liability insurance",
        category="insurance_policy",
        expiry_date=in_days(12),
        document_number="POL-778",
        entity_id=supplier["id"],
    )
    owner.uploaded(
        org_id,
        name="Forklift inspection",
        category="equipment_inspection",
        expiry_date=in_days(-20),
        issuing_authority="SafeLift Inspectors",
    )
    owner.uploaded(org_id, name="Company handbook", category="other")
    return owner, org_id, supplier


def test_library_default_order_is_soonest_expiry_first_with_undated_last(library):
    owner, org_id, _ = library

    assert _names(owner, org_id) == [
        "Forklift inspection",
        "Liability insurance",
        "Trade licence",
        "Company handbook",
    ]
    assert _names(owner, org_id, sort="-expiry_date")[-1] == "Company handbook"
    assert _names(owner, org_id, sort="name")[0] == "Company handbook"
    assert _names(owner, org_id, sort="-name")[0] == "Trade licence"


def test_library_filters(library):
    owner, org_id, supplier = library

    assert _names(owner, org_id, category="insurance_policy") == ["Liability insurance"]
    assert _names(owner, org_id, status="expired") == ["Forklift inspection"]
    assert _names(owner, org_id, status="expiring_soon") == ["Liability insurance"]
    assert set(_names(owner, org_id, status="valid")) == {"Trade licence", "Company handbook"}
    assert _names(owner, org_id, entity_id=supplier["id"]) == ["Liability insurance"]
    assert _names(owner, org_id, verification="verified") == []
    assert len(_names(owner, org_id, verification="pending")) == 4


def test_library_search_matches_name_number_and_authority(library):
    owner, org_id, _ = library

    assert _names(owner, org_id, q="LICENCE") == ["Trade licence"]
    assert _names(owner, org_id, q="pol-77") == ["Liability insurance"]
    assert _names(owner, org_id, q="safelift") == ["Forklift inspection"]
    assert _names(owner, org_id, q="100%") == []  # wildcard characters are taken literally
    assert _names(owner, org_id, q="_") == []


def test_library_pagination_and_parameter_validation(library):
    owner, org_id, _ = library

    page = _docs(owner, org_id, page=2, page_size=3)

    assert page["total"] == 4
    assert len(page["items"]) == 1
    assert owner.get(f"/organizations/{org_id}/documents", params={"sort": "password"}).status_code == 422
    assert owner.get(f"/organizations/{org_id}/documents", params={"status": "archived"}).status_code == 422
    assert owner.get(f"/organizations/{org_id}/documents", params={"page_size": 1000}).status_code == 422


# -------------------------------------------------------------------- updates


def test_update_changes_details_and_links(org):
    owner, org_id = org
    document = owner.uploaded(org_id)
    supplier = owner.create_entity(org_id)
    owner_membership = owner.membership_id(org_id, owner.email)

    response = owner.patch(
        f"/organizations/{org_id}/documents/{document['id']}",
        json={
            "name": "Trade licence 2027",
            "category": "business_license",
            "expiry_date": in_days(400),
            "entity_id": supplier["id"],
            "owner_membership_id": owner_membership,
            "notes": "  Original kept in the safe.  ",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Trade licence 2027"
    assert body["entity"] == {"id": supplier["id"], "name": "Globex", "type": "supplier"}
    assert body["owner"]["email"] == "owner@example.com"
    assert body["notes"] == "Original kept in the safe."

    cleared = owner.patch(
        f"/organizations/{org_id}/documents/{document['id']}",
        json={"entity_id": None, "notes": "", "name": None},
    ).json()
    assert cleared["entity"] is None
    assert cleared["notes"] is None
    assert cleared["name"] == "Trade licence 2027"  # a name cannot be cleared


def test_update_rejects_invalid_values(org):
    owner, org_id = org
    document = owner.uploaded(org_id, issue_date=in_days(-10))
    url = f"/organizations/{org_id}/documents/{document['id']}"

    assert owner.patch(url, json={"category": "nonsense"}).status_code == 422
    assert owner.patch(url, json={"expiry_date": in_days(-30)}).status_code == 422
    assert owner.patch(url, json={"entity_id": str(uuid.uuid4())}).status_code == 422
    assert owner.patch(url, json={"name": "x" * 300}).status_code == 422


# --------------------------------------------------------------- verification


def test_verification_needs_the_right_role(org, actor):
    owner, org_id = org
    member, manager = actor("member@example.com"), actor("manager@example.com", "Carla Compliance")
    add_member(owner, org_id, member, "member")
    add_member(owner, org_id, manager, "compliance_manager")
    document = member.uploaded(org_id)
    url = f"/organizations/{org_id}/documents/{document['id']}"

    assert member.post(f"{url}/verify").status_code == 403
    assert member.post(f"{url}/reject", json={"reason": "blurry"}).status_code == 403

    verified = manager.post(f"{url}/verify")
    assert verified.status_code == 200
    assert verified.json()["verification_status"] == "verified"
    assert verified.json()["verified_by_name"] == "Carla Compliance"
    assert verified.json()["verified_at"] is not None


def test_reject_needs_a_reason_and_records_it(org):
    owner, org_id = org
    document = owner.uploaded(org_id)
    url = f"/organizations/{org_id}/documents/{document['id']}"

    assert owner.post(f"{url}/reject", json={"reason": "   "}).status_code == 422
    rejected = owner.post(f"{url}/reject", json={"reason": "The scan is unreadable."}).json()

    assert rejected["verification_status"] == "rejected"
    assert rejected["rejection_reason"] == "The scan is unreadable."
    # Verifying afterwards clears the rejection.
    assert owner.post(f"{url}/verify").json()["rejection_reason"] is None


def test_changing_verified_facts_sends_the_document_back_for_review(org):
    owner, org_id = org
    document = owner.uploaded(org_id, expiry_date=in_days(100))
    url = f"/organizations/{org_id}/documents/{document['id']}"
    owner.post(f"{url}/verify")

    # Notes and the display name are not facts taken from the document.
    still_verified = owner.patch(url, json={"notes": "Filed.", "name": "Renamed"}).json()
    assert still_verified["verification_status"] == "verified"

    changed = owner.patch(url, json={"expiry_date": in_days(500)}).json()
    assert changed["verification_status"] == "pending"
    assert changed["verified_at"] is None
    assert changed["verified_by_name"] is None


# ------------------------------------------------------------------- versions


def test_replacing_the_file_adds_a_version_and_keeps_the_history(org):
    owner, org_id = org
    document = owner.uploaded(org_id, filename="licence-2026.pdf", expiry_date=in_days(5))
    url = f"/organizations/{org_id}/documents/{document['id']}"
    owner.post(f"{url}/verify")

    response = owner.client.post(
        f"{API}{url}/versions",
        headers=owner.headers,
        data={"expiry_date": in_days(370), "note": "Renewed for another year"},
        files={"file": ("licence-2027.png", PNG_BYTES, "image/png")},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["version_count"] == 2
    assert body["current_version"]["version_number"] == 2
    assert body["current_version"]["original_filename"] == "licence-2027.png"
    assert body["current_version"]["note"] == "Renewed for another year"
    assert [v["version_number"] for v in body["versions"]] == [2, 1]
    assert body["versions"][1]["original_filename"] == "licence-2026.pdf"
    assert body["status"] == "valid"
    assert body["verification_status"] == "pending"  # the new file has not been checked

    # The earlier version can still be downloaded.
    old = _file_url(owner, org_id, body, version=body["versions"][1]).json()
    assert owner.client.get(old["url"]).content == PDF_BYTES


def test_replacement_file_is_validated_too(org):
    owner, org_id = org
    document = owner.uploaded(org_id)

    response = owner.client.post(
        f"{API}/organizations/{org_id}/documents/{document['id']}/versions",
        headers=owner.headers,
        files={"file": ("fake.pdf", PNG_BYTES, "application/pdf")},
    )

    assert response.status_code == 422
    assert error_code(response) == "file_content_mismatch"
    assert owner.get(f"/organizations/{org_id}/documents/{document['id']}").json()["version_count"] == 1


# ------------------------------------------------------------------- download


def test_download_link_returns_the_file_as_an_attachment(org, client):
    owner, org_id = org
    document = owner.uploaded(org_id, filename="Contrat d'été.pdf")

    link = _file_url(owner, org_id, document)

    assert link.status_code == 200
    assert link.json()["expires_in"] == get_settings().file_url_ttl_seconds
    # The link itself is the credential: no Authorization header is sent.
    download = client.get(link.json()["url"])
    assert download.status_code == 200
    assert download.content == PDF_BYTES
    assert download.headers["content-type"] == "application/pdf"
    assert download.headers["content-disposition"].startswith("attachment;")
    assert "filename*=UTF-8''Contrat%20d%27%C3%A9t%C3%A9.pdf" in download.headers["content-disposition"]
    assert download.headers["x-content-type-options"] == "nosniff"
    assert download.headers["cache-control"] == "private, no-store"


def test_preview_link_is_inline_only_for_types_a_browser_can_show_safely(org, client):
    owner, org_id = org
    pdf = owner.uploaded(org_id)
    word = owner.uploaded(org_id, filename="policy.docx", content=docx_bytes())

    pdf_preview = client.get(_file_url(owner, org_id, pdf, "inline").json()["url"])
    word_preview = client.get(_file_url(owner, org_id, word, "inline").json()["url"])

    assert pdf_preview.headers["content-disposition"].startswith("inline;")
    assert pdf_preview.headers["x-frame-options"] == "SAMEORIGIN"
    assert word_preview.headers["content-disposition"].startswith("attachment;")


def test_expired_tampered_and_wrong_kind_of_tokens_are_refused(org, client, monkeypatch):
    owner, org_id = org
    document = owner.uploaded(org_id)
    good = _file_url(owner, org_id, document).json()["url"]

    tampered = client.get(good[:-3] + ("aaa" if not good.endswith("aaa") else "bbb"))
    access_token, _ = create_access_token(uuid.uuid4(), uuid.uuid4())
    wrong_kind = client.get(f"{API}/files/{access_token}")
    monkeypatch.setattr(get_settings(), "file_url_ttl_seconds", -1)
    expired = client.get(_file_url(owner, org_id, document).json()["url"])

    for response in (tampered, wrong_kind, expired):
        assert response.status_code == 404
        assert error_code(response) == "file_link_invalid"


def test_downloads_are_audited_and_previews_are_not(org):
    owner, org_id = org
    document = owner.uploaded(org_id)

    _file_url(owner, org_id, document, "inline")
    _file_url(owner, org_id, document, "attachment")

    actions = [e["action"] for e in owner.get(f"/organizations/{org_id}/audit-logs").json()["items"]]
    assert actions.count("document.downloaded") == 1


# -------------------------------------------------------------------- archive


def test_archive_hides_a_document_and_restore_brings_it_back(org, actor):
    owner, org_id = org
    member = actor("member@example.com")
    add_member(owner, org_id, member, "member")
    document = owner.uploaded(org_id, expiry_date=in_days(-3))
    url = f"/organizations/{org_id}/documents/{document['id']}"

    assert member.post(f"{url}/archive").status_code == 403
    archived = owner.post(f"{url}/archive")

    assert archived.status_code == 200
    assert archived.json()["status"] == "archived"
    assert _docs(owner, org_id)["total"] == 0
    assert _names(owner, org_id, archived="true") == ["certificate"]
    # Archived documents are read-only until restored, and their files stay available.
    assert owner.patch(url, json={"name": "New"}).status_code == 409
    assert error_code(owner.post(f"{url}/verify")) == "document_archived"
    assert _file_url(owner, org_id, document).status_code == 200

    restored = owner.post(f"{url}/restore").json()
    assert restored["status"] == "expired"
    assert _docs(owner, org_id)["total"] == 1


def test_there_is_no_way_to_delete_a_document_or_a_version(org):
    owner, org_id = org
    document = owner.uploaded(org_id)
    url = f"/organizations/{org_id}/documents/{document['id']}"

    assert owner.delete(url).status_code == 405
    assert owner.delete(f"{url}/versions/{document['current_version']['id']}").status_code in (404, 405)


# ---------------------------------------------------------------------- audit


def test_document_events_are_recorded(org):
    owner, org_id = org
    document = owner.uploaded(org_id, name="Permit")
    url = f"/organizations/{org_id}/documents/{document['id']}"
    owner.patch(url, json={"expiry_date": in_days(90)})
    owner.post(f"{url}/reject", json={"reason": "Wrong year"})
    owner.post(f"{url}/verify")
    owner.post(f"{url}/archive")
    owner.post(f"{url}/restore")

    entries = owner.get(f"/organizations/{org_id}/audit-logs", params={"action": "document."}).json()["items"]

    assert [entry["action"] for entry in entries] == [
        "document.restored",
        "document.archived",
        "document.verified",
        "document.rejected",
        "document.updated",
        "document.uploaded",
    ]
    updated = entries[4]
    assert updated["metadata"] == {"name": "Permit", "fields": ["expiry_date"]}
    assert all(entry["resource_id"] == document["id"] for entry in entries)


# ---------------------------------------------------------- tenant isolation


def test_documents_are_invisible_and_untouchable_from_another_organization(org, actor, client):
    owner, org_id = org
    document = owner.uploaded(org_id, name="Secret contract")
    version_id = document["current_version"]["id"]
    intruder = actor("intruder@evil.example")
    other_org = intruder.create_org("Evil Corp")["id"]
    own_document = intruder.uploaded(other_org)

    # Through the victim's organization URL: not a member, so everything is 404.
    victim_url = f"/organizations/{org_id}/documents/{document['id']}"
    assert intruder.get(victim_url).status_code == 404
    assert intruder.get(f"/organizations/{org_id}/documents").status_code == 404
    assert intruder.get(f"/organizations/{org_id}/dashboard").status_code == 404
    assert intruder.get(f"/organizations/{org_id}/search", params={"q": "secret"}).status_code == 404

    # Through the intruder's own organization URL with the victim's IDs.
    own_url = f"/organizations/{other_org}/documents"
    assert intruder.get(f"{own_url}/{document['id']}").status_code == 404
    assert intruder.patch(f"{own_url}/{document['id']}", json={"name": "Mine"}).status_code == 404
    assert intruder.post(f"{own_url}/{document['id']}/archive").status_code == 404
    assert intruder.post(f"{own_url}/{document['id']}/verify").status_code == 404
    for document_id in (document["id"], own_document["id"]):
        response = intruder.post(
            f"{own_url}/{document_id}/versions/{version_id}/file-url", json={"disposition": "attachment"}
        )
        assert response.status_code == 404
    upload = intruder.client.post(
        f"{API}{own_url}/{document['id']}/versions",
        headers=intruder.headers,
        files={"file": ("x.pdf", PDF_BYTES, "application/pdf")},
    )
    assert upload.status_code == 404

    assert _names(intruder, other_org) == ["certificate"]
    assert _names(intruder, other_org, q="secret") == []
    assert owner.get(victim_url).json()["name"] == "Secret contract"
    assert owner.get(victim_url).json()["version_count"] == 1
