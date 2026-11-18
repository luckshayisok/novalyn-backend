"""Subscriptions and payments.

Checkout sends people to the payment provider's own page and only records the
plan they chose. The plan changes when the provider's signed webhook confirms
payment. If no provider is configured, checkout says so: nothing here ever
pretends that a payment went through.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import AppError, ConflictError, NotFoundError, ValidationFailedError
from app.core.plans import PLANS, SELF_SERVE_PLANS, Plan
from app.core.tenancy import allow_all_tenants
from app.core.time import utcnow
from app.integrations.payments import all_providers, configured_providers
from app.integrations.payments.base import (
    EventKind,
    PaymentProvider,
    PaymentProviderError,
    WebhookEvent,
    WebhookSignatureError,
)
from app.models import BillingEvent, Invoice, Organization, Subscription, SubscriptionStatus
from app.services.audit import AuditService
from app.services.context import OrgContext
from app.services.plans import Entitlements, Usage, get_subscription

logger = logging.getLogger(__name__)

INTERVALS = ("month", "year")
INVOICE_HISTORY = 24


class PaymentsNotConfiguredError(ConflictError):
    code = "payments_not_configured"


class PaymentProviderFailedError(AppError):
    status_code = 502
    code = "payment_provider_error"


class InvalidWebhookError(AppError):
    status_code = 400
    code = "invalid_webhook"


@dataclass(frozen=True)
class ProviderOption:
    name: str
    label: str
    currency: str
    # (plan, interval) pairs this provider can take payment for.
    available: list[tuple[str, str]]


@dataclass
class BillingOverview:
    subscription: Subscription
    entitlements: Entitlements
    usage: Usage
    invoices: list[Invoice]
    providers: list[ProviderOption]
    sales_email: str


class BillingService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.audit = AuditService(db)

    # ------------------------------------------------------------- for people

    def providers(self) -> list[ProviderOption]:
        options = []
        for provider in configured_providers().values():
            available = [
                (plan.value, interval)
                for plan in SELF_SERVE_PLANS
                for interval in INTERVALS
                if provider.price_reference(plan.value, interval)
            ]
            options.append(ProviderOption(provider.name, provider.label, provider.currency, available))
        return options

    def overview(self, ctx: OrgContext) -> BillingOverview:
        entitlements = Entitlements(self.db, ctx.organization_id)
        invoices = list(
            self.db.scalars(
                select(Invoice)
                .where(Invoice.organization_id == ctx.organization_id)
                .order_by(Invoice.issued_at.desc())
                .limit(INVOICE_HISTORY)
            )
        )
        overview = BillingOverview(
            subscription=entitlements.subscription,
            entitlements=entitlements,
            usage=entitlements.usage(),
            invoices=invoices,
            providers=self.providers(),
            sales_email=get_settings().sales_email,
        )
        self.db.commit()  # keeps a subscription row created on first read
        return overview

    def start_checkout(self, ctx: OrgContext, *, plan: str, interval: str, provider_name: str | None) -> str:
        """Create a hosted checkout and return its URL. The plan does not change here."""
        settings = get_settings()
        if plan not in {p.value for p in SELF_SERVE_PLANS}:
            raise ValidationFailedError(
                f"This plan is not sold online. Contact {settings.sales_email}.", code="plan_not_self_serve"
            )
        if interval not in INTERVALS:
            raise ValidationFailedError("Choose monthly or yearly billing.", code="invalid_interval")

        providers = configured_providers()
        if not providers:
            raise PaymentsNotConfiguredError(
                f"Online payment is not set up yet. Contact {settings.sales_email} to change your plan."
            )
        if provider_name is None and len(providers) == 1:
            provider_name = next(iter(providers))
        provider = providers.get(provider_name or "")
        if provider is None:
            raise ValidationFailedError("Choose how you want to pay.", code="invalid_provider")
        if not provider.price_reference(plan, interval):
            raise ConflictError(
                f"The {PLANS[Plan(plan)].name} plan cannot be paid {interval}ly with {provider.label} yet.",
                code="plan_not_available",
            )

        subscription = get_subscription(self.db, ctx.organization_id)
        if (
            subscription.plan != Plan.FREE
            and subscription.status in SubscriptionStatus.ENTITLED
            and subscription.provider_subscription_id
            and not subscription.cancel_at_period_end
        ):
            raise ConflictError(
                f"Your organization already pays for the {PLANS[Plan(subscription.plan)].name} plan. "
                f"To change plans, contact {settings.sales_email}.",
                code="already_subscribed",
            )

        billing_url = f"{settings.frontend_url.rstrip('/')}/o/{ctx.organization.slug}/billing"
        customer_id = subscription.provider_customer_id if subscription.provider == provider.name else None
        try:
            session = provider.create_checkout(
                organization_id=str(ctx.organization_id),
                plan=plan,
                interval=interval,
                customer_email=ctx.user.email,
                customer_id=customer_id,
                success_url=f"{billing_url}?checkout=success",
                cancel_url=f"{billing_url}?checkout=canceled",
            )
        except PaymentProviderError as error:
            raise PaymentProviderFailedError(str(error)) from error

        subscription.pending_plan = plan
        self.audit.record_for(
            ctx,
            action="billing.checkout_started",
            resource_type="subscription",
            resource_id=subscription.id,
            metadata={"plan": plan, "interval": interval, "provider": provider.name},
        )
        self.db.commit()
        return session.url

    def cancel(self, ctx: OrgContext) -> Subscription:
        """Stop renewing at the end of the paid period. The plan stays until then."""
        subscription = get_subscription(self.db, ctx.organization_id)
        if subscription.plan == Plan.FREE or not subscription.provider_subscription_id:
            raise ConflictError("There is no paid subscription to cancel.", code="no_subscription")
        if subscription.cancel_at_period_end:
            raise ConflictError("The subscription is already set to end.", code="already_canceled")
        provider = configured_providers().get(subscription.provider or "")
        if provider is None:
            raise PaymentsNotConfiguredError(
                f"This subscription cannot be changed online right now. Contact {get_settings().sales_email}."
            )
        try:
            provider.cancel_at_period_end(subscription.provider_subscription_id)
        except PaymentProviderError as error:
            raise PaymentProviderFailedError(str(error)) from error
        subscription.cancel_at_period_end = True
        self.audit.record_for(
            ctx,
            action="billing.cancellation_requested",
            resource_type="subscription",
            resource_id=subscription.id,
            metadata={"plan": subscription.plan},
        )
        self.db.commit()
        return subscription

    # ------------------------------------------------------------- webhooks

    def handle_webhook(self, provider_name: str, headers: dict[str, str], body: bytes) -> str:
        """Verify, record and apply a provider event once. Returns processed, duplicate or ignored."""
        provider = all_providers().get(provider_name)
        if provider is None or not provider.is_configured():
            raise NotFoundError()
        # A verified webhook is a system event: it names its organization in signed data.
        allow_all_tenants(self.db)
        try:
            event = provider.parse_webhook(headers, body)
        except WebhookSignatureError as error:
            logger.warning("Rejected %s webhook: %s", provider_name, error)
            raise InvalidWebhookError("The webhook signature is not valid.") from error
        except (ValueError, json.JSONDecodeError) as error:
            raise InvalidWebhookError("The webhook body is not valid JSON.") from error
        if not event.event_id:
            raise InvalidWebhookError("The webhook has no event id.")

        record = self.db.scalars(
            select(BillingEvent).where(
                BillingEvent.provider == provider_name, BillingEvent.provider_event_id == event.event_id
            )
        ).first()
        if record is not None and record.processed_at is not None:
            return "duplicate"
        if record is None:
            record = BillingEvent(
                provider=provider_name,
                provider_event_id=event.event_id[:200],
                event_type=event.event_type[:100] or "unknown",
                payload=event.raw,
            )
            self.db.add(record)
            try:
                self.db.flush()
            except IntegrityError:
                # The same event is being processed by another request right now.
                self.db.rollback()
                return "duplicate"

        try:
            organization_id, outcome = self._apply(provider, event)
            record.organization_id = organization_id
            record.processed_at = utcnow()
            record.error = None if outcome != "unmatched" else "No matching organization"
            self.db.commit()
        except Exception:
            self.db.rollback()
            logger.exception("Could not apply %s webhook %s", provider_name, event.event_id)
            raise  # the provider retries
        return "ignored" if outcome in ("ignored", "unmatched") else "processed"

    def _find_subscription(self, provider: PaymentProvider, event: WebhookEvent) -> Subscription | None:
        if event.organization_id:
            try:
                organization_id = uuid.UUID(event.organization_id)
            except ValueError:
                organization_id = None
            if organization_id and self.db.get(Organization, organization_id) is not None:
                return get_subscription(self.db, organization_id)
        if event.provider_subscription_id:
            return self.db.scalars(
                select(Subscription).where(
                    Subscription.provider == provider.name,
                    Subscription.provider_subscription_id == event.provider_subscription_id,
                )
            ).first()
        return None

    def _apply(self, provider: PaymentProvider, event: WebhookEvent) -> tuple[uuid.UUID | None, str]:
        if event.kind == EventKind.IGNORED:
            return None, "ignored"
        subscription = self._find_subscription(provider, event)
        if subscription is None:
            logger.warning("%s event %s matches no organization", provider.name, event.event_id)
            return None, "unmatched"

        if event.invoice:
            self._record_invoice(subscription, provider, event)

        if event.kind == EventKind.SUBSCRIPTION_UPDATED:
            self._subscription_updated(subscription, provider, event)
        elif event.kind == EventKind.SUBSCRIPTION_CANCELED:
            self._subscription_ended(subscription, event)
        elif event.kind in (EventKind.PAYMENT_SUCCEEDED, EventKind.PAYMENT_FAILED):
            same = event.provider_subscription_id == subscription.provider_subscription_id
            if (
                same
                and event.kind == EventKind.PAYMENT_FAILED
                and subscription.status == SubscriptionStatus.ACTIVE
            ):
                subscription.status = SubscriptionStatus.PAST_DUE
                self._audit(subscription, "billing.payment_failed", {"plan": subscription.plan})
            elif (
                same
                and event.kind == EventKind.PAYMENT_SUCCEEDED
                and subscription.status == SubscriptionStatus.PAST_DUE
            ):
                subscription.status = SubscriptionStatus.ACTIVE
        return subscription.organization_id, "processed"

    def _subscription_updated(
        self, subscription: Subscription, provider: PaymentProvider, event: WebhookEvent
    ) -> None:
        status = event.status or SubscriptionStatus.INCOMPLETE
        entitled = status in SubscriptionStatus.ENTITLED
        different = (
            subscription.provider_subscription_id is not None
            and event.provider_subscription_id is not None
            and subscription.provider_subscription_id != event.provider_subscription_id
        )
        if different and not entitled:
            return  # news about an older subscription that is no longer the current one
        if not entitled:
            # Checkout started but not paid yet: remember the ids, keep the current plan.
            if status == SubscriptionStatus.INCOMPLETE:
                subscription.provider_customer_id = (
                    event.provider_customer_id or subscription.provider_customer_id
                )
                return
            if subscription.plan == Plan.FREE:
                return

        plan = event.plan if event.plan in {p.value for p in SELF_SERVE_PLANS} else None
        plan = plan or (subscription.pending_plan if entitled else None) or subscription.plan
        previous = (subscription.plan, subscription.status)

        subscription.status = status if status in SubscriptionStatus.ALL else SubscriptionStatus.INCOMPLETE
        if entitled:
            subscription.plan = plan
            if subscription.pending_plan == plan:
                subscription.pending_plan = None
        subscription.provider = provider.name
        subscription.provider_subscription_id = (
            event.provider_subscription_id or subscription.provider_subscription_id
        )
        subscription.provider_customer_id = event.provider_customer_id or subscription.provider_customer_id
        if event.interval in INTERVALS:
            subscription.billing_interval = event.interval
        if event.current_period_end:
            subscription.current_period_end = event.current_period_end
        if event.cancel_at_period_end is not None:
            subscription.cancel_at_period_end = event.cancel_at_period_end

        if previous != (subscription.plan, subscription.status):
            self._audit(
                subscription,
                "billing.plan_changed" if previous[0] != subscription.plan else "billing.status_changed",
                {
                    "from_plan": previous[0],
                    "plan": subscription.plan,
                    "from_status": previous[1],
                    "status": subscription.status,
                    "provider": provider.name,
                },
            )

    def _subscription_ended(self, subscription: Subscription, event: WebhookEvent) -> None:
        if (
            subscription.provider_subscription_id
            and event.provider_subscription_id
            and subscription.provider_subscription_id != event.provider_subscription_id
        ):
            return  # an older subscription ended; the current one is unaffected
        previous = subscription.plan
        subscription.plan = Plan.FREE
        subscription.status = SubscriptionStatus.ACTIVE
        subscription.provider_subscription_id = None
        subscription.billing_interval = None
        subscription.current_period_end = None
        subscription.cancel_at_period_end = False
        subscription.pending_plan = None
        if previous != Plan.FREE:
            self._audit(
                subscription, "billing.subscription_ended", {"from_plan": previous, "plan": Plan.FREE.value}
            )

    def _record_invoice(
        self, subscription: Subscription, provider: PaymentProvider, event: WebhookEvent
    ) -> None:
        data = event.invoice
        assert data is not None
        if not data.provider_invoice_id:
            return
        invoice = self.db.scalars(
            select(Invoice).where(
                Invoice.provider == provider.name, Invoice.provider_invoice_id == data.provider_invoice_id
            )
        ).first()
        if invoice is None:
            invoice = Invoice(
                organization_id=subscription.organization_id,
                provider=provider.name,
                provider_invoice_id=data.provider_invoice_id[:200],
                amount=data.amount,
                currency=data.currency[:3],
                issued_at=data.issued_at,
                status=data.status,
            )
            self.db.add(invoice)
        # A failed invoice that is later paid keeps one row, now marked paid.
        invoice.status = data.status
        invoice.amount = data.amount
        invoice.number = (data.number or invoice.number or "")[:100] or None
        invoice.hosted_url = (data.hosted_url or invoice.hosted_url or "")[:1000] or None
        invoice.description = (data.description or "")[:300] or None

    def _audit(self, subscription: Subscription, action: str, metadata: dict) -> None:
        self.audit.record(
            organization_id=subscription.organization_id,
            actor_user_id=None,
            action=action,
            resource_type="subscription",
            resource_id=subscription.id,
            metadata=metadata,
        )
