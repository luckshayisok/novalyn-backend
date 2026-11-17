"""Tenant isolation: organization A must never see or change organization B's data.

These tests attack the boundary the way a curious customer would: by swapping
IDs in URLs for ones that belong to another organization.
"""

import uuid

import pytest
from sqlalchemy import select

from app.core.permissions import Role
from app.core.tenancy import clear_tenant, set_tenant
from app.models import AuditLog, Invitation, Membership
from app.repositories.base import TenantMismatchError
from app.repositories.organizations import InvitationRepository, MembershipRepository
from tests.helpers import add_member


@pytest.fixture
def two_orgs(actor):
    alice = actor("alice@a.example", "Alice A")
    bob = actor("bob@b.example", "Bob B")
    org_a = alice.create_org("Alpha Industries")["id"]
    org_b = bob.create_org("Beta Logistics")["id"]

    carol = actor("carol@b.example", "Carol B")
    carol_membership = add_member(bob, org_b, carol, "member")
    invitation_b = bob.invite(org_b, "pending@b.example").json()["id"]

    return {
        "alice": alice,
        "bob": bob,
        "carol": carol,
        "org_a": org_a,
        "org_b": org_b,
        "carol_membership": carol_membership,
        "bob_membership": bob.membership_id(org_b, bob.email),
        "invitation_b": invitation_b,
    }


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "", None),
        ("PATCH", "", {"name": "Taken Over"}),
        ("POST", "/onboarding/complete", None),
        ("GET", "/members", None),
        ("GET", "/invitations", None),
        ("POST", "/invitations", {"email": "spy@a.example", "role": "admin"}),
        ("GET", "/audit-logs", None),
        ("POST", "/leave", None),
    ],
)
def test_non_member_gets_not_found_for_every_route_of_another_organization(two_orgs, method, path, body):
    alice, org_b = two_orgs["alice"], two_orgs["org_b"]

    response = alice.request(method, f"/organizations/{org_b}{path}", json=body)

    # 404, not 403: the response does not confirm that the organization exists.
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_response_for_foreign_and_nonexistent_organizations_is_identical(two_orgs):
    alice = two_orgs["alice"]

    foreign = alice.get(f"/organizations/{two_orgs['org_b']}")
    missing = alice.get(f"/organizations/{uuid.uuid4()}")

    assert foreign.status_code == missing.status_code == 404
    assert foreign.json()["error"]["message"] == missing.json()["error"]["message"]


def test_cannot_change_another_organizations_member_through_own_organization(two_orgs):
    alice, org_a = two_orgs["alice"], two_orgs["org_a"]
    foreign_membership = two_orgs["carol_membership"]

    change = alice.patch(f"/organizations/{org_a}/members/{foreign_membership}", json={"role": "viewer"})
    remove = alice.delete(f"/organizations/{org_a}/members/{foreign_membership}")

    assert change.status_code == 404
    assert remove.status_code == 404
    # Carol is untouched in her own organization.
    members = two_orgs["bob"].get(f"/organizations/{two_orgs['org_b']}/members").json()
    carol = next(m for m in members if m["user"]["email"] == "carol@b.example")
    assert carol["role"] == "member"


def test_cannot_resend_or_revoke_another_organizations_invitation(two_orgs):
    alice, org_a = two_orgs["alice"], two_orgs["org_a"]
    foreign_invitation = two_orgs["invitation_b"]

    resend = alice.post(f"/organizations/{org_a}/invitations/{foreign_invitation}/resend")
    revoke = alice.delete(f"/organizations/{org_a}/invitations/{foreign_invitation}")

    assert resend.status_code == 404
    assert revoke.status_code == 404
    invitations = two_orgs["bob"].get(f"/organizations/{two_orgs['org_b']}/invitations").json()
    assert [invitation["status"] for invitation in invitations] == ["pending"]


