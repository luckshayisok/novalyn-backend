"""Payment provider interface.

Providers take people to their own hosted checkout and tell NOVALYN what
happened through signed webhooks. NOVALYN never handles card details, and a
plan changes only when a verified webhook says a payment went through.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


class PaymentProviderError(Exception):
    """The provider could not be reached or refused the request. The message is safe to show."""


class WebhookSignatureError(Exception):
    """The webhook did not come from the provider, or was altered on the way."""


@dataclass(frozen=True)
class CheckoutSession:
    url: str
    reference: str | None = None


@dataclass(frozen=True)
class InvoiceData:
    provider_invoice_id: str
    status: str  # paid, failed
    amount: int  # smallest currency unit
    currency: str
    issued_at: datetime
    number: str | None = None
    hosted_url: str | None = None
    description: str | None = None


class EventKind:
    SUBSCRIPTION_UPDATED = "subscription_updated"  # created, activated, renewed or changed
    SUBSCRIPTION_CANCELED = "subscription_canceled"
    PAYMENT_SUCCEEDED = "payment_succeeded"
    PAYMENT_FAILED = "payment_failed"
    IGNORED = "ignored"


@dataclass(frozen=True)
class WebhookEvent:
    """A provider event translated into what NOVALYN cares about."""

    event_id: str
    event_type: str  # the provider's own name, kept for the record
    kind: str
    organization_id: str | None = None
    plan: str | None = None
    interval: str | None = None
    status: str | None = None  # active, trialing, past_due, canceled, incomplete
    provider_subscription_id: str | None = None
    provider_customer_id: str | None = None
    current_period_end: datetime | None = None
    cancel_at_period_end: bool | None = None
    invoice: InvoiceData | None = None
    raw: dict = field(default_factory=dict)


class PaymentProvider(Protocol):
    name: str
    label: str
    currency: str

    def is_configured(self) -> bool: ...

    def price_reference(self, plan: str, interval: str) -> str | None:
        """The provider's id for this plan and interval (Stripe price, Razorpay plan), if configured."""

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
    ) -> CheckoutSession: ...

    def cancel_at_period_end(self, subscription_id: str) -> None: ...

    def parse_webhook(self, headers: dict[str, str], body: bytes) -> WebhookEvent:
        """Verify the signature and translate the event. Raises WebhookSignatureError."""
