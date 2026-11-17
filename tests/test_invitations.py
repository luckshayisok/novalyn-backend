"""Invitations: create, preview, accept, resend, revoke."""

import pytest

from app.core.config import get_settings
from tests.helpers import API, add_member, emails_to, error_code, invite_token, last_email

INVITEE = "newhire@example.com"


@pytest.fixture
def org(actor):
    owner = actor("owner@example.com", "Olivia Owner")
    organization = owner.create_org("Acme Facilities")
    return owner, organization["id"]


def test_invitation_flow_from_email_to_membership(org, actor, client):
    owner, org_id = org

    created = owner.invite(org_id, "NewHire@Example.com", "compliance_manager")

    assert created.status_code == 201
    assert created.json()["email"] == INVITEE
    assert created.json()["status"] == "pending"
    assert created.json()["invited_by_name"] == "Olivia Owner"
    message = last_email(INVITEE)
    assert "Acme Facilities" in message.subject
    assert "Compliance Manager" in message.text
    token = invite_token(message)

    preview = client.get(f"{API}/invitations/{token}")
    assert preview.status_code == 200
    assert preview.json() == {
        "organization_name": "Acme Facilities",
        "email": INVITEE,
        "role": "compliance_manager",
        "status": "pending",
        "invited_by_name": "Olivia Owner",
    }

    invitee = actor(INVITEE, "Nina Newhire", verified=False)
    accepted = invitee.post(f"/invitations/{token}/accept")

    assert accepted.status_code == 200
    assert accepted.json()["id"] == org_id
    assert accepted.json()["role"] == "compliance_manager"
    # Receiving the invitation link proves the address.
    assert invitee.get("/auth/me").json()["user"]["is_email_verified"] is True
    assert owner.get(f"/organizations/{org_id}/invitations").json() == []
    assert len(owner.get(f"/organizations/{org_id}/members").json()) == 2


def test_inviter_must_have_a_verified_email(actor):
    owner = actor("owner@example.com", verified=False)
    org_id = owner.create_org("Acme")["id"]

    response = owner.invite(org_id, INVITEE)

    assert response.status_code == 403
    assert error_code(response) == "email_not_verified"
    assert emails_to(INVITEE) == []


def test_ownership_is_never_granted_by_invitation(org):
    owner, org_id = org

    response = owner.invite(org_id, INVITEE, "owner")

    assert response.status_code == 403


def test_admin_can_invite_only_below_admin(org, actor):
    owner, org_id = org
    admin = actor("admin@example.com")
    add_member(owner, org_id, admin, "admin")

    assert admin.invite(org_id, "a@example.com", "admin").status_code == 403
    assert admin.invite(org_id, "b@example.com", "compliance_manager").status_code == 201
    assert owner.invite(org_id, "c@example.com", "admin").status_code == 201


@pytest.mark.parametrize("role", ["compliance_manager", "member", "viewer"])
def test_roles_without_invite_permission_cannot_see_or_send_invitations(org, actor, role):
    owner, org_id = org
    person = actor(f"{role}@example.com")
    add_member(owner, org_id, person, role)

    assert person.invite(org_id, INVITEE).status_code == 403
    assert person.get(f"/organizations/{org_id}/invitations").status_code == 403


def test_duplicate_pending_invitation_is_rejected(org):
    owner, org_id = org
    assert owner.invite(org_id, INVITEE).status_code == 201

    response = owner.invite(org_id, INVITEE.upper())

    assert response.status_code == 409
    assert error_code(response) == "already_invited"
    assert len(emails_to(INVITEE)) == 1


def test_existing_member_cannot_be_invited(org, actor):
    owner, org_id = org
    member = actor("member@example.com")
    add_member(owner, org_id, member)

    response = owner.invite(org_id, "member@example.com")

    assert response.status_code == 409
    assert error_code(response) == "already_member"


