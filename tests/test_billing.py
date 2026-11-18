"""Billing: plans, checkout, signed webhooks, invoices and cancellation.

Payment providers are never called for real: their HTTP requests are replaced
by recorders, and webhooks are signed here with test secrets.
"""

import hashlib
import hmac
import json
import time
import uuid

import pytest

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.integrations.payments.razorpay_provider import RazorpayProvider
from app.integrations.payments.stripe_provider import StripeProvider
from app.models import BillingEvent, Invoice
from app.services.plans import get_subscription
from tests.helpers import API, add_member, error_code
from tests.test_plans import set_plan

STRIPE_WEBHOOK_SECRET = "whsec_test_only"
RAZORPAY_WEBHOOK_SECRET = "rzp_webhook_test_only"


@pytest.fixture
def owner(actor):
    return actor("owner@example.com", "Olivia Owner")


@pytest.fixture
def org(owner):
    org_id = owner.create_org("Acme")["id"]
    set_plan(org_id, "free")
    return org_id


@pytest.fixture
def stripe(monkeypatch):
    """Stripe configured with test values; API calls are recorded instead of sent."""
    settings = get_settings()
    for name, value in {
        "stripe_secret_key": "sk_test_only",
        "stripe_webhook_secret": STRIPE_WEBHOOK_SECRET,
        "stripe_price_starter_month": "price_starter_month",
        "stripe_price_growth_month": "price_growth_month",
        "stripe_price_growth_year": "price_growth_year",
    }.items():
        monkeypatch.setattr(settings, name, value)
    calls: list[tuple[str, dict]] = []

    def fake_post(self, path, data):
        calls.append((path, data))
        if path == "/checkout/sessions":
            return {"id": "cs_test_1", "url": "https://checkout.stripe.com/c/pay/cs_test_1"}
        return {"id": path.rsplit("/", 1)[-1], "cancel_at_period_end": True}

    monkeypatch.setattr(StripeProvider, "_post", fake_post)
    return calls


@pytest.fixture
def razorpay(monkeypatch):
    settings = get_settings()
    for name, value in {
        "razorpay_key_id": "rzp_test_only",
        "razorpay_key_secret": "rzp_secret_test_only",
        "razorpay_webhook_secret": RAZORPAY_WEBHOOK_SECRET,
        "razorpay_plan_starter_month": "plan_starter_month",
    }.items():
        monkeypatch.setattr(settings, name, value)
    calls: list[tuple[str, dict]] = []

    def fake_post(self, path, payload):
        calls.append((path, payload))
        return {"id": "sub_rzp_1", "short_url": "https://rzp.io/i/test1"}

    monkeypatch.setattr(RazorpayProvider, "_post", fake_post)
    return calls


def stripe_webhook(client, event: dict, *, secret: str = STRIPE_WEBHOOK_SECRET, timestamp: int | None = None):
    body = json.dumps(event).encode()
    timestamp = timestamp or int(time.time())
    signature = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return client.post(
        f"{API}/billing/webhooks/stripe",
        content=body,
        headers={"Stripe-Signature": f"t={timestamp},v1={signature}", "Content-Type": "application/json"},
    )


def razorpay_webhook(client, event: dict, event_id: str, *, secret: str = RAZORPAY_WEBHOOK_SECRET):
    body = json.dumps(event).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        f"{API}/billing/webhooks/razorpay",
        content=body,
        headers={"X-Razorpay-Signature": signature, "X-Razorpay-Event-Id": event_id},
    )


def subscription_event(
    org_id, *, event_id="evt_1", status="active", plan="growth", kind="updated", sub="sub_1"
):
    return {
        "id": event_id,
        "type": f"customer.subscription.{kind}",
        "data": {
            "object": {
                "id": sub,
                "customer": "cus_1",
                "status": status,
                "cancel_at_period_end": False,
                "metadata": {"organization_id": org_id, "plan": plan, "interval": "month"},
                "items": {"data": [{"current_period_end": int(time.time()) + 30 * 86400}]},
            }
        },
    }


