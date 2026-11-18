"""Compliance calendar, reports and exports, and audit log filters."""

import csv
import io
from datetime import timedelta

import pytest

from tests.helpers import add_member, error_code, in_days, today
from tests.test_plans import set_plan


@pytest.fixture
def team(actor):
    owner = actor("owner@example.com", "Olivia Owner")
    org_id = owner.create_org("Acme")["id"]
    manager = actor("cm@example.com", "Carla Compliance")
    member = actor("member@example.com", "Mia Member")
    viewer = actor("viewer@example.com", "Vic Viewer")
    ids = {
        "manager": add_member(owner, org_id, manager, "compliance_manager"),
        "member": add_member(owner, org_id, member, "member"),
        "viewer": add_member(owner, org_id, viewer, "viewer"),
    }
    return {"owner": owner, "manager": manager, "member": member, "viewer": viewer, "org": org_id, "ids": ids}


def _calendar(actor_, org_id, start, end, **params):
    return actor_.get(
        f"/organizations/{org_id}/calendar",
        params={"start": start.isoformat(), "end": end.isoformat(), **params},
    )


# --------------------------------------------------------------------- calendar


def test_calendar_shows_expiries_and_renewal_deadlines(team):
    owner, org = team["owner"], team["org"]
    licence = owner.uploaded(org, name="Trade licence", expiry_date=in_days(20))
    owner.uploaded(org, name="Old permit", expiry_date=in_days(-3))
    owner.uploaded(org, name="Far away", expiry_date=in_days(200))
    renewal = team["manager"].post(
        f"/organizations/{org}/renewals",
        json={
            "document_id": licence["id"],
            "deadline": in_days(10),
            "assignee_membership_id": team["ids"]["member"],
        },
    )
    assert renewal.status_code == 201, renewal.text

    response = _calendar(team["viewer"], org, today() - timedelta(days=7), today() + timedelta(days=30))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["today"] == today().isoformat()
    events = [(event["type"], event["document_name"], event["date"]) for event in body["events"]]
    assert events == [
        ("expired", "Old permit", in_days(-3)),
        ("renewal_deadline", "Trade licence", in_days(10)),
        ("expiry", "Trade licence", in_days(20)),
    ]
    deadline = body["events"][1]
    assert deadline["renewal_id"] == renewal.json()["id"]
    assert deadline["owner_name"] == "Mia Member"

    only_deadlines = _calendar(
        owner, org, today(), today() + timedelta(days=30), types=["renewal_deadline"]
    ).json()["events"]
    assert [event["type"] for event in only_deadlines] == ["renewal_deadline"]

    mine = _calendar(owner, org, today(), today() + timedelta(days=30), owner=team["ids"]["member"]).json()
    assert [event["type"] for event in mine["events"]] == ["renewal_deadline"]


def test_calendar_filters_by_category_and_location(team):
    owner, org = team["owner"], team["org"]
    office = owner.create_entity(org, "location", "Head office")
    supplier = owner.create_entity(org, "supplier", "Globex", location_id=office["id"])
    owner.uploaded(org, name="Office permit", expiry_date=in_days(5), entity_id=office["id"])
    owner.uploaded(org, name="Supplier insurance", category="insurance_policy", expiry_date=in_days(6),
                   entity_id=supplier["id"])  # fmt: skip
    owner.uploaded(org, name="Elsewhere", expiry_date=in_days(7))

    start, end = today(), today() + timedelta(days=14)
    at_office = _calendar(owner, org, start, end, location=office["id"]).json()["events"]
    assert {event["document_name"] for event in at_office} == {"Office permit", "Supplier insurance"}
    insurance = _calendar(owner, org, start, end, category="insurance_policy").json()["events"]
    assert [event["document_name"] for event in insurance] == ["Supplier insurance"]


