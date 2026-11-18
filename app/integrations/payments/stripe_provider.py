"""Stripe: hosted Checkout for subscriptions, and webhooks for what happened next.

Uses Stripe's HTTP API directly (form-encoded requests, Bearer secret key).
Webhooks are verified with the `Stripe-Signature` header: an HMAC-SHA256 of
"{timestamp}.{body}" with the endpoint's signing secret, within 5 minutes.
"""

import hashlib
import hmac
import json
import logging
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.config import Settings
from app.integrations.payments.base import (
    CheckoutSession,
    EventKind,
    InvoiceData,
    PaymentProviderError,
    WebhookEvent,
    WebhookSignatureError,
)

logger = logging.getLogger(__name__)

API = "https://api.stripe.com/v1"
TOLERANCE_SECONDS = 300

_STATUS = {
    "active": "active",
    "trialing": "trialing",
    "past_due": "past_due",
    "unpaid": "past_due",
    "canceled": "canceled",
    "incomplete": "incomplete",
    "incomplete_expired": "canceled",
    "paused": "past_due",
}


def _timestamp(value: Any) -> datetime | None:
    return datetime.fromtimestamp(int(value), UTC) if value else None


def verify_signature(header: str | None, body: bytes, secret: str, *, now: float | None = None) -> None:
    if not header:
        raise WebhookSignatureError("Missing Stripe-Signature header")
    parts: dict[str, list[str]] = {}
    for item in header.split(","):
        key, _, value = item.strip().partition("=")
        parts.setdefault(key, []).append(value)
    try:
        timestamp = int(parts["t"][0])
    except (KeyError, ValueError) as error:
        raise WebhookSignatureError("Malformed Stripe-Signature header") from error
    if abs((now or time.time()) - timestamp) > TOLERANCE_SECONDS:
        raise WebhookSignatureError("Stripe webhook timestamp is outside the tolerance")
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, candidate) for candidate in parts.get("v1", [])):
        raise WebhookSignatureError("Stripe webhook signature does not match")


