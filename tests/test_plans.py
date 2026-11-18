"""Plan limits and features are enforced by the server."""

import uuid

import pytest

from app.core.database import system_session
from app.models import Subscription, SubscriptionStatus
from app.services.plans import get_subscription
from tests.helpers import add_member, error_code, in_days


def set_plan(org_id: str, plan: str, status: str = SubscriptionStatus.ACTIVE, **fields) -> None:
    with system_session() as db:
        subscription = get_subscription(db, uuid.UUID(org_id))
        subscription.plan = plan
        subscription.status = status
        for key, value in fields.items():
            setattr(subscription, key, value)
        db.commit()


@pytest.fixture
def owner(actor):
    return actor("owner@example.com", "Olivia Owner")


def test_new_organizations_start_on_the_configured_plan(owner, db):
    org_id = owner.create_org("Acme")["id"]
    subscription = db.query(Subscription).filter_by(organization_id=uuid.UUID(org_id)).one()
    assert subscription.plan == "business"  # DEFAULT_PLAN in the test settings
    assert subscription.status == "active"


def test_document_limit(owner):
    org_id = owner.create_org("Acme")["id"]
    set_plan(org_id, "free")
    for index in range(10):
        owner.uploaded(org_id, name=f"Permit {index}")

    response = owner.upload(org_id, name="One too many")
    assert response.status_code == 402
    assert error_code(response) == "plan_limit_reached"
    assert response.json()["error"]["details"] == {"limit": "max_documents", "max": 10, "current": 10}

    # Archiving frees a place; restoring it again is blocked while the plan is full.
    documents = owner.get(f"/organizations/{org_id}/documents").json()["items"]
    archived = documents[0]["id"]
    assert owner.post(f"/organizations/{org_id}/documents/{archived}/archive").status_code == 200
    assert owner.upload(org_id, name="Fits now").status_code == 201
    restore = owner.post(f"/organizations/{org_id}/documents/{archived}/restore")
    assert restore.status_code == 402


def test_custom_limits_override_the_plan(owner):
    org_id = owner.create_org("Acme")["id"]
    set_plan(org_id, "free", custom_limits={"max_documents": 1})
    owner.uploaded(org_id)
    assert owner.upload(org_id).status_code == 402


def test_member_limit_counts_open_invitations(owner, actor):
    org_id = owner.create_org("Acme")["id"]
    set_plan(org_id, "starter")  # 3 people
    add_member(owner, org_id, actor("one@example.com"))
    assert owner.invite(org_id, "two@example.com").status_code == 201

    response = owner.invite(org_id, "three@example.com")
    assert response.status_code == 402
    assert error_code(response) == "plan_limit_reached"
    assert response.json()["error"]["details"]["limit"] == "max_members"


def test_accepting_an_invitation_is_checked_again(owner, actor):
    org_id = owner.create_org("Acme")["id"]
    assert owner.invite(org_id, "late@example.com").status_code == 201
    set_plan(org_id, "free")  # the plan dropped to one person after the invitation went out
    late = actor("late@example.com")
    response = late.accept_invitation_for()
    assert response.status_code == 402


def test_advanced_roles_need_business(owner, actor):
    org_id = owner.create_org("Acme")["id"]
    set_plan(org_id, "growth")
    response = owner.invite(org_id, "viewer@example.com", "viewer")
    assert response.status_code == 402
    assert error_code(response) == "feature_not_in_plan"
    assert response.json()["error"]["details"] == {"feature": "advanced_permissions", "plan": "business"}
    assert owner.invite(org_id, "admin@example.com", "admin").status_code == 201

    membership = add_member(owner, org_id, actor("m@example.com"))
    change = owner.patch(f"/organizations/{org_id}/members/{membership}", json={"role": "compliance_manager"})
    assert change.status_code == 402


def test_entities_need_growth(owner):
    org_id = owner.create_org("Acme")["id"]
    set_plan(org_id, "starter")
    response = owner.post(f"/organizations/{org_id}/entities", json={"type": "supplier", "name": "Globex"})
    assert response.status_code == 402
    assert response.json()["error"]["details"]["plan"] == "growth"

    set_plan(org_id, "growth")
    owner.create_entity(org_id, "location", "Head office")
    second = owner.post(f"/organizations/{org_id}/entities", json={"type": "location", "name": "Depot"})
    assert second.status_code == 402
    assert response.json()["error"]["code"] == "feature_not_in_plan"
    assert second.json()["error"]["details"]["limit"] == "max_locations"


def test_custom_reminder_days_need_growth(owner):
    org_id = owner.create_org("Acme")["id"]
    set_plan(org_id, "starter")
    response = owner.patch(f"/organizations/{org_id}", json={"reminder_offsets": [45, 10]})
    assert response.status_code == 402
    # Other settings still save.
    assert owner.patch(f"/organizations/{org_id}", json={"name": "Acme Ltd"}).status_code == 200


def test_earlier_versions_need_document_history(owner):
    org_id = owner.create_org("Acme")["id"]
    document = owner.uploaded(org_id, name="Permit")
    url = f"/organizations/{org_id}/documents/{document['id']}"
    replaced = owner.client.post(
        f"/api/v1{url}/versions",
        headers=owner.headers,
        files={"file": ("new.pdf", b"%PDF-1.4\n%%EOF\n", "application/pdf")},
    )
    assert replaced.status_code == 201, replaced.text
    versions = {version["version_number"]: version["id"] for version in replaced.json()["versions"]}

    set_plan(org_id, "free")
    old = owner.post(f"{url}/versions/{versions[1]}/file-url", json={})
    assert old.status_code == 402
    assert error_code(old) == "feature_not_in_plan"
    # The current file is always available.
    assert owner.post(f"{url}/versions/{versions[2]}/file-url", json={}).status_code == 200


def test_lapsed_subscription_falls_back_to_free(owner):
    org_id = owner.create_org("Acme")["id"]
    set_plan(org_id, "growth", status=SubscriptionStatus.CANCELED)
    for index in range(10):
        owner.uploaded(org_id, name=f"Permit {index}", expiry_date=in_days(30))
    assert owner.upload(org_id).status_code == 402


def test_past_due_keeps_the_plan_while_the_provider_retries(owner):
    org_id = owner.create_org("Acme")["id"]
    set_plan(org_id, "growth", status=SubscriptionStatus.PAST_DUE)
    owner.create_entity(org_id, "supplier", "Globex")