def test_calendar_range_is_limited(team):
    owner, org = team["owner"], team["org"]
    too_long = _calendar(owner, org, today(), today() + timedelta(days=90))
    assert too_long.status_code == 422
    assert error_code(too_long) == "range_too_long"
    backwards = _calendar(owner, org, today(), today() - timedelta(days=1))
    assert error_code(backwards) == "invalid_range"


def test_calendar_is_tenant_isolated(team, actor):
    team["owner"].uploaded(team["org"], name="Secret", expiry_date=in_days(3))
    outsider = actor("outsider@example.com")
    own_org = outsider.create_org("Other")["id"]
    assert _calendar(outsider, team["org"], today(), today() + timedelta(days=10)).status_code == 404
    assert _calendar(outsider, own_org, today(), today() + timedelta(days=10)).json()["events"] == []


# ---------------------------------------------------------------------- reports


def test_report_catalog_shows_what_the_plan_includes(team):
    owner, org = team["owner"], team["org"]
    set_plan(org, "starter")
    catalog = owner.get(f"/organizations/{org}/reports").json()
    allowed = {report["key"]: report["allowed"] for report in catalog["reports"]}
    assert allowed["expiring"] is True
    assert allowed["by_supplier"] is False
    assert catalog["pdf_export"] is True and catalog["csv_export"] is False
    assert catalog["can_export"] is True
    expiring = next(report for report in catalog["reports"] if report["key"] == "expiring")
    assert expiring["default_start"] == today().isoformat()
    assert team["viewer"].get(f"/organizations/{org}/reports").json()["can_export"] is False


def test_expiring_report(team):
    owner, org = team["owner"], team["org"]
    owner.uploaded(org, name="Soon", expiry_date=in_days(10), document_number="P-1")
    owner.uploaded(org, name="Later", expiry_date=in_days(60))
    owner.uploaded(org, name="Much later", expiry_date=in_days(120))

    report = team["viewer"].get(f"/organizations/{org}/reports/expiring").json()
    assert [row["name"] for row in report["rows"]] == ["Soon", "Later"]
    assert report["rows"][0]["days_left"] == 10
    assert report["rows"][0]["number"] == "P-1"
    assert report["summary"][0] == {"label": "Documents expiring", "value": 2}
    assert report["start"] == today().isoformat()

    wider = owner.get(
        f"/organizations/{org}/reports/expiring", params={"start": in_days(0), "end": in_days(200)}
    ).json()
    assert len(wider["rows"]) == 3


def test_every_report_runs(team):
    owner, org = team["owner"], team["org"]
    supplier = owner.create_entity(org, "supplier", "Globex")
    owner.uploaded(org, name="Insurance", expiry_date=in_days(-5), entity_id=supplier["id"])
    owner.uploaded(org, name="Permit", expiry_date=in_days(40))
    catalog = owner.get(f"/organizations/{org}/reports").json()["reports"]
    for report in catalog:
        response = owner.get(f"/organizations/{org}/reports/{report['key']}")
        assert response.status_code == 200, (report["key"], response.text)
        for format_, kind in (("csv", "text/csv"), ("pdf", "application/pdf")):
            export = owner.get(f"/organizations/{org}/reports/{report['key']}", params={"format": format_})
            assert export.status_code == 200, (report["key"], format_, export.text)
            assert export.headers["content-type"].startswith(kind)
            assert "attachment" in export.headers["content-disposition"]
    assert owner.get(f"/organizations/{org}/reports/nonexistent").status_code == 404


def test_csv_export_neutralises_formulas(team):
    owner, org = team["owner"], team["org"]
    owner.uploaded(org, name='=HYPERLINK("http://evil.example")', expiry_date=in_days(5))
    owner.uploaded(org, name="Café licence", expiry_date=in_days(6))
    response = owner.get(f"/organizations/{org}/reports/expiring", params={"format": "csv"})
    assert response.content.startswith(b"\xef\xbb\xbf")  # BOM so Excel reads UTF-8
    rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert rows[0][:2] == ["Document", "Type"]
    assert rows[1][0] == '\'=HYPERLINK("http://evil.example")'
    assert rows[2][0] == "Café licence"


