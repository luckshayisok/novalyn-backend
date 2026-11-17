"""Document intelligence through the API: upload, read, review, apply."""

from datetime import timedelta

import pytest

from app.core.time import utcnow
from app.integrations.ai.base import ProviderError, ProviderResult, RawField
from app.services import extraction as extraction_module
from tests.helpers import API, PNG_BYTES, add_member, error_code, in_days
from tests.samples import FIRE_CERTIFICATE, text_pdf, word_document

DOCX = "certificate.docx"


@pytest.fixture
def org(actor):
    owner = actor("owner@example.com", "Olivia Owner")
    return owner, owner.create_org("Acme")["id"]


def _state(actor_, org_id, document_id):
    response = actor_.get(f"/organizations/{org_id}/documents/{document_id}/extraction")
    assert response.status_code == 200, response.text
    return response.json()


def _upload_certificate(owner, org_id, lines=FIRE_CERTIFICATE, **fields):
    return owner.uploaded(org_id, filename="scan.pdf", content=text_pdf(lines), category="other", **fields)


class StubProvider:
    """A provider with a fixed answer, standing in for Claude."""

    name = "anthropic"
    label = "Claude"

    def __init__(self, fields=None, error=None, reads_files=True):
        self.fields = fields or {}
        self.error = error
        self.reads_files = reads_files
        self.inputs = []

    def can_read_file(self, content_type, size_bytes):
        return self.reads_files

    def extract(self, data):
        self.inputs.append(data)
        if self.error:
            raise self.error
        return ProviderResult(fields=self.fields, provider=self.name, model="claude-opus-5-5")


@pytest.fixture
def use_provider(monkeypatch):
    def install(provider):
        monkeypatch.setattr(extraction_module, "get_ai_provider", lambda: provider)
        return provider

    return install


CONFIDENT = {
    "name": RawField("Trade Licence", 0.97),
    "category": RawField("business_license", 0.95),
    "document_number": RawField("TL-55-2026", 0.96, evidence="Licence No. TL-55-2026"),
    "issuing_authority": RawField("City Council", 0.93),
    "holder_name": RawField("Acme Facilities Ltd", 0.94),
    "issue_date": RawField("1 July 2026", 0.95, iso="2026-07-01"),
    "expiry_date": RawField("30 June 2027", 0.95, iso="2027-06-30"),
}


# ------------------------------------------------------------------- pipeline


def test_upload_reads_the_file_and_stores_suggestions(org):
    owner, org_id = org

    document = _upload_certificate(owner, org_id)
    state = _state(owner, org_id, document["id"])

    assert document["extraction_status"] in ("review_required", "extraction_completed")
    assert state["enabled"] is True
    extraction = state["extraction"]
    assert extraction["provider"] == "heuristic"
    assert extraction["provider_label"] == "Rule-based reader (development)"
    assert extraction["text_source"] == "pdf_text"
    assert extraction["version_id"] == document["current_version"]["id"]
    fields = extraction["fields"]
    assert fields["document_number"]["value"] == "FS-2291"
    assert fields["expiry_date"]["value"] == "2027-03-14"
    assert fields["expiry_date"]["raw"] == "14 March 2027"
    assert fields["issue_date"]["value"] == "2026-03-15"
    assert fields["category"]["value"] == "safety_certificate"
    # The rule-based reader is never confident enough to skip a close look.
    assert extraction["status"] == "review_required"
    assert extraction["attempts"] == 1


def test_suggestions_never_change_the_document_by_themselves(org):
    owner, org_id = org

    document = _upload_certificate(owner, org_id)
    current = owner.get(f"/organizations/{org_id}/documents/{document['id']}").json()

    assert current["category"] == "other"
    assert current["document_number"] is None
    assert current["expiry_date"] is None
    assert current["verification_status"] == "pending"


def test_word_documents_are_read(org):
    owner, org_id = org
    data = word_document(FIRE_CERTIFICATE)

    document = owner.uploaded(org_id, filename=DOCX, content=data)

    extraction = _state(owner, org_id, document["id"])["extraction"]
    assert extraction["text_source"] == "docx"
    assert extraction["fields"]["document_number"]["value"] == "FS-2291"


