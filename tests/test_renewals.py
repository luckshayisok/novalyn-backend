"""Renewal workflow: request, upload, approval, rejection, history, permissions."""

import pytest

from tests.helpers import API, PNG_BYTES, add_member, emails_to, error_code, in_days
from tests.samples import text_pdf

RENEWED = text_pdf(["TRADE LICENCE", "Licence No: TL-55-2027", "Expiry date: 30 June 2028"])


@pytest.fixture
def team(actor):
    owner = actor("owner@example.com", "Olivia Owner")
    org_id = owner.create_org("Acme")["id"]
    manager = actor("cm@example.com", "Carla Compliance")
    member = actor("member@example.com", "Mia Member")
    other = actor("other@example.com", "Otto Other")
    viewer = actor("viewer@example.com", "Vic Viewer")
    ids = {
        "manager": add_member(owner, org_id, manager, "compliance_manager"),
        "member": add_member(owner, org_id, member, "member"),
        "other": add_member(owner, org_id, other, "member"),
        "viewer": add_member(owner, org_id, viewer, "viewer"),
    }
    document = owner.uploaded(org_id, name="Trade licence", expiry_date=in_days(20), issue_date=in_days(-345))
    return {
        "owner": owner,
        "manager": manager,
        "member": member,
        "other": other,
        "viewer": viewer,
        "org": org_id,
        "ids": ids,
        "document": document,
    }


def _url(team, renewal_id="", suffix=""):
    base = f"/organizations/{team['org']}/renewals"
    return f"{base}/{renewal_id}{suffix}" if renewal_id else base


def _create(team, actor_=None, **body):
    actor_ = actor_ or team["manager"]
    payload = {"document_id": team["document"]["id"], **body}
    return actor_.post(_url(team), json=payload)


def _upload(actor_, team, renewal_id, content=RENEWED, filename="licence-2027.pdf", **fields):
    return actor_.client.post(
        f"{API}/organizations/{team['org']}/renewals/{renewal_id}/file",
        headers=actor_.headers,
        data={key: value for key, value in fields.items() if value is not None},
        files={"file": (filename, content, "application/pdf")},
    )


def _document(team):
    return team["owner"].get(f"/organizations/{team['org']}/documents/{team['document']['id']}").json()


def _requested(team):
    response = _create(team, assignee_membership_id=team["ids"]["member"], request=True)
    assert response.status_code == 201, response.text
    return response.json()


def test_the_whole_renewal(team):
    manager, member, owner = team["manager"], team["member"], team["owner"]

    renewal = _requested(team)
    assert renewal["status"] == "requested"
    assert renewal["deadline"] == team["document"]["expiry_date"]  # defaults to the expiry date
    assert renewal["previous_expiry_date"] == team["document"]["expiry_date"]
    assert renewal["assignee"]["email"] == "member@example.com"
    assert emails_to("member@example.com")[-1].subject.startswith("Please renew Trade licence")
    assert _document(team)["renewal"]["status"] == "requested"

    started = member.post(_url(team, renewal["id"], "/start"))
    assert started.status_code == 200
    assert started.json()["status"] == "in_progress"
    assert started.json()["can"]["upload"] is True
    assert started.json()["can"]["approve"] is False

    uploaded = _upload(
        member, team, renewal["id"], expiry_date=in_days(385), note="New licence from the council"
    )
    assert uploaded.status_code == 200, uploaded.text
    body = uploaded.json()
    assert body["status"] == "submitted"
    assert body["new_version"]["state"] == "pending"
    assert body["proposed_expiry_date"] == in_days(385)
    assert body["suggested_expiry_date"] == "2028-06-30"  # read from the file by the rule-based reader
    # The renewed file is not the current one until the renewal is approved.
    document = _document(team)
    assert document["current_version"]["id"] == team["document"]["current_version"]["id"]
    assert document["expiry_date"] == team["document"]["expiry_date"]

    submitted = member.post(_url(team, renewal["id"], "/submit"), json={"note": "Ready"})
    assert submitted.json()["status"] == "awaiting_approval"
    assert emails_to("cm@example.com")[-1].subject.startswith("Renewal of Trade licence needs approval")

    approved = manager.post(_url(team, renewal["id"], "/approve"))
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "completed"
    assert approved.json()["completed_by_name"] == "Carla Compliance"
    document = _document(team)
    assert document["current_version"]["id"] == body["new_version"]["id"]
    assert document["expiry_date"] == in_days(385)
    assert document["status"] == "valid"
    assert document["verification_status"] == "verified"
    assert document["renewal"] is None
    assert [
        v["state"]
        for v in owner.get(f"/organizations/{team['org']}/documents/{team['document']['id']}").json()[
            "versions"
        ]
    ] == ["active", "active"]
    assert emails_to("member@example.com")[-1].subject.startswith("Renewal of Trade licence approved")

    history = [(e["action"], e["to_status"]) for e in approved.json()["events"]]
    assert history == [
        ("created", "not_started"),
        ("requested", "requested"),
        ("started", "in_progress"),
        ("file_uploaded", "submitted"),
        ("submitted", "awaiting_approval"),
        ("approved", "completed"),
    ]
    actions = [
        e["action"]
        for e in owner.get(f"/organizations/{team['org']}/audit-logs", params={"action": "renewal."}).json()[
            "items"
        ]
    ]
    assert actions == ["renewal.completed", "renewal.submitted", "renewal.file_uploaded", "renewal.created"]


