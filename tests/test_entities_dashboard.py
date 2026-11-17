"""Entities, dashboard figures and global search."""

import uuid
from datetime import timedelta

import pytest

from tests.helpers import add_member, in_days, today


@pytest.fixture
def org(actor):
    owner = actor("owner@example.com", "Olivia Owner")
    return owner, owner.create_org("Acme")["id"]


def _entities(actor_, org_id, **params):
    response = actor_.get(f"/organizations/{org_id}/entities", params=params)
    assert response.status_code == 200, response.text
    return response.json()


# -------------------------------------------------------------------- entities


def test_create_each_kind_of_entity(org):
    owner, org_id = org

    for type_ in ("employee", "supplier", "equipment", "location", "department"):
        body = owner.create_entity(org_id, type_, f"Test {type_}")
        assert body["type"] == type_
        assert body["document_count"] == 0
        assert body["archived_at"] is None

    counts = owner.get(f"/organizations/{org_id}/entities/counts").json()
    assert [(item["type"], item["count"]) for item in counts] == [
        ("employee", 1),
        ("supplier", 1),
        ("equipment", 1),
        ("location", 1),
        ("department", 1),
    ]


def test_entity_details_are_stored_and_trimmed(org):
    owner, org_id = org
    membership = owner.membership_id(org_id, owner.email)

    body = owner.create_entity(
        org_id,
        "supplier",
        "  Globex Supplies  ",
        code="SUP-014",
        email="accounts@globex.example",
        phone="+91 98765 43210",
        contact_name="Hank Scorpio",
        address="12 Industrial Road",
        notes="Preferred vendor.",
        owner_membership_id=membership,
    )

    assert body["name"] == "Globex Supplies"
    assert body["code"] == "SUP-014"
    assert body["contact_name"] == "Hank Scorpio"
    assert body["owner"]["email"] == "owner@example.com"


def test_entity_validation(org, actor):
    owner, org_id = org
    url = f"/organizations/{org_id}/entities"
    stranger = actor("stranger@example.com")
    other_org = stranger.create_org("Other")["id"]
    foreign_location = stranger.create_entity(other_org, "location", "Their warehouse")["id"]

    assert owner.post(url, json={"type": "vehicle", "name": "Van"}).status_code == 422
    assert owner.post(url, json={"type": "supplier", "name": "   "}).status_code == 422
    assert owner.post(url, json={"type": "supplier", "name": "A", "email": "not-an-email"}).status_code == 422
    foreign = owner.post(url, json={"type": "employee", "name": "Ann", "location_id": foreign_location})
    assert foreign.status_code == 422
    assert foreign.json()["error"]["details"][0]["field"] == "location_id"
    assert _entities(owner, org_id)["total"] == 0


def test_location_and_department_links(org):
    owner, org_id = org
    warehouse = owner.create_entity(org_id, "location", "North Warehouse")
    maintenance = owner.create_entity(org_id, "department", "Maintenance")
    supplier = owner.create_entity(org_id, "supplier", "Globex")

    forklift = owner.create_entity(
        org_id, "equipment", "Forklift 3", location_id=warehouse["id"], department_id=maintenance["id"]
    )
    assert forklift["location"] == {"id": warehouse["id"], "name": "North Warehouse", "type": "location"}
    assert forklift["department"]["name"] == "Maintenance"

    url = f"/organizations/{org_id}/entities"
    # A link must point at a record of the right kind, and a location cannot sit inside a location.
    wrong_kind = owner.post(url, json={"type": "employee", "name": "Ann", "location_id": supplier["id"]})
    nested = owner.post(url, json={"type": "location", "name": "Bay 2", "location_id": warehouse["id"]})
    assert wrong_kind.status_code == 422
    assert nested.status_code == 422

    cleared = owner.patch(f"{url}/{forklift['id']}", json={"location_id": None}).json()
    assert cleared["location"] is None
    assert cleared["department"]["name"] == "Maintenance"