def test_photo_without_ocr_or_a_file_reading_provider_fails_with_guidance(org):
    owner, org_id = org

    document = owner.uploaded(org_id, filename="photo.png", content=PNG_BYTES)
    extraction = _state(owner, org_id, document["id"])["extraction"]

    assert extraction["status"] == "failed"
    assert "Enter the details by hand" in extraction["error"]


def test_scans_go_to_a_provider_that_reads_files(org, use_provider):
    owner, org_id = org
    provider = use_provider(StubProvider(fields=CONFIDENT))

    document = owner.uploaded(org_id, filename="photo.png", content=PNG_BYTES)
    extraction = _state(owner, org_id, document["id"])["extraction"]

    assert provider.inputs[0].file == PNG_BYTES
    assert provider.inputs[0].text is None
    assert extraction["text_source"] == "file"
    assert extraction["status"] == "extraction_completed"
    assert extraction["model"] == "claude-opus-5-5"


def test_text_documents_are_sent_as_text_not_files(org, use_provider):
    owner, org_id = org
    provider = use_provider(StubProvider(fields=CONFIDENT))

    _upload_certificate(owner, org_id)

    assert provider.inputs[0].file is None
    assert "FS-2291" in provider.inputs[0].text


def test_provider_errors_are_recorded_and_the_upload_still_succeeds(org, use_provider):
    owner, org_id = org
    use_provider(
        StubProvider(
            error=ProviderError("The AI provider is busy or unreachable. Try again in a few minutes.")
        )
    )

    response = owner.upload(org_id, filename="scan.pdf", content=text_pdf(FIRE_CERTIFICATE))

    assert response.status_code == 201
    extraction = _state(owner, org_id, response.json()["id"])["extraction"]
    assert extraction["status"] == "failed"
    assert extraction["error"] == "The AI provider is busy or unreachable. Try again in a few minutes."


def test_unexpected_errors_do_not_leak_details(org, use_provider):
    owner, org_id = org
    use_provider(StubProvider(error=RuntimeError("secret internal detail")))

    document = _upload_certificate(owner, org_id)
    extraction = _state(owner, org_id, document["id"])["extraction"]

    assert extraction["status"] == "failed"
    assert "secret" not in extraction["error"]


def test_when_reading_is_switched_off_nothing_is_created(org, use_provider):
    owner, org_id = org
    use_provider(None)

    document = _upload_certificate(owner, org_id)

    assert document["extraction_status"] is None
    assert _state(owner, org_id, document["id"]) == {"enabled": False, "extraction": None}
    retry = owner.post(f"/organizations/{org_id}/documents/{document['id']}/extraction/retry")
    assert retry.status_code == 409
    assert error_code(retry) == "extraction_disabled"


def test_ambiguous_and_missing_dates_ask_for_a_check(org, use_provider):
    owner, org_id = org
    use_provider(
        StubProvider(
            fields={
                "name": RawField("Permit", 0.99),
                "issue_date": RawField("03/04/2026", 0.95, iso="2026-04-03"),
            }
        )
    )

    document = _upload_certificate(owner, org_id)
    extraction = _state(owner, org_id, document["id"])["extraction"]

    assert extraction["status"] == "review_required"
    assert extraction["fields"]["issue_date"]["confidence"] <= 0.5
    assert any("could mean" in warning for warning in extraction["warnings"])
    assert any("No expiry date" in warning for warning in extraction["warnings"])


def test_a_name_on_the_document_matching_a_record_suggests_that_record(org, use_provider):
    owner, org_id = org
    supplier = owner.create_entity(org_id, "supplier", "Acme Facilities Ltd")
    use_provider(StubProvider(fields=CONFIDENT))

    document = _upload_certificate(owner, org_id)
    suggestion = _state(owner, org_id, document["id"])["extraction"]["fields"]["entity_id"]

    assert suggestion["value"] == supplier["id"]
    assert "matches the record" in suggestion["note"]


def test_a_new_file_gets_a_new_reading(org):
    owner, org_id = org
    document = _upload_certificate(owner, org_id)
    first = _state(owner, org_id, document["id"])["extraction"]

    response = owner.client.post(
        f"{API}/organizations/{org_id}/documents/{document['id']}/versions",
        headers=owner.headers,
        files={
            "file": (
                "renewed.pdf",
                text_pdf(["Street Works Permit", "Permit No: SW-7781", "Expiry date: 2028-01-31"]),
                "application/pdf",
            )
        },
    )

    second = _state(owner, org_id, document["id"])["extraction"]
    assert response.status_code == 201
    assert second["id"] != first["id"]
    assert second["version_id"] == response.json()["current_version"]["id"]
    assert second["fields"]["expiry_date"]["value"] == "2028-01-31"


