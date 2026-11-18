"""Subscriptions, payment-provider events and invoices."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, CheckConstraint, ForeignKey, String, UniqueConstraint, Uuid, false
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.tenancy import TenantScoped
from app.models.mixins import CreatedAt, Timestamps, UUIDPrimaryKey
from app.models.types import JSONType, UTCDateTime


class SubscriptionStatus:
    ACTIVE = "active"
    TRIALING = "trialing"
    PAST_DUE = "past_due"  # a payment failed; the plan stays while the provider retries
    CANCELED = "canceled"
    INCOMPLETE = "incomplete"  # checkout started, first payment not confirmed

    ALL = (ACTIVE, TRIALING, PAST_DUE, CANCELED, INCOMPLETE)
    # Statuses in which the paid plan applies.
    ENTITLED = (ACTIVE, TRIALING, PAST_DUE)


_STATUSES = ", ".join(f"'{value}'" for value in SubscriptionStatus.ALL)


class Subscription(UUIDPrimaryKey, Timestamps, TenantScoped, Base):
    """The organization's plan. One per organization."""

    __tablename__ = "subscriptions"
    __table_args__ = (
        UniqueConstraint("organization_id"),
        CheckConstraint(f"status IN ({_STATUSES})", name="status_valid"),
        CheckConstraint("plan IN ('free', 'starter', 'growth', 'business')", name="plan_valid"),
    )

    plan: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default=SubscriptionStatus.ACTIVE, nullable=False)
    billing_interval: Mapped[str | None] = mapped_column(String(16))  # month, year
    provider: Mapped[str | None] = mapped_column(String(32))  # stripe, razorpay, manual
    provider_customer_id: Mapped[str | None] = mapped_column(String(200))
    provider_subscription_id: Mapped[str | None] = mapped_column(String(200), index=True)
    # The plan the customer chose at checkout; applied when the provider confirms payment.
    pending_plan: Mapped[str | None] = mapped_column(String(32))
    current_period_end: Mapped[datetime | None] = mapped_column(UTCDateTime)
    cancel_at_period_end: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    # Business plans: limits agreed with the customer, for example {"max_documents": 5000}.
    custom_limits: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)


class BillingEvent(UUIDPrimaryKey, CreatedAt, Base):
    """A webhook received from a payment provider. Stored once, so a resend is not processed twice."""

    __tablename__ = "billing_events"
    __table_args__ = (UniqueConstraint("provider", "provider_event_id"),)

    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_event_id: Mapped[str] = mapped_column(String(200), nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="SET NULL"), index=True
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    error: Mapped[str | None] = mapped_column(String(500))


class Invoice(UUIDPrimaryKey, CreatedAt, TenantScoped, Base):
    """A charge reported by the payment provider: the billing history."""

    __tablename__ = "invoices"
    __table_args__ = (UniqueConstraint("provider", "provider_invoice_id"),)

    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_invoice_id: Mapped[str] = mapped_column(String(200), nullable=False)
    number: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(32), nullable=False)  # paid, failed, refunded
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)  # smallest currency unit
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    description: Mapped[str | None] = mapped_column(String(300))
    hosted_url: Mapped[str | None] = mapped_column(String(1000))
    issued_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