def billing(owner, org_id):
    response = owner.get(f"/organizations/{org_id}/billing")
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------- overview


def test_public_plan_catalog(client):
    plans = client.get(f"{API}/billing/plans").json()
    assert [plan["key"] for plan in plans] == ["free", "starter", "growth", "business"]
    business = plans[-1]
    assert business["self_serve"] is False and business["monthly_price"] is None
    assert business["max_documents"] is None
    starter = plans[1]
    assert starter["monthly_price"] == {"USD": 1900, "INR": 149900}
    assert {feature["key"] for feature in starter["features"]} >= {"basic_reports", "pdf_export"}


def test_overview_shows_plan_usage_and_limits(owner, org, actor):
    owner.uploaded(org)
    owner.invite(org, "pending@example.com")  # over the Free limit is refused...
    overview = billing(owner, org)
    assert overview["subscription"]["plan"] == "free"
    assert overview["documents"] == {"used": 1, "limit": 10}
    assert overview["members"] == {"used": 1, "limit": 1}
    assert overview["features"] == ["automated_reminders"]
    assert overview["payments_enabled"] is False
    assert overview["providers"] == []
    assert overview["invoices"] == []

    member = actor("member@example.com")
    set_plan(org, "starter")
    add_member(owner, org, member)
    # Every member may see the plan, but only the owner may change it.
    assert billing(member, org)["subscription"]["plan"] == "starter"
    assert member.post(f"/organizations/{org}/billing/checkout", json={"plan": "growth"}).status_code == 403


# ---------------------------------------------------------------- checkout


def test_checkout_without_payment_setup_says_so(owner, org):
    response = owner.post(f"/organizations/{org}/billing/checkout", json={"plan": "starter"})
    assert response.status_code == 409
    assert error_code(response) == "payments_not_configured"
    assert "not set up" in response.json()["error"]["message"]
    assert billing(owner, org)["subscription"]["plan"] == "free"