def test_invitation_can_only_be_accepted_by_the_invited_address(org, actor):
    owner, org_id = org
    owner.invite(org_id, INVITEE)
    token = invite_token(last_email(INVITEE))
    someone_else = actor("intruder@example.com")

    response = someone_else.post(f"/invitations/{token}/accept")

    assert response.status_code == 403
    assert error_code(response) == "invitation_email_mismatch"
    assert someone_else.get(f"/organizations/{org_id}").status_code == 404


def test_accepting_requires_authentication(org, client):
    owner, org_id = org
    owner.invite(org_id, INVITEE)
    token = invite_token(last_email(INVITEE))

    assert client.post(f"{API}/invitations/{token}/accept").status_code == 401


def test_revoked_invitation_cannot_be_accepted(org, actor, client):
    owner, org_id = org
    invitation_id = owner.invite(org_id, INVITEE).json()["id"]
    token = invite_token(last_email(INVITEE))

    assert owner.delete(f"/organizations/{org_id}/invitations/{invitation_id}").status_code == 204

    assert client.get(f"{API}/invitations/{token}").json()["status"] == "revoked"
    response = actor(INVITEE).post(f"/invitations/{token}/accept")
    assert response.status_code == 410
    assert error_code(response) == "invitation_revoked"
    assert owner.delete(f"/organizations/{org_id}/invitations/{invitation_id}").status_code == 409


def test_expired_invitation_cannot_be_accepted_and_can_be_replaced(org, actor, monkeypatch):
    owner, org_id = org
    monkeypatch.setattr(get_settings(), "invitation_ttl_days", -1)
    owner.invite(org_id, INVITEE)
    expired_token = invite_token(last_email(INVITEE))
    assert owner.get(f"/organizations/{org_id}/invitations").json()[0]["status"] == "expired"

    invitee = actor(INVITEE)
    expired = invitee.post(f"/invitations/{expired_token}/accept")
    assert expired.status_code == 410
    assert error_code(expired) == "invitation_expired"

    monkeypatch.setattr(get_settings(), "invitation_ttl_days", 7)
    assert owner.invite(org_id, INVITEE).status_code == 201
    open_invitations = owner.get(f"/organizations/{org_id}/invitations").json()
    assert [invitation["status"] for invitation in open_invitations] == ["pending"]
    assert invitee.accept_invitation_for().status_code == 200


def test_resend_issues_a_new_link_and_disables_the_old_one(org, actor, client):
    owner, org_id = org
    invitation_id = owner.invite(org_id, INVITEE).json()["id"]
    old_token = invite_token(last_email(INVITEE))

    response = owner.post(f"/organizations/{org_id}/invitations/{invitation_id}/resend")

    assert response.status_code == 200
    new_token = invite_token(last_email(INVITEE))
    assert new_token != old_token
    assert len(emails_to(INVITEE)) == 2
    assert client.get(f"{API}/invitations/{old_token}").status_code == 404
    assert actor(INVITEE).post(f"/invitations/{new_token}/accept").status_code == 200


def test_invitation_cannot_be_accepted_twice(org, actor):
    owner, org_id = org
    owner.invite(org_id, INVITEE)
    token = invite_token(last_email(INVITEE))
    invitee = actor(INVITEE)
    assert invitee.post(f"/invitations/{token}/accept").status_code == 200

    response = invitee.post(f"/invitations/{token}/accept")

    assert response.status_code == 410
    assert error_code(response) == "invitation_accepted"


def test_unknown_invitation_token_is_not_found(client):
    response = client.get(f"{API}/invitations/{'z' * 43}")

    assert response.status_code == 404


def test_invitation_email_escapes_organization_name(actor):
    owner = actor("owner@example.com", "Olivia Owner")
    org_id = owner.create_org('<script>alert("x")</script> Ltd')["id"]

    owner.invite(org_id, INVITEE)

    html = last_email(INVITEE).html
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