def test_rejection_keeps_the_file_and_can_be_reopened(team):
    manager, member = team["manager"], team["member"]
    renewal = _requested(team)
    first = _upload(member, team, renewal["id"], expiry_date=in_days(385)).json()["new_version"]

    rejected = manager.post(_url(team, renewal["id"], "/reject"), json={"reason": "Wrong company name"})

    assert rejected.json()["status"] == "rejected"
    assert rejected.json()["rejection_reason"] == "Wrong company name"
    assert emails_to("member@example.com")[-1].subject.startswith("Renewal of Trade licence sent back")
    versions = (
        {v["id"]: v["state"] for v in _document(team)["versions"]} if "versions" in _document(team) else None
    )
    detail = team["owner"].get(f"/organizations/{team['org']}/documents/{team['document']['id']}").json()
    assert {v["id"]: v["state"] for v in detail["versions"]}[first["id"]] == "rejected"
    assert detail["current_version"]["id"] == team["document"]["current_version"]["id"]
    assert versions is None or versions[first["id"]] == "rejected"

    reopened = manager.post(_url(team, renewal["id"], "/reopen"))
    assert reopened.json()["status"] == "in_progress"
    again = _upload(member, team, renewal["id"], expiry_date=in_days(385)).json()
    assert again["new_version"]["version_number"] == 3  # numbers are never reused


def test_uploading_twice_keeps_both_files(team):
    member = team["member"]
    renewal = _requested(team)
    first = _upload(member, team, renewal["id"]).json()["new_version"]
    second = _upload(member, team, renewal["id"], filename="better-scan.pdf").json()["new_version"]

    detail = team["owner"].get(f"/organizations/{team['org']}/documents/{team['document']['id']}").json()
    states = {v["id"]: v["state"] for v in detail["versions"]}
    assert states[first["id"]] == "rejected"
    assert states[second["id"]] == "pending"


def test_rules_on_dates_and_files(team):
    member = team["member"]
    renewal = _requested(team)

    earlier = _upload(member, team, renewal["id"], expiry_date=in_days(10))
    backwards = _upload(member, team, renewal["id"], issue_date=in_days(400), expiry_date=in_days(390))
    bad_file = _upload(member, team, renewal["id"], content=PNG_BYTES, filename="fake.pdf")
    no_file = member.post(_url(team, renewal["id"], "/submit"))

    assert earlier.status_code == 422
    assert backwards.status_code == 422
    assert error_code(bad_file) == "file_content_mismatch"
    assert error_code(no_file) == "renewal_wrong_status"

    _upload(member, team, renewal["id"])  # no new expiry date given
    missing = member.post(_url(team, renewal["id"], "/submit"))
    assert error_code(missing) == "renewal_no_expiry"


