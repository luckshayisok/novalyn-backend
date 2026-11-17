"""Audit log: what is recorded, who can read it, and that it cannot be changed."""

import uuid

import pytest
from sqlalchemy import select

from app.models import AuditLog
from app.models.audit_log import AuditLogImmutableError
from app.services.audit import AuditService
from tests.helpers import add_member


@pytest.fixture
def org(actor):
    owner = actor("owner@example.com", "Olivia Owner")
    return owner, owner.create_org("Acme")["id"]


def _actions(actor_, org_id, **params):
    response = actor_.get(f"/organizations/{org_id}/audit-logs", params=params)
    assert response.status_code == 200, response.text
    return [entry["action"] for entry in response.json()["items"]]


def test_team_and_settings_events_are_recorded_newest_first(org, actor):
    owner, org_id = org
    member = actor("member@example.com", "Mia Member")
    membership_id = add_member(owner, org_id, member, "member")
    owner.patch(f"/organizations/{org_id}/members/{membership_id}", json={"role": "viewer"})
    owner.patch(f"/organizations/{org_id}", json={"name": "Acme Group"})
    owner.post(f"/organizations/{org_id}/onboarding/complete")
    owner.delete(f"/organizations/{org_id}/members/{membership_id}")

    assert _actions(owner, org_id) == [
        "member.removed",
        "organization.onboarding_completed",
        "organization.settings_changed",
        "member.role_changed",
        "member.joined",
        "member.invited",
        "organization.created",
    ]


def test_entries_carry_actor_resource_and_metadata(org, actor):
    owner, org_id = org
    member = actor("member@example.com", "Mia Member")
    membership_id = add_member(owner, org_id, member, "member")
    owner.patch(f"/organizations/{org_id}/members/{membership_id}", json={"role": "admin"})
    owner.patch(f"/organizations/{org_id}", json={"name": "Acme Group"})

    items = owner.get(f"/organizations/{org_id}/audit-logs").json()["items"]
    by_action = {entry["action"]: entry for entry in items}

    role_change = by_action["member.role_changed"]
    assert role_change["actor"]["email"] == "owner@example.com"
    assert role_change["resource_type"] == "membership"
    assert role_change["resource_id"] == membership_id
    assert role_change["metadata"]["from"] == "member"
    assert role_change["metadata"]["to"] == "admin"

    joined = by_action["member.joined"]
    assert joined["actor"]["email"] == "member@example.com"

    settings = by_action["organization.settings_changed"]
    assert settings["metadata"]["changes"] == {"name": {"from": "Acme", "to": "Acme Group"}}


def test_unchanged_settings_do_not_create_an_entry(org):
    owner, org_id = org

    owner.patch(f"/organizations/{org_id}", json={"name": "Acme"})

    assert _actions(owner, org_id) == ["organization.created"]


def test_pagination_and_action_filter(org):
    owner, org_id = org
    for index in range(5):
        owner.invite(org_id, f"person{index}@example.com")

    page = owner.get(f"/organizations/{org_id}/audit-logs", params={"page": 2, "page_size": 2}).json()

    assert page["total"] == 6
    assert page["page"] == 2
    assert len(page["items"]) == 2
    assert _actions(owner, org_id, action="member.") == ["member.invited"] * 5
    assert _actions(owner, org_id, action="organization.created") == ["organization.created"]
    assert owner.get(f"/organizations/{org_id}/audit-logs", params={"action": "x%"}).status_code == 422
    assert owner.get(f"/organizations/{org_id}/audit-logs", params={"page_size": 500}).status_code == 422


def test_only_roles_with_audit_permission_can_read(org, actor):
    owner, org_id = org
    expected = {"admin": 200, "compliance_manager": 200, "member": 403, "viewer": 403}
    for role, status in expected.items():
        person = actor(f"{role}@example.com")
        add_member(owner, org_id, person, role)

        assert person.get(f"/organizations/{org_id}/audit-logs").status_code == status, role


def test_there_is_no_route_to_change_or_delete_entries(org):
    owner, org_id = org
    entry_id = owner.get(f"/organizations/{org_id}/audit-logs").json()["items"][0]["id"]

    for method in ("PATCH", "PUT", "DELETE", "POST"):
        collection = owner.request(method, f"/organizations/{org_id}/audit-logs")
        single = owner.request(method, f"/organizations/{org_id}/audit-logs/{entry_id}")
        assert collection.status_code == 405, method
        assert single.status_code in (404, 405), method


def test_orm_rejects_updates_and_deletes(org, db):
    entry = db.scalars(select(AuditLog)).first()

    entry.action = "tampered"
    with pytest.raises(AuditLogImmutableError):
        db.commit()
    db.rollback()

    db.delete(entry)
    with pytest.raises(AuditLogImmutableError):
        db.commit()
    db.rollback()

    assert db.scalars(select(AuditLog)).one().action == "organization.created"


def test_metadata_must_not_contain_secrets(org, db):
    _, org_id = org

    for key in ("password", "reset_token", "client_secret"):
        with pytest.raises(ValueError, match="secrets"):
            AuditService(db).record(
                organization_id=uuid.UUID(org_id),
                actor_user_id=None,
                action="test.event",
                resource_type="test",
                metadata={key: "value"},
            )


def test_stored_entries_never_contain_credentials_or_link_tokens(org, actor, db):
    owner, org_id = org
    add_member(owner, org_id, actor("member@example.com"), "member")

    for entry in db.scalars(select(AuditLog)):
        flattened = str(entry.event_metadata).lower()
        assert "password" not in flattened
        assert "token" not in flattened