class StripeProvider:
    name = "stripe"
    label = "Card (Stripe)"
    currency = "USD"

    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self._settings = settings
        self._client = client

    def is_configured(self) -> bool:
        return bool(self._settings.stripe_secret_key and self._settings.stripe_webhook_secret)

    def price_reference(self, plan: str, interval: str) -> str | None:
        return getattr(self._settings, f"stripe_price_{plan}_{interval}", None)

    def _post(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
        client = self._client or httpx.Client(timeout=20)
        try:
            response = client.post(
                f"{API}{path}",
                data=data,
                headers={"Authorization": f"Bearer {self._settings.stripe_secret_key}"},
            )
        except httpx.HTTPError as error:
            raise PaymentProviderError("Stripe could not be reached. Try again in a few minutes.") from error
        finally:
            if self._client is None:
                client.close()
        if response.status_code >= 400:
            logger.error("Stripe request failed: %s %s", response.status_code, response.text[:500])
            raise PaymentProviderError("Stripe could not start the payment. Try again, or contact support.")
        return response.json()

    def create_checkout(
        self,
        *,
        organization_id: str,
        plan: str,
        interval: str,
        customer_email: str,
        customer_id: str | None,
        success_url: str,
        cancel_url: str,
    ) -> CheckoutSession:
        price = self.price_reference(plan, interval)
        if not price:
            raise PaymentProviderError("This plan is not available for online payment yet.")
        data: dict[str, Any] = {
            "mode": "subscription",
            "line_items[0][price]": price,
            "line_items[0][quantity]": 1,
            "success_url": success_url,
            "cancel_url": cancel_url,
            "client_reference_id": organization_id,
            "metadata[organization_id]": organization_id,
            "metadata[plan]": plan,
            "metadata[interval]": interval,
            "subscription_data[metadata][organization_id]": organization_id,
            "subscription_data[metadata][plan]": plan,
            "subscription_data[metadata][interval]": interval,
        }
        if customer_id:
            data["customer"] = customer_id
        else:
            data["customer_email"] = customer_email
        session = self._post("/checkout/sessions", data)
        return CheckoutSession(url=session["url"], reference=session.get("id"))

    def cancel_at_period_end(self, subscription_id: str) -> None:
        self._post(f"/subscriptions/{subscription_id}", {"cancel_at_period_end": "true"})

    def parse_webhook(self, headers: dict[str, str], body: bytes) -> WebhookEvent:
        secret = self._settings.stripe_webhook_secret
        if not secret:
            raise WebhookSignatureError("Stripe webhooks are not configured")
        verify_signature(headers.get("stripe-signature"), body, secret)
        event = json.loads(body)
        kind = event.get("type", "")
        obj = event.get("data", {}).get("object", {})
        base = {"event_id": event.get("id", ""), "event_type": kind, "raw": event}

        if kind == "checkout.session.completed" and obj.get("mode") == "subscription":
            metadata = obj.get("metadata") or {}
            # Bank debits can finish checkout before the money arrives:
            # only a paid session activates the plan.
            # The rest arrives later as customer.subscription.updated.
            paid = obj.get("payment_status") in ("paid", "no_payment_required")
            return WebhookEvent(
                **base,
                kind=EventKind.SUBSCRIPTION_UPDATED,
                organization_id=obj.get("client_reference_id") or metadata.get("organization_id"),
                plan=metadata.get("plan"),
                interval=metadata.get("interval"),
                status="active" if paid else "incomplete",
                provider_subscription_id=obj.get("subscription"),
                provider_customer_id=obj.get("customer"),
            )
        if kind in (
            "customer.subscription.created",
            "customer.subscription.updated",
            "customer.subscription.deleted",
        ):
            metadata = obj.get("metadata") or {}
            items = (obj.get("items") or {}).get("data") or [{}]
            period_end = obj.get("current_period_end") or items[0].get("current_period_end")
            deleted = kind.endswith("deleted")
            return WebhookEvent(
                **base,
                kind=EventKind.SUBSCRIPTION_CANCELED if deleted else EventKind.SUBSCRIPTION_UPDATED,
                organization_id=metadata.get("organization_id"),
                plan=metadata.get("plan"),
                interval=metadata.get("interval"),
                status="canceled" if deleted else _STATUS.get(obj.get("status", ""), "incomplete"),
                provider_subscription_id=obj.get("id"),
                provider_customer_id=obj.get("customer"),
                current_period_end=_timestamp(period_end),
                cancel_at_period_end=bool(obj.get("cancel_at_period_end")),
            )
        if kind in ("invoice.paid", "invoice.payment_failed"):
            paid = kind == "invoice.paid"
            # Newer API versions move the subscription under `parent`; accept both shapes.
            details = (
                (obj.get("parent") or {}).get("subscription_details") or obj.get("subscription_details") or {}
            )
            subscription_id = obj.get("subscription") or details.get("subscription")
            return WebhookEvent(
                **base,
                kind=EventKind.PAYMENT_SUCCEEDED if paid else EventKind.PAYMENT_FAILED,
                organization_id=(details.get("metadata") or {}).get("organization_id"),
                provider_subscription_id=subscription_id,
                provider_customer_id=obj.get("customer"),
                invoice=InvoiceData(
                    provider_invoice_id=obj.get("id", ""),
                    status="paid" if paid else "failed",
                    amount=int((obj.get("amount_paid") if paid else obj.get("amount_due")) or 0),
                    currency=str(obj.get("currency", "usd")).upper(),
                    issued_at=_timestamp(obj.get("created")) or datetime.now(UTC),
                    number=obj.get("number"),
                    hosted_url=obj.get("hosted_invoice_url"),
                    description="NOVALYN subscription",
                ),
            )
        return WebhookEvent(**base, kind=EventKind.IGNORED)