def test_list_filters_search_lookup_and_document_counts(org):
    owner, org_id = org
    globex = owner.create_entity(org_id, "supplier", "Globex Supplies", code="SUP-014")
    owner.create_entity(org_id, "supplier", "Initech")
    owner.create_entity(org_id, "employee", "Ann Globe")
    owner.uploaded(org_id, entity_id=globex["id"], expiry_date=in_days(-5))
    owner.uploaded(org_id, entity_id=globex["id"], expiry_date=in_days(10))
    owner.uploaded(org_id, entity_id=globex["id"], expiry_date=in_days(300))
    archived = owner.uploaded(org_id, entity_id=globex["id"])
    owner.post(f"/organizations/{org_id}/documents/{archived['id']}/archive")

    suppliers = _entities(owner, org_id, type="supplier")
    assert [item["name"] for item in suppliers["items"]] == ["Globex Supplies", "Initech"]
    first = suppliers["items"][0]
    assert (first["document_count"], first["expiring_soon_count"], first["expired_count"]) == (3, 1, 1)
    assert suppliers["items"][1]["document_count"] == 0

    assert [item["name"] for item in _entities(owner, org_id, q="glob")["items"]] == [
        "Ann Globe",
        "Globex Supplies",
    ]
    assert [item["name"] for item in _entities(owner, org_id, q="sup-014")["items"]] == ["Globex Supplies"]

    lookup = owner.get(
        f"/organizations/{org_id}/entities/lookup", params={"type": "supplier", "q": "ini"}
    ).json()
    assert lookup == [{"id": suppliers["items"][1]["id"], "name": "Initech", "type": "supplier"}]

    detail = owner.get(f"/organizations/{org_id}/entities/{globex['id']}").json()
    assert detail["document_count"] == 3


def test_update_archive_and_restore(org):
    owner, org_id = org
    entity = owner.create_entity(org_id, "employee", "Ann Lee")
    url = f"/organizations/{org_id}/entities/{entity['id']}"

    updated = owner.patch(url, json={"name": "Ann Lee-Park", "phone": "555-0100", "notes": ""}).json()
    assert updated["name"] == "Ann Lee-Park"
    assert updated["phone"] == "555-0100"
    assert owner.patch(url, json={"name": ""}).status_code == 422

    assert owner.post(f"{url}/archive").json()["archived_at"] is not None
    assert _entities(owner, org_id)["total"] == 0
    assert _entities(owner, org_id, archived="true")["total"] == 1
    assert owner.get(f"/organizations/{org_id}/entities/lookup").json() == []
    # An archived record cannot be attached to a new document.
    assert owner.upload(org_id, entity_id=entity["id"]).status_code == 422

    assert owner.post(f"{url}/restore").json()["archived_at"] is None
    assert owner.upload(org_id, entity_id=entity["id"]).status_code == 201


def test_entity_permissions_and_audit(org, actor):
    owner, org_id = org
    viewer, member = actor("viewer@example.com"), actor("member@example.com")
    add_member(owner, org_id, viewer, "viewer")
    add_member(owner, org_id, member, "member")
    url = f"/organizations/{org_id}/entities"

    assert viewer.post(url, json={"type": "supplier", "name": "X"}).status_code == 403
    created = member.post(url, json={"type": "supplier", "name": "Globex"})
    assert created.status_code == 201
    entity_id = created.json()["id"]
    assert viewer.get(f"{url}/{entity_id}").status_code == 200
    assert viewer.patch(f"{url}/{entity_id}", json={"name": "Y"}).status_code == 403
    assert viewer.post(f"{url}/{entity_id}/archive").status_code == 403

    member.patch(f"{url}/{entity_id}", json={"code": "S-1"})
    member.patch(f"{url}/{entity_id}", json={"code": "S-1"})  # no change, no entry
    entries = owner.get(f"/organizations/{org_id}/audit-logs", params={"action": "entity."}).json()["items"]
    assert [entry["action"] for entry in entries] == ["entity.updated", "entity.created"]
    assert entries[0]["metadata"] == {"name": "Globex", "type": "supplier", "fields": ["code"]}