def test_one_open_renewal_per_document_and_no_direct_replacement_meanwhile(team):
    _requested(team)

    duplicate = _create(team)
    replace = team["owner"].client.post(
        f"{API}/organizations/{team['org']}/documents/{team['document']['id']}/versions",
        headers=team["owner"].headers,
        files={"file": ("x.pdf", RENEWED, "application/pdf")},
    )

    assert error_code(duplicate) == "renewal_exists"
    assert error_code(replace) == "renewal_in_progress"


def test_permissions(team):
    manager, member, other, viewer = team["manager"], team["member"], team["other"], team["viewer"]
    viewer_assign = _create(team, assignee_membership_id=team["ids"]["viewer"])
    assert viewer_assign.status_code == 422  # viewers cannot be assigned

    assert _create(team, actor_=member).status_code == 403
    renewal = _requested(team)

    assert viewer.get(_url(team, renewal["id"])).status_code == 200
    assert viewer.post(_url(team, renewal["id"], "/start")).status_code == 403
    assert other.post(_url(team, renewal["id"], "/start")).status_code == 403  # not the assignee
    assert _upload(other, team, renewal["id"]).status_code == 403
    _upload(member, team, renewal["id"], expiry_date=in_days(385))
    assert member.post(_url(team, renewal["id"], "/approve")).status_code == 403
    assert member.post(_url(team, renewal["id"], "/reject"), json={"reason": "x"}).status_code == 403
    # A manager can do the assignee's steps too.
    assert manager.post(_url(team, renewal["id"], "/submit")).status_code == 200
    assert manager.post(_url(team, renewal["id"], "/approve")).status_code == 200
    assert error_code(manager.post(_url(team, renewal["id"], "/approve"))) == "renewal_wrong_status"


def test_reassigning_notifies_the_new_assignee(team):
    renewal = _requested(team)

    response = team["manager"].patch(
        _url(team, renewal["id"]),
        json={"assignee_membership_id": team["ids"]["other"], "deadline": in_days(15)},
    )

    assert response.json()["assignee"]["email"] == "other@example.com"
    assert response.json()["deadline"] == in_days(15)
    assert emails_to("other@example.com")[-1].subject.startswith("Please renew Trade licence")


def test_list_filters_and_dashboard(team):
    owner, manager, member = team["owner"], team["manager"], team["member"]
    late = owner.uploaded(team["org"], name="Late permit", expiry_date=in_days(-3))
    _requested(team)
    overdue = manager.post(_url(team), json={"document_id": late["id"], "deadline": in_days(-1)}).json()

    def ids(**params):
        return [item["id"] for item in owner.get(_url(team), params=params).json()["items"]]

    assert len(ids()) == 2  # open by default
    assert ids(status="overdue") == [overdue["id"]]
    assert ids(status="closed") == []
    assert len(member.get(_url(team), params={"mine": "true"}).json()["items"]) == 1

    dashboard = owner.get(f"/organizations/{team['org']}/dashboard").json()
    assert dashboard["metrics"]["renewals_in_progress"] == 2
    assert dashboard["metrics"]["renewals_overdue"] == 1
    assert dashboard["open_renewals"][0]["document_name"] == "Late permit"
    assert sum(month["started"] for month in dashboard["renewal_trend"]) == 2
    assert len(dashboard["renewal_trend"]) == 6


def test_renewals_are_isolated_between_organizations(team, actor):
    renewal = _requested(team)
    intruder = actor("intruder@evil.example")
    other_org = intruder.create_org("Evil")["id"]

    assert intruder.get(_url(team, renewal["id"])).status_code == 404
    assert intruder.get(f"/organizations/{other_org}/renewals/{renewal['id']}").status_code == 404
    assert intruder.post(f"/organizations/{other_org}/renewals/{renewal['id']}/approve").status_code == 404
    response = intruder.post(
        f"/organizations/{other_org}/renewals", json={"document_id": team["document"]["id"]}
    )
    assert response.status_code == 404
