"""Plan, subscription and billing schemas."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import Field

from app.core.plans import FEATURE_LABELS, PLAN_ORDER, PLANS, SELF_SERVE_PLANS, PlanSpec
from app.models import Invoice
from app.schemas.common import Schema
from app.services.billing import BillingOverview, ProviderOption


class FeatureOut(Schema):
    key: str
    label: str


class PlanOut(Schema):
    key: str
    name: str
    description: str
    max_documents: int | None
    max_members: int | None
    max_locations: int | None
    features: list[FeatureOut]
    monthly_price: dict[str, int] | None
    yearly_price: dict[str, int] | None
    self_serve: bool

    @classmethod
    def build(cls, spec: PlanSpec) -> "PlanOut":
        return cls(
            key=spec.plan.value,
            name=spec.name,
            description=spec.description,
            max_documents=spec.max_documents,
            max_members=spec.max_members,
            max_locations=spec.max_locations,
            features=[FeatureOut(key=feature.value, label=label) for feature, label in FEATURE_LABELS.items()
                      if feature in spec.features],
            monthly_price=spec.monthly_price,
            yearly_price=spec.yearly_price,
            self_serve=spec.plan in SELF_SERVE_PLANS,
        )  # fmt: skip


def plan_catalog() -> list[PlanOut]:
    return [PlanOut.build(PLANS[plan]) for plan in PLAN_ORDER]


class ProviderOut(Schema):
    name: str
    label: str
    currency: str
    available: list[str]  # "starter:month", "growth:year", ...

    @classmethod
    def build(cls, option: ProviderOption) -> "ProviderOut":
        return cls(
            name=option.name,
            label=option.label,
            currency=option.currency,
            available=[f"{plan}:{interval}" for plan, interval in option.available],
        )


class SubscriptionOut(Schema):
    plan: str
    plan_name: str
    effective_plan: str  # what applies now; Free while a payment is incomplete or after cancellation
    status: str
    billing_interval: str | None
    provider: str | None
    pending_plan: str | None
    current_period_end: datetime | None
    cancel_at_period_end: bool
    custom_limits: bool


class LimitOut(Schema):
    used: int
    limit: int | None


class InvoiceOut(Schema):
    id: uuid.UUID
    number: str | None
    status: str
    amount: int
    currency: str
    description: str | None
    hosted_url: str | None
    issued_at: datetime
    provider: str

    @classmethod
    def build(cls, invoice: Invoice) -> "InvoiceOut":
        return cls.model_validate(invoice)


class BillingOut(Schema):
    subscription: SubscriptionOut
    features: list[str]
    documents: LimitOut
    members: LimitOut
    locations: LimitOut
    invoices: list[InvoiceOut]
    providers: list[ProviderOut]
    payments_enabled: bool
    sales_email: str
    plans: list[PlanOut]

    @classmethod
    def build(cls, overview: BillingOverview) -> "BillingOut":
        subscription = overview.subscription
        entitlements = overview.entitlements
        usage = overview.usage
        return cls(
            subscription=SubscriptionOut(
                plan=subscription.plan,
                plan_name=PLANS[entitlements.plan].name,
                effective_plan=entitlements.plan.value,
                status=subscription.status,
                billing_interval=subscription.billing_interval,
                provider=subscription.provider,
                pending_plan=subscription.pending_plan,
                current_period_end=subscription.current_period_end,
                cancel_at_period_end=subscription.cancel_at_period_end,
                custom_limits=bool(subscription.custom_limits),
            ),
            features=sorted(feature.value for feature in entitlements.spec.features),
            documents=LimitOut(used=usage.documents, limit=entitlements.max_documents),
            members=LimitOut(used=usage.members + usage.pending_invitations, limit=entitlements.max_members),
            locations=LimitOut(used=usage.locations, limit=entitlements.max_locations),
            invoices=[InvoiceOut.build(invoice) for invoice in overview.invoices],
            providers=[ProviderOut.build(option) for option in overview.providers],
            payments_enabled=bool(overview.providers),
            sales_email=overview.sales_email,
            plans=plan_catalog(),
        )


class CheckoutRequest(Schema):
    plan: Literal["starter", "growth"]
    interval: Literal["month", "year"] = "month"
    provider: Literal["stripe", "razorpay"] | None = Field(default=None)


class CheckoutResponse(Schema):
    url: str