def test_pdf_export_is_a_pdf(team):
    owner, org = team["owner"], team["org"]
    owner.uploaded(org, name="Licence — main site …", expiry_date=in_days(5))
    response = owner.get(f"/organizations/{org}/reports/expiring", params={"format": "pdf"})
    assert response.status_code == 200
    assert response.content.startswith(b"%PDF-")


def test_exports_need_permission_and_plan(team):
    owner, org = team["owner"], team["org"]
    viewer = team["viewer"]
    assert viewer.get(f"/organizations/{org}/reports/expiring", params={"format": "csv"}).status_code == 403

    set_plan(org, "starter")
    csv_export = owner.get(f"/organizations/{org}/reports/expiring", params={"format": "csv"})
    assert csv_export.status_code == 402
    assert csv_export.json()["error"]["details"]["feature"] == "csv_export"
    assert owner.get(f"/organizations/{org}/reports/expiring", params={"format": "pdf"}).status_code == 200
    assert owner.get(f"/organizations/{org}/reports/by_supplier").status_code == 402

    set_plan(org, "free")
    assert owner.get(f"/organizations/{org}/reports/expiring").status_code == 402


def test_exports_are_audited(team):
    owner, org = team["owner"], team["org"]
    owner.get(f"/organizations/{org}/reports/expiring", params={"format": "pdf"})
    owner.get(f"/organizations/{org}/reports/expiring")  # viewing is not audited
    entries = owner.get(f"/organizations/{org}/audit-logs", params={"action": "report."}).json()["items"]
    assert len(entries) == 1
    assert entries[0]["metadata"]["format"] == "pdf"


def test_report_period_is_validated(team):
    owner, org = team["owner"], team["org"]
    backwards = owner.get(
        f"/organizations/{org}/reports/expiring", params={"start": in_days(10), "end": in_days(1)}
    )
    assert error_code(backwards) == "invalid_range"
    long = owner.get(
        f"/organizations/{org}/reports/activity", params={"start": in_days(-2000), "end": in_days(0)}
    )
    assert error_code(long) == "range_too_long"


def test_reports_only_include_the_organizations_own_data(team, actor):
    team["owner"].uploaded(team["org"], name="Acme permit", expiry_date=in_days(5))
    outsider = actor("outsider@example.com")
    other = outsider.create_org("Other")["id"]
    outsider.uploaded(other, name="Other permit", expiry_date=in_days(5))
    rows = team["owner"].get(f"/organizations/{team['org']}/reports/expiring").json()["rows"]
    assert [row["name"] for row in rows] == ["Acme permit"]
    assert outsider.get(f"/organizations/{team['org']}/reports/expiring").status_code == 404


# ------------------------------------------------------------------ audit filters


def test_audit_log_filters(team):
    owner, manager, org = team["owner"], team["manager"], team["org"]
    owner.uploaded(org, name="By owner")
    manager.uploaded(org, name="By manager")
    url = f"/organizations/{org}/audit-logs"

    by_manager = owner.get(url, params={"actor": manager.user["id"]}).json()["items"]
    assert by_manager and all(entry["actor"]["email"] == "cm@example.com" for entry in by_manager)

    documents = owner.get(url, params={"resource_type": "document"}).json()["items"]
    assert {entry["action"] for entry in documents} == {"document.uploaded"}

    assert owner.get(url, params={"start": in_days(0), "end": in_days(0)}).json()["total"] > 0
    assert owner.get(url, params={"start": in_days(1)}).json()["total"] == 0
    assert owner.get(url, params={"end": in_days(-1)}).json()["total"] == 0
    assert error_code(owner.get(url, params={"start": in_days(1), "end": in_days(0)})) == "invalid_range"
    assert owner.get(url, params={"resource_type": "Document;"}).status_code == 422
