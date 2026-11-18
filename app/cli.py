"""Command-line tasks: `python -m app.cli <command>`.

seed            Create a demo organization with one user per role. Development only.
scan-expiries   Emit the expiry reminders that are due now (Celery Beat runs this hourly).
dispatch        Deliver pending notifications now (Celery Beat runs this every minute).
set-plan <organization-slug> <free|starter|growth|business>
                Put an organization on a plan by hand, for example a Business plan agreed
                with sales. Payment is handled outside NOVALYN; the change is audited.
"""

import os
import sys

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.permissions import Role
from app.core.security import hash_password
from app.core.time import utcnow
from app.models import Membership, Organization, User
from app.repositories.organizations import OrganizationRepository
from app.repositories.users import UserRepository
from app.services.auth import validate_password

DEMO_ORGANIZATION = {"name": "Northwind Facilities", "slug": "northwind-facilities"}
DEMO_USERS: list[tuple[str, str, Role]] = [
    ("owner@northwind.example", "Olivia Owner", Role.OWNER),
    ("admin@northwind.example", "Adam Admin", Role.ADMIN),
    ("compliance@northwind.example", "Carla Compliance", Role.COMPLIANCE_MANAGER),
    ("member@northwind.example", "Milan Member", Role.MEMBER),
    ("viewer@northwind.example", "Vera Viewer", Role.VIEWER),
]


class SeedError(RuntimeError):
    pass


def seed(db: Session, password: str) -> list[str]:
    """Create the demo data if it is missing. Returns the emails that were created."""
    if get_settings().is_production:
        raise SeedError("Seeding demo data is not allowed in production.")
    validate_password(password, email="seed@northwind.example")

    organization = db.query(Organization).filter_by(slug=DEMO_ORGANIZATION["slug"]).one_or_none()
    if organization is None:
        organization = Organization(
            **DEMO_ORGANIZATION,
            industry="facility_management",
            company_size="51-200",
            timezone="UTC",
            document_categories=["business_license", "insurance_policy", "safety_certificate"],
            onboarding_completed_at=utcnow(),
        )
        OrganizationRepository(db).add(organization)
        db.flush()

    users = UserRepository(db)
    created: list[str] = []
    password_hash = hash_password(password)
    for email, full_name, role in DEMO_USERS:
        if users.get_by_email(email) is not None:
            continue
        user = User(email=email, full_name=full_name, password_hash=password_hash, email_verified_at=utcnow())
        users.add(user)
        db.flush()
        db.add(Membership(organization_id=organization.id, user_id=user.id, role=role.value))
        created.append(email)
    db.commit()
    return created


class PlanChangeError(RuntimeError):
    pass


def set_plan(db: Session, slug: str, plan: str) -> str:
    """Set an organization's plan manually. Returns a line describing the change."""
    from app.core.plans import PLANS, Plan
    from app.models import SubscriptionStatus
    from app.services.audit import AuditService
    from app.services.plans import get_subscription

    if plan not in {p.value for p in Plan}:
        raise PlanChangeError(f"Unknown plan {plan!r}. Choose free, starter, growth or business.")
    organization = db.query(Organization).filter_by(slug=slug).one_or_none()
    if organization is None:
        raise PlanChangeError(f"No organization has the slug {slug!r}.")
    subscription = get_subscription(db, organization.id)
    if subscription.provider_subscription_id and subscription.provider in ("stripe", "razorpay"):
        raise PlanChangeError(
            f"This organization pays through {subscription.provider}. Change the plan there instead."
        )
    previous = subscription.plan
    subscription.plan = plan
    subscription.status = SubscriptionStatus.ACTIVE
    subscription.provider = None if plan == Plan.FREE else "manual"
    subscription.pending_plan = None
    subscription.cancel_at_period_end = False
    AuditService(db).record(
        organization_id=organization.id,
        actor_user_id=None,
        action="billing.plan_changed",
        resource_type="subscription",
        resource_id=subscription.id,
        metadata={"from_plan": previous, "plan": plan, "provider": "manual"},
    )
    db.commit()
    return f"{organization.name}: {PLANS[Plan(previous)].name} -> {PLANS[Plan(plan)].name}"


def main(argv: list[str]) -> int:
    command = argv[1:]
    if command[:1] == ["set-plan"]:
        if len(command) != 3:
            print("Usage: python -m app.cli set-plan <organization-slug> <plan>", file=sys.stderr)
            return 2
        try:
            with SessionLocal() as db:
                print(set_plan(db, command[1], command[2]))
        except PlanChangeError as error:
            print(str(error), file=sys.stderr)
            return 1
        return 0
    if command == ["scan-expiries"]:
        from app.services.reminders import ReminderScanner

        with SessionLocal() as db:
            print(f"Reminders queued: {ReminderScanner(db).run()}")
        return 0
    if command == ["dispatch"]:
        from app.services.notifications import Dispatcher

        with SessionLocal() as db:
            print(f"Events delivered: {Dispatcher(db).run()}")
        return 0
    if command != ["seed"]:
        print(__doc__)
        return 2
    password = os.environ.get("SEED_PASSWORD")
    if not password:
        print("Set SEED_PASSWORD to the password the demo users should get.", file=sys.stderr)
        return 1
    try:
        with SessionLocal() as db:
            created = seed(db, password)
    except SeedError as error:
        print(str(error), file=sys.stderr)
        return 1
    if created:
        print("Created demo users in 'Northwind Facilities':")
        for email in created:
            print(f"  {email}")
    else:
        print("Demo data already exists; nothing to do.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