# ------------------------------------------------------------------- applying


def test_apply_saves_confirmed_values_and_records_who_did_it(org, use_provider):
    owner, org_id = org
    use_provider(StubProvider(fields=CONFIDENT))
    document = _upload_certificate(owner, org_id)
    url = f"/organizations/{org_id}/documents/{document['id']}/extraction"

    response = owner.post(
        f"{url}/apply",
        json={
            "name": "Trade Licence",
            "category": "business_license",
            "document_number": "TL-55-2026",
            "expiry_date": "2027-06-30",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["document_number"] == "TL-55-2026"
    assert body["expiry_date"] == "2027-06-30"
    assert body["verification_status"] == "pending"
    assert body["extraction_status"] == "verified"
    extraction = _state(owner, org_id, document["id"])["extraction"]
    assert extraction["status"] == "verified"
    assert extraction["reviewed_by_name"] == "Olivia Owner"
    assert set(extraction["applied_fields"]) == {"name", "category", "document_number", "expiry_date"}
    entry = owner.get(f"/organizations/{org_id}/audit-logs", params={"action": "document.extraction"}).json()
    assert entry["items"][0]["action"] == "document.extraction_applied"
    assert entry["items"][0]["metadata"]["verified"] is False


def test_apply_and_verify_in_one_step(org, use_provider):
    owner, org_id = org
    use_provider(StubProvider(fields=CONFIDENT))
    document = _upload_certificate(owner, org_id)

    body = owner.post(
        f"/organizations/{org_id}/documents/{document['id']}/extraction/apply",
        json={"expiry_date": "2027-06-30", "verify": True},
    ).json()

    assert body["verification_status"] == "verified"
    assert body["verified_by_name"] == "Olivia Owner"


def test_apply_on_a_verified_document_sends_it_back_for_review_unless_verified_again(org, use_provider):
    owner, org_id = org
    use_provider(StubProvider(fields=CONFIDENT))
    document = _upload_certificate(owner, org_id, expiry_date=in_days(100))
    owner.post(f"/organizations/{org_id}/documents/{document['id']}/verify")

    body = owner.post(
        f"/organizations/{org_id}/documents/{document['id']}/extraction/apply",
        json={"expiry_date": "2027-06-30"},
    ).json()

    assert body["verification_status"] == "pending"


def test_apply_validates_values_like_any_other_edit(org, use_provider):
    owner, org_id = org
    use_provider(StubProvider(fields=CONFIDENT))
    document = _upload_certificate(owner, org_id)
    url = f"/organizations/{org_id}/documents/{document['id']}/extraction/apply"

    backwards = owner.post(url, json={"issue_date": "2027-07-01", "expiry_date": "2027-06-30"})
    unknown = owner.post(url, json={"category": "tax_return"})

    assert backwards.status_code == 422
    assert unknown.status_code == 422
    assert _state(owner, org_id, document["id"])["extraction"]["status"] == "extraction_completed"


def test_apply_and_dismiss_need_suggestions_waiting(org, use_provider):
    owner, org_id = org
    use_provider(StubProvider(fields=CONFIDENT))
    document = _upload_certificate(owner, org_id)
    url = f"/organizations/{org_id}/documents/{document['id']}/extraction"

    dismissed = owner.post(f"{url}/dismiss")
    again = owner.post(f"{url}/apply", json={"name": "X"})

    assert dismissed.status_code == 200
    assert dismissed.json()["extraction"]["status"] == "dismissed"
    assert again.status_code == 409
    assert error_code(again) == "extraction_not_ready"
    assert owner.get(f"/organizations/{org_id}/documents/{document['id']}").json()["name"] == "scan"


def test_permissions(org, actor, use_provider):
    owner, org_id = org
    use_provider(StubProvider(fields=CONFIDENT))
    viewer, member = actor("viewer@example.com"), actor("member@example.com")
    add_member(owner, org_id, viewer, "viewer")
    add_member(owner, org_id, member, "member")
    document = _upload_certificate(owner, org_id)
    url = f"/organizations/{org_id}/documents/{document['id']}/extraction"

    assert viewer.get(url).status_code == 200
    assert viewer.post(f"{url}/apply", json={"name": "X"}).status_code == 403
    assert viewer.post(f"{url}/dismiss").status_code == 403
    assert viewer.post(f"{url}/retry").status_code == 403
    # Members may apply suggestions but not verify the document.
    assert member.post(f"{url}/apply", json={"name": "X", "verify": True}).status_code == 403
    assert member.post(f"{url}/apply", json={"name": "Licence"}).status_code == 200


# --------------------------------------------------------------------- retry


def test_retry_reads_the_file_again(org, use_provider):
    owner, org_id = org
    use_provider(StubProvider(error=ProviderError("busy")))
    document = _upload_certificate(owner, org_id)
    assert _state(owner, org_id, document["id"])["extraction"]["status"] == "failed"

    use_provider(StubProvider(fields=CONFIDENT))
    response = owner.post(f"/organizations/{org_id}/documents/{document['id']}/extraction/retry")

    assert response.status_code == 200
    extraction = response.json()["extraction"]
    assert extraction["status"] == "extraction_completed"
    assert extraction["attempts"] == 2
    assert extraction["error"] is None


def test_retry_is_refused_while_a_reading_is_running_unless_it_is_stale(org, db):
    from app.models import DocumentExtraction

    owner, org_id = org
    document = _upload_certificate(owner, org_id)
    row = db.query(DocumentExtraction).one()
    row.status = "processing"
    row.started_at = utcnow()
    db.commit()
    url = f"/organizations/{org_id}/documents/{document['id']}/extraction/retry"

    busy = owner.post(url)
    row.started_at = utcnow() - timedelta(hours=1)
    db.commit()
    stale = owner.post(url)

    assert busy.status_code == 409
    assert error_code(busy) == "extraction_in_progress"
    assert stale.status_code == 200


def test_running_twice_processes_once(org, db):
    from app.models import DocumentExtraction
    from app.services.extraction import ExtractionService

    owner, org_id = org
    _upload_certificate(owner, org_id)
    row = db.query(DocumentExtraction).one()

    ExtractionService(db).run(row.id)  # already finished: claimed by nobody
    db.refresh(row)

    assert row.attempts == 1


# ----------------------------------------------------------- dashboard, isolation


def test_dashboard_counts_suggestions_waiting(org, use_provider):
    owner, org_id = org
    use_provider(StubProvider(fields=CONFIDENT))
    first = _upload_certificate(owner, org_id)
    _upload_certificate(owner, org_id)
    owner.post(f"/organizations/{org_id}/documents/{first['id']}/extraction/dismiss")

    metrics = owner.get(f"/organizations/{org_id}/dashboard").json()["metrics"]

    assert metrics["suggestions_to_review"] == 1


def test_suggestions_are_isolated_between_organizations(org, actor):
    owner, org_id = org
    document = _upload_certificate(owner, org_id)
    intruder = actor("intruder@evil.example")
    other_org = intruder.create_org("Evil Corp")["id"]

    for path in ("", "/retry", "/apply", "/dismiss"):
        method = intruder.get if path == "" else intruder.post
        kwargs = {"json": {"name": "Mine"}} if path == "/apply" else {}
        assert (
            method(
                f"/organizations/{org_id}/documents/{document['id']}/extraction{path}", **kwargs
            ).status_code
            == 404
        )
        assert (
            method(
                f"/organizations/{other_org}/documents/{document['id']}/extraction{path}", **kwargs
            ).status_code
            == 404
        )


def test_library_filter_for_suggestions_waiting(org, use_provider):
    owner, org_id = org
    use_provider(StubProvider(fields=CONFIDENT))
    waiting = _upload_certificate(owner, org_id, name="Waiting")
    reviewed = _upload_certificate(owner, org_id, name="Reviewed")
    owner.post(f"/organizations/{org_id}/documents/{reviewed['id']}/extraction/dismiss")

    items = owner.get(f"/organizations/{org_id}/documents", params={"suggestions": "true"}).json()["items"]

    assert [item["id"] for item in items] == [waiting["id"]]