def test_lists_contain_only_the_callers_organization(two_orgs):
    alice, bob = two_orgs["alice"], two_orgs["bob"]

    alice_members = alice.get(f"/organizations/{two_orgs['org_a']}/members").json()
    alice_invitations = alice.get(f"/organizations/{two_orgs['org_a']}/invitations").json()
    alice_audit = alice.get(f"/organizations/{two_orgs['org_a']}/audit-logs").json()
    bob_audit = bob.get(f"/organizations/{two_orgs['org_b']}/audit-logs").json()

    assert [m["user"]["email"] for m in alice_members] == ["alice@a.example"]
    assert alice_invitations == []
    assert {entry["action"] for entry in alice_audit["items"]} == {"organization.created"}
    assert alice_audit["total"] == 1
    # Beta's trail: created, Carol invited, Carol joined, one more invitation.
    assert bob_audit["total"] == 4
    assert all("a.example" not in str(entry["metadata"]) for entry in bob_audit["items"])


def test_me_lists_only_own_memberships(two_orgs):
    organizations = two_orgs["alice"].get("/auth/me").json()["organizations"]

    assert [org["id"] for org in organizations] == [two_orgs["org_a"]]


def test_member_of_two_organizations_has_separate_roles(two_orgs):
    """Being an owner in one organization grants nothing in another."""
    alice, bob, org_b = two_orgs["alice"], two_orgs["bob"], two_orgs["org_b"]
    add_member(bob, org_b, alice, "viewer")

    assert alice.get(f"/organizations/{org_b}").json()["role"] == "viewer"
    assert alice.patch(f"/organizations/{org_b}", json={"name": "Mine Now"}).status_code == 403
    assert alice.invite(org_b, "friend@a.example").status_code == 403
    assert alice.get(f"/organizations/{org_b}/audit-logs").status_code == 403


def test_malformed_organization_id_is_rejected(two_orgs):
    response = two_orgs["alice"].get("/organizations/not-a-uuid/members")

    assert response.status_code == 422


# ------------------------------------------------------------ data-access layer


def test_orm_tenant_filter_hides_other_tenants_rows(two_orgs, db):
    org_a, org_b = uuid.UUID(two_orgs["org_a"]), uuid.UUID(two_orgs["org_b"])

    assert len(db.scalars(select(Membership)).all()) == 3  # no tenant set: unfiltered

    set_tenant(db, org_a)
    assert {m.organization_id for m in db.scalars(select(Membership))} == {org_a}
    assert db.scalars(select(Invitation)).all() == []
    assert {entry.organization_id for entry in db.scalars(select(AuditLog))} == {org_a}

    # Switching tenant must switch the filter (guards against a cached statement
    # keeping the first tenant's ID).
    set_tenant(db, org_b)
    assert {m.organization_id for m in db.scalars(select(Membership))} == {org_b}
    invitations = db.scalars(select(Invitation)).all()
    assert len(invitations) == 2  # Carol's accepted invitation and the pending one
    assert {invitation.organization_id for invitation in invitations} == {org_b}

    # A query that names another tenant explicitly still returns nothing.
    assert db.scalars(select(Membership).where(Membership.organization_id == org_a)).all() == []

    clear_tenant(db)
    assert len(db.scalars(select(Membership)).all()) == 3


def test_tenant_repository_cannot_read_another_tenants_row_by_id(two_orgs, db):
    org_a = uuid.UUID(two_orgs["org_a"])
    foreign_membership = uuid.UUID(two_orgs["carol_membership"])
    foreign_invitation = uuid.UUID(two_orgs["invitation_b"])

    assert MembershipRepository(db, org_a).get(foreign_membership) is None
    assert InvitationRepository(db, org_a).get(foreign_invitation) is None
    assert MembershipRepository(db, org_a).get_by_email("carol@b.example") is None


def test_tenant_repository_refuses_cross_tenant_writes(two_orgs, db):
    org_a, org_b = uuid.UUID(two_orgs["org_a"]), uuid.UUID(two_orgs["org_b"])
    foreign = Membership(organization_id=org_b, user_id=uuid.uuid4(), role=Role.OWNER.value)

    with pytest.raises(TenantMismatchError):
        MembershipRepository(db, org_a).add(foreign)

    existing = MembershipRepository(db, org_b).get(uuid.UUID(two_orgs["carol_membership"]))
    with pytest.raises(TenantMismatchError):
        MembershipRepository(db, org_a).delete(existing)
