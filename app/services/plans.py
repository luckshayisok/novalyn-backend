"""What an organization's plan allows, and the checks that enforce it.

Every limit is checked here, on the server. The frontend only shows the same
information so people are not surprised.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import AppError
from app.core.plans import FEATURE_LABELS, PLANS, Feature, Plan, PlanSpec, first_plan_with
from app.core.time import utcnow
from app.models import Document, Entity, Invitation, Membership, Subscription, SubscriptionStatus


class PlanLimitError(AppError):
    status_code = 402
    code = "plan_limit_reached"
    message = "Your plan's limit has been reached."


class FeatureNotInPlanError(AppError):
    status_code = 402
    code = "feature_not_in_plan"
    message = "This is not included in your plan."


@dataclass(frozen=True)
class Usage:
    documents: int
    members: int
    pending_invitations: int
    locations: int


def get_subscription(db: Session, organization_id: uuid.UUID) -> Subscription:
    """The organization's subscription. Organizations created before billing existed get the default plan."""
    subscription = db.scalars(
        select(Subscription).where(Subscription.organization_id == organization_id)
    ).first()
    if subscription is None:
        subscription = Subscription(
            organization_id=organization_id,
            plan=get_settings().default_plan,
            status=SubscriptionStatus.ACTIVE,
            custom_limits={},
        )
        db.add(subscription)
        db.flush()
    return subscription


def usage(db: Session, organization_id: uuid.UUID) -> Usage:
    documents = db.scalar(
        select(func.count())
        .select_from(Document)
        .where(Document.organization_id == organization_id, Document.archived_at.is_(None))
    )
    members = db.scalar(
        select(func.count()).select_from(Membership).where(Membership.organization_id == organization_id)
    )
    pending = db.scalar(
        select(func.count())
        .select_from(Invitation)
        .where(
            Invitation.organization_id == organization_id,
            Invitation.accepted_at.is_(None),
            Invitation.revoked_at.is_(None),
            Invitation.expires_at > utcnow(),
        )
    )
    locations = db.scalar(
        select(func.count())
        .select_from(Entity)
        .where(
            Entity.organization_id == organization_id, Entity.type == "location", Entity.archived_at.is_(None)
        )
    )
    return Usage(documents or 0, members or 0, pending or 0, locations or 0)


class Entitlements:
    def __init__(self, db: Session, organization_id: uuid.UUID) -> None:
        self.db = db
        self.organization_id = organization_id
        self.subscription = get_subscription(db, organization_id)
        entitled = self.subscription.status in SubscriptionStatus.ENTITLED
        self.spec: PlanSpec = PLANS[Plan(self.subscription.plan)] if entitled else PLANS[Plan.FREE]
        custom = self.subscription.custom_limits or {}

        def limit(name: str) -> int | None:
            value = custom.get(name, getattr(self.spec, name))
            return int(value) if value is not None else None

        self.max_documents = limit("max_documents")
        self.max_members = limit("max_members")
        self.max_locations = limit("max_locations")

    @property
    def plan(self) -> Plan:
        return self.spec.plan

    def has(self, feature: Feature) -> bool:
        return feature in self.spec.features

    def require(self, feature: Feature) -> None:
        if not self.has(feature):
            needed = first_plan_with(feature)
            raise FeatureNotInPlanError(
                f"{FEATURE_LABELS[feature]} is part of the {needed.name} plan. Upgrade to use it.",
                details={"feature": feature.value, "plan": needed.plan.value},
            )

    def usage(self) -> Usage:
        return usage(self.db, self.organization_id)

    def check_documents(self, adding: int = 1) -> None:
        if self.max_documents is None:
            return
        current = self.usage().documents
        if current + adding > self.max_documents:
            raise PlanLimitError(
                f"Your {self.spec.name} plan includes {self.max_documents} active documents "
                f"and you have {current}. Archive documents you no longer need, or upgrade your plan.",
                details={"limit": "max_documents", "max": self.max_documents, "current": current},
            )

    def check_members(self, *, adding: int = 1, count_pending: bool = True) -> None:
        if self.max_members is None:
            return
        current = self.usage()
        taken = current.members + (current.pending_invitations if count_pending else 0)
        if taken + adding > self.max_members:
            people = "person" if self.max_members == 1 else "people"
            raise PlanLimitError(
                f"Your {self.spec.name} plan includes {self.max_members} {people}, "
                "counting open invitations. Revoke an invitation, remove a member, or upgrade your plan.",
                details={"limit": "max_members", "max": self.max_members, "current": taken},
            )

    def check_locations(self, adding: int = 1) -> None:
        if self.max_locations is None:
            return
        current = self.usage().locations
        if current + adding > self.max_locations:
            needed = first_plan_with(Feature.MULTIPLE_LOCATIONS)
            raise PlanLimitError(
                f"Your {self.spec.name} plan includes {self.max_locations} location. "
                f"More locations are part of the {needed.name} plan.",
                details={"limit": "max_locations", "max": self.max_locations, "current": current},
            )
