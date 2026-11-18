"""Organization deletion, account deletion and retention."""

import uuid
from datetime import timedelta

import pytest

from app.core.storage import get_storage
from app.core.time import utcnow
from app.models import (
    AuditLog,
    DocumentVersion,
    Notification,
    Organization,
    OutboxEvent,
    OutboxStatus,
    RefreshToken,
    User,
)
from app.services.data_lifecycle import OrganizationPurger, RetentionService
from app.services.plans import get_subscription
from tests.helpers import PASSWORD, add_member, emails_to, error_code


@pytest.fixture
def team(actor):
    owner = actor("owner@example.com", "Olivia Owner")
    org_id = owner.create_org("Acme")["id"]
    admin = actor("admin@example.com", "Adam Admin")
    member = actor("member@example.com", "Mia Member")
    add_member(owner, org_id, admin, "admin")
    add_member(owner, org_id, member, "member")
    return {"owner": owner, "admin": admin, "member": member, "org": org_id}


def _schedule(actor_, org_id, name="Acme"):
    return actor_.post(f"/organizations/{org_id}/deletion", json={"confirm_name": name})


# ------------------------------------------------------------- organization


def test_owner_schedules_and_cancels_deletion(team):
    owner, org = team["owner"], team["org"]
    assert error_code(_schedule(owner, org, "acme")) == "confirmation_mismatch"
    assert _schedule(team["admin"], org).status_code == 403  # only owners

    response = _schedule(owner, org)
    assert response.status_code == 200, response.text
    scheduled = response.json()["deletion_scheduled_for"]
    assert scheduled[:10] == (utcnow() + timedelta(days=30)).date().isoformat()
    # Owners and admins are told; members are not.
    assert any("will be deleted" in m.subject for m in emails_to("owner@example.com"))
    assert any("will be deleted" in m.subject for m in emails_to("admin@example.com"))
    assert not any("will be deleted" in m.subject for m in emails_to("member@example.com"))
    assert _schedule(owner, org).status_code == 409

    # The organization keeps working during the grace period.
    assert owner.upload(org).status_code == 201

    cancelled = owner.delete(f"/organizations/{org}/deletion")
    assert cancelled.status_code == 200
    assert cancelled.json()["deletion_scheduled_for"] is None
    actions = [entry["action"] for entry in owner.get(f"/organizations/{org}/audit-logs").json()["items"]]
    assert {"organization.deletion_scheduled", "organization.deletion_canceled"} <= set(actions)


def test_a_paid_subscription_must_be_cancelled_first(team, db):
    subscription = get_subscription(db, uuid.UUID(team["org"]))
    subscription.plan, subscription.provider, subscription.provider_subscription_id = (
        "growth",
        "stripe",
        "sub_1",
    )
    db.commit()
    response = _schedule(team["owner"], team["org"])
    assert response.status_code == 409
    assert error_code(response) == "cancel_subscription_first"


def test_after_the_grace_period_everything_is_erased(team, actor, db):
    owner, org = team["owner"], team["org"]
    owner.uploaded(org, name="Permit")
    other = actor("other@example.com", "Otto Other")
    other_org = other.create_org("Other")["id"]
    other.uploaded(other_org, name="Kept")
    keys = db.query(DocumentVersion.storage_key).filter_by(organization_id=uuid.UUID(org)).all()
    assert keys and all(get_storage().exists(key) for (key,) in keys)

    _schedule(owner, org)
    assert OrganizationPurger(db).run().organizations == []  # not due yet

    result = OrganizationPurger(db).run(now=utcnow() + timedelta(days=31))
    assert result.organizations == ["Acme"]
    db.expire_all()
    assert db.get(Organization, uuid.UUID(org)) is None
    assert db.query(DocumentVersion).filter_by(organization_id=uuid.UUID(org)).count() == 0
    assert db.query(AuditLog).filter_by(organization_id=uuid.UUID(org)).count() == 0
    assert not any(get_storage().exists(key) for (key,) in keys)
    # Accounts stay; other organizations are untouched.
    assert owner.get("/auth/me").status_code == 200
    assert other.get(f"/organizations/{other_org}/documents").json()["total"] == 1