def test_entities_are_isolated_between_organizations(org, actor):
    owner, org_id = org
    entity = owner.create_entity(org_id, "supplier", "Secret Supplier")
    intruder = actor("intruder@evil.example")
    other_org = intruder.create_org("Evil Corp")["id"]

    assert intruder.get(f"/organizations/{org_id}/entities").status_code == 404
    own = f"/organizations/{other_org}/entities"
    assert intruder.get(f"{own}/{entity['id']}").status_code == 404
    assert intruder.patch(f"{own}/{entity['id']}", json={"name": "Mine"}).status_code == 404
    assert intruder.post(f"{own}/{entity['id']}/archive").status_code == 404
    assert _entities(intruder, other_org, q="secret")["total"] == 0
    assert intruder.get(f"{own}/lookup").json() == []
    assert intruder.get(f"{own}/{uuid.uuid4()}").status_code == 404
    assert owner.get(f"/organizations/{org_id}/entities/{entity['id']}").json()["name"] == "Secret Supplier"


# ------------------------------------------------------------------- dashboard


def test_dashboard_of_an_empty_organization_is_all_zeros(org):
    owner, org_id = org

    body = owner.get(f"/organizations/{org_id}/dashboard").json()

    assert body["metrics"] == {
        "total": 0,
        "valid": 0,
        "expiring_soon": 0,
        "expired": 0,
        "pending_verification": 0,
        "archived": 0,
        "suggestions_to_review": 0,
        "renewals_in_progress": 0,
        "renewals_overdue": 0,
        "renewals_awaiting_approval": 0,
    }
    assert body["by_category"] == []
    assert len(body["upcoming_expirations"]) == 6
    assert all(month["count"] == 0 for month in body["upcoming_expirations"])
    assert body["by_group"] == []
    assert body["expiring_documents"] == body["expired_documents"] == body["recent_documents"] == []
    assert body["expiring_window_days"] == 30


def test_dashboard_figures_match_the_documents(org):
    owner, org_id = org
    warehouse = owner.create_entity(org_id, "location", "North Warehouse")
    office = owner.create_entity(org_id, "department", "Operations")
    forklift = owner.create_entity(org_id, "equipment", "Forklift 3", location_id=warehouse["id"])
    ann = owner.create_entity(org_id, "employee", "Ann Lee", department_id=office["id"])
    freelancer = owner.create_entity(org_id, "employee", "Bo Free")

    owner.uploaded(
        org_id, name="Expired A", category="permit", expiry_date=in_days(-40), entity_id=warehouse["id"]
    )
    owner.uploaded(
        org_id, name="Expired B", category="permit", expiry_date=in_days(-2), entity_id=forklift["id"]
    )
    owner.uploaded(
        org_id, name="Soon A", category="insurance_policy", expiry_date=in_days(3), entity_id=ann["id"]
    )
    owner.uploaded(
        org_id, name="Soon B", category="permit", expiry_date=in_days(30), entity_id=freelancer["id"]
    )
    valid = owner.uploaded(org_id, name="Valid A", category="business_license", expiry_date=in_days(75))
    owner.uploaded(org_id, name="No expiry", category="other")
    gone = owner.uploaded(org_id, name="Archived", category="permit", expiry_date=in_days(-100))
    owner.post(f"/organizations/{org_id}/documents/{valid['id']}/verify")
    owner.post(f"/organizations/{org_id}/documents/{gone['id']}/archive")

    body = owner.get(f"/organizations/{org_id}/dashboard").json()

    assert body["metrics"] == {
        "total": 6,
        "valid": 2,
        "expiring_soon": 2,
        "expired": 2,
        "pending_verification": 5,
        "archived": 1,
        # The sample files have no readable text, so there are no suggestions to review.
        "suggestions_to_review": 0,
        "renewals_in_progress": 0,
        "renewals_overdue": 0,
        "renewals_awaiting_approval": 0,
    }
    assert [(item["category"], item["count"]) for item in body["by_category"]] == [
        ("permit", 3),
        ("business_license", 1),
        ("insurance_policy", 1),
        ("other", 1),
    ]
    assert body["by_category"][0]["label"] == "Permits"

    # Location: one document attached directly, one through the forklift. Department: one through Ann.
    assert [(group["name"], group["type"], group["count"]) for group in body["by_group"]] == [
        ("North Warehouse", "location", 2),
        ("Operations", "department", 1),
    ]
    assert body["ungrouped_count"] == 3

    assert [item["name"] for item in body["expiring_documents"]] == ["Soon A", "Soon B", "Valid A"]
    assert [item["name"] for item in body["expired_documents"]] == ["Expired B", "Expired A"]
    assert body["recent_documents"][0]["name"] == "No expiry"
    assert len(body["recent_documents"]) == 5

    # Upcoming expirations: every dated, unexpired document within the six-month window is counted once.
    months = {month["month"]: month["count"] for month in body["upcoming_expirations"]}
    assert sum(months.values()) == 3
    for days in (3, 30, 75):
        day = today() + timedelta(days=days)
        assert months[f"{day.year:04d}-{day.month:02d}"] >= 1
    assert list(months)[0] == f"{today().year:04d}-{today().month:02d}"


