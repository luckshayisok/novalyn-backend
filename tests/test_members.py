"""Team membership: listing, role changes, removal, leaving."""

import pytest

from tests.helpers import add_member, error_code


@pytest.fixture
def team(actor):
    """An organization with one member of each role."""
    owner = actor("owner@example.com", "Olivia Owner")
    org = owner.create_org("Acme")
    people = {"owner": owner}
    ids = {"owner": owner.membership_id(org["id"], owner.email)}
    for role in ("admin", "compliance_manager", "member", "viewer"):
        person = actor(f"{role}@example.com", f"{role.title()} Person")
        ids[role] = add_member(owner, org["id"], person, role)
        people[role] = person
    return org["id"], people, ids


def test_every_role_can_list_members(team):
    org_id, people, _ = team

    for role, person in people.items():
        response = person.get(f"/organizations/{org_id}/members")

        assert response.status_code == 200, role
        members = response.json()
        assert len(members) == 5
        assert [m["is_self"] for m in members].count(True) == 1
        assert next(m for m in members if m["is_self"])["user"]["email"] == person.email


def test_owner_can_assign_any_role(team):
    org_id, people, ids = team

    promoted = people["owner"].patch(
        f"/organizations/{org_id}/members/{ids['member']}", json={"role": "admin"}
    )
    second_owner = people["owner"].patch(
        f"/organizations/{org_id}/members/{ids['admin']}", json={"role": "owner"}
    )

    assert promoted.status_code == 200
    assert promoted.json()["role"] == "admin"
    assert second_owner.status_code == 200
    assert second_owner.json()["role"] == "owner"


def test_admin_can_manage_roles_below_admin_only(team):
    org_id, people, ids = team
    admin = people["admin"]

    allowed = admin.patch(
        f"/organizations/{org_id}/members/{ids['member']}", json={"role": "compliance_manager"}
    )
    promote_to_admin = admin.patch(f"/organizations/{org_id}/members/{ids['viewer']}", json={"role": "admin"})
    promote_to_owner = admin.patch(f"/organizations/{org_id}/members/{ids['viewer']}", json={"role": "owner"})
    demote_owner = admin.patch(f"/organizations/{org_id}/members/{ids['owner']}", json={"role": "member"})

    assert allowed.status_code == 200
    assert promote_to_admin.status_code == 403
    assert promote_to_owner.status_code == 403
    assert demote_owner.status_code == 403


@pytest.mark.parametrize("role", ["compliance_manager", "member", "viewer"])
def test_roles_without_member_management_cannot_change_roles_or_remove(team, role):
    org_id, people, ids = team
    target = ids["viewer"] if role != "viewer" else ids["member"]

    change = people[role].patch(f"/organizations/{org_id}/members/{target}", json={"role": "member"})
    remove = people[role].delete(f"/organizations/{org_id}/members/{target}")

    assert change.status_code == 403
    assert remove.status_code == 403


def test_nobody_can_change_their_own_role(team):
    org_id, people, ids = team

    response = people["owner"].patch(
        f"/organizations/{org_id}/members/{ids['owner']}", json={"role": "admin"}
    )

    assert response.status_code == 403
    assert error_code(response) == "cannot_change_own_role"


def test_role_change_rejects_unknown_roles(team):
    org_id, people, ids = team

    response = people["owner"].patch(
        f"/organizations/{org_id}/members/{ids['member']}", json={"role": "superuser"}
    )

    assert response.status_code == 422


def test_removed_member_loses_access(team):
    org_id, people, ids = team

    response = people["admin"].delete(f"/organizations/{org_id}/members/{ids['member']}")

    assert response.status_code == 204
    assert people["member"].get(f"/organizations/{org_id}").status_code == 404
    assert people["member"].get("/auth/me").json()["organizations"] == []
    assert len(people["owner"].get(f"/organizations/{org_id}/members").json()) == 4


def test_admin_cannot_remove_owner_or_another_admin(team, actor):
    org_id, people, ids = team
    second_admin = actor("admin2@example.com")
    second_admin_id = add_member(people["owner"], org_id, second_admin, "admin")

    assert people["admin"].delete(f"/organizations/{org_id}/members/{ids['owner']}").status_code == 403
    assert people["admin"].delete(f"/organizations/{org_id}/members/{second_admin_id}").status_code == 403


def test_removing_yourself_goes_through_leave(team):
    org_id, people, ids = team

    response = people["admin"].delete(f"/organizations/{org_id}/members/{ids['admin']}")

    assert response.status_code == 409
    assert error_code(response) == "use_leave"


def test_member_can_leave(team):
    org_id, people, _ = team

    response = people["member"].post(f"/organizations/{org_id}/leave")

    assert response.status_code == 204
    assert people["member"].get(f"/organizations/{org_id}").status_code == 404


def test_last_owner_cannot_leave_until_another_owner_exists(team):
    org_id, people, ids = team
    owner = people["owner"]

    blocked = owner.post(f"/organizations/{org_id}/leave")
    assert blocked.status_code == 409
    assert error_code(blocked) == "last_owner"

    owner.patch(f"/organizations/{org_id}/members/{ids['admin']}", json={"role": "owner"})
    assert owner.post(f"/organizations/{org_id}/leave").status_code == 204
    assert people["admin"].get(f"/organizations/{org_id}").json()["role"] == "owner"


def test_owner_can_demote_another_owner_but_one_always_remains(team):
    org_id, people, ids = team
    owner = people["owner"]
    owner.patch(f"/organizations/{org_id}/members/{ids['admin']}", json={"role": "owner"})

    demoted = owner.patch(f"/organizations/{org_id}/members/{ids['admin']}", json={"role": "member"})

    assert demoted.status_code == 200
    members = owner.get(f"/organizations/{org_id}/members").json()
    assert [m["role"] for m in members].count("owner") == 1
