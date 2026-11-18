"""Razorpay: subscriptions paid through Razorpay's hosted page, and signed webhooks.

Uses Razorpay's HTTP API with basic authentication (key id and secret).
Webhooks are verified with the `X-Razorpay-Signature` header: a hex
HMAC-SHA256 of the raw body with the webhook secret.
"""

import hashlib
import hmac
import json
import logging
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

API = "https://api.razorpay.com/v1"
# Razorpay subscriptions need a number of billing cycles; ten years either way.
TOTAL_COUNT = {"month": 120, "year": 10}

_STATUS = {
    "subscription.activated": "active",
    "subscription.charged": "active",
    "subscription.resumed": "active",
    "subscription.updated": None,  # keep whatever the entity's status says
    "subscription.pending": "past_due",
    "subscription.halted": "past_due",
    "subscription.paused": "past_due",
    "subscription.cancelled": "canceled",
    "subscription.completed": "canceled",
}
_ENTITY_STATUS = {
    "active": "active",
    "authenticated": "incomplete",
    "created": "incomplete",
    "pending": "past_due",
    "halted": "past_due",
    "paused": "past_due",
    "cancelled": "canceled",
    "completed": "canceled",
    "expired": "canceled",
}


def verify_signature(signature: str | None, body: bytes, secret: str) -> None:
    if not signature:
        raise WebhookSignatureError("Missing X-Razorpay-Signature header")
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise WebhookSignatureError("Razorpay webhook signature does not match")


def _timestamp(value: Any) -> datetime | None:
    return datetime.fromtimestamp(int(value), UTC) if value else None


class RazorpayProvider:
    name = "razorpay"
    label = "UPI, cards and netbanking (Razorpay)"
    currency = "INR"

    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self._settings = settings
        self._client = client

    def is_configured(self) -> bool:
        settings = self._settings
        return bool(
            settings.razorpay_key_id and settings.razorpay_key_secret and settings.razorpay_webhook_secret
        )

    def price_reference(self, plan: str, interval: str) -> str | None:
        return getattr(self._settings, f"razorpay_plan_{plan}_{interval}", None)

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        client = self._client or httpx.Client(timeout=20)
        try:
            response = client.post(
                f"{API}{path}",
                json=payload,
                auth=(self._settings.razorpay_key_id or "", self._settings.razorpay_key_secret or ""),
            )
        except httpx.HTTPError as error:
            raise PaymentProviderError(
                "Razorpay could not be reached. Try again in a few minutes."
            ) from error
        finally:
            if self._client is None:
                client.close()
        if response.status_code >= 400:
            logger.error("Razorpay request failed: %s %s", response.status_code, response.text[:500])
            raise PaymentProviderError("Razorpay could not start the payment. Try again, or contact support.")
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
        plan_id = self.price_reference(plan, interval)
        if not plan_id:
            raise PaymentProviderError("This plan is not available for online payment yet.")
        subscription = self._post(
            "/subscriptions",
            {
                "plan_id": plan_id,
                "total_count": TOTAL_COUNT[interval],
                "quantity": 1,
                "customer_notify": 1,
                "notes": {"organization_id": organization_id, "plan": plan, "interval": interval},
            },
        )
        url = subscription.get("short_url")
        if not url:
            raise PaymentProviderError("Razorpay did not return a payment link.")
        return CheckoutSession(url=url, reference=subscription.get("id"))

    def cancel_at_period_end(self, subscription_id: str) -> None:
        self._post(f"/subscriptions/{subscription_id}/cancel", {"cancel_at_cycle_end": 1})

    def parse_webhook(self, headers: dict[str, str], body: bytes) -> WebhookEvent:
        secret = self._settings.razorpay_webhook_secret
        if not secret:
            raise WebhookSignatureError("Razorpay webhooks are not configured")
        verify_signature(headers.get("x-razorpay-signature"), body, secret)
        event = json.loads(body)
        kind = event.get("event", "")
        payload = event.get("payload") or {}
        entity = (payload.get("subscription") or {}).get("entity") or {}
        notes = entity.get("notes") or {}
        # Razorpay sends the event id in a header; fall back to a stable key built from the body.
        event_id = headers.get("x-razorpay-event-id") or hashlib.sha256(body).hexdigest()
        base = {"event_id": event_id, "event_type": kind, "raw": event}

        if kind in _STATUS:
            status = _STATUS[kind] or _ENTITY_STATUS.get(entity.get("status", ""), "incomplete")
            invoice = None
            payment = (payload.get("payment") or {}).get("entity")
            if kind == "subscription.charged" and payment:
                invoice = InvoiceData(
                    provider_invoice_id=payment.get("invoice_id") or payment.get("id", ""),
                    status="paid",
                    amount=int(payment.get("amount") or 0),
                    currency=str(payment.get("currency", "INR")).upper(),
                    issued_at=_timestamp(payment.get("created_at")) or datetime.now(UTC),
                    description="NOVALYN subscription",
                )
            return WebhookEvent(
                **base,
                kind=EventKind.SUBSCRIPTION_CANCELED
                if status == "canceled"
                else EventKind.SUBSCRIPTION_UPDATED,
                organization_id=notes.get("organization_id"),
                plan=notes.get("plan"),
                interval=notes.get("interval"),
                status=status,
                provider_subscription_id=entity.get("id"),
                provider_customer_id=entity.get("customer_id"),
                current_period_end=_timestamp(entity.get("current_end")),
                invoice=invoice,
            )
        return WebhookEvent(**base, kind=EventKind.IGNORED)
