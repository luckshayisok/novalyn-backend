"""Organizations: creation, settings, onboarding, reference options."""

from tests.helpers import API, add_member, error_code


def test_create_organization_makes_the_creator_owner(actor):
    ada = actor("ada@example.com")

    response = ada.post(
        "/organizations",
        json={"name": "  Acme Facilities  ", "industry": "facility_management", "company_size": "11-50"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Acme Facilities"
    assert body["slug"] == "acme-facilities"
    assert body["role"] == "owner"
    assert body["industry"] == "facility_management"
    assert body["timezone"] == "UTC"
    assert body["document_categories"] == []
    assert body["onboarding_completed_at"] is None
    assert "billing:manage" in body["permissions"]
    assert "organization:delete" in body["permissions"]


def test_organizations_with_the_same_name_get_distinct_slugs(actor):
    ada = actor("ada@example.com")
    bob = actor("bob@example.com")

    first = ada.create_org("Acme")
    second = bob.create_org("Acme")

    assert first["slug"] == "acme"
    assert second["slug"].startswith("acme-")
    assert second["slug"] != first["slug"]


def test_create_organization_requires_authentication(client):
    response = client.post(f"{API}/organizations", json={"name": "Acme"})

    assert response.status_code == 401


def test_create_organization_validates_input(actor):
    ada = actor("ada@example.com")

    assert ada.post("/organizations", json={"name": "   "}).status_code == 422
    assert ada.post("/organizations", json={"name": "Acme", "industry": "astrology"}).status_code == 422
    assert ada.post("/organizations", json={"name": "Acme", "company_size": "huge"}).status_code == 422
    assert ada.post("/organizations", json={"name": "Acme", "timezone": "Mars/Olympus"}).status_code == 422


def test_list_organizations_returns_only_the_callers(actor):
    ada = actor("ada@example.com")
    bob = actor("bob@example.com")
    ada_org = ada.create_org("Ada Works")
    bob.create_org("Bob Works")

    response = ada.get("/organizations")

    assert response.status_code == 200
    assert [org["id"] for org in response.json()] == [ada_org["id"]]


def test_update_organization_settings(actor):
    ada = actor("ada@example.com")
    org = ada.create_org("Acme")

    response = ada.patch(
        f"/organizations/{org['id']}",
        json={
            "name": "Acme Group",
            "industry": "manufacturing",
            "timezone": "Asia/Kolkata",
            "document_categories": ["permit", "insurance_policy", "permit"],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Acme Group"
    assert body["slug"] == "acme"  # the URL identifier does not change with the name
    assert body["industry"] == "manufacturing"
    assert body["timezone"] == "Asia/Kolkata"
    # Canonical order, no duplicates.
    assert body["document_categories"] == ["insurance_policy", "permit"]


def test_update_organization_can_clear_optional_fields_but_not_the_name(actor):
    ada = actor("ada@example.com")
    org = ada.create_org("Acme", industry="manufacturing")

    response = ada.patch(f"/organizations/{org['id']}", json={"industry": None, "name": None})

    assert response.status_code == 200
    assert response.json()["industry"] is None
    assert response.json()["name"] == "Acme"


def test_update_organization_rejects_unknown_values(actor):
    ada = actor("ada@example.com")
    org = ada.create_org("Acme")

    assert ada.patch(f"/organizations/{org['id']}", json={"timezone": "Nowhere/City"}).status_code == 422
    response = ada.patch(f"/organizations/{org['id']}", json={"document_categories": ["tax_return"]})
    assert response.status_code == 422


def test_only_owner_and_admin_can_update_settings(actor):
    ada = actor("ada@example.com")
    org = ada.create_org("Acme")
    for role in ("compliance_manager", "member", "viewer"):
        person = actor(f"{role}@example.com")
        add_member(ada, org["id"], person, role)

        response = person.patch(f"/organizations/{org['id']}", json={"name": "Hijacked"})

        assert response.status_code == 403, role
        assert error_code(response) == "permission_denied"

    admin = actor("admin@example.com")
    add_member(ada, org["id"], admin, "admin")
    assert admin.patch(f"/organizations/{org['id']}", json={"name": "Acme Ltd"}).status_code == 200


def test_complete_onboarding_is_idempotent(actor):
    ada = actor("ada@example.com")
    org = ada.create_org("Acme")

    first = ada.post(f"/organizations/{org['id']}/onboarding/complete")
    second = ada.post(f"/organizations/{org['id']}/onboarding/complete")

    assert first.status_code == 200
    assert first.json()["onboarding_completed_at"] is not None
    assert second.json()["onboarding_completed_at"] == first.json()["onboarding_completed_at"]


def test_permissions_in_the_response_follow_the_role(actor):
    ada = actor("ada@example.com")
    org = ada.create_org("Acme")
    viewer = actor("viewer@example.com")
    add_member(ada, org["id"], viewer, "viewer")

    body = viewer.get(f"/organizations/{org['id']}").json()

    assert body["role"] == "viewer"
    assert "documents:read" in body["permissions"]
    assert "documents:write" not in body["permissions"]
    assert "members:invite" not in body["permissions"]


def test_options_lists_reference_data(client):
    response = client.get(f"{API}/meta/options")

    assert response.status_code == 200
    body = response.json()
    assert {"value": "manufacturing", "label": "Manufacturing", "description": None} in body["industries"]
    assert [role["value"] for role in body["roles"]] == [
        "owner",
        "admin",
        "compliance_manager",
        "member",
        "viewer",
    ]
    assert len(body["document_categories"]) == 9
    assert all(role["description"] for role in body["roles"])