def test_past_its_date_an_organization_is_gone_before_the_job_runs(team, db):
    organization = db.get(Organization, uuid.UUID(team["org"]))
    organization.deletion_scheduled_for = utcnow() - timedelta(minutes=1)
    db.commit()
    owner = team["owner"]
    assert owner.get(f"/organizations/{team['org']}").status_code == 404
    assert owner.get("/auth/me").json()["organizations"] == []


# ------------------------------------------------------------------ accounts


def test_deleting_an_account(team, db, new_client):
    member, org = team["member"], team["org"]
    wrong = member.request("DELETE", "/auth/me", json={"password": "not-the-password"})
    assert wrong.status_code == 401
    assert error_code(wrong) == "wrong_password"

    response = member.request("DELETE", "/auth/me", json={"password": PASSWORD})
    assert response.status_code == 204
    assert db.query(User).filter_by(email="member@example.com").count() == 0
    emails = [item["user"]["email"] for item in team["owner"].get(f"/organizations/{org}/members").json()]
    assert "member@example.com" not in emails

    entry = team["owner"].get(f"/organizations/{org}/audit-logs", params={"action": "member.account_deleted"})
    item = entry.json()["items"][0]
    assert item["metadata"] == {"role": "member"}  # no name or email kept
    # The address is free again, and the old session no longer works.
    assert member.get("/auth/me").status_code == 401
    login = new_client().post(
        "/api/v1/auth/login", json={"email": "member@example.com", "password": PASSWORD}
    )
    assert login.status_code == 401


def test_the_only_owner_must_hand_over_or_delete_first(team):
    owner, org = team["owner"], team["org"]
    blocked = owner.request("DELETE", "/auth/me", json={"password": PASSWORD})
    assert blocked.status_code == 409
    assert blocked.json()["error"]["details"] == {"organizations": ["Acme"]}

    _schedule(owner, org)  # an organization on its way out does not block
    assert owner.request("DELETE", "/auth/me", json={"password": PASSWORD}).status_code == 204


# ----------------------------------------------------------------- retention


def test_retention_removes_only_old_records(team, db):
    now = utcnow()
    user_id = uuid.UUID(team["member"].user["id"])
    org_id = uuid.UUID(team["org"])

    def event(age_days: int) -> OutboxEvent:
        created = now - timedelta(days=age_days)
        return OutboxEvent(
            organization_id=org_id, event_type="test", status=OutboxStatus.DONE, created_at=created
        )

    def session(token: str, expires_in_days: int) -> RefreshToken:
        expires = now + timedelta(days=expires_in_days)
        return RefreshToken(
            user_id=user_id, family_id=uuid.uuid4(), token_hash=token * 64, expires_at=expires
        )

    old_event, read_event, unread_event = event(100), event(1), event(1)
    db.add_all([old_event, read_event, unread_event, session("a", -40), session("b", 5)])
    db.flush()
    read_long_ago = now - timedelta(days=200)
    db.add_all(
        [
            Notification(organization_id=org_id, user_id=user_id, event_id=read_event.id, type="t",
                         title="old", body="", read_at=read_long_ago),
            Notification(organization_id=org_id, user_id=user_id, event_id=unread_event.id, type="t",
                         title="unread", body=""),
        ]
    )  # fmt: skip
    db.commit()

    removed = RetentionService(db).run()
    assert removed["sessions"] >= 1
    assert removed["read_notifications"] == 1
    assert removed["notification_events"] == 1
    db.expire_all()
    assert db.query(RefreshToken).filter_by(token_hash="b" * 64).count() == 1
    assert [n.title for n in db.query(Notification).filter_by(user_id=user_id)] == ["unread"]
    assert db.get(OutboxEvent, old_event.id) is None
    assert db.get(OutboxEvent, unread_event.id) is not None