def test_dashboard_uses_the_organizations_time_zone(org, monkeypatch):
    """Late evening UTC is tomorrow in Tokyo: a document expiring 'today' in UTC has expired there."""
    from datetime import UTC, datetime

    from app.services import expiry

    owner, org_id = org
    moment = datetime(2026, 3, 10, 22, 30, tzinfo=UTC)
    monkeypatch.setattr(expiry, "utcnow", lambda: moment)
    owner.uploaded(org_id, expiry_date="2026-03-10")

    utc_view = owner.get(f"/organizations/{org_id}/dashboard").json()
    owner.patch(f"/organizations/{org_id}", json={"timezone": "Asia/Tokyo"})
    tokyo_view = owner.get(f"/organizations/{org_id}/dashboard").json()

    assert utc_view["today"] == "2026-03-10"
    assert utc_view["metrics"]["expiring_soon"] == 1
    assert tokyo_view["today"] == "2026-03-11"
    assert tokyo_view["metrics"]["expired"] == 1


def test_every_role_can_read_the_dashboard(org, actor):
    owner, org_id = org
    viewer = actor("viewer@example.com")
    add_member(owner, org_id, viewer, "viewer")

    assert viewer.get(f"/organizations/{org_id}/dashboard").status_code == 200


# ---------------------------------------------------------------------- search


def test_search_finds_documents_and_entities(org):
    owner, org_id = org
    owner.create_entity(org_id, "supplier", "Globex Supplies")
    owner.uploaded(org_id, name="Globex liability insurance", document_number="POL-778")
    owner.uploaded(org_id, name="Trade licence")
    hidden = owner.uploaded(org_id, name="Globex old contract")
    owner.post(f"/organizations/{org_id}/documents/{hidden['id']}/archive")

    body = owner.get(f"/organizations/{org_id}/search", params={"q": "globex"}).json()
    by_number = owner.get(f"/organizations/{org_id}/search", params={"q": "pol-7"}).json()

    assert [item["name"] for item in body["documents"]] == ["Globex liability insurance"]
    assert [item["name"] for item in body["entities"]] == ["Globex Supplies"]
    assert [item["name"] for item in by_number["documents"]] == ["Globex liability insurance"]
    assert owner.get(f"/organizations/{org_id}/search", params={"q": "g"}).status_code == 422
    assert owner.get(f"/organizations/{org_id}/search", params={"q": "zzz"}).json() == {
        "documents": [],
        "entities": [],
    }