def test_checkout_redirects_to_stripe_and_waits_for_payment(owner, org, stripe):
    response = owner.post(
        f"/organizations/{org}/billing/checkout", json={"plan": "growth", "interval": "year"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["url"] == "https://checkout.stripe.com/c/pay/cs_test_1"

    path, data = stripe[0]
    assert path == "/checkout/sessions"
    assert data["mode"] == "subscription"
    assert data["line_items[0][price]"] == "price_growth_year"
    assert data["client_reference_id"] == org
    assert data["customer_email"] == "owner@example.com"
    assert data["success_url"].endswith("/billing?checkout=success")

    overview = billing(owner, org)
    assert overview["subscription"]["plan"] == "free"  # nothing changes until Stripe confirms payment
    assert overview["subscription"]["pending_plan"] == "growth"
    assert overview["payments_enabled"] is True
    assert overview["providers"][0]["available"] == ["starter:month", "growth:month", "growth:year"]


def test_checkout_rejects_plans_that_are_not_sold_online(owner, org, stripe):
    business = owner.post(f"/organizations/{org}/billing/checkout", json={"plan": "business"})
    assert business.status_code == 422
    unpriced = owner.post(
        f"/organizations/{org}/billing/checkout", json={"plan": "starter", "interval": "year"}
    )
    assert unpriced.status_code == 409
    assert error_code(unpriced) == "plan_not_available"
    assert stripe == []


def test_checkout_with_razorpay(owner, org, razorpay):
    response = owner.post(
        f"/organizations/{org}/billing/checkout", json={"plan": "starter", "provider": "razorpay"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["url"] == "https://rzp.io/i/test1"
    path, payload = razorpay[0]
    assert path == "/subscriptions"
    assert payload["plan_id"] == "plan_starter_month"
    assert payload["notes"]["organization_id"] == org


# ---------------------------------------------------------------- webhooks


def test_stripe_webhook_activates_the_plan(owner, org, stripe, client):
    response = stripe_webhook(client, subscription_event(org))
    assert response.status_code == 200, response.text
    assert response.json()["message"] == "processed"

    overview = billing(owner, org)
    assert overview["subscription"]["plan"] == "growth"
    assert overview["subscription"]["status"] == "active"
    assert overview["subscription"]["provider"] == "stripe"
    assert overview["documents"]["limit"] == 500
    actions = [entry["action"] for entry in owner.get(f"/organizations/{org}/audit-logs").json()["items"]]
    assert "billing.plan_changed" in actions


def test_webhooks_need_a_valid_signature(owner, org, stripe, razorpay, client):
    forged = stripe_webhook(client, subscription_event(org), secret="whsec_wrong")
    assert forged.status_code == 400
    stale = stripe_webhook(client, subscription_event(org), timestamp=int(time.time()) - 3600)
    assert stale.status_code == 400
    unsigned = client.post(f"{API}/billing/webhooks/stripe", json=subscription_event(org))
    assert unsigned.status_code == 400
    forged_rzp = razorpay_webhook(client, {"event": "subscription.activated"}, "evt", secret="wrong")
    assert forged_rzp.status_code == 400
    assert billing(owner, org)["subscription"]["plan"] == "free"


def test_webhooks_are_refused_when_the_provider_is_not_configured(org, client):
    assert stripe_webhook(client, subscription_event(org)).status_code == 404


def test_a_resent_event_is_applied_once(owner, org, stripe, client):
    event = subscription_event(org)
    assert stripe_webhook(client, event).json()["message"] == "processed"
    set_plan(org, "starter")  # if the resend were applied again, it would move the plan back
    assert stripe_webhook(client, event).json()["message"] == "duplicate"
    assert billing(owner, org)["subscription"]["plan"] == "starter"
    with SessionLocal() as db:
        assert db.query(BillingEvent).count() == 1


def test_unpaid_checkout_does_not_change_the_plan(owner, org, stripe, client):
    event = {
        "id": "evt_cs",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "mode": "subscription",
                "payment_status": "unpaid",
                "client_reference_id": org,
                "subscription": "sub_1",
                "customer": "cus_1",
                "metadata": {"organization_id": org, "plan": "growth", "interval": "month"},
            }
        },
    }
    assert stripe_webhook(client, event).status_code == 200
    assert billing(owner, org)["subscription"]["plan"] == "free"

    event["id"], event["data"]["object"]["payment_status"] = "evt_cs_paid", "paid"
    assert stripe_webhook(client, event).status_code == 200
    assert billing(owner, org)["subscription"]["plan"] == "growth"


def test_invoices_and_failed_payments(owner, org, stripe, client):
    stripe_webhook(client, subscription_event(org))

    def invoice_event(event_id, kind, amount):
        return {
            "id": event_id,
            "type": kind,
            "data": {
                "object": {
                    "id": "in_1",
                    "number": "NOV-0001",
                    "customer": "cus_1",
                    "currency": "usd",
                    "amount_paid": amount if kind == "invoice.paid" else 0,
                    "amount_due": amount,
                    "created": int(time.time()),
                    "hosted_invoice_url": "https://invoice.stripe.com/i/test",
                    "parent": {
                        "subscription_details": {
                            "subscription": "sub_1",
                            "metadata": {"organization_id": org},
                        }
                    },
                }
            },
        }

    stripe_webhook(client, invoice_event("evt_fail", "invoice.payment_failed", 5900))
    overview = billing(owner, org)
    assert overview["subscription"]["status"] == "past_due"
    assert overview["subscription"]["plan"] == "growth"  # kept while Stripe retries
    assert overview["invoices"][0]["status"] == "failed"

    stripe_webhook(client, invoice_event("evt_paid", "invoice.paid", 5900))
    overview = billing(owner, org)
    assert overview["subscription"]["status"] == "active"
    assert len(overview["invoices"]) == 1  # the same invoice, now paid
    invoice = overview["invoices"][0]
    assert (invoice["status"], invoice["amount"], invoice["currency"], invoice["number"]) == (
        "paid",
        5900,
        "USD",
        "NOV-0001",
    )


def test_subscription_ending_returns_to_free(owner, org, stripe, client):
    stripe_webhook(client, subscription_event(org))
    # News about some other, older subscription does not touch the current one.
    stripe_webhook(client, subscription_event(org, event_id="evt_old", kind="deleted", sub="sub_old"))
    assert billing(owner, org)["subscription"]["plan"] == "growth"

    stripe_webhook(client, subscription_event(org, event_id="evt_end", kind="deleted"))
    overview = billing(owner, org)
    assert overview["subscription"]["plan"] == "free"
    assert overview["documents"]["limit"] == 10


def test_razorpay_charge_activates_and_records_the_payment(owner, org, razorpay, client):
    event = {
        "event": "subscription.charged",
        "payload": {
            "subscription": {
                "entity": {
                    "id": "sub_rzp_1",
                    "status": "active",
                    "customer_id": "cust_1",
                    "current_end": int(time.time()) + 30 * 86400,
                    "notes": {"organization_id": org, "plan": "starter", "interval": "month"},
                }
            },
            "payment": {
                "entity": {"id": "pay_1", "invoice_id": "inv_1", "amount": 149900, "currency": "INR",
                           "created_at": int(time.time())}
            },
        },
    }  # fmt: skip
    assert razorpay_webhook(client, event, "evt_rzp_1").json()["message"] == "processed"
    overview = billing(owner, org)
    assert overview["subscription"]["plan"] == "starter"
    assert overview["subscription"]["provider"] == "razorpay"
    assert overview["invoices"][0]["amount"] == 149900
    assert overview["invoices"][0]["currency"] == "INR"
    assert razorpay_webhook(client, event, "evt_rzp_1").json()["message"] == "duplicate"


def test_events_for_unknown_organizations_are_recorded_and_ignored(stripe, client):
    response = stripe_webhook(client, subscription_event(str(uuid.uuid4())))
    assert response.status_code == 200
    assert response.json()["message"] == "ignored"
    with SessionLocal() as db:
        assert db.query(BillingEvent).one().error == "No matching organization"
        assert db.query(Invoice).count() == 0


# ---------------------------------------------------------------- cancellation


def test_cancel_at_period_end(owner, org, stripe, client):
    assert owner.post(f"/organizations/{org}/billing/cancel").status_code == 409  # nothing to cancel on Free

    stripe_webhook(client, subscription_event(org))
    response = owner.post(f"/organizations/{org}/billing/cancel")
    assert response.status_code == 200, response.text
    assert stripe[-1] == ("/subscriptions/sub_1", {"cancel_at_period_end": "true"})
    overview = billing(owner, org)
    assert overview["subscription"]["cancel_at_period_end"] is True
    assert overview["subscription"]["plan"] == "growth"  # until the period ends
    assert owner.post(f"/organizations/{org}/billing/cancel").status_code == 409


def test_a_paying_organization_cannot_start_a_second_subscription(owner, org, stripe, client):
    stripe_webhook(client, subscription_event(org))
    response = owner.post(f"/organizations/{org}/billing/checkout", json={"plan": "starter"})
    assert response.status_code == 409
    assert error_code(response) == "already_subscribed"


def test_billing_is_tenant_isolated(owner, org, actor):
    outsider = actor("outsider@example.com")
    assert outsider.get(f"/organizations/{org}/billing").status_code == 404
    assert (
        outsider.post(f"/organizations/{org}/billing/checkout", json={"plan": "starter"}).status_code == 404
    )


def test_set_plan_command(owner, org, db):
    from app.cli import PlanChangeError
    from app.cli import set_plan as cli_set_plan

    slug = owner.get(f"/organizations/{org}").json()["slug"]
    assert cli_set_plan(db, slug, "business") == "Acme: Free -> Business"
    assert billing(owner, org)["documents"]["limit"] is None
    with pytest.raises(PlanChangeError):
        cli_set_plan(db, slug, "platinum")
    with pytest.raises(PlanChangeError):
        cli_set_plan(db, "no-such-org", "free")
    assert get_subscription(db, uuid.UUID(org)).provider == "manual"
